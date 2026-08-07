"""
lily_metrics.py — full session metrics accumulation (WO-LILY-UPGRADE-168
U3(b) + operator directive "she must use all the metrics she can").

1.6.8 replaces the fragile `conversation_item_added` -> `item.metrics`
latency read with the first-class `metrics_collected` event (typed
LLM/STT/TTS/EOU/VAD metrics) plus the new `session_usage_updated` rollup.
This module consumes ALL of it: every typed metric the framework emits is
folded into one comprehensive per-session summary written into the session
report — token usage (incl. cached), TTS characters, STT audio duration,
and the full latency family (LLM ttft, TTS ttfb, EOU / transcription
delays, VAD inference).

Deliberately duck-typed on each metric's `.type` string rather than
coupled to the plugin classes, so it is fully unit-testable with light
fakes and can't break a session on an unexpected shape — every collect is
defensive and a bad metric is skipped, never raised.

The EOU / transcription-delay and STT-audio-duration lines here are also
the incoming-quality signals WO-LILY-STT-001 Q2 asks the session report to
carry, so the two WOs share this one summary.
"""

import logging

logger = logging.getLogger("lily_metrics")


def _pct(values, q):
    """The q-percentile (0..100) of a numeric list, nearest-rank. Returns
    None for an empty list. Stdlib only — no numpy on the hot path."""
    xs = sorted(v for v in values if isinstance(v, (int, float)))
    if not xs:
        return None
    if len(xs) == 1:
        return round(float(xs[0]), 4)
    k = max(0, min(len(xs) - 1, int(round((q / 100.0) * (len(xs) - 1)))))
    return round(float(xs[k]), 4)


def _avg(values):
    xs = [v for v in values if isinstance(v, (int, float))]
    return round(sum(xs) / len(xs), 4) if xs else None


