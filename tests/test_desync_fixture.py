"""WO-LILY-DESYNC-HONESTY-001 — the desync/honesty regression fixture.

Two live sessions are the evidence base and enter the suite here:

* 2026-07-15 01:33 — questions 1–3 delivered conversationally but never
  registered: `LILY_WINDOW | FALLBACK_OPEN` fired on q=2,3,5,6 with
  spoken/prompt ratios 0.00–0.15 ("paraphrased beyond recognition"), the
  pipeline forced an "official re-run" of already-answered questions
  ("you asked me that already" ×3), scores committed only on the re-run
  while Lily claimed the point was "safe and sound", and she invented
  mechanisms for the gap ("the digital board takes a second to refresh
  once I submit to the database" — false).

* 2026-07-15 22:54 (lily-BBD306-d2153aa7) — the engine reached q=5
  committing score=1/attempted=2 while Lily verbally ran a DIFFERENT
  quiz: her conversational asks never registered as deliveries, windows
  opened against wrong turns via the ratio fallback, and 2 of 3 correct
  spoken answers landed outside engine windows. At 23:04 she "confirmed"
  a screen misspelling that never existed and narrated "you should
  actually have three points" — ungrounded validation instead of
  speaking to published state.

Sub-agent B contract pinned here: delivery registration is STRUCTURAL —
the `q_{N}_delivery` claim is the delivery event (claimed at dispatch for
code-dispatched delivery turns; claimed by core-sentence performance for
organic turns); the window opens and the question marks delivered off the
claim, never off text similarity. The ratio matcher is telemetry only.
Zero FALLBACK_OPEN: a window can never again open on a question nobody
was delivered.

This file imports lily_agent (and therefore livekit) — same boundary
note as test_say_gate_dispatch.py.
"""

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_audeering_consumers
import lily_evaluation
import lily_say_gate
from lily_agent import WINDOW_FALLBACK_AGENT_TURNS, LilyGame
from lily_scorekeeper import (
    LilyScorekeeper,
    lily_detect_state_contradiction,
)


class _FakeSession:
    def __init__(self) -> None:
        self.instructions: list[str] = []

    def generate_reply(self, instructions: str) -> None:
        self.instructions.append(instructions)


class _FakeAgentHandle:
    def set_preemptive_generation(self, enabled: bool) -> None:
        pass


def _make_game() -> LilyGame:
    """Minimal LilyGame via __new__ — the attributes the delivery-claim /
    window-open / state-block paths touch (test_say_gate_dispatch pattern,
    extended for on_agent_speech_finished + open_window)."""
    game = LilyGame.__new__(LilyGame)
    game.session = _FakeSession()
    game.agent = _FakeAgentHandle()
    game._preemptive_paused = False
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game.sk = LilyScorekeeper("desync-fixture")
    game.memory_block = ""
    game.reconnected = False
    game.game_started = True
    game.game_over = False
    game.armed_question = None
    game.next_question = None
    game.used_prompts = []
    game.supabase = None
    game.ui_phase = "question"
    game._window_timer = None
    game._bed_handle = None
    game.background_audio = None
    game._steal_window = False
    game._adjudicating = False
    game._pending_reveal_event = None
    game._pending_unbound_award = None
    game._armed_speech_misses = 0
    game._pending_delivery_qnum = None
    game.eliminated = []
    game.acoustic = lily_audeering_consumers.LilyAcousticState()
    game.prefs = {}
    game._prefs_offer_made = False

    game.pending_clarify = {}
    game.forget_state = "idle"
    game.forget_requester = None
    game._state_note = None
    game._user_turn_index = 0
    game.promoted_categories = []

    # adjudicate-path attributes (Sub-agent E scenarios):
    game.rounds_total = 3
    game.asked_history = []
    game.group_id = "grp_desync"
    game.prewager_standings = None
    game.highlights = []
    game.reasoning = None  # Tier-1 decides; Tier-2 never reached here
    game._prefetch_task = None
    game._watchdog_task = None
    game._prefetch_stall_ticks = 0
    game._judged_keys = set()
    game._spec_judge = {}
    game._addressee_rows = {}
    game._forget_target_group = None

    game.metadata_publishes: list[str] = []
    game.attribute_publishes: list[bool] = []

    async def _publish_metadata(question_text, **kwargs):
        game.metadata_publishes.append(question_text or "")

    async def _publish_attributes():
        game.attribute_publishes.append(True)

    game.publish_metadata = _publish_metadata
    game.publish_attributes = _publish_attributes
    return game


