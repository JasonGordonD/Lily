"""WO-LILY-SUPPLY-001 S1 — bank-first question supply.

Operator ruling: "the bank serves, the author replenishes. Live question
authoring never sits on the delivery path again."

Evidence (2026-09-06): grok-4.5 authoring takes 20-39 s to the FIRST
content token on every call — streaming stopped it dying, not being slow —
while `lily_questions` holds 448 active rows. Before this WO the fixed
family rotation generated-first for every non-operator lane and reached
the bank only as insurance AFTER the author had already failed, so every
question the table heard was paid for at authoring latency and the bank's
role was to cover a crash.

Three archaeology facts this file pins, each of which was a live defect:

  1. `lily_fetch_bank_question` filtered `.eq("adult", True)` for every
     caller ("unified adult deck", WO-PRMPT-LILY-REFACTOR-001), so the
     307 active adult=false rows were structurally unreachable and a
     general table's only bank was the 141-row adult register. The draw
     now takes an explicit DECK.
  2. The rotation's lane names are not the bank's category vocabulary:
     `pop culture` (family) vs `pop_culture` (38 rows), and
     `lifestyle-potpourri` (family) which matches NOTHING — the bank
     stores `lifestyle`. Two of four lanes could only ever be served by
     the any-category fallback stage. The lane->category map is now a
     declared, tested table.
  3. `_bank_to_supply` built its own exclusion union (history | drawn)
     and left the BURNED sets out, so the recovery ladder could draw a
     question whose answer had already gone to air; only the later
     REARM_BLOCKED guard caught it, one wasted slot at a time.

No test here asserts on question source TEXT — the fixtures carry rows,
the assertions are about ids, decks, lanes, exclusion sets and call
counts.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import lily_audeering_consumers
import lily_bank
import lily_persistence
import lily_reasoning
import lily_say_gate
from fake_supabase import FakeSupabase
from fakes import FakeSession
from lily_agent import CATEGORY_FAMILIES, LilyGame
from lily_scorekeeper import LilyScorekeeper

SESSION_ID = "lily-S1-supply001"
GROUP_ID = "grp_supply_001"


# ---------------------------------------------------------------------------
# Fixture bank — shaped like the live table (the columns the draw reads).
# ---------------------------------------------------------------------------

def _row(rid, category, adult, tier=1, status="active", **extra):
    row = {
        "id": rid,
        "mode": "adult" if adult else "general",
        "category": category,
        "question": f"fixture question {rid}",
        "canonical_answer": f"answer {rid}",
        "acceptable_answers": [f"answer {rid}"],
        "difficulty_tier": tier,
        "reveal_color": "",
        "source": "fixture",
        "adult": adult,
        "status": status,
    }
    row.update(extra)
    return row


def _bank_rows():
    """Two decks x four lanes, several tiers — enough that every lane can
    serve a whole game without the any-category fallback."""
    rows = []
    rid = 100
    plan = [
        # (category, adult)
        ("academic", False), ("science", False), ("history", False),
        ("pop_culture", False), ("music", False),
        ("wordplay", False), ("literature", False),
        ("lifestyle", False), ("art", False),
        ("adult_couples", True), ("adult_kink", True),
        ("adult_science", True), ("adult_popculture", True),
        ("adult_wordplay", True), ("drinking", True),
    ]
    for category, adult in plan:
        for tier in (1, 2, 3):
            for _ in range(3):
                rid += 1
                rows.append(_row(rid, category, adult, tier=tier))
    # One burned row per deck: never servable.
    rid += 1
    rows.append(_row(rid, "academic", False, status="burned"))
    rid += 1
    rows.append(_row(rid, "adult_kink", True, status="burned"))
    return rows


def _db_with_bank():
    db = FakeSupabase()
    db.tables["lily_questions"] = _bank_rows()
    db.tables["lily_asked_history"] = []
    return db


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Game harness — a bare LilyGame with the real supply mixin.
# ---------------------------------------------------------------------------

class _ExplodingReasoning:
    """The author, fully disabled. Every entry point raises, so any await
    of an authoring call fails the test loudly instead of silently
    degrading — this is the picture-arsenal enforcement shape
    (tests/test_arsenal_seed_job.py::
    test_a_seeded_bank_serves_instantly_with_zero_generation)."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def prefetch_question(self, *a, **kw):
        self.calls.append("prefetch_question")
        raise AssertionError(
            "the author was awaited on the delivery path"
        )

    async def ensure_choices(self, question):
        self.calls.append("ensure_choices")
        raise AssertionError(
            "MC synthesis was awaited on the delivery path"
        )

    async def prefetch_picture_question(self, *a, **kw):
        self.calls.append("prefetch_picture_question")
        raise AssertionError("picture authoring on the delivery path")


