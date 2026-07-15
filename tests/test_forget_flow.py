"""WO-LILY-FORGETME-001 agent-level flow tests: the deterministic spoken
forget flow (request -> one confirmation -> yes/no resolution), the
two-step lily_forget_group tool contract, in-session teardown (fresh
anonymous binding, STT known_speakers clear, memory-block removal,
resolve/upgrade suppression), the memory_forgotten packet, and the
greeting/disclosure interlock.

This file imports lily_agent (and therefore livekit) — same boundary note
as test_award_gate.py / test_say_gate_dispatch.py.
"""

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_audeering_consumers
import lily_say_gate
from lily_agent import LilyAgent, LilyGame
from lily_scorekeeper import LilyScorekeeper


class _FakeSession:
    def __init__(self) -> None:
        self.instructions: list[str] = []

    def generate_reply(self, instructions: str) -> None:
        self.instructions.append(instructions)


class _FakeAgentHandle:
    def set_preemptive_generation(self, enabled: bool) -> None:
        pass


class _FakeLocalParticipant:
    def __init__(self) -> None:
        self.packets: list[tuple[dict, str]] = []

    async def publish_data(self, data, reliable=True, topic=None):
        self.packets.append((json.loads(data.decode("utf-8")), topic))


class _FakeRoom:
    def __init__(self) -> None:
        self.local_participant = _FakeLocalParticipant()


class _FakeCtx:
    def __init__(self) -> None:
        self.room = _FakeRoom()


class _FakeSTTOptions:
    def __init__(self) -> None:
        self.known_speakers = [{"label": "Sarah", "speaker_identifiers": ["id1"]}]


class _FakeSTT:
    def __init__(self) -> None:
        self._stt_options = _FakeSTTOptions()


def _make_game() -> LilyGame:
    game = LilyGame.__new__(LilyGame)
    game.ctx = _FakeCtx()
    game.session = _FakeSession()
    game.agent = _FakeAgentHandle()
    game._preemptive_paused = False
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game.sk = LilyScorekeeper("test-room")
    game.sk.bind_speaker("S1", "Sarah")
    game.stt = _FakeSTT()
    game.memory_block = "[RETURNING TABLE]\nrematch energy."
    game.memory_total_games = 1
    game._memory_disclosure_offered = False
    game.reconnected = False
    # game_started=True keeps the lobby auto-start safety net (which needs
    # the full prefetch machinery) out of these transcript-driven tests;
    # the forget flow itself is deliberately UNGATED by game_started (the
    # tool test flips this back to False to prove it).
    game.game_started = True
    game.game_over = False
    game.armed_question = None
    game.next_question = None
    game.used_prompts = []
    game.supabase = None
    game._window_timer = None
    game._bed_handle = None
    game._pending_unbound_award = None
    game.acoustic = lily_audeering_consumers.LilyAcousticState()
    game.group_id = "grp_device_uuid"
    game.group_id_source = "participant_metadata"
    game.highlights = []
    game.pending_clarify = {}
    game._addressee_rows = {}
    game._user_turn_index = 0
    game.forget_state = "idle"
    game.forget_requester = None
    game._forget_target_group = None
    return game


def _segment(game: LilyGame, text: str, label: str = "S1"):
    now = time.time()
    result = game.sk.on_transcript_segment(
        text=text, speaker_label=label, now=now, segment_start_time=now
    )
    game.on_transcript_event(result, text, speaker_label=label, segment_ts=now)
    return result


async def _drive(game: LilyGame, *texts: str, label: str = "S1"):
    for text in texts:
        _segment(game, text, label=label)
        await asyncio.sleep(0.01)


# ---------------------------------------------------------------------------
# Spoken flow: request -> one confirmation -> yes -> cascade + teardown
# ---------------------------------------------------------------------------