def _arm(game: LilyGame, prompt: str, answer: str = "-") -> None:
    """Arm one question the way arm_next_question leaves state (the round
    arithmetic itself is pinned in test_round_loop)."""
    game.armed_question = {"prompt": prompt, "canonical_answer": answer}
    game.sk.start_question(game.armed_question)
    game._armed_speech_misses = 0
    game._pending_delivery_qnum = None
    game.ui_phase = "question"


def _run(coro, game: LilyGame | None = None):
    """Run one scenario; the window-expiry timer (if any) is cancelled
    INSIDE the loop so no task outlives it."""

    async def _wrapped():
        result = await coro
        if (
            game is not None
            and game._window_timer is not None
            and not game._window_timer.done()
        ):
            game._window_timer.cancel()
            await asyncio.sleep(0)
        return result

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_wrapped())
    finally:
        loop.close()


async def _drain():
    await asyncio.sleep(0)


# The 01:33 session's paraphrase class: Lily weaves the question into
# banter — the distinctive-token ratio lands at 0.00–0.15 and the old
# text matcher NEVER fires.
Q2_PROMPT = (
    "Which planet in our solar system holds the record for the most "
    "confirmed moons orbiting it?"
)
Q2_PARAPHRASE = (
    "Moon hoarding, my friends! Somebody out there in the dark keeps WAY "
    "too many of them — who is the hoarder of our little neighborhood?"
)
Q3_PROMPT = "Name the strait that separates Europe from Asia at Istanbul."
Q3_PARAPHRASE = (
    "Istanbul! Two continents, one city — what do we call that famous "
    "ribbon of water running right through the middle of it?"
)


def test_fixture_paraphrases_are_below_the_old_tiers():
    # Sanity-pin the fixture texts to the live evidence class: both fall
    # below the old paraphrase tier, so under the old design ONLY the
    # ghost fallback could have opened these windows.
    for prompt, spoken in (
        (Q2_PROMPT, Q2_PARAPHRASE),
        (Q3_PROMPT, Q3_PARAPHRASE),
    ):
        ratio = lily_evaluation.lily_question_spoken_ratio(prompt, spoken)
        assert ratio < lily_evaluation.QUESTION_SPOKEN_PARAPHRASE_RATIO


# -- structural claims: the q2/q3 replay --------------------------------------


def test_q2_replay_structural_claim_registers_delivery(caplog):
    # The code-dispatched delivery turn (question nudge / begin_round
    # post-tool / skip follow-up): the 01:33 q2 paraphrase drifts from
    # the sheet, so WS-1 strict registration rewrites it to the
    # deterministic sheet before claiming (never a silent claim on a
    # paraphrase); the window opens off the claim at the sheet turn's
    # playout, and FALLBACK_OPEN never fires.
    game = _make_game()
    _arm(game, Q2_PROMPT)

    async def scenario():
        game.expect_delivery()  # the structural dispatch signal
        assert game.register_delivery_claim(Q2_PARAPHRASE) == "rewrite_strict"
        assert game.say_registry.state("q_1_delivery") is None
        # tts_node rewrite protocol: re-arm, speak the sheet, claim.
        game.expect_delivery()
        sheet = game.rendered_armed_question()
        verdict = game.register_delivery_claim(sheet)
        assert verdict == "claimed_structural"
        assert game.say_registry.state("q_1_delivery") is not None
        assert not game.sk.answer_window_open  # opens at playout, not claim
        with caplog.at_level(logging.INFO):
            game.on_agent_speech_finished(sheet)
        await _drain()

    _run(scenario(), game)
    assert game.sk.answer_window_open is True
    assert game.say_registry.state("q_1_delivery") == lily_say_gate.CLAIM_CONFIRMED
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "FALLBACK_OPEN" not in joined
    assert "reason=delivery_claim" in joined
    # The question reached the glass off the claim publish:
    assert Q2_PROMPT in game.metadata_publishes



