"""WO-LILY-BARGE-RESILIENCE-001 — the doubling class (P1) and the barge-to-ask
ordering (P2).

The doubling incident (session lily-6A32BD-741b9b66, transcript 205/216/249):
the q1 "1930s -> Rami" result aired THREE times. Root cause: all three
anti-repeat guards (C6 receipt-note, N12 register_transition_narration,
ORGANIC_PREEMPTED) bind at on-air CONFIRM, and a barge cancels the airing turn
BEFORE it confirms — so none of them ever stamped, and the keyed verdict SAY
plus a later free organic turn each restated the ruling.

The fix stamps ONE durable "result for qN stated on air" fact at the AIRING
itself (tts_node, before the frames yield), gates the keyed verdict SAY on it,
and injects the C6 anti-double into the organic system-prompt wrap while the
fact is fresh.
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_desync_fixture import (  # noqa: E402
    FEMUR_QUESTION,
    _adjudicate_and_drain,
    _arm_question,
    _make_game,
    _run,
)


def _open_reveal_transition(game, qnum: int, answer: str) -> str:
    """Open q_{qnum}'s transition and journal its reveal stage with the
    canonical answer — the state the airing stamp reads."""
    owner = f"owner_{qnum}"
    assert game.open_question_transition(qnum, owner=owner, source="test")
    game.journal_transition(
        qnum, "reveal", owner=owner, detail={"answer": answer},
    )
    return owner


# -- the airing stamp (the belt the confirm-blind guards lacked) --------------


def test_result_stamp_fires_when_a_reveal_turn_airs_the_answer():
    # A turn carrying the transition's answer + a verdict cue stamps the fact
    # AT AIRING — this is the point a barge cannot un-do, because it runs
    # before the TTS frames yield and long before the confirm the old guards
    # waited on.
    game = _make_game()
    _open_reveal_transition(game, 1, "the 1930s")
    assert game.result_aired_for(1) is None
    game.stamp_result_aired_from_turn("It's the 1930s — point to Rami.")
    assert game.result_aired_for(1) is not None


def test_result_stamp_ignores_a_non_result_turn():
    # Banter / a question read that does not put the answer on the air must
    # never stamp the fact (a false stamp would gag the real verdict beat).
    game = _make_game()
    _open_reveal_transition(game, 1, "the 1930s")
    game.stamp_result_aired_from_turn("Alright, everyone ready for this one?")
    assert game.result_aired_for(1) is None


def test_result_stamp_is_first_airing_wins():
    # First airing wins: a later airing of the same qnum does not overwrite
    # the record, so the fact names the words the room first heard.
    game = _make_game()
    _open_reveal_transition(game, 1, "the 1930s")
    game.stamp_result_aired_from_turn("It's the 1930s — point to Rami.")
    first = game.result_aired_for(1)
    game.stamp_result_aired_from_turn("Nineteen-thirties, that's yours, Rami.")
    assert game.result_aired_for(1) == first


# -- D1: keyed verdict SAY is gated once the result is on the air --------------


def _score_and_reveal(game):
    game.arm_next_question = lambda: False
    game.start_prefetch = lambda: None
    game.sk.bind_speaker("S1", "Rami")
    _arm_question(game, FEMUR_QUESTION)
    now = 5000.0
    game.sk.open_answer_window(duration=30.0, now=now)
    game.sk.on_transcript_segment(
        text="the femur", speaker_label="S1", is_final=True,
        now=now + 2, segment_start_time=now + 2,
    )


def test_d1_keyed_verdict_say_suppressed_when_result_already_aired():
    # D1: a barge cut the organic reveal turn before it confirmed, so
    # _verdict_already_spoken (which reads confirm-time _last_assistant_text)
    # misses — but the reveal DID put the answer on the air, so the durable
    # fact gates the keyed adjudicate_verdict SAY. Result stated exactly once.
    game = _make_game()
    _score_and_reveal(game)
    qnum = game.sk.question_number
    # The reveal aired the answer (then got barge-cut before confirm):
    game._result_aired = {
        "qnum": qnum, "text": "The femur — point to Rami.",
        "at": time.monotonic(),
    }

    _run(_adjudicate_and_drain(game), game)

    # The keyed verdict SAY did NOT fire a second statement of the result:
    assert game.session.said == []
    assert game.session.instructions == []
    # ...but the point still committed — scoring is independent of the cancel:
    assert game.sk.players["Rami"]["score"] > 0


def test_d1_keyed_verdict_say_fires_normally_without_the_fact():
    # Control: with no result-aired fact, the verdict beat speaks as usual —
    # the gate only suppresses a SECOND statement, never the first.
    game = _make_game()
    _score_and_reveal(game)

    _run(_adjudicate_and_drain(game), game)

    assert len(game.session.said) == 1
    assert "femur" in game.session.said[0].lower()
    assert game.sk.players["Rami"]["score"] > 0


# -- D2: the organic wrap carries the anti-double while the fact is fresh ------


def test_d2_organic_wrap_forbids_restating_the_aired_result():
    # D2: after the verdict aired, a free organic turn (the prefs
    # confirmation) would open by restating the ruling. The state block now
    # carries the ALREADY-RULED constraint so the conversational brain does
    # not re-announce it (transcript 249).
    game = _make_game()
    game._result_aired = {
        "qnum": 1, "text": "The femur — point to Rami.", "at": time.monotonic(),
    }
    block = game.build_state_block()
    assert "ALREADY-RULED" in block
    assert "do NOT restate" in block


def test_d2_constraint_clears_when_the_next_window_opens():
    # The fact is fresh only until the next question is in play: a non-steal
    # window open clears it, so the constraint does not linger onto turns
    # about the NEW question.
    game = _make_game()
    game._result_aired = {
        "qnum": 1, "text": "The femur — point to Rami.", "at": time.monotonic(),
    }
    assert "ALREADY-RULED" in game.build_state_block()
    game.sk.bind_speaker("S1", "Rami")
    _arm_question(game, FEMUR_QUESTION)

    async def scenario():
        game.open_window(duration=30.0)
        await asyncio.sleep(0)

    _run(scenario(), game)
    assert game.result_aired_recent() is None
    assert "ALREADY-RULED" not in game.build_state_block()


def test_d2_steal_window_does_not_clear_the_fact():
    # A steal window rides the SAME question — it must not clear the fact
    # (the result of that very question is what a steal would be stealing).
    game = _make_game()
    game.sk.bind_speaker("S1", "Rami")
    _arm_question(game, FEMUR_QUESTION)
    qnum = game.sk.question_number
    game._result_aired = {
        "qnum": qnum, "text": "Nobody landed it — it was the femur.",
        "at": time.monotonic(),
    }

    async def scenario():
        # A steal window opens on the same (unburned) armed question:
        game.open_window(duration=5.0, steal=True)
        await asyncio.sleep(0)

    _run(scenario(), game)
    assert game.result_aired_recent() is not None


# -- D3: across a barge storm the result is announced once total --------------


def test_d3_storm_reentry_never_restates_the_result():
    # D3: 3+ rapid barges spanning reveal -> verdict -> next-delivery. Once the
    # result is on the air, every adjudicate re-entry for that question is
    # gated — no third restatement (205/216/249). The fact is first-airing-wins
    # and the gate is idempotent, so the storm cannot re-open the ruling.
    game = _make_game()
    _score_and_reveal(game)
    qnum = game.sk.question_number
    game._result_aired = {
        "qnum": qnum, "text": "The femur — point to Rami.",
        "at": time.monotonic(),
    }

    async def storm():
        for _ in range(3):
            game._adjudicating = False
            game._question_transitioning = False
            await game.adjudicate(steal_allowed=False)
        await asyncio.sleep(0)

    _run(storm(), game)

    # Not one verdict SAY aired across the whole storm — the result the room
    # already heard is never restated:
    assert game.session.said == []


# -- D4: a cut-verdict re-air is itself a producer of the fact -----------------


def test_d4_cut_verdict_reair_stamps_the_fact_and_gates_the_next_beat():
    # D4: a barge cuts the verdict, reair_cut_verdict re-airs the RESULT from
    # the transition journal on the same key. That re-air is a producer of the
    # result word too — it flows through the same airing hook — so once it has
    # aired, a subsequent transition/re-entry does not restate the same reveal.
    game = _make_game()
    owner = f"owner_1"
    assert game.open_question_transition(1, owner=owner, source="test")
    # The reveal entry carries the committed result (answer + who won); the
    # verdict was cut, so C8 re-airs it as one deterministic line:
    game.journal_transition(
        1, "reveal", owner=owner,
        detail={"answer": "the femur", "correct": True, "winner": "Rami"},
    )
    game.journal_transition(
        1, "verdict", owner=owner, detail={"key": "q_1_reveal"},
    )
    reaired = game.reair_cut_verdict(["q_1_reveal"])
    assert reaired
    reair_line = game.session.said[-1]
    # The airing hook (tts_node) fires on the re-aired words in production;
    # simulate it and confirm the fact is now stamped and gates a re-statement.
    game.stamp_result_aired_from_turn(reair_line)
    assert game.result_aired_for(1) is not None

