"""WO-LILY-CONTROL-GATES-001 — the control gates, driven on the real paths.

Every test here drives REAL methods on REAL state — on_transcript_event for
the spoken-start and restart-confirm paths, on_agent_speech_finished for
airing/suppression, set_pacing / open_window / adjudicate for the window
paths. No source text is inspected anywhere in this file.

The audits reproduced (Auditors A and C, scripts scratchpad/audit_repros.py
R2/R8/R9 and scratchpad/audit_scenarios.py A/C/D/E/G against main a380531):

  R1  resolve_restart_confirm had no expiry, no requester bind and no
      "confirm actually aired" check; consulted on every final from any
      player with the forget flow's broad yes-set; the game was not paused.
      R2: ten minutes later S2's "Yeah it's the femur" was bound as an
      ANSWER and wiped the board. R9: two "restart the game" finals from
      anyone wiped the board with no yes.
  R2  execute_restart omitted _stale_retry_counts (R8) and
      _relaxed_settle_pending; an untracked ensure_future(adjudicate)
      survived the reset and could commit a dead-game verdict into game 2.
  S1  every spoken start was deferred by its OWN address stamp
      (lobby_unsettled:address_unanswered), aired "One sec — locking the
      table first", and the latch made a second request silent.
  S2  "not ready to start" / "are you ready to start?" / "let's go get a
      drink first" / "let's play it by ear" / "let's begin with the rules"
      all started; "I want to play relaxed" set a start flag forever.
  S3  the settle watcher exhausted into an auto-start net that needs ≥2
      players — a solo table could be stranded.
  D1  a TIMED window's clock survived a mid-window relaxed flip (the 08-15
      burn); a protest on an open window with no verdict yet was
      PROTEST_UNANCHORED and the question timed out under the protest.
  D2  the dispute-hold released on ANY confirmed speech ("Take your time.",
      a pacing ack, the N+1 delivery); a timeout progressed silently.
  D3  note_protest_final withdrew the bound answer on ANY contest class
      during settle ("I say Detroit, final" got withdrawn after scoring).
"""

import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_config
import lily_say_gate
import lily_scorekeeper
from lily_agent import LilyGame
from test_bind_dispute_p0 import (
    Q_WILDE, _arm_wilde, _final, _run, _make_game as _dispute_game,
)
from test_desync_fixture import FEMUR_QUESTION
import test_restart_wo5 as R

FIXTURE_0815 = (
    Path(__file__).resolve().parent
    / "fixtures" / "live_20260815_1347_gameflow.txt"
)


# ---------------------------------------------------------------------------
# Harness — SpeechHandle-shaped lanes (ids are what the gates bind to)
# ---------------------------------------------------------------------------


class _Handle:
    def __init__(self, sid: str) -> None:
        self.id = sid
        self.interrupted = False

    def interrupt(self, force=False):
        self.interrupted = True
        return self


class _HandleSession:
    def __init__(self) -> None:
        self.instructions: list[str] = []
        self.said: list[str] = []
        self._n = 0

    def _handle(self) -> _Handle:
        self._n += 1
        return _Handle(f"speech_{self._n}")

    def generate_reply(self, instructions: str):
        self.instructions.append(instructions)
        return self._handle()

    def say(self, text, *a, **k):
        self.said.append(text)
        return self._handle()


def _sid(game: LilyGame, act: str) -> str | None:
    for sid, a in reversed(list(game._dispatched_act_by_speech.items())):
        if a == act:
            return sid
    return None


def _confirm_playout(game: LilyGame, act: str, **outcome) -> str:
    """The act's handle reaches on_agent_speech_finished — cleanly by
    default, or with interrupted/suppressed/failed=True."""
    sid = _sid(game, act)
    assert sid is not None, f"no dispatched handle for act={act}"
    game.on_agent_speech_finished(act, speech_id=sid, **outcome)
    return sid


def _said(game: LilyGame, needle: str) -> list:
    return [s for s in game.session.said if needle.lower() in s.lower()]


def _stakes_game() -> LilyGame:
    """A mid-game table with stakes (test_restart_wo5's harness: q3 on
    the board, Rami up 2) — handle-returning lanes."""
    return R._live_game()