def test_q3_replay_no_ghost_window_then_nudged_delivery(caplog):
    # The q3 shape with NO structural dispatch: organic banter-weave below
    # any recognizable performance. The window must NOT open on those
    # turns (the old fallback opened it — the ghost game); instead, after
    # WINDOW_FALLBACK_AGENT_TURNS finished turns, ONE structural delivery
    # nudge dispatches; a nudged turn that still paraphrases is rewritten
    # to the sheet (WS-1), the sheet claims, and the window opens on a
    # registered delivery. Zero FALLBACK_OPEN, zero re-runs.
    game = _make_game()
    _arm(game, Q3_PROMPT)

    async def scenario():
        with caplog.at_level(logging.INFO):
            for _ in range(WINDOW_FALLBACK_AGENT_TURNS):
                assert game.register_delivery_claim(Q3_PARAPHRASE) is None
                game.on_agent_speech_finished(Q3_PARAPHRASE)
            # Ghost window never opened on unregistered turns:
            assert game.sk.answer_window_open is False
            # ...but the pipeline did not stall: ONE delivery nudge went out.
            assert len(game.session.instructions) == 1
            assert "exactly as written" in game.session.instructions[0]
            # The nudged turn still paraphrased — strict registration
            # rewrites it to the sheet before any claim (WS-1):
            assert game.register_delivery_claim(Q3_PARAPHRASE) == (
                "rewrite_strict"
            )
            game.expect_delivery()
            sheet = game.rendered_armed_question()
            assert game.register_delivery_claim(sheet) == "claimed_structural"
            game.on_agent_speech_finished(sheet)
        await _drain()

    _run(scenario(), game)
    assert game.sk.answer_window_open is True
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "FALLBACK_OPEN" not in joined
    assert "DELIVERY_NUDGE" in joined



def test_bbd306_wrong_quiz_never_opens_engine_window():
    # 22:54 session (lily-BBD306): Lily verbally ran a DIFFERENT quiz
    # while q=5 sat armed — the ratio fallback opened windows against
    # wrong turns and correct answers landed outside them. Now: an
    # invented question registers nothing and the engine window stays
    # shut until the ARMED question is actually delivered.
    game = _make_game()
    _arm(game, Q2_PROMPT)
    invented = (
        "Here is one for you: what year did the Berlin Wall come down? "
        "Think fast, my friends!"
    )

    async def scenario():
        assert game.register_delivery_claim(invented) is None
        game.on_agent_speech_finished(invented)
        assert game.sk.answer_window_open is False
        assert game.say_registry.state("q_1_delivery") is None
        await _drain()

    _run(scenario(), game)



# -- organic claims: the core-sentence contract --------------------------------


def test_organic_core_sentence_claims_delivery():
    # Flourish before and after, the core sentence whole (with TTS tags
    # riding along): the organic turn claims and the window opens at its
    # playout.
    game = _make_game()
    _arm(game, Q3_PROMPT)
    spoken = (
        "[excited] Round two, this table is ON FIRE. [pause] Name the "
        "strait that separates Europe from Asia, at Istanbul! "
        "[whispering] Think carefully."
    )

    async def scenario():
        verdict = game.register_delivery_claim(spoken)
        assert verdict == "claimed_core_sentence"
        game.on_agent_speech_finished(spoken)
        await _drain()

    _run(scenario(), game)
    assert game.sk.answer_window_open is True



def test_flourish_inside_the_core_sentence_does_not_claim():
    # The prompt contract is "flourish before and after, never inside":
    # a sentence broken up mid-flight is not a clean performance and
    # never registers organically (the nudge path recovers it).
    game = _make_game()
    _arm(game, Q3_PROMPT)
    spoken = (
        "Name the strait — Dave, wake up — that separates, and I mean "
        "REALLY separates, Europe from... you know, Asia. At Istanbul."
    )
    assert game.register_delivery_claim(spoken) is None
    assert game.say_registry.state("q_1_delivery") is None



def test_duplicate_reask_is_suppressed_not_redelivered():
    # BUG-2 stands: once q_N is delivered, a turn that textually
    # re-performs it comes back "duplicate" (tts_node yields silence) —
    # the "official re-run" class is physically impossible.
    game = _make_game()
    _arm(game, Q3_PROMPT)

    async def scenario():
        assert game.register_delivery_claim(Q3_PROMPT) == "claimed_core_sentence"
        assert game.register_delivery_claim(Q3_PROMPT) == "duplicate"
        await _drain()

    _run(scenario(), game)



def test_banter_after_registered_delivery_speaks_normally():
    # A stale structural flag meeting an already-claimed key with no
    # textual re-ask is NOT a duplicate — banter must never be swallowed.
    game = _make_game()
    _arm(game, Q3_PROMPT)

    async def scenario():
        assert game.register_delivery_claim(Q3_PROMPT) == "claimed_core_sentence"
        game.expect_delivery()  # no-op: already claimed
        assert game._pending_delivery_qnum is None
        assert game.register_delivery_claim("What a table tonight!") is None
        await _drain()

    _run(scenario(), game)



