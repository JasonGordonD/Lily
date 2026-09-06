"""WO-LILY-SUPPLY-001 S2 — the background question author.

Operator ruling: "the bank serves, the author replenishes. Live question
authoring never sits on the delivery path again."

These tests check the five things that decide whether that ruling actually
holds in code, and each of them is a way the previous shape of this
problem went wrong somewhere in this tree already:

  * a watermark crossing replenishes THAT LANE and nothing else (the
    arsenal's own per-partition independence — refilling suggestive must
    never mask explicit going dry);
  * an authoring failure retries with real backoff, is counted, and never
    raises into the caller (the delivery path must not be able to notice);
  * a near-duplicate is rejected before it is banked (ARSENAL-SEED A5 —
    the in-session picture replenisher once hashed every entry to the same
    value and banked exactly one row per partition, undetected);
  * a moderation refusal is a counted, logged outcome rather than an error
    (the arsenal's A9 rule, applied to text);
  * the row that lands is status='ready' with a lane, a sha256 and a
    replenished_at stamp — the seam S1's draw reads.

Writers are INSERT-tested against a fake client (fleet S3): no test here
asserts on source text, and every write goes through the production
function that will do it live.
"""

import asyncio
import itertools
import logging
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_bank_replenish as bank
import lily_config


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# A fake supabase client with the two behaviours this design leans on:
# the partial unique index on (lane) WHERE status='running', and the unique
# index on question_text_sha256. A fake that silently accepted both would
# let these tests pass while the real guarantees went untested.
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, data, count=None):
        self.data = data
        self.count = count


class _Query:
    def __init__(self, db, table):
        self._db = db
        self._table = table
        self._filters = []
        self._op = None
        self._payload = None
        self._limit = None
        self._order = None
        self._desc = False

    def select(self, _cols="*", count=None):
        self._op = "select"
        return self

    def insert(self, row):
        self._op = "insert"
        self._payload = row
        return self

    def update(self, patch):
        self._op = "update"
        self._payload = patch
        return self

    def eq(self, col, val):
        self._filters.append((col, val))
        return self

    def limit(self, n):
        self._limit = n
        return self

    def order(self, col, desc=False):
        self._order = col
        self._desc = desc
        return self

    def _matches(self, row):
        return all(row.get(c) == v for c, v in self._filters)

    def execute(self):
        rows = self._db.tables.setdefault(self._table, [])
        if self._op == "insert":
            items = (
                self._payload if isinstance(self._payload, list)
                else [self._payload]
            )
            created = []
            for item in items:
                row = dict(item)
                row.setdefault("id", str(uuid.uuid4()))
                self._db.apply_defaults(self._table, row)
                self._db.enforce_constraints(self._table, row)
                rows.append(row)
                created.append(row)
            return _Result(created)
        matched = [r for r in rows if self._matches(r)]
        if self._op == "update":
            for row in matched:
                for key, value in (self._payload or {}).items():
                    row[key] = self._db.now if value == "now()" else value
            return _Result(list(matched))
        if self._order:
            matched = sorted(
                matched, key=lambda r: str(r.get(self._order) or ""),
                reverse=self._desc,
            )
        if self._limit is not None:
            matched = matched[: self._limit]
        return _Result(list(matched))


class FakeBankDB:
    def __init__(self):
        self.tables = {}
        self.now = "2026-09-06T12:00:00+00:00"
        self.clock = itertools.count(1)

    def table(self, name):
        return _Query(self, name)

    def apply_defaults(self, table, row):
        import datetime

        if table == bank.RUNS_TABLE:
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            row.setdefault("started_at", now)
            row.setdefault("heartbeat_at", now)
            for counter in (
                "authored_count", "accepted_count", "skipped_duplicate",
                "rejected_verify", "rejected_moderation", "error_count",
                "prompt_tokens", "completion_tokens", "cost_tokens",
            ):
                row.setdefault(counter, 0)
        if table == bank.QUESTIONS_TABLE:
            row.setdefault("status", "active")
            row.setdefault("adult", False)
            row.setdefault("mode", "general")
            if row.get("replenished_at") == "now()":
                row["replenished_at"] = self.now

    def enforce_constraints(self, table, row):
        existing = self.tables.setdefault(table, [])
        if table == bank.RUNS_TABLE and row.get("status") == "running":
            for other in existing:
                if (
                    other.get("lane") == row.get("lane")
                    and other.get("status") == "running"
                ):
                    raise Exception(
                        "duplicate key value violates unique constraint "
                        '"lily_bank_replenish_runs_one_active_idx"'
                    )
        if table == bank.QUESTIONS_TABLE and row.get("question_text_sha256"):
            for other in existing:
                if other.get("question_text_sha256") == row["question_text_sha256"]:
                    raise Exception(
                        "duplicate key value violates unique constraint "
                        '"lily_questions_sha256_unique_idx"'
                    )


