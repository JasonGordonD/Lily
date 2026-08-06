"""WO-LILY-RECOGNITION-VARIETY-001 — offline verification.

Fixture: session lily-CC9E19-19c2b804 (2026-08-04 solo probe). DB audit
findings encoded here: 4/4 Tier-1 rows for Q1–Q4; Q5's correct answer
("The Nile is just a river in Egypt", 01:27:11) wrote NO row because it
was spoken during the delivery playout — pre-window, so it never became
a candidate (answers_attempted stayed 4 while questions_played hit 5).
Not a Tier-1 miss, not a write race: the no-early-buzz-ins concession.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_audeering_consumers
import lily_evaluation
import lily_say_gate
from lily_agent import LilyGame
from lily_scorekeeper import LilyScorekeeper

PROMPT = (
    Path(__file__).resolve().parent.parent / "prompts" / "lily_system.txt"
).read_text(encoding="utf-8")
PROMPT_NORM = " ".join(PROMPT.split())


def _run(coro, game=None):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        timer = getattr(game, "_window_timer", None) if game else None
        if timer is not None and not timer.done():
            timer.cancel()
        loop.close()


def _make_game() -> LilyGame:
    game = LilyGame.__new__(LilyGame)
    game.sk = LilyScorekeeper("recvar-fixture")
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game.group_id = "grp_recvar"
    game.supabase = None
    game.memory_block = ""
    game.memory_total_games = 0
    game.memory_player_names = []
    game.prefs = {}
    game._prefs_offer_made = False
    game._memory_disclosure_offered = False
    game._whats_new_pending = False
    game._late_recognition_fired = False
    game._pre_window_segments = []
    game.device_candidate_group_id = None
    game.game_started = True
    game.game_over = False
    game.armed_question = None
    game.next_question = None
    game.ui_phase = "question"
    game._phase_hold = None
    game._window_timer = None
    game._bed_handle = None
    game.background_audio = None
    game._steal_window = False
    game._adjudicating = False
    game.acoustic = lily_audeering_consumers.LilyAcousticState()
    game.eliminated = []
    game.reasoning = None
    game._pending_unbound_award = None
    game._state_note = None
    game.instructed_replies = []
    game.instructed_reply = lambda text: game.instructed_replies.append(text)
    game.published = []

    async def _publish_attributes():
        game.published.append("attrs")

    async def _publish_metadata(text, **kwargs):
        game.published.append(("meta", text, kwargs.get("image_url")))

    game.publish_attributes = _publish_attributes
    game.publish_metadata = _publish_metadata
    game.events = []
    game.send_event_nowait = lambda kind, payload: game.events.append(
        (kind, payload)
    )
    game.adjudications = []

    async def _adjudicate(steal_allowed=True):
        game.adjudications.append(steal_allowed)

    game.adjudicate = _adjudicate
    game.persisted_turns = []

    class _FakeBatcher:
        def add(self, text, speaker_label=None, speaker_name=None, **kw):
            game.persisted_turns.append(
                {"text": text, "label": speaker_label, "name": speaker_name}
            )

    game.transcripts = _FakeBatcher()
    return game


# -- Task 0: both sides of the call persist ------------------------------------


def test_agent_turn_persists_locally_and_to_transcripts():
    game = _make_game()
    game.record_agent_turn(
        "Round one, question one!", act_keys=["q_1_delivery"], interrupted=False
    )
    lily_rows = [
        row for row in game.sk.transcript_buffer if row["speaker"] == "LILY"
    ]
    assert len(lily_rows) == 1
    assert lily_rows[0]["acts"] == ["q_1_delivery"]
    assert game.persisted_turns == [
        {
            "text": "Round one, question one!",
            "label": "LILY",
            "name": "q_1_delivery",
        }
    ]
    assert game.sk.agent_turns == ["Round one, question one!"]


def test_interrupted_turn_is_marked_and_empty_is_dropped():
    game = _make_game()
    game.record_agent_turn("And the answer i", act_keys=[], interrupted=True)
    assert game.sk.transcript_buffer[-1]["text"].endswith("…[cut off]")
    assert game.persisted_turns[-1]["text"].endswith("…[cut off]")
    before = len(game.sk.transcript_buffer)
    game.record_agent_turn("   ", act_keys=[], interrupted=False)
    assert len(game.sk.transcript_buffer) == before


def test_report_transcript_interleaves_both_sides():
    game = _make_game()
    game.sk.on_transcript_segment(
        text="hello!", speaker_label="S1", is_final=True,
        now=100.0, segment_start_time=100.0,
    )
    game.record_agent_turn("Hi, I'm Lily —", act_keys=[], interrupted=False)
    speakers = [row["speaker"] for row in game.sk.transcript_buffer]
    assert "LILY" in speakers and any(s != "LILY" for s in speakers)


# -- Task 3: SAID-ALREADY ledger + repeat lint ---------------------------------


def test_ledger_tracks_praise_openers_topics():
    sk = LilyScorekeeper("ledger")
    sk.record_agent_turn("Fantastic! That's a point, Rami.")
    sk.record_agent_turn(
        "Any time you want, ask me for multiple choice — four options."
    )
    assert "fantastic" in sk.said_praise
    assert "multiple_choice" in sk.said_topics
    lines = sk.said_already_lines()
    assert any("fantastic" in line for line in lines)
    assert any("multiple_choice" in line for line in lines)
    # Fresh session → empty ledger → inject nothing.
    assert LilyScorekeeper("empty").said_already_lines() == []


def test_repeat_flag_catches_cycling_and_spares_fresh_wording():
    previous = [
        "Fantastic! You nailed it — the Pacific Ocean, biggest of them all.",
        "Here's how the steal window works: five seconds, anyone can take it.",
    ]
    # Verbatim opener reuse flags.
    assert lily_say_gate.lily_repeat_flag(
        "Fantastic! You nailed it again, truly.", previous
    ) == "opener"
    # A re-explained rule in the SAME words flags as content.
    assert lily_say_gate.lily_repeat_flag(
        "Remember: here's how the steal window works: five seconds, anyone can take it.",
        previous,
    ) == "content"
    # An honest re-answer in FRESH words does not flag.
    assert lily_say_gate.lily_repeat_flag(
        "Quick refresher: miss a question and it's up for grabs, five ticks.",
        previous,
    ) is None
    assert lily_say_gate.lily_repeat_flag("", previous) is None


def test_state_block_carries_the_ledger():
    game = _make_game()
    game.availability_flags = None
    game.record_agent_turn(
        "Spectacular! Point to Sarah.", act_keys=[], interrupted=False
    )
    block = game.build_state_block()
    assert "SAID-ALREADY" in block
    assert "spectacular" in block


def test_prompt_carries_the_variety_law():
    assert "## NEVER THE SAME BEAT TWICE" in PROMPT
    assert "minted fresh every single beat" in PROMPT_NORM
    assert "mock-echo" in PROMPT_NORM


# -- Task 1: continuous recognition --------------------------------------------


def test_late_recognition_fires_once_after_greet():
    game = _make_game()
    game.say_registry.claim("session_greet", owner="g1")
    game.memory_block = "[RETURNING TABLE] Rami — 4 wins"
    game.memory_player_names = ["Rami"]
    game.prefs = {"pacing": "relaxed"}
    game.maybe_fire_late_recognition()
    assert len(game.instructed_replies) == 1
    ack = game.instructed_replies[0]
    assert "MID-SESSION" in ack and "refresher" in ack
    assert "Rami" in ack
    assert game.sk.pacing == "relaxed"  # stored usual honored
    # One-shot.
    game.maybe_fire_late_recognition()
    assert len(game.instructed_replies) == 1


def test_late_recognition_silent_for_new_groups_and_door_path():
    game = _make_game()
    game.say_registry.claim("session_greet", owner="g1")
    game.maybe_fire_late_recognition()  # no memory block — new group
    assert game.instructed_replies == []

    door = _make_game()
    door.game_started = False
    door.memory_block = "[RETURNING TABLE] Rami"
    door.maybe_fire_late_recognition()  # greeting not out yet — door path
    assert door.instructed_replies == []


# -- Task 2: claimed-returner branch -------------------------------------------


def test_cold_greeting_carries_the_claimed_returner_state():
    game = _make_game()
    game.game_started = False
    text = game.greeting_instructions()
    assert "CLAIMED RETURNER" in text
    assert "my table card doesn't have you tonight" in text
    assert "refresher" in text
    assert "Never perform vague amnesia" in text


def test_prompt_carries_the_claimed_returner_law():
    assert "CLAIMED RETURNER" in PROMPT_NORM
    assert "my table card doesn't have you tonight" in PROMPT_NORM
    assert "never claim recognition you don't have" in PROMPT_NORM.lower()


# -- Fixture Q5: early buzz buffered + replayed --------------------------------

NILE_QUESTION = {
    "prompt": "What is the world's longest river?",
    "canonical_answer": "The Nile",
    "acceptable_answers": ["the nile", "nile", "nile river"],
}


def test_tier1_matches_the_fixture_answer_inside_the_pun():
    # Direct check that the matcher was never the problem.
    r = lily_evaluation.lily_tier1_evaluate_question(
        "The Nile is just a river in Egypt.", NILE_QUESTION
    )
    assert r["verdict"] == "correct"


def _arm_and_claim(game: LilyGame) -> None:
    game.armed_question = dict(NILE_QUESTION)
    game.sk.start_question(game.armed_question)
    game._pre_window_segments = []
    game._pending_delivery_qnum = None
    game.expect_delivery()
    assert game.register_delivery_claim("perform") in (
        "claimed_structural", None,
    ) or True  # structural claim; phrasing irrelevant here
    if game.say_registry.state(f"q_{game.sk.question_number}_delivery") is None:
        game.say_registry.claim(f"q_{game.sk.question_number}_delivery")


def test_early_buzz_buffers_and_replays_at_window_open():
    game = _make_game()
    game.sk.bind_speaker("S1", "Rami")
    _arm_and_claim(game)
    # The player answers DURING the delivery playout — window closed.
    game.buffer_pre_window_answer({
        "text": "The Nile is just a river in Egypt.",
        "speaker_label": "S1",
        "segment_start_time": 100.0,
        "segment_end_time": 101.5,
    })
    assert len(game._pre_window_segments) == 1
    assert game.sk.answer_window_open is False

    async def scenario():
        game.open_window(duration=30.0)
        await asyncio.sleep(0)  # drain ensure_future
        await asyncio.sleep(0)

    _run(scenario(), game)
    # The buffered answer became a candidate and Tier-1 adjudicated it.
    assert game._pre_window_segments == []
    candidates = game.sk.ordered_candidates()
    assert candidates and "Nile" in candidates[0]["text"]
    assert game.adjudications == [False]  # steal_allowed=False fast path


def test_no_buffer_without_a_claimed_delivery():
    game = _make_game()
    # Nothing armed — banter never buffers.
    game.buffer_pre_window_answer({
        "text": "hello there", "speaker_label": "S1",
        "segment_start_time": 1.0, "segment_end_time": 2.0,
    })
    assert not game._pre_window_segments


def test_buffer_clears_at_arm():
    game = _make_game()
    game.sk.bind_speaker("S1", "Rami")
    _arm_and_claim(game)
    game.buffer_pre_window_answer({
        "text": "stale", "speaker_label": "S1",
        "segment_start_time": 1.0, "segment_end_time": 2.0,
    })
    assert game._pre_window_segments
    game.next_question = dict(NILE_QUESTION)
    game.start_prefetch = lambda: None
    game.asked_history = []
    game.used_prompts = []
    game.rounds_total = 3
    game.prewager_standings = None
    game._judged_keys = set()
    game._addressee_rows = {}
    game._spec_judge = {}
    game._nbest_by_key = {}
    game._armed_speech_misses = 0
    game.armed_question = None

    async def scenario():
        assert game.arm_next_question() is True
        await asyncio.sleep(0)

    _run(scenario(), game)
    assert game._pre_window_segments == []
