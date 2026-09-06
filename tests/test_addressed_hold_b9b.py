"""WO-LILY-ADDRESSED-001 follow-up (B9b) — the operator's ruling on the
FL-1 gap, verbatim:

  Do NOT widen the corpus for this. The trigger already has the hard-rule
  path — use it. Three deterministic rules, no corpus change:
    a) Solo session: every utterance is host-directed by definition.
       There is nobody else.
    b) Any session: an utterance containing her name is host-directed
       regardless of adjacency.
    c) Any session: an interrogative-shaped utterance with no open answer
       window is host-directed regardless of adjacency.
  These cover the between-questions gap where the classifier scores 0.35.

Every rule reaches the `addressed` trigger through the same host_directed
classification — no bypass. Fixtures drive the production objects; the
classifier unit tests drive lily_addressee_classifier directly.
"""

import logging
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_addressee_classifier as fl1  # noqa: E402
from test_addressed_hold_b9 import (  # noqa: E402
    Q_MC, _acts, _airgate_game, _armed_next, _lines, _live_window,
)
from test_bind_dispute_p0 import _final, _run  # noqa: E402


def _no_adjacency(game, at):
    """A fresh classifier with Lily's last turn long gone — the prior
    alone (0.35 between questions), no adjacency bonus."""
    classifier = fl1.LilyAddresseeClassifier()
    classifier.note_agent_prompt(at - 60.0)
    game.addressee_classifier = classifier


def _two_players(game):
    game.sk.bind_speaker("S1", "Rami")
    game.sk.bind_speaker("S2", "Chris")


def _signals(text, **kw):
    base = dict(
        text=text, speaker_label="S1", ts=100.0, window_open=False,
        expectation_match=False, phase="idle", command_shaped=False,
        register=None,
    )
    base.update(kw)
    return fl1.LilyUtteranceSignals(**base)


# ---------------------------------------------------------------------------
# the classifier: the three rules on the hard-rule path
# ---------------------------------------------------------------------------


def test_prior_alone_between_questions_is_side_chatter():
    """The gap the ruling covers, pinned: idle, no name, no adjacency,
    not interrogative, not solo → 0.35 < 0.60."""
    j = fl1.LilyAddresseeClassifier().classify(
        _signals("that was a good one dude")
    )
    assert j.classification == fl1.CLASS_SIDE_CHATTER and j.score == 0.35


def test_rule_a_solo_session_is_host_directed_by_definition():
    j = fl1.LilyAddresseeClassifier().classify(
        _signals("that was a good one", solo=True)
    )
    assert j.classification == fl1.CLASS_HOST_DIRECTED
    assert j.reason == "solo" and j.score >= 0.6


@pytest.mark.parametrize("text", [
    "Lily, that was unfair",            # vocative — the old rule
    "I think Lily got that one wrong",  # mention — contain-anywhere
    "Lily is a joke",                   # referential — still her name
])
def test_rule_b_her_name_anywhere_is_host_directed(text):
    j = fl1.LilyAddresseeClassifier().classify(_signals(text))
    assert j.classification == fl1.CLASS_HOST_DIRECTED
    assert j.reason in ("vocative", "name")


def test_rule_c_interrogative_with_no_open_window_is_host_directed():
    j = fl1.LilyAddresseeClassifier().classify(
        _signals("why are you so slow?", question_shaped=True)
    )
    assert j.classification == fl1.CLASS_HOST_DIRECTED
    assert j.reason == "interrogative"


def test_rule_c_never_fires_into_an_open_window():
    """An interrogative INTO an open window is left to the scoring path:
    answer-shaped ("is it Mars?") matches the expectation and is
    definitional; a non-answer keeps the window prior (0.65 ≥ 0.60 —
    host by score, not by rule)."""
    c = fl1.LilyAddresseeClassifier()
    j = c.classify(_signals(
        "is it Mars?", window_open=True, expectation_match=True,
        phase="question", question_shaped=True,
    ))
    assert j.classification == fl1.CLASS_HOST_DIRECTED
    assert j.reason == "window+match"
    j2 = fl1.LilyAddresseeClassifier().classify(_signals(
        "is it Mars?", window_open=True, phase="question",
        question_shaped=True,
    ))
    assert j2.reason != "interrogative"


def test_rule_c_leaves_a_question_to_the_table_alone():
    """The 81BCB0 ground truth: "Have you guys seen Loki?" is asked of the
    other players (lily_table_address), not of her — rule (c) stands
    down; the prior alone decides (side chatter between questions)."""
    j = fl1.LilyAddresseeClassifier().classify(
        _signals("Have you guys seen Loki?", question_shaped=True)
    )
    assert j.classification == fl1.CLASS_SIDE_CHATTER
    assert j.reason == "score"


