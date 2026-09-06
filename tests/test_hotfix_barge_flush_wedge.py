"""HOTFIX-BARGE-FLUSH-001 — live 2026-09-06 11:59:33 UTC (session
lily-FCE88B-7a2b83e1, OTel bundle): after "Hi, this is Rami" the turn
commit cancelled a NEVER-AIRED preemptive generation (the late-recognition
carrier); Y7's cut classifier called that cancellation a deliberate human
barge (the human HAD just spoken), and AIRGATE-001 D2's queue flush then
cancelled the organic reply to that very utterance, dispatched 30 ms
earlier — `LILY_SPEECH | CANCELLED speech_5a02dd… reason=user_barge_flush`,
"yielding the floor (no auto-resume, no re-air, no regeneration)". Total
silence for the rest of the call.

Two guards close it: (1) a cut speech that never STARTED playout cannot
have been barged by a human — the flush (and the user-cut counter) require
the cut speech to have aired; (2) the flush never cancels a dispatch
created at or after the latest user final — that is the reply to the turn
that "barged", the one thing the room is owed.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_airgate_001 import _Handle, _game  # noqa: E402


def _human_starts_speaking(game):
    """The VAD rising edge (session user_state_changed) — precedes every
    dispatch made for what the human is saying."""
    game.note_user_speech_state(True)


def _human_just_spoke(game):
    game.note_user_speech_state(False)
    game._last_user_turn_at = time.monotonic()


def test_turn_commit_cancelling_an_unaired_preemptive_does_not_flush_the_reply():
    game = _game()
    _human_starts_speaking(game)  # 11:59:26 — "Hi, this is Rami" begins
    game.note_user_final()  # 11:59:29 — the final lands
    preemptive = _Handle("speech_preemptive")  # late-recognition carrier
    game.note_speech_handle(preemptive)
    reply = _Handle("speech_reply")  # the organic reply to the same turn
    game.note_speech_handle(reply)
    _human_just_spoke(game)

    # The framework cancels the never-aired preemptive at turn commit.
    game.on_agent_speech_finished(
        "", speech_id="speech_preemptive", interrupted=True,
    )

    assert reply.interrupts == [], (
        "the reply to the barging turn was flushed — the 11:59 silence wedge"
    )
    assert "speech_reply" in (game._speech_handles or {})


def test_a_real_barge_on_aired_speech_flushes_stale_queue_but_keeps_the_reply():
    game = _game()
    stale = _Handle("speech_stale")  # a composite queued BEFORE the human spoke
    game.note_speech_handle(stale)
    _human_starts_speaking(game)  # the barge begins
    game.note_user_final()
    aired = _Handle("speech_aired")
    game.note_speech_handle(aired)
    game.note_playout_started("speech_aired")  # this one is on the air
    reply = _Handle("speech_reply")  # dispatched for the new turn
    game.note_speech_handle(reply)
    _human_just_spoke(game)

    game.on_agent_speech_finished(
        "", speech_id="speech_aired", interrupted=True,
    )

    assert stale.interrupts == [True], "the stale pre-final composite must still flush"
    assert reply.interrupts == [], "the reply to the barging turn must survive"
