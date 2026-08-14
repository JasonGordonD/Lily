"""M1a: every configurable Gemini text filter is explicitly BLOCK_NONE."""

from __future__ import annotations

import inspect

from google.genai import types as gt

import lily_gemini_safety
import lily_imagegen
import lily_reasoning
import lily_search


EXPECTED = {
    gt.HarmCategory.HARM_CATEGORY_HARASSMENT,
    gt.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
    gt.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
    gt.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
}


def test_shared_sdk_policy_disables_all_configurable_categories():
    settings = lily_gemini_safety.lily_gemini_safety_settings()
    assert {item.category for item in settings} == EXPECTED
    assert all(
        item.threshold == gt.HarmBlockThreshold.BLOCK_NONE
        for item in settings
    )


def test_livekit_plugin_policy_matches_sdk_policy():
    settings = lily_gemini_safety.lily_gemini_safety_dicts()
    assert {item["category"] for item in settings} == {
        category.name for category in EXPECTED
    }
    assert all(item["threshold"] == "BLOCK_NONE" for item in settings)


def test_reasoning_legacy_multimodal_helper_uses_shared_policy():
    assert {
        item.category for item in lily_reasoning._SAFETY_SETTINGS
    } == EXPECTED


def test_every_remaining_gemini_lane_imports_shared_policy():
    # lily_imagegen dropped from this list in WO-PRMPT-LILY-REFACTOR-001:
    # its Gemini image path was deleted (all imagegen rides Grok Imagine),
    # so it is no longer a Gemini lane.
    for module in (
        lily_reasoning,
        lily_search,
    ):
        source = inspect.getsource(module)
        assert "lily_gemini_safety" in source, module.__name__


def test_image_and_grounding_configs_apply_safety_settings():
    # Image generation no longer routes through Gemini (the content-mode gate
    # was removed; all image gen goes to Grok Imagine), so only the grounded
    # search lane still applies Gemini safety settings.
    search_source = inspect.getsource(lily_search._lily_grounded_generate)
    assert "safety_settings" in search_source
