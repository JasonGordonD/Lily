"""WO-LILY-VOICE-TRUTH-001 — Auditor B/D's findings, pinned as BEHAVIOR.

Every test here drives real methods on real state (LilyGame.bare +
LilyScorekeeper, fakes only for Supabase/embedder/say-gate). Failing-first
on a380531 (see the CHANGELOG entry for the run).

  V1  the ECAPA probe is speech-gated, retried, and receipted with numbers;
      enrollment reads the voiced union; under the minimum nothing enrolls.
  V2  the stated-name door ALWAYS consults the name index (staged device
      candidate or not), prefers the group with the most history, breaks
      ties on the device; memory + voiceprints file under ONE group id.
  V3  recognition carry is keyed by SPEECH ID: Auditor B's I1 (no double
      welcome-back) and I2 (no blackout); the late beat stamps on ITS
      confirm and re-arms on suppression (Auditor D P1-2); an invalidated
      preemptive generation never counts.
  V4  the name door promotes memory FIRST; the rekey/reloads run
      concurrently; the door latency is on the promotion event.
  V5  the operator's verbatim wording is byte-identical on both sides of
      every pair; the forbidden strings are gone; the memory block states
      its true provenance and the CONFIRMED/GUESSED case.
  V6  a re-keyed placeholder migrates into the named seat (no ghost); the
      solo clamp never retires a seat with a record.
  V7  every module-level name lily_identity.py references resolves.
"""

import ast
import asyncio
import builtins
import inspect
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_assessment
import lily_bank
import lily_config
import lily_identity
import lily_memory
import lily_persistence
import lily_say_gate
import lily_voice_embedder
import lily_voice_identity
import lily_agent
from lily_agent import LILY_SYSTEM_PROMPT, LilyGame
from lily_scorekeeper import LilyScorekeeper

# Tolerant lookups so the file COLLECTS on the pre-WO tree (a380531) and the
# assertions fail there — the failing-first evidence, not an ImportError.
_PAIR1_CONFIRMED_VS_GUESSED = getattr(lily_agent, "_PAIR1_CONFIRMED_VS_GUESSED", "<missing PAIR1>")
_PAIR2_DONT_NARRATE_THE_GAP = getattr(lily_agent, "_PAIR2_DONT_NARRATE_THE_GAP", "<missing PAIR2>")
_PAIR3_NAME_QUESTION_ALONE = getattr(lily_agent, "_PAIR3_NAME_QUESTION_ALONE", "<missing PAIR3>")

ROOT = Path(__file__).resolve().parent.parent
RATE = 16000
FRAME = 160
TAG = "ecapa-192-v2"


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# -- shared fixtures ----------------------------------------------------------


def _game(session_id="voice-truth", **kw) -> LilyGame:
    game = LilyGame.bare()
    game.sk = LilyScorekeeper(session_id)
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game.group_id = session_id
    game.group_id_source = "room_name"
    game.supabase = object()
    game.stt = None
    game.forget_state = None
    game.device_candidate_group_id = None
    game.device_candidate_source = None
    game.device_identity_verified = False
    game.device_identity_rejected = False
    game._device_candidate_memory = None
    game._device_candidate_memory_block = ""
    game._device_candidate_prefs = {}
    game._device_candidate_voiceprints = []
    game.memory_block = ""
    game.memory_total_games = 0
    game.memory_player_names = []
    game.memory_settled = asyncio.Event()
    game.prefs = {}
    game.armed_question = None
    game.game_started = False
    game.game_over = False
    game.pending_clarify = None
    game._prefs_offer_made = False
    game._memory_disclosure_offered = False
    game._whats_new_pending = False
    game.persist_prefs = lambda *a, **k: None
    game.publish_attributes_nowait = lambda: None
    game.dispatches = []

    def _gated_say(key, act, instr, source=None, **kwargs):
        game.dispatches.append((key, act, instr, source))
        game._dispatched_act_by_speech[f"s-{act}-{len(game.dispatches)}"] = act
        return True

    game.gated_say = _gated_say
    game.say_registry.claim("session_greet", owner="greet-1")
    for k, v in kw.items():
        setattr(game, k, v)
    return game


def _confirm(game, speech_id, text="...", **kw):
    game._resume_preemptive = lambda: None
    game._pending_reveal_event = None
    game._state_note = None
    game.on_agent_speech_finished(text, speech_id=speech_id, **kw)


def _late_beats(game):
    return [d for d in game.dispatches if d[1] == "late_recognition"]


def _enable_embedder(monkeypatch, embedding):
    monkeypatch.setattr(lily_config, "voice_identity_enabled", lambda: True)
    monkeypatch.setattr(lily_config, "voice_identity_model_tag", lambda: TAG)
    monkeypatch.setattr(lily_voice_embedder, "lily_voice_embedder_loaded", lambda: True)
    monkeypatch.setattr(lily_voice_embedder, "lily_voice_embedder_load_attempted", lambda: True)
    def _extract(samples, sample_rate=16000):
        # The real extractor materializes a PCM BUILDER inside the thread.
        if callable(samples):
            samples = samples()
        return embedding(samples) if callable(embedding) else embedding

    monkeypatch.setattr(lily_voice_embedder, "lily_extract_embedding", _extract)


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _feed(probe, clock, seconds, value):
    for _ in range(int(seconds * RATE / FRAME)):
        clock.t += FRAME / RATE
        probe.add_frame([value] * FRAME, sample_rate=RATE)


