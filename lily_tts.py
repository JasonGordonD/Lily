"""
lily_tts.py — ElevenLabs TTS for LILY.

WO-FLEET-LKA-171-TTD-PORT-001 (Minka cutover port, MinkaMoor 979175e →
9a6ee3f → 1eeb8d2 → 4a38b1a → 1c17415). livekit.agents.tts.TTS subclass.
Live path (stream AND say) is livekit-plugins-elevenlabs 1.7.1
eleven_v3_conversational over the text-to-dialogue multi-stream-input
websocket (pcm_24000). The HTTP text-to-speech stream path, the
with-timestamps alignment path, the per-request chunk retry and the
StreamAdapter wrapping are gone.

Retained from Lily's own baseline (this module's job, not the runbook's):
  - the speaker-tag empty-text guard (`_is_empty_after_strip`)
  - voice_settings resolved per ACTIVE voice at request time:
      voice1 (primary): stability 0.5, speed 0.87
      baseline (voice2/Raven's + any other id): stability 0.4, speed 0.90
    shared: similarity 0.9, style 0.0, speaker_boost
  - PATCH-003 P7 pace multiplier on the resolved speed
  - `set_voice` (lily_voice_switch) and `update_options`

When ElevenLabs fails, the turn degrades to SILENCE. There is no
substitute voice, no second provider, no fallback model. If the emitter
is already started (mid-stream fail), we flush — never call initialize()
again (RuntimeError: AudioEmitter already started).
"""

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, replace

import aiohttp

from livekit.agents import (
    APIConnectionError,
    APIConnectOptions,
    APITimeoutError,
    tts,
    utils,
)
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS

import lily_config
from lily_tts_receipts import record_tts_event

logger = logging.getLogger("lily_tts")

_LAST_SETUP_FRAME: dict = {}
_DIALOGUE_VOICE_SETTINGS_KEYS = frozenset({
    "stability",
    "similarity_boost",
    "style",
    "use_speaker_boost",
    "speed",
})


def _unlock_dialogue_voice_settings() -> None:
    """Send the full voice_settings dict on the TTD context-setup frame.
    The 1.7.1 plugin filters to stability-only; the operator's 2026-09-06
    isolation probe proved the server accepts all five keys. Runtime half
    of the two-half rule (the Dockerfile exact-string patch is the other)."""
    import livekit.plugins.elevenlabs.tts as el_tts

    el_tts._DIALOGUE_VOICE_SETTINGS_FIELDS = _DIALOGUE_VOICE_SETTINGS_KEYS
    orig = el_tts._build_dialogue_context_init_packet
    if getattr(orig, "_lily_full_settings", False):
        return

    def wrapped(opts, *, context_id: str):
        pkt = orig(opts, context_id=context_id)
        if isinstance(pkt, dict):
            _LAST_SETUP_FRAME.clear()
            _LAST_SETUP_FRAME.update(pkt)
            logger.info("TTS | ttd_setup_frame %s", json.dumps(pkt, default=str)[:800])
        return pkt

    wrapped._lily_full_settings = True  # type: ignore[attr-defined]
    el_tts._build_dialogue_context_init_packet = wrapped


def _contexts_open(inner) -> int:
    conn = getattr(inner, "_TTS__current_connection", None)
    if conn is None:
        return 0
    return len(getattr(conn, "_active_contexts", set()) or set())


ELEVENLABS_API_BASE = "https://api.elevenlabs.io/v1"
MODEL_ID = "eleven_v3_conversational"
OUTPUT_FORMAT = "pcm_24000"
SAMPLE_RATE = 24000
NUM_CHANNELS = 1
# ElevenLabs per-request character cap (eleven_v3: 4,200). The websocket
# path sentence-tokenizes inside the plugin, so this is the last-resort
# guard for one say() utterance longer than the cap — split with margin.
ELEVENLABS_REQUEST_CHAR_CAP = 4200
MAX_CHUNK_SIZE = lily_config.tts_max_chunk_size()
# Abort a speech if ElevenLabs yields no first audio byte within this
# window (floored at 5 s at the watchdog).
TTS_TTFB_TIMEOUT_SECS = float(os.getenv("LILY_TTS_TTFB_TIMEOUT_SECS", "2.0"))
# After a TTFB trip, log the open window. Subsequent turns still try
# ElevenLabs — there is no substitute voice to fail over to.
TTS_CIRCUIT_COOLDOWN_SECS = float(os.getenv("LILY_TTS_CIRCUIT_COOLDOWN_SECS", "20.0"))
_tts_circuit_open_until: float = 0.0