def _lobby_game() -> LilyGame:
    """A settled lobby with one bound player and start_game's heavy
    dependencies stubbed, so the REAL start_game can commit."""
    g = R._make_game(started=False)
    g._game_start_committed = False
    g.next_question = {"id": "q1", "prompt": "x", "canonical_answer": "y",
                       "acceptable_answers": ["y"]}
    g.session_started_at = time.time() - 300
    g._last_user_turn_at = time.monotonic() - 60
    g._last_bind_at = time.time() - 100  # intake settled long ago

    async def _noop_async(*a, **k):
        return None

    g.resolve_group_identity = _noop_async
    g.publish_attributes = _noop_async
    g.start_prefetch = lambda: None
    g.arm_next_question = lambda: True
    g.start_idle_watchdog = lambda: None
    g.fire_enrollment = lambda trigger: None
    g._enroll_started = False
    return g


async def _settle(ticks: int = 8) -> None:
    for _ in range(ticks):
        await asyncio.sleep(0)


# ===========================================================================
# R1 — the restart confirm: TTL, requester bind, aired gate, unwind
# ===========================================================================


def test_r2_replay_stale_yes_from_another_speaker_does_not_wipe():
    """FAILING-FIRST (audit R2, verbatim): the confirm aired, ten minutes
    pass, a DIFFERENT player answers a trivia question with "Yeah it's the
    femur". Pre-WO: bound as an answer AND the board wiped. Now: the
    stale confirm is dropped out loud, the yes is not consent, the answer
    binds as an answer, the game stands."""
    g = _stakes_game()

    async def _go():
        await R._drive(g, "Lily, restart the game")
        assert g.restart_confirm_pending() is True
        assert g.progression_paused_reason() == "restart_confirm_pending"
        _confirm_playout(g, "restart_confirm")
        assert g._pending_restart_confirm["aired_at"] is not None
        # ten minutes go by, the table keeps playing
        g._pending_restart_confirm["aired_at"] -= 600
        g._pending_restart_confirm["at"] -= 600
        g.armed_question = dict(FEMUR_QUESTION)
        g.sk.start_question(g.armed_question)
        g.sk.open_answer_window(20.0)
        res = R._segment(g, "Yeah it's the femur", label="S2")
        assert bool(res.get("candidate_recorded")) is True
        assert g.game_started is True                 # NOT wiped
        assert g.sk.ledger_scores()["Rami"] == 2
        assert g._pending_restart_confirm is None     # dropped, out loud
        assert g.restart_intent_present() is False
        assert _said(g, "didn't catch a yes")

    asyncio.run(_go())


def test_r9_replay_restated_by_another_speaker_does_not_wipe():
    """FAILING-FIRST (audit R9): "restart the game" twice. From ANOTHER
    speaker after the confirm aired: still pending, nothing reset. The
    requester restating it after it aired: that confirms."""
    g = _stakes_game()

    async def _go():
        await R._drive(g, "restart the game", label="S1")
        _confirm_playout(g, "restart_confirm")
        await R._drive(g, "restart the game", label="S2")
        assert g.game_started is True
        assert g.sk.ledger_scores()["Rami"] == 2
        assert g.restart_confirm_pending() is True
        assert len(_said(g, "scores gone")) == 1      # never re-asked
        await R._drive(g, "restart the game", label="S1")
        assert g.game_started is False                # the requester confirms
        assert g.sk.ledger_scores()["Rami"] == 0
        ev = g._game_restart_events[-1]
        assert ev["confirm_requester"] == "Rami"
        assert ev["confirm_aired_at"] is not None
        assert ev["resolved_by"] == "restated_by_requester"

    asyncio.run(_go())


def test_r9_dup_final_before_the_confirm_airs_does_not_wipe():
    """FAILING-FIRST (audit R9, the STT-dup / echo shape): two identical
    finals 10ms apart from the same label — the second lands before the
    confirm could have aired. Not an answer to a question nobody heard."""
    g = _stakes_game()

    async def _go():
        await R._drive(g, "restart the game", "restart the game")
        assert g.game_started is True
        assert g.sk.ledger_scores()["Rami"] == 2
        assert g.restart_confirm_pending() is True

    asyncio.run(_go())


