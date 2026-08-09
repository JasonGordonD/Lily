"""WO-LILY-HOTFIX-007 Y10 — the FLOOR-001 counterweight.

Lily received the whole push mandate in CODE (the 3.0s responsiveness
budget, the M1 "silence is her failure mode" gate, the auto-resume
watchdog) and none of FLOOR-001's restraint. FL-1 shipped a per-utterance
addressee classifier whose judgment was consumed ONLY as two prompt
context lines; FL-2, the floor-state machine it names as its downstream
contract, was never built, so nothing that detects the room holding the
floor had any authority over a dispatch decision.

Y10 is the minimum counterweight:

  1. floor_state() — a DERIVED read (no new state) over the four surfaces
     that already each held a piece of it: the hold, _question_pending,
     sk.host_speaking, and FL-1's last_addressee_judgment.
  2. The graded choice — the cut-recovery watchdog, the purest code-side
     push path (a machine timer speaking into silence with nobody asking),
     can now choose SILENCE when the floor is not hers.
  3. Chain F closed — trigger_cut_recovery dispatched via instructed_reply
     and so skipped every gate in gated_say (hold, question-pending,
     progression pause, live-game). It routes through the same funnel now.

Fixture-first and framework-free: LilyGame.__new__ harnesses plus
source-order pins, the pattern of test_cut_recovery.py /
test_meta_progression_pause.py.
"""

import inspect
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_addressee_classifier
import lily_say_gate
import lily_speech_delivery
from lily_agent import LilyGame, _CUT_RECOVERY_DIRECTIVE
from lily_scorekeeper import LilyScorekeeper


class _FakeSession:
    def __init__(self) -> None:
        self.instructions: list = []

    def generate_reply(self, instructions: str):
        self.instructions.append(instructions)
        return object()  # truthy SpeechHandle stand-in


class _FakeAgentHandle:
    def set_preemptive_generation(self, enabled: bool) -> None:
        pass


def _game(session_id: str = "y10-floor") -> LilyGame:
    game = LilyGame.__new__(LilyGame)
    game.session = _FakeSession()
    game.agent = _FakeAgentHandle()
    game._preemptive_paused = False
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game.sk = LilyScorekeeper(session_id)
    game.transcripts = None
    game.game_started = True
    game.game_over = False
    game.armed_question = None
    game._adjudicating = False
    game._hold_active = False
    game._question_pending = False
    game._delivery_stop_sticky = False
    game._awaiting_address_since = 0.0
    game._setup_pending = set()
    game.addressee_classifier = (
        lily_addressee_classifier.LilyAddresseeClassifier()
    )
    game.last_addressee_judgment = None
    return game


def _judgment(classification: str, ts: float):
    return lily_addressee_classifier.LilyAddresseeJudgment(
        classification=classification,
        score=0.2,
        components={"prior": 0.35, "name": 0.0, "acoustic": None},
        name_evidence=lily_addressee_classifier.NAME_NONE,
        cluster_id=None,
        cluster_event=None,
        reason="score",
        speaker_label="S1",
        ts=ts,
    )


# ---------------------------------------------------------------------------
# 1. floor_state — the derived read
# ---------------------------------------------------------------------------

def test_open_floor_is_the_residual_state():
    game = _game()
    assert game.floor_state() == LilyGame.FLOOR_OPEN


def test_hold_is_a_floor_state():
    game = _game()
    game._hold_active = True
    assert game.floor_state() == LilyGame.FLOOR_HOLD


def test_her_own_live_audio_is_her_floor():
    game = _game()
    game.sk.host_speaking = True
    assert game.floor_state() == LilyGame.FLOOR_LILY_SPEAKING


def test_recent_side_cluster_gives_the_floor_to_the_room():
    game = _game()
    game.last_addressee_judgment = _judgment(
        lily_addressee_classifier.CLASS_SIDE_CLUSTER, time.time()
    )
    assert game.room_holds_floor() is True
    assert game.floor_state() == LilyGame.FLOOR_PLAYER_SPEAKING


