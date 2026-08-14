"""
lily_memory.py — LILY persistent cross-session memory ("rematch" promoted to v1).

Memory keys on group_id — a stable device-scoped UUID the frontend passes as
participant token metadata ({"lily_group_id": "<uuid>"}), resolved in
lily_agent.entrypoint with the priority chain participant metadata ->
LILY_GROUP_ID env override -> room name (legacy fallback; random per session,
so nothing re-keys on it).

Deliberately dependency-light: stdlib plus lily_config (itself stdlib-only —
ambient discipline keeps every env read there), so the pure logic here
(metadata parsing, summary template, memory-block builder, KB-bank mode
guard) is testable offline without livekit or the supabase client installed.
The two Supabase I/O functions take an already-constructed client, run their
blocking calls off-thread, and are fire-and-forget: they log LILY_MEMORY |
markers and never raise into the session.
"""

import asyncio
import hashlib
import json
import logging
from typing import Optional

import lily_config

logger = logging.getLogger("lily_memory")

MEMORY_BLOCK_MARKER = "[RETURNING TABLE]"
MEMORY_BLOCK_MAX_CHARS = 600
MEMORY_SESSIONS_LIMIT = 3   # last N games loaded for the block
MEMORY_FACTS_IN_BLOCK = 4   # running bits surfaced per session
MEMORY_HIGHLIGHTS_CAP = 20  # callouts kept per session memory row


# ---------------------------------------------------------------------------
# Group identity — participant token metadata parsing (pure)
# ---------------------------------------------------------------------------

def lily_parse_group_id_from_metadata(metadata) -> Optional[str]:
    """Extract lily_group_id from a participant's token metadata string
    (JSON {"lily_group_id": "<uuid>"}). Tolerates absent/None/unparseable
    metadata and non-dict payloads — returns None rather than raising."""
    if not metadata or not isinstance(metadata, str):
        return None
    try:
        data = json.loads(metadata)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    group_id = data.get("lily_group_id")
    if isinstance(group_id, str) and group_id.strip():
        return group_id.strip()
    return None


# ---------------------------------------------------------------------------
# Group identity — name-set hash fallback + voiceprint matching (pure)
# ---------------------------------------------------------------------------

NAME_SET_GROUP_PREFIX = "grp_"


def lily_normalize_player_names(names) -> list:
    """Normalized sorted player-name set: lowercase, stripped, deduped,
    sorted. The canonical roster spelling for both the name-set group hash
    and the lily_memories.player_names audit column."""
    seen = set()
    for n in names or []:
        s = str(n or "").strip().lower()
        if s:
            seen.add(s)
    return sorted(seen)


def lily_name_set_group_id(names) -> Optional[str]:
    """Fallback group id (resolution step c): sha1 of the normalized sorted
    player-name set joined with '|' (e.g. "carly|kali|rami"), prefixed
    "grp_". Deterministic — the same table of names re-keys to the same
    group across sessions. Returns None when no usable names exist."""
    norm = lily_normalize_player_names(names)
    if not norm:
        return None
    digest = hashlib.sha1("|".join(norm).encode("utf-8")).hexdigest()
    return NAME_SET_GROUP_PREFIX + digest


def _identifier_strings(value) -> set:
    """Flatten any voiceprint-identifier shape (str, list, nested list,
    dict values, SpeakerIdentifier-like objects) into a set of identifier
    strings."""
    out: set = set()
    if value is None:
        return out
    if isinstance(value, str):
        if value.strip():
            out.add(value.strip())
        return out
    if isinstance(value, dict):
        for v in value.values():
            out |= _identifier_strings(v)
        return out
    if isinstance(value, (list, tuple, set)):
        for v in value:
            out |= _identifier_strings(v)
        return out
    ids = getattr(value, "speaker_identifiers", None)
    if ids is not None:
        out |= _identifier_strings(ids)
    return out


