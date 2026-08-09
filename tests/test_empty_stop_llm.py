"""Empty STOP intercept (post PR #12 residual P0) + voice-inventory anchors.

LLM FinishReason.STOP with no text and no tools must not reach TTS as
silence on lobby/banter turns. Pure helpers are covered here; the
llm_node guard is exercised with a stubbed Agent.default.llm_node.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from livekit.agents import APIConnectionError, APIStatusError

import lily_agent
from lily_agent import (
    LilyAgent,
    lily_is_prohibited_content_error,
    lily_llm_chunk_signal,
    lily_llm_stream_is_empty_stop,
)


def test_chunk_signal_counts_plain_text():
    assert lily_llm_chunk_signal("  hi  ") == (2, 0)
    assert lily_llm_chunk_signal("   ") == (0, 0)
    assert lily_llm_chunk_signal(None) == (0, 0)


def test_chunk_signal_counts_chat_chunk_delta():
    delta = SimpleNamespace(content="Jupiter", tool_calls=[])
    chunk = SimpleNamespace(delta=delta)
    assert lily_llm_chunk_signal(chunk) == (7, 0)


def test_chunk_signal_counts_tool_calls_without_text():
    tool = SimpleNamespace(name="lily_bind_speaker")
    delta = SimpleNamespace(content="", tool_calls=[tool])
    chunk = SimpleNamespace(delta=delta)
    assert lily_llm_chunk_signal(chunk) == (0, 1)
    assert not lily_llm_stream_is_empty_stop(0, 1)


def test_empty_stop_predicate():
    assert lily_llm_stream_is_empty_stop(0, 0) is True
    assert lily_llm_stream_is_empty_stop(1, 0) is False
    assert lily_llm_stream_is_empty_stop(0, 1) is False


def _prohibited_error():
    return APIStatusError(
        '{"block_reason":"PROHIBITED_CONTENT","safety_ratings":null}',
        status_code=-1,
        retryable=False,
        request_id="req-prohibited-1",
    )


def test_prohibited_content_predicate_is_specific():
    assert lily_is_prohibited_content_error(_prohibited_error())
    assert not lily_is_prohibited_content_error(
        APIStatusError("rate limited", status_code=429)
    )


def _run(coro):
    return asyncio.run(coro)


def test_llm_empty_stop_retries_then_raises(monkeypatch):
    """Lobby/organic path: two empty streams → APIConnectionError (fail
    closed; no silent TTS turn)."""
    calls = {"n": 0}

    async def _empty_default(agent, chat_ctx, tools, model_settings):
        calls["n"] += 1
        if False:  # pragma: no cover — keep async generator shape
            yield "x"

    monkeypatch.setattr(LilyAgent.default, "llm_node", _empty_default)

    agent = LilyAgent.__new__(LilyAgent)
    game = SimpleNamespace(
        sk=SimpleNamespace(session_id="lily-empty", answer_window_open=False),
        game_started=False,
        armed_question=None,
        publish_attributes_nowait=lambda: None,
        expect_delivery=lambda: None,
        rendered_armed_question=lambda: "",
        # No opener recover surface — schedule is a no-op; raise still fires.
        _empty_stop_lobby_recover_count=0,
        reconnected=False,
    )
    agent._game = game
    object.__setattr__(agent, "_llm", None)
    agent._apply_context_blocks = lambda ctx: None
    agent._thinking_level_for_turn = lambda ctx: "low"

    async def _drain():
        out = []
        with pytest.raises(APIConnectionError):
            async for chunk in agent.llm_node(None, [], None):
                out.append(chunk)
        return out

    assert _run(_drain()) == []
    assert calls["n"] == 2


def test_prohibited_keyed_delivery_bypasses_model_with_sheet(monkeypatch, caplog):
    calls = {"n": 0}

    async def _blocked(agent, chat_ctx, tools, model_settings):
        calls["n"] += 1
        raise _prohibited_error()
        yield  # pragma: no cover

    monkeypatch.setattr(LilyAgent.default, "llm_node", _blocked)
    agent = LilyAgent.__new__(LilyAgent)
    expect = {"called": False}
    game = SimpleNamespace(
        sk=SimpleNamespace(
            session_id="lily-blocked-sheet",
            question_number=4,
            answer_window_open=False,
        ),
        game_started=True,
        armed_question={
            "id": "q_8291",
            "category": "academic",
            "prompt": "Who wrote Frankenstein?",
        },
        _pending_delivery_qnum=4,
        _delivery_stop_sticky=False,
        publish_attributes_nowait=lambda: None,
        expect_delivery=lambda: expect.__setitem__("called", True),
        rendered_armed_question=lambda: "Who wrote Frankenstein?",
    )
    agent._game = game
    object.__setattr__(agent, "_llm", None)
    agent._apply_context_blocks = lambda ctx: None
    agent._thinking_level_for_turn = lambda ctx: "low"

    async def _drain():
        return [chunk async for chunk in agent.llm_node(None, [], None)]

    with caplog.at_level("ERROR"):
        assert _run(_drain()) == ["Who wrote Frankenstein?"]
    assert calls["n"] == 1
    assert expect["called"] is True
    joined = "\n".join(record.message for record in caplog.records)
    assert "request_id=req-prohibited-1" in joined
    assert "q=q_8291" in joined
    assert "PROHIBITED_CONTENT_SHEET" in joined


def test_prohibited_conversation_fails_closed_without_retry(monkeypatch):
    calls = {"n": 0}

    async def _blocked(agent, chat_ctx, tools, model_settings):
        calls["n"] += 1
        raise _prohibited_error()
        yield  # pragma: no cover

    monkeypatch.setattr(LilyAgent.default, "llm_node", _blocked)
    agent = LilyAgent.__new__(LilyAgent)
    game = SimpleNamespace(
        sk=SimpleNamespace(
            session_id="lily-blocked-chat",
            question_number=0,
            answer_window_open=False,
        ),
        game_started=False,
        armed_question=None,
        _pending_delivery_qnum=None,
        _delivery_stop_sticky=False,
        publish_attributes_nowait=lambda: None,
    )
    agent._game = game
    object.__setattr__(agent, "_llm", None)
    agent._apply_context_blocks = lambda ctx: None
    agent._thinking_level_for_turn = lambda ctx: "low"

    async def _drain():
        with pytest.raises(APIStatusError):
            async for _ in agent.llm_node(None, [], None):
                pass

    _run(_drain())
    assert calls["n"] == 1


def test_lobby_empty_stop_schedules_one_greet_recover(monkeypatch):
    """F1: cold open empty STOP fail-closed re-dispatches session_greet once."""
    import lily_say_gate

    async def _empty_default(agent, chat_ctx, tools, model_settings):
        if False:  # pragma: no cover
            yield "x"

    monkeypatch.setattr(LilyAgent.default, "llm_node", _empty_default)

    agent = LilyAgent.__new__(LilyAgent)
    said = []
    registry = lily_say_gate.SpeechActRegistry()
    registry.claim("session_greet", owner="failed_speech")

    game = SimpleNamespace(
        sk=SimpleNamespace(session_id="lily-lobby-recover", answer_window_open=False),
        game_started=False,
        armed_question=None,
        reconnected=False,
        _empty_stop_lobby_recover_count=0,
        say_registry=registry,
        publish_attributes_nowait=lambda: None,
        expect_delivery=lambda: None,
        rendered_armed_question=lambda: "",
        greeting_instructions=lambda: "Hi, I'm Lily — you host trivia.",
        rejoin_instructions=lambda: "lost you a second",
        gated_say=lambda key, act, instructions, source: (
            said.append(
                {"key": key, "act": act, "instructions": instructions, "source": source}
            )
            or True
        ),
    )
    agent._game = game
    object.__setattr__(agent, "_llm", None)
    agent._apply_context_blocks = lambda ctx: None
    agent._thinking_level_for_turn = lambda ctx: "low"

    async def _scenario():
        with pytest.raises(APIConnectionError):
            async for _ in agent.llm_node(None, [], None):
                pass
        await asyncio.sleep(0.25)
        return said

    assert _run(_scenario()) == [
        {
            "key": "session_greet",
            "act": "greet",
            "instructions": "Hi, I'm Lily — you host trivia.",
            "source": "empty_stop_lobby_recover",
        }
    ]
    assert game._empty_stop_lobby_recover_count == 1
    assert registry.state("session_greet") is None  # released before re-dispatch


def test_lobby_empty_stop_recover_capped(monkeypatch):
    """Second empty STOP in the same lobby does not greet again."""
    import lily_say_gate

    async def _empty_default(agent, chat_ctx, tools, model_settings):
        if False:  # pragma: no cover
            yield "x"

    monkeypatch.setattr(LilyAgent.default, "llm_node", _empty_default)

    agent = LilyAgent.__new__(LilyAgent)
    said = []
    game = SimpleNamespace(
        sk=SimpleNamespace(session_id="lily-lobby-cap", answer_window_open=False),
        game_started=False,
        armed_question=None,
        reconnected=False,
        _empty_stop_lobby_recover_count=1,  # already recovered once
        say_registry=lily_say_gate.SpeechActRegistry(),
        publish_attributes_nowait=lambda: None,
        expect_delivery=lambda: None,
        rendered_armed_question=lambda: "",
        greeting_instructions=lambda: "Hi, I'm Lily — you host trivia.",
        gated_say=lambda *a, **k: said.append(True) or True,
    )
    agent._game = game
    object.__setattr__(agent, "_llm", None)
    agent._apply_context_blocks = lambda ctx: None
    agent._thinking_level_for_turn = lambda ctx: "low"

    async def _scenario():
        with pytest.raises(APIConnectionError):
            async for _ in agent.llm_node(None, [], None):
                pass
        await asyncio.sleep(0.25)
        return said

    assert _run(_scenario()) == []
    assert game._empty_stop_lobby_recover_count == 1


def test_lobby_empty_stop_skips_recover_after_confirmed_greet(monkeypatch):
    """Once she opened the night, later lobby empties must not re-greet."""
    import lily_say_gate

    async def _empty_default(agent, chat_ctx, tools, model_settings):
        if False:  # pragma: no cover
            yield "x"

    monkeypatch.setattr(LilyAgent.default, "llm_node", _empty_default)

    agent = LilyAgent.__new__(LilyAgent)
    said = []
    registry = lily_say_gate.SpeechActRegistry()
    registry.claim("session_greet", owner="ok")
    registry.confirm("session_greet")

    game = SimpleNamespace(
        sk=SimpleNamespace(session_id="lily-lobby-open", answer_window_open=False),
        game_started=False,
        armed_question=None,
        reconnected=False,
        _empty_stop_lobby_recover_count=0,
        say_registry=registry,
        publish_attributes_nowait=lambda: None,
        expect_delivery=lambda: None,
        rendered_armed_question=lambda: "",
        greeting_instructions=lambda: "Hi, I'm Lily — you host trivia.",
        gated_say=lambda *a, **k: said.append(True) or True,
    )
    agent._game = game
    object.__setattr__(agent, "_llm", None)
    agent._apply_context_blocks = lambda ctx: None
    agent._thinking_level_for_turn = lambda ctx: "low"

    async def _scenario():
        with pytest.raises(APIConnectionError):
            async for _ in agent.llm_node(None, [], None):
                pass
        await asyncio.sleep(0.25)
        return said

    assert _run(_scenario()) == []
    assert game._empty_stop_lobby_recover_count == 0


def test_llm_empty_stop_forces_armed_sheet(monkeypatch):
    """Game delivery path: after two empties, yield the deterministic sheet."""
    calls = {"n": 0}

    async def _empty_default(agent, chat_ctx, tools, model_settings):
        calls["n"] += 1
        if False:  # pragma: no cover
            yield "x"

    monkeypatch.setattr(LilyAgent.default, "llm_node", _empty_default)

    agent = LilyAgent.__new__(LilyAgent)
    expect = {"called": False}
    game = SimpleNamespace(
        sk=SimpleNamespace(session_id="lily-sheet", answer_window_open=False),
        game_started=True,
        armed_question={
            "prompt": "Name the largest planet.",
            "canonical_answer": "Jupiter",
        },
        publish_attributes_nowait=lambda: None,
        expect_delivery=lambda: expect.__setitem__("called", True),
        rendered_armed_question=lambda: "Name the largest planet.",
    )
    agent._game = game
    object.__setattr__(agent, "_llm", None)
    agent._apply_context_blocks = lambda ctx: None
    agent._thinking_level_for_turn = lambda ctx: "low"

    async def _drain():
        return [chunk async for chunk in agent.llm_node(None, [], None)]

    assert _run(_drain()) == ["Name the largest planet."]
    assert calls["n"] == 2
    assert expect["called"] is True


def test_llm_empty_stop_recovers_on_retry(monkeypatch):
    calls = {"n": 0}

    async def _flaky_default(agent, chat_ctx, tools, model_settings):
        calls["n"] += 1
        if calls["n"] == 1:
            return
            yield  # pragma: no cover
        yield "Hey table — who's ready?"

    monkeypatch.setattr(LilyAgent.default, "llm_node", _flaky_default)

    agent = LilyAgent.__new__(LilyAgent)
    game = SimpleNamespace(
        sk=SimpleNamespace(session_id="lily-recover", answer_window_open=False),
        game_started=False,
        armed_question=None,
        publish_attributes_nowait=lambda: None,
        expect_delivery=lambda: None,
        rendered_armed_question=lambda: "",
    )
    agent._game = game
    object.__setattr__(agent, "_llm", None)
    agent._apply_context_blocks = lambda ctx: None
    agent._thinking_level_for_turn = lambda ctx: "low"

    async def _drain():
        return [chunk async for chunk in agent.llm_node(None, [], None)]

    assert _run(_drain()) == ["Hey table — who's ready?"]
    assert calls["n"] == 2


def test_voice_inventory_freeze_doc_exists():
    """Extraction must not start without the freeze catalog on disk."""
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "docs" / "voice_inventory.md"
    text = path.read_text(encoding="utf-8")
    assert "session_greet" in text
    assert "q_{N}_delivery" in text
    assert "Audeering" in text
    assert "zero string" in text.lower()
