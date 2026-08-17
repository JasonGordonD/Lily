"""WO-LILY-RECOG-DELIVERY-001 — the recognition delivery race.

THE 17:51 FIXTURE (live 2026-08-14, session lily-FD3994-358c0ac8, committed
as tests/fixtures/live_20260814_1751_recognition.txt): the name-door
promotion is a fire-and-forget task spawned at bind whose sequential
Supabase awaits completed ~80s AFTER the organic reply to "this is Rami"
aired memory-blind (lily_llm_usage: the reply generated at 13.4k prompt
tokens at 21:51:25-27 and aired 21:51:38; the ~437-token [RETURNING TABLE]
block first appears at 21:52:51). bfadc42's promotion tails stamped
note_recognition_aired("name_door_organic") with ZERO verification — a
receipt that lied (S2) — and maybe_fire_late_recognition hard-returns on
the fact, so every stated-name returner recognized through the only
functioning door got PERMANENT zero recognition.

Pinned here:
  - the race itself, end-to-end through the REAL door/stage/promote/upgrade
    path with a fake Supabase that delays the group lookup until after the
    organic reply's context snapshot: the returner must still HEAR
    recognition-bearing content (FAILS on bfadc42 — the stamp lies and the
    beat is retired);
  - the carried path stamps ONLY on the carrying turn's playout CONFIRM
    (greet-leg discipline), never at the promotion tail;
  - a cut carrying turn re-arms the beat instead of burning recognition;
  - the promotion-owed beat survives game_start_committed (CLASS 7
    exemption) and delivers ONE compact welcome-back at the
    between-questions seam; the forbid stays absolute for other lanes;
  - identity-promotion telemetry (source, group_id, ts,
    short_circuit_decision, carried_memory) recorded and persisted through
    the existing lily_sessions.metadata lane at both write sites (S1);
  - the <continuity> rail names un-aired content as OWED, not banned;
  - the live fixture is committed with its hash (S13).
"""

import asyncio
import hashlib
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_bank
import lily_identity
import lily_memory
import lily_persistence
import lily_say_gate
from lily_agent import LILY_SYSTEM_PROMPT, LilyAgent, LilyGame
from lily_scorekeeper import LilyScorekeeper

FIXTURE = Path(__file__).resolve().parent / "fixtures" / (
    "live_20260814_1751_recognition.txt"
)
FIXTURE_SHA256 = (
    "390a217dbd9f73cf8fdaa856093a5bc9517a38385f6f9cf7555e2a2295985ca4"
)

REAL_TABLE = "grp_rami_regular"


def _game(**kw):
    game = LilyGame.bare()
    game.sk = LilyScorekeeper("lily-FD3994-358c0ac8")
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game.group_id = "lily-FD3994"
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
    # The 17:51 shape: the cold opener already aired.
    game.say_registry.claim("session_greet", owner="greet-1")
    for k, v in kw.items():
        setattr(game, k, v)
    return game


def _patch_persistence(monkeypatch, *, lookup):
    """The fake Supabase lane: every function the door/stage/promote/upgrade
    chain awaits, patched where the identity module reads them."""
    async def _memory(sb, gid):
        return {"total_games": 18, "player_names": ["Rami"], "sessions": []}

    async def _prefs(sb, gid):
        return {}

    async def _voiceprints(sb, gid):
        return []

    async def _rekey(sb, old, new, sid):
        return None

    async def _asked(sb, gid):
        return []

    monkeypatch.setattr(
        lily_persistence, "lily_groups_for_player_name", lookup
    )
    monkeypatch.setattr(lily_memory, "lily_load_group_memory", _memory)
    monkeypatch.setattr(lily_persistence, "lily_load_group_prefs", _prefs)
    monkeypatch.setattr(
        lily_persistence, "lily_load_voiceprints", _voiceprints
    )
    monkeypatch.setattr(lily_persistence, "lily_rekey_group", _rekey)
    monkeypatch.setattr(lily_bank, "lily_load_asked_history", _asked)
    # On bfadc42 the W3 Cut 3 extraction left lily_identity WITHOUT its
    # lily_bank import (upgrade_group_id NameError'd on every real
    # promotion). Injected here so the fixture demonstrates the BLACKOUT on
    # that build rather than the crash; on the fixed build this is a no-op.
    monkeypatch.setattr(lily_identity, "lily_bank", lily_bank, raising=False)


def _confirm(game, text, speech_id="s-organic", **kw):
    game._resume_preemptive = lambda: None
    game._pending_reveal_event = None
    game._state_note = None
    game.on_agent_speech_finished(text, speech_id=speech_id, **kw)


def _recog_dispatches(game):
    return [d for d in game.dispatches if d[1] == "late_recognition"]


