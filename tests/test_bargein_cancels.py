"""Y7 (WO-LILY-HOTFIX-007): a deliberate barge-in is a CANCEL, not a cut
to be recovered.

Live defect class: the player talks over Lily, she stops — and then the
recovery machinery brings the killed line back. Either the cut-recovery
watchdog auto-resumes it 3.5s later, or the re-air gate hands the same
content to the next dispatch as "say it again in fresh words". Chain A of
docs/GUARD_MAP.md loops from there.

ARCHAEOLOGY (why the mechanisms that already existed did not cover this):

  * `_cut_recovery_should_arm` (WS-3) refuses keyed game acts, open
    windows, adjudication, STOP and game-over — but arms on ANY
    `interrupted`, because `handle.interrupted` is CAUSE-BLIND. A human
    barge-in, a `cancel_speech(force=True)`, an MC abort and a
    paused-then-committed turn all set the same flag.
  * `_cut_recovery_should_fire`'s user-turn recency guard IS a barge-in
    proxy — the only one that existed — but it keys on
    `_last_user_turn_at`, stamped by `note_user_turn()` from
    `on_user_turn_completed`, i.e. only when a transcript COMMITS. Two
    framework facts (livekit-agents 1.6.8) make that insufficient:
      1. `_interrupt_by_audio_activity` pauses the speech and calls
         `_on_end_of_agent_speech(ignore_user_transcript_until=now)`;
         `AudioRecognition._flush_held_transcripts` then DROPS transcripts
         whose end time falls inside that ignore window. The very
         utterance that caused the barge can therefore never commit as a
         user turn — routine in a game whose steady state is shouting
         over the host (tests/test_bargein_is_normal.py).
      2. `_on_end_of_turn` awaits `current_speech.interrupt()` BEFORE it
         awaits `on_user_turn_completed`, so even when a turn does
         commit, `on_agent_speech_finished` can run first — the stamp is
         not available at ARM time at all.

  So: the VAD layer, which necessarily runs before the framework's
  interruption decision (`on_vad_inference_done` -> min_duration budget
  -> `_interrupt_by_audio_activity`), is the only place the CAUSE is
  knowable when the arms fire. Lily already subscribes to it
  (`user_state_changed` -> `_user_speaking`, the P0-2 kickoff floor); Y7
  reads that same signal as the cut cause instead of adding a detector.

Framework-free: LilyGame decision methods on the test_cut_recovery seam.
"""

import inspect
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_config
import lily_agent
from lily_agent import LilyGame
from test_cut_recovery import _make_game

LINE = "Round one, pictures — and the first one is a proper trap, so"


def _barging(game: LilyGame, *, ended_ago: float | None = 0.0) -> LilyGame:
    """Put the game in the state the VAD wiring leaves behind when a human
    talks over Lily. `ended_ago=None` = still speaking at the cut."""
    if ended_ago is None:
        game.note_user_speech_state(True)
        return game
    game.note_user_speech_state(True)
    game.note_user_speech_state(False)
    game._user_speech_ended_at = time.monotonic() - ended_ago
    return game


# -- the cut-cause discriminator ---------------------------------------------


def test_still_speaking_at_the_cut_is_a_barge_in():
    game = _barging(_make_game(), ended_ago=None)
    assert game.cut_was_deliberate_barge_in() is True


def test_speech_that_just_ended_is_still_the_barge_that_caused_the_cut():
    """The framework commits a paused barge on the final transcript, which
    lands after VAD end-of-speech; the cut therefore arrives with the user
    already back in `listening`."""
    game = _barging(_make_game(), ended_ago=0.4)
    assert game.cut_was_deliberate_barge_in() is True


def test_speech_that_ended_long_ago_is_not_the_cause():
    game = _barging(_make_game(), ended_ago=30.0)
    assert game.cut_was_deliberate_barge_in() is False


def test_a_silent_room_is_never_a_barge_in():
    assert _make_game().cut_was_deliberate_barge_in() is False


def test_a_committed_user_turn_at_the_cut_is_also_a_barge_in():
    """The pre-existing fire-time proxy, promoted to the same predicate so
    both halves of "did a human take the floor" have one home."""
    game = _make_game()
    game.note_user_turn()
    assert game.cut_was_deliberate_barge_in() is True


# -- THE fixture: cancel, not recover ----------------------------------------


def test_vad_barge_in_blocks_the_arm_with_no_committed_turn():
    """THE reproduction. The barge utterance was dropped inside the
    framework's ignore-user-transcript window, so `note_user_turn` never
    fired: before Y7 this armed the auto-resume and Lily re-aired the line
    the player had just killed."""
    game = _barging(_make_game(), ended_ago=0.3)
    assert not game._cut_recovery_should_arm(
        [], interrupted=True, failed=False
    )