def seed_bank_row(db, lane, question, **overrides):
    """One servable bank row in `lane`, shaped like the live table."""
    fields = bank.lily_lane_row_fields(lane)
    row = {
        "mode": fields["mode"],
        "adult": fields["adult"],
        "category": fields["category"],
        "question": question,
        "canonical_answer": overrides.pop("canonical_answer", "an answer"),
        "acceptable_answers": ["an answer"],
        "difficulty_tier": 2,
        "status": overrides.pop("status", "active"),
        "source": "seed_v1",
    }
    row.update(overrides)
    db.table(bank.QUESTIONS_TABLE).insert(row).execute()
    return row


def fill_lane(db, lane, n, *, prefix="seeded question number"):
    for i in range(n):
        seed_bank_row(db, lane, f"{prefix} {i} in {lane}")


def make_author(questions, *, fail_with=None, fail_times=0, delay=0.0):
    """An author fake: yields `questions` in order, optionally raising
    `fail_with` for the first `fail_times` calls."""
    state = {"calls": 0, "failures": 0, "lanes": []}
    pool = list(questions)

    async def _author(lane, run_id=None):
        state["calls"] += 1
        state["lanes"].append(lane)
        if delay:
            await asyncio.sleep(delay)
        if fail_with is not None and state["failures"] < fail_times:
            state["failures"] += 1
            raise fail_with
        if not pool:
            return None
        q = dict(pool.pop(0))
        q.setdefault("difficulty_tier", 2)
        return q

    _author.state = state
    return _author


async def _always_verify(question, run_id=None):
    return True, "verified"


def q(prompt, answer="an answer"):
    return {"prompt": prompt, "canonical_answer": answer,
            "acceptable_answers": [answer]}


# ---------------------------------------------------------------------------
# The watermark: 40% consumed, per lane, independently.
# ---------------------------------------------------------------------------


def test_consumed_pct_is_the_shortfall_against_target():
    assert bank.lily_bank_consumed_pct(40, 40) == 0.0
    assert bank.lily_bank_consumed_pct(24, 40) == 0.4
    assert bank.lily_bank_consumed_pct(0, 40) == 1.0
    # A lane deeper than target is not "negatively consumed".
    assert bank.lily_bank_consumed_pct(60, 40) == 0.0


def test_watermark_fires_at_forty_percent_consumed():
    """The arsenal's rule, unchanged: 40% of depth gone fires the refill."""
    assert bank.lily_bank_should_replenish(24, target=40, ratio=0.40) is True
    assert bank.lily_bank_should_replenish(25, target=40, ratio=0.40) is False


def test_watermark_scales_with_configured_depth():
    """Stated as a ratio so a depth change cannot silently move the trigger
    to 80%-empty — the exact bug lily_replenish_threshold exists to stop."""
    assert bank.lily_bank_should_replenish(3, target=5, ratio=0.40) is True
    assert bank.lily_bank_should_replenish(4, target=5, ratio=0.40) is False


def test_the_watermark_is_shortfall_only_and_says_so():
    """The arsenal's serves-this-session limb is NOT implemented, because
    a background process has no session's draw counts in front of it. The
    limitation is stated in the docstring and there is no parameter for
    it — a dead argument would read as a feature."""
    import inspect

    params = set(inspect.signature(bank.lily_bank_should_replenish).parameters)
    assert "consumed" not in params
    assert "shortfall" in (bank.lily_bank_should_replenish.__doc__ or "").lower()


def test_watermark_reads_the_lane_and_emits_its_receipt(caplog):
    db = FakeBankDB()
    lane = "general:academic"
    fill_lane(db, lane, 4)
    with caplog.at_level(logging.INFO, logger="lily_bank_replenish"):
        mark = _run(bank.lily_bank_watermark(db, lane=lane, target=10))
    assert mark["ready"] == 4 and mark["target"] == 10
    assert mark["consumed_pct"] == 0.6
    assert mark["below_watermark"] is True
    line = [r for r in caplog.messages if "WATERMARK" in r]
    assert line, "the WATERMARK receipt must be emitted"
    assert "lane=general:academic" in line[0]
    assert "ready=4" in line[0] and "target=10" in line[0]
    assert "consumed_pct=" in line[0]


def test_ready_and_active_rows_both_count_as_servable():
    """S1's draw accepts both; the watermark must measure the same pool, or
    a lane the author just filled would read empty and be filled again."""
    db = FakeBankDB()
    lane = "general:wordplay"
    fill_lane(db, lane, 3)
    fill_lane(db, lane, 2, prefix="ready row")
    for row in db.tables[bank.QUESTIONS_TABLE][3:]:
        row["status"] = "ready"
    depth = _run(bank.lily_lane_depth(db, lane=lane))
    assert depth["active"] == 3 and depth["ready"] == 2
    assert depth["servable"] == 5


