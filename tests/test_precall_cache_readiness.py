"""Pre-call readiness (WO-LILY-HOTFIX-007): the cache/preemptive
preconditions proven across a scripted game, offline.

These are the two claims a live call bets on, checked end-to-end through
the REAL hooks (real ChatContext, real _apply_context_blocks, real
_trim_history, the framework's real is_equivalent) with zero network:

  1. PREFIX STABILITY — the system-item prefix (instructions + injected
     blocks) is byte-identical across consecutive quiet turns. This is
     the precondition for Grok's prefix cache; if it churns here, the
     live hit rate is zero no matter what the provider does.
  2. PREEMPTIVE VALIDITY — the framework's own ChatContext.is_equivalent
     passes between the pre-turn snapshot and the committed context on
     ordinary turns, INCLUDING the killer churn case (an utterance
     landing as an answer candidate mid-window) and the turn after a
     history trim. Equivalence passing == the speculative reply is USED;
     every failure here would have been a doubled LLM call live.

A live-fire complement (real Grok, real cached_tokens) lives in
.github/workflows/cache-canary.yml — CI has the key, this sandbox
doesn't.
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
    game.sk = LilyScorekeeper("precall")
    game.sk.bind_speaker("S1", "Rami")
    game.session_started_at = 0.0
    game.availability_flags = None
    game.promoted_categories = []
    game.armed_question = None
    game.game_started = True
    game.game_over = False
    game.next_question = None
    game.memory_block = "[RETURNING TABLE]\nRami — 18 games on this device."
    game._pending_unbound_award = None
    game._delivery_stop_sticky = False
    game.eliminated = []
    game.forget_state = None
    game.prefs = {}
    return game


def _agent(game) -> LilyAgent:
    agent = LilyAgent.__new__(LilyAgent)
    agent._game = game
    return agent


def _system_prefix(ctx) -> list[str]:
    """Every system item's text, in order — the cacheable prefix shape."""
    out = []
    for item in ctx.items:
        if getattr(item, "role", None) == "system":
            out.append("\n".join(str(c) for c in item.content))
    return out


def _turn(agent, ctx, text, now) -> None:
    """One committed user turn through the real injection path."""
    ctx.add_message(role="user", content=text)
    agent._trim_history(ctx)
    agent._apply_context_blocks(ctx, now=now)
    ctx.add_message(role="assistant", content=f"(reply to: {text[:24]})")


def test_instructions_survive_context_block_injection():
    """THE PROMPT-EVICTION REGRESSION (found by this file's first run,
    2026-08-09). 59c437b added the literal "[GAME STATE]" and
    "[RETURNING TABLE]" strings to lily_system.txt; the marker-text scans
    in _apply_context_blocks then matched the INSTRUCTIONS item and
    popped the entire persona/rules prompt on the first user turn of
    every session (and skipped injecting the memory block). The framework
    inserts instructions ONCE at activity start (plain-str instructions
    are never re-added per generation), so every user-turn generation of
    every 2026-08-09 call — including lily-A070E8 — ran with NO system
    prompt. Production-exact sequence pinned here."""
    from livekit.agents.voice.generation import (
        INSTRUCTIONS_MESSAGE_ID,
        update_instructions,
    )

    game = _game()
    agent = _agent(game)
    ctx = ChatContext.empty()
    update_instructions(
        ctx, instructions=LILY_SYSTEM_PROMPT, add_if_missing=True
    )
    ctx.add_message(role="user", content="hey Lily")
    agent._apply_context_blocks(ctx, now=1_000.0)
    ids = [getattr(m, "id", None) for m in ctx.items]
    assert INSTRUCTIONS_MESSAGE_ID in ids, (
        "the system prompt was evicted by a block scan — every generation "
        "this session would run without Lily's persona and rules"
    )
    assert ctx.items[0].id == INSTRUCTIONS_MESSAGE_ID  # still the prefix
    # And the memory block actually injected (the marker-in-prompt false
    # positive used to skip it).
    texts = [str(m.content) for m in ctx.items]
    assert any("[RETURNING TABLE]" in t for t in texts)


def test_system_prefix_is_byte_stable_across_quiet_turns():
    game = _game()
    agent = _agent(game)
    ctx = ChatContext.empty()
    ctx.add_message(role="system", content=LILY_SYSTEM_PROMPT)
    _turn(agent, ctx, "hey Lily, warm us up", 1_000.0)
    prefix_a = _system_prefix(ctx)
    for i in range(6):
        _turn(agent, ctx, f"small talk {i}", 1_001.0 + i)
    assert _system_prefix(ctx) == prefix_a


def test_preemptive_equivalence_survives_the_killer_churn():
    """The exact sequence that used to invalidate every in-game turn:
    window open, an utterance lands as an answer candidate, next turn
    commits. Snapshot-vs-committed equivalence must PASS."""
    game = _game()
    game.sk.start_question({
        "prompt": "Which planet has the shortest day?",
        "canonical_answer": "Jupiter",
        "acceptable_answers": ["jupiter"],
    })
    game.sk.open_answer_window(duration=15.0)
    agent = _agent(game)
    ctx = ChatContext.empty()
    ctx.add_message(role="system", content=LILY_SYSTEM_PROMPT)
    _turn(agent, ctx, "read it again?", 1_000.0)

    # Preemptive snapshot is taken from the current ctx state...
    snapshot = ctx.copy()
    # ...then the candidate lands (volatile churn) before commit.
    game.sk.answer_candidates["S1"] = {
        "player": "Rami", "speaker_label": "S1",
        "text": "Jupiter!", "segment_start_time": 2.0,
    }
    agent._apply_context_blocks(ctx, now=1_002.0)
    assert snapshot.is_equivalent(ctx), (
        "candidate landing invalidated the context — preemptive would be "
        "discarded on every answer turn live"
    )


def test_equivalence_recovers_the_turn_after_a_trim(monkeypatch):
    monkeypatch.setenv("LILY_HISTORY_TRIM_HIGH", "40")
    monkeypatch.setenv("LILY_HISTORY_TRIM_LOW", "20")
    game = _game()
    agent = _agent(game)
    ctx = ChatContext.empty()
    ctx.add_message(role="system", content=LILY_SYSTEM_PROMPT)
    for i in range(25):
        _turn(agent, ctx, f"turn {i}", 1_000.0 + i)
    assert len(ctx.items) < 40  # the trim fired crossing the watermark
    # The trim turn itself invalidates (accepted, counted by Y2); the
    # NEXT quiet turn must be equivalent again.
    snapshot = ctx.copy()
    agent._apply_context_blocks(ctx, now=2_000.0)
    assert snapshot.is_equivalent(ctx)


def test_thirty_quiet_turns_zero_would_be_invalidations():
    """The headline number, simulated: across 30 quiet committed turns,
    how many would have discarded a speculative reply? Must be zero."""
    game = _game()
    agent = _agent(game)
    ctx = ChatContext.empty()
    ctx.add_message(role="system", content=LILY_SYSTEM_PROMPT)
    _turn(agent, ctx, "warmup", 999.0)
    would_invalidate = 0
    for i in range(30):
        snapshot = ctx.copy()
        _turn(agent, ctx, f"chatter {i}", 1_000.0 + i)
        # Committed ctx gained the user+assistant turns; equivalence is
        # judged on the shared prefix shape the framework compares —
        # strip the two new tail messages for the check.
        committed = ctx.copy()
        committed.items[:] = committed.items[:-2]
        if not snapshot.is_equivalent(committed):
            would_invalidate += 1
    assert would_invalidate == 0
