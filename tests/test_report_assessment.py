"""WS-12 report pipeline: assessment fill + wrap-up-beat trigger + sweep.

Root cause on record: the close-path report WRITE fires (rows exist), but no
assessment producer ever existed — 41/41 lily_session_reports rows sat at
report_status='pending'. The pipeline must complete on the wrap-up beat and
never depend on the shutdown/close path (fleet: shutdown callbacks fire in
0-22% of sessions); a reconciliation sweep catches orphaned pending rows.
"""

import asyncio
import datetime
import json
import logging

import lily_assessment


class _Result:
    def __init__(self, data=None):
        self.data = data if data is not None else []


class _ReportQuery:
    """Chain fake for the lily_session_reports table: supports the upsert,
    pending-guarded update, and sweep-select chains the module uses."""

    def __init__(self, db):
        self.db = db
        self._op = None
        self._payload = None
        self._filters = []
        self._lt = None
        self._limit = None

    # -- write side (row upsert, close-path shape) --
    def upsert(self, payload, on_conflict=None):
        assert on_conflict == "session_id"
        self._op = "upsert"
        self._payload = dict(payload)
        return self

    # -- assessment fill --
    def update(self, payload):
        self._op = "update"
        self._payload = dict(payload)
        return self

    def eq(self, col, val):
        self._filters.append((col, val))
        return self

    # -- sweep select --
    def select(self, cols):
        self._op = "select"
        return self

    def lt(self, col, val):
        self._lt = (col, val)
        return self

    def order(self, col, desc=False):
        return self

    def limit(self, n):
        self._limit = n
        return self

    def execute(self):
        if self.db.fail_writes and self._op in ("upsert", "update"):
            raise RuntimeError("db outage")
        if self._op == "upsert":
            sid = self._payload["session_id"]
            row = self.db.rows.setdefault(
                sid, {"session_id": sid, "report_status": "pending",
                      "assessment": None,
                      "created_at": self.db.now_iso()}
            )
            row.update(self._payload)
            return _Result([dict(row)])
        if self._op == "update":
            matched = []
            for row in self.db.rows.values():
                if all(row.get(c) == v for c, v in self._filters):
                    row.update(self._payload)
                    matched.append(dict(row))
            return _Result(matched)
        if self._op == "select":
            out = [
                dict(row) for row in self.db.rows.values()
                if all(row.get(c) == v for c, v in self._filters)
                and (self._lt is None or row[self._lt[0]] < self._lt[1])
            ]
            out.sort(key=lambda r: r["created_at"])
            if self._limit is not None:
                out = out[: self._limit]
            return _Result(out)
        raise AssertionError("no op set")


class _ReportDB:
    def __init__(self):
        self.rows = {}
        self.fail_writes = False

    @staticmethod
    def now_iso():
        return datetime.datetime.now(datetime.timezone.utc).isoformat()

    def table(self, name):
        assert name == "lily_session_reports"
        return _ReportQuery(self)

    def seed(self, session_id, age_s=0.0, transcript=None, game_stats=None,
             status="pending", assessment=None):
        created = (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(seconds=age_s)
        ).isoformat()
        self.rows[session_id] = {
            "session_id": session_id,
            "group_id": "g1",
            "created_at": created,
            "transcript": transcript if transcript is not None else [],
            "game_stats": game_stats if game_stats is not None else {},
            "report_status": status,
            "assessment": assessment,
        }


async def _fake_generate(transcript, game_stats):
    return {"summary": "good table", "n_turns": len(transcript)}


# ---------------------------------------------------------------------------
# Assessment fill
# ---------------------------------------------------------------------------

def test_fill_assessment_completes_pending_row():
    db = _ReportDB()
    db.seed("s1")
    filled = asyncio.run(
        lily_assessment.lily_fill_assessment(db, "s1", {"summary": "x"})
    )
    assert filled is True
    row = db.rows["s1"]
    assert row["report_status"] == "complete"
    assert row["assessment"] == {"summary": "x"}


def test_fill_assessment_never_clobbers_completed_row():
    db = _ReportDB()
    db.seed("s1", status="complete", assessment={"summary": "first"})
    filled = asyncio.run(
        lily_assessment.lily_fill_assessment(db, "s1", {"summary": "second"})
    )
    assert filled is False
    assert db.rows["s1"]["assessment"] == {"summary": "first"}


# ---------------------------------------------------------------------------
# Assess-session: happy path + fail-visible alert, row stays retryable
# ---------------------------------------------------------------------------

def test_assess_session_fills_row():
    db = _ReportDB()
    db.seed("s1")
    ok = asyncio.run(lily_assessment.lily_assess_session(
        db, "s1", [{"role": "user", "text": "hi"}], {"rounds_played": 2},
        generate=_fake_generate,
    ))
    assert ok is True
    assert db.rows["s1"]["report_status"] == "complete"
    assert db.rows["s1"]["assessment"]["n_turns"] == 1


