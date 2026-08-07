"""
lily_arsenal.py — the standing picture arsenal (WO-LILY-PATCH-003
binding additions A/B/C; stocked and completed by WO-LILY-ARSENAL-SEED-001).

Pre-generated question+image pairs, watermark-replenished, so pictures-on
at game start costs ZERO generation wait and the delivery path never
awaits Grok. Three register partitions, each tracking consumption
independently:

  general | adult_suggestive | adult_explicit

WHY THIS MODULE EXISTS, restated after 2026-08-07: an EMPTY arsenal
degrades silently to live generation, which is the dead air it was built
to prevent — the operator sat through 67 seconds of silence waiting for a
picture question. The shelf shipped in PATCH-003 P2; nothing was ever put
on it. Everything below that concerns entry ANATOMY, the quality GATE and
the seeding RUN exists so the shelf can actually be stocked, and so a
stocked shelf stays coherent instead of becoming a pile of pictures.

WHAT AN ENTRY IS (A1). Not "a picture". A complete, self-contained
picture question that can be reconstructed and served from its stored
fields alone:

  question_text + canonical_answer + acceptable_answers  — the question
  generation_prompt + generation_model + intensity       — how it was made
  image_storage_path + image_source + is_real_image      — the image
  format + options                                       — its SHAPE (A2)
  binding_direction                                      — image-first or
                                                           question-first
  subject_area + difficulty_tier + reveal_color          — the spread (A3)
  question_text_hash                                     — dedup key

BINDING DIRECTION is recorded per entry because correspondence failures
cluster by direction, and a cluster is invisible if nobody wrote down
which way each entry went. Image-first generates the picture and then
writes the question about what is in it (safer). Question-first writes the
question and generates an image to complete it (riskier — the image has to
actually show what the stem claims).

Rules encoded here:
  - DRAW: partition + status='ready' + NOT EXISTS a usage row for this
    group (group no-repeat, DB-enforced by the UNIQUE constraint — a
    second serve to the same group violates it).
  - WATERMARK: replenishment fires when a partition is 40% CONSUMED
    (lily_config.arsenal_replenish_ratio), tracked per partition —
    background, concurrent with play, NEVER on the delivery path.
    Expressed as a RATIO, not a count, so it stays correct when the
    operator changes depth.
  - GATE: entries land 'generating' and are PROMOTED to 'ready'. Adult
    partitions default to operator review — the draw cannot see an entry
    no one has passed.
  - RETIRE, never delete (status='retired') so provenance survives.

Stdlib + the injected supabase client only; every call is defensive
(returns None / [] / False on any failure, logs loudly) so a picture
lane hiccup can never break question supply.
"""

import asyncio
import datetime
import logging
import math
from typing import Optional

import lily_bank
import lily_config

logger = logging.getLogger("lily_arsenal")

ARSENAL_TABLE = "lily_picture_arsenal"
USAGE_TABLE = "lily_picture_arsenal_usage"
RUNS_TABLE = "lily_picture_arsenal_runs"
ARSENAL_STORAGE_BUCKET = "lily-arsenal"

# Standing depth per partition and the replenishment watermark. These are
# the DEFAULTS; lily_config reads the live values so depth changes without
# a deploy. Kept as module constants because callers (and the PATCH-003
# tests) reference them directly.
ARSENAL_TARGET_DEPTH = 10
ARSENAL_REPLENISH_AT_SERVED = 4  # fire when the 4th is served (6 remain)

PARTITIONS = ("general", "adult_suggestive", "adult_explicit")
ADULT_PARTITIONS = ("adult_suggestive", "adult_explicit")

STATUS_GENERATING = "generating"
STATUS_READY = "ready"
STATUS_REJECTED = "rejected"
STATUS_RETIRED = "retired"

BINDING_IMAGE_FIRST = "image_first"
BINDING_QUESTION_FIRST = "question_first"

# A seeding run whose heartbeat has been silent this long is presumed dead
# and its row can be reclaimed. Comfortably longer than the slowest
# plausible single generation (45s provider timeout × a few reworks) so a
# merely slow run is never mistaken for a crashed one.
RUN_STALE_AFTER_SECONDS = 900.0

