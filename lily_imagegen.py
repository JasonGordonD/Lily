"""
lily_imagegen.py — LILY image generation for INVENTED content only
(WO-LILY-OMNIBUS-002 sub-agent J). Native lift of the JRVS image stack
(maya_jrvs donor) onto Lily's Gemini infra:

  - BACKGROUND-TASK PATTERN (donor: _background_tool_exec /
    _persist_and_deliver_generated_image): generation never runs inside a
    live turn — it happens at PREFETCH TIME only, inside the reasoning
    node's background prefetch task, and its result reaches the vocal node
    only as a finished question payload.

  - ASPECT-RATIO CLAMP (donor: mjrvs_aspect_ratio_clamp,
    WO-FLEET-ASPECT-RATIO-CLAMP-AND-VISIBLE-ERRORS-001): an off-list
    aspect_ratio crashed the donor's background task AFTER a successful
    render, discarding the image with no visible error. The clamp maps any
    requested ratio to the orientation-preserving nearest supported value
    BEFORE it hits the provider wire. The supported set here is the Gemini
    image set (the donor's was xAI's); the algorithm, the auto fallback and
    the ASPECT_CLAMP log line are ported unchanged.

  - NO-SILENT-CRASH / VISIBLE ERROR ROWS (donor: media_gen_attempts — the
    stack where the rule originated): EVERY generation attempt — success
    OR failure — writes one lily_image_attempts row (writer in
    lily_images); rejection/error rows carry the actual provider message
    in failure_reason. A failed image never disappears silently.

HARD RULES:
  - image_source='generated' ONLY — this stack is for INVENTED content.
    Real entities are NEVER generated (they go through lily_search's Exa
    sourcing, sub-agent I) — a plausible-but-wrong Eiffel Tower is a lie
    on the screen.
  - Prefetch-time only. Nothing in this module runs on the vocal path.

REFERENCE ROUND — "real or imagined" (round 2 in pictures mode): a
generated plausible-fake photo (this module) alternates with a real photo
(sub-agent I's Exa sourcing); the table guesses which; adjudication
accepts real/fake/imagined variants. Chosen over "emoji story" because
the donor is a PHOTOREALISTIC single-image pipeline (photography prompts,
aspect clamp, moderation-visible failures) — exactly the plausible-fake
generator this round needs, and it composes with I's real-photo sourcing;
an emoji round would not exercise the donor stack at all.
"""

import asyncio
import logging
from typing import Final, Optional

from google import genai as google_genai
from google.genai import types as genai_types

import lily_config
import lily_images
import lily_search

logger = logging.getLogger("lily_imagegen")

# ---------------------------------------------------------------------------
# Aspect-ratio clamp (donor algorithm; Gemini image supported set).
# "auto" is first-class: it means "omit image_config — let the model pick".
# ---------------------------------------------------------------------------

SUPPORTED_ASPECT_RATIOS: Final[frozenset[str]] = frozenset(
    {
        "1:1",
        "2:3",
        "3:2",
        "3:4",
        "4:3",
        "4:5",
        "5:4",
        "9:16",
        "16:9",
        "21:9",
        "auto",
    }
)

_PORTRAIT_CANDIDATES: Final[tuple[str, ...]] = ("9:16", "2:3", "3:4", "4:5")
_LANDSCAPE_CANDIDATES: Final[tuple[str, ...]] = ("16:9", "3:2", "4:3", "5:4", "21:9")
_SQUARE_CANDIDATES: Final[tuple[str, ...]] = ("1:1",)

# Δ threshold: if the nearest same-orientation match is farther than this
# in decimal w/h, fall back to "auto" rather than force a numeric guess.
_ORIENTATION_DELTA_THRESHOLD: Final[float] = 0.30

# Log format string (identical across fleet — do not diverge).
_LOG_FORMAT_STRING: Final[str] = (
    "ASPECT_CLAMP | requested=%s clamped=%s reason=%s delta=%s"
)


def _parse_ratio_to_decimal(value: str) -> Optional[float]:
    """Parse 'W:H' to W/H as a float. None on any failure."""
    if not value or ":" not in value:
        return None
    try:
        w_str, h_str = value.split(":", 1)
        w = float(w_str.strip())
        h = float(h_str.strip())
        if h == 0.0:
            return None
        return w / h
    except (ValueError, ZeroDivisionError):
        return None


