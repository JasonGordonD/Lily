"""WO-LILY-PATCH-003 P7 — pacing requests are honored.

Fixture: "Can you please speak slower?" answered by a fragment, a
half-ack, and no change. Delivery rate is a session field: slower lowers
the TTS speed where supported AND shortens sentences / adds pause at the
text layer regardless, with a single ack applied before the next turn.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_scorekeeper
import lily_tts
from lily_agent import LilyGame
from lily_scorekeeper import LilyScorekeeper


# -- detection -----------------------------------------------------------------


def test_slower_requests_detected():
    for txt in ["Can you please speak slower?", "slow down",
                "you're going too fast", "talk a little slower",
                "not so fast, Lily"]:
        assert lily_scorekeeper.lily_detect_pace_request(txt) == "slow", txt


def test_faster_and_none():
    assert lily_scorekeeper.lily_detect_pace_request("speed it up") == "normal"
    assert lily_scorekeeper.lily_detect_pace_request("too slow") == "normal"
    assert lily_scorekeeper.lily_detect_pace_request("what's the capital?") is None


# -- state + TTS rate ----------------------------------------------------------


def test_set_delivery_pace_lowers_tts_speed():
    tts = lily_tts.LilyTTS.__new__(lily_tts.LilyTTS)
    tts._opts = lily_tts._TTSOpts(
        voice_id="v", api_key="k", model_id="m", output_format="pcm_24000",
    )
    assert tts._opts.pace_multiplier == 1.0
    assert tts.set_pace("slow") is True
    assert tts._opts.pace_multiplier < 1.0
    assert tts.set_pace("normal") is True
    assert tts._opts.pace_multiplier == 1.0
    assert tts.set_pace("garbage") is False


def _make_game():
    game = LilyGame.__new__(LilyGame)
    game.sk = LilyScorekeeper("p7")

    class _FakeTTS:
        def __init__(self):
            self.paces = []

        def set_pace(self, level):
            self.paces.append(level)
            return True

    game.tts = _FakeTTS()
    return game


def test_set_delivery_pace_updates_state_and_voice():
    game = _make_game()
    assert game.set_delivery_pace("slow") is True
    assert game.sk.delivery_pace == "slow"
    assert game.tts.paces == ["slow"]
    assert game.set_delivery_pace("bad") is False


def test_pace_survives_snapshot():
    sk = LilyScorekeeper("p7")
    sk.delivery_pace = "slow"
    snap = sk.snapshot()
    sk2 = LilyScorekeeper("p7")
    sk2.rehydrate(snap)
    assert sk2.delivery_pace == "slow"


def test_pace_field_default_is_normal():
    assert LilyScorekeeper("p7").delivery_pace == "normal"
