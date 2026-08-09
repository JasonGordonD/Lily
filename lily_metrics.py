"""
lily_metrics.py — full session metrics accumulation (WO-LILY-UPGRADE-168
U3(b) + operator directive "she must use all the metrics she can").

Uses the framework's BLESSED, non-deprecated metrics surface (the coupling
audit confirmed `metrics_collected` is deprecated since 1.6.0 and logs a
warning on every event). Two sources, both still first-class at 1.6.8:

  1. `ChatMessage.metrics` — a per-turn `MetricsReport` (TypedDict) attached
     to every conversation item. Latency + turn-taking:
       user turns: transcription_delay, end_of_turn_delay,
                   on_user_turn_completed_delay
       agent turns: llm_node_ttft, tts_node_ttfb, playback_latency,
                    e2e_latency
  2. `session_usage_updated` -> `AgentSessionUsage.model_usage` — the
     cumulative token / character / audio-duration rollup per (provider,
     model). Emitted as a running total, so the latest snapshot wins.

Everything is folded into ONE comprehensive per-session summary written
into the session report metadata AND the mid-game heartbeat. The
turn-taking (transcription / end-of-turn) delays double as
WO-LILY-STT-001 Q2's incoming-quality signals.

Duck-typed and fully defensive — a missing key or unexpected shape is
skipped, never raised, so a metrics hiccup can't touch a live session.
"""

import logging

logger = logging.getLogger("lily_metrics")


def _pct(values, q):
    """The q-percentile (0..100) of a numeric list, nearest-rank. None for
    an empty list. Stdlib only — no numpy on the hot path."""
    xs = sorted(v for v in values if isinstance(v, (int, float)))
    if not xs:
        return None
    if len(xs) == 1:
        return round(float(xs[0]), 4)
    k = max(0, min(len(xs) - 1, int(round((q / 100.0) * (len(xs) - 1)))))
    return round(float(xs[k]), 4)


def _ms(seconds):
    """Framework latency fields are seconds; the report reads in ms."""
    return None if seconds is None else round(seconds * 1000, 1)


