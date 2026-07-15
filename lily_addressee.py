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
# Addressee-confidence fusion (Speechmatics diarization + acoustic read)
# -----------------------------------------------------------------------------

_DIARIZATION_CONF_FIELDS = (
    "speaker_confidence",
    "diarization_confidence",
    "speaker_id_confidence",
    "confidence",
)

_ROOM_READ_TO_ACOUSTIC_CONFIDENCE = {
    "agitated / on edge": 0.35,
    "hot / riding high": 0.48,
    "valence sagging": 0.58,
    "flat / low energy": 0.74,
}


def _clamp_confidence(value) -> Optional[float]:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v < 0.0:
        return 0.0
    if v > 1.0:
        return 1.0
    return round(v, 3)


def lily_extract_diarization_confidence(event) -> Optional[float]:
    """Best-effort Speechmatics diarization confidence extraction.

    The 1.6.4 event shape has drifted in the field across wrappers, so this
    helper checks common attribute names and an optional dict payload before
    giving up.
    """
    if event is None:
        return None
    for field in _DIARIZATION_CONF_FIELDS:
        val = getattr(event, field, None)
        conf = _clamp_confidence(val)
        if conf is not None:
            return conf
    if isinstance(event, dict):
        for field in _DIARIZATION_CONF_FIELDS:
            conf = _clamp_confidence(event.get(field))
            if conf is not None:
                return conf
    return None


def lily_fallback_diarization_confidence(
    attribution: Optional[str],
    speaker_label: Optional[str],
) -> float:
    """Deterministic fallback when no explicit diarization confidence exists."""
    if attribution == "speaker_id":
        return 0.90
    if attribution == "label_match":
        return 0.86
    if attribution == "name_match":
        return 0.80
    if attribution == "self_introduction":
        return 0.70
    return 0.65 if speaker_label else 0.50


def lily_acoustic_addressee_confidence(
    room_read: Optional[str],
    child_veto_active: bool,
    *,
    breaker_open: bool = False,
) -> Optional[float]:
    """Acoustic-side confidence from room-read banding + child ladder.

    Returns None when the acoustic pipeline is unavailable (breaker open),
    so fusion can fall back to diarization-only confidence.
    """
    if breaker_open:
        return None
    confidence = _ROOM_READ_TO_ACOUSTIC_CONFIDENCE.get(
        str(room_read or "").strip().lower(),
        0.62,
    )
    if child_veto_active:
        confidence = min(confidence, 0.38)
    return round(confidence, 3)


def lily_fuse_addressee_confidence(
    diarization_confidence: Optional[float],
    acoustic_confidence: Optional[float],
    *,
    diarization_weight: float = 0.75,
    acoustic_weight: float = 0.25,
) -> Optional[float]:
    """Weighted addressee-confidence fusion.

    The score remains additive to existing overlap logic: callers feed this
    into thresholding; they do not replace overlap/state priors with it.
    """
    diar = _clamp_confidence(diarization_confidence)
    acu = _clamp_confidence(acoustic_confidence)
    if diar is None and acu is None:
        return None
    if diar is None:
        return acu
    if acu is None:
        return diar
    dw = max(0.0, float(diarization_weight))
    aw = max(0.0, float(acoustic_weight))
    if dw + aw <= 0:
        return round((diar + acu) / 2.0, 3)
    score = (diar * dw + acu * aw) / (dw + aw)
    return round(max(0.0, min(1.0, score)), 3)


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
