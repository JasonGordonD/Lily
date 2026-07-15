"""Tests for lily_nbest + the n-best-aware evaluation layer
(WO-LILY-ADDRESSEE-H1-001 Task 1). Pure — no network, no livekit, no
speechmatics import (the patch installer is exercised through its test
injection points)."""

import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_config
from lily_evaluation import (
    lily_build_judge_prompt,
    lily_tier1_evaluate_nbest,
    lily_tier1_evaluate_question,
)
from lily_nbest import (
    LilyNBestCollector,
    lily_install_nbest_stt_patch,
    lily_nbest_dispersion,
    lily_synthesize_hypotheses,
)


# ---------------------------------------------------------------------------
# Dispersion — edge contract
# ---------------------------------------------------------------------------

def test_dispersion_empty_is_none():
    assert lily_nbest_dispersion([]) is None
    assert lily_nbest_dispersion(None) is None


def test_dispersion_single_hypothesis_is_zero():
    assert lily_nbest_dispersion([0.73]) == 0.0
    assert lily_nbest_dispersion([{"text": "x", "confidence": 0.5}]) == 0.0


def test_dispersion_variance_value():
    # Population variance of [0.9, 0.5]: mean 0.7 -> (0.04 + 0.04)/2 = 0.04
    assert lily_nbest_dispersion([0.9, 0.5]) == 0.04


def test_dispersion_tight_set_is_small_fractured_is_large():
    tight = lily_nbest_dispersion([0.9, 0.88, 0.86])
    fractured = lily_nbest_dispersion([0.95, 0.3, 0.35])
    assert tight < lily_config.nbest_dispersion_threshold() < fractured


def test_dispersion_ignores_garbage_entries():
    assert lily_nbest_dispersion(["nope", None, 0.5]) == 0.0
    assert lily_nbest_dispersion(["nope", None]) is None


# ---------------------------------------------------------------------------
# Utterance synthesis from per-word alternatives
# ---------------------------------------------------------------------------

def _word(*alts):
    return {"alternatives": [
        {"content": c, "confidence": conf} for c, conf in alts
    ]}


def test_synthesis_backbone_first_then_substitutions():
    words = [
        _word(("mad", 0.9), ("madagascar", 0.5)),
        _word(("at", 0.8)),
    ]
    hyps = lily_synthesize_hypotheses(words, max_hypotheses=3)
    assert hyps[0]["text"] == "mad at"          # slot 0 is always 1-best
    assert hyps[0]["confidence"] == 0.85
    assert hyps[1]["text"] == "madagascar at"
    assert hyps[1]["confidence"] == 0.65


def test_synthesis_is_bounded():
    words = [_word(*((f"alt{i}", 0.9 - i * 0.05) for i in range(10)))]
    hyps = lily_synthesize_hypotheses(words, max_hypotheses=3)
    assert len(hyps) == 3
    assert hyps[0]["text"] == "alt0"
    # Ceiling holds even for absurd config values.
    assert len(lily_synthesize_hypotheses(words, max_hypotheses=999)) <= 8


def test_synthesis_empty_and_garbage_words():
    assert lily_synthesize_hypotheses([]) == []
    assert lily_synthesize_hypotheses([{"alternatives": []}, {}]) == []
    assert lily_synthesize_hypotheses(
        [{"alternatives": [{"content": "   "}]}]
    ) == []


# ---------------------------------------------------------------------------
# (1) STT-mangled proper noun present only in hypothesis slot 2–3 scores
#     correct via the n-best Tier-1 wrapper
# ---------------------------------------------------------------------------

_MADAGASCAR_Q = {"acceptable_answers": ["Madagascar"]}
_MADAGASCAR_HYPS = [
    {"text": "mad at gas car", "confidence": 0.62},   # slot 0 = the 1-best
    {"text": "made a gas car", "confidence": 0.60},
    {"text": "madagascar", "confidence": 0.58},       # the real answer, slot 2
]


def test_one_best_alone_misses_the_mangled_noun():
    # Precondition for the wrapper test: 1-best genuinely fails.
    r = lily_tier1_evaluate_question("mad at gas car", _MADAGASCAR_Q)
    assert r["verdict"] == "uncertain"


