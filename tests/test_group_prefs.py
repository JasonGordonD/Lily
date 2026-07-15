"""Group preferences WO tests: the pacing flag (spoken detection, sticky
scorekeeper state, snapshot/rehydrate, state-block line, relaxed window
multiplier), the opaque lily_group_prefs persistence (write/load/re-key),
the forget-cascade interlock's in-session half, the ask-once flow latch,
and prefs application at game start.

Pure-logic tests run against lily_scorekeeper / lily_memory /
lily_persistence with a fake postgrest client; the game-level tests import
lily_agent (and therefore livekit) — same boundary note as
test_forget_flow.py / test_say_gate_dispatch.py.
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_audeering_consumers
import lily_config
import lily_memory
import lily_persistence
import lily_say_gate
from lily_agent import LilyAgent, LilyGame
from lily_memory import lily_build_memory_block, lily_prefs_summary
from lily_scorekeeper import (
    LilyScorekeeper,
    lily_detect_control_command,
    lily_detect_pacing_choice,
)


# ---------------------------------------------------------------------------
# Spoken pacing detection — deterministic, paraphrase-tolerant
# ---------------------------------------------------------------------------

def test_relaxed_phrases_fire():
    for phrase in (
        "let's play relaxed",
        "Let's play relaxed rounds!",
        "can we keep it relaxed",
        "keep it chill please",
        "let's go freeform",
        "make it casual",
        "relaxed pace please",
        "freeform mode",
        "untimed rounds",
        "no timer please",
        "can we do this without the timer",
        "turn the timer off",
        "turn off the countdown",
        "Relaxed.",
        "relaxed please",
    ):
        assert lily_detect_control_command(phrase) == "pacing_relaxed", phrase


def test_timed_phrases_fire():
    for phrase in (
        "timed rounds",
        "let's do timed rounds",
        "Timed rounds, please!",
        "keep it timed",
        "let's play timed",
        "timed mode",
        "with the timer",
        "put us on the clock",
        "put the timer back on",
        "turn the clock back on",
        "bring the timer back",
        "Timed.",
        "timed please",
    ):
        assert lily_detect_control_command(phrase) == "pacing_timed", phrase


def test_negated_timed_reads_as_relaxed():
    for phrase in (
        "no timed rounds",
        "don't want timed rounds",
        "not timed please",
        "can we do this without timed rounds",
    ):
        assert lily_detect_control_command(phrase) == "pacing_relaxed", phrase


def test_ordinary_speech_never_flips_pacing():
    for phrase in (
        "I'm relaxed",                      # standalone-word rule: whole
        "she seemed pretty relaxed to me",  # utterance only
        "that was a relaxing question",
        "the timer on my oven broke",
        "we timed the eggs perfectly",
        "it's about time",
        "take it easy Dave",
        "Tungsten",
        "",
    ):
        cmd = lily_detect_control_command(phrase)
        assert cmd not in ("pacing_relaxed", "pacing_timed"), (phrase, cmd)


def test_ambiguous_both_directions_is_none():
    # Fires both ways un-negated -> nothing flips on ambiguity.
    assert lily_detect_pacing_choice("timed rounds or relaxed rounds") is None


def test_pacing_choice_wins_over_start_game():
    # "let's play relaxed" is a pacing choice, NOT a game start; a bare
    # "let's play" still starts the game.
    assert lily_detect_control_command("let's play relaxed") == "pacing_relaxed"
    assert lily_detect_control_command("let's play") == "start_game"
    assert lily_detect_control_command("let's play timed rounds") == "pacing_timed"


def test_pacing_fragment_proof_across_finals():
    sk = LilyScorekeeper("test-room")
    sk.bind_speaker("S1", "Sarah")
    r1 = sk.on_transcript_segment(text="No.", speaker_label="S1", now=100.0)
    assert r1["control_command"] is None
    r2 = sk.on_transcript_segment(text="Timer.", speaker_label="S1", now=101.0)
    assert r2["control_command"] == "pacing_relaxed"


def test_pacing_choice_not_recorded_as_answer_candidate():
    sk = LilyScorekeeper("test-room")
    sk.bind_speaker("S1", "Sarah")
    sk.start_question({"prompt": "q", "canonical_answer": "Tungsten"})
    sk.open_answer_window(now=100.0)
    result = sk.on_transcript_segment(
        text="let's play relaxed", speaker_label="S1", now=101.0,
        segment_start_time=101.0,
    )
    assert result["control_command"] == "pacing_relaxed"
    assert result["candidate_recorded"] is False
    assert sk.answer_candidates == {}


# ---------------------------------------------------------------------------
# Scorekeeper pacing flag — sticky, snapshotted, in the state block
# ---------------------------------------------------------------------------

def test_pacing_defaults_timed_and_validates():
    sk = LilyScorekeeper("test-room")
    assert sk.pacing == "timed"
    sk.set_pacing("relaxed")
    assert sk.pacing == "relaxed"
    sk.set_pacing("frantic")  # invalid — ignored
    assert sk.pacing == "relaxed"
    sk.set_pacing("timed")
    assert sk.pacing == "timed"


def test_pacing_survives_snapshot_rehydrate():
    sk = LilyScorekeeper("room-1")
    sk.set_pacing("relaxed")
    snap = sk.snapshot()
    assert snap["pacing"] == "relaxed"
    sk2 = LilyScorekeeper("room-1")
    sk2.rehydrate(snap)
    assert sk2.pacing == "relaxed"
    # Old checkpoints (no pacing key) rehydrate to the default untouched.
    sk3 = LilyScorekeeper("room-1")
    old_snap = {k: v for k, v in snap.items() if k != "pacing"}
    sk3.rehydrate(old_snap)
    assert sk3.pacing == "timed"


def test_state_block_carries_pacing_and_relaxed_note():
    sk = LilyScorekeeper("test-room")
    block = sk.build_state_block()
    assert "pacing=timed" in block
    assert "looser tempo" not in block  # timed = today's behavior, no note
    sk.set_pacing("relaxed")
    block = sk.build_state_block()
    assert "pacing=relaxed" in block
    assert "looser tempo" in block
    assert "no countdown talk" in block


# ---------------------------------------------------------------------------
# Relaxed window multiplier — config + the game's duration derivation
# ---------------------------------------------------------------------------

def test_relaxed_window_multiplier_config(monkeypatch):
    monkeypatch.delenv("LILY_RELAXED_WINDOW_MULTIPLIER", raising=False)
    assert lily_config.relaxed_window_multiplier() == 2.0
    monkeypatch.setenv("LILY_RELAXED_WINDOW_MULTIPLIER", "3.5")
    assert lily_config.relaxed_window_multiplier() == 3.5
    monkeypatch.setenv("LILY_RELAXED_WINDOW_MULTIPLIER", "not-a-number")
    assert lily_config.relaxed_window_multiplier() == 2.0


def _minimal_game() -> LilyGame:
    game = LilyGame.__new__(LilyGame)
    game.sk = LilyScorekeeper("test-room")
    return game


def test_answer_window_duration_timed_is_todays_behavior(monkeypatch):
    monkeypatch.delenv("LILY_ANSWER_WINDOW_SECONDS", raising=False)
    monkeypatch.delenv("LILY_RELAXED_WINDOW_MULTIPLIER", raising=False)
    game = _minimal_game()
    assert game._answer_window_duration() == lily_config.answer_window_seconds()
    assert game._answer_window_duration() == 15.0


def test_answer_window_duration_relaxed_stretches(monkeypatch):
    monkeypatch.delenv("LILY_ANSWER_WINDOW_SECONDS", raising=False)
    monkeypatch.delenv("LILY_RELAXED_WINDOW_MULTIPLIER", raising=False)
    game = _minimal_game()
    game.sk.set_pacing("relaxed")
    assert game._answer_window_duration() == 30.0  # 15s x 2.0 default
    monkeypatch.setenv("LILY_RELAXED_WINDOW_MULTIPLIER", "1.5")
    assert game._answer_window_duration() == 22.5
    # Flipping back to timed restores the exact base window.
    game.sk.set_pacing("timed")
    assert game._answer_window_duration() == 15.0


# ---------------------------------------------------------------------------
# Fake postgrest client (select/upsert/update/delete/eq/in_/limit)
# ---------------------------------------------------------------------------

class _FakeResult:
    def __init__(self, data=None, count=None):
        self.data = data
        self.count = count


class _FakeQuery:
    def __init__(self, db, table):
        self._db = db
        self._table = table
        self._action = "select"
        self._payload = None
        self._on_conflict = None
        self._filters = []
        self._limit = None

    def select(self, *_cols, count=None):
        self._action = "select"
        return self

    def delete(self):
        self._action = "delete"
        return self

    def update(self, payload):
        self._action = "update"
        self._payload = payload
        return self

    def upsert(self, payload, on_conflict=None):
        self._action = "upsert"
        self._payload = payload
        self._on_conflict = on_conflict
        return self

    def eq(self, col, val):
        self._filters.append(lambda row: row.get(col) == val)
        return self

    def in_(self, col, vals):
        vals = list(vals)
        self._filters.append(lambda row: row.get(col) in vals)
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, n):
        self._limit = n
        return self

    def execute(self):
        if self._table not in self._db.tables:
            raise Exception(
                f'relation "public.{self._table}" does not exist (42P01)'
            )
        rows = self._db.tables[self._table]
        matched = [r for r in rows if all(f(r) for f in self._filters)]
        if self._action == "delete":
            self._db.tables[self._table] = [r for r in rows if r not in matched]
            return _FakeResult(data=list(matched))
        if self._action == "update":
            for r in matched:
                r.update(self._payload)
            return _FakeResult(data=list(matched))
        if self._action == "upsert":
            self._db.upserts.append((self._table, dict(self._payload),
                                     self._on_conflict))
            key = self._on_conflict or "group_id"
            existing = next(
                (r for r in rows if r.get(key) == self._payload.get(key)), None
            )
            if existing is not None:
                existing.update(self._payload)
            else:
                rows.append(dict(self._payload))
            return _FakeResult(data=[dict(self._payload)])
        data = matched[: self._limit] if self._limit is not None else matched
        return _FakeResult(data=list(data))


class _FakeSupabase:
    def __init__(self, tables=None):
        self.tables = {
            name: [dict(r) for r in rows]
            for name, rows in (tables or {}).items()
        }
        self.upserts = []

    def table(self, name):
        return _FakeQuery(self, name)


# ---------------------------------------------------------------------------
# lily_group_prefs persistence — whole-dict opaque upsert, load, re-key
# ---------------------------------------------------------------------------

def test_write_and_load_prefs_roundtrip():
    db = _FakeSupabase({"lily_group_prefs": []})
    prefs = {"pacing": "relaxed", "media_mode": "images"}  # opaque foreign key
    asyncio.run(lily_persistence.lily_write_group_prefs(db, "gA", prefs))
    table, payload, on_conflict = db.upserts[0]
    assert table == "lily_group_prefs"
    assert on_conflict == "group_id"
    assert payload["group_id"] == "gA"
    assert payload["prefs"] == prefs  # the WHOLE dict, opaquely
    assert "updated_at" in payload
    loaded = asyncio.run(lily_persistence.lily_load_group_prefs(db, "gA"))
    assert loaded == prefs


def test_load_prefs_missing_or_no_client_is_empty():
    db = _FakeSupabase({"lily_group_prefs": []})
    assert asyncio.run(lily_persistence.lily_load_group_prefs(db, "gX")) == {}
    assert asyncio.run(lily_persistence.lily_load_group_prefs(None, "gX")) == {}
    assert asyncio.run(lily_persistence.lily_load_group_prefs(db, "")) == {}
    # Absent table (013 not applied) degrades to cold-group behavior.
    bare = _FakeSupabase({})
    assert asyncio.run(lily_persistence.lily_load_group_prefs(bare, "gX")) == {}


def test_rekey_prefs_moves_row_to_resolved_id():
    db = _FakeSupabase({"lily_group_prefs": [
        {"group_id": "room-1", "prefs": {"pacing": "relaxed"}},
    ]})
    asyncio.run(lily_persistence.lily_rekey_group_prefs(
        db, "room-1", "grp_resolved", "room-1"
    ))
    rows = db.tables["lily_group_prefs"]
    assert {r["group_id"] for r in rows} == {"grp_resolved"}
    assert rows[0]["prefs"] == {"pacing": "relaxed"}


def test_rekey_prefs_merges_with_session_choices_winning():
    # The resolved group has a stored usual; this session (under the
    # provisional room-random id) chose differently and added a foreign
    # key — the session's choices win key-by-key, opaquely.
    db = _FakeSupabase({"lily_group_prefs": [
        {"group_id": "grp_resolved",
         "prefs": {"pacing": "timed", "round_format": "mc"}},
        {"group_id": "room-1",
         "prefs": {"pacing": "relaxed", "media_mode": "images"}},
    ]})
    asyncio.run(lily_persistence.lily_rekey_group_prefs(
        db, "room-1", "grp_resolved", "room-1"
    ))
    rows = db.tables["lily_group_prefs"]
    assert len(rows) == 1  # old row deleted (provisional id == session id)
    assert rows[0]["group_id"] == "grp_resolved"
    assert rows[0]["prefs"] == {
        "pacing": "relaxed",          # session choice won
        "round_format": "mc",         # stored key survived, untouched
        "media_mode": "images",       # session's foreign key rode along
    }


def test_rekey_prefs_conservative_delete_scope():
    # A prior name-set-hash id is NOT the session id — its row is merged
    # into the resolved id but never deleted (same conservatism as the
    # voiceprint re-key: never destroys another real group's row).
    db = _FakeSupabase({"lily_group_prefs": [
        {"group_id": "grp_namehash", "prefs": {"pacing": "relaxed"}},
    ]})
    asyncio.run(lily_persistence.lily_rekey_group_prefs(
        db, "grp_namehash", "grp_device", "room-1"
    ))
    by_group = {r["group_id"]: r["prefs"] for r in db.tables["lily_group_prefs"]}
    assert by_group == {
        "grp_namehash": {"pacing": "relaxed"},
        "grp_device": {"pacing": "relaxed"},
    }


def test_rekey_prefs_noop_without_old_row_and_tolerates_absent_table():
    db = _FakeSupabase({"lily_group_prefs": [
        {"group_id": "grp_other", "prefs": {"pacing": "timed"}},
    ]})
    asyncio.run(lily_persistence.lily_rekey_group_prefs(
        db, "room-1", "grp_resolved", "room-1"
    ))
    assert db.tables["lily_group_prefs"] == [
        {"group_id": "grp_other", "prefs": {"pacing": "timed"}}
    ]
    # Absent table (013 lag) never raises out of the re-key.
    asyncio.run(lily_persistence.lily_rekey_group_prefs(
        _FakeSupabase({}), "room-1", "grp_resolved", "room-1"
    ))


def test_lily_rekey_group_carries_prefs_along():
    db = _FakeSupabase({
        "lily_sessions": [{"session_id": "room-1", "group_id": "room-1"}],
        "lily_group_facts": [],
        "lily_speaker_voiceprints": [],
        "lily_group_prefs": [
            {"group_id": "room-1", "prefs": {"pacing": "relaxed"}},
        ],
    })
    asyncio.run(lily_persistence.lily_rekey_group(
        db, "room-1", "grp_resolved", "room-1"
    ))
    assert db.tables["lily_group_prefs"][0]["group_id"] == "grp_resolved"
    assert db.tables["lily_sessions"][0]["group_id"] == "grp_resolved"


# ---------------------------------------------------------------------------
# Prefs summary + the [RETURNING TABLE] "usual" line
# ---------------------------------------------------------------------------

def test_prefs_summary_shapes():
    assert lily_prefs_summary({"pacing": "relaxed"}) == "relaxed pacing"
    assert lily_prefs_summary({}) == ""
    assert lily_prefs_summary(None) == ""
    # Opaque foreign keys render generically, deterministically sorted.
    assert lily_prefs_summary(
        {"pacing": "timed", "round_format": "mc"}
    ) == "timed pacing, round_format: mc"
    # Non-scalar / empty values are skipped, never crash the line.
    assert lily_prefs_summary(
        {"pacing": "relaxed", "junk": {"nested": 1}, "empty": ""}
    ) == "relaxed pacing"


def _memory():
    return {
        "sessions": [{"winner": "Dave", "question_count": 12,
                      "players": [{"name": "Dave", "score": 5}]}],
        "facts": [],
        "player_names": ["Dave"],
        "last_winner": "Dave",
        "total_games": 2,
    }


def test_memory_block_gains_compact_prefs_line():
    block = lily_build_memory_block(_memory(), prefs={"pacing": "relaxed"})
    assert "usual: relaxed pacing." in block


def test_memory_block_without_prefs_has_no_usual_line():
    assert "usual:" not in lily_build_memory_block(_memory())
    assert "usual:" not in lily_build_memory_block(_memory(), prefs={})


# ---------------------------------------------------------------------------
# Game-level harness (test_forget_flow pattern)
# ---------------------------------------------------------------------------

class _FakeSession:
    def __init__(self) -> None:
        self.instructions: list[str] = []

    def generate_reply(self, instructions: str) -> None:
        self.instructions.append(instructions)


class _FakeAgentHandle:
    def set_preemptive_generation(self, enabled: bool) -> None:
        pass


class _FakeLocalParticipant:
    def __init__(self) -> None:
        self.attributes: dict = {}

    async def set_attributes(self, attrs) -> None:
        self.attributes.update(attrs)


class _FakeRoom:
    def __init__(self) -> None:
        self.local_participant = _FakeLocalParticipant()


class _FakeCtx:
    def __init__(self) -> None:
        self.room = _FakeRoom()


def _make_game(supabase=None) -> LilyGame:
    game = LilyGame.__new__(LilyGame)
    game.ctx = _FakeCtx()
    game.session = _FakeSession()
    game.agent = _FakeAgentHandle()
    game._preemptive_paused = False
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game.sk = LilyScorekeeper("test-room")
    game.sk.bind_speaker("S1", "Sarah")
    game.ui_phase = "lobby"
    game.memory_block = "[RETURNING TABLE]\nrematch energy."
    game.memory_total_games = 2
    game._memory_disclosure_offered = True
    game.reconnected = False
    game.game_started = True
    game.game_over = False
    game.armed_question = None
    game.next_question = None
    game.used_prompts = []
    game.supabase = supabase
    game._window_timer = None
    game._bed_handle = None
    game._pending_unbound_award = None
    game.acoustic = lily_audeering_consumers.LilyAcousticState()
    game.group_id = "grp_device_uuid"
    game.group_id_source = "participant_metadata"
    game.highlights = []
    game.pending_clarify = {}
    game._addressee_rows = {}
    game._user_turn_index = 0
    game.forget_state = "idle"
    game.forget_requester = None
    game._forget_target_group = None
    game.prefs = {}
    game._prefs_offer_made = False
    return game


def _segment(game: LilyGame, text: str, label: str = "S1"):
    now = time.time()
    result = game.sk.on_transcript_segment(
        text=text, speaker_label=label, now=now, segment_start_time=now
    )
    game.on_transcript_event(result, text, speaker_label=label, segment_ts=now)
    return result


# ---------------------------------------------------------------------------
# set_pacing — flag + opaque prefs + persistence + seam attribute
# ---------------------------------------------------------------------------

def test_set_pacing_flips_flag_and_persists_whole_dict():
    db = _FakeSupabase({"lily_group_prefs": []})
    game = _make_game(supabase=db)
    game.prefs = {"media_mode": "images"}  # a concurrent feature's key

    async def _run():
        assert game.set_pacing("relaxed", source="test") is True
        await asyncio.sleep(0.01)  # let the fire-and-forget write land

    asyncio.run(_run())
    assert game.sk.pacing == "relaxed"
    assert game.prefs == {"media_mode": "images", "pacing": "relaxed"}
    _table, payload, on_conflict = db.upserts[0]
    assert on_conflict == "group_id"
    assert payload["group_id"] == "grp_device_uuid"
    # The WHOLE dict persisted opaquely — the foreign key rode along.
    assert payload["prefs"] == {"media_mode": "images", "pacing": "relaxed"}
    # Seam addition: the pacing participant attribute published.
    assert game.ctx.room.local_participant.attributes.get("pacing") == "relaxed"


def test_set_pacing_rejects_invalid():
    game = _make_game()

    async def _run():
        assert game.set_pacing("frantic", source="test") is False

    asyncio.run(_run())
    assert game.sk.pacing == "timed"
    assert game.prefs == {}


def test_spoken_pacing_choice_commits_and_acknowledges():
    db = _FakeSupabase({"lily_group_prefs": []})
    game = _make_game(supabase=db)

    async def _run():
        _segment(game, "let's play relaxed")
        await asyncio.sleep(0.01)

    asyncio.run(_run())
    assert game.sk.pacing == "relaxed"
    assert game.prefs["pacing"] == "relaxed"
    assert len(db.upserts) == 1  # persisted on the preference change
    assert len(game.session.instructions) == 1
    ack = game.session.instructions[0]
    assert "relaxed" in ack
    assert "usual" in ack
    # Flipping back by voice works the same way.
    asyncio.run(_drive_one(game, "timed rounds please"))
    assert game.sk.pacing == "timed"
    assert game.prefs["pacing"] == "timed"


async def _drive_one(game: LilyGame, text: str) -> None:
    _segment(game, text)
    await asyncio.sleep(0.01)


# ---------------------------------------------------------------------------
# Prefs application at game start — silent, opaque
# ---------------------------------------------------------------------------

def test_apply_prefs_at_game_start_sets_pacing_silently():
    game = _make_game()
    game.prefs = {"pacing": "relaxed", "round_format": "mc"}  # opaque mix
    game.apply_prefs_at_game_start()
    assert game.sk.pacing == "relaxed"
    # Silent: no instructed reply was dispatched for "the usual".
    assert game.session.instructions == []
    # The foreign key passed through untouched for its own feature.
    assert game.prefs["round_format"] == "mc"


def test_apply_prefs_ignores_invalid_and_empty():
    game = _make_game()
    game.prefs = {"pacing": "frantic"}
    game.apply_prefs_at_game_start()
    assert game.sk.pacing == "timed"
    game.prefs = {}
    game.apply_prefs_at_game_start()
    assert game.sk.pacing == "timed"


def test_start_game_applies_the_usual():
    game = _make_game()
    game.game_started = False
    game.prefs = {"pacing": "relaxed"}

    async def _noop_async(*a, **k):
        return None

    game.resolve_group_identity = _noop_async
    game.publish_attributes = _noop_async
    game.start_prefetch = lambda: None
    game.arm_next_question = lambda: True
    game.fire_enrollment = lambda trigger: None
    game._enroll_started = False
    asyncio.run(game.start_game("host_tool"))
    assert game.sk.pacing == "relaxed"


# ---------------------------------------------------------------------------
# The ask-once flow — latch semantics
# ---------------------------------------------------------------------------

def test_prefs_offer_issued_once_with_the_usual():
    game = _make_game()
    game.prefs = {"pacing": "relaxed"}
    offer = game.prefs_offer_instruction()
    assert "play the usual" in offer
    assert "relaxed pacing" in offer
    assert "Never ask about preferences again tonight" in offer
    # Consumed — never re-asked this session.
    assert game.prefs_offer_instruction() == ""


def test_prefs_offer_empty_for_cold_groups():
    game = _make_game()
    game.prefs = {}
    assert game.prefs_offer_instruction() == ""
    assert game._prefs_offer_made is False  # nothing consumed


def test_returning_greeting_carries_the_ask_once():
    game = _make_game()
    game.prefs = {"pacing": "relaxed"}
    greet = game.greeting_instructions()
    assert "play the usual" in greet
    assert "relaxed pacing" in greet
    # The latch: a second greeting build never repeats the question.
    assert "play the usual" not in game.greeting_instructions()


def test_cold_greeting_has_no_preferences_ceremony():
    game = _make_game()
    game.memory_block = ""
    game.prefs = {}
    greet = game.greeting_instructions()
    assert "play the usual" not in greet


def test_start_game_ride_along_offers_once_after_midlobby_upgrade():
    # Memory + prefs resolved AFTER the greeting (mid-lobby upgrade): the
    # game-start beat carries the ask-once — under the same latch.
    game = _make_game()
    game.game_started = False
    game.prefs = {"pacing": "relaxed"}

    async def _noop_async(*a, **k):
        return None

    game.resolve_group_identity = _noop_async
    game.publish_attributes = _noop_async
    game.start_prefetch = lambda: None
    game.arm_next_question = lambda: True
    game.fire_enrollment = lambda trigger: None
    game._enroll_started = False
    asyncio.run(game.start_game("voice"))
    assert len(game.session.instructions) == 1
    assert "play the usual" in game.session.instructions[0]
    # Latch consumed — a later greeting build never re-asks.
    assert "play the usual" not in game.greeting_instructions()


# ---------------------------------------------------------------------------
# Forget interlock — the in-session half (the cascade half lives in
# test_forget.py: lily_group_prefs joins the plan and the executor)
# ---------------------------------------------------------------------------

def test_forget_teardown_clears_prefs_but_keeps_live_pacing():
    game = _make_game()
    game.stt = None
    game.prefs = {"pacing": "relaxed"}
    game.sk.set_pacing("relaxed")

    async def _run():
        await game.execute_forget(source="test")

    asyncio.run(_run())
    assert game.forget_state == "done"
    # The stored 'usual' is recognition data — gone with the identity.
    assert game.prefs == {}
    # Tonight's tempo is tonight's choice, not identity — the game keeps
    # its live pacing.
    assert game.sk.pacing == "relaxed"
    # A fresh choice after the deletion persists under the fresh id only.
    assert game.group_id.startswith("anon_")
