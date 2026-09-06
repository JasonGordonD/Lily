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
        assert fields["category"] == bank.lily_lane_bank_category(lane)
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


# ---------------------------------------------------------------------------
# Review fixes: the shutdown callback's ARITY, and the runner's usage lane.
# Both are "looks wired, does nothing" defects — the class a background job
# is most prone to, because nobody is watching it work.
# ---------------------------------------------------------------------------


def _livekit_style_dispatch(callback, reason="user_initiated"):
    """How livekit 1.6.10 invokes a shutdown callback: it inspects the
    arity and hands a one-argument callable the shutdown REASON string.
    A defaulted lambda (`lambda t=task:`) reads as arity 1 — the reason
    lands in `t` and the real task is never touched."""
    import inspect

    if len(inspect.signature(callback).parameters) >= 1:
        return callback(reason)
    return callback()


def test_the_shutdown_callback_takes_no_arguments():
    """P1-1. Registered with arity 1, the framework passes the reason
    string and the author task outlives its session, silently."""
    import inspect

    async def _never():
        await asyncio.sleep(60)

    async def _scenario():
        task = asyncio.ensure_future(_never())
        callback = bank.lily_shutdown_callback(task)
        assert len(inspect.signature(callback).parameters) == 0, (
            "livekit hands a 1-arg callback the shutdown reason"
        )
        await _livekit_style_dispatch(callback)
        return task

    task = asyncio.run(_scenario())
    assert task.cancelled(), "the shutdown callback must actually stop the job"


def test_the_entrypoint_registers_a_zero_arg_shutdown_callback():
    """The call site, not just the helper: lily_agent must hand the
    framework the factory's result, never a defaulted lambda."""
    import inspect

    import lily_agent

    source = inspect.getsource(lily_agent.entrypoint)
    assert "lily_bank_replenish.lily_shutdown_callback(" in source
    assert "lambda t=_bank_author_task" not in source


def _load_runner():
    import importlib.util

    path = Path(__file__).resolve().parent.parent / "scripts" / "bank_replenish.py"
    spec = importlib.util.spec_from_file_location("bank_replenish_runner", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_runner_binds_the_usage_lane_so_cost_is_not_always_zero():
    """P1-2. lily_metrics.record_llm_call routes through a module-global
    collector the agent's entrypoint binds. A CLI process has no
    entrypoint: unbound, every call records nothing, no lily_llm_usage row
    is written, and every runner receipt says cost_tokens=0 — on the only
    job that will actually be run, and the one number the effort decision
    needs."""
    import lily_metrics

    runner = _load_runner()
    db = FakeBankDB()
    previous = lily_metrics.current_collector()
    try:
        # Unbound: the transport's receipt goes nowhere.
        lily_metrics.set_current_collector(None)
        assert lily_metrics.record_llm_call(
            purpose=bank.USAGE_PURPOSE, model="grok-4.5", effort="medium",
            ttft_ms=17000.0, total_ms=18000.0, prompt_tokens=983,
            completion_tokens=875, session_id="run-abc",
        ) is False
        assert db.tables.get(bank.USAGE_TABLE, []) == []

        async def _scenario():
            runner._bind_usage_context(db)
            assert lily_metrics.record_llm_call(
                purpose=bank.USAGE_PURPOSE, model="grok-4.5", effort="medium",
                ttft_ms=17000.0, total_ms=18000.0, prompt_tokens=983,
                completion_tokens=875, session_id="run-abc",
            ) is True
            # The write is scheduled on the loop, not awaited by the caller.
            for _ in range(20):
                await asyncio.sleep(0)
                if db.tables.get(bank.USAGE_TABLE):
                    break
            return await bank.lily_run_cost_tokens(db, run_id="run-abc")

        cost = asyncio.run(_scenario())
    finally:
        lily_metrics.set_current_collector(previous)
    assert db.tables[bank.USAGE_TABLE], "a usage row must land"
    assert cost["cost_tokens"] == 1858
    assert cost["calls"] == 1


def test_the_runner_offers_exactly_the_module_s_lanes_and_binds_before_authoring():
    """The runner's lane list is the module's, so the category alias and
    the per-lane register ride into every out-of-session run too — and the
    usage lane is bound BEFORE the first authoring call, or the run's own
    receipt would miss its first rows."""
    import inspect

    runner = _load_runner()
    parser_source = inspect.getsource(runner.main)
    assert "choices=list(lily_bank_replenish.LANES)" in parser_source
    body = inspect.getsource(runner._amain)
    assert body.index("_bind_usage_context") < body.index("lily_live_author")
    assert "supabase=supabase" in body
