"""HOTFIX-DOUBLE-WELCOME-001 — the second welcome-back that aired behind
the first.

Live 2026-09-06, two sessions, same shape:
  lily-C47CD4-690cc8fd  14:24:12Z "Rami. There you are ... sixteen questions
                        deep ..." then 14:24:25Z "Took me a second — welcome
                        back, Rami. Last time you walked out with the win ..."
  lily-38C562-2eb12a08  17:31:31Z "[soft] Rami — there you are. Last time you
                        cleaned up the board, twenty-two questions deep."
                        then 17:31:47Z "[soft] Took me a second — welcome
                        back, Rami ... twenty-two deep ..."

The durable rows (lily_llm_usage / lily_sessions.metadata) for 38C562: the
player's "Hi, this is Rami" final at 17:31:04.9; identity_promotions
{voiceprint_match, late_beat_path} at 17:31:07.331; the organic reply
speech_5e44e95458fc RE-generated at ~17:31:09.5 (+231 prompt tokens — the
block) while the late beat speech_62c4466666fa was dispatched at ~17:31:07.6.
The organic confirmed at 17:31:31 and stamped recognition_aired; the beat's
SpeechHandle was already queued in the framework's sequential scheduler and
aired anyway, 17:31:31→47. Retiring the beat was bookkeeping only
(flight=None, carriers={}); nothing ever reached its handle.

Pinned here, on the real objects (LilyGame.bare + the real gated_say /
instructed_reply / on_agent_speech_finished, handles minted through
note_speech_handle by test_airgate_001's _HandleSession):
  1. the live shape — organic confirms first → the queued beat's handle is
     interrupted before playout; ONE recognition line; receipt + airgate row;
  2. the reverse order — the beat confirms first → the queued organic
     carrier's handle is retired (its text was generated WITH the block, so
     it cannot air without the memory line; the beat already answered the
     name);
  3. the carrier-cut path re-arms exactly once when nothing else is queued,
     and does NOT re-arm over a beat that is still queued (which used to
     dispatch a second beat at the next seam behind the first);
  4. no second dispatch when a carrier registered inside the promotion
     window (the rekey/reload awaits between "block visible" and the tail's
     maybe_fire_late_recognition) — the carrier confirms or re-arms.
"""

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_airgate_001 import _game as _airgate_game  # noqa: E402

BLOCK = (
    "[RETURNING TABLE]\nThis table has played with you 22 time(s) before."
)


def _lobby_game():
    """The 17:31 lobby: greet aired, memory block promoted, nothing owed
    yet. Real gated_say → instructed_reply → session.generate_reply mints a
    SpeechHandle-shaped fake that note_speech_handle tracks."""
    game = _airgate_game()
    game.game_started = False
    game.ui_phase = "lobby"
    game.memory_block = BLOCK
    game.memory_total_games = 22
    game.prefs = {}
    game._recognition_aired = None
    game._late_recognition_fired = False
    game._late_recognition_pending = False
    game.say_registry.claim("session_greet", owner="greet-1")
    game._resume_preemptive = lambda: None
    game._pending_reveal_event = None
    game._state_note = None
    game.dispatched = []
    real_gated_say = game.gated_say

    def _spy(key, act, instructions, source, *a, **kw):
        ok = real_gated_say(key, act, instructions, source, *a, **kw)
        game.dispatched.append((act, ok))
        return ok

    game.gated_say = _spy
    return game


def _organic_reply(game):
    """The framework's own reply to the human's utterance: a handle minted
    by the session (speech_created → note_speech_handle), no dispatched
    act — exactly what speech_5e44e95458fc was."""
    return game.session._handle()


def _beats(game):
    return [act for act, ok in game.dispatched if act == "late_recognition" and ok]


def _on_air(game, speech_id):
    game.note_playout_started(speech_id)
    game.note_recognition_playout_started(speech_id)


def _confirm(game, speech_id, text="...", **kw):
    game.on_agent_speech_finished(text, speech_id=speech_id, **kw)