def lily_candidate_labels_confirmed(current_identifiers, candidate_rows) -> bool:
    """Voice confirmation via the LABEL ROUND-TRIP (2026-07-16 fix): we
    inject a candidate group's stored identifiers as known_speakers under
    PLAYER-NAME labels; Speechmatics assigns one of those labels to a live
    stream ONLY when its own biometric match recognizes the voice. So a
    current speaker id carrying an injected label IS the vendor's
    recognition decision.

    This exists because exact identifier-string overlap can never match
    across sessions: production data shows Speechmatics REFRESHES the
    identifier blobs every session for the same voice (7 same-voice rows,
    7 distinct strings, one shared prefix family). Transient diarization
    labels (S0, S1...) never count."""
    import re as _re
    if not candidate_rows:
        return False
    wanted = {
        str(lbl).strip().lower()
        for lbl in (
            (row or {}).get("speaker_label") or (row or {}).get("player_name")
            for row in candidate_rows
        )
        if lbl and str(lbl).strip()
    }
    if not wanted:
        return False
    transient = _re.compile(r"^s\d+$", _re.IGNORECASE)
    for entry in _flatten_identifier_entries(current_identifiers):
        label = getattr(entry, "label", None) or (
            entry.get("label") if isinstance(entry, dict) else None
        )
        if not label:
            continue
        label = str(label).strip()
        if transient.match(label):
            continue
        if label.lower() in wanted:
            return True
    return False


def _flatten_identifier_entries(value) -> list:
    """Flatten get_speaker_ids() shapes (entry, list, nested list) into a
    flat list of entries (SpeakerIdentifier-like objects or dicts)."""
    out: list = []
    if value is None:
        return out
    if isinstance(value, (list, tuple, set)):
        for v in value:
            out.extend(_flatten_identifier_entries(v))
        return out
    out.append(value)
    return out


def lily_match_group_by_voiceprints(current_identifiers, stored_rows) -> Optional[str]:
    """Resolution step (b): match this session's Speechmatics speaker
    identifiers against stored lily_speaker_voiceprints rows
    ([{group_id, speaker_identifiers}]). Returns the group_id with the most
    exact identifier-string overlap (ties broken lexicographically for
    determinism), or None when nothing overlaps."""
    current = _identifier_strings(current_identifiers)
    if not current:
        return None
    counts: dict = {}
    for row in stored_rows or []:
        gid = (row or {}).get("group_id")
        if not gid:
            continue
        overlap = len(current & _identifier_strings(row.get("speaker_identifiers")))
        if overlap:
            counts[gid] = counts.get(gid, 0) + overlap
    if not counts:
        return None
    best = max(counts.values())
    return sorted(g for g, c in counts.items() if c == best)[0]


# ---------------------------------------------------------------------------
# Session summary — deterministic template, no LLM call
# ---------------------------------------------------------------------------

def lily_session_winner(standings) -> Optional[str]:
    """Sole top scorer with a positive score, else None (tie or no scores)."""
    if not standings:
        return None
    top = max(int(p.get("score") or 0) for p in standings)
    if top <= 0:
        return None
    leaders = [p.get("name") for p in standings if int(p.get("score") or 0) == top]
    return leaders[0] if len(leaders) == 1 and leaders[0] else None


def lily_build_session_summary(standings, winner, question_count) -> str:
    """Deterministic one-line session summary (template, never an LLM)."""
    qc = int(question_count or 0)
    if not standings:
        return f"Session ended with no players bound after {qc} question(s)."
    score_line = ", ".join(
        f"{p.get('name')} {int(p.get('score') or 0)}" for p in standings
    )
    if winner:
        top = max(int(p.get("score") or 0) for p in standings)
        return (
            f"{winner} won with {top} point(s) over {qc} question(s). "
            f"Final scores: {score_line}."
        )
    return (
        f"No sole winner over {qc} question(s) (tie or no points). "
        f"Final scores: {score_line}."
    )


