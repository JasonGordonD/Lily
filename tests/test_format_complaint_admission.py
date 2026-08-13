"""WO-LILY-CANARY-DEFECTS-001 ROOT 2 — a PURE format complaint is never
scored as an answer, but a FUSED complaint+answer keeps its answer.

Live lily-A9B757 (2026-08-13, 04:38:01): "I said I don't want mcqs" landed
in the Madonna window, was admitted as an answer, scored INCORRECT, and
closed the window ~before Rami's real answer ("...it's Madonna") — which
then hit a closed window and was ignored. The format directive is not a
control command (format is set by lily_set_round_format), so it fell through
every non-answer gate and reached adjudication as a wrong answer.

Bias: false suppression is worse than the defect. Any residual answer
content — right OR wrong — makes the utterance NOT a pure directive, so the
answer clause is preserved and stays adjudicable.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_evaluation as ev


MADONNA = {
    "canonical_answer": "Madonna",
    "acceptable_answers": ["madonna"],
    "category": "pop culture",
}
ROSTER = ["Rami"]


# -- path 1: the in-window candidate recorder gate (the live path) -----------

def test_pure_format_complaint_is_a_non_answer():
    assert (
        ev.lily_non_answer_utterance("I said I don't want mcqs", MADONNA, ROSTER)
        == "format_directive"
    )


def test_pure_no_multiple_choice_is_a_non_answer():
    assert (
        ev.lily_non_answer_utterance("No multiple choice, please", MADONNA, ROSTER)
        == "format_directive"
    )


def test_fused_complaint_plus_correct_answer_is_admitted():
    # The coordinator's fused shape — Madonna survives (answer-surface
    # containment admits it before any directive check even runs).
    assert (
        ev.lily_non_answer_utterance(
            "I don't want that, but it's Madonna", MADONNA, ROSTER
        )
        is None
    )


def test_fused_format_marker_plus_wrong_answer_is_admitted():
    # A wrong answer fused with a format gripe still gets ruled on (a residual
    # answer token makes it not pure) — bias to admit.
    assert (
        ev.lily_non_answer_utterance(
            "no multiple choice, it's Sparta", MADONNA, ROSTER
        )
        is None
    )


# -- path 2: lily_answer_shaped (pre-window / early-answer surface) -----------

def test_pure_format_complaint_is_not_answer_shaped():
    assert (
        ev.lily_answer_shaped("I said I don't want mcqs", MADONNA, is_command=False)
        is False
    )


def test_fused_format_marker_plus_answer_is_answer_shaped():
    assert (
        ev.lily_answer_shaped(
            "no multiple choice, it's Madonna", MADONNA, is_command=False
        )
        is True
    )


def test_answer_mentioning_choice_without_refusal_stays_adjudicable():
    # "the choice is Madonna" mentions a choice but asks for nothing — an
    # answer, not a directive (no 'multiple choice' marker, no refusal).
    assert (
        ev.lily_answer_shaped("the choice is Madonna", MADONNA, is_command=False)
        is True
    )


def test_ordinary_wrong_answer_unaffected():
    assert (
        ev.lily_answer_shaped("Sparta", MADONNA, is_command=False) is True
    )


# -- the helper directly -----------------------------------------------------

def test_detector_pure_vs_fused():
    assert ev.lily_detect_format_directive("I don't want multiple choice") is True
    assert ev.lily_detect_format_directive("no more mcqs") is True
    assert ev.lily_detect_format_directive("stop with the multiple choice") is True
    # fused / not-pure -> False (kept adjudicable)
    assert ev.lily_detect_format_directive("no multiple choice, it's Madonna") is False
    # no format marker -> False
    assert ev.lily_detect_format_directive("I don't want that") is False
    # marker but no refusal -> False (a neutral mention)
    assert ev.lily_detect_format_directive("this is a multiple choice round") is False
