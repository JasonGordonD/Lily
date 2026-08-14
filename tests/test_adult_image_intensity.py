"""Adult image intensity: sticky state, clear on exit, state block."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lily_scorekeeper import LilyScorekeeper


def test_default_suggestive():
    sk = LilyScorekeeper(session_id="s1")
    assert sk.adult_image_intensity == "suggestive"


def test_set_and_reject_intensity():
    # Unified adult deck (content-mode gate removed): intensity is set and
    # rejected directly; there is no adult-exit reset (no general deck).
    sk = LilyScorekeeper(session_id="s1")
    assert sk.set_adult_image_intensity("explicit") is True
    assert sk.adult_image_intensity == "explicit"
    assert sk.set_adult_image_intensity("nope") is False
    assert sk.adult_image_intensity == "explicit"


def test_snapshot_rehydrate_and_state_block():
    sk = LilyScorekeeper(session_id="s1")
    sk.set_adult_image_intensity("explicit")
    snap = sk.snapshot()
    assert snap["adult_image_intensity"] == "explicit"
    sk2 = LilyScorekeeper(session_id="s2")
    sk2.rehydrate(snap)
    assert sk2.adult_image_intensity == "explicit"
    block = sk.build_state_block()
    assert "adult_image=explicit" in block
