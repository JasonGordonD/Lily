"""
lily_bank_replenish.py — the BACKGROUND question author (WO-LILY-SUPPLY-001
S2, "bank-first question supply").

Operator ruling, 2026-09-06, verbatim: "the bank serves, the author
replenishes. Live question authoring never sits on the delivery path
again."

THE EVIDENCE. grok-4.5 authoring takes 20-39 seconds to its first content
token per call — measured, per call, in lily_llm_usage since
WO-LILY-STREAMING-REASONING-001 put the transport on SSE. Verification is
3-7s. The bank draw is a single indexed SELECT. For as long as authoring
sat inside prefetch, every question the bank could not supply cost a table
half a minute of Lily talking around a hole. This module moves the author
OFF that path and leaves nothing but the draw on it.

WHAT THIS IS, STRUCTURALLY: the picture arsenal's replenishment pattern
(WO-LILY-ARSENAL-SEED-001), applied to text. Same watermark ratio, same
per-partition-independent tracking, same "generate in the background,
serve only from what is already ready", same run receipt, same
moderation-is-an-expected-outcome accounting. Where the arsenal says
`partition` this module says `lane`; where the arsenal prices a run in
images it prices one in tokens. Nothing here is a new idea; the idea was
already load-bearing one shelf over.

  lily_arsenal.lily_should_replenish        -> lily_bank_should_replenish
  lily_arsenal.lily_replenish_threshold     -> reused verbatim
  lily_arsenal.lily_arsenal_is_duplicate    -> lily_bank_find_duplicate
  lily_arsenal.lily_arsenal_replenish       -> lily_replenish_lane
  lily_picture_arsenal_runs                 -> lily_bank_replenish_runs
  lily_arsenal_gen.lily_is_moderation_rejection / lily_is_unavailable
                                            -> reused verbatim

A LANE is one slot of the question rotation: `<deck>:<category>`, where
deck is general or adult and category is the round family that deck
rotates through (lily_agent.CATEGORY_FAMILIES /
lily_agent.ADULT_CATEGORY_FAMILIES). The deck IS the register for the text
bank — unlike pictures, which split adult into suggestive/explicit heat
partitions, an adult text row is just adult. Each lane carries its own
target depth and its own 40%-consumed watermark, evaluated independently,
so a run that refills academic can never mask adult_kink going dry.

THE DELIVERY-PATH RULE, stated as a contract because it is the whole
point: nothing in this module is ever awaited by the game. The in-session
entry point is one guarded `lily_spawn` in lily_agent's entrypoint that
starts a detached supervisor; the supervisor sleeps, sweeps, and dies
quietly on cancellation. Every failure inside is swallowed and counted.
An author that hangs for a minute costs the bank one slow sweep and the
table nothing at all.

WRITES LAND AT status='ready' (never 'active'). Migration 029 documents
the seam with S1: S1's draw accepts 'ready' or 'active'; this module is
the only writer of 'ready' and writes no other status.

Stdlib + the injected supabase client + the injected author/verify
callables. Everything external arrives as a parameter, which is what lets
the whole pipeline be tested against fakes on a host with no provider
credentials — the same discipline lily_arsenal_gen ships.
"""

import asyncio
import datetime
import hashlib
import logging
import time
from typing import Callable, Optional

import lily_arsenal
import lily_arsenal_gen
import lily_bank
import lily_config

logger = logging.getLogger("lily_bank_replenish")

QUESTIONS_TABLE = "lily_questions"
RUNS_TABLE = "lily_bank_replenish_runs"
USAGE_TABLE = "lily_llm_usage"

# The status a replenished row lands at. NOT 'active': 'active' is the
# 448-row standing bank's own value and this module never touches it.
STATUS_READY = "ready"
STATUS_ACTIVE = "active"
# Both are servable to S1's draw; both count toward a lane's depth.
SERVABLE_STATUSES = (STATUS_READY, STATUS_ACTIVE)

# The tag every authoring/verification call this module makes carries into
# lily_llm_usage, so a run's token cost is a SELECT rather than an estimate.
USAGE_PURPOSE = "bank_replenish"

# A run whose heartbeat has been silent this long is presumed dead and its
# row can be reclaimed (lily_arsenal.RUN_STALE_AFTER_SECONDS, same reason:
# comfortably longer than the slowest plausible single authoring call).
RUN_STALE_AFTER_SECONDS = lily_arsenal.RUN_STALE_AFTER_SECONDS

DECK_GENERAL = "general"
DECK_ADULT = "adult"

# ---------------------------------------------------------------------------
# The lane table.
#
# SOURCE OF TRUTH: lily_agent.CATEGORY_FAMILIES (lily_agent.py:147) and
# lily_agent.ADULT_CATEGORY_FAMILIES (lily_agent.py:152) — the rotations
# _category_for_round actually serves from (lily_agent.py:3031-3044).
# They are RESTATED here rather than imported because lily_agent imports
# this module (the entrypoint hook), and importing it back would close a
# cycle. tests/test_supply_001_s2_lanes.py asserts the two agree, so a
# family added to the rotation without a lane reads RED instead of
# silently getting no supply.
# ---------------------------------------------------------------------------

GENERAL_FAMILIES = ("academic", "pop culture", "wordplay", "lifestyle-potpourri")
ADULT_FAMILIES = ("adult_couples", "adult_kink")


def lily_lane_id(deck: str, category: str) -> str:
    """`general` + `academic` -> `general:academic`. The lane id is the
    watermark's key, the receipt's key, and the value written to
    lily_questions.lane."""
    return f"{str(deck or '').strip().lower()}:{str(category or '').strip()}"


