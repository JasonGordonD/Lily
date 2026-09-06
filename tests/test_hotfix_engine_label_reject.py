"""HOTFIX-ENGINE-LABEL-001 — live 2026-09-06 12:20–12:22 UTC, three sessions
(lily-A2930D-8b4c61db, lily-879368-f7c030f1, lily-FE9AD4-12697417) deaf:
VAD heard the room (vad_seconds 4.4 / 2.05), Speechmatics delivered zero
segments, zero user transcripts. The worker log names it:

  speechmatics.voice._client — Server error: invalid input:
  transcription_config.speaker_diarization_config.speakers.0.label:
  Must not validate the schema (not)
  livekit.agents — Error in _stt_pump

lily_speaker_voiceprints row id 427 (speaker_label "S1", player_name
null — its identifiers were written by the 12:01 session's enrollment
path) was injected as a known speaker; Speechmatics forbids S<n> labels
there and rejected the whole StartRecognition, so nothing was ever
transcribed. The hygiene chokepoint only dropped dunder labels, and the
loader did not carry the row id or player_name at all.

Operator rule (verbatim): never inject a voiceprint whose label matches
^S\\d+$ or whose player_name is null; log the skip with the row id.
"""

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_persistence  # noqa: E402
import lily_stt_tuning  # noqa: E402

# The exact live row, as PostgREST returns it (read-only SELECT, 2026-09-06).
_ROW_427 = {
    "id": 427,
    "speaker_label": "S1",
    "player_name": None,
    "speaker_identifiers": [{"label": "S1", "speaker_identifiers": ["blob-427"]}],
}


class _Query:
    def __init__(self, rows):
        self._rows = rows
        self.selected = None

    def select(self, cols):
        self.selected = cols
        return self

    def eq(self, *_a):
        return self

    def execute(self):
        return type("R", (), {"data": self._rows})()


class _Supabase:
    def __init__(self, rows):
        self.query = _Query(rows)

    def table(self, _name):
        return self.query


def _labels(rows):
    return [r["label"] for r in lily_stt_tuning.lily_filter_enrollable_speakers(rows)]


def test_live_row_427_never_reaches_start_recognition(caplog):
    """The exact config that took STT down: loader → chokepoint → nothing."""
    db = _Supabase([_ROW_427])
    loaded = asyncio.run(lily_persistence.lily_load_voiceprints(db, "grp_live"))
    assert "id" in db.query.selected and "player_name" in db.query.selected
    assert loaded and loaded[0]["id"] == 427 and loaded[0]["label"] == "S1"

    with caplog.at_level(logging.WARNING, logger="lily_stt_tuning"):
        injected = lily_stt_tuning.lily_filter_enrollable_speakers(loaded)
    assert injected == []
    skip_lines = [r.getMessage() for r in caplog.records if "SKIPPED" in r.getMessage()]
    assert skip_lines, "the skip must be logged"
    assert "row_id=427" in skip_lines[0] and "label=S1" in skip_lines[0]


def test_engine_auto_labels_never_reach_start_recognition():
    rows = [
        {"label": "S1", "speaker_identifiers": ["blob-s1"]},
        {"label": "Rami", "speaker_identifiers": ["blob-rami"]},
        {"label": "s2", "speaker_identifiers": ["blob-s2"]},
        {"label": "S12", "speaker_identifiers": ["blob-s12"]},
        {"label": "__ASSISTANT__", "speaker_identifiers": ["blob-echo"]},
    ]
    assert _labels(rows) == ["Rami"]


def test_null_player_name_never_reaches_start_recognition(caplog):
    """Second half of the rule: a real-looking label with no bound player
    is still an unbound voice — not injected, skip logged with the row id."""
    rows = [
        {"id": 99, "label": "Rami", "player_name": None, "speaker_identifiers": ["a"]},
        {"id": 100, "label": "Chris", "player_name": "", "speaker_identifiers": ["b"]},
        {"id": 101, "label": "Sam", "player_name": "Sam", "speaker_identifiers": ["c"]},
    ]
    with caplog.at_level(logging.WARNING, logger="lily_stt_tuning"):
        assert _labels(rows) == ["Sam"]
    msgs = [r.getMessage() for r in caplog.records]
    assert any("NULL_PLAYER_NAME_SKIPPED" in m and "row_id=99" in m for m in msgs)
    assert any("NULL_PLAYER_NAME_SKIPPED" in m and "row_id=100" in m for m in msgs)


def test_enrollment_never_writes_an_unbound_engine_label(caplog):
    """The 12:01Z session's write that armed row 427: S1 heard, nobody bound
    to it, no prior binding in the table → its identifiers must not be
    persisted. A label that IS bound (roster or stored) still writes."""
    from test_voiceprint_enrollment import (
        _FakeScorekeeper, _FakeSTT, _FakeSupabase, _SpeakerIdentifier,
    )

    db = _FakeSupabase({"lily_speaker_voiceprints": []})
    stt = _FakeSTT([
        _SpeakerIdentifier("S1", ["blob-unbound"]),
        _SpeakerIdentifier("S2", ["blob-rami"]),
    ])
    sk = _FakeScorekeeper({"Rami": {"speaker_label": "S2"}})

    with caplog.at_level(logging.INFO, logger="lily_persistence"):
        wrote = asyncio.run(lily_persistence.lily_enroll_voiceprints(
            stt, db, "grp_live", sk, trigger="test_427",
        ))
    assert wrote is True
    rows = db.tables["lily_speaker_voiceprints"]
    assert [(r["speaker_label"], r["player_name"]) for r in rows] == [("S2", "Rami")]
    assert any(
        "UNBOUND_ENGINE_LABEL_SKIPPED" in r.getMessage() and "label=S1" in r.getMessage()
        for r in caplog.records
    )


def test_real_names_that_merely_start_with_s_survive():
    rows = [
        {"label": "Sam", "speaker_identifiers": ["a"]},
        {"label": "S1mon", "speaker_identifiers": ["b"]},
        {"label": "Sarah 2", "speaker_identifiers": ["c"]},
    ]
    assert _labels(rows) == ["Sam", "S1mon", "Sarah 2"]


def test_predicate_shape():
    assert lily_stt_tuning.lily_is_engine_speaker_label("S1")
    assert lily_stt_tuning.lily_is_engine_speaker_label(" s7 ")
    assert not lily_stt_tuning.lily_is_engine_speaker_label("Rami")
    assert not lily_stt_tuning.lily_is_engine_speaker_label("")
    assert not lily_stt_tuning.lily_is_engine_speaker_label(None)