def _attach_probe(game, clock, **kw):
    kw.setdefault("min_voiced_seconds", 3.0)
    kw.setdefault("retry_voiced_seconds", 2.0)
    probe = lily_voice_embedder.LilyVoiceProbe(clock=clock, **kw)
    game.attach_voice_probe(probe)
    return probe


async def _settle(n=6):
    # The match attempt hops through asyncio.to_thread (the embedder); give
    # the thread a real slice, not just loop iterations.
    for _ in range(n):
        await asyncio.sleep(0.02)


# =============================================================================
# V1 — the probe is speech-gated, retried, receipted; enrollment is voiced
# =============================================================================


def test_v1_no_match_attempt_until_min_voiced_seconds_of_human_speech(monkeypatch):
    """8s of frames before anyone speaks (the live shape) schedule NOTHING;
    the first attempt comes only after 3s of voiced (STT-segment) audio."""
    _enable_embedder(monkeypatch, [1.0, 0.0, 0.0])
    game = _game()
    game._voice_identity_pool, game._voice_identity_pool_loaded = [], True
    clock = _Clock()
    probe = _attach_probe(game, clock)
    attempts = []

    async def fake_attempt():
        attempts.append(game._voice_identity_attempts)
        game._voice_identity_inflight = False
        return False

    game._voice_identity_match_at_start = fake_attempt

    async def scenario():
        _feed(probe, clock, 8.0, 3)                       # room tone
        game.note_voice_probe_vad(False)
        assert game.maybe_start_voice_identity_match() is False
        assert attempts == []
        t0 = clock.t
        _feed(probe, clock, 2.0, 900)
        game.note_voiced_segment(t0, clock.t, "S1")       # 2.0s voiced < 3.0
        await _settle()
        assert attempts == []
        t1 = clock.t
        _feed(probe, clock, 1.5, 900)
        game.note_voiced_segment(t1, clock.t, "S1")       # 3.5s: due
        await _settle()
        assert attempts == [1]
        assert abs(game._voice_identity_voiced_seconds - 3.5) < 0.05
        assert game._voice_identity_gate_source == "stt_segments"

    _run(scenario())


def test_v1_no_match_is_retried_on_new_voiced_audio_until_a_match(monkeypatch):
    """Rule (c): a no-match is not final while the probe lives — +2s voiced
    earns another attempt; the attempt that clears the threshold promotes.
    The receipt carries the numbers of EVERY attempt's best."""
    calls = {"n": 0}

    def embedding(_samples):
        calls["n"] += 1
        # attempt 1: 0.62 (below 0.75); attempt 2: 0.99
        return [0.62, 0.79, 0.0] if calls["n"] == 1 else [0.99, 0.02, 0.0]

    _enable_embedder(monkeypatch, embedding)
    game = _game()
    game._voice_identity_pool = [
        {"group_id": "grp_rami_real", "centroid": [1.0, 0.0, 0.0], "sample_count": 5},
    ]
    game._voice_identity_pool_loaded = True
    staged, promoted = [], []

    async def fake_stage(gid, source):
        staged.append((gid, source))
        return True

    async def fake_promote(trigger, **kw):
        promoted.append(trigger)

    game.stage_device_candidate = fake_stage
    game._promote_device_candidate = fake_promote
    clock = _Clock()
    probe = _attach_probe(game, clock)

    async def scenario():
        t0 = clock.t
        _feed(probe, clock, 3.5, 900)
        game.note_voiced_segment(t0, clock.t, "S1")
        await _settle(10)
        assert game._voice_identity_attempts == 1
        assert game._voice_id_outcome == "no_match"
        assert game._voice_identity_resolved is False          # window still open
        assert game._voice_identity_best_score is not None
        assert game._voice_identity_best_score < 0.75
        t1 = clock.t
        _feed(probe, clock, 1.0, 900)
        game.note_voiced_segment(t1, clock.t, "S1")             # +1.0 < retry
        await _settle(10)
        assert game._voice_identity_attempts == 1
        t2 = clock.t
        _feed(probe, clock, 1.5, 900)
        game.note_voiced_segment(t2, clock.t, "S1")             # +2.5 >= retry
        await _settle(10)
        assert game._voice_identity_attempts == 2
        assert game._voice_identity_matched is True
        assert staged == [("grp_rami_real", "voice_identity_match")]
        assert promoted == ["voice_identity_match"]
        receipt = game.voice_identity_receipt()
        assert receipt["outcome"].startswith("match:grp_rami_real")
        assert receipt["attempts"] == 2
        assert receipt["best_score"] > 0.9
        assert receipt["gate_source"] == "stt_segments"
        assert receipt["threshold"] == 0.75
        assert receipt["model_tag"] == TAG
        assert abs(receipt["voiced_seconds"] - 6.0) < 0.1

    _run(scenario())


