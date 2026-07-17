"""Tests for stable group identity (WO-LILY-MEMORY-CLOSEOUT-001 Task 3):
name-set hash fallback, voiceprint identifier matching, and the
lily_memories player_names audit write. Pure logic + stub client — no
livekit, no network."""

import asyncio
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lily_memory import (
    NAME_SET_GROUP_PREFIX,
    lily_match_group_by_voiceprints,
    lily_name_set_group_id,
    lily_normalize_player_names,
    lily_write_session_memory,
)


# ---------------------------------------------------------------------------
# Name normalization
# ---------------------------------------------------------------------------

def test_normalize_sorts_lowercases_dedupes():
    assert lily_normalize_player_names(["Rami", "  Carly ", "kali", "RAMI"]) == [
        "carly", "kali", "rami"
    ]


def test_normalize_drops_empty_and_none():
    assert lily_normalize_player_names(["", None, "  ", "Dave"]) == ["dave"]
    assert lily_normalize_player_names([]) == []
    assert lily_normalize_player_names(None) == []


# ---------------------------------------------------------------------------
# Name-set group-id hash (resolution step c)
# ---------------------------------------------------------------------------

def test_name_set_hash_known_value():
    expected = NAME_SET_GROUP_PREFIX + hashlib.sha1(
        b"carly|kali|rami"
    ).hexdigest()
    assert lily_name_set_group_id(["Carly", "Kali", "Rami"]) == expected


def test_name_set_hash_order_and_case_insensitive():
    a = lily_name_set_group_id(["Rami", "Carly", "Kali"])
    b = lily_name_set_group_id(["kali", "RAMI", " carly "])
    assert a == b
    assert a.startswith("grp_")


def test_name_set_hash_dedupes():
    assert lily_name_set_group_id(["Dave", "dave", "DAVE"]) == \
        lily_name_set_group_id(["Dave"])


def test_name_set_hash_differs_for_different_rosters():
    assert lily_name_set_group_id(["Sarah", "Dave"]) != \
        lily_name_set_group_id(["Sarah", "Dave", "Priya"])


def test_name_set_hash_empty_returns_none():
    assert lily_name_set_group_id([]) is None
    assert lily_name_set_group_id(["", None]) is None
    assert lily_name_set_group_id(None) is None


# ---------------------------------------------------------------------------
# Voiceprint identifier matching (resolution step b)
# ---------------------------------------------------------------------------

class FakeSpeakerIdentifier:
    """Shape-compatible with the Speechmatics SpeakerIdentifier at 1.6.4."""

    def __init__(self, label, speaker_identifiers):
        self.label = label
        self.speaker_identifiers = speaker_identifiers


STORED = [
    {"group_id": "grp_aaa", "player_name": "Sarah",
     "speaker_identifiers": ["id-sarah-1", "id-sarah-2"]},
    {"group_id": "grp_aaa", "player_name": "Dave",
     "speaker_identifiers": ["id-dave-1"]},
    {"group_id": "grp_bbb", "player_name": "Sarah",
     "speaker_identifiers": ["id-other-1"]},
]


def test_voiceprint_match_finds_group_with_overlap():
    current = [FakeSpeakerIdentifier("Sarah", ["id-sarah-2", "id-new"])]
    assert lily_match_group_by_voiceprints(current, STORED) == "grp_aaa"


def test_voiceprint_match_most_overlap_wins():
    current = [
        FakeSpeakerIdentifier("Sarah", ["id-sarah-1", "id-sarah-2"]),
        FakeSpeakerIdentifier("X", ["id-other-1"]),
    ]
    # grp_aaa overlaps twice, grp_bbb once.
    assert lily_match_group_by_voiceprints(current, STORED) == "grp_aaa"


def test_voiceprint_match_tie_is_deterministic():
    current = [FakeSpeakerIdentifier("S", ["id-sarah-1", "id-other-1"])]
    # One overlap each — lexicographically first group wins.
    assert lily_match_group_by_voiceprints(current, STORED) == "grp_aaa"


def test_voiceprint_match_none_without_overlap():
    current = [FakeSpeakerIdentifier("S", ["id-unknown"])]
    assert lily_match_group_by_voiceprints(current, STORED) is None
    assert lily_match_group_by_voiceprints([], STORED) is None
    assert lily_match_group_by_voiceprints(None, STORED) is None
    assert lily_match_group_by_voiceprints(current, []) is None
    assert lily_match_group_by_voiceprints(current, None) is None


