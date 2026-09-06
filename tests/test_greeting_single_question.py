"""Operator decision (WO-LILY-VOICE-TRUTH-001 PAIR 3, 2026-09-06) — REVERSES
the 2026-08-14 one-question-'or' rule (69186bf): the name question stands
alone on its own turn; who-else-is-here and the fun fact are separate beats
on later turns; never folded into one breath, never joined with 'or'. Both
PART TWO branches (fresh room / familiar device) carry the SAME verbatim
text, and so does prompts/lily_system.txt (the three copies are pinned
byte-identical in test_voice_truth_wo.py).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lily_agent import LilyGame, _PAIR3_NAME_QUESTION_ALONE


def _fresh_game() -> LilyGame:
    game = LilyGame.bare()
    game.memory_block = ""
    game._first_human_utterance_seen = False
    game.device_candidate_group_id = None
    return game


def test_fresh_room_beat_asks_the_name_on_its_own_turn():
    text = _fresh_game().greeting_instructions()
    assert "who's at the mic tonight" in text  # the pinned orienting beat
    assert _PAIR3_NAME_QUESTION_ALONE in text
    assert "never join them with 'or'" in text
    # The reversed rule is gone in every form.
    assert "SAME single question joined with" not in text
    assert "join it into that same question with 'or'" not in text
    assert "or, what should I call you" not in text


def test_familiar_device_beat_carries_the_same_rule():
    game = _fresh_game()
    game.device_candidate_group_id = "grp_abc123"
    text = game.greeting_instructions()
    assert "ONE question only" in text
    assert _PAIR3_NAME_QUESTION_ALONE in text
    assert "join it into that same question with 'or'" not in text