def test_assess_session_failure_alerts_and_leaves_pending(caplog):
    db = _ReportDB()
    db.seed("s1")

    async def _boom(transcript, game_stats):
        raise RuntimeError("model down")

    with caplog.at_level(logging.ERROR, logger="lily_assessment"):
        ok = asyncio.run(lily_assessment.lily_assess_session(
            db, "s1", [], {}, generate=_boom,
        ))
    assert ok is False
    assert db.rows["s1"]["report_status"] == "pending"
    assert any("LILY_REPORT | ASSESS_FAILED" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Wrap-up beat: the exit bar — a session's report is written AND assessed on
# the wrap-up path alone, inside the pipeline deadline, no shutdown involved.
# ---------------------------------------------------------------------------

def test_wrap_up_beat_produces_assessed_report_within_deadline():
    db = _ReportDB()

    async def _drive():
        await asyncio.wait_for(
            lily_assessment.lily_wrap_up_report(
                db, "s-sim", "g1",
                transcript=[{"role": "user", "text": "answer"}],
                game_stats={"rounds_played": 3},
                generate=_fake_generate,
            ),
            timeout=lily_assessment.report_deadline_seconds(),
        )

    asyncio.run(_drive())
    row = db.rows["s-sim"]
    assert row["report_status"] == "complete"
    assert row["assessment"]["summary"] == "good table"
    assert row["game_stats"] == {"rounds_played": 3}


def test_wrap_up_beat_write_survives_assess_failure(caplog):
    db = _ReportDB()

    async def _boom(transcript, game_stats):
        raise RuntimeError("model down")

    with caplog.at_level(logging.ERROR, logger="lily_assessment"):
        asyncio.run(lily_assessment.lily_wrap_up_report(
            db, "s-sim", "g1", transcript=[], game_stats={}, generate=_boom,
        ))
    row = db.rows["s-sim"]
    assert row["report_status"] == "pending"  # sweep will retry
    assert any("ASSESS_FAILED" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Reconciliation sweep: orphaned pending rows get assessed from stored data
# ---------------------------------------------------------------------------

def test_sweep_backfills_orphaned_pending_rows():
    db = _ReportDB()
    db.seed("old1", age_s=3600, transcript=[{"t": 1}], game_stats={"g": 1})
    db.seed("old2", age_s=7200, transcript=[{"t": 2}, {"t": 3}])
    db.seed("fresh", age_s=1)  # inside min-age grace window: untouched
    db.seed("done", age_s=3600, status="complete", assessment={"a": 1})

    stats = asyncio.run(lily_assessment.lily_report_sweep(
        db, generate=_fake_generate, min_age_s=600,
    ))
    assert stats == {"scanned": 2, "assessed": 2, "failed": 0}
    assert db.rows["old1"]["report_status"] == "complete"
    assert db.rows["old1"]["assessment"]["n_turns"] == 1
    assert db.rows["old2"]["assessment"]["n_turns"] == 2
    assert db.rows["fresh"]["report_status"] == "pending"
    assert db.rows["done"]["assessment"] == {"a": 1}


def test_sweep_counts_failures_and_never_raises(caplog):
    db = _ReportDB()
    db.seed("old1", age_s=3600)

    async def _boom(transcript, game_stats):
        raise RuntimeError("model down")

    with caplog.at_level(logging.ERROR, logger="lily_assessment"):
        stats = asyncio.run(lily_assessment.lily_report_sweep(
            db, generate=_boom, min_age_s=600,
        ))
    assert stats == {"scanned": 1, "assessed": 0, "failed": 1}
    assert db.rows["old1"]["report_status"] == "pending"


def test_sweep_with_no_supabase_is_a_silent_noop():
    stats = asyncio.run(lily_assessment.lily_report_sweep(None))
    assert stats == {"scanned": 0, "assessed": 0, "failed": 0}


def test_default_assessment_uses_grok_4_5_high(monkeypatch):
    captured = {}

    async def _fake_grok(self, prompt, **kwargs):
        captured.update(kwargs)
        return json.dumps({
            "summary": "A session.",
            "group_dynamics": "Solo.",
            "per_player": {},
            "host_performance": "Reviewed.",
            "flags": [],
        })

    monkeypatch.setattr(
        lily_assessment.lily_reasoning.LilyReasoning,
        "_generate_grok_json",
        _fake_grok,
    )
    result = asyncio.run(
        lily_assessment._default_generate([], {"rounds_played": 0})
    )
    assert result["summary"] == "A session."
    assert captured["model"] == "grok-4.5"
    assert captured["effort"] == "high"
    assert captured["timeout"] == 60.0


# ---------------------------------------------------------------------------
# Model-output parsing: fenced / prose-wrapped JSON still lands
# ---------------------------------------------------------------------------

def test_parse_assessment_json_handles_fences():
    raw = "```json\n" + json.dumps({"summary": "s"}) + "\n```"
    assert lily_assessment._parse_assessment_json(raw) == {"summary": "s"}


def test_parse_assessment_json_rejects_non_object():
    try:
        lily_assessment._parse_assessment_json("[1, 2]")
    except ValueError:
        pass
    else:
        raise AssertionError("non-object assessment must be rejected")
