"""
lily_voice_embedder.py — the speaker-embedding extractor seam
(WO-LILY-VOICE-IDENTITY-001).

Turns captured speech into a fixed-dim embedding for
lily_voice_identity.lily_match_voice. Operator decision: ECAPA-TDNN
(SpeechBrain `spkrec-ecapa-voxceleb`, 192-dim), computed OFF the vocal path
— a bounded probe at session start and per-player enrollment at session
close, never per spoken turn.

The model dependency (torch + speechbrain) is heavy and lives in the deploy
image, NOT the test/dev tree. So this module is a graceful seam:

  - it imports cleanly with no ML deps present;
  - `lily_voice_embedder_available()` reports whether the model actually
    loaded (lazy, cached, one attempt);
  - `lily_extract_embedding(...)` returns None whenever the model is
    unavailable or extraction fails.

Every caller checks availability first, so a deploy without the model runs
exactly as before this module existed — recognition simply stays device-
linked. Nothing here ever raises into a session.
"""

import collections
import logging
import os
import threading
import time
from typing import Optional

logger = logging.getLogger("lily_voice_embedder")

# WHERE THE BAKED MODEL LIVES. The Dockerfile downloads ECAPA at build time
# so a live session never fetches it — but it was being written to
# /tmp/lily-ecapa, and /tmp is routinely mounted as tmpfs by the container
# runtime, which SHADOWS the baked copy and silently restores the cold
# download to the critical path. Live 2026-08-08 lily-2C489B: recognition
# landed 3m31s and SIXTEEN player turns after the greeting, while the
# player was saying "I have met you a million times", "you still don't
# remember me", "I just told you my name". The model was not slow to
# compare — it was slow to EXIST.
#
# /app is the image's own working directory: baked at build, never
# shadowed at runtime, writable by the appuser that runs the agent.
ECAPA_SAVEDIR = os.environ.get("LILY_ECAPA_SAVEDIR", "/app/.cache/lily-ecapa")

# Expected embedding dimension for the pinned model (ecapa-192). A model
# returning another dim is rejected so a misconfigured image can't poison
# the centroid pool with mismatched vectors.
ECAPA_DIM = 192

# ECAPA operates on 16 kHz mono; the track frame sink resamples to this.
ECAPA_SAMPLE_RATE = 16000