def test_mangled_noun_in_slot_2_scores_correct():
    dispersion = lily_nbest_dispersion(_MADAGASCAR_HYPS)  # tight set
    r = lily_tier1_evaluate_nbest(
        "mad at gas car",
        _MADAGASCAR_Q,
        hypotheses=_MADAGASCAR_HYPS,
        dispersion=dispersion,
        dispersion_threshold=lily_config.nbest_dispersion_threshold(),
    )
    assert r["verdict"] == "correct"
    assert r["matched_answer"] == "Madagascar"
    assert r["nbest"]["hit_index"] == 2
    assert r["nbest"]["escalated_by_dispersion"] is False


def test_wrapper_without_hypotheses_matches_single_text_path():
    base = lily_tier1_evaluate_question("canberra", {
        "acceptable_answers": ["Canberra"]
    })
    wrapped = lily_tier1_evaluate_nbest("canberra", {
        "acceptable_answers": ["Canberra"]
    })
    assert wrapped["verdict"] == base["verdict"] == "correct"
    assert wrapped["nbest"]["evaluated"] == 1
    assert wrapped["nbest"]["hit_index"] == 0


# ---------------------------------------------------------------------------
# (2) High-dispersion fractured deliberation escalates instead of scoring
# ---------------------------------------------------------------------------

def test_high_dispersion_escalates_instead_of_scoring():
    fractured = [
        {"text": "no wait madagascar maybe", "confidence": 0.95},
        {"text": "no wait mad at gas car maybe", "confidence": 0.30},
        {"text": "no wait made a gas car maybe", "confidence": 0.35},
    ]
    dispersion = lily_nbest_dispersion(fractured)
    threshold = lily_config.nbest_dispersion_threshold()
    assert dispersion > threshold  # this IS the fractured case
    r = lily_tier1_evaluate_nbest(
        "no wait mad at gas car maybe",
        _MADAGASCAR_Q,
        hypotheses=fractured,
        dispersion=dispersion,
        dispersion_threshold=threshold,
    )
    assert r["verdict"] == "uncertain"       # escalated, never auto-scored
    assert r["nbest"]["escalated_by_dispersion"] is True


def test_high_dispersion_also_demotes_definitive_mc_incorrect():
    q = {
        "choices": ["Paris", "London", "Rome", "Madrid"],
        "canonical_answer": "Paris",
        "acceptable_answers": ["Paris"],
    }
    assert lily_tier1_evaluate_question("letter b", q)["verdict"] == "incorrect"
    r = lily_tier1_evaluate_nbest(
        "letter b", q,
        hypotheses=[{"text": "letter b", "confidence": 0.9}],
        dispersion=0.09,
        dispersion_threshold=0.02,
    )
    assert r["verdict"] == "uncertain"
    assert r["nbest"]["escalated_by_dispersion"] is True


def test_no_dispersion_gate_when_threshold_absent():
    r = lily_tier1_evaluate_nbest(
        "mad at gas car",
        _MADAGASCAR_Q,
        hypotheses=_MADAGASCAR_HYPS,
        dispersion=0.5,  # huge — but no threshold supplied, no gate
    )
    assert r["verdict"] == "correct"
    assert r["nbest"]["escalated_by_dispersion"] is False


def test_precedence_uncertain_blocks_definitive_incorrect():
    q = {
        "choices": ["Paris", "London", "Rome", "Madrid"],
        "canonical_answer": "Paris",
        "acceptable_answers": ["Paris"],
    }
    # 1-best is a clean wrong letter pick; a hypothesis is an unresolvable
    # mumble — the set is NOT definitively wrong, so escalate.
    r = lily_tier1_evaluate_nbest(
        "letter b", q,
        hypotheses=[{"text": "lettuce bee hmm", "confidence": 0.4}],
    )
    assert r["verdict"] == "uncertain"


# ---------------------------------------------------------------------------
# (3) Collector — raw AddTranscript ingestion and drain
# ---------------------------------------------------------------------------

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


