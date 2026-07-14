"""Tests for lily_evaluation — Tier-1 matcher and Tier-2 judge contract."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lily_evaluation import (
    LILY_JUDGE_INSTRUCTIONS,
    lily_build_judge_prompt,
    lily_normalize_answer,
    lily_parse_judge_response,
    lily_tier1_evaluate,
)


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def test_normalize_strips_punctuation_case_articles():
    assert lily_normalize_answer("The Great Gatsby!") == "great gatsby"


def test_normalize_strips_hedge_prefixes():
    assert lily_normalize_answer("Um, I think it's Canberra") == "canberra"
    assert lily_normalize_answer("the answer is Tungsten.") == "tungsten"
    assert lily_normalize_answer("is it maybe Portugal?") == "portugal"


def test_normalize_pure_filler_is_empty():
    assert lily_normalize_answer("um") == ""
    assert lily_normalize_answer("I think") == ""


# ---------------------------------------------------------------------------
# Tier 1 — exact / close / uncertain-escalates
# ---------------------------------------------------------------------------

def test_exact_match():
    r = lily_tier1_evaluate("Canberra", ["canberra"])
    assert r["verdict"] == "correct"
    assert r["method"] == "exact"


def test_hedged_exact_match():
    r = lily_tier1_evaluate("uh, I think it's Canberra?", ["canberra"])
    assert r["verdict"] == "correct"


def test_containment_match():
    r = lily_tier1_evaluate("Canberra, Australia", ["canberra"])
    assert r["verdict"] == "correct"
    assert r["method"] == "containment"


def test_close_fuzzy_match():
    # STT-mangled single-letter slip
    r = lily_tier1_evaluate("Canbera", ["canberra"])
    assert r["verdict"] == "correct"
    assert r["method"] in ("fuzzy", "phonetic")


def test_phonetic_match():
    r = lily_tier1_evaluate("Kanberra", ["canberra"])
    assert r["verdict"] == "correct"


def test_wrong_answer_escalates_never_rejects():
    """Tier 1 never returns 'incorrect' — uncertainty escalates."""
    r = lily_tier1_evaluate("Sydney", ["canberra"])
    assert r["verdict"] == "uncertain"


def test_borderline_escalates():
    r = lily_tier1_evaluate("the capital one", ["canberra"])
    assert r["verdict"] == "uncertain"


def test_empty_attempt_escalates():
    assert lily_tier1_evaluate("", ["canberra"])["verdict"] == "uncertain"
    assert lily_tier1_evaluate("um", ["canberra"])["verdict"] == "uncertain"


def test_multiple_acceptable_answers():
    r = lily_tier1_evaluate("JFK", ["john f kennedy", "kennedy", "jfk"])
    assert r["verdict"] == "correct"


# ---------------------------------------------------------------------------
# Tier 2 — judge contract
# ---------------------------------------------------------------------------

def test_judge_instructions_never_rederive():
    assert (
        "evaluates against the supplied canonical answer only" in
        LILY_JUDGE_INSTRUCTIONS
    )
    assert "never" in LILY_JUDGE_INSTRUCTIONS


def test_build_judge_prompt_ordered_attempts():
    prompt = lily_build_judge_prompt(
        "Capital of Australia?",
        "Canberra",
        [("Sarah", "is it Sydney"), ("Dave", "canberra I think")],
        acceptable_answers=["canberra"],
    )
    assert "CANONICAL ANSWER: Canberra" in prompt
    assert prompt.index("Sarah") < prompt.index("Dave")


def test_parse_judge_response_plain():
    raw = (
        '{"verdict": "correct", "winner": "Dave", '
        '"normalized_answer": "canberra", "reason": "Dave had it."}'
    )
    v = lily_parse_judge_response(raw)
    assert v["verdict"] == "correct"
    assert v["winner"] == "Dave"


def test_parse_judge_response_fenced():
    raw = '```json\n{"verdict": "partial", "winner": null, "normalized_answer": "x", "reason": "r"}\n```'
    v = lily_parse_judge_response(raw)
    assert v["verdict"] == "partial"


def test_parse_judge_response_invalid_verdict_is_none():
    assert lily_parse_judge_response('{"verdict": "meh"}') is None


def test_parse_judge_response_junk_is_none():
    assert lily_parse_judge_response("total nonsense") is None
    assert lily_parse_judge_response("") is None
