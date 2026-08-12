"""C14b (WO-LILY-HOSTLOOP-001) — per-question delivery timestamps persist.

These four moments (core_sentence_spoken_at, delivery_confirmed_at,
window_opened_at, window_closed_at) existed only as transient log lines;
the C3/C4 gates need them queryable. They ride
lily_sessions.metadata.question_timeline via the existing session-end
write — no DDL (C14a's join migration is report-only, per the WO).
"""

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_agent
from lily_scorekeeper import LilyScorekeeper


def _sk() -> LilyScorekeeper:
    sk = LilyScorekeeper("c14b")
    sk.bind_speaker("S1", "Rami")
    sk.start_question({
        "prompt": "Capital of France?", "canonical_answer": "Paris",
        "acceptable_answers": ["paris"],
    })
    return sk


def test_window_edges_stamp_on_open_and_close():
    sk = _sk()
    sk.open_answer_window(duration=15.0, now=1_000.0)
    sk.close_answer_window()
    q = sk.question_timeline[sk.question_number]
    assert q["window_opened_at"] == 1_000.0
    assert q["window_closed_at"] >= 1_000.0


def test_first_stamp_wins_and_steal_reopen_is_separate():
    sk = _sk()
    sk.open_answer_window(duration=15.0, now=1_000.0)
    sk.close_answer_window()
    sk.open_answer_window(duration=8.0, now=1_020.0)  # steal window
    q = sk.question_timeline[sk.question_number]
    assert q["window_opened_at"] == 1_000.0  # original edge preserved
    assert q["window_reopened_at"] == 1_020.0


def test_close_without_open_stamps_nothing():
    sk = _sk()
    sk.close_answer_window()
    timeline = getattr(sk, "question_timeline", {}) or {}
    assert "window_closed_at" not in timeline.get(sk.question_number, {})


def test_delivery_confirm_and_core_completion_sites_stamp():
    """Source pins: the confirmed-delivery block and the C3a core-
    completion arm both stamp the timeline."""
    src = inspect.getsource(lily_agent)
    i = src.index('note_question_time("delivery_confirmed_at")')
    assert '_delivery' in src[i - 300:i]  # keyed on the delivery claim
    import lily_speech_delivery

    sd = inspect.getsource(lily_speech_delivery)
    j = sd.index('note_question_time("core_sentence_spoken_at")')
    assert "CORE_COMPLETION_ARM" in sd[j - 700:j]


def test_timeline_rides_the_session_end_metadata():
    src = inspect.getsource(lily_agent)
    assert src.count('"question_timeline"') >= 2  # both write sites
