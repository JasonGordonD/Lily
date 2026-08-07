"""
lily_reasoning.py — LILY background reasoning node.

Runs on `gemini-3.1-pro-preview` via the google-genai SDK with its OWN
client (spec §11.5: HTTP client isolation — the vocal node's plugin client
and this client are two separate clients from day one; each genai.Client
instance owns its own HTTP transport).

Responsibilities:
  - Question prefetch (N+1): the next question is generated in the
    background while the current one plays out, in the structured
    question JSON shape from spec §4.2.
  - Verification at prefetch time: the reasoning node checks the
    generated question pins to exactly one defensible answer before it
    is handed to the vocal node. KB-bank questions bypass verification.
  - Tier-2 judge transport: the judge CONTRACT lives in lily_evaluation;
    this module carries the call on the vocal model (spec §4.4 — the
    vocal model owns Tier-2 evaluation) without producing a spoken turn.

Failure discipline (spec §11.2 — honest failure, never silent): every
prefetch/verification failure writes a status note into the scorekeeper
state block so Lily says "give me one second, this table broke my
question machine" instead of inventing an explanation.
"""

import asyncio
import json
import logging
import random
import re
from typing import Optional

import aiohttp

from google import genai as google_genai
from google.genai import types as genai_types

import lily_config
import lily_evaluation
# Web tools + image pipeline (WO-LILY-OMNIBUS-002): lily_search and
# lily_imagegen are REASONING-NODE-ONLY — this module is their one legal
# consumer seam. The vocal node (lily_agent) must never import them; web
# results and images reach it only as finished question payloads, bank
# rows, or state-block facts (guardrail: lily_search import tripwire +
# tests/test_web_guardrails.py).
import lily_imagegen
import lily_search

logger = logging.getLogger("lily_reasoning")

# Reference picture round (sub-agent J) — re-exported so the agent layer
# can gate WHICH rounds are picture rounds without importing the image
# stack itself.
REAL_OR_IMAGINED_ROUND = lily_imagegen.REAL_OR_IMAGINED_ROUND

# Current-events categories get a fresh-facts brief from the web at
# prefetch (Tavily; reasoning node only). Anything else generates from
# model knowledge.
_CURRENT_EVENTS_RE = re.compile(
    r"\b(current events?|news|this (?:week|month|year)|headlines?)\b",
    re.IGNORECASE,
)

# Content GENERATION (categories, questions, round-building) runs at HIGH
# thinking — operator rule 2026-08-06: generation is never low, quality over
# latency. Live-verified on gemini-3.1-pro-preview: thinking_level=high with
# response_schema returns valid structured JSON (finishReason STOP, no body
# starvation — the old starvation trap was thinking_BUDGET, not level).
REASONING_THINKING_LEVEL = "high"  # spec §4.4: thinking_level, never thinking_budget
# Tier-2 adjudication of a close/ambiguous player answer is a HIGH-stakes
# reasoning call (operator rule 2026-08-06: adjudication -> HIGH). The 12s
# bound in judge() + Tier-1 fallback protects the critical path if a HIGH
# turn runs long.
JUDGE_THINKING_LEVEL = "high"
PREFETCH_TIMEOUT_SECONDS = 30.0

# xAI multi-agent transport (Engineering Note 2026-08-07): the
# grok-*-multi-agent tier rejects the Chat Completions endpoint (HTTP 400
# "Multi Agent requests are not allowed on chat completions") and speaks the
# Responses API only, where it also does not accept `response_format`, so
# the JSON contract is prompt-enforced. Beta model — interface may change.
_MULTI_AGENT_JSON_DIRECTIVE = (
    "\n\nRespond with ONLY a single valid JSON object and nothing else — no "
    "prose, no explanation, no markdown code fences."
)


def _lily_is_multi_agent_model(model) -> bool:
    """True for xAI's multi-agent tier ids (e.g. grok-4.20-multi-agent),
    which must use the Responses API instead of Chat Completions."""
    m = str(model or "").lower()
    return "multi-agent" in m or "multi_agent" in m