class LilyVoiceProbe:
    """The SPEECH-GATED probe buffer (WO-LILY-VOICE-TRUTH-001 V1).

    The old probe was wall-clock audio: every frame from track_subscribed
    went into one 8-second ring, the match fired at 2.5s of FRAMES, and
    enrollment read the same first 8s. In every instrumented session that
    2.5s mark was 20+s before any human spoke, so the centroids encoded
    room tone (same-device sessions 0.99 vs each other; 0/7 cross-device
    matches ever; zero seconds of the player scored 0.70 against his
    26-sample centroid).

    THE GATE, AS A STATED RULE (lily_config, rules (a)-(f)):
      (a) a frame is VOICED only when its wall-clock span falls inside a
          human (non-LILY) STT segment [segment_start, segment_end] fed by
          the transcript events — `note_voiced_segment` — or, when the
          segment feed is unavailable, inside a VAD user-speaking interval
          — `note_vad_state`. Which source is active is `gate_source`
          ("stt_segments" | "vad"), decided once, logged and persisted.
          Energy is never the gate.
      (b) `match_due()` is False until `voiced_seconds` >= min_voiced.
      (c) after an attempt (`mark_attempt`), it is False again until a
          further retry_voiced seconds of voiced audio has accrued.
      (d) `enroll_pcm()` is the UNION of voiced chunks, bounded to the most
          recent enroll_max seconds.
      (e)/(f) live on the game side (lily_identity: outcome + receipt).

    Mechanics: raw frames are kept UNRESAMPLED in a timestamped ring of
    `raw_window_seconds` (STT finals land ~1-3s after the audio, so the
    ring must outlast that lag). A voiced interval slices the ring, the
    slice is resampled once (the injected `resampler`), and the 16 kHz
    samples join the voiced union. Nothing is resampled per frame any
    more — the per-frame work is one deque append.

    Pure/stdlib: the livekit AudioStream iteration and the resampler live
    in the agent wiring; this is the fully-testable buffer + gate."""

    def __init__(
        self,
        *,
        min_voiced_seconds: float = 3.0,
        retry_voiced_seconds: float = 2.0,
        enroll_max_seconds: float = 30.0,
        raw_window_seconds: float = 30.0,
        match_window_seconds: float = 15.0,
        sample_rate: int = ECAPA_SAMPLE_RATE,
        gate_source: str = "auto",
        vad_fallback_after_seconds: float = 15.0,
        resampler=None,
        clock=None,
    ):
        self._rate = int(sample_rate)
        self._min_voiced = max(0.0, float(min_voiced_seconds))
        self._retry_voiced = max(0.0, float(retry_voiced_seconds))
        self._enroll_max = max(0.1, float(enroll_max_seconds))
        self._raw_window = max(1.0, float(raw_window_seconds))
        self._match_window = max(0.5, float(match_window_seconds))
        self._mode = gate_source if gate_source in ("auto", "stt", "vad") else "auto"
        self._vad_fallback_after = max(0.0, float(vad_fallback_after_seconds))
        self._resampler = resampler
        self._clock = clock or time.time
        # Raw ring: (t_start, t_end, in_rate, samples) in arrival order.
        self._raw = collections.deque()
        self._raw_span = 0.0
        # Voiced union at self._rate, bounded to enroll_max seconds.
        self._voiced = collections.deque(maxlen=max(1, int(self._enroll_max * self._rate)))
        self._voiced_seconds = 0.0          # cumulative, uncapped
        self._attempts = 0
        self._last_attempt_voiced = None
        self._gate_source = None            # "stt_segments" | "vad" | None
        self._segments_seen = 0
        # VAD interval bookkeeping (for "vad" mode and the "auto" fallback).
        self._vad_speaking = False
        self._vad_started_at = None
        self._vad_intervals = []            # closed (start, end) not yet sliced
        self._vad_seconds = 0.0             # cumulative VAD-detected speech
        self._matched = False

    # -- properties ---------------------------------------------------------

    @property
    def voiced_seconds(self) -> float:
        """Cumulative voiced audio seen (uncapped)."""
        return round(self._voiced_seconds, 3)

    @property
    def union_seconds(self) -> float:
        """Voiced audio currently held for enrollment (capped)."""
        return round(len(self._voiced) / self._rate, 3)

    @property
    def attempts(self) -> int:
        return self._attempts

    @property
    def gate_source(self) -> Optional[str]:
        return self._gate_source

    @property
    def min_voiced_seconds(self) -> float:
        return self._min_voiced

    def __len__(self) -> int:
        return len(self._voiced)

    # -- raw frames ---------------------------------------------------------

    def add_frame(self, samples, sample_rate: int = ECAPA_SAMPLE_RATE, at=None) -> None:
        """Append one raw frame (int16 samples at `sample_rate`) stamped
        with its arrival wall-clock `at` (default now). Frames are held
        unresampled in the timestamped ring; nothing here is voiced yet."""
        if samples is None:
            return
        try:
            n = len(samples)
        except TypeError:
            return
        if n <= 0:
            return
        rate = int(sample_rate) if sample_rate else self._rate
        t_end = float(at) if at is not None else self._clock()
        t_start = t_end - (n / rate)
        self._raw.append((t_start, t_end, rate, samples))
        self._raw_span = t_end - self._raw[0][0]
        while self._raw and (t_end - self._raw[0][1]) > self._raw_window:
            self._raw.popleft()

    # -- the gate -----------------------------------------------------------

    def note_voiced_segment(self, start, end) -> float:
        """(a) PRIMARY: a human STT segment [start, end] (wall-clock) has
        landed — slice the matching raw audio into the voiced union.
        Returns the voiced seconds added (0.0 when nothing overlapped, or
        when the gate is pinned to VAD)."""
        if self._mode == "vad":
            return 0.0
        try:
            start = float(start)
            end = float(end)
        except (TypeError, ValueError):
            return 0.0
        if end <= start:
            return 0.0
        self._segments_seen += 1
        if self._gate_source is None:
            self._gate_source = "stt_segments"
            logger.info(
                "LILY_VOICE_ID | GATE_SOURCE | source=stt_segments — voiced "
                "audio is frames inside human STT segments"
            )
        return self._slice_into_voiced(start, end)

    def note_vad_state(self, speaking: bool, at=None) -> float:
        """(a) FALLBACK: the framework VAD user-speaking flag, polled per
        frame. Records speaking intervals; in "vad" mode (or once the
        "auto" fallback has engaged) a closed interval is sliced into the
        voiced union. Returns voiced seconds added."""
        now = float(at) if at is not None else self._clock()
        speaking = bool(speaking)
        added = 0.0
        if speaking and not self._vad_speaking:
            self._vad_speaking = True
            self._vad_started_at = now
        elif not speaking and self._vad_speaking:
            self._vad_speaking = False
            start = self._vad_started_at if self._vad_started_at is not None else now
            self._vad_started_at = None
            if now > start:
                self._vad_seconds += now - start
                self._vad_intervals.append((start, now))
        if self._mode == "stt":
            self._vad_intervals.clear()
            return 0.0
        if self._gate_source is None:
            if self._mode == "vad":
                self._gate_source = "vad"
                logger.info(
                    "LILY_VOICE_ID | GATE_SOURCE | source=vad — pinned by "
                    "config; voiced audio is frames inside VAD intervals"
                )
            elif (
                self._segments_seen == 0
                and self._vad_seconds >= self._vad_fallback_after > 0
            ):
                self._gate_source = "vad"
                logger.warning(
                    "LILY_VOICE_ID | GATE_SOURCE | source=vad reason="
                    "no_timed_stt_segment_after_%.1fs_of_vad_speech — the "
                    "segment feed is unavailable; falling back to the VAD flag",
                    self._vad_seconds,
                )
        if self._gate_source == "vad":
            pending, self._vad_intervals = self._vad_intervals, []
            for s, e in pending:
                added += self._slice_into_voiced(s, e)
        else:
            # Keep only what the raw ring can still serve if the fallback
            # engages later.
            while self._vad_intervals and (
                now - self._vad_intervals[0][1] > self._raw_window
            ):
                self._vad_intervals.pop(0)
        return added

    def _slice_into_voiced(self, start: float, end: float) -> float:
        pieces = []
        in_rate = None
        for t0, t1, rate, samples in self._raw:
            if t1 <= start or t0 >= end:
                continue
            if in_rate is None:
                in_rate = rate
            elif rate != in_rate:
                continue  # a rate change mid-slice: skip the odd frame
            n = len(samples)
            lo = 0 if t0 >= start else int((start - t0) / (t1 - t0) * n)
            hi = n if t1 <= end else int((end - t0) / (t1 - t0) * n)
            if hi > lo:
                pieces.append(samples[lo:hi])
        if not pieces or in_rate is None:
            return 0.0
        out = []
        for piece in pieces:
            out.extend(piece)
        if in_rate != self._rate:
            if self._resampler is None:
                return 0.0
            try:
                out = list(self._resampler(out, in_rate) or [])
            except Exception as e:  # never raise into the session
                logger.warning("LILY_VOICE_ID | RESAMPLE_FAILED | %s", e)
                return 0.0
        if not out:
            return 0.0
        self._voiced.extend(out)
        added = len(out) / self._rate
        self._voiced_seconds += added
        return added

    # -- match scheduling ---------------------------------------------------

    def match_due(self) -> bool:
        """(b)/(c): enough NEW voiced audio for a (re)attempt."""
        if self._matched:
            return False
        if self._voiced_seconds < self._min_voiced:
            return False
        if self._attempts == 0 or self._last_attempt_voiced is None:
            return True
        return (self._voiced_seconds - self._last_attempt_voiced) >= self._retry_voiced

    def mark_attempt(self) -> int:
        self._attempts += 1
        self._last_attempt_voiced = self._voiced_seconds
        return self._attempts

    def mark_matched(self) -> None:
        self._matched = True

    def ready(self) -> bool:
        """Enough VOICED speech accrued to enroll (the hard floor)."""
        return self._voiced_seconds >= self._min_voiced and len(self._voiced) > 0

    def match_ready(self) -> bool:
        return self.match_due()

    def match_pcm(self) -> Optional[list]:
        """Normalized float PCM of the most recent match_window seconds of
        VOICED audio, or None under the minimum."""
        if not self.ready():
            return None
        n = int(self._match_window * self._rate)
        buf = self._voiced
        if len(buf) > n:
            buf = list(buf)[-n:]
        return [s / 32768.0 for s in buf]

    def enroll_pcm(self) -> Optional[list]:
        """(d) Normalized float PCM of the whole voiced union (bounded), or
        None under the minimum — never a byte of un-voiced audio."""
        if not self.ready():
            return None
        return [s / 32768.0 for s in self._voiced]

    def pcm(self) -> Optional[list]:
        return self.enroll_pcm()

    def receipt(self) -> dict:
        """The probe's half of the (f) receipt."""
        return {
            "voiced_seconds": self.voiced_seconds,
            "union_seconds": self.union_seconds,
            "attempts": self._attempts,
            "gate_source": self._gate_source,
            "segments_seen": self._segments_seen,
            "vad_seconds": round(self._vad_seconds, 3),
        }


