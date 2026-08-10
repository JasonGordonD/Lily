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


# -- W8 (WO-LILY-HOTFIX-009): the stop DETECTS but did not ROUTE --------------
#
# Live lily-5E3036, 05:39:10: Rami shouts the real utterance below. It never
# routed to handle_stop_primitive — zero LILY_STOP/LILY_HOLD in the whole
# trace, LILY_SPINE hold=clear throughout — because a phantom second player
# ("Rummy", an STT name-capture artifact) made roster_size() read 2, so
# solo=False, and a bare stop counts only in solo. Lily then narrated three
# "Stopped…" confirmations of a hold that mechanically did NOT exist, and the
# q_6 Freudian question aired at 05:40:11 with no hold to block it. W8 routes
# the emphatic-repetition register regardless of roster, and backs any
# stopped-state narration with an actual hold.

# The 05:39:10 utterance, verbatim from lily_transcripts (speaker Rami).
_REAL_STOP_UTTERANCE = (
    "Stop stop stop stop stop stop stop, I. This is unfair. "
    "Are you not listening?"
)

# The four real confabulated hold-narration turns (Lily, source rows
# 05:39:26 / 05:39:49 / 05:40:00 / 05:40:17).
_REAL_HOLD_NARRATIONS = [
    "[soft] Stopped. I'm listening. …[cut off]",
    "[soft] Still stopped. You say when.",
    "[soft] Yes. I heard you. Stopped until you say go.",
    "[soft] Still stopped until you say go. …[cut off]",
]


def _polluted_roster_game():
    """The live roster: one real player (Rami) plus the phantom 'Rummy', so
    roster_size() == 2 and solo=False — exactly what suppressed the stop."""
    game = _armed_q6_game()
    game.sk.players = {"rami": {"score": 2}, "rummy": {"score": 0}}
    assert game.sk.roster_size() == 2
    return game


def test_emphatic_repetition_stop_is_roster_independent():
    """The root-cause needle. The real shouted utterance, and a bare
    'stop stop stop', fire even when solo=False — the emphatic-repetition
    register removes the who-is-being-addressed ambiguity the solo gate
    guards. A single bare stop still needs solo; two is not yet emphatic;
    the word-bound/negation guards still hold."""
    assert lily_scorekeeper.lily_detect_stop(
        _REAL_STOP_UTTERANCE, solo=False
    ) is True
    assert lily_scorekeeper.lily_detect_stop("stop stop stop", solo=False)
    # Unchanged: a single bare stop is still solo-only; two is not emphatic.
    assert lily_scorekeeper.lily_detect_stop("stop", solo=False) is False
    assert lily_scorekeeper.lily_detect_stop("stop stop", solo=False) is False
    assert lily_scorekeeper.lily_detect_stop("stop", solo=True) is True
    # Still word-bounded and negation-guarded.
    assert lily_scorekeeper.lily_detect_stop(
        "stopwatch stopwatch stopwatch", solo=False
    ) is False
    assert lily_scorekeeper.lily_detect_stop(
        "don't stop stop stop", solo=False
    ) is False


def test_real_stop_utterance_routes_to_hold_with_polluted_roster():
    """The primary end-to-end needle, on the production consult path. With the
    live polluted roster (solo=False), the real 05:39:10 utterance enters the
    hold MECHANICALLY: maybe_route_stop returns True, the hold is active, the
    sticky STOP latch is set, and exactly one ack aired. On base this routed
    nothing (detection returned False for a bare stop in a non-solo room)."""
    game = _polluted_roster_game()
    handled = game.maybe_route_stop(_REAL_STOP_UTTERANCE)
    assert handled is True
    assert game._hold_active is True
    assert game.game_delivery_stopped() is True
    assert len(game.instructed_replies) == 1
    assert "stop" in game.instructed_replies[0].lower()


def test_solo_and_addressed_stop_routing_unchanged():
    """No regression on the paths that routed before W8: a solo bare stop
    still routes, an addressed stop routes regardless of roster, and a lone
    bare stop in a genuine multi-player room still does not."""
    solo = _armed_q6_game()
    solo.sk.players = {"rami": {"score": 0}}
    assert solo.maybe_route_stop("stop") is True
    assert solo._hold_active is True

    addressed = _polluted_roster_game()
    assert addressed.maybe_route_stop("Lily, stop!") is True
    assert addressed._hold_active is True

    lone = _polluted_roster_game()
    assert lone.maybe_route_stop("stop") is False
    assert lone._hold_active is False


