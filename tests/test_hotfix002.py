"""WO-LILY-HOTFIX-002 — agent-transcript integrity + group-binding loudness.

Fixture: session lily-AAC431-6208ff7c (2026-08-06 05:19, first healthy
session on the omnibus build). The DB audit found the record was NOT dark
— 14 LILY rows landed live — but FOUR of them were verbatim duplicates of
the previous spoken turn, each written right after a tool-call-only turn:
the playout watcher's `spoken or _last_assistant_text` fallback fabricated
a re-record whenever a handle carried chat items but no assistant text.
Group binding: all five post-deploy sessions ended still-quarantined
(group_id == session_id) with zero log evidence of why; the same group's
voiceprint table carried a duplicate enrollment label (Chris under both
S1 and S4 — undefined engine behaviour inside StartRecognition's
speakers list).

Pinned here:
  - N distinct agent turns -> N LILY rows, playout timing present;
  - a verbatim repeat of the previous recorded turn is skipped (the
    fallback-echo class), loudly;
  - a transcript write failure is an ERROR log, never silence;
  - duplicate enrollment labels MERGE their identifier blobs;
  - minting a throwaway group WARNs with the discriminating reason
    (no_token_present vs token_unreadable);
  - a candidate that can never verify (no get_speaker_ids) says so.
"""

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_stt_tuning
import lily_agent
from lily_agent import LilyGame
from lily_scorekeeper import LilyScorekeeper


class _FakeBatcher:
    def __init__(self, fail=False):
        self.rows = []
        self.fail = fail

    def add(self, text, speaker_label=None, speaker_name=None,
            segment_start=None, segment_end=None):
        if self.fail:
            raise RuntimeError("supabase down")
        self.rows.append({
            "text": text, "label": speaker_label, "name": speaker_name,
            "segment_start": segment_start, "segment_end": segment_end,
        })


def _make_game(fail_batcher=False) -> LilyGame:
    game = LilyGame.__new__(LilyGame)
    game.sk = LilyScorekeeper("hotfix002-fixture")
    game.transcripts = _FakeBatcher(fail=fail_batcher)
    return game


# -- Defect 1: both-sides record integrity -------------------------------------


def test_n_distinct_turns_persist_as_n_lily_rows_with_timing():
    game = _make_game()
    turns = [f"Spoken turn number {i}, fresh words each time." for i in range(5)]
    for text in turns:
        game.record_agent_turn(text, act_keys=[], interrupted=False)
    rows = game.transcripts.rows
    assert len(rows) == 5
    assert [r["text"] for r in rows] == turns
    assert all(r["label"] == "LILY" for r in rows)
    # Playout timing anchor present on every agent row.
    assert all(isinstance(r["segment_end"], float) for r in rows)
    assert game.sk.agent_turns == turns


def test_verbatim_repeat_of_previous_turn_is_skipped_loudly(caplog):
    """THE lily-AAC431 dup class: the same text arriving again right after
    (the tool-turn fallback echo) is not a real utterance."""
    game = _make_game()
    game.record_agent_turn("Welcome, Rami! Locked in.", act_keys=[], interrupted=False)
    with caplog.at_level(logging.WARNING):
        game.record_agent_turn(
            "Welcome, Rami! Locked in.", act_keys=[], interrupted=False
        )
    assert len(game.transcripts.rows) == 1
    assert game.sk.agent_turns == ["Welcome, Rami! Locked in."]
    assert any("DUP_TURN_SKIPPED" in r.message for r in caplog.records)
    # A DIFFERENT next turn still records — only verbatim echoes skip.
    game.record_agent_turn("Round one, question one!", act_keys=[], interrupted=False)
    assert len(game.transcripts.rows) == 2


def test_transcript_write_failure_is_an_error_log_not_silence(caplog):
    game = _make_game(fail_batcher=True)
    with caplog.at_level(logging.ERROR):
        game.record_agent_turn("This will fail to persist.", act_keys=[], interrupted=False)
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("TRANSCRIPT_PERSIST_FAILED" in r.message for r in errors)
    # The local (scorekeeper) record still landed — one leg down, not both.
    assert game.sk.agent_turns == ["This will fail to persist."]


# -- Defect 2: enrollment hygiene + binding loudness ---------------------------


