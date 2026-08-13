"""WO-LILY-HOTFIX-006 N2 — the custom round that was narrated and never built.

Live evidence, session lily-16A9AE (2026-08-08). The operator asked for a
Cape Cod round. Lily said:

    "I'm putting together a custom round all about Cape Cod for you right now"
    "I'm putting your round together right now."

The `lily_asked_history` rows for that session hold six questions — Gold,
Vatican City, Psycho, One Direction, rupee, Rehab — and NOT ONE of them is
about Cape Cod. Every one of those six is a curated bank row that ships in
migration 004 under `academic` / `pop_culture`. The Cape Cod questions the
players actually heard (the canal, Provincetown, the National Seashore,
Chatham) were conversational improvisation: never armed, never registered,
never scoreable. The engine ran a generic round underneath the fiction,
which is why "In Mumbai or Delhi, dinner gets paid for in this Indian
currency" and "Harry Styles first found fame in this X Factor boy band"
surfaced mid-round and the operator asked what that had to do with Cape Cod.

THE MECHANISM (traced, not guessed):

  lily_set_category("Cape Cod")
    -> _category_override[rnd] = "Cape Cod"        (round steered, honestly)
    -> returns "Round topic set ... I'm generating those questions now"
       IMMEDIATELY, before a single question exists
  _prefetch_inner
    -> _is_operator_category("Cape Cod") is True  -> prefer_bank
    -> lily_fetch_bank_question("Cape Cod", ...)
         stage 1 (category, tier)  -> empty
         stage 2 (category, None)  -> empty
         stage 3 (None, None)      -> ANY ACTIVE ROW  <-- the defect
    -> prefetch_question(from_bank=<generic row>) returns it verbatim;
       GENERATION NEVER RUNS
    -> arm_next_question serves it and writes an asked_history row with no
       category on it at all

So the "compounding arsenal" optimisation (CAPABILITY-RESTORE-001: an
operator topic prefers previously-banked questions for that topic) silently
turned "build me a Cape Cod round" into "serve me anything", and the
confirmation line was produced from nothing but the operator's own words.

THE CONTRACT N2 PINS: a category request has exactly two honest outcomes —
registered questions in that category, or a plain refusal. The confirmation
line has exactly ONE producer and it reads the registration result, the same
discipline as HOTFIX-005 X1 (score read from the committed ledger, never
narrated freely) and X3 (no reveal without a delivery that reached playout).
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_bank
import lily_config
import lily_reasoning
import lily_say_gate
from lily_agent import (
    LilyAgent,
    LilyGame,
    lily_custom_round_line,
    lily_narrated_custom_round_divergence,
)
from lily_persistence import lily_fetch_bank_question
from lily_scorekeeper import LilyScorekeeper


# ---------------------------------------------------------------------------
# The six rows the live session actually served. Verbatim from migration 004
# (004_lily_questions_expansion.sql) — the point of copying them exactly is
# that the fixture fails the way the session failed, not in some nearby way.
# ---------------------------------------------------------------------------

LIVE_GENERIC_ROWS = [
    {
        "id": 4001, "category": "academic", "difficulty_tier": 2,
        "question": ("In Mumbai or Delhi, dinner gets paid for in this "
                     "Indian currency."),
        "canonical_answer": "rupee", "acceptable_answers": ["rupee", "rupees"],
        "adult": False, "status": "active",
    },
    {
        "id": 4002, "category": "pop_culture", "difficulty_tier": 2,
        "question": ("Harry Styles first found fame in this X Factor boy "
                     "band."),
        "canonical_answer": "One Direction",
        "acceptable_answers": ["one direction", "1d"],
        "adult": False, "status": "active",
    },
    {
        "id": 4003, "category": "pop_culture", "difficulty_tier": 3,
        "question": ("A stolen envelope of cash, the Bates Motel, and one "
                     "infamous shower — name this 1960 Hitchcock thriller."),
        "canonical_answer": "Psycho", "acceptable_answers": ["psycho"],
        "adult": False, "status": "active",
    },
    {
        "id": 4004, "category": "pop_culture", "difficulty_tier": 2,
        "question": ("Amy Winehouse said no, no, no in this Grammy-winning "
                     "one-word hit."),
        "canonical_answer": "Rehab", "acceptable_answers": ["rehab"],
        "adult": False, "status": "active",
    },
]

CAPE_COD_BANK_ROW = {
    "id": 4900, "category": "Cape Cod", "difficulty_tier": 1,
    "question": "This canal cuts Cape Cod off from the Massachusetts mainland.",
    "canonical_answer": "the Cape Cod Canal",
    "acceptable_answers": ["cape cod canal", "the cape cod canal"],
    "adult": False, "status": "active",
}

CAPE_COD_GENERATED = [
    {
        "id": "q_cc1",
        "prompt": ("The tip of Cape Cod holds this town, where the Mayflower "
                   "first dropped anchor in 1620."),
        "canonical_answer": "Provincetown",
        "acceptable_answers": ["provincetown", "p-town"],
        "difficulty_tier": 1, "category": "potpourri", "reveal_color": "",
    },
    {
        "id": "q_cc2",
        "prompt": ("This president signed the Cape Cod National Seashore "
                   "into being in 1961."),
        "canonical_answer": "John F. Kennedy",
        "acceptable_answers": ["jfk", "kennedy", "john f kennedy"],
        "difficulty_tier": 2, "category": "potpourri", "reveal_color": "",
    },
]


# ---------------------------------------------------------------------------
# Fakes: a supabase double over the two tables the supply path touches, plus
# a reasoning node whose ONLY stubbed surface is the pair of provider calls.
# prefetch_question itself is the real method — the from_bank short-circuit
# that skipped generation in the live session has to be real machinery here
# or the fixture proves nothing.
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, db, table):
        self._db, self._table = db, table
        self._filters, self._op, self._payload = [], None, None
        self._limit, self._single = None, False

    def select(self, *a, **k):
        self._op = "select"
        return self

    def insert(self, payload):
        self._op, self._payload = "insert", payload
        return self

    def upsert(self, payload, on_conflict=None):
        self._op, self._payload = "upsert", payload
        return self

    def eq(self, col, val):
        self._filters.append((col, val))
        return self

    def limit(self, n):
        self._limit = n
        return self

    def order(self, *a, **k):
        return self

    def maybe_single(self):
        self._single = True
        return self

    def execute(self):
        rows = self._db.tables.setdefault(self._table, [])
        if self._op in ("insert", "upsert"):
            items = (self._payload if isinstance(self._payload, list)
                     else [self._payload])
            for item in items:
                if self._table in self._db.reject_columns:
                    bad = self._db.reject_columns[self._table] & set(item)
                    if bad:
                        raise Exception(
                            f"column \"{sorted(bad)[0]}\" of relation "
                            f"\"{self._table}\" does not exist"
                        )
                rows.append(dict(item))
            return _Result([dict(i) for i in items])
        matched = [
            r for r in rows
            if all(r.get(c) == v for c, v in self._filters)
        ]
        if self._limit is not None:
            matched = matched[: self._limit]
        if self._single:
            return _Result(matched[0] if matched else None)
        return _Result(matched)


class _Supabase:
    def __init__(self, questions=(), reject_columns=None):
        self.tables = {"lily_questions": [dict(q) for q in questions]}
        # Columns the schema does NOT have — used to prove migration-lag
        # tolerance on the asked-history category write.
        self.reject_columns = reject_columns or {}

    def table(self, name):
        return _Query(self, name)

    @property
    def asked_rows(self):
        return self.tables.get("lily_asked_history", [])


def _reasoning(generated=(), available=True):
    """A real LilyReasoning with only the two provider calls stubbed.

    `available=False` is the generation-unavailable case: generate_question
    returns None, exactly as it does when the key is missing, the provider is
    down, or the JSON comes back unparseable.
    """
    node = lily_reasoning.LilyReasoning.__new__(lily_reasoning.LilyReasoning)
    queue = [dict(q) for q in generated]
    node.generated_calls = []

    async def _generate_question(category, tier, mode, avoid, **kw):
        node.generated_calls.append(category)
        if not available or not queue:
            return None
        return queue.pop(0)

    async def _verify_question(question, mode="general"):
        return True, "ok"

    node.generate_question = _generate_question
    node.verify_question = _verify_question
    return node


class _Session:
    def __init__(self):
        self.instructions = []

    def generate_reply(self, instructions):
        self.instructions.append(instructions)


class _RoomAPI:
    async def update_room_metadata(self, req):
        return None


class _Ctx:
    def __init__(self):
        self.api = type("API", (), {"room": _RoomAPI()})()
        self.room = type("Room", (), {
            "name": "lily-16A9AE",
            "local_participant": type("P", (), {
                "attributes": {},
                "set_attributes": staticmethod(
                    lambda attrs: asyncio.sleep(0)
                ),
            })(),
        })()


def _make_game(reasoning=None, supabase=None, question_number=0):
    """Real LilyGame via __new__ (the harness test_adult_identity uses), wired
    with everything _prefetch_inner + arm_next_question actually touch."""
    game = LilyGame.bare()
    game.ctx = _Ctx()
    game.session = _Session()
    game.agent = type("A", (), {
        "set_preemptive_generation": staticmethod(lambda enabled: None)
    })()
    game._preemptive_paused = False
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game.sk = LilyScorekeeper("lily-16A9AE")
    game.sk.question_number = question_number
    game.rounds_total = 4
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
    game.group_id = "grp_cape_cod"
    game.promoted_categories = []
    game.prewager_standings = None
    game.highlights = []
    game.supabase = supabase
    game.reasoning = reasoning or _reasoning()
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
    game._category_override = {}
    game._drawn_ids = set()
    game._drawn_hashes = set()
    game._burned_question_ids = set()
    game._burned_question_hashes = set()
    game._delivered_to_playout = set()
    game._aired_stems = set()
    game._nbest_by_key = {}
    game._pre_window_segments = []
    game._prehook_answer_suppressions = set()
    game._undelivered_ticks = 0
    game._undelivered_refires = 0
    game._supply_stall_ticks = 0
    game._pending_delivery_qnum = None
    game._active_delivery_qnum = None
    game._active_delivery_started_at = None
    game._mc_delivery_qnum = None
    game._question_transitioning = False
    game._phase_hold = None
    return game


def _ask_for(game, topic, settle=0.0):
    """Drive the real tool the way the live session did.

    `settle` keeps the tool's own event loop alive a beat longer so the
    fire-and-forget legs (the arm's ledger write, the curate path's bank
    insert) land — they are ensure_future'd inside this loop, so a loop that
    closes the instant the tool returns silently drops them.
    """
    agent = LilyAgent.__new__(LilyAgent)
    agent._game = game

    async def _drive():
        out = await LilyAgent.lily_set_category.__wrapped__(agent, None, topic)
        if settle:
            await asyncio.sleep(settle)
        for t in asyncio.all_tasks():
            if t is not asyncio.current_task():
                t.cancel()
        return out

    return asyncio.run(_drive())


def _prompts_served(game):
    return [row for row in game.used_prompts]


# ---------------------------------------------------------------------------
# 1. THE CAPE COD FIXTURE — it must not be able to reproduce.
# ---------------------------------------------------------------------------


def test_cape_cod_request_never_serves_a_generic_bank_row():
    """THE fixture. The bank holds exactly what production held that night:
    the live generic rows and nothing about Cape Cod. The any-category bank
    fallback made "build me a Cape Cod round" resolve to the rupee question.
    """
    sb = _Supabase(questions=LIVE_GENERIC_ROWS)
    game = _make_game(_reasoning(CAPE_COD_GENERATED), sb)
    _ask_for(game, "Cape Cod")

    served = game.armed_question or game.next_question
    assert served is not None, "a built round must have a question in hand"
    live_generic = {r["question"] for r in LIVE_GENERIC_ROWS}
    assert served["prompt"] not in live_generic, (
        "a Cape Cod request resolved to a generic bank row — the exact "
        f"live defect: {served['prompt']!r}"
    )
    assert served["category"] == "Cape Cod"


def test_the_asked_ledger_records_the_category_of_the_served_question():
    """The evidence that convicted the session was the LEDGER: six rows, not
    one of them Cape Cod. A row that does not carry its category cannot be
    audited that way at all — so the category is written with the row."""
    sb = _Supabase(questions=LIVE_GENERIC_ROWS)
    game = _make_game(_reasoning(CAPE_COD_GENERATED), sb)
    _ask_for(game, "Cape Cod", settle=0.05)

    assert game.asked_history, "nothing was registered"
    assert game.asked_history[-1]["category"] == "Cape Cod"
    # The durable row is written when the question goes to AIR, not at arm
    # (2026-08-09, barge-in WO): a question the table never heard must not
    # be spent forever. N2's contract is about WHAT the row carries, which
    # is unchanged — so drive the question to air and read the row. The
    # write is ensure_future'd, so it needs a loop alive past the call.
    async def _air():
        game.record_question_asked(reason="test_air")
        await asyncio.sleep(0.05)

    asyncio.run(_air())
    assert sb.asked_rows, "no lily_asked_history row was written"
    assert sb.asked_rows[-1]["category"] == "Cape Cod"


def test_registration_happens_before_the_question_is_spoken():
    """"Registered questions in that category" is a claim about the ledger,
    and the ledger has to be true BEFORE the stem goes to air — the live
    round was narrated first and never registered at all."""
    sb = _Supabase(questions=LIVE_GENERIC_ROWS)
    game = _make_game(_reasoning(CAPE_COD_GENERATED), sb)
    _ask_for(game, "Cape Cod", settle=0.05)

    assert game.asked_history[-1]["category"] == "Cape Cod"
    # Nothing has been delivered yet: no delivery claim, no playout record.
    key = f"q_{game.sk.question_number}_delivery"
    assert game.say_registry.state(key) is None
    assert game.sk.question_number not in game._delivered_to_playout


# ---------------------------------------------------------------------------
# 2. Acceptance fixture — generation AVAILABLE.
# ---------------------------------------------------------------------------


def test_a_built_round_confirms_and_names_the_subject():
    sb = _Supabase(questions=LIVE_GENERIC_ROWS)
    game = _make_game(_reasoning(CAPE_COD_GENERATED), sb)
    msg = _ask_for(game, "Cape Cod")
    assert "Cape Cod" in msg
    assert "can't build" not in msg.lower()


def test_the_request_routes_to_the_generation_lane():
    """Requirement 1: the request reaches generate_question with the subject
    the table named — it is not quietly answered out of the deck."""
    sb = _Supabase(questions=LIVE_GENERIC_ROWS)
    node = _reasoning(CAPE_COD_GENERATED)
    game = _make_game(node, sb)
    _ask_for(game, "Cape Cod")
    assert node.generated_calls == ["Cape Cod"]


def test_the_built_question_is_banked_under_the_requested_category():
    """The compounding arsenal only compounds if the round's questions bank
    under the topic. They used to bank under the generator's own label (or
    'potpourri'), so a second Cape Cod request could never find them."""
    sb = _Supabase(questions=LIVE_GENERIC_ROWS)
    game = _make_game(_reasoning(CAPE_COD_GENERATED), sb)
    _ask_for(game, "Cape Cod", settle=0.05)
    banked = [
        r for r in sb.tables["lily_questions"] if r.get("source") == "generated"
    ]
    assert banked and banked[0]["category"] == "Cape Cod"


def test_a_promoted_proposal_cannot_relabel_a_requested_topic():
    """The defect from the other side: the curate path relabels a generated
    question when the generator proposes a category and that category is
    already promoted. A question built FOR the Cape Cod round arriving
    labelled 'geography' fails its registration check, and a round she
    really did build collapses into a refusal. The table's word wins."""
    generated = [dict(CAPE_COD_GENERATED[0], proposed_category="geography")]
    sb = _Supabase(questions=LIVE_GENERIC_ROWS)
    game = _make_game(_reasoning(generated), sb)
    game.promoted_categories = ["geography"]
    msg = _ask_for(game, "Cape Cod")
    served = game.armed_question or game.next_question
    assert served["category"] == "Cape Cod"
    assert game.custom_round_registrations("Cape Cod") == ["q_cc1"]
    assert "can't build" not in msg.lower()