def _receipts(caplog, marker):
    return [r.getMessage() for r in caplog.records if marker in r.getMessage()]


def _duplicate_rows(game):
    return [
        e for e in game.airgate_events() if e["reason"] == "recognition_duplicate"
    ]


# ---------------------------------------------------------------------------
# (1) the live shape
# ---------------------------------------------------------------------------


def test_1_live_shape_organic_confirms_first_retires_the_queued_beat(caplog):
    caplog.set_level(logging.INFO)
    game = _lobby_game()
    organic = _organic_reply(game)               # 17:31:05.8 reply in flight
    assert game.maybe_fire_late_recognition() is True   # 17:31:07.3 late_beat_path
    beat_id = game._late_recognition_flight["speech_id"]
    assert beat_id and beat_id != organic.id
    assert beat_id in game._speech_handles
    game.note_generation_snapshot(speech_id=beat_id)      # 17:31:07.6 WITH block
    game.note_generation_snapshot(speech_id=organic.id)   # 17:31:09.5 WITH block
    assert organic.id in game._recognition_carriers
    assert beat_id in game._recognition_carriers
    # 17:31:31 — the organic reply plays out in full and confirms.
    _on_air(game, organic.id)
    _confirm(
        game, organic.id,
        "Rami — there you are. Last time you cleaned up the board, "
        "twenty-two questions deep.",
    )
    fact = game.recognition_aired()
    assert fact is not None and fact["source"] == "memory_turn_organic"
    # The queued beat's handle was interrupted (force) BEFORE playout.
    beat = game._speech_handles[beat_id]
    assert beat.interrupts == [True]
    assert beat_id not in game._playout_started_ids
    assert beat_id in game._suppressed_speech_ids
    receipts = _receipts(caplog, "RECOGNITION_DUPLICATE_RETIRED")
    assert len(receipts) == 1
    assert f"speech_id={beat_id}" in receipts[0]
    assert "act=late_recognition" in receipts[0]
    assert "reason=aired_by=memory_turn_organic" in receipts[0]
    rows = _duplicate_rows(game)
    assert len(rows) == 1
    assert rows[0]["stage"] == "cancel"
    assert rows[0]["act"] == "late_recognition"
    assert rows[0]["speech_id"] == beat_id
    assert rows[0]["detail"]["aired_by"] == "memory_turn_organic"
    # The framework reports the interrupted beat: nothing stamps twice,
    # nothing re-arms, no seam resurrection.
    _confirm(game, beat_id, "", interrupted=True, suppressed=True)
    assert game.recognition_aired() is fact
    assert game._late_recognition_pending is False
    assert game._late_recognition_flight is None
    assert game.flush_late_recognition_at_seam() is False
    assert _beats(game) == ["late_recognition"]   # dispatched once, never aired


# ---------------------------------------------------------------------------
# (2) the reverse order
# ---------------------------------------------------------------------------


def test_2_reverse_order_beat_confirms_first_retires_the_queued_organic(caplog):
    """The beat is ahead in the queue (the organic was a preemptive the
    framework invalidated at turn commit and regenerated behind the beat,
    now WITH the block). The beat confirms → the organic carrier's handle
    is retired. It cannot air WITHOUT the memory line: its text was
    generated with the block in context (that is precisely what registered
    it as a carrier) and the codebase performs no text surgery on a queued
    handle; the beat already answered the name."""
    caplog.set_level(logging.INFO)
    game = _lobby_game()
    assert game.maybe_fire_late_recognition() is True
    beat_id = game._late_recognition_flight["speech_id"]
    game.note_generation_snapshot(speech_id=beat_id)
    organic = _organic_reply(game)
    game.note_generation_snapshot(speech_id=organic.id)
    assert organic.id in game._recognition_carriers
    _on_air(game, beat_id)
    _confirm(game, beat_id, "Took me a second — welcome back, Rami. Twenty-two deep.")
    fact = game.recognition_aired()
    assert fact is not None and fact["source"] == "late_recognition_beat"
    assert organic.interrupts == [True]
    assert organic.id not in game._playout_started_ids
    receipts = _receipts(caplog, "RECOGNITION_DUPLICATE_RETIRED")
    assert len(receipts) == 1
    assert f"speech_id={organic.id}" in receipts[0]
    assert "reason=aired_by=late_recognition_beat" in receipts[0]
    rows = _duplicate_rows(game)
    assert len(rows) == 1
    assert rows[0]["stage"] == "cancel"
    assert rows[0]["speech_id"] == organic.id
    assert rows[0]["act"] is None                      # organic: no dispatched act
    assert rows[0]["detail"]["carrier_source"] == "memory_turn_organic"
    _confirm(game, organic.id, "", interrupted=True, suppressed=True)
    assert game.recognition_aired() is fact
    assert game._late_recognition_pending is False
    assert game.flush_late_recognition_at_seam() is False
    assert _beats(game) == ["late_recognition"]


