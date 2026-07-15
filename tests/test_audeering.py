"""Tests for the Audeering devAIce acoustic pipeline (WO-LILY-AUDEERING-001):
gates, room-read banding, the child-signal ladder + adult-mode veto logic,
the rubric zero-scalar lint, null-safety, snapshot semantics, and the
upload-config contract. Pure logic — no livekit / aiohttp / supabase
required. Ports the relevant JRVS test_audeering_consumers_d.py regressions
(safety-outside-the-smoother, neutral suppression, no-raw-scalars) and adds
the Lily-specific ladder/veto and snapshot coverage."""

import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_audeering_client as client
import lily_audeering_consumers as consumers
from lily_audeering_consumers import (
    LilyAcousticState,
    LilyRoomBaseline,
    advance_child_ladder,
    child_veto_active,
    derive_room_read,
    derive_scene_descriptor,
    derive_state_lines,
    is_music_with_speech,
    lily_audeering_rubric_block,
    quality_gate,
    scene_top_label,
)

_ENV_VARS = (
    "AUDEERING_API_KEY",
    "AUDEERING_MAX_UPLOADS_PER_SESSION",
    "AUDEERING_WINDOW_SECONDS_F",
    "AUDEERING_CAPTURE_INTERVAL_SECONDS",
    "AUDEERING_MIN_SNR_DB",
    "AUDEERING_SNR_TRANSIT_ADJUST",
    "AUDEERING_AVD_SMOOTH_WINDOW",
    "AUDEERING_AVD_NEUTRAL_BAND",
    "AUDEERING_CHILD_HALT_THRESHOLD_HIGH",
    "AUDEERING_CHILD_HALT_THRESHOLD_BORDERLINE",
    "AUDEERING_CHILD_HALT_SUSTAINED_N",
    "AUDEERING_CHILD_HALT_ENABLED",
    "AUDEERING_CHILD_STEP_UP_ENABLED",
)


def _reset_env() -> None:
    for var in _ENV_VARS:
        os.environ.pop(var, None)


def _parsed(**overrides) -> dict:
    base = {
        "dimension": {"arousal": 0.0, "valence": 0.0, "dominance": 0.0},
        "audioQuality": {"snr": 20.0},
        "aed": ["speech"],
        "scene": {"label": "indoor"},
        "speakerSegments": [],
    }
    base.update(overrides)
    return base


class _EnvCase(unittest.TestCase):
    def setUp(self) -> None:
        _reset_env()

    def tearDown(self) -> None:
        _reset_env()


# ---------------------------------------------------------------------------
# Upload config contract (Task 1)
# ---------------------------------------------------------------------------

class TestUploadConfig(_EnvCase):
    def test_exact_module_set(self):
        self.assertEqual(
            client._CONFIG["modules"],
            {
                "expression": {"expressionModel": "large"},
                "prosody": {},
                "audioQuality": {},
                "aed": {},
                "scene": {"outputSubScene": True},
                "speakerAttributes": {"speakerAttributesModel": "large"},
            },
        )

    def test_asr_and_speaker_verification_excluded(self):
        self.assertNotIn("asr", client._CONFIG["modules"])
        self.assertNotIn("speakerVerification", client._CONFIG["modules"])

    def test_speaker_attributes_model_large_is_mandatory(self):
        self.assertEqual(
            client._CONFIG["modules"]["speakerAttributes"],
            {"speakerAttributesModel": "large"},
        )

    def test_capture_window_at_least_five_seconds(self):
        import lily_config
        # Scene model is optimized for >5s windows; the floor is enforced
        # even if the env tries to shrink it.
        os.environ["AUDEERING_WINDOW_SECONDS_F"] = "2.0"
        self.assertGreaterEqual(lily_config.audeering_window_seconds(), 5.0)
        os.environ.pop("AUDEERING_WINDOW_SECONDS_F")
        self.assertGreaterEqual(lily_config.audeering_window_seconds(), 5.0)


# ---------------------------------------------------------------------------
# Parser (client) — gender-nested child schema, scene sub-scene
# ---------------------------------------------------------------------------

