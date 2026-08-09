"""Multiple-choice round format (WO-LILY-OMNIBUS-002, sub-agent G).

Covers the four seams the WO names:
  - generation shape: the MC addendum demands 4 choices; bank questions
    without choices get 3 synthesized distractors at prefetch (reasoning
    node only); synthesis failure degrades honestly to freeform
  - Tier-1 MC matching: letters ("B", "letter b"), positions ("the second
    one"), fuzzy option text; clean wrong picks are DEFINITIVE incorrect;
    mumbles stay Tier-2 ("uncertain")
  - 50/50 elimination: two wrong options out, canonical always survives;
    `eliminated` indices ride room metadata next to `choices`
  - round_format flag: scorekeeper field, default schedule (round 2 is the
    session's one MC round), lily_set_round_format override, snapshot /
    rehydrate, state-block line

The agent-level tests import lily_agent (and therefore livekit) — same
boundary note as test_say_gate_dispatch.py.
"""

import asyncio
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_audeering_consumers
import lily_evaluation
import lily_say_gate
from lily_agent import LilyAgent, LilyGame
from lily_evaluation import (
    lily_canonical_choice_index,
    lily_fifty_fifty_eliminations,
    lily_tier1_evaluate_mc,
    lily_tier1_evaluate_question,
)
from lily_reasoning import LilyReasoning, lily_valid_choices
from lily_scorekeeper import DEFAULT_MC_ROUND, LilyScorekeeper

CHOICES = ["Canberra", "Sydney", "Melbourne", "SpongeBob's pineapple"]
CANONICAL = "Canberra"

MC_QUESTION = {
    "id": "q_4242",
    "category": "academic",
    "difficulty_tier": 2,
    "prompt": "Name the capital city of Australia.",
    "canonical_answer": CANONICAL,
    "acceptable_answers": ["canberra"],
    "reveal_color": "",
    "choices": list(CHOICES),
}


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Generation shape
# ---------------------------------------------------------------------------

def _reasoning_with_queue(seen: list, raws: list[str]) -> LilyReasoning:
    """LilyReasoning with Grok transport stubbed to record calls and pop one
    canned response per call."""
    r = LilyReasoning.__new__(LilyReasoning)
    r._model = "test-reasoning-model"
    r._vocal_model = "test-vocal-model"
    queue = list(raws)

    async def _fake_generate(model, prompt, thinking_level, **kwargs):
        seen.append({"model": model, "prompt": prompt, **kwargs})
        return queue.pop(0)

    async def _fake_grok(prompt, **kwargs):
        seen.append({"prompt": prompt, **kwargs})
        return queue.pop(0)

    r._generate = _fake_generate
    r._generate_grok_json = _fake_grok
    return r


def test_mc_generation_prompt_demands_four_choices():
    seen: list = []
    r = _reasoning_with_queue(seen, [json.dumps(MC_QUESTION)])
    q = _run(r.generate_question("academic", 2, "general", [], multiple_choice=True))
    assert q is not None and q["choices"] == CHOICES
    prompt = seen[0]["prompt"]
    assert "MULTIPLE-CHOICE" in prompt
    assert "EXACTLY 4" in prompt
    assert "VERBATIM" in prompt                 # canonical among the 4
    assert "comically wrong" in prompt          # pub-convention laugh option
    assert "RANDOMIZE" in prompt                # order randomized


def test_freeform_generation_prompt_has_no_mc_addendum():
    seen: list = []
    freeform = {k: v for k, v in MC_QUESTION.items() if k != "choices"}
    r = _reasoning_with_queue(seen, [json.dumps(freeform)])
    q = _run(r.generate_question("academic", 2, "general", []))
    assert q is not None and "choices" not in q
    assert "MULTIPLE-CHOICE" not in seen[0]["prompt"]


def test_valid_choices_shape_check():
    assert lily_valid_choices(MC_QUESTION) is True
    bad = dict(MC_QUESTION)
    bad["choices"] = CHOICES[:3]
    assert lily_valid_choices(bad) is False           # not 4
    bad["choices"] = ["Canberra", "Canberra", "Sydney", "Melbourne"]
    assert lily_valid_choices(bad) is False           # duplicate
    bad["choices"] = ["Sydney", "Melbourne", "Perth", "Darwin"]
    assert lily_valid_choices(bad) is False           # canonical missing
    bad["choices"] = ["", "Sydney", "Melbourne", "Canberra"]
    assert lily_valid_choices(bad) is False           # empty option
    assert lily_valid_choices({"canonical_answer": "x"}) is False
    assert lily_valid_choices(None) is False


