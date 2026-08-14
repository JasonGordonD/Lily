"""Group reconciliation — auto-merge of fragmented returning-player identity
groups (WO-PRMPT-LILY-GROUP-RECONCILE-001).

Covers lily_merge_groups (re-key across the group-keyed tables, collision
handling on the three unique-constrained tables, session-keyed retention,
audit shape), the individual-level safety bar, and the name-fragment scan.

Same self-contained fake-postgrest idiom as test_forget.py — a richer chain
(upsert/on_conflict, contains, count) because the merge leans on those.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_persistence


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Fake postgrest client — select/eq/in_/contains/order/limit/range +
# update/delete/upsert(on_conflict). Enough of the chain for the reconciler.
# ---------------------------------------------------------------------------

class _Result:
    def __init__(self, data=None, count=None):
        self.data = data
        self.count = count


class _Query:
    def __init__(self, db, table):
        self._db = db
        self._table = table
        self._action = "select"
        self._payload = None
        self._on_conflict = None
        self._filters = []
        self._count = None
        self._limit = None
        self._order = None
        self._desc = False

    def select(self, *_cols, count=None):
        self._action = "select"
        self._count = count
        return self

    def insert(self, payload):
        self._action = "insert"
        self._payload = payload
        return self

    def update(self, payload):
        self._action = "update"
        self._payload = payload
        return self

    def upsert(self, payload, on_conflict=None):
        self._action = "upsert"
        self._payload = payload
        self._on_conflict = on_conflict
        return self

    def delete(self):
        self._action = "delete"
        return self

    def eq(self, col, val):
        self._filters.append(lambda r: r.get(col) == val)
        return self

    def in_(self, col, vals):
        vals = list(vals)
        self._filters.append(lambda r: r.get(col) in vals)
        return self

    def contains(self, col, needle):
        needle = list(needle)
        self._filters.append(
            lambda r: all(n in (r.get(col) or []) for n in needle)
        )
        return self

    def order(self, col, desc=False):
        self._order = col
        self._desc = desc
        return self

    def limit(self, n):
        self._limit = n
        return self

    def range(self, start, end):
        self._range = (start, end)
        return self

    def _matches(self):
        rows = self._db.tables.get(self._table, [])
        return [r for r in rows if all(f(r) for f in self._filters)]

    def execute(self):
        if self._table not in self._db.tables:
            raise Exception(
                f'relation "public.{self._table}" does not exist (42P01)'
            )
        rows = self._db.tables[self._table]
        if self._action == "insert":
            items = self._payload if isinstance(self._payload, list) else [self._payload]
            for it in items:
                rows.append(dict(it))
            return _Result(data=list(items))
        if self._action == "upsert":
            key = self._on_conflict
            payload = self._payload
            existing = None
            if key:
                existing = next(
                    (r for r in rows if r.get(key) == payload.get(key)), None
                )
            if existing is not None:
                existing.update(payload)
                return _Result(data=[existing])
            rows.append(dict(payload))
            return _Result(data=[payload])

        matched = self._matches()
        if self._action == "delete":
            self._db.tables[self._table] = [r for r in rows if r not in matched]
            return _Result(data=list(matched))
        if self._action == "update":
            for r in matched:
                r.update(self._payload)
            return _Result(data=list(matched))
        # select
        if self._order:
            matched = sorted(
                matched, key=lambda r: str(r.get(self._order) or ""),
                reverse=self._desc,
            )
        count = len(matched) if self._count == "exact" else None
        if self._limit is not None:
            matched = matched[: self._limit]
        return _Result(data=list(matched), count=count)


class _Supabase:
    def __init__(self, tables):
        self.tables = {n: [dict(r) for r in rows] for n, rows in tables.items()}

    def table(self, name):
        return _Query(self, name)


CANON = "gA"
DUP = "gB"


def _seed():
    return _Supabase({
        "lily_sessions": [
            {"session_id": "room-1", "group_id": CANON},
            {"session_id": "room-2", "group_id": DUP},
            {"session_id": "room-3", "group_id": DUP},
        ],
        "lily_memories": [
            {"group_id": CANON, "session_id": "room-1", "player_names": ["Rami"],
             "played_at": "2026-08-10", "question_count": 5},
            {"group_id": DUP, "session_id": "room-2", "player_names": ["Rami"],
             "played_at": "2026-08-12", "question_count": 3},
        ],
        "lily_group_facts": [
            {"group_id": DUP, "fact": "owns 40 typewriters"},
        ],
        "lily_asked_history": [
            {"group_id": DUP, "question_id": "kb_1"},
            {"group_id": CANON, "question_id": "kb_2"},
        ],
        "lily_speaker_voiceprints": [
            # Collision on S1, clean move for S2.
            {"id": 1, "group_id": CANON, "speaker_label": "S1",
             "player_name": "Rami", "speaker_identifiers": ["a"]},
            {"id": 2, "group_id": DUP, "speaker_label": "S1",
             "player_name": "Rami", "speaker_identifiers": ["b"]},
            {"id": 3, "group_id": DUP, "speaker_label": "S2",
             "player_name": None, "speaker_identifiers": ["c"]},
        ],
        "lily_group_prefs": [
            {"group_id": CANON, "prefs": {"pacing": "relaxed"},
             "updated_at": "2026-08-10"},
            {"group_id": DUP, "prefs": {"pacing": "timed", "deck": "adult"},
             "updated_at": "2026-08-12"},
        ],
        "lily_voice_identity": [
            {"id": "v1", "group_id": CANON, "model_tag": "ecapa-192-v1",
             "status": "active", "updated_at": "2026-08-10"},
            {"id": "v2", "group_id": DUP, "model_tag": "ecapa-192-v1",
             "status": "active", "updated_at": "2026-08-12"},
        ],
        # Session-keyed (no group_id) — must be RETAINED, never touched.
        "lily_transcripts": [
            {"session_id": "room-2", "text": "private"},
        ],
        "lily_answers": [
            {"session_id": "room-2", "verdict": "correct"},
        ],
    })


def _by(db, table, **eqs):
    return [
        r for r in db.tables[table]
        if all(r.get(k) == v for k, v in eqs.items())
    ]


# -- lily_merge_groups --------------------------------------------------------

def test_merge_rekeys_plain_group_tables_onto_canonical():
    db = _seed()
    res = _run(lily_persistence.lily_merge_groups(
        db, CANON, [DUP], reason="test"))
    assert res["ok"] is True
    # No row left under the duplicate on any group-keyed table.
    for table in ("lily_sessions", "lily_memories", "lily_group_facts",
                  "lily_asked_history", "lily_speaker_voiceprints",
                  "lily_group_prefs", "lily_voice_identity"):
        assert _by(db, table, group_id=DUP) == [], f"{table} still has dup rows"
    # Canonical gained the moved rows.
    assert len(_by(db, "lily_sessions", group_id=CANON)) == 3
    assert len(_by(db, "lily_memories", group_id=CANON)) == 2
    assert len(_by(db, "lily_group_facts", group_id=CANON)) == 1


def test_merge_never_deletes_rows_it_only_rekeys():
    db = _seed()
    before = sum(len(v) for v in db.tables.values())
    _run(lily_persistence.lily_merge_groups(db, CANON, [DUP], reason="test"))
    after = sum(len(v) for v in db.tables.values())
    # Two collisions remove exactly two loser rows (voiceprint S1, prefs);
    # voice_identity RETIRES (keeps the row). Everything else is a pure rekey.
    assert before - after == 2


def test_merge_voiceprint_collision_merges_identifiers_keeps_one_row():
    db = _seed()
    _run(lily_persistence.lily_merge_groups(db, CANON, [DUP], reason="test"))
    s1 = _by(db, "lily_speaker_voiceprints", group_id=CANON, speaker_label="S1")
    assert len(s1) == 1
    assert set(s1[0]["speaker_identifiers"]) == {"a", "b"}
    # The clean S2 row moved with no collision.
    assert len(_by(db, "lily_speaker_voiceprints", group_id=CANON,
                   speaker_label="S2")) == 1


def test_merge_prefs_collision_merges_dicts_newest_wins():
    db = _seed()
    res = _run(lily_persistence.lily_merge_groups(
        db, CANON, [DUP], reason="test"))
    prefs = _by(db, "lily_group_prefs", group_id=CANON)
    assert len(prefs) == 1
    # Union of keys; DUP is newer so it wins the 'pacing' conflict.
    assert prefs[0]["prefs"] == {"pacing": "timed", "deck": "adult"}
    # Superseded fragment row content is preserved in the audit.
    coll = res["merges"][0]["collisions"]["lily_group_prefs"]
    assert coll and coll[0]["group_id"] == DUP


def test_merge_voice_identity_collision_retires_older_keeps_newest():
    db = _seed()
    res = _run(lily_persistence.lily_merge_groups(
        db, CANON, [DUP], reason="test"))
    active = _by(db, "lily_voice_identity", group_id=CANON, status="active")
    assert len(active) == 1
    # DUP centroid is newer -> it is the survivor under canonical.
    assert active[0]["id"] == "v2"
    retired = [r for r in db.tables["lily_voice_identity"]
               if r["status"] == "retired"]
    assert len(retired) == 1 and retired[0]["id"] == "v1"
    # Nothing deleted from this table.
    assert len(db.tables["lily_voice_identity"]) == 2
    assert res["merges"][0]["collisions"]["lily_voice_identity"]


def test_merge_leaves_session_keyed_rows_untouched():
    db = _seed()
    res = _run(lily_persistence.lily_merge_groups(
        db, CANON, [DUP], reason="test"))
    # Rows keyed by session_id have no group_id — retained on historical ids.
    assert db.tables["lily_transcripts"][0]["session_id"] == "room-2"
    assert db.tables["lily_answers"][0]["session_id"] == "room-2"
    assert "lily_transcripts" in res["merges"][0]["session_keyed_retained"]


def test_merge_is_idempotent_second_run_is_a_noop():
    db = _seed()
    _run(lily_persistence.lily_merge_groups(db, CANON, [DUP], reason="test"))
    res2 = _run(lily_persistence.lily_merge_groups(
        db, CANON, [DUP], reason="test"))
    assert res2["ok"] is True
    for table in db.tables:
        assert _by(db, table, group_id=DUP) == []


def test_merge_skips_canonical_in_duplicate_list():
    db = _seed()
    res = _run(lily_persistence.lily_merge_groups(
        db, CANON, [CANON, DUP], reason="test"))
    # The canonical id is dropped; only the real fragment is merged.
    assert [m["duplicate_group_id"] for m in res["merges"]] == [DUP]


def test_merge_tolerates_absent_optional_table():
    db = _seed()
    del db.tables["lily_asked_history"]  # migration 010 not yet applied
    res = _run(lily_persistence.lily_merge_groups(
        db, CANON, [DUP], reason="test"))
    assert res["ok"] is True
    assert "lily_asked_history" in res["merges"][0]["skipped"]


def test_merge_no_client_or_empty_canonical_is_safe():
    assert _run(lily_persistence.lily_merge_groups(
        None, CANON, [DUP], reason="t"))["ok"] is False
    db = _seed()
    assert _run(lily_persistence.lily_merge_groups(
        db, "", [DUP], reason="t"))["ok"] is False


# -- safety bar ---------------------------------------------------------------

def test_safety_bar_voice_link_allows():
    assert lily_persistence.lily_reconcile_safety_bar(
        CANON, {"group_id": DUP, "player_names": ["Rami"]},
        voice_linked=True) is True


def test_safety_bar_same_device_allows():
    assert lily_persistence.lily_reconcile_safety_bar(
        CANON, {"group_id": DUP, "player_names": ["Rami"]},
        same_device_key=True) is True


def test_safety_bar_single_player_name_plus_voice_match_allows():
    assert lily_persistence.lily_reconcile_safety_bar(
        CANON, {"group_id": DUP, "player_names": ["Rami"],
                "voice_match": True}) is True


def test_safety_bar_name_only_refuses():
    # Sole name matches but no voice confirmation — MERGE_CANDIDATE, not merge.
    assert lily_persistence.lily_reconcile_safety_bar(
        CANON, {"group_id": DUP, "player_names": ["Rami"],
                "voice_match": False}) is False


def test_safety_bar_multi_player_group_never_merges_on_name():
    # A collection (two names) is a different thing from an individual.
    assert lily_persistence.lily_reconcile_safety_bar(
        CANON, {"group_id": DUP, "player_names": ["Rami", "Carly"],
                "voice_match": True}) is False


def test_safety_bar_rejects_self():
    assert lily_persistence.lily_reconcile_safety_bar(
        CANON, {"group_id": CANON, "player_names": ["Rami"]},
        voice_linked=True) is False


# -- fragment scan ------------------------------------------------------------

def test_find_name_fragments_single_player_only_excludes_canonical():
    db = _Supabase({
        "lily_memories": [
            {"group_id": CANON, "player_names": ["Rami"], "played_at": "2026-08-10",
             "question_count": 5},
            {"group_id": "gFrag1", "player_names": ["Rami"], "played_at": "2026-08-12",
             "question_count": 3},
            {"group_id": "gFrag2", "player_names": ["Rami"], "played_at": "2026-08-11",
             "question_count": 2},
            # Multi-player group sharing the name is a DIFFERENT collection.
            {"group_id": "gMulti", "player_names": ["Rami", "Carly"],
             "played_at": "2026-08-13", "question_count": 9},
        ],
    })
    frags = _run(lily_persistence.lily_find_name_fragments(db, CANON, "Rami"))
    ids = {f["group_id"] for f in frags}
    assert ids == {"gFrag1", "gFrag2"}
    assert CANON not in ids
    assert "gMulti" not in ids


def test_find_name_fragments_empty_for_unknown_name():
    db = _Supabase({"lily_memories": []})
    assert _run(lily_persistence.lily_find_name_fragments(db, CANON, "Nobody")) == []