class TestParser(_EnvCase):
    def test_gender_nested_child_schema(self):
        parsed = client.parse_devaice_response({
            "speakerAttributes": [
                {"gender": {"female": 0.05, "male": 0.05, "child": 0.9}, "age": 9.0},
                {"gender": {"female": 0.7, "male": 0.25, "child": 0.05}, "age": 34.0},
            ],
        })
        segs = parsed["speakerSegments"]
        self.assertEqual(len(segs), 2)
        self.assertAlmostEqual(segs[0]["child"], 0.9)
        self.assertAlmostEqual(segs[0]["age"], 9.0)
        self.assertAlmostEqual(segs[1]["female"], 0.7)

    def test_null_age_and_null_child_preserved_as_none(self):
        parsed = client.parse_devaice_response({
            "speakerAttributes": [{"gender": {}, "age": None}],
        })
        seg = parsed["speakerSegments"][0]
        self.assertIsNone(seg["child"])
        self.assertIsNone(seg["age"])

    def test_scene_sub_scene_parsed(self):
        parsed = client.parse_devaice_response({
            "scene": {"label": "indoor", "subScene": "indoor_small"},
            "dimension": {"arousal": 0.5},
        })
        self.assertEqual(
            parsed["scene"], {"label": "indoor", "subScene": "indoor_small"}
        )

    def test_result_wrapper_and_dimension(self):
        parsed = client.parse_devaice_response({
            "result": {
                "expression": {
                    "dimension": {"arousal": 0.6, "valence": -0.4, "dominance": 0.1},
                    "category": {"happy": 0.1, "angry": 0.6},
                },
                "audioQuality": {"snr": 18.0},
                "aed": [{"label": "Speech"}, {"label": "Music"}],
            }
        })
        self.assertAlmostEqual(parsed["dimension"]["arousal"], 0.6)
        self.assertAlmostEqual(parsed["category"]["angry"], 0.6)
        self.assertEqual(parsed["aed"], ["speech", "music"])
        self.assertAlmostEqual(parsed["audioQuality"]["snr"], 18.0)

    def test_empty_payload_returns_none(self):
        self.assertIsNone(client.parse_devaice_response({}))
        self.assertIsNone(client.parse_devaice_response(None))


# ---------------------------------------------------------------------------
# Quality gate (Task 2) — SNR first, transit adjustment
# ---------------------------------------------------------------------------

class TestQualityGate(_EnvCase):
    def test_low_snr_suppresses_affect(self):
        b = LilyRoomBaseline()
        lines, _ = derive_state_lines(
            _parsed(
                dimension={"arousal": 0.8, "valence": -0.8, "dominance": -0.5},
                audioQuality={"snr": 5.0},
            ),
            b,
        )
        self.assertEqual(lines, ())

    def test_missing_audio_quality_allows(self):
        keep, reason = quality_gate({"dimension": {}})
        self.assertTrue(keep)
        self.assertEqual(reason, "no_audio_quality")

    def test_missing_snr_allows(self):
        keep, reason = quality_gate({"audioQuality": {"rt60": 0.4}})
        self.assertTrue(keep)
        self.assertEqual(reason, "no_snr")

    def test_transit_scene_loosens_snr_bar(self):
        # Default MIN_SNR=12, transit adjust -2 -> threshold 10 in transit.
        keep_transit, _ = quality_gate(
            {"audioQuality": {"snr": 11.0}}, scene_label="transport"
        )
        keep_indoor, _ = quality_gate(
            {"audioQuality": {"snr": 11.0}}, scene_label="indoor"
        )
        self.assertTrue(keep_transit)
        self.assertFalse(keep_indoor)

    def test_gate_runs_before_avd_smoother(self):
        # A suppressed window must NOT feed the smoother.
        b = LilyRoomBaseline()
        derive_state_lines(
            _parsed(
                dimension={"arousal": 0.9, "valence": -0.9, "dominance": 0.0},
                audioQuality={"snr": 1.0},
            ),
            b,
        )
        self.assertEqual(len(b.avd_arousal), 0)


# ---------------------------------------------------------------------------
# AED music gate (Task 2)
# ---------------------------------------------------------------------------

