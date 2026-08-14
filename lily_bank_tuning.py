"""
lily_bank_tuning.py — difficulty self-tuning + retirement (WO-LILY-OMNIBUS-002,
sub-agent E).

Session-end job, fire-and-forget: aggregate lily_answers per bank question
across ALL sessions, with an exposure floor of TUNE_MIN_SERVINGS servings
counted from lily_asked_history. Per question, per run (one move only):

    success > 75%  ->  difficulty_tier down one (min 1: the table finds it
                       easier than its label)
    success < 30%  ->  difficulty_tier up one (max 4)
    success < 10% or > 95%  ->  status='retired' (broken or trivial;
                       checked FIRST — retirement outranks a tier move)

Only bank rows are tunable (question_id shape 'kb_<row id>'); generated
questions acquire kb_ ids once banked, then enter the same loop. Every
decision applied logs `LILY_TUNE | ...`.

The DECISIONS are pure functions (stdlib only, offline-testable); the DB
I/O is a thin wrapper that reads three tables, computes the plan, and
applies row updates. It never raises into the session.
"""

import asyncio
import logging

logger = logging.getLogger("lily_bank_tuning")

TUNE_MIN_SERVINGS = 5        # exposure floor: servings from lily_asked_history
TUNE_TIER_DOWN_ABOVE = 0.75  # success > 75% -> one tier easier (min 1)
TUNE_TIER_UP_BELOW = 0.30    # success < 30% -> one tier harder (max 4)
TUNE_RETIRE_BELOW = 0.10     # success < 10% -> retire (broken/unfair)
TUNE_RETIRE_ABOVE = 0.95     # success > 95% -> retire (trivial)
TIER_MIN = 1
TIER_MAX = 4


# ---------------------------------------------------------------------------
# Pure decisions
# ---------------------------------------------------------------------------

def lily_tuning_decision(
    servings: int,
    correct: int,
    current_tier: int,
    status: str = "active",
) -> dict:
    """One question's tuning decision. Returns
    {action: none|retire|tier_down|tier_up, new_tier, success_rate, reason}.
    One move per run by construction — a single action, one tier step."""
    servings = int(servings or 0)
    correct = int(correct or 0)
    tier = int(current_tier or 2)
    if (status or "active") != "active":
        return {"action": "none", "new_tier": None, "success_rate": None,
                "reason": f"status={status} not tunable"}
    if servings < TUNE_MIN_SERVINGS:
        return {"action": "none", "new_tier": None, "success_rate": None,
                "reason": f"exposure floor: servings={servings} < {TUNE_MIN_SERVINGS}"}
    success = correct / servings
    # Retirement outranks tier moves.
    if success < TUNE_RETIRE_BELOW or success > TUNE_RETIRE_ABOVE:
        return {"action": "retire", "new_tier": None, "success_rate": success,
                "reason": ("nobody gets it" if success < TUNE_RETIRE_BELOW
                           else "everybody gets it")}
    if success > TUNE_TIER_DOWN_ABOVE:
        if tier <= TIER_MIN:
            return {"action": "none", "new_tier": None, "success_rate": success,
                    "reason": "easy but already at min tier"}
        return {"action": "tier_down", "new_tier": tier - 1,
                "success_rate": success, "reason": "success above 75%"}
    if success < TUNE_TIER_UP_BELOW:
        if tier >= TIER_MAX:
            return {"action": "none", "new_tier": None, "success_rate": success,
                    "reason": "hard but already at max tier"}
        return {"action": "tier_up", "new_tier": tier + 1,
                "success_rate": success, "reason": "success below 30%"}
    return {"action": "none", "new_tier": None, "success_rate": success,
            "reason": "success within band"}


def lily_aggregate_question_stats(asked_rows, answer_rows) -> dict:
    """Aggregate per question_id: servings from lily_asked_history rows
    ([{question_id}]) and correct answers from lily_answers rows
    ([{question_id, verdict}], verdict == 'correct' counts)."""
    stats: dict = {}
    for row in asked_rows or []:
        qid = (row or {}).get("question_id")
        if not qid:
            continue
        entry = stats.setdefault(qid, {"servings": 0, "correct": 0})
        entry["servings"] += 1
    for row in answer_rows or []:
        qid = (row or {}).get("question_id")
        if not qid:
            continue
        entry = stats.setdefault(qid, {"servings": 0, "correct": 0})
        if (row or {}).get("verdict") == "correct":
            entry["correct"] += 1
    return stats


def lily_tuning_plan(asked_rows, answer_rows, question_rows) -> list:
    """The full pure plan: per bank row ([{id, difficulty_tier, status}]),
    the decision plus the update payload to apply. Only rows with an
    actionable decision are returned:
    [{row_id, question_id, action, update, success_rate, servings}]."""
    stats = lily_aggregate_question_stats(asked_rows, answer_rows)
    plan = []
    for row in question_rows or []:
        row_id = (row or {}).get("id")
        if row_id is None:
            continue
        qid = f"kb_{row_id}"
        entry = stats.get(qid)
        if not entry:
            continue
        decision = lily_tuning_decision(
            entry["servings"], entry["correct"],
            row.get("difficulty_tier") or 2,
            row.get("status") or "active",
        )
        if decision["action"] == "none":
            continue
        update = (
            {"status": "retired"}
            if decision["action"] == "retire"
            else {"difficulty_tier": decision["new_tier"]}
        )
        plan.append({
            "row_id": row_id,
            "question_id": qid,
            "action": decision["action"],
            "update": update,
            "success_rate": decision["success_rate"],
            "servings": entry["servings"],
            "reason": decision["reason"],
        })
    return plan


# ---------------------------------------------------------------------------
# Thin DB I/O — session-end job, fire-and-forget, never raises
# ---------------------------------------------------------------------------

async def lily_run_bank_tuning(supabase) -> int:
    """Run one tuning pass over the whole bank. Returns the number of
    rows updated (0 on any failure). Logs `LILY_TUNE | ...` per applied
    decision plus a run summary."""
    if supabase is None:
        return 0
    try:
        asked = await asyncio.to_thread(
            lambda: supabase.table("lily_asked_history")
            .select("question_id").execute()
        )
        answers = await asyncio.to_thread(
            lambda: supabase.table("lily_answers")
            .select("question_id, verdict").execute()
        )
        questions = await asyncio.to_thread(
            lambda: supabase.table("lily_questions")
            .select("id, difficulty_tier, status").execute()
        )
        plan = lily_tuning_plan(
            asked.data or [], answers.data or [], questions.data or []
        )
        applied = 0
        for step in plan:
            try:
                await asyncio.to_thread(
                    lambda s=step: supabase.table("lily_questions")
                    .update(s["update"])
                    .eq("id", s["row_id"])
                    .execute()
                )
                applied += 1
                logger.info(
                    "LILY_TUNE | %s | question_id=%s servings=%d "
                    "success=%.2f update=%s reason=%s",
                    step["action"].upper(), step["question_id"],
                    step["servings"], step["success_rate"],
                    step["update"], step["reason"],
                )
            except Exception as e:
                logger.error(
                    "LILY_TUNE | APPLY_FAILED | question_id=%s error=%s",
                    step["question_id"], e,
                )
        logger.info(
            "LILY_TUNE | RUN_COMPLETE | candidates=%d applied=%d",
            len(plan), applied,
        )
        return applied
    except Exception as e:
        logger.error("LILY_TUNE | RUN_FAILED | error=%s", e)
        return 0
