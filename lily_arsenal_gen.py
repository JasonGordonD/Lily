"""
lily_arsenal_gen.py — the arsenal GENERATION pipeline
(WO-LILY-ARSENAL-SEED-001 A4, A5, A9).

Turns one plan slot ({subject_area, format, difficulty_tier,
binding_direction}) into one complete, servable arsenal entry — or into an
honest, counted rejection.

THE CHOKEPOINT RULE. The arsenal must not become a second, looser path to
image generation. Every image here goes through
lily_imagegen.lily_generate_image_bytes with the SAME mode and intensity
live generation uses, which means it picks up the same provider routing,
the same adult art-direction addendum, the same aspect clamp and the same
structural floor. This module builds prompts and handles outcomes; it does
not talk to any image provider itself.

DEPENDENCY INJECTION. Every external effect — authoring the question,
generating the image, uploading it, classifying it — arrives as a callable.
That is not ceremony: it is the only way this pipeline can be tested at
all, because the seeding host has no provider credentials and the fleet
policy blocks api.x.ai from anywhere but the deployment. The live wiring
lives in lily_arsenal_seed; the fakes live in the tests, and they exercise
the same code the operator's run does.

BINDING DIRECTION (A1) changes the ORDER of the two generation steps:

  image_first    — generate the image, then write the question about what
                   is actually in it. Correspondence is nearly free; the
                   image drives. Used for read-the-scene formats.
  question_first — write the question, then generate an image to complete
                   it. Riskier: the image has to show what the stem
                   claims, so this path VERIFIES correspondence with the
                   classifier before the entry can be banked at all.

MODERATION IS AN EXPECTED OUTCOME (A9), not an error. On record:
`xAI image HTTP 400: Generated image rejected by content moderation`
(2026-08-07 18:47:48). Seeding is exactly where that friction belongs —
offline, where a refusal costs a retry, rather than live at a table where
it costs silence. A refusal here is reworked a bounded number of times,
then skipped and COUNTED, so a partition whose configured heat exceeds
what the provider will paint shows up as a rejection-rate number the
operator can act on instead of a mystery.
"""

import asyncio
import json
import logging
import re
import time
from typing import Callable, Optional

import lily_arsenal
import lily_arsenal_content
import lily_arsenal_formats
import lily_config

logger = logging.getLogger("lily_arsenal_gen")

# Offline author timeout for the ADULT deck. The live game bounds Grok at
# PREFETCH_TIMEOUT_SECONDS (30s) because dead-air at a table is intolerable,
# but seeding is a batch job with no dead-air budget, and the adult author
# rides grok-*-multi-agent — a 4-agent (low) / 16-agent (high) Responses-API
# fan-out that routinely exceeds 30s to write one question. Reusing the live
# 30s here made every adult author call TimeoutError-out (empty str repr in
# 3.11), logging an empty AUTHOR_FAILED and banking nothing. Give the offline
# author room; the arsenal absorbs the wait.
ADULT_AUTHOR_TIMEOUT_SECONDS = 180.0

# Outcomes a slot can end in. Everything is counted; nothing is dropped.
OUTCOME_CREATED = "created"
OUTCOME_DUPLICATE = "duplicate"
OUTCOME_MODERATION = "moderation_rejected"
OUTCOME_CLASSIFIER = "classifier_rejected"
OUTCOME_AUTHOR_FAILED = "author_failed"
OUTCOME_ERROR = "error"
OUTCOME_UNAVAILABLE = "generation_unavailable"

# Provider phrasings that mean "the model refused to paint this", as
# distinct from "the call broke". The distinction matters: a refusal is
# reworked and counted against the partition's rejection rate, while a
# transport error is retried as a transient and counted as an error.
_MODERATION_SIGNATURES = (
    "content moderation",
    "rejected by content",
    "safety rejection",
    "possible safety rejection",
    "content policy",
    "violates",
    "prohibited_content",
    "blocked",
)

# Transport / availability phrasings — never reworked, because rewriting
# the prompt does not fix an unconfigured key or a dead socket.
_UNAVAILABLE_SIGNATURES = (
    "unconfigured",
    "api key",
    "timeout",
    "timed out",
    "connection",
    "unauthorized",
    "http 401",
    "http 403",
    "http 429",
    "http 5",
)


