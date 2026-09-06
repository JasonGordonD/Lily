"""WO-LILY-COMPOSITION-FOLLOWUP-001 — the composition reviewer's findings
on the deployed wave (lily-D11A7E-c46e33c3, 11:43Z) plus the operator's
B1-B5 / L1-L3 decisions, driven on the REAL paths (on_transcript_event,
on_user_turn_completed, on_agent_speech_finished, the say gate, the
metrics collector, the probe). No source text is inspected anywhere.

Live receipts pinned:
  P0-1/B2  "I need you to pause the game for a moment" → "Paused." → the
           next question 6 s later; 11:50:36Z "Paused." → window 3 at
           11:50:43Z. A pause now holds progression until an explicit
           resume; the next final does not release it.
  P0-2/B1  "I would comfortably say a." (11:47:38Z) read uncertain; "Earth
           tool." (11:47:55Z, STT for "Earth to Lily") bound to "Earth".
  P0-3/B4  "can I get some multiple choice answers" (11:48:55Z, kb_318)
           recorded as the candidate; StopResponse; 45 s of silence.
  B3       "question's still up" 11:48:17Z, window closed on its timer
           11:48:18Z under a relaxed preference.
  B5       11:45:40Z start phrase → first question 11:47:05Z.
  C1-C10, L1-L3: see the CHANGELOG entry.
"""

import asyncio
import logging
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_acts  # noqa: E402
import lily_agent  # noqa: E402
import lily_config  # noqa: E402
import lily_evaluation  # noqa: E402
import lily_metrics  # noqa: E402
import lily_scorekeeper  # noqa: E402
import lily_speech_delivery  # noqa: E402
import lily_voice_embedder as ve  # noqa: E402
from lily_agent import LILY_SYSTEM_PROMPT  # noqa: E402
from lily_scorekeeper import LilyScorekeeper  # noqa: E402

import test_restart_wo5 as R  # noqa: E402
from test_airgate_001 import _Handle, _game as _airgate_game  # noqa: E402
from test_bind_dispute_p0 import (  # noqa: E402
    _final, _make_game as _dispute_game, _run,
)
from test_control_gates_w2 import _lobby_game, _said, _settle, _sid  # noqa: E402
from test_organic_double_verdict import _drive_prehook  # noqa: E402

Q_MC = {
    "id": "kb_planets",
    "prompt": "Which planet is known as the Red Planet?",
    "canonical_answer": "Mars",
    "acceptable_answers": ["mars"],
    "choices": ["Earth", "Mars", "Venus", "Jupiter"],
    "category": "science",
}
Q_NUMERIC = {
    "id": "kb_318",
    "prompt": "In what year did Apollo 11 land on the Moon?",
    "canonical_answer": "1969",
    "acceptable_answers": ["1969"],
    "category": "history",
}
Q_FREEFORM = {
    "id": "kb_wilde2",
    "prompt": "Who wrote The Importance of Being Earnest?",
    "canonical_answer": "Oscar Wilde",
    "acceptable_answers": ["oscar wilde", "wilde"],
    "category": "literature",
}

LIVE_PAUSE = "I need you to pause the game for a moment"
LIVE_LETTER = "I would comfortably say a."
LIVE_EARTH_TOOL = "Earth tool."
LIVE_MC_REQUEST = "can I get some multiple choice answers"
LIVE_KEEP = "Let's keep it like it is"


def _arm(game, question, *, window=None, pacing=None):
    """A live card on the table (the bind-dispute harness shape)."""
    game.sk.bind_speaker("S1", "Rami")
    game.armed_question = dict(question)
    game.sk.start_question(game.armed_question)
    game.sk.round = 1
    game.sk.set_phase("round")
    if pacing:
        game.sk.set_pacing(pacing)
    delivered = getattr(game, "_delivered_to_playout", None)
    if delivered is None:
        delivered = game._delivered_to_playout = set()
    delivered.add(game.sk.question_number)
    key = f"q_{game.sk.question_number}_delivery"
    game.say_registry.claim(key, owner="speech_delivery")
    game.say_registry.confirm(key)
    at = time.time()
    if window is not None:
        game.sk.open_answer_window(window, now=at - 1.0)
    return at


# ===========================================================================
# P0-1 / B2 — the PAUSE is a hard stop on progression
# ===========================================================================


@pytest.mark.parametrize("text", [
    LIVE_PAUSE,
    "can we pause the game",
    "pause please",
    "let's pause for a sec",
    "Lily, hit pause",
    "pause this for a minute",
])
def test_pause_sentences_are_detected(text):
    assert lily_scorekeeper.lily_detect_pause_request(text) is True


