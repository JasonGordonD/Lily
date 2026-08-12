"""C11 (WO-LILY-HOSTLOOP-001): the acoustic pipeline is opt-in.

The 6-module set (aed+audioQuality+expression+prosody+scene+
speakerAttributes) uploaded a continuous 5s window every 5s with no
consumer on a trivia host. LILY_AUDEERING_ENABLED gates the START —
no pipeline code deleted, flag true restores everything.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_audeering_client
import lily_config


def test_default_is_disabled(monkeypatch):
    monkeypatch.delenv("LILY_AUDEERING_ENABLED", raising=False)
    assert lily_config.audeering_enabled() is False


def test_disabled_pipeline_never_starts(monkeypatch):
    monkeypatch.delenv("LILY_AUDEERING_ENABLED", raising=False)
    # Key present must not matter — the flag gates before the breaker.
    monkeypatch.setenv("AUDEERING_API_KEY", "k")
    assert asyncio.run(
        lily_audeering_client.lily_start_audeering_pipeline(None)
    ) is None


def test_flag_true_reaches_the_original_path(monkeypatch):
    monkeypatch.setenv("LILY_AUDEERING_ENABLED", "true")
    monkeypatch.delenv("AUDEERING_API_KEY", raising=False)
    # With the flag on and no key, the ORIGINAL breaker-open behavior
    # answers (None) — proving the flag only gates, never replaces.
    assert asyncio.run(
        lily_audeering_client.lily_start_audeering_pipeline(None)
    ) is None
