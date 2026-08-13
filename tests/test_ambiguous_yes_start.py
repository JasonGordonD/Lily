"""WO-2: bare yes after an A-or-B offer must not open round one.

Live failure class: Lily asks ready-or-waiting / voice-only-or-chase;
Rami answers "Yes, I am." / "Yes, ma'am" — that answers the choice, not
a start. Kickoff only on explicit play language / start_game / begin_round
after the block clears.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_scorekeeper
from lily_agent import LilyAgent, LilyGame
from lily_scorekeeper import LilyScorekeeper


def _game():
    game = LilyGame.bare()
    game.sk = LilyScorekeeper("wo2-ambiguous-yes")
    game.game_started = False
    game.game_over = False
    game._pending_or_choice_offer = False
    game._ambiguous_yes_blocks_start = False
    game._recognition_dispute = False
    game._recognition_dispute_why_answered = False
    game.next_question = {"id": "q1", "prompt": "x"}
    game.armed_question = None
    game._last_bind_at = None
    game.session_started_at = 0.0
    game.ui_phase = "lobby"
    game.memory_block = ""
    game.memory_total_games = 0
    game.memory_player_names = []
    game.device_candidate_group_id = None
    game.supabase = None
    game._drawn_ids = set()
    game._phase_hold = None
    game.promoted_categories = []
    game.acoustic = None
    game._pending_unbound_award = None
    game._late_answer_note = None
    game._explain_request_note = None
    game._contest_note = None
    game._returner_honesty_note = None
    game._recognition_why_note = None
    game._state_note = None
    return game


# -- detectors -----------------------------------------------------------------


def test_bare_affirmatives():
    for text in [
        "Yes",
        "Yeah",
        "Yep.",
        "Yes, I am.",
        "Yes I am",
        "Yes, ma'am",
        "Yes ma'am.",
        "Okay",
        "Sure",
    ]:
        assert lily_scorekeeper.lily_is_bare_affirmative(text), text


def test_not_bare_when_explicit_start():
    for text in [
        "Yes, let's start",
        "Yeah let's play",
        "Let's go",
        "Ready to start",
        "Dive in",
    ]:
        assert not lily_scorekeeper.lily_is_bare_affirmative(text), text


def test_not_bare_when_extra_payload():
    assert not lily_scorekeeper.lily_is_bare_affirmative(
        "Yes, ma'am. Me, myself and I."
    )


def test_or_choice_offer_shapes():
    offers = [
        "Ready to dive in, or are you waiting on anyone else?",
        "Want me to run voice-only grown-up trivia for now, or are you "
        "trying to get the images live first?",
        "Want me to keep chasing that, or start voice-only grown-up trivia "
        "right now while it's sorted?",
        "Want a quick refresher, or straight into round one?",
    ]
    for text in offers:
        assert lily_scorekeeper.lily_detect_or_choice_offer(text), text


def test_non_offer_does_not_arm():
    assert not lily_scorekeeper.lily_detect_or_choice_offer(
        "Awesome to have you here, Rami!"
    )


# -- arm / block / clear -------------------------------------------------------


def test_bare_yes_after_offer_blocks_start():
    g = _game()
    g.note_or_choice_offer(
        "Ready to dive in, or are you waiting on anyone else?"
    )
    assert g._pending_or_choice_offer is True
    g.note_user_start_intent("Yes, I am.", command=None)
    assert g.ambiguous_yes_blocks_start() is True
    assert g.start_blocked_reason() == "ambiguous_yes"


def test_explicit_start_command_clears_pending_offer():
    g = _game()
    g.note_or_choice_offer(
        "Ready to dive in, or are you waiting on anyone else?"
    )
    g.note_user_start_intent("Let's start the game", command="start_game")
    assert g.ambiguous_yes_blocks_start() is False
    assert g._pending_or_choice_offer is False


def test_conversational_choice_consumes_offer_without_lock():
    g = _game()
    g.note_or_choice_offer(
        "Want me to run voice-only grown-up trivia for now, or are you "
        "trying to get the images live first?"
    )
    g.note_user_start_intent("Live immediately.", command=None)
    assert g.ambiguous_yes_blocks_start() is False
    assert g._pending_or_choice_offer is False


def test_start_game_deferred_on_ambiguous_yes():
    g = _game()
    g._ambiguous_yes_blocks_start = True
    g.intake_roundrobin_active = lambda: False

    async def _run():
        await g.start_game(source="voice")
        return g.game_started

    assert asyncio.run(_run()) is False


def test_begin_round_refuses_ambiguous_yes():
    g = _game()
    g._ambiguous_yes_blocks_start = True
    g.intake_roundrobin_active = lambda: False
    agent = LilyAgent.__new__(LilyAgent)
    agent._game = g
    msg = asyncio.run(LilyAgent.lily_begin_round.__wrapped__(agent, None))
    assert "bare yes" in msg.lower() or "choice" in msg.lower()
    assert g.game_started is False


def test_lets_start_after_block_clears_gate():
    """After a bare-yes lock, explicit start language clears the gate."""
    g = _game()
    g.note_or_choice_offer(
        "Ready to dive in, or are you waiting on anyone else?"
    )
    g.note_user_start_intent("Yes, I am.", command=None)
    assert g.start_blocked_reason() == "ambiguous_yes"

    g.note_user_start_intent("Let's start", command="start_game")
    assert g.start_blocked_reason() is None
    assert g.ambiguous_yes_blocks_start() is False
