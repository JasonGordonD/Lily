"""SAFETY GATE regression tests (WO-LILY-DESYNC-HONESTY-001 Sub-agent A):
adult mode refuses while the acoustic breaker is open.

Live finding (2026-07-15 01:43:37): adult mode was entered while
LILY_AUDEERING_BREAKER was OPEN (missing AUDEERING_API_KEY at startup) —
the key-gated disable switched off the child-signal SENSOR without
switching off the FEATURE that depends on it. These tests pin the fix:

- lily_child_gate_ready() is THE single readiness flag (pipeline
  configured AND started AND breaker CLOSED) and lily_enter_adult_mode
  reads that flag only — no sensor = no adult mode, FAIL CLOSED.
- Consensus + retry still refuse while the gate is unavailable.
- With the gate ready, entry proceeds (and the child-signal veto still
  refuses independently — the sensor can EXIT or BLOCK adult mode, never
  authorize it).
- A mid-session breaker OPEN while adult mode is active exits adult mode
  through the SAME sticky-flag revert path as "back to normal"
  (LilyGame.on_child_gate_lost, wired as the acoustic state's
  on_breaker_open callback), and the next question serves general.

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


def _call_enter_adult(agent: LilyAgent) -> str:
    # FunctionTool exposes the raw coroutine via __wrapped__; fresh event
    # loop per call (test_award_gate pattern).
    coro = LilyAgent.lily_enter_adult_mode.__wrapped__(agent, None)
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
    client._ACTIVE_PIPELINE = None
    client._UPLOAD_DISABLED_REASON = None
    yield
    os.environ.pop("AUDEERING_API_KEY", None)
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
# Entry refused while the gate is unavailable (fail CLOSED)
# ---------------------------------------------------------------------------

def test_entry_refused_with_gate_unavailable_and_reason_logged(caplog):
    agent, game = _make_agent()
    with caplog.at_level(logging.WARNING, logger="lily_agent"):
        msg = _call_enter_adult(agent)
    assert "NOT available" in msg
    assert game.sk.mode == "general"          # feature stayed down
    assert game.publish_calls == 0            # no side effects
    # LLM-readable reason: honest in-character shape, no mechanism named,
    # no retry, consensus cannot override.
    assert "general deck" in msg
    assert "honestly" in msg
    assert "consensus cannot override" in msg
    # Refusal reason logged (LILY_<AREA> | key=value discipline).
    assert any(
        "LILY_ADULT_GATE | ADULT_MODE_DECLINED" in r.message
        and "reason=child_gate_unavailable" in r.message
        for r in caplog.records
    )


def test_entry_refused_when_key_missing_breaker_open():
    # The exact 01:43:37 condition: missing key -> breaker OPEN at startup.
    agent, game = _make_agent()
    state = consumers.LilyAcousticState()
    client._ACTIVE_PIPELINE = client.LilyAudeeringPipeline(state)
    msg = _call_enter_adult(agent)
    assert "NOT available" in msg
    assert game.sk.mode == "general"


def test_consensus_and_retry_still_refused_while_gate_unavailable():
    # The tool IS the post-consensus path — repeated calls model the table
    # re-consenting and Lily retrying. Every attempt refuses.
    agent, game = _make_agent()
    for _ in range(3):
        msg = _call_enter_adult(agent)
        assert "NOT available" in msg
        assert game.sk.mode == "general"
    assert game.publish_calls == 0


def test_refusal_names_no_mechanism_to_players():
    # The suggested spoken line must not surface the mechanism; the only
    # mention of system/audio/detection words is the DO-NOT instruction.
    agent, _ = _make_agent()
    msg = _call_enter_adult(agent)
    for word in ("breaker", "audeering", "acoustic", "api", "key", "child"):
        assert word not in msg.lower()


# ---------------------------------------------------------------------------
# Entry proceeds with the gate ready; the child veto is untouched
# ---------------------------------------------------------------------------

def test_entry_proceeds_when_gate_ready():
    agent, game = _make_agent()
    _ready_pipeline(consumers.LilyAcousticState())
    msg = _call_enter_adult(agent)
    assert "Adult mode is ON" in msg
    assert game.sk.mode == "adult"
    assert game.publish_calls == 1


def test_child_veto_still_refuses_even_with_gate_ready():
    # The gate never AUTHORIZES: with the sensor healthy but the ladder
    # tripped, the pre-existing veto refusal stands.
    agent, game = _make_agent()
    _ready_pipeline(consumers.LilyAcousticState())
    import lily_config
    n = lily_config.audeering_child_halt_sustained_n()
    game.acoustic.baseline.child_high_streak = n
    msg = _call_enter_adult(agent)
    assert "NOT available" in msg
    assert game.sk.mode == "general"
    assert game.publish_calls == 0


# ---------------------------------------------------------------------------
# Mid-session breaker trip during adult mode -> automatic sticky revert
# ---------------------------------------------------------------------------

def _enter_adult_then_wire_trip(agent, game):
    state = consumers.LilyAcousticState()
    pipeline = _ready_pipeline(state)
    assert "Adult mode is ON" in _call_enter_adult(agent)
    assert game.sk.mode == "adult"
    # Production wiring (entrypoint): breaker CLOSED->OPEN -> gate-lost.
    state.on_breaker_open = lambda reason: _gate_lost(game, reason)
    return state, pipeline


def test_mid_session_trip_exits_adult_mode_via_sticky_revert(caplog):
    agent, game = _make_agent()
    state, pipeline = _enter_adult_then_wire_trip(agent, game)
    with caplog.at_level(logging.WARNING, logger="lily_agent"):
        pipeline._open_breaker("session capture cap reached")
    # Sticky flag reverted in code — the same path "back to normal" uses.
    assert game.sk.mode == "general"
    assert game.publish_nowait_calls == 1
    # Deterministic revert speech: mode_revert act, general question next,
    # honest and mechanism-free.
    assert len(game.said) == 1
    revert = game.said[0]
    assert revert["act"] == "mode_revert"
    assert revert["source"] == "child_gate"
    # D seam: the auto-revert flushes the adult deck and the general deck
    # re-draws — the instruction is honest about the one-beat gap.
    assert "general deck is re-drawing" in revert["instructions"]
    # Both switches flushed: entry drew the adult deck, the trip re-draws
    # general.
    assert game.flush_calls == ["enter_adult", "child_gate"]
    for word in ("breaker", "audeering", "acoustic", "sensor"):
        assert word not in revert["instructions"].lower()
    # Exit logged with the breaker reason.
    assert any(
        "LILY_ADULT_GATE | ADULT_MODE_EXIT" in r.message
        and "reason=child_gate_lost" in r.message
        for r in caplog.records
    )
    # And the gate is closed for the session remainder: re-entry refused.
    assert "NOT available" in _call_enter_adult(agent)
    assert game.sk.mode == "general"


def test_next_question_serves_general_after_revert():
    agent, game = _make_agent()
    _, pipeline = _enter_adult_then_wire_trip(agent, game)
    pipeline._open_breaker("quota exhausted duration=0 uploads=0")
    assert game.sk.mode == "general"
    # The supply path passes mode=sk.mode into the bank; the mode filter
    # hard-excludes adult rows for a general-mode table.
    rows = [
        {"id": 1, "question": "capital of France", "adult": False},
        {"id": 2, "question": "adult-register question", "adult": True},
    ]
    served = lily_bank_mode_filter(rows, game.sk.mode)
    assert [r["id"] for r in served] == [1]


def test_mode_change_recorded_on_gate_loss_revert():
    # The revert flows through sk.set_mode, so the mode_changes audit trail
    # (the scorekeeper's sticky-flag ledger) records it like any revert.
    agent, game = _make_agent()
    _, pipeline = _enter_adult_then_wire_trip(agent, game)
    pipeline._open_breaker("invalid API credentials")
    assert game.sk.mode_changes[-1]["from"] == "adult"
    assert game.sk.mode_changes[-1]["to"] == "general"


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
