"""
WS-11 (WO-LILY-OMNIBUS-003) — telemetry restoration.

Session lily-81BCB0-583a0f16 evidence (live DB, 2026-08-05): zero
lily_acoustic_trajectories rows fleet-wide with every addressee
acoustic_snapshot explicit-null — the Audeering lane was OFFLINE at
preflight (devAIce durationQuota=0 opens the breaker), not
running-without-persistence. n_best_dispersion was 0.000 on every row
because tap-only mode (LILY_STT_MAX_ALTERNATIVES=1) synthesizes exactly
one hypothesis per utterance — variance of a single confidence is
structurally zero even when the recognizer was torn word-by-word.
overlap_flag never fired because a final whose drain came up empty
(raw-word speaker tags disagreeing with the event's speaker_id in an
echo room) falls back to a degenerate zero-length arrival span that the
strict-epsilon gate can never flip. Garbled finals (mean word confidence
0.57–0.63) were scored/engaged instead of clarified.

These tests pin the repaired paths:
  1. drain() dispersion falls back to per-word confidence variance on
     single-hypothesis sets (nonzero on ambiguous finals).
  2. drain(speaker_label=...) retries unfiltered when the labeled take is
     empty — restoring stream times, so overlap spans stay real.
  3. Overlap flags fire on constructed overlapping cross-speaker speech.
  4. A garbled final triggers the clarify posture, not content engagement,
     within the existing per-question/per-session clarify caps.
  5. One lily_acoustic_trajectories row per finalized user turn when the
     pipeline is healthy; none (unchanged contract) when offline.
  6. Offline reason is durably visible: pipeline breaker reason surfaces
     in build_game_stats()["acoustic_lane"].
  7. lily_log_addressee fail-soft: pre-DDL prod (migration 018 not yet
     applied) never loses a corpus row — the write retries without the
     new telemetry keys.
  8. agent_classification derives per utterance from the segment result.
"""

import asyncio
import time

import pytest

import lily_audeering_client
import lily_audeering_consumers
import lily_config
import lily_nbest
import lily_persistence
import lily_say_gate
from lily_agent import LilyGame
from lily_nbest import LilyNBestCollector
from lily_scorekeeper import LilyScorekeeper


def _add_transcript(*results):
    return {"message": "AddTranscript", "results": list(results)}


def _raw_word(content, confidence, speaker="S1", start=0.0, extra_alts=()):
    alts = [{"content": content, "confidence": confidence, "speaker": speaker}]
    alts.extend(
        {"content": c, "confidence": conf, "speaker": speaker}
        for c, conf in extra_alts
    )
    return {
        "type": "word", "start_time": start, "end_time": start + 0.3,
        "alternatives": alts,
    }


# ---------------------------------------------------------------------------
# (1) Dispersion — word-confidence fallback on single-hypothesis sets
# ---------------------------------------------------------------------------

def test_drain_single_hypothesis_dispersion_from_word_confidences():
    col = LilyNBestCollector()
    col.ingest_message(_add_transcript(
        _raw_word("ninja", 0.95, start=1.0),
        _raw_word("girl", 0.55, start=1.4),
        _raw_word("5050", 0.60, start=1.8),
    ))
    out = col.drain()
    assert len(out["hypotheses"]) == 1
    assert out["dispersion"] > 0.0
    assert out["dispersion_source"] == "word_confidence_variance"
    assert out["mean_word_confidence"] == pytest.approx(0.7, abs=1e-6)
    assert out["min_word_confidence"] == pytest.approx(0.55)


def test_drain_multi_hypothesis_keeps_hypothesis_variance():
    col = LilyNBestCollector(max_hypotheses=3)
    col.ingest_message(_add_transcript(
        _raw_word("madrid", 0.9, extra_alts=(("mad rid", 0.4),)),
    ))
    out = col.drain()
    assert len(out["hypotheses"]) > 1
    assert out["dispersion_source"] == "hypothesis_variance"
    assert out["dispersion"] == lily_nbest.lily_nbest_dispersion(
        out["hypotheses"]
    )


def test_drain_uniform_word_confidences_stay_zero():
    col = LilyNBestCollector()
    col.ingest_message(_add_transcript(
        _raw_word("paris", 0.9, start=1.0),
        _raw_word("france", 0.9, start=1.4),
    ))
    out = col.drain()
    assert out["dispersion"] == 0.0