def test_a_banked_topic_is_served_from_the_bank_without_regenerating():
    """PROTECTED (CAPABILITY-RESTORE-001): an operator topic that already has
    banked questions prefers them. Strictness must not break the arsenal."""
    sb = _Supabase(questions=LIVE_GENERIC_ROWS + [CAPE_COD_BANK_ROW])
    node = _reasoning(CAPE_COD_GENERATED)
    game = _make_game(node, sb)
    _ask_for(game, "Cape Cod")
    served = game.armed_question or game.next_question
    assert served["prompt"] == CAPE_COD_BANK_ROW["question"]
    assert node.generated_calls == [], "the bank had it; nothing to generate"


# ---------------------------------------------------------------------------
# 3. Acceptance fixture — generation UNAVAILABLE.
# ---------------------------------------------------------------------------


def test_generation_unavailable_is_a_plain_refusal_with_a_real_offer():
    sb = _Supabase(questions=LIVE_GENERIC_ROWS)
    game = _make_game(_reasoning(available=False), sb)
    msg = _ask_for(game, "Cape Cod")
    lowered = msg.lower()
    assert "can't build a custom cape cod round right now" in lowered
    assert "deck" in lowered, "a refusal has to offer what she CAN do"


def test_a_refused_round_fabricates_nothing():
    """Zero fabricated questions, zero improvised stems: nothing armed under
    the topic, nothing added to the served-prompt list, nothing in the
    ledger."""
    sb = _Supabase(questions=LIVE_GENERIC_ROWS)
    game = _make_game(_reasoning(available=False), sb)
    _ask_for(game, "Cape Cod")

    served = game.armed_question or game.next_question
    assert served is None or served.get("category") != "Cape Cod"
    assert not [p for p in _prompts_served(game)]
    assert not [r for r in game.asked_history if r.get("category") == "Cape Cod"]
    assert sb.asked_rows == []