@pytest.mark.parametrize("text", [
    "why did you pause?",
    "don't pause it",
    "the pause button on my phone is broken",
    "unpause",
    "wait, is it Saturn?",
])
def test_pause_talk_and_negations_never_fire(text):
    assert lily_scorekeeper.lily_detect_pause_request(text) is False


def test_pause_sentence_holds_progression_until_explicit_resume():
    """The 11:43Z receipt through the real transcript path: the pause
    sentence enters a STICKY hold, dispatch_armed_question refuses, a
    later ordinary final does NOT release it, "resume" does."""
    game = _dispute_game()
    _arm(game, Q_FREEFORM)
    game.sk.close_answer_window()
    game.armed_question = dict(Q_MC)  # the NEXT card, armed
    at = time.time()

    def _go():
        _final(game, LIVE_PAUSE, at)
        assert game.pause_sticky() is True
        assert game.progression_paused_reason() == "hold"
        assert game.dispatch_armed_question(source="test") is False
        assert _said(game, "paused")
        # 11:50:36Z → 11:50:43Z: the very next final used to release it.
        _final(game, "so anyway, how are the scores looking", at + 6)
        assert game.pause_sticky() is True
        assert game.progression_paused_reason() == "hold"
        assert game.dispatch_armed_question(source="test") is False
        # An explicit resume lifts it.
        _final(game, "okay Lily, resume", at + 12)
        assert game.pause_sticky() is False
        assert game._hold_active is False
        assert game.progression_paused_reason() != "hold"

    _run(_go)


def test_pause_never_lifts_on_the_hold_timeout(monkeypatch):
    monkeypatch.setenv("LILY_HOLD_TIMEOUT_SECONDS", "0.01")
    game = _dispute_game()
    _arm(game, Q_FREEFORM)
    at = time.time()

    async def _go():
        _final(game, "pause please", at)
        assert game.pause_sticky() is True
        await asyncio.sleep(0.05)
        assert game.hold_timed_out() is True
        assert await game._wp_hold() == lily_agent._WATCH_HALT
        assert game.pause_sticky() is True

    _run(_go)


def test_hold_on_a_sec_mid_window_holds_the_clock_and_keeps_the_window():
    """B2's own example — "hold on a sec" while a TIMED window is open:
    the clock is held (deadline lifted, expiry cancelled), the window and
    its candidate SURVIVE (the STOP brake would have wiped them), and
    "okay go" re-arms the remaining clock on the same window."""
    game = _dispute_game()
    at = _arm(game, Q_FREEFORM, window=20.0)

    async def _go():
        game._arm_window_expiry(19.0)
        _final(game, "Wilde", at)
        assert "Rami" in game.sk.answer_candidates
        _final(game, "hold on a sec", at + 1)
        assert game.pause_sticky() is True
        assert game.sk.answer_window_open is True
        assert game.sk.answer_window_deadline is None
        assert game._window_timer is None
        assert "Rami" in game.sk.answer_candidates  # not wiped
        assert game.sk.current_question is not None
        _final(game, "okay go", at + 2)
        assert game.pause_sticky() is False
        assert game.sk.answer_window_deadline is not None
        assert game._window_timer is not None and not game._window_timer.done()
        game._window_timer.cancel()

    _run(_go)


def test_answer_landing_in_the_open_window_lifts_the_pause():
    game = _dispute_game()
    at = _arm(game, Q_FREEFORM, window=30.0)

    def _go():
        _final(game, "hold on a second", at)
        assert game.pause_sticky() is True
        _final(game, "Oscar Wilde", at + 2)
        assert game.pause_sticky() is False
        assert "Rami" in game.sk.answer_candidates

    _run(_go)


def test_organic_paused_backs_a_hold():
    """An organic "Paused." asserts a state; the narration integrity
    organ now enters the hold behind it (pre-fix: False, nothing held)."""
    assert lily_scorekeeper.lily_detect_hold_narration("Paused.") is True
    assert lily_scorekeeper.lily_detect_hold_narration(
        "Okay — paused. Say when."
    ) is True
    assert lily_scorekeeper.lily_detect_hold_narration(
        "the clock paused for a second there, carry on"
    ) is False
    game = _airgate_game()
    assert game.back_hold_narration("Paused.") is True
    assert game._hold_active is True


@pytest.mark.parametrize("text,expected", [
    ("resume", True), ("okay go", True), ("go ahead", True),
    ("we're back", True), ("let's keep going", True), ("unpause", True),
    ("Ready.", True),
    ("go get me a drink", False), ("okay so what's the score", False),
    ("Saturn", False),
])
def test_pause_release_phrases(text, expected):
    assert lily_scorekeeper.lily_detect_pause_release(text) is expected


def test_stop_and_pause_own_the_turn_for_the_prehook():
    game = _airgate_game()
    assert game.stop_or_hold_owns_turn(LIVE_PAUSE) is True


