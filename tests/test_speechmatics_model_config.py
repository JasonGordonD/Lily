"""M3: enhanced Speechmatics uses TranscriptionConfig.model, not deprecated op."""

from __future__ import annotations

import inspect
import warnings

from speechmatics.rt import Model
from speechmatics.voice import VoiceAgentClient

import lily_agent
import lily_stt_tuning
from lily_speechmatics import LilySpeechmaticsSTT


def _build() -> LilySpeechmaticsSTT:
    tuned = lily_stt_tuning.lily_tuned_stt_kwargs()
    return LilySpeechmaticsSTT(
        api_key="test-key",
        **tuned,
    )


def test_voice_client_receives_enhanced_model_without_deprecation():
    stt = _build()
    config = stt._prepare_config()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        client = VoiceAgentClient(api_key="test-key", config=config)

    assert not any(
        "operating_point is deprecated" in str(item.message)
        for item in caught
    )
    assert client._transcription_config.model == Model.ENHANCED
    assert client._transcription_config.operating_point is None
    wire = client._transcription_config.to_dict()
    assert wire["model"] == Model.ENHANCED
    assert "operating_point" not in wire


def test_entrypoint_never_passes_deprecated_operating_point():
    source = inspect.getsource(lily_agent.entrypoint)
    assert "operating_point=" not in source
    assert "LilySpeechmaticsSTT(" in source


def test_tuned_artifact_names_the_supported_model_property():
    constructor = lily_stt_tuning.LILY_STT_TUNED["constructor"]
    assert constructor["model"] == "enhanced"
    assert "operating_point" not in constructor