def test_a_refused_round_leaves_no_standing_override():
    """The round must not keep running under a topic she just refused — the
    override is rolled back so the fixed rotation takes it honestly."""
    sb = _Supabase(questions=LIVE_GENERIC_ROWS)
    game = _make_game(_reasoning(available=False), sb)
    _ask_for(game, "Cape Cod")
    assert game._is_operator_category("Cape Cod") is False
    assert "Cape Cod" not in game._category_override.values()


def test_generation_failure_does_not_fall_back_to_a_generic_question():
    """The insurance bank draw is the other door the generic round came
    through: generation fails, the bank serves anything, and the fiction
    survives. For an operator topic it must serve that topic or nothing."""
    sb = _Supabase(questions=LIVE_GENERIC_ROWS)
    game = _make_game(_reasoning(available=False), sb)
    _ask_for(game, "Cape Cod")
    served = game.armed_question or game.next_question
    live_generic = {r["question"] for r in LIVE_GENERIC_ROWS}
    assert served is None or served["prompt"] not in live_generic


# ---------------------------------------------------------------------------
# 4. The confirmation line has exactly one producer.
# ---------------------------------------------------------------------------


def test_the_confirmation_line_is_unproducible_without_a_registration():
    """HOTFIX-005 X1 discipline: the spoken number comes off the ledger, not
    out of the conversation. Here the spoken ROUND comes off the registration
    result — an empty result can only produce the refusal."""
    empty = {"category": "Cape Cod", "registered": []}
    assert "can't build" in lily_custom_round_line(empty).lower()
    built = {"category": "Cape Cod", "registered": ["q_cc1"]}
    assert "can't build" not in lily_custom_round_line(built).lower()
    assert "Cape Cod" in lily_custom_round_line(built)
    # A malformed / absent result is a refusal, never a confirmation.
    assert "can't build" in lily_custom_round_line(None).lower()
    assert "can't build" in lily_custom_round_line({}).lower()