# ===========================================================================
# P0-2 / B1 — a choice letter inside a sentence; the utterance IS the option
# ===========================================================================


@pytest.mark.parametrize("text,index,method", [
    (LIVE_LETTER, 0, "letter"),
    ("say b.", 1, "letter"),
    ("I'd say c", 2, "letter"),
    ("a", 0, "letter"),
    ("I'd go A", 0, "letter"),
    ("Earth", 0, "choice_text"),
    ("Mars, uh", 1, "choice_text"),
    ("it's Venus, final answer", 2, "choice_text"),
    ("Jupitor", 3, "choice_text"),
    ("the second one", 1, "positional"),
])
def test_mc_resolves_letters_and_whole_utterance_options(text, index, method):
    r = lily_evaluation.lily_tier1_evaluate_mc(
        text, Q_MC["choices"], Q_MC["canonical_answer"]
    )
    assert r["selected_index"] == index, r
    assert r["method"] == method, r


@pytest.mark.parametrize("text", [
    LIVE_EARTH_TOOL,
    "earth to Lily",
    "is Earth even a planet in this game",
    "what was the second one again",
])
def test_mc_never_resolves_by_containment_or_prefix(text):
    r = lily_evaluation.lily_tier1_evaluate_mc(
        text, Q_MC["choices"], Q_MC["canonical_answer"]
    )
    assert r["selected_index"] is None, r
    assert r["verdict"] == "uncertain"
    assert lily_evaluation.lily_mc_unresolved(text, Q_MC) is True


def test_letter_pick_binds_and_earth_tool_cannot_revise_it():
    """The live sequence through the real path: the letter binds A, the
    garbled "Earth tool." lands next and is REFUSED as a revision — the
    committed pick stands (B1: binding closed for that player)."""
    game = _dispute_game()
    at = _arm(game, Q_MC, window=30.0)

    def _go():
        _final(game, LIVE_LETTER, at)
        cand = game.sk.answer_candidates.get("Rami")
        assert cand is not None and cand["text"] == LIVE_LETTER
        assert lily_evaluation.lily_tier1_evaluate_question(
            cand["text"], game.sk.current_question
        )["selected_index"] == 0
        _final(game, LIVE_EARTH_TOOL, at + 3)
        cand = game.sk.answer_candidates.get("Rami")
        assert cand["text"] == LIVE_LETTER  # never revised to "Earth"
        assert len(cand["attempts"]) == 1
        # A real re-pick still revises.
        _final(game, "no wait, C", at + 5)
        assert game.sk.answer_candidates["Rami"]["text"] == "no wait, C"

    _run(_go)


def test_earth_tool_alone_goes_through_the_clarify_door_not_the_ledger():
    game = _dispute_game()
    at = _arm(game, Q_MC, window=30.0)

    def _go():
        _final(game, LIVE_EARTH_TOOL, at)
        assert "Rami" in game.pending_clarify
        assert game.pending_clarify["Rami"].get("shape") == (
            lily_evaluation.LILY_SHAPE_MC_UNRESOLVED
        )
        assert game.sk.answer_candidates.get("Rami") is None  # withdrawn
        assert any(
            "thinking out loud" in i for i in game.session.instructions
        )
        # And no "Locked in" receipt aired over the clarify.
        assert not _said(game, "locked")

    _run(_go)


# ===========================================================================
# P0-3 / B4 — a mid-window META request never binds, never owns the turn
# ===========================================================================


@pytest.mark.parametrize("text,kind", [
    (LIVE_MC_REQUEST, "choices"),
    ("give me options", "choices"),
    ("what were the options", "choices"),
    ("make this one multiple choice", "choices"),
    ("can I get a hint", "hint"),
    ("repeat the question", "repeat"),
    ("say that again", "repeat"),
    (LIVE_KEEP, "keep"),
    ("keep it as it is", "keep"),
])
def test_meta_request_classes(text, kind):
    assert lily_scorekeeper.lily_detect_meta_request(text) == kind


@pytest.mark.parametrize("text", ["Paris", "1969", "I think it's Wilde", "a"])
def test_answers_are_not_meta_requests(text):
    assert lily_scorekeeper.lily_detect_meta_request(text) is None


