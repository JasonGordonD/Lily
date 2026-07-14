"""Tests for lily_binding — fragmented-STT name extraction."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lily_binding import (
    LilyFragmentAccumulator,
    lily_extract_name,
    lily_extract_name_from_fragments,
    lily_is_valid_name,
)


# ---------------------------------------------------------------------------
# Name extraction
# ---------------------------------------------------------------------------

def test_introducer_patterns():
    assert lily_extract_name("I'm Sarah") == "Sarah"
    assert lily_extract_name("my name is jack") == "Jack"
    assert lily_extract_name("this is Dave") == "Dave"
    assert lily_extract_name("call me Priya") == "Priya"


def test_leading_vocative_stripped():
    assert lily_extract_name("Lily, this is Jack") == "Jack"


def test_diarization_tag_stripped():
    assert lily_extract_name("[S2] I'm Marcus") == "Marcus"


def test_capitalized_fallback():
    assert lily_extract_name("uh yeah Rosa here") == "Rosa"


def test_agent_name_never_extracted():
    assert lily_extract_name("Lily") is None
    assert lily_extract_name("lily?") is None


def test_stopwords_not_names():
    assert lily_extract_name("okay ready") is None
    assert lily_extract_name("yes hello") is None
    assert lily_extract_name("um well just testing") is None


def test_game_vocabulary_not_names():
    """Expanded stopword list carries the game vocabulary."""
    for phrase in (
        "Play!", "start", "Ready", "question", "Trivia",
        "team", "Skip", "pass", "let's play trivia",
        "ready to start the game",
    ):
        assert lily_extract_name(phrase) is None, phrase


def test_is_valid_name():
    assert lily_is_valid_name("Marcus")
    assert not lily_is_valid_name("play")
    assert not lily_is_valid_name("Skip")
    assert not lily_is_valid_name("lily")
    assert not lily_is_valid_name("did")
    assert not lily_is_valid_name("x")
    assert not lily_is_valid_name("")
    assert not lily_is_valid_name("S2")  # labels are not names


# ---------------------------------------------------------------------------
# 2-second fragment accumulation
# ---------------------------------------------------------------------------

def test_fragmented_stt_name_extraction():
    """Speechmatics fragments: 'This.' / 'Call.' / 'My name is Jack.'
    must parse correctly via the 2-second accumulation window."""
    acc = LilyFragmentAccumulator(window=2.0)
    acc.add("S1", "This.", now=100.0)
    acc.add("S1", "Call.", now=100.6)
    acc.add("S1", "My name is Jack.", now=101.4)
    name = lily_extract_name_from_fragments(acc, "S1", now=101.5)
    assert name == "Jack"


def test_fragments_expire_outside_window():
    acc = LilyFragmentAccumulator(window=2.0)
    acc.add("S1", "My name is", now=100.0)
    acc.add("S1", "Rosa", now=103.0)  # first fragment expired
    combined = acc.combined("S1", now=103.1)
    assert "My name is" not in combined
    assert combined == "Rosa"
    # Extraction still lands on the surviving capitalized token.
    assert lily_extract_name_from_fragments(acc, "S1", now=103.1) == "Rosa"


def test_fragments_are_per_speaker():
    acc = LilyFragmentAccumulator(window=2.0)
    acc.add("S1", "I'm Sarah", now=100.0)
    acc.add("S2", "I'm Dave", now=100.1)
    assert lily_extract_name_from_fragments(acc, "S1", now=100.2) == "Sarah"
    assert lily_extract_name_from_fragments(acc, "S2", now=100.2) == "Dave"


def test_fragment_only_stopwords_yields_none():
    acc = LilyFragmentAccumulator(window=2.0)
    acc.add("S1", "okay.", now=100.0)
    acc.add("S1", "ready to play.", now=100.5)
    assert lily_extract_name_from_fragments(acc, "S1", now=100.6) is None