def test_the_state_block_denies_a_round_that_is_not_in_the_ledger():
    """The tool is not the only channel: she can also just talk. The state
    block carries the grounded read so free narration has the truth in front
    of it (the same place _score_authority_line lives)."""
    game = _make_game(_reasoning(available=False), _Supabase())
    game._category_override = {1: "Cape Cod"}
    line = game.custom_round_state_line()
    assert line is not None
    assert "'Cape Cod' round 1: NOT BUILT" in line
    assert "zero registered questions" in line


def test_the_state_block_confirms_a_round_that_is_in_the_ledger():
    sb = _Supabase(questions=LIVE_GENERIC_ROWS)
    game = _make_game(_reasoning(CAPE_COD_GENERATED), sb)
    _ask_for(game, "Cape Cod")
    line = game.custom_round_state_line()
    assert "'Cape Cod' round 1: BUILT" in line
    assert "'Cape Cod' round 1: NOT BUILT" not in line


def test_the_supply_stall_fallback_never_fills_a_named_round_with_a_stranger():
    """The third door into the generic round. WS-6 arms a bank question
    straight into a starving game, and for the fixed families "anything" is
    the right answer — but the live complaint was a specific one: "what does
    that have to do with Cape Cod?". Inside a named round the fallback must
    never serve a stranger.

    LIVEFIRE-001 CLASS 6 (6b/6c): a dry bank is a SUPPLY DEFECT, not an end
    state — so the fallback now KEEPS GENERATING for the named topic before
    it would ever release the name. Here generation still has Cape Cod
    questions, so the round keeps its name and serves an on-topic question;
    no stranger, and (unlike the old behavior) no unnecessary flip. The
    generation-genuinely-fails path (flip, announced) is covered by
    tests/test_livefire_class6_named_category.py."""
    sb = _Supabase(questions=LIVE_GENERIC_ROWS)
    game = _make_game(_reasoning(CAPE_COD_GENERATED), sb)
    _ask_for(game, "Cape Cod")           # round 1 is genuinely built
    game.next_question = None            # ...and then supply starves
    game.armed_question = None
    game.sk.answer_window_open = False

    async def _drive():
        out = await game.arm_supply_fallback()
        for t in asyncio.all_tasks():
            if t is not asyncio.current_task():
                t.cancel()
        return out

    asyncio.run(_drive())
    # The named topic kept generating: the round keeps its name and the
    # armed question is an on-topic Cape Cod question, never a generic
    # stranger from the deck.
    assert "Cape Cod" in game._category_override.values()
    armed = game.armed_question
    assert armed is not None
    assert armed["id"] in {"q_cc1", "q_cc2"}
    generic_ids = {q["id"] for q in LIVE_GENERIC_ROWS}
    assert armed["id"] not in generic_ids


