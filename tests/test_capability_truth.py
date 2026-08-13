"""WO-LILY-CAPABILITY-RESTORE-001 Task 4 — the capability truth test.

Lily reports what the manifest / availability layer tells her. The
2026-08-06 session exposed the failure mode: she can HONESTLY relay a
LYING config — announce a capability off (or deny one entirely) because
the manifest, the prompt, or the runtime flag disagreed with what the
code actually does. This suite makes the three surfaces provably agree,
failing on mismatch in EITHER direction:

    what she CLAIMS   (prompts/lily_system.txt options block)
      == what the manifest SAYS   (lily_capabilities.LILY_CAPABILITIES)
        == what ACTUALLY WORKS    (registered tools + resolving code +
                                   availability flags wired to real deps)

test_capability_lint.py already pins one direction (every REGISTERED
tool maps to an entry, every code_ref resolves). This file pins the
directions it does NOT:

  - every manifest-NAMED tool is actually a registered function tool
    (a manifest can name a tool that was never wired — a lie the lint
    above never sees);
  - every askable feature is claimed in the prompt, and every non-askable
    one is deliberately unclaimed (manifest <-> claims);
  - every availability_key is computed at runtime from a REAL dependency
    check, never a hardcoded literal (the "switched on tonight" line she
    relays must track reality — the core anti-lying-config guard);
  - the two capabilities that regressed on 2026-08-06 (vision, custom
    category) are INVOKED and asserted against their real contracts.
"""

import importlib
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_capabilities
import lily_config
import lily_say_gate
import lily_vision
import lily_voice_switch
from lily_agent import LilyAgent, LilyGame

from livekit.agents.llm import tool_context

REPO = Path(__file__).resolve().parent.parent
AGENT_SRC = (REPO / "lily_agent.py").read_text(encoding="utf-8")
PROMPT = (REPO / "prompts" / "lily_system.txt").read_text(encoding="utf-8")

# Module-level tools passed to LilyAgent via tools=[...] at construction.
TOOL_MODULES = (lily_voice_switch, lily_vision)


def _registered_tool_names() -> set:
    names = set()
    for name, member in inspect.getmembers(LilyAgent):
        try:
            if tool_context.is_function_tool(member) or (
                tool_context.is_raw_function_tool(member)
            ):
                names.add(name)
        except Exception:
            continue
    for module in TOOL_MODULES:
        for name, member in inspect.getmembers(module):
            try:
                if tool_context.is_function_tool(member):
                    names.add(name)
            except Exception:
                continue
    return names


def _options_block() -> str:
    start = PROMPT.index("## WHAT THE TABLE CAN ASK FOR")
    end = PROMPT.index("## ", start + 10)
    return PROMPT[start:end]


def _resolve_code_ref(ref: str):
    module_name, _, attr_path = ref.partition(":")
    obj = importlib.import_module(module_name)
    for part in [p for p in attr_path.split(".") if p]:
        obj = getattr(obj, part)
    return obj


def _availability_flags_source() -> str:
    """The entrypoint block that builds game.availability_flags."""
    start = AGENT_SRC.index("game.availability_flags = {")
    end = AGENT_SRC.index("}", start)
    return AGENT_SRC[start : end + 1]


# -- manifest <-> WORKS: every named tool is really registered ----------------


def test_every_manifest_named_tool_is_a_registered_function_tool():
    """The direction capability_lint can't see: a manifest entry may NAME a
    tool that was never wired onto the agent — a capability she'd claim and
    the LLM could never call. Every tool listed in any entry must be live."""
    registered = _registered_tool_names()
    named = set()
    for entry in lily_capabilities.LILY_CAPABILITIES:
        named.update(entry.get("tools") or [])
    phantom = sorted(named - registered)
    assert not phantom, (
        f"manifest names tools that are not registered function tools: "
        f"{phantom} — either wire the tool onto LilyAgent / a tools=[...] "
        "module, or correct the manifest entry's `tools` list"
    )


def test_every_manifest_code_ref_resolves_to_live_code():
    orphans = []
    for entry in lily_capabilities.LILY_CAPABILITIES:
        try:
            assert _resolve_code_ref(entry["code_ref"]) is not None
        except (ImportError, AttributeError, AssertionError):
            orphans.append(entry["key"])
    assert not orphans, f"manifest entries with no living code: {orphans}"


# -- manifest <-> CLAIMS -------------------------------------------------------


def test_every_askable_feature_is_claimed_in_the_prompt():
    block = _options_block()
    missing = [
        (key, marker)
        for key, marker in lily_capabilities.lily_askable_markers()
        if marker not in block
    ]
    assert not missing, (
        f"askable manifest features not claimed in WHAT THE TABLE CAN ASK "
        f"FOR: {missing}"
    )


def test_non_askable_features_carry_no_prompt_marker():
    """A feature that is not player-askable (bonus_points, group_memory)
    must not smuggle a claim into the options block — the two surfaces
    stay honest in both directions."""
    stray = [
        entry["key"]
        for entry in lily_capabilities.LILY_CAPABILITIES
        if not entry.get("askable") and entry.get("prompt_marker")
    ]
    assert not stray, f"non-askable features carrying a prompt_marker: {stray}"


# -- availability layer: flags track REAL dependencies, never literals --------


def test_every_availability_key_is_computed_at_runtime():
    src = _availability_flags_source()
    missing = [
        entry["availability_key"]
        for entry in lily_capabilities.LILY_CAPABILITIES
        if entry.get("availability_key")
        and f'"{entry["availability_key"]}"' not in src
    ]
    assert not missing, (
        f"manifest availability_keys with no runtime flag in the entrypoint "
        f"(they would be permanently OFF — a lie in the other direction): "
        f"{missing}"
    )