def test_mc_request_on_a_numeric_card_builds_options_and_reasks():
    """kb_318 at 11:48:55Z: the request is never a candidate, the organic
    lane is NOT suppressed (no ownership), the directive is armed, and the
    choices-on-demand lane puts four options on the live card and re-asks
    the same question as MC with the window still open."""
    game = _dispute_game()
    at = _arm(game, Q_NUMERIC, window=30.0)

    def _go():
        res = game.sk.on_transcript_segment(
            text=LIVE_MC_REQUEST, speaker_label="S1", is_final=True,
            now=at, segment_start_time=at, segment_end_time=at + 1.5,
        )
        assert res["candidate_recorded"] is False
        assert res.get("meta_request") == "choices"
        game.on_transcript_event(
            res, LIVE_MC_REQUEST, speaker_label="S1", segment_ts=at
        )
        assert game.sk.answer_candidates == {}
        assert game._explain_request_note  # the one-line directive

    _run(_go, settle_ticks=60)
    choices = game.sk.current_question.get("choices")
    assert isinstance(choices, list) and len(choices) == 4
    assert "1969" in choices
    assert len(set(choices)) == 4
    assert game.sk.answer_window_open is True
    reask = _said(game, "now with options")
    assert reask and "1969" in reask[0]
    assert "already live" not in " ".join(game.session.said).lower()
    timer = getattr(game, "_window_timer", None)
    if timer is not None:
        timer.cancel()
    # Ownership: the meta request never owns the turn.
    assert _drive_prehook(game, LIVE_MC_REQUEST) is False
    # ...and the new options now resolve a letter on the same window.
    r = lily_evaluation.lily_tier1_evaluate_question(
        "I'd say b", game.sk.current_question
    )
    assert r["selected_index"] == 1


def test_mc_request_on_a_freeform_card_uses_the_reasoning_node():
    game = _dispute_game()
    calls = []

    class _Reasoning:
        async def ensure_choices(self, question):
            calls.append(question.get("id"))
            question["choices"] = ["Oscar Wilde", "G. B. Shaw", "Yeats", "Beckett"]

        async def judge(self, *a, **k):
            return '{"verdict": "incorrect", "reason": "x"}'

    game.reasoning = _Reasoning()
    at = _arm(game, Q_FREEFORM, window=30.0)

    def _go():
        _final(game, "give me some options", at)
        assert game.sk.answer_candidates == {}

    _run(_go, settle_ticks=60)
    assert calls == ["kb_wilde2"]
    assert game.sk.current_question["choices"][0] == "Oscar Wilde"
    assert _said(game, "now with options")
    timer = getattr(game, "_window_timer", None)
    if timer is not None:
        timer.cancel()


def test_mc_request_on_a_card_with_choices_arms_the_directive_only():
    game = _dispute_game()
    at = _arm(game, Q_MC, window=30.0)

    def _go():
        _final(game, LIVE_MC_REQUEST, at)
        assert game.sk.answer_candidates == {}
        note = game._explain_request_note or ""
        assert "A) Earth" in note and "D) Jupiter" in note
        assert not _said(game, "now with options")

    _run(_go)
    assert _drive_prehook(game, LIVE_MC_REQUEST) is False


def test_synthesis_failure_airs_the_honest_free_answer_line():
    game = _dispute_game()

    class _Reasoning:
        async def ensure_choices(self, question):
            question.pop("choices", None)

        async def judge(self, *a, **k):
            return "{}"

    game.reasoning = _Reasoning()
    at = _arm(game, Q_FREEFORM, window=30.0)
    _run(lambda: _final(game, "options please", at), settle_ticks=60)
    assert _said(game, "free answer")
    assert "choices" not in game.sk.current_question


def test_hint_and_repeat_requests_arm_directives_and_never_bind():
    game = _dispute_game()
    at = _arm(game, Q_MC, window=30.0)

    def _go():
        _final(game, "can I get a hint", at)
        assert game.sk.answer_candidates == {}
        assert "hint" in (game._explain_request_note or "").lower()
        _final(game, "repeat the question", at + 2)
        assert game.sk.answer_candidates == {}
        assert "repeat" in (game._explain_request_note or "").lower()
        assert "D) Jupiter" in game._explain_request_note

    _run(_go)


# ===========================================================================
# B3 — relaxed pacing kills the timer on the confirm / keep / prefs paths
# ===========================================================================


def test_keep_it_like_it_is_reasserts_the_relaxed_usual_and_kills_the_clock():
    """11:48:17Z: "Let's keep it like it is" with relaxed on file and a
    timed window running was bound as an ANSWER and the clock burned the
    question a second later."""
    game = _dispute_game()
    game.prefs = {"pacing": "relaxed"}
    at = _arm(game, Q_FREEFORM, window=20.0, pacing="timed")
    assert game.sk.answer_window_deadline is not None

    def _go():
        _final(game, LIVE_KEEP, at)
        assert game.sk.answer_candidates == {}
        assert game.sk.pacing == "relaxed"
        assert game.sk.answer_window_deadline is None
        assert game.sk.answer_window_open is True
        assert any(
            "keep things as they are" in i for i in game.session.instructions
        )

    _run(_go)
    assert _drive_prehook(game, LIVE_KEEP) is True  # the pacing_kept ack owns it