class TestMusicGate(_EnvCase):
    def test_music_with_speech_suppresses_affect(self):
        b = LilyRoomBaseline()
        lines, _ = derive_state_lines(
            _parsed(
                dimension={"arousal": 0.8, "valence": -0.8, "dominance": 0.0},
                aed=["music", "speech"],
            ),
            b,
        )
        self.assertEqual(lines, ())

    def test_singing_suppresses_affect(self):
        self.assertTrue(is_music_with_speech({"aed": ["singing", "speech"]}))

    def test_speech_only_passes(self):
        self.assertFalse(is_music_with_speech({"aed": ["speech"]}))


# ---------------------------------------------------------------------------
# Room-temperature AVD banding (Task 3)
# ---------------------------------------------------------------------------

class TestRoomRead(_EnvCase):
    def test_neutral_emits_nothing(self):
        b = LilyRoomBaseline()
        lines, _ = derive_state_lines(
            _parsed(dimension={"arousal": 0.05, "valence": -0.05, "dominance": 0.03}),
            b,
        )
        self.assertEqual(lines, ())

    def test_flat_room_descriptor(self):
        b = LilyRoomBaseline()
        lines, _ = derive_state_lines(
            _parsed(dimension={"arousal": -0.6, "valence": 0.0, "dominance": 0.0}),
            b,
        )
        self.assertIn("[room read: flat / low energy]", lines)

    def test_hot_room_descriptor(self):
        b = LilyRoomBaseline()
        lines, _ = derive_state_lines(
            _parsed(dimension={"arousal": 0.6, "valence": 0.5, "dominance": 0.1}),
            b,
        )
        self.assertIn("[room read: hot / riding high]", lines)

    def test_sagging_valence_descriptor(self):
        b = LilyRoomBaseline()
        lines, _ = derive_state_lines(
            _parsed(dimension={"arousal": 0.0, "valence": -0.5, "dominance": 0.0}),
            b,
        )
        self.assertIn("[room read: valence sagging]", lines)

    def test_agitated_descriptor(self):
        b = LilyRoomBaseline()
        lines, _ = derive_state_lines(
            _parsed(dimension={"arousal": 0.7, "valence": -0.5, "dominance": 0.0}),
            b,
        )
        self.assertIn("[room read: agitated / on edge]", lines)

    def test_smoothing_averages_across_windows(self):
        os.environ["AUDEERING_AVD_SMOOTH_WINDOW"] = "4"
        b = LilyRoomBaseline()
        # Three hot windows then one flat spike: smoothed mean stays hot.
        for _ in range(3):
            derive_state_lines(
                _parsed(dimension={"arousal": 0.8, "valence": 0.6, "dominance": 0.0}),
                b,
            )
        lines, _ = derive_state_lines(
            _parsed(dimension={"arousal": -0.2, "valence": 0.1, "dominance": 0.0}),
            b,
        )
        self.assertIn("[room read: hot / riding high]", lines)

    def test_category_scores_not_consumed(self):
        # A wildly angry category with a neutral dimension must not band.
        b = LilyRoomBaseline()
        lines, _ = derive_state_lines(
            _parsed(
                dimension={"arousal": 0.0, "valence": 0.0, "dominance": 0.0},
                category={"angry": 0.99},
            ),
            b,
        )
        self.assertEqual(lines, ())

    def test_no_raw_scalars_in_lines(self):
        # D0 cardinal rule ported from JRVS: unique decimals in must never
        # appear in prompt-visible output.
        b = LilyRoomBaseline()
        lines, _ = derive_state_lines(
            _parsed(
                dimension={"arousal": 0.71234, "valence": -0.5678, "dominance": 0.1234},
                audioQuality={"snr": 22.789, "rt60": 0.412},
                prosody={"f0": {"avg": 250.567}, "loudness": {"avg": 8.234}},
                scene={"label": "outdoor"},
            ),
            b,
        )
        joined = " ".join(lines)
        self.assertTrue(lines)  # something was derived
        self.assertIsNone(re.search(r"\d", joined), f"scalar leak: {joined!r}")
        for token in ("dB", "Hz", "arousal=", "valence=", "snr="):
            self.assertNotIn(token, joined)


# ---------------------------------------------------------------------------
# Scene consumer (Task 5)
# ---------------------------------------------------------------------------