def test_ratio_telemetry_still_logs(caplog):
    # The matcher is demoted, not deleted: every playout with a question
    # armed logs `LILY_WINDOW | RATIO | … telemetry` and acts on nothing.
    game = _make_game()
    _arm(game, Q2_PROMPT)

    async def scenario():
        with caplog.at_level(logging.INFO):
            game.on_agent_speech_finished("just banter, no question here")
        await _drain()

    _run(scenario(), game)
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "LILY_WINDOW | RATIO" in joined
    assert "telemetry" in joined
    assert game.sk.answer_window_open is False



# -- scripted round: delivered and registered exactly once ----------------------


def test_scripted_round_every_question_delivered_and_registered_once(caplog):
    # A full scripted mini-round mixing structural (q1: begin_round-style
    # dispatch; q3: nudge) and organic (q2: core sentence) deliveries:
    # every question registers exactly once, every window opens exactly
    # once off the claim, no FALLBACK_OPEN, and every re-ask after
    # registration is a suppressed duplicate.
    game = _make_game()
    prompts = [
        ("Which planet is famously called the red planet?", "structural"),
        (Q3_PROMPT, "organic"),
        (Q2_PROMPT, "nudge"),
    ]

    async def scenario():
        with caplog.at_level(logging.INFO):
            for idx, (prompt, mode) in enumerate(prompts, start=1):
                _arm(game, prompt)
                key = f"q_{idx}_delivery"
                if mode == "structural":
                    # WS-1: a structural turn that drifts from the sheet
                    # is rewritten before claiming — never claimed silently.
                    game.expect_delivery()
                    drifted = "Off we go — the one everybody paints red!"
                    assert game.register_delivery_claim(drifted) == (
                        "rewrite_strict"
                    )
                    game.expect_delivery()
                    spoken = game.rendered_armed_question()
                    assert game.register_delivery_claim(spoken) == (
                        "claimed_structural"
                    )
                elif mode == "organic":
                    spoken = f"Next up. {prompt} Go!"
                    assert game.register_delivery_claim(spoken) == (
                        "claimed_core_sentence"
                    )
                else:
                    drifted = "Somebody is hoarding moons out there, friends!"
                    for _ in range(WINDOW_FALLBACK_AGENT_TURNS):
                        assert game.register_delivery_claim(drifted) is None
                        game.on_agent_speech_finished(drifted)
                    assert game.sk.answer_window_open is False
                    assert game.register_delivery_claim(drifted) == (
                        "rewrite_strict"
                    )
                    game.expect_delivery()
                    spoken = game.rendered_armed_question()
                    assert game.register_delivery_claim(spoken) == (
                        "claimed_structural"
                    )
                game.on_agent_speech_finished(spoken)
                assert game.sk.answer_window_open is True
                assert game.say_registry.state(key) == (
                    lily_say_gate.CLAIM_CONFIRMED
                )
                # Re-asking a delivered question is a suppressed duplicate
                # even after its window closed (the reveal repeats the
                # answer, never the question):
                game.sk.close_answer_window()
                assert game.register_delivery_claim(prompt) == "duplicate"
                if game._window_timer is not None:
                    game._window_timer.cancel()
                game.armed_question = None
        await _drain()

    _run(scenario(), game)
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "FALLBACK_OPEN" not in joined
    assert joined.count("act=question_delivery") >= 3
    # Exactly one registration per question:
    for idx in range(1, 4):
        assert game.say_registry.state(f"q_{idx}_delivery") == (
            lily_say_gate.CLAIM_CONFIRMED
        )



# -- expect_delivery edge discipline -------------------------------------------


def test_expect_delivery_noops_when_window_open_or_claimed():
    game = _make_game()
    _arm(game, Q3_PROMPT)

    async def scenario():
        game.sk.open_answer_window(now=100.0)
        game.expect_delivery()
        assert game._pending_delivery_qnum is None
        game.sk.close_answer_window()
        assert game.register_delivery_claim(Q3_PROMPT) == "claimed_core_sentence"
        game.expect_delivery()
        assert game._pending_delivery_qnum is None
        await _drain()

    _run(scenario(), game)



def test_stale_delivery_intent_dies_at_next_arm():
    # A pending flag for q_N never leaks into q_N+1: arming resets it.
    game = _make_game()
    _arm(game, Q3_PROMPT)
    game.expect_delivery()
    assert game._pending_delivery_qnum == 1
    _arm(game, Q2_PROMPT)
    assert game._pending_delivery_qnum is None
    assert game.consume_pending_delivery(2) is False



