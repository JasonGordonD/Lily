"""Tests for WO-LILY-FORGETME-001 pure logic (lily_forget) + the cascade
executor (lily_persistence.lily_forget_group_data against a fake postgrest
client). No livekit, no network.

Covers the WO's offline verification list:
  - cascade plan builders (per-table statements + tombstone)
  - the cascade executor: hard deletes, session-keyed deletes, optional
    lily_asked_history skip, tombstone re-key, verification, isolation of
    other groups, honest partial failure
  - yes/no confirmation resolution (pending-confirm state)
  - explain-memory shapes (cold / warm / unreadable) — counts only,
    vendor/table names never spoken
  - disclosure cap logic (first rematch, then every 5th)
(Spoken "forget me" command detection lives in tests/test_commands.py with
the rest of the command layer.)
"""

import asyncio
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_forget
from lily_forget import (
    FORGET_TOMBSTONE_PREFIX,
    lily_build_forget_plan,
    lily_explain_memory_result,
    lily_forget_result_message,
    lily_is_absent_table_error,
    lily_parse_forget_confirmation,
    lily_should_disclose_memory,
    lily_tombstone_group_id,
)


# ---------------------------------------------------------------------------
# Tombstone
# ---------------------------------------------------------------------------

def test_tombstone_shape_and_determinism():
    t = lily_tombstone_group_id("grp_abc123")
    assert t.startswith(FORGET_TOMBSTONE_PREFIX)
    suffix = t[len(FORGET_TOMBSTONE_PREFIX):]
    assert len(suffix) == 12
    assert all(c in "0123456789abcdef" for c in suffix)
    # sha1-12 of the OLD group id, deterministic (a retry re-keys to the
    # SAME tombstone).
    expected = hashlib.sha1(b"grp_abc123").hexdigest()[:12]
    assert suffix == expected
    assert lily_tombstone_group_id("grp_abc123") == t
    assert lily_tombstone_group_id("other-group") != t


# ---------------------------------------------------------------------------
# Cascade plan builder
# ---------------------------------------------------------------------------

def test_plan_covers_every_table_with_correct_keys():
    plan = lily_build_forget_plan("g1", ["room-2", "room-1", "room-2"])
    by_table = {op["table"]: op for op in plan}
    # Hard deletes keyed by group_id
    for table in ("lily_speaker_voiceprints", "lily_memories", "lily_group_facts"):
        op = by_table[table]
        assert op["action"] == "delete"
        assert op["column"] == "group_id"
        assert op["values"] == ["g1"]
        assert op["optional"] is False
    # Session-keyed deletes (no group_id column on these tables) — deduped,
    # sorted session ids
    for table in ("lily_addressee_log", "lily_acoustic_trajectories"):
        op = by_table[table]
        assert op["action"] == "delete"
        assert op["column"] == "session_id"
        assert op["values"] == ["room-1", "room-2"]
        assert op["optional"] is False
    # Optional future table — skipped gracefully when absent
    assert by_table["lily_asked_history"]["optional"] is True
    assert by_table["lily_asked_history"]["column"] == "group_id"
    # Re-key, never delete: lily_sessions -> tombstone
    rekey = by_table["lily_sessions"]
    assert rekey["action"] == "rekey"
    assert rekey["new_group_id"] == lily_tombstone_group_id("g1")
    # Deletes run BEFORE the re-key (a timeout can never strand
    # identity-bearing rows behind a moved key).
    assert plan[-1]["table"] == "lily_sessions"
    # lily_answers is RETAINED (no group_id column, migration 001) — it
    # must never appear in the plan.
    assert "lily_answers" not in by_table
    assert "lily_answers" in lily_forget.RETAINED_SESSION_KEYED_TABLES


def test_plan_tolerates_empty_sessions():
    plan = lily_build_forget_plan("g1", [])
    by_table = {op["table"]: op for op in plan}
    assert by_table["lily_addressee_log"]["values"] == []
    assert by_table["lily_sessions"]["action"] == "rekey"


def test_absent_table_error_matcher():
    assert lily_is_absent_table_error('relation "lily_asked_history" does not exist')
    assert lily_is_absent_table_error("42P01: undefined table")
    assert lily_is_absent_table_error(
        "PGRST205: Could not find the table 'public.lily_asked_history'"
    )
    assert not lily_is_absent_table_error("permission denied for table")
    assert not lily_is_absent_table_error("")


