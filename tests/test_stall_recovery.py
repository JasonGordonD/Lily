"""
Live 2026-07-15 session regressions (the femur game):

1. Self-correction at adjudication — "the spine… no, the femur" must score
   on the femur; the earliest CORRECT attempt across the table wins, and a
   revision competes from its own (later) timestamp.
2. Solo-table steal trap — a steal window must not open when no unjudged
   player exists to steal (it could never record anything and burned five
   silent seconds before re-adjudicating an empty set).
3. Arm-failure honesty — when the reveal cannot arm a next question, the
   consumed question is cleared and a status note tells Lily to vamp, not
   re-ask ("for the official record…") or invent.
4. Idle watchdog — a live game that is armed-less, window-less, and not
   adjudicating always self-heals: loaded question arms, dead prefetch
   restarts.
"""

import asyncio
import json
import time

import lily_audeering_consumers
import lily_say_gate
from lily_agent import LilyGame
from lily_scorekeeper import LilyScorekeeper


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


class _FakeReasoning:
    """Supply stub: prefetch returns the queued question (or None)."""

    def __init__(self, question=None):
        self.question = question
        self.prefetch_calls = 0

    async def prefetch_question(self, sk, **kw):
        self.prefetch_calls += 1
        return dict(self.question) if self.question else None

    async def prefetch_picture_question(self, supabase, **kw):
        return None


QUESTION = {
    "id": "q_1001",
    "prompt": "Often measuring over eighteen inches in adults, what is the "
              "longest human bone?",
    "canonical_answer": "the femur",
    "acceptable_answers": ["femur", "the femur", "thigh bone", "thighbone"],
    "reveal_color": "",
    "category": "academic",
    "difficulty_tier": 1,
}


def _make_game() -> LilyGame:
    game = LilyGame.__new__(LilyGame)
    game.ctx = _FakeCtx()
    game.session = _FakeSession()
    game.agent = _FakeAgentHandle()
    game._preemptive_paused = False
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game.sk = LilyScorekeeper("test-room")
    game.rounds_total = 3
    game.ui_phase = "answering"
    game.memory_block = ""
    game.reconnected = False
    game.game_started = True
    game.game_over = False
    game.armed_question = None
    game.next_question = None
    game.eliminated = []
    game.used_prompts = []
    game.asked_history = []
    game.group_id = "grp_test"
    game.promoted_categories = set()
    game.prewager_standings = None
    game.highlights = []
    game.supabase = None
    game.reasoning = _FakeReasoning()
    game.background_audio = None
    game._bed_handle = None
    game._prefetch_task = None
    game._window_timer = None
    game._watchdog_task = None
    game._prefetch_stall_ticks = 0
    game._steal_window = False
    game._adjudicating = False
    game._judged_keys = set()
    game._spec_judge = {}
    game._addressee_rows = {}
    game._pending_reveal_event = None
    game._pending_unbound_award = None
    game._user_turn_index = 0
    game._armed_speech_misses = 0
    game.pending_clarify = {}
    game.forget_state = "idle"
    game.forget_requester = None
    game._forget_target_group = None
    game.acoustic = lily_audeering_consumers.LilyAcousticState()
    return game


def _arm(game: LilyGame, question: dict) -> None:
    game.armed_question = dict(question)
    game.sk.start_question(game.armed_question)
    game.sk.round = 1
    game.sk.set_phase("round")


def _final(game: LilyGame, text: str, label: str, at: float) -> dict:
    return game.sk.on_transcript_segment(
        text=text, speaker_label=label, is_final=True,
        now=at, segment_start_time=at,
    )


def _reveal_doc(game: LilyGame) -> dict:
    assert game.ctx.api.room.requests, "no metadata was published"
    return json.loads(game.ctx.api.room.requests[-1].metadata)


# ─── 1. self-correction scores ───────────────────────────────────────────────

def test_revision_scores_at_adjudication():
    game = _make_game()
    game.sk.bind_speaker("S1", "Rami")
    _arm(game, QUESTION)
    now = time.time()
    game.sk.open_answer_window(duration=30.0, now=now)
    _final(game, "Ah! The spine.", "S1", now + 17)
    _final(game, "The femur.", "S1", now + 33 - 30 + 25)  # still in window

    asyncio.run(game.adjudicate(steal_allowed=True))

    assert game.sk.players["Rami"]["score"] > 0
    doc = _reveal_doc(game)
    assert doc["reveal"]["winner"] == "Rami"
    assert doc["reveal"]["correct"] is True


