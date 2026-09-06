"""WO-LILY-ADDRESSED-001 (B9) — progression yields to the table.

Operator text, VERBATIM (the UNIVERSAL RULE):

  Progression yields to the table. Whenever a player addresses her — a
  question, a comment, a joke, a correction, a request, anything directed
  at her rather than at the game — the game holds, she responds in kind,
  and it resumes only when the table gives it back. Answer-shaped
  utterances into an open window get scored. Everything else gets a host.

And B9's own contract for the question case, verbatim:

  A direct question to the host earns a real answer, and the game waits
  for it. Up to three sentences — enough to actually explain who Kinsey
  was, not enough to become a lecture. Once she's answered, she offers
  the way back rather than seizing it: '…anyway — ready for the next
  one?' If the table wants more, they ask, and she gives another three.
  The question is the invitation; the offer is the exit.

Live evidence: session lily-C47CD4-690cc8fd 14:26:28Z — "Who's Alfred
Kingsley, anyways?" was answered 24 s later FUSED with "Next one." and the
question re-read; Q3 had dispatched the instant the reveal finished.

Every fixture here drives the production objects (a real LilyGame over the
handle-returning fake session from test_airgate_001, finals through the
real transcript path, the response through the real say pipeline). None
asserts on source text; the one prompt check is labelled a PIN.
"""

import logging
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_addressee_classifier  # noqa: E402
import lily_say_gate  # noqa: E402
import lily_scorekeeper  # noqa: E402
from lily_agent import (  # noqa: E402
    LILY_SYSTEM_PROMPT, SpeechTurn, run_say_pipeline,
)
from test_airgate_001 import _game as _airgate_game, _pipeline_agent  # noqa: E402
from test_bind_dispute_p0 import _final, _run  # noqa: E402
from test_composition_followup_w6 import LIVE_PAUSE  # noqa: E402

Q_MC = {
    "id": "kb_planets",
    "prompt": "Which planet is known as the Red Planet?",
    "canonical_answer": "Mars",
    "acceptable_answers": ["mars"],
    "choices": ["Earth", "Mars", "Venus", "Jupiter"],
    "category": "science",
}
Q_NEXT = {
    "id": "kb_wilde2",
    "prompt": "Who wrote The Importance of Being Earnest?",
    "canonical_answer": "Oscar Wilde",
    "acceptable_answers": ["oscar wilde", "wilde"],
    "category": "literature",
}

OFFER = "…anyway — ready for the next one?"

LIVE_KINSEY = "Who's Alfred Kinsey, anyways?"
LIVE_NAME = "Your name is spelled wrong on the page"
LIVE_SLOW = "Why are you so slow?"
LIVE_FLOOR = "We're not talking to you, that was side banter"
LIVE_DIAMOND = "I said diamond"
LIVE_BEAT = "I hate that word, beat"
LIVE_UNFAIR = "That's unfair"

FIVE_SENTENCES = (
    "Alfred Kinsey was an American biologist. He started out studying "
    "gall wasps, thousands of them. Then he turned to human sexuality "
    "and published the Kinsey Reports in the late forties and fifties. "
    "They were bestsellers and scandals at once. His institute still "
    "exists at Indiana University."
)
THREE_SENTENCES = (
    "Fair. I'm slower than I should be tonight. I'll keep my turns "
    "shorter from here."
)


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


def _armed_next(game, question=Q_NEXT):
    """Between questions: a live game, no window open, the NEXT card armed
    and undelivered — the state the reveal-to-Q3 dispatch serves."""
    game.sk.bind_speaker("S1", "Rami")
    game.game_started = True
    game.sk.round = 1
    game.sk.set_phase("round")
    game.armed_question = dict(question)


def _live_window(game, question=Q_MC, *, window=25.0):
    """A live card with its answer window OPEN (a clock on it)."""
    game.sk.bind_speaker("S1", "Rami")
    game.game_started = True
    game.armed_question = dict(question)
    game.sk.start_question(game.armed_question)
    game.sk.round = 1
    game.sk.set_phase("round")
    delivered = getattr(game, "_delivered_to_playout", None)
    if delivered is None:
        delivered = game._delivered_to_playout = set()
    delivered.add(game.sk.question_number)
    key = f"q_{game.sk.question_number}_delivery"
    game.say_registry.claim(key, owner="speech_delivery")
    game.say_registry.confirm(key)
    at = time.time()
    game.sk.open_answer_window(window, now=at - 1.0)
    return at


