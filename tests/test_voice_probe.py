"""WO-LILY-VOICE-TRUTH-001 V1 — the SPEECH-GATED probe buffer (LilyVoiceProbe).

The pre-WO probe was wall-clock audio: every frame fed one 8s ring, the
match fired at 2.5s of FRAMES, enrollment read the same first 8s. Auditor B
showed every instrumented session's 2.5s mark was 20+s before any human
spoke — the centroids were room tone (same-device 0.99, 0/7 cross-device,
zero seconds of the player scoring 0.70 against his 26-sample centroid).

Pinned here, as the stated rules (lily_config, (a)-(d)):
  (a) voiced = frames inside a human STT segment [start, end] (primary), or
      inside a VAD user-speaking interval when the segment feed is
      unavailable (fallback) — the active source is `gate_source`;
  (b) no match is due before min_voiced seconds of VOICED audio — 8s of
      silence-shaped frames earn nothing;
  (c) a further attempt is due only after retry_voiced more voiced seconds;
  (d) enroll_pcm() is the union of voiced chunks, bounded by enroll_max —
      not one sample of un-voiced audio.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_voice_embedder as ve

RATE = 16000
FRAME = 160  # 10 ms frames, like the live sink


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _probe(clock, **kw):
    kw.setdefault("min_voiced_seconds", 3.0)
    kw.setdefault("retry_voiced_seconds", 2.0)
    kw.setdefault("enroll_max_seconds", 30.0)
    return ve.LilyVoiceProbe(clock=clock, **kw)


def _feed(probe, clock, seconds, value):
    """Feed `seconds` of 10 ms frames of constant `value` (silence-shaped
    when tiny, voiced-shaped when loud — the probe must NOT care: energy
    is never the gate)."""
    for _ in range(int(seconds * RATE / FRAME)):
        clock.t += FRAME / RATE
        probe.add_frame([value] * FRAME, sample_rate=RATE)


# -- (b): silence-shaped frames earn no match ----------------------------------


def test_eight_seconds_of_frames_without_a_segment_earn_no_match():
    """THE finding. 8s of wall-clock frames, no human segment: the old probe
    was match_ready at 2.5s; the gated probe is not due, has no PCM, and
    holds zero voiced seconds."""
    clock = _Clock()
    p = _probe(clock)
    _feed(p, clock, 8.0, 3)  # room tone
    assert p.voiced_seconds == 0.0
    assert p.match_due() is False
    assert p.match_ready() is False
    assert p.match_pcm() is None
    assert p.enroll_pcm() is None
    assert p.gate_source is None


def test_loud_frames_without_a_segment_still_earn_nothing():
    """Energy is NOT the gate (addendum #2 (a)): loud room noise with no
    human STT segment is not voiced."""
    clock = _Clock()
    p = _probe(clock)
    _feed(p, clock, 6.0, 12000)
    assert p.voiced_seconds == 0.0
    assert p.match_due() is False


# -- (a) primary: STT segments slice the raw ring -----------------------------


def test_segment_slices_only_the_frames_inside_it():
    clock = _Clock()
    p = _probe(clock)
    _feed(p, clock, 8.0, 3)            # silence-shaped, t=1000..1008
    t_speak0 = clock.t
    _feed(p, clock, 4.0, 1000)         # the human, t=1008..1012
    t_speak1 = clock.t
    _feed(p, clock, 2.0, 3)            # silence after
    added = p.note_voiced_segment(t_speak0, t_speak1)
    assert abs(added - 4.0) < 0.05
    assert abs(p.voiced_seconds - 4.0) < 0.05
    assert p.gate_source == "stt_segments"
    pcm = p.enroll_pcm()
    assert pcm is not None
    # (d) the union carries ONLY voiced samples — not one silence sample.
    assert all(abs(s - 1000 / 32768.0) < 1e-9 for s in pcm)
    assert abs(len(pcm) / RATE - 4.0) < 0.05


def test_match_due_only_after_min_voiced_then_retry_step():
    clock = _Clock()
    p = _probe(clock)
    _feed(p, clock, 5.0, 3)
    t0 = clock.t
    _feed(p, clock, 2.0, 500)
    p.note_voiced_segment(t0, clock.t)      # 2.0s voiced < 3.0 min
    assert p.match_due() is False
    t1 = clock.t
    _feed(p, clock, 1.5, 500)
    p.note_voiced_segment(t1, clock.t)      # 3.5s voiced >= min
    assert p.match_due() is True
    assert p.mark_attempt() == 1
    assert p.match_due() is False           # (c) nothing new yet
    t2 = clock.t
    _feed(p, clock, 1.0, 500)
    p.note_voiced_segment(t2, clock.t)      # +1.0 < retry 2.0
    assert p.match_due() is False
    t3 = clock.t
    _feed(p, clock, 1.2, 500)
    p.note_voiced_segment(t3, clock.t)      # +2.2 >= retry
    assert p.match_due() is True
    p.mark_attempt()
    p.mark_matched()
    t4 = clock.t
    _feed(p, clock, 5.0, 500)
    p.note_voiced_segment(t4, clock.t)
    assert p.match_due() is False           # matched: never again


def test_segment_outside_the_raw_ring_adds_nothing():
    clock = _Clock()
    p = _probe(clock, raw_window_seconds=5.0)
    _feed(p, clock, 20.0, 700)
    # A segment older than the ring can serve is gone.
    assert p.note_voiced_segment(clock.t - 19.0, clock.t - 15.0) == 0.0
    assert p.voiced_seconds == 0.0


def test_enroll_union_is_bounded_to_enroll_max():
    clock = _Clock()
    p = _probe(clock, enroll_max_seconds=3.0, raw_window_seconds=60.0)
    _feed(p, clock, 10.0, 900)
    p.note_voiced_segment(clock.t - 10.0, clock.t)
    assert abs(p.voiced_seconds - 10.0) < 0.05   # cumulative, uncapped
    assert abs(p.union_seconds - 3.0) < 0.01     # (d) bounded union
    assert abs(len(p.enroll_pcm()) / RATE - 3.0) < 0.01


def test_match_pcm_is_the_most_recent_window_of_voiced_audio():
    clock = _Clock()
    p = _probe(clock, match_window_seconds=2.0, raw_window_seconds=60.0)
    _feed(p, clock, 4.0, 100)
    _feed(p, clock, 4.0, 200)
    p.note_voiced_segment(clock.t - 8.0, clock.t)
    pcm = p.match_pcm()
    assert abs(len(pcm) / RATE - 2.0) < 0.01
    assert all(abs(s - 200 / 32768.0) < 1e-9 for s in pcm)  # the newest 2s


def test_bad_input_is_safe():
    p = _probe(_Clock())
    p.add_frame(None)
    p.add_frame(12345)
    p.add_frame([])
    assert p.note_voiced_segment("x", None) == 0.0
    assert p.note_voiced_segment(5.0, 4.0) == 0.0
    assert len(p) == 0


# -- (a) fallback: the VAD flag, only when segments are unavailable ------------


def test_vad_intervals_are_not_voiced_while_segments_are_expected():
    """auto mode: VAD speech under the fallback threshold with no segment
    yet is HELD, not embedded — the segment feed may simply be late."""
    clock = _Clock()
    p = _probe(clock, vad_fallback_after_seconds=15.0)
    p.note_vad_state(True)
    _feed(p, clock, 4.0, 800)
    p.note_vad_state(False)
    assert p.voiced_seconds == 0.0
    assert p.gate_source is None


def test_vad_fallback_engages_when_no_segment_ever_arrives():
    clock = _Clock()
    p = _probe(clock, vad_fallback_after_seconds=6.0, raw_window_seconds=60.0)
    for _ in range(2):
        p.note_vad_state(True)
        _feed(p, clock, 4.0, 800)
        p.note_vad_state(False)
        _feed(p, clock, 1.0, 2)
    # 8s of VAD speech, zero segments: the fallback engages and BACK-FILLS
    # the held intervals from the raw ring.
    assert p.gate_source == "vad"
    assert abs(p.voiced_seconds - 8.0) < 0.1
    assert all(abs(s - 800 / 32768.0) < 1e-9 for s in p.enroll_pcm())
    # Once on VAD, a later interval slices immediately.
    p.note_vad_state(True)
    _feed(p, clock, 2.0, 800)
    assert p.note_vad_state(False) > 1.9


def test_a_segment_arriving_first_pins_the_gate_to_stt():
    clock = _Clock()
    p = _probe(clock, vad_fallback_after_seconds=15.0)
    p.note_vad_state(True)
    _feed(p, clock, 3.0, 600)
    p.note_vad_state(False)
    p.note_voiced_segment(clock.t - 3.0, clock.t)  # the final lands ~now
    assert p.gate_source == "stt_segments"
    # More VAD speech never flips the gate once segments have reported.
    p.note_vad_state(True)
    _feed(p, clock, 5.0, 600)
    p.note_vad_state(False)
    assert p.gate_source == "stt_segments"
    assert abs(p.voiced_seconds - 3.0) < 0.05


def test_explicit_gate_modes():
    clock = _Clock()
    vad_only = _probe(clock, gate_source="vad")
    _feed(vad_only, clock, 3.0, 500)
    assert vad_only.note_voiced_segment(clock.t - 3.0, clock.t) == 0.0
    vad_only.note_vad_state(True)
    _feed(vad_only, clock, 3.0, 500)
    assert vad_only.note_vad_state(False) > 2.9
    assert vad_only.gate_source == "vad"

    stt_only = _probe(clock, gate_source="stt", vad_fallback_after_seconds=1.0)
    stt_only.note_vad_state(True)
    _feed(stt_only, clock, 5.0, 500)
    stt_only.note_vad_state(False)
    assert stt_only.voiced_seconds == 0.0
    assert stt_only.gate_source is None


def test_receipt_carries_the_gate_numbers():
    clock = _Clock()
    p = _probe(clock)
    _feed(p, clock, 4.0, 500)
    p.note_voiced_segment(clock.t - 4.0, clock.t)
    p.mark_attempt()
    r = p.receipt()
    assert r["gate_source"] == "stt_segments"
    assert abs(r["voiced_seconds"] - 4.0) < 0.05
    assert r["attempts"] == 1
    assert r["segments_seen"] == 1
