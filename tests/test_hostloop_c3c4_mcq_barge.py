"""WO-LILY-HOSTLOOP-001 C3+C4 — MCQ barge-in binding/resume + answer-window
integrity, offline.

The two live failures these clauses close, and the mechanisms that already
existed but did not cover them:

  Session B (lily-05BB92, 2026-08-12) — during phase=question /
  delivery=active the answer window was CLOSED for the whole options read
  (it opened only at full-choices playout completion / CONFIRMED), while
  Y7's BARGE_IN_CANCEL policy yielded the floor with no resume. A barge
  therefore killed the choices, the utterance never bound, and nothing
  re-aired. WS-5's mc_early_answer_check was the one path that could bind
  mid-read, but it required verdict == "correct", so a resolved-but-WRONG
  pick ("B", "Sydney") fell straight through to the cancel.

  Session A (2026-08-12 04:50 UTC) — the complaint fragment "Like speaking
  at the." was scored as the q6 answer (verdict=incorrect). The pre-window
  buffer took ANY final overlapping delivery playout, and the replay at
  window open passes assume_in_window=True, which by design bypasses both
  the WS-10 sanity gate and window membership — so anything buffered became
  a candidate by construction.

C3a  window arms at CORE-QUESTION completion, options keep airing into it.
C3b  an ANSWER-SHAPED barge binds (right or wrong), truncates, adjudicates.
C3c  a NON-answer-shaped barge cancels TTS and RESUMES from the interrupted
     choice — the deliberate carve-out from Y7, scoped to question phase.
C3d  INVARIANT: a question never ends half-aired with no answer bound and no
     resume pending.
C4   adjudication only ever consumes answer-shaped speech in an open (or
     C3a-extended) window; everything else stays conversation.

Fixture idiom follows tests/test_mc_answer_aborts_read.py (LilyGame via
__new__, stubbed adjudicate/publishes, fake session recording interrupt()).
"""

import asyncio
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_audeering_consumers
import lily_config
import lily_evaluation
import lily_say_gate
import lily_speech_delivery
from lily_agent import LilyGame
from lily_scorekeeper import LilyScorekeeper

# Stem is 6 words; at the default 3.0 wps the core question is estimated to
# finish 2.0s after playout start. Each choice is 1 word + 1 letter label = 2
# words, so with playout starting at t=200.0 the option cursor runs:
#   choice A (index 0): words 6-8   -> t 202.000 .. 202.667
#   choice B (index 1): words 8-10  -> t 202.667 .. 203.333   <- "choice 2"
#   choice C (index 2): words 10-12 -> t 203.333 .. 204.000
#   choice D (index 3): words 12-14 -> t 204.000 .. 204.667
CHOICES = ["Canberra", "Sydney", "Melbourne", "Perth"]
MC_QUESTION = {
    "id": "q_4242",
    "category": "academic",
    "difficulty_tier": 2,
    "prompt": "Name the capital city of Australia.",  # 6 words
    "canonical_answer": "Canberra",
    "acceptable_answers": ["canberra"],
    "reveal_color": "",
    "choices": list(CHOICES),
}
FREEFORM_QUESTION = {
    "id": "q_9001",
    "category": "academic",
    "difficulty_tier": 2,
    "prompt": "What is the capital city of Australia?",
    "canonical_answer": "Canberra",
    "acceptable_answers": ["canberra"],
    "reveal_color": "",
}

