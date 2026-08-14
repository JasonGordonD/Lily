"""Tests for the P2 preemptive-generation repair (2026-07-14).

The state-block / adult-layer / memory-block injections used to run in
llm_node, AFTER preemptive generation snapshots the chat context — every
user turn logged "chat context changed after on_user_turn_completed"
(13/session) and the preemptive LLM run was discarded: double LLM cost.

The injections now run in on_user_turn_completed against BOTH the turn
context and the persistent agent context, with stable item ids and
rewrite-only-on-change, so livekit 1.6.6's equivalence check
(ChatContext.is_equivalent: item ids + content) validates the preemptive
run whenever game state held still across the turn boundary. These tests
drive that exact check.

This file imports lily_agent (and therefore livekit) — same boundary note
as test_award_gate.py.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from livekit.agents.llm import ChatContext

import lily_memory
from lily_agent import (
    _ADULT_LAYER_MARKER,
    _STATE_BLOCK_MARKER,
    LilyAgent,
    LilyGame,
    _message_text,
    lily_temporal_context,
)


class _FakeSk:
    def __init__(self, mode: str = "general") -> None:
        self.mode = mode


class _FakeGame:
    def __init__(self) -> None:
        self.sk = _FakeSk()
        self.memory_block = ""
        self.state_text = "scores: none yet"

    def build_state_block(self, *, now=None) -> str:
        return f"{_STATE_BLOCK_MARKER}\n{self.state_text}"

    def build_state_block_split(self, *, now=None):
        # Mirrors the real split: stable = the marker block; the volatile
        # tail only ever reaches per-generation copies.
        return self.build_state_block(now=now), "volatile: tail"

    def note_user_turn(self) -> None:
        # WS-3 cut-recovery: on_user_turn_completed stamps user-turn recency
        # here so a real barge stands the auto-resume watchdog down.
        pass


def _make_agent() -> tuple[LilyAgent, _FakeGame]:
    agent = LilyAgent.__new__(LilyAgent)  # sidestep heavy livekit init
    game = _FakeGame()
    agent._game = game
    agent._chat_ctx = ChatContext.empty()
    return agent, game


def _state_items(ctx: ChatContext) -> list:
    return [
        m for m in ctx.items
        if getattr(m, "role", None) == "system"
        and _STATE_BLOCK_MARKER in _message_text(m)
    ]


def _run_hook(agent: LilyAgent, turn_ctx: ChatContext) -> None:
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(agent.on_user_turn_completed(turn_ctx, None))
    finally:
        loop.close()


# -- state block: replace-then-append with stable identity -------------------------

def test_state_block_appended_with_stable_id():
    agent, game = _make_agent()
    ctx = ChatContext.empty()
    agent._apply_context_blocks(ctx)
    (state,) = _state_items(ctx)
    assert state.id == LilyAgent._CTX_ID_STATE
    assert game.state_text in _message_text(state)
    assert ctx.items[-1] is state


def test_unchanged_state_leaves_items_untouched():
    # THE preemptive-validity property: an unchanged state block must not
    # be rewritten (same object, same id, same content, same position).
    agent, _ = _make_agent()
    ctx = ChatContext.empty()
    agent._apply_context_blocks(ctx)
    before = list(ctx.items)
    agent._apply_context_blocks(ctx)
    assert list(ctx.items) == before
    assert ctx.items[-1] is before[-1]


def test_changed_state_replaced_then_appended():
    agent, game = _make_agent()
    ctx = ChatContext.empty()
    agent._apply_context_blocks(ctx)
    game.state_text = "scores: Dave 1"
    ctx.add_message(role="user", content=["what's the score?"])
    agent._apply_context_blocks(ctx)
    states = _state_items(ctx)
    assert len(states) == 1  # replace, never accumulate
    assert "Dave 1" in _message_text(states[0])
    assert ctx.items[-1] is states[0]  # then append fresh at the end
    assert states[0].id == LilyAgent._CTX_ID_STATE  # id stays stable


def test_temporal_context_carries_utc_and_session_elapsed():
    line = lily_temporal_context(1_725_689_600, now=1_725_693_261)
    assert "current UTC 2024-09-07T07:14:21Z" in line
    assert "session elapsed 01:01:01 (3661s)" in line
    assert "do not recite" in line


# -- adult layer: add/remove on the sticky flag --------------------------------------

def test_adult_layer_always_present():
    # Unified adult deck (content-mode gate removed): the adult layer is
    # always injected and never removed.
    agent, game = _make_agent()
    ctx = ChatContext.empty()
    agent._apply_context_blocks(ctx)
    assert any(_ADULT_LAYER_MARKER in _message_text(m) for m in ctx.items)
    assert _ADULT_LAYER_MARKER in _message_text(ctx.items[0])
    # Idempotent — re-applying does not duplicate the layer:
    agent._apply_context_blocks(ctx)
    assert sum(
        _ADULT_LAYER_MARKER in _message_text(m) for m in ctx.items
    ) == 1


# -- memory block: once ---------------------------------------------------------------

def test_memory_block_injected_once():
    agent, game = _make_agent()
    game.memory_block = f"{lily_memory.MEMORY_BLOCK_MARKER}\nlast game: Dave won"
    ctx = ChatContext.empty()
    agent._apply_context_blocks(ctx)
    agent._apply_context_blocks(ctx)
    assert sum(
        lily_memory.MEMORY_BLOCK_MARKER in _message_text(m) for m in ctx.items
    ) == 1


# -- the hook + the 1.6.6 equivalence check --------------------------------------------

def test_hook_applies_to_turn_ctx_and_persistent_ctx():
    agent, game = _make_agent()
    turn_ctx = agent._chat_ctx.copy()
    _run_hook(agent, turn_ctx)
    assert _state_items(turn_ctx), "turn context must see the state block"
    assert _state_items(agent._chat_ctx), (
        "persistent context must carry the block so the next preemptive "
        "snapshot sees it"
    )


def test_preemptive_equivalence_holds_when_state_unchanged():
    # Simulates 1.6.6 agent_activity: preemptive snapshots agent.chat_ctx
    # DURING user speech; at end of turn the hook runs on a fresh copy and
    # the framework compares snapshot.is_equivalent(turn_ctx).
    agent, _ = _make_agent()
    _run_hook(agent, agent._chat_ctx.copy())  # a prior turn populated the blocks

    preemptive_snapshot = agent._chat_ctx.copy()  # preemptive fires
    turn_ctx = agent._chat_ctx.copy()             # end of turn
    _run_hook(agent, turn_ctx)                    # hook runs; state unchanged
    assert preemptive_snapshot.is_equivalent(turn_ctx), (
        "unchanged game state must validate the preemptive run"
    )


def test_preemptive_equivalence_breaks_honestly_when_state_changed():
    agent, game = _make_agent()
    _run_hook(agent, agent._chat_ctx.copy())

    preemptive_snapshot = agent._chat_ctx.copy()
    game.state_text = "scores: Dave 2, window OPEN"  # game moved mid-turn
    turn_ctx = agent._chat_ctx.copy()
    _run_hook(agent, turn_ctx)
    assert not preemptive_snapshot.is_equivalent(turn_ctx), (
        "a changed state block is genuinely new context — the preemptive "
        "run saw stale state and must be discarded"
    )


def test_hook_failure_never_eats_the_turn():
    agent, game = _make_agent()
    game.build_state_block = None  # force a TypeError inside the injection
    _run_hook(agent, ChatContext.empty())  # must not raise


# -- per-turn preemptive control ---------------------------------------------------------

def test_set_preemptive_generation_flips_live_turn_handling():
    agent, _ = _make_agent()
    agent._turn_handling = {}
    agent.set_preemptive_generation(False)
    assert agent._turn_handling["preemptive_generation"]["enabled"] is False
    agent.set_preemptive_generation(True)
    assert agent._turn_handling["preemptive_generation"]["enabled"] is True


class _FakeSession:
    def __init__(self) -> None:
        self.instructions: list[str] = []

    def generate_reply(self, instructions: str) -> None:
        self.instructions.append(instructions)


class _FakePreemptiveAgent:
    def __init__(self) -> None:
        self.calls: list[bool] = []

    def set_preemptive_generation(self, enabled: bool) -> None:
        self.calls.append(enabled)


def _make_game_for_replies() -> LilyGame:
    game = LilyGame.bare()
    game.session = _FakeSession()
    game.agent = _FakePreemptiveAgent()
    game._preemptive_paused = False
    return game


def test_instructed_reply_pauses_preemptive_and_speaks():
    game = _make_game_for_replies()
    game.instructed_reply("announce the reveal")
    assert game.session.instructions == ["announce the reveal"]
    assert game.agent.calls == [False]
    assert game._preemptive_paused is True


def test_resume_preemptive_on_playout_completion():
    game = _make_game_for_replies()
    game.instructed_reply("steal window")
    game._resume_preemptive()  # on_agent_speech_finished path
    assert game.agent.calls == [False, True]
    assert game._preemptive_paused is False
    # Idempotent when nothing is paused:
    game._resume_preemptive()
    assert game.agent.calls == [False, True]


def test_instructed_reply_without_session_is_noop():
    game = LilyGame.bare()
    game.session = None
    game.agent = _FakePreemptiveAgent()
    game._preemptive_paused = False
    game.instructed_reply("anything")
    assert game.agent.calls == []
