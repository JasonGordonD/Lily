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
    CLAIM_CONFIRMED,
    CLAIM_PENDING,
    LILY_STATE_SENTINEL_CLOSE,
    LILY_STATE_SENTINEL_OPEN,
    SpeechActRegistry,
    lily_clean_for_speech,
    lily_filter_leaks,
    lily_strip_emoji,
    lily_strip_markdown,
    lily_wrap_state_block,
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


# ===========================================================================
# Say-gate WO extensions (2026-07-14): SpeechActRegistry + leak filter
# ===========================================================================

# -- SpeechActRegistry: claim / confirm / release ------------------------------------

def test_claim_is_atomic_check_and_set():
    reg = SpeechActRegistry()
    assert reg.claim("session_greet") is True
    # The race window: a second dispatch of the same act must fail HERE,
    # at dispatch time — never wait for playback completion.
    assert reg.claim("session_greet") is False
    assert reg.state("session_greet") == CLAIM_PENDING


def test_distinct_keys_do_not_collide():
    reg = SpeechActRegistry()
    assert reg.claim("q_1_delivery") is True
    assert reg.claim("q_1_reveal") is True
    assert reg.claim("q_2_delivery") is True


def test_rejoin_does_not_trip_greet():
    # Reconnect keeps its distinct re-entry line under its own key.
    reg = SpeechActRegistry()
    assert reg.claim("session_rejoin") is True
    assert reg.claim("session_greet") is True


def test_confirm_pending_marks_played_acts():
    reg = SpeechActRegistry()
    reg.claim("q_3_delivery")
    reg.claim("finale")
    confirmed = reg.confirm_pending()
    assert sorted(confirmed) == ["finale", "q_3_delivery"]
    assert reg.state("q_3_delivery") == CLAIM_CONFIRMED
    # Idempotent — nothing pending on the second pass.
    assert reg.confirm_pending() == []


def test_release_on_playback_failure_allows_redelivery():
    # The 19:27:52 swallowed-delivery class: claimed at dispatch, the turn
    # produced empty text, playback never happened — the claim must
    # release so the retry can redeliver.
    reg = SpeechActRegistry()
    assert reg.claim("q_5_delivery") is True
    released = reg.release_pending()
    assert released == ["q_5_delivery"]
    assert reg.state("q_5_delivery") is None
    assert reg.claim("q_5_delivery") is True  # retry redelivers


def test_confirmed_acts_never_release():
    reg = SpeechActRegistry()
    reg.claim("session_greet")
    reg.confirm_pending()  # playout completed — genuinely spoken
    assert reg.release("session_greet") is False
    assert reg.release_pending() == []
    assert reg.claim("session_greet") is False  # spoken acts stay done


def test_release_single_key():
    reg = SpeechActRegistry()
    reg.claim("q_2_reveal")
    assert reg.release("q_2_reveal") is True
    assert reg.release("q_2_reveal") is False  # already released
    assert reg.claim("q_2_reveal") is True


def test_release_pending_leaves_confirmed_untouched():
    reg = SpeechActRegistry()
    reg.claim("session_greet")
    reg.confirm_pending()
    reg.claim("q_1_delivery")  # in flight
    assert reg.release_pending() == ["q_1_delivery"]
    assert reg.state("session_greet") == CLAIM_CONFIRMED


def test_owner_scopes_confirmation_and_release():
    reg = SpeechActRegistry()
    assert reg.claim("q_1_delivery", owner="speech-a")
    assert reg.claim("q_1_reveal", owner="speech-b")

    assert reg.confirm_owner("speech-a") == ["q_1_delivery"]
    assert reg.state("q_1_delivery") == CLAIM_CONFIRMED
    assert reg.state("q_1_reveal") == CLAIM_PENDING

    assert reg.release_owner("speech-b") == ["q_1_reveal"]
    assert reg.state("q_1_reveal") is None


def test_dispatch_reservation_reassigns_to_speech_handle():
    reg = SpeechActRegistry()
    assert reg.claim("session_greet", owner="dispatch-1")
    assert reg.reassign_owner("dispatch-1", "speech-1") == ["session_greet"]
    assert reg.confirm_owner("dispatch-1") == []
    assert reg.confirm_owner("speech-1") == ["session_greet"]