def test_yes_from_another_speaker_or_before_airing_is_not_consent():
    g = _stakes_game()

    async def _go():
        await R._drive(g, "restart the game", label="S1")
        await R._drive(g, "yes", label="S1")          # before it aired
        assert g.game_started is True
        _confirm_playout(g, "restart_confirm")
        await R._drive(g, "yes", label="S2")          # not the requester
        assert g.game_started is True
        assert g.restart_confirm_pending() is True
        await R._drive(g, "yes", label="S1")          # requester, after air
        assert g.game_started is False
        ev = g._game_restart_events[-1]
        assert ev["resolved_by"] == "voice_yes"
        assert ev["requester"] == "Rami"
        assert ev["confirm_aired_at"] is not None

    asyncio.run(_go())


def test_confirm_ttl_expires_and_drops_out_loud(monkeypatch):
    monkeypatch.setenv("LILY_RESTART_CONFIRM_TTL_SECONDS", "0.2")
    g = _stakes_game()

    async def _go():
        await R._drive(g, "restart the game")
        _confirm_playout(g, "restart_confirm")
        await asyncio.sleep(0.3)
        # The progression gate is one consult site: the expiry is read
        # there too, and the restart pause lifts with the drop (the
        # address debt of the request final itself clears at playout
        # START live — note_playout_started — not modelled here).
        assert g.progression_paused_reason() != "restart_confirm_pending"
        assert g._pending_restart_confirm is None
        assert _said(g, "didn't catch a yes")
        await R._drive(g, "yes")
        assert g.game_started is True
        assert g.sk.ledger_scores()["Rami"] == 2

    asyncio.run(_go())


def test_no_from_any_player_drops_the_ask():
    g = _stakes_game()

    async def _go():
        await R._drive(g, "restart the game", label="S1")
        _confirm_playout(g, "restart_confirm")
        await R._drive(g, "no, keep going", label="S2")
        assert g._pending_restart_confirm is None
        assert g.game_started is True
        assert _said(g, "the game stands")

    asyncio.run(_go())


def test_confirm_suppressed_unwinds_pending_and_reasks_once():
    """FAILING-FIRST: the confirm's handle is suppressed (freshness gate /
    barge flush / cancel — W1's on_dispatch_suppressed seam, consumed
    here through the playout report). The pending state unwinds, ONE
    re-ask airs; a second loss drops the ask with the one-liner, and a
    later yes from the requester is not consent."""
    g = _stakes_game()

    async def _go():
        await R._drive(g, "restart the game")
        first = g._pending_restart_confirm["speech_id"]
        _confirm_playout(g, "restart_confirm", suppressed=True)
        await _settle()
        pending = g._pending_restart_confirm
        assert pending is not None and pending["attempts"] == 2
        assert pending["speech_id"] not in (None, first)   # re-asked
        assert len(_said(g, "scores gone")) == 2
        _confirm_playout(g, "restart_confirm", interrupted=True)
        await _settle()
        assert g._pending_restart_confirm is None            # dropped
        assert len(_said(g, "scores gone")) == 2             # never a third
        assert _said(g, "didn't catch a yes")
        await R._drive(g, "yes")
        assert g.game_started is True

    asyncio.run(_go())


def test_on_dispatch_suppressed_hook_is_the_seam():
    """W1's hook, called directly with the bound handle: same unwind."""
    g = _stakes_game()
    assert g.request_restart(
        source="voice_command", requester="Rami", text="restart"
    ) == "confirm_armed"
    sid = g._pending_restart_confirm["speech_id"]
    g.on_dispatch_suppressed("restart_confirm", sid, "stale_reply_superseded")
    assert g._pending_restart_confirm["attempts"] == 2
    assert g._pending_restart_confirm["speech_id"] != sid
    # An unrelated act is a no-op for this consumer.
    g.on_dispatch_suppressed("hold_ack", "speech_x", "flush")
    assert g._pending_restart_confirm["attempts"] == 2


def test_tool_path_binds_to_the_detector_requester():
    g = _stakes_game()
    g.note_player_restart_intent(
        source="voice_command", text="restart the game", requester="Rami"
    )
    assert g.request_restart(source="host_tool") == "confirm_armed"
    assert g._pending_restart_confirm["requester"] == "Rami"


# ===========================================================================
# R2 — the reset is complete; resumable coroutines abandon across it
# ===========================================================================


