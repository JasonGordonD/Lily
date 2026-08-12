"""
lily_scorekeeper.py — LILY Scorekeeper (descendant of Lovebirds Loop 1).

Runs on every Speechmatics transcript segment. Zero LLM calls, pure local
state, no livekit imports — importable anywhere (tests run without the
agents framework installed).

Owns:
  - the N-player roster and per-player state (score/streak/talk-time/quiet)
  - the system-directed classifier (vocative "Lily" — those turns must
    never count as answer attempts)
  - the answer-window state: opens on TTS playback completion, bounded
    duration, finals-only, one candidate per player (first final wins),
    order decided by STT segment timestamps — never by an LLM
  - unrostered-speaker detection (LILY_STATE | UNROSTERED_SPEAKER,
    open-floor fallback)
  - sticky player-command detection ("back to normal", "skip") —
    period/fragment-proof, enforced deterministically at the
    transcript-event layer (the prompt is texture, not the mechanism)
  - the compact state block injected before each Lily turn
  - the prior-state machine (WO-ADDRESSEE-H1 Task 2): OPEN_WINDOW /
    OVERLAP / HOST_SPEAKING / SCORING / IDLE, with cross-speaker
    timestamp-overlap detection inside the open window — drives the
    Tier-1 acceptance threshold (env-tunable via lily_config)
  - overlap-time addressee confidence fusion: diarization confidence
    blended with room-level acoustic confidence to conservatively demote
    low-confidence crosstalk attributions to open-floor utterances

The scorekeeper owns ORDER; the LLM owns CORRECTNESS.
"""

import logging
import re as _re
import time
from datetime import datetime, timezone
from typing import Optional

import lily_binding
import lily_config
import lily_evaluation

logger = logging.getLogger("lily_scorekeeper")

TRANSCRIPT_BUFFER_SIZE = 30
DEFAULT_ANSWER_WINDOW_SECONDS = 15.0
FRAGMENT_JOIN_WINDOW_SECONDS = 2.0  # ASR fragment accumulation for commands
QUARANTINE_LOG_SIZE = 200  # WS-10 segment-sanity quarantine (full payloads)

# ---------------------------------------------------------------------------
# Prior states (WO-LILY-ADDRESSEE-H1-001 Task 2) — scorekeeper-owned game
# states driving the Tier-1 acceptance threshold. String values are also
# the lily_addressee_log.prior_state column vocabulary and the keys of
# lily_config.tier1_threshold_for_prior (spelled literally there because
# this module imports lily_config, not the other way around).
#
# Precedence (most specific wins): SCORING > HOST_SPEAKING > OVERLAP >
# OPEN_WINDOW > IDLE.
# ---------------------------------------------------------------------------

PRIOR_OPEN_WINDOW = "OPEN_WINDOW"    # window open, no crosstalk — lowered bar
PRIOR_OVERLAP = "OVERLAP"            # >=2 overlapping speakers in-window
PRIOR_HOST_SPEAKING = "HOST_SPEAKING"  # Lily on air — backchannels expected
PRIOR_SCORING = "SCORING"            # adjudication in flight
PRIOR_IDLE = "IDLE"                  # window closed, nothing special

PRIOR_STATES = (
    PRIOR_OPEN_WINDOW, PRIOR_OVERLAP, PRIOR_HOST_SPEAKING,
    PRIOR_SCORING, PRIOR_IDLE,
)


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _coerce_confidence(value: object) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return _clamp01(float(value))
    return None


def lily_overlap_fused_confidence(
    diarization_confidence: object,
    acoustic_confidence: object,
) -> float:
    """Fused addressee confidence for overlap arbitration (0..1).

    Missing signals degrade to a neutral fallback; available signals are
    blended with env-tunable weights.
    """
    diar = _coerce_confidence(diarization_confidence)
    acoustic = _coerce_confidence(acoustic_confidence)
    neutral = lily_config.overlap_fusion_neutral_confidence()
    if diar is None and acoustic is None:
        return round(neutral, 3)
    if diar is None:
        return round(acoustic if acoustic is not None else neutral, 3)
    if acoustic is None:
        return round(diar, 3)
    w_diar = lily_config.overlap_fusion_diarization_weight()
    return round(_clamp01(diar * w_diar + acoustic * (1.0 - w_diar)), 3)


# Per-speaker span-list cap inside one window (a 15–30s window can't need
# more; keeps a pathologically chatty table bounded).
_MAX_SPANS_PER_SPEAKER = 20

# Round formats (multiple-choice WO): freeform is the classic open ask;
# multiple_choice reads four options aloud. The default session includes
# exactly ONE multiple-choice round — round 2 — unless the table asks for
# a different arrangement via the lily_set_round_format tool.
ROUND_FORMATS = ("freeform", "multiple_choice")
DEFAULT_MC_ROUND = 2

# ---------------------------------------------------------------------------
# System-directed turn classifier — lifted verbatim from lbs_scorekeeper
# (lbs_is_system_directed) with the vocative swapped to "Lily".
# Utterances addressed to the host, not answers. These MUST NOT be recorded
# as answer candidates during an open answer window.
# ---------------------------------------------------------------------------

_SYSTEM_DIRECTED_PHRASES = (
    "are you there",
    "are you listening",
    "can you hear",
    "are you functioning",
    "are you working",
    "are you doing",
    "are you broken",
    "are you alive",
    "are you with us",
)

# Standalone "hello" variants — entire utterance is just hello/hello?/hello.
_SYSTEM_DIRECTED_HELLO_RE = _re.compile(
    r"^\s*hello\s*[?.!,]*\s*$",
    _re.IGNORECASE,
)

# Vocative "Lily" as standalone (e.g. "Lily." / "Lily?" / "Lily, are you there")
# — matches when Lily is the first standalone token, followed by punctuation
# and either nothing else or a system-directed continuation. (The Lovebirds
# original made only the closing bracket of the diarization tag optional; the
# whole tag is optional here because Lily strips tags before classification.)
_VOCATIVE_LILY_RE = _re.compile(
    r"^\s*(?:\[S\d+\]\s*)?lily\s*[?.,!]+",
    _re.IGNORECASE,
)


def _strip_diarization_tag(text: str) -> str:
    return _re.sub(r"^\s*\[S\d+\]\s*", "", text).strip()


def lily_is_system_directed(text: str) -> tuple[bool, Optional[str]]:
    """
    Classify whether an utterance is addressed to the host (Lily) rather
    than being game content / an answer attempt.

    Returns (is_system_directed, matched_pattern). matched_pattern is the
    pattern string that fired (for logging) or None.

    Conservative heuristic — keyword/regex only, no LLM. Only fires on
    high-confidence vocative addresses, not casual mentions of "Lily"
    in table talk.
    """
    if not text:
        return False, None
    raw = text
    stripped = _strip_diarization_tag(text)
    lower = stripped.lower()

    # Standalone "Hello?" / "Hello." / "Hello"
    if _SYSTEM_DIRECTED_HELLO_RE.match(stripped):
        return True, "standalone_hello"

    # Vocative Lily at start: "Lily." "Lily?" "Lily, ..."
    if _VOCATIVE_LILY_RE.match(raw):
        return True, "vocative_lily"

    # Diagnostic phrases — typically directed at the agent
    for phrase in _SYSTEM_DIRECTED_PHRASES:
        if phrase in lower:
            return True, f"phrase:{phrase}"

    return False, None


# ---------------------------------------------------------------------------
# Sticky player commands — deterministic detection (spec §11.4).
# "back to normal" (adult-mode revert) and "skip" are enforced in code at
# the transcript-event layer; detection is period/fragment-proof against
# ASR fragmentation ("Back. To normal."). Cross-segment fragments are
# handled by the scorekeeper's per-speaker fragment join below.
# ---------------------------------------------------------------------------

_COMMAND_NORMALIZE_RE = _re.compile(r"[^a-z0-9\s]+")


def _normalize_command_text(text: str) -> str:
    stripped = _strip_diarization_tag(text or "")
    lowered = stripped.lower()
    cleaned = _COMMAND_NORMALIZE_RE.sub(" ", lowered)
    return _re.sub(r"\s+", " ", cleaned).strip()


def _normalize_answer_text(text: str) -> str:
    """Answer-equality normalizer for the WS-8 ghost-fold echo check —
    same casing/punctuation flattening as command normalization, reused so
    a diarizer echo copy ("Mark." vs "Mark") compares equal to the bound
    player's recorded answer."""
    return _normalize_command_text(text)


# Pacing choice — group prefs WO: "timed" (the standard clock, today's
# behavior) vs "relaxed" (no time pressure; the answer window stretches by
# LILY_RELAXED_WINDOW_MULTIPLIER). Deterministic, paraphrase-tolerant
# detection at the command layer, like "back to normal": the spoken choice
# flips the flag in code; the lily_set_pacing tool covers phrasings these
# conservative patterns miss. Checked BEFORE start_game so "let's play
# relaxed" reads as a pacing choice, not a game start.
# NOTE (post-merge reconcile): "freeform" deliberately does NOT appear in
# these patterns — the multiple-choice WO owns that word as the open
# ROUND FORMAT ("freeform" vs "multiple_choice", lily_set_round_format),
# so a spoken "let's go freeform" must reach that feature, not flip
# pacing. Relaxed pacing keys on relaxed/casual/chill/timer words only.
_PACING_RELAXED_PATTERNS = (
    # "let's play relaxed" / "keep it chill" / "make this casual"
    r"\b(?:play|go|keep (?:it|things)|make (?:it|this)|do)"
    r"(?: it| this| things)?(?: more)?"
    r" (?:relaxed|casual|chill|laid back)\b",
    # "relaxed rounds" / "casual pace" / "untimed game"
    r"\b(?:relaxed|casual|chill|laid back|untimed)"
    r" (?:rounds?|pace|pacing|mode|game|play|style)\b",
    # "no timer" / "without the clock" / "turn the timer off"
    r"\bno (?:timers?|clock|countdowns?)\b",
    r"\bwithout (?:the |a )?(?:timers?|clock|countdowns?)\b",
    r"\bturn (?:the )?(?:timers?|clock|countdowns?) off\b",
    r"\bturn off the (?:timers?|clock|countdowns?)\b",
    # A whole-utterance answer to the pacing offer ("Relaxed, please.") —
    # entire-utterance only, so "I'm relaxed" table talk never fires.
    r"^(?:relaxed|casual|chill)(?: please| rounds?)?$",
)
_PACING_TIMED_PATTERNS = (
    # "let's play timed" / "keep it timed" / "make this timed"
    r"\b(?:play|go|keep (?:it|things)|make (?:it|this)|do)"
    r"(?: it| this| things)?(?: more)?"
    r" timed\b",
    # "timed rounds" / "timed mode" / "timed pace"
    r"\btimed (?:rounds?|pace|pacing|mode|game|play|style)\b",
    # "with the timer" / "on the clock" / "put the timer back on"
    r"\b(?:with|on) (?:the|a) (?:timer|clock)\b",
    r"\b(?:put|turn) the (?:timers?|clock) (?:back )?on\b",
    r"\bbring the (?:timers?|clock) back\b",
    # Whole-utterance answer to the offer ("Timed." / "timed please")
    r"^timed(?: please| rounds?)?$",
)
_PACING_RELAXED_RE = _re.compile("|".join(_PACING_RELAXED_PATTERNS))
_PACING_TIMED_RE = _re.compile("|".join(_PACING_TIMED_PATTERNS))
# SHARED negation/refusal builder (WO-LILY-HOTFIX-009 W3 + fix-loop
# consolidation). Every deterministic mode-extraction surface — pacing
# (lily_detect_pacing_choice), media (lily_detect_media_choice) and the
# adult-deck lobby intent (lily_parse_lobby_setup_intents) — keys its
# ENABLE on a noun vocabulary, so each needs the SAME refusal check or a
# refusal re-inverts into the thing refused. Rather than patch negation
# into each independently, they route through this one builder: the next
# surface added inherits the whole construction set by calling it.
#
# The live W3 failure was the gap: "I'm not playing with the timer" hit
# the enable substring "with the timer" while an adjective-only negation
# guard ("not timed") missed the noun three tokens away. Explicit refusal
# CONSTRUCTIONS (not a wildcard window) so "timed rounds, not relaxed"
# never misreads. `noun` is a raw alternation of the topic's nouns
# (e.g. "timer|timed|clock|countdown"); apostrophes normalize to spaces
# ("don t"). Constructions covered:
#   1. "[neg] … playing|doing|using|messing|dealing with … NOUN"
#   2. "stop|kill|lose|drop|ditch|cut|cancel|scrap|forget … the NOUN"
#   3. "[neg] with|on the NOUN"
#   4. "don't want … NOUN"
#   5. "[neg] put|turn|bring|switch … NOUN (on)" — negates an ENABLE verb
#      ("don't put the timer on", "don't turn on the timer"): the fix-loop
#      HIGH-1 gap, where the negation lands on the enable verb, not the noun
#   6. "[neg] (the) NOUN" — bare adjacent negation ("no timer", "not timed")
def _build_refusal_re(noun: str) -> "_re.Pattern":
    return _re.compile(
        r"\b(?:not|no|don t|dont|do not|won t|wont|never|ain t)\b"
        r"(?:\s+(?:gonna|going to|want to|wanna|here to|about to))?"
        r"\s+(?:playing|play|doing|do|using|use|messing|dealing|bothering"
        r"|working|running|down for|into)"
        r"(?:\s+(?:with|around with|around))?"
        r"\s+(?:the\s+|a\s+|any\s+|that\s+|this\s+)?"
        rf"(?:{noun})s?\b"
        r"|\b(?:stop|kill|lose|drop|ditch|cut|cancel|scrap|forget|kill off"
        r"|no more|skip)"
        r"(?:\s+(?:the|that|this|with(?: the)?|about(?: the)?))?"
        rf"\s+(?:{noun})s?\b"
        rf"|\b(?:not|no)\s+(?:with|on)\s+(?:the\s+|a\s+)?(?:{noun})s?\b"
        r"|\b(?:don t want|dont want|do not want|not want|won t do|no need for)"
        rf"(?:\s+(?:the|any|a))?\s+(?:{noun})s?\b"
        r"|\b(?:not|no|don t|dont|do not|never)\s+"
        r"(?:put|turn|bring|switch|get|have|start|throw|set)"
        r"(?:\s+(?:on|up|back|us|it|the|a|any|some)){0,3}\s+"
        rf"(?:{noun})s?(?:\s+(?:on|up|back|back on))?\b"
        r"|\b(?:no|not|don t|dont|do not|won t do|without)"
        rf"\s+(?:the\s+|it\s+|any\s+|them\s+|more\s+)?(?:{noun})s?\b"
    )


_PACING_TIMER_REFUSAL_RE = _build_refusal_re(r"timer|timed|clock|countdown")


_PACE_SLOWER_RE = _re.compile(
    r"\b(?:speak|talk|go|say (?:it|that)|slow) (?:it |them )?"
    r"(?:down |a bit |a little )?slow(?:er|ly)?\b"
    r"|\bslow(?:er| down| it down)\b"
    r"|\btoo fast\b|\byou'?re going too fast\b|\bnot so fast\b"
)
_PACE_FASTER_RE = _re.compile(
    r"\b(?:speak|talk|go|say (?:it|that)) (?:it |them )?"
    r"(?:a bit |a little )?fast(?:er)?\b"
    r"|\bspeed (?:it |things )?up\b|\btoo slow\b|\bhurry (?:it )?up\b"
)


def lily_detect_pace_request(text: str) -> Optional[str]:
    """PATCH-003 P7 — detect a delivery-rate request. Returns "slow",
    "normal" (faster/back-to-normal), or None. Punctuation/fragment-proof
    like the other command detectors. The 'slower' fixture ('Can you
    please speak slower?') answered by a fragment and no change is what
    this closes."""
    normalized = _normalize_command_text(text)
    if not normalized:
        return None
    if _PACE_SLOWER_RE.search(normalized):
        return "slow"
    if _PACE_FASTER_RE.search(normalized):
        return "normal"
    return None


def lily_detect_pacing_choice(text_normalized: str) -> Optional[str]:
    """Classify a normalized utterance as a pacing choice. Returns
    "pacing_relaxed", "pacing_timed", or None. A REFUSED timer ("no timed
    rounds", "I'm not playing with the timer") is a relaxed request and is
    checked FIRST so OFF wins the collision — the enable patterns key on
    the same timer/clock nouns, and a refusal must never lose to the
    substring it contains. When BOTH directions fire un-negated the
    utterance is ambiguous — returns None (the prompt/tool layer sorts it
    out conversationally; nothing flips on ambiguity)."""
    if _PACING_TIMER_REFUSAL_RE.search(text_normalized):
        return "pacing_relaxed"
    relaxed = _PACING_RELAXED_RE.search(text_normalized) is not None
    timed = _PACING_TIMED_RE.search(text_normalized) is not None
    if relaxed and not timed:
        return "pacing_relaxed"
    if timed and not relaxed:
        return "pacing_timed"
    return None


# Spoken game-start phrases — deterministic, so start_game engages from the
# spoken path too (not just the lily_begin_round tool / lily_control.start
# RPC). Conservative set; "let s" is "let's" after punctuation stripping.
_START_GAME_RE = _re.compile(
    r"\b(?:"
    r"start the (?:game|quiz|trivia)"
    r"|let\s?s (?:start|play|go|begin)"
    r"|start round one"
    r"|ready to (?:start|begin|play)"
    r"|dive in"
    r"|begin (?:the )?(?:game|round|quiz)"
    r"|kick (?:it )?off"
    r")\b"
)

# HOSTLOOP-001 C7 — the BARE start intent. Session A (2026-08-12 04:51):
# the player said "Starts." — an STT rendering of "start" — which matched
# none of the phrase patterns above; 13 seconds of dead air followed, then
# a false "welcome back" re-greet. A bare "start"/"starts"/"begin"/
# "kick off" IS the start intent when it is essentially the whole
# utterance — matched as an INTENT (utterance-shaped, ≤4 tokens with only
# filler around it), never as a substring, so "before we start, one
# question" and "she starts crying every time" can never launch a game.
_BARE_START_TOKEN_RE = _re.compile(
    r"^(?:start|starts|begin|begins|kick ?off)$"
)
_START_FILLER_TOKENS = frozenset({
    "ok", "okay", "yeah", "yes", "yep", "so", "alright", "right", "now",
    "lily", "please", "then", "well", "go", "and", "us", "it", "off",
})


def lily_is_bare_start_intent(text: str) -> bool:
    """True when the utterance IS the start intent (C7), not merely
    contains a start-ish word."""
    normalized = _normalize_command_text(text)
    if not normalized:
        return False
    tokens = normalized.split()
    if not tokens or len(tokens) > 4:
        return False
    core = [t for t in tokens if t not in _START_FILLER_TOKENS]
    if not core:
        return False
    return bool(_BARE_START_TOKEN_RE.fullmatch(" ".join(core)))

