"""REFACTOR-STAGE-1B-001 P1-5 — fire-and-forget game coroutines get an
exception observer.

`asyncio.ensure_future(coro)` with no consumer means a raising `_breathe`
/ `_immediate` / `_discharge` / floor-line `_run` surfaced only as asyncio's
"Task exception was never retrieved" at GC time — no session id, no count,
and a lost beat (the N+1 breath that never dispatched) with nothing in the
log to say why. `lily_spawn(coro, name, game=)` attaches a done-callback:
`LILY_TASK | FAULT | name=<name>` at ERROR with traceback and
game._task_faults, consumed by session_metrics.task_faults.
"""

import asyncio
import gc
import logging
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_agent  # noqa: E402
from lily_agent import LilyGame  # noqa: E402
from lily_scorekeeper import LilyScorekeeper  # noqa: E402


def _game():
    sk = LilyScorekeeper("lily-S1B-spawn")
    return LilyGame.bare(sk=sk), sk


def _faults(caplog):
    return [r for r in caplog.records if "LILY_TASK | FAULT" in r.getMessage()]


def test_a_raising_coroutine_is_logged_and_counted(caplog):
    game, sk = _game()

    async def _boom():
        raise RuntimeError("breath died")

    async def scenario():
        with caplog.at_level(logging.ERROR, logger="lily_agent"):
            task = lily_agent.lily_spawn(_boom(), "breathe", game=game)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert task.done()

    asyncio.run(scenario())
    assert game._task_faults == 1
    f = _faults(caplog)
    assert len(f) == 1
    msg = f[0].getMessage()
    assert "name=breathe" in msg and "session=lily-S1B-spawn" in msg
    assert "faults=1" in msg and "error_class=RuntimeError" in msg
    assert f[0].exc_info is not None and f[0].levelno == logging.ERROR


def test_the_exception_is_retrieved_so_asyncio_never_complains():
    """Control first: a bare ensure_future of a raising coroutine, dropped
    and collected, DOES reach the loop's exception handler as "Task
    exception was never retrieved" — so the assertion below can fail.
    Then lily_spawn: the observer retrieved it; the handler stays silent."""
    seen = []

    async def _boom():
        raise RuntimeError("x")

    async def scenario():
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda lp, ctx: seen.append(ctx.get("message")))
        # control
        t = asyncio.ensure_future(_boom())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert t.done()
        del t
        gc.collect()
        assert any("never retrieved" in (m or "") for m in seen), seen
        seen.clear()
        # the fix
        game, _ = _game()
        t = lily_agent.lily_spawn(_boom(), "discharge", game=game)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert t.done()
        del t
        gc.collect()
        assert seen == []
        assert game._task_faults == 1

    asyncio.run(scenario())


def test_cancellation_and_success_are_not_faults(caplog):
    game, sk = _game()

    async def _slow():
        await asyncio.sleep(10)

    async def _fine():
        return 1

    async def scenario():
        with caplog.at_level(logging.ERROR, logger="lily_agent"):
            t = lily_agent.lily_spawn(_slow(), "breathe", game=game)
            await asyncio.sleep(0)
            t.cancel()
            await asyncio.sleep(0)
            lily_agent.lily_spawn(_fine(), "discharge", game=game)
            await asyncio.sleep(0)
            await asyncio.sleep(0)

    asyncio.run(scenario())
    assert getattr(game, "_task_faults", 0) == 0
    assert _faults(caplog) == []


def test_faults_accumulate_and_ride_session_metadata(caplog):
    game, sk = _game()
    assert lily_agent.lily_session_metadata(game, sk, {}, None)["session_metrics"][
        "task_faults"
    ] == 0

    async def _boom():
        raise ValueError("x")

    async def scenario():
        with caplog.at_level(logging.ERROR, logger="lily_agent"):
            for name in ("breathe", "fusion_clip_immediate", "discharge"):
                lily_agent.lily_spawn(_boom(), name, game=game)
            await asyncio.sleep(0)
            await asyncio.sleep(0)

    asyncio.run(scenario())
    assert game._task_faults == 3
    assert lily_agent.lily_session_metadata(game, sk, {}, None)["session_metrics"][
        "task_faults"
    ] == 3
    assert [
        m for m in (r.getMessage() for r in _faults(caplog))
        if "name=fusion_clip_immediate" in m
    ]


def test_a_game_less_spawn_still_logs(caplog):
    async def _boom():
        raise RuntimeError("x")

    async def scenario():
        with caplog.at_level(logging.ERROR, logger="lily_agent"):
            lily_agent.lily_spawn(_boom(), "floor_line")
            await asyncio.sleep(0)
            await asyncio.sleep(0)

    asyncio.run(scenario())
    f = _faults(caplog)
    assert len(f) == 1 and "session=? faults=?" in f[0].getMessage()


def test_consume_seam_returns_the_non_task_untouched(monkeypatch):
    """Existing fixtures monkeypatch asyncio.ensure_future with a fake that
    consumes the coroutine (no loop); lily_spawn must not blow up there."""
    consumed = []

    def _consume(coro, *a, **k):
        coro.close()
        consumed.append(True)
        return None

    monkeypatch.setattr(asyncio, "ensure_future", _consume)

    async def _x():
        return None

    assert lily_agent.lily_spawn(_x(), "breathe", game=None) is None
    assert consumed == [True]


# ---------------------------------------------------------------------------
# a real site: the floor-line scheduler
# ---------------------------------------------------------------------------

def test_floor_line_site_reports_its_fault(caplog):
    game = types.SimpleNamespace(
        sk=types.SimpleNamespace(session_id="lily-S1B-floor"),
        floor_line_owed=lambda: True,
        fire_floor_line=lambda reason: (_ for _ in ()).throw(RuntimeError("floor")),
    )

    async def scenario():
        with caplog.at_level(logging.ERROR, logger="lily_agent"):
            lily_agent._lily_schedule_floor_if_owed(game, "suppressed")
            await asyncio.sleep(0)
            await asyncio.sleep(0)

    asyncio.run(scenario())
    assert game._task_faults == 1
    f = _faults(caplog)
    assert len(f) == 1
    assert "name=floor_line session=lily-S1B-floor" in f[0].getMessage()


def test_discharge_site_reports_its_fault(monkeypatch, caplog):
    """open_window_after_discharge's `_discharge` calls open_window after
    the gap; an open_window that raises is a counted LILY_TASK fault, not
    a silent lost window."""
    import lily_config
    game, sk = _game()
    monkeypatch.setattr(lily_config, "room_discharge_seconds", lambda: 0.001)

    def _open(*a, **k):
        raise RuntimeError("window exploded")

    game.open_window = _open

    async def scenario():
        with caplog.at_level(logging.ERROR, logger="lily_agent"):
            game.open_window_after_discharge()
            await asyncio.sleep(0.02)

    asyncio.run(scenario())
    assert game._task_faults == 1
    assert any("name=discharge" in r.getMessage() for r in _faults(caplog))
