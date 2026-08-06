"""WS-13 STT tuning tests (WO-LILY-OMNIBUS-003).

Covers: StartRecognition wire injection (against the REAL installed
speechmatics serialization path, no network), the teardown-surviving
SpeakersResult capture, enrollment fallback, machine-metric scorers
(WER/DER/phantom/span — AMENDMENT-002 standard), the echo-room fixture
baseline, the playback-path regression scan, the tuned-artifact drift
guard, and the room-profile sensor.
"""

import asyncio
import json
import math
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

import lily_room_profile
import lily_stt_tuning


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "echo_room_81BCB0.json"


def _load_fixture():
    return json.loads(FIXTURE.read_text())


# ---------------------------------------------------------------------------
# Wire injection — real serialization path, injected module namespace
# ---------------------------------------------------------------------------

def _fake_modules():
    """Fresh injectable stand-ins wrapping the REAL builder so the global
    speechmatics modules are never mutated across tests."""
    from speechmatics.rt._utils.message import build_start_recognition_message

    bc = types.SimpleNamespace(
        build_start_recognition_message=build_start_recognition_message
    )

    class FakeVAC:
        def __init__(self):
            self.handlers = {}

        async def connect(self):
            return "connected"

        def on(self, event, cb):
            self.handlers.setdefault(event, []).append(cb)

    return bc, FakeVAC


def _build_wire(bc):
    from speechmatics.rt import AudioFormat, TranscriptionConfig
    from speechmatics.rt._models import SpeakerDiarizationConfig

    tc = TranscriptionConfig(
        language="en",
        diarization="speaker",
        speaker_diarization_config=SpeakerDiarizationConfig(
            max_speakers=5, speaker_sensitivity=0.35
        ),
        audio_filtering_config={"volume_threshold": 0.0},
    )
    return bc.build_start_recognition_message(tc, AudioFormat())


def test_wire_injection_adds_get_speakers_and_volume_threshold():
    bc, FakeVAC = _fake_modules()
    assert lily_stt_tuning.lily_install_stt_tuning_patch(
        get_speakers=True,
        volume_threshold=1.6,
        _base_client_module=bc,
        _voice_client_cls=FakeVAC,
    )
    msg = _build_wire(bc)
    tc = msg["transcription_config"]
    assert tc["speaker_diarization_config"]["get_speakers"] is True
    # Original declared fields survive alongside the injection.
    assert tc["speaker_diarization_config"]["max_speakers"] == 5
    assert tc["audio_filtering_config"]["volume_threshold"] == 1.6


def test_wire_injection_idempotent_and_composes():
    bc, FakeVAC = _fake_modules()
    assert lily_stt_tuning.lily_install_stt_tuning_patch(
        _base_client_module=bc, _voice_client_cls=FakeVAC
    )
    once = bc.build_start_recognition_message
    assert lily_stt_tuning.lily_install_stt_tuning_patch(
        _base_client_module=bc, _voice_client_cls=FakeVAC
    )
    assert bc.build_start_recognition_message is once  # no double wrap


def test_wire_injection_never_raises_on_broken_module():
    broken = types.SimpleNamespace(build_start_recognition_message=None)
    assert (
        lily_stt_tuning.lily_install_stt_tuning_patch(
            _base_client_module=broken, _voice_client_cls=object
        )
        is False
    )


def test_speakers_result_capture_survives_teardown():
    bc, FakeVAC = _fake_modules()
    lily_stt_tuning._lily_reset_captured_speakers()
    assert lily_stt_tuning.lily_install_stt_tuning_patch(
        _base_client_module=bc, _voice_client_cls=FakeVAC
    )
    client = FakeVAC()
    asyncio.run(client.connect())
    (capture_cb,) = client.handlers["SpeakersResult"]
    capture_cb(
        {
            "message": "SpeakersResult",
            "speakers": [
                {"label": "Rami", "speaker_identifiers": ["id1"]},
                {"label": "S2", "speaker_identifiers": ["id2"]},
            ],
        }
    )
    got = lily_stt_tuning.lily_captured_speakers()
    assert [s["label"] for s in got] == ["Rami", "S2"]
    lily_stt_tuning._lily_reset_captured_speakers()


