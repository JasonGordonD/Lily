"""WS-3 (WO-LILY-STREAM-INTEGRITY-002): cut-recovery contract.

Live defect (session lily-0BD414-ba80eb97, 08-06 ~18:17-18:22): organic
turns cut mid-sentence by a barge-in ("...how does that sound to you? If"
[dead]) had NO auto-resume — resumption depended on a player re-prompting
("If what?"). Keyed game acts recover through the game loop; organic
conversational turns did not.

This contract auto-resumes a cut/failed ORGANIC turn that leaves DEAD AIR,
composing fresh from where meaning broke within one turn — WITHOUT an
operator poke. The one-emission mandate (omnibus delivery-gate invariant)
is preserved: a real barge-in carrying a user turn is answered by the
normal reply path, and the watchdog stands down (the user-turn recency
guard and the speaking check both suppress it) — it fires ONLY into
silence, never double-speaking.

Framework-free: exercises LilyGame decision methods on the same seam as
test_regeneration_gate.py.
"""

import os
import sys
import asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_audeering_consumers  # noqa: F401  (import-order parity)
import lily_say_gate
from lily_agent import LilyGame, _CUT_RECOVERY_DIRECTIVE
from lily_scorekeeper import LilyScorekeeper


class _FakeSession:
    def __init__(self) -> None:
        self.instructions: list = []

    def generate_reply(self, instructions: str):
        self.instructions.append(instructions)
        return object()  # a truthy SpeechHandle stand-in


class _FakeAgentHandle:
    def set_preemptive_generation(self, enabled: bool) -> None:
        pass


def _make_game(session_id: str = "ws3-cut") -> LilyGame:
    game = LilyGame.__new__(LilyGame)
    game.session = _FakeSession()
    game.agent = _FakeAgentHandle()
    game._preemptive_paused = False
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game.sk = LilyScorekeeper(session_id)
    game.transcripts = None
    game.game_started = True
    game.game_over = False
    game.armed_question = None
    game._adjudicating = False
    return game


# --------------------------------------------------------------------------
# _cut_recovery_should_arm — arm only for cut/failed ORGANIC turns
# --------------------------------------------------------------------------

def test_arm_on_interrupted_organic_turn():
    game = _make_game()
    assert game._cut_recovery_should_arm([], interrupted=True, failed=False)


def test_arm_on_failed_organic_turn():
    game = _make_game()
    # A mid-stream TTS failure (WS-2 tail-chunk death) also arms recovery.
    assert game._cut_recovery_should_arm([], interrupted=False, failed=True)


def test_no_arm_on_clean_turn():
    game = _make_game()
    assert not game._cut_recovery_should_arm([], interrupted=False, failed=False)


def test_no_arm_for_keyed_game_act():
    game = _make_game()
    # A released keyed claim (question delivery / reveal / scores) recovers
    # through the game loop; the auto-resume must NOT also fire.
    assert not game._cut_recovery_should_arm(
        ["q_3_delivery"], interrupted=True, failed=False
    )


def test_no_arm_during_answer_window():
    game = _make_game()
    game.sk.answer_window_open = True
    assert not game._cut_recovery_should_arm([], interrupted=True, failed=False)


def test_no_arm_after_game_over():
    game = _make_game()
    game.game_over = True
    assert not game._cut_recovery_should_arm([], interrupted=True, failed=False)


# --------------------------------------------------------------------------
# _cut_recovery_should_fire — fire ONLY into dead air (one-emission)
# --------------------------------------------------------------------------

def test_fire_into_dead_air():
    game = _make_game()
    game.arm_cut_recovery("How does that sound to you? If")
    assert game._cut_recovery_should_fire(game._cut_recovery_token)


def test_no_fire_when_user_turn_recent():
    # THE one-emission guard: a real barge-in carried a user turn, so the
    # normal reply path owns the recovery — the watchdog must stand down.
    game = _make_game()
    game.note_user_turn()          # player barged with content
    game.arm_cut_recovery("...but I can't")
    assert not game._cut_recovery_should_fire(game._cut_recovery_token)


