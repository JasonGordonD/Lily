"""WO-LILY-BIND-DISPUTE-001 — inverted scrutiny, the missing dispute
concept, and solo-relaxed instant adjudication.

Two live calls reproduced the same P0s, both committed as fixtures
(tests/fixtures/live_20260814_1751_gameflow.txt — lily-FD3994-358c0ac8;
tests/fixtures/live_20260815_1347_gameflow.txt — lily-359C62-5613a25a):

DEFECT 1 — INVERTED SCRUTINY (08-14 q4). "What's he. Face. Uh."
(Tier-1 sim 0.143 vs Oscar Wilde — BAND_REJECT) HARD-BOUND ("Locked
in—") and burned the question, while "The. State." (0.769 —
BAND_CLARIFY) got the "answer, or thinking out loud?" check. Resembling
the answer bought scrutiny; resembling NOTHING bought a lock. The gap
was DECLARED OPEN at CHANGELOG "a fragment/completeness rule needs its
own clause". Fix under test: on an open question a BAND_REJECT
utterance with no COMMITTED ANSWER SHAPE (wh-search / disfluency tail /
fragment tail) routes into the existing pending-clarify machinery and
its bind is WITHDRAWN; the receipt stands down. Committed wrong answers
("Paris.", "how many states") still bind — shape gates fragments only.

DEFECT 2 — NO DISPUTE CONCEPT (both calls). All four live protest lines
returned False from lily_detect_verdict_contest; progression had no
dispute state; the address debt was discharged by pre-committed reveal
lines; _contest_note was cleared by ANY confirmed turn. Fix under test:
the widened detector catches all four lines VERBATIM (read from the
fixtures); a protest within the dispute window of a verdict airing arms
a dispute-hold that pauses progression until a POST-protest turn
confirms on air (with a hard timeout so it can never wedge).

DEFECT 3 — SOLO-RELAXED INSTANT ADJUDICATION (both burns: Oscar Wilde
08-14, Aphrodite 08-15). Roster-complete adjudicated in the SAME TICK
as the bind — the verdict was committed before any protest could land.
Fix under test: a settle window (bind revisable/disputable for M
seconds; VAD-quiet + no pending clarify + no dispute-hold to close);
a binding-denial protest during settle withdraws the bind (no burn).
Timed tables are untouched.
"""

import asyncio
import time
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_audeering_consumers
import lily_config
import lily_evaluation
import lily_say_gate
import lily_scorekeeper
from lily_agent import LilyGame
from lily_scorekeeper import LilyScorekeeper

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FIXTURE_0814 = FIXTURES / "live_20260814_1751_gameflow.txt"
FIXTURE_0815 = FIXTURES / "live_20260815_1347_gameflow.txt"

# The real q4 of the 08-14 call (q_4_delivery transcript, 21:54:38).
Q_WILDE = {
    "id": "kb_wilde",
    "prompt": (
        "Imprisoned in 1895 for gross indecency with men, who was this "
        "celebrated Irish playwright?"
    ),
    "canonical_answer": "Oscar Wilde",
    "acceptable_answers": ["oscar wilde"],
    "category": "academic",
}

# 21:54:43 — the fragment that hard-bound live.
FRAGMENT = "What's he. Face. Uh."
# 21:53:54 — the mid-band utterance that (correctly) got the check.
MIDBAND = "The. State."


class _FakeSession:
    def __init__(self) -> None:
        self.instructions: list[str] = []
        self.said: list[str] = []

    def generate_reply(self, instructions: str) -> None:
        self.instructions.append(instructions)

    def say(self, text, *a, **k):
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
        return '{"verdict": "incorrect", "reason": "not the answer"}'


def _make_game(session_id: str = "lily-FD3994-358c0ac8") -> LilyGame:
    game = LilyGame.bare()
    game.session = _FakeSession()
    game.agent = _FakeAgentHandle()
    game._preemptive_paused = False
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game.sk = LilyScorekeeper(session_id)
    game.rounds_total = 3
    game.ui_phase = "question"
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
    game.metadata_publishes = []

    async def _publish_metadata(question_text, **kwargs):
        game.metadata_publishes.append(question_text or "")

    async def _publish_attributes(*a, **k):
        pass

    game.publish_metadata = _publish_metadata
    game.publish_attributes = _publish_attributes
    game.send_event_nowait = lambda kind, payload=None: None
    return game


