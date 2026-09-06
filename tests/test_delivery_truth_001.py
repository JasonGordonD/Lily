"""WO-LILY-DELIVERY-TRUTH-001 — the airing stamp binds at the FIRST FRAME,
the airing gate re-decides at the first frame, receipts never lie, the STOP
brake keeps its own acknowledgment, and the record carries all of it.

Every test here is a BEHAVIOR drive on real LilyGame/LilyAgent methods with
fake session/handles only (no source pins). Each was reproduced first by the
read-only audit (scratchpad/audit_repros.py R1/R3/R4/R5/R6/R7/R8) and fails
on main @ a380531 — see the CHANGELOG entry for the failing-first log.

Defects (audit numbering):
  A1  `_result_aired` stamped in tts_node BEFORE any frame — for
      generate_reply speeches the framework runs tts_node before
      authorization, so a composite interrupted pre-frame was "aired", the
      keyed sheet gagged on that stamp, the claim CONFIRMED, the ruling
      lost (R7).
  A5  the airing gate's decisions ran at LLM-stream end only.
  A7  reair_budget_spent CONFIRMED + journaled narration="" as aired with
      no stamp (R6).
  A4  the STOP brake cancelled its own ack; already_acked forbade a
      replacement (R1).
  A6  the framework's turn-commit interrupt kills the current speech before
      on_user_turn_completed — synchronous code acks died to their trigger.
  A3  restart/settle/late-recognition acts were not freshness/flush-exempt
      (R3/R4).
  A8  a flushed read under address_unanswered/setup_pending never armed the
      C3d resume watch.
  A9  one-utterance-one-reply matched exact text; the hook sees the JOINED
      turn (R5).
  A10a purge_game_scoped left `_stale_retry_counts` behind (R8).
  A11 every airgate decision was logger-only.
  A14 the sha-pinned hostloop fixture is a reconstruction; the real rows
      are pinned here.
"""

import asyncio
import hashlib
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_agent  # noqa: E402
import lily_say_gate  # noqa: E402
import lily_speech_delivery  # noqa: E402
from lily_agent import (  # noqa: E402
    Agent,
    LilyAgent,
    LilyGame,
    SpeechTurn,
    _SpeechHandleContextVar,
    run_say_pipeline,
)
from lily_scorekeeper import LilyScorekeeper  # noqa: E402
from test_airgate_001 import (  # noqa: E402
    _game,
    _Handle,
    _journal_reveal,
    _pipeline_agent,
)
from test_desync_fixture import FEMUR_QUESTION, _arm_question, _run  # noqa: E402
import test_restart_wo5 as R  # noqa: E402

SHEET = "It's the femur — point to Rami."
COMPOSITE = "Correct — the femur! That one goes to Rami."


# ---------------------------------------------------------------------------
# helpers — drive the REAL tts_node with the synthesizer swapped for a recorder
# ---------------------------------------------------------------------------


def _tts_agent(game):
    agent = LilyAgent.__new__(LilyAgent)
    agent._game = game
    agent._reair_regen_pending = False
    agent._empty_retry_pending = False
    return agent


def _drive_tts_node(agent, raw_text, *, speech_id):
    """Run LilyAgent.tts_node end-to-end for `speech_id` (the framework's
    SpeechHandle ContextVar pinned the way the TTS task sees it). Both
    synthesis exits are recorded; NO frame is played — this is exactly the
    generate_reply ordering (tts_node before authorization)."""
    captured = []

    async def _recording_default(agent_self, text, model_settings):
        async for chunk in text:
            captured.append(chunk)
        if False:  # pragma: no cover
            yield

    async def _recording_aligned(agent_self, full, model_settings):
        captured.append(full)
        if False:  # pragma: no cover
            yield

    original = Agent.default.tts_node
    original_aligned = LilyAgent._lily_aligned_tts_frames
    Agent.default.tts_node = _recording_default
    LilyAgent._lily_aligned_tts_frames = _recording_aligned
    try:
        async def _speak():
            _SpeechHandleContextVar.set(_Handle(speech_id))

            async def _chunks():
                yield raw_text

            async for _frame in agent.tts_node(_chunks(), None):
                pass

        _run(_speak(), agent._game)
    finally:
        Agent.default.tts_node = original
        LilyAgent._lily_aligned_tts_frames = original_aligned
    return captured


