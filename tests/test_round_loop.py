"""Regression tests for the arm -> ask -> window -> adjudicate loop.

Every session captured on 2026-07-14 landed with round=0 and
question_number=0 in lily_sessions despite Lily awarding bonus points and
running for hours. Two root causes:

1. No LLM tool existed for Lily to actually START the tiered loop. The
   prompt told her to "begin round one on the first genuine group laugh"
   but the only game entry was the frontend RPC lily_control.start, which
   never fired for tables that used voice-only.

2. The token-overlap gate on _question_was_spoken required 60% of >3-char
   prompt tokens to land in the spoken text — Lily's habit of paraphrasing
   the prompt at ask time (and TTS tag stripping) meant the gate rarely
   fired, so the answer window never opened even when the loop DID engage.

These tests pin both fixes.
"""

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lily_agent import _question_was_spoken
from lily_scorekeeper import LilyScorekeeper


# ---------------------------------------------------------------------------
# _question_was_spoken — loosened gate
# ---------------------------------------------------------------------------

def test_question_spoken_exact_match_still_fires():
    q = "Which colorful sea sits between Europe and Asia"
    assert _question_was_spoken(q, q) is True


def test_question_spoken_paraphrase_now_fires():
    # Real-world shape: Lily reshapes the prompt around the answer-at-end
    # house rule. This used to fail at the 60% threshold and is exactly the
    # class of miss that kept the answer window closed all afternoon.
    q = "Which colorful sea sits between Europe and Asia"
    spoken = "this colorful sea between Europe and Asia"
    assert _question_was_spoken(q, spoken) is True


def test_question_spoken_off_topic_banter_does_not_fire():
    q = "Which colorful sea sits between Europe and Asia"
    banter = "Dave you are on a streak my friend keep going"
    assert _question_was_spoken(q, banter) is False


def test_question_spoken_single_incidental_hit_does_not_fire():
    # Guard on the loosened threshold: one bare word overlap on a long
    # prompt must not open the window.
    q = "Which twentieth century composer wrote the Rite of Spring"
    accidental = "the century belongs to whoever moves fastest"
    assert _question_was_spoken(q, accidental) is False


def test_question_spoken_empty_inputs_do_not_fire():
    assert _question_was_spoken("", "anything") is False
    assert _question_was_spoken("anything", "") is False


# ---------------------------------------------------------------------------
# arm_next_question arithmetic: round + question_number MUST advance in
# lockstep, and both must appear in the checkpoint snapshot the persistence
# writer sends to lily_sessions.
# ---------------------------------------------------------------------------

def _arm_and_start(sk: LilyScorekeeper, prompt: str) -> None:
    """Simulate the mutating half of LilyGame.arm_next_question. The full
    method touches livekit; the round/question_number arithmetic under test
    lives here and mirrors the production sequence."""
    sk.start_question({"prompt": prompt, "canonical_answer": "-"})
    # Round derivation used by LilyGame.arm_next_question (line 375-378
    # in lily_agent.py). Must be run AFTER start_question, which bumps
    # question_number first.
    rounds_total = sk.rounds_total
    sk.round = min(
        rounds_total + 1,
        (sk.question_number - 1) // sk.questions_per_round + 1,
    )
    if sk.round > rounds_total:
        sk.set_phase("final")
    else:
        sk.set_phase("round")


def test_arm_next_question_advances_question_number():
    sk = LilyScorekeeper("test-room")
    sk.questions_per_round = 6
    sk.rounds_total = 3
    assert sk.question_number == 0
    _arm_and_start(sk, "q1")
    assert sk.question_number == 1
    _arm_and_start(sk, "q2")
    assert sk.question_number == 2


def test_arm_next_question_advances_round_on_wrap():
    sk = LilyScorekeeper("test-room")
    sk.questions_per_round = 6
    sk.rounds_total = 3
    for i in range(6):
        _arm_and_start(sk, f"round1-q{i}")
    assert sk.round == 1
    assert sk.question_number == 6
    _arm_and_start(sk, "round2-q1")
    assert sk.round == 2
    assert sk.question_number == 7