def test_the_two_live_sentences_are_flagged_as_divergences():
    """The X1 safety net, aimed at the two sentences that actually aired.
    The state block and the tool result prevent; this makes a prevention
    failure loud in the session it happens in instead of days later in a
    ledger query."""
    unbuilt = ["Cape Cod"]
    assert lily_narrated_custom_round_divergence(
        "I'm putting together a custom round all about Cape Cod for you "
        "right now", unbuilt,
    ) == "Cape Cod"
    assert lily_narrated_custom_round_divergence(
        "Alright — I'm putting your Cape Cod round together right now.",
        unbuilt,
    ) == "Cape Cod"


def test_the_divergence_net_does_not_fire_on_ordinary_talk():
    """She is free to TALK about Cape Cod; only a round/questions claim over
    an empty ledger is the offence. A detector that cried wolf on chat would
    be turned off inside a week."""
    unbuilt = ["Cape Cod"]
    assert lily_narrated_custom_round_divergence(
        "I love Cape Cod in the autumn.", unbuilt,
    ) is None
    assert lily_narrated_custom_round_divergence(
        "Here's your next question.", unbuilt,
    ) is None
    # The refusal itself names the topic and the word 'round' — but a
    # refused topic is only unbuilt AFTER the refusal, so check the built
    # case: a real round may be spoken about freely.
    assert lily_narrated_custom_round_divergence(
        "Your Cape Cod round is ready!", [],
    ) is None