def lily_parse_lane(lane: str) -> tuple:
    """`general:academic` -> ('general', 'academic'). Returns ('', '') for
    anything that is not a lane id — callers treat that as "no lane"."""
    raw = str(lane or "")
    if ":" not in raw:
        return "", ""
    deck, _, category = raw.partition(":")
    deck = deck.strip().lower()
    category = category.strip()
    if deck not in (DECK_GENERAL, DECK_ADULT) or not category:
        return "", ""
    return deck, category


LANES = tuple(
    [lily_lane_id(DECK_GENERAL, c) for c in GENERAL_FAMILIES]
    + [lily_lane_id(DECK_ADULT, c) for c in ADULT_FAMILIES]
)


def lily_lane_is_adult(lane: str) -> bool:
    return lily_parse_lane(lane)[0] == DECK_ADULT


def lily_lane_row_fields(lane: str) -> dict:
    """The (mode, adult, category) triple a row in this lane carries.

    Matches what the standing bank already holds, measured 2026-09-06:
    general rows are mode='general' adult=false; the migration-014 adult
    families are mode='adult' adult=true. lily_fetch_bank_question filters
    on `adult` and `category`, so those two are the load-bearing ones;
    `mode` rides along for the operator's SQL and for
    lily_memory.lily_bank_mode_filter."""
    deck, category = lily_parse_lane(lane)
    if not deck:
        return {}
    adult = deck == DECK_ADULT
    return {
        "mode": DECK_ADULT if adult else DECK_GENERAL,
        "adult": adult,
        "category": category,
    }


# ---------------------------------------------------------------------------
# Counts and the watermark — the arsenal's rule, per lane.
# ---------------------------------------------------------------------------


async def lily_lane_depth(supabase, *, lane: str) -> dict:
    """{'ready', 'active', 'servable', 'burned'} for one lane.

    `servable` = ready + active, and it is what the watermark reads. A
    legacy row carries no `lane` value at all, so the count is taken on
    (adult, category) — the same predicate the draw uses — rather than on
    the new column, which would read every lane as empty on day one and
    fire a full-depth authoring run against a bank that is already stocked.

    Never raises: a count that cannot be taken returns zeros and logs, and
    a zero count cannot cause a spend because the caller's watermark is
    also gated on the run receipt's one-active-per-lane index."""
    out = {"ready": 0, "active": 0, "servable": 0, "burned": 0, "read_failed": False}
    fields = lily_lane_row_fields(lane)
    if supabase is None or not fields:
        out["read_failed"] = True
        return out
    try:
        rows = await asyncio.to_thread(
            lambda: supabase.table(QUESTIONS_TABLE)
            .select("id,status")
            .eq("adult", fields["adult"])
            .eq("category", fields["category"])
            .limit(5000)
            .execute()
        )
    except Exception as e:
        logger.warning(
            "LILY_BANK | DEPTH_FAILED | lane=%s error_class=%s error=%s",
            lane, type(e).__name__, e,
        )
        out["read_failed"] = True
        return out
    for r in rows.data or []:
        status = str((r or {}).get("status") or STATUS_ACTIVE)
        if status == STATUS_READY:
            out["ready"] += 1
        elif status == STATUS_ACTIVE:
            out["active"] += 1
        elif status == "burned":
            out["burned"] += 1
    out["servable"] = out["ready"] + out["active"]
    return out


def lily_bank_consumed_pct(servable: int, target: int) -> float:
    """How much of a lane's target depth is gone, 0.0-1.0. The number the
    WATERMARK receipt prints, so "why did that fire" is answerable from a
    log line instead of by re-deriving the arithmetic."""
    tgt = max(1, int(target or 1))
    gone = max(0, tgt - max(0, int(servable or 0)))
    return round(min(1.0, gone / tgt), 3)


def lily_bank_should_replenish(
    servable: int,
    *,
    lane: Optional[str] = None,
    target: Optional[int] = None,
    ratio: Optional[float] = None,
    consumed: Optional[int] = None,
) -> bool:
    """The watermark: fire when a lane is `ratio` CONSUMED — 40% by
    default, the arsenal's own number, tracked per lane independently.

    TWO ways to be 40% consumed, and they are the same two the arsenal
    carries (lily_arsenal.lily_should_replenish), transposed:

      shortfall against target  (target - servable >= threshold)
          the standing lane is short: rows burned, or the lane was never
          stocked to depth. This is the limb the out-of-session runner and
          the background sweep read, because neither has a session's
          serving history in front of it.

      serves this session        (consumed >= threshold)
          the caller counted rows actually drawn from this lane and the
          count crossed the ratio. Optional: `consumed=None` — the default
          — evaluates the shortfall limb alone.

    Threshold comes from lily_arsenal.lily_replenish_threshold, unchanged,
    so the two banks cannot drift apart on what "40% consumed" means."""
    tgt = lily_config.bank_target_depth(lane) if target is None else int(target)
    tgt = max(1, tgt)
    r = lily_config.bank_replenish_ratio() if ratio is None else float(ratio)
    threshold = lily_arsenal.lily_replenish_threshold(tgt, r)
    if consumed is not None and int(consumed) >= threshold:
        return True
    return (tgt - max(0, int(servable or 0))) >= threshold


async def lily_bank_watermark(
    supabase, *, lane: str, target: Optional[int] = None, consumed: Optional[int] = None
) -> dict:
    """Read one lane's depth and decide. Emits the S1 receipt:

        LILY_BANK | WATERMARK | lane= ready= target= consumed_pct=

    Consumer: lily_replenish_sweep (which replenishes exactly the lanes
    this says are below) and lily_bank_health (the operator's readout)."""
    tgt = max(1, lily_config.bank_target_depth(lane) if target is None else int(target))
    depth = await lily_lane_depth(supabase, lane=lane)
    servable = depth["servable"]
    pct = lily_bank_consumed_pct(servable, tgt)
    below = (
        False
        if depth.get("read_failed")
        else lily_bank_should_replenish(
            servable, lane=lane, target=tgt, consumed=consumed
        )
    )
    logger.info(
        "LILY_BANK | WATERMARK | lane=%s ready=%d target=%d consumed_pct=%.2f "
        "below=%s ready_new=%d active=%d burned=%d",
        lane, servable, tgt, pct, below,
        depth["ready"], depth["active"], depth["burned"],
    )
    return {
        "lane": lane,
        "ready": servable,
        "ready_new": depth["ready"],
        "active": depth["active"],
        "burned": depth["burned"],
        "target": tgt,
        "consumed_pct": pct,
        "below_watermark": below,
        "read_failed": bool(depth.get("read_failed")),
    }


