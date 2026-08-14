"""P0-2 BE8D8B: setup intents are consumed before any start owner."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_scorekeeper
import lily_say_gate
from lily_agent import LilyAgent, LilyGame
from lily_scorekeeper import LilyScorekeeper


BE8D8B_SETUP = (
    "I want to do three things. I want you to change your voice. "
    "I want to play the adult deck. Uh, the one with the pictures. "
    "The mixed pictures. Uh, both the suggestive and explicit. "
    "I am explicitly acknowledging out loud that I'm above 18 years old. "
    "I'm 43 and I'm born in 1983."
)


def _game() -> LilyGame:
    game = LilyGame.bare()
    game.sk = LilyScorekeeper("p0-multi-intent")
    game.game_started = False
    game.game_over = False
    game._setup_requested = set()
    game._setup_pending = set()
    game._setup_heat_requested = None
    game._setup_start_requested = False
    game._age_consent_confirmed = False
    game._user_speaking = False
    game._recognition_dispute = False
    game._recognition_dispute_why_answered = False
    game._ambiguous_yes_blocks_start = False
    game._pending_picture_on_offer = False
    game.next_question = {"id": "general-prefetch", "prompt": "general"}
    game.armed_question = None
    game._last_bind_at = None
    game.supabase = object()
    game.publish_attributes_nowait = lambda: None
    game.intake_roundrobin_active = lambda: False
    return game


def test_be8d8b_final_parses_all_setup_intents():
    intents = lily_scorekeeper.lily_parse_lobby_setup_intents(BE8D8B_SETUP)
    assert intents == {
        "start": True,
        "voice": True,
        "adult": True,
        "media": "pictures",
        "heat": "mix",
        "age_mentioned": True,
        "age_consent": True,
    }


def test_parse_is_nonexclusive_for_start_plus_pictures():
    text = "Let's play, with pictures on."
    assert lily_scorekeeper.lily_detect_control_command(text) == "start_game"
    intents = lily_scorekeeper.lily_parse_lobby_setup_intents(text)
    assert intents["start"] is True
    assert intents["media"] == "pictures"


def test_be8d8b_setup_blocks_start_and_does_not_draw_general_picture():
    game = _game()
    calls = []
    game.try_activate_pictures = lambda **kwargs: calls.append(kwargs) or "on"

    intents = game.note_lobby_setup_intents(BE8D8B_SETUP)

    assert intents["start"] is True
    assert game._setup_start_requested is True
    # 'adult' is no longer a pending setup job — the unified adult deck is
    # always active (content-mode gate removed), so requesting it is a no-op.
    assert game.pending_setup_jobs() == {
        "consent",
        "heat",
        "pictures",
        "voice",
    }
    assert game.start_blocked_reason() == "setup_pending"
    # Heat must commit first; no premature picture activation.
    assert calls == []
    assert game.sk.media_mode == "voice_only"


def test_start_game_choke_refuses_setup_pending():
    game = _game()
    game._setup_pending = {"voice", "adult"}

    asyncio.run(game.start_game(source="voice"))

    assert game.game_started is False


def test_begin_round_refuses_setup_pending():
    game = _game()
    game._setup_pending = {"voice", "adult", "pictures"}
    agent = LilyAgent.__new__(LilyAgent)
    agent._game = game

    msg = asyncio.run(LilyAgent.lily_begin_round.__wrapped__(agent, None))

    assert "setup is incomplete" in msg.lower()
    assert "adult" in msg.lower()
    assert game.game_started is False


def test_user_speaking_blocks_llm_start_race():
    game = _game()
    game._user_speaking = True

    assert game.start_blocked_reason() == "user_speaking"
    asyncio.run(game.start_game(source="host_tool"))
    assert game.game_started is False


def test_setup_jobs_clear_only_after_real_tool_commits():
    game = _game()
    game._setup_pending = {"voice", "adult", "pictures", "heat", "consent"}

    game.mark_setup_applied("voice")
    assert "voice" not in game.pending_setup_jobs()
    assert game.start_blocked_reason() == "setup_pending"

    game.mark_setup_applied("adult", "consent")
    game.mark_setup_applied("pictures", "heat")
    assert game.pending_setup_jobs() == set()
    assert game.start_blocked_reason() is None


def test_picture_only_setup_applies_before_start():
    game = _game()
    game.next_question = None
    calls = []

    def activate(**kwargs):
        calls.append(kwargs)
        game.sk.set_media_mode("pictures")
        return "on"

    game.try_activate_pictures = activate
    intents = game.note_lobby_setup_intents("Let's play with pictures on.")

    assert intents["start"] is True
    assert calls and calls[0]["source"] == "multi_intent_setup"
    assert game.sk.media_mode == "pictures"
    assert game.pending_setup_jobs() == set()


def test_be8d8b_kickoff_fragments_are_detected():
    for text in [
        "Round",
        "Round One",
        "Let's do it!",
        "Let's kick off Round One!",
        "Round One is Geography!",
    ]:
        assert lily_say_gate.lily_unowned_kickoff_fragment(text), text


def test_real_question_or_hold_is_not_kickoff_debris():
    assert not lily_say_gate.lily_unowned_kickoff_fragment(
        "Let's do it! Looking at these blue domes, what country are we in?"
    )
    assert not lily_say_gate.lily_unowned_kickoff_fragment(
        "Hold on — don't start Round One yet."
    )


def test_kickoff_words_require_delivery_owner():
    game = _game()
    game._setup_pending = {"voice", "adult"}

    assert game.unowned_kickoff_must_suppress("Round", None) is True
    assert game.unowned_kickoff_must_suppress("Let's do it!", None) is True
    assert (
        game.unowned_kickoff_must_suppress(
            "Round One is Geography!", "claimed_structural"
        )
        is False
    )
