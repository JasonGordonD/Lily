"""WO-LILY-HOTFIX-006 N12 / N8 / N13 — the question transition.

Three defects, one boundary: the moment a question ENDS. All evidence
below is live, from the 2026-08-08 sessions.

N12 — TWO LANES NARRATED THE SAME TRANSITION WITH OPPOSITE VERDICTS.
Session lily-D99BE7, question three, inside ONE beat she said both:

    "Chris got it in right on time with Russia! That's a point for Chris."
    "No points on that one — the answer was Russia!"

The ledger is unambiguous (verified: q_8294, Chris, "Russia.", correct,
1 point) — she announced both outcomes. In the same stretch question FOUR
had already been delivered by one lane while the other was still
revealing question three, which is what Rhonda said aloud:

    "She's getting confused. Like she jumped from a question to question
     and a third question."

and then the repair itself doubled — she apologised twice for the same
loop ("I caught myself stanching mid-thought" / "I caught my own loop
there and reset us"). Every one of those is the same defect: the
transition had no owner, so two lanes ran it.

The fix is structural, on the machinery that already exists for this
(SpeechActRegistry claim keys): a question transition is ONE JOURNALED
EVENT — reveal, verdict, next-delivery, in that order, serialized under
one owner. A second lane's narration of the same transition loses the
claim and is suppressed.

N8 — REVEAL-THEN-STEAL. Also live, back to back:

    "No worries! The correct answer is Frankenstein"
    "Frankenstein! That opens a five-second steal window for Chris"

The answer was revealed and then a steal window was opened ON THE
REVEALED ANSWER. Reveal BURNS the question (WS-4 burn protocol); the
steal path checks reveal state at dispatch. The steal mechanic itself —
after an UNREVEALED question — is protected here and must keep working.

N13 — ROSTER COUNT WRONG IN THE SPOKEN TEXT. "Whenever you four..."
spoken to a table of THREE, immediately after correctly naming Rami,
Rhonda and Chris. Same disease as the narrated score in HOTFIX-005 X1: a
number GENERATED instead of READ. The roster count is injected as
authoritative read-only state, and a spoken count that disagrees with the
enrolled roster is made loud.

This file imports lily_agent (and therefore livekit) — same boundary note
as test_hotfix006_adjudication.py.
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_audeering_consumers
import lily_persistence
import lily_say_gate
import lily_scorekeeper
from lily_agent import LilyGame
from lily_scorekeeper import LilyScorekeeper


# ---------------------------------------------------------------------------
# Harness — the __new__-built LilyGame the adjudication/desync fixtures
# drive, extended with the playout seam (on_agent_speech_finished) because
# every N12 assertion is about the ORDER two lanes reach the air in.
# ---------------------------------------------------------------------------


class _FakeSession:
    def __init__(self) -> None:
        self.instructions: list[str] = []
        self.said: list[str] = []

    def generate_reply(self, instructions: str) -> None:
        self.instructions.append(instructions)

    def say(self, text, *a, **k):
        # REFACTOR W2a: deterministic direct_say lane (the verdict beat).
        self.said.append(text)
        return None


class _FakeAgentHandle:
    def set_preemptive_generation(self, enabled: bool) -> None:
        pass


class _FakeReasoning:
    async def prefetch_question(self, sk, **kw):
        return None

    async def prefetch_picture_question(self, supabase, **kw):
        return None

    async def judge(self, *a, **kw):
        # Tier-2 IS legitimately reached in the N8 fixtures: Rhonda's real
        # "We don't know." matches no answer surface, so the judge is
        # consulted and rules it a miss. That miss is precisely the
        # precondition for a steal window — which is the mechanic under
        # test on both sides (protected when unrevealed, refused when
        # revealed).
        return '{"verdict": "incorrect", "reason": "not an answer"}'


# The live 2026-08-08 questions, by their real ids.
#
# q_8294 is the question Chris answered "Russia." to — the row the ledger
# records as correct, 1 point, while she narrated BOTH outcomes.
Q_RUSSIA = {
    "id": "q_8294",
    "prompt": "Which country is the largest in the world by land area?",
    "canonical_answer": "Russia",
    "acceptable_answers": ["russia"],
    "category": "geography",
}
# Question FOUR — the one that got delivered by the other lane while
# question three was still being revealed.
Q_FOUR = {
    "id": "q_8295",
    "prompt": "Which ocean is the deepest on Earth?",
    "canonical_answer": "Pacific",
    "acceptable_answers": ["pacific", "the pacific"],
    "category": "geography",
}
# N8's question: the answer she revealed and then offered a steal on.
Q_FRANKENSTEIN = {
    "id": "q_4822",
    "prompt": "Which 1818 novel introduced a creature stitched together "
              "from corpses?",
    "canonical_answer": "Frankenstein",
    "acceptable_answers": ["frankenstein"],
    "category": "literature",
}

# The live pair, verbatim. Same beat, same question, opposite verdicts.
N12_LANE_A_VERDICT = (
    "Chris got it in right on time with Russia! That's a point for Chris."
)
N12_LANE_B_VERDICT = "No points on that one — the answer was Russia!"
# N8's pair, verbatim.
N8_REVEAL = "No worries! The correct answer is Frankenstein"


def _make_game(session_id: str = "lily-D99BE7") -> LilyGame:
    game = LilyGame.__new__(LilyGame)
    game.session = _FakeSession()
    game.agent = _FakeAgentHandle()
    game._preemptive_paused = False
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game.sk = LilyScorekeeper(session_id)
    game.rounds_total = 3
    game.ui_phase = "answering"
    game.memory_block = ""
    game.reconnected = False
    game.game_started = True
    game.game_over = False
    game.armed_question = None
    game.next_question = None
    game.eliminated = []
    game.used_prompts = []
    game.asked_history = []
    game.group_id = "grp_test"
    game.promoted_categories = []
    game.prewager_standings = None
    game.highlights = []
    game.supabase = None
    game.reasoning = _FakeReasoning()
    game.background_audio = None
    game._bed_handle = None
    game._prefetch_task = None
    game._window_timer = None
    game._watchdog_task = None
    game._prefetch_stall_ticks = 0
    game._armed_limbo_ticks = 0
    game._steal_window = False
    game._adjudicating = False
    game._judged_keys = set()
    game._spec_judge = {}
    game._addressee_rows = {}
    game._pending_reveal_event = None
    game._pending_unbound_award = None
    game._user_turn_index = 0
    game._armed_speech_misses = 0
    game._pending_delivery_qnum = None
    game._state_note = None
    game.pending_clarify = {}
    game.forget_state = "idle"
    game.forget_requester = None
    game._forget_target_group = None
    game.prefs = {}
    game._prefs_offer_made = False
    game.acoustic = lily_audeering_consumers.LilyAcousticState()

    game.metadata_publishes: list[str] = []
    game.attribute_publishes: list[dict] = []

    async def _publish_metadata(question_text, **kwargs):
        game.metadata_publishes.append(question_text or "")

    async def _publish_attributes(*a, **k):
        game.attribute_publishes.append(
            {n: s["score"] for n, s in game.sk.players.items()}
        )

    game.publish_metadata = _publish_metadata
    game.publish_attributes = _publish_attributes
    game.send_event_nowait = lambda kind, payload=None: None
    return game


def _arm(game: LilyGame, question: dict, *, delivered: bool = True) -> None:
    """Arm one question the way arm_next_question leaves state. `delivered`
    records the delivery-reached-playout fact the reveal side gates on
    (HOTFIX-005 X3) so these fixtures adjudicate a genuinely spoken
    question."""
    game.armed_question = dict(question)
    game.sk.start_question(game.armed_question)
    game.sk.round = 1
    game.sk.set_phase("round")
    game._armed_speech_misses = 0
    game._pending_delivery_qnum = None
    game.ui_phase = "question"
    if delivered:
        delivered_set = getattr(game, "_delivered_to_playout", None)
        if delivered_set is None:
            delivered_set = game._delivered_to_playout = set()
        delivered_set.add(game.sk.question_number)


def _final(game: LilyGame, text: str, label: str, at: float, **kw) -> dict:
    return game.sk.on_transcript_segment(
        text=text, speaker_label=label, is_final=True,
        now=at, segment_start_time=at, segment_end_time=at + 0.5, **kw
    )


def _run(step, game: LilyGame | None = None):
    """Drive one scenario inside a live event loop, then cancel the window
    timers and metadata publishes it scheduled (same pattern as the
    adjudication fixture: an un-processed cancel prints 'Task was destroyed
    but it is pending!' and pollutes every later run)."""

    async def _scenario():
        out = step
        if callable(out):
            out = out()
        if asyncio.iscoroutine(out):
            out = await out
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        pending = [
            t for t in asyncio.all_tasks() if t is not asyncio.current_task()
        ]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        return out

    return asyncio.run(_scenario())


def _dispatched(game: LilyGame, needle: str) -> list[str]:
    # REFACTOR W2a: deterministic beats (verdict, steal opener) air on the
    # direct_say lane; scan both so needle matches are mechanism-agnostic.
    return [
        x for x in (game.session.instructions + game.session.said)
        if needle in x
    ]


# ===========================================================================
# N12 — one transition, one owner, one narration
# ===========================================================================


def _adjudicate_q3(game: LilyGame, *, arm_next: bool = True):
    """THE lily-D99BE7 question-three transition, on real machinery: Chris
    answers "Russia." inside the window and the ruling commits."""
    game.sk.bind_speaker("S1", "Rami")
    game.sk.bind_speaker("S2", "Rhonda")
    game.sk.bind_speaker("S3", "Chris")
    # Questions one and two are already behind us — this is question THREE.
    game.sk.question_number = 2
    _arm(game, Q_RUSSIA)
    assert game.sk.question_number == 3
    if arm_next:
        game.next_question = dict(Q_FOUR)
    now = time.time()
    game.sk.open_answer_window(
        duration=30.0, now=now, question_id=Q_RUSSIA["id"],
        question_index=3, registered=True,
    )
    _final(game, "Russia.", "S3", now + 2)

    async def _step():
        await game.adjudicate(steal_allowed=True)

    return _step


def test_the_transition_is_one_journaled_event_in_order():
    """N12 core: reveal, verdict, next-delivery — exactly one of each, in
    that order, for question three. Under the old design the journal did
    not exist and nothing said which lane owned the beat."""
    game = _make_game()
    step = _adjudicate_q3(game)

    def _scenario():
        async def _go():
            await step()
            # The verdict beat plays out; the post-reveal lane delivers Q4.
            game.on_agent_speech_finished(N12_LANE_A_VERDICT)
            await asyncio.sleep(0)
        return _go()

    _run(_scenario, game)

    # The ledger is the truth the narration must match (verified live row).
    assert game.sk.players["Chris"]["score"] == 1
    assert game.transition_stages(3) == ["reveal", "verdict", "next_delivery"]


def test_a_second_lane_cannot_own_the_same_transition():
    """The claim key IS the mechanism: whichever lane opens the q_3
    transition owns reveal + verdict + next-delivery. The second lane —
    the one that said "No points on that one" AND then apologised for its
    own loop — loses the claim and never narrates."""
    game = _make_game()
    _run(_adjudicate_q3(game), game)

    assert game.say_registry.state("q_3_transition") is not None
    assert game.open_question_transition(
        3, owner="lane_b", source="conversational"
    ) is False


def test_the_contradictory_second_verdict_cannot_air():
    """THE N12 fixture. Lane A committed and narrated: "Chris got it in
    right on time with Russia! That's a point for Chris." Lane B then
    narrated the opposite about the same question — "No points on that one
    — the answer was Russia!" — inside the same beat. The second narration
    of a transition already narrated is suppressed at the gate."""
    game = _make_game()
    _run(_adjudicate_q3(game), game)

    # Lane A's own verdict turn is the one the transition owns — it airs.
    assert game.register_transition_narration(
        N12_LANE_A_VERDICT, speech_id=None
    ) != "duplicate"
    # Lane B's contradiction is a SECOND narration of the same transition.
    assert game.register_transition_narration(
        N12_LANE_B_VERDICT, speech_id="lane_b_speech"
    ) == "duplicate"


def test_ordinary_talk_during_a_transition_still_airs():
    """The suppression is narrow by construction: only a second NARRATION
    of the transition is silenced. Banter, encouragement and answers to
    the table are untouched — a false suppression is a worse defect than
    the one being fixed."""
    game = _make_game()
    _run(_adjudicate_q3(game), game)

    for line in (
        "Nice hustle, Rhonda — that was close.",
        "Chris, you're on fire tonight.",
        "Yes, the board updates itself — nothing for you to do.",
    ):
        assert game.register_transition_narration(line) is None, line


def test_the_owning_lanes_other_keyed_beats_are_never_suppressed():
    """A keyed act of the SAME lane — the standings flourish, the finale —
    belongs to this transition by construction. Only an unkeyed second
    narration is the defect; suppressing the scores beat because it names
    the answer would be the fix eating the game."""
    game = _make_game()
    _run(_adjudicate_q3(game), game)

    # Give the standings flourish a claim of its own, the way gated_say
    # does at dispatch, and speak a line that WOULD read as a verdict.
    game.say_registry.claim("round_1_scores", owner="flourish_speech")
    assert game.register_transition_narration(
        "Russia it was — and that's a point for Chris, who's now leading.",
        speech_id="flourish_speech",
    ) is None


def test_once_the_next_question_is_out_the_ruling_can_be_discussed():
    """The gate is a BEAT gate, not a gag order. Once question four has
    been delivered the transition is over — a player asking "did Chris get
    that one?" gets a real answer, even though the answer necessarily
    names the ruling again."""
    game = _make_game()
    _run(_adjudicate_q3(game), game)
    assert game.register_transition_narration(N12_LANE_A_VERDICT) == "narration"

    def _finish():
        game.on_agent_speech_finished(N12_LANE_A_VERDICT)
    _run(_finish, game)
    assert "next_delivery" in game.transition_stages(3)

    assert game.register_transition_narration(
        "He did — Russia, that was Chris's point."
    ) is None


def test_a_stale_transition_stops_owning_her_words():
    """The same bound for a transition that never got a next delivery (the
    finale, a stalled supply line): past a beat's length she is talking
    about a ruling, not announcing it."""
    import lily_agent

    game = _make_game()
    _run(_adjudicate_q3(game), game)
    assert game.register_transition_narration(N12_LANE_A_VERDICT) == "narration"
    # Age the verdict stage past the narration window.
    entry = game._transition_entry(3, "verdict")
    entry["at"] -= lily_agent._TRANSITION_NARRATION_WINDOW_SECONDS + 1.0

    assert game.register_transition_narration(N12_LANE_B_VERDICT) is None


def test_a_verdict_re_air_speaks_in_fresh_words():
    """WS-3 re-air: a cut verdict re-dispatches and is deliberately spoken
    FRESH. The re-air owns the verdict claim, so it re-binds the beat's
    narration instead of being read as a contradiction of its own earlier
    wording."""
    game = _make_game()
    _run(_adjudicate_q3(game), game)

    # First airing binds.
    assert game.register_transition_narration(N12_LANE_A_VERDICT) == "narration"
    # The cut releases the claim; the retry re-claims it under a new
    # speech id and says the same thing differently.
    game.say_registry.release("q_3_reveal")
    game.say_registry.claim("q_3_reveal", owner="reair_speech")
    assert game.register_transition_narration(
        "Russia — correct, and the point goes to Chris.",
        speech_id="reair_speech",
    ) == "narration"


def test_question_four_cannot_be_delivered_mid_reveal_of_question_three():
    """Rhonda's line, verbatim: "She's getting confused. Like she jumped
    from a question to question and a third question." Question four had
    been delivered by one lane while the other was still revealing three.
    The next delivery is the LAST stage of the transition — it cannot air
    before the verdict it follows has played out."""
    game = _make_game()
    _run(_adjudicate_q3(game), game)

    # The verdict is dispatched but has NOT played out — question four is
    # not the room's business yet.
    assert game.say_registry.state("q_3_reveal") == lily_say_gate.CLAIM_PENDING
    assert game.dispatch_armed_question(source="second_lane") is False
    assert game.say_registry.state("q_4_delivery") is None
    assert "next_delivery" not in game.transition_stages(3)

    # Verdict plays out -> the SAME transition releases the delivery.
    def _finish():
        game.on_agent_speech_finished(N12_LANE_A_VERDICT)
    _run(_finish, game)
    assert game.transition_stages(3) == ["reveal", "verdict", "next_delivery"]


def test_a_second_adjudication_lane_narrates_nothing(caplog):
    """The whole-transition claim, exercised through adjudicate() itself: a
    second lane reaching the reveal for a question already revealed commits
    nothing further and — critically — SPEAKS nothing further. One beat,
    one verdict."""
    import logging

    game = _make_game()
    _run(_adjudicate_q3(game), game)
    # REFACTOR W2a: the verdict is the deterministic sheet on the direct_say
    # lane. N12 unchanged: one transition, one owner, ONE verdict beat — the
    # second lane speaks nothing further (say-registry claim + journal own the
    # suppression, both independent of the speech lane).
    verdicts = len(game.session.said)
    assert verdicts == 1

    # The second lane, still holding question three (the live shape: two
    # reveal chains alive over one question).
    game.armed_question = dict(Q_RUSSIA)
    game.sk.current_question = dict(Q_RUSSIA)
    game.sk.question_number = 3

    async def _second_lane():
        await game.adjudicate(steal_allowed=False)

    with caplog.at_level(logging.ERROR):
        _run(_second_lane, game)

    # W2a: the second lane speaks nothing further — the verdict-beat count on
    # the direct_say lane is unchanged.
    assert len(game.session.said) == verdicts
    assert game.transition_stages(3).count("verdict") == 1
    assert "SECOND_LANE_REFUSED" in "\n".join(
        r.getMessage() for r in caplog.records
    )


def test_the_next_delivery_is_journaled_once_however_many_lanes_ask():
    """Two lanes racing the post-reveal dispatch produce ONE delivery. The
    second is refused by the journal, not by luck of timing."""
    game = _make_game()
    _run(_adjudicate_q3(game), game)

    def _finish():
        game.on_agent_speech_finished(N12_LANE_A_VERDICT)
    _run(_finish, game)

    before = len(_dispatched(game, "Deliver ONLY the armed question"))
    assert before == 1
    assert game.dispatch_armed_question(source="second_lane") is False
    assert len(_dispatched(game, "Deliver ONLY the armed question")) == 1


# ===========================================================================
# N8 — a revealed question cannot open a steal window
# ===========================================================================


def _frankenstein_miss(game: LilyGame):
    """The N8 setup: the Frankenstein question is live, the window closes
    with NOBODY having answered it, and more than one player is unjudged
    (so a steal is mechanically possible)."""
    game.sk.bind_speaker("S1", "Rami")
    game.sk.bind_speaker("S3", "Chris")
    _arm(game, Q_FRANKENSTEIN)
    now = time.time()
    game.sk.open_answer_window(
        duration=30.0, now=now, question_id=Q_FRANKENSTEIN["id"],
        question_index=game.sk.question_number, registered=True,
    )
    # Rhonda's non-answer: someone spoke, nobody answered.
    _final(game, "We don't know.", "S1", now + 3)

    async def _step():
        await game.adjudicate(steal_allowed=True)

    return _step


def test_ordinary_steal_window_still_opens_on_an_unrevealed_question():
    """PROTECTED MECHANIC. Nobody landed it, the answer has NOT gone to
    air, a player is still unjudged: the five-second steal window opens
    exactly as designed. This test guards the fix below from
    over-reaching."""
    game = _make_game("lily-steal-ok")
    _run(_frankenstein_miss(game), game)

    assert game._steal_window is True
    assert game.sk.answer_window_open is True
    assert _dispatched(game, "five-second steal window")


def test_reveal_then_steal_cannot_reproduce():
    """THE N8 fixture, verbatim from the live session:

        "No worries! The correct answer is Frankenstein"
        "Frankenstein! That opens a five-second steal window for Chris"

    Her own previous turn already put the answer on the air. A revealed
    question is burned; a burned question has nothing left to steal."""
    game = _make_game("lily-4FB3B2")
    game._last_assistant_text = N8_REVEAL
    _run(_frankenstein_miss(game), game)

    assert game._steal_window is False
    assert not _dispatched(game, "five-second steal window")
    # Reveal burns: the question can never re-arm or be stolen later.
    assert game._is_burned(Q_FRANKENSTEIN) is True


def test_a_burned_question_can_never_open_a_steal_window_at_dispatch():
    """The dispatch-seam guard, independent of how the reveal happened:
    open_window(steal=True) over a question whose answer already aired is
    refused outright."""
    game = _make_game("lily-burned-steal")
    _arm(game, Q_FRANKENSTEIN)
    game._burn_question(game.armed_question, reason="revealed")

    def _step():
        game.open_window(duration=5.0, steal=True)

    _run(_step, game)
    assert game.sk.answer_window_open is False
    assert game._steal_window is False


def test_reading_the_question_is_never_a_reveal():
    """PROTECTED, and the reason the N8 predicate is stricter than the
    organic-preempt one: a multiple-choice stem carries the correct answer
    among its options. Reading the question out is not giving it away — if
    it counted as a reveal, every MC question would lose its steal
    window."""
    game = _make_game("lily-mc-steal")
    mc = dict(Q_FRANKENSTEIN)
    mc["choices"] = ["Dracula", "Frankenstein", "Carmilla", "The Golem"]
    # Her last turn IS the delivery — stem plus every option, answer
    # included, and "Alright" carries the substring the old cue table
    # matched on.
    game._last_assistant_text = (
        "Alright — which 1818 novel introduced a creature stitched together "
        "from corpses? Dracula, Frankenstein, Carmilla, or The Golem?"
    )
    assert game._reveal_already_on_air(mc) is False

    game.sk.bind_speaker("S1", "Rami")
    game.sk.bind_speaker("S3", "Chris")
    _arm(game, mc)
    now = time.time()
    game.sk.open_answer_window(
        duration=30.0, now=now, question_id=mc["id"],
        question_index=game.sk.question_number, registered=True,
    )
    _final(game, "We don't know.", "S1", now + 3)

    async def _step():
        await game.adjudicate(steal_allowed=True)

    _run(_step, game)
    assert game._steal_window is True
    assert _dispatched(game, "five-second steal window")


# ===========================================================================
# N13 — the spoken roster count is READ, never computed
# ===========================================================================

# The live table: three players, correctly named in the same breath that
# miscounted them.
LIVE_ROSTER = ("Rami", "Rhonda", "Chris")

# Every phrasing a count reaches the air in. The FOUR variants are the
# live defect ("Whenever you four..."); the THREE variants are the truth.
WRONG_COUNT_PHRASINGS = (
    "Whenever you four are ready, we'll dive in!",
    "The four of you are up.",
    "All four of you get a shot at this one.",
    "Four players, three rounds — let's go.",
    "I've got all 4 of you on the board.",
)
RIGHT_COUNT_PHRASINGS = (
    "Whenever you three are ready, we'll dive in!",
    "The three of you are up.",
    "All three of you get a shot at this one.",
    "Three players, three rounds — let's go.",
    "I've got all 3 of you on the board.",
)
# Numbers that are NOT a count of people. A count detector that fires on
# these would be worse than the defect.
NON_ROSTER_NUMBERS = (
    "That's question four of six.",
    "Round four starts now.",
    "Chris, you're on four points.",
    "The answer is Fantastic Four.",
    "Give me four seconds on the clock.",
)


def _roster_game(names=LIVE_ROSTER) -> LilyGame:
    game = LilyGame.__new__(LilyGame)
    game.sk = LilyScorekeeper("lily-D99BE7")
    for i, name in enumerate(names, start=1):
        game.sk.bind_speaker(f"S{i}", name)
    return game


def test_every_wrong_count_phrasing_is_a_divergence():
    """N13: "Whenever you four..." to a table of THREE. The count was
    generated, not read — the same disease as the narrated score."""
    for line in WRONG_COUNT_PHRASINGS:
        div = lily_scorekeeper.lily_narrated_roster_count_divergence(
            line, list(LIVE_ROSTER)
        )
        assert div is not None, line
        assert div["spoken"] == 4 and div["roster"] == 3


def test_every_right_count_phrasing_is_clean():
    for line in RIGHT_COUNT_PHRASINGS:
        assert lily_scorekeeper.lily_narrated_roster_count_divergence(
            line, list(LIVE_ROSTER)
        ) is None, line


def test_numbers_that_are_not_people_never_flag():
    for line in NON_ROSTER_NUMBERS:
        assert lily_scorekeeper.lily_narrated_roster_count_divergence(
            line, list(LIVE_ROSTER)
        ) is None, line


def test_empty_roster_never_flags():
    assert lily_scorekeeper.lily_narrated_roster_count_divergence(
        "Whenever you four are ready", []
    ) is None


def test_state_block_injects_the_authoritative_roster_count():
    """The X1 pattern: the number is injected as read-only state so the
    model never has to compute it."""
    game = _roster_game()
    line = game._roster_authority_line()
    assert line is not None
    assert "ROSTER" in line and "AUTHORITATIVE" in line
    assert "3 player" in line
    for name in LIVE_ROSTER:
        assert name in line
    assert "NEVER compute" in line


def test_roster_authority_line_rides_the_state_block():
    game = _make_game()
    for i, name in enumerate(LIVE_ROSTER, start=1):
        game.sk.bind_speaker(f"S{i}", name)
    block = game.build_state_block()
    assert "ROSTER — AUTHORITATIVE" in block
    assert "3 player" in block


def test_no_roster_no_line():
    game = _roster_game(names=())
    assert game._roster_authority_line() is None


def test_solo_table_reads_one_player():
    game = _roster_game(names=("Rami",))
    line = game._roster_authority_line()
    assert "1 player" in line
    assert lily_scorekeeper.lily_narrated_roster_count_divergence(
        "Whenever you two are ready", ["Rami"]
    ) is not None
