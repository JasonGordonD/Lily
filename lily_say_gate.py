"""
lily_say_gate.py — LILY outbound-speech gate.

This module is the DESIGNATED CHOKE POINT for ALL outbound speech
hygiene. It carries (a) the pure text-hygiene functions applied in
lily_agent's tts_node before synthesis (P4, 2026-07-14 observability
review): deterministic markdown/emoji stripping with the empty-result
guard; (b) the say-gate WO extensions (Dr. Tijoux consolidated
directive, 2026-07-14): the SpeechActRegistry for idempotent speech
acts (double greeting / BUG-2 double question delivery), and the
state-block leak filter (sentinel envelope + bracketed-metadata line
suppression) so injected ambient context can never be read aloud.
Every rule about what Lily may say out loud lives behind this one
gateway. Add new outbound rules HERE, not inline in tts_node.

Contract:
  - Square-bracket [tags] are PRESERVED verbatim. They are ElevenLabs v3
    audio tags ([excited], [whispering], [pause], accent tags, ...) —
    load-bearing TTS controls, not markdown. We deliberately preserve
    ALL bracket content rather than maintaining a known-tag allowlist.
  - What gets stripped: markdown emphasis asterisks (**bold**, *italic*,
    nested), backticks (inline and fenced), ATX headers (#, ##, ...),
    leading bullet markers (-, *, •), emoji/pictographs (Unicode symbol
    blocks — the Rafiq empty-text-guard ranges), and the resulting
    double spaces.
  - Empty-result guard: text that strips to nothing (emoji-only turns)
    comes back as "" so the caller's empty-candidate path engages
    instead of shipping junk to the synthesizer (an empty synthesis
    request is an ElevenLabs 400).

Pure stdlib, no livekit imports — unit-testable without the agent
runtime.
"""

import re

# Emoji / pictograph blocks — structural reference: the Rafiq
# empty-text-guard ranges (prmpt_common elevenlabs TTS), extended with
# the misc-symbols and arrows-supplement blocks (star, sparkles, warning
# signs) that Gemini favors in celebratory text.
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001F5FF"  # misc symbols & pictographs
    "\U0001F600-\U0001F64F"  # emoticons
    "\U0001F680-\U0001F6FF"  # transport & map
    "\U0001F1E6-\U0001F1FF"  # regional indicator flags
    "\U0001F900-\U0001F9FF"  # supplemental symbols & pictographs
    "\U0001FA00-\U0001FAFF"  # chess symbols + extended-A/B
    "\U00002600-\U000026FF"  # misc symbols (sun, star, warning...)
    "\U00002700-\U000027BF"  # dingbats
    "\U00002B00-\U00002BFF"  # arrows supplement (incl. U+2B50 star)
    "\U0000FE00-\U0000FE0F"  # variation selectors
    "\U0000200D"             # zero-width joiner
    "\U000020E3"             # combining enclosing keycap
    "]+"
)

# ATX headers at line start: "# ", "## Title", up to ######.
_HEADER_RE = re.compile(r"(?m)^[ \t]{0,3}#{1,6}[ \t]+")
# Bullet markers at line start: "- item", "* item", "• item".
_BULLET_RE = re.compile(r"(?m)^[ \t]*[-*•][ \t]+")
# Markdown emphasis / code markers anywhere: runs of asterisks and
# backticks are deleted outright (handles bold, italic, nesting, and
# fenced/inline code markers in one pass). Square brackets untouched.
_EMPHASIS_RE = re.compile(r"[*`]+")
# Whitespace collapse for the holes the strips leave behind.
_MULTISPACE_RE = re.compile(r"[ \t]{2,}")
_SPACE_BEFORE_PUNCT_RE = re.compile(r" +([,.!?;:])")


def lily_strip_emoji(text: str) -> str:
    """Remove emoji/pictograph codepoints. Pure; never touches [tags]."""
    return _EMOJI_RE.sub("", text or "")


def lily_strip_markdown(text: str) -> str:
    """Remove spoken-markdown artifacts: headers, bullet markers,
    asterisk emphasis, backticks. Square-bracket audio tags are
    preserved verbatim (they are TTS controls, not markdown)."""
    t = text or ""
    t = _HEADER_RE.sub("", t)
    t = _BULLET_RE.sub("", t)
    t = _EMPHASIS_RE.sub("", t)
    return t


def lily_clean_for_speech(text: str) -> str:
    """The gate: full outbound hygiene pass for one spoken turn.

    Markdown strip + emoji strip + whitespace collapse, preserving all
    [bracket] audio tags. Returns "" when nothing speakable survives
    (the caller's empty-candidate guard handles that case)."""
    t = lily_strip_markdown(text)
    t = lily_strip_emoji(t)
    t = _MULTISPACE_RE.sub(" ", t)
    t = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", t)
    # Collapse whitespace-only lines left behind by stripped bullets or
    # emoji-only lines, then trim the whole turn.
    lines = [ln.rstrip() for ln in t.splitlines()]
    t = "\n".join(ln for ln in lines if ln.strip())
    return t.strip()


# ---------------------------------------------------------------------------
# State-block leak filter (say-gate WO §1)
#
# The injected [GAME STATE] block rides as SYSTEM-role context wrapped in a
# sentinel envelope. If any of it echoes into an outbound spoken turn (the
# observed live bug class: state block read aloud, the Bosporus answer
# spoken BEFORE the question), the filter below deterministically strips
# it. Suppression is line-based plus envelope-based so a partial fragment
# ("<lily_state" cut at a chunk boundary) is caught too. Ordinary
# [bracket] audio tags are untouched — only the exact metadata markers
# fire.
# ---------------------------------------------------------------------------