_model = None
_load_attempted = False
_load_lock = threading.Lock()


def _load_model():
    """Lazy, one-shot, thread-safe model load. Returns the encoder or None.
    A missing dependency or load failure is logged once and cached as
    unavailable — never retried per call, never raised."""
    global _model, _load_attempted
    if _load_attempted:
        return _model
    with _load_lock:
        if _load_attempted:
            return _model
        _load_attempted = True
        try:
            # OFFLINE BY DEFAULT. `from_hparams` reaches Hugging Face to
            # resolve the revision even when every file is already cached,
            # so a cold or throttled network turns "load a local model"
            # into an unbounded wait sitting directly in front of
            # recognition. The baked image has the files; forbid the
            # round-trip rather than hope it is fast. Overridable for the
            # image build itself (LILY_ECAPA_ALLOW_FETCH=1), which is the
            # one moment a fetch is correct.
            if os.environ.get("LILY_ECAPA_ALLOW_FETCH") != "1":
                os.environ.setdefault("HF_HUB_OFFLINE", "1")
                os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
            # Imported here, not at module top: the dep is image-only.
            from speechbrain.inference.speaker import EncoderClassifier
            _model = EncoderClassifier.from_hparams(
                source="speechbrain/spkrec-ecapa-voxceleb",
                savedir=ECAPA_SAVEDIR,
            )
            logger.info(
                "LILY_VOICE_EMBEDDER | ECAPA loaded | savedir=%s offline=%s",
                ECAPA_SAVEDIR, os.environ.get("HF_HUB_OFFLINE", "0"),
            )
        except Exception as e:
            _model = None
            logger.info(
                "LILY_VOICE_EMBEDDER | unavailable (model not loaded: %s) — "
                "voice recognition stays device-linked this deploy",
                type(e).__name__,
            )
        return _model


