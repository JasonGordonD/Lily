"""REFACTOR-STAGE-1B-001 P1-1 — the entrypoint's event handlers are
module-level functions that take their state explicitly, and every one of
them registers through the HOTFIX-STT-QUARANTINE-001 guard.

The closures were the mechanism that hid the P0 (no test could call the
`user_input_transcribed` handler, so a TypeError in its quarantine branch
shipped). These tests drive each lifted handler directly — no source-text
slicing — and push the quarantine path through the REAL registration
(`lily_guarded_handler` on a real `livekit.rtc.EventEmitter`) with the real
scorekeeper, the real timestamp reconciler and the real transcript batcher.
"""

import asyncio
import logging
import sys
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from livekit import rtc  # noqa: E402
from livekit.agents.voice.events import UserInputTranscribedEvent  # noqa: E402

import lily_agent  # noqa: E402
import lily_config  # noqa: E402
import lily_persistence  # noqa: E402
from lily_agent import LilyGame  # noqa: E402
from lily_scorekeeper import LilyScorekeeper  # noqa: E402


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

class _Batcher(lily_persistence.LilyTranscriptBatcher):
    """The real writer, never flushing in-test."""

    def __init__(self, session_id):
        super().__init__(object(), session_id)
        self._last_flush = float("inf")


def _game(session_id="lily-S1B-handlers"):
    sk = LilyScorekeeper(session_id)
    game = LilyGame.bare(sk=sk)
    game.supabase = None
    # __init__-only attributes (bare() skips the ctx-bound constructor).
    game.nbest_collector = None
    game.audeering_pipeline = None
    game.fragments = lily_agent.LilyFragmentAccumulator()
    game.game_started = False
    game.publish_user_transcript_nowait = lambda *a, **k: None
    game.publish_attributes_nowait = lambda *a, **k: None
    game.send_event_nowait = lambda *a, **k: None
    return game, sk


class _NBest:
    """A fake n-best collector handing back stream-relative timings that
    make the reconciled span 104 s — the lily-81BCB0 corruption shape the
    WS-10 gate was written against (segment_max_span_seconds default 30)."""

    def __init__(self, start=0.0, end=104.0):
        self.start, self.end = start, end

    def drain(self, speaker_label=None):
        return {"stream_start_time": self.start, "stream_end_time": self.end}


def _final(text, speaker="S1", created_at=None):
    return UserInputTranscribedEvent(
        transcript=text, is_final=True, speaker_id=speaker,
        created_at=created_at if created_at is not None else time.time(),
    )


# ---------------------------------------------------------------------------
# the guard: every lifted handler registers through it
# ---------------------------------------------------------------------------

def test_guarded_handler_swallows_a_typeerror_and_counts_it(caplog):
    game, _ = _game()
    emitter = rtc.EventEmitter()

    def _broken(ev):
        raise TypeError("boom")

    emitter.on("close", lily_agent.lily_guarded_handler("close", game, _broken))
    with caplog.at_level(logging.ERROR, logger="lily_stt_tuning"):
        emitter.emit("close", object())  # must not raise
    assert game._stt_handler_faults == 1
    faults = [r for r in caplog.records if "HANDLER_FAULT" in r.getMessage()]
    assert faults and "handler=close" in faults[0].getMessage()
    assert faults[0].exc_info is not None


def test_guarded_handler_passes_every_positional_arg_through():
    """room `track_subscribed` hands (track, publication, participant)."""
    game, _ = _game()
    seen = []
    handler = lily_agent.lily_guarded_handler(
        "track_subscribed", game, lambda *args: seen.append(args)
    )
    handler("t", "p", "pt")
    handler("t")
    assert seen == [("t", "p", "pt"), ("t",)]
    assert getattr(game, "_stt_handler_faults", 0) == 0


# ---------------------------------------------------------------------------
# user_input_transcribed — the quarantine path, through the real wiring
# ---------------------------------------------------------------------------