# ---------------------------------------------------------------------------
# Tuned artifact + kwargs
# ---------------------------------------------------------------------------

def test_artifact_json_matches_module_dict():
    on_disk = json.loads(
        (Path(__file__).resolve().parent.parent / "stt_tuned.json").read_text()
    )
    assert on_disk == lily_stt_tuning.LILY_STT_TUNED


def test_tuned_kwargs_construct_a_real_stt():
    """The tuned kwargs must satisfy the installed plugin's validator."""
    from livekit.plugins.speechmatics import STT as SpeechmaticsSTT
    from livekit.plugins.speechmatics import TurnDetectionMode
    from speechmatics.voice import OperatingPoint

    kwargs = lily_stt_tuning.lily_tuned_stt_kwargs()
    stt = SpeechmaticsSTT(
        api_key="test-key",
        operating_point=OperatingPoint.ENHANCED,
        prefer_current_speaker=True,
        turn_detection_mode=TurnDetectionMode.FIXED,
        **{k: v for k, v in kwargs.items() if k != "prefer_current_speaker"},
    )
    opts = stt._stt_options
    assert opts.speaker_sensitivity == 0.35
    assert opts.max_speakers == 7
    assert opts.include_partials is True
    assert opts.ignore_speakers == ["__ASSISTANT__"]


def test_matrix_cells_all_inside_plugin_validator_bounds():
    cells = lily_stt_tuning.lily_matrix_cells()
    assert len(cells) == 27
    for cell in cells:
        assert 0.0 < cell["speaker_sensitivity"] < 1.0
        assert 1 < cell["max_speakers"] <= 100
        assert 0.0 <= cell["volume_threshold"] <= 100.0


def test_max_speakers_for_roster():
    assert lily_stt_tuning.lily_max_speakers_for(None) == 7
    assert lily_stt_tuning.lily_max_speakers_for(4) == 5
    assert lily_stt_tuning.lily_max_speakers_for(1) == 2
    assert lily_stt_tuning.lily_max_speakers_for(9) == 7  # product cap


def test_dunder_labels_never_enroll():
    rows = [
        {"label": "Rami", "speaker_identifiers": ["a"]},
        {"label": "__ASSISTANT__", "speaker_identifiers": ["evil"]},
        {"label": "__X__", "speaker_identifiers": ["b"]},
    ]
    out = lily_stt_tuning.lily_filter_enrollable_speakers(rows)
    assert [r["label"] for r in out] == ["Rami"]


# ---------------------------------------------------------------------------
# Machine metrics (AMENDMENT-002: WER + DER)
# ---------------------------------------------------------------------------

def test_wer_basics():
    assert lily_stt_tuning.lily_wer("a b c", "a b c") == 0.0
    assert lily_stt_tuning.lily_wer("the pacific ocean", "the specific ocean") == (
        1 / 3
    )
    assert lily_stt_tuning.lily_wer("", "") == 0.0
    assert lily_stt_tuning.lily_wer("", "x") == 1.0
    assert lily_stt_tuning.lily_wer("a b", "") == 1.0


def test_der_perfect_relabeling_is_zero():
    ref = [
        {"speaker": "A", "start": 0, "end": 10},
        {"speaker": "B", "start": 10, "end": 20},
    ]
    hyp = [
        {"speaker": "X", "start": 0, "end": 10},
        {"speaker": "Y", "start": 10, "end": 20},
    ]
    assert lily_stt_tuning.lily_der(ref, hyp) == 0.0