def test_no_fire_when_already_speaking():
    game = _make_game()
    game.arm_cut_recovery("...so we won't")
    game.sk.host_speaking = True   # audio already resumed / new turn airing
    assert not game._cut_recovery_should_fire(game._cut_recovery_token)


def test_no_fire_when_superseded_by_newer_cut():
    game = _make_game()
    game.arm_cut_recovery("first cut")
    stale_token = game._cut_recovery_token
    game.arm_cut_recovery("second cut")  # newer cut bumps the token
    assert not game._cut_recovery_should_fire(stale_token)
    assert game._cut_recovery_should_fire(game._cut_recovery_token)


def test_cancel_stands_down_watchdog():
    game = _make_game()
    game.arm_cut_recovery("cut")
    token = game._cut_recovery_token
    game.cancel_cut_recovery()  # new audio started airing, etc.
    assert not game._cut_recovery_should_fire(token)


# --------------------------------------------------------------------------
# trigger_cut_recovery — dispatch fresh, re-air-gated
# --------------------------------------------------------------------------

def test_trigger_dispatches_fresh_resume():
    game = _make_game()
    game.arm_cut_recovery("How does that sound to you? If")
    assert game.trigger_cut_recovery() is True
    assert len(game.session.instructions) == 1
    assert _CUT_RECOVERY_DIRECTIVE in game.session.instructions[0]
    # HOTFIX-007 Y10: the resume now dispatches through gated_say (chain F
    # closed), so it CONSUMES the re-air arm itself instead of leaving it
    # for the next code dispatch — the regenerate-not-replay signal reaches
    # tts_node via _reair_turn_pending, which is the arm's actual purpose.
    assert game.peek_reair_gate() is False
    assert game._reair_turn_pending is True


# --------------------------------------------------------------------------
# on_agent_speech_finished — end-to-end wiring
# --------------------------------------------------------------------------

def test_interrupted_organic_turn_arms_recovery_end_to_end():
    game = _make_game()
    game.on_agent_speech_finished(
        "How does that sound to you? If", speech_id="s1", interrupted=True
    )
    # Recovery armed (token advanced) and the re-air gate is set.
    assert getattr(game, "_cut_recovery_token", 0) >= 1
    assert game.peek_reair_gate() is True
    # Dead air (no user turn) → the watchdog would fire.
    assert game._cut_recovery_should_fire(game._cut_recovery_token)


def test_live_barge_does_not_double_speak_end_to_end():
    # Reproduces the 08-06 live shape: the player barged WITH content
    # ("If what?") right at the cut. The user turn stamps recency; the cut
    # then arms recovery, but should_fire is False — the normal reply path
    # owns it, no double-speak.
    game = _make_game()
    game.note_user_turn()  # "If what?" landed
    game.on_agent_speech_finished(
        "How does that sound to you? If", speech_id="s1", interrupted=True
    )
    assert not game._cut_recovery_should_fire(game._cut_recovery_token)


# --------------------------------------------------------------------------
# _cut_recovery_watch — the async watchdog, grace + supersession
# --------------------------------------------------------------------------

def test_watch_fires_into_silence():
    os.environ["LILY_CUT_RECOVERY_GRACE"] = "0.02"
    try:
        game = _make_game()
        game.arm_cut_recovery("...how does that sound to you? If")
        token = game._cut_recovery_token
        asyncio.run(
            game._cut_recovery_watch(token)
        )
        assert len(game.session.instructions) == 1
        assert _CUT_RECOVERY_DIRECTIVE in game.session.instructions[0]
    finally:
        del os.environ["LILY_CUT_RECOVERY_GRACE"]


def test_watch_stands_down_if_user_turn_during_grace():
    os.environ["LILY_CUT_RECOVERY_GRACE"] = "0.05"
    try:
        game = _make_game()
        game.arm_cut_recovery("...but I can't")
        token = game._cut_recovery_token

        async def _drive():
            watch = asyncio.ensure_future(game._cut_recovery_watch(token))
            await asyncio.sleep(0.01)
            game.note_user_turn()  # player speaks during the grace window
            await watch

        asyncio.run(_drive())
        assert game.session.instructions == []  # no auto-resume dispatched
    finally:
        del os.environ["LILY_CUT_RECOVERY_GRACE"]
