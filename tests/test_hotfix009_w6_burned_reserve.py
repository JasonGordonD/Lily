"""WO-LILY-HOTFIX-009 W6 — a burned question is never re-served and never
re-revealed; the timeout adjudication survives its own timer.

THE live re-serve loop (session lily-5E3036-b56b5eb4, q4 kb_491 "Winning
Nobel prizes in both Physics and Chemistry, who was this pioneering
Polish-French scientist?" — Marie Curie):

  05:34:16  q_4_delivery airs; window opens on the delivery claim.
  05:34:19  Rami: "Oh! I don't know." — the only candidate.
  05:34:35  Her conversational lane reveals ORGANICALLY: "Pass locked.
            Marie Curie — double Nobel, Physics and Chemistry." (no
            verdict cue, so nothing burned — lily_verdict_narration
            returns None for this phrasing).
  05:34:47  Window timeout adjudication #1: ruling committed (SCORE_COMMIT
            incorrect, 0 points), steal branch taken — survives only
            because the steal branch returns before any await.
  05:34:52  Steal-timer adjudication #2 opens the q4 transition, journals
            REVEAL, then dies silently at the reveal-publish gather — no
            verdict stage, no CRASHED line. Root cause: adjudicate() ran
            INSIDE the steal timer task (open_window's _expire awaits
            adjudicate) and its own timer-cancel line cancelled the task
            it was running in; CancelledError landed at the first await.
  05:35:10→05:36:34  Wedge: armed q4, terminal, journal=[reveal]. Z2c's
            REOPEN_REFUSED_TERMINAL correctly holds the window shut; the
            watchdog is paused nearly every tick (host_speaking /
            address_unanswered) while Rami argues.
  05:36:42  ARMED_LIMBO forces adjudicate(reclaim_transition=True). The
            journal has no air-proof (_transition_reached_air False — the
            reveal aired organically, unjournaled), so the 1C53C6 reclaim
            fires (RECLAIMED_UNAIRED) and RE-NARRATES the whole beat:
            "Nobody landed it. Marie Curie." airs at 05:36:51 — 2m16s
            after the answer went to air — and q5 (q_7391, diamond)
            delivers 4 seconds later on top of it.

Fixes under test:
  1. adjudicate() never cancels the window timer when it IS the window
     timer task — the timeout adjudication completes its beat.
  2. Recovery re-entry over a narration-PARTIAL transition of a BURNED
     question resumes as bookkeeping (RESUMED_BURNED): the missing stages
     are journaled as already-on-air, nothing re-airs, and the Z2c release
     seams see a narration-complete beat. An UNBURNED dead journal keeps
     the 1C53C6 reclaim (nothing aired there; re-narration is the cure).

Draw-side (brief fault (a)) verified existing, no code: the trace shows
no burned re-draw — q5 was fresh (q_7391). Burn is already authoritative
at draw (_no_repeat_exclusion), arm (REARM_BLOCKED), steal and reconnect.

Same import boundary note as test_hotfix006_transitions.py.
"""

import asyncio
import inspect
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_audeering_consumers
import lily_say_gate
from lily_agent import LilyGame
from lily_scorekeeper import LilyScorekeeper


class _FakeSession:
    def __init__(self) -> None:
        self.instructions: list[str] = []

    def generate_reply(self, instructions: str) -> None:
        self.instructions.append(instructions)


class _FakeAgentHandle:
    def set_preemptive_generation(self, enabled: bool) -> None:
        pass


class _FakeReasoning:
    """Supply state of the incident: generation starved (the q5 draw only
    landed minutes later); the judge rules the pass incorrect."""

    async def prefetch_question(self, sk, **kw):
        return None

    async def prefetch_picture_question(self, supabase, **kw):
        return None

    async def judge(self, *a, **kw):
        return '{"verdict": "incorrect", "reason": "not an answer"}'


# The real q4 of the incident session (id from the 05:34:07 durable-asked
# row and the 05:36:43 burn row; prompt/answer from the q_4_delivery and
# q_4_reveal transcript rows).
Q_CURIE = {
    "id": "kb_491",
    "prompt": (
        "Winning Nobel prizes in both Physics and Chemistry, who was this "
        "pioneering Polish-French scientist?"
    ),
    "canonical_answer": "Marie Curie",
    "acceptable_answers": ["marie curie", "curie"],
    "category": "academic",
}

