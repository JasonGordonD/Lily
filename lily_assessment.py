"""
lily_assessment.py — LILY post-session report assessment (the "clinical desk").

WS-12 (WO-LILY-OMNIBUS-003): lily_session_reports rows were written at close
but NOTHING ever filled `assessment` — every row in production sat at
report_status='pending' (41/41 on 2026-08-05). The desk was designed but
never built. This module is that layer, with two triggers, neither of which
depends on the shutdown/close path (fleet: shutdown callbacks fire in 0-22%
of sessions):

  - Wrap-up beat: `lily_wrap_up_report` runs at finish_game — writes the
    report row (idempotent with the close-path upsert) and fills the
    assessment immediately.
  - Reconciliation sweep: `lily_report_sweep` runs at session start —
    assesses orphaned pending rows (aborted sessions, past failures) from
    their STORED transcript/game_stats.

Failure discipline (§11.2, fail-visible): an assessment failure logs
`LILY_REPORT | ASSESS_FAILED` at ERROR and leaves the row pending, so the
sweep retries it — never a silent no-op, never a clobbered row. The fill is
pending-guarded (UPDATE ... WHERE report_status='pending'), so a completed
assessment is never overwritten by a re-run, matching the write side's
deliberate omission of these columns.

Runs offline on Grok 4.5 High; no live-turn latency impact.
"""

import asyncio
import datetime
import json
import logging
import re
from typing import Awaitable, Callable, Optional

import lily_config
import lily_reasoning

logger = logging.getLogger("lily_assessment")

Generate = Callable[[list, dict], Awaitable[dict]]

_SYSTEM_INSTRUCTION = (
    "You are the PRMPT clinical desk reviewing one completed Lily trivia "
    "session (Lily is a voice trivia host for a group at a table). From the "
    "transcript and game stats, produce a JSON assessment object with "
    "exactly these keys: summary (2-3 sentences on how the session went), "
    "group_dynamics (who drove, crosstalk, mood arc), per_player (object "
    "keyed by player name: engagement + one observation each), "
    "host_performance (pacing, adjudication fairness, missed moments), "
    "flags (array of anything a human should review: disputes, distress, "
    "safety-relevant moments; empty array if none). Ground every claim in "
    "the transcript — never invent players or events."
)

def report_deadline_seconds() -> float:
    """M-minute exit bar (default 5): wrap-up beat -> assessed report."""
    return lily_config.report_deadline_seconds()


def _sweep_min_age_seconds() -> float:
    # Grace window so the sweep never races a live session's own wrap-up
    # beat (both are pending-guarded, but the beat has fresher transcript).
    return lily_config.report_sweep_min_age_seconds()


def _sweep_limit() -> int:
    return lily_config.report_sweep_limit()


def _assessment_model() -> str:
    return lily_config.assessment_model()


class AssessmentParseError(ValueError):
    """The model's output could not be parsed into an assessment object.
    A DETERMINISTIC failure for a given text (trailing prose, truncation,
    non-object) — distinct from a transient failure (timeout, empty
    candidate, network). The sweep terminalizes on this so it stops
    re-running a permanent error (HOTFIX-005 X11)."""


