"""WO-LILY-OMNIBUS-003 WS-1 — structural-claim integrity fixture.

Evidence session `lily-81BCB0-583a0f16` (2026-08-05, 4-player echo room),
rows committed in tests/fixtures/claim_integrity_lily_81BCB0.json:

* 22:48:39 — the auto-start safety net fired MID round-robin (Rami and
  Chris bound; Rhonda and Paige still introducing themselves). Question
  one (q_0001, Jupiter) armed, the structural delivery flag armed, and
  the next outbound turn — the intake acknowledgment "Hi, Chris! Got you
  locked in. That leaves two more. Who's next?" — claimed `q_1_delivery`
  regardless of phrasing. Rhonda's self-introduction ("Hi. My name is
  Rhonda.") then fell into the ghost window and was adjudicated as a
  wrong answer to a question never spoken.

* 22:58:44 — `q_7_delivery` (q_8231, Walter White) claimed on the
  apology turn "My bad, team! …" which REVEALS the answer and carries no
  question text: Walter White never received a legitimate window.

WS-1 contract pinned here:
  1. The strict text-sanity rewrite applies to EVERY structural claim: a
     delivery turn that does not contain the question text comes back
     "rewrite_strict" and is rewritten to the deterministic sheet before
     claiming — never claimed silently.
  2. Pre-game (game_started=False) the claim lifecycle is inert: no
     claim registration, no delivery-flag arming, no window arming.
  3. begin_round (tool / voice / auto-start — all converge on
     start_game) holds while the intake round-robin is still growing: a
     speaker bind inside lily_config.intake_settle_seconds() defers the
     start; the per-segment auto-start net retries once names stop
     landing.

The question PROMPTS below are reconstructed (the record persists only
question_text_hash + canonical_answer); every spoken turn and canonical
answer is verbatim from the session rows.

This file imports lily_agent (and therefore livekit) — same boundary
note as test_desync_fixture.py.
"""

import asyncio
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_audeering_consumers
import lily_config
import lily_evaluation
import lily_say_gate
from lily_agent import LilyAgent, LilyGame
from lily_scorekeeper import LilyScorekeeper

FIXTURE = json.loads(
    (Path(__file__).resolve().parent
     / "fixtures" / "claim_integrity_lily_81BCB0.json").read_text()
)

_BY_ACT = {
    r["speaker_name"]: r["text"]
    for r in FIXTURE["transcripts"]
    if r["speaker_name"]
}
INTAKE_ACK = _BY_ACT["q_1_delivery"]          # "Hi, Chris! Got you locked in…"
APOLOGY_TURN = _BY_ACT["q_7_delivery"]        # "My bad, team! …Walter White…"
RHONDA_INTRO = "Hi. My name is Rhonda."

# Prompts reconstructed around the verbatim canonical answers:
GHOST_Q1 = {
    "id": "q_0001",
    "prompt": "Which planet in our solar system is the largest?",
    "canonical_answer": "Jupiter",
    "acceptable_answers": ["jupiter"],
    "category": "academic",
    "difficulty_tier": 1,
}
Q7_WALTER = {
    "id": "q_8231",
    "prompt": (
        "In the series Breaking Bad, what is the full name of the "
        "chemistry teacher who breaks bad?"
    ),
    "canonical_answer": "Walter White",
    "acceptable_answers": ["walter white"],
    "category": "pop culture",
    "difficulty_tier": 2,
}


class _FakeSession:
    def __init__(self) -> None:
        self.instructions: list[str] = []

    def generate_reply(self, instructions: str) -> None:
        self.instructions.append(instructions)


class _FakeAgentHandle:
    def set_preemptive_generation(self, enabled: bool) -> None:
        pass


def _make_game(game_started: bool) -> LilyGame:
    """Minimal LilyGame via __new__ (test_desync_fixture pattern) — the
    attributes the claim / window / start_game paths touch."""
    game = LilyGame.bare()
    game.session = _FakeSession()
    game.agent = _FakeAgentHandle()
    game._preemptive_paused = False
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game.sk = LilyScorekeeper(FIXTURE["session_id"])
    game.memory_block = ""
    game.reconnected = False
    game.game_started = game_started
    game.game_over = False
    game.armed_question = None
    game.next_question = None
    game.used_prompts = []
    game.supabase = None
    game.ui_phase = "lobby"
    game._window_timer = None
    game._bed_handle = None
    game.background_audio = None
    game._steal_window = False
    game._adjudicating = False
    game._pending_reveal_event = None
    game._pending_unbound_award = None
    game._armed_speech_misses = 0
    game._pending_delivery_qnum = None
    game._phase_hold = None
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
    game.session_started_at = time.time() - 300.0  # lobby grace elapsed
    game._prefetch_task = None
    game._watchdog_task = None
    game._prefetch_stall_ticks = 0
    game._judged_keys = set()

    game.metadata_publishes: list[str] = []

    async def _publish_metadata(question_text, **kwargs):
        game.metadata_publishes.append(question_text or "")

    async def _publish_attributes(*a, **k):
        pass

    game.publish_metadata = _publish_metadata
    game.publish_attributes = _publish_attributes
    game.publish_attributes_nowait = lambda: None
    return game