def test_hold_narration_without_state_enters_hold():
    """Honesty needle. Each real confabulated line, spoken with no hold and
    no sticky STOP, enters the hold to back the claim — a narrated stopped
    state can no longer exist without the mechanical state behind it."""
    for line in _REAL_HOLD_NARRATIONS:
        game = _armed_q6_game()
        assert game._hold_active is False
        backed = game.back_hold_narration(line)
        assert backed is True, line
        assert game._hold_active is True, line


def test_hold_narration_noop_when_already_backed():
    """No double-action / no over-reach: already-held (the legitimate
    stop-ack path) and sticky-STOP (the claim is backed by the latch) are
    both no-ops, and a normal turn never enters a hold."""
    held = _armed_q6_game()
    held.enter_hold(reason="stop_primitive")
    assert held.back_hold_narration("Stopped. I'm listening.") is False

    sticky = _armed_q6_game()
    sticky._delivery_stop_sticky = True
    assert sticky.back_hold_narration("Still stopped until you say go.") is False
    assert sticky._hold_active is False

    normal = _armed_q6_game()
    assert normal.back_hold_narration("Here's your next question.") is False
    assert normal._hold_active is False


def test_confabulated_narration_path_suppresses_freudian_delivery():
    """The lived 05:40 path is unreproducible end-to-end (part 2 + W2). Lily
    narrates 'Stopped until you say go' with no hold; backing it enters the
    hold; the very next q_6 delivery through the tts_node lane is then
    suppressed ('held') instead of airing as it did at 05:40:11."""
    game = _armed_q6_game()
    assert game.back_hold_narration(
        "Yes. I heard you. Stopped until you say go."
    ) is True
    assert game._hold_active is True

    game._pending_delivery_qnum = 6
    result = game.register_delivery_claim(_Q6_FREUDIAN, speech_id="s6")
    assert result == "held"
    assert game.say_registry.state("q_6_delivery") is None


def test_routed_stop_then_freudian_delivery_suppressed():
    """The other end-to-end path (part 1 + W2): the emphatic stop routes with
    the polluted roster, enters the hold, and the armed q_6 delivery is then
    suppressed by the same delivery-lane gate."""
    game = _polluted_roster_game()
    assert game.maybe_route_stop(_REAL_STOP_UTTERANCE) is True
    game._pending_delivery_qnum = 6
    result = game.register_delivery_claim(_Q6_FREUDIAN, speech_id="s6")
    # Sticky STOP (set by the primitive) already freezes this lane at None;
    # either way the Freudian question does not air.
    assert result in (None, "held")
    assert game.say_registry.state("q_6_delivery") is None


# -- W8 residual (W2 review SPEC-1 i/ii): prose payload & sub-threshold --------
# paraphrase slip the delivery-lane gate, which is coextensive with armed-
# question detection. Coordinator ruling: the held state is made EXPLICIT to
# the LLM/prompt surface so the model does not narrate any game payload while
# held; the mechanical gate stays the backstop for the armed-question form. The
# state block ([GAME STATE]) is what llm_node injects as a system message
# (`_apply_context_blocks`, tested in test_context_blocks.py), so asserting the
# directive is in the stable block asserts the LLM is instructed against it.

def _with_state_block_attrs(game):
    """Fill the few state-block attributes _make_game omits so
    build_state_block_split runs (mirrors test_preemptive_volatile_split)."""
    game.session_started_at = 0.0
    game.availability_flags = None
    game.promoted_categories = []
    game.memory_block = ""
    game._pending_unbound_award = None
    game.eliminated = []
    game.forget_state = None
    game.prefs = {}
    return game