def _events(game, reason):
    return [e for e in game.airgate_events() if e["reason"] == reason]


# ---------------------------------------------------------------------------
# A1 — the stamp binds at the first frame, never at synthesis (audit R7)
# ---------------------------------------------------------------------------


def test_a1_tts_node_without_a_frame_stamps_nothing_and_sheet_airs():
    game = _game()
    _journal_reveal(game, 1, answer="the femur")
    game.sk.start_question(dict(FEMUR_QUESTION))
    agent = _tts_agent(game)
    # The organic composite runs tts_node (LLM stream ended) — and is then
    # interrupted before its first frame (the framework's authorization
    # wait / end-of-turn interrupt). No frame ever played.
    assert _drive_tts_node(agent, COMPOSITE, speech_id="speech_organic")
    assert game.result_aired_for(1) is None  # main: stamped here already
    game.on_agent_speech_finished(
        "", speech_id="speech_organic", interrupted=True,
    )
    assert game.result_aired_for(1) is None
    assert game.session.said == []
    # The narration that died pre-frame is on the record as dropped:
    (dropped,) = _events(game, "narration_dropped_before_air")
    assert dropped["speech_id"] == "speech_organic"
    assert dropped["qnum"] == 1 and dropped["stage"] == "playout_end"
    # The keyed verdict sheet reaches the gate under its own id and AIRS —
    # the claim stays PENDING (never confirmed for words nobody heard):
    assert game.say_registry.claim("q_1_reveal", owner="speech_sheet")
    assert _drive_tts_node(agent, SHEET, speech_id="speech_sheet") == [SHEET]
    assert game.say_registry.state("q_1_reveal") == lily_say_gate.CLAIM_PENDING
    assert game._transition_holds_next_delivery("test") is True  # still owed
    # Its FIRST FRAME is what stamps the fact, keyed by the airing speech:
    game.note_playout_started("speech_sheet")
    assert game.result_aired_for(1) == SHEET
    assert game._result_aired["speech_id"] == "speech_sheet"
    assert "speech_sheet" in game._playout_started_ids
    # ...and the resume/re-air consumers read only that playout fact:
    game.on_agent_speech_finished(SHEET, speech_id="speech_sheet")
    assert game.say_registry.state("q_1_reveal") == lily_say_gate.CLAIM_CONFIRMED
    assert game._transition_holds_next_delivery("test") is False


def test_a1_completed_playout_that_skipped_the_first_frame_hook_still_stamps():
    """agent_state 'speaking' can fire with current_speech unset; a
    COMPLETED playout is the strongest airing evidence and stamps at
    on_agent_speech_finished — a cut one never does."""
    game = _game()
    _journal_reveal(game, 1, answer="the femur")
    game.sk.start_question(dict(FEMUR_QUESTION))
    agent = _tts_agent(game)
    _drive_tts_node(agent, COMPOSITE, speech_id="speech_organic")
    game.on_agent_speech_finished(COMPOSITE, speech_id="speech_organic")
    assert game.result_aired_for(1) == COMPOSITE
    assert game._result_aired["speech_id"] == "speech_organic"
    assert _events(game, "narration_dropped_before_air") == []


def test_a1_resume_and_reair_consumers_read_only_the_playout_stamp():
    game = _game()
    _journal_reveal(game, 1, answer="the femur")
    _arm_question(game, FEMUR_QUESTION)
    qnum = game.sk.question_number
    game._delivery_barge_cut_qnum = qnum
    agent = _tts_agent(game)
    _drive_tts_node(agent, COMPOSITE, speech_id="speech_organic")
    # Synthesis alone changes nothing the consumers read:
    assert game._question_barge_resume_still_owed(qnum) is True
    game._user_cut_counts = {"q_1_reveal": 1}
    assert game.reair_cut_verdict(["q_1_reveal"]) is True  # re-air owed
    assert any("femur" in s.lower() for s in game.session.said)


# ---------------------------------------------------------------------------
# A5 — the gate re-decides at the first frame; a late fail interrupts
# ---------------------------------------------------------------------------


