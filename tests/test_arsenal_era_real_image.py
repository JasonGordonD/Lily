"""Period/era formats source a genuinely-dated REAL image instead of
generating one.

Residual ceiling after the correspondence fix (b81b4c2): era_or_origin ("what
decade is this?") is correctly gate-rejected when the image is AI-generated —
a render carries only an IMPRESSION of a period ("pin-up styling reads 1940s
not 1890s", "impossible to identify a specific decade"). A genuinely-dated
archival photograph carries authentic period cues, so era_or_origin now
SOURCES its image via the existing Exa real-image path and banks it with
is_real_image=True.

These check the wiring, not a live provider or a live Exa call:
  - era_or_origin + a wired source_real that returns a sourced image banks a
    web/real entry (is_real_image True, image_source 'web', curated answer,
    provenance in generation_prompt), and NEVER calls imagegen;
  - era_or_origin + source_real returning None falls back to the generated
    path unchanged (imagegen IS called, is_real_image False) — never worse;
  - era_or_origin with no source_real wired uses the generated path (the
    pre-fix behaviour);
  - a sourced real image that FAILS the gate is a counted classifier
    rejection, not banked, and does NOT fall back to generation;
  - a non-period format (identify) never calls source_real even when wired.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_arsenal_gen


def _run(coro):
    return asyncio.run(coro)


REAL_BYTES = b"\xff\xd8\xff\x00archival-photo"
GEN_BYTES = b"\xff\xd8\xff\x00generated-render"


def _era_slot():
    return {
        "format": "era_or_origin",
        "subject_area": "history",
        "difficulty_tier": 2,
        "binding_direction": "question_first",
        "entry_index": 0,
    }


def _sourced():
    return {
        "image_bytes": REAL_BYTES,
        "content_type": "image/jpeg",
        "question_text": "This is a real photograph — what decade is it from?",
        "canonical_answer": "the 1880s",
        "acceptable_answers": ["1880s", "victorian"],
        "reveal_color": "The 1880s — the penny-farthing's one brief decade.",
        "provenance": "real image via Exa: page=https://loc.gov/x image=https://loc.gov/x.jpg",
    }


async def _upload_ok(data, mime, partition):
    return f"arsenal/{partition}/real.jpg"


async def _classify_ok(image_bytes, content_type, claim, brief):
    return True, "ok"


async def _classify_reject(image_bytes, content_type, claim, brief):
    return False, "period cue did not land"


def _make_imagegen(calls):
    async def imagegen(prompt, partition, intensity):
        calls.append(prompt)
        return GEN_BYTES, "image/jpeg", "gemini-image-test"
    return imagegen


async def _author(partition, plan, image_description):
    return {
        "question_text": "What decade is this kitchen from?",
        "canonical_answer": "the 1970s",
        "acceptable_answers": ["1970s"],
        "reveal_color": "",
    }


def test_era_sources_real_image_and_banks_web_entry():
    gen_calls = []
    source_calls = []

    async def source_real(partition, plan):
        source_calls.append((partition, plan.get("entry_index")))
        return _sourced()

    result = _run(lily_arsenal_gen.lily_generate_entry(
        partition="general",
        plan=_era_slot(),
        author=_author,
        imagegen=_make_imagegen(gen_calls),
        upload=_upload_ok,
        classify=_classify_ok,
        source_real=source_real,
    ))

    assert result["outcome"] == lily_arsenal_gen.OUTCOME_CREATED
    entry = result["entry"]
    assert entry["is_real_image"] is True
    assert entry["image_source"] == "web"
    assert entry["canonical_answer"] == "the 1880s"
    assert "1880s" in entry["acceptable_answers"]
    assert entry["image_storage_path"] == "arsenal/general/real.jpg"
    assert "Exa" in entry["generation_prompt"]  # provenance recorded
    assert entry["format"] == "era_or_origin"
    # The whole point: no generation spend on the real path.
    assert gen_calls == [], "imagegen must NOT be called on the real path"
    assert result["cost_usd"] == 0.0
    assert source_calls == [("general", 0)]


def test_era_falls_back_to_generated_when_no_real_image_sourced():
    gen_calls = []

    async def source_real(partition, plan):
        return None  # nothing passed the conservative filter

    result = _run(lily_arsenal_gen.lily_generate_entry(
        partition="general",
        plan=_era_slot(),
        author=_author,
        imagegen=_make_imagegen(gen_calls),
        upload=_upload_ok,
        classify=_classify_ok,
        source_real=source_real,
    ))

    assert result["outcome"] == lily_arsenal_gen.OUTCOME_CREATED
    assert result["entry"]["is_real_image"] is False
    assert result["entry"]["image_source"] == "generated"
    assert gen_calls, "generated path must run when no real image is sourced"


def test_era_uses_generated_path_when_source_real_unwired():
    gen_calls = []
    result = _run(lily_arsenal_gen.lily_generate_entry(
        partition="general",
        plan=_era_slot(),
        author=_author,
        imagegen=_make_imagegen(gen_calls),
        upload=_upload_ok,
        classify=_classify_ok,
        source_real=None,
    ))
    assert result["outcome"] == lily_arsenal_gen.OUTCOME_CREATED
    assert result["entry"]["is_real_image"] is False
    assert gen_calls, "no source_real wired -> the pre-fix generated path"


def test_sourced_real_image_gate_reject_is_counted_not_regenerated():
    gen_calls = []

    async def source_real(partition, plan):
        return _sourced()

    result = _run(lily_arsenal_gen.lily_generate_entry(
        partition="general",
        plan=_era_slot(),
        author=_author,
        imagegen=_make_imagegen(gen_calls),
        upload=_upload_ok,
        classify=_classify_reject,
        source_real=source_real,
    ))

    assert result["outcome"] == lily_arsenal_gen.OUTCOME_CLASSIFIER
    assert result["entry"] is None
    # A rejected real image must NOT then generate one — that is exactly the
    # image the gate was refusing.
    assert gen_calls == [], "gate-rejected real image must not fall back to generation"


def test_non_period_format_never_calls_source_real():
    source_calls = []

    async def source_real(partition, plan):
        source_calls.append(plan.get("format"))
        return _sourced()

    slot = {
        "format": "identify",
        "subject_area": "tools",
        "difficulty_tier": 2,
        "binding_direction": "image_first",
        "entry_index": 0,
    }

    async def describe(image_bytes, content_type):
        return "a brass sextant on a chart table"

    result = _run(lily_arsenal_gen.lily_generate_entry(
        partition="general",
        plan=slot,
        author=_author,
        imagegen=_make_imagegen([]),
        upload=_upload_ok,
        classify=_classify_ok,
        describe=describe,
        source_real=source_real,
    ))

    assert result["outcome"] == lily_arsenal_gen.OUTCOME_CREATED
    assert result["entry"]["is_real_image"] is False
    assert source_calls == [], "identify must never touch the real-image path"


import lily_search


def test_source_period_entry_general_register_and_provenance():
    async def fake_find(query, **kw):
        return {
            "image_url": "https://loc.gov/photo.jpg",
            "page_url": "https://loc.gov/item/x",
            "title": query,
        }

    async def fake_fetch(url):
        return REAL_BYTES, "image/jpeg"

    orig = lily_search.lily_find_real_entity_image
    lily_search.lily_find_real_entity_image = fake_find
    try:
        out = _run(lily_search.lily_source_period_entry(
            "general", {"entry_index": 0}, fetch=fake_fetch,
        ))
    finally:
        lily_search.lily_find_real_entity_image = orig

    assert out is not None
    assert out["image_bytes"] == REAL_BYTES
    assert out["canonical_answer"].startswith("the ")
    assert out["acceptable_answers"], "curated decade manglings must be present"
    assert "Exa" in out["provenance"]
    assert "loc.gov" in out["provenance"]


def test_source_period_entry_suggestive_picks_suggestive_subject():
    captured = {}

    async def fake_find(query, **kw):
        captured["query"] = query
        return {
            "image_url": "https://commons.wikimedia.org/x.jpg",
            "page_url": "https://commons.wikimedia.org/item",
            "title": query,
        }

    async def fake_fetch(url):
        return REAL_BYTES, "image/jpeg"

    suggestive_queries = {
        s["query"] for s in lily_search.lily_period_subjects_for_register("suggestive")
    }
    orig = lily_search.lily_find_real_entity_image
    lily_search.lily_find_real_entity_image = fake_find
    try:
        out = _run(lily_search.lily_source_period_entry(
            "adult_suggestive", {"entry_index": 1}, fetch=fake_fetch,
        ))
    finally:
        lily_search.lily_find_real_entity_image = orig

    assert out is not None
    assert captured["query"] in suggestive_queries


def test_source_period_entry_none_on_no_candidate():
    async def fake_find(query, **kw):
        return None

    orig = lily_search.lily_find_real_entity_image
    lily_search.lily_find_real_entity_image = fake_find
    try:
        out = _run(lily_search.lily_source_period_entry(
            "general", {"entry_index": 0},
        ))
    finally:
        lily_search.lily_find_real_entity_image = orig

    assert out is None, "no candidate -> None -> caller falls back to generation"


if __name__ == "__main__":  # pragma: no cover
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
