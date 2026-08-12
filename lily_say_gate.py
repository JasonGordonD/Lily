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
import time
from typing import Optional

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

# Tool-call leak (LILY-P0 2026-08-12): the Grok vocal model intermittently
# emits a function call in the CONTENT stream instead of as a structured
# tool_use, so the raw JSON `{"name": "lily_bind_speaker", "arguments":
# {...}}` reaches TTS and is spoken aloud. Lily never legitimately opens a
# spoken turn with a JSON object, so a turn whose content leads with a
# tool-call-shaped object is metadata, not speech. Anchored to the object
# lead and tolerant of barge-in truncation (arguments cut mid-object).
_TOOL_CALL_LEAK_RE = re.compile(
    r'\{\s*"name"\s*:\s*"[A-Za-z0-9_]+"\s*,\s*"arguments"\s*:',
)

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
# "[state note:" is the honesty assist (WO-LILY-DESYNC-HONESTY-001
# Sub-agent C): the grounded truth injected when a player calls out a
# state desync — context for her acknowledgment, never words on the air.
LEAK_LINE_MARKERS = (
    "[GAME STATE]",
    "[room read:",
    "[env:",
    "[RETURNING TABLE]",
    "[state note:",
)


def _rest_after_json_object(t: str, start: int) -> str:
    """Return the substring AFTER the brace-balanced JSON object that begins
    at ``start``. String-aware (braces inside quoted values do not count).
    A truncated/unterminated object (barge-in cut it mid-arguments) returns
    "" — nothing after an unterminated tool call is speech."""
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(t)):
        c = t[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return t[i + 1:]
    return ""


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

    # Tool-call leak: excise a function-call JSON object that leaked into
    # spoken content. Strip the balanced object (or to end, if truncated);
    # a pure tool-call turn collapses to "" and the caller's empty-candidate
    # retry regenerates instead of airing JSON. Carries no answer material,
    # so the tts_node burn protocol must NOT fire on this reason alone.
    _tc = _TOOL_CALL_LEAK_RE.search(t)
    if _tc is not None:
        t = t[: _tc.start()] + _rest_after_json_object(t, _tc.start())
        reasons.append("tool_call")

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
# Mirror lint (WO-LILY-SELFKNOWLEDGE-INTAKE-001 Task 2a) — LOG-ONLY in v1.
#
# The sycophantic-mirror ban is enforced by the prompt contract; this lint
# exists so drift is measurable in telemetry rather than vibes. The
# fixtures showed the mirror is situational, not constant ("not letting
# you off the hook" got playful deflection) — hence a flag, never a
# suppression. Patterns target the OPENING of a turn only: flattery
# openers and agreement-echo starts, conservative by design (a false flag
# pollutes the telemetry the lint exists to provide). tts_node logs
# `LILY_SAY | MIRROR_FLAG | pattern=...` when this returns a match.
# ---------------------------------------------------------------------------

LILY_MIRROR_OPENERS: list = [
    # Flattery-opener class: appraising the utterance, first breath.
    r"(?:that|this)(?:'s| is| was)(?: such| really| honestly)? "
    r"a(?:n)? (?:fantastic|great|excellent|amazing|wonderful|brilliant|"
    r"perfect|incredible|good|fabulous) (?:point|question|idea|thought|"
    r"observation|call|catch)",
    r"(?:what a|such a) (?:fantastic|great|excellent|amazing|wonderful|"
    r"brilliant|perfect|good) (?:point|question|idea|thought|observation)",
    r"great question",
    r"fantastic point",
    r"perfect sweet spot",
    r"(?:i )?love (?:that|this) (?:point|question|idea|thought)",
    # Agreement-echo class: "exactly"/"absolutely right" as the opener.
    r"exactly[.!,—-]",
    r"(?:you're|you are) (?:absolutely|exactly|so) right",
    r"absolutely[.!,—-]",
]

_MIRROR_RES = [
    re.compile(pattern, re.IGNORECASE) for pattern in LILY_MIRROR_OPENERS
]
# Leading ElevenLabs audio tags ([excited] ...) don't shield an opener.
_MIRROR_LEAD_STRIP = re.compile(r"^(?:\s*\[[^\]]*\])*\s*")


def _repeat_norm_words(text: str) -> list:
    """Normalize a turn for repetition comparison: audio tags out,
    lowercase, punctuation stripped, whitespace-split."""
    cleaned = re.sub(r"\[[^\]]*\]", " ", text or "")
    cleaned = re.sub(r"[^\w'\s]", " ", cleaned.lower())
    return [w for w in cleaned.split() if w]


def lily_repeat_flag(text: str, previous_turns: list) -> Optional[str]:
    """Repetition lint (WO-LILY-RECOGNITION-VARIETY-001 Task 3b) —
    LOG-ONLY. Returns "opener" when the turn opens with the same leading
    4-gram as an earlier agent turn, "content" when it shares any 6-word
    run with one, else None. A player asking "explain again" is answered
    in FRESH words per the prompt law, so an honest re-answer does not
    flag — only verbatim cycling does. Pure; caller logs REPEAT_FLAG."""
    words = _repeat_norm_words(text)
    if not words or not previous_turns:
        return None
    opener = tuple(words[:4]) if len(words) >= 4 else None
    grams = {
        tuple(words[i:i + 6]) for i in range(max(0, len(words) - 5))
    }
    for prev in previous_turns:
        prev_words = _repeat_norm_words(prev)
        if opener and len(prev_words) >= 4 and tuple(prev_words[:4]) == opener:
            return "opener"
        if grams:
            prev_grams = {
                tuple(prev_words[i:i + 6])
                for i in range(max(0, len(prev_words) - 5))
            }
            if grams & prev_grams:
                return "content"
    return None


_SELF_HOLD_RE = re.compile(
    r"\b(?:take your time|no rush|whenever you(?:'re| are) ready"
    r"|i(?:'ll| will) wait|no hurry|in your own time|whenever you like"
    r"|take a (?:moment|beat|breath)|i(?:'ll| will) hold|i(?:'ll| will) pause)\b",
    re.IGNORECASE,
)


def lily_self_hold_phrase(text: str) -> bool:
    """PATCH-002 A4b — True when a played agent turn promised to wait
    ('take your time', 'no rush', 'whenever you're ready'). Her own words
    bind her: the turn that says it enters the hold, so she can't then
    keep talking 5s later. Pure."""
    return bool(_SELF_HOLD_RE.search(text or ""))


def lily_paraphrase_repeat_flag(
    text: str, previous_turns: list, threshold: float = 0.6
) -> Optional[str]:
    """PATCH-002 A4a — SEMANTIC repeat lint. lily_repeat_flag catches
    verbatim/n-gram cycling; this catches repeat-in-MEANING (the live
    reassurance storm: three semantically identical 'take your time'
    turns in 30s). Returns "paraphrase" when the turn's content-word set
    overlaps any recent agent turn above `threshold` (Jaccard on
    stopword-stripped tokens), else None. Cheap and dependency-free — a
    bag-of-content-words proxy for semantic similarity, tuned
    conservative so only genuine restatements flag. Pure; log/suppress
    decision is the caller's."""
    words = set(_repeat_norm_words(text)) - _STOPWORDS
    if len(words) < 3 or not previous_turns:
        return None
    for prev in previous_turns:
        prev_words = set(_repeat_norm_words(prev)) - _STOPWORDS
        if len(prev_words) < 3:
            continue
        union = words | prev_words
        if not union:
            continue
        jaccard = len(words & prev_words) / len(union)
        if jaccard >= threshold:
            return "paraphrase"
    return None


_STOPWORDS: frozenset = frozenset({
    "a", "an", "the", "and", "or", "but", "so", "to", "of", "in", "on",
    "for", "with", "your", "you", "i", "im", "ill", "well", "just",
    "is", "are", "it", "its", "that", "this", "we", "me", "my", "at", "as",
})


def lily_stacked_question_flag(text: str) -> int:
    """PATCH-003 P10 lint — count DISTINCT questions posed in one outbound
    turn. Asking creates an obligation to listen, and one question per
    turn is the rule (two stacked questions, neither answer awaited, was
    the live 'anything I should know — or ready to dive straight in?'
    pattern). LOG-ONLY: returns the question count so tts_node can flag
    >1; the rewrite/split stays a prompt-contract concern (Doc-owned), not
    a mechanical edit to her words. A rhetorical '?' inside one sentence
    is not double-counted — only sentence-terminal question marks count."""
    if not text:
        return 0
    # Strip audio tags, then count sentence-terminal question marks (a
    # '?' followed by whitespace/end, not mid-token).
    cleaned = re.sub(r"\[[^\]]*\]", " ", text)
    return len(re.findall(r"\?(?=\s|$|[\"'’”)])", cleaned.strip()))


def lily_yield_after_first_question(text: str) -> tuple[str, bool]:
    """End a conversational turn at its first completed question.

    Prompt instructions alone did not stop turns such as "Want a
    refresher? Or straight in?" or a question followed by more explanation.
    Once Lily asks, the room owns the floor. Game-question deliveries are
    exempted by the caller because multiple-choice options legitimately
    follow their stem.
    """
    if not text:
        return text, False
    match = re.search(r"\?(?=\s|$|[\"'’”)])", text)
    if match is None:
        return text, False
    end = match.end()
    # Preserve quote/parenthesis punctuation that closes the question.
    while end < len(text) and text[end] in "\"'’”)":
        end += 1
    clipped = text[:end].rstrip()
    return clipped, bool(text[end:].strip())


_FALSE_CLEAN_SLATE_RE = re.compile(
    r"\b(?:you|this table|we)\s+(?:have|has|haven't|hasn't|never)\b"
    r".{0,45}\b(?:no\s+)?recorded\s+game\b"
    r"|\b(?:first|clean[- ]slate)\s+(?:game|night|session)\b"
    r"|\beffectively\s+a\s+clean\s+slate\b"
    # Live 2026-08-09 Rami session: organic intake asserted absence while
    # the voice-identity probe was still outstanding.
    r"|\b(?:completely\s+)?clean\s+slate\b"
    r"|\bblank\s+slate\b"
    r"|\bno\s+saved\s+(?:voices?|games?|stats?|facts?|history|records?)\b"
    r"|\bno\s+(?:past|prior|previous|recorded)\s+games?\b"
    r"|\bno\s+(?:saved\s+)?(?:voices?|games?)\s+(?:or\s+"
    r"(?:saved\s+)?(?:voices?|games?)\s+)?on\s+file\b"
    r"|\bnothing\s+(?:is\s+)?saved\b"
    r"|\bnothing\s+on\s+file\b"
    r"|\bno\s+(?:stats|facts|record|history)\s+on\s+file\b"
    r"|\bmemory\s+bank\s+is\s+sitting\s+on\b"
    r"|\bno\s+record(?:ed)?\s+(?:of\s+you|on\s+file)\b",
    re.IGNORECASE,
)


def lily_false_clean_slate_claim(text: str) -> bool:
    """True when Lily asserts ABSENCE of memory/history as a settled fact.

    Forbidden while identity is unresolved or a returner dispute is open —
    UNKNOWN must not be spoken as EMPTY.
    """
    return bool(_FALSE_CLEAN_SLATE_RE.search(text or ""))


_STILL_CHECKING_REWRITE = (
    "If we've played, I'll get the card as we talk — I'm still checking. "
    "What should I call you?"
)


def lily_still_checking_rewrite() -> str:
    """Inventory-stable spoken sheet when a false empty-memory claim is
    caught before playout. No personality polish — one honest beat."""
    return _STILL_CHECKING_REWRITE


# ---------------------------------------------------------------------------
# HOSTLOOP-001 C6/C8 — RECEIPT SHEETS (deterministic, model-free).
#
# The measured cost of routing every acknowledgment through the LLM
# composite: 8–13s from a finished answer to a spoken verdict (Session B,
# lily-05BB92), and acks landing a whole turn late (Session A, 2026-08-12
# 04:50 — the ack for answer N airing after answer N+1 was already in).
# Adjudication itself is deterministic and instant at Tier 1; the seconds
# are generation.
#
# So the RECEIPT is a sheet, not a generation — the same treatment
# lily_still_checking_rewrite / lily_picture_pending_rewrite already get
# for the other "say exactly this, now" cases. It travels the ordinary
# dispatch funnel (gated_say -> hold/floor/live-game gates -> tts_node
# hygiene), so nothing here is a bypass; only the words are fixed.
#
# Deliberately SHORT (< 15 chars for the acks): tts_node's air-path dup
# guard already exempts short turns ("an honest 'Nice one!' may
# legitimately recur"), and lily_repeat_flag / lily_paraphrase_repeat_flag
# need 4 words / 3 content words to fire — so a receipt that recurs every
# question is not a repetition defect and is not treated as one.
#
# NEVER a verdict word on an UNCERTAIN Tier-1 (the judge has not ruled):
# an uncertain answer gets the neutral "locked in" ack, which commits to
# nothing. Receipts never carry the canonical answer, so they can never
# read as a reveal (lily_verdict_narration / _verdict_already_spoken both
# require the answer to be present) — the composite still owns the reveal.
# ---------------------------------------------------------------------------

LILY_RECEIPT_CORRECT = "Correct!"
LILY_RECEIPT_INCORRECT = "Ooh, no—"
LILY_RECEIPT_NEUTRAL = "Locked in—"


def lily_answer_receipt(verdict: str | None) -> Optional[str]:
    """The SHORT spoken receipt for one adjudicated-at-Tier-1 answer.

    "correct" / "incorrect" (a definitive MC pick of a wrong option) get
    the verdict word; "uncertain" — the escalate-to-judge verdict — gets
    the neutral ack, never a verdict word. Anything else returns None (no
    receipt is owed). Pure."""
    if verdict == "correct":
        return LILY_RECEIPT_CORRECT
    if verdict == "incorrect":
        return LILY_RECEIPT_INCORRECT
    if verdict == "uncertain":
        return LILY_RECEIPT_NEUTRAL
    return None


def lily_verdict_reair_line(
    *, correct: bool, answer: str, winner: str | None = None
) -> str:
    """ONE line that re-airs a verdict whose beat was cut by a barge-in
    (C8 / Session B 36:25, where the cut released the claim and the result
    was dropped silently).

    Unlike the receipts above this DOES carry the answer — the dropped
    thing was the result, and a re-air that omits it re-airs nothing. One
    beat only: verdict word, the answer, the name when there is one. No
    flourish, no next question."""
    answer_text = str(answer or "").strip().rstrip(".!?")
    if correct:
        if winner:
            return f"Correct, {winner} — {answer_text}."
        return f"Correct — {answer_text}."
    return f"Nobody had it — {answer_text}."


# False on-screen picture claims (WO-B4) — "look at the screen" / "picture
# is up" only when lily_control.image_shown confirmed the armed URL.
_FALSE_ON_SCREEN_RE = re.compile(
    r"(?:"
    r"\blook(?:ing)?\s+at\s+the\s+screen\b"
    r"|\beyes?\s+on\s+the\s+screen\b"
    r"|\b(?:the\s+)?picture\s+is\s+(?:up|on|live|there)\b"
    r"|\b(?:it'?s|its)\s+on\s+the\s+screen\b"
    r"|\bon\s+the\s+screen\s+now\b"
    r"|\bthere\s+it\s+is\b.{0,40}\bscreen\b"
    r"|\bimage\s+is\s+(?:up|on|live)\b"
    r")",
    re.IGNORECASE,
)

_PICTURE_PENDING_REWRITE = (
    "The picture should be coming up — tell me when you see it on the screen."
)

_PICTURE_DIDNT_LAND_REWRITE = (
    "That picture didn't land on the screen on my side — we'll keep going "
    "voice-first."
)


def lily_false_on_screen_claim(text: str) -> bool:
    """True when Lily asserts a picture is visible on the glass."""
    return bool(_FALSE_ON_SCREEN_RE.search(text or ""))


def lily_picture_pending_rewrite() -> str:
    """Sheet when she claims on-screen without image_shown confirm."""
    return _PICTURE_PENDING_REWRITE


def lily_picture_didnt_land_rewrite() -> str:
    """Sheet when the intended image never got image_shown in time."""
    return _PICTURE_DIDNT_LAND_REWRITE


# P0-5 start debris: a standalone kickoff/category fragment has no right to
# speak unless the same TTS turn structurally owns q_N_delivery.
_UNOWNED_KICKOFF_RE = re.compile(
    r"\b(?:"
    r"round(?:\s+(?:one|1))?\b"
    r"|let'?s\s+(?:do\s+it|kick(?:\s+off)?|begin|start)\b"
    r"|here\s+we\s+go\b"
    r"|time\s+for\s+round\s+(?:one|1)\b"
    r"|kick\s+off\s+round\s+(?:one|1)\b"
    r")",
    re.IGNORECASE,
)
_KICKOFF_NEGATION_RE = re.compile(
    r"\b(?:do\s+not|don'?t|won'?t|not\s+yet|hold|wait|before)\b",
    re.IGNORECASE,
)


def lily_unowned_kickoff_fragment(text: str) -> bool:
    """True for a kickoff/category teaser that does not contain the Q.

    A real delivery normally contains a question mark and is separately
    claimed by q_N_delivery. Negated/hold language is conversational, not
    a kickoff claim.
    """
    value = (text or "").strip()
    if not value or "?" in value or len(value) > 220:
        return False
    if _KICKOFF_NEGATION_RE.search(value):
        return False
    return bool(_UNOWNED_KICKOFF_RE.search(value))


def lily_mirror_flag(text: str) -> Optional[str]:
    """Return the matched mirror pattern when the turn OPENS with a
    flattery/agreement-echo reflex, else None. Only the first ~120 chars
    are considered — the ban is on openers; mid-turn enthusiasm about
    CONTENT is her warmth working as designed. Pure; log-only caller."""
    if not text:
        return None
    head = _MIRROR_LEAD_STRIP.sub("", text)[:120]
    for pattern_re in _MIRROR_RES:
        match = pattern_re.search(head)
        if match and match.start() <= 40:
            return match.re.pattern
    return None


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
#
# WO-LILY-HOTFIX-001: dup suppression is only legitimate against an act
# that actually PLAYED (CLAIM_CONFIRMED) or is genuinely in flight. The
# 08-06 wedge (Krisp NC hung RoomIO audio setup) produced a third state
# the lifecycle had no exit from: claimed, never played, never failed —
# the greet's claim froze PENDING forever and dup-suppressed its own
# retry (permanent silence, M1's exact failure mode). Claims now carry
# their claim time so the agent layer can supersede a STALE pending claim
# whose speech never reached playout; confirmed acts remain final forever.
# ---------------------------------------------------------------------------

CLAIM_PENDING = "pending"
CLAIM_CONFIRMED = "confirmed"


class SpeechActRegistry:
    """Registry of claimed speech-act keys for one session."""

    def __init__(self) -> None:
        self._acts: dict[str, str] = {}  # key -> pending | confirmed
        self._owners: dict[str, str] = {}  # pending key -> speech/reservation id
        self._claimed_at: dict[str, float] = {}  # pending key -> monotonic claim time

    def claim(self, key: str, owner: str | None = None) -> bool:
        """Atomic check-and-set at dispatch time. True = caller may speak;
        False = the act was already claimed (duplicate — suppress)."""
        if key in self._acts:
            return False
        self._acts[key] = CLAIM_PENDING
        self._claimed_at[key] = time.monotonic()
        if owner:
            self._owners[key] = owner
        return True

    def state(self, key: str):
        """CLAIM_PENDING, CLAIM_CONFIRMED, or None (unclaimed)."""
        return self._acts.get(key)

    def owner_of(self, key: str) -> Optional[str]:
        """Speech/reservation id holding a PENDING claim, else None."""
        return self._owners.get(key)

    def pending_age(self, key: str) -> Optional[float]:
        """Seconds since a PENDING claim was made; None unless pending.
        The staleness input for WO-LILY-HOTFIX-001: a pending claim whose
        speech never reached playout inside the deadline is a wedge, not
        an in-flight dispatch."""
        if self._acts.get(key) != CLAIM_PENDING:
            return None
        claimed = self._claimed_at.get(key)
        if claimed is None:
            return None
        return max(0.0, time.monotonic() - claimed)

    def confirm(self, key: str) -> None:
        """Playout completed — the act was genuinely spoken."""
        if key in self._acts:
            self._acts[key] = CLAIM_CONFIRMED
            self._owners.pop(key, None)
            self._claimed_at.pop(key, None)

    def reassign_owner(self, old_owner: str, new_owner: str) -> list[str]:
        """Move pending claims from a dispatch reservation to its concrete
        SpeechHandle id. Returns the keys moved."""
        moved = [
            key for key, owner in self._owners.items()
            if owner == old_owner and self._acts.get(key) == CLAIM_PENDING
        ]
        for key in moved:
            self._owners[key] = new_owner
        return moved

    def keys_for_owner(self, owner: str) -> list[str]:
        """PENDING claim keys held by one speech/reservation id. Read-only
        (confirm_owner/release_owner mutate; this only reports). The
        transition gate (HOTFIX-006 N12) uses it to tell a code-dispatched
        keyed act — a verdict beat, a standings flourish, the finale —
        apart from a stray conversational turn narrating the same beat a
        second time."""
        if not owner:
            return []
        return [
            key for key, claim_owner in self._owners.items()
            if claim_owner == owner and self._acts.get(key) == CLAIM_PENDING
        ]

    def confirm_owner(self, owner: str) -> list[str]:
        """Confirm only pending claims performed by one speech handle."""
        confirmed = [
            key for key, claim_owner in self._owners.items()
            if claim_owner == owner and self._acts.get(key) == CLAIM_PENDING
        ]
        for key in confirmed:
            self._acts[key] = CLAIM_CONFIRMED
            self._owners.pop(key, None)
            self._claimed_at.pop(key, None)
        return confirmed

    def release_owner(self, owner: str) -> list[str]:
        """Release only pending claims belonging to one failed speech."""
        released = [
            key for key, claim_owner in self._owners.items()
            if claim_owner == owner and self._acts.get(key) == CLAIM_PENDING
        ]
        for key in released:
            self._acts.pop(key, None)
            self._owners.pop(key, None)
            self._claimed_at.pop(key, None)
        return released

    def confirm_pending(self) -> list[str]:
        """Confirm every pending claim (agent playout completed). Returns
        the confirmed keys."""
        confirmed = [k for k, v in self._acts.items() if v == CLAIM_PENDING]
        for k in confirmed:
            self._acts[k] = CLAIM_CONFIRMED
            self._owners.pop(k, None)
            self._claimed_at.pop(k, None)
        return confirmed

    def release(self, key: str) -> bool:
        """Release ONE claim after playback failure so a retry can
        redeliver. Only pending (never-played) claims release; a confirmed
        act stays done forever. True if released."""
        if self._acts.get(key) == CLAIM_PENDING:
            del self._acts[key]
            self._owners.pop(key, None)
            self._claimed_at.pop(key, None)
            return True
        return False

    def release_pending(self) -> list[str]:
        """Release every pending claim (the swallowed-turn path: dispatch
        happened, playback produced nothing). Returns the released keys so
        the caller can relog them."""
        released = [k for k, v in self._acts.items() if v == CLAIM_PENDING]
        for k in released:
            del self._acts[k]
            self._owners.pop(k, None)
            self._claimed_at.pop(k, None)
        return released