class TestScene(_EnvCase):
    def test_sub_scene_small_indoor(self):
        self.assertEqual(
            derive_scene_descriptor(
                {"scene": {"label": "indoor", "subScene": "indoor_small"}}
            ),
            "small indoor room",
        )

    def test_sub_scene_large_indoor(self):
        self.assertEqual(
            derive_scene_descriptor(
                {"scene": {"label": "indoor", "subScene": "indoor_large"}}
            ),
            "large indoor venue",
        )

    def test_plain_indoor_is_silent(self):
        self.assertIsNone(derive_scene_descriptor({"scene": {"label": "indoor"}}))

    def test_transport_descriptor_and_env_line(self):
        b = LilyRoomBaseline()
        lines, _ = derive_state_lines(
            _parsed(
                dimension={"arousal": 0.6, "valence": 0.5, "dominance": 0.0},
                scene={"label": "transport"},
            ),
            b,
        )
        self.assertIn("[env: in transit]", lines)

    def test_scene_top_label_feeds_transit_gate(self):
        self.assertEqual(scene_top_label({"scene": {"label": "transport"}}), "transport")
        self.assertEqual(scene_top_label({"scene": "vehicle"}), "transport")
        self.assertIsNone(scene_top_label({"scene": None}))

    def test_env_line_at_most_once_per_refresh(self):
        state = LilyAcousticState()
        state.record_response(
            _parsed(
                dimension={"arousal": 0.6, "valence": 0.5, "dominance": 0.0},
                scene={"label": "outdoor"},
            )
        )
        state.record_response(
            _parsed(
                dimension={"arousal": 0.6, "valence": 0.5, "dominance": 0.0},
                scene={"label": "outdoor"},
            )
        )
        lines = state.state_block_lines()
        self.assertEqual(sum(1 for l in lines if l.startswith("[env:")), 1)


# ---------------------------------------------------------------------------
# Child-signal ladder (Task 4) — thresholds, sustain, null-safety
# ---------------------------------------------------------------------------

def _child_seg(conf) -> dict:
    return {"child": conf, "female": None, "male": None, "age": None}


