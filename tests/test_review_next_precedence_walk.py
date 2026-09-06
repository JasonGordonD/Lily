"""Composition review of integ/next c71287e — the precedence walk.

Operator: "the precedence chain is the load-bearing part — STOP retires,
explicit pause above, addressed above reply-owed and above every question
lane … Have the reviewer walk EVERY dispatch site against the precedence
table … A hold that one lane ignores is not a hold."

The walk found the gated_say chokepoint for question_delivery /
question_nudge read `addressed` but not the two holds W9b placed ABOVE it
(`dispute_hold`, `restart_confirm_pending`) nor B7's `reply_owed` latch,
so four lanes that only pass through that chokepoint (window_fallback, the
fusion delivery, the C3c/C3d resume, the stale-claim retry) could put a
read on the air under them. The chokepoint — and `expect_delivery`, which
arms the delivery expectation — now read all of them.
"""

import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_airgate_001 import _game  # noqa: E402


def _live_game():
    game = _game()
    game.game_started = True
    game.game_over = False
    return game


@pytest.mark.parametrize(
    "predicate,value,expected_reason",
    [
        ("dispute_hold_active", True, "dispute_hold"),
        ("restart_confirm_pending", True, "restart_confirm_pending"),
        ("reply_owed_reason", "reply_owed", "reply_owed"),
    ],
)
@pytest.mark.parametrize("act", ["question_nudge", "question_delivery"])
def test_every_question_lane_is_refused_under_the_higher_holds(
    caplog, predicate, value, expected_reason, act
):
    game = _live_game()
    # The lane-independent chokepoint: whatever lane calls gated_say with a
    # progression act, these holds refuse it.
    setattr(game, predicate, (lambda *a, **k: value))
    with caplog.at_level(logging.INFO):
        dispatched = game.gated_say(
            None, act, "Next question: what is the capital of France?",
            source="window_fallback",
        )
    assert dispatched is False
    assert any(
        "DISPATCH_PAUSED" in r.getMessage() and f"reason={expected_reason}" in r.getMessage()
        for r in caplog.records
    ), expected_reason


@pytest.mark.parametrize(
    "predicate,value",
    [
        ("dispute_hold_active", True),
        ("restart_confirm_pending", True),
        ("reply_owed_reason", "reply_owed"),
        ("addressed_active", True),
    ],
)
def test_expect_delivery_never_arms_under_the_higher_holds(caplog, predicate, value):
    game = _live_game()
    game._pending_delivery_qnum = None
    setattr(game, predicate, (lambda *a, **k: value))
    with caplog.at_level(logging.INFO):
        game.expect_delivery()
    assert getattr(game, "_pending_delivery_qnum", None) is None
    assert any("EXPECT_BLOCKED" in r.getMessage() for r in caplog.records)


def test_a_clear_table_still_dispatches():
    game = _live_game()
    game._pending_delivery_qnum = None
    game.armed_question = {"prompt": "Capital of France?", "canonical_answer": "Paris"}
    game.sk.answer_window_open = False
    game.expect_delivery()
    assert getattr(game, "_pending_delivery_qnum", None) is not None
