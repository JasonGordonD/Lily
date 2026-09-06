"""
lily_acts.py — the shared string vocabulary of the Lily host loop.

DEFINE-ONLY module (REFACTOR Stage 1a, item 4): no imports, no logic beyond
`claim_key`. It names the strings that today ride as literals through
gated_say acts, say-registry claim keys, the LWW participant-attribute set,
lily_sessions.metadata and the pacing preference, so a later stage can
retarget call sites one module at a time without inventing new spellings.
Only NON-hot modules (lily_game_control, lily_supply) read from it in this
stage; the hot files keep their literals until the fix wave lands.

Every value here is the EXACT spelling already on the wire / in the spine
logs / in the DB — changing one is a protocol change, not a rename.
"""

from __future__ import annotations

from typing import Final

# ---------------------------------------------------------------------------
# Speech acts — the second positional argument of LilyGame.gated_say and the
# act tag the say gate / playout gates reason about.
# ---------------------------------------------------------------------------

# Game lane (a STOP freezes these; none may air without a live game).
ACT_QUESTION_DELIVERY: Final[str] = "question_delivery"
ACT_QUESTION_NUDGE: Final[str] = "question_nudge"
ACT_QUESTION_REOFFER: Final[str] = "question_reoffer"
ACT_VERDICT: Final[str] = "verdict"
ACT_VERDICT_HOLD: Final[str] = "verdict_hold"
ACT_REVEAL: Final[str] = "reveal"
ACT_REVEAL_FLOURISH: Final[str] = "reveal_flourish"
ACT_REVEAL_SCORES: Final[str] = "reveal_scores"
ACT_REVEAL_FINALE: Final[str] = "reveal_finale"
ACT_STEAL_WINDOW: Final[str] = "steal_window"
ACT_ANSWER_RECEIPT: Final[str] = "answer_receipt"
ACT_LATE_ANSWER: Final[str] = "late_answer"
ACT_SKIP: Final[str] = "skip"
ACT_GAME_START: Final[str] = "game_start"
ACT_START_SETTLE_HOLD: Final[str] = "start_settle_hold"
ACT_CUT_RECOVERY: Final[str] = "cut_recovery"
ACT_SUPPLY_EXHAUSTED: Final[str] = "supply_exhausted"
ACT_CLARIFY_QUESTION: Final[str] = "clarify_question"

# Control-plane / obligation acks.
ACT_STOP_ACK: Final[str] = "stop_ack"
ACT_HOLD_ACK: Final[str] = "hold_ack"
ACT_FLOOR: Final[str] = "floor"
ACT_RESTART_CONFIRM: Final[str] = "restart_confirm"
ACT_RESTART_DECLINED: Final[str] = "restart_declined"
ACT_RESTART_ACK: Final[str] = "restart_ack"
# WO-LILY-CONTROL-GATES-001 (W2) obligation lines, named here per
# WO-LILY-COMPOSITION-FOLLOWUP-001 C7 (define only — the hot files keep
# their literals until the retarget wave).
ACT_RESTART_CONFIRM_DROPPED: Final[str] = "restart_confirm_dropped"
ACT_DISPUTE_TIMEOUT_ACK: Final[str] = "dispute_timeout_ack"
ACT_START_SETTLE_OVERRIDE: Final[str] = "start_settle_override"
# COMPOSITION-FOLLOWUP-001 B4: the choices-on-demand re-ask.
ACT_QUESTION_REASK: Final[str] = "question_reask"
# WO-LILY-OPERATOR-MODS-001 (B6 / B8): the operator acknowledgment and the
# silence-budget reply lane (the holding line itself rides ACT_FLOOR).
ACT_OPERATOR_ACK: Final[str] = "operator_ack"
ACT_SILENCE_BUDGET_REPLY: Final[str] = "silence_budget_reply"

# Identity / greeting lane.
ACT_GREET: Final[str] = "greet"
ACT_REJOIN: Final[str] = "rejoin"
ACT_LATE_RECOGNITION: Final[str] = "late_recognition"

# Preferences lane.
ACT_PACING_SET: Final[str] = "pacing_set"
ACT_PACING_KEPT: Final[str] = "pacing_kept"
ACT_PACING_CONFIRM: Final[str] = "pacing_confirm"
ACT_PACE_ACK: Final[str] = "pace_ack"
ACT_MEDIA_MODE: Final[str] = "media_mode"
ACT_MEDIA_MODE_UNAVAILABLE: Final[str] = "media_mode_unavailable"

