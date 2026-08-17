"""WO-LILY-AIRGATE-001 — the dequeue-time airing gate, user-cut discipline,
STOP off the finals-only path, and one-utterance-one-reply.

Live call 2026-08-14 17:51 EDT (fixture: fixtures/live_20260814_1751_
hostloop.txt): triple verdict airings, stale acks colliding with answers,
a floor line fired over the player's answer, STOP ignored 7-17s plus a
double acknowledgment. Root cause (read-only investigation): the pipeline
decided "should this air?" at ENQUEUE only — nothing re-checked at playout,
`_result_aired` had no reader in the playout path, the re-air/watchdog lanes
never consulted it, "stop stop stop…" never finalized so the finals-only
consult sat deaf, and no command path marked turn ownership so the organic
lane doubled every code ack.

Deliverable (i) — the keyed-sheet suppression at yield — extends
test_barge_resilience_001.py; this file carries (ii)-(vi) plus the S13
fixture pin.
"""

import asyncio
import hashlib
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_say_gate  # noqa: E402
from test_desync_fixture import (  # noqa: E402
    FEMUR_QUESTION,
    _arm_question,
    _make_game,
    _run,
)


class _Handle:
    def __init__(self, sid):
        self.id = sid
        self.interrupts = []

    def interrupt(self, *, force=False):
        self.interrupts.append(force)
        return self


class _HandleSession:
    """Fake session whose dispatch lanes return SpeechHandle-shaped objects
    (the AIRGATE freshness meta is keyed by speech_id, so the say/reply
    lanes must mint ids the way the framework does)."""

    def __init__(self):
        self.instructions = []
        self.said = []
        self.interrupted = 0
        self._n = 0

    def _handle(self):
        self._n += 1
        return _Handle(f"speech_{self._n}")

    def generate_reply(self, instructions):
        self.instructions.append(instructions)
        return self._handle()

    def say(self, text, *a, **k):
        self.said.append(text)
        return self._handle()

    def interrupt(self):
        self.interrupted += 1


def _game():
    game = _make_game()
    game.session = _HandleSession()
    game.publish_attributes_nowait = lambda: None
    return game


def _pipeline_agent():
    agent = type("_PipelineAgent", (), {})()
    agent._reair_regen_pending = False
    agent._empty_retry_pending = False
    return agent


def _journal_reveal(game, qnum, *, answer, correct=True, winner="Rami"):
    owner = f"owner_{qnum}"
    assert game.open_question_transition(qnum, owner=owner, source="test")
    game.journal_transition(
        qnum, "reveal", owner=owner,
        detail={"answer": answer, "correct": correct, "winner": winner},
    )
    game.journal_transition(
        qnum, "verdict", owner=owner, detail={"key": f"q_{qnum}_reveal"},
    )
    return owner


# ---------------------------------------------------------------------------
# (ii) — USER-CUT discipline: reair_cut_verdict re-airs at most ONCE per key
# and NEVER once the result is stamped; every refusal confirms the key so
# N+1 releases (accounting, never a silence wedge).
# ---------------------------------------------------------------------------


def test_ii_cut_verdict_never_reairs_once_result_stamped():
    game = _game()
    _journal_reveal(game, 1, answer="the femur")
    game._result_aired = {
        "qnum": 1, "text": "Correct — the femur!", "at": time.monotonic(),
        "speech_id": "speech_A",
    }
    assert game.reair_cut_verdict(["q_1_reveal"]) is False
    # Nothing re-aired...
    assert game.session.said == []
    # ...but never a wedge: the key confirms so the transition releases N+1.
    assert (
        game.say_registry.state("q_1_reveal") == lily_say_gate.CLAIM_CONFIRMED
    )
    assert game._transition_holds_next_delivery("test") is False


def test_ii_cut_verdict_reairs_at_most_once_per_key():
    game = _game()
    _journal_reveal(game, 1, answer="the femur")
    # First deliberate cut (counted at the barge classification):
    game._user_cut_counts = {"q_1_reveal": 1}
    assert game.reair_cut_verdict(["q_1_reveal"]) is True
    assert len(game.session.said) == 1
    assert "femur" in game.session.said[0].lower()
    assert game._verdict_reair_counts.get("q_1_reveal") == 1
    # The re-air itself is deliberately cut too (second user cut, claim
    # released by the cut path):
    game.say_registry.release("q_1_reveal")
    game._user_cut_counts["q_1_reveal"] = 2
    game.clear_composite_flight(None)
    game._composite_flight_state = None
    assert game.reair_cut_verdict(["q_1_reveal"]) is False
    # No third read of the beat the room keeps cutting...
    assert len(game.session.said) == 1
    # ...and still no wedge:
    assert (
        game.say_registry.state("q_1_reveal") == lily_say_gate.CLAIM_CONFIRMED
    )


