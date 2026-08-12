"""WO-LILY-LIVEFIRE-001 CLASS 4 — STOP is a brake, not a substring.

Fixture lily-639007-f80aa6bf 17:59:55: "Why why why stop? Why?" fired the
stop primitive (bare stop in a solo room) and, worse, the freeze burned
kb_457 — the next Greece card. A question ABOUT stopping is meta, never the
brake (4a); and a genuine STOP freezes supply but never burns armed/supplied
content (4b).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_scorekeeper
from lily_scorekeeper import lily_detect_stop


# -- 4a: meta/question about stopping is never a brake --------------------

def test_fixture_why_stop_is_not_a_brake():
    # The exact live line, in a solo room (where a bare stop would fire).
    assert lily_detect_stop("Why why why stop? Why?", solo=True) is False


def test_meta_stop_questions_are_not_brakes():
    for line in [
        "why stop?",
        "why did you stop?",
        "when do you stop?",
        "did you stop the game?",
        "whats the reason you stopped?",
        "how come you stopped?",
    ]:
        assert lily_detect_stop(line, solo=True) is False, line


# -- genuine imperatives still fire ---------------------------------------

def test_genuine_stops_still_fire():
    assert lily_detect_stop("stop", solo=True) is True
    assert lily_detect_stop("stop stop stop", solo=False) is True
    assert lily_detect_stop("Lily, stop", solo=False) is True
    assert lily_detect_stop("stop the game", solo=True) is True


def test_polite_imperative_stop_still_fires():
    # A polite request is still a command — no interrogative/reason lead-in.
    assert lily_detect_stop("can you stop", solo=True) is True
    assert lily_detect_stop("please stop", solo=True) is True


def test_imperative_stop_then_question_is_untouched():
    # "stop" leads; a trailing question does not turn it into meta.
    assert lily_detect_stop("stop, why are you still going?", solo=True) is True


# -- 4b: a genuine STOP never burns armed/supplied content ----------------

def test_stop_freeze_preserves_armed_and_next(monkeypatch):
    import lily_agent
    game = lily_agent.LilyGame.__new__(lily_agent.LilyGame)
    game.sk = lily_scorekeeper.LilyScorekeeper("class4")
    armed = {"question_id": "kb_457", "prompt": "Greece Q", "category": "Greece"}
    nxt = {"question_id": "kb_999", "prompt": "Greece Q2", "category": "Greece"}
    game.armed_question = armed
    game.next_question = nxt
    game._window_timer = None
    game._bed_handle = None
    game._prefetch_task = None
    game._steal_window = False
    game.pending_clarify = {}
    # A burn would call _burn_question — fail loud if the freeze tries it.
    burned = []
    game._burn_question = lambda q, **kw: burned.append(q.get("question_id"))
    game.clear_pending_clarify_for_question = lambda *a, **k: None
    game.publish_attributes_nowait = lambda: None
    game.publish_metadata = None

    game._freeze_game_delivery_for_stop()

    # Supply is frozen …
    assert game._delivery_stop_sticky is True
    # … but the armed and prefetched cards SURVIVE and were never burned.
    assert game.armed_question is armed
    assert game.next_question is nxt
    assert burned == [], "STOP must not burn armed/supplied content"
