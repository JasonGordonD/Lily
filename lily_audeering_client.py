"""
lily_audeering_client.py — async audEERING devAIce Web API client + capture
pipeline for Lily (WO-LILY-AUDEERING-001; native lift of
mjrvs_audeering_client.py + the AudeeringSentimentPipeline half of
mjrvs_audeering_injection.py).

Surface: devAIce Web API v4.9.0
  - Upload  : POST https://devaice-web-api.audeering.com/api/v2/upload
  - Result  : GET  https://devaice-web-api.audeering.com/api/v2/uploads/{id}/result
  - Account : GET  https://devaice-web-api.audeering.com/api/v2/account-info

Fail-soft contract (JRVS D-cross, carried verbatim):
  - Missing AUDEERING_API_KEY -> circuit breaker opens: one structured log
    line, uploads disabled, the session runs unaffected (best-effort).
  - Every error path returns None; acoustic capture NEVER gates the voice
    pipeline and is never awaited on a turn.
  - Timeout retries are forbidden (a timed-out upload may still complete
    server-side and burn quota). 429s honour Retry-After with a capped
    backoff, then log-and-drop.

Billing is audio-seconds, not per-module (JRVS Probe-C Q1): the full module
set below is 1× quota.
"""

from __future__ import annotations

import asyncio
import enum
import json
import logging
import struct
import time
from typing import Any

import lily_config
import lily_audeering_consumers

try:  # pure-logic tests run without aiohttp installed
    import aiohttp
except ModuleNotFoundError:  # pragma: no cover
    aiohttp = None  # type: ignore[assignment]

try:
    from livekit import rtc  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - unit tests without LiveKit
    rtc = None  # type: ignore[assignment]

logger = logging.getLogger("lily_audeering.client")

AUDEERING_UPLOAD_URL = "https://devaice-web-api.audeering.com/api/v2/upload"
AUDEERING_RESULT_URL = "https://devaice-web-api.audeering.com/api/v2/uploads/{upload_id}/result"
AUDEERING_ACCOUNT_INFO_URL = "https://devaice-web-api.audeering.com/api/v2/account-info"
AUDEERING_API_VERSION = "4.9.0"

_UPLOAD_SEMAPHORE = asyncio.Semaphore(4)
_UPLOAD_DISABLED_REASON: str | None = None

# Defensive 429 / Retry-After (JRVS D-cross): honour the header when present,
# else exponential backoff, hard cap, then log-and-skip.
_RETRY_MAX_ATTEMPTS = 4
_RETRY_BACKOFF_SEQUENCE_S = (0.25, 0.5, 1.0, 2.0)
_RETRY_CAP_S = 4.0

# Upload config — EXACTLY this module set (WO-LILY-AUDEERING-001 Task 1).
# `asr` and `speakerVerification` are EXCLUDED. `features` is NOT requested
# (JRVS carried it as legacy; Lily starts clean). speakerAttributesModel
# "large" is MANDATORY — the small model returns null child scores on short
# segments and would starve the safety ladder. scene.outputSubScene drives
# the small/medium/large-indoor hosting calibration; ONE classification per
# capture window (no continuous windowing), which is why the window must
# stay >=5s (scene model is optimized for >5s).
_CONFIG_MODULES = {
    "expression": {"expressionModel": "large"},
    "prosody": {},
    "audioQuality": {},
    "aed": {},
    "scene": {"outputSubScene": True},
    "speakerAttributes": {"speakerAttributesModel": "large"},
}

_CONFIG = {
    "timeout": 20000,
    "apiVersion": AUDEERING_API_VERSION,
    "modules": _CONFIG_MODULES,
}


def _parse_retry_after(header_val: str | None) -> float | None:
    if not header_val:
        return None
    try:
        return max(0.0, min(float(header_val.strip()), _RETRY_CAP_S))
    except ValueError:
        return None  # HTTP-date form: fall back to the backoff sequence


async def _sleep_for_retry(attempt: int, retry_after: float | None) -> None:
    if retry_after is not None:
        await asyncio.sleep(retry_after)
        return
    idx = min(attempt, len(_RETRY_BACKOFF_SEQUENCE_S) - 1)
    await asyncio.sleep(min(_RETRY_BACKOFF_SEQUENCE_S[idx], _RETRY_CAP_S))


