"""Unit + golden tests for the tts_node speech pipeline (REFACTOR W1b).

tts_node's ~600 lines of sequential surgery were extracted into 17 named
SpeechTransform stages run in order by run_say_pipeline(). These tests prove
each stage is independently testable WITHOUT constructing a LilyAgent (a light
fake game / fake agent is enough), and a golden test drives representative
turns end-to-end through the real pipeline, asserting the byte-preserved
outcome (rewrite text / Silence reason / scheduled regen).

The behavioral equivalents of each stage (the lily_say_gate / lily_scorekeeper
pure helpers, and the LilyGame decision methods) already have their own suites;
these tests pin the WIRING — order, text threading, suppression funnel, the
uniform TRANSFORM log — that the refactor introduced.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_agent
from lily_agent import (
    AirDupGuard,
    BackHoldNarration,
    DeliveryClaim,
    DisputeSycophancyRewrite,
    EmptyCandidateRetry,
    FalseEmptyRewrite,
    HygieneClean,
    LeakFilter,
    OnScreenClaimRewrite,
    PunctuationFlush,
    RegenGate,
    RepeatLints,
    RevealDeliveryFusionClip,
    SAY_PIPELINE,
    ScoreLineGate,
    Silence,
    SpeechTransform,
    SpeechTurn,
    TransitionNarration,
    UnownedKickoffSuppress,
    YieldAfterFirstQuestion,
    run_say_pipeline,
)


# --- lightweight fakes (no LilyAgent / LilyGame construction) -----------------

class FakeSK:
    def __init__(self, **kw):
        self.session_id = "pipe-test"
        self.question_number = 3
        self.answer_window_open = False
        self.agent_turns = []
        self._ledger_scores = {}
        self._ledger_streaks = {}
        self.__dict__.update(kw)

    def ledger_scores(self):
        return self._ledger_scores

    def ledger_streaks(self):
        return self._ledger_streaks


class FakeRegistry:
    def __init__(self, released=None):
        self._released = released or []
        self.release_owner_calls = []
        self.release_pending_calls = 0

    def release_owner(self, speech_id):
        self.release_owner_calls.append(speech_id)
        return list(self._released)

    def release_pending(self):
        self.release_pending_calls += 1
        return list(self._released)


class FakeGame:
    """A stand-in exposing exactly the surface the pipeline touches. Every
    decision method defaults to the pass-through / no-op answer; a test flips
    only what it needs."""

    def __init__(self, **kw):
        self.sk = FakeSK()
        self.say_registry = FakeRegistry()
        self.armed_question = None
        self.game_started = False
        self._recognition_dispute = False
        self._recognition_dispute_why_answered = False
        self._returner_claim_seen = False
        self.on_answer_leak_calls = 0
        self.expect_delivery_calls = 0
        self.back_hold_narration_arg = None
        self._resume = None
        self._delivery_result = "none"
        self._transition_result = "none"
        self._air_dup = False
        self._unowned = False
        self._transition_awaiting = False
        self._must_rewrite_false_empty = False
        self._false_on_screen_confirmed = True
        self._false_on_screen_failed = False
        self._should_regen = False
        self._is_question_delivery = False
        self._rendered_armed = "The armed sheet question?"
        self.__dict__.update(kw)

    # leak / hygiene
    def on_answer_leak(self):
        self.on_answer_leak_calls += 1

    # reveal / delivery fusion
    def transition_awaiting_delivery(self):
        return self._transition_awaiting

    # false empty
    def must_rewrite_false_empty_claim(self, text):
        return self._must_rewrite_false_empty

    def identity_probe_outstanding(self):
        return False

    # on-screen
    def picture_on_glass_confirmed(self):
        return self._false_on_screen_confirmed

    def picture_on_glass_failed(self):
        return self._false_on_screen_failed

    # yield / repeat
    def is_question_delivery_turn(self, text):
        return self._is_question_delivery

    # regen
    def reair_verbatim_should_regenerate(self, text, repeat_kind):
        return self._should_regen

    # empty candidate
    def expect_delivery(self):
        self.expect_delivery_calls += 1

    def rendered_armed_question(self):
        return self._rendered_armed

    # back hold
    def back_hold_narration(self, text):
        self.back_hold_narration_arg = text

    # delivery claim
    def take_pending_delivery_resume(self):
        return self._resume

    def register_delivery_claim(self, text, speech_id=None):
        return self._delivery_result

    # unowned kickoff
    def unowned_kickoff_must_suppress(self, text, delivery):
        return self._unowned

    def start_blocked_reason(self):
        return "no_delivery_owner"

    # transition narration
    def register_transition_narration(self, text, speech_id=None):
        return self._transition_result

    # air dup
    def air_dup_guard(self, text, delivery):
        return self._air_dup


class FakeSession:
    def __init__(self):
        self.reply_calls = []

    def generate_reply(self, instructions=None):
        self.reply_calls.append(instructions)
        # return a sentinel coroutine-ish object; the pipeline stage only
        # stores the factory, it does not await it here.
        return ("generate_reply", instructions)


class FakeAgent:
    def __init__(self):
        self.session = FakeSession()
        self._reair_regen_pending = False
        self._empty_retry_pending = False


def make_turn(text, game=None, agent=None, speech_id="sp1", raw=None):
    game = game or FakeGame()
    agent = agent or FakeAgent()
    return SpeechTurn(
        text=text,
        raw=raw if raw is not None else text,
        game=game,
        agent=agent,
        speech_id=speech_id,
    )


# --- registry / structural pins ----------------------------------------------

def test_pipeline_stage_order_is_fixed():
    assert [t.name for t in SAY_PIPELINE] == [
        "leak_filter",
        "hygiene_clean",
        "reveal_delivery_fusion_clip",
        "score_line_gate",
        "false_empty_rewrite",
        "on_screen_claim_rewrite",
        "dispute_sycophancy_rewrite",
        "yield_after_first_question",
        "repeat_lints",
        "regen_gate",
        "empty_candidate_retry",
        "back_hold_narration",
        "delivery_claim",
        "unowned_kickoff_suppress",
        "transition_narration",
        "air_dup_guard",
        # WO-LILY-AIRGATE-001: the dequeue-time airing gate — the last
        # content decision before the frames yield.
        "result_aired_gate",
        "freshness_gate",
        "punctuation_flush",
    ]


def test_every_stage_is_a_speech_transform_with_a_name():
    for t in SAY_PIPELINE:
        assert isinstance(t, SpeechTransform)
        assert isinstance(t.name, str) and t.name


# --- per-transform unit tests -------------------------------------------------

def test_leak_filter_passthrough_when_clean():
    turn = make_turn("A perfectly clean line.")
    out = LeakFilter().apply(turn)
    assert out is turn
    assert turn.text == "A perfectly clean line."
    assert turn.game.on_answer_leak_calls == 0


def test_leak_filter_burns_on_answer_bearing_leak():
    # A bracketed metadata line is a leak that could carry answer material.
    leaked = "[GAME STATE] the answer is Ottawa\nOttawa is the capital."
    turn = make_turn(leaked)
    LeakFilter().apply(turn)
    # burn protocol fired (answer-bearing leak) and the metadata was stripped
    assert turn.game.on_answer_leak_calls == 1
    assert "[GAME STATE]" not in turn.text


def test_hygiene_clean_strips_markdown_keeps_audio_tags():
    turn = make_turn("that is **exactly** right [whispering]")
    HygieneClean().apply(turn)
    assert turn.text == "that is exactly right [whispering]"


def test_reveal_delivery_fusion_clip_only_when_awaiting():
    turn = make_turn("Crete. Next up: what was the agoge?",
                     game=FakeGame(_transition_awaiting=False))
    RevealDeliveryFusionClip().apply(turn)
    assert turn.text == "Crete. Next up: what was the agoge?"


def test_score_line_gate_passthrough_with_no_ledger():
    turn = make_turn("Nice one.")
    ScoreLineGate().apply(turn)
    assert turn.text == "Nice one."


def test_false_empty_rewrite_replaces_when_flagged():
    turn = make_turn("No saved stats — clean slate!",
                     game=FakeGame(_must_rewrite_false_empty=True))
    FalseEmptyRewrite().apply(turn)
    assert "clean slate" not in turn.text.lower()
    assert turn.text  # rewritten to the still-checking line


def test_on_screen_claim_pending_rewrite():
    turn = make_turn("Look at the screen — the picture is up.",
                     game=FakeGame(_false_on_screen_confirmed=False,
                                   _false_on_screen_failed=False))
    OnScreenClaimRewrite().apply(turn)
    assert turn.text != "Look at the screen — the picture is up."


def test_on_screen_claim_confirmed_passes_through():
    turn = make_turn("Look at the screen — the picture is up.",
                     game=FakeGame(_false_on_screen_confirmed=True))
    OnScreenClaimRewrite().apply(turn)
    assert turn.text == "Look at the screen — the picture is up."


def test_dispute_sycophancy_rewrite_when_mirroring_open_dispute():
    turn = make_turn("You are so right, my mistake.",
                     game=FakeGame(_recognition_dispute=True,
                                   _recognition_dispute_why_answered=False))
    DisputeSycophancyRewrite().apply(turn)
    assert "protocol" in turn.text  # answered the why


def test_yield_after_first_question_stashes_n_questions():
    turn = make_turn("What is the capital of France? And also, how are you?")
    YieldAfterFirstQuestion().apply(turn)
    assert turn.n_questions >= 1


def test_yield_exempt_for_mc_delivery():
    armed = {"choices": ["A", "B", "C"]}
    game = FakeGame(armed_question=armed, _is_question_delivery=True)
    turn = make_turn("Pick one: A? B? C?", game=game)
    before = turn.text
    YieldAfterFirstQuestion().apply(turn)
    assert turn.text == before  # MC delivery not clipped


def test_repeat_lints_are_log_only():
    turn = make_turn("A one-off line.")
    before = turn.text
    RepeatLints().apply(turn)
    assert turn.text == before
    assert turn.repeat_kind in (None, "", False) or isinstance(turn.repeat_kind, str)


def test_regen_gate_passthrough_when_not_a_reair():
    game = FakeGame(_should_regen=False)
    turn = make_turn("fresh content", game=game)
    out = RegenGate().apply(turn)
    assert out is turn
    assert turn.agent._reair_regen_pending is False


def test_regen_gate_suppresses_and_schedules_on_reair():
    game = FakeGame(_should_regen=True)
    turn = make_turn("verbatim replay", game=game)
    turn.repeat_kind = "verbatim"
    out = RegenGate().apply(turn)
    assert isinstance(out, Silence)
    assert out.reason == "regen_reair"
    assert out.schedule is not None
    assert turn.agent._reair_regen_pending is True
    # the suppressed handle is recorded and the owner released
    assert "sp1" in game._suppressed_speech_ids
    assert game.say_registry.release_owner_calls == ["sp1"]
    # scheduling actually calls generate_reply with the fresh-words directive
    out.schedule()
    assert turn.agent.session.reply_calls and turn.agent.session.reply_calls[0]


def test_regen_gate_stubborn_repeat_suppresses_without_schedule():
    game = FakeGame(_should_regen=False, _is_question_delivery=False)
    agent = FakeAgent()
    agent._reair_regen_pending = True
    turn = make_turn("still the same", game=game, agent=agent)
    turn.repeat_kind = "verbatim"
    out = RegenGate().apply(turn)
    assert isinstance(out, Silence)
    assert out.reason == "stubborn_repeat"
    assert out.schedule is None
    assert agent._reair_regen_pending is False


def test_empty_candidate_first_empty_schedules_retry():
    game = FakeGame()
    turn = make_turn("", game=game)
    out = EmptyCandidateRetry().apply(turn)
    assert isinstance(out, Silence)
    assert out.reason == "empty_candidate_retry"
    assert out.schedule is not None
    assert turn.agent._empty_retry_pending is True


def test_empty_candidate_second_empty_forces_armed_sheet():
    game = FakeGame(game_started=True, armed_question={"q": "x"},
                    _rendered_armed="The armed sheet question?")
    game.sk.answer_window_open = False
    agent = FakeAgent()
    agent._empty_retry_pending = True
    turn = make_turn("", game=game, agent=agent)
    out = EmptyCandidateRetry().apply(turn)
    assert out is turn  # falls through with the forced sheet
    assert turn.text == "The armed sheet question?"
    assert game.expect_delivery_calls == 1
    assert agent._empty_retry_pending is False


def test_empty_candidate_second_empty_no_sheet_gives_up():
    game = FakeGame(game_started=False)
    agent = FakeAgent()
    agent._empty_retry_pending = True
    turn = make_turn("", game=game, agent=agent)
    out = EmptyCandidateRetry().apply(turn)
    assert isinstance(out, Silence)
    assert out.reason == "empty_candidate_giveup"


def test_empty_candidate_resets_pending_on_normal_length():
    agent = FakeAgent()
    agent._empty_retry_pending = True
    turn = make_turn("a normal reply", agent=agent)
    out = EmptyCandidateRetry().apply(turn)
    assert out is turn
    assert agent._empty_retry_pending is False


def test_back_hold_narration_is_state_only():
    turn = make_turn("Stopped until you say go.")
    before = turn.text
    BackHoldNarration().apply(turn)
    assert turn.text == before
    assert turn.game.back_hold_narration_arg == before


def test_delivery_claim_resume_replaces_text_before_claim():
    game = FakeGame(_resume="Remaining options: B, or C?",
                    _delivery_result="claimed_structural")
    turn = make_turn("model prose that should be replaced", game=game)
    out = DeliveryClaim().apply(turn)
    assert out is turn
    assert turn.text == "Remaining options: B, or C?"
    assert turn.delivery == "claimed_structural"


def test_delivery_claim_duplicate_suppresses_no_release():
    game = FakeGame(_delivery_result="duplicate")
    turn = make_turn("already delivered", game=game)
    out = DeliveryClaim().apply(turn)
    assert isinstance(out, Silence)
    assert out.reason == "delivery_duplicate"
    assert "sp1" in game._suppressed_speech_ids
    # duplicate/held path marks suppressed but does NOT release the owner
    assert game.say_registry.release_owner_calls == []


def test_delivery_claim_held_suppresses():
    game = FakeGame(_delivery_result="held")
    turn = make_turn("armed question under a hold", game=game)
    out = DeliveryClaim().apply(turn)
    assert isinstance(out, Silence)
    assert out.reason == "delivery_held"


def test_delivery_claim_rewrite_strict_substitutes_sheet():
    class G(FakeGame):
        def __init__(self):
            super().__init__()
            self._calls = 0

        def register_delivery_claim(self, text, speech_id=None):
            self._calls += 1
            return "rewrite_strict" if self._calls == 1 else "claimed_structural"

    game = G()
    turn = make_turn("loose paraphrase of the question", game=game)
    out = DeliveryClaim().apply(turn)
    assert out is turn
    assert turn.text == game._rendered_armed
    assert turn.delivery == "claimed_structural"


def test_unowned_kickoff_suppresses_and_releases():
    game = FakeGame(_unowned=True)
    turn = make_turn("Round two, let's do it!", game=game)
    turn.delivery = "none"
    out = UnownedKickoffSuppress().apply(turn)
    assert isinstance(out, Silence)
    assert out.reason == "unowned_kickoff"
    assert "sp1" in game._suppressed_speech_ids
    assert game.say_registry.release_owner_calls == ["sp1"]


def test_transition_narration_duplicate_suppresses():
    game = FakeGame(_transition_result="duplicate")
    turn = make_turn("No points on that one — the answer was Russia!", game=game)
    out = TransitionNarration().apply(turn)
    assert isinstance(out, Silence)
    assert out.reason == "transition_duplicate"
    assert "sp1" in game._suppressed_speech_ids
    assert game.say_registry.release_owner_calls == ["sp1"]


def test_transition_narration_passthrough_when_first():
    game = FakeGame(_transition_result="claimed")
    turn = make_turn("That's a point for Chris.", game=game)
    out = TransitionNarration().apply(turn)
    assert out is turn


def test_air_dup_guard_suppresses_verbatim_replay():
    game = FakeGame(_air_dup=True)
    turn = make_turn("my bad", game=game)
    turn.delivery = "none"
    out = AirDupGuard().apply(turn)
    assert isinstance(out, Silence)
    assert out.reason == "air_dup"
    assert game.say_registry.release_owner_calls == ["sp1"]


def test_punctuation_flush_appends_period():
    turn = make_turn("no terminal punctuation here")
    PunctuationFlush().apply(turn)
    assert turn.text.endswith(".")


def test_punctuation_flush_leaves_terminated_text():
    for ending in (".", "!", "?"):
        turn = make_turn("already terminated" + ending)
        PunctuationFlush().apply(turn)
        assert turn.text == "already terminated" + ending


# --- mark_suppressed funnel (GUARD_MAP chain F) -------------------------------

def test_mark_suppressed_creates_and_reuses_the_set():
    game = FakeGame()
    turn = make_turn("x", game=game)
    assert not hasattr(game, "_suppressed_speech_ids")
    turn.mark_suppressed()
    assert game._suppressed_speech_ids == {"sp1"}
    # idempotent / additive on a second call
    turn.mark_suppressed()
    assert game._suppressed_speech_ids == {"sp1"}


def test_mark_suppressed_noop_without_speech_id():
    game = FakeGame()
    turn = make_turn("x", game=game, speech_id=None)
    turn.mark_suppressed()
    assert getattr(game, "_suppressed_speech_ids", None) in (None, set())


# --- golden: representative turns end-to-end through run_say_pipeline ----------

def test_golden_clean_turn_flushes_and_speaks():
    turn = make_turn("that is exactly right")
    outcome = run_say_pipeline(turn)
    assert outcome is None  # speak
    assert turn.text == "that is exactly right."  # punctuation flushed


def test_golden_markdown_turn_cleaned_then_spoken():
    turn = make_turn("that is **exactly** right [whispering]")
    outcome = run_say_pipeline(turn)
    assert outcome is None
    assert turn.text == "that is exactly right [whispering]."


def test_golden_duplicate_delivery_is_silenced():
    game = FakeGame(_delivery_result="duplicate")
    turn = make_turn("the same question again", game=game)
    outcome = run_say_pipeline(turn)
    assert isinstance(outcome, Silence)
    assert outcome.reason == "delivery_duplicate"


def test_golden_empty_turn_schedules_retry():
    turn = make_turn("")
    outcome = run_say_pipeline(turn)
    assert isinstance(outcome, Silence)
    assert outcome.reason == "empty_candidate_retry"
    assert outcome.schedule is not None


def test_golden_unowned_kickoff_silenced_before_speaking():
    game = FakeGame(_unowned=True, _delivery_result="none")
    turn = make_turn("Round two, let's go!", game=game)
    outcome = run_say_pipeline(turn)
    assert isinstance(outcome, Silence)
    assert outcome.reason == "unowned_kickoff"


def test_golden_transform_log_emitted_for_replace(caplog):
    import logging
    caplog.set_level(logging.INFO, logger=lily_agent.logger.name)
    turn = make_turn("**bold** text")
    run_say_pipeline(turn)
    transform_lines = [
        r.getMessage() for r in caplog.records
        if "TRANSFORM | name=" in r.getMessage()
    ]
    assert any("name=hygiene_clean action=replace" in m for m in transform_lines)
    assert any("name=punctuation_flush action=replace" in m for m in transform_lines)


def test_golden_transform_log_emitted_for_suppress(caplog):
    import logging
    caplog.set_level(logging.INFO, logger=lily_agent.logger.name)
    game = FakeGame(_delivery_result="held")
    turn = make_turn("armed question under hold", game=game)
    run_say_pipeline(turn)
    transform_lines = [
        r.getMessage() for r in caplog.records
        if "TRANSFORM | name=" in r.getMessage()
    ]
    assert any("name=delivery_claim action=suppress" in m for m in transform_lines)