def lily_is_moderation_rejection(error: object) -> bool:
    """True when a generation failure was the provider REFUSING the
    content, rather than the call failing."""
    text = str(error or "").lower()
    if any(sig in text for sig in _UNAVAILABLE_SIGNATURES):
        return False
    return any(sig in text for sig in _MODERATION_SIGNATURES)


def lily_is_unavailable(error: object) -> bool:
    """True when generation is DOWN rather than refusing. The seeding job
    stops a partition on this instead of burning its whole plan against a
    provider that is not answering."""
    return any(sig in str(error or "").lower() for sig in _UNAVAILABLE_SIGNATURES)


# -- prompt construction (A4) -------------------------------------------------


def lily_build_image_prompt(
    *, partition: str, plan: dict, stem: Optional[str] = None
) -> str:
    """Compose the generation prompt for one slot: SUBJECT from the
    partition brief, COMPOSITION from the format, HOUSE STYLE from the
    partition (A3) — in that order, so the subject leads and the styling
    qualifies it rather than burying it.

    The adult art-direction addendum is deliberately NOT applied here.
    lily_imagegen.lily_generate_image_bytes applies it at the wire for
    mode='adult', and applying it twice would both bloat the prompt and
    give the arsenal its own divergent copy of the house look — precisely
    the second, looser path the work order forbids."""
    subject = str(plan.get("subject_area") or "").strip()
    fmt = str(plan.get("format") or "identify")
    spec = lily_arsenal_formats.lily_format_spec(fmt) or {}
    tier = int(plan.get("difficulty_tier") or 2)

    if stem:
        # question_first: the stem is the subject of record — the image has
        # to show what the question already claims.
        core = f"An image that clearly and unambiguously depicts: {stem.strip()}"
    else:
        core = f"A clear, single-subject image of {subject}"

    # Difficulty is a COMPOSITION dial, not a caption: an easy question
    # shows its subject head-on, a hard one shows it obliquely. Saying
    # "make this hard" to an image model produces noise, not difficulty.
    framing = {
        1: "Shot head-on and obvious, the subject unmistakable and centred.",
        2: "A slightly indirect angle — recognisable, but it takes a "
           "moment's read.",
        3: "An oblique, partial or unusual view: identifiable only by "
           "someone who looks closely. Still fair — never a trick.",
    }.get(tier, "A clear, readable view of the subject.")

    composition = str(spec.get("image_composition") or "").strip()
    house = lily_arsenal_content.lily_house_style(partition)

    parts = [core, framing]
    if composition:
        parts.append(composition)
    if house:
        parts.append(house)
    # Legibility across a room is the whole delivery context — this is a
    # screen a table looks at, not a phone held at reading distance.
    parts.append(
        "No text, watermarks or captions unless the format requires them. "
        "Readable at a glance from across a room."
    )
    return " ".join(p for p in parts if p).strip()


def lily_rework_prompt(prompt: str, attempt: int) -> str:
    """Rework a prompt the provider refused (A9). Each attempt steps the
    request down one notch toward what the model will actually paint,
    WITHOUT abandoning the slot's subject — a rework that changes the
    subject is not a retry, it is a different question.

    Bounded by lily_config.arsenal_moderation_retries(); after that the
    slot is skipped and counted."""
    base = (prompt or "").strip()
    ladder = [
        # 1st rework: same scene, implied rather than depicted.
        "Render this suggestively rather than graphically: implication, "
        "silhouette, framing and expression carry the meaning. Nothing "
        "explicit is shown directly.",
        # 2nd rework: pull all the way back to objects and setting.
        "Render this as a tasteful still-life or setting: the objects and "
        "the atmosphere of the scene, with no explicit action depicted.",
    ]
    step = ladder[min(max(0, attempt - 1), len(ladder) - 1)]
    return f"{base} {step}"


# -- answer sets (A4) ---------------------------------------------------------

_ARTICLE_RE = re.compile(r"^(the|a|an)\s+", re.IGNORECASE)
_NON_ALNUM_RE = re.compile(r"[^a-z0-9\s]+")
_WS_RE = re.compile(r"\s+")


