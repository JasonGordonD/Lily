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
import re
from typing import Optional

from google import genai as google_genai
from google.genai import types as genai_types

import lily_config

logger = logging.getLogger("lily_reasoning")

REASONING_THINKING_LEVEL = "medium"  # spec §4.4: thinking_level, never thinking_budget
JUDGE_THINKING_LEVEL = "low"
PREFETCH_TIMEOUT_SECONDS = 30.0

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


def _strip_fences(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    start, end = t.find("{"), t.rfind("}")
    if start != -1 and end > start:
        t = t[start:end + 1]
    return t


def lily_parse_question_json(raw: str) -> Optional[dict]:
    """Parse and shape-check the structured question JSON (spec §4.2)."""
    try:
        data = json.loads(_strip_fences(raw))
    except (json.JSONDecodeError, ValueError):
        return None
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
    ) -> str:
        config = genai_types.GenerateContentConfig(
            # Default sampling params — never override temperature/top_p/top_k
            # on Gemini 3.x (spec §4.4).
            thinking_config=genai_types.ThinkingConfig(
                thinking_level=thinking_level
            ),
            safety_settings=_SAFETY_SETTINGS,
            max_output_tokens=lily_config.vocal_max_output_tokens(),
            system_instruction=system_instruction,
            response_mime_type=response_mime_type,
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

    # -- question prefetch + verification -----------------------------------

    async def generate_question(
        self,
        category: str,
        difficulty_tier: int,
        mode: str,
        avoid_questions: list[str],
    ) -> Optional[dict]:
        avoid_block = "\n".join(f"- {q}" for q in avoid_questions[-20:]) or "- (none yet)"
        prompt = _GENERATION_PROMPT.format(
            category=category,
            difficulty_tier=difficulty_tier,
            mode=mode,
            avoid_block=avoid_block,
        )
        raw = await self._generate(
            self._model,
            prompt,
            REASONING_THINKING_LEVEL,
            response_mime_type="application/json",
        )
        parsed = lily_parse_question_json(raw)
        if parsed is None:
            logger.warning(
                "LILY_REASONING | QUESTION_PARSE_FAILED | raw_prefix=%r",
                (raw or "")[:400],
            )
        return parsed

    async def verify_question(self, question: dict) -> tuple[bool, str]:
        """Verification at prefetch time on the 3.1 Pro node."""
        prompt = _VERIFICATION_PROMPT.format(
            question_json=json.dumps(question, ensure_ascii=False)
        )
        raw = await self._generate(
            self._model,
            prompt,
            REASONING_THINKING_LEVEL,
            response_mime_type="application/json",
        )
        try:
            data = json.loads(_strip_fences(raw))
        except (json.JSONDecodeError, ValueError):
            logger.warning(
                "LILY_REASONING | VERIFY_PARSE_FAILED | raw_prefix=%r",
                (raw or "")[:400],
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

    async def prefetch_question(
        self,
        scorekeeper,
        category: str,
        difficulty_tier: int,
        avoid_questions: list[str],
        from_bank: Optional[dict] = None,
    ) -> Optional[dict]:
        """Prefetch the N+1 question. KB-bank questions bypass verification
        (spec §4.5). On failure, writes an honest status note into the
        state block (§11.2) and returns None."""
        if from_bank is not None:
            scorekeeper.clear_status_notes()
            return from_bank
        try:
            question = await asyncio.wait_for(
                self.generate_question(
                    category, difficulty_tier, scorekeeper.mode, avoid_questions
                ),
                timeout=PREFETCH_TIMEOUT_SECONDS,
            )
            if question is None:
                raise RuntimeError("question generation returned unparseable JSON")
            ok, reason = await asyncio.wait_for(
                self.verify_question(question),
                timeout=PREFETCH_TIMEOUT_SECONDS,
            )
            if not ok:
                raise RuntimeError(f"verification failed: {reason}")
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

    # -- Tier-2 judge transport (contract in lily_evaluation) ----------------

    async def judge(self, system_instructions: str, user_prompt: str) -> str:
        """One non-spoken LLM turn on the vocal model for Tier-2
        adjudication. Returns the raw model text (parsed by
        lily_evaluation.lily_parse_judge_response)."""
        return await self._generate(
            self._vocal_model,
            user_prompt,
            JUDGE_THINKING_LEVEL,
            system_instruction=system_instructions,
        )