# P0-2 multi-intent setup parser. Unlike on_transcript_segment's
# command-or-media dispatch result, this deliberately returns ALL setup
# intents present in the final so "let's play + adult + pictures + voice"
# cannot collapse to start_game.
_START_PARAPHRASE_RE = _re.compile(
    r"\b(?:i|we)\s+(?:want|would like)\s+to\s+play\b"
)
_VOICE_CHANGE_REQUEST_RE = _re.compile(
    r"\b(?:"
    r"(?:change|switch|swap|use)\s+(?:your\s+)?voice"
    r"|(?:the\s+)?other\s+voice"
    r"|voice\s+(?:one|two|1|2)"
    r")\b"
)
_ADULT_DECK_REQUEST_RE = _re.compile(
    r"\b(?:adult|grown[- ]?up|18\s*\+)\s+(?:deck|trivia|game|mode)\b"
)
# W3 fix-loop (MEDIUM-1): the adult deck is a mode reached by spoken
# preference too, so it shares the negation hazard — "I don't want the
# adult deck" hit the request substring "adult deck" and enabled it.
# Same shared refusal builder as pacing/media; blocks the request when the
# utterance refuses it. (Apostrophes normalize to spaces; "18+" -> "18".)
_ADULT_DECK_REFUSAL_RE = _build_refusal_re(
    r"adult (?:deck|trivia|game|mode|round|content|stuff)"
    r"|grown up (?:deck|trivia|game|mode)"
    r"|grownup (?:deck|trivia|game|mode)"
    r"|18 (?:deck|trivia|game|mode)"
)
_SETUP_AGE_MENTION_RE = _re.compile(
    r"\b(?:18\s*\+|18|eighteen|above\s+18|over\s+18|"
    r"\d{2}\s+years?\s+old|born\s+in\s+(?:19|20)\d{2})\b"
)
_ADULT_HEAT_MIX_RE = _re.compile(
    r"\b(?:mixed?\s+pictures?|pictures?\s+in\s+mixed?\s+mode|"
    r"both\s+(?:the\s+)?suggestive\s+and\s+explicit|"
    r"suggestive\s+and\s+explicit)\b"
)
_ADULT_HEAT_EXPLICIT_RE = _re.compile(
    r"\b(?:explicit|full\s+explicit)\b"
)
_ADULT_HEAT_SUGGESTIVE_RE = _re.compile(
    r"\b(?:suggestive|spicy[- ]but[- ]suggestive)\b"
)


def lily_parse_lobby_setup_intents(text: str) -> dict:
    """Return every setup/start intent in one user final.

    This is intentionally non-exclusive; the scorekeeper's control result
    remains exclusive for scoring, while the agent uses this full parse to
    finish setup before kickoff.
    """
    normalized = _normalize_command_text(text)
    if not normalized:
        return {
            "start": False,
            "voice": False,
            "adult": False,
            "media": None,
            "heat": None,
            "age_mentioned": False,
            "age_consent": False,
        }
    heat = None
    if _ADULT_HEAT_MIX_RE.search(normalized):
        heat = "mix"
    elif _ADULT_HEAT_EXPLICIT_RE.search(normalized):
        heat = "explicit"
    elif _ADULT_HEAT_SUGGESTIVE_RE.search(normalized):
        heat = "suggestive"
    return {
        "start": bool(
            _START_GAME_RE.search(normalized)
            or _START_PARAPHRASE_RE.search(normalized)
        ),
        "voice": bool(_VOICE_CHANGE_REQUEST_RE.search(normalized)),
        "adult": bool(_ADULT_DECK_REQUEST_RE.search(normalized))
        and not _ADULT_DECK_REFUSAL_RE.search(normalized),
        # Run independently from control-command detection: no XOR.
        "media": lily_detect_media_choice(text),
        "heat": heat,
        # Presence is ordering evidence only; P0-3 owns consent semantics.
        "age_mentioned": bool(_SETUP_AGE_MENTION_RE.search(normalized)),
        "age_consent": lily_detect_age_consent(text),
    }

# Bare affirmatives — must NOT start the game after an A-or-B offer
# (WO-2: "Yes, I am." after ready-or-waiting).
_BARE_AFFIRMATIVE_RE = _re.compile(
    r"^\s*(?:"
    r"y(?:es|eah|ep|up|a)|yup|sure|ok(?:ay)?|alright|all right"
    r"|yes(?:,?\s*(?:ma'?am|sir|please|i am|i do|i will))?"
    r"|yeah(?:,?\s*(?:i am|i do))?"
    r")\.?\s*$",
    _re.IGNORECASE,
)

# Agent offer shapes that make a following bare "yes" ambiguous.
_OR_CHOICE_OFFER_RE = _re.compile(
    r"\b(?:"
    r"ready to (?:dive|jump|start|begin|go).{0,40}\bor\b.{0,40}wait"
    r"|waiting on anyone"
    r"|or are you waiting"
    r"|ready .{0,20}or .{0,30}wait"
    r"|want .{0,40}\bor\b.{0,40}(?:straight|start|jump|dive|refresher)"
    r"|refresher.{0,20}\bor\b.{0,20}(?:straight|ready|start)"
    r"|voice-only.{0,40}\bor\b"
    r"|keep chasing.{0,40}\bor\b.{0,40}start"
    r")\b",
    _re.IGNORECASE,
)


def lily_is_bare_affirmative(text: str) -> bool:
    """True for a short yes/yeah/yep/okay with no start/play/go payload."""
    normalized = _normalize_command_text(text)
    if not normalized:
        return False
    # Explicit start language always wins — not bare.
    if _START_GAME_RE.search(normalized):
        return False
    raw = (text or "").strip()
    if len(raw) > 48:
        return False
    return bool(_BARE_AFFIRMATIVE_RE.match(raw))


def lily_detect_or_choice_offer(text: str) -> bool:
    """True when Lily's turn offered an A-or-B choice (ready vs waiting,
    refresher vs start, voice-only vs chase pictures, etc.)."""
    return bool(_OR_CHOICE_OFFER_RE.search(text or ""))

# "Forget me" — the deletion right (WO-LILY-FORGETME-001). Deterministic,
# paraphrase-tolerant detection at the command layer, like "back to
# normal": the prompt drives the spoken confirmation, but the REQUEST is
# never left to prompt whim. Detection only ARMS a pending-confirm state
# — nothing is deleted until a deterministic spoken yes — so a rare false
# positive costs one polite question, never data.
_FORGET_PATTERNS = (
    # "forget me" / "forget us" / "forget about us" / "forget this table"
    r"\bforget (?:about )?(?:me|us|this table)\b",
    # "forget everything/what you know (about me/us)"
    r"\bforget (?:everything|all|anything|whatever|what)"
    r"(?: (?:about (?:me|us|this table)|you (?:know|remember|have)))\b",
    # "delete/erase/wipe/remove/clear what you know about us"
    r"\b(?:delete|erase|wipe|remove|clear) "
    r"(?:everything|all|anything|whatever|what) you (?:know|remember|have)\b",
    # "delete me" / "erase us" / "wipe us"
    r"\b(?:delete|erase|wipe|remove) (?:me|us)\b",
    # "delete my data" / "wipe our history" / "erase our memories"
    r"\b(?:delete|erase|wipe|remove|clear) (?:my|our) "
    r"(?:data|memory|memories|history|info|information|file|profile|voice|voices)\b",
)
_FORGET_RE = _re.compile("|".join(_FORGET_PATTERNS))
# Negation guard: "don't forget us", "never forget me" must NOT fire —
# they are the opposite request. (Apostrophes normalize to spaces, so
# "don't" arrives as "don t".)
_FORGET_NEGATION_RE = _re.compile(
    r"\b(?:don t|dont|do not|never|won t|wont|will not|can t|cant|cannot|"
    r"couldn t|couldnt|didn t|didnt|not)\s+(?:ever\s+)?forget\b"
)


# STOP primitive (WO-LILY-PATCH-002 A5/T12) — the runaway-agent brake.
# When Lily runs away (re-airing, talking over a request, question-storming),
# an addressed "Lily, stop" is recognized at the dispatch gate BEFORE the
# LLM, so the halt can never itself be answered by a re-aired question. She
# stops, takes a breath, waits. Garble tolerant (the stop/staap/stahp STT
# class). An ADDRESSED stop ("Lily, stop") always fires; a BARE stop fires
# only in solo (one player — no ambiguity about who is being told to stop).
_STOP_WORD = r"st[ao][h']?[ao]?p+|st[ao]wp|stlop|halt|freeze"
_STOP_CORE_RE = _re.compile(rf"\b(?:{_STOP_WORD})\b")
# Emphatic repetition ("stop stop stop") — three or more stop-words in a
# row is an unambiguous runaway-agent brake. The solo gate exists only to
# disambiguate WHO a lone "stop" addresses; a shouted repetition removes
# that ambiguity, so it fires regardless of roster (the live lily-5E3036
# leak: "Stop stop stop stop stop stop stop" never routed because a phantom
# second player made the room read non-solo).
_STOP_REPEAT_RE = _re.compile(
    rf"\b(?:{_STOP_WORD})\b(?:[\s,.!]+(?:{_STOP_WORD})\b){{2,}}"
)
_STOP_ADDRESSED_RE = _re.compile(
    rf"\b(?:lily|lilly|lil)\b[\s,.!]*(?:{_STOP_WORD})"
    rf"|(?:{_STOP_WORD})\b[\s,.!]*\b(?:lily|lilly|lil)\b"
)
_STOP_NEGATION_RE = _re.compile(r"\b(?:do not|dont|don t|please dont|never)\s+st")
_QUIT_GAME_RE = _re.compile(
    r"\b(?:"
    r"stop (?:the )?(?:quiz|game|trivia)"
    r"|quit (?:the )?(?:quiz|game|trivia)"
    r"|end (?:the )?(?:quiz|game|trivia)"
    r"|i (?:do not|don t|dont) want to play anymore"
    r"|i m done (?:playing|with (?:the )?(?:quiz|game|trivia))"
    r")\b"
)
_RESUME_GAME_RE = _re.compile(
    r"^(?:lily )?(?:"
    r"resume(?: the (?:quiz|game|trivia))?"
    r"|continue(?: the (?:quiz|game|trivia))?"
    r"|keep (?:going|playing)"
    r"|go on"
    r"|next question"
    r"|start (?:the (?:quiz|game|trivia) )?again"
    r"|let s (?:resume|continue|keep playing)"
    r")(?: please)?$"
)


def lily_detect_stop(text: str, *, solo: bool = False) -> bool:
    """True when this utterance is an addressed STOP (or a bare stop in a
    solo room) — the runaway-agent brake. Deterministic + garble-tolerant.
    Word-bounded (never 'stopwatch'/'unstoppable') and negation-guarded
    ('don't stop')."""
    normalized = _normalize_command_text(text)
    if not normalized or _STOP_NEGATION_RE.search(normalized):
        return False
    if _QUIT_GAME_RE.search(normalized):
        return True
    if _STOP_ADDRESSED_RE.search(normalized):
        return True
    # Emphatic repetition is roster-independent (see _STOP_REPEAT_RE).
    if _STOP_REPEAT_RE.search(normalized):
        return True
    return bool(solo and _STOP_CORE_RE.search(normalized))


# HOSTLOOP-001 C13 — the STOP EQUIVALENTS ("hold on", "wait", "pause",
# "one sec", "give us a minute/second"). These have never had a user-side
# detector: enter_hold fired only from STOP, declines and Lily's own
# wait-promises, so a spoken "hold on" halted nothing (3.4/F report: STOP
# defied ×4 — the defiance class includes the softer forms). DANGER SHAPE,
# handled: "wait" is ANSWER vocabulary in trivia ("wait, is it Saturn?!")
# — so equivalents fire only UTTERANCE-SHAPED (the request is essentially
# the whole utterance, filler-tolerant), never embedded. "hold on a
# second"/"hold on hold on" fire; "wait, Saturn!" and "hold on, I know
# this one" never do (content after the request word = an answer coming).
_HOLD_REQUEST_CORE_RE = _re.compile(
    r"^(?:"
    r"(?:hold|hang) on(?: (?:a )?(?:sec(?:ond)?|minute|moment|bit))?"
    r"|wait(?: wait)*(?: (?:a )?(?:sec(?:ond)?|minute|moment|bit))?"
    r"|pause(?: (?:it|that|the (?:game|quiz|trivia)))?"
    r"|(?:one|two) sec(?:ond)?s?"
    r"|(?:give (?:us|me) )?(?:a|one) (?:sec(?:ond)?|minute|moment)"
    r")$"
)
_HOLD_REQUEST_FILLER = frozenset({
    "lily", "please", "ok", "okay", "just", "uh", "um", "hey", "no",
})


def lily_detect_hold_request(text: str) -> bool:
    """True when the utterance IS a hold request (C13) — never when the
    request word merely leads into content ("wait, is it Saturn")."""
    normalized = _normalize_command_text(text)
    if not normalized:
        return False
    tokens = normalized.split()
    if not tokens or len(tokens) > 6:
        return False
    core = [t for t in tokens if t not in _HOLD_REQUEST_FILLER]
    if not core:
        return False
    return bool(_HOLD_REQUEST_CORE_RE.fullmatch(" ".join(core)))


def lily_detect_resume_game(text: str) -> bool:
    """True only for an explicit whole-utterance resume after sticky STOP."""
    normalized = _normalize_command_text(text)
    if not normalized:
        return False
    if _re.search(
        r"\b(?:do not|don t|dont|not yet|never)\b.{0,20}"
        r"\b(?:resume|continue|start|go on|next question)\b",
        normalized,
    ):
        return False
    return bool(_RESUME_GAME_RE.match(normalized))


# Hold-narration integrity (WO-LILY-HOTFIX-009 W8) — the stop-ack register
# Lily speaks when she says she has halted: "Stopped. I'm listening.",
# "Still stopped. You say when.", "Stopped until you say go." (all four
# real lily-5E3036 confabulations). A turn in this register asserts a hold
# state; the claim-integrity organ requires that state to actually exist.
# Runs on Lily's OWN outbound text (real casing/punctuation), not user STT,
# so it is not routed through _normalize_command_text.
_HOLD_NARRATION_STATE_RE = _re.compile(r"\bstopped\b", _re.IGNORECASE)
# W8 review Finding 3: a bare `\bstill\b` cue tripped on recap/scorekeeping
# patter ("still tied at two", "still your turn", "still on question four")
# whenever it shared a turn with a past-tense "stopped", entering a hold on
# ordinary speech. Because this latch runs before register_delivery_claim, a
# combined recap+question turn ("You stopped him — still tied. Next: who…")
# then had its legitimate same-turn delivery returned "held". The narration
# register never says "still <anything>" except "still stopped", so the cue
# is bound to that adjacency — the genuine confabulations keep firing (each
# also carries "I'm listening" or a release invitation), recap patter does
# not. Per the coordinator ruling the brake may over-fire recoverably but
# must never suppress a legitimate payload.
_HOLD_NARRATION_CUE_RE = _re.compile(
    r"\bstill\s+stopped\b"
    r"|\bi'?m listening\b"
    r"|\b(?:say (?:the word|when|go)|until you say|you say (?:when|go))\b",
    _re.IGNORECASE,
)


def lily_detect_hold_narration(text: str) -> bool:
    """True when an outbound turn narrates a stopped/hold state — a
    "stopped" assertion paired with a hold cue ("still stopped", "I'm
    listening", a 'say the word / until you say go' release invitation).
    Deliberately conservative: a bare "stopped" with no hold cue ("the
    clock stopped"), or a recap that merely says "still" ("still tied at
    two"), does not fire."""
    if not text:
        return False
    return bool(
        _HOLD_NARRATION_STATE_RE.search(text)
        and _HOLD_NARRATION_CUE_RE.search(text)
    )


# ---------------------------------------------------------------------------
# Age-consent detector (WO-LILY-HOTFIX-004 Defect 1) — the 18+ ceremony's
# DETERMINISTIC floor. The adult-mode tool trusted the model's boolean and
# entered on "Should I verify?" (a question, not consent). This is the
# affirmative the tool now also requires: an explicit spoken YES tied to an
# age/adult anchor. Questions ("should I verify?", "are we all 18?"),
# verification talk, and negations ("under 18", "not all 18") never fire.
# ---------------------------------------------------------------------------

# Reject: interrogatives, verification talk, and negations — none is consent.
_AGE_CONSENT_NEGATION_RE = _re.compile(
    r"\b(?:"
    r"should (?:i|we|you)"          # "should I verify"
    r"|do (?:you|we|i) (?:need|want|have|got)"  # "do you need to verify"
    r"|how (?:do|should|can|would) (?:we|i|you)"
    r"|need(?:s)? to verify|verify|verif|checking|check (?:their|our|your) age"
    r"|are (?:we|you|they)(?: all)?"  # "are we all 18" (a question)
    r"|is everyone|is everybody"
    r"|not (?:all )?(?:18|eighteen|adults?|of age|over)"
    r"|under (?:18|eighteen|age)"
    r"|isn t|aren t|we re not|not sure|don t know"
    r")\b"
)

# Accept: an affirmation anchored to an age/adult token, or a plain
# declarative "(we're / everyone's / all of us) 18 / adults / over 18".
_AGE_CONSENT_RE = _re.compile(
    r"\b(?:"
    r"(?:yes|yeah|yep|yup|yup|confirmed|confirm|correct|absolutely|"
    r"definitely|for sure|we do|i do)\b[a-z0-9 ]*?\b"
    r"(?:18|eighteen|adults?|older|of age|grown|over 18|legal)"
    r"|(?:we re|we are|i m|i am|everyone s|everybody s|all of us(?: are)?)\s+"
    r"(?:all\s+)?(?:18|eighteen|adults?|over 18|of age|grown|legal)"
    r"|all (?:of us )?(?:are )?(?:18|eighteen|adults|over 18)"
    r"|(?:18|eighteen) (?:plus|or older|or over|and older|and up)"
    r")\b"
)


def lily_detect_age_consent(text: str) -> bool:
    """True only for an explicit, affirmative adult-age declaration.

    Accepts the original 18+ affirmatives plus a first-person adult age or
    birth year. Questions, verification talk, and negations remain false.
    The deterministic result is the gate; a model boolean is not a second
    ceremony.
    """
    normalized = _normalize_command_text(text)
    if not normalized or _AGE_CONSENT_NEGATION_RE.search(normalized):
        return False
    if _AGE_CONSENT_RE.search(normalized):
        return True
    if _re.search(
        r"\b(?:i m|i am|we re|we are)\s+"
        r"(?:explicitly\s+)?(?:above|over)\s+(?:the age of\s+)?18\b",
        normalized,
    ):
        return True
    age_match = _re.search(
        r"\b(?:i m|i am|my age is)\s+(\d{2,3})"
        r"(?:\s+years?\s+old)?\b",
        normalized,
    )
    if age_match:
        age = int(age_match.group(1))
        if 18 <= age <= 120:
            return True
    year_match = _re.search(
        r"\b(?:i (?:was|am) )?born in ((?:19|20)\d{2})\b",
        normalized,
    )
    if year_match:
        year = int(year_match.group(1))
        current_year = datetime.now(timezone.utc).year
        # Birth year alone lacks month/day. Require the year that makes 18+
        # unambiguous for the entire current year; boundary year re-asks.
        if current_year - 120 <= year <= current_year - 19:
            return True
    return False


# ---------------------------------------------------------------------------
# Camera-lane request (WO-LILY-VIDEOIN-001 V1) — the explicit player action
# that opens the sparse show-and-tell lane ("look at this", "can you see
# this"). The camera NEVER publishes without this (or the UI control), so
# the detector is the spoken half of the user-initiated gate. Anchored to a
# SHOW/SEE verb + a deictic (this/here/it/my ...), so "look at the score" or
# "can you see question three" never opens the camera.
# ---------------------------------------------------------------------------

_CAMERA_REQUEST_RE = _re.compile(
    r"\b(?:"
    r"look at (?:this|these|here|it|my |what)"
    r"|(?:can|could|will) you (?:see|look at) (?:this|these|it|my |what)"
    r"|(?:do|can) you see (?:this|these|it|my |what)"
    r"|check (?:this|these) out"
    r"|(?:let me|i(?:'| )?ll|i want to|wanna) show you"
    r"|watch this|see this|look here"
    r"|(?:turn|switch) (?:on )?(?:the )?camera"
    r"|use (?:the |your )?camera"
    r")\b"
)

