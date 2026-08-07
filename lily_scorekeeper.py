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
# Negated timed is a relaxed request: "no timed rounds", "don't want it
# timed", "not timed". The negation word must IMMEDIATELY precede "timed"
# (give or take a want/the/it) so "timed rounds, not relaxed" never
# misreads. (Apostrophes normalize to spaces: "don t".)
_PACING_TIMED_NEGATION_RE = _re.compile(
    r"\b(?:no|not|don t want|dont want|do not want|won t do|without)"
    r"\s+(?:the\s+|it\s+|any\s+|them\s+)?timed\b"
)


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
    "pacing_relaxed", "pacing_timed", or None. A negated timed ("no timed
    rounds") is a relaxed request; when BOTH directions fire un-negated
    the utterance is ambiguous — returns None (the prompt/tool layer sorts
    it out conversationally; nothing flips on ambiguity)."""
    if _PACING_TIMED_NEGATION_RE.search(text_normalized):
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
    r"|let\s?s (?:start|play)"
    r"|start round one"
    r")\b"
)

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
_STOP_ADDRESSED_RE = _re.compile(
    rf"\b(?:lily|lilly|lil)\b[\s,.!]*(?:{_STOP_WORD})"
    rf"|(?:{_STOP_WORD})\b[\s,.!]*\b(?:lily|lilly|lil)\b"
)
_STOP_NEGATION_RE = _re.compile(r"\b(?:do not|dont|don t|please dont|never)\s+st")


def lily_detect_stop(text: str, *, solo: bool = False) -> bool:
    """True when this utterance is an addressed STOP (or a bare stop in a
    solo room) — the runaway-agent brake. Deterministic + garble-tolerant.
    Word-bounded (never 'stopwatch'/'unstoppable') and negation-guarded
    ('don't stop')."""
    normalized = _normalize_command_text(text)
    if not normalized or _STOP_NEGATION_RE.search(normalized):
        return False
    if _STOP_ADDRESSED_RE.search(normalized):
        return True
    return bool(solo and _STOP_CORE_RE.search(normalized))


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
    """True only for an explicit, affirmative 18+ consent utterance. The
    deterministic floor the adult-mode gate requires IN ADDITION to the
    model's flag — a question or verification prompt can never satisfy it."""
    normalized = _normalize_command_text(text)
    if not normalized or _AGE_CONSENT_NEGATION_RE.search(normalized):
        return False
    return bool(_AGE_CONSENT_RE.search(normalized))


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
    r"|picture rounds?"
    r"|use the screen"
    r"|screen on"
    r")\b"
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
    if _MEDIA_VOICE_ONLY_RE.search(normalized):
        return "voice_only"
    if _MEDIA_PICTURES_RE.search(normalized):
        return "pictures"
    return None


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
    ) -> dict:
        """
        Bind a diarization label to a player name (lily_bind_speaker tool).
        Re-binding an existing player updates their label (late-session
        label drift resolves through the same path).
        """
        name = player_name.strip()
        # If this label was bound to someone else, release it there.
        for other_name, state in self.players.items():
            if other_name != name and state.get("speaker_label") == speaker_label:
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

    def roster_size(self) -> int:
        return len(self.players)

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
    ) -> None:
        """Open the answer window (called on TTS playback-completion).
        reset_candidates=False reopens for a steal window: prior candidates
        are kept so one-candidate-per-player still holds and only new
        players can commit."""
        t = now if now is not None else time.time()
        self.answer_window_open = True
        self.answer_window_opened_at = t
        self.answer_window_deadline = t + (
            duration if duration is not None else self.answer_window_seconds
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
            "LILY_STATE | ANSWER_WINDOW_OPEN | session=%s q=%d deadline_in=%.1fs",
            self.session_id, self.question_number,
            (self.answer_window_deadline - t),
        )

    def close_answer_window(self) -> None:
        if self.answer_window_open:
            logger.info(
                "LILY_STATE | ANSWER_WINDOW_CLOSED | session=%s q=%d candidates=%d",
                self.session_id, self.question_number, len(self.answer_candidates),
            )
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
        when no spoken timestamp exists (arrival_time-source finals)."""
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
            and spoken_ts > self.answer_window_deadline
        ):
            return False
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
            # PATCH-001 T5(c) evaluator hygiene: a backchannel ("Yeah") or
            # a bare roster-name fragment ("Chris.") in an open window is
            # LOGGED, never adjudicated as an attempt — the live fixtures
            # scored "Yeah" incorrect and wrote a null-player "Chris." row,
            # consuming those players' judgments. Answer-surface matches
            # always pass (a yes/no question keeps "yeah" scoreable).
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
                }
                if segment_conf is not None:
                    attempt_entry["addressee_confidence"] = segment_conf
                self.answer_candidates[key] = {
                    "player": player,
                    "speaker_label": speaker_label,
                    "text": clean,
                    "segment_start_time": seg_start,
                    "segment_end_time": seg_end,
                    "timestamp": ts,
                    "unrostered": player is None,
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
    ) -> Optional[dict]:
        """The SOLE score writer (WS-7). Every scoring mutation — an
        adjudicated result, a bonus, a make-good, an operator-directed
        award — goes through here and lands one score_ledger entry with a
        cause code. Nothing else may touch players[name]["score"].
        WS-4's idempotent award keying bolts onto this entry point.

        Cause vocabulary: "answer" (adjudication commit, correct set),
        "bonus", "make_good", "operator_award", "rehydrate" (checkpoint
        seed). correct=True applies answer semantics (streak/answers_correct
        advance); correct=False resets the streak and forces points to 0;
        correct=None moves points without touching the streak.

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
        if question_id is not None and points and correct is not False:
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
            "eval_tier": eval_tier,
            "score_after": state["score"],
            "ts": _now_iso(),
        }
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
    ) -> Optional[dict]:
        """Commit an adjudicated result. Event-bound truth: Lily never
        announces a score change that hasn't landed here first. Delegates
        to apply_score_event (cause="answer") — the single write path."""
        entry = self.apply_score_event(
            player_name,
            cause="answer",
            correct=correct,
            points=points,
            category=category,
            question_id=question_id,
            transcript=transcript,
            eval_tier=eval_tier,
        )
        if entry is None:
            return None
        return self.players.get(player_name)

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
        """Compact state block injected before each Lily turn."""
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
        window = "open" if self.is_window_open(now=now) else "closed"
        lines.append(f"answer_window={window} candidates={len(self.answer_candidates)}")
        if self.answer_candidates:
            for c in self.ordered_candidates():
                who = c["player"] or f"unbound voice {c['speaker_label']}"
                lines.append(f"  answered: {who}: {c['text']!r}")
        if self.unrostered_labels:
            labels = ", ".join(sorted(self.unrostered_labels))
            lines.append(f"unbound voices heard: {labels}")
        for note in self.status_notes:
            lines.append(f"note: {note}")
        return "\n".join(lines)

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