# ==============================================================================
# Sub-agent C — honesty grounded on published state
# ==============================================================================
#
# 01:37 replay: "my score is not updating, it's still showing zero" — the
# point had NOT committed (score truly 0), and Lily invented "the digital
# board takes a second to refresh once I submit to the database". The
# deterministic assist injects a grounded [state note: …] so her
# acknowledgment speaks to committed truth; the leak filter guarantees the
# note itself never speaks.


def _bind(game: LilyGame, name: str, label: str, score: int = 0) -> None:
    game.sk.bind_speaker(label, name)
    game.sk.players[name]["score"] = score


def test_0137_replay_player_correct_note_is_grounded():
    # The point never committed: the player is RIGHT that the board shows
    # zero. The note says so — grounded, with the anti-fiction instruction.
    note = lily_detect_state_contradiction(
        "my score is not updating, it's still showing zero",
        "Rami",
        {"Rami": {"score": 0}},
    )
    assert note is not None
    assert note.startswith("player is correct — Rami's committed score is 0")
    assert "never invent a mechanism" in note


def test_0137_replay_injects_state_note_into_context():
    # Full agent-layer path: the finalized utterance flows through
    # on_transcript_event and the note lands in the next turn's state
    # block, then consumes itself once her reply finishes playing.
    game = _make_game()
    _bind(game, "Rami", "S1", score=0)

    async def scenario():
        result = {
            "player": "Rami",
            "attribution": "label_match",
            "system_directed": False,
            "control_command": None,
            "media_choice": None,
            "candidate_recorded": False,
            "unrostered": False,
        }
        game.on_transcript_event(
            result,
            "my score is not updating, it's still showing zero",
            speaker_label="S1",
        )
        block = game.build_state_block()
        assert "[state note: player is correct — Rami's committed score is 0" in block
        # One-shot: consumed when the acknowledging turn finishes playing.
        game.on_agent_speech_finished("good catch — let me re-sync, one sec")
        assert game._state_note is None
        assert "[state note:" not in game.build_state_block()
        await _drain()

    _run(scenario(), game)


def test_2304_replay_uncommitted_number_is_never_validated():
    # 22:54 session, 23:04: she narrated "you should actually have three
    # points" off a player's complaint — validating a number the
    # scorekeeper never committed. The note grounds the mismatch case.
    note = lily_detect_state_contradiction(
        "hold on, I should actually have three points",
        "Rami",
        {"Rami": {"score": 1}},
    )
    assert note is not None
    assert "player says 3" in note
    assert "committed score is 1" in note
    assert "never validate" in note


def test_stuck_board_callout_without_number_grounds_committed_score():
    note = lily_detect_state_contradiction(
        "the scoreboard is frozen",
        "Sarah",
        {"Sarah": {"score": 2}},
    )
    assert note is not None
    assert "Sarah's committed score is 2" in note


def test_detector_is_conservative():
    players = {"Rami": {"score": 1}}
    # No anchor word — trivia answers and table talk never fire:
    assert lily_detect_state_contradiction(
        "the answer is zero degrees", "Rami", players
    ) is None
    # Anchor without a checkable claim:
    assert lily_detect_state_contradiction(
        "what a scoreboard we have tonight", "Rami", players
    ) is None
    # Unresolved speaker — nothing to ground against:
    assert lily_detect_state_contradiction(
        "my score is still showing zero", None, players
    ) is None
    assert lily_detect_state_contradiction(
        "my score is still showing zero", "Ghost", players
    ) is None


def test_commands_never_double_as_state_callouts():
    # "turn the screen off" carries the anchor word but is a media command
    # — the agent layer routes it to the media flow, and no note is set.
    game = _make_game()
    _bind(game, "Rami", "S1", score=0)

    async def scenario():
        result = {
            "player": "Rami",
            "attribution": "label_match",
            "system_directed": False,
            "control_command": None,
            "media_choice": "voice_only",
            "candidate_recorded": False,
            "unrostered": False,
        }
        game.on_transcript_event(result, "screen off please", speaker_label="S1")
        assert game._state_note is None
        await _drain()

    _run(scenario(), game)


# -- the note never speaks -------------------------------------------------------


