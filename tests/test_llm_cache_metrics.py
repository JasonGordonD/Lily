"""Y1c (WO-LILY-HOTFIX-007) — per-call LLM cache metrics, pinned.

Y1's verify clause requires "cache hit rate logged per call" so the Y1a
static-prefix restructure can be MEASURED, not asserted. The per-turn
MetricsReport (blessed U3(b) surface) carries no token counts; only the
LLM component's per-call LLMMetrics has prompt_cached_tokens. The
component-level `metrics_collected` event is first-class at 1.6.8 — the
deprecation the U3(b) audit flagged is the AgentSession-level
subscription, which stays avoided (pinned below).
"""

import inspect
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_agent
from lily_metrics import LilyMetricsCollector


def _call(prompt, cached, completion=50, ttft=1.2, request_id="req_1"):
    return SimpleNamespace(
        prompt_tokens=prompt,
        prompt_cached_tokens=cached,
        completion_tokens=completion,
        ttft=ttft,
        request_id=request_id,
        speech_id="speech_1",
        cancelled=False,
    )


def test_cache_hit_rate_and_totals():
    c = LilyMetricsCollector()
    c.collect_llm_call(_call(10000, 8000, ttft=1.0))
    c.collect_llm_call(_call(10000, 0, ttft=3.0))
    cache = c.summary()["llm_cache"]
    assert cache["calls"] == 2
    assert cache["prompt_tokens"] == 20000
    assert cache["cached_tokens"] == 8000
    assert cache["cache_hit_rate"] == 0.4
    assert cache["calls_with_cache_hit"] == 1
    assert cache["ttft_ms_p50"] == 1000.0
    assert cache["ttft_ms_p95"] == 3000.0


def test_per_call_log_line_carries_the_numbers(caplog):
    c = LilyMetricsCollector()
    with caplog.at_level(logging.INFO, logger="lily_metrics"):
        c.collect_llm_call(_call(9000, 8100, ttft=0.9, request_id="req_x"))
    line = next(r.message for r in caplog.records if "LLM_CALL" in r.message)
    assert "request=req_x" in line
    assert "prompt=9000" in line
    assert "cached=8100" in line
    assert "hit=90.0%" in line
    assert "ttft_ms=900.0" in line


def test_round_trips_per_turn_grouped_by_speech_id():
    """Y4 measurement gate: >1 call on one speech_id is a serialized
    round-trip (tool follow-up / regen) inside a single spoken turn."""
    c = LilyMetricsCollector()
    for sid, n in (("speech_a", 1), ("speech_b", 3), ("speech_c", 1)):
        for _ in range(n):
            m = _call(1000, 0)
            m.speech_id = sid
            c.collect_llm_call(m)
    cache = c.summary()["llm_cache"]
    assert cache["calls"] == 5
    assert cache["calls_per_turn_p50"] == 1
    assert cache["calls_per_turn_max"] == 3
    assert cache["turns_with_multiple_calls"] == 1


def test_no_calls_means_no_llm_cache_section():
    assert "llm_cache" not in LilyMetricsCollector().summary()


def test_garbage_never_raises_and_never_counts():
    """Wave-1 review finding: an all-empty payload used to fold as a
    zero-token call, inflating the denominator. Now it is not a call."""
    c = LilyMetricsCollector()
    c.collect_llm_call(None)
    c.collect_llm_call(object())
    c.collect_llm_call({"prompt_tokens": "not_a_number"})
    assert "llm_cache" not in c.summary()


def test_deferred_fold_sees_the_late_speech_id_stamp():
    """The HIGH wave-1 review finding, reproduced: LLMMetrics.speech_id is
    None at emit time and stamped IN PLACE by a sibling subscriber whose
    ordering vs ours is a set-iteration coin flip. collect_llm_call_soon
    defers one loop tick, so the stamp always lands first."""
    import asyncio

    async def scenario():
        c = LilyMetricsCollector()
        m = _call(1000, 0)
        m.speech_id = None          # what our handler may see at emit time
        c.collect_llm_call_soon(m)  # defers via call_soon
        m.speech_id = "speech_late"  # the framework's stamp, same emit pass
        await asyncio.sleep(0)      # run the deferred fold
        return c

    c = asyncio.run(scenario())
    cache = c.summary()["llm_cache"]
    assert cache["calls"] == 1
    assert cache["calls_per_turn_max"] == 1  # grouped under speech_late
    assert "speech_late" in c._llm_calls_by_speech


def test_collect_soon_falls_back_immediately_without_a_loop():
    c = LilyMetricsCollector()
    c.collect_llm_call_soon(_call(500, 100))
    assert c.summary()["llm_cache"]["calls"] == 1


def test_cancelled_calls_are_counted():
    """Interrupt-path waste: preemptive discards outside the equivalence
    warning (8 silent cancel sites in the framework) surface here as
    cancelled calls — the complement to the Y2 invalidated counter."""
    c = LilyMetricsCollector()
    m = _call(1000, 0)
    m.cancelled = True
    c.collect_llm_call(m)
    c.collect_llm_call(_call(1000, 500))
    cache = c.summary()["llm_cache"]
    assert cache["calls"] == 2
    assert cache["cancelled_calls"] == 1


def test_wiring_is_component_level_on_both_transports():
    """Source pins: the general node is wired at session build, the adult
    node at swap-in, and the DEPRECATED session-level subscription is still
    never used."""
    src = inspect.getsource(lily_agent)
    assert src.count('_wire_llm_metrics(general_vocal_llm)') == 1
    # Adult swap-in re-uses the same wire via the game handle.
    assert '_llm_metrics_wire' in src
    enter_src = inspect.getsource(lily_agent.LilyGame.enter_adult_vocal)
    assert '_llm_metrics_wire' in enter_src
    # U3(b) stays honored: no AgentSession-level metrics subscription.
    assert 'session.on("metrics_collected"' not in src
    assert "session.on('metrics_collected'" not in src
