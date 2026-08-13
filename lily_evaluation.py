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
import random
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


def _strip_fillers(text: str) -> str:
    """Lowercase, strip punctuation, and repeatedly strip hedge/filler
    prefixes — WITHOUT dropping articles (the multiple-choice letter parser
    needs a bare 'a' to survive). Returns "" for pure-filler utterances."""
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
    return lowered


def lily_normalize_answer(text: str) -> str:
    """Tier-1 normalization: lowercase, strip punctuation, strip hedge
    prefixes, drop articles, collapse whitespace."""
    lowered = _strip_fillers(text)
    if not lowered:
        return ""
    words = [w for w in lowered.split() if w not in _ARTICLES]
    return " ".join(words)


# Spoken/prompt overlap ratio — TELEMETRY ONLY since the desync WO
# (WO-LILY-DESYNC-HONESTY-001 Sub-agent B). The tiers below used to open
# the answer window; live evidence (ratios 0.00–0.15 on questions the
# table demonstrably heard, and fallback windows opened against turns
# that carried no question at all) demoted the matcher: delivery
# registration is STRUCTURAL (the q_{N}_delivery claim), and the ratio
# is logged as `LILY_WINDOW | RATIO | … telemetry` and acted on by
# nothing. The constants stay for log continuity and analysis.
QUESTION_SPOKEN_VERBATIM_RATIO = 0.6
QUESTION_SPOKEN_PARAPHRASE_RATIO = 0.3
QUESTION_SPOKEN_MIN_HITS = 2


def lily_question_spoken_ratio(
    question_prompt: str,
    spoken_text: str,
    min_hits: int = QUESTION_SPOKEN_MIN_HITS,
) -> float:
    """How much of the armed question did this agent turn actually perform?
    Fraction of the prompt's distinctive tokens (len > 3) present in the
    spoken text, 0.0..1.0 — but 0.0 when fewer than `min_hits` tokens
    matched (single incidental hits never count as evidence). Pure —
    TELEMETRY ONLY (desync WO Sub-agent B): logged per outbound turn while
    a question is armed, never used to open the window or register
    delivery."""
    if not question_prompt or not spoken_text:
        return 0.0
    strip = lambda s: re.sub(r"[^a-z0-9\s]", " ", s.lower())
    q_tokens = [t for t in strip(question_prompt).split() if len(t) > 3]
    if not q_tokens:
        return 0.0
    spoken = set(strip(spoken_text).split())
    hits = sum(1 for t in q_tokens if t in spoken)
    if hits < min(min_hits, len(q_tokens)):
        return 0.0
    return hits / len(q_tokens)


# Structural delivery detection (desync WO Sub-agent B): the organic-turn
# claim mechanism. A turn PRESENTS the armed question when it contains the
# question's core answer-bearing sentence as written — flourish before and
# after, never inside (the prompt states that contract; this detects
# compliance). Used ONLY to decide the q_{N}_delivery CLAIM for organic
# LLM turns; every downstream action (window open, delivered marking) keys
# off the claim event, never off text similarity.

# Bracketed audio tags ([excited], [pause], …) and SSML-ish <break/> tags
# are TTS controls, not words — stripped before containment so a tag
# before/after the sentence can never break the match. (A tag INSIDE the
# core sentence breaks containment by design: the contract is the sentence
# lands whole.)
_TTS_TAG_RE = re.compile(r"\[[^\]]*\]|<[^>]*>")


def _presentation_normalize(text: str) -> str:
    """Lowercase, tag-strip, punctuation-strip, whitespace-collapse — the
    canonical form for core-sentence containment."""
    t = _TTS_TAG_RE.sub(" ", text or "")
    t = re.sub(r"[^a-z0-9\s]", " ", t.lower())
    return " ".join(t.split())


def lily_question_core_sentence(question_prompt: str) -> str:
    """The armed question's core answer-bearing sentence: the LAST sentence
    of the prompt (house style puts the thing being asked for at the very
    end, so everyone knows the moment to jump in). Single-sentence prompts
    return whole. Pure."""
    prompt = (question_prompt or "").strip()
    if not prompt:
        return ""
    sentences = [
        s.strip() for s in re.split(r"(?<=[.?!])\s+", prompt) if s.strip()
    ]
    return sentences[-1] if sentences else prompt