# ---------------------------------------------------------------------------
# (3) the carrier-cut path
# ---------------------------------------------------------------------------


def test_3a_cut_carrier_with_nothing_queued_re_arms_exactly_once(caplog):
    """No regression on the owed path: a name-door carrier cut on the air,
    no beat queued → ONE re-arm receipt, ONE beat at the seam, and no
    duplicate retirement (nothing else was in flight)."""
    caplog.set_level(logging.INFO)
    game = _lobby_game()
    game._name_door_entry_seq = game._ctx_snapshot_seq
    game._name_door_promotion_tail("name_stated", "grp_rami")
    assert game._name_door_watch is not None
    organic = _organic_reply(game)
    game.note_generation_snapshot(speech_id=organic.id)
    _on_air(game, organic.id)
    _confirm(game, organic.id, "Rami! Welcome—", interrupted=True)
    assert game.recognition_aired() is None
    assert game._late_recognition_pending is True
    assert game._late_recognition_promotion_owed is True
    assert len(_receipts(caplog, "RECOGNITION_CARRY_UNRESOLVED")) == 1
    assert _receipts(caplog, "RECOGNITION_DUPLICATE_RETIRED") == []
    assert _duplicate_rows(game) == []
    # The seam delivers the owed beat exactly once; it stamps on ITS confirm.
    assert game.flush_late_recognition_at_seam() is True
    assert _beats(game) == ["late_recognition"]
    beat_id = game._late_recognition_flight["speech_id"]
    game.note_generation_snapshot(speech_id=beat_id)
    _on_air(game, beat_id)
    _confirm(game, beat_id, "Took me a second — welcome back, Rami.")
    assert game.recognition_aired()["source"] == "late_recognition_beat"
    assert len(_receipts(caplog, "RECOGNITION_CARRY_UNRESOLVED")) == 1
    assert game.flush_late_recognition_at_seam() is False
    assert _beats(game) == ["late_recognition"]


def test_3b_cut_carrier_with_the_beat_queued_lets_the_beat_stamp_no_second_beat(caplog):
    """The live shape with the organic CUT instead of confirmed: the beat
    is still queued behind it with a live handle. Pre-fix the older-never-
    aired prune dropped the beat's carrier, the re-arm cleared its flight,
    the beat then aired UNSTAMPED and the next seam dispatched a SECOND
    beat — two "Took me a second"s."""
    caplog.set_level(logging.INFO)
    game = _lobby_game()
    organic = _organic_reply(game)
    assert game.maybe_fire_late_recognition() is True
    beat_id = game._late_recognition_flight["speech_id"]
    game.note_generation_snapshot(speech_id=beat_id)
    game.note_generation_snapshot(speech_id=organic.id)
    _on_air(game, organic.id)
    _confirm(game, organic.id, "Rami — there you—", interrupted=True)
    assert game.recognition_aired() is None
    # Nothing owed: the queued beat is still the one flight.
    flight = game._late_recognition_flight
    assert flight is not None and flight["speech_id"] == beat_id
    assert beat_id in game._recognition_carriers
    assert _receipts(caplog, "RECOGNITION_CARRY_UNRESOLVED") == []
    assert game.flush_late_recognition_at_seam() is False
    assert _beats(game) == ["late_recognition"]
    # The beat airs and stamps; the seam has nothing left to deliver.
    _on_air(game, beat_id)
    _confirm(game, beat_id, "Took me a second — welcome back, Rami.")
    assert game.recognition_aired()["source"] == "late_recognition_beat"
    assert game.flush_late_recognition_at_seam() is False
    assert _beats(game) == ["late_recognition"]
    assert _receipts(caplog, "RECOGNITION_DUPLICATE_RETIRED") == []