def test_a_lane_crossing_the_watermark_replenishes_that_lane_only():
    """Per-lane independence. academic is starved, wordplay is stocked; the
    sweep must open exactly one run and author only for academic."""
    db = FakeBankDB()
    starved = "general:academic"
    stocked = "general:wordplay"
    fill_lane(db, stocked, lily_config.bank_target_depth(stocked))
    fill_lane(db, starved, 1)
    author = make_author([q(f"a fresh academic question {i}") for i in range(5)])
    results = _run(bank.lily_replenish_sweep(
        db, author=author, verify=_always_verify,
        lanes=[starved, stocked],
    ))
    assert [r["lane"] for r in results] == [starved]
    assert set(author.state["lanes"]) == {starved}
    runs = db.tables.get(bank.RUNS_TABLE, [])
    assert [r["lane"] for r in runs] == [starved]


def test_a_stocked_lane_opens_no_run_at_all():
    db = FakeBankDB()
    lane = "adult:adult_kink"
    fill_lane(db, lane, 40)
    author = make_author([q("never authored")])
    results = _run(bank.lily_replenish_sweep(
        db, author=author, verify=_always_verify, lanes=[lane],
    ))
    assert results == []
    assert author.state["calls"] == 0
    assert db.tables.get(bank.RUNS_TABLE, []) == []


# ---------------------------------------------------------------------------
# Failure: backoff, counted, never raised.
# ---------------------------------------------------------------------------


def test_author_failure_retries_with_exponential_backoff():
    """base * 2^(N-1) — a failing provider costs the bank a slow retry."""
    assert bank.lily_backoff_seconds(1, base=5.0) == 5.0
    assert bank.lily_backoff_seconds(2, base=5.0) == 10.0
    assert bank.lily_backoff_seconds(3, base=5.0) == 20.0


def test_author_failure_sleeps_between_attempts_and_never_raises(caplog):
    db = FakeBankDB()
    lane = "general:academic"
    fill_lane(db, lane, 1)
    slept = []

    async def _sleep(seconds):
        slept.append(seconds)

    author = make_author(
        [q("a question that finally arrives")],
        fail_with=RuntimeError("xAI stream broke mid-flight"),
        fail_times=2,
    )
    with caplog.at_level(logging.WARNING, logger="lily_bank_replenish"):
        summary = _run(bank.lily_replenish_lane(
            db, lane=lane, author=author, verify=_always_verify,
            target=3, max_new=1, sleep=_sleep,
        ))
    # Two failures, two backed-off waits, then success — and no exception
    # ever reached the caller.
    assert slept == [5.0, 10.0]
    assert summary["accepted"] == 1
    assert summary["errors"] == 2
    assert any("REPLENISH_FAILED" in m for m in caplog.messages)


def test_exhausted_attempts_leave_the_lane_for_the_next_sweep():
    db = FakeBankDB()
    lane = "general:academic"

    async def _sleep(_s):
        return None

    author = make_author(
        [], fail_with=RuntimeError("still broken"), fail_times=99
    )
    summary = _run(bank.lily_replenish_lane(
        db, lane=lane, author=author, verify=_always_verify,
        target=2, max_new=1, sleep=_sleep,
    ))
    assert summary["accepted"] == 0
    assert summary["errors"] == 3  # bank_replenish_max_attempts default
    assert db.tables.get(bank.QUESTIONS_TABLE, []) == []


def test_provider_unavailable_stops_the_lane_immediately():
    """A missing key or a dead socket is not something a rewrite fixes —
    lily_arsenal_gen.lily_is_unavailable's own rule, reused."""
    db = FakeBankDB()
    lane = "adult:adult_couples"

    async def _sleep(_s):
        return None

    author = make_author(
        [], fail_with=RuntimeError("XAI_API_KEY missing — unconfigured"),
        fail_times=99,
    )
    summary = _run(bank.lily_replenish_lane(
        db, lane=lane, author=author, verify=_always_verify,
        target=5, max_new=5, sleep=_sleep,
    ))
    assert summary["status"] == "failed"
    assert author.state["calls"] == 1, "an unavailable provider is not retried"


def test_moderation_refusal_is_counted_and_logged_not_raised(caplog):
    db = FakeBankDB()
    lane = "adult:adult_kink"

    async def _sleep(_s):
        return None

    author = make_author(
        [], fail_with=RuntimeError(
            "xAI 400: Generated content rejected by content moderation"
        ),
        fail_times=99,
    )
    with caplog.at_level(logging.WARNING, logger="lily_bank_replenish"):
        summary = _run(bank.lily_replenish_lane(
            db, lane=lane, author=author, verify=_always_verify,
            target=2, max_new=1, sleep=_sleep,
        ))
    assert summary["rejected_moderation"] == 3
    assert summary["errors"] == 0, "a refusal is an outcome, not an error"
    assert any("MODERATION_REJECTED" in m for m in caplog.messages)
    assert any("lane=adult:adult_kink" in m for m in caplog.messages)


