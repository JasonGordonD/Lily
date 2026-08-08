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


def _resolve(ctx, monkeypatch, wait=0.0):
    monkeypatch.delenv("LILY_GROUP_ID", raising=False)
    # The resolver reads the wait from config now (it became a knob so a
    # slow-booting deployment can widen the propagation window).
    monkeypatch.setattr(
        lily_agent.lily_config, "participant_metadata_wait_seconds",
        lambda: wait,
    )
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


# -- 2026-08-06 log-audit fixes ------------------------------------------------


def test_stop_idle_watchdog_cancels_and_flags():
    """TICK_FAILED class: the watchdog must die WITH the session — after
    stop_idle_watchdog() no tick can ever run against a dead AgentSession."""
    game = LilyGame.__new__(LilyGame)
    game.game_over = False
    game.game_started = True

    async def scenario():
        game._watchdog_task = asyncio.ensure_future(asyncio.sleep(60))
        game.stop_idle_watchdog()
        await asyncio.sleep(0)
        assert game._session_closed is True
        assert game._watchdog_task is None

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(scenario())
    finally:
        loop.close()


def test_near_miss_rewrites_before_nudge_or_playout(caplog):
    """Session 05AAC9 q=2 fixture: a near-verbatim performance (ratio 1.00)
    with unread MC options is replaced before first playout, so the old
    question-then-nudge double read cannot occur."""
    from test_desync_fixture import _make_game as _desync_game

    game = _desync_game()
    game.armed_question = {
        "prompt": "Which planet in our solar system has the most moons?",
        "canonical_answer": "Saturn",
        "acceptable_answers": ["saturn"],
        "choices": ["Jupiter", "Saturn", "Uranus", "Neptune"],
    }
    game.sk.start_question(game.armed_question)
    game._armed_speech_misses = 0
    game._pending_delivery_qnum = None
    game.ui_phase = "question"
    # She performs the question sentence verbatim — but never the options.
    spoken = "Which planet in our solar system has the most moons?"
    with caplog.at_level(logging.WARNING):
        assert game.register_delivery_claim(spoken) == "rewrite_strict"
    rewrites = [
        r for r in caplog.records
        if "near_verbatim_unregistered" in r.getMessage()
    ]
    assert rewrites
    assert game.session.instructions == []  # caller replaces this first turn
    assert game.sk.answer_window_open is False


# -- WO-LILY-ARSENAL-SEED-001 follow-on: the amnesia race ---------------------


class _LateMetadataParticipant:
    """A participant already in the room whose `metadata` field has not yet
    propagated to the agent. Presence and metadata-readiness are different
    things: the object lands in room.remote_participants first, and the
    metadata syncs a beat later."""

    def __init__(self, metadata, ready_on_scan):
        self._metadata = metadata
        self._ready_on_scan = ready_on_scan
        self.scans = 0
        self.kind = None
        self.identity = "late-probe"

    @property
    def metadata(self):
        self.scans += 1
        return self._metadata if self.scans > self._ready_on_scan else None


def test_present_participant_with_late_metadata_is_waited_for(monkeypatch):
    """THE amnesia bug. The resolver used to break the moment a human was
    present without usable metadata, on the reasoning that token metadata
    is fixed at join so waiting could not help. That confuses the VALUE
    (fixed) with its PROPAGATION (asynchronous) — and a busy or
    slow-booting agent loses that race, mints a throwaway group, and greets
    a returning table as a stranger with its real history sitting in the
    database untouched. 27 of 68 live sessions went this way."""
    late = _LateMetadataParticipant(
        '{"lily_group_id": "grp_0b07f989"}', ready_on_scan=1
    )
    ctx = _FakeCtx(participants={"p1": late})
    group, source = _resolve(ctx, monkeypatch, wait=2.0)
    assert (group, source) == ("grp_0b07f989", "participant_metadata")
    assert late.scans > 1, "the resolver must poll, not give up on first sight"


def test_a_participant_with_genuinely_no_metadata_still_mints_a_throwaway(
    caplog, monkeypatch
):
    """The wait is bounded: a token that truly carries no group id still
    falls through to a throwaway rather than hanging the greeting — and it
    says so at WARNING, because silent amnesia is the defect class."""
    bare = _FakeParticipant(metadata=None)
    ctx = _FakeCtx(participants={"p1": bare})
    with caplog.at_level(logging.WARNING):
        group, source = _resolve(ctx, monkeypatch, wait=0.0)
    assert (group, source) == ("lily-TEST99", "room_name")
    assert any(
        "THROWAWAY_GROUP_MINTED" in r.message for r in caplog.records
    )
    assert any(
        "no lily_group_id metadata within" in r.getMessage()
        for r in caplog.records
    )


def test_metadata_already_present_returns_on_the_first_scan(monkeypatch):
    """Polling must cost nothing in the common case."""
    ready = _LateMetadataParticipant(
        '{"lily_group_id": "grp_ready"}', ready_on_scan=0
    )
    ctx = _FakeCtx(participants={"p1": ready})
    group, source = _resolve(ctx, monkeypatch, wait=30.0)
    assert (group, source) == ("grp_ready", "participant_metadata")
    assert ready.scans == 1