# Near-duplicate threshold for the arsenal. Deliberately LOWER (looser,
# catches more) than lily_bank's 0.87 general-bank ratio: a picture bank
# of ten entries per partition cannot afford two questions that rhyme,
# where a thousand-row text bank can absorb it.
ARSENAL_DUP_RATIO = 0.82


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


def lily_partition_intensity(partition: str) -> Optional[str]:
    """The FIXED render heat a partition banks at. An adult_explicit row is
    explicit and an adult_suggestive row is suggestive, so a 'mix' session
    draws true-to-partition pairs from either pool rather than re-rolling
    heat at serve time. General banks no intensity at all."""
    return {
        "adult_suggestive": "suggestive",
        "adult_explicit": "explicit",
    }.get(partition)


# -- draw and burn (A7) -------------------------------------------------------


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
            .eq("status", STATUS_READY)
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
            "LILY_ARSENAL | SERVED | partition=%s group=%s id=%s format=%s",
            partition, group_id, row["id"], row.get("format"),
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
    """Reconstruct a servable §4.2 question from a stored entry — the A1
    round-trip. Everything the game needs to SPEAK the question and put
    its image on the rail comes out of the row; nothing is re-derived and
    nothing is regenerated."""
    acceptable = row.get("acceptable_answers")
    if not isinstance(acceptable, list) or not acceptable:
        acceptable = [str(row.get("canonical_answer") or "").lower()]
    options = row.get("options")
    if not isinstance(options, list) or not options:
        options = None
    path = row.get("image_storage_path")
    return {
        "id": f"arsenal_{row.get('id')}",
        "category": "pictures",
        "difficulty_tier": int(row.get("difficulty_tier") or 2),
        "prompt": row.get("question_text") or "",
        "canonical_answer": row.get("canonical_answer") or "",
        "acceptable_answers": acceptable,
        "options": options,
        "reveal_color": row.get("reveal_color") or "",
        "image_url": path,
        "image_source": row.get("image_source") or "arsenal",
        "image_storage_path": path,
        # Format drives how the question is SPOKEN, so a round can mix
        # shapes instead of serving one forever.
        "format": row.get("format") or "identify",
        "is_real_image": bool(row.get("is_real_image")),
        "_arsenal_partition": row.get("partition"),
        "_arsenal_intensity": row.get("intensity"),
        "_arsenal_binding": row.get("binding_direction"),
        "_arsenal_subject_area": row.get("subject_area"),
    }


async def lily_arsenal_retire(supabase, *, arsenal_id: str, reason: str = "") -> bool:
    """Retire an entry — status='retired', never a delete, so provenance
    survives (A7). A retired row keeps its usage history and its prompt;
    the draw simply stops seeing it."""
    if supabase is None or not arsenal_id:
        return False
    try:
        await asyncio.to_thread(
            lambda: supabase.table(ARSENAL_TABLE)
            .update({
                "status": STATUS_RETIRED,
                "retired_at": "now()",
                "rejected_reason": (reason or "")[:500] or None,
            })
            .eq("id", arsenal_id)
            .execute()
        )
        logger.info(
            "LILY_ARSENAL | RETIRED | id=%s reason=%s", arsenal_id, reason[:120]
        )
        return True
    except Exception as e:
        logger.warning("LILY_ARSENAL | RETIRE_FAILED | id=%s: %s", arsenal_id, e)
        return False


# -- counts and the watermark (A8) --------------------------------------------


async def lily_arsenal_served_count(
    supabase, *, session_id: str, partition: str
) -> int:
    """This session's served count for a partition — the watermark input."""
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
            .eq("status", STATUS_READY)
            .execute()
        )
        return int(getattr(result, "count", None) or len(result.data or []))
    except Exception as e:
        logger.warning(
            "LILY_ARSENAL | READY_COUNT_FAILED | partition=%s: %s", partition, e
        )
        return 0


