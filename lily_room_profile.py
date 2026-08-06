"""
lily_room_profile.py — session-start acoustic room profiling
(WO-LILY-OMNIBUS-003 WS-13 item 6, NEW in AMENDMENT-002).

Blind RT60 / DRR estimation from a few seconds of in-room speech —
dedicated reverberation physics, NOT devAIce `audio_quality` (which
measures SNR/clipping/quality and does not isolate reverberation; devAIce
output stays a coarse quality gate only, unchanged in
lily_audeering_consumers).

Method (single-channel, blind, numpy-only — no new dependencies):
  * Frame the PCM into short RMS-energy frames (dB domain).
  * RT60: find sustained energy-DECAY runs (speech offsets ringing into the
    room), fit a line to each run's dB slope, take the median decay rate,
    extrapolate to -60 dB. This is the classic blind decay-rate family
    (pyroomacoustics-class); on speech it is an ESTIMATE with real variance
    — the mapping below therefore uses coarse bands, never the raw float.
  * DRR proxy: energy ratio of frames near local peaks (direct-dominated)
    to the frames in their immediate decay shadow (reverb-dominated), in
    dB. A proxy, labeled as such — sufficient for band classification.

Profile mapping (WS-13 item 6):
  * high RT60  -> longer end-of-utterance silence thresholds + semantic
    turn detection recommendation (SMART_TURN needs the [smart] extra; the
    recommendation is recorded, adoption is a WS-8 stream-swap decision).
  * low DRR    -> lowered finalization-confidence thresholds + a state-block
    note asking Lily to listen generously (positive framing per the
    standing constraint).

The estimator consumes the FIRST audeering capture window (same bytes the
upload path already holds — zero new capture machinery, coverage duration
untouched).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

import numpy as np

logger = logging.getLogger("lily_room_profile")

_FRAME_MS = 20.0
_MIN_DECAY_FRAMES = 5          # >= 100 ms of monotonic-ish decay
_MIN_DECAY_DB = 8.0            # ignore shallow dips
_FLOOR_DB = -70.0              # silence floor guard

# Coarse bands — the raw estimates carry real variance on speech.
RT60_HIGH_S = 0.6              # above: reverberant room
DRR_LOW_DB = 2.0               # below: reverb-dominated capture


@dataclass
class LilyRoomProfile:
    rt60_estimate_s: Optional[float]
    drr_estimate_db: Optional[float]
    frames_analyzed: int
    decay_runs_used: int

    @property
    def reverberant(self) -> bool:
        return self.rt60_estimate_s is not None and self.rt60_estimate_s >= RT60_HIGH_S

    @property
    def low_drr(self) -> bool:
        return self.drr_estimate_db is not None and self.drr_estimate_db < DRR_LOW_DB


def lily_estimate_room_profile(
    pcm_int16: bytes,
    sample_rate: int,
) -> Optional[LilyRoomProfile]:
    """Blind room profile from mono int16 PCM. Returns None when the audio
    is too short/quiet to say anything (never guesses). Never raises."""
    try:
        if not pcm_int16 or sample_rate <= 0:
            return None
        samples = np.frombuffer(pcm_int16, dtype=np.int16).astype(np.float64)
        samples /= 32768.0
        frame_len = max(1, int(sample_rate * _FRAME_MS / 1000.0))
        n_frames = len(samples) // frame_len
        if n_frames < 50:  # < ~1s of audio
            return None
        frames = samples[: n_frames * frame_len].reshape(n_frames, frame_len)
        rms = np.sqrt(np.mean(frames * frames, axis=1))
        db = 20.0 * np.log10(np.maximum(rms, 1e-8))
        active = db > (_FLOOR_DB + 20.0)
        if active.sum() < 25:  # not enough speech energy
            return None

        # --- RT60: decay-run slope fitting -------------------------------
        decay_rates: list[float] = []
        run_start = None
        for i in range(1, n_frames):
            falling = db[i] < db[i - 1] and db[i] > _FLOOR_DB
            if falling and run_start is None:
                run_start = i - 1
            elif not falling and run_start is not None:
                run_len = i - run_start
                drop = db[run_start] - db[i - 1]
                if run_len >= _MIN_DECAY_FRAMES and drop >= _MIN_DECAY_DB:
                    x = np.arange(run_start, i) * (_FRAME_MS / 1000.0)
                    slope = np.polyfit(x, db[run_start:i], 1)[0]  # dB/s, negative
                    if slope < -1.0:
                        decay_rates.append(-slope)
                run_start = None

        rt60 = None
        if decay_rates:
            rt60 = float(60.0 / np.median(decay_rates))
            rt60 = float(min(rt60, 5.0))  # cap absurd extrapolations

        # --- DRR proxy: peak frames vs their decay shadow ----------------
        drr = None
        peak_thresh = np.percentile(db[active], 80)
        peaks = np.where(db >= peak_thresh)[0]
        direct, tail = [], []
        for p in peaks:
            direct.append(rms[p] ** 2)
            lo, hi = p + 2, min(p + 6, n_frames)  # 40-120 ms after the peak
            if hi > lo:
                tail.append(float(np.mean(rms[lo:hi] ** 2)))
        if direct and tail and np.mean(tail) > 0:
            drr = float(10.0 * np.log10(np.mean(direct) / np.mean(tail)))

        return LilyRoomProfile(
            rt60_estimate_s=rt60,
            drr_estimate_db=drr,
            frames_analyzed=n_frames,
            decay_runs_used=len(decay_rates),
        )
    except Exception as exc:  # noqa: BLE001 — sensor must never cascade
        logger.warning("LILY_ROOM_PROFILE | estimate failed: %r", exc)
        return None


def lily_profile_stt_adjustments(profile: LilyRoomProfile) -> dict[str, Any]:
    """Profile -> recommended STT/adjudication adjustments (item 6 mapping).

    Pure mapping; callers decide adoption. `end_of_utterance_silence_trigger`
    stays inside the plugin validator's (0, 2) bound. The finalization-
    confidence delta lowers Tier-1's fuzzy threshold via the existing
    threshold parameter (lily_evaluation), never a new gate."""
    adjustments: dict[str, Any] = {
        "end_of_utterance_silence_trigger": None,
        "recommend_semantic_turn_detection": False,
        "tier1_threshold_delta": 0.0,
        "state_note": None,
    }
    if profile.reverberant:
        adjustments["end_of_utterance_silence_trigger"] = 1.0
        adjustments["recommend_semantic_turn_detection"] = True
    if profile.low_drr:
        adjustments["tier1_threshold_delta"] = -0.05
        adjustments["state_note"] = (
            "[room profile: lively acoustics — listen generously and favor "
            "phonetic matches when an answer sounds close]"
        )
    return adjustments
