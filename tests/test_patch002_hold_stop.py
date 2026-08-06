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
    game._speech_handles = {}
    game._suppressed_speech_ids = set()
    game._armed_speech_misses = 0
    game._undelivered_ticks = 0
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
