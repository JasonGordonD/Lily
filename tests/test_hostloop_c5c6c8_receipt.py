"""WO-LILY-HOSTLOOP-001 C5+C6+C8 — emission discipline, receipt latency,
interrupted-delivery resume. Offline, fixture-first.

THE EVIDENCE these clauses close, and the mechanisms that already existed
but did not cover it:

  Session A (2026-08-12 04:50 UTC) — spoken acks lagging a whole turn (the
  ack for answer N airing after answer N+1 was already in), and a
  self-interrupting DOUBLE emission at 04:52:50 / 04:52:54. The say-gate
  registry makes an act idempotent PER KEY, but the progression loop's own
  beats are largely keyless (gated_say(None, ...)) or carry different keys
  for one beat, so two composites raced with nothing refusing either.
  N12's transition journal has the right shape but only exists from the
  reveal onward and governs narration TEXT; the framework's speech queue
  serializes playout, not generation, so both composites aired back to
  back.

  Session B (lily-05BB92) — verdict spoken-latency 8–13s, and a verdict
  speech cut by barge-in at 36:25 (LILY_SAY | RELEASED | reason=interrupted)
  that dropped the result silently. Adjudication itself is deterministic
  and instant at Tier 1: the seconds were the LLM composite, and the drop
  was Y7's cancel-not-recover policy applied to a beat that is a RESULT,
  not a conversational line.

C5  single-flight: one host composite in flight; a later beat PREEMPTS a
    stale one, the same beat REFUSES a second, the designed staged pair
    (verdict then flourish) still speaks.
C6  a SHORT deterministic receipt for every answer utterance, off the
    Tier-1 verdict that already exists, through the ordinary dispatch
    funnel — and structurally incapable of doubling with the composite.
C8  a barge-cut verdict re-airs its result as one line and un-wedges N+1;
    a barge-cut phase=question read resumes or re-offers.

Fixture idiom follows tests/test_hostloop_c3c4_mcq_barge.py and
tests/test_say_gate_dispatch.py (LilyGame via __new__, a fake session
recording BOTH dispatch lanes, source-order pins).
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
import lily_scorekeeper
import lily_speech_delivery
from lily_agent import LilyGame
from lily_scorekeeper import LilyScorekeeper

MC_QUESTION = {
    "id": "q_5150",
    "category": "academic",
    "difficulty_tier": 2,
    "prompt": "Name the capital city of Australia.",  # 6 words
    "canonical_answer": "Canberra",
    "acceptable_answers": ["canberra"],
    "reveal_color": "",
    "choices": ["Canberra", "Sydney", "Melbourne", "Perth"],
}
FREEFORM_QUESTION = {
    "id": "q_5151",
    "category": "academic",
    "difficulty_tier": 2,
    "prompt": "What is the capital city of Australia?",
    "canonical_answer": "Canberra",
    "acceptable_answers": ["canberra"],
    "reveal_color": "",
}

PLAYOUT_START = 300.0
AT_CHOICE_2 = 303.0


class _FakeHandle:
    def __init__(self, speech_id: str) -> None:
        self.id = speech_id
        self.interrupts = 0

    def interrupt(self, force: bool = False):
        self.interrupts += 1
        return None


class _FakeSession:
    """Records the TWO dispatch lanes separately — which is the whole point
    of C6: `instructions` is the model-mediated lane (generate_reply, the
    8–13s one) and `texts` is the deterministic lane (say)."""

    def __init__(self) -> None:
        self.instructions: list[str] = []
        self.texts: list[str] = []
        self.interrupts = 0
        self._n = 0

    def _handle(self) -> _FakeHandle:
        self._n += 1
        return _FakeHandle(f"speech-{self._n}")

    def generate_reply(self, instructions: str) -> _FakeHandle:
        self.instructions.append(instructions)
        return self._handle()

    def say(self, text: str) -> _FakeHandle:
        self.texts.append(text)
        return self._handle()

    def interrupt(self, *, force: bool = False):
        self.interrupts += 1
        return None


class _FakeAgentHandle:
    def set_preemptive_generation(self, enabled: bool) -> None:
        pass


def _run(coro, game=None):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        timer = getattr(game, "_window_timer", None) if game else None
        if timer is not None and not timer.done():
            timer.cancel()
        loop.close()


def _make_game() -> LilyGame:
    game = LilyGame.bare()
    game.session = _FakeSession()
    game.agent = _FakeAgentHandle()
    game._preemptive_paused = False
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game.sk = LilyScorekeeper("hostloop-c5c6c8-fixture")
    game.group_id = "grp_c5c6c8"
    game.supabase = None
    game.memory_block = ""
    game.prefs = {}
    game._prefs_offer_made = False
    game.reconnected = False
    game.used_prompts = []
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
    game._pending_reveal_event = None
    game._armed_speech_misses = 0
    game._pending_unbound_award = None
    game._undelivered_ticks = 0
    game._undelivered_refires = 0
    game._pending_delivery_qnum = None
    game._mc_delivery_qnum = None
    game._mc_delivery_started_at = None
    game._mc_delivery_stem_words = 0
    game._active_delivery_qnum = None
    game._active_delivery_started_at = None
    game._active_delivery_ended_at = None
    game._pre_window_segments = []
    game._recent_finals = []
    game._delivery_stop_sticky = False
    game._hold_active = False
    game._question_pending = False
    game._awaiting_address_since = 0.0
    game._delivery_barge_cut_qnum = None
    game._pending_delivery_resume = None
    game._playout_started_ids = set()
    game._speech_handles = {}
    game._suppressed_speech_ids = set()
    game._composite_flight_state = None
    game._answer_receipt_aired = None
    game._answer_receipts_fired = None
    game._answer_receipts_qnum = None
    game._transition_journal = {}
    game._open_transition_qnum = None
    game._last_assistant_text = ""
    game.published = []

    async def _publish_attributes(*a, **k):
        game.published.append("attrs")

    async def _publish_metadata(text, **kwargs):
        game.published.append(("meta", text))

    game.publish_attributes = _publish_attributes
    game.publish_metadata = _publish_metadata
    game.publish_attributes_nowait = lambda: game.published.append("attrs")
    game.events = []
    game.send_event_nowait = lambda kind, payload: game.events.append(
        (kind, payload)
    )
    game.adjudications = []

    async def _adjudicate(steal_allowed=True):
        game.adjudications.append(steal_allowed)

    game.adjudicate = _adjudicate
    game.deterministic_replies = []
    game.mark_deterministic_reply = (
        lambda text: game.deterministic_replies.append(text)
    )
    return game


def _arm(game: LilyGame, question: dict) -> None:
    game.armed_question = dict(question)
    game.sk.start_question(game.armed_question)
    game._pre_window_segments = []
    game._pending_delivery_qnum = None


def _arm_and_claim(game: LilyGame, question: dict) -> str:
    """Arm + register the delivery through the real register_delivery_claim,
    then model the framework's speaking transition at PLAYOUT_START."""
    _arm(game, question)
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