def lily_turn_presents_question(
    question_prompt: str, spoken_text: str
) -> bool:
    """Does this outbound agent turn PERFORM the armed question — its core
    sentence as written, contiguously, with any flourish outside it?
    Word-boundary containment on normalized text. Pure; decides the
    organic q_{N}_delivery claim in tts_node and nothing else."""
    core = _presentation_normalize(
        lily_question_core_sentence(question_prompt)
    )
    spoken = _presentation_normalize(spoken_text)
    if not core or not spoken:
        return False
    return f" {core} " in f" {spoken} "


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
    threshold: Optional[float] = None,
) -> dict:
    """
    Tier-1 evaluation of one attempt against acceptable_answers.

    threshold (WO-ADDRESSEE-H1 Task 2): the state-prior acceptance
    threshold. None keeps the pre-H1 behavior exactly
    (FUZZY_CORRECT_THRESHOLD). Every accept path is gated on its match
    similarity reaching the threshold — exact and containment matches
    carry similarity 1.0, so a threshold ABOVE 1.0 disables Tier-1
    auto-accept entirely (the OVERLAP / HOST_SPEAKING / SCORING defaults:
    everything escalates to the Tier-2 judge). The phonetic bar scales
    proportionally (PHONETIC_FUZZY_THRESHOLD × threshold/baseline).

    Returns:
      {
        "verdict": "correct" | "uncertain",
        "matched_answer": str | None,
        "method": "exact" | "containment" | "fuzzy" | "phonetic" | None,
        "similarity": float,
      }

    "uncertain" means escalate to the Tier-2 judge — Tier 1 never rejects.
    """
    t = FUZZY_CORRECT_THRESHOLD if threshold is None else threshold
    phonetic_t = PHONETIC_FUZZY_THRESHOLD * (t / FUZZY_CORRECT_THRESHOLD)
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
            if 1.0 >= t:
                return {
                    "verdict": "correct",
                    "matched_answer": raw_answer,
                    "method": "exact",
                    "similarity": 1.0,
                }
            best_sim, best_answer = 1.0, raw_answer
            continue

        # Whole-word containment ("uh Canberra, Australia" contains "canberra")
        if _contains_phrase(attempt, answer):
            if 1.0 >= t:
                return {
                    "verdict": "correct",
                    "matched_answer": raw_answer,
                    "method": "containment",
                    "similarity": 1.0,
                }
            best_sim, best_answer = 1.0, raw_answer
            continue

        sim = SequenceMatcher(None, attempt, answer).ratio()
        if sim > best_sim:
            best_sim = sim
            best_answer = raw_answer

        if sim >= t:
            return {
                "verdict": "correct",
                "matched_answer": raw_answer,
                "method": "fuzzy",
                "similarity": round(sim, 3),
            }

        # Phonetic path: soundex agreement + moderate string similarity
        if (
            t <= 1.0
            and _phrase_soundex(attempt) == _phrase_soundex(answer)
            and sim >= phonetic_t
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
# Tier-1 outcome bands (WO-ADDRESSEE-H1 Task 2 — consumed by Task 4)
#
# The state-prior threshold splits Tier-1 similarity space into three bands:
# at/above the threshold is ACCEPT territory; the MIDDLE band — within
# clarify_margin below the threshold — is where Task 4's deterministic
# clarify question fires; further below, the classification stands (REJECT
# band: act on it, write the implicit label as wired in B1). Pure — callers
# pass lily_config.tier1_clarify_margin() for the env-tunable margin.
# ---------------------------------------------------------------------------

BAND_ACCEPT = "accept"
BAND_CLARIFY = "clarify"
BAND_REJECT = "reject"


def lily_tier1_band(
    similarity: float,
    threshold: float,
    clarify_margin: float,
) -> str:
    """Which band a Tier-1 similarity lands in under the active
    state-prior threshold: BAND_ACCEPT (>= threshold), BAND_CLARIFY
    (the ambiguous middle: [threshold - clarify_margin, threshold)),
    or BAND_REJECT (below the middle band)."""
    if similarity >= threshold:
        return BAND_ACCEPT
    if similarity >= threshold - clarify_margin:
        return BAND_CLARIFY
    return BAND_REJECT


# ---------------------------------------------------------------------------
# Tier 1 — multiple-choice matching (multiple-choice WO)
#
# When the live question carries four choices, an attempt selects ONE of
# them: by letter ("B", "letter b", "option c"), by position ("the second
# one", "number three"), or by (fuzzy) option text. A resolved selection is
# DEFINITIVE — unlike the freeform matcher, MC Tier 1 may return
# "incorrect" (a clean letter pick of a wrong option is not uncertainty).
# Only unresolvable attempts (mumbles) escalate to the Tier-2 judge.
# ---------------------------------------------------------------------------

MC_CHOICE_LETTERS = "ABCD"

# HOSTLOOP-001 C3b: NATO letter spellings resolve to their letter. A table
# that has been read "A) ... B) ..." answers "bravo" as often as "B" — and
# every such pick used to land as "uncertain" (no selection resolved), which
# on the barge path meant NOT answer-shaped: the utterance never bound, the
# choices died, nothing re-aired (Session B, lily-05BB92). The spelling
# alphabet already exists in lily_binding._NATO_CANONICAL, but that copy is
# name-binding vocabulary (HOTFIX-009 W7) and is cue-gated on a NAME
# introducer, so it can never resolve a CHOICE; only the four letters an MC
# question actually offers belong here.
#
# Anchored, whole-utterance only (as with the bare letters): a resolved pick
# is the entire utterance after fillers strip. Per-choice TEXT matching still
# runs FIRST in lily_tier1_evaluate_mc, so a question whose option really is
# "Delta" resolves by choice_text, not by this spelling.
_LETTER_INDEX = {
    "a": 0, "b": 1, "c": 2, "d": 3,
    "alpha": 0, "alfa": 0, "bravo": 1, "charlie": 2, "delta": 3,
}
_LETTER_RE = re.compile(
    r"^(?:letter |option |choice )?"
    r"(alpha|alfa|bravo|charlie|delta|[abcd])$"
)
_ORDINAL_INDEX = {"first": 0, "second": 1, "third": 2, "fourth": 3, "last": 3}
_POSITIONAL_RE = re.compile(
    r"^(?:the )?(first|second|third|fourth|last)"
    r"(?: (?:one|option|choice|answer))?$"
)
_NUMBER_INDEX = {
    "one": 0, "1": 0, "two": 1, "2": 1,
    "three": 2, "3": 2, "four": 3, "4": 3,
}
_NUMBER_POSITIONAL_RE = re.compile(r"^(?:the )?number (one|two|three|four|[1-4])$")


def lily_canonical_choice_index(
    choices: list[str], canonical_answer: str
) -> Optional[int]:
    """Index of the canonical answer inside the four choices (normalized
    equality first, then a fuzzy fallback for near-verbatim drift). None
    when the answer genuinely isn't among the choices."""
    if not choices:
        return None
    canon = lily_normalize_answer(canonical_answer)
    if not canon:
        return None
    norms = [lily_normalize_answer(str(c)) for c in choices]
    for i, n in enumerate(norms):
        if n == canon:
            return i
    best_i, best_sim = None, 0.0
    for i, n in enumerate(norms):
        sim = SequenceMatcher(None, n, canon).ratio()
        if sim > best_sim:
            best_i, best_sim = i, sim
    return best_i if best_sim >= FUZZY_CORRECT_THRESHOLD else None


def lily_tier1_evaluate_mc(
    transcript_text: str,
    choices: list[str],
    canonical_answer: str,
    threshold: Optional[float] = None,
) -> dict:
    """
    Tier-1 evaluation of one attempt against a four-choice question.

    threshold (WO-ADDRESSEE-H1 Task 2) gates the OPTION-TEXT resolvers
    (they run the freeform matcher per choice); explicit letter /
    positional picks ("B", "the second one") are deterministic
    full-utterance parses and resolve regardless — a bare letter is a
    committed selection even under a raised prior.

    Returns:
      {
        "verdict": "correct" | "incorrect" | "uncertain",
        "selected_index": int | None,
        "matched_answer": str | None,   # the selected choice's text
        "method": "choice_text" | "letter" | "positional" | None,
        "similarity": float,
      }

    "uncertain" (no resolvable selection, or the canonical answer can't be
    located among the choices) escalates to the Tier-2 judge — mumbles
    stay Tier-2 territory.
    """
    selected: Optional[int] = None
    method: Optional[str] = None
    similarity = 0.0

    # Per-choice text evaluation (reuses the freeform matcher one choice at
    # a time). exact/containment selects immediately; fuzzy/phonetic hits
    # are held as the LAST resort — a letter or position is more explicit.
    fuzzy_best: Optional[int] = None
    fuzzy_sim = 0.0
    if choices:
        for i, choice in enumerate(choices):
            r = lily_tier1_evaluate(
                transcript_text, [str(choice)], threshold=threshold
            )
            if r["method"] in ("exact", "containment"):
                selected, method, similarity = i, "choice_text", r["similarity"]
                break
            if r["verdict"] == "correct" and r["similarity"] > fuzzy_sim:
                fuzzy_best, fuzzy_sim = i, r["similarity"]

    kept = _strip_fillers(transcript_text)  # articles preserved: bare "a" = A

    # Letter: "b", "letter b", "option c", "is it d".
    if selected is None and kept:
        m = _LETTER_RE.match(kept)
        if m:
            selected, method, similarity = _LETTER_INDEX[m.group(1)], "letter", 1.0

    # Positional: "the second one", "second", "number three".
    if selected is None and kept:
        m = _POSITIONAL_RE.match(kept)
        if m:
            selected, method, similarity = (
                _ORDINAL_INDEX[m.group(1)], "positional", 1.0,
            )
        else:
            m = _NUMBER_POSITIONAL_RE.match(kept)
            if m:
                selected, method, similarity = (
                    _NUMBER_INDEX[m.group(1)], "positional", 1.0,
                )

    # Fuzzy/phonetic option-text hit — the least explicit resolver.
    if selected is None and fuzzy_best is not None:
        selected, method, similarity = fuzzy_best, "choice_text", fuzzy_sim

    if selected is None or not choices or selected >= len(choices):
        return {
            "verdict": "uncertain",
            "selected_index": None,
            "matched_answer": None,
            "method": None,
            "similarity": 0.0,
        }

    canonical_idx = lily_canonical_choice_index(
        [str(c) for c in choices], canonical_answer
    )
    if canonical_idx is None:
        # Malformed question (answer not among choices) — never rule
        # definitively off a broken sheet; escalate.
        verdict = "uncertain"
    else:
        verdict = "correct" if selected == canonical_idx else "incorrect"
    return {
        "verdict": verdict,
        "selected_index": selected,
        "matched_answer": str(choices[selected]),
        "method": method,
        "similarity": round(similarity, 3),
    }


def lily_tier1_evaluate_question(
    transcript_text: str,
    question: dict,
    threshold: Optional[float] = None,
) -> dict:
    """Format dispatch: a question carrying four choices runs the MC
    matcher; anything else runs the freeform matcher against
    acceptable_answers. Pure — the single Tier-1 entry point for the
    agent's adjudication paths. threshold is the optional state-prior
    acceptance threshold (WO-ADDRESSEE-H1 Task 2); None keeps the pre-H1
    behavior exactly."""
    q = question or {}
    choices = q.get("choices")
    if isinstance(choices, list) and len(choices) == 4:
        return lily_tier1_evaluate_mc(
            transcript_text, choices, str(q.get("canonical_answer", "")),
            threshold=threshold,
        )
    return lily_tier1_evaluate(
        transcript_text, q.get("acceptable_answers") or [], threshold=threshold
    )


# HOSTLOOP-001 C3b/C4 — "is this utterance an ANSWER ATTEMPT?", which is a
# different question from "is it RIGHT?".
#
# Both live failures came from conflating the two:
#
#   Session B (lily-05BB92): the only barge path that could bind an answer
#   mid-read required verdict == "correct" (WS-5 mc_early_answer_check). A
#   player who barged with a WRONG-but-committed pick ("B", "Sydney") was
#   therefore not "an answer" to that gate, fell through to Y7's
#   BARGE_IN_CANCEL, and the question died half-aired: choices killed, no
#   binding, no re-air.
#
#   Session A (04:50 UTC): the reverse error. The pre-window buffer took ANY
#   final that overlapped delivery playout, so the complaint fragment "Like
#   speaking at the." was replayed at window open with assume_in_window=True
#   and scored as the q6 answer (verdict=incorrect).
#
# One predicate answers both, and it deliberately owns NO matching logic of
# its own — it reads the verdict/selection the existing Tier-1 matchers
# already produce.
_ANSWER_SHAPE_MIN_CHARS = 2


def lily_answer_shaped(
    text: str,
    question: dict,
    *,
    is_command: bool = False,
) -> bool:
    """True when `text` is an ANSWER ATTEMPT at `question` — right or wrong.

    Multiple choice: the utterance RESOLVES one of the armed choices, by
    letter ("B", "letter b"), NATO spelling ("bravo"), position ("the second
    one") or (fuzzy) choice text. That is exactly
    `lily_tier1_evaluate_mc`'s `selected_index is not None`, so this reads
    that field rather than re-deriving it — MC Tier-1 returns "incorrect"
    for a clean pick of a wrong option and "uncertain" ONLY when nothing
    resolved, which is precisely the answer-shaped / not-answer-shaped line.

    Open (freeform) questions have no closed option set to resolve against,
    so "answer-shaped" is the weaker content test the clause names: any
    content utterance that is not itself a question and not a control
    command. A correct freeform match short-circuits regardless.

    `is_command` is passed in by the caller (lily_scorekeeper owns command
    detection and imports this module — the flag keeps that direction of
    dependency intact and this function pure).
    """
    raw = (text or "").strip()
    if not raw or is_command:
        return False
    # A pure format directive ("I don't want multiple choice") is never an
    # answer attempt (ROOT 2). The detector is pure-only — a fused
    # complaint+answer keeps a residual answer token and stays answer-shaped.
    if lily_detect_format_directive(raw):
        return False
    q = question or {}
    choices = q.get("choices")
    if isinstance(choices, list) and len(choices) == 4:
        return (
            lily_tier1_evaluate_question(raw, q).get("selected_index")
            is not None
        )
    # Freeform. A deterministic Tier-1 hit is answer-shaped by construction.
    if lily_tier1_evaluate_question(raw, q).get("verdict") == "correct":
        return True
    # Asking is not answering — "wait, what was C again?" is conversation,
    # and the re-offer path (not adjudication) owes it a reply.
    if "?" in raw:
        return False
    stripped = _strip_fillers(raw)
    if not stripped or len(stripped) < _ANSWER_SHAPE_MIN_CHARS:
        # Pure filler ("I think...", "um") carries no content to score.
        return False
    return True


def lily_tier1_evaluate_nbest(
    transcript_text: str,
    question: dict,
    hypotheses: Optional[list] = None,
    dispersion: Optional[float] = None,
    dispersion_threshold: Optional[float] = None,
    threshold: Optional[float] = None,
) -> dict:
    """n-best-aware Tier-1 wrapper (WO-LILY-ADDRESSEE-H1-001 Task 1).

    Runs the existing format-dispatch matcher over the 1-best transcript
    PLUS every synthesized hypothesis text, and returns the best verdict
    under the precedence correct > uncertain > incorrect — a hit in ANY
    hypothesis slot counts, an uncertain anywhere blocks a definitive
    incorrect. Existing single-text functions are untouched.

    Dispersion gate: when `dispersion` and `dispersion_threshold` are both
    supplied and dispersion >= threshold, any DEFINITIVE verdict (correct,
    or MC's incorrect) is demoted to "uncertain" — high confidence-variance
    across hypotheses is a deliberation signal, and deliberation escalates
    to the judge instead of scoring. Pure: thresholds are parameters, env
    resolution stays in lily_config at the call site.

    Returns the winning evaluation dict (same base shape as
    lily_tier1_evaluate_question) plus an "nbest" key:
      {"evaluated": int, "hit_index": int,   # 0 = the 1-best transcript
       "dispersion": float|None, "escalated_by_dispersion": bool}
    """
    texts: list[str] = [transcript_text]
    seen = {lily_normalize_answer(transcript_text)}
    for h in hypotheses or []:
        text = h.get("text") if isinstance(h, dict) else h
        if not text or not isinstance(text, str):
            continue
        norm = lily_normalize_answer(text)
        if norm in seen:
            continue
        seen.add(norm)
        texts.append(text)

    _rank = {"correct": 2, "uncertain": 1, "incorrect": 0}
    best: Optional[dict] = None
    best_index = 0
    for i, text in enumerate(texts):
        # `threshold` (state priors, Task 2) rides every per-hypothesis
        # evaluation — the two H1 lanes compose.
        r = lily_tier1_evaluate_question(text, question, threshold=threshold)
        if best is None or _rank[r["verdict"]] > _rank[best["verdict"]]:
            best, best_index = r, i
            if best["verdict"] == "correct":
                break

    assert best is not None  # texts is never empty
    escalated = False
    if (
        best["verdict"] in ("correct", "incorrect")
        and dispersion is not None
        and dispersion_threshold is not None
        and dispersion >= dispersion_threshold
    ):
        # Deliberation prior: the recognizer itself was torn — check,
        # don't score. Matched info is kept for the judge's benefit.
        best["verdict"] = "uncertain"
        escalated = True

    best["nbest"] = {
        "evaluated": len(texts),
        "hit_index": best_index,
        "dispersion": dispersion,
        "escalated_by_dispersion": escalated,
    }
    return best


def lily_fifty_fifty_eliminations(
    choices: list[str],
    canonical_answer: str,
    rng: Optional[random.Random] = None,
) -> list[int]:
    """50/50 lifeline: pick TWO wrong options to eliminate, keeping the
    canonical answer and one random distractor on the board. Returns the
    sorted eliminated indices, or [] when the canonical answer can't be
    located among the choices (never eliminate blind)."""
    if not choices or len(choices) != 4:
        return []
    canonical_idx = lily_canonical_choice_index(
        [str(c) for c in choices], canonical_answer
    )
    if canonical_idx is None:
        return []
    wrong = [i for i in range(len(choices)) if i != canonical_idx]
    picker = rng if rng is not None else random
    return sorted(picker.sample(wrong, 2))


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
    hypotheses_by_speaker: Optional[dict] = None,
) -> str:
    """Build the Tier-2 judge user prompt. attempts is ordered
    (speaker, text), earliest first — order is already decided by
    scorekeeper timestamps and must not be re-litigated.

    hypotheses_by_speaker (WO-LILY-ADDRESSEE-H1-001 Task 1): optional
    {speaker: [{"text", "confidence"}, ...]} of ASR n-best hypotheses for
    an attempt — competing transcriptions of the SAME utterance. The judge
    evaluates the SET against the supplied answer domain: a semantic hit in
    any slot with adequate confidence is a hit. The hypotheses widen what
    the player may have SAID, never what the answer IS — the
    judge-never-invents rule is restated in-prompt whenever they appear."""
    lines = [
        f"QUESTION: {question_prompt}",
        f"CANONICAL ANSWER: {canonical_answer}",
    ]
    if acceptable_answers:
        lines.append("ACCEPTABLE VARIANTS: " + ", ".join(acceptable_answers))
    lines.append("ATTEMPTS IN ORDER (earliest first):")
    any_hypotheses = False
    for idx, (speaker, text) in enumerate(attempts, start=1):
        lines.append(f"{idx}. {speaker}: {text!r}")
        hyps = (hypotheses_by_speaker or {}).get(speaker) or []
        extra = []
        attempt_norm = lily_normalize_answer(text)
        for h in hyps:
            htext = h.get("text") if isinstance(h, dict) else h
            if not htext or not isinstance(htext, str):
                continue
            if lily_normalize_answer(htext) == attempt_norm:
                continue  # identical to the transcribed attempt — noise
            conf = h.get("confidence") if isinstance(h, dict) else None
            extra.append((htext, conf))
        if extra:
            any_hypotheses = True
            lines.append(
                "   ASR N-BEST — the player may have said any of "
                "(alternative transcriptions of the SAME utterance):"
            )
            for j, (htext, conf) in enumerate(extra, start=1):
                suffix = (
                    f" (confidence {conf:.2f})"
                    if isinstance(conf, (int, float)) else ""
                )
                lines.append(f"     {j}. {htext!r}{suffix}")
    if any_hypotheses:
        lines.append(
            "N-BEST RULE: the alternative transcriptions above are competing "
            "ASR hypotheses of the same spoken words. If ANY hypothesis with "
            "adequate confidence expresses the canonical answer, judge the "
            "attempt correct. The hypotheses only widen what the player may "
            "have SAID — they never change what the answer IS. Judge only "
            "against the canonical answer supplied above."
        )
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


