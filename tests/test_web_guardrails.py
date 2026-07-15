"""Guardrail inspection tests (WO-LILY-OMNIBUS-002 sub-agent K):

HARD CONSTRAINT — no web tool on the vocal path, ever. Exa and Tavily are
bound to the reasoning node ONLY (question verification + current-events
sourcing + real-entity image sourcing, all at prefetch). Web results reach
Lily only as bank rows or state-block facts prepared by the reasoning node.

These tests enforce the constraint by inspection: the vocal module
(lily_agent) must contain no reference to the web/image stacks, the only
legal importers of lily_search are the reasoning-side modules, and the
import tripwire in lily_search must fire on a direct vocal import.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import lily_search

REPO = Path(__file__).resolve().parent.parent

# The reasoning-side modules — the ONLY code allowed to touch web tools.
LEGAL_SEARCH_IMPORTERS = {"lily_reasoning", "lily_imagegen"}


def _module_source(name: str) -> str:
    return (REPO / f"{name}.py").read_text(encoding="utf-8")


def _direct_imports(source: str) -> set:
    """Module names imported at any level of a source file (both
    `import x` and `from x import y`, including lazy in-function forms)."""
    names = set()
    for m in re.finditer(
        r"^\s*import\s+([\w\.]+)|^\s*from\s+([\w\.]+)\s+import",
        source, re.MULTILINE,
    ):
        names.add((m.group(1) or m.group(2)).split(".")[0])
    return names


# ---------------------------------------------------------------------------
# The vocal node never touches the web/image stacks
# ---------------------------------------------------------------------------

def test_vocal_module_never_references_web_tools():
    source = _module_source("lily_agent")
    assert "lily_search" not in source, (
        "GUARDRAIL: lily_agent (the vocal path) must never reference "
        "lily_search — web tools are reasoning-node-only"
    )
    assert "lily_imagegen" not in source, (
        "GUARDRAIL: lily_agent must reach the image stack only through "
        "lily_reasoning.prefetch_picture_question"
    )


def test_vocal_module_namespace_has_no_search_tools():
    import lily_agent  # noqa: F401 — imported for namespace inspection
    assert "lily_search" not in vars(sys.modules["lily_agent"])
    assert "lily_imagegen" not in vars(sys.modules["lily_agent"])


def test_only_reasoning_side_modules_import_lily_search():
    importers = set()
    for path in REPO.glob("lily_*.py"):
        name = path.stem
        if name == "lily_search":
            continue
        if "lily_search" in _direct_imports(path.read_text(encoding="utf-8")):
            importers.add(name)
    assert importers <= LEGAL_SEARCH_IMPORTERS, (
        f"GUARDRAIL: unexpected lily_search importers: "
        f"{importers - LEGAL_SEARCH_IMPORTERS}"
    )


def test_no_search_calls_outside_reasoning_side():
    # Belt over braces: no module outside the reasoning side may even
    # NAME a search function.
    for path in REPO.glob("lily_*.py"):
        name = path.stem
        if name == "lily_search" or name in LEGAL_SEARCH_IMPORTERS:
            continue
        source = path.read_text(encoding="utf-8")
        for symbol in ("lily_exa_search", "lily_tavily_search",
                       "lily_find_real_entity_image",
                       "lily_current_events_brief",
                       "lily_web_verification_context"):
            assert symbol not in source, (
                f"GUARDRAIL: {name}.py references {symbol} — web tools are "
                "reasoning-node-only"
            )


# ---------------------------------------------------------------------------
# The import tripwire (code-level enforcement)
# ---------------------------------------------------------------------------

def test_tripwire_fires_on_direct_vocal_import():
    with pytest.raises(RuntimeError, match="GUARDRAIL"):
        lily_search.lily_forbid_vocal_import(
            ["lily_search", "importlib._bootstrap", "lily_agent"]
        )


def test_tripwire_allows_the_reasoning_seam():
    # lily_agent -> lily_reasoning -> lily_search is the ONE legal path.
    lily_search.lily_forbid_vocal_import([
        "lily_search", "importlib._bootstrap", "lily_reasoning",
        "importlib._bootstrap", "lily_agent",
    ])


# ---------------------------------------------------------------------------
# The reasoning node is where web results become facts
# ---------------------------------------------------------------------------

def _reasoning_with_capture(raw: str):
    import lily_reasoning

    r = lily_reasoning.LilyReasoning.__new__(lily_reasoning.LilyReasoning)
    r._model = "stub-model"
    r._vocal_model = "stub-vocal"
    captured = {}

    async def _fake_generate(model, prompt, thinking_level, **kwargs):
        captured["prompt"] = prompt
        return raw

    r._generate = _fake_generate
    return r, captured


def test_verification_consumes_tavily_context_on_reasoning_node(monkeypatch):
    import asyncio

    import lily_reasoning

    monkeypatch.setenv("TAVILY_API_KEY", "k")

    async def fake_context(prompt, answer, **kw):
        return "Answer: The Bosporus separates Europe and Asia."

    monkeypatch.setattr(
        lily_reasoning.lily_search, "lily_web_verification_context",
        fake_context,
    )
    r, captured = _reasoning_with_capture(
        '{"verdict": "pass", "reason": "checks out"}'
    )
    ok, reason = asyncio.run(r.verify_question({
        "prompt": "Which strait?", "canonical_answer": "Bosporus",
    }))
    assert ok is True
    assert "WEB CONTEXT" in captured["prompt"]
    assert "Bosporus separates" in captured["prompt"]


def test_verification_without_key_stays_model_only(monkeypatch):
    import asyncio

    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    r, captured = _reasoning_with_capture(
        '{"verdict": "pass", "reason": "checks out"}'
    )
    ok, _ = asyncio.run(r.verify_question({
        "prompt": "Which strait?", "canonical_answer": "Bosporus",
    }))
    assert ok is True
    assert "WEB CONTEXT" not in captured["prompt"]


def test_current_events_brief_reaches_generation_prompt(monkeypatch):
    import asyncio

    import lily_reasoning

    monkeypatch.setenv("TAVILY_API_KEY", "k")

    async def fake_brief(topic, **kw):
        return "- Headline: a thing happened (https://n)"

    monkeypatch.setattr(
        lily_reasoning.lily_search, "lily_current_events_brief", fake_brief,
    )
    r, captured = _reasoning_with_capture(
        '{"id": "q_0001", "category": "current events", "difficulty_tier": 2,'
        ' "prompt": "What happened?", "canonical_answer": "a thing",'
        ' "acceptable_answers": ["a thing"], "reveal_color": ""}'
    )
    q = asyncio.run(r.generate_question("current events", 2, "general", []))
    assert q is not None
    assert "FRESH WEB FACTS" in captured["prompt"]
    # Evergreen categories never trigger the brief.
    captured.clear()
    q = asyncio.run(r.generate_question("wordplay", 2, "general", []))
    assert "FRESH WEB FACTS" not in captured["prompt"]


# ---------------------------------------------------------------------------
# Web keys are optional — never a boot requirement
# ---------------------------------------------------------------------------

def test_missing_web_keys_disable_not_crash(monkeypatch):
    import asyncio

    import lily_config
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    assert lily_config.exa_api_key() is None
    assert lily_config.tavily_api_key() is None
    assert "error" in asyncio.run(lily_search.lily_exa_search("q"))
    assert "error" in asyncio.run(lily_search.lily_tavily_search("q"))