class _CountingReasoning(_ExplodingReasoning):
    """A HEALTHY author: available, fast, and — with a stocked bank —
    never called. Proves the ordering, not just the failure tolerance."""

    async def prefetch_question(self, sk, **kw):
        self.calls.append("prefetch_question")
        return {
            "id": "q_authored_1",
            "prompt": "authored question",
            "canonical_answer": "authored",
            "acceptable_answers": ["authored"],
            "category": kw.get("category") or "academic",
            "difficulty_tier": 1,
            "reveal_color": "",
        }

    async def ensure_choices(self, question):
        self.calls.append("ensure_choices")
        return None


def _make_game(db, reasoning=None, group_id=GROUP_ID, session_id=SESSION_ID,
               deck="general"):
    game = LilyGame.bare()
    # The deck the session may serve — the availability flag the entrypoint
    # sets (adult_deck_gate_mode / acoustic pipeline / architect mode).
    game.availability_flags = {"adult_deck": deck == "adult"}
    game.session = FakeSession()
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game.sk = LilyScorekeeper(session_id)
    game.sk.questions_per_round = 3
    game.game_started = True
    game.game_over = False
    game.armed_question = None
    game.next_question = None
    game._next_question_reserve = None
    game.asked_history = []
    game.used_prompts = []
    game.supabase = db
    game.reasoning = reasoning if reasoning is not None else _ExplodingReasoning()
    game.group_id = group_id
    game.rounds_total = 3
    game.prewager_standings = None
    game.eliminated = []
    game.ui_phase = "question"
    game._phase_hold = None
    game._adjudicating = False
    game._question_transitioning = False
    game._drawn_ids = set()
    game._drawn_hashes = set()
    game._burned_question_ids = set()
    game._burned_question_hashes = set()
    game._judged_keys = set()
    game._spec_judge = {}
    game._nbest_by_key = {}
    game._addressee_rows = {}
    game._pre_window_segments = []
    game._prehook_answer_suppressions = set()
    game._category_override = {}
    game._custom_round_registered = {}
    game._custom_round_refused = []
    game.promoted_categories = []
    game.acoustic = lily_audeering_consumers.LilyAcousticState()
    game.session_started_at = time.time() - 300.0
    game.instructed_replies = []
    game.instructed_reply = lambda text: game.instructed_replies.append(text)
    game._set_ui_phase = lambda phase: None
    game.publish_attributes_nowait = lambda: None
    game.settle_context_nowait = lambda: None
    game.publish_question_to_glass = lambda **kw: None
    game.expect_delivery = lambda *a, **kw: None
    game.said = []
    game.gated_say = lambda key, act, text, source=None, **kw: game.said.append(
        (act, source)
    )
    game.progression_paused_reason = lambda: None
    return game