def _arm_wilde(game: LilyGame) -> float:
    game.sk.bind_speaker("S1", "Rami")
    game.armed_question = dict(Q_WILDE)
    game.sk.start_question(game.armed_question)
    game.sk.round = 1
    game.sk.set_phase("round")
    delivered = getattr(game, "_delivered_to_playout", None)
    if delivered is None:
        delivered = game._delivered_to_playout = set()
    delivered.add(game.sk.question_number)
    key = f"q_{game.sk.question_number}_delivery"
    game.say_registry.claim(key, owner="speech_delivery_q4")
    game.say_registry.confirm(key)
    return time.time()


def _final(game: LilyGame, text: str, at: float) -> None:
    """One finalized user segment through the real production path."""
    result = game.sk.on_transcript_segment(
        text=text, speaker_label="S1", is_final=True,
        now=at, segment_start_time=at, segment_end_time=at + 1.5,
    )
    game.on_transcript_event(result, text, speaker_label="S1", segment_ts=at)


def _run(coro_fn, *, settle_ticks: int = 25):
    async def _scenario():
        out = coro_fn()
        if asyncio.iscoroutine(out):
            out = await out
        for _ in range(settle_ticks):
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


def _fixture_line(path: Path, stamp: str) -> str:
    """The player text of the fixture line carrying `stamp` (S13: the
    tests read the committed transcripts, never a retyped copy)."""
    for line in path.read_text().splitlines():
        if line.startswith(stamp):
            return line.split(": ", 1)[1]
    raise AssertionError(f"{stamp} not found in {path.name}")


# ===========================================================================
# DEFECT 1 — the shape sensor itself (pure)
# ===========================================================================


def test_live_fragment_has_no_committed_answer_shape():
    assert lily_evaluation.lily_uncommitted_answer_shape(FRAGMENT) == (
        lily_evaluation.LILY_SHAPE_WH_SEARCH
    )


def test_committed_answers_keep_answer_shape():
    # The design-intent class (lily_evaluation "how many states" comment)
    # and every confident wrong answer stays bindable — shape gates
    # FRAGMENTS, never wrong answers.
    for text in (
        "how many states",
        "Paris",
        "Benjamin. Franklin.",
        "Saturn?",
        "Michelangelo",
        MIDBAND,  # mid-band already has its own (clarify) path
        "I never can remember the name of it. So I'm. I'm surrendering.",
    ):
        assert lily_evaluation.lily_uncommitted_answer_shape(text) is None, text


def test_disfluency_and_fragment_tails_detected():
    assert lily_evaluation.lily_uncommitted_answer_shape("It was the. Um.") == (
        lily_evaluation.LILY_SHAPE_DISFLUENCY_TAIL
    )
    assert lily_evaluation.lily_uncommitted_answer_shape("it's the. the.") == (
        lily_evaluation.LILY_SHAPE_FRAGMENT_TAIL
    )


# ===========================================================================
# DEFECT 1 — (i) the live fragment routes to clarify, never binds
# ===========================================================================


def test_fragment_on_open_question_clarifies_instead_of_binding():
    """FAILING-FIRST (i): the 21:54:43 replay. Pre-WO the fragment
    skipped the clarify (BAND_REJECT) and hard-bound; now it routes into
    the pending-clarify machinery and the bind is withdrawn."""
    game = _make_game()
    now = _arm_wilde(game)
    game.sk.set_pacing("relaxed")

    def _go():
        game.open_window()
        _final(game, FRAGMENT, now + 5)

    _run(_go)

    # The clarify is out to Rami…
    assert "Rami" in game.pending_clarify
    # …the bind did NOT stand…
    assert "Rami" not in game.sk.answer_candidates
    # …the question is still live (no verdict, no burn):
    assert game.sk.answer_window_open
    aired = " ".join(game.session.said + game.session.instructions).lower()
    assert "oscar wilde" not in aired
    assert "locked in" not in aired