def _lily_strip_json_fences(text: str) -> str:
    """Strip a leading/trailing markdown code fence if the model wrapped its
    JSON (the multi-agent path has no response_format to guarantee raw JSON)."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t[3:]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


def _lily_extract_chat_text(data) -> str:
    """Chat Completions: choices[0].message.content."""
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"xAI adult generation malformed response: {e}")


def _lily_extract_responses_text(data) -> str:
    """Responses API: the leader agent's message text. Prefer the SDK-style
    `output_text` convenience if present; otherwise walk `output[]` for
    message items and concatenate their output_text parts (reasoning/tool
    items are skipped). Fences stripped so the caller's json.loads sees raw
    JSON."""
    if isinstance(data, dict):
        direct = data.get("output_text")
        if isinstance(direct, str) and direct.strip():
            return _lily_strip_json_fences(direct)
        parts = []
        for item in (data.get("output") or []):
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            for chunk in (item.get("content") or []):
                if isinstance(chunk, dict) and chunk.get("type") in (
                    "output_text", "text"
                ):
                    parts.append(chunk.get("text") or "")
        if parts:
            return _lily_strip_json_fences("".join(parts))
    raise RuntimeError("xAI adult generation malformed Responses payload")

# Adult-product context (§11.1): explicit safety settings on every call —
# an unconfigured node goes mute mid-innuendo-round with zero diagnostics.
_SAFETY_SETTINGS = [
    genai_types.SafetySetting(
        category=genai_types.HarmCategory.HARM_CATEGORY_HARASSMENT,
        threshold=genai_types.HarmBlockThreshold.BLOCK_NONE,
    ),
    genai_types.SafetySetting(
        category=genai_types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
        threshold=genai_types.HarmBlockThreshold.BLOCK_NONE,
    ),
    genai_types.SafetySetting(
        category=genai_types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
        threshold=genai_types.HarmBlockThreshold.BLOCK_NONE,
    ),
    genai_types.SafetySetting(
        category=genai_types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
        threshold=genai_types.HarmBlockThreshold.BLOCK_NONE,
    ),
]

# Structured output (2026-07-14 P1 fix: QUESTION_PARSE_FAILED -> PREFETCH_FAILED):
# every generation/verification call pins BOTH response_mime_type="application/json"
# AND a response_schema, so the model returns schema-conformant JSON and the
# regex/fence-stripping parse path is retired to a defensive last resort.
#
# The question schema carries ALL fields — current plus reserved-for-later
# sub-agents — so downstream schema evolution is additive, never breaking:
#   choices            (exactly 4 strings; multiple-choice, sub-agent G — ACTIVE:
#                       populated when the round runs multiple choice)
#   image_url / image_source (generated|web|none; images, sub-agent H)
#   proposed_category  (category proposals, sub-agent F)
_QUESTION_RESPONSE_SCHEMA = genai_types.Schema(
    type=genai_types.Type.OBJECT,
    properties={
        "id": genai_types.Schema(
            type=genai_types.Type.STRING,
            description="Question id, shape q_<4 digits>.",
        ),
        "category": genai_types.Schema(type=genai_types.Type.STRING),
        "difficulty_tier": genai_types.Schema(
            type=genai_types.Type.INTEGER,
            description="1 (warm-up) to 4 (final round).",
        ),
        "prompt": genai_types.Schema(
            type=genai_types.Type.STRING,
            description="The question exactly as Lily should speak it.",
        ),
        "canonical_answer": genai_types.Schema(type=genai_types.Type.STRING),
        "acceptable_answers": genai_types.Schema(
            type=genai_types.Type.ARRAY,
            items=genai_types.Schema(type=genai_types.Type.STRING),
            description="Lowercase canonical answer plus common variants.",
        ),
        "reveal_color": genai_types.Schema(
            type=genai_types.Type.STRING,
            description="One short spicy fact or trap-note for the reveal.",
        ),
        # Reserved (optional) fields — populated by later sub-agents.
        "choices": genai_types.Schema(
            type=genai_types.Type.ARRAY,
            items=genai_types.Schema(type=genai_types.Type.STRING),
            min_items=4,
            max_items=4,
            nullable=True,
            description="Exactly 4 options for multiple-choice questions "
                        "(sub-agent G). Omit for open questions.",
        ),
        "image_url": genai_types.Schema(
            type=genai_types.Type.STRING, nullable=True,
        ),
        "image_source": genai_types.Schema(
            type=genai_types.Type.STRING,
            enum=["generated", "web", "none"],
            nullable=True,
            description="Provenance of image_url (sub-agent H).",
        ),
        "proposed_category": genai_types.Schema(
            type=genai_types.Type.STRING, nullable=True,
            description="Model-proposed category (sub-agent F).",
        ),
    },
    required=[
        "id", "category", "difficulty_tier", "prompt",
        "canonical_answer", "acceptable_answers", "reveal_color",
    ],
)

# Grok JSON mode carries no server-side schema — these addenda pin the
# exact shapes the Gemini response_schema enforces, for the adult-deck
# transport. The defensive parsers downstream cover any drift.
_GROK_QUESTION_SHAPE_ADDENDUM = """

Respond with ONLY a JSON object, no markdown fences, with EXACTLY these
fields: "id" (string, shape q_<4 digits>), "category" (string),
"difficulty_tier" (integer 1-4), "prompt" (the question exactly as the
host should speak it), "canonical_answer" (string),
"acceptable_answers" (array of lowercase strings: the canonical answer
plus common variants), "reveal_color" (one short spicy fact for the
reveal). Include "choices" (array of exactly 4 strings) ONLY for a
multiple-choice question."""

_GROK_VERDICT_SHAPE_ADDENDUM = """

Respond with ONLY a JSON object, no markdown fences, with EXACTLY these
fields: "verdict" ("pass" or "fail"), "reason" (string), and
"corrected_canonical_answer" (string, ONLY when a small correction fixes
the question; null otherwise)."""

_VERIFICATION_RESPONSE_SCHEMA = genai_types.Schema(
    type=genai_types.Type.OBJECT,
    properties={
        "verdict": genai_types.Schema(
            type=genai_types.Type.STRING, enum=["pass", "fail"],
        ),
        "reason": genai_types.Schema(type=genai_types.Type.STRING),
        "corrected_canonical_answer": genai_types.Schema(
            type=genai_types.Type.STRING, nullable=True,
            description="Only when a small correction fixes the question; "
                        "null otherwise.",
        ),
    },
    required=["verdict", "reason"],
)

_GENERATION_PROMPT = """You write questions for Lily, a live voice trivia host.

