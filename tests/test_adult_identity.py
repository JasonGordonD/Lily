"""UNIVERSAL QUESTION IDENTITY + adult transition (WO-LILY-DESYNC-HONESTY-001
Sub-agent D).

Live findings (2026-07-15 01:43+ adult segment):

1. Three adult reveals (Freud, Franklin, anatomy) each played TWICE — the
   adult deck served questions with no armed q_N identity, so reveals had
   no dedup keys. Fix: the adult deck flows through the SAME armed pipeline
   (arm -> claim -> window -> reveal, keyed end-to-end); the supply gap
   that invited freestyle presentation is closed by flush + immediate
   re-prefetch on every mode switch.
2. Adult entry served a leftover GENERAL question ("powerhouse of the
   cell" — user: "wait, THAT's the adult section?") because the armed
   queue survived the mode switch. Fix: flush_for_mode_switch on BOTH
   directions.
3. Adult questions announced as "academic category". Fix: category labels
   follow the bank row (adult_couples / adult_kink, migration 014); the
   round-family rotation is mode-aware and never overwrites them.

Harness pattern from test_stall_recovery.py (LilyGame via __new__).
"""

import asyncio
import json
import logging
import time
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_audeering_consumers
import lily_say_gate
from lily_agent import LilyGame, ADULT_CATEGORY_FAMILIES
from lily_persistence import lily_fetch_bank_question
from lily_scorekeeper import LilyScorekeeper


class _FakeSession:
    def __init__(self) -> None:
        self.instructions: list[str] = []

    def generate_reply(self, instructions: str) -> None:
        self.instructions.append(instructions)


class _FakeAgentHandle:
    def set_preemptive_generation(self, enabled: bool) -> None:
        pass


class _FakeRoomAPI:
    def __init__(self) -> None:
        self.requests: list = []

    async def update_room_metadata(self, req) -> None:
        self.requests.append(req)


class _FakeLocalParticipant:
    def __init__(self) -> None:
        self.attributes: dict = {}

    async def set_attributes(self, attrs) -> None:
        self.attributes.update(attrs)


class _FakeCtx:
    def __init__(self) -> None:
        self.api = type("API", (), {"room": _FakeRoomAPI()})()
        self.room = type(
            "Room", (),
            {"name": "test-room", "local_participant": _FakeLocalParticipant()},
        )()


GENERAL_Q = {
    "id": "q_2001",
    "prompt": "Known as the powerhouse of the cell, what is this organelle?",
    "canonical_answer": "the mitochondria",
    "acceptable_answers": ["mitochondria", "the mitochondria", "mitochondrion"],
    "reveal_color": "",
    "category": "academic",
    "difficulty_tier": 1,
}

GENERAL_Q2 = {
    "id": "q_2002",
    "prompt": "What is the capital of Australia?",
    "canonical_answer": "Canberra",
    "acceptable_answers": ["canberra"],
    "reveal_color": "",
    "category": "academic",
    "difficulty_tier": 1,
}

ADULT_Q = {
    "id": "kb_9101",
    "prompt": "In repeated brand surveys, the position that most often "
              "claims the number one spot is this one.",
    "canonical_answer": "Doggy style",
    "acceptable_answers": ["doggy style", "doggy"],
    "reveal_color": "",
    "category": "adult_couples",
    "difficulty_tier": 1,
}

ADULT_Q2 = {
    "id": "kb_9102",
    "prompt": "In sexual play, the practice known as edging means this.",
    "canonical_answer": "building up to orgasm and then pausing",
    "acceptable_answers": ["orgasm control", "stop and start"],
    "reveal_color": "",
    "category": "adult_kink",
    "difficulty_tier": 1,
}


class _DeckReasoning:
    """Deck-aware supply stub: serves from the deck matching sk.mode AT
    FETCH TIME (mirrors mode-aware generation / the mode-filtered bank).
    Each deck is a list consumed front-to-back."""

    def __init__(self, general=(), adult=(), delay: float = 0.0):
        self.general = [dict(q) for q in general]
        self.adult = [dict(q) for q in adult]
        self.delay = delay
        self.calls: list[dict] = []

    async def prefetch_question(self, sk, **kw):
        self.calls.append({"mode": sk.mode, **kw})
        if self.delay:
            await asyncio.sleep(self.delay)
        deck = self.adult if sk.mode == "adult" else self.general
        return deck.pop(0) if deck else None

    async def prefetch_picture_question(self, supabase, **kw):
        return None


