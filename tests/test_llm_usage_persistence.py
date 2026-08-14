"""WO-LILY-LLM-USAGE-PERSISTENCE — durable per-call LLM usage.

The 2026-08-14 lobby dead-air could not be confirmed from the database:
Lily emitted per-call LLMMetrics (ttft, tokens, empty-STOP) to logs only.
This pins the collector's usage sink (the single per-call choke point in
collect_llm_call), the empty-STOP finish-state flow off llm_node's guard,
and the fail-open persistence writer.
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_persistence
from lily_metrics import LilyMetricsCollector


def _call(prompt=1000, completion=50, ttft=1.2, duration=2.5,
          request_id="req_1", speech_id="speech_1", cancelled=False):
    return SimpleNamespace(
        prompt_tokens=prompt,
        prompt_cached_tokens=0,
        completion_tokens=completion,
        ttft=ttft,
        duration=duration,
        request_id=request_id,
        speech_id=speech_id,
        cancelled=cancelled,
    )


# -- the sink receives every per-call field, ms-converted ---------------------


def test_usage_sink_receives_call_fields():
    c = LilyMetricsCollector()
    seen = []
    c.set_usage_sink(seen.append)
    c.collect_llm_call(_call(prompt=1200, completion=40, ttft=1.0,
                             duration=3.0, speech_id="sp_A"))
    assert len(seen) == 1
    row = seen[0]
    assert row["utterance_id"] == "sp_A"
    assert row["prompt_tokens"] == 1200
    assert row["completion_tokens"] == 40
    assert row["ttft_ms"] == 1000.0   # seconds -> ms
    assert row["total_ms"] == 3000.0
    # A plain finish is not an empty-STOP.
    assert row["empty_stop"] is False
    assert row["finish_reason"] is None


# -- empty-STOP finish state stashed by the guard rides the matching call -----


def test_empty_stop_finish_state_flows_to_sink():
    c = LilyMetricsCollector()
    seen = []
    c.set_usage_sink(seen.append)
    # llm_node's guard stamps the verdict for this speech BEFORE the metrics
    # event folds (call_soon ordering in production; direct here).
    c.note_finish_state("sp_dead", "stop_empty", True)
    c.collect_llm_call(_call(prompt=800, completion=0, speech_id="sp_dead"))
    assert seen[0]["empty_stop"] is True
    assert seen[0]["finish_reason"] == "stop_empty"

    # The state is CONSUMED — a later call on the same speech is a plain
    # finish again (no stale empty-STOP bleed onto a recovered turn).
    c.collect_llm_call(_call(prompt=800, completion=20, speech_id="sp_dead"))
    assert seen[1]["empty_stop"] is False
    assert seen[1]["finish_reason"] is None


# -- fail-open: no sink, and a raising sink, never break the fold -------------


def test_no_sink_is_safe():
    c = LilyMetricsCollector()
    c.collect_llm_call(_call())  # sink is None; must not raise
    assert c.summary()["llm_cache"]["calls"] == 1


def test_sink_raise_is_fail_open():
    c = LilyMetricsCollector()

    def _boom(_row):
        raise RuntimeError("db down")

    c.set_usage_sink(_boom)
    c.collect_llm_call(_call())          # swallowed, no propagation
    assert c.summary()["llm_cache"]["calls"] == 1


def test_note_finish_state_defensive():
    c = LilyMetricsCollector()
    c.note_finish_state(None, "stop_empty", True)   # no speech id -> no-op
    c.set_usage_sink(lambda r: None)
    c.collect_llm_call(_call(speech_id="sp_x"))       # unrelated, no raise


# -- persistence writer is fail-open on a missing client / empty row ----------


def test_write_llm_usage_none_supabase_noop():
    # No client and empty row are both silent no-ops (never raise).
    assert asyncio.run(
        lily_persistence.lily_write_llm_usage(None, {"session_id": "s"})
    ) is None
    assert asyncio.run(
        lily_persistence.lily_write_llm_usage(object(), {})
    ) is None


def test_write_llm_usage_swallows_client_error():
    class _Boom:
        def table(self, *_a, **_k):
            raise RuntimeError("PGRST205: table not found")

    # A client that raises (e.g. migration 026 not applied) is swallowed.
    assert asyncio.run(
        lily_persistence.lily_write_llm_usage(
            _Boom(), {"session_id": "s", "utterance_id": "u"}
        )
    ) is None