Write ONE trivia question following these constraints:
- Well-known, verifiable fact with a single short answer (a name, date,
  place, or number). Ask only about things you are certain of.
- PIN the question to exactly one defensible answer ("the 27th president,
  born in Ohio" — never "a president from Ohio"). If it could have two
  right answers, it has zero — pick a cleaner fact.
- Short. One breath when spoken. The thing being asked for goes at the
  very END of the sentence, with one narrowing adjective near the end.
- Target difficulty: a table of ordinary adults gets about half of these
  right overall. difficulty_tier {difficulty_tier} of 4 (1 = warm-up
  ~65% success, 4 = final round ~40-45% success).
- Category: {category}.
- Mode: {mode}. In adult mode: innuendo and wordplay, surprising sex-ed
  facts, pop culture scandal, drinking culture, questionable historical
  decisions — about the world, never about the people in the room.
- Do NOT repeat or closely resemble any of these already-used questions:
{avoid_block}

Respond with ONLY a JSON object, no markdown fences, exactly this shape:
{{"id": "q_<4 digits>", "category": "{category}", "difficulty_tier": {difficulty_tier},
 "prompt": "<the question as Lily should speak it>",
 "canonical_answer": "<the single answer>",
 "acceptable_answers": ["<lowercase canonical>", "<common variants>"],
 "reveal_color": "<one short spicy fact or trap-note for the reveal>"}}"""

_VERIFICATION_PROMPT = """You verify trivia questions before a live host performs them.
Question JSON:
{question_json}

Check, using careful reasoning:
1. Is the stated canonical_answer factually correct for the prompt?
2. Is the prompt pinned to exactly ONE defensible answer (no second
   correct answer a reasonable pub table could argue)?
3. Is the answer short and speakable?

Respond with ONLY a JSON object, no markdown fences:
{{"verdict": "pass" | "fail", "reason": "<one line>",
 "corrected_canonical_answer": "<only if a small correction fixes it, else null>"}}"""

# Multiple-choice generation (sub-agent G): appended to the generation
# prompt when the active round runs multiple choice. The `choices` slot
# already exists in _QUESTION_RESPONSE_SCHEMA (exactly 4 strings, nullable).
_MC_CHOICES_ADDENDUM = """
This is a MULTIPLE-CHOICE question. ALSO include a "choices" array in the
JSON object — EXACTLY 4 options, each short and speakable:
- The canonical_answer appears VERBATIM as one of the 4.
- Two distractors are genuinely plausible — same category, same shape,
  the kind a confident table argues over.
- Exactly ONE distractor is clearly, comically wrong — the pub-quiz laugh
  option: wrong enough to get the laugh, on-topic enough to be said with
  a straight face.
- RANDOMIZE the order of the 4 (never alphabetical, never answer-first).
Add to the JSON object:
 "choices": ["<option>", "<option>", "<option>", "<option>"]"""

# Distractor synthesis for questions that arrive WITHOUT choices (KB-bank
# rows, or a generated MC question whose choices failed validation) — runs
# on the reasoning node only, at prefetch time.
_DISTRACTOR_PROMPT = """You write wrong-answer options for a live pub-trivia host.
Question: {prompt}
Correct answer: {canonical_answer}

Write EXACTLY 3 distractors for a four-option multiple-choice read of this
question:
- Two genuinely plausible — same category, same shape as the correct answer.
- Exactly one clearly, comically wrong — the pub-quiz laugh option.
- None correct or arguably correct; none a restatement of the answer.
- Each short and speakable.

Respond with ONLY a JSON object, no markdown fences:
{{"distractors": ["<plausible>", "<plausible>", "<clearly wrong>"]}}"""

_DISTRACTOR_RESPONSE_SCHEMA = genai_types.Schema(
    type=genai_types.Type.OBJECT,
    properties={
        "distractors": genai_types.Schema(
            type=genai_types.Type.ARRAY,
            items=genai_types.Schema(type=genai_types.Type.STRING),
            min_items=3,
            max_items=3,
        ),
    },
    required=["distractors"],
)


def _strip_fences(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    start, end = t.find("{"), t.rfind("}")
    if start != -1 and end > start:
        t = t[start:end + 1]
    return t


def _shape_question(data) -> Optional[dict]:
    """Shape-check + default-fill one parsed question dict (spec §4.2)."""
    if not isinstance(data, dict):
        return None
    if not data.get("prompt") or not data.get("canonical_answer"):
        return None
    if not isinstance(data.get("acceptable_answers"), list) or not data["acceptable_answers"]:
        data["acceptable_answers"] = [str(data["canonical_answer"]).lower()]
    data.setdefault("id", "q_0000")
    data.setdefault("category", "potpourri")
    data.setdefault("difficulty_tier", 2)
    data.setdefault("reveal_color", "")
    return data


def lily_valid_choices(question: Optional[dict]) -> bool:
    """Multiple-choice shape check (pure): exactly 4 non-empty, mutually
    distinct options with the canonical answer among them (normalized
    equality — the generation prompt demands verbatim inclusion)."""
    if not isinstance(question, dict):
        return False
    choices = question.get("choices")
    if not isinstance(choices, list) or len(choices) != 4:
        return False
    texts = [str(c).strip() for c in choices]
    if any(not t for t in texts):
        return False
    norms = [lily_evaluation.lily_normalize_answer(t) for t in texts]
    if len(set(norms)) != 4:
        return False
    canon = lily_evaluation.lily_normalize_answer(
        str(question.get("canonical_answer", ""))
    )
    return bool(canon) and canon in norms


def lily_parse_question_json(raw: str) -> Optional[dict]:
    """DEFENSIVE LAST RESORT parser (fence-strip + shape-check).

    Since the P1 structured-output fix, generation runs with
    response_mime_type + response_schema and is parsed by a plain
    json.loads first; this path only runs if schema-mode output somehow
    fails a direct parse. It must never again be the primary parser."""
    try:
        data = json.loads(_strip_fences(raw))
    except (json.JSONDecodeError, ValueError):
        return None
    return _shape_question(data)


class LilyReasoning:
    """The background reasoning node. Never speaks; writes enriched state
    (prefetched questions, failure notes) for the vocal node to perform."""

    def __init__(self, api_key: Optional[str] = None) -> None:
        # Own client — separate HTTP transport from the vocal node's
        # livekit-plugins-google client (spec §11.5).
        self._client = google_genai.Client(
            api_key=api_key or lily_config.google_api_key()
        )
        self._model = lily_config.reasoning_model()
        self._vocal_model = lily_config.vocal_model()

    async def _generate(
        self,
        model: str,
        prompt: str,
        thinking_level: str,
        system_instruction: Optional[str] = None,
        response_mime_type: Optional[str] = None,
        response_schema: Optional[genai_types.Schema] = None,
        max_output_tokens: Optional[int] = None,
    ) -> str:
        config = genai_types.GenerateContentConfig(
            # Default sampling params — never override temperature/top_p/top_k
            # on Gemini 3.x (spec §4.4).
            thinking_config=genai_types.ThinkingConfig(
                thinking_level=thinking_level
            ),
            safety_settings=_SAFETY_SETTINGS,
            # P1 root cause (2026-07-14 19:27 logs): on Gemini 3.x thinking
            # tokens count toward max_output_tokens. The shared 800-token
            # vocal budget let 3.1-pro's thinking starve the JSON body into
            # truncation — every call here now names its own budget.
            max_output_tokens=(
                max_output_tokens
                if max_output_tokens is not None
                else lily_config.vocal_max_output_tokens()
            ),
            system_instruction=system_instruction,
            response_mime_type=response_mime_type,
            response_schema=response_schema,
        )
        response = await asyncio.to_thread(
            self._client.models.generate_content,
            model=model,
            contents=prompt,
            config=config,
        )
        text = getattr(response, "text", None)
        if not text:
            # Empty candidate is a loggable event, never silence (§11.1).
            logger.warning(
                "LILY_REASONING | EMPTY_CANDIDATE | model=%s — retrying once",
                model,
            )
            response = await asyncio.to_thread(
                self._client.models.generate_content,
                model=model,
                contents=prompt,
                config=config,
            )
            text = getattr(response, "text", None)
        if not text:
            raise RuntimeError(f"empty candidate from {model} after retry")
        return text

    async def _generate_grok_json(
        self,
        prompt: str,
        *,
        system_instruction: Optional[str] = None,
        max_tokens: int,
    ) -> str:
        """ADULT-deck generation transport (owner directive 2026-08-06):
        xAI Grok chat completions in JSON mode. Gemini's non-overridable
        PROHIBITED_CONTENT filter refuses adult-deck material on both the
        generation AND verification legs, so adult questions ride Grok —
        the fleet's established adult-content provider (vision + adult
        imagegen already use XAI_API_KEY). Same honest-failure contract
        as _generate: raises on any failure; the prefetch wrapper turns
        that into a status note + bank fallback, never silence."""
        key = lily_config.xai_api_key()
        if not key:
            raise RuntimeError(
                "XAI_API_KEY missing — adult-deck generation unavailable"
            )
        model = lily_config.adult_reasoning_model()
        effort = lily_config.adult_reasoning_effort()
        # xAI's multi-agent tier (grok-*-multi-agent) does NOT support the
        # Chat Completions endpoint (HTTP 400 "Multi Agent requests are not
        # allowed on chat completions") and rejects `max_tokens`; it speaks
        # the Responses API only. Route by model id so a slot-secret swap to
        # the heavy tier works instead of 400ing. Everything else keeps the
        # chat-completions path (grok-4.2 / grok-4.5 base tiers).
        if _lily_is_multi_agent_model(model):
            endpoint = "https://api.x.ai/v1/responses"
            # System instruction rides `input` as a system-role turn; the
            # multi-agent model has no response_format, so the JSON contract
            # is prompt-enforced (fences stripped on the way out).
            supply = []
            if system_instruction:
                supply.append({"role": "system", "content": system_instruction})
            supply.append({
                "role": "user",
                "content": prompt + _MULTI_AGENT_JSON_DIRECTIVE,
            })
            body = {"model": model, "input": supply}
            if effort:
                body["reasoning"] = {"effort": effort}
        else:
            endpoint = "https://api.x.ai/v1/chat/completions"
            messages = []
            if system_instruction:
                messages.append({"role": "system", "content": system_instruction})
            messages.append({"role": "user", "content": prompt})
            body = {
                "model": model,
                "messages": messages,
                "response_format": {"type": "json_object"},
                "max_tokens": max_tokens,
            }
            if effort:
                body["reasoning_effort"] = effort
        async with aiohttp.ClientSession() as http:
            async with http.post(
                endpoint,
                json=body,
                headers={"Authorization": f"Bearer {key}"},
                timeout=aiohttp.ClientTimeout(total=PREFETCH_TIMEOUT_SECONDS),
            ) as resp:
                if resp.status != 200:
                    err = await resp.text()
                    raise RuntimeError(
                        f"xAI adult generation {resp.status}: {err[:300]}"
                    )
                data = await resp.json()
        text = (
            _lily_extract_responses_text(data)
            if _lily_is_multi_agent_model(model)
            else _lily_extract_chat_text(data)
        )
        if not (text or "").strip():
            raise RuntimeError(f"empty candidate from {model}")
        logger.info(
            "LILY_REASONING | ADULT_GROK_GENERATION | model=%s effort=%s "
            "transport=%s chars=%d",
            model, effort or "-",
            "responses" if _lily_is_multi_agent_model(model) else "chat",
            len(text),
        )
        return text

    async def approve_entity_image(
        self,
        image_bytes: bytes,
        content_type: str,
        entity: str,
    ) -> tuple[bool, str]:
        """Content gate for web-sourced images (OR amendment W1): before a
        fetched image is CACHED it must be approved image-vs-question by
        the reasoning node — the Exa retrieval path has no moderation of
        its own (generation does), and one bad cached image serves
        forever. FAIL CLOSED: any error, timeout, or unparseable verdict
        rejects (the round degrades to text-only, which is always safe).

        Judge-never-invents discipline applies: the model judges only
        whether THIS image plausibly shows the named entity and is
        appropriate — it supplies no facts of its own."""
        reason = "unknown"
        try:
            config = genai_types.GenerateContentConfig(
                thinking_config=genai_types.ThinkingConfig(
                    thinking_level="low"
                ),
                safety_settings=_SAFETY_SETTINGS,
                max_output_tokens=lily_config.judge_max_output_tokens(),
                response_mime_type="application/json",
                response_schema=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "approved": genai_types.Schema(
                            type=genai_types.Type.BOOLEAN
                        ),
                        "reason": genai_types.Schema(
                            type=genai_types.Type.STRING
                        ),
                    },
                    required=["approved", "reason"],
                ),
            )
            mime = (
                content_type.split(";", 1)[0].strip().lower()
                or "image/jpeg"
            )
            prompt = (
                "You are a strict content gate for a family-friendly trivia "
                "game played on a shared screen. Approve this photograph "
                f"ONLY if BOTH hold: (1) it plausibly depicts {entity!r} — "
                "the actual subject, not a map, diagram, logo, screenshot "
                "of text, or unrelated scene; (2) it is appropriate for a "
                "general audience (no nudity, gore, violence, or shock "
                "content). When unsure, reject. Answer in the JSON schema."
            )
            response = await asyncio.wait_for(
                asyncio.to_thread(
                    self._client.models.generate_content,
                    model=self._model,
                    contents=[
                        genai_types.Part.from_bytes(
                            data=image_bytes, mime_type=mime
                        ),
                        prompt,
                    ],
                    config=config,
                ),
                timeout=20.0,
            )
            verdict = json.loads(getattr(response, "text", "") or "{}")
            approved = bool(verdict.get("approved"))
            reason = str(verdict.get("reason", ""))[:300]
            logger.log(
                logging.INFO if approved else logging.WARNING,
                "LILY_REASONING | IMAGE_CONTENT_GATE | entity=%r "
                "approved=%s reason=%r", entity, approved, reason,
            )
            return approved, reason
        except Exception as e:
            logger.warning(
                "LILY_REASONING | IMAGE_CONTENT_GATE | entity=%r "
                "approved=False (fail closed) error_class=%s error=%s",
                entity, type(e).__name__, e,
            )
            return False, f"gate error ({type(e).__name__}): {e}"

    # -- question prefetch + verification -----------------------------------

    async def generate_question(
        self,
        category: str,
        difficulty_tier: int,
        mode: str,
        avoid_questions: list[str],
        multiple_choice: bool = False,
        avoid_answers: Optional[list] = None,
    ) -> Optional[dict]:
        avoid_block = "\n".join(f"- {q}" for q in avoid_questions[-20:]) or "- (none yet)"
        prompt = _GENERATION_PROMPT.format(
            category=category,
            difficulty_tier=difficulty_tier,
            mode=mode,
            avoid_block=avoid_block,
        )
        # Answer-level no-repeat (migration 017): this group has already
        # played these facts — a reworded question with the same answer is
        # still a repeat. The curation gate enforces it; this steers the
        # generator away from wasting a draw.
        if avoid_answers:
            recent = [str(a) for a in avoid_answers if a][-30:]
            if recent:
                prompt += (
                    "\n\nNEVER write a question whose answer is any of "
                    "these (this table has already played them): "
                    + "; ".join(recent)
                )
        if multiple_choice:
            prompt += _MC_CHOICES_ADDENDUM
        # Current-events sourcing at prefetch (WO-LILY-OMNIBUS-002 K):
        # Tavily brief on the reasoning node only; failure or a missing
        # key just means evergreen model knowledge (never blocks).
        if _CURRENT_EVENTS_RE.search(category or "") and lily_config.tavily_api_key():
            try:
                brief = await lily_search.lily_current_events_brief(category)
            except Exception as e:
                logger.warning("LILY_REASONING | CURRENT_EVENTS_BRIEF failed: %s", e)
                brief = None
            if brief:
                prompt += (
                    "\n\nFRESH WEB FACTS (sourced by the reasoning node at "
                    "prefetch — ground the question in one of these, and only "
                    "in what they actually say):\n" + brief
                )
        if mode == "adult":
            # Owner directive 2026-08-06: adult questions generate on Grok
            # (Gemini's hard filter refuses the material). JSON mode has no
            # server-side schema — the shape addendum pins the fields and
            # the defensive parser below covers drift.
            raw = await self._generate_grok_json(
                prompt + _GROK_QUESTION_SHAPE_ADDENDUM,
                max_tokens=lily_config.reasoning_max_output_tokens(),
            )
        else:
            raw = await self._generate(
                self._model,
                prompt,
                REASONING_THINKING_LEVEL,
                response_mime_type="application/json",
                response_schema=_QUESTION_RESPONSE_SCHEMA,
                max_output_tokens=lily_config.reasoning_max_output_tokens(),
            )
        # Schema mode: the output IS the JSON document — parse it directly.
        parsed: Optional[dict] = None
        try:
            parsed = _shape_question(json.loads(raw))
        except (json.JSONDecodeError, ValueError, TypeError):
            parsed = None
        if parsed is None:
            # Defensive last resort only — schema-mode output should never
            # need fence stripping.
            parsed = lily_parse_question_json(raw)
            if parsed is not None:
                logger.warning(
                    "LILY_REASONING | QUESTION_PARSE_FALLBACK | schema-mode "
                    "output needed the defensive parser | raw_prefix=%r",
                    (raw or "")[:200],
                )
        if parsed is None:
            logger.warning(
                "LILY_REASONING | QUESTION_PARSE_FAILED | raw_prefix=%r",
                (raw or "")[:400],
            )
        return parsed

    async def verify_question(
        self, question: dict, mode: str = "general"
    ) -> tuple[bool, str]:
        """Verification at prefetch time on the 3.1 Pro node — or on Grok
        for the adult deck (Gemini's hard filter refuses to even VERIFY
        adult material; a refused verification would reject every good
        adult question)."""
        prompt = _VERIFICATION_PROMPT.format(
            question_json=json.dumps(question, ensure_ascii=False)
        )
        # Web-grounded verification (WO-LILY-OMNIBUS-002 K): one bounded
        # Tavily fact block, reasoning node only, prefetch-time only.
        # Failure or a missing key means model-knowledge verification —
        # exactly the pre-WO behavior.
        if lily_config.tavily_api_key():
            try:
                web_context = await lily_search.lily_web_verification_context(
                    question.get("prompt", ""),
                    str(question.get("canonical_answer", "")),
                )
            except Exception as e:
                logger.warning(
                    "LILY_REASONING | VERIFY_WEB_CONTEXT failed: %s", e
                )
                web_context = None
            if web_context:
                prompt += (
                    "\n\nWEB CONTEXT (fetched by the reasoning node — use it "
                    "to check the fact; distrust it if it conflicts with "
                    "strong knowledge):\n" + web_context
                )
        if mode == "adult":
            raw = await self._generate_grok_json(
                prompt + _GROK_VERDICT_SHAPE_ADDENDUM,
                max_tokens=lily_config.reasoning_max_output_tokens(),
            )
        else:
            raw = await self._generate(
                self._model,
                prompt,
                REASONING_THINKING_LEVEL,
                response_mime_type="application/json",
                response_schema=_VERIFICATION_RESPONSE_SCHEMA,
                max_output_tokens=lily_config.reasoning_max_output_tokens(),
            )
        # Schema mode: direct parse first; fence stripping is a defensive
        # last resort. Honest failure stays intact — an unparseable verdict
        # fails verification, never passes silently.
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError, TypeError):
            try:
                data = json.loads(_strip_fences(raw))
            except (json.JSONDecodeError, ValueError):
                logger.warning(
                    "LILY_REASONING | VERIFY_PARSE_FAILED | raw_prefix=%r",
                    (raw or "")[:400],
                )
                return False, "verifier returned unparseable output"
        if not isinstance(data, dict):
            logger.warning(
                "LILY_REASONING | VERIFY_PARSE_FAILED | non-object verdict | "
                "raw_prefix=%r", (raw or "")[:400],
            )
            return False, "verifier returned unparseable output"
        if data.get("verdict") == "pass":
            return True, data.get("reason") or "verified"
        corrected = data.get("corrected_canonical_answer")
        if corrected:
            question["canonical_answer"] = corrected
            question["acceptable_answers"] = [str(corrected).lower()]
            return True, f"corrected: {data.get('reason', '')}"
        return False, data.get("reason") or "failed verification"

    # -- multiple-choice supply (sub-agent G) --------------------------------

    async def ensure_choices(self, question: Optional[dict]) -> None:
        """Attach a valid 4-option `choices` array to a question that lacks
        one: 3 synthesized distractors (reasoning node) + the canonical
        answer, order randomized. No-op when choices are already valid.
        On synthesis failure the question DEGRADES to freeform (choices
        removed) — an MC round with an open question beats a dead prefetch."""
        if not isinstance(question, dict):
            return
        if lily_valid_choices(question):
            return
        canonical = str(question.get("canonical_answer", "")).strip()
        if not canonical or not question.get("prompt"):
            question.pop("choices", None)
            return
        distractors: list[str] = []
        try:
            raw = await self._generate(
                self._model,
                _DISTRACTOR_PROMPT.format(
                    prompt=question.get("prompt", ""),
                    canonical_answer=canonical,
                ),
                REASONING_THINKING_LEVEL,
                response_mime_type="application/json",
                response_schema=_DISTRACTOR_RESPONSE_SCHEMA,
                max_output_tokens=lily_config.reasoning_max_output_tokens(),
            )
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, ValueError, TypeError):
                data = json.loads(_strip_fences(raw))
            canon_norm = lily_evaluation.lily_normalize_answer(canonical)
            seen = {canon_norm}
            for d in (data or {}).get("distractors", []):
                text = str(d).strip()
                norm = lily_evaluation.lily_normalize_answer(text)
                if text and norm and norm not in seen:
                    seen.add(norm)
                    distractors.append(text)
        except Exception as e:
            logger.warning(
                "LILY_REASONING | MC_SYNTHESIS_FAILED | id=%s error_class=%s error=%s",
                question.get("id"), type(e).__name__, e,
            )
        if len(distractors) < 3:
            logger.warning(
                "LILY_REASONING | MC_CHOICES_MISSING | id=%s — question "
                "degrades to freeform", question.get("id"),
            )
            question.pop("choices", None)
            return
        choices = distractors[:3] + [canonical]
        random.shuffle(choices)
        question["choices"] = choices
        logger.info(
            "LILY_REASONING | MC_CHOICES_SYNTHESIZED | id=%s", question.get("id")
        )

    async def prefetch_question(
        self,
        scorekeeper,
        category: str,
        difficulty_tier: int,
        avoid_questions: list[str],
        from_bank: Optional[dict] = None,
        multiple_choice: bool = False,
        avoid_answers: Optional[list] = None,
    ) -> Optional[dict]:
        """Prefetch the N+1 question. KB-bank questions bypass verification
        (spec §4.5); when the round runs multiple choice, bank questions
        without choices get 3 synthesized distractors here (reasoning node
        only). On failure, writes an honest status note into the state
        block (§11.2) and returns None."""
        if from_bank is not None:
            if multiple_choice:
                await self.ensure_choices(from_bank)
            scorekeeper.clear_status_notes()
            return from_bank
        try:
            question = await asyncio.wait_for(
                self.generate_question(
                    category, difficulty_tier, scorekeeper.mode,
                    avoid_questions, multiple_choice=multiple_choice,
                    avoid_answers=avoid_answers,
                ),
                timeout=PREFETCH_TIMEOUT_SECONDS,
            )
            if question is None:
                raise RuntimeError("question generation returned unparseable JSON")
            pre_verify_answer = str(question.get("canonical_answer", ""))
            ok, reason = await asyncio.wait_for(
                self.verify_question(question, mode=scorekeeper.mode),
                timeout=PREFETCH_TIMEOUT_SECONDS,
            )
            if not ok:
                raise RuntimeError(f"verification failed: {reason}")
            if multiple_choice and not lily_valid_choices(question):
                # A verification correction may have moved the canonical
                # answer out from under an otherwise-good set: swap the
                # stale entry in place (order stays randomized).
                choices = question.get("choices")
                corrected = str(question.get("canonical_answer", ""))
                if (
                    isinstance(choices, list) and len(choices) == 4
                    and corrected != pre_verify_answer
                ):
                    old_norm = lily_evaluation.lily_normalize_answer(
                        pre_verify_answer
                    )
                    for i, c in enumerate(choices):
                        if lily_evaluation.lily_normalize_answer(str(c)) == old_norm:
                            choices[i] = corrected
                            break
                # Still invalid (missing / malformed / unswappable):
                # synthesize distractors; failure degrades to freeform.
                await asyncio.wait_for(
                    self.ensure_choices(question),
                    timeout=PREFETCH_TIMEOUT_SECONDS,
                )
            scorekeeper.clear_status_notes()
            logger.info(
                "LILY_REASONING | PREFETCH_OK | id=%s category=%s tier=%s",
                question.get("id"), question.get("category"),
                question.get("difficulty_tier"),
            )
            return question
        except (asyncio.TimeoutError, RuntimeError, Exception) as e:
            logger.error(
                "LILY_REASONING | PREFETCH_FAILED | error_class=%s error=%s",
                type(e).__name__, e,
            )
            scorekeeper.set_status_note(
                "question machine failure: the next question did not arrive — "
                "tell the table honestly and vamp; do not invent an explanation"
            )
            return None

    # -- picture-question supply (WO-LILY-OMNIBUS-002 H/I/J) ------------------

    async def generate_demo_image(
        self,
        supabase,
        *,
        session_id: str,
        adult: bool = False,
        intensity: str = "suggestive",
    ) -> Optional[str]:
        """Self-knowledge WO "show me" demo (12:47 live fixture): ONE
        generated tabletop image so a skeptic asking to SEE picture
        rounds gets shown — through this, the one legal image seam.
        Cache-first via the shared no-silent-crash wrapper (a session's
        demo generates at most once per deck); returns a public bucket
        URL or None. Never raises.

        adult=True: sample rides the adult deck (Grok + lily_adult_style
        chokepoint). intensity is the player-chosen heat
        (suggestive|explicit); style is applied inside generation."""
        if adult:
            prompt = (
                "A grown-up cocktail-lounge trivia scene: a dimly lit "
                "speakeasy table, martini glasses, playing cards and a "
                "smoldering-glance couple leaning close mid-question, "
                "flirtatious grown-up energy, permissive wear"
            )
            question_id = f"demo_adult_{session_id}"
            return await lily_imagegen.lily_generate_question_image(
                supabase,
                session_id=session_id,
                question_id=question_id,
                prompt=prompt,
                aspect_ratio="16:9",
                mode="adult",
                intensity=intensity,
            )
        prompt = (
            "A warm, playful pub-trivia tabletop scene: a wooden "
            "table with scattered answer cards, a chalkboard "
            "scoreboard, soft rose-colored lighting, no text"
        )
        question_id = f"demo_{session_id}"
        return await lily_imagegen.lily_generate_question_image(
            supabase,
            session_id=session_id,
            question_id=question_id,
            prompt=prompt,
            aspect_ratio="16:9",
        )

    async def prefetch_picture_question(
        self,
        supabase,
        *,
        kind: str,
        question_index: int,
        session_id: str,
        mode: str = "general",
        intensity: str = "suggestive",
        exclude_ids: Optional[set] = None,
        exclude_hashes: Optional[set] = None,
    ) -> Optional[dict]:
        """Picture-question supply at prefetch, reasoning-node-side by
        design (web tools + image generation stay off the vocal path; the
        agent layer only decides WHICH slots are picture slots and only in
        media_mode='pictures').

        kind: 'real_or_imagined' (reference round, sub-agent J — generated
        plausible fake vs Exa-sourced real photo) or 'real_entity' ("name
        this landmark", sub-agent I — web-sourced ONLY, never generated).

        Returns the §4.2 question shape with image attached, or None on
        ANY failure — the caller falls back to the standard text supply
        (text-only fallback). Adult mode supplies picture rounds exactly
        like the other decks; generated images route to the Grok adult
        image model (mode + intensity threaded to the builders below)."""
        if supabase is None:
            return None
        # Picture builders use deterministic ids for a slot. Honour the
        # shared supply interface's id exclusion before doing expensive web
        # or image-generation work. Prompt hashes are intentionally not used
        # here: picture formats reuse a small set of spoken templates, so a
        # hash exclusion would disable otherwise distinct images.
        excluded = {str(value) for value in (exclude_ids or set())}
        candidate_id = (
            f"roi_{question_index:04d}"
            if kind == "real_or_imagined"
            else f"pic_{question_index:04d}"
            if kind == "real_entity"
            else None
        )
        if candidate_id and candidate_id in excluded:
            logger.info(
                "LILY_REASONING | PICTURE_PREFETCH_SKIPPED | id=%s "
                "reason=asked_history",
                candidate_id,
            )
            return None
        try:
            if kind == "real_or_imagined":
                return await lily_imagegen.lily_build_real_or_imagined_question(
                    supabase, index=question_index, session_id=session_id,
                    approve=self.approve_entity_image, mode=mode,
                    intensity=intensity,
                )
            if kind == "real_entity":
                return await lily_search.lily_build_real_entity_picture_question(
                    supabase, index=question_index, session_id=session_id,
                    approve=self.approve_entity_image,
                )
            logger.warning(
                "LILY_REASONING | PICTURE_PREFETCH | unknown kind=%r", kind
            )
            return None
        except Exception as e:
            # Builders are no-silent-crash themselves; this is the last-
            # resort belt so picture supply can never break text supply.
            logger.error(
                "LILY_REASONING | PICTURE_PREFETCH_FAILED | kind=%s "
                "error_class=%s error=%s", kind, type(e).__name__, e,
            )
            return None

    # -- Tier-2 judge transport (contract in lily_evaluation) ----------------

    async def judge(self, system_instructions: str, user_prompt: str) -> str:
        """One non-spoken LLM turn on the vocal model for Tier-2
        adjudication. Returns the raw model text (parsed by
        lily_evaluation.lily_parse_judge_response). Hard-bounded: this
        call sits inside adjudicate, where an unbounded hang wedges
        _adjudicating=True for the rest of the session (live 2026-07-15
        04:05 stall class) — the caller treats TimeoutError as
        judge-unavailable and rules on Tier-1 alone."""
        return await asyncio.wait_for(
            self._generate(
                self._vocal_model,
                user_prompt,
                JUDGE_THINKING_LEVEL,
                system_instruction=system_instructions,
                max_output_tokens=lily_config.judge_max_output_tokens(),
            ),
            timeout=12.0,
        )
