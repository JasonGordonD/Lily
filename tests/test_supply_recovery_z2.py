"""WO-LILY-HOTFIX-008 Z2 — phase-independent supply recovery.

Evidence session `lily-938EFF-2260354c` (the 03:42:56 → 03:47:28 spine):
q2's prefetch FAILED loudly and bounded at 03:43:12 (`PREFETCH_FAILED
error_class=TimeoutError`, 20s wall, honest status note set, returned
None) — and NOTHING recovered. Every supply-recovery rung (IDLE_REARM,
the WS-6 SUPPLY_STALL fallback, IDLE_REPREFETCH, PREFETCH_HARD_TIMEOUT)
lived exclusively in the watchdog's idle branch, reachable only with
nothing armed and no window open. q1 stayed `delivery=armed` /
window-cycling the whole session, so the watchdog took the healthy path
every tick; the fire-and-forget prefetch task was done-and-dead with
`next_question=None`, invisible; zero WATCHDOG lines, exactly one
prefetch attempt all session — while the fallback bank held 464 active
rows. The inline insurance draw ran inside the same dead task and
emitted no telemetry about what it did.

Contract pinned here (Z2):
  1. A failed prefetch schedules its OWN bounded recovery — de-escalated
     re-prefetch, then the curated bank, then ONE honest line with an
     explicit pause offer. Never an open-ended wait, never gated on an
     idle tick that may never come.
  2. The watchdog detects the silent supply window (`next_question`
     None + prefetch task done/absent + game live past the first arm)
     from ANY phase and WARNs the first tick it exists past one tick.
  3. Recovery lands questions on the SUPPLY line only (`next_question`)
     — delivery stays owned by the phase machinery, so q2 arms on
     supply even while q1's delivery is deadlocked (that transition
     deadlock is a separate WO and out of scope here).
  4. Deliberate discards (mode/category switch, duplicates) are NOT
     supply failures and must not burn the retry budget.
  5. The inline insurance draw logs what it did (HIT / EMPTY / ERROR).

Recovery budget asserted below: from the failure event, a fallback
question lands on the supply line within one retry
(`prefetch_total_budget_seconds` ≈ 45s) + one 20s bank draw ≈ 65s —
plus at most 2 watchdog ticks (~20s) of detection when the failure
event itself was missed. The 4.5-minute spine is unreproducible.

Same import boundary as test_supply_fallback.py (pulls in livekit via
lily_agent).
"""

import asyncio
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_audeering_consumers
import lily_persistence
import lily_say_gate
from lily_agent import LilyGame
from lily_scorekeeper import LilyScorekeeper

SESSION_ID = "lily-938EFF-2260354c"

Q1 = {
    "id": "q_armed_1",
    "prompt": "Which planet has the Great Red Spot?",
    "canonical_answer": "Jupiter",
    "acceptable_answers": ["jupiter"],
    "category": "academic",
    "difficulty_tier": 1,
    "reveal_color": "",
}

BANK_Q = {
    "id": "kb_777",
    "prompt": "What is the tallest mountain on Earth above sea level?",
    "canonical_answer": "Mount Everest",
    "acceptable_answers": ["mount everest", "everest"],
    "category": "academic",
    "difficulty_tier": 1,
    "reveal_color": "",
}


class _FakeSession:
    def __init__(self) -> None:
        self.instructions: list[str] = []

    def generate_reply(self, instructions: str) -> None:
        self.instructions.append(instructions)


class _FakeReasoning:
    """Supply that fails on demand — the 03:43:12 TimeoutError shape is a
    prefetch_question that returns None (lily_reasoning catches, notes,
    returns None). Captures the effort kwarg so de-escalation is
    assertable."""

    def __init__(self, results=None) -> None:
        self.calls: list[dict] = []
        self.results = list(results or [])

    async def prefetch_question(self, sk, **kw):
        self.calls.append(kw)
        if self.results:
            result = self.results.pop(0)
        else:
            result = None
        if result is None:
            sk.set_status_note(
                "question machine failure: the next question did not arrive "
                "— tell the table honestly and vamp; do not invent an "
                "explanation"
            )
        return dict(result) if isinstance(result, dict) else result

    async def ensure_choices(self, question):
        return None


