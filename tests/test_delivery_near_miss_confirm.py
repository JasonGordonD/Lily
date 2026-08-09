"""One-owner delivery: near-miss confirms + opens — never full re-read.

Gun 3 (NUDGE_NEAR_MISS) used to dispatch question_nudge after the table
already heard a ≥0.9 similarity performance → double question. Force
claim confirm + open_window instead.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_audeering_consumers
import lily_say_gate
from lily_agent import WINDOW_FALLBACK_AGENT_TURNS, LilyGame
from lily_scorekeeper import LilyScorekeeper


class _FakeSession:
    def __init__(self) -> None:
        self.instructions: list[str] = []

    def generate_reply(self, instructions: str) -> None:
        self.instructions.append(instructions)


def _game(prompt: str) -> LilyGame:
    game = LilyGame.__new__(LilyGame)
    game.session = _FakeSession()
    game.agent = None
    game._preemptive_paused = False
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game.sk = LilyScorekeeper("near-miss")
    game.game_started = True
    game.game_over = False
    game.armed_question = {
        "id": "q1",
        "prompt": prompt,
        "canonical_answer": "x",
    }
    game.next_question = None
    game.ui_phase = "question"
    game._window_timer = None
    game._bed_handle = None
    game.background_audio = None
    game._steal_window = False
    game._adjudicating = False
    game._pending_reveal_event = None
    game._armed_speech_misses = 0
    game._pending_delivery_qnum = None
    game._undelivered_ticks = 0
    game._undelivered_refires = 0
    game._last_armed_speech_ratio = 0.0
    game._question_transitioning = False
    game._whats_new_pending = False
    game._late_answer_note = None
    game._playout_started_ids = set()
    game._speech_handles = {}
    game._delivery_speech_acts = {}
    game._stale_retry_counts = {}
    game.addressee_classifier = None
    game.eliminated = []
    game.acoustic = lily_audeering_consumers.LilyAcousticState()
    game.sk.question_number = 1
    game.sk.set_phase("round")
    game.record_agent_turn = lambda *a, **k: None
    game.note_or_choice_offer = lambda *a, **k: None
    game.note_picture_on_offer = lambda *a, **k: None
    game.open_window = lambda *a, **k: game.sk.open_answer_window(duration=30.0)
    game.open_window_after_discharge = (
        LilyGame.open_window_after_discharge.__get__(game, LilyGame)
    )
    game._answer_window_duration = lambda: 30.0
    game._start_bed = lambda: None
    game._set_ui_phase = lambda p: setattr(game, "ui_phase", p)
    game.publish_attributes_nowait = lambda: None
    game.publish_metadata = lambda *a, **k: asyncio.sleep(0)
    game.question_already_answered = lambda *_a, **_k: False
    game.expect_delivery = lambda: None
    game.gated_say = lambda *a, **k: game.session.generate_reply(
        a[2] if len(a) > 2 else ""
    )
    game._replay_pre_window_answers = lambda: None
    game.force_confirm_delivery_heard = (
        LilyGame.force_confirm_delivery_heard.__get__(game, LilyGame)
    )
    game.on_agent_speech_finished = (
        LilyGame.on_agent_speech_finished.__get__(game, LilyGame)
    )
    game.reconcile_undelivered_claim = (
        LilyGame.reconcile_undelivered_claim.__get__(game, LilyGame)
    )
    game._delivery_confirmed = (
        LilyGame._delivery_confirmed.__get__(game, LilyGame)
    )
    return game


PROMPT = "What is the capital of France?"


def test_force_confirm_opens_window_without_nudge():
    g = _game(PROMPT)
    assert g.sk.answer_window_open is False
    assert g.force_confirm_delivery_heard(reason="unit", ratio=1.0) is True
    assert g.say_registry.state("q_1_delivery") == lily_say_gate.CLAIM_CONFIRMED
    assert g.sk.answer_window_open is True
    assert g.session.instructions == []


def test_near_miss_on_speech_finished_does_not_nudge(monkeypatch):
    g = _game(PROMPT)
    monkeypatch.setattr(
        "lily_evaluation.lily_question_spoken_ratio",
        lambda prompt, spoken: 1.0,
    )
    for _ in range(WINDOW_FALLBACK_AGENT_TURNS):
        g.on_agent_speech_finished(PROMPT)
    assert g.sk.answer_window_open is True
    assert g.say_registry.state("q_1_delivery") == lily_say_gate.CLAIM_CONFIRMED
    # No full re-read nudge dispatched.
    assert g.session.instructions == []


def test_undelivered_near_miss_confirms_not_refires():
    g = _game(PROMPT)
    g._last_armed_speech_ratio = 0.95
    g._undelivered_reconcile_ticks = lambda: 1
    g._undelivered_ticks = 0
    # First call: ticks 0→1, threshold 1 → confirmed immediately.
    assert g.reconcile_undelivered_claim() == "confirmed"
    assert g.sk.answer_window_open is True
    assert g.session.instructions == []
    # Second call: already confirmed / window open → idle.
    assert g.reconcile_undelivered_claim() == "idle"
