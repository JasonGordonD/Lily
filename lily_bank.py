"""
lily_bank.py — bank curation loop (WO-LILY-OMNIBUS-002, sub-agents D + F).

Three concerns, all keyed to the curated lily_questions bank:

  D1  Banking-on-generation: verified generated questions are inserted
      into lily_questions (source='generated') so the bank self-grows —
      with near-duplicate detection at insert time. Dups are DISCARDED
      and logged `LILY_BANK | DUP_DISCARDED`.
  D2  Per-group asked history (migration 010, lily_asked_history): one
      row per question SERVED; the bank draw and the generation
      avoid-list exclude the group's history so a returning table never
      hears a repeat.
  F   Gated category proposals (migration 011,
      lily_category_candidates): generation may return a
      `proposed_category`; proposals are tallied (use_count + distinct
      groups) but the question SERVES under its round FAMILY until the
      category is promoted (use_count >= 10 AND >= 3 distinct groups).
      Lily never announces unpromoted categories.

Deliberately dependency-light (lily_memory pattern): the pure logic —
text normalization, hashing, near-dup detection, history sets, promotion
gating — is stdlib-only and offline-testable. The Supabase I/O functions
take an already-constructed client, run blocking calls off-thread, and
are fire-and-forget: they log `LILY_BANK | ...` markers and never raise
into the live session.
"""

import asyncio
import difflib
import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("lily_bank")

# Near-dup gate: exact normalized-hash match (any category) OR difflib
# ratio >= DUP_FUZZY_RATIO against a row in the SAME category.
DUP_FUZZY_RATIO = 0.87

# Asked-history draw window: the most recent N servings per group are
# excluded from bank draws and generation output.
ASKED_HISTORY_LIMIT = 500

# Category-proposal promotion gate (sub-agent F).
CATEGORY_PROMOTE_MIN_USES = 10
CATEGORY_PROMOTE_MIN_GROUPS = 3

# ---------------------------------------------------------------------------
# Lane map: the fixed family rotation -> the bank's category vocabulary
# (WO-LILY-SUPPLY-001 S1)
# ---------------------------------------------------------------------------
#
# The rotation (lily_agent.CATEGORY_FAMILIES) and the bank
# (lily_questions.category) were never the same vocabulary, and the draw
# compared them with an exact `.eq("category", family)`. Live counts,
# 2026-09-06:
#
#   family "academic"            -> 148 general rows labelled "academic"  HIT
#   family "wordplay"            ->  40 general rows labelled "wordplay"  HIT
#   family "pop culture"         ->   6 rows labelled "pop culture" while
#                                     38 sit under "pop_culture"        MISS
#   family "lifestyle-potpourri" ->   0 rows: the bank stores "lifestyle"
#                                                                       MISS
#
# Two of four lanes could therefore only ever be served by the
# any-category fallback stage — which is exactly the stage HOTFIX-006 N2
# had to make refusable, because "anything" is how Psycho ended up in a
# Cape Cod round. Bank-first supply cannot rest on it, so the mapping is
# declared here as data, tested against the live vocabulary, and used by
# the draw (ordered: the primary category first, then the lane's
# neighbours) and by the per-lane health readout.
#
# Each lane lists its bank categories most-specific-first, per deck. A
# category may appear in exactly ONE lane (lily_lane_for_category is the
# declared inverse); anything unlisted falls to the potpourri lane, which
# is what "potpourri" means.
POTPOURRI_LANE = "lifestyle-potpourri"

LANE_BANK_CATEGORIES: dict = {
    "academic": {
        "general": ["academic", "science", "history", "geography",
                    "nature", "mythology"],
        "adult": ["adult_science", "adult_history", "academic", "science",
                  "history", "geography", "mythology"],
    },
    "pop culture": {
        "general": ["pop_culture", "pop culture", "music", "sports"],
        "adult": ["adult_popculture", "pop_culture", "pop culture",
                  "music", "sports"],
    },
    "wordplay": {
        "general": ["wordplay", "literature"],
        "adult": ["adult_wordplay", "wordplay", "literature"],
    },
    POTPOURRI_LANE: {
        "general": ["lifestyle", "art", "Greece", "potpourri",
                    POTPOURRI_LANE],
        "adult": ["adult_couples", "adult_kink", "drinking", "lifestyle",
                  "art", "potpourri"],
    },
}