def _nearest_within_orientation(
    decimal: float, candidates: tuple[str, ...]
) -> tuple[str, float]:
    best_candidate = candidates[0]
    best_delta = float("inf")
    for cand in candidates:
        cand_decimal = _parse_ratio_to_decimal(cand)
        if cand_decimal is None:
            continue
        delta = abs(decimal - cand_decimal)
        if delta < best_delta:
            best_delta = delta
            best_candidate = cand
    return best_candidate, best_delta


def clamp_aspect_ratio(requested: str) -> tuple[str, str, Optional[float]]:
    """Clamp `requested` to the nearest supported ratio.

    Returns (clamped, reason, delta); `clamped` is guaranteed to be in
    SUPPORTED_ASPECT_RATIOS. Never raises."""
    raw = (requested or "").strip().lower()

    if not raw:
        return "auto", "empty_input", None

    if raw in SUPPORTED_ASPECT_RATIOS:
        return raw, "already_supported", 0.0 if raw != "auto" else None

    decimal = _parse_ratio_to_decimal(raw)
    if decimal is None:
        return "auto", "parse_fail_fallback_to_auto", None

    if decimal < 1.0:
        candidates = _PORTRAIT_CANDIDATES
    elif decimal > 1.0:
        candidates = _LANDSCAPE_CANDIDATES
    else:
        candidates = _SQUARE_CANDIDATES

    clamped, delta = _nearest_within_orientation(decimal, candidates)

    if delta > _ORIENTATION_DELTA_THRESHOLD:
        return "auto", "orientation_fallback_to_auto", delta

    return clamped, "orientation_preserving_nearest", delta


def clamp_and_log(requested: str) -> str:
    """Clamp `requested` and log the ASPECT_CLAMP line. Silent when the
    value was already supported."""
    clamped, reason, delta = clamp_aspect_ratio(requested)
    if reason == "already_supported":
        return clamped
    delta_repr = f"{delta:.3f}" if delta is not None else "n/a"
    log = logger.warning if reason == "orientation_fallback_to_auto" else logger.info
    log(_LOG_FORMAT_STRING, requested, clamped, reason, delta_repr)
    return clamped


# ---------------------------------------------------------------------------
# Generation — Gemini image model on its own client (prefetch-time only)
# ---------------------------------------------------------------------------

GENERATION_TIMEOUT_SECONDS = 45.0


async def lily_generate_image_bytes(
    prompt: str,
    *,
    aspect_ratio: str = "16:9",
    api_key: Optional[str] = None,
    model: Optional[str] = None,
) -> tuple[bytes, str, str]:
    """One image generation call. Returns (image_bytes, mime_type, model).

    RAISES RuntimeError with the provider's message on any failure —
    callers (the no-silent-crash wrappers below) turn that into a visible
    lily_image_attempts error row. Aspect ratio is clamped before the wire
    (the donor crash class: an off-list value 400s AFTER a good render)."""
    if not (prompt or "").strip():
        raise RuntimeError("empty image prompt")
    mdl = model or lily_config.imagegen_model()
    clamped = clamp_and_log(aspect_ratio)
    config = None
    if clamped != "auto":
        config = genai_types.GenerateContentConfig(
            image_config=genai_types.ImageConfig(aspect_ratio=clamped)
        )
    client = google_genai.Client(api_key=api_key or lily_config.google_api_key())
    response = await asyncio.wait_for(
        asyncio.to_thread(
            client.models.generate_content,
            model=mdl,
            contents=prompt,
            config=config,
        ),
        timeout=GENERATION_TIMEOUT_SECONDS,
    )
    text_reason = ""
    for candidate in getattr(response, "candidates", None) or []:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", None) or []:
            inline = getattr(part, "inline_data", None)
            data = getattr(inline, "data", None) if inline else None
            if data:
                mime = getattr(inline, "mime_type", None) or "image/png"
                logger.info(
                    "LILY_IMAGEGEN | GENERATED | model=%s bytes=%d aspect=%s",
                    mdl, len(data), clamped,
                )
                return data, mime, mdl
            part_text = getattr(part, "text", None)
            if part_text:
                text_reason = part_text
    # Empty candidate / text-only refusal — surface the provider's words
    # (the donor's silent-rejection problem: the model refuses without the
    # caller ever learning why).
    raise RuntimeError(
        "no image in response"
        + (f": {text_reason[:300]}" if text_reason else " (possible safety rejection)")
    )


