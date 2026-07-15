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
        # to_thread: the postgrest client is synchronous — running it inline
        # blocks the event loop (and therefore the live audio pipeline) for a
        # full cross-region HTTP round trip on every score change.
        await asyncio.to_thread(
            lambda: supabase.table("lily_sessions")
            .upsert(payload, on_conflict="session_id")
            .execute()
        )
    except Exception as e:
        logger.error("lily_checkpoint error: %s", e)


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
        extra = {}
        if metadata_provider is not None:
            try:
                extra["metadata"] = metadata_provider()
            except Exception as e:
                logger.debug("heartbeat metadata_provider failed: %s", e)
        await lily_checkpoint(supabase, scorekeeper, **extra)
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
# Addressee-label corpus (B1 training-data flywheel)
# ---------------------------------------------------------------------------

async def lily_log_addressee(
    supabase: SupabaseClient,
    row: dict,
) -> Optional[int]:
    """Fire-and-forget insert into lily_addressee_log. Returns the new row
    id when available (kept by the caller for the later label UPDATE).
    Tolerates failures silently — corpus logging must never surface into
    the live session (debug log only)."""
    try:
        result = await asyncio.to_thread(
            lambda: supabase.table("lily_addressee_log").insert(row).execute()
        )
        data = getattr(result, "data", None) or []
        if data and isinstance(data[0], dict):
            return data[0].get("id")
        return None
    except Exception as e:
        logger.debug("lily_log_addressee error: %s", e)
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
            # Burn protocol (say-gate WO, migration 009): only status='active'
            # rows are servable — 'burned' (answer leaked on air) and any
            # future lifecycle value (e.g. the tier-retirement sub-agent's
            # 'retired') are excluded. A missing column (pre-009 schema)
            # reads as active.
            and (r.get("status") or "active") == "active"
        ]
        if not candidates:
            return None
        preferred = [
            r for r in candidates
            if r.get("category") == category
            and r.get("difficulty_tier") == difficulty_tier
        ] or [r for r in candidates if r.get("category") == category] or candidates
        row = preferred[0]
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
                "(plugin drift vs livekit-plugins-speechmatics 1.6.4)", trigger
            )
            return False
        # get_speaker_ids() is ASYNC at 1.6.4 and needs ~5 spoken words per
        # speaker before it returns useful identifiers — this task stays in
        # the background and tolerates (but LOGS) empty results. Awaitable
        # check kept defensive in case a plugin bump makes it synchronous.
        speaker_ids = get_ids()
        if asyncio.iscoroutine(speaker_ids) or isinstance(speaker_ids, asyncio.Future):
            speaker_ids = await speaker_ids
        if not speaker_ids:
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
                "group_id": gid,
                "speaker_label": label,
                # Speechmatics may already return the enrolled player name as
                # the label on a rematch (known_speakers are injected with
                # player-name labels) — keep the roster mapping as fallback.
                "player_name": label_to_name.get(label)
                or (label if label in scorekeeper.players else None),
                "speaker_identifiers": identifiers,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
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


async def lily_load_voiceprints_by_players(
    supabase: SupabaseClient,
    player_names: list,
) -> list:
    """Candidate voiceprints for group-identity resolution step (b): every
    stored row whose player_name matches one of this session's roster names
    (exact or lowercase spelling). Returns raw dicts
    [{group_id, player_name, speaker_label, speaker_identifiers}] for
    lily_memory.lily_match_group_by_voiceprints."""
    names = sorted({
        variant
        for n in player_names or []
        if str(n or "").strip()
        for variant in (str(n).strip(), str(n).strip().lower(),
                        str(n).strip().capitalize())
    })
    if not names:
        return []
    try:
        result = await asyncio.to_thread(
            lambda: supabase.table("lily_speaker_voiceprints")
            .select("group_id, player_name, speaker_label, speaker_identifiers")
            .in_("player_name", names)
            .execute()
        )
        return result.data or []
    except Exception as e:
        logger.error("lily_load_voiceprints_by_players error: %s", e)
        return []


async def lily_rekey_group(
    supabase: SupabaseClient,
    old_group_id: str,
    new_group_id: str,
    session_id: str,
) -> None:
    """Mid-session group-id upgrade: move rows written under the provisional
    id to the RESOLVED id. Scoped conservatively — lily_sessions and
    lily_group_facts by THIS session; lily_speaker_voiceprints only when the
    provisional id was the room-random session id (never merges two real
    groups). Each table update tolerates failure independently."""
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
    ]
    if old_group_id == session_id:
        updates.append(
            ("lily_speaker_voiceprints",
             lambda: supabase.table("lily_speaker_voiceprints")
             .update({"group_id": new_group_id})
             .eq("group_id", old_group_id).execute())
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
