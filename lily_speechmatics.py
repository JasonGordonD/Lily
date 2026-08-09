"""Speechmatics plugin compatibility at the RT-model boundary.

livekit-plugins-speechmatics 1.6.8 exposes only the deprecated
``operating_point`` constructor option, while speechmatics-rt expects
``TranscriptionConfig.model``. Keep every plugin feature and override only
the VoiceAgentConfig handed to the RT SDK.
"""

from __future__ import annotations

from typing import Any

from livekit.plugins.speechmatics import STT
from speechmatics.rt import Model


class LilySpeechmaticsSTT(STT):
    """STT configured with ``model=enhanced`` without deprecation warnings."""

    def _prepare_config(self, *args: Any, **kwargs: Any):
        config = super()._prepare_config(*args, **kwargs)
        # VoiceAgentClient still forwards this field into
        # TranscriptionConfig. None is intentionally omitted from the wire
        # and does not trigger the SDK deprecation warning.
        config.operating_point = None
        advanced = dict(config.advanced_engine_control or {})
        advanced["model"] = Model.ENHANCED
        config.advanced_engine_control = advanced
        return config

