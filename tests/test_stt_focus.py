"""WO-LILY-STT-001 Q0 — Speechmatics speaker focus (focus_mode=IGNORE).

Enrolled players are the game, every other voice is room. The SAFETY
invariant dominates these tests: focus_mode=IGNORE with an empty/absent
focus set would drop every voice and mute the whole table, so it must be
withheld unless focus is explicitly enabled AND the enrolled set is
non-empty. Default OFF — ships inert until the shouted-utterance acceptance
risk is measured on fixtures.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_config
import lily_agent
from lily_agent import lily_stt_focus_kwargs, SpeakerFocusMode


class _Spk:
    def __init__(self, label):
        self.label = label


def test_default_is_off():
    import os
    os.environ.pop("LILY_STT_FOCUS_MODE", None)
    assert lily_config.stt_focus_mode() == "off"


def test_off_yields_no_focus_kwargs(monkeypatch):
    monkeypatch.setattr(lily_config, "stt_focus_mode", lambda: "off")
    assert lily_stt_focus_kwargs([_Spk("Rami"), _Spk("Sam")]) == {}


def test_ignore_with_enrolled_builds_focus(monkeypatch):
    monkeypatch.setattr(lily_config, "stt_focus_mode", lambda: "ignore")
    fk = lily_stt_focus_kwargs([_Spk("Rami"), _Spk("Sam")])
    assert fk["focus_speakers"] == ["Rami", "Sam"]
    assert fk["focus_mode"] == SpeakerFocusMode.IGNORE


def test_ignore_with_empty_set_is_withheld(monkeypatch):
    # THE safety invariant: never enable IGNORE on an empty set (would mute
    # the whole table).
    monkeypatch.setattr(lily_config, "stt_focus_mode", lambda: "ignore")
    assert lily_stt_focus_kwargs([]) == {}
    assert lily_stt_focus_kwargs(None) == {}


def test_ignore_skips_labelless_speakers(monkeypatch):
    monkeypatch.setattr(lily_config, "stt_focus_mode", lambda: "ignore")
    fk = lily_stt_focus_kwargs([_Spk("Rami"), _Spk(None), _Spk("")])
    assert fk["focus_speakers"] == ["Rami"]


def test_all_labelless_under_ignore_is_withheld(monkeypatch):
    monkeypatch.setattr(lily_config, "stt_focus_mode", lambda: "ignore")
    assert lily_stt_focus_kwargs([_Spk(None), _Spk("")]) == {}


def test_config_rejects_unknown_values(monkeypatch):
    monkeypatch.setenv("LILY_STT_FOCUS_MODE", "banana")
    assert lily_config.stt_focus_mode() == "off"
    monkeypatch.setenv("LILY_STT_FOCUS_MODE", "IGNORE")
    assert lily_config.stt_focus_mode() == "ignore"
