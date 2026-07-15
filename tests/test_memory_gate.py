"""Memory at the door (WO-LILY-DESYNC-HONESTY-001 Sub-agent F).

Two gates around persistent memory:

  (a) The greeting awaits group resolution + memory load with a short
      budget (LILY_GREETING_MEMORY_BUDGET_SECONDS, default 1.5s) — the
      live failure was [RETURNING TABLE] landing one turn AFTER the
      greeting fired, cold-greeting a four-time returning table. Memory
      ready in time -> recognition rides the FIRST utterance; timeout ->
      cold greet exactly as before, the room is never blocked beyond the
      budget.

  (b) lily_memories narrative rows require >= LILY_MEMORY_MIN_QUESTIONS
      (default 3) questions OR round 2 reached — an aborted one-question
      session ("No sole winner over 1 question(s). Final scores: Rami 0")
      must never become 'last game' material. The session row still
      writes through its own path; only the narrative is gated.

This file imports lily_agent (and therefore livekit) — same boundary note
as test_award_gate.py.
"""

import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_say_gate
from lily_agent import LilyAgent, LilyGame
from lily_memory import lily_write_session_memory
from lily_scorekeeper import LilyScorekeeper


class _FakeSession:
    def __init__(self) -> None:
        self.instructions: list[str] = []

    def generate_reply(self, instructions: str) -> None:
        self.instructions.append(instructions)


class _FakeAgentHandle:
    def set_preemptive_generation(self, enabled: bool) -> None:
        pass


def _make_game() -> LilyGame:
    """Minimal LilyGame via __new__ — the attributes the greeting path
    touches (same pattern as test_say_gate_dispatch)."""
    game = LilyGame.__new__(LilyGame)
    game.session = _FakeSession()
    game.agent = _FakeAgentHandle()
    game._preemptive_paused = False
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game.sk = LilyScorekeeper("test-room")
    game.memory_block = ""
    game.memory_settled = asyncio.Event()
    game.memory_total_games = 0
    game._memory_disclosure_offered = False
    game.reconnected = False
    game.game_started = False
    game.game_over = False
    game.prefs = {}
    game._prefs_offer_made = False
    return game


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _with_env(name: str, value: str):
    """Tiny env-override context (lily_config reads lazily per call)."""
    class _Ctx:
        def __enter__(self):
            self._old = os.environ.get(name)
            os.environ[name] = value

        def __exit__(self, *a):
            if self._old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = self._old
    return _Ctx()


# ---------------------------------------------------------------------------
# (a) Greeting budget
# ---------------------------------------------------------------------------

def test_greeting_awaits_nothing_when_memory_already_settled():
    game = _make_game()
    game.memory_settled.set()
    started = time.monotonic()
    _run(game.await_greeting_memory())
    assert time.monotonic() - started < 0.2


def test_greeting_wait_picks_up_late_memory_within_budget():
    # THE live race, replayed: memory resolves shortly AFTER the greeting
    # dispatch reaches the gate but WITHIN the budget — the first
    # utterance must carry the recognition, not the cold question.
    game = _make_game()
    agent = LilyAgent.__new__(LilyAgent)
    agent._game = game

    async def _settle_late():
        await asyncio.sleep(0.05)
        game.memory_block = (
            "[RETURNING TABLE]\nThis table has played with you 4 time(s) "
            "before — rematch energy.\nReturning players: Rami."
        )
        game.memory_settled.set()

    async def _scenario():
        settle = asyncio.ensure_future(_settle_late())
        await agent.on_enter()
        await settle

    with _with_env("LILY_GREETING_MEMORY_BUDGET_SECONDS", "1.0"):
        _run(_scenario())

    assert len(game.session.instructions) == 1
    first = game.session.instructions[0]
    # Recognition composed from memory, not the cold first-time question:
    assert "memory KNOWS this table" in first
    assert "memory gives no answer" not in first
    assert game.say_registry.state("session_greet") is not None