def test_quarantined_final_is_stored_with_no_speaker_name_and_never_raises(caplog):
    """The HOTFIX-STT-QUARANTINE-001 defect, now reachable: a 104 s final
    hits the WS-10 span gate (real scorekeeper), the raw text lands in the
    session transcript store with speaker_name None (real batcher), the
    handler returns without a fault, and nothing downstream of the gate
    (voiced-segment note, fragments, transcript event) ran."""
    game, sk = _game()
    game.nbest_collector = _NBest(0.0, 104.0)
    transcripts = _Batcher(sk.session_id)
    downstream = []
    game.note_voiced_segment = lambda *a, **k: downstream.append("voiced")
    game.on_transcript_event = lambda *a, **k: downstream.append("event")

    emitter = rtc.EventEmitter()
    emitter.on(
        "user_input_transcribed",
        lily_agent.lily_guarded_handler(
            "user_input_transcribed", game,
            lambda ev: lily_agent._on_transcribed_body(game, sk, transcripts, ev),
        ),
    )
    assert lily_config.segment_max_span_seconds() < 104.0
    with caplog.at_level(logging.ERROR, logger="lily_stt_tuning"):
        emitter.emit("user_input_transcribed", _final("we were playing via telegram"))

    assert getattr(game, "_stt_handler_faults", 0) == 0
    assert not [r for r in caplog.records if "HANDLER_FAULT" in r.getMessage()]
    assert sk.quarantined_segments and sk.quarantined_segments[-1]["reason"] == "span"
    assert len(transcripts._batch) == 1
    row = transcripts._batch[0]
    assert row["speaker_label"] == "S1"
    assert row.get("speaker_name") is None
    assert row["session_id"] == sk.session_id
    assert "we were playing via telegram" in row["text"]
    assert downstream == []


def test_sane_final_flows_to_the_transcript_event_and_the_store():
    game, sk = _game()
    transcripts = _Batcher(sk.session_id)
    events = []
    game.on_transcript_event = lambda result, text, **k: events.append((result, text))
    game.note_voiced_segment = lambda *a, **k: None
    game.note_intake_overlap = lambda *a, **k: None

    lily_agent._on_transcribed_body(game, sk, transcripts, _final("Jupiter"))

    assert len(events) == 1 and events[0][1] == "Jupiter"
    assert events[0][0]["quarantined"] is False
    assert len(transcripts._batch) == 1
    assert transcripts._batch[0]["speaker_label"] == "S1"


def test_interim_routes_stop_and_stores_nothing():
    game, sk = _game()
    transcripts = _Batcher(sk.session_id)
    routed = []
    game.route_stop_from_interim = lambda text: routed.append(text)
    ev = UserInputTranscribedEvent(
        transcript="[S1] stop stop stop", is_final=False, speaker_id="S1"
    )
    lily_agent._on_transcribed_body(game, sk, transcripts, ev)
    assert routed == ["stop stop stop"]  # engine tag stripped
    assert transcripts._batch == []


def test_a_fault_in_the_transcribed_handler_costs_one_final_not_the_ear(caplog):
    """Registered the way the entrypoint registers it: a raise inside the
    body is a HANDLER_FAULT line + counter, and the emitter returns."""
    game, sk = _game()
    transcripts = _Batcher(sk.session_id)
    game.publish_user_transcript_nowait = lambda *a, **k: (_ for _ in ()).throw(
        TypeError("missing 1 required positional argument")
    )
    emitter = rtc.EventEmitter()
    emitter.on(
        "user_input_transcribed",
        lily_agent.lily_guarded_handler(
            "user_input_transcribed", game,
            lambda ev: lily_agent._on_transcribed_body(game, sk, transcripts, ev),
        ),
    )
    with caplog.at_level(logging.ERROR, logger="lily_stt_tuning"):
        emitter.emit("user_input_transcribed", _final("Jupiter"))
    assert game._stt_handler_faults == 1
    assert any(
        "handler=user_input_transcribed" in r.getMessage() for r in caplog.records
    )


# ---------------------------------------------------------------------------
# the other session handlers
# ---------------------------------------------------------------------------

