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


def test_confirmed_entry_proceeds_without_acoustic_pipeline():
    agent, game = _make_agent()
    assert client.lily_child_gate_ready() is False
    msg = _call_enter_adult(agent, confirmed_all_18_plus=True)
    assert "Adult mode is ON" in msg
    assert "18+ confirmed" in msg
    assert game.sk.mode == "adult"
    assert game.publish_calls == 1


def test_missing_audeering_key_does_not_block_confirmed_entry():
    agent, game = _make_agent()
    state = consumers.LilyAcousticState()
    client._ACTIVE_PIPELINE = client.LilyAudeeringPipeline(state)
    assert client.lily_child_gate_ready() is False
    assert "Adult mode is ON" in _call_enter_adult(agent, True)
    assert game.sk.mode == "adult"


def test_active_young_voice_signal_blocks_normal_entry():
    agent, game = _make_agent()
    import lily_config
    n = lily_config.audeering_child_halt_sustained_n()
    game.acoustic.baseline.child_high_streak = n
    msg = _call_enter_adult(agent, True)
    assert "NOT available" in msg
    assert game.sk.mode == "general"
    assert game.publish_calls == 0


def test_spoken_architect_claim_does_not_override_configuration():
    agent, game = _make_agent()
    # Voice content cannot set LILY_ARCHITECT_MODE; without server config,
    # the same unconfirmed tool call remains blocked.
    msg = _call_enter_adult(agent, confirmed_all_18_plus=False)
    assert "NOT enabled yet" in msg
    assert game.sk.mode == "general"


def test_server_authenticated_architect_mode_overrides_entry_and_veto(caplog):
    os.environ["LILY_ARCHITECT_MODE"] = "1"
    agent, game = _make_agent()
    game.acoustic.baseline.child_high_streak = 999
    with caplog.at_level(logging.WARNING, logger="lily_agent"):
        msg = _call_enter_adult(agent, confirmed_all_18_plus=False)
    assert "Adult mode is ON" in msg
    assert "architect override" in msg
    assert game.sk.mode == "adult"
    assert any("ARCHITECT_OVERRIDE" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# Mid-session monitoring loss is observational, not authorization
# ---------------------------------------------------------------------------

def _enter_adult_then_wire_trip(agent, game):
    state = consumers.LilyAcousticState()
    pipeline = _ready_pipeline(state)
    assert "Adult mode is ON" in _call_enter_adult(agent)
    assert game.sk.mode == "adult"
    # Production wiring (entrypoint): breaker CLOSED->OPEN -> gate-lost.
    state.on_breaker_open = lambda reason: _gate_lost(game, reason)
    return state, pipeline


def test_mid_session_breaker_trip_does_not_exit_adult_mode(caplog):
    agent, game = _make_agent()
    _, pipeline = _enter_adult_then_wire_trip(agent, game)
    with caplog.at_level(logging.WARNING, logger="lily_agent"):
        pipeline._open_breaker("session capture cap reached")
    assert game.sk.mode == "adult"
    assert game.publish_nowait_calls == 0
    assert game.said == []
    assert game.flush_calls == ["enter_adult"]
    assert any(
        "LILY_ADULT_GATE | MONITORING_UNAVAILABLE" in record.message
        and "adult_mode_continues=true" in record.message
        for record in caplog.records
    )


def test_next_question_remains_adult_after_monitoring_loss():
    agent, game = _make_agent()
    _, pipeline = _enter_adult_then_wire_trip(agent, game)
    pipeline._open_breaker("quota exhausted duration=0 uploads=0")
    assert game.sk.mode == "adult"
    rows = [
        {"id": 1, "question": "capital of France", "adult": False},
        {"id": 2, "question": "adult-register question", "adult": True},
    ]
    served = lily_bank_mode_filter(rows, game.sk.mode)
    assert [r["id"] for r in served] == [1, 2]


def test_monitoring_loss_records_no_mode_change():
    agent, game = _make_agent()
    _, pipeline = _enter_adult_then_wire_trip(agent, game)
    changes_before = list(game.sk.mode_changes)
    pipeline._open_breaker("invalid API credentials")
    assert game.sk.mode_changes == changes_before


def test_actual_young_voice_signal_still_exits_adult_mode():
    agent, game = _make_agent()
    _call_enter_adult(agent, True)
    LilyGame.on_child_signal(game, {"tier": "high_halt"})
    assert game.sk.mode == "general"
    assert game.flush_calls == ["enter_adult", "child_signal"]
    assert game.said[-1]["source"] == "child_signal"


def test_architect_mode_ignores_post_entry_young_voice_signal():
    os.environ["LILY_ARCHITECT_MODE"] = "1"
    agent, game = _make_agent()
    _call_enter_adult(agent, False)
    LilyGame.on_child_signal(game, {"tier": "high_halt"})
    assert game.sk.mode == "adult"
    assert game.flush_calls == ["enter_adult"]
    assert game.said == []


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