def test_recent_table_talk_gives_the_floor_to_the_room():
    game = _game()
    game.last_addressee_judgment = _judgment(
        lily_addressee_classifier.CLASS_SIDE_CHATTER, time.time()
    )
    assert game.floor_state() == LilyGame.FLOOR_PLAYER_SPEAKING


def test_stale_table_talk_releases_the_floor():
    # A lone side-chatter line goes stale in the classifier's own adjacency
    # window — an old aside is not a running conversation.
    game = _game()
    stale = time.time() - (game.addressee_classifier.adjacency_seconds + 1.0)
    game.last_addressee_judgment = _judgment(
        lily_addressee_classifier.CLASS_SIDE_CHATTER, stale
    )
    assert game.room_holds_floor() is False
    assert game.floor_state() == LilyGame.FLOOR_OPEN


def test_live_cluster_holds_the_floor_longer_than_a_lone_aside():
    # Same age, different classification: a locked cluster is a running
    # conversation and keeps the floor for its own liveness bound.
    game = _game()
    age = game.addressee_classifier.adjacency_seconds + 2.0
    assert age < game.addressee_classifier.cluster_max_gap_seconds
    ts = time.time() - age
    game.last_addressee_judgment = _judgment(
        lily_addressee_classifier.CLASS_SIDE_CHATTER, ts
    )
    assert game.floor_state() == LilyGame.FLOOR_OPEN
    game.last_addressee_judgment = _judgment(
        lily_addressee_classifier.CLASS_SIDE_CLUSTER, ts
    )
    assert game.floor_state() == LilyGame.FLOOR_PLAYER_SPEAKING


def test_direct_address_never_takes_the_floor_from_her():
    # The canon KEEPS the responsiveness budget for a direct address ("she
    # never has to be told twice") — Y10 must not turn host-directed speech
    # into a reason for silence.
    game = _game()
    game.last_addressee_judgment = _judgment(
        lily_addressee_classifier.CLASS_HOST_DIRECTED, time.time()
    )
    assert game.room_holds_floor() is False
    assert game.floor_state() == LilyGame.FLOOR_OPEN


def test_question_pending_is_the_rooms_floor():
    # PATCH-003 P10 state, read as a floor state rather than re-implemented.
    game = _game()
    game._question_pending = True
    assert game.floor_state() == LilyGame.FLOOR_PLAYER_SPEAKING


def test_hold_outranks_a_live_room_read():
    game = _game()
    game._hold_active = True
    game.last_addressee_judgment = _judgment(
        lily_addressee_classifier.CLASS_SIDE_CLUSTER, time.time()
    )
    assert game.floor_state() == LilyGame.FLOOR_HOLD


def test_her_live_audio_outranks_a_stale_room_read():
    game = _game()
    game.sk.host_speaking = True
    game.last_addressee_judgment = _judgment(
        lily_addressee_classifier.CLASS_SIDE_CLUSTER, time.time()
    )
    assert game.floor_state() == LilyGame.FLOOR_LILY_SPEAKING


def test_floor_state_survives_a_bare_new_harness():
    # No addressee classifier, no hold attributes: the read must degrade to
    # OPEN rather than raising inside a speech path.
    game = LilyGame.__new__(LilyGame)
    game.sk = LilyScorekeeper("y10-bare")
    assert game.floor_state() == LilyGame.FLOOR_OPEN


# ---------------------------------------------------------------------------
# 2. The graded choice — the auto-resume can now choose silence
# ---------------------------------------------------------------------------

def test_recovery_stands_down_while_the_room_holds_the_floor():
    game = _game()
    game.arm_cut_recovery("...and the answer was")
    game.last_addressee_judgment = _judgment(
        lily_addressee_classifier.CLASS_SIDE_CLUSTER, time.time()
    )
    assert game._cut_recovery_should_fire(game._cut_recovery_token) is False
    assert game.session.instructions == []


def test_recovery_stands_down_on_table_talk():
    game = _game()
    game.arm_cut_recovery("...and the answer was")
    game.last_addressee_judgment = _judgment(
        lily_addressee_classifier.CLASS_SIDE_CHATTER, time.time()
    )
    assert game._cut_recovery_should_fire(game._cut_recovery_token) is False