async def lily_arsenal_bank_depth(supabase, *, partition: str) -> dict:
    """What is on the shelf OR on its way to it: {'ready', 'pending',
    'depth'}. `depth` = ready + pending.

    The seeding job sizes its work off `depth`, not off `ready`, and that
    distinction is what makes a re-run idempotent under a REVIEW gate.
    Adult entries land at 'generating' and wait for the operator, so a job
    that measured only 'ready' would find the shelf empty on every re-run
    and generate another full batch on top of the batch already queued for
    review — paying twice and burying the operator."""
    out = {"ready": 0, "pending": 0, "depth": 0}
    if supabase is None:
        return out
    try:
        rows = await asyncio.to_thread(
            lambda: supabase.table(ARSENAL_TABLE)
            .select("id,status")
            .eq("partition", partition)
            .limit(2000)
            .execute()
        )
        for r in rows.data or []:
            if r.get("status") == STATUS_READY:
                out["ready"] += 1
            elif r.get("status") == STATUS_GENERATING:
                out["pending"] += 1
    except Exception as e:
        logger.warning(
            "LILY_ARSENAL | DEPTH_FAILED | partition=%s: %s", partition, e
        )
    out["depth"] = out["ready"] + out["pending"]
    return out


def lily_replenish_threshold(target: int, ratio: Optional[float] = None) -> int:
    """Served-count at which a partition is 'ratio consumed' and refill
    fires. At depth 10 / 40% that is 4 — the standing behaviour. At depth 5
    it is 2, which a hardcoded 4 would have got wrong by firing only after
    the shelf was 80% empty. Always at least 1."""
    r = lily_config.arsenal_replenish_ratio() if ratio is None else ratio
    return max(1, math.ceil(max(1, int(target)) * r))


async def lily_arsenal_available_to_group(
    supabase, *, partition: str, group_id: str
) -> int:
    """Ready entries in `partition` this group has NOT already been served.

    This — not the global ready count — is what "running dry" means at a
    table. An entry served to one group stays 'ready' forever because it is
    still new to every other group, so play NEVER lowers the global count.
    A watermark watching only that number would sit at 10/10 while the
    group in front of you worked through the last entry it had not seen."""
    if supabase is None or not group_id:
        return 0
    try:
        served = await _served_arsenal_ids(supabase, group_id)
        rows = await asyncio.to_thread(
            lambda: supabase.table(ARSENAL_TABLE)
            .select("id")
            .eq("partition", partition)
            .eq("status", STATUS_READY)
            .limit(2000)
            .execute()
        )
        return sum(1 for r in (rows.data or []) if r.get("id") not in served)
    except Exception as e:
        logger.warning(
            "LILY_ARSENAL | AVAILABLE_COUNT_FAILED | partition=%s group=%s: %s",
            partition, group_id, e,
        )
        return 0


def lily_should_replenish(
    served_count: int,
    ready_count: int,
    *,
    partition: Optional[str] = None,
    target: Optional[int] = None,
    available_to_group: Optional[int] = None,
) -> bool:
    """Watermark: fire when this session has consumed `ratio` of a
    partition's depth AND the pool is below target. Concurrent with play;
    the caller runs it in the background.

    Per partition INDEPENDENTLY — a 'mix' session burning both adult pools
    evaluates each on its own count, so refilling suggestive never masks an
    explicit pool going dry.

    TWO ways to be below target, because there are two ways to run dry:

      ready_count < target
          the standing shelf itself is short (entries retired, or the bank
          was never seeded to depth).

      target - available_to_group >= threshold
          the shelf is full but THIS GROUP has already consumed `ratio` of
          it. Entries are never consumed globally — one served to this
          table is still new to every other table — so without this the
          watermark would read 10/10 and hold while the group in front of
          you ran out of things it had not already been shown.

    `available_to_group=None` keeps the original PATCH-003 behaviour for
    callers that do not track it."""
    tgt = lily_config.arsenal_target_depth(partition) if target is None else target
    threshold = lily_replenish_threshold(tgt)
    if served_count < threshold:
        return False
    if ready_count < tgt:
        return True
    if available_to_group is None:
        return False
    # Consumed BY THIS GROUP, across every session it has played — which is
    # what "40% consumed" means at a table, and is not the same as this
    # session's served count.
    return (tgt - available_to_group) >= threshold