# ===========================================================================
# C5 — EMISSION DISCIPLINE (single-flight on the progression loop).
# ===========================================================================

def test_c5_a_second_composite_for_the_same_beat_is_refused():
    """THE Session A hole, directly. Two keyless composites of the same beat
    used to race — each claimed nothing, so no registry key refused either
    and both reached the air (04:52:50 / 04:52:54)."""
    game = _make_game()
    _arm(game, MC_QUESTION)
    assert game.gated_say(None, "question_nudge", "ask it", "window_fallback")
    assert not game.gated_say(
        None, "question_nudge", "ask it differently", "idle_watchdog"
    )
    assert len(game.session.instructions) == 1


def test_c5_the_designed_staged_pair_of_one_transition_still_speaks():
    """T4 splits the beat on purpose: the short verdict turn, then the
    flourish/standings. Same question, DIFFERENT act — the framework's queue
    and N12's journal already order those, so single-flight must not refuse
    the second or the reveal beat loses half of itself."""
    game = _make_game()
    _arm(game, MC_QUESTION)
    assert game.gated_say("q_1_verdict", "verdict", "correct!", "adjudicate")
    assert game.gated_say(
        "round_1_scores", "reveal_scores", "standings", "adjudicate"
    )
    assert len(game.session.instructions) == 2