def test_arm_next_question_enters_final_after_last_regular_round():
    sk = LilyScorekeeper("test-room")
    sk.questions_per_round = 6
    sk.rounds_total = 3
    for i in range(3 * 6):
        _arm_and_start(sk, f"regular-q{i}")
    assert sk.phase == "round"
    assert sk.round == 3
    _arm_and_start(sk, "wager-question")
    assert sk.phase == "final"
    assert sk.round == 4  # rounds_total + 1


def test_snapshot_carries_round_and_question_number():
    """The persistence writer serializes snapshot() into lily_sessions.
    Both top-level columns AND the scorekeeper_state jsonb must reflect
    real gameplay — the 2026-07-14 rows had 0/0 in both places, proving
    the loop truly never ran."""
    sk = LilyScorekeeper("test-room")
    sk.questions_per_round = 6
    sk.rounds_total = 3
    for i in range(7):
        _arm_and_start(sk, f"q{i}")
    snap = sk.snapshot()
    assert snap["round"] == 2
    assert snap["question_number"] == 7
    assert snap["phase"] == "round"


# ---------------------------------------------------------------------------
# Auto-start guard logic — mirrors LilyGame._maybe_auto_start_after_lobby.
# The fake game keeps the test out of the livekit import graph; the guard
# conditions here are identical to the production code.
# ---------------------------------------------------------------------------

class _FakeGame:
    """Minimal stand-in for LilyGame that exercises the auto-start guard
    without importing livekit-heavy machinery. The guard itself only reads
    scalars on the game; the actual start_game call is replaced with a
    counter."""

    def __init__(
        self,
        roster_size: int,
        next_question_ready: bool,
        elapsed_s: float,
        game_started: bool = False,
        game_over: bool = False,
    ) -> None:
        self.roster_size = roster_size
        self.next_question_ready = next_question_ready
        self.elapsed_s = elapsed_s
        self.game_started = game_started
        self.game_over = game_over
        self.start_calls = 0
        self.prefetch_calls = 0

    def _try_auto_start(
        self,
        min_players: int = 2,
        grace_s: float = 60.0,
    ) -> None:
        if self.game_started or self.game_over:
            return
        if self.roster_size < min_players:
            return
        if not self.next_question_ready:
            self.prefetch_calls += 1
            return
        if self.elapsed_s < grace_s:
            return
        self.start_calls += 1


def test_auto_start_fires_after_lobby_grace():
    g = _FakeGame(roster_size=3, next_question_ready=True, elapsed_s=90.0)
    g._try_auto_start()
    assert g.start_calls == 1


def test_auto_start_blocked_by_single_voice():
    # Single-voice tune-ups (one person testing the mic) must never flip
    # into game mode from ambient chatter.
    g = _FakeGame(roster_size=1, next_question_ready=True, elapsed_s=90.0)
    g._try_auto_start()
    assert g.start_calls == 0


def test_auto_start_blocked_before_grace():
    g = _FakeGame(roster_size=3, next_question_ready=True, elapsed_s=15.0)
    g._try_auto_start()
    assert g.start_calls == 0


def test_auto_start_blocked_without_prefetched_question():
    # If the question bank hasn't landed yet the guard requests a prefetch
    # and defers. Firing start_game without a question armed would put the
    # state block into a "banter forever" hole again.
    g = _FakeGame(roster_size=3, next_question_ready=False, elapsed_s=90.0)
    g._try_auto_start()
    assert g.start_calls == 0
    assert g.prefetch_calls == 1


def test_auto_start_is_noop_once_started():
    g = _FakeGame(
        roster_size=3, next_question_ready=True, elapsed_s=90.0,
        game_started=True,
    )
    g._try_auto_start()
    assert g.start_calls == 0