PLAYOUT_START = 200.0
# A moment strictly inside choice 2 (index 1) and past stem protection.
AT_CHOICE_2 = 203.0
# Session A's fragment, verbatim.
SESSION_A_FRAGMENT = "Like speaking at the."


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
    game = LilyGame.__new__(LilyGame)
    game.sk = LilyScorekeeper("hostloop-c3c4-fixture")
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game.group_id = "grp_c3c4"
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
    game._pending_delivery_qnum = None
    game._mc_delivery_qnum = None
    game._mc_delivery_started_at = None
    game._mc_delivery_stem_words = 0
    game._active_delivery_qnum = None
    game._active_delivery_started_at = None
    game._active_delivery_ended_at = None
    game._recent_finals = []
    game._delivery_stop_sticky = False
    game._hold_active = False
    # C3 state under test.
    game._delivery_barge_cut_qnum = None
    game._pending_delivery_resume = None
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
    # gated_say is the real dispatch surface; record what the resume asks for.
    game.said = []

    def _gated_say(key, act, instructions, source=None, **kwargs):
        game.said.append(
            {"key": key, "act": act, "instructions": instructions,
             "source": source}
        )
        return True

    game.gated_say = _gated_say
    game.deterministic_replies = []
    game.mark_deterministic_reply = (
        lambda text: game.deterministic_replies.append(text)
    )
    return game


def _arm_and_claim(game: LilyGame, question: dict) -> str:
    """Arm + register the delivery through the real register_delivery_claim
    path (so _note_mc_delivery_start runs), then model the framework's
    speaking transition at PLAYOUT_START."""
    game.armed_question = dict(question)
    game.sk.start_question(game.armed_question)
    game._pre_window_segments = []
    game._pending_delivery_qnum = None
    game.expect_delivery()
    result = game.register_delivery_claim(game.rendered_armed_question())
    game._active_delivery_qnum = game.sk.question_number
    game._active_delivery_started_at = PLAYOUT_START
    game._active_delivery_ended_at = None
    if game._mc_delivery_qnum is not None:
        game._mc_delivery_started_at = PLAYOUT_START
    return result


def _seg(text: str, start: float = AT_CHOICE_2, label: str = "S1") -> dict:
    return {
        "text": text,
        "speaker_label": label,
        "segment_start_time": start,
        "segment_end_time": start + 0.6,
    }


def _barged(game: LilyGame) -> None:
    """Model the framework barge: Y7 classified the cut as a deliberate
    barge-in and on_agent_speech_finished marked the question as owing
    either a binding or a resume."""
    game.note_question_barge_cut(game.sk.question_number)


# ===========================================================================
# THE WALKED EXAMPLE — an MCQ barge at choice 2, both paths.
# ===========================================================================

def test_walked_example_barge_at_choice_2_binds_when_answer_shaped():
    """(i) Answer-shaped barge at choice 2 -> binds, skips the rest, verdict.

    The table has heard the stem, "A) Canberra", and is part-way into
    "B) Sydney" when a player says "Sydney". Under WS-5 this was verdict
    != "correct" and so bound nothing (Session B). Now it binds.
    """
    game = _make_game()
    game.sk.bind_speaker("S1", "Rami")
    _arm_and_claim(game, MC_QUESTION)
    qnum = game.sk.question_number
    # Precondition: we really are inside choice 2 (index 1).
    assert game._mc_choice_airing_index(AT_CHOICE_2) == 1
    _barged(game)

    async def scenario():
        bound = game.early_answer_check(_seg("Sydney"), now=AT_CHOICE_2)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return bound

    bound = _run(scenario(), game)

    assert bound is True                        # the read ended on an answer
    assert game.session.interrupts == 1         # remaining choices truncated
    assert game.sk.answer_window_open is True
    cands = game.sk.ordered_candidates()
    assert cands and cands[0]["text"] == "Sydney"   # the utterance BOUND
    assert game.adjudications == [False]            # proceeded to verdict
    # C3d: nothing is owed — the question was answered, not abandoned.
    assert game._question_barge_resume_still_owed(qnum) is False
    # No RESUME dispatched. (HOSTLOOP-001 C6 amendment: `said` is no longer
    # empty here — the same binding now also fires the instant receipt — so
    # the pin names the act it was always about instead of counting
    # dispatches.)
    assert [s for s in game.said if s["act"] == "question_nudge"] == []
    # C6: and the bound answer got its word inside the same tick, from the
    # deterministic lane, with no LLM in the path.
    receipts = [s for s in game.said if s["act"] == "answer_receipt"]
    assert len(receipts) == 1
    assert game.answer_receipt_aired_for(qnum) == lily_say_gate.LILY_RECEIPT_INCORRECT