def test_c5_a_lapped_queue_preempts_the_stale_composite():
    """"The player has lapped the queue": the in-flight composite belongs to
    a question the game has already moved past, so it is DROPPED (through
    the existing T1 cancel_speech path) and current state speaks instead."""
    game = _make_game()
    _arm(game, MC_QUESTION)
    assert game.gated_say(None, "question_nudge", "ask q1", "window_fallback")
    stale = game._composite_flight_state
    assert stale["qnum"] == game.sk.question_number
    # Give the stale dispatch a live handle so the preemption can reach it.
    handle = _FakeHandle(stale["owner"])
    game._speech_handles[stale["owner"]] = handle

    game.sk.question_number += 1          # the table lapped it
    _arm(game, MC_QUESTION)
    assert game.gated_say(None, "question_nudge", "ask q2", "window_fallback")

    assert handle.interrupts == 1                       # stale one dropped
    assert stale["owner"] in game._suppressed_speech_ids
    assert game._composite_flight_state["qnum"] == game.sk.question_number
    assert game.session.instructions == ["ask q1", "ask q2"]


def test_c5_the_lane_frees_at_playout_completion():
    game = _make_game()
    _arm(game, MC_QUESTION)
    assert game.gated_say(None, "steal_window", "five seconds", "adjudicate")
    owner = game._composite_flight_state["owner"]
    assert not game.gated_say(None, "steal_window", "again", "adjudicate")
    game.clear_composite_flight(owner)
    assert game.gated_say(None, "steal_window", "fresh beat", "adjudicate")


def test_c5_a_released_claim_frees_the_lane():
    """The swallowed-turn / regen-gate / STOP paths all RELEASE the claim,
    and a released claim is a dead dispatch by the say gate's own lifecycle
    — so the legitimate redelivery must not be refused as a race."""
    game = _make_game()
    _arm(game, MC_QUESTION)
    assert game.gated_say("q_1_reveal", "reveal", "the answer is...", "adj")
    assert game.say_registry.release_pending() == ["q_1_reveal"]
    assert game.gated_say("q_1_reveal", "reveal", "the answer is...", "retry")
    assert len(game.session.instructions) == 2


def test_c5_a_wedged_flight_can_never_enforce_silence():
    """Failure direction is SPEAK. A flight whose speech never reached
    playout is dead bookkeeping past the stale-claim deadline — the
    HOTFIX-001 lesson (a frozen token must never mute her) applied to this
    state too."""
    game = _make_game()
    _arm(game, MC_QUESTION)
    assert game.gated_say(None, "question_nudge", "ask it", "window_fallback")
    game._composite_flight_state["at"] -= (
        lily_speech_delivery._STALE_CLAIM_SECONDS + 1.0
    )
    assert game.gated_say(None, "question_nudge", "ask it again", "watchdog")
    assert len(game.session.instructions) == 2


def test_c5_non_composite_acts_are_untouched():
    """Scoped to the progression loop's composites. The conversational acks
    (pace, media, hold) are not composites and keep speaking freely — a
    false suppression here would be a worse defect than the double emission
    this closes."""
    game = _make_game()
    assert game.gated_say(None, "pace_ack", "slower it is", "voice_command")
    assert game.gated_say(None, "pace_ack", "back to speed", "voice_command")
    assert len(game.session.instructions) == 2
    assert game._composite_flight_state is None


def test_c5_a_refused_composite_never_takes_the_flight_token():
    """Ordering pin: the flight gate runs AFTER the refusal gates, so a
    dispatch the hold/live-game gates reject cannot leave a token behind
    that mutes the lane once the gate clears."""
    game = _make_game()
    game.game_started = False            # P8: no live game
    assert not game.gated_say(None, "steal_window", "five seconds", "adj")
    assert game._composite_flight_state is None
    src = inspect.getsource(LilyGame.gated_say)
    assert src.index("hold_blocks_dispatch") < src.index(
        "composite_flight_blocks_dispatch"
    )
    assert src.index("game_payload_blocked") < src.index(
        "composite_flight_blocks_dispatch"
    )


# ===========================================================================
# C6 — RECEIPT LATENCY. The verdict word, off the deterministic lane, now.
# ===========================================================================

def test_c6_a_correct_answer_gets_its_word_from_the_deterministic_lane():
    """No LLM in the path: the receipt goes out as FIXED words through
    session.say, which is what makes sub-2s possible at all. The 8–13s
    lane (generate_reply) is not touched."""
    game = _make_game()
    game.sk.bind_speaker("S1", "Rami")
    _arm(game, FREEFORM_QUESTION)
    assert game.fire_answer_receipt(
        "correct", text="Canberra", player="Rami"
    ) is True
    assert game.session.texts == [lily_say_gate.LILY_RECEIPT_CORRECT]
    assert game.session.instructions == []     # the composite lane is idle