LILY_STATE_SENTINEL_OPEN = "<lily_state>"
LILY_STATE_SENTINEL_CLOSE = "</lily_state>"

# Envelope fragments: any partial/whole spelling of the sentinel tag.
_SENTINEL_FRAGMENT_RE = re.compile(r"<\s*/?\s*lily_state\b|lily_state\s*>", re.IGNORECASE)
# Whole envelope (open ... close), non-greedy, spans lines.
_SENTINEL_ENVELOPE_RE = re.compile(
    r"<\s*lily_state\b[^>]*>.*?</\s*lily_state\s*>",
    re.IGNORECASE | re.DOTALL,
)

# Bracketed metadata line markers — the injected-context headers that must
# never be spoken. Matched case-insensitively anywhere in a line; a line
# containing one is dropped whole (it is metadata, not speech).
LEAK_LINE_MARKERS = (
    "[GAME STATE]",
    "[room read:",
    "[env:",
    "[RETURNING TABLE]",
)


def lily_filter_leaks(text: str) -> tuple[str, list[str]]:
    """Strip leaked injected-context from one outbound spoken turn.

    Returns (filtered_text, leak_reasons). leak_reasons is empty for clean
    text (the common case — text comes back unchanged); non-empty means a
    leak was detected and removed, and the caller must log
    LILY_SAY_SUPPRESSED | reason=leak and run the burn protocol.

    Removal order:
      1. whole sentinel envelopes (<lily_state>...</lily_state>) — the
         entire injected block, however it got echoed;
      2. any remaining line carrying a sentinel FRAGMENT (chunk-boundary
         partials like "<lily_state" or "lily_state>");
      3. any line carrying a bracketed metadata marker ([GAME STATE],
         [room read:, [env:, [RETURNING TABLE]).

    Pure and deterministic; audio [tags] and clean text pass untouched.
    """
    t = text or ""
    reasons: list[str] = []

    if _SENTINEL_ENVELOPE_RE.search(t):
        t = _SENTINEL_ENVELOPE_RE.sub("", t)
        reasons.append("sentinel_envelope")

    kept_lines: list[str] = []
    for line in t.splitlines():
        if _SENTINEL_FRAGMENT_RE.search(line):
            reasons.append("sentinel_fragment")
            continue
        lowered = line.lower()
        marker = next(
            (m for m in LEAK_LINE_MARKERS if m.lower() in lowered), None
        )
        if marker is not None:
            reasons.append(f"metadata:{marker}")
            continue
        kept_lines.append(line)
    filtered = "\n".join(kept_lines)
    if not reasons:
        return text or "", []
    return filtered.strip(), reasons


def lily_wrap_state_block(block: str) -> str:
    """Wrap the injected state block in the sentinel envelope. The
    envelope is what makes an echoed block deterministically strippable
    by lily_filter_leaks before synthesis."""
    return f"{LILY_STATE_SENTINEL_OPEN}\n{block}\n{LILY_STATE_SENTINEL_CLOSE}"


# ---------------------------------------------------------------------------
# Idempotent speech acts (say-gate WO §1)
#
# Game-critical speech acts are claimed AT DISPATCH TIME — never at
# playback completion; the dispatch→playout gap is exactly the race window
# that produced the live double greeting and double question delivery
# (BUG-2). Keys:
#   session_greet, session_rejoin, q_{N}_delivery, q_{N}_reveal,
#   round_{N}_scores, finale
#
# Lifecycle: claim(key) at dispatch (atomic check-and-set; a second claim
# fails and the duplicate speech is suppressed) -> confirm at playout
# completion -> release on playback FAILURE (empty candidate after
# hygiene, swallowed turn) so a retry can legitimately redeliver a
# claimed-but-never-played act. Pure stdlib; single-threaded asyncio makes
# the dict check-and-set atomic (no awaits inside).
# ---------------------------------------------------------------------------

CLAIM_PENDING = "pending"
CLAIM_CONFIRMED = "confirmed"


class SpeechActRegistry:
    """Registry of claimed speech-act keys for one session."""

    def __init__(self) -> None:
        self._acts: dict[str, str] = {}  # key -> pending | confirmed

    def claim(self, key: str) -> bool:
        """Atomic check-and-set at dispatch time. True = caller may speak;
        False = the act was already claimed (duplicate — suppress)."""
        if key in self._acts:
            return False
        self._acts[key] = CLAIM_PENDING
        return True

    def state(self, key: str):
        """CLAIM_PENDING, CLAIM_CONFIRMED, or None (unclaimed)."""
        return self._acts.get(key)

    def confirm(self, key: str) -> None:
        """Playout completed — the act was genuinely spoken."""
        if key in self._acts:
            self._acts[key] = CLAIM_CONFIRMED

    def confirm_pending(self) -> list[str]:
        """Confirm every pending claim (agent playout completed). Returns
        the confirmed keys."""
        confirmed = [k for k, v in self._acts.items() if v == CLAIM_PENDING]
        for k in confirmed:
            self._acts[k] = CLAIM_CONFIRMED
        return confirmed

    def release(self, key: str) -> bool:
        """Release ONE claim after playback failure so a retry can
        redeliver. Only pending (never-played) claims release; a confirmed
        act stays done forever. True if released."""
        if self._acts.get(key) == CLAIM_PENDING:
            del self._acts[key]
            return True
        return False

    def release_pending(self) -> list[str]:
        """Release every pending claim (the swallowed-turn path: dispatch
        happened, playback produced nothing). Returns the released keys so
        the caller can relog them."""
        released = [k for k, v in self._acts.items() if v == CLAIM_PENDING]
        for k in released:
            del self._acts[k]
        return released