def test_bank_question_gets_three_synthesized_distractors_at_prefetch():
    bank = {
        "id": "kb_0007",
        "prompt": "Name the capital city of Australia.",
        "canonical_answer": "Canberra",
        "acceptable_answers": ["canberra"],
    }
    seen: list = []
    r = _reasoning_with_queue(seen, [json.dumps(
        {"distractors": ["Sydney", "Melbourne", "SpongeBob's pineapple"]}
    )])
    sk = LilyScorekeeper("test-room")
    q = _run(r.prefetch_question(
        sk, category="academic", difficulty_tier=2,
        avoid_questions=[], from_bank=bank, multiple_choice=True,
    ))
    assert q is bank
    assert lily_valid_choices(q) is True
    assert sorted(q["choices"]) == sorted(
        ["Canberra", "Sydney", "Melbourne", "SpongeBob's pineapple"]
    )
    # Synthesis ran on the reasoning node — exactly one call, MC schema.
    assert len(seen) == 1
    assert seen[0]["model"] == "grok-4.5"
    assert seen[0]["effort"] == "medium"


def test_bank_question_freeform_prefetch_never_synthesizes():
    bank = {"prompt": "p?", "canonical_answer": "a", "acceptable_answers": ["a"]}
    seen: list = []
    r = _reasoning_with_queue(seen, [])
    sk = LilyScorekeeper("test-room")
    q = _run(r.prefetch_question(
        sk, category="academic", difficulty_tier=2,
        avoid_questions=[], from_bank=bank,
    ))
    assert q is bank and "choices" not in q
    assert seen == []  # no LLM call at all


def test_synthesis_failure_degrades_to_freeform():
    bank = {"prompt": "p?", "canonical_answer": "a", "acceptable_answers": ["a"]}
    seen: list = []
    r = _reasoning_with_queue(seen, ["no json here at all"])
    sk = LilyScorekeeper("test-room")
    q = _run(r.prefetch_question(
        sk, category="academic", difficulty_tier=2,
        avoid_questions=[], from_bank=bank, multiple_choice=True,
    ))
    assert q is bank
    assert "choices" not in q  # honest degradation, not a broken 3-option read


def test_generated_mc_question_with_invalid_choices_gets_synthesis_fallback():
    generated = dict(MC_QUESTION)
    generated["choices"] = ["Sydney", "Melbourne", "Perth", "Darwin"]  # no canon
    seen: list = []
    r = _reasoning_with_queue(seen, [
        json.dumps(generated),                              # generation
        json.dumps({"verdict": "pass", "reason": "ok"}),    # verification
        json.dumps({"distractors": ["Sydney", "Melbourne", "Perth"]}),
    ])
    sk = LilyScorekeeper("test-room")
    q = _run(r.prefetch_question(
        sk, category="academic", difficulty_tier=2,
        avoid_questions=[], multiple_choice=True,
    ))
    assert q is not None
    assert lily_valid_choices(q) is True
    assert "Canberra" in q["choices"]


def test_verification_correction_swaps_stale_choice_in_place():
    generated = dict(MC_QUESTION)
    generated["choices"] = ["Canberra", "Sydney", "Melbourne", "Perth"]
    seen: list = []
    r = _reasoning_with_queue(seen, [
        json.dumps(generated),
        json.dumps({
            "verdict": "fail", "reason": "off by one",
            "corrected_canonical_answer": "Greater Canberra",
        }),
    ])
    sk = LilyScorekeeper("test-room")
    q = _run(r.prefetch_question(
        sk, category="academic", difficulty_tier=2,
        avoid_questions=[], multiple_choice=True,
    ))
    assert q is not None
    assert q["canonical_answer"] == "Greater Canberra"
    # The stale "Canberra" entry was swapped in place — order preserved,
    # no third LLM call needed.
    assert q["choices"] == ["Greater Canberra", "Sydney", "Melbourne", "Perth"]
    assert len(seen) == 2