def test_ii_user_cut_counter_requires_vad_positive_evidence():
    game = _game()
    # Committed-turn proxy alone (Y7's slow-STT corner) — NOT VAD-positive:
    game._user_speaking = False
    game._user_speech_ended_at = None
    game._last_user_turn_at = time.monotonic()
    assert game.cut_had_vad_evidence() is False
    # The human's own voice at the cut is:
    game._user_speaking = True
    assert game.cut_had_vad_evidence() is True
    game._user_speaking = False
    game._user_speech_ended_at = time.monotonic()
    assert game.cut_had_vad_evidence() is True


def test_ii_deliberate_barge_flushes_queued_dispatches_and_rearms_delivery():
    game = _game()
    _arm_question(game, FEMUR_QUESTION)
    qnum = game.sk.question_number
    delivery_key = f"q_{qnum}_delivery"
    # Three queued handles, none started airing: a conversational reply, the
    # queued question read (owns the delivery claim), and a stop ack.
    game._speech_handles = {
        "speech_chat": _Handle("speech_chat"),
        "speech_read": _Handle("speech_read"),
        "speech_stop_ack": _Handle("speech_stop_ack"),
    }
    game._dispatched_act_by_speech = {"speech_stop_ack": "stop_ack"}
    assert game.say_registry.claim(delivery_key, owner="speech_read")
    flushed = game.flush_queued_dispatches_on_barge(cut_speech_id=None)
    # Non-obligation handles flushed; the stop ack survives (it IS the
    # reply the barge is owed):
    assert set(flushed) == {"speech_chat", "speech_read"}
    assert "speech_stop_ack" not in flushed
    # C3d holds through the flush: the read's claim released and the
    # structural delivery expectation re-armed, so the read re-registers.
    assert game.say_registry.state(delivery_key) is None
    assert game._pending_delivery_qnum == qnum
    # Keyed game obligations are never flushed:
    game2 = _game()
    game2._speech_handles = {"speech_verdict": _Handle("speech_verdict")}
    assert game2.say_registry.claim("q_1_reveal", owner="speech_verdict")
    assert game2.flush_queued_dispatches_on_barge() == []


# ---------------------------------------------------------------------------
# (iii) — FRESHNESS: a stale conversational ack superseded by a newer
# committed user turn is suppressed at yield, with accounting.
# ---------------------------------------------------------------------------


def test_iii_stale_ack_superseded_by_newer_user_turn_is_suppressed():
    from lily_agent import Silence, SpeechTurn, run_say_pipeline

    game = _game()
    game.note_user_final()  # the triggering final ("I don't want a timer")
    assert game.gated_say(
        None, "pacing_set", "[ack]", source="voice_command",
        text="Relaxed it is — no clock from here.",
    )
    speech_id = "speech_1"  # the dispatch above minted exactly one handle
    # A NEWER final commits before the ack reaches the synthesizer:
    game.note_user_final()
    turn = SpeechTurn(
        text="Relaxed it is — no clock from here.",
        raw="Relaxed it is — no clock from here.",
        game=game, agent=_pipeline_agent(), speech_id=speech_id,
    )
    outcome = run_say_pipeline(turn)
    assert isinstance(outcome, Silence)
    assert outcome.reason == "stale_reply_superseded"
    # Accounting, never a bare drop: the suppressed handle is recorded (the
    # playout watcher routes it to the not-recorded path, and tts_node's
    # terminal-suppression branch runs the floor-owed check on it).
    assert speech_id in game._suppressed_speech_ids


def test_iii_fresh_ack_airs_and_meta_is_consumed_once():
    from lily_agent import Silence, SpeechTurn, run_say_pipeline

    game = _game()
    game.note_user_final()
    assert game.gated_say(
        None, "pacing_set", "[ack]", source="voice_command",
        text="Relaxed it is.",
    )
    turn = SpeechTurn(
        text="Relaxed it is.", raw="Relaxed it is.",
        game=game, agent=_pipeline_agent(), speech_id="speech_1",
    )
    assert not isinstance(run_say_pipeline(turn), Silence)
    # The decision is one-shot per dispatch:
    assert game.conversational_turn_superseded("speech_1") is None


def test_iii_ack_expires_on_age_alone():
    game = _game()
    assert game.gated_say(
        None, "pace_ack", "[ack]", source="voice_command", text="Slower it is.",
    )
    meta = game._conversational_dispatch_meta["speech_1"]
    meta["at"] -= 13.0  # older than the module's too-late-to-air deadline
    assert game.conversational_turn_superseded("speech_1") == "expired"


