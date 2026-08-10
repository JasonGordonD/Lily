"""WO-LILY-HOTFIX-010 V4 — the hold binds the greeting/session-entry lane at
DISPATCH time, and exactly ONE stop acknowledgment airs.

Live fixture (lily, 11:08:18–11:08:28): a player shouts stop across the
opening roster+menu; the pre-composed greeting keeps playing, then the stop
is acknowledged TWICE ~70ms apart — two lanes each discovering the stop, one
the mechanical brake, one an outbound turn that narrated a stopped state.

These lock the dispatch-layer guarantees:
  * a stop mid-greeting terminates the greeting (claim released, speech
    interrupted) and CANCELS any queued greeting beat — a re-dispatch while
    held is refused, never deferred-and-fired-later;
  * the mechanical brake reads the SHARED hold state, so when the narration
    lane has already acknowledged the stop the brake adds no second ack;
  * a prior NON-stop hold (an unanswered question) still lets the stop ack
    through — the suppression is scoped to a stop-class hold only.

Plus the two low-severity greeting-composition edges folded in from the
V1+V3 review: (A) a cut greeting must not double-disclose what's-new, and
(B) a pending device candidate must not compose the first-time/claimed-
returner framing on top of the "verify the voice first" device beat.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_capabilities
import lily_say_gate
from lily_agent import LilyGame
from lily_scorekeeper import LilyScorekeeper


class _Handle:
    def __init__(self, sid):
        self.id = sid
        self.interrupts = []

    def interrupt(self, *, force=False):
        self.interrupts.append(force)
        return self


class _FakeSession:
    def __init__(self):
        self.interrupted = False

    def interrupt(self):
        self.interrupted = True


def _make_game():
    game = LilyGame.__new__(LilyGame)
    game.sk = LilyScorekeeper("v4-fixture")
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game.session = _FakeSession()
    game._hold_active = False
    game._hold_since = 0.0
    game._hold_reason = None
    game._delivery_stop_sticky = False
    game._speech_handles = {}
    game._suppressed_speech_ids = set()
    game._armed_speech_misses = 0
    game._undelivered_ticks = 0
    game._undelivered_refires = 0
    game._supply_stall_ticks = 0
    game._prefetch_stall_ticks = 0
    game._pending_delivery_qnum = None
    game._active_delivery_qnum = None
    game._active_delivery_started_at = None
    game._active_delivery_ended_at = None
    game._delivery_speech_acts = {}
    game._pre_window_segments = []
    game._recent_finals = []
    game._pending_reveal_event = None
    game._prefetch_task = None
    game._window_timer = None
    game._bed_handle = None
    game._steal_window = False
    game._phase_hold = None
    game._mc_delivery_qnum = None
    game._mc_delivery_started_at = None
    game.armed_question = None
    game.next_question = None
    game.pending_clarify = {}
    game.used_prompts = []
    game._burned_question_ids = set()
    game._burned_question_hashes = set()
    game.supabase = None
    game.game_started = False
    game.game_over = False
    game.publish_attributes_nowait = lambda: None
    game.start_prefetch = lambda: None
    game.instructed_replies = []

    def _reply(text):
        game.instructed_replies.append(text)
        return _Handle(f"sp{len(game.instructed_replies)}")

    game.instructed_reply = _reply
    return game


# -- item 1: the hold binds the greeting/session-entry lane -------------------


def test_stop_mid_greeting_terminates_and_cancels_the_beat():
    game = _make_game()
    # The opener is airing: session_greet claimed PENDING, its speech tracked.
    game.say_registry.claim("session_greet", owner="greet1")
    game._speech_handles["greet1"] = _Handle("greet1")

    game.handle_stop_primitive("stop")

    # The greeting terminates IMMEDIATELY: speech interrupted, claim released.
    assert game._speech_handles["greet1"].interrupts == [True]
    assert game.say_registry.state("session_greet") is None
    assert game._hold_active is True

    # A queued greeting beat is CANCELLED, not deferred-and-fired-later: a
    # re-dispatch of the opener while held is refused at the dispatch gate.
    for source in ("on_enter", "entrypoint", "empty_stop_lobby_recover"):
        assert (
            game.gated_say(
                "session_greet", "greet", "welcome back", source=source
            )
            is False
        ), source
    # Nothing but the single stop acknowledgment aired.
    assert len(game.instructed_replies) == 1
    assert "stop" in game.instructed_replies[0].lower()


# -- item 2: exactly ONE stop acknowledgment airs -----------------------------


def test_double_ack_narration_lane_first_suppresses_mechanical_ack():
    game = _make_game()
    # The narration lane wins first: an outbound turn narrated a stopped state
    # (its own words are the acknowledgment) and back_hold_narration entered
    # the hold to back the claim.
    assert game.back_hold_narration("Stopped. I'm listening.") is True
    assert game._hold_active is True
    assert game._hold_reason == "narrated_stop"

    # The mechanical brake reaches the same stop 70ms later. It must NOT add a
    # second acknowledgment — the 11:08:28 double-ack is unreproducible.
    game.handle_stop_primitive("stop")
    assert game.instructed_replies == []
    assert game.game_delivery_stopped() is True


def test_mechanical_first_then_narration_stays_single_ack():
    game = _make_game()
    game.handle_stop_primitive("stop")
    assert len(game.instructed_replies) == 1
    # The mechanical ack itself narrates a stop; back_hold_narration is a
    # no-op because the hold is already active — no third lane, no re-ack.
    assert game.back_hold_narration("Stopped. Say the word.") is False
    assert len(game.instructed_replies) == 1


def test_prior_non_stop_hold_does_not_swallow_the_stop_ack():
    game = _make_game()
    # A hold from an UNANSWERED QUESTION is active — not a stop. The player
    # then says stop; the stop still earns its own acknowledgment.
    game.enter_hold(reason="question_unanswered")
    game.handle_stop_primitive("stop")
    assert len(game.instructed_replies) == 1
    assert "stop" in game.instructed_replies[0].lower()


def test_repeated_stop_never_double_acks():
    game = _make_game()
    game.handle_stop_primitive("stop")
    game.handle_stop_primitive("stop")
    assert len(game.instructed_replies) == 1


# -- edge (A): a cut greeting must not double-disclose what's-new -------------


def test_whats_new_emits_once_across_carriers_when_greet_never_confirms():
    game = _make_game()
    game.group_id = "grp"
    # Returning table, feature stamp lagged; the greet is cut, so its confirm
    # (which advances the durable stamp) never runs.
    game.prefs = {"last_seen_feature_version": 1}
    game.memory_total_games = 3

    # Carrier one: the late-recognition beat composes the delta.
    first = game.whats_new_instruction()
    assert first, "the first carrier should disclose the delta"
    delta = lily_capabilities.lily_whats_new(1)
    assert delta and delta[0] in first

    # Carrier two: the game-start ride-along. It must NOT disclose again even
    # though the durable stamp is still lagged (greet never confirmed).
    assert game.prefs["last_seen_feature_version"] == 1
    assert game.whats_new_instruction() == ""


# -- edge (B): device candidate vs the deferred first-time framing -----------


def test_device_candidate_suppresses_first_time_framing_after_utterance():
    game = _make_game()
    game.group_id = "grp"
    game.memory_block = ""          # no verified present-table memory
    game.prefs = {}
    game.memory_total_games = 0
    game._first_human_utterance_seen = True
    game.device_candidate_group_id = "dev-123"

    text = game.greeting_instructions()
    # The device beat owns the turn — verify the voice first.
    assert "DEVICE looks familiar" in text
    # The contradictory first-time / claimed-returner framing must be absent.
    assert "first time" not in text.lower()
    assert "CLAIMED RETURNER" not in text


def test_no_device_candidate_still_composes_first_time_framing():
    # Regression anchor for (B): with NO device candidate the deferred beat is
    # exactly what SHOULD compose — the gate removes it only for the device
    # case, it does not delete the branch.
    game = _make_game()
    game.group_id = "grp"
    game.memory_block = ""
    game.prefs = {}
    game.memory_total_games = 0
    game._first_human_utterance_seen = True
    game.device_candidate_group_id = None
    game.identity_probe_outstanding = lambda: False

    text = game.greeting_instructions()
    assert "first time" in text.lower()
    assert "CLAIMED RETURNER" in text
