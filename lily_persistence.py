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
import json
import logging
import random
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from postgrest.exceptions import APIError as PostgrestAPIError
from supabase import Client as SupabaseClient, create_client as create_supabase_client
from supabase.client import ClientOptions as SupabaseClientOptions

import lily_evaluation
import lily_bank
import lily_config
import lily_forget
import lily_memory
import lily_stt_tuning

logger = logging.getLogger("lily_persistence")

TRANSCRIPT_BATCH_SIZE = 10
TRANSCRIPT_BATCH_FLUSH_SECONDS = 30.0
TRANSCRIPT_FLUSH_ATTEMPTS = 3
BANK_FETCH_CANDIDATE_LIMIT = 100


def lily_create_supabase_client() -> Optional[SupabaseClient]:
    url = lily_config.supabase_url()
    key = lily_config.supabase_service_role_key()
    if not url or not key:
        logger.error("LILY_INIT | SUPABASE_ENV_MISSING | url_set=%s key_set=%s",
                     bool(url), bool(key))
        return None
    try:
        # HTTP timeouts at the client root (live 2026-07-15: one hanging
        # postgrest call froze the heartbeat loop mid-session — checkpoints
        # and the last_active_at beat stopped at 01:40 while the game ran
        # on). The sync client ships with NO timeout by default.
        return create_supabase_client(url, key, options=SupabaseClientOptions(
            postgrest_client_timeout=10,
            storage_client_timeout=20,
        ))
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
    # Bounded retries (live 2026-07-15 22:38: the client-level 10s HTTP
    # timeout — added to stop MID-SESSION hangs — made one transiently slow
    # boot-time round trip fatal: ReadTimeout -> fail-fast RuntimeError ->
    # the job died and Lily never joined the room). Fail-fast stays the
    # policy for a genuinely unpersistable room; a transient timeout gets
    # three attempts with short backoff first. Boot-time only — nothing
    # else is running yet, so the blocking sleeps are harmless.
    last_error: Optional[Exception] = None
    for attempt in range(1, 4):
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
            logger.info(
                "LILY_INIT | EARLY_SESSION_ROW | inserted session_id=%s "
                "attempt=%d", session_id, attempt,
            )
            return
        except Exception as e:
            last_error = e
            logger.warning(
                "LILY_INIT | EARLY_SESSION_ROW_RETRY | room_id=%s attempt=%d/3 "
                "error_class=%s error=%s",
                session_id, attempt, type(e).__name__, e,
            )
            if attempt < 3:
                time.sleep(2.0 * attempt)
    logger.error(
        "LILY_INIT | EARLY_SESSION_ROW_INSERT_FAILED | room_id=%s error_class=%s error=%s",
        session_id, type(last_error).__name__, last_error,
    )
    raise RuntimeError(
        f"LILY_INIT early_session_row insert failed room_id={session_id} "
        f"error_class={type(last_error).__name__}"
    ) from last_error


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
        # to_thread: the postgrest client is synchronous — running it inline
        # blocks the event loop (and therefore the live audio pipeline) for a
        # full cross-region HTTP round trip on every score change. wait_for:
        # belt on top of the client-level timeout — a hang here previously
        # froze the heartbeat loop for the rest of the session.
        await asyncio.wait_for(
            asyncio.to_thread(
                lambda: supabase.table("lily_sessions")
                .upsert(payload, on_conflict="session_id")
                .execute()
            ),
            timeout=15.0,
        )
    except Exception as e:
        logger.error("lily_checkpoint error: %s", e)


async def lily_set_training_optin(
    supabase: SupabaseClient,
    session_id: str,
    value: bool,
) -> bool:
    """WO-ADDRESSEE-H1 Task 5b — the consent flag, implemented not
    aspirational. Sets lily_sessions.training_optin (default FALSE;
    column applied with schema amendment 5a) with its timestamp, logged.
    Set ONLY by explicit host action. It gates NOTHING yet — no audio
    retention exists to gate — but the flag and its audit trail exist
    BEFORE any H3 audio-retention work can begin."""
    if supabase is None or not session_id:
        return False
    try:
        await asyncio.wait_for(
            asyncio.to_thread(
                lambda: supabase.table("lily_sessions").update({
                    "training_optin": bool(value),
                    "training_optin_at": datetime.now(timezone.utc).isoformat(),
                }).eq("session_id", session_id).execute()
            ),
            timeout=10.0,
        )
        logger.info(
            "LILY_PRIVACY | TRAINING_OPTIN | session=%s value=%s",
            session_id, bool(value),
        )
        return True
    except Exception as e:
        logger.error("lily_set_training_optin error: %s", e)
        return False


async def lily_heartbeat(
    supabase: SupabaseClient,
    scorekeeper,
    stop_event: asyncio.Event,
    interval: Optional[float] = None,
    metadata_provider=None,
) -> None:
    """Background task: checkpoint every `interval` seconds while live.
    metadata_provider (optional zero-arg callable) contributes a metadata
    dict per beat — used for rolling latency averages (§11.6)."""
    delay = interval if interval is not None else lily_config.checkpoint_interval_seconds()
    while not stop_event.is_set():
        await asyncio.sleep(delay)
        if stop_event.is_set():
            break
        try:
            extra = {}
            if metadata_provider is not None:
                try:
                    extra["metadata"] = metadata_provider()
                except Exception as e:
                    logger.debug("heartbeat metadata_provider failed: %s", e)
            await lily_checkpoint(supabase, scorekeeper, **extra)
            logger.debug("Heartbeat checkpoint — phase=%s", scorekeeper.phase)
        except Exception:
            # The heartbeat must outlive any single bad beat — a crash here
            # silently ends checkpoints AND the last_active_at freshness the
            # frontend watchdog keys on.
            logger.exception("LILY_HEARTBEAT | BEAT_FAILED — continuing")


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


ABANDONED_SESSION_MIN_AGE_SECONDS = 900.0  # 15 min inactive, non-ended


async def lily_sweep_abandoned_sessions(
    supabase: Optional[SupabaseClient],
    min_age_s: Optional[float] = None,
    limit: int = 20,
) -> dict:
    """PATCH-001 T9 (RETIRE_WITH_WS6): close out sessions that died
    mid-game. A session whose phase is not 'ended' and whose updated_at
    is older than the threshold crashed or was abandoned (the live
    89A97A: phase stuck 'round', q_3812 registered at the instant of
    death). Force phase -> ended with a loud WARN so a crash-instant
    ghost leaves no active trace; the report sweep (separate) then
    assesses it from the stored transcript. Runs at session start
    alongside lily_report_sweep. Never raises; returns counts."""
    stats = {"scanned": 0, "closed": 0}
    if supabase is None:
        return stats
    try:
        min_age = (
            min_age_s if min_age_s is not None
            else ABANDONED_SESSION_MIN_AGE_SECONDS
        )
        cutoff = (
            datetime.now(timezone.utc)
            - timedelta(seconds=min_age)
        ).isoformat()
        result = await asyncio.to_thread(
            lambda: supabase.table("lily_sessions")
            .select("session_id, phase, updated_at")
            .neq("phase", "ended")
            .lt("updated_at", cutoff)
            .order("updated_at")
            .limit(limit)
            .execute()
        )
        rows = result.data or []
        for row in rows:
            stats["scanned"] += 1
            sid = row["session_id"]
            try:
                await asyncio.to_thread(
                    lambda: supabase.table("lily_sessions")
                    .update({
                        "phase": "ended",
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    })
                    .eq("session_id", sid)
                    .neq("phase", "ended")
                    .execute()
                )
                stats["closed"] += 1
                logger.warning(
                    "LILY_SWEEP | ABANDONED_SESSION_CLOSED | session=%s "
                    "was_phase=%s last_active=%s — forced ended (crash/"
                    "abandonment); report sweep will assess",
                    sid, row.get("phase"), row.get("updated_at"),
                )
            except Exception as e:
                logger.error(
                    "LILY_SWEEP | ABANDONED_CLOSE_FAILED | session=%s error=%s",
                    sid, e,
                )
        if stats["scanned"]:
            logger.info(
                "LILY_SWEEP | ABANDONED | scanned=%d closed=%d",
                stats["scanned"], stats["closed"],
            )
    except Exception as e:
        logger.error("LILY_SWEEP | ABANDONED_SWEEP_FAILED | error=%s", e)
    return stats


# ---------------------------------------------------------------------------
# Transcript batching (lbs pattern: flush at 10 rows or 30s)
# ---------------------------------------------------------------------------