def test_spoken_forget_flow_yes_deletes_and_tears_down():
    game = _make_game()

    async def _run():
        await _drive(game, "Lily, forget me")
        assert game.forget_state == "pending_confirm"
        assert game.forget_requester == "Sarah"
        assert len(game.session.instructions) == 1
        confirm = game.session.instructions[0]
        # The confirmation names the scope, plainly
        for word in ("voices", "games", "facts", "gone for good"):
            assert word in confirm
        assert "keeps going" in confirm

        await _drive(game, "yes, do it")
        assert game.forget_state == "done"

    asyncio.run(_run())
    # In-session teardown: fresh ANONYMOUS binding, not the device id
    assert game.group_id != "grp_device_uuid"
    assert game.group_id.startswith("anon_")
    assert game.group_id_source == "post_forget_anonymous"
    # STT enrolled speakers cleared (1.6.4: option-level clear — no live
    # de-enrollment API exists on the plugin)
    assert game.stt._stt_options.known_speakers == []
    # [RETURNING TABLE] injection stops immediately
    assert game.memory_block == ""
    # memory_forgotten packet emitted AFTER the cascade, with BOTH
    # discriminators spelled memory_forgotten
    packets = game.ctx.room.local_participant.packets
    assert len(packets) == 1
    packet, topic = packets[0]
    assert topic == "lily.events"
    assert packet["type"] == "memory_forgotten"
    assert packet["event"] == "memory_forgotten"
    # Completion line: one warm line, zero mourning, game continues
    done_line = game.session.instructions[-1]
    assert "warm" in done_line
    assert "zero mourning" in done_line


def test_spoken_forget_flow_no_drops_it():
    game = _make_game()

    async def _run():
        await _drive(game, "forget everything you know about us")
        assert game.forget_state == "pending_confirm"
        await _drive(game, "no, never mind")
        assert game.forget_state == "declined"

    asyncio.run(_run())
    # Nothing deleted, nothing emitted, identity untouched
    assert game.group_id == "grp_device_uuid"
    assert game.memory_block != ""
    assert game.ctx.room.local_participant.packets == []
    drop_line = game.session.instructions[-1]
    assert "Never re-raise" in drop_line


def test_ambiguous_reply_stays_pending_and_never_deletes():
    game = _make_game()

    async def _run():
        await _drive(game, "Lily, forget me")
        await _drive(game, "how much do you even remember?")
        assert game.forget_state == "pending_confirm"
        # A repeated request never asks twice
        await _drive(game, "forget me")
        assert len(game.session.instructions) == 1

    asyncio.run(_run())
    assert game.group_id == "grp_device_uuid"


def test_only_the_requester_resolves_the_confirmation():
    game = _make_game()
    game.sk.bind_speaker("S2", "Dave")

    async def _run():
        await _drive(game, "forget me", label="S1")
        # Dave's "yes" (table chatter) must not fire Sarah's deletion
        await _drive(game, "yes", label="S2")
        assert game.forget_state == "pending_confirm"
        await _drive(game, "yes", label="S1")
        assert game.forget_state == "done"

    asyncio.run(_run())


def test_second_request_after_done_answers_plainly_without_redeleting():
    game = _make_game()

    async def _run():
        await _drive(game, "forget me", "yes")
        assert game.forget_state == "done"
        packets_before = len(game.ctx.room.local_participant.packets)
        await _drive(game, "forget me again please")
        assert game.forget_state == "done"
        line = game.session.instructions[-1]
        assert "already" in line
        assert len(game.ctx.room.local_participant.packets) == packets_before

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Post-deletion identity suppression (never rebuild the deleted group)
# ---------------------------------------------------------------------------

def test_resolve_and_upgrade_suppressed_after_forget():
    game = _make_game()
    game.group_id_source = "room_name"  # weak source — normally upgradable

    async def _run():
        await _drive(game, "forget us", "yes")
        fresh = game.group_id
        # Late device metadata may not re-key to the deleted identity
        await game.upgrade_group_id("grp_device_uuid", "participant_metadata_late")
        assert game.group_id == fresh
        # game-start re-resolution may not re-run the name-set hash
        await game.resolve_group_identity(trigger="game_start")
        assert game.group_id == fresh
        assert game.group_id_source == "post_forget_anonymous"

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# The lily_forget_group tool — two-step, ungated by game_started
# ---------------------------------------------------------------------------

def _call_tool(coro):
    return asyncio.run(coro)