# ---------------------------------------------------------------------------
# Dedup: exact sha256 of the normalized text + the ARSENAL-SEED A5
# similarity check against the lane's own rows.
# ---------------------------------------------------------------------------


def lily_bank_text_sha256(text) -> str:
    """sha256 hex of the NORMALIZED question text
    (lily_bank.lily_normalize_question_text — lowercased, punctuation
    stripped, whitespace collapsed), so two questions that differ only in
    punctuation or capitalisation hash identically.

    Deliberately not lily_bank.lily_question_text_hash, which is sha1 and
    is lily_asked_history's per-group serving key. Different digest,
    different column (question_text_sha256), different meaning."""
    return hashlib.sha256(
        lily_bank.lily_normalize_question_text(text).encode("utf-8")
    ).hexdigest()


async def lily_bank_find_duplicate(
    supabase, *, lane: str, question_text: str, ratio: Optional[float] = None
) -> Optional[dict]:
    """Near-duplicate check for a candidate question against the LANE's
    rows. Returns the matching row (with 'match': 'exact' | 'fuzzy') or
    None when the candidate is genuinely new.

    ARSENAL-SEED A5's check, applied to text: the exact normalized-hash
    match alone lets a bank fill with the same question in different
    words, which is exactly what an author asked for "one more academic
    question" forty times will produce. lily_bank.lily_find_duplicate is
    reused unchanged (exact hash any category + difflib ratio same
    category) so the replenisher, the curation gate and the arsenal all
    agree on what "the same question" means.

    FAIL CLOSED on a read failure: returning None on an unreadable bank
    would let the author fill it with duplicates precisely when the
    database is unhealthy. A failed check returns a sentinel row so the
    caller skips the candidate."""
    text = str(question_text or "").strip()
    fields = lily_lane_row_fields(lane)
    if supabase is None or not text or not fields:
        return {"match": "unchecked", "reason": "no client or no lane"}
    r = lily_config.bank_replenish_dup_ratio() if ratio is None else float(ratio)
    try:
        rows = await asyncio.to_thread(
            lambda: supabase.table(QUESTIONS_TABLE)
            .select("id,question,category,status")
            .eq("adult", fields["adult"])
            .eq("category", fields["category"])
            .limit(2000)
            .execute()
        )
    except Exception as e:
        logger.warning(
            "LILY_BANK | DUP_CHECK_FAILED | lane=%s error_class=%s error=%s",
            lane, type(e).__name__, e,
        )
        return {"match": "unchecked", "reason": f"{type(e).__name__}: {e}"}
    existing = [
        {
            "id": row.get("id"),
            "question": row.get("question"),
            "category": row.get("category") or fields["category"],
        }
        for row in (rows.data or [])
        if row.get("question")
    ]
    return lily_bank.lily_find_duplicate(
        text, fields["category"], existing, ratio=r
    )


# ---------------------------------------------------------------------------
# The write: one verified, deduped question -> one status='ready' row.
# ---------------------------------------------------------------------------


def lily_ready_row(lane: str, question: dict, *, run_id: Optional[str] = None) -> dict:
    """The exact row a replenished question lands as. Pure, so the INSERT
    payload is testable without a client (fleet S3) and so the status
    contract with S1 is readable in one place.

    status='ready' — verified, deduped, moderation-passed, never served."""
    fields = lily_lane_row_fields(lane)
    text = str((question or {}).get("prompt") or (question or {}).get("question") or "").strip()
    canonical = str((question or {}).get("canonical_answer") or "").strip()
    acceptable = (question or {}).get("acceptable_answers")
    if not isinstance(acceptable, list) or not acceptable:
        acceptable = [canonical.lower()] if canonical else []
    choices = (question or {}).get("choices")
    if not isinstance(choices, list) or len(choices) != 4:
        choices = None
    try:
        tier = int((question or {}).get("difficulty_tier") or 2)
    except (TypeError, ValueError):
        tier = 2
    row = {
        "mode": fields.get("mode", DECK_GENERAL),
        "adult": bool(fields.get("adult")),
        "category": fields.get("category", ""),
        "question": text,
        "canonical_answer": canonical,
        "acceptable_answers": [str(a) for a in acceptable if str(a).strip()],
        "difficulty_tier": tier,
        "reveal_color": str((question or {}).get("reveal_color") or "") or None,
        "source": "bank_replenish_v1",
        "status": STATUS_READY,
        "lane": lane,
        "question_text_sha256": lily_bank_text_sha256(text),
        "replenished_at": "now()",
    }
    if choices:
        row["choices"] = [str(c) for c in choices]
    if run_id:
        row["replenish_run_id"] = run_id
    return row


async def lily_bank_insert_ready(
    supabase, *, lane: str, question: dict, run_id: Optional[str] = None
) -> bool:
    """INSERT one verified question as a status='ready' bank row. Returns
    True on a real insert, False on any refusal or failure.

    Every failure is swallowed — replenishment is background and must
    never surface on any spoken path (lily_arsenal_insert's own rule,
    same words, same reason)."""
    if supabase is None or not lily_lane_row_fields(lane):
        return False
    row = lily_ready_row(lane, question or {}, run_id=run_id)
    if not row["question"] or not row["canonical_answer"]:
        return False
    try:
        await asyncio.to_thread(
            lambda: supabase.table(QUESTIONS_TABLE).insert(row).execute()
        )
    except Exception as e:
        logger.warning(
            "LILY_BANK | INSERT_FAILED | lane=%s error_class=%s error=%s",
            lane, type(e).__name__, e,
        )
        return False
    logger.info(
        "LILY_BANK | BANKED | lane=%s status=%s hash=%s tier=%s",
        lane, STATUS_READY, row["question_text_sha256"][:12], row["difficulty_tier"],
    )
    return True