# ---------------------------------------------------------------------------
# Dedup: exact sha256 + the ARSENAL-SEED A5 similarity check.
# ---------------------------------------------------------------------------


def test_text_hash_is_sha256_of_the_normalized_text():
    a = bank.lily_bank_text_sha256("Which sea, colourful, sits between Europe?")
    b = bank.lily_bank_text_sha256("which sea colourful sits between europe")
    assert a == b, "punctuation and case must not change the dedup key"
    assert len(a) == 64, "sha256 hex"


def test_dedup_rejects_an_exact_duplicate():
    db = FakeBankDB()
    lane = "general:academic"
    seed_bank_row(db, lane, "Which planet is known as the red planet?")
    hit = _run(bank.lily_bank_find_duplicate(
        db, lane=lane,
        question_text="which planet is known as the RED planet",
    ))
    assert hit and hit["match"] == "exact"


def test_dedup_rejects_a_near_duplicate():
    """The whole point of A5: an author asked forty times for one more
    academic question writes the same question in different words."""
    db = FakeBankDB()
    lane = "general:academic"
    seed_bank_row(
        db, lane,
        "Which planet in our solar system is known as the red planet?",
    )
    hit = _run(bank.lily_bank_find_duplicate(
        db, lane=lane,
        question_text=(
            "Which planet in our solar system is known as the red planet"
        ),
        ratio=0.87,
    ))
    assert hit and hit["match"] in ("exact", "fuzzy")


def test_dedup_lets_a_genuinely_new_question_through():
    db = FakeBankDB()
    lane = "general:academic"
    seed_bank_row(db, lane, "Which planet is known as the red planet?")
    hit = _run(bank.lily_bank_find_duplicate(
        db, lane=lane,
        question_text="Who wrote the novel Beloved?",
    ))
    assert hit is None


def test_a_near_duplicate_is_never_banked_and_is_counted(caplog):
    db = FakeBankDB()
    lane = "general:academic"
    seed_bank_row(db, lane, "Which planet is known as the red planet?")
    before = len(db.tables[bank.QUESTIONS_TABLE])
    author = make_author([q("Which planet is known as the red planet?")])
    with caplog.at_level(logging.INFO, logger="lily_bank_replenish"):
        summary = _run(bank.lily_replenish_lane(
            db, lane=lane, author=author, verify=_always_verify,
            target=5, max_new=1,
        ))
    assert summary["dup"] == 1 and summary["accepted"] == 0
    assert len(db.tables[bank.QUESTIONS_TABLE]) == before
    assert any("DUP_REJECTED" in m for m in caplog.messages)


def test_an_unreadable_bank_fails_the_dedup_closed():
    """Returning None on a read failure would let the author fill the bank
    with duplicates exactly when the database is unhealthy."""

    class _Broken(FakeBankDB):
        def table(self, name):
            raise RuntimeError("PostgREST unreachable")

    hit = _run(bank.lily_bank_find_duplicate(
        _Broken(), lane="general:academic", question_text="anything at all",
    ))
    assert hit is not None and hit["match"] == "unchecked"


# ---------------------------------------------------------------------------
# The write: status='ready', lane, hash, stamp. INSERT-tested (fleet S3).
# ---------------------------------------------------------------------------


def test_ready_row_carries_the_status_contract_with_s1():
    row = bank.lily_ready_row(
        "adult:adult_kink",
        {"prompt": "a question?", "canonical_answer": "an answer",
         "acceptable_answers": ["an answer"], "difficulty_tier": 3},
        run_id="run-1",
    )
    assert row["status"] == "ready", "never 'active' — S2 writes 'ready' only"
    assert row["lane"] == "adult:adult_kink"
    assert row["category"] == "adult_kink"
    assert row["adult"] is True and row["mode"] == "adult"
    assert row["question_text_sha256"] == bank.lily_bank_text_sha256("a question?")
    assert row["replenished_at"] == "now()"
    assert row["replenish_run_id"] == "run-1"
    assert row["source"] == "bank_replenish_v1"
    assert row["difficulty_tier"] == 3


def test_general_lane_rows_are_not_adult():
    row = bank.lily_ready_row(
        "general:pop culture",
        {"prompt": "who sang this?", "canonical_answer": "someone"},
    )
    assert row["adult"] is False and row["mode"] == "general"
    # The BANK's spelling, not the rotation's — see LANE_BANK_CATEGORY.
    assert row["category"] == "pop_culture"


def test_difficulty_tier_is_clamped_to_the_live_check_constraint():
    """The generation prompt says "tier N of 4"; the column allows 1..3.
    An unclamped 4 is an INSERT the database refuses — a good question
    lost as a mystery in the error count."""
    # 0 and None are "the author said nothing" and take the tier-2 default;
    # everything else is clamped into range.
    for given, expected in ((0, 2), (None, 2), (1, 1), (3, 3), (4, 3), (99, 3)):
        row = bank.lily_ready_row(
            "general:academic",
            {"prompt": "q?", "canonical_answer": "a", "difficulty_tier": given},
        )
        assert row["difficulty_tier"] == expected


