"""WS-7 (WO-LILY-OMNIBUS-003) — score integrity: single write path,
complete audit.

Live evidence (session lily-81BCB0-583a0f16): a spoken false score
("Rhonda and Rami tied with 2") persisted into state; the Walter White
make-good and a bonus point had no lily_answers rows; a point sat on a
player_name: null row; per_player, lily_answers, and final_standings
disagreed pairwise.

The fix under test:
  - apply_score_event is the SOLE score writer; record_result and
    award_bonus delegate to it; every mutation lands a ledger entry
    with a cause code.
  - Standings are derived from the ledger (ledger_scores), not the
    parallel per-player counters.
  - Wrap-up reconciliation (reconcile_scores) compares counters to
    ledger sums and hard-logs any mismatch.
  - Narration (agent turns, player claims) can never seed or mutate a
    score.

Pure scorekeeper + persistence tests — no livekit required.
"""

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_persistence
from lily_scorekeeper import LilyScorekeeper


def make_sk(**kwargs):
    sk = LilyScorekeeper(session_id="test-room", **kwargs)
    sk.bind_speaker("S1", "Sarah")
    sk.bind_speaker("S2", "Dave")
    sk.bind_speaker("S3", "Priya")
    return sk


# ---------------------------------------------------------------------------
# Single write path + ledger
# ---------------------------------------------------------------------------

def test_apply_score_event_is_the_choke_point():
    sk = make_sk()
    entry = sk.apply_score_event(
        "Sarah", cause="answer", correct=True, points=2,
        question_id="q-1", transcript="tungsten",
    )
    assert entry is not None
    assert entry["player"] == "Sarah"
    assert entry["cause"] == "answer"
    assert entry["points"] == 2
    assert entry["score_after"] == 2
    assert sk.players["Sarah"]["score"] == 2
    assert sk.players["Sarah"]["streak"] == 1
    assert sk.score_ledger[-1] is entry


def test_record_result_routes_through_ledger():
    sk = make_sk()
    sk.record_result("Sarah", correct=True, points=2)
    sk.record_result("Dave", correct=False, points=0)
    causes = [e["cause"] for e in sk.score_ledger]
    assert causes == ["answer", "answer"]
    # Incorrect commits carry zero points but still audit (streak reset).
    assert sk.score_ledger[1]["points"] == 0
    assert sk.score_ledger[1]["correct"] is False
    assert sk.players["Sarah"]["score"] == 2
    assert sk.players["Dave"]["score"] == 0


def test_award_bonus_lands_on_ledger_with_cause():
    sk = make_sk()
    entry = sk.award_bonus("Priya")
    assert entry is not None
    assert entry["cause"] == "bonus"
    assert entry["points"] == 1
    # Bonus never touches the streak.
    assert sk.players["Priya"]["streak"] == 0
    assert sk.players["Priya"]["score"] == 1


def test_make_good_cause_behaves_like_a_correct_answer():
    sk = make_sk()
    entry = sk.apply_score_event(
        "Dave", cause="make_good", correct=True, points=3,
        transcript="the pacific",
    )
    assert entry["cause"] == "make_good"
    assert sk.players["Dave"]["score"] == 3
    assert sk.players["Dave"]["streak"] == 1
    assert sk.players["Dave"]["answers_correct"] == 1


def test_unknown_player_never_mutates_or_audits():
    sk = make_sk()
    entry = sk.apply_score_event("Nobody", cause="bonus", points=1)
    assert entry is None
    assert sk.score_ledger == []


def test_every_point_traces_to_a_cause():
    sk = make_sk()
    sk.record_result("Sarah", correct=True, points=2)
    sk.record_result("Dave", correct=False)
    sk.award_bonus("Dave")
    sk.apply_score_event("Priya", cause="make_good", correct=True, points=1)
    for entry in sk.score_ledger:
        assert entry["cause"]
        assert "ts" in entry
    sums = sk.ledger_scores()
    assert sums == {"Sarah": 2, "Dave": 1, "Priya": 1}


# ---------------------------------------------------------------------------
# Standings derive from the ledger
# ---------------------------------------------------------------------------