# ---------------------------------------------------------------------------
# Run receipts (mirrors lily_arsenal's run bookkeeping).
# ---------------------------------------------------------------------------


async def lily_run_start(
    supabase, *, lane: str, target: int, ready_at_start: int = 0
) -> Optional[str]:
    """Open a replenishment-run row and return its id, or None if a run is
    ALREADY active for this lane.

    Concurrency safety is the database's job: the partial unique index on
    (lane) WHERE status='running' means a second concurrent run's insert
    fails outright. An in-session sweep and a cron run cannot double-fill
    a lane — structurally, not by convention."""
    if supabase is None:
        return None
    deck, category = lily_parse_lane(lane)
    if not deck:
        return None
    try:
        result = await asyncio.to_thread(
            lambda: supabase.table(RUNS_TABLE)
            .insert({
                "lane": lane,
                "deck": deck,
                "category": category,
                "status": "running",
                "target_depth": int(target),
                "ready_at_start": int(ready_at_start),
                "effort": lily_config.bank_replenish_effort(),
            })
            .execute()
        )
        rows = result.data or []
        run_id = rows[0].get("id") if rows else None
        logger.info(
            "LILY_BANK | REPLENISH_START | lane=%s target=%d ready=%d run=%s "
            "effort=%s",
            lane, int(target), int(ready_at_start), run_id,
            lily_config.bank_replenish_effort(),
        )
        return run_id
    except Exception as e:
        logger.info(
            "LILY_BANK | REPLENISH_BLOCKED | lane=%s: %s — a run is already "
            "active for this lane", lane, e,
        )
        return None


async def lily_run_heartbeat(supabase, *, run_id: str) -> bool:
    """Mark a run still alive. What separates 'interrupted' from 'running'
    is a heartbeat that stopped."""
    if supabase is None or not run_id:
        return False
    try:
        await asyncio.to_thread(
            lambda: supabase.table(RUNS_TABLE)
            .update({"heartbeat_at": "now()"})
            .eq("id", run_id)
            .execute()
        )
        return True
    except Exception:
        return False


async def lily_run_finish(
    supabase, *, run_id: str, summary: dict, status: str = "completed"
) -> bool:
    """Close a run row with its numbers. Written even on failure — a run
    that died is a run whose numbers the operator still needs."""
    if supabase is None or not run_id:
        return False
    try:
        await asyncio.to_thread(
            lambda: supabase.table(RUNS_TABLE)
            .update({
                "status": status,
                "finished_at": "now()",
                "authored_count": int(summary.get("authored", 0)),
                "accepted_count": int(summary.get("accepted", 0)),
                "skipped_duplicate": int(summary.get("dup", 0)),
                "rejected_verify": int(summary.get("rejected_verify", 0)),
                "rejected_moderation": int(summary.get("rejected_moderation", 0)),
                "error_count": int(summary.get("errors", 0)),
                "prompt_tokens": int(summary.get("prompt_tokens", 0)),
                "completion_tokens": int(summary.get("completion_tokens", 0)),
                "cost_tokens": int(summary.get("cost_tokens", 0)),
                "duration_seconds": round(
                    float(summary.get("duration_seconds", 0.0)), 2
                ),
                "notes": (summary.get("notes") or "")[:2000] or None,
            })
            .eq("id", run_id)
            .execute()
        )
        return True
    except Exception as e:
        logger.warning(
            "LILY_BANK | RUN_FINISH_FAILED | run=%s error_class=%s error=%s",
            run_id, type(e).__name__, e,
        )
        return False


def _age_seconds(timestamp) -> Optional[float]:
    raw = str(timestamp or "").strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        parsed = datetime.datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.timezone.utc)
        now = datetime.datetime.now(datetime.timezone.utc)
        return (now - parsed).total_seconds()
    except (ValueError, TypeError):
        return None


async def lily_run_reclaim_stale(
    supabase, *, lane: str, max_age_seconds: float = RUN_STALE_AFTER_SECONDS
) -> int:
    """An interrupted run leaves its row 'running' and would block the lane
    forever. Mark genuinely DEAD rows failed so a later run can proceed.

    Staleness is judged on the HEARTBEAT, exactly as
    lily_arsenal.lily_run_reclaim_stale judges it: reclaiming every
    'running' row on sight would hand the concurrency guard back its own
    key."""
    if supabase is None:
        return 0
    try:
        rows = await asyncio.to_thread(
            lambda: supabase.table(RUNS_TABLE)
            .select("id,heartbeat_at,started_at")
            .eq("lane", lane)
            .eq("status", "running")
            .execute()
        )
        stale = []
        for r in rows.data or []:
            if not r.get("id"):
                continue
            age = _age_seconds(r.get("heartbeat_at") or r.get("started_at"))
            if age is None or age >= max_age_seconds:
                stale.append(r["id"])
        for run_id in stale:
            await asyncio.to_thread(
                lambda rid=run_id: supabase.table(RUNS_TABLE)
                .update({
                    "status": "failed",
                    "finished_at": "now()",
                    "notes": "reclaimed as stale by a later run (interrupted)",
                })
                .eq("id", rid)
                .execute()
            )
        if stale:
            logger.info(
                "LILY_BANK | RUN_RECLAIMED | lane=%s count=%d", lane, len(stale)
            )
        return len(stale)
    except Exception as e:
        logger.warning(
            "LILY_BANK | RUN_RECLAIM_FAILED | lane=%s error_class=%s error=%s",
            lane, type(e).__name__, e,
        )
        return 0