# Decks: the `adult` boolean column on lily_questions. "adult" is the
# unified standard deck WO-PRMPT-LILY-REFACTOR-001 left in place; the 307
# active adult=false rows are the "general" deck, structurally unreachable
# before this WO because every draw filtered `.eq("adult", True)`.
BANK_DECKS = ("general", "adult")

# ---------------------------------------------------------------------------
# The S1 <-> S2 seam (WO-LILY-SUPPLY-001)
# ---------------------------------------------------------------------------
#
# STATUS. Migration 009 established `status`, with 'active' = servable.
# The S2 background author lands its rows at 'ready' — verified, deduped,
# moderation-passed, never served — and never writes or reinterprets
# 'active'. Both are servable to this draw. The ORDER matters and is
# deliberate: 'active' first, so the 448-row standing bank drains before
# the replenished reserve does. Anything else — 'burned' (WS-4),
# 'retired' (the E tuning job) — is not servable and never has been.
BANK_SERVABLE_STATUSES = ("active", "ready")

# LANE ID. S2 stamps `lily_questions.lane` as `<deck>:<category>`
# (lily_bank_replenish.lily_lane_id) and keys its watermark, its run
# receipts and its depth counts on it. THE DRAW DOES NOT FILTER ON THAT
# COLUMN, on purpose: it is NULL on all 448 pre-existing rows, so a draw
# that required it would serve only what S2 had authored and would have
# emptied the standing bank overnight. The draw's own `adult` + `category`
# pair IS the same key — S2 says so itself ("the replenisher back-reads a
# lane's depth by (mode, adult, category) so legacy rows count toward
# depth without being rewritten") — and this function is where the two
# spellings are reconciled, for the receipt and the health readout.
def lily_lane_key(deck, category) -> str:
    """`general` + `academic` -> `general:academic`. Mirrors S2's
    `lily_bank_replenish.lily_lane_id` exactly. Restated rather than
    imported: lily_agent imports the replenisher (the entrypoint hook),
    and importing it back from here would close a cycle."""
    return f"{str(deck or '').strip().lower()}:{str(category or '').strip()}"


# S2's run receipts, read (never written) by the health readout.
REPLENISH_RUNS_TABLE = "lily_bank_replenish_runs"


def lily_lane_categories(lane, deck: str = "general") -> list:
    """The bank `category` values a rotation lane draws from, in draw
    order, for one deck. Unknown lane -> that lane's own name (an
    operator topic draws strictly under its own label, HOTFIX-006 N2).

    Each list ends with the lane's OWN family name where that is not
    already a bank category, because that is the `category` S2 writes:
    its lanes are `<deck>:<family>`, so a replenished potpourri row lands
    as category='lifestyle-potpourri' — a value no pre-WO bank row has
    ever carried. Leaving it off would have made every row S2 authored
    for that lane undrawable."""
    entry = LANE_BANK_CATEGORIES.get(str(lane or ""))
    if entry is None:
        name = str(lane or "").strip()
        return [name] if name else []
    deck_key = "adult" if str(deck or "").lower() == "adult" else "general"
    return list(entry[deck_key])


def lily_lane_for_category(category) -> str:
    """The rotation lane a bank category belongs to — the declared inverse
    of lily_lane_categories, and the grouping key of the health readout.
    Anything unlisted (an operator topic, a category promoted since this
    table was written) is potpourri, which is what potpourri is for."""
    name = str(category or "").strip()
    for lane, decks in LANE_BANK_CATEGORIES.items():
        for categories in decks.values():
            if name in categories:
                return lane
    return POTPOURRI_LANE


# ---------------------------------------------------------------------------
# Normalization + hashing (pure)
# ---------------------------------------------------------------------------

_NON_ALNUM_RE = re.compile(r"[^a-z0-9\s]+")
_WS_RE = re.compile(r"\s+")


def lily_normalize_question_text(text) -> str:
    """Canonical dedup form: lowercase, punctuation stripped, whitespace
    collapsed. 'Which sea, colorful, sits between Europe & Asia?' and
    'which sea colorful sits between europe  asia' normalize identically."""
    t = str(text or "").lower()
    t = _NON_ALNUM_RE.sub(" ", t)
    t = _WS_RE.sub(" ", t).strip()
    return t


def lily_question_text_hash(text) -> str:
    """sha1 hex of the normalized question text — the exact-dup key and
    the lily_asked_history.question_text_hash value."""
    return hashlib.sha1(
        lily_normalize_question_text(text).encode("utf-8")
    ).hexdigest()


