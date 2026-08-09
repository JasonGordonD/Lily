"""Y2 measurement gate (WO-LILY-HOTFIX-007) — preemptive outcomes counted.

The Y2 decision (settle context at turn boundary vs the Y1a/Y2 volatile
split) closes on the INVALIDATION RATE of the deployed build, per the
mandate's "measurement closes on traces/metrics, never transcript rows."
The framework already announces both outcomes on its own logger
(livekit.agents / agent_activity): invalidation at WARNING (always
emitted), use at DEBUG. The tap is a logging.Filter on that exact logger
— no private API, no monkeypatch; it observes records, never alters them.
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lily_metrics import LilyMetricsCollector

_TAP_LOGGER = "lily_test_preemptive_tap"


def _tapped_collector():
    c = LilyMetricsCollector()
    f = c.attach_preemptive_tap(logger_name=_TAP_LOGGER)
    return c, f, logging.getLogger(_TAP_LOGGER)


def test_invalidation_warning_is_counted():
    c, f, lg = _tapped_collector()
    try:
        lg.warning(
            "preemptive generation invalidated after `on_user_turn_completed` "
            "because the transcript, chat context, tools, or tool choice changed"
        )
        lg.warning(
            "preemptive generation invalidated after `on_user_turn_completed` "
            "because the transcript, chat context, tools, or tool choice changed"
        )
        assert c.summary()["preemptive"] == {"used": 0, "invalidated": 2}
    finally:
        lg.removeFilter(f)


def test_used_debug_line_is_counted_when_debug_enabled():
    c, f, lg = _tapped_collector()
    lg.setLevel(logging.DEBUG)
    try:
        lg.debug("using preemptive generation")
        assert c.summary()["preemptive"]["used"] == 1
    finally:
        lg.removeFilter(f)
        lg.setLevel(logging.NOTSET)


def test_unrelated_records_pass_untouched_and_uncounted():
    c, f, lg = _tapped_collector()
    try:
        assert f.filter(
            logging.LogRecord(_TAP_LOGGER, logging.WARNING, __file__, 1,
                              "something else entirely", None, None)
        ) is True  # the tap never suppresses a record
        lg.warning("scheduling paused for unrelated reasons")
        assert "preemptive" not in c.summary()
    finally:
        lg.removeFilter(f)


def test_tap_matches_the_framework_message_verbatim():
    """The strings the tap matches must exist in the installed framework —
    if livekit-agents rewords them, this fails loudly instead of the
    counter silently reading zero forever."""
    import inspect
    from livekit.agents.voice import agent_activity

    src = inspect.getsource(agent_activity)
    assert "preemptive generation invalidated" in src
    assert "using preemptive generation" in src
    assert agent_activity.logger.name == "livekit.agents"
