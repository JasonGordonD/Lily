"""
lily_binding.py — name-to-voice binding helpers for LILY.

Native lift of the Lovebirds fragmented-STT fixes (lbs_onboarding.py):
  - 2-second fragment accumulation before name extraction, so
    "This." / "Call." / "My name is Jack." parses correctly
  - expanded stopword list (35+ entries) extended with Lily's game
    vocabulary: play, start, ready, question, trivia, team, skip, pass
  - introducer-pattern + capitalized-token name extraction

Pure local logic, zero LLM calls, no livekit imports — importable and
testable anywhere. The lily_bind_speaker tool in lily_agent.py routes
its name-extraction path through this module.
"""

import logging
import re
import time
from typing import Optional

logger = logging.getLogger("lily_binding")

FRAGMENT_ACCUMULATION_SECONDS = 2.0

# Auxiliary verbs — never names (lifted from lbs_onboarding.AUXILIARY_VERBS)
AUXILIARY_VERBS = {
    "did", "can", "are", "were", "is", "do", "does", "have", "has",
    "will", "would", "should", "could", "was", "may", "might", "shall",
    "been", "being", "had", "got", "get", "let", "need", "must",
    "and",
}

RESERVED_AGENT_NAMES = {"lily"}

# Expanded stopword list — Lovebirds set (fragmented-STT hardened) plus
# Lily's game vocabulary so "Play!", "Trivia!", "Skip" never bind as names.
_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "am", "are", "was", "were", "be",
    "been", "being", "have", "has", "had", "do", "does", "did", "will",
    "would", "could", "should", "may", "might", "shall", "can", "must",
    "not", "no", "so", "if", "then", "than", "that", "this", "these",
    "those", "it", "its", "my", "your", "his", "her", "our", "their",
    "me", "him", "us", "them", "who", "what", "where", "when", "how",
    "just", "also", "very", "too", "here", "there", "now", "well",
    "like", "about", "up", "out", "get", "got", "let", "need",
    "hi", "hello", "hey", "um", "uh", "yeah", "yes", "ok", "okay",
    "alone", "solo", "only", "one", "two", "person", "people",
    # Common words misidentified as names from fragmented STT
    "call", "calling", "conducting", "test", "testing", "own", "phone",
    "session", "using", "actually", "talking", "speaking", "working",
    "right", "sure", "think", "know", "thing", "today", "time",
    "going", "really", "good", "want", "said", "say", "tell", "told",
    "still", "first", "last", "back", "new", "old", "same", "much",
    "way", "try", "trying", "start", "stop", "run", "running",
    "mode", "real", "ready", "please", "thanks", "thank",
    # Game vocabulary (Lily extension)
    "play", "playing", "question", "trivia", "team", "skip", "pass",
    "game", "round", "point", "points", "answer",
    "you", "tonight",
    # Adjudication/correction vocabulary (live 2026-08-09 lily-A070E8: a
    # name-fix exchange bound the player as "Correct" and then "Supposed" —
    # the confirmation word and a fragment of "it's supposed to be" both
    # passed as capitalized STT tokens). Verdict words, spelling-fix words
    # and screen-complaint words are never names. Lowercase exact match, so
    # real names that merely CONTAIN these (Wright, Newman) stay bindable.
    "correct", "incorrect", "supposed", "wrong", "answers", "questions",
    "score", "scores", "rounds", "next", "repeat", "again", "screen",
    "spell", "spelled", "spelling", "word", "correctly", "exactly",
    "instead", "removed", "put", "puts", "putting", "fix", "fixed",
    "fixing",
    "latency", "terrible", "board", "locked",
}

_INTRODUCER_RE = re.compile(
    r"\b(?:i['’`]?m|i\s+am|this\s+is|my\s+name\s+is|it['’`]?s|call\s+me|they\s+call\s+me|name['’`]?s)\s+([a-zA-Z]+)",
    re.IGNORECASE,
)


def _strip_diarization_tag(raw: str) -> str:
    """Strip Speechmatics diarization tag: '[S1] Jack' -> 'Jack'."""
    return re.sub(r'^\[S\d+\]\s*', '', raw.strip())


def lily_is_valid_name(candidate: str) -> bool:
    """Whether a candidate token is plausible as a player name."""
    if not candidate:
        return False
    token = candidate.strip()
    if not re.fullmatch(r"[a-zA-Z][a-zA-Z'\-]*", token):
        return False
    lowered = token.lower()
    if len(lowered) < 2:
        return False
    if lowered in AUXILIARY_VERBS or lowered in _STOPWORDS:
        return False
    if lowered in RESERVED_AGENT_NAMES:
        return False
    return True


