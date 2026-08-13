"""lily-1C53C6 — the journaled-but-silent transition deadlock.

Live, 2026-08-09 07:38–07:43, single caller, adult mode. Question 2's
reveal generation (grok-4.5) timed out twice at the prefetch timeout; the
transition had already been OPENED and JOURNALED, but its verdict turn
died before any audio played. From there the watchdog and the N12 guard
locked each other out forever:

    ARMED_LIMBO  -> forcing adjudication          (every ~20s)
    adjudicate   -> open_question_transition
                 -> SECOND_LANE_REFUSED            ("a journal already
                                                    exists")

~92 seconds of dead air until the caller hung up. The N12 guard keyed
purely on "journal exists" and never asked whether the journaled stages
ever reached the air — the state the class already tracks (bound
narrations, CONFIRMED stage keys, a journaled next_delivery).

The fix: the watchdog's recovery adjudication passes
reclaim_transition=True, and open_question_transition may then RECLAIM a
journal none of whose stages provably aired — releasing the dead claims
so the recovery narrates. A transition with ANY aired stage keeps the
original refusal: recovery must never grant a second narration of
something that actually played (N12 stands).

Same import boundary note as test_hotfix006_transitions.py.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio

import lily_config
import lily_say_gate
from lily_agent import LilyGame
from lily_scorekeeper import LilyScorekeeper


def _make_game(session_id: str = "lily-1C53C6") -> LilyGame:
    """Minimal LilyGame via __new__ — only what the transition-journal
    surface touches (test_hotfix006_transitions pattern, narrowed)."""
    game = LilyGame.bare()
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game.sk = LilyScorekeeper(session_id)
    return game


def _open_and_journal_silent_transition(game: LilyGame, qnum: int = 2) -> str:
    """Reproduce the wedged state from the live session: transition opened,
    reveal + verdict journaled, verdict key claimed at dispatch — and then
    the speech dies before playout. No narration bound, nothing CONFIRMED."""
    owner = "lane_a"
    assert game.open_question_transition(qnum, owner=owner, source="adjudicate")
    game.journal_transition(
        qnum, "reveal", owner=owner,
        detail={"answer": "Jupiter", "correct": False, "winner": None},
    )
    verdict_key = f"q_{qnum}_reveal"
    assert game.say_registry.claim(verdict_key, owner="speech_dead")
    game.journal_transition(
        qnum, "verdict", owner=owner, detail={"key": verdict_key},
    )
    # The claim stays PENDING forever — the turn never reached playout.
    assert game.say_registry.state(verdict_key) == lily_say_gate.CLAIM_PENDING
    return verdict_key


def test_the_live_deadlock_a_plain_reopen_still_refuses():
    """Without the recovery flag the N12 refusal stands — exactly the loop
    the watchdog was stuck in. This is the pre-fix behavior and it must
    stay: an ordinary second lane never reclaims anything."""
    game = _make_game()
    _open_and_journal_silent_transition(game)

    assert game.open_question_transition(
        2, owner="lane_b", source="adjudicate"
    ) is False


def test_recovery_reclaims_a_journaled_transition_that_never_aired():
    """THE fix. The recovery lane (reclaim_unaired=True) takes over the
    dead transition: journal reset, transition claim and the stale verdict
    key released, and the fresh lane can claim + journal + speak."""
    game = _make_game()
    stale_verdict_key = _open_and_journal_silent_transition(game)

    assert game.open_question_transition(
        2, owner="recovery", source="adjudicate", reclaim_unaired=True
    ) is True
    # The dead lane's stage key was released — the recovery's verdict
    # dispatch can claim it again instead of bouncing off a ghost.
    assert game.say_registry.state(stale_verdict_key) is None
    assert game.say_registry.claim(stale_verdict_key, owner="speech_new")
    # Fresh journal for the reclaimed transition — stages replay cleanly.
    assert game.transition_stages(2) == []
    assert game.journal_transition(
        2, "reveal", owner="recovery", detail={"answer": "Jupiter"},
    )


def test_recovery_never_reclaims_a_bound_narration():
    """N12 stands: a transition whose verdict NARRATION is bound (words
    actually went out through the say gate) is not reclaimable — recovery
    must not re-narrate a beat that played."""
    game = _make_game()
    _open_and_journal_silent_transition(game)
    entry = game._transition_entry(2, "verdict")
    entry["detail"]["narration"] = "Nobody landed it — Jupiter!"

    assert game.open_question_transition(
        2, owner="recovery", source="adjudicate", reclaim_unaired=True
    ) is False


def test_recovery_never_reclaims_a_confirmed_stage_key():
    """N12 stands: CLAIM_CONFIRMED means the stage's speech reached
    playout — the same signal every other aired-or-not decision trusts."""
    game = _make_game()
    verdict_key = _open_and_journal_silent_transition(game)
    game.say_registry.confirm(verdict_key)

    assert game.open_question_transition(
        2, owner="recovery", source="adjudicate", reclaim_unaired=True
    ) is False


def test_recovery_never_reclaims_a_finished_transition():
    """N12 stands: a journaled next_delivery means the beat completed —
    question N+1 went out under this transition. Nothing to recover."""
    game = _make_game()
    _open_and_journal_silent_transition(game)
    game.journal_transition(
        2, "next_delivery", owner="lane_a", detail={"qnum": 3},
    )

    assert game.open_question_transition(
        2, owner="recovery", source="adjudicate", reclaim_unaired=True
    ) is False


def test_prefetch_total_budget_bounds_stacked_stalls():
    """The trigger (lily_reasoning): per-call timeouts used to be the only
    bound, so generate -> verify -> choices could stack ~90s of dead wait
    before PREFETCH_FAILED. The whole chain now shares one overall budget;
    a hung model call fails honestly within it (status note set, None
    returned) instead of stacking."""
    import lily_reasoning

    reasoning = lily_reasoning.LilyReasoning.__new__(
        lily_reasoning.LilyReasoning
    )

    async def _hang(*args, **kwargs):
        await asyncio.sleep(60)

    reasoning.generate_question = _hang

    sk = LilyScorekeeper("lily-1C53C6")

    original_budget = lily_config.prefetch_total_budget_seconds
    lily_config.prefetch_total_budget_seconds = lambda: 0.2
    try:
        async def _go():
            return await asyncio.wait_for(
                reasoning.prefetch_question(sk, "space", 2, []),
                timeout=5.0,  # the budget must fire well before this
            )
        result = asyncio.new_event_loop().run_until_complete(_go())
    finally:
        lily_config.prefetch_total_budget_seconds = original_budget

    assert result is None
    assert sk.status_notes  # the honest "question machine failure" note


def test_prefetch_walls_are_read_at_call_time():
    """Hygiene (HOTFIX-008): 82ad673 froze the lily_config wall accessors
    into module constants at import, so a live env change to either wall
    did nothing until the next deploy. The walls are read through the
    accessors at call time — patching an accessor AFTER lily_reasoning
    is imported must move the wall. Pinned on the per-call wall; the
    overall-budget accessor is pinned the same way by
    test_prefetch_total_budget_bounds_stacked_stalls above."""
    import lily_reasoning

    reasoning = lily_reasoning.LilyReasoning.__new__(
        lily_reasoning.LilyReasoning
    )

    async def _hang(*args, **kwargs):
        await asyncio.sleep(60)

    reasoning.generate_question = _hang

    sk = LilyScorekeeper("lily-1C53C6")

    original_wall = lily_config.prefetch_timeout_seconds
    lily_config.prefetch_timeout_seconds = lambda: 0.2
    try:
        async def _go():
            return await asyncio.wait_for(
                reasoning.prefetch_question(sk, "space", 2, []),
                timeout=5.0,  # the 0.2s per-call wall must fire well first
            )
        result = asyncio.new_event_loop().run_until_complete(_go())
    finally:
        lily_config.prefetch_timeout_seconds = original_wall

    assert result is None
    assert sk.status_notes  # the honest "question machine failure" note