def test_v1_window_expiry_resolves_no_match_with_the_numbers(monkeypatch):
    _enable_embedder(monkeypatch, [0.5, 0.86, 0.0])
    monkeypatch.setattr(lily_config, "voice_probe_window_seconds", lambda: 60.0)
    game = _game()
    game._voice_identity_pool = [
        {"group_id": "grp_other", "centroid": [1.0, 0.0, 0.0], "sample_count": 2},
    ]
    game._voice_identity_pool_loaded = True
    clock = _Clock()
    probe = _attach_probe(game, clock)

    async def scenario():
        t0 = clock.t
        _feed(probe, clock, 4.0, 900)
        game.note_voiced_segment(t0, clock.t, "S1")
        await _settle(10)
        assert game._voice_identity_attempts == 1
        assert game._voice_identity_resolved is False
        assert game.identity_probe_outstanding() is True   # N1: still open
        # The window elapses with the room quiet: the per-frame tick closes it.
        game._voice_identity_window_started_at -= 61.0
        game.note_voice_probe_vad(False)
        await _settle(10)
        assert game._voice_identity_resolved is True
        r = game.voice_identity_receipt()
        assert r["outcome"] == "no_match"
        assert r["attempts"] == 1
        assert 0.4 < r["best_score"] < 0.75
        assert r["runner_up"] is None
        assert r["threshold"] == 0.75

    _run(scenario())


def test_v1_enrollment_reads_the_voiced_union_not_the_first_8s(monkeypatch):
    seen = {}

    def embedding(samples):
        seen["samples"] = list(samples)
        return [1.0, 0.0, 0.0]

    _enable_embedder(monkeypatch, embedding)

    class _SB:
        def __init__(self):
            self.rows = []

        def table(self, name):
            sb = self

            class _Q:
                def __init__(self):
                    self._ins = None
                    self._filters = []

                def select(self, *a, **k): return self
                def eq(self, c, v): self._filters.append((c, v)); return self
                def limit(self, n): return self
                def insert(self, r): self._ins = r; return self
                def update(self, p): self._upd = p; return self

                def execute(self):
                    class _R:
                        pass
                    r = _R()
                    if self._ins is not None:
                        sb.rows.append(dict(self._ins))
                        r.data = [self._ins]
                    else:
                        r.data = [
                            row for row in sb.rows
                            if all(row.get(c) == v for c, v in self._filters)
                        ]
                    return r
            return _Q()

    sb = _SB()
    game = _game(group_id="grp_0b07f989", group_id_source="participant_metadata")
    game.supabase = sb
    game.identity_persistence_allowed = lambda: True
    clock = _Clock()
    probe = _attach_probe(game, clock, enroll_max_seconds=30.0, raw_window_seconds=60.0)
    game._voice_identity_pool, game._voice_identity_pool_loaded = [], True

    async def scenario():
        _feed(probe, clock, 8.0, 3)                 # the first 8s: room tone
        t0 = clock.t
        _feed(probe, clock, 9.0, 1000)              # the human
        game.note_voiced_segment(t0, clock.t, "S1")  # (on the loop, as live)
        await _settle()
        return await game._voice_identity_enroll_at_close()

    assert _run(scenario()) is True
    assert all(abs(s - 1000 / 32768.0) < 1e-9 for s in seen["samples"])
    assert abs(len(seen["samples"]) / RATE - 9.0) < 0.05
    assert sb.rows and sb.rows[0]["model_tag"] == TAG
    enrollment = game.voice_identity_receipt()["enrollment"]
    assert enrollment["status"] == "enrolled"
    assert enrollment["group_id"] == "grp_0b07f989"
    assert enrollment["gate_source"] == "stt_segments"
    assert abs(enrollment["voiced_seconds"] - 9.0) < 0.05


def test_v1_under_the_minimum_nothing_enrolls_and_the_receipt_says_so(monkeypatch):
    _enable_embedder(monkeypatch, [1.0, 0.0, 0.0])
    game = _game(group_id="grp_0b07f989", group_id_source="participant_metadata")
    game.identity_persistence_allowed = lambda: True
    writes = []

    async def _upsert(*a, **k):
        writes.append(k)
        return True

    monkeypatch.setattr(lily_persistence, "lily_upsert_voice_identity", _upsert)
    clock = _Clock()
    probe = _attach_probe(game, clock)

    async def scenario():
        _feed(probe, clock, 20.0, 3)
        t0 = clock.t
        _feed(probe, clock, 1.5, 900)
        game.note_voiced_segment(t0, clock.t, "S1")   # 1.5s < 3.0 minimum
        game._voice_identity_finalize()
        return await game._voice_identity_enroll_at_close()

    assert _run(scenario()) is False
    assert writes == []
    r = game.voice_identity_receipt()
    assert r["outcome"] == "insufficient_voiced"
    assert abs(r["voiced_seconds"] - 1.5) < 0.05
    assert r["attempts"] == 0
    assert r["enrollment"]["status"] == "skipped_insufficient_voiced"


def test_v1_best_score_persists_on_a_single_final_no_match(monkeypatch):
    """Injected-probe path (no live sink): the one attempt is final and the
    receipt still carries best/runner-up."""
    _enable_embedder(monkeypatch, [0.7, 0.71, 0.0])
    game = _game(group_id="voiceA", group_id_source="participant_metadata")
    game._voice_identity_pcm = [0.1, 0.2, 0.3]
    game._voice_identity_voiced_seconds = 5.0
    game._voice_identity_pool = [
        {"group_id": "g1", "centroid": [1.0, 0.0, 0.0], "sample_count": 1},
        {"group_id": "g2", "centroid": [0.0, 1.0, 0.0], "sample_count": 1},
    ]
    game._voice_identity_pool_loaded = True
    assert _run(game._voice_identity_match_at_start()) is False
    r = game.voice_identity_receipt()
    assert r["outcome"] == "no_match"
    assert r["best_score"] is not None and r["runner_up"] is not None
    assert r["best_group"] == "g2"
    assert game._voice_identity_resolved is True