def _make_game(reasoning=None) -> LilyGame:
    game = LilyGame.__new__(LilyGame)
    game.ctx = _FakeCtx()
    game.session = _FakeSession()
    game.agent = _FakeAgentHandle()
    game._preemptive_paused = False
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game.sk = LilyScorekeeper("test-room")
    game.rounds_total = 3
    game.ui_phase = "answering"
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
    game.promoted_categories = set()
    game.prewager_standings = None
    game.highlights = []
    game.supabase = None
    game.reasoning = reasoning or _DeckReasoning()
    game.background_audio = None
    game._bed_handle = None
    game._prefetch_task = None
    game._window_timer = None
    game._watchdog_task = None
    game._prefetch_stall_ticks = 0
    game._steal_window = False
    game._adjudicating = False
    game._judged_keys = set()
    game._spec_judge = {}
    game._addressee_rows = {}
    game._pending_reveal_event = None
    game._pending_unbound_award = None
    game._user_turn_index = 0
    game._armed_speech_misses = 0
    game.pending_clarify = {}
    game.forget_state = "idle"
    game.forget_requester = None
    game._forget_target_group = None
    game.acoustic = lily_audeering_consumers.LilyAcousticState()
    return game


def _arm(game: LilyGame, question: dict) -> None:
    game.armed_question = dict(question)
    game.sk.start_question(game.armed_question)
    game.sk.round = 1
    game.sk.set_phase("round")


def _final(game: LilyGame, text: str, label: str, at: float) -> dict:
    return game.sk.on_transcript_segment(
        text=text, speaker_label=label, is_final=True,
        now=at, segment_start_time=at,
    )


def _drain(game: LilyGame, seconds: float = 0.05) -> None:
    """Run the loop long enough for flush -> prefetch -> auto-advance to
    settle, then cancel stragglers."""

    async def scenario():
        await asyncio.sleep(seconds)
        for t in asyncio.all_tasks():
            if t is not asyncio.current_task():
                t.cancel()

    asyncio.run(scenario())


# ─── 1. mode switch flushes and re-arms from the adult deck ─────────────────

def test_enter_adult_flushes_leftover_general_and_arms_adult():
    # The exact live defect: a general question armed + one prefetched when
    # the table switches to adult. Both must die; the adult deck arms.
    reasoning = _DeckReasoning(general=[GENERAL_Q2], adult=[ADULT_Q, ADULT_Q2])
    game = _make_game(reasoning)
    _arm(game, GENERAL_Q)
    game.next_question = dict(GENERAL_Q2)

    async def scenario():
        game.sk.set_mode("adult")
        game.flush_for_mode_switch(source="enter_adult")
        # Flushed synchronously — nothing from the general deck survives.
        assert game.armed_question is None
        assert game.next_question is None
        assert game.sk.current_question is None
        await asyncio.sleep(0.05)
        for t in asyncio.all_tasks():
            if t is not asyncio.current_task():
                t.cancel()

    asyncio.run(scenario())

    # Re-armed from the ADULT deck via the prefetch auto-advance...
    assert game.armed_question is not None
    assert game.armed_question["id"] == ADULT_Q["id"]
    assert game.armed_question["category"] == "adult_couples"
    # ...through the armed pipeline: identity registered on the scorekeeper.
    assert game.sk.current_question["id"] == ADULT_Q["id"]
    # The nudge tells Lily to ask it from the state block, word for word.
    assert any("state block" in i for i in game.session.instructions)
    # The general leftover can never be served as adult material.
    assert (game.armed_question or {}).get("prompt") != GENERAL_Q["prompt"]


