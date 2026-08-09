"""The published phase must be the phase that was actually reached.

`publish_attributes_nowait` schedules a task and the coroutine READ
`self.ui_phase` when the loop got round to running it — not when the
publish was queued. Two consecutive SYNCHRONOUS transitions therefore
collapsed into whichever ran last. `adjudicate` does exactly that:

    if round_over:
        self._set_ui_phase("scores")     # queues publish A
    if not self.arm_next_question():     # -> "question", queues publish B

Measured against the real publish path before the fix:

    PHASES ON THE WIRE:   ['question', 'question']
    agent-internal ui_phase: question

Three live symptoms, one cause:

  * `phase="scores"` has never reached the wire on a normal round
    boundary, so `LilyStandings` — a whole designed screen — only ever
    rendered when supply was starved enough for arm_next_question to FAIL.
  * The reveal was erased ~1 RTT after it published. The frontend gates
    all reveal rendering on `phase === 'reveal' || 'scores'`, and the
    `reveal` data packet fires at the verdict turn's PLAYOUT START — an
    LLM round-trip later. She said "Correct — Saturn! Point to Maya" over
    a board showing no answer and no verdict. "Go back" showed answers the
    live screen never displayed, because history folds `state.reveal`.
  * Between arm and air, room metadata still held question N's prompt and
    choices while `question_number` read N+1 — so the just-answered
    question was re-animated onto the glass as the live one, under the new
    number, for the whole flourish + LLM + TTS.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_recognition_variety import _make_game


def _wired(game):
    """Record the phase each publish puts ON THE WIRE, reading it exactly
    where publish_attributes reads it."""
    wire = []

    async def _publish(phase=None):
        wire.append(phase or game._phase_hold or game.ui_phase)

    game.publish_attributes = _publish
    return wire


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# -- the defect ---------------------------------------------------------------


def test_two_synchronous_transitions_both_reach_the_wire():
    """THE fixture. The round-boundary shape, exactly as adjudicate runs
    it. Before the fix both publishes said "question"."""
    async def _drive():
        g = _make_game()
        wire = _wired(g)
        g._phase_hold = None
        g.ui_phase = "reveal"
        g._set_ui_phase("scores")
        g._set_ui_phase("question")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return wire

    assert _run(_drive()) == ["scores", "question"], (
        "a phase transition was overwritten before its publish ran"
    )


def test_the_reveal_survives_arming_the_next_question():
    """The reveal is the whole point of the beat. Arming N+1 must not
    yank the board off it before the verdict has even been spoken."""
    async def _drive():
        g = _make_game()
        wire = _wired(g)
        g._phase_hold = None
        g.ui_phase = "reveal"
        # arm_next_question's transition, without the rest of the arm.
        g._phase_hold = g.ui_phase
        g._set_ui_phase("question")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return wire

    assert _run(_drive()) == ["reveal"], (
        "arming the next question erased the reveal from the glass"
    )


def test_the_hold_covers_every_phase_an_arm_can_interrupt():
    """lobby, reveal and scores are all states the board must stay on
    until the next question actually AIRS."""
    from test_bargein_is_normal import _armed_game  # noqa: F401  (import guard)

    for phase in ("lobby", "reveal", "scores"):
        g = _make_game()
        g._phase_hold = None
        g.ui_phase = phase
        if g.ui_phase in ("lobby", "reveal", "scores"):
            g._phase_hold = g.ui_phase
        assert g._phase_hold == phase


def test_the_hold_releases_when_the_question_reaches_air(monkeypatch):
    """PROTECTED. A hold that never clears is a board frozen on the last
    reveal — worse than the bug. publish_question_to_glass owns the
    release, at the moment the room starts hearing the question."""
    from test_bargein_is_normal import _armed_game

    g, rec = _armed_game(monkeypatch, ui_phase="question", phase_hold="reveal")
    g.publish_question_to_glass(reason="playout_started")
    assert g._phase_hold is None


# -- protected ----------------------------------------------------------------


def test_an_explicit_phase_still_wins_over_a_live_read():
    """The scheduled phase is authoritative; a later mutation must not
    rewrite a publish that was already queued."""
    async def _drive():
        g = _make_game()
        wire = _wired(g)
        g._phase_hold = None
        g.ui_phase = "answering"
        g._set_ui_phase("reveal")
        g.ui_phase = "something_else_entirely"  # racing mutation
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return wire

    assert _run(_drive()) == ["reveal"]


def test_a_publish_with_no_scheduled_phase_still_reads_live_state():
    """PROTECTED. The awaited callers (`await self.publish_attributes()`)
    pass nothing and must keep working off current state."""
    async def _drive():
        g = _make_game()
        wire = _wired(g)
        g._phase_hold = None
        g.ui_phase = "final"
        await g.publish_attributes()
        return wire

    assert _run(_drive()) == ["final"]


def test_a_repeated_transition_still_publishes_nothing():
    """PROTECTED. _set_ui_phase only publishes on a CHANGE; binding the
    phase at schedule time must not turn every call into a publish."""
    async def _drive():
        g = _make_game()
        wire = _wired(g)
        g._phase_hold = None
        g.ui_phase = "question"
        g._set_ui_phase("question")
        await asyncio.sleep(0)
        return wire

    assert _run(_drive()) == []