def _disable_uploads(reason: str) -> None:
    global _UPLOAD_DISABLED_REASON
    if _UPLOAD_DISABLED_REASON is None:
        _UPLOAD_DISABLED_REASON = reason
        logger.error("LILY_AUDEERING | uploads disabled for process: %s", reason)


def uploads_disabled_reason() -> str | None:
    return _UPLOAD_DISABLED_REASON


# ---------------------------------------------------------------------------
# Response parsing — normalize devAIce JSON to the stable consumer shape
# ---------------------------------------------------------------------------

def _float_or_none(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_float_dict(source: Any, keys: tuple[str, ...]) -> dict[str, float]:
    data = source if isinstance(source, dict) else {}
    out: dict[str, float] = {}
    for key in keys:
        val = _float_or_none(data.get(key))
        if val is not None:
            out[key] = val
    return out


def _first_dict(*values: Any) -> dict[str, Any]:
    for value in values:
        if isinstance(value, dict):
            return value
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    return item
    return {}


def _parse_speaker_segments(speaker_src: Any) -> list[dict[str, Any]]:
    """Per-VAD-segment speakerAttributes results.

    Schema: {gender: {female, male, child} summing to 1, age: number|null}.
    devAIce returns one result per VAD speech segment — a young voice
    produces its own segment-level scores, and the ladder's sustained-N
    streak counts segments. NULL-SAFE: a segment with a null/absent child
    score is kept with child=None so the ladder can skip it (neither
    advancing nor resetting the streak)."""
    if isinstance(speaker_src, dict):
        segments = [speaker_src]
    elif isinstance(speaker_src, list):
        segments = [s for s in speaker_src if isinstance(s, dict)]
    else:
        return []
    out: list[dict[str, Any]] = []
    for seg in segments:
        gender = seg.get("gender") if isinstance(seg.get("gender"), dict) else {}
        child = _float_or_none(gender.get("child"))
        if child is None:
            # Tolerate flat exports ({"child": 0.9, ...}).
            child = _float_or_none(seg.get("child"))
        out.append({
            "child": child,
            "female": _float_or_none(gender.get("female")),
            "male": _float_or_none(gender.get("male")),
            "age": _float_or_none(seg.get("age")),
        })
    return out


def parse_devaice_response(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    """Normalize a devAIce response to the shape lily_audeering_consumers
    consumes. Accepts common wrappers (result/results/root modules). Never
    raises on shape drift."""
    if not isinstance(payload, dict):
        return None
    root = _first_dict(payload.get("result"), payload.get("results"), payload)
    expression = _first_dict(root.get("expression"), root.get("expressions"))
    category_src = _first_dict(
        expression.get("category"), expression.get("categories"),
        root.get("category"),
    )
    dimension_src = _first_dict(
        expression.get("dimension"), expression.get("dimensions"),
        root.get("dimension"),
    )
    prosody_src = _first_dict(root.get("prosody"), expression.get("prosody"))

    category = _coerce_float_dict(category_src, ("angry", "happy", "neutral", "sad"))
    dimension = _coerce_float_dict(dimension_src, ("arousal", "dominance", "valence"))

    prosody: dict[str, Any] = {}
    for nested_key in ("f0", "loudness"):
        nested = prosody_src.get(nested_key)
        if isinstance(nested, dict):
            prosody[nested_key] = nested
    for key in ("speakingRate", "speakingRateVariation", "intonationScore"):
        val = _float_or_none(prosody_src.get(key))
        if val is not None:
            prosody[key] = val

    audio_quality_src = _first_dict(root.get("audioQuality"), payload.get("audioQuality"))
    audio_quality: dict[str, float] = {}
    for qkey in ("snr", "rt60", "clippingRatio", "silenceRatio"):
        qval = _float_or_none(audio_quality_src.get(qkey))
        if qval is not None:
            audio_quality[qkey] = qval

    speaker_segments = _parse_speaker_segments(
        root.get("speakerAttributes")
        if "speakerAttributes" in root
        else payload.get("speakerAttributes")
    )

    aed_src = root.get("aed") if "aed" in root else payload.get("aed")
    aed_tags: list[str] = []
    if isinstance(aed_src, list):
        for item in aed_src:
            if isinstance(item, dict):
                label = item.get("label") or item.get("class") or item.get("tag")
                if isinstance(label, str) and label.strip():
                    aed_tags.append(label.strip().lower())
            elif isinstance(item, str) and item.strip():
                aed_tags.append(item.strip().lower())
    elif isinstance(aed_src, dict):
        label = aed_src.get("label") or aed_src.get("class") or aed_src.get("tag")
        if isinstance(label, str) and label.strip():
            aed_tags.append(label.strip().lower())

    scene_src = root.get("scene") if "scene" in root else payload.get("scene")
    scene: dict[str, Any] | None = None
    if isinstance(scene_src, dict):
        label = scene_src.get("label") or scene_src.get("class") or scene_src.get("category")
        sub = scene_src.get("subScene") or scene_src.get("subscene")
        scene = {}
        if isinstance(label, str) and label.strip():
            scene["label"] = label.strip().lower()
        if isinstance(sub, str) and sub.strip():
            scene["subScene"] = sub.strip().lower()
        if not scene:
            scene = None
    elif isinstance(scene_src, str) and scene_src.strip():
        scene = {"label": scene_src.strip().lower()}

    if not (
        category or dimension or prosody or audio_quality
        or speaker_segments or aed_tags or scene
    ):
        return None
    return {
        "category": category,
        "dimension": dimension,
        "prosody": prosody,
        "audioQuality": audio_quality,
        "speakerSegments": speaker_segments,
        "aed": aed_tags,
        "scene": scene,
    }


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------

async def _account_info(client, headers: dict[str, str]) -> dict[str, Any] | None:
    try:
        async with client.get(
            AUDEERING_ACCOUNT_INFO_URL,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=8.0, connect=3.0, sock_read=5.0),
        ) as resp:
            if resp.status != 200:
                logger.error("LILY_AUDEERING | account-info failed status=%d", resp.status)
                return None
            payload = await resp.json(content_type=None)
            return payload if isinstance(payload, dict) else None
    except Exception as exc:
        logger.warning("LILY_AUDEERING | account-info exception: %r", exc)
        return None


async def account_allows_uploads() -> bool:
    """Best-effort quota preflight. Only deterministic terminal states
    return False; network misses stay best-effort."""
    if _UPLOAD_DISABLED_REASON is not None:
        return False
    api_key = lily_config.audeering_api_key()
    if not api_key:
        _disable_uploads("missing AUDEERING_API_KEY")
        return False
    if aiohttp is None:
        _disable_uploads("aiohttp unavailable")
        return False
    timeout = aiohttp.ClientTimeout(total=10.0, connect=5.0, sock_read=8.0)
    async with aiohttp.ClientSession(timeout=timeout) as client:
        info = await _account_info(client, {"X-API-Key": api_key})
    if info is None:
        return True
    duration_quota = info.get("durationQuota")
    upload_quota = info.get("uploadQuota")
    if duration_quota == 0 or upload_quota == 0:
        _disable_uploads(
            f"quota exhausted duration={duration_quota!r} uploads={upload_quota!r}"
        )
        return False
    return True


async def _log_account_quota(client, headers: dict[str, str]) -> None:
    """Log quota state after a 403 without exposing secrets."""
    try:
        async with client.get(
            AUDEERING_ACCOUNT_INFO_URL,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10.0, connect=5.0, sock_read=8.0),
        ) as resp:
            if resp.status == 200:
                payload = await resp.json(content_type=None)
                duration_quota = payload.get("durationQuota")
                upload_quota = payload.get("uploadQuota")
                if duration_quota == 0 or upload_quota == 0:
                    _disable_uploads(
                        f"quota exhausted duration={duration_quota!r} "
                        f"uploads={upload_quota!r}"
                    )
                logger.error(
                    "LILY_AUDEERING | upload forbidden status=403 quota "
                    "duration=%r uploads=%r expiration=%r",
                    duration_quota, upload_quota, payload.get("expirationDate"),
                )
                return
            if resp.status == 401:
                _disable_uploads("account-info invalid API credentials")
                return
            logger.error(
                "LILY_AUDEERING | upload forbidden status=403; account-info "
                "status=%d", resp.status,
            )
    except Exception as exc:
        logger.error(
            "LILY_AUDEERING | upload forbidden status=403; account-info "
            "failed: %r", exc,
        )


