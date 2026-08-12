"""WO-LILY-LIVEFIRE-001 CLASS 1 — SPOKEN SCORE = LEDGER ONLY.

Fixture: session lily-639007-f80aa6bf. At 17:58:45 the reveal aired
"Rami, you're at three, streak of three." while the committed ledger read
score=2 streak=2 (SCORE_COMMIT 17:58:26). The X1 detector logged the
divergence but the sentence had already gone to TTS. Class 1 makes it a
gate: the organic lane is color only, every number is printed by the spine
from the ledger authority, and the offending sentence is suppressed and
re-emitted as ONE template line. Suppress-and-reemit, never in-place.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_scorekeeper
from lily_scorekeeper import (
    LilyScorekeeper,
    lily_score_line_gate,
    lily_ledger_score_line,
    _sentence_narrates_state,
)


# -- the fixture line cannot air ------------------------------------------

def test_athens_three_vs_two_line_cannot_air():
    # The exact aired reveal, with the committed ledger of that moment.
    text = "[excited] Athens — that's the one. Rami, you're at three, streak of three."
    kept, suppressed, line = lily_score_line_gate(
        text, {"Rami": 2}, {"Rami": 2}
    )
    # The number-bearing sentence is gone; the color sentence stays.
    assert "three" not in kept.lower()
    assert "Athens" in kept
    assert suppressed  # the 3/3 sentence was suppressed
    # The re-emitted line carries the LEDGER truth (two), not three.
    assert "two" in line and "three" not in line


def test_no_organic_total_or_streak_reaches_tts():
    # Every score-grammar variant from 1c is caught, spelled and digit.
    ledger = {"Rami": 2}
    for line in [
        "that's two for you, Rami",
        "you're on the board at one, streak lit",
        "that's four straight for you",
        "you're sitting on three",
        "you're at 3 points",
        "streak of three",
        "still at zero",
        "that puts you at 5",
    ]:
        assert _sentence_narrates_state(line), line
        kept, suppressed, _ = lily_score_line_gate(line, ledger, {"Rami": 2})
        assert suppressed and not kept.strip(), line


# -- streak has its own authority gate (1d) -------------------------------

def test_streak_of_three_at_ledger_two_cannot_air():
    kept, suppressed, line = lily_score_line_gate(
        "Rami, streak of three, keep it rolling.", {"Rami": 2}, {"Rami": 2}
    )
    assert suppressed
    assert "streak of two" in line


# -- color survives, non-score numbers survive ----------------------------

def test_color_only_sentence_is_untouched():
    text = "[excited] Crete — labyrinth, Minotaur, the whole island. That's yours."
    kept, suppressed, line = lily_score_line_gate(text, {"Rami": 3}, {"Rami": 3})
    assert not suppressed
    assert kept == text and line == ""


def test_question_stem_number_is_not_a_score():
    # A year in a question stem must not read as a score total.
    text = "In what year did the Peloponnesian War begin? Was it 431 BC?"
    _, suppressed, _ = lily_score_line_gate(text, {"Rami": 2}, {"Rami": 2})
    assert not suppressed


def test_empty_ledger_is_pass_through():
    text = "Rami, you're at three, streak of three."
    kept, suppressed, line = lily_score_line_gate(text, {}, {})
    assert kept == text and not suppressed and line == ""


# -- ledger authority accessors -------------------------------------------

def test_ledger_streaks_reads_committed_rows():
    sk = LilyScorekeeper("class1")
    sk.players = {"Rami": {"score": 0, "streak": 0, "answers_correct": 0}}
    sk.apply_score_event("Rami", cause="answer", correct=True, points=1,
                         question_id="q1")
    sk.apply_score_event("Rami", cause="answer", correct=True, points=1,
                         question_id="q2")
    assert sk.ledger_scores() == {"Rami": 2}
    assert sk.ledger_streaks() == {"Rami": 2}
    sk.apply_score_event("Rami", cause="answer", correct=False, points=0,
                         question_id="q3")
    assert sk.ledger_streaks() == {"Rami": 0}


def test_ledger_line_multiplayer_template():
    line = lily_ledger_score_line({"Rami": 3, "Sam": 1}, {"Rami": 3, "Sam": 0})
    assert "Rami at three" in line and "Sam at one" in line
    assert "streak of three" in line  # Rami on a live streak
