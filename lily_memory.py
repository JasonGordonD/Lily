"""
lily_memory.py — LILY persistent cross-session memory ("rematch" promoted to v1).

Memory keys on group_id — a stable device-scoped UUID the frontend passes as
participant token metadata ({"lily_group_id": "<uuid>"}), resolved in
lily_agent.entrypoint with the priority chain participant metadata ->
LILY_GROUP_ID env override -> room name (legacy fallback; random per session,
so nothing re-keys on it).

Deliberately dependency-light: stdlib only, so the pure logic here (metadata
parsing, summary template, memory-block builder, KB-bank mode guard) is
testable offline without livekit or the supabase client installed. The two
Supabase I/O functions take an already-constructed client, run their blocking
calls off-thread, and are fire-and-forget: they log LILY_MEMORY | markers and
never raise into the session.
"""

import asyncio
import hashlib
import json
import logging
from typing import Optional

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
) -> None:
    """Upsert one lily_memories row for this session (on_conflict=session_id,
    so finish_game and the shutdown callback can both call it safely).
    Fire-and-forget: logs LILY_MEMORY | markers, never raises."""
    if supabase is None:
        logger.info("LILY_MEMORY | WRITE_SKIPPED | session=%s no supabase client",
                    session_id)
        return
    standings = list(standings or [])
    if not standings and not question_count:
        logger.info("LILY_MEMORY | WRITE_SKIPPED | session=%s empty session",
                    session_id)
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
# The [RETURNING TABLE] block (pure)
# ---------------------------------------------------------------------------

def lily_build_memory_block(
    memory: Optional[dict],
    max_chars: int = MEMORY_BLOCK_MAX_CHARS,
) -> str:
    """Compact system-context block for a returning table: who they are, who
    won last time, running bits, total games — written so Lily greets
    returning players by name and uses callbacks naturally. Returns "" for
    empty/None memory. Capped at ~600 chars."""
    if not memory:
        return ""
    sessions = memory.get("sessions") or []
    facts = memory.get("facts") or []
    names = memory.get("player_names") or []
    if not sessions and not facts:
        return ""
    total_games = memory.get("total_games") or len(sessions)
    lines = [MEMORY_BLOCK_MARKER]
    lines.append(
        f"This table has played with you {total_games} time(s) before — "
        "rematch energy."
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
    lines.append(
        "Greet returning players back BY NAME, reference who won last time, "
        "and drop the running bits as callbacks — naturally, like a host who "
        "remembers her regulars."
    )
    block = "\n".join(lines)
    if len(block) > max_chars:
        block = block[: max_chars - 1].rstrip() + "…"
    return block


# ---------------------------------------------------------------------------
# KB-bank mode guard (pure) — consent-safety for the adult column
# ---------------------------------------------------------------------------

def lily_bank_mode_filter(rows, mode: str) -> list:
    """Filter curated-bank rows by session mode: adult=true rows surface
    ONLY when mode == 'adult'. General mode hard-excludes them — an
    adult-register question must never land at a general-mode table.
    Pure (lives here so it's testable offline; lily_persistence applies it)."""
    rows = list(rows or [])
    if (mode or "general") == "adult":
        return rows
    return [r for r in rows if not (r or {}).get("adult")]