def test_a_refused_topic_stays_watched_for_the_rest_of_the_session():
    """The second live sentence landed after the round had already failed to
    appear. A refusal rolls the override back, so without the refused list
    the topic would drop out of every honesty read at exactly the moment it
    most needed watching."""
    sb = _Supabase(questions=LIVE_GENERIC_ROWS)
    game = _make_game(_reasoning(available=False), sb)
    _ask_for(game, "Cape Cod")
    assert game.custom_round_unbuilt_topics() == ["Cape Cod"]
    assert lily_narrated_custom_round_divergence(
        "I'm putting your round together right now — Cape Cod, coming up.",
        game.custom_round_unbuilt_topics(),
    ) == "Cape Cod"
    line = game.custom_round_state_line()
    assert "'Cape Cod': NOT BUILT" in line


def test_a_built_topic_is_not_watched():
    sb = _Supabase(questions=LIVE_GENERIC_ROWS)
    game = _make_game(_reasoning(CAPE_COD_GENERATED), sb)
    _ask_for(game, "Cape Cod")
    assert game.custom_round_unbuilt_topics() == []


# ---------------------------------------------------------------------------
# 5. Protected behaviour — strictness is scoped to operator topics only.
# ---------------------------------------------------------------------------


def test_the_fixed_rotation_still_gets_the_any_row_insurance_draw():
    """PROTECTED. The any-category bank fallback is the anti-starvation
    policy for the FIXED families (WS-6). N2 removes it only for a topic the
    table named, where "anything" is a lie rather than a fallback."""
    sb = _Supabase(questions=LIVE_GENERIC_ROWS)
    row = asyncio.run(lily_fetch_bank_question(
        sb, "no_such_family", 2, [], mode="general",
    ))
    assert row is not None, "the family rotation must never starve"

    strict = asyncio.run(lily_fetch_bank_question(
        sb, "Cape Cod", 2, [], mode="general", strict_category=True,
    ))
    assert strict is None, "a Cape Cod draw may only return Cape Cod"


