"""
lily_tts.py — ElevenLabs HTTP Streaming TTS for LILY.

Native lift of the Lovebirds production TTS wrapper (lbs_tts.py):
proper livekit.agents.tts.TTS subclass streaming raw PCM from
/v1/text-to-speech/{voice_id}/stream (NOT the dialogue endpoint) using
eleven_v3 at 24 kHz. AgentSession handles all audio output routing.

Retained from the Lovebirds baseline:
  - byte-alignment carry on the PCM stream
  - empty-text guard
  - 5K sentence-boundary split
  - voice_settings resolved per ACTIVE voice at request time:
      voice1 (primary): stability 0.5, speed 0.87 (principal adjustment
        2026-07-31)
      baseline (voice2/Raven's + any other id): stability 0.4, speed 0.90
        (principal adjustment 2026-07-15; Raven's baseline was 0.93)
    shared: similarity 0.9, style 0.0, speaker_boost
"""

import asyncio
import logging
import re
from dataclasses import dataclass, replace

import aiohttp

from livekit.agents import (
    APIConnectionError,
    APIConnectOptions,
    APIStatusError,
    APITimeoutError,
    tts,
    utils,
)
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS

import lily_config

logger = logging.getLogger("lily_tts")

ELEVENLABS_API_BASE = "https://api.elevenlabs.io/v1"
MODEL_ID = "eleven_v3"
OUTPUT_FORMAT = "pcm_24000"
SAMPLE_RATE = 24000
NUM_CHANNELS = 1
# ElevenLabs rejects a /stream request whose text exceeds the per-request
# character cap (eleven_v3: 4,200). A chunk over the cap 4xx's mid-turn,
# and because earlier chunks already pushed audio the turn dies
# mid-sentence (WO-LILY-STREAM-INTEGRITY-002 WS-2). The framework's
# StreamAdapter sentence-splits first, so this cap is the last-resort
# guard for a single sentence longer than the cap — split with comfortable
# margin BELOW 4,200 so a boundary-less long sentence never rides the edge.
ELEVENLABS_REQUEST_CHAR_CAP = 4200
MAX_CHUNK_SIZE = 3800

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


@dataclass
class _TTSOpts:
    voice_id: str
    api_key: str
    model_id: str
    output_format: str
    # PATCH-003 P7: session pace multiplier on the resolved speed
    # (1.0 = normal, <1.0 = slower). Snapshotted per synthesize() like
    # every other opt, so a mid-session pace change takes the next turn.
    pace_multiplier: float = 1.0