# -- leak filter: sentinel envelope ---------------------------------------------------

def test_sentinel_envelope_stripped():
    leaked = (
        "Alright table!\n"
        f"{LILY_STATE_SENTINEL_OPEN}\n[GAME STATE]\nphase=round round=1/3\n"
        f"current_question: 'This strait?'\n{LILY_STATE_SENTINEL_CLOSE}\n"
        "Who's ready?"
    )
    filtered, reasons = lily_filter_leaks(leaked)
    assert "sentinel_envelope" in reasons
    assert "GAME STATE" not in filtered
    assert "phase=round" not in filtered
    assert "Alright table!" in filtered
    assert "Who's ready?" in filtered


def test_wrapped_block_round_trips_to_silence():
    # A fully-echoed injected block must strip to nothing.
    block = lily_wrap_state_block("[GAME STATE]\nphase=lobby\n(no players)")
    filtered, reasons = lily_filter_leaks(block)
    assert reasons
    assert filtered == ""


def test_sentinel_fragment_line_dropped():
    # Chunk-boundary partial: the envelope open tag cut mid-way.
    filtered, reasons = lily_filter_leaks(
        "Next one's a beauty.\n<lily_state\nDave, you're up."
    )
    assert "sentinel_fragment" in reasons
    assert "<lily_state" not in filtered
    assert "Next one's a beauty." in filtered
    assert "Dave, you're up." in filtered


def test_closing_fragment_dropped():
    filtered, reasons = lily_filter_leaks("lily_state> and the scores hold.")
    assert "sentinel_fragment" in reasons
    assert "lily_state" not in filtered


# -- leak filter: bracketed metadata lines --------------------------------------------

def test_game_state_header_line_dropped():
    filtered, reasons = lily_filter_leaks(
        "Big round coming.\n[GAME STATE] phase=round round=2/3\nHere we go."
    )
    assert any(r.startswith("metadata:") for r in reasons)
    assert "phase=round" not in filtered
    assert "Big round coming." in filtered
    assert "Here we go." in filtered


def test_room_read_env_and_returning_table_lines_dropped():
    for marker in ("[room read: lively]", "[env: music]", "[RETURNING TABLE]"):
        filtered, reasons = lily_filter_leaks(f"Hello!\n{marker} details\nOnward.")
        assert reasons, marker
        assert marker.split()[0].strip("[]").lower() not in filtered.lower()
        assert "Hello!" in filtered


def test_metadata_marker_case_insensitive():
    _, reasons = lily_filter_leaks("[game state] phase=lobby")
    assert reasons


# -- leak filter: clean text passes, audio tags preserved ------------------------------

def test_clean_text_passes_untouched():
    text = "Sarah takes the round! [excited] Next question coming up."
    filtered, reasons = lily_filter_leaks(text)
    assert reasons == []
    assert filtered == text


def test_audio_tags_are_not_leaks():
    text = "[whispering] the answer is... [pause] Tokyo."
    filtered, reasons = lily_filter_leaks(text)
    assert reasons == []
    assert filtered == text


def test_leak_filter_then_hygiene_preserves_tags():
    # The tts_node order: leak filter first, then the markdown/emoji pass.
    leaked = "[excited] **Sarah** wins!\n[GAME STATE] phase=round"
    filtered, reasons = lily_filter_leaks(leaked)
    assert reasons
    assert lily_clean_for_speech(filtered) == "[excited] Sarah wins!"


def test_empty_and_none_safe_for_leak_filter():
    assert lily_filter_leaks("") == ("", [])
    assert lily_filter_leaks(None) == ("", [])


# -- sentinel wrapper -------------------------------------------------------------------

def test_wrap_state_block_shape():
    wrapped = lily_wrap_state_block("[GAME STATE]\nphase=lobby")
    assert wrapped.startswith(LILY_STATE_SENTINEL_OPEN)
    assert wrapped.endswith(LILY_STATE_SENTINEL_CLOSE)
    assert "[GAME STATE]" in wrapped
