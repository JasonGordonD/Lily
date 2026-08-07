"""WO-LILY-HOTFIX-005 X1 (LEAD) — score integrity.

The spoken score was LLM-computed from context (narrated 13 against a true
9). Fix: the state block injects the authoritative committed-ledger score
as read-only; the narrated-divergence detector flags any spoken score that
matches no ledger total (SCORE_DIVERGENCE at ERROR). Glass already projects
ledger_scores(), so state-block and glass agree.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_scorekeeper
from lily_scorekeeper import lily_narrated_score_divergence, LilyScorekeeper
from lily_agent import LilyGame


# -- narrated-divergence detector ---------------------------------------------

def test_off_ledger_score_is_divergence():
    d = lily_narrated_score_divergence("you're at 13 points", {"Rami": 9})
    assert d is not None
    assert d["spoken"] == 13 and d["ledger_values"] == [9]


def test_on_ledger_score_is_fine():
    assert lily_narrated_score_divergence("you're at 9 points", {"Rami": 9}) is None


def test_matches_any_players_total():
    # multi-player: a stated score matching ANY player's total is fine.
    assert lily_narrated_score_divergence(
        "Sam, you've got 12", {"Rami": 9, "Sam": 12}) is None


def test_no_score_claim_is_none():
    assert lily_narrated_score_divergence("nice one, that's correct", {"Rami": 9}) is None


def test_empty_ledger_never_flags():
    assert lily_narrated_score_divergence("you're at 13", {}) is None


# -- state block carries the authoritative ledger score -----------------------

def test_state_block_injects_authoritative_score():
    game = LilyGame.__new__(LilyGame)
    game.sk = LilyScorekeeper("x1")
    game.sk.players = {"Rami": {"score": 0, "streak": 0}}
    game.sk.score_ledger = [{"player": "Rami", "points": 5},
                            {"player": "Rami", "points": 4}]
    line = game._score_authority_line()
    assert "SCORES" in line and "AUTHORITATIVE" in line
    assert "Rami 9" in line
    assert "NEVER compute" in line
