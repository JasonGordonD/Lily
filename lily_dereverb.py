"""
lily_dereverb.py — server-side pre-STT dereverberation node (WO-LILY-OMNIBUS-003
WS-16, AMENDMENT-002).

Default-off experiment behind LILY_DEREVERB_NODE ("off" | "wpe" | "aic").
The node sits between room audio and the Speechmatics STT stream
(LilyAgent.stt_node wraps the frame iterable). Off is a pure passthrough
that imports nothing beyond the stdlib — boot must never require the
dereverb dependencies.

wpe — nara_wpe block-online WPE (linear-prediction late-tail removal with a
delay margin; early reflections preserved by construction). Single-channel,
streaming STFT with overlap-add; emits exactly as many samples as it
receives, so frame cadence and stream timestamps are untouched — the only
timing cost is a constant in-stream algorithmic delay of one STFT window.

aic — ai-coustics enhancement via livekit-plugins-ai-coustics
(FrameProcessor, per-frame in-place enhancement, no added stream delay).
Requires LiveKit Cloud credentials; without them every frame passes through
unchanged with one structured log line.

Any failure anywhere degrades to passthrough — a broken enhancer must never
cost a session its STT.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from typing import Any, AsyncIterable, Optional

import lily_config

logger = logging.getLogger("lily.dereverb")

# STFT geometry scales with sample rate: 32 ms window / 8 ms hop.
_WINDOW_MS = 32
_HOP_MS = 8


class LilyWpeDereverb:
    """Streaming single-channel block-online WPE over int16 PCM blocks.

    process_block(pcm: bytes) -> bytes returns exactly len(pcm) bytes; the
    dereverberated signal inside the stream is delayed by one STFT window
    (algorithmic latency, reported via latency_seconds()).
    """

    def __init__(
        self,
        sample_rate: int,
        taps: int = 10,
        delay: int = 2,
        alpha: float = 0.9999,
    ) -> None:
        import numpy as np
        from nara_wpe.wpe import get_power_online, online_wpe_step

        self._np = np
        self._online_wpe_step = online_wpe_step
        self._get_power_online = get_power_online

        self.sample_rate = sample_rate
        self.taps = taps
        self.delay = delay
        self.alpha = alpha

        self._fft_size = 2 ** int(np.ceil(np.log2(sample_rate * _WINDOW_MS / 1000)))
        self._hop = max(1, int(sample_rate * _HOP_MS / 1000))
        self._window = np.sqrt(np.hanning(self._fft_size + 1)[:-1]).astype(np.float64)
        # sqrt-hann analysis+synthesis at this overlap sums to a constant.
        ola_gain = np.zeros(self._fft_size)
        for off in range(0, self._fft_size, self._hop):
            ola_gain[: self._fft_size - off] += (
                self._window[off:] * self._window[off:]
            )
        self._ola_norm = float(np.median(ola_gain))

        freq_bins = self._fft_size // 2 + 1
        self._buffer_frames: deque = deque(maxlen=taps + delay + 1)
        self._inv_cov = np.stack([np.identity(taps) for _ in range(freq_bins)]).astype(
            np.complex128
        )
        self._filter_taps = np.zeros((freq_bins, taps, 1), dtype=np.complex128)

        self._in_fifo = np.zeros(0, dtype=np.float64)
        # Overlap-add accumulator: position 0 is the next sample to be
        # finalized; each hop finalizes exactly `hop` samples.
        self._ola = np.zeros(self._fft_size, dtype=np.float64)
        # Prime the output with one window of silence: constant in-stream
        # delay, exact sample-count preservation from the first block.
        self._out_fifo = np.zeros(self._fft_size, dtype=np.float64)
        self._proc_time = 0.0
        self._audio_time = 0.0

    def latency_seconds(self) -> float:
        return self._fft_size / self.sample_rate

    def realtime_factor(self) -> Optional[float]:
        if self._audio_time <= 0:
            return None
        return self._proc_time / self._audio_time

    def process_block(self, pcm: bytes) -> bytes:
        np = self._np
        t0 = time.perf_counter()
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float64) / 32768.0
        n = len(samples)
        self._in_fifo = np.concatenate([self._in_fifo, samples])

        while len(self._in_fifo) >= self._fft_size:
            frame = self._in_fifo[: self._fft_size]
            self._in_fifo = self._in_fifo[self._hop :]
            spec = np.fft.rfft(frame * self._window)  # (F,)
            self._buffer_frames.append(spec[:, None])  # (F, 1)
            if len(self._buffer_frames) == self._buffer_frames.maxlen:
                y_step = np.stack(list(self._buffer_frames))  # (T, F, 1)
                try:
                    power = self._get_power_online(y_step.transpose(1, 2, 0))
                    z, self._inv_cov, self._filter_taps = self._online_wpe_step(
                        y_step,
                        power,
                        self._inv_cov,
                        self._filter_taps,
                        alpha=self.alpha,
                        taps=self.taps,
                        delay=self.delay,
                    )
                    out_spec = z[:, 0]
                except Exception:
                    logger.warning("DEREVERB | wpe step failed — passthrough frame")
                    out_spec = spec
            else:
                out_spec = spec
            block = np.fft.irfft(out_spec, n=self._fft_size) * self._window
            self._ola += block / self._ola_norm
            self._out_fifo = np.concatenate([self._out_fifo, self._ola[: self._hop]])
            self._ola = np.concatenate(
                [self._ola[self._hop :], np.zeros(self._hop, dtype=np.float64)]
            )

        out, self._out_fifo = self._out_fifo[:n], self._out_fifo[n:]
        if len(out) < n:  # never expected after priming; keep count exact
            out = np.concatenate([out, np.zeros(n - len(out), dtype=np.float64)])
        self._proc_time += time.perf_counter() - t0
        self._audio_time += n / self.sample_rate
        return (np.clip(out, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()


class _WpeFrameProcessor:
    """Per-frame adapter: same frame geometry out as in."""

    def __init__(self) -> None:
        self._wpe: Optional[LilyWpeDereverb] = None
        self._broken = False

    def process(self, frame: Any) -> Any:
        if self._broken or getattr(frame, "num_channels", 1) != 1:
            return frame
        try:
            if self._wpe is None or self._wpe.sample_rate != frame.sample_rate:
                self._wpe = LilyWpeDereverb(sample_rate=frame.sample_rate)
            processed = self._wpe.process_block(bytes(frame.data))
            return type(frame)(
                data=processed,
                sample_rate=frame.sample_rate,
                num_channels=frame.num_channels,
                samples_per_channel=frame.samples_per_channel,
            )
        except Exception as e:
            self._broken = True
            logger.warning("DEREVERB | wpe processor failed (%s) — passthrough", e)
            return frame


class _AicFrameProcessor:
    """ai-coustics enhancement adapter (lazy plugin import, fail-open)."""

    def __init__(self) -> None:
        self._enhancer: Any = None
        self._broken = False

    def _ensure(self) -> bool:
        if self._broken:
            return False
        if self._enhancer is not None:
            return True
        try:
            from livekit.plugins import ai_coustics

            self._enhancer = ai_coustics.audio_enhancement()
            url = lily_config.livekit_url()
            key = lily_config.livekit_api_key()
            secret = lily_config.livekit_api_secret()
            if url and key and secret:
                from livekit import api as lk_api

                token = (
                    lk_api.AccessToken(key, secret)
                    .with_identity("lily-dereverb-node")
                    .with_grants(lk_api.VideoGrants(room_join=True, room="lily-dereverb"))
                    .to_jwt()
                )
                self._enhancer._on_credentials_updated(token=token, url=url)
                self._enhancer._on_stream_info_updated(
                    room_name="lily-dereverb",
                    participant_identity="lily-dereverb-node",
                    publication_sid="lily-dereverb-track",
                )
            return True
        except Exception as e:
            self._broken = True
            logger.warning("DEREVERB | aic init failed (%s) — passthrough", e)
            return False

    def process(self, frame: Any) -> Any:
        if not self._ensure():
            return frame
        try:
            return self._enhancer._process(frame)
        except Exception as e:
            self._broken = True
            logger.warning("DEREVERB | aic process failed (%s) — passthrough", e)
            return frame


def lily_create_dereverb_processor(mode: Optional[str] = None) -> Optional[Any]:
    """Factory for the pre-STT processor. Returns None when the flag is off
    (the default) — the off path performs no heavy imports at all. Any
    construction failure returns None with one structured log line."""
    mode = mode if mode is not None else lily_config.dereverb_node_mode()
    if mode == "off":
        return None
    try:
        if mode == "wpe":
            proc = _WpeFrameProcessor()
        elif mode == "aic":
            proc = _AicFrameProcessor()
        else:
            return None
        logger.info("DEREVERB | node armed mode=%s", mode)
        return proc
    except Exception as e:
        logger.warning("DEREVERB | node construction failed (%s) — off", e)
        return None


async def lily_dereverb_frames(
    audio: AsyncIterable[Any], processor: Any
) -> AsyncIterable[Any]:
    """Wrap an audio-frame stream with the dereverb processor, 1:1 frames."""
    async for frame in audio:
        yield processor.process(frame)