VOICE_SETTINGS = {
    "stability": 0.4,
    "similarity_boost": 0.9,
    "style": 0.0,
    "use_speaker_boost": True,
    "speed": 0.90,
}

# Voice1 (primary) runs its own tuning — principal adjustment 2026-07-31:
# stability 0.5 / speed 0.87. Voice2 (Raven's) keeps the baseline above.
VOICE1_SETTINGS = {
    **VOICE_SETTINGS,
    "stability": 0.5,
    "speed": 0.87,
}


def _voice_settings_for(voice_id: str) -> dict:
    """Per-voice settings, resolved against the ACTIVE voice id at request
    time (voice1's id can be env-overridden, so this cannot be a static
    id-keyed map baked at import)."""
    if voice_id == lily_config.lily_voice_1():
        return VOICE1_SETTINGS
    return VOICE_SETTINGS


def trip_tts_circuit(seconds: float | None = None) -> None:
    """Open the TTS circuit so subsequent turns skip ElevenLabs briefly."""
    global _tts_circuit_open_until
    cool = TTS_CIRCUIT_COOLDOWN_SECS if seconds is None else float(seconds)
    until = time.monotonic() + max(0.0, cool)
    if until > _tts_circuit_open_until:
        _tts_circuit_open_until = until
        logger.warning(
            "TTS | circuit OPEN for %.1fs (ElevenLabs failed; no substitute voice)", cool
        )


def tts_circuit_is_open() -> bool:
    return time.monotonic() < _tts_circuit_open_until


def _ensure_emitter_initialized(
    output_emitter: tts.AudioEmitter,
    *,
    emitter_started: bool = False,
) -> bool:
    """Initialize the emitter once. Never raise on 'already started'."""
    if emitter_started:
        return True
    try:
        output_emitter.initialize(
            request_id=utils.shortuuid(),
            sample_rate=SAMPLE_RATE,
            num_channels=NUM_CHANNELS,
            mime_type="audio/pcm",
        )
        return True
    except RuntimeError as e:
        if "already started" in str(e).lower():
            logger.warning("TTS | initialize skipped — emitter already started")
            return True
        raise


