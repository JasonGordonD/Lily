"""REFACTOR-STAGE-1B-001 P1-2 — the four HOTFIX-005/006 divergence safety
nets in the conversation_item_added handler no longer die silently.

Before: `except Exception: pass` around each net (score / roster /
custom_round / verdict) — a net that raised reported nothing, which reads
as "no divergence" (S1: a sensor wired to nothing manufactures false
exoneration). Now: `LILY_DIVERGENCE_NET | FAULT | net=<name>` at WARNING
with traceback, plus game._divergence_net_faults, whose consumer is
lily_sessions.metadata.session_metrics.divergence_net_faults via
lily_session_metadata (both write sites: heartbeat and close).
"""

import logging
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_agent  # noqa: E402
import lily_scorekeeper  # noqa: E402
from lily_agent import LilyGame  # noqa: E402
from lily_scorekeeper import LilyScorekeeper  # noqa: E402


def _game():
    sk = LilyScorekeeper("lily-S1B-nets")
    return LilyGame.bare(sk=sk), sk


def _assistant_item(text="You four are tied at three."):
    msg = types.SimpleNamespace(role="assistant", id="i1", content=text, metrics=None)
    return types.SimpleNamespace(item=msg)


def _metrics():
    fed = []
    return types.SimpleNamespace(collect_turn=fed.append), fed


_NETS = [
    ("score", lily_scorekeeper, "lily_narrated_score_divergence"),
    ("roster", lily_scorekeeper, "lily_narrated_roster_count_divergence"),
    ("custom_round", lily_agent, "lily_narrated_custom_round_divergence"),
    ("verdict", lily_scorekeeper, "lily_narrated_verdict_divergence"),
]


@pytest.mark.parametrize("net,module,fn", _NETS)
def test_a_net_that_raises_is_loud_and_counted(monkeypatch, caplog, net, module, fn):
    game, sk = _game()

    def _boom(*a, **k):
        raise RuntimeError(f"{net} net exploded")

    monkeypatch.setattr(module, fn, _boom)
    metrics, fed = _metrics()
    with caplog.at_level(logging.WARNING, logger="lily_agent"):
        lily_agent._on_item_added_body(game, metrics, {}, _assistant_item())
    assert game._divergence_net_faults == 1
    faults = [r for r in caplog.records if "LILY_DIVERGENCE_NET | FAULT" in r.getMessage()]
    assert len(faults) == 1
    assert f"net={net}" in faults[0].getMessage()
    assert "session=lily-S1B-nets" in faults[0].getMessage()
    assert faults[0].exc_info is not None and faults[0].levelno == logging.WARNING
    # The handler finished its job past the dead net.
    assert fed == [None]
    assert game._last_assistant_turn == ("i1", "You four are tied at three.")


def test_faults_accumulate_across_nets_and_turns(monkeypatch, caplog):
    game, sk = _game()

    def _boom(*a, **k):
        raise ValueError("x")

    for _, module, fn in _NETS:
        monkeypatch.setattr(module, fn, _boom)
    metrics, _ = _metrics()
    with caplog.at_level(logging.WARNING, logger="lily_agent"):
        lily_agent._on_item_added_body(game, metrics, {}, _assistant_item())
        lily_agent._on_item_added_body(game, metrics, {}, _assistant_item())
    assert game._divergence_net_faults == 8
    assert sum(
        "LILY_DIVERGENCE_NET | FAULT" in r.getMessage() for r in caplog.records
    ) == 8


def test_healthy_nets_count_nothing(caplog):
    game, sk = _game()
    metrics, _ = _metrics()
    with caplog.at_level(logging.WARNING, logger="lily_agent"):
        lily_agent._on_item_added_body(game, metrics, {}, _assistant_item("Nice one."))
    assert getattr(game, "_divergence_net_faults", 0) == 0
    assert not [r for r in caplog.records if "LILY_DIVERGENCE_NET" in r.getMessage()]


def test_counter_rides_session_metadata_and_increments(monkeypatch):
    """The consumer: the key is present (0) on a clean game and carries the
    count after a net faults — the same builder feeds heartbeat and close."""
    game, sk = _game()
    before = lily_agent.lily_session_metadata(game, sk, {}, None)["session_metrics"]
    assert before["divergence_net_faults"] == 0
    assert before["handler_faults"] == 0

    def _boom(*a, **k):
        raise RuntimeError("net")

    monkeypatch.setattr(lily_scorekeeper, "lily_narrated_score_divergence", _boom)
    metrics, _ = _metrics()
    lily_agent._on_item_added_body(game, metrics, {}, _assistant_item())
    after = lily_agent.lily_session_metadata(game, sk, {}, None)["session_metrics"]
    assert after["divergence_net_faults"] == 1


def test_handler_faults_ride_session_metadata_too():
    game, sk = _game()
    game._stt_handler_faults = 3
    block = lily_agent.lily_session_metadata(game, sk, {}, None)["session_metrics"]
    assert block["handler_faults"] == 3


def test_fault_keys_sit_beside_the_collectors_summary():
    """A real collector's summary is preserved; the fault keys are added,
    never replacing the 1.6.8 metrics block."""
    import lily_metrics
    game, sk = _game()
    collector = lily_metrics.LilyMetricsCollector()
    block = lily_agent.lily_session_metadata(game, sk, {}, collector)["session_metrics"]
    summary = collector.summary()
    for k, v in summary.items():
        assert block[k] == v
    assert set(block) - set(summary) == {"handler_faults", "divergence_net_faults"}