def test_barge_in_arms_nothing_end_to_end(caplog):
    """The whole cut path for a deliberate barge-in: no cut-recovery token,
    no re-air arm, and the cause is stated in the log."""
    game = _barging(_make_game(), ended_ago=0.2)
    with caplog.at_level("INFO", logger="lily.agent"):
        game.on_agent_speech_finished(LINE, speech_id="s1", interrupted=True)
    assert getattr(game, "_cut_recovery_token", 0) == 0
    assert game.peek_reair_gate() is False
    assert any("BARGE_IN_CANCEL" in r.getMessage() for r in caplog.records)


def test_barge_in_turn_does_not_regenerate():
    """"Must NOT regenerate": with no re-air arm, the WS-3 regen gate reads
    the next turn as ordinary speech even if it repeats — the killed line is
    not laundered back into the room as "the same thing in fresh words"."""
    game = _barging(_make_game(), ended_ago=0.2)
    game.on_agent_speech_finished(LINE, speech_id="s1", interrupted=True)
    assert game.reair_verbatim_should_regenerate(LINE, "verbatim") is False


def test_barge_in_does_not_dispatch_an_auto_resume():
    game = _barging(_make_game(), ended_ago=0.2)
    game.on_agent_speech_finished(LINE, speech_id="s1", interrupted=True)
    assert game.session.instructions == []


def test_a_barged_turn_leaves_no_stale_reair_arm_on_the_next_act():
    """The re-air arm is consumed ONLY by gated_say (take_reair_dispatch,
    the sole call site), so a barged CONVERSATIONAL turn used to leave it
    armed for whatever code-dispatched act came next — handing the next
    question delivery "you were cut short mid-question, pick up from where
    you broke off rather than starting the whole thing again" for a
    question the table has never heard. Not after a barge-in."""
    game = _barging(_make_game(), ended_ago=0.2)
    game.on_agent_speech_finished(LINE, speech_id="s1", interrupted=True)
    assert game.gated_say(
        None, "question_delivery", "Read the armed question.", "y7_next"
    )
    assert "cut short" not in game.session.instructions[-1]


def test_a_network_cut_still_hands_the_next_act_its_regen_directive():
    """PROTECTED (WS-3 mech. 20): for the cause recovery owns, the
    re-dispatch still carries the pick-up-where-you-broke-off directive."""
    game = _make_game()
    game.on_agent_speech_finished(LINE, speech_id="s1", interrupted=True)
    assert game.gated_say(
        None, "question_delivery", "Read the armed question.", "y7_next"
    )
    assert "cut short" in game.session.instructions[-1]


# -- PROTECTED: recovery still owns the causes it was built for --------------


def test_network_cut_still_arms_and_still_resumes():
    """PROTECTED (WS-3's own live shape, lily-0BD414): a stream that died
    mid-air with nobody talking is exactly what the auto-resume exists
    for."""
    game = _make_game()
    game.on_agent_speech_finished(
        "How does that sound to you? If", speech_id="s1", interrupted=True
    )
    assert getattr(game, "_cut_recovery_token", 0) >= 1
    assert game.peek_reair_gate() is True
    assert game._cut_recovery_should_fire(game._cut_recovery_token)


def test_mid_stream_failure_arms_even_with_the_room_talking():
    """PROTECTED. A TTS/network death is a different CAUSE: the player's
    voice near the cut does not make a dead stream her fault, and nothing
    she said is answered by silence."""
    game = _barging(_make_game(), ended_ago=0.1)
    assert game._cut_recovery_should_arm([], interrupted=False, failed=True)


def test_keyed_game_act_still_recovers_through_the_game_loop():
    """PROTECTED: the cause gate did not displace the released-claim gate."""
    game = _barging(_make_game(), ended_ago=0.1)
    assert not game._cut_recovery_should_arm(
        ["q_3_delivery"], interrupted=True, failed=False
    )


# -- Y5 contract: the record stays honest ------------------------------------


def test_the_cut_off_marker_survives_a_barge_in():
    """Y5 (tests/test_transcript_truth.py): a cut turn partially aired and
    belongs in the record, marked. Cancelling the RECOVERY must not cancel
    the honest transcript row."""
    game = _barging(_make_game(), ended_ago=0.2)
    game.on_agent_speech_finished(LINE, speech_id="s1", interrupted=True)
    assert game.sk.agent_turns == [LINE]
    assert game.sk.transcript_buffer[-1]["text"].endswith("…[cut off]")


# -- the hold: the codebase's own record of a player taking the floor -------