def test_state_note_never_speaks_through_leak_filter():
    # If the note echoes into an outbound turn, the say-gate leak filter
    # drops the line whole and the spoken text carries no trace of it.
    note_line = (
        "[state note: player is correct — Rami's committed score is 0; "
        "nothing more has been committed]"
    )
    outbound = (
        "You're right, let me re-sync.\n"
        f"{note_line}\n"
        "Good catch — the board's behind me. Next question!"
    )
    filtered, reasons = lily_say_gate.lily_filter_leaks(outbound)
    assert "metadata:[state note:" in reasons
    assert "[state note:" not in filtered
    assert "committed score" not in filtered
    spoken = lily_say_gate.lily_clean_for_speech(filtered)
    assert "state note" not in spoken.lower()
    assert "re-sync" in spoken


def test_state_note_leak_reason_is_pinned_for_the_burn_guard():
    # tts_node skips the burn protocol when the ONLY leak is the state
    # note (it carries scores, never answers). Pin the exact reason
    # string that guard compares against.
    filtered, reasons = lily_say_gate.lily_filter_leaks(
        "[state note: player is correct — the board is right]"
    )
    assert reasons == ["metadata:[state note:"]
    assert filtered == ""


def test_ordinary_audio_tags_still_pass_the_extended_filter():
    # The new marker must not widen the filter: [excited]/[pause] and
    # normal speech pass untouched.
    text = "[excited] Rami takes the point! [pause] Next one."
    filtered, reasons = lily_say_gate.lily_filter_leaks(text)
    assert reasons == []
    assert filtered == text


# ==============================================================================
# Sub-agent E — score truth: commit at adjudication, screen never contradicted
# ==============================================================================
#
# 01:37:54 replay: "my score is not updating, it's still showing zero" —
# points committed only on the "official re-run" while Lily called the
# point "safe and sound". Post-B the identity chain (delivery/answer/
# verdict) makes committing at adjudication safe; pinned here: the score
# commits and its attribute publish DISPATCHES in the same tick as the
# verdict, is never queued behind the metadata round-trip, and the board
# is non-zero at the moment she says "on the board".

FEMUR_QUESTION = {
    "id": "q_1001",
    "prompt": "Often measuring the longest in adults, what is the longest "
              "human bone?",
    "canonical_answer": "the femur",
    "acceptable_answers": ["femur", "the femur", "thigh bone"],
    "category": "academic",
    "difficulty_tier": 1,
}


def _arm_question(game: LilyGame, question: dict) -> None:
    game.armed_question = dict(question)
    game.sk.start_question(game.armed_question)
    game.sk.round = 1
    game.sk.set_phase("round")


ROUND_TWO_MC = {
    "id": "q_round_two",
    "prompt": "Which planet in our solar system has the most moons?",
    "canonical_answer": "Saturn",
    "acceptable_answers": ["saturn"],
    "choices": ["Jupiter", "Saturn", "Uranus", "Neptune"],
    "category": "academic",
    "difficulty_tier": 2,
}


def test_round_transition_reveal_and_question_use_separate_strict_turns():
    """Regression for the 00:07 Jupiter-vs-Verona identity split.

    The round-closing reveal must stop before N+1. Only after that reveal
    completes may a strict question-only turn deliver the armed MC sheet.
    """
    game = _make_game()
    game.sk.questions_per_round = 6
    game.sk.question_number = 5
    game.next_question = dict(ROUND_TWO_MC)
    game.start_prefetch = lambda: None
    game.sk.bind_speaker("S1", "Rami")
    _arm_question(game, FEMUR_QUESTION)  # q=6, closes round one
    now = 900.0
    game.sk.open_answer_window(duration=30.0, now=now)
    game.sk.on_transcript_segment(
        text="femur", speaker_label="S1", is_final=True,
        now=now + 2, segment_start_time=now + 2,
    )

    _run(_adjudicate_and_drain(game), game)

    assert game.sk.question_number == 7
    assert game.armed_question["id"] == ROUND_TWO_MC["id"]
    # PATCH-001 T4: adjudication now dispatches TWO beats — the immediate
    # verdict word, then the standings flourish that never restates it.
    # The strict q7 delivery dispatches only at reveal playout completion
    # (delivery_scenario below).
    assert len(game.session.instructions) == 2
    verdict, reveal = game.session.instructions
    assert "VERDICT BEAT" in verdict
    assert "separate authoritative delivery turn follows" in reveal
    assert "do NOT restate" in reveal
    assert ROUND_TWO_MC["prompt"] not in verdict
    assert ROUND_TWO_MC["prompt"] not in reveal
    assert game._pending_delivery_qnum is None

    async def delivery_scenario():
        # Completing the reveal playout dispatches, but does not open, q7.
        game._pending_reveal_event = None  # UI plumbing is outside this test
        game.on_agent_speech_finished(
            "The femur is correct. Round one is over."
        )
        assert len(game.session.instructions) == 3
        delivery = game.session.instructions[2]
        assert ROUND_TWO_MC["prompt"] in delivery
        for choice in ROUND_TWO_MC["choices"]:
            assert choice in delivery
        assert game._pending_delivery_qnum == 7
        assert game.sk.answer_window_open is False

        # If the vocal turn resurrects Verona or emits a malformed
        # three-option sheet, strict registration requests deterministic
        # replacement before any answer window can open.
        malformed = (
            f"{ROUND_TWO_MC['prompt']} "
            "A) Jupiter B) Saturn C) Uranus"
        )
        assert game.register_delivery_claim(malformed) == "rewrite_strict"
        assert game.say_registry.state("q_7_delivery") is None
        game.expect_delivery()
        stale = "Romeo and Juliet is set in which Italian city?"
        assert game.register_delivery_claim(stale) == "rewrite_strict"
        exact = game.rendered_armed_question()
        assert "D) Neptune" in exact
        game.expect_delivery()
        assert game.register_delivery_claim(exact) == "claimed_structural"
        await _drain()

    _run(delivery_scenario(), game)