def _make_game(mode: str = "general") -> LilyGame:
    """test_supply_fallback._make_game, extended with the REAL
    start_prefetch and a scriptable reasoning fake — the Z2 contract is
    about the real supply task's failure path."""
    game = LilyGame.__new__(LilyGame)
    game.session = _FakeSession()
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game.sk = LilyScorekeeper(SESSION_ID)
    game.sk.mode = mode
    game.game_started = True
    game.game_over = False
    game.armed_question = None
    game.next_question = None
    game.asked_history = []
    game.used_prompts = []
    game.supabase = object()  # non-None sentinel; the bank fetch is patched
    game.reasoning = _FakeReasoning()
    game.group_id = "grp_test"
    game.rounds_total = 3
    game.prewager_standings = None
    game.eliminated = []
    game.ui_phase = "question"
    game._phase_hold = None
    game._adjudicating = False
    game._question_transitioning = False
    game._armed_speech_misses = 0
    game._pending_delivery_qnum = None
    game._undelivered_ticks = 0
    game._undelivered_refires = 0
    game._supply_stall_ticks = 0
    game._prefetch_stall_ticks = 0
    game._armed_limbo_ticks = 0
    game._drawn_ids = set()
    game._drawn_hashes = set()
    game._judged_keys = set()
    game._spec_judge = {}
    game._nbest_by_key = {}
    game._addressee_rows = {}
    game._pre_window_segments = []
    game._prefetch_task = None
    game._category_override = {}
    game._custom_round_registered = {}
    game.promoted_categories = []
    game.acoustic = lily_audeering_consumers.LilyAcousticState()
    game.session_started_at = time.time() - 300.0
    game.instructed_replies: list[str] = []
    game.instructed_reply = lambda text: game.instructed_replies.append(text)
    game._set_ui_phase = lambda phase: None
    game.publish_attributes_nowait = lambda: None
    return game


def _arm_q1_window_open(game: LilyGame) -> None:
    """The exact 03:43 shape: q1 armed, its answer window OPEN and
    cycling, its transition deadlocked elsewhere — delivery is busy, the
    supply line is nobody's problem."""
    game.armed_question = dict(Q1)
    game.sk.start_question(game.armed_question)
    game.sk.answer_window_open = True


def _patch_bank(monkeypatch, script):
    """Scripted lily_fetch_bank_question: pops the next result per call
    (last entry repeats forever). Returns the call log."""
    calls: list[dict] = []
    remaining = list(script)

    async def _fake_fetch(supabase, category, difficulty_tier,
                          exclude_prompts, mode="general",
                          exclude_ids=None, exclude_hashes=None,
                          exclude_answers=None, strict_category=False):
        calls.append({"category": category, "mode": mode})
        result = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        return dict(result) if isinstance(result, dict) else result

    monkeypatch.setattr(
        lily_persistence, "lily_fetch_bank_question", _fake_fetch
    )
    return calls