# ---------------------------------------------------------------------------
# Fake postgrest client — enough of the chain for the cascade executor
# ---------------------------------------------------------------------------

class _FakeResult:
    def __init__(self, data=None, count=None):
        self.data = data
        self.count = count


class _FakeQuery:
    def __init__(self, db, table):
        self._db = db
        self._table = table
        self._action = "select"
        self._payload = None
        self._filters = []
        self._count = None
        self._limit = None

    def select(self, *_cols, count=None):
        self._action = "select"
        self._count = count
        return self

    def delete(self):
        self._action = "delete"
        return self

    def update(self, payload):
        self._action = "update"
        self._payload = payload
        return self

    def eq(self, col, val):
        self._filters.append(lambda row: row.get(col) == val)
        return self

    def in_(self, col, vals):
        vals = list(vals)
        self._filters.append(lambda row: row.get(col) in vals)
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, n):
        self._limit = n
        return self

    def _matches(self):
        rows = self._db.tables[self._table]
        return [r for r in rows if all(f(r) for f in self._filters)]

    def execute(self):
        if self._table not in self._db.tables:
            raise Exception(
                f'relation "public.{self._table}" does not exist (42P01)'
            )
        matched = self._matches()
        if self._action == "delete":
            self._db.tables[self._table] = [
                r for r in self._db.tables[self._table] if r not in matched
            ]
            return _FakeResult(data=list(matched))
        if self._action == "update":
            for r in matched:
                r.update(self._payload)
            return _FakeResult(data=list(matched))
        data = matched[: self._limit] if self._limit is not None else matched
        count = len(matched) if self._count else None
        return _FakeResult(data=list(data), count=count)


class _FakeSupabase:
    def __init__(self, tables):
        self.tables = {name: [dict(r) for r in rows] for name, rows in tables.items()}

    def table(self, name):
        return _FakeQuery(self, name)


def _seed(with_asked_history=False):
    tables = {
        "lily_sessions": [
            {"session_id": "room-1", "group_id": "gA"},
            {"session_id": "room-2", "group_id": "gA"},
            {"session_id": "room-x", "group_id": "gB"},
        ],
        "lily_speaker_voiceprints": [
            {"group_id": "gA", "speaker_label": "S1"},
            {"group_id": "gA", "speaker_label": "S2"},
            {"group_id": "gB", "speaker_label": "S1"},
        ],
        "lily_memories": [
            {"group_id": "gA", "session_id": "room-1"},
            {"group_id": "gB", "session_id": "room-x"},
        ],
        "lily_group_facts": [
            {"group_id": "gA", "fact": "owns 40 typewriters"},
        ],
        "lily_addressee_log": [
            {"session_id": "room-1", "transcript": "tungsten"},
            {"session_id": "room-2", "transcript": "the bosporus"},
            {"session_id": "room-x", "transcript": "other group"},
        ],
        "lily_acoustic_trajectories": [
            {"session_id": "room-2", "turn_index": 1},
            {"session_id": "room-x", "turn_index": 1},
        ],
        "lily_answers": [
            {"session_id": "room-1", "verdict": "correct"},
        ],
    }
    if with_asked_history:
        tables["lily_asked_history"] = [
            {"group_id": "gA", "question_id": "kb_1"},
            {"group_id": "gB", "question_id": "kb_2"},
        ]
    return _FakeSupabase(tables)


def _run_cascade(db, group_id="gA", session_id="room-2"):
    import lily_persistence
    return asyncio.run(
        lily_persistence.lily_forget_group_data(db, group_id, session_id)
    )