def lily_extract_name(raw: str) -> Optional[str]:
    """Best-effort first-name extraction from a raw STT utterance.

    Strategy (lifted from lbs_onboarding._extract_name):
      1. Try known introducer patterns (regex with word boundaries)
      2. Fall back to first capitalized or long-enough non-stopword token
    """
    text = _strip_diarization_tag(raw or "")
    if not text:
        return None
    # Strip leading vocative address: "Lily, this is Jack" -> "this is Jack"
    tokens_raw = text.split()
    if len(tokens_raw) > 1 and tokens_raw[0].rstrip(",").lower() in RESERVED_AGENT_NAMES:
        text = " ".join(tokens_raw[1:]).lstrip(", ")

    # Strategy 1: introducer pattern — capture name after "I'm", "this is", etc.
    m = _INTRODUCER_RE.search(text)
    if m:
        candidate = m.group(1).strip()
        if lily_is_valid_name(candidate):
            return candidate.capitalize()

    # Strategy 2: find first standalone word that looks like a name.
    # Prefer capitalized words, then any word >= 2 chars not in stopwords.
    tokens = re.findall(r"[a-zA-Z]+", text)
    for token in tokens:
        if token[0].isupper() and lily_is_valid_name(token):
            return token.capitalize()
    for token in tokens:
        if lily_is_valid_name(token):
            return token.capitalize()
    return None


def lily_extract_explicit_name(raw: str) -> Optional[str]:
    """Extract only direct self-identification, never conversational nouns.

    Accepted: introducer forms ("my name is Rami", "call me Rami") or a
    bare one-token name. This is the durable-binding provenance gate.
    """
    text = _strip_diarization_tag(raw or "").strip()
    if not text:
        return None
    tokens_raw = text.split()
    if (
        len(tokens_raw) > 1
        and tokens_raw[0].rstrip(",").lower() in RESERVED_AGENT_NAMES
    ):
        text = " ".join(tokens_raw[1:]).lstrip(", ")
    match = _INTRODUCER_RE.search(text)
    if match:
        candidate = match.group(1).strip()
        if lily_is_valid_name(candidate):
            return candidate.capitalize()
    bare = re.fullmatch(r"\s*([a-zA-Z][a-zA-Z'\-]*)[.!?]?\s*", text)
    if bare and lily_is_valid_name(bare.group(1)):
        return bare.group(1).capitalize()
    return None


class LilyFragmentAccumulator:
    """Per-speaker 2-second fragment accumulation for name extraction.

    Speechmatics often fragments speech into short chunks
    ("This." / "Call." / "My name is Jack."). Every final segment is
    added here; combined() returns the joined text of all fragments from
    that speaker inside the accumulation window, so name extraction runs
    against the whole phrase instead of a lone fragment.
    """

    def __init__(self, window: float = FRAGMENT_ACCUMULATION_SECONDS) -> None:
        self.window = window
        self._fragments: dict[str, list[tuple[float, str]]] = {}

    def add(self, speaker_label: str, text: str, now: Optional[float] = None) -> str:
        """Record a final fragment; returns the current combined text."""
        t = now if now is not None else time.time()
        clean = (text or "").strip()
        frags = self._fragments.setdefault(speaker_label, [])
        if clean:
            frags.append((t, clean))
        self._fragments[speaker_label] = [
            (ft, ftext) for ft, ftext in frags if t - ft <= self.window
        ]
        return self.combined(speaker_label, now=t)

    def combined(self, speaker_label: str, now: Optional[float] = None) -> str:
        t = now if now is not None else time.time()
        frags = self._fragments.get(speaker_label, [])
        live = [(ft, ftext) for ft, ftext in frags if t - ft <= self.window]
        self._fragments[speaker_label] = live
        combined = " ".join(ftext for _, ftext in live)
        if len(live) > 1:
            logger.info(
                "LILY_BIND | NAME_ACCUMULATE | fragments=%d | combined=%s",
                len(live), combined[:80],
            )
        return combined

    def clear(self, speaker_label: str) -> None:
        self._fragments.pop(speaker_label, None)


def lily_extract_name_from_fragments(
    accumulator: LilyFragmentAccumulator,
    speaker_label: str,
    now: Optional[float] = None,
) -> Optional[str]:
    """Run name extraction over the speaker's accumulated fragment window."""
    combined = accumulator.combined(speaker_label, now=now)
    if not combined:
        return None
    return lily_extract_name(combined)
