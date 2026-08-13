"""The double verdict (live 2026-08-12 lily-639007): every answered
question aired TWO verdict emissions — the ORGANIC reply (17s late,
"you're at three" FABRICATED) and adjudicate's keyed ledger-true
composite ("that's two for you"). Root cause: both existing suppressor
checks are flow-dependent, and W4's relaxed beat-close adjudicates at
the transcript seam and CLOSES the window ~2s before the framework
commits the turn — so at commit, the exact-text mark could miss and the
window-liveness prehook always declined. Ownership now follows the DATA:
a turn whose text the scorekeeper consumed as an answer candidate
belongs to adjudication, whatever the event ordering.
"""

import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from livekit.agents import StopResponse

from lily_scorekeeper import LilyScorekeeper
from lily_agent import LilyAgent
from test_desync_fixture import FEMUR_QUESTION, _arm_question, _make_game


def _sk():
    sk = LilyScorekeeper("dbl-verdict")
    sk.bind_speaker("S1", "Rami")
    sk.start_question({
        "prompt": "What famous Greek mountain was home to the twelve "
                  "Olympian gods?",
        "canonical_answer": "Mount Olympus",
        "acceptable_answers": ["olympus", "mount olympus"],
    })
    return sk


def test_the_live_shape_candidate_recorded_then_window_closed():
    """The exact lily-639007 ordering: candidate lands, beat closes,
    window gone — the commit-time check must still own the turn."""
    sk = _sk()
    sk.open_answer_window(duration=15.0)
    sk.on_transcript_segment(
        text="Olympus.", speaker_label="S1", is_final=True,
        now=time.time(), segment_start_time=time.time(),
        segment_end_time=time.time() + 1,
    )
    sk.close_answer_window()  # the relaxed beat-close, before commit
    assert not sk.answer_window_open
    assert sk.recent_answer_text_matches("Olympus.") is True


def test_non_answer_chatter_is_never_owned():
    sk = _sk()
    sk.open_answer_window(duration=15.0)
    sk.on_transcript_segment(
        text="Olympus.", speaker_label="S1", is_final=True,
        now=time.time(), segment_start_time=time.time(),
        segment_end_time=time.time() + 1,
    )
    assert sk.recent_answer_text_matches(
        "why are you repeating yourself"
    ) is False


def test_ownership_expires():
    sk = _sk()
    sk.note_recent_answer_text("Olympus.", now=1_000.0)
    assert sk.recent_answer_text_matches("Olympus.", now=1_010.0) is True
    assert sk.recent_answer_text_matches("Olympus.", now=1_040.0) is False


def _prehook_message(text: str):
    return type("Message", (), {"content": [text]})()


def _drive_prehook(game, text: str) -> bool:
    """Drive the ACTUAL on_user_turn_completed prehook. Returns True when it
    owned the turn (raised StopResponse -> organic reply suppressed)."""
    agent = LilyAgent.__new__(LilyAgent)
    agent._game = game

    async def scenario() -> bool:
        try:
            await agent.on_user_turn_completed(None, _prehook_message(text))
        except StopResponse:
            return True
        return False

    return asyncio.run(scenario())


# -- The live race (lily-A9B757, 2026-08-13): the organic reply reached the
# prehook BEFORE the transcript callback recorded the answer candidate, so
# recent_answer_text_matches was still False and the organic verdict aired
# alongside the deterministic sheet. Ownership must not depend on that
# ordering — it is decided inline from the armed question + answer state.


def test_prehook_owns_wrong_answer_shaped_turn_before_candidate_recorded():
    """A wrong-but-answer-shaped in-window turn is still adjudication's to
    rule on ('no point this time' fires deterministically); the organic
    reply must not narrate a second verdict over it. Window OPEN, candidate
    NOT yet recorded — the data-side check cannot see it."""
    game = _make_game()
    game.sk.bind_speaker("S1", "Rami")
    _arm_question(game, FEMUR_QUESTION)
    game.sk.open_answer_window(duration=30.0, now=1_000.0)
    assert game.sk.recent_answer_text_matches("the tibia") is False
    assert _drive_prehook(game, "the tibia") is True


def test_prehook_owns_correct_answer_when_window_not_open_but_delivery_current():
    """The live escape shape: a correct buzz lands in the discharge / W4
    beat-close microgap — window not open at prehook time, candidate not
    recorded — yet the current question's delivery has aired and it has not
    been adjudicated. Ownership rides the delivery, not the window boolean."""
    game = _make_game()
    game.sk.bind_speaker("S1", "Rami")
    _arm_question(game, FEMUR_QUESTION)
    qnum = game.sk.question_number
    game._active_delivery_qnum = qnum
    game._active_delivery_started_at = 1_000.0
    game._active_delivery_ended_at = 1_002.0  # delivery finished, window not (re)open
    assert game.sk.answer_window_open is False
    assert game.sk.recent_answer_text_matches("femur") is False
    assert _drive_prehook(game, "femur") is True


def test_prehook_does_not_own_a_non_answer_question_during_the_window():
    """Over-suppression guard: a clarify/banter turn that is NOT answer-shaped
    ('what was the category again?') stays with the conversational lane even
    with the window open — false suppression is worse than the double."""
    game = _make_game()
    game.sk.bind_speaker("S1", "Rami")
    _arm_question(game, FEMUR_QUESTION)
    game.sk.open_answer_window(duration=30.0, now=1_000.0)
    assert game.correct_answer_owns_user_turn("what was the category again?") is False


def test_prehook_ignores_a_queued_but_unaired_delivery():
    """A delivery still queued (never on air) cannot own speech — pins the
    same boundary as test_recognition_variety, now through the prehook."""
    game = _make_game()
    game.sk.bind_speaker("S1", "Rami")
    _arm_question(game, FEMUR_QUESTION)
    qnum = game.sk.question_number
    game._active_delivery_qnum = qnum
    game._active_delivery_started_at = None  # queued TTS, not on air
    assert game.sk.answer_window_open is False
    assert _drive_prehook(game, "femur") is False


def test_start_composite_never_rewelcomes_after_the_late_beat():
    """The second welcome-back (13:56:44, 11s after the recognition beat):
    'if you haven't done one yet' left the decision to the model. The
    fired flag now decides in code."""
    import inspect

    import lily_agent

    src = inspect.getsource(lily_agent.LilyGame.start_game)
    assert "_late_recognition_fired" in src
    assert "do NOT welcome the table back again" in src