def _emit_silent_placeholder(
    output_emitter: tts.AudioEmitter,
    reason: str,
    text: str,
    *,
    emitter_started: bool = False,
) -> None:
    """Satisfy the SDK 'at least one frame' contract without double-initialize."""
    _ensure_emitter_initialized(output_emitter, emitter_started=emitter_started)
    try:
        output_emitter.push(b"\x00" * (SAMPLE_RATE // 100 * 2))  # 10ms PCM16 silence
        output_emitter.flush()
    except Exception as e:
        logger.warning("TTS | silent placeholder push/flush failed (%s): %s", reason, e)
    logger.info(
        "TTS | silent placeholder (%s, started=%s) | text=%r",
        reason,
        emitter_started,
        (text or "")[:80],
    )


async def _degrade_without_voice(
    output_emitter: tts.AudioEmitter,
    text: str,
    reason: str,
    *,
    emitter_started: bool = False,
    audio_bytes: int = 0,
) -> None:
    """Silence + flush. Lily has no side chat channel: the transcript wire
    already carries the text, so the degrade is SILENCE ONLY. Never
    re-initialize."""
    logger.warning(
        "TTS | DEGRADED_TO_SILENCE | reason=%s audio_bytes=%d text=%r — no "
        "substitute voice (company rule)",
        reason, audio_bytes, (text or "")[:80],
    )
    _emit_silent_placeholder(
        output_emitter,
        reason,
        text,
        emitter_started=emitter_started,
    )


@dataclass
class _TTSOpts:
    voice_id: str
    api_key: str
    model_id: str
    output_format: str
    # PATCH-003 P7: session pace multiplier on the resolved speed
    # (1.0 = normal, <1.0 = slower).
    pace_multiplier: float = 1.0


class LilyTTS(tts.TTS):
    """ElevenLabs TTS for the LILY agent.

    Live path is eleven_v3_conversational via livekit-plugins-elevenlabs 1.7.1
    text-to-dialogue websocket (stream and say). Sanitizer, per-voice
    settings and the TTFB circuit stay here. On ElevenLabs failure:
    silence. No substitute voice.
    """

    def __init__(
        self,
        voice_id: str | None = None,
        *,
        api_key: str | None = None,
        model_id: str = MODEL_ID,
    ) -> None:
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=True),
            sample_rate=SAMPLE_RATE,
            num_channels=NUM_CHANNELS,
        )
        self._opts = _TTSOpts(
            voice_id=voice_id or lily_config.lily_voice_id(),
            api_key=api_key or lily_config.eleven_api_key(),
            model_id=model_id,
            output_format=OUTPUT_FORMAT,
            pace_multiplier=1.0,
        )
        self._inner: object | None = None
        logger.info(
            "TTS | model=%s voice=%s streaming=True ttd format=%s",
            self._opts.model_id, self._opts.voice_id, OUTPUT_FORMAT,
        )

    # PATCH-003 P7 pace levels — a modest slow-down the ElevenLabs speed
    # param supports cleanly (below ~0.8 the voice distorts).
    _PACE_MULTIPLIERS = {"normal": 1.0, "slow": 0.88}

    def set_pace(self, level: str) -> bool:
        """Set the session delivery pace ('normal' | 'slow'). Returns True
        if the level was applied to the TTS speed. Safe to call between
        turns — the inner plugin re-opens its context on the next speech."""
        mult = self._PACE_MULTIPLIERS.get((level or "").strip().lower())
        if mult is None:
            return False
        self._opts.pace_multiplier = mult
        self._apply_voice_to_inner()
        return True

    @property
    def model(self) -> str:
        return self._opts.model_id

    @property
    def provider(self) -> str:
        return "ElevenLabs"

    def update_options(self, *, voice_id: str | None = None) -> None:
        """Update the active voice. Safe to call between sequential say() calls."""
        if voice_id is not None:
            self._opts.voice_id = voice_id
            self._apply_voice_to_inner()

    def set_voice(self, voice_id: str) -> None:
        """Runtime voice-id swap (public API used by
        `lily_voice_switch.lily_switch_voice`). Only the voice target and
        its per-voice settings change; model/format/api_key are untouched.
        The inner plugin marks its connection non-current, so the NEXT
        speech opens a context on the new voice — no session teardown."""
        if not voice_id or not voice_id.strip():
            raise ValueError("set_voice requires a non-empty voice_id")
        self._opts.voice_id = voice_id.strip()
        self._apply_voice_to_inner()

    def _resolved_voice_settings(self) -> dict:
        """The five-key dict for the ACTIVE voice with the P7 pace applied."""
        settings = dict(_voice_settings_for(self._opts.voice_id))
        if self._opts.pace_multiplier != 1.0 and "speed" in settings:
            settings["speed"] = round(settings["speed"] * self._opts.pace_multiplier, 3)
        return settings

    def _plugin_voice_settings(self):
        from livekit.plugins.elevenlabs import VoiceSettings

        s = self._resolved_voice_settings()
        return VoiceSettings(
            stability=s["stability"],
            similarity_boost=s["similarity_boost"],
            style=s["style"],
            speed=s["speed"],
            use_speaker_boost=s["use_speaker_boost"],
        )

    def _apply_voice_to_inner(self) -> None:
        inner = getattr(self, "_inner", None)
        if inner is None:
            return
        inner.update_options(  # type: ignore[attr-defined]
            voice_id=self._opts.voice_id,
            voice_settings=self._plugin_voice_settings(),
        )

    def _ensure_session(self) -> aiohttp.ClientSession:
        return utils.http_context.http_session()

    def _ensure_inner(self):
        """Lazy official plugin — eleven_v3_conversational TTD websocket."""
        if self._inner is None:
            from livekit.plugins.elevenlabs import TTS as ElevenLabsPluginTTS

            _unlock_dialogue_voice_settings()
            self._inner = ElevenLabsPluginTTS(
                voice_id=self._opts.voice_id,
                api_key=self._opts.api_key or "missing",
                model=self._opts.model_id,
                encoding="pcm_24000",
                voice_settings=self._plugin_voice_settings(),
                apply_text_normalization="auto",
            )
        return self._inner

    async def _degrade_after_elevenlabs(
        self,
        output_emitter: tts.AudioEmitter,
        text: str,
        reason: str,
    ) -> None:
        trip_tts_circuit()
        await _degrade_without_voice(
            output_emitter,
            text,
            reason,
            emitter_started=False,
            audio_bytes=0,
        )

    def stream(
        self, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS
    ) -> "LilySynthesizeStream":
        return LilySynthesizeStream(tts=self, conn_options=conn_options)

    @staticmethod
    def _is_empty_after_strip(text: str) -> bool:
        """Check if text is empty after removing speaker tags and whitespace."""
        if not text:
            return True
        cleaned = re.sub(r"</?speaker[^>]*>", "", text).strip()
        return not cleaned

    @staticmethod
    def _sanitize_tts_text(text: str) -> str | None:
        """Lily's pre-transport sanitizer. The say-pipeline in lily_agent
        (SAY_PIPELINE transforms) already owns wording; this strips the
        speaker tags that must never reach ElevenLabs and returns None when
        nothing speakable remains."""
        if not text or not text.strip():
            return None
        cleaned = re.sub(r"</?speaker[^>]*>", "", text)
        if not cleaned.strip():
            logger.info("TTS | input was empty after speaker-tag strip — skipping TTS call")
            return None
        return cleaned

    def synthesize(
        self,
        text: str,
        *,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
        time_offset: float = 0.0,
    ) -> "LilyChunkedStream":
        """Synthesize one text into a LilyChunkedStream (the say() path).

        `time_offset` is accepted for the per-sentence aligned caller in
        LilyAgent._lily_aligned_tts_frames; the websocket path carries no
        per-word alignment out of the wrapper, so it is inert here.
        """
        sanitized = self._sanitize_tts_text(text)
        skip = sanitized is None or self._is_empty_after_strip(sanitized or "")
        return LilyChunkedStream(
            tts=self,
            input_text=sanitized or text,
            conn_options=conn_options,
            opts=replace(self._opts),
            skip_empty=skip,
            time_offset=time_offset,
        )


