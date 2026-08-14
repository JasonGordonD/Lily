"""WO-LILY-QUESTION-FIRING-001 — kill the dead time between reveal and the
next question.

Audited session lily-6A32BD-741b9b66: after a reveal, RevealDeliveryFusionClip
clips the fused next-question off the reveal turn (correct — keeps reveals
clean), but the deferred standalone delivery then waited for the 2-agent-turn
LILY_WINDOW DELIVERY_NUDGE before airing — ~6s of pure clip->wait->nudge dead
time, and in the worst case the clipped question was shown on screen but never
voiced (answer scored 0).

Fix 1 — the clip arms an immediate breathed dispatch instead of waiting.
Fix 2 — a delivery watchdog guarantees a clipped question airs.
Fix 3 — skip the doomed live pre-generation when a deliverable is stocked.

The dispatch bypasses the N12 verdict-airing wedge on purpose (the clip only
fires once the reveal has aired), but keeps every other load-bearing gate:
supply readiness, an already-answered question, and address_unanswered.
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_config
from test_hotfix006_transitions import (
    Q_RUSSIA,
    _arm,
    _dispatched,
    _make_game,
    _run,
)


def _awaiting_delivery(game, qnum, owner="t_fusion"):
    """Drive a transition to the reveal+verdict-aired state the fusion clip
    fires in — reveal owns the floor, the next question is not yet delivered."""
    assert game.open_question_transition(qnum, owner=owner, source="adjudicate")
    game.journal_transition(qnum, "reveal", owner=owner, detail={"answer": "x"})
    game.journal_transition(
        qnum, "verdict", owner=owner, detail={"key": f"q_{qnum}_reveal"}
    )
    assert game.transition_awaiting_delivery() is True


# -- Fix 1: the clipped question delivers immediately ----------------------


def test_fire_owed_delivery_dispatches_the_armed_question():
    game = _make_game()
    _arm(game, Q_RUSSIA)
    qnum = game.sk.question_number
    _awaiting_delivery(game, qnum)

    game._fusion_owed_delivery_qnum = qnum
    assert game._fire_owed_fusion_delivery(source="fusion_clip_immediate")

    # The armed question aired as a standalone delivery.
    assert _dispatched(game, Q_RUSSIA["prompt"])
    assert game._pending_delivery_qnum == qnum
    # The owed marker is spent, and the transition closed on next_delivery so
    # no other lane re-narrates it.
    assert game._fusion_owed_delivery_qnum is None
    assert "next_delivery" in game.transition_stages(qnum)


def test_note_fires_inline_without_a_running_loop():
    # A unit-context clip (no event loop) records the owed marker and fires
    # once inline so the transform stays deterministic.
    game = _make_game()
    _arm(game, Q_RUSSIA)
    _awaiting_delivery(game, game.sk.question_number)
    game.note_fusion_clipped_delivery()
    assert _dispatched(game, Q_RUSSIA["prompt"])


def test_note_is_a_noop_without_an_armed_question():
    game = _make_game()
    game.armed_question = None
    game.note_fusion_clipped_delivery()
    assert game._fusion_owed_delivery_qnum is None
    assert not _dispatched(game, Q_RUSSIA["prompt"])


def test_breath_is_respected_before_the_immediate_dispatch(monkeypatch):
    # PACING-001: the immediate dispatch waits the operator-pinned breath —
    # it must not fire synchronously at clip time, and it must fire after.
    monkeypatch.setattr(
        lily_config, "inter_question_breath_seconds", lambda: 0.05
    )

    game = _make_game()
    _arm(game, Q_RUSSIA)
    _awaiting_delivery(game, game.sk.question_number)

    def _scenario():
        async def _go():
            game.note_fusion_clipped_delivery()
            # Breath not elapsed: nothing aired yet (proves it is not bypassed).
            assert not _dispatched(game, Q_RUSSIA["prompt"])
            await asyncio.sleep(0.12)
            # Breath elapsed: the immediate path fired.
            assert _dispatched(game, Q_RUSSIA["prompt"])
        return _go()

    _run(_scenario, game)


# -- Fix 2: the watchdog guarantees the question airs ----------------------


def test_watchdog_reemits_when_the_deadline_passes():
    game = _make_game()
    _arm(game, Q_RUSSIA)
    qnum = game.sk.question_number
    _awaiting_delivery(game, qnum)

    def _scenario():
        async def _go():
            game._fusion_owed_delivery_qnum = qnum
            # Deadline already past — the backstop fires on its first tick.
            game._fusion_delivery_deadline = (
                asyncio.get_running_loop().time() - 1.0
            )
            task = asyncio.ensure_future(game._fusion_delivery_watchdog(qnum))
            await asyncio.sleep(0.05)
            if not task.done():
                task.cancel()
        return _go()

    _run(_scenario, game)
    assert _dispatched(game, Q_RUSSIA["prompt"])
    assert game._fusion_owed_delivery_qnum is None


# -- claim discipline: the immediate path and the nudge never both fire ----


def test_no_double_delivery_when_a_lane_already_owns_the_delivery():
    game = _make_game()
    _arm(game, Q_RUSSIA)
    qnum = game.sk.question_number
    _awaiting_delivery(game, qnum)

    # First fire delivers.
    game._fusion_owed_delivery_qnum = qnum
    assert game._fire_owed_fusion_delivery(source="fusion_clip_immediate")
    fired_once = len(_dispatched(game, Q_RUSSIA["prompt"]))
    assert fired_once == 1

    # A re-clip re-arms the marker, but the delivery is already pending — the
    # second fire stands down (no second airing).
    game._fusion_owed_delivery_qnum = qnum
    assert game._fire_owed_fusion_delivery(source="fusion_clip_immediate") is False
    assert len(_dispatched(game, Q_RUSSIA["prompt"])) == 1
    assert game._fusion_owed_delivery_qnum is None

    # And the normal post-reveal seam cannot double it either: next_delivery
    # is journaled, so dispatch_armed_question reads DUP and declines.
    assert game.dispatch_armed_question(source="post_reveal") is False
    assert len(_dispatched(game, Q_RUSSIA["prompt"])) == 1


# -- load-bearing gates survive --------------------------------------------


def test_address_unanswered_holds_the_delivery():
    game = _make_game()
    _arm(game, Q_RUSSIA)
    qnum = game.sk.question_number
    _awaiting_delivery(game, qnum)
    game._awaiting_address_since = time.time()

    game._fusion_owed_delivery_qnum = qnum
    # EXPECT_BLOCKED: the gate holds, nothing airs, and the marker survives so
    # the watchdog can retry once the address resolves.
    assert game._fire_owed_fusion_delivery(source="fusion_clip_immediate") is False
    assert not _dispatched(game, Q_RUSSIA["prompt"])
    assert game._fusion_owed_delivery_qnum == qnum


def test_answered_question_never_re_airs():
    game = _make_game()
    _arm(game, Q_RUSSIA)
    qnum = game.sk.question_number
    _awaiting_delivery(game, qnum)
    game._answered_questions = {qnum}

    game._fusion_owed_delivery_qnum = qnum
    assert game._fire_owed_fusion_delivery(source="fusion_clip_immediate") is False
    assert not _dispatched(game, Q_RUSSIA["prompt"])
    assert game._fusion_owed_delivery_qnum is None


# -- Fix 3: skip live pre-generation when stock is in hand -----------------


def test_skip_live_pregen_only_when_stocked():
    game = _make_game()
    game.next_question = None
    game._next_question_reserve = None
    # Stock LOW (nothing in hand): generation runs as today.
    assert game._skip_live_pregen_when_stocked() is False

    # A stocked head: skip the doomed live generation, draw from the bank.
    game.next_question = {"id": "q_head"}
    assert game._skip_live_pregen_when_stocked() is True

    # A stocked depth-2 reserve alone also counts as stock in hand.
    game.next_question = None
    game._next_question_reserve = {"id": "q_reserve"}
    assert game._skip_live_pregen_when_stocked() is True