# ---------------------------------------------------------------------------
# PATCH-001 T5(c) — non-answer utterance gate (evaluator hygiene).
#
# Live fixtures (Aug 6): "Yeah" (a backchannel) was formally adjudicated
# as an answer attempt and scored incorrect; a bare name fragment
# ("Chris.") produced a null-player answers row. Non-answer-shaped
# utterances in an open window are LOGGED, never scored as attempts —
# per-player stats stay honest. Conservative by design: anything that
# matches the question's own answer surface is ALWAYS an answer, so a
# yes/no question keeps "yeah" scoreable.
# ---------------------------------------------------------------------------

LILY_BACKCHANNELS: frozenset = frozenset({
    "yeah", "yes", "yep", "yup", "no", "nope", "ok", "okay", "oh",
    "uh huh", "mhm", "mm", "hmm", "huh", "right", "wow", "no way",
    "come on", "wait", "what", "really", "nice", "cool", "haha", "lol",
})

# Procedural imperatives spoken AT the host or the table about the RUN OF
# PLAY, not about the trivia fact. Live RM_qs6YeUdkV7or: Rami's "Go." was
# recorded as the q_1 ANSWER_CANDIDATE while his real "Okay. It's Jupiter."
# arrived later — N9 fixed the utterance binding, but "Go." should never
# have entered the candidate set. Same class: "Continue.", "Next.",
# "Start again.", "Go ahead." Answer-surface override upstream still wins,
# so a question whose canonical answer is literally "Go" stays scoreable.
LILY_PROCEDURAL_IMPERATIVES: frozenset = frozenset({
    "go", "continue", "next", "next question", "start again", "start over",
    "go ahead", "keep going", "carry on", "go on", "move on",
})