# Trailing whitespace is optional so a greeting ending in "?" flushes.
_SENTENCE_FLUSH = re.compile(r"[.!?…][\"')\]]*\s*$")


class LilySynthesizeStream(tts.SynthesizeStream):
    """Sanitize LLM tokens, then drive the official TTD websocket stream."""

    def __init__(
        self,
        *,
        tts: LilyTTS,
        conn_options: APIConnectOptions,
    ) -> None:
        super().__init__(tts=tts, conn_options=conn_options)
        self._lily = tts

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        inner_tts = self._lily._ensure_inner()
        inner = None
        speech_id = utils.shortuuid()
        t0 = time.perf_counter()
        ttfb_ms = None
        chars = 0
        interrupted = False
        error = None
        ctx_max = _contexts_open(inner_tts)
        buf = ""
        first_text = asyncio.Event()

        def _flush_buf() -> None:
            nonlocal buf, inner, chars, t0
            piece = buf
            buf = ""
            if not piece.strip():
                return
            cleaned = LilyTTS._sanitize_tts_text(piece)
            if not cleaned:
                return
            if inner is None:
                # Open the socket only on the first non-empty sanitized
                # flush: an empty open trips the plugin watchdog on text=''
                # then dies with 1008 input_timeout_exceeded.
                inner = inner_tts.stream(conn_options=self._conn_options)
                t0 = time.perf_counter()
                first_text.set()
            chars += len(cleaned)
            inner.push_text(cleaned)

        async def _pump() -> None:
            nonlocal buf
            async for data in self._input_ch:
                if isinstance(data, self._FlushSentinel):
                    _flush_buf()
                    if inner is not None:
                        inner.flush()
                    continue
                buf += data
                if _SENTENCE_FLUSH.search(buf) or len(buf) >= 80:
                    _flush_buf()
            _flush_buf()
            if inner is not None:
                inner.end_input()
            first_text.set()

        pump_t = asyncio.create_task(_pump())
        emitter_started = False
        total_bytes = 0
        chunk_count = 0
        ttfb = max(5.0, TTS_TTFB_TIMEOUT_SECS)
        try:
            await first_text.wait()
            if inner is None:
                _emit_silent_placeholder(
                    output_emitter, "ttd-no-text", self._collected_preview()
                )
                error = "no-text"
                return
            inner_iter = inner.__aiter__()
            try:
                first = await asyncio.wait_for(inner_iter.__anext__(), timeout=ttfb)
            except StopAsyncIteration:
                first = None
            except asyncio.TimeoutError:
                logger.warning(
                    "TTS | TTD websocket TTFB watchdog fired after %.1fs — no first byte chars=%d",
                    ttfb,
                    chars,
                )
                await inner.aclose()
                if chars:
                    await self._lily._degrade_after_elevenlabs(
                        output_emitter, self._collected_preview(), "ttfb-timeout"
                    )
                else:
                    _emit_silent_placeholder(
                        output_emitter, "ttfb-timeout", self._collected_preview()
                    )
                error = "ttfb-timeout"
                return

            def _push_event(ev: object) -> None:
                nonlocal emitter_started, total_bytes, chunk_count
                frame = getattr(ev, "frame", None)
                if frame is None:
                    return
                data = bytes(getattr(frame, "data", b"") or b"")
                if not data:
                    return
                if not emitter_started:
                    try:
                        output_emitter.initialize(
                            request_id=utils.shortuuid(),
                            sample_rate=SAMPLE_RATE,
                            num_channels=NUM_CHANNELS,
                            mime_type="audio/pcm",
                            stream=True,
                        )
                    except RuntimeError as e:
                        if "already started" not in str(e).lower():
                            raise
                    try:
                        output_emitter.start_segment(segment_id=utils.shortuuid())
                    except Exception:
                        pass
                    emitter_started = True
                output_emitter.push(data)
                total_bytes += len(data)
                chunk_count += 1

            if first is not None:
                ttfb_ms = round((time.perf_counter() - t0) * 1000, 1)
                _push_event(first)
            async for ev in inner_iter:
                _push_event(ev)

            if total_bytes == 0:
                _emit_silent_placeholder(
                    output_emitter, "ttd-stream-empty", self._collected_preview()
                )
                return
            try:
                output_emitter.end_segment()
            except Exception:
                pass
            output_emitter.flush()
        except asyncio.CancelledError:
            interrupted = True
            error = "interrupted"
            raise
        except Exception as e:
            logger.warning("TTS | TTD websocket error: %s", e)
            error = repr(e)
            trip_tts_circuit()
            if emitter_started:
                await _degrade_without_voice(
                    output_emitter,
                    self._collected_preview(),
                    "ttd-ws-error",
                    emitter_started=True,
                    audio_bytes=total_bytes,
                )
                return
            await self._lily._degrade_after_elevenlabs(
                output_emitter, self._collected_preview(), "ttd-ws-error"
            )
        finally:
            pump_t.cancel()
            try:
                await pump_t
            except (asyncio.CancelledError, Exception):
                interrupted = True
            if inner is not None:
                try:
                    await inner.aclose()
                except Exception:
                    pass
            record_tts_event(
                transport="ws_ttd",
                model_id=self._lily._opts.model_id,
                voice_id=self._lily._opts.voice_id,
                chars=chars,
                ttfb_ms=ttfb_ms,
                total_ms=round((time.perf_counter() - t0) * 1000, 1),
                audio_ms=round(total_bytes / (SAMPLE_RATE * 2) * 1000, 1),
                chunks=chunk_count,
                interrupted=interrupted,
                contexts_open_max=max(ctx_max, _contexts_open(inner_tts)),
                error=error,
                speech_id=speech_id,
                raw={"setup_frame": dict(_LAST_SETUP_FRAME)} if _LAST_SETUP_FRAME else None,
            )

    def _collected_preview(self) -> str:
        return (getattr(self, "_input_text", None) or "")[:200]


