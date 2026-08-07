"""WO-LILY-HOTFIX-004 (P0) — two adult-deck hard blockers.

Defect 1: the 18+ gate accepted "Should I verify?" (a question) as consent.
  Fix: a DETERMINISTIC explicit-consent utterance must be heard this session,
  in addition to the model's confirmed_all_18_plus flag.
Defect 2: a queued adult question re-aired after an apology + commitment not
  to. Fix: a STOP in the adult deck burns the armed + queued questions so
  the objected-to material can never re-air.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_scorekeeper
from lily_scorekeeper import lily_detect_age_consent, LilyScorekeeper
from lily_agent import LilyGame


# -- Defect 1: the consent detector -------------------------------------------


def test_the_live_failure_utterance_is_not_consent():
    # The exact 19:41-class failure: a question read as consent.
    assert lily_detect_age_consent("Should I verify?") is False
    assert lily_detect_age_consent("should i verify") is False


def test_questions_and_verification_talk_never_consent():
    for t in [
        "do you need to verify our age?",
        "how do we verify?",
        "are we all 18?",
        "is everyone over 18?",
        "what's the grown-up deck?",
        "do you want to check our age?",
    ]:
        assert lily_detect_age_consent(t) is False, t


def test_negations_never_consent():
    for t in [
        "we're not all 18",
        "one of us is under 18",
        "not sure everyone's 18",
        "no, we're not adults",
    ]:
        assert lily_detect_age_consent(t) is False, t


def test_explicit_affirmatives_are_consent():
    for t in [
        "yes, we're all 18",
        "we are all adults here",
        "yep, everyone's over 18",
        "confirmed, all 18 plus",
        "yes we're all of age",
        "we're all 18 and older",
        "absolutely, we are all adults",
    ]:
        assert lily_detect_age_consent(t) is True, t


# -- Defect 1: the agent gate requires BOTH flag and heard-consent ------------


def _game(mode="general"):
    g = LilyGame.__new__(LilyGame)
    g.sk = LilyScorekeeper("h4")
    g.sk.mode = mode
    g._age_consent_confirmed = False
    return g


def test_consent_latches_on_a_real_yes_not_a_question():
    g = _game()
    # Simulate the on_transcript_event latch logic.
    def hear(text):
        if not g._age_consent_confirmed and lily_scorekeeper.lily_detect_age_consent(text):
            g._age_consent_confirmed = True

    hear("Should I verify?")
    assert g._age_consent_confirmed is False  # question did NOT latch
    hear("yes, we're all 18 and want the grown-up deck")
    assert g._age_consent_confirmed is True   # real yes latched
    hear("anything else")  # stays latched
    assert g._age_consent_confirmed is True


# -- Defect 2: STOP in adult mode burns the queued question -------------------


def _adult_game_with_queue():
    g = LilyGame.__new__(LilyGame)
    g.sk = LilyScorekeeper("h4b")
    g.sk.mode = "adult"
    g.armed_question = {"id": "adult_1", "prompt": "spicy one"}
    g.next_question = {"id": "adult_2", "prompt": "spicier one"}
    g._window_timer = None
    g._burned_question_ids = set()
    g._burned_question_hashes = set()
    g.used_prompts = []
    g.supabase = None
    g.publish_attributes_nowait = lambda: None
    return g


def test_stop_in_adult_burns_armed_and_queued():
    g = _adult_game_with_queue()
    burned = g._burn_pending_adult_questions(reason="stop_in_adult")
    assert burned is True
    assert g.armed_question is None
    assert g.next_question is None
    # Both are now in the dead set and can never re-arm.
    assert "adult_1" in g._burned_question_ids
    assert "adult_2" in g._burned_question_ids
    assert g._is_burned({"id": "adult_1", "prompt": "spicy one"}) is True


def test_burn_is_noop_when_nothing_queued():
    g = _adult_game_with_queue()
    g.armed_question = None
    g.next_question = None
    assert g._burn_pending_adult_questions(reason="x") is False