# ---------------------------------------------------------------------------
# Supabase I/O — fire-and-forget, idempotent via session_id upsert
# ---------------------------------------------------------------------------

async def lily_write_session_memory(
    supabase,
    group_id: str,
    session_id: str,
    standings,
    question_count: int,
    highlights=None,
    round_reached: int = 0,
) -> None:
    """Upsert one lily_memories row for this session (on_conflict=session_id,
    so finish_game and the shutdown callback can both call it safely).
    Fire-and-forget: logs LILY_MEMORY | markers, never raises.

    Write threshold (WO-LILY-DESYNC-HONESTY-001 F): the narrative row is
    written only when the session played >= LILY_MEMORY_MIN_QUESTIONS
    (default 3 — the same count the summary reports) OR reached round 2.
    Below threshold nothing writes here — the lily_sessions row still
    lands via its own path; an aborted one-question session must never
    become 'last game' material ('No sole winner over 1 question(s).')."""
    if supabase is None:
        logger.info("LILY_MEMORY | WRITE_SKIPPED | session=%s no supabase client",
                    session_id)
        return
    standings = list(standings or [])
    if not standings and not question_count:
        logger.info("LILY_MEMORY | WRITE_SKIPPED | session=%s empty session",
                    session_id)
        return
    min_questions = lily_config.memory_min_questions()
    if int(question_count or 0) < min_questions and int(round_reached or 0) < 2:
        logger.info(
            "LILY_MEMORY | WRITE_SKIPPED | session=%s below threshold "
            "(questions=%d < %d, round=%d) — session row only, no narrative",
            session_id, int(question_count or 0), min_questions,
            int(round_reached or 0),
        )
        return
    winner = lily_session_winner(standings)
    players = [
        {
            "name": p.get("name"),
            "score": int(p.get("score") or 0),
            "streak": int(p.get("streak") or 0),
        }
        for p in standings
    ]
    payload = {
        "group_id": group_id,
        "session_id": session_id,
        "players": players,
        "winner": winner,
        "question_count": int(question_count or 0),
        "highlights": list(highlights or [])[-MEMORY_HIGHLIGHTS_CAP:],
        "summary": lily_build_session_summary(standings, winner, question_count),
        # Name-set audit column (migration 007): the normalized sorted
        # roster this memory was written under.
        "player_names": lily_normalize_player_names(
            p.get("name") for p in standings
        ),
    }

    def _upsert(p):
        return (
            supabase.table("lily_memories")
            .upsert(p, on_conflict="session_id")
            .execute()
        )

    try:
        try:
            await asyncio.to_thread(_upsert, payload)
        except Exception as e:
            # Migration-lag tolerance: if production hasn't applied 007 yet
            # (player_names column missing), the memory row must still land.
            if "player_names" in str(e):
                logger.warning(
                    "LILY_MEMORY | WRITE_RETRY_WITHOUT_PLAYER_NAMES | "
                    "session=%s (apply migrations/007): %s", session_id, e,
                )
                payload = {k: v for k, v in payload.items() if k != "player_names"}
                await asyncio.to_thread(_upsert, payload)
            else:
                raise
        logger.info(
            "LILY_MEMORY | WRITE | session=%s group=%s winner=%s players=%d q=%d",
            session_id, group_id, winner, len(players), int(question_count or 0),
        )
    except Exception as e:
        logger.error(
            "LILY_MEMORY | WRITE_FAILED | session=%s group=%s error=%s",
            session_id, group_id, e,
        )


