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

import logging
import threading
from typing import Optional

logger = logging.getLogger("lily_voice_embedder")

# Expected embedding dimension for the pinned model (ecapa-192). A model
# returning another dim is rejected so a misconfigured image can't poison
# the centroid pool with mismatched vectors.
ECAPA_DIM = 192

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
            # Imported here, not at module top: the dep is image-only.
            from speechbrain.inference.speaker import EncoderClassifier
            _model = EncoderClassifier.from_hparams(
                source="speechbrain/spkrec-ecapa-voxceleb",
                savedir="/tmp/lily-ecapa",
            )
            logger.info("LILY_VOICE_EMBEDDER | ECAPA loaded")
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