async def upload_audio(
    audio_bytes: bytes,
    *,
    session=None,
) -> dict[str, Any] | None:
    """Upload one WAV window; return the parsed acoustic shape or None.

    One upload attempt per capture window — a timeout may still complete
    server-side and burn quota, so timeout retries are forbidden. 429s
    retry with Retry-After/backoff; 401 disables the process; everything
    else logs and drops. Never gates the voice pipeline."""
    if _UPLOAD_DISABLED_REASON is not None:
        return None
    api_key = lily_config.audeering_api_key()
    if not api_key:
        logger.warning("LILY_AUDEERING | missing AUDEERING_API_KEY; dropping capture")
        return None
    if not audio_bytes or aiohttp is None:
        return None

    close_session = session is None
    timeout = aiohttp.ClientTimeout(total=60.0, connect=10.0, sock_read=50.0)
    client = session or aiohttp.ClientSession(timeout=timeout)
    headers = {"X-API-Key": api_key}
    try:
        async with _UPLOAD_SEMAPHORE:
            logger.info(
                "LILY_AUDEERING | UPLOAD_SUBMITTED audio_bytes_count=%d",
                len(audio_bytes),
            )
            for attempt in range(_RETRY_MAX_ATTEMPTS):
                form = aiohttp.FormData()
                form.add_field(
                    "config", json.dumps(_CONFIG), content_type="application/json"
                )
                form.add_field(
                    "file", audio_bytes,
                    filename="lily-window.wav",
                    content_type="application/octet-stream",
                )
                async with client.post(
                    AUDEERING_UPLOAD_URL, data=form, headers=headers
                ) as resp:
                    if resp.status == 200:
                        parsed = parse_devaice_response(
                            await resp.json(content_type=None)
                        )
                        if parsed is not None:
                            logger.info(
                                "LILY_AUDEERING | RESULT_RECEIVED modules=%s",
                                sorted(k for k, v in parsed.items() if v),
                            )
                        return parsed
                    if resp.status == 202:
                        payload = await resp.json(content_type=None)
                        upload_id = payload.get("uploadId") or payload.get("id")
                        if not upload_id:
                            logger.warning("LILY_AUDEERING | 202 without uploadId")
                            return None
                        return await _poll_once(client, str(upload_id), headers)
                    if resp.status == 429:
                        retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                        logger.warning(
                            "LILY_AUDEERING | rate limited (429) attempt=%d "
                            "retry_after=%s", attempt + 1, retry_after,
                        )
                        if attempt + 1 >= _RETRY_MAX_ATTEMPTS:
                            return None
                        await _sleep_for_retry(attempt, retry_after)
                        continue
                    if resp.status == 401:
                        _disable_uploads("invalid API credentials")
                        return None
                    if resp.status == 403:
                        await _log_account_quota(client, headers)
                        return None
                    text = await resp.text()
                    logger.error(
                        "LILY_AUDEERING | upload failed status=%d body=%s",
                        resp.status, text[:500],
                    )
                    return None
            return None
    except asyncio.TimeoutError:
        logger.info("LILY_AUDEERING | upload timed out; dropping capture")
        return None
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("LILY_AUDEERING | upload exception: %r", exc)
        return None
    finally:
        if close_session:
            await client.close()