# ---------------------------------------------------------------------------
# Tier-1 MC matching
# ---------------------------------------------------------------------------

def test_mc_letter_match_correct():
    r = lily_tier1_evaluate_mc("A", CHOICES, CANONICAL)
    assert r["verdict"] == "correct"
    assert r["selected_index"] == 0
    assert r["method"] == "letter"


def test_mc_letter_match_wrong_is_definitive_incorrect():
    r = lily_tier1_evaluate_mc("B", CHOICES, CANONICAL)
    assert r["verdict"] == "incorrect"
    assert r["selected_index"] == 1
    assert r["matched_answer"] == "Sydney"


def test_mc_letter_b_spoken_forms():
    for spoken in ("letter b", "option B.", "is it b", "I think it's B"):
        r = lily_tier1_evaluate_mc(spoken, CHOICES, CANONICAL)
        assert r["selected_index"] == 1, spoken
        assert r["method"] == "letter", spoken


def test_mc_bare_a_is_letter_not_article():
    # lily_normalize_answer drops articles; the MC parser must keep a bare
    # "a" alive as the letter A.
    r = lily_tier1_evaluate_mc("a", CHOICES, CANONICAL)
    assert r["selected_index"] == 0
    assert r["method"] == "letter"
    assert r["verdict"] == "correct"


def test_mc_positional_the_second_one():
    r = lily_tier1_evaluate_mc("the second one", CHOICES, CANONICAL)
    assert r["selected_index"] == 1
    assert r["method"] == "positional"
    assert r["verdict"] == "incorrect"


def test_mc_positional_forms():
    assert lily_tier1_evaluate_mc("first", CHOICES, CANONICAL)["selected_index"] == 0
    assert lily_tier1_evaluate_mc("the last one", CHOICES, CANONICAL)["selected_index"] == 3
    assert lily_tier1_evaluate_mc("number three", CHOICES, CANONICAL)["selected_index"] == 2
    assert lily_tier1_evaluate_mc("the fourth option", CHOICES, CANONICAL)["selected_index"] == 3


def test_mc_option_text_exact_and_hedged():
    r = lily_tier1_evaluate_mc("Canberra", CHOICES, CANONICAL)
    assert r["verdict"] == "correct" and r["method"] == "choice_text"
    r = lily_tier1_evaluate_mc("um, I think it's Sydney?", CHOICES, CANONICAL)
    assert r["verdict"] == "incorrect" and r["selected_index"] == 1


def test_mc_option_text_fuzzy_stt_mangling():
    r = lily_tier1_evaluate_mc("Canbera", CHOICES, CANONICAL)
    assert r["verdict"] == "correct"
    assert r["method"] == "choice_text"


def test_mc_article_answer_not_swallowed_by_letter_parser():
    # "a dolphin" must match the option text, never read as letter A.
    choices = ["a dolphin", "a whale", "a shark", "a rubber duck"]
    r = lily_tier1_evaluate_mc("a dolphin", choices, "a dolphin")
    assert r["verdict"] == "correct"
    assert r["method"] == "choice_text"


def test_mc_mumble_stays_tier2():
    r = lily_tier1_evaluate_mc("the capital one you know", CHOICES, CANONICAL)
    assert r["verdict"] == "uncertain"
    assert r["selected_index"] is None
    r = lily_tier1_evaluate_mc("", CHOICES, CANONICAL)
    assert r["verdict"] == "uncertain"


def test_mc_broken_sheet_escalates_never_rules():
    # Canonical answer absent from the choices: a selection must escalate,
    # never rule definitively off a malformed question.
    r = lily_tier1_evaluate_mc("B", ["w", "x", "y", "z"], "Canberra")
    assert r["verdict"] == "uncertain"


def test_question_dispatch_by_format():
    mc = lily_tier1_evaluate_question("the second one", MC_QUESTION)
    assert mc["verdict"] == "incorrect"
    freeform = {k: v for k, v in MC_QUESTION.items() if k != "choices"}
    ff = lily_tier1_evaluate_question("the second one", freeform)
    assert ff["verdict"] == "uncertain"  # freeform Tier 1 never rejects


