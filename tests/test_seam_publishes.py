"""
Seam-contract publishes — backend reconciliation TODO items (a)-(c):
  (a) question metadata carries the optional `category` key
  (b) answer_window JSON carries `steal: true` during a steal window
  (c) a `lock` {name} packet fires when an answer candidate is recorded

Fixture patterns follow tests/test_multiple_choice.py (metadata harness)
and tests/test_group_prefs.py (attribute + transcript-event harness).
"""

import asyncio
import json
import time

import lily_audeering_consumers
import lily_say_gate
from lily_agent import LilyGame
from lily_scorekeeper import LilyScorekeeper


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _FakeSession:
    def __init__(self) -> None:
        self.instructions: list[str] = []

    def generate_reply(self, instructions: str) -> None:
        self.instructions.append(instructions)


class _FakeAgentHandle:
    def set_preemptive_generation(self, enabled: bool) -> None:
        pass


class _FakeRoomAPI:
    def __init__(self) -> None:
        self.requests: list = []

    async def update_room_metadata(self, req) -> None:
        self.requests.append(req)


class _FakeLocalParticipant:
    def __init__(self) -> None:
        self.attributes: dict = {}

    async def set_attributes(self, attrs) -> None:
        self.attributes.update(attrs)


class _FakeCtx:
    def __init__(self) -> None:
        self.api = type("API", (), {"room": _FakeRoomAPI()})()
        self.room = type(
            "Room", (),
            {"name": "test-room", "local_participant": _FakeLocalParticipant()},
        )()


def _make_game() -> LilyGame:
    game = LilyGame.bare()
    game.ctx = _FakeCtx()
    game.session = _FakeSession()
    game.agent = _FakeAgentHandle()
    game._preemptive_paused = False
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game.sk = LilyScorekeeper("test-room")
    game.ui_phase = "lobby"
    game.memory_block = ""
    game.reconnected = False
    game.game_started = True
    game.game_over = False
    game.armed_question = None
    game.next_question = None
    game.eliminated = []
    game.used_prompts = []
    game.supabase = None
    game._window_timer = None
    game._bed_handle = None
    game._pending_unbound_award = None
    game._steal_window = False
    game._spec_judge = {}
    game._addressee_rows = {}
    game._user_turn_index = 0
    game.pending_clarify = {}
    game.forget_state = "idle"
    game.forget_requester = None
    game._forget_target_group = None
    game.acoustic = lily_audeering_consumers.LilyAcousticState()
    return game


def _published(game: LilyGame) -> dict:
    req = game.ctx.api.room.requests[-1]
    return json.loads(req.metadata)


# -- (a) category in question metadata ---------------------------------------

def test_publish_metadata_carries_category_when_known():
    game = _make_game()
    _run(game.publish_metadata("Which sea?", category="academic"))
    doc = _published(game)
    assert doc["category"] == "academic"


def test_publish_metadata_omits_category_when_unknown():
    game = _make_game()
    _run(game.publish_metadata("Which sea?"))
    doc = _published(game)
    assert "category" not in doc


# -- (b) steal flag in answer_window JSON -------------------------------------

def _window_attr(game: LilyGame) -> dict:
    return json.loads(
        game.ctx.room.local_participant.attributes["answer_window"]
    )


def test_answer_window_carries_steal_true_during_steal_window():
    game = _make_game()
    game.sk.open_answer_window(duration=5.0)
    game._steal_window = True
    _run(game.publish_attributes())
    window = _window_attr(game)
    assert window["open"] is True
    assert window["steal"] is True


def test_answer_window_has_no_steal_key_on_regular_window():
    game = _make_game()
    game.sk.open_answer_window(duration=15.0)
    game._steal_window = False
    _run(game.publish_attributes())
    window = _window_attr(game)
    assert window["open"] is True
    assert "steal" not in window


def test_answer_window_drops_stale_steal_flag_once_closed():
    game = _make_game()
    game._steal_window = True  # left over from the steal that just resolved
    _run(game.publish_attributes())
    window = _window_attr(game)
    assert window["open"] is False
    assert "steal" not in window


# -- (c) lock packet on candidate recording -----------------------------------

def test_lock_beat_fires_when_candidate_recorded():
    game = _make_game()
    game.sk.bind_speaker("S1", "Sarah")
    events: list = []
    game.send_event_nowait = lambda t, p: events.append((t, p))
    game.sk.current_question = {
        "prompt": "Which sea?",
        "acceptable_answers": ["bosporus"],
    }
    game.sk.open_answer_window(duration=30.0)

    async def scenario():
        now = time.time()
        result = game.sk.on_transcript_segment(
            text="the caspian", speaker_label="S1", is_final=True,
            now=now, segment_start_time=now,
        )
        assert result["candidate_recorded"]
        game.on_transcript_event(
            result, "the caspian", speaker_label="S1", segment_ts=now
        )
        # Any speculative-judge task spawned by the Tier-1 fallthrough is
        # not under test here — cancel so the loop closes clean.
        for task in asyncio.all_tasks():
            if task is not asyncio.current_task():
                task.cancel()

    asyncio.run(scenario())
    assert ("lock", {"name": "Sarah"}) in events


def test_no_lock_beat_without_open_window():
    game = _make_game()
    game.sk.bind_speaker("S1", "Sarah")
    events: list = []
    game.send_event_nowait = lambda t, p: events.append((t, p))
    game.sk.current_question = None

    async def scenario():
        now = time.time()
        result = game.sk.on_transcript_segment(
            text="the caspian", speaker_label="S1", is_final=True,
            now=now, segment_start_time=now,
        )
        assert not result["candidate_recorded"]
        game.on_transcript_event(
            result, "the caspian", speaker_label="S1", segment_ts=now
        )
        for task in asyncio.all_tasks():
            if task is not asyncio.current_task():
                task.cancel()

    asyncio.run(scenario())
    assert all(t != "lock" for t, _ in events)