def test_insert_lands_one_ready_row_through_the_production_writer():
    db = FakeBankDB()
    ok = _run(bank.lily_bank_insert_ready(
        db, lane="general:academic",
        question=q("Who wrote the novel Beloved?", "Toni Morrison"),
        run_id="run-9",
    ))
    assert ok is True
    rows = db.tables[bank.QUESTIONS_TABLE]
    assert len(rows) == 1
    assert rows[0]["status"] == "ready"
    assert rows[0]["lane"] == "general:academic"
    assert rows[0]["replenished_at"] == db.now
    assert rows[0]["question_text_sha256"]


def test_insert_refuses_a_question_with_no_answer():
    db = FakeBankDB()
    ok = _run(bank.lily_bank_insert_ready(
        db, lane="general:academic",
        question={"prompt": "half a question?", "canonical_answer": ""},
    ))
    assert ok is False
    assert db.tables.get(bank.QUESTIONS_TABLE, []) == []


def test_the_sha256_unique_index_is_the_last_line_of_defence():
    """Two concurrent runs racing on the same text: one row, one honest
    failure, never two rows."""
    db = FakeBankDB()
    first = _run(bank.lily_bank_insert_ready(
        db, lane="general:academic", question=q("the very same question?"),
    ))
    second = _run(bank.lily_bank_insert_ready(
        db, lane="general:academic", question=q("the very same question?"),
    ))
    assert first is True and second is False
    assert len(db.tables[bank.QUESTIONS_TABLE]) == 1


# ---------------------------------------------------------------------------
# Verification stays in-line (it is fast, and it is the gate that stops a
# wrong answer being banked forever).
# ---------------------------------------------------------------------------


def test_a_question_that_fails_verification_is_never_banked(caplog):
    db = FakeBankDB()

    async def _reject(question, run_id=None):
        return False, "the canonical answer is wrong"

    author = make_author([q("a plausible but wrong question?")])
    with caplog.at_level(logging.INFO, logger="lily_bank_replenish"):
        summary = _run(bank.lily_replenish_lane(
            db, lane="general:academic", author=author, verify=_reject,
            target=5, max_new=1,
        ))
    assert summary["rejected_verify"] == 1 and summary["accepted"] == 0
    assert db.tables.get(bank.QUESTIONS_TABLE, []) == []
    assert any("VERIFY_REJECTED" in m for m in caplog.messages)


def test_a_verifier_that_raises_rejects_rather_than_banks():
    db = FakeBankDB()

    async def _explode(question, run_id=None):
        raise RuntimeError("verifier returned unparseable output")

    author = make_author([q("an unverifiable question?")])
    summary = _run(bank.lily_replenish_lane(
        db, lane="general:academic", author=author, verify=_explode,
        target=5, max_new=1,
    ))
    assert summary["rejected_verify"] == 1
    assert db.tables.get(bank.QUESTIONS_TABLE, []) == []


# ---------------------------------------------------------------------------
# Run receipts, concurrency, and cost.
# ---------------------------------------------------------------------------


def test_a_second_run_on_a_live_lane_stands_down():
    """The partial unique index is the guarantee, not a flag: an in-session
    author and a cron run cannot double-fill one lane."""
    db = FakeBankDB()
    lane = "general:academic"
    first = _run(bank.lily_run_start(db, lane=lane, target=40))
    second = _run(bank.lily_run_start(db, lane=lane, target=40))
    assert first is not None
    assert second is None


def test_a_dead_run_is_reclaimed_and_a_live_one_is_left_alone():
    db = FakeBankDB()
    lane = "general:academic"
    _run(bank.lily_run_start(db, lane=lane, target=40))
    assert _run(bank.lily_run_reclaim_stale(db, lane=lane)) == 0
    db.tables[bank.RUNS_TABLE][0]["heartbeat_at"] = "2020-01-01T00:00:00+00:00"
    assert _run(bank.lily_run_reclaim_stale(db, lane=lane)) == 1
    assert db.tables[bank.RUNS_TABLE][0]["status"] == "failed"
    # And now the lane is free again.
    assert _run(bank.lily_run_start(db, lane=lane, target=40)) is not None


