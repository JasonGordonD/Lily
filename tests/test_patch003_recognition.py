"""WO-LILY-PATCH-003 P5 + P10-lint — recognition-once, stacked-question lint.

P5 fixture: recognized by name at greet (18:30:45), then "Wait — Rami!
NOW I've got you" two minutes later, mid-conversation, over his open
question. The late beat fires at most once, only on a real
unknown→identified transition, never when the greet already recognized
them, and never over an open exchange.

P10-lint fixture: "anything I should know about you before we start — or
you ready to dive straight in?" — two questions stacked in one breath.
One question per turn; the lint flags >1 (rewrite stays Doc's).
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_say_gate
from lily_agent import LilyGame
from lily_scorekeeper import LilyScorekeeper


def _make_game():
    game = LilyGame.__new__(LilyGame)
    game.sk = LilyScorekeeper("p5-fixture")
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game.memory_block = "[RETURNING TABLE] Rami — 4 wins"
    game.memory_player_names = ["Rami"]
    game.prefs = {}
    game.game_started = True
    game._late_recognition_fired = False
    game._recognized_at_greet = False
    game.pending_clarify = {}
    game.instructed_replies = []
    game.instructed_reply = lambda t: game.instructed_replies.append(t)
    game.say_registry.claim("session_greet", owner="g1")  # greet went out
    return game


# -- P5: recognition-once ------------------------------------------------------


def test_recognized_at_greet_kills_the_late_beat():
    game = _make_game()
    game._recognized_at_greet = True
    game.maybe_fire_late_recognition()
    assert game.instructed_replies == []
    assert game._late_recognition_fired is True  # consumed, never re-armed


def test_late_beat_defers_over_an_open_window():
    game = _make_game()
    game.sk.open_answer_window(duration=30.0, now=100.0)
    game.maybe_fire_late_recognition()
    assert game.instructed_replies == []  # held for a seam
    assert game._late_recognition_fired is False  # NOT consumed — will retry
    # Window closes → the next invocation fires it, once.
    game.sk.close_answer_window()
    game.maybe_fire_late_recognition()
    assert len(game.instructed_replies) == 1
    assert game._late_recognition_fired is True
    game.maybe_fire_late_recognition()
    assert len(game.instructed_replies) == 1  # exactly once


def test_late_beat_fires_once_at_a_seam_on_real_transition():
    game = _make_game()
    game.maybe_fire_late_recognition()
    assert len(game.instructed_replies) == 1
    assert "MID-SESSION" in game.instructed_replies[0]


# -- P10 lint: stacked questions -----------------------------------------------


def test_stacked_question_lint_counts_terminal_questions():
    # The reliable, mechanical contract: distinct sentence-terminal
    # question marks. Two separately-punctuated questions in a turn flag.
    single = "Ready for question one, Rami?"
    stacked = "Want a refresher on the options? Or straight in?"
    triple = "First time? Or a regular? Want the rundown?"
    assert lily_say_gate.lily_stacked_question_flag(single) == 1
    assert lily_say_gate.lily_stacked_question_flag(stacked) == 2
    assert lily_say_gate.lily_stacked_question_flag(triple) == 3
    assert lily_say_gate.lily_stacked_question_flag("No questions here.") == 0
    # A single disjunctive question sharing one '?' is grammatically ONE
    # question — the semantic "two unrelated asks joined by 'or'" case is
    # a prompt-contract concern (Doc), not this mechanical lint.
    disjunctive = ("Anything I should know about you — or ready to dive "
                   "straight in?")
    assert lily_say_gate.lily_stacked_question_flag(disjunctive) == 1


def test_stacked_question_lint_ignores_audio_tags():
    tagged = "[excited] You got it! Ready for the next one?"
    assert lily_say_gate.lily_stacked_question_flag(tagged) == 1


def test_false_clean_slate_claim_is_caught_for_pending_returner():
    assert lily_say_gate.lily_false_clean_slate_claim(
        "Since this table hasn't played a recorded game with me yet, "
        "tonight is effectively a clean slate."
    )
    assert not lily_say_gate.lily_false_clean_slate_claim(
        "My table card hasn't connected yet, but I believe you."
    )