class LilyTranscriptBatcher:
    def __init__(self, supabase: SupabaseClient, session_id: str) -> None:
        self._supabase = supabase
        self._session_id = session_id
        self._batch: list[dict] = []
        self._last_flush = time.time()
        self._flush_lock = asyncio.Lock()
        self._disabled = False

    def add(
        self,
        text: str,
        speaker_label: Optional[str],
        speaker_name: Optional[str],
        segment_start: Optional[float] = None,
        segment_end: Optional[float] = None,
    ) -> None:
        if self._disabled:
            return
        self._batch.append({
            "event_id": str(uuid.uuid4()),
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
        async with self._flush_lock:
            if not self._batch or self._disabled:
                return
            rows, self._batch = self._batch, []
            self._last_flush = time.time()
            for attempt in range(TRANSCRIPT_FLUSH_ATTEMPTS):
                try:
                    await asyncio.to_thread(
                        lambda: self._supabase.table("lily_transcripts")
                        .upsert(rows, on_conflict="event_id")
                        .execute()
                    )
                    return
                except Exception as e:
                    if attempt + 1 < TRANSCRIPT_FLUSH_ATTEMPTS:
                        await asyncio.sleep(0.2 * (2 ** attempt))
                        continue
                    self._batch = rows + self._batch
                    logger.error(
                        "transcript flush error (%d rows retained for retry): %s",
                        len(rows), e,
                    )

    async def discard_pending(self, *, disable: bool = False) -> None:
        """Wait for any in-flight flush, then discard queued transcript rows.

        Forget uses ``disable=True`` so rows spoken before or after deletion
        cannot race back into the same session audit stream.
        """
        async with self._flush_lock:
            self._batch.clear()
            if disable:
                self._disabled = True


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
    cause: Optional[str] = None,
    utterance_id: Optional[str] = None,
) -> None:
    """Fire-and-forget audit row for every scoring event (WS-7: not just
    adjudicated attempts — bonuses, make-goods, operator awards too).

    ``cause`` targets the lily_answers.cause column (Doc DDL request), and
    ``utterance_id`` the HOTFIX-006 N9 column: WHICH spoken utterance this
    row is about. The live q_1052 row carried Rami's "Go." with nothing to
    say which utterance it had bound — his actual answer, "Okay. It's
    Jupiter.", never entered the ledger. Until either column lands the
    first insert fails and is retried once with the optional keys stripped
    — the row is never lost, and non-adjudication events keep their trace
    because their verdict field carries the cause code. Once the columns
    exist the same code path lights them up unchanged."""
    row = {
        "session_id": session_id,
        "player_name": player_name,
        "question_id": question_id,
        "question_index": question_index,
        "transcript": transcript,
        "verdict": verdict,
        "eval_tier": eval_tier,
        "awarded_points": awarded_points,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    if cause is not None:
        row["cause"] = cause
    if utterance_id is not None:
        row["utterance_id"] = utterance_id
    try:
        await asyncio.to_thread(
            lambda: supabase.table("lily_answers").insert(row).execute()
        )
    except Exception as e:
        optional = [k for k in ("cause", "utterance_id") if k in row]
        if not optional:
            logger.error("lily_write_answer error: %s", e)
            return
        # Strip optional columns one at a time so a pre-024 database that
        # is missing only ``cause`` still keeps ``utterance_id`` (and the
        # reverse) when that column has already landed. Last resort drops
        # every remaining optional key.
        last_err = e
        for key in list(optional):
            dropped = row.pop(key, None)
            logger.info(
                "lily_write_answer: optional column %r not accepted (%s) — "
                "retrying without it (cause is encoded in verdict for "
                "non-adjudication rows; dropped=%r)",
                key, last_err, dropped,
            )
            try:
                await asyncio.to_thread(
                    lambda: supabase.table("lily_answers").insert(row).execute()
                )
                return
            except Exception as e2:
                last_err = e2
                continue
        logger.error("lily_write_answer error: %s", last_err)


async def lily_write_score_event(
    supabase: SupabaseClient,
    session_id: str,
    entry: dict,
) -> None:
    """Persist one score-ledger entry (LilyScorekeeper.apply_score_event)
    as a lily_answers row. Adjudication rows keep verdict semantics
    (correct/incorrect); every other cause writes its cause code as the
    verdict — the clean non-DDL trace until the cause column lands."""
    cause = entry.get("cause") or "unknown"
    if cause == "answer":
        verdict = "correct" if entry.get("correct") else "incorrect"
    else:
        verdict = cause
    await lily_write_answer(
        supabase,
        session_id,
        entry.get("player"),
        entry.get("question_id"),
        int(entry.get("question_index") or 0),
        entry.get("transcript") or "",
        verdict,
        int(entry.get("eval_tier") or 0),
        int(entry.get("points") or 0),
        cause=cause,
        utterance_id=entry.get("utterance_id"),
    )


# ---------------------------------------------------------------------------
# Addressee-label corpus (B1 training-data flywheel)
# ---------------------------------------------------------------------------

async def lily_log_addressee(
    supabase: SupabaseClient,
    row: dict,
) -> Optional[int]:
    """Fire-and-forget insert into lily_addressee_log. Returns the new row
    id when available (kept by the caller for the later label UPDATE).
    Tolerates failures silently — corpus logging must never surface into
    the live session (debug log only).

    WS-11 fail-soft: the telemetry columns land in later migrations —
    FL-1's migration 018 (agent_classification + addressee_score +
    addressee_score_components + side_cluster_id + side_cluster_event) and
    WS-11's migration 019 (timing_source, timing_drift_seconds). Until those
    DDLs are applied in a given environment the insert raises PGRST204
    ("column does not exist") and the WHOLE corpus row was being lost (FL-1
    ships no fail-soft of its own). On any insert failure, retry once with
    ALL of those keys stripped so the base row (the training-data flywheel)
    still lands — no memory is bad memory. The retry is best-effort: on a
    mixed-migration environment (018 applied, 019 not) it drops FL-1's
    columns from that one retried row too; corpus completeness over
    per-row column fidelity."""
    _TELEMETRY_KEYS = (
        # FL-1 migration 018
        "agent_classification",
        "addressee_score",
        "addressee_score_components",
        "side_cluster_id",
        "side_cluster_event",
        # WS-11 migration 019
        "timing_source",
        "timing_drift_seconds",
    )
    try:
        result = await asyncio.to_thread(
            lambda: supabase.table("lily_addressee_log").insert(row).execute()
        )
        data = getattr(result, "data", None) or []
        if data and isinstance(data[0], dict):
            return data[0].get("id")
        return None
    except Exception as e:
        if not any(k in row for k in _TELEMETRY_KEYS):
            logger.debug("lily_log_addressee error: %s", e)
            return None
        logger.debug(
            "lily_log_addressee telemetry insert failed (%s) — retrying "
            "without WS-11 columns (migration 018 not applied here)", e,
        )
        base_row = {k: v for k, v in row.items() if k not in _TELEMETRY_KEYS}
        try:
            result = await asyncio.to_thread(
                lambda: supabase.table("lily_addressee_log")
                .insert(base_row)
                .execute()
            )
            data = getattr(result, "data", None) or []
            if data and isinstance(data[0], dict):
                return data[0].get("id")
            return None
        except Exception as e2:
            logger.debug("lily_log_addressee retry error: %s", e2)
            return None


async def lily_update_addressee_label(
    supabase: SupabaseClient,
    row_id: int,
    label: str,
    label_source: str,
) -> None:
    """Fire-and-forget label UPDATE on an earlier lily_addressee_log row
    (implicit labels at adjudication commit, appeal corrections, explicit
    clarify resolutions). Silent on failure (debug log only)."""
    try:
        await asyncio.to_thread(
            lambda: supabase.table("lily_addressee_log")
            .update({"label": label, "label_source": label_source})
            .eq("id", row_id)
            .execute()
        )
    except Exception as e:
        logger.debug("lily_update_addressee_label error: %s", e)


# ---------------------------------------------------------------------------
# Acoustic trajectories (WO-LILY-AUDEERING-001 Task 6)
# ---------------------------------------------------------------------------

async def lily_write_acoustic_trajectory(
    supabase: SupabaseClient,
    session_id: str,
    turn_index: int,
    snapshot: Optional[dict],
) -> None:
    """One lily_acoustic_trajectories row per user turn — the LATEST devAIce
    capture at the moment the turn finalized. Fire-and-forget via to_thread;
    silent on failure (debug log only — telemetry must never surface into
    the live session). No row is written when no snapshot exists (breaker
    open / nothing captured yet) — the trajectory table records signal, the
    addressee log records the explicit-null health state."""
    if supabase is None or not snapshot:
        return
    try:
        await asyncio.to_thread(
            lambda: supabase.table("lily_acoustic_trajectories").insert({
                "session_id": session_id,
                "turn_index": turn_index,
                "category": snapshot.get("category") or {},
                "dimension": snapshot.get("dimension") or {},
                "prosody": snapshot.get("prosody") or {},
                "features": snapshot.get("features") or {},
                "audio_quality": snapshot.get("audio_quality") or {},
                "scene": snapshot.get("scene"),
            }).execute()
        )
    except Exception as e:
        logger.debug("lily_write_acoustic_trajectory error: %s", e)


# ---------------------------------------------------------------------------
# Session reports (B3 — Lovebirds call_reports shape, WRITE side only)
# ---------------------------------------------------------------------------

async def lily_write_session_report(
    supabase: SupabaseClient,
    session_id: str,
    group_id: str,
    transcript: list,
    game_stats: dict,
) -> None:
    """One lily_session_reports row at session close — idempotent upsert on
    session_id (Lovebirds call-report pattern). WRITE side only: the payload
    deliberately omits report_status and assessment, so report_status keeps
    its 'pending' default on insert and the clinical desk's later assessment
    fill is never clobbered by a re-run."""
    if supabase is None:
        return
    try:
        await asyncio.to_thread(
            lambda: supabase.table("lily_session_reports").upsert({
                "session_id": session_id,
                "group_id": group_id,
                "transcript": transcript,
                "game_stats": game_stats,
            }, on_conflict="session_id").execute()
        )
        logger.info("LILY_REPORT | WRITE | session=%s group=%s", session_id, group_id)
    except Exception as e:
        logger.error("lily_write_session_report error: %s", e)


# ---------------------------------------------------------------------------
# Curated question bank (demo-day insurance; runbook KB-only fallback)
# ---------------------------------------------------------------------------

async def lily_fetch_bank_question(
    supabase: SupabaseClient,
    category: str,
    difficulty_tier: int,
    exclude_prompts: list[str],
    mode: str = "general",
    exclude_ids: Optional[set] = None,
    exclude_hashes: Optional[set] = None,
    exclude_answers: Optional[set] = None,
    strict_category: bool = False,
) -> Optional[dict]:
    """Pull one unused curated question from lily_questions, preferring the
    requested category/tier, falling back to any unused row. Returns the
    §4.2 structured shape or None.

    Category strictness (WO-LILY-HOTFIX-006 N2): `strict_category=True`
    removes the any-category fallback stage, so the draw returns a row in
    THIS category or nothing at all. That stage is the anti-starvation
    policy for the fixed family rotation — "academic is dry, serve
    pop_culture" is a fallback nobody is lied to by. For a topic the table
    NAMED it is something else entirely: in session lily-16A9AE a "Cape Cod"
    round drew Psycho, One Direction and the rupee question through this
    exact stage while Lily narrated a custom Cape Cod round over the top.
    A named topic gets that topic or an honest refusal — never "anything".

    Mode guard (consent-safety): adult=true bank rows are returned ONLY
    when mode == 'adult' — general mode hard-excludes them.

    Asked-history guard (bank-curation WO, migration 010): rows whose
    kb_ id is in `exclude_ids`, whose normalized-text hash is in
    `exclude_hashes`, OR whose normalized canonical ANSWER is in
    `exclude_answers` (the group's lily_asked_history) are never re-served
    to that group. PATCH-001 T7: the answer guard closes the live
    cross-session repeat (kb_469 'Mars' re-served to a group that played
    a differently-worded Mars question the previous day — same answer,
    distinct id and text hash, so id/hash exclusion alone missed it; the
    generation path already avoided answers, the bank path did not)."""
    exclude_ids = exclude_ids or set()
    exclude_hashes = exclude_hashes or set()
    exclude_answers = {
        lily_evaluation.lily_normalize_answer(str(a))
        for a in (exclude_answers or set())
        if str(a).strip()
    }
    try:
        def _query_stage(
            stage_category: Optional[str],
            stage_tier: Optional[int],
        ):
            query = (
                supabase.table("lily_questions")
                .select("*")
                .eq("status", "active")
                # Adult mode serves the adult deck only; general mode hard
                # excludes it. Deck exhaustion falls through to mode-aware
                # generation, never across the consent boundary.
                .eq("adult", mode == "adult")
            )
            if stage_category is not None:
                query = query.eq("category", stage_category)
            if stage_tier is not None:
                query = query.eq("difficulty_tier", stage_tier)
            return query.limit(BANK_FETCH_CANDIDATE_LIMIT).execute()

        # Tier relaxation stays inside a strict draw: a Cape Cod question at
        # the wrong difficulty is still a Cape Cod question. Only the
        # category filter is inviolable.
        stages = (
            (category, difficulty_tier),
            (category, None),
        ) if strict_category else (
            (category, difficulty_tier),
            (category, None),
            (None, None),
        )
        row = None
        for stage_category, stage_tier in stages:
            rows = await asyncio.to_thread(
                _query_stage, stage_category, stage_tier
            )
            pool = lily_memory.lily_bank_mode_filter(rows.data or [], mode)
            candidates = [
                r for r in pool
                if r.get("question") and r["question"] not in exclude_prompts
                and (r.get("status") or "active") == "active"
                and f"kb_{r.get('id', 0)}" not in exclude_ids
                and (
                    not exclude_hashes
                    or lily_bank.lily_question_text_hash(r["question"])
                    not in exclude_hashes
                )
                and (
                    not exclude_answers
                    or lily_evaluation.lily_normalize_answer(
                        str(r.get("canonical_answer") or r.get("answer") or "")
                    ) not in exclude_answers
                )
            ]
            if candidates:
                row = random.choice(candidates)
                break
        if row is None:
            return None
        # Live-schema tolerance: the production table stores the answer as
        # `canonical_answer` (text) with `acceptable_answers` text[]; the
        # repo's 001 migration used `answer`. Read both, prefer live.
        answer = row.get("canonical_answer") or row.get("answer") or ""
        acceptable = row.get("acceptable_answers")
        if not isinstance(acceptable, list) or not acceptable:
            acceptable = [str(answer).lower()]
        out = {
            "id": f"kb_{row.get('id', 0)}",
            "category": row.get("category") or category,
            "difficulty_tier": row.get("difficulty_tier") or difficulty_tier,
            "prompt": row["question"],
            "canonical_answer": answer,
            "acceptable_answers": acceptable,
            "reveal_color": row.get("reveal_color") or "",
        }
        # Bank image wiring (migration 012, sub-agent H): the stored image
        # triplet rides along when the row carries it — CACHE-FIRST means a
        # bank row with an image is never re-sourced. The agent layer strips
        # it again in voice_only mode (pictures are a lobby choice).
        if row.get("image_url"):
            out["image_url"] = row["image_url"]
            out["image_source"] = row.get("image_source") or "web"
            if row.get("image_license_note"):
                out["image_license_note"] = row["image_license_note"]
        # Bank MC wiring (adult bank, migration 014): stored choices ride
        # along — exactly 4 plain options, positional letter mapping — so
        # a bank MC row runs the MC matcher instead of synthesis.
        if isinstance(row.get("choices"), list) and row["choices"]:
            out["choices"] = [str(c) for c in row["choices"]]
        # image_prompt rides along for pictures-mode serving (generation
        # prompt for rows that ship a prompt instead of a cached URL); it
        # is never published to the room and is stripped with the rest of
        # the image fields in voice_only.
        if row.get("image_prompt"):
            out["image_prompt"] = row["image_prompt"]
        return out
    except Exception as e:
        logger.error("lily_fetch_bank_question error: %s", e)
        return None


async def lily_burn_question(
    supabase: SupabaseClient, question_id: str
) -> bool:
    """Burn protocol (say-gate WO): mark one lily_questions row
    status='burned' after its answer leaked on air. Only bank questions
    (id shape 'kb_<row id>') have a DB row to mark; generated questions
    are simply discarded by the caller. Scope is GLOBAL today (migration
    009 status column); per-group burn rides lily_asked_history later.
    Returns True when a row was marked."""
    qid = str(question_id or "")
    if not qid.startswith("kb_"):
        return False
    try:
        row_id = int(qid[3:])
    except ValueError:
        return False
    try:
        await asyncio.to_thread(
            lambda: supabase.table("lily_questions")
            .update({"status": "burned"})
            .eq("id", row_id)
            .execute()
        )
        return True
    except Exception as e:
        logger.error("lily_burn_question error for %s: %s", qid, e)
        return False


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
# Group preferences (group prefs WO — lily_group_prefs, migration 013)
# ---------------------------------------------------------------------------

async def lily_write_group_prefs(
    supabase: SupabaseClient,
    group_id: str,
    prefs: dict,
) -> None:
    """Upsert the WHOLE opaque prefs dict for a group (on_conflict=group_id)
    — called on every preference change. The dict is stored opaquely: this
    WO writes {"pacing": ...}; round_format / media_mode keys are written by
    their own features post-merge and ride the same whole-dict upsert.
    Fire-and-forget: logs LILY_PREFS | markers, never raises."""
    if supabase is None or not group_id:
        return
    try:
        payload = {
            "group_id": group_id,
            "prefs": dict(prefs or {}),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        await asyncio.to_thread(
            lambda: supabase.table("lily_group_prefs")
            .upsert(payload, on_conflict="group_id")
            .execute()
        )
        logger.info(
            "LILY_PREFS | WRITE | group=%s keys=%s",
            group_id, ",".join(sorted((prefs or {}).keys())) or "-",
        )
    except Exception as e:
        logger.error("lily_write_group_prefs error: %s", e)


async def lily_load_group_prefs(
    supabase: SupabaseClient,
    group_id: str,
) -> dict:
    """Load the stored prefs dict for a group. Returns {} when nothing is
    stored or on any error (prefs are never load-bearing — a missing 013
    table degrades to cold-group behavior)."""
    if supabase is None or not group_id:
        return {}
    try:
        result = await asyncio.to_thread(
            lambda: supabase.table("lily_group_prefs")
            .select("prefs")
            .eq("group_id", group_id)
            .limit(1)
            .execute()
        )
        rows = result.data or []
        prefs = (rows[0] or {}).get("prefs") if rows else None
        if isinstance(prefs, dict) and prefs:
            logger.info(
                "LILY_PREFS | LOADED | group=%s keys=%s",
                group_id, ",".join(sorted(prefs.keys())),
            )
            return dict(prefs)
        return {}
    except Exception as e:
        logger.error("lily_load_group_prefs error: %s", e)
        return {}


async def lily_rekey_group_prefs(
    supabase: SupabaseClient,
    old_group_id: str,
    new_group_id: str,
    session_id: str,
) -> None:
    """Move a prefs row written under a provisional group id to the RESOLVED
    id on a mid-session upgrade. group_id is the table's PRIMARY KEY, so a
    blind UPDATE collides when the resolved group already has a row —
    instead the two dicts MERGE key-by-key with the OLD (this session's
    fresher choices) winning, upserted under the new id. The old row is
    deleted only when the provisional id was the room-random session id
    (the same conservatism as the voiceprint re-key: never destroys another
    real group's row). Tolerates failure independently, like the other
    lily_rekey_group tables."""
    if not old_group_id or not new_group_id or old_group_id == new_group_id:
        return
    try:
        old_res = await asyncio.to_thread(
            lambda: supabase.table("lily_group_prefs")
            .select("prefs").eq("group_id", old_group_id).limit(1).execute()
        )
        old_rows = old_res.data or []
        old_prefs = (old_rows[0] or {}).get("prefs") if old_rows else None
        if not isinstance(old_prefs, dict) or not old_prefs:
            return  # nothing written under the provisional id — no-op
        new_res = await asyncio.to_thread(
            lambda: supabase.table("lily_group_prefs")
            .select("prefs").eq("group_id", new_group_id).limit(1).execute()
        )
        new_rows = new_res.data or []
        stored = (new_rows[0] or {}).get("prefs") if new_rows else None
        merged = dict(stored) if isinstance(stored, dict) else {}
        merged.update(old_prefs)  # this session's choices win the reconcile
        await asyncio.to_thread(
            lambda: supabase.table("lily_group_prefs").upsert({
                "group_id": new_group_id,
                "prefs": merged,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }, on_conflict="group_id").execute()
        )
        if old_group_id == session_id:
            await asyncio.to_thread(
                lambda: supabase.table("lily_group_prefs")
                .delete().eq("group_id", old_group_id).execute()
            )
        logger.info(
            "LILY_MEMORY | REKEY | table=lily_group_prefs session=%s old=%s "
            "new=%s keys=%s",
            session_id, old_group_id, new_group_id,
            ",".join(sorted(merged.keys())),
        )
    except Exception as e:
        logger.error(
            "LILY_MEMORY | REKEY_FAILED | table=lily_group_prefs session=%s "
            "error=%s", session_id, e,
        )


# ---------------------------------------------------------------------------
# Voiceprint persistence (fully passive — Lovebirds Task 6 lift)
# ---------------------------------------------------------------------------

async def lily_enroll_voiceprints(
    stt,
    supabase: SupabaseClient,
    group_id,
    scorekeeper,
    trigger: str = "unspecified",
) -> bool:
    """Background task: capture Speechmatics speaker identifiers for this
    session's voices and upsert to lily_speaker_voiceprints keyed on
    group_id (unique on (group_id, speaker_label)).

    Fires after the first binding commits, at game start, on a group-id
    upgrade, and (awaited) at session close for late binders — the upsert is
    idempotent, so repeat calls refresh rather than duplicate.

    `group_id` may be a plain string OR a zero-arg callable returning the
    CURRENT group id: callers pass a callable so the write always lands
    under the latest RESOLVED id even when the task was scheduled before a
    mid-session upgrade.

    No-silent-crash rule: every failure path writes a structured
    `LILY_ENROLL | FAILED | reason=...` line. Returns True only when rows
    were actually written."""
    try:
        if supabase is None:
            logger.error(
                "LILY_ENROLL | FAILED | trigger=%s reason=no_supabase_client", trigger
            )
            return False
        get_ids = getattr(stt, "get_speaker_ids", None)
        if get_ids is None:
            logger.error(
                "LILY_ENROLL | FAILED | trigger=%s reason=no_get_speaker_ids_api "
                "(plugin drift vs livekit-plugins-speechmatics 1.6.6)", trigger
            )
            return False
        # Live 2026-07-15 finding: the plugin's get_speaker_ids() returns
        # empty for any stream whose websocket is already closed — at the
        # session_close trigger that is ALWAYS the case, so 21 minutes of
        # speech still enrolled nothing and the failure read as "not enough
        # words". Detect the dead-stream case and name it, so mid-game
        # triggers (first_bind / game_start / round_complete) are visibly
        # the ones that must succeed.
        # WS-13: the get_speakers StartRecognition injection
        # (lily_stt_tuning) makes the server PUSH a SpeakersResult at end of
        # transcript, captured into a store that survives stream teardown.
        # That store is the fallback for both dead-stream cases below —
        # session_close enrollment no longer depends on a live websocket.
        speaker_ids = None
        streams = getattr(stt, "_streams", None)
        stream_dead = streams is not None and not any(
            getattr(getattr(s, "_client", None), "_is_connected", False)
            for s in streams
        )
        if not stream_dead:
            # get_speaker_ids() is ASYNC at 1.6.6 and needs ~5 spoken words
            # per speaker before it returns useful identifiers — this task
            # stays in the background and tolerates (but LOGS) empty
            # results. Awaitable check kept defensive in case a plugin bump
            # makes it synchronous.
            speaker_ids = get_ids()
            if asyncio.iscoroutine(speaker_ids) or isinstance(
                speaker_ids, asyncio.Future
            ):
                speaker_ids = await speaker_ids
        if not speaker_ids:
            captured = lily_stt_tuning.lily_captured_speakers()
            if captured:
                logger.info(
                    "LILY_ENROLL | FALLBACK | trigger=%s "
                    "source=captured_speakers_result n=%d stream_dead=%s",
                    trigger, len(captured), stream_dead,
                )
                speaker_ids = captured
            elif stream_dead:
                logger.error(
                    "LILY_ENROLL | FAILED | trigger=%s "
                    "reason=stt_stream_disconnected_and_no_captured_speakers "
                    "(GET_SPEAKERS needs a live STT session and no "
                    "SpeakersResult was pushed before teardown)", trigger,
                )
                return False
            else:
                logger.error(
                    "LILY_ENROLL | FAILED | trigger=%s reason=no_speaker_ids_yet "
                    "(Speechmatics needs ~5 spoken words per speaker)", trigger
                )
                return False
        # Return shape is list[SpeakerIdentifier] (or a nested list) —
        # flatten defensively.
        flat = []
        for entry in speaker_ids:
            if isinstance(entry, list):
                flat.extend(entry)
            else:
                flat.append(entry)

        gid = group_id() if callable(group_id) else group_id
        label_to_name = {
            state.get("speaker_label"): name
            for name, state in scorekeeper.players.items()
            if state.get("speaker_label")
        }
        # Case-insensitive roster fallback: returning-table labels may be
        # normalized differently by STT ("sarah" vs "Sarah").
        name_lookup = {
            str(name).strip().lower(): name
            for name in (scorekeeper.players or {})
            if str(name).strip()
        }
        # Keep prior label->player_name bindings when a refresh happens
        # before (re)binding catches up this session; otherwise a null
        # player_name overwrite silently narrows next-session lookup
        # (lily_load_voiceprints_by_players filters by player_name).
        known_names_by_label: dict[str, str] = {}
        entry_labels = sorted({
            str(
                getattr(entry, "label", None)
                or (entry.get("label") if isinstance(entry, dict) else "")
            ).strip()
            for entry in flat
            if str(
                getattr(entry, "label", None)
                or (entry.get("label") if isinstance(entry, dict) else "")
            ).strip()
        })
        if gid and entry_labels:
            try:
                existing = await asyncio.to_thread(
                    lambda: supabase.table("lily_speaker_voiceprints")
                    .select("speaker_label, player_name")
                    .eq("group_id", gid)
                    .in_("speaker_label", entry_labels)
                    .execute()
                )
                for row in existing.data or []:
                    label = str((row or {}).get("speaker_label") or "").strip()
                    player_name = str((row or {}).get("player_name") or "").strip()
                    if label and player_name:
                        known_names_by_label[label] = player_name
            except Exception as e:
                logger.warning(
                    "LILY_ENROLL | EXISTING_LABEL_LOOKUP_FAILED | trigger=%s "
                    "group=%s error=%s",
                    trigger, gid, e,
                )
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
            # Dunder-wrapped labels are the engine's ignore namespace
            # (__ASSISTANT__): never persist identifiers under one — on a
            # rematch it would enroll a real voice into an ignored slot.
            if isinstance(label, str) and label.startswith("__") and label.endswith("__"):
                continue
            if not identifiers:
                continue
            resolved_name = label_to_name.get(label)
            if resolved_name is None and isinstance(label, str):
                resolved_name = name_lookup.get(label.strip().lower())
            rows.append({
                "group_id": gid,
                "speaker_label": label,
                # Speechmatics may already return the enrolled player name as
                # the label on a rematch (known_speakers are injected with
                # player-name labels) — keep the roster mapping as fallback.
                "player_name": (
                    resolved_name
                    or known_names_by_label.get(label)
                ),
                "speaker_identifiers": identifiers,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
        # WS-8 under-threshold surfacing: a bound player whose label produced
        # NO identifiers this pass is below Speechmatics' ~5-word floor (the
        # live Chris case: 0.4s of attributed speech never crossed it). Such
        # a player is silently absent from get_speaker_ids() output — record
        # the gap on the scorekeeper so the multi-trigger schedule keeps
        # retrying on their next speech instead of never enrolling them.
        enrolled_labels = {
            str(r.get("speaker_label")) for r in rows if r.get("speaker_identifiers")
        }
        try:
            unenrolled = {
                str(state.get("speaker_label"))
                for state in (scorekeeper.players or {}).values()
                if state.get("speaker_label")
                and str(state.get("speaker_label")) not in enrolled_labels
            }
            setattr(scorekeeper, "unenrolled_bound_labels", unenrolled)
            if unenrolled:
                logger.info(
                    "LILY_ENROLL | UNDER_THRESHOLD | trigger=%s group=%s "
                    "labels=%s (below ~5-word floor — retry on next speech)",
                    trigger, gid, sorted(unenrolled),
                )
        except Exception:
            pass  # tracking is enrichment; never break the enrollment write
        if not rows:
            logger.error(
                "LILY_ENROLL | FAILED | trigger=%s reason=no_usable_identifiers "
                "entries=%d", trigger, len(flat)
            )
            return False
        await asyncio.to_thread(
            lambda: supabase.table("lily_speaker_voiceprints").upsert(
                rows, on_conflict="group_id,speaker_label"
            ).execute()
        )
        logger.info(
            "LILY_ENROLL | OK | trigger=%s group=%s speakers=%d bound=%d",
            trigger, gid, len(rows),
            sum(1 for r in rows if r["player_name"]),
        )
        return True
    except Exception as e:
        logger.error(
            "LILY_ENROLL | FAILED | trigger=%s reason=exception error_class=%s error=%s",
            trigger, type(e).__name__, e,
        )
        return False


async def lily_groups_for_player_name(
    supabase: SupabaseClient,
    player_name: str,
) -> list:
    """Every group whose stored memory lists `player_name`, most recent
    first. Used by the NAME-STATED recognition door.

    Live 2026-08-08 `lily-2C489B`: recognition landed 3m31s and sixteen
    player turns after the greeting, because the only door open was the
    biometric — and the biometric was behind a cold ECAPA load. The player
    had said "My name is Rami" at twenty-two seconds, and by then a
    four-win regular's whole file was one indexed query away.

    A name is WEAKER evidence than a voice and this returns candidates, not
    a verdict: the caller acts only on an unambiguous single match, and the
    matcher's later verdict still outranks it. `player_names` is a text[]
    with a GIN index (migration 007), so `contains` is the cheap path.
    Returns [] on any failure — recognition is never load-bearing."""
    name = str(player_name or "").strip()
    if supabase is None or not name:
        return []
    seen: list = []
    try:
        for variant in dict.fromkeys(
            (name, name.lower(), name.capitalize(), name.title())
        ):
            rows = await asyncio.to_thread(
                lambda v=variant: supabase.table("lily_memories")
                .select("group_id, played_at")
                .contains("player_names", [v])
                .order("played_at", desc=True)
                .limit(20)
                .execute()
            )
            for row in rows.data or []:
                gid = row.get("group_id")
                if gid and gid not in seen:
                    seen.append(gid)
    except Exception as e:
        logger.warning(
            "LILY_MEMORY | NAME_LOOKUP_FAILED | name=%s error=%s", name, e
        )
        return []
    return seen


async def lily_load_voiceprints_by_players(
    supabase: SupabaseClient,
    player_names: list,
) -> list:
    """Candidate voiceprints for group-identity resolution step (b): every
    stored row whose player_name OR speaker_label matches one of this
    session's roster names (exact or simple case variants). Returns raw dicts
    [{group_id, player_name, speaker_label, speaker_identifiers}] for
    lily_memory.lily_match_group_by_voiceprints."""
    names = sorted({
        variant
        for n in player_names or []
        if str(n or "").strip()
        for variant in (
            str(n).strip(),
            str(n).strip().lower(),
            str(n).strip().capitalize(),
            str(n).strip().title(),
        )
    })
    if not names:
        return []
    try:
        by_name = await asyncio.to_thread(
            lambda: supabase.table("lily_speaker_voiceprints")
            .select("group_id, player_name, speaker_label, speaker_identifiers")
            .in_("player_name", names)
            .execute()
        )
        by_label = await asyncio.to_thread(
            lambda: supabase.table("lily_speaker_voiceprints")
            .select("group_id, player_name, speaker_label, speaker_identifiers")
            .in_("speaker_label", names)
            .execute()
        )
        merged: dict[tuple[str, str, str], dict] = {}
        for row in (by_name.data or []) + (by_label.data or []):
            if not isinstance(row, dict):
                continue
            key = (
                str(row.get("group_id") or ""),
                str(row.get("speaker_label") or ""),
                str(row.get("player_name") or ""),
            )
            merged[key] = row
        return list(merged.values())
    except Exception as e:
        logger.error("lily_load_voiceprints_by_players error: %s", e)
        return []


# ---------------------------------------------------------------------------
# Durable voice identity (WO-LILY-VOICE-IDENTITY-001) — device-independent
# recognition by our OWN speaker embedding, since Speechmatics refreshes its
# blobs per session. The centroid is a running mean of a person's enrolled
# unit embeddings; matching (cosine + margin) lives in lily_voice_identity.
# Every function is defensive — a voice-lane failure never breaks a session,
# and the whole feature no-ops until the lily_voice_identity table exists.
# ---------------------------------------------------------------------------

VOICE_IDENTITY_TABLE = "lily_voice_identity"


def _parse_vector(value):
    """pgvector round-trips as a '[0.1,0.2,...]' string via supabase-py (and
    as a list from a fake/local client). Return a list[float] or None."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        try:
            return [float(x) for x in value]
        except (TypeError, ValueError):
            return None
    if isinstance(value, str):
        try:
            import json
            parsed = json.loads(value)
            return [float(x) for x in parsed] if isinstance(parsed, list) else None
        except (ValueError, TypeError):
            return None
    return None


async def lily_load_voice_identities(
    supabase: SupabaseClient, model_tag: str
) -> list:
    """Active per-person centroids for THIS embedding space (model_tag), for
    the start-of-session match. Returns [{group_id, centroid: list[float],
    sample_count}]; [] on any failure or when the table is absent."""
    if supabase is None or not model_tag:
        return []
    try:
        rows = await asyncio.to_thread(
            lambda: supabase.table(VOICE_IDENTITY_TABLE)
            .select("group_id, centroid, sample_count")
            .eq("model_tag", model_tag)
            .eq("status", "active")
            .execute()
        )
        out = []
        for row in (rows.data or []):
            centroid = _parse_vector(row.get("centroid"))
            gid = row.get("group_id")
            if gid and centroid:
                out.append({
                    "group_id": gid,
                    "centroid": centroid,
                    "sample_count": int(row.get("sample_count") or 1),
                })
        return out
    except Exception as e:
        logger.warning("lily_load_voice_identities error: %s", e)
        return []


async def lily_upsert_voice_identity(
    supabase: SupabaseClient,
    *,
    group_id: str,
    centroid: list,
    sample_count: int,
    model_tag: str,
) -> bool:
    """Store (or refresh) a person's centroid for this embedding space.
    Select-then-update-or-insert on (group_id, model_tag) so it works
    without relying on a server-side upsert conflict target. Returns True on
    a committed write, False on any failure. Never raises."""
    if supabase is None or not group_id or not centroid or not model_tag:
        return False
    try:
        existing = await asyncio.to_thread(
            lambda: supabase.table(VOICE_IDENTITY_TABLE)
            .select("id")
            .eq("group_id", group_id)
            .eq("model_tag", model_tag)
            .limit(1)
            .execute()
        )
        payload = {
            "group_id": group_id,
            "centroid": list(centroid),
            "sample_count": int(sample_count),
            "model_tag": model_tag,
            "status": "active",
            # updated_at has a DEFAULT but no trigger and was never written
            # on update, so it froze at insert and reported a voiceprint as
            # untouched for days while its sample_count climbed 4 -> 7. A
            # freshness column that only ever shows creation time is worse
            # than no column: it reads as "enrollment has stopped".
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if existing.data:
            row_id = existing.data[0]["id"]
            await asyncio.to_thread(
                lambda: supabase.table(VOICE_IDENTITY_TABLE)
                .update(payload)
                .eq("id", row_id)
                .execute()
            )
            logger.info(
                "LILY_VOICE_ID | CENTROID_UPDATED | group=%s tag=%s n=%d",
                group_id, model_tag, sample_count,
            )
        else:
            await asyncio.to_thread(
                lambda: supabase.table(VOICE_IDENTITY_TABLE)
                .insert(payload)
                .execute()
            )
            logger.info(
                "LILY_VOICE_ID | CENTROID_ENROLLED | group=%s tag=%s",
                group_id, model_tag,
            )
        return True
    except Exception as e:
        logger.warning(
            "lily_upsert_voice_identity error (group=%s): %s", group_id, e
        )
        return False


async def lily_retire_voice_identity(
    supabase: SupabaseClient, group_id: str
) -> bool:
    """Retire (never delete) every voice-identity row for a group — the
    forget_me cascade's biometric arm, so a durable voiceprint stops
    matching the moment someone asks to be forgotten. Retire preserves
    provenance; matching only ever reads status='active'."""
    if supabase is None or not group_id:
        return False
    try:
        await asyncio.to_thread(
            lambda: supabase.table(VOICE_IDENTITY_TABLE)
            .update({"status": "retired"})
            .eq("group_id", group_id)
            .execute()
        )
        logger.info("LILY_VOICE_ID | RETIRED | group=%s", group_id)
        return True
    except Exception as e:
        logger.warning(
            "lily_retire_voice_identity error (group=%s): %s", group_id, e
        )
        return False


# ---------------------------------------------------------------------------
# WS-8 identity reconciliation — operator speaker merge (one transaction
# across roster/voiceprints + retro-attribution of the merged label's prior
# utterances). The scorekeeper side is LilyGame.merge_speakers; this is the
# durable side.
# ---------------------------------------------------------------------------

async def lily_dedupe_group_voiceprints(
    supabase: SupabaseClient,
    group_id: str,
    player_name: str,
) -> int:
    """Collapse a group's voiceprint rows for one player down to a single
    row (WS-8 deliverable: one row per player per group). The freshest row
    with usable identifiers wins; the rest are deleted by id. Returns the
    number of rows deleted (0 = already deduped or nothing to do).

    Deletion is row-scoped and id-keyed — never a bare DELETE. The winner
    is chosen by (has-identifiers, updated_at) so a stale duplicate can
    never overwrite a good enrollment.

    Idempotent: re-running re-selects the survivors and deletes whatever
    losers remain, so a dedupe interrupted mid-loop heals on the next run.
    Raises on a PostgREST failure so the caller can surface a re-runnable
    partial merge (the previous swallow-to-0 hid failure as a no-op)."""
    if supabase is None or not group_id or not player_name:
        return 0
    res = await asyncio.to_thread(
        lambda: supabase.table("lily_speaker_voiceprints")
        .select("id, speaker_label, player_name, speaker_identifiers, updated_at")
        .eq("group_id", group_id)
        .eq("player_name", player_name)
        .execute()
    )
    rows = [r for r in (res.data or []) if isinstance(r, dict)]
    if len(rows) <= 1:
        return 0

    def _rank(row: dict):
        has_ids = 1 if row.get("speaker_identifiers") else 0
        return (has_ids, str(row.get("updated_at") or ""))

    winner = max(rows, key=_rank)
    loser_ids = [
        r.get("id") for r in rows
        if r.get("id") is not None and r.get("id") != winner.get("id")
    ]
    deleted = 0
    for lid in loser_ids:
        await asyncio.to_thread(
            lambda lid=lid: supabase.table("lily_speaker_voiceprints")
            .delete().eq("id", lid).execute()
        )
        deleted += 1
    logger.info(
        "LILY_MERGE | VOICEPRINT_DEDUPE | group=%s player=%s kept=%s "
        "deleted=%d",
        group_id, player_name, winner.get("id"), deleted,
    )
    return deleted


async def lily_merge_speaker(
    supabase: SupabaseClient,
    session_id: str,
    group_id: Optional[str],
    from_label: str,
    into_player: str,
) -> dict:
    """Durable side of an operator speaker merge — retro-attribute the
    merged label's prior utterances and dedupe voiceprints.

    NOT a true DB transaction: PostgREST has no cross-table transaction, so
    these are three independent writes. Each leg is INDIVIDUALLY IDEMPOTENT
    (every write is `SET col=into WHERE speaker_label=label`, or a
    re-select-and-delete dedupe), so a mid-sequence failure half-applies but
    re-running the WHOLE merge heals it — running it twice yields the same
    end state as running it once. On any leg failure we log
    `LILY_MERGE | PARTIAL | safe_to_rerun=true` so the operator can simply
    re-fire. (True atomicity needs a Postgres stored-proc — filed as a Doc
    DDL request in the WS-8 report.)

    1. lily_transcripts.speaker_name  ← into_player   (this session, label)
    2. lily_addressee_log.player_name ← into_player   (this session, label)
    3. lily_speaker_voiceprints.player_name ← into_player (group, label),
       then collapse the group's rows for that player to one.

    Returns a per-leg summary. lily_answers has no speaker_label column
    (it is already keyed on player_name), so it needs no retro pass — the
    scorekeeper merge makes subsequent awards land under the right name."""
    label = (from_label or "").strip()
    into = (into_player or "").strip()
    summary = {
        "transcripts_updated": None,
        "addressee_updated": None,
        "voiceprint_relabeled": None,
        "voiceprints_deduped": 0,
        "dedupe_failed": False,
        "partial": False,
    }
    if supabase is None or not label or not into:
        return summary
    # 1. transcripts (speaker_name is the attributed-player column).
    try:
        await asyncio.to_thread(
            lambda: supabase.table("lily_transcripts")
            .update({"speaker_name": into})
            .eq("session_id", session_id)
            .eq("speaker_label", label)
            .execute()
        )
        summary["transcripts_updated"] = True
    except Exception as e:
        summary["transcripts_updated"] = False
        logger.error(
            "LILY_MERGE | TRANSCRIPT_RETRO_FAILED | session=%s label=%s error=%s",
            session_id, label, e,
        )
    # 2. addressee log.
    try:
        await asyncio.to_thread(
            lambda: supabase.table("lily_addressee_log")
            .update({"player_name": into})
            .eq("session_id", session_id)
            .eq("speaker_label", label)
            .execute()
        )
        summary["addressee_updated"] = True
    except Exception as e:
        summary["addressee_updated"] = False
        logger.error(
            "LILY_MERGE | ADDRESSEE_RETRO_FAILED | session=%s label=%s error=%s",
            session_id, label, e,
        )
    # 3. voiceprints — relabel then dedupe (group-scoped).
    if group_id:
        try:
            await asyncio.to_thread(
                lambda: supabase.table("lily_speaker_voiceprints")
                .update({"player_name": into})
                .eq("group_id", group_id)
                .eq("speaker_label", label)
                .execute()
            )
            summary["voiceprint_relabeled"] = True
        except Exception as e:
            summary["voiceprint_relabeled"] = False
            logger.error(
                "LILY_MERGE | VOICEPRINT_RELABEL_FAILED | group=%s label=%s error=%s",
                group_id, label, e,
            )
        try:
            summary["voiceprints_deduped"] = await lily_dedupe_group_voiceprints(
                supabase, group_id, into
            )
        except Exception as e:
            summary["dedupe_failed"] = True
            logger.error(
                "LILY_MERGE | VOICEPRINT_DEDUPE_FAILED | group=%s player=%s error=%s",
                group_id, into, e,
            )
    # A leg that was ATTEMPTED and failed (explicit False) or a failed
    # dedupe means the merge half-applied. Every leg is idempotent, so the
    # operator-facing remedy is a plain re-fire — surface it loudly.
    summary["partial"] = (
        summary["transcripts_updated"] is False
        or summary["addressee_updated"] is False
        or summary["voiceprint_relabeled"] is False
        or summary["dedupe_failed"]
    )
    if summary["partial"]:
        logger.warning(
            "LILY_MERGE | PARTIAL | safe_to_rerun=true session=%s group=%s "
            "from_label=%s into=%s summary=%s — re-fire the merge to heal",
            session_id, group_id, label, into, summary,
        )
    logger.info(
        "LILY_MERGE | DONE | session=%s group=%s from_label=%s into=%s summary=%s",
        session_id, group_id, label, into, summary,
    )
    return summary


async def lily_rekey_speaker_voiceprints(
    supabase: SupabaseClient,
    old_group_id: str,
    new_group_id: str,
    session_id: str,
) -> None:
    """Move provisional voiceprint rows onto a resolved group id without
    colliding on ``(group_id, speaker_label)``.

    A blind UPDATE fails when the resolved group already has the same
    labels (returning tables / name-set hash upgrades onto an existing
    ``grp_*`` — RM_qs6YeUdkV7or logged
    ``REKEY_FAILED ... duplicate key ... S1 already exists``). For each
    provisional row: merge identifiers into the existing resolved row when
    the label is already taken, otherwise retarget the provisional row.
    Provisional rows are deleted after a successful merge. Tolerates
    failure independently like the other rekey helpers.
    """
    if not old_group_id or not new_group_id or old_group_id == new_group_id:
        return
    try:
        old_res = await asyncio.to_thread(
            lambda: supabase.table("lily_speaker_voiceprints")
            .select(
                "id, speaker_label, player_name, speaker_identifiers, "
                "sample_count, updated_at"
            )
            .eq("group_id", old_group_id)
            .execute()
        )
        old_rows = [
            r for r in (old_res.data or []) if isinstance(r, dict)
        ]
        if not old_rows:
            return
        moved = 0
        merged = 0
        for row in old_rows:
            label = row.get("speaker_label")
            if not label:
                continue
            existing_res = await asyncio.to_thread(
                lambda label=label: supabase.table("lily_speaker_voiceprints")
                .select(
                    "id, speaker_label, player_name, speaker_identifiers, "
                    "sample_count"
                )
                .eq("group_id", new_group_id)
                .eq("speaker_label", label)
                .limit(1)
                .execute()
            )
            existing_rows = [
                r for r in (existing_res.data or []) if isinstance(r, dict)
            ]
            if existing_rows:
                existing = existing_rows[0]
                old_ids = list(row.get("speaker_identifiers") or [])
                new_ids = list(existing.get("speaker_identifiers") or [])
                # Preserve order, drop duplicates.
                seen = set()
                merged_ids = []
                for ident in new_ids + old_ids:
                    if ident in seen or ident is None:
                        continue
                    seen.add(ident)
                    merged_ids.append(ident)
                patch = {
                    "speaker_identifiers": merged_ids,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
                # Prefer a real player_name when the provisional row has one
                # and the resolved row does not.
                if row.get("player_name") and not existing.get("player_name"):
                    patch["player_name"] = row["player_name"]
                old_samples = int(row.get("sample_count") or 0)
                new_samples = int(existing.get("sample_count") or 0)
                if old_samples or new_samples:
                    patch["sample_count"] = max(old_samples, new_samples)
                existing_id = existing.get("id")
                if existing_id is not None:
                    await asyncio.to_thread(
                        lambda existing_id=existing_id, patch=patch: (
                            supabase.table("lily_speaker_voiceprints")
                            .update(patch)
                            .eq("id", existing_id)
                            .execute()
                        )
                    )
                old_id = row.get("id")
                if old_id is not None:
                    await asyncio.to_thread(
                        lambda old_id=old_id: (
                            supabase.table("lily_speaker_voiceprints")
                            .delete()
                            .eq("id", old_id)
                            .execute()
                        )
                    )
                merged += 1
            else:
                old_id = row.get("id")
                if old_id is not None:
                    await asyncio.to_thread(
                        lambda old_id=old_id: (
                            supabase.table("lily_speaker_voiceprints")
                            .update({"group_id": new_group_id})
                            .eq("id", old_id)
                            .execute()
                        )
                    )
                else:
                    # Id-less fakes / legacy rows: best-effort retarget by
                    # the provisional group + label.
                    await asyncio.to_thread(
                        lambda label=label: (
                            supabase.table("lily_speaker_voiceprints")
                            .update({"group_id": new_group_id})
                            .eq("group_id", old_group_id)
                            .eq("speaker_label", label)
                            .execute()
                        )
                    )
                moved += 1
        logger.info(
            "LILY_MEMORY | REKEY | table=lily_speaker_voiceprints "
            "session=%s old=%s new=%s moved=%d merged=%d",
            session_id, old_group_id, new_group_id, moved, merged,
        )
    except Exception as e:
        logger.error(
            "LILY_MEMORY | REKEY_FAILED | table=lily_speaker_voiceprints "
            "session=%s error=%s",
            session_id, e,
        )


async def lily_rekey_group(
    supabase: SupabaseClient,
    old_group_id: str,
    new_group_id: str,
    session_id: str,
) -> None:
    """Mid-session group-id upgrade: move rows written under the provisional
    id to the RESOLVED id. Scoped conservatively — lily_sessions and
    lily_group_facts by THIS session; lily_speaker_voiceprints only when the
    provisional id was weak (the room-random session id or deterministic
    name-set hash), never on arbitrary strong->strong merges. Each table
    update tolerates failure independently."""
    if not old_group_id or not new_group_id or old_group_id == new_group_id:
        return
    updates = [
        ("lily_sessions",
         lambda: supabase.table("lily_sessions")
         .update({"group_id": new_group_id}).eq("session_id", session_id).execute()),
        ("lily_group_facts",
         lambda: supabase.table("lily_group_facts")
         .update({"group_id": new_group_id})
         .eq("source_session_id", session_id).execute()),
        # Asked history (migration 010): this session's served-question
        # rows follow the resolved id so the no-repeat guard holds on
        # the group's next visit.
        ("lily_asked_history",
         lambda: supabase.table("lily_asked_history")
         .update({"group_id": new_group_id})
         .eq("session_id", session_id).execute()),
    ]
    is_name_set_hash = (
        isinstance(old_group_id, str)
        and old_group_id.startswith(lily_memory.NAME_SET_GROUP_PREFIX)
        and len(old_group_id) == len(lily_memory.NAME_SET_GROUP_PREFIX) + 40
        and all(ch in "0123456789abcdef" for ch in old_group_id[-40:].lower())
    )
    for table, call in updates:
        try:
            await asyncio.to_thread(call)
            logger.info(
                "LILY_MEMORY | REKEY | table=%s session=%s old=%s new=%s",
                table, session_id, old_group_id, new_group_id,
            )
        except Exception as e:
            logger.error(
                "LILY_MEMORY | REKEY_FAILED | table=%s session=%s error=%s",
                table, session_id, e,
            )
    if old_group_id == session_id or is_name_set_hash:
        await lily_rekey_speaker_voiceprints(
            supabase, old_group_id, new_group_id, session_id
        )
    # lily_group_prefs (group prefs WO): PK-safe merge re-key — its own
    # helper because a blind UPDATE collides with an existing prefs row
    # under the resolved id. Tolerates failure independently like the rest.
    await lily_rekey_group_prefs(
        supabase, old_group_id, new_group_id, session_id
    )


# ---------------------------------------------------------------------------
# The right to be forgotten (WO-LILY-FORGETME-001 Task 1)
# ---------------------------------------------------------------------------

# Hard cap on the whole cascade (deletes + verification counts). Deletion
# is NOT fire-and-forget: it must complete and be VERIFIED before the tool
# acknowledges — but it must also never hang a live table.
FORGET_CASCADE_TIMEOUT_SECONDS = 20.0


async def lily_forget_group_data(
    supabase: SupabaseClient,
    group_id: str,
    session_id: str,
) -> dict:
    """The delete cascade for one group — awaited, verified, time-capped.

    HARD-DELETES all group rows from lily_speaker_voiceprints,
    lily_memories, lily_group_facts; deletes every session-keyed transcript,
    answer, addressee, acoustic, report, and image-attempt row via the
    group's session ids (lily_sessions where group_id = X, plus the CURRENT
    session id); deletes lily_group_prefs (group prefs WO interlock —
    preferences are recognition data) and lily_asked_history IF the tables
    exist (absent-table errors — migration lag / future WO — are skipped,
    never failed);
    then re-keys lily_sessions to the `forgotten_<sha1-12>` tombstone so
    operational records survive without linkable identity.

    Every table is VERIFIED with a count query after its delete/re-key
    (remaining rows under the old key must be 0). All DB calls run via
    asyncio.to_thread; the whole cascade is capped at
    FORGET_CASCADE_TIMEOUT_SECONDS. Returns an honest result dict:
    {ok, tombstone, deleted {table: n}, rekeyed {table: n},
    skipped [table], failed {table: reason}, verified [table], timed_out}
    — ok=True only when nothing failed and everything verified. The
    caller's message layer names the succeeded/failed tables so Lily can
    report honestly on partial failure."""
    result: dict = {
        "ok": False,
        "tombstone": lily_forget.lily_tombstone_group_id(group_id),
        "deleted": {},
        "rekeyed": {},
        "skipped": [],
        "failed": {},
        "verified": [],
        "timed_out": False,
    }
    if supabase is None or not group_id:
        result["failed"]["_client"] = "no supabase client or empty group id"
        return result

    def _session_ids():
        ids = []
        # Include the tombstone for retries after an older/partial cascade
        # re-keyed sessions before all session-keyed deletes completed.
        for candidate in (group_id, lily_forget.lily_tombstone_group_id(group_id)):
            res = (
                supabase.table("lily_sessions")
                .select("session_id")
                .eq("group_id", candidate)
                .execute()
            )
            ids.extend(
                r["session_id"] for r in (res.data or []) if r.get("session_id")
            )
        return ids

    def _apply(op):
        table = supabase.table(op["table"])
        if op["action"] == "rekey":
            q = table.update({"group_id": op["new_group_id"]})
        else:
            q = table.delete()
        if len(op["values"]) == 1:
            q = q.eq(op["column"], op["values"][0])
        else:
            q = q.in_(op["column"], op["values"])
        return q.execute()

    def _count_remaining(op):
        q = supabase.table(op["table"]).select(op["column"], count="exact")
        if len(op["values"]) == 1:
            q = q.eq(op["column"], op["values"][0])
        else:
            q = q.in_(op["column"], op["values"])
        return q.limit(1).execute()

    async def _run() -> None:
        try:
            ids = await asyncio.to_thread(_session_ids)
        except Exception as e:
            ids = []
            result["failed"]["lily_sessions:session_scan"] = (
                f"{type(e).__name__}: {str(e)[:200]}"
            )
        session_ids = sorted(set(ids) | {session_id})
        plan = lily_forget.lily_build_forget_plan(group_id, session_ids)
        for op in plan:
            if not op["values"]:
                continue
            # Never move the session lookup key after a required deletion or
            # verification failed. Keeping it on the original group makes a
            # retry able to rediscover every historical session.
            if op["action"] == "rekey" and result["failed"]:
                logger.warning(
                    "LILY_FORGET | REKEY_DEFERRED | table=%s failures=%s",
                    op["table"], ",".join(sorted(result["failed"])),
                )
                continue
            try:
                res = await asyncio.to_thread(_apply, op)
                rows = len(getattr(res, "data", None) or [])
                bucket = "rekeyed" if op["action"] == "rekey" else "deleted"
                result[bucket][op["table"]] = rows
            except Exception as e:
                msg = str(e)
                if op.get("optional") and lily_forget.lily_is_absent_table_error(msg):
                    result["skipped"].append(op["table"])
                    logger.info(
                        "LILY_FORGET | SKIPPED | table=%s (not applied yet): %s",
                        op["table"], msg[:120],
                    )
                    continue
                result["failed"][op["table"]] = f"{type(e).__name__}: {msg[:200]}"
                continue
            # Verification — count queries, not trust: zero rows may remain
            # under the old key.
            try:
                chk = await asyncio.to_thread(_count_remaining, op)
                remaining = getattr(chk, "count", None)
                if remaining == 0:
                    result["verified"].append(op["table"])
                else:
                    result["failed"][op["table"]] = (
                        f"verify: {remaining} row(s) still keyed to the old group"
                    )
            except Exception as e:
                result["failed"][op["table"]] = (
                    f"verify failed: {type(e).__name__}: {str(e)[:120]}"
                )

    try:
        await asyncio.wait_for(_run(), timeout=FORGET_CASCADE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        result["timed_out"] = True
        result["failed"]["_timeout"] = (
            f"cascade exceeded {FORGET_CASCADE_TIMEOUT_SECONDS:.0f}s — tables "
            "not listed as verified are unconfirmed"
        )
    result["ok"] = not result["failed"]
    logger.info(
        "LILY_FORGET | group=%s | session=%s | ok=%s | tombstone=%s | "
        "tables=%s | rows=%s | skipped=%s | failed=%s",
        group_id, session_id, result["ok"], result["tombstone"],
        ",".join(result["verified"]) or "-",
        json.dumps({**result["deleted"], **result["rekeyed"]}),
        ",".join(result["skipped"]) or "-",
        json.dumps(result["failed"]) if result["failed"] else "-",
    )
    return result


async def lily_count_group_memory(
    supabase: SupabaseClient,
    group_id: str,
) -> Optional[dict]:
    """Read-only counts for the lily_explain_memory tool — counts only,
    never raw contents, no new storage. Returns
    {voiceprints, games, last_played_at, facts} for the group, or None on
    any error (the tool's message layer reports the honest can't-check
    shape rather than guessing)."""
    if supabase is None or not group_id:
        return None
    try:
        vp, mem, facts = await asyncio.gather(
            asyncio.to_thread(
                lambda: supabase.table("lily_speaker_voiceprints")
                .select("group_id", count="exact")
                .eq("group_id", group_id)
                .limit(1)
                .execute()
            ),
            asyncio.to_thread(
                lambda: supabase.table("lily_memories")
                .select("played_at", count="exact")
                .eq("group_id", group_id)
                .order("played_at", desc=True)
                .limit(1)
                .execute()
            ),
            asyncio.to_thread(
                lambda: supabase.table("lily_group_facts")
                .select("group_id", count="exact")
                .eq("group_id", group_id)
                .limit(1)
                .execute()
            ),
        )
        last_played = None
        if mem.data:
            last_played = (mem.data[0] or {}).get("played_at")
        return {
            "voiceprints": getattr(vp, "count", None) or 0,
            "games": getattr(mem, "count", None) or 0,
            "last_played_at": last_played,
            "facts": getattr(facts, "count", None) or 0,
        }
    except Exception as e:
        logger.error("lily_count_group_memory error: %s", e)
        return None


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