def test_restart_resets_retry_counts_settle_pending_and_bumps_generation():
    """FAILING-FIRST (audit R8): game 2's q_1 watchdog budget was
    pre-spent by game 1's retry counts; the settle marker survived."""
    g = _stakes_game()
    g._stale_retry_counts = {"q_1_delivery": 2}
    g._relaxed_settle_pending = 3
    gen = g._game_generation
    g.execute_restart(source="test")
    assert g._stale_retry_counts == {}
    assert g._relaxed_settle_pending is None
    assert g._relaxed_settle_task is None
    assert g._game_generation == gen + 1
    assert g._game_restart_events[-1]["generation"] == gen + 1


def test_adjudicate_abandons_across_a_restart_generation_token():
    """FAILING-FIRST: an untracked ensure_future(adjudicate) is parked on
    the Tier-2 judge await when the table restarts. Pre-WO its post-await
    guard read only _delivery_stop_sticky (cleared by the reset itself)
    and the dead game's verdict committed into game 2. Now the generation
    token stands it down: no ledger row, no result aired, no verdict."""
    g = _dispute_game("lily-gen-token")
    _arm_wilde(g)
    entered = asyncio.Event()
    release = asyncio.Event()

    class _GatedReasoning:
        async def prefetch_question(self, sk, **kw):
            return None

        async def prefetch_picture_question(self, supabase, **kw):
            return None

        async def judge(self, *a, **kw):
            entered.set()
            await release.wait()
            return '{"verdict": "correct", "winner": "Rami", "reason": "x"}'

    g.reasoning = _GatedReasoning()

    async def _go():
        g.open_window()
        now = time.time()
        g.sk.on_transcript_segment(
            text="Wild.", speaker_label="S1", is_final=True, now=now,
            segment_start_time=now, segment_end_time=now + 1,
        )
        assert "Rami" in g.sk.answer_candidates
        task = asyncio.ensure_future(g.adjudicate(steal_allowed=False))
        await asyncio.wait_for(entered.wait(), timeout=2.0)
        g.execute_restart(source="test")               # mid-await
        assert g.game_started is False
        release.set()
        await asyncio.wait_for(task, timeout=2.0)
        assert g.sk.score_ledger == []
        assert g.sk.ledger_scores().get("Rami", 0) == 0
        assert g._result_aired is None
        assert not [s for s in g.session.said if "wilde" in s.lower()]
        assert not [i for i in g.session.instructions if "VERDICT" in i]
        assert g._adjudicating is False

    _run(lambda: _go())


# ===========================================================================
# S1 — the spoken start is not deferred by its own trigger
# ===========================================================================


def test_spoken_start_via_on_transcript_event_is_not_self_deferred():
    """FAILING-FIRST (audit A): "Let's start the game." through the REAL
    on_transcript_event. Pre-WO: classify_addressee stamped the address
    debt first, start_game read lobby_unsettled:address_unanswered off
    its own trigger, aired "One sec — locking the table first" and the
    kickoff landed a poll late. Now: the kickoff composite dispatches in
    the same tick, no hold line, and the code lane owns the reply."""
    g = _lobby_game()

    async def _go():
        text = "Let's start the game."
        R._segment(g, text)
        # Same event: the debt minted by the start phrase itself is gone,
        # the reply lane is owned by code, the intent fact is recorded.
        assert g._awaiting_address_since == 0.0
        assert g._player_start_intent is not None
        await _settle()
        assert g.game_started is True
        assert g._player_start_intent["source"] == "voice"
        assert not _said(g, "locking the table")
        assert any("round one" in i.lower() for i in g.session.instructions)
        assert g.consume_deterministic_reply(text) is True  # one reply lane

    asyncio.run(_go())


def test_second_spoken_start_after_a_deferral_is_not_silent():
    """FAILING-FIRST: _start_hold_said latched after the first deferral,
    so a second request got NOTHING. Now the latch re-arms per request."""
    g = _lobby_game()
    g._last_bind_at = time.time()  # a name bind just landed: intake active

    async def _go():
        R._segment(g, "Let's start the game.")
        await _settle()
        assert g.game_started is False
        assert len(_said(g, "locking the table")) == 1
        R._segment(g, "Lily, let's start the game.")
        await _settle()
        assert len(_said(g, "locking the table")) == 2
        task = g._pending_start_task
        if task is not None:
            task.cancel()

    asyncio.run(_go())


