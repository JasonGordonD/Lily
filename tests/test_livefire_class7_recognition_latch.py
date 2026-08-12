"""WO-LILY-LIVEFIRE-001 CLASS 7 — recognition latch (7a).

Fixture lily-639007-f80aa6bf: after an explicit Start, a LATE_RECOGNITION
beat aired as act=game_start ("welcome back... want relaxed pacing, or change
anything?") and q_1's kickoff was suppressed (no_delivery_owner). 7a: once
start_game commits, recognition speech is FORBIDDEN — it belongs to the
greeting/intake window, never inside or after game start.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_hotfix006_transitions import _make_game


def test_recognition_forbidden_once_start_committed():
    game = _make_game()
    game.memory_block = "[RETURNING TABLE] history..."
    game._late_recognition_fired = False
    game._late_recognition_pending = False
    game.say_registry.claim("session_greet", "s0")

    # Pre-start: recognition is allowed (nothing forbids it here).
    game._game_start_committed = False
    assert game.late_recognition_blocked_reason() != "game_start_committed"

    # Start commits — recognition speech is now forbidden outright.
    game._game_start_committed = True
    assert game.late_recognition_blocked_reason() == "game_start_committed"


def test_late_recognition_is_retired_not_deferred_after_start():
    game = _make_game()
    game.memory_block = "[RETURNING TABLE] history..."
    game._late_recognition_fired = False
    game._late_recognition_pending = False
    game.say_registry.claim("session_greet", "s0")
    game._game_start_committed = True

    fired = game.maybe_fire_late_recognition()
    # It does not fire, and it is RETIRED (not left pending for a later seam
    # it can never safely take) — so the game-start composite cannot ride it.
    assert fired is False
    assert game._late_recognition_fired is True
    assert game._late_recognition_pending is False


def test_start_game_sets_the_commit_latch():
    # The latch is wired to the real start commit, distinct from
    # game_started (which several sites use as "greeting dispatched").
    import inspect
    from lily_agent import LilyGame
    src = inspect.getsource(LilyGame.start_game)
    assert "_game_start_committed = True" in src
