"""P0-D: explicit confirmed identity is required before Q1."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lily_agent import LilyAgent, LilyGame
from lily_binding import LilyFragmentAccumulator
from lily_scorekeeper import LilyScorekeeper


def _game() -> LilyGame:
    game = LilyGame.__new__(LilyGame)
    game.sk = LilyScorekeeper("identity-before-q1")
    game.fragments = LilyFragmentAccumulator()
    game._confirmed_name_evidence = {}
    game._identity_required_before_start = True
    game._delivery_stop_sticky = False
    game._recognition_dispute = False
    game._recognition_dispute_why_answered = False
    game._ambiguous_yes_blocks_start = False
    game._setup_pending = set()
    game._user_speaking = False
    game.game_started = False
    game.game_over = False
    game._last_bind_at = None
    game.on_speaker_bound = lambda label, name: ""
    return game


def _call_bind(game: LilyGame, label: str, name: str) -> str:
    agent = LilyAgent.__new__(LilyAgent)
    agent._game = game
    return asyncio.run(
        LilyAgent.lily_bind_speaker.__wrapped__(
            agent, None, label, name
        )
    )


def test_playing_returner_sentence_cannot_bind_playing():
    game = _game()
    text = (
        "No, Lily. It's not my first time playing with you tonight. "
        "And I'm on my own."
    )
    game.fragments.add("S1", text)

    msg = _call_bind(game, "S1", "Playing")

    assert "no explicit name was confirmed" in msg
    assert game.sk.players == {}
    assert game.start_blocked_reason() == "identity_unconfirmed"


def test_explicit_name_evidence_outlives_fragment_window_and_overrules_tool():
    game = _game()
    game.note_confirmed_name_evidence("S1", "Rami")

    msg = _call_bind(game, "S1", "Playing")

    assert "Bound: voice S1 is Rami" in msg
    assert "Rami" in game.sk.players
    assert "Playing" not in game.sk.players
    assert game.start_blocked_reason() is None


def test_bare_name_is_valid_confirmed_evidence():
    game = _game()
    game.fragments.add("S1", "Rami.")
    game.note_confirmed_name_evidence("S1", "Rami")

    msg = _call_bind(game, "S1", "Rami")

    assert "Bound: voice S1 is Rami" in msg


def test_named_biometric_label_can_bind_without_spoken_name():
    game = _game()

    msg = _call_bind(game, "Rami", "Rami")

    assert "Bound: voice Rami is Rami" in msg


def test_begin_round_refuses_before_confirmed_identity():
    game = _game()
    game.intake_roundrobin_active = lambda: False
    agent = LilyAgent.__new__(LilyAgent)
    agent._game = game

    msg = asyncio.run(
        LilyAgent.lily_begin_round.__wrapped__(agent, None)
    )

    assert "no player has confirmed a name" in msg.lower()
    assert game.game_started is False