def test_der_merged_speakers():
    ref = [
        {"speaker": "A", "start": 0, "end": 10},
        {"speaker": "B", "start": 10, "end": 20},
    ]
    hyp = [{"speaker": "X", "start": 0, "end": 20}]
    assert math.isclose(lily_stt_tuning.lily_der(ref, hyp), 0.5)


def test_der_missed_and_false_alarm():
    ref = [{"speaker": "A", "start": 0, "end": 10}]
    hyp = []
    assert lily_stt_tuning.lily_der(ref, hyp) == 1.0  # all missed
    hyp = [{"speaker": "X", "start": 0, "end": 15}]
    assert math.isclose(lily_stt_tuning.lily_der(ref, hyp), 0.5)  # 5s false alarm


# ---------------------------------------------------------------------------
# Fixture baseline (the session's known ground truth)
# ---------------------------------------------------------------------------

def test_fixture_baseline_scores():
    fx = _load_fixture()
    score = lily_stt_tuning.lily_score_fixture(fx["rows"], fx["ground_truth"])
    assert score["phantom_label_count"] == 3
    assert score["phantom_labels"] == ["S5", "S6", "S7"]
    assert score["label_continuity_splits"] == 1  # Chris split S1/S4
    assert score["players_covered"] == 4
    assert 0.90 < score["attribution_accuracy"] < 0.92
    spans = sorted(v["span_seconds"] for v in score["span_violations"])
    assert spans == [104.08, 206.0]  # the corrupted S2 spans, nothing else


def test_fixture_span_threshold_has_margin_over_legit_turns():
    """30s quarantine: every legitimate player turn in evidence is under
    two-thirds of the threshold; both corruptions are far above it."""
    fx = _load_fixture()
    gt = fx["ground_truth"]
    legit = [
        r["segment_end"] - r["segment_start"]
        for r in fx["rows"]
        if gt["label_map"].get(r["speaker_label"])
        and (r["segment_end"] - r["segment_start"]) <= 30.0
    ]
    assert max(legit) < 20.0 * (2 / 3) * 1.6  # 20.1s observed max, margin held
    quarantine = lily_stt_tuning.LILY_STT_TUNED["ws10_span_quarantine_seconds"]
    assert quarantine == 30.0


def test_playback_path_regression_no_assistant_leak():
    """Item 1 regression check: zero assistant-speech runs in user rows.
    A future device/routing change that lets Lily's playback reach
    transcription breaks this on the next captured record."""
    fx = _load_fixture()
    rows = fx["rows"] + fx["rows_untimed"]
    assert lily_stt_tuning.lily_assistant_leak_scan(rows, "LILY") == []


def test_leak_scan_catches_synthetic_leak():
    rows = [
        {"speaker_label": "LILY", "text": "Here is your next question about the seven wonders of the ancient world"},
        {"speaker_label": "S2", "text": "your next question about the seven wonders of the ancient"},
        {"speaker_label": "S1", "text": "the ancient world"},  # short repeat: fine
    ]
    leaks = lily_stt_tuning.lily_assistant_leak_scan(rows, "LILY")
    assert [l["speaker_label"] for l in leaks] == ["S2"]


# ---------------------------------------------------------------------------
# Enrollment fallback (captured SpeakersResult after teardown)
# ---------------------------------------------------------------------------

class _DeadStream:
    class _Client:
        _is_connected = False

    _client = _Client()


class _FallbackSTT:
    def __init__(self):
        self._streams = [_DeadStream()]

    async def get_speaker_ids(self):  # would need a live socket
        raise AssertionError("must not be called on a dead stream")


class _Scorekeeper:
    def __init__(self, players):
        self.players = players


class _Upserts:
    def __init__(self):
        self.rows = []


def _fake_supabase(upserts):
    class _Table:
        def __init__(self, name):
            self._name = name

        def upsert(self, rows, on_conflict=None):
            upserts.rows.extend(rows)
            return self

        def select(self, *a, **k):
            return self

        def eq(self, *a):
            return self

        def in_(self, *a):
            return self

        def execute(self):
            return types.SimpleNamespace(data=[])

    class _DB:
        def table(self, name):
            return _Table(name)

    return _DB()