# ---------------------------------------------------------------------------
# Meta-speech (WO-LILY-HOTFIX-006 N4) — the open window admits ANSWER-SHAPED
# utterances only.
#
# Seven confirmed production rows, 2026-08-08, every one meta-speech about
# the game entered into lily_answers as an answer attempt:
#
#   "Sorry. We're talking about Cape Cod, Massachusetts. The peninsula."
#       -> kb_14, incorrect                        (a topic CLARIFICATION)
#   "But what does that mean to do with Cape Cod?"
#       -> kb_176, incorrect                       (a QUESTION to the host)
#   "Um. Why are we. Why are we in Mumbai or Delhi? Uh. And why are we
#    talking about that?"  -> kb_128, CORRECT, 1 POINT AWARDED. A player
#                             scored for COMPLAINING ABOUT THE TOPIC.
#   "Oh. Why did you point at me? I wasn't even listening..."  -> q_1052
#   "Like, can we just put it here? ..."                       -> q_4819
#   "Use me. The person. And now you telling her my answer."   -> q_8294
#   "She's getting confused. Like she jumped from a question to question."
#                                                              -> q_8291
#
# A player also said aloud "Sorry, Lily. We're not talking to you. That was
# side banter" and was scored anyway.
#
# The classes below are deliberately narrow and anchored on that evidence.
# The answer-surface override upstream is what keeps them conservative: a
# genuine answer murmured inside a complaint-heavy turn matches a surface
# and returns before any of this runs, so the filter can never eat a real
# answer that was actually spoken.
# ---------------------------------------------------------------------------

