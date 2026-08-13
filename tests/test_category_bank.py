"""WO-LILY-CAPABILITY-RESTORE-001 (scope addition) — category bank
persistence.

When an operator names a topic on the fly, the category is registered as
first-class in lily_category_candidates and its generated questions bank
into lily_questions (via the existing curate path), so a later request for
the same topic can draw from the bank instead of regenerating from scratch
— the arsenal compounds. This suite pins:

  - the pure operator-category payload builder (first-class flag, bump,
    group dedup);
  - first-class visibility: operator_requested rows surface immediately,
    without earning the use_count/group promotion gate;
  - registration is idempotent by name (no duplicate category) and never
    raises; a failed write logs the full payload for recovery;
  - the live wiring: lily_set_category marks the topic an operator
    category, which routes the supply path to prefer the bank.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_bank
import lily_say_gate
from lily_agent import LilyAgent, LilyGame
from lily_scorekeeper import LilyScorekeeper


class _FakeResult:
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    def __init__(self, store, upserts):
        self._store = store
        self._upserts = upserts
        self._name = None
        self._single = False

    def select(self, *a, **k):
        return self

    def eq(self, col, val):
        if col == "name":
            self._name = val
        return self

    def maybe_single(self):
        self._single = True
        return self

    def upsert(self, payload, on_conflict=None):
        self._upserts.append(dict(payload))
        self._store[payload["name"]] = dict(payload)
        return self

    def execute(self):
        if self._single:
            return _FakeResult(self._store.get(self._name))
        return _FakeResult(list(self._store.values()))


class _FakeSupabase:
    def __init__(self, seed=None):
        self.store = dict(seed or {})
        self.upserts = []

    def table(self, name):
        assert name == "lily_category_candidates"
        return _FakeQuery(self.store, self.upserts)


class _RaisingSupabase:
    def table(self, name):
        raise RuntimeError("db down")


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# -- pure builder --------------------------------------------------------------


def test_apply_operator_category_builds_first_class_payload():
    p = lily_bank.lily_apply_operator_category(None, "Game of Thrones", "", "grp1")
    assert p["name"] == "game of thrones"
    assert p["operator_requested"] is True
    assert p["use_count"] == 1
    assert p["groups"] == ["grp1"]
    assert p["family"] == "game of thrones"  # its own family


def test_apply_operator_category_bumps_and_dedups_group():
    existing = {
        "name": "japan", "family": "japan",
        "use_count": 2, "groups": ["grp1"], "operator_requested": True,
    }
    p = lily_bank.lily_apply_operator_category(existing, "Japan", "", "grp1")
    assert p["use_count"] == 3
    assert p["groups"] == ["grp1"]  # same group, no dup
    q = lily_bank.lily_apply_operator_category(existing, "Japan", "", "grp2")
    assert sorted(q["groups"]) == ["grp1", "grp2"]


# -- first-class visibility ----------------------------------------------------


def test_promoted_or_operator_first_class_on_operator_flag():
    # operator_requested wins even at use_count 1, zero groups
    assert lily_bank._promoted_or_operator(
        {"operator_requested": True, "use_count": 1, "groups": []}
    ) is True
    # a plain proposal still needs the count gate
    assert lily_bank._promoted_or_operator(
        {"operator_requested": False, "use_count": 1, "groups": ["g"]}
    ) is False
    assert lily_bank._promoted_or_operator(
        {"use_count": 10, "groups": ["a", "b", "c"]}
    ) is True


def test_load_promoted_includes_operator_requested():
    seed = {
        "game of thrones": {
            "name": "game of thrones", "use_count": 1, "groups": ["g1"],
            "operator_requested": True,
        },
        "weak proposal": {
            "name": "weak proposal", "use_count": 2, "groups": ["g1"],
            "operator_requested": False,
        },
    }
    names = _run(lily_bank.lily_load_promoted_categories(_FakeSupabase(seed)))
    assert "game of thrones" in names
    assert "weak proposal" not in names


# -- registration: idempotent, first-class, never raises -----------------------


def test_register_operator_category_writes_first_class_row():
    sb = _FakeSupabase()
    ok = _run(lily_bank.lily_register_operator_category(
        sb, "Game of Thrones", "", "grp1"))
    assert ok is True
    assert sb.store["game of thrones"]["operator_requested"] is True
    assert sb.store["game of thrones"]["use_count"] == 1


def test_register_operator_category_is_idempotent_by_name():
    sb = _FakeSupabase()
    _run(lily_bank.lily_register_operator_category(sb, "Japan", "", "grp1"))
    _run(lily_bank.lily_register_operator_category(sb, "japan", "", "grp1"))
    # one row (dedup by normalized name), use_count bumped
    assert list(sb.store.keys()) == ["japan"]
    assert sb.store["japan"]["use_count"] == 2


def test_register_operator_category_never_raises_and_returns_false_on_failure():
    ok = _run(lily_bank.lily_register_operator_category(
        _RaisingSupabase(), "Japan", "", "grp1"))
    assert ok is False  # logged full payload for recovery, no raise


def test_register_operator_category_noops_without_supabase():
    assert _run(lily_bank.lily_register_operator_category(
        None, "Japan", "", "grp1")) is False


# -- live wiring: operator topic routes the supply path to the bank ------------


def _make_game(question_number: int = 0) -> LilyGame:
    game = LilyGame.bare()
    game.sk = LilyScorekeeper("category-bank-fixture")
    game.sk.mode = "general"
    game.sk.question_number = question_number
    game.sk.questions_per_round = 6
    game.rounds_total = 4
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game._category_override = {}
    game.next_question = None
    game.armed_question = None
    game._prefetch_task = None
    game._prefetch_stall_ticks = 0
    game.game_started = True
    game.game_over = False
    game.supabase = None  # tool's register leg is guarded off
    game.group_id = "grp_fixture"
    game._custom_round_registered = {}

    def _supply():
        """HOTFIX-006 N2: the tool now builds before it answers, and an
        unbuilt topic rolls its override back. This models _prefetch_inner's
        commit tail so the fixture exercises the bank-preference wiring
        rather than the refusal path."""
        rnd = game._round_for_next_question()
        category = game._category_for_round(rnd)
        question = {
            "id": "q_built", "prompt": f"A {category} question?",
            "canonical_answer": "answer", "acceptable_answers": ["answer"],
            "category": category,
        }
        game.next_question = question
        game._register_custom_question(category, question)

    game.start_prefetch = _supply
    return game


def test_set_category_marks_it_an_operator_category():
    game = _make_game()
    agent = LilyAgent.__new__(LilyAgent)
    agent._game = game
    _run(LilyAgent.lily_set_category.__wrapped__(agent, None, "Game of Thrones"))
    # _is_operator_category is what flips the supply path to prefer the bank.
    assert game._is_operator_category("Game of Thrones") is True
    assert game._is_operator_category("academic") is False


def test_fixed_rotation_is_not_treated_as_operator_category():
    game = _make_game()
    # No override set → the fixed families are never bank-preferred.
    assert game._is_operator_category("academic") is False
    assert game._is_operator_category("") is False
