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
    """Bounded accumulator for a session's captured speech, fed 16 kHz mono
    int16 samples by the track frame sink and read as normalized float PCM
    for embedding. Rate-agnostic (assumes the sink already resampled to
    ECAPA_SAMPLE_RATE); keeps at most `target_seconds` of the MOST RECENT
    audio (a ring buffer) so a long session doesn't grow unbounded and the
    probe reflects current, in-room voice.

    Pure/stdlib — the livekit AudioStream iteration and resampling live in
    the agent wiring; this is the fully-testable buffer + gate."""

    def __init__(self, target_seconds: float = 8.0,
                 sample_rate: int = ECAPA_SAMPLE_RATE,
                 match_seconds: float = 2.5):
        self._target = max(1, int(target_seconds * sample_rate))
        # A floor below which an embedding is too noisy to ENROLL.
        self._floor = max(1, int(0.5 * self._target))
        # MATCHING is a different job from enrolling and wants a different
        # bar. Enrollment folds a sample into a stored centroid, so it wants
        # a long clean take. Recognition only has to clear a cosine
        # threshold, which ECAPA does on a couple of seconds of speech.
        # Sharing one 4-second floor meant recognition waited for an
        # enrollment-grade sample before it could even try — and on a
        # congested loop that is minutes of wall clock, not seconds. Live
        # 2026-08-08: the match landed correctly ("NOW I've got you:
        # reigning champion, four wins") 3m36s into the session, long after
        # the greeting had already called the player a blank slate.
        self._match_floor = max(1, int(match_seconds * sample_rate))
        self._buf = collections.deque(maxlen=self._target)

    def add_samples(self, samples) -> None:
        """Append 16 kHz mono int16 samples (any iterable of ints).

        deque.extend, NOT a per-sample Python loop. The old form ran
        `for s in samples: append(int(s))` — sixteen thousand interpreter
        iterations per second per participant, on the EVENT LOOP, for audio
        that is already int16 so the int() was a no-op anyway. The sink
        feeding the probe was itself congesting the loop it shares with the
        Silero VAD, which is why recognition was slowest exactly when it
        most needed to be fast. extend() does the same work in C."""
        if samples is None:
            return
        try:
            self._buf.extend(samples)
        except TypeError:
            return

    def __len__(self) -> int:
        return len(self._buf)

    def ready(self) -> bool:
        """Enough speech accrued to ENROLL a usable centroid."""
        return len(self._buf) >= self._floor

    def match_ready(self) -> bool:
        """Enough speech accrued to attempt RECOGNITION — a lower bar than
        enrollment, so a returning voice is placed near the door instead of
        several minutes into the night."""
        return len(self._buf) >= self._match_floor

    def match_pcm(self) -> Optional[list]:
        """Normalized float PCM for a RECOGNITION attempt — same buffer,
        the lower floor."""
        if len(self._buf) < self._match_floor:
            return None
        return [s / 32768.0 for s in self._buf]

    def pcm(self) -> Optional[list]:
        """Normalized float PCM in [-1, 1], or None below the floor. int16
        is scaled by 1/32768."""
        if len(self._buf) < self._floor:
            return None
        return [s / 32768.0 for s in self._buf]

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