class LilyTTS(tts.TTS):
    """ElevenLabs TTS via HTTP streaming for the LILY agent.

    Calls POST /v1/text-to-speech/{voice_id}/stream with eleven_v3 and
    streams raw PCM at 24 kHz directly into the LiveKit agents
    AudioEmitter pipeline.
    """

    def __init__(
        self,
        voice_id: str | None = None,
        *,
        api_key: str | None = None,
        model_id: str = MODEL_ID,
    ) -> None:
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False),
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

    # PATCH-003 P7 pace levels — a modest slow-down the ElevenLabs speed
    # param supports cleanly (below ~0.8 the voice distorts). Text-layer
    # compensation (shorter sentences, more pause) rides the state block
    # regardless of whether the voice honors the speed change.
    _PACE_MULTIPLIERS = {"normal": 1.0, "slow": 0.88}

    def set_pace(self, level: str) -> bool:
        """Set the session delivery pace ('normal' | 'slow'). Returns True
        if the level was applied to the TTS speed. Safe to call between
        turns — synthesize() snapshots _opts each call."""
        mult = self._PACE_MULTIPLIERS.get((level or "").strip().lower())
        if mult is None:
            return False
        self._opts.pace_multiplier = mult
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

    def set_voice(self, voice_id: str) -> None:
        """Runtime voice-id swap (port of Zuna's WO-ZUNA-VOICE-SWITCH-TOOL-001).

        Public API used by `lily_voice_switch.lily_switch_voice`. Mutates
        `self._opts.voice_id` so every subsequent
        `/v1/text-to-speech/{voice_id}/stream` request targets the new
        voice. No session teardown — `synthesize()` snapshots `self._opts`
        via `replace()` on each call, so the next turn picks up the swap
        without any reconnect.

        Locked invariants (model_id / output_format / voice_settings /
        api_key) are untouched — only the voice target changes.
        """
        if not voice_id or not voice_id.strip():
            raise ValueError("set_voice requires a non-empty voice_id")
        self._opts.voice_id = voice_id.strip()

    def _ensure_session(self) -> aiohttp.ClientSession:
        return utils.http_context.http_session()

    @staticmethod
    def _is_empty_after_strip(text: str) -> bool:
        """Check if text is empty after removing speaker tags and whitespace."""
        if not text:
            return True
        cleaned = re.sub(r"</?speaker[^>]*>", "", text).strip()
        return not cleaned

    def synthesize(
        self,
        text: str,
        *,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> "LilyChunkedStream":
        return LilyChunkedStream(
            tts=self,
            input_text=text,
            conn_options=conn_options,
            opts=replace(self._opts),
            skip_empty=self._is_empty_after_strip(text),
        )


class LilyChunkedStream(tts.ChunkedStream):
    # ChunkedStream subclass interface (_run(output_emitter) signature and
    # AudioEmitter initialize/push/flush) verified unchanged at 1.6.6.
    def __init__(
        self,
        *,
        tts: LilyTTS,
        input_text: str,
        conn_options: APIConnectOptions,
        opts: _TTSOpts,
        skip_empty: bool = False,
    ) -> None:
        super().__init__(tts=tts, input_text=input_text, conn_options=conn_options)
        self._opts = opts
        self._skip_empty = skip_empty
        # Tail-chunk delivery accounting (WO-LILY-STREAM-INTEGRITY-002 WS-2,
        # claim-vs-delivery model): a chunk counts DELIVERED only once its
        # audio actually flushed. If synthesis dies after the first chunk
        # aired, undelivered_remainder holds the text that never reached the
        # air so the failure surfaces as a partial cut (→ cut-recovery
        # regenerates it) rather than silent mid-sentence death.
        self.chunks_total = 0
        self.chunks_delivered = 0
        self.undelivered_remainder = ""

    @staticmethod
    def _split_text(text: str) -> list[str]:
        """Split text so every chunk stays under MAX_CHUNK_SIZE (comfortably
        below the ElevenLabs per-request cap). Prefer a sentence boundary;
        fall back to the last whitespace before the cap so a boundary-less
        long sentence never splits mid-WORD (a mid-word cut drops the word
        and can leave a phantom that the tokenizer re-glues). Every emitted
        chunk is guaranteed <= MAX_CHUNK_SIZE."""
        if len(text) <= MAX_CHUNK_SIZE:
            return [text]
        chunks = []
        remaining = text
        while len(remaining) > MAX_CHUNK_SIZE:
            boundary = -1
            for m in re.finditer(r'[.!?]\s', remaining[:MAX_CHUNK_SIZE]):
                boundary = m.end()
            if boundary == -1:
                # No sentence boundary under the cap — break at the last
                # whitespace instead of slicing through a word.
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

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        if self._skip_empty:
            logger.warning("TTS | skipping empty text (text=%r)", self._input_text)
            return

        text_chunks = self._split_text(self._input_text)
        self.chunks_total = len(text_chunks)

        url = (
            f"{ELEVENLABS_API_BASE}/text-to-speech/{self._opts.voice_id}"
            f"/stream?output_format={self._opts.output_format}"
        )

        try:
            initialized = False
            total_bytes = 0
            chunk_count = 0

            for chunk_index, text_chunk in enumerate(text_chunks):
                # P7: apply the session pace to the resolved speed (a slow
                # pace lowers it modestly; normal leaves it untouched).
                voice_settings = dict(_voice_settings_for(self._opts.voice_id))
                if self._opts.pace_multiplier != 1.0 and "speed" in voice_settings:
                    voice_settings["speed"] = round(
                        voice_settings["speed"] * self._opts.pace_multiplier, 3
                    )
                body = {
                    "text": text_chunk,
                    "model_id": self._opts.model_id,
                    "voice_settings": voice_settings,
                    "apply_text_normalization": "auto",
                }

                async with self._tts._ensure_session().post(
                    url,
                    headers={
                        "xi-api-key": self._opts.api_key,
                        "Content-Type": "application/json",
                    },
                    json=body,
                    timeout=aiohttp.ClientTimeout(
                        total=30,
                        sock_connect=self._conn_options.timeout,
                    ),
                ) as resp:
                    if resp.status != 200:
                        err_body = await resp.text()
                        raise APIStatusError(
                            message=f"ElevenLabs error: {err_body[:200]}",
                            status_code=resp.status,
                            request_id=None,
                            body=err_body,
                        )

                    if not initialized:
                        output_emitter.initialize(
                            request_id=utils.shortuuid(),
                            sample_rate=SAMPLE_RATE,
                            num_channels=NUM_CHANNELS,
                            mime_type="audio/pcm",
                        )
                        initialized = True

                    carry = b""
                    async for audio_chunk, _ in resp.content.iter_chunks():
                        data = carry + audio_chunk
                        usable = len(data) - (len(data) % 2)
                        if usable > 0:
                            output_emitter.push(data[:usable])
                            total_bytes += usable
                            chunk_count += 1
                        carry = data[usable:]
                    if len(carry) >= 2:
                        usable = len(carry) - (len(carry) % 2)
                        output_emitter.push(carry[:usable])
                        total_bytes += usable
                        chunk_count += 1

                # This chunk's audio has flushed to the emitter — delivered.
                self.chunks_delivered = chunk_index + 1

            logger.info(
                "TTS stream complete, bytes=%d chunks=%d duration=%.2fs",
                total_bytes, chunk_count, total_bytes / (SAMPLE_RATE * 2),
            )
            output_emitter.flush()

        except asyncio.TimeoutError as e:
            raise APITimeoutError() from e
        except aiohttp.ClientResponseError as e:
            raise APIStatusError(
                message=e.message,
                status_code=e.status,
                request_id=None,
                body=None,
            ) from e
        except (APIStatusError, APITimeoutError):
            raise
        except Exception as e:
            raise APIConnectionError() from e
        finally:
            # Tail-chunk accounting (WS-2, claim-vs-delivery): if synthesis
            # ended before every chunk flushed, the tail never reached the
            # air. Record it and log the partial cut so a mid-turn TTS
            # failure surfaces as a delivery gap the cut-recovery path
            # regenerates — never silent mid-sentence death. No-op on the
            # clean all-delivered path.
            if self.chunks_delivered < self.chunks_total:
                self.undelivered_remainder = "".join(
                    text_chunks[self.chunks_delivered:]
                )
                logger.warning(
                    "LILY_TTS | TAIL_CHUNK_UNDELIVERED | delivered=%d/%d | "
                    "remainder=%d chars — partial cut, cut-recovery "
                    "regenerates the tail",
                    self.chunks_delivered, self.chunks_total,
                    len(self.undelivered_remainder),
                )


async def lily_prewarm_tts_connection() -> None:
    """Establish the pooled TCP+TLS connection to ElevenLabs at session
    start so the FIRST synthesis request of the session skips the full
    handshake (~100-250ms off first-greeting TTFB). Any response status is
    fine — the connection in the shared keep-alive pool is the product.
    Fire-and-forget; never raises."""
    try:
        session = utils.http_context.http_session()
        async with session.get(
            f"{ELEVENLABS_API_BASE}/models",
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            logger.info("TTS | prewarm connection status=%s", resp.status)
    except Exception as e:
        logger.debug("TTS | prewarm skipped: %s", e)
