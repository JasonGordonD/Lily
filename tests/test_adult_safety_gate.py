"""Adult entry and optional young-voice veto policy regression tests.

- Explicit verbal confirmation that every player is 18+ authorizes entry.
- audEERING readiness never authorizes, blocks, or revokes adult mode.
- An actual young-voice signal remains a post-entry veto.
- Server-authenticated architect mode overrides confirmation and vetoes;
  merely saying "I'm the architect" cannot activate it.

This file imports lily_agent (and therefore livekit) — same boundary note
as test_award_gate.py.
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import lily_audeering_client as client
import lily_audeering_consumers as consumers
from lily_agent import LilyAgent, LilyGame
from lily_memory import lily_bank_mode_filter
from lily_scorekeeper import LilyScorekeeper


# ---------------------------------------------------------------------------
# Harness (test_award_gate pattern: sidestep the heavy livekit __init__s and
# wire only the surfaces the code under test touches)
# ---------------------------------------------------------------------------

class _FakeGame:
    """Minimum surface lily_enter_adult_mode / on_child_gate_lost touch:
    sk, acoustic, session, publish_attributes(_nowait), gated_say, and the
    mode-switch flush seam (WO-LILY-DESYNC-HONESTY-001 D: every mode
    switch — entry, spoken revert, veto, breaker trip — flushes the
    armed/prefetched questions and re-draws from the new deck; recorded
    here, exercised for real in test_adult_identity.py)."""

    def __init__(self) -> None:
        self.sk = LilyScorekeeper("test-room")
        self.acoustic = consumers.LilyAcousticState()
        self.session = object()  # non-None: revert instructions dispatch
        self.publish_calls = 0
        self.publish_nowait_calls = 0
        self.said: list[dict] = []
        self.flush_calls: list[str] = []
        # HOTFIX-004 Defect 1: these tests exercise the gate's OTHER
        # conditions (sensor readiness, child veto, persistence). A real
        # spoken 18+ consent is assumed heard — the deterministic floor
        # itself is covered in test_hotfix004.py.
        self._age_consent_confirmed = True

    def flush_for_mode_switch(self, source: str) -> None:
        self.flush_calls.append(source)

    async def publish_attributes(self) -> None:
        self.publish_calls += 1

    def publish_attributes_nowait(self) -> None:
        self.publish_nowait_calls += 1

    def gated_say(self, key, act, instructions, source, extra_keys=()) -> bool:
        self.said.append({
            "key": key, "act": act,
            "instructions": instructions, "source": source,
        })
        return True


def _make_agent() -> tuple[LilyAgent, _FakeGame]:
    agent = LilyAgent.__new__(LilyAgent)  # sidestep livekit Agent base
    game = _FakeGame()
    # Adult entry requires a persisted consent audit trail (degraded
    # no-persistence sessions refuse) — give the fake a live client.
    game.supabase = object()
    agent._game = game
    return agent, game


def _call_enter_adult(
    agent: LilyAgent,
    confirmed_all_18_plus: bool = True,
) -> str:
    # FunctionTool exposes the raw coroutine via __wrapped__; fresh event
    # loop per call (test_award_gate pattern).
    coro = LilyAgent.lily_enter_adult_mode.__wrapped__(
        agent, None, confirmed_all_18_plus
    )
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _gate_lost(game: _FakeGame, reason: str) -> None:
    # on_child_gate_lost is a plain LilyGame method over exactly the
    # surface _FakeGame provides — invoke it unbound, as the production
    # on_breaker_open callback does against the real game.
    LilyGame.on_child_gate_lost(game, reason)


def _ready_pipeline(state: consumers.LilyAcousticState) -> client.LilyAudeeringPipeline:
    """A configured pipeline with breaker CLOSED, registered as the active
    child-gate source. started is forced (start() would hit the network)."""
    os.environ["AUDEERING_API_KEY"] = "test-key-1234"
    pipeline = client.LilyAudeeringPipeline(state)
    pipeline._started = True
    client._ACTIVE_PIPELINE = pipeline
    return pipeline


@pytest.fixture(autouse=True)
def _clean_gate_state():
    """Module-global + env hygiene around every test."""
    os.environ.pop("AUDEERING_API_KEY", None)
    os.environ.pop("LILY_ARCHITECT_MODE", None)
    client._ACTIVE_PIPELINE = None
    client._UPLOAD_DISABLED_REASON = None
    yield
    os.environ.pop("AUDEERING_API_KEY", None)
    os.environ.pop("LILY_ARCHITECT_MODE", None)
    client._ACTIVE_PIPELINE = None
    client._UPLOAD_DISABLED_REASON = None


# ---------------------------------------------------------------------------
# The readiness flag itself
# ---------------------------------------------------------------------------

def test_gate_not_ready_without_pipeline():
    # No pipeline at all (e.g. entrypoint never ran) -> fail CLOSED.
    assert client.lily_child_gate_ready() is False


def test_gate_not_ready_when_breaker_open_from_startup():
    # Missing AUDEERING_API_KEY opens the breaker at construction — the
    # exact live-session condition. Registered but never ready.
    state = consumers.LilyAcousticState()
    pipeline = client.LilyAudeeringPipeline(state)
    client._ACTIVE_PIPELINE = pipeline
    assert pipeline.breaker_open is True
    assert client.lily_child_gate_ready() is False


def test_gate_ready_when_configured_and_breaker_closed():
    state = consumers.LilyAcousticState()
    _ready_pipeline(state)
    assert client.lily_child_gate_ready() is True


def test_gate_drops_to_not_ready_on_mid_session_trip():
    state = consumers.LilyAcousticState()
    pipeline = _ready_pipeline(state)
    assert client.lily_child_gate_ready() is True
    pipeline._open_breaker("session capture cap reached")
    assert client.lily_child_gate_ready() is False


def test_start_helper_registers_even_a_breaker_open_pipeline():
    # lily_start_audeering_pipeline returns None on a missing key but must
    # still register the pipeline so the gate tracks the REAL breaker.
    state = consumers.LilyAcousticState()
    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(
            client.lily_start_audeering_pipeline(state)
        )
    finally:
        loop.close()
    assert result is None
    assert client._ACTIVE_PIPELINE is not None
    assert client.lily_child_gate_ready() is False


# ---------------------------------------------------------------------------
# Entry policy: explicit 18+ confirmation, independent of audEERING
# ---------------------------------------------------------------------------

def test_entry_requires_explicit_18_plus_confirmation(caplog):
    agent, game = _make_agent()
    _ready_pipeline(consumers.LilyAcousticState())  # gate READY — the age
    # ceremony is what refuses here, not the sensor
    with caplog.at_level(logging.WARNING, logger="lily_agent"):
        msg = _call_enter_adult(agent, confirmed_all_18_plus=False)
    assert "NOT enabled yet" in msg
    assert "18 or older" in msg
    assert "confirmed_all_18_plus=true" in msg
    assert game.sk.mode == "general"
    assert game.publish_calls == 0
    assert any(
        "reason=age_confirmation_required" in record.message
        for record in caplog.records
    )


def test_entry_refused_without_acoustic_pipeline_even_when_confirmed(
    caplog, monkeypatch
):
    # LEGACY "sensor" MODE (opt-in since the 2026-08-06 owner directive
    # opened the deck by default): sensor and deck deploy as one unit.
    # No pipeline -> no adult mode, 18+ consensus notwithstanding.
    monkeypatch.setenv("LILY_ADULT_DECK", "sensor")
    agent, game = _make_agent()
    assert client.lily_child_gate_ready() is False
    with caplog.at_level(logging.WARNING, logger="lily_agent"):
        msg = _call_enter_adult(agent, confirmed_all_18_plus=True)
    assert "NOT available" in msg
    assert game.sk.mode == "general"
    assert game.publish_calls == 0
    assert any(
        "reason=child_gate_unavailable" in r.message for r in caplog.records
    )


def test_missing_audeering_key_blocks_confirmed_entry(monkeypatch):
    # The exact live condition: breaker OPEN from a missing key —
    # blocking only in legacy sensor mode.
    monkeypatch.setenv("LILY_ADULT_DECK", "sensor")
    agent, game = _make_agent()
    state = consumers.LilyAcousticState()
    client._ACTIVE_PIPELINE = client.LilyAudeeringPipeline(state)
    assert client.lily_child_gate_ready() is False
    assert "NOT available" in _call_enter_adult(agent, True)
    assert game.sk.mode == "general"


def test_confirmed_entry_proceeds_when_gate_ready():
    agent, game = _make_agent()
    _ready_pipeline(consumers.LilyAcousticState())
    msg = _call_enter_adult(agent, confirmed_all_18_plus=True)
    assert "Adult mode is ON" in msg
    assert "18+ confirmed" in msg
    assert game.sk.mode == "adult"
    assert game.publish_calls == 1


def test_degraded_no_persistence_session_refuses_adult_mode():
    # A memoryless session has no persisted consent audit trail.
    agent, game = _make_agent()
    _ready_pipeline(consumers.LilyAcousticState())
    game.supabase = None
    assert "NOT available" in _call_enter_adult(agent, True)
    assert game.sk.mode == "general"


def test_active_young_voice_signal_blocks_normal_entry():
    agent, game = _make_agent()
    _ready_pipeline(consumers.LilyAcousticState())
    import lily_config
    n = lily_config.audeering_child_halt_sustained_n()
    game.acoustic.baseline.child_high_streak = n
    msg = _call_enter_adult(agent, True)
    assert "NOT available" in msg
    assert game.sk.mode == "general"
    assert game.publish_calls == 0


def test_spoken_architect_claim_does_not_override_configuration():
    agent, game = _make_agent()
    _ready_pipeline(consumers.LilyAcousticState())
    # Voice content cannot set LILY_ARCHITECT_MODE; without server config,
    # the same unconfirmed tool call remains blocked.
    msg = _call_enter_adult(agent, confirmed_all_18_plus=False)
    assert "NOT enabled yet" in msg
    assert game.sk.mode == "general"


def test_architect_mode_substitutes_age_ceremony_only(caplog):
    # With the gate READY and NO active signal, server-authenticated
    # architect mode stands in for the spoken 18+ ceremony (controlled
    # testing) — that is the WHOLE of its power.
    os.environ["LILY_ARCHITECT_MODE"] = "1"
    agent, game = _make_agent()
    _ready_pipeline(consumers.LilyAcousticState())
    with caplog.at_level(logging.WARNING, logger="lily_agent"):
        msg = _call_enter_adult(agent, confirmed_all_18_plus=False)
    assert "Adult mode is ON" in msg
    assert "architect override" in msg
    assert game.sk.mode == "adult"
    assert any("ARCHITECT_OVERRIDE" in r.message for r in caplog.records)


def test_architect_mode_never_overrides_active_child_signal():
    # The veto is absolute: an ACTIVE young-voice signal blocks entry for
    # everyone, architect mode included.
    os.environ["LILY_ARCHITECT_MODE"] = "1"
    agent, game = _make_agent()
    _ready_pipeline(consumers.LilyAcousticState())
    import lily_config
    game.acoustic.baseline.child_high_streak = (
        lily_config.audeering_child_halt_sustained_n()
    )
    msg = _call_enter_adult(agent, confirmed_all_18_plus=False)
    assert "NOT available" in msg
    assert game.sk.mode == "general"


def test_architect_mode_never_overrides_dead_sensor(monkeypatch):
    # Sensor mode: sensor down means deck down — for the architect too.
    monkeypatch.setenv("LILY_ADULT_DECK", "sensor")
    os.environ["LILY_ARCHITECT_MODE"] = "1"
    agent, game = _make_agent()
    assert client.lily_child_gate_ready() is False
    msg = _call_enter_adult(agent, confirmed_all_18_plus=False)
    assert "NOT available" in msg
    assert game.sk.mode == "general"


# ---------------------------------------------------------------------------
# Mid-session sensor loss exits adult mode (fail closed, mid-session too)
# ---------------------------------------------------------------------------

def _enter_adult_then_wire_trip(agent, game):
    state = consumers.LilyAcousticState()
    pipeline = _ready_pipeline(state)
    assert "Adult mode is ON" in _call_enter_adult(agent)
    assert game.sk.mode == "adult"
    # Production wiring (entrypoint): breaker CLOSED->OPEN -> gate-lost.
    state.on_breaker_open = lambda reason: _gate_lost(game, reason)
    return state, pipeline


def test_mid_session_breaker_trip_exits_adult_mode(caplog):
    agent, game = _make_agent()
    _, pipeline = _enter_adult_then_wire_trip(agent, game)
    with caplog.at_level(logging.WARNING, logger="lily_agent"):
        pipeline._open_breaker("session capture cap reached")
    assert game.sk.mode == "general"
    assert game.flush_calls == ["enter_adult", "child_gate"]
    assert game.said and game.said[-1]["source"] == "child_gate"
    assert any(
        "CHILD_GATE_LOST" in record.message
        and "action=adult_mode_exit" in record.message
        for record in caplog.records
    )


def test_next_question_is_general_after_sensor_loss():
    agent, game = _make_agent()
    _, pipeline = _enter_adult_then_wire_trip(agent, game)
    pipeline._open_breaker("quota exhausted duration=0 uploads=0")
    assert game.sk.mode == "general"
    rows = [
        {"id": 1, "question": "capital of France", "adult": False},
        {"id": 2, "question": "adult-register question", "adult": True},
    ]
    served = lily_bank_mode_filter(rows, game.sk.mode)
    assert [r["id"] for r in served] == [1]


def test_sensor_loss_records_the_mode_change():
    agent, game = _make_agent()
    _, pipeline = _enter_adult_then_wire_trip(agent, game)
    changes_before = len(game.sk.mode_changes)
    pipeline._open_breaker("invalid API credentials")
    assert len(game.sk.mode_changes) == changes_before + 1


def test_actual_young_voice_signal_still_exits_adult_mode():
    agent, game = _make_agent()
    _ready_pipeline(consumers.LilyAcousticState())
    _call_enter_adult(agent, True)
    LilyGame.on_child_signal(game, {"tier": "high_halt"})
    assert game.sk.mode == "general"
    assert game.flush_calls == ["enter_adult", "child_signal"]
    assert game.said[-1]["source"] == "child_signal"


def test_architect_mode_never_ignores_post_entry_young_voice_signal():
    # The invariant survives entry: a signal DURING adult mode exits it,
    # architect mode or not.
    os.environ["LILY_ARCHITECT_MODE"] = "1"
    agent, game = _make_agent()
    _ready_pipeline(consumers.LilyAcousticState())
    _call_enter_adult(agent, False)
    assert game.sk.mode == "adult"
    LilyGame.on_child_signal(game, {"tier": "high_halt"})
    assert game.sk.mode == "general"
    assert game.flush_calls == ["enter_adult", "child_signal"]
    assert game.said and game.said[-1]["source"] == "child_signal"


def test_breaker_trip_outside_adult_mode_is_silent_noop():
    # Startup breaker-open (missing key, mode=general) must not dispatch
    # any speech or flip anything — entry is refused by the tool gate.
    _, game = _make_agent()
    game.acoustic.on_breaker_open = lambda reason: _gate_lost(game, reason)
    game.acoustic.set_breaker_open(True, reason="missing AUDEERING_API_KEY at startup")
    assert game.sk.mode == "general"
    assert game.said == []
    assert game.publish_nowait_calls == 0


# ---------------------------------------------------------------------------
# State-hook plumbing (transition-edge semantics, exception guard)
# ---------------------------------------------------------------------------

def test_on_breaker_open_fires_once_per_transition():
    state = consumers.LilyAcousticState()
    fired: list[str] = []
    state.on_breaker_open = fired.append
    state.set_breaker_open(True, reason="first")
    state.set_breaker_open(True, reason="repeat")   # already OPEN: no edge
    assert fired == ["first"]
    state.set_breaker_open(False)
    state.set_breaker_open(True, reason="second")
    assert fired == ["first", "second"]


def test_on_breaker_open_exception_never_breaks_the_pipeline():
    state = consumers.LilyAcousticState()

    def _boom(reason: str) -> None:
        raise RuntimeError("gate hook crashed")

    state.on_breaker_open = _boom
    state.set_breaker_open(True, reason="whatever")  # must not raise
    assert state.breaker_open is True
