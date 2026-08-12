"""GameControl — one typed state machine for the LilyGame operability spine.

REFACTOR WAVE 1a. LilyGame carries ~a dozen independently-true latches
(_hold_active, _delivery_stop_sticky, _question_pending, _adjudicating,
_question_transitioning, _open_transition_qnum, _pending_delivery_qnum,
_active_delivery_qnum, _recognition_dispute, _late_recognition_pending,
_ambiguous_yes_blocks_start, _setup_pending). They are correct in the ORDER
the races were discovered (lily-5E3036, lily-1C53C6, lily-938EFF,
lily-16A9AE) — not correct by construction. Overlapping flags let a hold
block the question sheet but not a prose reveal, because each dispatcher
re-implemented a slightly different subset of the gate.

This module collapses that into ONE object with ONE gate: `may(act)`.
Every dispatcher asks the same question — may() returns a reason to refuse
or None to proceed — so the spine is promoted from a log line
(LilyGame.spine_fields) to the control plane.

`__post_init__` refuses to STORE an illegal combination: the machine cannot
represent "adjudicating nothing" or "stopped but not in the STOPPED phase".
Where the old latch forest can still produce such a combo, `from_latches`
raises IllegalControlState and the shadow-parity layer records it — that is
the point of the wave, not a bug.

Pure module: no lily_agent import, no I/O. Fully unit-testable without
constructing LilyAgent.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum


class Phase(Enum):
    LOBBY = "lobby"
    ARMING = "arming"
    DELIVERING = "delivering"
    ANSWERING = "answering"
    ADJUDICATING = "adjudicating"
    TRANSITION = "transition"   # reveal -> verdict -> next
    HELD = "held"
    STOPPED = "stopped"
    FINAL = "final"


class Floor(Enum):
    HOST = "host"
    ROOM = "room"
    CLARIFY = "clarify"


# Delivery is a small string enum kept as str for log/spine continuity.
DELIVERY_STATES = frozenset({"none", "pending", "active", "confirmed"})

# Live game-lane payloads (mirror of LilyGame._GAME_LANE_ACTS). None of these
# may air without a live game; a STOP freezes them all. Kept here so may() and
# the legacy game_payload_blocked share ONE taxonomy.
GAME_LANE_ACTS = frozenset({
    "question_delivery", "question_nudge", "verdict", "reveal",
    "reveal_flourish", "reveal_scores", "reveal_finale", "steal_window",
    "answer_receipt",
})

# The adjudication commit is not a gated_say act — it is the reveal/verdict
# transition's own entry gate (LilyGame.adjudicate).
ADJUDICATE_ACT = "adjudicate"

# The kickoff act. Its refusal ladder is LilyGame.start_blocked_reason() — the
# single choke already on main (game_stopped / recognition_dispute /
# ambiguous_yes / identity_unconfirmed / user_speaking / setup_pending). may()
# owns only the one reason it can express authoritatively from the typed state
# (game_stopped); the rest are DELEGATED to start_blocked_reason and will be
# subsumed when the lily_begin_round site is wired (later wave). may() must not
# duplicate that ladder here — it would drift.
BEGIN_ROUND_ACT = "begin_round"

# Acts that open/continue the reveal->verdict->next transition. Reserved for
# the later waves that rewire dispatch_armed_question / tts_node claim /
# lily_begin_round; may() already answers them so those call sites need no new
# vocabulary when they are wired.
TRANSITION_ACTS = frozenset({
    "reveal", "reveal_flourish", "reveal_scores", "reveal_finale", "verdict",
})


class IllegalControlState(ValueError):
    """A GameControl combination the typed machine refuses to store."""


@dataclass
class GameControl:
    """The single owner of "where is the round right now".

    Fields:
      phase        — the primary activity (one of Phase).
      floor        — whose turn it is to hold the room (Floor).
      qnum         — current question number, or None pre-game.
      delivery     — none | pending | active | confirmed (the stem's progress
                     toward playout). "none" means nothing is armed.
      transition_q — the question a reveal->verdict->next transition is open
                     on, else None. Non-None only in Phase.TRANSITION.
      hold_reason  — why the floor is yielded, or None. INDEPENDENT of phase:
                     a hold can be live while adjudication/delivery runs, so a
                     dispatch is refused for `hold` even when phase reports the
                     more-specific activity. adjudicate deliberately ignores it
                     (a window-timeout ruling runs regardless of a hold).
      stop_sticky  — the persistent STOP latch. True iff phase is STOPPED.
    """

    phase: Phase
    floor: Floor
    qnum: int | None
    delivery: str
    transition_q: int | None
    hold_reason: str | None
    stop_sticky: bool

    def __post_init__(self) -> None:
        if not isinstance(self.phase, Phase):
            raise IllegalControlState(f"phase must be Phase, got {self.phase!r}")
        if not isinstance(self.floor, Floor):
            raise IllegalControlState(f"floor must be Floor, got {self.floor!r}")
        if self.delivery not in DELIVERY_STATES:
            raise IllegalControlState(
                f"delivery must be one of {sorted(DELIVERY_STATES)}, "
                f"got {self.delivery!r}"
            )
        # Phase.STOPPED means the STOP latch is set. The reverse is NOT an
        # invariant: a STOP that lands pre-game (lobby) or post-game (final)
        # leaves stop_sticky set while the phase reports lobby/final, and the
        # legacy gate still reports it as `game_stopped` there — so stop_sticky
        # is an independent fact, checked first, not folded into the phase.
        if self.phase is Phase.STOPPED and not self.stop_sticky:
            raise IllegalControlState("phase=stopped but stop_sticky is False")
        # Adjudication commits a reveal; there is nothing to adjudicate with
        # no armed question (the LilyGame.adjudicate `armed_question is None`
        # guard, promoted to an invariant).
        if self.phase is Phase.ADJUDICATING and self.delivery == "none":
            raise IllegalControlState("phase=adjudicating but delivery=none")
        # A transition question number belongs only to an open transition.
        if self.transition_q is not None and self.phase is not Phase.TRANSITION:
            raise IllegalControlState(
                f"transition_q set but phase={self.phase.value}"
            )

    # -- the one gate every dispatcher asks -------------------------------

    def may(self, act: str) -> str | None:
        """Return a reason to refuse `act`, or None to proceed.

        This reproduces, from the typed state alone, the refusal decisions
        the latch forest makes today at the two call sites wired this wave:

          * LilyGame.gated_say — the code-triggered speech funnel. Its
            state-based gates are the hold yield (PATCH-002 A4) and the
            game-lane live-game gate (PATCH-003 P8: game_payload_blocked).
            Reasons: "hold", "game_stopped", "no_live_game".
          * LilyGame.adjudicate — the reveal/verdict commit. Its guard is
            stop / already-adjudicating / transitioning / no-armed. adjudicate
            does NOT respect a hold (a timed-out window still rules).

        Source-level exemptions (stop_primitive / hold_ack / hold_release),
        the conversational question-pending yield, the composite-flight lane,
        and per-key dup suppression are dispatch-CONTEXT, not game STATE, and
        stay at the call site — folded into later waves, not modeled here.
        """
        if act == ADJUDICATE_ACT:
            if self.stop_sticky:
                return "game_stopped"
            if self.phase is Phase.ADJUDICATING:
                return "already_adjudicating"
            if self.phase is Phase.TRANSITION:
                return "transitioning"
            if self.delivery == "none":
                return "no_armed_question"
            return None

        if act == BEGIN_ROUND_ACT:
            # DELEGATED, not duplicated. GameControl authoritatively owns the
            # STOP reason (the command-path spine — a stopped game cannot
            # kick off); every other kickoff gate lives in
            # LilyGame.start_blocked_reason() and is folded in when that site
            # is wired.
            if self.stop_sticky:
                return "game_stopped"
            return None

        # Every other act is a gated_say dispatch. Hold is checked FIRST —
        # the same order gated_say uses (hold gate precedes the game-payload
        # gate), so a game delivery that lands during a hold is refused for
        # `hold`, not `game_stopped`.
        if self.hold_reason is not None:
            return "hold"

        if act in GAME_LANE_ACTS:
            if self.stop_sticky:
                return "game_stopped"
            if self.phase in (Phase.LOBBY, Phase.FINAL):
                return "no_live_game"
        return None

    # -- helpers ----------------------------------------------------------

    def is_live(self) -> bool:
        """A round is in play (not lobby, not finished)."""
        return self.phase not in (Phase.LOBBY, Phase.FINAL)

    def with_(self, **changes) -> "GameControl":
        """A validated copy with fields replaced (re-runs __post_init__)."""
        return replace(self, **changes)


def _derive_delivery(
    *,
    active_delivery_qnum: int | None,
    pending_delivery_qnum: int | None,
    armed_question: object | None,
    delivery_confirmed: bool,
) -> str:
    if delivery_confirmed:
        return "confirmed"
    if active_delivery_qnum is not None:
        return "active"
    if pending_delivery_qnum is not None or armed_question is not None:
        return "pending"
    return "none"


def _derive_floor(
    *,
    recognition_dispute: bool,
    question_pending: bool,
    answer_window_open: bool,
) -> Floor:
    if recognition_dispute:
        return Floor.CLARIFY
    if question_pending or answer_window_open:
        return Floor.ROOM
    return Floor.HOST


def from_latches(
    *,
    game_started: bool,
    game_over: bool,
    delivery_stop_sticky: bool,
    adjudicating: bool,
    question_transitioning: bool,
    hold_active: bool,
    hold_reason: str | None,
    answer_window_open: bool,
    active_delivery_qnum: int | None,
    pending_delivery_qnum: int | None,
    open_transition_qnum: int | None,
    armed_question: object | None,
    question_number: int | None,
    recognition_dispute: bool = False,
    question_pending: bool = False,
    delivery_confirmed: bool = False,
    game_start_committed: bool = False,
) -> GameControl:
    """Project a GameControl from the current latch values (control derived
    FROM the latches, this wave's direction — keeps behavior byte-identical
    while parity is proven; the later wave flips authority and deletes the
    latches).

    The phase PRIORITY is the one load-bearing decision here and is fixed
    highest-to-lowest: FINAL > LOBBY > STOPPED > ADJUDICATING > TRANSITION >
    HELD > ANSWERING > DELIVERING > ARMING. STOPPED/ADJUDICATING/TRANSITION
    outrank HELD so the adjudicate guard reads the right phase; the hold is
    NOT lost because hold_reason is an independent field the dispatch gate
    still consults.

    Raises IllegalControlState if the latch combination cannot be stored
    (e.g. adjudicating with nothing armed). Callers running in production
    catch it and record a parity divergence.
    """
    delivery = _derive_delivery(
        active_delivery_qnum=active_delivery_qnum,
        pending_delivery_qnum=pending_delivery_qnum,
        armed_question=armed_question,
        delivery_confirmed=delivery_confirmed,
    )
    floor = _derive_floor(
        recognition_dispute=recognition_dispute,
        question_pending=question_pending,
        answer_window_open=answer_window_open,
    )

    # Phase priority. `game_start_committed` (Class 7a) is the round-committed
    # latch — recognition speech is forbidden once it is set. It is folded in
    # here so a committed round never derives as LOBBY even for an instant
    # between the commit and game_started; the recognition-act gate
    # (LilyGame.late_recognition_blocked_reason, its own single choke) reads
    # the same committed state and is subsumed when that site is wired.
    if game_over:
        phase = Phase.FINAL
    elif not game_started and not game_start_committed:
        phase = Phase.LOBBY
    elif delivery_stop_sticky:
        phase = Phase.STOPPED
    elif adjudicating:
        phase = Phase.ADJUDICATING
    elif question_transitioning:
        phase = Phase.TRANSITION
    elif hold_active:
        phase = Phase.HELD
    elif answer_window_open:
        phase = Phase.ANSWERING
    elif active_delivery_qnum is not None:
        phase = Phase.DELIVERING
    else:
        phase = Phase.ARMING

    # stop_sticky and hold are INDEPENDENT of the phase (a hold or a STOP can
    # be live pre-game, mid-adjudication, etc.), exactly as the legacy gates
    # read _delivery_stop_sticky / _hold_active directly. hold_reason is set
    # whenever the hold is active — with the real reason, or a sentinel so the
    # dispatch gate still refuses for `hold` when _hold_active carries no
    # reason string.
    stop_sticky = bool(delivery_stop_sticky)
    hr = (hold_reason or "held") if hold_active else None
    # transition_q belongs only to an open transition.
    transition_q = open_transition_qnum if phase is Phase.TRANSITION else None

    return GameControl(
        phase=phase,
        floor=floor,
        qnum=question_number,
        delivery=delivery,
        transition_q=transition_q,
        hold_reason=hr,
        stop_sticky=stop_sticky,
    )