def _stub_start_dependencies(game: LilyGame) -> None:
    """start_game's heavy collaborators, stubbed the way
    test_say_gate_dispatch._start_game does — the intake gate under test
    runs first and for real."""

    async def _noop_async(*a, **k):
        return None

    game.resolve_group_identity = _noop_async
    game.start_prefetch = lambda: None
    game.arm_next_question = lambda: False
    game.start_idle_watchdog = lambda: None
    game.fire_enrollment = lambda trigger: None
    game._enroll_started = True
    game.request_device_verification = lambda trigger: None
    game.send_event_nowait = lambda *a, **k: None


def _arm(game: LilyGame, question: dict) -> None:
    game.armed_question = dict(question)
    game.sk.start_question(game.armed_question)
    game._armed_speech_misses = 0
    game._pending_delivery_qnum = None


def _run(coro, game: LilyGame | None = None):
    async def _wrapped():
        result = await coro
        if (
            game is not None
            and game._window_timer is not None
            and not game._window_timer.done()
        ):
            game._window_timer.cancel()
            await asyncio.sleep(0)
        return result

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_wrapped())
    finally:
        loop.close()


async def _drain():
    await asyncio.sleep(0)


# -- the evidence rows pin the live defect classes -----------------------------


def test_evidence_rows_pin_the_live_defect():
    # Both claimed turns lack their question's text entirely:
    assert not lily_evaluation.lily_turn_presents_question(
        GHOST_Q1["prompt"], INTAKE_ACK
    )
    assert not lily_evaluation.lily_turn_presents_question(
        Q7_WALTER["prompt"], APOLOGY_TURN
    )
    # …and the apology turn is a reveal, not a delivery:
    assert "Walter White" in APOLOGY_TURN
    # Rhonda's self-introduction really was adjudicated against the ghost
    # q_0001 (a question never spoken):
    ghost_rows = [
        a for a in FIXTURE["answers"] if a["question_id"] == "q_0001"
    ]
    assert ghost_rows and ghost_rows[0]["transcript"] == RHONDA_INTRO
    assert ghost_rows[0]["verdict"] == "incorrect"
    # Canonical answers are verbatim from lily_asked_history:
    answers = {
        r["question_id"]: r["canonical_answer"]
        for r in FIXTURE["asked_history"]
    }
    assert answers == {"q_0001": "Jupiter", "q_8231": "Walter White"}


# -- pre-game: the claim lifecycle is inert ------------------------------------


def test_pregame_structural_flag_never_claims():
    # 22:48:45 replay, defensive layer: even with a question armed while
    # game_started is False, the intake acknowledgment registers NOTHING —
    # expect_delivery is a pre-game no-op and the claim gate returns None
    # (never a silent structural claim).
    game = _make_game(game_started=False)
    _arm(game, GHOST_Q1)

    game.expect_delivery()
    assert game._pending_delivery_qnum is None

    # Even a forced stale flag cannot claim pre-game:
    game._pending_delivery_qnum = 1
    assert game.register_delivery_claim(INTAKE_ACK) is None
    assert game.say_registry.state("q_1_delivery") is None


def test_pregame_window_never_arms():
    game = _make_game(game_started=False)
    _arm(game, GHOST_Q1)

    async def scenario():
        game.open_window()
        assert game.sk.answer_window_open is False
        game.on_agent_speech_finished(INTAKE_ACK)
        await _drain()

    _run(scenario(), game)
    assert game.sk.answer_window_open is False
    assert game.metadata_publishes == []


# -- begin_round holds while the intake round-robin grows ----------------------


def test_auto_start_defers_while_intake_roundrobin_grows(caplog):
    # 22:48:39 replay: Rami is bound, Chris's bind just landed, two more
    # introductions are coming. Grace elapsed, roster >= 2, prefetch done
    # — every legacy auto-start guard passes — but the bind is fresh, so
    # the start DEFERS and nothing arms.
    game = _make_game(game_started=False)
    _stub_start_dependencies(game)
    game.sk.bind_speaker("Rami", "Rami")
    game.sk.bind_speaker("S1", "Chris")
    game.next_question = dict(GHOST_Q1)

    async def scenario():
        with caplog.at_level(logging.INFO):
            game.on_speaker_bound("S1", "Chris")
            await _drain()
            await _drain()

    _run(scenario(), game)
    assert game.game_started is False
    assert game.armed_question is None
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "START_DEFERRED" in joined
    assert "AUTO_START" not in joined