def test_canonical_choice_index():
    assert lily_canonical_choice_index(CHOICES, "canberra") == 0
    assert lily_canonical_choice_index(CHOICES, "The Canberra!") == 0
    assert lily_canonical_choice_index(CHOICES, "Perth") is None
    assert lily_canonical_choice_index([], "x") is None


# ---------------------------------------------------------------------------
# 50/50 elimination
# ---------------------------------------------------------------------------

def test_fifty_fifty_eliminates_two_wrong_keeps_canonical():
    for seed in range(12):
        eliminated = lily_fifty_fifty_eliminations(
            CHOICES, CANONICAL, rng=random.Random(seed)
        )
        assert len(eliminated) == 2
        assert 0 not in eliminated          # canonical index survives
        assert eliminated == sorted(eliminated)
        assert all(0 <= i <= 3 for i in eliminated)


def test_fifty_fifty_never_eliminates_blind():
    # Canonical not among the choices — refuse rather than guess.
    assert lily_fifty_fifty_eliminations(["w", "x", "y", "z"], "Canberra") == []
    assert lily_fifty_fifty_eliminations(CHOICES[:3], CANONICAL) == []
    assert lily_fifty_fifty_eliminations([], CANONICAL) == []


# ---------------------------------------------------------------------------
# round_format flag: schedule, override, snapshot, state block
# ---------------------------------------------------------------------------

def test_default_schedule_one_mc_round():
    sk = LilyScorekeeper("test-room")
    assert sk.round_format == "freeform"
    assert sk.format_for_round(1) == "freeform"
    assert sk.format_for_round(DEFAULT_MC_ROUND) == "multiple_choice"
    assert sk.format_for_round(3) == "freeform"
    assert sk.format_for_round(4) == "freeform"  # wager round stays open


def test_apply_round_format_follows_schedule():
    sk = LilyScorekeeper("test-room")
    sk.apply_round_format_for_round(1)
    assert sk.round_format == "freeform"
    sk.apply_round_format_for_round(2)
    assert sk.round_format == "multiple_choice"
    sk.apply_round_format_for_round(3)
    assert sk.round_format == "freeform"


def test_set_round_format_overrides_schedule_and_sticks():
    sk = LilyScorekeeper("test-room")
    assert sk.set_round_format("multiple_choice") is True
    assert sk.round_format == "multiple_choice"
    # The override is sticky across round boundaries until changed.
    sk.apply_round_format_for_round(3)
    assert sk.round_format == "multiple_choice"
    assert sk.set_round_format("freeform") is True
    sk.apply_round_format_for_round(DEFAULT_MC_ROUND)
    assert sk.round_format == "freeform"  # explicit ask beats the schedule


def test_set_round_format_rejects_unknown():
    sk = LilyScorekeeper("test-room")
    assert sk.set_round_format("karaoke") is False
    assert sk.round_format == "freeform"
    assert sk.round_format_override is None


def test_round_format_snapshot_and_rehydrate():
    sk = LilyScorekeeper("test-room")
    sk.set_round_format("multiple_choice")
    snap = sk.snapshot()
    assert snap["round_format"] == "multiple_choice"
    assert snap["round_format_override"] == "multiple_choice"
    sk2 = LilyScorekeeper("test-room-2")
    sk2.rehydrate(snap)
    assert sk2.round_format == "multiple_choice"
    assert sk2.round_format_override == "multiple_choice"
    # Old snapshots (pre-WO) rehydrate to the defaults, not a crash.
    sk3 = LilyScorekeeper("test-room-3")
    sk3.rehydrate({"phase": "round", "round": 1})
    assert sk3.round_format == "freeform"
    assert sk3.round_format_override is None


def test_state_block_carries_format_line():
    sk = LilyScorekeeper("test-room")
    assert "format=freeform" in sk.build_state_block()
    sk.set_round_format("multiple_choice")
    assert "format=multiple_choice" in sk.build_state_block()


# ---------------------------------------------------------------------------
# Agent seam: metadata carries choices/eliminated; the two tools
# ---------------------------------------------------------------------------

class _FakeSession:
    def __init__(self) -> None:
        self.instructions: list[str] = []

    def generate_reply(self, instructions: str) -> None:
        self.instructions.append(instructions)