def test_c6_an_uncertain_tier1_gets_a_neutral_ack_never_a_verdict_word():
    """The clause's hard line: UNCERTAIN means the JUDGE has not ruled, so
    the receipt commits to nothing."""
    game = _make_game()
    _arm(game, FREEFORM_QUESTION)
    assert game.fire_answer_receipt(
        "uncertain", text="uh, Sydney maybe", player="Rami"
    ) is True
    assert game.session.texts == [lily_say_gate.LILY_RECEIPT_NEUTRAL]
    receipt = game.session.texts[0]
    for word in ("correct", "right", "wrong", "no"):
        assert word not in lily_evaluation.lily_normalize_answer(
            receipt
        ).split()


def test_c6_the_receipt_vocabulary_is_exactly_three_mappings():
    assert lily_say_gate.lily_answer_receipt("correct") == (
        lily_say_gate.LILY_RECEIPT_CORRECT
    )
    assert lily_say_gate.lily_answer_receipt("incorrect") == (
        lily_say_gate.LILY_RECEIPT_INCORRECT
    )
    assert lily_say_gate.lily_answer_receipt("uncertain") == (
        lily_say_gate.LILY_RECEIPT_NEUTRAL
    )
    assert lily_say_gate.lily_answer_receipt("partial") is None
    assert lily_say_gate.lily_answer_receipt(None) is None


def test_c6_no_receipt_can_ever_preempt_or_satisfy_the_reveal():
    """THE anti-double, structurally. Both "she already said it" detectors
    require the canonical ANSWER to be present; no receipt carries one, so
    a receipt can never suppress the verdict beat, never burn the question
    as revealed, and never bind as the transition's narration."""
    answer = str(MC_QUESTION["canonical_answer"])
    for receipt in (
        lily_say_gate.LILY_RECEIPT_CORRECT,
        lily_say_gate.LILY_RECEIPT_INCORRECT,
        lily_say_gate.LILY_RECEIPT_NEUTRAL,
    ):
        assert lily_scorekeeper.lily_verdict_narration(receipt, answer) is None
        game = _make_game()
        _arm(game, MC_QUESTION)
        game._last_assistant_text = receipt
        assert game._verdict_already_spoken(game.armed_question, None) is False
        assert game._reveal_already_on_air(game.armed_question) is False


def test_c6_the_composite_is_told_the_receipt_already_aired():
    """The other half of not doubling: the verdict instructions name the
    exact words the room has already heard, so the beat carries on from the
    receipt instead of ruling twice."""
    game = _make_game()
    _arm(game, FREEFORM_QUESTION)
    qnum = game.sk.question_number
    assert game.answer_receipt_aired_for(qnum) is None
    game.fire_answer_receipt("correct", text="Canberra", player="Rami")
    assert game.answer_receipt_aired_for(qnum) == (
        lily_say_gate.LILY_RECEIPT_CORRECT
    )
    # A receipt belongs to ONE question; the next question's composite is
    # not told about it.
    assert game.answer_receipt_aired_for(qnum + 1) is None
    src = inspect.getsource(LilyGame.adjudicate)
    assert "answer_receipt_aired_for" in src
    assert src.index("answer_receipt_aired_for") < src.index(
        'source="adjudicate_verdict"'
    )


def test_c6_one_answer_gets_one_receipt_and_a_question_is_capped():
    game = _make_game()
    _arm(game, FREEFORM_QUESTION)
    assert game.fire_answer_receipt(
        "uncertain", text="Sydney", player="Rami"
    ) is True
    # Same player, same words — one answer, one receipt.
    assert game.fire_answer_receipt(
        "uncertain", text="Sydney", player="Rami"
    ) is False
    # Different answerers each get theirs...
    for name in ("Chris", "Rhonda", "Dana"):
        assert game.fire_answer_receipt(
            "uncertain", text=f"guess {name}", player=name
        ) is True
    # ...up to the cap, then the composite owns the words again.
    assert game.fire_answer_receipt(
        "uncertain", text="one more", player="Eve"
    ) is False
    assert len(game.session.texts) == (
        lily_speech_delivery._ANSWER_RECEIPT_MAX_PER_QUESTION
    )


