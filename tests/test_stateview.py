"""StateView schema — the stable state block is built from a typed view and
rendered in a fixed order (W2b item 2), not hand-concatenated.

Two acceptance properties the schema exists to guarantee:
  * the STABLE prefix is byte-identical across renders of the same state
    (the prompt-cache boundary — an accidental extra line or a reordered
    field silently busts the cache), and
  * canonical_answer NEVER reaches the stable block (the armed NEXT-QUESTION
    slot carries prompt/category/choices/image status only).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_audeering_consumers
from lily_agent import LilyGame
from lily_glass import StateView
from lily_scorekeeper import LilyScorekeeper

_SECRET = "Jupiter-is-the-canonical-answer-do-not-leak"


def _game_with_armed_question() -> LilyGame:
    game = LilyGame.__new__(LilyGame)
    game.sk = LilyScorekeeper("stateview")
    game.sk.bind_speaker("S1", "Rami")
    game.game_started = True
    game.game_over = False
    game.next_question = None
    game._pending_unbound_award = None
    game.availability_flags = None
    game.promoted_categories = []
    game.last_addressee_judgment = None
    game.acoustic = lily_audeering_consumers.LilyAcousticState()
    game._state_note = None
    # Armed, window CLOSED -> the NEXT QUESTION need-to-know line renders.
    game.armed_question = {
        "prompt": "Which planet has the shortest day?",
        "category": "space",
        "canonical_answer": _SECRET,
        "acceptable_answers": [_SECRET.lower()],
        "reveal_color": _SECRET,
    }
    return game


def test_stable_block_is_byte_identical_across_renders():
    # Y1: the same state rendered twice yields the identical stable prefix —
    # no accidental extra line, no reordered field.
    game = _game_with_armed_question()
    stable_a, _ = game.build_state_block_split(now=1000.0)
    stable_b, _ = game.build_state_block_split(now=2000.0)  # now only moves volatile
    assert stable_a == stable_b


def test_canonical_answer_never_in_stable_block():
    # The armed question is on the stable block, but only its need-to-know
    # fields — never the answer material.
    game = _game_with_armed_question()
    stable, _ = game.build_state_block_split(now=1000.0)
    assert "NEXT QUESTION" in stable          # the question IS present…
    assert "shortest day" in stable           # …with its prompt…
    assert _SECRET not in stable              # …but never its answer.
    assert _SECRET.lower() not in stable


def test_state_view_render_is_ordered_and_skips_empty():
    # The render walks _ORDER and splices list slots in place; empty slots
    # are skipped, so the line set and order are a pure function of the view.
    view = StateView()
    view.score = "score: Rami 2"
    view.roster = "roster: 1 player"
    view.acoustic = ["[env: warm room]"]
    view.lobby = ["game not started: lobby", "extra categories: history"]
    assert view.render() == [
        "score: Rami 2",
        "roster: 1 player",
        "[env: warm room]",
        "game not started: lobby",
        "extra categories: history",
    ]
    # Order follows the schema, not insertion: score precedes roster
    # precedes acoustic precedes lobby regardless of assignment order.
    v2 = StateView()
    v2.lobby = ["z"]
    v2.answered_closed = "a"
    assert v2.render() == ["a", "z"]


def test_state_view_default_is_empty():
    assert StateView().render() == []