def _adjacent(game, at):
    """FL-1's adjacency anchor: Lily's last turn ended a beat before this
    final (the live shape — the reveal had just finished)."""
    classifier = getattr(game, "addressee_classifier", None)
    if classifier is None:
        classifier = lily_addressee_classifier.LilyAddresseeClassifier()
        game.addressee_classifier = classifier
    classifier.note_agent_prompt(at - 1.0)


def _acts(game):
    return list((game._dispatched_act_by_speech or {}).values())


def _organic_reply(game):
    """The framework's own generate_reply at turn commit: a handle no Lily
    lane stamped."""
    return game.session._handle()


def _respond(game, text, *, speech_id=None):
    """The organic response through the REAL say pipeline (tts_node's
    stages, same order), on the framework's unstamped handle."""
    reply = _organic_reply(game) if speech_id is None else None
    sid = speech_id or reply.id
    turn = SpeechTurn(
        text=text, raw=text, game=game, agent=_pipeline_agent(),
        speech_id=sid,
    )
    outcome = run_say_pipeline(turn)
    assert outcome is None, f"pipeline suppressed the response: {outcome}"
    return sid, turn.text


def _air(game, sid, text):
    game.note_playout_started(sid)
    game.on_agent_speech_finished(text, speech_id=sid)


def _lines(caplog, needle):
    return [r.getMessage() for r in caplog.records if needle in r.getMessage()]


def _addressed_events(game, stage):
    return [
        e for e in game.airgate_events()
        if e.get("reason") == "addressed" and e.get("stage") == stage
    ]


# ---------------------------------------------------------------------------
# the response-contract classifier and the acceptance detector (pure)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text,subtype", [
    (LIVE_KINSEY, "question"),
    ("tell me more about him", "question"),
    ("who was that guy", "question"),
    (LIVE_NAME, "correction"),
    (LIVE_DIAMOND, "correction"),
    ("the score is wrong, I should have two points", "correction"),
    ("you misheard me", "correction"),
    (LIVE_SLOW, "complaint"),
    (LIVE_UNFAIR, "complaint"),
    (LIVE_BEAT, "complaint"),
    ("you're not listening to me", "complaint"),
    ("why do you keep jumping from question to question", "structural"),
    ("are you even listening", "structural"),
    ("can you show us a picture", "request"),
    ("please speak slower", "request"),
    ("switch to a british accent", "request"),
    ("haha you're funny", "banter"),
    ("nice one Lily", "banter"),
    (LIVE_FLOOR, "floor_hold"),
    ("can you explain the question", "game_meta"),
    ("give me a hint", "game_meta"),
    ("switch to multiple choice", "game_meta"),  # the W6 choices lane
    ("that was interesting", "other"),
])
def test_response_contract_subtype(text, subtype):
    assert lily_scorekeeper.lily_classify_address(text) == subtype


@pytest.mark.parametrize("text", [
    "yes", "yeah", "sure", "okay", "ready", "okay ready", "next one",
    "next question", "hit me", "yeah, next one", "go ahead", "let's go",
    "we're ready", "sure, go ahead", "bring it on", "okay go",
])
def test_acceptance_detected(text):
    assert lily_scorekeeper.lily_detect_addressed_acceptance(text) is True


@pytest.mark.parametrize("text", [
    "no", "not yet", "no, wait", "hold on", LIVE_KINSEY, "Lily",
    "yes but who was he really", "tell me more", "I said diamond",
    "start over",
])
def test_acceptance_not_detected(text):
    assert lily_scorekeeper.lily_detect_addressed_acceptance(text) is False


# ---------------------------------------------------------------------------
# the sentence cap (pure)
# ---------------------------------------------------------------------------


