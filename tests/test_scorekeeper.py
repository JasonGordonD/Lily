"""Tests for lily_scorekeeper — pure local state, no livekit required."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lily_scorekeeper import (
    LilyScorekeeper,
    lily_is_system_directed,
)


def make_sk(**kwargs):
    sk = LilyScorekeeper(session_id="test-room", **kwargs)
    sk.bind_speaker("S1", "Sarah")
    sk.bind_speaker("S2", "Dave")
    sk.bind_speaker("S3", "Priya")
    return sk


# ---------------------------------------------------------------------------
# System-directed classifier
# ---------------------------------------------------------------------------

def test_vocative_lily_is_system_directed():
    ok, pattern = lily_is_system_directed("Lily, are you there?")
    assert ok
    assert pattern == "vocative_lily"


def test_standalone_hello_is_system_directed():
    ok, pattern = lily_is_system_directed("Hello?")
    assert ok
    assert pattern == "standalone_hello"


def test_diagnostic_phrase_is_system_directed():
    ok, _ = lily_is_system_directed("hey can you hear us")
    assert ok


def test_casual_lily_mention_is_not_system_directed():
    ok, _ = lily_is_system_directed("I told Lily's joke to my mum")
    assert not ok


def test_normal_answer_is_not_system_directed():
    ok, _ = lily_is_system_directed("Tungsten")
    assert not ok


def test_diarization_tagged_vocative():
    ok, _ = lily_is_system_directed("[S2] Lily? Are you there?")
    assert ok


def test_lily_are_you_there_does_not_score_during_open_window():
    """The bug to prevent: 'Lily, are you there?' during an open answer
    window must NOT count as an answer attempt."""
    sk = make_sk()
    sk.start_question({"prompt": "q", "canonical_answer": "Tungsten"})
    sk.open_answer_window(now=100.0)
    result = sk.on_transcript_segment(
        text="Lily, are you there?",
        speaker_label="S1",
        now=101.0,
        segment_start_time=101.0,
    )
    assert result["system_directed"] is True
    assert result["candidate_recorded"] is False
    assert sk.answer_candidates == {}


# ---------------------------------------------------------------------------
# Answer window
# ---------------------------------------------------------------------------

def test_first_final_orders_revision_updates_answer():
    # Self-correction (live 2026-07-15 fix): a later final from the SAME
    # player revises their answer (current text + attempts list) while the
    # ORDER position stays their first final. One slot per player holds.
    sk = make_sk()
    sk.start_question({"prompt": "q", "canonical_answer": "Tungsten"})
    sk.open_answer_window(now=100.0)
    sk.on_transcript_segment(
        text="Iron", speaker_label="S1", now=101.0, segment_start_time=101.0
    )
    result = sk.on_transcript_segment(
        text="No wait, Tungsten", speaker_label="S1",
        now=102.0, segment_start_time=102.0,
    )
    assert result["candidate_recorded"] is True
    assert len(sk.answer_candidates) == 1
    cand = sk.answer_candidates["Sarah"]
    assert cand["text"] == "No wait, Tungsten"
    assert cand["segment_start_time"] == 101.0  # order key: first final
    assert [a["text"] for a in cand["attempts"]] == ["Iron", "No wait, Tungsten"]
    # A revision is not a new attempt for the tally.
    assert sk.players["Sarah"]["answers_attempted"] == 1


def test_one_candidate_per_player_multiple_players():
    sk = make_sk()
    sk.start_question({"prompt": "q", "canonical_answer": "Tungsten"})
    sk.open_answer_window(now=100.0)
    sk.on_transcript_segment(
        text="Iron", speaker_label="S2", now=101.0, segment_start_time=101.0
    )
    sk.on_transcript_segment(
        text="Tungsten", speaker_label="S1", now=101.5, segment_start_time=101.5
    )
    sk.on_transcript_segment(
        text="Copper", speaker_label="S2", now=102.0, segment_start_time=102.0
    )
    assert len(sk.answer_candidates) == 2
    # Dave revised Iron -> Copper; his order position stays his first final.
    assert sk.answer_candidates["Dave"]["text"] == "Copper"
    assert sk.answer_candidates["Dave"]["segment_start_time"] == 101.0
    assert [a["text"] for a in sk.answer_candidates["Dave"]["attempts"]] == [
        "Iron", "Copper",
    ]


def test_order_by_segment_timestamp_not_arrival():
    """Adjudication order is a timestamp comparison — a segment that
    ARRIVES later but STARTED earlier ranks first."""
    sk = make_sk()
    sk.start_question({"prompt": "q", "canonical_answer": "Tungsten"})
    sk.open_answer_window(now=100.0)
    # Dave's final arrives first but started later.
    sk.on_transcript_segment(
        text="Tungsten", speaker_label="S2", now=103.0, segment_start_time=102.5
    )
    sk.on_transcript_segment(
        text="Tungsten", speaker_label="S1", now=103.5, segment_start_time=101.2
    )
    ordered = sk.ordered_candidates()
    assert [c["player"] for c in ordered] == ["Sarah", "Dave"]


def test_segments_outside_window_are_game_inert():
    sk = make_sk()
    sk.start_question({"prompt": "q", "canonical_answer": "Tungsten"})
    # Window never opened
    result = sk.on_transcript_segment(
        text="Tungsten", speaker_label="S1", now=100.0, segment_start_time=100.0
    )
    assert result["candidate_recorded"] is False
    assert sk.answer_candidates == {}

    # Window opened then expired (bounded duration)
    sk.open_answer_window(duration=15.0, now=100.0)
    result = sk.on_transcript_segment(
        text="Tungsten", speaker_label="S1", now=120.0, segment_start_time=120.0
    )
    assert result["candidate_recorded"] is False

    # Window explicitly closed
    sk.open_answer_window(duration=15.0, now=200.0)
    sk.close_answer_window()
    result = sk.on_transcript_segment(
        text="Tungsten", speaker_label="S1", now=201.0, segment_start_time=201.0
    )
    assert result["candidate_recorded"] is False
    assert sk.answer_candidates == {}


def test_partials_never_score():
    sk = make_sk()
    sk.start_question({"prompt": "q", "canonical_answer": "Tungsten"})
    sk.open_answer_window(now=100.0)
    result = sk.on_transcript_segment(
        text="Tungs", speaker_label="S1", is_final=False,
        now=101.0, segment_start_time=101.0,
    )
    assert result["candidate_recorded"] is False
    assert sk.answer_candidates == {}


def test_steal_window_preserves_prior_candidates():
    sk = make_sk()
    sk.start_question({"prompt": "q", "canonical_answer": "Tungsten"})
    sk.open_answer_window(now=100.0)
    sk.on_transcript_segment(
        text="Iron", speaker_label="S1", now=101.0, segment_start_time=101.0
    )
    sk.close_answer_window()
    sk.open_answer_window(duration=5.0, now=110.0, reset_candidates=False)
    # Sarah already committed — her steal-window final lands as a revision
    # on her preserved slot (adjudication filters judged players, so a
    # judged answerer still cannot steal).
    sk.on_transcript_segment(
        text="Tungsten", speaker_label="S1", now=111.0, segment_start_time=111.0
    )
    assert sk.answer_candidates["Sarah"]["text"] == "Tungsten"
    assert sk.answer_candidates["Sarah"]["segment_start_time"] == 101.0
    # A new player can steal.
    sk.on_transcript_segment(
        text="Tungsten", speaker_label="S2", now=112.0, segment_start_time=112.0
    )
    assert sk.answer_candidates["Dave"]["text"] == "Tungsten"


# ---------------------------------------------------------------------------
# Unrostered speaker — open-floor fallback
# ---------------------------------------------------------------------------

def test_unrostered_speaker_open_floor():
    sk = make_sk()
    sk.start_question({"prompt": "q", "canonical_answer": "Tungsten"})
    sk.open_answer_window(now=100.0)
    result = sk.on_transcript_segment(
        text="Tungsten", speaker_label="S9", now=101.0, segment_start_time=101.0
    )
    assert result["unrostered"] is True
    assert result["player"] is None
    # Recorded as open-floor, never silently attributed to a player.
    assert "unrostered:S9" in sk.answer_candidates
    cand = sk.answer_candidates["unrostered:S9"]
    assert cand["player"] is None
    assert cand["unrostered"] is True
    assert "S9" in sk.unrostered_labels


def test_unrostered_label_clears_on_bind():
    sk = make_sk()
    sk.on_transcript_segment(text="hello everyone I'm here", speaker_label="S9")
    assert "S9" in sk.unrostered_labels
    sk.bind_speaker("S9", "Marcus")
    assert "S9" not in sk.unrostered_labels
    assert sk.players["Marcus"]["speaker_label"] == "S9"


# ---------------------------------------------------------------------------
# Attribution resolver (generalized lbs_attribute_partner_b priority order)
# ---------------------------------------------------------------------------

def test_resolver_priority_speaker_id_first():
    sk = make_sk()
    sk.players["Sarah"]["speaker_id"] = "spk_abc"
    name, method = sk.resolve_speaker("spk_abc", "S2", None, "whatever")
    assert (name, method) == ("Sarah", "speaker_id")


def test_resolver_label_match():
    sk = make_sk()
    name, method = sk.resolve_speaker(None, "S2", None, "Tungsten")
    assert (name, method) == ("Dave", "label_match")


def test_resolver_exact_name_match():
    sk = make_sk()
    name, method = sk.resolve_speaker(None, None, "priya", "hi")
    assert (name, method) == ("Priya", "name_match")


def test_resolver_self_introduction_cue_gated():
    sk = make_sk()
    name, method = sk.resolve_speaker(None, None, None, "hey, my name is Dave")
    assert (name, method) == ("Dave", "self_introduction")
    # An ANSWER containing a rostered name must not misattribute.
    name, method = sk.resolve_speaker(None, None, None, "Sarah Michelle Gellar")
    assert name is None


# ---------------------------------------------------------------------------
# Scoring + state block
# ---------------------------------------------------------------------------

def test_record_result_score_and_streak():
    sk = make_sk()
    sk.record_result("Sarah", correct=True, points=2)
    sk.record_result("Sarah", correct=True, points=1)
    assert sk.players["Sarah"]["score"] == 3
    assert sk.players["Sarah"]["streak"] == 2
    sk.record_result("Sarah", correct=False, points=0)
    assert sk.players["Sarah"]["score"] == 3
    assert sk.players["Sarah"]["streak"] == 0


def test_record_result_counts_answers_correct():
    sk = make_sk()
    sk.record_result("Sarah", correct=True, points=2)
    sk.record_result("Sarah", correct=False, points=0)
    sk.record_result("Sarah", correct=True, points=1)
    assert sk.players["Sarah"]["answers_correct"] == 2


def test_state_block_contents():
    sk = make_sk()
    sk.set_phase("round")
    sk.round = 2
    sk.record_result("Sarah", correct=True, points=2)
    sk.players["Dave"]["questions_since_spoke"] = 4
    sk.set_status_note("question machine failure: be honest")
    sk.start_question({
        "prompt": "This 1985 film?", "canonical_answer": "Back to the Future",
    })
    sk.open_answer_window(now=100.0)
    sk.on_transcript_segment(
        text="Back to the Future", speaker_label="S1",
        now=101.0, segment_start_time=101.0,
    )
    block = sk.build_state_block(now=101.5)
    assert block.startswith("[GAME STATE]")
    assert "phase=round" in block
    assert "Sarah: score=2 streak=1" in block
    # start_question bumped every player's counter (4 -> 5)
    assert "quiet for 5 questions" in block
    assert "question machine failure" in block
    assert "answer_window=open" in block
    assert "Back to the Future" in block
    assert "answered: Sarah" in block


def test_snapshot_rehydrate_roundtrip():
    sk = make_sk()
    sk.set_phase("round")
    sk.round = 2
    sk.record_result("Dave", correct=True, points=3)
    sk.start_question({"prompt": "q7", "canonical_answer": "42"})
    snap = sk.snapshot()

    sk2 = LilyScorekeeper(session_id="test-room")
    sk2.rehydrate(snap)
    assert sk2.phase == "round"
    assert sk2.round == 2
    assert sk2.question_number == sk.question_number
    assert sk2.players["Dave"]["score"] == 3
    assert sk2.current_answer == "42"


def test_state_block_question_counter_is_within_round():
    """Regression: the cumulative counter rendered against per-round size
    ("question=7/6") read as overtime and steered the host toward ending
    the game after round one."""
    sk = make_sk()
    sk.rounds_total = 3
    sk.questions_per_round = 6
    sk.round = 2
    for _ in range(7):
        sk.start_question({"prompt": "q", "canonical_answer": "a"})
    block = sk.build_state_block()
    assert "question=7/6" not in block
    assert "question=1/6 in this round" in block
    assert "(#7 of 18 total" in block
    assert "round=2/3" in block
