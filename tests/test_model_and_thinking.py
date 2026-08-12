"""WO-LILY-CAPABILITY-RESTORE-001 (operator model addenda 2026-08-06) —
brain/image model pins, thinking-level policy, and image provider routing.

Operator-directed swaps (override the fleet no-model-pin rule for these):
  - brain (vocal) -> grok-4.5 through the xAI/OpenAI-compatible plugin.
  - standard-deck image gen -> gemini-3.1-flash-lite-image (Nano Banana 2
    Lite); live-verified via generate_content.
  - adult-deck image gen -> xAI grok-imagine-image (Gemini refuses adult).
  - thinking_level: HIGH for content generation + adjudication (never low),
    LOW for reflexive banter, ESCALATE to HIGH on complex user turns.

Model IDs are pinned constants here (defaults) — the live resolve/status:ok
proof runs against the funded GOOGLE_API_KEY / XAI_API_KEY and is attached
to the WO report, not CI (keys + cost).
"""

import asyncio
import inspect
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_config
import lily_imagegen
import lily_reasoning
from lily_agent import (
    _lily_thinking_level_for_text,
    lily_build_grok_vocal_llm,
    lily_grok_conversation_id,
)


# -- model pins ----------------------------------------------------------------


def test_brain_model_is_grok_4_5():
    assert lily_config.vocal_model() == "grok-4.5"
    assert lily_config.vocal_effort() == "low"


def test_question_reasoning_models_are_grok_4_5():
    assert lily_config.reasoning_model() == "grok-4.5"
    assert lily_config.reasoning_effort() == "medium"
    assert lily_config.adult_reasoning_model() == "grok-4.5"
    assert lily_config.adult_reasoning_effort() == "high"
    assert lily_config.judge_model() == "grok-4.5"
    assert lily_config.judge_effort() == "medium"
    assert lily_config.assessment_model() == "grok-4.5"
    assert lily_config.assessment_effort() == "high"


def test_vocal_effort_is_low_by_default_contract():
    assert lily_config.vocal_effort() == "low"


def test_grok_vocal_requires_xai_key():
    try:
        lily_build_grok_vocal_llm(
            model="grok-4.5", effort="low", api_key=""
        )
        raise AssertionError("missing XAI key must fail")
    except RuntimeError as exc:
        assert "XAI_API_KEY" in str(exc)


def test_entrypoint_uses_grok_for_general_vocal():
    from lily_agent import entrypoint

    source = inspect.getsource(entrypoint)
    assert "lily_build_grok_vocal_llm(" in source
    assert "api_key=lily_config.xai_api_key()" in source
    assert "GoogleLLM(" not in source


def test_grok_builder_retains_spoken_turn_token_cap():
    from lily_agent import lily_build_grok_vocal_llm

    source = inspect.getsource(lily_build_grok_vocal_llm)
    assert "max_completion_tokens" in source
    assert "vocal_max_output_tokens" in source


def test_grok_vocal_sets_session_stable_cache_routing_header():
    conversation_id = lily_grok_conversation_id("lily-private-room-name")
    llm = lily_build_grok_vocal_llm(
        model="grok-4.5",
        effort="low",
        api_key="xai-test",
        conversation_id=conversation_id,
    )
    assert llm._client.default_headers["x-grok-conv-id"] == conversation_id
    assert "private-room-name" not in conversation_id
    assert conversation_id == lily_grok_conversation_id(
        "lily-private-room-name"
    )
    assert conversation_id != lily_grok_conversation_id("another-session")


def test_standard_imagegen_is_nano_banana_2_lite():
    assert lily_config.imagegen_model() == "gemini-3.1-flash-lite-image"


def test_adult_imagegen_routes_to_grok():
    assert lily_config.adult_imagegen_model() == "grok-imagine-image"


def test_image_and_brain_pins_are_separate_constants():
    # 3.6-flash does NOT do image generation — the pins must not collapse.
    assert lily_config.vocal_model() != lily_config.imagegen_model()
    assert lily_config.vocal_model() != lily_config.adult_imagegen_model()


# -- thinking-level policy -----------------------------------------------------


def test_generation_thinking_is_high_never_low():
    # Operator firm rule: content generation is never low.
    assert lily_reasoning.REASONING_THINKING_LEVEL == "high"


def test_adjudication_thinking_is_high():
    # Tier-2 adjudication of a close/ambiguous answer is high-stakes.
    assert lily_reasoning.JUDGE_THINKING_LEVEL == "high"


def test_banter_stays_low():
    for turn in [
        "haha nice",
        "yeah",
        "okay let's go",
        "Sarah's up next",
        "woo!",
    ]:
        assert _lily_thinking_level_for_text(turn) == "low", turn


def test_complex_turns_escalate_to_high():
    for turn in [
        "wait that's not right, the answer should be Paris",  # dispute
        "why does that count? she said it after the buzzer",   # adjudication
        "actually I think you scored that wrong",              # dispute
        "that's unfair, the rule says otherwise",              # rules
        # multi-step / ambiguous / long
        "can you do a round about the Ming dynasty but only the "
        "emperors and also skip the really obscure ones please",
        "what if two of us answer at once? and who gets the point?",
    ]:
        assert _lily_thinking_level_for_text(turn) == "high", turn


