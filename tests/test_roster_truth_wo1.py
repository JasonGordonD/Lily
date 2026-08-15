"""WO-LILY-ROSTER-TRUTH-001 — the roster tells one truth on every surface.

Live 2026-08-15 13:47 EDT (lily-359C62-5613a25a), root-caused with DB
proof (lily_sessions persisted players.Rami placeholder:true with 39s of
talk time and an answer attempted; a phantom "UU" seat; final_standings
["Player", "Player 2"]):

  D1  bind_speaker's migration loop requires other_name != name, so a
      bind landing on a placeholder already KEYED by that very name fell
      to setdefault and never cleared placeholder:True — every surface
      anonymized the real player forever.
  D2  the placeholder hook runs ~0.5ms after end-of-turn but diarization
      labels land ~1.3s later: present_placeholder_label() invented the
      hardcoded "UU" anchor off an EMPTY sightings map (a seat for a
      voice nobody observed), the next turn minted a SECOND seat off the
      by-then-observed label, and no solo-arity detector existed for
      "Just me. Myself and I.".
  D3  the players attribute payload said "Player 2" while the
      player_bind beat said "Rami" — two channels, two names, three
      chips at a one-man table. The payload (stamped with a monotonic
      roster_gen) is now the single roster-truth wire; beats reference
      the gen, only animate, and corrections air as rename/unbind.
  D4  stored relaxed pacing sat in the STAGED (unverified) device
      candidate while self.prefs stayed empty, so Q1 aired a timer
      against the table's standing choice. Pacing — and ONLY pacing —
      now weakly applies from the staged candidate (identity boundary:
      no names/history from an unverified candidate, no write-back).

Fixture: tests/fixtures/live_20260815_1347_roster.txt (S13; verbatim
from lily_transcripts rows 4136-4158). Tests (i)-(iii) and (v) fail on
pre-WO code — verified by running this file against the unpatched tree.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_scorekeeper
from lily_agent import LilyGame
from lily_scorekeeper import (
    LilyScorekeeper,
    lily_detect_solo_assertion,
    lily_parse_lobby_setup_intents,
    lily_surface_names,
)

FIXTURE = Path(__file__).parent / "fixtures" / "live_20260815_1347_roster.txt"


def _phantom_uu_seat() -> dict:
    """The exact legacy seat shape the live DB row persisted — a "UU"
    placeholder minted by the pre-WO empty-sightings default. Injected
    directly because the mint path now (correctly) refuses to create it."""
    return {
        "speaker_label": "UU",
        "speaker_id": None,
        "score": 0,
        "streak": 0,
        "talk_time_s": 0.0,
        "answers_attempted": 0,
        "answers_correct": 0,
        "last_correct_category": None,
        "questions_since_spoke": 2,
        "lobby_fact": None,
        "lifeline_available": True,
        "placeholder": True,
    }


# ---------------------------------------------------------------------------
# D1 — placeholder flag must not survive a same-key bind
# ---------------------------------------------------------------------------


def test_same_key_bind_clears_placeholder_flag():
    """(i) ensure_present_placeholder("Rami") then bind_speaker("Rami",
    "Rami") — the live shape: a biometric name-shaped diarization label
    keys the placeholder, then the bind tool binds that very name. The
    other_name != name migration loop skips it; the flag must clear at
    the setdefault exit too. FAILS pre-WO (flag survived; every surface
    said "Player 2")."""
    sk = LilyScorekeeper("wo1-d1")
    sk.unrostered_labels["Rami"] = 3
    assert sk.ensure_present_placeholder("Rami") == "Rami"
    assert sk.players["Rami"].get("placeholder") is True

    sk.bind_speaker("Rami", "Rami")

    assert not sk.players["Rami"].get("placeholder")
    assert sk.has_active_placeholder() is False
    assert sk.real_player_names() == ["Rami"]
    assert lily_surface_names(sk.players)["Rami"] == "Rami"


def test_same_key_adoption_emits_rename_and_bumps_gen():
    """The adoption is a MIGRATION: the glass gets a rename beat naming
    the seat's previous surface ("Player" -> "Rami"), and roster_gen is
    monotonic across the mint and the adopt."""
    sk = LilyScorekeeper("wo1-d1-events")
    sk.unrostered_labels["Rami"] = 1
    sk.ensure_present_placeholder("Rami")
    gen_after_mint = sk.roster_gen
    assert gen_after_mint >= 1

    sk.bind_speaker("Rami", "Rami")
    assert sk.roster_gen > gen_after_mint

    events = sk.drain_roster_events()
    gens = [e["gen"] for e in events]
    assert gens == sorted(gens)
    adopt = next(e for e in events if e["kind"] == "adopt")
    assert adopt["old_surfaced"] == "Player"
    assert adopt["new"] == "Rami"
    # Drained means drained — the publisher consumes exactly once.
    assert sk.drain_roster_events() == []


def test_label_keyed_placeholder_still_migrates_on_bind():
    """Regression guard for the pre-existing migration path (HOTFIX-010
    V5): a placeholder keyed "S1" binding to "Rami" migrates history and
    retires the placeholder key — and now also emits the rename event."""
    sk = LilyScorekeeper("wo1-d1-migrate")
    sk.unrostered_labels["S1"] = 1
    sk.ensure_present_placeholder("S1")
    sk.apply_score_event(
        "S1", cause="answer", correct=True, points=3,
        question_id="q1", transcript="Jupiter",
    )
    sk.bind_speaker("S1", "Rami")

    assert set(sk.players) == {"Rami"}
    assert not sk.players["Rami"].get("placeholder")
    assert sk.ledger_scores() == {"Rami": 3}
    migrate = next(
        e for e in sk.drain_roster_events() if e["kind"] == "migrate"
    )
    assert migrate["old_surfaced"] == "Player"
    assert migrate["new"] == "Rami"


# ---------------------------------------------------------------------------
# D2a/D2c — no seat for an unobserved voice; one placeholder max
# ---------------------------------------------------------------------------


def test_two_placeholder_hooks_pre_bind_mint_at_most_one_seat():
    """(ii) Hook 1 fires with an EMPTY sightings map (diarization lands
    ~1.3s later): minting must DEFER — never the "UU" default. Hook 2
    fires with one observed label: exactly one seat, keyed to it. FAILS
    pre-WO (hook 1 minted the "UU" phantom, hook 2 minted a second)."""
    sk = LilyScorekeeper("wo1-d2")

    # Hook 1 — nothing observed yet.
    assert sk.present_placeholder_label() is None
    assert sk.ensure_present_placeholder("") is None  # legacy direct call
    assert sk.players == {}

    # Hook 2 — the label has landed.
    sk.unrostered_labels["Rami"] = 1
    label = sk.present_placeholder_label()
    assert label == "Rami"
    assert sk.ensure_present_placeholder(label) == "Rami"

    # Diarizer churn under a third label never forks a second seat.
    sk.unrostered_labels["S2"] = 1
    assert sk.ensure_present_placeholder("S2") is None

    assert sk.roster_size() == 1
    assert "UU" not in sk.players
    assert sk.has_active_placeholder() is True


def test_observed_uu_label_is_still_bindable():
    """"UU" as an OBSERVED sighting stays legal — the ban is on inventing
    it off an empty map."""
    sk = LilyScorekeeper("wo1-d2-uu")
    sk.unrostered_labels["UU"] = 2
    assert sk.present_placeholder_label() == "UU"
    assert sk.ensure_present_placeholder("UU") == "UU"
    sk.bind_speaker("UU", "Rami")
    assert set(sk.players) == {"Rami"}
    assert not sk.players["Rami"].get("placeholder")


# ---------------------------------------------------------------------------
# D2b — the solo assertion clamps the roster
# ---------------------------------------------------------------------------


def test_solo_assertion_detector_phrases():
    positives = [
        "Just me. Myself and I.",
        "just me",
        "Only me tonight.",
        "I'm playing solo",
        "It's just me, by myself.",
        "Nobody else is here.",
        "solo",
        "It's solo.",
        "me, myself and I",
    ]
    negatives = [
        "Just me and Sarah tonight.",
        "Only me and him.",
        "Han Solo",
        "What is this?",
        "Why are these three players here?",
        "There are four of us.",
        "",
    ]
    for text in positives:
        assert lily_detect_solo_assertion(text), text
    for text in negatives:
        assert not lily_detect_solo_assertion(text), text


def test_lobby_setup_intents_carry_solo():
    intents = lily_parse_lobby_setup_intents("Just me. Myself and I.")
    assert intents["solo"] is True
    assert intents["start"] is False
    assert lily_parse_lobby_setup_intents("let's play")["solo"] is False
    assert lily_parse_lobby_setup_intents("")["solo"] is False


def test_solo_assertion_clamps_roster_to_the_speaking_voice():
    """(iii) The live shape: "Rami" bound plus the phantom "UU"
    placeholder. "Just me. Myself and I." clamps the roster to the
    asserting voice — size 1, phantom retired, retirement drained as an
    unbind-able event, and future placeholder mints for other labels
    refused. FAILS pre-WO (no solo intent existed; the phantom stayed)."""
    sk = LilyScorekeeper("wo1-d2b")
    sk.players["UU"] = _phantom_uu_seat()
    sk.bind_speaker("Rami", "Rami")
    assert sk.roster_size() == 2
    sk.drain_roster_events()

    assert lily_detect_solo_assertion("Just me. Myself and I.")
    retired = sk.clamp_roster_solo("Rami")

    assert retired == ["UU"]
    assert sk.roster_size() == 1
    assert set(sk.players) == {"Rami"}
    assert sk.solo_voice_label == "Rami"
    retire = next(
        e for e in sk.drain_roster_events() if e["kind"] == "retire"
    )
    assert retire["old_surfaced"] == "Player"  # never the raw label
    assert retire["label"] == "UU"
    # The latch refuses later phantom mints for other labels…
    sk.unrostered_labels["S3"] = 1
    assert sk.ensure_present_placeholder("S3") is None
    assert sk.roster_size() == 1


def test_solo_clamp_never_retires_named_players_and_binds_clear_latch():
    """Multiplayer stays intact: named seats survive a joking "just me",
    and a real bind on a different voice clears the latch."""
    sk = LilyScorekeeper("wo1-d2b-multi")
    sk.bind_speaker("S1", "Rami")
    sk.bind_speaker("S2", "Dave")
    sk.clamp_roster_solo("S1")
    assert set(sk.players) == {"Rami", "Dave"}  # nobody retired
    assert sk.solo_voice_label == "S1"

    sk.bind_speaker("S3", "Priya")  # a third real voice binds
    assert sk.solo_voice_label is None  # latch cleared
    assert set(sk.players) == {"Rami", "Dave", "Priya"}


def test_solo_clamp_without_label_retires_nothing():
    sk = LilyScorekeeper("wo1-d2b-nolabel")
    sk.players["UU"] = _phantom_uu_seat()
    assert sk.clamp_roster_solo(None) == []
    assert sk.clamp_roster_solo("") == []
    assert "UU" in sk.players  # cannot tell the speaker's seat from a phantom
    assert sk.solo_voice_label is None


# ---------------------------------------------------------------------------
# D3 — one roster truth on the wire: beat/payload parity, corrections air
# ---------------------------------------------------------------------------


def _make_game() -> tuple[LilyGame, list]:
    """on_speaker_bound fixture per test_hotfix009_w7_name_projection —
    real scorekeeper, real publish_roster_events, captured beats."""
    game = LilyGame.bare()
    game.sk = LilyScorekeeper("wo1-d3")
    game.supabase = None
    game.memory_block = ""
    game._pending_unbound_award = None
    game.fire_enrollment = lambda trigger: None
    game._enroll_started = True
    game.request_device_verification = lambda trigger: None
    game._maybe_retune_stt_for_roster = lambda: None
    game._maybe_auto_start_after_lobby = lambda: None
    game.publish_attributes_nowait = lambda: None
    events: list = []
    game.send_event_nowait = (
        lambda kind, payload: events.append((kind, dict(payload)))
    )

    async def _noop_recognize(name):  # keeps on_speaker_bound loop-safe
        return None

    game.maybe_recognize_by_stated_name = _noop_recognize
    return game, events


def test_beat_and_payload_agree_on_a_bound_seat():
    """(iv) The live defect: payload said "Player 2", beat said "Rami".
    After the same-key adoption fix the two channels must carry the SAME
    verbatim name, and the correction airs as a rename beat referencing
    roster_gen. FAILS pre-WO."""
    game, events = _make_game()
    sk = game.sk
    sk.unrostered_labels["Rami"] = 1
    sk.ensure_present_placeholder("Rami")
    assert [p["name"] for p in game._players_payload()] == ["Player"]

    sk.bind_speaker("Rami", "Rami")
    game.on_speaker_bound("Rami", "Rami")

    payload_names = [p["name"] for p in game._players_payload()]
    assert payload_names == ["Rami"]
    bind_beats = [p for k, p in events if k == "player_bind"]
    assert bind_beats and bind_beats[-1]["name"] == "Rami"
    assert bind_beats[-1]["roster_gen"] == sk.roster_gen
    renames = [p for k, p in events if k == "rename"]
    assert renames and renames[-1]["old_name"] == "Player"
    assert renames[-1]["new_name"] == "Rami"
    assert renames[-1]["roster_gen"] >= 1
    # Nothing on either channel ever said "Player 2".
    assert all("Player 2" not in str(p) for _, p in events)


def test_solo_assertion_through_the_agent_publishes_unbind():
    game, events = _make_game()
    sk = game.sk
    sk.players["UU"] = _phantom_uu_seat()
    sk.bind_speaker("Rami", "Rami")
    game.game_started = False
    game._setup_requested = set()
    game._setup_pending = set()

    intents = game.note_lobby_setup_intents(
        "Just me. Myself and I.", speaker_label="Rami"
    )

    assert intents["solo"] is True
    assert set(sk.players) == {"Rami"}
    unbinds = [p for k, p in events if k == "unbind"]
    assert unbinds and unbinds[-1]["name"] == "Player"
    assert unbinds[-1]["speaker_label"] == "UU"
    assert unbinds[-1]["roster_gen"] == sk.roster_gen


def test_rummy_to_rami_rename_airs_the_real_old_name():
    """The W7 same-voice correction path now also reaches the glass as a
    rename beat carrying the real previous spelling."""
    game, events = _make_game()
    sk = game.sk
    sk.bind_speaker("S1", "Rummy")
    game.on_speaker_bound("S1", "Rummy")
    events.clear()

    sk.bind_speaker("S1", "Rami", rename=True)
    game.on_speaker_bound("S1", "Rami")

    renames = [p for k, p in events if k == "rename"]
    assert renames and renames[-1]["old_name"] == "Rummy"
    assert renames[-1]["new_name"] == "Rami"
    assert [p["name"] for p in game._players_payload()] == ["Rami"]


def _call_bind_tool(game: LilyGame, label: str, name: str) -> str:
    from lily_agent import LilyAgent

    agent = LilyAgent.__new__(LilyAgent)
    agent._game = game
    return asyncio.run(
        LilyAgent.lily_bind_speaker.__wrapped__(agent, None, label, name)
    )


def test_bind_tool_receipt_is_honest_over_a_degraded_seat():
    """S2 — receipts never lie: the live 13:47 bind returned "Bound:
    voice Rami is Rami." while the committed seat kept placeholder:True.
    The tool now verifies the COMMITTED seat; a degraded commit gets a
    degraded receipt. Simulated by a bind writer that leaves the flag —
    the exact pre-WO behavior."""
    game, _events = _make_game()
    game.fragments = __import__("lily_binding").LilyFragmentAccumulator()
    game._confirmed_name_evidence = {}
    game.note_confirmed_name_evidence("Rami", "Rami")
    sk = game.sk

    def degraded_bind(label, name, **kwargs):
        seat = LilyScorekeeper.bind_speaker(sk, label, name, **kwargs)
        seat["placeholder"] = True  # the pre-WO commit shape
        return seat

    sk.bind_speaker = degraded_bind
    msg = _call_bind_tool(game, "Rami", "Rami")
    assert "DEGRADED" in msg
    assert not msg.startswith("Bound:")


def test_bind_tool_receipt_clean_after_fix():
    """The same-key adoption now commits cleanly, so the honest receipt is
    the ordinary success string — parity restored end to end."""
    game, _events = _make_game()
    game.fragments = __import__("lily_binding").LilyFragmentAccumulator()
    game._confirmed_name_evidence = {}
    game.note_confirmed_name_evidence("Rami", "Rami")
    sk = game.sk
    sk.unrostered_labels["Rami"] = 1
    sk.ensure_present_placeholder("Rami")

    msg = _call_bind_tool(game, "Rami", "Rami")

    assert msg.startswith("Bound: voice Rami is Rami.")
    assert not sk.players["Rami"].get("placeholder")
    assert [p["name"] for p in game._players_payload()] == ["Rami"]


# ---------------------------------------------------------------------------
# D4 — staged pacing reaches game start; identity boundary holds
# ---------------------------------------------------------------------------


def _pacing_game() -> LilyGame:
    game = LilyGame.bare()
    game.sk = LilyScorekeeper("wo1-d4")
    game.prefs = {}
    return game


def test_staged_relaxed_pacing_applies_at_game_start():
    """(v) prefs empty, device-candidate prefs staged with relaxed:
    Q1 must not arm against pacing:"timed". FAILS pre-WO (self.prefs
    empty meant the staged choice was unreachable)."""
    game = _pacing_game()
    game._device_candidate_prefs = {"pacing": "relaxed"}
    game.apply_prefs_at_game_start()
    assert game.sk.pacing == "relaxed"


def test_session_spoken_pacing_outranks_staged_candidate():
    game = _pacing_game()
    game.prefs = {"pacing": "timed"}
    game._device_candidate_prefs = {"pacing": "relaxed"}
    game.apply_prefs_at_game_start()
    assert game.sk.pacing == "timed"


def test_weak_apply_respects_the_identity_boundary():
    """ONLY the pacing key crosses the quarantine: no write-back into
    self.prefs (that would launder an unverified candidate's prefs under
    the current group id), no this-session provenance, no other staged
    keys applied."""
    game = _pacing_game()
    game._device_candidate_prefs = {
        "pacing": "relaxed",
        "media_mode": "pictures",  # another feature's key — stays staged
    }
    game.apply_prefs_at_game_start()
    assert game.sk.pacing == "relaxed"
    assert game.prefs == {}  # nothing written back
    assert not getattr(game, "_pacing_stated_this_session", False)
    assert game.sk.media_mode == "voice_only"  # untouched


def test_garbage_staged_pacing_is_ignored():
    game = _pacing_game()
    game._device_candidate_prefs = {"pacing": "warp-speed"}
    before = game.sk.pacing
    game.apply_prefs_at_game_start()
    assert game.sk.pacing == before


# ---------------------------------------------------------------------------
# S13 — the 13:47 live fixture drives the detectors
# ---------------------------------------------------------------------------


def _fixture_lines() -> list[tuple[str, str]]:
    rows = []
    for line in FIXTURE.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        _ts, speaker, text = (part.strip() for part in line.split("|", 2))
        rows.append((speaker, text))
    return rows


def test_live_fixture_is_committed_and_parses():
    assert FIXTURE.is_file()
    rows = _fixture_lines()
    assert len(rows) == 23  # lily_transcripts rows 4136-4158
    assert "lily-359C62-5613a25a" in FIXTURE.read_text(encoding="utf-8")


def test_live_fixture_solo_line_fires_and_neighbors_do_not():
    rami_lines = [text for speaker, text in _fixture_lines()
                  if speaker == "Rami"]
    solo = [t for t in rami_lines if lily_detect_solo_assertion(t)]
    assert any("Just me. Myself and I." in t for t in solo)
    # The arity complaints are questions, not assertions.
    assert not lily_detect_solo_assertion("[Rami] What is this?")
    assert not lily_detect_solo_assertion(
        "[Rami] Why are these three players here?"
    )
    # And the D4 evidence really is in this call.
    assert any("play relaxed" in t for t in rami_lines)


def test_live_fixture_replay_ends_with_one_true_seat():
    """Replay the roster-relevant moments of the call against the fixed
    code: greeting turn (no diarization yet — no seat), the phantom the
    old code minted cannot mint, the name binds, the solo line clamps.
    End state: ONE seat, named Rami, no placeholder, surfaces verbatim."""
    sk = LilyScorekeeper("wo1-replay")

    # 17:47:37 turn hook — diarization not landed yet: defer, no "UU".
    assert sk.present_placeholder_label() is None
    assert sk.players == {}

    # The label lands (name-shaped, biometric); next hook mints one seat.
    sk.unrostered_labels["Rami"] = 1
    sk.ensure_present_placeholder(sk.present_placeholder_label())

    # The bind tool fires (LLM TTFT ~5.5s later): same-key adoption.
    sk.bind_speaker("Rami", "Rami")

    # 17:48:02 — "Just me. Myself and I."
    assert lily_detect_solo_assertion("Just me. Myself and I.")
    sk.clamp_roster_solo("Rami")

    assert sk.roster_size() == 1
    assert sk.real_player_names() == ["Rami"]
    assert sk.has_active_placeholder() is False
    assert lily_surface_names(sk.players) == {"Rami": "Rami"}
    # The persisted shape can no longer be ["Player", "Player 2"].
    assert "UU" not in sk.players


def test_roster_gen_is_monotonic_across_mutations():
    sk = LilyScorekeeper("wo1-gen")
    seen = [sk.roster_gen]
    sk.unrostered_labels["S1"] = 1
    sk.ensure_present_placeholder("S1")
    seen.append(sk.roster_gen)
    sk.bind_speaker("S1", "Rami")
    seen.append(sk.roster_gen)
    sk.bind_speaker("S2", "Dave")
    seen.append(sk.roster_gen)
    sk.players["UU"] = _phantom_uu_seat()
    sk.clamp_roster_solo("S1")  # named seats survive; UU retires
    seen.append(sk.roster_gen)
    assert seen == sorted(seen)
    assert len(set(seen)) == len(seen)  # strictly increasing
