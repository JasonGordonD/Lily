"""REFACTOR-STAGE-1B-001 P1-3 — telemetry writers that used to fail at
DEBUG with no counter now fail at a bounded WARNING with a
`*_failure_count`, on the pattern the LLM-usage lane already used (S3:
every writer path INSERT-tested with a client that raises).

Writers: lily_persistence.lily_log_addressee (base-row site and
retry site, plus the degraded retry itself), lily_update_addressee_label,
lily_write_acoustic_trajectory; lily_search._record_grounding_usage;
lily_metrics.LilyMetricsCollector._on_usage_write_done. Consumer:
lily_agent.lily_session_metadata -> session_metrics.telemetry_write_failures
and session_metrics.llm_usage.done_callback_failure_count.
"""

import asyncio
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_agent  # noqa: E402
import lily_metrics  # noqa: E402
import lily_persistence  # noqa: E402
import lily_search  # noqa: E402
from lily_agent import LilyGame  # noqa: E402
from lily_scorekeeper import LilyScorekeeper  # noqa: E402


# ---------------------------------------------------------------------------
# a Supabase client whose every write raises
# ---------------------------------------------------------------------------

class _Query:
    def __init__(self, db, table):
        self.db, self.table_name = db, table

    def insert(self, row):
        self.db.attempts.append(("insert", self.table_name, row))
        return self

    def update(self, values):
        self.db.attempts.append(("update", self.table_name, values))
        return self

    def eq(self, *a):
        return self

    def execute(self):
        raise self.db.exc


class _RaisingDB:
    def __init__(self, exc=None):
        self.exc = exc or RuntimeError("PGRST301 JWT expired")
        self.attempts = []

    def table(self, name):
        return _Query(self, name)


@pytest.fixture(autouse=True)
def _reset_counters(monkeypatch):
    monkeypatch.setattr(lily_persistence, "_addressee_log_failure_count", 0)
    monkeypatch.setattr(lily_persistence, "_addressee_log_degraded_count", 0)
    monkeypatch.setattr(lily_persistence, "_addressee_label_failure_count", 0)
    monkeypatch.setattr(lily_persistence, "_acoustic_trajectory_failure_count", 0)
    monkeypatch.setattr(lily_search, "_grounding_usage_failure_count", 0)
    monkeypatch.setattr(lily_metrics, "_lane_failure_counts", {})


def _warnings(caplog, needle):
    return [
        r for r in caplog.records
        if needle in r.getMessage() and r.levelno == logging.WARNING
    ]


# ---------------------------------------------------------------------------
# lily_log_addressee — both sites
# ---------------------------------------------------------------------------

def test_addressee_log_base_row_failure_warns_and_counts(caplog):
    db = _RaisingDB()
    with caplog.at_level(logging.DEBUG, logger="lily_persistence"):
        row_id = asyncio.run(lily_persistence.lily_log_addressee(
            db, {"session_id": "s", "transcript": "hello", "is_final": True}
        ))
    assert row_id is None
    assert len(db.attempts) == 1  # no telemetry keys -> no retry
    assert lily_persistence._addressee_log_failure_count == 1
    w = _warnings(caplog, "LILY_ADDRESSEE_LOG | WRITE_FAILED")
    assert len(w) == 1
    assert "failures=1" in w[0].getMessage()
    assert "error_class=RuntimeError" in w[0].getMessage()
    assert "session=s" in w[0].getMessage()


def test_addressee_log_retry_site_failure_warns_and_counts(caplog):
    db = _RaisingDB()
    with caplog.at_level(logging.DEBUG, logger="lily_persistence"):
        row_id = asyncio.run(lily_persistence.lily_log_addressee(
            db, {
                "session_id": "s", "transcript": "hello", "is_final": True,
                "timing_source": "stt_stream_reconciled",  # WS-11 column
            }
        ))
    assert row_id is None
    assert len(db.attempts) == 2  # telemetry insert, then the stripped retry
    assert "timing_source" not in db.attempts[1][2]
    assert lily_persistence._addressee_log_degraded_count == 1
    assert lily_persistence._addressee_log_failure_count == 1
    assert len(_warnings(caplog, "LILY_ADDRESSEE_LOG | WRITE_DEGRADED")) == 1
    w = _warnings(caplog, "LILY_ADDRESSEE_LOG | WRITE_FAILED")
    assert len(w) == 1 and "retry without telemetry columns" in w[0].getMessage()