def test_availability_flags_are_dependency_checks_not_hardcoded_literals():
    """The anti-lying-config core: a flag hardcoded True/False would make
    'switched on tonight' a fiction she relays honestly. Every flag value
    must be an expression that consults a real dependency."""
    src = _availability_flags_source()
    for bad in (": True", ": False", ":True", ":False"):
        assert bad not in src, (
            f"availability flag hardcoded to a literal ({bad!r}) — it must "
            "read a real dependency (key present, pipeline up) so the line "
            "she relays tracks reality"
        )
    # And the specific dependencies are actually referenced.
    assert "lily_vision_available" in src  # vision -> XAI key
    assert "exa_api_key" in src            # pictures_real_sourcing -> EXA key


def test_vision_flag_tracks_the_xai_key(monkeypatch):
    """vision availability must flip with the real dependency — the flag
    she reports is grounded in whether the key is actually present."""
    monkeypatch.setattr(lily_config, "xai_api_key", lambda: "")
    assert lily_vision.lily_vision_available() is False
    monkeypatch.setattr(lily_config, "xai_api_key", lambda: "xai-live-key")
    assert lily_vision.lily_vision_available() is True


def test_vision_flag_bound_to_the_live_check_in_the_entrypoint():
    """MUTATION GUARD (operator 2026-08-06): the runtime vision flag MUST be
    computed from lily_vision.lily_vision_available(), never a literal. If a
    future edit hardcodes it off (the 'manifest lies, she relays the lie'
    defect — vision works but she denies photo analysis), this goes red."""
    src = _availability_flags_source()
    assert '"vision": lily_vision.lily_vision_available()' in src, (
        "vision availability flag is no longer bound to the live key check "
        "— it must never be a hardcoded literal"
    )


def test_vision_off_line_is_scoped_not_a_blanket_picture_denial():
    """When vision is off (no XAI key), the line must name photo-LOOKING
    specifically and affirm that picture rounds / generated images still
    work — it must NEVER collapse into 'pictures are off tonight' (the
    2026-08-06 user-facing symptom). Generated pictures ride Gemini/Grok
    image-gen, which need no vision key."""
    flags = {
        entry["availability_key"]: True
        for entry in lily_capabilities.LILY_CAPABILITIES
        if entry.get("availability_key")
    }
    flags["vision"] = False  # only photo-analysis off; everything else on
    lines = lily_capabilities.lily_availability_lines(flags)
    vision_lines = [ln for ln in lines if ln.startswith("image_ingestion")]
    assert len(vision_lines) == 1
    line = vision_lines[0].lower()
    assert "picture rounds" in line and "still work" in line, line
    # No other capability is reported off in this scenario.
    assert lines == vision_lines


def test_availability_lines_never_overclaim_and_stay_honest():
    all_on = {
        entry["availability_key"]: True
        for entry in lily_capabilities.LILY_CAPABILITIES
        if entry.get("availability_key")
    }
    assert lily_capabilities.lily_availability_lines(all_on) == []
    # Unknown/absent deps -> OFF for every gated feature, never overclaim.
    lines = lily_capabilities.lily_availability_lines({})
    assert len(lines) == len(all_on)
    # Pictures caveat is PARTIAL: generated imagery is never gated.
    assert "generated pictures work regardless" in " ".join(lines)


# -- INVOKE the two regressed capabilities against their real contracts --------


def test_vision_capability_invocation_contract():
    """image_ingestion is manifest-on: exercise the real code path and
    assert its structured contract (never raises; honest status)."""
    import asyncio

    def _run(coro):
        return asyncio.new_event_loop().run_until_complete(coro)

    empty = _run(lily_vision.lily_describe_image(""))
    assert empty == {"status": "unavailable", "reason": "empty image_url"}
    bad = _run(lily_vision.lily_describe_image("ftp://x/y.png"))
    assert bad == {"status": "error", "reason": "invalid image_url scheme"}


def test_custom_category_capability_invocation_routes_the_topic():
    """custom_category is manifest-on: invoking the tool must route the
    requested subject into the generator seam (_category_for_round) —
    proving she builds the round instead of denying it."""
    import asyncio

    game = LilyGame.bare()
    game.sk = __import__("lily_scorekeeper").LilyScorekeeper("truth-fixture")
    game.sk.mode = "general"
    game.sk.question_number = 0
    game.sk.questions_per_round = 6
    game.rounds_total = 4
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game._category_override = {}
    game.next_question = None
    game.armed_question = None
    game._prefetch_task = None
    game._prefetch_stall_ticks = 0
    game.game_started = True
    game.game_over = False
    game._custom_round_registered = {}

    def _supply():
        # HOTFIX-006 N2: the tool builds before it answers and rolls the
        # override back when nothing registers, so "she builds the round
        # instead of denying it" is now proven THROUGH a real commit —
        # this models _prefetch_inner's commit tail.
        rnd = game._round_for_next_question()
        category = game._category_for_round(rnd)
        question = {
            "id": "q_built", "prompt": f"A {category} question?",
            "canonical_answer": "answer", "acceptable_answers": ["answer"],
            "category": category,
        }
        game.next_question = question
        game._register_custom_question(category, question)

    game.start_prefetch = _supply

    agent = LilyAgent.__new__(LilyAgent)
    agent._game = game
    coro = LilyAgent.lily_set_category.__wrapped__(agent, None, "Ancient Egypt")
    loop = asyncio.new_event_loop()
    try:
        msg = loop.run_until_complete(coro)
    finally:
        loop.close()
    assert game._category_for_round(1) == "Ancient Egypt"
    assert "can't" not in msg.lower() and "pre-loaded" not in msg.lower()