def test_split_sentences_keeps_tags_decimals_and_beats_together():
    text = (
        "[soft] Kinsey was... a biologist. He counted 3.5 million wasps! "
        "<break time=\"0.4s\"/> Dr. Kinsey then turned to people. "
        "…anyway — ready for the next one?"
    )
    assert lily_say_gate.lily_split_sentences(text) == [
        "[soft] Kinsey was... a biologist.",
        "He counted 3.5 million wasps!",
        "<break time=\"0.4s\"/> Dr. Kinsey then turned to people.",
        "…anyway — ready for the next one?",
    ]


def test_cap_trims_and_appends_the_offer():
    out = lily_say_gate.lily_cap_addressed_response(
        FIVE_SENTENCES, cap=3, offer=OFFER, offer_key="ready for the next one",
    )
    assert out["sentences"] == 5 and out["trimmed"] and out["offer_appended"]
    kept = lily_say_gate.lily_split_sentences(out["text"])
    assert len(kept) == 4 and kept[-1] == OFFER
    assert kept[:3] == lily_say_gate.lily_split_sentences(FIVE_SENTENCES)[:3]


def test_cap_leaves_a_conforming_response_untouched():
    text = "Kinsey was a biologist. He studied wasps. " + OFFER
    out = lily_say_gate.lily_cap_addressed_response(
        text, cap=3, offer=OFFER, offer_key="ready for the next one",
    )
    assert out["changed"] is False and out["text"] == text
    assert out["sentences"] == 2 and out["offer_present"]


def test_cap_without_an_offer_never_appends_one():
    out = lily_say_gate.lily_cap_addressed_response(
        FIVE_SENTENCES, cap=None, offer=None, offer_key="ready for the next one",
    )
    assert out["changed"] is False and out["text"] == FIVE_SENTENCES


# ---------------------------------------------------------------------------
# Kinsey — the live row: question → held → 3 sentences → offer → Q3 once,
# after the acceptance
# ---------------------------------------------------------------------------


def test_kinsey_question_holds_answers_offers_and_q3_reads_once_after_acceptance(
    caplog,
):
    game = _airgate_game()
    _armed_next(game)
    at = time.time()
    _adjacent(game, at)

    def _go():
        with caplog.at_level(logging.INFO):
            _final(game, LIVE_KINSEY, at)
        # HELD: the spine waits — no Q3 over the question.
        assert game.progression_paused_reason() == "addressed"
        state = game.addressed_state()
        assert state["subtype"] == "question"
        assert state["fl1_score"] >= 0.6
        assert game.dispatch_armed_question(source="test") is False
        assert "question_delivery" not in _acts(game)
        assert _lines(caplog, "LILY_ADDRESSED | HELD")
        assert len(_addressed_events(game, "hold")) == 1
        # S8: the directive is on the state block at generation time.
        block = game.build_state_block()
        assert "ADDRESSED" in block and "Kinsey" in block and OFFER in block
        assert "three sentences" in block
        # The response — five sentences from the model — through the real
        # say pipeline: three plus the operator's offer, logged.
        sid, aired = _respond(game, FIVE_SENTENCES)
        kept = lily_say_gate.lily_split_sentences(aired)
        assert len(kept) == 4 and kept[-1] == OFFER
        state = game.addressed_state()
        assert state["responded"] and state["sentences"] == 5 and state["trimmed"]
        assert any("sentences=5" in m for m in _lines(caplog, "LILY_ADDRESSED | TRIMMED"))
        assert _lines(caplog, "LILY_ADDRESSED | RESPONDED")
        # The offer reaches the room; the hold stands — the table takes it.
        _air(game, sid, aired)
        assert game.addressed_state()["offer_aired"] is True
        assert _lines(caplog, "LILY_ADDRESSED | OFFER_AIRED")
        assert game.progression_paused_reason() == "addressed"
        assert game.dispatch_armed_question(source="test") is False
        assert "question_delivery" not in _acts(game)
        # The table gives it back — Q3 reads exactly once, now.
        _final(game, "yes", at + 30)
        assert game.progression_paused_reason() != "addressed"
        assert _acts(game).count("question_delivery") == 1
        released = _lines(caplog, "LILY_ADDRESSED | RELEASED")
        assert released and "by=acceptance" in released[-1]
        events = _addressed_events(game, "release")
        assert len(events) == 1
        assert events[0]["detail"]["released_by"] == "acceptance"
        assert events[0]["detail"]["sentences"] == 5
        assert events[0]["detail"]["utterance"] == LIVE_KINSEY

    _run(_go)