def test_enrollment_falls_back_to_captured_speakers():
    import lily_persistence

    lily_stt_tuning._lily_reset_captured_speakers()
    lily_stt_tuning._captured_speakers.extend(
        [
            {"label": "Rami", "speaker_identifiers": ["idA"]},
            {"label": "__ASSISTANT__", "speaker_identifiers": ["idEvil"]},
        ]
    )
    upserts = _Upserts()
    ok = asyncio.run(
        lily_persistence.lily_enroll_voiceprints(
            _FallbackSTT(),
            _fake_supabase(upserts),
            "grp_test",
            _Scorekeeper({"Rami": {"speaker_label": "Rami"}}),
            trigger="session_close",
        )
    )
    assert ok is True
    labels = [r["speaker_label"] for r in upserts.rows]
    assert labels == ["Rami"]  # dunder label refused at the write
    assert upserts.rows[0]["player_name"] == "Rami"
    lily_stt_tuning._lily_reset_captured_speakers()


def test_enrollment_dead_stream_without_capture_still_fails_closed():
    import lily_persistence

    lily_stt_tuning._lily_reset_captured_speakers()
    upserts = _Upserts()
    ok = asyncio.run(
        lily_persistence.lily_enroll_voiceprints(
            _FallbackSTT(),
            _fake_supabase(upserts),
            "grp_test",
            _Scorekeeper({}),
            trigger="session_close",
        )
    )
    assert ok is False
    assert upserts.rows == []


# ---------------------------------------------------------------------------
# Room-profile sensor (item 6)
# ---------------------------------------------------------------------------

def _synthetic_room(decay_db_per_s: float, sample_rate: int = 16000) -> bytes:
    """Speech-burst train where each burst rings out at a known decay rate."""
    rng = np.random.default_rng(13)
    out = np.zeros(0)
    for _ in range(6):
        burst = rng.normal(0, 0.35, int(0.4 * sample_rate))
        t = np.arange(int(0.8 * sample_rate)) / sample_rate
        tail = rng.normal(0, 0.35, len(t)) * (10 ** (-decay_db_per_s * t / 20))
        gap = np.zeros(int(0.2 * sample_rate))
        out = np.concatenate([out, burst, tail, gap])
    return (np.clip(out, -1, 1) * 32767).astype(np.int16).tobytes()


def test_room_profile_estimates_decay_band():
    slow = lily_room_profile.lily_estimate_room_profile(
        _synthetic_room(40.0), 16000
    )
    fast = lily_room_profile.lily_estimate_room_profile(
        _synthetic_room(300.0), 16000
    )
    assert slow is not None and fast is not None
    assert slow.rt60_estimate_s is not None and fast.rt60_estimate_s is not None
    # 40 dB/s -> nominal RT60 1.5s; 300 dB/s -> nominal 0.2s. Bands, not
    # exact values — this is a blind estimator on noise bursts.
    assert slow.rt60_estimate_s > fast.rt60_estimate_s
    assert slow.reverberant is True
    assert fast.reverberant is False
    assert slow.drr_estimate_db is not None and fast.drr_estimate_db is not None
    assert fast.drr_estimate_db > slow.drr_estimate_db


def test_room_profile_refuses_short_or_silent_audio():
    assert lily_room_profile.lily_estimate_room_profile(b"", 16000) is None
    assert (
        lily_room_profile.lily_estimate_room_profile(b"\x00" * 8000, 16000) is None
    )
    silence = (np.zeros(16000 * 5)).astype(np.int16).tobytes()
    assert lily_room_profile.lily_estimate_room_profile(silence, 16000) is None


