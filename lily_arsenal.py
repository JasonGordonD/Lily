"""
lily_arsenal.py — the standing picture arsenal (WO-LILY-PATCH-003
binding additions A/B/C).

Pre-generated question+image pairs, watermark-replenished, so pictures-on
at game start costs ZERO generation wait and the delivery path never
awaits Grok. Three register partitions, each tracking consumption
independently:

  general | adult_suggestive | adult_explicit

Schema (Doc, migration lily_picture_arsenal_001, live on prod):
  lily_picture_arsenal(id, partition, question_text, question_text_hash,
    canonical_answer, acceptable_answers, generation_prompt,
    generation_model, intensity, image_storage_path, status, ...)
  lily_picture_arsenal_usage(id, arsenal_id, group_id, session_id,
    partition, served_at, UNIQUE(arsenal_id, group_id))

Rules encoded here:
  - DRAW: partition + status='ready' + NOT EXISTS a usage row for this
    group (group no-repeat, DB-enforced by the UNIQUE constraint — a
    second serve to the same group violates it).
  - WATERMARK: when the FOURTH image from a partition is served this
    session (six remaining toward a standing ten), replenishment fires
    for that partition — background, concurrent with play, NEVER on the
    delivery path.
  - RETIRE, never delete (status='retired') so provenance survives.

Stdlib + the injected supabase client only; every call is defensive
(returns None / [] / False on any failure, logs loudly) so a picture
lane hiccup can never break question supply.
"""

import asyncio
import logging
from typing import Optional

import lily_bank

logger = logging.getLogger("lily_arsenal")

ARSENAL_TABLE = "lily_picture_arsenal"
USAGE_TABLE = "lily_picture_arsenal_usage"
ARSENAL_STORAGE_BUCKET = "lily-arsenal"

# Standing depth per partition and the replenishment watermark.
ARSENAL_TARGET_DEPTH = 10
ARSENAL_REPLENISH_AT_SERVED = 4  # fire when the 4th is served (6 remain)

PARTITIONS = ("general", "adult_suggestive", "adult_explicit")


def lily_partitions_for(mode: str, intensity: Optional[str]) -> list:
    """Which arsenal partition(s) a session draws from. General deck ->
    'general'. Adult deck -> the heat-matched partition; 'mix' draws from
    BOTH adult partitions (each watermark fires on its own count)."""
    if mode != "adult":
        return ["general"]
    level = (intensity or "suggestive").strip().lower()
    if level == "explicit":
        return ["adult_explicit"]
    if level == "mix":
        return ["adult_suggestive", "adult_explicit"]
    return ["adult_suggestive"]


async def lily_arsenal_draw(
    supabase,
    *,
    partition: str,
    group_id: str,
    session_id: str,
    exclude_answers: Optional[set] = None,
) -> Optional[dict]:
    """Draw one ready pair from `partition` this group has NOT been served,
    record the usage row (DB-enforced group no-repeat), and return the
    question shape with its image. None on empty pool or any failure —
    the caller falls to the next rung of the ladder. `exclude_answers`
    adds the group's cross-session played answers (belt over the usage
    constraint, for answers seen via non-arsenal supply)."""
    if supabase is None or not partition or not group_id:
        return None
    excl = {str(a).strip().lower() for a in (exclude_answers or set()) if str(a).strip()}
    try:
        served_ids = await _served_arsenal_ids(supabase, group_id)
        rows = await asyncio.to_thread(
            lambda: supabase.table(ARSENAL_TABLE)
            .select("*")
            .eq("partition", partition)
            .eq("status", "ready")
            .limit(50)
            .execute()
        )
        pool = [
            r for r in (rows.data or [])
            if r.get("id") not in served_ids
            and str(r.get("canonical_answer") or "").strip().lower() not in excl
        ]
        if not pool:
            logger.info(
                "LILY_ARSENAL | POOL_EMPTY | partition=%s group=%s", partition, group_id
            )
            return None
        # Deterministic pick (oldest ready first — stable, no Math.random).
        row = min(pool, key=lambda r: str(r.get("created_at") or ""))
        # Claim it for this group; the UNIQUE(arsenal_id, group_id) makes a
        # double-serve a DB error, which we treat as "someone else took it"
        # and fall through.
        try:
            await asyncio.to_thread(
                lambda: supabase.table(USAGE_TABLE)
                .insert({
                    "arsenal_id": row["id"],
                    "group_id": group_id,
                    "session_id": session_id,
                    "partition": partition,
                })
                .execute()
            )
        except Exception as e:
            logger.warning(
                "LILY_ARSENAL | USAGE_INSERT_RACE | partition=%s group=%s id=%s: %s",
                partition, group_id, row["id"], e,
            )
            return None
        logger.info(
            "LILY_ARSENAL | SERVED | partition=%s group=%s id=%s",
            partition, group_id, row["id"],
        )
        return _row_to_question(row)
    except Exception as e:
        logger.error(
            "LILY_ARSENAL | DRAW_FAILED | partition=%s group=%s: %s",
            partition, group_id, e,
        )
        return None