def test_plain_hold_state_surface_forbids_game_payloads():
    """Residual (i) prevention needle. A plain hold — here the backed
    stopped-state narration, but equally a self-wait-promise or P10 — makes
    the prompt surface carry an explicit directive that no game payload
    (ask/arm/reveal/score/nudge/steal), in prose OR as the question, may air
    while held. On base the [GAME STATE] block has no such directive for a
    plain hold, so an organic prose reveal/verdict slips both the prompt and
    the armed-question-only delivery gate."""
    game = _with_state_block_attrs(_armed_q6_game())
    assert game.back_hold_narration("Stopped. I'm listening.") is True
    assert game._hold_active is True and game._delivery_stop_sticky is False
    stable, _ = game.build_state_block_split()
    assert "held: you have PAUSED" in stable
    # The directive enumerates the forbidden payloads, in prose or as the Q.
    for token in ("reveal", "score", "steal", "in prose or as"):
        assert token in stable, token
    # The stronger sticky-STOP directive does not fire (this is a plain hold).
    assert "game_delivery: STOPPED" not in stable


def test_sticky_stop_state_surface_unchanged_and_no_double_directive():
    """The sticky STOP path (handle_stop_primitive) keeps its own stronger
    STOPPED directive and does NOT additionally emit the plain-hold line —
    the two are mutually exclusive, no redundant double-directive."""
    game = _with_state_block_attrs(_armed_q6_game())
    game.handle_stop_primitive("Lily, stop!")
    stable, _ = game.build_state_block_split()
    assert "game_delivery: STOPPED" in stable
    assert "held: you have PAUSED" not in stable


def test_no_hold_state_surface_has_no_stop_directive():
    """No hold, no sticky → neither directive. The held directive never
    leaks into ordinary play."""
    game = _with_state_block_attrs(_armed_q6_game())
    stable, _ = game.build_state_block_split()
    assert "held: you have PAUSED" not in stable
    assert "game_delivery: STOPPED" not in stable


def test_hold_release_clears_the_payload_directive():
    """The directive is bound to live hold state, not latched: once the hold
    releases (the player's go), the prompt surface no longer forbids payloads,
    so the game resumes."""
    game = _with_state_block_attrs(_armed_q6_game())
    game.back_hold_narration("Still stopped until you say go.")
    assert "held: you have PAUSED" in game.build_state_block_split()[0]
    game.release_hold(reason="user_go")
    assert game._hold_active is False
    assert "held: you have PAUSED" not in game.build_state_block_split()[0]


# -- W8 review Finding 3: recap/scorekeeping patter must never enter a hold ----
# The bare `\bstill\b` cue tripped whenever a past-tense "stopped" shared a turn
# with the very common word "still" ("still tied", "still your turn", "still on
# question four"). Because back_hold_narration runs before register_delivery_claim,
# that false hold would return a legitimate same-turn delivery "held". Coordinator
# ruling: the brake may over-fire recoverably but must NEVER suppress a real
# payload — the cue is bound to the "still stopped" adjacency.

_RECAP_PATTER_NOT_A_HOLD = [
    "You stopped Rami cold — still tied at two.",
    "You stopped for a sec, but we're still on question four.",
    "We stopped the clock, still your turn.",
    "recap: you stopped, he answered, still nobody's ahead.",
]


def test_recap_still_patter_does_not_detect_or_enter_hold():
    """Finding 3 needle. Recap/scorekeeping lines that pair a past-tense
    'stopped' with 'still' are NOT the stop-ack register and must not enter a
    hold; the four genuine confabulations still do (regression guard)."""
    for line in _RECAP_PATTER_NOT_A_HOLD:
        assert lily_scorekeeper.lily_detect_hold_narration(line) is False, line
        game = _armed_q6_game()
        assert game.back_hold_narration(line) is False, line
        assert game._hold_active is False, line
    # The narration register is unchanged — each real line still fires.
    for line in _REAL_HOLD_NARRATIONS:
        assert lily_scorekeeper.lily_detect_hold_narration(line) is True, line


def test_recap_still_patter_never_suppresses_a_real_question():
    """The suppression vector this closes: a recap mentioning 'still tied at
    two' shares a turn with a real question. The recap must not enter a hold,
    so the armed q_6 delivery is NOT returned 'held' — the legitimate payload
    airs. The mirror of test_confabulated_narration_path_suppresses_freudian_
    delivery, proving the latch fires only on a genuine stopped-state claim."""
    game = _armed_q6_game()
    assert game.back_hold_narration("You stopped him cold — still tied at two.") is False
    assert game._hold_active is False

    game._pending_delivery_qnum = 6
    result = game.register_delivery_claim(_Q6_FREUDIAN, speech_id="s6")
    assert result != "held", result
    assert game._hold_active is False