# ---------------------------------------------------------------------------
# (4) no second dispatch when a carrier is in flight
# ---------------------------------------------------------------------------


def _staged_candidate(game):
    game.memory_block = ""
    game.memory_total_games = 0
    game.device_candidate_group_id = "grp_rami"
    game.device_candidate_source = "device_id"
    game._device_candidate_memory = {
        "total_games": 22, "player_names": ["Rami"], "sessions": [],
    }
    game._device_candidate_memory_block = ""
    game._device_candidate_prefs = {}
    game._device_candidate_voiceprints = []
    game.memory_settled = asyncio.Event()


def _promote_with_organic_snapshot_in_window(game, monkeypatch, organic):
    """The late_beat_path promotion: the block becomes visible, then the
    rekey/reload awaits run — and the organic reply's generation snapshots
    WITH the just-visible block inside that window (17:31:09.5)."""
    seen = {}

    async def _upgrade(candidate, label):
        seen["block_visible"] = game.memory_block.startswith("[RETURNING TABLE]")
        game.note_generation_snapshot(speech_id=organic.id)

    monkeypatch.setattr(game, "upgrade_group_id", _upgrade)
    asyncio.run(game._promote_device_candidate("voiceprint_match"))
    assert seen["block_visible"] is True
    events = game._identity_promotion_events or []
    assert events and events[-1]["short_circuit_decision"] == "late_beat_path"


def test_4a_no_beat_dispatch_over_a_carrier_registered_in_the_promotion_window(monkeypatch):
    game = _lobby_game()
    _staged_candidate(game)
    organic = _organic_reply(game)
    _promote_with_organic_snapshot_in_window(game, monkeypatch, organic)
    # The organic generation is the registered carrier; the tail deferred.
    assert organic.id in game._recognition_carriers
    assert _beats(game) == []
    assert game.late_recognition_blocked_reason() == "recognition_carry_inflight"
    # It confirms → stamped under it; nothing else ever dispatches.
    _on_air(game, organic.id)
    _confirm(game, organic.id, "Rami — there you are. Twenty-two deep.")
    assert game.recognition_aired()["source"] == "memory_turn_organic"
    assert game.maybe_fire_late_recognition() is False
    assert game.flush_late_recognition_at_seam() is False
    assert _beats(game) == []


def test_4b_carrier_registered_in_the_window_and_cut_re_arms_once(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    game = _lobby_game()
    _staged_candidate(game)
    organic = _organic_reply(game)
    _promote_with_organic_snapshot_in_window(game, monkeypatch, organic)
    assert _beats(game) == []
    _on_air(game, organic.id)
    _confirm(game, organic.id, "Rami — there—", interrupted=True)
    assert game.recognition_aired() is None
    assert len(_receipts(caplog, "RECOGNITION_CARRY_UNRESOLVED")) == 1
    assert game.flush_late_recognition_at_seam() is True
    assert _beats(game) == ["late_recognition"]
    beat_id = game._late_recognition_flight["speech_id"]
    game.note_generation_snapshot(speech_id=beat_id)
    _on_air(game, beat_id)
    _confirm(game, beat_id, "Took me a second — welcome back, Rami.")
    assert game.recognition_aired()["source"] == "late_recognition_beat"
    assert game.flush_late_recognition_at_seam() is False
    assert _beats(game) == ["late_recognition"]