def test_an_older_unanswered_address_still_defers_the_start():
    """Only the debt minted by the start phrase itself is released — an
    earlier unanswered address keeps the WO-3 settle gate."""
    g = _lobby_game()
    g._awaiting_address_since = time.time() - 5
    g._address_stamp_seq = -1

    async def _go():
        R._segment(g, "Let's start the game.")
        # The classifier re-stamps for the new host-directed final, which
        # the start branch releases; the point of this test is the gate
        # read with an OLDER debt, so re-plant it before start_game runs.
        g._awaiting_address_since = time.time() - 5
        g._address_stamp_seq = -1
        await _settle()
        assert g.game_started is False
        assert _said(g, "locking the table")
        task = g._pending_start_task
        if task is not None:
            task.cancel()

    asyncio.run(_go())


# ===========================================================================
# S2 — negation / question / deferral guards; the setup flag expires
# ===========================================================================


@pytest.mark.parametrize("text", [
    "not ready to start",
    "are you ready to start?",
    "let's go get a drink first",
    "let's play it by ear",
    "let's begin with the rules",
    "I'm ready to play... just kidding, one sec",
    "we're ready to play whenever",
    "ready to play some music?",
    "I don't want to play yet",
])
def test_negated_question_and_deferred_start_phrases_do_not_start(text):
    """FAILING-FIRST (audit G): every one of these opened round one."""
    g = _lobby_game()

    async def _go():
        R._segment(g, text)
        await _settle()
        assert g.game_started is False, text
        assert g.start_intent_present() is False, text
        assert not _said(g, "locking the table")

    asyncio.run(_go())


@pytest.mark.parametrize("text", [
    "let's start the game", "let's play the game", "lets go",
    "ready to play", "can we start the game", "Ok let's start, Lily",
])
def test_real_start_phrases_still_start(text):
    g = _lobby_game()

    async def _go():
        R._segment(g, text)
        await _settle()
        assert g.game_started is True, text

    asyncio.run(_go())


def test_pacing_sentence_does_not_set_the_start_flag():
    """FAILING-FIRST (audit G): "I want to play relaxed" set
    _setup_start_requested=True (never cleared) — start_intent_present()
    True forever, and the auto-start net could open round one."""
    g = _lobby_game()

    async def _go():
        R._segment(g, "I want to play relaxed")
        await _settle()
        assert g.sk.pacing == "relaxed"
        assert getattr(g, "_setup_start_requested", False) is False
        assert g.start_intent_present() is False
        assert g.game_started is False

    asyncio.run(_go())


def test_setup_start_flag_expires(monkeypatch):
    monkeypatch.setenv("LILY_SETUP_START_INTENT_TTL_SECONDS", "0.2")
    g = _lobby_game()
    g.note_lobby_setup_intents("I want to play")
    assert g._setup_start_requested is True
    assert g.start_intent_present() is True
    time.sleep(0.25)
    assert g.start_intent_present() is False
    assert g._setup_start_requested is False
    # A legacy flag with no stamp never expires (harness compatibility).
    g._setup_start_requested = True
    g._setup_start_requested_at = 0.0
    assert g.start_intent_present() is True


# ===========================================================================
# S3 — bounded fallback after the settle watcher exhausts
# ===========================================================================


def test_settle_watcher_exhaustion_latches(monkeypatch):
    g = _lobby_game()
    g.session = _HandleSession()
    g.say_registry = lily_say_gate.SpeechActRegistry()
    g._last_bind_at = time.time() + 10_000  # never settles
    real_sleep = asyncio.sleep

    async def _fast(delay, *a, **k):
        return await real_sleep(0)

    async def _go():
        g.note_player_start_intent(source="voice_command", text="start")
        monkeypatch.setattr(asyncio, "sleep", _fast)
        try:
            g._defer_start_until_settled("voice")
            task = g._pending_start_task
            assert task is not None
            await asyncio.wait_for(task, timeout=5.0)
        finally:
            monkeypatch.setattr(asyncio, "sleep", real_sleep)
        assert g._start_settle_exhausted is True
        assert g.game_started is False

    asyncio.run(_go())