def test_walked_example_barge_at_choice_2_resumes_when_not_answer_shaped():
    """(ii) Non-answer barge at choice 2 -> TTS cancelled, read RESUMES from
    choice 2. Choices C and D were never aired and are still owed."""
    game = _make_game()
    game.sk.bind_speaker("S1", "Rami")
    _arm_and_claim(game, MC_QUESTION)
    qnum = game.sk.question_number
    assert game._mc_choice_airing_index(AT_CHOICE_2) == 1
    _barged(game)
    # Before: the question is half-aired and owes something (C3d).
    assert game._question_barge_resume_still_owed(qnum) is True

    bound = game.early_answer_check(_seg(SESSION_A_FRAGMENT), now=AT_CHOICE_2)

    assert bound is False                       # nothing bound
    assert game.sk.ordered_candidates() == []   # and nothing scoreable
    assert game._pre_window_segments == []      # C4: never even buffered
    assert game.session.interrupts == 1         # TTS cancelled
    # RESUMED from choice 2 (index 1) — B, C, D; never the stem, never A.
    assert len(game.said) == 1
    resume = game.said[0]
    assert resume["act"] == "question_nudge"
    assert resume["source"] == "mcq_barge_resume"
    assert "B) Sydney" in resume["instructions"]
    assert "C) Melbourne" in resume["instructions"]
    assert "D) Perth" in resume["instructions"]
    assert "A) Canberra" not in resume["instructions"]
    assert "capital city of Australia" not in resume["instructions"]
    # The verbatim text tts_node will speak is staged and one-shot.
    staged = game.take_pending_delivery_resume()
    assert staged == "B) Sydney\nC) Melbourne\nD) Perth"
    assert game.take_pending_delivery_resume() is None
    # C3d: the obligation is discharged — a resume is out.
    assert game._question_barge_resume_still_owed(qnum) is False


# ===========================================================================
# C3a — the window arms at CORE-QUESTION completion, not at full choices.
# ===========================================================================

def test_core_completion_delay_is_the_stem_estimate_not_full_playout():
    game = _make_game()
    _arm_and_claim(game, MC_QUESTION)
    # 6 stem words / 3.0 wps = 2.0s after playout start.
    assert lily_config.mc_stem_protect_words_per_second() == 3.0
    assert game.core_completion_delay(PLAYOUT_START) == 2.0
    # Measured from playout start, and never negative.
    assert game.core_completion_delay(PLAYOUT_START + 0.5) == 1.5
    assert game.core_completion_delay(PLAYOUT_START + 99) == 0.0


def test_core_completion_arm_opens_window_while_options_still_read():
    """The point of C3a: an OPEN window with the option read still in
    flight. That state was previously unrepresentable, which is exactly why
    a barge during the choices could not bind."""
    game = _make_game()
    _arm_and_claim(game, MC_QUESTION)
    qnum = game.sk.question_number
    assert game.sk.answer_window_open is False
    assert game._core_completion_window_should_arm(qnum) is True

    _run(_open_core(game), game)

    assert game.sk.answer_window_open is True
    # ... and the read is STILL in flight, so C3b/C3c can act on it.
    assert game._mc_delivery_qnum == qnum
    assert game._mc_delivery_started_at == PLAYOUT_START
    assert game._active_delivery_qnum == qnum
    assert game._mc_choice_airing_index(AT_CHOICE_2) == 1


async def _open_core(game):
    game.open_window(core_completion=True)


def test_ordinary_window_open_still_clears_the_in_flight_read_markers():
    """The WS-5 clear is untouched for every non-core-completion open."""
    game = _make_game()
    _arm_and_claim(game, MC_QUESTION)

    _run(_open_plain(game), game)

    assert game.sk.answer_window_open is True
    assert game._mc_delivery_qnum is None
    assert game._active_delivery_qnum is None
    assert game._active_delivery_started_at is None


