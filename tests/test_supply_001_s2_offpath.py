"""WO-LILY-SUPPLY-001 S2 — the author is OFF the delivery path.

The ruling this file exists to make mechanical: "Live question authoring
never sits on the delivery path again."

The measured cost of getting this wrong is on record: grok-4.5 authoring
takes 20-39 seconds to its first content token, and for as long as that
call sat inside prefetch, a table waited through it. So the test here is
not "the code looks detached" — it is a fixture game drawing questions
while the replenisher's author is stubbed to sleep SIXTY SECONDS, with the
draw's wall-clock measured. If any await path coupled the two, the draw
could not finish in milliseconds.

Also covered: the lane table cannot silently drift from the rotation
lily_agent actually serves, and the supervisor unwinds cleanly on
cancellation (a session teardown must not leave a task screaming).
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_bank_replenish as bank
import lily_config
import lily_persistence
from test_supply_001_s2_bank import FakeBankDB, fill_lane, q


# ---------------------------------------------------------------------------
# The load-bearing one: a sixty-second author, and a delivery-path draw that
# does not notice.
# ---------------------------------------------------------------------------


def test_a_sixty_second_author_costs_the_delivery_path_nothing():
    """The replenisher runs detached with an author that never returns
    inside the test's lifetime. The delivery path — the real bank draw,
    lily_persistence.lily_fetch_bank_question — must complete immediately
    and must return a question, twenty times over."""
    db = FakeBankDB()
    # The adult lane on purpose: it is the deck whose authoring is the slow
    # one (grok-4.5 multi-agent), and it is the register
    # lily_fetch_bank_question serves under the unified deck.
    lane = "adult:adult_couples"
    # A starved lane, so the watermark definitely fires and the author is
    # definitely called: the test would be vacuous against a full bank.
    fill_lane(db, lane, 1)

    async def _scenario():
        author_started = asyncio.Event()

        async def _glacial_author(lane_id, run_id=None):
            author_started.set()
            await asyncio.sleep(60)  # the 20-39s first-token reality, worse
            return q("a question nobody will ever wait for")

        async def _verify(question, run_id=None):
            return True, "verified"

        # Detached exactly as lily_agent spawns it: created, never awaited.
        job = asyncio.ensure_future(bank.lily_bank_replenish_loop(
            db, author=_glacial_author, verify=_verify, lanes=[lane],
            interval_seconds=0.01,
        ))
        await asyncio.wait_for(author_started.wait(), timeout=2.0)

        started = time.monotonic()
        drawn = []
        for _ in range(20):
            row = await lily_persistence.lily_fetch_bank_question(
                db, "adult_couples", 2, [],
            )
            drawn.append(row)
        elapsed = time.monotonic() - started

        assert job.done() is False, (
            "the author must still be mid-call — otherwise this test is not "
            "measuring anything"
        )
        job.cancel()
        try:
            await job
        except asyncio.CancelledError:
            pass
        return elapsed, drawn

    elapsed, drawn = asyncio.run(_scenario())
    assert all(row is not None for row in drawn), (
        "the delivery path draws from the bank, not from the author"
    )
    assert elapsed < 1.0, (
        f"the delivery path waited {elapsed:.2f}s while a 60s author ran — "
        "authoring is back on the delivery path"
    )


def test_the_supervisor_unwinds_cleanly_on_cancellation():
    """Session teardown cancels the job; it must re-raise CancelledError and
    leave nothing running."""
    db = FakeBankDB()

    async def _scenario():
        entered = asyncio.Event()

        async def _author(lane_id, run_id=None):
            entered.set()
            await asyncio.sleep(60)

        job = asyncio.ensure_future(bank.lily_bank_replenish_loop(
            db, author=_author, verify=None, lanes=["general:academic"],
            interval_seconds=0.01,
        ))
        await asyncio.wait_for(entered.wait(), timeout=2.0)
        await bank.lily_stop_background_author(job)
        return job

    job = asyncio.run(_scenario())
    assert job.cancelled() or job.done()
    assert bank.lily_active_lanes() == set(), (
        "a cancelled run must not leave its lane latched — the next session "
        "would never replenish it"
    )


def test_stopping_a_job_that_never_started_is_a_no_op():
    asyncio.run(bank.lily_stop_background_author(None))


def test_the_loop_survives_a_sweep_that_explodes():
    """A background job that dies on the first bad night is a background job
    that silently stops replenishing."""
    db = FakeBankDB()

    async def _author(lane_id, run_id=None):
        raise RuntimeError("provider hiccup")

    async def _sleep(_s):
        return None

    sweeps = asyncio.run(bank.lily_bank_replenish_loop(
        db, author=_author, verify=None, lanes=["general:academic"],
        interval_seconds=0.0, max_sweeps=3, sleep=_sleep,
    ))
    assert sweeps == 3


def test_one_lane_never_runs_twice_at_once():
    """In-process single-flight: a sweep must not stack on its predecessor
    when a lane's authoring outlives the sweep interval."""
    db = FakeBankDB()
    lane = "general:academic"
    fill_lane(db, lane, 1)

    async def _scenario():
        in_author = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def _author(lane_id, run_id=None):
            calls.append(lane_id)
            in_author.set()
            await release.wait()
            return q("eventually")

        async def _verify(question, run_id=None):
            return True, "verified"

        first = asyncio.ensure_future(bank.lily_replenish_sweep(
            db, author=_author, verify=_verify, lanes=[lane],
        ))
        await asyncio.wait_for(in_author.wait(), timeout=2.0)
        # A second sweep arrives while the first is still authoring.
        second = await bank.lily_replenish_sweep(
            db, author=_author, verify=_verify, lanes=[lane],
        )
        concurrent_calls = len(calls)
        release.set()
        await first
        return second, concurrent_calls

    second, concurrent_calls = asyncio.run(_scenario())
    assert second == [], "the second sweep must stand down on a busy lane"
    assert concurrent_calls == 1, (
        "only the first sweep's author may be in flight for this lane"
    )


