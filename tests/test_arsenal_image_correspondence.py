"""Image/question correspondence fix — the image-first author must write the
stem from a VISION caption of what the model ACTUALLY rendered, not from the
intended generation prompt.

Root cause of the 9/10 gate-refusal plateau: grok-imagine follows prompts
loosely (a "codpiece" prompt draws a corset; "three people" draws five), so a
stem authored from the prompt disagreed with the picture and the
correspondence gate correctly rejected it. The fix authors from the rendered
image.

These check the wiring, not a live provider:
  - image-first: describe() is called once with the generated image_bytes and
    the author receives its caption (never the raw prompt);
  - image-first with no describer / a failing describer: author falls back to
    the prompt (never worse than before);
  - question-first: describe() is NEVER called and the author receives None.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_arsenal_formats
import lily_arsenal_gen


def _run(coro):
    return asyncio.run(coro)


IMAGE_BYTES = b"\xff\xd8\xff\x00rendered-pixels"
CAPTION = "A woman in a codpiece and doublet holds a lute in a stone hall."


async def _imagegen(prompt, partition, intensity):
    return IMAGE_BYTES, "image/jpeg", "grok-imagine-test"


async def _upload(data, mime, partition):
    return "arsenal/general/x.jpg"


async def _classify(image_bytes, content_type, claim, brief):
    return True, "ok"


def _slot(binding):
    return {
        "format": "identify",
        "subject_area": "history",
        "difficulty_tier": 2,
        "binding_direction": binding,
    }


def test_image_first_author_receives_vision_caption_not_prompt():
    seen = {}

    async def author(partition, plan, image_description):
        seen["desc"] = image_description
        return {"question_text": "Q?", "canonical_answer": "codpiece"}

    async def describe(image_bytes, content_type):
        seen["describe_bytes"] = image_bytes
        seen["describe_mime"] = content_type
        return CAPTION

    result = _run(
        lily_arsenal_gen.lily_generate_entry(
            partition="general",
            plan=_slot(lily_arsenal_formats.BINDING_IMAGE_FIRST),
            author=author,
            imagegen=_imagegen,
            upload=_upload,
            classify=_classify,
            describe=describe,
        )
    )

    assert result["outcome"] == lily_arsenal_gen.OUTCOME_CREATED
    # describe() saw the ACTUAL rendered bytes...
    assert seen["describe_bytes"] == IMAGE_BYTES
    assert seen["describe_mime"] == "image/jpeg"
    # ...and the author was handed that caption, NOT the generation prompt.
    assert seen["desc"] == CAPTION
    # generation_prompt still records what was sent to the image model.
    assert result["entry"]["generation_prompt"] != CAPTION


def test_image_first_falls_back_to_prompt_when_describe_absent():
    seen = {}

    async def author(partition, plan, image_description):
        seen["desc"] = image_description
        return {"question_text": "Q?", "canonical_answer": "a"}

    result = _run(
        lily_arsenal_gen.lily_generate_entry(
            partition="general",
            plan=_slot(lily_arsenal_formats.BINDING_IMAGE_FIRST),
            author=author,
            imagegen=_imagegen,
            upload=_upload,
            classify=_classify,
            describe=None,
        )
    )
    assert result["outcome"] == lily_arsenal_gen.OUTCOME_CREATED
    # No describer wired -> author gets the generation prompt (prior behaviour).
    assert seen["desc"] == result["entry"]["generation_prompt"]
    assert seen["desc"]  # non-empty prompt string


def test_image_first_falls_back_to_prompt_when_describe_raises():
    seen = {}

    async def author(partition, plan, image_description):
        seen["desc"] = image_description
        return {"question_text": "Q?", "canonical_answer": "a"}

    async def describe(image_bytes, content_type):
        raise RuntimeError("vision down")

    result = _run(
        lily_arsenal_gen.lily_generate_entry(
            partition="general",
            plan=_slot(lily_arsenal_formats.BINDING_IMAGE_FIRST),
            author=author,
            imagegen=_imagegen,
            upload=_upload,
            classify=_classify,
            describe=describe,
        )
    )
    assert result["outcome"] == lily_arsenal_gen.OUTCOME_CREATED
    assert seen["desc"] == result["entry"]["generation_prompt"]


def test_question_first_path_untouched_describe_never_called():
    seen = {"describe_called": False}

    async def author(partition, plan, image_description):
        seen["desc"] = image_description
        return {"question_text": "Q?", "canonical_answer": "a"}

    async def describe(image_bytes, content_type):
        seen["describe_called"] = True
        return CAPTION

    result = _run(
        lily_arsenal_gen.lily_generate_entry(
            partition="general",
            plan=_slot(lily_arsenal_formats.BINDING_QUESTION_FIRST),
            author=author,
            imagegen=_imagegen,
            upload=_upload,
            classify=_classify,
            describe=describe,
        )
    )
    assert result["outcome"] == lily_arsenal_gen.OUTCOME_CREATED
    # Question-first authors BEFORE any image exists -> image_description=None,
    # and the describer is never invoked.
    assert seen["desc"] is None
    assert seen["describe_called"] is False


if __name__ == "__main__":
    test_image_first_author_receives_vision_caption_not_prompt()
    test_image_first_falls_back_to_prompt_when_describe_absent()
    test_image_first_falls_back_to_prompt_when_describe_raises()
    test_question_first_path_untouched_describe_never_called()
    print("ALL PASS")
