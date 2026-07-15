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
from lily_scorekeeper import LilyScorekeeper


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
    # post-tool / skip follow-up) claims at dispatch REGARDLESS of
    # phrasing: the 01:33 q2 paraphrase registers, the window opens off
    # the claim at that turn's playout, and FALLBACK_OPEN never fires.
    game = _make_game()
    _arm(game, Q2_PROMPT)

    async def scenario():
        game.expect_delivery()  # the structural dispatch signal
        verdict = game.register_delivery_claim(Q2_PARAPHRASE)
        assert verdict == "claimed_structural"
        assert game.say_registry.state("q_1_delivery") is not None
        assert not game.sk.answer_window_open  # opens at playout, not claim
        with caplog.at_level(logging.INFO):
            game.on_agent_speech_finished(Q2_PARAPHRASE)
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
    # nudge dispatches, its turn claims, and the window opens on a
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
            # The nudged turn claims at dispatch regardless of phrasing:
            verdict = game.register_delivery_claim(Q3_PARAPHRASE)
            assert verdict == "claimed_structural"
            game.on_agent_speech_finished(Q3_PARAPHRASE)
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
                    game.expect_delivery()
                    spoken = "Off we go — the one everybody paints red!"
                    assert game.register_delivery_claim(spoken) == (
                        "claimed_structural"
                    )
                elif mode == "organic":
                    spoken = f"Next up. {prompt} Go!"
                    assert game.register_delivery_claim(spoken) == (
                        "claimed_core_sentence"
                    )
                else:
                    spoken = "Somebody is hoarding moons out there, friends!"
                    for _ in range(WINDOW_FALLBACK_AGENT_TURNS):
                        assert game.register_delivery_claim(spoken) is None
                        game.on_agent_speech_finished(spoken)
                    assert game.sk.answer_window_open is False
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