# ---------------------------------------------------------------------------
# The lane table cannot drift from the rotation the game actually serves.
# ---------------------------------------------------------------------------


def test_lane_table_matches_the_rotation_lily_agent_serves():
    """lily_bank_replenish restates the families rather than importing
    lily_agent (which imports it back). This is the guard that makes the
    restatement safe: add a family to the rotation without a lane and the
    lane gets no supply — silently, until a table notices."""
    import lily_agent

    assert list(bank.GENERAL_FAMILIES) == list(lily_agent.CATEGORY_FAMILIES)
    assert list(bank.ADULT_FAMILIES) == list(lily_agent.ADULT_CATEGORY_FAMILIES)
    assert len(bank.LANES) == len(lily_agent.CATEGORY_FAMILIES) + len(
        lily_agent.ADULT_CATEGORY_FAMILIES
    )


def test_every_lane_round_trips_through_its_id():
    for lane in bank.LANES:
        deck, category = bank.lily_parse_lane(lane)
        assert deck in ("general", "adult")
        assert bank.lily_lane_id(deck, category) == lane
        fields = bank.lily_lane_row_fields(lane)
        assert fields["category"] == category
        assert fields["adult"] is (deck == "adult")


def test_an_unknown_lane_is_refused_rather_than_guessed():
    assert bank.lily_parse_lane("nonsense") == ("", "")
    assert bank.lily_parse_lane("pictures:whatever") == ("", "")
    assert bank.lily_lane_row_fields("nonsense") == {}
    db = FakeBankDB()
    assert asyncio.run(bank.lily_bank_insert_ready(
        db, lane="nonsense", question=q("a question?"),
    )) is False


# ---------------------------------------------------------------------------
# The knobs (and the effort decision's revert path).
# ---------------------------------------------------------------------------


def test_the_in_session_author_is_off_by_default():
    """The runner tops the bank from cron; turning the live job on is an
    operator decision with a spend attached."""
    assert lily_config.bank_replenish_enabled() is False


def test_background_effort_ships_on_the_live_tier_and_reverts_in_one_var(
    monkeypatch
):
    """The interim 'drop to medium' stays until the operator rules. This is
    a SEPARATE knob for a job nobody waits on, so raising it costs latency
    that nobody is spending."""
    monkeypatch.delenv("LILY_BANK_REPLENISH_EFFORT", raising=False)
    assert lily_config.bank_replenish_effort() == lily_config.adult_reasoning_effort()
    monkeypatch.setenv("LILY_BANK_REPLENISH_EFFORT", "high")
    assert lily_config.bank_replenish_effort() == "high"
    # And the live delivery-path tier is untouched by it.
    assert lily_config.adult_reasoning_effort() == "medium"


def test_watermark_ratio_defaults_to_the_arsenal_number(monkeypatch):
    monkeypatch.delenv("LILY_BANK_REPLENISH_RATIO", raising=False)
    assert lily_config.bank_replenish_ratio() == 0.40
    monkeypatch.setenv("LILY_BANK_REPLENISH_RATIO", "0")
    assert lily_config.bank_replenish_ratio() == 0.40, "0 would fire forever"


def test_target_depth_takes_a_per_lane_override(monkeypatch):
    monkeypatch.delenv("LILY_BANK_TARGET_DEPTH", raising=False)
    assert lily_config.bank_target_depth() == 40
    monkeypatch.setenv("LILY_BANK_TARGET_DEPTH_ADULT_ADULT_KINK", "12")
    assert lily_config.bank_target_depth("adult:adult_kink") == 12
    assert lily_config.bank_target_depth("general:academic") == 40


def test_session_metrics_carries_the_bank_replenish_block():
    """S1: the sensor's consumer. lily_session_metadata is the single
    builder for both lily_sessions.metadata write sites."""
    import lily_agent

    bank.lily_reset_session_summary()

    class _SK:
        session_id = "lily-TEST"
        question_timeline = {}

    class _Metrics:
        def summary(self):
            return {}

    class _Game:
        pass

    meta = lily_agent.lily_session_metadata(_Game(), _SK(), {}, _Metrics())
    assert meta["session_metrics"]["bank_replenish"] == {
        "runs": 0, "authored": 0, "accepted": 0, "rejected": 0, "dup": 0,
    }