def lily_answer_set(canonical: str, extra=None) -> list:
    """Build the acceptable-answer set at GENERATION time, not serve time.

    A player says a thing out loud in a noisy room and a recogniser writes
    down something near it. The set therefore carries, alongside whatever
    near-misses the author supplied: the bare canonical, the
    article-stripped form ("the Colosseum" -> "colosseum"), and the
    possessive-stripped form. Those three cover the manglings that are
    purely grammatical rather than acoustic; the ACOUSTIC ones are handled
    downstream by lily_evaluation's phonetic tier, which keys homophonic
    initials together ("Kanberra"/"Canberra") — so the set does not need to
    enumerate spellings the phonetic matcher already collapses.

    NOTE (deliberate divergence from the work order's A4 line): the answer
    set is NOT pushed into the STT's `additional_vocab`. That slot is pinned
    by WO-LILY-STT-001 to the assistant name and player names, with a test
    asserting it — 'never answer nouns (expectation-primed matching is the
    generalizing mechanism; preloading answers does not generalize)'.
    Preloading the night's answers into the recogniser would also leak them
    into any vocab dump. The set does its work where it belongs: in
    acceptable_answers, which the Tier-1 evaluator already matches
    exactly, by containment, fuzzily and phonetically."""
    out = []
    seen = set()

    def _add(value) -> None:
        text = str(value or "").strip().lower()
        text = _NON_ALNUM_RE.sub(" ", text)
        text = _WS_RE.sub(" ", text).strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)

    _add(canonical)
    stripped = _ARTICLE_RE.sub("", str(canonical or "").strip())
    _add(stripped)
    _add(re.sub(r"'s\b", "", str(canonical or ""), flags=re.IGNORECASE))
    for item in (extra or []):
        _add(item)
    return out


# -- the register-aware content gate (A4) -------------------------------------


def lily_classifier_brief(partition: str) -> str:
    """The instruction the outbound classifier judges an arsenal image
    against, per REGISTER.

    The existing web-image gate (lily_reasoning.approve_entity_image) is
    hardcoded family-friendly — 'no nudity, gore, violence' — which is
    correct for a general-deck photograph sourced off the web and WRONG for
    an adult_explicit arsenal entry, where the whole point is content that
    gate would refuse. Reusing it unchanged would have quietly rejected
    every adult entry and left those two partitions permanently empty,
    which is the same empty shelf in a different costume.

    So the gate is register-aware. What does NOT vary by register is the
    structural floor: no minors, nothing non-consensual, nothing outside
    legal hard limits. That is hardcoded into every brief below and is not
    configurable at any heat."""
    floor = (
        "ABSOLUTE FLOOR, applies at every register and is not negotiable: "
        "reject outright if the image depicts, suggests or could be read as "
        "depicting anyone under 18; reject anything non-consensual or "
        "coercive; reject anything outside legal hard limits. When unsure "
        "about age or consent, REJECT."
    )
    register = {
        "general": (
            "This image plays at ANY table, including a family one. It must "
            "be suitable for a general audience: no nudity, no sexual "
            "content, no gore, no shock content."
        ),
        "adult_suggestive": (
            "This image plays at a gated ADULT table at SUGGESTIVE heat. "
            "Sensuality, innuendo, flirtation, permissive wear and "
            "kink-coded styling are all IN BOUNDS and expected — do not "
            "reject for being racy. Reject only if it goes to graphic "
            "hardcore detail, which overshoots this partition's heat."
        ),
        "adult_explicit": (
            "This image plays at a gated ADULT table at EXPLICIT heat, the "
            "top of the configured ladder. Graphic adult content between "
            "adults is IN BOUNDS — do not reject for explicitness. Reject "
            "only on the absolute floor below."
        ),
    }.get(partition, "Treat this as general-audience content.")
    return f"{register} {floor}"


async def lily_classify_entry(
    *,
    partition: str,
    image_bytes: bytes,
    content_type: str,
    claim: str,
    classify: Optional[Callable] = None,
) -> tuple:
    """Run the outbound classifier over one candidate entry. Returns
    (approved: bool, reason: str).

    Judges TWO things at once, which is what makes it the picture
    contract's gate rather than a content filter: (1) register-appropriate
    per the brief above, and (2) CORRESPONDENCE — the image actually shows
    what the question claims. (2) is the one that matters for
    question-first entries, where nothing else checks that the picture and
    the stem are about the same thing.

    FAIL CLOSED on any error, exactly like the web-image gate: an entry
    that could not be judged is not banked. A missing classifier is a
    configuration state, not a pass — it returns False with a reason the
    run summary reports, so a run with no classifier cannot quietly fill
    the bank with unvetted images."""
    if classify is None:
        return False, "no classifier configured (fail closed)"
    try:
        approved, reason = await classify(
            image_bytes, content_type, claim, lily_classifier_brief(partition)
        )
        logger.log(
            logging.INFO if approved else logging.WARNING,
            "LILY_ARSENAL_GEN | CLASSIFIER | partition=%s approved=%s reason=%r",
            partition, approved, str(reason)[:200],
        )
        return bool(approved), str(reason or "")[:300]
    except Exception as e:
        logger.warning(
            "LILY_ARSENAL_GEN | CLASSIFIER_FAILED | partition=%s "
            "approved=False (fail closed) error_class=%s error=%s",
            partition, type(e).__name__, e,
        )
        return False, f"gate error ({type(e).__name__}): {e}"