def test_flush_sets_honest_gap_note_and_clears_screen():
    reasoning = _DeckReasoning(adult=[ADULT_Q])
    game = _make_game(reasoning)
    _arm(game, GENERAL_Q)

    async def scenario():
        game.sk.set_mode("adult")
        game.flush_for_mode_switch(source="enter_adult")
        # Honest one-beat gap in the state block, BEFORE the new draw lands.
        assert any(
            "deck switch committed" in n and "adult deck" in n
            for n in game.sk.status_notes
        )
        await asyncio.sleep(0.05)
        for t in asyncio.all_tasks():
            if t is not asyncio.current_task():
                t.cancel()

    asyncio.run(scenario())

    # The old question came off the glass (metadata cleared).
    cleared = [
        json.loads(r.metadata) for r in game.ctx.api.room.requests
    ]
    assert any(doc["question"] == "" for doc in cleared)


def test_flush_cancels_inflight_prefetch_and_relaunches():
    # A slow general-deck draw is in flight when the mode flips: the flush
    # cancels it, relaunches immediately, and the adult question lands.
    reasoning = _DeckReasoning(general=[GENERAL_Q2], adult=[ADULT_Q],
                               delay=0.2)
    game = _make_game(reasoning)

    async def scenario():
        game.start_prefetch()  # general-deck draw, slow
        old_task = game._prefetch_task
        await asyncio.sleep(0)
        game.sk.set_mode("adult")
        game.flush_for_mode_switch(source="enter_adult")
        assert game._prefetch_task is not old_task  # relaunched NOW
        assert game._prefetch_stall_ticks == 0      # watchdog cooperation
        await asyncio.sleep(0.3)
        for t in asyncio.all_tasks():
            if t is not asyncio.current_task():
                t.cancel()

    asyncio.run(scenario())

    assert game.armed_question is not None
    assert game.armed_question["category"] == "adult_couples"


def test_inflight_old_deck_draw_discarded_by_commit_guard():
    # The cancel race: a draw past its last await commits AFTER the mode
    # flipped. The supply_mode commit guard discards it — a wrong-deck
    # question never lands in next_question.
    reasoning = _DeckReasoning(general=[GENERAL_Q2], delay=0.05)
    game = _make_game(reasoning)

    async def scenario():
        game.start_prefetch()  # drawing from the GENERAL deck
        await asyncio.sleep(0.01)
        game.sk.set_mode("adult")  # flip mid-flight, no flush: guard only
        await asyncio.sleep(0.1)   # let the old draw complete
        for t in asyncio.all_tasks():
            if t is not asyncio.current_task():
                t.cancel()

    asyncio.run(scenario())

    assert game.next_question is None
    assert game.armed_question is None


# ─── 2. adult reveal keyed exactly once; no unkeyed reveal path ──────────────

def test_adult_reveal_keyed_once_zero_suppressions(caplog):
    # The armed pipeline gives the adult reveal its q_N identity; because
    # only one dispatch is ever generated, the gate's suppression count
    # stays ZERO (dedup by construction, not by catching duplicates).
    game = _make_game()
    game.sk.set_mode("adult")
    game.sk.bind_speaker("S1", "Rami")
    _arm(game, ADULT_Q)
    now = time.time()
    game.sk.open_answer_window(duration=30.0, now=now)
    _final(game, "doggy style", "S1", now + 3)

    with caplog.at_level(logging.WARNING, logger="lily_agent"):
        asyncio.run(game.adjudicate(steal_allowed=False))
        # A second adjudicate on the consumed question is a structural
        # no-op — nothing armed, no second reveal generated.
        asyncio.run(game.adjudicate(steal_allowed=False))

    assert game.say_registry.state("q_1_reveal") is not None
    reveals = [i for i in game.session.instructions if "COMMITTED" in i]
    assert len(reveals) == 1
    assert not any(
        "LILY_SAY_SUPPRESSED" in r.message for r in caplog.records
    )
    # Exactly ONE reveal packet is pending (single-shot: consumed at TTS
    # start or playout completion, whichever fires first) — the second
    # adjudicate queued nothing on top of it.
    assert game._pending_reveal_event == {"correct": True, "winner": "Rami"}
    # And the reveal metadata carries the bank row's own category.
    doc = json.loads(game.ctx.api.room.requests[-1].metadata)
    assert doc["category"] == "adult_couples"
    assert doc["reveal"]["winner"] == "Rami"