def test_tool_confirm_false_refuses_and_arms_the_deterministic_parse():
    game = _make_game()
    agent = LilyAgent.__new__(LilyAgent)
    agent._game = game
    result = _call_tool(
        LilyAgent.lily_forget_group.__wrapped__(agent, None, False)
    )
    assert "NOT deleted" in result
    assert "spoken yes" in result
    assert "confirm=true" in result
    # Two-step: the refusal armed the pending-confirm state so the spoken
    # yes is parsed in code even if the follow-up tool call never comes.
    assert game.forget_state == "pending_confirm"
    assert game.forget_requester is None  # any voice settles a tool-armed flow
    # Nothing was deleted
    assert game.group_id == "grp_device_uuid"
    assert game.ctx.room.local_participant.packets == []


def test_tool_confirm_true_deletes_even_before_game_started():
    # UNGATED by game_started (tool-gating principle: deletion neither
    # mutates game outcomes nor emits game events).
    game = _make_game()
    game.game_started = False
    agent = LilyAgent.__new__(LilyAgent)
    agent._game = game
    result = _call_tool(
        LilyAgent.lily_forget_group.__wrapped__(agent, None, True)
    )
    assert game.forget_state == "done"
    assert "gone for good" in result
    assert "warm" in result
    assert game.group_id.startswith("anon_")
    packet, _topic = game.ctx.room.local_participant.packets[0]
    assert packet["type"] == "memory_forgotten"


def test_tool_second_confirm_true_reports_already_done():
    game = _make_game()
    agent = LilyAgent.__new__(LilyAgent)
    agent._game = game
    _call_tool(LilyAgent.lily_forget_group.__wrapped__(agent, None, True))
    result = _call_tool(
        LilyAgent.lily_forget_group.__wrapped__(agent, None, True)
    )
    assert "Already forgotten" in result
    assert len(game.ctx.room.local_participant.packets) == 1


# ---------------------------------------------------------------------------
# lily_explain_memory tool — read-only, ungated
# ---------------------------------------------------------------------------

def test_explain_memory_tool_offline_shape():
    game = _make_game()  # supabase None -> honest can't-check shape
    agent = LilyAgent.__new__(LilyAgent)
    agent._game = game
    result = _call_tool(
        LilyAgent.lily_explain_memory.__wrapped__(agent, None)
    )
    assert "Could not read" in result
    assert "device" in result  # recognition source still reported


# ---------------------------------------------------------------------------
# Greeting interlock: dynamic greeting + Task 4 disclosure
# ---------------------------------------------------------------------------

def test_returning_greet_carries_disclosure_once_on_first_rematch():
    game = _make_game()
    game.memory_total_games = 1  # first rematch -> disclose
    greet = game.greeting_instructions()
    # Composition order: the one-breath self-intro is ALWAYS part one —
    # never skipped for a returning table (the live 'welcome back
    # everyone' with no intro is the exact bug this pins).
    assert "PART ONE, always" in greet
    assert "Hi, I'm Lily" in greet
    assert greet.index("I'm Lily") < greet.index("welcome back")
    # Memory KNOWS -> act on it, never ask; per-player composition with
    # the mixed-table nuance
    assert "Do NOT ask if it's their first time" in greet
    assert "welcome back, all of you" in greet
    assert "hello to the new faces" in greet
    # Refresher (not walkthrough) for returners, from the single block
    assert "refresher" in greet
    assert "WHAT THE TABLE CAN ASK FOR" in greet
    # Task 4 disclosure folded into the same beat
    assert "forget" in greet
    assert "disclosure" in greet
    # Once per session — a second build never repeats the clause
    assert "disclosure" not in game.greeting_instructions()


def test_returning_greet_disclosure_respects_frequency_cap():
    game = _make_game()
    game.memory_total_games = 3  # not 1, not a multiple of 5 -> no clause
    greet = game.greeting_instructions()
    assert "disclosure" not in greet
    assert "refresher" in greet  # the refresher offer still happens
    assert "Hi, I'm Lily" in greet  # the intro survives regardless


def test_cold_greet_asks_first_time_and_never_discloses():
    game = _make_game()
    game.memory_block = ""
    game.memory_total_games = 0
    greet = game.greeting_instructions()
    # Intro first, always — then memory gives no answer, so she asks
    assert "Hi, I'm Lily" in greet
    assert "memory gives no answer" in greet
    assert "first time" in greet
    assert "WHAT THE TABLE CAN ASK FOR" in greet
    assert "at most once tonight" in greet
    assert "disclosure" not in greet
    # Neutral-history rule preserved
    assert "Never claim you remember them" in greet