def test_c6_the_receipt_respects_the_hold_and_the_stop():
    """No new bypass — chain F stays closed. The receipt rides gated_say, so
    every gate that binds a composite binds it."""
    held = _make_game()
    _arm(held, FREEFORM_QUESTION)
    held._hold_active = True
    assert held.fire_answer_receipt(
        "correct", text="Canberra", player="Rami"
    ) is False
    assert held.session.texts == []
    assert held.answer_receipt_aired_for(held.sk.question_number) is None

    stopped = _make_game()
    _arm(stopped, FREEFORM_QUESTION)
    stopped._delivery_stop_sticky = True
    assert stopped.fire_answer_receipt(
        "correct", text="Canberra", player="Rami"
    ) is False
    assert stopped.session.texts == []


def test_c6_no_receipt_once_the_transition_has_narrated_its_verdict():
    """From the reveal onward the beat belongs to the transition (N12); a
    receipt then would be a second ruling of one committed row."""
    game = _make_game()
    _arm(game, FREEFORM_QUESTION)
    qnum = game.sk.question_number
    assert game.open_question_transition(qnum, owner="t1", source="test")
    game.journal_transition(qnum, "reveal", owner="t1", detail={})
    game.journal_transition(
        qnum, "verdict", owner="t1", detail={"key": f"q_{qnum}_reveal"}
    )
    assert game.fire_answer_receipt(
        "correct", text="Canberra", player="Rami"
    ) is False


def test_c6_receipts_are_short_enough_to_recur_legally():
    """A receipt fires every question by design. The air-path dup guard
    exempts short turns and the repetition lints need 4 words / 3 content
    words, so a recurring receipt is not a repetition defect — and must not
    be silenced as one."""
    game = _make_game()
    _arm(game, FREEFORM_QUESTION)
    for receipt in (
        lily_say_gate.LILY_RECEIPT_CORRECT,
        lily_say_gate.LILY_RECEIPT_INCORRECT,
        lily_say_gate.LILY_RECEIPT_NEUTRAL,
    ):
        assert len(receipt) < 15                     # dup-guard exempt
        game.sk.agent_turns = [receipt] * 6
        assert game.air_dup_guard(receipt, None) is False
        assert lily_say_gate.lily_repeat_flag(receipt, [receipt]) is None
        assert lily_say_gate.lily_paraphrase_repeat_flag(
            receipt, [receipt]
        ) is None
        # ...and it survives outbound hygiene unmutated (the em dash and the
        # exclamation are the register, not markdown).
        assert lily_say_gate.lily_clean_for_speech(receipt) == receipt
        assert lily_say_gate.lily_stacked_question_flag(receipt) == 0


def test_c6_the_ack_stands_down_when_she_is_about_to_ask_instead():
    """"Locked in—" followed by "was that an answer, or thinking out loud?"
    contradicts itself. The Task-4 clarify fires on the middle BAND, a subset
    of the uncertain verdict, and there the clarify question IS the receipt —
    same deterministic dispatch, same sub-2s window. One band rule, read from
    the same values, not a second one."""
    game = _make_game()
    _arm(game, FREEFORM_QUESTION)
    threshold = lily_evaluation.FUZZY_CORRECT_THRESHOLD
    margin = lily_config.tier1_clarify_margin()
    in_band = {"verdict": "uncertain", "similarity": threshold - margin / 2}
    far_below = {"verdict": "uncertain", "similarity": 0.0}
    assert lily_evaluation.lily_tier1_band(
        in_band["similarity"], threshold, margin
    ) == lily_evaluation.BAND_CLARIFY
    assert game._receipt_yields_to_clarify(in_band, threshold) is True
    assert game._receipt_yields_to_clarify(far_below, threshold) is False
    # A definitive verdict never yields, and an unreadable one fails OPEN —
    # the receipt existing is the clause.
    assert game._receipt_yields_to_clarify(
        {"verdict": "correct", "similarity": in_band["similarity"]}, threshold
    ) is False
    assert game._receipt_yields_to_clarify(
        {"verdict": "uncertain", "similarity": None}, threshold
    ) is False
    src = inspect.getsource(LilyGame.on_transcript_event)
    assert "_receipt_yields_to_clarify" in src


