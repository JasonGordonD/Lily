"""P0-G: unresolved direct-address/meta work owns the floor over N+1."""

import inspect

import lily_say_gate
from lily_agent import LilyGame
from lily_scorekeeper import LilyScorekeeper


QUESTION = {
    "prompt": "This planet is known as the Red Planet.",
    "canonical_answer": "Mars",
    "acceptable_answers": ["mars"],
}


def _game() -> LilyGame:
    game = LilyGame.__new__(LilyGame)
    game.sk = LilyScorekeeper("meta-pause")
    game.sk.bind_speaker("S1", "Rami")
    game.sk.start_question(dict(QUESTION))
    game.armed_question = dict(QUESTION)
    game.game_started = True
    game.game_over = False
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game._delivery_stop_sticky = False
    game._hold_active = False
    game._question_pending = False
    game._awaiting_address_since = 0.0
    game._setup_pending = set()
    game._answered_questions = set()
    game._pending_delivery_qnum = None
    game._transition_journal = {}
    game._open_transition_qnum = None
    return game


def test_unanswered_direct_address_blocks_question_dispatch():
    game = _game()
    game._awaiting_address_since = 100.0
    called = []
    game.expect_delivery = lambda: called.append("expect")
    game.gated_say = lambda *a, **k: called.append("say") or True
    assert game.dispatch_armed_question(source="post_reveal") is False
    assert called == []


def test_host_speaking_and_setup_each_pause_progression():
    game = _game()
    game.sk.host_speaking = True
    assert game.progression_paused_reason() == "host_speaking"
    game.sk.host_speaking = False
    game._setup_pending = {"pictures"}
    assert game.progression_paused_reason() == "setup_pending"


def test_expect_delivery_does_not_arm_while_meta_is_unanswered():
    game = _game()
    game._awaiting_address_since = 100.0
    game.expect_delivery()
    assert game._pending_delivery_qnum is None


def test_question_nudge_is_suppressed_at_dispatch_choke_point():
    game = _game()
    game._awaiting_address_since = 100.0
    game.instructed_reply = lambda _: (_ for _ in ()).throw(
        AssertionError("paused progression must not generate speech")
    )
    assert game.gated_say(
        None,
        "question_nudge",
        "ask the next question",
        source="idle_watchdog",
    ) is False


def test_watchdog_checks_pause_before_delivery_recovery():
    # The pause gate must sit ahead of the delivery/idle recovery rows so a
    # paused game is never reconciled or re-armed. In the W2b policy table
    # that priority IS the row order.
    game = LilyGame.__new__(LilyGame)
    names = [p.name for p in game._make_watch_policies()]
    assert names.index("progression_paused") < names.index("armed")
    assert names.index("progression_paused") < names.index("idle_rearm")