def test_keep_it_answers_a_pending_switch_as_no():
    game = _dispute_game()
    game.prefs = {"pacing": "relaxed"}
    game._pacing_stated_this_session = True
    at = _arm(game, Q_FREEFORM, window=20.0, pacing="relaxed")

    def _go():
        _final(game, "put the timer back on", at)
        assert game._pending_pacing == "timed"
        _final(game, LIVE_KEEP, at + 2)
        assert game._pending_pacing is None
        assert game.sk.pacing == "relaxed"
        assert game.sk.answer_window_deadline is None

    _run(_go)


def test_prefs_applied_at_game_start_convert_an_open_timed_window():
    game = _dispute_game()
    game.prefs = {"pacing": "relaxed"}
    _arm(game, Q_FREEFORM, window=20.0, pacing="timed")
    assert game.sk.answer_window_deadline is not None

    def _go():
        game.apply_prefs_at_game_start()
        assert game.sk.pacing == "relaxed"
        assert game.sk.answer_window_deadline is None

    _run(_go)


# ===========================================================================
# B5 — any start phrase starts
# ===========================================================================


@pytest.mark.parametrize("text", [
    "ready", "Ready.", "let's go", "get the show on the road",
    "I'm ready whenever you are", "ready to start", "Ready to start?",
    "we're ready", "let's get started", "let's do this",
])
def test_start_phrases_detected(text):
    assert lily_scorekeeper.lily_detect_control_command(text) == "start_game"


@pytest.mark.parametrize("text", [
    "are you ready to start?", "not ready yet", "we're ready to order",
    "let's go get a drink first", "Lily, are we ready?",
])
def test_start_guards_still_hold(text):
    assert lily_scorekeeper.lily_detect_control_command(text) != "start_game"


@pytest.mark.parametrize("text", [
    "get the show on the road", "I'm ready whenever you are", "Ready to start?",
])
def test_start_phrases_start_the_game_with_no_lock_the_table_beat(text):
    g = _lobby_game()

    async def _go():
        R._segment(g, text)
        await _settle()
        assert g.game_started is True
        assert not _said(g, "locking the table")

    asyncio.run(_go())


# ===========================================================================
# C1 — a STOP over a pending restart confirm drops the ask silently
# ===========================================================================


def test_stop_over_restart_confirm_drops_the_ask_and_a_later_yes_does_not_wipe():
    g = R._live_game()

    async def _go():
        await R._drive(g, "Lily, restart the game")
        assert g.restart_confirm_pending() is True
        sid = _sid(g, "restart_confirm")
        g.note_speech_handle(R._Handle(sid))  # the framework's speech_created
        await R._drive(g, "Lily, stop")
        assert g.restart_confirm_pending() is False
        assert len(_said(g, "scores gone")) == 1  # never re-asked
        assert not _said(g, "didn't catch a yes")  # dropped silently
        await R._drive(g, "yes")
        assert g.game_started is True
        assert g.sk.ledger_scores()["Rami"] == 2

    asyncio.run(_go())


# ===========================================================================
# C2 — an unconsumed code-ack mark cannot own a later turn
# ===========================================================================


def test_unconsumed_yes_mark_does_not_own_a_later_turn():
    game = _airgate_game()
    game.note_user_final()                      # final 1: "yes" (restart)
    game.mark_deterministic_reply("yes")       # ack uninterruptible → commit skipped
    game.note_user_final()                      # final 2
    game.note_user_final()                      # final 3: "yes let's go again"
    assert game.consume_deterministic_reply("yes let's go again") is False


def test_unconsumed_no_mark_does_not_own_no_stop_it_i_know_this_one():
    game = _airgate_game()
    game.note_user_final()
    game.mark_deterministic_reply("no")        # restart declined, uninterruptible
    game.note_user_final()
    game.note_user_final()
    assert game.consume_deterministic_reply("no stop it I know this one") is False


def test_unconsumed_hold_mark_does_not_own_a_later_answer():
    game = _airgate_game()
    game.note_user_final()
    game.mark_deterministic_reply("hold on")
    game.note_user_final()
    game.note_user_final()
    assert game.consume_deterministic_reply("hold on I know this one Saturn") is False


def test_joined_turn_still_consumes_the_mark_of_its_first_final():
    """Invariance (audit R5): "I don't want a timer." + "it stresses me
    out" are two finals and ONE commit that lands after the second final
    — the first final's mark must survive exactly one more final."""
    game = _airgate_game()
    game.note_user_final()
    game.mark_deterministic_reply("I don't want a timer.")
    game.note_user_final()
    assert game.consume_deterministic_reply(
        "I don't want a timer. it stresses me out"
    ) is True


# ===========================================================================
# C3 — the addressing turn is not cut by the result gate
# ===========================================================================