# The real q5 (id from the 05:36:55 window_open burn row; prompt/answer
# from the 05:36:55 delivery row and Rami's judged "diamond" answer).
Q_DIAMOND = {
    "id": "q_7391",
    "prompt": (
        "What is the hardest naturally occurring substance found on Earth?"
    ),
    "canonical_answer": "diamond",
    "acceptable_answers": ["diamond"],
    "category": "academic",
}


def _make_game(session_id: str = "lily-5E3036-b56b5eb4") -> LilyGame:
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


def _arm_incident_q4(game: LilyGame) -> float:
    """q4 armed, delivered to playout, delivery claim CONFIRMED — the
    05:34:16 state — with Rami's pass in as the only candidate."""
    game.sk.bind_speaker("S1", "Rami")
    game.armed_question = dict(Q_CURIE)
    game.sk.start_question(game.armed_question)
    game.sk.round = 2
    game.sk.set_phase("round")
    game.ui_phase = "question"
    delivered = getattr(game, "_delivered_to_playout", None)
    if delivered is None:
        delivered = game._delivered_to_playout = set()
    delivered.add(game.sk.question_number)
    key = f"q_{game.sk.question_number}_delivery"
    game.say_registry.claim(key, owner="speech_delivery_q4")
    game.say_registry.confirm(key)
    now = time.time()
    game.sk.open_answer_window(
        duration=30.0, now=now, question_id=Q_CURIE["id"],
        question_index=game.sk.question_number, registered=True,
    )
    game.sk.on_transcript_segment(
        text="Oh! I don't know.", speaker_label="S1", is_final=True,
        now=now + 3, segment_start_time=now + 3, segment_end_time=now + 3.5,
    )
    # 05:36:34, the last finished turn before the ARMED_LIMBO recovery —
    # no answer, no verdict cue.
    game._last_assistant_text = (
        "Yeah — I hear you. I still can't see what's jammed back there, "
        "so I can't hand you a fix."
    )
    return now


def _run(step):
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


def _verdict_beats(game: LilyGame) -> list[str]:
    return [
        i for i in game.session.instructions
        if "VERDICT BEAT" in i or Q_CURIE["canonical_answer"] in i
    ]


# ===========================================================================
# 0. Source needles — the mirrored timer topology below must match the
#    live open_window body, and the fix must stay in adjudicate.
# ===========================================================================


def test_source_needles_timer_topology_and_self_cancel_guard():
    open_window_src = inspect.getsource(LilyGame.open_window)
    # _expire awaits adjudicate INSIDE the task bound to _window_timer —
    # the topology the mirrored timer in these tests reproduces.
    assert "await self.adjudicate(steal_allowed=not steal)" in open_window_src
    assert "self._window_timer = asyncio.ensure_future(_expire())" in (
        open_window_src
    )
    adjudicate_src = inspect.getsource(LilyGame.adjudicate)
    assert "is not asyncio.current_task()" in adjudicate_src
    # The steal-branch replacement site carries the same guard: open_window
    # runs from adjudicate INSIDE the timer task it replaces.
    assert "is not asyncio.current_task()" in open_window_src


# ===========================================================================
# 1. The 05:34:52 death — adjudicate running inside its own window timer
#    must complete the beat instead of cancelling itself at the reveal
#    publish. (On the pre-fix code the task dies with stages=[reveal],
#    the question stays armed, and no verdict ever airs.)
# ===========================================================================


