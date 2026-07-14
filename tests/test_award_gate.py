"""Regression tests for the lily_award_bonus game_started gate.

Pre-fix (2026-07-14) every stalled session showed lily_award_bonus as
the ENTIRE scoring path — round=0, question_number=0, but
final_standings non-empty from bonus-only backfill. That masked the
arm/ask/adjudicate loop being broken. Now that the loop is fixed, the
gate refuses award calls before game_started with an explicit tool
result, so a future stall surfaces as empty scores plus a spoken
adaptation instead of ghost bonuses.

This file imports lily_agent (and therefore livekit). test_round_loop.py
was deliberately kept livekit-free by the parallel WO-LILY-MEMORY-
CLOSEOUT-001 merge; keeping the LilyAgent-dependent tests in their own
file preserves that boundary.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lily_agent import LilyAgent
from lily_scorekeeper import LilyScorekeeper


class _FakeBonusGame:
    """Minimum surface LilyAgent.lily_award_bonus touches: game_started,
    sk.players, sk.award_bonus, send_event_nowait, publish_attributes."""

    def __init__(self, game_started: bool, players: list[str]) -> None:
        self.game_started = game_started
        self.sk = LilyScorekeeper("test-room")
        # Seed the roster directly — production path goes through
        # bind_speaker, but the tool under test only reads/mutates
        # players[name]["score"], so a minimal shape suffices.
        for p in players:
            self.sk.players[p] = {
                "speaker_label": None,
                "speaker_id": None,
                "score": 0,
                "streak": 0,
                "talk_time_s": 0.0,
                "answers_attempted": 0,
                "answers_correct": 0,
                "last_correct_category": None,
                "questions_since_spoke": 0,
                "lobby_fact": None,
                "lifeline_available": True,
            }
        self.events: list[tuple[str, dict]] = []
        self.publish_calls = 0

    def send_event_nowait(self, event_type: str, payload: dict) -> None:
        self.events.append((event_type, dict(payload)))

    async def publish_attributes(self) -> None:
        self.publish_calls += 1


def _make_agent(
    game_started: bool, players: list[str]
) -> tuple[LilyAgent, _FakeBonusGame]:
    """Sidestep LilyAgent.__init__ (heavy livekit Agent base) and just wire
    the one attribute the tool reads."""
    agent = LilyAgent.__new__(LilyAgent)
    game = _FakeBonusGame(game_started=game_started, players=players)
    agent._game = game
    return agent, game


def _call_award(agent: LilyAgent, player: str, reason: str) -> str:
    # FunctionTool exposes the raw coroutine via __wrapped__; call it
    # directly with a stub RunContext (the tool body doesn't touch it).
    # Fresh event loop per call — bypasses the pytest event-loop-closed
    # trap when tests are batched.
    coro = LilyAgent.lily_award_bonus.__wrapped__(agent, None, player, reason)
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_award_bonus_refused_before_game_started():
    agent, game = _make_agent(game_started=False, players=["Dave", "Sam"])
    msg = _call_award(agent, "Dave", "best wrong answer of the warmup")
    # Fail-loud refusal, not silent no-op.
    assert "only be awarded once a round is underway" in msg
    assert "lily_begin_round" in msg
    # Critically: no mutation, no downstream side effects.
    assert game.sk.players["Dave"]["score"] == 0
    assert game.events == []
    assert game.publish_calls == 0


def test_award_bonus_allowed_once_game_started():
    agent, game = _make_agent(game_started=True, players=["Dave", "Sam"])
    msg = _call_award(agent, "Dave", "brilliant misfire")
    assert msg == "Bonus point to Dave."
    assert game.sk.players["Dave"]["score"] == 1
    assert len(game.events) == 1
    assert game.events[0][0] == "best_wrong_answer"
    assert game.events[0][1]["player"] == "Dave"
    assert game.events[0][1]["answer"] == "brilliant misfire"
    assert game.publish_calls == 1


def test_award_bonus_gate_precedes_roster_check():
    # If Lily hallucinates a name AND the game isn't started, the gate
    # message wins — she needs to know the loop isn't live before she
    # tries again with a different name.
    agent, game = _make_agent(game_started=False, players=["Dave"])
    msg = _call_award(agent, "Ghost", "spectral vibes")
    assert "only be awarded once a round is underway" in msg
    assert game.sk.players["Dave"]["score"] == 0


def test_award_bonus_unknown_name_after_game_started_still_refuses_by_name():
    # Once the loop is live the pre-existing roster-check message stays.
    agent, game = _make_agent(game_started=True, players=["Dave"])
    msg = _call_award(agent, "Ghost", "spectral vibes")
    assert "No rostered player" in msg
    assert "Ghost" in msg
    assert game.sk.players["Dave"]["score"] == 0
    assert game.events == []
