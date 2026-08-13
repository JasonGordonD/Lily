"""Y3 (WO-LILY-HOTFIX-007) — conversation history is bounded, pinned.

Archaeology: nothing ever trimmed the chat context — a long session grew
the prompt without limit (live: 134,357 input tokens on a trivia game;
operator: "it's a fucking trivia agent... why is it 115k?"). The
framework ships the mechanism (ChatContext.truncate); Lily's Y3 is a
WIRE to it with hysteresis watermarks, plus the re-insertion of the
injected system blocks that _apply_context_blocks' own comment promised
("re-inserted if history trimming ever drops it").

Hysteresis is the cache story: a trim slides the provider's cacheable
prefix, so it must fire rarely in big steps, never per turn.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_config
from livekit.agents.llm import ChatContext

from lily_agent import LILY_SYSTEM_PROMPT, LilyAgent, LilyGame, _STATE_BLOCK_MARKER
from lily_scorekeeper import LilyScorekeeper


def _game() -> LilyGame:
    game = LilyGame.bare()
    game.sk = LilyScorekeeper("y3-window")
    game.session_started_at = 0.0
    game.availability_flags = None
    game.promoted_categories = []
    game.armed_question = None
    game.game_started = True
    game.game_over = False
    game.next_question = None
    game.memory_block = "[RETURNING TABLE]\nRami has played before."
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


def _ctx(n_turns: int) -> ChatContext:
    """A real ChatContext: instructions + n_turns alternating messages."""
    ctx = ChatContext.empty()
    ctx.add_message(role="system", content=LILY_SYSTEM_PROMPT[:200])
    for i in range(n_turns):
        role = "user" if i % 2 == 0 else "assistant"
        ctx.add_message(role=role, content=f"turn {i}")
    return ctx


def test_below_the_high_watermark_is_byte_stable(monkeypatch):
    """No churn under the watermark — an untouched context is what keeps
    the cacheable prefix cacheable."""
    monkeypatch.setenv("LILY_HISTORY_TRIM_HIGH", "120")
    ctx = _ctx(80)
    before = [id(item) for item in ctx.items]
    _agent(_game())._trim_history(ctx)
    assert [id(item) for item in ctx.items] == before


def test_crossing_high_trims_to_low_and_keeps_instructions(monkeypatch):
    monkeypatch.setenv("LILY_HISTORY_TRIM_HIGH", "120")
    monkeypatch.setenv("LILY_HISTORY_TRIM_LOW", "60")
    ctx = _ctx(140)
    _agent(_game())._trim_history(ctx)
    # Framework truncate keeps the tail plus re-adds the instruction item.
    assert len(ctx.items) <= 61
    assert ctx.items[0].role == "system"
    # The newest turn survives; the oldest is gone.
    texts = [str(item.content) for item in ctx.items]
    assert any("turn 139" in t for t in texts)
    assert not any("'turn 0'" in t for t in texts)


def test_blocks_reinserted_after_a_trim(monkeypatch):
    """The promise in _apply_context_blocks' comment, proven: memory and
    state blocks dropped by a trim come back on the very next injection."""
    monkeypatch.setenv("LILY_HISTORY_TRIM_HIGH", "50")
    monkeypatch.setenv("LILY_HISTORY_TRIM_LOW", "20")
    game = _game()
    agent = _agent(game)
    ctx = _ctx(10)
    agent._apply_context_blocks(ctx, now=1_000.0)
    texts = [str(item.content) for item in ctx.items]
    assert any("[RETURNING TABLE]" in t for t in texts)
    # Grow past the watermark, trim, re-inject — the Y3 hook order.
    for i in range(60):
        ctx.add_message(role="user", content=f"later turn {i}")
    agent._trim_history(ctx)
    agent._apply_context_blocks(ctx, now=1_001.0)
    texts = [str(item.content) for item in ctx.items]
    assert any("[RETURNING TABLE]" in t for t in texts)
    assert any(_STATE_BLOCK_MARKER in t for t in texts)


def test_zero_high_disables_trimming(monkeypatch):
    monkeypatch.setenv("LILY_HISTORY_TRIM_HIGH", "0")
    ctx = _ctx(300)
    _agent(_game())._trim_history(ctx)
    assert len(ctx.items) == 301


def test_watermark_floors():
    assert lily_config.history_trim_high() >= 0
    assert lily_config.history_trim_low() >= 10


def test_trim_runs_before_block_injection_in_the_hook():
    """Source-order pin (same idiom as the watchdog pin): trim precedes
    _apply_context_blocks inside on_user_turn_completed, so a trim can
    never drop a block that was injected the same turn."""
    import inspect

    src = inspect.getsource(LilyAgent.on_user_turn_completed)
    assert src.index("_trim_history(turn_ctx)") \
        < src.index("_trim_history(self._chat_ctx)") \
        < src.index("_apply_context_blocks(")