def test_collector_ingest_and_drain_roundtrip():
    col = LilyNBestCollector(max_hypotheses=3)
    col.ingest_message(_add_transcript(
        _raw_word("mad", 0.62, start=1.0,
                  extra_alts=(("madagascar", 0.58),)),
        {"type": "punctuation", "alternatives": [
            {"content": ".", "confidence": 1.0}]},  # punctuation skipped
    ))
    out = col.drain(speaker_label="S1")
    assert out is not None
    assert out["source"] == "per_word_synthesis"
    assert out["word_count"] == 1
    assert [h["text"] for h in out["hypotheses"]] == ["mad", "madagascar"]
    assert out["dispersion"] == lily_nbest_dispersion([0.62, 0.58])
    # Second drain: buffer consumed.
    assert col.drain(speaker_label="S1") is None


def test_collector_single_alternative_words_yield_one_hypothesis():
    col = LilyNBestCollector()
    col.ingest_message(_add_transcript(_raw_word("canberra", 0.91)))
    out = col.drain()
    assert len(out["hypotheses"]) == 1
    assert out["dispersion"] == 0.0


def test_collector_speaker_filter():
    col = LilyNBestCollector()
    col.ingest_message(_add_transcript(
        _raw_word("madrid", 0.9, speaker="S1", start=1.0),
        _raw_word("lisbon", 0.8, speaker="S2", start=1.5),
    ))
    s1 = col.drain(speaker_label="S1")
    assert s1["hypotheses"][0]["text"] == "madrid"
    s2 = col.drain(speaker_label="S2")
    assert s2["hypotheses"][0]["text"] == "lisbon"
    assert col.drain() is None


def test_collector_malformed_messages_never_raise():
    col = LilyNBestCollector()
    for garbage in (
        None, "AddTranscript", 42, [], {},
        {"results": "nope"}, {"results": None},
        {"results": ["not-a-dict"]},
        {"results": [{"type": "word"}]},                      # no alternatives
        {"results": [{"type": "word", "alternatives": "x"}]},
        {"results": [{"type": "word",
                      "alternatives": [{"content": "  "}]}]},  # blank content
    ):
        col.ingest_message(garbage)
    assert col.drain() is None


def test_collector_buffer_is_capped():
    col = LilyNBestCollector(max_buffer_words=5)
    for i in range(20):
        col.ingest_message(_add_transcript(_raw_word(f"w{i}", 0.9, start=i)))
    out = col.drain()
    assert out["word_count"] == 5
    assert out["hypotheses"][0]["text"] == "w15 w16 w17 w18 w19"


# ---------------------------------------------------------------------------
# (4) Patch installer — defensive contract
# ---------------------------------------------------------------------------

def _fake_base_client_module():
    def build_start_recognition_message(**kwargs):
        return {
            "message": "StartRecognition",
            "audio_format": {},
            "transcription_config": {"language": "en"},
        }
    return types.SimpleNamespace(
        build_start_recognition_message=build_start_recognition_message
    )


def _fake_voice_client_cls():
    class FakeVoiceAgentClient:
        def __init__(self):
            self.handlers = []
            self.connected = False

        def on(self, event, callback):
            self.handlers.append((event, callback))

        async def connect(self, *args, **kwargs):
            self.connected = True
            return "ok"

    return FakeVoiceAgentClient


def test_patch_arms_and_injects_max_alternatives():
    bc = _fake_base_client_module()
    vac = _fake_voice_client_cls()
    col = LilyNBestCollector()
    assert lily_install_nbest_stt_patch(
        col, 4, _base_client_module=bc, _voice_client_cls=vac,
    ) is True
    msg = bc.build_start_recognition_message()
    assert msg["transcription_config"]["max_alternatives"] == 4
    client = vac()
    assert asyncio.run(client.connect()) == "ok"
    assert client.connected is True
    assert ("AddTranscript", col.ingest_message) in client.handlers


def test_patch_is_idempotent():
    bc = _fake_base_client_module()
    vac = _fake_voice_client_cls()
    col = LilyNBestCollector()
    assert lily_install_nbest_stt_patch(
        col, 3, _base_client_module=bc, _voice_client_cls=vac) is True
    assert lily_install_nbest_stt_patch(
        col, 3, _base_client_module=bc, _voice_client_cls=vac) is True
    client = vac()
    asyncio.run(client.connect())
    # One tap, not two — the second install must not re-wrap.
    assert len(client.handlers) == 1