def test_iii_obligation_acks_are_exempt_from_freshness():
    game = _game()
    game.note_user_final()
    assert game.gated_say(
        None, "stop_ack", "[ack]", source="stop_primitive", text="Stopped.",
    )
    # No freshness meta is recorded for the stop ack — a newer final can
    # never gag the one acknowledgment a STOP is owed.
    assert "speech_1" not in (game._conversational_dispatch_meta or {})
    game.note_user_final()
    assert game.conversational_turn_superseded("speech_1") is None


# ---------------------------------------------------------------------------
# (iv) — ONE utterance, ONE reply: every code-ack lane marks turn ownership,
# and the on_user_turn_completed hook owns stop/hold turns directly.
# ---------------------------------------------------------------------------


def test_iv_pacing_command_yields_exactly_one_reply_lane():
    # The 17:51 shape: "no timer please" got the pacing_set code ack AND an
    # organic reply. The command handler now marks the turn, so the organic
    # lane (consume_deterministic_reply in on_user_turn_completed) is owned.
    from test_forget_flow import _make_game as _make_glass_game

    game = _make_glass_game()
    text = "no timer please"
    now = time.time()
    result = game.sk.on_transcript_segment(
        text=text, speaker_label="S1", now=now, segment_start_time=now
    )
    game.on_transcript_event(result, text, speaker_label="S1", segment_ts=now)
    # The code ack dispatched (one reply lane)...
    assert game.sk.pacing == "relaxed"
    assert any("pacing" in i for i in game.session.instructions)
    # ...and the turn is marked, so the organic lane is suppressed:
    assert game.consume_deterministic_reply(text) is True


def test_iv_stop_and_hold_acks_mark_turn_ownership():
    game = _game()
    game.handle_stop_primitive("stop stop stop")
    assert game.consume_deterministic_reply("stop stop stop") is True
    game2 = _game()
    game2.handle_hold_request("hold on a sec")
    assert game2.consume_deterministic_reply("hold on a sec") is True


def test_iv_prehook_owns_stop_and_hold_turns():
    from livekit.agents import StopResponse
    from lily_agent import LilyAgent

    def _drive(game, text):
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

    game = _game()
    game.sk.bind_speaker("S1", "Rami")
    # A stop salvo (roster-independent emphatic repetition) is owned:
    assert _drive(game, "Stop. Stop stop stop.") is True
    # A hold-equivalent is owned:
    assert _drive(game, "hold on a sec") is True
    # Ordinary speech is NOT owned (the organic lane replies as usual):
    assert _drive(game, "what was the score again") is False
    # Answer vocabulary never trips the hold detector (C13's danger shape):
    assert _drive(game, "wait, is it Saturn") is False


def test_iv_floor_line_never_fires_over_the_players_voice():
    # 17:54:19 in the fixture: the floor line aired mid-utterance because
    # floor_line_owed read host_speaking only. It now reads the VAD floor.
    game = _game()
    game._awaiting_address_since = time.time() - 10.0
    assert game.floor_line_owed() is True
    game._user_speaking = True
    assert game.floor_line_owed() is False
    game._user_speaking = False
    assert game.floor_line_owed() is True


# ---------------------------------------------------------------------------
# (v) — STOP off the finals-only path: a stop salvo that never finalizes
# halts within the provisional (interim) window, with exactly one ack.
# ---------------------------------------------------------------------------


def test_v_stop_salvo_without_finals_halts_on_the_interim():
    game = _game()
    game.armed_question = dict(FEMUR_QUESTION)
    # 17:55:40 — "Stop. Stop stop stop." holds endpointing open; no final
    # ever reaches maybe_route_stop. The interim consult brakes anyway:
    assert game.route_stop_from_interim("Stop. Stop stop stop") is True
    assert game._hold_active is True
    assert game.game_delivery_stopped() is True
    # Exactly one acknowledgment aired:
    stop_acks = [s for s in game.session.said if "stopped" in s.lower()]
    assert len(stop_acks) == 1
    # The still-growing interim of the same utterance is debounced:
    assert game.route_stop_from_interim("Stop. Stop stop stop, Lily") is False
    # And even past the debounce, the brake is idempotent — no second ack:
    game._interim_stop_routed_at = 0.0
    game.route_stop_from_interim("stop stop stop")
    stop_acks = [s for s in game.session.said if "stopped" in s.lower()]
    assert len(stop_acks) == 1


def test_v_ordinary_interim_never_trips_the_brake():
    game = _game()
    assert game.route_stop_from_interim("I think it's the femur") is False
    assert game.route_stop_from_interim("don't stop the game") is False
    assert game._hold_active is False
    assert game.game_delivery_stopped() is False


