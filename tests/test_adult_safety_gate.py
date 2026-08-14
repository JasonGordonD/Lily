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