async def _open_plain(game):
    game.open_window()


def test_core_completion_arm_stands_down_when_no_longer_applicable():
    game = _make_game()
    _arm_and_claim(game, MC_QUESTION)
    qnum = game.sk.question_number

    game._delivery_stop_sticky = True
    assert game._core_completion_window_should_arm(qnum) is False
    game._delivery_stop_sticky = False

    game._adjudicating = True
    assert game._core_completion_window_should_arm(qnum) is False
    game._adjudicating = False

    assert game._core_completion_window_should_arm(qnum + 1) is False

    game._mc_delivery_qnum = None      # read already ended
    assert game._core_completion_window_should_arm(qnum) is False


def test_answer_shaped_barge_binds_inside_the_c3a_open_window():
    """During the C3a span the scorekeeper has already recorded the answer as
    an in-window candidate, so the barge path only truncates — it must not
    re-seed the utterance and double-count it."""
    game = _make_game()
    game.sk.bind_speaker("S1", "Rami")
    _arm_and_claim(game, MC_QUESTION)
    _run(_open_core(game), game)
    assert game.sk.answer_window_open is True
    # The live in-window path records the candidate ahead of the barge fork
    # (assume_in_window models spoken-time membership; the fixture's segment
    # clock is synthetic and does not share the window's wall clock).
    game.sk.on_transcript_segment(
        is_final=True, now=AT_CHOICE_2, assume_in_window=True, **_seg("Sydney")
    )
    assert len(game.sk.ordered_candidates()) == 1

    async def scenario():
        bound = game.early_answer_check(_seg("Sydney"), now=AT_CHOICE_2)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return bound

    bound = _run(scenario(), game)

    assert bound is True
    assert game.session.interrupts == 1
    assert game._pre_window_segments == []          # no re-seed
    assert len(game.sk.ordered_candidates()) == 1   # no double-count
    assert game.adjudications == [False]


# ===========================================================================
# C3b — answer-shaped, by every route the clause names.
# ===========================================================================

def test_answer_shape_covers_letters_nato_positions_and_choice_text():
    q = MC_QUESTION
    for text in (
        "B", "letter B", "option c", "d",            # choice letters A-D
        "bravo", "charlie", "delta", "alpha", "alfa",  # NATO spellings
        "the second one", "number three",            # positions
        "Sydney", "Melbourne", "Canberra",           # choice text
    ):
        assert lily_evaluation.lily_answer_shaped(text, q) is True, text
    for text in (
        SESSION_A_FRAGMENT, "wait what?", "can you repeat that?",
        "uhh", "I think", "",
    ):
        assert lily_evaluation.lily_answer_shaped(text, q) is False, text


def test_nato_spelling_resolves_to_its_letter_in_tier1():
    """NATO spellings resolve through the EXISTING letter parser, so the
    verdict/selection they produce is the same one a bare letter produces."""
    for word, letter in (
        ("alpha", "a"), ("alfa", "a"), ("bravo", "b"),
        ("charlie", "c"), ("delta", "d"),
    ):
        spelled = lily_evaluation.lily_tier1_evaluate_question(
            word, MC_QUESTION
        )
        bare = lily_evaluation.lily_tier1_evaluate_question(
            letter, MC_QUESTION
        )
        assert spelled["selected_index"] == bare["selected_index"], word
        assert spelled["verdict"] == bare["verdict"], word
        assert spelled["method"] == "letter", word


def test_choice_text_still_wins_over_a_nato_reading():
    """A question whose option genuinely IS "Delta" resolves by choice text —
    per-choice matching runs before the letter parser."""
    q = dict(MC_QUESTION, choices=["Delta", "Sydney", "Melbourne", "Perth"],
             canonical_answer="Delta", acceptable_answers=["delta"])
    r = lily_evaluation.lily_tier1_evaluate_question("Delta", q)
    assert r["selected_index"] == 0
    assert r["method"] == "choice_text"
    assert r["verdict"] == "correct"