async def lily_run_cost_tokens(supabase, *, run_id: str) -> dict:
    """COST PER QUESTION, measured rather than estimated.

    Every authoring and verification call this module makes is tagged
    purpose='bank_replenish' with usage_session_id=<run id>, so the run's
    token bill is a SELECT over lily_llm_usage (migrations 026/027) — the
    same rows the streaming transport already writes on every call. No new
    meter, no price-sheet guess: prompt + completion tokens, summed.

    Returns {'prompt_tokens', 'completion_tokens', 'cost_tokens', 'calls'};
    zeros when the table cannot be read (the receipt then says 0, which is
    honest — nothing was measured — rather than inventing a number)."""
    out = {"prompt_tokens": 0, "completion_tokens": 0, "cost_tokens": 0, "calls": 0}
    if supabase is None or not run_id:
        return out
    try:
        rows = await asyncio.to_thread(
            lambda: supabase.table(USAGE_TABLE)
            .select("prompt_tokens,completion_tokens")
            .eq("session_id", run_id)
            .limit(1000)
            .execute()
        )
    except Exception as e:
        logger.info(
            "LILY_BANK | COST_READ_SKIPPED | run=%s error_class=%s error=%s",
            run_id, type(e).__name__, e,
        )
        return out
    for r in rows.data or []:
        out["calls"] += 1
        for key in ("prompt_tokens", "completion_tokens"):
            try:
                out[key] += int((r or {}).get(key) or 0)
            except (TypeError, ValueError):
                pass
    out["cost_tokens"] = out["prompt_tokens"] + out["completion_tokens"]
    return out


# ---------------------------------------------------------------------------
# Process-level counters — session_metrics.bank_replenish's source (S1).
# ---------------------------------------------------------------------------

_SESSION_COUNTS = {"runs": 0, "authored": 0, "accepted": 0, "rejected": 0, "dup": 0}


def lily_session_summary() -> dict:
    """{runs, authored, accepted, rejected, dup} for this process — the
    consumer is lily_agent.lily_session_metadata's
    session_metrics.bank_replenish block, so "did the author run tonight,
    and what did it produce" is a field on the session row rather than a
    log grep. Always present, zeros when nothing fired: "it never ran" is
    a stated value, not an absence."""
    return dict(_SESSION_COUNTS)


def lily_reset_session_summary() -> None:
    """Test seam only (and a fresh process starts here anyway)."""
    for key in _SESSION_COUNTS:
        _SESSION_COUNTS[key] = 0


def _note(**deltas) -> None:
    for key, value in deltas.items():
        if key in _SESSION_COUNTS:
            _SESSION_COUNTS[key] += int(value or 0)


# ---------------------------------------------------------------------------
# One lane, end to end.
# ---------------------------------------------------------------------------


def lily_backoff_seconds(attempt: int, *, base: Optional[float] = None) -> float:
    """Exponential backoff for a failed authoring attempt: base * 2^(N-1),
    N counted from 1. Bounded by lily_config.bank_replenish_max_attempts()
    attempts per slot, so the ceiling is base*4 at the default of 3."""
    b = lily_config.bank_replenish_backoff_seconds() if base is None else float(base)
    return max(0.0, b) * (2 ** max(0, int(attempt) - 1))


