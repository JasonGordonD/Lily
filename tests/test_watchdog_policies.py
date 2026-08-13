"""Per-policy unit tests for the W2b idle-watchdog policy table.

Each row is exercised in isolation: its `when` predicate fires on exactly the
state it names, and its `run` either ENDS the tick (returns _WATCH_HALT — the
old `continue`) or falls through. The parity contract for commit A is that
this table, walked in order, reproduces the pre-refactor _idle_watchdog (the
full suite is the behavioural net; these pin the seams).
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lily_agent import LilyGame, WatchPolicy, _WATCH_HALT


def _run(coro):
    return asyncio.run(coro)


def _bare_game() -> LilyGame:
    game = LilyGame.bare()
    game.sk = SimpleNamespace(
        answer_window_open=False, question_number=1, session_id="wp"
    )
    game._adjudicating = False
    game._question_transitioning = False
    game.armed_question = None
    return game


# -- table shape ----------------------------------------------------------


def test_policy_table_is_ordered_and_typed():
    table = LilyGame.bare()._make_watch_policies()
    assert all(isinstance(p, WatchPolicy) for p in table)
    names = [p.name for p in table]
    # Commit B: the OPERATOR'S stated priority — supply_silent ahead of the
    # question/armed halts; address/question-reoffer lowest.
    assert names == [
        "hold_timeout", "supply_silent", "progression_paused", "busy_reset",
        "armed", "address_unanswered", "question_reoffer", "idle_rearm",
        "supply_stall",
    ]
    assert all(p.every_ticks == 1 for p in table)
    # The 2260354c fix, structurally: supply_silent precedes every HALT that
    # could hide it (question_reoffer, armed, paused, busy).
    assert names.index("supply_silent") < names.index("question_reoffer")
    assert names.index("supply_silent") < names.index("armed")
    assert names.index("supply_silent") < names.index("progression_paused")


def _tick_game_supply_silent() -> LilyGame:
    """A game whose supply line is silent, wired so a full tick can run the
    early rows without the heavy armed/idle machinery."""
    g = _bare_game()
    g._hold_active = False
    g._supply_silent_window = lambda: True
    g._supply_silent_ticks = LilyGame.SUPPLY_SILENT_WARN_TICKS
    g.recovered = []
    g.ensure_supply_recovery = lambda trigger: g.recovered.append(trigger)
    g.progression_paused_reason = lambda: ""
    # armed policy body, stubbed to the not-confirmed (reconcile) branch.
    g._prefetch_stall_ticks = 0
    g._supply_stall_ticks = 0
    g._armed_limbo_ticks = 0
    g.say_registry = SimpleNamespace(state=lambda key: "pending")
    g.reconcile_undelivered_claim = lambda: None
    return g


def test_supply_silent_fires_while_a_question_is_armed():
    # Commit B acceptance (2260354c): supply recovery must run even while a
    # question is armed — it precedes the armed HALT in the table.
    g = _tick_game_supply_silent()
    g.armed_question = {"prompt": "?"}  # armed -> armed row will HALT the tick
    _run(g._run_watch_policies())
    assert g.recovered == ["watchdog_silent_window"]


def test_supply_silent_fires_while_a_conversational_question_is_pending():
    # The A->B distinguisher: under the OLD order question_reoffer (which
    # HALTS whenever _question_pending) sat ahead of supply_silent, so a
    # pending conversational question hid supply recovery. Under B,
    # supply_silent runs first. Non-idle here via an open answer window (a
    # trivia window can be open while a conversational question is pending).
    g = _tick_game_supply_silent()
    g.armed_question = None
    g.sk.answer_window_open = True   # non-idle -> supply_silent recovers
    g._question_pending = True       # in commit A this would HALT before supply_silent
    _run(g._run_watch_policies())
    assert g.recovered == ["watchdog_silent_window"]


# -- when predicates ------------------------------------------------------


def test_when_hold():
    g = _bare_game()
    g._hold_active = False
    assert LilyGame._when_hold(g) is False
    g._hold_active = True
    assert LilyGame._when_hold(g) is True


def test_when_question_pending():
    g = _bare_game()
    g._question_pending = False
    assert LilyGame._when_question_pending(g) is False
    g._question_pending = True
    assert LilyGame._when_question_pending(g) is True


def test_when_progression_paused_reads_the_reason():
    g = _bare_game()
    g.progression_paused_reason = lambda: ""
    assert LilyGame._when_progression_paused(g) is False
    g.progression_paused_reason = lambda: "meta_between_rounds"
    assert LilyGame._when_progression_paused(g) is True


def test_when_busy_covers_window_adjudicating_transitioning():
    g = _bare_game()
    assert LilyGame._when_busy(g) is False
    g.sk.answer_window_open = True
    assert LilyGame._when_busy(g) is True
    g.sk.answer_window_open = False
    g._adjudicating = True
    assert LilyGame._when_busy(g) is True
    g._adjudicating = False
    g._question_transitioning = True
    assert LilyGame._when_busy(g) is True


def test_when_armed_and_idle_are_complementary():
    g = _bare_game()
    g.armed_question = None
    assert LilyGame._when_armed(g) is False
    assert LilyGame._when_idle(g) is True
    g.armed_question = {"prompt": "?"}
    assert LilyGame._when_armed(g) is True
    assert LilyGame._when_idle(g) is False


# -- halt / fall-through semantics ---------------------------------------


def test_hold_halts_when_not_timed_out_releases_when_timed_out():
    g = _bare_game()
    released = []
    g.release_hold = lambda reason: released.append(reason)
    # Not timed out -> ends the tick, no release.
    g.hold_timed_out = lambda: False
    assert _run(LilyGame._wp_hold(g)) == _WATCH_HALT
    assert released == []
    # Timed out -> release and FALL THROUGH (non-halt return).
    g.hold_timed_out = lambda: True
    assert _run(LilyGame._wp_hold(g)) != _WATCH_HALT
    assert released == ["timeout"]


def test_busy_reset_zeroes_counters_and_halts():
    g = _bare_game()
    g._prefetch_stall_ticks = 5
    g._armed_limbo_ticks = 3
    g._supply_stall_ticks = 9
    assert _run(LilyGame._wp_busy_reset(g)) == _WATCH_HALT
    assert g._prefetch_stall_ticks == 0
    assert g._armed_limbo_ticks == 0
    assert g._supply_stall_ticks == 0


def test_supply_silent_falls_through_and_resets_when_quiet():
    g = _bare_game()
    g._supply_silent_ticks = 4
    g._supply_silent_window = lambda: False  # not in a silent window
    assert _run(LilyGame._wp_supply_silent(g)) != _WATCH_HALT
    assert g._supply_silent_ticks == 0  # else-branch reset


def test_idle_rearm_halts_with_supply_falls_through_without():
    g = _bare_game()
    g._armed_limbo_ticks = 7
    # No supply in hand -> reset limbo counter, fall through to supply_stall.
    g.next_question = None
    assert _run(LilyGame._wp_idle_rearm(g)) != _WATCH_HALT
    assert g._armed_limbo_ticks == 0
    # Supply in hand -> arm + halt (arm/nudge machinery stubbed).
    g.next_question = {"prompt": "next?"}
    g._supply_stall_ticks = 2
    g.session = None  # skip the nudge branch cleanly
    g.arm_next_question = lambda: True
    assert _run(LilyGame._wp_idle_rearm(g)) == _WATCH_HALT
    assert g._supply_stall_ticks == 0