# ---------------------------------------------------------------------------
# (2) Drain fallback — labeled miss must not produce an empty (span-less) set
# ---------------------------------------------------------------------------

def test_drain_label_mismatch_falls_back_unfiltered():
    col = LilyNBestCollector()
    col.ingest_message(_add_transcript(
        _raw_word("tokyo", 0.8, speaker="Rami", start=2.0),
        _raw_word("tower", 0.7, speaker="Rami", start=2.4),
    ))
    out = col.drain(speaker_label="S4")
    assert out is not None
    assert out["word_count"] == 2
    assert out["speaker_filter_fallback"] is True
    assert out["stream_start_time"] == 2.0
    # Buffer fully consumed by the fallback.
    assert col.drain() is None


def test_drain_labeled_match_does_not_fall_back():
    col = LilyNBestCollector()
    col.ingest_message(_add_transcript(
        _raw_word("madrid", 0.9, speaker="S1", start=1.0),
        _raw_word("lisbon", 0.8, speaker="S2", start=1.5),
    ))
    s1 = col.drain(speaker_label="S1")
    assert s1["hypotheses"][0]["text"] == "madrid"
    assert s1["speaker_filter_fallback"] is False
    s2 = col.drain(speaker_label="S2")
    assert s2["hypotheses"][0]["text"] == "lisbon"


# ---------------------------------------------------------------------------
# (3) Overlap — constructed overlapping cross-speaker speech flips the flag
# ---------------------------------------------------------------------------

def test_overlap_fires_on_constructed_overlapping_speech():
    sk = LilyScorekeeper("test-room")
    sk.bind_speaker("S1", "Sarah")
    sk.bind_speaker("S2", "Rami")
    t0 = time.time()
    sk.open_answer_window(duration=20.0, now=t0)
    sk.on_transcript_segment(
        text="the eiffel tower", speaker_label="S1",
        segment_start_time=t0 + 1.0, segment_end_time=t0 + 3.0, now=t0 + 3.2,
    )
    assert sk.overlap_flag is False
    r2 = sk.on_transcript_segment(
        text="no it's the louvre", speaker_label="S2",
        segment_start_time=t0 + 2.0, segment_end_time=t0 + 4.0, now=t0 + 4.2,
    )
    assert sk.overlap_flag is True
    assert r2["overlap_flag"] is True


def _drive_seam(sk, reconciler, collector, speaker_label, words, arrival_ts, t):
    """Reproduce the production seam (lily_agent.on_transcript_event):
    drain(speaker_label) -> reconcile(stream times) -> on_transcript_segment.
    No explicit segment times — the overlap span comes ONLY from what the
    drain recovered, so this exercises the label-mismatch fallback fix."""
    collector.ingest_message(_add_transcript(*words))
    nbest = collector.drain(speaker_label=speaker_label)
    timing = reconciler.reconcile(
        arrival_ts=arrival_ts,
        stream_start=(nbest or {}).get("stream_start_time"),
        stream_end=(nbest or {}).get("stream_end_time"),
    )
    seg_start = float(timing["start_time"])
    seg_end = float(timing["end_time"])
    sk.on_transcript_segment(
        text=" ".join(w["alternatives"][0]["content"] for w in words),
        speaker_label=speaker_label,
        segment_start_time=seg_start,
        segment_end_time=seg_end,
        now=arrival_ts,
    )
    return nbest, timing


def test_overlap_fires_from_drain_recovered_times_on_label_mismatch():
    # Echo room: each utterance's per-word tags disagree with the event's
    # speaker_id, so the labeled drain misses and falls back to the whole
    # buffer — the fix that keeps the recovered stream times. Two such
    # utterances overlap in stream time; the recovered times must flip the
    # overlap gate. Before the fix, drain returned None, the reconciler
    # produced degenerate arrival-point spans, and overlap never fired.
    sk = LilyScorekeeper("test-room")
    reconciler = lily_nbest.LilyTimestampReconciler()
    col = LilyNBestCollector()
    t0 = 1.0
    sk.open_answer_window(duration=30.0, now=t0)

    # S1 speaks [1.0, 3.0]; words tagged GHOST (mismatch), arrival=stream_end.
    n1, _ = _drive_seam(
        sk, reconciler, col, "S1",
        [_raw_word("the", 0.8, speaker="GHOST", start=1.0),
         _raw_word("answer", 0.8, speaker="GHOST", start=2.7)],
        arrival_ts=3.0, t=t0,
    )
    assert n1["speaker_filter_fallback"] is True
    assert n1["stream_start_time"] == 1.0 and n1["stream_end_time"] == 3.0
    assert sk.overlap_flag is False

    # S2 speaks [2.0, 4.0]; overlaps S1 by ~1.0s. Same mismatch/fallback.
    n2, _ = _drive_seam(
        sk, reconciler, col, "S2",
        [_raw_word("no", 0.8, speaker="GHOST2", start=2.0),
         _raw_word("louvre", 0.8, speaker="GHOST2", start=3.7)],
        arrival_ts=4.0, t=t0,
    )
    assert n2["speaker_filter_fallback"] is True
    assert sk.overlap_flag is True


