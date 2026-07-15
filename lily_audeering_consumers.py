"""
lily_audeering_consumers.py — devAIce module consumers for Lily
(WO-LILY-AUDEERING-001; native lift of mjrvs_audeering_consumers.py).

Owns:
  - Room-temperature AVD banding + rolling smoother (expression.dimension,
    room-level) -> [room read: <descriptor>] state-block lines
  - Reliability gate (audioQuality.snr) — runs FIRST, in front of every
    other consumer
  - Non-speech gate (aed music co-active with speech) -> suppress affect
  - Child-signal LADDER (speakerAttributes.gender.child) -> adult-mode VETO
    (Lily HAS the action surface JRVS lacked: the sticky-mode flag)
  - Scene consumer (sub-scene) -> low-priority [env: ...] line
  - Rubric loader + import-time zero-scalar lint
  - LilyAcousticState — latest-lines/snapshot store the agent reads from

D-cross invariants (carried verbatim from the JRVS donor):
  1. ZERO raw scalars in prompt-visible output — NL descriptors or nothing.
  2. Quality gate first; suppressed segments produce empty output.
  3. Baseline-relative reads where a baseline exists.
  4. Smoothing on DESCRIPTORS only. Safety triggers (the child ladder) run
     OUTSIDE the smoother with their own sustained-N streak counters — a
     single high-confidence child segment must never be averaged away.
  5. Never block a turn — state reads are synchronous, no awaits.
  6. Consumer exceptions never stop raw-signal recording.
  7. Neutral band -> inject NOTHING (no "room is neutral" lines).

SAFETY FRAMING (doc-verbatim, stamped at every emit site): the
speakerAttributes module estimates "how the speaker sounds, not necessarily
the actual attributes of the speaker"; age MAE is ±8.46yr. The child signal
can EXIT or BLOCK adult mode, NEVER authorize it — whole-room verbal
consensus remains necessary and is no longer sufficient. Age/gender are
otherwise telemetry-only, stamped PERCEIVED_NOT_VERIFIED, never spoken,
never in the prompt.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import lily_config

logger = logging.getLogger("lily_audeering.consumers")

# Doc-verbatim framing stamp — logged at EVERY speaker-attribute emit site.
PERCEIVED_FRAMING = (
    "framing=PERCEIVED_NOT_VERIFIED — the module estimates how the speaker "
    "sounds, not necessarily the actual attributes of the speaker (age MAE "
    "±8.46yr); signal can EXIT or BLOCK adult mode, NEVER authorize it; "
    "whole-room verbal consensus remains necessary and is no longer sufficient"
)


# ---------------------------------------------------------------------------
# Per-room state (smoother deques + safety-trigger streaks)
# ---------------------------------------------------------------------------

@dataclass
class LilyRoomBaseline:
    """Room-level smoother deques + child-ladder streak counters.

    NOT thread-safe on its own; LilyAcousticState serializes access.
    The AVD deques feed the smoothed descriptor (D-cross rule 4); the child
    streaks live OUTSIDE the smoother — widening AUDEERING_AVD_SMOOTH_WINDOW
    must never delay the ladder (JRVS regression, ported to Lily tests).
    """

    avd_arousal: deque = field(default_factory=lambda: deque(maxlen=16))
    avd_valence: deque = field(default_factory=lambda: deque(maxlen=16))
    avd_dominance: deque = field(default_factory=lambda: deque(maxlen=16))
    # Safety-trigger streak counters (OUTSIDE the smoother). The streak
    # counts VAD speech SEGMENTS — a young voice produces its own
    # segment-level scores.
    child_high_streak: int = 0
    child_borderline_streak: int = 0
    # Hard-question streak fed by the game (sagging-valence gimme rule).
    hard_question_streak: int = 0
    segments_seen: int = 0

    def observe_avd(
        self,
        arousal: float | None,
        valence: float | None,
        dominance: float | None,
    ) -> None:
        if arousal is not None:
            self.avd_arousal.append(arousal)
        if valence is not None:
            self.avd_valence.append(valence)
        if dominance is not None:
            self.avd_dominance.append(dominance)


def _maybe_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


# ---------------------------------------------------------------------------
# Reliability gate (INTERNAL, never injected) — runs FIRST
# ---------------------------------------------------------------------------

def quality_gate(
    parsed: dict[str, Any], *, scene_label: str | None = None
) -> tuple[bool, str]:
    """Return (keep, reason). keep=False suppresses this window's AFFECT
    derivations (never the child ladder — safety is not subordinate to
    audio quality).

    Doc caveat (encoded here so nobody 'fixes' it later): the devAIce SNR
    model is strong on BACKGROUND NOISE (CCC 0.94) and weak on
    SPEECH-DISTORTION (CCC 0.38). This is a background-noise gate ONLY —
    do NOT tighten the threshold chasing distortion artifacts; the model
    cannot see them.

    Transit scenes loosen the bar by AUDEERING_SNR_TRANSIT_ADJUST (JRVS
    D2b pattern: scene feeds the reliability gate). snr never touches the
    prompt.
    """
    aq = parsed.get("audioQuality")
    if not isinstance(aq, dict):
        return True, "no_audio_quality"
    snr = _maybe_float(aq.get("snr"))
    if snr is None:
        return True, "no_snr"
    threshold = lily_config.audeering_min_snr_db()
    if scene_label == "transport":
        threshold = threshold + lily_config.audeering_snr_transit_adjust()
    if snr < threshold:
        return False, f"snr_below_threshold_{snr:.1f}_lt_{threshold:.1f}"
    return True, "ok"


# ---------------------------------------------------------------------------
# Non-speech gate (INTERNAL) — party rooms: singing must not read as an
# emotional event. AED thresholds ride the doc defaults (server-side; the
# API only returns tags that cleared them — nothing to tune client-side).
# ---------------------------------------------------------------------------

_MUSIC_TAGS = frozenset({"music", "instrumental", "singing"})


def is_music_with_speech(parsed: dict[str, Any]) -> bool:
    """Music co-active with speech -> suppress affect reads."""
    aed = parsed.get("aed")
    if not isinstance(aed, list):
        return False
    return any(isinstance(t, str) and t.lower() in _MUSIC_TAGS for t in aed)


# ---------------------------------------------------------------------------
# Room-temperature AVD -> banded descriptor (smoothed; descriptors ONLY)
# ---------------------------------------------------------------------------

def _avd_smoothed_means(
    baseline: LilyRoomBaseline,
) -> tuple[float | None, float | None, float | None]:
    window = lily_config.audeering_avd_smooth_window()

    def _mean_last_n(dq: deque) -> float | None:
        if not dq:
            return None
        last_n = list(dq)[-window:]
        return sum(last_n) / len(last_n)

    return (
        _mean_last_n(baseline.avd_arousal),
        _mean_last_n(baseline.avd_valence),
        _mean_last_n(baseline.avd_dominance),
    )


def derive_room_read(baseline: LilyRoomBaseline) -> str | None:
    """Room-level AVD descriptor, banded and tuned to hosting.

    Neutral band (all axes) -> None: inject NOTHING (D-cross rule 7).
    Phrase list mirrors prompts/lily_room_read_rubric.txt exactly — the
    rubric maps each phrase to a host move (flat -> easier question +
    spotlight; hot -> tighten and ride it; sagging valence on a
    hard-question streak -> drop a gimme).
    """
    arousal, valence, dominance = _avd_smoothed_means(baseline)
    neutral = lily_config.audeering_avd_neutral_band()

    def _in_neutral(v: float | None) -> bool:
        return v is None or abs(v) < neutral

    if _in_neutral(arousal) and _in_neutral(valence) and _in_neutral(dominance):
        return None

    a = arousal if arousal is not None else 0.0
    v = valence if valence is not None else 0.0

    # Compound bands — most-specific first.
    if a > 0.4 and v < -0.3:
        return "agitated / on edge"
    if a > 0.3 and v > 0.2:
        return "hot / riding high"
    if a < -neutral:
        return "flat / low energy"
    if v < -0.3:
        # The rubric's gimme rule keys on this phrase when the game has
        # been running hard questions (hard_question_streak is advisory
        # color for logs; the phrase itself carries no scalar).
        return "valence sagging"
    return None


# ---------------------------------------------------------------------------
# Scene consumer — single classification per capture window (the client
# requests {"scene": {"outputSubScene": true}} and each >=5s window yields
# ONE label; there is no continuous sub-windowing).
# ---------------------------------------------------------------------------

_SCENE_DESCRIPTORS = {
    # Sub-scene first (outputSubScene: true).
    "indoor_small": "small indoor room",
    "small_indoor": "small indoor room",
    "indoor_medium": "medium indoor space",
    "medium_indoor": "medium indoor space",
    "indoor_large": "large indoor venue",
    "large_indoor": "large indoor venue",
    # Top-level fallbacks.
    "indoor": None,  # baseline — silent (don't narrate the default)
    "outdoor": "outdoors",
    "transport": "in transit",
    "vehicle": "in transit",
    "car": "in transit",
}


def derive_scene_descriptor(parsed: dict[str, Any]) -> str | None:
    scene = parsed.get("scene")
    if isinstance(scene, dict):
        sub = scene.get("subScene")
        if isinstance(sub, str) and sub.strip():
            desc = _SCENE_DESCRIPTORS.get(sub.strip().lower())
            if desc is not None:
                return desc
        label = scene.get("label")
        if isinstance(label, str):
            return _SCENE_DESCRIPTORS.get(label.strip().lower())
        return None
    if isinstance(scene, str):
        return _SCENE_DESCRIPTORS.get(scene.strip().lower())
    return None


def scene_top_label(parsed: dict[str, Any]) -> str | None:
    """Top-level scene label (feeds the transit SNR adjustment)."""
    scene = parsed.get("scene")
    if isinstance(scene, dict):
        label = scene.get("label")
        return label.strip().lower() if isinstance(label, str) else None
    if isinstance(scene, str):
        s = scene.strip().lower()
        return "transport" if s in ("vehicle", "car") else s
    return None


# ---------------------------------------------------------------------------
# Child-signal LADDER (SAFETY — runs OUTSIDE the smoother, BEFORE the gates)
# ---------------------------------------------------------------------------

def advance_child_ladder(
    speaker_segments: list[dict[str, Any]],
    baseline: LilyRoomBaseline,
) -> dict[str, Any] | None:
    """Feed one capture window's per-VAD-segment speakerAttributes results
    into the sustained-N ladder. Returns a trip event dict or None.

    Signal: speakerAttributes.gender.child (schema: {gender: {female, male,
    child} summing to 1, age: number|null}); results are PER VAD SPEECH
    SEGMENT — a young voice produces its own segment-level scores, and the
    sustained-N streak counts segments.

    Tiers (veto-only, BOTH trip the adult-mode veto):
      HIGH       >= AUDEERING_CHILD_HALT_THRESHOLD_HIGH        sustained N
      BORDERLINE >= AUDEERING_CHILD_HALT_THRESHOLD_BORDERLINE  sustained N

    Ladder-fix semantics (JRVS WO-JRVS-AUDEERING-CHILD-LADDER-FIX-001,
    lifted intact): any score >= borderline increments the child-present
    (borderline) streak — HIGH segments increment it too, so a signal
    oscillating around the high threshold cannot reset itself and evade
    both tiers. Below borderline resets both.

    NULL-SAFE: a null child score (too-short segment, small-model result)
    neither advances NOR resets a streak — the segment is skipped.

    OUTSIDE the smoother: streaks advance per segment regardless of
    AUDEERING_AVD_SMOOTH_WINDOW (regression-pinned in tests).
    """
    high_thresh = lily_config.audeering_child_halt_threshold_high()
    border_thresh = lily_config.audeering_child_halt_threshold_borderline()
    sustained_n = lily_config.audeering_child_halt_sustained_n()

    tripped: dict[str, Any] | None = None
    for seg in speaker_segments or []:
        if not isinstance(seg, dict):
            continue
        child_conf = _maybe_float(seg.get("child"))
        if child_conf is None:
            # Null score: neither advance nor reset (null-safety rule).
            continue
        if child_conf >= border_thresh:
            baseline.child_borderline_streak += 1
            if child_conf >= high_thresh:
                baseline.child_high_streak += 1
            else:
                baseline.child_high_streak = 0
        else:
            baseline.child_high_streak = 0
            baseline.child_borderline_streak = 0

        tier: str | None = None
        if baseline.child_high_streak >= sustained_n:
            tier = "high_halt"
        elif baseline.child_borderline_streak >= sustained_n:
            tier = "borderline_step_up"
        if tier is not None:
            tripped = {
                "kind": "child_signal",
                "tier": tier,
                "child_confidence": float(child_conf),
                "sustained_n": max(
                    baseline.child_high_streak,
                    baseline.child_borderline_streak,
                ),
            }
    return tripped


def child_veto_active(baseline: LilyRoomBaseline) -> bool:
    """True while EITHER tier's streak is sustained. Veto-only, both tiers:
    exits active adult mode and blocks lily_enter_adult_mode. The signal
    can EXIT or BLOCK adult mode, NEVER authorize it (doc framing above)."""
    sustained_n = lily_config.audeering_child_halt_sustained_n()
    if (
        lily_config.audeering_child_halt_enabled()
        and baseline.child_high_streak >= sustained_n
    ):
        return True
    if (
        lily_config.audeering_child_step_up_enabled()
        and baseline.child_borderline_streak >= sustained_n
    ):
        return True
    return False


def emit_child_signal_telemetry(event: dict[str, Any]) -> None:
    """Structured log for a ladder trip. Framing stamp doc-verbatim."""
    logger.warning(
        "LILY_AUDEERING_CHILD | tier=%s child_confidence=%.2f sustained_n=%d "
        "action=adult_mode_veto %s",
        event.get("tier"),
        event.get("child_confidence", 0.0),
        event.get("sustained_n", 0),
        PERCEIVED_FRAMING,
    )


def emit_speaker_attribute_telemetry(speaker_segments: list[dict[str, Any]]) -> None:
    """Age/gender are telemetry-only: stamped PERCEIVED_NOT_VERIFIED, never
    spoken, never in the prompt. Framing stamp doc-verbatim at this emit
    site too."""
    for seg in speaker_segments or []:
        if not isinstance(seg, dict):
            continue
        age = _maybe_float(seg.get("age"))
        female = _maybe_float(seg.get("female"))
        male = _maybe_float(seg.get("male"))
        if age is None and female is None and male is None:
            continue
        logger.info(
            "LILY_AUDEERING_SPEAKER | perceived_age=%s perceived_female=%s "
            "perceived_male=%s action=telemetry_only %s",
            f"{age:.1f}" if age is not None else "null",
            f"{female:.2f}" if female is not None else "null",
            f"{male:.2f}" if male is not None else "null",
            PERCEIVED_FRAMING,
        )


# ---------------------------------------------------------------------------
# Orchestrator — one call per parsed upload result
# ---------------------------------------------------------------------------

def derive_state_lines(
    parsed: dict[str, Any],
    baseline: LilyRoomBaseline,
) -> tuple[tuple[str, ...], dict[str, Any] | None]:
    """Run every consumer over one capture window's parsed result.

    Returns (state_lines, child_event). state_lines is () when nothing is
    meaningfully non-neutral or the gates suppressed the window. Never
    raises past its own boundary. NL output only — zero raw scalars.
    """
    if not isinstance(parsed, dict):
        return (), None

    baseline.segments_seen += 1
    scene_label = scene_top_label(parsed)

    # SAFETY FIRST (JRVS child-gate fix, lifted): the child ladder runs
    # BEFORE the quality and music gates. A young voice on noisy audio, or
    # a child singing (which the AED gate tags as music), must still be
    # evaluated — safety is NOT subordinate to audio quality.
    speaker_segments = parsed.get("speakerSegments") or []
    child_event = advance_child_ladder(speaker_segments, baseline)
    if child_event is not None:
        emit_child_signal_telemetry(child_event)
    emit_speaker_attribute_telemetry(speaker_segments)

    # Quality gate — FIRST for affect (D-cross rule 2).
    keep, reason = quality_gate(parsed, scene_label=scene_label)
    if not keep:
        logger.debug("LILY_AUDEERING_QUALITY_GATE | suppressed reason=%s", reason)
        return (), child_event

    # AED gate: music co-active with speech -> no affect read.
    if is_music_with_speech(parsed):
        logger.debug("LILY_AUDEERING_AED_GATE | suppressed reason=music_segment")
        return (), child_event

    # Update the room-level AVD smoother, then band. `category` scores are
    # deliberately NOT consumed (WO Task 3) — dimension only.
    dimension = parsed.get("dimension") or {}
    if isinstance(dimension, dict):
        baseline.observe_avd(
            arousal=_maybe_float(dimension.get("arousal")),
            valence=_maybe_float(dimension.get("valence")),
            dominance=_maybe_float(dimension.get("dominance")),
        )

    lines: list[str] = []
    room_read = derive_room_read(baseline)
    if room_read:
        lines.append(f"[room read: {room_read}]")

    # Low-priority env line — the state store keeps only the LATEST scene,
    # so at most one [env: ...] line appears per state-block refresh.
    scene_desc = derive_scene_descriptor(parsed)
    if scene_desc:
        lines.append(f"[env: {scene_desc}]")

    return tuple(lines), child_event


# ---------------------------------------------------------------------------
# LilyAcousticState — the store the agent reads (synchronous, never blocks)
# ---------------------------------------------------------------------------

class LilyAcousticState:
    """Thread-safe latest-signal store.

    The upload task writes via record_response(); the agent reads
    state_block_lines() inside build_state_block (synchronous — D-cross
    rule: never block a turn) and latest_snapshot()/addressee snapshots at
    persistence time. breaker_open mirrors the pipeline's circuit breaker
    so the addressee snapshot can be an EXPLICIT null when the pipeline is
    down (Task 6 contract)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.baseline = LilyRoomBaseline()
        self._room_line: str | None = None
        self._env_line: str | None = None
        self._latest_snapshot: dict[str, Any] | None = None
        self._breaker_open: bool = False
        # Optional veto callback, wired by the entrypoint to the game's
        # adult-mode exit path. Called OUTSIDE the lock.
        self.on_child_signal: Optional[Callable[[dict], None]] = None
        # Optional breaker-open callback (WO-LILY-DESYNC-HONESTY-001
        # Sub-agent A), wired by the entrypoint to the game's child-gate
        # loss path: sensor down while adult mode is active -> exit adult
        # mode, fail CLOSED. Called OUTSIDE the lock, on the CLOSED->OPEN
        # transition only.
        self.on_breaker_open: Optional[Callable[[str], None]] = None

    # -- pipeline side ------------------------------------------------------

    def set_breaker_open(self, is_open: bool, reason: str = "unspecified") -> None:
        with self._lock:
            was_open = self._breaker_open
            self._breaker_open = bool(is_open)
        if is_open and not was_open and self.on_breaker_open is not None:
            try:
                self.on_breaker_open(reason)
            except Exception as exc:  # noqa: BLE001 — hook never breaks the pipeline
                logger.error(
                    "LILY_AUDEERING_BREAKER | on_breaker_open hook failed "
                    "exc_type=%s exc=%s", type(exc).__name__, str(exc)[:200],
                )

    @property
    def breaker_open(self) -> bool:
        return self._breaker_open

    def record_response(self, parsed: dict[str, Any]) -> None:
        """Consume one parsed upload result. Consumer exceptions NEVER stop
        the raw signal from being recorded (D-cross rule 6)."""
        if not isinstance(parsed, dict):
            return
        # Raw-signal snapshot first — recorded even if the consumers below
        # blow up on shape drift.
        with self._lock:
            self._latest_snapshot = _snapshot_from_parsed(parsed)
        child_event: dict[str, Any] | None = None
        try:
            with self._lock:
                lines, child_event = derive_state_lines(parsed, self.baseline)
                room = next((l for l in lines if l.startswith("[room read:")), None)
                env = next((l for l in lines if l.startswith("[env:")), None)
                # Room read: replace on every window (stale reads decay by
                # replacement, neutral clears the line). Env: keep latest
                # non-null classification.
                self._room_line = room
                if env is not None:
                    self._env_line = env
        except Exception as exc:  # noqa: BLE001 — consumer guard boundary
            logger.warning(
                "LILY_AUDEERING_CONSUMERS | derive failed exc_type=%s exc=%s",
                type(exc).__name__, str(exc)[:200],
            )
        if child_event is not None and self.on_child_signal is not None:
            try:
                self.on_child_signal(child_event)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "LILY_AUDEERING_VETO | callback failed exc_type=%s exc=%s",
                    type(exc).__name__, str(exc)[:200],
                )

    # -- agent side ----------------------------------------------------------

    def state_block_lines(self) -> tuple[str, ...]:
        """Lines for the [GAME STATE] block. Empty when neutral/suppressed.
        [env: ...] appears at most once per refresh."""
        with self._lock:
            return tuple(l for l in (self._room_line, self._env_line) if l)

    def child_veto_active(self) -> bool:
        with self._lock:
            return child_veto_active(self.baseline)

    def latest_snapshot(self) -> dict[str, Any] | None:
        with self._lock:
            return dict(self._latest_snapshot) if self._latest_snapshot else None

    def addressee_snapshot(self) -> dict[str, Any] | None:
        """Snapshot value for lily_addressee_log.acoustic_snapshot.

        Non-null when the pipeline is healthy AND a capture has landed;
        EXPLICIT None (the caller always sets the key, so the column is an
        explicit SQL null, never absent) when the breaker is open or no
        signal exists yet."""
        with self._lock:
            if self._breaker_open or self._latest_snapshot is None:
                return None
            return dict(self._latest_snapshot)


