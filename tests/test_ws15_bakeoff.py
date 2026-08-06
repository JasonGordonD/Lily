"""WS-15 diarization bake-off harness tests (WO-LILY-OMNIBUS-003, AMENDMENT-002).

Pure-python: no network, no vendor SDK, no torch. Exercises the word-alignment
glue (the pyannote Live-1 integration path), the four game-level metrics, and
the credential-gated challenger arm.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_EVAL = Path(__file__).resolve().parents[1] / "eval" / "ws15_diar_bakeoff"
sys.path.insert(0, str(_EVAL))

import game_metrics as gm  # noqa: E402
import diar_providers as dp  # noqa: E402


# --- ground truth used across tests -----------------------------------------
# Two players; ans0 by A (0-2s), chatter by B under it, ans1 by A (5-7s).
TURNS = [
    {"speaker": "A", "start": 0.0, "end": 2.0, "text": "the femur", "is_answer": True},
    {"speaker": "B", "start": 0.5, "end": 1.5, "text": "no way", "is_answer": False},
    {"speaker": "A", "start": 5.0, "end": 7.0, "text": "carbon", "is_answer": True},
]


# --- word_align (challenger integration path) -------------------------------

def test_word_align_assigns_by_overlap():
    words = [(0.1, 0.9, "the", "?"), (1.0, 1.9, "femur", "?"), (5.1, 6.9, "carbon", "?")]
    diar = [{"speaker": "spk0", "start": 0.0, "end": 2.0},
            {"speaker": "spk1", "start": 5.0, "end": 7.0}]
    out = gm.word_align(words, diar)
    assert [w[3] for w in out] == ["spk0", "spk0", "spk1"]
    # text + timestamps untouched
    assert [w[2] for w in out] == ["the", "femur", "carbon"]


def test_word_align_nearest_when_no_overlap():
    words = [(3.0, 3.4, "gap", "?")]
    diar = [{"speaker": "spk0", "start": 0.0, "end": 2.0},
            {"speaker": "spk1", "start": 5.0, "end": 7.0}]
    assert gm.word_align(words, diar)[0][3] == "spk0"  # 3.0 nearer to 2.0 than 5.0


def test_word_align_empty_diar_is_passthrough():
    words = [(0.0, 1.0, "x", "orig")]
    assert gm.word_align(words, []) == words


# --- phantom_label_count ----------------------------------------------------

def test_phantom_zero_when_labels_track_speakers():
    # 2 real speakers (A, B) -> 2 clusters is not excess
    words = [(0.1, 1.9, "the femur", "A"), (0.5, 1.4, "no way", "B"),
             (5.1, 6.9, "carbon", "A")]
    assert gm.phantom_label_count(words, TURNS)["phantom_label_count"] == 0


def test_phantom_counts_excess_cluster():
    # 3 hyp clusters over a 2-speaker roster -> 1 phantom, named as the
    # lowest-speaking-time extra ('C')
    words = [(0.1, 1.9, "the femur", "A"), (0.5, 1.4, "no way", "B"),
             (5.1, 6.9, "carbon", "A"), (5.5, 5.6, "x", "C")]
    r = gm.phantom_label_count(words, TURNS)
    assert r["phantom_label_count"] == 1 and r["phantom_labels"] == ["C"]


def test_phantom_multiple_excess():
    words = [(0.1, 1.9, "a", "A"), (0.5, 1.4, "b", "B"),
             (5.1, 6.9, "c", "C"), (5.2, 5.3, "d", "D")]
    assert gm.phantom_label_count(words, TURNS)["phantom_label_count"] == 2


# --- answer_attribution_accuracy --------------------------------------------

def test_attribution_perfect():
    words = [(0.1, 1.9, "the femur", "A"), (5.1, 6.9, "carbon", "A")]
    assert gm.answer_attribution_accuracy(words, TURNS)["answer_attribution_accuracy"] == 1.0


def test_attribution_wrong_speaker():
    # Non-overlapping turns: cluster c0 covers A's answer AND B's longer non-answer
    # span, so c0's majority maps to B -> A's first answer is mis-credited.
    turns2 = [
        {"speaker": "A", "start": 0.0, "end": 2.0, "text": "femur", "is_answer": True},
        {"speaker": "B", "start": 3.0, "end": 5.0, "text": "chatter", "is_answer": False},
        {"speaker": "A", "start": 6.0, "end": 8.0, "text": "carbon", "is_answer": True},
    ]
    words = [(0.1, 1.9, "femur", "c0"), (3.0, 5.0, "chatter", "c0"),
             (6.1, 7.9, "carbon", "c1")]
    r = gm.answer_attribution_accuracy(words, turns2)
    assert r["n_answers"] == 2 and r["correct"] == 1  # ans0 wrong, ans1 right


def test_attribution_missing_answer_is_miss():
    words = [(0.1, 1.9, "the femur", "A")]  # ans1 has no words
    r = gm.answer_attribution_accuracy(words, TURNS)
    assert r["n_answers"] == 2 and r["correct"] == 1
    assert r["answer_attribution_accuracy"] == 0.5


# --- dropped_answer_rate ----------------------------------------------------

def test_dropped_zero_full_coverage():
    words = [(0.0, 2.0, "the femur", "A"), (5.0, 7.0, "carbon", "A")]
    assert gm.dropped_answer_rate(words, TURNS)["dropped_answer_rate"] == 0.0


def test_dropped_answer_when_buried():
    # ans1 (5-7s) has no hypothesis words -> dropped
    words = [(0.0, 2.0, "the femur", "A")]
    r = gm.dropped_answer_rate(words, TURNS)
    assert r["dropped"] == 1 and r["dropped_answer_rate"] == 0.5


# --- first_answer_timestamp_fidelity ----------------------------------------

def test_first_answer_fidelity_exact():
    words = [(0.0, 2.0, "the femur", "A"), (5.0, 7.0, "carbon", "A")]
    r = gm.first_answer_timestamp_fidelity(words, TURNS)
    assert r["first_answer_timestamp_fidelity"] == 1.0 and r["delta_s"] == 0.0


def test_first_answer_fidelity_partial():
    words = [(1.0, 2.0, "femur", "A")]  # 1.0s late onset, tol 2.0 -> 0.5
    r = gm.first_answer_timestamp_fidelity(words, TURNS)
    assert r["first_answer_timestamp_fidelity"] == pytest.approx(0.5, abs=1e-6)


def test_first_answer_fidelity_zero_when_absent():
    words = [(5.0, 7.0, "carbon", "A")]  # nothing in first-answer span
    r = gm.first_answer_timestamp_fidelity(words, TURNS)
    assert r["first_answer_timestamp_fidelity"] == 0.0


# --- adapters + shared WS-13 DER wiring --------------------------------------

def test_words_to_segments_merges_runs():
    words = [(0.0, 1.0, "a", "A"), (1.1, 2.0, "b", "A"), (2.2, 3.0, "c", "B")]
    segs = gm.words_to_segments(words)
    assert len(segs) == 2 and segs[0]["speaker"] == "A" and segs[0]["end"] == 2.0


def test_perfect_labeling_scores_zero_der():
    from lily_stt_tuning import lily_der
    words = [(0.0, 2.0, "the femur", "A"), (0.5, 1.5, "no way", "B"),
             (5.0, 7.0, "carbon", "A")]
    der = lily_der(gm.turns_to_segments(TURNS), gm.words_to_segments(words))
    assert der < 0.15  # near-perfect; small edge slack from segment merge


def test_all_game_metrics_bundles_four():
    words = [(0.0, 2.0, "the femur", "A"), (5.0, 7.0, "carbon", "A")]
    m = gm.all_game_metrics(words, TURNS)
    for k in ("phantom_label_count", "answer_attribution_accuracy",
              "dropped_answer_rate", "first_answer_timestamp_fidelity"):
        assert k in m


# --- challenger credential gate ---------------------------------------------

def test_challenger_unavailable_without_key(monkeypatch):
    monkeypatch.delenv("PYANNOTEAI_API_KEY", raising=False)
    monkeypatch.delenv("PYANNOTE_API_KEY", raising=False)
    ch = dp.PyannoteLive1Challenger()
    assert ch.available() is False
    import asyncio
    with pytest.raises(dp.ChallengerUnavailable):
        asyncio.run(ch.diarize(__import__("numpy").zeros(1600, dtype="int16")))


def test_challenger_reports_available_with_key(monkeypatch):
    monkeypatch.setenv("PYANNOTEAI_API_KEY", "sk-test")
    assert dp.PyannoteLive1Challenger().available() is True


def test_incumbent_available_reflects_key(monkeypatch):
    monkeypatch.delenv("SPEECHMATICS_API_KEY", raising=False)
    assert dp.SpeechmaticsIncumbent().available() is False
    monkeypatch.setenv("SPEECHMATICS_API_KEY", "sm-test")
    assert dp.SpeechmaticsIncumbent().available() is True
