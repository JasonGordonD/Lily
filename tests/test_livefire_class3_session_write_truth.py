"""WO-LILY-LIVEFIRE-001 CLASS 3 — session write truth.

Fixture lily-639007-f80aa6bf: asked_history recorded 5 delivered Greece
questions, but q6 was armed, burned by a STOP, and never asked — the armed
cursor question_number reached 6 and the winner write said "4 point(s) over
6 question(s)". Player-facing counts now derive from asked_history (asked =
delivered), never from the armed/supply cursor; a burned/discarded card
never increments the count.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lily_agent import LilyGame
from lily_memory import lily_build_session_summary, lily_session_winner


def _game_with_asked(n: int) -> LilyGame:
    game = LilyGame.bare()
    game.asked_history = [
        {"question_id": f"q_{i}", "category": "Greece"} for i in range(n)
    ]
    return game


# -- delivered count is the player-facing count ---------------------------

def test_questions_asked_count_from_history():
    assert _game_with_asked(5).questions_asked_count() == 5


def test_burned_card_not_counted():
    # The armed-then-burned q6 was removed from asked_history at release, so
    # the mirror holds 5 even though question_number reached 6.
    game = _game_with_asked(6)
    # Simulate the release that a STOP-burn performs on the armed card.
    game.asked_history.pop()
    assert game.questions_asked_count() == 5


def test_empty_history_is_zero():
    game = LilyGame.bare()
    game.asked_history = []
    assert game.questions_asked_count() == 0


# -- the fixture winner write reads "over 5 questions" --------------------

def test_fixture_winner_summary_over_five():
    standings = [{"name": "Rami", "score": 4}]
    winner = lily_session_winner(standings)
    assert winner == "Rami"
    qc = _game_with_asked(5).questions_asked_count()
    summary = lily_build_session_summary(standings, winner, qc)
    assert "over 5 question(s)" in summary
    assert "over 6 question(s)" not in summary
    assert "Rami won with 4 point(s)" in summary