def test_a_conforming_answer_is_not_rewritten_but_still_responds(caplog):
    game = _airgate_game()
    _armed_next(game)
    at = time.time()
    _adjacent(game, at)

    def _go():
        _final(game, LIVE_KINSEY, at)
        text = "Kinsey was a biologist. He studied wasps, then people. " + OFFER
        with caplog.at_level(logging.INFO):
            sid, aired = _respond(game, text)
        assert aired == text
        assert game.addressed_state()["responded"] is True
        assert game.addressed_state()["trimmed"] is False
        assert not _lines(caplog, "LILY_ADDRESSED | TRIMMED")

    _run(_go)


def test_the_offer_is_appended_when_the_model_leaves_it_off(caplog):
    game = _airgate_game()
    _armed_next(game)
    at = time.time()
    _adjacent(game, at)

    def _go():
        _final(game, LIVE_KINSEY, at)
        with caplog.at_level(logging.INFO):
            sid, aired = _respond(game, "Kinsey was a biologist.")
        assert aired.endswith(OFFER)
        assert _lines(caplog, "LILY_ADDRESSED | OFFER_APPENDED")

    _run(_go)


def test_a_code_dispatched_act_is_never_capped_as_the_response():
    game = _airgate_game()
    _armed_next(game)
    at = time.time()
    _adjacent(game, at)

    def _go():
        _final(game, LIVE_KINSEY, at)
        assert game.gated_say(None, "floor", "", source="silence_budget",
                              text="I'm here — the floor's yours.") is True
        sid = next(s for s, a in game._dispatched_act_by_speech.items()
                   if a == "floor")
        turn = SpeechTurn(
            text=FIVE_SENTENCES, raw=FIVE_SENTENCES, game=game,
            agent=_pipeline_agent(), speech_id=sid,
        )
        run_say_pipeline(turn)
        assert game.addressed_state()["responded"] is False

    _run(_go)


# ---------------------------------------------------------------------------
# the other contracts, from the transcripts
# ---------------------------------------------------------------------------


def test_name_correction_holds_fixes_and_offers(caplog):
    """"Your name is spelled wrong on the page" → correction → held → the
    fix goes through the reversal path (the bind tool) → confirm → offer."""
    game = _airgate_game()
    _armed_next(game)
    at = time.time()
    _adjacent(game, at)

    def _go():
        with caplog.at_level(logging.INFO):
            _final(game, LIVE_NAME, at)
        assert game.progression_paused_reason() == "addressed"
        assert game.addressed_state()["subtype"] == "correction"
        block = game.build_state_block()
        assert "lily_bind_speaker" in block and "confirm" in block.lower()
        sid, aired = _respond(
            game, "You're right, I had it wrong. Fixed — it's Rami with an "
            "i. Sorry about that. Let me know if anything else is off.",
        )
        kept = lily_say_gate.lily_split_sentences(aired)
        assert len(kept) == 3 and kept[-1] == OFFER  # ≤ 2 + the offer
        assert "question_delivery" not in _acts(game)

    _run(_go)


def test_complaint_holds_acknowledges_plainly_and_offers(caplog):
    """"Why are you so slow?" → complaint → held → plain ack, no joke →
    offer. The contract line says so; the cap enforces the length."""
    game = _airgate_game()
    _armed_next(game)
    at = time.time()
    _adjacent(game, at)

    def _go():
        with caplog.at_level(logging.INFO):
            _final(game, LIVE_SLOW, at)
        assert game.progression_paused_reason() == "addressed"
        assert game.addressed_state()["subtype"] == "complaint"
        block = game.build_state_block()
        assert "no joke" in block and "no levity" in block
        sid, aired = _respond(game, THREE_SENTENCES)
        kept = lily_say_gate.lily_split_sentences(aired)
        assert len(kept) == 3 and kept[-1] == OFFER
        assert game.addressed_state()["trimmed"] is True
        assert "question_delivery" not in _acts(game)

    _run(_go)