async def _poll_once(client, upload_id: str, headers: dict[str, str]) -> dict[str, Any] | None:
    try:
        url = AUDEERING_RESULT_URL.format(upload_id=upload_id)
        for attempt in range(_RETRY_MAX_ATTEMPTS):
            async with client.get(
                url, headers=headers,
                timeout=aiohttp.ClientTimeout(total=15.0, connect=5.0, sock_read=12.0),
            ) as resp:
                if resp.status == 200:
                    return parse_devaice_response(await resp.json(content_type=None))
                if resp.status == 202:
                    logger.info(
                        "LILY_AUDEERING | result pending upload_id=%s "
                        "attempt=%d/%d",
                        upload_id, attempt + 1, _RETRY_MAX_ATTEMPTS,
                    )
                    if attempt + 1 >= _RETRY_MAX_ATTEMPTS:
                        logger.warning(
                            "LILY_AUDEERING | poll exhausted upload_id=%s",
                            upload_id,
                        )
                        return None
                    retry_after = _parse_retry_after(
                        resp.headers.get("Retry-After")
                    )
                    await _sleep_for_retry(attempt, retry_after)
                    continue
                if resp.status == 429:
                    retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                    if attempt + 1 >= _RETRY_MAX_ATTEMPTS:
                        return None
                    await _sleep_for_retry(attempt, retry_after)
                    continue
                logger.error("LILY_AUDEERING | poll failed status=%d", resp.status)
                return None
        return None
    except asyncio.TimeoutError:
        logger.info("LILY_AUDEERING | poll timed out upload_id=%s", upload_id)
        return None


