"""WO-LILY-LLM-USAGE-ALL-PATHS-001 — one lily_llm_usage row per LLM call on
EVERY path, purpose/model/effort/ttft/total from the actual call.

Live facts that drove this: 64 rows, ALL purpose='vocal', because the only
writer was the vocal component's metrics_collected sink (purpose and model
hardcoded) and every off-path transport (reasoning, judge, assessment,
vision, grounding, arsenal_gen) was a direct API call that never wrote.
The writer also swallowed every failure at DEBUG.

Behavior, not source text: a fake supabase captures inserts; each call
site is driven through its REAL transport code with a fake HTTP session /
fake genai client and must produce exactly one row with the right fields.
"""

import asyncio
import inspect
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_arsenal_gen
import lily_assessment
import lily_config
import lily_metrics
import lily_persistence
import lily_reasoning
import lily_search
import lily_vision
from lily_metrics import LilyMetricsCollector


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _Sb:
    """Minimal supabase fake: captures every lily_llm_usage insert. `fail`
    is a callable(row) -> Exception|None consulted per insert so a test can
    script a missing-column error or an outage."""

    def __init__(self, fail=None):
        self.rows = []
        self.attempts = []
        self._fail = fail

    def table(self, name):
        assert name == "lily_llm_usage"
        sb = self

        class _Q:
            def insert(self_q, row):
                self_q.row = row
                return self_q

            def execute(self_q):
                sb.attempts.append(dict(self_q.row))
                if sb._fail is not None:
                    exc = sb._fail(self_q.row)
                    if exc is not None:
                        raise exc
                sb.rows.append(dict(self_q.row))
                return SimpleNamespace(data=[dict(self_q.row)])

        return _Q()


class _FakeResp:
    def __init__(self, body, status=200):
        self._body = body
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self, content_type=None):
        return self._body

    async def text(self):
        return "err"