# Not a camera request: asking her to look at GAME state, not a held object.
_CAMERA_REQUEST_NEGATION_RE = _re.compile(
    r"\b(?:"
    r"look at (?:the )?(?:score|board|question|screen|time|clock|category)"
    r"|see (?:the )?(?:score|board|question|answer)"
    r"|turn off (?:the )?camera|camera off|no camera"
    r")\b"
)


def lily_detect_camera_request(text: str) -> bool:
    """True when a player explicitly asks Lily to look through the camera —
    the spoken trigger that opens the sparse show-and-tell lane. Deictic-
    anchored and negation-guarded (game-state 'look at the score' and
    'camera off' never fire)."""
    normalized = _normalize_command_text(text)
    if not normalized or _CAMERA_REQUEST_NEGATION_RE.search(normalized):
        return False
    return bool(_CAMERA_REQUEST_RE.search(normalized))


# ---------------------------------------------------------------------------
# Explain-on-request (WO-LILY-HOTFIX-005 X12) — a player asking for the
# ACTIVE question to be restated in plain language. The live failure: the
# operator asked twice (14:40:27, 14:40:44) and never got a restatement,
# then Lily claimed she "can and will" explain. Anchored to an explain/
# rephrase verb + a question/that/this/it reference, so "explain the rules"
# and "what do you mean, we're tied?" don't fire.
# ---------------------------------------------------------------------------

_EXPLAIN_REQUEST_RE = _re.compile(
    r"\b(?:"
    r"explain (?:the |that |this |your )?question"
    r"|(?:can|could|will|would) you explain (?:the |that |this |it)"
    r"|what (?:does|do) (?:that|this|the question|it) mean"
    r"|what do you mean by (?:the |that |this )?question"
    r"|i (?:don t|do not|can t|cannot) (?:get|understand|follow) "
    r"(?:the |that |this )?question"
    r"|(?:re ?phrase|reword|clarify|break down|unpack) "
    r"(?:the |that |this )?question"
    r"|say (?:the question|that|it) (?:again|differently|another way)"
    r"|(?:what s|what is) the question(?: again)?"
    r"|in (?:plain|simple) (?:english|terms|words)"
    r"|come again(?: on that)?"
    r")\b"
)


def lily_detect_explain_request(text: str) -> bool:
    """True when a player asks for the ACTIVE question to be restated in
    plain language (X12). Anchored to an explain/rephrase verb + a question
    reference so game-state and rules questions never fire."""
    normalized = _normalize_command_text(text)
    if not normalized:
        return False
    return bool(_EXPLAIN_REQUEST_RE.search(normalized))


# ---------------------------------------------------------------------------
# Verdict contest (WO-LILY-HOTFIX-005 X12) — a player asserting they were
# misheard or that their answer was in fact correct, contesting a ruling.
# The live failure: the operator said "the correct answer is A" three times
# (14:39:27, 14:39:59) and was told "we're past that one either way." A
# contest earns ONE re-check against the committed record. Anchored to a
# self-reference + a mishear/correction cue so a fresh answer ("the answer
# is Paris") to a LIVE question does not read as a contest.
# ---------------------------------------------------------------------------

_VERDICT_CONTEST_RE = _re.compile(
    r"\b(?:"
    r"you (?:misheard|didn t hear|mis heard|got me wrong)"
    r"|i (?:did |actually |already )?(?:said|say|answered)\b[a-z0-9 ']*?"
    r"(?:not|correct|right|it)"
    r"|(?:that s|thats|that is) (?:wrong|not right|incorrect|not what i said)"
    r"|i (?:was|am) (?:right|correct)"
    r"|i (?:did |actually )?(?:get|got) (?:it|that) right"
    r"|(?:the )?(?:correct )?answer (?:is|was) (?:a|b|c|d)\b"
    r"|i (?:should have|shoulda) (?:got|gotten) (?:the|that|a) point"
    r"|(?:check|go back to|review) (?:the|that|my) (?:answer|last one)"
    # W1 (HOTFIX-009) rule-violation contest — the diamond form: the player
    # disputes not that they were misheard but that a RULE was misapplied
    # (a clock on a relaxed round) and the score held wrongly. Anchored to a
    # rules/clock reference so trivia content never fires.
    r"|you violated (?:the )?rules?"
    r"|we (?:had )?agreed (?:on |to )?(?:no timer|relaxed)"
    r"|you (?:put|threw|tossed|dinged) (?:me )?(?:on )?(?:a |the )?(?:timer|clock)"
    r"|(?:keep|keeping|held|holding|hold) (?:my|me) (?:score|point)"
    r")\b"
)


def lily_detect_verdict_contest(text: str) -> bool:
    """True when a player contests the last ruling — asserting they were
    misheard or that their answer was correct (X12). Anchored so a fresh
    answer to a live question is not mistaken for a contest."""
    normalized = _normalize_command_text(text)
    if not normalized:
        return False
    return bool(_VERDICT_CONTEST_RE.search(normalized))


def lily_detect_control_command(text: str) -> Optional[str]:
    """
    Detect a sticky player command in an utterance.
    Returns "back_to_normal", "forget_me", "pacing_relaxed",
    "pacing_timed", "skip", "start_game", or None.

    Punctuation-proof: "Back. To normal." and "back to... normal" both fire.
    "forget_me" fires on paraphrase-tolerant deletion requests ("forget
    me/us", "delete what you know about us", "erase my data" — negated
    forms like "don't forget us" never fire) and takes priority over
    "skip" ("forget us and skip this one" is a deletion request).
    "skip" fires as a standalone word ("skip", "can we skip this one"),
    never inside other words ("skipper" does not fire). Pacing choices
    ("let's play relaxed", "timed rounds", "no timer" — group prefs WO)
    fire "pacing_relaxed"/"pacing_timed" and are checked BEFORE
    start_game, so "let's play relaxed" is a pacing choice, not a game
    start. "start the game" / "let's start" / "let's play" / "start round
    one" fire "start_game" (the agent layer ignores it once the game is
    running).
    """
    normalized = _normalize_command_text(text)
    if not normalized:
        return None
    if "back to normal" in normalized:
        return "back_to_normal"
    if _FORGET_RE.search(normalized) and not _FORGET_NEGATION_RE.search(normalized):
        return "forget_me"
    pacing = lily_detect_pacing_choice(normalized)
    if pacing:
        return pacing
    if _re.search(r"\bskip\b", normalized):
        return "skip"
    if _START_GAME_RE.search(normalized):
        return "start_game"
    if lily_is_bare_start_intent(normalized):
        return "start_game"  # C7: "Starts." is a start, deterministically
    return None


# ---------------------------------------------------------------------------
# Returner-claim detector (WO-LILY-RECOGNITION-HONESTY-001) — a player
# asserting prior contact or that Lily should recognize them, arriving in
# ANY turn (not just the greeting landing). It exists to trip the honesty
# gate: when memory is empty, Lily must never DENY prior contact or argue
# with their memory ("we haven't played before" / "your voice isn't on
# file" / "I promise my system doesn't have you saved" — the 19:41 live
# failure). Conservative: a recognition/prior-contact VERB anchored to a
# SELF/us reference, so "do you know the answer" and "you should know this
# category" never fire.
# ---------------------------------------------------------------------------

_RETURNER_CLAIM_RE = _re.compile(
    r"\b(?:"
    # "do you know / remember / recognize who I am / me / my voice / us"
    r"(?:do|don t|dont) you (?:know|remember|recogni[sz]e)"
    r" (?:who (?:i|we) (?:am|are)|me|us|my voice|our voices)"
    # "you know / remember / recognize me / my voice / who I am"
    r"|you (?:should |ought to |do |must )?(?:know|remember|recogni[sz]e)"
    r" (?:me|us|my voice|our voices|who (?:i|we) (?:am|are))"
    # "you know my voice" phrased as a bare assertion
    r"|you know my voice"
    # "remember me / us"
    r"|remember (?:me|us)\b"
    # "we've met / played before", "I played with you before"
    r"|(?:we|i) (?:have |ve |'ve )?(?:met|played)(?: with you)? before"
    r"|(?:we|i) (?:have |ve |'ve )?(?:played|met) (?:with you )?(?:before|last time)"
    # BE8D8B live regression: "I certainly have been on your table before."
    # This is an explicit returner assertion even though it avoids
    # met/played/remember vocabulary.
    r"|(?:we|i) (?:certainly )?(?:have |ve |'ve )?been "
    r"(?:on|at) (?:your|this) table before"
    # "we've crossed paths", "we know each other"
    r"|we (?:have |ve |'ve )?crossed paths"
    r"|we know each other"
    # "it's not my/our first time" — an explicit returner assertion
    r"|(?:it s|its|it is) not (?:my|our) first time"
    r"|not (?:my|our) first (?:time|game)"
    # contracted negation ("isn't/aren't/wasn't our first time" — the
    # apostrophe normalizes to a space, so "isn't" arrives as "isn t")
    r"|(?:isn|aren|wasn|weren) t (?:my|our) first (?:time|game)"
    r")\b"
)

# Negations that turn a match into a NON-claim: "you don't know me" is the
# player conceding, not asserting; "this is my first time" is a newcomer.
_RETURNER_CLAIM_NEGATION_RE = _re.compile(
    r"\b(?:"
    r"(?:this is|it s|its|it is) (?:my|our) first time"
    r"|you (?:don t|dont|do not) (?:know|remember|recogni[sz]e) (?:me|us)"
    r"|(?:we|i) (?:have|ve|'ve)? ?(?:never|not) (?:met|played)"
    r")\b"
)


def lily_detect_returner_claim(text: str) -> bool:
    """True when the utterance ASSERTS prior contact / that Lily should
    recognize this speaker. Trips the honesty gate (agent layer) only when
    memory is empty — a grounded, never-denying response. Deterministic,
    negation-guarded, self-anchored to avoid trivia-content false hits."""
    normalized = _normalize_command_text(text)
    if not normalized:
        return False
    if _RETURNER_CLAIM_NEGATION_RE.search(normalized):
        return False
    return bool(_RETURNER_CLAIM_RE.search(normalized))


# ---------------------------------------------------------------------------
# Recognition dispute (live 2026-08-09 Rami session): after a false clean
# slate, "why did you say blank / still pulling?" must lock kickoff until
# one honest why-answer lands. Deterministic; not trivia-content.
# ---------------------------------------------------------------------------

_RECOGNITION_DISPUTE_RE = _re.compile(
    r"\b(?:"
    r"why (?:did|do|would) you (?:say|tell|claim|call)"
    r"|why .{0,40}(?:clean slate|blank|nothing on file|no stats|no record|empty)"
    r"|how come (?:you )?(?:said|say|told)"
    r"|you said .{0,40}(?:clean slate|blank|nothing|no stats|empty|first)"
    r"|why didn t you say (?:you were )?still"
    r"|still (?:pulling|loading|checking)"
    r"|why .{0,20}(?:protocol|determin)"
    r")\b"
)


def lily_detect_recognition_dispute(text: str) -> bool:
    """True when the player challenges a false empty-memory / clean-slate
    claim or asks why she spoke as if the record were final."""
    normalized = _normalize_command_text(text)
    if not normalized:
        return False
    return bool(_RECOGNITION_DISPUTE_RE.search(normalized))


# ---------------------------------------------------------------------------
# Media-mode spoken choice (WO-LILY-OMNIBUS-002 sub-agent K) — the lobby
# offer: voice_only (default) or pictures. Same deterministic,
# punctuation/fragment-proof command-layer pattern as the sticky commands
# above; the flag is sticky and flips instantly in code, never by the LLM.
# ---------------------------------------------------------------------------

_MEDIA_PICTURES_RE = _re.compile(
    r"\b(?:"
    r"pictures? on"
    r"|(?:turn|put) (?:the )?pictures? (?:on|up)"
    r"|with (?:the )?pictures?"
    r"|pictures? in(?:\s+\w+){0,3}\s+mode"
    r"|pictures? (?:and|with) mix"
    r"|picture rounds?"
    r"|picture (?:trivia|lane|bank)"
    r"|use the screen"
    r"|screen on"
    r"|(?:get|make|switch|turn) .{0,24}(?:images?|pictures?) (?:on|live|up)"
    r"|(?:images?|pictures?) live"
    r"|live (?:with )?(?:the )?(?:images?|pictures?)"
    r")\b"
)

# Affirmative / go-live replies after Lily offers to switch pictures on
# ("want them on?", "images live first?"). Deterministic — not LLM whim.
_MEDIA_PICTURE_ON_CONFIRM_RE = _re.compile(
    r"^\s*(?:"
    r"y(?:es|eah|ep|up)|sure|ok(?:ay)?|alright|all right"
    r"|yes(?:,?\s*(?:ma'?am|sir|please|i am|i do|i will))?"
    r"|live(?:\s+immediately)?"
    r"|(?:turn|switch|put) (?:them|it|pictures?|images?) on"
    r"|get (?:them|it|pictures?|images?) (?:on|live)"
    r")\.?\s*$",
    _re.IGNORECASE,
)

# Lily's spoken offer to turn pictures on while media_mode is still off.
_MEDIA_PICTURE_ON_OFFER_RE = _re.compile(
    r"(?:"
    r"want (?:them|pictures?|images?) on"
    r"|not switched on yet"
    r"|pictures? (?:are )?(?:not|aren'?t) (?:switched )?on"
    r"|get the images? live"
    r"|images? live first"
    r"|picture lane .{0,40}not"
    r")",
    _re.IGNORECASE,
)

_MEDIA_VOICE_ONLY_RE = _re.compile(
    r"\b(?:"
    r"voice only"
    r"|no pictures?"
    r"|without (?:the )?pictures?"
    r"|pictures? off"
    r"|turn (?:the )?pictures? off"
    r"|screen off"
    r")\b"
)

# A REFUSED picture/screen is a voice_only request — same asymmetry the W3
# pacing fix closes: the pictures-enable patterns key on "with pictures" /
# "images live", so a verb-separated refusal ("I'm not playing with
# pictures", "I don't want the pictures / the picture deck", "stop with the
# images") would otherwise hit the enable substring and turn the screen ON.
# Built from the shared refusal builder; checked FIRST in
# lily_detect_media_choice so OFF wins the collision.
_MEDIA_REFUSAL_RE = _build_refusal_re(
    r"picture|image|screen|picture (?:deck|round|lane)"
)


def lily_detect_media_choice(text: str) -> Optional[str]:
    """
    Detect a spoken media-mode choice in an utterance.
    Returns "pictures", "voice_only", or None.

    Punctuation-proof like lily_detect_control_command. The OFF direction
    wins a collision ("no pictures on the screen please" -> voice_only) —
    turning the screen off must never lose to a substring that turns it on.
    """
    normalized = _normalize_command_text(text)
    if not normalized:
        return None
    if _MEDIA_REFUSAL_RE.search(normalized) or _MEDIA_VOICE_ONLY_RE.search(normalized):
        return "voice_only"
    if _MEDIA_PICTURES_RE.search(normalized):
        return "pictures"
    return None


def lily_detect_picture_on_offer(text: str) -> bool:
    """True when Lily offered to switch pictures on (lane healthy, off)."""
    return bool(_MEDIA_PICTURE_ON_OFFER_RE.search(text or ""))


def lily_is_picture_on_confirm(text: str) -> bool:
    """True for a short yes / live / turn-them-on after a picture-on offer."""
    raw = (text or "").strip()
    if not raw or len(raw) > 48:
        return False
    # Explicit voice-only wins — never treat "no pictures" as a confirm.
    if lily_detect_media_choice(raw) == "voice_only":
        return False
    return bool(_MEDIA_PICTURE_ON_CONFIRM_RE.match(raw))


# ---------------------------------------------------------------------------
# Published-state honesty assist (WO-LILY-DESYNC-HONESTY-001 Sub-agent C)
#
# Live evidence, twice: at 01:37 a player said "my score is not updating,
# it's still showing zero" and Lily invented mechanisms ("the digital
# board takes a second to refresh once I submit to the database" — false,
# nothing had committed); at 23:04 she narrated "you should actually have
# three points" — ungrounded validation of a player's complaint. The
# assist is deterministic: when a player's utterance makes a checkable
# claim about their published score, the agent layer injects ONE
# grounded [state note: …] line built from the score the scorekeeper
# actually committed — her acknowledgment is grounded, never guessed.
# The note is context, never speech (the say-gate leak filter strips it
# if echoed). Detection is conservative: it requires a score/board
# anchor word plus either a concrete value claim or a stuck/desync
# phrase, so table talk never fires it.
# ---------------------------------------------------------------------------

_STATE_ANCHOR_RE = _re.compile(
    r"\b(?:scores?|points?|board|scoreboard|screen)\b"
)

_SCORE_NUMBER_WORDS = {
    "zero": 0, "nothing": 0, "none": 0, "one": 1, "two": 2, "three": 3,
    "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10,
}
_SCORE_NUMBER_PATTERN = (
    r"(zero|nothing|none|one|two|three|four|five|six|seven|eight|nine|"
    r"ten|\d+)"
)

# A concrete value claim about the player's own score / the board:
# "still showing zero", "it says two", "stuck at zero", "i have one
# point", "i should have three points", "my score is zero".
_SCORE_VALUE_CLAIM_RE = _re.compile(
    r"\b(?:"
    r"(?:still\s+)?(?:showing|shows|says|reads|reading|displaying)"
    r"|stuck\s+(?:at|on)"
    r"|still\s+(?:at|on)"
    r"|(?:i|we)\s+(?:only\s+)?(?:have|has|got)"
    r"|should\s+(?:actually\s+)?(?:have|be\s+at|show|say)"
    r"|score\s+is(?:\s+still)?"
    r"|it\s+s\s+still"
    r")\s+(?:a\s+|an\s+|like\s+)?" + _SCORE_NUMBER_PATTERN +
    r"(?:\s+points?)?\b"
)

# A stuck/desync callout with no concrete value: "my score is not
# updating", "the board isn't moving", "the scoreboard is behind/frozen/
# wrong", "it never updated".
_SCORE_STUCK_RE = _re.compile(
    r"\b(?:"
    r"(?:not|isn\s?t|hasn\s?t|didn\s?t|don\s?t|doesn\s?t|never|won\s?t|"
    r"stopped)\s+(?:been\s+)?(?:updat\w*|mov\w*|chang\w*|count\w*|"
    r"register\w*|show\w*)"
    r"|(?:behind|frozen|stuck|wrong|stale|lagg\w*)\b"
    r")"
)


def _extract_claimed_score(normalized: str) -> Optional[int]:
    m = _SCORE_VALUE_CLAIM_RE.search(normalized)
    if not m:
        return None
    token = m.group(1)
    if token.isdigit():
        return int(token)
    return _SCORE_NUMBER_WORDS.get(token)


# How LILY narrates a player's score (distinct from the player-claim regex
# above): "you're at 13", "you've got 13", "you have 13 points", "that puts
# you at 13", "you're up to 13", "sitting at 13", "score is 13", "now on 13".
_NARRATED_SCORE_RE = _re.compile(
    r"\b(?:"
    r"you\s+(?:re|are|ve|have|had)\s+(?:(?:got|now|up\s+to|at|on)\s+)?"
    r"|puts?\s+you\s+(?:at|on|up\s+to)\s+"
    r"|(?:sitting|standing|now)\s+(?:at|on)\s+"
    r"|score\s+is\s+(?:now\s+)?"
    r")(?:a\s+|an\s+)?" + _SCORE_NUMBER_PATTERN + r"(?:\s+points?)?\b"
)


