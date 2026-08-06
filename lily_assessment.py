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

Runs on the reasoning model (`gemini-3.1-pro-preview`) with its own lazy
genai.Client — same HTTP-client-isolation rule as lily_reasoning (§11.5).
"""

import asyncio
import datetime
import json
import logging
import re
from typing import Awaitable, Callable, Optional

from google import genai as google_genai
from google.genai import types as genai_types

import lily_config

logger = logging.getLogger("lily_assessment")

Generate = Callable[[list, dict], Awaitable[dict]]

# Adult-product context (§11.1): same explicit safety settings as the
# reasoning node — an adult-mode transcript must not mute the desk.
_SAFETY_SETTINGS = [
    genai_types.SafetySetting(
        category=genai_types.HarmCategory.HARM_CATEGORY_HARASSMENT,
        threshold=genai_types.HarmBlockThreshold.BLOCK_NONE,
    ),
    genai_types.SafetySetting(
        category=genai_types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
        threshold=genai_types.HarmBlockThreshold.BLOCK_NONE,
    ),
    genai_types.SafetySetting(
        category=genai_types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
        threshold=genai_types.HarmBlockThreshold.BLOCK_NONE,
    ),
    genai_types.SafetySetting(
        category=genai_types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
        threshold=genai_types.HarmBlockThreshold.BLOCK_NONE,
    ),
]

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


_client: Optional[google_genai.Client] = None


def _get_client() -> google_genai.Client:
    global _client
    if _client is None:
        _client = google_genai.Client(api_key=lily_config.google_api_key())
    return _client


def _parse_assessment_json(text: str) -> dict:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in assessment output")
    parsed = json.loads(cleaned[start:end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("assessment must be a JSON object")
    return parsed


async def _default_generate(transcript: list, game_stats: dict) -> dict:
    prompt = json.dumps(
        {"transcript": transcript, "game_stats": game_stats},
        ensure_ascii=False, default=str,
    )
    config = genai_types.GenerateContentConfig(
        thinking_config=genai_types.ThinkingConfig(thinking_level="low"),
        safety_settings=_SAFETY_SETTINGS,
        # Thinking tokens count toward max_output_tokens on Gemini 3.x
        # (P1 root cause, 2026-07-14) — name a budget wide enough for both.
        max_output_tokens=4096,
        system_instruction=_SYSTEM_INSTRUCTION,
        response_mime_type="application/json",
    )
    response = await asyncio.to_thread(
        _get_client().models.generate_content,
        model=_assessment_model(),
        contents=prompt,
        config=config,
    )
    text = getattr(response, "text", None)
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


async def lily_assess_session(
    supabase,
    session_id: str,
    transcript: list,
    game_stats: dict,
    generate: Optional[Generate] = None,
) -> bool:
    """Generate + fill one session's assessment. Never raises: failure logs
    the ASSESS_FAILED alert and leaves the row pending for the sweep."""
    generate = generate or _default_generate
    try:
        assessment = await asyncio.wait_for(
            generate(transcript or [], game_stats or {}),
            timeout=report_deadline_seconds(),
        )
        filled = await lily_fill_assessment(supabase, session_id, assessment)
        logger.info(
            "LILY_REPORT | ASSESSED | session=%s filled=%s", session_id, filled
        )
        return filled
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