def test_ledger_scores_covers_every_rostered_player():
    sk = make_sk()
    sk.record_result("Sarah", correct=True, points=2)
    sums = sk.ledger_scores()
    assert sums == {"Sarah": 2, "Dave": 0, "Priya": 0}


def test_narration_cannot_alter_state():
    """Injected narration containing a wrong score never mutates state:
    agent turns and player claims are read surfaces, not write paths."""
    sk = make_sk()
    sk.record_result("Sarah", correct=True, points=1)
    before_scores = {n: s["score"] for n, s in sk.players.items()}
    before_ledger = len(sk.score_ledger)

    # Lily "speaks" a false score — the live failure class.
    sk.record_agent_turn("Rhonda and Rami are tied with 2 points each!")
    # A player claims a false score in a final transcript segment.
    sk.on_transcript_segment(
        "my score should actually be five points",
        speaker_label="S2", is_final=True,
    )

    assert {n: s["score"] for n, s in sk.players.items()} == before_scores
    assert len(sk.score_ledger) == before_ledger
    assert sk.ledger_scores()["Sarah"] == 1


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

def test_reconcile_clean_game_reports_no_mismatch():
    sk = make_sk()
    sk.record_result("Sarah", correct=True, points=2)
    sk.award_bonus("Dave")
    assert sk.reconcile_scores() == []


