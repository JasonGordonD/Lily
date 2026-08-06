"""WS-14 (WO-LILY-OMNIBUS-003, AMENDMENT-001/002): interruption + noise layer.

Covers, against the INSTALLED livekit-agents 1.6.6 in the venv:
  - the false-interruption contract the session actually resolves:
    resume_false_interruption ON, timeout 2.0s, adaptive interruption
    requested (Speechmatics qualifies: aligned_transcript + streaming) —
    this is the #3418 pause/resume machinery Lily now pins explicitly;
  - regenerate-not-replay: an interrupted (real barge-in) delivery
    releases its claim and re-arms the structural flag, and the retry
    routes through gated_say -> instructed_reply -> generate_reply — the
    single choke point where WS-3's regeneration gate sits — as a fresh
    instructions-driven generation, never a replay of the cut text;
  - the #3418 ghost signature (registered speech that never airs):
    failure surfaces as suppressed -> claims release, the turn is never
    recorded as heard, no ghost window opens, redelivery is permitted;
  - Krisp resolver: ambient NC by default, LILY_NOISE_CANCELLATION=off
    kill switch, and BVC structurally unreachable (the multiplayer trap:
    in a one-mic room the "background voices" are the other players);
  - room-discharge pacing (AMENDMENT-002): the gap delays the window's
    mic-sensitive phase, a racing open wins, gap=0 is the pre-WS-14
    synchronous open (the suite-wide conftest baseline).

Same import boundary note as test_say_gate_dispatch.py (imports
lily_agent and therefore livekit).
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_audeering_consumers
import lily_config
import lily_say_gate
import lily_agent
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


def _make_game() -> LilyGame:
    """Minimal LilyGame via __new__ (test_desync_fixture pattern)."""
    game = LilyGame.__new__(LilyGame)
    game.session = _FakeSession()
    game.agent = _FakeAgentHandle()
    game._preemptive_paused = False
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game.sk = LilyScorekeeper("ws14-interruption")
    game.memory_block = ""
    game.reconnected = False
    game.game_started = True
    game.game_over = False
    game.armed_question = None
    game.next_question = None
    game.used_prompts = []
    game.supabase = None
    game.ui_phase = "question"
    game._window_timer = None
    game._bed_handle = None
    game.background_audio = None
    game._steal_window = False
    game._adjudicating = False
    game._pending_reveal_event = None
    game._pending_unbound_award = None
    game._armed_speech_misses = 0
    game._pending_delivery_qnum = None
    game._strict_delivery_qnum = None
    game.eliminated = []
    game.acoustic = lily_audeering_consumers.LilyAcousticState()
    game.prefs = {}
    game._prefs_offer_made = False
    game.pending_clarify = {}
    game.forget_state = "idle"
    game.forget_requester = None
    game._state_note = None
    game._user_turn_index = 0
    game.promoted_categories = []
    game._prefetch_task = None
    game._watchdog_task = None
    game._prefetch_stall_ticks = 0
    game._pre_window_segments = None

    async def _publish_metadata(question_text, **kwargs):
        pass

    async def _publish_attributes():
        pass

    game.publish_metadata = _publish_metadata
    game.publish_attributes = _publish_attributes
    return game


def _arm(game: LilyGame, prompt: str) -> None:
    game.armed_question = {"prompt": prompt, "canonical_answer": "-"}
    game.sk.start_question(game.armed_question)
    game._armed_speech_misses = 0
    game._pending_delivery_qnum = None


PROMPT = (
    "You've inherited a llama farm: which South American country is home "
    "to the largest llama population?"
)


# -- framework contract: false-interruption pause/resume (1.6.6) --------------


def test_session_resolves_false_interruption_contract():
    """The exact options lily_agent passes resolve, at the installed
    1.6.6, to: pause-on-trigger + resume-after-2s-silence + adaptive
    interruption requested. A noise burst with no transcript therefore
    pauses and resumes playout instead of hard-cutting the turn — the
    framework-layer end of the fragment-storm loop."""
    from livekit.agents import (
        AgentSession,
        InterruptionOptions,
        TurnHandlingOptions,
    )

    async def _build() -> AgentSession:
        # AgentSession needs a running loop (suite-order safe).
        return AgentSession(
            turn_handling=TurnHandlingOptions(
                interruption=InterruptionOptions(
                    min_words=1,
                    min_duration=0.8,
                    resume_false_interruption=True,
                    false_interruption_timeout=(
                        lily_config.false_interruption_timeout()
                    ),
                    mode=lily_config.interruption_mode(),
                ),
            ),
        )

    session = asyncio.run(_build())
    opts = session.options.interruption
    assert opts["resume_false_interruption"] is True
    assert opts["false_interruption_timeout"] == 2.0
    assert opts["min_words"] == 1
    assert opts["min_duration"] == 0.8
    assert session.interruption_detection == "adaptive"


def test_speechmatics_qualifies_for_adaptive_interruption(monkeypatch):
    """1.6.6's _resolve_interruption_detection requires an STT with
    aligned transcripts + streaming; Speechmatics reports both, so the
    adaptive request is honored (and degrades to VAD on its own if the
    Cloud detector is unavailable — never a hard dependency)."""
    monkeypatch.setenv("SPEECHMATICS_API_KEY", "test-key")
    from livekit.plugins.speechmatics import STT

    stt = STT(language="en")
    assert stt.capabilities.aligned_transcript  # "chunk"
    assert stt.capabilities.streaming is True


# -- regenerate-not-replay: the WS-3 gate point -------------------------------


def test_interrupted_delivery_regenerates_through_gated_say():
    """A real barge-in cuts the delivery turn mid-air ("You've…"). The
    claim releases, the structural flag re-arms, and the retry is a FRESH
    dispatch through gated_say (lily_agent's single dispatch choke point)
    — the deterministic sheet, never the cut fragment replayed."""
    game = _make_game()
    _arm(game, PROMPT)
    game.expect_delivery()
    assert game.register_delivery_claim(PROMPT, speech_id="speech_1") == (
        "claimed_structural"
    )
    assert game.say_registry.state("q_1_delivery") == lily_say_gate.CLAIM_PENDING

    # Playout cut after one word — interrupted, never completed:
    game.on_agent_speech_finished(
        "You've", speech_id="speech_1", interrupted=True
    )
    # Claim released so redelivery is legitimate; structural flag re-armed:
    assert game.say_registry.state("q_1_delivery") is None
    assert game._pending_delivery_qnum == game.sk.question_number
    # The cut fragment IS in the record, marked interrupted (Task 0):
    assert any("You've" in t for t in game.sk.agent_turns)

    # The retry routes through the gate point (gated_say -> generate_reply):
    assert game.dispatch_armed_question(source="ws14_retry") is True
    instr = game.session.instructions[-1]
    assert PROMPT in instr  # regenerated from the armed sheet
    assert "You've…" not in instr  # never the cut playback replayed


def test_ghost_delivery_suppressed_releases_and_never_records():
    """#3418's Lisa-ghost signature: a speech registers its claim at
    dispatch but produces no audio. At 1.6.6 the failure surfaces via
    SpeechHandle.exception() and the playout watcher maps it to the
    suppressed path: claim releases, the turn is NOT recorded as heard,
    no window opens off it, and redelivery stays permitted."""
    game = _make_game()
    _arm(game, PROMPT)
    game.expect_delivery()
    assert game.register_delivery_claim(PROMPT, speech_id="speech_9") == (
        "claimed_structural"
    )
    turns_before = list(game.sk.agent_turns)

    game.on_agent_speech_finished(
        PROMPT, speech_id="speech_9", suppressed=True
    )
    assert game.say_registry.state("q_1_delivery") is None
    assert game.sk.agent_turns == turns_before
    assert game.sk.answer_window_open is False
    # Redelivery is legitimate and routes through the same gate point:
    assert game.dispatch_armed_question(source="ws14_ghost_retry") is True


# -- Krisp: NC on, BVC provably off -------------------------------------------


def test_nc_default_is_ambient_model(monkeypatch):
    monkeypatch.delenv("LILY_NOISE_CANCELLATION", raising=False)
    opts = lily_agent.lily_noise_cancellation_options()
    assert opts is not None
    model = os.path.basename(opts.options["modelPath"]).lower()
    assert "bvc" not in model  # the ambient model, never a BVC weights file


def test_nc_kill_switch(monkeypatch):
    monkeypatch.setenv("LILY_NOISE_CANCELLATION", "off")
    assert lily_agent.lily_noise_cancellation_options() is None


def test_bvc_is_unreachable(monkeypatch):
    """No env value can produce BVC: the config coerces unknown values
    (including "bvc") to "nc", and the resolver never constructs the BVC
    model — enforced here by making BVC() explode if touched."""

    def _boom(*args, **kwargs):
        raise AssertionError("BVC constructed — the table just got erased")

    monkeypatch.setattr(lily_agent.noise_cancellation, "BVC", _boom)
    monkeypatch.setattr(lily_agent.noise_cancellation, "BVCTelephony", _boom)
    for value in ("bvc", "BVC", "bvct", "background_voice", "garbage"):
        monkeypatch.setenv("LILY_NOISE_CANCELLATION", value)
        opts = lily_agent.lily_noise_cancellation_options()
        assert opts is not None
        model = os.path.basename(opts.options["modelPath"]).lower()
        assert "bvc" not in model


# -- room-discharge pacing (AMENDMENT-002) ------------------------------------


def test_discharge_gap_delays_window_open(monkeypatch):
    monkeypatch.setenv("LILY_ROOM_DISCHARGE_SECONDS", "0.05")
    game = _make_game()
    _arm(game, PROMPT)
    opened: list = []
    game.open_window = lambda duration=None, steal=False: opened.append(
        duration
    )

    async def scenario():
        game.open_window_after_discharge()
        assert opened == []  # mic-sensitive phase held during discharge
        await asyncio.sleep(0.1)
        assert opened == [None]

    asyncio.run(scenario())


def test_discharge_zero_gap_is_synchronous(monkeypatch):
    """gap=0 (the suite conftest baseline) is exactly the pre-WS-14
    behavior: the window opens inline at playout completion."""
    monkeypatch.setenv("LILY_ROOM_DISCHARGE_SECONDS", "0")
    game = _make_game()
    _arm(game, PROMPT)
    opened: list = []
    game.open_window = lambda duration=None, steal=False: opened.append(
        duration
    )
    game.open_window_after_discharge()
    assert opened == [None]


def test_discharge_racing_open_wins(monkeypatch):
    """If the window opened (or adjudication started) while the gap was
    pending, the discharge timer stands down instead of double-opening."""
    monkeypatch.setenv("LILY_ROOM_DISCHARGE_SECONDS", "0.05")
    game = _make_game()
    _arm(game, PROMPT)
    opened: list = []
    game.open_window = lambda duration=None, steal=False: opened.append(
        duration
    )

    async def scenario():
        game.open_window_after_discharge()
        game.sk.answer_window_open = True  # racing open landed first
        await asyncio.sleep(0.1)
        assert opened == []

    asyncio.run(scenario())


def test_answers_during_gap_keep_buffering():
    """During the discharge gap the window is still closed and the
    delivery claim is registered — exactly buffer_pre_window_answer's
    buffering condition, so early answers spoken into the gap replay at
    open instead of vanishing."""
    game = _make_game()
    _arm(game, PROMPT)
    game.expect_delivery()
    game.register_delivery_claim(PROMPT, speech_id="speech_2")
    assert game.sk.answer_window_open is False
    game.buffer_pre_window_answer(
        {"speaker_label": "S1", "text": "Peru", "segment_start": 1.0}
    )
    assert game._pre_window_segments and (
        game._pre_window_segments[-1]["text"] == "Peru"
    )