class TestChildLadder(_EnvCase):
    def test_high_sustained_two_segments_trips_halt(self):
        b = LilyRoomBaseline()
        event = advance_child_ladder([_child_seg(0.9), _child_seg(0.9)], b)
        self.assertIsNotNone(event)
        self.assertEqual(event["tier"], "high_halt")
        self.assertGreaterEqual(b.child_high_streak, 2)

    def test_single_high_segment_does_not_trip(self):
        b = LilyRoomBaseline()
        event = advance_child_ladder([_child_seg(0.9)], b)
        self.assertIsNone(event)
        self.assertFalse(child_veto_active(b))

    def test_borderline_sustained_trips_step_up(self):
        b = LilyRoomBaseline()
        event = advance_child_ladder([_child_seg(0.6), _child_seg(0.55)], b)
        self.assertIsNotNone(event)
        self.assertEqual(event["tier"], "borderline_step_up")

    def test_oscillation_around_high_cannot_evade_borderline(self):
        # JRVS ladder fix, lifted intact: HIGH segments also advance the
        # child-present streak, so 0.9 / 0.6 alternation still trips.
        b = LilyRoomBaseline()
        event = advance_child_ladder([_child_seg(0.9), _child_seg(0.6)], b)
        self.assertIsNotNone(event)
        self.assertEqual(event["tier"], "borderline_step_up")

    def test_adult_voice_resets_both_streaks(self):
        b = LilyRoomBaseline()
        advance_child_ladder([_child_seg(0.9)], b)
        advance_child_ladder([_child_seg(0.1)], b)
        self.assertEqual(b.child_high_streak, 0)
        self.assertEqual(b.child_borderline_streak, 0)

    def test_null_score_neither_advances_nor_resets(self):
        # Null-safety: too-short segments / small-model nulls are skipped.
        b = LilyRoomBaseline()
        advance_child_ladder([_child_seg(0.9)], b)
        self.assertEqual(b.child_high_streak, 1)
        advance_child_ladder([_child_seg(None)], b)
        self.assertEqual(b.child_high_streak, 1)  # not reset, not advanced
        event = advance_child_ladder([_child_seg(0.9)], b)
        self.assertIsNotNone(event)
        self.assertEqual(event["tier"], "high_halt")

    def test_streak_counts_segments_within_one_window(self):
        # Per-VAD-segment results: two child segments in ONE upload sustain.
        b = LilyRoomBaseline()
        _, event = derive_state_lines(
            _parsed(speakerSegments=[_child_seg(0.95), _child_seg(0.92)]),
            b,
        )
        self.assertIsNotNone(event)
        self.assertEqual(event["tier"], "high_halt")

    def test_wide_smoother_window_does_not_delay_child_streak(self):
        # JRVS D-cross rule-5 regression, ported: safety triggers run
        # OUTSIDE the smoother — widening AUDEERING_AVD_SMOOTH_WINDOW must
        # not slow the ladder.
        os.environ["AUDEERING_AVD_SMOOTH_WINDOW"] = "10"
        b = LilyRoomBaseline()
        derive_state_lines(_parsed(speakerSegments=[_child_seg(0.95)]), b)
        derive_state_lines(_parsed(speakerSegments=[_child_seg(0.95)]), b)
        self.assertGreaterEqual(b.child_high_streak, 2)
        self.assertTrue(child_veto_active(b))

    def test_ladder_runs_even_when_quality_gate_suppresses(self):
        # Safety is NOT subordinate to audio quality: a child voice on
        # noisy audio still advances the ladder.
        b = LilyRoomBaseline()
        for _ in range(2):
            lines, event = derive_state_lines(
                _parsed(
                    speakerSegments=[_child_seg(0.95)],
                    audioQuality={"snr": 1.0},
                ),
                b,
            )
            self.assertEqual(lines, ())
        self.assertGreaterEqual(b.child_high_streak, 2)

    def test_ladder_runs_even_on_music_segment(self):
        # A child singing (AED tags music) must still be evaluated.
        b = LilyRoomBaseline()
        for _ in range(2):
            derive_state_lines(
                _parsed(
                    speakerSegments=[_child_seg(0.95)],
                    aed=["music", "speech"],
                ),
                b,
            )
        self.assertGreaterEqual(b.child_high_streak, 2)

    def test_thresholds_env_tunable(self):
        os.environ["AUDEERING_CHILD_HALT_THRESHOLD_HIGH"] = "0.95"
        b = LilyRoomBaseline()
        event = advance_child_ladder([_child_seg(0.9), _child_seg(0.9)], b)
        # 0.9 < 0.95 -> only the borderline tier trips.
        self.assertEqual(event["tier"], "borderline_step_up")


# ---------------------------------------------------------------------------
# Adult-mode veto (Task 4) — enable flags ON for Lily, both tiers veto
# ---------------------------------------------------------------------------

class TestAdultModeVeto(_EnvCase):
    def test_enable_flags_default_on_for_lily(self):
        import lily_config
        self.assertTrue(lily_config.audeering_child_halt_enabled())
        self.assertTrue(lily_config.audeering_child_step_up_enabled())

    def test_high_tier_activates_veto(self):
        b = LilyRoomBaseline()
        advance_child_ladder([_child_seg(0.9), _child_seg(0.9)], b)
        self.assertTrue(child_veto_active(b))

    def test_borderline_tier_also_vetoes(self):
        # Veto-only, BOTH tiers.
        b = LilyRoomBaseline()
        advance_child_ladder([_child_seg(0.6), _child_seg(0.6)], b)
        self.assertTrue(child_veto_active(b))

    def test_veto_clears_when_streak_resets(self):
        b = LilyRoomBaseline()
        advance_child_ladder([_child_seg(0.9), _child_seg(0.9)], b)
        self.assertTrue(child_veto_active(b))
        advance_child_ladder([_child_seg(0.05)], b)
        self.assertFalse(child_veto_active(b))

    def test_state_fires_child_callback(self):
        state = LilyAcousticState()
        events = []
        state.on_child_signal = events.append
        for _ in range(2):
            state.record_response(_parsed(speakerSegments=[_child_seg(0.95)]))
        self.assertTrue(events)
        self.assertEqual(events[-1]["tier"], "high_halt")
        self.assertTrue(state.child_veto_active())

    def test_callback_exception_never_escapes(self):
        state = LilyAcousticState()

        def _boom(event):
            raise RuntimeError("veto handler crashed")

        state.on_child_signal = _boom
        for _ in range(2):
            state.record_response(_parsed(speakerSegments=[_child_seg(0.95)]))
        # Raw signal still recorded despite the callback failure.
        self.assertIsNotNone(state.latest_snapshot())

    def test_framing_stamp_is_doc_verbatim(self):
        self.assertIn(
            "how the speaker sounds, not necessarily the actual attributes "
            "of the speaker",
            consumers.PERCEIVED_FRAMING,
        )
        self.assertIn("±8.46yr", consumers.PERCEIVED_FRAMING)
        self.assertIn("NEVER authorize", consumers.PERCEIVED_FRAMING)
        self.assertIn(
            "whole-room verbal consensus remains necessary and is no longer "
            "sufficient",
            consumers.PERCEIVED_FRAMING,
        )


