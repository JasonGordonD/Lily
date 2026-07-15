"""Tests for the media_mode sticky flag (WO-LILY-OMNIBUS-002 sub-agent K):
deterministic spoken-choice detection, default voice_only, picture-slot
gating, and picture exclusion in voice_only.

The gating tests import lily_agent (and therefore livekit) — same boundary
note as test_award_gate.py.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_audeering_consumers
import lily_reasoning
import lily_say_gate
from lily_agent import LilyGame
from lily_scorekeeper import LilyScorekeeper, lily_detect_media_choice


# ---------------------------------------------------------------------------
# Spoken-choice detection — deterministic, punctuation-proof
# ---------------------------------------------------------------------------

def test_detect_pictures_variants():
    assert lily_detect_media_choice("pictures on") == "pictures"
    assert lily_detect_media_choice("can we get the picture rounds?") == "pictures"
    assert lily_detect_media_choice("put the pictures on!") == "pictures"
    assert lily_detect_media_choice("let's use the screen") == "pictures"
    assert lily_detect_media_choice("Pictures. On.") == "pictures"


def test_detect_voice_only_variants():
    assert lily_detect_media_choice("voice only") == "voice_only"
    assert lily_detect_media_choice("no pictures please") == "voice_only"
    assert lily_detect_media_choice("pictures off") == "voice_only"
    assert lily_detect_media_choice("turn the pictures off") == "voice_only"
    assert lily_detect_media_choice("Voice... only.") == "voice_only"


def test_off_direction_wins_collisions():
    assert lily_detect_media_choice(
        "no pictures on the screen please"
    ) == "voice_only"


def test_ordinary_speech_does_not_fire():
    assert lily_detect_media_choice("that picture was famous") is None
    assert lily_detect_media_choice("I only heard his voice") is None
    assert lily_detect_media_choice("Tungsten") is None
    assert lily_detect_media_choice("") is None


# ---------------------------------------------------------------------------
# Sticky flag on the scorekeeper — default voice_only
# ---------------------------------------------------------------------------

def test_default_is_voice_only():
    sk = LilyScorekeeper("test-room")
    assert sk.media_mode == "voice_only"


def test_set_media_mode_sticky_and_guarded():
    sk = LilyScorekeeper("test-room")
    sk.set_media_mode("pictures")
    assert sk.media_mode == "pictures"
    sk.set_media_mode("hologram")  # invalid — ignored
    assert sk.media_mode == "pictures"
    sk.set_media_mode("voice_only")
    assert sk.media_mode == "voice_only"


def test_media_mode_survives_snapshot_rehydrate():
    sk = LilyScorekeeper("test-room")
    sk.set_media_mode("pictures")
    snap = sk.snapshot()
    assert snap["media_mode"] == "pictures"
    sk2 = LilyScorekeeper("test-room")
    sk2.rehydrate(snap)
    assert sk2.media_mode == "pictures"


def test_state_block_carries_media_mode():
    sk = LilyScorekeeper("test-room")
    assert "media=voice_only" in sk.build_state_block()
    sk.set_media_mode("pictures")
    assert "media=pictures" in sk.build_state_block()


def test_transcript_segment_detects_media_choice():
    sk = LilyScorekeeper("test-room")
    sk.bind_speaker("S1", "Sarah")
    result = sk.on_transcript_segment(
        text="pictures on", speaker_label="S1", is_final=True,
    )
    assert result["media_choice"] == "pictures"
    # Detection is decoupled from the flag — the agent layer flips it.
    assert sk.media_mode == "voice_only"


def test_transcript_segment_fragment_joined_choice():
    sk = LilyScorekeeper("test-room")
    sk.bind_speaker("S1", "Sarah")
    r1 = sk.on_transcript_segment(
        text="voice", speaker_label="S1", is_final=True, now=100.0,
    )
    assert r1["media_choice"] is None
    r2 = sk.on_transcript_segment(
        text="only.", speaker_label="S1", is_final=True, now=100.8,
    )
    assert r2["media_choice"] == "voice_only"


def test_control_command_takes_precedence_over_media_choice():
    sk = LilyScorekeeper("test-room")
    sk.bind_speaker("S1", "Sarah")
    result = sk.on_transcript_segment(
        text="skip it and put the pictures on", speaker_label="S1",
        is_final=True,
    )
    assert result["control_command"] == "skip"
    assert result["media_choice"] is None


# ---------------------------------------------------------------------------
# Agent-layer gating — picture slots exist ONLY in pictures mode
# ---------------------------------------------------------------------------

def _make_game() -> LilyGame:
    game = LilyGame.__new__(LilyGame)
    game.session = None
    game.agent = None
    game._preemptive_paused = False
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game.sk = LilyScorekeeper("test-room")
    game.sk.questions_per_round = 6
    game.rounds_total = 3
    # game not started: the prefetch auto-advance stays out of the way so
    # the tests can inspect next_question as prefetched.
    game.game_started = False
    game.game_over = False
    game.ui_phase = "lobby"
    game.prewager_standings = None
    game._judged_keys = set()
    game._addressee_rows = {}
    game._spec_judge = {}
    game._armed_speech_misses = 0
    game.armed_question = None
    game.next_question = None
    game.used_prompts = []
    # Post-merge reconcile: the text-supply path now consults the per-group
    # asked history (migration 010) and the bank-curation gate.
    game.asked_history = []
    game.group_id = "grp_test"
    game.promoted_categories = set()
    game.supabase = None
    game._prefetch_task = None
    game._window_timer = None
    game._bed_handle = None
    game._pending_unbound_award = None
    game._adjudicating = False
    game.memory_block = ""
    game.acoustic = lily_audeering_consumers.LilyAcousticState()
    game.publish_attributes_nowait = lambda: None
    return game


def test_picture_slots_excluded_in_voice_only():
    game = _make_game()
    assert game.sk.media_mode == "voice_only"
    for rnd in (1, 2, 3, 4):
        assert game._picture_kind_for_slot(rnd) is None


def test_picture_slots_in_pictures_mode():
    game = _make_game()
    game.sk.set_media_mode("pictures")
    # Reference round: every question is real-or-imagined.
    game.sk.question_number = 8  # mid round 2
    assert game._picture_kind_for_slot(
        lily_reasoning.REAL_OR_IMAGINED_ROUND
    ) == "real_or_imagined"
    # Other rounds: first question of the round is a real-entity slot.
    game.sk.question_number = 0
    assert game._picture_kind_for_slot(1) == "real_entity"
    game.sk.question_number = 1  # mid-round
    assert game._picture_kind_for_slot(1) is None
    game.sk.question_number = 12  # first of round 3
    assert game._picture_kind_for_slot(3) == "real_entity"
    # The wager round is always text.
    assert game._picture_kind_for_slot(4) is None


def test_picture_slots_excluded_in_adult_mode():
    game = _make_game()
    game.sk.set_media_mode("pictures")
    game.sk.set_mode("adult")
    for rnd in (1, 2, 3):
        assert game._picture_kind_for_slot(rnd) is None


class _FakeReasoning:
    def __init__(self, question=None, picture_question=None):
        self.question = question
        self.picture_question = picture_question
        self.prefetch_calls = []
        self.picture_calls = []

    async def prefetch_question(self, sk, **kw):
        self.prefetch_calls.append(kw)
        return dict(self.question) if self.question else None

    async def prefetch_picture_question(self, supabase, **kw):
        self.picture_calls.append(kw)
        return dict(self.picture_question) if self.picture_question else None


def _run_prefetch(game):
    async def scenario():
        game.start_prefetch()
        await game._prefetch_task

    asyncio.run(scenario())


def test_voice_only_strips_cached_bank_image(monkeypatch):
    monkeypatch.delenv("LILY_KB_ONLY", raising=False)
    game = _make_game()
    game.reasoning = _FakeReasoning(question={
        "id": "kb_5", "prompt": "Which sea?", "canonical_answer": "Bosporus",
        "acceptable_answers": ["bosporus"], "reveal_color": "",
        "image_url": "https://cdn.example/web/x.jpg", "image_source": "web",
        "image_license_note": "web image via Exa: ...",
    })
    _run_prefetch(game)
    q = game.next_question
    assert q is not None
    assert "image_url" not in q
    assert "image_license_note" not in q
    assert q["image_source"] == "none"
    # And no picture builder was consulted.
    assert game.reasoning.picture_calls == []


def test_pictures_mode_serves_picture_slot_via_reasoning(monkeypatch):
    monkeypatch.delenv("LILY_KB_ONLY", raising=False)
    game = _make_game()
    game.sk.set_media_mode("pictures")
    game.supabase = object()
    game.sk.question_number = 6  # next question opens round 2
    picture_q = {
        "id": "roi_0006", "prompt": "Real, or imagined?",
        "canonical_answer": "real", "acceptable_answers": ["real"],
        "reveal_color": "", "image_url": "https://cdn.example/web/y.jpg",
        "image_source": "web",
    }
    game.reasoning = _FakeReasoning(picture_question=picture_q)
    _run_prefetch(game)
    q = game.next_question
    assert q["image_url"] == "https://cdn.example/web/y.jpg"
    assert game.reasoning.picture_calls[0]["kind"] == "real_or_imagined"
    # The picture slot was served without touching the text generator.
    assert game.reasoning.prefetch_calls == []


def test_picture_builder_failure_falls_back_to_text(monkeypatch):
    monkeypatch.delenv("LILY_KB_ONLY", raising=False)
    game = _make_game()
    game.sk.set_media_mode("pictures")
    game.supabase = None  # bank fallback unavailable; text path only
    game.sk.question_number = 6
    game.reasoning = _FakeReasoning(
        question={"id": "q_0001", "prompt": "Which sea?",
                  "canonical_answer": "Bosporus",
                  "acceptable_answers": ["bosporus"], "reveal_color": ""},
        picture_question=None,  # builder failed -> text-only fallback
    )
    _run_prefetch(game)
    assert game.reasoning.picture_calls, "picture slot should be attempted"
    q = game.next_question
    assert q["id"] == "q_0001"
    assert "image_url" not in q
