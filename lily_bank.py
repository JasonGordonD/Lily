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
    mode: str,
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
            "mode": mode or "general",
            "category": category,
            "question": prompt,
            "canonical_answer": str(question.get("canonical_answer") or ""),
            "acceptable_answers": [str(a) for a in acceptable],
            "difficulty_tier": int(question.get("difficulty_tier") or 2),
            "reveal_color": question.get("reveal_color") or "",
            "source": "generated",
            # Consent-safety: adult-mode output never surfaces at a
            # general-mode table (lily_memory.lily_bank_mode_filter).
            "adult": (mode or "general") == "adult",
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
            payload.get("mode", mode), prompt[:80],
        )
        return row_id
    except Exception as e:
        logger.error("LILY_BANK | BANK_FAILED | error=%s prompt=%r", e, prompt[:80])
        return None


async def lily_record_asked(
    supabase,
    group_id: str,
    question: dict,
    session_id: str,
) -> None:
    """One lily_asked_history row per question SERVED (armed into the
    state block for delivery). Fire-and-forget."""
    if supabase is None or not group_id or not isinstance(question, dict):
        return
    try:
        await asyncio.to_thread(
            lambda: supabase.table("lily_asked_history").insert({
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
                "session_id": session_id,
                "asked_at": datetime.now(timezone.utc).isoformat(),
            }).execute()
        )
        logger.info(
            "LILY_BANK | ASKED_RECORDED | group=%s session=%s question_id=%s",
            group_id, session_id, question.get("id"),
        )
    except Exception as e:
        logger.error("LILY_BANK | ASKED_RECORD_FAILED | group=%s error=%s",
                     group_id, e)


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


async def lily_load_promoted_categories(supabase) -> list:
    """Names of PROMOTED category candidates (use_count >= 10 AND >= 3
    distinct groups) — the only proposals Lily may ever announce. Sorted
    for deterministic state-block lines."""
    if supabase is None:
        return []
    try:
        result = await asyncio.to_thread(
            lambda: supabase.table("lily_category_candidates")
            .select("name, use_count, groups")
            .execute()
        )
        promoted = sorted(
            r["name"]
            for r in (result.data or [])
            if r.get("name")
            and lily_category_promotion_ready(
                r.get("use_count"), len(r.get("groups") or [])
            )
        )
        if promoted:
            logger.info(
                "LILY_BANK | CATEGORIES_PROMOTED | names=%s", ", ".join(promoted)
            )
        return promoted
    except Exception as e:
        logger.error("LILY_BANK | CATEGORY_LOAD_FAILED | error=%s", e)
        return []
