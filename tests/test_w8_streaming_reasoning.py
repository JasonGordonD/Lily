"""WO-LILY-STREAMING-REASONING-001 — the question-reasoning transport STREAMS,
so the prefetch wall applies to idle time, not total generation.

Evidence (lily_llm_usage, purpose='reasoning', 2026-09-06): 16 of 16 rows
across six sessions ended at total_ms≈20001, ttft_ms null, finish_reason=
'cancelled' — the non-streaming POST answered only once generation was
done, and the 20s wall measured the whole answer. Operator ruling:
"medium effort now, streaming transport as the durable fix".

Behavior, not source text: a fake aiohttp session serves Server-Sent
Events off a scripted timeline (per-line delays), a fake supabase captures
the lily_llm_usage rows, and each contract below is driven through the
REAL transport (`LilyReasoning._generate_grok_json`) and, for the wall
semantics, the REAL prefetch chain (`prefetch_question`).

RED on main: the fake response deliberately has no usable `.json()` —
main's transport reads `await resp.json()` and never touches `.content`.
"""

import asyncio
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import lily_config
import lily_metrics
import lily_persistence
import lily_reasoning
from lily_metrics import LilyMetricsCollector
from lily_scorekeeper import LilyScorekeeper


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _Sb:
    """Minimal supabase fake: captures every lily_llm_usage insert."""

    def __init__(self):
        self.rows = []

    def table(self, name):
        assert name == "lily_llm_usage"
        sb = self

        class _Q:
            def insert(self_q, row):
                self_q.row = row
                return self_q

            def execute(self_q):
                sb.rows.append(dict(self_q.row))
                return SimpleNamespace(data=[dict(self_q.row)])

        return _Q()


def _sse(obj) -> bytes:
    return b"data: " + json.dumps(obj).encode() + b"\n"


_BLANK = b"\n"
_DONE = b"data: [DONE]\n"


class _Content:
    """aiohttp StreamReader stand-in: a scripted timeline of
    (delay_seconds, line) pairs; exhausted -> b"" (EOF)."""

    def __init__(self, script):
        self._script = list(script)

    async def readline(self):
        if not self._script:
            return b""
        delay, line = self._script.pop(0)
        if delay:
            await asyncio.sleep(delay)
        return line


class _Resp:
    content_type = "text/event-stream"

    def __init__(self, script, status=200):
        self.status = status
        self.content = _Content(script)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def text(self):
        return "err"

    async def json(self, *a, **k):
        # The non-streaming read is GONE: a transport that still consumes
        # the body as one JSON document fails here (RED on main).
        raise AssertionError(
            "transport read the SSE body as one JSON document"
        )


class _Session:
    """Captures the POST; serves `script` (or `script_for(url)`)."""
    script = None
    status = 200
    captured = {}

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def post(self, url, json=None, headers=None, timeout=None):
        _Session.captured = {"url": url, "body": json, "timeout": timeout}
        script = _Session.script
        if callable(script):
            script = script(url)
        return _Resp(script, _Session.status)