def test_receipt_stands_down_for_reject_shape():
    """The 'Locked in—' receipt yields to the clarify on the reject-shape
    route exactly as it does on the mid-band route."""
    game = _make_game()
    t1 = {
        "verdict": "uncertain",
        "similarity": 0.143,
        "attempt_text": FRAGMENT,
    }
    assert game._receipt_yields_to_clarify(t1, 0.84) is True
    # A committed wrong answer at the same similarity keeps its receipt.
    t1_committed = {
        "verdict": "uncertain",
        "similarity": 0.143,
        "attempt_text": "Michelangelo",
    }
    assert game._receipt_yields_to_clarify(t1_committed, 0.84) is False


def test_committed_wrong_answer_still_binds_on_reject_band():
    """Design intent pinned: shape gates fragments, not wrong answers —
    a confident wrong answer in BAND_REJECT binds exactly as before."""
    game = _make_game()
    now = _arm_wilde(game)
    game.sk.set_pacing("timed")

    def _go():
        game.sk.open_answer_window(
            duration=15.0, now=now, question_id=Q_WILDE["id"],
            question_index=game.sk.question_number, registered=True,
        )
        _final(game, "Michelangelo", now + 4)

    _run(_go)

    assert "Rami" in game.sk.answer_candidates
    assert game.pending_clarify == {}


# ===========================================================================
# (v) — "The. State." keeps its exact pre-WO clarify path
# ===========================================================================


def test_midband_the_state_path_unchanged():
    game = _make_game()
    game.sk.bind_speaker("S1", "Rami")
    game.sk.answer_candidates["Rami"] = {
        "player": "Rami", "speaker_label": "S1", "text": MIDBAND,
        "segment_start_time": time.time(), "timestamp": time.time(),
        "attempts": [],
    }
    cand = {"player": "Rami", "speaker_label": "S1", "text": MIDBAND,
            "segment_start_time": time.time(), "timestamp": time.time()}
    game._maybe_fire_clarify(
        cand, {"verdict": "uncertain", "similarity": 0.769},
        0.84, "OPEN_WINDOW",
    )
    # Clarify fires (mid band) AND the candidate stays bound — the
    # mid-band contract is byte-identical to WO-ADDRESSEE-H1 Task 4.
    assert "Rami" in game.pending_clarify
    assert "Rami" in game.sk.answer_candidates


# ===========================================================================
# DEFECT 2 — (ii) all four live protest lines, verbatim from the fixtures
# ===========================================================================


def test_all_four_live_protest_lines_detected_verbatim():
    """FAILING-FIRST (ii): every one of these returned False pre-WO."""
    lines = [
        _fixture_line(FIXTURE_0814, "21:54:50 Rami"),
        _fixture_line(FIXTURE_0814, "21:54:54 Rami"),
        _fixture_line(FIXTURE_0815, "17:49:27 Rami"),
        _fixture_line(FIXTURE_0815, "17:49:47 Rami"),
    ]
    assert len(lines) == 4
    for line in lines:
        assert lily_scorekeeper.lily_detect_verdict_contest(line), line


def test_hedged_answers_never_read_as_contest():
    for text in (
        "I'm thinking Aphrodite",
        "I was thinking maybe Saturn",
        "the answer is Paris",
        "let me think",
        "Sigmund Freud",
    ):
        assert not lily_scorekeeper.lily_detect_verdict_contest(text), text


# ===========================================================================
# DEFECT 2 — (iii) protest between verdict-commit and N+1 holds the game
# ===========================================================================


def _protest_after_verdict(game: LilyGame) -> None:
    """The 08-14 21:54 sequence: verdict airs, protest lands inside the
    dispute window."""
    qnum = game.sk.question_number
    game.note_result_aired(qnum, "Nobody had it — Oscar Wilde")
    game.note_protest_final(
        _fixture_line(FIXTURE_0814, "21:54:54 Rami"), "Rami", time.time(),
    )


