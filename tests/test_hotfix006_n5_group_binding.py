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
from lily_agent import _STRONG_GROUP_SOURCES, LilyGame
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

    async def _upgrade(new_id, src):
        g.upgraded.append((new_id, src))

    g.upgrade_group_id = _upgrade
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


def test_a_weak_binding_is_still_allowed_to_resolve(monkeypatch):
    """PROTECTED — the whole point of the resolver is that a room-random or
    stale-hash session CAN still find its group. N5 narrows what may
    override a biometric verdict; it must not freeze weak bindings."""
    g = _game(group_id="lily-ROOM-random", source="room_name")
    monkeypatch.setattr(
        lily_agent.lily_memory, "lily_name_set_group_id", lambda names: "grp_hashed"
    )
    _run(g.resolve_group_identity("game_start"))
    assert g.upgraded == [("grp_hashed", "name_set_hash")]


def test_post_forget_binding_is_still_suppressed():
    """PROTECTED — WO-LILY-FORGETME-001: after a deletion the session stays
    anonymous; re-resolving would rebuild the identity the table just
    deleted."""
    g = _game(group_id="lily-anon", source="room_name")
    g.forget_state = "done"
    _run(g.resolve_group_identity("game_start"))
    assert g.upgraded == []
