"""C13 (WO-LILY-HOSTLOOP-001) — STOP equivalents halt within one utterance.

Archaeology (why this wasn't duplication): HOTFIX-009 W8/W2 + HOTFIX-010
V4 already built the STOP lane — addressed stop, quit-game, emphatic
repetition, solo bare stop, garble tolerance, sticky latch, hold binding
on the delivery/greeting lanes, single ack, narration integrity. What
never existed is a USER-side detector for the softer equivalents: "hold
on", "wait", "pause", "one sec" — enter_hold fired only from STOP,
declines, and Lily's own wait-promises. The 3.4/F "STOP defied ×4" class
includes these forms.

The danger shape, pinned hard: "wait" is ANSWER vocabulary in trivia.
Equivalents fire only utterance-shaped; embedded forms never do.
"""

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_say_gate
from lily_agent import LilyGame
from lily_scorekeeper import LilyScorekeeper, lily_detect_hold_request


def test_hold_equivalents_fire_utterance_shaped():
    for text in (
        "hold on", "Hold on a second", "hang on", "wait", "Wait wait",
        "wait a minute", "pause", "pause the game", "one sec",
        "give us a minute", "okay Lily, hold on", "just wait a moment",
    ):
        assert lily_detect_hold_request(text) is True, text


def test_answers_and_content_never_fire():
    """The trivia danger shape: request-words leading into content."""
    for text in (
        "wait, is it Saturn?",
        "wait I know this one it's Jupiter",
        "hold on, I know this",
        "wait for the next question please, Maya is coming back",
        "the pause button on my phone is broken",
        "we can't wait for round two",
        "hold on to your hats everyone",
    ):
        assert lily_detect_hold_request(text) is False, text


def _game() -> LilyGame:
    game = LilyGame.__new__(LilyGame)
    game.sk = LilyScorekeeper("c13-hold")
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game.session = None
    game.agent = None
    return game


def test_hold_request_enters_the_hold_and_acks_once():
    game = _game()
    acks = []
    game.gated_say = lambda key, act, instructions, source=None, **kw: acks.append(act)
    game.handle_hold_request("hold on")
    assert game._hold_active is True
    assert game._hold_reason == "player_hold_request"
    assert acks == ["hold_ack"]
    # Second request while held: hold refreshed, NO second ack (the V4
    # double-ack lesson, honored on this lane from day one).
    game.handle_hold_request("wait")
    assert acks == ["hold_ack"]


def test_hold_request_is_not_the_sticky_stop():
    """A beat, not a brake: no sticky delivery latch, so the existing
    hold-release paths (player speaks on, timeout) resume the game
    without an explicit 'resume' utterance."""
    game = _game()
    game.gated_say = lambda *a, **kw: None
    game.handle_hold_request("one sec")
    assert not getattr(game, "_delivery_stop_sticky", False)


def test_consult_routes_stop_before_hold():
    """Source pin: the deterministic consult checks the hard STOP first —
    'stop' never degrades into a soft hold — and the hold consult lives in
    the same pre-LLM reflex path."""
    src = None
    for name, member in inspect.getmembers(LilyGame, inspect.isfunction):
        try:
            s = inspect.getsource(member)
        except (OSError, TypeError):
            continue
        if "lily_detect_stop(" in s and "lily_detect_hold_request(" in s:
            src = s
            break
    assert src is not None, "consult method with both detectors not found"
    assert src.index("lily_detect_stop(") < src.index(
        "lily_detect_hold_request("
    )
