"""WO-LILY-HOTFIX-010 V7 — throwaway groups & hash fragmentation.

Locks the resolve-before-mint + rebind ordering:

  1. A biometric (ECAPA centroid) match REBINDS the session's group even
     when the matched group has no memory to stage — the identity resolved,
     so a session that booted onto the room-name fallback must not keep
     group_id == session_id (the throwaway signature) once a KNOWN voice
     spoke.
  2. Three consecutive sessions whose one enrolled voice matches the same
     centroid all bind to ONE group — no fragmentation.
  3. resolve_group_identity does NOT mint a name-set-hash group while an
     enrolled-voice match is in flight (the race that fractures one table
     into several grp_ hashes). V1c IDENTITY — ONE AUTHORITY hardens this:
     even after the voice route reports no match, the name-set hash may only
     PROPOSE a quarantined candidate (staged like device metadata) that a
     voice must confirm — it never mints or switches group_id on a heard
     name alone. Only a biometric match or env_override creates or switches
     a group.

These fixtures FAIL on pre-V7 code (the match was dropped on empty staging)
and pin the V1c contract (the name-set hash is quarantined, never minted).

Embedder / probe / supabase are injected fakes (same shape as
test_hotfix010_v2_voice_id_latency.py).
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_config
import lily_memory
import lily_voice_embedder
from lily_agent import LilyGame
from lily_identity import _STRONG_GROUP_SOURCES
from lily_scorekeeper import LilyScorekeeper


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


TAG = "ecapa-192-v1"


def _enable(monkeypatch, *, embedding=None):
    monkeypatch.setattr(lily_config, "voice_identity_enabled", lambda: True)
    monkeypatch.setattr(lily_config, "voice_identity_model_tag", lambda: TAG)
    monkeypatch.setattr(
        lily_voice_embedder, "lily_voice_embedder_available", lambda: True
    )
    monkeypatch.setattr(
        lily_voice_embedder, "lily_voice_embedder_loaded", lambda: True
    )
    monkeypatch.setattr(
        lily_voice_embedder, "lily_voice_embedder_load_attempted", lambda: True
    )
    monkeypatch.setattr(
        lily_voice_embedder, "lily_extract_embedding",
        lambda samples, sample_rate=16000: embedding,
    )


def _game(session_id):
    """A game booted onto the room-name throwaway (group_id == session_id)."""
    g = LilyGame.__new__(LilyGame)
    g.sk = LilyScorekeeper(session_id)
    g.supabase = object()  # truthy; no path in these tests hits the wire
    g.group_id = session_id
    g.group_id_source = "room_name"
    g.device_identity_verified = False
    g.device_candidate_group_id = None
    g.forget_state = None
    g.game_started = False
    g._voice_identity_pcm = [0.1, 0.2, 0.3]
    g._voice_identity_attempted = False
    g._voice_identity_resolved = False
    g.stt = None
    # Record identity rebinds instead of hitting the DB, but mirror
    # upgrade_group_id's contract (no-op on equal, respects forget).
    g._upgrade_calls = []

    async def _record_upgrade(new_group_id, source):
        if not new_group_id or new_group_id == g.group_id:
            return
        if g.forget_state in ("executing", "done", "failed"):
            return
        g._upgrade_calls.append((new_group_id, source))
        g.group_id = new_group_id
        g.group_id_source = source

    g.upgrade_group_id = _record_upgrade
    return g


def _preload(g, pool):
    g._voice_identity_pool = pool
    g._voice_identity_pool_loaded = True


async def _stage_empty(gid, source):
    """The matched group has no memory to stage (thin prior table)."""
    return False


# -- 1. a biometric match rebinds off a throwaway even with empty memory ------


def test_voice_match_rebinds_throwaway_with_empty_memory(monkeypatch):
    _enable(monkeypatch, embedding=[0.99, 0.02, 0.0])
    g = _game("lily-ROOM1")
    assert g.group_id == g.sk.session_id  # throwaway precondition
    _preload(g, [{"group_id": "table_real", "centroid": [1.0, 0.0, 0.0],
                  "sample_count": 3}])
    g.stage_device_candidate = _stage_empty

    assert _run(g._voice_identity_match_at_start()) is True
    # The identity bound to the real group — NOT the throwaway.
    assert g.group_id == "table_real"
    assert g.group_id != g.sk.session_id
    assert g.group_id_source in _STRONG_GROUP_SOURCES
    assert g.device_identity_verified is True
    assert ("table_real", "voice_identity_match") in g._upgrade_calls


# -- 2. three sessions, one enrolled voice -> one group, no fragmentation -----


def test_three_sessions_same_voice_bind_one_group(monkeypatch):
    _enable(monkeypatch, embedding=[0.98, 0.03, 0.0])
    pool = [{"group_id": "table_real", "centroid": [1.0, 0.0, 0.0],
             "sample_count": 4}]
    resolved = set()
    for i in range(3):
        g = _game(f"lily-NIGHT{i}")
        _preload(g, pool)
        g.stage_device_candidate = _stage_empty
        assert _run(g._voice_identity_match_at_start()) is True
        resolved.add(g.group_id)
    # One stable table, not three room-name fragments.
    assert resolved == {"table_real"}


# -- 3a. resolve_group_identity defers the name-set mint while voice in flight -


def test_resolve_defers_name_set_mint_while_voice_in_flight(monkeypatch):
    _enable(monkeypatch)
    g = _game("lily-ROOM3")
    g.sk.players["amanda"] = {}
    g.sk.players["rami"] = {}
    # A voice match was scheduled and has NOT reported yet.
    g._voice_identity_attempted = True
    g._voice_identity_resolved = False

    _run(g.resolve_group_identity("game_start"))

    # No name-set hash minted ahead of the biometric — still the throwaway,
    # untouched, waiting for the voice route to report.
    assert g.group_id == "lily-ROOM3"
    assert g.group_id_source == "room_name"
    assert g._upgrade_calls == []


# -- 3b. name-set hash QUARANTINES once the voice route reports no match ------


def test_name_set_quarantines_after_voice_reports(monkeypatch):
    _enable(monkeypatch)
    g = _game("lily-ROOM4")
    g.sk.players["amanda"] = {}
    g.sk.players["rami"] = {}
    # The voice route already reported (resolved, no match).
    g._voice_identity_attempted = True
    g._voice_identity_resolved = True

    staged, requested = [], []

    async def _stage(candidate, source):
        staged.append((candidate, source))
        return True  # a table for this name set is on file

    g.stage_device_candidate = _stage
    g.request_device_verification = lambda trigger: requested.append(trigger)

    _run(g.resolve_group_identity("voice_no_match"))

    expected = lily_memory.lily_name_set_group_id(["amanda", "rami"])
    # PROPOSED, not minted: quarantined for a voice to confirm. group_id
    # is untouched — the anonymous session id stands until biometry lands.
    assert staged == [(expected, "name_set_hash")]
    assert requested == ["voice_no_match"]
    assert g._upgrade_calls == []
    assert g.group_id == "lily-ROOM4"
    assert g.group_id_source == "room_name"


# -- 3c. a voice NO-MATCH mid-game re-invokes the resolver to PROPOSE ---------


def test_voice_no_match_triggers_deferred_proposal(monkeypatch):
    # Non-matching centroid -> match is None. Game already started on a weak
    # group: the no-match branch re-invokes resolve_group_identity, which now
    # PROPOSES (quarantines) the name-set hash instead of minting it.
    _enable(monkeypatch, embedding=[1.0, 0.0, 0.0])
    g = _game("lily-ROOM5")
    g.sk.players["carly"] = {}
    g.sk.players["kali"] = {}
    g.game_started = True
    _preload(g, [{"group_id": "someone_else", "centroid": [0.0, 1.0, 0.0],
                  "sample_count": 5}])

    staged, requested = [], []

    async def _stage(candidate, source):
        staged.append((candidate, source))
        return True

    g.stage_device_candidate = _stage
    g.request_device_verification = lambda trigger: requested.append(trigger)

    assert _run(g._voice_identity_match_at_start()) is False
    expected = lily_memory.lily_name_set_group_id(["carly", "kali"])
    # The re-invoked resolver quarantined the name-set hash — never minted it.
    assert staged == [(expected, "name_set_hash")]
    assert requested == ["voice_no_match"]
    assert g.group_id == "lily-ROOM5"        # still anonymous, awaiting voice
    assert g.group_id_source == "room_name"
    assert g._upgrade_calls == []
