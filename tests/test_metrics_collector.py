"""WO-LILY-UPGRADE-168 U3(b) + "she must use all the metrics she can" —
the 1.6.8 metrics accumulator.

Feeds fake typed metrics (duck-typed on .type, exactly as the framework's
LLMMetrics/STTMetrics/TTSMetrics/EOUMetrics/VADMetrics arrive on the
metrics_collected event) and asserts the comprehensive per-session summary
that lands in the report metadata.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_metrics


class _M:
    """A stand-in for a framework metric object — attribute access only."""
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_llm_metrics_fold_tokens_and_ttft():
    c = lily_metrics.LilyMetricsCollector()
    c.collect(_M(type="llm_metrics", ttft=0.2, tokens_per_second=40.0,
                 prompt_tokens=100, prompt_cached_tokens=30,
                 completion_tokens=50, total_tokens=150, cancelled=False))
    c.collect(_M(type="llm_metrics", ttft=0.4, tokens_per_second=60.0,
                 prompt_tokens=200, prompt_cached_tokens=0,
                 completion_tokens=80, total_tokens=280, cancelled=True))
    s = c.summary()
    assert s["llm"]["calls"] == 2
    assert s["llm"]["prompt_tokens"] == 300
    assert s["llm"]["prompt_cached_tokens"] == 30
    assert s["llm"]["completion_tokens"] == 130
    assert s["llm"]["total_tokens"] == 430
    assert s["llm"]["cancelled"] == 1
    assert s["llm"]["ttft_ms_p50"] is not None  # ttft captured in ms


def test_stt_and_tts_audio_and_characters():
    c = lily_metrics.LilyMetricsCollector()
    c.collect(_M(type="stt_metrics", audio_duration=3.5, connection_reused=True))
    c.collect(_M(type="stt_metrics", audio_duration=2.5, connection_reused=False))
    c.collect(_M(type="tts_metrics", ttfb=0.15, characters_count=120,
                 audio_duration=6.0, cancelled=False))
    s = c.summary()
    assert s["stt"]["calls"] == 2
    assert s["stt"]["audio_duration_s"] == 6.0
    assert s["stt"]["connection_reused"] == 1
    assert s["tts"]["characters"] == 120
    assert s["tts"]["audio_duration_s"] == 6.0
    assert s["tts"]["ttfb_ms_p50"] == 150.0


def test_eou_turn_taking_delays_are_quality_signals():
    c = lily_metrics.LilyMetricsCollector()
    for d in (0.5, 0.7, 0.9):
        c.collect(_M(type="eou_metrics", end_of_utterance_delay=d,
                     transcription_delay=d / 2,
                     on_user_turn_completed_delay=d + 0.1))
    s = c.summary()
    tt = s["turn_taking"]
    assert tt["end_of_utterance_delay_ms_p50"] is not None
    assert tt["transcription_delay_ms_p50"] is not None
    assert tt["on_user_turn_completed_delay_ms_p50"] is not None


def test_vad_metrics_accumulate():
    c = lily_metrics.LilyMetricsCollector()
    c.collect(_M(type="vad_metrics", idle_time=1.2,
                 inference_duration_total=0.05, inference_count=100))
    c.collect(_M(type="vad_metrics", idle_time=2.0,
                 inference_duration_total=0.06, inference_count=120))
    s = c.summary()
    assert s["vad"]["inference_count"] == 220
    assert s["vad"]["idle_time_s"] == 2.0  # latest snapshot


def test_unknown_and_none_metrics_never_raise():
    c = lily_metrics.LilyMetricsCollector()
    c.collect(None)
    c.collect(_M(type="realtime_model_metrics", foo=1))  # unknown -> counted only
    c.collect(_M())  # no type
    s = c.summary()
    assert s["metrics_events"] >= 1
    # No typed section materializes from unknown/none.
    assert "llm" not in s and "stt" not in s


def test_empty_summary_is_minimal():
    c = lily_metrics.LilyMetricsCollector()
    s = c.summary()
    assert s == {"metrics_events": 0}


def test_session_usage_rollup_recorded():
    c = lily_metrics.LilyMetricsCollector()
    c.collect_session_usage(_M(model_usage=[_M(), _M(), _M()]))
    s = c.summary()
    assert s["session_usage"]["model_usage_entries"] == 3


def test_collect_is_defensive_against_bad_fields():
    c = lily_metrics.LilyMetricsCollector()
    # Missing fields default to 0/None, never raise.
    c.collect(_M(type="llm_metrics"))
    c.collect(_M(type="tts_metrics"))
    s = c.summary()
    assert s["llm"]["calls"] == 1
    assert s["tts"]["calls"] == 1