class _FakeAgentHandle:
    def set_preemptive_generation(self, enabled: bool) -> None:
        pass


class _FakeRoomAPI:
    def __init__(self) -> None:
        self.requests: list = []

    async def update_room_metadata(self, req) -> None:
        self.requests.append(req)


class _FakeCtx:
    def __init__(self) -> None:
        self.api = type("API", (), {"room": _FakeRoomAPI()})()
        self.room = type("Room", (), {"name": "test-room"})()


def _make_game() -> LilyGame:
    game = LilyGame.__new__(LilyGame)
    game.ctx = _FakeCtx()
    game.session = _FakeSession()
    game.agent = _FakeAgentHandle()
    game._preemptive_paused = False
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game.sk = LilyScorekeeper("test-room")
    game.memory_block = ""
    game.reconnected = False
    game.game_started = False
    game.game_over = False
    game.armed_question = None
    game.next_question = None
    game.eliminated = []
    game.used_prompts = []
    game.supabase = None
    game._window_timer = None
    game._bed_handle = None
    game._pending_unbound_award = None
    game.acoustic = lily_audeering_consumers.LilyAcousticState()
    return game


def _published(game: LilyGame) -> dict:
    req = game.ctx.api.room.requests[-1]
    return json.loads(req.metadata)


def test_publish_metadata_carries_choices_and_eliminated():
    game = _make_game()
    _run(game.publish_metadata(
        MC_QUESTION["prompt"], choices=CHOICES, eliminated=[1, 3]
    ))
    doc = _published(game)
    assert doc["question"] == MC_QUESTION["prompt"]
    assert doc["choices"] == CHOICES              # exactly the 4 strings
    assert doc["eliminated"] == [1, 3]            # indices into choices
    # Existing document shape untouched:
    assert doc["reveal"] == {"answer": "", "winner": None, "correct": False}
    assert doc["wager"] is False


def test_publish_metadata_omits_mc_keys_for_freeform():
    game = _make_game()
    _run(game.publish_metadata("An open question?"))
    doc = _published(game)
    assert "choices" not in doc
    assert "eliminated" not in doc


def test_publish_metadata_defaults_eliminated_to_empty_with_choices():
    game = _make_game()
    _run(game.publish_metadata(MC_QUESTION["prompt"], choices=CHOICES))
    doc = _published(game)
    assert doc["choices"] == CHOICES
    assert doc["eliminated"] == []


