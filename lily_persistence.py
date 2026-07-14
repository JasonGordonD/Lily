"""
lily_persistence.py — LILY Supabase checkpointing & session plumbing.

Native lift of the Lovebirds persistence patterns (lbs_persistence.py +
lbs_agent.py session-init hardening):
  - session_id = room name (never random UUIDs)
  - fail-fast RuntimeError out of the entrypoint on a null client or an
    early-row insert failure, with named exception handlers and
    structured LILY_INIT | log markers
  - checkpoint on every score change, every 60s (heartbeat), and on key
    events; shutdown gate with 30s timeout lives in lily_agent.py
  - transcript batching (flush at 10 rows or 30s, lbs pattern)
  - lily_answers audit writes
  - passive voiceprint enrollment (stt.get_speaker_ids() ->
    lily_speaker_voiceprints) and lily_load_voiceprints -> known_speakers
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Optional

import httpx
from postgrest.exceptions import APIError as PostgrestAPIError
from supabase import Client as SupabaseClient, create_client as create_supabase_client

import lily_config
import lily_memory

logger = logging.getLogger("lily_persistence")

TRANSCRIPT_BATCH_SIZE = 10
TRANSCRIPT_BATCH_FLUSH_SECONDS = 30.0


def lily_create_supabase_client() -> Optional[SupabaseClient]:
    url = lily_config.supabase_url()
    key = lily_config.supabase_service_role_key()
    if not url or not key:
        logger.error("LILY_INIT | SUPABASE_ENV_MISSING | url_set=%s key_set=%s",
                     bool(url), bool(key))
        return None
    try:
        return create_supabase_client(url, key)
    except Exception as e:
        logger.error("LILY_INIT | SUPABASE_CLIENT_CREATE_FAILED | error_class=%s error=%s",
                     type(e).__name__, e)
        return None


# ---------------------------------------------------------------------------
# Session init — fail-fast hardening (Lovebirds pattern)
# ---------------------------------------------------------------------------

def lily_init_session(
    supabase: Optional[SupabaseClient],
    session_id: str,
    group_id: Optional[str] = None,
) -> None:
    """Insert the early session row. Raises RuntimeError on any failure so
    the entrypoint rejects unpersistable rooms fail-fast."""
    if supabase is None:
        logger.error(
            "LILY_INIT | UNPERSISTABLE_ROOM_REJECTED | room_id=%s reason=supabase_client_none",
            session_id,
        )
        raise RuntimeError(
            f"LILY_INIT unpersistable room (supabase_client_none) room_id={session_id}"
        )
    try:
        supabase.table("lily_sessions").upsert(
            {
                "session_id": session_id,
                "group_id": group_id or session_id,
                "phase": "lobby",
                "mode": "general",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            on_conflict="session_id",
        ).execute()
        logger.info("LILY_INIT | EARLY_SESSION_ROW | inserted session_id=%s", session_id)
    except (PostgrestAPIError, httpx.HTTPError, asyncio.TimeoutError) as e:
        logger.error(
            "LILY_INIT | EARLY_SESSION_ROW_INSERT_FAILED | room_id=%s error_class=%s error=%s",
            session_id, type(e).__name__, e,
        )
        raise RuntimeError(
            f"LILY_INIT early_session_row insert failed room_id={session_id} "
            f"error_class={type(e).__name__}"
        ) from e
    except Exception as e:
        # Catch supabase-client raised exceptions that don't subclass the above
        logger.error(
            "LILY_INIT | EARLY_SESSION_ROW_INSERT_FAILED | room_id=%s error_class=%s error=%s",
            session_id, type(e).__name__, e,
        )
        raise RuntimeError(
            f"LILY_INIT early_session_row insert failed room_id={session_id} "
            f"error_class={type(e).__name__}"
        ) from e


def lily_check_existing_session(
    supabase: SupabaseClient,
    session_id: str,
) -> Optional[dict]:
    """Return the active session row for session_id, or None. Used on room
    join to detect reconnections (rehydrate scores + round position)."""
    try:
        result = (
            supabase.table("lily_sessions")
            .select("*")
            .eq("session_id", session_id)
            .neq("phase", "ended")
            .maybe_single()
            .execute()
        )
        return result.data if result else None
    except Exception as e:
        logger.error("lily_check_existing_session error: %s", e)
        return None


# ---------------------------------------------------------------------------
# Checkpointing
# ---------------------------------------------------------------------------

async def lily_checkpoint(
    supabase: SupabaseClient,
    scorekeeper,
    **extra_fields,
) -> None:
    """Upsert current session state to lily_sessions. Called on every score
    change, on the 60s heartbeat, and on key events."""
    try:
        snap = scorekeeper.snapshot()
        payload = {
            "session_id": scorekeeper.session_id,
            "phase": snap["phase"],
            "mode": snap["mode"],
            "round": snap["round"],
            "question_number": snap["question_number"],
            "scorekeeper_state": snap,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            **extra_fields,
        }
        supabase.table("lily_sessions").upsert(
            payload, on_conflict="session_id"
        ).execute()
    except Exception as e:
        logger.error("lily_checkpoint error: %s", e)


async def lily_heartbeat(
    supabase: SupabaseClient,
    scorekeeper,
    stop_event: asyncio.Event,
    interval: Optional[float] = None,
) -> None:
    """Background task: checkpoint every `interval` seconds while live."""
    delay = interval if interval is not None else lily_config.checkpoint_interval_seconds()
    while not stop_event.is_set():
        await asyncio.sleep(delay)
        if stop_event.is_set():
            break
        await lily_checkpoint(supabase, scorekeeper)
        logger.debug("Heartbeat checkpoint — phase=%s", scorekeeper.phase)


async def lily_session_end(
    supabase: SupabaseClient,
    scorekeeper,
    final_standings: Optional[list] = None,
    **extra_fields,
) -> None:
    """Final checkpoint on room disconnect or session close."""
    scorekeeper.set_phase("wrapup")
    extra = dict(extra_fields)
    if final_standings is not None:
        extra["final_standings"] = final_standings
    await lily_checkpoint(supabase, scorekeeper, phase="ended", **extra)
    logger.info("Session ended — session_id=%s", scorekeeper.session_id)


# ---------------------------------------------------------------------------
# Transcript batching (lbs pattern: flush at 10 rows or 30s)
# ---------------------------------------------------------------------------

class LilyTranscriptBatcher:
    def __init__(self, supabase: SupabaseClient, session_id: str) -> None:
        self._supabase = supabase
        self._session_id = session_id
        self._batch: list[dict] = []
        self._last_flush = time.time()

    def add(
        self,
        text: str,
        speaker_label: Optional[str],
        speaker_name: Optional[str],
        segment_start: Optional[float] = None,
        segment_end: Optional[float] = None,
    ) -> None:
        self._batch.append({
            "session_id": self._session_id,
            "speaker_label": speaker_label,
            "speaker_name": speaker_name,
            "text": text,
            "segment_start": segment_start,
            "segment_end": segment_end,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        if (
            len(self._batch) >= TRANSCRIPT_BATCH_SIZE
            or (time.time() - self._last_flush) >= TRANSCRIPT_BATCH_FLUSH_SECONDS
        ):
            asyncio.ensure_future(self.flush())

    async def flush(self) -> None:
        if not self._batch:
            return
        rows, self._batch = self._batch, []
        self._last_flush = time.time()
        try:
            await asyncio.to_thread(
                lambda: self._supabase.table("lily_transcripts").insert(rows).execute()
            )
        except Exception as e:
            logger.error("transcript flush error (%d rows dropped): %s", len(rows), e)


# ---------------------------------------------------------------------------
# Answer audit log
# ---------------------------------------------------------------------------

async def lily_write_answer(
    supabase: SupabaseClient,
    session_id: str,
    player_name: Optional[str],
    question_id: Optional[str],
    question_index: int,
    transcript: str,
    verdict: str,
    eval_tier: int,
    awarded_points: int,
) -> None:
    """Fire-and-forget audit row for every adjudicated attempt."""
    try:
        await asyncio.to_thread(
            lambda: supabase.table("lily_answers").insert({
                "session_id": session_id,
                "player_name": player_name,
                "question_id": question_id,
                "question_index": question_index,
                "transcript": transcript,
                "verdict": verdict,
                "eval_tier": eval_tier,
                "awarded_points": awarded_points,
                "ts": datetime.now(timezone.utc).isoformat(),
            }).execute()
        )
    except Exception as e:
        logger.error("lily_write_answer error: %s", e)


# ---------------------------------------------------------------------------
# Curated question bank (demo-day insurance; runbook KB-only fallback)
# ---------------------------------------------------------------------------

async def lily_fetch_bank_question(
    supabase: SupabaseClient,
    category: str,
    difficulty_tier: int,
    exclude_prompts: list[str],
    mode: str = "general",
) -> Optional[dict]:
    """Pull one unused curated question from lily_questions, preferring the
    requested category/tier, falling back to any unused row. Returns the
    §4.2 structured shape or None.

    Mode guard (consent-safety): adult=true bank rows are returned ONLY
    when mode == 'adult' — general mode hard-excludes them."""
    try:
        rows = await asyncio.to_thread(
            lambda: supabase.table("lily_questions")
            .select("*")
            .execute()
        )
        pool = lily_memory.lily_bank_mode_filter(rows.data or [], mode)
        candidates = [
            r for r in pool
            if r.get("question") and r["question"] not in exclude_prompts
        ]
        if not candidates:
            return None
        preferred = [
            r for r in candidates
            if r.get("category") == category
            and r.get("difficulty_tier") == difficulty_tier
        ] or [r for r in candidates if r.get("category") == category] or candidates
        row = preferred[0]
        acceptable = row.get("acceptable_answers")
        if not isinstance(acceptable, list) or not acceptable:
            acceptable = [str(row.get("answer", "")).lower()]
        return {
            "id": f"kb_{row.get('id', 0)}",
            "category": row.get("category") or category,
            "difficulty_tier": row.get("difficulty_tier") or difficulty_tier,
            "prompt": row["question"],
            "canonical_answer": row.get("answer", ""),
            "acceptable_answers": acceptable,
            "reveal_color": row.get("reveal_color") or "",
        }
    except Exception as e:
        logger.error("lily_fetch_bank_question error: %s", e)
        return None


# ---------------------------------------------------------------------------
# Group facts (lobby facts / running-bit material, persisted for rematches)
# ---------------------------------------------------------------------------

async def lily_write_group_fact(
    supabase: SupabaseClient,
    group_id: str,
    player_name: str,
    fact: str,
    source_session_id: str,
) -> None:
    try:
        await asyncio.to_thread(
            lambda: supabase.table("lily_group_facts").insert({
                "group_id": group_id,
                "player_name": player_name,
                "fact": fact,
                "source_session_id": source_session_id,
            }).execute()
        )
    except Exception as e:
        logger.error("lily_write_group_fact error: %s", e)


# ---------------------------------------------------------------------------
# Voiceprint persistence (fully passive — Lovebirds Task 6 lift)
# ---------------------------------------------------------------------------

async def lily_enroll_voiceprints(
    stt,
    supabase: SupabaseClient,
    group_id: str,
    scorekeeper,
) -> None:
    """Background task: capture Speechmatics speaker identifiers for every
    bound player and upsert to lily_speaker_voiceprints keyed on group_id
    (unique on (group_id, speaker_label))."""
    try:
        get_ids = getattr(stt, "get_speaker_ids", None)
        if get_ids is None:
            logger.info("VOICEPRINT | stt has no get_speaker_ids — skipping enrollment")
            return
        # get_speaker_ids() is ASYNC at 1.6.4 and needs ~5 spoken words per
        # speaker before it returns useful identifiers — this task stays in
        # the background and tolerates empty results.
        speaker_ids = await get_ids()
        if not speaker_ids:
            logger.info("VOICEPRINT | no speaker identifiers available yet")
            return
        # Return shape is list[SpeakerIdentifier] (or a nested list) —
        # flatten defensively.
        flat = []
        for entry in speaker_ids:
            if isinstance(entry, list):
                flat.extend(entry)
            else:
                flat.append(entry)

        label_to_name = {
            state.get("speaker_label"): name
            for name, state in scorekeeper.players.items()
            if state.get("speaker_label")
        }
        rows = []
        for entry in flat:
            label = getattr(entry, "label", None) or (
                entry.get("label") if isinstance(entry, dict) else None
            )
            identifiers = getattr(entry, "speaker_identifiers", None) or (
                entry.get("speaker_identifiers") if isinstance(entry, dict) else None
            )
            if not label:
                continue
            rows.append({
                "group_id": group_id,
                "speaker_label": label,
                "player_name": label_to_name.get(label),
                "speaker_identifiers": identifiers,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
        if not rows:
            return
        await asyncio.to_thread(
            lambda: supabase.table("lily_speaker_voiceprints").upsert(
                rows, on_conflict="group_id,speaker_label"
            ).execute()
        )
        logger.info(
            "VOICEPRINT | enrolled %d speakers for group=%s", len(rows), group_id
        )
    except Exception as e:
        logger.error("lily_enroll_voiceprints error: %s", e)


async def lily_load_voiceprints(
    supabase: SupabaseClient,
    group_id: str,
) -> list:
    """Load stored voiceprints for a returning group. Returns plain dicts
    ({label, speaker_identifiers}); lily_agent.py converts them to
    Speechmatics SpeakerIdentifier objects for the known_speakers kwarg.
    Instant recognition on rematch."""
    try:
        result = await asyncio.to_thread(
            lambda: supabase.table("lily_speaker_voiceprints")
            .select("speaker_label, player_name, speaker_identifiers")
            .eq("group_id", group_id)
            .execute()
        )
        rows = result.data or []
        known = []
        for row in rows:
            identifiers = row.get("speaker_identifiers")
            if not identifiers:
                continue
            known.append({
                "label": row.get("player_name") or row.get("speaker_label"),
                "speaker_identifiers": identifiers,
            })
        if known:
            logger.info(
                "VOICEPRINT | loaded %d known speakers for group=%s",
                len(known), group_id,
            )
        return known
    except Exception as e:
        logger.error("lily_load_voiceprints error: %s", e)
        return []