def test_recovery_stands_down_inside_a_hold():
    # GUARD_MAP chain F, stated in the doc: "A cut that lands while a hold
    # is active therefore produces an auto-resume that the hold was
    # specifically designed to prevent."
    game = _game()
    game.arm_cut_recovery("...take your time, I'll")
    game._hold_active = True
    assert game._cut_recovery_should_fire(game._cut_recovery_token) is False


def test_recovery_still_fires_into_a_genuine_lull():
    # The counterweight must not become a mute button: an open floor is
    # exactly when the canon says the floor comes back to her.
    game = _game()
    game.arm_cut_recovery("...and the answer was")
    assert game.floor_state() == LilyGame.FLOOR_OPEN
    assert game._cut_recovery_should_fire(game._cut_recovery_token) is True


def test_recovery_still_fires_after_the_room_read_goes_stale():
    game = _game()
    game.arm_cut_recovery("...and the answer was")
    game.last_addressee_judgment = _judgment(
        lily_addressee_classifier.CLASS_SIDE_CHATTER,
        time.time() - (game.addressee_classifier.adjacency_seconds + 1.0),
    )
    assert game._cut_recovery_should_fire(game._cut_recovery_token) is True


# ---------------------------------------------------------------------------
# 3. Chain F — the auto-resume goes through the dispatch gate
# ---------------------------------------------------------------------------

def test_hold_gate_now_binds_the_auto_resume():
    # Chain F's exact defect: bypass the fire decision entirely and dispatch
    # while held. Before Y10 this spoke; now the shared hold gate refuses.
    game = _game()
    game._hold_active = True
    assert game.trigger_cut_recovery() is False
    assert game.session.instructions == []


def test_hold_exempt_sources_do_not_cover_cut_recovery():
    game = _game()
    assert "cut_recovery" not in LilyGame._HOLD_EXEMPT_SOURCES
    game._hold_active = True
    assert game.hold_blocks_dispatch("cut_recovery", "cut_recovery") is True


def test_question_pending_gate_now_binds_the_auto_resume():
    game = _game()
    game._question_pending = True
    assert game.trigger_cut_recovery() is False
    assert game.session.instructions == []


def test_stopped_game_blocks_the_auto_resume_at_dispatch():
    # P8 lane: STOP is sticky, and the hold it enters binds the resume.
    game = _game()
    game._delivery_stop_sticky = True
    game._hold_active = True
    assert game.trigger_cut_recovery() is False
    assert game.session.instructions == []


def test_open_floor_resume_still_dispatches_the_directive():
    game = _game()
    assert game.trigger_cut_recovery() is True
    assert len(game.session.instructions) == 1
    assert _CUT_RECOVERY_DIRECTIVE in game.session.instructions[0]


def test_gated_refusal_leaves_no_live_reair_arm():
    # Y10 review F3. A stood-down recovery must leave NO live arm: the next
    # code dispatch can be an unrelated act minutes later (an IDLE_REARM
    # question_nudge), which would otherwise air a never-cut question
    # carrying the "you were cut short mid-question" directive.
    game = _game()
    game._hold_active = True
    assert game.trigger_cut_recovery() is False
    assert game.peek_reair_gate() is False
    # Cleared, NOT consumed — consuming would hand the tts_node regen gate a
    # turn that is not a re-air.
    assert getattr(game, "_reair_turn_pending", False) is False


def test_yielded_recovery_leaves_no_live_reair_arm():
    game = _game()
    game.arm_reair_gate()  # the cut arms it in on_agent_speech_finished
    game.arm_cut_recovery("...and the answer was")
    game.last_addressee_judgment = _judgment(
        lily_addressee_classifier.CLASS_SIDE_CLUSTER, time.time()
    )
    assert game._cut_recovery_should_fire(game._cut_recovery_token) is False
    assert game.peek_reair_gate() is False
    assert getattr(game, "_reair_turn_pending", False) is False