def test_floor_hold_declaration_is_held_and_never_scored(caplog):
    """"We're not talking to you" into an OPEN window: addressed-as-meta
    — held, NOT scored, the offer is the exit."""
    game = _airgate_game()
    at = _live_window(game, Q_MC)

    def _go():
        with caplog.at_level(logging.INFO):
            _final(game, LIVE_FLOOR, at)
        assert not game.sk.ordered_candidates()  # not scored
        assert game.progression_paused_reason() == "addressed"
        state = game.addressed_state()
        assert state["subtype"] == "floor_hold"
        assert state["fl1_reason"] == "floor-hold"
        # The open window's clock is held the way a pause holds it —
        # candidates kept, deadline lifted.
        assert state["clock_held"] is True
        assert game.sk.answer_window_deadline is None
        assert game.sk.answer_window_open is True
        block = game.build_state_block()
        assert "floor is theirs" in block and OFFER in block

    _run(_go)


def test_i_said_diamond_after_the_window_is_a_correction_on_the_reversal_path(
    caplog,
):
    game = _airgate_game()
    _armed_next(game)
    at = time.time()
    _adjacent(game, at)

    def _go():
        with caplog.at_level(logging.INFO):
            _final(game, LIVE_DIAMOND, at)
        assert game.progression_paused_reason() == "addressed"
        assert game.addressed_state()["subtype"] == "correction"
        block = game.build_state_block()
        assert "lily_correct_verdict" in block
        assert "committed record" in block
        assert "question_delivery" not in _acts(game)

    _run(_go)


def test_game_meta_request_holds_without_the_offer():
    """A hint request on a live card: the hold (clock held), the existing
    hint directive is the response, no offer, never trimmed."""
    game = _airgate_game()
    at = _live_window(game, Q_MC)

    def _go():
        _final(game, "Lily, can I get a hint?", at)
        assert game.progression_paused_reason() == "addressed"
        assert game.addressed_state()["subtype"] == "game_meta"
        assert "hint" in (game._explain_request_note or "").lower()
        block = game.build_state_block()
        assert "ADDRESSED" in block and OFFER not in block
        sid, aired = _respond(game, FIVE_SENTENCES)
        assert aired == FIVE_SENTENCES

    _run(_go)


# ---------------------------------------------------------------------------
# exits: an answer lands, STOP, pause — and what never lifts it
# ---------------------------------------------------------------------------


def test_answer_into_the_open_window_mid_hold_scores_and_releases(caplog):
    game = _airgate_game()
    at = _live_window(game, Q_MC)
    _adjacent(game, at)

    def _go():
        with caplog.at_level(logging.INFO):
            _final(game, "Lily, who's Alfred Kinsey?", at)
            assert game.progression_paused_reason() == "addressed"
            assert game.addressed_state()["clock_held"] is True
            _final(game, "Mars", at + 3)
        assert game.sk.ordered_candidates()  # scored
        assert game.addressed_active() is False
        released = _lines(caplog, "LILY_ADDRESSED | RELEASED")
        assert released and "by=answer" in released[-1]
        assert _addressed_events(game, "release")[0]["detail"][
            "released_by"
        ] == "answer"

    _run(_go)


def test_stop_during_the_hold_wins(caplog):
    game = _airgate_game()
    _armed_next(game)
    at = time.time()
    _adjacent(game, at)

    def _go():
        with caplog.at_level(logging.INFO):
            _final(game, LIVE_KINSEY, at)
            assert game.progression_paused_reason() == "addressed"
            _final(game, "stop stop stop", at + 5)
        assert game.progression_paused_reason() == "game_stopped"
        assert game.addressed_active() is False
        released = _lines(caplog, "LILY_ADDRESSED | RELEASED")
        assert released and "by=stop" in released[-1]
        assert "question_delivery" not in _acts(game)

    _run(_go)


