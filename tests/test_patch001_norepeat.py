"""WO-LILY-PATCH-001 T7 + T9 — cross-session no-repeat and the
abandoned-session sweeper.

T7 fixture: kb_469 (Mars) served as a FRESH question to a group that
played a differently-worded Mars question the previous day — same
answer, distinct id and text hash, so the id/hash exclusion missed it.
The generation path already avoided played answers; the bank path did
not. (PATCH-002 A1 extends this to group-scoped burn.)

T9 fixture: session 89A97A died mid-round (phase stuck 'round', q_3812
registered at the instant of death) — a crash-instant ghost. A session
inactive past threshold in a non-ended phase is force-closed.
"""

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_persistence


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# -- T7: bank draw excludes played answers -------------------------------------


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def select(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def execute(self):
        return type("R", (), {"data": self._rows})()


class _FakeSupabase:
    def __init__(self, rows):
        self._rows = rows

    def table(self, name):
        return _FakeQuery(self._rows)


MARS_ROW = {
    "id": 469, "question": "Which planet is known as the Red Planet?",
    "canonical_answer": "Mars", "acceptable_answers": ["mars"],
    "category": "science", "difficulty_tier": 1, "status": "active",
    "adult": False,
}
SATURN_ROW = {
    "id": 470, "question": "Which planet has the most moons?",
    "canonical_answer": "Saturn", "acceptable_answers": ["saturn"],
    "category": "science", "difficulty_tier": 1, "status": "active",
    "adult": False,
}


def test_bank_draw_excludes_a_played_answer_across_wordings():
    """The group played Mars yesterday (different wording). A fresh
    Mars-answer bank row is excluded even though its id/hash are new."""
    supa = _FakeSupabase([MARS_ROW, SATURN_ROW])
    out = _run(lily_persistence.lily_fetch_bank_question(
        supa, "science", 1, exclude_prompts=[],
        exclude_answers={"Mars"},
    ))
    assert out is not None
    assert out["canonical_answer"] == "Saturn"  # Mars filtered by answer


def test_bank_draw_without_answer_exclusion_is_unchanged():
    supa = _FakeSupabase([MARS_ROW])
    out = _run(lily_persistence.lily_fetch_bank_question(
        supa, "science", 1, exclude_prompts=[],
    ))
    assert out is not None and out["canonical_answer"] == "Mars"


# -- T9: abandoned-session sweeper ---------------------------------------------


class _SweepQuery:
    def __init__(self, store, rows):
        self._store = store
        self._rows = rows
        self._update = None

    def select(self, *a, **k):
        return self

    def update(self, payload):
        self._update = payload
        return self

    def neq(self, *a, **k):
        return self

    def lt(self, *a, **k):
        return self

    def eq(self, col, val):
        self._store["updated_id"] = val
        return self

    def order(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def execute(self):
        if self._update is not None:
            self._store["closed"] = self._store.get("closed", 0) + 1
            return type("R", (), {"data": [{}]})()
        return type("R", (), {"data": self._rows})()


class _SweepSupabase:
    def __init__(self, rows):
        self.store = {}
        self._rows = rows

    def table(self, name):
        return _SweepQuery(self.store, self._rows)


def test_abandoned_session_is_force_closed(caplog):
    supa = _SweepSupabase([
        {"session_id": "lily-89A97A", "phase": "round",
         "updated_at": "2026-08-06T20:00:00+00:00"},
    ])
    with caplog.at_level(logging.WARNING):
        stats = _run(lily_persistence.lily_sweep_abandoned_sessions(supa))
    assert stats == {"scanned": 1, "closed": 1}
    assert any("ABANDONED_SESSION_CLOSED" in r.message for r in caplog.records)


def test_sweeper_noop_when_nothing_abandoned():
    supa = _SweepSupabase([])
    stats = _run(lily_persistence.lily_sweep_abandoned_sessions(supa))
    assert stats == {"scanned": 0, "closed": 0}


def test_sweeper_null_supabase_is_safe():
    assert _run(lily_persistence.lily_sweep_abandoned_sessions(None)) == {
        "scanned": 0, "closed": 0
    }
