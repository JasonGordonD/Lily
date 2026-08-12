"""The double greeting (live 2026-08-12 15:26 ET): recognition landed 8s
after the greet and the late-recognition beat aired as a FULL second
greeting — the opener repeated verbatim with "Took me a second... Welcome
back" bolted on. Two fixes pinned here: the beat's instructions now
forbid the reprise explicitly, and the dispatch rides gated_say (one of
the five raw instructed_reply lanes from the Y10 review, now funneled) —
so a hold refuses it and the pending bit re-arms instead of burning it.
"""

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_say_gate
from lily_agent import LilyGame
from lily_scorekeeper import LilyScorekeeper


def _game(**kw):
    game = LilyGame.__new__(LilyGame)
    game.sk = LilyScorekeeper("regreet")
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game.memory_block = "[RETURNING TABLE]\n18 games."
    game._late_recognition_fired = False
    game._late_recognition_pending = False
    game._recognized_at_greet = False
    game.game_started = True  # past the door path
    game.prefs = {}
    for k, v in kw.items():
        setattr(game, k, v)
    return game


def test_beat_dispatches_through_the_funnel_with_anti_reprise():
    game = _game()
    calls = []
    game.gated_say = (
        lambda key, act, instr, source=None, **kw:
        calls.append((key, act, instr, source)) or True
    )
    game.late_recognition_blocked_reason = lambda: None
    assert game.maybe_fire_late_recognition() is True
    (key, act, instr, source), = calls
    assert key is None and act == "late_recognition"
    assert "do NOT re-introduce yourself" in instr
    assert "ALREADY heard your greeting" in instr
    assert game._late_recognition_fired is True


def test_refused_beat_rearms_instead_of_burning():
    game = _game()
    game.gated_say = lambda *a, **kw: False  # hold/floor/flight refusal
    game.late_recognition_blocked_reason = lambda: None
    assert game.maybe_fire_late_recognition() is False
    assert game._late_recognition_fired is False
    assert game._late_recognition_pending is True


def test_no_raw_instructed_reply_in_the_beat():
    src = inspect.getsource(LilyGame.maybe_fire_late_recognition)
    assert "self.instructed_reply(" not in src
    assert "self.gated_say(" in src
