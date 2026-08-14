"""WO-LILY-NEVER-SILENT-001 — the anti-silence floor.

Lily is a party host; a party host never goes silent when spoken to. Every
suppression/guard outcome on a host-directed final must degrade to a short
in-character line, never to silence (live lily-EFC239: "Hello?" then "Lily.",
then 48s of dead air to session close).

The floor is ONE mechanism wired at three silence sites:
  * the empty-STOP guard yields the line as its generation content;
  * tts_node schedules it when the say pipeline suppressed a turn to silence;
  * the address-unanswered watchdog fires it past the responsiveness budget.
All three route through floor_line_owed (the verify-nothing-aired check) and
the one-line-per-address latch, and the line set is a deterministic sheet.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_config
import lily_say_gate
from lily_agent import LilyAgent, LilyGame, _lily_schedule_floor_if_owed


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 1. The line set — a deterministic, rotating sheet (no LLM on this path)
# ---------------------------------------------------------------------------

def test_floor_line_rotates_within_a_context():
    lobby = [lily_say_gate.lily_floor_line("lobby", n) for n in range(3)]
    assert len(set(lobby)) == 3  # three distinct lines, no verbatim recurrence
    # Rotation wraps deterministically.
    assert lily_say_gate.lily_floor_line("lobby", 3) == lobby[0]


def test_floor_line_contexts_are_short_and_populated():
    for context in ("lobby", "game"):
        for n in range(len(lily_say_gate.LILY_FLOOR_LINES[context])):
            line = lily_say_gate.lily_floor_line(context, n)
            assert line and len(line) <= 90


def test_unknown_context_answers_presence_never_silence():
    # An unknown context must still produce a lobby-class line, never "".
    assert lily_say_gate.lily_floor_line("nonsense", 0) in (
        lily_say_gate.LILY_FLOOR_LINES["lobby"]
    )


# ---------------------------------------------------------------------------
# 2. floor_line_owed — the verify-nothing-aired / scoping surface
# ---------------------------------------------------------------------------

def _game(**overrides) -> LilyGame:
    game = LilyGame.bare()
    game.sk = SimpleNamespace(
        session_id="floor", host_speaking=False, answer_window_open=False,
        question_number=1,
    )
    game.game_started = False
    game.game_over = False
    game.armed_question = None
    for k, v in overrides.items():
        setattr(game, k, v)
    return game


def test_hail_in_lobby_is_owed_a_floor_line():
    game = _game()
    game._awaiting_address_since = time.time()  # a host-directed final
    assert game.floor_line_owed() is True
    line = game.floor_line_if_owed("empty_stop")
    assert line in lily_say_gate.LILY_FLOOR_LINES["lobby"]


def test_mid_game_uses_the_game_context_line():
    game = _game(game_started=True)
    game._awaiting_address_since = time.time()
    line = game.floor_line_if_owed("say_suppressed")
    assert line in lily_say_gate.LILY_FLOOR_LINES["game"]


def test_no_outstanding_address_is_never_owed():
    game = _game()
    game._awaiting_address_since = 0.0
    assert game.floor_line_owed() is False
    assert game.floor_line_if_owed("empty_stop") is None


def test_stop_hold_and_question_pending_stand_the_floor_down():
    for latch in ("_delivery_stop_sticky", "_hold_active", "_question_pending"):
        game = _game()
        game._awaiting_address_since = time.time()
        setattr(game, latch, True)
        assert game.floor_line_owed() is False, latch


def test_her_own_live_audio_stands_the_floor_down():
    game = _game()
    game._awaiting_address_since = time.time()
    game.sk.host_speaking = True
    assert game.floor_line_owed() is False


def test_one_line_per_address_then_a_new_address_reopens_it():
    game = _game()
    ts = time.time()
    game._awaiting_address_since = ts
    assert game.floor_line_if_owed("empty_stop") is not None
    # Same outstanding address: already floored, no second line (no storm).
    assert game.floor_line_owed() is False
    assert game.floor_line_if_owed("empty_stop") is None
    # A NEW host-directed final mints a new latch — the floor may speak again.
    game._awaiting_address_since = ts + 1.0
    assert game.floor_line_if_owed("empty_stop") is not None


def test_real_playout_clears_the_latch_so_the_floor_never_double_speaks():
    # note_playout_started is the surface that clears _awaiting_address_since;
    # once a real response aired, the floor is not owed (no double-speak).
    game = _game()
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game._playout_started_ids = set()
    game._awaiting_address_since = time.time()
    game.note_playout_started("speech-real")
    assert game._awaiting_address_since == 0.0
    assert game.floor_line_owed() is False


# ---------------------------------------------------------------------------
# 3. Empty generation gets a floor line (the incident path)
# ---------------------------------------------------------------------------

def _empty_stop_agent(game, monkeypatch) -> LilyAgent:
    async def _empty(agent, chat_ctx, tools, model_settings, extra_kwargs=None):
        if False:  # pragma: no cover — keep async-generator shape
            yield "x"

    monkeypatch.setattr(LilyAgent, "_vocal_llm_stream", _empty)
    agent = LilyAgent.__new__(LilyAgent)
    agent._game = game
    object.__setattr__(agent, "_llm", None)
    agent._apply_context_blocks = lambda ctx, **kw: None
    agent._thinking_level_for_turn = lambda ctx: "low"
    return agent


def test_empty_generation_after_a_hail_airs_the_floor_line(monkeypatch):
    game = _game()
    game.publish_attributes_nowait = lambda: None
    game.expect_delivery = lambda: None
    game.rendered_armed_question = lambda: ""
    game._awaiting_address_since = time.time()  # a bare hail is outstanding
    agent = _empty_stop_agent(game, monkeypatch)

    async def _drain():
        return [chunk async for chunk in agent.llm_node(None, [], None)]

    out = _run(_drain())
    assert out == [lily_say_gate.LILY_FLOOR_LINES["lobby"][0]]
    # The address is marked floored — no retry storm behind the yielded line.
    assert game._floor_fired_for_ts == game._awaiting_address_since


def test_empty_generation_without_an_address_still_fails_closed(monkeypatch):
    # No host-directed final outstanding (a cold-open opener, an instruction-
    # driven generation): the floor does not fire — the existing fail-closed
    # / opener-recover path is preserved.
    from livekit.agents import APIConnectionError

    game = _game()
    game.publish_attributes_nowait = lambda: None
    game.expect_delivery = lambda: None
    game.rendered_armed_question = lambda: ""
    game.reconnected = False
    game._empty_stop_lobby_recover_count = 1  # cap the opener recover
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game.greeting_instructions = lambda: "Hi."
    game.gated_say = lambda *a, **k: True
    game._awaiting_address_since = 0.0
    agent = _empty_stop_agent(game, monkeypatch)

    async def _drain():
        with pytest.raises(APIConnectionError):
            async for _ in agent.llm_node(None, [], None):
                pass

    _run(_drain())
    assert game._floor_fired_for_ts == 0.0  # floor never fired


def test_a_normal_reply_never_triggers_the_floor(monkeypatch):
    # A contentful generation answers the address the ordinary way; the floor
    # is never consulted even with an address outstanding.
    async def _speaks(agent, chat_ctx, tools, model_settings, extra_kwargs=None):
        yield "Hey — right here, what do you need?"

    monkeypatch.setattr(LilyAgent, "_vocal_llm_stream", _speaks)
    game = _game()
    game.publish_attributes_nowait = lambda: None
    game._awaiting_address_since = time.time()
    agent = LilyAgent.__new__(LilyAgent)
    agent._game = game
    object.__setattr__(agent, "_llm", None)
    agent._apply_context_blocks = lambda ctx, **kw: None
    agent._thinking_level_for_turn = lambda ctx: "low"

    async def _drain():
        return [chunk async for chunk in agent.llm_node(None, [], None)]

    assert _run(_drain()) == ["Hey — right here, what do you need?"]
    assert game._floor_fired_for_ts == 0.0  # floor never fired


# ---------------------------------------------------------------------------
# 4. tts_node say-suppression schedules the floor; watchdog fires it
# ---------------------------------------------------------------------------

def test_say_suppression_schedules_a_floor_line_when_owed():
    game = _game()
    game._awaiting_address_since = time.time()
    fired = []
    game.gated_say = lambda key, act, instr, source, text=None: (
        fired.append({"act": act, "source": source, "text": text}) or True
    )

    async def _scenario():
        _lily_schedule_floor_if_owed(game, "say_suppressed")
        await asyncio.sleep(0)  # let the scheduled task run

    _run(_scenario())
    assert len(fired) == 1
    assert fired[0]["act"] == "floor"
    assert fired[0]["source"] == "say_suppressed"
    assert fired[0]["text"] in lily_say_gate.LILY_FLOOR_LINES["lobby"]


def test_say_suppression_does_not_schedule_when_not_owed():
    # A duplicate/held suppression whose original already aired cleared the
    # address latch — nothing is owed, so no floor line is scheduled.
    game = _game()
    game._awaiting_address_since = 0.0
    fired = []
    game.gated_say = lambda *a, **k: fired.append(True) or True

    async def _scenario():
        _lily_schedule_floor_if_owed(game, "say_suppressed")
        await asyncio.sleep(0)

    _run(_scenario())
    assert fired == []


def test_address_watchdog_fires_the_floor_past_the_budget(monkeypatch):
    monkeypatch.setattr(lily_config, "responsiveness_budget_seconds", lambda: 3.0)
    game = _game()
    game._awaiting_address_since = time.time() - 30.0  # the 34s-silence class
    game._address_unanswered_warned = False
    fired = []
    game.gated_say = lambda key, act, instr, source, text=None: (
        fired.append({"act": act, "source": source, "text": text}) or True
    )

    result = _run(LilyGame._wp_address_unanswered(game))
    assert result == "address_checked"
    assert game._address_unanswered_warned is True
    assert len(fired) == 1
    assert fired[0]["act"] == "floor"
    assert fired[0]["source"] == "address_unanswered"


def test_address_watchdog_within_budget_does_not_fire(monkeypatch):
    monkeypatch.setattr(lily_config, "responsiveness_budget_seconds", lambda: 3.0)
    game = _game()
    game._awaiting_address_since = time.time() - 1.0  # inside the budget
    game._address_unanswered_warned = False
    fired = []
    game.gated_say = lambda *a, **k: fired.append(True) or True

    _run(LilyGame._wp_address_unanswered(game))
    assert fired == []


# ---------------------------------------------------------------------------
# 5. Structure pins — the floor rides gated_say, never a bespoke bypass
# ---------------------------------------------------------------------------

def test_fire_floor_line_dispatches_through_gated_say():
    game = _game()
    game._awaiting_address_since = time.time()
    calls = []
    game.gated_say = lambda key, act, instr, source, text=None: (
        calls.append((key, act, source, text)) or True
    )
    assert game.fire_floor_line("watchdog") is True
    assert calls == [
        (None, "floor", "watchdog", lily_say_gate.LILY_FLOOR_LINES["lobby"][0])
    ]