def test_greeting_times_out_cold_and_never_blocks_beyond_budget():
    game = _make_game()  # memory never settles
    agent = LilyAgent.__new__(LilyAgent)
    agent._game = game

    started = time.monotonic()
    with _with_env("LILY_GREETING_MEMORY_BUDGET_SECONDS", "0.05"):
        _run(agent.on_enter())
    elapsed = time.monotonic() - started

    assert elapsed < 1.0  # budget 0.05s, generous CI margin — never 1.5s+
    assert len(game.session.instructions) == 1
    # Cold greet: the neutral-history part, exactly as before the gate.
    assert "memory gives no answer" in game.session.instructions[0]


def test_zero_budget_disables_the_wait():
    game = _make_game()
    started = time.monotonic()
    with _with_env("LILY_GREETING_MEMORY_BUDGET_SECONDS", "0"):
        _run(game.await_greeting_memory())
    assert time.monotonic() - started < 0.2


def test_rejoin_path_never_waits_on_memory():
    # A reconnect is mid-game — recognition is irrelevant and the rejoin
    # line must go out immediately even with memory unsettled.
    game = _make_game()
    game.reconnected = True
    agent = LilyAgent.__new__(LilyAgent)
    agent._game = game
    started = time.monotonic()
    with _with_env("LILY_GREETING_MEMORY_BUDGET_SECONDS", "5.0"):
        _run(agent.on_enter())
    assert time.monotonic() - started < 0.5
    assert game.say_registry.state("session_rejoin") is not None


def test_game_via_new_without_event_attribute_skips_wait():
    # Defensive path: harness-built games without the __init__ attribute
    # set must not crash or wait.
    game = LilyGame.__new__(LilyGame)
    _run(LilyGame.await_greeting_memory(game))


# ---------------------------------------------------------------------------
# (b) Memory write threshold
# ---------------------------------------------------------------------------

class StubTable:
    def __init__(self, sink):
        self._sink = sink
        self._payload = None

    def upsert(self, payload, on_conflict=None):
        self._payload = (payload, on_conflict)
        return self

    def execute(self):
        self._sink.append(self._payload)
        return self


class StubSupabase:
    def __init__(self):
        self.rows = []

    def table(self, name):
        assert name == "lily_memories"
        return StubTable(self.rows)


STANDINGS = [{"name": "Rami", "score": 0, "streak": 0}]


def test_one_question_session_writes_no_narrative():
    # The live junk row: "No sole winner over 1 question(s). Final
    # scores: Rami 0" — below threshold, nothing writes.
    stub = StubSupabase()
    _run(lily_write_session_memory(
        stub, "grp_x", "room-1", STANDINGS, 1, highlights=[], round_reached=1,
    ))
    assert stub.rows == []


def test_three_question_session_writes_narrative():
    stub = StubSupabase()
    _run(lily_write_session_memory(
        stub, "grp_x", "room-1", STANDINGS, 3, highlights=[], round_reached=1,
    ))
    assert len(stub.rows) == 1
    payload, on_conflict = stub.rows[0]
    assert on_conflict == "session_id"
    assert payload["question_count"] == 3


def test_round_two_writes_even_below_question_threshold():
    # "OR reached round 2": the round arm of the threshold stands alone.
    stub = StubSupabase()
    _run(lily_write_session_memory(
        stub, "grp_x", "room-1", STANDINGS, 2, highlights=[], round_reached=2,
    ))
    assert len(stub.rows) == 1


def test_threshold_is_env_tunable():
    stub = StubSupabase()
    with _with_env("LILY_MEMORY_MIN_QUESTIONS", "5"):
        _run(lily_write_session_memory(
            stub, "grp_x", "room-1", STANDINGS, 4, highlights=[],
            round_reached=1,
        ))
        assert stub.rows == []
        _run(lily_write_session_memory(
            stub, "grp_x", "room-2", STANDINGS, 5, highlights=[],
            round_reached=1,
        ))
    assert len(stub.rows) == 1


def test_legacy_call_without_round_still_gates():
    # Callers that never learned the kwarg (round defaults to 0) gate on
    # the question count alone.
    stub = StubSupabase()
    _run(lily_write_session_memory(
        stub, "grp_x", "room-1", STANDINGS, 1, highlights=[],
    ))
    assert stub.rows == []
    _run(lily_write_session_memory(
        stub, "grp_x", "room-2", STANDINGS, 12, highlights=[],
    ))
    assert len(stub.rows) == 1