def lily_find_duplicate(
    prompt,
    category,
    existing_rows,
    ratio: float = DUP_FUZZY_RATIO,
) -> Optional[dict]:
    """Near-dup detection against existing bank rows
    ([{id, question, category}]). Returns the first matching row (with a
    'match' key: 'exact' | 'fuzzy') or None.

      - exact: identical normalized-text hash, ANY category (the same
        question filed under two categories is still the same question);
      - fuzzy: difflib.SequenceMatcher ratio >= `ratio` on the normalized
        texts, SAME category only (cross-category fuzzy hits are usually
        different facts that share sentence furniture)."""
    norm = lily_normalize_question_text(prompt)
    if not norm:
        return None
    cat = str(category or "").strip().lower()
    for row in existing_rows or []:
        existing_norm = lily_normalize_question_text((row or {}).get("question"))
        if not existing_norm:
            continue
        if existing_norm == norm:
            return {**row, "match": "exact"}
        if str((row or {}).get("category") or "").strip().lower() != cat:
            continue
        if difflib.SequenceMatcher(None, norm, existing_norm).ratio() >= ratio:
            return {**row, "match": "fuzzy"}
    return None


# ---------------------------------------------------------------------------
# Asked-history sets (pure)
# ---------------------------------------------------------------------------

def lily_history_answers(rows) -> set:
    """Normalized canonical answers from lily_asked_history rows
    (migration 017; pre-017 rows lack the column and contribute
    nothing). The answer-level no-repeat key: a regenerated 'what metal
    is Au?' has a fresh text hash every time, but its answer is always
    gold."""
    import lily_evaluation
    out = set()
    for r in rows or []:
        ans = (r or {}).get("canonical_answer")
        if ans:
            norm = lily_evaluation.lily_normalize_answer(str(ans))
            if norm:
                out.add(norm)
    return out


def lily_history_hashes(rows) -> set:
    """question_text_hash set from lily_asked_history rows."""
    return {
        h for h in (
            (r or {}).get("question_text_hash") for r in rows or []
        ) if h
    }


def lily_history_question_ids(rows) -> set:
    """question_id set from lily_asked_history rows (kb_ ids for bank
    rows, q_ ids for generated ones)."""
    return {
        q for q in (
            (r or {}).get("question_id") for r in rows or []
        ) if q
    }


# ---------------------------------------------------------------------------
# Category-proposal gating (pure, sub-agent F)
# ---------------------------------------------------------------------------

def lily_normalize_category_name(name) -> str:
    return _WS_RE.sub(" ", str(name or "").strip().lower())


def lily_category_promotion_ready(use_count, group_count) -> bool:
    """Promotion gate: a proposed category graduates only after
    use_count >= 10 across >= 3 distinct groups."""
    return (
        int(use_count or 0) >= CATEGORY_PROMOTE_MIN_USES
        and int(group_count or 0) >= CATEGORY_PROMOTE_MIN_GROUPS
    )


def lily_apply_category_proposal(existing_row, name, family, group_id) -> dict:
    """Pure upsert-payload builder: bump use_count, add group_id to the
    distinct-groups list. `existing_row` is the current
    lily_category_candidates row or None."""
    row = existing_row or {}
    groups = [g for g in (row.get("groups") or []) if g]
    gid = str(group_id or "").strip()
    if gid and gid not in groups:
        groups.append(gid)
    return {
        "name": lily_normalize_category_name(name),
        "family": row.get("family") or family,
        "use_count": int(row.get("use_count") or 0) + 1,
        "groups": groups,
    }


# ---------------------------------------------------------------------------
# Supabase I/O — fire-and-forget, LILY_BANK | markers, never raises
# ---------------------------------------------------------------------------