class LilyMetricsCollector:
    """Folds every `metrics_collected` metric (and the `session_usage_updated`
    rollup) into one summary. One instance per session."""

    def __init__(self):
        # LLM
        self._llm_ttft = []
        self._llm_tps = []
        self._llm_prompt_tokens = 0
        self._llm_cached_tokens = 0
        self._llm_completion_tokens = 0
        self._llm_total_tokens = 0
        self._llm_count = 0
        self._llm_cancelled = 0
        # STT
        self._stt_count = 0
        self._stt_audio_duration = 0.0
        self._stt_reused = 0
        # TTS
        self._tts_ttfb = []
        self._tts_count = 0
        self._tts_characters = 0
        self._tts_audio_duration = 0.0
        self._tts_cancelled = 0
        # EOU (turn-taking / incoming-quality signals)
        self._eou_delay = []
        self._transcription_delay = []
        self._on_user_turn_delay = []
        # VAD
        self._vad_inference_count = 0
        self._vad_inference_duration = 0.0
        self._vad_idle_time = None
        # session_usage_updated rollup (latest snapshot)
        self._session_usage = None
        self._total_metrics = 0

    def collect(self, metric) -> None:
        """Fold one metric object from a `metrics_collected` event. Duck-typed
        on `.type`; never raises."""
        if metric is None:
            return
        try:
            mtype = getattr(metric, "type", None)
            g = lambda k, d=None: getattr(metric, k, d)
            if mtype == "llm_metrics":
                self._llm_count += 1
                if g("ttft", 0) and g("ttft") > 0:
                    self._llm_ttft.append(g("ttft"))
                if g("tokens_per_second", 0) and g("tokens_per_second") > 0:
                    self._llm_tps.append(g("tokens_per_second"))
                self._llm_prompt_tokens += int(g("prompt_tokens", 0) or 0)
                self._llm_cached_tokens += int(g("prompt_cached_tokens", 0) or 0)
                self._llm_completion_tokens += int(g("completion_tokens", 0) or 0)
                self._llm_total_tokens += int(g("total_tokens", 0) or 0)
                if g("cancelled"):
                    self._llm_cancelled += 1
            elif mtype == "stt_metrics":
                self._stt_count += 1
                self._stt_audio_duration += float(g("audio_duration", 0) or 0)
                if g("connection_reused"):
                    self._stt_reused += 1
            elif mtype == "tts_metrics":
                self._tts_count += 1
                if g("ttfb", 0) and g("ttfb") > 0:
                    self._tts_ttfb.append(g("ttfb"))
                self._tts_characters += int(g("characters_count", 0) or 0)
                self._tts_audio_duration += float(g("audio_duration", 0) or 0)
                if g("cancelled"):
                    self._tts_cancelled += 1
            elif mtype == "eou_metrics":
                if g("end_of_utterance_delay") is not None:
                    self._eou_delay.append(g("end_of_utterance_delay"))
                if g("transcription_delay") is not None:
                    self._transcription_delay.append(g("transcription_delay"))
                if g("on_user_turn_completed_delay") is not None:
                    self._on_user_turn_delay.append(g("on_user_turn_completed_delay"))
            elif mtype == "vad_metrics":
                self._vad_inference_count += int(g("inference_count", 0) or 0)
                self._vad_inference_duration += float(g("inference_duration_total", 0) or 0)
                self._vad_idle_time = g("idle_time", self._vad_idle_time)
            else:
                # Unknown/realtime metric — counted but not decomposed.
                pass
            self._total_metrics += 1
        except Exception as e:
            logger.warning("LILY_METRICS | COLLECT_SKIPPED | %s", e)

    def collect_session_usage(self, usage) -> None:
        """Store the latest `session_usage_updated` rollup (AgentSessionUsage
        — the framework's authoritative token/audio aggregate). Kept as the
        blessed cross-check against our per-metric sums."""
        if usage is None:
            return
        try:
            model_usage = getattr(usage, "model_usage", None)
            self._session_usage = {
                "model_usage_entries": len(model_usage) if model_usage else 0,
            }
        except Exception as e:
            logger.warning("LILY_METRICS | SESSION_USAGE_SKIPPED | %s", e)

    def summary(self) -> dict:
        """The comprehensive block written into the session report metadata.
        Only non-empty sections appear, so a voice-only or short session
        doesn't pad the report with nulls."""
        out: dict = {"metrics_events": self._total_metrics}
        if self._llm_count:
            out["llm"] = {
                "calls": self._llm_count,
                "ttft_ms_p50": _ms(_pct(self._llm_ttft, 50)),
                "ttft_ms_p95": _ms(_pct(self._llm_ttft, 95)),
                "tokens_per_second_avg": _avg(self._llm_tps),
                "prompt_tokens": self._llm_prompt_tokens,
                "prompt_cached_tokens": self._llm_cached_tokens,
                "completion_tokens": self._llm_completion_tokens,
                "total_tokens": self._llm_total_tokens,
                "cancelled": self._llm_cancelled,
            }
        if self._stt_count:
            out["stt"] = {
                "calls": self._stt_count,
                "audio_duration_s": round(self._stt_audio_duration, 2),
                "connection_reused": self._stt_reused,
            }
        if self._tts_count:
            out["tts"] = {
                "calls": self._tts_count,
                "ttfb_ms_p50": _ms(_pct(self._tts_ttfb, 50)),
                "ttfb_ms_p95": _ms(_pct(self._tts_ttfb, 95)),
                "characters": self._tts_characters,
                "audio_duration_s": round(self._tts_audio_duration, 2),
                "cancelled": self._tts_cancelled,
            }
        if self._eou_delay or self._transcription_delay or self._on_user_turn_delay:
            out["turn_taking"] = {
                "end_of_utterance_delay_ms_p50": _ms(_pct(self._eou_delay, 50)),
                "end_of_utterance_delay_ms_p95": _ms(_pct(self._eou_delay, 95)),
                "transcription_delay_ms_p50": _ms(_pct(self._transcription_delay, 50)),
                "transcription_delay_ms_p95": _ms(_pct(self._transcription_delay, 95)),
                "on_user_turn_completed_delay_ms_p50": _ms(_pct(self._on_user_turn_delay, 50)),
            }
        if self._vad_inference_count:
            out["vad"] = {
                "inference_count": self._vad_inference_count,
                "inference_duration_total_s": round(self._vad_inference_duration, 3),
                "idle_time_s": (
                    round(self._vad_idle_time, 2)
                    if isinstance(self._vad_idle_time, (int, float)) else None
                ),
            }
        if self._session_usage is not None:
            out["session_usage"] = self._session_usage
        return out


def _ms(seconds):
    """Framework latency fields are seconds; the report reads in ms."""
    if seconds is None:
        return None
    return round(seconds * 1000, 1)