# -- dedup and the quality gate (A5) ------------------------------------------


async def lily_arsenal_is_duplicate(
    supabase, *, partition: str, question_text: str, check_kb: bool = True
) -> Optional[dict]:
    """Near-duplicate check for a candidate question. Returns the matching
    row (with a 'match' key: 'exact' | 'fuzzy', and 'source': 'arsenal' |
    'kb') or None when the candidate is genuinely new.

    Two layers, because the exact hash alone lets a bank fill with the same
    question in different words:
      1. the arsenal itself — including 'generating' rows, so two entries
         in the same run cannot collide;
      2. the standing question bank — an arsenal question duplicating a KB
         question is a repeat waiting to happen at the table, even though
         they live in different tables.

    Reuses lily_bank.lily_find_duplicate (exact hash + difflib ratio) so
    the arsenal and the bank agree on what 'the same question' means."""
    if supabase is None or not (question_text or "").strip():
        return None
    try:
        rows = await asyncio.to_thread(
            lambda: supabase.table(ARSENAL_TABLE)
            .select("id,question_text,partition,status")
            .eq("partition", partition)
            .limit(500)
            .execute()
        )
        existing = [
            {"id": r.get("id"), "question": r.get("question_text"),
             "category": "pictures"}
            for r in (rows.data or [])
            if r.get("status") != STATUS_RETIRED
        ]
        hit = lily_bank.lily_find_duplicate(
            question_text, "pictures", existing, ratio=ARSENAL_DUP_RATIO
        )
        if hit:
            return {**hit, "source": "arsenal"}
    except Exception as e:
        logger.warning(
            "LILY_ARSENAL | DUP_CHECK_FAILED | partition=%s: %s", partition, e
        )
        return None
    if not check_kb:
        return None
    try:
        kb = await asyncio.to_thread(
            lambda: supabase.table("lily_questions")
            .select("id,question,category")
            .eq("category", "pictures")
            .limit(500)
            .execute()
        )
        hit = lily_bank.lily_find_duplicate(
            question_text, "pictures", kb.data or [], ratio=ARSENAL_DUP_RATIO
        )
        if hit:
            return {**hit, "source": "kb"}
    except Exception as e:
        # A KB dedup miss is not fatal — the arsenal layer already ran.
        logger.info("LILY_ARSENAL | KB_DUP_CHECK_SKIPPED | %s", e)
    return None


def lily_gate_status(partition: str, *, classifier_ok: bool) -> str:
    """The status a freshly generated entry lands at, per the configured
    gate (A5).

    Nothing that failed the outbound classifier is ever promotable — it
    lands 'rejected' regardless of gate mode. A clean entry lands 'ready'
    under 'auto' and stays 'generating' under 'review', where the draw
    cannot see it until the operator passes it. Adult partitions default
    to review for exactly the reason the work order gives: a bad explicit
    image reaching a table is expensive in a way a bad general one is
    not."""
    if not classifier_ok:
        return STATUS_REJECTED
    return (
        STATUS_READY
        if lily_config.arsenal_gate_mode(partition) == "auto"
        else STATUS_GENERATING
    )


async def lily_arsenal_pending_review(
    supabase, *, partition: Optional[str] = None, limit: int = 100
) -> list:
    """Entries sitting at 'generating' awaiting the operator's pass."""
    if supabase is None:
        return []
    try:
        q = (
            supabase.table(ARSENAL_TABLE)
            .select("*")
            .eq("status", STATUS_GENERATING)
        )
        if partition:
            q = q.eq("partition", partition)
        rows = await asyncio.to_thread(lambda: q.limit(limit).execute())
        return list(rows.data or [])
    except Exception as e:
        logger.warning("LILY_ARSENAL | PENDING_FAILED | %s", e)
        return []