def _snapshot_from_parsed(parsed: dict[str, Any]) -> dict[str, Any]:
    """Persistence snapshot (drt_acoustic_trajectories clone shape):
    category / dimension / prosody / features jsonb. Speaker attributes are
    stamped PERCEIVED_NOT_VERIFIED in the payload itself so no downstream
    reader can mistake them for verified identity."""
    snap: dict[str, Any] = {
        "category": parsed.get("category") or {},
        "dimension": parsed.get("dimension") or {},
        "prosody": parsed.get("prosody") or {},
        # `features` module is NOT requested by Lily's upload config; the
        # column is kept for schema parity with drt_acoustic_trajectories.
        "features": parsed.get("features") or {},
        "audio_quality": parsed.get("audioQuality") or {},
        "aed": parsed.get("aed") or [],
        "scene": parsed.get("scene"),
        "captured_at": time.time(),
    }
    segs = parsed.get("speakerSegments")
    if segs:
        snap["speaker_attributes"] = {
            "framing": "PERCEIVED_NOT_VERIFIED",
            "segments": segs,
        }
    return snap


_STATE = LilyAcousticState()


def lily_get_acoustic_state() -> LilyAcousticState:
    return _STATE


def lily_reset_acoustic_state() -> LilyAcousticState:
    """Test/reconnect helper: fresh module-level state."""
    global _STATE
    _STATE = LilyAcousticState()
    return _STATE