def _parse_assessment_json(text: str) -> dict:
    """Lenient extraction (HOTFIX-005 X11): the model appended prose after
    the JSON object ("Extra data: line 13 column 1"), and the old
    find/rfind slice grabbed the LAST '}' — which lands inside trailing
    prose when a stray brace appears there. Parse the FIRST complete JSON
    object with raw_decode from the first '{' instead: it consumes exactly
    one value and ignores everything after it, so trailing prose can never
    poison the parse."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    start = cleaned.find("{")
    if start == -1:
        raise AssessmentParseError("no JSON object in assessment output")
    try:
        parsed, _end = json.JSONDecoder().raw_decode(cleaned[start:])
    except json.JSONDecodeError as e:
        raise AssessmentParseError(f"unparseable assessment JSON: {e}") from e
    if not isinstance(parsed, dict):
        raise AssessmentParseError("assessment must be a JSON object")
    return parsed


async def _default_generate(transcript: list, game_stats: dict) -> dict:
    prompt = json.dumps(
        {"transcript": transcript, "game_stats": game_stats},
        ensure_ascii=False, default=str,
    )
    reasoning = lily_reasoning.LilyReasoning.__new__(
        lily_reasoning.LilyReasoning
    )
    text = await reasoning._generate_grok_json(
        prompt,
        system_instruction=_SYSTEM_INSTRUCTION,
        max_tokens=4096,
        timeout=60.0,
        model=_assessment_model(),
        effort=lily_config.assessment_effort(),
    )
    if not text:
        raise RuntimeError(f"empty candidate from {_assessment_model()}")
    return _parse_assessment_json(text)


async def lily_fill_assessment(supabase, session_id: str, assessment: dict) -> bool:
    """Pending-guarded fill: sets assessment + report_status='complete' only
    while the row is still pending. Returns True iff a row was filled."""
    if supabase is None:
        return False
    result = await asyncio.to_thread(
        lambda: supabase.table("lily_session_reports")
        .update({"assessment": assessment, "report_status": "complete"})
        .eq("session_id", session_id)
        .eq("report_status", "pending")
        .execute()
    )
    return bool(result.data)


async def lily_mark_report_failed(
    supabase, session_id: str, reason: str
) -> bool:
    """Terminalize a pending row (HOTFIX-005 X11): report_status='failed'
    so the sweep's pending filter excludes it and a deterministic parse
    error stops re-running forever. Pending-guarded like the fill, so a
    row that later completed is never regressed. `report_error` carries the
    reason for the desk (best-effort; dropped if the column is absent)."""
    if supabase is None:
        return False

    def _update(payload):
        return (
            supabase.table("lily_session_reports")
            .update(payload)
            .eq("session_id", session_id)
            .eq("report_status", "pending")
            .execute()
        )

    payload = {"report_status": "failed", "report_error": reason[:500]}
    try:
        result = await asyncio.to_thread(lambda: _update(payload))
    except Exception:
        # report_error column may not exist yet (migration pending) — the
        # terminal status is what matters; retry without the note.
        result = await asyncio.to_thread(
            lambda: _update({"report_status": "failed"})
        )
    return bool(result.data)


async def lily_assess_session(
    supabase,
    session_id: str,
    transcript: list,
    game_stats: dict,
    generate: Optional[Generate] = None,
) -> bool:
    """Generate + fill one session's assessment. Never raises.

    Failure discipline (HOTFIX-005 X11): a TRANSIENT failure (timeout,
    empty candidate, network) logs ASSESS_FAILED and leaves the row pending
    for the sweep. A DETERMINISTIC parse failure gets ONE repair retry
    (a fresh generation), and if that also fails to parse the row is
    TERMINALIZED (report_status='failed') so the sweep stops re-running a
    permanent error instead of looping on it forever."""
    generate = generate or _default_generate

    async def _generate_once() -> dict:
        return await asyncio.wait_for(
            generate(transcript or [], game_stats or {}),
            timeout=report_deadline_seconds(),
        )

    try:
        try:
            assessment = await _generate_once()
        except AssessmentParseError as first:
            # One repair retry — the model may append prose nondeterministically;
            # a second draw often lands clean.
            logger.warning(
                "LILY_REPORT | ASSESS_PARSE_RETRY | session=%s error=%s",
                session_id, first,
            )
            assessment = await _generate_once()
        filled = await lily_fill_assessment(supabase, session_id, assessment)
        logger.info(
            "LILY_REPORT | ASSESSED | session=%s filled=%s", session_id, filled
        )
        return filled
    except AssessmentParseError as e:
        # Deterministic after a repair retry — terminalize so the sweep
        # stops looping on a permanent error.
        terminal = await lily_mark_report_failed(
            supabase, session_id, f"parse: {e}"
        )
        logger.error(
            "LILY_REPORT | ASSESS_FAILED_TERMINAL | session=%s error=%s "
            "terminalized=%s — permanent parse failure; sweep will not retry",
            session_id, e, terminal,
        )
        return False
    except Exception as e:
        logger.error(
            "LILY_REPORT | ASSESS_FAILED | session=%s error=%s — row stays "
            "pending; the reconciliation sweep will retry it",
            session_id, e,
        )
        return False


async def lily_wrap_up_report(
    supabase,
    session_id: str,
    group_id: str,
    transcript: list,
    game_stats: dict,
    generate: Optional[Generate] = None,
) -> None:
    """Wrap-up-beat trigger: write the report row NOW (idempotent upsert on
    session_id, same shape as the close-path write) and assess it. The
    close path may still re-upsert a fuller transcript later; its payload
    omits assessment/report_status so the fill is never clobbered."""
    if supabase is None:
        return
    try:
        # Local import dodges nothing — lily_persistence has no heavy deps —
        # but keeps the module graph acyclic (persistence never imports us).
        import lily_persistence
        await lily_persistence.lily_write_session_report(
            supabase,
            session_id=session_id,
            group_id=group_id,
            transcript=transcript,
            game_stats=game_stats,
        )
        await lily_assess_session(
            supabase, session_id, transcript, game_stats, generate=generate
        )
    except Exception as e:
        logger.error(
            "LILY_REPORT | WRAPUP_REPORT_FAILED | session=%s error=%s",
            session_id, e,
        )


async def lily_report_sweep(
    supabase,
    generate: Optional[Generate] = None,
    min_age_s: Optional[float] = None,
    limit: Optional[int] = None,
) -> dict:
    """Reconciliation sweep for orphaned pending rows (sessions whose
    wrap-up beat never ran or whose assessment failed). Assesses from the
    STORED transcript/game_stats. Never raises; returns counts."""
    stats = {"scanned": 0, "assessed": 0, "failed": 0}
    if supabase is None:
        return stats
    try:
        min_age = min_age_s if min_age_s is not None else _sweep_min_age_seconds()
        cutoff = (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(seconds=min_age)
        ).isoformat()
        result = await asyncio.to_thread(
            lambda: supabase.table("lily_session_reports")
            .select("session_id, transcript, game_stats")
            .eq("report_status", "pending")
            .lt("created_at", cutoff)
            .order("created_at")
            .limit(limit if limit is not None else _sweep_limit())
            .execute()
        )
        rows = result.data or []
        for row in rows:
            stats["scanned"] += 1
            ok = await lily_assess_session(
                supabase, row["session_id"],
                row.get("transcript") or [], row.get("game_stats") or {},
                generate=generate,
            )
            stats["assessed" if ok else "failed"] += 1
        if stats["scanned"] or stats["failed"]:
            logger.info(
                "LILY_REPORT | SWEEP | scanned=%d assessed=%d failed=%d",
                stats["scanned"], stats["assessed"], stats["failed"],
            )
    except Exception as e:
        logger.error("LILY_REPORT | SWEEP_FAILED | error=%s", e)
    return stats