def test_protest_after_verdict_arms_dispute_hold_and_pauses_progression():
    """FAILING-FIRST (iii): pre-WO progression_paused_reason had no
    dispute state and N+1 fired over the protest."""
    game = _make_game()
    _arm_wilde(game)
    _protest_after_verdict(game)

    assert game.dispute_hold_active() is True
    assert game.progression_paused_reason() == "dispute_hold"
    # The advance gate consults it: dispatch_armed_question refuses.
    assert game.dispatch_armed_question(source="post_reveal") is False


def test_pre_committed_speech_cannot_release_the_dispute():
    """A reveal line dispatched BEFORE the protest, confirming after it,
    must not clear the contest note or the hold (pre-WO both were blind
    clears)."""
    game = _make_game()
    _arm_wilde(game)
    game._note_speech_dispatch("sp_pre")  # queued before the protest
    time.sleep(0.02)
    game._contest_note = "[verdict contest — live]"
    _protest_after_verdict(game)

    _run(lambda: game.on_agent_speech_finished(
        "It was Oscar Wilde — no point this time.", speech_id="sp_pre",
    ))

    assert game._contest_note is not None          # note survives
    assert game.dispute_hold_active() is True       # hold survives
    assert game.progression_paused_reason() == "dispute_hold"


def test_post_protest_turn_confirming_releases_the_dispute():
    game = _make_game()
    _arm_wilde(game)
    game._contest_note = "[verdict contest — live]"
    _protest_after_verdict(game)
    assert game.dispute_hold_active() is True

    game._note_speech_dispatch("sp_post")  # dispatched AFTER the protest
    _run(lambda: game.on_agent_speech_finished(
        "You're right — let me re-check that one against the record.",
        speech_id="sp_post",
    ))

    assert game._contest_note is None
    assert game.dispute_hold_active() is False
    assert game.progression_paused_reason() is None


def test_dispute_hold_times_out_never_a_silence_wedge(monkeypatch):
    monkeypatch.setenv("LILY_DISPUTE_HOLD_TIMEOUT_SECONDS", "0.05")
    game = _make_game()
    _arm_wilde(game)
    _protest_after_verdict(game)
    assert game.dispute_hold_active() is True
    time.sleep(0.06)
    assert game.dispute_hold_active() is False
    assert game.progression_paused_reason() is None


def test_unanchored_protest_does_not_hold_progression():
    """A protest-shaped final with NO ruling aired inside the dispute
    window keeps its X12 one-shot but never pauses the game."""
    game = _make_game()
    _arm_wilde(game)
    game.note_protest_final(
        "I did not say a word. I was still thinking.", "Rami", time.time(),
    )
    assert game.dispute_hold_active() is False
    assert game.progression_paused_reason() is None


def test_pre_debt_playout_cannot_discharge_address_debt():
    """The address debt a protest armed must not be credited to a speech
    dispatched BEFORE the protest (pre-WO: ANY playout start cleared it)."""
    game = _make_game()
    game._note_speech_dispatch("sp_pre")
    time.sleep(0.02)
    game._awaiting_address_since = time.time()
    game.note_playout_started("sp_pre")
    assert game._awaiting_address_since  # still armed

    game._note_speech_dispatch("sp_post")
    game.note_playout_started("sp_post")
    assert not game._awaiting_address_since  # a real reply pays it


# ===========================================================================
# DEFECT 3 — (iv) the solo-relaxed settle window
# ===========================================================================


def test_solo_relaxed_bind_then_protest_no_burn(monkeypatch):
    """FAILING-FIRST (iv): both live burns. Pre-WO roster-complete
    adjudicated in the same tick; now the bind settles, the protest
    withdraws it, and the question survives."""
    monkeypatch.setenv("LILY_RELAXED_SETTLE_SECONDS", "0.2")
    game = _make_game()
    now = _arm_wilde(game)
    game.sk.set_pacing("relaxed")

    async def _go():
        game.open_window()
        _final(game, "Michelangelo", now + 4)  # committed wrong answer binds
        # Settle window is open — nothing adjudicated in this tick.
        assert game._relaxed_settle_pending == game.sk.question_number
        assert game.sk.answer_window_open
        await asyncio.sleep(0.05)
        # The immediate protest disowns the bind (binding-denial class).
        _final(
            game,
            "What do you mean, locked in? I didn't say anything.",
            now + 6,
        )
        assert "Rami" not in game.sk.answer_candidates  # bind withdrawn
        assert game.dispute_hold_active() is True
        await asyncio.sleep(0.5)  # well past the settle span

    _run(lambda: _go())

    # NO BURN: window still open, verdict never aired, question intact.
    assert game.sk.answer_window_open
    aired = " ".join(game.session.said + game.session.instructions).lower()
    assert "oscar wilde" not in aired