def test_c6_the_receipt_seam_sits_ahead_of_every_slow_step():
    """Source-order pin on WHERE the latency is won: the receipt fires from
    the instant-Tier-1 seam, before the speculative judge, before
    adjudicate, and therefore before the commit, the publishes and the LLM
    composite."""
    src = inspect.getsource(LilyGame.on_transcript_event)
    assert "fire_answer_receipt" in src
    assert src.index("fire_answer_receipt") < src.index(
        "asyncio.ensure_future(self.adjudicate("
    )
    assert src.index("fire_answer_receipt") < src.index(
        "_speculative_judge"
    )
    # And it reads the verdict the path ALREADY computed — no second matcher.
    receipt_src = inspect.getsource(LilyGame.fire_answer_receipt)
    assert "lily_tier1" not in receipt_src


def test_c6_a_mid_read_binding_receipts_before_the_window_even_opens():
    """The C3b fork: an answer-shaped barge binds while the window is still
    closed, so the scorekeeper recorded no candidate and the instant-Tier-1
    seam never sees the utterance. It is the fastest binding in the system
    and the one most owed an instant word."""
    game = _make_game()
    game.sk.bind_speaker("S1", "Rami")
    assert _arm_and_claim(game, MC_QUESTION) == "claimed_structural"
    assert game.sk.answer_window_open is False

    async def scenario():
        aborted = game.mc_early_answer_check(_seg("Sydney"), now=AT_CHOICE_2)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return aborted

    assert _run(scenario(), game) is True
    assert game.session.texts == [lily_say_gate.LILY_RECEIPT_INCORRECT]
    # ...and the same utterance reaching the instant seam afterwards does
    # not receipt twice (identity is who + what, not the carrier).
    assert game.fire_answer_receipt(
        "incorrect", text="Sydney", player="Rami"
    ) is False


# ===========================================================================
# C8 — INTERRUPTED-DELIVERY RESUME, generalized.
# ===========================================================================

def _cut_verdict(game: LilyGame, key: str) -> list:
    """Model the framework barge that killed a verdict beat: the claim was
    owned by the dead speech and on_agent_speech_finished released it."""
    game.say_registry.claim(key, owner="speech-verdict")
    return game.say_registry.release_owner("speech-verdict")


def test_c8_a_cut_verdict_reairs_the_result_as_one_line():
    """Session B 36:25. The cut released q_{N}_reveal, Y7 recovered nothing
    (correctly, for a conversational line), and the RULING never reached the
    room. One deterministic line now does — carrying the answer, because a
    re-air that omits the result re-airs nothing."""
    game = _make_game()
    _arm(game, MC_QUESTION)
    qnum = game.sk.question_number
    game.sk.current_question = dict(MC_QUESTION)
    assert game.open_question_transition(qnum, owner="t1", source="adjudicate")
    game.journal_transition(
        qnum, "reveal", owner="t1",
        detail={"answer": "Canberra", "correct": True, "winner": "Rami"},
    )
    released = _cut_verdict(game, f"q_{qnum}_reveal")
    assert released == [f"q_{qnum}_reveal"]

    assert game.reair_cut_verdict(released) is True
    assert game.session.texts == ["Correct, Rami — Canberra."]
    assert game.session.instructions == []      # deterministic, no LLM


def test_c8_the_reair_retakes_the_key_so_the_next_question_is_not_wedged():
    """The second half of the 36:25 defect, and the quieter one: the
    transition's verdict entry still names the released key, so
    _transition_holds_next_delivery reads "not CONFIRMED" forever and
    question N+1 is held behind a verdict that can never confirm."""
    game = _make_game()
    _arm(game, MC_QUESTION)
    qnum = game.sk.question_number
    game.sk.current_question = dict(MC_QUESTION)
    key = f"q_{qnum}_reveal"
    assert game.open_question_transition(qnum, owner="t1", source="adjudicate")
    game.journal_transition(
        qnum, "reveal", owner="t1",
        detail={"answer": "Canberra", "correct": False, "winner": None},
    )
    game.journal_transition(qnum, "verdict", owner="t1", detail={"key": key})
    released = _cut_verdict(game, key)
    # THE WEDGE: claim gone, journal still pointing at it.
    assert game.say_registry.state(key) is None
    assert game._transition_holds_next_delivery("post_reveal") is True

    assert game.reair_cut_verdict(released) is True
    assert game.session.texts == ["Nobody had it — Canberra."]
    assert game.say_registry.state(key) == lily_say_gate.CLAIM_PENDING
    # ...and once the re-aired line plays out, N+1 is released as normal.
    game.say_registry.confirm(key)
    assert game._transition_holds_next_delivery("post_reveal") is False