def test_session_usage_handler_feeds_the_collector():
    seen = []
    metrics = types.SimpleNamespace(collect_session_usage=seen.append)
    lily_agent._on_session_usage_body(metrics, types.SimpleNamespace(usage="U"))
    assert seen == ["U"]


def test_item_added_records_the_assistant_turn_and_feeds_metrics():
    game, sk = _game()
    fed = []
    metrics = types.SimpleNamespace(collect_turn=fed.append)
    metrics_raw = {
        "first_token_latency_ms": [], "tts_first_frame_ms": [], "e2e_latency_ms": [],
    }
    report = {"llm_node_ttft": 0.25, "tts_node_ttfb": 0.1, "e2e_latency": 0.9}
    msg = types.SimpleNamespace(
        role="assistant", id="item_7", content=["Nice one, Rami."], metrics=report,
    )
    lily_agent._on_item_added_body(
        game, metrics, metrics_raw, types.SimpleNamespace(item=msg)
    )
    assert fed == [report]
    assert game._last_assistant_turn == ("item_7", "Nice one, Rami.")
    assert metrics_raw["first_token_latency_ms"] == [250.0]
    assert metrics_raw["tts_first_frame_ms"] == [100.0]
    assert metrics_raw["e2e_latency_ms"] == [900.0]


def test_item_added_user_turn_feeds_metrics_only():
    game, sk = _game()
    fed = []
    metrics = types.SimpleNamespace(collect_turn=fed.append)
    before = game._last_assistant_turn
    msg = types.SimpleNamespace(role="user", content="Jupiter", metrics={"x": 1})
    lily_agent._on_item_added_body(game, metrics, {}, types.SimpleNamespace(item=msg))
    assert fed == [{"x": 1}]
    assert game._last_assistant_turn == before


def test_item_added_ring_buffer_is_capped():
    game, sk = _game()
    metrics = types.SimpleNamespace(collect_turn=lambda r: None)
    metrics_raw = {
        "first_token_latency_ms": [1.0] * lily_agent._METRICS_CAP,
        "tts_first_frame_ms": [], "e2e_latency_ms": [],
    }
    msg = types.SimpleNamespace(
        role="assistant", id="i", content="x", metrics={"llm_node_ttft": 0.5},
    )
    lily_agent._on_item_added_body(game, metrics, metrics_raw, types.SimpleNamespace(item=msg))
    assert len(metrics_raw["first_token_latency_ms"]) == lily_agent._METRICS_CAP
    assert metrics_raw["first_token_latency_ms"][-1] == 500.0


def test_false_interruption_handler_logs_the_ws14_line(caplog):
    _, sk = _game()
    with caplog.at_level(logging.WARNING, logger="lily_agent"):
        lily_agent._on_false_interruption_body(sk, types.SimpleNamespace(resumed=True))
    msgs = [r.getMessage() for r in caplog.records]
    assert any(
        "FALSE_INTERRUPTION" in m and sk.session_id in m and "resumed=True" in m
        for m in msgs
    )


def test_user_state_handler_stamps_both_edges():
    game, sk = _game()
    seen = []
    orig = game.note_user_speech_state
    game.note_user_speech_state = lambda s: (seen.append(s), orig(s))
    lily_agent._on_user_state_body(game, sk, types.SimpleNamespace(new_state="speaking"))
    assert game._user_speaking is True
    lily_agent._on_user_state_body(game, sk, types.SimpleNamespace(new_state="listening"))
    assert game._user_speaking is False
    assert seen == [True, False]


def test_agent_state_handler_marks_playout_and_releases_the_reveal():
    game, sk = _game()
    started, recog, sent = [], [], []
    game.note_playout_started = started.append
    game.note_recognition_playout_started = recog.append
    game.send_event_nowait = lambda t, p: sent.append((t, p))
    game._pending_reveal_event = {"answer": "Jupiter"}
    session = types.SimpleNamespace(current_speech=types.SimpleNamespace(id="speech_3"))

    lily_agent._on_agent_state_body(
        game, sk, session, types.SimpleNamespace(new_state="speaking")
    )
    assert sk.host_speaking is True
    assert started == ["speech_3"] and recog == ["speech_3"]
    assert sent == [("reveal", {"answer": "Jupiter"})]
    assert game._pending_reveal_event is None

    lily_agent._on_agent_state_body(
        game, sk, session, types.SimpleNamespace(new_state="listening")
    )
    assert sk.host_speaking is False
    assert started == ["speech_3"]  # no second playout mark