def test_nato_barge_binds_at_choice_2():
    game = _make_game()
    game.sk.bind_speaker("S1", "Rami")
    _arm_and_claim(game, MC_QUESTION)
    _barged(game)

    async def scenario():
        bound = game.early_answer_check(_seg("Bravo."), now=AT_CHOICE_2)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return bound

    assert _run(scenario(), game) is True
    cands = game.sk.ordered_candidates()
    assert cands and cands[0]["text"] == "Bravo."
    assert game.adjudications == [False]


def test_a_correct_barge_keeps_the_ws5_replay_path_and_adjudicates_once():
    """WS-5's correct-answer path is unchanged: the replay's instant Tier-1
    fires it, and C3b must not double-adjudicate on top."""
    game = _make_game()
    game.sk.bind_speaker("S1", "Rami")
    _arm_and_claim(game, MC_QUESTION)
    _barged(game)

    async def scenario():
        bound = game.early_answer_check(_seg("Canberra"), now=AT_CHOICE_2)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return bound

    assert _run(scenario(), game) is True
    assert game.adjudications == [False]        # exactly once
    assert game.sk.ordered_candidates()[0]["text"] == "Canberra"


# ===========================================================================
# C3c — resume from the interrupted choice (the Y7 carve-out).
# ===========================================================================

def test_choice_airing_index_walks_the_stem_then_each_choice():
    game = _make_game()
    _arm_and_claim(game, MC_QUESTION)
    # Inside the protected stem: no choice has begun.
    assert game._mc_choice_airing_index(PLAYOUT_START) is None
    assert game._mc_choice_airing_index(201.9) is None
    assert game._mc_choice_airing_index(202.1) == 0
    assert game._mc_choice_airing_index(203.0) == 1
    assert game._mc_choice_airing_index(203.7) == 2
    assert game._mc_choice_airing_index(204.3) == 3
    # Past the end: the last choice, never an index off the end.
    assert game._mc_choice_airing_index(400.0) == 3


def test_resume_sheet_renders_only_the_remaining_choices():
    game = _make_game()
    _arm_and_claim(game, MC_QUESTION)
    assert game.rendered_armed_choices_from(0) == (
        "A) Canberra\nB) Sydney\nC) Melbourne\nD) Perth"
    )
    assert game.rendered_armed_choices_from(1) == (
        "B) Sydney\nC) Melbourne\nD) Perth"
    )
    assert game.rendered_armed_choices_from(3) == "D) Perth"
    # Never re-reads the stem — that is the whole point of resuming.
    for i in range(4):
        assert MC_QUESTION["prompt"] not in game.rendered_armed_choices_from(i)


def test_resume_uses_the_same_labels_as_the_full_delivery_sheet():
    """Shared MC_CHOICE_LETTERS rendering — a resumed option is labelled
    exactly as it would have been in the uninterrupted read."""
    game = _make_game()
    _arm_and_claim(game, MC_QUESTION)
    full = game.rendered_armed_question()
    for i in range(4):
        for line in game.rendered_armed_choices_from(i).split("\n"):
            assert line in full


def test_resume_never_fires_for_a_read_that_completed_normally():
    """A delivery that ran to completion and is merely waiting out the
    room-discharge gap must never be re-read: no barge, nothing owed."""
    game = _make_game()
    _arm_and_claim(game, MC_QUESTION)
    qnum = game.sk.question_number
    game._active_delivery_ended_at = 205.0      # playout finished cleanly
    assert game._delivery_barge_cut_qnum is None
    assert game._question_barge_resume_still_owed(qnum) is False
    assert game._maybe_resume_mcq_read(
        _seg(SESSION_A_FRAGMENT, 205.5), now=205.5
    ) is False
    assert game.said == []
    assert game.session.interrupts == 0


