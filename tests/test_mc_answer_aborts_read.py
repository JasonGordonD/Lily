"""WS-5 (WO-LILY-OMNIBUS-003) — MC barge-in restoration + answer-aborts-read.

Two layers, offline:

  Layer 1 — literal barge-in on MC deliveries. Diagnosis (ws5-report):
  barge-in is NOT structurally disabled on MC deliveries (interruptions are
  enabled globally, min_words=1/min_duration=0.8, and the delivery is one
  interruptible SpeechHandle). What made it look dead was the interrupted
  delivery releasing its claim and a resolution path RE-DISPATCHING the same
  read verbatim (WS-3 owns the no-verbatim-replay gate), plus the window
  opening only at FULL playout so the barging answer landed in a closed
  window. mc_early_answer_check halts the read AND adjudicates, so it neither
  replays nor goes inert — the JOINT "TTS halts + no verbatim replay" check.

  Layer 2 — the contract. Answers become adjudicable once actual delivery
  playout begins and the stem has been read (stem stays protected); a
  buffered final that Tier-1-matches a read option truncates the remaining
  options and jumps to adjudication. Pre-claim/queued speech is never an
  answer to a question the table has not heard.

Idiom mirrors test_recognition_variety._make_game (LilyGame via __new__,
stubbed adjudicate + publishes), plus a fake session recording interrupt().
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_audeering_consumers
import lily_config
import lily_evaluation
import lily_say_gate
from lily_agent import LilyGame
from lily_scorekeeper import LilyScorekeeper

CHOICES = ["Canberra", "Sydney", "Melbourne", "SpongeBob's pineapple"]
CANONICAL = "Canberra"  # choices[0] -> letter A
MC_QUESTION = {
    "id": "q_4242",
    "category": "academic",
    "difficulty_tier": 2,
    "prompt": "Name the capital city of Australia.",  # 6 words
    "canonical_answer": CANONICAL,
    "acceptable_answers": ["canberra"],
    "reveal_color": "",
    "choices": list(CHOICES),
}
FREEFORM_QUESTION = {
    "id": "q_9001",
    "category": "academic",
    "difficulty_tier": 2,
    "prompt": "What is the capital city of Australia?",
    "canonical_answer": CANONICAL,
    "acceptable_answers": ["canberra"],
    "reveal_color": "",
}


def _run(coro, game=None):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        timer = getattr(game, "_window_timer", None) if game else None
        if timer is not None and not timer.done():
            timer.cancel()
        loop.close()


class _FakeSession:
    def __init__(self) -> None:
        self.interrupts = 0

    def interrupt(self, *, force: bool = False):
        self.interrupts += 1
        return None


def _make_game() -> LilyGame:
    game = LilyGame.bare()
    game.sk = LilyScorekeeper("mc-abort-fixture")
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game.group_id = "grp_mc"
    game.supabase = None
    game.memory_block = ""
    game.prefs = {}
    game._pre_window_segments = []
    game.game_started = True
    game.game_over = False
    game.armed_question = None
    game.next_question = None
    game.ui_phase = "question"
    game._phase_hold = None
    game._window_timer = None
    game._bed_handle = None
    game.background_audio = None
    game._steal_window = False
    game._adjudicating = False
    game.acoustic = lily_audeering_consumers.LilyAcousticState()
    game.eliminated = []
    game.reasoning = None
    game._state_note = None
    game._undelivered_ticks = 0
    game._undelivered_refires = 0
    # WS-5 state.
    game._pending_delivery_qnum = None
    game._mc_delivery_qnum = None
    game._mc_delivery_started_at = None
    game._mc_delivery_stem_words = 0
    game._active_delivery_qnum = None
    game._active_delivery_started_at = None
    game._active_delivery_ended_at = None
    game._recent_finals = []
    game.session = _FakeSession()
    game.published = []

    async def _publish_attributes(*a, **k):
        game.published.append("attrs")

    async def _publish_metadata(text, **kwargs):
        game.published.append(("meta", text))

    game.publish_attributes = _publish_attributes
    game.publish_metadata = _publish_metadata
    game.events = []
    game.send_event_nowait = lambda kind, payload: game.events.append(
        (kind, payload)
    )
    game.instructed_replies = []
    game.instructed_reply = lambda text: game.instructed_replies.append(text)
    game.adjudications = []

    async def _adjudicate(steal_allowed=True):
        game.adjudications.append(steal_allowed)

    game.adjudicate = _adjudicate
    return game


def _arm_and_claim(game: LilyGame, question: dict) -> str:
    """Arm a question and register its delivery through the real
    register_delivery_claim path (so _note_mc_delivery_start runs), using
    the deterministic sheet as the spoken text so the structural claim
    lands (prompt + every option present)."""
    game.armed_question = dict(question)
    game.sk.start_question(game.armed_question)
    game._pre_window_segments = []
    game._pending_delivery_qnum = None
    game.expect_delivery()
    sheet = game.rendered_armed_question()
    result = game.register_delivery_claim(sheet)
    # Model the framework's speaking transition (actual delivery playout).
    game._active_delivery_started_at = 200.0
    game._active_delivery_ended_at = None
    if game._mc_delivery_qnum is not None:
        game._mc_delivery_started_at = 200.0
    return result


def _seg(text: str, start: float, label: str = "S1") -> dict:
    return {
        "text": text,
        "speaker_label": label,
        "segment_start_time": start,
        "segment_end_time": start + 0.6,
    }


# -- direct matcher sanity (the matcher was never the problem) ----------------

def test_matcher_accepts_option_text_and_letter():
    assert (
        lily_evaluation.lily_tier1_evaluate_question("Canberra", MC_QUESTION)[
            "verdict"
        ]
        == "correct"
    )
    assert (
        lily_evaluation.lily_tier1_evaluate_question("letter A", MC_QUESTION)[
            "verdict"
        ]
        == "correct"
    )
    assert (
        lily_evaluation.lily_tier1_evaluate_question("Sydney", MC_QUESTION)[
            "verdict"
        ]
        == "incorrect"
    )


# -- Layer 2 core: correct answer during options truncates + adjudicates ------

def test_correct_answer_during_options_truncates_and_adjudicates():
    game = _make_game()
    game.sk.bind_speaker("S1", "Rami")
    assert _arm_and_claim(game, MC_QUESTION) == "claimed_structural"
    qnum = game.sk.question_number
    assert game._mc_delivery_qnum == qnum
    # Stem: 6 words / 3.0 wps = 2.0s protected. The correct answer lands at
    # t=205 (well past the stem, during the options read).
    game._mc_delivery_started_at = 200.0

    async def scenario():
        aborted = game.mc_early_answer_check(
            _seg("Canberra", 205.0), now=205.0
        )
        await asyncio.sleep(0)  # drain the adjudicate ensure_future
        await asyncio.sleep(0)
        return aborted

    aborted = _run(scenario(), game)
    assert aborted is True
    # Layer 1: the read was halted (options 3-4 never air).
    assert game.session.interrupts == 1
    # The window opened early and the answerer scored.
    assert game.sk.answer_window_open is True
    cands = game.sk.ordered_candidates()
    assert cands and cands[0]["text"] == "Canberra"
    assert cands[0]["player"] == "Rami"
    assert game.adjudications == [False]  # instant Tier-1 fast path, no steal
    # MC delivery is no longer in flight.
    assert game._mc_delivery_qnum is None


def test_no_verbatim_replay_after_mc_barge():
    # JOINT with WS-3: the aborted read must not re-dispatch the delivery.
    game = _make_game()
    game.sk.bind_speaker("S1", "Rami")
    _arm_and_claim(game, MC_QUESTION)
    game._mc_delivery_started_at = 200.0

    async def scenario():
        aborted = game.mc_early_answer_check(_seg("Canberra", 205.0), now=205.0)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return aborted

    aborted = _run(scenario(), game)
    assert aborted is True
    # No re-dispatch of the read (no new instructed_reply / nudge queued):
    # the read is truncated and adjudicated, never replayed.
    assert game.instructed_replies == []
    assert game.sk.answer_window_open is True


def test_scores_even_if_framework_barge_released_the_claim_first():
    # Hardening: a framework barge can fire and release q_{N}_delivery
    # BEFORE the final transcript reaches the check. The answer is still
    # scored — the seed does not depend on the claim being present.
    game = _make_game()
    game.sk.bind_speaker("S1", "Rami")
    _arm_and_claim(game, MC_QUESTION)
    game._mc_delivery_started_at = 200.0
    key = f"q_{game.sk.question_number}_delivery"
    game.say_registry.release(key)  # framework interrupt got here first
    assert game.say_registry.state(key) is None

    async def scenario():
        aborted = game.mc_early_answer_check(_seg("Canberra", 205.0), now=205.0)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return aborted

    aborted = _run(scenario(), game)
    assert aborted is True
    cands = game.sk.ordered_candidates()
    assert cands and cands[0]["text"] == "Canberra"
    assert game.adjudications == [False]


# -- Layer 2: the stem stays protected ----------------------------------------

def test_stem_is_protected_early_answer_does_not_truncate():
    game = _make_game()
    game.sk.bind_speaker("S1", "Rami")
    _arm_and_claim(game, MC_QUESTION)
    qnum = game.sk.question_number
    game._mc_delivery_started_at = 200.0
    # Stem protected until t=202.0 (6 words / 3.0 wps). Answer at t=201.0.
    aborted = game.mc_early_answer_check(_seg("Canberra", 201.0), now=201.0)
    assert aborted is False
    assert game.session.interrupts == 0
    assert game.sk.answer_window_open is False
    # Still in flight — a later (post-stem) answer can still abort.
    assert game._mc_delivery_qnum == qnum


# -- SUPERSEDED by HOSTLOOP-001 C3b ------------------------------------------
#
# This slot used to hold `test_wrong_pick_during_options_does_not_truncate`,
# pinning "a wrong pick during the options read never truncates". WS-5 wanted
# that: truncating a read is a costly act and only a provably CORRECT answer
# earned it.
#
# Session B (lily-05BB92, 2026-08-12) is the bill for it. A resolved but WRONG
# pick was not "an answer" to any gate on the barge path, so it fell through
# to Y7's BARGE_IN_CANCEL: floor yielded, remaining choices killed, utterance
# never bound, nothing re-aired. The question ended half-delivered.
#
# C3b redraws the line at ANSWER-SHAPED rather than CORRECT: a resolved pick
# is a committed answer whether or not it is right, so it binds, ends the
# read, and goes to verdict. Correctness is adjudication's job, not the barge
# gate's. The stem protection, the feature toggle and the freeform rules that
# WS-5 also pinned are unchanged and still tested above/below.

def test_wrong_but_committed_pick_during_options_binds_and_truncates():
    """C3b: an answer-shaped WRONG pick ends the read and adjudicates."""
    game = _make_game()
    game.sk.bind_speaker("S1", "Rami")
    _arm_and_claim(game, MC_QUESTION)
    game._mc_delivery_started_at = 200.0

    async def scenario():
        aborted = game.mc_early_answer_check(_seg("Sydney", 205.0), now=205.0)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return aborted

    aborted = _run(scenario(), game)
    assert aborted is True
    assert game.session.interrupts == 1
    assert game.sk.answer_window_open is True
    cands = game.sk.ordered_candidates()
    assert cands and cands[0]["text"] == "Sydney"
    assert game.adjudications == [False]
    # The verdict itself stays adjudication's call — the pick is simply bound.
    assert (
        lily_evaluation.lily_tier1_evaluate_question("Sydney", MC_QUESTION)[
            "verdict"
        ]
        == "incorrect"
    )


def test_non_answer_speech_during_options_never_truncates():
    """C3b's other side: non-answer speech is not an answer, so it must not
    bind or truncate. (Its resume is C3c's job — pinned in the C3/C4 suite.)"""
    game = _make_game()
    game.sk.bind_speaker("S1", "Rami")
    _arm_and_claim(game, MC_QUESTION)
    game._mc_delivery_started_at = 200.0
    aborted = game.mc_early_answer_check(
        _seg("Like speaking at the.", 205.0), now=205.0
    )
    assert aborted is False
    assert game.session.interrupts == 0
    assert game.sk.answer_window_open is False


# -- feature toggle + freeform never arm the abort ----------------------------

def test_feature_off_never_aborts():
    game = _make_game()
    game.sk.bind_speaker("S1", "Rami")
    _arm_and_claim(game, MC_QUESTION)
    game._mc_delivery_started_at = 200.0
    os.environ["LILY_MC_ANSWER_ABORTS_READ"] = "off"
    try:
        aborted = game.mc_early_answer_check(_seg("Canberra", 205.0), now=205.0)
    finally:
        del os.environ["LILY_MC_ANSWER_ABORTS_READ"]
    assert aborted is False
    assert game.session.interrupts == 0
    assert game.sk.answer_window_open is False


def test_freeform_delivery_never_arms_mc_abort():
    game = _make_game()
    game.sk.bind_speaker("S1", "Rami")
    assert _arm_and_claim(game, FREEFORM_QUESTION) in (
        "claimed_structural",
        "claimed_core_sentence",
    )
    # No 4-choice set -> not an MC delivery -> nothing to truncate.
    assert game._mc_delivery_qnum is None
    aborted = game.mc_early_answer_check(_seg("Canberra", 205.0), now=205.0)
    assert aborted is False
    assert game.session.interrupts == 0


def test_correct_freeform_answer_aborts_read_and_adjudicates():
    """A one-word answer can barge into a freeform clue immediately."""
    game = _make_game()
    game.sk.bind_speaker("S1", "Rami")
    assert _arm_and_claim(game, FREEFORM_QUESTION) in (
        "claimed_structural",
        "claimed_core_sentence",
    )

    async def scenario():
        aborted = game.early_answer_check(
            _seg("Canberra", 205.0), now=205.0
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return aborted

    assert _run(scenario(), game) is True
    assert game.session.interrupts == 1
    assert game.sk.answer_window_open is True
    assert game.sk.ordered_candidates()[0]["text"] == "Canberra"
    assert game.adjudications == [False]


def test_freeform_chatter_does_not_abort_read():
    game = _make_game()
    _arm_and_claim(game, FREEFORM_QUESTION)
    assert game.early_answer_check(
        _seg("wait what was the category", 205.0), now=205.0
    ) is False
    assert game.session.interrupts == 0
    assert game.sk.answer_window_open is False


# -- P0-F: queued/pre-claim speech is never an answer -------------------------

def test_pre_claim_final_within_old_horizon_is_dropped():
    game = _make_game()
    game.sk.bind_speaker("S1", "Rami")
    # The player buzzes just BEFORE the delivery claim lands.
    game.armed_question = dict(MC_QUESTION)
    game.sk.start_question(game.armed_question)
    game._pre_window_segments = []
    key = f"q_{game.sk.question_number}_delivery"
    game.say_registry.claim(key)  # delivery registered (buffer precondition)
    game.note_recent_final(_seg("Canberra", 100.0), 100.0)
    # Even 1.5s later is ineligible: no question audio had started.
    game._backfill_prewindow_from_recent(now=101.5)
    assert game._pre_window_segments == []
    assert game._recent_finals == []


def test_pre_claim_final_beyond_T_is_dropped():
    game = _make_game()
    game.sk.bind_speaker("S1", "Rami")
    game.armed_question = dict(MC_QUESTION)
    game.sk.start_question(game.armed_question)
    game._pre_window_segments = []
    key = f"q_{game.sk.question_number}_delivery"
    game.say_registry.claim(key)
    game.note_recent_final(_seg("Canberra", 100.0), 100.0)
    # note_recent_final already trims to the horizon around its OWN ts, but
    # the backfill cutoff is what gates capture: a claim 5s later (> 3.0s
    # default) drops the stale final.
    game._recent_finals = [(100.0, _seg("Canberra", 100.0))]
    game._backfill_prewindow_from_recent(now=105.0)
    assert game._pre_window_segments == []


def test_backfill_respects_zero_horizon():
    game = _make_game()
    game.sk.bind_speaker("S1", "Rami")
    game.armed_question = dict(MC_QUESTION)
    game.sk.start_question(game.armed_question)
    game._pre_window_segments = []
    key = f"q_{game.sk.question_number}_delivery"
    game.say_registry.claim(key)
    game._recent_finals = [(100.0, _seg("Canberra", 100.0))]
    os.environ["LILY_BUZZ_PREWINDOW_SECONDS"] = "0"
    try:
        game._backfill_prewindow_from_recent(now=100.1)
    finally:
        del os.environ["LILY_BUZZ_PREWINDOW_SECONDS"]
    assert game._pre_window_segments == []
    assert game._recent_finals == []


# -- open_window clears the MC-in-flight flag (normal full-playout path) -------

def test_open_window_clears_mc_flag():
    game = _make_game()
    game.sk.bind_speaker("S1", "Rami")
    _arm_and_claim(game, MC_QUESTION)
    assert game._mc_delivery_qnum is not None

    async def scenario():
        game.open_window(duration=30.0)
        await asyncio.sleep(0)

    _run(scenario(), game)
    assert game._mc_delivery_qnum is None


# -- config defaults ----------------------------------------------------------

def test_config_defaults():
    assert lily_config.mc_answer_aborts_read() is True
    assert lily_config.buzz_prewindow_seconds() == 3.0
    assert lily_config.mc_stem_protect_words_per_second() == 3.0