def test_v1_lily_segments_never_count_as_voiced():
    game = _game()
    clock = _Clock()
    probe = _attach_probe(game, clock)
    _feed(probe, clock, 5.0, 900)

    async def scenario():
        return game.note_voiced_segment(clock.t - 5.0, clock.t, "LILY")

    assert _run(scenario()) == 0.0
    assert probe.voiced_seconds == 0.0


# =============================================================================
# V2 — the name door + one group id for memory and voiceprints
# =============================================================================


def _name_door_game(monkeypatch, *, groups, history, staged=None, staged_names=()):
    game = _game("lily-2C489B")
    game.upgrades = []
    game.staged = []

    async def _lookup(sb, name):
        return list(groups)

    async def _history(sb, gids):
        return dict(history)

    monkeypatch.setattr(lily_persistence, "lily_groups_for_player_name", _lookup)
    monkeypatch.setattr(lily_persistence, "lily_group_history", _history)

    async def _stage(candidate, src):
        game.staged.append((candidate, src))
        game.device_candidate_group_id = candidate
        game._device_candidate_memory = {"total_games": 4, "player_names": ["Rami"]}
        game._device_candidate_memory_block = "[RETURNING TABLE] 4 game(s)"
        game._device_candidate_prefs = {}
        game._device_candidate_voiceprints = []
        return True

    async def _upgrade(new_id, src):
        game.upgrades.append((new_id, src))
        game.group_id = new_id
        game.group_id_source = src

    game.stage_device_candidate = _stage
    game.upgrade_group_id = _upgrade
    if staged:
        game.device_candidate_group_id = staged
        game._device_candidate_memory = {
            "total_games": 1, "player_names": list(staged_names),
        }
        game._device_candidate_memory_block = "[RETURNING TABLE] 1 game(s)"
    return game


def test_v2_name_index_consulted_with_a_staged_device_candidate_most_history_wins(monkeypatch):
    """Auditor B's live shape: the device fragment (1 thin session, name on
    file) is staged; the index knows the same name on the 23-session group.
    Pre-fix the door short-circuited to device_plus_name on the fragment
    without ever querying the index."""
    game = _name_door_game(
        monkeypatch,
        groups=["grp_device_frag", "grp_rami_23"],
        history={
            "grp_device_frag": {"sessions": 1, "questions": 2},
            "grp_rami_23": {"sessions": 23, "questions": 210},
        },
        staged="grp_device_frag", staged_names=["Rami"],
    )
    assert _run(game.maybe_recognize_by_stated_name("Rami")) is True
    assert game.upgrades == [("grp_rami_23", "name_stated")]
    assert game.staged == [("grp_rami_23", "name_stated")]
    assert game.device_identity_verified is False
    assert game.identity_confirmed_source == "name_stated"


def test_v2_device_candidate_breaks_a_history_tie(monkeypatch):
    game = _name_door_game(
        monkeypatch,
        groups=["grp_recent_other", "grp_device"],
        history={
            "grp_recent_other": {"sessions": 5, "questions": 40},
            "grp_device": {"sessions": 5, "questions": 40},
        },
        staged="grp_device", staged_names=["Rami"],
    )
    promoted = []

    async def _promote(trigger, *, verified=True):
        promoted.append((trigger, verified))

    game._promote_device_candidate = _promote
    assert _run(game.maybe_recognize_by_stated_name("Rami")) is True
    assert promoted == [("device_plus_name", False)]


def test_v2_recency_is_the_last_resort_when_history_is_unknown(monkeypatch):
    game = _name_door_game(
        monkeypatch, groups=["grp_newest", "grp_older"], history={},
    )
    assert _run(game.maybe_recognize_by_stated_name("Rami")) is True
    assert game.upgrades == [("grp_newest", "name_stated")]


def test_v2_memory_and_voiceprints_file_under_the_same_group_id(monkeypatch):
    """A cold room-name session with a device-stable id carried in: the
    session memory used to write under the ROOM NAME while the voiceprints
    redirected to the DEVICE id. finish_game's memory write now files
    under persistence_group_id — the same id fire_enrollment's voiceprint
    write uses."""
    game = _game("lily-ROOM1")
    game._carried_device_group_id = "dev_abcdef"
    assert game.persistence_group_id() == "dev_abcdef"
    assert game._effective_enroll_group_id() == "dev_abcdef"
    game.supabase = object()
    game.session = None
    game.agent = None
    game.prewager_standings = None
    game.finale_sent = False
    game.highlights = []
    game.ui_phase = "question"
    game.asked_history = []
    game.game_started = True
    game.session_started_at = time.time()
    written = {}

    async def _memory(sb, group_id, session_id, *a, **k):
        written["memory"] = group_id

    async def _report(sb, session_id, group_id, *a, **k):
        written["report"] = group_id

    async def _checkpoint(*a, **k):
        return None

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(lily_memory, "lily_write_session_memory", _memory)
    monkeypatch.setattr(lily_assessment, "lily_wrap_up_report", _report)
    monkeypatch.setattr(lily_persistence, "lily_checkpoint", _checkpoint)
    game.send_event = _noop
    game.publish_attributes = _noop
    game.publish_metadata = _noop

    async def scenario():
        await game.finish_game()
        await _settle(10)

    _run(scenario())
    assert written == {"memory": "dev_abcdef", "report": "dev_abcdef"}


