"""
lily_forget.py — WO-LILY-FORGETME-001: memory transparency + the right to
be forgotten.

Principle stamp: recognition data is held FOR the players, at their
pleasure — the explanation is honest and plain when pressed; deletion is
immediate, complete, verified, and costs only the recognition itself.
Known granularity limit: identity is group-keyed, per-player deletion is
v2 — "I remember tables, not individuals."

Pure stdlib module (lily_memory / lily_addressee pattern): everything here
is offline-testable with no livekit or supabase installed —
  - the tombstone id derivation (forgotten_<sha1-12>),
  - the per-table delete/re-key cascade PLAN builder (lily_persistence
    executes it against the live client),
  - the deterministic yes/no confirmation parser for the pending-confirm
    state (same pattern as the clarify-reply parser),
  - the tool-result / instructed-reply message shapes (the tool result
    tells Lily what to say — including honest partial-failure reporting),
  - the lily_explain_memory result shapes (counts only, never raw
    contents; cold groups get the honest "nothing yet" shape),
  - the lobby-disclosure frequency cap (first rematch, then every 5th).

The spoken "forget me" command DETECTION lives in lily_scorekeeper's
command layer (lily_detect_control_command -> "forget_me"), beside
"back to normal" — deterministic, paraphrase-tolerant, fragment-proof.
"""

import hashlib
import re as _re
from typing import Optional

# ---------------------------------------------------------------------------
# Tombstone — operational records survive without linkable identity
# ---------------------------------------------------------------------------

FORGET_TOMBSTONE_PREFIX = "forgotten_"


def lily_tombstone_group_id(group_id: str) -> str:
    """Tombstone id for re-keyed operational rows: `forgotten_<sha1-12>`
    (first 12 hex chars of sha1 of the old group_id). Deterministic so a
    retried cascade re-keys to the SAME tombstone; one-way so the old
    identity cannot be recovered from it."""
    digest = hashlib.sha1(str(group_id or "").encode("utf-8")).hexdigest()
    return FORGET_TOMBSTONE_PREFIX + digest[:12]


# ---------------------------------------------------------------------------
# The delete cascade — per-table plan (pure; lily_persistence executes)
#
# Schemas verified against migrations/ (2026-07-15):
#   001: lily_speaker_voiceprints(group_id), lily_group_facts(group_id),
#        lily_sessions(session_id PK, group_id), lily_answers and
#        lily_transcripts (session_id only — NO group_id column)
#   003: lily_memories(group_id)
#   005: lily_addressee_log(session_id only — NO group_id column)
#   006: lily_session_reports(session_id)
#   008: lily_acoustic_trajectories(session_id only — NO group_id column)
#   012: lily_image_attempts(session_id)
#   013: lily_group_prefs(group_id PK) — a forgotten table's preferences
#        are recognition data (group prefs WO interlock).
#   lily_asked_history: lands with a future WO — optional, absent-table
#        errors (42P01 / PGRST205) are tolerated and logged as skipped.
# ---------------------------------------------------------------------------

# HARD-DELETE, keyed directly by group_id.
HARD_DELETE_GROUP_TABLES = (
    "lily_speaker_voiceprints",
    "lily_memories",
    "lily_group_facts",
)

# HARD-DELETE, keyed by session_id (no group_id column) — deleted via the
# group's session ids (lily_sessions where group_id = X, plus the current
# session id).
SESSION_KEYED_DELETE_TABLES = (
    "lily_transcripts",
    "lily_answers",
    "lily_addressee_log",
    "lily_acoustic_trajectories",
    "lily_session_reports",
    "lily_image_attempts",
)

# HARD-DELETE if present — a missing table is skipped gracefully (via the
# absent-table matcher below), never a failure. Any OTHER error on these
# tables still fails honestly.
#   lily_asked_history: ships with a future WO.
#   lily_group_prefs (group prefs WO interlock — a forgotten table's
#       preferences are recognition data): migration 013 ships alongside
#       the feature; before production applies it there were never any
#       prefs stored, so an absent table is an honest skip, not a partial
#       failure the table would be told to retry forever.
OPTIONAL_GROUP_TABLES = (
    "lily_asked_history",
    "lily_group_prefs",
)

# RETAINED but re-keyed to the tombstone: operational records survive
# without linkable identity.
REKEY_TABLE = "lily_sessions"