def test_cascade_deletes_verifies_and_tombstones():
    db = _seed()
    result = _run_cascade(db)
    assert result["ok"] is True
    assert result["timed_out"] is False
    # Hard deletes with honest per-table counts
    assert result["deleted"] == {
        "lily_speaker_voiceprints": 2,
        "lily_memories": 1,
        "lily_group_facts": 1,
        "lily_addressee_log": 2,
        "lily_acoustic_trajectories": 1,
    }
    # lily_asked_history isn't applied yet -> skipped, never failed
    assert result["skipped"] == ["lily_asked_history"]
    assert result["failed"] == {}
    # Re-key: operational records survive without linkable identity
    tomb = lily_tombstone_group_id("gA")
    assert result["tombstone"] == tomb
    assert result["rekeyed"] == {"lily_sessions": 2}
    assert {r["group_id"] for r in db.tables["lily_sessions"]} == {tomb, "gB"}
    # Every touched table verified (count queries came back 0)
    assert set(result["verified"]) == {
        "lily_speaker_voiceprints", "lily_memories", "lily_group_facts",
        "lily_addressee_log", "lily_acoustic_trajectories", "lily_sessions",
    }
    # Other groups untouched
    assert db.tables["lily_speaker_voiceprints"] == [
        {"group_id": "gB", "speaker_label": "S1"}
    ]
    assert db.tables["lily_memories"] == [{"group_id": "gB", "session_id": "room-x"}]
    assert [r["session_id"] for r in db.tables["lily_addressee_log"]] == ["room-x"]
    # lily_answers RETAINED untouched (session-keyed, no group_id column)
    assert db.tables["lily_answers"] == [{"session_id": "room-1", "verdict": "correct"}]


def test_cascade_deletes_asked_history_when_present():
    db = _seed(with_asked_history=True)
    result = _run_cascade(db)
    assert result["ok"] is True
    assert result["skipped"] == []
    assert result["deleted"]["lily_asked_history"] == 1
    assert db.tables["lily_asked_history"] == [
        {"group_id": "gB", "question_id": "kb_2"}
    ]


def test_cascade_includes_current_session_even_if_unlisted():
    # The current session may not be under the group in lily_sessions yet
    # (mid-upgrade edge) — its session-keyed rows must still die.
    db = _seed()
    db.tables["lily_sessions"] = [
        {"session_id": "room-1", "group_id": "gA"},
        {"session_id": "room-x", "group_id": "gB"},
    ]
    result = _run_cascade(db, session_id="room-2")
    assert result["deleted"]["lily_addressee_log"] == 2  # room-1 AND room-2
    assert result["deleted"]["lily_acoustic_trajectories"] == 1


def test_cascade_partial_failure_is_honest_and_names_tables():
    db = _seed()

    class _FailingSupabase(_FakeSupabase):
        def table(self, name):
            if name == "lily_memories":
                raise Exception("connection reset by peer")
            return super().table(name)

    failing = _FailingSupabase(db.tables)
    result = _run_cascade(failing)
    assert result["ok"] is False
    assert "lily_memories" in result["failed"]
    # Everything else still ran and verified
    assert "lily_speaker_voiceprints" in result["verified"]
    assert "lily_sessions" in result["rekeyed"] or "lily_sessions" in result["failed"]
    # The message layer names succeeded AND failed tables, honestly
    msg = lily_forget_result_message(result)
    assert "PARTIAL" in msg
    assert "lily_speaker_voiceprints" in msg
    assert "lily_memories" in msg
    assert "again" in msg  # the retry offer


def test_cascade_refuses_without_client():
    import lily_persistence
    result = asyncio.run(
        lily_persistence.lily_forget_group_data(None, "gA", "room-1")
    )
    assert result["ok"] is False


# ---------------------------------------------------------------------------
# Yes/no confirmation resolution (pending-confirm state)
# ---------------------------------------------------------------------------

def test_confirmation_yes_variants():
    for reply in (
        "yes", "Yes!", "yeah", "yep", "yup", "sure", "absolutely",
        "do it", "go ahead", "please do", "delete it", "delete it all",
        "wipe it", "erase it", "confirmed", "[S1] yes, do it",
        "I'm sure", "we're sure", "yes please",
    ):
        assert lily_parse_forget_confirmation(reply) == "yes", reply


def test_confirmation_no_variants():
    for reply in (
        "no", "No.", "nope", "nah", "never mind", "nevermind", "cancel",
        "stop", "wait", "hold on", "don't", "keep it", "keep everything",
        "leave it", "changed my mind", "just kidding", "[S2] no no no",
    ):
        assert lily_parse_forget_confirmation(reply) == "no", reply


