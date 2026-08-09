"""A returner who SAYS their name must not wait on a biometric.

Live evidence, session `lily-2C489B-a61fb6d9` (2026-08-08). Recognition
landed at 22:49:15 — 3 minutes 31 seconds and SIXTEEN player turns after
the greeting at 22:45:44. What he said in between, verbatim from
lily_transcripts:

  22:46:04  "My name is Rami."
  22:46:15  "I have met you a million times."
  22:47:36  "You still don't remember me."
  22:47:51  "I just told you my name. You forgot my name already."

`grp_0b07f989` holds his table's file — four wins, seven voice samples.
The only door open was the ECAPA matcher, and the matcher was behind a
cold model load that the image was supposed to have baked away but wrote
to /tmp, which the runtime mounts as tmpfs. So the one piece of evidence
the player handed over at twenty-two seconds — his name — was never used.

The door added here is deliberately weak, and these fixtures pin the
weakness as hard as the capability: a name is not a voice, and the day it
starts outranking one is the day N5 comes back.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_agent
import lily_persistence
from lily_agent import _STRONG_GROUP_SOURCES
from test_recognition_variety import _make_game

REAL_TABLE = "grp_0b07f989673dcf11e62da96343a39fd4006c1405"


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _game(*, groups_for_name, group_id="lily-2C489B", source="room_name",
          memory_block="", forget_state=None, verified=False):
    g = _make_game()
    g.supabase = object()
    g.group_id = group_id
    g.group_id_source = source
    g.memory_block = memory_block
    g.forget_state = forget_state
    g.device_identity_verified = verified
    g.device_candidate_group_id = None
    g.upgrades = []
    g.staged = []

    async def _fake_lookup(sb, name):
        return list(groups_for_name)

    lily_persistence.lily_groups_for_player_name = _fake_lookup

    async def _stage(candidate, src):
        g.staged.append((candidate, src))
        g.device_candidate_group_id = candidate
        g._device_candidate_memory = {
            "total_games": 4, "player_names": ["Rami"],
        }
        g._device_candidate_memory_block = "[RETURNING TABLE] 4 game(s)"
        g._device_candidate_prefs = {}
        g._device_candidate_voiceprints = []
        return True

    async def _upgrade(new_id, src):
        g.upgrades.append((new_id, src))
        g.group_id = new_id
        g.group_id_source = src

    g.stage_device_candidate = _stage
    g.upgrade_group_id = _upgrade
    g.maybe_fire_late_recognition = lambda: False
    g.memory_settled = asyncio.Event()
    return g


# -- the defect ---------------------------------------------------------------


def test_a_stated_name_opens_the_door_without_the_biometric():
    """THE fixture. He said "My name is Rami" at 22:46:04 and his table's
    file was one indexed query away the whole time."""
    g = _game(groups_for_name=[REAL_TABLE])
    assert _run(g.maybe_recognize_by_stated_name("Rami")) is True
    assert g.group_id == REAL_TABLE
    assert g.memory_block.startswith("[RETURNING TABLE]")


def test_the_matcher_still_runs_after_a_name_opened_the_door():
    """THE guard that keeps N5 dead. Promoting on a NAME must not set
    device_identity_verified — that flag short-circuits
    _voice_identity_match_at_start, so latching it here would let one
    misheard name permanently lock out the voice that has twelve games of
    history behind it. Exactly the inversion N5 exists to prevent."""
    g = _game(groups_for_name=[REAL_TABLE])
    _run(g.maybe_recognize_by_stated_name("Rami"))
    assert g.device_identity_verified is False, (
        "a stated name closed the door on the biometric"
    )


def test_a_name_binding_is_weak_so_a_voice_can_overwrite_it():
    """'name_stated' must stay OUT of _STRONG_GROUP_SOURCES: strong sources
    make resolve_group_identity return early, which would freeze the
    session onto whatever the name matched."""
    assert "name_stated" not in _STRONG_GROUP_SOURCES


def test_the_ledger_records_a_name_match_as_a_name_match():
    """Promotion coerced every non-strong trigger to "voiceprint_match",
    which would file this as biometric evidence that never existed — in the
    one table an operator reads to debug recognition."""
    g = _game(groups_for_name=[REAL_TABLE])
    _run(g.maybe_recognize_by_stated_name("Rami"))
    assert g.upgrades == [(REAL_TABLE, "name_stated")]


def test_two_tables_with_the_same_name_resolve_to_neither():
    """Being slow is recoverable; merging two families' histories is not."""
    g = _game(groups_for_name=[REAL_TABLE, "grp_some_other_rami"])
    assert _run(g.maybe_recognize_by_stated_name("Rami")) is False
    assert g.upgrades == []


def test_a_name_nobody_remembers_changes_nothing():
    g = _game(groups_for_name=[])
    assert _run(g.maybe_recognize_by_stated_name("Chris")) is False
    assert g.upgrades == []


# -- protected ----------------------------------------------------------------


