"""WO-LILY-HOTFIX-009 W4 — relaxed pacing reaches the window and steal
machinery: no window expiry, no steal clock, no answer rejected on timing.

THE live incident (session lily-5E3036-b56b5eb4, relaxed/no-timer solo,
durable group pref pacing=relaxed, q2 kb_450 mitochondria):

  05:31:31  q_2_delivery airs: "Often called the powerhouse of the cell,
            what is this energy-producing cellular organelle?"
  05:31:35  Rami: "I never can remember the name of it. So I'm. I'm
            surrendering." (lily_answers id=131, verdict incorrect)
  05:32:11  transcripts id=3484: "Nobody locked it. Steal window — five
            seconds, anyone else want it?" — a five-second steal clock
            armed during relaxed play (addressee_log id=799,
            agent_action=adjudicated_other).
  05:32:58  id=3487, her own concession: "You asked for relaxed, and I
            tossed a five-second steal clock at you anyway. That's on me.
            No timers. Relaxed means relaxed."

The mode was honoured in prose and ignored by the machinery: sk.pacing
existed, was published to the glass, and stretched exactly one number
(the window base — ×LILY_RELAXED_WINDOW_MULTIPLIER), while the steal
branch, the _expire timer, and the late-answer rejection gate never read
it. Fix under test (W4): relaxed pacing disarms the clocks in code —
windows open with no deadline and no expiry task, the steal branch never
arms, nothing is filed late — and the beat closes on people instead
(_maybe_close_relaxed_beat: every rostered player answered, no clarify
pending -> adjudicate). Timed mode is byte-identical.

Roster note: the steal fixture rosters the REAL ghost shape ("Rummy"
captured at 05:29:30 was never replaced when "Rami" was NATO-spelled —
one voiceprint, two rostered players), because that is exactly why
stealers_exist read True on a solo table. W7 owns the roster truth; W4
must refuse the clock even with the wrong roster.

Same import boundary note as test_hotfix006_transitions.py.
"""

import pytest

import asyncio
import time
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_audeering_consumers
import lily_config
import lily_say_gate
from lily_agent import LilyGame
from lily_scorekeeper import LilyScorekeeper


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
    """The judge rules the surrender incorrect (lily_answers id=131)."""

    async def prefetch_question(self, sk, **kw):
        return None

    async def prefetch_picture_question(self, supabase, **kw):
        return None

    async def judge(self, *a, **kw):
        return '{"verdict": "incorrect", "reason": "not an answer"}'


# The real q2 of the incident session (id from the 05:31:25 asked_history
# row; prompt from the q_2_delivery transcript id=3481).
Q_MITO = {
    "id": "kb_450",
    "prompt": (
        "Often called the powerhouse of the cell, what is this "
        "energy-producing cellular organelle?"
    ),
    "canonical_answer": "mitochondria",
    "acceptable_answers": ["mitochondria", "the mitochondria"],
    "category": "academic",
}

# The real q5 (q_7391 diamond) — used by the no-late-rejection fixture;
# the utterance is Rami's actual in-session diamond answer (05:37:23).
Q_DIAMOND = {
    "id": "q_7391",
    "prompt": (
        "What is the hardest naturally occurring substance found on Earth?"
    ),
    "canonical_answer": "diamond",
    "acceptable_answers": ["diamond"],
    "category": "academic",
}

# transcripts id=3482 — the surrender that became the sole candidate.
SURRENDER = "I never can remember the name of it. So I'm. I'm surrendering."
# transcripts / addressee_log id=819 — the correct diamond answer.
DIAMOND_ANSWER = "Answer to your fucking question is diamond."


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
    game.group_id = "grp_3dc3770e9d0b1533"
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


def _arm_q2(game: LilyGame, *, ghost_roster: bool) -> float:
    """q2 armed, delivered to playout, claim CONFIRMED — the 05:31:31
    state. ghost_roster=True reproduces the real Rummy/Rami double
    roster (one human, two rostered names)."""
    if ghost_roster:
        game.sk.bind_speaker("S1", "Rummy")  # 05:29:30 mis-capture
    game.sk.bind_speaker("S1", "Rami")       # 05:30:14 NATO lock
    game.armed_question = dict(Q_MITO)
    game.sk.start_question(game.armed_question)
    game.sk.round = 1
    game.sk.set_phase("round")
    game.ui_phase = "question"
    delivered = getattr(game, "_delivered_to_playout", None)
    if delivered is None:
        delivered = game._delivered_to_playout = set()
    delivered.add(game.sk.question_number)
    key = f"q_{game.sk.question_number}_delivery"
    game.say_registry.claim(key, owner="speech_delivery_q2")
    game.say_registry.confirm(key)
    return time.time()


