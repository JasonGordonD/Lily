"""The double verdict (live 2026-08-12 lily-639007): every answered
question aired TWO verdict emissions — the ORGANIC reply (17s late,
"you're at three" FABRICATED) and adjudicate's keyed ledger-true
composite ("that's two for you"). Root cause: both existing suppressor
checks are flow-dependent, and W4's relaxed beat-close adjudicates at
the transcript seam and CLOSES the window ~2s before the framework
commits the turn — so at commit, the exact-text mark could miss and the
window-liveness prehook always declined. Ownership now follows the DATA:
a turn whose text the scorekeeper consumed as an answer candidate
belongs to adjudication, whatever the event ordering.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lily_scorekeeper import LilyScorekeeper


def _sk():
    sk = LilyScorekeeper("dbl-verdict")
    sk.bind_speaker("S1", "Rami")
    sk.start_question({
        "prompt": "What famous Greek mountain was home to the twelve "
                  "Olympian gods?",
        "canonical_answer": "Mount Olympus",
        "acceptable_answers": ["olympus", "mount olympus"],
    })
    return sk


def test_the_live_shape_candidate_recorded_then_window_closed():
    """The exact lily-639007 ordering: candidate lands, beat closes,
    window gone — the commit-time check must still own the turn."""
    sk = _sk()
    sk.open_answer_window(duration=15.0)
    sk.on_transcript_segment(
        text="Olympus.", speaker_label="S1", is_final=True,
        now=time.time(), segment_start_time=time.time(),
        segment_end_time=time.time() + 1,
    )
    sk.close_answer_window()  # the relaxed beat-close, before commit
    assert not sk.answer_window_open
    assert sk.recent_answer_text_matches("Olympus.") is True


def test_non_answer_chatter_is_never_owned():
    sk = _sk()
    sk.open_answer_window(duration=15.0)
    sk.on_transcript_segment(
        text="Olympus.", speaker_label="S1", is_final=True,
        now=time.time(), segment_start_time=time.time(),
        segment_end_time=time.time() + 1,
    )
    assert sk.recent_answer_text_matches(
        "why are you repeating yourself"
    ) is False


def test_ownership_expires():
    sk = _sk()
    sk.note_recent_answer_text("Olympus.", now=1_000.0)
    assert sk.recent_answer_text_matches("Olympus.", now=1_010.0) is True
    assert sk.recent_answer_text_matches("Olympus.", now=1_040.0) is False


def test_hook_consults_the_data_check():
    import inspect

    import lily_agent

    src = inspect.getsource(lily_agent.LilyAgent.on_user_turn_completed)
    assert "recent_answer_text_matches" in src
    assert "candidate_owned" in src
