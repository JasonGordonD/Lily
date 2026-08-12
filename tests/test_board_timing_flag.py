"""C9 (WO-LILY-HOSTLOOP-001): board render timing behind one flag.

The playout-start gating already existed (the lily-2C489B deadlock fix:
publish_question_to_glass wired from note_playout_started, window-open
and reconnect as idempotent backstops). C9 adds the single reversible
flag: LILY_BOARD_ON_PLAYOUT_START, default true. False reverts to the
legacy arm-time post (glass may lead the voice) as the rollback
position.
"""

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_config
import lily_speech_delivery
from lily_agent import LilyGame


def test_default_is_playout_start(monkeypatch):
    monkeypatch.delenv("LILY_BOARD_ON_PLAYOUT_START", raising=False)
    assert lily_config.board_on_playout_start() is True


def test_playout_start_wiring_is_the_primary_path():
    """Source pins: the glass post fires from note_playout_started, and
    the arm path posts ONLY under the reverted flag."""
    src = inspect.getsource(lily_speech_delivery)
    assert 'publish_question_to_glass(reason="playout_started")' in src
    import lily_agent

    agent_src = inspect.getsource(lily_agent)
    i = agent_src.index('publish_question_to_glass(reason="serve_time_flag")')
    guard = agent_src.rindex("board_on_playout_start", 0, i)
    # The serve-time post sits behind the flag check (within the same block).
    assert i - guard < 400


def test_flag_false_reverts_to_arm_time(monkeypatch):
    monkeypatch.setenv("LILY_BOARD_ON_PLAYOUT_START", "false")
    assert lily_config.board_on_playout_start() is False
