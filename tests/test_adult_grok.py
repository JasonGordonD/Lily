"""HOTFIX-003 — adult deck on Grok (owner directive 2026-08-06).

Live fixture: session lily-105865, 21:33 UTC — gemini-3.6-flash refused
the spoken turn around the Kama Sutra answer (`generation blocked by
gemini: FinishReason.PROHIBITED_CONTENT`, a NON-overridable filter that
BLOCK_NONE cannot reach), producing four blocked generations, a 4x
re-acknowledged answer and ~58s of retry stall. The deck itself opened
fine; Gemini would not voice it.

The fix, pinned here:
  - adult entry swaps the session vocal LLM to xAI Grok
    (LILY_ADULT_VOCAL_MODEL, default grok-4.5, reasoning effort
    toggleable low/high, default high); every adult exit restores the
    general Gemini node;
  - adult question GENERATION and VERIFICATION route to Grok
    (LILY_ADULT_REASONING_MODEL, default grok-4.2 at high effort — the
    multi-agent tier) — Gemini refuses to even verify the material;
  - no XAI key = loud degradation to tonight's status quo, never a
    crash and never a blocked entry.
"""

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_config
import lily_reasoning
from lily_agent import LilyGame
from lily_reasoning import LilyReasoning
from lily_scorekeeper import LilyScorekeeper


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# -- config pins ---------------------------------------------------------------


def test_adult_model_pins_and_coercions(monkeypatch):
    for var in ("LILY_ADULT_VOCAL_MODEL", "LILY_ADULT_VOCAL_EFFORT",
                "LILY_ADULT_REASONING_MODEL", "LILY_ADULT_REASONING_EFFORT"):
        monkeypatch.delenv(var, raising=False)
    assert lily_config.adult_vocal_model() == "grok-4.5"
    # X13 lowered the front-facing vocal lane high -> medium; the
    # 2026-08-08 directive drops it again medium -> LOW after a starved
    # slot lost a direct address past the 3.0s budget. Thinking depth is
    # the cheapest thing to give back under contention.
    assert lily_config.adult_vocal_effort() == "low"
    assert lily_config.adult_reasoning_model() == "grok-4.5"
    assert lily_config.adult_reasoning_effort() == "high"
    monkeypatch.setenv("LILY_ADULT_VOCAL_EFFORT", "low")
    assert lily_config.adult_vocal_effort() == "low"
    monkeypatch.setenv("LILY_ADULT_VOCAL_EFFORT", "medium")
    assert lily_config.adult_vocal_effort() == "medium"
    monkeypatch.setenv("LILY_ADULT_VOCAL_EFFORT", "garbage")
    assert lily_config.adult_vocal_effort() == "low"
    monkeypatch.setenv("LILY_ADULT_REASONING_EFFORT", "off")
    assert lily_config.adult_reasoning_effort() == "high"


def test_adult_reasoning_effort_is_always_high(monkeypatch):
    """Adult sub-theme/category/question authorship never downgrades."""
    monkeypatch.delenv("LILY_ADULT_REASONING_EFFORT", raising=False)
    assert lily_config.adult_reasoning_effort() == "high"
    # A caller pins its own tier.
    assert lily_config.adult_reasoning_effort("high") == "high"
    assert lily_config.adult_reasoning_effort("low") == "high"
    # "off" injects as "send no parameter at all".
    assert lily_config.adult_reasoning_effort("off") == "high"
    # An unrecognised injection falls back to the CONFIGURED value rather
    # than silently substituting a tier the operator never chose — and
    # never raises, because a bad string must not take a lane down.
    assert lily_config.adult_reasoning_effort("turbo") == "high"
    monkeypatch.setenv("LILY_ADULT_REASONING_EFFORT", "low")
    assert lily_config.adult_reasoning_effort("turbo") == "high"
    # Injection still beats the environment.
    assert lily_config.adult_reasoning_effort("high") == "high"


# -- the vocal swap ------------------------------------------------------------


class _FakeAgent:
    def __init__(self):
        self.updates = []

    def update_options(self, **kwargs):
        self.updates.append(kwargs)


def _make_game():
    game = LilyGame.bare()
    game.sk = LilyScorekeeper("adult-grok-fixture")
    game.agent = _FakeAgent()
    game._general_llm = object()
    return game


def test_enter_adult_vocal_swaps_and_exit_restores(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "xai-test-key")
    game = _make_game()
    sentinel = object()
    game._adult_llm = sentinel  # constructed-once cache path
    assert game.enter_adult_vocal() is True
    assert game.agent.updates == [{"llm": sentinel}]
    game.exit_adult_vocal()
    assert game.agent.updates[-1] == {"llm": game._general_llm}


def test_enter_adult_vocal_without_key_degrades_loudly(monkeypatch, caplog):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    game = _make_game()
    with caplog.at_level(logging.ERROR):
        assert game.enter_adult_vocal() is False
    assert game.agent.updates == []  # Gemini stays — degraded, not broken
    assert any("SWAP_UNAVAILABLE" in r.message for r in caplog.records)