def test_restated_start_after_exhaustion_starts_solo_table_with_one_line():
    """FAILING-FIRST: a SOLO table (auto-start net needs ≥2 players) whose
    lobby never settled had no structural "always starts" guarantee. After
    the watcher exhausts, the next restated start fires regardless of
    settle, with one line."""
    g = _lobby_game()
    assert g.sk.roster_size() < lily_config.auto_start_min_players()
    g._last_bind_at = time.time()          # still "unsettled"
    g._start_settle_exhausted = True       # the watcher already gave up

    async def _go():
        R._segment(g, "Let's start the game.")
        await _settle()
        assert g.game_started is True
        assert _said(g, "locking the table as it stands")
        assert not _said(g, "locking the table first")
        assert g._start_settle_exhausted is False  # consumed by the start

    asyncio.run(_go())


# ===========================================================================
# D1 — a timed window survives a relaxed flip; a protest anchors to the
#      OPEN window and holds its expiry
# ===========================================================================


def test_timed_window_relaxed_flip_cancels_expiry_no_burn(monkeypatch):
    """FAILING-FIRST (audit D, the 08-15 window): set_pacing never
    touched _window_timer, so the clock ran out and the question burned.
    Now the flip converts the open window to untimed; the timeline carries
    the receipt (window_untimed_at + reason after the pacing flip)."""
    monkeypatch.setenv("LILY_ANSWER_WINDOW_SECONDS", "0.3")
    g = _dispute_game("lily-D1a")
    _arm_wilde(g)
    g.prefs = {}

    async def _go():
        g.open_window()
        assert g.sk.answer_window_deadline is not None
        assert g._window_timer is not None and not g._window_timer.done()
        assert g.set_pacing("relaxed", source="voice_command") is True
        assert g.sk.answer_window_deadline is None
        assert g._window_timer is None
        await asyncio.sleep(0.6)             # well past the old clock
        assert g.sk.answer_window_open is True
        assert g._is_burned(Q_WILDE) is False
        row = g.sk.question_timeline[g.sk.question_number]
        assert "window_untimed_at" in row
        assert row["window_untimed_reason"] == "pacing_flip:voice_command"
        assert row["window_untimed_at"] >= row["window_opened_at"]
        aired = " ".join(g.session.said + g.session.instructions).lower()
        assert "oscar wilde" not in aired

    _run(lambda: _go())


def test_0815_replay_timed_window_fused_pacing_protest_no_burn(monkeypatch):
    """The 17:49:27 line verbatim (S13 fixture) on a TIMED window through
    the real final path: pacing flips relaxed, the protest anchors to the
    OPEN window, the question does not burn."""
    monkeypatch.setenv("LILY_ANSWER_WINDOW_SECONDS", "0.6")
    QA = {
        "id": "kb_aph", "category": "academic",
        "prompt": "sea foam gave rise to which goddess of love?",
        "canonical_answer": "Aphrodite", "acceptable_answers": ["aphrodite"],
    }
    g = _dispute_game("lily-D1-0815")
    g.sk.bind_speaker("S1", "Rami")
    g.armed_question = dict(QA)
    g.sk.start_question(g.armed_question)
    g.sk.round = 1
    g.sk.set_phase("round")
    g._delivered_to_playout = {g.sk.question_number}
    key = f"q_{g.sk.question_number}_delivery"
    g.say_registry.claim(key, owner="x")
    g.say_registry.confirm(key)
    g.prefs = {}
    g._pending_pacing = None
    g._pending_pacing_requester = None
    g._pacing_stated_this_session = False
    line = next(
        l.split(": ", 1)[1]
        for l in FIXTURE_0815.read_text().splitlines()
        if l.startswith("17:49:27 Rami")
    )
    now = time.time()

    async def _go():
        g.open_window()
        assert g.sk.answer_window_deadline is not None
        _final(g, line, now + 1)
        assert g.sk.pacing == "relaxed"
        assert g.sk.answer_window_deadline is None
        assert g.dispute_hold_active() is True
        assert g._dispute_hold_reason == "open_window"
        await asyncio.sleep(1.0)
        assert g.sk.answer_window_open is True
        assert g._is_burned(QA) is False
        aired = " ".join(g.session.said + g.session.instructions).lower()
        assert "aphrodite" not in aired

    _run(lambda: _go(), settle_ticks=5)