async def lily_replenish_lane(
    supabase,
    *,
    lane: str,
    author: Callable,
    verify: Optional[Callable] = None,
    target: Optional[int] = None,
    max_new: Optional[int] = None,
    run_id: Optional[str] = None,
    sleep: Optional[Callable] = None,
) -> dict:
    """Top ONE lane back toward its target depth. Returns the run summary.

    Callables (injected, so the whole loop is testable with fakes on a host
    with no provider credentials):

      author(lane, run_id) -> question dict | None
          Writes one question. RAISES on provider failure — the exception
          text is classified by lily_arsenal_gen's own signature matchers
          into moderation-refused / provider-unavailable / error, so this
          module counts the same three outcomes the arsenal counts.
      verify(question, run_id) -> (ok: bool, reason: str)
          The EXISTING verify step (lily_reasoning.verify_question, 3-7s).
          Fast, so it stays in-line: it is the gate that stops the author
          banking a wrong answer forever. `None` skips verification and is
          only for tests that are exercising something else.

    THE PIPELINE per slot: author -> verify -> dedup (exact sha256 + the A5
    similarity check) -> insert as status='ready'. Every rejection is
    counted and logged with its own receipt; nothing is silently dropped.

    NEVER RAISES except CancelledError, which is re-raised immediately and
    unchanged so a cancelled session tears the job down cleanly."""
    started = time.monotonic()
    tgt = max(1, lily_config.bank_target_depth(lane) if target is None else int(target))
    budget_cap = (
        lily_config.bank_replenish_max_new_per_run() if max_new is None
        else max(0, int(max_new))
    )
    summary = {
        "lane": lane,
        "target": tgt,
        "authored": 0,
        "accepted": 0,
        "dup": 0,
        "rejected_verify": 0,
        "rejected_moderation": 0,
        "errors": 0,
        "status": "completed",
        "notes": "",
    }
    if supabase is None or not lily_lane_row_fields(lane) or author is None:
        summary["status"] = "failed"
        summary["notes"] = "no client, unknown lane, or no author"
        return summary

    napper = sleep or asyncio.sleep
    depth = await lily_lane_depth(supabase, lane=lane)
    shortfall = max(0, tgt - depth["servable"])
    budget = min(shortfall, budget_cap)
    summary["ready_at_start"] = depth["servable"]
    if budget <= 0:
        summary["notes"] = "already at target"
        return summary

    max_attempts = lily_config.bank_replenish_max_attempts()
    _note(runs=1)
    for _slot in range(budget):
        question = None
        for attempt in range(1, max_attempts + 1):
            try:
                question = await author(lane, run_id)
                break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # The arsenal's classification, reused verbatim: a provider
                # REFUSING the content is an expected outcome to be counted,
                # a dead socket or a missing key is a reason to stop the
                # lane entirely, and everything else is a transient worth a
                # backed-off retry.
                if lily_arsenal_gen.lily_is_unavailable(e):
                    summary["errors"] += 1
                    summary["status"] = "failed"
                    summary["notes"] = f"provider unavailable: {str(e)[:200]}"
                    logger.warning(
                        "LILY_BANK | REPLENISH_FAILED | lane=%s reason=unavailable "
                        "error_class=%s error=%s", lane, type(e).__name__, e,
                    )
                    _note(rejected=1)
                    return await _close(
                        summary, supabase, run_id=run_id, started=started
                    )
                moderation = lily_arsenal_gen.lily_is_moderation_rejection(e)
                if moderation:
                    summary["rejected_moderation"] += 1
                    _note(rejected=1)
                    logger.warning(
                        "LILY_BANK | MODERATION_REJECTED | lane=%s attempt=%d/%d "
                        "reason=%s", lane, attempt, max_attempts, str(e)[:200],
                    )
                else:
                    summary["errors"] += 1
                    _note(rejected=1)
                back = lily_backoff_seconds(attempt)
                logger.warning(
                    "LILY_BANK | REPLENISH_FAILED | lane=%s attempt=%d/%d "
                    "backoff_s=%.1f error_class=%s error=%s",
                    lane, attempt, max_attempts, back, type(e).__name__, e,
                )
                if attempt >= max_attempts:
                    question = None
                    break
                try:
                    await napper(back)
                except asyncio.CancelledError:
                    raise
        if question is None:
            # This slot is spent. The next sweep tries again; a lane that
            # cannot author is a WARN with a number, never a hot loop.
            continue
        summary["authored"] += 1
        _note(authored=1)

        text = str(question.get("prompt") or question.get("question") or "").strip()
        if not text or not str(question.get("canonical_answer") or "").strip():
            summary["errors"] += 1
            _note(rejected=1)
            continue

        if verify is not None:
            try:
                ok, reason = await verify(question, run_id)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                ok, reason = False, f"verify error ({type(e).__name__}): {e}"
            if not ok:
                summary["rejected_verify"] += 1
                _note(rejected=1)
                logger.info(
                    "LILY_BANK | VERIFY_REJECTED | lane=%s reason=%s",
                    lane, str(reason)[:200],
                )
                continue
            # Verification may CORRECT the canonical answer in place
            # (lily_reasoning.verify_question does), so re-read the text
            # after it rather than before.
            text = str(question.get("prompt") or question.get("question") or "").strip()

        dup = await lily_bank_find_duplicate(
            supabase, lane=lane, question_text=text
        )
        if dup:
            summary["dup"] += 1
            _note(dup=1)
            logger.info(
                "LILY_BANK | DUP_REJECTED | lane=%s match=%s existing_id=%s",
                lane, dup.get("match"), dup.get("id"),
            )
            continue

        if await lily_bank_insert_ready(
            supabase, lane=lane, question=question, run_id=run_id
        ):
            summary["accepted"] += 1
            _note(accepted=1)
        else:
            summary["errors"] += 1
            _note(rejected=1)
        if run_id:
            await lily_run_heartbeat(supabase, run_id=run_id)

    return await _close(summary, supabase, run_id=run_id, started=started)


async def _close(summary: dict, supabase, *, run_id, started: float) -> dict:
    """Price the run off its usage rows, emit the cost receipt, close the
    receipt row. One exit for every path out of lily_replenish_lane so a
    run can never finish without a number."""
    summary["duration_seconds"] = round(time.monotonic() - started, 2)
    cost = await lily_run_cost_tokens(supabase, run_id=run_id) if run_id else {}
    summary["prompt_tokens"] = int(cost.get("prompt_tokens", 0))
    summary["completion_tokens"] = int(cost.get("completion_tokens", 0))
    summary["cost_tokens"] = int(cost.get("cost_tokens", 0))
    summary["llm_calls"] = int(cost.get("calls", 0))
    accepted = max(0, int(summary.get("accepted", 0)))
    summary["cost_tokens_per_question"] = (
        round(summary["cost_tokens"] / accepted, 1) if accepted else None
    )
    rejected = (
        int(summary.get("rejected_verify", 0))
        + int(summary.get("rejected_moderation", 0))
        + int(summary.get("errors", 0))
    )
    summary["rejected"] = rejected
    # Deliverable 3's line, verbatim shape.
    logger.info(
        "LILY_BANK | REPLENISH | lane=%s authored=%d accepted=%d rejected=%d "
        "dup=%d cost_tokens=%d",
        summary.get("lane"), summary.get("authored", 0), accepted, rejected,
        summary.get("dup", 0), summary["cost_tokens"],
    )
    logger.info(
        "LILY_BANK | REPLENISH_DONE | lane=%s status=%s accepted=%d target=%d "
        "duration_s=%.1f cost_tokens_per_question=%s effort=%s",
        summary.get("lane"), summary.get("status"), accepted,
        summary.get("target"), summary["duration_seconds"],
        summary["cost_tokens_per_question"], lily_config.bank_replenish_effort(),
    )
    if run_id:
        await lily_run_finish(
            supabase, run_id=run_id, summary=summary,
            status="completed" if summary.get("status") == "completed" else "failed",
        )
    return summary


# ---------------------------------------------------------------------------
# The sweep and the detached supervisor.
# ---------------------------------------------------------------------------

# In-process single-flight. The DB index is the cross-process guarantee;
# this is the cheap one that stops a sweep stacking on its own predecessor
# when a lane's authoring outlives the sweep interval.
_ACTIVE_LANES: set = set()