async def _drain():
    me = asyncio.current_task()
    for _ in range(3):
        pending = [t for t in asyncio.all_tasks() if t is not me and not t.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await asyncio.sleep(0)


def _bound(sb):
    c = LilyMetricsCollector()
    c.bind_usage_context(supabase=sb, session_id="sess-w8", phase="lobby")
    lily_metrics.set_current_collector(c)
    return c


def _run(coro):
    async def _wrapped():
        try:
            return await coro
        finally:
            await _drain()
    try:
        return asyncio.run(_wrapped())
    finally:
        lily_metrics.set_current_collector(None)
        lily_persistence._llm_usage_absent_columns.clear()
        _Session.script = None
        _Session.status = 200


@pytest.fixture
def transport(monkeypatch):
    monkeypatch.setattr(lily_config, "xai_api_key", lambda: "k")
    monkeypatch.setattr(lily_reasoning.aiohttp, "ClientSession", _Session)
    monkeypatch.setattr(lily_config, "tavily_api_key", lambda: None)
    return lily_reasoning.LilyReasoning.__new__(lily_reasoning.LilyReasoning)


def _chat_delta(content=None, reasoning=None, finish=None):
    delta = {"role": "assistant"}
    if content is not None:
        delta["content"] = content
    if reasoning is not None:
        delta["reasoning_content"] = reasoning
    choice = {"index": 0, "delta": delta}
    if finish is not None:
        choice["finish_reason"] = finish
    return _sse({"object": "chat.completion.chunk", "choices": [choice]})


_QUESTION = (
    '{"prompt":"Q?","canonical_answer":"A","acceptable_answers":["A"],'
    '"verdict":"pass","reason":"ok"}'
)


def _chunked(text, n):
    return [text[i:i + n] for i in range(0, len(text), n)]


# ---------------------------------------------------------------------------
# 1. Chat-completions stream: ttft at the first CONTENT delta, total at the
#    end, usage from the terminal chunk, reasoning deltas excluded.
# ---------------------------------------------------------------------------

def test_chat_stream_ttft_at_first_content_total_at_end_usage_captured(transport):
    sb = _Sb()
    _Session.script = [
        (0.05, _chat_delta()),                       # role-only, no content
        (0.0, _BLANK),
        (0.05, _chat_delta(reasoning="SECRET thinking")),
        (0.0, _BLANK),
        (0.10, _chat_delta(content='{"prompt":"Q?",')),  # FIRST content token
        (0.0, _BLANK),
        (0.10, _chat_delta(content='"canonical_answer":"A"}')),
        (0.0, _BLANK),
        (0.05, _chat_delta(finish="stop")),
        (0.0, _BLANK),
        (0.05, _sse({"choices": [], "usage": {
            "prompt_tokens": 100, "completion_tokens": 20}})),
        (0.0, _BLANK),
        (0.0, _DONE),
        (0.0, _BLANK),
    ]

    async def go():
        _bound(sb)
        return await transport._generate_grok_json(
            "p", max_tokens=10, model="grok-4.2", effort="medium",
            purpose="reasoning",
        )

    text = _run(go())
    assert text == '{"prompt":"Q?","canonical_answer":"A"}'
    assert "SECRET" not in text
    # The request asked to stream, with the usage chunk.
    body = _Session.captured["body"]
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert body["response_format"] == {"type": "json_object"}
    # The row.
    assert len(sb.rows) == 1
    row = sb.rows[0]
    assert row["purpose"] == "reasoning"
    assert row["finish_reason"] == "stop"
    assert row["prompt_tokens"] == 100 and row["completion_tokens"] == 20
    # ttft is the FIRST CONTENT token (~0.2s in), not the headers (~0s)
    # and not the role/reasoning chunks before it.
    assert row["ttft_ms"] is not None and row["ttft_ms"] >= 150
    # total is the whole stream (~0.4s); ttft sits strictly inside it.
    assert row["total_ms"] >= 350
    assert row["ttft_ms"] < row["total_ms"]
    # Schema mode: the concatenated content parses through the existing path.
    shaped = lily_reasoning._shape_question(json.loads(text))
    assert shaped["prompt"] == "Q?" and shaped["canonical_answer"] == "A"


# ---------------------------------------------------------------------------
# 2. Responses API stream (grok-4.5 lives here): output_text deltas only,
#    usage + status from response.completed.
# ---------------------------------------------------------------------------

def test_responses_stream_grok_4_5(transport):
    sb = _Sb()
    _Session.script = [
        (0.02, _sse({"type": "response.created",
                     "response": {"id": "r1", "status": "in_progress",
                                  "output": [], "usage": None}})),
        (0.0, _BLANK),
        (0.05, _sse({"type": "response.reasoning_text.delta",
                     "delta": "SECRET"})),
        (0.0, _BLANK),
        (0.10, _sse({"type": "response.output_text.delta",
                     "delta": '```json\n{"prompt":"Q?",'})),
        (0.0, _BLANK),
        (0.10, _sse({"type": "response.output_text.delta",
                     "delta": '"canonical_answer":"A"}\n```'})),
        (0.0, _BLANK),
        (0.05, _sse({"type": "response.completed", "response": {
            "id": "r1", "status": "completed",
            "usage": {"input_tokens": 120, "output_tokens": 30},
            "output": [{"type": "message", "content": [
                {"type": "output_text",
                 "text": '{"prompt":"Q?","canonical_answer":"A"}'}]}],
        }})),
        (0.0, _BLANK),
    ]

    async def go():
        _bound(sb)
        return await transport._generate_grok_json(
            "p", max_tokens=10, model="grok-4.5", effort="medium",
            purpose="reasoning",
        )

    text = _run(go())
    # fences stripped (the Responses path has no response_format)
    assert text == '{"prompt":"Q?","canonical_answer":"A"}'
    assert "SECRET" not in text
    assert _Session.captured["url"].endswith("/responses")
    assert _Session.captured["body"]["stream"] is True
    row = sb.rows[0]
    assert row["finish_reason"] == "completed"
    assert row["prompt_tokens"] == 120 and row["completion_tokens"] == 30
    assert row["ttft_ms"] is not None and row["ttft_ms"] >= 120
    assert row["ttft_ms"] < row["total_ms"]
    assert lily_reasoning._shape_question(json.loads(text))["prompt"] == "Q?"


def test_reasoning_deltas_never_reach_the_json_parser(transport):
    """xAI may stream the reasoning thread separately; only content is
    concatenated, so json.loads sees a clean document even when reasoning
    deltas are interleaved with content on both endpoints."""
    sb = _Sb()

    def _script(url):
        if url.endswith("/responses"):
            return [
                (0.0, _sse({"type": "response.output_text.delta", "delta": '{"a":'})),
                (0.0, _BLANK),
                (0.0, _sse({"type": "response.reasoning_text.delta", "delta": "NOT JSON {"})),
                (0.0, _BLANK),
                (0.0, _sse({"type": "response.reasoning_summary_text.delta", "delta": "]]]"})),
                (0.0, _BLANK),
                (0.0, _sse({"type": "response.output_text.delta", "delta": '1}'})),
                (0.0, _BLANK),
                (0.0, _sse({"type": "response.completed",
                            "response": {"status": "completed", "output": []}})),
                (0.0, _BLANK),
            ]
        return [
            (0.0, _chat_delta(content='{"a":', reasoning="NOT JSON {")),
            (0.0, _BLANK),
            (0.0, _chat_delta(reasoning="]]]")),
            (0.0, _BLANK),
            (0.0, _chat_delta(content='1}', finish="stop")),
            (0.0, _BLANK),
            (0.0, _DONE),
            (0.0, _BLANK),
        ]

    _Session.script = _script

    async def go():
        _bound(sb)
        chat = await transport._generate_grok_json("p", max_tokens=10, model="grok-4.2")
        resp = await transport._generate_grok_json("p", max_tokens=10, model="grok-4.5")
        return chat, resp

    chat, resp = _run(go())
    assert json.loads(chat) == {"a": 1}
    assert json.loads(resp) == {"a": 1}


# ---------------------------------------------------------------------------
# 3. The wall is an IDLE wall: a gap longer than it -> "timeout" row + raise.
#    A stall AFTER the first token still fails (ttft is set, total is short).
# ---------------------------------------------------------------------------

def test_idle_gap_longer_than_wall_records_timeout_and_raises(transport):
    sb = _Sb()
    _Session.script = [
        (0.05, _chat_delta(content='{"prompt":"Q?",')),
        (0.0, _BLANK),
        (2.0, _chat_delta(content='"canonical_answer":"A"}', finish="stop")),
        (0.0, _BLANK),
        (0.0, _DONE),
    ]

    async def go():
        _bound(sb)
        try:
            await transport._generate_grok_json(
                "p", max_tokens=10, model="grok-4.2", timeout=0.3,
                purpose="reasoning",
            )
        except asyncio.TimeoutError:
            return "timeout"
        return "silent"

    assert _run(go()) == "timeout"
    assert len(sb.rows) == 1
    row = sb.rows[0]
    assert row["finish_reason"] == "timeout"
    assert row["ttft_ms"] is not None          # the first token DID arrive
    assert row["total_ms"] < 1500              # fired at the wall, not at EOF
    assert row["prompt_tokens"] is None and row["completion_tokens"] is None


def test_idle_wall_is_read_from_config_at_call_time(transport, monkeypatch):
    """HOTFIX-008 hygiene, relocated: the per-call wall now lives inside
    the transport (idle), and it is still read through the lily_config
    accessor at call time — patching it AFTER import must move it."""
    sb = _Sb()
    monkeypatch.setattr(lily_config, "prefetch_timeout_seconds", lambda: 0.2)
    _Session.script = [
        (0.0, _chat_delta(content='{"a":')),
        (0.0, _BLANK),
        (2.0, _chat_delta(content='1}', finish="stop")),
        (0.0, _BLANK),
        (0.0, _DONE),
    ]

    async def go():
        _bound(sb)
        try:
            await asyncio.wait_for(
                transport._generate_grok_json("p", max_tokens=10, model="grok-4.2"),
                timeout=5.0,   # the 0.2s idle wall must fire well first
            )
        except asyncio.TimeoutError:
            return "timeout"
        return "silent"

    assert _run(go()) == "timeout"
    assert sb.rows[0]["finish_reason"] == "timeout"
    assert sb.rows[0]["total_ms"] < 1500


# ---------------------------------------------------------------------------
# 4. An OUTER cancel mid-stream -> "cancelled" row carrying how far it got.
# ---------------------------------------------------------------------------

def test_outer_cancel_mid_stream_records_cancelled_with_partial_chars(transport):
    sb = _Sb()
    first = '{"prompt":"Q?","canonical_'          # 26 chars land before the cut
    _Session.script = [
        (0.05, _chat_delta(content=first)),
        (0.0, _BLANK),
        (5.0, _chat_delta(content='answer":"A"}', finish="stop")),
        (0.0, _BLANK),
        (0.0, _DONE),
    ]

    async def go():
        _bound(sb)
        try:
            await asyncio.wait_for(
                transport._generate_grok_json(
                    "p", max_tokens=10, model="grok-4.2", timeout=10.0,
                    purpose="reasoning",
                ),
                timeout=0.3,
            )
        except asyncio.TimeoutError:
            return "cancelled-by-outer-wall"
        return "silent"

    assert _run(go()) == "cancelled-by-outer-wall"
    assert len(sb.rows) == 1
    row = sb.rows[0]
    assert row["finish_reason"] == f"cancelled:chars={len(first)}"
    assert row["ttft_ms"] is not None
    assert row["total_ms"] < 1500


# ---------------------------------------------------------------------------
# 5. The prefetch chain no longer cuts a long HEALTHY stream at the per-call
#    wall: chunks keep arriving inside the idle wall, generation runs longer
#    than the wall, the question lands. (RED on main: the per-leg
#    wait_for(prefetch_timeout_seconds) killed it.)
# ---------------------------------------------------------------------------

def test_prefetch_chain_survives_generation_longer_than_the_wall(
    transport, monkeypatch
):
    sb = _Sb()
    monkeypatch.setattr(lily_config, "prefetch_timeout_seconds", lambda: 0.3)
    monkeypatch.setattr(lily_config, "prefetch_total_budget_seconds", lambda: 8.0)
    monkeypatch.setattr(lily_config, "adult_reasoning_model", lambda: "grok-4.5")

    def _script(url):
        # Nine chunks 0.1s apart: ~0.9s of generation per leg, every gap
        # well inside the 0.3s idle wall, total well past it.
        script = []
        for piece in _chunked(_QUESTION, 12):
            script.append((0.1, _sse({"type": "response.output_text.delta",
                                      "delta": piece})))
            script.append((0.0, _BLANK))
        script.append((0.0, _sse({"type": "response.completed", "response": {
            "status": "completed",
            "usage": {"input_tokens": 10, "output_tokens": 5}, "output": [],
        }})))
        script.append((0.0, _BLANK))
        return script

    _Session.script = _script
    sk = LilyScorekeeper("lily-w8")

    async def go():
        _bound(sb)
        return await transport.prefetch_question(sk, "history", 2, [])

    question = _run(go())
    assert question is not None, "a healthy-but-long stream was cut"
    assert question["prompt"] == "Q?" and question["canonical_answer"] == "A"
    assert not sk.status_notes
    # generate + verify: two rows, each a full stream longer than the wall.
    reasoning_rows = [r for r in sb.rows if r["purpose"] == "reasoning"]
    assert len(reasoning_rows) == 2
    for row in reasoning_rows:
        assert row["finish_reason"] == "completed"
        assert row["total_ms"] > 300 * 2
        assert row["ttft_ms"] is not None and row["ttft_ms"] < row["total_ms"]


# ---------------------------------------------------------------------------
# 6. Receipt (S1): one STREAM line per call, every outcome.
# ---------------------------------------------------------------------------

def test_stream_receipt_line_per_call(transport, caplog):
    sb = _Sb()
    _Session.script = [
        (0.0, _chat_delta(content='{"a":1}', finish="stop")),
        (0.0, _BLANK),
        (0.0, _sse({"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2}})),
        (0.0, _BLANK),
        (0.0, _DONE),
        (0.0, _BLANK),
    ]

    async def go():
        _bound(sb)
        await transport._generate_grok_json(
            "p", max_tokens=10, model="grok-4.2", purpose="reasoning",
        )
        _Session.script = [
            (0.0, _chat_delta(content='{"a":')),
            (0.0, _BLANK),
            (2.0, _chat_delta(content='1}')),
        ]
        try:
            await transport._generate_grok_json(
                "p", max_tokens=10, model="grok-4.2", timeout=0.2,
                purpose="reasoning",
            )
        except asyncio.TimeoutError:
            pass

    with caplog.at_level(logging.INFO, logger="lily_reasoning"):
        _run(go())
    lines = [r.getMessage() for r in caplog.records
             if "LILY_REASONING | STREAM |" in r.getMessage()]
    assert len(lines) == 2
    for needle in ("purpose=reasoning", "model=grok-4.2", "ttft_ms=",
                   "total_ms=", "chunks=", "chars=", "finish="):
        assert all(needle in line for line in lines), needle
    assert "finish=stop" in lines[0] and "chars=7" in lines[0]
    assert "finish=timeout" in lines[1] and "chars=5" in lines[1]


# ---------------------------------------------------------------------------
# 7. Edges: an HTTP error has no first token (ttft null, honest); a server
#    that ignores `stream` is consumed as one document and LOGGED as such.
# ---------------------------------------------------------------------------

def test_http_error_row_has_null_ttft_and_raises(transport):
    sb = _Sb()
    _Session.script = []
    _Session.status = 500

    async def go():
        _bound(sb)
        try:
            await transport._generate_grok_json("p", max_tokens=10, model="grok-4.2")
        except RuntimeError:
            return "raised"
        return "silent"

    assert _run(go()) == "raised"
    assert sb.rows[0]["finish_reason"] == "http_500"
    assert sb.rows[0]["ttft_ms"] is None


def test_json_reply_to_a_stream_request_is_consumed_and_logged(
    transport, caplog, monkeypatch
):
    sb = _Sb()

    class _JsonResp(_Resp):
        content_type = "application/json"

        async def json(self, *a, **k):
            return {"choices": [{"message": {"content": '{"a":1}'},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    class _JsonSession(_Session):
        def post(self, url, json=None, headers=None, timeout=None):
            return _JsonResp([])

    monkeypatch.setattr(lily_reasoning.aiohttp, "ClientSession", _JsonSession)

    async def go():
        _bound(sb)
        return await transport._generate_grok_json("p", max_tokens=10, model="grok-4.2")

    with caplog.at_level(logging.WARNING, logger="lily_reasoning"):
        assert _run(go()) == '{"a":1}'
    assert any("STREAM_FALLBACK_JSON" in r.getMessage() for r in caplog.records)
    assert sb.rows[0]["finish_reason"] == "stop"
    assert sb.rows[0]["prompt_tokens"] == 1