def test_timeout_adjudication_survives_its_own_timer():
    game = _make_game()
    _arm_incident_q4(game)
    qnum = game.sk.question_number
    # Adjudication #1 (05:34:47) already judged the pass — the steal-timer
    # re-entry skips the judged candidate synchronously, exactly the path
    # that reached the reveal publish in the trace.
    game._judged_keys = {"Rami"}
    assert game.next_question is None  # supply starved until 05:36

    async def _timer():
        # Mirror of open_window's _expire (source-needled above): the
        # adjudication is awaited INSIDE the task that _window_timer
        # points at.
        await asyncio.sleep(0.01)
        if game.sk.answer_window_open and not game._adjudicating:
            await game.adjudicate(steal_allowed=False)

    async def _go():
        game._window_timer = asyncio.ensure_future(_timer())
        await asyncio.gather(game._window_timer, return_exceptions=True)

    _run(_go)

    # The beat completed: reveal AND verdict journaled, the verdict beat
    # dispatched once, the question consumed and burned. With a
    # NON-organic verdict still airing, the Z2c release correctly defers
    # to the verdict's playout seam — reachable the moment the claim
    # confirms.
    assert not game._window_timer.cancelled()
    assert game.transition_stages(qnum) == ["reveal", "verdict"]
    assert len(_verdict_beats(game)) == 1
    assert game.armed_question is None
    assert game._is_burned(Q_CURIE)
    assert not game._adjudicating
    # Verdict playout completes → the beat releases (Z2c, mech 80).
    game.say_registry.confirm(f"q_{qnum}_reveal")
    assert game.release_completed_transition(
        qnum, reason="supply_empty_post_reveal"
    )
    assert game.transition_stages(qnum) == [
        "reveal", "verdict", "next_delivery"
    ]
    assert game._open_transition_qnum is None


# ===========================================================================
# 1b. The 05:34:47 shape — the STEAL branch replaces the timer from inside
#     the timer task (open_window's cancel site). The outer timer must
#     survive the replacement, and the steal-expiry adjudication that
#     follows (the 05:34:52 death) must complete its beat.
# ===========================================================================


def test_steal_branch_timer_replacement_does_not_cancel_its_own_task():
    import lily_config

    game = _make_game()
    _arm_incident_q4(game)
    qnum = game.sk.question_number
    # A second, unjudged player makes the steal branch reachable (the live
    # session's roster made stealers_exist true at 05:34:47).
    game.sk.bind_speaker("S2", "Maria")
    assert game.next_question is None

    real_steal_seconds = lily_config.steal_window_seconds
    lily_config.steal_window_seconds = lambda: 0.05
    try:
        async def _timer():
            await asyncio.sleep(0.01)
            if game.sk.answer_window_open and not game._adjudicating:
                await game.adjudicate(steal_allowed=True)

        async def _go():
            outer = asyncio.ensure_future(_timer())
            game._window_timer = outer
            await asyncio.gather(outer, return_exceptions=True)
            # The steal branch replaced the timer WITHOUT cancelling the
            # task it was running in.
            assert not outer.cancelled()
            steal_timer = game._window_timer
            assert steal_timer is not outer
            assert game._steal_window
            # The steal window expires with no stealer — the follow-on
            # adjudication (the one that died at 05:34:52) completes.
            await asyncio.gather(steal_timer, return_exceptions=True)
            assert not steal_timer.cancelled()

        _run(_go)
    finally:
        lily_config.steal_window_seconds = real_steal_seconds

    assert game.transition_stages(qnum)[:2] == ["reveal", "verdict"]
    assert len(_verdict_beats(game)) == 1
    assert game.armed_question is None
    assert game._is_burned(Q_CURIE)


# ===========================================================================
# 2. The 05:36:42 zombie — recovery re-entry over the narration-partial
#    journal of a BURNED question must resume as bookkeeping, never
#    re-narrate, and the fresh question must follow without collision.
#    (On the pre-fix code RECLAIMED_UNAIRED re-narrates: "Nobody landed
#    it. Marie Curie" re-airs and q5 lands on top of it.)
# ===========================================================================


def _wedge_partial_burned(game: LilyGame) -> int:
    """The wedge as the trace recorded it, plus the burn the organic
    reveal SHOULD carry: armed q4, terminal (ruling committed at
    05:34:47), delivery claim confirmed, window closed, transition open
    with stages=[reveal] and no air-proof, answer on the air."""
    qnum = game.sk.question_number
    game.sk.close_answer_window()
    game.note_answer_heard(qnum)
    game._judged_keys = {"Rami"}
    assert game.open_question_transition(
        qnum, owner="transition_bcdc1780", source="adjudicate"
    )
    game.journal_transition(
        qnum, "reveal", owner="transition_bcdc1780",
        detail={
            "answer": Q_CURIE["canonical_answer"],
            "correct": False,
            "winner": None,
            "question_id": Q_CURIE["id"],
        },
    )
    game._burn_question(dict(Q_CURIE), reason="revealed_on_air",
                        persist=False)
    assert not game._transition_reached_air(qnum)
    return qnum


