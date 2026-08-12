"""GameControl typed machine + may(act).

REFACTOR WAVE 1a. These tests construct GameControl directly — no LilyAgent,
no LilyGame, no event loop. They pin (1) may()'s refusal decisions for every
wired act, (2) the illegal combinations the machine refuses to store, and
(3) the from_latches projection + phase priority.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lily_game_control import (  # noqa: E402
    Floor,
    GameControl,
    IllegalControlState,
    Phase,
    from_latches,
)

CONV_ACT = "conversational"
GAME_ACT = "question_delivery"


def live(**over) -> GameControl:
    """A valid mid-round control, ARMING with a card pending."""
    base = dict(
        phase=Phase.ARMING,
        floor=Floor.HOST,
        qnum=3,
        delivery="pending",
        transition_q=None,
        hold_reason=None,
        stop_sticky=False,
    )
    base.update(over)
    return GameControl(**base)


# -- may(): dispatch (gated_say) acts ------------------------------------

def test_may_clear_arming_proceeds():
    gc = live()
    assert gc.may(CONV_ACT) is None
    assert gc.may(GAME_ACT) is None


def test_may_hold_refuses_every_dispatch():
    gc = live(phase=Phase.HELD, hold_reason="player_wait")
    assert gc.may(CONV_ACT) == "hold"
    assert gc.may(GAME_ACT) == "hold"


def test_may_hold_beats_game_lane_reason():
    # Hold is checked first, exactly like gated_say: a game delivery landing
    # during a hold is refused for `hold`, not the game-lane reason.
    gc = live(phase=Phase.ADJUDICATING, delivery="active", hold_reason="wait")
    assert gc.may(GAME_ACT) == "hold"


def test_may_stopped_blocks_game_lane_only():
    gc = live(phase=Phase.STOPPED, stop_sticky=True, delivery="none")
    assert gc.may(GAME_ACT) == "game_stopped"
    # A non-game conversational act is not blocked by the STOP alone.
    assert gc.may(CONV_ACT) is None


def test_may_lobby_blocks_game_lane_no_live_game():
    gc = GameControl(
        phase=Phase.LOBBY, floor=Floor.HOST, qnum=0, delivery="none",
        transition_q=None, hold_reason=None, stop_sticky=False,
    )
    assert gc.may(GAME_ACT) == "no_live_game"
    assert gc.may(CONV_ACT) is None


def test_may_final_blocks_game_lane_no_live_game():
    gc = GameControl(
        phase=Phase.FINAL, floor=Floor.HOST, qnum=9, delivery="none",
        transition_q=None, hold_reason=None, stop_sticky=False,
    )
    assert gc.may(GAME_ACT) == "no_live_game"
    assert gc.may(CONV_ACT) is None


def test_may_all_game_lane_acts_blocked_in_lobby():
    from lily_game_control import GAME_LANE_ACTS
    gc = GameControl(
        phase=Phase.LOBBY, floor=Floor.HOST, qnum=0, delivery="none",
        transition_q=None, hold_reason=None, stop_sticky=False,
    )
    for act in GAME_LANE_ACTS:
        assert gc.may(act) == "no_live_game", act


# -- may(): the adjudicate act -------------------------------------------

def test_may_adjudicate_proceeds_when_armed():
    assert live(delivery="pending").may("adjudicate") is None
    assert live(phase=Phase.ANSWERING, delivery="active").may("adjudicate") is None


def test_may_adjudicate_refused_no_armed():
    gc = live(phase=Phase.ARMING, delivery="none", qnum=3)
    assert gc.may("adjudicate") == "no_armed_question"


def test_may_adjudicate_refused_when_stopped():
    gc = live(phase=Phase.STOPPED, stop_sticky=True, delivery="none")
    assert gc.may("adjudicate") == "game_stopped"


def test_may_adjudicate_refused_when_already_adjudicating():
    gc = live(phase=Phase.ADJUDICATING, delivery="active")
    assert gc.may("adjudicate") == "already_adjudicating"


def test_may_adjudicate_refused_when_transitioning():
    gc = live(phase=Phase.TRANSITION, delivery="active", transition_q=3)
    assert gc.may("adjudicate") == "transitioning"


def test_may_begin_round_owns_only_stop_delegates_rest():
    # may() authoritatively owns the STOP reason for begin_round (command-path
    # spine); it does NOT reimplement start_blocked_reason's other gates.
    assert live(phase=Phase.STOPPED, stop_sticky=True,
                delivery="none").may("begin_round") == "game_stopped"
    # A live/lobby state with no stop passes may() — the remaining kickoff
    # gates are delegated to LilyGame.start_blocked_reason, not duplicated here.
    assert live().may("begin_round") is None
    lobby = GameControl(
        phase=Phase.LOBBY, floor=Floor.HOST, qnum=0, delivery="none",
        transition_q=None, hold_reason=None, stop_sticky=False,
    )
    assert lobby.may("begin_round") is None
    # begin_round is NOT subject to the generic hold gate (start_blocked_reason
    # does not check the hold) — only stop.
    assert live(phase=Phase.HELD, hold_reason="wait").may("begin_round") is None


def test_may_adjudicate_ignores_hold():
    # A window-timeout ruling runs regardless of a hold (adjudicate never
    # checked _hold_active).
    gc = live(phase=Phase.ANSWERING, delivery="active", hold_reason="wait")
    assert gc.may("adjudicate") is None


# -- illegal combinations the machine refuses to store -------------------

def test_stop_sticky_without_stopped_phase_is_legal():
    # A STOP that lands pre-game / post-game leaves stop_sticky set while the
    # phase reports lobby/final; stop_sticky is independent of the phase.
    gc = live(stop_sticky=True, phase=Phase.ARMING)
    assert gc.stop_sticky is True
    assert gc.may(GAME_ACT) == "game_stopped"


def test_illegal_stopped_phase_without_stop_sticky():
    with pytest.raises(IllegalControlState):
        GameControl(
            phase=Phase.STOPPED, floor=Floor.HOST, qnum=3, delivery="none",
            transition_q=None, hold_reason=None, stop_sticky=False,
        )


def test_illegal_adjudicating_nothing():
    with pytest.raises(IllegalControlState):
        live(phase=Phase.ADJUDICATING, delivery="none")


def test_lobby_with_pregame_hold_is_legal():
    # Pre-game holds are real (a decline/wait before START); the dispatch gate
    # must still refuse for `hold`.
    gc = GameControl(
        phase=Phase.LOBBY, floor=Floor.HOST, qnum=0, delivery="none",
        transition_q=None, hold_reason="wait", stop_sticky=False,
    )
    assert gc.may(CONV_ACT) == "hold"


def test_illegal_transition_q_outside_transition():
    with pytest.raises(IllegalControlState):
        live(phase=Phase.ARMING, transition_q=3)


def test_illegal_bad_delivery_value():
    with pytest.raises(IllegalControlState):
        live(delivery="armed")


def test_illegal_bad_phase_type():
    with pytest.raises(IllegalControlState):
        live(phase="arming")


# -- from_latches projection + phase priority ----------------------------

def _latch_defaults(**over):
    base = dict(
        game_started=True,
        game_over=False,
        delivery_stop_sticky=False,
        adjudicating=False,
        question_transitioning=False,
        hold_active=False,
        hold_reason=None,
        answer_window_open=False,
        active_delivery_qnum=None,
        pending_delivery_qnum=None,
        open_transition_qnum=None,
        armed_question=None,
        question_number=3,
    )
    base.update(over)
    return base


def test_from_latches_lobby():
    gc = from_latches(**_latch_defaults(game_started=False, question_number=0))
    assert gc.phase is Phase.LOBBY
    assert gc.delivery == "none"


def test_from_latches_final_phase_but_independent_facts_survive():
    # game_over wins the PHASE, but stop_sticky / hold are independent facts
    # the legacy gates still read directly — they are not folded into phase.
    gc = from_latches(**_latch_defaults(
        game_over=True, delivery_stop_sticky=True,
    ))
    assert gc.phase is Phase.FINAL
    assert gc.stop_sticky is True
    # A game-lane act post-game with the STOP latch reports game_stopped, the
    # same reason the legacy game_payload gate gives (stop precedes no_live_game).
    assert gc.may(GAME_ACT) == "game_stopped"
    # Hold, when also live, is reported first (matches gated_say order).
    held = from_latches(**_latch_defaults(
        game_over=True, delivery_stop_sticky=True,
        hold_active=True, hold_reason="wait",
    ))
    assert held.hold_reason == "wait"
    assert held.may(GAME_ACT) == "hold"


def test_from_latches_stop_priority_over_adjudicating():
    gc = from_latches(**_latch_defaults(
        delivery_stop_sticky=True, adjudicating=True,
    ))
    assert gc.phase is Phase.STOPPED
    assert gc.stop_sticky is True


def test_from_latches_adjudicating_over_hold_but_hold_reason_survives():
    gc = from_latches(**_latch_defaults(
        adjudicating=True, hold_active=True, hold_reason="wait",
        active_delivery_qnum=3,
    ))
    assert gc.phase is Phase.ADJUDICATING
    # Hold not lost: the dispatch gate still refuses for it...
    assert gc.hold_reason == "wait"
    assert gc.may("conversational") == "hold"
    # ...while the adjudicate act reads the phase and refuses correctly.
    assert gc.may("adjudicate") == "already_adjudicating"


def test_from_latches_transition_over_hold():
    gc = from_latches(**_latch_defaults(
        question_transitioning=True, hold_active=True, hold_reason="wait",
        open_transition_qnum=3, active_delivery_qnum=3,
    ))
    assert gc.phase is Phase.TRANSITION
    assert gc.transition_q == 3


def test_from_latches_held():
    gc = from_latches(**_latch_defaults(hold_active=True, hold_reason="decline"))
    assert gc.phase is Phase.HELD
    assert gc.hold_reason == "decline"


def test_from_latches_answering():
    gc = from_latches(**_latch_defaults(answer_window_open=True))
    assert gc.phase is Phase.ANSWERING
    assert gc.floor is Floor.ROOM


def test_from_latches_delivering():
    gc = from_latches(**_latch_defaults(active_delivery_qnum=3))
    assert gc.phase is Phase.DELIVERING
    assert gc.delivery == "active"


def test_from_latches_arming_with_armed_card():
    gc = from_latches(**_latch_defaults(armed_question={"id": "q3"}))
    assert gc.phase is Phase.ARMING
    assert gc.delivery == "pending"
    assert gc.may("adjudicate") is None


def test_from_latches_confirmed_delivery():
    gc = from_latches(**_latch_defaults(
        active_delivery_qnum=3, delivery_confirmed=True,
    ))
    assert gc.delivery == "confirmed"


def test_from_latches_clarify_floor():
    gc = from_latches(**_latch_defaults(recognition_dispute=True))
    assert gc.floor is Floor.CLARIFY


def test_from_latches_committed_round_never_derives_lobby():
    # Class 7a: a committed round is never LOBBY, even if game_started has not
    # flipped yet (they are set atomically in practice, so this only hardens
    # the derivation). Recognition-act gating reads the same committed state.
    gc = from_latches(**_latch_defaults(
        game_started=False, game_start_committed=True, question_number=1,
    ))
    assert gc.phase is not Phase.LOBBY
    # And a genuine pre-start lobby (neither started nor committed) stays LOBBY.
    lobby = from_latches(**_latch_defaults(
        game_started=False, game_start_committed=False, question_number=0,
    ))
    assert lobby.phase is Phase.LOBBY


def test_from_latches_always_valid_across_priority_cross_product():
    # Every latch combination from_latches produces must be a storable
    # GameControl (from_latches raises IllegalControlState otherwise).
    import itertools
    bools = [False, True]
    for (started, over, stop, adj, trans, hold, awo) in itertools.product(
        bools, bools, bools, bools, bools, bools, bools
    ):
        armed = {"id": "q"} if (adj or awo) else None
        active = 3 if adj else None
        try:
            gc = from_latches(**_latch_defaults(
                game_started=started, game_over=over,
                delivery_stop_sticky=stop, adjudicating=adj,
                question_transitioning=trans, hold_active=hold,
                hold_reason="r" if hold else None,
                answer_window_open=awo,
                active_delivery_qnum=active,
                armed_question=armed,
                open_transition_qnum=3 if trans else None,
            ))
        except IllegalControlState:
            # Adjudicating with nothing armed is the one combo we deliberately
            # forbid; from_latches surfaces it rather than storing it.
            assert adj and armed is None
            continue
        assert isinstance(gc, GameControl)
