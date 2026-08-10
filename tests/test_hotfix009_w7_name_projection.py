"""HOTFIX-009 W7 — the glass shows the wrong name until the player complains.

Live 2026-08-10 session RM_RQTZZanrHURF (lily_transcripts /
lily_addressee_log evidence export):

  05:29:29  [S1] "He should call me rummy."         -> bound "Rummy"
  05:29:53  [S1] "Hey, listen, I said my name is
                  Romeo. Alpha. Mike. Indigo."       -> nothing changed
  05:30:14  LILY "Rami — spelled out loud and clear. Got you."
  05:30:16  [S1] still attributed "Rummy"; the player has to ask
                 "for my name to be spelled correctly on the page"
  05:30:35  [S1] finally "Rami" — only after the complaint, and only
                 because the memory known-name snap had become available.

Three gaps, one projection: the NATO correction never parsed as name
evidence (the A070E8 stoplist made the correction utterance inert), the
stale evidence then outranked any corrected re-bind, and a re-bind that
did land forked a fresh player entry instead of renaming — leaving the
misspelled ghost on the glass. The glass renders `_players_payload()`
off `sk.players`; the fix routes a correction through the existing
evidence -> bind -> publish path so it reaches the glass in ONE seam
update.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_audeering_consumers
import lily_say_gate
from lily_agent import LilyGame
from lily_binding import (
    LilyFragmentAccumulator,
    lily_extract_explicit_name,
)
from lily_scorekeeper import LilyScorekeeper

# Real utterances from the evidence export (verbatim STT finals).
CAPTURE = "He should call me rummy."
NATO_CORRECTION = (
    "Hey, listen, I said my name is Romeo. Alpha. Mike. Indigo."
)
SCREEN_COMPLAINT = (
    "Well, what I do want is something else, which is for my name to be "
    "spelled correctly on the, uh, on the page."
)


# ---------------------------------------------------------------------------
# Extractor: the spelled correction is name evidence; complaints are not
# ---------------------------------------------------------------------------

def test_nato_correction_extracts_the_spelled_name():
    # Base behavior: the introducer regex grabs "Romeo" — the first NATO
    # word — as the name. The spelled run must assemble instead.
    assert lily_extract_explicit_name(NATO_CORRECTION) == "Rami"


def test_as_in_spelling_extracts_the_name():
    # The A070E8 session spelled the same name letter-by-letter.
    assert (
        lily_extract_explicit_name(
            "My name is R as in Romeo, A as in Apple, M as in Mary, "
            "I as in India."
        )
        == "Rami"
    )


def test_plain_introductions_are_unchanged():
    assert lily_extract_explicit_name(CAPTURE) == "Rummy"
    assert lily_extract_explicit_name("my name is Jack") == "Jack"
    # A single NATO-word name is a NAME, not a spelled letter.
    assert lily_extract_explicit_name("my name is Romeo") == "Romeo"


def test_screen_complaint_extracts_no_name():
    # A070E8 direction preserved: correction/complaint vocabulary is inert.
    assert lily_extract_explicit_name(SCREEN_COMPLAINT) is None


def test_spelled_trivia_answer_is_not_name_evidence():
    # No name cue -> no spelled assembly (an answer being spelled out
    # during a window must never rebind anybody).
    assert lily_extract_explicit_name("It's spelled C. A. T.") is None


def test_informal_word_run_is_a_stated_name_not_an_assembly():
    # LOW #3: the informal spelling words are real first names — a run of
    # them alone is somebody stating a full name, never "Rose". Majority
    # of units must be single letters or canonical NATO words.
    assert (
        lily_extract_explicit_name("my name is Robert Oscar Sam Edward")
        == "Robert"
    )


def test_rename_gate_similarity():
    # The same-person gate: respellings match, unrelated names never do.
    from lily_binding import lily_names_probably_same

    assert lily_names_probably_same("Rummy", "Rami") is True
    assert lily_names_probably_same("Romney", "Rami") is True
    assert lily_names_probably_same("Alice", "Bob") is False
    assert lily_names_probably_same("Rummy", "Robin") is False


def test_rename_gate_homophone_respellings():
    # Delta review MEDIUM: the spelled inlet assembles "Kris"/"Geoff"-
    # shaped corrections — leading-sound classes {c,k},{g,j},{f,ph},{s,sh}
    # must not bail on the first grapheme.
    from lily_binding import lily_names_probably_same

    assert lily_names_probably_same("Chris", "Kris") is True
    assert lily_names_probably_same("Jeff", "Geoff") is True
    assert lily_names_probably_same("Sean", "Shawn") is True
    # Different people sharing a leading sound still refuse.
    assert lily_names_probably_same("Chris", "Carol") is False
    assert lily_names_probably_same("Kris", "Karen") is False
    assert lily_names_probably_same("Jeff", "George") is False
    assert lily_names_probably_same("Sean", "Sam") is False
    # Nickname map deliberately NOT built (close-out residual): stays a
    # refusal -> fork, identical to pre-fix behavior.
    assert lily_names_probably_same("Bob", "Robert") is False


# ---------------------------------------------------------------------------
# Scorekeeper: a same-voice correction renames, never forks
# ---------------------------------------------------------------------------

def test_rename_migrates_the_player_and_the_ledger():
    sk = LilyScorekeeper("test-room")
    sk.bind_speaker("S1", "Rummy")
    sk.apply_score_event("Rummy", cause="bonus", points=1)
    assert sk.ledger_scores() == {"Rummy": 1}

    sk.bind_speaker("S1", "Rami", rename=True)

    assert set(sk.players) == {"Rami"}          # no ghost entry
    assert sk.players["Rami"]["speaker_label"] == "S1"
    assert sk.ledger_scores() == {"Rami": 1}    # score travels with the rename


def test_plain_rebind_still_releases_not_renames():
    # Today's label-drift semantics stay intact when rename is not asserted.
    sk = LilyScorekeeper("test-room")
    sk.bind_speaker("S1", "Alice")
    sk.bind_speaker("S1", "Bob")
    assert set(sk.players) == {"Alice", "Bob"}
    assert sk.players["Alice"]["speaker_label"] is None
    assert sk.players["Bob"]["speaker_label"] == "S1"


def test_second_voice_on_reused_label_never_takes_score_history():
    # MEDIUM (coordinator ruling): rename=True asserted for a DISSIMILAR
    # name — the migration writer refuses (diarization mis-capture class:
    # a second voice reusing a label must not inherit the first player's
    # ledger). Falls back to release semantics.
    sk = LilyScorekeeper("test-room")
    sk.bind_speaker("S1", "Alice")
    sk.apply_score_event("Alice", cause="bonus", points=3)

    sk.bind_speaker("S1", "Bob", rename=True)

    assert set(sk.players) == {"Alice", "Bob"}
    assert sk.ledger_scores() == {"Alice": 3, "Bob": 0}
    assert sk.players["Alice"]["speaker_label"] is None
    assert sk.players["Bob"]["speaker_label"] == "S1"


# ---------------------------------------------------------------------------
# Production replay: correction reaches the glass in one seam update
# ---------------------------------------------------------------------------

def _make_game() -> LilyGame:
    game = LilyGame.__new__(LilyGame)
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game.sk = LilyScorekeeper("test-room")
    game.fragments = LilyFragmentAccumulator()
    game.ui_phase = "lobby"
    game.game_started = False
    game.game_over = False
    game.memory_block = ""
    game.supabase = None
    game.highlights = []
    game._pending_unbound_award = None
    game._steal_window = False
    game.acoustic = lily_audeering_consumers.LilyAcousticState()
    # on_speaker_bound collaborators, stubbed per test_claim_integrity_fixture.
    game.fire_enrollment = lambda trigger: None
    game._enroll_started = True
    game.request_device_verification = lambda trigger: None
    game.send_event_nowait = lambda *a, **k: None
    game._maybe_retune_stt_for_roster = lambda: None
    game._maybe_auto_start_after_lobby = lambda: None
    game.attribute_publishes = []
    game.publish_attributes_nowait = lambda: game.attribute_publishes.append(
        [p["name"] for p in game._players_payload()]
    )
    return game


def _transcript_final(game: LilyGame, label: str, text: str, at: float) -> None:
    """The transcript-site evidence sequence (lily_agent entrypoint,
    fragment accumulator -> explicit-name extraction -> evidence note)."""
    combined = game.fragments.add(label, text, now=at)
    explicit = lily_extract_explicit_name(combined)
    if explicit:
        game.note_confirmed_name_evidence(label, explicit)


def test_correction_reaches_the_glass_in_one_update():
    game = _make_game()
    t0 = time.time()

    # 05:29:29 — capture, then the bind the tool path performed.
    _transcript_final(game, "S1", CAPTURE, t0)
    game.sk.bind_speaker("S1", "Rummy")
    game.on_speaker_bound("S1", "Rummy")
    assert game.attribute_publishes[-1] == ["Rummy"]

    publishes_before = len(game.attribute_publishes)
    # The ONE agent-side name snapshot outside sk.players (review-round
    # derivation sweep): prewager_standings is compared BY NAME at
    # wrap-up — a rename between final wager and wrap-up must re-key it
    # or a false "took the crown" highlight mints.
    game.prewager_standings = [{"name": "Rummy", "score": 2}]

    # 05:29:53 — the NATO correction final, through the same site.
    _transcript_final(game, "S1", NATO_CORRECTION, t0 + 24.0)

    # The glass projection now shows the corrected name — no ghost, no
    # second ask, exactly one further seam update.
    assert [p["name"] for p in game._players_payload()] == ["Rami"]
    assert game.confirmed_name_for_label("S1") == "Rami"
    assert len(game.attribute_publishes) == publishes_before + 1
    assert game.attribute_publishes[-1] == ["Rami"]
    # W5 interface: the correction is a REBIND, not an ADD — everything
    # downstream of sk.players (roster size, checkpoint scorekeeper_state,
    # final_standings, session_reports.per_player, lily_memories
    # player_names) derives from this one identity. The live session's
    # ghost inflated the roster to 2 and fired a steal window at a solo
    # table (05:32:11 UTC).
    assert game.sk.roster_size() == 1
    assert set(game.sk.players) == {"Rami"}
    assert set(game.sk.ledger_scores()) == {"Rami"}
    assert game.prewager_standings == [{"name": "Rami", "score": 2}]


def test_midgame_dissimilar_call_me_does_not_auto_rebind():
    # Reviewer LOW #1 + MEDIUM, agent level: a bound player (or a second
    # voice on their label) saying "call me Bob" mid-game records evidence
    # but auto-rebinds NOTHING — a dissimilar name is a new binding
    # decision owned by the tool path, not a correction.
    game = _make_game()
    t0 = time.time()
    _transcript_final(game, "S1", "call me Alice please", t0)
    game.sk.bind_speaker("S1", "Alice")
    game.on_speaker_bound("S1", "Alice")
    game.sk.apply_score_event("Alice", cause="bonus", points=2)
    publishes_before = len(game.attribute_publishes)

    _transcript_final(game, "S1", "call me Bob", t0 + 30.0)

    assert set(game.sk.players) == {"Alice"}
    assert game.sk.players["Alice"]["speaker_label"] == "S1"
    assert game.sk.ledger_scores() == {"Alice": 2}
    assert game.confirmed_name_for_label("S1") == "Bob"  # evidence recorded
    assert len(game.attribute_publishes) == publishes_before


def test_complaint_alone_changes_nothing():
    # The 05:30:16 complaint must not itself rebind or publish (it carries
    # no name); with the correction inlet working it never needs to.
    game = _make_game()
    t0 = time.time()
    _transcript_final(game, "S1", CAPTURE, t0)
    game.sk.bind_speaker("S1", "Rummy")
    game.on_speaker_bound("S1", "Rummy")
    publishes_before = len(game.attribute_publishes)

    _transcript_final(game, "S1", SCREEN_COMPLAINT, t0 + 47.0)

    assert [p["name"] for p in game._players_payload()] == ["Rummy"]
    assert len(game.attribute_publishes) == publishes_before
