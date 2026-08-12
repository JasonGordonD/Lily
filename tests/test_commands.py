"""Tests for sticky player-command detection — deterministic, period and
fragment proof (spec §11.4)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lily_scorekeeper import LilyScorekeeper, lily_detect_control_command


def make_sk():
    sk = LilyScorekeeper(session_id="test-room")
    sk.bind_speaker("S1", "Sarah")
    return sk


# ---------------------------------------------------------------------------
# Direct detection — punctuation-proof
# ---------------------------------------------------------------------------

def test_back_to_normal_plain():
    assert lily_detect_control_command("back to normal") == "back_to_normal"


def test_back_to_normal_period_proof():
    assert lily_detect_control_command("Back. To normal.") == "back_to_normal"
    assert lily_detect_control_command("back to... normal") == "back_to_normal"
    assert lily_detect_control_command("BACK TO NORMAL!") == "back_to_normal"


def test_back_to_normal_in_sentence():
    assert (
        lily_detect_control_command("okay can we go back to normal please")
        == "back_to_normal"
    )


def test_skip_standalone():
    assert lily_detect_control_command("skip") == "skip"
    assert lily_detect_control_command("Skip!") == "skip"
    assert lily_detect_control_command("can we skip this one") == "skip"


def test_skip_not_inside_words():
    assert lily_detect_control_command("the skipper of the boat") is None
    assert lily_detect_control_command("skipping stones") is None


def test_back_to_normal_wins_over_skip():
    assert (
        lily_detect_control_command("skip it, back to normal")
        == "back_to_normal"
    )


def test_no_command_in_ordinary_speech():
    assert lily_detect_control_command("Tungsten") is None
    assert lily_detect_control_command("that was a normal question") is None
    assert lily_detect_control_command("") is None


def test_start_game_phrases():
    assert lily_detect_control_command("start the game") == "start_game"
    assert lily_detect_control_command("Start the quiz!") == "start_game"
    assert lily_detect_control_command("okay let's start") == "start_game"
    assert lily_detect_control_command("Let's play.") == "start_game"
    assert lily_detect_control_command("lets play") == "start_game"
    assert lily_detect_control_command("can we start round one") == "start_game"


def test_start_game_not_in_ordinary_speech():
    assert lily_detect_control_command("the game was great") is None
    assert lily_detect_control_command("we started late") is None
    assert lily_detect_control_command("I play tennis") is None
    # HOSTLOOP-001 C7 revoked the old "bare start is ignored" pin: Session
    # A's "Starts." produced 13s of dead air because the lone token didn't
    # fire. A bare start AS THE WHOLE UTTERANCE is now the intent; start
    # buried in a sentence still never fires (test_start_intent.py).
    assert lily_detect_control_command("start") == "start_game"


def test_skip_wins_over_start():
    assert lily_detect_control_command("skip it and start the game") == "skip"


def test_start_game_fragment_proof():
    sk = make_sk()
    r1 = sk.on_transcript_segment(text="Let's.", speaker_label="S1", now=100.0)
    assert r1["control_command"] is None
    r2 = sk.on_transcript_segment(text="Start!", speaker_label="S1", now=101.0)
    assert r2["control_command"] == "start_game"


# ---------------------------------------------------------------------------
# Fragment-proof across segments (scorekeeper 2s join)
# ---------------------------------------------------------------------------

def test_command_across_fragmented_finals():
    sk = make_sk()
    r1 = sk.on_transcript_segment(text="Back to.", speaker_label="S1", now=100.0)
    assert r1["control_command"] is None
    r2 = sk.on_transcript_segment(text="Normal.", speaker_label="S1", now=101.0)
    assert r2["control_command"] == "back_to_normal"


def test_fragments_do_not_join_across_gap():
    sk = make_sk()
    sk.on_transcript_segment(text="Back to.", speaker_label="S1", now=100.0)
    r2 = sk.on_transcript_segment(text="Normal.", speaker_label="S1", now=104.0)
    assert r2["control_command"] is None


def test_fragments_do_not_join_across_speakers():
    sk = make_sk()
    sk.bind_speaker("S2", "Dave")
    sk.on_transcript_segment(text="Back to.", speaker_label="S1", now=100.0)
    r2 = sk.on_transcript_segment(text="Normal.", speaker_label="S2", now=100.5)
    assert r2["control_command"] is None


def test_command_not_recorded_as_answer_candidate():
    sk = make_sk()
    sk.start_question({"prompt": "q", "canonical_answer": "Tungsten"})
    sk.open_answer_window(now=100.0)
    result = sk.on_transcript_segment(
        text="skip", speaker_label="S1", now=101.0, segment_start_time=101.0
    )
    assert result["control_command"] == "skip"
    assert result["candidate_recorded"] is False
    assert sk.answer_candidates == {}


def test_command_detection_survives_repeat():
    """The fragment buffer clears after a command fires so the same
    fragments don't double-fire."""
    sk = make_sk()
    r1 = sk.on_transcript_segment(text="back to normal", speaker_label="S1", now=100.0)
    assert r1["control_command"] == "back_to_normal"
    r2 = sk.on_transcript_segment(text="thanks", speaker_label="S1", now=100.5)
    assert r2["control_command"] is None