def test_addressee_log_cadence_is_bounded(caplog):
    """First 10 at WARNING, then DEBUG until the 100th."""
    db = _RaisingDB()
    row = {"session_id": "s", "transcript": "x", "is_final": True}
    with caplog.at_level(logging.DEBUG, logger="lily_persistence"):
        for _ in range(12):
            asyncio.run(lily_persistence.lily_log_addressee(db, row))
    assert lily_persistence._addressee_log_failure_count == 12
    recs = [r for r in caplog.records if "LILY_ADDRESSEE_LOG | WRITE_FAILED" in r.getMessage()]
    assert len(recs) == 12
    assert [r.levelno for r in recs[:10]] == [logging.WARNING] * 10
    assert [r.levelno for r in recs[10:]] == [logging.DEBUG] * 2


# ---------------------------------------------------------------------------
# lily_update_addressee_label
# ---------------------------------------------------------------------------

def test_addressee_label_update_failure_warns_and_counts(caplog):
    db = _RaisingDB(exc=ValueError("bad row"))
    with caplog.at_level(logging.DEBUG, logger="lily_persistence"):
        asyncio.run(lily_persistence.lily_update_addressee_label(
            db, 41, "host_directed", "adjudication_commit"
        ))  # never raises
    assert db.attempts == [("update", "lily_addressee_log",
                            {"label": "host_directed", "label_source": "adjudication_commit"})]
    assert lily_persistence._addressee_label_failure_count == 1
    w = _warnings(caplog, "LILY_ADDRESSEE_LOG | LABEL_UPDATE_FAILED")
    assert len(w) == 1
    msg = w[0].getMessage()
    assert "row_id=41" in msg and "failures=1" in msg and "error_class=ValueError" in msg


# ---------------------------------------------------------------------------
# lily_write_acoustic_trajectory
# ---------------------------------------------------------------------------

def test_acoustic_trajectory_failure_warns_and_counts(caplog):
    db = _RaisingDB()
    with caplog.at_level(logging.DEBUG, logger="lily_persistence"):
        asyncio.run(lily_persistence.lily_write_acoustic_trajectory(
            db, "lily-S1B", 3, {"category": {"anger": 0.1}}
        ))
    assert db.attempts[0][:2] == ("insert", "lily_acoustic_trajectories")
    assert db.attempts[0][2]["turn_index"] == 3
    assert lily_persistence._acoustic_trajectory_failure_count == 1
    w = _warnings(caplog, "LILY_ACOUSTIC | TRAJECTORY_WRITE_FAILED")
    assert len(w) == 1
    assert "session=lily-S1B turn=3 failures=1" in w[0].getMessage()


def test_acoustic_trajectory_skips_without_snapshot_and_counts_nothing():
    db = _RaisingDB()
    asyncio.run(lily_persistence.lily_write_acoustic_trajectory(db, "s", 1, None))
    asyncio.run(lily_persistence.lily_write_acoustic_trajectory(None, "s", 1, {"x": 1}))
    assert db.attempts == []
    assert lily_persistence._acoustic_trajectory_failure_count == 0


# ---------------------------------------------------------------------------
# lily_search grounding usage row
# ---------------------------------------------------------------------------

def test_grounding_usage_record_failure_warns_and_counts(monkeypatch, caplog):
    def _boom(**kwargs):
        raise RuntimeError("collector exploded")

    monkeypatch.setattr(lily_metrics, "record_llm_call", _boom)
    with caplog.at_level(logging.DEBUG, logger="lily_search"):
        lily_search._record_grounding_usage("gemini-x", 0.0, None, "timeout")
    assert lily_search.lily_grounding_usage_failure_count() == 1
    # Reported to the lily_metrics registry — the path the vocal module reads.
    assert lily_metrics.lily_telemetry_failure_counts() == {
        "grounding_usage_failure_count": 1,
    }
    w = _warnings(caplog, "LILY_SEARCH | GROUNDING | USAGE_RECORD_FAILED")
    assert len(w) == 1
    assert "model=gemini-x failures=1 error_class=RuntimeError" in w[0].getMessage()