async def lily_arsenal_promote(
    supabase, *, arsenal_id: str, reviewed_by: str = "operator"
) -> bool:
    """Operator pass: 'generating' -> 'ready'. This is the ONLY way an
    entry in a review-gated partition becomes servable."""
    if supabase is None or not arsenal_id:
        return False
    try:
        await asyncio.to_thread(
            lambda: supabase.table(ARSENAL_TABLE)
            .update({
                "status": STATUS_READY,
                "reviewed_at": "now()",
                "reviewed_by": reviewed_by,
            })
            .eq("id", arsenal_id)
            .execute()
        )
        logger.info(
            "LILY_ARSENAL | PROMOTED | id=%s by=%s", arsenal_id, reviewed_by
        )
        return True
    except Exception as e:
        logger.warning("LILY_ARSENAL | PROMOTE_FAILED | id=%s: %s", arsenal_id, e)
        return False


async def lily_arsenal_reject(
    supabase, *, arsenal_id: str, reason: str, reviewed_by: str = "operator"
) -> bool:
    """Operator fail: 'generating' -> 'rejected', with the reason kept.
    A rejection is a RECORDED outcome, never a silent drop."""
    if supabase is None or not arsenal_id:
        return False
    try:
        await asyncio.to_thread(
            lambda: supabase.table(ARSENAL_TABLE)
            .update({
                "status": STATUS_REJECTED,
                "rejected_reason": (reason or "")[:500],
                "reviewed_at": "now()",
                "reviewed_by": reviewed_by,
            })
            .eq("id", arsenal_id)
            .execute()
        )
        logger.info("LILY_ARSENAL | REJECTED | id=%s reason=%s", arsenal_id, reason[:120])
        return True
    except Exception as e:
        logger.warning("LILY_ARSENAL | REJECT_FAILED | id=%s: %s", arsenal_id, e)
        return False


# -- banking an entry ---------------------------------------------------------


