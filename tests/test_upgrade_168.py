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


def test_requirements_pinned_to_1_6_8():
    req = (_REPO / "requirements.txt").read_text()
    for pkg in [
        "livekit-agents",
        "livekit-plugins-speechmatics",
        "livekit-plugins-google",
        "livekit-plugins-silero",
        "livekit-plugins-openai",
    ]:
        assert re.search(rf"^{re.escape(pkg)}==1\.6\.8$", req, re.M), pkg
    # NC stays pinned where it was (compatible with 1.6.8; upgrades only for
    # compatibility, and this migration confirmed 0.2.6 is compatible).
    assert re.search(r"^livekit-plugins-noise-cancellation==0\.2\.6$", req, re.M)


def test_installed_agents_is_1_6_8():
    import livekit.agents as a
    assert a.__version__ == "1.6.8", a.__version__


def test_metrics_events_exist_on_pinned_framework():
    """The migration subscribes to metrics_collected + session_usage_updated
    and reads MetricsCollectedEvent.metrics / SessionUsageUpdatedEvent.usage.
    If a bump renames either, this fails instead of the report silently
    losing the metrics block."""
    from livekit.agents.voice.events import (
        MetricsCollectedEvent,
        SessionUsageUpdatedEvent,
    )
    assert "metrics" in MetricsCollectedEvent.__annotations__
    assert "usage" in SessionUsageUpdatedEvent.__annotations__
    # And the event-name literals our @session.on(...) handlers use.
    from livekit.agents.voice.events import EventTypes
    literals = getattr(EventTypes, "__args__", ())
    assert "metrics_collected" in literals
    assert "session_usage_updated" in literals


def test_noise_cancellation_stays_off_post_upgrade():
    import lily_agent
    # Default (no LILY_NOISE_CANCELLATION env) resolves to no NC on input.
    assert lily_agent.lily_noise_cancellation_options() is None


def test_metrics_collector_consumes_real_framework_metric_shape():
    """A real 1.6.8 TTSMetrics folds through our collector — proves the
    duck-typed .type dispatch matches the framework's actual literals."""
    import lily_metrics
    from livekit.agents.metrics import TTSMetrics
    m = TTSMetrics(
        type="tts_metrics", label="eleven", request_id="r1", timestamp=0.0,
        ttfb=0.12, duration=1.0, audio_duration=4.0, cancelled=False,
        characters_count=90, input_tokens=0, output_tokens=0, streamed=True,
        acquire_time=0.0, connection_reused=True, segment_id=None,
        speech_id=None, metadata=None,
    )
    c = lily_metrics.LilyMetricsCollector()
    c.collect(m)
    s = c.summary()
    assert s["tts"]["calls"] == 1
    assert s["tts"]["characters"] == 90
    assert s["tts"]["ttfb_ms_p50"] == 120.0
