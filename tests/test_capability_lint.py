"""WO-LILY-CAPABILITY-LINT-001 — bidirectional tool↔manifest lint.

The manifest stays true only if it can't drift. Runs on every merge:

  Direction 1: every REGISTERED function tool either maps to a
    `lily_capabilities` entry (via the entry's `tools` list) or is
    declared in `LILY_INTERNAL_TOOLS` — invisible by declaration, not
    by omission.
  Direction 2: every manifest entry's `code_ref` resolves to real code
    (module, module:attr, or module:Class.attr) — no orphaned claims.

Out of scope by WO: runtime reflection and architecture self-description
— the manifest remains the only self-knowledge surface.
"""

import importlib
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_capabilities
import lily_vision
import lily_voice_switch
from lily_agent import LilyAgent

from livekit.agents.llm import tool_context

# Modules whose module-level function tools are passed to LilyAgent via
# tools=[...] — extend when a new tool module is registered.
TOOL_MODULES = (lily_voice_switch, lily_vision)


def _registered_tool_names() -> set:
    """Every function tool the live agent registers: @function_tool
    methods on LilyAgent plus the module-level tools passed via
    `tools=[...]` at construction (lily_voice_switch)."""
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


def _manifest_mapped_tools() -> set:
    mapped = set()
    for entry in lily_capabilities.LILY_CAPABILITIES:
        mapped.update(entry.get("tools") or [])
    return mapped


def _lint_tools(registered, mapped, internal) -> list:
    """Direction 1, pure — returns the offending tool names."""
    return sorted(registered - mapped - internal)


def _resolve_code_ref(ref: str) -> bool:
    """Direction 2 resolver: 'module', 'module:attr', 'module:Class.attr'."""
    if not ref:
        return False
    module_name, _, attr_path = ref.partition(":")
    try:
        obj = importlib.import_module(module_name)
    except ImportError:
        return False
    if not attr_path:
        return True
    for part in attr_path.split("."):
        obj = getattr(obj, part, None)
        if obj is None:
            return False
    return True


def test_every_registered_tool_is_mapped_or_declared_internal():
    offenders = _lint_tools(
        _registered_tool_names(),
        _manifest_mapped_tools(),
        set(lily_capabilities.LILY_INTERNAL_TOOLS),
    )
    assert not offenders, (
        f"tools outside Lily's self-knowledge: {offenders}. Two legal "
        "fixes: (1) player-facing → add/extend a lily_capabilities entry "
        "and list the tool in its `tools`; (2) plumbing → declare it in "
        "lily_capabilities.LILY_INTERNAL_TOOLS with a comment."
    )


def test_every_manifest_entry_maps_to_real_code():
    orphans = [
        entry["key"]
        for entry in lily_capabilities.LILY_CAPABILITIES
        if not _resolve_code_ref(entry.get("code_ref", ""))
    ]
    assert not orphans, (
        f"manifest entries with no living code behind them: {orphans}. "
        "Two legal fixes: (1) the feature exists → point `code_ref` at "
        "its module or symbol; (2) the feature is gone → delete the "
        "entry (and let the version history carry the memory)."
    )


def test_internal_tools_are_not_also_mapped():
    # A tool can't be both invisible and a capability — one declaration.
    both = sorted(
        set(lily_capabilities.LILY_INTERNAL_TOOLS) & _manifest_mapped_tools()
    )
    assert not both, f"tools declared BOTH internal and capability: {both}"


# -- the WO's verify criteria, exercised synthetically -------------------------


def test_lint_names_an_unflagged_dummy_tool():
    offenders = _lint_tools(
        _registered_tool_names() | {"lily_dummy_new_tool"},
        _manifest_mapped_tools(),
        set(lily_capabilities.LILY_INTERNAL_TOOLS),
    )
    assert offenders == ["lily_dummy_new_tool"]


def test_lint_clears_the_dummy_once_flagged_internal():
    offenders = _lint_tools(
        _registered_tool_names() | {"lily_dummy_new_tool"},
        _manifest_mapped_tools(),
        set(lily_capabilities.LILY_INTERNAL_TOOLS) | {"lily_dummy_new_tool"},
    )
    assert offenders == []


def test_lint_names_an_orphan_manifest_entry():
    assert _resolve_code_ref("lily_module_that_never_existed") is False
    assert _resolve_code_ref("lily_agent:LilyGame.no_such_method") is False
    assert _resolve_code_ref("lily_voice_switch") is True
    assert _resolve_code_ref("lily_agent:LilyGame.skip_question") is True
