"""REFACTOR W2a — the deterministic verdict sheet.

lily_verdict_sheet composes the verdict beat from the committed ruling, model-
free, replacing the 8-13s LLM composite. These tests construct the sheet
directly — no LilyGame, no event loop.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lily_scorekeeper import lily_scores_sheet, lily_verdict_sheet  # noqa: E402


def test_scores_sheet_round_close_reads_standings():
    s = lily_scores_sheet(ledger_scores={"Rami": 3, "Deb": 2})
    assert s == "That closes the round. On the board: Rami at three; Deb at two."


def test_scores_sheet_finale_names_the_winner():
    s = lily_scores_sheet(
        ledger_scores={"Rami": 5, "Deb": 2}, final=True, winner="Rami"
    )
    assert s == "That's the game — Rami takes it! On the board: Rami at five; Deb at two."


def test_scores_sheet_finale_no_sole_winner():
    s = lily_scores_sheet(ledger_scores={"Rami": 3, "Deb": 3}, final=True, winner=None)
    assert s.startswith("That's the game!")


def test_scores_sheet_empty_ledger_degrades():
    assert lily_scores_sheet(ledger_scores={}) == "That closes the round."
    assert lily_scores_sheet(ledger_scores={}, final=True) == "That's the game!"


def test_scores_sheet_never_restates_a_verdict():
    # Standings-only by construction — no correct/incorrect, no point award.
    s = lily_scores_sheet(ledger_scores={"Rami": 3})
    assert "correct" not in s.lower() and "point" not in s.lower()


def test_winner_scored_no_receipt_rules_then_points():
    s = lily_verdict_sheet(answer="Jupiter", winner="Rami", winner_scored=True)
    assert s == "Correct — Jupiter! Point to Rami."


def test_winner_scored_with_receipt_skips_the_verdict_word():
    # HOSTLOOP-001 C6: the receipt already aired the verdict word; do not rule
    # a second time — carry on to the answer and the point.
    s = lily_verdict_sheet(
        answer="Jupiter", winner="Rami", winner_scored=True, receipt_aired=True
    )
    assert s == "It's Jupiter — point to Rami."
    assert "Correct" not in s


def test_nobody_landed_it():
    s = lily_verdict_sheet(answer="Marie Curie", winner=None, winner_scored=False)
    assert s == "Nobody landed it — it was Marie Curie."


def test_nobody_with_receipt_does_not_recredit():
    s = lily_verdict_sheet(
        answer="Marie Curie", winner=None, winner_scored=False, receipt_aired=True
    )
    assert s == "It was Marie Curie — no point this time."
    assert "Correct" not in s and "Point to" not in s


def test_name_passed_but_not_scored_is_still_nobody():
    # winner_scored is the ledger truth; a name without a committed correct row
    # must not be credited.
    s = lily_verdict_sheet(answer="Saturn", winner="Deb", winner_scored=False)
    assert s == "Nobody landed it — it was Saturn."


def test_empty_answer_degrades_gracefully():
    assert lily_verdict_sheet(answer="", winner="Rami", winner_scored=True) == \
        "Correct — point to Rami!"
    assert lily_verdict_sheet(answer="", winner=None, winner_scored=False) == \
        "Nobody landed it."


def test_answer_and_names_are_stripped():
    s = lily_verdict_sheet(answer="  Jupiter  ", winner="  Rami ", winner_scored=True)
    assert s == "Correct — Jupiter! Point to Rami."


def test_sheet_is_deterministic():
    a = lily_verdict_sheet(answer="Jupiter", winner="Rami", winner_scored=True)
    b = lily_verdict_sheet(answer="Jupiter", winner="Rami", winner_scored=True)
    assert a == b
