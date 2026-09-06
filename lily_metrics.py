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

WO-LILY-LLM-USAGE-ALL-PATHS-001 — one lily_llm_usage row per LLM call on
EVERY path. The collector is the single scheduling seam for the durable
per-call receipt:

  * `wire_llm(llm, purpose=...)` subscribes one LLM COMPONENT's
    `metrics_collected` and pins that component's identity (purpose, model,
    effort read from its constructor opts) onto every event it emits — so
    a swapped vocal LLM (adult_vocal) attributes its own rows and an
    in-flight call on the old component still lands under the old one.
  * `record_llm_call(...)` schedules `lily_persistence.lily_record_llm_call`
    fire-and-forget and COUNTS failures into `llm_usage_write_failures`
    (bounded) so the session report carries the receipt lane's own health.
  * The module-level `record_llm_call(...)` routes to the collector bound
    with `set_current_collector` — the seam the off-path transports
    (reasoning / judge / assessment / vision / grounding / arsenal_gen)
    call without holding any session object. One collector per process
    (LiveKit runs one job per process); a call with no bound collector is
    a no-op that returns False.
"""

import asyncio
import logging
from typing import Optional

logger = logging.getLogger("lily_metrics")

# Purpose vocabulary for lily_llm_usage.purpose (the operator's contract:
# "a row per LLM call with purpose, model, effort, ttft_ms, total_ms").
LLM_PURPOSES = (
    "vocal",            # framework vocal LLM (AgentSession lane)
    "adult_vocal",      # a swapped-in adult vocal LLM wired via wire_llm
    "reasoning",        # question authoring / verification / distractors
    "adult_reasoning",  # bare Grok JSON transport default (adult config)
    "judge",            # Tier-2 adjudication
    "assessment",       # session report assessment
    "vision",           # Grok vision (describe / content gate)
    "grounding",        # Gemini google_search / url_context grounding
    "arsenal_gen",      # standing picture-arsenal author + image gate
)

_USAGE_FAILURE_CAP = 10_000


def _resolve(value):
    """A usage-context field is a value or a zero-arg callable (the session
    id / phase / client are read lazily at record time)."""
    return value() if callable(value) else value


def _llm_identity(llm) -> tuple:
    """(model, effort) off an LLM component's constructor opts — the ACTUAL
    call arguments, not a config accessor re-read later. Effort is None
    unless the plugin was built with a string reasoning_effort (the
    framework's NotGiven sentinel is not a string)."""
    opts = getattr(llm, "_opts", None)
    model = getattr(opts, "model", None) or getattr(llm, "model", None)
    effort = getattr(opts, "reasoning_effort", None)
    if not isinstance(effort, str):
        effort = None
    return (model if isinstance(model, str) else None), effort


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


def _num_or_none(value):
    """L2 unit rule: the framework's 0–1 decimals persist as numbers,
    exactly as emitted — never strings, never percentages."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


class _FrameworkDebugShield(logging.Filter):
    """Keeps the framework's DEBUG flood (created so the taps can count
    it) out of a root handler: drops `<prefix>.*` records below INFO —
    output unchanged, only counting changes. Shared by
    enable_preemptive_used_capture and attach_eot_tap (L2), which
    installs one only where a handler has none."""

    def __init__(self, prefix: str):
        super().__init__()
        self.prefix = prefix

    def filter(self, record):
        return not (
            record.name.startswith(self.prefix)
            and record.levelno < logging.INFO
        )


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
        self._llm_cancelled = 0
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
        # WO-LILY-LLM-USAGE-PERSISTENCE: durable per-call usage sink and the
        # empty-STOP finish states llm_node's guard stashes by speech_id.
        # collect_llm_call consumes the state so ONE write per call carries
        # the finish verdict. None sink -> persistence off (tests, no db).
        self._usage_sink = None
        self._finish_states = {}  # speech_id -> (finish_reason, empty_stop)
        # WO-LILY-LLM-USAGE-ALL-PATHS-001: the durable-receipt lane's own
        # accounting. Context = {supabase, session_id, phase} (values or
        # zero-arg callables) bound once from the entrypoint; rows scheduled
        # and write failures are counted so the session report can say
        # when the receipt itself failed (S2).
        self._usage_context = None
        self._usage_rows_scheduled = 0
        self._usage_write_failures = 0
        # WO-LILY-COMPOSITION-FOLLOWUP-001 L2: the per-USER-turn end-of-turn
        # receipt. Each user turn's MetricsReport is classified
        # (commit_reason) and correlated to the framework's own DEBUG
        # records ("eot prediction" / "user turn committed", tapped off the
        # livekit.agents logger) by last_speaking_time ≈ stopped_speaking_at.
        # Bounded ring (oldest dropped); persisted whole under
        # session_metrics.turn_taking.turns.
        self._eot_turns = []
        self._eot_records = {}          # round(last_speaking_time, 3) -> bundle
        self._eot_last_prediction = None
        self._eot_tap_attached = False
        self._eot_tap_level = None
        self._eot_prediction_timeouts = 0
        self._eot_cloud_failures = 0
        self._commit_reasons = {}
        self._turn_detector_source = None
        self._endpointing_bounds = None  # zero-arg -> (min_delay, max_delay)

    def set_usage_sink(self, sink) -> None:
        """Install a per-call usage sink: a callable taking one fields dict
        (utterance_id, ttft_ms, total_ms, prompt_tokens, completion_tokens,
        finish_reason, empty_stop, purpose, model, effort). When a sink is
        set it REPLACES the collector's own scheduling (record_llm_call) for
        the framework-LLM path — a test seam and an override hook. None
        (the default) means collect_llm_call records through the bound
        usage context directly."""
        self._usage_sink = sink

    # -- WO-LILY-LLM-USAGE-ALL-PATHS-001: durable per-call receipt lane ----

    def bind_usage_context(self, *, supabase, session_id, phase=None) -> None:
        """Bind the session context every durable row needs. Each field is a
        value or a zero-arg callable resolved at record time (the supabase
        client and ui_phase live on the game object and can change)."""
        self._usage_context = {
            "supabase": supabase, "session_id": session_id, "phase": phase,
        }

    @property
    def llm_usage_write_failures(self) -> int:
        """Rows whose durable write failed this session (bounded)."""
        return self._usage_write_failures

    @property
    def llm_usage_rows_scheduled(self) -> int:
        return self._usage_rows_scheduled

    def _note_usage_write_failure(self, reason) -> None:
        if self._usage_write_failures < _USAGE_FAILURE_CAP:
            self._usage_write_failures += 1
        # The writer already logged the WARNING with the error detail; this
        # is the counter's own trace so a session log can be grepped for
        # the running total.
        logger.debug(
            "LILY_METRICS | USAGE_WRITE_FAILED | failures=%d reason=%s",
            self._usage_write_failures, reason,
        )

    def _on_usage_write_done(self, task) -> None:
        try:
            if task.cancelled():
                self._note_usage_write_failure("cancelled")
                return
            exc = task.exception()
            if exc is not None:
                self._note_usage_write_failure(type(exc).__name__)
                return
            if task.result() is not True:
                self._note_usage_write_failure("insert_failed")
        except Exception as e:  # never let a callback raise into the loop
            logger.debug("LILY_METRICS | USAGE_DONE_CB | %s", e)

    def record_llm_call(
        self,
        *,
        purpose: str,
        model: Optional[str],
        effort: Optional[str],
        ttft_ms: Optional[float],
        total_ms: Optional[float],
        prompt_tokens: Optional[int] = None,
        completion_tokens: Optional[int] = None,
        finish_reason: Optional[str] = None,
        utterance_id: Optional[str] = None,
        empty_stop: Optional[bool] = None,
        session_id: Optional[str] = None,
        phase: Optional[str] = None,
    ) -> bool:
        """Schedule ONE durable lily_llm_usage row, fire-and-forget. Never
        raises; returns True iff a write was scheduled. `session_id` /
        `phase` default to the bound context (the report sweep assessing
        ANOTHER session passes its own). Failures — including "no event
        loop" and a client that is None at record time — are counted."""
        ctx = self._usage_context
        if ctx is None:
            return False
        try:
            import lily_persistence  # lazy: keeps this module import-light

            sb = _resolve(ctx.get("supabase"))
            if sb is None:
                return False
            sid = session_id or _resolve(ctx.get("session_id"))
            ph = phase if phase is not None else _resolve(ctx.get("phase"))
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._note_usage_write_failure("no_running_loop")
            return False
        except Exception as e:
            self._note_usage_write_failure(type(e).__name__)
            logger.warning("LILY_METRICS | USAGE_SCHEDULE_FAILED | %s", e)
            return False
        try:
            task = loop.create_task(lily_persistence.lily_record_llm_call(
                sb,
                session_id=sid,
                purpose=purpose,
                model=model,
                effort=effort,
                ttft_ms=ttft_ms,
                total_ms=total_ms,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                finish_reason=finish_reason,
                phase=ph,
                utterance_id=utterance_id,
                empty_stop=empty_stop,
            ))
            task.add_done_callback(self._on_usage_write_done)
            self._usage_rows_scheduled += 1
            return True
        except Exception as e:
            self._note_usage_write_failure(type(e).__name__)
            logger.warning("LILY_METRICS | USAGE_SCHEDULE_FAILED | %s", e)
            return False

    def wire_llm(self, llm, *, purpose: str = "vocal",
                 model: Optional[str] = None,
                 effort: Optional[str] = None) -> dict:
        """Subscribe one LLM COMPONENT's `metrics_collected` and pin its
        identity (purpose + model + effort from ITS constructor opts) onto
        every event it emits. THE seam for the vocal lane and any swapped
        component (adult_vocal): call it on the new LLM at the swap site
        and its turns write their own rows. Returns the identity dict."""
        opt_model, opt_effort = _llm_identity(llm)
        ident = {
            "purpose": purpose,
            "model": model or opt_model,
            "effort": effort or opt_effort,
        }
        # collect_llm_call_soon, not collect_llm_call: the framework's
        # sibling subscriber stamps speech_id onto the event in place, and
        # emitter subscriber order is a coin flip — the deferred fold
        # always sees the stamp (wave-1 review, HIGH finding).
        llm.on(
            "metrics_collected",
            lambda m, _ident=ident: self.collect_llm_call_soon(m, _ident),
        )
        logger.info(
            "LILY_METRICS | LLM_WIRED | purpose=%s model=%s effort=%s",
            ident["purpose"], ident["model"], ident["effort"],
        )
        return ident

    def note_finish_state(self, speech_id, finish_reason, empty_stop) -> None:
        """Stash a call's finish verdict for the next collect_llm_call fold
        of the same speech_id — llm_node's empty-STOP guard is the only
        caller today (empty_stop=True, finish_reason='stop_empty'). Bounded
        ring; an unconsumed state evicts oldest. Defensive: never raises."""
        if not speech_id:
            return
        try:
            states = self._finish_states
            states[speech_id] = (finish_reason, bool(empty_stop))
            while len(states) > 200:
                states.pop(next(iter(states)))
        except Exception as e:
            logger.warning("LILY_METRICS | FINISH_STATE_SKIPPED | %s", e)

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
            self._fold_user_turn(g)
        except Exception as e:
            logger.warning("LILY_METRICS | TURN_SKIPPED | %s", e)

    # -- L2: the per-turn end-of-turn receipt --------------------------------

    _EOT_TURNS_CAP = 200
    _EOT_RECORDS_CAP = 64
    _EOT_CORRELATION_TOLERANCE_S = 0.25

    def bind_turn_detector(self, source) -> None:
        """A zero-arg callable returning the session's live turn_detection
        (a string mode or a model object); its name is stamped on every
        turn entry (it flips on a cloud→local fallback)."""
        self._turn_detector_source = source

    def bind_endpointing_bounds(self, source) -> None:
        """A zero-arg callable returning (min_delay, max_delay) seconds —
        the commit_reason classifier's bounds. Defaults to lily_config's
        stt_min/max_endpointing_delay when unbound."""
        self._endpointing_bounds = source

    def _turn_detector_name(self):
        src = self._turn_detector_source
        if not callable(src):
            return None
        try:
            td = src()
        except Exception:
            return None
        if td is None:
            return None
        if isinstance(td, str):
            return td
        for attr in ("model_name", "model", "name"):
            v = getattr(td, attr, None)
            if isinstance(v, str) and v:
                return v
        return type(td).__name__

    def _endpointing_bounds_now(self):
        src = self._endpointing_bounds
        if callable(src):
            try:
                lo, hi = src()
                return float(lo), float(hi)
            except Exception:
                pass
        try:
            import lily_config
            return (
                float(lily_config.stt_min_endpointing_delay()),
                float(lily_config.stt_max_endpointing_delay()),
            )
        except Exception:
            return None, None

    def classify_commit_reason(self, end_of_turn_delay, transcription_delay):
        """WHY the framework committed the turn when it did (operator L2):
        max_delay (|eot − max| < 0.05 s), stt_final (|eot − transcription_
        delay| < 0.3 s: the commit rode the STT final), min_delay (|eot −
        min| < 0.05 s), else other. The order is the operator's."""
        if not isinstance(end_of_turn_delay, (int, float)):
            return "other"
        eot = float(end_of_turn_delay)
        lo, hi = self._endpointing_bounds_now()
        if hi is not None and abs(eot - hi) < 0.05:
            return "max_delay"
        if isinstance(transcription_delay, (int, float)) and abs(
            eot - float(transcription_delay)
        ) < 0.3:
            return "stt_final"
        if lo is not None and abs(eot - lo) < 0.05:
            return "min_delay"
        return "other"

    def _fold_user_turn(self, g) -> None:
        """Build one turn entry from a USER-turn MetricsReport (it carries
        stopped_speaking_at); agent-turn reports fold nothing here. EVERY
        entry carries eot_probability / eot_threshold / eot_model /
        eot_source (operator rule) — null + a source name when no debug
        record was captured for the turn."""
        stopped = g("stopped_speaking_at")
        eot = g("end_of_turn_delay")
        if not isinstance(stopped, (int, float)) or not isinstance(
            eot, (int, float)
        ):
            return
        td = g("transcription_delay")
        outc = g("on_user_turn_completed_delay")
        started = g("started_speaking_at")
        reason = self.classify_commit_reason(eot, td)
        entry = {
            "started_speaking_at": (
                float(started) if isinstance(started, (int, float)) else None
            ),
            "stopped_speaking_at": float(stopped),
            "vad_end_of_speech_at": float(stopped),
            "stt_final_at": (
                float(stopped) + float(td) if isinstance(td, (int, float)) else None
            ),
            "commit_at": float(stopped) + float(eot),
            "transcription_delay_ms": _ms(td) if isinstance(td, (int, float)) else None,
            "end_of_turn_delay_ms": _ms(eot),
            "on_user_turn_completed_delay_ms": (
                _ms(outc) if isinstance(outc, (int, float)) else None
            ),
            "commit_reason": reason,
            # The framework's own numbers — 0–1 decimals exactly as emitted
            # (probability=0.00569…, unlikely_threshold=0.56), never
            # percentages, never strings.
            "eot_probability": None,
            "eot_threshold": None,
            "eot_model": self._turn_detector_name(),
            "eot_source": (
                "no_debug_record" if self._eot_tap_attached else "tap_not_attached"
            ),
            "endpointing_delay": None,
            "commit_trigger": None,
            "from_cache": None,
        }
        bundle = self._pop_eot_record(float(stopped))
        if bundle is not None:
            pred = bundle.get("prediction") or {}
            commit = bundle.get("commit") or {}
            prob = pred.get("probability")
            if prob is None:
                prob = commit.get("end_of_turn_probability")
            thr = pred.get("unlikely_threshold")
            if thr is None:
                thr = commit.get("unlikely_threshold")
            entry["eot_probability"] = _num_or_none(prob)
            entry["eot_threshold"] = _num_or_none(thr)
            entry["endpointing_delay"] = _num_or_none(pred.get("endpointing_delay"))
            entry["commit_trigger"] = commit.get("source") or pred.get("trigger")
            entry["from_cache"] = pred.get("from_cache")
            if bundle.get("model"):
                entry["eot_model"] = bundle["model"]
            if pred.get("timed_out"):
                entry["eot_source"] = "prediction_timed_out"
            elif pred:
                entry["eot_source"] = "debug_record"
            else:
                entry["eot_source"] = "commit_record_only"
        self._commit_reasons[reason] = self._commit_reasons.get(reason, 0) + 1
        self._eot_turns.append(entry)
        while len(self._eot_turns) > self._EOT_TURNS_CAP:
            self._eot_turns.pop(0)

    def _pop_eot_record(self, stopped_speaking_at):
        best_key, best_gap = None, None
        for key, bundle in self._eot_records.items():
            lst = bundle.get("last_speaking_time")
            if not isinstance(lst, (int, float)):
                continue
            gap = abs(float(lst) - float(stopped_speaking_at))
            if gap <= self._EOT_CORRELATION_TOLERANCE_S and (
                best_gap is None or gap < best_gap
            ):
                best_key, best_gap = key, gap
        if best_key is None:
            return None
        return self._eot_records.pop(best_key)

    def _note_eot_prediction(self, extras: dict) -> None:
        self._eot_last_prediction = {
            "probability": extras.get("probability"),
            "unlikely_threshold": extras.get("unlikely_threshold"),
            "endpointing_delay": extras.get("endpointing_delay"),
            "trigger": extras.get("trigger"),
            "from_cache": extras.get("from_cache"),
            "language": extras.get("language"),
        }

    def _note_user_turn_committed(self, extras: dict) -> None:
        lst = extras.get("last_speaking_time")
        bundle = {
            "last_speaking_time": lst,
            "commit": {
                "last_speaking_time": lst,
                "last_final_transcript_time": extras.get("last_final_transcript_time"),
                "speech_start_time": extras.get("speech_start_time"),
                "delay_completed": extras.get("delay_completed"),
                "source": extras.get("source"),
                "end_of_turn_probability": extras.get("end_of_turn_probability"),
                "unlikely_threshold": extras.get("unlikely_threshold"),
            },
            "prediction": self._eot_last_prediction,
            "model": self._turn_detector_name(),
        }
        self._eot_last_prediction = None
        key = (
            round(float(lst), 3) if isinstance(lst, (int, float))
            else f"unkeyed_{len(self._eot_records)}"
        )
        self._eot_records[key] = bundle
        while len(self._eot_records) > self._EOT_RECORDS_CAP:
            self._eot_records.pop(next(iter(self._eot_records)))

    def attach_eot_tap(self, logger_name: str = "livekit.agents"):
        """L2: tap the framework's end-of-turn DEBUG records off the EXACT
        logger audio_recognition logs through — a logging.Filter on
        logging.getLogger(logger_name), the attach_preemptive_tap pattern
        (logger-level filters see records logged on that logger; no
        private API, nothing monkeypatched). Captures "eot prediction"
        (probability, unlikely_threshold, endpointing_delay, trigger,
        from_cache), "user turn committed" (last_speaking_time,
        delay_completed, source, end_of_turn_probability,
        unlikely_threshold), and the warnings "eot prediction timed out" /
        "cloud turn detector failed".

        INDEPENDENT of enable_preemptive_used_capture (operator addendum
        #2): this sets the logger to DEBUG itself (only ever LOWERS it) and
        installs its own root-handler shield for livekit.* records below
        INFO when none is present — the receipt populates even if the C12
        capture is disabled. The effective level at attach is asserted
        (isEnabledFor(DEBUG)), logged as LILY_METRICS | EOT_TAP and
        persisted as eot_tap_level. Returns the filter."""
        lk = logging.getLogger(logger_name)
        if lk.level == logging.NOTSET or lk.level > logging.DEBUG:
            lk.setLevel(logging.DEBUG)
        prefix = logger_name.split(".")[0]
        for handler in logging.getLogger().handlers:
            if not any(
                isinstance(f, _FrameworkDebugShield) and f.prefix == prefix
                for f in handler.filters
            ):
                handler.addFilter(_FrameworkDebugShield(prefix))
        collector = self

        class _EotFilter(logging.Filter):
            def filter(self, record):
                try:
                    msg = record.getMessage()
                    extras = record.__dict__
                    if msg.startswith("eot prediction timed out"):
                        collector._eot_prediction_timeouts += 1
                        collector._eot_last_prediction = {"timed_out": True}
                    elif msg.startswith("eot prediction"):
                        collector._note_eot_prediction(extras)
                    elif msg.startswith("user turn committed"):
                        collector._note_user_turn_committed(extras)
                    elif "cloud turn detector failed" in msg:
                        collector._eot_cloud_failures += 1
                except Exception:
                    pass
                return True

        f = _EotFilter()
        lk.addFilter(f)
        enabled = lk.isEnabledFor(logging.DEBUG)
        self._eot_tap_attached = True
        self._eot_tap_level = logging.getLevelName(lk.getEffectiveLevel())
        logger.info(
            "LILY_METRICS | EOT_TAP | logger=%s level=%s enabled_for_debug=%s "
            "— per-turn end-of-turn receipt %s (COMPOSITION-FOLLOWUP-001 L2)",
            logger_name, self._eot_tap_level, enabled,
            "armed" if enabled else "ATTACHED BUT DEBUG DISABLED",
        )
        return f

    def eot_turns(self) -> list:
        """The bounded per-turn list (a copy) — the L2 receipt."""
        return [dict(e) for e in self._eot_turns]

    def collect_llm_call_soon(self, m, identity=None) -> None:
        """Fold one per-call LLMMetrics, DEFERRED one event-loop tick
        (wave-1 review finding, HIGH): the framework's own subscriber on
        the same emitter stamps speech_id onto the event IN PLACE
        (agent_activity._on_metrics_collected), and rtc.EventEmitter keeps
        subscribers in a SET — whether our handler runs before or after
        the stamp is an address-hash coin flip (~50% of sessions would
        silently lose all per-turn grouping). call_soon runs after the
        whole emit pass, so the stamp always lands first. Falls back to an
        immediate fold when no loop is running (tests, teardown)."""
        try:
            asyncio.get_running_loop().call_soon(
                self.collect_llm_call, m, identity
            )
        except RuntimeError:
            self.collect_llm_call(m, identity)

    def collect_llm_call(self, m, identity=None) -> None:
        """Fold one per-call LLMMetrics from the LLM COMPONENT's
        `metrics_collected` event (HOTFIX-007 Y1c). The component-level
        event is first-class at 1.6.8 — the deprecation the U3(b) audit
        flagged is only on AgentSession.on("metrics_collected"), which we
        still avoid. Emits one INFO line per call so a live session's log
        answers "is the prompt prefix cache-hitting?" without a redeploy.

        `identity` is the {purpose, model, effort} dict wire_llm pinned on
        the emitting component; None (legacy subscription) records as a
        plain "vocal" call with unknown model/effort."""
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
            # An all-empty payload is not a call (review finding: garbage
            # objects were folding as zero-token calls, inflating the
            # denominator).
            if not (prompt or cached or completion or g("request_id")):
                return
            self._llm_calls += 1
            if g("cancelled"):
                # Speculative/interrupted work thrown away after reaching
                # a first token — the interrupt-path waste the preemptive
                # `invalidated` counter structurally cannot see.
                self._llm_cancelled += 1
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
            # WO-LILY-LLM-USAGE-PERSISTENCE: one durable row per call. The
            # finish verdict (empty-STOP) rides the state stashed by
            # llm_node's guard for this speech_id; default is a plain finish.
            # Inside this try, so a sink raise is fail-open like the fold.
            # Nothing here runs before the first token — the framework
            # emits metrics_collected AFTER the call completes.
            if self._usage_sink is not None or self._usage_context is not None:
                finish_reason, empty_stop = self._finish_states.pop(
                    speech, (None, False)
                )
                duration = g("duration")
                ident = identity or {}
                fields = {
                    "utterance_id": speech,
                    "ttft_ms": (
                        _ms(ttft) if isinstance(ttft, (int, float)) else None
                    ),
                    "total_ms": (
                        _ms(duration)
                        if isinstance(duration, (int, float)) else None
                    ),
                    "prompt_tokens": prompt,
                    "completion_tokens": completion,
                    "finish_reason": finish_reason,
                    "empty_stop": empty_stop,
                    "purpose": ident.get("purpose") or "vocal",
                    "model": ident.get("model"),
                    "effort": ident.get("effort"),
                }
                if self._usage_sink is not None:
                    self._usage_sink(fields)
                else:
                    self.record_llm_call(**fields)
        except Exception as e:
            logger.warning("LILY_METRICS | LLM_CALL_SKIPPED | %s", e)

    def attach_preemptive_tap(self, logger_name: str = "livekit.agents"):
        """Count the framework's preemptive-generation outcomes off its own
        log records (HOTFIX-007 Y2 measurement gate). A logging.Filter on
        the EXACT logger agent_activity logs through — no framework private
        API touched, nothing monkeypatched, and a Filter cannot flood or
        reformat anything (it only observes records already being logged).

        Reliability contract, stated honestly (wave-1 review): "preemptive
        generation invalidated" is a WARNING and is always counted — but it
        covers ONLY the equivalence-mismatch path at turn commit, which is
        exactly the number the Y2 settle-vs-split decision needs. The
        framework's ~8 other _cancel_preemptive_generation sites
        (interruptions, pauses, agent switch) discard silently; that
        interrupt-path waste shows up instead as cancelled_calls in the
        llm_cache block. "using preemptive generation" is DEBUG and only
        counts when the deploy log level allows debug records (at the
        production INFO level it reads 0). Returns the filter so a
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

    def enable_preemptive_used_capture(
        self, logger_name: str = "livekit.agents"
    ):
        """Make preemptive SURVIVAL measurable at a production INFO deploy
        (WO-LILY-HOSTLOOP-001 C12: "measured preemptive survival >0"). The
        framework announces a USED speculation only at DEBUG — at INFO the
        record is never created, so the tap's `used` counter read 0 by
        construction, not by measurement. This sets the framework logger to
        DEBUG so the records EXIST for the tap to count, and shields every
        root handler from the resulting debug flood (a filter dropping
        livekit.* records below INFO — output is unchanged; only counting
        changes). Returns the shield filter for detach/testing. Call AFTER
        attach_preemptive_tap; handlers attached later are not shielded
        (documented limit)."""
        lk = logging.getLogger(logger_name)
        if lk.level == logging.NOTSET or lk.level > logging.DEBUG:
            lk.setLevel(logging.DEBUG)

        prefix = logger_name.split(".")[0]
        shield = _FrameworkDebugShield(prefix)
        for handler in logging.getLogger().handlers:
            handler.addFilter(shield)
        return shield

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
        turn_taking = {}
        if self._transcription_delay or self._end_of_turn_delay or self._on_user_turn_delay:
            turn_taking.update({
                "transcription_delay_ms_p50": _ms(_pct(self._transcription_delay, 50)),
                "transcription_delay_ms_p95": _ms(_pct(self._transcription_delay, 95)),
                "end_of_turn_delay_ms_p50": _ms(_pct(self._end_of_turn_delay, 50)),
                "end_of_turn_delay_ms_p95": _ms(_pct(self._end_of_turn_delay, 95)),
                "on_user_turn_completed_delay_ms_p50": _ms(_pct(self._on_user_turn_delay, 50)),
            })
        if self._eot_turns or self._eot_tap_attached:
            # L2: the per-turn receipt + the instrument's own proof
            # (eot_tap_attached / eot_tap_level) so the FIRST receipt
            # call shows whether the tap could see the records.
            turn_taking.update({
                "turns": self.eot_turns(),
                "commit_reasons": dict(self._commit_reasons),
                "eot_tap_attached": bool(self._eot_tap_attached),
                "eot_tap_level": self._eot_tap_level,
                "eot_prediction_timeouts": self._eot_prediction_timeouts,
                "cloud_turn_detector_failures": self._eot_cloud_failures,
                "turn_detector": self._turn_detector_name(),
            })
        if turn_taking:
            out["turn_taking"] = turn_taking
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
                "cancelled_calls": self._llm_cancelled,
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
        # WO-LILY-LLM-USAGE-ALL-PATHS-001: the receipt lane's own health is
        # a row whenever the lane is live (context bound) or anything was
        # attempted — a session with 0 failures says so explicitly.
        if (
            self._usage_context is not None
            or self._usage_rows_scheduled
            or self._usage_write_failures
        ):
            out["llm_usage"] = {
                "rows_scheduled": self._usage_rows_scheduled,
                "llm_usage_write_failures": self._usage_write_failures,
            }
        return out


# ---------------------------------------------------------------------------
# Module-level seam for the off-path transports (WO-LILY-LLM-USAGE-ALL-
# PATHS-001). lily_reasoning / lily_vision / lily_search / lily_assessment /
# lily_arsenal_gen hold no session object; they call record_llm_call here
# and the entrypoint binds the session's collector once. One collector per
# process — LiveKit runs one job per process, and the standalone seeding
# job (lily_arsenal_seed) binds nothing, so its calls are no-ops.
# ---------------------------------------------------------------------------

_current_collector: Optional[LilyMetricsCollector] = None


def set_current_collector(collector: Optional[LilyMetricsCollector]) -> None:
    """Bind (or with None, unbind) the process's usage collector."""
    global _current_collector
    _current_collector = collector


def current_collector() -> Optional[LilyMetricsCollector]:
    return _current_collector


def record_llm_call(**fields) -> bool:
    """Route one off-path LLM call receipt to the bound collector (see
    LilyMetricsCollector.record_llm_call for the fields). Never raises;
    False when no collector is bound or the write was not scheduled."""
    c = _current_collector
    if c is None:
        return False
    try:
        return c.record_llm_call(**fields)
    except Exception as e:
        logger.warning("LILY_METRICS | RECORD_LLM_CALL_FAILED | %s", e)
        return False