def lily_build_forget_plan(group_id: str, session_ids) -> list:
    """Ordered per-table operation plan for the forget cascade. Each op:
    {table, action: delete|rekey, column, values, optional[, new_group_id]}.
    session_ids should already include the CURRENT session id; they are
    deduped and sorted for determinism. Deletes run before the re-key so a
    timeout can never leave identity-bearing rows behind a moved key."""
    sessions = sorted({str(s) for s in (session_ids or []) if s})
    plan = []
    for table in HARD_DELETE_GROUP_TABLES:
        plan.append({
            "table": table, "action": "delete",
            "column": "group_id", "values": [group_id], "optional": False,
        })
    for table in SESSION_KEYED_DELETE_TABLES:
        plan.append({
            "table": table, "action": "delete",
            "column": "session_id", "values": list(sessions), "optional": False,
        })
    for table in OPTIONAL_GROUP_TABLES:
        plan.append({
            "table": table, "action": "delete",
            "column": "group_id", "values": [group_id], "optional": True,
        })
    plan.append({
        "table": REKEY_TABLE, "action": "rekey",
        "column": "group_id", "values": [group_id], "optional": False,
        "new_group_id": lily_tombstone_group_id(group_id),
    })
    return plan


_ABSENT_TABLE_MARKERS = (
    "42P01",                    # postgres undefined_table
    "PGRST205",                 # PostgREST: table not in schema cache
    "does not exist",
    "Could not find the table",
)


def lily_is_absent_table_error(message: str) -> bool:
    """True when an error message means the table hasn't been created yet
    (lily_asked_history lands with a future WO) — skipped, not failed."""
    msg = str(message or "")
    return any(marker in msg for marker in _ABSENT_TABLE_MARKERS)


# ---------------------------------------------------------------------------
# Deterministic yes/no confirmation parsing (pending-confirm state)
# ---------------------------------------------------------------------------

_PUNCT_RE = _re.compile(r"[^a-z0-9\s]+")


def _normalize(text: str) -> str:
    stripped = _re.sub(r"^\s*\[S\d+\]\s*", "", text or "").strip()
    lowered = _PUNCT_RE.sub(" ", stripped.lower())
    return _re.sub(r"\s+", " ", lowered).strip()


_CONFIRM_YES_PATTERNS = (
    r"\byes\b",
    r"\byeah\b",
    r"\byep\b",
    r"\byup\b",
    r"\bsure\b",
    r"\babsolutely\b",
    r"\bdefinitely\b",
    r"\bplease do\b",
    r"\bdo it\b",
    r"\bgo ahead\b",
    r"\bconfirm(?:ed)?\b",
    r"\bdelete it(?: all)?\b",
    r"\bwipe it\b",
    r"\berase it\b",
    r"\bi m sure\b",
    r"\bwe re sure\b",
)

_CONFIRM_NO_PATTERNS = (
    r"\bno\b",
    r"\bnope\b",
    r"\bnah\b",
    r"\bnever ?mind\b",
    r"\bcancel\b",
    r"\bstop\b",
    r"\bwait\b",
    r"\bhold on\b",
    r"\bdon t\b",
    r"\bdo not\b",
    r"\bkeep (?:it|them|everything)\b",
    r"\bleave it\b",
    r"\bchanged (?:my|our) mind\b",
    r"\bjust kidding\b",
    r"\bjoking\b",
)

_CONFIRM_YES_RE = _re.compile("|".join(_CONFIRM_YES_PATTERNS))
_CONFIRM_NO_RE = _re.compile("|".join(_CONFIRM_NO_PATTERNS))


def lily_parse_forget_confirmation(text: str) -> Optional[str]:
    """Parse a reply to the forget confirmation question ("everything —
    voices, games, facts — gone for good; tonight's game keeps going —
    yes?") into "yes", "no", or None (unparseable / fires both directions
    — stays pending, the prompt keeps the conversation going).

    Deterministic, keyword/regex only — same pattern as the clarify-reply
    parser in lily_addressee. A "yes" triggers the cascade; a "no" drops
    it for the night; anything ambiguous does NOTHING destructive."""
    normalized = _normalize(text)
    if not normalized:
        return None
    yes = _CONFIRM_YES_RE.search(normalized) is not None
    no = _CONFIRM_NO_RE.search(normalized) is not None
    if yes and not no:
        return "yes"
    if no and not yes:
        return "no"
    return None


# ---------------------------------------------------------------------------
# Tool-result / instructed-reply shapes — the result tells Lily what to say
# ---------------------------------------------------------------------------

def _rows_summary(result: dict) -> str:
    bits = []
    for table, n in (result.get("deleted") or {}).items():
        bits.append(f"{table}: {n} deleted")
    for table, n in (result.get("rekeyed") or {}).items():
        bits.append(f"{table}: {n} re-keyed to tombstone")
    for table in result.get("skipped") or []:
        bits.append(f"{table}: skipped (not present)")
    return "; ".join(bits) if bits else "nothing was stored"


