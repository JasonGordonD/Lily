"""WO-LILY-OMNIBUS-003 WS-2 — registered-undelivered reconciliation.

Evidence session `lily-81BCB0-583a0f16`: q_0001 (Jupiter) and q_2943
(Lisa) were registered in asked_history and never reached playout — the
engine held the round-2 loop for ~5 minutes on an unheard question. WS-0
covers the FAILED generation (GENERATION_FAILED + suppressed-path
release); this file pins the DIFFERENT class WS-2 owns: a delivery armed
and registered whose claim never confirms and never releases (dispatched,
no playout completion, no exception), or a delivery never dispatched at
all — a fully-silent stall the finished-turn nudge machinery never trips.

Contract pinned here:
  1. Reconciliation is idle while the delivery is confirmed, no question
     is armed, a window is open, a ruling is in flight, or the reconcile
     window has not yet elapsed.
  2. A claim stuck registered-but-unplayed past the reconcile window
     RE-FIRES: the stale PENDING claim releases and a fresh structural
     delivery is dispatched (expect_delivery armed + a question nudge).
  3. After UNDELIVERED_MAX_REFIRES re-fires that still never air, the
     question RELEASES: it is deregistered from the in-memory
     asked_history mirror and dropped so the idle path arms a fresh one.
  4. no_stuck_claims() — WS-6's gate — is True until a claim is stuck past
     the reconcile window, and True again once reconciliation clears it.

Same import boundary as test_claim_integrity_fixture.py (pulls in
livekit via lily_agent).
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_audeering_consumers
import lily_config
import lily_say_gate
from lily_agent import LilyGame, UNDELIVERED_MAX_REFIRES
from lily_scorekeeper import LilyScorekeeper

SESSION_ID = "lily-81BCB0-583a0f16"

# Prompts reconstructed around the verbatim canonical answers (the record
# persists only question_text_hash + canonical_answer for asked_history).
GHOST_Q1 = {
    "id": "q_0001",
    "prompt": "Which planet in our solar system is the largest?",
    "canonical_answer": "Jupiter",
    "category": "academic",
    "difficulty_tier": 1,
}
GHOST_Q_LISA = {
    "id": "q_2943",
    "prompt": "What is the name of the eldest Simpson child?",
    "canonical_answer": "Lisa",
    "category": "pop culture",
    "difficulty_tier": 1,
}


class _FakeSession:
    def __init__(self) -> None:
        self.instructions: list[str] = []

    def generate_reply(self, instructions: str) -> None:
        self.instructions.append(instructions)


def _make_game(game_started: bool = True) -> LilyGame:
    game = LilyGame.__new__(LilyGame)
    game.session = _FakeSession()
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game.sk = LilyScorekeeper(SESSION_ID)
    game.game_started = game_started
    game.game_over = False
    game.armed_question = None
    game.next_question = None
    game.asked_history = []
    game.used_prompts = []
    game.supabase = None
    game.ui_phase = "question"
    game._adjudicating = False
    game._question_transitioning = False
    game._armed_speech_misses = 0
    game._pending_delivery_qnum = None
    game._undelivered_ticks = 0
    game._undelivered_refires = 0
    game.acoustic = lily_audeering_consumers.LilyAcousticState()
    game.session_started_at = time.time() - 300.0
    # gated_say -> instructed_reply: capture instead of speaking.
    game.instructed_replies: list[str] = []
    game.instructed_reply = lambda text: game.instructed_replies.append(text)
    return game


def _arm(game: LilyGame, question: dict) -> None:
    game.armed_question = dict(question)
    game.sk.start_question(game.armed_question)
    game.asked_history.append({
        "question_id": question["id"],
        "question_text_hash": f"hash_{question['id']}",
        "canonical_answer": question["canonical_answer"],
    })


def _delivery_key(game: LilyGame) -> str:
    return f"q_{game.sk.question_number}_delivery"


def _tick_to_threshold(game: LilyGame) -> str:
    """Drive reconcile the exact number of stuck ticks its window needs,
    returning the final verdict."""
    ticks = game._undelivered_reconcile_ticks()
    verdict = "idle"
    for _ in range(ticks):
        verdict = game.reconcile_undelivered_claim()
    return verdict


# -- idle cases ----------------------------------------------------------------


def test_reconcile_idle_when_delivery_confirmed():
    game = _make_game()
    _arm(game, GHOST_Q1)
    key = _delivery_key(game)
    game.say_registry.claim(key)
    game.say_registry.confirm(key)  # aired
    assert game.reconcile_undelivered_claim() == "idle"
    assert game.reconcile_undelivered_claim() == "idle"
    assert game.instructed_replies == []


def test_reconcile_idle_before_threshold():
    game = _make_game()
    _arm(game, GHOST_Q1)
    game.say_registry.claim(_delivery_key(game))  # PENDING, just dispatched
    # One tick short of the window: still in-flight, not stuck.
    for _ in range(game._undelivered_reconcile_ticks() - 1):
        assert game.reconcile_undelivered_claim() == "idle"
    assert game.instructed_replies == []


def test_reconcile_idle_pre_game_and_when_unarmed():
    game = _make_game(game_started=False)
    _arm(game, GHOST_Q1)
    assert _tick_to_threshold(game) == "idle"
    game2 = _make_game()
    game2.armed_question = None
    assert game2.reconcile_undelivered_claim() == "idle"


def test_reconcile_idle_while_adjudicating():
    game = _make_game()
    _arm(game, GHOST_Q1)
    game.say_registry.claim(_delivery_key(game))
    game._adjudicating = True
    assert _tick_to_threshold(game) == "idle"
    assert game.instructed_replies == []


# -- the WS-2 stuck class ------------------------------------------------------


def test_pending_claim_never_played_refires():
    # Kill a delivery pre-playout: claimed at dispatch, on_agent_speech_
    # finished never fires (no confirm, no release). It must re-fire.
    game = _make_game()
    _arm(game, GHOST_Q1)
    key = _delivery_key(game)
    game.say_registry.claim(key)
    assert game.say_registry.state(key) == lily_say_gate.CLAIM_PENDING

    assert _tick_to_threshold(game) == "refired"
    # Stale claim released so the re-ask re-claims cleanly.
    assert game.say_registry.state(key) is None
    # Fresh structural delivery armed + a nudge dispatched.
    assert game._pending_delivery_qnum == game.sk.question_number
    assert len(game.instructed_replies) == 1
    assert game._undelivered_refires == 1


def test_refire_holds_while_table_is_still_talking():
    # An interrupted delivery with recent user speech must not re-ask on
    # top of the banter (RM_qs6 / RM_VYp6 undelivered-refire loops).
    game = _make_game()
    _arm(game, GHOST_Q1)
    key = _delivery_key(game)
    game.say_registry.claim(key)
    game._last_user_turn_at = time.monotonic()  # just spoke
    # Even past the reconcile tick threshold, recent talk keeps it idle.
    for _ in range(game._undelivered_reconcile_ticks() + 2):
        assert game.reconcile_undelivered_claim() == "idle"
    assert game._undelivered_refires == 0
    assert game.say_registry.state(key) == lily_say_gate.CLAIM_PENDING


def test_undelivered_no_claim_refires():
    # Armed and in asked_history, delivery never dispatched, fully silent
    # (no finished agent turn ever advances the nudge machinery).
    game = _make_game()
    _arm(game, GHOST_Q_LISA)
    assert game.say_registry.state(_delivery_key(game)) is None
    assert _tick_to_threshold(game) == "refired"
    assert game._pending_delivery_qnum == game.sk.question_number
    assert len(game.instructed_replies) == 1


def test_refire_exhaustion_releases_question():
    game = _make_game()
    _arm(game, GHOST_Q1)
    qid = GHOST_Q1["id"]
    assert any(r["question_id"] == qid for r in game.asked_history)

    # Each re-fire cycle: a stuck claim, threshold ticks, re-fire.
    for attempt in range(1, UNDELIVERED_MAX_REFIRES + 1):
        game.say_registry.claim(_delivery_key(game))  # dispatched, never airs
        assert _tick_to_threshold(game) == "refired"
        assert game._undelivered_refires == attempt

    # Next stuck cycle exhausts re-fires -> release.
    game.say_registry.claim(_delivery_key(game))
    assert _tick_to_threshold(game) == "released"
    # Deregistered from the in-memory mirror; dropped so a fresh one arms.
    assert not any(r["question_id"] == qid for r in game.asked_history)
    assert game.armed_question is None
    assert game._undelivered_refires == 0


def test_release_pops_reconstructed_draw_without_id_match():
    game = _make_game()
    _arm(game, GHOST_Q1)
    # Simulate a mirror row whose id does not match (reconstructed draw).
    game.asked_history[-1]["question_id"] = None
    game._undelivered_refires = UNDELIVERED_MAX_REFIRES
    game.say_registry.claim(_delivery_key(game))
    assert _tick_to_threshold(game) == "released"
    assert game.asked_history == []
    assert game.armed_question is None


# -- WS-6 predicate ------------------------------------------------------------


def test_no_stuck_claims_predicate_tracks_reconciliation():
    game = _make_game()
    # Nothing armed: clean.
    assert game.no_stuck_claims() is True

    _arm(game, GHOST_Q1)
    game.say_registry.claim(_delivery_key(game))
    # Freshly dispatched, no re-fire yet: in-flight, not stuck.
    assert game.no_stuck_claims() is True

    # First re-fire raises the persistent stuck signal.
    assert _tick_to_threshold(game) == "refired"
    assert game.no_stuck_claims() is False

    # The delivery finally airs: its claim confirms -> cleared.
    key = _delivery_key(game)
    game.say_registry.claim(key)
    game.say_registry.confirm(key)
    assert game.no_stuck_claims() is True


def test_predicate_stays_false_across_ticks_during_persistent_stall():
    # The WS-6 requirement: no_stuck_claims() must report False for the
    # DURATION of an active stall, not just at the single threshold-crossing
    # tick. _undelivered_refires persists across the reset that clears
    # _undelivered_ticks each cycle.
    game = _make_game()
    _arm(game, GHOST_Q1)
    game.say_registry.claim(_delivery_key(game))

    # First stuck window -> first re-fire; stuck signal now up.
    assert _tick_to_threshold(game) == "refired"
    assert game._undelivered_refires == 1
    assert game.no_stuck_claims() is False

    # Delivery STILL never airs. Across the idle ticks that accrue toward
    # the next re-fire, a WS-6 reader BETWEEN watchdog ticks keeps seeing
    # stuck — this is exactly the between-tick window the old ticks-based
    # predicate lost.
    ticks = game._undelivered_reconcile_ticks()
    for _ in range(ticks - 1):
        assert game.reconcile_undelivered_claim() == "idle"
        assert game.no_stuck_claims() is False
    # The window-closing tick re-fires again; still stuck across it.
    assert game.reconcile_undelivered_claim() == "refired"
    assert game._undelivered_refires == 2
    assert game.no_stuck_claims() is False

    # Only once the question is released does it clear.
    game.say_registry.claim(_delivery_key(game))
    assert _tick_to_threshold(game) == "released"
    assert game.armed_question is None
    assert game.no_stuck_claims() is True


def test_no_stuck_claims_true_when_delivery_confirmed():
    game = _make_game()
    _arm(game, GHOST_Q1)
    key = _delivery_key(game)
    game.say_registry.claim(key)
    game.say_registry.confirm(key)
    game._undelivered_refires = 5  # even mid-re-fire-cycle, a confirmed
    assert game.no_stuck_claims() is True  # delivery is never stuck


# -- evidence pin --------------------------------------------------------------


def test_pin_ghost_session_undelivered_pair():
    # Both q_0001 and q_2943 registered in asked_history, delivery never
    # aired: the WS-2 defect class, reconciled within the window.
    for question in (GHOST_Q1, GHOST_Q_LISA):
        game = _make_game()
        _arm(game, question)
        assert any(
            r["question_id"] == question["id"] for r in game.asked_history
        )
        # Registered-undelivered and unheard -> stuck -> re-fires (not a
        # silent five-minute hold).
        assert _tick_to_threshold(game) == "refired"
