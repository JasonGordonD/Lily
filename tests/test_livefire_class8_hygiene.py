"""WO-LILY-LIVEFIRE-001 CLASS 8 — hygiene sweep.

Fixture lily-639007-f80aa6bf. Small items:
- 8a: one PRIMARY greet source (on_enter); the entrypoint path is an explicit
  fallback gated on the registry, not an always-suppressed dup.
- 8b: the yield-after-question gate clips STACKED questions only — a single
  question with a declarative tail is preserved (the live 82-char cut after
  name-bind).
- 8c: lobby-vamp stacking — flagged (LLM-generated, see CHANGELOG/README).
- 8d: the 6a memory-vs-prefetch finding — two independent problems (see the
  Class 6 diagnosis in CHANGELOG).
"""

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_agent
from lily_say_gate import lily_stacked_question_flag, lily_yield_after_first_question


# -- 8b: yield applies to stacked questions only --------------------------

def test_single_question_with_declarative_tail_is_one_question():
    # This must NOT trip the yield gate (n_questions < 2) — the live
    # 82-char non-question tail after name-bind stays.
    assert lily_stacked_question_flag(
        "Got it, Rami? Let's set you up with a category."
    ) == 1


def test_stacked_questions_count_two():
    assert lily_stacked_question_flag(
        "Want a refresher on the options? Or straight in?"
    ) == 2


def test_tts_node_gates_yield_on_stacked_only():
    # The tts_node call site only clips when there are >= 2 questions.
    src = inspect.getsource(lily_agent.LilyAgent.tts_node)
    assert "n_questions >= 2" in src
    assert "lily_yield_after_first_question" in src


def test_yield_function_still_clips_at_first_question():
    # The function itself is unchanged — the caller decides when to use it.
    clipped, yielded = lily_yield_after_first_question(
        "Refresher? Or dive in?"
    )
    assert clipped == "Refresher?"
    assert yielded is True


# -- 8a: entrypoint greet is an explicit fallback, not a suppressed dup ----

def test_entrypoint_greet_gated_on_registry_state():
    src = inspect.getsource(lily_agent.entrypoint)
    # Both entrypoint openers only dispatch when on_enter did NOT already
    # claim the opener — suppression is no longer load-bearing.
    greet_guard = src.index('say_registry.state("session_greet") is None')
    rejoin_guard = src.index('say_registry.state("session_rejoin") is None')
    # Each guard sits before its own gated_say(source="entrypoint").
    greet_say = src.index('"greet"', greet_guard)
    rejoin_say = src.index('"rejoin"', rejoin_guard)
    assert greet_guard < greet_say
    assert rejoin_guard < rejoin_say