def test_a_live_arm_survives_a_recovery_that_does_speak():
    # The cleanup is scoped to the REFUSAL paths — a resume that dispatches
    # still consumes the arm the normal way (WS-3 regenerate-not-replay).
    game = _game()
    game.arm_reair_gate()
    assert game.trigger_cut_recovery() is True
    assert game.peek_reair_gate() is False
    assert game._reair_turn_pending is True


# ---------------------------------------------------------------------------
# Y10 review F1 — the address debt must not outlive the yielded recovery
# ---------------------------------------------------------------------------

def test_yielded_recovery_releases_the_address_debt():
    game = _game()
    game._awaiting_address_since = time.time() - 30.0
    game._address_unanswered_warned = True
    game.arm_cut_recovery("...yes, we've played before, you and")
    game.last_addressee_judgment = _judgment(
        lily_addressee_classifier.CLASS_SIDE_CLUSTER, time.time()
    )
    assert game._cut_recovery_should_fire(game._cut_recovery_token) is False
    assert game._awaiting_address_since == 0.0
    assert game._address_unanswered_warned is False


def test_gated_refusal_releases_the_address_debt():
    game = _game()
    game._awaiting_address_since = time.time() - 30.0
    game._hold_active = True
    assert game.trigger_cut_recovery() is False
    assert game._awaiting_address_since == 0.0


def test_address_latch_never_parks_progression_after_a_yield():
    # THE F1 SEQUENCE, end to end. A host-directed final starts the
    # responsiveness latch; the answering turn is cut BEFORE playout (so
    # note_playout_started never clears it); the room is mid-conversation so
    # the recovery yields. Without the stand-down release,
    # progression_paused_reason() returns address_unanswered forever and the
    # idle watchdog logs WATCHDOG_PAUSED and skips the ENTIRE game-lane
    # recovery ladder on every tick — machine-unbounded in-game dead air.
    game = _game()
    game.last_addressee_judgment = _judgment(
        lily_addressee_classifier.CLASS_HOST_DIRECTED, time.time()
    )
    game._awaiting_address_since = time.time()          # host-directed final
    game.on_agent_speech_finished(                       # cut pre-playout
        "Yes — we've played before, you and", speech_id="s1", interrupted=True
    )
    assert game.progression_paused_reason() == "address_unanswered"
    game.last_addressee_judgment = _judgment(           # table starts talking
        lily_addressee_classifier.CLASS_SIDE_CLUSTER, time.time()
    )
    assert game._cut_recovery_should_fire(game._cut_recovery_token) is False
    assert game.session.instructions == []              # she stayed quiet
    assert game.progression_paused_reason() is None     # ...and nothing parked
    assert game.floor_state() == LilyGame.FLOOR_PLAYER_SPEAKING


def test_debt_release_leaves_an_unset_latch_alone():
    game = _game()
    game._hold_active = True
    assert game.trigger_cut_recovery() is False
    assert game._awaiting_address_since == 0.0


# ---------------------------------------------------------------------------
# Y10 review F2 — the floor yield is in-game only
# ---------------------------------------------------------------------------

def test_pre_game_recovery_does_not_yield_to_the_room():
    # No machine path can re-engage pre-game (the idle watchdog returns
    # early when game_started is False), so a yielded lobby/intake resume
    # would be one-shot dead air exactly where intake needs her back.
    game = _game()
    game.game_started = False
    game.arm_cut_recovery("...and what should I call")
    game.last_addressee_judgment = _judgment(
        lily_addressee_classifier.CLASS_SIDE_CLUSTER, time.time()
    )
    assert game.floor_state() == LilyGame.FLOOR_PLAYER_SPEAKING
    assert game._floor_yields_recovery() is None
    assert game._cut_recovery_should_fire(game._cut_recovery_token) is True


def test_pre_game_hold_is_still_honoured_at_the_dispatch_gate():
    # The pre-game carve-out is scoped to the FLOOR read; an explicit hold
    # ("give us a minute") is refused one layer down, by gated_say.
    game = _game()
    game.game_started = False
    game._hold_active = True
    assert game._floor_yields_recovery() is None
    assert game.trigger_cut_recovery() is False
    assert game.session.instructions == []


