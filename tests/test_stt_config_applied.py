"""WO-LILY-STT-001 Q3 — STT config attestation (applied == intended).

Reads the EFFECTIVE Speechmatics config off a constructed STT's
_stt_options and asserts it matches what the build intended. This makes the
audit's claimed-but-unwired class (the max_speakers=7 ghost) a build-time
red instead of a live surprise.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("SPEECHMATICS_API_KEY", "test-key")

import lily_agent
import lily_stt_tuning
from livekit.plugins.speechmatics import (
    STT, OperatingPoint, TurnDetectionMode, SpeakerFocusMode, SpeakerIdentifier,
)


def _build(**extra):
    tuned = lily_stt_tuning.lily_tuned_stt_kwargs()
    return STT(
        operating_point=OperatingPoint.ENHANCED,
        prefer_current_speaker=True,
        turn_detection_mode=TurnDetectionMode.FIXED,
        **extra,
        **{k: v for k, v in tuned.items() if k != "prefer_current_speaker"},
    )


def test_applied_reflects_intended_tuned_values():
    stt = _build()
    applied = lily_agent.lily_stt_config_applied(stt)
    tuned = lily_stt_tuning.lily_tuned_stt_kwargs()
    # The tuned levers actually reached the wire (no ghost).
    assert applied["operating_point"] in ("enhanced", "OperatingPoint.ENHANCED")
    assert applied["prefer_current_speaker"] is True
    if "max_speakers" in tuned:
        assert applied["max_speakers"] == tuned["max_speakers"]
    if "speaker_sensitivity" in tuned:
        assert applied["speaker_sensitivity"] == tuned["speaker_sensitivity"]


def test_applied_reports_focus_off_by_default():
    stt = _build()
    applied = lily_agent.lily_stt_config_applied(stt)
    assert applied["focus_mode"] in ("retain", "SpeakerFocusMode.RETAIN")
    assert applied["focus_speakers"] == 0


def test_applied_reports_focus_when_wired():
    stt = _build(
        focus_speakers=["Rami", "Sam"], focus_mode=SpeakerFocusMode.IGNORE,
    )
    applied = lily_agent.lily_stt_config_applied(stt)
    assert applied["focus_mode"] in ("ignore", "SpeakerFocusMode.IGNORE")
    assert applied["focus_speakers"] == 2


def test_config_applied_empty_on_stub():
    class _NoOpts: pass
    assert lily_agent.lily_stt_config_applied(_NoOpts()) == {}