def _run(step):
    async def _scenario():
        out = step
        if callable(out):
            out = out()
        if asyncio.iscoroutine(out):
            out = await out
        # Drain by SETTLING, not by counting ticks. The original two
        # sleep(0) ticks encoded a Python-scheduler implementation detail:
        # adjudicate's reveal-publish `await asyncio.gather(...)` resolves
        # within two ticks on 3.11 and needs a third on 3.13 — which made
        # this harness cancel adjudication mid-reveal ON CI ONLY and
        # blocked every deploy from 2026-08-10 09:14 to 08-12 (the W4
        # landing run itself was the first red). sleep(0) is a scheduler
        # yield, never a timer, so W4's no-clock invariant is untouched;
        # the bound exists only so a genuinely stuck task cannot hang the
        # suite (it is cancelled below exactly as before).
        for _ in range(25):
            if not [
                t for t in asyncio.all_tasks()
                if t is not asyncio.current_task()
            ]:
                break
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


# ===========================================================================
# 1. The 05:32:11 steal clock — a missed question in relaxed mode must
#    never arm a steal window, even with the real (wrong) two-name
#    roster that made stealers_exist read True.
# ===========================================================================


def test_relaxed_missed_question_never_arms_steal_clock():
    game = _make_game()
    now = _arm_q2(game, ghost_roster=True)
    game.sk.set_pacing("relaxed")
    assert len(game.sk.players) == 2  # the real ghost shape (W7's fault)
    game.sk.open_answer_window(
        duration=None, now=now, question_id=Q_MITO["id"],
        question_index=game.sk.question_number, registered=True,
    )
    game.sk.on_transcript_segment(
        text=SURRENDER, speaker_label="S1", is_final=True,
        now=now + 4, segment_start_time=now + 4, segment_end_time=now + 6,
    )

    _run(lambda: game.adjudicate(steal_allowed=True))

    assert game._steal_window is False
    # W2a: the steal opener is a deterministic say sheet — scan both lanes so
    # the "no steal beat" assertion stays honest.
    steal_beats = [
        x for x in (game.session.instructions + game.session.said)
        if "steal" in x.lower()
    ]
    assert steal_beats == []  # 05:32:11's "Steal window — five seconds"
    # The beat fell through to the ordinary reveal instead of parking on
    # a clock: the ruling committed and the transition ran.
    assert not game.sk.answer_window_open
    # REFACTOR W2a: the reveal answer now airs in the deterministic verdict
    # sheet ("Nobody landed it — it was mitochondria.") on the direct_say lane.
    assert any(
        "mitochondria" in s.lower() for s in game.session.said
    )


# ===========================================================================
# 2. Relaxed windows are untimed: no deadline, no expiry task. ("No
#    timers. Relaxed means relaxed.")
# ===========================================================================


def test_relaxed_window_opens_with_no_deadline_and_no_expiry_task():
    game = _make_game()
    _arm_q2(game, ghost_roster=False)
    game.sk.set_pacing("relaxed")

    def _go():
        game.open_window()

    _run(_go)

    assert game.sk.answer_window_open
    assert game.sk.answer_window_deadline is None
    assert game._window_timer is None
    # An hour later the window is still open — nothing expires it.
    assert game.sk.is_window_open(now=time.time() + 3600.0)
    assert game.sk.window_contains(time.time() + 3600.0)


def test_timed_window_still_arms_exactly_todays_clock(monkeypatch):
    monkeypatch.delenv("LILY_ANSWER_WINDOW_SECONDS", raising=False)
    game = _make_game()
    _arm_q2(game, ghost_roster=False)
    assert game.sk.pacing == "timed"  # the default

    def _go():
        game.open_window()

    _run(_go)

    assert game.sk.answer_window_open
    assert game.sk.answer_window_deadline is not None
    deadline_in = game.sk.answer_window_deadline - time.time()
    assert 0 < deadline_in <= lily_config.answer_window_seconds() + 1.0
    assert game._window_timer is not None


# ===========================================================================
# 3. No answer is rejected on timing in relaxed mode: the late-answer
#    gate (the machinery behind "just past the window, so it doesn't
#    score") never files in relaxed mode.
# ===========================================================================


def test_relaxed_correct_answer_is_never_filed_late():
    game = _make_game()
    now = time.time()
    game.sk.bind_speaker("S1", "Rami")
    game.armed_question = dict(Q_DIAMOND)
    game.sk.start_question(game.armed_question)
    game.sk.set_pacing("relaxed")
    delivered = getattr(game, "_delivered_to_playout", None)
    if delivered is None:
        delivered = game._delivered_to_playout = set()
    delivered.add(game.sk.question_number)
    # A closed window over the diamond question, with a deadline in the
    # PAST from timed history (_last_window_deadline is remembered across
    # close precisely so lateness is measurable — N9).
    game.sk.open_answer_window(
        duration=15.0, now=now - 60.0, question_id=Q_DIAMOND["id"],
        question_index=game.sk.question_number, registered=True,
    )
    game.sk.close_answer_window()

    record = game.note_late_answer(
        DIAMOND_ANSWER,
        player="Rami",
        speaker_label="S1",
        segment_ts=now,  # 45s past that deadline
    )

    assert record is None
    assert game.sk.late_answers == []
    assert getattr(game, "_late_answer_note", None) is None


