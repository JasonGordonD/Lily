"""Unit tests for the voice-preset surface (port of Zuna's
WO-ZUNA-VOICE-SWITCH-TOOL-001).

Config contract: voice1 is the hardcoded primary/default
(LILY_VOICE_1_DEFAULT, env-overridable via LILY_VOICE_1); voice2 is
Raven's voice — the former default — via LILY_VOICE_ID with
RAVEN_VOICE_ID fallback; lily_voice_id() (what LilyTTS boots with) must
resolve to voice1.

The lily_voice_switch tool module itself imports livekit, so those tests
are importorskip-guarded and only run where requirements.txt is
installed (CI); the config contract tests are pure and always run.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_config


VOICE_1_ID = "W3C2vBPukr5b5jvoXhPK"


# -- config contract -----------------------------------------------------------

def test_voice_1_hardcoded_default(monkeypatch):
    monkeypatch.delenv("LILY_VOICE_1", raising=False)
    assert lily_config.LILY_VOICE_1_DEFAULT == VOICE_1_ID
    assert lily_config.lily_voice_1() == VOICE_1_ID


def test_voice_1_env_override(monkeypatch):
    monkeypatch.setenv("LILY_VOICE_1", "override_voice_id")
    assert lily_config.lily_voice_1() == "override_voice_id"


def test_default_session_voice_is_voice_1(monkeypatch):
    """LilyTTS() boots on lily_voice_id() — it must resolve to voice1,
    even when the legacy Raven vars are still set in the environment."""
    monkeypatch.delenv("LILY_VOICE_1", raising=False)
    monkeypatch.setenv("LILY_VOICE_ID", "raven_voice_id")
    monkeypatch.setenv("RAVEN_VOICE_ID", "raven_fallback_id")
    assert lily_config.lily_voice_id() == VOICE_1_ID


def test_voice_2_reads_legacy_vars(monkeypatch):
    monkeypatch.setenv("LILY_VOICE_ID", "raven_voice_id")
    monkeypatch.setenv("RAVEN_VOICE_ID", "raven_fallback_id")
    assert lily_config.lily_voice_2() == "raven_voice_id"
    monkeypatch.delenv("LILY_VOICE_ID")
    assert lily_config.lily_voice_2() == "raven_fallback_id"
    monkeypatch.delenv("RAVEN_VOICE_ID")
    assert lily_config.lily_voice_2() is None


# -- tool module (requires livekit) --------------------------------------------

try:
    import livekit.agents  # noqa: F401
    _HAS_LIVEKIT = True
except ImportError:
    _HAS_LIVEKIT = False

requires_livekit = pytest.mark.skipif(
    not _HAS_LIVEKIT, reason="livekit-agents not installed"
)


@requires_livekit
def test_preset_values_snapshot(monkeypatch):
    import lily_voice_switch

    monkeypatch.delenv("LILY_VOICE_1", raising=False)
    monkeypatch.setenv("LILY_VOICE_ID", "raven_voice_id")
    values = lily_voice_switch._preset_values()
    assert values == {"voice1": VOICE_1_ID, "voice2": "raven_voice_id"}


@requires_livekit
def test_active_preset_reverse_lookup(monkeypatch):
    import lily_voice_switch

    monkeypatch.delenv("LILY_VOICE_1", raising=False)
    monkeypatch.setenv("LILY_VOICE_ID", "raven_voice_id")

    class _Opts:
        voice_id = "raven_voice_id"

    class _FakeTTS:
        _opts = _Opts()

    assert lily_voice_switch._active_preset_key(_FakeTTS()) == "voice2"
    _Opts.voice_id = VOICE_1_ID
    assert lily_voice_switch._active_preset_key(_FakeTTS()) == "voice1"
    _Opts.voice_id = "some_unknown_id"
    assert lily_voice_switch._active_preset_key(_FakeTTS()) is None


@requires_livekit
def test_per_voice_settings_resolution(monkeypatch):
    """voice1 carries its own tuning (stability 0.5 / speed 0.87); every
    other id — voice2 included — gets the baseline (0.4 / 0.90)."""
    import lily_tts

    monkeypatch.delenv("LILY_VOICE_1", raising=False)
    v1 = lily_tts._voice_settings_for(VOICE_1_ID)
    assert v1["stability"] == 0.5
    assert v1["speed"] == 0.87
    baseline = lily_tts._voice_settings_for("raven_voice_id")
    assert baseline["stability"] == 0.4
    assert baseline["speed"] == 0.90
    # shared invariants
    for settings in (v1, baseline):
        assert settings["similarity_boost"] == 0.9
        assert settings["style"] == 0.0
        assert settings["use_speaker_boost"] is True
    # env override moves the voice1 tuning with the id
    monkeypatch.setenv("LILY_VOICE_1", "override_voice_id")
    assert lily_tts._voice_settings_for("override_voice_id")["speed"] == 0.87
    assert lily_tts._voice_settings_for(VOICE_1_ID)["speed"] == 0.90


@requires_livekit
def test_set_voice_validation():
    from lily_tts import LilyTTS

    tts = LilyTTS(voice_id="initial", api_key="test_key")
    tts.set_voice("  new_voice  ")
    assert tts._opts.voice_id == "new_voice"
    with pytest.raises(ValueError):
        tts.set_voice("   ")
    with pytest.raises(ValueError):
        tts.set_voice("")
    assert tts._opts.voice_id == "new_voice"