async def _served_arsenal_ids(supabase, group_id: str) -> set:
    try:
        rows = await asyncio.to_thread(
            lambda: supabase.table(USAGE_TABLE)
            .select("arsenal_id")
            .eq("group_id", group_id)
            .execute()
        )
        return {r.get("arsenal_id") for r in (rows.data or [])}
    except Exception as e:
        logger.warning("LILY_ARSENAL | SERVED_IDS_FAILED | group=%s: %s", group_id, e)
        return set()


def _row_to_question(row: dict) -> dict:
    acceptable = row.get("acceptable_answers")
    if not isinstance(acceptable, list) or not acceptable:
        acceptable = [str(row.get("canonical_answer") or "").lower()]
    path = row.get("image_storage_path")
    return {
        "id": f"arsenal_{row.get('id')}",
        "category": "pictures",
        "difficulty_tier": int(row.get("difficulty_tier") or 2),
        "prompt": row.get("question_text") or "",
        "canonical_answer": row.get("canonical_answer") or "",
        "acceptable_answers": acceptable,
        "reveal_color": row.get("reveal_color") or "",
        "image_url": path,
        "image_source": "arsenal",
        "image_storage_path": path,
        "_arsenal_partition": row.get("partition"),
        "_arsenal_intensity": row.get("intensity"),
    }


async def lily_arsenal_served_count(
    supabase, *, session_id: str, partition: str
) -> int:
    """This session's served count for a partition — the watermark input
    (fire replenishment at ARSENAL_REPLENISH_AT_SERVED)."""
    if supabase is None:
        return 0
    try:
        result = await asyncio.to_thread(
            lambda: supabase.table(USAGE_TABLE)
            .select("id", count="exact")
            .eq("session_id", session_id)
            .eq("partition", partition)
            .execute()
        )
        return int(getattr(result, "count", None) or len(result.data or []))
    except Exception as e:
        logger.warning(
            "LILY_ARSENAL | COUNT_FAILED | session=%s partition=%s: %s",
            session_id, partition, e,
        )
        return 0


async def lily_arsenal_ready_count(supabase, *, partition: str) -> int:
    if supabase is None:
        return 0
    try:
        result = await asyncio.to_thread(
            lambda: supabase.table(ARSENAL_TABLE)
            .select("id", count="exact")
            .eq("partition", partition)
            .eq("status", "ready")
            .execute()
        )
        return int(getattr(result, "count", None) or len(result.data or []))
    except Exception as e:
        logger.warning(
            "LILY_ARSENAL | READY_COUNT_FAILED | partition=%s: %s", partition, e
        )
        return 0


def lily_should_replenish(served_count: int, ready_count: int) -> bool:
    """Watermark: fire when this session has served the 4th from a
    partition (six remaining toward ten) AND the standing pool is below
    target. Concurrent with play; the caller runs it in the background."""
    return (
        served_count >= ARSENAL_REPLENISH_AT_SERVED
        and ready_count < ARSENAL_TARGET_DEPTH
    )