# ---------------------------------------------------------------------------
# Rubric lint (Task 3)
# ---------------------------------------------------------------------------

class TestRubricLint(_EnvCase):
    def test_rubric_has_zero_digits(self):
        rubric = lily_audeering_rubric_block()
        match = re.search(r"\d", rubric)
        self.assertIsNone(match, f"digit in rubric: {match}")

    def test_rubric_has_no_units(self):
        rubric = lily_audeering_rubric_block()
        self.assertIsNone(re.search(r"\bdB\b", rubric, re.IGNORECASE))
        self.assertIsNone(re.search(r"\bHz\b", rubric, re.IGNORECASE))

    def test_import_time_lint_is_wired_and_idempotent(self):
        self.assertTrue(callable(consumers._lint_rubric_free_of_scalars))
        consumers._lint_rubric_free_of_scalars()  # no-raise on current rubric

    def test_rubric_lists_every_room_read_phrase(self):
        rubric = lily_audeering_rubric_block()
        for phrase in (
            "flat / low energy",
            "hot / riding high",
            "valence sagging",
            "agitated / on edge",
        ):
            self.assertIn(phrase, rubric)

    def test_rubric_lists_every_env_phrase(self):
        rubric = lily_audeering_rubric_block()
        for phrase in (
            "small indoor room",
            "medium indoor space",
            "large indoor venue",
            "outdoors",
            "in transit",
        ):
            self.assertIn(phrase, rubric)

    def test_rubric_maps_descriptors_to_host_moves(self):
        # Normalize line breaks so phrases wrapped across lines still match.
        rubric = re.sub(r"\s+", " ", lily_audeering_rubric_block())
        self.assertIn("easier question", rubric)   # flat -> easier + spotlight
        self.assertIn("spotlight", rubric)
        self.assertIn("ride it", rubric)           # hot -> tighten and ride
        self.assertIn("gimme", rubric)             # sagging -> drop a gimme


# ---------------------------------------------------------------------------
# Snapshot semantics (Task 6)
# ---------------------------------------------------------------------------

class TestSnapshots(_EnvCase):
    def test_addressee_snapshot_null_when_breaker_open(self):
        state = LilyAcousticState()
        state.record_response(_parsed(dimension={"arousal": 0.5}))
        self.assertIsNotNone(state.addressee_snapshot())
        state.set_breaker_open(True)
        self.assertIsNone(state.addressee_snapshot())

    def test_addressee_snapshot_null_before_first_capture(self):
        state = LilyAcousticState()
        self.assertIsNone(state.addressee_snapshot())

    def test_snapshot_carries_trajectory_shape(self):
        state = LilyAcousticState()
        state.record_response(
            _parsed(
                dimension={"arousal": 0.5, "valence": 0.2},
                category={"happy": 0.7},
                prosody={"speakingRate": 4.2},
            )
        )
        snap = state.latest_snapshot()
        for key in ("category", "dimension", "prosody", "features",
                    "audio_quality", "aed", "scene", "captured_at"):
            self.assertIn(key, snap)
        self.assertAlmostEqual(snap["dimension"]["arousal"], 0.5)

    def test_speaker_attributes_stamped_perceived_not_verified(self):
        state = LilyAcousticState()
        state.record_response(_parsed(speakerSegments=[_child_seg(0.2)]))
        snap = state.latest_snapshot()
        self.assertEqual(
            snap["speaker_attributes"]["framing"], "PERCEIVED_NOT_VERIFIED"
        )

    def test_raw_signal_recorded_even_when_consumers_fail(self):
        # D-cross rule 6: consumer exceptions never stop raw-signal
        # recording. A poisoned baseline makes derive blow up.
        state = LilyAcousticState()
        state.baseline = None  # type: ignore[assignment]
        state.record_response(_parsed(dimension={"arousal": 0.9}))
        self.assertIsNotNone(state.latest_snapshot())

    def test_state_block_lines_clear_on_neutral(self):
        state = LilyAcousticState()
        state.record_response(
            _parsed(dimension={"arousal": 0.8, "valence": 0.6, "dominance": 0.0})
        )
        self.assertTrue(state.state_block_lines())
        # Enough neutral windows to pull the smoothed mean inside the band.
        for _ in range(8):
            state.record_response(
                _parsed(dimension={"arousal": 0.0, "valence": 0.0, "dominance": 0.0})
            )
        self.assertEqual(
            tuple(l for l in state.state_block_lines() if l.startswith("[room read:")),
            (),
        )


