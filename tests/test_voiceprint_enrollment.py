"""Voiceprint enrollment persistence tests.

Focus: returning-group partial overlap (existing table + one new guest) and
label->name durability when refreshes happen before rebinding catches up.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_memory
import lily_persistence


class _SpeakerIdentifier:
    def __init__(self, label, speaker_identifiers):
        self.label = label
        self.speaker_identifiers = speaker_identifiers


class _ConnectedStream:
    class _Client:
        _is_connected = True

    _client = _Client()


class _FakeSTT:
    def __init__(self, speaker_ids):
        self._speaker_ids = list(speaker_ids)
        self._streams = [_ConnectedStream()]

    async def get_speaker_ids(self):
        return list(self._speaker_ids)


class _FakeScorekeeper:
    def __init__(self, players):
        self.players = players


class _Result:
    def __init__(self, data=None):
        self.data = list(data or [])


class _Table:
    def __init__(self, db, name):
        self._db = db
        self._name = name
        self._op = None
        self._rows = []
        self._on_conflict = None
        self._select_cols = None
        self._eq_filters = []
        self._in_filters = []

    def upsert(self, rows, on_conflict=None):
        self._op = "upsert"
        self._rows = list(rows or [])
        self._on_conflict = on_conflict
        return self

    def select(self, cols, count=None):  # noqa: ARG002 - parity with client API
        self._op = "select"
        self._select_cols = [c.strip() for c in str(cols).split(",")]
        return self

    def eq(self, column, value):
        self._eq_filters.append((column, value))
        return self

    def in_(self, column, values):
        self._in_filters.append((column, set(values or [])))
        return self

    def execute(self):
        table = self._db.tables.setdefault(self._name, [])
        if self._op == "upsert":
            keys = [k.strip() for k in str(self._on_conflict or "").split(",") if k.strip()]
            for row in self._rows:
                candidate = dict(row or {})
                idx = None
                for i, existing in enumerate(table):
                    if keys and all(existing.get(k) == candidate.get(k) for k in keys):
                        idx = i
                        break
                if idx is None:
                    table.append(candidate)
                else:
                    table[idx].update(candidate)
            return _Result(self._rows)
        if self._op == "select":
            rows = []
            for existing in table:
                if any(existing.get(col) != val for col, val in self._eq_filters):
                    continue
                if any(existing.get(col) not in vals for col, vals in self._in_filters):
                    continue
                if self._select_cols:
                    rows.append({col: existing.get(col) for col in self._select_cols})
                else:
                    rows.append(dict(existing))
            return _Result(rows)
        raise AssertionError(f"unsupported operation {self._op!r}")


class _FakeSupabase:
    def __init__(self, tables=None):
        self.tables = {
            name: [dict(row) for row in rows]
            for name, rows in (tables or {}).items()
        }

    def table(self, name):
        return _Table(self, name)


def _players():
    return {
        "Sarah": {"speaker_label": "Sarah"},
        "Dave": {"speaker_label": "Dave"},
        "Priya": {"speaker_label": "S9"},
    }


def test_partial_overlap_group_plus_new_guest_enrolls_and_matches_next_visit():
    db = _FakeSupabase({
        "lily_speaker_voiceprints": [
            {
                "group_id": "grp_table",
                "speaker_label": "Sarah",
                "player_name": "Sarah",
                "speaker_identifiers": ["id-sarah-0"],
            },
            {
                "group_id": "grp_table",
                "speaker_label": "Dave",
                "player_name": "Dave",
                "speaker_identifiers": ["id-dave-0"],
            },
        ]
    })
    stt = _FakeSTT([
        _SpeakerIdentifier("Sarah", ["id-sarah-1"]),
        _SpeakerIdentifier("Dave", ["id-dave-1"]),
        _SpeakerIdentifier("S9", ["id-priya-1"]),
    ])
    sk = _FakeScorekeeper(_players())

    wrote = asyncio.run(lily_persistence.lily_enroll_voiceprints(
        stt, db, "grp_table", sk, trigger="test_partial_overlap"
    ))
    assert wrote is True
    priya_row = next(
        r for r in db.tables["lily_speaker_voiceprints"]
        if r["group_id"] == "grp_table" and r["speaker_label"] == "S9"
    )
    assert priya_row["player_name"] == "Priya"
    assert priya_row["speaker_identifiers"] == ["id-priya-1"]

    stored = asyncio.run(lily_persistence.lily_load_voiceprints_by_players(
        db, ["Dave", "Priya"]
    ))
    # Next visit can still resolve the durable group id on partial-overlap
    # rosters, including the newly joined guest.
    matched = lily_memory.lily_match_group_by_voiceprints(
        [_SpeakerIdentifier("who", ["id-priya-1"])],
        stored,
    )
    assert matched == "grp_table"


def test_enroll_refresh_preserves_existing_player_name_when_unbound():
    db = _FakeSupabase({
        "lily_speaker_voiceprints": [
            {
                "group_id": "grp_table",
                "speaker_label": "S9",
                "player_name": "Priya",
                "speaker_identifiers": ["id-priya-old"],
            },
        ]
    })
    stt = _FakeSTT([_SpeakerIdentifier("S9", ["id-priya-new"])])
    sk = _FakeScorekeeper({
        # S9 is currently unbound in this refresh pass.
        "Dave": {"speaker_label": "Dave"},
    })

    wrote = asyncio.run(lily_persistence.lily_enroll_voiceprints(
        stt, db, "grp_table", sk, trigger="test_preserve_name"
    ))
    assert wrote is True
    row = db.tables["lily_speaker_voiceprints"][0]
    assert row["speaker_label"] == "S9"
    assert row["player_name"] == "Priya"
    assert row["speaker_identifiers"] == ["id-priya-new"]