def test_adult_entry_and_every_exit_are_hooked():
    """Source-level pin: the enter tool swaps in; all three exit paths
    (back-to-normal, child-signal veto, child-gate lost) swap out."""
    # REFACTOR W3: one exit path (child-gate-lost, in on_transcript_event) moved
    # to lily_glass with the transcript surface; scan both owner modules.
    root = Path(lily_reasoning.__file__).parent
    src = root.joinpath("lily_agent.py").read_text(encoding="utf-8")
    glass = root.joinpath("lily_glass.py").read_text(encoding="utf-8")
    combined = src + glass
    assert combined.count('getattr(self, "exit_adult_vocal", lambda: None)()') == 3
    assert 'getattr(self._game, "enter_adult_vocal", lambda: None)()' in src


# -- generation routing --------------------------------------------------------


def _make_reasoning():
    r = LilyReasoning.__new__(LilyReasoning)
    r._model = "gemini-test"
    r.calls = {"grok": [], "gemini": []}

    async def _fake_grok(prompt, **kwargs):
        r.calls["grok"].append(prompt)
        return (
            '{"id": "q_0001", "category": "spice", "difficulty_tier": 2, '
            '"prompt": "Test?", "canonical_answer": "yes", '
            '"acceptable_answers": ["yes"], "reveal_color": "spicy."}'
        )

    async def _fake_gemini(model, prompt, level, **kwargs):
        r.calls["gemini"].append(prompt)
        return (
            '{"id": "q_0002", "category": "plain", "difficulty_tier": 2, '
            '"prompt": "Test?", "canonical_answer": "no", '
            '"acceptable_answers": ["no"], "reveal_color": "plain."}'
        )

    r._generate_grok_json = _fake_grok
    r._generate = _fake_gemini
    return r


def test_adult_question_generation_routes_to_grok():
    r = _make_reasoning()
    q = _run(r.generate_question("after dark", 2, "adult", []))
    assert q is not None and q["canonical_answer"] == "yes"
    assert len(r.calls["grok"]) == 1 and not r.calls["gemini"]
    # The JSON shape addendum rides the Grok prompt (no server schema).
    assert "ONLY a JSON object" in r.calls["grok"][0]


def test_general_question_generation_routes_to_grok():
    r = _make_reasoning()
    q = _run(r.generate_question("history", 2, "general", []))
    assert q is not None and q["canonical_answer"] == "yes"
    assert len(r.calls["grok"]) == 1 and not r.calls["gemini"]


def test_adult_verification_routes_to_grok():
    r = _make_reasoning()

    async def _fake_grok(prompt, **kwargs):
        r.calls["grok"].append(prompt)
        return '{"verdict": "pass", "reason": "checks out"}'

    r._generate_grok_json = _fake_grok
    ok, reason = _run(
        r.verify_question({"prompt": "Test?", "canonical_answer": "yes"},
                          mode="adult")
    )
    assert ok is True
    assert len(r.calls["grok"]) == 1 and not r.calls["gemini"]


def test_grok_transport_requires_key(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    r = LilyReasoning.__new__(LilyReasoning)
    try:
        _run(r._generate_grok_json("prompt", max_tokens=100))
        raise AssertionError("must raise without XAI_API_KEY")
    except RuntimeError as e:
        assert "XAI_API_KEY" in str(e)


# -- Tier-2 judge provider routing --------------------------------------------


def _judge_probe(monkeypatch, *, adult, has_xai_key=True):
    """Run judge() with both transports stubbed; report which one fired."""
    import asyncio

    import lily_reasoning

    calls = {"gemini": 0, "grok": 0}
    r = lily_reasoning.LilyReasoning.__new__(lily_reasoning.LilyReasoning)
    r._vocal_model = "gemini-3.6-flash"

    async def fake_gemini(model, prompt, level, **kw):
        calls["gemini"] += 1
        return "{}"

    async def fake_grok(prompt, **kw):
        calls["grok"] += 1
        return "{}"

    monkeypatch.setattr(r, "_generate", fake_gemini, raising=False)
    monkeypatch.setattr(r, "_generate_grok_json", fake_grok, raising=False)
    monkeypatch.setattr(
        lily_config, "xai_api_key", lambda: "xai-test" if has_xai_key else None
    )
    asyncio.run(r.judge("instructions", "prompt", adult=adult))
    return calls


def test_judge_routes_to_grok_on_the_adult_deck(monkeypatch):
    """The judge used to run on Gemini unconditionally — including
    mid-adult-session, sending adult content to the one provider the
    session had already swapped AWAY from. PROHIBITED_CONTENT is not a
    settable safety category, so it blocked, the 12s bound fired, and
    adjudication silently degraded to Tier-1: twelve seconds of stall per
    close call, with a weaker ruling to show for it."""
    calls = _judge_probe(monkeypatch, adult=True)
    assert calls["grok"] == 1
    assert calls["gemini"] == 0


def test_judge_routes_to_grok_on_the_general_deck(monkeypatch):
    calls = _judge_probe(monkeypatch, adult=False)
    assert calls["gemini"] == 0
    assert calls["grok"] == 1


def test_judge_provider_does_not_change_by_deck_or_key_probe(monkeypatch):
    calls = _judge_probe(monkeypatch, adult=True, has_xai_key=False)
    assert calls["gemini"] == 0
    assert calls["grok"] == 1