# ---------------------------------------------------------------------------
# "Forget me" — the deletion right (WO-LILY-FORGETME-001), paraphrase-
# tolerant, negation-guarded, fragment-proof
# ---------------------------------------------------------------------------

def test_forget_me_core_phrases():
    for phrase in (
        "forget me",
        "Forget us!",
        "Lily, forget me.",
        "forget about us",
        "forget this table",
        "forget everything about me",
        "forget everything you know about us",
        "forget what you know about me",
    ):
        assert lily_detect_control_command(phrase) == "forget_me", phrase


def test_forget_me_delete_paraphrases():
    for phrase in (
        "delete what you know about me",
        "Delete everything you know about us.",
        "erase everything you know",
        "wipe what you know about this table",
        "delete my data",
        "delete our history",
        "erase our memories",
        "wipe my file",
        "clear our data",
        "erase us",
        "wipe me",
        "remove everything you have on us",
    ):
        assert lily_detect_control_command(phrase) == "forget_me", phrase


def test_forget_me_punctuation_and_fragment_proof():
    assert lily_detect_control_command("Forget. Me.") == "forget_me"
    assert lily_detect_control_command("delete... what you know about us") == "forget_me"
    # Cross-segment ASR fragments join through the scorekeeper's 2s window.
    sk = make_sk()
    r1 = sk.on_transcript_segment(
        text="Delete everything.", speaker_label="S1", now=100.0
    )
    assert r1["control_command"] is None
    r2 = sk.on_transcript_segment(
        text="You know about us.", speaker_label="S1", now=101.0
    )
    assert r2["control_command"] == "forget_me"


def test_forget_me_negations_do_not_fire():
    for phrase in (
        "don't forget me",
        "Don't ever forget us!",
        "never forget me",
        "you won't forget us, right",
        "do not forget about me",
        "she didn't forget us",
    ):
        assert lily_detect_control_command(phrase) != "forget_me", phrase


def test_forget_me_ordinary_speech_does_not_fire():
    for phrase in (
        "I forget the answer",
        "oh forget it",
        "I forgot my keys",
        "forget the second clue",
        "she can never remember my name",
        "delete that last point, that was wrong",  # score appeal, not deletion
        "wipe the floor with them",
    ):
        assert lily_detect_control_command(phrase) != "forget_me", phrase


def test_forget_me_wins_over_skip():
    assert (
        lily_detect_control_command("forget us and skip this one")
        == "forget_me"
    )


def test_forget_me_not_recorded_as_answer_candidate():
    sk = make_sk()
    sk.start_question({"prompt": "q", "canonical_answer": "Tungsten"})
    sk.open_answer_window(now=100.0)
    result = sk.on_transcript_segment(
        text="Lily, forget me", speaker_label="S1", now=101.0,
        segment_start_time=101.0,
    )
    assert result["control_command"] == "forget_me"
    assert result["candidate_recorded"] is False
    assert sk.answer_candidates == {}