def test_the_run_receipt_carries_the_numbers_and_the_token_cost(caplog):
    db = FakeBankDB()
    lane = "general:academic"
    run_id = _run(bank.lily_run_start(db, lane=lane, target=4))
    # The streaming transport's usage rows for this run (migrations
    # 026/027), tagged with the run id — this is where cost comes from.
    # Two author calls + two verify calls = four usage rows.
    for _ in range(4):
        db.table(bank.USAGE_TABLE).insert(
            {"session_id": run_id, "prompt_tokens": 500,
             "completion_tokens": 125, "purpose": bank.USAGE_PURPOSE}
        ).execute()
    author = make_author([q("first banked question?"), q("second banked one?")])
    with caplog.at_level(logging.INFO, logger="lily_bank_replenish"):
        summary = _run(bank.lily_replenish_lane(
            db, lane=lane, author=author, verify=_always_verify,
            target=4, max_new=2, run_id=run_id,
        ))
    assert summary["accepted"] == 2
    assert summary["cost_tokens"] == 2500
    assert summary["cost_tokens_per_question"] == 1250.0
    row = db.tables[bank.RUNS_TABLE][0]
    assert row["status"] == "completed"
    assert row["accepted_count"] == 2
    assert row["cost_tokens"] == 2500
    cost_line = [m for m in caplog.messages if "| REPLENISH |" in m]
    assert cost_line, "the cost receipt must be emitted"
    assert "authored=2" in cost_line[0] and "accepted=2" in cost_line[0]
    assert "dup=0" in cost_line[0] and "cost_tokens=2500" in cost_line[0]


def test_start_and_done_receipts_are_emitted(caplog):
    db = FakeBankDB()
    lane = "general:academic"
    author = make_author([q("one more question?")])

    async def _sleep(_s):
        return None

    with caplog.at_level(logging.INFO, logger="lily_bank_replenish"):
        run_id = _run(bank.lily_run_start(db, lane=lane, target=2))
        _run(bank.lily_replenish_lane(
            db, lane=lane, author=author, verify=_always_verify,
            target=2, max_new=1, run_id=run_id, sleep=_sleep,
        ))
    assert any("REPLENISH_START" in m for m in caplog.messages)
    assert any("REPLENISH_DONE" in m for m in caplog.messages)


def test_a_run_that_dies_still_closes_its_receipt():
    db = FakeBankDB()
    lane = "general:academic"

    async def _sleep(_s):
        return None

    run_id = _run(bank.lily_run_start(db, lane=lane, target=5))
    author = make_author(
        [], fail_with=RuntimeError("http 503 upstream"), fail_times=99
    )
    _run(bank.lily_replenish_lane(
        db, lane=lane, author=author, verify=_always_verify,
        target=5, max_new=2, run_id=run_id, sleep=_sleep,
    ))
    row = db.tables[bank.RUNS_TABLE][0]
    assert row["status"] == "failed"
    assert row["finished_at"], "a run that died is still a run with numbers"


# ---------------------------------------------------------------------------
# session_metrics.bank_replenish (S1: the sensor has a consumer).
# ---------------------------------------------------------------------------


def test_session_summary_counts_the_run():
    bank.lily_reset_session_summary()
    db = FakeBankDB()
    author = make_author([q("a counted question?")])
    _run(bank.lily_replenish_lane(
        db, lane="general:academic", author=author, verify=_always_verify,
        target=3, max_new=1,
    ))
    summary = bank.lily_session_summary()
    assert summary["runs"] == 1
    assert summary["authored"] == 1
    assert summary["accepted"] == 1
    assert summary["dup"] == 0
    bank.lily_reset_session_summary()


def test_session_summary_is_zeros_when_nothing_ran():
    bank.lily_reset_session_summary()
    assert bank.lily_session_summary() == {
        "runs": 0, "authored": 0, "accepted": 0, "rejected": 0, "dup": 0,
    }


# ---------------------------------------------------------------------------
# Health readout.
# ---------------------------------------------------------------------------


def test_health_reports_count_target_and_rejection_rate_per_lane():
    db = FakeBankDB()
    fill_lane(db, "general:academic", 40)
    fill_lane(db, "general:wordplay", 2)
    db.table(bank.RUNS_TABLE).insert({
        "lane": "general:wordplay", "deck": "general", "category": "wordplay",
        "status": "completed", "target_depth": 40, "authored_count": 4,
        "accepted_count": 1, "rejected_verify": 2, "rejected_moderation": 1,
        "error_count": 0, "cost_tokens": 5000,
        "finished_at": "2026-09-06T11:00:00+00:00",
    }).execute()
    health = _run(bank.lily_bank_health(
        db, lanes=["general:academic", "general:wordplay"]
    ))
    academic = health["lanes"]["general:academic"]
    wordplay = health["lanes"]["general:wordplay"]
    assert academic["stocked"] is True and academic["below_watermark"] is False
    assert wordplay["below_watermark"] is True
    assert wordplay["rejection_rate"] == 0.75
    assert wordplay["last_replenished_at"] == "2026-09-06T11:00:00+00:00"
    assert wordplay["cost_tokens"] == 5000
    assert health["healthy"] is False
    readout = bank.lily_format_bank_health(health)
    assert "general:wordplay" in readout and "LOW" in readout