def test_no_reveal_can_fire_without_armed_identity():
    # If nothing is armed there is no identity — adjudicate must generate
    # NOTHING (no reveal speech, no packet, no metadata): the freestyle
    # double-reveal class is structurally impossible from this path.
    game = _make_game()
    game.sk.set_mode("adult")
    game.sk.bind_speaker("S1", "Rami")
    assert game.armed_question is None

    asyncio.run(game.adjudicate(steal_allowed=True))

    assert game.session.instructions == []
    assert game._pending_reveal_event is None
    assert game.ctx.api.room.requests == []


# ─── 3. general never bleeds post-switch ─────────────────────────────────────

def test_general_never_bleeds_after_switch_across_questions():
    reasoning = _DeckReasoning(general=[GENERAL_Q2], adult=[ADULT_Q, ADULT_Q2])
    game = _make_game(reasoning)
    game.sk.bind_speaker("S1", "Rami")
    _arm(game, GENERAL_Q)
    game.next_question = dict(GENERAL_Q2)

    served: list[dict] = []

    async def scenario():
        game.sk.set_mode("adult")
        game.flush_for_mode_switch(source="enter_adult")
        await asyncio.sleep(0.05)
        served.append(dict(game.armed_question))
        # Play the question through the full pipeline: window -> reveal
        # (adjudicate arms the next prefetched question afterwards).
        now = time.time()
        game.sk.open_answer_window(duration=30.0, now=now)
        _final(game, "doggy style", "S1", now + 3)
        await game.adjudicate(steal_allowed=False)
        await asyncio.sleep(0.05)
        served.append(dict(game.armed_question))
        for t in asyncio.all_tasks():
            if t is not asyncio.current_task():
                t.cancel()

    asyncio.run(scenario())

    assert [q["id"] for q in served] == [ADULT_Q["id"], ADULT_Q2["id"]]
    for q in served:
        assert q["category"] in ADULT_CATEGORY_FAMILIES
        assert q["prompt"] not in (GENERAL_Q["prompt"], GENERAL_Q2["prompt"])
    # Every supply call after the switch drew in adult mode.
    assert all(c["mode"] == "adult" for c in reasoning.calls)


def test_flushed_general_question_stays_excluded_from_redraw():
    # F+G seam: the drawn-set registered the general question before the
    # flush; flushing must NOT un-exclude it — a question that touched the
    # wrong segment is never re-served this session.
    reasoning = _DeckReasoning(adult=[ADULT_Q])
    game = _make_game(reasoning)
    game._drawn_ids = set()
    game._drawn_hashes = set()
    assert game._register_draw(GENERAL_Q) is True  # drawn pre-switch
    _arm(game, GENERAL_Q)

    async def scenario():
        game.sk.set_mode("adult")
        game.flush_for_mode_switch(source="enter_adult")
        await asyncio.sleep(0.05)
        for t in asyncio.all_tasks():
            if t is not asyncio.current_task():
                t.cancel()

    asyncio.run(scenario())

    assert GENERAL_Q["id"] in game._drawn_ids  # still excluded
    assert game._register_draw(dict(GENERAL_Q)) is False


# ─── 4. back to normal flushes the adult armed question ─────────────────────

def test_back_to_normal_transcript_path_flushes_adult_and_rearms_general():
    # The deterministic transcript-event layer: "back to normal" flips the
    # sticky flag, flushes the armed ADULT question, and the general deck
    # re-arms — the adult question is never finished or revealed.
    reasoning = _DeckReasoning(general=[GENERAL_Q2], adult=[ADULT_Q2])
    game = _make_game(reasoning)
    game.sk.set_mode("adult")
    game.sk.bind_speaker("S1", "Rami")
    _arm(game, ADULT_Q)

    async def scenario():
        game.on_transcript_event(
            {"control_command": "back_to_normal", "player": "Rami",
             "candidate_recorded": False},
            "okay, back to normal please",
            speaker_label="S1",
        )
        # Committed in code, instantly.
        assert game.sk.mode == "general"
        assert game.armed_question is None
        assert game.sk.current_question is None
        await asyncio.sleep(0.05)
        for t in asyncio.all_tasks():
            if t is not asyncio.current_task():
                t.cancel()

    asyncio.run(scenario())

    assert game.armed_question is not None
    assert game.armed_question["id"] == GENERAL_Q2["id"]
    assert game.armed_question["category"] not in ADULT_CATEGORY_FAMILIES
    # The revert speech is honest about the re-draw and forbids finishing
    # the adult question.
    revert = [i for i in game.session.instructions if "back to normal" in i]
    assert revert and "re-drawing" in revert[0]