# -- one slot, end to end -----------------------------------------------------


async def lily_generate_entry(
    *,
    partition: str,
    plan: dict,
    author,
    imagegen,
    upload,
    classify=None,
    describe=None,
    cost_per_image: Optional[float] = None,
    max_moderation_retries: Optional[int] = None,
) -> dict:
    """Produce ONE arsenal entry from one plan slot, or an honest outcome.

    Callables:
      author(partition, plan, image_description) -> dict | None
          Writes the question. `image_description` is None on the
          question-first path (nothing has been drawn yet) and, on the
          image-first path, carries a VISION caption of what the image model
          ACTUALLY rendered — not the intended prompt. The image model
          adheres to prompts loosely, so a stem authored from the prompt
          disagrees with the picture and the gate rejects it; authoring from
          the rendered image is what makes the stem match.
      imagegen(prompt, partition, intensity) -> (bytes, mime, model)
          RAISES on refusal or failure — the same contract
          lily_imagegen.lily_generate_image_bytes has.
      upload(bytes, mime, partition) -> storage_path | None
      classify(bytes, mime, claim, brief) -> (approved, reason)
      describe(bytes, mime) -> str | None
          Captions the rendered image for the image-first author. Optional:
          if unwired or it fails, the author falls back to the generation
          prompt (the prior behaviour), never worse.

    Returns {outcome, entry, attempts, cost_usd, reason}. `entry` is
    populated only on OUTCOME_CREATED; every other outcome carries a reason
    the run summary counts and reports."""
    started = time.monotonic()
    intensity = lily_arsenal.lily_partition_intensity(partition)
    binding = str(
        plan.get("binding_direction")
        or lily_arsenal_formats.lily_binding_direction(
            str(plan.get("format") or "identify")
        )
    )
    retries = (
        lily_config.arsenal_moderation_retries()
        if max_moderation_retries is None
        else max_moderation_retries
    )
    unit_cost = (
        lily_config.arsenal_image_cost_usd()
        if cost_per_image is None
        else cost_per_image
    )

    def _result(outcome, *, entry=None, attempts=0, reason="") -> dict:
        return {
            "outcome": outcome,
            "entry": entry,
            "attempts": attempts,
            # Cost counts ATTEMPTS, not successes: a refused generation is
            # still billed, and a run summary that only prices the entries
            # it kept understates the shelf.
            "cost_usd": round(attempts * unit_cost, 5),
            "reason": reason,
            "seconds": round(time.monotonic() - started, 2),
            "binding_direction": binding,
        }

    # -- question-first: write the stem, then draw it ------------------------
    stem_question = None
    if binding == lily_arsenal_formats.BINDING_QUESTION_FIRST:
        try:
            stem_question = await author(partition, plan, None)
        except Exception as e:
            logger.warning(
                "LILY_ARSENAL_GEN | AUTHOR_FAILED | partition=%s: %s", partition, e
            )
            return _result(OUTCOME_AUTHOR_FAILED, reason=str(e)[:300])
        if not stem_question:
            return _result(OUTCOME_AUTHOR_FAILED, reason="author returned nothing")

    prompt = lily_build_image_prompt(
        partition=partition,
        plan=plan,
        stem=(stem_question or {}).get("question_text") if stem_question else None,
    )

    # -- generate, reworking a refusal a bounded number of times ------------
    attempts = 0
    image_bytes = mime = model = None
    last_error = ""
    for attempt in range(retries + 1):
        attempts += 1
        try:
            image_bytes, mime, model = await imagegen(
                prompt if attempt == 0 else lily_rework_prompt(prompt, attempt),
                partition,
                intensity,
            )
            break
        except Exception as e:
            last_error = str(e)[:300]
            if lily_is_unavailable(e):
                logger.warning(
                    "LILY_ARSENAL_GEN | GENERATION_UNAVAILABLE | partition=%s: %s",
                    partition, last_error,
                )
                return _result(
                    OUTCOME_UNAVAILABLE, attempts=attempts, reason=last_error
                )
            if not lily_is_moderation_rejection(e):
                logger.warning(
                    "LILY_ARSENAL_GEN | GENERATION_ERROR | partition=%s: %s",
                    partition, last_error,
                )
                return _result(OUTCOME_ERROR, attempts=attempts, reason=last_error)
            logger.info(
                "LILY_ARSENAL_GEN | MODERATION_REJECTED | partition=%s "
                "attempt=%d/%d — reworking prompt",
                partition, attempt + 1, retries + 1,
            )
    if image_bytes is None:
        # Every rework was refused. Counted, not hidden: this is the number
        # that tells the operator the configured heat exceeds what the
        # provider will paint.
        return _result(OUTCOME_MODERATION, attempts=attempts, reason=last_error)

    # -- image-first: now write the question about what was drawn -----------
    question = stem_question
    if question is None:
        # Author from what the model ACTUALLY rendered, not the intended
        # prompt: grok-imagine follows prompts loosely (codpiece -> corset,
        # "three people" -> five), so a stem written from the prompt
        # disagrees with the picture and the correspondence gate correctly
        # rejects it. A vision caption of image_bytes closes that gap. Fall
        # back to the prompt if no describer is wired or it fails.
        rendered = prompt
        if describe is not None:
            try:
                caption = await describe(image_bytes, mime or "image/jpeg")
                if caption:
                    rendered = caption
            except Exception as e:
                logger.warning(
                    "LILY_ARSENAL_GEN | DESCRIBE_FAILED | partition=%s "
                    "(falling back to prompt): %s", partition, e,
                )
        try:
            question = await author(partition, plan, rendered)
        except Exception as e:
            logger.warning(
                "LILY_ARSENAL_GEN | AUTHOR_FAILED | partition=%s: %s", partition, e
            )
            return _result(
                OUTCOME_AUTHOR_FAILED, attempts=attempts, reason=str(e)[:300]
            )
        if not question:
            return _result(
                OUTCOME_AUTHOR_FAILED, attempts=attempts,
                reason="author returned nothing",
            )

    # -- the gate: register + correspondence, before anything is banked -----
    claim = str(question.get("question_text") or "").strip()
    canonical = str(question.get("canonical_answer") or "").strip()
    approved, reason = await lily_classify_entry(
        partition=partition,
        image_bytes=image_bytes,
        content_type=mime or "image/jpeg",
        claim=f"{claim} (the correct answer is: {canonical})",
        classify=classify,
    )
    if not approved:
        return _result(OUTCOME_CLASSIFIER, attempts=attempts, reason=reason)

    path = await upload(image_bytes, mime or "image/jpeg", partition)
    if not path:
        return _result(
            OUTCOME_ERROR, attempts=attempts,
            reason="image generated and approved but bucket store failed",
        )

    entry = {
        "question_text": claim,
        "prompt": claim,
        "canonical_answer": canonical,
        "acceptable_answers": lily_answer_set(
            canonical, question.get("acceptable_answers")
        ),
        "options": question.get("options"),
        "reveal_color": question.get("reveal_color") or "",
        "generation_prompt": prompt,
        "generation_model": model,
        "image_storage_path": path,
        "image_source": "generated",
        "is_real_image": False,
        "format": plan.get("format") or "identify",
        "binding_direction": binding,
        "subject_area": plan.get("subject_area"),
        "difficulty_tier": int(plan.get("difficulty_tier") or 2),
        "_arsenal_intensity": intensity,
    }
    return _result(OUTCOME_CREATED, entry=entry, attempts=attempts, reason=reason)