def test_c8_the_reair_line_is_the_transitions_narration_not_a_second_one():
    """The re-air holds the verdict key, which is exactly the branch N12's
    narration gate already has for a re-air after a cut — so it BINDS as the
    narration rather than being suppressed as a duplicate of it."""
    game = _make_game()
    _arm(game, MC_QUESTION)
    qnum = game.sk.question_number
    key = f"q_{qnum}_reveal"
    assert game.open_question_transition(qnum, owner="t1", source="adjudicate")
    game.journal_transition(
        qnum, "reveal", owner="t1",
        detail={"answer": "Canberra", "correct": True, "winner": "Rami"},
    )
    game.journal_transition(qnum, "verdict", owner="t1", detail={"key": key})
    game.say_registry.claim(key, owner="speech-reair")
    line = lily_say_gate.lily_verdict_reair_line(
        correct=True, answer="Canberra", winner="Rami",
    )
    assert game.register_transition_narration(
        line, speech_id="speech-reair"
    ) == "narration"


def test_c8_a_cut_verdict_does_not_reair_into_a_stop():
    game = _make_game()
    _arm(game, MC_QUESTION)
    qnum = game.sk.question_number
    game.sk.current_question = dict(MC_QUESTION)
    released = _cut_verdict(game, f"q_{qnum}_reveal")
    game._delivery_stop_sticky = True
    assert game.reair_cut_verdict(released) is False
    assert game.session.texts == []


def test_c8_only_the_verdict_beats_own_keys_trigger_a_reair():
    """Scope pin: Y7's cancel-not-recover stays the policy for everything
    that is not the verdict RESULT."""
    game = _make_game()
    _arm(game, MC_QUESTION)
    qnum = game.sk.question_number
    assert game.open_question_transition(qnum, owner="t1", source="adjudicate")
    game.journal_transition(
        qnum, "reveal", owner="t1",
        detail={"answer": "Canberra", "correct": False, "winner": None},
    )
    for key in (
        f"q_{qnum}_delivery", "session_greet", "round_1_scores",
        f"q_{qnum + 1}_reveal",     # a different question's beat
    ):
        assert game.reair_cut_verdict([key]) is False
    assert game.session.texts == []
    assert game.reair_cut_verdict([f"q_{qnum}_verdict"]) is True


def test_c8_the_reair_reads_the_question_off_the_KEY_not_the_live_counter():
    """The one that would have made this whole clause a no-op in production.

    adjudicate dispatches the verdict and then, in the SAME tick, consumes
    the question and arms N+1 — so `sk.question_number` already names N+1 by
    the time that verdict's playout ends, and `sk.current_question` is the
    NEXT question. Deriving the key from live state would look for
    q_{N+1}_reveal (never released) and find nothing; reading the ANSWER from
    live state would put the next question's answer on the air.

    Both come from the released key and the journal instead."""
    game = _make_game()
    _arm(game, MC_QUESTION)
    revealed = game.sk.question_number
    key = f"q_{revealed}_reveal"
    assert game.open_question_transition(
        revealed, owner="t1", source="adjudicate"
    )
    game.journal_transition(
        revealed, "reveal", owner="t1",
        detail={"answer": "Canberra", "correct": True, "winner": "Rami"},
    )
    released = _cut_verdict(game, key)

    # The game has moved on exactly as adjudicate leaves it.
    game.sk.question_number = revealed + 1
    game.sk.current_question = dict(FREEFORM_QUESTION)
    game.armed_question = dict(FREEFORM_QUESTION)
    game.sk.current_question["canonical_answer"] = "Wellington"

    assert game.reair_cut_verdict(released) is True
    # The REVEALED question's committed result, not the armed one's answer.
    assert game.session.texts == ["Correct, Rami — Canberra."]
    assert "Wellington" not in game.session.texts[0]
    assert game.say_registry.state(key) == lily_say_gate.CLAIM_PENDING


