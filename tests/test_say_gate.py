"""Unit tests for lily_say_gate — the outbound-speech hygiene gate (P4).

The stripper runs in tts_node BEFORE the punctuation-flush guard: markdown
emphasis, headers, bullet markers, backticks and emoji must never reach the
synthesizer (ElevenLabs reads "asterisk asterisk" or worse, silence), while
[bracket] audio tags are load-bearing ElevenLabs v3 controls and must pass
through verbatim. Pure module — no livekit imports.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lily_say_gate import (
    lily_clean_for_speech,
    lily_strip_emoji,
    lily_strip_markdown,
)


# -- markdown emphasis ---------------------------------------------------------

def test_bold_stripped():
    assert lily_clean_for_speech("that is **exactly** right") == "that is exactly right"


def test_italic_stripped():
    assert lily_clean_for_speech("a *very* close call") == "a very close call"


def test_nested_emphasis_stripped():
    assert (
        lily_clean_for_speech("**Sarah takes *the whole* round**")
        == "Sarah takes the whole round"
    )


def test_triple_asterisk_bold_italic():
    assert lily_clean_for_speech("***huge***") == "huge"


def test_backticks_stripped():
    assert lily_clean_for_speech("the answer is `Tokyo`") == "the answer is Tokyo"


def test_fenced_code_markers_stripped():
    assert lily_clean_for_speech("```\nTokyo\n```") == "Tokyo"


# -- headers and bullets ---------------------------------------------------------

def test_header_marker_stripped():
    assert lily_clean_for_speech("## Round Two") == "Round Two"


def test_deep_header_stripped():
    assert lily_clean_for_speech("###### tiny header") == "tiny header"


def test_hash_mid_sentence_preserved():
    # Only line-start ATX headers are markdown; a spoken "number sign" or
    # "#1" mid-sentence is not.
    assert lily_clean_for_speech("Dave is #1 tonight") == "Dave is #1 tonight"


def test_bullet_markers_stripped():
    text = "- Sarah: 5\n- Dave: 3\n• Sam: 1"
    assert lily_clean_for_speech(text) == "Sarah: 5\nDave: 3\nSam: 1"


def test_hyphenated_word_preserved():
    assert lily_clean_for_speech("a well-known fact") == "a well-known fact"


# -- emoji ---------------------------------------------------------------------

def test_emoji_stripped():
    assert lily_clean_for_speech("Sarah wins! \U0001F389\U0001F3C6") == "Sarah wins!"


def test_emoticon_range_stripped():
    assert lily_clean_for_speech("tough luck \U0001F605") == "tough luck"


def test_star_and_sparkles_stripped():
    assert lily_clean_for_speech("⭐ bonus point ✨") == "bonus point"


def test_flag_and_zwj_sequence_stripped():
    # Regional indicators + ZWJ family sequence
    assert lily_clean_for_speech("\U0001F1EB\U0001F1F7 France it is") == "France it is"
    assert (
        lily_clean_for_speech("go \U0001F468‍\U0001F469‍\U0001F467 team")
        == "go team"
    )


def test_variation_selector_stripped():
    assert lily_clean_for_speech("warning ⚠️ ahead") == "warning ahead"


def test_strip_emoji_pure_function():
    assert lily_strip_emoji("abc\U0001F600def") == "abcdef"
    assert lily_strip_emoji("") == ""


# -- audio tags preserved --------------------------------------------------------

def test_known_audio_tags_preserved():
    text = "[excited] Sarah takes it! [pause] Next question."
    assert lily_clean_for_speech(text) == text


def test_whisper_and_break_preserved():
    text = '[whispering] the answer is... <break time="1.2s"/> Tokyo.'
    assert lily_clean_for_speech(text) == text


def test_all_bracket_tags_preserved_even_unknown():
    # Deliberate design: ALL [bracket] content passes through — safer than
    # maintaining a known-tag allowlist and stripping a load-bearing tag.
    text = "[strong Texan accent] y'all ready?"
    assert lily_clean_for_speech(text) == text


def test_tag_adjacent_markdown_stripped_tag_kept():
    assert (
        lily_clean_for_speech("[excited] **Sarah** wins \U0001F389")
        == "[excited] Sarah wins"
    )


# -- empty-result guard ----------------------------------------------------------

def test_emoji_only_strips_to_empty():
    assert lily_clean_for_speech("\U0001F389\U0001F389\U0001F389") == ""


def test_markdown_only_strips_to_empty():
    assert lily_clean_for_speech("***") == ""
    assert lily_clean_for_speech("```") == ""


def test_empty_and_none_safe():
    assert lily_clean_for_speech("") == ""
    assert lily_clean_for_speech(None) == ""
    assert lily_strip_markdown(None) == ""
    assert lily_strip_emoji(None) == ""


# -- whitespace hygiene ----------------------------------------------------------

def test_double_spaces_collapsed():
    assert (
        lily_clean_for_speech("Sarah  **wins**  the  round")
        == "Sarah wins the round"
    )


def test_space_before_punctuation_healed():
    # "word **!**" -> "word !" -> "word!"
    assert lily_clean_for_speech("she got it **!**") == "she got it!"


def test_emoji_hole_does_not_leave_double_space():
    assert lily_clean_for_speech("great \U0001F44F answer") == "great answer"


def test_blank_lines_from_stripped_bullets_removed():
    assert lily_clean_for_speech("scores:\n\U0001F3C6\n- Dave: 3") == "scores:\nDave: 3"


# -- plain speech untouched -------------------------------------------------------

def test_plain_text_unchanged():
    text = "Dave started first, but Sarah got the whole answer out. Sarah's point."
    assert lily_clean_for_speech(text) == text


def test_punctuation_and_apostrophes_untouched():
    text = "who's winning? Sarah! by two... maybe three."
    assert lily_clean_for_speech(text) == text