def test_a_hold_stands_the_auto_resume_down():
    """Chain F (GUARD_MAP §8): `trigger_cut_recovery` bypasses gated_say,
    so mechanisms 5-11 never run on the auto-resume — including the hold
    that every other lane obeys. A player saying "hang on a sec" during the
    grace window is a deliberate yield; resuming into it is the same defect
    class as re-airing over a barge. Gated on the EXISTING hold predicate,
    at the existing fire-time check."""
    game = _make_game()
    game.arm_cut_recovery("...so the next one is")
    assert game._cut_recovery_should_fire(game._cut_recovery_token)
    game.enter_hold(reason="player_wait")
    assert not game._cut_recovery_should_fire(game._cut_recovery_token)


# -- structural pins ---------------------------------------------------------


def test_the_vad_budget_is_the_frameworks_interruption_floor():
    """Y7's "~200ms budget" is the framework's own VAD gate:
    `on_vad_inference_done` interrupts once `speech_duration >=
    interruption.min_duration`, and min_words=1 keeps a one-word quiz
    shout eligible. Pinned so the budget cannot drift into the seconds
    (the 0.8s setting that made barge-in look dead)."""
    assert 0.1 <= lily_config.interruption_min_duration() <= 0.25


def test_the_cause_rides_the_existing_vad_wiring():
    """NO NEW LAYERS (mandate rule 2): the cut cause is read off the
    `user_state_changed` subscription that already existed for the P0-2
    kickoff floor — one handler, no second VAD subscriber."""
    src = inspect.getsource(lily_agent.entrypoint)
    assert src.count('@session.on("user_state_changed")') == 1
    handler = src[src.index('@session.on("user_state_changed")'):]
    handler = handler[: handler.index('@session.on("agent_state_changed")')]
    assert "note_user_speech_state" in handler


def test_recovery_gained_no_new_dispatch_path():
    """Chain F pin, POST-INTEGRATION FORM. Y7's original pin held
    `trigger_cut_recovery` to its historical `instructed_reply` route
    ("no new bypass"); Y10 then closed chain F by routing the resume
    through gated_say — the shared funnel — which SUPERSEDES that route
    (the Y7 commit anticipated exactly this). The invariant that
    survives both: the resume has ONE dispatch path, it is the same
    funnel every other code-driven turn uses, and no raw bypass
    remains."""
    src = inspect.getsource(LilyGame.trigger_cut_recovery)
    assert src.count("self.gated_say(") == 1
    # Prose in the docstring may NAME the old route; no CALL to it remains.
    assert "self.instructed_reply(" not in src
    assert "self.session.generate_reply(" not in src


def test_the_arms_are_gated_on_one_cause_call():
    """Source pin on the gate itself: the cut path decides the cause ONCE
    and the re-air arm reads that decision, so the two arms can never
    drift apart into "recovery off, re-air on"."""
    src = inspect.getsource(LilyGame.on_agent_speech_finished)
    decide = src.index("cut_was_deliberate_barge_in")
    reair = src.index("self.arm_reair_gate()")
    recovery = src.index("_cut_recovery_should_arm")
    assert decide < reair < recovery


def test_user_turn_cancel_clears_the_arm_of_a_recent_cut():
    """Y7 review F2, the slow-STT corner, closed at integration: a barge
    whose transcript commits after the 2s VAD window misclassifies at cut
    time (arm set, recovery armed); the committing turn then cancels the
    watchdog — and previously left the arm live for an unrelated code
    dispatch minutes later to inherit the stale "cut short" directive.
    The cancel now clears an arm that belongs to the cut being cancelled."""
    game = _make_game("y7-f2-corner")
    game.arm_reair_gate()
    game._cut_recovery_armed_at = time.monotonic() - 1.0  # recent cut
    game.note_user_turn()
    assert game.peek_reair_gate() is False


def test_user_turn_cancel_leaves_an_unrelated_arm_alone():
    """The scope guard: an arm with no recent cut behind it (or from a cut
    long outside grace+lookback) is not this cancel's to clear."""
    game = _make_game("y7-f2-scope")
    game.arm_reair_gate()
    game._cut_recovery_armed_at = time.monotonic() - 300.0  # ancient
    game.note_user_turn()
    assert game.peek_reair_gate() is True


def test_user_turn_cancel_never_releases_the_address_debt():
    """The arm clears but the address latch does NOT: the user turn's own
    organic reply pays the debt at playout — releasing it here would let
    code dispatches jump in front of the answer (the latch's whole job)."""
    game = _make_game("y7-f2-debt")
    game.arm_reair_gate()
    game._cut_recovery_armed_at = time.monotonic() - 1.0
    game._awaiting_address_since = time.monotonic() - 2.0
    game.note_user_turn()
    assert game.peek_reair_gate() is False
    assert game._awaiting_address_since > 0.0