# =============================================================================
# V3 — recognition carry keyed by speech id (I1, I2, late beat, suppression)
# =============================================================================


BLOCK = "[RETURNING TABLE]\nThis table has played with you 4 time(s) before."


def _fast_door(game, group="grp_thin"):
    game._name_door_entry_seq = game._ctx_snapshot_seq
    game.memory_block = BLOCK
    game._name_door_promotion_tail("name_stated", group)
    return game._name_door_watch


def test_v3_i1_greeting_finishing_first_yields_one_welcome_back_not_two():
    """Auditor B I1: the greeting is still in flight when the fast door arms
    the watch; it confirms first. Pre-fix: the watch resolved UNCARRIED on
    the greeting's confirm, the organic reply carried the welcome-back
    unobserved, and the seam beat aired a SECOND one."""
    game = _game()
    assert _fast_door(game) is not None
    # The greeting (its snapshot predates the door) plays out and confirms.
    game.note_recognition_playout_started("s-greet")
    _confirm(game, "s-greet")
    assert game.recognition_aired() is None
    # The organic reply snapshots WITH the block under ITS id, airs, confirms.
    game.note_generation_snapshot(speech_id="s-organic")
    assert game.late_recognition_blocked_reason() == "recognition_carry_inflight"
    game.note_recognition_playout_started("s-organic")
    _confirm(game, "s-organic", "Rami — four games deep, welcome back.")
    assert game.recognition_aired()["source"] == "name_door_organic"
    game.game_started = True
    game._game_start_committed = True
    assert game.flush_late_recognition_at_seam() is False
    assert _late_beats(game) == []                     # no second welcome-back


def test_v3_i2_unrelated_speech_never_stamps_and_a_cut_carrier_re_arms():
    """Auditor B I2: a preemptive generation snapshots with the block; an
    unrelated deterministic line confirms; the real reply is cut. Pre-fix:
    the deterministic line stamped recognition (never carried it) and the
    cut reply left a PERMANENT blackout."""
    game = _game()
    _fast_door(game)
    game.note_generation_snapshot(speech_id="s-preemptive")   # later invalidated
    game._dispatched_act_by_speech["s-floor"] = "floor_ack"
    game.note_recognition_playout_started("s-floor")
    _confirm(game, "s-floor", "I'm here — the floor's yours.")
    assert game.recognition_aired() is None                    # not stamped
    # The preemptive is invalidated (cancelled before airing) — dropped.
    _confirm(game, "s-preemptive", "", interrupted=True)
    assert game.recognition_aired() is None
    # The real reply snapshots, reaches the air, and is CUT.
    game.note_generation_snapshot(speech_id="s-real")
    game.note_recognition_playout_started("s-real")
    _confirm(game, "s-real", "Rami —", interrupted=True)
    assert game.recognition_aired() is None
    assert game._late_recognition_pending is True
    assert game._late_recognition_promotion_owed is True
    assert game.maybe_fire_late_recognition() is True          # no blackout
    assert len(_late_beats(game)) == 1


def test_v3_late_beat_stamps_on_its_own_confirm_never_at_dispatch():
    game = _game(memory_block=BLOCK)
    assert game.maybe_fire_late_recognition() is True
    assert game.recognition_aired() is None
    flight = game._late_recognition_flight
    assert flight is not None and flight["speech_id"] == "s-late_recognition-1"
    assert game.late_recognition_blocked_reason() == "recognition_beat_inflight"
    # An unrelated speech confirming meanwhile stamps nothing.
    _confirm(game, "s-other", "Next up —")
    assert game.recognition_aired() is None
    beat = flight["speech_id"]
    game.note_generation_snapshot(speech_id=beat)
    game.note_recognition_playout_started(beat)
    _confirm(game, beat, "Took me a second — I know this table.")
    assert game.recognition_aired()["source"] == "late_recognition_beat"
    assert game.maybe_fire_late_recognition() is False


def test_v3_suppressed_late_beat_re_arms_via_the_w1_seam():
    """Auditor D P1-2: the freshness gate / a flush suppresses the beat
    before it airs. Pre-fix the dispatch-time stamp had already retired
    every lane — blackout. Now on_dispatch_suppressed re-arms it as OWED."""
    game = _game(memory_block=BLOCK)
    assert game.maybe_fire_late_recognition() is True
    beat = game._late_recognition_flight["speech_id"]
    game.on_dispatch_suppressed("late_recognition", beat, "freshness_gate")
    assert game.recognition_aired() is None
    assert game._late_recognition_flight is None
    assert game._late_recognition_pending is True
    assert game._late_recognition_fired is False
    assert game.maybe_fire_late_recognition() is True
    assert len(_late_beats(game)) == 2
    # The suppressed-path playout exit (suppressed=True) re-arms the same way.
    beat2 = game._late_recognition_flight["speech_id"]
    game.note_generation_snapshot(speech_id=beat2)
    _confirm(game, beat2, "", suppressed=True)
    assert game.recognition_aired() is None
    assert game._late_recognition_pending is True


