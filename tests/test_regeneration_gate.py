"""WS-3 (WO-LILY-OMNIBUS-003, AMENDMENT-001): barged-turn regeneration.

The live defect (session lily-81BCB0-583a0f16): interrupted turns
re-dispatch IDENTICAL text — greet aired twice, a tension line four
times, the Black Panther reveal five. The claim gate (WS-1/4/7) now
stops the double-AIR, but nothing yet forces a barged turn to
REGENERATE rather than replay. WS-3 graduates the repeat lint from
telemetry to a GATE on the re-air path:

  - a re-dispatch after a cut/suppressed turn carries a regeneration
    directive at the single dispatch choke (gated_say), so the retried
    conversational line is spoken FRESH and SHORTER, never verbatim;
  - question deliveries are exempt from the "shorter, answer-first"
    reshape — a barged question is re-read verbatim on purpose (players
    need the whole question) — but still take a clean-delivery directive;
  - the tts_node repeat lint becomes a gate: a re-air turn that STILL
    repeats an already-aired turn is suppressed and regenerated once.

Same import boundary as test_interruption_layer.py (imports lily_agent
and therefore livekit). Framework-free: exercises LilyGame decision
methods, the same seam WS-14's regenerate-not-replay test uses.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_audeering_consumers
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
    game = LilyGame.bare()
    game.session = _FakeSession()
    game.agent = _FakeAgentHandle()
    game._preemptive_paused = False
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game.sk = LilyScorekeeper("ws3-regen")
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
    game._reair_gate_armed = False
    game._reair_turn_pending = False
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

    async def _publish_attributes(*a, **k):
        pass

    game.publish_metadata = _publish_metadata
    game.publish_attributes = _publish_attributes
    return game


def _arm(game: LilyGame, prompt: str) -> None:
    game.armed_question = {"prompt": prompt, "canonical_answer": "-"}
    game.sk.start_question(game.armed_question)
    game._armed_speech_misses = 0
    game._pending_delivery_qnum = None


PROMPT = "Which planet is the largest in the Solar System?"


# -- the arm: an interrupted/suppressed turn schedules a re-air ---------------


def test_interrupt_arms_reair_gate():
    game = _make_game()
    _arm(game, PROMPT)
    game.expect_delivery()
    game.register_delivery_claim(PROMPT, speech_id="s1")
    game.on_agent_speech_finished("Which pla", speech_id="s1", interrupted=True)
    assert game.peek_reair_gate() is True


def test_suppressed_with_release_arms_reair_gate():
    game = _make_game()
    _arm(game, PROMPT)
    game.expect_delivery()
    game.register_delivery_claim(PROMPT, speech_id="s2")
    game.on_agent_speech_finished("", speech_id="s2", suppressed=True)
    assert game.peek_reair_gate() is True


def test_suppressed_without_release_does_not_arm():
    game = _make_game()
    _arm(game, PROMPT)
    # No claim registered for this speech id -> nothing releases.
    game.on_agent_speech_finished("", speech_id="ghost", suppressed=True)
    assert game.peek_reair_gate() is False


def test_clean_playout_does_not_arm():
    game = _make_game()
    game.say_registry.claim("session_greet", owner="g1")
    game.on_agent_speech_finished(
        "Hi table, welcome in!", speech_id="g1", interrupted=False
    )
    assert game.peek_reair_gate() is False


# -- the dispatch directive: re-air regenerates, never replays ----------------


def test_conversational_reair_carries_regen_directive():
    """A barged conversational line (reveal/verdict) re-dispatched through
    gated_say carries the fresh-words directive, and the arm is consumed."""
    game = _make_game()
    game.arm_reair_gate()
    ok = game.gated_say(
        "q_1_reveal",
        "reveal",
        "Announce that Rami got it: the answer is Jupiter.",
        source="adjudicate",
    )
    assert ok is True
    instr = game.session.instructions[-1]
    assert lily_agent._REGEN_REAIR_DIRECTIVE.strip() in instr
    assert game.peek_reair_gate() is False  # one-shot consumed


def test_question_delivery_reair_keeps_verbatim_and_clean_directive():
    """A barged question re-read takes the clean-delivery directive, NOT
    the conversational reshape — the question text stays exact."""
    game = _make_game()
    _arm(game, PROMPT)
    game.expect_delivery()
    game.arm_reair_gate()
    assert game.dispatch_armed_question(source="post_barge") is True
    instr = game.session.instructions[-1]
    assert PROMPT in instr  # question verbatim, re-read cleanly
    assert lily_agent._REGEN_DELIVERY_DIRECTIVE.strip() in instr
    assert lily_agent._REGEN_REAIR_DIRECTIVE.strip() not in instr


def test_reair_directive_is_one_shot():
    game = _make_game()
    game.arm_reair_gate()
    game.gated_say(None, "steal_window", "Announce a five-second steal.",
                   source="adjudicate")
    first = game.session.instructions[-1]
    assert lily_agent._REGEN_REAIR_DIRECTIVE.strip() in first
    # A subsequent, unrelated dispatch is NOT a re-air:
    game.gated_say(None, "media_mode", "Confirm media mode is on.",
                   source="voice")
    second = game.session.instructions[-1]
    assert lily_agent._REGEN_REAIR_DIRECTIVE.strip() not in second


def test_first_time_dispatch_has_no_directive():
    game = _make_game()
    game.gated_say("q_1_reveal", "reveal", "Announce Jupiter.",
                   source="adjudicate")
    instr = game.session.instructions[-1]
    assert lily_agent._REGEN_REAIR_DIRECTIVE.strip() not in instr
    assert lily_agent._REGEN_DELIVERY_DIRECTIVE.strip() not in instr


def test_dup_suppressed_dispatch_does_not_consume_arm():
    """A dispatch that loses the claim race must not eat the re-air arm —
    the real re-dispatch still needs it."""
    game = _make_game()
    game.say_registry.claim("q_1_reveal")  # already owned
    game.arm_reair_gate()
    ok = game.gated_say("q_1_reveal", "reveal", "Announce Jupiter.",
                        source="adjudicate")
    assert ok is False  # suppressed as dup
    assert game.peek_reair_gate() is True  # arm preserved


# -- the tts_node gate: verbatim replay on the re-air path is suppressed ------


def test_verbatim_conversational_reair_is_gated():
    """The tts_node decision: a re-air turn that repeats an already-aired
    turn verbatim must regenerate (True). Consumes the turn signal."""
    game = _make_game()
    game.sk.record_agent_turn("The tension is real, folks, hold tight now")
    # Mark the current outbound turn as a re-air (set by the dispatch):
    game._reair_turn_pending = True
    repeat = lily_say_gate.lily_repeat_flag(
        "The tension is real, folks, hold tight now", game.sk.agent_turns
    )
    assert repeat is not None
    assert game.reair_verbatim_should_regenerate(
        "The tension is real, folks, hold tight now", repeat
    ) is True
    # one-shot: the turn signal is consumed
    assert game._reair_turn_pending is False


def test_question_delivery_reair_is_exempt_from_verbatim_gate():
    """A barged question re-read repeats the sheet verbatim BY DESIGN and
    must NOT be gated."""
    game = _make_game()
    _arm(game, PROMPT)
    game.expect_delivery()  # sets _pending_delivery_qnum
    game.sk.record_agent_turn(PROMPT)
    game._reair_turn_pending = True
    repeat = lily_say_gate.lily_repeat_flag(PROMPT, game.sk.agent_turns)
    assert repeat is not None
    assert game.reair_verbatim_should_regenerate(PROMPT, repeat) is False


def test_non_reair_turn_is_not_gated():
    """A first-time turn that merely shares a phrase with history is NOT a
    re-air and must air (the lint stays telemetry off the re-air path)."""
    game = _make_game()
    game.sk.record_agent_turn("The tension is real, folks, hold tight now")
    # No re-air pending:
    repeat = lily_say_gate.lily_repeat_flag(
        "The tension is real, folks, hold tight now", game.sk.agent_turns
    )
    assert repeat is not None
    assert game.reair_verbatim_should_regenerate(
        "The tension is real, folks, hold tight now", repeat
    ) is False


def test_fresh_reair_is_not_gated():
    """A re-air that came back with FRESH words (no repeat) airs — the gate
    only bites verbatim replays."""
    game = _make_game()
    game.sk.record_agent_turn("The tension is real, folks, hold tight now")
    game._reair_turn_pending = True
    fresh = "Jupiter it is — Rami takes the point, two for two."
    repeat = lily_say_gate.lily_repeat_flag(fresh, game.sk.agent_turns)
    assert repeat is None
    assert game.reair_verbatim_should_regenerate(fresh, repeat) is False


# -- full loop: interrupt -> re-dispatch -> fresh text, no verbatim replay ----


def test_barge_then_regenerate_full_loop_no_verbatim():
    """End-to-end on the decision seam: a reveal is barged, re-dispatched
    with the regen directive, and a verbatim retry is caught by the
    tts_node gate while a fresh retry airs."""
    game = _make_game()
    # First reveal airs partially, then is cut:
    game.say_registry.claim("q_1_reveal", owner="rev1")
    game.on_agent_speech_finished(
        "Rami came in first and said", speech_id="rev1", interrupted=True
    )
    assert game.peek_reair_gate() is True

    # Re-dispatch carries the directive (regenerate, don't replay):
    game.say_registry.release_owner("rev1")  # claim freed for redelivery
    game.gated_say("q_1_reveal", "reveal",
                   "Announce Rami got it: Jupiter.", source="adjudicate")
    assert lily_agent._REGEN_REAIR_DIRECTIVE.strip() in (
        game.session.instructions[-1]
    )
    # The dispatch handed the re-air signal to tts_node:
    assert game._reair_turn_pending is True

    # If the LLM STILL returns a verbatim replay of the aired fragment,
    # the tts_node gate suppresses it:
    verbatim = "Rami came in first and said Jupiter"
    repeat = lily_say_gate.lily_repeat_flag(verbatim, game.sk.agent_turns)
    assert repeat is not None
    assert game.reair_verbatim_should_regenerate(verbatim, repeat) is True
