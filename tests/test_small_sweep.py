"""Small sweep (WO-LILY-DESYNC-HONESTY-001 Sub-agent G).

  G1 — preemptive generation is OFF while the game is live (during rounds
       nearly every user turn honestly changes the state block, so 1.6.4's
       equivalence check discarded the speculative run anyway — 10
       warnings/session and a dead LLM call each) and ON in the lobby and
       wrapup, where the quiet context lets the check pass. The P2
       playout-completion resume must respect the game-live latch.

  G2 — draw idempotency: a prefetch registers what it DRAWS the moment the
       question lands, not at serving/arm — the live q_0492 double-draw
       ran through the arm-registration window. A duplicate draw is
       discarded at a final gate whatever the supply source.

  G3 — spoken-output hygiene wiring: LilyAgent.tts_node itself (the ONLY
       synthesis path — all speech routes through generate_reply) must
       run the say-gate markdown/emoji strip on the text that reaches the
       default TTS node, so no speech path can bypass it.

This file imports lily_agent (and therefore livekit) — same boundary note
as test_award_gate.py.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_say_gate
from lily_agent import Agent, LilyAgent, LilyGame
from lily_scorekeeper import LilyScorekeeper


class _FakeSession:
    def __init__(self) -> None:
        self.instructions: list[str] = []

    def generate_reply(self, instructions: str) -> None:
        self.instructions.append(instructions)


class _RecordingAgentHandle:
    def __init__(self) -> None:
        self.calls: list[bool] = []

    def set_preemptive_generation(self, enabled: bool) -> None:
        self.calls.append(enabled)


def _make_game() -> LilyGame:
    game = LilyGame.__new__(LilyGame)
    game.session = _FakeSession()
    game.agent = _RecordingAgentHandle()
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
    game.asked_history = []
    game.promoted_categories = []
    game.supabase = None
    game.group_id = "grp_test"
    game._drawn_ids = set()
    game._drawn_hashes = set()
    game._prefetch_task = None
    game._window_timer = None
    game._bed_handle = None
    game._adjudicating = False
    game.rounds_total = 3
    game.prefs = {}
    game._prefs_offer_made = False
    game.publish_attributes_nowait = lambda: None
    return game


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# G1 — preemptive generation off while the game is live
# ---------------------------------------------------------------------------

def _start_game(game: LilyGame, source: str = "voice") -> None:
    async def _noop_async(*a, **k):
        return None

    game.resolve_group_identity = _noop_async
    game.publish_attributes = _noop_async
    game.start_prefetch = lambda: None
    game.arm_next_question = lambda: True
    game.start_idle_watchdog = lambda: None
    game.apply_prefs_at_game_start = lambda: None
    game.fire_enrollment = lambda trigger: None
    game._enroll_started = False
    _run(game.start_game(source))


def test_start_game_disables_preemptive_generation():
    game = _make_game()
    _start_game(game)
    assert game.game_started is True
    # set_game_live_preemptive(True) fired: the first recorded call is the
    # game-live OFF (False = disabled).
    assert game.agent.calls and game.agent.calls[0] is False
    assert True not in game.agent.calls


def test_playout_resume_respects_game_live_latch():
    # The P2 pause/resume pair must not re-enable preemptive mid-game.
    game = _make_game()
    _start_game(game)
    game.agent.calls.clear()
    game._preemptive_paused = True
    game._resume_preemptive()
    assert game._preemptive_paused is False
    assert game.agent.calls == []  # no True while live


def test_playout_resume_reenables_in_lobby():
    game = _make_game()  # game_started False — lobby
    game._preemptive_paused = True
    game._resume_preemptive()
    assert game.agent.calls == [True]


def test_finish_game_reenables_preemptive_generation():
    game = _make_game()
    _start_game(game)
    game.agent.calls.clear()
    game.prewager_standings = None
    game.finale_sent = False
    game.highlights = []
    game.ui_phase = "question"

    async def _noop_async(*a, **k):
        return None

    game.send_event = _noop_async
    game.publish_attributes = _noop_async
    _run(game.finish_game())
    assert game.game_over is True
    assert game.agent.calls and game.agent.calls[0] is True


def test_set_game_live_preemptive_tolerates_missing_agent():
    game = _make_game()
    game.agent = None
    game.set_game_live_preemptive(True)  # must not raise


# ---------------------------------------------------------------------------
# G2 — prefetch draw idempotency
# ---------------------------------------------------------------------------

BANK_Q = {
    "id": "kb_0492",
    "prompt": "This organelle is the powerhouse of the cell.",
    "canonical_answer": "the mitochondria",
    "category": "academic",
}


class _StubReasoning:
    """Supply stub that keeps serving the SAME question — the live class:
    the second draw happened before the first serving registered."""

    def __init__(self, question) -> None:
        self._q = question
        self.calls = 0

    async def prefetch_question(self, sk, category=None, difficulty_tier=None,
                                avoid_questions=None, from_bank=None,
                                multiple_choice=False, avoid_answers=None):
        self.calls += 1
        return dict(self._q)


def _run_prefetch(game: LilyGame) -> None:
    async def _go():
        game.start_prefetch()
        await game._prefetch_task

    _run(_go())


def test_register_draw_claims_once_per_question():
    game = _make_game()
    assert game._register_draw(dict(BANK_Q)) is True
    assert game._register_draw(dict(BANK_Q)) is False           # same id
    assert game._register_draw({"id": "kb_9999",                # same text
                                "prompt": BANK_Q["prompt"]}) is False
    assert game._register_draw({"id": "kb_1", "prompt": "other"}) is True


def test_register_draw_lazy_inits_for_harness_built_games():
    game = LilyGame.__new__(LilyGame)
    assert game._register_draw(dict(BANK_Q)) is True
    assert game._register_draw(dict(BANK_Q)) is False


def test_duplicate_draw_is_discarded_not_prefetched_twice():
    # THE q_0492 regression: draw one lands; the question is consumed
    # WITHOUT the serving registering anywhere (the live window); the
    # supply line then serves the identical question again — the second
    # draw must be discarded, never staged as next_question.
    game = _make_game()
    game.reasoning = _StubReasoning(BANK_Q)

    _run_prefetch(game)
    assert game.next_question is not None
    assert game.next_question["id"] == "kb_0492"

    # Consumed before any registration (the hole the fix closes):
    game.next_question = None

    _run_prefetch(game)
    assert game.reasoning.calls == 2
    assert game.next_question is None  # duplicate discarded at the gate


def test_distinct_draws_flow_normally():
    game = _make_game()
    game.reasoning = _StubReasoning(BANK_Q)
    _run_prefetch(game)
    game.next_question = None
    game.reasoning._q = {
        "id": "kb_0777",
        "prompt": "This planet is known as the red planet.",
        "canonical_answer": "Mars",
        "category": "academic",
    }
    _run_prefetch(game)
    assert game.next_question is not None
    assert game.next_question["id"] == "kb_0777"


# ---------------------------------------------------------------------------
# G3 — tts_node wiring: the strip fires at TTS input on the real path
# ---------------------------------------------------------------------------

def _drive_tts_node(agent: LilyAgent, raw_text: str) -> list[str]:
    """Run LilyAgent.tts_node end-to-end with the default TTS node swapped
    for a recorder — captures exactly what reaches synthesis."""
    captured: list[str] = []

    async def _recording_default(agent_self, text, model_settings):
        async for chunk in text:
            captured.append(chunk)
        if False:  # pragma: no cover — keeps this an async generator
            yield

    original = Agent.default.tts_node
    Agent.default.tts_node = _recording_default
    try:
        async def _speak():
            async def _chunks():
                yield raw_text

            async for _frame in agent.tts_node(_chunks(), None):
                pass

        _run(_speak())
    finally:
        Agent.default.tts_node = original
    return captured


def test_tts_node_strips_markdown_and_emoji_at_tts_input():
    agent = LilyAgent.__new__(LilyAgent)
    game = _make_game()
    agent._game = game
    agent._empty_retry_pending = False

    captured = _drive_tts_node(
        agent, "**Correct!** *Sarah* takes it \U0001F389"
    )
    assert captured == ["Correct! Sarah takes it."]


def test_tts_node_preserves_audio_tags_and_flush_period():
    agent = LilyAgent.__new__(LilyAgent)
    game = _make_game()
    agent._game = game
    agent._empty_retry_pending = False

    captured = _drive_tts_node(agent, "[excited] `Ten` points")
    # Bracket audio tags survive; backticks stripped; the punctuation-flush
    # guard appends the terminal period.
    assert captured == ["[excited] Ten points."]
