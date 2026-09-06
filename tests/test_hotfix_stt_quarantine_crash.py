"""HOTFIX-STT-QUARANTINE-001 — broken-code sweep P0-1 (d4d79e3).

The WS-10 quarantine branch of the `user_input_transcribed` handler stored
the insane final with `transcripts.add(text, speaker_label=…,
segment_start=…, segment_end=…)` — no `speaker_name`, which
`LilyTranscriptBatcher.add` required positionally. Executed repro:
`TypeError: LilyTranscriptBatcher.add() missing 1 required positional
argument: 'speaker_name'`.

Blast radius (verified against livekit-agents 1.6.10 source, executed):
`livekit.rtc.EventEmitter.emit` re-raises TypeError out of a handler (it
logs every other exception class); AgentSession emits the event from inside
AudioRecognition's `_stt_consumer` loop (`async for ev in event_ch: await
self._on_stt_event(ev)`), so the raise kills the consumer — no restart —
and the session is deaf for the rest of the call. The quarantine gate is a
live production path (its thresholds cite 104 s / 206 s spans and a
3.5-minute-late final from real sessions).

Two guards: the writer accepts the quarantine call shape, and every
Lily-side handler fault costs one final, never the ear.
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_persistence  # noqa: E402
import lily_stt_tuning  # noqa: E402


def _batcher():
    b = lily_persistence.LilyTranscriptBatcher(object(), "lily-TEST-quarantine")
    b._last_flush = float("inf")  # never trips the time-based flush in-test
    return b


def test_quarantined_final_write_shape_is_accepted():
    """The exact call the quarantine branch makes: no speaker_name."""
    b = _batcher()
    b.add(
        "we were playing via telegram",
        speaker_label="S1",
        segment_start=104.2,
        segment_end=206.9,
    )
    assert len(b._batch) == 1
    row = b._batch[0]
    assert row["speaker_label"] == "S1"
    assert row.get("speaker_name") is None
    assert row["session_id"] == "lily-TEST-quarantine"


def test_handler_fault_never_kills_the_framework_consumer(caplog):
    """Registered through the real EventEmitter: an unguarded TypeError
    re-raises (the framework's contract); the guarded handler logs the
    fault, counts it on the game, and the emit returns."""
    from livekit import rtc

    class _Game:
        pass

    def _broken(ev):
        raise TypeError("missing 1 required positional argument: 'speaker_name'")

    unguarded = rtc.EventEmitter()
    unguarded.on("user_input_transcribed", _broken)
    raised = False
    try:
        unguarded.emit("user_input_transcribed", object())
    except TypeError:
        raised = True
    assert raised, "the framework re-raises TypeError — that is the defect's path"

    game = _Game()
    guarded = rtc.EventEmitter()
    guarded.on(
        "user_input_transcribed",
        lambda ev: lily_stt_tuning.lily_run_stt_handler(
            _broken, ev, game=game, name="user_input_transcribed"
        ),
    )
    with caplog.at_level(logging.ERROR, logger="lily_stt_tuning"):
        guarded.emit("user_input_transcribed", object())  # must not raise
    assert game._stt_handler_faults == 1
    faults = [r for r in caplog.records if "HANDLER_FAULT" in r.getMessage()]
    assert faults and faults[0].exc_info is not None
    assert "handler=user_input_transcribed" in faults[0].getMessage()


def test_healthy_handler_runs_and_counts_nothing():
    seen = []

    class _Game:
        pass

    game = _Game()
    lily_stt_tuning.lily_run_stt_handler(seen.append, "final", game=game)
    assert seen == ["final"]
    assert getattr(game, "_stt_handler_faults", 0) == 0
