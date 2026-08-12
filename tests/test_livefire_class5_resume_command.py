"""WO-LILY-LIVEFIRE-001 CLASS 5 — resume is a command.

Fixture lily-639007-f80aa6bf ~14:01: "Continue with Greece, dude. Make more
fucking questions." never matched the anchored ^continue$ regex, so the
addressee layer filed it side_chatter, _delivery_stop_sticky stayed True, the
LLM narrated a resume the machine had not executed, and the feed went quiet.
Resume is now one command path: recognized anywhere in the utterance, it
clears sticky and releases the hold atomically and the state machine restarts
delivery.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_scorekeeper
from lily_scorekeeper import lily_detect_resume_game
from test_hotfix006_transitions import _make_game


# -- 5a/5b: resume recognized anywhere in the utterance -------------------

def test_fixture_resume_line_is_recognized():
    assert lily_detect_resume_game(
        "Continue with Greece, dude. Make more fucking questions."
    ) is True


def test_resume_intent_set():
    for line in [
        "continue", "continue with greece", "keep going",
        "make more questions", "resume", "give us another question",
        "next question", "let's keep playing",
    ]:
        assert lily_detect_resume_game(line) is True, line


def test_resume_negations_are_not_resume():
    for line in [
        "not yet, don't continue", "hold off on more questions",
        "maybe later, keep it paused",
    ]:
        assert lily_detect_resume_game(line) is False, line


# -- 5c/5d: one atomic transition, delivery restarts from the spine -------

def test_resume_clears_sticky_and_restarts_delivery():
    game = _make_game()
    # Freeze the game as a STOP would, with an armed card preserved (Class 4).
    armed = {"id": "kb_457", "prompt": "Greece Q", "category": "Greece"}
    game.armed_question = armed
    game.next_question = None
    game._delivery_stop_sticky = True
    game._hold_active = True
    game._hold_reason = "stop_primitive"

    dispatched = []
    game.dispatch_armed_question = lambda *, source: dispatched.append(source) or True
    game.start_prefetch = lambda *a, **k: None

    assert game.game_delivery_stopped() is True
    ok = game.resume_game_delivery(reason="spoken_resume")

    assert ok is True
    # Sticky-clear and hold-release are one atomic operation (5c).
    assert game._delivery_stop_sticky is False
    assert game._hold_active is False
    # No narrated-but-frozen state — the game is live again.
    assert game.game_delivery_stopped() is False
    # The state machine restarted delivery of the preserved armed card (5d).
    assert dispatched == ["resume"]


def test_resume_is_noop_when_not_stopped():
    game = _make_game()
    game._delivery_stop_sticky = False
    assert game.resume_game_delivery(reason="spoken_resume") is False