def test_strict_draw_still_crosses_difficulty_tiers_within_the_topic():
    """Strictness is about the CATEGORY. A Cape Cod question at the wrong
    tier is still a Cape Cod question."""
    sb = _Supabase(questions=[CAPE_COD_BANK_ROW])  # tier 1
    row = asyncio.run(lily_fetch_bank_question(
        sb, "Cape Cod", 3, [], mode="general", strict_category=True,
    ))
    assert row is not None and row["category"] == "Cape Cod"


def test_asked_history_category_survives_a_pre_migration_schema():
    """Migration-lag tolerance (the lily_register_operator_category pattern):
    a database that has not taken migration 023 yet must still get its
    asked-history row — the category is dropped, the registration is not."""
    sb = _Supabase(reject_columns={"lily_asked_history": {"category"}})
    asyncio.run(lily_bank.lily_record_asked(
        sb, "grp", {"id": "q_cc1", "prompt": "p", "canonical_answer": "a",
                    "category": "Cape Cod"}, "sess",
    ))
    assert len(sb.asked_rows) == 1
    assert "category" not in sb.asked_rows[0]


def test_adult_mode_still_redirects_instead_of_building():
    """PROTECTED (deck-identity firewall): a custom label must never ride an
    adult question. Unchanged by N2 — redirect, never a flat denial."""
    game = _make_game(_reasoning(CAPE_COD_GENERATED), _Supabase())
    game.sk.mode = "adult"
    msg = _ask_for(game, "Cape Cod")
    assert "back to normal" in msg.lower()
    assert game._category_override == {}


def test_the_build_budget_is_bounded_and_configurable():
    """A blocking build is the price of an honest confirmation; it must be
    bounded so a dead provider cannot hold the table in silence."""
    assert lily_config.custom_round_build_seconds() > 0