# ---------------------------------------------------------------------------
# Review fixes (S2 GO-WITH-FIXES, 2026-09-06). Each of these was a way the
# job would have looked like it worked while doing nothing, or doing the
# wrong thing quietly — which is the failure mode a background job is most
# prone to, because nobody is sitting in front of it.
# ---------------------------------------------------------------------------


def test_lane_categories_are_the_bank_s_spelling_not_the_rotation_s():
    """P1-3. Measured live: the bank stores `lifestyle` and `pop_culture`;
    the rotation calls those families `lifestyle-potpourri` and `pop
    culture`. Reading the rotation's spelling would have counted ZERO rows
    in two of six lanes, read them as fully consumed, and authored ~74
    questions into category values nothing draws from."""
    assert bank.lily_lane_bank_category("general:lifestyle-potpourri") == "lifestyle"
    assert bank.lily_lane_bank_category("general:pop culture") == "pop_culture"
    # Unaliased families are unchanged.
    assert bank.lily_lane_bank_category("general:academic") == "academic"
    assert bank.lily_lane_bank_category("adult:adult_kink") == "adult_kink"


def test_a_lane_counts_every_spelling_it_covers():
    """Live: 38 rows under `pop_culture` AND 6 under `pop culture`. Both
    are in the lane; counting one of them understates its depth."""
    lane = "general:pop culture"
    assert bank.lily_lane_bank_categories(lane) == ("pop_culture", "pop culture")
    db = FakeBankDB()
    for i in range(3):
        seed_bank_row(db, lane, f"underscore spelled {i}")
    for i in range(2):
        seed_bank_row(
            db, lane, f"space spelled {i}", category="pop culture",
        )
    depth = _run(bank.lily_lane_depth(db, lane=lane))
    assert depth["servable"] == 5


def test_an_aliased_lane_lands_its_rows_where_the_draw_looks():
    db = FakeBankDB()
    _run(bank.lily_bank_insert_ready(
        db, lane="general:lifestyle-potpourri",
        question=q("what herb is in pesto?", "basil"),
    ))
    row = db.tables[bank.QUESTIONS_TABLE][0]
    assert row["category"] == "lifestyle"
    assert row["lane"] == "general:lifestyle-potpourri"
    # And it counts toward the lane it was banked for.
    depth = _run(bank.lily_lane_depth(db, lane="general:lifestyle-potpourri"))
    assert depth["ready"] == 1


def test_dedup_spans_both_spellings_of_a_lane():
    db = FakeBankDB()
    lane = "general:pop culture"
    seed_bank_row(
        db, lane, "Which band recorded the album Rumours?",
        category="pop culture",
    )
    hit = _run(bank.lily_bank_find_duplicate(
        db, lane=lane, question_text="which band recorded the album Rumours",
    ))
    assert hit and hit["match"] == "exact"


def test_the_heartbeat_beats_on_an_all_rejection_run():
    """P2-3. A run whose every candidate is rejected banks nothing. With
    the heartbeat only on the insert path, a long all-rejection run goes
    silent and the NEXT run reclaims it as dead while it is still working."""
    db = FakeBankDB()
    lane = "general:academic"
    run_id = _run(bank.lily_run_start(db, lane=lane, target=5))
    row = db.tables[bank.RUNS_TABLE][0]
    row["heartbeat_at"] = "2020-01-01T00:00:00+00:00"

    async def _reject(question, run_id=None):
        return False, "no"

    async def _sleep(_s):
        return None

    author = make_author([q("rejected one?"), q("rejected two?")])
    summary = _run(bank.lily_replenish_lane(
        db, lane=lane, author=author, verify=_reject,
        target=5, max_new=2, run_id=run_id, sleep=_sleep,
    ))
    assert summary["accepted"] == 0 and summary["rejected_verify"] == 2
    assert row["heartbeat_at"] == db.now, (
        "an all-rejection run must still prove it is alive"
    )


def test_run_start_distinguishes_a_busy_lane_from_a_broken_table(caplog):
    """P2-5. Reporting a missing table or an RLS refusal as 'a run is
    already active' hides the day migration 029 was never applied behind a
    reassuring INFO line."""
    assert bank._is_duplicate_key_error(
        Exception('duplicate key value violates unique constraint "x"')
    ) is True
    assert bank._is_duplicate_key_error(
        Exception('relation "lily_bank_replenish_runs" does not exist')
    ) is False

    class _NoTable(FakeBankDB):
        def enforce_constraints(self, table, row):
            raise Exception('relation "lily_bank_replenish_runs" does not exist')

    with caplog.at_level(logging.INFO, logger="lily_bank_replenish"):
        assert _run(bank.lily_run_start(
            _NoTable(), lane="general:academic", target=5
        )) is None
    assert any("reason=run_start" in m for m in caplog.messages)
    assert not any("already_active" in m for m in caplog.messages)