def test_burned_partial_reclaim_never_renarrates_and_q5_follows():
    game = _make_game()
    _arm_incident_q4(game)
    qnum = _wedge_partial_burned(game)
    game.next_question = dict(Q_DIAMOND)  # supply recovered by 05:36

    spoken_before = list(game.session.instructions)
    _run(lambda: game.adjudicate(
        steal_allowed=False, reclaim_transition=True
    ))

    # Nothing re-aired for the burned question.
    assert game.session.instructions == spoken_before
    assert _verdict_beats(game) == []
    # The journal was completed as already-on-air — narration-complete for
    # every release seam — and the beat holds no second-lane door open.
    stages = game.transition_stages(qnum)
    assert stages[:2] == ["reveal", "verdict"]
    assert game.transition_narration_complete(qnum) or (
        "next_delivery" in stages
    )
    assert game._transition_reached_air(qnum)
    # The fresh question armed; the burned one is consumed and can never
    # come back through arm or draw.
    assert game.armed_question is not None
    assert game.armed_question["id"] == Q_DIAMOND["id"]
    assert game._is_burned(Q_CURIE)
    ids, hashes = game._no_repeat_exclusion()
    assert Q_CURIE["id"] in ids

    # The beat closes on its own last stage and q5 delivers exactly once —
    # no reveal riding on top of it.
    game.session.instructions.clear()
    assert game.dispatch_armed_question(source="post_reveal")
    assert "next_delivery" in game.transition_stages(qnum)
    deliveries = [
        i for i in game.session.instructions
        if Q_DIAMOND["prompt"] in i
    ]
    assert len(deliveries) == 1
    assert _verdict_beats(game) == []


def test_burned_partial_reclaim_with_empty_supply_releases_the_beat():
    """The 05:34-shaped corner: burned partial wedge AND supply still
    starved — the resume must close the beat (terminal next_delivery,
    claim freed) so supply recovery stays reachable, not wedge again."""
    game = _make_game()
    _arm_incident_q4(game)
    qnum = _wedge_partial_burned(game)
    assert game.next_question is None

    spoken_before = list(game.session.instructions)
    _run(lambda: game.adjudicate(
        steal_allowed=False, reclaim_transition=True
    ))

    assert game.session.instructions == spoken_before
    assert game.transition_stages(qnum) == [
        "reveal", "verdict", "next_delivery"
    ]
    assert game.armed_question is None
    assert game.sk.current_question is None
    assert game._open_transition_qnum is None
    assert not game._adjudicating


# ===========================================================================
# 3. The 1C53C6 reclaim is untouched — an UNBURNED dead journal (nothing
#    aired) still reclaims and re-narrates: that recovery lane is the cure
#    there, and gating it on burn must not break it.
# ===========================================================================


def test_unburned_dead_journal_keeps_the_reclaim_renarration():
    game = _make_game()
    _arm_incident_q4(game)
    qnum = game.sk.question_number
    game.sk.close_answer_window()
    game.note_answer_heard(qnum)
    game._judged_keys = {"Rami"}
    assert game.open_question_transition(
        qnum, owner="transition_dead", source="adjudicate"
    )
    game.journal_transition(
        qnum, "reveal", owner="transition_dead",
        detail={"answer": Q_CURIE["canonical_answer"], "correct": False,
                "winner": None, "question_id": Q_CURIE["id"]},
    )
    assert not game._is_burned(Q_CURIE)

    _run(lambda: game.adjudicate(
        steal_allowed=False, reclaim_transition=True
    ))

    # Recovery narrated the beat that never aired — one verdict beat.
    assert len(_verdict_beats(game)) == 1
    assert "verdict" in game.transition_stages(qnum)