LILY_META_INTERROGATIVE = "interrogative"
LILY_META_HOST_ADDRESS = "host_address"
LILY_META_GAME_TALK = "game_talk"

# A question ASKED rather than answered. A bare noun-phrase question is NOT
# this ("Saturn?" is an ordinary hedged guess and still an attempt): the
# interrogative must also carry a wh-word at a clause boundary, or a
# modal/auxiliary aimed at the host or the table's own conduct. Every live
# row above that ends in "?" fires here.
_INTERROGATIVE_WH_RE = re.compile(
    r"(?:^|[,.;:!?]\s*|\band\s+|\bbut\s+|\bso\s+|\bor\s+|\blike\s+)"
    r"(?:why|what|how come|how|who|when|where|which)\b"
)
# Modal / auxiliary interrogatives about US or YOU — "can we just put it
# here?", "why did you point at me?", "are you listening?". Never about a
# third-party fact, which is what an answer-shaped question would be.
_INTERROGATIVE_ADDRESS_RE = re.compile(
    r"\b(?:can|could|would|will|should|do|does|did|are|is|was|were|have|has)"
    r"\s+(?:we|you|i|she|he|they|u)\b"
)

# Talk ABOUT the host or the run of play — corrections, complaints,
# procedural remarks. Third-person narration of the host ("she's getting
# confused", "she jumped from a question to question"), second-person
# narration of what she is doing ("you telling her my answer"), and the
# table declaring what IT is doing ("we're talking about Cape Cod").
#
# Note what is deliberately NOT here: any bare mention of "answer",
# "question" or "point". "The answer is Provincetown" is a hedged wrong
# GUESS and must keep auditing as an attempt — _FILLER_PREFIXES already
# treats "the answer is" as an answer form.
_GAME_TALK_RE = re.compile(
    # The host, narrated in the third person. The PREDICATE is what makes
    # this host-conduct talk rather than a fact about a third party — "it's
    # going to be Saturn" is a hedged guess and must stay adjudicable, so
    # generic verbs (got / going / talking) are deliberately absent.
    r"\b(?:she|he)\s*(?:'s|s| is| was| keeps| kept| just)?\s*"
    r"(?:getting\s+confus\w+|confus\w+|jumped|jumping|skipping|skipped|"
    r"asking|asked)\b"
    # the host, narrated in the second person
    r"|\byou(?:'re|r| are)?\s+"
    r"(?:telling|told|saying|said|asking|asked|giving|gave|reading|read|"
    r"pointing|point|skipping|skipped|confusing|repeating)\b"
    # "you guys said it loud" — table correcting the host about attribution
    r"|\byou\s+guys\s+(?:said|saying|told|telling|read|reading)\b"
    # spotlight complaint without a clean interrogative mark
    r"|\bpoint(?:ed|ing)?\s+at\s+me\b"
    # the table declaring the floor / the topic (the clarification class)
    r"|\bwe(?:'re| are| were)\s+"
    r"(?:talking|discussing|arguing|having|not\s+talking|in\s+the\s+middle)\b"
    # explicitly disowned speech
    r"|\bside\s+(?:banter|conversation|chat)\b"
    r"|\bignore\s+(?:that|it|me|us)\b"
    r"|\bdon'?t\s+count\s+(?:that|it)\b"
    r"|\bnot\s+(?:my|an)\s+answer\b"
    r"|\bwasn'?t\s+(?:even\s+)?(?:listening|answering|talking)\b"
    # procedural narration of the run of play itself
    r"|\bfrom\s+(?:a\s+)?questions?\s+to\s+(?:a\s+)?questions?\b"
)


