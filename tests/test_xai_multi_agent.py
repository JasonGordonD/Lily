"""Engineering Note 2026-08-07 — xAI multi-agent 400 fix.

grok-*-multi-agent rejects Chat Completions ("Multi Agent requests are not
allowed on chat completions") and rejects max_tokens; it speaks only the
Responses API. These pin the routing (which endpoint + body per model) and
both response parsers, so a slot-secret swap to the heavy tier works instead
of 400ing.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_reasoning as R


# -- model routing -------------------------------------------------------------

def test_multi_agent_detection():
    assert R._lily_is_multi_agent_model("grok-4.20-multi-agent") is True
    assert R._lily_is_multi_agent_model("grok-4.2-multi_agent") is True
    assert R._lily_is_multi_agent_model("GROK-4.20-MULTI-AGENT") is True
    assert R._lily_is_multi_agent_model("grok-4.2") is False
    assert R._lily_is_multi_agent_model("grok-4.5") is False
    assert R._lily_is_multi_agent_model(None) is False


# -- Responses API parser ------------------------------------------------------

def test_responses_parser_walks_output_message():
    data = {
        "id": "resp_1",
        "output": [
            {"type": "reasoning", "content": [{"type": "text", "text": "thinking"}]},
            {"type": "message", "role": "assistant",
             "content": [{"type": "output_text", "text": '{"q":"a"}'}]},
        ],
    }
    assert R._lily_extract_responses_text(data) == '{"q":"a"}'


def test_responses_parser_prefers_output_text_convenience():
    data = {"output_text": '{"q":"b"}', "output": []}
    assert R._lily_extract_responses_text(data) == '{"q":"b"}'


def test_responses_parser_strips_markdown_fences():
    data = {"output": [{"type": "message", "content": [
        {"type": "output_text", "text": '```json\n{"q":"c"}\n```'}]}]}
    assert R._lily_extract_responses_text(data) == '{"q":"c"}'


def test_responses_parser_skips_non_message_items():
    data = {"output": [{"type": "reasoning", "content": [
        {"type": "text", "text": "nope"}]}]}
    try:
        R._lily_extract_responses_text(data)
        assert False, "should raise on no message content"
    except RuntimeError:
        pass


# -- Chat Completions parser ---------------------------------------------------

def test_chat_parser_reads_choices():
    data = {"choices": [{"message": {"content": '{"q":"d"}'}}]}
    assert R._lily_extract_chat_text(data) == '{"q":"d"}'


def test_chat_parser_raises_on_malformed():
    try:
        R._lily_extract_chat_text({"nope": 1})
        assert False
    except RuntimeError:
        pass


# -- end-to-end request shaping (mocked HTTP) ---------------------------------

import asyncio
import lily_config


class _FakeResp:
    def __init__(self, payload):
        self.status = 200
        self._payload = payload
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def json(self): return self._payload
    async def text(self): return ""


class _FakeSession:
    """Captures the POST url + body; returns a canned payload per endpoint."""
    captured = {}
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    def post(self, url, json=None, headers=None, timeout=None):
        _FakeSession.captured = {"url": url, "body": json}
        if url.endswith("/responses"):
            return _FakeResp({"output": [{"type": "message", "content": [
                {"type": "output_text", "text": '{"ok":1}'}]}]})
        return _FakeResp({"choices": [{"message": {"content": '{"ok":1}'}}]})


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _call(monkeypatch, model):
    monkeypatch.setattr(lily_config, "xai_api_key", lambda: "k")
    monkeypatch.setattr(lily_config, "adult_reasoning_model", lambda: model)
    monkeypatch.setattr(lily_config, "adult_reasoning_effort", lambda: "high")
    monkeypatch.setattr(R.aiohttp, "ClientSession", lambda *a, **k: _FakeSession())
    r = R.LilyReasoning.__new__(R.LilyReasoning)
    text = _run(r._generate_grok_json(
        "make a question", system_instruction="be adult", max_tokens=800))
    return text, _FakeSession.captured


def test_multi_agent_routes_to_responses_no_max_tokens(monkeypatch):
    text, cap = _call(monkeypatch, "grok-4.20-multi-agent")
    assert text == '{"ok":1}'
    assert cap["url"] == "https://api.x.ai/v1/responses"
    assert "input" in cap["body"] and "messages" not in cap["body"]
    assert "max_tokens" not in cap["body"]  # unsupported on multi-agent
    assert cap["body"]["reasoning"] == {"effort": "high"}
    # system instruction rides input as a system turn
    assert cap["body"]["input"][0]["role"] == "system"


def test_base_tier_stays_on_chat_completions(monkeypatch):
    text, cap = _call(monkeypatch, "grok-4.2")
    assert text == '{"ok":1}'
    assert cap["url"] == "https://api.x.ai/v1/chat/completions"
    assert "messages" in cap["body"] and "input" not in cap["body"]
    assert cap["body"]["max_tokens"] == 800
    assert cap["body"]["response_format"] == {"type": "json_object"}
    assert cap["body"]["reasoning_effort"] == "high"