async def lily_generate_question_image(
    supabase,
    *,
    session_id: str,
    question_id: str,
    prompt: str,
    aspect_ratio: str = "16:9",
) -> Optional[str]:
    """NO-SILENT-CRASH wrapper: generate + store one INVENTED-content image
    and return its public bucket URL, or None (text-only fallback).

    CACHE-FIRST: a bank row (kb_ id) that already carries an image
    short-circuits — nothing is generated. Every attempt writes a visible
    lily_image_attempts row; this function NEVER raises."""
    # Cache-first (sub-agent H rule): check the bank row before generating.
    cached = await lily_images.lily_cached_bank_image(supabase, question_id)
    if cached is not None:
        return cached["image_url"]
    try:
        data, mime, mdl = await lily_generate_image_bytes(
            prompt, aspect_ratio=aspect_ratio
        )
    except Exception as e:
        await lily_images.lily_record_image_attempt(
            supabase, session_id=session_id, question_id=question_id,
            source="generated", prompt=prompt,
            status=lily_images.ATTEMPT_REJECTED
            if "safety" in str(e).lower() or "no image in response" in str(e)
            else lily_images.ATTEMPT_ERROR,
            failure_reason=f"{type(e).__name__}: {e}",
            model=lily_config.imagegen_model(),
        )
        return None
    url = await lily_images.lily_upload_image_bytes(
        supabase, data, source="generated", content_type=mime
    )
    if url is None:
        await lily_images.lily_record_image_attempt(
            supabase, session_id=session_id, question_id=question_id,
            source="generated", prompt=prompt,
            status=lily_images.ATTEMPT_ERROR,
            failure_reason="generated ok but bucket store failed",
            model=mdl,
        )
        return None
    await lily_images.lily_record_image_attempt(
        supabase, session_id=session_id, question_id=question_id,
        source="generated", prompt=prompt,
        status=lily_images.ATTEMPT_SUCCESS, model=mdl, image_url=url,
    )
    # Write-back for bank rows so the next session cache-hits (no-op for
    # generated q_/pic_ ids, which have no DB row).
    await lily_images.lily_save_bank_image(
        supabase, question_id, image_url=url, image_source="generated",
        image_license_note=(
            f"generated by {mdl}; prompt head: {(prompt or '')[:160]}"
        ),
    )
    return url


# ---------------------------------------------------------------------------
# Reference round — "real or imagined" (pictures mode, round 2)
# ---------------------------------------------------------------------------

REAL_OR_IMAGINED_ROUND: Final[int] = 2

REAL_OR_IMAGINED_PROMPT = (
    "Eyes on the screen. One photograph, one question — is it real, "
    "or imagined?"
)

# Adjudication accepts real/fake/imagined variants (tier-1 matches against
# acceptable_answers; hedge prefixes are stripped by the normalizer).
_REAL_ACCEPTABLE = [
    "real", "a real photo", "real photo", "genuine", "actual", "true",
    "it exists", "really real",
]
_IMAGINED_ACCEPTABLE = [
    "imagined", "fake", "a fake", "generated", "ai", "ai generated",
    "made up", "not real", "imaginary", "invented",
]

# Invented-content prompts — plausible-fake photography in the donor's
# register (subject + setting + light, one camera directive, positive
# phrasing, no on-image text). All subjects are INVENTED — nothing here
# depicts a real, nameable entity.
IMAGINED_PROMPTS: Final[tuple[str, ...]] = (
    "A plausible tourist photograph of a grand stone lighthouse standing "
    "in the middle of a desert plain, golden-hour light, casual phone-photo "
    "framing. The lighthouse is invented — it exists nowhere.",
    "A believable travel photo of a small coastal town where every rooftop "
    "is mirrored glass, soft overcast light, candid street-level framing. "
    "The town is invented — it exists nowhere.",
    "A convincing nature photograph of a waterfall splitting around a "
    "perfectly spherical boulder, morning mist, handheld camera framing. "
    "The place is invented — it exists nowhere.",
    "A plausible city photograph of a subway station platform with a full "
    "grove of live birch trees growing through it, warm artificial light, "
    "commuter phone-photo framing. The station is invented — it exists "
    "nowhere.",
    "A believable aerial photograph of a five-sided baseball stadium built "
    "on a river island, late afternoon light, drone framing. The stadium "
    "is invented — it exists nowhere.",
    "A convincing photograph of a mountain village where the houses are "
    "built into the face of a single enormous split rock, clear alpine "
    "light, hiker phone-photo framing. The village is invented — it exists "
    "nowhere.",
)