def test_cost_waits_for_a_usage_row_still_in_flight():
    """P2-6. The transport writes its usage row fire-and-forget, so the
    last call's row is typically NOT in the table when the run ends. A
    straight SELECT under-counts — on a one-question run, by everything."""
    db = FakeBankDB()
    lane = "general:academic"
    run_id = _run(bank.lily_run_start(db, lane=lane, target=3))
    landed = {"n": 0}

    async def _late_sleep(_seconds):
        # Each poll, one more scheduled usage write lands — exactly the
        # shape of a task queued on the loop behind the caller.
        landed["n"] += 1
        db.table(bank.USAGE_TABLE).insert(
            {"session_id": run_id, "prompt_tokens": 600,
             "completion_tokens": 200}
        ).execute()

    author = make_author([q("a question with a late receipt?")])
    summary = _run(bank.lily_replenish_lane(
        db, lane=lane, author=author, verify=_always_verify,
        target=3, max_new=1, run_id=run_id, sleep=_late_sleep,
    ))
    assert summary["accepted"] == 1
    assert summary["llm_calls_expected"] == 2  # one author + one verify
    assert summary["cost_tokens"] == 1600, "both rows must be counted"
    assert db.tables[bank.RUNS_TABLE][0]["cost_tokens"] == 1600


def test_a_cost_read_that_never_settles_still_closes_the_run(caplog):
    db = FakeBankDB()
    lane = "general:academic"
    run_id = _run(bank.lily_run_start(db, lane=lane, target=3))

    async def _no_op_sleep(_seconds):
        return None

    author = make_author([q("a question whose receipt never lands?")])
    with caplog.at_level(logging.INFO, logger="lily_bank_replenish"):
        summary = _run(bank.lily_replenish_lane(
            db, lane=lane, author=author, verify=_always_verify,
            target=3, max_new=1, run_id=run_id, sleep=_no_op_sleep,
        ))
    assert summary["accepted"] == 1
    assert summary["cost_tokens"] == 0
    assert any("COST_PARTIAL" in m for m in caplog.messages)
    assert db.tables[bank.RUNS_TABLE][0]["status"] == "completed"


def test_the_live_author_steers_off_the_lane_s_existing_questions():
    """P2-7. generate_question already takes an avoid-list; the A5 gate
    rejects a near-duplicate AFTER paying for it, and this is the half
    that stops one being written."""
    db = FakeBankDB()
    lane = "general:academic"
    seed_bank_row(db, lane, "Which planet is known as the red planet?")
    seed_bank_row(db, lane, "Who wrote the novel Beloved?")
    seen = {}

    class _FakeReasoning:
        async def generate_question(
            self, category, tier, avoid, *, effort=None, purpose=None,
            usage_session_id=None, mode="adult",
        ):
            seen.update(
                category=category, tier=tier, avoid=list(avoid),
                effort=effort, purpose=purpose, run=usage_session_id, mode=mode,
            )
            return q("a genuinely new question?")

    author = bank.lily_live_author(_FakeReasoning(), supabase=db)
    result = _run(author(lane, "run-77"))
    assert result is not None
    assert "Which planet is known as the red planet?" in seen["avoid"]
    assert "Who wrote the novel Beloved?" in seen["avoid"]
    assert seen["purpose"] == bank.USAGE_PURPOSE
    assert seen["run"] == "run-77"
    assert 1 <= seen["tier"] <= 3
    assert seen["effort"] == lily_config.bank_replenish_effort()


def test_the_live_author_writes_in_the_lane_s_register():
    """P1-3 second half. The generation prompt hardcoded 'Mode: adult';
    stocking `academic` through it would have filled a general lane with
    innuendo."""
    seen = {}

    class _FakeReasoning:
        async def generate_question(
            self, category, tier, avoid, *, effort=None, purpose=None,
            usage_session_id=None, mode="adult",
        ):
            seen[category] = mode
            return q("a question?")

    author = bank.lily_live_author(_FakeReasoning(), supabase=None)
    _run(author("general:academic"))
    _run(author("adult:adult_kink"))
    _run(author("general:pop culture"))
    assert seen["academic"] == "general"
    assert seen["adult_kink"] == "adult"
    # And the category it asks for is the bank's spelling.
    assert seen["pop_culture"] == "general"


def test_the_generation_prompt_carries_the_register_it_is_given():
    import lily_reasoning

    general = lily_reasoning._GENERATION_PROMPT.format(
        category="academic", difficulty_tier=2, avoid_block="- none",
        mode_block=lily_reasoning._MODE_BLOCKS["general"],
    )
    adult = lily_reasoning._GENERATION_PROMPT.format(
        category="academic", difficulty_tier=2, avoid_block="- none",
        mode_block=lily_reasoning._MODE_BLOCKS["adult"],
    )
    assert "Mode: general" in general
    assert "Mode: adult" not in general
    assert "innuendo and wordplay" not in general
    # The adult render is what the prompt has always said, unchanged.
    assert "Mode: adult" in adult and "innuendo and wordplay" in adult