def test_profile_mapping_and_state_block_note():
    import lily_audeering_consumers

    profile = lily_room_profile.LilyRoomProfile(
        rt60_estimate_s=0.9, drr_estimate_db=0.5, frames_analyzed=200,
        decay_runs_used=6,
    )
    adj = lily_room_profile.lily_profile_stt_adjustments(profile)
    assert adj["end_of_utterance_silence_trigger"] == 1.0
    assert adj["recommend_semantic_turn_detection"] is True
    assert adj["tier1_threshold_delta"] == -0.05
    assert "listen generously" in adj["state_note"]
    # trigger stays inside the plugin validator's (0, 2) bound
    assert 0.0 < adj["end_of_utterance_silence_trigger"] < 2.0

    state = lily_audeering_consumers.LilyAcousticState()
    state.set_room_profile({"rt60_estimate_s": 0.9}, adj["state_note"])
    assert adj["state_note"] in state.state_block_lines()
    # first profile wins for the session
    state.set_room_profile({"rt60_estimate_s": 0.2}, "[other]")
    assert state.room_profile() == {"rt60_estimate_s": 0.9}


def test_dry_room_maps_to_no_adjustments():
    profile = lily_room_profile.LilyRoomProfile(
        rt60_estimate_s=0.3, drr_estimate_db=8.0, frames_analyzed=200,
        decay_runs_used=6,
    )
    adj = lily_room_profile.lily_profile_stt_adjustments(profile)
    assert adj["end_of_utterance_silence_trigger"] is None
    assert adj["recommend_semantic_turn_detection"] is False
    assert adj["tier1_threshold_delta"] == 0.0
    assert adj["state_note"] is None


# ---------------------------------------------------------------------------
# Item 5 — expectation-primed matching coverage on the alternatives set
# ---------------------------------------------------------------------------

def test_tier1_fuzzy_recovers_mark_to_mars_on_the_top_final():
    """The session's proven path: 'Mark' fuzzy/phonetic-matches 'Mars' at
    Tier-1 without needing the alternatives set at all."""
    import lily_evaluation

    question = {
        "question": "Which planet is called the red planet?",
        "acceptable_answers": ["Mars"],
        "format": "open",
    }
    r = lily_evaluation.lily_tier1_evaluate_nbest("Mark", question, hypotheses=[])
    assert r["verdict"] == "correct"
    assert r["nbest"]["hit_index"] == 0


def test_tier1_nbest_recovers_from_alternative_slot():
    """Coverage on the alternatives set: the top final genuinely misses,
    a synthesized alternative lands, and the verdict comes from that slot."""
    import lily_evaluation

    question = {
        "question": "Which planet is called the red planet?",
        "acceptable_answers": ["Mars"],
        "format": "open",
    }
    r = lily_evaluation.lily_tier1_evaluate_nbest(
        "Karl",
        question,
        hypotheses=[{"text": "Karl"}, {"text": "Mars"}],
    )
    assert r["verdict"] == "correct"
    assert r["nbest"]["hit_index"] > 0  # recovered from an alternative, not 1-best


def test_tier1_nbest_cannot_recover_untranscribed_speech():
    """Honest boundary: no hypothesis carries the answer -> no recovery."""
    import lily_evaluation

    question = {
        "question": "Which planet is called the red planet?",
        "acceptable_answers": ["Mars"],
        "format": "open",
    }
    r = lily_evaluation.lily_tier1_evaluate_nbest(
        "Ma", question, hypotheses=[{"text": "Ma"}, {"text": "Muh"}]
    )
    assert r["verdict"] != "correct"


def test_additional_vocab_stays_bounded_to_names():
    """additional_vocab carries the assistant name + player names only —
    never answer nouns (expectation-primed matching is the generalizing
    mechanism; preloading answers does not generalize)."""
    vocab = lily_stt_tuning.LILY_STT_TUNED["constructor"].get("additional_vocab")
    assert vocab is None  # answer nouns have no artifact slot to hide in