def lily_narrated_score_divergence(
    text: str, ledger_scores: dict
) -> Optional[dict]:
    """HOTFIX-005 X1: SCORE_DIVERGENCE detector. If Lily's OWN outbound line
    NARRATES a score that matches NO player's committed ledger total, the
    number is fabricated — return {spoken, ledger_values} for an ERROR log.
    None when no score is narrated or the number is a real ledger total. The
    ledger (and the state block + glass that project it) is the only truth;
    this is the safety net catching the model speaking a number off-ledger."""
    if not ledger_scores:
        return None
    normalized = _normalize_command_text(text)
    if not normalized:
        return None
    m = _NARRATED_SCORE_RE.search(normalized)
    if not m:
        return None
    token = m.group(1)
    spoken = int(token) if token.isdigit() else _SCORE_NUMBER_WORDS.get(token)
    if spoken is None:
        return None
    values = set(int(v) for v in ledger_scores.values())
    if spoken in values:
        return None
    return {"spoken": spoken, "ledger_values": sorted(values)}


# How Lily narrates a VERDICT about a named player's answer (distinct from
# the score-value regexes above). WO-LILY-HOTFIX-006 N9 part 3: at 21:10 she
# said "Jupiter was spot on, Rami, but just a split second late!" while the
# committed q_1052 ledger row for Rami read incorrect, transcript "Go.".
# The conversational lane KNEW the answer was right and who gave it; the
# ledger recorded a different utterance as wrong. Narration may not
# contradict the ledger — on disagreement the ledger wins.
_VERDICT_CORRECT_CUES = (
    "spot on", "you got it", "you nailed", "nailed it", "that s correct",
    "is correct", "was correct", "correct", "exactly right", "dead right",
    "you re right", "that s right", "was right", "got there", "well done",
    "bang on", "on the money",
)
# Miss-side cues. A verdict that NEGATES correctness is not a claim of
# correctness, however many "right"s it contains ("not quite right").
# HOTFIX-006 N12 adds the no-points family: the second lane's contradiction
# in lily-D99BE7 was "No points on that one — the answer was Russia!" over a
# committed CORRECT row, and nothing in this table matched it.
_VERDICT_MISS_RE = _re.compile(
    r"\b(?:not\s+quite|nobody\s+(?:got|landed)|no\s+one\s+(?:got|landed)"
    r"|wasn\s?t\s+(?:it|right|correct)|isn\s?t\s+(?:it|right|correct)"
    r"|not\s+(?:it|right|correct)|missed\s+it|off\s+by|afraid\s+not"
    r"|no\s+(?:points?|score)|zero\s+points?"
    r"|sorry\b)"
)


def lily_narrated_verdict_divergence(
    text: str, score_ledger: Optional[list]
) -> Optional[dict]:
    """HOTFIX-006 N9 part 3: SCORE_DIVERGENCE for VERDICTS. If Lily's own
    outbound line tells a NAMED player their answer was correct while that
    player's committed ledger row for the question says otherwise, the
    narration is off-ledger — return {player, spoken, ledger, entry} for an
    ERROR log. None when no verdict is narrated about a named player, when
    the line rules a miss, or when narration and ledger agree.

    A verdict spoken about a player's answer must GENERATE from that
    player's ledger row. This is the safety net for when it doesn't: the
    ledger wins and the divergence is made loud, exactly as the X1 score
    detector does for narrated totals."""
    if not score_ledger:
        return None
    normalized = _normalize_command_text(text)
    if not normalized:
        return None
    if _VERDICT_MISS_RE.search(normalized):
        # She is narrating a MISS. A ledger row marked incorrect agrees
        # with that, and a correct row would be a different (opposite)
        # defect than the one this detector exists for.
        return None
    if not any(cue in normalized for cue in _VERDICT_CORRECT_CUES):
        return None
    # The most recent adjudicated row per player is the one a verdict beat
    # can be about; a name in the line binds the claim to that player.
    latest: dict[str, dict] = {}
    for entry in score_ledger:
        if entry.get("cause") != "answer":
            continue
        name = entry.get("player")
        if name:
            latest[name] = entry
    for name, entry in latest.items():
        needle = _normalize_command_text(str(name))
        if not needle or not _re.search(rf"\b{_re.escape(needle)}\b", normalized):
            continue
        if entry.get("correct") is True:
            continue
        return {
            "player": name,
            "spoken": "correct",
            "ledger": "incorrect",
            "question_id": entry.get("question_id"),
            "ledger_transcript": entry.get("transcript"),
            "utterance_id": entry.get("utterance_id"),
        }
    return None


def lily_verdict_narration(
    text: str, canonical_answer: Optional[str] = None
) -> Optional[str]:
    """HOTFIX-006 N12: does THIS outbound turn NARRATE a question's verdict?

    Returns "correct", "miss", or None. The caller (the transition gate)
    uses it to decide whether a turn is a SECOND narration of a transition
    that has already been narrated once.

    THE evidence, one beat apart in lily-D99BE7 over the same committed
    row (q_8294, Chris, "Russia.", correct, 1 point):

        "Chris got it in right on time with Russia! That's a point for
         Chris."                                              -> "correct"
        "No points on that one — the answer was Russia!"       -> "miss"

    Binding to the QUESTION is what keeps this narrow: when
    canonical_answer is given, the answer has to appear in the turn, so
    encouragement, banter and answers to the table ("Nice hustle,
    Rhonda") are never verdicts. Same shape as LilyGame's
    _verdict_already_spoken, which asks the question of her PREVIOUS turn;
    this one is pure, takes any text, and reports which side was
    narrated — which is how a contradiction becomes visible."""
    normalized = _normalize_command_text(text)
    if not normalized:
        return None
    if canonical_answer:
        answer = _normalize_command_text(str(canonical_answer))
        if not answer or answer not in normalized:
            return None
    if _VERDICT_MISS_RE.search(normalized):
        return "miss"
    if any(cue in normalized for cue in _VERDICT_CORRECT_CUES):
        return "correct"
    # Reveal-shaped without a verdict word: "the answer was Russia" is
    # still a narration of the ruling (it puts the answer on the air).
    if _re.search(r"\b(?:the\s+)?answer\s+(?:is|was)\b", normalized):
        return "miss"
    if _re.search(r"\bpoint\s+(?:for|to|goes)\b|\bon\s+the\s+board\b", normalized):
        return "correct"
    return None


# ---------------------------------------------------------------------------
# HOTFIX-006 N13 — the spoken ROSTER COUNT is read, never computed.
#
# Live: "Whenever you four..." spoken to a table of THREE, in the same
# breath that correctly named Rami, Rhonda and Chris. She had the names and
# still generated the number. Exactly the HOTFIX-005 X1 disease (narrated
# 13 against a committed 9): the count is state, and state is READ.
#
# The detector below is the X1 safety net's twin — the state block injects
# the authoritative count (LilyGame._roster_authority_line), and any spoken
# count that disagrees with the enrolled roster is made loud at ERROR.
# ---------------------------------------------------------------------------

_ROSTER_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}
_ROSTER_NUMBER_PATTERN = (
    r"(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten)"
)
# Nouns that make a number something OTHER than a count of people. Without
# this, "I'll give you three seconds" reads as a roster claim.
_ROSTER_NOT_PEOPLE = (
    r"(?!\s+(?:seconds?|minutes?|points?|questions?|rounds?|answers?|"
    r"options?|choices?|clues?|hints?|guesses|tries|attempts?|letters?|"
    r"categories|category|of\s+(?:six|ten|those|these|them)))"
)
# Only PERSON-COUNT constructions. Each pattern carries exactly one group.
_ROSTER_COUNT_RES = (
    # "Whenever you four are ready" — the live line.
    _re.compile(r"\byou\s+" + _ROSTER_NUMBER_PATTERN + r"\b" + _ROSTER_NOT_PEOPLE),
    # "the four of you", "all 4 of you", "both of you" handled by the count.
    _re.compile(_ROSTER_NUMBER_PATTERN + r"\s+of\s+you\b"),
    # "Four players, three rounds"
    _re.compile(_ROSTER_NUMBER_PATTERN + r"\s+players?\b"),
)


def lily_narrated_roster_count_divergence(
    text: str, roster_names: Optional[list]
) -> Optional[dict]:
    """HOTFIX-006 N13: ROSTER_DIVERGENCE detector. If Lily's OWN outbound
    line speaks a COUNT OF PLAYERS that is not the enrolled roster size,
    the number was generated — return {spoken, roster, names} for an ERROR
    log. None when no player count is spoken, when the count is right, or
    when nobody is enrolled yet (an empty roster has no truth to violate).

    Deliberately narrow: only person-count constructions match, and
    counts of seconds / points / questions / rounds never do. A false flag
    would poison the telemetry this exists to provide."""
    if not roster_names:
        return None
    normalized = _normalize_command_text(text)
    if not normalized:
        return None
    roster = len(roster_names)
    for pattern in _ROSTER_COUNT_RES:
        m = pattern.search(normalized)
        if not m:
            continue
        token = m.group(1)
        spoken = (
            int(token) if token.isdigit() else _ROSTER_NUMBER_WORDS.get(token)
        )
        if spoken is None or spoken == roster:
            continue
        return {
            "spoken": spoken,
            "roster": roster,
            "names": list(roster_names),
        }
    return None


def lily_detect_state_contradiction(
    text: str,
    player_name: Optional[str],
    players: dict,
) -> Optional[str]:
    """Deterministic honesty assist: does this utterance make a checkable
    claim about the player's published score? Returns the grounded note
    body (the agent layer wraps it as `[state note: …]` and injects it
    into the next turn's context), or None.

    Grounding, off the score the scorekeeper committed and published:
      - the claim MATCHES committed state -> "player is correct — …"
        (the live class: she narrated a point that never committed; the
        player and the board are right, she must not invent a refresh);
      - the claim DIFFERS from committed state -> the committed number,
        with an explicit instruction never to validate an uncommitted
        one (the 23:04 "three points" class);
      - a stuck/desync callout with no number -> the committed number,
        so the acknowledgment is grounded either way.
    Pure; requires a resolved rostered player."""
    if not text or not player_name:
        return None
    state = players.get(player_name)
    if state is None:
        return None
    normalized = _normalize_command_text(text)
    if not normalized or not _STATE_ANCHOR_RE.search(normalized):
        return None
    committed = int(state.get("score", 0))
    claimed = _extract_claimed_score(normalized)
    if claimed is not None:
        if claimed == committed:
            return (
                f"player is correct — {player_name}'s committed score is "
                f"{committed}; nothing more has been committed. Acknowledge "
                "honestly and re-sync; never invent a mechanism for the gap"
            )
        return (
            f"player says {claimed} but {player_name}'s committed score is "
            f"{committed} — that committed number is the only score truth; "
            "speak to it and never validate a number the scorekeeper has "
            "not committed"
        )
    if _SCORE_STUCK_RE.search(normalized):
        return (
            f"player flagged the board — {player_name}'s committed score "
            f"is {committed}; that is the truth to speak to. Acknowledge "
            "honestly and re-sync; never invent a mechanism for the gap"
        )
    return None


# ---------------------------------------------------------------------------
# Scorekeeper
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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


