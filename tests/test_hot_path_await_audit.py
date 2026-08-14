"""Hot-path await audit: glass publishes must not gate speech.

Persistence writes on adjudicate/reveal/answer/transcript were already
fire-and-forget. Residual blockers were `await publish_attributes` /
`await publish_metadata` sitting in front of `gated_say` or tool returns.
This suite locks the nowait contract for those seams while leaving
honesty awaits (adjudicate gather, custom round, forget, greeting memory,
group identity resolve) alone.
"""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

import lily_agent
from lily_agent import LilyGame


def test_skip_question_does_not_await_glass_before_speech(monkeypatch):
    """skip → next question must not wait on metadata/attribute RTT."""
    src = inspect.getsource(LilyGame.skip_question)
    assert "await self.publish_metadata" not in src
    assert "await self.publish_attributes()" not in src
    assert "ensure_future(self.publish_metadata" in src
    assert "publish_attributes_nowait()" in src


def test_start_game_awaits_identity_not_attributes():
    src = inspect.getsource(LilyGame.start_game)
    assert "await self.resolve_group_identity" in src
    assert "await self.publish_attributes()" not in src
    assert "publish_attributes_nowait()" in src


def test_award_bonus_publishes_nowait():
    # lily_enter_adult_mode is now a stub (content-mode gate removed) and no
    # longer publishes; the hot-path nowait discipline still applies to
    # lily_award_bonus.
    bonus_src = inspect.getsource(lily_agent.LilyAgent.lily_award_bonus)
    assert "await self._game.publish_attributes()" not in bonus_src
    assert "publish_attributes_nowait()" in bonus_src


def test_adjudicate_still_awaits_glass_before_verdict():
    """desync-E honesty: committed scores hit the glass before speech."""
    src = inspect.getsource(LilyGame.adjudicate)
    assert "await asyncio.gather" in src
    assert "publish_attributes()" in src
    assert "publish_metadata(" in src


def test_skip_question_speaks_without_waiting_on_slow_publish():
    """Behavioral: a hung publish must not delay gated_say dispatch."""
    game = LilyGame.bare()
    game.sk = SimpleNamespace(
        session_id="hot-path",
        answer_window_open=True,
        question_number=1,
        current_question={"id": "q1"},
        close_answer_window=lambda: None,
        set_phase=lambda *_a, **_k: None,
    )
    game.armed_question = {"id": "q1", "prompt": "Capital of France?"}
    game._window_timer = None
    game._adjudicating = False
    game._question_transitioning = False
    game._stop_bed = lambda: None
    game._set_ui_phase = lambda *_a, **_k: None
    game._terminate_aired_stem = lambda **_k: None
    game.expect_delivery = lambda: None
    game.arm_next_question = lambda: True

    said = {"n": 0}

    def _gated_say(*_a, **_k):
        said["n"] += 1
        return True

    game.gated_say = _gated_say

    async def _slow_publish(*_a, **_k):
        await asyncio.sleep(60)

    game.publish_metadata = _slow_publish
    game.publish_attributes = _slow_publish
    nowait = {"n": 0}
    game.publish_attributes_nowait = lambda: nowait.__setitem__("n", nowait["n"] + 1)

    async def _run():
        # Must complete promptly even though publish coroutines are slow —
        # they are scheduled, not awaited.
        await asyncio.wait_for(game.skip_question("voice"), timeout=1.0)

    asyncio.run(_run())
    assert said["n"] == 1
    assert nowait["n"] == 1
