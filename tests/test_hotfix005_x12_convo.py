"""WO-LILY-HOTFIX-005 X12 — two conversational gaps.

Explain-on-request: the operator asked twice (14:40:27, 14:40:44) for the
active question to be explained and got nothing, then Lily claimed she "can
and will." Contest: he said "the correct answer is A" three times and was
told "we're past that one either way."

The detectors arm one-shot state-block directives so the reply restates the
question / re-checks the ruling instead of ignoring it.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lily_scorekeeper import (
    lily_detect_explain_request,
    lily_detect_verdict_contest,
)


# -- explain-on-request -------------------------------------------------------

def test_explain_request_fires_on_plain_asks():
    for t in [
        "can you explain the question?",
        "what does that mean?",
        "I don't understand the question",
        "rephrase the question please",
        "say that again, differently",
        "what's the question again?",
        "explain this question",
        "put it in plain english",
    ]:
        assert lily_detect_explain_request(t), t


def test_explain_request_ignores_unrelated():
    for t in [
        "explain the rules of the game",
        "what do you mean we're tied?",
        "the answer is Paris",
        "let's keep going",
        "who's winning?",
    ]:
        assert not lily_detect_explain_request(t), t


# -- verdict contest ----------------------------------------------------------

def test_contest_fires_on_mishear_and_correction():
    for t in [
        "you misheard me",
        "the correct answer is A",
        "the answer was B",
        "I said Mercury, not Mars",
        "that's wrong, I was right",
        "I actually got that right",
        "go back to my answer",
    ]:
        assert lily_detect_verdict_contest(t), t


def test_contest_ignores_fresh_answers_and_chatter():
    for t in [
        "the answer is Paris",
        "hmm, I think it's oxygen",
        "nice one",
        "let's play relaxed",
        "can we skip this one?",
    ]:
        assert not lily_detect_verdict_contest(t), t