def test_voiceprint_match_tolerates_shapes():
    # Nested lists, plain strings, dict rows with missing fields.
    current = [["id-dave-1"], "id-extra"]
    stored = STORED + [{"group_id": None, "speaker_identifiers": ["id-dave-1"]},
                       {"speaker_identifiers": ["id-dave-1"]}, None]
    assert lily_match_group_by_voiceprints(current, stored) == "grp_aaa"


# ---------------------------------------------------------------------------
# lily_memories write carries the player_names audit column
# ---------------------------------------------------------------------------

class StubTable:
    def __init__(self, sink, fail_first_with=None):
        self._sink = sink
        self._fail_first_with = fail_first_with
        self._payload = None

    def upsert(self, payload, on_conflict=None):
        self._payload = (payload, on_conflict)
        return self

    def execute(self):
        if self._fail_first_with is not None:
            msg, self._fail_first_with = self._fail_first_with, None
            raise RuntimeError(msg)
        self._sink.append(self._payload)
        return self


class StubSupabase:
    def __init__(self, fail_first_with=None):
        self.rows = []
        self._fail_first_with = fail_first_with
        self._table = None

    def table(self, name):
        assert name == "lily_memories"
        if self._table is None:
            self._table = StubTable(self.rows, self._fail_first_with)
        return self._table


STANDINGS = [
    {"name": "Rami", "score": 7, "streak": 2},
    {"name": "Carly", "score": 4, "streak": 0},
]


def test_write_session_memory_includes_normalized_player_names():
    stub = StubSupabase()
    asyncio.run(lily_write_session_memory(
        stub, "grp_x", "room-1", STANDINGS, 12, highlights=[]
    ))
    assert len(stub.rows) == 1
    payload, on_conflict = stub.rows[0]
    assert on_conflict == "session_id"
    assert payload["player_names"] == ["carly", "rami"]
    assert payload["question_count"] == 12
    assert payload["group_id"] == "grp_x"


def test_write_session_memory_retries_without_player_names_column():
    """Migration-lag tolerance: if production lacks the 007 column, the
    memory row still lands (without the audit column)."""
    stub = StubSupabase(
        fail_first_with="column \"player_names\" of relation "
                        "\"lily_memories\" does not exist"
    )
    asyncio.run(lily_write_session_memory(
        stub, "grp_x", "room-1", STANDINGS, 5, highlights=[]
    ))
    assert len(stub.rows) == 1
    payload, _ = stub.rows[0]
    assert "player_names" not in payload
    assert payload["question_count"] == 5


# ---------------------------------------------------------------------------
# Label round-trip confirmation (2026-07-16: identifier strings refresh
# every session — exact overlap can never match; the injected label coming
# back IS the vendor's biometric recognition)
# ---------------------------------------------------------------------------

from lily_memory import lily_candidate_labels_confirmed


class _SpeakerId:
    def __init__(self, label, ids):
        self.label = label
        self.speaker_identifiers = ids


def _cand_rows():
    return [{"group_id": "fe600d1f", "player_name": "Rami",
             "speaker_label": "Rami",
             "speaker_identifiers": ["OLD_SESSION_BLOB"]}]


def test_injected_label_round_trip_confirms():
    # Fresh-session blob differs from the stored one — only the label,
    # which the engine assigns on ITS recognition, matches.
    current = [_SpeakerId("Rami", ["FRESH_SESSION_BLOB"])]
    assert lily_candidate_labels_confirmed(current, _cand_rows()) is True


def test_transient_diarization_labels_never_confirm():
    current = [_SpeakerId("S1", ["FRESH_BLOB"]), _SpeakerId("S0", ["X"])]
    assert lily_candidate_labels_confirmed(current, _cand_rows()) is False


def test_unrelated_label_never_confirms():
    current = [_SpeakerId("Dave", ["FRESH_BLOB"])]
    assert lily_candidate_labels_confirmed(current, _cand_rows()) is False


def test_label_match_is_case_insensitive_and_handles_nesting():
    current = [[_SpeakerId("rami", ["A"])]]  # multi-stream nested shape
    assert lily_candidate_labels_confirmed(current, _cand_rows()) is True


def test_empty_inputs_never_confirm():
    assert lily_candidate_labels_confirmed(None, _cand_rows()) is False
    assert lily_candidate_labels_confirmed([_SpeakerId("Rami", [])], []) is False
    assert lily_candidate_labels_confirmed([], _cand_rows()) is False