def test_013754_replay_score_published_before_the_verdict_speaks():
    game = _make_game()
    timeline: list = []

    def _speak(instructions: str) -> None:
        timeline.append(("reveal_speech", instructions))
        game.session.instructions.append(instructions)

    game.session.generate_reply = _speak

    async def slow_metadata(question_text, **kwargs):
        timeline.append(("meta_start", question_text or ""))
        await asyncio.sleep(0.05)  # the network round-trip
        timeline.append(("meta_end", question_text or ""))

    async def attrs_publish():
        timeline.append(
            ("attrs", {n: s["score"] for n, s in game.sk.players.items()})
        )

    game.publish_metadata = slow_metadata
    game.publish_attributes = attrs_publish
    game.arm_next_question = lambda: False
    game.start_prefetch = lambda: None

    game.sk.bind_speaker("S1", "Rami")
    _arm_question(game, FEMUR_QUESTION)
    now = 1000.0
    game.sk.open_answer_window(duration=30.0, now=now)
    game.sk.on_transcript_segment(
        text="The femur.", speaker_label="S1", is_final=True,
        now=now + 3, segment_start_time=now + 3,
    )

    async def scenario():
        await game.adjudicate(steal_allowed=True)
        await _drain()

    _run(scenario(), game)

    # Commit happened:
    assert game.sk.players["Rami"]["score"] > 0

    kinds = [entry[0] for entry in timeline]
    assert "reveal_speech" in kinds and "attrs" in kinds and "meta_end" in kinds
    # (a) The attribute publish is NEVER queued behind the metadata
    # round-trip: every attrs publish lands before the metadata call
    # completes (they were dispatched together, in the verdict tick).
    last_attrs = max(i for i, k in enumerate(kinds) if k == "attrs")
    meta_end = kinds.index("meta_end")
    assert last_attrs < meta_end, timeline
    # (b) The board is non-zero at the moment she says "on the board":
    # an attrs publish carrying Rami's committed point precedes the
    # verdict speech dispatch.
    speech_at = kinds.index("reveal_speech")
    committed_published = [
        i for i, entry in enumerate(timeline)
        if entry[0] == "attrs" and entry[1].get("Rami", 0) > 0
    ]
    assert committed_published and committed_published[0] < speech_at, timeline
    # (c) The reveal itself is the gated q_1_reveal act:
    assert game.say_registry.state("q_1_reveal") is not None


def test_adjudication_publishes_reveal_and_attributes_concurrently():
    # The gather contract: both publishes START before either completes —
    # the score truth never waits a full metadata round-trip.
    game = _make_game()
    timeline: list = []

    async def slow_metadata(question_text, **kwargs):
        timeline.append("meta_start")
        await asyncio.sleep(0.05)
        timeline.append("meta_end")

    async def slow_attrs():
        timeline.append("attrs_start")
        await asyncio.sleep(0.01)
        timeline.append("attrs_end")

    game.publish_metadata = slow_metadata
    game.publish_attributes = slow_attrs
    game.arm_next_question = lambda: False
    game.start_prefetch = lambda: None

    game.sk.bind_speaker("S1", "Rami")
    _arm_question(game, FEMUR_QUESTION)
    now = 2000.0
    game.sk.open_answer_window(duration=30.0, now=now)
    game.sk.on_transcript_segment(
        text="femur", speaker_label="S1", is_final=True,
        now=now + 2, segment_start_time=now + 2,
    )

    _run(_adjudicate_and_drain(game), game)

    assert timeline.index("attrs_start") < timeline.index("meta_end")
    assert timeline.index("attrs_end") < timeline.index("meta_end")


