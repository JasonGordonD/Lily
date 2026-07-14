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
  - voice_settings: stability 0.4, similarity 0.9, style 0.0,
    speaker_boost, speed 0.93 (Raven's voice baseline)
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
MAX_CHUNK_SIZE = 5000

VOICE_SETTINGS = {
    "stability": 0.4,
    "similarity_boost": 0.9,
    "style": 0.0,
    "use_speaker_boost": True,
    "speed": 0.93,
}


@dataclass
class _TTSOpts:
    voice_id: str
    api_key: str
    model_id: str
    output_format: str


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
        )

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
    # AudioEmitter initialize/push/flush) verified unchanged at 1.6.4.
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

    @staticmethod
    def _split_text(text: str) -> list[str]:
        """Split text at sentence boundaries if it exceeds MAX_CHUNK_SIZE."""
        if len(text) <= MAX_CHUNK_SIZE:
            return [text]
        chunks = []
        remaining = text
        while len(remaining) > MAX_CHUNK_SIZE:
            boundary = -1
            for m in re.finditer(r'[.!?]\s', remaining[:MAX_CHUNK_SIZE]):
                boundary = m.end()
            if boundary == -1:
                boundary = MAX_CHUNK_SIZE
            chunks.append(remaining[:boundary])
            remaining = remaining[boundary:]
        if remaining:
            chunks.append(remaining)
        logger.info(
            "TTS | splitting text: %d chars into %d chunks",
            len(text), len(chunks),
        )
        return chunks

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        if self._skip_empty:
            logger.warning("TTS | skipping empty text (text=%r)", self._input_text)
            return

        text_chunks = self._split_text(self._input_text)

        url = (
            f"{ELEVENLABS_API_BASE}/text-to-speech/{self._opts.voice_id}"
            f"/stream?output_format={self._opts.output_format}"
        )

        try:
            initialized = False
            total_bytes = 0
            chunk_count = 0

            for text_chunk in text_chunks:
                body = {
                    "text": text_chunk,
                    "model_id": self._opts.model_id,
                    "voice_settings": VOICE_SETTINGS,
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