def test_v3_a_snapshot_without_a_speech_id_is_never_a_carrier():
    game = _game()
    _fast_door(game)
    game.note_generation_snapshot()  # unkeyed
    assert game._recognition_carriers in (None, {})
    _confirm(game, "s-anything", "Welcome back!")
    assert game.recognition_aired() is None      # honest: no receipt without an id
    assert game._late_recognition_pending is True


def test_v3_stale_flight_re_arms_instead_of_holding_the_seam_forever(monkeypatch):
    game = _game(memory_block=BLOCK)
    assert game.maybe_fire_late_recognition() is True
    game._late_recognition_flight["at"] -= 60.0     # never reached the air
    assert game.late_recognition_blocked_reason() != "recognition_beat_inflight"
    assert game._late_recognition_pending is True
    assert game._late_recognition_flight is None


# =============================================================================
# V4 — memory first; concurrent loads; door latency on the event
# =============================================================================


def test_v4_memory_is_visible_before_the_rekey_awaits_complete(monkeypatch):
    game = _game()
    gate = asyncio.Event()
    seen = {}

    async def _rekey(sb, old, new, sid):
        seen["block_at_rekey"] = game.memory_block
        await gate.wait()

    async def _asked(sb, gid): return []
    async def _prefs(sb, gid): return {}
    async def _memory(sb, gid):
        return {"total_games": 18, "player_names": ["Rami"], "sessions": []}

    monkeypatch.setattr(lily_persistence, "lily_rekey_group", _rekey)
    monkeypatch.setattr(lily_bank, "lily_load_asked_history", _asked)
    monkeypatch.setattr(lily_persistence, "lily_load_group_prefs", _prefs)
    monkeypatch.setattr(lily_memory, "lily_load_group_memory", _memory)
    game.device_candidate_group_id = "grp_rami"
    game._device_candidate_memory = {"total_games": 18, "player_names": ["Rami"]}
    game._device_candidate_memory_block = ""
    game._name_door_opened_at = time.time() - 0.5

    async def scenario():
        task = asyncio.get_running_loop().create_task(
            game._promote_device_candidate("name_stated", verified=False)
        )
        await _settle()
        assert game.memory_block.startswith("[RETURNING TABLE]")   # before rekey
        assert "STATED NAME" in game.memory_block                    # provenance
        assert game._name_door_watch is not None                     # tail decided
        gate.set()
        await task

    _run(scenario())
    assert seen["block_at_rekey"].startswith("[RETURNING TABLE]")
    (ev,) = game._identity_promotion_events
    assert ev["source"] == "name_stated" and ev["door_ms"] >= 400


def test_v4_upgrade_loads_run_concurrently(monkeypatch):
    game = _game()

    async def _slow(*a, **k):
        await asyncio.sleep(0.05)
        return [] if a and isinstance(a[-1], str) else None

    async def _memory(sb, gid):
        await asyncio.sleep(0.05)
        return {"total_games": 3, "player_names": ["Rami"], "sessions": []}

    async def _prefs(sb, gid):
        await asyncio.sleep(0.05)
        return {}

    monkeypatch.setattr(lily_persistence, "lily_rekey_group", _slow)
    monkeypatch.setattr(lily_bank, "lily_load_asked_history", _slow)
    monkeypatch.setattr(lily_persistence, "lily_load_group_prefs", _prefs)
    monkeypatch.setattr(lily_memory, "lily_load_group_memory", _memory)
    t0 = time.monotonic()
    _run(game.upgrade_group_id("grp_rami", "voice_identity_match"))
    elapsed = time.monotonic() - t0
    assert elapsed < 0.15, f"sequential loads: {elapsed:.3f}s"   # 4 x 0.05 = 0.20 sequential
    assert game.memory_block.startswith("[RETURNING TABLE]")
    assert "VOICE MATCH" in game.memory_block
    assert game.identity_confirmed_source == "voice_identity_match"


# =============================================================================
# V5 — the operator's verbatim wording, the forbidden strings, provenance
# =============================================================================


def _prompt_norm():
    return " ".join(
        (ROOT / "prompts" / "lily_system.txt").read_text(encoding="utf-8").split()
    )


def test_v5_pair_text_is_byte_identical_across_all_copies():
    prompt = _prompt_norm()
    assert " ".join(_PAIR1_CONFIRMED_VS_GUESSED.split()) in prompt
    assert " ".join(_PAIR2_DONT_NARRATE_THE_GAP.split()) in prompt
    assert " ".join(_PAIR3_NAME_QUESTION_ALONE.split()) in prompt
    assert getattr(lily_identity, "_PAIR1_CONFIRMED_VS_GUESSED", None) == _PAIR1_CONFIRMED_VS_GUESSED
    game = _game(memory_block=BLOCK, _first_human_utterance_seen=True)
    greet = game.greeting_instructions()
    assert _PAIR1_CONFIRMED_VS_GUESSED in greet
    game.maybe_fire_late_recognition()
    assert _PAIR1_CONFIRMED_VS_GUESSED in _late_beats(game)[0][2]


