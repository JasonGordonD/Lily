"""WO-LILY-HOTFIX-008 Z2c — release the question-transition claim when
narration is complete but supply is empty.

THE live deadlock (session lily-938EFF-2260354c, 03:42:56→03:47:28, q1
"What is the chemical symbol for the precious metal gold?"): Rami
answered correctly, Lily's own conversational turn performed the verdict
organically ("Au! That's gold. Point to you...") — ORGANIC_PREEMPTED —
and the q2 prefetch had already FAILED (03:43:12, TimeoutError). The
adjudication opened `q_1_transition`, journaled reveal+verdict (organic,
confirmed on air) and then died before consuming the question, leaving
ONE circular wait:

  - the transition's completion marker (next_delivery) only journals when
    a question dispatches — which needs supply;
  - supply recovery lives behind the idle watchdog — which needs the game
    idle;
  - idle needs the transition closed and the question consumed.

Meanwhile the confirmed `q_1_delivery` claim made every post-ruling agent
turn REOPEN the dead question's window (`LILY_WINDOW | OPEN
reason=delivery_claim`, the phase=reveal→answering regression), feeding
candidates back into adjudication, and every re-entered adjudication
found the transition "already owned and narrated" — SECOND_LANE_REFUSED,
12 times, ~4.5 minutes of honest vamping and zero questions.

The fix, at source (no new refusal guards):
  1. a narration-complete transition (reveal+verdict journaled, verdict
     provably aired) that cannot arm N+1 is RELEASED — terminal
     next_delivery marker journaled (delivered_q=None), claim freed, open
     slot cleared — from the arm-failed branch, the post_reveal playout
     seam, and the recovery re-entry (RESUMED_COMPLETE: recovery resumes
     the aired beat's bookkeeping instead of refusing);
  2. the delivery_claim window reopen refuses a TERMINAL question
     (adjudication owns it — a ruled question has no answer window); the
     legitimate first open lands in the same branch pre-adjudication and
     is untouched.

The lily-1C53C6 reclaim (mech 55/56: a journaled transition whose speech
NEVER aired is reclaimed fresh) is protected below in both directions —
_transition_reached_air, SECOND_LANE_REFUSED and ARMED_LIMBO are
deliberately unmodified.

Same import boundary note as test_hotfix006_transitions.py.
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
    """The incident's supply state: the q2 prefetch FAILED and nothing is
    prefetched — every draw returns None."""

    async def prefetch_question(self, sk, **kw):
        return None

    async def prefetch_picture_question(self, supabase, **kw):
        return None

    async def judge(self, *a, **kw):
        return '{"verdict": "incorrect", "reason": "not an answer"}'


# The real q1 of the incident session (id from the 03:42:56 durable-asked
# row).
Q_GOLD = {
    "id": "q_1732",
    "prompt": "What is the chemical symbol for the precious metal gold?",
    "canonical_answer": "Au",
    "acceptable_answers": ["au"],
    "category": "academic",
}

# Representative of speech_68228cc98f06 — the organic verdict turn
# (answer + verdict cue, which is what _verdict_already_spoken keys on).
ORGANIC_VERDICT = (
    "Rami — A like alpha, umbrella... Au! Correct — that's gold. "
    "Point to you, you're on the board."
)


def _make_game(session_id: str = "lily-938EFF-2260354c") -> LilyGame:
    game = LilyGame.bare()
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


def _arm_incident_q1(game: LilyGame) -> float:
    """q1 armed, delivered to playout, delivery claim CONFIRMED (the
    03:43:07 window open rode exactly this claim), window open, Rami's
    correct final in — the state the 03:43:38 adjudication started from."""
    game.sk.bind_speaker("S1", "Rami")
    game.armed_question = dict(Q_GOLD)
    game.sk.start_question(game.armed_question)
    game.sk.round = 1
    game.sk.set_phase("round")
    game.ui_phase = "question"
    delivered = getattr(game, "_delivered_to_playout", None)
    if delivered is None:
        delivered = game._delivered_to_playout = set()
    delivered.add(game.sk.question_number)
    key = f"q_{game.sk.question_number}_delivery"
    game.say_registry.claim(key, owner="speech_delivery_q1")
    game.say_registry.confirm(key)
    now = time.time()
    game.sk.open_answer_window(
        duration=30.0, now=now, question_id=Q_GOLD["id"],
        question_index=game.sk.question_number, registered=True,
    )
    game.sk.on_transcript_segment(
        text="Au.", speaker_label="S1", is_final=True,
        now=now + 2, segment_start_time=now + 2, segment_end_time=now + 2.5,
    )
    # Her organic verdict is the last finished turn — ORGANIC_PREEMPTED.
    game._last_assistant_text = ORGANIC_VERDICT
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


def _terminal_stage(game: LilyGame, qnum: int) -> dict | None:
    for entry in game.transition_journal(qnum):
        if entry["stage"] == "next_delivery":
            return entry
    return None


def _assert_released_and_idle_reachable(game: LilyGame, qnum: int) -> None:
    """The Z2c postcondition: beat closed, claim freed, question consumed,
    and the idle watchdog's supply-recovery preconditions (nothing armed,
    no window, no ruling in flight) all hold."""
    assert game.transition_stages(qnum) == [
        "reveal", "verdict", "next_delivery"
    ]
    terminal = _terminal_stage(game, qnum)
    assert terminal is not None
    assert terminal["detail"]["delivered_q"] is None
    assert game._open_transition_qnum is None
    assert game.say_registry.state(game.transition_key(qnum)) in (
        None, lily_say_gate.CLAIM_CONFIRMED
    )
    assert game.armed_question is None
    assert game.sk.current_question is None
    assert not game.sk.answer_window_open
    assert not game._adjudicating


# ===========================================================================
# 1. The WO replay — organic verdict + arm_next_question failure, the
#    adjudication running to completion. The transition RELEASES at the
#    arm-failed branch.
# ===========================================================================


def test_organic_verdict_with_empty_supply_releases_the_transition():
    game = _make_game()
    _arm_incident_q1(game)
    assert game.next_question is None  # PREFETCH_FAILED at 03:43:12

    _run(lambda: game.adjudicate(steal_allowed=True))

    # The ruling committed (the real SCORE_COMMIT: Rami, correct, 1).
    assert game.sk.players["Rami"]["score"] == 1
    _assert_released_and_idle_reachable(game, 1)
    assert _terminal_stage(game, 1)["detail"]["source"] == (
        "supply_empty_at_arm"
    )
    # The release must NOT have handed the beat to a second lane: the
    # aired verdict still refuses re-narration, in BOTH modes (this is the
    # aired-verdict gap that reopened Chain B — recovery may still not
    # re-narrate what played).
    assert not game.open_question_transition(
        1, owner="second_lane", source="test"
    )
    assert not game.open_question_transition(
        1, owner="second_lane", source="test", reclaim_unaired=True
    )


def test_release_leaves_the_game_where_a_fresh_transition_can_open():
    """The point of the release: when supply returns, question TWO opens
    its own transition normally — nothing about q1's closed beat blocks
    the next one."""
    game = _make_game()
    _arm_incident_q1(game)
    _run(lambda: game.adjudicate(steal_allowed=True))
    _assert_released_and_idle_reachable(game, 1)

    assert game.open_question_transition(
        2, owner="q2_lane", source="adjudicate"
    )
    assert game._open_transition_qnum == 2


# ===========================================================================
# 2. The TRUE incident wedge — the first adjudication died mid-beat (its
#    publish await never returned; CancelledError skips the CRASHED log),
#    leaving the transition open [reveal, verdict], the question still
#    armed, and adjudication marked done. LILY_SPINE at 03:43:44.668:
#    phase=answering q=1 delivery=armed window=open — six seconds AFTER
#    the transition opened.
# ===========================================================================


def _wedge_incident_state(game: LilyGame):
    """Reproduce the wedge with the real machinery: adjudicate reaches the
    reveal publish and the task is cancelled there."""
    hang = asyncio.Event()

    async def _hanging_publish(question_text, **kwargs):
        await hang.wait()

    real_publish = game.publish_metadata
    game.publish_metadata = _hanging_publish

    async def _go():
        task = asyncio.ensure_future(game.adjudicate(steal_allowed=True))
        for _ in range(20):
            await asyncio.sleep(0)
            if game.transition_stages(1) == ["reveal", "verdict"]:
                break
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        game.publish_metadata = real_publish

    return _go


def test_cancelled_adjudication_wedge_no_window_reopen_for_ruled_question():
    """Item 2 alone, on the wedge: the organic turn's playout completion
    must NOT reopen the ruled question's window off its confirmed
    delivery claim (the 03:43:44 phase regression)."""
    game = _make_game()
    _arm_incident_q1(game)

    def _scenario():
        async def _go():
            await _wedge_incident_state(game)()
            # The wedge, as the spine recorded it.
            assert game.armed_question is not None
            assert game.transition_stages(1) == ["reveal", "verdict"]
            assert not game._adjudicating
            assert not game.sk.answer_window_open
            # The organic verdict turn finishes playing out.
            opens: list = []
            game.open_window_after_discharge = (
                lambda *a, **k: opens.append(1)
            )
            game.on_agent_speech_finished(
                ORGANIC_VERDICT, speech_id="speech_organic"
            )
            assert opens == []
            assert not game.sk.answer_window_open
        return _go()

    _run(_scenario)


def test_recovery_resumes_the_aired_beat_and_releases_it():
    """The forced recovery adjudication (ARMED_LIMBO's reclaim path) on
    the wedge: instead of SECOND_LANE_REFUSED it RESUMES the
    narration-complete transition — re-airing NOTHING — consumes the
    question, fails to arm (supply empty) and releases the beat. The
    stall is over: supply recovery is reachable."""
    game = _make_game()
    _arm_incident_q1(game)

    def _scenario():
        async def _go():
            await _wedge_incident_state(game)()
            spoken_before = list(game.session.instructions)
            await game.adjudicate(
                steal_allowed=False, reclaim_transition=True
            )
            # Nothing re-aired: no verdict beat, no reveal, no flourish.
            assert game.session.instructions == spoken_before
        return _go()

    _run(_scenario)

    _assert_released_and_idle_reachable(game, 1)
    # No double award on the re-entered adjudication.
    assert game.sk.players["Rami"]["score"] == 1
    # The consumed question is burned — it can never re-arm.
    assert game._is_burned(Q_GOLD)


def test_recovery_resume_requires_the_reclaim_flag():
    """A NORMAL adjudication (window-close lane, reclaim_transition
    False) over the wedge keeps today's refusal — resume is recovery's
    move only, exactly like reclaim_unaired."""
    game = _make_game()
    _arm_incident_q1(game)

    def _scenario():
        async def _go():
            await _wedge_incident_state(game)()
            await game.adjudicate(steal_allowed=False)
        return _go()

    _run(_scenario)

    # Refused: the beat is untouched and the question unconsumed.
    assert game.transition_stages(1) == ["reveal", "verdict"]
    assert game.armed_question is not None


# ===========================================================================
# 3. Chain B must not reopen in the other direction — the lily-1C53C6
#    reclaim (mech 55/56): a journaled transition whose speech NEVER
#    aired is still reclaimed fresh, and is NEVER "released complete".
# ===========================================================================


def test_unaired_transition_still_reclaims_and_never_releases():
    game = _make_game()
    game.sk.question_number = 2
    assert game.open_question_transition(
        2, owner="dead_lane", source="adjudicate"
    )
    game.journal_transition(2, "reveal", owner="dead_lane", detail={})
    # Verdict journaled, dispatched — but its key never confirmed and no
    # narration ever bound: the speech died before playout.
    game.say_registry.claim("q_2_reveal", owner="dead_lane")
    game.journal_transition(
        2, "verdict", owner="dead_lane",
        detail={"key": "q_2_reveal", "narration": None},
    )

    # Not narration-complete: the release must refuse it.
    assert not game.transition_narration_complete(2)
    assert not game.release_completed_transition(2, reason="test")
    assert game.transition_stages(2) == ["reveal", "verdict"]

    # The watchdog reclaim still gets it (RECLAIMED_UNAIRED).
    assert game.open_question_transition(
        2, owner="recovery_lane", source="adjudicate", reclaim_unaired=True
    )
    assert game.transition_stages(2) == []
    assert game._open_transition_qnum == 2


def test_release_refuses_incomplete_or_already_closed_beats():
    game = _make_game()
    game.sk.question_number = 1
    assert game.open_question_transition(1, owner="lane", source="test")
    # Reveal only — no verdict yet.
    game.journal_transition(1, "reveal", owner="lane", detail={})
    assert not game.release_completed_transition(1, reason="test")
    # Aired verdict — now complete, releases once...
    game.journal_transition(
        1, "verdict", owner="lane",
        detail={"key": None, "narration": "the verdict that played"},
    )
    assert game.release_completed_transition(1, reason="test")
    # ...and only once (the beat is already closed).
    assert not game.release_completed_transition(1, reason="test")
    assert game.transition_stages(1) == [
        "reveal", "verdict", "next_delivery"
    ]


# ===========================================================================
# 4. The terminal-reopen refusal never touches the question's legitimate
#    FIRST open (same branch, pre-adjudication).
# ===========================================================================


def test_first_window_open_from_delivery_claim_is_untouched():
    game = _make_game()
    game.sk.bind_speaker("S1", "Rami")
    game.armed_question = dict(Q_GOLD)
    game.sk.start_question(game.armed_question)
    game.sk.round = 1
    game.sk.set_phase("round")
    game.ui_phase = "question"
    key = f"q_{game.sk.question_number}_delivery"
    game.say_registry.claim(key, owner="speech_delivery_q1")
    game.say_registry.confirm(key)

    opens: list = []
    game.open_window_after_discharge = lambda *a, **k: opens.append(1)

    def _scenario():
        game.on_agent_speech_finished(
            Q_GOLD["prompt"], speech_id="speech_delivery_q1"
        )

    _run(_scenario)
    # Pre-adjudication: not terminal, the delivery-claim open fires.
    assert opens == [1]

    # After adjudication owns the question, the same seam refuses.
    game.note_answer_heard(game.sk.question_number)

    def _again():
        game.on_agent_speech_finished(
            "Anytime, champ.", speech_id="speech_banter"
        )

    _run(_again)
    assert opens == [1]
    assert not game.sk.answer_window_open


# ===========================================================================
# 5. Combined Z2 + Z2c end-to-end: the 03:42:56→03:47:28 stall is
#    unreproducible. Prefetch fails, the transition releases, Z2's
#    delivery-state-independent bank draw lands q2 on the supply line, q2
#    arms and delivers, and a FRESH transition opens for it.
# ===========================================================================


BANK_Q2 = {
    "id": "kb_777",
    "prompt": "What is the tallest mountain on Earth above sea level?",
    "canonical_answer": "Mount Everest",
    "acceptable_answers": ["mount everest", "everest"],
    "category": "academic",
    "difficulty_tier": 1,
    "reveal_color": "",
}


def test_combined_z2_z2c_the_2260354c_stall_is_unreproducible(monkeypatch):
    game = _make_game()
    _arm_incident_q1(game)

    # Z2 seam: the curated bank has rows (464 active in prod while the
    # incident vamped); generation stays starved.
    async def _fake_fetch(supabase, category, difficulty_tier,
                          exclude_prompts, mode="general",
                          exclude_ids=None, exclude_hashes=None,
                          exclude_answers=None, strict_category=False):
        return dict(BANK_Q2)

    monkeypatch.setattr(
        lily_persistence, "lily_fetch_bank_question", _fake_fetch
    )

    def _scenario():
        async def _go():
            # The incident beat: organic verdict, arm fails (no supply),
            # the transition RELEASES instead of holding the game.
            await game.adjudicate(steal_allowed=True)
            _assert_released_and_idle_reachable(game, 1)
            # Z2: the bank draw reaches the supply line with no idle tick
            # required and no delivery-state coupling.
            game.supabase = object()  # sentinel; the fetch is patched
            assert await game._bank_to_supply(
                trigger="recovery:test"
            ) == "supplied"
            assert game.next_question is not None
            assert game.next_question.get("id") == "kb_777"
            # The idle watchdog's re-arm rung (reachable BECAUSE the
            # transition released) arms and delivers q2.
            assert game.arm_next_question()
            assert game.sk.question_number == 2
            game.supabase = None  # checkpoint writes are out of scope
            assert game.dispatch_armed_question(source="idle_watchdog")
        return _go()

    _run(_scenario)

    # q2 went out as a question-only delivery of the bank row.
    assert any(
        BANK_Q2["prompt"] in i for i in game.session.instructions
    )
    # The structural delivery intent is armed for q2 (the claim itself
    # registers at speech_created in the live pipeline, outside this
    # harness).
    assert game._pending_delivery_qnum == 2
    # And q2's transition opens FRESH — q1's closed beat blocks nothing,
    # re-narrates nothing.
    assert game.open_question_transition(
        2, owner="q2_lane", source="adjudicate"
    )
    assert not game.open_question_transition(
        1, owner="stray_lane", source="test"
    )
