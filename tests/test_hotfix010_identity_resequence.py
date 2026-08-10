"""WO-LILY-HOTFIX-010 V1 + V3 — the identity re-sequence.

Root cause: group-recognition-spoken-as-person-recognition. A successful
GROUP-level match caused greeting / late-recognition to recite the group's
HISTORICAL name roster (memory_player_names — a multi-session union, never
scrubbed of STT conflations) as if each were a present, recognized person.
The live artefact was a cold opener that greeted "Rami, Rhonda, Chris,
Miranda" — four names, none of them confirmed present on the mic.

V1 (single present-voice naming source): a person is named ONLY from the
present-voice source (sk.players / a stated name), never from memory.
V3 (minimal opener): the opening turn is a bare self-intro + ONE orienting
beat, then silence — recognition / walkthrough / prefs / what's-new are
deferred until a human has actually spoken.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_config
from test_hotfix006_n1_identity_race import _game
from test_recognition_variety import _make_game

FOUR = ["Rami", "Rhonda", "Chris", "Miranda"]


# -- V3: the cold opener is minimal -------------------------------------------


def test_cold_opener_is_intro_plus_one_orienting_beat_then_silence(monkeypatch):
    """The opening turn (no human has spoken yet) is PART ONE self-intro +
    ONE orienting beat and nothing more — no recognition, no first-time
    question, no walkthrough/refresher offer."""
    monkeypatch.setattr(lily_config, "voice_identity_enabled", lambda: True)
    g = _game(resolved=True, memory_block="[RETURNING TABLE] 12 games",
              spoken=False)
    text = g.greeting_instructions()
    assert "Hi, I'm Lily" in text                       # PART ONE, always
    assert "who's at the mic tonight" in text           # the one orienting beat
    # None of the deferred rich beats:
    assert "memory KNOWS this TABLE" not in text
    assert "first time playing with you" not in text
    assert "refresher" not in text
    assert "walkthrough" not in text


def test_opening_turn_requests_no_name_and_recites_no_history(monkeypatch):
    """No name request beyond 'who's at the mic', and zero history recited —
    no remembered names, no winner, no counts."""
    monkeypatch.setattr(lily_config, "voice_identity_enabled", lambda: True)
    g = _game(resolved=True, memory_block="[RETURNING TABLE] Rami — 4 wins",
              spoken=False)
    g.memory_player_names = list(FOUR)
    text = g.greeting_instructions()
    for name in FOUR:
        assert name not in text
    # No concrete history recited (the orienting beat names "winners" only to
    # forbid reciting them, so match the specific stored fact instead).
    assert "4 wins" not in text


def test_returning_table_callback_only_after_the_player_has_spoken(monkeypatch):
    """A returning table's recognition beat appears only AFTER the first
    human utterance, never in the pre-utterance opener."""
    monkeypatch.setattr(lily_config, "voice_identity_enabled", lambda: True)
    cold = _game(resolved=True, memory_block="[RETURNING TABLE] 12 games",
                 spoken=False).greeting_instructions()
    assert "memory KNOWS this TABLE" not in cold

    after = _game(resolved=True, memory_block="[RETURNING TABLE] 12 games",
                  spoken=True).greeting_instructions()
    assert "memory KNOWS this TABLE" in after


# -- V1: no name without a present-voice match --------------------------------


def test_the_rami_rhonda_chris_miranda_greeting_is_unreproducible(monkeypatch):
    """THE live failure. With four remembered names and NO voice matched
    present, neither the greeting (opener or deferred beat) nor the
    late-recognition beat may recite any of them."""
    monkeypatch.setattr(lily_config, "voice_identity_enabled", lambda: True)

    # Greeting surface — both the cold opener and the deferred beat.
    for spoken in (False, True):
        g = _game(resolved=True, memory_block="[RETURNING TABLE] Rami — 4 wins",
                  spoken=spoken)
        g.memory_player_names = list(FOUR)
        text = g.greeting_instructions()
        for name in FOUR:
            assert name not in text, f"{name!r} recited at spoken={spoken}"

    # Late-recognition surface — the promotion-path beat that leaked the roster.
    game = _make_game()
    game.say_registry.claim("session_greet", owner="g1")
    game.memory_block = "[RETURNING TABLE] Rami — 4 wins"
    game.memory_player_names = list(FOUR)
    game.maybe_fire_late_recognition()
    assert len(game.instructed_replies) == 1
    ack = game.instructed_replies[0]
    for name in FOUR:
        assert name not in ack, f"{name!r} recited in the late-recognition beat"


def test_no_name_is_spoken_without_a_present_voice_match(monkeypatch):
    """The naming contract: name a person ONLY from the ROSTER (present-voice)
    authority, never from remembered names."""
    monkeypatch.setattr(lily_config, "voice_identity_enabled", lambda: True)
    game = _make_game()                       # sk has no bound speakers
    game.say_registry.claim("session_greet", owner="g1")
    game.memory_block = "[RETURNING TABLE] Rami — 4 wins"
    game.memory_player_names = ["Rami"]
    game.maybe_fire_late_recognition()
    ack = game.instructed_replies[0]
    assert "Rami" not in ack
    assert "ROSTER field is the sole naming authority" in ack
    assert "do not read any roster of names from memory" in ack


def test_a_returning_enrolled_player_is_resolved_from_voice(monkeypatch):
    """When a voice IS matched present, the present name lives in the ROSTER
    authority line (the sole naming source the beat routes through) — this is
    what authorizes naming, not memory_player_names."""
    monkeypatch.setattr(lily_config, "voice_identity_enabled", lambda: True)
    game = _make_game()
    game.sk.bind_speaker("S1", "Rami")        # a present voice resolves
    assert list(game.sk.players) == ["Rami"]
    roster = game._roster_authority_line()
    assert roster is not None and "Rami" in roster

    game.say_registry.claim("session_greet", owner="g1")
    game.memory_block = "[RETURNING TABLE] Rami — 4 wins"
    game.memory_player_names = ["Rami"]
    game.maybe_fire_late_recognition()
    ack = game.instructed_replies[0]
    # The beat itself does not inline the name; it delegates naming to the
    # ROSTER field, which now carries the present voice.
    assert "ROSTER field is the sole naming authority" in ack
