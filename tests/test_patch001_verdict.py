"""WO-LILY-PATCH-001 T4 + T6 — verdict-first acknowledgments and
row-backed awards, from the Aug 6 evening fixtures.

T4: measured live ack latency was 11–12s commit-to-completion, long
enough that players re-answered ("Saturn" twice, "Kama Sutra" ×15). The
verdict word now airs as its own SHORT beat dispatched at commit
(budget: dispatch within ~1.5s, logged as COMMIT_TO_DISPATCH_MS), with
flourish/standings as a separate turn that never restates it.

T6: "Saturn is correct — you're on the board!" aired three times with
zero answers rows. Narration is generated only after the scorekeeper
commit returns success; a failed commit produces an in-character hold
plus an ERROR, never a celebration.
"""

import asyncio
import logging
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from livekit.agents import StopResponse
import lily_evaluation
from lily_agent import LilyAgent
from test_desync_fixture import (  # noqa: E402
    FEMUR_QUESTION, _adjudicate_and_drain, _arm_question, _make_game, _run,
)


def _answered_game():
    game = _make_game()
    game.sk.bind_speaker("S1", "Rami")
    _arm_question(game, FEMUR_QUESTION)
    now = 900.0
    game.sk.open_answer_window(duration=30.0, now=now)
    game.sk.on_transcript_segment(
        text="femur", speaker_label="S1", is_final=True,
        now=now + 2, segment_start_time=now + 2,
    )
    return game


def test_normal_question_airs_exactly_one_acknowledgment(caplog):
    game = _answered_game()
    with caplog.at_level(logging.INFO):
        _run(_adjudicate_and_drain(game), game)
    # REFACTOR W2a: exactly one acknowledgment still airs, now as the
    # DETERMINISTIC verdict sheet on the direct_say lane (no LLM composite).
    assert len(game.session.instructions) == 0
    assert len(game.session.said) == 1
    verdict = game.session.said[0]
    assert "femur" in verdict.lower()
    assert "Rami" in verdict
    # Budget telemetry present and inside the ~1.5s dispatch budget
    # (offline sim: effectively instant — the assertion pins the LOG
    # CONTRACT; live telemetry reads the same line).
    lat = [r for r in caplog.records if "COMMIT_TO_DISPATCH_MS" in r.message]
    assert lat, "verdict latency telemetry must be logged"
    ms = float(re.search(r"ms=(\d+)", lat[0].getMessage()).group(1))
    assert ms < 1500


def test_deterministic_verdict_suppresses_organic_llm_reply():
    game = _answered_game()
    game.mark_deterministic_reply("femur")
    agent = LilyAgent.__new__(LilyAgent)
    agent._game = game
    message = type("Message", (), {"content": ["femur"]})()

    async def scenario():
        with pytest.raises(StopResponse):
            await agent.on_user_turn_completed(None, message)

    asyncio.run(scenario())
    assert game.consume_deterministic_reply("femur") is False


def test_pre_generation_hook_suppresses_correct_answer_before_event_callback():
    """LiveKit does not guarantee whether the public transcript event or
    on_user_turn_completed runs first. A clean answer must be engine-owned
    even when the hook wins that race."""
    game = _answered_game()
    agent = LilyAgent.__new__(LilyAgent)
    agent._game = game
    message = type("Message", (), {"content": ["No, I said the femur."]})()

    async def scenario():
        with pytest.raises(StopResponse):
            await agent.on_user_turn_completed(None, message)

    asyncio.run(scenario())
    token = (
        game.sk.question_number,
        lily_evaluation.lily_normalize_answer("No, I said the femur."),
    )
    assert token in game._prehook_answer_suppressions
    # The later transcript callback consumes the reservation instead of
    # leaving a stale suppression for a future same-word turn.
    game.mark_deterministic_reply("No, I said the femur.")
    assert token not in game._prehook_answer_suppressions


def test_failed_commit_holds_in_character_and_never_celebrates(caplog):
    game = _answered_game()

    def _boom(*args, **kwargs):
        raise RuntimeError("ledger write refused")

    game.sk.record_result = _boom
    with caplog.at_level(logging.ERROR):
        _run(_adjudicate_and_drain(game), game)
    # REFACTOR W2a: the commit-failure hold is a deterministic direct_say
    # sheet — no verdict narration, just the in-character hold.
    joined = " ".join(game.session.said)
    assert "Correct" not in joined       # no verdict narration
    assert "point" not in joined.lower()
    assert "double-check" in joined      # the in-character hold aired
    assert any("COMMIT_FAILED" in r.message for r in caplog.records)
    # No score moved.
    assert (game.sk.players.get("Rami") or {}).get("score", 0) == 0
