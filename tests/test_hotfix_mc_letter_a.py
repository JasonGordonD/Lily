"""HOTFIX-MC-LETTER-A-001 — live 2026-09-06 13:27:27Z, session
lily-BE84AA-11451c02, Q5 (four choices, answer A): the player said "A."
with the window open and nothing bound — no window_closed_at, the organic
lane re-read the question and whispered "A...", the player said "Yes,
that's my answer." Q2's "The answer is a." bound only because the options
read was still in flight (the early-answer path evaluates the raw text).

Cause: `lily_non_answer_utterance` normalizes the final first and returns
"empty" when nothing survives — and the normalizer strips the article "a",
so "A.", "a", "The answer is a." all normalize to "" and never reach the
answer-surface override the docstring promises ("an MC letter is an answer
no matter what else it looks like"). "b." survives; only option A is
unanswerable.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_evaluation  # noqa: E402
from test_bind_dispute_p0 import _make_game  # noqa: E402
from test_composition_followup_w6 import Q_MC, _arm, _final  # noqa: E402


def test_letter_a_is_an_answer_attempt_on_a_four_choice_card():
    for text in ("A.", "a", "The answer is a.", "I'd say a"):
        assert lily_evaluation.lily_non_answer_utterance(text, dict(Q_MC), ["Rami"]) is None, text


def test_letter_a_lands_as_a_candidate_through_the_production_path():
    """The live shape: window open, delivery finished, the player says "A."."""
    for text in ("A.", "The answer is a."):
        game = _make_game()
        at = _arm(game, Q_MC, window=30.0)
        _final(game, text, at + 1)
        assert "Rami" in game.sk.answer_candidates, text
        assert game.sk.answer_candidates["Rami"]["text"] == text


def test_article_in_conversation_is_still_not_an_answer():
    # The bare article inside a clause stays a non-answer (B1: conversational
    # speech never binds); the "empty" reason is preserved for real nothing.
    assert lily_evaluation.lily_non_answer_utterance("it's a", dict(Q_MC), ["Rami"]) is not None
    assert lily_evaluation.lily_non_answer_utterance("", dict(Q_MC), ["Rami"]) == "empty"
    # Freeform card: a lone "a" is still nothing.
    assert lily_evaluation.lily_non_answer_utterance(
        "a", {"prompt": "x", "canonical_answer": "Wilde"}, ["Rami"]
    ) == "empty"