def test_resume_stands_down_once_an_answer_has_bound():
    game = _make_game()
    game.sk.bind_speaker("S1", "Rami")
    _arm_and_claim(game, MC_QUESTION)
    qnum = game.sk.question_number
    _barged(game)
    assert game._question_barge_resume_still_owed(qnum) is True
    # An answer bound (buffered, pre-window).
    game._pre_window_segments = [_seg("Sydney")]
    assert game._question_barge_resume_still_owed(qnum) is False
    # Or bound as an in-window candidate.
    game._pre_window_segments = []
    game.sk.open_answer_window(
        duration=10.0, reset_candidates=True,
        question_id=MC_QUESTION["id"], question_index=qnum, registered=True,
    )
    game.sk.on_transcript_segment(
        is_final=True, now=AT_CHOICE_2, assume_in_window=True, **_seg("Sydney")
    )
    assert game.sk.ordered_candidates()
    assert game._question_barge_resume_still_owed(qnum) is False


def test_resume_stands_down_when_the_game_stopped_or_moved_on():
    game = _make_game()
    _arm_and_claim(game, MC_QUESTION)
    qnum = game.sk.question_number
    _barged(game)

    game._delivery_stop_sticky = True
    assert game._question_barge_resume_still_owed(qnum) is False
    game._delivery_stop_sticky = False

    game.game_over = True
    assert game._question_barge_resume_still_owed(qnum) is False
    game.game_over = False

    game._adjudicating = True
    assert game._question_barge_resume_still_owed(qnum) is False
    game._adjudicating = False

    assert game._question_barge_resume_still_owed(qnum + 1) is False


def test_resume_is_dispatched_once_not_on_every_following_final():
    """The marker clears when the resume goes out, so a second non-answer
    final does not stack another read."""
    game = _make_game()
    _arm_and_claim(game, MC_QUESTION)
    _barged(game)
    assert game.early_answer_check(
        _seg(SESSION_A_FRAGMENT), now=AT_CHOICE_2
    ) is False
    assert len(game.said) == 1
    assert game.early_answer_check(
        _seg("and another thing"), now=AT_CHOICE_2
    ) is False
    assert len(game.said) == 1


def test_fallback_resume_fires_when_the_barge_utterance_never_arrives():
    """The framework drops a transcript that falls inside
    ignore_user_transcript_until (Y7's own note), so the invariant cannot
    depend on the utterance arriving. The fallback covers that."""
    game = _make_game()
    _arm_and_claim(game, MC_QUESTION)
    qnum = game.sk.question_number
    _barged(game)

    async def scenario():
        # Drive the watchdog body without waiting out the real grace.
        assert game._question_barge_resume_still_owed(qnum) is True
        game.mcq_barge_resume(AT_CHOICE_2)

    _run(scenario(), game)
    assert len(game.said) == 1
    assert game.said[0]["source"] == "mcq_barge_resume"
    assert game._question_barge_resume_still_owed(qnum) is False


# ===========================================================================
# C3d — THE INVARIANT.
# ===========================================================================

def test_invariant_a_barged_question_never_ends_with_nothing_pending():
    """C3d, pinned directly: after ANY barge during an MC read, the question
    has either a bound answer or a resume out. Never neither — which is the
    exact state Session B ended in."""
    for utterance, expect in (
        ("Sydney", "bound"),            # answer-shaped, wrong
        ("Canberra", "bound"),          # answer-shaped, right
        ("bravo", "bound"),             # answer-shaped, NATO
        ("the second one", "bound"),    # answer-shaped, positional
        (SESSION_A_FRAGMENT, "resumed"),
        ("wait, hang on", "resumed"),
        ("that's not fair", "resumed"),
    ):
        game = _make_game()
        game.sk.bind_speaker("S1", "Rami")
        _arm_and_claim(game, MC_QUESTION)
        qnum = game.sk.question_number
        _barged(game)

        async def scenario():
            bound = game.early_answer_check(
                _seg(utterance), now=AT_CHOICE_2
            )
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            return bound

        bound = _run(scenario(), game)
        answer_bound = bool(
            game.sk.ordered_candidates() or game._pre_window_segments
        )
        # C6 amendment: the receipt also dispatches through gated_say now, so
        # "a resume is pending" is read off the resume's own act rather than
        # off the dispatch count.
        resume_pending = any(
            s["act"] == "question_nudge" for s in game.said
        )

        # THE INVARIANT.
        assert answer_bound or resume_pending, (
            f"{utterance!r} left the question half-aired with nothing pending"
        )
        # And nothing is still owed afterwards.
        assert game._question_barge_resume_still_owed(qnum) is False
        if expect == "bound":
            assert bound is True and answer_bound and not resume_pending
        else:
            assert bound is False and resume_pending and not answer_bound


