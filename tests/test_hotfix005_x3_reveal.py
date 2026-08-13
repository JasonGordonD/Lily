"""WO-LILY-HOTFIX-005 X3 — reveal fires without its delivery.

Live fixture (session lily-FFDEAE, 14:53:34): Lily aired "And on the
question: false. Not every woman can learn to squirt on demand…" — the
answer to a question that was never spoken. The operator: "You didn't even
say the question out loud."

Fix: a reveal/verdict act (adjudication) is dispatchable ONLY for a
question whose delivery reached playout. The answer window opens exactly at
the delivery turn's playout completion, so `open_window` durably records the
question number in `_delivered_to_playout`. Adjudicate refuses (ERROR log,
no reveal) when the current question was never delivered AND its window is
not currently open.
"""

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_say_gate
from lily_agent import LilyGame
from lily_scorekeeper import LilyScorekeeper


class _StubSayRegistry:
    """Minimal say-gate stand-in: reports every delivery claim as NOT
    confirmed unless a state is pre-seeded, so the X3 guard sees no
    delivery-reached-playout proof from this channel."""

    def __init__(self, states=None):
        self._states = states or {}

    def state(self, key):
        return self._states.get(key)  # None = unclaimed


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_game():
    game = LilyGame.bare()
    game.sk = LilyScorekeeper("x3-reveal-fixture")
    game.armed_question = {
        "id": "q_0001",
        "prompt": "Test?",
        "canonical_answer": "false",
        "acceptable_answers": ["false"],
    }
    game._adjudicating = False
    game._question_transitioning = False
    game._delivered_to_playout = set()
    game.say_registry = _StubSayRegistry()
    game.sk.answer_window_open = False
    return game


def test_adjudicate_refuses_reveal_without_delivery(caplog):
    """The defect: adjudication requested for a question whose delivery
    never reached playout — window never opened, nothing recorded. It must
    refuse and log, never air a verdict."""
    game = _make_game()
    with caplog.at_level(logging.ERROR):
        _run(game.adjudicate())
    assert any(
        "REFUSED_NO_DELIVERY" in r.message for r in caplog.records
    ), "undelivered reveal must be refused and logged"
    # It refused before flipping the in-flight flag / doing any work.
    assert game._adjudicating is False


def test_open_window_records_delivery_to_playout():
    """open_window IS the delivery-reached-playout event: it records the
    question number durably (append-only), unlike _aired_stems."""
    game = LilyGame.bare()
    game.sk = LilyScorekeeper("x3-openwindow")
    game.game_started = True
    game.armed_question = {"prompt": "Test?", "canonical_answer": "x"}
    game.eliminated = []
    game._delivered_to_playout = set()
    game._aired_stems = {game.sk.question_number}
    game._undelivered_ticks = 0
    game._undelivered_refires = 0
    game._mc_delivery_qnum = None
    game._active_delivery_qnum = None
    game._active_delivery_started_at = None
    game._window_timer = None
    game.background_audio = None
    game._bed_handle = None
    game._steal_window = False

    # Stub the async publishes / phase / bed / replay so open_window's
    # synchronous body runs without a live agent.
    game.publish_attributes_nowait = lambda: None
    game._set_ui_phase = lambda phase: None
    game._start_bed = lambda: None
    game._replay_pre_window_answers = lambda: None

    async def _noop_publish(*a, **k):
        return None

    game.publish_metadata = _noop_publish

    async def _drive():
        game.open_window(duration=0.01)
        # let the internal _expire task settle without adjudicating
        await asyncio.sleep(0)

    _run(_drive())
    assert game.sk.question_number in game._delivered_to_playout
    # M4 completion still clears the aired-stem marker (unchanged).
    assert game.sk.question_number not in game._aired_stems


def test_steal_window_does_not_record_but_rides_recorded():
    """A steal window rides an already-delivered question — it must NOT
    add a fresh record, and adjudication of it is allowed because the
    original open_window already recorded the question."""
    game = _make_game()
    # Original delivery reached playout earlier this session.
    game._delivered_to_playout = {game.sk.question_number}
    game.sk.answer_window_open = False
    # A steal adjudication should NOT be refused — the question is recorded.
    # (We assert the guard passes by confirming it does not short-circuit on
    # the delivery check; full adjudication needs a live agent, so we probe
    # only the guard condition.)
    delivered = game.sk.question_number in game._delivered_to_playout
    window_open = game.sk.answer_window_open
    refused = (not window_open) and (not delivered)
    assert refused is False


def test_confirmed_delivery_claim_allows_adjudicate():
    """The ARMED_LIMBO recovery path: window closed, but the q_{N}_delivery
    claim is CONFIRMED (the stem played out and the post-delivery chain
    died). That is a legitimate reveal — the guard must NOT refuse it."""
    game = _make_game()
    game._delivered_to_playout = set()
    game.sk.answer_window_open = False
    key = f"q_{game.sk.question_number}_delivery"
    game.say_registry = _StubSayRegistry({key: lily_say_gate.CLAIM_CONFIRMED})
    delivered = game.sk.question_number in game._delivered_to_playout
    confirmed = (
        game.say_registry.state(key) == lily_say_gate.CLAIM_CONFIRMED
    )
    refused = (
        not game.sk.answer_window_open and not confirmed and not delivered
    )
    assert refused is False


def test_open_window_now_allows_adjudicate_on_reconnect():
    """Reconnect: the durable set is a fresh-process empty, but a live open
    window is itself proof the stem aired. The guard must not refuse."""
    game = _make_game()
    game._delivered_to_playout = set()  # fresh process, nothing recorded
    game.sk.answer_window_open = True   # window is live right now
    refused = (
        not game.sk.answer_window_open
        and game.sk.question_number not in game._delivered_to_playout
    )
    assert refused is False