# -- THE RACE: the fixture the original work lacked ---------------------------


def test_1751_slow_name_door_still_delivers_recognition(monkeypatch):
    """END-TO-END, the 17:51 blackout: the group lookup completes only
    AFTER the organic reply's context snapshot — the reply airs
    memory-blind. A returner who states their name must still HEAR
    recognition-bearing content within the session (here: the very next
    beat after the promotion lands). On bfadc42 this FAILS: the promotion
    tail stamps 'recognition aired' unverified and every later lane
    hard-returns on it — zero recognition, permanently."""
    game = _game()
    gate = asyncio.Event()

    async def delayed_lookup(sb, name):
        await gate.wait()  # the ~15 sequential Supabase awaits, compressed
        return [REAL_TABLE]

    _patch_persistence(monkeypatch, lookup=delayed_lookup)

    async def scenario():
        # 21:51:22 "this is Rami" -> bind spawns the door fire-and-forget.
        task = asyncio.get_running_loop().create_task(
            game.maybe_recognize_by_stated_name("Rami")
        )
        await asyncio.sleep(0)  # door entered; parked on the slow lookup
        # 21:51:25-27: the organic reply snapshots its context — the
        # promotion has NOT landed, so the snapshot is memory-blind.
        # (getattr: on bfadc42 the per-turn marker does not exist — the
        # test then proceeds and fails on the BLACKOUT assertion below,
        # which is the live defect, not a missing helper.)
        note = getattr(game, "note_generation_snapshot", None)
        if note is not None:
            note()
        assert not game.memory_block
        # 21:51:38: the memory-blind reply airs and confirms.
        _confirm(
            game,
            "Rami — glad you're here. Is it just you tonight, or is "
            "someone else hiding behind the mic?",
        )
        # 21:52:51: the promotion's awaits finally complete.
        gate.set()
        assert await task is True

    asyncio.run(scenario())
    assert game.memory_block.startswith("[RETURNING TABLE]")
    # The un-lied tail: nothing stamped "aired" for a turn that aired
    # memory-blind — the beat delivered instead.
    recog = _recog_dispatches(game)
    assert recog, (
        "the returner heard ZERO recognition-bearing content — the 17:51 "
        "total recognition blackout"
    )
    assert "[RETURNING TABLE]" in recog[0][2]
    fact = game.recognition_aired()
    assert fact is not None and fact["source"] == "late_recognition_beat"
    # And exactly once — the antirepeat guarantee holds.
    assert len(recog) == 1
    assert game.flush_late_recognition_at_seam() is False


def test_fast_name_door_stamps_only_on_confirm(monkeypatch):
    """The 11:31 fast case, un-lied: the promotion lands BEFORE the organic
    reply's snapshot, so the reply carries the memory — but the fact stamps
    only when that turn's playout CONFIRMS, never at the tail."""
    game = _game()

    async def instant_lookup(sb, name):
        return [REAL_TABLE]

    _patch_persistence(monkeypatch, lookup=instant_lookup)
    assert asyncio.run(game.maybe_recognize_by_stated_name("Rami")) is True
    assert game.memory_block.startswith("[RETURNING TABLE]")
    # Tail: carried decision — watch armed, NO stamp, no late beat.
    assert game.recognition_aired() is None
    assert game._name_door_watch is not None
    assert _recog_dispatches(game) == []
    # While the watch is armed the beat is held, not fireable.
    assert game.late_recognition_blocked_reason() == (
        "recognition_carry_inflight"
    )
    # The organic reply snapshots WITH the block, then plays out in full.
    game.note_generation_snapshot()
    assert game._name_door_watch["inflight_seq"] is not None
    _confirm(game, "Rami! Eighteen games deep — welcome back.")
    fact = game.recognition_aired()
    assert fact is not None and fact["source"] == "name_door_organic"
    # Retired everywhere: no late beat, no seam resurrection.
    assert game.maybe_fire_late_recognition() is False
    assert game.flush_late_recognition_at_seam() is False
    assert _recog_dispatches(game) == []


def test_carried_organic_cut_re_arms_the_beat(monkeypatch):
    """A cut carrying turn never delivered the welcome-back — recognition
    is still OWED: the beat re-arms and the next seam delivers it."""
    game = _game()

    async def instant_lookup(sb, name):
        return [REAL_TABLE]

    _patch_persistence(monkeypatch, lookup=instant_lookup)
    asyncio.run(game.maybe_recognize_by_stated_name("Rami"))
    game.note_generation_snapshot()
    _confirm(game, "Rami! Welcome—", interrupted=True)
    assert game.recognition_aired() is None
    assert game._late_recognition_pending is True
    assert game._late_recognition_promotion_owed is True
    # The seam delivers the owed beat.
    assert game.flush_late_recognition_at_seam() is True
    assert len(_recog_dispatches(game)) == 1
    assert game.recognition_aired()["source"] == "late_recognition_beat"