async def lily_load_group_memory(supabase, group_id: str) -> Optional[dict]:
    """Load the returning-table memory for a group: last 3 lily_memories rows
    plus lily_group_facts. Returns
    {sessions, facts, player_names, last_winner, total_games} or None when
    the group has no history (or on any error — memory is never load-bearing)."""
    if supabase is None or not group_id:
        return None
    try:
        mem_result = await asyncio.to_thread(
            lambda: supabase.table("lily_memories")
            .select("*", count="exact")
            .eq("group_id", group_id)
            .order("played_at", desc=True)
            .limit(MEMORY_SESSIONS_LIMIT)
            .execute()
        )
        facts_result = await asyncio.to_thread(
            lambda: supabase.table("lily_group_facts")
            .select("player_name, fact")
            .eq("group_id", group_id)
            .order("created_at", desc=True)
            .execute()
        )
        sessions = mem_result.data or []
        facts = facts_result.data or []
        if not sessions and not facts:
            return None
        total_games = getattr(mem_result, "count", None) or len(sessions)
        player_names: list[str] = []
        for sess in sessions:  # newest first — recent names lead
            for p in sess.get("players") or []:
                name = (p or {}).get("name")
                if name and name not in player_names:
                    player_names.append(name)
        memory = {
            "sessions": sessions,
            "facts": facts,
            "player_names": player_names,
            "last_winner": sessions[0].get("winner") if sessions else None,
            "total_games": total_games,
        }
        logger.info(
            "LILY_MEMORY | LOADED | group=%s sessions=%d facts=%d total_games=%d",
            group_id, len(sessions), len(facts), total_games,
        )
        return memory
    except Exception as e:
        logger.error("LILY_MEMORY | LOAD_FAILED | group=%s error=%s", group_id, e)
        return None


# ---------------------------------------------------------------------------
# Group preferences summary (pure) — group prefs WO
# ---------------------------------------------------------------------------

def lily_prefs_summary(prefs) -> str:
    """One compact human line for a group's stored preferences ("relaxed
    pacing"). OPAQUE: renders whatever scalar keys the dict carries — the
    keys this WO owns get natural phrasing; unknown keys (round_format /
    media_mode land with their own features post-merge) render generically
    as "key: value", so their stored choices surface without changes here.
    Returns "" for empty/None prefs."""
    if not isinstance(prefs, dict):
        return ""
    bits = []
    for key in sorted(prefs):
        value = prefs[key]
        if value is None or value == "" or not isinstance(value, (str, int, float, bool)):
            continue
        if key == "pacing":
            bits.append(f"{value} pacing")
        else:
            bits.append(f"{key}: {value}")
    return ", ".join(bits)


# ---------------------------------------------------------------------------
# The [RETURNING TABLE] block (pure)
# ---------------------------------------------------------------------------

def lily_build_memory_block(
    memory: Optional[dict],
    prefs: Optional[dict] = None,
    max_chars: int = MEMORY_BLOCK_MAX_CHARS,
) -> str:
    """Compact system-context block for a returning table: who they are, who
    won last time, running bits, total games — written so Lily greets
    returning players by name and uses callbacks naturally. `prefs` (the
    stored lily_group_prefs dict) adds one compact "usual" line ("usual:
    relaxed pacing") for the ask-once preferences flow. Returns "" for
    empty/None memory. Capped at ~600 chars."""
    if not memory:
        return ""
    sessions = memory.get("sessions") or []
    facts = memory.get("facts") or []
    names = memory.get("player_names") or []
    usual = lily_prefs_summary(prefs)
    if not sessions and not facts and not names and not usual:
        return ""
    total_games = memory.get("total_games") or len(sessions)
    lines = [MEMORY_BLOCK_MARKER]
    if sessions or facts:
        lines.append(
            f"This table has played with you {total_games} time(s) before — "
            "rematch energy."
        )
    else:
        # Voiceprint-only recognition can predate a qualifying game-memory
        # row (short lobby/tune-up sessions). Preserve the known names without
        # inventing a prior score, winner, or game count.
        lines.append(
            "Voice recognition matched people you have met before; no prior "
            "game result is on the table card."
        )
    if names:
        lines.append("Returning players: " + ", ".join(names[:6]) + ".")
    if sessions:
        last = sessions[0]
        winner = memory.get("last_winner")
        qc = last.get("question_count")
        if winner:
            lines.append(
                f"Last game: {winner} won"
                + (f" ({qc} questions)." if qc else ".")
            )
        elif last.get("summary"):
            lines.append(f"Last game: {last['summary']}")
    fact_bits = []
    for f in facts[:MEMORY_FACTS_IN_BLOCK]:
        fact = str((f or {}).get("fact") or "").strip()
        if not fact:
            continue
        who = (f or {}).get("player_name")
        fact_bits.append(f"{who}: {fact}" if who else fact)
    if fact_bits:
        lines.append("Running bits you remember: " + "; ".join(fact_bits) + ".")
    if usual:
        lines.append(f"usual: {usual}.")
    if sessions or facts:
        lines.append(
            "Greet returning players back BY NAME, reference who won last "
            "time, and drop the running bits as callbacks — naturally, like "
            "a host who remembers her regulars."
        )
    else:
        lines.append(
            "Recognize these returning players BY NAME, but do not invent a "
            "past game, winner, score, fact, or preference."
        )
    block = "\n".join(lines)
    if len(block) > max_chars:
        block = block[: max_chars - 1].rstrip() + "…"
    return block