def _aired_result(game, qnum):
    game._result_aired = {
        "qnum": qnum, "speech_id": "speech_verdict",
        "text": "the femur", "at": time.monotonic(),
    }
    game._resolve_result_narration = lambda text: qnum


def test_keyless_restatement_is_suppressed_without_a_contest():
    game = _airgate_game()
    _aired_result(game, 1)
    assert game.result_narration_already_aired(
        "Right — it was the femur.", speech_id="speech_reply"
    ) == "suppress"


def test_addressing_turn_may_restate_the_ruling_under_a_contest():
    game = _airgate_game()
    _aired_result(game, 1)
    game.arm_contest_note(reason="test")
    assert game.result_narration_already_aired(
        "You're right — it was the femur, and the point stands.",
        speech_id="speech_reply",
    ) is None
    game2 = _airgate_game()
    _aired_result(game2, 1)
    game2._dispute_hold_since = time.time()
    assert game2.result_narration_already_aired(
        "It was the femur — let me look at that again.", speech_id="speech_x"
    ) is None


# ===========================================================================
# C4 — the W2 obligation lines are exempt from freshness and the barge flush
# ===========================================================================


@pytest.mark.parametrize("act", [
    "restart_confirm_dropped", "dispute_timeout_ack", "start_settle_override",
])
def test_w2_obligation_lines_survive_a_barge_flush(act):
    game = _airgate_game()
    game.note_user_final()
    handle = _Handle("speech_obl")
    game.note_speech_handle(handle)
    game._dispatched_act_by_speech["speech_obl"] = act
    game._user_speech_started_at = time.monotonic() + 100  # queued before the barge
    flushed = game.flush_queued_dispatches_on_barge(cut_speech_id=None)
    assert "speech_obl" not in flushed
    assert handle.interrupts == []
    assert act in lily_speech_delivery._FRESHNESS_EXEMPT_ACTS


# ===========================================================================
# C5 — the contest detector's format hint
# ===========================================================================


def test_contest_multiple_choice_hint_reads_the_card():
    game = _airgate_game()
    assert game.contest_multiple_choice_hint() is None
    game.sk.current_question = dict(Q_FREEFORM)
    assert game.contest_multiple_choice_hint() is False
    game.sk.current_question = dict(Q_MC)
    assert game.contest_multiple_choice_hint() is True
    game.sk.current_question = None
    game._last_adjudicated_question = dict(Q_MC)
    assert game.contest_multiple_choice_hint() is True


def test_bare_answer_is_a_is_a_contest_only_on_a_multiple_choice_card():
    assert lily_scorekeeper.lily_detect_verdict_contest(
        "the answer is A", multiple_choice=True
    ) is True
    assert lily_scorekeeper.lily_detect_verdict_contest(
        "the answer is a", multiple_choice=False
    ) is False


# ===========================================================================
# C6 — migration numbering
# ===========================================================================


def test_migration_prefixes_are_unique_and_the_retirement_script_is_028():
    root = Path(__file__).resolve().parent.parent / "migrations"
    names = sorted(p.name for p in root.glob("*.sql"))
    prefixes = [n[:3] for n in names]
    assert len(prefixes) == len(set(prefixes)), names
    assert "028_retire_ecapa_v1_room_tone_centroids.sql" in names
    assert "027_retire_ecapa_v1_room_tone_centroids.sql" not in names
    body = (root / "028_retire_ecapa_v1_room_tone_centroids.sql").read_text()
    assert "lily_speaker_voiceprints" in body  # HOTFIX-ENGINE-LABEL-001 block carried


# ===========================================================================
# C8 — a raising voice receipt does not take the metadata payload down
# ===========================================================================


def test_session_metadata_survives_a_raising_voice_receipt():
    class _Game:
        def voice_identity_receipt(self):
            raise RuntimeError("probe exploded")

    payload = lily_agent.lily_session_metadata(_Game(), LilyScorekeeper("t"), {}, None)
    assert payload["voice_identity"]["outcome"].startswith("receipt_failed:")
    assert "question_timeline" in payload


# ===========================================================================
# C10 — the voiced-slice resample runs off the event loop
# ===========================================================================


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _feed(probe, clock, seconds, rate):
    frame = int(rate * 0.01)
    for _ in range(int(seconds * 100)):
        clock.t += 0.01
        probe.add_frame([100] * frame, sample_rate=rate)