# ---------------------------------------------------------------------------
# Capture pipeline — room audio -> >=5s WAV windows -> fire-and-forget uploads
# ---------------------------------------------------------------------------

AUDEERING_SAMPLE_RATE = 48000
_MIN_AUDIO_SECONDS = 0.6
_MAX_TRACKS = 8


class _BreakerState(enum.Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"


# The session's active pipeline, registered at construction time by
# lily_start_audeering_pipeline (registered even when the breaker opened at
# construction, so the gate below tracks the REAL breaker state rather than
# inferring it). WO-LILY-DESYNC-HONESTY-001 Sub-agent A.
_ACTIVE_PIPELINE: "LilyAudeeringPipeline | None" = None


def lily_child_gate_ready() -> bool:
    """THE single readiness flag for the adult-mode safety gate
    (WO-LILY-DESYNC-HONESTY-001 Sub-agent A): True only when the acoustic
    pipeline is configured AND started AND the circuit breaker is CLOSED —
    i.e. the child-signal sensor is actually watching the room.

    The child-signal sensor and the adult deck deploy as ONE unit: no
    pipeline (missing AUDEERING_API_KEY), a failed preflight, or a
    mid-session breaker OPEN all read as not-ready, and adult mode FAILS
    CLOSED. `lily_enter_adult_mode` reads this flag only."""
    pipeline = _ACTIVE_PIPELINE
    return (
        pipeline is not None
        and pipeline.started
        and not pipeline.breaker_open
    )


def _mask_key(key: str) -> str:
    if not key or len(key) < 8:
        return "****"
    return f"****{key[-4:]}"


def _build_wav(pcm_int16_bytes: bytes, sample_rate: int = AUDEERING_SAMPLE_RATE) -> bytes:
    data_size = len(pcm_int16_bytes)
    byte_rate = sample_rate * 2
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + data_size, b"WAVE",
        b"fmt ", 16,
        1, 1, sample_rate, byte_rate, 2, 16,
        b"data", data_size,
    )
    return header + pcm_int16_bytes