def test_earliest_correct_across_table_wins():
    # A answers wrong then revises to correct; B said it correct in between.
    # The revision competes from its own timestamp — B's earlier correct wins.
    game = _make_game()
    game.sk.bind_speaker("S1", "Sarah")
    game.sk.bind_speaker("S2", "Dave")
    _arm(game, QUESTION)
    now = time.time()
    game.sk.open_answer_window(duration=30.0, now=now)
    _final(game, "the spine", "S1", now + 2)   # Sarah wrong
    _final(game, "the femur", "S2", now + 5)   # Dave correct
    _final(game, "no — the femur!", "S1", now + 8)  # Sarah revises, too late

    asyncio.run(game.adjudicate(steal_allowed=True))

    assert game.sk.players["Dave"]["score"] > 0
    assert game.sk.players["Sarah"]["score"] == 0
    assert _reveal_doc(game)["reveal"]["winner"] == "Dave"


# ─── 2. steal only with a possible stealer ───────────────────────────────────

def test_solo_missed_question_skips_steal_and_reveals():
    game = _make_game()
    game.sk.bind_speaker("S1", "Rami")
    _arm(game, QUESTION)
    now = time.time()
    game.sk.open_answer_window(duration=30.0, now=now)
    _final(game, "Ah! The spine.", "S1", now + 17)

    async def scenario():
        await game.adjudicate(steal_allowed=True)
        for task in asyncio.all_tasks():
            if task is not asyncio.current_task():
                task.cancel()

    asyncio.run(scenario())

    # No steal window opened — straight to the reveal.
    assert game.sk.answer_window_open is False
    assert game._steal_window is False
    doc = _reveal_doc(game)
    assert doc["reveal"]["correct"] is False
    assert doc["reveal"]["answer"] == "the femur"


def test_multiplayer_missed_opens_steal_for_unjudged_player():
    game = _make_game()
    game.sk.bind_speaker("S1", "Sarah")
    game.sk.bind_speaker("S2", "Dave")  # Dave never answers — he can steal
    _arm(game, QUESTION)
    now = time.time()
    game.sk.open_answer_window(duration=30.0, now=now)
    _final(game, "the spine", "S1", now + 2)

    async def scenario():
        await game.adjudicate(steal_allowed=True)
        opened = game.sk.answer_window_open, game._steal_window
        for task in asyncio.all_tasks():
            if task is not asyncio.current_task():
                task.cancel()
        return opened

    window_open, steal = asyncio.run(scenario())
    assert window_open is True
    assert steal is True


# ─── 3. arm-failure honesty ──────────────────────────────────────────────────

def test_arm_failure_sets_honest_note_and_clears_question():
    game = _make_game()
    game.sk.bind_speaker("S1", "Rami")
    _arm(game, QUESTION)
    now = time.time()
    game.sk.open_answer_window(duration=30.0, now=now)
    _final(game, "the spine", "S1", now + 2)
    game.next_question = None  # nothing prefetched — arm must fail

    async def scenario():
        await game.adjudicate(steal_allowed=False)
        for task in asyncio.all_tasks():
            if task is not asyncio.current_task():
                task.cancel()

    asyncio.run(scenario())

    assert game.armed_question is None
    assert game.sk.current_question is None
    assert any("still being written" in n for n in game.sk.status_notes)


# ─── 4. idle watchdog ────────────────────────────────────────────────────────

def _run_watchdog_ticks(game: LilyGame, ticks: int = 3) -> None:
    game.WATCHDOG_INTERVAL_SECONDS = 0.01

    async def scenario():
        task = asyncio.ensure_future(game._idle_watchdog())
        await asyncio.sleep(0.01 * (ticks + 2))
        game.game_over = True
        task.cancel()
        for t in asyncio.all_tasks():
            if t is not asyncio.current_task():
                t.cancel()

    asyncio.run(scenario())


def test_watchdog_arms_loaded_question_when_idle():
    game = _make_game()
    game.sk.bind_speaker("S1", "Rami")
    game.next_question = dict(QUESTION)

    _run_watchdog_ticks(game)

    assert game.armed_question is not None
    assert game.sk.question_number == 1
    # The nudge went out so Lily actually asks it.
    assert any("state block" in i for i in game.session.instructions)


def test_watchdog_restarts_dead_prefetch():
    game = _make_game()
    game.sk.bind_speaker("S1", "Rami")
    game.reasoning = _FakeReasoning(question=None)  # supply returns nothing
    assert game._prefetch_task is None

    _run_watchdog_ticks(game)

    # The watchdog relaunched the supply line at least once.
    assert game.reasoning.prefetch_calls >= 1


def test_watchdog_quiet_while_question_armed():
    game = _make_game()
    game.sk.bind_speaker("S1", "Rami")
    _arm(game, QUESTION)
    game.next_question = dict(QUESTION)
    before = game.sk.question_number

    _run_watchdog_ticks(game)

    # Armed game: the watchdog must not double-arm or nudge.
    assert game.sk.question_number == before
    assert game.session.instructions == []