def test_timed_late_gate_unchanged():
    game = _make_game()
    now = time.time()
    game.sk.bind_speaker("S1", "Rami")
    game.armed_question = dict(Q_DIAMOND)
    game.sk.start_question(game.armed_question)
    delivered = getattr(game, "_delivered_to_playout", None)
    if delivered is None:
        delivered = game._delivered_to_playout = set()
    delivered.add(game.sk.question_number)
    game.sk.open_answer_window(
        duration=15.0, now=now - 60.0, question_id=Q_DIAMOND["id"],
        question_index=game.sk.question_number, registered=True,
    )
    game.sk.close_answer_window()

    record = game.note_late_answer(
        DIAMOND_ANSWER, player="Rami", speaker_label="S1", segment_ts=now,
    )

    assert record is not None
    assert record["verdict"] == "correct"
    assert record["seconds_late"] > 0
    assert len(game.sk.late_answers) == 1


# ===========================================================================
# 4. Without a clock the relaxed beat closes on PEOPLE: once every
#    rostered player has an answer in, adjudication fires through the
#    same seam the instant-Tier-1 path uses. Solo table (the truthful
#    roster): one answer — right, wrong, or a pass — closes the beat.
# ===========================================================================


def test_relaxed_solo_wrong_answer_adjudicates_without_any_clock():
    game = _make_game()
    now = _arm_q2(game, ghost_roster=False)
    game.sk.set_pacing("relaxed")

    def _go():
        game.open_window()
        assert game.sk.answer_window_deadline is None  # nothing to wait on
        result = game.sk.on_transcript_segment(
            text=SURRENDER, speaker_label="S1", is_final=True,
            now=now + 4, segment_start_time=now + 4, segment_end_time=now + 6,
        )
        game.on_transcript_event(
            result, SURRENDER, speaker_label="S1", segment_ts=now + 4,
        )

    _run(_go)

    # The beat closed: ruling committed, window gone, reveal aired — with
    # no timer anywhere (the only closure was the roster completing).
    assert not game.sk.answer_window_open
    # REFACTOR W2a: the reveal answer now airs in the deterministic verdict
    # sheet ("Nobody landed it — it was mitochondria.") on the direct_say lane.
    assert any(
        "mitochondria" in s.lower() for s in game.session.said
    )
    assert game._steal_window is False


def test_relaxed_multiplayer_waits_for_the_roster():
    """Two ROSTERED players, one answer in: the untimed beat stays open
    (no clock may close it) until the second player answers; then it
    adjudicates."""
    game = _make_game()
    now = _arm_q2(game, ghost_roster=False)
    game.sk.bind_speaker("S2", "Maria")
    game.sk.set_pacing("relaxed")

    def _first():
        game.open_window()
        result = game.sk.on_transcript_segment(
            text=SURRENDER, speaker_label="S1", is_final=True,
            now=now + 4, segment_start_time=now + 4, segment_end_time=now + 6,
        )
        game.on_transcript_event(
            result, SURRENDER, speaker_label="S1", segment_ts=now + 4,
        )

    _run(_first)
    assert game.sk.answer_window_open  # still waiting on Maria, untimed

    def _second():
        result = game.sk.on_transcript_segment(
            text="Is it the nucleus?", speaker_label="S2", is_final=True,
            now=now + 9, segment_start_time=now + 9, segment_end_time=now + 10,
        )
        game.on_transcript_event(
            result, "Is it the nucleus?", speaker_label="S2",
            segment_ts=now + 9,
        )

    _run(_second)
    assert not game.sk.answer_window_open  # roster complete -> adjudicated


# ===========================================================================
# 5. Timed mode steal machinery is untouched for a LEGITIMATE steal: a real
#    second hearable player (S2/Maria) has not answered, so the missed
#    question opens the steal window exactly as today. The ghost shape rides
#    along (Rummy's label was nulled by the NATO correction) to prove W5
#    discounts it: two ROSTERED names but two HEARABLE people, and the steal
#    arms on the real second voiceprint, not on the ghost.
#    (Before W5 this test rostered only the ghost solo table and still armed
#    a steal — that was the W5 defect; a table of one hearable person now
#    never steals, in any pacing. See test_hotfix009_w5_solo_steal.py.)
# ===========================================================================


def test_timed_missed_question_still_opens_the_steal_window():
    game = _make_game()
    now = _arm_q2(game, ghost_roster=True)
    game.sk.bind_speaker("S2", "Maria")  # a real, distinct second voiceprint
    assert game.sk.pacing == "timed"
    game.sk.open_answer_window(
        duration=15.0, now=now, question_id=Q_MITO["id"],
        question_index=game.sk.question_number, registered=True,
    )
    # Only Rami answers (and misses); Maria stays silent -> eligible stealer.
    game.sk.on_transcript_segment(
        text=SURRENDER, speaker_label="S1", is_final=True,
        now=now + 4, segment_start_time=now + 4, segment_end_time=now + 6,
    )

    _run(lambda: game.adjudicate(steal_allowed=True))

    assert game._steal_window is True
    assert game.sk.answer_window_open
    assert any(
        "steal" in x.lower()
        for x in (game.session.instructions + game.session.said)
    )