def _meta_normalize(text: str) -> str:
    """Lowercased, diarization-tag-stripped text with punctuation KEPT —
    the interrogative test needs the question mark and the clause
    boundaries that lily_normalize_answer throws away."""
    stripped = re.sub(r"^\s*\[S\d+\]\s*", "", text or "").strip().lower()
    return re.sub(r"\s+", " ", stripped)


def lily_meta_speech_utterance(text: str) -> Optional[str]:
    """Return the meta-speech class of an utterance, or None when it is not
    meta-speech (WO-LILY-HOTFIX-006 N4).

    Classes: LILY_META_HOST_ADDRESS (spoken AT Lily — FL-1's own name
    evidence and floor-hold detectors decide this), LILY_META_INTERROGATIVE
    (a question asked, not an answer given), LILY_META_GAME_TALK
    (corrections, complaints, procedural remarks about the run of play).

    Pure and deterministic. This is the conservative answer-shape floor the
    WO requires wherever FL-1's fused classification is unavailable: an
    utterance that is interrogative, or addressed to Lily, is not an
    answer. Callers apply the answer-surface override FIRST."""
    normalized = _meta_normalize(text)
    if not normalized:
        return None

    # -- addressed to Lily (FL-1 machinery, reused verbatim) --------------
    # Imported lazily so this module stays importable in the offline pure
    # harnesses that predate FL-1; the classifier is stdlib-only, so this
    # never fails in practice.
    try:
        import lily_addressee_classifier as _fl1

        if _fl1.lily_floor_hold(normalized):
            # "Sorry, Lily. We're not talking to you." — the table saying
            # in plain words that this is not for her. It was scored.
            return LILY_META_HOST_ADDRESS
        if _fl1.lily_name_evidence(normalized) != _fl1.NAME_NONE:
            # Any use of her name — vocative, mention or referential. A
            # player naming the host is talking TO or ABOUT her, not
            # answering a trivia question. (An answer that happens to BE
            # "Lily" matched its surface upstream and never reaches here.)
            return LILY_META_HOST_ADDRESS
    except Exception:  # pragma: no cover - defensive, FL-1 is stdlib-only
        pass

    # -- a question asked, not an answer given ---------------------------
    # STT routinely drops the terminal "?" ("Why did you point at me" with
    # no mark). Require a host/table ADDRESS shape before treating a
    # mark-less wh-clause as interrogative, so a hedged closest-number
    # guess ("how many states") stays adjudicable.
    has_wh = bool(_INTERROGATIVE_WH_RE.search(normalized))
    has_address = bool(_INTERROGATIVE_ADDRESS_RE.search(normalized))
    if "?" in normalized and (has_wh or has_address):
        return LILY_META_INTERROGATIVE
    if has_wh and has_address:
        return LILY_META_INTERROGATIVE
    if re.search(
        r"\bwhy\s+(?:did|do|are|is|would|can'?t|don'?t|does)\s+you\b",
        normalized,
    ):
        return LILY_META_INTERROGATIVE

    # -- corrections / complaints / procedural remarks -------------------
    if _GAME_TALK_RE.search(normalized):
        return LILY_META_GAME_TALK
    return None