def test_explicit_pause_during_the_hold_wins_and_one_resume_lifts_both():
    game = _airgate_game()
    _armed_next(game)
    at = time.time()
    _adjacent(game, at)

    def _go():
        _final(game, LIVE_KINSEY, at)
        assert game.progression_paused_reason() == "addressed"
        sid, aired = _respond(game, FIVE_SENTENCES)
        _air(game, sid, aired)  # she answered and offered…
        _final(game, LIVE_PAUSE, at + 5)  # …and the table pauses anyway
        assert game.pause_sticky() is True
        assert game.progression_paused_reason() == "hold"  # the pause wins
        assert game.addressed_active() is True  # …and the address stands
        assert game.dispatch_armed_question(source="test") is False
        _final(game, "okay go", at + 20)
        assert game.pause_sticky() is False
        assert game.addressed_active() is False
        assert _acts(game).count("question_delivery") == 1

    _run(_go)


def test_no_timer_lifts_the_hold_and_the_reply_owed_latch_sits_below_it():
    game = _airgate_game()
    _armed_next(game)
    at = time.time()
    _adjacent(game, at)

    def _go():
        _final(game, LIVE_KINSEY, at)
        state = game.addressed_state()
        game._addressed["mono"] -= 3600.0  # an hour under the hold
        game._addressed["since"] -= 3600.0
        assert game.progression_paused_reason() == "addressed"
        assert game.hold_timed_out() is False  # not the C13 hold
        game.note_user_turn()  # B7's latch is armed too…
        assert game.progression_paused_reason() == "addressed"  # …below
        assert game.dispatch_armed_question(source="watchdog") is False
        assert state["seq"] == game.addressed_state()["seq"]

    _run(_go)


def test_a_new_address_restarts_the_cycle_and_the_game_never_advances(
    caplog,
):
    """Three addresses in sequence, no acceptance between: the game never
    advances; each cycle's offer repeats once on the silence (B8), then
    B8 is back to its own line; no dispatch."""
    game = _airgate_game()
    _armed_next(game)
    at = time.time()
    floor_lines = set(sum(
        (list(v) for v in lily_say_gate.LILY_FLOOR_LINES.values()), []
    ))

    def _cycle(text, response, when):
        _adjacent(game, when)
        _final(game, text, when)
        assert game.progression_paused_reason() == "addressed"
        sid, aired = _respond(game, response)
        _air(game, sid, aired)
        assert game.addressed_state()["offer_aired"] is True
        # the silence after the offer: B8 fires the offer, once
        game.note_user_final("hmm")
        game.note_user_turn()
        assert game.silence_budget_state() == "fire"
        assert game.silence_budget_fire() is True
        assert game.session.said[-1] == OFFER
        for sid_, act in list(game._dispatched_act_by_speech.items()):
            game.on_agent_speech_finished(act, speech_id=sid_)
        # a second silence in the same cycle: B8's own line, not the offer
        game.note_user_final("hmm")
        game.note_user_turn()
        assert game.silence_budget_fire() is True
        assert game.session.said[-1] in floor_lines
        for sid_, act in list(game._dispatched_act_by_speech.items()):
            game.on_agent_speech_finished(act, speech_id=sid_)

    def _go():
        with caplog.at_level(logging.INFO):
            _cycle(LIVE_KINSEY, FIVE_SENTENCES, at)
            _cycle(LIVE_SLOW, THREE_SENTENCES, at + 40)
            _cycle(LIVE_NAME, "Fixed. Sorry.", at + 80)
        assert game.progression_paused_reason() == "addressed"
        assert game.addressed_state()["seq"] == 3
        assert "question_delivery" not in _acts(game)
        assert game.dispatch_armed_question(source="test") is False
        released = _lines(caplog, "LILY_ADDRESSED | RELEASED")
        assert len(released) == 2
        assert all("by=new_address" in m for m in released)
        assert len(_lines(caplog, "LILY_ADDRESSED | OFFER_REPEATED")) == 3
        assert len(_addressed_events(game, "hold")) == 3

    _run(_go)


# ---------------------------------------------------------------------------
# the trigger is FL-1 — and what it deliberately does not hold
# ---------------------------------------------------------------------------