async def lily_bank_generated_question(
    supabase,
    question: dict,
) -> Optional[int]:
    """Bank one VERIFIED generated question into lily_questions with
    near-dup detection at insert time (banking-on-generation — this is
    where the bank self-grows). Returns the new row id, or None when the
    question was a dup (LILY_BANK | DUP_DISCARDED) or the write failed."""
    if supabase is None or not isinstance(question, dict):
        return None
    prompt = str(question.get("prompt") or "").strip()
    if not prompt:
        return None
    category = question.get("category") or "potpourri"
    try:
        existing = await asyncio.to_thread(
            lambda: supabase.table("lily_questions")
            .select("id, question, category")
            .execute()
        )
        dup = lily_find_duplicate(prompt, category, existing.data or [])
        if dup is not None:
            logger.info(
                "LILY_BANK | DUP_DISCARDED | match=%s existing_id=%s "
                "category=%s prompt=%r",
                dup.get("match"), dup.get("id"), category, prompt[:80],
            )
            return None
        acceptable = question.get("acceptable_answers")
        if not isinstance(acceptable, list) or not acceptable:
            acceptable = [str(question.get("canonical_answer") or "").lower()]
        payload = {
            "mode": "adult",
            "category": category,
            "question": prompt,
            "canonical_answer": str(question.get("canonical_answer") or ""),
            "acceptable_answers": [str(a) for a in acceptable],
            "difficulty_tier": int(question.get("difficulty_tier") or 2),
            "reveal_color": question.get("reveal_color") or "",
            "source": "generated",
            "adult": True,
            "status": "active",
        }

        def _insert(p):
            return supabase.table("lily_questions").insert(p).execute()

        try:
            result = await asyncio.to_thread(_insert, payload)
        except Exception as e:
            # Migration-lag tolerance (lily_memory pattern): a schema
            # without the live `mode` column must still bank the row.
            if "mode" in str(e):
                payload = {k: v for k, v in payload.items() if k != "mode"}
                result = await asyncio.to_thread(_insert, payload)
            else:
                raise
        data = getattr(result, "data", None) or []
        row_id = data[0].get("id") if data and isinstance(data[0], dict) else None
        logger.info(
            "LILY_BANK | BANKED | id=%s category=%s tier=%s mode=%s prompt=%r",
            row_id, category, payload.get("difficulty_tier"),
            payload.get("mode"), prompt[:80],
        )
        return row_id
    except Exception as e:
        # Cardinal Rule (no memory is bad memory): a failed bank write must
        # never silently drop a generated question — log the COMPLETE
        # category+question payload so it can be recovered later, not just
        # the 80-char prompt head.
        try:
            recover = json.dumps(payload, default=str)
        except Exception:
            recover = repr(payload)
        logger.error(
            "LILY_BANK | BANK_FAILED | RECOVERY_PAYLOAD | error=%s payload=%s",
            e, recover,
        )
        return None