def test_c8_a_reair_never_invents_a_result_it_has_no_record_of():
    """The journal is the record of what was committed; with no reveal entry
    there is nothing to state, and stating something would be worse than the
    silence this clause exists to fix."""
    game = _make_game()
    _arm(game, MC_QUESTION)
    qnum = game.sk.question_number
    game.sk.current_question = dict(MC_QUESTION)   # live state HAS an answer
    released = _cut_verdict(game, f"q_{qnum}_reveal")
    assert game.reair_cut_verdict(released) is False
    assert game.session.texts == []


def test_c8_a_freeform_read_cut_by_a_barge_reoffers_the_whole_question():
    """C3c could only resume an MC options read (it needs a per-choice
    position). A FREEFORM read cut by a barge had no resume point, so it
    fell through to Y7's cancel and the question died half-aired — the same
    shape as Session B, one format over. It is re-offered whole from the
    deterministic sheet."""
    game = _make_game()
    game.sk.bind_speaker("S1", "Rami")
    assert _arm_and_claim(game, FREEFORM_QUESTION) == "claimed_structural"
    qnum = game.sk.question_number
    assert game._mc_delivery_qnum is None          # no MC resume point
    assert game._mc_choice_airing_index(AT_CHOICE_2) is None

    game.note_question_barge_cut(qnum)
    assert game._question_barge_resume_still_owed(qnum) is True
    assert game.mcq_barge_resume(AT_CHOICE_2) is True

    # The re-offer speaks the armed sheet verbatim, not a paraphrase.
    assert game.take_pending_delivery_resume() == (
        game.rendered_armed_question()
    )
    nudges = [i for i in game.session.instructions if "again" in i]
    assert len(nudges) == 1
    assert FREEFORM_QUESTION["prompt"] in nudges[0]
    assert game.session.interrupts == 1
    # C3d, one format over: nothing is left owed.
    assert game._question_barge_resume_still_owed(qnum) is False


def test_c8_the_owed_predicate_covers_the_read_and_excludes_the_delivered():
    """The generalized mark: "the room does not have this question yet, and
    the speech that just died was the speech that owed it"."""
    qnum_key = "q_1_delivery"

    # (a) an MC read in flight — C3c's original arm, unchanged.
    mc = _make_game()
    _arm_and_claim(mc, MC_QUESTION)
    assert mc._question_owed_recovery([]) is True

    # (b) a freeform read whose delivery claim the cut just released.
    free = _make_game()
    _arm_and_claim(free, FREEFORM_QUESTION)
    free.say_registry.release(qnum_key)
    assert free._question_owed_recovery([qnum_key]) is True
    assert free._question_owed_recovery([]) is False     # nothing owed it

    # (c) a question the room demonstrably HEARD is never re-read.
    heard = _make_game()
    _arm_and_claim(heard, FREEFORM_QUESTION)
    heard.say_registry.confirm(qnum_key)
    assert heard._question_owed_recovery([qnum_key]) is False

    # (d) outside phase=question, Y7's cancel still governs.
    elsewhere = _make_game()
    _arm_and_claim(elsewhere, MC_QUESTION)
    elsewhere.ui_phase = "reveal"
    assert elsewhere._question_owed_recovery([]) is False

    # (e) and a stopped or finished game recovers nothing.
    stopped = _make_game()
    _arm_and_claim(stopped, MC_QUESTION)
    stopped._delivery_stop_sticky = True
    assert stopped._question_owed_recovery([]) is False
    over = _make_game()
    _arm_and_claim(over, MC_QUESTION)
    over.game_over = True
    assert over._question_owed_recovery([]) is False


def test_c8_wiring_pin_on_agent_speech_finished():
    """Source-order pins on the two C8 hooks: the composite lane is freed
    BEFORE the re-air dispatches (the re-air is itself act="verdict" and
    would otherwise be refused as racing the flight that just died), and
    both hooks read Y7's own `barge_in` decision rather than re-deriving a
    cut cause."""
    src = inspect.getsource(LilyGame.on_agent_speech_finished)
    assert "clear_composite_flight" in src
    assert "reair_cut_verdict" in src
    assert src.index("clear_composite_flight") < src.index(
        "reair_cut_verdict"
    )
    assert src.index("barge_in =") < src.index("reair_cut_verdict")
    tail = src.split("reair_cut_verdict")[0]
    guard = tail[tail.rindex("if "):]
    assert "barge_in" in guard and "interrupted" in guard
