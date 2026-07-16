"""
WO-ADDRESSEE-H1 Task 4 — the formalized clarify trigger (explicit-label
engine): Tier-1 similarity in the middle band under the active state-prior
threshold fires the binary clarify question deterministically; the reply
writes an explicit label through the existing pending-clarify machinery.
Rate limits: once per question, capped per session.
"""

import asyncio
import time

import lily_audeering_consumers
import lily_config
import lily_evaluation
import lily_say_gate
from lily_agent import LilyGame
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
    game = LilyGame.__new__(LilyGame)
    game.session = _FakeSession()
    game.agent = _FakeAgentHandle()
    game._preemptive_paused = False
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game.sk = LilyScorekeeper("test-room")
    game.sk.bind_speaker("S1", "Sarah")
    game.supabase = None
    game.pending_clarify = {}
    game._addressee_rows = {}
    game.acoustic = lily_audeering_consumers.LilyAcousticState()
    game.events = []
    game.send_event_nowait = lambda t, p: game.events.append((t, p))
    return game


def _cand(player="Sarah", text="the femer"):
    return {"player": player, "speaker_label": "S1", "text": text,
            "segment_start_time": time.time(), "timestamp": time.time()}


def _t1(similarity):
    return {"verdict": "uncertain", "similarity": similarity}


THRESHOLD = 0.88  # margin default 0.15 → clarify band [0.73, 0.88)


def test_middle_band_fires_named_binary_clarify():
    game = _make_game()
    game.sk.question_number = 3
    game._maybe_fire_clarify(_cand(), _t1(0.80), THRESHOLD, "OPEN_WINDOW")

    assert "Sarah" in game.pending_clarify
    assert game._session_clarify_count == 1
    assert any(
        "answer, or thinking out" in i and "Sarah" in i
        for i in game.session.instructions
    )
    assert ("clarify", {"name": "Sarah"}) in game.events


def test_fires_at_most_once_per_question():
    game = _make_game()
    game.sk.bind_speaker("S2", "Dave")
    game.sk.question_number = 3
    game._maybe_fire_clarify(_cand(), _t1(0.80), THRESHOLD, "OPEN_WINDOW")
    game._maybe_fire_clarify(
        _cand(player="Dave"), _t1(0.82), THRESHOLD, "OPEN_WINDOW"
    )
    assert game._session_clarify_count == 1
    assert "Dave" not in game.pending_clarify


def test_session_cap_respected(monkeypatch):
    monkeypatch.setenv("LILY_CLARIFY_MAX_PER_SESSION", "1")
    game = _make_game()
    game.sk.question_number = 1
    game._maybe_fire_clarify(_cand(), _t1(0.80), THRESHOLD, "OPEN_WINDOW")
    game.pending_clarify.clear()
    game.sk.question_number = 2
    game._maybe_fire_clarify(_cand(), _t1(0.80), THRESHOLD, "OPEN_WINDOW")
    assert game._session_clarify_count == 1


def test_outside_band_never_fires():
    game = _make_game()
    game.sk.question_number = 1
    # Above threshold (would have accepted) and below the band.
    game._maybe_fire_clarify(_cand(), _t1(0.95), THRESHOLD, "OPEN_WINDOW")
    game._maybe_fire_clarify(_cand(), _t1(0.40), THRESHOLD, "OPEN_WINDOW")
    assert game.pending_clarify == {}
    assert game.session.instructions == []


def test_unrostered_and_missing_similarity_never_fire():
    game = _make_game()
    game.sk.question_number = 1
    game._maybe_fire_clarify(_cand(player=None), _t1(0.80), THRESHOLD, "IDLE")
    game._maybe_fire_clarify(_cand(), {"verdict": "uncertain"}, THRESHOLD, "IDLE")
    assert game.pending_clarify == {}