def test_auto_start_fires_once_intake_settles():
    # Names stopped landing AND the lobby went quiet: the settle window
    # elapsed, the quiet-after-last-user-turn gate is satisfied, and the
    # next per-segment auto-start check starts the game.
    game = _make_game(game_started=False)
    _stub_start_dependencies(game)
    game.sk.bind_speaker("Rami", "Rami")
    game.sk.bind_speaker("S1", "Chris")
    game.next_question = dict(GHOST_Q1)
    game._last_bind_at = time.time() - (
        lily_config.intake_settle_seconds() + 5.0
    )
    game.session_started_at = time.time() - (
        lily_config.auto_start_lobby_grace_seconds() + 5.0
    )
    game._last_user_turn_at = time.monotonic() - (
        lily_config.auto_start_quiet_seconds() + 5.0
    )

    async def scenario():
        game._maybe_auto_start_after_lobby()
        await _drain()
        await _drain()

    _run(scenario(), game)
    assert game.game_started is True


def test_auto_start_holds_while_lobby_still_talking():
    # Grace + intake settle pass, but a user turn just landed — banter is
    # still live, so auto-start must defer (RM_VYp6 mid-fact-collection).
    game = _make_game(game_started=False)
    _stub_start_dependencies(game)
    game.sk.bind_speaker("Rami", "Rami")
    game.sk.bind_speaker("S1", "Chris")
    game.next_question = dict(GHOST_Q1)
    game._last_bind_at = time.time() - (
        lily_config.intake_settle_seconds() + 5.0
    )
    game.session_started_at = time.time() - (
        lily_config.auto_start_lobby_grace_seconds() + 5.0
    )
    game._last_user_turn_at = time.monotonic()  # just spoke

    async def scenario():
        game._maybe_auto_start_after_lobby()
        await _drain()

    _run(scenario(), game)
    assert game.game_started is False


def test_begin_round_tool_holds_while_intake_grows():
    # Lily calling the tool mid round-robin gets a result that keeps her
    # conducting intake instead of the "round one is armed" contract; the
    # game does not start.
    agent = LilyAgent.__new__(LilyAgent)
    game = _make_game(game_started=False)
    _stub_start_dependencies(game)
    agent._game = game
    game.sk.bind_speaker("Rami", "Rami")
    game.sk.bind_speaker("S1", "Chris")
    game.next_question = dict(GHOST_Q1)
    game._last_bind_at = time.time()

    result = _run(LilyAgent.lily_begin_round.__wrapped__(agent, None))
    assert game.game_started is False
    assert "sole deliverer" not in result
    assert "name" in result.lower()


def test_rhonda_intro_is_game_inert():
    # The full intake replay: with the start deferred, no question is
    # armed, no claim registers, no window opens — Rhonda's introduction
    # records nothing anywhere in the answer pipeline.
    game = _make_game(game_started=False)
    _stub_start_dependencies(game)
    game.sk.bind_speaker("Rami", "Rami")
    game.sk.bind_speaker("S1", "Chris")
    game.next_question = dict(GHOST_Q1)

    async def scenario():
        game.on_speaker_bound("S1", "Chris")   # bind just landed
        await _drain()
        await _drain()
        assert game.game_started is False
        # Lily's intake acknowledgment goes out and registers nothing:
        assert game.register_delivery_claim(INTAKE_ACK) is None
        game.on_agent_speech_finished(INTAKE_ACK)
        await _drain()
        # Rhonda introduces herself into the (correctly absent) window:
        now = time.time()
        game.sk.on_transcript_segment(
            text=RHONDA_INTRO, speaker_label="S2", is_final=True,
            now=now, segment_start_time=now,
        )
        await _drain()

    _run(scenario(), game)
    assert game.sk.answer_window_open is False
    assert game.sk.ordered_candidates() == []
    assert game.say_registry.state("q_1_delivery") is None
    assert game.metadata_publishes == []


# -- every structural claim is text-checked (the q_7 apology replay) -----------


def test_q7_apology_rewrites_to_sheet_before_claiming():
    # Mid-game, q7 armed, the plain (non-strict) structural flag set —
    # the historical path that silently claimed q_7_delivery on "My bad,
    # team!". Now: the mismatching turn comes back "rewrite_strict" with
    # NO claim, and the tts_node rewrite protocol delivers the sheet.
    game = _make_game(game_started=True)
    game.ui_phase = "question"
    _arm(game, Q7_WALTER)
    game.sk.question_number = 7

    async def scenario():
        game.expect_delivery()  # plain arm — no strict flag
        verdict = game.register_delivery_claim(APOLOGY_TURN)
        assert verdict == "rewrite_strict"
        assert game.say_registry.state("q_7_delivery") is None
        assert game.sk.answer_window_open is False

        # tts_node rewrite protocol: re-arm, speak the sheet, claim.
        game.expect_delivery()
        sheet = game.rendered_armed_question()
        assert lily_evaluation.lily_turn_presents_question(
            Q7_WALTER["prompt"], sheet
        )
        assert game.register_delivery_claim(sheet) == "claimed_structural"
        game.on_agent_speech_finished(sheet)
        await _drain()

    _run(scenario(), game)
    # The window opened off a turn that actually carries the question:
    assert game.sk.answer_window_open is True
    assert (
        game.say_registry.state("q_7_delivery")
        == lily_say_gate.CLAIM_CONFIRMED
    )
    assert Q7_WALTER["prompt"] in game.metadata_publishes