# Real-photo subjects for the "real" half ride sub-agent I's curated list
# (lily_search.REAL_ENTITY_SUBJECTS) — sourced via Exa, NEVER generated.


async def lily_build_real_or_imagined_question(
    supabase,
    *,
    index: int,
    session_id: str,
    difficulty_tier: int = 2,
) -> Optional[dict]:
    """Build one 'real or imagined' question: even indexes serve a REAL
    photo (Exa-sourced, sub-agent I), odd indexes a GENERATED plausible
    fake (this module). Returns the §4.2 question shape with the image
    attached, or None on ANY failure — the caller falls back to the
    standard text supply (text-only fallback). Never raises."""
    try:
        if index % 2 == 0:
            # REAL branch — web-sourced, never generated.
            subject = lily_search.REAL_ENTITY_SUBJECTS[
                (index // 2) % len(lily_search.REAL_ENTITY_SUBJECTS)
            ]
            entity = subject["entity"]
            candidate = await lily_search.lily_find_real_entity_image(entity)
            if candidate is None:
                return None
            url = await lily_images.lily_fetch_url_to_bucket(
                supabase, candidate["image_url"], source="web"
            )
            if url is None:
                await lily_images.lily_record_image_attempt(
                    supabase, session_id=session_id,
                    question_id=f"roi_{index:04d}", source="web",
                    prompt=entity, status=lily_images.ATTEMPT_ERROR,
                    failure_reason=(
                        "fetch/store failed for accepted candidate "
                        f"{candidate['image_url'][:300]}"
                    ),
                )
                return None
            await lily_images.lily_record_image_attempt(
                supabase, session_id=session_id,
                question_id=f"roi_{index:04d}", source="web", prompt=entity,
                status=lily_images.ATTEMPT_SUCCESS, image_url=url,
            )
            return {
                "id": f"roi_{index:04d}",
                "category": "real or imagined",
                "difficulty_tier": difficulty_tier,
                "prompt": REAL_OR_IMAGINED_PROMPT,
                "canonical_answer": "real",
                "acceptable_answers": list(_REAL_ACCEPTABLE),
                "reveal_color": f"It's real — that's the {entity}.",
                "image_url": url,
                "image_source": "web",
                "image_license_note": (
                    f"web image via Exa: page={candidate['page_url']} "
                    f"image={candidate['image_url']}"
                ),
            }
        # IMAGINED branch — generated plausible fake (invented content only).
        gen_prompt = IMAGINED_PROMPTS[(index // 2) % len(IMAGINED_PROMPTS)]
        qid = f"roi_{index:04d}"
        url = await lily_generate_question_image(
            supabase, session_id=session_id, question_id=qid,
            prompt=gen_prompt, aspect_ratio="16:9",
        )
        if url is None:
            return None  # error row already written; text-only fallback
        return {
            "id": qid,
            "category": "real or imagined",
            "difficulty_tier": difficulty_tier,
            "prompt": REAL_OR_IMAGINED_PROMPT,
            "canonical_answer": "imagined",
            "acceptable_answers": list(_IMAGINED_ACCEPTABLE),
            "reveal_color": (
                "Imagined — that place does not exist. The machine made it "
                "up this afternoon."
            ),
            "image_url": url,
            "image_source": "generated",
            "image_license_note": (
                f"generated by {lily_config.imagegen_model()}; "
                f"prompt head: {gen_prompt[:160]}"
            ),
        }
    except Exception as e:
        # No-silent-crash: visible row + log line; the round continues
        # text-only.
        logger.error(
            "LILY_IMAGEGEN | REAL_OR_IMAGINED_FAILED | index=%d "
            "error_class=%s error=%s", index, type(e).__name__, e,
        )
        await lily_images.lily_record_image_attempt(
            supabase, session_id=session_id, question_id=f"roi_{index:04d}",
            source="generated" if index % 2 else "web",
            prompt="real-or-imagined builder",
            status=lily_images.ATTEMPT_ERROR,
            failure_reason=f"{type(e).__name__}: {e}",
        )
        return None