def test_a5_result_already_aired_by_first_frame_interrupts_the_sheet():
    game = _game()
    _journal_reveal(game, 1, answer="the femur")
    game.sk.start_question(dict(FEMUR_QUESTION))
    agent = _tts_agent(game)
    # Both speeches pass the ENQUEUE-time gate before either has a frame
    # (nothing stamped yet): the organic composite, then the keyed sheet.
    _drive_tts_node(agent, COMPOSITE, speech_id="speech_A")
    assert game.say_registry.claim("q_1_reveal", owner="speech_B")
    assert _drive_tts_node(agent, SHEET, speech_id="speech_B") == [SHEET]
    handle_b = _Handle("speech_B")
    game.note_speech_handle(handle_b)
    game._dispatched_act_by_speech["speech_B"] = "verdict"
    # A's first frame stamps the fact...
    game.note_playout_started("speech_A")
    assert game._result_aired["speech_id"] == "speech_A"
    # ...so B's first frame is a LATE FAIL: interrupted (force), suppressed,
    # never marked airing, claim confirmed against A's words (N+1 free).
    game.note_playout_started("speech_B")
    assert handle_b.interrupts == [True]
    assert "speech_B" in game._suppressed_speech_ids
    assert "speech_B" not in game._playout_started_ids
    assert game.say_registry.state("q_1_reveal") == lily_say_gate.CLAIM_CONFIRMED
    assert game._transition_entry(1, "verdict")["detail"]["narration"] == (
        COMPOSITE
    )
    assert game._transition_holds_next_delivery("test") is False
    # The receipt an operator pulls (lily_sessions.metadata.airgate_events):
    (late,) = _events(game, "late_result_already_aired")
    assert late["speech_id"] == "speech_B"
    assert late["act"] == "verdict"
    assert late["key"] == "q_1_reveal"
    assert late["qnum"] == 1
    assert late["stage"] == "first_frame"
    assert late["detail"]["stamped_by"] == "speech_A"
    assert late["ts"] and late["mono"]


def test_a5_ack_superseded_between_synthesis_and_first_frame_is_cut():
    game = _game()
    game.note_user_final()  # "I don't want a timer"
    assert game.gated_say(
        None, "media_mode", "[ack]", source="voice_command",
        text="Camera lane is open.",
    )
    speech_id = f"speech_{game.session._n}"
    handle = game._speech_handles[speech_id]
    turn = SpeechTurn(
        text="Camera lane is open.", raw="Camera lane is open.",
        game=game, agent=_pipeline_agent(), speech_id=speech_id,
    )
    assert run_say_pipeline(turn) is None  # fresh at enqueue
    # A newer final commits before the first frame (TTS TTFB gap):
    game.note_user_final()
    game.note_playout_started(speech_id)
    assert handle.interrupts == [True]
    assert speech_id in game._suppressed_speech_ids
    assert speech_id not in game._playout_started_ids
    (late,) = _events(game, "late_stale_reply_superseded")
    assert late["act"] == "media_mode" and late["stage"] == "first_frame"
    # A fresh ack at the first frame airs, and its record is consumed once:
    game2 = _game()
    game2.note_user_final()
    assert game2.gated_say(
        None, "media_mode", "[ack]", source="voice_command",
        text="Camera lane is open.",
    )
    sid2 = f"speech_{game2.session._n}"
    turn2 = SpeechTurn(
        text="Camera lane is open.", raw="Camera lane is open.",
        game=game2, agent=_pipeline_agent(), speech_id=sid2,
    )
    assert run_say_pipeline(turn2) is None
    game2.note_playout_started(sid2)
    assert game2._speech_handles[sid2].interrupts == []
    assert sid2 in game2._playout_started_ids
    assert sid2 not in (game2._conversational_dispatch_meta or {})


# ---------------------------------------------------------------------------
# A7 — no stamp, no "aired" receipt (audit R6)
# ---------------------------------------------------------------------------