# ---------------------------------------------------------------------------
# Circuit breaker (client)
# ---------------------------------------------------------------------------

class TestCircuitBreaker(_EnvCase):
    def test_missing_key_opens_breaker_without_raising(self):
        state = LilyAcousticState()
        pipeline = client.LilyAudeeringPipeline(state)
        self.assertTrue(pipeline.breaker_open)
        self.assertFalse(pipeline.started)
        # Breaker state mirrors into the acoustic state -> explicit-null
        # addressee snapshots.
        self.assertTrue(state.breaker_open)
        self.assertIsNone(state.addressee_snapshot())

    def test_masked_key_never_leaks(self):
        self.assertEqual(client._mask_key("supersecretkey123"), "****y123")
        self.assertEqual(client._mask_key("short"), "****")

    def test_wav_header_shape(self):
        pcm = b"\x00\x01" * 100
        wav = client._build_wav(pcm)
        self.assertTrue(wav.startswith(b"RIFF"))
        self.assertIn(b"WAVE", wav[:16])
        self.assertEqual(len(wav), 44 + len(pcm))

    def test_retry_after_parser(self):
        self.assertEqual(client._parse_retry_after("2"), 2.0)
        self.assertEqual(client._parse_retry_after("30"), client._RETRY_CAP_S)
        self.assertIsNone(client._parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT"))
        self.assertIsNone(client._parse_retry_after(None))


class _PollResponse:
    def __init__(self, status, payload=None, headers=None):
        self.status = status
        self.payload = payload or {}
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def json(self, content_type=None):
        return self.payload


class _PollClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def get(self, *_args, **_kwargs):
        response = self.responses[self.calls]
        self.calls += 1
        return response


class TestAsyncResultPolling(unittest.IsolatedAsyncioTestCase):
    async def test_pending_results_are_polled_until_ready(self):
        fake = _PollClient([
            _PollResponse(202),
            _PollResponse(202),
            _PollResponse(200, {"ready": True}),
        ])
        sleeps = []
        original_sleep = client._sleep_for_retry
        original_parse = client.parse_devaice_response

        async def _no_sleep(attempt, retry_after):
            sleeps.append((attempt, retry_after))

        client._sleep_for_retry = _no_sleep
        client.parse_devaice_response = lambda payload: payload
        try:
            result = await client._poll_once(fake, "upload-1", {})
        finally:
            client._sleep_for_retry = original_sleep
            client.parse_devaice_response = original_parse

        self.assertEqual(result, {"ready": True})
        self.assertEqual(fake.calls, 3)
        self.assertEqual(len(sleeps), 2)

    async def test_repeated_pending_results_exhaust_bound(self):
        fake = _PollClient([
            _PollResponse(202)
            for _ in range(client._RETRY_MAX_ATTEMPTS)
        ])
        original_sleep = client._sleep_for_retry

        async def _no_sleep(*_args):
            return None

        client._sleep_for_retry = _no_sleep
        try:
            result = await client._poll_once(fake, "upload-2", {})
        finally:
            client._sleep_for_retry = original_sleep

        self.assertIsNone(result)
        self.assertEqual(fake.calls, client._RETRY_MAX_ATTEMPTS)


if __name__ == "__main__":
    unittest.main()
