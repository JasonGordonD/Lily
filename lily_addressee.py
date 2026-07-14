"""
lily_addressee.py — addressee-label derivation for the lily_addressee_log
corpus (the B1 training-data flywheel).

Pure stdlib module (lily_memory-style): the clarify-reply parser, the
seconds-into-window computation, and the implicit-label derivation at
adjudication commit all live here so they are testable offline with no
livekit / supabase installed. lily_agent.py wires these into the transcript
event layer; lily_persistence.py owns the actual inserts/updates.

Label vocabulary:
  label:        host_directed | deliberation | unknown
  label_source: implicit_scored_unappealed  — adjudication committed and the
                                              attribution was never appealed
                implicit_appealed           — an appeal re-entered Tier-2 and
                                              corrected the attribution
                explicit_clarify            — ground truth from the clarify
                                              moment ("is that your answer or
                                              are you thinking out loud?")
"""

import re as _re
from typing import Optional

# -- labels -------------------------------------------------------------------

LABEL_HOST_DIRECTED = "host_directed"
LABEL_DELIBERATION = "deliberation"
LABEL_UNKNOWN = "unknown"

LABEL_SOURCE_IMPLICIT_SCORED = "implicit_scored_unappealed"
LABEL_SOURCE_IMPLICIT_APPEALED = "implicit_appealed"
LABEL_SOURCE_EXPLICIT_CLARIFY = "explicit_clarify"

# -- agent actions ------------------------------------------------------------

AGENT_ACTION_SCORED = "scored"
AGENT_ACTION_IGNORED = "ignored"
AGENT_ACTION_CLARIFIED = "clarified"
AGENT_ACTION_ADJUDICATED_OTHER = "adjudicated_other"


# -----------------------------------------------------------------------------
# Clarify-reply parser — the explicit ground-truth moment
# -----------------------------------------------------------------------------

_PUNCT_RE = _re.compile(r"[^a-z0-9\s']+")


def _normalize(text: str) -> str:
    stripped = _re.sub(r"^\s*\[S\d+\]\s*", "", text or "").strip()
    lowered = _PUNCT_RE.sub(" ", stripped.lower())
    return _re.sub(r"\s+", " ", lowered).strip()


# Affirmative: "that was my answer" / "yes" / "final answer" / "lock it in".
_AFFIRMATIVE_PATTERNS = (
    r"\byes\b",
    r"\byeah\b",
    r"\byep\b",
    r"\byup\b",
    r"\bfinal answer\b",
    r"\bthat's my answer\b",
    r"\bthats my answer\b",
    r"\bthat is my answer\b",
    r"\bit's my answer\b",
    r"(?<!not )\bmy answer\b",
    r"\bi'm answering\b",
    r"\bim answering\b",
    r"\block (?:it|that) in\b",
    r"\blocking (?:it|that) in\b",
    r"\bi'm sure\b",
    r"\bim sure\b",
)

# Negative / deliberation: "no" / "just thinking" / "talking to him".
_NEGATIVE_PATTERNS = (
    r"\bno\b",
    r"\bnope\b",
    r"\bnah\b",
    r"\bjust thinking\b",
    r"\bthinking out loud\b",
    r"\bstill thinking\b",
    r"\bjust talking\b",
    r"\btalking to (?:him|her|them|each other|ourselves|my\w*)\b",
    r"\bstill arguing\b",
    r"\bstill discussing\b",
    r"\bstill deciding\b",
    r"\bnot my answer\b",
    r"\bnot an answer\b",
    r"\bwasn't answering\b",
    r"\bwasnt answering\b",
    r"\bignore (?:that|it|me)\b",
    r"\bdon't count (?:that|it)\b",
    r"\bdont count (?:that|it)\b",
)

_AFFIRMATIVE_RE = _re.compile("|".join(_AFFIRMATIVE_PATTERNS))
_NEGATIVE_RE = _re.compile("|".join(_NEGATIVE_PATTERNS))


def lily_parse_clarify_reply(text: str) -> str:
    """
    Parse a player's reply to the clarify question ("is that your answer or
    are you thinking out loud?") into a label for the CLARIFIED utterance:

      affirmative  -> LABEL_HOST_DIRECTED  ("yes", "that's my answer",
                                            "final answer")
      negative     -> LABEL_DELIBERATION   ("no", "just thinking",
                                            "talking to him")
      unparseable  -> LABEL_UNKNOWN        (anything else, or a reply that
                                            fires both directions)

    Deterministic, keyword/regex only — no LLM.
    """
    normalized = _normalize(text)
    if not normalized:
        return LABEL_UNKNOWN
    affirmative = _AFFIRMATIVE_RE.search(normalized) is not None
    negative = _NEGATIVE_RE.search(normalized) is not None
    if affirmative and not negative:
        return LABEL_HOST_DIRECTED
    if negative and not affirmative:
        return LABEL_DELIBERATION
    return LABEL_UNKNOWN


# -----------------------------------------------------------------------------
# Window timing
# -----------------------------------------------------------------------------

def lily_seconds_into_window(
    opened_at: Optional[float],
    segment_ts: Optional[float],
) -> Optional[float]:
    """Seconds between the answer window opening and this segment's start.
    None when either timestamp is missing; clamped at 0 (a segment stamped
    fractionally before the open is 'at open', never negative)."""
    if opened_at is None or segment_ts is None:
        return None
    return round(max(0.0, segment_ts - opened_at), 3)


# -----------------------------------------------------------------------------
# Implicit weak labels at adjudication commit
# -----------------------------------------------------------------------------

def lily_candidate_key(candidate: dict) -> str:
    """The scorekeeper's candidate key: player name, or the open-floor
    unrostered key for an unbound voice."""
    return (
        candidate.get("player")
        or f"unrostered:{candidate.get('speaker_label') or 'UU'}"
    )


def lily_labels_for_adjudication(
    ordered_candidates: list,
    winner_key: Optional[str],
) -> dict:
    """
    Implicit weak labels at adjudication commit: the winning utterance and
    every scored-incorrect utterance were treated as answers, so they get
    label=host_directed with label_source=implicit_scored_unappealed.

    Unrostered non-winners are never scored (they're held for binding, not
    committed), so they get no implicit label.

    Returns {candidate_key: (label, label_source)}.
    """
    labels: dict = {}
    for cand in ordered_candidates or []:
        key = lily_candidate_key(cand)
        if key == winner_key or cand.get("player"):
            labels[key] = (LABEL_HOST_DIRECTED, LABEL_SOURCE_IMPLICIT_SCORED)
    return labels