# ---------------------------------------------------------------------------
# Live provider bindings (shared by the seeding job and in-session refill)
# ---------------------------------------------------------------------------


def lily_extract_json_object(raw: object) -> Optional[dict]:
    """Best-effort parse of an author reply into a dict.

    Survives ```json fenced output and prose wrapped around the object by
    pulling the first balanced {...} (string- and escape-aware) and parsing
    that. Returns None on anything that is not valid JSON — it does not
    coerce garbage into a dict."""
    text = str(raw or "").strip()
    if not text:
        return None

    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()

    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass

    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(text[start : i + 1])
                    return obj if isinstance(obj, dict) else None
                except Exception:
                    return None
    return None


async def lily_author_question(
    reasoning, *, partition: str, plan: dict, image_description: Optional[str]
) -> Optional[dict]:
    """Write one question for a slot, through the SAME deck routing the
    live question supply uses: Gemini for general, Grok for the adult
    partitions (Gemini's non-overridable PROHIBITED_CONTENT filter refuses
    adult-deck material — the reason the adult decks already ride xAI).

    Returns the question fields only; the image, the gate and the banking
    are the caller's."""
    import json

    fmt = str(plan.get("format") or "identify")
    spec = lily_arsenal_formats.lily_format_spec(fmt) or {}
    brief = lily_arsenal_content.lily_brief(partition) or {}
    adult = partition in lily_arsenal.ADULT_PARTITIONS

    scene = (
        f"The image has already been generated from this prompt:\n"
        f"{image_description}\n\n"
        "Write the question about WHAT IS ACTUALLY IN THAT IMAGE."
        if image_description
        else "The image has NOT been generated yet — it will be generated "
        "from your question, so the question must describe something an "
        "image can unambiguously show."
    )
    instruction = (
        f"You are writing ONE picture-trivia question for a spoken trivia "
        f"game. Register: {partition}. Format: {fmt} — {spec.get('description', '')}\n"
        f"Subject area: {plan.get('subject_area')}\n"
        f"Difficulty tier: {plan.get('difficulty_tier')} (1 easy, 3 hard)\n"
        f"Partition intent: {brief.get('summary', '')}\n"
        f"Avoid: {', '.join(brief.get('avoid') or ())}\n\n"
        f"{scene}\n\n"
        "The question is SPOKEN ALOUD and the image is on a screen. Keep the "
        "stem to one or two sentences. "
        + (
            "Provide exactly four options that are PHONETICALLY DISTINCT from "
            "one another — they are spoken and heard, never shown as letters. "
            if spec.get("answer_style") == "multiple_choice"
            else ""
        )
        + "Return JSON with keys: question_text, canonical_answer, "
        "acceptable_answers, options (array of 4 or null), reveal_color (one "
        "spoken sentence revealing the answer with personality).\n"
        "acceptable_answers is the array a LEXICAL grader matches a spoken "
        "answer against — it does NOT infer synonyms, so coverage here is "
        "what makes a genuinely-correct spoken answer count. Enumerate the "
        "informal ways a real player would say this answer OUT LOUD: the "
        "canonical answer, formal synonyms, AND the common colloquial "
        "abbreviations, slang, and short-forms people actually blurt out "
        "(adult terms explicitly in scope). E.g. for 'dominatrix' include "
        "'dom', 'domme', 'mistress'; for 'submissive' include 'sub'. For a "
        "PAIRED answer, include the abbreviated pairing AND each part alone "
        "(e.g. 'dominatrix and submissive' -> 'dom and sub', 'dom', 'sub'). "
        "Add plausible speech-recogniser mishearings. Only include phrasings "
        "that are genuinely EQUIVALENT to the correct answer — never a "
        "near-miss, an option distractor, or a wrong-but-close term."
    )
    async def _call():
        if adult:
            return await reasoning._generate_grok_json(
                instruction,
                system_instruction=(
                    "You write adult-register trivia for a gated adult table. "
                    "Consenting adults only; never minors, never "
                    "non-consensual. Return JSON only."
                ),
                max_tokens=2048,
                timeout=ADULT_AUTHOR_TIMEOUT_SECONDS,
            )
        return await reasoning._generate(
            reasoning._model,
            instruction,
            thinking_level="low",
            response_mime_type="application/json",
            max_output_tokens=2048,
        )

    last_err: object = "author returned nothing"
    attempts = (1, 2, 3)
    for attempt in attempts:
        try:
            raw = await _call()
            data = lily_extract_json_object(raw)
            if data and data.get("question_text") and data.get("canonical_answer"):
                return data
            last_err = "unparseable or incomplete author output"
        except Exception as e:
            # str(TimeoutError()) is "" on 3.11 (asyncio.TimeoutError is the
            # builtin); fall back to repr so a timeout never logs blank.
            last_err = str(e) or repr(e)
        if attempt < attempts[-1]:
            logger.info(
                "LILY_ARSENAL_SEED | AUTHOR_RETRY | partition=%s format=%s: %s",
                partition, fmt, last_err,
            )

    logger.warning(
        "LILY_ARSENAL_SEED | AUTHOR_FAILED | partition=%s format=%s: %s",
        partition, fmt, last_err,
    )
    return None