class LilyAudeeringPipeline:
    """Best-effort room-audio acoustic pipeline.

    Lifecycle (entrypoint):
        pipeline = await lily_start_audeering_pipeline(state)
        # register on ctx.room.on("track_subscribed") AFTER session.start()
        # (the JRVS Tijoux wiring lesson: handlers registered before
        # session.start fire into a half-initialized session), and run a
        # safety-net scan over ALREADY-subscribed tracks — the user mic
        # often subscribes during the session.start() window and the
        # handler alone misses it.
        asyncio.create_task(lily_audeering_audio_fork(track, pipeline))
        # on shutdown:
        await pipeline.stop()

    Circuit breaker: missing AUDEERING_API_KEY opens the breaker at
    construction — one structured log line, session runs unaffected.
    The breaker state mirrors into LilyAcousticState so addressee-row
    snapshots go EXPLICIT-null while it is open."""

    def __init__(self, state: "lily_audeering_consumers.LilyAcousticState | None" = None) -> None:
        self._state = state or lily_audeering_consumers.lily_get_acoustic_state()
        self._api_key = lily_config.audeering_api_key() or ""
        self._breaker = _BreakerState.CLOSED
        self._stop_event = asyncio.Event()
        self._track_tasks: dict[Any, asyncio.Task] = {}
        self._upload_tasks: set = set()
        self._started = False
        self._open_logged = False
        self._http_session = None
        self._uploads_this_session = 0
        if not self._api_key:
            self._open_breaker("missing AUDEERING_API_KEY at startup")

    @property
    def started(self) -> bool:
        return self._started and self._breaker == _BreakerState.CLOSED

    @property
    def breaker_open(self) -> bool:
        return self._breaker == _BreakerState.OPEN

    def _open_breaker(self, reason: str) -> None:
        if self._breaker == _BreakerState.OPEN:
            return
        self._breaker = _BreakerState.OPEN
        try:
            # Reason rides along so the state's on_breaker_open hook (the
            # adult-mode safety gate) can log WHY the sensor went down.
            self._state.set_breaker_open(True, reason=reason)
        except Exception:
            pass
        if not self._open_logged:
            self._open_logged = True
            logger.warning(
                "LILY_AUDEERING_BREAKER | state_transition=CLOSED->OPEN "
                "reason=%s session_remainder=disabled (best-effort pipeline; "
                "the session runs unaffected)", reason,
            )

    async def start(self) -> None:
        if self._breaker == _BreakerState.OPEN:
            logger.info("LILY_AUDEERING | start no-op (breaker OPEN)")
            return
        try:
            if not await account_allows_uploads():
                self._open_breaker("audEERING account preflight failed")
                return
            self._started = True
            self._stop_event.clear()
            if self._http_session is None and aiohttp is not None:
                connector = aiohttp.TCPConnector(
                    limit=8, limit_per_host=8, ttl_dns_cache=300,
                    keepalive_timeout=60,
                )
                self._http_session = aiohttp.ClientSession(connector=connector)
            self._state.set_breaker_open(False)
            logger.info(
                "LILY_AUDEERING | pipeline started (key=%s modules=%s "
                "window_s=%.1f interval_s=%.1f)",
                _mask_key(self._api_key),
                sorted(_CONFIG_MODULES.keys()),
                lily_config.audeering_window_seconds(),
                lily_config.audeering_capture_interval_seconds(),
            )
        except Exception as exc:  # noqa: BLE001 — never crash the entrypoint
            self._open_breaker(f"start exception {type(exc).__name__}")

    def attach_audio_track(self, track) -> None:
        """Attach one room audio track. Multi-mic tables attach several;
        each gets its own capture loop feeding the shared state."""
        try:
            if self._breaker == _BreakerState.OPEN or not self._started:
                return
            if track in self._track_tasks or len(self._track_tasks) >= _MAX_TRACKS:
                return
            self._track_tasks[track] = asyncio.create_task(self._audio_loop(track))
            logger.info(
                "LILY_AUDEERING_AUDIO | track attached (total=%d)",
                len(self._track_tasks),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("LILY_AUDEERING_AUDIO | attach failed: %r", exc)

    async def stop(self) -> None:
        try:
            self._stop_event.set()
            for task in list(self._track_tasks.values()):
                if not task.done():
                    task.cancel()
            if self._track_tasks:
                await asyncio.gather(
                    *self._track_tasks.values(), return_exceptions=True
                )
            self._track_tasks.clear()
            for task in list(self._upload_tasks):
                if not task.done():
                    task.cancel()
            if self._upload_tasks:
                await asyncio.gather(*self._upload_tasks, return_exceptions=True)
                self._upload_tasks.clear()
            if self._http_session is not None:
                try:
                    await self._http_session.close()
                except Exception:
                    pass
                self._http_session = None
            logger.info("LILY_AUDEERING | pipeline stopped (state=%s)", self._breaker.value)
        except Exception as exc:  # noqa: BLE001
            logger.warning("LILY_AUDEERING | stop exception: %r", exc)

    async def _audio_loop(self, track) -> None:
        if rtc is None:
            self._open_breaker("livekit.rtc unavailable")
            return
        window_s = lily_config.audeering_window_seconds()
        interval_s = lily_config.audeering_capture_interval_seconds()
        window_bytes = int(AUDEERING_SAMPLE_RATE * window_s) * 2
        min_bytes = int(AUDEERING_SAMPLE_RATE * _MIN_AUDIO_SECONDS) * 2
        # Buffer slightly beyond the window so late frames don't shrink it.
        max_buffer_bytes = int(window_bytes * 1.5)
        audio_stream = rtc.AudioStream(
            track, sample_rate=AUDEERING_SAMPLE_RATE, num_channels=1
        )
        pcm_chunks: list[bytes] = []
        buffer_bytes = 0
        last_capture = time.monotonic()
        try:
            async for frame_event in audio_stream:
                if self._stop_event.is_set() or self._breaker == _BreakerState.OPEN:
                    return
                chunk = bytes(frame_event.frame.data)
                if not chunk:
                    continue
                pcm_chunks.append(chunk)
                buffer_bytes += len(chunk)
                while buffer_bytes > max_buffer_bytes and pcm_chunks:
                    removed = pcm_chunks.pop(0)
                    buffer_bytes -= len(removed)
                now = time.monotonic()
                if (
                    buffer_bytes >= min_bytes
                    and now - last_capture >= interval_s
                ):
                    all_pcm = b"".join(pcm_chunks)
                    if len(all_pcm) > window_bytes:
                        all_pcm = all_pcm[-window_bytes:]
                    self._capture_audio_window(_build_wav(all_pcm))
                    last_capture = now
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — unawaited task guard
            logger.warning(
                "LILY_AUDEERING_AUDIO | loop failed exc_type=%s exc=%s",
                type(exc).__name__, str(exc)[:200],
            )
        finally:
            try:
                await audio_stream.aclose()
            except Exception:
                pass

    def _capture_audio_window(self, wav_bytes: bytes) -> None:
        """Schedule one upload without awaiting it inline (never blocks a
        turn)."""
        try:
            if self._breaker == _BreakerState.OPEN or not wav_bytes:
                return
            self._uploads_this_session += 1
            if self._uploads_this_session > lily_config.audeering_max_uploads_per_session():
                self._open_breaker("session capture cap reached")
                return
            task = asyncio.create_task(self._process_window(wav_bytes))
            self._upload_tasks.add(task)
            task.add_done_callback(self._upload_tasks.discard)
        except Exception as exc:  # noqa: BLE001
            logger.warning("LILY_AUDEERING | capture schedule failed: %r", exc)

    async def _process_window(self, wav_bytes: bytes) -> None:
        try:
            parsed = await upload_audio(wav_bytes, session=self._http_session)
            if parsed is None:
                if _UPLOAD_DISABLED_REASON is not None:
                    self._open_breaker(_UPLOAD_DISABLED_REASON)
                return
            # Consumer exceptions never stop raw-signal recording — the
            # state's record_response owns that guard.
            self._state.record_response(parsed)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "LILY_AUDEERING | window processing failed exc_type=%s exc=%s",
                type(exc).__name__, str(exc)[:200],
            )