async def _drain(game, seconds: float = 3.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        await asyncio.sleep(0.005)
        task = getattr(game, "_prefetch_task", None)
        if task is None or task.done():
            return
    raise AssertionError("prefetch task never finished")


async def _supply_once(game):
    """One prefetch cycle, started INSIDE the loop (start_prefetch calls
    asyncio.ensure_future, which needs a running loop)."""
    game.start_prefetch()
    await _drain(game)


async def _play(game, questions: int):
    """Drive `questions` complete supply->arm cycles the way the live game
    does: prefetch, arm, deliver (the durable asked-history write rides
    the playout seam), consume, repeat."""
    served = []
    for _ in range(questions):
        game.start_prefetch()
        await _drain(game)
        assert game.arm_next_question(), "nothing armed"
        served.append(dict(game.armed_question))
        # Delivery reached the room: the durable ledger row is written
        # here, before the next draw reads it.
        await lily_bank.lily_record_asked(
            game.supabase, game.group_id, game.armed_question,
            game.sk.session_id,
        )
        game.armed_question = None
    return served


# ---------------------------------------------------------------------------
# 1. The lane map: the fixed family rotation onto bank categories
# ---------------------------------------------------------------------------

def test_every_fixed_family_maps_onto_at_least_one_bank_category():
    for family in CATEGORY_FAMILIES:
        general = lily_bank.lily_lane_categories(family, deck="general")
        adult = lily_bank.lily_lane_categories(family, deck="adult")
        assert general, f"lane {family!r} has no general bank categories"
        assert adult, f"lane {family!r} has no adult bank categories"
        assert general[0] != family or family in {"academic", "wordplay"}, (
            "a lane whose own name is not a bank category must map to one "
            "that is"
        )


def test_the_two_lanes_the_bank_could_never_serve_now_resolve():
    """`pop culture` (family) vs `pop_culture` (38 live rows), and
    `lifestyle-potpourri` (family) vs `lifestyle` (40 live rows). Before
    this WO both fell through to the any-category stage."""
    assert "pop_culture" in lily_bank.lily_lane_categories(
        "pop culture", deck="general"
    )
    assert "lifestyle" in lily_bank.lily_lane_categories(
        "lifestyle-potpourri", deck="general"
    )


def test_lane_for_category_is_the_declared_inverse():
    for family in CATEGORY_FAMILIES:
        for deck in ("general", "adult"):
            for category in lily_bank.lily_lane_categories(family, deck=deck):
                assert lily_bank.lily_lane_for_category(category) == family


def test_an_unknown_category_lands_in_the_potpourri_lane():
    assert lily_bank.lily_lane_for_category("Cape Cod") == (
        "lifestyle-potpourri"
    )


# ---------------------------------------------------------------------------
# 2. The draw: deck, lane, register, exclusions, pool_remaining
# ---------------------------------------------------------------------------

def test_the_general_deck_never_serves_an_adult_row():
    db = _db_with_bank()
    for _ in range(25):
        q = _run(lily_persistence.lily_fetch_bank_question(
            db, "academic", 1, [], deck="general",
            lane_categories=lily_bank.lily_lane_categories(
                "academic", deck="general"
            ),
        ))
        assert q is not None
        row = next(r for r in db.tables["lily_questions"]
                   if f"kb_{r['id']}" == q["id"])
        assert row["adult"] is False


def test_the_adult_deck_never_serves_a_general_row():
    db = _db_with_bank()
    for _ in range(25):
        q = _run(lily_persistence.lily_fetch_bank_question(
            db, "academic", 1, [], deck="adult",
            lane_categories=lily_bank.lily_lane_categories(
                "academic", deck="adult"
            ),
        ))
        assert q is not None
        row = next(r for r in db.tables["lily_questions"]
                   if f"kb_{r['id']}" == q["id"])
        assert row["adult"] is True


def test_the_draw_stays_inside_its_lane_while_the_lane_has_rows():
    db = _db_with_bank()
    lane = lily_bank.lily_lane_categories("pop culture", deck="general")
    for _ in range(25):
        q = _run(lily_persistence.lily_fetch_bank_question(
            db, "pop culture", 2, [], deck="general", lane_categories=lane,
        ))
        assert q is not None
        assert q["category"] in lane


def test_the_register_relaxes_inside_the_lane_before_the_lane_is_left():
    """Register (difficulty_tier) is the soft axis; the lane is not."""
    db = FakeSupabase()
    db.tables["lily_questions"] = [
        _row(1, "wordplay", False, tier=3),
        _row(2, "academic", False, tier=1),
    ]
    q = _run(lily_persistence.lily_fetch_bank_question(
        db, "wordplay", 1, [], deck="general",
        lane_categories=lily_bank.lily_lane_categories(
            "wordplay", deck="general"
        ),
    ))
    assert q is not None and q["id"] == "kb_1"


def test_a_burned_row_is_never_drawn():
    db = _db_with_bank()
    burned = {f"kb_{r['id']}" for r in db.tables["lily_questions"]
              if r["status"] == "burned"}
    for _ in range(40):
        q = _run(lily_persistence.lily_fetch_bank_question(
            db, "academic", 1, [], deck="general",
            lane_categories=lily_bank.lily_lane_categories(
                "academic", deck="general"
            ),
        ))
        assert q is None or q["id"] not in burned


def test_the_draw_reports_the_pool_it_drew_from():
    db = _db_with_bank()
    stats: dict = {}
    q = _run(lily_persistence.lily_fetch_bank_question(
        db, "academic", 1, [], deck="general",
        lane_categories=lily_bank.lily_lane_categories(
            "academic", deck="general"
        ),
        stats=stats,
    ))
    assert q is not None
    assert stats["pool_remaining"] >= 1
    assert stats["excluded"] == 0
    assert stats["deck"] == "general"
    assert stats["lane_category"] in lily_bank.lily_lane_categories(
        "academic", deck="general"
    )


def test_exclusions_shrink_the_reported_pool():
    db = _db_with_bank()
    first: dict = {}
    q = _run(lily_persistence.lily_fetch_bank_question(
        db, "academic", 1, [], deck="general",
        lane_categories=["academic"], stats=first,
    ))
    assert q is not None
    second: dict = {}
    _run(lily_persistence.lily_fetch_bank_question(
        db, "academic", 1, [], deck="general", lane_categories=["academic"],
        exclude_ids={q["id"]}, stats=second,
    ))
    assert second["excluded"] >= 1
    assert second["pool_remaining"] == first["pool_remaining"] - 1


def test_the_legacy_callers_unified_adult_deck_is_unchanged():
    """No `deck` argument = the pre-WO behaviour, exactly (the adult
    identity fixture still owns that contract)."""
    db = _db_with_bank()
    for _ in range(10):
        q = _run(lily_persistence.lily_fetch_bank_question(
            db, "academic", 1, [],
        ))
        assert q is not None
        row = next(r for r in db.tables["lily_questions"]
                   if f"kb_{r['id']}" == q["id"])
        assert row["adult"] is True


# ---------------------------------------------------------------------------
# 3. The delivery path: zero generation
# ---------------------------------------------------------------------------

def test_a_full_game_plays_from_the_bank_with_the_author_disabled():
    """The S1 acceptance, in miniature: six questions end to end and the
    reasoning lane is never entered."""
    db = _db_with_bank()
    author = _ExplodingReasoning()
    game = _make_game(db, reasoning=author)
    served = _run(_play(game, 6))
    assert len(served) == 6
    assert len({q["id"] for q in served}) == 6
    assert all(q["id"].startswith("kb_") for q in served)
    assert author.calls == [], "the author was called on the delivery path"


def test_the_delivery_path_makes_no_reasoning_lane_call(monkeypatch):
    """Enforced the way the picture arsenal enforces it: the transport
    itself raises, so any reasoning-lane call anywhere below the supply
    seam fails the run."""
    async def _explode(*a, **kw):
        raise AssertionError("reasoning transport entered on delivery")

    monkeypatch.setattr(
        lily_reasoning.LilyReasoning, "_generate_grok_json", _explode
    )
    db = _db_with_bank()
    game = _make_game(db, reasoning=_ExplodingReasoning())
    served = _run(_play(game, 6))
    assert len(served) == 6


def test_a_healthy_author_is_still_never_called_while_the_bank_has_rows():
    db = _db_with_bank()
    author = _CountingReasoning()
    game = _make_game(db, reasoning=author)
    served = _run(_play(game, 6))
    assert all(q["id"].startswith("kb_") for q in served)
    assert author.calls == []


def test_the_author_is_the_replenisher_when_the_lane_and_the_bank_are_dry():
    """Bank-first is not bank-only: an empty bank still gets a question,
    and the receipt says the author had to serve."""
    db = FakeSupabase()
    db.tables["lily_questions"] = []
    author = _CountingReasoning()
    game = _make_game(db, reasoning=author)
    _run(_supply_once(game))
    # The prefetch commit's auto-advance may have armed it already; either
    # slot is "the author served this one".
    landed = game.armed_question or game.next_question
    assert landed is not None
    assert landed["id"] == "q_authored_1"
    receipt = game.supply_receipt()
    assert receipt["generation_calls_on_delivery_path"] == 1
    assert receipt["author_draws"] == 1
    assert receipt["bank_draws"] == 0
    assert receipt["bank_dry_lanes"] == ["general:academic"]


def test_the_supply_receipt_counts_bank_draws_and_zero_generation():
    db = _db_with_bank()
    game = _make_game(db)
    _run(_play(game, 4))
    receipt = game.supply_receipt()
    assert receipt["bank_draws"] == 4
    assert receipt["generation_calls_on_delivery_path"] == 0
    assert receipt["pool_remaining_min"] >= 0


def test_every_bank_draw_emits_its_receipt_line(caplog):
    caplog.set_level("INFO", logger="lily_agent")
    db = _db_with_bank()
    game = _make_game(db)
    _run(_play(game, 3))
    lines = [r.getMessage() for r in caplog.records
             if "BANK_DRAW" in r.getMessage()]
    assert len(lines) == 3
    for line in lines:
        for field in ("session=", "q=", "id=", "deck=", "lane=",
                      "excluded=", "pool_remaining="):
            assert field in line


def test_the_question_timeline_names_the_bank_as_the_source():
    db = _db_with_bank()
    game = _make_game(db)
    _run(_play(game, 3))
    timeline = game.sk.question_timeline
    assert set(timeline) == {1, 2, 3}
    for row in timeline.values():
        assert row["source"] == "bank"
        assert str(row["bank_id"]).startswith("kb_")


# ---------------------------------------------------------------------------
# 4. Consecutive-session no-repeat (the group ledger)
# ---------------------------------------------------------------------------

def test_a_second_session_never_draws_what_the_first_one_asked():
    db = _db_with_bank()
    first = _make_game(db, session_id="lily-S1-a")
    served_1 = _run(_play(first, 6))

    second = _make_game(db, session_id="lily-S1-b")
    second.asked_history = _run(
        lily_bank.lily_load_asked_history(db, GROUP_ID)
    )
    assert len(second.asked_history) == 6
    served_2 = _run(_play(second, 6))

    ids_1 = {q["id"] for q in served_1}
    ids_2 = {q["id"] for q in served_2}
    assert ids_1 & ids_2 == set()
    hashes_1 = {lily_bank.lily_question_text_hash(q["prompt"])
                for q in served_1}
    hashes_2 = {lily_bank.lily_question_text_hash(q["prompt"])
                for q in served_2}
    assert hashes_1 & hashes_2 == set()


def test_a_different_group_may_still_be_served_the_same_rows():
    """The no-repeat window is per GROUP, not global — proving the
    exclusion is scoped and not just a global burn."""
    db = _db_with_bank()
    first = _make_game(db, session_id="lily-S1-a", group_id="grp_one")
    served_1 = _run(_play(first, 4))
    other = _make_game(db, session_id="lily-S1-c", group_id="grp_two")
    other.asked_history = _run(
        lily_bank.lily_load_asked_history(db, "grp_two")
    )
    assert other.asked_history == []
    served_2 = _run(_play(other, 4))
    assert {q["id"] for q in served_1} & {q["id"] for q in served_2}


def test_the_in_session_mirror_is_written_before_the_next_draw_reads_it():
    """S4 (write-back-before-next-read): arming appends to the mirror the
    very next draw excludes, in the same synchronous call."""
    db = _db_with_bank()
    game = _make_game(db)
    _run(_supply_once(game))
    assert game.arm_next_question()
    armed = dict(game.armed_question)
    assert game.asked_history[-1]["question_id"] == armed["id"]
    ids, hashes = game._no_repeat_exclusion()
    assert armed["id"] in ids
    assert lily_bank.lily_question_text_hash(armed["prompt"]) in hashes


def test_the_asked_history_writer_inserts_the_row(caplog):
    """Fleet S3: every writer path INSERT-tested against a fake client."""
    db = FakeSupabase()
    db.tables["lily_asked_history"] = []
    question = {
        "id": "kb_4242",
        "prompt": "fixture question 4242",
        "canonical_answer": "answer 4242",
        "category": "academic",
    }
    _run(lily_bank.lily_record_asked(db, GROUP_ID, question, SESSION_ID))
    rows = db.tables["lily_asked_history"]
    assert len(rows) == 1
    row = rows[0]
    assert row["group_id"] == GROUP_ID
    assert row["question_id"] == "kb_4242"
    assert row["question_text_hash"] == lily_bank.lily_question_text_hash(
        question["prompt"]
    )
    assert row["canonical_answer"] == "answer 4242"
    assert row["category"] == "academic"
    assert row["session_id"] == SESSION_ID


# ---------------------------------------------------------------------------
# 5. The exclusion union the recovery ladder was missing
# ---------------------------------------------------------------------------

def test_the_recovery_bank_draw_excludes_burned_questions():
    """`_bank_to_supply` built its own (history | drawn) union and left the
    burned sets out, so the Z2 ladder could re-draw a question whose answer
    was already on air — caught only later, by REARM_BLOCKED, one wasted
    slot at a time."""
    db = FakeSupabase()
    db.tables["lily_questions"] = [
        _row(1, "academic", False, tier=1),
        _row(2, "academic", False, tier=1),
    ]
    game = _make_game(db)
    dead = _row(1, "academic", False, tier=1)
    game._burned_question_ids = {"kb_1"}
    game._burned_question_hashes = {
        lily_bank.lily_question_text_hash(dead["question"])
    }
    result = _run(game._bank_to_supply(trigger="test"))
    assert result == "supplied"
    assert game.next_question["id"] == "kb_2"


# ---------------------------------------------------------------------------
# 6. Per-lane bank health
# ---------------------------------------------------------------------------

def test_bank_health_reports_ready_and_burned_per_lane():
    db = _db_with_bank()
    health = _run(lily_bank.lily_bank_health(db))
    assert set(health) == set(CATEGORY_FAMILIES)
    for lane, row in health.items():
        assert row["ready"] > 0, lane
        assert row["burned"] >= 0
        # S2 owns these two; until it lands they are honestly null, not 0.
        assert row["last_replenished_at"] is None
        assert row["rejection_rate"] is None
    assert health["academic"]["burned"] == 1


def test_bank_health_reads_the_s2_lane_health_rows_when_they_exist():
    """The S2 contract, coded against a fixture: S2 writes one
    `lily_bank_lane_health` row per lane and S1 reads it verbatim."""
    db = _db_with_bank()
    db.tables["lily_bank_lane_health"] = [
        {
            "lane": "academic",
            "last_replenished_at": "2026-09-06T10:00:00+00:00",
            "rejection_rate": 0.25,
        },
    ]
    health = _run(lily_bank.lily_bank_health(db))
    assert health["academic"]["last_replenished_at"] == (
        "2026-09-06T10:00:00+00:00"
    )
    assert health["academic"]["rejection_rate"] == 0.25
    assert health["wordplay"]["last_replenished_at"] is None