def test_a7_reair_budget_spent_journals_verdict_dropped_not_aired():
    game = _game()
    _journal_reveal(game, 1, answer="the femur")
    game._user_cut_counts = {"q_1_reveal": 2}  # cut twice, no frame ever
    assert game.result_aired_for(1) is None
    assert game.reair_cut_verdict(["q_1_reveal"]) is False
    assert game.session.said == []
    # N+1 still releases (no wedge)...
    assert game.say_registry.state("q_1_reveal") == lily_say_gate.CLAIM_CONFIRMED
    assert game._transition_holds_next_delivery("test") is False
    # ...but the record is HONEST: dropped, no narration, the reason named.
    detail = game._transition_entry(1, "verdict")["detail"]
    assert detail["verdict_dropped"] is True
    assert detail["narration"] is None
    assert detail["source"] == "verdict_dropped"
    assert detail["drop_reason"] == "reair_budget_spent"
    (ev,) = _events(game, "verdict_dropped")
    assert ev["key"] == "q_1_reveal" and ev["qnum"] == 1
    assert ev["detail"]["reason"] == "reair_budget_spent"
    assert ev["detail"]["cuts"] == 2
    assert ev["detail"]["answer"] == "the femur"
    assert ev["detail"]["winner"] == "Rami"
    # The ruling rides into the next composite's context, once:
    assert "the femur" in game._state_note
    assert "Rami" in game._state_note
    assert "question 1" in game._state_note


def test_a7_a_real_stamp_still_binds_the_aired_words():
    game = _game()
    _journal_reveal(game, 1, answer="the femur")
    game.note_airing_pending("speech_A", COMPOSITE)
    game.note_playout_started("speech_A")
    game._user_cut_counts = {"q_1_reveal": 2}
    assert game.reair_cut_verdict(["q_1_reveal"]) is False
    detail = game._transition_entry(1, "verdict")["detail"]
    assert detail["narration"] == COMPOSITE
    assert detail.get("verdict_dropped") is None
    assert _events(game, "verdict_dropped") == []


# ---------------------------------------------------------------------------
# A4 — the brake keeps its own acknowledgment (audit R1)
# ---------------------------------------------------------------------------


def test_a4_stop_ack_survives_the_debounced_interim_reroute():
    game = _game()
    game.armed_question = dict(FEMUR_QUESTION)
    assert game.route_stop_from_interim("Stop stop stop") is True
    ack_id = f"speech_{game.session._n}"
    ack = game._speech_handles[ack_id]
    assert game.session.interrupted == 1
    # the salvo keeps growing past the 2s debounce ("Stop stop stop stop
    # stop ..." lasted ~7s live):
    game._interim_stop_routed_at -= 2.1
    assert game.route_stop_from_interim("Stop stop stop stop stop stop")
    assert ack.interrupts == []
    assert ack_id not in game._suppressed_speech_ids
    assert game.session.interrupted == 1  # no second session.interrupt()
    assert len([s for s in game.session.said if "Stopped" in s]) == 1
    (survived,) = _events(game, "ack_survives_brake")
    assert survived["speech_id"] == ack_id and survived["act"] == "stop_ack"
    assert survived["stage"] == "brake"
    # The ack's first frame is the live receipt that it aired:
    game.note_playout_started(ack_id)
    (airing,) = _events(game, "ack_airing")
    assert airing["speech_id"] == ack_id and airing["stage"] == "first_frame"


def test_a4_stop_ack_survives_the_finals_reentry():
    game = _game()
    game.armed_question = dict(FEMUR_QUESTION)
    game.route_stop_from_interim("Stop stop stop")
    ack_id = f"speech_{game.session._n}"
    ack = game._speech_handles[ack_id]
    assert game.maybe_route_stop("Stop stop stop stop stop stop stop.") is True
    assert ack.interrupts == []
    assert game.session.interrupted == 1
    assert len([s for s in game.session.said if "Stopped" in s]) == 1


def test_a4_first_entry_still_brakes_everything_else():
    game = _game()
    game.say_registry.claim("q_3_delivery", owner="live1")
    live = _Handle("live1")
    game.note_speech_handle(live)
    game._dispatched_act_by_speech["live1"] = "question_delivery"
    game.handle_stop_primitive("Lily, stop!")
    assert live.interrupts == [True]
    assert game.say_registry.state("q_3_delivery") is None
    assert game.session.interrupted == 1
    assert game._hold_active is True
    # A hold ack already airing is never braked either (the handle is
    # planted: handle_hold_request's own dispatch is blocked by the hold it
    # just entered — source="hold_request" is not hold-exempt — a
    # pre-existing, out-of-scope finding recorded in the WO report):
    game2 = _game()
    hold = _Handle("speech_hold")
    game2.note_speech_handle(hold)
    game2._dispatched_act_by_speech["speech_hold"] = "hold_ack"
    game2.handle_stop_primitive("stop")
    assert hold.interrupts == []
    assert "speech_hold" not in game2._suppressed_speech_ids


