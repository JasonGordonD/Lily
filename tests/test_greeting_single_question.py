"""Operator directive 2026-08-14: the opener's orienting beat is ONE
question. Live pattern: "Who's at the mic tonight, and what should I call
you?" — the instruction said only "ask who's at the mic tonight", and the
model improvised a SECOND stacked question with "and". The fix is
structural, not scripted: a name ask is allowed only folded into the same
single question joined with "or" (wording free), never as a second
question. Both PART TWO branches (fresh room / familiar device) carry it.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lily_agent import LilyGame


def _fresh_game() -> LilyGame:
    game = LilyGame.bare()
    game.memory_block = ""
    game._first_human_utterance_seen = False
    game.device_candidate_group_id = None
    return game


def test_fresh_room_beat_is_one_question_or_joined():
    text = _fresh_game().greeting_instructions()
    assert "who's at the mic tonight" in text  # the pinned orienting beat
    assert "SAME single question joined with" in text
    assert "'or'" in text
    assert "never stack two separate questions" in text
    # The two-ask shape is explicitly named as the forbidden pattern.
    assert "And what should I call you?' is two asks" in text


def test_familiar_device_beat_carries_the_same_rule():
    game = _fresh_game()
    game.device_candidate_group_id = "grp_abc123"
    text = game.greeting_instructions()
    assert "ONE question only" in text
    assert "never as a second stacked question" in text
