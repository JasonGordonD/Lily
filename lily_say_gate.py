"""
lily_say_gate.py — LILY outbound-speech hygiene gate.

This module is the DESIGNATED CHOKE POINT for ALL outbound speech
hygiene. Today it carries the pure text-hygiene functions applied in
lily_agent's tts_node before synthesis (P4, 2026-07-14 observability
review): deterministic markdown/emoji stripping with the empty-result
guard. The follow-up say-gate WO extends this module into a full
outbound-speech gate — idempotent speech acts, state-block leak filter,
duplicate suppression — so every rule about what Lily may say out loud
lives behind one gateway. Add new outbound hygiene HERE, not inline in
tts_node.

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