async def _adjudicate_and_drain(game: LilyGame):
    await game.adjudicate(steal_allowed=True)
    await _drain()


# -- voice/glass sync (2026-07-31): screen never leads the voice ---------------


def test_delivery_claim_does_not_publish_question_at_dispatch():
    # Dispatch-time publish led the voice by the length of any audio queued
    # ahead of the delivery turn (a greeting mid-playout while the question
    # hit the glass). The claim itself stays at dispatch; the SCREEN publish
    # moves to window open (delivery playout completion).
    game = _make_game()
    _arm(game, Q3_PROMPT)

    async def scenario():
        game.expect_delivery()
        assert game.register_delivery_claim(Q3_PROMPT) == "claimed_structural"
        await _drain()
        # Claimed, but nothing on the glass yet:
        assert game.metadata_publishes == []
        game.on_agent_speech_finished(Q3_PROMPT)
        await _drain()

    _run(scenario(), game)
    # Playout completed -> window open -> the question reaches the glass.
    assert game.sk.answer_window_open is True
    assert Q3_PROMPT in game.metadata_publishes


def test_first_question_phase_hold_keeps_lobby_until_playout():
    # The first question arms while Lily is still greeting. The published
    # phase holds on "lobby" (internal ui_phase flips for turn logic);
    # window open at delivery playout drops the hold and publishes
    # "answering" — the board never replaces the lobby mid-salutation.
    game = _make_game()
    game.ui_phase = "lobby"
    game._phase_hold = None
    game.next_question = {"prompt": Q3_PROMPT, "canonical_answer": "Bosphorus"}
    game.start_prefetch = lambda: None

    async def scenario():
        assert game.arm_next_question() is True
        assert game.ui_phase == "question"          # internal truth
        assert game._phase_hold == "lobby"          # published hold
        game.expect_delivery()
        assert game.register_delivery_claim(Q3_PROMPT) == "claimed_structural"
        await _drain()
        assert game._phase_hold == "lobby"          # still held at dispatch
        game.on_agent_speech_finished(Q3_PROMPT)
        await _drain()

    _run(scenario(), game)
    assert game.sk.answer_window_open is True
    assert game._phase_hold is None                 # hold dropped at playout
    assert game.ui_phase == "answering"


def test_mid_game_arm_does_not_hold_phase():
    # The hold is strictly a lobby -> first-delivery bridge: arming from any
    # in-game phase publishes immediately exactly as before.
    game = _make_game()
    game.ui_phase = "reveal"
    game._phase_hold = None
    game.next_question = {"prompt": Q2_PROMPT, "canonical_answer": "-"}
    game.start_prefetch = lambda: None

    async def scenario():
        assert game.arm_next_question() is True
        await _drain()

    _run(scenario(), game)
    assert game._phase_hold is None
    assert game.ui_phase == "question"


def test_reveal_beat_carries_committed_winner_score():
    # 08-04 wrong-score fix: score truth rides the WIRE. The reveal beat
    # carries the winner's COMMITTED score so the frontend targets a real
    # number — its old guessed increment double-counted once commits began
    # publishing ahead of the beat (desync-E ordering).
    game = _make_game()
    game.arm_next_question = lambda: False
    game.start_prefetch = lambda: None
    game.sk.bind_speaker("S1", "Rami")
    _arm_question(game, FEMUR_QUESTION)
    now = 3000.0
    game.sk.open_answer_window(duration=30.0, now=now)
    game.sk.on_transcript_segment(
        text="femur", speaker_label="S1", is_final=True,
        now=now + 2, segment_start_time=now + 2,
    )

    _run(_adjudicate_and_drain(game), game)

    ev = game._pending_reveal_event
    assert ev is not None
    assert ev["winner"] == "Rami"
    assert ev["correct"] is True
    committed = game.sk.players["Rami"]["score"]
    assert committed > 0
    assert ev["winner_score"] == committed
