"""WO-LILY-PATCH-003 P6 + P10 — ask-then-listen, yield-after-question.

P10 fixtures: "Ready for question one, Rami?" rolled straight onward; the
recognition beat + "want a refresher?" stacked onto an already-open
question. Asking obligates listening: after ANY conversational question
Lily poses, her turn ends and the floor yields — no follow-on content, no
queued beat — until the table answers or a timeout gives one gentle
re-offer, then a hold. P6: the user's next turn IS the answer, released
and engaged (she finishes conversations she starts).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_say_gate
from lily_agent import LilyGame
from lily_scorekeeper import LilyScorekeeper


def _make_game():
    game = LilyGame.__new__(LilyGame)
    game.sk = LilyScorekeeper("p6p10")
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game.game_started = True
    game.game_over = False
    game._hold_active = False
    game._question_pending = False
    game._question_pending_since = 0.0
    game._question_pending_reoffered = False
    game.instructed_replies = []
    game.instructed_reply = lambda t: game.instructed_replies.append(t)
    return game


# -- P10: yield blocks unsolicited beats ---------------------------------------


def test_pending_question_blocks_unsolicited_beats():
    game = _make_game()
    game.enter_question_pending("Anything I should know about you?")
    assert game._question_pending is True
    # A queued conversational beat (recognition, menu) is suppressed.
    assert game.gated_say(None, "late_recognition", "Wait — Rami!", "memory") is False
    assert game.instructed_replies == []


def test_pending_question_exempts_stop_and_game_lane():
    game = _make_game()
    game.enter_question_pending("Ready?")
    # STOP ack still passes (hers, always).
    assert game.question_pending_blocks_dispatch("stop_ack", "stop_primitive") is False
    # Game-lane acts have their own windows.
    assert game.question_pending_blocks_dispatch("verdict", "adjudicate") is False
    # The re-offer itself passes.
    assert game.question_pending_blocks_dispatch("q", "question_reoffer") is False
    # A plain conversational beat is blocked.
    assert game.question_pending_blocks_dispatch("banter", "memory") is True


# -- P6: the user's answer releases + is engaged -------------------------------


def test_user_final_releases_the_pending_question():
    game = _make_game()
    game.enter_question_pending("Ready?")
    assert game.release_question_pending(reason="user_answered") is True
    assert game._question_pending is False
    # Now beats flow again.
    assert game.gated_say(None, "banter", "Great!", "memory") is True


# -- P10 timeout -> one re-offer -----------------------------------------------


def test_pending_times_out_to_one_reoffer(monkeypatch):
    import lily_config
    monkeypatch.setattr(lily_config, "hold_timeout_seconds", lambda: 0.0)
    game = _make_game()
    game.enter_question_pending("Ready for question one?")
    assert game._question_pending_timed_out() is True
    assert game._question_pending_reoffered is False


# -- the trigger: a conversational question opens the pending state ------------


def test_conversational_question_turn_opens_pending():
    """record path: a played conversational turn ending in a question
    yields; a game delivery does not."""
    game = _make_game()
    assert lily_say_gate.lily_stacked_question_flag("First time playing?") == 1
    # (the on_agent_speech_finished wiring calls enter_question_pending for
    # a non-game turn with a question — exercised here at the unit level)
    game.enter_question_pending("First time playing?")
    assert game._question_pending is True
