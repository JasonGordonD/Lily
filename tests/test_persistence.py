"""Focused persistence reliability tests with an in-memory Supabase fake."""

import asyncio

import lily_persistence


class _Result:
    def __init__(self, data=None):
        self.data = data or []


class _TranscriptQuery:
    def __init__(self, db):
        self.db = db
        self.rows = []

    def upsert(self, rows, on_conflict=None):
        assert on_conflict == "event_id"
        self.rows = [dict(row) for row in rows]
        return self

    def execute(self):
        self.db.calls += 1
        if self.db.failures:
            self.db.failures -= 1
            raise RuntimeError("temporary outage")
        by_id = {row["event_id"]: row for row in self.db.rows}
        for row in self.rows:
            by_id[row["event_id"]] = row
        self.db.rows = list(by_id.values())
        return _Result(self.rows)


class _TranscriptDB:
    def __init__(self, failures=0):
        self.failures = failures
        self.calls = 0
        self.rows = []

    def table(self, name):
        assert name == "lily_transcripts"
        return _TranscriptQuery(self)


def _batcher(db):
    return lily_persistence.LilyTranscriptBatcher(db, "room-1")


def test_transcript_flush_retries_without_duplicates(monkeypatch):
    monkeypatch.setattr(lily_persistence, "TRANSCRIPT_FLUSH_ATTEMPTS", 3)
    db = _TranscriptDB(failures=1)
    batcher = _batcher(db)
    batcher.add("hello", "S1", "Sarah")

    asyncio.run(batcher.flush())

    assert db.calls == 2
    assert [row["text"] for row in db.rows] == ["hello"]
    assert batcher._batch == []


def test_transcript_flush_retains_rows_after_exhaustion(monkeypatch):
    monkeypatch.setattr(lily_persistence, "TRANSCRIPT_FLUSH_ATTEMPTS", 2)
    db = _TranscriptDB(failures=2)
    batcher = _batcher(db)
    batcher.add("keep me", "S1", "Sarah")

    asyncio.run(batcher.flush())

    assert db.rows == []
    assert [row["text"] for row in batcher._batch] == ["keep me"]


def test_forget_discard_disables_future_transcript_writes():
    db = _TranscriptDB()
    batcher = _batcher(db)
    batcher.add("before forget", "S1", "Sarah")

    asyncio.run(batcher.discard_pending(disable=True))
    batcher.add("after forget", "S1", "Sarah")
    asyncio.run(batcher.flush())

    assert batcher._batch == []
    assert db.rows == []