async def lily_start_audeering_pipeline(
    state: "lily_audeering_consumers.LilyAcousticState | None" = None,
) -> LilyAudeeringPipeline | None:
    """Create and start the pipeline. Returns None when the key is missing
    (breaker open, one structured log, session unaffected). Never raises.

    The constructed pipeline is registered as the child-gate source even
    when it fails to start — lily_child_gate_ready() reads the live breaker
    state, and the adult deck fails CLOSED with the sensor down."""
    global _ACTIVE_PIPELINE
    try:
        pipeline = LilyAudeeringPipeline(state)
        _ACTIVE_PIPELINE = pipeline
        if pipeline.breaker_open:
            return None
        await pipeline.start()
        if not pipeline.started:
            logger.warning("LILY_AUDEERING | pipeline did not start (preflight/breaker)")
            return None
        logger.info(
            "LILY_AUDEERING | PIPELINE ACTIVE | modules=%s",
            "+".join(sorted(_CONFIG_MODULES.keys())),
        )
        return pipeline
    except Exception as exc:  # noqa: BLE001 — entrypoint guard
        logger.error("LILY_AUDEERING | pipeline start failed: %r", exc)
        return None


async def lily_audeering_audio_fork(track, pipeline: LilyAudeeringPipeline | None) -> None:
    """Attach a LiveKit audio track to a started pipeline. Never raises —
    acoustic failure never cascades into the voice pipeline."""
    if pipeline is None:
        return
    try:
        pipeline.attach_audio_track(track)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("LILY_AUDEERING | audio fork failed: %r", exc)
