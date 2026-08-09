"""Focused persistence reliability tests with an in-memory Supabase fake."""

import asyncio
import hashlib
from types import SimpleNamespace

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


class _QueryResult:
    def __init__(self, data=None):
        self.data = data or []
        self.count = len(self.data)


class _TableQuery:
    def __init__(self, db, table_name):
        self._db = db
        self._table = table_name
        self._filters = []
        self._selected = None
        self._update_values = None
        self._upsert_rows = None
        self._upsert_conflict = None
        self._delete = False
        self._limit = None

    def select(self, columns, count=None):
        self._selected = columns
        return self

    def eq(self, column, value):
        self._filters.append(("eq", column, value))
        return self

    def in_(self, column, values):
        self._filters.append(("in", column, list(values)))
        return self

    def limit(self, n):
        self._limit = n
        return self

    def update(self, values):
        self._update_values = dict(values)
        return self

    def upsert(self, rows, on_conflict=None):
        if isinstance(rows, list):
            self._upsert_rows = [dict(r) for r in rows]
        else:
            self._upsert_rows = [dict(rows)]
        self._upsert_conflict = on_conflict
        return self

    def delete(self):
        self._delete = True
        return self

    def execute(self):
        rows = self._db.tables.setdefault(self._table, [])

        def _match(row):
            for op, column, value in self._filters:
                if op == "eq" and row.get(column) != value:
                    return False
                if op == "in" and row.get(column) not in value:
                    return False
            return True

        if self._upsert_rows is not None:
            conflict_cols = [
                c.strip()
                for c in (self._upsert_conflict or "").split(",")
                if c.strip()
            ]
            for incoming in self._upsert_rows:
                matched = False
                if conflict_cols:
                    for idx, existing in enumerate(rows):
                        if all(existing.get(c) == incoming.get(c) for c in conflict_cols):
                            rows[idx] = {**existing, **incoming}
                            matched = True
                            break
                if not matched:
                    rows.append(dict(incoming))
            return _QueryResult(self._upsert_rows)

        if self._update_values is not None:
            out = []
            for row in rows:
                if _match(row):
                    row.update(self._update_values)
                    out.append(dict(row))
            return _QueryResult(out)

        if self._delete:
            keep, removed = [], []
            for row in rows:
                if _match(row):
                    removed.append(dict(row))
                else:
                    keep.append(row)
            self._db.tables[self._table] = keep
            return _QueryResult(removed)

        selected = [dict(r) for r in rows if _match(r)]
        if self._limit is not None:
            selected = selected[: self._limit]
        if self._selected and self._selected != "*":
            cols = [c.strip() for c in self._selected.split(",")]
            selected = [{k: row.get(k) for k in cols} for row in selected]
        return _QueryResult(selected)


class _SupabaseDB:
    def __init__(self, tables=None):
        self.tables = {k: [dict(r) for r in v] for k, v in (tables or {}).items()}

    def table(self, name):
        return _TableQuery(self, name)


class _ConnectedClient:
    _is_connected = True


class _ConnectedStream:
    _client = _ConnectedClient()


class _FakeSTT:
    def __init__(self, payload):
        self._payload = payload
        self._streams = [_ConnectedStream()]

    async def get_speaker_ids(self):
        return self._payload


def test_load_voiceprints_by_players_falls_back_to_speaker_label():
    db = _SupabaseDB({
        "lily_speaker_voiceprints": [
            {
                "group_id": "grp_a",
                "player_name": None,
                "speaker_label": "Sarah",
                "speaker_identifiers": ["id-sarah"],
            },
            {
                "group_id": "grp_b",
                "player_name": "Dave",
                "speaker_label": "S2",
                "speaker_identifiers": ["id-dave"],
            },
        ]
    })
    rows = asyncio.run(
        lily_persistence.lily_load_voiceprints_by_players(db, ["sarah", "dave"])
    )
    keys = {(r.get("group_id"), r.get("speaker_label")) for r in rows}
    assert ("grp_a", "Sarah") in keys
    assert ("grp_b", "S2") in keys


def test_rekey_group_moves_voiceprints_for_name_set_hash_ids():
    hash_id = "grp_" + hashlib.sha1(b"dave|sarah").hexdigest()
    db = _SupabaseDB({
        "lily_sessions": [{"session_id": "room-1", "group_id": hash_id}],
        "lily_group_facts": [{"group_id": hash_id, "source_session_id": "room-1"}],
        "lily_asked_history": [{"group_id": hash_id, "session_id": "room-1"}],
        "lily_speaker_voiceprints": [
            {
                "id": 1,
                "group_id": hash_id,
                "speaker_label": "S1",
                "player_name": "Sarah",
                "speaker_identifiers": ["id-sarah"],
            }
        ],
        "lily_group_prefs": [],
    })
    asyncio.run(
        lily_persistence.lily_rekey_group(db, hash_id, "grp_device_uuid", "room-1")
    )
    assert db.tables["lily_sessions"][0]["group_id"] == "grp_device_uuid"
    assert db.tables["lily_speaker_voiceprints"][0]["group_id"] == "grp_device_uuid"