def test_confirmation_ambiguous_is_never_destructive():
    # Fires both directions, or neither -> None (stays pending; nothing
    # is deleted on ambiguity).
    for reply in (
        "yes wait no", "no... actually yes", "what does that mean",
        "how much do you remember", "", "   ", "tungsten",
    ):
        assert lily_parse_forget_confirmation(reply) is None, reply


# ---------------------------------------------------------------------------
# Tool-result / instructed-reply shapes
# ---------------------------------------------------------------------------

def test_result_message_success_names_counts_and_the_warm_line():
    msg = lily_forget_result_message({
        "ok": True,
        "deleted": {"lily_speaker_voiceprints": 3, "lily_memories": 5},
        "rekeyed": {"lily_sessions": 5},
        "skipped": ["lily_asked_history"],
        "failed": {},
    })
    assert "verified" in msg.lower()
    assert "lily_speaker_voiceprints: 3 deleted" in msg
    assert "lily_sessions: 5 re-keyed to tombstone" in msg
    assert "warm" in msg
    assert "zero mourning" in msg
    assert "game" in msg  # tonight's game keeps going


def test_result_message_already_done_and_in_progress():
    assert "Already forgotten" in lily_forget_result_message(
        {"ok": True, "already_done": True}
    )
    assert "already running" in lily_forget_result_message(
        {"ok": False, "in_progress": True}
    )


# ---------------------------------------------------------------------------
# lily_explain_memory shapes
# ---------------------------------------------------------------------------

def test_explain_memory_cold_shape():
    msg = lily_explain_memory_result(
        {"voiceprints": 0, "games": 0, "last_played_at": None, "facts": 0},
        "room_name",
    )
    assert "Nothing on file" in msg
    assert "clean slate" in msg
    assert "nothing to forget yet" in msg


def test_explain_memory_warm_shape_counts_only():
    msg = lily_explain_memory_result(
        {
            "voiceprints": 3,
            "games": 5,
            "last_played_at": "2026-07-01T22:15:00+00:00",
            "facts": 4,
        },
        "participant_metadata",
    )
    assert "3 voice(s)" in msg
    assert "5 game(s)" in msg
    assert "last played 2026-07-01" in msg
    assert "4 fact(s)" in msg
    assert "device" in msg           # how recognition happened this session
    assert "forget me" in msg        # the standing offer rides along
    assert "never read raw contents" in msg


def test_explain_memory_sources_never_name_vendors_or_tables():
    for source in (
        "participant_metadata", "dispatch_metadata",
        "participant_metadata_late", "voiceprint_match", "name_set_hash",
        "env_override", "room_name", "post_forget_anonymous", "unknown_src",
    ):
        msg = lily_explain_memory_result(
            {"voiceprints": 1, "games": 1, "last_played_at": None, "facts": 0},
            source,
        ).lower()
        for forbidden in (
            "speechmatics", "supabase", "postgres", "elevenlabs", "gemini",
            "lily_speaker_voiceprints", "lily_memories", "lily_group_facts",
        ):
            assert forbidden not in msg, (source, forbidden)


def test_explain_memory_recognition_source_this_session():
    voice = lily_explain_memory_result(
        {"voiceprints": 1, "games": 1, "last_played_at": None, "facts": 0},
        "voiceprint_match",
    )
    assert "voice" in voice.lower()
    fresh = lily_explain_memory_result(
        {"voiceprints": 0, "games": 0, "last_played_at": None, "facts": 0},
        "post_forget_anonymous",
    )
    assert "deleted this session" in fresh


def test_explain_memory_unreadable_is_honest():
    msg = lily_explain_memory_result(None, "participant_metadata")
    assert "Could not read" in msg
    assert "never guess" in msg


# ---------------------------------------------------------------------------
# Disclosure cap (Task 4)
# ---------------------------------------------------------------------------

def test_disclosure_cap_first_rematch_then_every_fifth():
    assert lily_should_disclose_memory(0) is False   # cold group: nothing
    assert lily_should_disclose_memory(None) is False
    assert lily_should_disclose_memory(1) is True    # first rematch
    for games in (2, 3, 4, 6, 7, 8, 9, 11):
        assert lily_should_disclose_memory(games) is False, games
    for games in (5, 10, 15, 20):
        assert lily_should_disclose_memory(games) is True, games
