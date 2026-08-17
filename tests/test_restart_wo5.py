"""WO-LILY-RESTART-001 — Lily can RESTART the game on request.

The design under test (operator directive, built on WO-1..4 machinery):

* A DETERMINISTIC restart detector in the command lane
  (lily_detect_restart_game / control command "restart_game") — the same
  family as the stop/pacing detectors. "restart the game" / "start over" /
  "new game" / "from the top" fire; "can we restart the question" is
  question-scoped and never does; a bare "start" is the START intent (C7),
  never a restart.
* CONFIRM-GATED: a detected restart on a table with a live game (any
  question dispatched or score > 0) asks ONE deterministic confirm
  ("Restart from scratch — scores gone. Sure?") and proceeds only on an
  affirmative final from a player; a bare-lobby restart skips the confirm.
  The confirm does not deadlock with WO-3's dispute-hold or WO-1's hold
  machinery — restart intent during a hold is legal.
* THE RESET: scores / round / question state / journals / claims for the
  dead game cleared; roster and identity/recognition state KEPT
  (recognition_aired stays stamped — no re-welcome monologue). Back to the
  lobby with WO-3's start gate armed: a fresh start intent is required to
  begin again. Every WO-1 delivery obligation of the dead game is
  cancelled with accounting (claims released/purged, watchdogs stood
  down) — no dead-game verdict can re-air post-restart.
* The lily_restart_game TOOL verifies the detector-set intent fact (the
  WO-3 start-gate discipline) — model judgment alone can never wipe a
  scoreboard.
* Telemetry: each restart lands in lily_sessions.metadata.game_restarts
  (the identity_promotions lane) carrying the dead game's record.
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_audeering_consumers
import lily_say_gate
import lily_scorekeeper
from lily_agent import LilyAgent, LilyGame
from lily_scorekeeper import (
    LilyScorekeeper,
    lily_detect_control_command,
    lily_detect_restart_game,
)


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


def _make_game(*, started: bool) -> LilyGame:
    game = LilyGame.bare()
    game.session = _FakeSession()
    game.agent = _FakeAgentHandle()
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game.sk = LilyScorekeeper("lily-restart-test")
    game.sk.bind_speaker("S1", "Rami")
    game.game_started = started
    game._game_start_committed = started
    game.ui_phase = "question" if started else "lobby"
    game.game_over = False
    game.finale_sent = False
    game.armed_question = None
    game.next_question = None
    game.used_prompts = []
    game.supabase = None
    game.acoustic = lily_audeering_consumers.LilyAcousticState()
    game.memory_block = ""
    game.pending_clarify = {}
    game._addressee_rows = {}
    game._user_turn_index = 0
    game.forget_state = "idle"
    game.forget_spoken_confirmed = False
    game.forget_requester = None
    game.prefs = {}
    game.highlights = []
    game.group_id = "grp_test"
    game.eliminated = []
    game.prewager_standings = None
    return game


def _live_game() -> LilyGame:
    """A mid-game table with stakes: q3 on the board, Rami up 2."""
    game = _make_game(started=True)
    game.sk.set_phase("round")
    game.sk.round = 1
    game.sk.question_number = 3
    game.sk.apply_score_event("Rami", cause="answer", correct=True, points=1)
    game.sk.apply_score_event("Rami", cause="bonus", points=1)
    game.sk.note_question_time("window_opened_at")
    return game


def _segment(game: LilyGame, text: str, label: str = "S1"):
    now = time.time()
    result = game.sk.on_transcript_segment(
        text=text, speaker_label=label, now=now, segment_start_time=now
    )
    game.on_transcript_event(result, text, speaker_label=label, segment_ts=now)
    return result


async def _drive(game: LilyGame, *texts: str, label: str = "S1"):
    for text in texts:
        _segment(game, text, label=label)
        await asyncio.sleep(0.01)


# ---------------------------------------------------------------------------
# The detector — deterministic command lane, positives and negatives
# ---------------------------------------------------------------------------


def test_restart_detector_positives():
    for text in [
        "restart the game",
        "Restart the game.",
        "can we restart the game",
        "start over",
        "Let's start over.",
        "start again",
        "restart",
        "Okay, restart.",
        "new game",
        "let's play a new game",
        "from the top",
        "take it from the top",
        "start from scratch",
        "start the game over",
        "start the quiz again",
        "restart the whole thing",
    ]:
        assert lily_detect_restart_game(text) is True, text


def test_restart_detector_negatives():
    for text in [
        # question-scoped, not game-scoped — never kills the game:
        "can we restart the question",
        "restart the question",
        "restart that question please",
        "read the question from the top",
        # a bare "start" is the START intent (C7), never a restart:
        "start",
        "Starts.",
        "let's start",
        "start the game",
        "ready to start",
        # negations are the opposite request:
        "don't make us start over",
        "no need to start over",
        "we won't restart",
        # ordinary speech with start-ish words:
        "she starts crying every time",
        "before we start, one question",
        "",
    ]:
        assert lily_detect_restart_game(text) is False, text


def test_control_command_routes_restart_before_start():
    """"start the game over" contains the start phrase — the restart
    reading owns it. A bare start stays start_game; skip stays skip."""
    assert lily_detect_control_command("restart the game") == "restart_game"
    assert lily_detect_control_command("start the game over") == "restart_game"
    assert lily_detect_control_command("start over") == "restart_game"
    assert lily_detect_control_command("new game") == "restart_game"
    assert lily_detect_control_command("start the game") == "start_game"
    assert lily_detect_control_command("start") == "start_game"
    assert lily_detect_control_command("skip") == "skip"
    assert lily_detect_control_command("can we restart the question") is None


# ---------------------------------------------------------------------------
# Confirm gate on a live game; no confirm on a bare lobby
# ---------------------------------------------------------------------------


def test_live_game_restart_asks_one_confirm_then_resets_on_yes():
    game = _live_game()

    async def _run():
        await _drive(game, "Lily, restart the game")
        # Confirm armed, NOTHING reset yet.
        assert game._pending_restart_confirm is not None
        assert game.game_started is True
        assert game.sk.question_number == 3
        assert game.sk.ledger_scores()["Rami"] == 2
        confirms = [s for s in game.session.said if "scores gone" in s.lower()]
        assert len(confirms) == 1

        await _drive(game, "yes")
        assert game._pending_restart_confirm is None
        assert game.game_started is False
        assert game.sk.phase == "lobby"
        assert game.ui_phase == "lobby"
        assert game.sk.question_number == 0
        assert game.sk.ledger_scores()["Rami"] == 0

    asyncio.run(_run())


def test_live_game_restart_no_keeps_the_game():
    game = _live_game()

    async def _run():
        await _drive(game, "start over")
        assert game._pending_restart_confirm is not None
        await _drive(game, "no, keep going")
        assert game._pending_restart_confirm is None
        # Nothing touched, and the stale intent fact died with the no —
        # the tool cannot restart later on this dead ask.
        assert game.game_started is True
        assert game.sk.question_number == 3
        assert game.sk.ledger_scores()["Rami"] == 2
        assert game.restart_intent_present() is False

    asyncio.run(_run())


def test_ambiguous_reply_stays_pending_and_destroys_nothing():
    game = _live_game()

    async def _run():
        await _drive(game, "restart the game")
        await _drive(game, "what does that mean exactly")
        assert game._pending_restart_confirm is not None
        assert game.game_started is True
        assert game.sk.ledger_scores()["Rami"] == 2

    asyncio.run(_run())


def test_restated_restart_command_counts_as_the_affirmative():
    game = _live_game()

    async def _run():
        await _drive(game, "restart the game")
        assert game._pending_restart_confirm is not None
        await _drive(game, "yes, restart the game")
        assert game.game_started is False
        assert game.sk.ledger_scores()["Rami"] == 0

    asyncio.run(_run())


def test_bare_lobby_restart_skips_the_confirm():
    game = _make_game(started=False)
    game._game_start_committed = False

    async def _run():
        await _drive(game, "let's start over")
        # Nothing to lose: no confirm, straight to (already-fresh) lobby.
        assert game._pending_restart_confirm is None
        assert game.game_started is False
        assert game.sk.phase == "lobby"
        # One deterministic ack aired; the confirm line never did.
        assert not any("scores gone" in s.lower() for s in game.session.said)
        assert any("fresh" in s.lower() for s in game.session.said)
        # "let's start over" must NOT leave the setup parser's start flag
        # behind as phantom start intent for the auto-start net.
        assert game.start_intent_present() is False

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# The reset: roster + recognition kept, scores/claims/journals cleared
# ---------------------------------------------------------------------------


def test_reset_keeps_roster_and_recognition_clears_game():
    game = _live_game()
    game.sk.bind_speaker("S2", "Dana")
    game.memory_block = "[RETURNING TABLE]\nrematch energy."
    game.note_recognition_aired("greet")
    aired_before = game.recognition_aired()
    assert aired_before is not None
    gen_before = game.sk.roster_gen
    # Dead-game journals/claims in every WO-1 shape:
    game.say_registry.claim("q_3_verdict", owner="sp_dead")
    game.say_registry.claim("q_1_delivery", owner="sp_old")
    game.say_registry.confirm("q_1_delivery")
    game.say_registry.claim("session_greet", owner="sp_greet")
    game.say_registry.confirm("session_greet")
    game._transition_journal = {3: [{"stage": "reveal"}]}
    game._open_transition_qnum = 3
    game._result_aired = {"qnum": 3, "text": "It was Paris.", "at": 1.0}

    game.execute_restart(source="test")

    # The PEOPLE survive: both seats, labels intact, recognition stamped.
    assert set(game.sk.players) == {"Rami", "Dana"}
    assert game.sk.players["Rami"]["speaker_label"] == "S1"
    assert game.recognition_aired() == aired_before
    assert game._late_recognition_fired is True  # no re-welcome monologue
    assert game.memory_block  # memory is not forgotten by a restart
    # The GAME is dead: scores, ledger, journals, claims, receipts.
    assert game.sk.ledger_scores() == {"Rami": 0, "Dana": 0}
    assert game.sk.players["Rami"]["score"] == 0
    assert game.sk.score_ledger == []
    assert game.sk.question_number == 0
    assert game.sk.round == 0
    assert game.sk.phase == "lobby"
    assert game._transition_journal == {}
    assert game._open_transition_qnum is None
    assert game._result_aired is None
    assert game.say_registry.state("q_3_verdict") is None
    assert game.say_registry.state("q_1_delivery") is None
    # Session-scoped keys survive — a restart never re-opens the greeting.
    assert game.say_registry.state("session_greet") == (
        lily_say_gate.CLAIM_CONFIRMED
    )
    # UI truth: lobby phase + a roster_gen bump ordering the zero-score
    # payload republish after every pre-restart payload.
    assert game.ui_phase == "lobby"
    assert game.sk.roster_gen > gen_before
    assert all(p["score"] == 0 for p in game._players_payload())


def test_registry_purge_is_scoped_to_game_keys():
    reg = lily_say_gate.SpeechActRegistry()
    reg.claim("q_2_verdict")
    reg.claim("round_1_scores")
    reg.claim("finale")
    reg.claim("q_5_delivery")
    reg.confirm("q_5_delivery")
    reg.claim("session_rejoin")
    reg.confirm("session_rejoin")
    out = reg.purge_game_scoped()
    assert sorted(out["released"]) == ["finale", "q_2_verdict", "round_1_scores"]
    assert out["dropped_confirmed"] == ["q_5_delivery"]
    assert reg.state("session_rejoin") == lily_say_gate.CLAIM_CONFIRMED
    assert reg.state("q_2_verdict") is None


def test_post_restart_start_requires_fresh_intent():
    game = _live_game()
    game.note_player_start_intent(source="voice_command", text="let's play")
    game._setup_start_requested = True
    assert game.start_intent_present() is True

    game.execute_restart(source="test")

    # WO-3's lobby-settle start gate is re-armed: no stale intent survives.
    assert game.start_intent_present() is False
    assert game.start_gate_blocked_reason() == "no_start_intent"
    # And the per-final auto-start net cannot fire the dead ask.
    calls = []

    async def _record_start(source: str) -> None:
        calls.append(source)

    game.start_game = _record_start
    game._maybe_auto_start_after_lobby()
    assert calls == []


# ---------------------------------------------------------------------------
# No dead-game re-airs (WO-1 accounting, never bare-drop)
# ---------------------------------------------------------------------------


def test_no_dead_game_reairs_post_restart():
    game = _live_game()
    game.armed_question = {"id": "q_dead", "prompt": "x"}
    game._delivery_barge_cut_qnum = 3
    game.say_registry.claim("q_3_verdict", owner="sp_dead")
    game._result_aired = {"qnum": 3, "text": "Paris.", "at": 1.0}
    game._verdict_reair_counts = {"q_3_verdict": 0}
    game._user_cut_counts = {"q_3_verdict": 1}

    game.execute_restart(source="test")

    # The claim is GONE (a pending _stale_claim_watch exits on the
    # not-PENDING read), the barge-resume obligation is discharged, and a
    # game-lane dispatch is refused outright in the lobby.
    assert game.say_registry.state("q_3_verdict") is None
    assert game._question_barge_resume_still_owed(3) is False
    assert game.result_aired_for(3) is None
    assert game._verdict_reair_counts == {}
    assert game.gated_say(
        None, "verdict", "dead game verdict", source="watchdog"
    ) is False
    assert game.session.instructions == []  # nothing generated for it


def test_restart_cancels_inflight_dead_game_speech():
    game = _live_game()

    class _Handle:
        def __init__(self):
            self.interrupted = False

        def interrupt(self, force=False):
            self.interrupted = True

    handle = _Handle()
    game._speech_handles = {"sp_dead": handle}
    game.say_registry.claim("q_3_verdict", owner="sp_dead")

    game.execute_restart(source="test")

    assert handle.interrupted is True
    assert "sp_dead" in game._suppressed_speech_ids
    assert game.say_registry.state("q_3_verdict") is None


# ---------------------------------------------------------------------------
# Restart is legal during hold / dispute / sticky STOP — no deadlock
# ---------------------------------------------------------------------------


def test_restart_confirm_airs_during_hold_and_dispute():
    game = _live_game()
    game.enter_hold(reason="player_hold_request")
    game._dispute_hold_since = time.time()
    game._dispute_hold_reason = "result_aired"

    outcome = game.request_restart(source="voice_command", requester="Rami")
    assert outcome == "confirm_armed"
    # The confirm reached the air THROUGH the hold (exempt source), so the
    # gate cannot deadlock the restart behind the hold machinery.
    assert any("scores gone" in s.lower() for s in game.session.said)

    game.resolve_restart_confirm("yes", "Rami")
    assert game.game_started is False
    assert game._hold_active is False
    assert game.dispute_hold_active() is False


def test_stopped_game_start_over_restarts_not_resumes():
    """"start over" during a sticky STOP is a RESTART ask, not a resume —
    pre-WO the resume intent set silently resumed the game the table just
    disowned."""
    game = _live_game()
    game._delivery_stop_sticky = True

    async def _run():
        await _drive(game, "start over")
        # Not resumed: sticky still set, no delivery restarted — the
        # deterministic confirm is out instead.
        assert game._pending_restart_confirm is not None
        assert game._delivery_stop_sticky is True
        assert game.game_started is True

        await _drive(game, "yeah")
        assert game.game_started is False
        assert game._delivery_stop_sticky is False
        assert game.sk.ledger_scores()["Rami"] == 0

    asyncio.run(_run())


def test_plain_resume_still_resumes():
    """Invariance pin: an ordinary resume command is untouched by the
    restart lane."""
    game = _live_game()
    game._delivery_stop_sticky = True

    async def _run():
        await _drive(game, "keep going")
        assert game._delivery_stop_sticky is False
        assert game._pending_restart_confirm is None
        assert game.game_started is True

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# The tool verifies the detector-set fact (never model judgment)
# ---------------------------------------------------------------------------


def _tool(game: LilyGame) -> str:
    agent = LilyAgent.__new__(LilyAgent)
    agent._game = game
    return asyncio.run(LilyAgent.lily_restart_game.__wrapped__(agent, None))


def test_tool_refuses_without_detector_set_intent():
    game = _live_game()
    msg = _tool(game)
    assert "no_restart_intent" in msg
    assert game.game_started is True
    assert game.sk.ledger_scores()["Rami"] == 2


def test_tool_arms_confirm_with_intent_and_stakes():
    game = _live_game()
    game.note_player_restart_intent(
        source="voice_command", text="restart the game"
    )
    msg = _tool(game)
    assert "confirm_required" in msg
    assert game._pending_restart_confirm is not None
    assert game.game_started is True  # nothing reset on the tool call
    # A second call while pending reports the state, never doubles.
    msg2 = _tool(game)
    assert "confirm_pending" in msg2
    confirms = [s for s in game.session.said if "scores gone" in s.lower()]
    assert len(confirms) == 1


def test_tool_restarts_bare_lobby_with_intent():
    game = _make_game(started=False)
    game._game_start_committed = False
    game.note_player_restart_intent(source="voice_command", text="new game")
    msg = _tool(game)
    assert msg.startswith("RESTARTED")
    assert game.sk.phase == "lobby"
    assert game.restart_intent_present() is False  # the fact was consumed


def test_tool_names_already_lobby():
    game = _make_game(started=False)
    game._game_start_committed = False
    msg = _tool(game)
    assert "already_lobby" in msg


# ---------------------------------------------------------------------------
# Telemetry: game 1 ended by restart, on the record
# ---------------------------------------------------------------------------


def test_restart_lands_in_the_session_telemetry_lane():
    game = _live_game()
    timeline_before = dict(game.sk.question_timeline)
    assert timeline_before  # the dead game HAS a timeline to preserve

    game.execute_restart(source="voice_confirm", requester="Rami")

    events = game._game_restart_events
    assert events is not None and len(events) == 1
    event = events[0]
    assert event["source"] == "voice_confirm"
    assert event["requester"] == "Rami"
    dead = event["dead_game"]
    assert dead["question_number"] == 3
    assert dead["scores"]["Rami"] == 2
    # The dead game's timeline MOVED into the record — never bare-dropped —
    # and the live timeline is fresh for game 2.
    assert dead["question_timeline"] == timeline_before
    assert game.sk.question_timeline == {}