async def lily_arsenal_insert(
    supabase,
    *,
    partition,
    question,
    run_id: Optional[str] = None,
    classifier_ok: bool = True,
    cost_usd: Optional[float] = None,
    attempts: int = 1,
    check_duplicates: bool = True,
) -> bool:
    """Bank ONE generated pair as an arsenal row. Returns True on a real
    insert, False on a skip (missing image / duplicate text) or any
    failure.

    The row lands at the status the GATE dictates (A5) — 'ready' under
    auto, 'generating' under review, 'rejected' if the outbound classifier
    refused it. Duplicates are skipped idempotently: an exact hash match
    OR a near-duplicate in this partition or the standing bank means the
    pool already holds this question in substance.

    Every failure is swallowed — replenishment is background and must
    never surface on any spoken path."""
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
        if check_duplicates:
            dup = await lily_arsenal_is_duplicate(
                supabase, partition=partition, question_text=text
            )
            if dup:
                logger.info(
                    "LILY_ARSENAL | DUP_SKIPPED | partition=%s match=%s source=%s",
                    partition, dup.get("match"), dup.get("source"),
                )
                return False
        acceptable = question.get("acceptable_answers")
        if not isinstance(acceptable, list) or not acceptable:
            acceptable = [str(question.get("canonical_answer") or "").lower()]
        options = question.get("options")
        if not isinstance(options, list) or not options:
            options = None
        status = lily_gate_status(partition, classifier_ok=classifier_ok)
        row = {
            "partition": partition,
            "question_text": text,
            "question_text_hash": text_hash,
            "canonical_answer": question.get("canonical_answer") or "",
            "acceptable_answers": acceptable,
            "options": options,
            "generation_prompt": question.get("image_prompt")
            or question.get("generation_prompt") or "",
            "intensity": question.get("_arsenal_intensity")
            or question.get("intensity")
            or lily_partition_intensity(partition),
            "image_storage_path": path,
            "image_source": question.get("image_source") or "generated",
            "is_real_image": bool(question.get("is_real_image")),
            "format": question.get("format") or "identify",
            "binding_direction": question.get("binding_direction")
            or BINDING_IMAGE_FIRST,
            "subject_area": question.get("subject_area"),
            "difficulty_tier": int(question.get("difficulty_tier") or 2),
            "reveal_color": question.get("reveal_color") or "",
            "status": status,
            "gate_mode": lily_config.arsenal_gate_mode(partition),
            "classifier_verdict": "pass" if classifier_ok else "reject",
            "classified_at": "now()",
            "generation_attempts": max(1, int(attempts or 1)),
        }
        if question.get("generation_model"):
            row["generation_model"] = question["generation_model"]
        if cost_usd is not None:
            row["generation_cost_usd"] = round(float(cost_usd), 5)
        if run_id:
            row["run_id"] = run_id
        if not classifier_ok:
            row["rejected_reason"] = "outbound classifier refused"
        await asyncio.to_thread(
            lambda: supabase.table(ARSENAL_TABLE).insert(row).execute()
        )
        logger.info(
            "LILY_ARSENAL | BANKED | partition=%s format=%s status=%s hash=%s",
            partition, row["format"], status, text_hash[:12],
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
    target: Optional[int] = None,
    max_new: Optional[int] = None,
    run_id: Optional[str] = None,
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
    tgt = lily_config.arsenal_target_depth(partition) if target is None else target
    banked = 0
    # Cap the burst so a wedged generator can't spin unbounded; default to
    # exactly the shortfall.
    have = await lily_arsenal_ready_count(supabase, partition=partition)
    shortfall = max(0, tgt - have)
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
        if await lily_arsenal_insert(
            supabase, partition=partition, question=q, run_id=run_id
        ):
            banked += 1
    if banked:
        logger.info(
            "LILY_ARSENAL | REPLENISHED | partition=%s banked=%d target=%d",
            partition, banked, tgt,
        )
    else:
        # A partition that cannot refill is a WARN, never a silent pass —
        # an empty shelf must never again be discovered by a player.
        have_now = await lily_arsenal_ready_count(supabase, partition=partition)
        if have_now < lily_replenish_threshold(tgt):
            logger.warning(
                "ARSENAL_LOW | partition=%s ready=%d target=%d — refill "
                "banked nothing", partition, have_now, tgt,
            )
    return banked


# -- observability and cost (A10) ---------------------------------------------


async def lily_arsenal_health(supabase) -> dict:
    """Bank health readout: per-partition counts by status, last
    replenishment, rejection rate, oldest ready entry, and the standing
    cost of what is on the shelf.

    This is what the operator reads BEFORE a game night to see whether the
    bank is stocked — the check that did not exist on 2026-08-07, which is
    why an empty arsenal was discovered by a player instead."""
    out = {"partitions": {}, "healthy": True, "warnings": []}
    if supabase is None:
        out["healthy"] = False
        out["warnings"].append("no database handle")
        return out
    try:
        rows = await asyncio.to_thread(
            lambda: supabase.table(ARSENAL_TABLE)
            .select("partition,status,created_at,generation_cost_usd,format")
            .limit(5000)
            .execute()
        )
        data = list(rows.data or [])
    except Exception as e:
        logger.warning("LILY_ARSENAL | HEALTH_FAILED | %s", e)
        out["healthy"] = False
        out["warnings"].append(f"read failed: {e}")
        return out
    for partition in PARTITIONS:
        mine = [r for r in data if r.get("partition") == partition]
        by_status = {}
        for r in mine:
            key = r.get("status") or "unknown"
            by_status[key] = by_status.get(key, 0) + 1
        ready = by_status.get(STATUS_READY, 0)
        rejected = by_status.get(STATUS_REJECTED, 0)
        target = lily_config.arsenal_target_depth(partition)
        produced = ready + rejected + by_status.get(STATUS_GENERATING, 0)
        oldest = min(
            (str(r.get("created_at") or "") for r in mine
             if r.get("status") == STATUS_READY),
            default=None,
        )
        cost = 0.0
        for r in mine:
            try:
                cost += float(r.get("generation_cost_usd") or 0)
            except (TypeError, ValueError):
                pass
        formats = {}
        for r in mine:
            if r.get("status") == STATUS_READY:
                key = r.get("format") or "unknown"
                formats[key] = formats.get(key, 0) + 1
        entry = {
            "ready": ready,
            "target": target,
            "by_status": by_status,
            "pending_review": by_status.get(STATUS_GENERATING, 0),
            "rejected": rejected,
            "rejection_rate": round(rejected / produced, 3) if produced else 0.0,
            "oldest_ready_at": oldest,
            "cost_usd": round(cost, 5),
            "formats": formats,
            "below_watermark": ready < lily_replenish_threshold(target),
            "stocked": ready >= target,
        }
        if entry["below_watermark"]:
            out["healthy"] = False
            out["warnings"].append(
                f"{partition}: {ready}/{target} ready — below watermark"
            )
        out["partitions"][partition] = entry
    out["last_runs"] = await _last_runs(supabase)
    out["total_cost_usd"] = round(
        sum(p["cost_usd"] for p in out["partitions"].values()), 5
    )
    return out


async def _last_runs(supabase) -> dict:
    try:
        rows = await asyncio.to_thread(
            lambda: supabase.table(RUNS_TABLE)
            .select("*")
            .order("started_at", desc=True)
            .limit(30)
            .execute()
        )
    except Exception:
        return {}
    latest = {}
    for r in rows.data or []:
        part = r.get("partition")
        if part and part not in latest:
            latest[part] = {
                "started_at": r.get("started_at"),
                "finished_at": r.get("finished_at"),
                "status": r.get("status"),
                "created": r.get("created_count"),
                "rejected_moderation": r.get("rejected_moderation"),
                "cost_usd": r.get("cost_usd"),
            }
    return latest


def lily_arsenal_low_warnings(health: dict) -> list:
    """ARSENAL_LOW lines for every partition below its watermark. Emitting
    is the caller's job; this is the pure formatter so the readout and the
    log agree word for word."""
    lines = []
    for partition, entry in (health or {}).get("partitions", {}).items():
        if entry.get("below_watermark"):
            lines.append(
                f"ARSENAL_LOW | partition={partition} "
                f"ready={entry.get('ready')} target={entry.get('target')} "
                f"pending_review={entry.get('pending_review')}"
            )
    return lines


def lily_format_health_readout(health: dict) -> str:
    """One human-readable block the operator can read at a glance before a
    game night. Plain text on purpose — it goes in a log and a terminal."""
    if not health:
        return "arsenal: no readout"
    lines = ["PICTURE ARSENAL — bank health"]
    for partition in PARTITIONS:
        entry = (health.get("partitions") or {}).get(partition)
        if not entry:
            continue
        mark = "OK " if entry.get("stocked") else (
            "LOW" if entry.get("below_watermark") else "..."
        )
        lines.append(
            f"  [{mark}] {partition:<17} ready={entry['ready']}/{entry['target']}"
            f"  pending={entry['pending_review']}"
            f"  rejected={entry['rejected']}"
            f" (rate {entry['rejection_rate']:.0%})"
            f"  cost=${entry['cost_usd']:.2f}"
        )
        if entry.get("formats"):
            spread = ", ".join(
                f"{k}×{v}" for k, v in sorted(entry["formats"].items())
            )
            lines.append(f"        formats: {spread}")
    lines.append(f"  total standing spend: ${health.get('total_cost_usd', 0):.2f}")
    for warning in health.get("warnings") or []:
        lines.append(f"  WARN {warning}")
    return "\n".join(lines)


# -- seeding-run bookkeeping (A6 / A9 / A10) ----------------------------------


async def lily_run_start(
    supabase, *, partition: str, target: int
) -> Optional[str]:
    """Open a seeding-run row and return its id, or None if a run is
    ALREADY active for this partition.

    Concurrency safety is the database's job, not a flag's: the partial
    unique index on (partition) WHERE status='running' means the second
    concurrent run's insert fails outright. Two runs cannot double-fill
    the same shelf — structurally, not by convention."""
    if supabase is None:
        return None
    try:
        result = await asyncio.to_thread(
            lambda: supabase.table(RUNS_TABLE)
            .insert({
                "partition": partition,
                "status": "running",
                "target_depth": int(target),
            })
            .execute()
        )
        rows = result.data or []
        run_id = rows[0].get("id") if rows else None
        logger.info(
            "LILY_ARSENAL | RUN_START | partition=%s target=%d run=%s",
            partition, target, run_id,
        )
        return run_id
    except Exception as e:
        logger.warning(
            "LILY_ARSENAL | RUN_START_BLOCKED | partition=%s: %s — a run is "
            "already active for this partition", partition, e,
        )
        return None


async def lily_run_finish(
    supabase, *, run_id: str, summary: dict, status: str = "completed"
) -> bool:
    """Close a run row with its summary (A6): counts, duration, rejections,
    cost. Written even on failure — a run that died is a run whose numbers
    the operator still needs."""
    if supabase is None or not run_id:
        return False
    try:
        await asyncio.to_thread(
            lambda: supabase.table(RUNS_TABLE)
            .update({
                "status": status,
                "finished_at": "now()",
                "created_count": int(summary.get("created", 0)),
                "skipped_duplicate": int(summary.get("skipped_duplicate", 0)),
                "rejected_moderation": int(summary.get("rejected_moderation", 0)),
                "rejected_gate": int(summary.get("rejected_gate", 0)),
                "error_count": int(summary.get("errors", 0)),
                "cost_usd": round(float(summary.get("cost_usd", 0.0)), 5),
                "duration_seconds": round(float(summary.get("duration_seconds", 0.0)), 2),
                "notes": (summary.get("notes") or "")[:2000] or None,
            })
            .eq("id", run_id)
            .execute()
        )
        return True
    except Exception as e:
        logger.warning("LILY_ARSENAL | RUN_FINISH_FAILED | run=%s: %s", run_id, e)
        return False


async def lily_run_heartbeat(supabase, *, run_id: str) -> bool:
    """Mark a run still alive. What separates 'interrupted' from 'running'
    is a heartbeat that stopped, so the reclaim below can tell a crashed
    run from a slow one instead of guessing."""
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


def _age_seconds(timestamp) -> Optional[float]:
    """Age of an ISO-8601 timestamp in seconds, or None if unparseable."""
    raw = str(timestamp or "").strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        # Postgres microseconds can exceed 6 digits through PostgREST.
        parsed = datetime.datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.timezone.utc)
        now = datetime.datetime.now(datetime.timezone.utc)
        return (now - parsed).total_seconds()
    except (ValueError, TypeError):
        return None


