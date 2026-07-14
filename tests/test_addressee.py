"""Tests for lily_addressee — the pure B1 corpus logic (clarify-reply
parser, seconds-into-window, implicit label derivation). Stdlib only, no
livekit / supabase required."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lily_addressee import (
    AGENT_ACTION_CLARIFIED,
    AGENT_ACTION_IGNORED,
    AGENT_ACTION_SCORED,
    LABEL_DELIBERATION,
    LABEL_HOST_DIRECTED,
    LABEL_SOURCE_EXPLICIT_CLARIFY,
    LABEL_SOURCE_IMPLICIT_APPEALED,
    LABEL_SOURCE_IMPLICIT_SCORED,
    LABEL_UNKNOWN,
    lily_candidate_key,
    lily_labels_for_adjudication,
    lily_parse_clarify_reply,
    lily_seconds_into_window,
)


# ---------------------------------------------------------------------------
# Clarify-reply parser — affirmative
# ---------------------------------------------------------------------------

def test_clarify_plain_yes():
    assert lily_parse_clarify_reply("Yes") == LABEL_HOST_DIRECTED


def test_clarify_yeah_thats_my_answer():
    assert lily_parse_clarify_reply("yeah, that's my answer") == LABEL_HOST_DIRECTED


def test_clarify_final_answer():
    assert lily_parse_clarify_reply("Final answer!") == LABEL_HOST_DIRECTED


def test_clarify_lock_it_in():
    assert lily_parse_clarify_reply("lock it in") == LABEL_HOST_DIRECTED


def test_clarify_affirmative_with_diarization_tag():
    assert lily_parse_clarify_reply("[S2] Yep, final answer.") == LABEL_HOST_DIRECTED


# ---------------------------------------------------------------------------
# Clarify-reply parser — negative / deliberation
# ---------------------------------------------------------------------------

def test_clarify_plain_no():
    assert lily_parse_clarify_reply("No.") == LABEL_DELIBERATION


def test_clarify_just_thinking():
    assert lily_parse_clarify_reply("just thinking out loud") == LABEL_DELIBERATION


def test_clarify_talking_to_him():
    assert lily_parse_clarify_reply("I was talking to him") == LABEL_DELIBERATION


def test_clarify_still_arguing():
    assert lily_parse_clarify_reply("we're still arguing about it") == LABEL_DELIBERATION


def test_clarify_not_my_answer():
    assert lily_parse_clarify_reply("that's not my answer") == LABEL_DELIBERATION


# ---------------------------------------------------------------------------
# Clarify-reply parser — unparseable
# ---------------------------------------------------------------------------

def test_clarify_unrelated_reply_is_unknown():
    assert lily_parse_clarify_reply("what was the question again") == LABEL_UNKNOWN


def test_clarify_empty_is_unknown():
    assert lily_parse_clarify_reply("") == LABEL_UNKNOWN
    assert lily_parse_clarify_reply("   ") == LABEL_UNKNOWN


def test_clarify_mixed_signals_is_unknown():
    # Both directions fire -> refuse to guess.
    assert lily_parse_clarify_reply("no wait, yes, final answer") == LABEL_UNKNOWN


def test_clarify_word_boundaries():
    # "nose" must not fire the "no" cue; "yesterday" must not fire "yes".
    assert lily_parse_clarify_reply("the nose knows") == LABEL_UNKNOWN
    assert lily_parse_clarify_reply("I said that yesterday") == LABEL_UNKNOWN


# ---------------------------------------------------------------------------
# seconds_into_window
# ---------------------------------------------------------------------------

def test_seconds_into_window_normal():
    assert lily_seconds_into_window(100.0, 104.25) == 4.25


def test_seconds_into_window_clamped_at_zero():
    # A segment stamped fractionally before the open is "at open".
    assert lily_seconds_into_window(100.0, 99.9) == 0.0


def test_seconds_into_window_none_inputs():
    assert lily_seconds_into_window(None, 104.0) is None
    assert lily_seconds_into_window(100.0, None) is None
    assert lily_seconds_into_window(None, None) is None


def test_seconds_into_window_rounding():
    assert lily_seconds_into_window(100.0, 101.23456) == 1.235


# ---------------------------------------------------------------------------
# Implicit label derivation at adjudication commit
# ---------------------------------------------------------------------------

def _cand(player, label):
    return {"player": player, "speaker_label": label}


def test_labels_winner_and_scored_incorrect_are_host_directed():
    ordered = [
        _cand("Sarah", "S1"),   # winner
        _cand("Dave", "S2"),    # scored incorrect
        _cand("Priya", "S3"),   # scored incorrect
    ]
    labels = lily_labels_for_adjudication(ordered, "Sarah")
    assert labels == {
        "Sarah": (LABEL_HOST_DIRECTED, LABEL_SOURCE_IMPLICIT_SCORED),
        "Dave": (LABEL_HOST_DIRECTED, LABEL_SOURCE_IMPLICIT_SCORED),
        "Priya": (LABEL_HOST_DIRECTED, LABEL_SOURCE_IMPLICIT_SCORED),
    }


def test_labels_unrostered_non_winner_gets_no_label():
    # Unrostered non-winners are never scored — no implicit label.
    ordered = [_cand("Sarah", "S1"), _cand(None, "S9")]
    labels = lily_labels_for_adjudication(ordered, "Sarah")
    assert "unrostered:S9" not in labels
    assert "Sarah" in labels


def test_labels_unrostered_winner_is_labeled():
    ordered = [_cand(None, "S9"), _cand("Dave", "S2")]
    labels = lily_labels_for_adjudication(ordered, "unrostered:S9")
    assert labels["unrostered:S9"] == (
        LABEL_HOST_DIRECTED, LABEL_SOURCE_IMPLICIT_SCORED,
    )
    assert labels["Dave"] == (
        LABEL_HOST_DIRECTED, LABEL_SOURCE_IMPLICIT_SCORED,
    )


def test_labels_missed_question_scored_incorrect_still_labeled():
    # No winner: rostered candidates were still scored incorrect.
    ordered = [_cand("Sarah", "S1"), _cand(None, "S9")]
    labels = lily_labels_for_adjudication(ordered, None)
    assert labels == {
        "Sarah": (LABEL_HOST_DIRECTED, LABEL_SOURCE_IMPLICIT_SCORED),
    }


def test_labels_empty_candidates():
    assert lily_labels_for_adjudication([], None) == {}
    assert lily_labels_for_adjudication(None, None) == {}


def test_candidate_key():
    assert lily_candidate_key(_cand("Sarah", "S1")) == "Sarah"
    assert lily_candidate_key(_cand(None, "S9")) == "unrostered:S9"
    assert lily_candidate_key({"player": None, "speaker_label": None}) == "unrostered:UU"


def test_label_source_constants_spellings():
    # These strings are the corpus contract — locked.
    assert LABEL_SOURCE_IMPLICIT_SCORED == "implicit_scored_unappealed"
    assert LABEL_SOURCE_IMPLICIT_APPEALED == "implicit_appealed"
    assert LABEL_SOURCE_EXPLICIT_CLARIFY == "explicit_clarify"
    assert AGENT_ACTION_SCORED == "scored"
    assert AGENT_ACTION_IGNORED == "ignored"
    assert AGENT_ACTION_CLARIFIED == "clarified"