# ---------------------------------------------------------------------------
# A6 — code acks are non-interruptible; the turn-commit interrupt cannot
# kill them
# ---------------------------------------------------------------------------


def _framework_turn_commit(current_speech):
    """agent_activity._user_turn_completed_task at 1.6.x, modeled: the
    current speech is interrupted BEFORE on_user_turn_completed — unless it
    does not allow interruptions, in which case the reply is skipped."""
    if not current_speech.allow_interruptions:
        return "skipped_reply"
    current_speech.interrupt()
    return "interrupted"


def test_a6_stop_ack_survives_the_turn_commit_interrupt():
    game = _game()
    game.handle_stop_primitive("stop stop stop")
    ack_id = f"speech_{game.session._n}"
    ack = game._speech_handles[ack_id]
    assert game.session.say_kwargs[-1] == {"allow_interruptions": False}
    assert ack.allow_interruptions is False
    # The same utterance's turn commits ms later with the ack current:
    assert _framework_turn_commit(ack) == "skipped_reply"
    assert ack.interrupts == []
    assert ack.interrupted is False
    # Lily's own brake still reaches it (force=True):
    game.cancel_speech(ack_id, reason="game_restart")
    assert ack.interrupts == [True]


def test_a6_every_code_ack_lane_is_non_interruptible():
    game = _game()
    assert game.gated_say(
        None, "hold_ack", "[ack]", source="hold_release", text="Take your time.",
    )
    assert game.session.say_kwargs[-1] == {"allow_interruptions": False}
    game2 = _game()
    game2.game_started = True
    game2.sk.question_number = 2
    game2.sk.set_phase("round")
    assert game2.request_restart(
        source="voice_command", requester="Rami", text="restart the game",
    ) == "confirm_armed"
    assert game2.session.say_kwargs[-1] == {"allow_interruptions": False}
    # A game-lane payload keeps the session default:
    game3 = _game()
    assert game3.gated_say(
        None, "answer_receipt", "[receipt]", source="test", text="Correct!",
    )
    assert game3.session.say_kwargs[-1] == {}


# ---------------------------------------------------------------------------
# A3 — restart / settle / late-recognition acts are obligation acks
# (audit R3, R4) + the suppression hook
# ---------------------------------------------------------------------------


def test_a3_restart_confirm_and_ack_are_freshness_exempt():
    game = _game()
    game.game_started = True
    game.sk.question_number = 2
    game.sk.set_phase("round")
    game.note_user_final()  # "restart the game"
    assert game.request_restart(
        source="voice_command", requester="Rami", text="restart the game",
    ) == "confirm_armed"
    sid = f"speech_{game.session._n}"
    game.note_user_final()  # another final commits before tts_node
    turn = SpeechTurn(
        text=game._RESTART_CONFIRM_LINE, raw=game._RESTART_CONFIRM_LINE,
        game=game, agent=_pipeline_agent(), speech_id=sid,
    )
    assert run_say_pipeline(turn) is None  # main: stale_reply_superseded
    assert game._pending_restart_confirm is not None
    game.note_user_final()
    game.execute_restart(source="voice_confirm", requester="Rami")
    sid = f"speech_{game.session._n}"
    game.note_user_final()
    turn = SpeechTurn(
        text=game._RESTART_DONE_LINE, raw=game._RESTART_DONE_LINE,
        game=game, agent=_pipeline_agent(), speech_id=sid,
    )
    assert run_say_pipeline(turn) is None
    for act in (
        "restart_confirm", "restart_ack", "restart_declined",
        "start_settle_hold", "late_recognition",
    ):
        assert act in lily_speech_delivery._FRESHNESS_EXEMPT_ACTS
        assert act in lily_speech_delivery._BARGE_FLUSH_EXEMPT_ACTS