async def lily_record_asked(
    supabase,
    group_id: str,
    question: dict,
    session_id: str,
) -> bool:
    """One lily_asked_history row per question SERVED (armed into the
    state block for delivery). Fire-and-forget at the call site.

    WO-LILY-SUPPLY-001 S1: returns True only when the row actually
    landed. This is the durable half of the group no-repeat ledger — the
    ONLY thing that stops the next session re-serving what this one asked
    — and a bank-first game leans on it far harder than a generate-first
    one did, because with the bank serving, a lost row is a guaranteed
    repeat rather than a merely likely one. Fleet S2: the return value
    exists so a failure is a stated fact, not an inference from a log
    line nobody reads.

    The row carries the question's CATEGORY (migration 023,
    WO-LILY-HOTFIX-006 N2). Session lily-16A9AE was narrated as a custom
    Cape Cod round and its six ledger rows were generic curated questions;
    the category on the row is what makes "the round she promised was
    actually built" a fact the ledger can answer, per group and per
    session, instead of something reconstructed from question text."""
    if supabase is None or not group_id or not isinstance(question, dict):
        return False
    payload = {
        "group_id": group_id,
        "question_id": question.get("id"),
        "question_text_hash": lily_question_text_hash(
            question.get("prompt")
        ),
        # Answer-level no-repeat (migration 017): reworded
        # regenerations of the same fact share this key.
        "canonical_answer": str(
            question.get("canonical_answer") or ""
        )[:200] or None,
        "category": str(question.get("category") or "")[:120] or None,
        "session_id": session_id,
        "asked_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        try:
            await asyncio.to_thread(
                lambda: supabase.table("lily_asked_history")
                .insert(payload).execute()
            )
        except Exception as e:
            # Migration-lag tolerance (the lily_register_operator_category
            # pattern): a database still on the pre-023 schema must keep
            # REGISTERING served questions — losing the whole no-repeat
            # ledger to gain one audit column would be a bad trade.
            if "category" not in str(e):
                raise
            trimmed = {k: v for k, v in payload.items() if k != "category"}
            await asyncio.to_thread(
                lambda: supabase.table("lily_asked_history")
                .insert(trimmed).execute()
            )
            logger.info(
                "LILY_BANK | ASKED_RECORDED | group=%s session=%s "
                "question_id=%s (pre-023 schema — category skipped)",
                group_id, session_id, question.get("id"),
            )
            return True
        logger.info(
            "LILY_BANK | ASKED_RECORDED | group=%s session=%s question_id=%s "
            "category=%s",
            group_id, session_id, question.get("id"), payload["category"],
        )
        return True
    except Exception as e:
        # Cardinal Rule (no memory is bad memory): a lost asked-history row
        # is a repeat the table will hear next session, so the payload is
        # logged whole and recoverable, not summarised.
        try:
            recover = json.dumps(payload, default=str)
        except Exception:
            recover = repr(payload)
        logger.error(
            "LILY_BANK | ASKED_RECORD_FAILED | RECOVERY_PAYLOAD | group=%s "
            "error=%s payload=%s", group_id, e, recover,
        )
        return False


def _empty_lane_health() -> dict:
    return {
        "ready": 0,
        "active": 0,
        "servable": 0,
        "burned": 0,
        "last_replenished_at": None,
        "rejection_rate": None,
    }


async def lily_bank_health(supabase) -> dict:
    """Per-lane bank health (WO-LILY-SUPPLY-001 S1 deliverable 4).

    Returns {lane: {ready, active, servable, burned, last_replenished_at,
    rejection_rate}} for EVERY lane in the rotation, always all four, so an
    empty lane is a stated zero rather than a missing key. With the bank
    serving the delivery path, what starves a table is a lane running out —
    not the bank as a whole — and this is the readout that names which one.

      ready     rows the S2 replenisher has banked and nobody has served
                (status='ready');
      active    the standing bank's own rows (status='active');
      servable  ready + active — what the draw can actually reach;
      burned    retired by WS-4 (status='burned').

    `last_replenished_at` is the newest `lily_questions.replenished_at` in
    the lane (migration 029, stamped only by a replenishment run) and
    `rejection_rate` is (skipped_duplicate + rejected_verify +
    rejected_moderation) / authored_count over the most recent COMPLETED
    run per S2 lane inside this rotation lane
    (`lily_bank_replenish_runs`). Both stay None — never 0 — while S2 has
    not run: "not measured" and "measured as zero" are different claims
    and only one of them is true. S1 never writes either surface; a
    missing column or a missing table is a null, not an error."""
    lanes = {lane: _empty_lane_health() for lane in LANE_BANK_CATEGORIES}
    if supabase is None:
        return lanes
    try:
        try:
            result = await asyncio.to_thread(
                lambda: supabase.table("lily_questions")
                .select("id, category, adult, status, replenished_at")
                .execute()
            )
        except Exception as e:
            # Migration-lag tolerance (the lily_register_operator_category
            # pattern): a database still on the pre-029 schema has no
            # replenished_at, and the ready/active/burned counts are worth
            # more than the stamp.
            if "replenished_at" not in str(e):
                raise
            result = await asyncio.to_thread(
                lambda: supabase.table("lily_questions")
                .select("id, category, adult, status")
                .execute()
            )
        for row in (result.data or []):
            row = row or {}
            lane = lily_lane_for_category(row.get("category"))
            bucket = lanes.setdefault(lane, _empty_lane_health())
            status = str(row.get("status") or "active")
            if status in BANK_SERVABLE_STATUSES:
                bucket[status] = bucket.get(status, 0) + 1
                bucket["servable"] += 1
            elif status == "burned":
                bucket["burned"] += 1
            stamp = row.get("replenished_at")
            if stamp and (
                bucket["last_replenished_at"] is None
                or str(stamp) > str(bucket["last_replenished_at"])
            ):
                bucket["last_replenished_at"] = stamp
    except Exception as e:
        logger.error("LILY_BANK | HEALTH_READ_FAILED | error=%s", e)
        return lanes
    # S2's run receipts. An absent table (S2 not landed, or migration 029
    # not applied) leaves rejection_rate null, which is the honest answer.
    try:
        runs = await asyncio.to_thread(
            lambda: supabase.table(REPLENISH_RUNS_TABLE)
            .select(
                "lane, status, authored_count, accepted_count, "
                "skipped_duplicate, rejected_verify, rejected_moderation, "
                "started_at"
            )
            .eq("status", "completed")
            .order("started_at", desc=True)
            .limit(200)
            .execute()
        )
        latest: dict = {}
        for row in (runs.data or []):
            s2_lane = str((row or {}).get("lane") or "")
            if s2_lane and s2_lane not in latest:
                # Ordered newest-first, so the first sighting of an S2 lane
                # IS its most recent completed run.
                latest[s2_lane] = row
        totals: dict = {}
        for s2_lane, row in latest.items():
            _, category = (s2_lane.split(":", 1) + [""])[:2]
            bucket_lane = lily_lane_for_category(category)
            authored, rejected = totals.get(bucket_lane, (0, 0))
            authored += int(row.get("authored_count") or 0)
            rejected += (
                int(row.get("skipped_duplicate") or 0)
                + int(row.get("rejected_verify") or 0)
                + int(row.get("rejected_moderation") or 0)
            )
            totals[bucket_lane] = (authored, rejected)
        for bucket_lane, (authored, rejected) in totals.items():
            bucket = lanes.get(bucket_lane)
            if bucket is None or not authored:
                continue
            bucket["rejection_rate"] = round(rejected / authored, 4)
    except Exception as e:
        logger.info(
            "LILY_BANK | REPLENISH_RUNS_UNAVAILABLE | error=%s "
            "(no completed replenishment run to read a rejection rate from)",
            e,
        )
    logger.info(
        "LILY_BANK | HEALTH | %s",
        " ".join(
            f"{lane}:servable={v['servable']}(ready={v['ready']},"
            f"active={v['active']}),burned={v['burned']}"
            for lane, v in sorted(lanes.items())
        ),
    )
    return lanes


async def lily_load_asked_history(
    supabase,
    group_id: str,
    limit: int = ASKED_HISTORY_LIMIT,
) -> list:
    """The group's served-question history (most recent first):
    [{question_id, question_text_hash, canonical_answer}]. Bank draws and
    generated output are checked against these so a returning table never
    hears a repeat — by id, by normalized text hash, and (migration 017)
    by canonical ANSWER, which catches reworded regenerations of the same
    fact."""
    if supabase is None or not group_id:
        return []
    try:
        result = await asyncio.to_thread(
            lambda: supabase.table("lily_asked_history")
            .select("question_id, question_text_hash, canonical_answer")
            .eq("group_id", group_id)
            .order("asked_at", desc=True)
            .limit(limit)
            .execute()
        )
        rows = result.data or []
        if rows:
            logger.info(
                "LILY_BANK | HISTORY_LOADED | group=%s rows=%d",
                group_id, len(rows),
            )
        return rows
    except Exception as e:
        logger.error("LILY_BANK | HISTORY_LOAD_FAILED | group=%s error=%s",
                     group_id, e)
        return []


async def lily_record_category_proposal(
    supabase,
    name: str,
    family: str,
    group_id: str,
) -> None:
    """Tally one category proposal (sub-agent F): upsert
    lily_category_candidates with use_count+1 and the distinct group.
    Fire-and-forget; read-modify-write is tolerable at proposal rates."""
    norm = lily_normalize_category_name(name)
    if supabase is None or not norm:
        return
    try:
        existing = await asyncio.to_thread(
            lambda: supabase.table("lily_category_candidates")
            .select("name, family, use_count, groups")
            .eq("name", norm)
            .maybe_single()
            .execute()
        )
        row = getattr(existing, "data", None) if existing else None
        payload = lily_apply_category_proposal(row, norm, family, group_id)
        await asyncio.to_thread(
            lambda: supabase.table("lily_category_candidates")
            .upsert(payload, on_conflict="name")
            .execute()
        )
        logger.info(
            "LILY_BANK | CATEGORY_PROPOSED | name=%s family=%s use_count=%d "
            "groups=%d promoted=%s",
            norm, payload.get("family"), payload["use_count"],
            len(payload["groups"]),
            lily_category_promotion_ready(
                payload["use_count"], len(payload["groups"])
            ),
        )
    except Exception as e:
        logger.error("LILY_BANK | CATEGORY_PROPOSAL_FAILED | name=%s error=%s",
                     norm, e)


def lily_apply_operator_category(existing_row, name, family, group_id) -> dict:
    """Pure upsert-payload builder for an OPERATOR-requested category
    (WO-LILY-CAPABILITY-RESTORE-001). Stronger provenance than a model
    proposal: latches operator_requested=True so it is first-class
    immediately and never waits on the use_count/group promotion gate.
    Still bumps use_count and records the group like a proposal."""
    row = existing_row or {}
    groups = [g for g in (row.get("groups") or []) if g]
    gid = str(group_id or "").strip()
    if gid and gid not in groups:
        groups.append(gid)
    norm = lily_normalize_category_name(name)
    return {
        "name": norm,
        "family": row.get("family") or family or norm,
        "use_count": int(row.get("use_count") or 0) + 1,
        "groups": groups,
        "operator_requested": True,
    }


async def lily_register_operator_category(
    supabase, name: str, family: str, group_id: str,
) -> bool:
    """Register an operator-requested category as first-class in
    lily_category_candidates (idempotent upsert by name — no duplicate for
    a category that already exists). Returns True on write. Never raises:
    the live round must serve even if this fails, and a failed write logs
    the full payload for recovery (Cardinal Rule — no memory is bad
    memory). Fire-and-forget from the tool; read-modify-write is fine at
    operator-request rates."""
    norm = lily_normalize_category_name(name)
    if supabase is None or not norm:
        return False
    payload = None
    try:
        existing = await asyncio.to_thread(
            lambda: supabase.table("lily_category_candidates")
            .select("name, family, use_count, groups, operator_requested")
            .eq("name", norm)
            .maybe_single()
            .execute()
        )
        row = getattr(existing, "data", None) if existing else None
        payload = lily_apply_operator_category(row, norm, family, group_id)
        await asyncio.to_thread(
            lambda: supabase.table("lily_category_candidates")
            .upsert(payload, on_conflict="name")
            .execute()
        )
        logger.info(
            "LILY_BANK | OPERATOR_CATEGORY_REGISTERED | name=%s family=%s "
            "use_count=%d groups=%d",
            norm, payload.get("family"), payload["use_count"],
            len(payload["groups"]),
        )
        return True
    except Exception as e:
        # Migration-lag tolerance: a pre-020 schema without
        # operator_requested must still register the category (drop the
        # flag and retry as a proposal-shaped row).
        if payload is not None and "operator_requested" in str(e):
            try:
                fallback = {
                    k: v for k, v in payload.items()
                    if k != "operator_requested"
                }
                await asyncio.to_thread(
                    lambda: supabase.table("lily_category_candidates")
                    .upsert(fallback, on_conflict="name")
                    .execute()
                )
                logger.info(
                    "LILY_BANK | OPERATOR_CATEGORY_REGISTERED | name=%s "
                    "(pre-020 schema — flag skipped)", norm,
                )
                return True
            except Exception as e2:
                e = e2
        try:
            recover = json.dumps(payload, default=str)
        except Exception:
            recover = repr(payload)
        logger.error(
            "LILY_BANK | OPERATOR_CATEGORY_FAILED | RECOVERY_PAYLOAD | "
            "name=%s error=%s payload=%s", norm, e, recover,
        )
        return False


def _promoted_or_operator(row) -> bool:
    """A candidate is first-class if it EARNED promotion (use_count/groups)
    OR was operator-requested (WO-LILY-CAPABILITY-RESTORE-001)."""
    return bool(row.get("operator_requested")) or lily_category_promotion_ready(
        row.get("use_count"), len(row.get("groups") or [])
    )


async def lily_load_promoted_categories(supabase) -> list:
    """Names of first-class category candidates — those PROMOTED
    (use_count >= 10 AND >= 3 distinct groups) OR operator-requested (which
    are first-class on registration). The only categories Lily may
    announce. Sorted for deterministic state-block lines."""
    if supabase is None:
        return []
    try:
        try:
            result = await asyncio.to_thread(
                lambda: supabase.table("lily_category_candidates")
                .select("name, use_count, groups, operator_requested")
                .execute()
            )
        except Exception as e:
            # Migration-lag tolerance (pre-020 schema): fall back to the
            # count-based columns only.
            if "operator_requested" not in str(e):
                raise
            result = await asyncio.to_thread(
                lambda: supabase.table("lily_category_candidates")
                .select("name, use_count, groups")
                .execute()
            )
        promoted = sorted(
            r["name"]
            for r in (result.data or [])
            if r.get("name") and _promoted_or_operator(r)
        )
        if promoted:
            logger.info(
                "LILY_BANK | CATEGORIES_PROMOTED | names=%s", ", ".join(promoted)
            )
        return promoted
    except Exception as e:
        logger.error("LILY_BANK | CATEGORY_LOAD_FAILED | error=%s", e)
        return []