class LilyScorekeeper:
    """Pure local game state for one Lily session."""

    def __init__(
        self,
        session_id: str,
        answer_window_seconds: float = DEFAULT_ANSWER_WINDOW_SECONDS,
    ) -> None:
        self.session_id = session_id
        self.answer_window_seconds = answer_window_seconds

        # Roster: player_name -> per-player state (spec Part II §2.1)
        self.players: dict[str, dict] = {}

        # Game-level state
        self.phase: str = "lobby"          # lobby | round | final | wrapup
        self.round: int = 0
        self.mode: str = "general"         # general | adult (sticky flag)
        # Pacing (group prefs WO): "timed" (the standard clock — exactly
        # today's behavior) | "relaxed" (looser tempo; the agent layer
        # stretches the answer window by LILY_RELAXED_WINDOW_MULTIPLIER).
        # Sticky flag like mode; published as the participant attribute
        # `pacing` (a seam addition) and persisted per group as the
        # "pacing" key of the opaque lily_group_prefs dict.
        self.pacing: str = "timed"
        # PATCH-003 P7: delivery rate (normal | slow). Independent of
        # `pacing` (game-clock speed) — this is how fast she TALKS.
        self.delivery_pace: str = "normal"
        # Round format (multiple-choice WO): the CURRENT round's format,
        # plus the sticky explicit override set by lily_set_round_format
        # (None = follow the default schedule: round DEFAULT_MC_ROUND runs
        # multiple choice, every other round freeform).
        self.round_format: str = "freeform"  # freeform | multiple_choice
        self.round_format_override: Optional[str] = None
        # Lobby media choice (sub-agent K): voice_only | pictures — sticky
        # flag, default voice_only. Picture questions exist ONLY when this
        # says pictures.
        self.media_mode: str = "voice_only"
        # VIDEOIN-001: the sparse camera lane (show-and-tell). "off" (DEFAULT)
        # — the camera never publishes until a player explicitly opens it;
        # "open" while a look-at-this exchange is live. Transient: the lane
        # auto-closes after the exchange, nothing is retained, and it is
        # STRUCTURALLY unavailable in the adult deck (set_mode('adult')
        # forces it off — an open camera and adult content never coexist).
        self.camera_lane: str = "off"
        # Adult image intensity (player-chosen): suggestive | explicit.
        # Default suggestive until the table confirms explicit. Cleared to
        # suggestive on every adult exit so there is no residue.
        self.adult_image_intensity: str = "suggestive"
        self.category: Optional[str] = None
        self.question_number: int = 0
        self.questions_per_round: int = 6
        self.rounds_total: int = 3
        self.current_question: Optional[dict] = None
        self.current_answer: Optional[str] = None

        # Answer window
        self.answer_window_open: bool = False
        self.answer_window_opened_at: Optional[float] = None
        self.answer_window_deadline: Optional[float] = None
        # player_name (or "unrostered:<label>") -> candidate dict
        self.answer_candidates: dict[str, dict] = {}
        # WO-LILY-HOTFIX-006 N3 — the question this window belongs to,
        # captured AT OPEN and deliberately NOT cleared at close: the
        # adjudication commit and the late-answer path both read it after
        # the window is gone. `registered` is false for a window that was
        # never opened over a question the scorekeeper holds; nothing
        # recorded in an unregistered window is adjudicable.
        self.answer_window_question_id: Optional[str] = None
        self.answer_window_question_index: Optional[int] = None
        self.answer_window_registered: bool = False
        self._last_window_deadline: Optional[float] = None
        # Monotonic per-session counter behind the minted utterance ids
        # (N9): capture binds an UTTERANCE, never a slot, so every final
        # carries an id even when the STT event supplies none.
        self._utterance_seq: int = 0
        # Late-but-correct answers past the grace margin (N9 part 2) —
        # a DEFINED outcome, recorded here so it can be announced and
        # audited instead of silently lost.
        self.late_answers: list[dict] = []

        # Prior-state inputs (WO-ADDRESSEE-H1 Task 2). host_speaking is SET
        # by the agent layer on the framework's agent-state transitions
        # (TTS playback start/end) — the scorekeeper stays pure, it only
        # holds the flag. adjudicating mirrors LilyGame._adjudicating for
        # the same reason. overlap_flag flips on diarization-timestamp
        # overlap inside the open window (deliberation prior) and persists
        # until the NEXT window opens, so adjudication of the just-closed
        # window still sees it.
        self.host_speaking: bool = False
        self.adjudicating: bool = False
        self.overlap_flag: bool = False
        # speaker identity -> [(segment_start, segment_end), ...] recorded
        # inside the current window only; cleared at every window open.
        self._window_speaker_spans: dict[str, list[tuple[float, float]]] = {}
        # Fused addressee-confidence values recorded during the active
        # answer window (Speechmatics diarization + acoustic read). The
        # scorekeeper's thresholding uses the window aggregate additively on
        # top of PRIOR_* thresholds.
        self._window_addressee_confidences: list[float] = []
        self._last_addressee_confidence: float | None = None

        # Rolling tagged transcript buffer (last 30 lines)
        self.transcript_buffer: list[dict] = []

        # WO-LILY-RECOGNITION-VARIETY-001 Task 0/3 — the agent's OWN turns.
        # agent_turns: rolling final-spoken-text record (feeds the say-gate
        # repeat lint and the report); the SAID-ALREADY ledger tracks what
        # she has already delivered this session so nothing gets re-served:
        # praise words used, turn openers used, feature/rule topics already
        # explained. All bounded; all reset with the session object.
        self.agent_turns: list[str] = []
        self.said_praise: list[str] = []
        self.said_openers: list[str] = []
        self.said_topics: list[str] = []

        # Honest-failure status notes (spec §11.2) — reasoning-node failures
        # land here and surface in the state block.
        self.status_notes: list[str] = []

        # Mode changes recorded for the session report (B3 game_stats).
        self.mode_changes: list[dict] = []

        # Score ledger (WS-7 score integrity): every scoring mutation —
        # adjudicated answers, bonuses, make-goods, rehydration seeds —
        # lands one entry here via apply_score_event, the SOLE score
        # writer. Standings derive from ledger sums (ledger_scores);
        # wrap-up reconciliation (reconcile_scores) compares the parallel
        # per-player counters against them and hard-logs any drift.
        self.score_ledger: list[dict] = []

        # Per-speaker recent finals for fragment-joined command detection
        self._recent_fragments: dict[str, list[tuple[float, str]]] = {}

        # Log-only unrostered speaker sightings
        self.unrostered_labels: dict[str, int] = {}

        # WS-8 ghost-label posture: rolling (t, normalized_text, player) of
        # bound-player finals, pruned to the ghost-fold window. An unbound
        # single-utterance label duplicating one of these is a diarizer echo
        # phantom (max_speakers ceiling spawning S5/S6/S7 to absorb a
        # reverberant copy) and folds instead of scoring.
        self._recent_bound_answers: list[tuple[float, str, str]] = []

        # Segment sanity quarantine (WO-LILY-OMNIBUS-003 WS-10): insane
        # finals land here in full — logged, never discarded, game-inert.
        self.quarantined_segments: list[dict] = []

    # -- roster ------------------------------------------------------------

    def bind_speaker(
        self,
        speaker_label: str,
        player_name: str,
        speaker_id: Optional[str] = None,
        lobby_fact: Optional[str] = None,
        rename: bool = False,
    ) -> dict:
        """
        Bind a diarization label to a player name (lily_bind_speaker tool).
        Re-binding an existing player updates their label (late-session
        label drift resolves through the same path).

        rename=True (HOTFIX-009 W7) marks a same-voice self-correction:
        the label's current holder is the SAME person under a wrong
        spelling ("Rummy" -> "Rami"), so the entry MIGRATES — state and
        ledger history travel to the corrected name — instead of forking
        a fresh entry and leaving a misspelled ghost on the glass. Only
        callers whose name provenance is that voice's own words (confirmed
        evidence, the known-name snap of it) may assert it, and the
        migration writer verifies plausibility itself: the names must
        pass lily_names_probably_same, because this program's defect
        history IS diarization mis-capture — a second voice reusing a
        label and self-introducing an unrelated name must never silently
        take the first player's score history. A refused rename falls
        back to release semantics (logged RENAME_REFUSED).
        """
        name = player_name.strip()
        # If this label was bound to someone else, release it there —
        # or migrate the identity when this is a same-voice correction.
        for other_name, state in list(self.players.items()):
            if other_name != name and state.get("speaker_label") == speaker_label:
                if state.get("placeholder"):
                    # HOTFIX-010 V5: this label was hosting an unnamed
                    # present voice under a speaker-label placeholder (play
                    # and scoring proceeded without a name). A real name for
                    # the SAME label is that voice naming itself, so the
                    # placeholder's score history migrates to the name
                    # unconditionally — no similarity gate, because a
                    # placeholder is by construction the very voice now
                    # speaking its name. The placeholder key is retired.
                    migrated = self.players.pop(other_name)
                    for entry in self.score_ledger:
                        if entry.get("player") == other_name:
                            entry["player"] = name
                    if name not in self.players:
                        migrated.pop("placeholder", None)
                        migrated["speaker_label"] = None
                        self.players[name] = migrated
                    logger.info(
                        "LILY_STATE | PLACEHOLDER_NAMED | session=%s label=%s "
                        "placeholder=%s name=%s",
                        self.session_id, speaker_label, other_name, name,
                    )
                    continue
                if rename and name not in self.players and not (
                    lily_binding.lily_names_probably_same(other_name, name)
                ):
                    logger.info(
                        "LILY_STATE | RENAME_REFUSED | session=%s label=%s "
                        "from=%s to=%s (dissimilar — new binding, not a "
                        "correction)",
                        self.session_id, speaker_label, other_name, name,
                    )
                    rename = False
                if rename and name not in self.players:
                    migrated = self.players.pop(other_name)
                    for entry in self.score_ledger:
                        if entry.get("player") == other_name:
                            entry["player"] = name
                    self.players[name] = migrated
                    logger.info(
                        "LILY_STATE | PLAYER_RENAMED | session=%s label=%s "
                        "from=%s to=%s",
                        self.session_id, speaker_label, other_name, name,
                    )
                    continue
                state["speaker_label"] = None
                logger.info(
                    "LILY_STATE | LABEL_REBOUND | session=%s label=%s from=%s to=%s",
                    self.session_id, speaker_label, other_name, name,
                )
        player = self.players.setdefault(name, {
            "speaker_label": None,
            "speaker_id": None,
            "score": 0,
            "streak": 0,
            "talk_time_s": 0.0,
            "answers_attempted": 0,
            "answers_correct": 0,
            "last_correct_category": None,
            "questions_since_spoke": 0,
            "lobby_fact": None,
            "lifeline_available": True,
        })
        player["speaker_label"] = speaker_label
        if speaker_id:
            player["speaker_id"] = speaker_id
        if lobby_fact:
            player["lobby_fact"] = lobby_fact
        self.unrostered_labels.pop(speaker_label, None)
        logger.info(
            "LILY_STATE | SPEAKER_BOUND | session=%s label=%s name=%s",
            self.session_id, speaker_label, name,
        )
        return player

    def set_lobby_fact(self, player_name: str, fact: str) -> None:
        if player_name in self.players:
            self.players[player_name]["lobby_fact"] = fact

    def roster_size(self, include_placeholder: bool = True) -> int:
        if include_placeholder:
            return len(self.players)
        return sum(
            1 for s in self.players.values() if not s.get("placeholder")
        )

    def ensure_present_placeholder(self, speaker_label: str) -> Optional[str]:
        """HOTFIX-010 V5: stand up a placeholder identity for a present but
        unnamed voice so hosting proceeds — and SCORES — without a name
        first. The placeholder is keyed by the speaker label (its identity
        anchor); a real name for that label later migrates the accumulated
        history through bind_speaker. Idempotent: a real name already on the
        table, or this label already bound (named or placeholder), is a
        no-op. Returns the placeholder key, or None when nothing was
        created."""
        label = (speaker_label or "").strip() or "UU"
        for state in self.players.values():
            if state.get("speaker_label") == label:
                return None
        if self.roster_size(include_placeholder=False) >= 1:
            # A named voice is already hosting; the anonymous voice rides
            # its own attribution and names itself opportunistically.
            return None
        if label in self.players:
            return None
        self.players[label] = {
            "speaker_label": label,
            "speaker_id": None,
            "score": 0,
            "streak": 0,
            "talk_time_s": 0.0,
            "answers_attempted": 0,
            "answers_correct": 0,
            "last_correct_category": None,
            "questions_since_spoke": 0,
            "lobby_fact": None,
            "lifeline_available": True,
            "placeholder": True,
        }
        logger.info(
            "LILY_STATE | PLACEHOLDER_STOOD_UP | session=%s label=%s",
            self.session_id, label,
        )
        return label

    def has_active_placeholder(self) -> bool:
        return any(s.get("placeholder") for s in self.players.values())

    def real_player_names(self) -> list[str]:
        """Rostered NAMES only — placeholder anchors excluded. The spoken
        roster/score surfaces recite from this so a raw speaker label never
        reaches the air; the head count still includes placeholders."""
        return [
            n for n, s in self.players.items() if not s.get("placeholder")
        ]

    def present_placeholder_label(self) -> str:
        """The present unnamed voice's label — the most-observed unrostered
        label, else a stable anonymous anchor."""
        if self.unrostered_labels:
            return max(self.unrostered_labels.items(), key=lambda kv: kv[1])[0]
        return "UU"

    # -- attribution -------------------------------------------------------

    def resolve_speaker(
        self,
        speaker_id: Optional[str],
        speaker_label: Optional[str],
        speaker_name: Optional[str],
        text: str,
    ) -> tuple[Optional[str], Optional[str]]:
        """
        Resolve an utterance to a rostered player.

        Generalized from lbs_attribute_partner_b — same priority order,
        keyed on the roster instead of hardcoded partner B:
          1. speaker_id match against a bound speaker_id
          2. diarization label match against a bound speaker_label
          3. exact (case-insensitive) name match on speaker_name
          4. self-introduction word-boundary match inside the utterance
             ("my name is Jack" / "I'm Jack" / "this is Jack") — cue-gated
             so a trivia ANSWER containing a rostered name never
             misattributes.

        Returns (player_name, attribution_method) or (None, None).
        """
        # Path 1: bound speaker_id
        if speaker_id:
            for name, state in self.players.items():
                if state.get("speaker_id") and state["speaker_id"] == speaker_id:
                    return name, "speaker_id"

        # Path 2: diarization label — primary, or a merge alias (WS-8: a
        # person the diarizer split across two labels resolves on either
        # after an operator merge).
        if speaker_label:
            for name, state in self.players.items():
                if state.get("speaker_label") == speaker_label:
                    return name, "label_match"
            for name, state in self.players.items():
                if speaker_label in (state.get("alias_labels") or ()):
                    return name, "merge_alias_match"

        # Path 2b: the diarization label IS a rostered player's name —
        # voiceprint identification labels a recognized stream by the
        # PLAYER NAME it was enrolled under (known_speakers injection).
        # Live 2026-07-15 18:17 deafness: Speechmatics opened with a
        # transient "S0", Lily bound S0->Rami, then identification
        # converged and relabeled the stream "Rami" — which matched no
        # stored label, so every utterance for the rest of the session
        # went unrostered and nothing could score. When the label equals
        # a player name (case-insensitive), MIGRATE the stored label to
        # the incoming one — identification labels are stickier than the
        # transient S-numbers they replace.
        if speaker_label:
            wanted = str(speaker_label).strip().lower()
            for name, state in self.players.items():
                if name.strip().lower() == wanted:
                    old = state.get("speaker_label")
                    if old != speaker_label:
                        state["speaker_label"] = speaker_label
                        logger.info(
                            "LILY_STATE | LABEL_MIGRATED | session=%s "
                            "player=%s old_label=%s new_label=%s "
                            "(voiceprint identification converged)",
                            self.session_id, name, old, speaker_label,
                        )
                    return name, "voiceprint_label"

        # Path 3: exact name match
        if speaker_name:
            wanted = str(speaker_name).strip().lower()
            for name in self.players:
                if name.strip().lower() == wanted:
                    return name, "name_match"

        # Path 4: self-introduction inside utterance text
        if text:
            lowered = text.lower()
            for name in self.players:
                lname = name.strip().lower()
                if not lname or len(lname) < 2:
                    continue
                pattern = (
                    r"\b(?:my name is|i am|i'm|im|this is|call me|it's)\s+"
                    + _re.escape(lname) + r"\b"
                )
                if _re.search(pattern, lowered):
                    return name, "self_introduction"

        return None, None

    # -- answer window -----------------------------------------------------

    def open_answer_window(
        self,
        duration: Optional[float] = None,
        now: Optional[float] = None,
        reset_candidates: bool = True,
        question_id: Optional[str] = None,
        question_index: Optional[int] = None,
        registered: Optional[bool] = None,
        untimed: bool = False,
    ) -> None:
        """Open the answer window (called on TTS playback-completion).
        reset_candidates=False reopens for a steal window: prior candidates
        are kept so one-candidate-per-player still holds and only new
        players can commit.

        HOTFIX-009 W4: untimed=True opens the window with NO deadline —
        relaxed pacing runs no clock ("No timers. Relaxed means relaxed."),
        so is_window_open/window_contains never expire it and no _expire
        task is armed against it; the beat closes on the roster instead. An
        explicit numeric duration still sets a deadline even in relaxed mode
        (the late-answer path measures lateness against remembered
        deadlines) — untimed is the flag, not the pacing.

        WINDOW BINDING (WO-LILY-HOTFIX-006 N3, invariant 2): the question
        this window belongs to is captured HERE, at open, and carried on
        every candidate recorded inside it through to the ledger write —
        never inferred at adjudication time from whatever happens to be
        armed. In lily-4FB3B2 two questions were asked and BOTH answer rows
        were filed against q_4821; Rhonda's "We don't know." was spoken to
        the Frankenstein question and adjudicated against question one.
        Explicit ids override the live state only for the reconnect/replay
        callers that already know which question they are reopening."""
        t = now if now is not None else time.time()
        self.answer_window_open = True
        self.answer_window_opened_at = t
        self.answer_window_deadline = None if untimed else t + (
            duration if duration is not None else self.answer_window_seconds
        )
        # Captured identity. The id is the question's own id when it has
        # one; the index alone still separates two consecutive questions in
        # an id-less deck, so binding holds either way. Both persist past
        # close_answer_window — adjudication reads them AFTER the window is
        # gone, and the late-answer path reads them later still.
        self.answer_window_question_id = (
            question_id
            if question_id is not None
            else (self.current_question or {}).get("id")
        )
        self.answer_window_question_index = (
            question_index if question_index is not None else self.question_number
        )
        # A window is REGISTERED when it was opened over a question the
        # scorekeeper actually holds (N3 invariant 1). Speech arriving with
        # no open REGISTERED window is logged, never scored — in
        # lily-16A9AE, Chris's answer to an improvised, unregistered
        # question was adjudicated against kb_180 and marked incorrect
        # while Lily told him he had got it. The agent layer passes this
        # explicitly (it holds the armed question and checks it before
        # calling); the default reads the scorekeeper's own state.
        self.answer_window_registered = (
            bool(registered)
            if registered is not None
            else self.current_question is not None
        )
        if reset_candidates:
            self.answer_candidates = {}
        # Fresh window, fresh crosstalk assessment (steal windows included:
        # the deliberation prior is about what happens INSIDE this window).
        self.overlap_flag = False
        self._window_speaker_spans = {}
        self._window_addressee_confidences = []
        self._last_addressee_confidence = None
        logger.info(
            "LILY_STATE | ANSWER_WINDOW_OPEN | session=%s q=%d deadline_in=%s",
            self.session_id, self.question_number,
            (
                "untimed"
                if self.answer_window_deadline is None
                else f"{self.answer_window_deadline - t:.1f}s"
            ),
        )

    def close_answer_window(self) -> None:
        if self.answer_window_open:
            logger.info(
                "LILY_STATE | ANSWER_WINDOW_CLOSED | session=%s q=%d candidates=%d",
                self.session_id, self.question_number, len(self.answer_candidates),
            )
        # N9: the deadline is REMEMBERED across the close. The late-answer
        # path has to be able to say HOW late an answer was — "just past
        # the buzzer" is a reason it must be able to state, and a cleared
        # deadline made lateness unmeasurable the moment it mattered.
        if self.answer_window_deadline is not None:
            self._last_window_deadline = self.answer_window_deadline
        self.answer_window_open = False
        self.answer_window_opened_at = None
        self.answer_window_deadline = None

    def is_window_open(self, now: Optional[float] = None) -> bool:
        if not self.answer_window_open:
            return False
        t = now if now is not None else time.time()
        if self.answer_window_deadline is not None and t > self.answer_window_deadline:
            return False
        return True

    def window_contains(
        self, spoken_ts: Optional[float], now: Optional[float] = None
    ) -> bool:
        """Window membership by SPOKEN time (WS-10): a final belongs to
        the window its speech occurred in, never the window it FINALIZED
        in — the stale-utterance bug scored speech into a window opened
        minutes after it was spoken. Falls back to arrival-time membership
        when no spoken timestamp exists (arrival_time-source finals).

        N9 grace margin: speech spoken up to
        lily_config.late_answer_grace_seconds() past the deadline — while
        the window is still open, i.e. before the expiry task has run —
        still belongs to it. "Just a split second late" is a STATED,
        configurable outcome, not a coin flip against the expiry task's
        scheduling (Rami's "Okay. It's Jupiter." at 21:10:13)."""
        if spoken_ts is None:
            return self.is_window_open(now=now)
        if not self.answer_window_open:
            return False
        if (
            self.answer_window_opened_at is not None
            and spoken_ts < self.answer_window_opened_at
        ):
            return False
        if (
            self.answer_window_deadline is not None
            and spoken_ts > (
                self.answer_window_deadline
                + max(0.0, lily_config.late_answer_grace_seconds())
            )
        ):
            return False
        return True

    def seconds_past_deadline(self, spoken_ts: Optional[float]) -> Optional[float]:
        """How far past the CAPTURED window deadline this speech was
        spoken, or None when there is no deadline to measure against.
        Negative inside the window. Reads the last captured deadline so it
        still answers after close_answer_window (N9 late-answer path)."""
        deadline = self.answer_window_deadline
        if deadline is None:
            deadline = self._last_window_deadline
        if deadline is None or spoken_ts is None:
            return None
        return round(spoken_ts - deadline, 3)

    def _mint_utterance_id(
        self, speaker_label: Optional[str], seg_start: float
    ) -> str:
        """A stable identity for a final the STT event gave no id for
        (N9). Monotonic per session and carries the speaker + spoken time,
        so two finals can never collide into one slot and a log line names
        the exact utterance a ledger row bound to."""
        self._utterance_seq += 1
        return (
            f"u{self._utterance_seq}:{speaker_label or 'UU'}"
            f"@{seg_start:.3f}"
        )

    def window_binding(self) -> dict:
        """The identity captured at the last window-open (N3 invariant 2).
        Valid after the window closes — adjudication and the late-answer
        path both run there."""
        return {
            "question_id": self.answer_window_question_id,
            "question_index": self.answer_window_question_index,
            "registered": self.answer_window_registered,
            "deadline": (
                self.answer_window_deadline
                if self.answer_window_deadline is not None
                else self._last_window_deadline
            ),
        }

    def candidate_bound_to(
        self, candidate: dict, question_id: Optional[str],
        question_index: Optional[int],
    ) -> bool:
        """Whether this candidate arrived in THAT question's window (N3
        invariant 2). Candidates recorded before window binding existed
        (a checkpoint rehydrate, a hand-built harness) carry no stamp and
        are treated as bound — the invariant tightens what it can prove,
        it never invents a mismatch out of missing data."""
        stamped_id = candidate.get("window_question_id")
        stamped_index = candidate.get("window_question_index")
        if stamped_id is None and stamped_index is None:
            return True
        if question_id is not None and stamped_id is not None:
            return stamped_id == question_id
        if question_index is not None and stamped_index is not None:
            return stamped_index == question_index
        return True

    def ordered_candidates(self) -> list[dict]:
        """Candidates ordered by STT segment start time — first answer wins.
        Timestamp comparison, never an LLM judgment."""
        return sorted(
            self.answer_candidates.values(),
            key=lambda c: c["segment_start_time"],
        )

    # -- prior states (WO-ADDRESSEE-H1 Task 2) --------------------------------

    def prior_state(self, now: Optional[float] = None) -> str:
        """The active prior state, most specific first: SCORING
        (adjudication in flight) > HOST_SPEAKING (Lily on air) > OVERLAP
        (crosstalk detected inside the open window) > OPEN_WINDOW (window
        open, floor clean) > IDLE (window closed)."""
        if self.adjudicating:
            return PRIOR_SCORING
        if self.host_speaking:
            return PRIOR_HOST_SPEAKING
        if self.is_window_open(now=now):
            return PRIOR_OVERLAP if self.overlap_flag else PRIOR_OPEN_WINDOW
        return PRIOR_IDLE

    def window_prior_state(self) -> str:
        """The prior of the most recent answer window — OVERLAP if
        crosstalk flipped inside it, else OPEN_WINDOW. Valid after the
        window closes (overlap_flag persists until the next open), so
        adjudication evaluates candidates under the state they were
        CAPTURED in, not the SCORING state the evaluation runs in."""
        return PRIOR_OVERLAP if self.overlap_flag else PRIOR_OPEN_WINDOW

    def window_addressee_confidence(self) -> float | None:
        """Mean fused addressee-confidence seen in the active window."""
        if self._window_addressee_confidences:
            values = self._window_addressee_confidences
            return round(sum(values) / len(values), 3)
        return self._last_addressee_confidence

    def tier1_threshold_for_state(
        self,
        prior_state: str,
        *,
        addressee_confidence: Optional[float] = None,
    ) -> float:
        """Tier-1 threshold for a specific PRIOR_* state, plus the additive
        addressee-confidence penalty."""
        base = lily_config.tier1_threshold_for_prior(prior_state)
        conf = (
            addressee_confidence
            if addressee_confidence is not None
            else self.window_addressee_confidence()
        )
        return base + lily_config.tier1_addressee_penalty(conf)

    def tier1_threshold(
        self,
        now: Optional[float] = None,
        *,
        addressee_confidence: Optional[float] = None,
    ) -> float:
        """The active Tier-1 acceptance threshold.

        PRIOR_* drives the base threshold; fused addressee-confidence adds a
        small penalty when confidence is low.
        """
        return self.tier1_threshold_for_state(
            self.prior_state(now=now),
            addressee_confidence=addressee_confidence,
        )

    def _note_speaker_span(
        self, key: str, start: float, end: float
    ) -> None:
        """Overlap detection — pure timestamp arithmetic, zero models.
        Record one speaker's segment span [start, end] inside the open
        window and flip overlap_flag when it overlaps a DIFFERENT
        speaker's recorded span by more than the epsilon
        (LILY_OVERLAP_EPSILON_SECONDS — strict inequality, so degenerate
        zero-length spans never flip it)."""
        if end < start:
            start, end = end, start
        epsilon = lily_config.overlap_epsilon_seconds()
        for other_key, spans in self._window_speaker_spans.items():
            if other_key == key:
                continue
            for other_start, other_end in spans:
                overlap_s = min(end, other_end) - max(start, other_start)
                if overlap_s > epsilon:
                    if not self.overlap_flag:
                        logger.info(
                            "LILY_PRIOR | OVERLAP_DETECTED | session=%s q=%d "
                            "speakers=%s,%s overlap_s=%.3f epsilon=%.3f",
                            self.session_id, self.question_number,
                            other_key, key, overlap_s, epsilon,
                        )
                    self.overlap_flag = True
        spans = self._window_speaker_spans.setdefault(key, [])
        spans.append((start, end))
        if len(spans) > _MAX_SPANS_PER_SPEAKER:
            del spans[:-_MAX_SPANS_PER_SPEAKER]

    def _note_addressee_confidence(self, confidence: Optional[float]) -> None:
        conf = _clamp_confidence(confidence)
        if conf is None:
            return
        self._last_addressee_confidence = conf
        self._window_addressee_confidences.append(conf)
        if len(self._window_addressee_confidences) > 120:
            del self._window_addressee_confidences[:-120]

    # -- transcript ingestion ----------------------------------------------

    def _record_bound_answer(self, text: str, player: str, now: float) -> None:
        """Append a bound player's final to the ghost-fold reference log,
        pruned to the fold window. Trivial (empty-after-normalize) finals
        are ignored so filler never masquerades as a foldable answer."""
        norm = _normalize_answer_text(text)
        if not norm:
            return
        window = lily_config.ghost_fold_window_seconds()
        self._recent_bound_answers.append((now, norm, player))
        self._recent_bound_answers = [
            (t, n, p)
            for (t, n, p) in self._recent_bound_answers
            if now - t <= window
        ]

    def _ghost_fold_echo(
        self,
        speaker_label: Optional[str],
        text: str,
        now: float,
        echo_copy_signal: Optional[bool] = None,
    ) -> bool:
        """WS-8: decide whether an unbound label's final is a diarizer echo
        phantom of a bound player's just-recorded answer.

        Heuristic (text + timing): the label has been seen at most once
        (a single-utterance phantom, not a real recurring voice) AND its
        normalized text equals a bound player's answer recorded within the
        ghost-fold window.

        `echo_copy_signal` is WS-13's per-word volume determination
        (True = measurably quieter copy → corroborates the fold;
        False = as loud as the original → a real second speaker, veto the
        fold; None = signal absent, decide on text+timing alone). It never
        forces a fold on its own — a match is still required."""
        window = lily_config.ghost_fold_window_seconds()
        if window <= 0:
            return False
        norm = _normalize_answer_text(text)
        if not norm:
            return False
        # A label already seen more than once is a recurring voice, not a
        # one-shot phantom — never fold it (it may be a genuine unrostered
        # player). unrostered_labels was incremented for this segment just
        # above, so its first sighting reads as count 1.
        seen = self.unrostered_labels.get(speaker_label, 0) if speaker_label else 0
        if seen > 1:
            return False
        if echo_copy_signal is False:
            return False
        for (t, bound_norm, player) in self._recent_bound_answers:
            if now - t > window:
                continue
            if bound_norm == norm:
                logger.info(
                    "LILY_STATE | GHOST_FOLD | session=%s label=%s text=%r "
                    "echo_of=%s dt=%.2fs volume_signal=%s",
                    self.session_id, speaker_label, norm[:60], player,
                    now - t, echo_copy_signal,
                )
                return True
        return False

    def merge_speakers(
        self,
        from_label: str,
        into_player: str,
        now: Optional[float] = None,
    ) -> dict:
        """WS-8 operator merge — roster side of one reconciliation
        transaction (the persistence side is
        lily_persistence.lily_merge_speaker). Fold every utterance the
        diarizer split onto `from_label` back onto the player it really
        belongs to.

        - Binds `into_player` (creating the roster row if the merge names a
          voice that was never bound — the S1-holding-Chris's-intro case).
        - Releases `from_label` from any OTHER player it was bound to.
        - Retro-attributes in-memory state: open answer candidates keyed to
          the merged label and the rolling transcript buffer move to
          `into_player`.

        Returns a summary dict for the caller's spoken confirmation. Score
        is NOT recomputed here — a held open-floor award is committed
        through the normal on_speaker_bound path; this only makes the
        identity coherent."""
        t = now if now is not None else time.time()
        into = (into_player or "").strip()
        label = (from_label or "").strip()
        result = {
            "into_player": into,
            "from_label": label,
            "candidates_moved": 0,
            "buffer_lines_moved": 0,
            "created_player": False,
        }
        if not into or not label:
            return result
        if into not in self.players:
            result["created_player"] = True
        # Release the merged label from any OTHER player it was bound to
        # (primary or alias) — one voice, one owner.
        for other, state in self.players.items():
            if other == into:
                continue
            if state.get("speaker_label") == label:
                state["speaker_label"] = None
            aliases = state.get("alias_labels")
            if aliases and label in aliases:
                state["alias_labels"] = [a for a in aliases if a != label]
        target = self.players.get(into)
        if target is None:
            # Create the roster row (the S1-holding-Chris's-intro case) —
            # bind_speaker sets its primary label and every default field.
            self.bind_speaker(label, into)
        else:
            primary = target.get("speaker_label")
            if not primary:
                target["speaker_label"] = label
            elif primary != label:
                # The player already has a primary label (e.g. Chris on
                # S4); the diarizer's second split-label (S1) becomes an
                # alias so BOTH keep resolving to the player going forward,
                # not just historically.
                aliases = set(target.get("alias_labels") or ())
                aliases.add(label)
                target["alias_labels"] = sorted(aliases)
        # Retro-attribute any open answer candidate the diarizer parked
        # under the merged label (both the unrostered key form and a bare
        # label key) onto the player.
        moved_keys = [
            key for key in list(self.answer_candidates.keys())
            if key in (f"unrostered:{label}", label)
        ]
        for key in moved_keys:
            cand = self.answer_candidates.pop(key)
            cand["player"] = into
            cand["unrostered"] = False
            existing = self.answer_candidates.get(into)
            if existing is None:
                self.answer_candidates[into] = cand
            else:
                # Fold the merged attempts into the player's existing slot,
                # preserving their earlier order position.
                existing.setdefault("attempts", []).extend(
                    cand.get("attempts", [])
                )
            result["candidates_moved"] += 1
        # Retro-attribute the rolling transcript buffer.
        for entry in self.transcript_buffer:
            if entry.get("speaker_label") == label or entry.get("speaker") == label:
                entry["speaker"] = into
                result["buffer_lines_moved"] += 1
        self.unrostered_labels.pop(label, None)
        logger.info(
            "LILY_STATE | SPEAKER_MERGE | session=%s from_label=%s into=%s "
            "candidates=%d buffer=%d created=%s",
            self.session_id, label, into, result["candidates_moved"],
            result["buffer_lines_moved"], result["created_player"],
        )
        return result

    def on_transcript_segment(
        self,
        text: str,
        speaker_label: Optional[str] = None,
        speaker_id: Optional[str] = None,
        speaker_name: Optional[str] = None,
        is_final: bool = True,
        segment_start_time: Optional[float] = None,
        segment_end_time: Optional[float] = None,
        diarization_confidence: Optional[float] = None,
        acoustic_confidence: Optional[float] = None,
        timestamp_source: Optional[str] = None,
        timing_drift_seconds: Optional[float] = None,
        now: Optional[float] = None,
        timestamp: Optional[str] = None,
        addressee_confidence: Optional[float] = None,
        echo_copy_signal: Optional[bool] = None,
        assume_in_window: bool = False,
        utterance_id: Optional[str] = None,
    ) -> dict:
        """
        Process one transcript segment. Called on every STT event.
        Partials display, finals score — never the reverse.

        Returns an event dict for the agent layer:
          {
            "player": resolved name or None,
            "attribution": method or None,
            "system_directed": bool,
            "control_command": "skip" | "back_to_normal" | None,
            "candidate_recorded": bool,
            "unrostered": bool,
            "addressee_confidence": float | None,
            "prior_state": PRIOR_* string (WO-ADDRESSEE-H1 Task 2),
            "overlap_flag": bool,
            "addressee_fused_confidence": float in [0,1],
            "attribution_demoted": bool,
          }
        """
        t = now if now is not None else time.time()
        ts = timestamp or _now_iso()
        segment_conf = _clamp_confidence(addressee_confidence)
        result = {
            "player": None,
            "attribution": None,
            "system_directed": False,
            "control_command": None,
            "media_choice": None,
            "candidate_recorded": False,
            "unrostered": False,
            "addressee_confidence": segment_conf,
            # Prior state (WO-ADDRESSEE-H1 Task 2) — additive keys, refined
            # below for finals after overlap detection runs.
            "prior_state": self.prior_state(now=t),
            "overlap_flag": self.overlap_flag,
            "addressee_fused_confidence": None,
            "attribution_demoted": False,
            "ghost_folded": False,
            "quarantined": False,
            "quarantine_reason": None,
            # N9: this final's own transcript id (see below — minted when
            # the STT event supplies none). Every consumer that records an
            # utterance reads it from here.
            "utterance_id": utterance_id,
        }

        if not text or not text.strip():
            return result

        if not is_final:
            # Partials never score and never mutate state.
            return result

        clean = text.strip()

        # Segment sanity gate (WO-LILY-OMNIBUS-003 WS-10): corrupted STT
        # finals — spans of minutes, or finals landing minutes after the
        # speech ended — poisoned windows and talk-time (104s/206s spans,
        # a stale utterance scoring 3.5 minutes late). Quarantine them:
        # logged in full, excluded from windows and talk-time, game-inert.
        # Thresholds are config; WS-13's segmentation audit binds tuned
        # values via LILY_SEGMENT_MAX_SPAN_SECONDS /
        # LILY_SEGMENT_MAX_FINALIZATION_LAG_SECONDS.
        # assume_in_window marks the early-buzz REPLAY path: the segment
        # already passed this gate at original ingestion, and the buffered
        # wait for window open inflates its apparent finalization lag —
        # re-running the gate would false-positive legit early answers.
        span_s = None
        if segment_start_time is not None and segment_end_time is not None:
            span_s = abs(segment_end_time - segment_start_time)
        lag_s = None
        if segment_end_time is not None:
            lag_s = t - segment_end_time
        reasons = []
        if not assume_in_window:
            if (
                span_s is not None
                and span_s > lily_config.segment_max_span_seconds()
            ):
                reasons.append("span")
            if (
                lag_s is not None
                and lag_s > lily_config.segment_max_finalization_lag_seconds()
            ):
                reasons.append("lag")
        if reasons:
            reason = "+".join(reasons)
            result["quarantined"] = True
            result["quarantine_reason"] = reason
            self.quarantined_segments.append({
                "text": clean,
                "speaker_label": speaker_label,
                "segment_start_time": segment_start_time,
                "segment_end_time": segment_end_time,
                "span_seconds": round(span_s, 3) if span_s is not None else None,
                "finalization_lag_seconds": (
                    round(lag_s, 3) if lag_s is not None else None
                ),
                "reason": reason,
                "timestamp": ts,
                "timestamp_source": timestamp_source,
                "timing_drift_seconds": timing_drift_seconds,
            })
            if len(self.quarantined_segments) > QUARANTINE_LOG_SIZE:
                del self.quarantined_segments[:-QUARANTINE_LOG_SIZE]
            logger.warning(
                "LILY_SEGMENT | QUARANTINED | session=%s q=%d speaker=%s "
                "reason=%s span_s=%s lag_s=%s text=%r",
                self.session_id, self.question_number, speaker_label,
                reason, span_s, lag_s, clean[:80],
            )
            return result

        player, method = self.resolve_speaker(
            speaker_id, speaker_label, speaker_name, clean
        )
        result["player"] = player
        result["attribution"] = method

        # Overlap detection (WO-ADDRESSEE-H1 Task 2): inside the open
        # window, record this final's [segment_start, end≈now] span per
        # speaker and flip OVERLAP on cross-speaker timestamp overlap.
        # Runs BEFORE the prior is stamped so this very segment is
        # classified under the state it just created. Membership keys on
        # SPOKEN time (WS-10), so a late final lands in the window its
        # speech belongs to.
        if assume_in_window or self.window_contains(segment_start_time, now=t):
            span_key = player or speaker_label
            if span_key:
                seg_start = segment_start_time if segment_start_time is not None else t
                seg_end = (
                    segment_end_time
                    if segment_end_time is not None
                    else seg_start
                )
                self._note_speaker_span(
                    str(span_key),
                    seg_start,
                    seg_end,
                )
        prior = self.prior_state(now=t)
        result["prior_state"] = prior
        result["overlap_flag"] = self.overlap_flag
        # One canonical fused confidence feeds both threshold weighting and
        # conservative overlap attribution. The agent may provide an
        # alignment-checked fused value; otherwise derive it locally from
        # the separate telemetry signals.
        has_separate_signal = (
            _coerce_confidence(diarization_confidence) is not None
            or _coerce_confidence(acoustic_confidence) is not None
        )
        fused_conf = _clamp_confidence(segment_conf)
        if fused_conf is None:
            fused_conf = lily_overlap_fused_confidence(
                diarization_confidence, acoustic_confidence
            )
            segment_conf = fused_conf if has_separate_signal else None
        else:
            segment_conf = fused_conf
        result["addressee_confidence"] = segment_conf
        result["addressee_fused_confidence"] = fused_conf
        # Every classification decision logs its active prior state.
        logger.info(
            "LILY_PRIOR | state=%s threshold=%.3f overlap=%s | session=%s "
            "q=%d speaker=%s",
            prior, self.tier1_threshold(
                now=t, addressee_confidence=segment_conf
            ),
            self.overlap_flag, self.session_id, self.question_number,
            player or speaker_label or "?",
        )

        # Crosstalk fusion (WO-LILY-CROSSTALK-FUSION): when PRIOR_OVERLAP is
        # active, fuse diarization confidence with room-level acoustic
        # confidence and conservatively demote low-confidence roster
        # attributions to open-floor utterances.
        if (
            prior == PRIOR_OVERLAP
            and player
            and method in ("label_match", "name_match", "self_introduction")
            and fused_conf < lily_config.overlap_fusion_min_confidence()
        ):
            demoted_player = player
            player, method = None, "overlap_low_confidence_fusion"
            result["player"] = None
            result["attribution"] = method
            result["attribution_demoted"] = True
            logger.info(
                "LILY_PRIOR | ADDRESSEE_DEMOTED | session=%s q=%d "
                "speaker=%s confidence=%.3f threshold=%.3f",
                self.session_id,
                self.question_number,
                demoted_player,
                fused_conf,
                lily_config.overlap_fusion_min_confidence(),
            )

        # System-directed classification — "Lily, are you there?" must not
        # count as an answer attempt during an open window.
        is_sys, pattern = lily_is_system_directed(clean)
        result["system_directed"] = is_sys
        if is_sys:
            logger.info(
                "LILY_INTENT | SYSTEM_DIRECTED | session=%s speaker=%s pattern=%s",
                self.session_id, player or speaker_label, pattern,
            )

        # Sticky command detection — fragment-joined per speaker
        # (2s accumulation, so "back to" + "normal" across finals fires).
        frag_key = player or speaker_label or "unknown"
        frags = self._recent_fragments.setdefault(frag_key, [])
        frags.append((t, clean))
        self._recent_fragments[frag_key] = [
            (ft, ftext) for ft, ftext in frags
            if t - ft <= FRAGMENT_JOIN_WINDOW_SECONDS
        ]
        joined = " ".join(ftext for _, ftext in self._recent_fragments[frag_key])
        command = lily_detect_control_command(joined)
        if command:
            result["control_command"] = command
            self._recent_fragments[frag_key] = []
            logger.info(
                "LILY_INTENT | CONTROL_COMMAND | session=%s speaker=%s command=%s",
                self.session_id, player or speaker_label, command,
            )
        else:
            # Media-mode spoken choice (sub-agent K) — same fragment-joined
            # detection; the agent layer flips the sticky flag. Setting the
            # SAME mode again is a no-op there, so re-fires are harmless.
            choice = lily_detect_media_choice(joined)
            if choice:
                result["media_choice"] = choice
                self._recent_fragments[frag_key] = []
                logger.info(
                    "LILY_INTENT | MEDIA_CHOICE | session=%s speaker=%s choice=%s",
                    self.session_id, player or speaker_label, choice,
                )

        # Per-player bookkeeping
        duration = 0.0
        if segment_start_time is not None and segment_end_time is not None:
            duration = max(0.0, segment_end_time - segment_start_time)
        if duration <= 0:
            duration = len(clean.split()) * 0.4

        if player:
            state = self.players[player]
            state["talk_time_s"] += duration
            state["questions_since_spoke"] = 0
            # Ghost-fold reference: record this bound final so a later echo
            # copy on a phantom label can be recognized. Pruned to the fold
            # window; survives question transitions (the echo may land in a
            # different window than the original).
            self._record_bound_answer(clean, player, t)
        elif speaker_label:
            self.unrostered_labels[speaker_label] = (
                self.unrostered_labels.get(speaker_label, 0) + 1
            )
            result["unrostered"] = True
            logger.info(
                "LILY_STATE | UNROSTERED_SPEAKER | session=%s label=%s text=%r",
                self.session_id, speaker_label, clean[:80],
            )

        # Transcript buffer (rolling)
        self.transcript_buffer.append({
            "speaker": player or speaker_label or "?",
            "speaker_label": speaker_label,
            "text": clean,
            "timestamp": ts,
            "duration": duration,
        })
        if len(self.transcript_buffer) > TRANSCRIPT_BUFFER_SIZE:
            self.transcript_buffer = self.transcript_buffer[-TRANSCRIPT_BUFFER_SIZE:]

        # Answer-window candidate recording: finals only, window open,
        # not system-directed, not a control command. Segments outside an
        # open window are game-inert — they reach Lily conversationally but
        # never the scoring path.
        if (
            (
                assume_in_window
                or self.window_contains(segment_start_time, now=t)
            )
            and not is_sys
            and not command
            and not result.get("media_choice")
        ):
            seg_start = (
                segment_start_time if segment_start_time is not None else t
            )
            seg_end = (
                segment_end_time if segment_end_time is not None else seg_start
            )
            # N9: capture binds an UTTERANCE, identified by its own
            # transcript id — never "most recent", never "first-seen
            # fragment for that speaker". The live q_1052 row recorded
            # Rami's earlier "Go." while his actual "Okay. It's Jupiter."
            # never entered the ledger at all: a DIFFERENT utterance sat in
            # his slot. When the STT event carries no id we mint a stable
            # one so the binding can never silently degrade to a slot.
            uid = utterance_id or self._mint_utterance_id(
                speaker_label, seg_start
            )
            result["utterance_id"] = uid
            # N9 grace margin telemetry: how far past the deadline this
            # answer was spoken. Non-positive = inside the window; positive
            # = admitted on the STATED grace margin.
            past_deadline = self.seconds_past_deadline(seg_start)
            if past_deadline is not None and past_deadline > 0:
                result["late_within_grace"] = True
                result["seconds_late"] = past_deadline
                logger.info(
                    "LILY_ANSWER | LATE_WITHIN_GRACE | session=%s q=%d "
                    "label=%s late=%.3fs grace=%.3fs text=%r",
                    self.session_id, self.question_number, speaker_label,
                    past_deadline, lily_config.late_answer_grace_seconds(),
                    clean[:60],
                )
            # PATCH-001 T5(c) evaluator hygiene: a backchannel ("Yeah") or
            # a bare roster-name fragment ("Chris.") in an open window is
            # LOGGED, never adjudicated as an attempt — the live fixtures
            # scored "Yeah" incorrect and wrote a null-player "Chris." row,
            # consuming those players' judgments. Answer-surface matches
            # always pass (a yes/no question keeps "yeah" scoreable).
            # HOTFIX-006 N4 extends the same hook to meta-speech: questions
            # to the host, corrections, complaints, procedural remarks.
            non_answer = lily_evaluation.lily_non_answer_utterance(
                clean, self.current_question, list(self.players)
            )
            if non_answer:
                result["non_answer"] = non_answer
                logger.info(
                    "LILY_ANSWER | NON_ANSWER_LOGGED | session=%s q=%d "
                    "label=%s reason=%s text=%r — not an attempt",
                    self.session_id, self.question_number,
                    speaker_label, non_answer, clean[:60],
                )
                return result
            if player:
                key = player
            else:
                # WS-8 ghost-label posture: an unbound single-utterance
                # label duplicating a bound player's just-recorded answer
                # inside the echo window is a diarizer echo phantom — fold
                # it (no candidate, no score, no "and you are?" prompt)
                # rather than letting a ceiling-spawned S5/S6/S7 absorb a
                # reverberant copy of a real answer.
                if self._ghost_fold_echo(
                    speaker_label, clean, t, echo_copy_signal=echo_copy_signal
                ):
                    result["ghost_folded"] = True
                    return result
                # Open-floor fallback: unrostered answer is never silently
                # attributed — recorded under its label for Lily to resolve
                # in character ("great answer — and you are?").
                key = f"unrostered:{speaker_label or 'UU'}"
            existing = self.answer_candidates.get(key)
            # N3 invariant 2: a candidate left over from an EARLIER
            # question's window is not a slot this window's speech may
            # revise into. In lily-4FB3B2 both answer rows were filed
            # against q_4821 — close_answer_window never cleared candidates,
            # so Rhonda's answer to the Frankenstein question was absorbed
            # as a "revision" of her q_4821 candidate and inherited its
            # question id. A final arriving in a DIFFERENT window starts a
            # fresh candidate bound to the window it actually arrived in.
            if existing is not None and not self.candidate_bound_to(
                existing,
                self.answer_window_question_id,
                self.answer_window_question_index,
            ):
                logger.info(
                    "LILY_ANSWER | STALE_CANDIDATE_REPLACED | session=%s "
                    "q=%d key=%s was=%s/%s now=%s/%s — a new window's "
                    "answer never revises the previous window's candidate",
                    self.session_id, self.question_number, key,
                    existing.get("window_question_id"),
                    existing.get("window_question_index"),
                    self.answer_window_question_id,
                    self.answer_window_question_index,
                )
                del self.answer_candidates[key]
                existing = None
            if existing is None:
                # First final claims the player's slot — its timestamp is
                # their position in the answer order.
                attempt_entry = {
                    "text": clean,
                    "segment_start_time": seg_start,
                    "segment_end_time": seg_end,
                    "timestamp": ts,
                    "timestamp_source": timestamp_source,
                    "timing_drift_seconds": timing_drift_seconds,
                    # N9: the utterance's own identity travels with it.
                    "utterance_id": uid,
                }
                if segment_conf is not None:
                    attempt_entry["addressee_confidence"] = segment_conf
                self.answer_candidates[key] = {
                    "player": player,
                    "speaker_label": speaker_label,
                    "text": clean,
                    "utterance_id": uid,
                    "segment_start_time": seg_start,
                    "segment_end_time": seg_end,
                    "timestamp": ts,
                    "unrostered": player is None,
                    # N3 invariant 2: the question whose window this
                    # candidate arrived in, stamped at RECORD time from the
                    # identity captured at window-open. Adjudication filters
                    # on it and the ledger write carries it — nothing is
                    # inferred from whatever happens to be armed later.
                    "window_question_id": self.answer_window_question_id,
                    "window_question_index": self.answer_window_question_index,
                    "window_registered": self.answer_window_registered,
                    "addressee_fused_confidence": result.get(
                        "addressee_fused_confidence"
                    ),
                    "addressee_confidence": segment_conf,
                    "diarization_confidence": _coerce_confidence(
                        diarization_confidence
                    ),
                    "acoustic_confidence": _coerce_confidence(acoustic_confidence),
                    "timestamp_source": timestamp_source,
                    "timing_drift_seconds": timing_drift_seconds,
                    # Every in-window final from this player, in order —
                    # adjudication judges the whole set (self-correction,
                    # live 2026-07-15: "the spine… no, the femur" must be
                    # able to score on the femur).
                    "attempts": [attempt_entry],
                }
                result["candidate_recorded"] = True
                self._note_addressee_confidence(segment_conf)
                if player:
                    self.players[player]["answers_attempted"] += 1
                logger.info(
                    "LILY_STATE | ANSWER_CANDIDATE | session=%s q=%d key=%s t=%.3f text=%r",
                    self.session_id, self.question_number, key, seg_start, clean[:80],
                )
            else:
                # Self-correction (live 2026-07-15 fix): a later final from
                # the SAME player is a revision, not noise. It joins their
                # attempt set and becomes their current answer; their ORDER
                # position stays the first final (first-final-wins orders
                # players against each other, it never locks a player out
                # of revising). Adjudication scores the earliest CORRECT
                # attempt across the table, so a revision can win only from
                # its own (later) timestamp.
                existing["text"] = clean
                existing["utterance_id"] = uid
                existing["timestamp"] = ts
                existing["segment_end_time"] = seg_end
                existing["addressee_fused_confidence"] = result.get(
                    "addressee_fused_confidence"
                )
                if segment_conf is not None:
                    existing["addressee_confidence"] = segment_conf
                existing["diarization_confidence"] = _coerce_confidence(
                    diarization_confidence
                )
                existing["acoustic_confidence"] = _coerce_confidence(
                    acoustic_confidence
                )
                existing["timestamp_source"] = timestamp_source
                existing["timing_drift_seconds"] = timing_drift_seconds
                existing.setdefault("attempts", []).append(
                    {
                        "text": clean,
                        "segment_start_time": seg_start,
                        "segment_end_time": seg_end,
                        "timestamp": ts,
                        "timestamp_source": timestamp_source,
                        "timing_drift_seconds": timing_drift_seconds,
                        "utterance_id": uid,
                        **(
                            {"addressee_confidence": segment_conf}
                            if segment_conf is not None
                            else {}
                        ),
                    }
                )
                result["candidate_recorded"] = True
                self._note_addressee_confidence(segment_conf)
                logger.info(
                    "LILY_STATE | ANSWER_REVISED | session=%s q=%d key=%s t=%.3f text=%r",
                    self.session_id, self.question_number, key, seg_start, clean[:80],
                )

        return result

    # -- game flow ---------------------------------------------------------

    def start_question(self, question: Optional[dict] = None) -> None:
        """Advance to the next question. Resets candidates; window opens
        separately on the TTS playback-completion event."""
        self.question_number += 1
        self.current_question = question
        self.current_answer = (question or {}).get("canonical_answer")
        self.answer_candidates = {}
        self.close_answer_window()
        if question and question.get("category"):
            self.category = question["category"]
        for state in self.players.values():
            state["questions_since_spoke"] += 1

    def apply_score_event(
        self,
        player_name: str,
        *,
        cause: str,
        correct: Optional[bool] = None,
        points: int = 0,
        category: Optional[str] = None,
        question_id: Optional[str] = None,
        question_index: Optional[int] = None,
        transcript: Optional[str] = None,
        eval_tier: Optional[int] = None,
        utterance_id: Optional[str] = None,
        grounds: Optional[str] = None,
        actor: Optional[str] = None,
        corrects: Optional[dict] = None,
    ) -> Optional[dict]:
        """The SOLE score writer (WS-7). Every scoring mutation — an
        adjudicated result, a bonus, a make-good, an operator-directed
        award — goes through here and lands one score_ledger entry with a
        cause code. Nothing else may touch players[name]["score"].
        WS-4's idempotent award keying bolts onto this entry point.

        Cause vocabulary: "answer" (adjudication commit, correct set),
        "bonus", "make_good", "operator_award", "rehydrate" (checkpoint
        seed), "verdict_correction" (W1: an auditable reversal of a prior
        verdict — carries grounds/actor/corrects and is the ONLY cause
        exempt from the WS-4 idempotency belt, because a correction is by
        definition a second, intentional movement on an already-ruled
        question; its ONLY caller is correct_verdict, which will not run
        without a real prior verdict row, so the exemption cannot reopen
        the fabrication hole). correct=True applies answer semantics
        (streak/answers_correct advance); correct=False resets the streak
        and forces points to 0; correct=None moves points without touching
        the streak.

        Returns the ledger entry, or None for an unrostered name (warned,
        no mutation, no audit — a point can never land on a null player).
        """
        state = self.players.get(player_name)
        if state is None:
            logger.warning(
                "LILY_STATE | SCORE_FOR_UNKNOWN_PLAYER | session=%s name=%s cause=%s",
                self.session_id, player_name, cause,
            )
            return None
        # Idempotent awards (WS-4): one point-earning award per
        # (question_id, player) per session, capped at that award's value.
        # Duplicate resolution dispatches — a timed-out question re-armed
        # and the revealed answer echoed, a held award bound twice — cannot
        # double-increment. Keyed on the ledger itself, so the guard
        # travels with the checkpoint for free (rehydrate seeds it). Only
        # positive movements are keyed: an incorrect commit (points=0) still
        # audits and never consumes the key; bonuses carry no question_id
        # and are intentionally exempt.
        if (
            question_id is not None
            and points
            and correct is not False
            and cause != "verdict_correction"
        ):
            prior = self._existing_award(question_id, player_name)
            if prior is not None:
                logger.info(
                    "LILY_STATE | SCORE_DUPLICATE_AWARD | session=%s player=%s "
                    "question_id=%s cause=%s points=%d — already awarded "
                    "%d; refused (no mutation)",
                    self.session_id, player_name, question_id, cause, points,
                    prior.get("points") or 0,
                )
                return prior
        if correct is False:
            points = 0
            state["streak"] = 0
        else:
            state["score"] += points
            if correct is True:
                state["streak"] += 1
                state["answers_correct"] = state.get("answers_correct", 0) + 1
                if category or self.category:
                    state["last_correct_category"] = category or self.category
        entry = {
            "player": player_name,
            "cause": cause,
            "correct": correct,
            "points": points,
            "question_id": question_id,
            "question_index": (
                question_index if question_index is not None
                else self.question_number
            ),
            "transcript": transcript,
            # N9: which UTTERANCE this row is about. The live q_1052 row
            # carried Rami's "Go." with nothing to say which spoken thing
            # it was; the id makes the binding auditable.
            "utterance_id": utterance_id,
            "eval_tier": eval_tier,
            "score_after": state["score"],
            "ts": _now_iso(),
        }
        # W1 correction provenance: which verdict this row amends, on what
        # grounds, and who triggered it. Present only on verdict_correction
        # rows; snapshot copies the whole entry so they ride the checkpoint.
        if grounds is not None:
            entry["grounds"] = grounds
        if actor is not None:
            entry["actor"] = actor
        if corrects is not None:
            entry["corrects"] = corrects
        self.score_ledger.append(entry)
        logger.info(
            "LILY_STATE | SCORE_COMMIT | session=%s player=%s cause=%s correct=%s points=%d score=%d streak=%d",
            self.session_id, player_name, cause, correct, points,
            state["score"], state["streak"],
        )
        return entry

    def _existing_award(
        self, question_id: str, player_name: str
    ) -> Optional[dict]:
        """The prior point-earning ledger entry for this (question_id,
        player), or None. The idempotency key (WS-4): a positive award
        already on the ledger for the same question and player means any
        further positive movement is a duplicate dispatch."""
        for entry in self.score_ledger:
            if (
                entry.get("question_id") == question_id
                and entry.get("player") == player_name
                and entry.get("correct") is not False
                and (entry.get("points") or 0) > 0
            ):
                return entry
        return None

    def record_result(
        self,
        player_name: str,
        correct: bool,
        points: int = 1,
        category: Optional[str] = None,
        question_id: Optional[str] = None,
        transcript: Optional[str] = None,
        eval_tier: Optional[int] = None,
        question_index: Optional[int] = None,
        utterance_id: Optional[str] = None,
    ) -> Optional[dict]:
        """Commit an adjudicated result. Event-bound truth: Lily never
        announces a score change that hasn't landed here first. Delegates
        to apply_score_event (cause="answer") — the single write path.

        question_index/utterance_id are the HOTFIX-006 bindings: the
        question captured at WINDOW-OPEN (N3) and the utterance actually
        adjudicated (N9). Both default to today's behaviour when omitted."""
        entry = self.apply_score_event(
            player_name,
            cause="answer",
            correct=correct,
            points=points,
            category=category,
            question_id=question_id,
            question_index=question_index,
            transcript=transcript,
            eval_tier=eval_tier,
            utterance_id=utterance_id,
        )
        if entry is None:
            return None
        return self.players.get(player_name)

    # W1 (WO-LILY-HOTFIX-009) — the auditable reversal path. The closed set
    # of grounds on which a committed verdict may be amended. An unknown
    # ground is refused (fails closed).
    CORRECTION_GROUNDS = frozenset(
        {"wrong_rule", "answer_denied", "misheard", "out_of_window"}
    )

    def existing_correction(
        self, player_name: str, question_id: Optional[str]
    ) -> Optional[dict]:
        """The prior verdict_correction row for this (player, question), or
        None. A verdict may be corrected once — this is the correction
        analogue of the WS-4 idempotency belt."""
        for entry in self.score_ledger:
            if (
                entry.get("cause") == "verdict_correction"
                and entry.get("player") == player_name
                and entry.get("question_id") == question_id
            ):
                return entry
        return None

    def correct_verdict(
        self,
        player_name: str,
        *,
        grounds: str,
        actor: str = "player_contest",
        question_id: Optional[str] = None,
        delta: int = 1,
        canonical_answer: Optional[str] = None,
        corroborating_attempt: Optional[str] = None,
    ) -> Optional[dict]:
        """W1 — reverse a wrong verdict, auditably, without loosening the
        ledger. This is the ONLY writer of verdict_correction rows and the
        contract that separates a CORRECTION from a FABRICATION:

        * It REQUIRES a prior "answer" verdict row for (player, question) —
          a correction can only amend a ruling that actually happened. There
          is no path here to mint a point for an unadjudicated question, so
          the belt-exemption on the verdict_correction cause cannot reopen
          the fabrication hole.
        * Grounds must be in CORRECTION_GROUNDS (fails closed).
        * A verdict may be corrected once (existing_correction refuses a
          second amendment of the same verdict).
        * It only RESTORES a denied point (delta > 0). A spurious contest on
          a correctly-scored answer is refused. Negative reversal (a point
          given to the wrong player) is a FUTURE SURFACE — there is no caller
          for it yet, so the branch is not carried as dead code.
        * grounds="answer_denied" is MECHANICALLY corroborated (HOTFIX-009 W1
          harden): the recorded attempt for the contested verdict must
          fuzzy-match the canonical answer via the EXISTING Tier-1 matcher
          (lily_evaluation.lily_tier1_evaluate). Absent that corroboration
          the correction is refused. This converts the highest-risk ground
          from an LLM assertion into a mechanical check, so a rightly-denied
          WRONG answer (whose recorded attempt does not match canonical)
          cannot be restored. The other grounds (wrong_rule / misheard /
          out_of_window) stay judged by the caller within the existing
          bounds. NOTE: the corroborating attempt is whatever is in-session
          (the denied row's transcript, or one the caller supplies); an
          answer that was IGNORED at scoring time and survives only in
          lily_addressee_log is NOT in-session — that class routes through
          grounds="misheard" (the wrong utterance was bound), see the report.
        * The original row is never touched — a NEW row is appended carrying
          the delta, grounds, actor, and a `corrects` reference (the amended
          row's question_id + utterance_id). Standings derive from the
          ledger-including-corrections for free (ledger_scores sums every
          row); the counter is kept in step by apply_score_event, so
          reconcile stays clean.
        * Every correction hard-logs VERDICT_CORRECTED with grounds, actor
          and delta.

        Returns the correction ledger entry, or None on refusal (unrostered
        player / no prior verdict / bad grounds / already corrected /
        non-positive delta / uncorroborated answer_denied) — each refusal
        warns and mutates nothing.
        """
        state = self.players.get(player_name)
        if state is None:
            logger.warning(
                "LILY_SCORE | VERDICT_CORRECTION_UNKNOWN_PLAYER | "
                "session=%s name=%s", self.session_id, player_name,
            )
            return None
        ground = (grounds or "").strip().lower()
        if ground not in self.CORRECTION_GROUNDS:
            logger.warning(
                "LILY_SCORE | VERDICT_CORRECTION_BAD_GROUNDS | session=%s "
                "player=%s grounds=%r — refused", self.session_id,
                player_name, grounds,
            )
            return None
        original = self.ledger_row_for(player_name, question_id)
        if original is None:
            # No ruling to amend. A correction can only follow a verdict —
            # this is the line that keeps a correction from being invention.
            logger.warning(
                "LILY_SCORE | VERDICT_CORRECTION_NO_VERDICT | session=%s "
                "player=%s question_id=%s — refused (nothing to correct)",
                self.session_id, player_name, question_id,
            )
            return None
        if self.existing_correction(player_name, original.get("question_id")):
            logger.warning(
                "LILY_SCORE | VERDICT_CORRECTION_ALREADY_CORRECTED | "
                "session=%s player=%s question_id=%s — refused",
                self.session_id, player_name, original.get("question_id"),
            )
            return None
        # This tool RESTORES a denied point. delta must be positive; negative
        # reversal (taking a point off the wrong player) has no caller yet and
        # is a future surface, so it is not carried as a dead branch.
        if delta <= 0:
            logger.warning(
                "LILY_SCORE | VERDICT_CORRECTION_NON_POSITIVE_DELTA | "
                "session=%s player=%s delta=%s — refused (restoration only)",
                self.session_id, player_name, delta,
            )
            return None
        # The grounds must match reality, deterministically. A restoration
        # only applies to a verdict that actually DENIED the point — a
        # spurious contest on a correctly-scored answer (it already won its
        # point) is refused here, so a contest can never stack a second point
        # onto a right answer.
        awarded = int(original.get("points") or 0)
        already_scored = original.get("correct") is not False and awarded > 0
        if already_scored:
            logger.warning(
                "LILY_SCORE | VERDICT_CORRECTION_NOT_DENIED | session=%s "
                "player=%s question_id=%s — refused (the answer already "
                "scored; nothing was denied)", self.session_id, player_name,
                original.get("question_id"),
            )
            return None
        # HOTFIX-009 W1 harden: answer_denied is the highest-risk ground (a
        # rightly-denied WRONG answer is ledger-indistinguishable from a
        # denied CORRECT one). Corroborate it mechanically — the recorded
        # attempt must fuzzy-match the canonical answer through the existing
        # Tier-1 matcher. No canonical / no match ⇒ refuse. Other grounds
        # stay caller-judged.
        if ground == "answer_denied":
            attempt = (
                corroborating_attempt
                if corroborating_attempt is not None
                else original.get("transcript")
            )
            corroborated = bool(canonical_answer) and (
                lily_evaluation.lily_tier1_evaluate(
                    attempt or "", [canonical_answer]
                ).get("verdict") == "correct"
            )
            if not corroborated:
                logger.warning(
                    "LILY_SCORE | VERDICT_CORRECTION_UNCORROBORATED | "
                    "session=%s player=%s question_id=%s — refused "
                    "(answer_denied: recorded attempt %r does not match "
                    "canonical %r via Tier-1)", self.session_id, player_name,
                    original.get("question_id"), (attempt or "")[:60],
                    canonical_answer,
                )
                return None
        corrects = {
            "question_id": original.get("question_id"),
            "utterance_id": original.get("utterance_id"),
            "question_index": original.get("question_index"),
        }
        # A restoration re-applies answer semantics: a wrongly denied correct
        # answer becomes correct again (streak/answers_correct advance).
        entry = self.apply_score_event(
            player_name,
            cause="verdict_correction",
            correct=True,
            points=delta,
            question_id=original.get("question_id"),
            question_index=original.get("question_index"),
            # The audit trail persists via the transcript field (no DDL):
            # lily_write_score_event forwards it into lily_answers.transcript.
            transcript=f"grounds={ground} actor={actor} delta={delta:+d}",
            utterance_id=original.get("utterance_id"),
            grounds=ground,
            actor=actor,
            corrects=corrects,
        )
        if entry is not None:
            logger.info(
                "LILY_SCORE | VERDICT_CORRECTED | session=%s player=%s "
                "question_id=%s grounds=%s actor=%s delta=%+d score=%d "
                "corrects_utterance=%s",
                self.session_id, player_name, original.get("question_id"),
                ground, actor, delta, state["score"],
                original.get("utterance_id"),
            )
        return entry

    def award_bonus(self, player_name: str, points: int = 1,
                    transcript: Optional[str] = None) -> Optional[dict]:
        """Bonus point (e.g. best wrong answer of the round). Returns the
        ledger entry so the agent layer can persist the audit row."""
        return self.apply_score_event(
            player_name,
            cause="bonus",
            points=points,
            transcript=transcript,
        )

    def ledger_row_for(
        self, player_name: Optional[str], question_id: Optional[str] = None
    ) -> Optional[dict]:
        """The committed adjudication row for this player (optionally for a
        specific question) — the LAST one, since a make-good supersedes.

        WO-LILY-HOTFIX-006 N9 part 3: a verdict spoken about a player's
        answer generates from THIS. At 21:10 the conversational lane said
        "Jupiter was spot on, Rami" while Rami's committed q_1052 row read
        incorrect with transcript "Go." — narration and ledger describing
        two different utterances. Whatever is spoken about a player's
        answer must be readable off the row that was actually written."""
        if not player_name:
            return None
        found = None
        for entry in self.score_ledger:
            if entry.get("cause") != "answer":
                continue
            if entry.get("player") != player_name:
                continue
            if question_id is not None and entry.get("question_id") != question_id:
                continue
            found = entry
        return found

    def ledger_scores(self) -> dict[str, int]:
        """Per-player score totals derived from the ledger — the standings
        truth. Every rostered player appears (0 without entries)."""
        sums: dict[str, int] = {name: 0 for name in self.players}
        for entry in self.score_ledger:
            name = entry.get("player")
            if name in sums:
                sums[name] += int(entry.get("points") or 0)
        return sums

    def reconcile_scores(self) -> list[dict]:
        """Wrap-up reconciliation (WS-7): compare the per-player counters
        against ledger sums. Any drift means something wrote a score
        outside the choke point — hard-logged per player. Returns the
        mismatch list (empty when clean)."""
        sums = self.ledger_scores()
        mismatches = []
        for name, state in self.players.items():
            counter = int(state.get("score", 0))
            ledger = sums.get(name, 0)
            if counter != ledger:
                mismatches.append(
                    {"player": name, "counter": counter, "ledger": ledger}
                )
                logger.error(
                    "LILY_SCORE | RECONCILE_MISMATCH | session=%s player=%s "
                    "counter=%d ledger=%d",
                    self.session_id, name, counter, ledger,
                )
        if not mismatches:
            logger.info(
                "LILY_SCORE | RECONCILE_OK | session=%s players=%d entries=%d",
                self.session_id, len(self.players), len(self.score_ledger),
            )
        return mismatches

    def use_lifeline(self, player_name: str) -> bool:
        state = self.players.get(player_name)
        if state is not None and state.get("lifeline_available", False):
            state["lifeline_available"] = False
            return True
        return False

    def set_mode(self, mode: str) -> None:
        if mode not in ("general", "adult"):
            return
        if mode != self.mode:
            logger.info(
                "LILY_STATE | MODE_CHANGE | session=%s from=%s to=%s",
                self.session_id, self.mode, mode,
            )
            self.mode_changes.append({
                "from": self.mode,
                "to": mode,
                "at_question": self.question_number,
            })
        self.mode = mode
        # No residue: leaving adult always resets image intensity.
        if mode == "general":
            self.adult_image_intensity = "suggestive"
        # VIDEOIN-001 V3 constraint 2 (structural): entering the adult deck
        # CLOSES and disables the camera lane for the session-mode — an open
        # camera and adult content must never coexist. Not a setting.
        if mode == "adult" and self.camera_lane != "off":
            logger.info(
                "LILY_STATE | CAMERA_LANE_CLOSED | session=%s reason=adult_mode",
                self.session_id,
            )
            self.camera_lane = "off"

    def set_camera_lane(self, state: str) -> bool:
        """VIDEOIN-001: open/close the sparse camera lane. Structurally
        REFUSED in the adult deck (open camera never coexists with adult
        content). Returns True if the state was accepted."""
        value = (state or "").strip().lower()
        if value not in ("off", "open"):
            return False
        if value == "open" and self.mode == "adult":
            logger.info(
                "LILY_STATE | CAMERA_LANE_REFUSED | session=%s reason=adult_mode",
                self.session_id,
            )
            return False
        if value != self.camera_lane:
            logger.info(
                "LILY_STATE | CAMERA_LANE | session=%s from=%s to=%s",
                self.session_id, self.camera_lane, value,
            )
        self.camera_lane = value
        return True

    def set_adult_image_intensity(self, intensity: str) -> bool:
        """Sticky adult image heat: suggestive | explicit | mix (P3).
        Returns True if the value was accepted (even if unchanged)."""
        value = (intensity or "").strip().lower()
        if value not in ("suggestive", "explicit", "mix"):
            return False
        if value != self.adult_image_intensity:
            logger.info(
                "LILY_STATE | ADULT_IMAGE_INTENSITY | session=%s "
                "from=%s to=%s",
                self.session_id, self.adult_image_intensity, value,
            )
        self.adult_image_intensity = value
        return True

    def set_media_mode(self, mode: str) -> None:
        """Sticky media flag (sub-agent K): flips instantly, in code, on
        the deterministic spoken choice — never by the LLM."""
        if mode not in ("voice_only", "pictures"):
            return
        if mode != self.media_mode:
            logger.info(
                "LILY_STATE | MEDIA_MODE_CHANGE | session=%s from=%s to=%s",
                self.session_id, self.media_mode, mode,
            )
        self.media_mode = mode

    def set_phase(self, phase: str) -> None:
        if phase in ("lobby", "round", "final", "wrapup"):
            self.phase = phase

    def set_pacing(self, pacing: str) -> None:
        """Flip the sticky pacing flag (group prefs WO). Invalid values are
        ignored — the flag can only ever hold "timed" or "relaxed"."""
        if pacing not in ("timed", "relaxed"):
            return
        if pacing != self.pacing:
            logger.info(
                "LILY_STATE | PACING_CHANGE | session=%s from=%s to=%s",
                self.session_id, self.pacing, pacing,
            )
        self.pacing = pacing

    # -- round format (multiple-choice WO) -----------------------------------

    def set_round_format(self, fmt: str) -> bool:
        """Explicit format request (lily_set_round_format tool — the table
        asked). Callable in any phase; takes effect immediately (current
        round) and sticks for following rounds until changed again."""
        if fmt not in ROUND_FORMATS:
            return False
        if fmt != self.round_format:
            logger.info(
                "LILY_STATE | ROUND_FORMAT | session=%s from=%s to=%s source=tool",
                self.session_id, self.round_format, fmt,
            )
        self.round_format_override = fmt
        self.round_format = fmt
        return True

    def format_for_round(self, rnd: int) -> str:
        """The format a given round runs in: the explicit override when the
        table has asked, else the default schedule (round DEFAULT_MC_ROUND
        is the session's one multiple-choice round)."""
        if self.round_format_override is not None:
            return self.round_format_override
        return "multiple_choice" if rnd == DEFAULT_MC_ROUND else "freeform"

    def apply_round_format_for_round(self, rnd: int) -> None:
        """Round-boundary bookkeeping: flip round_format to whatever the
        schedule/override says this round runs in."""
        fmt = self.format_for_round(rnd)
        if fmt != self.round_format:
            logger.info(
                "LILY_STATE | ROUND_FORMAT | session=%s from=%s to=%s source=schedule round=%d",
                self.session_id, self.round_format, fmt, rnd,
            )
        self.round_format = fmt

    # -- honest failure notes (§11.2) ----------------------------------------

    # -- agent-turn record + SAID-ALREADY ledger (RECOGNITION-VARIETY 0/3) ----

    AGENT_TURNS_CAP = 40
    LEDGER_CAP = 24
    # Praise formulas the fixture player mock-echoed ("Fantastic.") — the
    # ledger tracks which have been SPENT this session.
    PRAISE_WORDS = (
        "fantastic", "amazing", "incredible", "brilliant", "wonderful",
        "excellent", "perfect", "spectacular", "gorgeous", "beautiful",
        "outstanding", "magnificent", "superb", "stellar", "impressive",
        "phenomenal", "glorious", "marvelous", "legendary", "unstoppable",
    )
    # Feature/rule topics — once explained, they live on the ledger and are
    # never re-explained unprompted (keys mirror the capabilities manifest).
    TOPIC_MARKERS = {
        "multiple_choice": ("multiple choice", "four options", "a, b, c"),
        "fifty_fifty": ("50/50", "fifty fifty", "fifty-fifty"),
        "steal": ("steal",),
        "skip": ("skip",),
        "pacing": ("relaxed", "timed rounds"),
        "pictures": ("picture round", "pictures on", "picture rounds"),
        "adult_deck": ("grown-up deck", "adult deck", "18 or older"),
        "forget_me": ("forget me", "forget you"),
        "voice_presets": ("switch my voice", "other voice", "two voices"),
        "wager": ("wager",),
        "bonus_points": ("points climb", "double points", "worth more"),
    }

    def record_agent_turn(
        self,
        text: str,
        *,
        timestamp: Optional[float] = None,
        act_keys: Optional[list] = None,
        interrupted: bool = False,
    ) -> None:
        """Record one of LILY'S OWN spoken turns (post-say-gate final text,
        at playout). Feeds three consumers: the session-report transcript
        (interleaved with player turns), the say-gate repeat lint
        (agent_turns), and the SAID-ALREADY ledger the state block carries
        so nothing gets delivered twice. Pure local state — persistence is
        the caller's fire-and-forget."""
        clean = (text or "").strip()
        if not clean:
            return
        recorded = clean + (" …[cut off]" if interrupted else "")
        self.transcript_buffer.append({
            "speaker": "LILY",
            "speaker_label": "LILY",
            "text": recorded,
            "timestamp": timestamp,
            "acts": list(act_keys or []),
        })
        if len(self.transcript_buffer) > TRANSCRIPT_BUFFER_SIZE:
            self.transcript_buffer = self.transcript_buffer[-TRANSCRIPT_BUFFER_SIZE:]

        self.agent_turns.append(clean)
        if len(self.agent_turns) > self.AGENT_TURNS_CAP:
            self.agent_turns = self.agent_turns[-self.AGENT_TURNS_CAP:]

        lowered = clean.lower()
        for word in self.PRAISE_WORDS:
            if word in lowered and word not in self.said_praise:
                self.said_praise.append(word)
        opener = " ".join(
            w for w in lowered.replace("—", " ").split()[:4] if w
        ).strip(".!,?")
        if opener and opener not in self.said_openers:
            self.said_openers.append(opener)
            self.said_openers = self.said_openers[-self.LEDGER_CAP:]
        for topic, markers in self.TOPIC_MARKERS.items():
            if topic not in self.said_topics and any(
                m in lowered for m in markers
            ):
                self.said_topics.append(topic)
        self.said_praise = self.said_praise[-self.LEDGER_CAP:]
        self.said_topics = self.said_topics[-self.LEDGER_CAP:]

    def said_already_lines(self) -> list:
        """Compact SAID-ALREADY ledger for the state block. Empty session →
        no lines (inject nothing)."""
        lines = []
        if self.said_praise:
            lines.append(
                "praise words already spent (mint fresh ones): "
                + ", ".join(self.said_praise[-10:])
            )
        if self.said_openers:
            lines.append(
                "turn openers already used (open differently): "
                + " | ".join(self.said_openers[-6:])
            )
        if self.said_topics:
            lines.append(
                "already explained (never re-explain unprompted): "
                + ", ".join(self.said_topics)
            )
        return lines

    def set_status_note(self, note: str) -> None:
        if note not in self.status_notes:
            self.status_notes.append(note)
            self.status_notes = self.status_notes[-5:]

    def clear_status_notes(self) -> None:
        self.status_notes = []

    # -- state block ---------------------------------------------------------

    def build_state_block(self, now: Optional[float] = None) -> str:
        """Compact state block injected before each Lily turn. Full render —
        byte-identical to the historical shape: stable lines with the
        volatile window/candidate lines in their historical mid-block
        position."""
        pre, post = self._stable_state_lines(now=now)
        return "\n".join(pre + self.volatile_state_lines(now=now) + post)

    def build_state_block_stable(self, now: Optional[float] = None) -> str:
        """The state block WITHOUT the per-turn volatile lines (answer
        window + candidates). This is what the preemptive-generation
        equivalence check may see: it changes only when the game genuinely
        moves (phase, scores, question), so a speculative run across an
        ordinary turn boundary stays valid (P2 volatile-tail split)."""
        pre, post = self._stable_state_lines(now=now)
        return "\n".join(pre + post)

    def volatile_state_lines(self, now: Optional[float] = None) -> list:
        """The per-turn volatile tail: answer window state + live answer
        candidates. Injected per-generation only (llm_node copy) — never
        into the persistent context the preemptive check compares."""
        window = "open" if self.is_window_open(now=now) else "closed"
        lines = [
            f"answer_window={window} candidates={len(self.answer_candidates)}"
        ]
        if self.answer_candidates:
            for c in self.ordered_candidates():
                who = c["player"] or f"unbound voice {c['speaker_label']}"
                lines.append(f"  answered: {who}: {c['text']!r}")
        return lines

    def _stable_state_lines(
        self, now: Optional[float] = None
    ) -> tuple[list, list]:
        """(pre, post): the stable lines before and after the volatile
        window/candidate chunk, preserving the historical full-block order."""
        lines = ["[GAME STATE]"]
        # question is shown WITHIN-ROUND (1..questions_per_round): the raw
        # cumulative count rendered against per-round size ("question=7/6")
        # read as overtime and steered the host toward wrapping the game.
        q_in_round = (
            ((self.question_number - 1) % self.questions_per_round) + 1
            if self.question_number > 0 else 0
        )
        total_questions = self.rounds_total * self.questions_per_round
        lines.append(
            f"phase={self.phase} round={self.round}/{self.rounds_total} "
            f"mode={self.mode} pacing={self.pacing} "
            f"format={self.round_format} media={self.media_mode} "
            f"adult_image={self.adult_image_intensity} "
            f"question={q_in_round}/{self.questions_per_round} in this round "
            f"(#{self.question_number} of {total_questions} total, "
            f"then one final wager question) "
            f"category={self.category or '-'}"
        )
        if self.pacing == "relaxed":
            # Group prefs WO prompt note: the flag alone doesn't change how
            # she talks — this line does. Timed carries no note (today's
            # behavior).
            lines.append(
                "relaxed pacing: looser tempo — the answer window runs "
                "longer on purpose. Give the table room to think; no "
                "countdown talk, no rushing anyone, no 'quickly now'."
            )
        if self.players:
            for name, s in sorted(
                self.players.items(), key=lambda kv: -kv[1]["score"]
            ):
                bits = [f"{name}: score={s['score']} streak={s['streak']}"]
                if s.get("questions_since_spoke", 0) >= 3:
                    bits.append(f"quiet for {s['questions_since_spoke']} questions")
                if s.get("lobby_fact"):
                    bits.append(f"fact: {s['lobby_fact']}")
                if not s.get("lifeline_available", True):
                    bits.append("lifeline used")
                lines.append("  " + " | ".join(bits))
        else:
            lines.append("  (no players bound yet)")
        if self.current_question:
            q = self.current_question
            # NEED-TO-KNOW (say-gate WO): the ambient state block NEVER
            # carries canonical_answer / acceptable_answers / reveal_color.
            # A transcript showed the vocal node reading the Bosporus
            # answer aloud BEFORE the question — the answer reaches the
            # vocal node only via the reveal-time instructed reply, and
            # the Tier-2 judge gets it in its dedicated call.
            lines.append(f"current_question: {q.get('prompt', '-')!r}")
        post = []
        if self.unrostered_labels:
            labels = ", ".join(sorted(self.unrostered_labels))
            post.append(f"unbound voices heard: {labels}")
        for note in self.status_notes:
            post.append(f"note: {note}")
        return lines, post

    # -- checkpoint snapshot ---------------------------------------------------

    def snapshot(self) -> dict:
        return {
            "session_id": self.session_id,
            "phase": self.phase,
            "round": self.round,
            "mode": self.mode,
            "pacing": self.pacing,
            "delivery_pace": self.delivery_pace,
            "round_format": self.round_format,
            "round_format_override": self.round_format_override,
            "media_mode": self.media_mode,
            "adult_image_intensity": self.adult_image_intensity,
            "category": self.category,
            "question_number": self.question_number,
            "questions_per_round": self.questions_per_round,
            "rounds_total": self.rounds_total,
            "current_question": self.current_question,
            "players": {name: dict(s) for name, s in self.players.items()},
            "unrostered_labels": dict(self.unrostered_labels),
            "status_notes": list(self.status_notes),
            "mode_changes": list(self.mode_changes),
            "score_ledger": [dict(e) for e in self.score_ledger],
        }

    def rehydrate(self, snap: dict) -> None:
        """Restore scores and round position from a checkpoint snapshot."""
        if not snap:
            return
        self.phase = snap.get("phase", self.phase)
        self.round = snap.get("round", self.round)
        self.mode = snap.get("mode", self.mode)
        self.pacing = snap.get("pacing", self.pacing)
        self.delivery_pace = snap.get("delivery_pace", self.delivery_pace)
        if self.delivery_pace not in ("normal", "slow"):
            self.delivery_pace = "normal"
        self.round_format = snap.get("round_format", self.round_format)
        self.round_format_override = snap.get(
            "round_format_override", self.round_format_override
        )
        self.media_mode = snap.get("media_mode", self.media_mode)
        self.adult_image_intensity = snap.get(
            "adult_image_intensity", self.adult_image_intensity
        )
        if self.adult_image_intensity not in ("suggestive", "explicit", "mix"):
            self.adult_image_intensity = "suggestive"
        self.category = snap.get("category", self.category)
        self.question_number = snap.get("question_number", self.question_number)
        self.questions_per_round = snap.get(
            "questions_per_round", self.questions_per_round
        )
        self.rounds_total = snap.get("rounds_total", self.rounds_total)
        self.current_question = snap.get("current_question")
        self.current_answer = (self.current_question or {}).get("canonical_answer")
        self.mode_changes = list(snap.get("mode_changes") or [])
        for name, s in (snap.get("players") or {}).items():
            self.players[name] = dict(s)
        # Score-ledger restore (WS-7): the ledger travels with the
        # checkpoint. Legacy snapshots (or any player whose restored
        # counter outruns the restored ledger) get a "rehydrate" seed
        # entry for the difference, so ledger sums == counters holds from
        # the first post-restore reconcile.
        self.score_ledger.extend(
            dict(e) for e in (snap.get("score_ledger") or [])
        )
        sums = self.ledger_scores()
        for name, state in self.players.items():
            diff = int(state.get("score", 0)) - sums.get(name, 0)
            if diff != 0:
                self.score_ledger.append({
                    "player": name,
                    "cause": "rehydrate",
                    "correct": None,
                    "points": diff,
                    "question_id": None,
                    "question_index": self.question_number,
                    "transcript": None,
                    "eval_tier": None,
                    "score_after": int(state.get("score", 0)),
                    "ts": _now_iso(),
                })
        logger.info(
            "LILY_STATE | REHYDRATED | session=%s players=%d phase=%s round=%d",
            self.session_id, len(self.players), self.phase, self.round,
        )
