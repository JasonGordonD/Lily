"""
lily_evaluation.py — two-tier answer adjudication for LILY.

Tier 1 — fast path, no LLM (pure, importable anywhere):
  normalize the attributed transcript and fuzzy/phonetic-match it against
  acceptable_answers. Clean answers score instantly. The threshold is tuned
  CONSERVATIVE: uncertainty escalates to Tier 2 rather than rejecting —
  Tier 1 never returns "incorrect".

Tier 2 — LLM judge call contract (prompt build + response parse, pure):
  ambiguous / partial / hedged / STT-mangled answers go to one LLM turn
  carrying the question, the canonical answer, and the ordered
  (speaker, text) attempts. Output contract:
    verdict (correct|incorrect|partial), normalized_answer, one-line reason.
  The judge evaluates against the supplied canonical answer only — it never
  re-derives or invents the fact at judge-time.

Order is pre-decided by scorekeeper timestamps in both tiers.
"""

import json
import logging
import re
from difflib import SequenceMatcher
from typing import Optional

logger = logging.getLogger("lily_evaluation")

# Conservative Tier-1 thresholds ([CC TUNE] — uncertainty escalates)
FUZZY_CORRECT_THRESHOLD = 0.88
PHONETIC_FUZZY_THRESHOLD = 0.75

_ARTICLES = {"the", "a", "an"}

# Hedge/filler prefixes stripped repeatedly from the front of an attempt.
_FILLER_PREFIXES = (
    "the answer is",
    "answer is",
    "i think it is",
    "i think it's",
    "i think its",
    "i think",
    "i want to say",
    "i'm gonna say",
    "im gonna say",
    "i'll say",
    "is it",
    "it is",
    "it's",
    "its",
    "that is",
    "that's",
    "thats",
    "maybe",
    "probably",
    "definitely",
    "oh",
    "um",
    "uh",
    "wait",
    "no wait",
    "ooh",
)

_PUNCT_RE = re.compile(r"[^a-z0-9\s]")


def lily_normalize_answer(text: str) -> str:
    """Tier-1 normalization: lowercase, strip punctuation, strip hedge
    prefixes, drop articles, collapse whitespace."""
    if not text:
        return ""
    lowered = text.lower().strip()
    lowered = _PUNCT_RE.sub(" ", lowered)
    lowered = re.sub(r"\s+", " ", lowered).strip()

    changed = True
    while changed:
        changed = False
        for prefix in _FILLER_PREFIXES:
            norm_prefix = _PUNCT_RE.sub(" ", prefix)
            norm_prefix = re.sub(r"\s+", " ", norm_prefix).strip()
            if lowered == norm_prefix:
                # The whole utterance is filler — nothing left to match.
                return ""
            if lowered.startswith(norm_prefix + " "):
                lowered = lowered[len(norm_prefix) + 1:]
                changed = True

    words = [w for w in lowered.split() if w not in _ARTICLES]
    return " ".join(words)


def _soundex(word: str) -> str:
    """Soundex-style phonetic key. Unlike classic Soundex, the FIRST letter
    is also encoded by its consonant group, so STT manglings that swap
    homophonic initials ("Kanberra"/"Canberra") still key identically."""
    word = re.sub(r"[^a-z]", "", word.lower())
    if not word:
        return ""
    codes = {
        "b": "1", "f": "1", "p": "1", "v": "1",
        "c": "2", "g": "2", "j": "2", "k": "2", "q": "2",
        "s": "2", "x": "2", "z": "2",
        "d": "3", "t": "3",
        "l": "4",
        "m": "5", "n": "5",
        "r": "6",
    }
    first = codes.get(word[0], word[0])
    encoded = []
    prev = codes.get(word[0], "")
    for ch in word[1:]:
        code = codes.get(ch, "")
        if code and code != prev:
            encoded.append(code)
        if ch not in ("h", "w"):
            prev = code
    return (first + "".join(encoded) + "000")[:4]


def _phrase_soundex(phrase: str) -> str:
    return " ".join(_soundex(w) for w in phrase.split() if w)


def _contains_phrase(haystack: str, needle: str) -> bool:
    """Word-boundary containment: 'canberra australia' contains 'canberra'."""
    if not needle:
        return False
    return re.search(rf"(?:^|\s){re.escape(needle)}(?:\s|$)", haystack) is not None


