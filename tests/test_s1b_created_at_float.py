"""REFACTOR-STAGE-1B-001 P2-2 — `ev.created_at` is a float epoch in
livekit-agents 1.6.10 (livekit/agents/voice/events.py:
`created_at: float = Field(default_factory=time.time)` on
UserInputTranscribedEvent), not a datetime. The transcribed handler's
`created.timestamp() if hasattr(created, "timestamp") else time.time()` was
therefore always the fallback: arrival_ts was HANDLER time, so a final
delayed in the framework's queue (a long emit chain, a slow earlier
handler) was stamped late, and the reconciler's "first answered first"
ordering keyed off the wrong clock. Verified against the installed package
source before the change (see test_installed_event_carries_a_float).
"""

import datetime as _dt
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from livekit.agents.voice.events import UserInputTranscribedEvent  # noqa: E402

import lily_agent  # noqa: E402
import lily_persistence  # noqa: E402
from lily_agent import LilyGame  # noqa: E402


def test_installed_event_carries_a_float():
    """The premise, executed against the installed package: no datetime."""
    ev = UserInputTranscribedEvent(transcript="x", is_final=True)
    assert isinstance(ev.created_at, float)
    assert not hasattr(ev.created_at, "timestamp")


def test_arrival_ts_accepts_a_float_epoch():
    assert lily_agent.lily_event_arrival_ts(1_700_000_000.25) == 1_700_000_000.25
    assert lily_agent.lily_event_arrival_ts(1_700_000_000) == 1_700_000_000.0


def test_arrival_ts_keeps_datetime_for_safety():
    when = _dt.datetime(2026, 9, 6, 12, 0, tzinfo=_dt.timezone.utc)
    assert lily_agent.lily_event_arrival_ts(when) == when.timestamp()


def test_arrival_ts_falls_back_to_now_for_anything_else():
    before = time.time()
    got = lily_agent.lily_event_arrival_ts(None)
    assert before <= got <= time.time()
    assert lily_agent.lily_event_arrival_ts("not a clock", fallback=42.0) == 42.0
    assert lily_agent.lily_event_arrival_ts(True, fallback=42.0) == 42.0  # bool is not a clock


class _Sk:
    """Scorekeeper stand-in recording the clock the handler passes."""

    def __init__(self):
        self.session_id = "lily-S1B-clock"
        self.calls = []

    def on_transcript_segment(self, **kwargs):
        self.calls.append(kwargs)
        return {"quarantined": False, "player": None, "utterance_id": "u1"}


def test_transcribed_handler_stamps_the_events_own_clock():
    """End to end through the lifted handler: a final whose created_at is
    an hour ago must reach the scorekeeper with now == created_at (and the
    reconciled segment start on that clock), not handler time."""
    sk = _Sk()
    game = LilyGame.bare(sk=sk)
    game.nbest_collector = None
    game.fragments = lily_agent.LilyFragmentAccumulator()
    game.game_started = False
    game.publish_user_transcript_nowait = lambda *a, **k: None
    game.note_voiced_segment = lambda *a, **k: None
    game.note_intake_overlap = lambda *a, **k: None
    game.on_transcript_event = lambda *a, **k: None
    game.note_confirmed_name_evidence = lambda *a, **k: None
    transcripts = lily_persistence.LilyTranscriptBatcher(object(), sk.session_id)
    transcripts._last_flush = float("inf")

    an_hour_ago = time.time() - 3600.0
    ev = UserInputTranscribedEvent(
        transcript="Jupiter", is_final=True, speaker_id="S1", created_at=an_hour_ago,
    )
    lily_agent._on_transcribed_body(game, sk, transcripts, ev)

    assert len(sk.calls) == 1
    assert sk.calls[0]["now"] == an_hour_ago
    assert sk.calls[0]["segment_start_time"] == an_hour_ago
    assert transcripts._batch[0]["segment_start"] == an_hour_ago
