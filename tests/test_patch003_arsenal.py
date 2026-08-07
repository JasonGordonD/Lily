"""WO-LILY-PATCH-003 binding additions A/B/C — the standing picture arsenal.

The arsenal is pre-generated picture pairs served with ZERO generation on
the delivery path (binding A), watermark-replenished in the background
(binding B), across three heat partitions where a mid-session heat flip
draws from the newly selected partition (binding C).

Tests run against an in-memory fake of the supabase query builder that
mirrors the live schema's shape: lily_picture_arsenal (ready rows) and
lily_picture_arsenal_usage with UNIQUE(arsenal_id, group_id) enforced.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_arsenal
import lily_bank


# -- in-memory fake supabase ---------------------------------------------------


class _Result:
    def __init__(self, data, count=None):
        self.data = data
        self.count = count


class _Query:
    def __init__(self, store, table):
        self._store = store
        self._table = table
        self._filters = []
        self._count = False
        self._limit = None
        self._insert = None

    def select(self, _cols, count=None):
        self._count = count == "exact"
        return self

    def eq(self, col, val):
        self._filters.append((col, val))
        return self

    def limit(self, n):
        self._limit = n
        return self

    def insert(self, row):
        self._insert = row
        return self

    def _matches(self, row):
        return all(row.get(c) == v for c, v in self._filters)

    def execute(self):
        rows = self._store.setdefault(self._table, [])
        if self._insert is not None:
            # UNIQUE(arsenal_id, group_id) on the usage table.
            if self._table == lily_arsenal.USAGE_TABLE:
                for r in rows:
                    if (
                        r.get("arsenal_id") == self._insert.get("arsenal_id")
                        and r.get("group_id") == self._insert.get("group_id")
                    ):
                        raise RuntimeError("duplicate key: (arsenal_id, group_id)")
            new = dict(self._insert)
            new.setdefault("id", f"row_{len(rows)}")
            rows.append(new)
            return _Result([new])
        matched = [r for r in rows if self._matches(r)]
        if self._limit is not None:
            matched = matched[: self._limit]
        if self._count:
            return _Result(matched, count=len(matched))
        return _Result(matched)


class _FakeSupabase:
    def __init__(self):
        self.store = {lily_arsenal.ARSENAL_TABLE: [], lily_arsenal.USAGE_TABLE: []}

    def table(self, name):
        return _Query(self.store, name)

    def seed_ready(self, partition, n, *, prefix="q"):
        rows = self.store[lily_arsenal.ARSENAL_TABLE]
        for i in range(n):
            text = f"{prefix} {partition} {i}"
            rows.append({
                "id": f"{partition}_{i}",
                "partition": partition,
                "status": "ready",
                "question_text": text,
                "question_text_hash": lily_bank.lily_question_text_hash(text),
                "canonical_answer": f"ans_{partition}_{i}",
                "acceptable_answers": [f"ans_{partition}_{i}"],
                "image_storage_path": f"lily-arsenal/{partition}_{i}.png",
                "intensity": partition.replace("adult_", ""),
                "created_at": f"2026-01-01T00:00:{i:02d}Z",
            })


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(coro)
        # Drain any fire-and-forget background tasks (e.g. the watermark
        # replenish kick) so the loop closes cleanly instead of orphaning
        # a pending task.
        pending = asyncio.all_tasks(loop)
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        return result
    finally:
        loop.close()


# -- binding C: partition selection --------------------------------------------


def test_partitions_general_deck():
    assert lily_arsenal.lily_partitions_for("general", None) == ["general"]


def test_partitions_adult_suggestive():
    assert lily_arsenal.lily_partitions_for("adult", "suggestive") == ["adult_suggestive"]


def test_partitions_adult_explicit():
    assert lily_arsenal.lily_partitions_for("adult", "explicit") == ["adult_explicit"]


def test_partitions_adult_mix_draws_both():
    assert lily_arsenal.lily_partitions_for("adult", "mix") == [
        "adult_suggestive",
        "adult_explicit",
    ]


def test_partitions_default_is_suggestive():
    assert lily_arsenal.lily_partitions_for("adult", None) == ["adult_suggestive"]


# -- binding A: zero-generation draw -------------------------------------------


def test_draw_serves_a_ready_pair_and_records_usage():
    sb = _FakeSupabase()
    sb.seed_ready("general", 3)
    q = _run(lily_arsenal.lily_arsenal_draw(
        sb, partition="general", group_id="g1", session_id="s1"
    ))
    assert q is not None
    assert q["image_source"] == "arsenal"
    assert q["image_url"].startswith("lily-arsenal/")
    assert q["id"].startswith("arsenal_")
    # A usage row was written (group no-repeat ledger).
    assert len(sb.store[lily_arsenal.USAGE_TABLE]) == 1


def test_draw_never_repeats_within_a_group():
    sb = _FakeSupabase()
    sb.seed_ready("general", 2)
    seen = set()
    for _ in range(2):
        q = _run(lily_arsenal.lily_arsenal_draw(
            sb, partition="general", group_id="g1", session_id="s1"
        ))
        assert q is not None
        seen.add(q["id"])
    # Both draws were distinct rows.
    assert len(seen) == 2
    # A third draw exhausts the group's unseen pool -> None (falls to next rung).
    assert _run(lily_arsenal.lily_arsenal_draw(
        sb, partition="general", group_id="g1", session_id="s1"
    )) is None


def test_draw_excludes_played_answers():
    sb = _FakeSupabase()
    sb.seed_ready("general", 2)
    # Exclude the answer of the oldest (deterministically-picked) row.
    q = _run(lily_arsenal.lily_arsenal_draw(
        sb, partition="general", group_id="g1", session_id="s1",
        exclude_answers={"ans_general_0"},
    ))
    assert q is not None
    assert q["canonical_answer"] != "ans_general_0"


def test_draw_returns_none_on_empty_pool():
    sb = _FakeSupabase()
    assert _run(lily_arsenal.lily_arsenal_draw(
        sb, partition="general", group_id="g1", session_id="s1"
    )) is None


def test_draw_none_without_supabase_or_group():
    sb = _FakeSupabase()
    sb.seed_ready("general", 1)
    assert _run(lily_arsenal.lily_arsenal_draw(
        None, partition="general", group_id="g1", session_id="s1"
    )) is None
    assert _run(lily_arsenal.lily_arsenal_draw(
        sb, partition="general", group_id="", session_id="s1"
    )) is None


# -- binding B: watermark + replenishment --------------------------------------


def test_should_replenish_fires_at_fourth_served_below_target():
    # 4 served, only 6 remain ready (< 10) -> fire.
    assert lily_arsenal.lily_should_replenish(4, 6) is True


def test_should_replenish_holds_before_fourth():
    assert lily_arsenal.lily_should_replenish(3, 6) is False


def test_should_replenish_holds_when_pool_full():
    assert lily_arsenal.lily_should_replenish(9, 10) is False


def test_served_and_ready_counts():
    sb = _FakeSupabase()
    sb.seed_ready("adult_explicit", 5)
    assert _run(lily_arsenal.lily_arsenal_ready_count(
        sb, partition="adult_explicit"
    )) == 5
    # Serve two to a group in this session -> served_count == 2.
    for _ in range(2):
        _run(lily_arsenal.lily_arsenal_draw(
            sb, partition="adult_explicit", group_id="g1", session_id="s9"
        ))
    assert _run(lily_arsenal.lily_arsenal_served_count(
        sb, session_id="s9", partition="adult_explicit"
    )) == 2


def test_insert_banks_a_pair_and_dedups_by_hash():
    sb = _FakeSupabase()
    q = {
        "prompt": "What is banked here?",
        "canonical_answer": "thing",
        "acceptable_answers": ["thing"],
        "image_storage_path": "lily-arsenal/new.png",
    }
    assert _run(lily_arsenal.lily_arsenal_insert(
        sb, partition="general", question=q
    )) is True
    # Same text hash -> idempotent skip (never a duplicate standing row).
    assert _run(lily_arsenal.lily_arsenal_insert(
        sb, partition="general", question=q
    )) is False
    assert _run(lily_arsenal.lily_arsenal_ready_count(
        sb, partition="general"
    )) == 1


def test_insert_skips_pictureless_generation():
    sb = _FakeSupabase()
    q = {"prompt": "no image here", "canonical_answer": "x"}
    assert _run(lily_arsenal.lily_arsenal_insert(
        sb, partition="general", question=q
    )) is False


def test_replenish_fills_toward_target():
    sb = _FakeSupabase()
    sb.seed_ready("general", 6)  # 6 ready, target 10 -> shortfall 4
    made = {"n": 0}
    # WO-LILY-ARSENAL-SEED-001 A5: the insert now runs a SIMILARITY check,
    # not just an exact-hash one, so the old fixture's questions ("fresh
    # general 1", "fresh general 2", ...) are correctly rejected as
    # near-duplicates of each other — they differ by one character. A bank
    # of ten cannot afford two questions that rhyme. Distinct subjects here
    # so this test measures what it means to measure: the replenisher fills
    # the shortfall and stops at target.
    subjects = [
        ("what breed of dog is shown here", "a beagle"),
        ("which planet is this", "saturn"),
        ("name the instrument in this picture", "a cello"),
        ("what sport is being played", "cricket"),
        ("which fruit is this", "a pomegranate"),
    ]

    async def gen_one(partition):
        prompt, answer = subjects[made["n"] % len(subjects)]
        made["n"] += 1
        return {
            "prompt": prompt,
            "canonical_answer": answer,
            "image_storage_path": f"lily-arsenal/fresh_{made['n']}.png",
        }

    banked = _run(lily_arsenal.lily_arsenal_replenish(
        sb, partition="general", generate_one=gen_one
    ))
    assert banked == 4
    assert _run(lily_arsenal.lily_arsenal_ready_count(
        sb, partition="general"
    )) == 10


def test_replenish_stops_when_generation_down():
    sb = _FakeSupabase()
    sb.seed_ready("general", 6)

    async def gen_down(partition):
        return None  # generation unavailable

    banked = _run(lily_arsenal.lily_arsenal_replenish(
        sb, partition="general", generate_one=gen_down
    ))
    assert banked == 0
    # Pool untouched; delivery path degrades to the truthful pictureless line.
    assert _run(lily_arsenal.lily_arsenal_ready_count(
        sb, partition="general"
    )) == 6


def test_replenish_noop_when_pool_already_full():
    sb = _FakeSupabase()
    sb.seed_ready("general", 10)

    async def gen_one(partition):
        raise AssertionError("must not generate when the pool is already full")

    banked = _run(lily_arsenal.lily_arsenal_replenish(
        sb, partition="general", generate_one=gen_one
    ))
    assert banked == 0


# -- agent wiring: heat-flip repartitions the draw (binding C) ------------------


def test_agent_draw_repartitions_on_heat_flip(monkeypatch):
    """The heat is read at draw time, so a mid-session flip changes the
    partition the very next draw pulls from — suggestive -> explicit."""
    from lily_agent import LilyGame
    from lily_scorekeeper import LilyScorekeeper

    game = LilyGame.__new__(LilyGame)
    game.sk = LilyScorekeeper("arsw")
    game.sk.mode = "adult"
    game.sk.adult_image_intensity = "suggestive"
    game.supabase = object()
    game.group_id = "g1"
    game.asked_history = []

    asked = []

    async def fake_draw(supabase, *, partition, group_id, session_id, exclude_answers=None):
        asked.append(partition)
        return None  # force the ladder to try every mapped partition

    monkeypatch.setattr(lily_arsenal, "lily_arsenal_draw", fake_draw)

    _run(game._arsenal_picture_draw("adult"))
    assert asked == ["adult_suggestive"]

    asked.clear()
    game.sk.adult_image_intensity = "explicit"
    _run(game._arsenal_picture_draw("adult"))
    assert asked == ["adult_explicit"]

    asked.clear()
    game.sk.adult_image_intensity = "mix"
    _run(game._arsenal_picture_draw("adult"))
    assert asked == ["adult_suggestive", "adult_explicit"]


def test_agent_draw_none_without_group_or_pipeline(monkeypatch):
    from lily_agent import LilyGame
    from lily_scorekeeper import LilyScorekeeper

    def _game():
        g = LilyGame.__new__(LilyGame)
        g.sk = LilyScorekeeper("arsw2")
        g.sk.mode = "general"
        g.asked_history = []
        return g

    g1 = _game()
    g1.supabase = None
    g1.group_id = "g1"
    assert _run(g1._arsenal_picture_draw("general")) is None

    g2 = _game()
    g2.supabase = object()
    g2.group_id = None
    assert _run(g2._arsenal_picture_draw("general")) is None


def test_agent_arsenal_rung_serves_with_no_generator_present():
    """Binding A, enforced: the arsenal rung has NO generation dependency.
    The game has no `.reasoning` at all and the draw still serves from the
    seeded pool — proof the delivery path never reaches generation."""
    from lily_agent import LilyGame
    from lily_scorekeeper import LilyScorekeeper

    sb = _FakeSupabase()
    sb.seed_ready("general", 3)
    game = LilyGame.__new__(LilyGame)
    game.sk = LilyScorekeeper("arsw3")
    game.sk.mode = "general"
    game.supabase = sb
    game.group_id = "g1"
    game.asked_history = []
    # Deliberately NO game.reasoning attribute.
    q = _run(game._arsenal_picture_draw("general"))
    assert q is not None
    assert q["image_source"] == "arsenal"