def test_in_game_room_read_still_yields():
    game = _game()
    game.last_addressee_judgment = _judgment(
        lily_addressee_classifier.CLASS_SIDE_CLUSTER, time.time()
    )
    assert game._floor_yields_recovery() == LilyGame.FLOOR_PLAYER_SPEAKING


def test_finished_game_does_not_yield_on_the_floor_read():
    # game_over has its own refusal further down should_fire; the floor read
    # stays out of it so the two reasons never get confused in the logs.
    game = _game()
    game.game_over = True
    game.last_addressee_judgment = _judgment(
        lily_addressee_classifier.CLASS_SIDE_CHATTER, time.time()
    )
    assert game._floor_yields_recovery() is None
    game.arm_cut_recovery("...that's the night")
    assert game._cut_recovery_should_fire(game._cut_recovery_token) is False


def test_resume_claims_no_speech_act_and_arms_no_retry_ladder():
    # Keyless by design: a refused resume is silence, not a queued retry.
    game = _game()
    armed: list = []
    game._arm_stale_claim_watchdog = lambda *a, **k: armed.append(a)
    assert game.trigger_cut_recovery() is True
    assert armed == []
    assert game.say_registry.state("cut_recovery") is None


# ---------------------------------------------------------------------------
# Source-order / structure pins
# ---------------------------------------------------------------------------

def test_cut_recovery_dispatches_through_the_gate_not_around_it():
    body = inspect.getsource(
        lily_speech_delivery.LilySpeechDeliveryMixin.trigger_cut_recovery
    )
    assert "self.gated_say(" in body
    assert "self.instructed_reply(" not in body


def test_gated_say_remains_the_only_dispatch_funnel_in_the_mixin():
    # The chain-F regression pin: exactly ONE instructed_reply call site in
    # the speech-delivery mixin, and it is the one inside gated_say.
    module = inspect.getsource(lily_speech_delivery)
    assert module.count("self.instructed_reply(") == 1
    assert (
        "self.instructed_reply("
        in inspect.getsource(
            lily_speech_delivery.LilySpeechDeliveryMixin.gated_say
        )
    )


def test_floor_is_read_before_the_fire_decision_returns_true():
    body = inspect.getsource(
        lily_speech_delivery.LilySpeechDeliveryMixin._cut_recovery_should_fire
    )
    floor = body.index("self._floor_yields_recovery()")
    assert floor < body.index("return not getattr(self, \"_adjudicating\"")
    assert "self.floor_state()" in inspect.getsource(
        lily_speech_delivery.LilySpeechDeliveryMixin._floor_yields_recovery
    )


def test_every_refusal_path_runs_the_shared_stand_down():
    # Both stand-down callers must go through the one helper, so the arm
    # clear and the address-debt release can never diverge per path.
    fire = inspect.getsource(
        lily_speech_delivery.LilySpeechDeliveryMixin._cut_recovery_should_fire
    )
    trigger = inspect.getsource(
        lily_speech_delivery.LilySpeechDeliveryMixin.trigger_cut_recovery
    )
    assert "self._stand_down_cut_recovery(" in fire
    assert "self._stand_down_cut_recovery(" in trigger
    stand_down = inspect.getsource(
        lily_speech_delivery.LilySpeechDeliveryMixin._stand_down_cut_recovery
    )
    assert "_reair_gate_armed" in stand_down
    assert "_awaiting_address_since" in stand_down
    # The arm is CLEARED, never consumed, on a stand-down.
    assert "take_reair_dispatch" not in stand_down


def test_floor_state_adds_no_stored_state():
    # NO NEW LAYERS: floor_state is a pure derivation. It must not assign.
    body = inspect.getsource(LilyGame.floor_state)
    assert "self._floor" not in body
    for line in body.splitlines():
        stripped = line.strip()
        assert not (stripped.startswith("self.") and "=" in stripped)
