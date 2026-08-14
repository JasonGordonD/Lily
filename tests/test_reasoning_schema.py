"""Tests for the reasoning-node structured-output contract (P1 fix).

2026-07-14 production sessions failed with QUESTION_PARSE_FAILED ->
PREFETCH_FAILED. Two root causes, both pinned here:

1. Free-text JSON parsing: generation/verification now set BOTH
   response_mime_type="application/json" AND a response_schema; the
   fence-stripping parser is retired to a defensive last resort.
2. Token starvation (19:27 logs: raw_prefix started as VALID JSON, i.e.
   truncation): on Gemini 3.x thinking tokens count toward
   max_output_tokens, and the reasoning node shared the vocal node's
   800-token budget. Generation/verification now run on a dedicated
   LILY_REASONING_MAX_OUTPUT_TOKENS budget (default 4096; prefetch is off
   the hot path) and the Tier-2 judge on LILY_JUDGE_MAX_OUTPUT_TOKENS
   (default 1024; latency-relevant, small verdict).

This file imports lily_reasoning (the Grok reasoning transport).
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_config
from lily_reasoning import (
    LilyReasoning,
    _shape_question,
    lily_parse_question_json,
)

VALID_QUESTION = {
    "id": "q_7294",
    "category": "pop culture",
    "difficulty_tier": 2,
    "prompt": "Name this 1985 film.",
    "canonical_answer": "Back to the Future",
    "acceptable_answers": ["back to the future"],
    "reveal_color": "It nearly starred Eric Stoltz.",
}


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _reasoning_with_stub(monkeypatch_target: dict, raw: str) -> LilyReasoning:
    """LilyReasoning with both transports stubbed for lane-specific tests."""
    r = LilyReasoning.__new__(LilyReasoning)
    r._model = "test-reasoning-model"
    r._vocal_model = "test-vocal-model"

    async def _fake_generate(model, prompt, thinking_level, **kwargs):
        monkeypatch_target.update(
            model=model, prompt=prompt, thinking_level=thinking_level, **kwargs
        )
        return raw

    async def _fake_grok(prompt, **kwargs):
        monkeypatch_target.update(prompt=prompt, **kwargs)
        return raw

    r._generate = _fake_generate
    r._generate_grok_json = _fake_grok
    return r


# -- schema shape ---------------------------------------------------------------
# test_question_schema_has_all_current_fields,
# test_question_schema_reserves_future_subagent_fields, and
# test_verification_schema_shape DELETED (WO-PRMPT-LILY-GEMINI-EXCISION-001):
# they asserted the genai_types.Schema constants (_QUESTION_RESPONSE_SCHEMA /
# _VERIFICATION_RESPONSE_SCHEMA) that were removed with the reasoning node's
# dead google-genai lane. The live Grok path pins the same shapes via the
# _GROK_*_SHAPE_ADDENDUM prompt strings, exercised by the call-contract tests
# below.


# -- generation call contract -----------------------------------------------------

def test_generate_question_sets_grok_model_effort_and_budget():
    seen: dict = {}
    r = _reasoning_with_stub(seen, json.dumps(VALID_QUESTION))
    q = _run(r.generate_question("pop culture", 2, []))
    assert q is not None and q["canonical_answer"] == "Back to the Future"
    assert seen["model"] == "grok-4.5"
    # Unified adult deck: authoring/verification always runs high.
    assert seen["effort"] == "high"
    assert seen["max_tokens"] == lily_config.reasoning_max_output_tokens()


def test_verify_question_sets_grok_model_effort_and_budget():
    seen: dict = {}
    r = _reasoning_with_stub(
        seen, json.dumps({"verdict": "pass", "reason": "checks out"})
    )
    ok, reason = _run(r.verify_question(dict(VALID_QUESTION)))
    assert ok is True and reason == "checks out"
    assert seen["model"] == "grok-4.5"
    # Unified adult deck: authoring/verification always runs high.
    assert seen["effort"] == "high"
    assert seen["max_tokens"] == lily_config.reasoning_max_output_tokens()


def test_judge_uses_grok_model_effort_and_judge_budget():
    seen: dict = {}
    r = _reasoning_with_stub(seen, '{"verdict": "correct"}')
    _run(r.judge("system", "prompt"))
    assert seen["model"] == "grok-4.5"
    assert seen["effort"] == "medium"
    assert seen["max_tokens"] == lily_config.judge_max_output_tokens()


# -- parse path: schema-mode primary, defensive fallback ---------------------------

def test_schema_mode_output_parses_without_fence_stripper():
    seen: dict = {}
    r = _reasoning_with_stub(seen, json.dumps(VALID_QUESTION))
    q = _run(r.generate_question("pop culture", 2, []))
    # LIVEFIRE-001 CLASS 6 (6e): the model's own id (q_7294) is overridden
    # with a process-unique id at shape time — the model reuses 4-digit ids
    # across generations (the live q_4821 collision), so allocation owns it.
    assert q["id"].startswith("q_gen_")
    assert q["acceptable_answers"] == ["back to the future"]


def test_fenced_output_still_recovered_by_defensive_parser():
    # Should never happen in schema mode — but the last-resort path stays.
    fenced = "```json\n" + json.dumps(VALID_QUESTION) + "\n```"
    seen: dict = {}
    r = _reasoning_with_stub(seen, fenced)
    q = _run(r.generate_question("pop culture", 2, []))
    assert q is not None and q["canonical_answer"] == "Back to the Future"


def test_garbage_output_returns_none():
    seen: dict = {}
    r = _reasoning_with_stub(seen, "the model rambled with no json at all")
    q = _run(r.generate_question("pop culture", 2, []))
    assert q is None


def test_verify_unparseable_fails_honestly():
    seen: dict = {}
    r = _reasoning_with_stub(seen, "not json")
    ok, reason = _run(r.verify_question(dict(VALID_QUESTION)))
    assert ok is False
    assert "unparseable" in reason


def test_verify_corrected_answer_applied():
    seen: dict = {}
    r = _reasoning_with_stub(seen, json.dumps({
        "verdict": "fail",
        "reason": "off by one",
        "corrected_canonical_answer": "1986",
    }))
    question = dict(VALID_QUESTION)
    ok, reason = _run(r.verify_question(question))
    assert ok is True
    assert question["canonical_answer"] == "1986"
    assert question["acceptable_answers"] == ["1986"]


def test_shape_question_defaults_and_rejects():
    shaped = _shape_question({"prompt": "p?", "canonical_answer": "a"})
    assert shaped["acceptable_answers"] == ["a"]
    assert shaped["difficulty_tier"] == 2
    assert _shape_question({"prompt": "p?"}) is None
    assert _shape_question("not a dict") is None
    # The retired-to-fallback parser still shape-checks.
    assert lily_parse_question_json("junk") is None


# -- token budgets ----------------------------------------------------------------

def test_reasoning_budget_default_and_override(monkeypatch):
    monkeypatch.delenv("LILY_REASONING_MAX_OUTPUT_TOKENS", raising=False)
    assert lily_config.reasoning_max_output_tokens() == 4096
    monkeypatch.setenv("LILY_REASONING_MAX_OUTPUT_TOKENS", "8192")
    assert lily_config.reasoning_max_output_tokens() == 8192
    # Spec §4.4 floor holds even for a misconfigured tiny budget.
    monkeypatch.setenv("LILY_REASONING_MAX_OUTPUT_TOKENS", "100")
    assert lily_config.reasoning_max_output_tokens() == 600


def test_judge_budget_default_and_override(monkeypatch):
    monkeypatch.delenv("LILY_JUDGE_MAX_OUTPUT_TOKENS", raising=False)
    assert lily_config.judge_max_output_tokens() == 1024
    monkeypatch.setenv("LILY_JUDGE_MAX_OUTPUT_TOKENS", "2048")
    assert lily_config.judge_max_output_tokens() == 2048