# ===========================================================================
# C4 — answer-window integrity.
# ===========================================================================

def test_session_a_fragment_never_becomes_a_candidate():
    """The Session A regression, end to end through the lane that scored it:
    buffer during delivery -> replay at window open -> adjudication."""
    game = _make_game()
    game.sk.bind_speaker("S1", "Rami")
    _arm_and_claim(game, MC_QUESTION)

    game.buffer_pre_window_answer(_seg(SESSION_A_FRAGMENT))
    assert game._pre_window_segments == []      # never buffered

    _run(_open_plain(game), game)               # replay runs here
    assert game.sk.ordered_candidates() == []   # never scoreable
    assert game.adjudications == []


def test_answer_shaped_speech_during_delivery_still_buffers_and_scores():
    """C4 must not break the early buzz the pre-window buffer exists for."""
    game = _make_game()
    game.sk.bind_speaker("S1", "Rami")
    _arm_and_claim(game, MC_QUESTION)

    game.buffer_pre_window_answer(_seg("Canberra"))
    assert len(game._pre_window_segments) == 1

    _run(_open_plain(game), game)
    cands = game.sk.ordered_candidates()
    assert cands and cands[0]["text"] == "Canberra"


def test_a_wrong_but_committed_early_pick_still_buffers():
    """Answer-shaped includes wrong picks — C4 gates on SHAPE, not verdict."""
    game = _make_game()
    game.sk.bind_speaker("S1", "Rami")
    _arm_and_claim(game, MC_QUESTION)
    game.buffer_pre_window_answer(_seg("Melbourne"))
    assert len(game._pre_window_segments) == 1


def test_a_control_command_during_the_read_is_never_an_answer():
    game = _make_game()
    game.sk.bind_speaker("S1", "Rami")
    _arm_and_claim(game, MC_QUESTION)
    for command in ("skip", "can we skip this one", "back to normal"):
        game._pre_window_segments = []
        game.buffer_pre_window_answer(_seg(command))
        assert game._pre_window_segments == [], command


def test_non_answer_shaped_speech_is_preserved_as_conversation():
    """C4 requires the speech be KEPT, just never scored. The scorekeeper's
    rolling transcript buffer is that record, and it is written by the
    caller's on_transcript_segment regardless of the answer-shape gate."""
    game = _make_game()
    game.sk.bind_speaker("S1", "Rami")
    _arm_and_claim(game, MC_QUESTION)

    game.sk.on_transcript_segment(
        is_final=True, now=AT_CHOICE_2, **_seg(SESSION_A_FRAGMENT)
    )
    game.buffer_pre_window_answer(_seg(SESSION_A_FRAGMENT))

    texts = [t["text"] for t in game.sk.transcript_buffer]
    assert SESSION_A_FRAGMENT in texts          # kept as conversation
    assert game._pre_window_segments == []      # never a candidate
    assert game.sk.ordered_candidates() == []