def test_resample_lands_off_the_loop_and_calls_back():
    clock = _Clock()
    seen = {"threads": [], "landed": []}
    import threading

    def _resampler(samples, in_rate):
        seen["threads"].append(threading.current_thread() is threading.main_thread())
        step = in_rate // 16000
        return samples[::step]

    probe = ve.LilyVoiceProbe(clock=clock, resampler=_resampler, min_voiced_seconds=1.0)
    probe.on_voiced_landed = lambda added: seen["landed"].append(added)

    async def _go():
        _feed(probe, clock, 4.0, 48000)
        queued = probe.note_voiced_segment(clock.t - 3.0, clock.t - 1.0)
        assert queued > 0
        assert probe.pending_slices == 1
        assert probe.voiced_seconds == 0.0  # counted when it LANDS
        for _ in range(50):
            if probe.pending_slices == 0:
                break
            await asyncio.sleep(0.01)
        assert probe.pending_slices == 0
        assert probe.voiced_seconds == pytest.approx(2.0, abs=0.05)
        assert seen["landed"] and seen["landed"][0] == pytest.approx(2.0, abs=0.05)
        assert seen["threads"] == [False]  # ran on a worker thread
        assert probe.receipt()["landed_slices"] == 1

    asyncio.run(_go())


def test_resample_is_synchronous_without_a_running_loop():
    clock = _Clock()
    probe = ve.LilyVoiceProbe(
        clock=clock, resampler=lambda s, r: s[:: r // 16000], min_voiced_seconds=1.0
    )
    _feed(probe, clock, 4.0, 48000)
    added = probe.note_voiced_segment(clock.t - 3.0, clock.t - 1.0)
    assert added == pytest.approx(2.0, abs=0.05)
    assert probe.voiced_seconds == pytest.approx(2.0, abs=0.05)
    assert probe.pending_slices == 0


# ===========================================================================
# L1 — the endpointing ceiling
# ===========================================================================


def test_endpointing_ceiling_is_two_and_a_half_seconds(monkeypatch):
    monkeypatch.delenv("LILY_STT_MAX_ENDPOINTING_DELAY", raising=False)
    assert lily_config.stt_max_endpointing_delay() == 2.5
    monkeypatch.setenv("LILY_STT_MAX_ENDPOINTING_DELAY", "3.5")
    assert lily_config.stt_max_endpointing_delay() == 3.5


# ===========================================================================
# L2 — the per-turn end-of-turn receipt
# ===========================================================================


def _collector():
    c = lily_metrics.LilyMetricsCollector()
    c.bind_endpointing_bounds(lambda: (0.6, 2.5))
    return c


def _user_report(stopped, td, eot, outc=0.02):
    return {
        "started_speaking_at": stopped - 1.0,
        "stopped_speaking_at": stopped,
        "transcription_delay": td,
        "end_of_turn_delay": eot,
        "on_user_turn_completed_delay": outc,
    }


@pytest.mark.parametrize("td,eot,reason", [
    (0.4, 2.5, "max_delay"),
    (0.4, 0.45, "stt_final"),
    (0.1, 0.6, "min_delay"),
    (0.2, 1.5, "other"),
])
def test_commit_reason_classification(td, eot, reason):
    c = _collector()
    c.collect_turn(_user_report(100.0, td, eot))
    turns = c.summary()["turn_taking"]["turns"]
    assert len(turns) == 1
    entry = turns[0]
    assert entry["commit_reason"] == reason
    assert entry["stt_final_at"] == pytest.approx(100.0 + td)
    assert entry["commit_at"] == pytest.approx(100.0 + eot)
    assert entry["vad_end_of_speech_at"] == 100.0
    for key in ("eot_probability", "eot_threshold", "eot_model", "eot_source"):
        assert key in entry
    assert entry["eot_probability"] is None
    assert entry["eot_source"] == "tap_not_attached"
    assert c.summary()["turn_taking"]["commit_reasons"] == {reason: 1}


@pytest.fixture
def framework_logger():
    lk = logging.getLogger("livekit.agents")
    before_level = lk.level
    before_filters = list(lk.filters)
    root = logging.getLogger()
    root_filters = {id(h): list(h.filters) for h in root.handlers}
    yield lk
    lk.setLevel(before_level)
    lk.filters[:] = before_filters
    for h in root.handlers:
        if id(h) in root_filters:
            h.filters[:] = root_filters[id(h)]


def test_eot_tap_captures_the_debug_records_without_the_c12_capture(framework_logger):
    """The tap alone (NO enable_preemptive_used_capture) sets DEBUG, sees
    the framework's records, and stamps the turn — units as emitted."""
    lk = framework_logger
    lk.setLevel(logging.WARNING)
    c = _collector()
    c.bind_turn_detector(lambda: "stt")
    c.attach_eot_tap()
    assert lk.isEnabledFor(logging.DEBUG)
    lk.debug("eot prediction", extra={
        "probability": 0.005690, "unlikely_threshold": 0.56,
        "endpointing_delay": 2.5, "language": "en", "trigger": "vad",
        "from_cache": False,
    })
    lk.debug("user turn committed", extra={
        "last_speaking_time": 100.02, "last_final_transcript_time": 100.4,
        "speech_start_time": 99.0, "delay_completed": True, "source": "vad",
        "end_of_turn_probability": 0.005690, "unlikely_threshold": 0.56,
    })
    c.collect_turn(_user_report(100.0, 0.4, 2.5))
    tt = c.summary()["turn_taking"]
    entry = tt["turns"][0]
    assert entry["eot_probability"] == pytest.approx(0.00569)
    assert isinstance(entry["eot_probability"], float)
    assert entry["eot_threshold"] == 0.56
    assert entry["eot_source"] == "debug_record"
    assert entry["from_cache"] is False
    assert entry["endpointing_delay"] == 2.5
    assert entry["commit_trigger"] == "vad"
    assert entry["eot_model"] == "stt"
    assert tt["eot_tap_attached"] is True
    assert tt["eot_tap_level"] == "DEBUG"
    # A turn with no record keeps the keys, null + a source name.
    c.collect_turn(_user_report(140.0, 0.3, 0.35))
    entry2 = c.summary()["turn_taking"]["turns"][1]
    assert entry2["eot_probability"] is None
    assert entry2["eot_threshold"] is None
    assert entry2["eot_source"] == "no_debug_record"


def test_eot_tap_counts_timeouts_and_never_raises_the_level(framework_logger):
    lk = framework_logger
    lk.setLevel(logging.DEBUG)
    c = _collector()
    c.attach_eot_tap()
    assert lk.level == logging.DEBUG
    lk.warning("eot prediction timed out, committing without a prediction", extra={"timeout": 3.0})
    lk.debug("user turn committed", extra={"last_speaking_time": 50.0, "source": "vad"})
    c.collect_turn(_user_report(50.1, 0.2, 0.25))
    tt = c.summary()["turn_taking"]
    assert tt["eot_prediction_timeouts"] == 1
    assert tt["turns"][0]["eot_source"] == "prediction_timed_out"


def test_eot_tap_shield_keeps_the_debug_flood_out_of_root_handlers(framework_logger):
    lk = framework_logger
    lk.setLevel(logging.WARNING)
    seen = []

    class _Sink(logging.Handler):
        def emit(self, record):
            seen.append((record.name, record.levelno))

    sink = _Sink(level=logging.DEBUG)
    root = logging.getLogger()
    root.addHandler(sink)
    try:
        c = _collector()
        c.attach_eot_tap()
        lk.debug("eot prediction", extra={"probability": 0.5})
        lk.info("something at info")
        assert ("livekit.agents", logging.DEBUG) not in seen
        assert ("livekit.agents", logging.INFO) in seen
    finally:
        root.removeHandler(sink)


def test_turn_list_is_bounded():
    c = _collector()
    for i in range(230):
        c.collect_turn(_user_report(1000.0 + i, 0.2, 0.25))
    tt = c.summary()["turn_taking"]
    assert len(tt["turns"]) == 200
    assert tt["turns"][0]["stopped_speaking_at"] == 1030.0
    assert tt["commit_reasons"]["stt_final"] == 230
    assert tt["end_of_turn_delay_ms_p95"] == 250.0


# ===========================================================================
# L3 — the continuity rails (operator wording, verbatim)
# ===========================================================================


def test_continuity_rails_carry_the_operator_wording():
    body = LILY_SYSTEM_PROMPT.split("<continuity>", 1)[1].split("</continuity>", 1)[0]
    assert (
        "A first welcome-back is\nowed ONLY when identity is CONFIRMED (voice "
        "match or the player gave\ntheir name this session). On a device-guess "
        "or partial history, nothing\nis owed — open as a fresh table."
    ) in body
    assert (
        "The name question stands\nalone on its own turn. Who-else-is-here and "
        "the fun fact are separate\nbeats on later turns. Never fold two of "
        "them into one breath, and never\njoin them with 'or'."
    ) in body
    assert "joined with \"or\"" not in body
    assert "never a repeat" not in body
    assert LILY_SYSTEM_PROMPT.count("CONTINUITY PROTOCOL") == 1


# ===========================================================================
# C7 — the act names exist (define only)
# ===========================================================================


def test_c7_act_and_metadata_names_are_defined():
    assert lily_acts.ACT_RESTART_CONFIRM_DROPPED == "restart_confirm_dropped"
    assert lily_acts.ACT_DISPUTE_TIMEOUT_ACK == "dispute_timeout_ack"
    assert lily_acts.ACT_START_SETTLE_OVERRIDE == "start_settle_override"
    assert lily_acts.META_AIRGATE_EVENTS == "airgate_events"
    assert lily_acts.META_CONFIG_SNAPSHOT == "config_snapshot"