async def lily_classify_image(
    reasoning, *, image_bytes: bytes, content_type: str, claim: str, brief: str
) -> tuple:
    """Register-aware outbound classifier over one candidate entry.

    Mirrors lily_reasoning.approve_entity_image's fail-closed discipline
    but takes its content brief from the caller, because that gate is
    hardcoded family-friendly and would refuse every adult entry."""
    import json

    from google.genai import types as genai_types

    try:
        config = genai_types.GenerateContentConfig(
            thinking_config=genai_types.ThinkingConfig(thinking_level="low"),
            max_output_tokens=lily_config.judge_max_output_tokens(),
            response_mime_type="application/json",
            response_schema=genai_types.Schema(
                type=genai_types.Type.OBJECT,
                properties={
                    "approved": genai_types.Schema(type=genai_types.Type.BOOLEAN),
                    "reason": genai_types.Schema(type=genai_types.Type.STRING),
                },
                required=["approved", "reason"],
            ),
        )
        mime = (content_type or "image/jpeg").split(";", 1)[0].strip().lower()
        prompt = (
            "You are the content gate for a picture-trivia bank. Approve "
            "this image ONLY if BOTH hold:\n"
            f"(1) REGISTER: {brief}\n"
            f"(2) CORRESPONDENCE: the image genuinely shows what this "
            f"question claims — {claim!r}. The image IS the question's own "
            "image, not decoration attached to it. If a player looking at "
            "this image could not reasonably arrive at that answer, reject.\n"
            "When unsure, reject. Answer in the JSON schema."
        )
        response = await asyncio.wait_for(
            asyncio.to_thread(
                reasoning._client.models.generate_content,
                model=reasoning._model,
                contents=[
                    genai_types.Part.from_bytes(data=image_bytes, mime_type=mime),
                    prompt,
                ],
                config=config,
            ),
            timeout=25.0,
        )
        verdict = json.loads(getattr(response, "text", "") or "{}")
        return bool(verdict.get("approved")), str(verdict.get("reason", ""))[:300]
    except Exception as e:
        return False, f"gate error ({type(e).__name__}): {e}"


