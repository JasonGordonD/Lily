"""WO-LILY-HOTFIX-006 N1 — the greeting races the identity match.

Live evidence, 2026-08-08, three consecutive sessions:

  lily-4FB3B2  transcript carries `[RETURNING TABLE] — matched to past game
               sessions! Table history on file: 12 game(s) played…` — landing
               roughly two and a half minutes AFTER Lily had already said
               "tonight is actually a clean slate — I don't have any…"
  lily-16A9AE  "my memory bank is sitting on a completely clean slate for
               you all right now."
  lily-D99BE7  same table, same night.

The matcher was never wrong. It found the real table and twelve prior games.
It was simply not waited for, and its absence was narrated as a fact.

The distinction this file pins is the whole defect: "I have no memory of
you" and "I do not know YET whether I have memory of you" are different
statements, and only the second was ever true at greeting time. Saying
nothing about memory is always available and always honest.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_config
from lily_agent import LilyGame
from lily_scorekeeper import LilyScorekeeper


def _game(*, resolved, supabase=object(), memory_block="", spoken=True):
    """Built on the harness test_recognition_variety already uses to drive
    greeting_instructions — LilyGame is constructed via __new__, so every
    attribute the greeting reads has to be supplied explicitly.

    HOTFIX-010 V3: the recognition / first-time / gap-naming beats compose
    only after the first human utterance. These N1 fixtures assert on that
    deferred content, so they default spoken=True; the cold-opener contract
    is pinned separately in test_hotfix010_identity_resequence."""
    from test_recognition_variety import _make_game

    g = _make_game()
    g.supabase = supabase
    g.memory_block = memory_block
    g.memory_total_games = 12 if memory_block else 0
    g.memory_player_names = ["Rami", "Rhonda", "Chris"] if memory_block else []
    g.device_candidate_group_id = None
    g._voice_identity_resolved = resolved
    g._recognized_at_greet = False
    g._first_human_utterance_seen = spoken
    g.forget_state = None
    g.group_prefs = None
    # The recognition branch stamps a feature version, which persists
    # prefs asynchronously. This fixture asserts on GREETING TEXT, so
    # the write is stubbed rather than dragging a loop into it.
    g.persist_prefs = lambda *a, **k: None
    return g


def _no_memory_claim(text: str) -> bool:
    """No phrasing that asserts the ABSENCE of memory."""
    lowered = text.lower()
    return not any(
        phrase in lowered
        for phrase in (
            "clean slate", "blank slate", "blank card",
            "doesn't have you", "does not have you",
            "nothing on file", "no record of you",
        )
    )


# -- the defect ---------------------------------------------------------------


def test_no_memory_claim_while_the_probe_is_outstanding(monkeypatch):
    """THE fixture. A probe still running means the absence of memory is
    UNKNOWN, not established — and an unknown may not be spoken as a fact."""
    monkeypatch.setattr(lily_config, "voice_identity_enabled", lambda: True)
    g = _game(resolved=False)
    assert g.identity_probe_outstanding() is True
    text = g.greeting_instructions()
    assert "MEMORY IS UNRESOLVED, NOT ABSENT" in text
    assert "Say " in text and "NOTHING about your memory" in text


def test_the_gap_naming_beat_is_suppressed_while_the_probe_is_out(monkeypatch):
    """'My table card doesn't have you tonight' is itself a memory claim.
    It is honest AFTER the probe returns and a lie before it."""
    monkeypatch.setattr(lily_config, "voice_identity_enabled", lambda: True)
    out = _game(resolved=False).greeting_instructions()
    # The gap-naming beat may still appear in the base text, but the
    # override that forbids speaking it must come after it. (HOTFIX-010 V1:
    # the gap beat is now voice-framed.)
    assert out.index("MEMORY IS UNRESOLVED") > out.index(
        "I don't recognise the voice yet"
    )


def test_a_resolved_probe_may_speak_the_gap(monkeypatch):
    """Once the probe has come back empty, the gap is a known fact and
    naming it plainly is the honest thing — this is the behaviour N1 must
    NOT regress."""
    monkeypatch.setattr(lily_config, "voice_identity_enabled", lambda: True)
    g = _game(resolved=True)
    assert g.identity_probe_outstanding() is False
    assert "MEMORY IS UNRESOLVED" not in g.greeting_instructions()


def test_voice_identity_disabled_is_not_an_outstanding_probe(monkeypatch):
    """Nothing is in flight when the feature is off — she may speak
    normally rather than being permanently gagged about memory."""
    monkeypatch.setattr(lily_config, "voice_identity_enabled", lambda: False)
    assert _game(resolved=False).identity_probe_outstanding() is False


def test_no_supabase_is_not_an_outstanding_probe(monkeypatch):
    monkeypatch.setattr(lily_config, "voice_identity_enabled", lambda: True)
    assert _game(resolved=False, supabase=None).identity_probe_outstanding() is False


# -- protected: the matcher's own success path --------------------------------


def test_a_known_table_still_gets_recognition_not_silence(monkeypatch):
    """PROTECTED. N1 fixes WHEN the matcher is used, never WHETHER. A table
    whose memory is already loaded still gets the full recognition beat."""
    monkeypatch.setattr(lily_config, "voice_identity_enabled", lambda: True)
    g = _game(resolved=False, memory_block="[RETURNING TABLE] 12 games")
    text = g.greeting_instructions()
    assert "memory KNOWS this TABLE" in text
    assert "MEMORY IS UNRESOLVED" not in text, (
        "a table we DO recognise must not be gagged"
    )


def test_a_genuinely_new_table_still_gets_a_warm_first_time_greeting(monkeypatch):
    """PROTECTED. The probe resolving empty is the ordinary new-table case
    and must still ask whether it's their first time."""
    monkeypatch.setattr(lily_config, "voice_identity_enabled", lambda: True)
    text = _game(resolved=True).greeting_instructions()
    assert "first time playing with you" in text
    assert "Hi, I'm Lily" in text


def test_the_self_intro_survives_every_branch(monkeypatch):
    """PROTECTED. A live session once opened with 'welcome back everyone'
    and no self-intro; the one-breath intro is unconditional."""
    monkeypatch.setattr(lily_config, "voice_identity_enabled", lambda: True)
    for resolved in (True, False):
        assert "Hi, I'm Lily" in _game(resolved=resolved).greeting_instructions()
