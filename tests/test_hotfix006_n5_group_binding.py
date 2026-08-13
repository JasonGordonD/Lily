"""WO-LILY-HOTFIX-006 N5 — the matcher's verdict outranks the name hash.

Live evidence, 2026-08-08, verified against production:

  lily-16A9AE  ->  grp_20427c697394bcf146c822f99f0387aa58f37a05
  lily-4FB3B2  ->  grp_f76e6116016497ba9245cd40f80a83dd14f8f50a

Same three humans — Rami, Rhonda, Chris — three minutes apart, two different
groups, because STT heard "Hi, I'm Miranda" in the second session. A changed
name set changes the hash, and the hash IS the identity.

Meanwhile the ECAPA voice matcher found the real table with TWELVE games on
file. Two identity systems ran side by side: one correct, one authoritative.

The structural fault this file pins: `_STRONG_GROUP_SOURCES` listed
`voiceprint_match` but NOT `voice_identity_match` — the source the ECAPA
matcher actually stages under. So the biometric verdict did not protect the
session, and a later name-hash resolution could overwrite it. The matcher
was outranked by a mishearing.

A second, quieter fault in the same path: resolve_group_identity's
voiceprint step loads stored prints BY PLAYER NAME
(`lily_load_voiceprints_by_players(supabase, names)`). When a name is
misheard, that lookup searches for the wrong person and finds nothing — so
ONE bad name defeats the voiceprint step and the hash step simultaneously,
and the fallback is guaranteed to mint a new group.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_agent
from lily_agent import LilyGame
from lily_identity import _STRONG_GROUP_SOURCES
from lily_scorekeeper import LilyScorekeeper


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _game(*, group_id, source):
    g = LilyGame.__new__(LilyGame)
    g.sk = LilyScorekeeper("hotfix006-n5")
    g.sk.players = {"Rami": None, "Miranda": None, "Chris": None}
    g.group_id = group_id
    g.group_id_source = source
    g.supabase = None
    g.stt = None
    g.forget_state = None
    g.device_candidate_group_id = None
    g.upgraded = []
    # V1c: the name-set hash may only PROPOSE a quarantined candidate. Record
    # both the stage attempt and any verification request; `_staged_present`
    # toggles whether the proposed group has a table on file to stage.
    g.staged = []
    g.verified_requests = []
    g._staged_present = False

    async def _upgrade(new_id, src):
        g.upgraded.append((new_id, src))

    async def _stage(candidate, source):
        g.staged.append((candidate, source))
        return g._staged_present

    def _request_verify(trigger):
        g.verified_requests.append(trigger)

    g.upgrade_group_id = _upgrade
    g.stage_device_candidate = _stage
    g.request_device_verification = _request_verify
    return g


# -- the defect ---------------------------------------------------------------


def test_the_ecapa_match_source_is_authoritative():
    """THE fixture. The matcher stages under 'voice_identity_match'. If that
    string is not a strong source, a mishearing can overwrite a biometric
    verdict that had twelve games of history behind it."""
    assert "voice_identity_match" in _STRONG_GROUP_SOURCES, (
        "the ECAPA matcher's verdict must outrank the name-set hash"
    )


def test_a_biometric_binding_is_never_rebound_by_a_misheard_name():
    """lily-4FB3B2 exactly: the table is already bound by voice, then STT
    mishears 'Rhonda' as 'Miranda'. The hash of the wrong name set must not
    move the session off the matched group."""
    g = _game(group_id="grp_the_real_table", source="voice_identity_match")
    _run(g.resolve_group_identity("game_start"))
    assert g.upgraded == [], (
        "a misheard name re-hashed the group out from under a biometric match"
    )
    assert g.group_id == "grp_the_real_table"


def test_the_env_override_still_outranks_everything():
    """PROTECTED — the operator pin is the highest authority."""
    g = _game(group_id="grp_pinned", source="env_override")
    _run(g.resolve_group_identity("game_start"))
    assert g.upgraded == []


def test_the_legacy_identifier_match_remains_authoritative():
    """PROTECTED — 'voiceprint_match' (the Speechmatics identifier path)
    was already strong and stays strong."""
    g = _game(group_id="grp_ident", source="voiceprint_match")
    _run(g.resolve_group_identity("game_start"))
    assert g.upgraded == []


def test_a_weak_binding_resolves_only_by_proposing_a_quarantined_candidate(
    monkeypatch,
):
    """V1c — a room-random or stale-hash session CAN still find its group,
    but a HEARD name set may only PROPOSE it. When the name-set hash matches
    a table on file, it stages a quarantined candidate that a voice must
    confirm — it never mints or switches group_id on the strength of a name
    alone. N5's structural half: the hash is no longer an identity."""
    g = _game(group_id="lily-ROOM-random", source="room_name")
    g._staged_present = True  # the hashed group has a table on file
    monkeypatch.setattr(
        lily_agent.lily_memory, "lily_name_set_group_id", lambda names: "grp_hashed"
    )
    _run(g.resolve_group_identity("game_start"))
    # Proposed, not minted: quarantined for voice, group_id untouched.
    assert g.upgraded == []
    assert g.staged == [("grp_hashed", "name_set_hash")]
    assert g.verified_requests == ["game_start"]
    assert g.group_id == "lily-ROOM-random"
    assert g.group_id_source == "room_name"


def test_a_misheard_name_cannot_mint_a_second_memory(monkeypatch):
    """THE V1c fixture — lily-4FB3B2 exactly, from the resolver side. STT
    mishears one name, so the heard name set (and its hash) is one this
    table has never produced before: there is NO table on file for it, and
    stage_device_candidate returns False for an empty group. The mishearing
    must therefore create NOTHING — no upgrade, no candidate, group_id stays
    the anonymous session id. A single misheard name is structurally
    incapable of minting a second grp_ memory."""
    g = _game(group_id="lily-anon-session", source="room_name")
    g._staged_present = False  # the misheard name set has no table on file
    minted = []

    def _hash(names):
        h = "grp_from_a_misheard_set"
        minted.append(h)
        return h

    monkeypatch.setattr(
        lily_agent.lily_memory, "lily_name_set_group_id", _hash
    )
    _run(g.resolve_group_identity("game_start"))
    # The hash was computed and the proposal was ATTEMPTED, but with no table
    # on file nothing was quarantined and nothing was minted.
    assert minted == ["grp_from_a_misheard_set"]
    assert g.staged == [("grp_from_a_misheard_set", "name_set_hash")]
    assert g.verified_requests == []          # nothing staged -> nothing to verify
    assert g.upgraded == []                    # never minted
    assert g.group_id == "lily-anon-session"   # still anonymous
    assert g.group_id_source == "room_name"


def test_post_forget_binding_is_still_suppressed():
    """PROTECTED — WO-LILY-FORGETME-001: after a deletion the session stays
    anonymous; re-resolving would rebuild the identity the table just
    deleted."""
    g = _game(group_id="lily-anon", source="room_name")
    g.forget_state = "done"
    _run(g.resolve_group_identity("game_start"))
    assert g.upgraded == []