def test_a3_barge_flush_never_flushes_a_queued_restart_confirm():
    game = _game()
    game.game_started = True
    game.sk.question_number = 2
    game.request_restart(
        source="voice_command", requester="Rami", text="restart the game",
    )
    sid = f"speech_{game.session._n}"
    handle = game._speech_handles[sid]
    assert game.flush_queued_dispatches_on_barge(cut_speech_id="other") == []
    assert handle.interrupts == []
    assert game._pending_restart_confirm is not None


def test_a3_on_dispatch_suppressed_fires_for_every_silence_and_flush_path():
    game = _game()
    seen = []
    game.add_dispatch_suppressed_listener(
        lambda act, sid, reason, **facts: seen.append((act, sid, reason))
    )
    # (1) a pipeline Silence, driven through the real tts_node:
    game.note_user_final()
    assert game.gated_say(
        None, "media_mode", "[ack]", source="voice_command",
        text="Camera lane is open.",
    )
    sid = f"speech_{game.session._n}"
    game.note_user_final()
    agent = _tts_agent(game)
    assert _drive_tts_node(agent, "Camera lane is open.", speech_id=sid) == []
    assert ("media_mode", sid, "stale_reply_superseded") in seen
    (ev,) = _events(game, "stale_reply_superseded")
    assert ev["speech_id"] == sid and ev["stage"] == "enqueue"
    # (2) a barge flush / cancel:
    game.gated_say(None, "media_mode", "[ack]", source="test", text="Hi.")
    sid2 = f"speech_{game.session._n}"
    game.flush_queued_dispatches_on_barge(cut_speech_id=None)
    assert ("media_mode", sid2, "user_barge_flush") in seen
    # (3) the STOP brake's cancels:
    game.gated_say(None, "media_mode", "[ack]", source="test", text="Yo.")
    sid3 = f"speech_{game.session._n}"
    game.handle_stop_primitive("stop")
    assert ("media_mode", sid3, "stop_primitive") in seen
    # A listener that raises never breaks the path:
    def _boom(*a, **k):
        raise RuntimeError("listener bug")
    game.add_dispatch_suppressed_listener(_boom)
    game.cancel_speech("ghost", reason="test")


# ---------------------------------------------------------------------------
# A8 — a flushed read under address debt arms the C3d watch
# ---------------------------------------------------------------------------


def test_a8_flushed_read_under_address_unanswered_arms_the_resume_watch():
    game = _game()
    _arm_question(game, FEMUR_QUESTION)
    qnum = game.sk.question_number
    delivery_key = f"q_{qnum}_delivery"
    game._awaiting_address_since = time.time()  # expect_delivery is a no-op
    read = _Handle("speech_read")
    game.note_speech_handle(read)
    assert game.say_registry.claim(delivery_key, owner="speech_read")
    assert game.flush_queued_dispatches_on_barge(cut_speech_id=None) == [
        "speech_read"
    ]
    assert game.say_registry.state(delivery_key) is None
    assert game._pending_delivery_qnum is None  # blocked by the debt (P0-G)
    # main: nothing re-armed and no watch — the question sat half-aired.
    assert game._delivery_barge_cut_qnum == qnum
    assert game._question_barge_resume_still_owed(qnum) is True


# ---------------------------------------------------------------------------
# A9 — ownership by containment, time-bounded (audit R5)
# ---------------------------------------------------------------------------


def _prehook_owns(game, text):
    from livekit.agents import StopResponse

    agent = LilyAgent.__new__(LilyAgent)
    agent._game = game
    message = type("Message", (), {"content": [text]})()

    async def scenario():
        try:
            await agent.on_user_turn_completed(None, message)
        except StopResponse:
            return True
        return False

    return asyncio.run(scenario())


def test_a9_joined_turn_is_owned_by_the_final_the_code_lane_answered():
    game = _game()
    game.sk.bind_speaker("S1", "Rami")
    game.mark_deterministic_reply("I don't want a timer")  # final #1
    # finals #1 + #2 joined by the framework into one message:
    assert _prehook_owns(game, "I don't want a timer. it stresses me out")
    assert game._deterministic_reply_texts == []  # consumed, not left behind


