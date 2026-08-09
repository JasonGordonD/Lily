"""Speech/delivery extract integrity (voice inventory freeze).

Locks the move contract: choke-point names stay on LilyGame, bodies live
in lily_speech_delivery, and spoken directives are byte-stable.
"""

from __future__ import annotations

from pathlib import Path

import lily_agent
import lily_speech_delivery
from lily_agent import LilyGame
from lily_speech_delivery import LilySpeechDeliveryMixin


_CHOKE_POINTS = (
    "gated_say",
    "expect_delivery",
    "register_delivery_claim",
    "arm_reair_gate",
    "arm_cut_recovery",
    "early_answer_check",
    "mc_early_answer_check",
    "note_playout_started",
)


def test_lilygame_inherits_speech_delivery_mixin():
    assert issubclass(LilyGame, LilySpeechDeliveryMixin)


def test_choke_points_resolve_on_lilygame_from_mixin():
    for name in _CHOKE_POINTS:
        assert hasattr(LilyGame, name)
        assert getattr(LilyGame, name) is getattr(LilySpeechDeliveryMixin, name)


def test_directives_reexported_byte_stable():
    assert lily_agent._CUT_RECOVERY_DIRECTIVE is lily_speech_delivery._CUT_RECOVERY_DIRECTIVE
    assert lily_agent._REGEN_REAIR_DIRECTIVE is lily_speech_delivery._REGEN_REAIR_DIRECTIVE
    assert lily_agent._REGEN_DELIVERY_DIRECTIVE is lily_speech_delivery._REGEN_DELIVERY_DIRECTIVE
    assert "sorry, looks like I cut out there" in lily_agent._CUT_RECOVERY_DIRECTIVE


def test_voice_inventory_names_speech_delivery_extract():
    text = (Path(__file__).resolve().parents[1] / "docs" / "voice_inventory.md").read_text()
    assert "speech/delivery" in text
    assert "zero string" in text.lower()