async def _drive_ttd_say(inner, text: str, ttfb: float, extra_pieces=()):
    """Push one complete utterance on the TTD websocket.

    Plugin SynthesizeStream.end_input() sends close_context. The dialogue
    server drops in-flight audio when that arrives before the first byte
    (Minka 4a38b1a: greeting 48 chars, audio_ms=0, error=ttfb-timeout).
    Flush the sentence so generation starts; close the context only after
    the first audio event. `extra_pieces` are the over-cap tail pieces of
    the SAME utterance — pushed in order on the same context, before the
    flush, never after the close.
    """
    inner.push_text(text)
    for piece in extra_pieces:
        inner.push_text(piece)
    inner.flush()
    inner_iter = inner.__aiter__()
    first = await asyncio.wait_for(inner_iter.__anext__(), timeout=ttfb)
    inner.end_input()
    return first, inner_iter


class LilyChunkedStream(tts.ChunkedStream):
    """The say() path — one utterance, one context, same websocket."""

    def __init__(
        self,
        *,
        tts: LilyTTS,
        input_text: str,
        conn_options: APIConnectOptions,
        opts: _TTSOpts,
        skip_empty: bool = False,
        time_offset: float = 0.0,
    ) -> None:
        super().__init__(tts=tts, input_text=input_text, conn_options=conn_options)
        self._opts = opts
        self._skip_empty = skip_empty
        self._time_offset = float(time_offset or 0.0)
        # Claim-vs-delivery accounting (WS-2) kept at utterance granularity:
        # one context per speech, so delivered is 0 or 1 and the remainder
        # is the whole text when no audio ever aired.
        self.chunks_total = 0
        self.chunks_delivered = 0
        self.undelivered_remainder = ""

    @staticmethod
    def _split_text(text: str) -> list[str]:
        """Split text so every chunk stays under MAX_CHUNK_SIZE (comfortably
        below the ElevenLabs per-request cap). Prefer a sentence boundary;
        fall back to the last whitespace before the cap so a boundary-less
        long sentence never splits mid-WORD."""
        if len(text) <= MAX_CHUNK_SIZE:
            return [text]
        chunks = []
        remaining = text
        while len(remaining) > MAX_CHUNK_SIZE:
            boundary = -1
            for m in re.finditer(r'[.!?]\s', remaining[:MAX_CHUNK_SIZE]):
                boundary = m.end()
            if boundary == -1:
                ws = remaining.rfind(" ", 0, MAX_CHUNK_SIZE)
                boundary = ws + 1 if ws > 0 else MAX_CHUNK_SIZE
            chunks.append(remaining[:boundary])
            remaining = remaining[boundary:]
        if remaining:
            chunks.append(remaining)
        logger.info(
            "TTS | splitting text: %d chars into %d chunks (cap=%d)",
            len(text), len(chunks), MAX_CHUNK_SIZE,
        )
        return chunks

    async def _fallback_after_elevenlabs(
        self,
        output_emitter: tts.AudioEmitter,
        reason: str,
        *,
        emitter_started: bool = False,
        audio_bytes: int = 0,
    ) -> None:
        """ElevenLabs failed — silence. No substitute voice. Never re-raise
        APITimeoutError (it stacks framework retries into dead air)."""
        trip_tts_circuit()
        await _degrade_without_voice(
            output_emitter,
            self._input_text,
            reason,
            emitter_started=emitter_started,
            audio_bytes=audio_bytes,
        )

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        try:
            await self._run_impl(output_emitter)
        except RuntimeError as e:
            if "already started" in str(e).lower():
                logger.warning(
                    "TTS | swallowed AudioEmitter already started — flush and exit cleanly"
                )
                try:
                    output_emitter.push(b"\x00" * (SAMPLE_RATE // 100 * 2))
                    output_emitter.flush()
                except Exception:
                    pass
                return
            raise

    async def _run_impl(self, output_emitter: tts.AudioEmitter) -> None:
        if self._skip_empty:
            # Emit a silent placeholder so the SDK's "at least one frame per
            # TTS call" contract holds instead of APIError killing the turn.
            _emit_silent_placeholder(output_emitter, "tag-only-skip", self._input_text)
            return

        text_chunks = self._split_text(self._input_text)
        self.chunks_total = 1
        # say() rides the same TTD websocket as streaming turns.
        inner_tts = self._tts._ensure_inner()
        try:
            await inner_tts._current_connection()
        except Exception as e:
            logger.warning("TTS | ttd prewarm failed: %s", e)
        inner = inner_tts.stream(conn_options=self._conn_options)
        speech_id = utils.shortuuid()
        t0 = time.perf_counter()
        ttfb_ms = None
        chunk_count = 0
        interrupted = False
        error = None
        ctx_max = _contexts_open(inner_tts)
        logger.info("TTS _run called via ws_ttd, text=%r", self._input_text[:80])
        ttfb = max(5.0, TTS_TTFB_TIMEOUT_SECS)
        emitter_started = False
        total_bytes = 0
        inner_iter = None
        try:
            t0 = time.perf_counter()
            try:
                # One context per speech: an over-cap utterance's tail
                # pieces ride the same context, in order, before the close.
                first, inner_iter = await _drive_ttd_say(
                    inner, text_chunks[0], ttfb, extra_pieces=text_chunks[1:]
                )
            except StopAsyncIteration:
                first = None
            except asyncio.TimeoutError:
                logger.warning(
                    "TTS | TTFB watchdog fired after %.1fs — no first byte",
                    ttfb,
                )
                await self._fallback_after_elevenlabs(output_emitter, "ttfb-timeout")
                error = "ttfb-timeout"
                return

            def _push_event(ev: object) -> None:
                nonlocal emitter_started, total_bytes, chunk_count, ttfb_ms
                frame = getattr(ev, "frame", None)
                if frame is None:
                    return
                data = bytes(getattr(frame, "data", b"") or b"")
                if not data:
                    return
                if ttfb_ms is None:
                    ttfb_ms = round((time.perf_counter() - t0) * 1000, 1)
                if not emitter_started:
                    _ensure_emitter_initialized(output_emitter, emitter_started=False)
                    emitter_started = True
                output_emitter.push(data)
                total_bytes += len(data)
                chunk_count += 1

            if first is not None:
                _push_event(first)
            if inner_iter is not None:
                async for ev in inner_iter:
                    _push_event(ev)

            if total_bytes == 0:
                _emit_silent_placeholder(
                    output_emitter, "ttd-say-empty", self._input_text,
                    emitter_started=emitter_started,
                )
                logger.warning(
                    "TTS | elevenlabs returned 0 audio frames for text=%r — emitting silent placeholder",
                    self._input_text[:80],
                )
            else:
                output_emitter.flush()
                self.chunks_delivered = 1
            logger.info(
                "TTS ws_ttd say complete, total_bytes=%d, chunks=%d, duration=%.2fs",
                total_bytes, chunk_count, total_bytes / (SAMPLE_RATE * 2),
            )
        except asyncio.CancelledError:
            interrupted = True
            error = "interrupted"
            raise
        except asyncio.TimeoutError:
            error = "timeout"
            await self._fallback_after_elevenlabs(
                output_emitter,
                "aiohttp-timeout",
                emitter_started=emitter_started,
                audio_bytes=total_bytes,
            )
            return
        except APITimeoutError:
            error = "api-timeout"
            await self._fallback_after_elevenlabs(
                output_emitter,
                "api-timeout",
                emitter_started=emitter_started,
                audio_bytes=total_bytes,
            )
            return
        except Exception as e:
            error = repr(e)
            if emitter_started:
                logger.warning(
                    "TTS | post-init error — flush instead of raise: %s",
                    e,
                )
                await _degrade_without_voice(
                    output_emitter,
                    self._input_text,
                    "post-init-error",
                    emitter_started=True,
                    audio_bytes=total_bytes,
                )
                return
            raise APIConnectionError() from e
        finally:
            if self.chunks_delivered < self.chunks_total:
                self.undelivered_remainder = self._input_text
                logger.warning(
                    "LILY_TTS | TAIL_CHUNK_UNDELIVERED | delivered=%d/%d | "
                    "remainder=%d chars — no audio aired for this speech",
                    self.chunks_delivered, self.chunks_total,
                    len(self.undelivered_remainder),
                )
            try:
                await inner.aclose()
            except Exception:
                pass
            record_tts_event(
                transport="ws_ttd",
                model_id=self._opts.model_id,
                voice_id=self._opts.voice_id,
                chars=len(self._input_text or ""),
                ttfb_ms=ttfb_ms,
                total_ms=round((time.perf_counter() - t0) * 1000, 1),
                audio_ms=round(total_bytes / (SAMPLE_RATE * 2) * 1000, 1),
                chunks=chunk_count,
                interrupted=interrupted,
                contexts_open_max=max(ctx_max, _contexts_open(inner_tts)),
                error=error,
                speech_id=speech_id,
                raw={"setup_frame": dict(_LAST_SETUP_FRAME)} if _LAST_SETUP_FRAME else None,
            )


async def lily_prewarm_tts_connection(tts_instance: "LilyTTS | None" = None) -> None:
    """Open the TTD websocket at session start so the greeting's first
    context skips the TLS + websocket handshake. Fire-and-forget; never
    raises. Without an instance there is nothing to warm (no HTTP pool
    exists on this path any more)."""
    if tts_instance is None:
        logger.debug("TTS | prewarm skipped: no TTS instance")
        return
    try:
        inner = tts_instance._ensure_inner()
        await inner._current_connection()
        logger.info("TTS | prewarm ttd websocket open")
    except Exception as e:
        logger.warning("TTS | PREWARM_NOT_OK | ttd websocket: %s", e)