def _call_tool(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_set_round_format_tool_sets_override():
    agent = LilyAgent.__new__(LilyAgent)
    game = _make_game()
    agent._game = game
    result = _call_tool(
        LilyAgent.lily_set_round_format.__wrapped__(agent, None, "multiple choice")
    )
    assert "multiple_choice" in result
    assert game.sk.round_format == "multiple_choice"
    assert game.sk.round_format_override == "multiple_choice"


def test_set_round_format_tool_rejects_unknown():
    agent = LilyAgent.__new__(LilyAgent)
    game = _make_game()
    agent._game = game
    result = _call_tool(
        LilyAgent.lily_set_round_format.__wrapped__(agent, None, "karaoke")
    )
    assert "Unknown format" in result
    assert game.sk.round_format == "freeform"


def test_set_round_format_freeform_strips_undelivered_choices():
    agent = LilyAgent.__new__(LilyAgent)
    game = _make_game()
    agent._game = game
    game.sk.set_round_format("multiple_choice")
    game.armed_question = dict(MC_QUESTION)
    game.next_question = dict(MC_QUESTION)
    game.eliminated = [1, 3]
    result = _call_tool(
        LilyAgent.lily_set_round_format.__wrapped__(agent, None, "freeform")
    )
    assert "freeform" in result
    assert "choices" not in game.armed_question   # undelivered: safe to reshape
    assert "choices" not in game.next_question
    assert game.eliminated == []


def test_set_round_format_freeform_leaves_delivered_question_alone():
    agent = LilyAgent.__new__(LilyAgent)
    game = _make_game()
    agent._game = game
    game.armed_question = dict(MC_QUESTION)
    game.sk.question_number = 3
    game.say_registry.claim("q_3_delivery")  # already read aloud
    _call_tool(LilyAgent.lily_set_round_format.__wrapped__(agent, None, "freeform"))
    assert game.armed_question["choices"] == CHOICES  # mid-flight question intact


def test_fifty_fifty_tool_commits_elimination_and_publishes():
    agent = LilyAgent.__new__(LilyAgent)
    game = _make_game()
    agent._game = game
    game.game_started = True
    game.sk.bind_speaker("S1", "Sarah")
    game.armed_question = dict(MC_QUESTION)
    game.sk.current_question = game.armed_question
    result = _call_tool(
        LilyAgent.lily_use_fifty_fifty.__wrapped__(agent, None, "Sarah")
    )
    assert "50/50 committed for Sarah" in result
    assert "Cross exactly those two out aloud" in result
    assert len(game.eliminated) == 2
    assert 0 not in game.eliminated  # canonical (index 0) survives
    # The eliminated options are named with their letters in the result.
    for i in game.eliminated:
        assert CHOICES[i] in result
        assert lily_evaluation.MC_CHOICE_LETTERS[i] in result
    # Seam: eliminated indices rode the metadata publish.
    doc = _published(game)
    assert doc["choices"] == CHOICES
    assert doc["eliminated"] == game.eliminated
    # Lifeline consumed.
    assert game.sk.players["Sarah"]["lifeline_available"] is False


def test_fifty_fifty_tool_single_use_per_player():
    agent = LilyAgent.__new__(LilyAgent)
    game = _make_game()
    agent._game = game
    game.game_started = True
    game.sk.bind_speaker("S1", "Sarah")
    game.armed_question = dict(MC_QUESTION)
    game.sk.current_question = game.armed_question
    _call_tool(LilyAgent.lily_use_fifty_fifty.__wrapped__(agent, None, "Sarah"))
    game.eliminated = []  # next MC question
    result = _call_tool(
        LilyAgent.lily_use_fifty_fifty.__wrapped__(agent, None, "Sarah")
    )
    assert "already used" in result


def test_fifty_fifty_tool_refuses_on_freeform_without_spending():
    agent = LilyAgent.__new__(LilyAgent)
    game = _make_game()
    agent._game = game
    game.game_started = True
    game.sk.bind_speaker("S1", "Sarah")
    game.armed_question = {
        "prompt": "Open question?", "canonical_answer": "x",
    }
    game.sk.current_question = game.armed_question
    result = _call_tool(
        LilyAgent.lily_use_fifty_fifty.__wrapped__(agent, None, "Sarah")
    )
    assert "NOT" in result
    assert game.sk.players["Sarah"]["lifeline_available"] is True  # kept


def test_fifty_fifty_tool_gates_and_refunds():
    agent = LilyAgent.__new__(LilyAgent)
    game = _make_game()
    agent._game = game
    # Before game start:
    result = _call_tool(
        LilyAgent.lily_use_fifty_fifty.__wrapped__(agent, None, "Sarah")
    )
    assert "hasn't started" in result
    # Unrostered player:
    game.game_started = True
    result = _call_tool(
        LilyAgent.lily_use_fifty_fifty.__wrapped__(agent, None, "Nobody")
    )
    assert "No rostered player" in result
    # Broken sheet (canonical missing from choices): refund, never blind.
    game.sk.bind_speaker("S1", "Sarah")
    game.armed_question = dict(MC_QUESTION)
    game.armed_question["choices"] = ["w", "x", "y", "z"]
    game.sk.current_question = game.armed_question
    result = _call_tool(
        LilyAgent.lily_use_fifty_fifty.__wrapped__(agent, None, "Sarah")
    )
    assert "NOT" in result
    assert game.sk.players["Sarah"]["lifeline_available"] is True


def test_adjudication_dispatch_mc_incorrect_is_tier1_final():
    """The adjudicate loop's per-candidate evaluation (via
    lily_tier1_evaluate_question) must treat an MC wrong pick as decided —
    not escalate it to the judge like a freeform miss."""
    t1 = lily_tier1_evaluate_question("letter d", MC_QUESTION)
    assert t1["verdict"] == "incorrect"          # definitive: no Tier-2
    t1 = lily_tier1_evaluate_question("hmm the capital", MC_QUESTION)
    assert t1["verdict"] == "uncertain"          # mumble: Tier-2 territory
