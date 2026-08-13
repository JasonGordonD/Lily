"""WO-LILY-HOTFIX-010 V5 — the name ask is a ONE-SHOT, not a never-satisfied
blocking gate.

Live 2026-08-10: "What should I call you?" was asked SEVEN times in 3.5
minutes — appended to the END of every turn, AFTER the player had already
given a name in his first utterance AND after she had echoed it. Both gate
sites (start_blocked_reason + the state-block intake injection) keyed only
on roster_size()<1 with no satisfaction path, and the name never bound to
the roster, so the ask re-fired forever and Round One never started.

These tests pin the fix: hosting requires no name first. A present but
unnamed voice plays and SCORES under a speaker-label placeholder; the ONE
ask is folded into the opening beat and never repeats; a name binds
opportunistically and migrates the placeholder's history. Each of these
tests references V5-only surface (identity_intake_line,
ensure_present_placeholder, roster_size(include_placeholder=...)), so the
file FAILS on pre-V5 code where the ask re-fires.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lily_agent import LilyGame
from lily_binding import LilyFragmentAccumulator
from lily_scorekeeper import LilyScorekeeper


def _game() -> LilyGame:
    game = LilyGame.bare()
    game.sk = LilyScorekeeper("name-refusal-v5")
    game.fragments = LilyFragmentAccumulator()
    game._confirmed_name_evidence = {}
    game._identity_required_before_start = True
    game._identity_ask_spent = False
    game._delivery_stop_sticky = False
    game._recognition_dispute = False
    game._recognition_dispute_why_answered = False
    game._ambiguous_yes_blocks_start = False
    game._setup_pending = set()
    game._user_speaking = False
    game._first_human_utterance_seen = False
    game.game_started = False
    game.game_over = False
    game._last_bind_at = None
    game.on_speaker_bound = lambda label, name: ""
    return game


def _first_human_utterance(game: LilyGame, label: str = "S1") -> None:
    """Mirror the V5 block in on_user_turn_completed: a present voice takes
    the floor, the one ask is spent, and — with no name bound — the
    speaker-label placeholder stands up so hosting proceeds."""
    game.sk.unrostered_labels[label] = game.sk.unrostered_labels.get(label, 0) + 1
    game._first_human_utterance_seen = True
    if game._identity_required_before_start:
        game._identity_ask_spent = True
        if game.sk.roster_size(include_placeholder=False) < 1:
            game.sk.ensure_present_placeholder(game.sk.present_placeholder_label())


def test_spent_ask_unblocks_start():
    """Pure-behavioral re-fire check on the shared start_blocked_reason
    surface: once the session's one ask has been spent, start proceeds. On
    the pre-V5 never-satisfied gate this stays 'identity_unconfirmed' — the
    block that outlived every ask."""
    game = _game()
    game._identity_ask_spent = True
    assert game.start_blocked_reason() is None


def test_cold_open_offers_the_one_ask_and_holds_start():
    game = _game()
    # Nobody has spoken: the ONE ask is available and start is held.
    assert game.identity_intake_line() is not None
    assert game.start_blocked_reason() == "identity_unconfirmed"


def test_seven_ask_sequence_is_unreproducible():
    game = _game()
    _first_human_utterance(game)  # no name given

    # The ask is spent; drive many subsequent turns and it must never
    # re-offer itself — the seven-ask sequence cannot recur.
    for _ in range(8):
        assert game.identity_intake_line() is None
    assert game._identity_gate_satisfied() is True


def test_no_name_given_start_proceeds():
    game = _game()
    _first_human_utterance(game)  # refuses to name himself
    assert game.start_blocked_reason() is None


def test_full_round_scored_under_placeholder_with_no_name():
    game = _game()
    _first_human_utterance(game)  # no name
    placeholder = game.sk.real_player_names()
    assert placeholder == []  # no real name recited
    assert game.sk.roster_size() == 1  # a present voice is counted
    assert game.sk.has_active_placeholder() is True

    key = game.sk.present_placeholder_label()
    # A full round: two questions adjudicated to the placeholder.
    game.sk.start_question({"id": "q1", "canonical_answer": "Mars"})
    game.sk.apply_score_event(
        key, cause="answer", correct=True, points=1,
        question_id="q1", transcript="Mars",
    )
    game.sk.start_question({"id": "q2", "canonical_answer": "Venus"})
    game.sk.apply_score_event(
        key, cause="answer", correct=True, points=1,
        question_id="q2", transcript="Venus",
    )

    assert game.sk.ledger_scores().get(key) == 2  # the placeholder scored
    assert game.sk.reconcile_scores() == []  # counter/ledger agree

    # And the raw speaker label never reaches a spoken authority surface.
    roster_line = game._roster_authority_line()
    score_line = game._score_authority_line()
    assert roster_line is not None and "1 player" in roster_line
    assert key not in roster_line
    assert score_line is None or key not in score_line


def test_name_given_once_is_never_re_requested():
    game = _game()
    game.sk.bind_speaker("S1", "Rami")
    for _ in range(8):
        assert game.identity_intake_line() is None
    assert game.start_blocked_reason() is None


def test_late_name_migrates_placeholder_history():
    game = _game()
    _first_human_utterance(game)  # anonymous start
    key = game.sk.present_placeholder_label()
    game.sk.apply_score_event(
        key, cause="answer", correct=True, points=3,
        question_id="q1", transcript="Jupiter",
    )
    assert game.sk.ledger_scores().get(key) == 3

    # He finally names himself; the same label binds Rami.
    game.sk.bind_speaker(key, "Rami")

    assert "Rami" in game.sk.players
    assert game.sk.has_active_placeholder() is False
    assert game.sk.real_player_names() == ["Rami"]
    assert game.sk.ledger_scores().get("Rami") == 3  # history travelled
    assert game.start_blocked_reason() is None


# --- HOTFIX-010 V5 fix-loop: the raw diarizer label placeholder must never
#     surface as an identity on ANY name surface. The two spoken authority
#     lines already recite real_player_names() only; _players_payload() (the
#     single choke point behind the frontend scoreboard, the finale/comeback
#     events, and Supabase persistence) was NOT filtered and emitted the raw
#     label as name=. These pin the symmetry: the raw label is aired/persisted
#     nowhere; a neutral non-identity marker appears instead; scoring is intact.
#     FAILS on pre-fix 7ee9b4c (name == raw label "S1"/"UU").


def _solo_unnamed_scored(points: int = 2) -> tuple[LilyGame, str]:
    """A solo, never-named session under a speaker-label placeholder that has
    scored — the exact bad-identity-data shape V1/V7 exist to prevent."""
    game = _game()
    _first_human_utterance(game, label="S1")  # no name ever given
    key = game.sk.present_placeholder_label()
    assert key == "S1"  # the raw diarizer label — must not surface anywhere
    game.sk.start_question({"id": "q1", "canonical_answer": "Mars"})
    game.sk.apply_score_event(
        key, cause="answer", correct=True, points=points,
        question_id="q1", transcript="Mars",
    )
    return game, key


def test_players_payload_neutralizes_raw_placeholder_label():
    game, key = _solo_unnamed_scored(points=2)
    payload = game._players_payload()
    assert len(payload) == 1
    row = payload[0]
    # Surface source: the raw label is gone; a neutral marker stands in.
    assert row["name"] != key
    assert row["name"] == "Player"
    # Scoring is intact under the neutral relabel.
    assert row["score"] == 2


def test_frontend_scoreboard_attr_has_no_raw_label():
    """Surface 1 — the published 'players' frontend attribute is exactly
    json.dumps(_players_payload()) (lily_agent.py:1201)."""
    game, key = _solo_unnamed_scored()
    players_attr = json.dumps(game._players_payload())
    assert key not in players_attr
    assert "Player" in players_attr


def test_finale_and_comeback_events_have_no_raw_label():
    """Surfaces 2 — finish_game builds standings = sorted(_players_payload())
    and hands standings[0]['name'] to the biggest_comeback event and the full
    list to the finale event."""
    game, key = _solo_unnamed_scored()
    standings = sorted(game._players_payload(), key=lambda p: -p["score"])
    # finale event {"standings": standings}
    assert all(row["name"] != key for row in standings)
    assert any(row["name"] == "Player" for row in standings)
    # biggest_comeback {"player"/"name": standings[0]["name"]}
    assert standings[0]["name"] != key
    assert standings[0]["name"] == "Player"


def test_cross_session_persistence_never_stores_raw_label():
    """Surface 3 (worst leg) — final_standings feeds lily_checkpoint,
    lily_write_session_memory and build_game_stats. None may key the durable
    player identity to the raw diarizer label."""
    game, key = _solo_unnamed_scored()
    standings = sorted(game._players_payload(), key=lambda p: -p["score"])
    # lily_checkpoint(final_standings=standings) / lily_write_session_memory(
    # standings, ...): the durable player identity is the neutral marker.
    for row in standings:
        assert row["name"] != key
    # build_game_stats(standings) -> game_stats["final_standings"] AND its
    # independent per_player identity map — neither may carry the raw label.
    game.highlights = []
    game.session_started_at = time.time()
    stats = game.build_game_stats(standings)
    assert key not in json.dumps(stats)
    assert "Player" in stats["per_player"]  # per_player keyed by neutral marker
    assert key not in stats["per_player"]


def test_bound_real_name_recites_and_never_neutralized():
    """The relabel is placeholder-only: once a real name binds (migrating the
    placeholder's history), it surfaces normally and is never masked."""
    game, key = _solo_unnamed_scored(points=3)
    game.sk.bind_speaker(key, "Rami")  # rename=False path: name binds
    payload = game._players_payload()
    names = [r["name"] for r in payload]
    assert "Rami" in names
    assert "Player" not in names
    assert key not in names
    assert game.sk.ledger_scores().get("Rami") == 3  # history travelled