def test_fl1_gates_the_hold_side_chatter_never_holds():
    """FL-1 is the trigger, not the words: a multi-player remark with no
    name, not interrogative, no adjacency reads side_chatter (0.35 <
    host_threshold 0.60) — no hold. (B9b: the operator's three hard rules
    — solo session, her name anywhere, an interrogative with no open
    window — now cover the cases this test used to pin as gaps; see
    test_addressed_hold_b9b.py.)"""
    game = _airgate_game()
    _armed_next(game)
    game.sk.bind_speaker("S2", "Chris")  # not a solo session
    at = time.time()
    classifier = lily_addressee_classifier.LilyAddresseeClassifier()
    classifier.note_agent_prompt(at - 60.0)
    game.addressee_classifier = classifier

    def _go():
        _final(game, "that was a good one dude", at)
        judgment = game.last_addressee_judgment
        assert judgment.classification == "side_chatter"
        assert game.addressed_active() is False
        assert game.progression_paused_reason() is None

    _run(_go)


def test_a_reply_to_her_own_question_is_not_an_address():
    game = _airgate_game()
    _armed_next(game)
    at = time.time()
    _adjacent(game, at)
    game._question_pending = True  # she asked the table something

    def _go():
        _final(game, "the second one I think", at)
        assert game.addressed_active() is False

    _run(_go)


def test_a_code_routed_final_is_not_an_address(caplog):
    """"speak slower" is routed by the pace lane (code ack) — the routing
    IS the response; no hold."""
    game = _airgate_game()
    _armed_next(game)
    at = time.time()
    _adjacent(game, at)

    def _go():
        with caplog.at_level(logging.INFO):
            _final(game, "Lily, speak slower please", at)
        assert game.addressed_active() is False
        assert any("reason=code_acked" in m or "reason=control_command" in m
                   for m in _lines(caplog, "LILY_ADDRESSED | NOT_HELD"))

    _run(_go)


def test_lobby_never_holds():
    game = _airgate_game()
    game.game_started = False
    game.ui_phase = "lobby"
    at = time.time()
    _adjacent(game, at)

    def _go():
        _final(game, "Lily, how does this work?", at)
        assert game.addressed_active() is False

    _run(_go)


def test_a_cut_read_resumes_on_the_acceptance_not_over_the_hold():
    """The barge that cut the read was an ADDRESS: the C3c resume defers
    under the hold and fires when the table gives the game back."""
    game = _airgate_game()
    # The read was cut before its window opened (W7's C3d harness shape).
    game.sk.bind_speaker("S1", "Rami")
    game.game_started = True
    game.armed_question = dict(Q_MC)
    game.sk.start_question(game.armed_question)
    game.sk.round = 1
    game.sk.set_phase("round")
    qnum = game.sk.question_number
    game.say_registry.release(f"q_{qnum}_delivery")
    game.ui_phase = "question"
    game._delivery_barge_cut_qnum = qnum
    at = time.time()
    _adjacent(game, at)
    seg = {"text": "Lily, what are the rules again?", "speaker_label": "S1"}

    def _go():
        _final(game, seg["text"], at)
        assert game.progression_paused_reason() == "addressed"
        assert game._question_barge_resume_still_owed(qnum) is True
        assert game._maybe_resume_mcq_read(seg, now=time.time()) is False
        assert "question_nudge" not in _acts(game)
        sid, aired = _respond(game, "Four options, first one in wins. " + OFFER)
        _air(game, sid, aired)
        assert game._maybe_resume_mcq_read(seg, now=time.time()) is False
        assert "question_nudge" not in _acts(game)  # still deferred
        _final(game, "okay ready", at + 20)
        assert game.addressed_active() is False
        assert "question_nudge" in _acts(game)

    _run(_go)


def test_prompt_pin_carries_the_universal_rule_and_keys_on_the_state_line():
    """PIN (not behavior coverage): the rail carries the operator's rule
    verbatim and keys on the ADDRESSED state-block line the hold writes."""
    flat = " ".join(LILY_SYSTEM_PROMPT.split())  # the rail is line-wrapped
    assert "Progression yields to the table." in flat
    assert "Everything else gets a host." in flat
    assert "ADDRESSED" in flat
    assert OFFER in flat