def test_speech_created_tracks_the_handle_and_reports_playout():
    game, sk = _game()
    finished = []
    game.on_agent_speech_finished = lambda spoken, **k: finished.append((spoken, k))

    class _Handle:
        id = "speech_9"
        interrupted = False
        chat_items = [types.SimpleNamespace(role="assistant", content="Next up!")]

        async def wait_for_playout(self):
            return None

        def exception(self):
            return None

    handle = _Handle()

    async def scenario():
        lily_agent._on_speech_created_body(
            game, types.SimpleNamespace(speech_handle=handle)
        )
        assert "speech_9" in game._speech_handles
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert finished == [(
        "Next up!",
        {"speech_id": "speech_9", "interrupted": False,
         "suppressed": False, "failed": False},
    )]


def test_speech_created_maps_a_failed_generation_to_the_suppressed_path():
    game, sk = _game()
    finished = []
    game.on_agent_speech_finished = lambda spoken, **k: finished.append(k)

    class _Handle:
        id = "speech_10"
        interrupted = False
        chat_items = []

        async def wait_for_playout(self):
            return None

        def exception(self):
            return RuntimeError("tts died")

    async def scenario():
        lily_agent._on_speech_created_body(
            game, types.SimpleNamespace(speech_handle=_Handle())
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert finished == [{
        "speech_id": "speech_10", "interrupted": False,
        "suppressed": True, "failed": True,
    }]


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------

def _close_stubs(game, sk, monkeypatch):
    game.stop_idle_watchdog = lambda: None
    game._voice_identity_finalize = lambda: None

    async def _enroll():
        return True

    game._voice_identity_enroll_at_close = _enroll
    game._players_payload = lambda: []
    game.identity_persistence_allowed = lambda: False

    async def _tuning(supabase):
        return None

    import lily_bank_tuning
    monkeypatch.setattr(lily_bank_tuning, "lily_run_bank_tuning", _tuning)


def test_close_handler_persists_metadata_and_releases_the_gate(monkeypatch):
    game, sk = _game()
    _close_stubs(game, sk, monkeypatch)
    ended = []

    async def _session_end(supabase, scorekeeper, *, final_standings, metadata):
        ended.append((scorekeeper.session_id, metadata))

    monkeypatch.setattr(lily_persistence, "lily_session_end", _session_end)
    transcripts = _Batcher(sk.session_id)
    flushed = []

    async def _flush():
        flushed.append(True)

    transcripts.flush = _flush

    async def scenario():
        heartbeat_stop, shutdown_gate = asyncio.Event(), asyncio.Event()
        lily_agent._on_close_body(
            game=game, scorekeeper=sk, transcripts=transcripts, supabase=None,
            stt=None, metrics_raw={"e2e_latency_ms": [1.0]}, session_metrics=None,
            heartbeat_stop=heartbeat_stop, shutdown_gate=shutdown_gate, ev=None,
        )
        await asyncio.wait_for(shutdown_gate.wait(), timeout=2.0)
        assert heartbeat_stop.is_set()

    asyncio.run(scenario())
    assert flushed == [True]
    assert ended and ended[0][0] == sk.session_id
    assert "session_metrics" in ended[0][1] and "voice_identity" in ended[0][1]


def test_close_handler_releases_the_gate_when_persistence_raises(monkeypatch, caplog):
    game, sk = _game()
    _close_stubs(game, sk, monkeypatch)

    async def _session_end(*a, **k):
        raise RuntimeError("db gone")

    monkeypatch.setattr(lily_persistence, "lily_session_end", _session_end)
    transcripts = _Batcher(sk.session_id)

    async def _flush():
        return None

    transcripts.flush = _flush

    async def scenario():
        heartbeat_stop, shutdown_gate = asyncio.Event(), asyncio.Event()
        with caplog.at_level(logging.ERROR, logger="lily_agent"):
            lily_agent._on_close_body(
                game=game, scorekeeper=sk, transcripts=transcripts, supabase=None,
                stt=None, metrics_raw={}, session_metrics=None,
                heartbeat_stop=heartbeat_stop, shutdown_gate=shutdown_gate, ev=None,
            )
            await asyncio.wait_for(shutdown_gate.wait(), timeout=2.0)

    asyncio.run(scenario())
    assert any("SESSION_CLOSE | persistence error" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# room handlers
# ---------------------------------------------------------------------------

def test_participant_connected_stages_a_late_device_candidate():
    game, sk = _game()
    game.group_id = "lily-room"
    game.group_id_source = "room_name"
    game.device_candidate_group_id = None
    staged = []

    async def _stage(candidate, source):
        staged.append((candidate, source))
        return True

    game.stage_device_candidate = _stage
    participant = types.SimpleNamespace(
        kind=rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD,
        metadata='{"lily_group_id": "device-42"}',
    )

    async def scenario():
        lily_agent._on_participant_connected_body(game, participant)
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert staged == [("device-42", "participant_metadata_late")]


def test_participant_connected_ignores_agents_and_strong_sources():
    game, sk = _game()
    game.group_id = "lily-room"
    game.group_id_source = "room_name"
    game.device_candidate_group_id = None
    staged = []

    async def _stage(candidate, source):
        staged.append(candidate)

    game.stage_device_candidate = _stage
    agent = types.SimpleNamespace(
        kind=rtc.ParticipantKind.PARTICIPANT_KIND_AGENT,
        metadata='{"lily_group_id": "device-42"}',
    )
    lily_agent._on_participant_connected_body(game, agent)
    import lily_identity
    game.group_id_source = next(iter(lily_identity._STRONG_GROUP_SOURCES))
    human = types.SimpleNamespace(
        kind=rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD,
        metadata='{"lily_group_id": "device-42"}',
    )
    lily_agent._on_participant_connected_body(game, human)
    assert staged == []


def test_track_subscribed_opens_the_camera_lane_and_forks_once(monkeypatch):
    game, sk = _game()
    game.camera_lane_status = lambda: {"available": True}
    lanes = []
    game.sk.set_camera_lane = lanes.append
    forks = []

    async def _fork(track, g):
        forks.append(track)

    monkeypatch.setattr(lily_agent, "_lily_camera_frame_fork", _fork)
    track = types.SimpleNamespace(kind=rtc.TrackKind.KIND_VIDEO)
    human = types.SimpleNamespace(kind=rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD)

    async def scenario():
        lily_agent._on_track_subscribed_body(game, None, track, None, human)
        lily_agent._on_track_subscribed_body(game, None, track, None, human)
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert lanes == ["open", "open"]
    assert forks == [track]  # one sink per camera track


def test_track_subscribed_ignores_the_agents_own_track(monkeypatch):
    game, sk = _game()
    lanes = []
    game.sk.set_camera_lane = lanes.append
    track = types.SimpleNamespace(kind=rtc.TrackKind.KIND_VIDEO)
    agent = types.SimpleNamespace(kind=rtc.ParticipantKind.PARTICIPANT_KIND_AGENT)
    lily_agent._on_track_subscribed_body(game, None, track, None, agent)
    assert lanes == []


def test_track_subscribed_forks_audio_to_audeering(monkeypatch):
    game, sk = _game()
    game._voice_capture_allowed = lambda: False
    forks = []

    async def _fork(track, pipeline):
        forks.append((track, pipeline))

    import lily_audeering_client
    monkeypatch.setattr(lily_audeering_client, "lily_audeering_audio_fork", _fork)
    track = types.SimpleNamespace(kind=rtc.TrackKind.KIND_AUDIO)
    human = types.SimpleNamespace(kind=rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD)

    async def scenario():
        lily_agent._on_track_subscribed_body(game, "PIPE", track, None, human)
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert forks == [(track, "PIPE")]