def test_patch_bad_voice_client_falls_back_cleanly():
    bc = _fake_base_client_module()
    original_build = bc.build_start_recognition_message
    # object() has no `connect` — plugin internals "shifted".
    assert lily_install_nbest_stt_patch(
        LilyNBestCollector(), 3,
        _base_client_module=bc, _voice_client_cls=object(),
    ) is False
    # Nothing was mutated on the healthy half either — no partial patch.
    assert bc.build_start_recognition_message is original_build
    assert "max_alternatives" not in (
        bc.build_start_recognition_message()["transcription_config"]
    )


def test_patch_bad_base_client_module_falls_back_cleanly():
    assert lily_install_nbest_stt_patch(
        LilyNBestCollector(), 3,
        _base_client_module=types.SimpleNamespace(),  # no builder attr
        _voice_client_cls=_fake_voice_client_cls(),
    ) is False


def test_patch_disabled_below_two_alternatives():
    assert lily_install_nbest_stt_patch(
        LilyNBestCollector(), 1,
        _base_client_module=_fake_base_client_module(),
        _voice_client_cls=_fake_voice_client_cls(),
    ) is False


def test_patched_connect_survives_broken_on():
    bc = _fake_base_client_module()

    class BrokenOn:
        def on(self, event, callback):
            raise RuntimeError("emitter changed shape")

        async def connect(self, *args, **kwargs):
            return "ok"

    assert lily_install_nbest_stt_patch(
        LilyNBestCollector(), 3,
        _base_client_module=bc, _voice_client_cls=BrokenOn,
    ) is True
    # The tap failure is logged and swallowed — connect still succeeds.
    assert asyncio.run(BrokenOn().connect()) == "ok"


# ---------------------------------------------------------------------------
# Judge prompt — hypotheses ride as SAID-widening only (judge-never-invents)
# ---------------------------------------------------------------------------

def test_judge_prompt_carries_nbest_hypotheses():
    prompt = lily_build_judge_prompt(
        "What large island nation lies off southeast Africa?",
        "Madagascar",
        [("Sarah", "mad at gas car")],
        acceptable_answers=["Madagascar"],
        hypotheses_by_speaker={"Sarah": _MADAGASCAR_HYPS},
    )
    assert "the player may have said any of" in prompt
    assert "'madagascar'" in prompt
    assert "(confidence 0.58)" in prompt
    assert "N-BEST RULE" in prompt
    # The never-invents constraint, restated in-prompt.
    assert "never change what the answer IS" in prompt
    assert "against the canonical answer supplied above" in prompt


def test_judge_prompt_unchanged_without_hypotheses():
    args = (
        "Capital of Australia?", "Canberra", [("Ben", "cambara")],
    )
    baseline = lily_build_judge_prompt(*args, acceptable_answers=["Canberra"])
    # Hypotheses identical to the attempt are noise — filtered, and the
    # prompt stays byte-identical to the pre-Task-1 shape.
    same = lily_build_judge_prompt(
        *args, acceptable_answers=["Canberra"],
        hypotheses_by_speaker={"Ben": [{"text": "cambara", "confidence": 0.7}]},
    )
    assert same == baseline
    assert "N-BEST RULE" not in baseline


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def test_config_defaults(monkeypatch):
    monkeypatch.delenv("LILY_STT_MAX_ALTERNATIVES", raising=False)
    monkeypatch.delenv("LILY_NBEST_DISPERSION_THRESHOLD", raising=False)
    # Default OFF (live 2026-07-14 23:31: the voice-endpoint schema
    # rejects the injected field and kills the session at the websocket).
    assert lily_config.stt_max_alternatives() == 1
    assert lily_config.nbest_dispersion_threshold() == 0.02


def test_config_bounds(monkeypatch):
    monkeypatch.setenv("LILY_STT_MAX_ALTERNATIVES", "0")
    assert lily_config.stt_max_alternatives() == 1  # kill switch floor
    monkeypatch.setenv("LILY_STT_MAX_ALTERNATIVES", "50")
    assert lily_config.stt_max_alternatives() == 8  # synthesis ceiling