# ---------------------------------------------------------------------------
# KB-bank mode guard (pure) — consent-safety for the adult column
# ---------------------------------------------------------------------------

def lily_bank_mode_filter(rows) -> list:
    """Curated-bank rows pass through unfiltered — the unified adult deck
    surfaces every row (adult=true included).
    Pure (lives here so it's testable offline; lily_persistence applies it)."""
    return list(rows or [])


# ---------------------------------------------------------------------------
# Known-name STT correction (live 2026-07-15 22:54, the "Romney" class)
# ---------------------------------------------------------------------------

def _name_consonant_skeleton(name: str) -> str:
    """Lowercased consonant skeleton ('romney' -> 'rmny', 'rami' -> 'rm').
    Deliberately crude — it exists only to catch STT garbling a name the
    table's memory already knows."""
    lowered = "".join(ch for ch in (name or "").lower() if ch.isalpha())
    return "".join(ch for ch in lowered if ch not in "aeiou")


def lily_known_name_correction(heard: str, known_names) -> "Optional[str]":
    """If an STT-heard player name is almost certainly a garbled spelling
    of a name this group's MEMORY already knows, return the remembered
    spelling; else None.

    The live failure class: a returning player's spoken name arrives
    mangled ("Romney" for "Rami") and would bind as a stranger — breaking
    recognition, memory continuity, and the scoreboard label — while the
    correct spelling sits in lily_memories.player_names the whole time.

    Deterministic and conservative, exactly-one-candidate rule:
      - the heard name IS a known name (case-insensitive) -> None
        (nothing to correct; also protects distinct real players)
      - candidate = known name sharing the first letter whose consonant
        skeleton prefixes the heard name's skeleton (or vice versa), or
        with difflib ratio >= 0.75
      - exactly ONE candidate matches -> return it; zero or several -> None
        (never guess between two plausible people)
    """
    import difflib

    heard_clean = "".join(ch for ch in (heard or "").lower() if ch.isalpha())
    if len(heard_clean) < 2:
        return None
    known = [str(n) for n in (known_names or []) if n and str(n).strip()]
    if any(heard_clean == k.lower() for k in known):
        return None
    heard_skel = _name_consonant_skeleton(heard)
    candidates = []
    for k in known:
        k_clean = k.lower()
        if not k_clean or k_clean[0] != heard_clean[0]:
            continue
        k_skel = _name_consonant_skeleton(k)
        skeleton_hit = bool(
            heard_skel and k_skel
            and (heard_skel.startswith(k_skel) or k_skel.startswith(heard_skel))
        )
        ratio_hit = difflib.SequenceMatcher(
            None, heard_clean, k_clean
        ).ratio() >= 0.75
        if skeleton_hit or ratio_hit:
            candidates.append(k)
    if len(candidates) == 1:
        return candidates[0]
    return None
