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

The scorekeeper owns ORDER; the LLM owns CORRECTNESS.
"""

import logging
import re as _re
import time
from datetime import datetime, timezone
from typing import Optional

import lily_config

logger = logging.getLogger("lily_scorekeeper")

TRANSCRIPT_BUFFER_SIZE = 30
DEFAULT_ANSWER_WINDOW_SECONDS = 15.0
FRAGMENT_JOIN_WINDOW_SECONDS = 2.0  # ASR fragment accumulation for commands

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

        # Rolling tagged transcript buffer (last 30 lines)
        self.transcript_buffer: list[dict] = []

        # Honest-failure status notes (spec §11.2) — reasoning-node failures
        # land here and surface in the state block.
        self.status_notes: list[str] = []

        # Mode changes recorded for the session report (B3 game_stats).
        self.mode_changes: list[dict] = []

        # Per-speaker recent finals for fragment-joined command detection
        self._recent_fragments: dict[str, list[tuple[float, str]]] = {}

        # Log-only unrostered speaker sightings
        self.unrostered_labels: dict[str, int] = {}

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

        # Path 2: diarization label
        if speaker_label:
            for name, state in self.players.items():
                if state.get("speaker_label") == speaker_label:
                    return name, "label_match"

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

    def tier1_threshold(self, now: Optional[float] = None) -> float:
        """The state-driven Tier-1 acceptance threshold (env-tunable via
        lily_config; see lily_config.tier1_threshold_for_prior)."""
        return lily_config.tier1_threshold_for_prior(self.prior_state(now=now))

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

    # -- transcript ingestion ----------------------------------------------

    def on_transcript_segment(
        self,
        text: str,
        speaker_label: Optional[str] = None,
        speaker_id: Optional[str] = None,
        speaker_name: Optional[str] = None,
        is_final: bool = True,
        segment_start_time: Optional[float] = None,
        segment_end_time: Optional[float] = None,
        now: Optional[float] = None,
        timestamp: Optional[str] = None,
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
            "prior_state": PRIOR_* string (WO-ADDRESSEE-H1 Task 2),
            "overlap_flag": bool,
          }
        """
        t = now if now is not None else time.time()
        ts = timestamp or _now_iso()
        result = {
            "player": None,
            "attribution": None,
            "system_directed": False,
            "control_command": None,
            "media_choice": None,
            "candidate_recorded": False,
            "unrostered": False,
            # Prior state (WO-ADDRESSEE-H1 Task 2) — additive keys, refined
            # below for finals after overlap detection runs.
            "prior_state": self.prior_state(now=t),
            "overlap_flag": self.overlap_flag,
        }

        if not text or not text.strip():
            return result

        if not is_final:
            # Partials never score and never mutate state.
            return result

        clean = text.strip()
        player, method = self.resolve_speaker(
            speaker_id, speaker_label, speaker_name, clean
        )
        result["player"] = player
        result["attribution"] = method

        # Overlap detection (WO-ADDRESSEE-H1 Task 2): inside the open
        # window, record this final's [segment_start, end≈now] span per
        # speaker and flip OVERLAP on cross-speaker timestamp overlap.
        # Runs BEFORE the prior is stamped so this very segment is
        # classified under the state it just created.
        if self.is_window_open(now=t):
            span_key = player or speaker_label
            if span_key:
                self._note_speaker_span(
                    str(span_key),
                    segment_start_time if segment_start_time is not None else t,
                    segment_end_time if segment_end_time is not None else t,
                )
        prior = self.prior_state(now=t)
        result["prior_state"] = prior
        result["overlap_flag"] = self.overlap_flag
        # Every classification decision logs its active prior state.
        logger.info(
            "LILY_PRIOR | state=%s threshold=%.3f overlap=%s | session=%s "
            "q=%d speaker=%s",
            prior, lily_config.tier1_threshold_for_prior(prior),
            self.overlap_flag, self.session_id, self.question_number,
            player or speaker_label or "?",
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
            self.is_window_open(now=t)
            and not is_sys
            and not command
            and not result.get("media_choice")
        ):
            seg_start = (
                segment_start_time if segment_start_time is not None else t
            )
            if player:
                key = player
            else:
                # Open-floor fallback: unrostered answer is never silently
                # attributed — recorded under its label for Lily to resolve
                # in character ("great answer — and you are?").
                key = f"unrostered:{speaker_label or 'UU'}"
            existing = self.answer_candidates.get(key)
            if existing is None:
                # First final claims the player's slot — its timestamp is
                # their position in the answer order.
                self.answer_candidates[key] = {
                    "player": player,
                    "speaker_label": speaker_label,
                    "text": clean,
                    "segment_start_time": seg_start,
                    "timestamp": ts,
                    "unrostered": player is None,
                    # Every in-window final from this player, in order —
                    # adjudication judges the whole set (self-correction,
                    # live 2026-07-15: "the spine… no, the femur" must be
                    # able to score on the femur).
                    "attempts": [
                        {"text": clean, "segment_start_time": seg_start,
                         "timestamp": ts},
                    ],
                }
                result["candidate_recorded"] = True
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
                existing.setdefault("attempts", []).append(
                    {"text": clean, "segment_start_time": seg_start,
                     "timestamp": ts}
                )
                result["candidate_recorded"] = True
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

    def record_result(
        self,
        player_name: str,
        correct: bool,
        points: int = 1,
        category: Optional[str] = None,
    ) -> Optional[dict]:
        """Commit an adjudicated result. Event-bound truth: Lily never
        announces a score change that hasn't landed here first."""
        state = self.players.get(player_name)
        if state is None:
            logger.warning(
                "LILY_STATE | SCORE_FOR_UNKNOWN_PLAYER | session=%s name=%s",
                self.session_id, player_name,
            )
            return None
        if correct:
            state["score"] += points
            state["streak"] += 1
            state["answers_correct"] = state.get("answers_correct", 0) + 1
            if category or self.category:
                state["last_correct_category"] = category or self.category
        else:
            state["streak"] = 0
        logger.info(
            "LILY_STATE | SCORE_COMMIT | session=%s player=%s correct=%s points=%d score=%d streak=%d",
            self.session_id, player_name, correct, points if correct else 0,
            state["score"], state["streak"],
        )
        return state

    def award_bonus(self, player_name: str, points: int = 1) -> None:
        """Bonus point (e.g. best wrong answer of the round)."""
        state = self.players.get(player_name)
        if state is not None:
            state["score"] += points

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
            "round_format": self.round_format,
            "round_format_override": self.round_format_override,
            "media_mode": self.media_mode,
            "category": self.category,
            "question_number": self.question_number,
            "questions_per_round": self.questions_per_round,
            "rounds_total": self.rounds_total,
            "current_question": self.current_question,
            "players": {name: dict(s) for name, s in self.players.items()},
            "unrostered_labels": dict(self.unrostered_labels),
            "status_notes": list(self.status_notes),
            "mode_changes": list(self.mode_changes),
        }

    def rehydrate(self, snap: dict) -> None:
        """Restore scores and round position from a checkpoint snapshot."""
        if not snap:
            return
        self.phase = snap.get("phase", self.phase)
        self.round = snap.get("round", self.round)
        self.mode = snap.get("mode", self.mode)
        self.pacing = snap.get("pacing", self.pacing)
        self.round_format = snap.get("round_format", self.round_format)
        self.round_format_override = snap.get(
            "round_format_override", self.round_format_override
        )
        self.media_mode = snap.get("media_mode", self.media_mode)
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
        logger.info(
            "LILY_STATE | REHYDRATED | session=%s players=%d phase=%s round=%d",
            self.session_id, len(self.players), self.phase, self.round,
        )