def test_empty_or_none_turn_is_low():
    assert _lily_thinking_level_for_text("") == "low"
    assert _lily_thinking_level_for_text(None) == "low"


def test_llm_node_depth_is_per_call_not_mutated_on_shared_opts():
    # W2b: vocal reasoning depth is chosen PER CALL and never mutated onto the
    # shared llm._opts. The old mutate-to-medium/restore-in-finally dance let
    # two overlapping generations leak one turn's depth onto the other (a
    # greeting rendered at "medium"). The default is lifted off the shared
    # opts ONCE; each turn's depth then rides extra_kwargs on its own chat().
    from lily_agent import LilyAgent
    from livekit.agents.types import NOT_GIVEN

    agent = LilyAgent.__new__(LilyAgent)
    opts = SimpleNamespace(reasoning_effort="low")
    object.__setattr__(agent, "_llm", SimpleNamespace(_opts=opts))

    # One-time normalization lifts the configured default OFF the shared opts.
    agent._ensure_vocal_depth_unshared()
    assert opts.reasoning_effort is NOT_GIVEN
    assert agent._vocal_effort_default == "low"

    # High turn -> medium (per call); every other turn -> the snapshotted
    # default. Both are returned as kwargs, never written back to _opts.
    agent._thinking_level_for_turn = lambda ctx: "high"
    assert agent._vocal_depth_for_turn(None) == {"reasoning_effort": "medium"}
    agent._thinking_level_for_turn = lambda ctx: "low"
    assert agent._vocal_depth_for_turn(None) == {"reasoning_effort": "low"}

    # The shared opts is NEVER re-mutated by depth selection — there is no
    # window in which an overlapping generation could read a leaked value.
    assert opts.reasoning_effort is NOT_GIVEN


def test_llm_node_depth_thinking_config_transport():
    # The Google transport carries depth as thinking_config, not
    # reasoning_effort; the per-call selector picks the right field and never
    # emits one the plugin does not accept.
    from lily_agent import LilyAgent
    from livekit.agents.types import NOT_GIVEN

    agent = LilyAgent.__new__(LilyAgent)
    opts = SimpleNamespace(thinking_config={"thinking_level": "low"})
    object.__setattr__(agent, "_llm", SimpleNamespace(_opts=opts))

    agent._ensure_vocal_depth_unshared()
    assert opts.thinking_config is NOT_GIVEN
    assert agent._vocal_thinking_default == {"thinking_level": "low"}

    agent._thinking_level_for_turn = lambda ctx: "high"
    assert agent._vocal_depth_for_turn(None) == {
        "thinking_config": {"thinking_level": "high"}
    }
    agent._thinking_level_for_turn = lambda ctx: "low"
    assert agent._vocal_depth_for_turn(None) == {
        "thinking_config": {"thinking_level": "low"}
    }


# -- image provider routing ----------------------------------------------------


def test_adult_mode_routes_image_gen_to_xai(monkeypatch):
    called = {}

    async def _fake_xai(prompt, *, model=None):
        called["xai"] = (prompt, model)
        return (b"xai-bytes", "image/jpeg", "grok-imagine-image")

    monkeypatch.setattr(lily_imagegen, "_generate_image_bytes_xai", _fake_xai)
    data, mime, mdl = asyncio.new_event_loop().run_until_complete(
        lily_imagegen.lily_generate_image_bytes("a scene", mode="adult")
    )
    # The adult style/intensity chokepoint (7be4fef) prepends the base
    # scene with the register-tagged art direction — the routing is what
    # this test pins, so assert the base prompt rides through, not equality.
    assert called["xai"][0].startswith("a scene")
    assert mdl == "grok-imagine-image"
    assert data == b"xai-bytes"


def test_general_mode_does_not_touch_xai(monkeypatch):
    # General deck must NEVER hit the adult provider.
    async def _boom(prompt, *, model=None):
        raise AssertionError("general mode must not route to xAI")

    monkeypatch.setattr(lily_imagegen, "_generate_image_bytes_xai", _boom)

    # Stub the Gemini client so no network call happens; assert general
    # takes the Gemini branch (model = the standard Lite pin).
    class _Inline:
        data = b"gemini-bytes"
        mime_type = "image/jpeg"

    class _Part:
        inline_data = _Inline()
        text = None

    class _Content:
        parts = [_Part()]

    class _Cand:
        content = _Content()

    class _Resp:
        candidates = [_Cand()]

    class _Models:
        def generate_content(self, **kw):
            return _Resp()

    class _Client:
        def __init__(self, **kw):
            self.models = _Models()

    monkeypatch.setattr(lily_imagegen.google_genai, "Client", _Client)
    monkeypatch.setattr(lily_config, "google_api_key", lambda: "k")
    data, mime, mdl = asyncio.new_event_loop().run_until_complete(
        lily_imagegen.lily_generate_image_bytes("a scene", mode="general")
    )
    assert data == b"gemini-bytes"
    assert mdl == "gemini-3.1-flash-lite-image"


def test_default_mode_is_general():
    sig = inspect.signature(lily_imagegen.lily_generate_image_bytes)
    assert sig.parameters["mode"].default == "general"
