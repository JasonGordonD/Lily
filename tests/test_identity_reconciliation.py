"""WS-8 — identity reconciliation + ghost-label posture.

Evidence session `lily-81BCB0-583a0f16` (2026-08-05, 4-player echo room):
the diarizer split Chris across S1 and S4, S1 stayed unrostered holding
Chris's self-introduction ("Hi, my name is Chris") and his Oz answer
("The Wizard of Oz"), the voiceprint table carried duplicate Chris rows
(S1->Chris stale + S4->Chris fresh), and echo phantoms S5-S7 absorbed
copies of real answers ("Mark" spoken by Rhonda, echoed by S5). Lily
promised to "ignore those extra speaker tags" — a lever she does not hold.

Three deliverable groups pinned here:

  * merge transaction — one reconciliation across roster + voiceprints,
    retro-attributing the merged label's prior utterances, deduped to one
    row per player per group;
  * ghost-label posture — an unbound single-utterance label duplicating a
    bound player's just-recorded answer within the echo window folds
    instead of scoring (WS-13's per-word volume signal pluggable);
  * enrollment — a bound player below the ~5-word floor is surfaced so the
    multi-trigger schedule keeps retrying instead of silently never
    enrolling.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_persistence
from lily_scorekeeper import LilyScorekeeper


# --- record subset: the S1/S4 = Chris fragment of lily-81BCB0-583a0f16 ---
# Exported from lily_transcripts / lily_speaker_voiceprints so the replay
# runs with no network.
FIXTURE = {
    "session_id": "lily-81BCB0-583a0f16",
    "group_id": "41dfc215-71f0-4b79-a6e9-671d6b085f75",
    "transcripts": [
        {"speaker_label": "S1", "speaker_name": None, "text": "Hi, my name is Chris."},
        {"speaker_label": "S4", "speaker_name": "Chris", "text": "Pacific ocean."},
        {"speaker_label": "S1", "speaker_name": None, "text": "The Wizard of Oz."},
    ],
    "addressee_log": [
        {"speaker_label": "S1", "player_name": None, "transcript": "Hi, my name is Chris."},
        {"speaker_label": "S1", "player_name": None, "transcript": "The Wizard of Oz."},
    ],
    "voiceprints": [
        # Stale July row for S1 under the group (updated_at bumped by the
        # idempotent upsert) — the duplicate Chris row.
        {
            "id": 13, "group_id": "41dfc215-71f0-4b79-a6e9-671d6b085f75",
            "speaker_label": "S1", "player_name": "Chris",
            "speaker_identifiers": ["id-s1-old"],
            "updated_at": "2026-08-05T22:51:59.113414+00:00",
        },
        # This session's fresh S4 = Chris row.
        {
            "id": 86, "group_id": "41dfc215-71f0-4b79-a6e9-671d6b085f75",
            "speaker_label": "S4", "player_name": "Chris",
            "speaker_identifiers": ["id-s4-fresh"],
            "updated_at": "2026-08-05T22:51:59.175723+00:00",
        },
    ],
}


# ---------------------------------------------------------------------------
# Fake PostgREST client — update / delete / select / upsert with eq/in_.
# ---------------------------------------------------------------------------

class _Result:
    def __init__(self, data=None):
        self.data = list(data or [])


class _Query:
    def __init__(self, db, name):
        self._db = db
        self._name = name
        self._op = None
        self._payload = None
        self._on_conflict = None
        self._select_cols = None
        self._eq = []
        self._in = []

    def update(self, payload):
        self._op, self._payload = "update", dict(payload)
        return self

    def delete(self):
        self._op = "delete"
        return self

    def upsert(self, rows, on_conflict=None):
        self._op = "upsert"
        self._payload = [dict(r) for r in rows]
        self._on_conflict = on_conflict
        return self

    def select(self, cols, count=None):  # noqa: ARG002
        self._op = "select"
        self._select_cols = [c.strip() for c in str(cols).split(",")]
        return self

    def eq(self, col, val):
        self._eq.append((col, val))
        return self

    def in_(self, col, vals):
        self._in.append((col, set(vals or [])))
        return self

    def _match(self, row):
        if any(row.get(c) != v for c, v in self._eq):
            return False
        if any(row.get(c) not in vs for c, vs in self._in):
            return False
        return True

    def execute(self):
        table = self._db.tables.setdefault(self._name, [])
        if self._op == "select":
            out = []
            for row in table:
                if not self._match(row):
                    continue
                if self._select_cols and self._select_cols != ["*"]:
                    out.append({c: row.get(c) for c in self._select_cols})
                else:
                    out.append(dict(row))
            return _Result(out)
        if self._op == "update":
            n = 0
            for row in table:
                if self._match(row):
                    row.update(self._payload)
                    n += 1
            return _Result([{"count": n}])
        if self._op == "delete":
            keep, removed = [], []
            for row in table:
                (removed if self._match(row) else keep).append(row)
            self._db.tables[self._name] = keep
            return _Result(removed)
        if self._op == "upsert":
            keys = [k.strip() for k in str(self._on_conflict or "").split(",") if k.strip()]
            for cand in self._payload:
                idx = next(
                    (i for i, ex in enumerate(table)
                     if keys and all(ex.get(k) == cand.get(k) for k in keys)),
                    None,
                )
                if idx is None:
                    table.append(dict(cand))
                else:
                    table[idx].update(cand)
            return _Result(self._payload)
        raise AssertionError(f"unsupported op {self._op!r}")


class _FakeSupabase:
    def __init__(self, tables):
        self.tables = {k: [dict(r) for r in v] for k, v in tables.items()}

    def table(self, name):
        return _Query(self, name)


class _SpeakerId:
    def __init__(self, label, speaker_identifiers):
        self.label = label
        self.speaker_identifiers = speaker_identifiers


class _ConnectedStream:
    class _Client:
        _is_connected = True

    _client = _Client()


class _FakeSTT:
    def __init__(self, ids):
        self._speaker_ids = list(ids)
        self._streams = [_ConnectedStream()]

    async def get_speaker_ids(self):
        return list(self._speaker_ids)


# ---------------------------------------------------------------------------
# Ghost-label posture
# ---------------------------------------------------------------------------

def _open_window(sk, t, duration=30.0):
    sk.start_question({"canonical_answer": "Mars"})
    sk.open_answer_window(duration=duration, now=t)


def test_echo_phantom_folds_instead_of_scoring():
    sk = LilyScorekeeper("s")
    sk.bind_speaker("S2", "Rhonda")
    _open_window(sk, 100.0)
    # Bound player answers.
    r1 = sk.on_transcript_segment(
        "Mark.", speaker_label="S2", now=101.0,
    )
    assert r1["player"] == "Rhonda"
    assert "Rhonda" in sk.answer_candidates
    # Echo phantom on a stray single-utterance label, same answer, inside
    # the echo window — folds: no candidate, flagged ghost_folded.
    r2 = sk.on_transcript_segment(
        "Mark.", speaker_label="S5", now=103.0,
    )
    assert r2["ghost_folded"] is True
    assert "unrostered:S5" not in sk.answer_candidates


def test_echo_veto_by_volume_signal_and_recurring_label():
    sk = LilyScorekeeper("s")
    sk.bind_speaker("S2", "Rhonda")
    _open_window(sk, 100.0)
    sk.on_transcript_segment("Mark.", speaker_label="S2", now=101.0)
    # WS-13 says the copy is as loud as the original -> a real second
    # speaker, veto the fold.
    r = sk.on_transcript_segment(
        "Mark.", speaker_label="S5", now=102.0, echo_copy_signal=False,
    )
    assert r["ghost_folded"] is False
    assert "unrostered:S5" in sk.answer_candidates


def test_recurring_label_is_not_folded():
    sk = LilyScorekeeper("s")
    sk.bind_speaker("S2", "Rhonda")
    _open_window(sk, 100.0)
    sk.on_transcript_segment("Mark.", speaker_label="S2", now=101.0)
    # A label already heard once this session is a recurring voice, not a
    # one-shot phantom.
    sk.on_transcript_segment("something else", speaker_label="S6", now=101.5)
    r = sk.on_transcript_segment("Mark.", speaker_label="S6", now=102.0)
    assert r["ghost_folded"] is False


def test_stale_echo_outside_window_is_not_folded():
    sk = LilyScorekeeper("s")
    sk.bind_speaker("S2", "Rhonda")
    # Answer window stays open across the whole span so we isolate the
    # ghost-fold window (8s), not answer-window expiry.
    _open_window(sk, 100.0, duration=100.0)
    sk.on_transcript_segment("Mark.", speaker_label="S2", now=101.0)
    # Well past the ghost-fold window (default 8s) — a genuine later voice.
    r = sk.on_transcript_segment("Mark.", speaker_label="S5", now=140.0)
    assert r["ghost_folded"] is False
    assert "unrostered:S5" in sk.answer_candidates


# ---------------------------------------------------------------------------
# Roster-side merge
# ---------------------------------------------------------------------------

def test_merge_moves_unrostered_candidate_and_binds_player():
    sk = LilyScorekeeper("s")
    sk.bind_speaker("S4", "Chris")
    sk.start_question({"canonical_answer": "The Wizard of Oz"})
    sk.open_answer_window(duration=30.0, now=200.0)
    # S1 (unrostered, the diarizer split) buzzes Chris's Oz answer.
    sk.on_transcript_segment(
        "The Wizard of Oz.", speaker_label="S1", now=201.0,
    )
    assert "unrostered:S1" in sk.answer_candidates
    out = sk.merge_speakers("S1", "Chris")
    assert out["candidates_moved"] == 1
    assert "unrostered:S1" not in sk.answer_candidates
    assert "Chris" in sk.answer_candidates
    assert sk.answer_candidates["Chris"]["player"] == "Chris"
    # Chris keeps his primary S4 label; the split-off S1 becomes an alias,
    # so utterances on EITHER label now resolve to Chris going forward.
    assert sk.players["Chris"]["speaker_label"] == "S4"
    assert "S1" in sk.players["Chris"]["alias_labels"]
    assert sk.resolve_speaker(None, "S1", None, "x")[0] == "Chris"
    assert sk.resolve_speaker(None, "S4", None, "x")[0] == "Chris"


def test_merge_creates_player_when_label_holds_intro():
    sk = LilyScorekeeper("s")
    # S1 never bound — it holds Chris's self-introduction only.
    out = sk.merge_speakers("S1", "Chris")
    assert out["created_player"] is True
    assert "Chris" in sk.players


# ---------------------------------------------------------------------------
# Durable-side merge + dedupe (retro-attribution)
# ---------------------------------------------------------------------------

def _db_from_fixture():
    return _FakeSupabase({
        "lily_transcripts": [
            {"session_id": FIXTURE["session_id"], **r}
            for r in FIXTURE["transcripts"]
        ],
        "lily_addressee_log": [
            {"session_id": FIXTURE["session_id"], **r}
            for r in FIXTURE["addressee_log"]
        ],
        "lily_speaker_voiceprints": list(FIXTURE["voiceprints"]),
    })


def test_durable_merge_retro_attributes_and_dedupes():
    db = _db_from_fixture()
    summary = asyncio.run(lily_persistence.lily_merge_speaker(
        db, FIXTURE["session_id"], FIXTURE["group_id"],
        from_label="S1", into_player="Chris",
    ))
    assert summary["transcripts_updated"] is True
    assert summary["addressee_updated"] is True
    assert summary["voiceprint_relabeled"] is True
    # S1 transcript rows now attributed to Chris.
    s1_rows = [
        r for r in db.tables["lily_transcripts"]
        if r["speaker_label"] == "S1"
    ]
    assert s1_rows and all(r["speaker_name"] == "Chris" for r in s1_rows)
    # Addressee rows too.
    assert all(
        r["player_name"] == "Chris"
        for r in db.tables["lily_addressee_log"]
        if r["speaker_label"] == "S1"
    )
    # Voiceprints deduped to ONE Chris row for the group.
    chris_rows = [
        r for r in db.tables["lily_speaker_voiceprints"]
        if r["group_id"] == FIXTURE["group_id"] and r["player_name"] == "Chris"
    ]
    assert len(chris_rows) == 1
    # The freshest row (real session identifiers) survived.
    assert chris_rows[0]["speaker_identifiers"] == ["id-s4-fresh"]
    assert summary["voiceprints_deduped"] == 1


def test_dedupe_prefers_row_with_identifiers_over_newer_empty():
    db = _FakeSupabase({
        "lily_speaker_voiceprints": [
            {
                "id": 1, "group_id": "g", "speaker_label": "S1",
                "player_name": "Chris", "speaker_identifiers": ["real"],
                "updated_at": "2026-01-01T00:00:00+00:00",
            },
            {
                "id": 2, "group_id": "g", "speaker_label": "S4",
                "player_name": "Chris", "speaker_identifiers": None,
                "updated_at": "2026-09-09T00:00:00+00:00",
            },
        ]
    })
    deleted = asyncio.run(
        lily_persistence.lily_dedupe_group_voiceprints(db, "g", "Chris")
    )
    assert deleted == 1
    rows = db.tables["lily_speaker_voiceprints"]
    assert len(rows) == 1
    # The row WITH identifiers wins even though it is older.
    assert rows[0]["speaker_identifiers"] == ["real"]


# ---------------------------------------------------------------------------
# Enrollment under-threshold surfacing
# ---------------------------------------------------------------------------

class _SK:
    def __init__(self, players):
        self.players = players


def test_bound_player_below_floor_is_surfaced_for_retry():
    db = _FakeSupabase({"lily_speaker_voiceprints": []})
    # Rami crossed the floor; Chris (S4) did not — only Rami comes back.
    stt = _FakeSTT([_SpeakerId("Rami", ["id-rami"])])
    sk = _SK({
        "Rami": {"speaker_label": "Rami"},
        "Chris": {"speaker_label": "S4"},
    })
    wrote = asyncio.run(lily_persistence.lily_enroll_voiceprints(
        stt, db, "grp", sk, trigger="game_start",
    ))
    assert wrote is True
    # S4 is tracked as under-threshold so the schedule keeps retrying it.
    assert getattr(sk, "unenrolled_bound_labels") == {"S4"}


def test_all_bound_enrolled_leaves_no_gap():
    db = _FakeSupabase({"lily_speaker_voiceprints": []})
    stt = _FakeSTT([
        _SpeakerId("Rami", ["id-rami"]),
        _SpeakerId("S4", ["id-chris"]),
    ])
    sk = _SK({
        "Rami": {"speaker_label": "Rami"},
        "Chris": {"speaker_label": "S4"},
    })
    asyncio.run(lily_persistence.lily_enroll_voiceprints(
        stt, db, "grp", sk, trigger="game_start",
    ))
    assert getattr(sk, "unenrolled_bound_labels") == set()