def test_a9_mark_never_owns_an_unrelated_or_stale_turn():
    game = _game()
    game.sk.bind_speaker("S1", "Rami")
    game.mark_deterministic_reply("no timer please")
    assert game.consume_deterministic_reply("what was the score") is False
    assert game._deterministic_reply_texts == ["no timer please"]
    # Token-bounded containment: "timer" alone is not the marked final.
    assert game.consume_deterministic_reply("the timer is fine") is False
    # Past the time bound the mark owns nothing and is dropped:
    game._deterministic_reply_marked_at["no timer please"] -= (
        lily_speech_delivery._DETERMINISTIC_REPLY_TTL_SECONDS + 1
    )
    assert game.consume_deterministic_reply("no timer please") is False
    assert game._deterministic_reply_texts == []


# ---------------------------------------------------------------------------
# A10a — purge pops the stale-retry counts (audit R8)
# ---------------------------------------------------------------------------


def test_a10a_restart_purge_pops_stale_retry_counts():
    game = R._live_game()
    game._stale_retry_counts = {"q_1_delivery": 2, "q_3_reveal": 1}
    game.execute_restart(source="test")
    assert game._stale_retry_counts == {}


def test_a10a_registry_purge_pops_game_scoped_counts_only():
    reg = lily_say_gate.SpeechActRegistry()
    reg.claim("q_2_verdict")
    counts = {"q_2_verdict": 1, "q_9_delivery": 2, "session_greet": 1}
    out = reg.purge_game_scoped(retry_counts=counts)
    assert out["released"] == ["q_2_verdict"]
    assert sorted(out["retry_counts_cleared"]) == ["q_2_verdict", "q_9_delivery"]
    assert counts == {"session_greet": 1}
    # No counts given: the pre-WO contract holds.
    assert reg.purge_game_scoped()["retry_counts_cleared"] == []


# ---------------------------------------------------------------------------
# A11 — airgate events on the record, in the session metadata lane
# ---------------------------------------------------------------------------


def test_a11_airgate_events_are_bounded_and_ride_session_metadata():
    game = LilyGame.bare(sk=LilyScorekeeper("lily-airgate-lane"))
    for i in range(lily_speech_delivery._AIRGATE_EVENTS_CAP + 5):
        game.note_airgate_event(
            "result_already_aired", act="verdict", key=f"q_{i}_reveal",
            speech_id=f"speech_{i}", qnum=i, stage="enqueue",
        )
    events = game.airgate_events()
    assert len(events) == lily_speech_delivery._AIRGATE_EVENTS_CAP
    assert events[-1]["key"] == (
        f"q_{lily_speech_delivery._AIRGATE_EVENTS_CAP + 4}_reveal"
    )
    assert set(events[-1]) == {
        "reason", "act", "key", "speech_id", "qnum", "stage", "detail",
        "ts", "mono",
    }
    payload = lily_agent.lily_session_metadata(game, game.sk, {}, None)
    assert payload["airgate_events"] == events
    assert payload["game_restarts"] == []  # same lane, beside game_restarts
    assert "identity_promotions" in payload and "voice_identity" in payload
    empty = lily_agent.lily_session_metadata(
        LilyGame.bare(sk=LilyScorekeeper("x")), None, {}, None
    )
    assert empty["airgate_events"] == []


# ---------------------------------------------------------------------------
# A14 — the REAL rows of the 17:51 call are the pinned evidence fixture
# ---------------------------------------------------------------------------

_FIXTURES = Path(__file__).resolve().parent / "fixtures"
_REAL_ROWS = _FIXTURES / "live_20260814_1751_gameflow.txt"
_REAL_ROWS_SHA256 = (
    "9f3bf862697a09435f3f1d384b812ff1afbc2f93fd18c30e27455caaa19ee2d5"
)


def test_a14_real_row_fixture_is_pinned_and_the_reconstruction_is_notes():
    data = _REAL_ROWS.read_bytes()
    assert hashlib.sha256(data).hexdigest() == _REAL_ROWS_SHA256
    assert "lily_transcripts" in data.decode("utf-8")  # sourced from rows
    notes = (_FIXTURES / "live_20260814_1751_hostloop.txt").read_text(
        encoding="utf-8"
    )
    header = "\n".join(notes.splitlines()[:12])
    assert "NOT A RECORD" in header
    assert "RECONSTRUCTION" in header