def test_memory_blind_turn_finishing_first_re_arms_the_beat(monkeypatch):
    """A validated preemptive reply can air memory-blind even when the tail
    judged the promotion carried (its snapshot predates the door). The
    blind turn's confirm resolves the watch to UNCARRIED — the beat is
    owed, never stamped."""
    game = _game()

    async def instant_lookup(sb, name):
        return [REAL_TABLE]

    _patch_persistence(monkeypatch, lookup=instant_lookup)
    asyncio.run(game.maybe_recognize_by_stated_name("Rami"))
    assert game._name_door_watch is not None
    # No memory-carrying snapshot happens — the blind reply just finishes.
    _confirm(game, "Rami — glad you're here.")
    assert game.recognition_aired() is None
    assert game._name_door_watch is None
    assert game._late_recognition_pending is True
    assert game._late_recognition_promotion_owed is True


# -- CLASS 7: the promotion-owed beat delivers between questions --------------


def test_owed_beat_survives_game_start_and_fires_between_questions(
    monkeypatch,
):
    """The 17:51 second killer: game start committed BEFORE the slow
    promotion landed, and the CLASS 7 forbid retired the beat outright. A
    promotion-OWED beat now defers instead and airs ONE compact
    welcome-back at the between-questions seam."""
    game = _game()
    game.game_started = True
    game._game_start_committed = True
    game.sk.answer_window_open = True  # mid-question when the tail runs

    gate = asyncio.Event()

    async def delayed_lookup(sb, name):
        await gate.wait()
        return [REAL_TABLE]

    _patch_persistence(monkeypatch, lookup=delayed_lookup)

    async def scenario():
        task = asyncio.get_running_loop().create_task(
            game.maybe_recognize_by_stated_name("Rami")
        )
        await asyncio.sleep(0)
        game.note_generation_snapshot()  # the blind reply's snapshot
        gate.set()
        assert await task is True

    asyncio.run(scenario())
    # Uncarried, mid-window: deferred, NOT retired (the CLASS 7 exemption).
    assert _recog_dispatches(game) == []
    assert game._late_recognition_pending is True
    assert game._late_recognition_fired is False
    # The window closes — the between-questions seam flushes the beat.
    game.sk.answer_window_open = False
    assert game.flush_late_recognition_at_seam() is True
    (dispatch,) = _recog_dispatches(game)
    instr = dispatch[2]
    # ONE compact beat: no refresher offer, no prefs ask, no reprise.
    assert "compact" in instr
    assert "refresher" not in instr.lower() or "do NOT offer a refresher" in instr
    assert "hand straight back to the game" in instr
    assert game.recognition_aired()["source"] == "late_recognition_beat"
    # Once. The seam cannot double it.
    assert game.flush_late_recognition_at_seam() is False
    assert len(_recog_dispatches(game)) == 1


def test_non_owed_beat_still_retired_after_start():
    """The CLASS 7 forbid is UNCHANGED for every non-promotion lane: a
    plain armed beat at game start stays retired, never deferred."""
    game = _game()
    game.memory_block = "[RETURNING TABLE]\n18 games."
    game._game_start_committed = True
    game.game_started = True
    assert game.late_recognition_blocked_reason() == "game_start_committed"
    assert game.maybe_fire_late_recognition() is False
    assert game._late_recognition_fired is True
    assert game._late_recognition_pending is False
    assert _recog_dispatches(game) == []


# -- the game-start ride-along carries under the same confirm discipline ------


def test_game_start_ride_along_stamps_on_confirm():
    game = _game()
    game.memory_block = "[RETURNING TABLE]\n18 games."
    game._late_recognition_pending = True
    game._late_recognition_promotion_owed = True
    game.note_game_start_carries_recognition()
    assert game._late_recognition_pending is False
    assert game.recognition_aired() is None
    game.note_generation_snapshot()  # the kickoff composite's snapshot
    _confirm(game, "Welcome back — round one, here we go.")
    assert game.recognition_aired()["source"] == "game_start_ride_along"


def test_game_start_ride_along_cut_re_arms_the_owed_beat():
    game = _game()
    game.memory_block = "[RETURNING TABLE]\n18 games."
    game._game_start_committed = True
    game.game_started = True
    game.note_game_start_carries_recognition()
    game.note_generation_snapshot()
    _confirm(game, "Welcome ba—", interrupted=True)
    assert game.recognition_aired() is None
    assert game._late_recognition_pending is True
    assert game._late_recognition_promotion_owed is True
    assert game.flush_late_recognition_at_seam() is True
    assert game.recognition_aired()["source"] == "late_recognition_beat"