_FORMAT_MARKER_RE = re.compile(r"\bmulti(?:ple)?\s*choices?\b|\bmcqs?\b")
_FORMAT_REFUSAL_RE = re.compile(
    r"\b(?:no|not|dont|don|stop|quit|hate|without|instead|rather|skip|"
    r"never|enough|sick|tired)\b"
)
# Scaffolding a pure format directive is built from — refusal words, format
# vocabulary and connective fillers. Anything NOT in this set is answer
# content: its presence makes the utterance fused, never pure.
_FORMAT_SCAFFOLD = frozenset({
    "i", "im", "said", "say", "saying", "want", "wanted", "wanna", "these",
    "those", "them", "this", "that", "with", "any", "some", "more", "please",
    "just", "we", "you", "to", "do", "did", "dont", "don", "t", "no", "not",
    "stop", "quit", "hate", "without", "instead", "rather", "of", "give",
    "gimme", "me", "us", "again", "and", "but", "let", "lets", "s", "really",
    "actually", "keep", "keeps", "keeping", "stick", "sticking", "go", "going",
    "doing", "its", "it", "is", "multiple", "choice", "choices", "mcq", "mcqs",
    "option", "options", "format", "kind", "type", "questions", "question",
    "way", "freeform", "free", "form", "open", "ended", "regular", "normal",
    "never", "enough", "sick", "tired", "thanks", "thank", "anymore",
    "the", "a", "an",
})