def lily_active_lanes() -> set:
    return set(_ACTIVE_LANES)


async def lily_replenish_sweep(
    supabase,
    *,
    author: Callable,
    verify: Optional[Callable] = None,
    lanes=None,
    consumed_by_lane: Optional[dict] = None,
    sleep: Optional[Callable] = None,
) -> list:
    """One pass over the lanes: read each watermark, replenish EXACTLY the
    lanes that crossed it, leave the others alone.

    Per lane independently — that is the whole reason the watermark is not
    a single bank-wide number. A sweep that found academic short and
    adult_kink full opens one run, not two, and the receipt for the lane it
    skipped is the WATERMARK line saying why.

    Never raises (CancelledError excepted)."""
    out = []
    for lane in (lanes or LANES):
        if lane in _ACTIVE_LANES:
            logger.info(
                "LILY_BANK | REPLENISH_SKIPPED | lane=%s reason=already_running",
                lane,
            )
            continue
        try:
            mark = await lily_bank_watermark(
                supabase, lane=lane,
                consumed=(consumed_by_lane or {}).get(lane),
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(
                "LILY_BANK | WATERMARK_FAILED | lane=%s error_class=%s error=%s",
                lane, type(e).__name__, e,
            )
            continue
        if not mark["below_watermark"]:
            continue
        _ACTIVE_LANES.add(lane)
        run_id = None
        try:
            await lily_run_reclaim_stale(supabase, lane=lane)
            run_id = await lily_run_start(
                supabase, lane=lane, target=mark["target"],
                ready_at_start=mark["ready"],
            )
            if run_id is None:
                # Another process holds this lane. Not an error: the bank
                # is being topped, just not by us.
                continue
            out.append(await lily_replenish_lane(
                supabase, lane=lane, author=author, verify=verify,
                target=mark["target"], run_id=run_id, sleep=sleep,
            ))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(
                "LILY_BANK | REPLENISH_FAILED | lane=%s reason=sweep "
                "error_class=%s error=%s", lane, type(e).__name__, e,
            )
            if run_id:
                await lily_run_finish(
                    supabase, run_id=run_id,
                    summary={"notes": f"sweep error: {type(e).__name__}"},
                    status="failed",
                )
        finally:
            _ACTIVE_LANES.discard(lane)
    return out


async def lily_bank_replenish_loop(
    supabase,
    *,
    author: Callable,
    verify: Optional[Callable] = None,
    lanes=None,
    interval_seconds: Optional[float] = None,
    max_sweeps: Optional[int] = None,
    sleep: Optional[Callable] = None,
) -> int:
    """The DETACHED supervisor: sweep, sleep, repeat, forever, until the
    task is cancelled.

    This coroutine is never awaited by the game. lily_agent starts it with
    ONE guarded lily_spawn at boot and the session's teardown cancels it;
    in between it holds no lock the delivery path can want, writes nothing
    the delivery path reads mid-turn, and swallows every failure. An author
    that takes sixty seconds delays the NEXT sweep and nothing else.

    Returns the number of sweeps completed (tests drive it with
    max_sweeps; live it only ever returns via CancelledError)."""
    napper = sleep or asyncio.sleep
    interval = (
        lily_config.bank_replenish_interval_seconds()
        if interval_seconds is None else float(interval_seconds)
    )
    sweeps = 0
    try:
        while max_sweeps is None or sweeps < max_sweeps:
            await lily_replenish_sweep(
                supabase, author=author, verify=verify, lanes=lanes, sleep=sleep,
            )
            sweeps += 1
            if max_sweeps is not None and sweeps >= max_sweeps:
                break
            await napper(interval)
    except asyncio.CancelledError:
        logger.info("LILY_BANK | REPLENISH_LOOP_CANCELLED | sweeps=%d", sweeps)
        raise
    return sweeps


# ---------------------------------------------------------------------------
# Live provider bindings — the ONLY place this module names lily_reasoning.
# Imported lazily so a test (or the CLI's --status) never constructs one.
# ---------------------------------------------------------------------------


def lily_live_author(reasoning, *, avoid_by_lane: Optional[dict] = None):
    """Bind the live authoring callable: the EXISTING question generator
    (lily_reasoning.generate_question), at the BACKGROUND author's effort
    and tagged for cost accounting.

    Effort is lily_config.bank_replenish_effort() — a separate knob from
    the live prefetch tier precisely because this call is off the critical
    path: its think time costs nobody anything, so effort is a quality dial
    here. The live path's interim "medium" is untouched by this."""

    async def _author(lane: str, run_id: Optional[str] = None) -> Optional[dict]:
        deck, category = lily_parse_lane(lane)
        if not deck:
            return None
        avoid = list((avoid_by_lane or {}).get(lane) or [])
        # Tier spread: the lane is topped across the difficulty range rather
        # than filled with one tier, the same spread discipline
        # lily_arsenal_content applies to picture subjects.
        tier = 1 + (int(time.time()) // 7) % 3
        return await reasoning.generate_question(
            category,
            tier,
            avoid,
            effort=lily_config.bank_replenish_effort(),
            purpose=USAGE_PURPOSE,
            usage_session_id=run_id,
        )

    return _author


def lily_live_verify(reasoning):
    """Bind the live verification callable: the EXISTING verify step,
    unchanged (3-7s, fast enough that it stays in-line). Tagged with the
    same purpose/run id so its tokens land on the run's bill too — a cost
    figure that counts only the authoring half understates the question."""

    async def _verify(question: dict, run_id: Optional[str] = None) -> tuple:
        return await reasoning.verify_question(
            question,
            purpose=USAGE_PURPOSE,
            usage_session_id=run_id,
        )

    return _verify


async def lily_run_background_author(supabase) -> int:
    """THE in-session entry point — the single coroutine lily_agent's
    entrypoint hands to `lily_spawn`, so the agent's whole footprint for
    this WO is one guarded spawn and one shutdown cancel.

    Everything provider-facing is constructed HERE, lazily, so importing
    this module (a test, the CLI's --status) never builds a reasoning node
    or reads a credential. Returns the sweep count; live it only ever ends
    by cancellation."""
    if not lily_config.bank_replenish_enabled():
        logger.info(
            "LILY_BANK | REPLENISH_DISABLED | in-session author off "
            "(LILY_BANK_REPLENISH_ENABLED); the bank is topped out of "
            "session by scripts/bank_replenish.py"
        )
        return 0
    if supabase is None:
        logger.info("LILY_BANK | REPLENISH_DISABLED | no database handle")
        return 0
    try:
        import lily_reasoning  # lazy: keeps this module import-light
        reasoning = lily_reasoning.LilyReasoning()
    except Exception as e:
        logger.warning(
            "LILY_BANK | REPLENISH_FAILED | reason=no_reasoning_node "
            "error_class=%s error=%s", type(e).__name__, e,
        )
        return 0
    logger.info(
        "LILY_BANK | REPLENISH_LOOP_START | lanes=%d interval_s=%.0f effort=%s",
        len(LANES), lily_config.bank_replenish_interval_seconds(),
        lily_config.bank_replenish_effort(),
    )
    return await lily_bank_replenish_loop(
        supabase,
        author=lily_live_author(reasoning),
        verify=lily_live_verify(reasoning),
    )


async def lily_stop_background_author(task) -> None:
    """Cancel the detached author at session shutdown and wait for it to
    unwind. Cancellation-safe by construction: the loop re-raises
    CancelledError after logging, and any in-flight authoring call is
    abandoned — the bank simply has one fewer row, which is the cheapest
    possible failure mode for a job nothing waits on."""
    if task is None:
        return
    cancel = getattr(task, "cancel", None)
    if not callable(cancel):
        return
    try:
        cancel()
        await task
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.info(
            "LILY_BANK | REPLENISH_LOOP_STOPPED | error_class=%s error=%s",
            type(e).__name__, e,
        )


# ---------------------------------------------------------------------------
# Health readout (deliverable: count, last replenishment, rejection rate).
# ---------------------------------------------------------------------------


async def lily_bank_health(supabase, *, lanes=None) -> dict:
    """Per-lane bank health: servable count, target, consumed %, last
    replenishment, and the rejection rate off the run receipts.

    This is what the operator reads BEFORE a game night. The picture
    arsenal grew one of these because an empty shelf was discovered by a
    player (2026-08-07); the question bank gets the same instrument for the
    same reason."""
    out = {"lanes": {}, "healthy": True, "warnings": []}
    if supabase is None:
        out["healthy"] = False
        out["warnings"].append("no database handle")
        return out
    runs = await _last_runs(supabase)
    for lane in (lanes or LANES):
        mark = await lily_bank_watermark(supabase, lane=lane)
        run = runs.get(lane) or {}
        authored = int(run.get("authored") or 0)
        rejected = int(run.get("rejected") or 0)
        entry = {
            "ready": mark["ready"],
            "ready_new": mark["ready_new"],
            "active": mark["active"],
            "burned": mark["burned"],
            "target": mark["target"],
            "consumed_pct": mark["consumed_pct"],
            "below_watermark": mark["below_watermark"],
            "stocked": mark["ready"] >= mark["target"],
            "last_replenished_at": run.get("finished_at") or run.get("started_at"),
            "last_run_status": run.get("status"),
            "last_run_accepted": run.get("accepted"),
            "rejection_rate": round(rejected / authored, 3) if authored else 0.0,
            "cost_tokens": int(run.get("cost_tokens") or 0),
        }
        if entry["below_watermark"]:
            out["healthy"] = False
            out["warnings"].append(
                f"{lane}: {entry['ready']}/{entry['target']} servable — "
                f"below watermark"
            )
        out["lanes"][lane] = entry
    return out


async def _last_runs(supabase) -> dict:
    try:
        rows = await asyncio.to_thread(
            lambda: supabase.table(RUNS_TABLE)
            .select("*")
            .order("started_at", desc=True)
            .limit(60)
            .execute()
        )
    except Exception:
        return {}
    latest = {}
    for r in rows.data or []:
        lane = (r or {}).get("lane")
        if lane and lane not in latest:
            latest[lane] = {
                "started_at": r.get("started_at"),
                "finished_at": r.get("finished_at"),
                "status": r.get("status"),
                "authored": r.get("authored_count"),
                "accepted": r.get("accepted_count"),
                "rejected": (
                    int(r.get("rejected_verify") or 0)
                    + int(r.get("rejected_moderation") or 0)
                    + int(r.get("error_count") or 0)
                ),
                "cost_tokens": r.get("cost_tokens"),
            }
    return latest


def lily_format_bank_health(health: dict) -> str:
    """One human-readable block for a terminal or a log — the question
    bank's counterpart to lily_arsenal.lily_format_health_readout, and
    deliberately the same shape so an operator reads both the same way."""
    if not health:
        return "bank: no readout"
    lines = ["QUESTION BANK — lane health"]
    for lane, entry in (health.get("lanes") or {}).items():
        mark = "OK " if entry.get("stocked") else (
            "LOW" if entry.get("below_watermark") else "..."
        )
        lines.append(
            f"  [{mark}] {lane:<28} servable={entry['ready']}/{entry['target']}"
            f"  consumed={entry['consumed_pct']:.0%}"
            f"  burned={entry['burned']}"
            f"  last={entry.get('last_replenished_at') or 'never'}"
            f"  reject_rate={entry['rejection_rate']:.0%}"
            f"  cost_tokens={entry['cost_tokens']}"
        )
    for warning in health.get("warnings") or []:
        lines.append(f"  WARN {warning}")
    return "\n".join(lines)