def test_rekey_voiceprints_merges_when_resolved_label_already_exists():
    """RM_qs6YeUdkV7or: upgrading onto an existing grp_* that already has
    S1 must merge, not fail with a unique-constraint error."""
    session_id = "lily-D99BE7-69362716"
    resolved = "grp_f76e6116016497ba9245cd40f80a83dd14f8f50a"
    db = _SupabaseDB({
        "lily_sessions": [{"session_id": session_id, "group_id": session_id}],
        "lily_group_facts": [],
        "lily_asked_history": [],
        "lily_speaker_voiceprints": [
            {
                "id": 10,
                "group_id": resolved,
                "speaker_label": "S1",
                "player_name": "Rami",
                "speaker_identifiers": ["id-rami-old"],
                "sample_count": 2,
            },
            {
                "id": 11,
                "group_id": session_id,
                "speaker_label": "S1",
                "player_name": "Rami",
                "speaker_identifiers": ["id-rami-new"],
                "sample_count": 1,
            },
            {
                "id": 12,
                "group_id": session_id,
                "speaker_label": "S2",
                "player_name": "Rhonda",
                "speaker_identifiers": ["id-rhonda"],
                "sample_count": 1,
            },
        ],
        "lily_group_prefs": [],
    })
    asyncio.run(
        lily_persistence.lily_rekey_group(db, session_id, resolved, session_id)
    )
    rows = db.tables["lily_speaker_voiceprints"]
    by_label = {r["speaker_label"]: r for r in rows}
    assert set(by_label) == {"S1", "S2"}
    assert by_label["S1"]["group_id"] == resolved
    assert by_label["S1"]["id"] == 10  # kept the resolved row
    assert by_label["S1"]["speaker_identifiers"] == [
        "id-rami-old", "id-rami-new",
    ]
    assert by_label["S1"]["sample_count"] == 2
    assert by_label["S2"]["group_id"] == resolved
    assert by_label["S2"]["player_name"] == "Rhonda"


def test_rekey_group_keeps_voiceprints_on_non_provisional_old_id():
    db = _SupabaseDB({
        "lily_sessions": [{"session_id": "room-1", "group_id": "grp_device_old"}],
        "lily_group_facts": [],
        "lily_asked_history": [],
        "lily_speaker_voiceprints": [
            {
                "group_id": "grp_device_old",
                "speaker_label": "S1",
                "player_name": "Sarah",
                "speaker_identifiers": ["id-sarah"],
            }
        ],
        "lily_group_prefs": [],
    })
    asyncio.run(
        lily_persistence.lily_rekey_group(
            db, "grp_device_old", "grp_device_new", "room-1"
        )
    )
    # Conservative: non-provisional old ids do not move voiceprints.
    assert db.tables["lily_speaker_voiceprints"][0]["group_id"] == "grp_device_old"
    assert db.tables["lily_sessions"][0]["group_id"] == "grp_device_new"


def test_enroll_voiceprints_maps_case_variant_label_to_player_name():
    db = _SupabaseDB({"lily_speaker_voiceprints": []})
    stt = _FakeSTT(
        [{"label": "sarah", "speaker_identifiers": ["id-sarah-1", "id-sarah-2"]}]
    )
    scorekeeper = SimpleNamespace(
        players={
            "Sarah": {"speaker_label": "S1"},
            "Dave": {"speaker_label": "S2"},
        }
    )
    ok = asyncio.run(
        lily_persistence.lily_enroll_voiceprints(
            stt,
            db,
            "grp_x",
            scorekeeper,
            trigger="bind_refresh",
        )
    )
    assert ok is True
    rows = db.tables["lily_speaker_voiceprints"]
    assert len(rows) == 1
    assert rows[0]["player_name"] == "Sarah"


# ---------------------------------------------------------------------------
# Boot-time init retries (live 2026-07-15 22:38: one transient ReadTimeout
# at the early-row insert killed the job — Lily never joined the room)
# ---------------------------------------------------------------------------

import pytest as _pytest
import lily_persistence as _lp


class _FlakyInitTable:
    def __init__(self, fail_times: int):
        self.fail_times = fail_times
        self.calls = 0

    def upsert(self, payload, on_conflict=None):
        return self

    def execute(self):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise TimeoutError("The read operation timed out")
        return type("R", (), {"data": []})()


class _FlakyInitClient:
    def __init__(self, fail_times: int):
        self._table = _FlakyInitTable(fail_times)

    def table(self, name):
        return self._table


def test_init_session_survives_transient_timeouts(monkeypatch):
    monkeypatch.setattr(_lp.time, "sleep", lambda s: None)
    client = _FlakyInitClient(fail_times=2)
    _lp.lily_init_session(client, "room-x", "grp-x")  # no raise
    assert client._table.calls == 3


def test_init_session_still_fails_fast_when_db_is_down(monkeypatch):
    monkeypatch.setattr(_lp.time, "sleep", lambda s: None)
    client = _FlakyInitClient(fail_times=99)
    with _pytest.raises(RuntimeError):
        _lp.lily_init_session(client, "room-x", "grp-x")
    assert client._table.calls == 3