def lily_detect_format_directive(text: str) -> bool:
    """True when `text` is a PURE format directive — a request/complaint about
    question FORMAT ("I don't want multiple choice", "no more mcqs") that
    carries no answer of its own.

    Format is set by lily_set_round_format, not here; this exists only to keep
    a pure directive OFF the scoring path (WO-LILY-CANARY-DEFECTS-001 ROOT 2,
    live lily-A9B757: "I said I don't want mcqs" was scored a wrong Madonna
    answer and closed the window before the real answer landed).

    Bias to admit: it requires a format marker AND a refusal, and it fires
    ONLY when every token is directive scaffolding. Any residual token — a
    right OR wrong answer clause — makes it not-pure and the utterance stays
    adjudicable, so a fused complaint+answer never loses its answer."""
    norm = re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()
    if not norm:
        return False
    if not _FORMAT_MARKER_RE.search(norm):
        return False
    if not _FORMAT_REFUSAL_RE.search(norm):
        return False
    return not [tok for tok in norm.split() if tok not in _FORMAT_SCAFFOLD]


def lily_non_answer_utterance(
    text: str, question: Optional[dict], roster_names: Optional[list] = None
) -> Optional[str]:
    """Return the reason a final is NOT an answer attempt ("backchannel",
    "bare_name", or one of the LILY_META_* meta-speech classes), or None
    when it is answer-shaped and must be judged.

    Answer-surface match always wins: a final matching the canonical
    answer, an acceptable variant, an MC choice, or an MC letter is an
    answer no matter what else it looks like. That override is why N4's
    meta-speech classes are safe — "Why are we even talking about this?
    Chatham, I guess. Ridiculous." carries the answer and still scores."""
    norm = lily_normalize_answer(text or "")
    if not norm:
        return "empty"
    q = question or {}
    surfaces = {
        lily_normalize_answer(str(q.get("canonical_answer") or ""))
    }
    for acc in q.get("acceptable_answers") or []:
        surfaces.add(lily_normalize_answer(str(acc)))
    for choice in q.get("choices") or []:
        surfaces.add(lily_normalize_answer(str(choice)))
    surfaces.discard("")
    if norm in surfaces:
        return None
    # A longer final containing an answer surface is an answer sentence.
    if any(f" {s} " in f" {norm} " for s in surfaces if s):
        return None
    if norm in LILY_BACKCHANNELS:
        return "backchannel"
    if norm in LILY_PROCEDURAL_IMPERATIVES:
        return "procedural"
    for name in roster_names or []:
        if norm == lily_normalize_answer(str(name)):
            return "bare_name"
    # A PURE format directive is a complaint about the run of play, never an
    # attempt (ROOT 2). It sits after the answer-surface override above, so a
    # fused complaint carrying the answer has already been admitted.
    if lily_detect_format_directive(text):
        return "format_directive"
    # N4: meta-speech LAST, so every answer-surface and roster escape has
    # already had its say. The seven live rows land here.
    return lily_meta_speech_utterance(text)