def test_grounding_usage_healthy_path_counts_nothing(monkeypatch):
    seen = []
    monkeypatch.setattr(lily_metrics, "record_llm_call", lambda **kw: seen.append(kw))
    lily_search._record_grounding_usage("gemini-x", 0.0, None, "timeout")
    assert seen and seen[0]["purpose"] == "grounding"
    assert lily_search.lily_grounding_usage_failure_count() == 0


# ---------------------------------------------------------------------------
# lily_metrics usage-write done-callback
# ---------------------------------------------------------------------------

def test_usage_done_callback_own_fault_warns_and_counts(caplog):
    c = lily_metrics.LilyMetricsCollector()

    class _BadTask:
        def cancelled(self):
            raise RuntimeError("not a task")

    with caplog.at_level(logging.DEBUG, logger="lily_metrics"):
        c._on_usage_write_done(_BadTask())  # never raises into the loop
    assert c._usage_done_callback_failure_count == 1
    w = _warnings(caplog, "LILY_METRICS | USAGE_DONE_CB_FAILED")
    assert len(w) == 1 and w[0].exc_info is not None
    assert "failures=1 error_class=RuntimeError" in w[0].getMessage()
    assert c.summary()["llm_usage"]["done_callback_failure_count"] == 1


def test_usage_done_callback_healthy_paths_count_nothing():
    c = lily_metrics.LilyMetricsCollector()

    class _Task:
        def __init__(self, result=True, exc=None):
            self._r, self._e = result, exc

        def cancelled(self):
            return False

        def exception(self):
            return self._e

        def result(self):
            return self._r

    c._on_usage_write_done(_Task())
    c._on_usage_write_done(_Task(result=False))
    c._on_usage_write_done(_Task(exc=RuntimeError("insert died")))
    assert c._usage_done_callback_failure_count == 0
    assert c.llm_usage_write_failures == 2  # the writer's own counter, unchanged


# ---------------------------------------------------------------------------
# consumer: session_metrics.telemetry_write_failures
# ---------------------------------------------------------------------------

def test_counters_ride_session_metadata(monkeypatch, caplog):
    sk = LilyScorekeeper("lily-S1B-meta")
    game = LilyGame.bare(sk=sk)
    block = lily_agent.lily_session_metadata(game, sk, {}, None)["session_metrics"]
    counts = block["telemetry_write_failures"]
    assert counts == {
        "llm_usage_failure_count": lily_persistence._llm_usage_failure_count,
        "addressee_log_failure_count": 0,
        "addressee_log_degraded_count": 0,
        "addressee_label_failure_count": 0,
        "acoustic_trajectory_failure_count": 0,
        "grounding_usage_failure_count": 0,
    }
    db = _RaisingDB()
    asyncio.run(lily_persistence.lily_update_addressee_label(db, 1, "a", "b"))
    asyncio.run(lily_persistence.lily_write_acoustic_trajectory(db, "s", 1, {"x": 1}))
    monkeypatch.setattr(
        lily_metrics, "record_llm_call",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("x")),
    )
    lily_search._record_grounding_usage("m", 0.0, None, None)
    after = lily_agent.lily_session_metadata(game, sk, {}, None)["session_metrics"]
    assert after["telemetry_write_failures"]["addressee_label_failure_count"] == 1
    assert after["telemetry_write_failures"]["acoustic_trajectory_failure_count"] == 1
    assert after["telemetry_write_failures"]["grounding_usage_failure_count"] == 1


def test_counts_lane_failure_is_itself_a_receipt(monkeypatch, caplog):
    def _boom():
        raise RuntimeError("counts broke")

    monkeypatch.setattr(lily_metrics, "lily_telemetry_failure_counts", _boom)
    sk = LilyScorekeeper("x")
    with caplog.at_level(logging.WARNING, logger="lily_agent"):
        block = lily_agent.lily_session_metadata(
            LilyGame.bare(sk=sk), sk, {}, None
        )["session_metrics"]
    assert block["telemetry_write_failures"] == {"outcome": "counts_failed:RuntimeError"}
    assert any("LILY_TELEMETRY | COUNTS_FAILED" in r.getMessage() for r in caplog.records)
