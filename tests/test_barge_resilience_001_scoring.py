"""WO-LILY-BARGE-RESILIENCE-001 — scoring integrity (P0) and resume ordering (P2).

Rows pinned here:
  * P0-3 — an answer that NAMES A CHOICE NOT YET READ ALOUD ("D" / "the last
    one" while only A-B aired) scores against the FULL armed sheet, never the
    aired prefix.
  * P0-4 — a clipped/delayed question whose answer is captured PRE_WINDOW and
    replayed, with the verbal confirm landing within grace, scores EXACTLY once
    and reconciliation reports attempts == scored (no PRE_WINDOW_REPLAY +
    LATE_WITHIN_GRACE 0-score miss — the 12:28 Q3 "prostate" defect).
  * R3 — barge-to-ask mid-delivery: she answers the interjected question, THEN
    the pending read resumes; the answer turn does not confirm the delivery and
    the read is not lost.

Idiom reuses test_mc_answer_aborts_read's offline fixture (LilyGame via
__new__, stubbed adjudicate + publishes, fake session recording interrupt()).
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_evaluation  # noqa: E402
from test_mc_answer_aborts_read import (  # noqa: E402
    _arm_and_claim,
    _make_game,
    _run,
    _seg,
)
from test_desync_fixture import (  # noqa: E402
    FEMUR_QUESTION,
    _arm_question,
)
from test_desync_fixture import _make_game as _make_scoring_game  # noqa: E402
from test_desync_fixture import _run as _run_scoring  # noqa: E402

# An MCQ whose CORRECT answer is the LAST choice (letter D). The read airs the
# stem + A-B; C-D are never voiced when the player calls the answer.
LAST_CHOICE_MC = {
    "id": "q_last_choice",
    "category": "academic",
    "difficulty_tier": 2,
    "prompt": "Name the capital city of Australia.",  # 6 words -> 2.0s stem
    "canonical_answer": "Perth",
    "acceptable_answers": ["perth"],
    "reveal_color": "",
    "choices": ["Canberra", "Sydney", "Melbourne", "Perth"],
}


# -- P0-3: an unread choice scores against the full armed sheet ----------------


def test_p0_3_matcher_resolves_an_unread_choice_against_full_sheet():
    # The scorer reads the ARMED question (all four choices), not what aired,
    # so the letter/position of a choice never read aloud still resolves.
    for said in ("letter D", "D", "the last one", "Perth"):
        assert (
            lily_evaluation.lily_tier1_evaluate_question(said, LAST_CHOICE_MC)[
                "verdict"
            ]
            == "correct"
        ), said


def test_p0_3_answer_naming_unread_choice_binds_and_scores_correct():
    # Live shape: only A-B have aired when the player answers "D". The read is
    # truncated, the pick binds as a candidate, and it adjudicates CORRECT —
    # scored against the full armed sheet, not the aired A-B prefix.
    game = _make_game()
    game.sk.bind_speaker("S1", "Rami")
    assert _arm_and_claim(game, LAST_CHOICE_MC) == "claimed_structural"
    game._mc_delivery_started_at = 200.0
    # Only A-B aired: the unread tail (C-D) is still owed by the read.
    assert game.rendered_armed_choices_from(2)  # C-D not yet read

    async def scenario():
        aborted = game.mc_early_answer_check(_seg("letter D", 205.0), now=205.0)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return aborted

    assert _run(scenario(), game) is True
    assert game.session.interrupts == 1
    assert game.sk.answer_window_open is True
    cands = game.sk.ordered_candidates()
    assert cands and cands[0]["player"] == "Rami"
    # The bound pick resolves against the FULL sheet — correct, though C-D
    # never aired:
    assert (
        lily_evaluation.lily_tier1_evaluate_question(
            cands[0]["text"], LAST_CHOICE_MC
        )["verdict"]
        == "correct"
    )
    assert game.adjudications == [False]  # instant Tier-1 fast path


# -- P0-4: clipped-question grace scoring + reconciliation --------------------


def _prime_clipped_delivery(game):
    """A fusion-clipped question: the delivery CLAIM lands (the question was
    shown / dispatched) but the voiced read was clipped, and its playout is
    modelled as in-flight so an early card-read answer buffers."""
    game.arm_next_question = lambda: False
    game.start_prefetch = lambda: None
    game.sk.bind_speaker("S1", "Rami")
    _arm_question(game, FEMUR_QUESTION)
    game.expect_delivery()
    sheet = game.rendered_armed_question()
    game.register_delivery_claim(sheet)
    game._active_delivery_started_at = 1000.0
    game._active_delivery_ended_at = None


def test_p0_4_clipped_prewindow_answer_scores_exactly_once_reconcile_clean():
    # The 12:28 Q3 shape: the question is shown but its read is clipped, the
    # player reads the card and answers PRE-WINDOW, the verbal confirm lands
    # within grace. The buffered answer replays at window open, the P0-4 belt
    # adjudicates it, and it scores EXACTLY once — reconciliation reports every
    # captured attempt adjudicated (no PRE_WINDOW_REPLAY + LATE_WITHIN_GRACE
    # 0-score miss).
    game = _make_scoring_game()
    _prime_clipped_delivery(game)
    qnum = game.sk.question_number
    # Player reads the card and answers before the window opened:
    game.buffer_pre_window_answer({
        "text": "the femur", "speaker_label": "S1",
        "segment_start_time": 1001.0, "segment_end_time": 1001.6,
    })
    assert game._pre_window_segments  # buffered, not lost

    async def scenario():
        # The clipped read finally reaches playout: window opens (late),
        # buffered answer replays, the belt adjudicates it into a clean window.
        game.open_window(duration=30.0)
        await asyncio.sleep(0)
        # The verbal confirm lands within grace — a second final from the same
        # player, folded into the SAME candidate (never a second score).
        game.sk.on_transcript_segment(
            text="The femur.", speaker_label="S1", is_final=True,
            now=1002.0, segment_start_time=1002.0, segment_end_time=1002.6,
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    _run_scoring(scenario(), game)

    # Scored exactly once, correct:
    assert game.sk.players["Rami"]["score"] == 1
    # Reconciliation: every captured attempt for this question reached the
    # ruling — attempts == scored, no drop.
    assert game.sk.reconcile_attempts_scored(qnum) == []


def test_p0_4_reconcile_flags_a_captured_answer_that_was_dropped():
    # The belt's teeth: an answer captured for a question that then vanishes
    # from the candidate set before adjudication (replaced / lost on a
    # clipped-late timing) is reported by reconciliation, so a scored-0 miss is
    # loud in its own session instead of silent.
    game = _make_scoring_game()
    game.sk.bind_speaker("S1", "Rami")
    _arm_question(game, FEMUR_QUESTION)
    qnum = game.sk.question_number
    game.sk.open_answer_window(duration=30.0, now=2000.0)
    game.sk.on_transcript_segment(
        text="the femur", speaker_label="S1", is_final=True,
        now=2001.0, segment_start_time=2001.0, segment_end_time=2001.6,
    )
    # Sanity: the answer was captured.
    assert game.sk.reconcile_attempts_scored(qnum) == []
    # Now simulate the drop — the candidate is lost before adjudication:
    game.sk.answer_candidates = {}
    unscored = game.sk.reconcile_attempts_scored(qnum)
    assert len(unscored) == 1


# -- P2 / R3: barge-to-ask ordering — answer first, THEN resume the read ------

import lily_config  # noqa: E402
from test_mc_answer_aborts_read import MC_QUESTION  # noqa: E402


def _owe_a_barge_cut_read(game):
    """A question read cut by a barge-to-ask: armed, claimed, unconfirmed,
    marked owed — the C3d 'still owed a resume' state."""
    game.sk.bind_speaker("S1", "Rami")
    _arm_and_claim(game, MC_QUESTION)
    qnum = game.sk.question_number
    game._delivery_barge_cut_qnum = qnum
    assert game._question_barge_resume_still_owed(qnum) is True
    return qnum


def test_r3_resume_is_deferred_until_she_finishes_answering(monkeypatch):
    # She is answering the interjected question ("what are the rules?") for the
    # first few grace ticks (host_speaking), then stops. The resume must fire
    # ONLY after she finishes — sequenced after her answer, never over it.
    game = _make_game()
    qnum = _owe_a_barge_cut_read(game)
    calls = []
    game.mcq_barge_resume = lambda now: calls.append(now) or True

    ticks = {"n": 0}

    def _grace():
        ticks["n"] += 1
        if ticks["n"] >= 3:            # her answer finishes on the 3rd tick
            game.sk.host_speaking = False
        return 0.0

    monkeypatch.setattr(lily_config, "cut_recovery_grace", _grace)
    game.sk.host_speaking = True       # she is on air, replying

    _run(game._question_barge_resume_watch(qnum), game)

    # Resumed exactly once, and only after she stopped speaking:
    assert len(calls) == 1
    assert ticks["n"] >= 3


def test_r3_resume_fires_immediately_when_she_is_not_speaking(monkeypatch):
    # Control: an ordinary (non-ask) barge — she is not on the air — resumes on
    # the first grace tick, no needless deferral.
    game = _make_game()
    qnum = _owe_a_barge_cut_read(game)
    calls = []
    game.mcq_barge_resume = lambda now: calls.append(now) or True
    monkeypatch.setattr(lily_config, "cut_recovery_grace", lambda: 0.0)
    game.sk.host_speaking = False

    _run(game._question_barge_resume_watch(qnum), game)

    assert len(calls) == 1


def test_r3_resume_does_not_fire_when_the_answer_bound_meanwhile(monkeypatch):
    # If her "answer" turn actually bound an answer (or one arrived) the read is
    # no longer owed — the resume stands down entirely, never doubling a read.
    game = _make_game()
    qnum = _owe_a_barge_cut_read(game)
    calls = []
    game.mcq_barge_resume = lambda now: calls.append(now) or True
    monkeypatch.setattr(lily_config, "cut_recovery_grace", lambda: 0.0)
    game.sk.host_speaking = False
    # An answer bound in the window -> no longer owed:
    game.sk.open_answer_window(duration=30.0, now=500.0)
    game.sk.on_transcript_segment(
        text="Canberra", speaker_label="S1", is_final=True,
        now=501.0, segment_start_time=501.0, segment_end_time=501.6,
    )
    assert game._question_barge_resume_still_owed(qnum) is False

    _run(game._question_barge_resume_watch(qnum), game)

    assert calls == []


# -- P0 regression pins (SOLID rows — capture is decoupled from the cancel) ----


def test_p0_1_freeform_early_answer_buffered_and_scored_despite_cancel():
    # P0-1: a correct answer shouted mid-stem of a freeform read is buffered,
    # replayed at window open, and scored correct via Tier-1 — the barge-cancel
    # of her read never drops it (capture is independent of the cancel).
    game = _make_scoring_game()
    _prime_clipped_delivery(game)  # armed freeform + delivery claimed, in flight
    game.buffer_pre_window_answer({
        "text": "the femur", "speaker_label": "S1",
        "segment_start_time": 1001.0, "segment_end_time": 1001.6,
    })
    assert game._pre_window_segments

    async def scenario():
        # The read is barge-cancelled (interrupted) — then its window opens and
        # the buffered answer replays and scores.
        game.on_agent_speech_finished(
            game.rendered_armed_question(), interrupted=True, failed=False,
        )
        game.open_window(duration=30.0)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    _run_scoring(scenario(), game)
    assert game.sk.players["Rami"]["score"] == 1


def test_p0_2_mcq_correct_after_choice_b_truncates_no_reoffer():
    # P0-2: a correct answer after choice B of 4 truncates the remaining read
    # and adjudicates; the unread choices C/D are NOT re-offered (no nudge /
    # resume dispatched — the read ends, it is not restarted).
    game = _make_game()
    game.sk.bind_speaker("S1", "Rami")
    _arm_and_claim(game, MC_QUESTION)  # canonical is choice A (Canberra)
    game._mc_delivery_started_at = 200.0

    async def scenario():
        aborted = game.mc_early_answer_check(_seg("Canberra", 205.0), now=205.0)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return aborted

    assert _run(scenario(), game) is True
    assert game.session.interrupts == 1              # read halted
    assert game.adjudications == [False]             # adjudicated
    assert game.instructed_replies == []             # no C/D re-offer
    assert game._delivery_barge_cut_qnum is None     # not owed a resume


def test_p0_5_barge_cut_verdict_leaves_committed_score_intact():
    # P0-5: the ruling commits BEFORE the verdict speaks (T4). A barge that cuts
    # the verdict mid-word cannot change the committed score — reair_cut_verdict
    # re-airs the words, the ledger row is untouched.
    game = _make_scoring_game()
    game.arm_next_question = lambda: False
    game.start_prefetch = lambda: None
    game.sk.bind_speaker("S1", "Rami")
    _arm_question(game, FEMUR_QUESTION)
    game.sk.open_answer_window(duration=30.0, now=4000.0)
    game.sk.on_transcript_segment(
        text="the femur", speaker_label="S1", is_final=True,
        now=4002.0, segment_start_time=4002.0, segment_end_time=4002.6,
    )

    async def scenario():
        await game.adjudicate(steal_allowed=True)
        await asyncio.sleep(0)

    _run_scoring(scenario(), game)
    committed = game.sk.players["Rami"]["score"]
    assert committed == 1
    # The verdict beat is cut and re-aired: the committed score is unchanged.
    game.reair_cut_verdict(["q_1_reveal"])
    assert game.sk.players["Rami"]["score"] == committed


def test_p0_6_barge_cut_correct_answer_still_wins_over_a_completed_wrong_one():
    # P0-6: two players. The CORRECT answer (S1) is barge-cut mid-answer but was
    # buffered; the WRONG answer (S2) completes. The buffered correct utterance
    # still binds and scores; the wrong one does not win by completing.
    game = _make_scoring_game()
    _prime_clipped_delivery(game)     # armed + delivery in flight
    game.sk.bind_speaker("S2", "Chris")
    # S1's correct answer (barge-cut) and S2's wrong answer both land early:
    game.buffer_pre_window_answer({
        "text": "the femur", "speaker_label": "S1",
        "segment_start_time": 1001.0, "segment_end_time": 1001.6,
    })
    game.buffer_pre_window_answer({
        "text": "the tibia", "speaker_label": "S2",
        "segment_start_time": 1001.4, "segment_end_time": 1002.4,
    })

    async def scenario():
        game.open_window(duration=30.0)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    _run_scoring(scenario(), game)
    assert game.sk.players["Rami"]["score"] == 1     # correct, buffered, wins
    assert game.sk.players["Chris"]["score"] == 0     # wrong, no win by finishing
