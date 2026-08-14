"""M1a: every configurable Gemini text filter is explicitly BLOCK_NONE."""

from __future__ import annotations

import inspect

from google.genai import types as gt

import lily_gemini_safety
import lily_imagegen
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


# test_reasoning_legacy_multimodal_helper_uses_shared_policy DELETED
# (WO-PRMPT-LILY-GEMINI-EXCISION-001): it asserted lily_reasoning._SAFETY_SETTINGS,
# which was removed with the reasoning node's dead google-genai lane.


def test_every_remaining_gemini_lane_imports_shared_policy():
    # lily_imagegen dropped from this list in WO-PRMPT-LILY-REFACTOR-001 (its
    # Gemini image path was deleted); lily_reasoning dropped in
    # WO-PRMPT-LILY-GEMINI-EXCISION-001 (its google-genai lane was excised —
    # it no longer imports lily_gemini_safety). Only lily_search remains a
    # Gemini lane.
    for module in (
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