def lily_voice_embedder_available() -> bool:
    """True only when the embedding model is present and loaded. Callers
    gate ALL enrollment/match work on this — False means the feature
    no-ops."""
    return _load_model() is not None


def lily_extract_embedding(
    samples, sample_rate: int = 16000
) -> Optional[list]:
    """Extract a 192-dim ECAPA embedding from mono PCM `samples` (a sequence
    of floats in [-1, 1], or a numpy array / torch tensor). Returns a
    list[float], or None on any failure / unavailable model / wrong output
    dim. Latency-insensitive by design (off the vocal path)."""
    model = _load_model()
    if model is None or samples is None:
        return None
    try:
        if callable(samples):
            # VOICE-TRUTH-001: the probe hands over a BUILDER so the float
            # list (up to enroll_max seconds) is materialized here, inside
            # the embedder thread, never on the event loop.
            samples = samples()
            if samples is None:
                return None
        import torch
        if not isinstance(samples, torch.Tensor):
            wav = torch.as_tensor(samples, dtype=torch.float32)
        else:
            wav = samples.to(torch.float32)
        if wav.ndim == 1:
            wav = wav.unsqueeze(0)  # (batch=1, time)
        with torch.no_grad():
            emb = model.encode_batch(wav)
        vec = emb.squeeze().detach().cpu().tolist()
        if not isinstance(vec, list) or len(vec) != ECAPA_DIM:
            logger.warning(
                "LILY_VOICE_EMBEDDER | UNEXPECTED_DIM | got=%s expected=%d",
                (len(vec) if isinstance(vec, list) else type(vec).__name__),
                ECAPA_DIM,
            )
            return None
        return [float(x) for x in vec]
    except Exception as e:
        logger.warning("LILY_VOICE_EMBEDDER | EXTRACT_FAILED | %s", e)
        return None


# ---------------------------------------------------------------------------
# Non-blocking availability (2026-08-08)
#
# lily_voice_embedder_available() CALLS _load_model(), and the first call
# downloads spkrec-ecapa-voxceleb from HuggingFace and loads a torch model.
# That is multi-second work. It was reachable from _voice_identity_ready(),
# which the transcript handler calls on the event loop on every final
# transcript — so the first player utterance blocked the loop for the whole
# load, and the Silero VAD (which shares that loop, and which drives
# barge-in and turn commit) fell behind by however long it took and never
# caught up. Measured live: 24.9s and 33s behind realtime.
#
# The docstring said "latency-insensitive by design (off the vocal path)".
# Off the vocal CALL GRAPH, yes. On the vocal EVENT LOOP all the same —
# which is the only thing scheduling cares about.
# ---------------------------------------------------------------------------


def lily_voice_embedder_loaded() -> bool:
    """Is the model ALREADY loaded? Pure read — never triggers a load, so it
    is safe to call from the event loop. False means 'not yet', not
    'unavailable': pair it with lily_warm_voice_embedder()."""
    return _model is not None


def lily_voice_embedder_load_attempted() -> bool:
    """Has a load been tried? Distinguishes 'still warming' from 'tried and
    genuinely unavailable', so a caller can stop waiting."""
    return _load_attempted


async def lily_warm_voice_embedder() -> bool:
    """Load the model OFF the event loop. Idempotent — _load_model latches
    on _load_attempted, so concurrent callers cost one load. Returns whether
    the model is usable afterwards."""
    import asyncio

    return await asyncio.to_thread(_load_model) is not None


async def lily_extract_embedding_async(samples, sample_rate: int = 16000):
    """lily_extract_embedding off the event loop. The ECAPA forward pass is
    hundreds of milliseconds to seconds of CPU on an 8-second probe; run
    inline it is a hard stall on every other task sharing the loop."""
    import asyncio

    return await asyncio.to_thread(lily_extract_embedding, samples, sample_rate)
