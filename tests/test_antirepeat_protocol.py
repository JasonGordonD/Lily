"""WO-LILY-ANTIREPEAT-PROTOCOL-001 — one continuous take, and the durable
recognition_aired fact.

THE 11:31 FIXTURE (live 2026-08-14): the player stated their name; the
name door promoted the group mid-turn, so the ORGANIC reply already in
flight picked up the just-injected [RETURNING TABLE] block and welcomed
the table back — and the promotion tail ALSO fired the late-recognition
beat, which aired the same welcome-back content ten seconds later. Two
independent lanes, one fact, no shared "recognition stated on air" record
(the beat dispatches keyless, so no registry dedupe applies; the
_recognized_at_greet kill-switch was initialized and never set).

Pinned here, following the BARGE-RESILIENCE-001 _result_aired pattern:
  - name-door promotions short-circuit the late beat (the organic turn
    carries the memory by construction) and stamp recognition_aired;
  - the durable fact retires the late beat PERMANENTLY, so an ECAPA voice
    match converging later on a fragmented returner's second group (the
    group-equality guard's blind spot) airs nothing;
  - stored pacing still applies at the promotion (it used to apply only
    inside the late beat);
  - the greet that composes the returning-table acknowledgment stamps the
    fact when it CONFIRMS on air — the wiring _recognized_at_greet never
    had (the inert flag is deleted);
  - the system prompt carries the CONTINUITY PROTOCOL block exactly once.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_say_gate
from lily_agent import LILY_SYSTEM_PROMPT, LilyGame
from lily_scorekeeper import LilyScorekeeper


def _game(**kw):
    game = LilyGame.bare()
    game.sk = LilyScorekeeper("antirepeat-1131")
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game.group_id = "lily-ROOM"
    game.group_id_source = "room_name"
    game.supabase = object()
    game.stt = None
    game.forget_state = "idle"
    game.device_candidate_group_id = None
    game.device_candidate_source = None
    game.device_identity_verified = False
    game.device_identity_rejected = False
    game._device_candidate_memory = None
    game._device_candidate_memory_block = ""
    game._device_candidate_prefs = {}
    game._device_candidate_voiceprints = []
    game.memory_block = ""
    game.memory_total_games = 0
    game.memory_player_names = []
    game.memory_settled = asyncio.Event()
    game.prefs = {}
    game.armed_question = None
    game.game_started = False
    game.game_over = False
    game.pending_clarify = None
    game._prefs_offer_made = False
    game._memory_disclosure_offered = False
    game._whats_new_pending = False
    game.persist_prefs = lambda *a, **k: None
    game.dispatches = []
    game.gated_say = (
        lambda key, act, instr, source=None, **kwargs:
        game.dispatches.append((key, act, instr, source)) or True
    )

    async def _upgrade(new_group_id, source):
        game.group_id = new_group_id
        game.group_id_source = source

    game.upgrade_group_id = _upgrade
    # The 11:31 shape: the opener already aired; the name lands mid-lobby.
    game.say_registry.claim("session_greet", owner="greet-1")
    for k, v in kw.items():
        setattr(game, k, v)
    return game


def _stage(game, group, prefs=None):
    game.device_candidate_group_id = group
    game.device_candidate_source = "name_stated"
    game._device_candidate_memory = {
        "total_games": 18, "player_names": ["Rami"],
    }
    game._device_candidate_memory_block = (
        "[RETURNING TABLE]\nplayers: Rami\ntotal games: 18"
    )
    game._device_candidate_prefs = dict(prefs or {})
    game._device_candidate_voiceprints = []


def _late_beats(game):
    return [d for d in game.dispatches if d[1] == "late_recognition"]


# -- the 11:31 double welcome-back: name-door short-circuit -------------------


def test_1131_name_door_promotion_fires_no_late_beat():
    """The organic reply answering the name utterance carries the promoted
    memory block by construction — firing the late beat too is the double."""
    game = _game()
    _stage(game, "grp_rami")
    asyncio.run(game._promote_device_candidate("name_stated", verified=False))
    assert game.memory_block  # promotion landed; the organic turn has it
    assert _late_beats(game) == []
    # Retired, not deferred — no seam flush may resurrect it.
    assert game._late_recognition_fired is True
    assert game._late_recognition_pending is False
    fact = game.recognition_aired()
    assert fact is not None and fact["source"] == "name_door_organic"
    assert game.flush_late_recognition_at_seam() is False
    assert _late_beats(game) == []


def test_device_plus_name_promotion_short_circuits_too():
    """The device+name door is the same stated-name moment — same organic
    turn in flight, same short-circuit."""
    game = _game()
    _stage(game, "grp_rami")
    asyncio.run(
        game._promote_device_candidate("device_plus_name", verified=False)
    )
    assert _late_beats(game) == []
    assert game.recognition_aired()["source"] == "name_door_organic"


def test_voice_identity_promotion_still_fires_the_beat_once():
    """The short-circuit is name-door-scoped: a biometric match landing
    with no organic name turn in flight still gets its catch-up beat —
    exactly once, and the dispatch stamps the durable fact."""
    game = _game()
    _stage(game, "grp_rami")
    asyncio.run(game._promote_device_candidate("voice_identity_match"))
    assert len(_late_beats(game)) == 1
    fact = game.recognition_aired()
    assert fact is not None and fact["source"] == "late_recognition_beat"
    # A second promotion of any kind airs nothing more.
    _stage(game, "grp_rami_frag2")
    asyncio.run(game._promote_device_candidate("voice_identity_match"))
    assert len(_late_beats(game)) == 1


# -- the durable fact retires every recognition lane --------------------------


def test_recognition_aired_retires_the_late_beat_permanently():
    game = _game()
    game.memory_block = "[RETURNING TABLE]\n18 games."
    game.note_recognition_aired("name_door_organic")
    assert game.maybe_fire_late_recognition() is False
    assert _late_beats(game) == []
    assert game._late_recognition_fired is True
    # A stray pending bit cannot resurrect it at a seam.
    game._late_recognition_pending = True
    assert game.flush_late_recognition_at_seam() is False
    assert _late_beats(game) == []
    assert game._late_recognition_pending is False


def test_note_recognition_aired_is_idempotent_first_airing_wins():
    game = _game()
    game.note_recognition_aired("greet")
    game.note_recognition_aired("late_recognition_beat")
    assert game.recognition_aired()["source"] == "greet"


def test_refused_beat_does_not_stamp_and_rearms():
    """A gate refusal (hold/floor/flight) burns nothing: no fact, pending
    re-armed — the beat is still owed."""
    game = _game()
    game.memory_block = "[RETURNING TABLE]\n18 games."
    game.gated_say = lambda *a, **kw: False
    assert game.maybe_fire_late_recognition() is False
    assert game.recognition_aired() is None
    assert game._late_recognition_fired is False
    assert game._late_recognition_pending is True


# -- the full 11:31 trace + the fragmented-returner convergence ---------------


def test_voice_match_after_name_door_airs_nothing():
    """B3: an ECAPA match landing AFTER a name-door promotion on a
    DIFFERENT group (a fragmented returner — the group-id equality guard
    at match time cannot catch it) must air NOTHING new."""
    game = _game()
    _stage(game, "grp_rami_frag1")
    asyncio.run(game._promote_device_candidate("name_stated", verified=False))
    assert _late_beats(game) == []
    # The matcher converges on the same human's second group.
    _stage(game, "grp_rami_frag2", prefs={"pacing": "relaxed"})
    asyncio.run(game._promote_device_candidate("voice_identity_match"))
    assert _late_beats(game) == []
    assert game.dispatches == []  # nothing else aired either
    # The first stamp survives as the record of what the room heard.
    assert game.recognition_aired()["source"] == "name_door_organic"


# -- stored pacing survives the short-circuit ---------------------------------


def test_stored_pacing_applies_at_promotion_despite_short_circuit():
    """The pacing APPLICATION used to live only inside the late beat —
    a short-circuited beat must not cost the table its saved pacing."""
    game = _game()
    _stage(game, "grp_rami", prefs={"pacing": "relaxed"})
    assert game.sk.pacing == "timed"
    asyncio.run(game._promote_device_candidate("name_stated", verified=False))
    assert game.sk.pacing == "relaxed"
    assert _late_beats(game) == []


def test_prefs_offer_rides_the_memory_block_usual_line():
    """The prefs OFFER needs no beat: lily_build_memory_block carries the
    'usual:' line and the system prompt instructs the one ask off it —
    verified so the short-circuit provably drops no player-facing offer."""
    import lily_memory
    block = lily_memory.lily_build_memory_block(
        {"total_games": 3, "player_names": ["Rami"], "sessions": []},
        prefs={"pacing": "relaxed"},
    )
    assert "usual:" in block
    assert '"usual:" line' in LILY_SYSTEM_PROMPT


# -- the greet wiring (replacing the inert _recognized_at_greet) --------------


def test_greet_carried_recognition_stamps_on_confirm():
    """A greet composed WITH the returning-table acknowledgment stamps the
    fact when it genuinely plays out — so the late beat (and every later
    promotion) is retired by the airing, not by an inert flag."""
    game = _game()
    game.say_registry = lily_say_gate.SpeechActRegistry()  # greet not out yet
    game.memory_block = "[RETURNING TABLE]\nplayers: Rami\ntotal games: 18"
    game._first_human_utterance_seen = True
    instr = game.greeting_instructions()
    assert "KNOWS this TABLE" in instr
    assert game._greet_carried_recognition is True
    assert game.recognition_aired() is None  # composed, not yet aired
    game.say_registry.claim("session_greet", owner="s1")
    game._resume_preemptive = lambda: None
    game._pending_reveal_event = None
    game._state_note = None
    game.on_agent_speech_finished("hi, I'm Lily — welcome back", speech_id="s1")
    fact = game.recognition_aired()
    assert fact is not None and fact["source"] == "greet"
    assert game.maybe_fire_late_recognition() is False
    assert _late_beats(game) == []


def test_cut_greet_does_not_stamp():
    """An interrupted greet never confirmed — recognition is still owed,
    so the fact must NOT stamp (the retry or the late beat carries it)."""
    game = _game()
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game.memory_block = "[RETURNING TABLE]\nplayers: Rami\ntotal games: 18"
    game._first_human_utterance_seen = True
    game.greeting_instructions()
    game.say_registry.claim("session_greet", owner="s1")
    game._resume_preemptive = lambda: None
    game._pending_reveal_event = None
    game._state_note = None
    game.on_agent_speech_finished("hi, I'm—", speech_id="s1", interrupted=True)
    assert game.recognition_aired() is None


def test_cold_greet_composes_no_recognition_and_never_stamps():
    game = _game()
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game.greeting_instructions()  # no memory block
    assert game._greet_carried_recognition is False


def test_the_inert_recognized_at_greet_flag_is_gone():
    """The dead kill-switch is DELETED (not left behind): the durable
    recognition_aired fact is its replacement."""
    game = LilyGame.bare()
    assert not hasattr(game, "_recognized_at_greet")
    assert game._recognition_aired is None
    assert game._greet_carried_recognition is False


# -- PART A: the prompt protocol ----------------------------------------------


def test_prompt_carries_the_continuity_protocol_exactly_once():
    assert LILY_SYSTEM_PROMPT.count("CONTINUITY PROTOCOL") == 1
    assert LILY_SYSTEM_PROMPT.count("<continuity>") == 1
    assert LILY_SYSTEM_PROMPT.count("</continuity>") == 1


def test_continuity_protocol_states_the_rails_not_scripts():
    body = LILY_SYSTEM_PROMPT.split("<continuity>", 1)[1].split(
        "</continuity>", 1
    )[0]
    # Rail 1: said stays said — advance, never restate.
    assert "SAID STAYS SAID" in body
    # Rail 2: every beat continues from the last words.
    assert "CONTINUATION" in body
    # Rail 3: one question per spoken turn, 'or'-folded.
    assert "ONE QUESTION PER SPOKEN TURN" in body
    # Rail 4: requested/cut re-airs are re-worded, never verbatim.
    assert "never a verbatim rerun" in body
    # The license: rails bind WHAT repeats, never phrasing.
    assert "improvise freely" in body


def test_lane_specific_reinforcements_survive():
    """The protocol generalizes; the lane-level lines stay as local
    reinforcement (the one-question greeting rule shipped 2026-08-14, the
    lobby ONE-ASK rule, the beat's own anti-reprise clause)."""
    game = _game()
    game.say_registry = lily_say_gate.SpeechActRegistry()
    assert "never stack two" in game.greeting_instructions()
    assert "ONE ASK PER TURN" in LILY_SYSTEM_PROMPT