def test_freeform_answer_shape_is_content_not_questions_or_commands():
    q = FREEFORM_QUESTION
    assert lily_evaluation.lily_answer_shaped("Canberra", q) is True
    assert lily_evaluation.lily_answer_shaped("Sydney", q) is True
    assert lily_evaluation.lily_answer_shaped("it's Canberra", q) is True
    # Asking is not answering.
    assert lily_evaluation.lily_answer_shaped("what was that again?", q) is False
    # Pure filler carries no content to score.
    assert lily_evaluation.lily_answer_shaped("I think", q) is False
    assert lily_evaluation.lily_answer_shaped("um", q) is False
    # Commands are excluded by the caller's flag.
    assert lily_evaluation.lily_answer_shaped(
        "skip", q, is_command=True
    ) is False


def test_answer_shape_failure_fails_closed():
    """A shape check that raises must not admit the utterance — failing open
    here would re-open the Session A hole."""
    game = _make_game()
    _arm_and_claim(game, MC_QUESTION)

    class _Boom(dict):
        def get(self, *a, **k):
            raise RuntimeError("boom")

    # Non-empty so it is truthy and actually reaches the raising .get().
    game.armed_question = _Boom(choices=list(CHOICES))
    assert game._seg_answer_shaped(_seg("Sydney")) is False


def test_no_armed_question_is_never_answer_shaped():
    game = _make_game()
    game.armed_question = None
    assert game._seg_answer_shaped(_seg("Sydney")) is False


# ===========================================================================
# Y7 preservation + source-order pins.
# ===========================================================================

def test_y7_cancel_policy_is_untouched_for_non_question_turns():
    """The carve-out is scoped to a question read. An organic barged turn
    still arms nothing — Y7's finding stands."""
    game = _make_game()
    game._user_speaking = True                  # a human took the floor
    game._mc_delivery_qnum = None               # not a question delivery
    assert game.cut_was_deliberate_barge_in() is True
    assert game._cut_recovery_should_arm([], True, False) is False


def test_barge_cut_marker_is_only_set_for_an_in_flight_question_read():
    """Source-order pin on the carve-out's scope in on_agent_speech_finished:
    note_question_barge_cut is guarded by barge_in AND by the question not
    being in the room yet.

    HOSTLOOP-001 C8 amendment: the inline `_mc_delivery_qnum ==
    question_number` test moved into `_question_owed_recovery`, which
    generalizes it to the freeform read and to a cut nudge that owned the
    delivery. The pin follows it there rather than pinning the MC-only
    spelling C8 was written to widen."""
    src = inspect.getsource(LilyGame.on_agent_speech_finished)
    assert "note_question_barge_cut" in src
    guard = src.split("note_question_barge_cut")[0]
    tail = guard[guard.rindex("if ("):]
    assert "barge_in" in tail
    assert "_question_owed_recovery" in tail
    owed = inspect.getsource(LilyGame._question_owed_recovery)
    # The MC arm C3c had is still there, and the room-already-has-it
    # exclusion is what keeps the generalization from re-reading a
    # delivered question.
    assert "_mc_delivery_qnum" in owed
    assert "CLAIM_CONFIRMED" in owed
    assert "ui_phase" in owed
    # Y7's own arms are still ahead of the carve-out, unchanged.
    assert src.index("arm_reair_gate") < src.index("note_question_barge_cut")
    assert "BARGE_IN_CANCEL" in inspect.getsource(
        LilyGame._cut_recovery_should_arm
    )


def test_c4_gate_precedes_the_buffer_append():
    """Source-order pin: the answer-shape gate must sit BEFORE the append,
    or a non-answer fragment is already in the replay set."""
    src = inspect.getsource(
        lily_speech_delivery.LilySpeechDeliveryMixin.buffer_pre_window_answer
    )
    assert src.index("_seg_answer_shaped") < src.index("buf.append")


def test_resume_is_consumed_before_the_delivery_claim_decision():
    """Source-order pin in tts_node: the staged verbatim resume replaces the
    model's prose BEFORE register_delivery_claim, which under C3a would see
    an open window and pass the prose straight through."""
    src = inspect.getsource(sys.modules["lily_agent"])
    take = src.index("take_pending_delivery_resume()")
    claim = src.index("delivery = self._game.register_delivery_claim(")
    assert take < claim
