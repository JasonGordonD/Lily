"""WO-LILY-PATCH-002 A4 + A5 — the hold state and the STOP primitive,
from the ~21:55 solo session fixtures.

A4 (solo vamping): five turns in ~30s against an explicit refusal;
"take your time" followed by more talking 5s later; a full question
airing mid-hold. Fix — the hold state binds EVERY dispatch lane: one
acknowledgment then yield; her own "take your time" binds her; the
delivery lane checks hold state at dispatch.

A5 (STOP primitive): "Lily. Lily. Stop!" answered by a re-aired
question — the runaway-agent brake. An addressed stop (bare stop in
solo) halts at the dispatch gate, cancels dispatches, enters the hold,
one acknowledgment.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_config
import lily_say_gate
import lily_scorekeeper
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
    game.sk = LilyScorekeeper("patch002-fixture")
    game.say_registry = __import__("lily_say_gate").SpeechActRegistry()
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
    game.game_started = True
    game.game_over = False
    game.publish_attributes_nowait = lambda: None
    game.start_prefetch = lambda: None
    game.instructed_replies = []

    def _reply(text):
        game.instructed_replies.append(text)
        h = _Handle(f"sp{len(game.instructed_replies)}")
        return h

    game.instructed_reply = _reply
    return game


# -- A5: STOP detection --------------------------------------------------------


def test_addressed_stop_fires_garble_tolerant():
    for txt in ["Lily, stop!", "Lily. Lily. Stop!", "lily staap",
                "stop, Lily", "Lilly, stahp"]:
        assert lily_scorekeeper.lily_detect_stop(txt) is True, txt


def test_bare_stop_solo_only():
    assert lily_scorekeeper.lily_detect_stop("stop", solo=True) is True
    assert lily_scorekeeper.lily_detect_stop("stop", solo=False) is False


def test_stop_word_bounded_and_negation_guarded():
    assert lily_scorekeeper.lily_detect_stop("stopwatch", solo=True) is False
    assert lily_scorekeeper.lily_detect_stop("unstoppable", solo=True) is False
    assert lily_scorekeeper.lily_detect_stop("don't stop, Lily") is False


def test_stop_primitive_halts_cancels_and_holds():
    game = _make_game()
    # A live delivery is airing.
    game.say_registry.claim("q_3_delivery", owner="live1")
    game._speech_handles["live1"] = _Handle("live1")
    game.handle_stop_primitive("Lily, stop!")
    # Playout cancelled, claim released, session interrupted, hold entered.
    assert game._speech_handles["live1"].interrupts == [True]
    assert game.say_registry.state("q_3_delivery") is None
    assert game.session.interrupted is True
    assert game._hold_active is True
    assert game.game_delivery_stopped() is True
    # Exactly one short acknowledgment aired (the stop ack is hold-exempt).
    assert len(game.instructed_replies) == 1
    assert "stop" in game.instructed_replies[0].lower()


# -- A4: the hold binds every lane ---------------------------------------------


def test_hold_blocks_conversation_and_delivery_but_not_release():
    game = _make_game()
    game.enter_hold(reason="test")
    assert game.hold_blocks_dispatch("question_delivery", "post_reveal") is True
    assert game.hold_blocks_dispatch("banter", "organic") is True
    assert game.hold_blocks_dispatch("stop_ack", "stop_primitive") is False
    assert game.hold_blocks_dispatch("release", "hold_release") is False


def test_gated_say_suppressed_while_held():
    game = _make_game()
    game.enter_hold(reason="test")
    assert game.gated_say(None, "banter", "chat", source="organic") is False
    assert game.instructed_replies == []
    # An exempt source still speaks.
    assert game.gated_say(None, "ack", "ok", source="hold_ack") is True
    assert len(game.instructed_replies) == 1


def test_user_speech_releases_hold_via_release_method():
    game = _make_game()
    game.enter_hold(reason="test")
    assert game.release_hold(reason="user_speech") is True
    assert game._hold_active is False
    assert game.gated_say(None, "banter", "chat", source="organic") is True


def test_sticky_stop_survives_conversational_hold_release():
    game = _make_game()
    game.handle_stop_primitive("Stop the quiz.")
    assert game.release_hold(reason="user_speech") is True

    assert game._hold_active is False
    assert game.game_delivery_stopped() is True
    # Conversation may continue; the game plane may not.
    assert game.gated_say(None, "banter", "chat", source="organic") is True
    assert (
        game.gated_say(
            None, "question_delivery", "Question?", source="post_reveal"
        )
        is False
    )


def test_stop_clears_all_question_delivery_state():
    game = _make_game()
    game.sk.question_number = 4
    game.armed_question = {"id": "q4", "prompt": "Frankenstein?"}
    game.next_question = {"id": "q5", "prompt": "Rosetta Stone?"}
    game.sk.current_question = dict(game.armed_question)
    game.sk.open_answer_window(
        duration=30.0, question_id="q4", question_index=4
    )
    game.sk.answer_candidates["Playing"] = {"text": "meta"}
    game._pending_delivery_qnum = 4
    game._active_delivery_qnum = 4
    game._delivery_speech_acts = {"s4": "question_delivery"}
    game._pre_window_segments = [{"text": "old meta"}]
    game._recent_finals = [(1.0, {"text": "old meta"})]

    game.handle_stop_primitive("I don't want to play anymore.")

    assert game.armed_question is None
    assert game.next_question is None
    assert game.sk.current_question is None
    assert game.sk.answer_window_open is False
    assert game.sk.answer_candidates == {}
    assert game._pending_delivery_qnum is None
    assert game._active_delivery_qnum is None
    assert game._delivery_speech_acts == {}
    assert game._pre_window_segments == []
    assert game._recent_finals == []
    assert game.next_question_ready() is False
    assert game.arm_next_question() is False


def test_only_explicit_resume_clears_sticky_stop():
    game = _make_game()
    starts = []
    game.start_prefetch = lambda: starts.append("prefetch")
    game.handle_stop_primitive("Stop the game.")
    game.release_hold(reason="user_speech")

    assert not lily_scorekeeper.lily_detect_resume_game("okay")
    assert not lily_scorekeeper.lily_detect_resume_game(
        "I don't want to continue."
    )
    assert game.game_delivery_stopped() is True

    assert lily_scorekeeper.lily_detect_resume_game("Continue the game")
    assert game.resume_game_delivery(reason="test") is True
    assert game.game_delivery_stopped() is False
    assert starts == ["prefetch"]


def test_repeated_stop_does_not_repeat_ack():
    game = _make_game()
    game.handle_stop_primitive("Stop the quiz.")
    game.handle_stop_primitive("I don't want to play anymore.")

    assert len(game.instructed_replies) == 1


def test_quit_phrases_are_stop_commands():
    for text in [
        "Stop the quiz.",
        "I don't want to play anymore.",
        "End the game.",
        "I'm done playing.",
    ]:
        assert lily_scorekeeper.lily_detect_stop(text, solo=True), text


def test_sticky_stop_blocks_expect_claim_and_pre_window_buffer():
    game = _make_game()
    game.sk.question_number = 6
    game.armed_question = {"id": "q6", "prompt": "Who was Caesar?"}
    game._delivery_stop_sticky = True

    game.expect_delivery()
    delivery = game.register_delivery_claim(
        "Who was Caesar?", speech_id="s6"
    )
    game.buffer_pre_window_answer(
        {
            "text": "This is meta speech, not an answer.",
            "speaker_label": "S1",
            "is_final": True,
        }
    )

    assert game._pending_delivery_qnum is None
    assert delivery is None
    assert game._pre_window_segments == []


def test_self_wait_promise_phrase_detected():
    assert lily_say_gate.lily_self_hold_phrase("Take your time, no rush!")
    assert lily_say_gate.lily_self_hold_phrase("I'll wait for you.")
    assert lily_say_gate.lily_self_hold_phrase("Whenever you're ready.")
    assert not lily_say_gate.lily_self_hold_phrase("Here's your next question.")


def test_hold_timeout_releases():
    game = _make_game()
    game.enter_hold(reason="test")
    assert game.hold_timed_out(now=game._hold_since + 1.0) is False
    late = game._hold_since + lily_config.hold_timeout_seconds() + 1.0
    assert game.hold_timed_out(now=late) is True


# -- A4a: semantic paraphrase lint ---------------------------------------------


def test_paraphrase_lint_catches_reassurance_storm():
    prev = ["No rush at all, take all the time you need to think it over."]
    # A semantically near-identical restatement (high content-word overlap).
    flag = lily_say_gate.lily_paraphrase_repeat_flag(
        "Take all the time you need — no rush to think it over at all.",
        prev, threshold=0.6,
    )
    assert flag == "paraphrase"


def test_paraphrase_lint_spares_fresh_content():
    prev = ["No rush at all, take your time."]
    assert lily_say_gate.lily_paraphrase_repeat_flag(
        "Round two, question one: which planet has the most moons?",
        prev, threshold=0.6,
    ) is None


# -- W2 (WO-LILY-HOTFIX-009): the hold binds the DELIVERY lane too ------------
#
# Live lily-5E3036, q_6 (kb_519): the player has the floor held and Lily is
# saying "still stopped until you say go", yet a full question aired anyway —
# transcript 05:40:11, trace `LILY_SAY | act=question_delivery |
# key=q_6_delivery | source=tts_node | trigger=structural`. That lane is
# register_delivery_claim (tts_node), which honoured the sticky-STOP latch
# but NOT the plain hold state: a question reaching the air through the
# organic/structural delivery lane never passes the gated_say hold gate the
# code-dispatch lanes do. This is the 006 "two lanes, opposite assertions,
# seconds apart" shape, on the hold/delivery pair rather than the
# reveal/verdict transition 006 serialized. The real armed question below is
# the kb_519 fixture verbatim from the session rows.

_Q6_FREUDIAN = (
    "Operating entirely on the pleasure principle, the primal and impulsive "
    "part of the Freudian psyche is what two-letter concept?"
)


def _armed_q6_game():
    game = _make_game()
    game.sk.question_number = 6
    game.armed_question = {
        "id": "kb_519",
        "prompt": _Q6_FREUDIAN,
        "acceptable_answers": ["id", "es"],
    }
    game.sk.current_question = dict(game.armed_question)
    return game


def test_hold_suppresses_delivery_lane_freudian_fixture():
    """The primary needle. A PLAIN hold (self-wait-promise / question-
    unanswered — not the sticky STOP) is active, the structural delivery is
    armed, and the outbound turn performs the armed question. On base this
    lane claims and airs q_6_delivery ("claimed_structural"); the hold must
    suppress it instead — physically silent, not deferred."""
    game = _armed_q6_game()
    game.enter_hold(reason="self_wait_promise")
    assert game._hold_active is True
    assert game._delivery_stop_sticky is False  # a hold, not the STOP latch
    game._pending_delivery_qnum = 6  # the structural nudge armed the delivery

    result = game.register_delivery_claim(_Q6_FREUDIAN, speech_id="s6")

    assert result == "held"
    # The delivery never claimed, so no window opens off it and nothing airs.
    assert game.say_registry.state("q_6_delivery") is None


def test_hold_does_not_suppress_the_owed_conversational_reply():
    """No over-reach: while held she still owes the player an answer. A
    conversational turn that is NOT the armed question is not a delivery and
    speaks normally (returns None), exactly as it does with no hold."""
    game = _armed_q6_game()
    game.enter_hold(reason="self_wait_promise")
    result = game.register_delivery_claim(
        "Still stopped until you say go.", speech_id="sc"
    )
    assert result is None
    assert game.say_registry.state("q_6_delivery") is None


def test_hold_release_re_enables_the_delivery_lane():
    """The hold is not a mute button — once the player says go and the hold
    releases, the same delivery airs through the same lane."""
    game = _armed_q6_game()
    game.enter_hold(reason="self_wait_promise")
    game._pending_delivery_qnum = 6
    assert game.register_delivery_claim(_Q6_FREUDIAN, speech_id="s6") == "held"

    assert game.release_hold(reason="user_speech") is True
    game._pending_delivery_qnum = 6
    result = game.register_delivery_claim(_Q6_FREUDIAN, speech_id="s6b")
    assert result == "claimed_structural"
    assert game.say_registry.state("q_6_delivery") is not None


def test_stop_primitive_delivery_lane_behaviour_unchanged():
    """The STOP primitive's existing behaviour is untouched: under the sticky
    STOP latch the delivery lane returns None (the pre-existing contract —
    conversation may continue, the game plane is frozen at gated_say), which
    is the same value it returned before W2."""
    game = _armed_q6_game()
    game.handle_stop_primitive("Lily, stop!")
    assert game.game_delivery_stopped() is True
    game._pending_delivery_qnum = 6
    result = game.register_delivery_claim(_Q6_FREUDIAN, speech_id="s6")
    assert result is None
    assert game.say_registry.state("q_6_delivery") is None