# ---------------------------------------------------------------------------
# (vi) — the resume watch defers while the user speaks / a hold binds, and
# consults result_aired_for before re-issuing.
# ---------------------------------------------------------------------------


def test_vi_resume_not_owed_once_result_aired():
    game = _game()
    _arm_question(game, FEMUR_QUESTION)
    qnum = game.sk.question_number
    game._delivery_barge_cut_qnum = qnum
    assert game._question_barge_resume_still_owed(qnum) is True
    game._result_aired = {
        "qnum": qnum, "text": "Correct — the femur!",
        "at": time.monotonic(), "speech_id": "speech_A",
    }
    assert game._question_barge_resume_still_owed(qnum) is False


def test_vi_resume_watch_defers_while_user_speaking_then_fires(monkeypatch):
    monkeypatch.setenv("LILY_CUT_RECOVERY_GRACE", "0.01")
    game = _game()
    _arm_question(game, FEMUR_QUESTION)
    qnum = game.sk.question_number
    game._delivery_barge_cut_qnum = qnum
    game._user_speaking = True

    async def scenario():
        watch = asyncio.ensure_future(game._question_barge_resume_watch(qnum))
        # Several grace periods pass while the player is mid-utterance —
        # the re-offer machine-gun from the 17:51 call must NOT fire:
        await asyncio.sleep(0.08)
        assert game.session.instructions == []
        assert game.session.said == []
        # The player finishes; the read is still owed, so the resume fires:
        game._user_speaking = False
        await asyncio.wait_for(watch, timeout=2.0)

    _run(scenario(), game)
    assert len(game.session.instructions) == 1
    assert "option" in game.session.instructions[0].lower() or (
        "question" in game.session.instructions[0].lower()
    )


def test_vi_resume_watch_defers_while_hold_active(monkeypatch):
    monkeypatch.setenv("LILY_CUT_RECOVERY_GRACE", "0.01")
    game = _game()
    _arm_question(game, FEMUR_QUESTION)
    qnum = game.sk.question_number
    game._delivery_barge_cut_qnum = qnum
    game._hold_active = True

    async def scenario():
        watch = asyncio.ensure_future(game._question_barge_resume_watch(qnum))
        await asyncio.sleep(0.08)
        assert game.session.instructions == []
        watch.cancel()
        await asyncio.sleep(0)

    _run(scenario(), game)
    assert game.session.instructions == []


def test_vi_stale_claim_watch_confirms_preaired_verdict_instead_of_reissuing():
    game = _game()
    _journal_reveal(game, 1, answer="the femur")
    game._result_aired = {
        "qnum": 1, "text": "Correct — the femur!",
        "at": time.monotonic(), "speech_id": "speech_A",
    }
    assert game.say_registry.claim("q_1_reveal", owner="speech_B")
    # Age the claim past the watchdog deadline:
    game.say_registry._claimed_at["q_1_reveal"] -= 13.0

    async def scenario():
        # One watchdog pass, with the sleep shrunk to keep the test fast:
        import lily_speech_delivery as lsd
        real_sleep = asyncio.sleep

        async def fast_sleep(_):
            await real_sleep(0)

        lsd_asyncio_sleep = lsd.asyncio.sleep
        lsd.asyncio.sleep = fast_sleep
        try:
            await game._stale_claim_watch(
                "q_1_reveal", "speech_B", "verdict", "[instr]",
                "adjudicate_verdict",
            )
        finally:
            lsd.asyncio.sleep = lsd_asyncio_sleep

    _run(scenario(), game)
    # Never re-issued — confirmed instead (no wedge, no silence):
    assert game.session.said == []
    assert game.session.instructions == []
    assert (
        game.say_registry.state("q_1_reveal") == lily_say_gate.CLAIM_CONFIRMED
    )


# ---------------------------------------------------------------------------
# S13 — the 17:51 evidence transcript is committed with its hash pinned.
# ---------------------------------------------------------------------------

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / (
    "live_20260814_1751_hostloop.txt"
)
_FIXTURE_SHA256 = (
    "0dacdacb44f9b01f390160fa825a3975885219dca5206947a902e725b164b207"
)


def test_s13_fixture_committed_and_hash_pinned():
    data = _FIXTURE.read_bytes()
    assert hashlib.sha256(data).hexdigest() == _FIXTURE_SHA256
    text = data.decode("utf-8")
    # The four defect classes this WO closes are all pinned in the record:
    assert "RECONSTRUCTION" in text  # provenance stated honestly (S2)
    assert "third statement" in text          # [A] triple verdict
    assert "TWO replies to one utterance" in text  # [B] double reply
    assert "_user_speaking" in text           # [C] floor over the answer
    assert "Stop stop stop" in text           # [D] the stop salvo