# Forget lane.
ACT_FORGET_CONFIRM: Final[str] = "forget_confirm"
ACT_FORGET_DECLINED: Final[str] = "forget_declined"
ACT_FORGET_DONE: Final[str] = "forget_done"
ACT_FORGET_ALREADY_DONE: Final[str] = "forget_already_done"

# Typed-machine acts (lily_game_control.may) that are not gated_say acts.
ACT_ADJUDICATE: Final[str] = "adjudicate"
ACT_BEGIN_ROUND: Final[str] = "begin_round"

# Live game-lane payloads — mirror of LilyGame._GAME_LANE_ACTS /
# lily_game_control.GAME_LANE_ACTS (that module composes its set from this).
GAME_LANE_ACTS: Final[frozenset[str]] = frozenset({
    ACT_QUESTION_DELIVERY, ACT_QUESTION_NUDGE, ACT_VERDICT, ACT_REVEAL,
    ACT_REVEAL_FLOURISH, ACT_REVEAL_SCORES, ACT_REVEAL_FINALE,
    ACT_STEAL_WINDOW, ACT_ANSWER_RECEIPT,
})

# ---------------------------------------------------------------------------
# Say-registry claim keys — `q_{N}_{suffix}` (SpeechActRegistry.claim).
# ---------------------------------------------------------------------------

CLAIM_DELIVERY: Final[str] = "delivery"
CLAIM_VERDICT: Final[str] = "verdict"
CLAIM_REVEAL: Final[str] = "reveal"
CLAIM_CLARIFY: Final[str] = "clarify"
CLAIM_LATE_ANSWER: Final[str] = "late_answer"
CLAIM_TRANSITION: Final[str] = "transition"


def claim_key(qnum, act: str) -> str:
    """The say-registry key for one question's act: ``q_{qnum}_{act}``.
    `qnum` is rendered with str() exactly as the historical f-strings did
    (an int normally; None pre-game renders as ``q_None_...``)."""
    return f"q_{qnum}_{act}"


# ---------------------------------------------------------------------------
# lily_sessions.metadata lanes (lily_agent.lily_session_metadata) — the
# wave's receipt keys, named per COMPOSITION-FOLLOWUP-001 C7 (define only).
# ---------------------------------------------------------------------------

META_AIRGATE_EVENTS: Final[str] = "airgate_events"
META_CONFIG_SNAPSHOT: Final[str] = "config_snapshot"

# ---------------------------------------------------------------------------
# LWW participant attributes (LilyGlassMixin.publish_attributes) — exact
# key spellings per the seam contract; values are strings on the wire.
# ---------------------------------------------------------------------------

ATTR_PHASE: Final[str] = "phase"
ATTR_ROUND: Final[str] = "round"
ATTR_QUESTION_NUMBER: Final[str] = "question_number"
ATTR_MODE: Final[str] = "mode"
ATTR_PACING: Final[str] = "pacing"
ATTR_MEDIA_MODE: Final[str] = "media_mode"
ATTR_PLAYERS: Final[str] = "players"
ATTR_ROSTER_GEN: Final[str] = "roster_gen"
ATTR_ANSWER_WINDOW: Final[str] = "answer_window"
ATTR_NEXT_QUESTION_READY: Final[str] = "next_question_ready"
ATTR_LAST_ACTIVE_AT: Final[str] = "last_active_at"

# Keys inside the JSON-encoded `answer_window` attribute value.
ANSWER_WINDOW_OPEN: Final[str] = "open"
ANSWER_WINDOW_DURATION_MS: Final[str] = "duration_ms"
ANSWER_WINDOW_OPENED_AT: Final[str] = "opened_at"
ANSWER_WINDOW_STEAL: Final[str] = "steal"

# ---------------------------------------------------------------------------
# lily_sessions.metadata keys (written at session end / close).
# ---------------------------------------------------------------------------

META_PIPELINE_LATENCY: Final[str] = "pipeline_latency"
META_SESSION_METRICS: Final[str] = "session_metrics"
META_QUESTION_TIMELINE: Final[str] = "question_timeline"
META_IDENTITY_PROMOTIONS: Final[str] = "identity_promotions"
META_GAME_RESTARTS: Final[str] = "game_restarts"
META_VOICE_IDENTITY: Final[str] = "voice_identity"

# ---------------------------------------------------------------------------
# Pacing preference — the two legal LilyScorekeeper.pacing values.
# ---------------------------------------------------------------------------

PACING_TIMED: Final[str] = "timed"
PACING_RELAXED: Final[str] = "relaxed"
PACING_VALUES: Final[tuple[str, ...]] = (PACING_TIMED, PACING_RELAXED)
