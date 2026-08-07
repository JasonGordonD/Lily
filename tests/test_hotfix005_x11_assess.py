"""WO-LILY-HOTFIX-005 X11 — assessment pipeline dies on strict JSON.

Live: `ASSESS_FAILED | Extra data: line 13 column 1 (char 930)` — the model
appended prose after the JSON object; the old find/rfind slice grabbed a
stray '}' in the prose and json.loads choked, so the row stayed pending and
the sweep retried a deterministic failure forever.

Fixes: raw_decode from the first '{' (lenient extraction), one repair retry,
then a terminal 'failed' status so the sweep stops looping.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_assessment
from lily_assessment import (
    AssessmentParseError,
    _parse_assessment_json,
    lily_assess_session,
)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# -- lenient extraction -------------------------------------------------------

def test_trailing_prose_parses_the_object():
    text = '{"summary": "ok", "nested": {"a": 1}}\nThat is my read. }'
    assert _parse_assessment_json(text) == {"summary": "ok", "nested": {"a": 1}}


def test_fenced_json_parses():
    assert _parse_assessment_json('```json\n{"x": 5}\n```') == {"x": 5}


def test_no_object_raises_parse_error():
    try:
        _parse_assessment_json("no json here at all")
        raise AssertionError("must raise")
    except AssessmentParseError:
        pass


def test_non_object_raises_parse_error():
    try:
        _parse_assessment_json("[1, 2, 3]")
        raise AssertionError("must raise")
    except AssessmentParseError:
        pass


# -- retry + terminalize ------------------------------------------------------

class _FakeSupabase:
    """Records the terminal-fail update; lily_fill_assessment path unused."""
    def __init__(self):
        self.marked_failed = []

    def table(self, name):
        self._t = name
        return self

    def update(self, payload):
        self._payload = payload
        return self

    def eq(self, col, val):
        self._eq = (col, val)
        return self

    def execute(self):
        # emulate a pending row being terminalized
        if self._payload.get("report_status") == "failed":
            self.marked_failed.append(self._payload)

        class _R:
            data = [{"session_id": "s"}]
        return _R()


def test_parse_failure_retries_once_then_terminalizes():
    calls = {"n": 0}

    async def _always_prose(_transcript, _stats):
        calls["n"] += 1
        # deterministic unparseable output every time
        return _parse_assessment_json("prose only, no braces")

    sb = _FakeSupabase()
    ok = _run(lily_assess_session(sb, "s", [], {}, generate=_always_prose))
    assert ok is False
    # one initial attempt + one repair retry = 2 generations
    assert calls["n"] == 2
    # terminalized so the sweep stops re-running it
    assert sb.marked_failed, "row must be terminalized on permanent parse failure"


def test_repair_retry_recovers():
    calls = {"n": 0}

    async def _prose_then_clean(_transcript, _stats):
        calls["n"] += 1
        if calls["n"] == 1:
            return _parse_assessment_json("garbage no json")  # raises
        return {"summary": "recovered"}

    class _FillSb(_FakeSupabase):
        def execute(self):
            class _R:
                data = [{"session_id": "s"}]
            return _R()

    ok = _run(lily_assess_session(_FillSb(), "s", [], {}, generate=_prose_then_clean))
    assert ok is True
    assert calls["n"] == 2


def test_transient_failure_stays_pending():
    async def _timeout(_transcript, _stats):
        raise TimeoutError("model slow")

    sb = _FakeSupabase()
    ok = _run(lily_assess_session(sb, "s", [], {}, generate=_timeout))
    assert ok is False
    # transient failure must NOT terminalize — the sweep retries
    assert not sb.marked_failed
