"""Say-gate WO agent-level tests (2026-07-14 consolidated directive).

Covers the offline-testable wiring around lily_say_gate:
  - gated_say dispatch dedupe (double greeting / rejoin-vs-greet)
  - the BUG-2 single-delivery contract (start_game host_tool source is
    silent; the lily_begin_round tool result carries the question payload)
  - need-to-know: neither state-block builder ever emits canonical_answer,
    acceptable_answers, or reveal_color into ambient context
  - burn protocol: leak -> LILY_BURN, discard, replacement pull; bank
    fetcher excludes non-'active' rows
  - callout gating: lily_log_clarify refuses before game_started with an
    LLM-readable recovery path (lily_note_fact stays deliberately ungated)

This file imports lily_agent (and therefore livekit) — same boundary note
as test_award_gate.py.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_audeering_consumers
import lily_say_gate
from lily_agent import LilyAgent, LilyGame
from lily_persistence import lily_burn_question, lily_fetch_bank_question
from lily_scorekeeper import LilyScorekeeper


class _FakeSession:
    def __init__(self) -> None:
        self.instructions: list[str] = []

    def generate_reply(self, instructions: str) -> None:
        self.instructions.append(instructions)


class _FakeAgentHandle:
    def set_preemptive_generation(self, enabled: bool) -> None:
        pass


def _make_game() -> LilyGame:
    """Minimal LilyGame via __new__ — the attributes gated_say /
    build_state_block / on_answer_leak touch."""
    game = LilyGame.__new__(LilyGame)
    game.session = _FakeSession()
    game.agent = _FakeAgentHandle()
    game._preemptive_paused = False
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game.sk = LilyScorekeeper("test-room")
    game.memory_block = ""
    game.reconnected = False
    game.game_started = False
    game.game_over = False
    game.armed_question = None
    game.next_question = None
    game.used_prompts = []
    game.supabase = None
    game._window_timer = None
    game._bed_handle = None
    game._adjudicating = False
    game._pending_reveal_event = None
    game._armed_speech_misses = 0
    game.ui_phase = "lobby"
    game._pending_unbound_award = None
    game.acoustic = lily_audeering_consumers.LilyAcousticState()
    game.prefs = {}
    game._prefs_offer_made = False
    return game


# -- gated dispatch: dedupe -------------------------------------------------------------

def test_gated_say_speaks_once_per_key():
    game = _make_game()
    assert game.gated_say("session_greet", "greet", "hello table", "on_enter")
    # The entrypoint's racing dispatch of the same act:
    assert not game.gated_say("session_greet", "greet", "hello again", "entrypoint")
    assert game.session.instructions == ["hello table"]


def test_rejoin_key_does_not_trip_greet_key():
    game = _make_game()
    assert game.gated_say("session_rejoin", "rejoin", "lost you a second", "on_enter")
    assert game.gated_say("session_greet", "greet", "hello table", "entrypoint")
    assert game.session.instructions == ["lost you a second", "hello table"]


def test_keyless_dispatch_always_speaks():
    game = _make_game()
    game.game_started = True  # P8: steal/lockout requires a live game
    assert game.gated_say(None, "steal_window", "five seconds!", "adjudicate")
    assert game.gated_say(None, "steal_window", "five seconds!", "adjudicate")
    assert len(game.session.instructions) == 2


def test_extra_keys_claimed_alongside_primary():
    # The final reveal claims q_N_reveal AND finale in one dispatch.
    game = _make_game()
    game.game_started = True  # P8: reveal requires a live game
    assert game.gated_say(
        "q_18_reveal", "reveal_finale", "and the winner is...",
        "adjudicate", extra_keys=("finale",),
    )
    assert game.say_registry.state("finale") == lily_say_gate.CLAIM_PENDING
    assert not game.gated_say("finale", "finale", "again?", "anywhere")


def test_on_enter_routes_by_reconnect_flag():
    agent = LilyAgent.__new__(LilyAgent)

    game = _make_game()
    agent._game = game
    asyncio.new_event_loop().run_until_complete(agent.on_enter())
    assert game.say_registry.state("session_greet") is not None
    assert game.say_registry.state("session_rejoin") is None

    game2 = _make_game()
    game2.reconnected = True
    agent._game = game2
    asyncio.new_event_loop().run_until_complete(agent.on_enter())
    assert game2.say_registry.state("session_rejoin") is not None
    assert game2.say_registry.state("session_greet") is None


def test_swallowed_dispatch_releases_and_redelivers():
    # Claim at dispatch -> playback failure releases -> retry redelivers.
    game = _make_game()
    game.game_started = True  # P8: reveal requires a live game
    game.gated_say("q_4_reveal", "reveal", "the answer is...", "adjudicate")
    released = game.say_registry.release_pending()  # tts_node empty path
    assert released == ["q_4_reveal"]
    assert game.gated_say("q_4_reveal", "reveal", "the answer is...", "retry")
    assert game.session.instructions.count("the answer is...") == 2


def test_suppressed_speech_cannot_confirm_or_open_question():
    game = _make_game()
    question = {
        "prompt": "Which colorful sea lies between Europe and Asia?",
        "canonical_answer": "Black Sea",
        "acceptable_answers": ["black sea"],
    }
    game.armed_question = question
    game.sk.start_question(question)
    game.ui_phase = "question"
    game.say_registry.claim("q_1_delivery", owner="speech-real")

    game.on_agent_speech_finished(
        question["prompt"],
        speech_id="speech-duplicate",
        suppressed=True,
    )

    assert game.say_registry.state("q_1_delivery") == lily_say_gate.CLAIM_PENDING
    assert game.sk.answer_window_open is False


def test_interrupted_speech_releases_only_its_claims():
    game = _make_game()
    game.say_registry.claim("session_greet", owner="speech-a")
    game.say_registry.claim("q_1_delivery", owner="speech-b")

    game.on_agent_speech_finished(
        "", speech_id="speech-a", interrupted=True,
    )

    assert game.say_registry.state("session_greet") is None
    assert game.say_registry.state("q_1_delivery") == lily_say_gate.CLAIM_PENDING


# -- BUG-2: one authoritative question delivery -------------------------------------------

def _start_game(game: LilyGame, source: str) -> None:
    async def _noop_async(*a, **k):
        return None

    game.resolve_group_identity = _noop_async
    game.publish_attributes = _noop_async
    game.start_prefetch = lambda: None
    game.arm_next_question = lambda: True
    game.fire_enrollment = lambda trigger: None
    game._enroll_started = False
    asyncio.new_event_loop().run_until_complete(game.start_game(source))


def test_start_game_host_tool_dispatches_no_speech():
    # The tool-call path: the tool RESULT carries the payload and the
    # post-tool turn is the sole deliverer — start_game must not race it
    # with an instructed reply.
    game = _make_game()
    _start_game(game, "host_tool")
    assert game.game_started is True
    assert game.session.instructions == []


def test_start_game_voice_path_still_speaks():
    game = _make_game()
    _start_game(game, "voice")
    assert game.game_started is True
    assert len(game.session.instructions) == 1
    assert "Kick off round one" in game.session.instructions[0]


def _call_tool(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_begin_round_result_carries_question_payload():
    agent = LilyAgent.__new__(LilyAgent)
    game = _make_game()
    agent._game = game

    async def _fake_start(source: str) -> None:
        game.game_started = True
        game.armed_question = {
            "prompt": "This strait separates Europe and Asia at Istanbul.",
            "category": "academic",
            "canonical_answer": "the Bosporus",
        }

    game.start_game = _fake_start
    result = _call_tool(LilyAgent.lily_begin_round.__wrapped__(agent, None))
    assert "sole deliverer" in result
    assert "This strait separates Europe and Asia at Istanbul." in result
    assert "word for word" in result
    # The tool path dispatched no racing instructed reply:
    assert game.session.instructions == []


def test_begin_round_result_honest_when_question_not_landed():
    agent = LilyAgent.__new__(LilyAgent)
    game = _make_game()
    agent._game = game

    async def _fake_start(source: str) -> None:
        game.game_started = True

    game.start_game = _fake_start
    result = _call_tool(LilyAgent.lily_begin_round.__wrapped__(agent, None))
    assert "hasn't landed" in result


# -- need-to-know: ambient context never carries the answer --------------------------------

_ANSWER_FIELDS = ("canonical_answer", "acceptable_answers", "reveal_color")


def test_scorekeeper_state_block_never_carries_answer():
    sk = LilyScorekeeper("test-room")
    sk.start_question({
        "prompt": "This strait separates Europe and Asia at Istanbul.",
        "canonical_answer": "the Bosporus",
        "acceptable_answers": ["bosporus", "the bosporus", "bosphorus"],
        "reveal_color": "Bosporus — the only strait splitting two continents.",
    })
    block = sk.build_state_block()
    assert "This strait separates Europe and Asia" in block
    assert "Bosporus" not in block
    assert "bosphorus" not in block
    for field in _ANSWER_FIELDS:
        assert field not in block


def test_game_state_block_next_question_is_need_to_know():
    game = _make_game()
    game.game_started = True
    game.armed_question = {
        "id": "q_0042",
        "prompt": "This strait separates Europe and Asia at Istanbul.",
        "category": "academic",
        "difficulty_tier": 2,
        "canonical_answer": "the Bosporus",
        "acceptable_answers": ["bosporus", "the bosporus"],
        "reveal_color": "Bosporus — continental split.",
    }
    block = game.build_state_block()
    assert "This strait separates Europe and Asia" in block
    assert "academic" in block
    assert "Bosporus" not in block
    for field in _ANSWER_FIELDS:
        assert field not in block


def test_game_state_block_choices_ride_along():
    game = _make_game()
    game.game_started = True
    game.armed_question = {
        "prompt": "Which of these is a strait?",
        "category": "academic",
        "choices": ["Bosporus", "Everest", "Sahara", "Danube"],
        "canonical_answer": "Bosporus",
    }
    block = game.build_state_block()
    # Choices are spoken content (the format), not answer material.
    assert "choices" in block
    assert "Everest" in block
    assert "canonical_answer" not in block


def test_prefetched_question_json_never_in_block():
    game = _make_game()
    game.game_started = True
    game.next_question = {
        "prompt": "hidden until armed",
        "canonical_answer": "secret",
    }
    block = game.build_state_block()
    assert "secret" not in block
    assert "hidden until armed" not in block
    assert "prefetched and ready" in block


# -- burn protocol ---------------------------------------------------------------------------

def test_on_answer_leak_burns_armed_and_prefetched():
    game = _make_game()
    game.game_started = True
    game.armed_question = {
        "id": "kb_7", "prompt": "armed prompt", "canonical_answer": "a",
    }
    game.next_question = {
        "id": "q_0001", "prompt": "prefetched prompt", "canonical_answer": "b",
    }
    game.sk.start_question(game.armed_question)
    game.sk.open_answer_window(now=100.0)
    armed_calls = []
    game.arm_next_question = lambda: armed_calls.append(True) or True

    async def _noop_publish(*a, **k):
        return None

    game.publish_metadata = _noop_publish
    game.publish_attributes = _noop_publish

    async def _run():
        game.on_answer_leak()
        await asyncio.sleep(0)  # drain the fire-and-forget publishes

    asyncio.new_event_loop().run_until_complete(_run())
    assert game.armed_question is None
    assert game.next_question is None
    assert game.sk.current_question is None
    assert game.sk.answer_window_open is False
    # Burned prompts never regenerate this session:
    assert "armed prompt" in game.used_prompts
    assert "prefetched prompt" in game.used_prompts
    # Replacement pull through the existing supply path:
    assert armed_calls == [True]


def test_on_answer_leak_noop_when_nothing_in_flight():
    game = _make_game()
    game.game_started = True
    called = []
    game.arm_next_question = lambda: called.append(True)
    game.on_answer_leak()
    assert called == []


class _FakeQuery:
    def __init__(self, rows) -> None:
        self._rows = rows
        self.updates: list[tuple] = []
        self._action = "select"
        self._filters = []
        self._limit = None

    def select(self, *_a, **_k):
        self._action = "select"
        self._filters = []
        self._limit = None
        return self

    def update(self, payload):
        self._action = "update"
        self._pending_update = payload
        return self

    def eq(self, col, val):
        if self._action == "update":
            self.updates.append((col, val, self._pending_update))
        elif col == "status":
            self._filters.append(
                lambda row: (row.get("status") or "active") == val
            )
        elif col == "adult":
            self._filters.append(lambda row: bool(row.get("adult")) == val)
        else:
            self._filters.append(lambda row: row.get(col) == val)
        return self

    def limit(self, value):
        self._limit = value
        return self

    def execute(self):
        rows = [
            row for row in self._rows
            if all(predicate(row) for predicate in self._filters)
        ]
        if self._limit is not None:
            rows = rows[:self._limit]

        class _R:
            data = rows
        return _R()


class _FakeSupabase:
    def __init__(self, rows) -> None:
        self.query = _FakeQuery(rows)

    def table(self, _name):
        return self.query


def test_bank_fetcher_excludes_burned_rows():
    rows = [
        {"id": 1, "question": "burned q", "canonical_answer": "x",
         "category": "academic", "difficulty_tier": 1, "status": "burned"},
        {"id": 2, "question": "retired q", "canonical_answer": "y",
         "category": "academic", "difficulty_tier": 1, "status": "retired"},
        {"id": 3, "question": "active q", "canonical_answer": "z",
         "category": "academic", "difficulty_tier": 1, "status": "active"},
    ]
    fake = _FakeSupabase(rows)
    q = asyncio.new_event_loop().run_until_complete(
        lily_fetch_bank_question(fake, "academic", 1, [])
    )
    assert q is not None
    assert q["prompt"] == "active q"
    assert fake.query._limit == 100


def test_bank_fetcher_randomizes_bounded_candidates(monkeypatch):
    rows = [
        {"id": 1, "question": "first", "canonical_answer": "a",
         "category": "academic", "difficulty_tier": 1, "status": "active"},
        {"id": 2, "question": "second", "canonical_answer": "b",
         "category": "academic", "difficulty_tier": 1, "status": "active"},
    ]
    monkeypatch.setattr("lily_persistence.random.choice", lambda values: values[-1])
    q = asyncio.new_event_loop().run_until_complete(
        lily_fetch_bank_question(_FakeSupabase(rows), "academic", 1, [])
    )
    assert q["prompt"] == "second"


def test_bank_fetcher_treats_missing_status_as_active():
    # Pre-009 schema tolerance: no status key reads as active.
    rows = [{"id": 4, "question": "legacy q", "canonical_answer": "w",
             "category": "academic", "difficulty_tier": 1}]
    q = asyncio.new_event_loop().run_until_complete(
        lily_fetch_bank_question(_FakeSupabase(rows), "academic", 1, [])
    )
    assert q is not None
    assert q["prompt"] == "legacy q"


def test_burn_question_marks_bank_row():
    fake = _FakeSupabase([])
    ok = asyncio.new_event_loop().run_until_complete(
        lily_burn_question(fake, "kb_7")
    )
    assert ok is True
    assert fake.query.updates == [("id", 7, {"status": "burned"})]


def test_burn_question_ignores_generated_ids():
    fake = _FakeSupabase([])
    ok = asyncio.new_event_loop().run_until_complete(
        lily_burn_question(fake, "q_0042")
    )
    assert ok is False
    assert fake.query.updates == []


# -- callout gating: clarify ---------------------------------------------------------------

def test_clarify_refused_before_game_started():
    agent = LilyAgent.__new__(LilyAgent)
    game = _make_game()
    agent._game = game
    game.sk.players["Sarah"] = {"speaker_label": "S1", "score": 0}
    events: list = []
    game.send_event_nowait = lambda t, p: events.append((t, p))
    msg = _call_tool(
        LilyAgent.lily_log_clarify.__wrapped__(agent, None, "Sarah")
    )
    # Fail-loud refusal naming the recovery path, not a silent no-op.
    assert "hasn't started" in msg
    assert "lily_begin_round" in msg
    assert events == []
    assert game.sk.answer_candidates == {}


def test_clarify_allowed_once_game_started():
    agent = LilyAgent.__new__(LilyAgent)
    game = _make_game()
    agent._game = game
    game.game_started = True
    game.pending_clarify = {}
    game.sk.players["Sarah"] = {"speaker_label": "S1", "score": 0}
    events: list = []
    game.send_event_nowait = lambda t, p: events.append((t, p))
    msg = _call_tool(
        LilyAgent.lily_log_clarify.__wrapped__(agent, None, "Sarah")
    )
    assert "Clarify logged for Sarah" in msg
    assert events == [("clarify", {"name": "Sarah"})]


def test_note_fact_stays_ungated():
    # Deliberate: lily_note_fact's primary habitat is the pre-game lobby —
    # a fact noted during phase confusion mutates nothing.
    agent = LilyAgent.__new__(LilyAgent)
    game = _make_game()
    agent._game = game
    game.record_group_fact = lambda name, fact: None
    game.sk.players["Dave"] = {"speaker_label": "S2", "score": 0,
                               "lobby_fact": None}
    msg = _call_tool(
        LilyAgent.lily_note_fact.__wrapped__(
            agent, None, "Dave", "owns 40 typewriters"
        )
    )
    assert "Noted" in msg


def test_bank_fetcher_passes_choices_and_image_prompt_through():
    # Seam TODO (e): adult-bank rows (migration 014) carry choices text[]
    # and image_prompt — both must survive into the served shape.
    rows = [{
        "id": 5, "question": "mc bank q", "canonical_answer": "b",
        "acceptable_answers": ["b"], "category": "academic",
        "difficulty_tier": 1, "status": "active",
        "choices": ["a", "b", "c", "d"],
        "image_prompt": "product photo of a thing",
    }]
    q = asyncio.new_event_loop().run_until_complete(
        lily_fetch_bank_question(_FakeSupabase(rows), "academic", 1, [])
    )
    assert q is not None
    assert q["choices"] == ["a", "b", "c", "d"]
    assert q["image_prompt"] == "product photo of a thing"


def test_bank_fetcher_omits_absent_choices_and_prompt():
    rows = [{"id": 6, "question": "plain q", "canonical_answer": "x",
             "category": "academic", "difficulty_tier": 1,
             "status": "active", "choices": None, "image_prompt": None}]
    q = asyncio.new_event_loop().run_until_complete(
        lily_fetch_bank_question(_FakeSupabase(rows), "academic", 1, [])
    )
    assert q is not None
    assert "choices" not in q
    assert "image_prompt" not in q