def test_reconcile_hard_logs_counter_tamper(caplog):
    sk = make_sk()
    sk.record_result("Sarah", correct=True, points=2)
    # A rogue writer bypasses the choke point.
    sk.players["Sarah"]["score"] += 1
    with caplog.at_level(logging.ERROR, logger="lily_scorekeeper"):
        mismatches = sk.reconcile_scores()
    assert mismatches == [{"player": "Sarah", "counter": 3, "ledger": 2}]
    assert any("RECONCILE_MISMATCH" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Snapshot / rehydrate keep the invariant
# ---------------------------------------------------------------------------

def test_snapshot_roundtrips_the_ledger():
    sk = make_sk()
    sk.record_result("Sarah", correct=True, points=2)
    sk.award_bonus("Dave")
    snap = sk.snapshot()
    sk2 = LilyScorekeeper(session_id="test-room")
    sk2.rehydrate(snap)
    assert sk2.ledger_scores() == {"Sarah": 2, "Dave": 1, "Priya": 0}
    assert sk2.reconcile_scores() == []


def test_legacy_snapshot_without_ledger_seeds_rehydrate_entries():
    sk = make_sk()
    sk.record_result("Dave", correct=True, points=3)
    snap = sk.snapshot()
    snap.pop("score_ledger", None)  # pre-WS-7 checkpoint shape
    sk2 = LilyScorekeeper(session_id="test-room")
    sk2.rehydrate(snap)
    assert sk2.players["Dave"]["score"] == 3
    assert sk2.ledger_scores()["Dave"] == 3
    assert sk2.reconcile_scores() == []
    assert any(e["cause"] == "rehydrate" for e in sk2.score_ledger)


# ---------------------------------------------------------------------------
# Full sim game (exit bar): standings == ledger sums exactly; every point
# on the final board traces to a row with a cause.
# ---------------------------------------------------------------------------

def test_full_sim_game_standings_equal_ledger_sums():
    sk = make_sk()
    now = 1000.0
    script = [
        # (winner, losers, points)
        ("Sarah", ["Dave"], 1),
        ("Dave", ["Sarah", "Priya"], 1),
        ("Priya", [], 2),
        (None, ["Sarah", "Dave"], 2),   # missed question
        ("Sarah", ["Priya"], 3),
    ]
    for winner, losers, points in script:
        sk.start_question({"prompt": "q", "canonical_answer": "a"})
        sk.open_answer_window(now=now)
        if winner:
            sk.on_transcript_segment(
                "an answer", speaker_label=sk.players[winner]["speaker_label"],
                is_final=True, now=now + 1,
            )
            sk.record_result(winner, correct=True, points=points)
        for loser in losers:
            sk.record_result(loser, correct=False)
        sk.close_answer_window()
        now += 30.0
    sk.award_bonus("Dave")
    sk.apply_score_event("Priya", cause="make_good", correct=True, points=1)

    sums = sk.ledger_scores()
    assert sums == {"Sarah": 4, "Dave": 2, "Priya": 3}
    # Standings derive from the ledger, and the parallel counters agree.
    assert {n: s["score"] for n, s in sk.players.items()} == sums
    assert sk.reconcile_scores() == []
    # Every point on the board traces to ledger rows with a cause.
    for name, total in sums.items():
        contributed = sum(
            e["points"] for e in sk.score_ledger if e["player"] == name
        )
        assert contributed == total
        assert all(e["cause"] for e in sk.score_ledger if e["player"] == name)


# ---------------------------------------------------------------------------
# Persistence: cause column with clean non-DDL fallback
# ---------------------------------------------------------------------------

class _Result:
    data = [{"id": 1}]


class _Query:
    def __init__(self, table):
        self._table = table

    def insert(self, row):
        self._row = row
        return self

    def execute(self):
        self._table.attempts.append(dict(self._row))
        if self._table.reject_cause and "cause" in self._row:
            raise RuntimeError(
                "PGRST204: column lily_answers.cause does not exist"
            )
        self._table.rows.append(dict(self._row))
        return _Result()


class _FakeTable:
    def __init__(self, reject_cause):
        self.reject_cause = reject_cause
        self.rows = []
        self.attempts = []


class _FakeSupabase:
    def __init__(self, reject_cause=False):
        self.answers = _FakeTable(reject_cause)

    def table(self, name):
        assert name == "lily_answers"
        return _Query(self.answers)


def test_write_answer_carries_cause_column():
    supabase = _FakeSupabase()
    asyncio.run(lily_persistence.lily_write_answer(
        supabase, "s-1", "Sarah", "q-1", 3, "tungsten", "correct", 1, 2,
        cause="answer",
    ))
    assert len(supabase.answers.rows) == 1
    row = supabase.answers.rows[0]
    assert row["cause"] == "answer"
    assert row["awarded_points"] == 2


def test_write_answer_falls_back_when_cause_column_missing():
    """Until the Doc DDL lands, the insert retries without the cause key
    — the row is never lost, and the cause survives in verdict for
    non-adjudication events."""
    supabase = _FakeSupabase(reject_cause=True)
    asyncio.run(lily_persistence.lily_write_answer(
        supabase, "s-1", "Dave", None, 4, "great wrong answer", "bonus", 0, 1,
        cause="bonus",
    ))
    assert len(supabase.answers.attempts) == 2
    assert "cause" in supabase.answers.attempts[0]
    assert "cause" not in supabase.answers.attempts[1]
    assert len(supabase.answers.rows) == 1
    assert supabase.answers.rows[0]["verdict"] == "bonus"


def test_write_score_event_builds_row_from_ledger_entry():
    sk = make_sk()
    entry = sk.award_bonus("Priya")
    supabase = _FakeSupabase()
    asyncio.run(lily_persistence.lily_write_score_event(
        supabase, sk.session_id, entry,
    ))
    assert len(supabase.answers.rows) == 1
    row = supabase.answers.rows[0]
    assert row["player_name"] == "Priya"
    assert row["verdict"] == "bonus"
    assert row["cause"] == "bonus"
    assert row["awarded_points"] == 1
    assert row["eval_tier"] == 0


def test_write_score_event_answer_cause_keeps_verdict_semantics():
    sk = make_sk()
    entry = sk.apply_score_event(
        "Sarah", cause="answer", correct=True, points=2,
        question_id="q-9", transcript="the femur", eval_tier=2,
    )
    supabase = _FakeSupabase()
    asyncio.run(lily_persistence.lily_write_score_event(
        supabase, sk.session_id, entry,
    ))
    row = supabase.answers.rows[0]
    assert row["verdict"] == "correct"
    assert row["cause"] == "answer"
    assert row["eval_tier"] == 2
    assert row["question_id"] == "q-9"