# -- background replenishment (NEVER on the delivery path) --------------------


async def lily_arsenal_insert(supabase, *, partition, question) -> bool:
    """Bank ONE generated pair as a ready arsenal row. Returns True on a
    real insert, False on a skip (missing image / duplicate text) or any
    failure. Duplicate text is idempotently skipped (RETIRE, never dup):
    the same question_text_hash already standing means the pool already
    holds it. Every failure is swallowed — replenishment is background and
    must never surface on any spoken path."""
    if supabase is None or partition not in PARTITIONS or not question:
        return False
    text = str(question.get("prompt") or question.get("question_text") or "").strip()
    path = question.get("image_storage_path") or question.get("image_url")
    if not text or not path:
        # A pictureless generation is not arsenal-worthy — the arsenal's
        # entire promise is a ready image with zero delivery-path wait.
        return False
    text_hash = lily_bank.lily_question_text_hash(text)
    try:
        existing = await asyncio.to_thread(
            lambda: supabase.table(ARSENAL_TABLE)
            .select("id")
            .eq("partition", partition)
            .eq("question_text_hash", text_hash)
            .limit(1)
            .execute()
        )
        if existing.data:
            return False
        acceptable = question.get("acceptable_answers")
        if not isinstance(acceptable, list) or not acceptable:
            acceptable = [str(question.get("canonical_answer") or "").lower()]
        await asyncio.to_thread(
            lambda: supabase.table(ARSENAL_TABLE)
            .insert({
                "partition": partition,
                "question_text": text,
                "question_text_hash": text_hash,
                "canonical_answer": question.get("canonical_answer") or "",
                "acceptable_answers": acceptable,
                "generation_prompt": question.get("image_prompt") or "",
                "intensity": question.get("_arsenal_intensity")
                or question.get("intensity"),
                "image_storage_path": path,
                "status": "ready",
            })
            .execute()
        )
        logger.info(
            "LILY_ARSENAL | BANKED | partition=%s hash=%s", partition, text_hash[:12]
        )
        return True
    except Exception as e:
        logger.warning(
            "LILY_ARSENAL | INSERT_FAILED | partition=%s: %s", partition, e
        )
        return False


async def lily_arsenal_replenish(
    supabase,
    *,
    partition: str,
    generate_one,
    target: int = ARSENAL_TARGET_DEPTH,
    max_new: Optional[int] = None,
) -> int:
    """Fill `partition` back toward `target` ready rows. `generate_one` is
    an async callable returning a §4.2 question dict WITH an image (or None
    when generation is unavailable). Returns how many rows were banked.

    Runs in the background, concurrent with play — the ONLY place the
    arsenal touches generation. The delivery path draws exclusively from
    already-ready rows, so a slow or down generator degrades gracefully to
    the truthful pictureless line, never a spoken stall. A generator that
    returns None (generation down) stops the loop immediately."""
    if supabase is None or partition not in PARTITIONS or generate_one is None:
        return 0
    banked = 0
    # Cap the burst so a wedged generator can't spin unbounded; default to
    # exactly the shortfall.
    have = await lily_arsenal_ready_count(supabase, partition=partition)
    shortfall = max(0, target - have)
    budget = shortfall if max_new is None else min(shortfall, max_new)
    for _ in range(budget):
        try:
            q = await generate_one(partition)
        except Exception as e:
            logger.warning(
                "LILY_ARSENAL | REPLENISH_GEN_FAILED | partition=%s: %s",
                partition, e,
            )
            break
        if q is None:
            logger.info(
                "LILY_ARSENAL | REPLENISH_GEN_DOWN | partition=%s banked=%d",
                partition, banked,
            )
            break
        if await lily_arsenal_insert(supabase, partition=partition, question=q):
            banked += 1
    if banked:
        logger.info(
            "LILY_ARSENAL | REPLENISHED | partition=%s banked=%d target=%d",
            partition, banked, target,
        )
    return banked
