"""Y2 (WO-LILY-HOTFIX-007) — settle-on-event, reproduced and fixed.

Live evidence (lily-05BB92, the first instrumented session): preemptive
invalidated=10, used=0 — every speculative reply died at the framework's
commit-time equivalence check, so every turn paid full LLM+TTS latency.
Cause: ASYNC game events (a prefetch landing flips the stable block's
"next question: ready" line) mutate the agent's persistent ctx inputs
between speculation and commit. The volatile-tail split can't see these
— they are STABLE-block changes, by design ("changes only when the game
genuinely moves") — the game just moves asynchronously.

The fix: settle_context_nowait refreshes the persistent ctx the moment
stable inputs change (same _apply_context_blocks, same idempotent
semantics), wired through the existing publish_attributes_nowait
chokepoint plus the two supply-landing sites that bypass it. The
speculation then snapshots CURRENT state and commit finds nothing new.

These tests replay the framework's ACTUAL comparison
(ChatContext.is_equivalent on real contexts) around a simulated async
landing — the reproduction fails without settle, passes with it.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from livekit.agents.llm import ChatContext

from lily_agent import LILY_SYSTEM_PROMPT, LilyAgent, LilyGame
from lily_scorekeeper import LilyScorekeeper


def _game() -> LilyGame:
    game = LilyGame.bare()
    game.sk = LilyScorekeeper("y2-settle")
    game.sk.bind_speaker("S1", "Rami")
    game.session_started_at = 0.0
    game.availability_flags = None
    game.promoted_categories = []
    game.armed_question = None
    game.game_started = True
    game.game_over = False
    game.next_question = None
    game.memory_block = ""
    game._pending_unbound_award = None
    game._delivery_stop_sticky = False
    game.eliminated = []
    game.forget_state = None
    game.prefs = {}
    return game


def _wire(game) -> LilyAgent:
    agent = LilyAgent.__new__(LilyAgent)
    agent._game = game
    agent._chat_ctx = ChatContext.empty()
    agent._chat_ctx.add_message(role="system", content=LILY_SYSTEM_PROMPT)
    game.agent = agent
    # Previous turn's hook left the blocks current.
    agent._apply_context_blocks(agent._chat_ctx, now=1_000.0)
    return agent


def _commit_side(agent) -> ChatContext:
    """What the framework compares against at commit: a copy of the agent
    ctx after on_user_turn_completed's block apply. (The user message is
    NOT in either compared context — the framework stores the speculation
    snapshot before inserting it and compares the transcript separately,
    word-level; verified against agent_activity at 1.6.8.)"""
    committed = agent._chat_ctx.copy()
    agent._apply_context_blocks(committed, now=1_002.0)
    return committed


def test_reproduction_async_landing_invalidates_without_settle():
    """The live defect, in miniature: speculation snapshots, THEN the
    prefetch lands (next_question flips), THEN commit re-applies blocks —
    the framework's own is_equivalent fails and the speculative reply is
    thrown away."""
    game = _game()
    agent = _wire(game)

    snapshot = agent._chat_ctx.copy()  # what speculation stores

    game.next_question = {  # the async landing — NO settle (old behavior)
        "prompt": "Which planet has the shortest day?",
        "canonical_answer": "Jupiter",
    }

    assert not snapshot.is_equivalent(_commit_side(agent))


def test_settle_at_the_landing_keeps_the_speculation_valid():
    """Event order with the fix: landing → settle → speculation → commit.
    The snapshot carries current state; commit finds nothing new."""
    game = _game()
    agent = _wire(game)

    game.next_question = {
        "prompt": "Which planet has the shortest day?",
        "canonical_answer": "Jupiter",
    }
    game.settle_context_nowait()  # what the landing sites now do

    snapshot = agent._chat_ctx.copy()

    assert snapshot.is_equivalent(_commit_side(agent))


def test_settle_is_idempotent_and_quiet_when_nothing_changed():
    """The chokepoint fires several times per turn — an unchanged state
    must leave the ctx items untouched (same objects), or settle itself
    would become the invalidator."""
    game = _game()
    agent = _wire(game)
    game.settle_context_nowait()
    before = [id(item) for item in agent._chat_ctx.items]
    game.settle_context_nowait()
    game.settle_context_nowait()
    assert [id(item) for item in agent._chat_ctx.items] == before


def test_settle_without_an_agent_handle_is_a_noop():
    game = _game()  # no .agent — pure fixtures, pre-session paths
    game.settle_context_nowait()  # must not raise


def test_the_chokepoint_and_both_landing_sites_settle():
    """Source pins: publish_attributes_nowait settles first; both async
    supply-landing sites (prefetch success, bank_to_supply) settle after
    setting next_question."""
    import inspect

    pub = inspect.getsource(LilyGame.publish_attributes_nowait)
    assert "settle_context_nowait" in pub

    import re

    import lily_agent as la
    import lily_supply

    src = inspect.getsource(la) + inspect.getsource(lily_supply)
    # Each landing site sets next_question then settles within a few lines.
    landings = [
        m.start()
        for m in re.finditer(r"self\.next_question = question\b", src)
    ]
    assert len(landings) >= 2
    settled = sum(
        1 for i in landings
        if "settle_context_nowait" in src[i:i + 500]
    )
    assert settled >= 2, "an async supply landing no longer settles"
