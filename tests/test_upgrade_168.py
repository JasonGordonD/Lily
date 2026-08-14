"""WO-LILY-UPGRADE-168 — migration guard tests.

These pin the assumptions the 1.6.8 migration introduced, so a future
drift (a plugin bump that renames an event, a pin that slips) reads red
here at build time instead of silently dropping telemetry live — the exact
silent-failure class the WO exists to prevent.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_REPO = Path(__file__).resolve().parent.parent


def test_requirements_pinned_to_1_6_10():
    req = (_REPO / "requirements.txt").read_text()
    for pkg in [
        "livekit-agents",
        "livekit-plugins-speechmatics",
        "livekit-plugins-google",
        "livekit-plugins-silero",
        "livekit-plugins-openai",
    ]:
        assert re.search(rf"^{re.escape(pkg)}==1\.6\.10$", req, re.M), pkg
    # NC stays pinned where it was (compatible with 1.6.8; upgrades only for
    # compatibility, and this migration confirmed 0.2.6 is compatible).
    assert re.search(r"^livekit-plugins-noise-cancellation==0\.2\.6$", req, re.M)


def test_installed_agents_is_1_6_10():
    import livekit.agents as a
    # Operator-ordered bump 2026-08-14: 1.6.8 -> 1.6.10. The rest of this
    # file pins the BEHAVIORAL assumptions; all held across the bump
    # (2612 tests green before this pin moved).
    assert a.__version__ == "1.6.10", a.__version__


def test_blessed_metrics_surface_exists_on_pinned_framework():
    """The migration reads the NON-deprecated surface: session_usage_updated
    (-> AgentSessionUsage.model_usage) and per-turn ChatMessage.metrics
    (MetricsReport). If a bump renames either, this fails instead of the
    report silently losing the metrics block. metrics_collected is
    deliberately NOT subscribed (deprecated since 1.6.0, warns per event)."""
    from livekit.agents.voice.events import SessionUsageUpdatedEvent, EventTypes
    assert "usage" in SessionUsageUpdatedEvent.__annotations__
    literals = getattr(EventTypes, "__args__", ())
    assert "session_usage_updated" in literals
    # The per-turn latency report and its fields the collector reads.
    from livekit.agents.llm.chat_context import MetricsReport
    keys = MetricsReport.__annotations__
    for f in ("llm_node_ttft", "tts_node_ttfb", "e2e_latency",
              "transcription_delay", "end_of_turn_delay",
              "on_user_turn_completed_delay"):
        assert f in keys, f
    # The usage rollup type literals the collector dispatches on (annotations
    # are stringized under `from __future__ import annotations`).
    from livekit.agents.metrics import LLMModelUsage, TTSModelUsage, STTModelUsage
    assert "llm_usage" in str(LLMModelUsage.__annotations__["type"])
    assert "tts_usage" in str(TTSModelUsage.__annotations__["type"])
    assert "stt_usage" in str(STTModelUsage.__annotations__["type"])


def test_noise_cancellation_stays_off_post_upgrade():
    import lily_agent
    # Default (no LILY_NOISE_CANCELLATION env) resolves to no NC on input.
    assert lily_agent.lily_noise_cancellation_options() is None


def test_metrics_collector_consumes_real_framework_usage_shape():
    """A real 1.6.8 TTSModelUsage folds through the usage rollup — proves the
    .type dispatch matches the framework's actual literals."""
    import lily_metrics
    from livekit.agents.metrics import TTSModelUsage

    class _U:
        def __init__(self, entries): self.model_usage = entries

    u = TTSModelUsage(
        type="tts_usage", provider="elevenlabs", model="eleven_v3",
        input_tokens=0, output_tokens=0, characters_count=90, audio_duration=4.0,
    )
    c = lily_metrics.LilyMetricsCollector()
    c.collect_session_usage(_U([u]))
    s = c.summary()
    assert s["usage"]["tts_characters"] == 90
    assert s["usage"]["tts_audio_duration_s"] == 4.0
