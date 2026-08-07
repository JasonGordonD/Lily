"""WO-LILY-UPGRADE-168 U3(b) + "she must use all the metrics she can" —
the 1.6.8 metrics accumulator, on the BLESSED (non-deprecated) surface.

Per-turn latency/turn-taking comes from ChatMessage.metrics (a MetricsReport
dict, total=False); cumulative token/audio usage from session_usage_updated
-> AgentSessionUsage.model_usage. The coupling audit confirmed
metrics_collected is deprecated since 1.6.0 and must not be subscribed.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_metrics


class _Usage:
    """Stand-in for AgentSessionUsage (attribute .model_usage)."""
    def __init__(self, entries):
        self.model_usage = entries


class _Entry:
    def __init__(self, **kw):
        self.__dict__.update(kw)


# -- per-turn latency (agent turns) -------------------------------------------


def test_agent_turn_latency_folds():
    c = lily_metrics.LilyMetricsCollector()
    c.collect_turn({"llm_node_ttft": 0.2, "tts_node_ttfb": 0.15,
                    "playback_latency": 0.3, "e2e_latency": 0.8})
    c.collect_turn({"llm_node_ttft": 0.4, "tts_node_ttfb": 0.25,
                    "e2e_latency": 1.2})
    s = c.summary()
    assert s["turns_measured"] == 2
    assert s["latency"]["llm_ttft_ms_p50"] is not None
    assert s["latency"]["tts_ttfb_ms_p50"] is not None
    assert s["latency"]["e2e_latency_ms_p95"] is not None


# -- per-turn turn-taking (user turns) — STT-001 Q2 quality signals -----------


def test_user_turn_taking_folds():
    c = lily_metrics.LilyMetricsCollector()
    for d in (0.5, 0.7, 0.9):
        c.collect_turn({"transcription_delay": d / 2, "end_of_turn_delay": d,
                        "on_user_turn_completed_delay": d + 0.1})
    s = c.summary()
    tt = s["turn_taking"]
    assert tt["transcription_delay_ms_p50"] is not None
    assert tt["end_of_turn_delay_ms_p50"] is not None
    assert tt["on_user_turn_completed_delay_ms_p50"] is not None


def test_zero_delay_is_valid_turn_taking():
    # A transcription_delay of 0.0 is legitimate and must be recorded.
    c = lily_metrics.LilyMetricsCollector()
    c.collect_turn({"transcription_delay": 0.0, "end_of_turn_delay": 0.0})
    s = c.summary()
    assert s["turn_taking"]["transcription_delay_ms_p50"] == 0.0


# -- cumulative usage (session_usage_updated) ---------------------------------


def test_session_usage_rollup_by_type():
    c = lily_metrics.LilyMetricsCollector()
    usage = _Usage([
        _Entry(type="llm_usage", input_tokens=1000, input_cached_tokens=200,
               output_tokens=400),
        _Entry(type="tts_usage", characters_count=850, audio_duration=42.0),
        _Entry(type="stt_usage", audio_duration=61.5),
    ])
    c.collect_session_usage(usage)
    u = c.summary()["usage"]
    assert u["llm_input_tokens"] == 1000
    assert u["llm_input_cached_tokens"] == 200
    assert u["llm_output_tokens"] == 400
    assert u["tts_characters"] == 850
    assert u["tts_audio_duration_s"] == 42.0
    assert u["stt_audio_duration_s"] == 61.5
    assert u["models"] == 3


def test_session_usage_sums_across_models_of_same_type():
    c = lily_metrics.LilyMetricsCollector()
    usage = _Usage([
        _Entry(type="llm_usage", input_tokens=100, output_tokens=50),
        _Entry(type="llm_usage", input_tokens=300, output_tokens=90),  # 2nd model
    ])
    c.collect_session_usage(usage)
    u = c.summary()["usage"]
    assert u["llm_input_tokens"] == 400
    assert u["llm_output_tokens"] == 140


def test_latest_usage_snapshot_wins():
    # session_usage_updated is cumulative; the latest snapshot replaces.
    c = lily_metrics.LilyMetricsCollector()
    c.collect_session_usage(_Usage([_Entry(type="llm_usage", input_tokens=100)]))
    c.collect_session_usage(_Usage([_Entry(type="llm_usage", input_tokens=500)]))
    assert c.summary()["usage"]["llm_input_tokens"] == 500


# -- defensiveness ------------------------------------------------------------


def test_empty_and_none_never_raise():
    c = lily_metrics.LilyMetricsCollector()
    c.collect_turn(None)
    c.collect_turn({})
    c.collect_session_usage(None)
    assert c.summary() == {"turns_measured": 0}


def test_partial_report_folds_present_fields_only():
    c = lily_metrics.LilyMetricsCollector()
    c.collect_turn({"e2e_latency": 0.9})  # only one field
    s = c.summary()
    assert s["turns_measured"] == 1
    assert "e2e_latency_ms_p50" in s["latency"]
    assert "llm_ttft_ms_p50" not in s["latency"]


def test_bad_usage_entry_is_skipped():
    c = lily_metrics.LilyMetricsCollector()
    c.collect_session_usage(_Usage([_Entry(type="unknown_usage", foo=1)]))
    u = c.summary()["usage"]
    assert u["llm_input_tokens"] == 0
    assert u["models"] == 1