def test_solo_relaxed_settle_closes_cleanly_when_undisputed(monkeypatch):
    """The settle window is a beat, not a wedge: quiet floor, no clarify,
    no dispute — the beat closes on its own and the verdict airs."""
    monkeypatch.setenv("LILY_RELAXED_SETTLE_SECONDS", "0.05")
    game = _make_game()
    now = _arm_wilde(game)
    game.sk.set_pacing("relaxed")

    async def _go():
        game.open_window()
        _final(game, "Michelangelo", now + 4)
        assert game.sk.answer_window_open  # not the same tick
        await asyncio.sleep(0.4)

    _run(lambda: _go())

    assert not game.sk.answer_window_open  # adjudicated after settle
    aired = " ".join(game.session.said + game.session.instructions).lower()
    assert "oscar wilde" in aired


# ===========================================================================
# (vi) — timed tables' latency pinned unchanged
# ===========================================================================


def test_timed_table_never_opens_a_settle_window():
    game = _make_game()
    now = _arm_wilde(game)
    assert game.sk.pacing == "timed"

    def _go():
        game.sk.open_answer_window(
            duration=15.0, now=now, question_id=Q_WILDE["id"],
            question_index=game.sk.question_number, registered=True,
        )
        _final(game, "Michelangelo", now + 4)

    _run(_go)

    assert game._relaxed_settle_pending is None
    assert game._relaxed_settle_task is None


def test_timed_breath_never_reads_relaxed_knobs(monkeypatch):
    """The timed inter-question seam is byte-identical: it never consults
    the relaxed multiplier or the floor-clear machinery."""
    game = _make_game()
    _arm_wilde(game)
    assert game.sk.pacing == "timed"

    def _boom():
        raise AssertionError("timed pacing consulted a relaxed knob")

    monkeypatch.setattr(lily_config, "relaxed_breath_multiplier", _boom)
    monkeypatch.setattr(lily_config, "relaxed_settle_seconds", _boom)
    monkeypatch.setenv("LILY_INTER_QUESTION_BREATH_SECONDS", "0")

    calls = []
    game.dispatch_armed_question = lambda *, source: calls.append(source) or True
    # No running loop + breath 0 -> the inline dispatch path, exactly the
    # pre-PACING-001 seam; the relaxed knobs must never be read.
    assert game._advance_after_breath(source="post_reveal") is True
    assert calls == ["post_reveal"]


def test_relaxed_breath_scales_and_waits_for_floor_clear(monkeypatch):
    monkeypatch.setenv("LILY_INTER_QUESTION_BREATH_SECONDS", "0.02")
    monkeypatch.setenv("LILY_RELAXED_BREATH_MULTIPLIER", "3.0")
    monkeypatch.setenv("LILY_RELAXED_SETTLE_SECONDS", "0.01")
    game = _make_game()
    _arm_wilde(game)
    game.sk.set_pacing("relaxed")
    game.sk.close_answer_window()
    game.armed_question = dict(Q_WILDE)

    calls = []
    game.dispatch_armed_question = lambda *, source: calls.append(source) or True

    async def _go():
        # A live dispute holds the dispatched breath past its sleep…
        game._dispute_hold_since = time.time()
        assert game._advance_after_breath(source="post_reveal") is True
        await asyncio.sleep(0.3)
        assert calls == []  # held by the floor-clear read
        # …and its release lets the dispatch through (self-releasing arm).
        game.release_dispute_hold(reason="test")
        await asyncio.sleep(1.2)

    _run(lambda: _go())
    assert calls == ["post_reveal"]
