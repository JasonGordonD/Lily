"""WO-LILY-HOTFIX-001 / WO-LILY-NC-BENCH-001 — deaf-mute wedge regressions.

Fixture: the 2026-08-06 04:21–04:30 UTC P0. Four consecutive sessions
(lily-813B86, lily-F70BF5, lily-90DAE0, lily-A7DAD8) opened deaf and
mute: Krisp NC on the join path wedged RoomIO audio setup, the greet's
dispatched speech never reached playout AND never failed — no confirm,
no release, its session_greet claim frozen CLAIM_PENDING — and the
entrypoint's belt-and-braces greet retry was then dup-suppressed against
that frozen claim. Permanent silence: M1's exact failure mode, enforced
by the say gate itself.

The contract under test (WO deliverable #2): dup suppression applies
ONLY to re-air of an utterance that actually played within the same
session. A say that never reached playout must never have its retry
dup-suppressed — a stale pending claim is superseded on retry, and every
keyed dispatch arms a watchdog that releases-and-retries a claim whose
speech never started airing (bounded, so a down audio path doesn't queue
infinite silence).

Registry scoping fact recorded here as a permanent regression
(deliverable #1's discriminating answer): the SpeechActRegistry is
per-session in-memory state with no persisted consumed-keys ledger, so a
cross-session leak is structurally impossible — two consecutive sessions
in one worker both greet.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_audeering_consumers
import lily_say_gate
import lily_agent
from lily_agent import LilyGame
from lily_scorekeeper import LilyScorekeeper


class _Handle:
    def __init__(self, speech_id):
        self.id = speech_id


def _make_game(session_id="wedge-fixture") -> LilyGame:
    game = LilyGame.__new__(LilyGame)
    game.sk = LilyScorekeeper(session_id)
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game._playout_started_ids = set()
    game._stale_retry_counts = {}
    game.game_started = False
    game.game_over = False
    game.armed_question = None
    game.reconnected = False
    game._pending_delivery_qnum = None
    game.acoustic = lily_audeering_consumers.LilyAcousticState()
    game.instructed_replies = []
    game._handle_seq = [0]

    def _instructed_reply(text):
        game.instructed_replies.append(text)
        game._handle_seq[0] += 1
        return _Handle(f"speech_{game._handle_seq[0]}")

    game.instructed_reply = _instructed_reply
    return game


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# -- the race protection the gate exists for stays intact ----------------------


def test_young_pending_claim_still_suppresses_the_double_greet():
    """The original double-greeting race (on_enter + entrypoint dispatch
    within the same seconds): the second dispatch is suppressed while the
    first is genuinely in flight."""
    game = _make_game()
    assert game.gated_say("session_greet", "greet", "greet them", "on_enter")
    assert not game.gated_say(
        "session_greet", "greet", "greet them", "entrypoint"
    )
    assert len(game.instructed_replies) == 1


def test_confirmed_act_is_final_forever():
    """An act that actually PLAYED never redelivers — even long after."""
    game = _make_game()
    assert game.gated_say("session_greet", "greet", "greet them", "on_enter")
    game.say_registry.confirm("session_greet")
    # Age the (now confirmed) act far past any deadline: still suppressed.
    game.say_registry._claimed_at["session_greet"] = -1e9
    assert not game.gated_say(
        "session_greet", "greet", "greet them", "entrypoint"
    )
    assert len(game.instructed_replies) == 1


# -- the wedge fix: stale never-played claims yield to the retry ---------------


def test_stale_never_played_claim_is_superseded_on_retry(monkeypatch):
    """THE 08-06 defect: greet claimed, playout never started, retry
    arrives — the retry must speak, not suppress."""
    game = _make_game()
    assert game.gated_say("session_greet", "greet", "greet them", "on_enter")
    # Age the pending claim past the deadline; playout never started.
    monkeypatch.setattr(lily_agent, "_STALE_CLAIM_SECONDS", 0.0)
    assert game.gated_say(
        "session_greet", "greet", "greet them", "entrypoint"
    ) is True
    assert len(game.instructed_replies) == 2
    # The retry now owns a live pending claim (confirms at ITS playout).
    assert (
        game.say_registry.state("session_greet") == lily_say_gate.CLAIM_PENDING
    )


def test_stale_claim_holds_while_its_speech_is_airing(monkeypatch):
    """A long monologue mid-playout is late, not wedged: its claim stays
    even past the deadline once playout started."""
    game = _make_game()
    assert game.gated_say("finale", "finale", "big finish", "code")
    owner = game.say_registry.owner_of("finale")
    game.note_playout_started(owner)
    monkeypatch.setattr(lily_agent, "_STALE_CLAIM_SECONDS", 0.0)
    assert not game.gated_say("finale", "finale", "big finish", "retry")
    assert len(game.instructed_replies) == 1


def test_stale_claim_holds_while_other_audio_is_airing(monkeypatch):
    """host_speaking means the audio path is alive — a queued act behind
    other playout is not the deaf case; no supersede."""
    game = _make_game()
    assert game.gated_say("session_greet", "greet", "greet them", "on_enter")
    game.sk.host_speaking = True
    monkeypatch.setattr(lily_agent, "_STALE_CLAIM_SECONDS", 0.0)
    assert not game.gated_say(
        "session_greet", "greet", "greet them", "entrypoint"
    )
    assert len(game.instructed_replies) == 1


# -- the watchdog: recovery without waiting for a second trigger path ----------


def test_watchdog_releases_and_retries_a_wedged_dispatch(monkeypatch):
    """No retry path ever fires (the 08-06 sessions had exactly one
    suppressed retry, then nothing): the watchdog itself frees the frozen
    claim and re-dispatches, bounded to _STALE_CLAIM_MAX_RETRIES before
    declaring the audio path down."""
    monkeypatch.setattr(lily_agent, "_STALE_CLAIM_SECONDS", 0.01)

    async def scenario():
        game = _make_game()
        assert game.gated_say(
            "session_greet", "greet", "greet them", "on_enter"
        )
        # Nothing ever starts airing. Watchdogs fire, release, retry.
        await asyncio.sleep(0.2)
        return game

    game = _run(scenario())
    # Original + bounded retries, then exhaustion (no infinite queue).
    assert len(game.instructed_replies) == 1 + lily_agent._STALE_CLAIM_MAX_RETRIES
    # Exhaustion leaves the key FREE — if audio recovers, a later trigger
    # may legitimately claim and speak; the claim is never poisoned.
    assert game.say_registry.state("session_greet") is None


def test_watchdog_is_a_noop_once_the_act_confirms(monkeypatch):
    monkeypatch.setattr(lily_agent, "_STALE_CLAIM_SECONDS", 0.01)

    async def scenario():
        game = _make_game()
        assert game.gated_say(
            "session_greet", "greet", "greet them", "on_enter"
        )
        game.say_registry.confirm("session_greet")
        await asyncio.sleep(0.05)
        return game

    game = _run(scenario())
    assert len(game.instructed_replies) == 1
    assert (
        game.say_registry.state("session_greet")
        == lily_say_gate.CLAIM_CONFIRMED
    )


def test_watchdog_leaves_airing_speech_alone(monkeypatch):
    monkeypatch.setattr(lily_agent, "_STALE_CLAIM_SECONDS", 0.01)

    async def scenario():
        game = _make_game()
        assert game.gated_say(
            "session_greet", "greet", "greet them", "on_enter"
        )
        game.note_playout_started(game.say_registry.owner_of("session_greet"))
        await asyncio.sleep(0.05)
        return game

    game = _run(scenario())
    assert len(game.instructed_replies) == 1
    assert (
        game.say_registry.state("session_greet") == lily_say_gate.CLAIM_PENDING
    )


# -- deliverable #5 regressions, permanent -------------------------------------


def test_two_consecutive_sessions_in_one_worker_both_greet():
    """Discriminating-check answer, frozen as a regression: the registry
    is per-session state — no cross-session consumed-key leak is possible,
    so back-to-back sessions (same worker, same group) both greet."""
    first = _make_game("lily-night-1")
    second = _make_game("lily-night-2")
    assert first.gated_say("session_greet", "greet", "hello", "on_enter")
    assert second.gated_say("session_greet", "greet", "hello", "on_enter")
    assert len(first.instructed_replies) == 1
    assert len(second.instructed_replies) == 1


def test_greet_barge_regenerates_not_replays_not_suppresses():
    """Mid-greet barge: the interrupted greet releases its claim, the
    retry is permitted, and it carries the regeneration directive (fresh
    words) rather than replaying — and is never dup-suppressed."""
    game = _make_game()
    # Fixture is minimal; the interrupted path only needs these members.
    game.arm_reair_gate = LilyGame.arm_reair_gate.__get__(game)
    assert game.gated_say(
        "session_greet", "greet", "greet them warmly", "on_enter"
    )
    speech_id = "speech_1"  # _Handle id assigned to the first dispatch
    assert game.say_registry.owner_of("session_greet") == speech_id
    # Barge-in: playout cut. The claim releases on the interrupted path
    # (on_agent_speech_finished's release_owner) — modeled directly here.
    released = game.say_registry.release_owner(speech_id)
    assert released == ["session_greet"]
    game.arm_reair_gate()
    # The retry speaks — never suppressed — and regenerates fresh.
    assert game.gated_say(
        "session_greet", "greet", "greet them warmly", "reair"
    )
    assert len(game.instructed_replies) == 2
    assert "fresh" in game.instructed_replies[1]
    assert game.instructed_replies[1] != game.instructed_replies[0]


# -- join path: 1.6.6-native room options, NC branch (NC-BENCH Task 2) ---------


def test_join_path_uses_native_room_options_not_the_deprecated_shim():
    """The 08-06 mute sessions logged the RoomInputOptions deprecation
    warning on the join path; the shim is now gone. The entrypoint builds
    1.6.6-native RoomOptions/AudioInputOptions."""
    src = Path(lily_agent.__file__).read_text(encoding="utf-8")
    assert "room_input_options=RoomInputOptions(" not in src
    assert "room_options=RoomOptions(" in src
    assert "audio_input=AudioInputOptions(" in src


def test_join_path_audio_setup_nc_off_branch(monkeypatch):
    """Exercise the room-audio setup exactly as session.start receives it,
    default (off) branch: the resolved AudioInputOptions carry NO NC
    processor, and the options object is accepted by the installed 1.6.6
    RoomOptions machinery (not the legacy conversion path)."""
    from livekit.agents.voice.room_io import AudioInputOptions, RoomOptions

    monkeypatch.delenv("LILY_NOISE_CANCELLATION", raising=False)
    opts = RoomOptions(
        audio_input=AudioInputOptions(
            noise_cancellation=lily_agent.lily_noise_cancellation_options(),
        ),
    )
    resolved = RoomOptions._ensure_options(opts)
    assert resolved is opts  # native object passes through, no shim
    assert resolved.audio_input.noise_cancellation is None
    # Audio format invariants the wedge made load-bearing: 24kHz mono in.
    assert resolved.audio_input.sample_rate == 24000
    assert resolved.audio_input.num_channels == 1


def test_join_path_audio_setup_nc_on_branch(monkeypatch):
    """Opt-in (bench-gated) branch: the SAME native path carries the
    Krisp ambient model — never BVC."""
    import os

    from livekit.agents.voice.room_io import AudioInputOptions, RoomOptions

    monkeypatch.setenv("LILY_NOISE_CANCELLATION", "nc")
    opts = RoomOptions(
        audio_input=AudioInputOptions(
            noise_cancellation=lily_agent.lily_noise_cancellation_options(),
        ),
    )
    resolved = RoomOptions._ensure_options(opts)
    nc = resolved.audio_input.noise_cancellation
    assert nc is not None
    model = os.path.basename(nc.options["modelPath"]).lower()
    assert "bvc" not in model
