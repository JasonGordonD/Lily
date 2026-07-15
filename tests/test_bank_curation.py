"""Bank curation loop (WO-LILY-OMNIBUS-002, sub-agents D/E/F).

Pure-logic coverage — everything here runs offline, outside the livekit /
supabase import graph:

  D: dedup normalizer + hash, exact/fuzzy near-dup detection, asked-history
     sets, and the dedup-at-insert path (against a fake supabase client).
  E: the tuning decision table (exposure floor, tier moves with clamps,
     retirement precedence, band boundaries) and the pure tuning plan.
  F: category-proposal upsert arithmetic and the promotion gate.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_bank
import lily_bank_tuning
from lily_bank import (
    lily_apply_category_proposal,
    lily_bank_generated_question,
    lily_category_promotion_ready,
    lily_find_duplicate,
    lily_history_hashes,
    lily_history_question_ids,
    lily_normalize_question_text,
    lily_question_text_hash,
)
from lily_bank_tuning import (
    lily_aggregate_question_stats,
    lily_tuning_decision,
    lily_tuning_plan,
)


# ---------------------------------------------------------------------------
# D — normalizer + hash
# ---------------------------------------------------------------------------

def test_normalize_lowercases_strips_punctuation_and_collapses_whitespace():
    assert lily_normalize_question_text(
        "  Which SEA, colorful, sits between Europe & Asia?! "
    ) == "which sea colorful sits between europe asia"


def test_normalize_handles_empty_and_none():
    assert lily_normalize_question_text(None) == ""
    assert lily_normalize_question_text("   ") == ""
    assert lily_normalize_question_text("?!.,;") == ""


def test_hash_equal_for_punctuation_and_case_variants():
    a = lily_question_text_hash("Which sea sits between Europe and Asia?")
    b = lily_question_text_hash("which sea sits between   europe and asia")
    assert a == b


def test_hash_differs_for_different_questions():
    a = lily_question_text_hash("Which sea sits between Europe and Asia?")
    b = lily_question_text_hash("Which river runs through Paris?")
    assert a != b


# ---------------------------------------------------------------------------
# D — near-dup detection
# ---------------------------------------------------------------------------

_EXISTING = [
    {"id": 1, "category": "academic",
     "question": "Which sea sits between Europe and Asia?"},
    {"id": 2, "category": "pop culture",
     "question": "Which pop star released the album Thriller?"},
]


def test_exact_dup_detected_regardless_of_category():
    # Same text filed under a different category is still the same question.
    dup = lily_find_duplicate(
        "which SEA sits between europe and asia", "wordplay", _EXISTING
    )
    assert dup is not None
    assert dup["id"] == 1
    assert dup["match"] == "exact"


def test_fuzzy_dup_detected_within_same_category():
    dup = lily_find_duplicate(
        "Which sea sits between Europe and Asia today?", "academic", _EXISTING
    )
    assert dup is not None
    assert dup["id"] == 1
    assert dup["match"] == "fuzzy"


def test_fuzzy_similarity_in_other_category_is_not_a_dup():
    # Near-identical text but a different category and not hash-exact:
    # the fuzzy tier is scoped to the same category only.
    dup = lily_find_duplicate(
        "Which sea sits between Europe and Asia today?", "wordplay", _EXISTING
    )
    assert dup is None


def test_distinct_question_is_not_a_dup():
    assert lily_find_duplicate(
        "Which planet is closest to the sun?", "academic", _EXISTING
    ) is None


def test_empty_prompt_is_never_a_dup():
    assert lily_find_duplicate("", "academic", _EXISTING) is None
    assert lily_find_duplicate("?!", "academic", _EXISTING) is None


def test_ratio_threshold_is_respected():
    rows = [{"id": 9, "category": "academic", "question": "aaaa bbbb cccc dddd"}]
    # Similar but below the 0.87 ratio (difflib 0.848): NOT a dup — the
    # fuzzy gate is a threshold, not a vibe.
    assert lily_find_duplicate("aaaa bbbb cccc", "academic", rows) is None
    # Above the threshold (difflib 0.9): dup.
    long_rows = [{"id": 10, "category": "academic",
                  "question": "Which broad river runs through central Paris?"}]
    assert lily_find_duplicate(
        "Which broad river runs through Paris?", "academic", long_rows
    ) is not None


# ---------------------------------------------------------------------------
# D — asked-history sets
# ---------------------------------------------------------------------------

def test_history_sets_extract_ids_and_hashes():
    rows = [
        {"question_id": "kb_7", "question_text_hash": "aa"},
        {"question_id": "q_0042", "question_text_hash": "bb"},
        {"question_id": None, "question_text_hash": None},
        {},
    ]
    assert lily_history_question_ids(rows) == {"kb_7", "q_0042"}
    assert lily_history_hashes(rows) == {"aa", "bb"}
    assert lily_history_question_ids(None) == set()
    assert lily_history_hashes(None) == set()


# ---------------------------------------------------------------------------
# D — dedup at insert (fake supabase client)
# ---------------------------------------------------------------------------

class _FakeResult:
    def __init__(self, data):
        self.data = data


class _FakeTable:
    def __init__(self, store, name):
        self._store = store
        self._name = name
        self._pending_insert = None

    def select(self, *_args, **_kwargs):
        return self

    def insert(self, payload):
        self._pending_insert = payload
        return self

    def execute(self):
        rows = self._store.setdefault(self._name, [])
        if self._pending_insert is not None:
            row = dict(self._pending_insert)
            row["id"] = len(rows) + 1
            rows.append(row)
            self._pending_insert = None
            return _FakeResult([row])
        return _FakeResult(list(rows))


class _FakeSupabase:
    def __init__(self):
        self.store = {}

    def table(self, name):
        return _FakeTable(self.store, name)


def _bank(supabase, question, mode="general"):
    return asyncio.run(lily_bank_generated_question(supabase, question, mode))


def _question(prompt, category="academic", **overrides):
    q = {
        "id": "q_0001",
        "category": category,
        "difficulty_tier": 2,
        "prompt": prompt,
        "canonical_answer": "the Answer",
        "acceptable_answers": ["the answer"],
        "reveal_color": "spice",
    }
    q.update(overrides)
    return q


def test_generated_question_is_banked_with_generated_source():
    fake = _FakeSupabase()
    row_id = _bank(fake, _question("Which river runs through Paris?"))
    assert row_id == 1
    rows = fake.store["lily_questions"]
    assert len(rows) == 1
    assert rows[0]["source"] == "generated"
    assert rows[0]["status"] == "active"
    assert rows[0]["adult"] is False
    assert rows[0]["question"] == "Which river runs through Paris?"


def test_exact_dup_is_discarded_at_insert():
    fake = _FakeSupabase()
    assert _bank(fake, _question("Which river runs through Paris?")) == 1
    assert _bank(fake, _question("which river runs through PARIS!?")) is None
    assert len(fake.store["lily_questions"]) == 1


def test_fuzzy_dup_same_category_is_discarded_at_insert():
    fake = _FakeSupabase()
    assert _bank(fake, _question("Which broad river runs through central Paris?")) == 1
    assert _bank(fake, _question("Which broad river runs through Paris?")) is None
    assert len(fake.store["lily_questions"]) == 1


def test_adult_mode_banks_with_adult_flag():
    fake = _FakeSupabase()
    _bank(fake, _question("Which emperor taxed beards?"), mode="adult")
    row = fake.store["lily_questions"][0]
    assert row["adult"] is True
    assert row["mode"] == "adult"


# ---------------------------------------------------------------------------
# E — tuning decision table
# ---------------------------------------------------------------------------

def test_tune_below_exposure_floor_is_untouched():
    d = lily_tuning_decision(servings=4, correct=4, current_tier=2)
    assert d["action"] == "none"
    assert "exposure floor" in d["reason"]


def test_tune_non_active_status_is_untouched():
    d = lily_tuning_decision(servings=20, correct=16, current_tier=2,
                             status="burned")
    assert d["action"] == "none"


def test_tune_high_success_moves_tier_down_one():
    d = lily_tuning_decision(servings=10, correct=8, current_tier=3)
    assert d["action"] == "tier_down"
    assert d["new_tier"] == 2


def test_tune_tier_down_clamps_at_min():
    d = lily_tuning_decision(servings=10, correct=8, current_tier=1)
    assert d["action"] == "none"


def test_tune_low_success_moves_tier_up_one():
    d = lily_tuning_decision(servings=10, correct=2, current_tier=2)
    assert d["action"] == "tier_up"
    assert d["new_tier"] == 3


def test_tune_tier_up_clamps_at_max():
    d = lily_tuning_decision(servings=10, correct=2, current_tier=4)
    assert d["action"] == "none"


def test_tune_retires_when_nobody_gets_it():
    d = lily_tuning_decision(servings=20, correct=1, current_tier=3)
    assert d["action"] == "retire"


def test_tune_retires_when_everybody_gets_it():
    d = lily_tuning_decision(servings=20, correct=20, current_tier=1)
    assert d["action"] == "retire"


def test_tune_retirement_outranks_tier_move():
    # 0% success is also < 30%: retirement must win, not tier_up.
    d = lily_tuning_decision(servings=10, correct=0, current_tier=2)
    assert d["action"] == "retire"


def test_tune_band_boundaries_are_exclusive():
    # Exactly 75% is NOT > 75%; exactly 30% is NOT < 30% -> no move.
    assert lily_tuning_decision(20, 15, 2)["action"] == "none"
    assert lily_tuning_decision(20, 6, 2)["action"] == "none"
    # Exactly 10% is NOT < 10% -> tier_up (still < 30%).
    assert lily_tuning_decision(20, 2, 2)["action"] == "tier_up"
    # Exactly 95% is NOT > 95% -> tier_down (still > 75%).
    assert lily_tuning_decision(20, 19, 2)["action"] == "tier_down"


def test_tune_midband_success_is_untouched():
    d = lily_tuning_decision(servings=10, correct=5, current_tier=2)
    assert d["action"] == "none"


def test_aggregate_counts_servings_from_history_and_correct_from_answers():
    asked = [{"question_id": "kb_1"}] * 6 + [{"question_id": "kb_2"}] * 3
    answers = (
        [{"question_id": "kb_1", "verdict": "correct"}] * 5
        + [{"question_id": "kb_1", "verdict": "incorrect"}] * 4
        + [{"question_id": "kb_2", "verdict": "correct"}]
    )
    stats = lily_aggregate_question_stats(asked, answers)
    assert stats["kb_1"] == {"servings": 6, "correct": 5}
    assert stats["kb_2"] == {"servings": 3, "correct": 1}


def test_tuning_plan_end_to_end():
    asked = (
        [{"question_id": "kb_1"}] * 10   # 9/10 correct -> tier_down
        + [{"question_id": "kb_2"}] * 10  # 0/10 -> retire
        + [{"question_id": "kb_3"}] * 4   # under exposure floor -> skip
        + [{"question_id": "kb_4"}] * 10  # 5/10 -> in band -> skip
    )
    answers = (
        [{"question_id": "kb_1", "verdict": "correct"}] * 9
        + [{"question_id": "kb_4", "verdict": "correct"}] * 5
    )
    questions = [
        {"id": 1, "difficulty_tier": 3, "status": "active"},
        {"id": 2, "difficulty_tier": 2, "status": "active"},
        {"id": 3, "difficulty_tier": 2, "status": "active"},
        {"id": 4, "difficulty_tier": 2, "status": "active"},
        {"id": 5, "difficulty_tier": 2, "status": "active"},  # never served
    ]
    plan = lily_tuning_plan(asked, answers, questions)
    by_qid = {p["question_id"]: p for p in plan}
    assert set(by_qid) == {"kb_1", "kb_2"}
    assert by_qid["kb_1"]["action"] == "tier_down"
    assert by_qid["kb_1"]["update"] == {"difficulty_tier": 2}
    assert by_qid["kb_2"]["action"] == "retire"
    assert by_qid["kb_2"]["update"] == {"status": "retired"}


def test_tuning_plan_one_move_per_run():
    # A very easy tier-4 question steps down exactly ONE tier per run.
    asked = [{"question_id": "kb_1"}] * 10
    answers = [{"question_id": "kb_1", "verdict": "correct"}] * 9
    questions = [{"id": 1, "difficulty_tier": 4, "status": "active"}]
    plan = lily_tuning_plan(asked, answers, questions)
    assert len(plan) == 1
    assert plan[0]["update"] == {"difficulty_tier": 3}


# ---------------------------------------------------------------------------
# F — category proposals: upsert arithmetic + promotion gate
# ---------------------------------------------------------------------------

def test_promotion_requires_both_uses_and_groups():
    assert lily_category_promotion_ready(10, 3) is True
    assert lily_category_promotion_ready(25, 7) is True
    assert lily_category_promotion_ready(9, 3) is False
    assert lily_category_promotion_ready(10, 2) is False
    assert lily_category_promotion_ready(0, 0) is False
    assert lily_category_promotion_ready(None, None) is False


def test_apply_proposal_creates_first_row():
    payload = lily_apply_category_proposal(None, "Deep Sea", "academic", "grp_a")
    assert payload == {
        "name": "deep sea",
        "family": "academic",
        "use_count": 1,
        "groups": ["grp_a"],
    }


def test_apply_proposal_increments_and_dedupes_groups():
    existing = {"name": "deep sea", "family": "academic",
                "use_count": 4, "groups": ["grp_a", "grp_b"]}
    payload = lily_apply_category_proposal(existing, "deep sea", "academic", "grp_b")
    assert payload["use_count"] == 5
    assert payload["groups"] == ["grp_a", "grp_b"]
    payload = lily_apply_category_proposal(existing, "deep sea", "academic", "grp_c")
    assert payload["groups"] == ["grp_a", "grp_b", "grp_c"]


def test_apply_proposal_keeps_original_family():
    existing = {"name": "deep sea", "family": "academic",
                "use_count": 1, "groups": ["grp_a"]}
    payload = lily_apply_category_proposal(existing, "deep sea", "pop culture", "grp_b")
    assert payload["family"] == "academic"


def test_proposal_count_reaching_gate_promotes():
    row = None
    # 10 proposals across 3 distinct groups -> promoted on the 10th.
    groups = ["grp_a", "grp_b", "grp_c"]
    for i in range(10):
        row = lily_apply_category_proposal(row, "deep sea", "academic",
                                           groups[i % 3])
        promoted = lily_category_promotion_ready(
            row["use_count"], len(row["groups"])
        )
        assert promoted is (i == 9)