def test_duplicate_enrollment_labels_merge_their_identifiers(caplog):
    """The 41dfc215 live shape: the same player under two engine labels
    must enroll ONCE, with all identifier blobs kept as match hints."""
    rows = [
        {"label": "Rami", "speaker_identifiers": ["blob-r1"]},
        {"label": "Chris", "speaker_identifiers": ["blob-c-s1"]},
        {"label": "Rhonda", "speaker_identifiers": ["blob-rh"]},
        {"label": "Chris", "speaker_identifiers": ["blob-c-s4"]},
    ]
    with caplog.at_level(logging.WARNING):
        out = lily_stt_tuning.lily_filter_enrollable_speakers(rows)
    assert [r["label"] for r in out] == ["Rami", "Chris", "Rhonda"]
    chris = next(r for r in out if r["label"] == "Chris")
    assert chris["speaker_identifiers"] == ["blob-c-s1", "blob-c-s4"]
    assert any("duplicate enrollment label merged" in r.message for r in caplog.records)


def test_dunder_labels_still_drop_and_identical_blobs_dedupe():
    rows = [
        {"label": "__ASSISTANT__", "speaker_identifiers": ["blob-x"]},
        {"label": "Rami", "speaker_identifiers": ["blob-1"]},
        {"label": "Rami", "speaker_identifiers": ["blob-1", "blob-2"]},
    ]
    out = lily_stt_tuning.lily_filter_enrollable_speakers(rows)
    assert [r["label"] for r in out] == ["Rami"]
    assert out[0]["speaker_identifiers"] == ["blob-1", "blob-2"]


class _FakeParticipant:
    def __init__(self, metadata=None, kind=None):
        self.metadata = metadata
        self.kind = kind
        self.identity = "probe"


class _FakeRoom:
    def __init__(self, participants=None):
        self.remote_participants = participants or {}


class _FakeJob:
    def __init__(self, metadata=None):
        self.metadata = metadata


class _FakeCtx:
    def __init__(self, job_meta=None, participants=None):
        self.job = _FakeJob(job_meta)
        self.room = _FakeRoom(participants)


def _resolve(ctx, monkeypatch):
    monkeypatch.delenv("LILY_GROUP_ID", raising=False)
    monkeypatch.setattr(lily_agent, "PARTICIPANT_METADATA_WAIT_SECONDS", 0.0)
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(
            lily_agent._resolve_initial_group_id(ctx, "lily-TEST99")
        )
    finally:
        loop.close()


def test_throwaway_mint_warns_no_token_present(caplog, monkeypatch):
    with caplog.at_level(logging.WARNING):
        group, source = _resolve(_FakeCtx(), monkeypatch)
    assert (group, source) == ("lily-TEST99", "room_name")
    warn = next(
        r for r in caplog.records if "THROWAWAY_GROUP_MINTED" in r.message
    )
    assert "no_token_present" in warn.getMessage()


def test_throwaway_mint_warns_token_unreadable(caplog, monkeypatch):
    ctx = _FakeCtx(job_meta='{"wrong_key": "x"}')
    with caplog.at_level(logging.WARNING):
        group, source = _resolve(ctx, monkeypatch)
    assert (group, source) == ("lily-TEST99", "room_name")
    warn = next(
        r for r in caplog.records if "THROWAWAY_GROUP_MINTED" in r.message
    )
    assert "token_unreadable" in warn.getMessage()


def test_token_present_still_resolves_dispatch_metadata(monkeypatch):
    ctx = _FakeCtx(job_meta='{"lily_group_id": "41dfc215-uuid"}')
    group, source = _resolve(ctx, monkeypatch)
    assert (group, source) == ("41dfc215-uuid", "dispatch_metadata")


def test_unverifiable_candidate_says_so(caplog):
    """An STT surface without get_speaker_ids makes promotion impossible —
    that condition must be a WARN, not an eternal silent None."""
    game = LilyGame.__new__(LilyGame)
    game.sk = LilyScorekeeper("hotfix002-verify")
    game.device_candidate_group_id = "41dfc215-uuid"
    game.stt = object()  # no get_speaker_ids
    game._device_candidate_voiceprints = []

    loop = asyncio.new_event_loop()
    try:
        with caplog.at_level(logging.WARNING):
            result = loop.run_until_complete(
                game.verify_device_candidate("test")
            )
    finally:
        loop.close()
    assert result is None
    assert game._device_verify_attempts == 1
    assert any(
        "DEVICE_VERIFY_UNAVAILABLE" in r.message for r in caplog.records
    )