def test_protest_on_open_timed_window_holds_expiry_until_addressed(monkeypatch):
    """FAILING-FIRST (D1b): a premature-adjudication protest on an OPEN
    timed window with no verdict aired was PROTEST_UNANCHORED and the
    clock burned the question. Now it arms the hold on the window; the
    expiry waits; the addressing turn releases it and the close runs."""
    monkeypatch.setenv("LILY_ANSWER_WINDOW_SECONDS", "0.2")
    monkeypatch.setenv("LILY_DISPUTE_HOLD_TIMEOUT_SECONDS", "30")
    g = _dispute_game("lily-D1b")
    g.session = _HandleSession()
    now = _arm_wilde(g)

    async def _go():
        g.open_window()
        _final(g, "I'm still talking, we're still preparing things", now + 1)
        assert g.dispute_hold_active() is True
        assert g._dispute_hold_reason == "open_window"
        row = g.sk.question_timeline[g.sk.question_number]
        assert row["dispute_anchor"] == "open_window"
        assert "window_held_at" in row
        await asyncio.sleep(0.5)                 # past the clock
        assert g.sk.answer_window_open is True   # held, not expired
        # The addressing turn: generated WITH the contest note in context.
        assert g._contest_note is not None
        assert g.gated_say(None, "contest_reply", "address it", source="test")
        sid = _confirm_playout(g, "contest_reply")
        assert g.dispute_hold_active() is False
        assert row["dispute_released_by"] == sid
        await asyncio.sleep(0.8)                 # the held close runs
        assert g.sk.answer_window_open is False

    _run(lambda: _go(), settle_ticks=5)


# ===========================================================================
# D2 — only the addressing turn discharges the dispute; timeout speaks
# ===========================================================================


def _disputed_game(session_id: str) -> tuple:
    g = _dispute_game(session_id)
    g.session = _HandleSession()
    now = _arm_wilde(g)
    g.note_result_aired(g.sk.question_number, "Nobody had it — Oscar Wilde")
    _final(g, "I did not say a word. I was still thinking.", now + 2)
    assert g.dispute_hold_active() is True
    assert g._contest_note is not None
    return g, now


def test_hold_ack_and_pacing_text_do_not_release_the_dispute():
    """FAILING-FIRST (audit E): "Take your time." (hold_ack, a fixed
    line) confirmed after the protest released the hold pre-WO."""
    g, _ = _disputed_game("lily-E")

    def _go():
        assert g.gated_say(
            None, "hold_ack", "[hold ack]", source="hold_ack",
            text="Take your time.",
        )
        _confirm_playout(g, "hold_ack")

    _run(_go)
    assert g.dispute_hold_active() is True
    assert g._contest_note is not None
    assert g.progression_paused_reason() == "dispute_hold"


def test_game_payload_does_not_release_the_dispute():
    g, _ = _disputed_game("lily-E-payload")
    g.release_hold(reason="test")

    def _go():
        # A question delivery generated after the protest — it carries the
        # state block but is a game payload, not an answer to the player.
        g._note_speech_dispatch("sp_delivery", lane="llm")
        g._dispatched_act_by_speech["sp_delivery"] = "question_delivery"
        g.on_agent_speech_finished("Next one.", speech_id="sp_delivery")

    _run(_go)
    assert g.dispute_hold_active() is True


def test_addressing_turn_releases_and_stamps_its_speech_id():
    g, _ = _disputed_game("lily-E-address")
    g.release_hold(reason="test")

    def _go():
        assert g.gated_say(
            None, "contest_reply", "re-check the record", source="test"
        )
        return _confirm_playout(g, "contest_reply")

    sid = _run(_go)
    assert g.dispute_hold_active() is False
    assert g._contest_note is None
    row = g.sk.question_timeline[g.sk.question_number]
    assert row["dispute_released_by"] == sid
    assert row["dispute_released_at"] >= row["dispute_armed_at"]


def test_turn_generated_before_the_note_cannot_release():
    g = _dispute_game("lily-E-pre")
    g.session = _HandleSession()
    now = _arm_wilde(g)
    g.note_result_aired(g.sk.question_number, "Nobody had it — Oscar Wilde")

    def _go():
        assert g.gated_say(None, "banter", "chat", source="test")  # pre-protest
        time.sleep(0.01)
        _final(g, "I did not say a word. I was still thinking.", now + 2)
        _confirm_playout(g, "banter")

    _run(_go)
    assert g.dispute_hold_active() is True