def test_post_forget_the_door_stays_shut():
    """PROTECTED — WO-LILY-FORGETME-001: after a deletion the session stays
    anonymous. A name lookup would rebuild the identity just deleted."""
    g = _game(groups_for_name=[REAL_TABLE], forget_state="done")
    assert _run(g.maybe_recognize_by_stated_name("Rami")) is False
    assert g.upgrades == []


def test_a_biometric_binding_is_never_reopened_by_a_name():
    """PROTECTED — N5, stated the other way round. The voice already
    decided; a name may not move the session off it."""
    g = _game(
        groups_for_name=["grp_somewhere_else"],
        group_id=REAL_TABLE,
        source="voice_identity_match",
    )
    assert _run(g.maybe_recognize_by_stated_name("Miranda")) is False
    assert g.upgrades == []


def test_an_already_recognised_table_is_left_alone():
    """PROTECTED — memory already loaded means this door has nothing to
    add, and re-promoting would fire a second recognition beat."""
    g = _game(
        groups_for_name=[REAL_TABLE],
        memory_block="[RETURNING TABLE] 12 game(s)",
    )
    assert _run(g.maybe_recognize_by_stated_name("Rami")) is False


def test_a_pending_device_candidate_is_not_fought_over():
    """PROTECTED — the device path is mid-verification; two resolvers
    racing for the same session is how the group id became a throwaway."""
    g = _game(groups_for_name=[REAL_TABLE])
    g.device_candidate_group_id = "grp_being_verified"
    assert _run(g.maybe_recognize_by_stated_name("Rami")) is False


def test_no_supabase_no_lookup():
    g = _game(groups_for_name=[REAL_TABLE])
    g.supabase = None
    assert _run(g.maybe_recognize_by_stated_name("Rami")) is False


def test_an_empty_name_does_nothing():
    g = _game(groups_for_name=[REAL_TABLE])
    assert _run(g.maybe_recognize_by_stated_name("   ")) is False


# -- the wall that made the biometric slow ------------------------------------


def test_the_ecapa_model_is_not_loaded_from_tmp():
    """/tmp is routinely mounted as tmpfs by the container runtime, which
    SHADOWS the copy the Dockerfile baked at build time and silently
    restores the cold download to the critical path — directly in front of
    recognition."""
    import lily_voice_embedder
    assert not lily_voice_embedder.ECAPA_SAVEDIR.startswith("/tmp"), (
        "the baked model can be shadowed by a tmpfs /tmp mount"
    )


def test_loading_the_baked_model_never_reaches_the_network():
    """`from_hparams` resolves the revision against Hugging Face even when
    every file is cached, so a cold or throttled network turns "load a
    local model" into an unbounded wait. The runtime must forbid it."""
    src = (
        Path(__file__).resolve().parent.parent / "lily_voice_embedder.py"
    ).read_text(encoding="utf-8")
    assert 'HF_HUB_OFFLINE' in src and 'LILY_ECAPA_ALLOW_FETCH' in src, (
        "the runtime model load can still make network calls"
    )


def test_the_image_proves_the_offline_load_at_build_time():
    """A build that can only load ECAPA while online has not actually baked
    it, and would fail open into a multi-minute session stall."""
    dockerfile = (
        Path(__file__).resolve().parent.parent / "Dockerfile"
    ).read_text(encoding="utf-8")
    assert "HF_HUB_OFFLINE=1" in dockerfile


# -- the device+name amnesia (2026-08-09) -------------------------------------


def test_a_staged_candidate_plus_its_own_stated_name_promotes():
    """THE 2026-08-09 fixture. The staged-candidate guard used to slam the
    name door shut UNCONDITIONALLY — so on a deploy without the ECAPA deps
    the same-device returner who said his own name stayed quarantined
    forever. Device history + a name ON that history is strictly stronger
    evidence than the name alone (which opens the door by itself), so it
    promotes exactly as weakly: verified=False, voice still outranks."""
    g = _game(groups_for_name=[REAL_TABLE])
    g.device_candidate_group_id = REAL_TABLE
    g._device_candidate_memory = {"total_games": 4, "player_names": ["Rami"]}
    promoted = []

    async def _promote(trigger, *, verified=True):
        promoted.append((trigger, verified))

    g._promote_device_candidate = _promote
    assert _run(g.maybe_recognize_by_stated_name("Rami")) is True
    assert promoted == [("device_plus_name", False)]


def test_a_stranger_name_on_a_staged_device_stays_quarantined():
    """A shared device is not an identity: a name NOT on the staged file
    keeps the quarantine — nothing promotes, nothing is fought over."""
    g = _game(groups_for_name=[REAL_TABLE])
    g.device_candidate_group_id = REAL_TABLE
    g._device_candidate_memory = {"total_games": 4, "player_names": ["Rami"]}
    promoted = []

    async def _promote(trigger, *, verified=True):
        promoted.append((trigger, verified))

    g._promote_device_candidate = _promote
    assert _run(g.maybe_recognize_by_stated_name("Chris")) is False
    assert promoted == []
