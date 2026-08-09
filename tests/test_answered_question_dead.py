"""P0-A: a committed/revealed question is dead forever.

Live fixture lily-9337B1-331ff234:
  "He said it" armed clarify -> "Sigmund Freud" scored/revealed ->
  stale final-answer check -> full Freud re-ask.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_say_gate
from lily_agent import LilyAgent, LilyGame
from lily_scorekeeper import LilyScorekeeper


class _FakeSession:
    def __init__(self) -> None:
        self.instructions: list[str] = []

    def generate_reply(self, instructions: str) -> None:
        self.instructions.append(instructions)


def _game() -> LilyGame:
    game = LilyGame.__new__(LilyGame)
    game.session = _FakeSession()
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game.sk = LilyScorekeeper("answered-dead")
    game.sk.bind_speaker("S1", "Rami")
    game.sk.question_number = 3
    game.sk.current_question = {
        "id": "q_1034",
        "prompt": (
            "Famously praising cocaine, who is this controversial "
            "Austrian psychoanalyst?"
        ),
        "canonical_answer": "Sigmund Freud",
        "acceptable_answers": ["sigmund freud", "freud"],
    }
    game.armed_question = dict(game.sk.current_question)
    game.game_started = True
    game.game_over = False
    game.pending_clarify = {}
    game._answered_questions = set()
    game._clarify_fired_questions = set()
    game._session_clarify_count = 0
    game._speech_handles = {}
    game._suppressed_speech_ids = set()
    game.supabase = None
    game.events = []
    game.send_event_nowait = lambda kind, payload: game.events.append(
        (kind, payload)
    )
    return game


def _candidate(text: str = "He said it.") -> dict:
    return {
        "player": "Rami",
        "speaker_label": "S1",
        "text": text,
        "segment_start_time": time.time(),
        "timestamp": time.time(),
    }


def test_answer_heard_clears_pending_clarify_and_claim():
    game = _game()
    game.sk.answer_candidates["Rami"] = _candidate()
    assert game.mark_pending_clarify("Rami") is True
    assert game.pending_clarify["Rami"]["question_number"] == 3
    assert game.say_registry.claim("q_3_clarify", owner="clarify-speech")

    game.note_answer_heard(3)

    assert game.question_is_terminal(3) is True
    assert game.pending_clarify == {}
    assert game.say_registry.state("q_3_clarify") is None


def test_terminal_question_cannot_arm_new_deterministic_clarify():
    game = _game()
    game._answered_questions.add(3)

    game._maybe_fire_clarify(
        _candidate(),
        {"verdict": "uncertain", "similarity": 0.8},
        0.88,
        "OPEN_WINDOW",
    )

    assert game.pending_clarify == {}
    assert game.session.instructions == []


def test_terminal_question_ignores_racing_clarify_reply():
    game = _game()
    game.pending_clarify["Rami"] = {
        "row_task": None,
        "question_number": 3,
    }
    game._answered_questions.add(3)

    game._resolve_clarify("Rami", "I'm thinking out loud.")

    assert game.pending_clarify == {}


def test_clarify_tool_refuses_after_result_or_closed_window():
    game = _game()
    game._answered_questions.add(3)
    agent = LilyAgent.__new__(LilyAgent)
    agent._game = game

    msg = asyncio.run(
        LilyAgent.lily_log_clarify.__wrapped__(agent, None, "Rami")
    )

    assert "already closed" in msg
    assert "do not re-ask" in msg
    assert game.pending_clarify == {}


def test_answered_closed_state_line_names_terminal_contract():
    game = _game()
    game._answered_questions.add(3)

    line = game.answered_closed_state_line()

    assert line is not None
    assert "q3 DONE" in line
    assert "Do not clarify" in line
    assert "re-ask" in line


def test_next_question_is_not_marked_closed():
    game = _game()
    game._answered_questions.add(3)
    game.sk.question_number = 4

    assert game.answered_closed_state_line() is None