def lily_forget_result_message(result: dict) -> str:
    """One string used as BOTH the lily_forget_group tool result and the
    deterministic post-confirmation instructed reply — it reports what
    happened and tells Lily exactly what to say (one warm line, zero
    mourning; honest naming of tables on partial failure)."""
    result = result or {}
    if result.get("already_done"):
        return (
            "Already forgotten this session — everything was deleted "
            "earlier and the night is running on a clean anonymous slate. "
            "Nothing left to delete. Say exactly that in one plain warm "
            "line and keep the game moving."
        )
    if result.get("in_progress"):
        return (
            "Deletion is already running — it was triggered a moment ago. "
            "Hold one beat; confirm when the completion line lands. Do not "
            "trigger it again."
        )
    if result.get("ok"):
        return (
            "Deleted and verified — this table's entire file is gone for "
            "good (" + _rows_summary(result) + "). Tonight's game keeps "
            "going under a fresh anonymous slate; nothing links back to "
            "who they were. Say ONE warm line confirming it's all gone — "
            "zero mourning, no ceremony — and get straight back into the "
            "game."
        )
    succeeded = sorted(
        list((result.get("deleted") or {}).keys())
        + list((result.get("rekeyed") or {}).keys())
    )
    failed = result.get("failed") or {}
    failed_bits = "; ".join(f"{t}: {reason}" for t, reason in sorted(failed.items()))
    return (
        "PARTIAL deletion — be honest with the table. "
        + ("Cleared: " + ", ".join(succeeded) + ". " if succeeded else "")
        + "NOT confirmed deleted: " + (failed_bits or "unknown") + ". "
        "Tell them plainly, in one calm line, that most of it is gone but "
        "part of the deletion did not go through just now, and that saying "
        "'Lily, forget me' again will finish the job. Never pretend it all "
        "worked; never name systems or table names out loud — 'part of it' "
        "is enough detail for the room."
    )


# ---------------------------------------------------------------------------
# lily_explain_memory result shapes — counts only, never raw contents
# ---------------------------------------------------------------------------

# Plain-language recognition sources, derived from resolve_group_identity's
# logged chain (LILY_MEMORY | GROUP_ID | source=...). Never names vendors,
# tables, or mechanisms beyond device-and-voice.
RECOGNITION_SOURCE_PLAIN = {
    "participant_metadata": "their device introduced the table when the room opened",
    "dispatch_metadata": "their device introduced the table when the room opened",
    "participant_metadata_late": "their device introduced the table when the room opened",
    "env_override": "an operator setting pinned this table's identity",
    "voiceprint_match": "their voices — a returning voice matched one you remember",
    "name_set_hash": "the set of player names matched a table you've hosted before",
    "room_name": "no recognition — fresh room, nothing matched",
    "post_forget_anonymous": (
        "no recognition — this table's memory was deleted this session; "
        "you are running fresh and anonymous"
    ),
}

_FALLBACK_SOURCE = "no recognition — fresh room, nothing matched"


def lily_explain_memory_result(counts: Optional[dict], group_id_source: str) -> str:
    """The lily_explain_memory tool result. counts is
    {voiceprints, games, last_played_at, facts} from
    lily_persistence.lily_count_group_memory, or None when the read failed.
    Counts only — never raw contents. Cold group -> the honest "nothing
    yet" shape. No new storage anywhere in this path."""
    how = RECOGNITION_SOURCE_PLAIN.get(group_id_source, _FALLBACK_SOURCE)
    if counts is None:
        return (
            "Could not read this table's file right now — the memory "
            "ledger is not answering. Recognition this session: " + how + ". "
            "Be honest: say you can't check the file at the moment, never "
            "guess counts, and keep the standing offer alive — 'Lily, "
            "forget me' deletes everything whenever they want."
        )
    voices = int(counts.get("voiceprints") or 0)
    games = int(counts.get("games") or 0)
    facts = int(counts.get("facts") or 0)
    last_played = counts.get("last_played_at")
    if not voices and not games and not facts:
        return (
            "Nothing on file for this table yet — zero saved voices, zero "
            "past games, zero facts. Recognition this session: " + how + ". "
            "Tell them plainly tonight is a clean slate: there is nothing "
            "to forget yet, and memory only starts once a game is played."
        )
    last_bit = ""
    if last_played:
        last_bit = f", last played {str(last_played)[:10]}"
    return (
        f"This table's file, counts only: {voices} voice(s) remembered, "
        f"{games} game(s) on record{last_bit}, {facts} fact(s) kept. "
        "Recognition this session: " + how + ". Speak the counts "
        "naturally — never read raw contents aloud, never name systems — "
        "and keep the standing offer in the same breath: say 'Lily, "
        "forget me' and it's all gone."
    )


# ---------------------------------------------------------------------------
# Lobby disclosure frequency cap (Task 4)
# ---------------------------------------------------------------------------

def lily_should_disclose_memory(total_games) -> bool:
    """RETURNING-group greet disclosure cap: first rematch (1 stored
    game), then every 5th stored game (5, 10, 15, ...). The persistent
    counter is the lily_memories row count itself (total_games from
    lily_load_group_memory) — exactly one row lands per played session,
    it survives sessions by construction, and it costs zero new columns:
    the cheapest store available, so no migration is needed. Cold groups
    (0 games) never disclose — there is nothing remembered to disclose."""
    games = int(total_games or 0)
    if games <= 0:
        return False
    return games == 1 or games % 5 == 0
