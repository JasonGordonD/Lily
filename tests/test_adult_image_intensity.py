"""Adult image intensity: sticky state, clear on exit, state block."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lily_scorekeeper import LilyScorekeeper


def test_default_suggestive():
    sk = LilyScorekeeper(session_id="s1")
    assert sk.adult_image_intensity == "suggestive"


def test_set_and_clear_on_adult_exit():
    sk = LilyScorekeeper(session_id="s1")
    sk.set_mode("adult")
    assert sk.set_adult_image_intensity("explicit") is True
    assert sk.adult_image_intensity == "explicit"
    assert sk.set_adult_image_intensity("nope") is False
    assert sk.adult_image_intensity == "explicit"
    sk.set_mode("general")
    assert sk.adult_image_intensity == "suggestive"


def test_snapshot_rehydrate_and_state_block():
    sk = LilyScorekeeper(session_id="s1")
    sk.set_mode("adult")
    sk.set_adult_image_intensity("explicit")
    snap = sk.snapshot()
    assert snap["adult_image_intensity"] == "explicit"
    sk2 = LilyScorekeeper(session_id="s2")
    sk2.rehydrate(snap)
    assert sk2.adult_image_intensity == "explicit"
    block = sk.build_state_block()
    assert "adult_image=explicit" in block
