"""WO-LILY-VOICE-IDENTITY-001 — the track frame-sink probe buffer.

LilyVoiceProbe is the testable half of the frame sink (the livekit
AudioStream iteration + resampling is the thin agent-side seam). It buffers
16 kHz mono int16 samples into a rolling window and gates on a speech floor.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_voice_embedder as ve


def test_under_floor_is_not_ready_and_pcm_none():
    p = ve.LilyVoiceProbe(target_seconds=1.0)  # target 16000, floor 8000
    p.add_samples([100] * 5000)
    assert p.ready() is False
    assert p.pcm() is None


def test_reaches_floor_and_normalizes():
    p = ve.LilyVoiceProbe(target_seconds=1.0)
    p.add_samples([16384] * 9000)  # over the 8000 floor
    assert p.ready() is True
    pcm = p.pcm()
    assert pcm is not None
    assert abs(pcm[0] - 0.5) < 1e-6  # 16384/32768


def test_ring_buffer_keeps_most_recent():
    p = ve.LilyVoiceProbe(target_seconds=1.0)  # cap 16000
    p.add_samples([1] * 16000)
    p.add_samples([2] * 4000)  # pushes 4000 oldest out
    assert len(p) == 16000
    pcm = p.pcm()
    # tail is the newer value
    assert pcm[-1] == 2 / 32768.0


def test_add_none_and_bad_input_safe():
    p = ve.LilyVoiceProbe(target_seconds=1.0)
    p.add_samples(None)
    p.add_samples(12345)  # not iterable
    assert len(p) == 0