def test_overlap_stays_dark_when_drain_yields_no_times():
    # Control: with no buffered words the drain yields nothing, the
    # reconciler degrades to arrival-point spans, and two finals arriving
    # ~1s apart do NOT overlap — proving it's the RECOVERED times (not the
    # arrival clock) that flip the gate in the test above.
    sk = LilyScorekeeper("test-room")
    reconciler = lily_nbest.LilyTimestampReconciler()
    col = LilyNBestCollector()
    t0 = 1.0
    sk.open_answer_window(duration=30.0, now=t0)
    for label, arrival in (("S1", 3.0), ("S2", 4.0)):
        nbest = col.drain(speaker_label=label)  # empty buffer -> None
        timing = reconciler.reconcile(
            arrival_ts=arrival, stream_start=(nbest or {}).get("stream_start_time"),
            stream_end=(nbest or {}).get("stream_end_time"),
        )
        assert timing["source"] == "arrival_time"
        sk.on_transcript_segment(
            text="x", speaker_label=label,
            segment_start_time=float(timing["start_time"]),
            segment_end_time=float(timing["end_time"]),
            now=arrival,
        )
    assert sk.overlap_flag is False


def test_degenerate_spans_never_flip_overlap():
    sk = LilyScorekeeper("test-room")
    t0 = time.time()
    sk.open_answer_window(duration=20.0, now=t0)
    sk.on_transcript_segment(
        text="one", speaker_label="S1",
        segment_start_time=t0 + 1.0, segment_end_time=t0 + 1.0, now=t0 + 1.0,
    )
    sk.on_transcript_segment(
        text="two", speaker_label="S2",
        segment_start_time=t0 + 1.0, segment_end_time=t0 + 1.0, now=t0 + 1.0,
    )
    assert sk.overlap_flag is False


# ---------------------------------------------------------------------------
# (4) Garble gate — low word confidence triggers clarify, not chit-chat
# ---------------------------------------------------------------------------

def _garbled_nbest(mean=0.58):
    return {
        "hypotheses": [{"text": "ninja girl 5050 first dates",
                        "confidence": mean}],
        "dispersion": 0.02,
        "dispersion_source": "word_confidence_variance",
        "word_count": 5,
        "mean_word_confidence": mean,
        "min_word_confidence": mean - 0.05,
        "source": "per_word_synthesis",
    }


def _clean_nbest():
    return _garbled_nbest(mean=0.95)


def test_garble_detector_thresholds():
    assert lily_nbest.lily_nbest_garbled(
        _garbled_nbest(), min_mean_confidence=0.65) is True
    assert lily_nbest.lily_nbest_garbled(
        _clean_nbest(), min_mean_confidence=0.65) is False
    assert lily_nbest.lily_nbest_garbled(None, min_mean_confidence=0.65) is False
    # Single-word interjections never trip the gate.
    short = _garbled_nbest()
    short["word_count"] = 1
    assert lily_nbest.lily_nbest_garbled(short, min_mean_confidence=0.65) is False


class _FakeSession:
    def __init__(self):
        self.instructions = []

    def generate_reply(self, instructions):
        self.instructions.append(instructions)


class _FakeAgentHandle:
    def set_preemptive_generation(self, enabled):
        pass


def _make_game():
    game = LilyGame.bare()
    game.session = _FakeSession()
    game.agent = _FakeAgentHandle()
    game._preemptive_paused = False
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game.sk = LilyScorekeeper("test-room")
    game.sk.bind_speaker("S1", "Sarah")
    game.supabase = None
    game.pending_clarify = {}
    game._addressee_rows = {}
    game.acoustic = lily_audeering_consumers.LilyAcousticState()
    game.events = []
    game.send_event_nowait = lambda t, p: game.events.append((t, p))
    game.game_started = True
    game.game_over = False
    game.forget_state = None
    return game


