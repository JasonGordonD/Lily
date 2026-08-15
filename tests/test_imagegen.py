"""Tests for lily_imagegen (WO-LILY-OMNIBUS-002 sub-agent J): the ported
aspect-ratio clamp, the no-silent-crash generation wrapper (visible error
rows, cache-first), and the 'real or imagined' reference round."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_evaluation
import lily_images
import lily_imagegen


def run(coro):
    return asyncio.run(coro)


class _AttemptCapture:
    def __init__(self):
        self.rows = []

    async def record(self, supabase, **kw):
        self.rows.append(kw)


# ---------------------------------------------------------------------------
# Aspect-ratio clamp (donor algorithm, Gemini supported set)
# ---------------------------------------------------------------------------

def test_clamp_supported_values_pass_through():
    for ratio in ("1:1", "16:9", "9:16", "4:5", "21:9", "auto"):
        clamped, reason, _ = lily_imagegen.clamp_aspect_ratio(ratio)
        assert clamped == ratio
        assert reason == "already_supported"


def test_clamp_donor_crash_class_never_escapes_supported_set():
    # The donor's observed crash value was '4:5' (unsupported at xAI);
    # whatever the provider set, an off-list value must clamp, not crash.
    for requested in ("17:9", "4:5.5", "10:16", "7:5", "1000:999"):
        clamped, _, _ = lily_imagegen.clamp_aspect_ratio(requested)
        assert clamped in lily_imagegen.SUPPORTED_ASPECT_RATIOS


def test_clamp_orientation_preserving_nearest():
    clamped, reason, _ = lily_imagegen.clamp_aspect_ratio("17:9")  # 1.888
    assert clamped == "16:9"  # nearest landscape
    assert reason == "orientation_preserving_nearest"
    clamped, _, _ = lily_imagegen.clamp_aspect_ratio("9:17")
    assert clamped == "9:16"  # portrait stays portrait
    clamped, _, _ = lily_imagegen.clamp_aspect_ratio("100:100")
    assert clamped == "1:1"


def test_clamp_parse_fail_falls_back_to_auto():
    for junk in ("", "square", "banana", "4x5", ":", "4:0"):
        clamped, reason, _ = lily_imagegen.clamp_aspect_ratio(junk)
        assert clamped == "auto", junk
        assert reason in ("empty_input", "parse_fail_fallback_to_auto")


def test_clamp_extreme_ratio_falls_back_to_auto():
    clamped, reason, delta = lily_imagegen.clamp_aspect_ratio("50:9")
    assert clamped == "auto"
    assert reason == "orientation_fallback_to_auto"
    assert delta > 0.30


def test_clamp_and_log_returns_supported_value():
    assert lily_imagegen.clamp_and_log("17:9") == "16:9"
    assert lily_imagegen.clamp_and_log("16:9") == "16:9"


# ---------------------------------------------------------------------------
# No-silent-crash generation wrapper
# ---------------------------------------------------------------------------

def test_generation_failure_writes_visible_error_row(monkeypatch):
    async def boom(prompt, **kw):
        raise RuntimeError("provider exploded")

    capture = _AttemptCapture()
    monkeypatch.setattr(lily_imagegen, "lily_generate_image_bytes", boom)
    monkeypatch.setattr(
        lily_imagegen.lily_images, "lily_record_image_attempt", capture.record
    )

    async def no_cache(supabase, qid):
        return None

    monkeypatch.setattr(
        lily_imagegen.lily_images, "lily_cached_bank_image", no_cache
    )
    url = run(lily_imagegen.lily_generate_question_image(
        object(), session_id="room-1", question_id="q_0001",
        prompt="an invented lighthouse",
    ))
    assert url is None  # text-only fallback, never raises
    row = capture.rows[0]
    assert row["status"] == lily_images.ATTEMPT_ERROR
    assert "provider exploded" in row["failure_reason"]
    assert row["source"] == "generated"


def test_generation_refusal_is_a_rejected_row(monkeypatch):
    async def refuse(prompt, **kw):
        raise RuntimeError("no image in response: cannot depict that")

    capture = _AttemptCapture()
    monkeypatch.setattr(lily_imagegen, "lily_generate_image_bytes", refuse)
    monkeypatch.setattr(
        lily_imagegen.lily_images, "lily_record_image_attempt", capture.record
    )

    async def no_cache(supabase, qid):
        return None

    monkeypatch.setattr(
        lily_imagegen.lily_images, "lily_cached_bank_image", no_cache
    )
    url = run(lily_imagegen.lily_generate_question_image(
        object(), session_id="s", question_id="q", prompt="p",
    ))
    assert url is None
    assert capture.rows[0]["status"] == lily_images.ATTEMPT_REJECTED
    assert "cannot depict that" in capture.rows[0]["failure_reason"]


def test_generation_success_records_row_and_writes_back(monkeypatch):
    async def fake_gen(prompt, **kw):
        return b"png", "image/png", "gemini-2.5-flash-image"

    async def fake_upload(supabase, data, **kw):
        assert kw["source"] == "generated"
        return "https://cdn.example/lily-images/generated/abc.png"

    async def no_cache(supabase, qid):
        return None

    saved = {}

    async def fake_save(supabase, qid, **kw):
        saved.update(kw, question_id=qid)
        return True

    capture = _AttemptCapture()
    monkeypatch.setattr(lily_imagegen, "lily_generate_image_bytes", fake_gen)
    monkeypatch.setattr(
        lily_imagegen.lily_images, "lily_upload_image_bytes", fake_upload
    )
    monkeypatch.setattr(
        lily_imagegen.lily_images, "lily_cached_bank_image", no_cache
    )
    monkeypatch.setattr(
        lily_imagegen.lily_images, "lily_save_bank_image", fake_save
    )
    monkeypatch.setattr(
        lily_imagegen.lily_images, "lily_record_image_attempt", capture.record
    )
    url = run(lily_imagegen.lily_generate_question_image(
        object(), session_id="s", question_id="kb_5", prompt="invented town",
    ))
    assert url == "https://cdn.example/lily-images/generated/abc.png"
    assert capture.rows[0]["status"] == lily_images.ATTEMPT_SUCCESS
    assert saved["image_source"] == "generated"
    assert "prompt head" in saved["image_license_note"]


def test_cache_first_short_circuits_generation(monkeypatch):
    async def cached(supabase, qid):
        return {"image_url": "https://cdn.example/cached.png",
                "image_source": "generated", "image_license_note": None}

    async def must_not_run(prompt, **kw):
        raise AssertionError("generator ran despite a cache hit")

    monkeypatch.setattr(
        lily_imagegen.lily_images, "lily_cached_bank_image", cached
    )
    monkeypatch.setattr(lily_imagegen, "lily_generate_image_bytes", must_not_run)
    url = run(lily_imagegen.lily_generate_question_image(
        object(), session_id="s", question_id="kb_5", prompt="anything",
    ))
    assert url == "https://cdn.example/cached.png"


# ---------------------------------------------------------------------------
# Reference round — real or imagined
# ---------------------------------------------------------------------------

def test_real_or_imagined_even_index_is_real_web_sourced(monkeypatch):
    async def fake_find(entity, **kw):
        return {"image_url": "https://upload.wikimedia.org/e.jpg",
                "page_url": "https://en.wikipedia.org/wiki/E", "title": "E"}

    async def fake_fetch_bytes(url, **kw):
        return b"imgbytes", "image/jpeg"

    async def fake_upload(supabase, data, **kw):
        assert kw["source"] == "web"
        return "https://cdn.example/lily-images/web/abc.jpg"

    capture = _AttemptCapture()
    monkeypatch.setattr(
        lily_imagegen.lily_search, "lily_find_real_entity_image", fake_find
    )
    monkeypatch.setattr(
        lily_imagegen.lily_images, "lily_fetch_image_bytes", fake_fetch_bytes
    )
    monkeypatch.setattr(
        lily_imagegen.lily_images, "lily_upload_image_bytes", fake_upload
    )
    monkeypatch.setattr(
        lily_imagegen.lily_images, "lily_record_image_attempt", capture.record
    )
    q = run(lily_imagegen.lily_build_real_or_imagined_question(
        object(), index=0, session_id="room-1"
    ))
    assert q["canonical_answer"] == "real"
    assert q["image_source"] == "web"
    assert q["category"] == "real or imagined"
    assert "Exa" in q["image_license_note"]


def test_real_or_imagined_odd_index_is_generated(monkeypatch):
    async def fake_gen_q_image(supabase, **kw):
        assert kw["prompt"] in lily_imagegen.IMAGINED_PROMPTS
        return "https://cdn.example/lily-images/generated/fake.png"

    monkeypatch.setattr(
        lily_imagegen, "lily_generate_question_image", fake_gen_q_image
    )
    q = run(lily_imagegen.lily_build_real_or_imagined_question(
        object(), index=1, session_id="room-1"
    ))
    assert q["canonical_answer"] == "imagined"
    assert q["image_source"] == "generated"
    assert q["image_url"].endswith("fake.png")


def test_generation_routes_to_grok_in_adult_mode(monkeypatch):
    # WO-LILY-ADULT-PICTURES-001: adult-deck generation goes to xAI Grok
    # Imagine; the Gemini path must never run for adult. Style chokepoint
    # always wraps the prompt before the wire.
    calls = {}

    async def fake_xai(prompt, *, model=None):
        calls["xai"] = {"prompt": prompt, "model": model}
        return b"png", "image/png", lily_imagegen.lily_config.adult_imagegen_model()

    monkeypatch.setattr(lily_imagegen, "_generate_image_bytes_xai", fake_xai)
    data, mime, model = run(
        lily_imagegen.lily_generate_image_bytes("invented place")
    )
    assert "invented place" in calls["xai"]["prompt"]
    assert "comic-book" in calls["xai"]["prompt"]
    assert "SUGGESTIVE" in calls["xai"]["prompt"]
    assert model == lily_imagegen.lily_config.adult_imagegen_model()
    assert model == "grok-imagine-image-2.0"


def test_adult_style_intensity_and_content_brief():
    sug = lily_imagegen.lily_adult_style("scene", intensity="suggestive")
    exp = lily_imagegen.lily_adult_style("scene", intensity="explicit")
    assert "comic-book" in sug and "comic-book" in exp
    assert "Permissive wear" in sug
    assert "kinky positions" in sug.lower() or "Kinky" in sug or "kinky" in sug
    assert "toys" in sug.lower()
    assert "Captions" in sug or "captions" in sug
    assert "SUGGESTIVE" in sug and "EXPLICIT" in exp
    assert "hardcore" in sug.lower() or "stop short" in sug.lower()
    assert lily_imagegen.lily_normalize_adult_image_intensity("EXPLICIT") == "explicit"
    assert lily_imagegen.lily_normalize_adult_image_intensity("nope") == "suggestive"


def test_real_or_imagined_generated_names_grok(monkeypatch):
    # Unified adult deck: the GENERATED branch routes to the Grok adult
    # image model and labels the license note with it (no mode threading).
    seen = {}

    async def fake_gen_q_image(supabase, **kw):
        seen["called"] = True
        return "https://cdn.example/lily-images/generated/adult.png"

    monkeypatch.setattr(
        lily_imagegen, "lily_generate_question_image", fake_gen_q_image
    )
    q = run(lily_imagegen.lily_build_real_or_imagined_question(
        object(), index=1, session_id="room-1"
    ))
    assert seen.get("called") is True
    assert q["image_source"] == "generated"
    assert lily_imagegen.lily_config.adult_imagegen_model() in q[
        "image_license_note"
    ]
    assert "grok-imagine-image-2.0" in q["image_license_note"]


def test_real_or_imagined_failure_is_text_only_fallback(monkeypatch):
    async def fake_gen_q_image(supabase, **kw):
        return None  # generation failed; error row already written inside

    monkeypatch.setattr(
        lily_imagegen, "lily_generate_question_image", fake_gen_q_image
    )
    assert run(lily_imagegen.lily_build_real_or_imagined_question(
        object(), index=1, session_id="room-1"
    )) is None


def test_real_or_imagined_never_generates_for_real_entities(monkeypatch):
    # The REAL branch must never touch the generator, even when sourcing
    # fails — real entities are web-or-nothing.
    async def fake_find(entity, **kw):
        return None

    async def must_not_run(*a, **kw):
        raise AssertionError("generator ran for a real entity")

    monkeypatch.setattr(
        lily_imagegen.lily_search, "lily_find_real_entity_image", fake_find
    )
    monkeypatch.setattr(lily_imagegen, "lily_generate_image_bytes", must_not_run)
    monkeypatch.setattr(
        lily_imagegen, "lily_generate_question_image", must_not_run
    )
    assert run(lily_imagegen.lily_build_real_or_imagined_question(
        object(), index=0, session_id="room-1"
    )) is None


def test_real_or_imagined_builder_never_raises(monkeypatch):
    async def boom(entity, **kw):
        raise RuntimeError("network down")

    capture = _AttemptCapture()
    monkeypatch.setattr(
        lily_imagegen.lily_search, "lily_find_real_entity_image", boom
    )
    monkeypatch.setattr(
        lily_imagegen.lily_images, "lily_record_image_attempt", capture.record
    )
    assert run(lily_imagegen.lily_build_real_or_imagined_question(
        object(), index=0, session_id="room-1"
    )) is None
    assert capture.rows[-1]["status"] == lily_images.ATTEMPT_ERROR


def test_real_or_imagined_adjudication_variants():
    # Table answers like "fake!", "it's AI", "that's real" must land via
    # the existing tier-1 matcher against acceptable_answers.
    real_accept = lily_imagegen._REAL_ACCEPTABLE
    imagined_accept = lily_imagegen._IMAGINED_ACCEPTABLE
    for spoken in ("real", "it's real", "that's genuine"):
        verdict = lily_evaluation.lily_tier1_evaluate(spoken, real_accept)
        assert verdict["verdict"] == "correct", spoken
    for spoken in ("fake", "imagined", "it's AI", "that's made up"):
        verdict = lily_evaluation.lily_tier1_evaluate(spoken, imagined_accept)
        assert verdict["verdict"] == "correct", spoken