# ---------------------------------------------------------------------------
# Rubric loader + import-time zero-scalar lint
# ---------------------------------------------------------------------------
# The rubric lives in prompts/lily_room_read_rubric.txt (separate file,
# appended to lily_system.txt at loader level in lily_agent.py). The static
# prompt carries ZERO scalars — no thresholds, no dB, no Hz, not a single
# digit. Enforced here at import time (JRVS D3 lint, tightened per WO to
# "any digit"): a scalar in the rubric fails the container at boot.

_RUBRIC_PATH = Path(__file__).parent / "prompts" / "lily_room_read_rubric.txt"
LILY_AUDEERING_RUBRIC = _RUBRIC_PATH.read_text(encoding="utf-8")


def lily_audeering_rubric_block() -> str:
    """Loader-level injection point — lily_agent appends this to the
    lily_system.txt prompt text."""
    return LILY_AUDEERING_RUBRIC


def _lint_rubric_free_of_scalars() -> None:
    """Fail-fast at import if the rubric contains ANY digit (stricter than
    the JRVS donor's decimal/dB/Hz/multi-digit patterns, per WO)."""
    match = re.search(r"\d", LILY_AUDEERING_RUBRIC)
    if match:
        raise RuntimeError(
            "LILY_AUDEERING rubric scalar-lint FAILED: found digit "
            f"{match.group(0)!r} in prompts/lily_room_read_rubric.txt — the "
            "rubric must carry zero scalars (WO-LILY-AUDEERING-001 / JRVS "
            "D-cross rule: phrase list only)."
        )
    for pattern, label in (
        (re.compile(r"\bdB\b", re.IGNORECASE), "decibels"),
        (re.compile(r"\bHz\b", re.IGNORECASE), "hertz"),
    ):
        m = pattern.search(LILY_AUDEERING_RUBRIC)
        if m:
            raise RuntimeError(
                f"LILY_AUDEERING rubric scalar-lint FAILED: found {label} "
                f"({m.group(0)!r}) — rubric must carry zero scalars."
            )


_lint_rubric_free_of_scalars()