def test_v5_forbidden_strings_are_gone_from_both_sides():
    prompt = _prompt_norm()
    for bad in (
        "welcome back, all of you",
        "returners greeted BY NAME",
        "my table card doesn't have you tonight",
        "THREE separate beats on THREE separate turns",
    ):
        assert bad not in prompt, bad
    game = _game(memory_block=BLOCK, _first_human_utterance_seen=True)
    greet = game.greeting_instructions()
    for bad in (
        "do NOT say 'welcome back, <name>'",
        "SAME single question joined with",
        "join it into that same question with 'or'",
        "never 'my table card doesn't have you', never 'new device'",
        "I don't recognise the voice yet",
    ):
        assert bad not in greet, bad
    cold = _game(memory_block="", _first_human_utterance_seen=True).greeting_instructions()
    assert "I don't recognise the voice yet" not in cold
    assert _PAIR2_DONT_NARRATE_THE_GAP in cold


def test_v5_identity_status_is_derived_from_state_not_judgment():
    game = _game(memory_block=BLOCK, _first_human_utterance_seen=True)
    assert "GUESSED" in game.identity_status_line()
    assert "GUESSED" in game.greeting_instructions().split(_PAIR1_CONFIRMED_VS_GUESSED)[0]
    game.identity_confirmed_source = "voice_identity_match"
    assert "CONFIRMED — voice match" in game.identity_status_line()
    game.identity_confirmed_source = "device_plus_name"
    assert "gave their name this session" in game.identity_status_line()
    game.identity_confirmed_source = "participant_metadata"   # not a confirmed source
    assert "GUESSED" in game.identity_status_line()


def test_v5_memory_block_states_the_true_provenance():
    memory = {"total_games": 4, "player_names": ["Rami"], "sessions": [
        {"winner": "Rami", "question_count": 10}], "facts": []}
    voice = lily_memory.lily_build_memory_block(memory, recognized_by="voice_identity_match")
    name = lily_memory.lily_build_memory_block(memory, recognized_by="name_stated")
    both = lily_memory.lily_build_memory_block(memory, recognized_by="device_plus_name")
    device = lily_memory.lily_build_memory_block(memory, recognized_by="participant_metadata")
    assert "by VOICE MATCH — identity CONFIRMED" in voice
    assert "by a STATED NAME this session — identity CONFIRMED" in name
    assert "DEVICE plus a STATED NAME this session — identity CONFIRMED" in both
    assert "identity GUESSED" in device and "no 'welcome back'" in device
    assert "Voice recognition matched" not in name
    # The names-only branch no longer claims a voice matched them.
    thin = lily_memory.lily_build_memory_block(
        {"sessions": [], "facts": [], "player_names": ["Rami"], "total_games": 0},
        recognized_by="name_stated",
    )
    assert "Voice recognition matched" not in thin
    assert "STATED NAME" in thin


def test_v5_promotion_rebuilds_the_block_under_the_real_door():
    game = _game()
    game.device_candidate_group_id = "grp_rami"
    game._device_candidate_memory = {"total_games": 4, "player_names": ["Rami"],
                                     "sessions": [{"winner": "Rami"}], "facts": []}
    game._device_candidate_memory_block = lily_memory.lily_build_memory_block(
        game._device_candidate_memory, recognized_by="participant_metadata"
    )
    assert "GUESSED" in game._device_candidate_memory_block

    async def _upgrade(new_id, src):
        game.group_id, game.group_id_source = new_id, src

    game.upgrade_group_id = _upgrade
    _run(game._promote_device_candidate("device_plus_name", verified=False))
    assert "DEVICE plus a STATED NAME" in game.memory_block
    assert "GUESSED" not in game.memory_block
    assert game.identity_confirmed_source == "device_plus_name"


# =============================================================================
# V6 — the roster re-key ghost and the solo clamp
# =============================================================================


def test_v6_rekeyed_placeholder_migrates_into_the_named_seat():
    """Auditor B F8b (roster_probe C): placeholder minted under S1 (20s,
    2 points); the engine re-labels the SAME voice "Rami" after a
    known_speakers refresh; the bind arrives under "Rami". Pre-fix: a new
    seat "Rami" plus a ghost "S1" keeping the points."""
    sk = LilyScorekeeper("probe-c")
    sk.unrostered_labels["S1"] = 2
    sk.ensure_present_placeholder("S1")
    sk.players["S1"]["score"] = 2
    sk.players["S1"]["talk_time_s"] = 20.0
    sk.unrostered_labels["Rami"] = 1
    assert sk.ensure_present_placeholder("Rami") is None   # refused (one max)
    sk.drain_roster_events()
    sk.bind_speaker("Rami", "Rami")
    assert set(sk.players) == {"Rami"}
    assert sk.players["Rami"]["score"] == 2
    assert sk.players["Rami"]["speaker_label"] == "Rami"
    assert "placeholder" not in sk.players["Rami"]
    events = sk.drain_roster_events()
    migrate = next(e for e in events if e["kind"] == "migrate")
    assert migrate["old_key"] == "S1" and migrate["new"] == "Rami"
    assert migrate["rekeyed_from"] == "S1"


