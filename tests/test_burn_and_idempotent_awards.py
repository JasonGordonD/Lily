"""WS-4 (WO-LILY-OMNIBUS-003) — revealed-question burn + idempotent awards.

Live evidence (session lily-81BCB0-583a0f16): the Stranger Things
question timed out, Lily spoke the answer aloud, the engine re-armed the
SAME question, solicited the echo of the just-revealed answer, and a
single lily_answers row carried 2 points for one question worth 1.

The fix under test:
  - Burn protocol: a question whose answer has gone to air (any reveal,
    including a timeout Lily resolves by speaking the answer with nobody
    scoring) is DEAD for the session — it can never re-arm and is
    excluded from every future draw. An echo of the revealed answer has
    no live window to score into.
  - Idempotent awards: scoring mutations are keyed on question_id +
    player. One point-earning award per question per player, regardless
    of how many resolution dispatches fire — a duplicate dispatch cannot
    change the ledger and cannot push a row above the question value.

Bolts onto WS-7's single write path (apply_score_event) and the existing
no-repeat draw guard. Pure scorekeeper + a minimal LilyGame fake — no
network, no livekit runtime.
"""

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_audeering_consumers
import lily_say_gate
from lily_agent import LilyGame
from lily_bank import lily_question_text_hash
from lily_scorekeeper import LilyScorekeeper


def make_sk(**kwargs):
    sk = LilyScorekeeper(session_id="ws4-room", **kwargs)
    sk.bind_speaker("S1", "Sarah")
    sk.bind_speaker("S2", "Dave")
    return sk


# ---------------------------------------------------------------------------
# Idempotent awards (pure scorekeeper — the WS-7 choke point)
# ---------------------------------------------------------------------------

def test_duplicate_answer_dispatch_cannot_change_the_ledger():
    """Exit bar: a duplicate resolution dispatch cannot change the
    ledger. Two identical correct commits for the same (question, player)
    land ONE point-earning entry; the score stays at the question value.
    """
    sk = make_sk()
    first = sk.apply_score_event(
        "Sarah", cause="answer", correct=True, points=1, question_id="q_st",
        transcript="Stranger Things",
    )
    second = sk.apply_score_event(
        "Sarah", cause="answer", correct=True, points=1, question_id="q_st",
        transcript="Stranger Things",  # the echo of the revealed answer
    )
    assert sk.players["Sarah"]["score"] == 1          # never 2
    assert sk.ledger_scores()["Sarah"] == 1
    positive = [
        e for e in sk.score_ledger
        if e["player"] == "Sarah" and e["question_id"] == "q_st"
        and (e["points"] or 0) > 0
    ]
    assert len(positive) == 1                         # one award only
    # The duplicate is idempotent: it returns the prior award, appends
    # nothing, and does not advance the streak a second time.
    assert second is first
    assert sk.players["Sarah"]["streak"] == 1


def test_no_award_above_question_value_under_repeated_dispatch():
    """Exit bar: no row can carry points above the question value. Five
    duplicate dispatches of a 1-point question never sum past 1."""
    sk = make_sk()
    for _ in range(5):
        sk.apply_score_event(
            "Sarah", cause="answer", correct=True, points=1,
            question_id="q_st",
        )
    assert sk.players["Sarah"]["score"] == 1
    assert sk.ledger_scores()["Sarah"] == 1
    # A second dispatch that claims MORE points for the same key is still
    # refused — the cap is one award per question per player.
    sk.apply_score_event(
        "Sarah", cause="answer", correct=True, points=5, question_id="q_st",
    )
    assert sk.players["Sarah"]["score"] == 1


def test_make_good_is_idempotent_per_question_and_player():
    """A held open-floor award bound by two make-good dispatches (double
    speaker-bind) lands ONE point — the WS-7 held-then-bound minor never
    becomes two points."""
    sk = make_sk()
    sk.apply_score_event(
        "Sarah", cause="make_good", correct=None, points=1,
        question_id="q_ww", transcript="Walter White",
    )
    sk.apply_score_event(
        "Sarah", cause="make_good", correct=None, points=1,
        question_id="q_ww", transcript="Walter White",
    )
    assert sk.players["Sarah"]["score"] == 1
    assert sk.ledger_scores()["Sarah"] == 1


def test_held_then_bound_award_is_one_award():
    """The live shape: the open-floor winner is refused on the unrostered
    label (no mutation, no ledger), then the same point commits once when
    the voice binds. Exactly one positive ledger entry for that
    (question, player)."""
    sk = make_sk()
    # Unrostered hold: refused, no ledger entry (WS-7 — a point can never
    # land on a null player).
    refused = sk.apply_score_event(
        "unrostered:S9", cause="answer", correct=True, points=1,
        question_id="q_ww",
    )
    assert refused is None
    # Bound make-good commits the held point once.
    sk.apply_score_event(
        "Sarah", cause="make_good", correct=None, points=1,
        question_id="q_ww",
    )
    positive = [
        e for e in sk.score_ledger
        if e["question_id"] == "q_ww" and (e["points"] or 0) > 0
    ]
    assert len(positive) == 1
    assert positive[0]["player"] == "Sarah"


def test_incorrect_row_does_not_block_a_later_correct():
    """An incorrect commit (points=0) does not consume the idempotency
    key — a subsequent correct award for the same (question, player)
    still lands."""
    sk = make_sk()
    sk.apply_score_event(
        "Sarah", cause="answer", correct=False, points=0, question_id="q1",
    )
    entry = sk.apply_score_event(
        "Sarah", cause="answer", correct=True, points=1, question_id="q1",
    )
    assert entry is not None
    assert sk.players["Sarah"]["score"] == 1


