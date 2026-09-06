"""Composition review of main c1ff3f6 (WO-LILY-STREAMING-REASONING-001 on
HOTFIX-MC-LETTER-A-001) — GO-WITH-FIXES, applied on integ/next.

P1-1  The supply-recovery regeneration for a named topic still ran under a
      literal 20 s TOTAL wall (lily_supply._bank_to_supply) — the exact
      class the streaming WO retired; live authoring ttft is 20–39 s. The
      chain's own total budget is the bound.
P1-2  The Z2 de-escalated retry effort was "medium"; with the authoring
      tier now medium (operator ruling) the retry was identical to the
      failed draw. It is "low".
P2-1  The transport's TOTAL wall and its IDLE wall both recorded as
      `timeout`; the contract says `timeout` = idle wall. The total wall is
      now `timeout:total`.
P2-4  A provider-error raise mid-stream left the SSE generator suspended
      until garbage collection; it is closed before the raise propagates.
P2-5  A backchannel lead-in before a terminal letter ("Yeah, a.",
      "Oh really? A.") still did not bind (the letter parser needed a pick
      lead-in). It binds.
"""

import asyncio
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_config  # noqa: E402
import lily_evaluation  # noqa: E402
import lily_persistence  # noqa: E402
import lily_reasoning  # noqa: E402
from test_bind_dispute_p0 import _make_game  # noqa: E402
from test_w8_streaming_reasoning import (  # noqa: E402
    _BLANK, _Sb, _Session, _bound, _chat_delta, _run, _sse, transport,  # noqa: F401
)

_CHOICES = ["Jupiter", "Saturn", "Neptune", "Uranus"]


# -- P1-1 -------------------------------------------------------------------

def test_named_topic_regeneration_wall_is_the_chain_budget(monkeypatch):
    game = _make_game()
    game.supabase = object()
    game.game_started = True
    game._is_operator_category = lambda c: True
    game._category_for_round = lambda r: "Cape Cod"
    state = {"completed": False}

    class _Reasoning:
        async def prefetch_question(self, *a, **k):
            await asyncio.sleep(0.3)
            state["completed"] = True
            return None

    game.reasoning = _Reasoning()

    async def _dry_bank(*a, **k):
        return None

    monkeypatch.setattr(lily_persistence, "lily_fetch_bank_question", _dry_bank)
    monkeypatch.setattr(lily_config, "prefetch_total_budget_seconds", lambda: 0.05)

    async def go():
        t0 = time.monotonic()
        await game._bank_to_supply(trigger="review")
        return time.monotonic() - t0

    elapsed = asyncio.run(go())
    # The chain budget (0.05 s) bounded the regeneration: it was cancelled
    # before the 0.3 s author returned. Under the old literal 20 s wall the
    # author completes.
    assert state["completed"] is False
    assert elapsed < 0.25


# -- P1-2 -------------------------------------------------------------------

def test_deescalated_retry_effort_sits_below_the_configured_tier():
    game = _make_game()
    game.game_started = True
    game.game_over = False
    game._session_closed = False
    game.next_question = None
    game._supply_retry_attempts = 0
    game._supply_exhausted_notified = False
    game.start_prefetch = lambda: None
    game._prefetch_task = None

    async def _bank(trigger):
        return "supplied"

    game._bank_to_supply = _bank
    asyncio.run(game._recover_supply("review"))
    assert game._prefetch_effort_override == "low"
    assert game._prefetch_effort_override != lily_config.adult_reasoning_effort()


# -- P2-1 -------------------------------------------------------------------

def test_total_wall_is_labelled_distinctly_from_the_idle_wall(transport, monkeypatch):
    sb = _Sb()
    # Every chunk gap (0.15 s) is inside the idle wall (0.2 s), but the
    # total budget (0.3 s; the transport ceiling is max(idle, budget))
    # runs out during the third gap: the total wall fires, not the idle.
    monkeypatch.setattr(lily_config, "prefetch_timeout_seconds", lambda: 0.2)
    monkeypatch.setattr(lily_config, "prefetch_total_budget_seconds", lambda: 0.3)
    _Session.script = [
        (0.15, _chat_delta(content='{"a":')),
        (0.0, _BLANK),
        (0.15, _chat_delta(content='1')),
        (0.0, _BLANK),
        (0.15, _chat_delta(content='}', finish="stop")),
        (0.0, _BLANK),
    ]

    async def go():
        _bound(sb)
        try:
            await transport._generate_grok_json("p", max_tokens=10, model="grok-4.2")
        except asyncio.TimeoutError:
            return "raised"
        return "ok"

    assert _run(go()) == "raised"
    assert sb.rows and sb.rows[-1]["finish_reason"] == "timeout:total"

    # And the idle wall still says plain `timeout`.
    sb2 = _Sb()
    monkeypatch.setattr(lily_config, "prefetch_timeout_seconds", lambda: 0.1)
    monkeypatch.setattr(lily_config, "prefetch_total_budget_seconds", lambda: 5.0)
    _Session.script = [
        (0.0, _chat_delta(content='{"a":')),
        (0.0, _BLANK),
        (0.5, _chat_delta(content='1}', finish="stop")),
        (0.0, _BLANK),
    ]

    async def go2():
        _bound(sb2)
        try:
            await transport._generate_grok_json("p", max_tokens=10, model="grok-4.2")
        except asyncio.TimeoutError:
            return "raised"
        return "ok"

    assert _run(go2()) == "raised"
    assert sb2.rows and sb2.rows[-1]["finish_reason"] == "timeout"


# -- P2-4 -------------------------------------------------------------------

def test_sse_generator_is_closed_before_a_provider_error_propagates(transport, monkeypatch):
    sb = _Sb()
    closed = {"at_raise": None}
    real_iter = lily_reasoning._lily_iter_sse

    async def _tracking_iter(*a, **k):
        try:
            async for item in real_iter(*a, **k):
                yield item
        finally:
            closed["at_raise"] = True

    monkeypatch.setattr(lily_reasoning, "_lily_iter_sse", _tracking_iter)
    _Session.script = [
        (0.0, _chat_delta(content='{"a":')),
        (0.0, _BLANK),
        (0.0, _sse({"error": {"message": "overloaded"}})),
        (0.0, _BLANK),
        (0.0, _chat_delta(content='1}', finish="stop")),
        (0.0, _BLANK),
    ]

    async def go():
        _bound(sb)
        try:
            await transport._generate_grok_json("p", max_tokens=10, model="grok-4.2")
        except RuntimeError:
            # Observed synchronously with the raise — not at GC.
            return closed["at_raise"]
        return "no-raise"

    assert _run(go()) is True
    assert sb.rows and sb.rows[-1]["finish_reason"] == "error:stream"


# -- P2-5 -------------------------------------------------------------------

def test_backchannel_lead_in_before_a_letter_still_binds():
    q = {"choices": _CHOICES, "canonical_answer": "Jupiter"}
    for text, idx in (("Yeah, a.", 0), ("Oh really? A.", 0), ("um, b", 1), ("sure b", 1)):
        r = lily_evaluation.lily_tier1_evaluate_mc(text, _CHOICES, "Jupiter")
        assert (r["selected_index"], r["method"]) == (idx, "letter"), text
        assert lily_evaluation.lily_non_answer_utterance(text, q, ["Rami"]) is None, text
    # The conversational article stays unresolved.
    for text in ("it's a", "I think it's a", "is it a"):
        assert lily_evaluation.lily_mc_unresolved(text, q), text
