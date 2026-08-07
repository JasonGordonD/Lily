"""WO-LILY-CAPABILITY-RESTORE-001 — on-the-fly category creation.

The live 2026-08-06 session had Lily DENY a custom topic ("my general
deck is pre-loaded... can't do a specific custom topic") even though her
README names "generates her own questions on the fly" as core behavior
and the generator already accepts any `category` string. The gap was
wiring: the round category came only from the fixed family rotation, with
no path for a table-named subject to reach it.

This suite pins the restored path deterministically: lily_set_category
routes the requested topic into _category_for_round (the exact seam
_prefetch_inner reads before calling the generator), invalidates the
stale prefetch, never denies, and keeps the adult deck's identity
firewall intact. The live proof that the generator honors an arbitrary
topic (a real Gemini "Game of Thrones" round) is attached to the WO
report — it needs a funded GOOGLE_API_KEY and does not run in CI.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_say_gate
from lily_agent import CATEGORY_FAMILIES, LilyAgent, LilyGame
from lily_scorekeeper import LilyScorekeeper


def _make_game(question_number: int = 0, mode: str = "general") -> LilyGame:
    """Real LilyGame via __new__ (sidestep the heavy livekit init) with the
    exact surface lily_set_category + _round_for_next_question +
    _category_for_round touch."""
    game = LilyGame.__new__(LilyGame)
    game.sk = LilyScorekeeper("custom-category-fixture")
    game.sk.mode = mode
    game.sk.question_number = question_number
    game.sk.questions_per_round = 6
    game.rounds_total = 4
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game._category_override = {}
    game.next_question = {"id": "q_old", "category": "academic"}
    game.armed_question = None
    game._prefetch_task = None
    game._prefetch_stall_ticks = 0
    game.game_started = True
    game.game_over = False
    game.prefetch_calls = 0
    game.start_prefetch = lambda: setattr(
        game, "prefetch_calls", game.prefetch_calls + 1
    )
    return game


def _call_set_category(game: LilyGame, topic: str) -> str:
    agent = LilyAgent.__new__(LilyAgent)
    agent._game = game
    coro = LilyAgent.lily_set_category.__wrapped__(agent, None, topic)
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_requested_topic_reaches_the_generator_seam():
    """The override lands on the round the NEXT question belongs to, and
    _category_for_round returns exactly the requested subject — the string
    _prefetch_inner passes to reasoning.prefetch_question(category=...)."""
    game = _make_game(question_number=0)  # next question is round 1
    msg = _call_set_category(game, "Game of Thrones")
    assert game._category_override == {1: "Game of Thrones"}
    assert game._category_for_round(1) == "Game of Thrones"
    # Never a denial — she commits to building the round.
    assert "Game of Thrones" in msg
    denials = ("can't", "cannot", "pre-loaded", "isn't available", "fixed")
    assert not any(d in msg.lower() for d in denials), msg


def test_stale_prefetch_is_dropped_and_reissued():
    game = _make_game(question_number=0)
    _call_set_category(game, "Japan")
    # The question drawn on the old category is discarded; a fresh draw
    # under the new topic is kicked off.
    assert game.next_question is None
    assert game.prefetch_calls == 1
    # And the table hears an honest "building your round" vamp, not silence.
    assert any("Japan" in note for note in game.sk.status_notes)


def test_topic_is_whitespace_normalized_and_bounded():
    game = _make_game(question_number=0)
    _call_set_category(game, "  90s   hip-hop\n")
    assert game._category_override[1] == "90s hip-hop"


def test_empty_topic_asks_again_and_sets_nothing():
    game = _make_game(question_number=0)
    msg = _call_set_category(game, "   ")
    assert game._category_override == {}
    assert "what subject" in msg.lower()


def test_override_applies_to_the_next_questions_round_when_midround():
    # question 8 of a 6-per-round game → the next question is round 2.
    game = _make_game(question_number=8)
    _call_set_category(game, "Formula 1")
    assert 2 in game._category_override
    assert game._category_for_round(2) == "Formula 1"
    # Rounds she didn't request keep the fixed rotation.
    assert game._category_for_round(1) == CATEGORY_FAMILIES[0]
    assert game._category_for_round(3) == CATEGORY_FAMILIES[2]


def test_adult_mode_redirects_without_denying_or_crossing_the_firewall():
    """The adult deck rotates its OWN families; a custom label must never
    ride an adult question (announcing an adult question as an academic
    category was a live defect). Redirect honestly — never a flat 'no'."""
    game = _make_game(question_number=0, mode="adult")
    msg = _call_set_category(game, "Game of Thrones")
    # No override set on the adult deck.
    assert game._category_override == {}
    # Honest redirect that still names the topic and the way back.
    assert "back to normal" in msg.lower()
    assert "Game of Thrones" in msg


def test_before_game_start_sets_override_without_prefetching():
    game = _make_game(question_number=0)
    game.game_started = False
    _call_set_category(game, "Ancient Rome")
    assert game._category_override[1] == "Ancient Rome"
    # No live supply line before the game engine is running.
    assert game.prefetch_calls == 0


def test_cancelled_old_category_draw_cannot_commit_after_switch():
    """A coroutine past its last await cannot resurrect the prior category."""
    game = _make_game(question_number=0)
    game.start_prefetch = LilyGame.start_prefetch.__get__(game, LilyGame)
    game.next_question = None
    game.supabase = None
    game.session = None
    game.group_id = "category-race"
    game.used_prompts = []
    game.asked_history = []
    game._drawn_ids = set()
    game._drawn_hashes = set()
    game._burned_question_ids = set()
    game._burned_question_hashes = set()
    game.promoted_categories = []
    game.publish_attributes_nowait = lambda: None

    class _Reasoning:
        def __init__(self):
            self.calls = 0

        async def prefetch_question(self, sk, *, category, **kwargs):
            self.calls += 1
            if self.calls == 1:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    # Simulate a provider call that completed at cancellation.
                    return {
                        "id": "stale-academic",
                        "prompt": "Old category question?",
                        "canonical_answer": "old",
                        "acceptable_answers": ["old"],
                        "category": category,
                    }
            return {
                "id": "fresh-japan",
                "prompt": "New category question?",
                "canonical_answer": "new",
                "acceptable_answers": ["new"],
                "category": category,
            }

    game.reasoning = _Reasoning()
    agent = LilyAgent.__new__(LilyAgent)
    agent._game = game

    async def scenario():
        game.start_prefetch()
        await asyncio.sleep(0)
        await LilyAgent.lily_set_category.__wrapped__(agent, None, "Japan")
        for _ in range(5):
            await asyncio.sleep(0)

    asyncio.run(scenario())
    landed = game.armed_question or game.next_question
    assert landed is not None
    assert landed["id"] == "fresh-japan"
    assert landed["category"] == "Japan"