def test_back_to_normal_noop_outside_adult_mode():
    reasoning = _DeckReasoning(general=[GENERAL_Q2])
    game = _make_game(reasoning)
    _arm(game, GENERAL_Q)

    async def scenario():
        game.on_transcript_event(
            {"control_command": "back_to_normal", "player": None,
             "candidate_recorded": False},
            "back to normal",
            speaker_label="S1",
        )
        await asyncio.sleep(0.02)
        for t in asyncio.all_tasks():
            if t is not asyncio.current_task():
                t.cancel()

    asyncio.run(scenario())

    # General mode: nothing flushed, nothing spoken, question survives.
    assert game.armed_question["id"] == GENERAL_Q["id"]
    assert game.session.instructions == []


# ─── 5. category label follows the bank row ─────────────────────────────────

class _FakeQuery:
    def __init__(self, rows) -> None:
        self._rows = rows

    def select(self, *_a):
        return self

    def execute(self):
        rows = self._rows

        class _R:
            data = rows
        return _R()


class _FakeSupabase:
    def __init__(self, rows) -> None:
        self.query = _FakeQuery(rows)

    def table(self, _name):
        return self.query


_BANK_ROWS = [
    {"id": 1, "question": "general academic q", "canonical_answer": "x",
     "acceptable_answers": ["x"], "category": "academic",
     "difficulty_tier": 1, "status": "active", "adult": False},
    {"id": 2, "question": "adult couples q", "canonical_answer": "y",
     "acceptable_answers": ["y"], "category": "adult_couples",
     "difficulty_tier": 1, "status": "active", "adult": True},
    {"id": 3, "question": "adult kink q", "canonical_answer": "z",
     "acceptable_answers": ["z"], "category": "adult_kink",
     "difficulty_tier": 2, "status": "active", "adult": True},
]


def _fetch(category, mode, rows=_BANK_ROWS):
    return asyncio.new_event_loop().run_until_complete(
        lily_fetch_bank_question(_FakeSupabase(rows), category, 1, [],
                                 mode=mode)
    )


def test_adult_mode_bank_serves_only_adult_rows():
    # Even when the requested family matches a GENERAL row exactly, adult
    # mode never serves it — the adult deck is the only deck.
    q = _fetch("academic", mode="adult")
    assert q is not None
    assert q["id"] in ("kb_2", "kb_3")
    assert q["category"] in ADULT_CATEGORY_FAMILIES


def test_adult_row_keeps_its_own_category_label():
    # The row's own category survives serving — the requested round family
    # never overwrites it (the "academic category" announcement defect).
    q = _fetch("adult_kink", mode="adult")
    assert q is not None
    assert q["prompt"] == "adult kink q"
    assert q["category"] == "adult_kink"


def test_general_mode_still_excludes_adult_rows():
    # The pre-existing consent guard is untouched by the deck cut.
    q = _fetch("academic", mode="general")
    assert q is not None
    assert q["id"] == "kb_1"
    assert q["category"] == "academic"


def test_adult_deck_exhaustion_returns_none_never_general():
    rows = [r for r in _BANK_ROWS if not r["adult"]]
    assert _fetch("academic", mode="adult", rows=rows) is None


def test_category_rotation_is_mode_aware():
    game = _make_game()
    assert game._category_for_round(1) == "academic"
    game.sk.set_mode("adult")
    assert game._category_for_round(1) == "adult_couples"
    assert game._category_for_round(2) == "adult_kink"
    assert game._category_for_round(3) == "adult_couples"
    game.sk.set_mode("general")
    assert game._category_for_round(1) == "academic"


def test_state_block_labels_armed_adult_question_with_bank_category():
    game = _make_game()
    game.sk.set_mode("adult")
    _arm(game, ADULT_Q)
    block = game.build_state_block()
    assert "adult_couples" in block
    assert '"category": "academic"' not in block