async def lily_describe_image(
    reasoning, *, image_bytes: bytes, content_type: str
) -> Optional[str]:
    """Caption the ACTUAL rendered image so the image-first author writes the
    stem about what the model really drew, not the loosely-followed prompt.

    Uses the SAME Gemini vision path as lily_classify_image — the gate
    already sees adult imagery over this path, so a literal description does
    too. Returns None on any error; lily_generate_entry then falls back to
    the generation prompt (never worse than the prior behaviour). Partition-
    agnostic: correspondence matters for every tier, not just adult."""
    from google.genai import types as genai_types

    try:
        config = genai_types.GenerateContentConfig(
            thinking_config=genai_types.ThinkingConfig(thinking_level="low"),
            max_output_tokens=lily_config.judge_max_output_tokens(),
        )
        mime = (content_type or "image/jpeg").split(";", 1)[0].strip().lower()
        prompt = (
            "Describe exactly what is visible in this image for a trivia "
            "question writer. Name the main subject, what it is doing, "
            "notable objects, the setting, clothing, and the count of people. "
            "Be concrete and literal — describe only what is actually shown, "
            "do not infer intent or add anything that is not visible. Two to "
            "four sentences of plain prose."
        )
        response = await asyncio.wait_for(
            asyncio.to_thread(
                reasoning._client.models.generate_content,
                model=reasoning._model,
                contents=[
                    genai_types.Part.from_bytes(data=image_bytes, mime_type=mime),
                    prompt,
                ],
                config=config,
            ),
            timeout=25.0,
        )
        text = str(getattr(response, "text", "") or "").strip()
        return text or None
    except Exception as e:
        logger.warning(
            "LILY_ARSENAL_GEN | DESCRIBE_ERROR | error_class=%s error=%s",
            type(e).__name__, e,
        )
        return None