async def lily_run_reclaim_stale(
    supabase, *, partition: str, max_age_seconds: float = RUN_STALE_AFTER_SECONDS
) -> int:
    """Resumability (A6): an interrupted run leaves its row 'running' and
    would block the next one forever. Mark genuinely DEAD rows failed so a
    re-run can proceed — the entries the dead run already banked stay
    banked, which is exactly what makes a resumed run cheap: it tops up the
    shortfall rather than starting over.

    Staleness is judged on the HEARTBEAT, not on existence. Reclaiming
    every 'running' row on sight would hand the concurrency guard back its
    own key: a second run would simply clear the first one's row and both
    would fill the same shelf. A run that is still beating is left alone
    and the caller is told to stand down."""
    if supabase is None:
        return 0
    try:
        rows = await asyncio.to_thread(
            lambda: supabase.table(RUNS_TABLE)
            .select("id,heartbeat_at,started_at")
            .eq("partition", partition)
            .eq("status", "running")
            .execute()
        )
        stale = []
        for r in rows.data or []:
            if not r.get("id"):
                continue
            age = _age_seconds(r.get("heartbeat_at") or r.get("started_at"))
            # An unparseable heartbeat is treated as stale: a row nobody can
            # date is a row nobody is tending.
            if age is None or age >= max_age_seconds:
                stale.append(r["id"])
            else:
                logger.info(
                    "LILY_ARSENAL | RUN_ACTIVE | partition=%s run=%s "
                    "heartbeat_age=%.0fs — leaving it alone",
                    partition, r["id"], age,
                )
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
                "LILY_ARSENAL | RUN_RECLAIMED | partition=%s count=%d",
                partition, len(stale),
            )
        return len(stale)
    except Exception as e:
        logger.warning(
            "LILY_ARSENAL | RUN_RECLAIM_FAILED | partition=%s: %s", partition, e
        )
        return 0