def test_cut_addressing_reply_keeps_the_hold_for_the_next_turn():
    """Audit E2: the addressing reply is CUT by the player — the hold
    stays (the note stays armed) until the next generated turn confirms."""
    g, _ = _disputed_game("lily-E2")
    g.release_hold(reason="test")

    def _go():
        assert g.gated_say(None, "contest_reply", "re-check", source="test")
        _confirm_playout(g, "contest_reply", interrupted=True)
        assert g.dispute_hold_active() is True
        assert g._contest_note is not None
        assert g.gated_say(
            None, "contest_reply", "re-check again", source="test"
        )
        _confirm_playout(g, "contest_reply")

    _run(_go)
    assert g.dispute_hold_active() is False


def test_dispute_timeout_airs_a_line_instead_of_silent_progression(monkeypatch):
    monkeypatch.setenv("LILY_DISPUTE_HOLD_TIMEOUT_SECONDS", "0.05")
    g, _ = _disputed_game("lily-E-timeout")
    g.release_hold(reason="test")
    time.sleep(0.08)
    assert g.dispute_hold_active() is False
    assert _said(g, "still on your call")
    assert g._contest_note is None
    row = g.sk.question_timeline[g.sk.question_number]
    assert row["dispute_released_by"] == "timeout"


# ===========================================================================
# D3 — the settle-withdraw is the binding-denial sub-class only
# ===========================================================================


def _settle_game(session_id: str, monkeypatch) -> tuple:
    monkeypatch.setenv("LILY_RELAXED_SETTLE_SECONDS", "5")
    QD = {
        "id": "kb_d", "prompt": "Motor City?", "canonical_answer": "Detroit",
        "acceptable_answers": ["detroit"], "category": "x",
    }
    g = _dispute_game(session_id)
    g.sk.bind_speaker("S1", "Rami")
    g.armed_question = dict(QD)
    g.sk.start_question(g.armed_question)
    g.sk.round = 1
    g.sk.set_phase("round")
    g._delivered_to_playout = {g.sk.question_number}
    key = f"q_{g.sk.question_number}_delivery"
    g.say_registry.claim(key, owner="x")
    g.say_registry.confirm(key)
    g.sk.set_pacing("relaxed")
    return g, time.time()


def test_contest_shaped_restatement_in_settle_keeps_the_answer(monkeypatch):
    """FAILING-FIRST (audit C): "I say Detroit, final" — a restatement
    that hits the contest regex — WITHDREW the bound answer. Only a
    binding denial may."""
    g, now = _settle_game("lily-C", monkeypatch)

    async def _go():
        g.open_window()
        _final(g, "Chicago.", now + 2)   # a committed (wrong) answer binds
        assert g._relaxed_settle_pending == g.sk.question_number
        assert "Rami" in g.sk.answer_candidates
        await asyncio.sleep(0.05)
        _final(g, "I say Detroit, final.", now + 4)
        assert "Rami" in g.sk.answer_candidates          # kept, not withdrawn
        assert g.sk.answer_window_open is True

    _run(lambda: _go(), settle_ticks=5)


def test_binding_denial_in_settle_still_withdraws(monkeypatch):
    g, now = _settle_game("lily-C-deny", monkeypatch)

    async def _go():
        g.open_window()
        _final(g, "Michelangelo", now + 2)
        assert "Rami" in g.sk.answer_candidates
        await asyncio.sleep(0.05)
        _final(g, "What do you mean, locked in? I didn't say anything.", now + 4)
        assert "Rami" not in g.sk.answer_candidates       # withdrawn

    _run(lambda: _go(), settle_ticks=5)


def test_subclass_detectors():
    assert lily_scorekeeper.lily_detect_binding_denial(
        "I did not say a word. I was still thinking."
    )
    assert not lily_scorekeeper.lily_detect_binding_denial("I say Detroit, final")
    assert lily_scorekeeper.lily_detect_premature_adjudication_protest(
        "I'm fucking still talking. We're still preparing things."
    )
    assert not lily_scorekeeper.lily_detect_premature_adjudication_protest(
        "the answer is Aphrodite"
    )