async def _drain(game: LilyGame, seconds: float = 2.0) -> None:
    """Let the prefetch task and any scheduled recovery ladder run to
    completion (bounded)."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        await asyncio.sleep(0.005)
        tasks = [
            getattr(game, "_prefetch_task", None),
            getattr(game, "_supply_recovery_task", None),
        ]
        live = [t for t in tasks if t is not None and not t.done()]
        if not live and getattr(game, "_supply_recovery_task", None) is not None:
            return
        if not live and game.next_question is not None:
            return


def _run(coro):
    return asyncio.run(coro)


# -- the failure schedules its own recovery (no idle tick required) ------------


def test_spine_2260354c_supply_recovers_while_q1_window_cycles(monkeypatch):
    # The regression spine: q1 armed + window open, q2's prefetch fails,
    # NO watchdog runs at all (zero WATCHDOG lines in the live session) —
    # and q2 still lands on the supply line, because the failure itself
    # scheduled the recovery. Bank script: the insurance draw inside the
    # failing task and the retry's insurance draw come back empty (the
    # live opacity), the recovery ladder's own bank rung hits.
    game = _make_game()
    _arm_q1_window_open(game)
    calls = _patch_bank(monkeypatch, [None, None, BANK_Q])

    async def scenario():
        game.start_prefetch()
        await _drain(game)

    _run(scenario())

    # q2 is ARMED on the supply line even though delivery is blocked.
    assert game.next_question is not None
    assert game.next_question.get("id") == "kb_777"
    # Delivery state untouched: q1 still armed, window still open, no
    # nudge/steal of the deadlocked transition, nothing spoken.
    assert game.armed_question.get("id") == "q_armed_1"
    assert game.sk.answer_window_open is True
    assert game.instructed_replies == []
    # The ladder actually climbed: retry prefetch ran, then the bank rung.
    assert len(game.reasoning.calls) == 2  # original + one bounded retry
    assert len(calls) == 3  # 2 insurance draws + the recovery bank rung
    # Incident closed: budget reset for the next one.
    assert game._supply_retry_attempts == 0


def test_retry_deescalates_effort(monkeypatch):
    # De-escalation on the retry: general medium -> low, adult high ->
    # medium. The original draw passes effort=None (config default).
    for mode, expected in (("general", "low"), ("adult", "medium")):
        game = _make_game(mode=mode)
        _arm_q1_window_open(game)
        _patch_bank(monkeypatch, [None, None, BANK_Q])

        async def scenario():
            game.start_prefetch()
            await _drain(game)

        _run(scenario())
        assert game.reasoning.calls[0].get("effort") is None
        assert game.reasoning.calls[1].get("effort") == expected, mode


def test_total_supply_failure_one_honest_line_and_pause_offer(monkeypatch):
    # Induced total failure: generation fails, bank empty everywhere.
    # Exactly one honest line with an explicit pause offer — never an
    # open-ended wait, never a repeat every tick.
    game = _make_game()
    _arm_q1_window_open(game)
    game.sk.answer_window_open = False  # window between cycles; still armed
    _patch_bank(monkeypatch, [None])

    async def scenario():
        game.start_prefetch()
        await _drain(game)
        # A later detection (watchdog or another failure) must NOT re-fire
        # the line: the incident is already declared.
        game.ensure_supply_recovery(trigger="watchdog_silent_window")
        await _drain(game, seconds=0.2)

    _run(scenario())

    assert game.next_question is None
    assert len(game.instructed_replies) == 1
    line = game.instructed_replies[0].lower()
    assert "pause" in line or "breather" in line
    assert game._supply_exhausted_notified is True
    # The honest state also reaches the state block.
    assert any("pause" in n for n in game.sk.status_notes)


def test_deliberate_discard_does_not_trigger_recovery():
    # A mode-switch discard is a deliberate empty result, not a supply
    # failure: no recovery task, no retry budget burned.
    game = _make_game()
    _arm_q1_window_open(game)

    class _FlippingReasoning(_FakeReasoning):
        def __init__(self, game):
            super().__init__(results=[dict(BANK_Q)])
            self._game = game

        async def prefetch_question(self, sk, **kw):
            result = await super().prefetch_question(sk, **kw)
            self._game.sk.mode = "adult"  # deck flips while draw in flight
            return result

    game.reasoning = _FlippingReasoning(game)

    async def scenario():
        game.start_prefetch()
        await asyncio.sleep(0.05)

    _run(scenario())
    assert game.next_question is None  # MODE_SWITCH_DISCARD
    assert getattr(game, "_supply_recovery_task", None) is None
    assert getattr(game, "_supply_retry_attempts", 0) == 0


# -- insurance telemetry (the zero-telemetry leg of the RCA) -------------------


def test_insurance_bank_hit_is_logged(monkeypatch, caplog):
    game = _make_game()
    _arm_q1_window_open(game)
    _patch_bank(monkeypatch, [BANK_Q])

    async def scenario():
        game.start_prefetch()
        await _drain(game, seconds=0.5)

    with caplog.at_level(logging.INFO, logger="lily_agent"):
        _run(scenario())
    assert game.next_question is not None
    assert any("INSURANCE_BANK_HIT" in r.message for r in caplog.records)
    # A successful insurance draw is not a failure: no recovery scheduled.
    assert getattr(game, "_supply_recovery_task", None) is None


def test_insurance_bank_empty_is_logged(monkeypatch, caplog):
    game = _make_game()
    _arm_q1_window_open(game)
    _patch_bank(monkeypatch, [None, None, BANK_Q])

    async def scenario():
        game.start_prefetch()
        await _drain(game)

    with caplog.at_level(logging.INFO, logger="lily_agent"):
        _run(scenario())
    assert any("INSURANCE_BANK_EMPTY" in r.message for r in caplog.records)


# -- the watchdog detects the silent window from ANY phase ---------------------


def test_watchdog_warns_and_recovers_supply_while_delivery_busy(
    monkeypatch, caplog
):
    # The backstop shape: the prefetch task died without its failure hook
    # (simulated: task absent, next_question None) while q1 is armed with
    # its window open. The watchdog must WARN on the first tick the
    # silent window exists past one tick and land supply via the ladder —
    # the healthy-path blindness that let 2260354c starve for 4.5 minutes.
    game = _make_game()
    _arm_q1_window_open(game)
    # No original prefetch ran (the task died trackless): the ladder makes
    # 2 bank calls — the retry's insurance draw (empty) and its own rung.
    calls = _patch_bank(monkeypatch, [None, BANK_Q])
    game.WATCHDOG_INTERVAL_SECONDS = 0.01

    async def scenario():
        task = asyncio.ensure_future(game._idle_watchdog())
        try:
            for _ in range(400):  # up to ~2s wall clock
                if game.next_question is not None:
                    break
                await asyncio.sleep(0.005)
        finally:
            game.game_over = True
            await task
        return game

    with caplog.at_level(logging.INFO, logger="lily_agent"):
        _run(scenario())

    assert game.next_question is not None
    assert game.next_question.get("id") == "kb_777"
    # Delivery untouched — the recovery landed SUPPLY only.
    assert game.armed_question.get("id") == "q_armed_1"
    assert game.sk.answer_window_open is True
    warned = [
        r for r in caplog.records if "SUPPLY_SILENT_WINDOW" in r.message
    ]
    assert warned, "the silent window must WARN the first tick it exists"
    assert warned[0].levelno == logging.WARNING
    assert len(calls) >= 1


def test_spine_timeline_no_unbounded_wait(monkeypatch):
    # The 03:42:56 -> 03:47:28 spine as a timeline: 28 watchdog ticks with
    # the supply line dead and delivery busy. Pre-Z2 this produced ZERO
    # recovery actions (the healthy path every tick). Now: within the
    # stated budget (2 detection ticks + 1 retry + 1 bank draw) a
    # fallback attempt MUST have happened — there is no path from
    # "prefetch dead" to "player waits out the spine" without one.
    game = _make_game()
    _arm_q1_window_open(game)
    calls = _patch_bank(monkeypatch, [None, BANK_Q])
    game.WATCHDOG_INTERVAL_SECONDS = 0.01

    async def scenario():
        task = asyncio.ensure_future(game._idle_watchdog())
        # 28 compressed ticks — the whole live spine.
        await asyncio.sleep(0.01 * 28)
        game.game_over = True
        await task

    _run(scenario())
    # A fallback attempt happened (retry prefetch and/or bank draws)...
    assert len(game.reasoning.calls) >= 1
    assert len(calls) >= 1
    # ...and it worked: q2 armed on the supply line, q1 untouched.
    assert game.next_question is not None
    assert game.armed_question.get("id") == "q_armed_1"


def test_silent_window_predicate_boundaries():
    game = _make_game()
    # Pre-first-arm: not a supply incident (game-start flow owns it).
    assert game.sk.question_number == 0
    assert game._supply_silent_window() is False
    # Past the first arm with a dead supply line: incident, regardless of
    # window state.
    _arm_q1_window_open(game)
    assert game._supply_silent_window() is True
    game.sk.answer_window_open = False
    assert game._supply_silent_window() is True
    # A prefetched question or a live prefetch task ends the window.
    game.next_question = dict(BANK_Q)
    assert game._supply_silent_window() is False
    game.next_question = None

    async def check_live_task():
        async def _hang():
            await asyncio.sleep(10)

        game._prefetch_task = asyncio.ensure_future(_hang())
        try:
            return game._supply_silent_window()
        finally:
            game._prefetch_task.cancel()

    assert _run(check_live_task()) is False