def _result(player="Sarah", recorded=False):
    return {
        "player": player,
        "candidate_recorded": recorded,
        "control_command": None,
        "media_choice": None,
        "system_directed": False,
        "prior_state": "OPEN_WINDOW",
        "overlap_flag": False,
    }


def test_garbled_final_fires_clarify_posture():
    game = _make_game()
    t0 = time.time()
    game.sk.open_answer_window(duration=20.0, now=t0)
    game._maybe_fire_confidence_clarify(
        _result(), "S1", t0 + 2.0, _garbled_nbest()
    )
    assert len(game.session.instructions) == 1
    assert game._session_clarify_count == 1


def test_clean_final_does_not_fire_clarify():
    game = _make_game()
    t0 = time.time()
    game.sk.open_answer_window(duration=20.0, now=t0)
    game._maybe_fire_confidence_clarify(
        _result(), "S1", t0 + 2.0, _clean_nbest()
    )
    assert game.session.instructions == []


def test_garble_clarify_respects_per_question_and_session_caps():
    game = _make_game()
    t0 = time.time()
    game.sk.open_answer_window(duration=20.0, now=t0)
    game._maybe_fire_confidence_clarify(
        _result(), "S1", t0 + 2.0, _garbled_nbest()
    )
    # Same question: capped at once.
    game._maybe_fire_confidence_clarify(
        _result(), "S1", t0 + 3.0, _garbled_nbest()
    )
    assert len(game.session.instructions) == 1
    # Session cap.
    for q in range(2, 10):
        game.sk.question_number = q
        game.sk.open_answer_window(duration=20.0, now=t0)
        game._maybe_fire_confidence_clarify(
            _result(), "S1", t0 + 2.0, _garbled_nbest()
        )
    assert (
        len(game.session.instructions)
        <= lily_config.clarify_max_per_session()
    )


def test_garble_clarify_needs_open_window():
    game = _make_game()
    game._maybe_fire_confidence_clarify(
        _result(), "S1", time.time(), _garbled_nbest()
    )
    assert game.session.instructions == []


def test_garble_clarify_positive_framing():
    game = _make_game()
    t0 = time.time()
    game.sk.open_answer_window(duration=20.0, now=t0)
    game._maybe_fire_confidence_clarify(
        _result(), "S1", t0 + 2.0, _garbled_nbest()
    )
    text = game.session.instructions[0].lower()
    for banned in ("transcription", "confidence", "audio quality", "stt",
                   "system", "detection"):
        assert banned not in text


# ---------------------------------------------------------------------------
# (5) Trajectory persistence — one row per finalized user turn when healthy
# ---------------------------------------------------------------------------

class _TrajQuery:
    def __init__(self, db):
        self.db = db
        self.row = None

    def insert(self, row):
        self.row = dict(row)
        return self

    def execute(self):
        self.db.inserted.append(self.row)
        return type("R", (), {"data": [dict(self.row, id=len(self.db.inserted))]})()


class _TrajDB:
    def __init__(self):
        self.inserted = []

    def table(self, name):
        assert name == "lily_acoustic_trajectories"
        return _TrajQuery(self)


def _healthy_parsed():
    return {
        "dimension": {"arousal": 0.2, "valence": 0.1, "dominance": 0.0},
        "audioQuality": {"snr": 20.0},
    }


def test_one_trajectory_row_per_turn_when_pipeline_healthy():
    game = _make_game()
    db = _TrajDB()
    game.supabase = db
    game._user_turn_index = 0
    game.acoustic.record_response(_healthy_parsed())

    async def _run():
        for _ in range(3):
            game.log_acoustic_trajectory()
        await asyncio.sleep(0.05)

    asyncio.run(_run())
    assert len(db.inserted) == 3
    assert sorted(r["turn_index"] for r in db.inserted) == [1, 2, 3]
    assert db.inserted[0]["session_id"] == game.sk.session_id
    assert db.inserted[0]["dimension"] == {
        "arousal": 0.2, "valence": 0.1, "dominance": 0.0,
    }


