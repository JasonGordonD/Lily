"""WO-LILY-PATCH-003 P9 — responsiveness floor after direct address.

Fixture: "There's no picture. Hey... Hello?" -> ~34 seconds of silence.
A direct address gets a response within the budget; unanswered silence
past it trips ADDRESS_UNANSWERED. The grounded holding beat ('one sec —
checking the picture lane') is a state-block template, never a vamp.
"""

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_config
import lily_say_gate
from lily_agent import LilyGame
from lily_scorekeeper import LilyScorekeeper
from lily_speech_delivery import LilySpeechDeliveryMixin


def _make_game():
    game = LilyGame.bare()
    game.sk = LilyScorekeeper("p9")
    game._awaiting_address_since = 0.0
    game._address_unanswered_warned = False
    game._playout_started_ids = set()
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game.cancel_cut_recovery = lambda: None
    return game


def test_nothing_awaiting_is_never_unanswered():
    game = _make_game()
    assert game.address_unanswered() is False


def test_address_within_budget_is_not_flagged(monkeypatch):
    monkeypatch.setattr(lily_config, "responsiveness_budget_seconds", lambda: 3.0)
    game = _make_game()
    game._awaiting_address_since = 100.0
    assert game.address_unanswered(now=101.0) is False  # 1s < 3s


def test_address_past_budget_is_flagged(monkeypatch):
    monkeypatch.setattr(lily_config, "responsiveness_budget_seconds", lambda: 3.0)
    game = _make_game()
    game._awaiting_address_since = 100.0
    assert game.address_unanswered(now=134.0) is True  # the 34s fixture


def test_dispatch_does_not_clear_the_clock():
    body = inspect.getsource(LilySpeechDeliveryMixin.gated_say)
    assert "_awaiting_address_since = 0.0" not in body


def test_real_response_playout_clears_the_clock():
    game = _make_game()
    game._awaiting_address_since = 100.0
    game._address_unanswered_warned = True
    game.note_playout_started("speech-1")
    assert game.address_unanswered(now=200.0) is False
    assert game._address_unanswered_warned is False
    assert "speech-1" in game._playout_started_ids


def test_budget_config_default():
    import os
    os.environ.pop("LILY_RESPONSIVENESS_BUDGET_SECONDS", None)
    assert lily_config.responsiveness_budget_seconds() == 3.0
