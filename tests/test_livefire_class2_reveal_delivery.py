"""WO-LILY-LIVEFIRE-001 CLASS 2 — one speech owner after verdict.

Fixture lily-639007-f80aa6bf. 2a (organic double verdict) is owned by the
data-side StopResponse landed in 000bfc2 — a user turn the scorekeeper
consumed as an answer candidate suppresses the organic reply, leaving the
keyed reveal as the sole ruling owner. 2b (this file): q_4's reveal fused
the q_5 delivery into ONE utterance ("Crete… Next up. What ancient Greek
city-state… agoge?"). The reveal turn may not deliver the next question;
it is clipped at the say-gate and the real delivery fires after the reveal
confirms.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lily_say_gate import (
    lily_clip_delivery_from_reveal,
    _is_delivery_sentence,
)
from test_hotfix006_transitions import _make_game


# -- the fixture fusion line is split -------------------------------------

def test_crete_agoge_fusion_is_clipped():
    text = (
        "[excited] Crete — labyrinth, Minotaur, the whole island. That's "
        "yours. Rami, you're sitting on three, streak still rolling. Next "
        "up. What ancient Greek city-state raised its boys in the agoge?"
    )
    kept, dropped = lily_clip_delivery_from_reveal(text)
    # The reveal color survives; the next question does not.
    assert "Crete" in kept and "labyrinth" in kept
    assert "agoge" not in kept and "?" not in kept
    # The dropped tail is the premature delivery.
    assert "Next up" in dropped and "agoge" in dropped


def test_reveal_without_next_question_is_untouched():
    text = "[excited] Athens — that's the one, Rami. Nicely played."
    kept, dropped = lily_clip_delivery_from_reveal(text)
    assert kept == text and dropped == ""


# -- a pure delivery turn is never eaten ----------------------------------

def test_pure_delivery_turn_is_not_clipped():
    # A delivery turn opens with its question — boundary at sentence 0, so
    # nothing is clipped (the transition-state gate also scopes this out).
    text = "What famous marble temple crowns the Acropolis in Athens?"
    kept, dropped = lily_clip_delivery_from_reveal(text)
    assert kept == text and dropped == ""


def test_mc_delivery_is_not_clipped():
    text = (
        "Which island held the Minotaur's labyrinth? Was it Crete, Naxos, "
        "or Rhodes?"
    )
    kept, dropped = lily_clip_delivery_from_reveal(text)
    assert kept == text and dropped == ""


# -- lead-in detection ----------------------------------------------------

def test_delivery_leadins_detected():
    for s in ["Next up.", "Next question:", "Here's your next one.",
              "Moving on.", "On to the next.", "What year was it?"]:
        assert _is_delivery_sentence(s), s
    for s in ["That's yours, Rami.", "Crete — the whole island.",
              "Nicely played."]:
        assert not _is_delivery_sentence(s), s


# -- the transition-state gate scopes the clip ----------------------------

def test_awaiting_delivery_true_after_reveal_verdict():
    game = _make_game()
    owner = "transition_test"
    assert game.open_question_transition(3, owner=owner, source="adjudicate")
    game.journal_transition(3, "reveal", owner=owner,
                            detail={"answer": "Crete"})
    game.journal_transition(3, "verdict", owner=owner,
                            detail={"key": "q_3_reveal"})
    # Reveal aired, next question not yet delivered — the reveal owns the
    # floor, so a fused question in this turn must be clipped.
    assert game.transition_awaiting_delivery() is True
    game.journal_transition(3, "next_delivery", detail={"delivered_q": 4})
    # The delivery has run — a delivery turn reads False and is not clipped.
    assert game.transition_awaiting_delivery() is False


def test_awaiting_delivery_false_with_no_open_transition():
    game = _make_game()
    assert game.transition_awaiting_delivery() is False
