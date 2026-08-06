"""
lily_voice_identity.py — durable, device-independent speaker matching
(WO-LILY-VOICE-IDENTITY-001, the identity redesign core).

WHY THIS EXISTS
  Speechmatics refreshes its speaker-identifier blobs every session (prod-
  verified: one voice, seven sessions, seven distinct strings — see
  lily_agent.py's DEVICE_VERIFY_LABEL_MATCH note). So the vendor's native
  voiceprints can NEVER match a returning voice across sessions on their
  own; recognition today is device-linked (a localStorage `lily_group_id`)
  with the vendor blobs only used as a same-session verification gate.

  A player on a new device, a cleared browser, or a private window is
  therefore unrecognizable today — the "you should know my voice" gap.

  This module is the durable core that closes it: it matches a voice by a
  MODEL-INDEPENDENT embedding (a fixed-dim float vector produced by a
  speaker-verification model on the audio) against stored per-person
  centroids, by cosine similarity with a margin gate. It is deliberately
  pure (stdlib math only) and knows NOTHING about which embedding model or
  storage backend is used — the model choice, the audio-path extraction,
  the pgvector DDL, and the privacy/consent surface are separate seams
  (see docs/voice_identity_design.md). Keeping the matcher pure means it is
  fully testable now and survives whatever model Doc/operator pick.

CONTRACT
  - An embedding is a list[float] of fixed dimension D (same D across a
    deployment). Callers pass raw model output; this module L2-normalizes.
  - A person's stored identity is a CENTROID (running mean of that person's
    enrolled embeddings) plus a sample count, so more speech sharpens it.
  - A match requires BOTH an absolute-similarity floor AND a margin over
    the runner-up (two similar housemates must not collapse to one id).
  - Every function is defensive: bad shapes / empty inputs return the
    safe "no match" / unchanged value, never raise.
"""

import logging
import math
from typing import Optional

logger = logging.getLogger("lily_voice_identity")

# Cosine-similarity floor for a match (tunable; conservative by default so
# a false merge — greeting a stranger by another player's name — is far
# costlier than a miss, which degrades gracefully to "ask who's playing").
DEFAULT_MATCH_THRESHOLD = 0.75

# The best candidate must beat the runner-up by at least this margin, or the
# match is ambiguous and withheld (multi-person household safety).
DEFAULT_MATCH_MARGIN = 0.06


def lily_l2_normalize(vec) -> Optional[list]:
    """Return the unit-length copy of `vec`, or None if it is empty / not
    finite / zero-norm (nothing to normalize)."""
    if not vec:
        return None
    try:
        values = [float(x) for x in vec]
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(x) for x in values):
        return None
    norm = math.sqrt(sum(x * x for x in values))
    if norm <= 0.0:
        return None
    return [x / norm for x in values]


def lily_cosine_similarity(a, b) -> Optional[float]:
    """Cosine similarity of two equal-length vectors in [-1, 1], or None on
    a shape mismatch / degenerate input."""
    na, nb = lily_l2_normalize(a), lily_l2_normalize(b)
    if na is None or nb is None or len(na) != len(nb):
        return None
    return sum(x * y for x, y in zip(na, nb))


def lily_match_voice(
    probe,
    candidates,
    *,
    threshold: float = DEFAULT_MATCH_THRESHOLD,
    margin: float = DEFAULT_MATCH_MARGIN,
) -> Optional[dict]:
    """Match a probe embedding against stored per-person centroids.

    `candidates`: iterable of dicts, each {"group_id": str, "centroid":
    list[float]} (extra keys ignored). Returns the winning candidate as
    {"group_id", "score", "runner_up", "margin"} when the top score clears
    BOTH the absolute threshold AND the margin over the runner-up; None
    otherwise (no confident match → the caller cold-starts / asks).

    Pure ranking: no I/O, no side effects. Deterministic — ties break on
    the candidates' given order (stable)."""
    pn = lily_l2_normalize(probe)
    if pn is None or not candidates:
        return None
    scored = []
    for cand in candidates:
        try:
            gid = cand.get("group_id")
            centroid = cand.get("centroid")
        except AttributeError:
            continue
        if not gid:
            continue
        sim = lily_cosine_similarity(pn, centroid)
        if sim is None:
            continue
        scored.append((sim, gid))
    if not scored:
        return None
    # Stable sort by score descending (Python's sort is stable; negate to
    # keep original order on ties).
    scored_sorted = sorted(scored, key=lambda s: s[0], reverse=True)
    best_score, best_gid = scored_sorted[0]
    runner_up = scored_sorted[1][0] if len(scored_sorted) > 1 else None
    if best_score < threshold:
        logger.info(
            "LILY_VOICE_ID | NO_MATCH | best=%.4f threshold=%.2f",
            best_score, threshold,
        )
        return None
    gap = best_score - runner_up if runner_up is not None else best_score
    if runner_up is not None and gap < margin:
        logger.info(
            "LILY_VOICE_ID | AMBIGUOUS | best=%.4f runner_up=%.4f margin=%.3f "
            "— withheld", best_score, runner_up, margin,
        )
        return None
    logger.info(
        "LILY_VOICE_ID | MATCH | group=%s score=%.4f gap=%.3f",
        best_gid, best_score, gap,
    )
    return {
        "group_id": best_gid,
        "score": best_score,
        "runner_up": runner_up,
        "margin": gap,
    }


def lily_update_centroid(centroid, count, embedding):
    """Fold a fresh embedding into a person's running-mean centroid and
    return (new_centroid, new_count). The stored centroid is the mean of
    all enrolled unit embeddings, re-normalized — more speech across
    sessions sharpens the identity without unbounded storage.

    A first enrollment (centroid None / count 0) seeds from the embedding.
    Bad input returns the prior (centroid, count) unchanged."""
    en = lily_l2_normalize(embedding)
    if en is None:
        return centroid, int(count or 0)
    prior_count = int(count or 0)
    if not centroid or prior_count <= 0:
        return en, 1
    cn = lily_l2_normalize(centroid)
    if cn is None or len(cn) != len(en):
        # Stored centroid unusable/mismatched — reseed rather than corrupt.
        return en, 1
    n = prior_count
    merged = [(c * n + e) / (n + 1) for c, e in zip(cn, en)]
    normed = lily_l2_normalize(merged)
    if normed is None:
        return cn, prior_count
    return normed, prior_count + 1
