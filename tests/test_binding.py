"""Tests for lily_binding — fragmented-STT name extraction."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lily_binding import (
    LilyFragmentAccumulator,
    lily_extract_explicit_name,
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
        "Play!", "playing", "start", "Ready", "question", "Trivia",
        "team", "Skip", "pass", "let's play trivia",
        "ready to start the game",
    ):
        assert lily_extract_name(phrase) is None, phrase


def test_9337b1_returner_sentence_never_extracts_playing_as_name():
    text = (
        "No, Lily. It's not my first time playing with you tonight. "
        "And I'm on my own."
    )
    assert lily_extract_name(text) is None
    assert lily_extract_explicit_name(text) is None


def test_explicit_name_extractor_requires_self_identification_or_bare_name():
    assert lily_extract_explicit_name("My name is Rami.") == "Rami"
    assert lily_extract_explicit_name("You should call me Rami.") == "Rami"
    assert lily_extract_explicit_name("Rami.") == "Rami"
    assert lily_extract_explicit_name("We are playing tonight.") is None


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


# ---------------------------------------------------------------------------
# Known-name STT snap (live 2026-07-15, the "Romney" class)
# ---------------------------------------------------------------------------

import lily_memory


def test_known_name_correction_snaps_garbled_returning_player():
    assert lily_memory.lily_known_name_correction("Romney", ["Rami"]) == "Rami"
    assert lily_memory.lily_known_name_correction("Sara", ["Sarah", "Dave"]) == "Sarah"


def test_known_name_correction_never_touches_exact_or_distinct_names():
    # Exact match: nothing to correct (protects distinct real players).
    assert lily_memory.lily_known_name_correction("Dave", ["Dave"]) is None
    # Different person, same first letter: no snap.
    assert lily_memory.lily_known_name_correction("Dan", ["Dave"]) is None
    assert lily_memory.lily_known_name_correction("Robbie", ["Rami"]) is None


def test_known_name_correction_refuses_ambiguity_and_junk():
    # Two plausible candidates -> never guess.
    assert lily_memory.lily_known_name_correction("Sari", ["Sarah", "Sara"]) is None
    # Empty / too short / no memory.
    assert lily_memory.lily_known_name_correction("", ["Rami"]) is None
    assert lily_memory.lily_known_name_correction("R", ["Rami"]) is None
    assert lily_memory.lily_known_name_correction("Romney", []) is None
    assert lily_memory.lily_known_name_correction("Romney", None) is None


# ---------------------------------------------------------------------------
# Voiceprint label migration (live 2026-07-15 18:17 deafness)
# ---------------------------------------------------------------------------

from lily_scorekeeper import LilyScorekeeper


def test_voiceprint_label_convergence_migrates_binding():
    # Speechmatics opens with a transient S0; Lily binds it; identification
    # then converges and relabels the stream by the enrolled player name.
    sk = LilyScorekeeper("test-room")
    sk.bind_speaker("S0", "Rami")
    sk.start_question({
        "prompt": "q", "canonical_answer": "hydrogen",
        "acceptable_answers": ["hydrogen"],
    })
    sk.open_answer_window(duration=30.0, now=100.0)
    result = sk.on_transcript_segment(
        text="hydrogen", speaker_label="Rami", is_final=True,
        now=101.0, segment_start_time=101.0,
    )
    # Attributed to Rami, label migrated, candidate recorded — not deaf.
    assert result["player"] == "Rami"
    assert result["candidate_recorded"] is True
    assert sk.players["Rami"]["speaker_label"] == "Rami"
    assert sk.players["Rami"]["talk_time_s"] > 0


def test_voiceprint_label_migration_is_case_insensitive_and_sticky():
    sk = LilyScorekeeper("test-room")
    sk.bind_speaker("S1", "Sarah")
    r = sk.on_transcript_segment(
        text="hello there", speaker_label="sarah", is_final=True, now=100.0,
    )
    assert r["player"] == "Sarah"
    assert sk.players["Sarah"]["speaker_label"] == "sarah"
    # Later segments with the migrated label resolve via the normal path.
    r2 = sk.on_transcript_segment(
        text="another line", speaker_label="sarah", is_final=True, now=101.0,
    )
    assert r2["player"] == "Sarah"


def test_unrelated_labels_still_go_unrostered():
    sk = LilyScorekeeper("test-room")
    sk.bind_speaker("S0", "Rami")
    r = sk.on_transcript_segment(
        text="hello", speaker_label="S7", is_final=True, now=100.0,
    )
    assert r["player"] is None
    assert r["unrostered"] is True
