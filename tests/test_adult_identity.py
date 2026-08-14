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
        self.said: list[str] = []

    def generate_reply(self, instructions: str) -> None:
        self.instructions.append(instructions)

    def say(self, text, *a, **k):
        # REFACTOR W2a: deterministic direct_say lane (the verdict beat).
        self.said.append(text)
        return None


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
        self.calls.append({**kw})
        if self.delay:
            await asyncio.sleep(self.delay)
        # Unified adult deck: serve the adult queue (fall back to general
        # only if no adult questions were staged).
        deck = self.adult if self.adult else self.general
        return deck.pop(0) if deck else None

    async def prefetch_picture_question(self, supabase, **kw):
        return None


def _make_game(reasoning=None) -> LilyGame:
    game = LilyGame.bare()
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


# ─── 2. adult reveal keyed exactly once; no unkeyed reveal path ──────────────

def test_adult_reveal_keyed_once_zero_suppressions(caplog):
    # The armed pipeline gives the adult reveal its q_N identity; because
    # only one dispatch is ever generated, the gate's suppression count
    # stays ZERO (dedup by construction, not by catching duplicates).
    game = _make_game()
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
    # REFACTOR W2a: the verdict/reveal beat airs once as the deterministic
    # sheet on the direct_say lane; the second adjudicate is a structural
    # no-op and adds nothing.
    assert len(game.session.said) == 1
    assert not any(
        "LILY_SAY_SUPPRESSED" in r.message for r in caplog.records
    )
    # Exactly ONE reveal packet is pending (single-shot: consumed at TTS
    # start or playout completion, whichever fires first) — the second
    # adjudicate queued nothing on top of it.
    assert game._pending_reveal_event == {
        "correct": True,
        "winner": "Rami",
        # Score truth on the wire (08-04 screen fix): the beat carries
        # the committed score.
        "winner_score": 1,
    }
    # And the reveal metadata carries the bank row's own category.
    doc = json.loads(game.ctx.api.room.requests[-1].metadata)
    assert doc["category"] == "adult_couples"
    assert doc["reveal"]["winner"] == "Rami"


def test_no_reveal_can_fire_without_armed_identity():
    # If nothing is armed there is no identity — adjudicate must generate
    # NOTHING (no reveal speech, no packet, no metadata): the freestyle
    # double-reveal class is structurally impossible from this path.
    game = _make_game()
    game.sk.bind_speaker("S1", "Rami")
    assert game.armed_question is None

    asyncio.run(game.adjudicate(steal_allowed=True))

    assert game.session.instructions == []
    assert game._pending_reveal_event is None
    assert game.ctx.api.room.requests == []


# ─── 5. category label follows the bank row ─────────────────────────────────

class _FakeQuery:
    def __init__(self, rows) -> None:
        self._rows = rows
        self._filters = []
        self._limit = None

    def select(self, *_a, **_k):
        self._filters = []
        self._limit = None
        return self

    def eq(self, column, value):
        self._filters.append(lambda row: row.get(column) == value)
        return self

    def limit(self, value):
        self._limit = value
        return self

    def execute(self):
        rows = [
            row for row in self._rows
            if all(predicate(row) for predicate in self._filters)
        ]
        if self._limit is not None:
            rows = rows[:self._limit]

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


def _fetch(category, rows=_BANK_ROWS):
    return asyncio.new_event_loop().run_until_complete(
        lily_fetch_bank_question(_FakeSupabase(rows), category, 1, [])
    )


def test_adult_mode_bank_serves_only_adult_rows():
    # Even when the requested family matches a GENERAL row exactly, adult
    # mode never serves it — the adult deck is the only deck.
    q = _fetch("academic")
    assert q is not None
    assert q["id"] in ("kb_2", "kb_3")
    assert q["category"] in ADULT_CATEGORY_FAMILIES


def test_adult_row_keeps_its_own_category_label():
    # The row's own category survives serving — the requested round family
    # never overwrites it (the "academic category" announcement defect).
    q = _fetch("adult_kink")
    assert q is not None
    assert q["prompt"] == "adult kink q"
    assert q["category"] == "adult_kink"


def test_adult_deck_exhaustion_returns_none_never_general():
    rows = [r for r in _BANK_ROWS if not r["adult"]]
    assert _fetch("academic", rows=rows) is None


def test_state_block_labels_armed_adult_question_with_bank_category():
    game = _make_game()
    _arm(game, ADULT_Q)
    block = game.build_state_block()
    assert "adult_couples" in block
    assert '"category": "academic"' not in block