class _FakeSession:
    """aiohttp.ClientSession stand-in: returns a canned body per endpoint,
    with a usage block and a finish reason so the row carries tokens."""
    body = None
    text = '{"ok":1}'
    status = 200

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def post(self, url, json=None, headers=None, timeout=None):
        if _FakeSession.body is not None:
            return _FakeResp(_FakeSession.body, _FakeSession.status)
        if url.endswith("/responses"):
            return _FakeResp({
                "status": "completed",
                "usage": {"input_tokens": 120, "output_tokens": 30},
                "output": [{"type": "message", "content": [
                    {"type": "output_text", "text": _FakeSession.text}]}],
            }, _FakeSession.status)
        return _FakeResp({
            "choices": [{"message": {"content": _FakeSession.text},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        }, _FakeSession.status)


async def _drain():
    """Let every scheduled fire-and-forget write land."""
    me = asyncio.current_task()
    for _ in range(3):
        pending = [t for t in asyncio.all_tasks() if t is not me and not t.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await asyncio.sleep(0)


def _bound(sb, session_id="sess-1", phase="lobby"):
    c = LilyMetricsCollector()
    c.bind_usage_context(supabase=sb, session_id=session_id, phase=phase)
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
        _FakeSession.body = None
        _FakeSession.text = '{"ok":1}'
        _FakeSession.status = 200


# ---------------------------------------------------------------------------
# 1. The writer contract
# ---------------------------------------------------------------------------

def test_record_llm_call_writes_one_full_row():
    sb = _Sb()
    ok = asyncio.run(lily_persistence.lily_record_llm_call(
        sb, session_id="s1", purpose="judge", model="grok-4.5",
        effort="medium", ttft_ms=812.5, total_ms=1400.0, prompt_tokens=10,
        completion_tokens=5, finish_reason="stop", phase="scoring",
        utterance_id="u1",
    ))
    assert ok is True
    assert len(sb.rows) == 1
    row = sb.rows[0]
    assert row == {
        "session_id": "s1", "utterance_id": "u1", "phase": "scoring",
        "model": "grok-4.5", "purpose": "judge", "effort": "medium",
        "ttft_ms": 812.5, "total_ms": 1400.0, "prompt_tokens": 10,
        "completion_tokens": 5, "finish_reason": "stop", "empty_stop": False,
    }


def test_record_llm_call_none_client_is_false_not_raise():
    assert asyncio.run(lily_persistence.lily_record_llm_call(
        None, session_id="s", purpose="vocal", model=None, effort=None,
        ttft_ms=None, total_ms=None, prompt_tokens=None,
        completion_tokens=None, finish_reason=None, phase=None,
    )) is False


def test_write_failure_warns_and_returns_false(caplog):
    sb = _Sb(fail=lambda row: RuntimeError("PGRST205: relation not found"))
    with caplog.at_level(logging.WARNING, logger="lily_persistence"):
        ok = asyncio.run(lily_persistence.lily_record_llm_call(
            sb, session_id="s1", purpose="vision", model="grok-4.5",
            effort=None, ttft_ms=1.0, total_ms=2.0, prompt_tokens=None,
            completion_tokens=None, finish_reason="ok", phase=None,
        ))
    assert ok is False
    assert not sb.rows
    warned = [r for r in caplog.records if "WRITE_FAILED" in r.getMessage()]
    assert warned and warned[0].levelno == logging.WARNING
    assert "purpose=vision" in warned[0].getMessage()


def test_missing_effort_column_drops_key_retries_once_and_warns(caplog):
    """Older env without migration 027: PGRST204 on 'effort' -> drop the
    key, retry once, WARN. The memo makes later rows drop it up front."""
    lily_persistence._llm_usage_absent_columns.clear()

    def _fail(row):
        if "effort" in row:
            return RuntimeError(
                "{'code': 'PGRST204', 'message': \"Could not find the "
                "'effort' column of 'lily_llm_usage' in the schema cache\"}"
            )
        return None

    sb = _Sb(fail=_fail)
    try:
        with caplog.at_level(logging.WARNING, logger="lily_persistence"):
            ok = asyncio.run(lily_persistence.lily_record_llm_call(
                sb, session_id="s1", purpose="reasoning", model="grok-4.5",
                effort="high", ttft_ms=1.0, total_ms=2.0, prompt_tokens=1,
                completion_tokens=1, finish_reason="stop", phase=None,
            ))
        assert ok is True
        assert len(sb.attempts) == 2            # once with, once without
        assert "effort" in sb.attempts[0]
        assert "effort" not in sb.attempts[1]
        assert len(sb.rows) == 1 and "effort" not in sb.rows[0]
        assert sb.rows[0]["purpose"] == "reasoning"
        assert any("COLUMN_ABSENT" in r.getMessage() for r in caplog.records)
        # Memoized: the next row never carries the key (one attempt).
        ok2 = asyncio.run(lily_persistence.lily_record_llm_call(
            sb, session_id="s1", purpose="judge", model="grok-4.5",
            effort="medium", ttft_ms=1.0, total_ms=2.0, prompt_tokens=1,
            completion_tokens=1, finish_reason="stop", phase=None,
        ))
        assert ok2 is True and len(sb.attempts) == 3
        assert "effort" not in sb.attempts[2]
    finally:
        lily_persistence._llm_usage_absent_columns.clear()


def test_legacy_write_llm_usage_now_warns_not_debug(caplog):
    sb = _Sb(fail=lambda row: RuntimeError("db down"))
    with caplog.at_level(logging.WARNING, logger="lily_persistence"):
        asyncio.run(lily_persistence.lily_write_llm_usage(
            sb, {"session_id": "s", "purpose": "vocal"}
        ))
    assert any(
        r.levelno == logging.WARNING and "WRITE_FAILED" in r.getMessage()
        for r in caplog.records
    )


# ---------------------------------------------------------------------------
# 2. The collector: scheduling, failure counter, summary row
# ---------------------------------------------------------------------------

def test_collector_schedules_row_and_summary_carries_health():
    sb = _Sb()

    async def go():
        c = _bound(sb)
        assert c.record_llm_call(
            purpose="judge", model="grok-4.5", effort="medium",
            ttft_ms=10.0, total_ms=20.0, finish_reason="stop",
        ) is True
        return c

    c = _run(go())
    assert len(sb.rows) == 1
    assert sb.rows[0]["session_id"] == "sess-1"
    assert sb.rows[0]["phase"] == "lobby"
    assert c.llm_usage_write_failures == 0
    assert c.llm_usage_rows_scheduled == 1
    assert c.summary()["llm_usage"] == {
        "rows_scheduled": 1, "llm_usage_write_failures": 0,
    }


def test_collector_counts_write_failures():
    sb = _Sb(fail=lambda row: RuntimeError("db down"))

    async def go():
        c = _bound(sb)
        for _ in range(3):
            c.record_llm_call(
                purpose="vision", model="grok-4.5", effort=None,
                ttft_ms=1.0, total_ms=1.0,
            )
        return c

    c = _run(go())
    assert c.llm_usage_write_failures == 3
    assert c.summary()["llm_usage"]["llm_usage_write_failures"] == 3


def test_collector_failure_counter_is_bounded():
    c = LilyMetricsCollector()
    for _ in range(lily_metrics._USAGE_FAILURE_CAP + 50):
        c._note_usage_write_failure("x")
    assert c.llm_usage_write_failures == lily_metrics._USAGE_FAILURE_CAP


def test_collector_no_loop_counts_failure_not_raise():
    sb = _Sb()
    c = LilyMetricsCollector()
    c.bind_usage_context(supabase=sb, session_id="s")
    assert c.record_llm_call(
        purpose="judge", model="m", effort=None, ttft_ms=1.0, total_ms=1.0,
    ) is False
    assert c.llm_usage_write_failures == 1


def test_unbound_collector_summary_unchanged_and_module_seam_noop():
    assert LilyMetricsCollector().summary() == {"turns_measured": 0}
    lily_metrics.set_current_collector(None)
    assert lily_metrics.record_llm_call(
        purpose="judge", model="m", effort=None, ttft_ms=1.0, total_ms=1.0,
    ) is False


def test_session_id_override_beats_bound_context():
    sb = _Sb()

    async def go():
        c = _bound(sb, session_id="live-session")
        c.record_llm_call(
            purpose="assessment", model="grok-4.5", effort="high",
            ttft_ms=1.0, total_ms=1.0, session_id="orphan-session",
        )

    _run(go())
    assert sb.rows[0]["session_id"] == "orphan-session"


# ---------------------------------------------------------------------------
# 3. Vocal path: model + effort from the component; adult swap wiring
# ---------------------------------------------------------------------------

class _FakeLLM:
    def __init__(self, model, effort):
        self._opts = SimpleNamespace(model=model, reasoning_effort=effort)
        self._handlers = {}

    def on(self, event, cb):
        self._handlers.setdefault(event, []).append(cb)

    def emit(self, m):
        for cb in self._handlers.get("metrics_collected", []):
            cb(m)


def _metrics(speech_id, ttft=1.2, duration=2.5):
    return SimpleNamespace(
        prompt_tokens=500, prompt_cached_tokens=0, completion_tokens=40,
        ttft=ttft, duration=duration, request_id="r", speech_id=speech_id,
        cancelled=False,
    )


def test_vocal_rows_carry_model_and_effort_from_the_component():
    sb = _Sb()

    async def go():
        c = _bound(sb)
        llm = _FakeLLM("grok-4.5", "low")
        c.wire_llm(llm)                      # purpose defaults to vocal
        llm.emit(_metrics("sp_1"))
        await asyncio.sleep(0)               # collect_llm_call_soon tick

    _run(go())
    assert len(sb.rows) == 1
    row = sb.rows[0]
    assert row["purpose"] == "vocal"
    assert row["model"] == "grok-4.5"
    assert row["effort"] == "low"
    assert row["ttft_ms"] == 1200.0 and row["total_ms"] == 2500.0
    assert row["utterance_id"] == "sp_1"
    assert row["prompt_tokens"] == 500 and row["completion_tokens"] == 40


def test_adult_swap_wires_its_own_identity():
    """A swapped-in vocal component wired through the same seam writes
    rows under adult_vocal with ITS model/effort; the original lane's
    rows are untouched (per-component identity, not a global)."""
    sb = _Sb()

    async def go():
        c = _bound(sb)
        general = _FakeLLM("grok-4.5", "low")
        c.wire_llm(general, purpose="vocal")
        adult = _FakeLLM("grok-4.5-adult", "medium")
        c.wire_llm(adult, purpose="adult_vocal")
        general.emit(_metrics("sp_g"))
        adult.emit(_metrics("sp_a"))
        await asyncio.sleep(0)

    _run(go())
    by = {r["utterance_id"]: r for r in sb.rows}
    assert by["sp_g"]["purpose"] == "vocal" and by["sp_g"]["effort"] == "low"
    assert by["sp_a"]["purpose"] == "adult_vocal"
    assert by["sp_a"]["model"] == "grok-4.5-adult"
    assert by["sp_a"]["effort"] == "medium"


def test_not_given_effort_records_none():
    class _NotGiven:  # the framework sentinel is not a str
        pass

    sb = _Sb()

    async def go():
        c = _bound(sb)
        llm = _FakeLLM("grok-4-fast", _NotGiven())
        c.wire_llm(llm)
        llm.emit(_metrics("sp"))
        await asyncio.sleep(0)

    _run(go())
    assert sb.rows[0]["effort"] is None


def test_legacy_sink_receives_identity_fields():
    c = LilyMetricsCollector()
    seen = []
    c.set_usage_sink(seen.append)
    llm = _FakeLLM("grok-4.5", "low")
    c.wire_llm(llm, purpose="adult_vocal")
    llm.emit(_metrics("sp"))   # no loop -> immediate fold
    assert seen[0]["purpose"] == "adult_vocal"
    assert seen[0]["model"] == "grok-4.5" and seen[0]["effort"] == "low"


def test_agent_wires_the_seam_and_binds_the_context():
    import lily_agent
    src = inspect.getsource(lily_agent)
    assert "session_metrics.wire_llm(llm, purpose=purpose)" in src
    assert "session_metrics.bind_usage_context(" in src
    assert "lily_metrics.set_current_collector(session_metrics)" in src
    # The hardcoded purpose/model sink is gone.
    assert 'row["purpose"] = "vocal"' not in src


# ---------------------------------------------------------------------------
# 4. Off-path call sites, driven through their real transport code
# ---------------------------------------------------------------------------

def _reasoning(monkeypatch):
    monkeypatch.setattr(lily_config, "xai_api_key", lambda: "k")
    monkeypatch.setattr(lily_reasoning.aiohttp, "ClientSession", _FakeSession)
    return lily_reasoning.LilyReasoning.__new__(lily_reasoning.LilyReasoning)


def test_bare_transport_default_purpose_is_adult_reasoning(monkeypatch):
    sb = _Sb()
    r = _reasoning(monkeypatch)
    monkeypatch.setattr(lily_config, "adult_reasoning_model", lambda: "grok-4.5")
    monkeypatch.setattr(
        lily_config, "adult_reasoning_effort", lambda override=None: "high"
    )

    async def go():
        _bound(sb)
        return await r._generate_grok_json("p", max_tokens=10)

    assert _run(go()) == '{"ok":1}'
    assert len(sb.rows) == 1
    row = sb.rows[0]
    assert row["purpose"] == "adult_reasoning"
    assert row["model"] == "grok-4.5" and row["effort"] == "high"
    assert row["finish_reason"] == "completed"        # responses api
    assert row["prompt_tokens"] == 120 and row["completion_tokens"] == 30
    assert isinstance(row["ttft_ms"], float) and isinstance(row["total_ms"], float)
    assert 0 <= row["ttft_ms"] <= row["total_ms"]


def test_model_and_effort_come_from_the_call_not_config_reread(monkeypatch):
    sb = _Sb()
    r = _reasoning(monkeypatch)
    calls = {"n": 0}

    def _model():
        calls["n"] += 1
        return "grok-4.5"

    monkeypatch.setattr(lily_config, "adult_reasoning_model", _model)

    async def go():
        _bound(sb)
        await r._generate_grok_json(
            "p", max_tokens=10, model="grok-4.2", effort="low",
            purpose="reasoning",
        )

    _run(go())
    assert calls["n"] == 0                       # never re-read
    assert sb.rows[0]["model"] == "grok-4.2"
    assert sb.rows[0]["effort"] == "low"
    assert sb.rows[0]["purpose"] == "reasoning"
    assert sb.rows[0]["finish_reason"] == "stop"  # chat completions


def test_generate_question_verify_and_distractors_are_reasoning(monkeypatch):
    sb = _Sb()
    r = _reasoning(monkeypatch)
    monkeypatch.setattr(lily_config, "tavily_api_key", lambda: None)
    _FakeSession.body = {
        "choices": [{"message": {"content":
            '{"prompt":"Q?","canonical_answer":"A","acceptable_answers":["A"],'
            '"distractors":["B","C","D"],"verdict":"pass","reason":"ok"}'},
            "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    monkeypatch.setattr(lily_config, "adult_reasoning_model", lambda: "grok-4.2")
    monkeypatch.setattr(lily_config, "reasoning_model", lambda: "grok-4.2")

    async def go():
        _bound(sb)
        await r.generate_question("history", 1, [])
        await r.verify_question({"prompt": "Q?", "canonical_answer": "A"})
        await r.ensure_choices({"prompt": "Q?", "canonical_answer": "A"})

    _run(go())
    # Rows land off to_thread tasks — completion order is not call order.
    assert [row["purpose"] for row in sb.rows] == ["reasoning"] * 3
    efforts = sorted(row["effort"] for row in sb.rows)
    # authoring + verification run high; distractors run reasoning_effort
    assert efforts == ["medium", "medium", "medium"]


def test_judge_row(monkeypatch):
    sb = _Sb()
    r = _reasoning(monkeypatch)

    async def go():
        _bound(sb, phase="scoring")
        await r.judge("rules", "who said what")

    _run(go())
    assert len(sb.rows) == 1
    assert sb.rows[0]["purpose"] == "judge"
    assert sb.rows[0]["model"] == lily_config.judge_model()
    assert sb.rows[0]["effort"] == lily_config.judge_effort()
    assert sb.rows[0]["phase"] == "scoring"


def test_failed_call_records_error_row_and_still_raises(monkeypatch):
    sb = _Sb()
    r = _reasoning(monkeypatch)
    _FakeSession.status = 500

    async def go():
        _bound(sb)
        try:
            await r._generate_grok_json("p", max_tokens=10, purpose="judge")
        except RuntimeError:
            return "raised"
        return "silent"

    assert _run(go()) == "raised"
    assert len(sb.rows) == 1
    assert sb.rows[0]["finish_reason"] == "http_500"
    assert sb.rows[0]["purpose"] == "judge"
    assert sb.rows[0]["ttft_ms"] is not None  # headers came back


def test_assessment_row_carries_the_assessed_session(monkeypatch):
    """The sweep assesses OTHER sessions inside a live process: the row
    must carry the assessed session's id, not the bound one."""
    sb = _Sb()
    monkeypatch.setattr(lily_config, "xai_api_key", lambda: "k")
    monkeypatch.setattr(lily_reasoning.aiohttp, "ClientSession", _FakeSession)
    _FakeSession.text = (
        '{"summary":"s","group_dynamics":"g","per_player":{},'
        '"host_performance":"h","flags":[]}'
    )
    filled = {}

    async def _fill(supabase, session_id, assessment):
        filled["session"] = session_id
        return True

    monkeypatch.setattr(lily_assessment, "lily_fill_assessment", _fill)

    async def go():
        _bound(sb, session_id="live-session")
        return await lily_assessment.lily_assess_session(
            object(), "orphan-session", [], {}
        )

    assert _run(go()) is True
    assert filled["session"] == "orphan-session"
    assert len(sb.rows) == 1
    assert sb.rows[0]["purpose"] == "assessment"
    assert sb.rows[0]["session_id"] == "orphan-session"
    assert sb.rows[0]["model"] == lily_config.assessment_model()
    assert sb.rows[0]["effort"] == lily_config.assessment_effort()


def test_arsenal_author_row(monkeypatch):
    sb = _Sb()
    r = _reasoning(monkeypatch)
    _FakeSession.text = (
        '{"question_text":"Q","canonical_answer":"A","acceptable_answers":'
        '["A"],"options":null,"reveal_color":"r"}'
    )

    async def go():
        _bound(sb)
        return await lily_arsenal_gen.lily_author_question(
            r, partition="general",
            plan={"format": "identify", "subject_area": "x",
                  "difficulty_tier": 1},
            image_description=None,
        )

    assert _run(go())["canonical_answer"] == "A"
    assert len(sb.rows) == 1
    assert sb.rows[0]["purpose"] == "arsenal_gen"
    assert sb.rows[0]["model"] == lily_config.reasoning_model()
    assert sb.rows[0]["effort"] == lily_config.reasoning_effort()


class _VisionSession(_FakeSession):
    def post(self, url, json=None, **kwargs):
        content = (
            '{"approved":true,"reason":"matches"}'
            if json.get("response_format") else "A description."
        )
        return _FakeResp({
            "choices": [{"message": {"content": content},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 700, "completion_tokens": 12},
        }, _FakeSession.status)


def test_vision_rows_default_and_arsenal_purpose(monkeypatch):
    sb = _Sb()
    monkeypatch.setattr(lily_config, "xai_api_key", lambda: "x")
    monkeypatch.setattr(lily_vision.aiohttp, "ClientSession", _VisionSession)

    async def go():
        _bound(sb)
        d = await lily_vision.lily_describe_image("https://x/i.jpg", "what")
        assert d["status"] == "ok"
        ok, _ = await lily_vision.lily_classify_image_bytes(
            b"jpg", "image/jpeg", "match?"
        )
        assert ok is True
        await lily_arsenal_gen.lily_classify_image(
            None, image_bytes=b"jpg", content_type="image/jpeg",
            claim="c", brief="b",
        )
        await lily_arsenal_gen.lily_describe_image(
            None, image_bytes=b"jpg", content_type="image/jpeg",
        )

    _run(go())
    # Rows land off to_thread tasks — completion order is not call order.
    purposes = sorted(row["purpose"] for row in sb.rows)
    assert purposes == ["arsenal_gen", "arsenal_gen", "vision", "vision"]
    for row in sb.rows:
        assert row["model"] == lily_config.vision_model()
        assert row["effort"] is None            # vision sends no effort
        assert row["finish_reason"] == "stop"
        assert row["prompt_tokens"] == 700 and row["completion_tokens"] == 12
        assert 0 <= row["ttft_ms"] <= row["total_ms"]


def test_vision_http_error_records_row(monkeypatch):
    sb = _Sb()
    monkeypatch.setattr(lily_config, "xai_api_key", lambda: "x")
    monkeypatch.setattr(lily_vision.aiohttp, "ClientSession", _VisionSession)
    _FakeSession.status = 429

    async def go():
        _bound(sb)
        return await lily_vision.lily_describe_image("https://x/i.jpg")

    assert _run(go())["status"] == "error"
    assert sb.rows[0]["finish_reason"] == "http_429"
    assert sb.rows[0]["purpose"] == "vision"


def test_grounding_row(monkeypatch):
    sb = _Sb()
    monkeypatch.setattr(lily_config, "google_api_key_present", lambda: True)
    monkeypatch.setattr(
        lily_config, "google_grounding_model", lambda: "gemini-3-flash"
    )

    class _Enum:
        name = "STOP"

    resp = SimpleNamespace(
        text="answer",
        candidates=[SimpleNamespace(
            grounding_metadata=None, url_context_metadata=None,
            finish_reason=_Enum(),
        )],
        usage_metadata=SimpleNamespace(
            prompt_token_count=55, candidates_token_count=7,
        ),
    )

    class _Models:
        def generate_content(self, **kw):
            _Models.model = kw["model"]
            return resp

    monkeypatch.setattr(
        lily_search, "_genai_grounding_client",
        lambda: SimpleNamespace(models=_Models()),
    )

    async def go():
        _bound(sb)
        return await lily_search._lily_grounded_generate(
            "q", use_search=True, use_url_context=False, timeout=5.0,
        )

    out = _run(go())
    assert out and out["text"] == "answer"
    assert _Models.model == "gemini-3-flash"
    assert len(sb.rows) == 1
    row = sb.rows[0]
    assert row["purpose"] == "grounding"
    assert row["model"] == "gemini-3-flash"
    assert row["effort"] is None
    assert row["finish_reason"] == "stop"
    assert row["prompt_tokens"] == 55 and row["completion_tokens"] == 7
    # Non-streaming SDK call, no first-byte hook: ttft == total, honestly.
    assert row["ttft_ms"] == row["total_ms"]


def test_grounding_failure_records_error_row(monkeypatch):
    sb = _Sb()
    monkeypatch.setattr(lily_config, "google_api_key_present", lambda: True)
    monkeypatch.setattr(lily_config, "google_grounding_model", lambda: "g")

    class _Models:
        def generate_content(self, **kw):
            raise ValueError("boom")

    monkeypatch.setattr(
        lily_search, "_genai_grounding_client",
        lambda: SimpleNamespace(models=_Models()),
    )

    async def go():
        _bound(sb)
        return await lily_search._lily_grounded_generate(
            "q", use_search=True, use_url_context=False, timeout=5.0,
        )

    assert _run(go()) is None
    assert sb.rows[0]["finish_reason"] == "error:ValueError"
    assert sb.rows[0]["purpose"] == "grounding"


def test_purpose_vocabulary_is_closed():
    assert set(lily_metrics.LLM_PURPOSES) == {
        "vocal", "adult_vocal", "reasoning", "adult_reasoning", "judge",
        "assessment", "vision", "grounding", "arsenal_gen",
    }


# ---------------------------------------------------------------------------
# 5. Migration is additive only
# ---------------------------------------------------------------------------

def test_migration_027_is_one_additive_nullable_column():
    sql = (Path(__file__).resolve().parent.parent / "migrations"
           / "027_lily_llm_usage_effort.sql").read_text().lower()
    body = "\n".join(
        l for l in sql.splitlines() if l.strip() and not l.strip().startswith("--")
    )
    assert "add column if not exists effort text" in body
    for forbidden in ("drop ", "alter column", "not null", "create table",
                      "rename"):
        assert forbidden not in body