def test_no_trajectory_row_when_lane_offline():
    game = _make_game()
    db = _TrajDB()
    game.supabase = db
    game._user_turn_index = 0

    async def _run():
        game.log_acoustic_trajectory()
        await asyncio.sleep(0.05)

    asyncio.run(_run())
    assert db.inserted == []
    # Turn index still advances — the trail stays per-turn aligned.
    assert game._user_turn_index == 1


# ---------------------------------------------------------------------------
# (6) Offline reason — durable in the session report's game_stats
# ---------------------------------------------------------------------------

def test_breaker_reason_recorded_on_pipeline(monkeypatch):
    monkeypatch.setattr(lily_config, "audeering_api_key", lambda: None)
    state = lily_audeering_consumers.LilyAcousticState()
    pipeline = lily_audeering_client.LilyAudeeringPipeline(state)
    assert pipeline.breaker_open is True
    health = pipeline.lane_health()
    assert health["breaker_open"] is True
    assert "AUDEERING_API_KEY" in health["reason"]
    assert health["uploads_this_session"] == 0


def test_game_stats_carries_acoustic_lane_health(monkeypatch):
    monkeypatch.setattr(lily_config, "audeering_api_key", lambda: None)
    game = _make_game()
    game.highlights = []
    game.session_started_at = time.time()
    game.audeering_pipeline = lily_audeering_client.LilyAudeeringPipeline(
        lily_audeering_consumers.LilyAcousticState()
    )
    stats = game.build_game_stats([])
    lane = stats["acoustic_lane"]
    assert lane["breaker_open"] is True
    assert "AUDEERING_API_KEY" in lane["reason"]


def test_game_stats_acoustic_lane_absent_pipeline():
    game = _make_game()
    game.highlights = []
    game.session_started_at = time.time()
    game.audeering_pipeline = None
    stats = game.build_game_stats([])
    assert stats["acoustic_lane"] == {
        "breaker_open": True, "reason": "pipeline_absent",
    }


# ---------------------------------------------------------------------------
# (7) Addressee fail-soft write — pre-DDL prod never loses a corpus row
# ---------------------------------------------------------------------------

class _AddresseeQuery:
    def __init__(self, db):
        self.db = db
        self.row = None

    def insert(self, row):
        self.row = dict(row)
        return self

    # Telemetry columns span FL-1's migration 018 AND WS-11's migration 019.
    _TELEMETRY = (
        "agent_classification", "addressee_score", "addressee_score_components",
        "side_cluster_id", "side_cluster_event",
        "timing_source", "timing_drift_seconds",
    )

    def execute(self):
        if self.db.reject_telemetry and any(
            k in self.row for k in self._TELEMETRY
        ):
            raise RuntimeError(
                "PGRST204: column lily_addressee_log.timing_source "
                "does not exist"
            )
        self.db.inserted.append(self.row)
        return type("R", (), {"data": [dict(self.row, id=len(self.db.inserted))]})()


class _AddresseeDB:
    def __init__(self, reject_telemetry=False):
        self.reject_telemetry = reject_telemetry
        self.inserted = []

    def table(self, name):
        assert name == "lily_addressee_log"
        return _AddresseeQuery(self)


def _row_telemetry():
    # WS-11's own columns (migration 019) plus FL-1's (migration 018) — the
    # fail-soft strips ALL of them so no corpus row is lost pre-DDL.
    return {
        "session_id": "s", "utterance_ts": "2026-08-05T00:00:00+00:00",
        "transcript": "hello", "is_final": True,
        "agent_classification": "host_directed",
        "timing_source": "stt_stream_reconciled",
        "timing_drift_seconds": 0.12,
    }


def test_log_addressee_writes_telemetry_when_columns_exist():
    db = _AddresseeDB()
    row_id = asyncio.run(lily_persistence.lily_log_addressee(db, _row_telemetry()))
    assert row_id == 1
    assert db.inserted[0]["timing_source"] == "stt_stream_reconciled"
    assert db.inserted[0]["agent_classification"] == "host_directed"


def test_log_addressee_falls_back_stripping_all_telemetry():
    db = _AddresseeDB(reject_telemetry=True)
    row_id = asyncio.run(lily_persistence.lily_log_addressee(db, _row_telemetry()))
    assert row_id == 1
    assert len(db.inserted) == 1
    # Both migrations' telemetry stripped; base row survives.
    assert "timing_source" not in db.inserted[0]
    assert "timing_drift_seconds" not in db.inserted[0]
    assert "agent_classification" not in db.inserted[0]
    assert db.inserted[0]["transcript"] == "hello"