def test_v6_a_real_second_voice_under_a_generic_label_is_not_absorbed():
    sk = LilyScorekeeper("probe-d")
    sk.unrostered_labels["S1"] = 3
    sk.ensure_present_placeholder("S1")
    sk.players["S1"]["score"] = 2
    sk.players["S1"]["talk_time_s"] = 20.0
    sk.bind_speaker("S2", "Chris")            # generic label: a second person
    assert set(sk.players) == {"S1", "Chris"}
    assert sk.players["S1"]["score"] == 2


def test_v6_a_generic_label_still_talking_after_the_named_label_is_a_second_voice():
    sk = LilyScorekeeper("probe-c2")
    sk.on_transcript_segment("hello", speaker_label="S1", is_final=True, now=10.0)
    sk.ensure_present_placeholder("S1")
    sk.players["S1"]["score"] = 1
    sk.on_transcript_segment("I'm Rami", speaker_label="Rami", is_final=True, now=12.0)
    sk.on_transcript_segment("still me", speaker_label="S1", is_final=True, now=13.0)
    sk.bind_speaker("Rami", "Rami")
    assert set(sk.players) == {"S1", "Rami"}   # S1 kept talking: two voices


def test_v6_solo_clamp_never_retires_a_seat_with_a_record():
    """Auditor B F8c (roster_probe E): "just me" with an unnamed second voice
    that has 3 points / 2 answers. Pre-fix the clamp retired it WITH its
    score."""
    sk = LilyScorekeeper("probe-e")
    sk.players["S2"] = {"speaker_label": "S2", "speaker_id": None, "score": 3,
                        "streak": 1, "talk_time_s": 12.0, "answers_attempted": 2,
                        "answers_correct": 2, "last_correct_category": None,
                        "questions_since_spoke": 0, "lobby_fact": None,
                        "lifeline_available": True, "placeholder": True}
    sk.players["UU"] = {"speaker_label": "UU", "speaker_id": None, "score": 0,
                        "streak": 0, "talk_time_s": 0.0, "answers_attempted": 0,
                        "answers_correct": 0, "last_correct_category": None,
                        "questions_since_spoke": 2, "lobby_fact": None,
                        "lifeline_available": True, "placeholder": True}
    sk.bind_speaker("S1", "Rami")
    retired = sk.clamp_roster_solo("S1")
    assert retired == ["UU"]                         # the empty phantom only
    assert "S2" in sk.players and sk.players["S2"]["score"] == 3
    assert sk.solo_voice_label == "S1"


# =============================================================================
# V7 — every module-level name lily_identity.py references resolves
# =============================================================================


def test_v7_every_free_name_in_lily_identity_resolves():
    """The NameError class that silently downgraded live for two days (the
    W3 Cut 3 extraction moved methods without their module names; the
    except-clauses swallowed the NameError). Walk every function body and
    check each name that is neither local, parameter, nor builtin exists
    on the module (or is bound by a lazy import inside that function)."""
    src = (ROOT / "lily_identity.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    module_names = set(vars(lily_identity)) | set(dir(builtins))
    missing = _free_names(tree, module_names)
    assert not missing, f"unresolvable names in lily_identity.py: {sorted(missing)}"


def _scope_bound(func) -> set:
    """Names a function (or lambda) binds: its parameters plus every
    store / import / except-alias / comprehension target / nested def in
    its subtree (a superset is fine — over-binding only lowers sensitivity,
    never fakes a failure)."""
    args = func.args
    bound = {a.arg for a in args.args + args.kwonlyargs + getattr(args, "posonlyargs", [])}
    if args.vararg:
        bound.add(args.vararg.arg)
    if args.kwarg:
        bound.add(args.kwarg.arg)
    for node in ast.walk(func):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            if node is not func:
                if not isinstance(node, ast.Lambda):
                    bound.add(node.name)
                a = node.args
                bound |= {x.arg for x in a.args + a.kwonlyargs + getattr(a, "posonlyargs", [])}
                if a.vararg:
                    bound.add(a.vararg.arg)
                if a.kwarg:
                    bound.add(a.kwarg.arg)
        elif isinstance(node, ast.ClassDef):
            bound.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.comprehension):
            for t in ast.walk(node.target):
                if isinstance(t, ast.Name):
                    bound.add(t.id)
    return bound


def _free_names(tree, module_names) -> set:
    """Every (function, name) whose Load has no binding in the function,
    any enclosing function, the module, or builtins — the NameError class."""
    missing = set()

    def visit(node, enclosing):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                bound = enclosing | _scope_bound(child)
                name = getattr(child, "name", "<lambda>")
                for sub in ast.walk(child):
                    if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                        if sub.id not in bound and sub.id not in module_names:
                            missing.add((name, sub.id))
                visit(child, bound)
            else:
                visit(child, enclosing)

    visit(tree, set())
    return missing


def test_v7_guard_catches_a_missing_module_name():
    """The guard is not vacuous: the pre-WO-2 shape (upgrade_group_id
    referencing lily_bank with no import) is detected by the same walk."""
    tree = ast.parse(
        "import x\n\ndef f(a):\n    def g(b):\n        return a + b + x.y\n"
        "    return lily_bank.load(a) + g(1)\n"
    )
    module_names = {"x"} | set(dir(builtins))
    assert _free_names(tree, module_names) == {("f", "lily_bank")}