def test_each_player_awards_once_on_the_same_question():
    """The key is (question, player): two different players each score
    their own point on the same question."""
    sk = make_sk()
    sk.apply_score_event("Sarah", cause="answer", correct=True, points=1,
                         question_id="q1")
    sk.apply_score_event("Dave", cause="answer", correct=True, points=1,
                         question_id="q1")
    assert sk.ledger_scores() == {"Sarah": 1, "Dave": 1}


def test_bonus_without_question_id_is_not_deduped():
    """Bonuses carry no question_id and are intentionally not keyed —
    two bonus awards both land."""
    sk = make_sk()
    sk.award_bonus("Sarah", points=1, transcript="best wrong answer")
    sk.award_bonus("Sarah", points=1, transcript="great sport")
    assert sk.players["Sarah"]["score"] == 2


def test_duplicate_award_logs_at_info(caplog):
    sk = make_sk()
    sk.apply_score_event("Sarah", cause="answer", correct=True, points=1,
                         question_id="q1")
    with caplog.at_level(logging.INFO):
        sk.apply_score_event("Sarah", cause="answer", correct=True, points=1,
                             question_id="q1")
    assert any("SCORE_DUPLICATE_AWARD" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Burn protocol (minimal LilyGame fake — re-arm guard)
# ---------------------------------------------------------------------------

def _make_game() -> LilyGame:
    """Minimal LilyGame via __new__ — only the attributes the burn / arm
    paths touch (per-file fake, mirroring the desync fixture pattern)."""
    game = LilyGame.__new__(LilyGame)
    game.sk = LilyScorekeeper("ws4-burn")
    game.game_started = True
    game.game_over = False
    game.armed_question = None
    game.next_question = None
    game.used_prompts = []
    game.supabase = None
    game.ui_phase = "question"
    game.rounds_total = 3
    game.asked_history = []
    game.group_id = "grp_ws4"
    game.prewager_standings = None
    game._armed_speech_misses = 0
    game._pending_delivery_qnum = None
    game._strict_delivery_qnum = None
    game._pre_window_segments = []
    game._judged_keys = set()
    game._spec_judge = {}
    game._addressee_rows = {}
    game._nbest_by_key = {}
    game._phase_hold = None
    game._drawn_ids = set()
    game._drawn_hashes = set()
    game._burned_question_ids = set()
    game._burned_question_hashes = set()
    game.eliminated = []
    game.acoustic = lily_audeering_consumers.LilyAcousticState()
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game.start_prefetch = lambda: None

    def _set_ui_phase(phase):
        game.ui_phase = phase

    game._set_ui_phase = _set_ui_phase
    return game


ST_QUESTION = {
    "id": "q_st",
    "prompt": "What 1980s-set Netflix series features the Upside Down?",
    "canonical_answer": "Stranger Things",
}


def test_burn_registers_id_and_text_hash():
    game = _make_game()
    game._burn_question(ST_QUESTION, reason="revealed")
    assert "q_st" in game._burned_question_ids
    assert lily_question_text_hash(ST_QUESTION["prompt"]) \
        in game._burned_question_hashes
    assert game._is_burned(ST_QUESTION) is True


def test_revealed_question_cannot_rearm_by_id():
    """Exit bar: a timeout-with-reveal question cannot re-arm. The same
    question object sitting in next_question is discarded, not performed
    again."""
    game = _make_game()
    game._burn_question(ST_QUESTION, reason="revealed")
    game.next_question = dict(ST_QUESTION)
    assert game.arm_next_question() is False
    assert game.armed_question is None
    assert game.next_question is None


def test_revealed_question_cannot_rearm_by_text_when_id_missing():
    """A regenerated draw of the same prompt with no id is still burned by
    its normalized-text hash."""
    game = _make_game()
    game._burn_question(ST_QUESTION, reason="revealed")
    game.next_question = {
        "prompt": ST_QUESTION["prompt"], "canonical_answer": "Stranger Things",
    }
    assert game.arm_next_question() is False
    assert game.armed_question is None


def test_unburned_question_still_arms():
    """The guard never over-blocks: a fresh question arms normally."""
    game = _make_game()
    game.next_question = {
        "id": "q_fresh", "prompt": "What is the capital of France?",
        "canonical_answer": "Paris",
    }
    assert game.arm_next_question() is True
    assert game.armed_question is not None
    assert game.armed_question.get("id") == "q_fresh"


def test_burned_ids_feed_the_no_repeat_exclusion():
    """The within-session no-repeat guard includes revealed questions:
    a burned id/hash rides the draw-exclusion sets alongside asked_history
    and this session's drawn set."""
    game = _make_game()
    game.asked_history = [{"question_id": "q_prior", "question_text_hash": "h_prior"}]
    game._drawn_ids = {"q_drawn"}
    game._drawn_hashes = {"h_drawn"}
    game._burn_question(ST_QUESTION, reason="revealed")
    ids, hashes = game._no_repeat_exclusion()
    assert {"q_prior", "q_drawn", "q_st"} <= ids
    assert lily_question_text_hash(ST_QUESTION["prompt"]) in hashes
    assert {"h_prior", "h_drawn"} <= hashes


def test_reconnect_never_restores_a_burned_question():
    """A reconnect must never resurrect a question whose answer already
    went to air."""
    game = _make_game()
    game._burn_question(ST_QUESTION, reason="revealed")
    game.sk.current_question = dict(ST_QUESTION)
    game.sk.phase = "round"
    game.reconnected = True
    game.restore_reconnected_state()
    assert game.armed_question is None