class LilyMetricsCollector:
    """Folds every per-turn MetricsReport and the latest session-usage
    rollup into one summary. One instance per session."""

    def __init__(self):
        # Per-turn latency (agent turns)
        self._llm_ttft = []
        self._tts_ttfb = []
        self._playback_latency = []
        self._e2e_latency = []
        # Per-turn turn-taking (user turns) — also STT-001 Q2 quality signals
        self._transcription_delay = []
        self._end_of_turn_delay = []
        self._on_user_turn_delay = []
        self._turns = 0
        # Cumulative usage (latest session_usage_updated snapshot)
        self._usage = None
        # Per-CALL LLM cache accounting (HOTFIX-007 Y1c). Distinct from the
        # per-turn report above: a turn can hide several calls (preemptive
        # regenerations, tool follow-ups), and only the per-call LLMMetrics
        # carries prompt_cached_tokens — the number that says whether the
        # Y1a static prefix is actually being served from Grok's cache.
        self._llm_calls = 0
        self._llm_prompt_tokens = 0
        self._llm_cached_tokens = 0
        self._llm_calls_with_hit = 0
        self._llm_call_ttft = []
        # Preemptive-generation outcomes (HOTFIX-007 Y2 measurement gate).
        # Counted off the framework's own log lines via a Filter tap —
        # "invalidated" is a WARNING (always emitted); "using" is DEBUG, so
        # `used` only populates when the deploy log level allows debug.
        # The Y2 settle-vs-split decision closes on `invalidated`.
        self._preemptive_used = 0
        self._preemptive_invalidated = 0
        # Round-trips per spoken turn (HOTFIX-007 Y4 measurement gate):
        # calls grouped by speech_id — >1 means tool follow-ups / regens
        # serialized inside one turn. Bounded ring of recent speech ids.
        self._llm_calls_by_speech = {}  # insertion-ordered; oldest evicted

    def collect_turn(self, report) -> None:
        """Fold one ChatMessage.metrics (a MetricsReport dict; total=False so
        every key is optional). Both agent-turn and user-turn reports pass
        through the same call — each contributes whichever fields it has."""
        if not report:
            return
        try:
            g = report.get if isinstance(report, dict) else (
                lambda k, d=None: getattr(report, k, d)
            )

            def _pos(key, bucket):
                v = g(key)
                if isinstance(v, (int, float)) and v > 0:
                    bucket.append(v)

            _pos("llm_node_ttft", self._llm_ttft)
            _pos("tts_node_ttfb", self._tts_ttfb)
            _pos("playback_latency", self._playback_latency)
            _pos("e2e_latency", self._e2e_latency)
            # Turn-taking delays can legitimately be ~0, so accept >= 0.
            for key, bucket in (
                ("transcription_delay", self._transcription_delay),
                ("end_of_turn_delay", self._end_of_turn_delay),
                ("on_user_turn_completed_delay", self._on_user_turn_delay),
            ):
                v = g(key)
                if isinstance(v, (int, float)) and v >= 0:
                    bucket.append(v)
            self._turns += 1
        except Exception as e:
            logger.warning("LILY_METRICS | TURN_SKIPPED | %s", e)

    def collect_llm_call(self, m) -> None:
        """Fold one per-call LLMMetrics from the LLM COMPONENT's
        `metrics_collected` event (HOTFIX-007 Y1c). The component-level
        event is first-class at 1.6.8 — the deprecation the U3(b) audit
        flagged is only on AgentSession.on("metrics_collected"), which we
        still avoid. Emits one INFO line per call so a live session's log
        answers "is the prompt prefix cache-hitting?" without a redeploy."""
        if m is None:
            return
        try:
            g = m.get if isinstance(m, dict) else (
                lambda k, d=None: getattr(m, k, d)
            )
            prompt = int(g("prompt_tokens") or 0)
            cached = int(g("prompt_cached_tokens") or 0)
            completion = int(g("completion_tokens") or 0)
            ttft = g("ttft")
            self._llm_calls += 1
            self._llm_prompt_tokens += prompt
            self._llm_cached_tokens += cached
            if cached > 0:
                self._llm_calls_with_hit += 1
            if isinstance(ttft, (int, float)) and ttft > 0:
                self._llm_call_ttft.append(ttft)
            speech = g("speech_id")
            if speech:
                by = self._llm_calls_by_speech
                by[speech] = by.get(speech, 0) + 1
                while len(by) > 200:
                    by.pop(next(iter(by)))
            hit = (100.0 * cached / prompt) if prompt else 0.0
            logger.info(
                "LILY_METRICS | LLM_CALL | request=%s speech=%s ttft_ms=%s "
                "prompt=%d cached=%d hit=%.1f%% completion=%d cancelled=%s",
                g("request_id") or "-", g("speech_id") or "-",
                _ms(ttft) if isinstance(ttft, (int, float)) else "-",
                prompt, cached, hit, completion, bool(g("cancelled")),
            )
        except Exception as e:
            logger.warning("LILY_METRICS | LLM_CALL_SKIPPED | %s", e)

    def attach_preemptive_tap(self, logger_name: str = "livekit.agents"):
        """Count the framework's preemptive-generation outcomes off its own
        log records (HOTFIX-007 Y2 measurement gate). A logging.Filter on
        the EXACT logger agent_activity logs through — no framework private
        API touched, nothing monkeypatched, and a Filter cannot flood or
        reformat anything (it only observes records already being logged).

        Reliability contract, stated honestly: "preemptive generation
        invalidated" is a WARNING and is always counted; "using preemptive
        generation" is DEBUG and is only counted when the deployment log
        level allows debug records to be created. Returns the filter so a
        caller/test can detach it."""
        collector = self

        class _PreemptiveOutcomeFilter(logging.Filter):
            def filter(self, record):
                try:
                    msg = record.getMessage()
                    if "preemptive generation invalidated" in msg:
                        collector._preemptive_invalidated += 1
                        logger.warning(
                            "LILY_METRICS | PREEMPTIVE_INVALIDATED | "
                            "total=%d — speculative reply discarded at turn "
                            "commit (context/transcript/tools changed)",
                            collector._preemptive_invalidated,
                        )
                    elif "using preemptive generation" in msg:
                        collector._preemptive_used += 1
                except Exception:
                    pass
                return True

        f = _PreemptiveOutcomeFilter()
        logging.getLogger(logger_name).addFilter(f)
        return f

    def collect_session_usage(self, usage) -> None:
        """Store the latest `session_usage_updated` rollup. `usage` is an
        AgentSessionUsage with `.model_usage: list[ModelUsage]`, each keyed
        by type literal ('llm_usage' / 'tts_usage' / 'stt_usage' / ...). The
        payload is a running total, so we rebuild from the latest snapshot
        (summing across models of the same type)."""
        if usage is None:
            return
        try:
            entries = getattr(usage, "model_usage", None) or []
            roll = {
                "llm_input_tokens": 0, "llm_input_cached_tokens": 0,
                "llm_output_tokens": 0,
                "tts_characters": 0, "tts_audio_duration_s": 0.0,
                "stt_audio_duration_s": 0.0,
                "models": 0,
            }
            for e in entries:
                t = getattr(e, "type", "")
                gi = lambda k: int(getattr(e, k, 0) or 0)
                gf = lambda k: float(getattr(e, k, 0) or 0.0)
                if t == "llm_usage":
                    roll["llm_input_tokens"] += gi("input_tokens")
                    roll["llm_input_cached_tokens"] += gi("input_cached_tokens")
                    roll["llm_output_tokens"] += gi("output_tokens")
                elif t == "tts_usage":
                    roll["tts_characters"] += gi("characters_count")
                    roll["tts_audio_duration_s"] += gf("audio_duration")
                elif t == "stt_usage":
                    roll["stt_audio_duration_s"] += gf("audio_duration")
                roll["models"] += 1
            roll["tts_audio_duration_s"] = round(roll["tts_audio_duration_s"], 2)
            roll["stt_audio_duration_s"] = round(roll["stt_audio_duration_s"], 2)
            self._usage = roll
        except Exception as e:
            logger.warning("LILY_METRICS | USAGE_SKIPPED | %s", e)

    def summary(self) -> dict:
        """The comprehensive block written into the session report metadata.
        Only non-empty sections appear, so a short/voice-only session
        doesn't pad the report with nulls."""
        out: dict = {"turns_measured": self._turns}
        latency = {}
        if self._llm_ttft:
            latency["llm_ttft_ms_p50"] = _ms(_pct(self._llm_ttft, 50))
            latency["llm_ttft_ms_p95"] = _ms(_pct(self._llm_ttft, 95))
        if self._tts_ttfb:
            latency["tts_ttfb_ms_p50"] = _ms(_pct(self._tts_ttfb, 50))
            latency["tts_ttfb_ms_p95"] = _ms(_pct(self._tts_ttfb, 95))
        if self._playback_latency:
            latency["playback_latency_ms_p50"] = _ms(_pct(self._playback_latency, 50))
        if self._e2e_latency:
            latency["e2e_latency_ms_p50"] = _ms(_pct(self._e2e_latency, 50))
            latency["e2e_latency_ms_p95"] = _ms(_pct(self._e2e_latency, 95))
        if latency:
            out["latency"] = latency
        if self._transcription_delay or self._end_of_turn_delay or self._on_user_turn_delay:
            out["turn_taking"] = {
                "transcription_delay_ms_p50": _ms(_pct(self._transcription_delay, 50)),
                "transcription_delay_ms_p95": _ms(_pct(self._transcription_delay, 95)),
                "end_of_turn_delay_ms_p50": _ms(_pct(self._end_of_turn_delay, 50)),
                "on_user_turn_completed_delay_ms_p50": _ms(_pct(self._on_user_turn_delay, 50)),
            }
        if self._usage is not None:
            out["usage"] = self._usage
        if self._llm_calls:
            cache = {
                "calls": self._llm_calls,
                "prompt_tokens": self._llm_prompt_tokens,
                "cached_tokens": self._llm_cached_tokens,
                "cache_hit_rate": round(
                    self._llm_cached_tokens / self._llm_prompt_tokens, 4
                ) if self._llm_prompt_tokens else 0.0,
                "calls_with_cache_hit": self._llm_calls_with_hit,
            }
            if self._llm_call_ttft:
                cache["ttft_ms_p50"] = _ms(_pct(self._llm_call_ttft, 50))
                cache["ttft_ms_p95"] = _ms(_pct(self._llm_call_ttft, 95))
            if self._llm_calls_by_speech:
                per_turn = list(self._llm_calls_by_speech.values())
                cache["calls_per_turn_p50"] = _pct(per_turn, 50)
                cache["calls_per_turn_max"] = max(per_turn)
                cache["turns_with_multiple_calls"] = sum(
                    1 for n in per_turn if n > 1
                )
            out["llm_cache"] = cache
        if self._preemptive_used or self._preemptive_invalidated:
            out["preemptive"] = {
                "used": self._preemptive_used,
                "invalidated": self._preemptive_invalidated,
            }
        return out