def test_start_game_wires_the_ride_along_latch():
    src = inspect.getsource(LilyGame.start_game)
    assert "note_game_start_carries_recognition" in src
    # Latched only when the dispatch was accepted — a refused composite
    # carries nothing.
    assert "if dispatched and ride_along_carries_recognition" in src


# -- the per-turn context marker ----------------------------------------------


def test_generation_snapshot_marker_rides_the_real_injection():
    """The marker increments exactly on per-generation (include_volatile)
    injections — the context snapshot the carried/uncarried comparison
    reads — and never on the persistent-context applications."""
    from livekit.agents.llm import ChatContext

    game = _game()
    game.sk.bind_speaker("S1", "Rami")
    game.session_started_at = 0.0
    game.availability_flags = None
    game.promoted_categories = []
    game.next_question = None
    game.eliminated = []
    game._pending_unbound_award = None
    game._delivery_stop_sticky = False
    agent = LilyAgent.__new__(LilyAgent)
    agent._game = game
    ctx = ChatContext.empty()
    agent._apply_context_blocks(ctx, now=1000.0)
    assert game._ctx_snapshot_seq == 0  # persistent-ctx application: no turn
    agent._apply_context_blocks(ctx, include_volatile=True)
    assert game._ctx_snapshot_seq == 1  # a real generation snapshot
    agent._apply_context_blocks(ctx, include_volatile=True)
    assert game._ctx_snapshot_seq == 2


# -- telemetry (S1): promotion events persist, no reconstruction --------------


def test_identity_promotion_events_carry_the_investigation_fields(
    monkeypatch,
):
    game = _game()

    async def instant_lookup(sb, name):
        return [REAL_TABLE]

    _patch_persistence(monkeypatch, lookup=instant_lookup)
    asyncio.run(game.maybe_recognize_by_stated_name("Rami"))
    events = game._identity_promotion_events
    assert events, "promotion left no telemetry — token-count archaeology"
    (ev,) = events  # deduped across the double tail (upgrade + promote)
    assert ev["source"] == "name_stated"
    assert ev["group_id"] == REAL_TABLE
    assert isinstance(ev["ts"], float)
    assert ev["short_circuit_decision"] == "carried_pending_confirm"
    assert ev["carried_memory"] is True
    # The confirm updates the verdict in place.
    game.note_generation_snapshot()
    _confirm(game, "Rami! Welcome back.")
    assert ev["short_circuit_decision"] == "organic_confirmed"
    assert "confirmed_at" in ev


def test_uncarried_promotion_records_the_blackout_shape(monkeypatch):
    game = _game()
    gate = asyncio.Event()

    async def delayed_lookup(sb, name):
        await gate.wait()
        return [REAL_TABLE]

    _patch_persistence(monkeypatch, lookup=delayed_lookup)

    async def scenario():
        task = asyncio.get_running_loop().create_task(
            game.maybe_recognize_by_stated_name("Rami")
        )
        await asyncio.sleep(0)
        game.note_generation_snapshot()
        gate.set()
        await task

    asyncio.run(scenario())
    (ev,) = game._identity_promotion_events
    assert ev["carried_memory"] is False
    assert ev["short_circuit_decision"] == "beat_armed"


def test_identity_promotions_ride_both_metadata_sites():
    """Same lane as question_timeline (lily_sessions.metadata via
    lily_session_end + the heartbeat) — no new table, no new writer path."""
    src = Path(
        inspect.getsourcefile(LilyGame)
    ).read_text(encoding="utf-8")
    assert src.count('"identity_promotions"') >= 2  # close + heartbeat


# -- rails: owed content is owed, not banned (deliverable 3) ------------------


def test_continuity_rail_names_owed_content():
    body = LILY_SYSTEM_PROMPT.split("<continuity>", 1)[1].split(
        "</continuity>", 1
    )[0]
    assert "OWED, not banned" in body
    assert "RE-AIRS only" in body
    assert "never a repeat" in body
    # Still one block, rails intact (the antirepeat pins also run).
    assert LILY_SYSTEM_PROMPT.count("CONTINUITY PROTOCOL") == 1


# -- the live fixture is committed with its hash (S13) ------------------------


def test_1751_fixture_committed_with_hash():
    data = FIXTURE.read_bytes()
    assert hashlib.sha256(data).hexdigest() == FIXTURE_SHA256
    text = data.decode("utf-8")
    assert "lily-FD3994-358c0ac8" in text
    assert "this is Rami" in text
    assert "21:52:51" in text  # the block's first appearance
    assert "name_door_organic" in text
