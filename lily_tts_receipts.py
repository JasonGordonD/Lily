"""WO-FLEET-LKA-171-TTD-PORT-001 — fleet_tts_events receipt writer (Lily).

Port of MinkaMoor minkamin_tts_receipts.py (9a6ee3f); only AGENT_NAME
changed. One row per speech. Fire-and-forget, 5 s timeout, never on the
voice path. Does not DELETE. The pre-provisioned
``session_id=__s3_verify_keep__`` row is left untouched.
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextvars import ContextVar
from typing import Any

import aiohttp

logger = logging.getLogger("lily_tts_receipts")

TABLE = "fleet_tts_events"
KEEP_SESSION_ID = "__s3_verify_keep__"
AGENT_NAME = "lily"
_WRITE_TIMEOUT_SECS = 5.0

_session_id: ContextVar[str] = ContextVar("tts_receipt_session_id", default="")
_node_name: ContextVar[str] = ContextVar("tts_receipt_node_name", default="")
_BG: set[asyncio.Task] = set()


def bind_session_id(session_id: str) -> None:
    _session_id.set(str(session_id or ""))


def bind_node_name(node_name: str) -> None:
    _node_name.set(str(node_name or ""))


def current_session_id() -> str:
    return _session_id.get() or ""


def current_node_name() -> str:
    return _node_name.get() or ""


def _credentials() -> tuple[str, str]:
    url = (os.getenv("SUPABASE_URL") or "").rstrip("/")
    key = (
        os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        or os.getenv("SUPABASE_SERVICE_KEY")
        or ""
    )
    return url, key


def record_tts_event(
    *,
    transport: str,
    model_id: str,
    voice_id: str,
    chars: int,
    ttfb_ms: float | None,
    total_ms: float | None,
    audio_ms: float | None,
    chunks: int,
    interrupted: bool,
    contexts_open_max: int,
    error: str | None = None,
    speech_id: str | None = None,
    session_id: str | None = None,
    node_name: str | None = None,
    raw: dict[str, Any] | None = None,
) -> None:
    """Spawn a 5 s INSERT. Never raises into the caller."""
    payload = {
        "agent_name": AGENT_NAME,
        "session_id": session_id if session_id is not None else current_session_id(),
        "node_name": node_name if node_name is not None else current_node_name() or None,
        "speech_id": speech_id,
        "transport": transport,
        "model_id": model_id,
        "voice_id": voice_id,
        "chars": int(chars or 0),
        "ttfb_ms": None if ttfb_ms is None else int(round(float(ttfb_ms))),
        "total_ms": None if total_ms is None else int(round(float(total_ms))),
        "audio_ms": None if audio_ms is None else int(round(float(audio_ms))),
        "chunks": int(chunks or 0),
        "interrupted": bool(interrupted),
        "contexts_open_max": int(contexts_open_max or 0),
        "error": (error or None),
        "raw": raw,
    }
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("TTS_RECEIPT | no running loop — drop row transport=%s", transport)
        return
    task = loop.create_task(_write_row(payload))
    _BG.add(task)
    task.add_done_callback(_BG.discard)


async def _write_row(payload: dict[str, Any]) -> None:
    try:
        await asyncio.wait_for(_insert(payload), timeout=_WRITE_TIMEOUT_SECS)
    except Exception as e:  # noqa: BLE001 — never in the voice path
        logger.warning("TTS_RECEIPT | write failed: %s", e)


async def _insert(payload: dict[str, Any]) -> None:
    url, key = _credentials()
    if not url or not key:
        logger.warning("TTS_RECEIPT | skip — supabase creds unset")
        return
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    timeout = aiohttp.ClientTimeout(total=_WRITE_TIMEOUT_SECS)
    async with aiohttp.ClientSession(timeout=timeout) as http:
        async with http.post(
            f"{url}/rest/v1/{TABLE}",
            headers=headers,
            json=payload,
        ) as resp:
            if resp.status not in (200, 201, 204):
                body = (await resp.text())[:300]
                logger.warning("TTS_RECEIPT | insert HTTP %d | %s", resp.status, body)
            else:
                logger.info(
                    "TTS_RECEIPT | ok transport=%s speech_id=%s ttfb_ms=%s",
                    payload.get("transport"),
                    payload.get("speech_id"),
                    payload.get("ttfb_ms"),
                )
