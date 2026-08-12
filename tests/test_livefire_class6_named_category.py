"""WO-LILY-LIVEFIRE-001 CLASS 6 — named-category supply.

Fixture lily-639007-f80aa6bf. 6a diagnosis (two independent resource
problems, see CHANGELOG): the 5 PREFETCH_FAILED TimeoutErrors (question-
driven, LILY_REASONING generation latency) do NOT align with the 5
memory-pressure warnings (livekit.agents, a FIXED 120s monitor firing at
17:54/17:56/17:58/18:00/18:02 — a static RSS baseline from the ECAPA
voiceprint preload at connect, not a per-question leak). This file covers
6b–6e:

- 6b/6c: a named topic keeps generating; a dry bank / an upstream timeout is
  a supply defect, not an end state.
- 6d: a category flip only happens on true generation failure, announced.
- 6e: duplicate-id draws are a generator defect, fixed at id allocation.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_persistence
import lily_reasoning
from lily_reasoning import _shape_question, _alloc_generated_id
from test_hotfix008_z2c_transition_release import _make_game, _run


# -- 6e: generated ids are unique at allocation ---------------------------

def test_generated_ids_are_process_unique():
    ids = {_alloc_generated_id() for _ in range(2000)}
    assert len(ids) == 2000
    assert all(i.startswith("q_gen_") for i in ids)


def test_shape_question_overrides_colliding_model_id():
    # The model reuses q_4821 across two generations (the live collision).
    q1 = _shape_question(
        {"id": "q_4821", "prompt": "Parthenon?", "canonical_answer": "Athena"}
    )
    q2 = _shape_question(
        {"id": "q_4821", "prompt": "Crete?", "canonical_answer": "Minos"}
    )
    assert q1["id"] != q2["id"]          # collision impossible now
    assert q1["id"] != "q_0000"          # no shared default either
    assert not q1["id"].startswith("kb_")  # still a generated id


# -- 6b/6c: a named topic keeps generating when the bank is dry -----------

GEN_Q = {
    "id": "q_gen_stub",
    "prompt": "Which Greek city-state raised its boys in the agoge?",
    "canonical_answer": "Sparta",
    "acceptable_answers": ["sparta"],
    "category": "Greece",
    "difficulty_tier": 1,
    "reveal_color": "",
}


def _operator_greece_game(monkeypatch, *, bank_dry=True, gen_result=None):
    game = _make_game()
    game.supabase = object()  # sentinel; the fetch is patched
    rnd = game._round_for_next_question()
    game._category_override = {rnd: "Greece"}
    assert game._is_operator_category("Greece") is True

    async def _fetch(*a, **k):
        return None if bank_dry else dict(GEN_Q)

    monkeypatch.setattr(lily_persistence, "lily_fetch_bank_question", _fetch)

    async def _gen(*a, **k):
        return dict(gen_result) if gen_result is not None else None

    game.reasoning.prefetch_question = _gen
    game._curate_generated_question = lambda q, cat, hh: q

    async def _ensure_choices(q):
        return None

    game.reasoning.ensure_choices = _ensure_choices
    return game, rnd


def test_named_topic_backfills_via_generation_no_flip(monkeypatch):
    game, rnd = _operator_greece_game(
        monkeypatch, bank_dry=True, gen_result=GEN_Q
    )

    def _scenario():
        return game._bank_to_supply(trigger="recovery:test")

    result = _run(_scenario)
    assert result == "supplied"
    assert game.next_question is not None
    assert game.next_question.get("canonical_answer") == "Sparta"
    # The topic KEPT its name — no flip to the fixed rotation.
    assert game._category_override.get(rnd) == "Greece"


def test_flip_only_after_generation_fails_and_is_announced(monkeypatch):
    # Bank dry AND generation returns nothing — only NOW may the round flip,
    # and it must be announced (no silent flip).
    game, rnd = _operator_greece_game(
        monkeypatch, bank_dry=True, gen_result=None
    )
    notes = []
    game.sk.set_status_note = lambda note: notes.append(note)

    def _scenario():
        return game._bank_to_supply(trigger="recovery:test")

    _run(_scenario)
    # The override was dropped (true generation failure) …
    assert game._category_override.get(rnd) != "Greece"
    # … and the flip was announced honestly — never silent.
    assert notes, "a category flip must set the honest released note"
    assert "Greece" in notes[-1]
