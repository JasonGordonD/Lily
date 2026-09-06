"""REFACTOR-STAGE-1B-001 P1-6 — `proposed_category` has a producer.

The consumer (LilyGame._curate_generated_question) reads
`question.get("proposed_category")` and tallies it through
lily_bank.lily_record_category_proposal — the base of the promotion ladder
(migration 011 lily_category_candidates, lily_load_promoted_categories,
the glass line "extra categories in tonight's rotation"). But the
generation shape sent to the model (_GROK_QUESTION_SHAPE_ADDENDUM) never
asked for the key, so no generated question ever carried one and the
ladder could not populate from gameplay. Now the shape names the optional
key, _shape_question normalises a model-supplied value (stripped string;
anything else dropped), and a fixture payload flows end to end into the
recorded proposal.
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_bank  # noqa: E402
import lily_reasoning  # noqa: E402
from lily_agent import LilyGame  # noqa: E402
from lily_reasoning import LilyReasoning, _shape_question  # noqa: E402
from lily_scorekeeper import LilyScorekeeper  # noqa: E402


_BASE = {"prompt": "Which planet is largest?", "canonical_answer": "Jupiter"}

# The exact object shape the addendum asks the model for, plus the
# optional key — a model that follows the shape emits this.
FIXTURE_GENERATION_PAYLOAD = {
    "id": "q_4821",
    "category": "geography",
    "difficulty_tier": 2,
    "prompt": "The tip of Cape Cod holds this town, where the Mayflower first "
              "dropped anchor in 1620.",
    "canonical_answer": "Provincetown",
    "acceptable_answers": ["provincetown", "p-town"],
    "reveal_color": "The Pilgrims signed the Mayflower Compact in its harbor.",
    "proposed_category": " Cape Cod ",
}


# -- producer: the shape sent to the model names the key ------------------

def test_generation_shape_asks_for_the_optional_key():
    addendum = lily_reasoning._GROK_QUESTION_SHAPE_ADDENDUM
    assert '"proposed_category"' in addendum
    # Optional, never demanded: the exact-fields contract stays intact.
    assert "EXACTLY these" in addendum


# -- _shape_question normalisation -----------------------------------------

def test_shape_question_normalises_a_model_supplied_value():
    shaped = _shape_question(dict(_BASE, proposed_category="  Cape Cod "))
    assert shaped["proposed_category"] == "Cape Cod"


def test_shape_question_drops_a_non_string_or_blank_value():
    assert "proposed_category" not in _shape_question(dict(_BASE, proposed_category=42))
    assert "proposed_category" not in _shape_question(dict(_BASE, proposed_category="   "))
    assert "proposed_category" not in _shape_question(dict(_BASE, proposed_category=None))
    assert "proposed_category" not in _shape_question(dict(_BASE, proposed_category=["x"]))


def test_shape_question_without_the_key_is_unchanged():
    assert "proposed_category" not in _shape_question(dict(_BASE))


# -- pipeline: fixture payload -> generate_question -> curate -> proposal --

def _reasoning(raw: str) -> LilyReasoning:
    r = LilyReasoning.__new__(LilyReasoning)
    r._model = "test-reasoning-model"
    r._vocal_model = "test-vocal-model"
    calls = []

    async def _fake_grok(prompt, **kwargs):
        calls.append(prompt)
        return raw

    r._generate_grok_json = _fake_grok
    r.calls = calls
    return r


def _game(supabase):
    game = LilyGame.bare(sk=LilyScorekeeper("lily-S1B-proposal"))
    game.supabase = supabase
    game.group_id = "group-77"
    game.asked_history = []
    game.promoted_categories = []
    return game


def test_fixture_payload_flows_to_the_recorded_proposal(monkeypatch):
    recorded, banked = [], []

    async def _record(supabase, name, family, group_id):
        recorded.append((name, family, group_id))

    async def _bank(supabase, question):
        banked.append(question)

    monkeypatch.setattr(lily_bank, "lily_record_category_proposal", _record)
    monkeypatch.setattr(lily_bank, "lily_bank_generated_question", _bank)
    node = _reasoning(json.dumps(FIXTURE_GENERATION_PAYLOAD))
    game = _game(supabase=object())

    async def scenario():
        question = await node.generate_question("geography", 2, [])
        assert question is not None
        # The prompt the model saw carried the optional-key instruction.
        assert '"proposed_category"' in node.calls[0]
        assert question["proposed_category"] == "Cape Cod"
        served = game._curate_generated_question(question, "geography", set())
        await asyncio.sleep(0)
        return served

    served = asyncio.run(scenario())
    # The consumer normalises (lily_normalize_category_name) before the tally.
    assert recorded == [("cape cod", "geography", "group-77")]
    # Unpromoted: the question SERVES under its round family.
    assert served["category"] == "geography"
    assert banked and banked[0]["prompt"] == FIXTURE_GENERATION_PAYLOAD["prompt"]


def test_a_promoted_proposal_relabels_a_rotation_family(monkeypatch):
    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(lily_bank, "lily_record_category_proposal", _noop)
    monkeypatch.setattr(lily_bank, "lily_bank_generated_question", _noop)
    game = _game(supabase=object())
    game.promoted_categories = ["cape cod"]
    question = _shape_question(dict(FIXTURE_GENERATION_PAYLOAD))

    async def scenario():
        served = game._curate_generated_question(question, "geography", set())
        await asyncio.sleep(0)
        return served

    assert asyncio.run(scenario())["category"] == "cape cod"


def test_no_proposal_records_nothing(monkeypatch):
    recorded = []

    async def _record(*a):
        recorded.append(a)

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(lily_bank, "lily_record_category_proposal", _record)
    monkeypatch.setattr(lily_bank, "lily_bank_generated_question", _noop)
    game = _game(supabase=object())
    payload = {k: v for k, v in FIXTURE_GENERATION_PAYLOAD.items() if k != "proposed_category"}

    async def scenario():
        game._curate_generated_question(_shape_question(payload), "geography", set())
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert recorded == []
