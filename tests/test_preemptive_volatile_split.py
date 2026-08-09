"""P2 volatile-tail split — preemptive generation back ON for live games.

G1 turned preemptive OFF for the whole live game because the state block
honestly changed on nearly every turn — the just-heard utterance lands as
an answer-candidate line, the answer window flips on the clock, and the
temporal-context line is a literal clock — so 1.6.x's equivalence check
rightly discarded almost every speculative run: double LLM cost, zero
win.

The split: those per-turn volatile lines now ride a SEPARATE system item
injected only on per-generation copies (llm_node's
include_volatile=True), which the equivalence check never sees. The
stable remainder — injected in on_user_turn_completed under the stable
item id — changes only when the game genuinely moves. An ordinary turn
boundary therefore keeps the speculative run valid, and live games keep
preemptive ON (LILY_LIVE_PREEMPTIVE=false restores G1).

Same import boundary note as test_hotfix006_transitions.py.
"""

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_config
import lily_say_gate
from lily_agent import LilyAgent, LilyGame, _STATE_BLOCK_MARKER
from lily_scorekeeper import LilyScorekeeper


def _sk() -> LilyScorekeeper:
    sk = LilyScorekeeper("volatile-split")
    sk.bind_speaker("S1", "Rami")
    sk.start_question({
        "prompt": "Which planet has the shortest day?",
        "canonical_answer": "Jupiter",
        "acceptable_answers": ["jupiter"],
    })
    return sk


# ── scorekeeper split ────────────────────────────────────────────────────


def test_full_block_recomposes_from_stable_plus_volatile():
    sk = _sk()
    full = sk.build_state_block()
    stable = sk.build_state_block_stable()
    volatile = sk.volatile_state_lines()
    assert "answer_window=" in full
    assert all(line in full for line in volatile)
    # Stable carries the game truth but none of the volatile tail.
    assert "[GAME STATE]" in stable
    assert "answer_window=" not in stable
    assert "answered:" not in stable


def test_a_landing_candidate_changes_only_the_volatile_tail():
    """THE point of the split: the exact churn that forced preemptive OFF
    (an utterance landing as a candidate) must leave the stable block
    byte-identical, so the equivalence check passes."""
    sk = _sk()
    sk.open_answer_window(duration=15.0)
    before_stable = sk.build_state_block_stable()
    before_volatile = sk.volatile_state_lines()
    sk.answer_candidates["S1"] = {
        "player": "Rami", "speaker_label": "S1",
        "text": "It's Jupiter!", "segment_start_time": 1.0,
    }
    assert sk.build_state_block_stable() == before_stable
    assert sk.volatile_state_lines() != before_volatile


# ── agent-level split + injection ────────────────────────────────────────


def _game() -> LilyGame:
    game = LilyGame.__new__(LilyGame)
    game.sk = _sk()
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


def _agent(game) -> LilyAgent:
    agent = LilyAgent.__new__(LilyAgent)
    agent._game = game
    return agent


def _fake_ctx(items=None):
    return types.SimpleNamespace(items=list(items or []))


def test_agent_split_puts_clock_and_window_in_the_volatile_tail():
    stable, volatile = _game().build_state_block_split(now=1_000.0)
    assert "[GAME STATE]" in stable
    assert "answer_window=" not in stable
    assert "answer_window=" in volatile
    # The temporal clock line — a guaranteed every-render invalidator —
    # lives in the tail, never in the equivalence-visible block.
    assert "session age" in volatile or "utc" in volatile.lower()
    assert "utc" not in stable.lower()


def test_hook_injection_is_stable_only_and_generation_adds_the_tail():
    game = _game()
    agent = _agent(game)
    ctx = _fake_ctx()

    # Hook path (on_user_turn_completed): stable only.
    agent._apply_context_blocks(ctx, now=1_000.0)
    texts = ["\n".join(str(c) for c in m.content) for m in ctx.items]
    assert any(_STATE_BLOCK_MARKER in t for t in texts)
    assert not any("answer_window=" in t for t in texts)

    # Generation path (llm_node): the volatile tail appends.
    agent._apply_context_blocks(ctx, now=1_001.0, include_volatile=True)
    tail = ctx.items[-1]
    assert tail.id == LilyAgent._CTX_ID_STATE_VOLATILE
    tail_text = "\n".join(str(c) for c in tail.content)
    assert "answer_window=" in tail_text
    assert lily_say_gate.LILY_STATE_SENTINEL_OPEN in tail_text


def test_stable_item_survives_a_candidate_landing():
    """Equivalence-check survival, end to end: hook-inject, land a
    candidate, hook-inject again — the stable state item is untouched
    (same object, same text), so is_equivalent passes."""
    game = _game()
    game.sk.open_answer_window(duration=15.0)
    agent = _agent(game)
    ctx = _fake_ctx()
    agent._apply_context_blocks(ctx, now=1_000.0)
    stable_before = [
        m for m in ctx.items
        if getattr(m, "id", None) == LilyAgent._CTX_ID_STATE
    ]
    game.sk.answer_candidates["S1"] = {
        "player": "Rami", "speaker_label": "S1",
        "text": "It's Jupiter!", "segment_start_time": 1.0,
    }
    agent._apply_context_blocks(ctx, now=1_000.0)
    stable_after = [
        m for m in ctx.items
        if getattr(m, "id", None) == LilyAgent._CTX_ID_STATE
    ]
    assert stable_before and stable_after
    assert stable_before[0] is stable_after[0]


def test_generation_refreshes_the_volatile_tail_in_place():
    game = _game()
    game.sk.open_answer_window(duration=15.0)
    agent = _agent(game)
    ctx = _fake_ctx()
    agent._apply_context_blocks(ctx, now=1_000.0, include_volatile=True)
    game.sk.answer_candidates["S1"] = {
        "player": "Rami", "speaker_label": "S1",
        "text": "Saturn?", "segment_start_time": 2.0,
    }
    agent._apply_context_blocks(ctx, now=1_002.0, include_volatile=True)
    tails = [
        m for m in ctx.items
        if getattr(m, "id", None) == LilyAgent._CTX_ID_STATE_VOLATILE
    ]
    assert len(tails) == 1  # replaced, never stacked
    assert "Saturn?" in "\n".join(str(c) for c in tails[0].content)


# ── the preemptive flag itself ───────────────────────────────────────────


class _FlagAgent:
    def __init__(self):
        self.calls = []

    def set_preemptive_generation(self, enabled):
        self.calls.append(enabled)


def test_live_game_keeps_preemptive_on_by_default(monkeypatch):
    game = _game()
    game.agent = _FlagAgent()
    game.set_game_live_preemptive(True)
    assert game.agent.calls == [True]


def test_env_escape_restores_the_g1_behavior(monkeypatch):
    monkeypatch.setattr(lily_config, "live_preemptive_enabled", lambda: False)
    game = _game()
    game.agent = _FlagAgent()
    game.set_game_live_preemptive(True)
    assert game.agent.calls == [False]
    game.set_game_live_preemptive(False)  # lobby/finale: always on
    assert game.agent.calls == [False, True]