def test_the_rules_break_a_live_side_cluster_like_the_vocative_rule():
    c = fl1.LilyAddresseeClassifier()
    c._lock_cluster(90.0, {"S1", "S2"}, 80.0)
    j = c.classify(_signals("who was that guy", question_shaped=True))
    assert j.cluster_event == fl1.CLUSTER_BREAK
    assert j.classification == fl1.CLASS_HOST_DIRECTED


# ---------------------------------------------------------------------------
# through the spine: each rule reaches the addressed trigger, no bypass
# ---------------------------------------------------------------------------


def test_solo_session_banter_between_questions_is_held(caplog):
    """Rule (a): one bound player, no adjacency, no name, not a question
    — held, banter contract, HARD_RULE receipt on the classification."""
    game = _airgate_game()
    _armed_next(game)  # binds exactly Rami
    at = time.time()
    _no_adjacency(game, at)

    def _go():
        with caplog.at_level(logging.INFO):
            _final(game, "haha that was a good one", at)
        assert game.last_addressee_judgment.reason == "solo"
        assert game.progression_paused_reason() == "addressed"
        assert game.addressed_state()["subtype"] == "banter"
        assert game.dispatch_armed_question(source="test") is False
        assert "question_delivery" not in _acts(game)
        assert any("HARD_RULE | rule=solo" in m
                   for m in _lines(caplog, "LILY_ADDRESSEE | CLASSIFIED"))

    _run(_go)


def test_her_name_with_no_adjacency_is_held(caplog):
    """Rule (b): two players, "Lily, that was unfair" a minute after her
    last turn — held, complaint contract."""
    game = _airgate_game()
    _armed_next(game)
    _two_players(game)
    at = time.time()
    _no_adjacency(game, at)

    def _go():
        with caplog.at_level(logging.INFO):
            _final(game, "Lily, that was unfair", at)
        assert game.last_addressee_judgment.classification == "host_directed"
        assert game.last_addressee_judgment.reason in ("vocative", "name")
        assert game.progression_paused_reason() == "addressed"
        assert game.addressed_state()["subtype"] == "complaint"
        assert "question_delivery" not in _acts(game)

    _run(_go)


def test_interrogative_with_no_open_window_is_held_as_a_complaint(caplog):
    """Rule (c): two players, "why are you so slow?" between questions,
    no adjacency, no name — held, complaint contract."""
    game = _airgate_game()
    _armed_next(game)
    _two_players(game)
    at = time.time()
    _no_adjacency(game, at)

    def _go():
        with caplog.at_level(logging.INFO):
            _final(game, "why are you so slow?", at)
        assert game.last_addressee_judgment.reason == "interrogative"
        assert game.progression_paused_reason() == "addressed"
        assert game.addressed_state()["subtype"] == "complaint"
        assert any("HARD_RULE | rule=interrogative" in m
                   for m in _lines(caplog, "LILY_ADDRESSEE | CLASSIFIED"))

    _run(_go)


def test_interrogative_answer_into_an_open_window_still_scores():
    """Negative: with the window open, "is it Mars?" is an ANSWER — it
    becomes the candidate and scores; nothing holds."""
    game = _airgate_game()
    _live_window(game, Q_MC)
    _two_players(game)
    at = time.time()
    _no_adjacency(game, at)

    def _go():
        _final(game, "is it Mars?", at)
        assert game.sk.ordered_candidates()  # scored
        assert game.addressed_active() is False
        assert game.last_addressee_judgment.reason == "window+match"

    _run(_go)


def test_a_protest_is_both_a_dispute_and_an_address_and_the_dispute_reads_first():
    """A solo player's protest right behind a verdict arms D2's dispute
    hold AND the addressed hold; both stand, and the reason names the
    more specific D2 state while it lasts."""
    game = _airgate_game()
    _armed_next(game)
    at = time.time()
    _no_adjacency(game, at)

    def _go():
        _final(game, "I didn't say a word, I was still thinking.", at)
        assert game.addressed_active() is True
        game._dispute_hold_since = time.time()
        assert game.dispute_hold_active() is True
        assert game.progression_paused_reason() == "dispute_hold"
        game.release_dispute_hold(reason="test")
        assert game.progression_paused_reason() == "addressed"

    _run(_go)


def test_multi_player_side_remark_stays_side_chatter():
    """Negative: two players, no name, not interrogative, no adjacency —
    side chatter; no hold, no address debt."""
    game = _airgate_game()
    _armed_next(game)
    _two_players(game)
    at = time.time()
    _no_adjacency(game, at)

    def _go():
        _final(game, "that was a good one dude", at)
        assert game.last_addressee_judgment.classification == "side_chatter"
        assert game.addressed_active() is False
        assert game.progression_paused_reason() is None

    _run(_go)