def lily_tier1_evaluate(
    transcript_text: str,
    acceptable_answers: list[str],
) -> dict:
    """
    Tier-1 evaluation of one attempt against acceptable_answers.

    Returns:
      {
        "verdict": "correct" | "uncertain",
        "matched_answer": str | None,
        "method": "exact" | "containment" | "fuzzy" | "phonetic" | None,
        "similarity": float,
      }

    "uncertain" means escalate to the Tier-2 judge — Tier 1 never rejects.
    """
    attempt = lily_normalize_answer(transcript_text)
    best_sim = 0.0
    best_answer = None

    if not attempt or not acceptable_answers:
        return {
            "verdict": "uncertain",
            "matched_answer": None,
            "method": None,
            "similarity": 0.0,
        }

    for raw_answer in acceptable_answers:
        answer = lily_normalize_answer(raw_answer)
        if not answer:
            continue

        # Exact normalized match
        if attempt == answer:
            return {
                "verdict": "correct",
                "matched_answer": raw_answer,
                "method": "exact",
                "similarity": 1.0,
            }

        # Whole-word containment ("uh Canberra, Australia" contains "canberra")
        if _contains_phrase(attempt, answer):
            return {
                "verdict": "correct",
                "matched_answer": raw_answer,
                "method": "containment",
                "similarity": 1.0,
            }

        sim = SequenceMatcher(None, attempt, answer).ratio()
        if sim > best_sim:
            best_sim = sim
            best_answer = raw_answer

        if sim >= FUZZY_CORRECT_THRESHOLD:
            return {
                "verdict": "correct",
                "matched_answer": raw_answer,
                "method": "fuzzy",
                "similarity": round(sim, 3),
            }

        # Phonetic path: soundex agreement + moderate string similarity
        if (
            _phrase_soundex(attempt) == _phrase_soundex(answer)
            and sim >= PHONETIC_FUZZY_THRESHOLD
        ):
            return {
                "verdict": "correct",
                "matched_answer": raw_answer,
                "method": "phonetic",
                "similarity": round(sim, 3),
            }

    return {
        "verdict": "uncertain",
        "matched_answer": best_answer,
        "method": None,
        "similarity": round(best_sim, 3),
    }


# ---------------------------------------------------------------------------
# Tier 2 — LLM judge call contract
# ---------------------------------------------------------------------------

LILY_JUDGE_INSTRUCTIONS = """You are the answer judge for a live trivia game.
You are given the question, the canonical answer, optional acceptable variants,
and the ordered answer attempts (earliest first).

The judge evaluates against the supplied canonical answer only — it never
re-derives or invents the fact at judge-time. Do not use outside knowledge to
decide what the true answer is; the canonical answer provided IS the truth.

Judge whether each attempt expresses the canonical answer: last names alone,
near-pronunciations, STT manglings, and the right idea in the wrong costume
all count as correct. A hedge around the right answer is still correct.
A genuinely different answer is incorrect. A meaningfully incomplete but
on-target answer is partial.

Respond with ONLY a JSON object, no markdown fences, exactly this shape:
{"verdict": "correct" | "incorrect" | "partial",
 "winner": "<player name of the earliest attempt judged correct or partial, else null>",
 "normalized_answer": "<the attempt reduced to its intended answer>",
 "reason": "<one short spoken-style line explaining the ruling>"}"""


def lily_build_judge_prompt(
    question_prompt: str,
    canonical_answer: str,
    attempts: list[tuple[str, str]],
    acceptable_answers: Optional[list[str]] = None,
    claimed_alternate: Optional[str] = None,
) -> str:
    """Build the Tier-2 judge user prompt. attempts is ordered
    (speaker, text), earliest first — order is already decided by
    scorekeeper timestamps and must not be re-litigated."""
    lines = [
        f"QUESTION: {question_prompt}",
        f"CANONICAL ANSWER: {canonical_answer}",
    ]
    if acceptable_answers:
        lines.append("ACCEPTABLE VARIANTS: " + ", ".join(acceptable_answers))
    lines.append("ATTEMPTS IN ORDER (earliest first):")
    for idx, (speaker, text) in enumerate(attempts, start=1):
        lines.append(f"{idx}. {speaker}: {text!r}")
    if claimed_alternate:
        lines.append(
            "APPEAL: the player calmly claims this alternate answer should "
            f"count: {claimed_alternate!r}. Judge the appeal against the "
            "canonical answer only."
        )
    return "\n".join(lines)


def lily_parse_judge_response(raw: str) -> Optional[dict]:
    """Parse the judge's JSON verdict. Returns None on any contract
    violation (caller escalates to an in-character honest failure)."""
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    # Tolerate stray prose around the JSON object.
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except (json.JSONDecodeError, ValueError):
        logger.warning("LILY_JUDGE | unparseable verdict: %r", raw[:200])
        return None
    verdict = data.get("verdict")
    if verdict not in ("correct", "incorrect", "partial"):
        logger.warning("LILY_JUDGE | invalid verdict: %r", verdict)
        return None
    return {
        "verdict": verdict,
        "winner": data.get("winner"),
        "normalized_answer": data.get("normalized_answer") or "",
        "reason": data.get("reason") or "",
    }
