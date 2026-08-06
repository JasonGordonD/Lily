"""WO-LILY-PATCH-003 P8 — game payloads require a live game state.

Fixture: "Nobody" — the buzzer-lockout opener aired into lobby
conversation with no question live. Every game-lane payload (delivery,
verdict, reveal, steal/lockout, scores) validates game state at dispatch;
in lobby or after game-over none can air. Extends the A4 hold/lane gate.
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_say_gate
from lily_agent import LilyGame
from lily_scorekeeper import LilyScorekeeper


def _make_game(started: bool, over: bool = False) -> LilyGame:
    game = LilyGame.__new__(LilyGame)
    game.sk = LilyScorekeeper("p8-fixture")
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game.game_started = started
    game.game_over = over
    game._hold_active = False
    game.instructed_replies = []
    game.instructed_reply = lambda t: game.instructed_replies.append(t)
    return game


def test_steal_lockout_blocked_in_lobby():
    """THE 'Nobody' fixture: the steal/lockout opener cannot air pre-game."""
    game = _make_game(started=False)
    with_log = game.gated_say(None, "steal_window", "Nobody landed it!", "adjudicate")
    assert with_log is False
    assert game.instructed_replies == []


def test_every_game_lane_act_blocked_in_lobby(caplog):
    game = _make_game(started=False)
    with caplog.at_level(logging.WARNING):
        for act in ("question_delivery", "verdict", "reveal", "reveal_finale",
                    "reveal_scores", "steal_window", "question_nudge"):
            assert game.gated_say(None, act, "x", "adjudicate") is False
    assert all(
        "no_live_game" in r.message
        for r in caplog.records if "SUPPRESSED" in r.message
    )


def test_game_lane_acts_blocked_after_game_over():
    game = _make_game(started=True, over=True)
    assert game.gated_say(None, "verdict", "Correct!", "adjudicate") is False


def test_game_lane_acts_air_during_a_live_game():
    game = _make_game(started=True)
    assert game.gated_say(None, "steal_window", "five seconds!", "adjudicate") is True
    assert game.gated_say(None, "verdict", "Correct — Saturn!", "adjudicate") is True


def test_non_game_acts_are_never_game_gated():
    """Greeting, banter, mode reverts must dispatch in lobby — only the
    game lane is gated."""
    game = _make_game(started=False)
    assert game.gated_say("session_greet", "greet", "hi table!", "on_enter") is True
    assert game.gated_say(None, "banter", "nice weather", "lobby") is True
