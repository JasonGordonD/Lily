"""WO-LILY-HOTFIX-005 X8 + X9 — phantom speaker and split-utterance remedy.

X8: a solo session minted a phantom [S2] that "spoke" at 14:49:49 because
max_speakers stayed at the construction fallback (7). The roster-aware cap
(roster + 1) shrinks it to 2 for a solo table; the live swap is wired behind
LILY_STT_ROSTER_RETUNE (default off — the reconnect is STT-001 Q4's to
validate).

X9: `transcript arrives after turn has been committed. consider raising
min_delay in the endpointing options` — the framework naming its own remedy.
The endpointing floor is raised so a slow enhanced-point STT delivers before
the turn commits.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_config
import lily_stt_tuning
from lily_agent import LilyGame
from lily_scorekeeper import LilyScorekeeper


# -- X8: roster-aware cap -----------------------------------------------------

def test_solo_roster_cap_is_two():
    # solo ⇒ 1 + margin = 2 (never the wide-open fallback that mints phantoms)
    assert lily_stt_tuning.lily_max_speakers_for(1) == 2


def test_small_table_cap_tracks_roster():
    assert lily_stt_tuning.lily_max_speakers_for(2) == 3
    assert lily_stt_tuning.lily_max_speakers_for(3) == 4
    # never exceeds the construction fallback
    assert lily_stt_tuning.lily_max_speakers_for(20) == 7
    # unknown roster falls back
    assert lily_stt_tuning.lily_max_speakers_for(None) == 7


class _FakeAgent:
    def __init__(self):
        self.updates = []

    def update_options(self, **kwargs):
        self.updates.append(kwargs)


def _retune_game(roster_size, applied=7):
    game = LilyGame.__new__(LilyGame)
    game.sk = LilyScorekeeper("x8")
    # roster_size() reads from bound players
    game.sk.players = {f"P{i}": {"score": 0} for i in range(roster_size)}
    game.agent = _FakeAgent()
    game._stt_roster_retuned = False
    game._stt_max_speakers_applied = applied
    game._rebuilt_with = []

    def _rebuild(cap):
        game._rebuilt_with.append(cap)
        return object()  # stand-in STT

    game._stt_rebuild = _rebuild
    return game


def test_retune_disabled_by_default_no_swap(monkeypatch):
    monkeypatch.delenv("LILY_STT_ROSTER_RETUNE", raising=False)
    game = _retune_game(1)
    game._maybe_retune_stt_for_roster()
    assert game.agent.updates == []  # inert until Q4 enables it
    assert game._rebuilt_with == []


def test_retune_shrinks_solo_cap_when_enabled(monkeypatch):
    monkeypatch.setenv("LILY_STT_ROSTER_RETUNE", "on")
    game = _retune_game(1, applied=7)
    game._maybe_retune_stt_for_roster()
    # solo → cap 2 via a single live swap
    assert game._rebuilt_with == [2]
    assert len(game.agent.updates) == 1 and "stt" in game.agent.updates[0]
    assert game._stt_max_speakers_applied == 2
    assert game._stt_roster_retuned is True


def test_retune_never_grows_and_fires_once(monkeypatch):
    monkeypatch.setenv("LILY_STT_ROSTER_RETUNE", "on")
    # a table already at/above the target: no swap, marked done
    game = _retune_game(10, applied=7)
    game._maybe_retune_stt_for_roster()
    assert game.agent.updates == []
    assert game._stt_roster_retuned is True
    # second call is a no-op
    game._maybe_retune_stt_for_roster()
    assert game.agent.updates == []


def test_retune_failsafe_keeps_live_stt(monkeypatch):
    monkeypatch.setenv("LILY_STT_ROSTER_RETUNE", "on")
    game = _retune_game(1, applied=7)

    def _boom(cap):
        raise RuntimeError("swap failed")

    game._stt_rebuild = _boom
    game._maybe_retune_stt_for_roster()
    # the swap failed — the construction cap is retained, nothing crashed
    assert game._stt_max_speakers_applied == 7
    assert game.agent.updates == []


# -- X9: endpointing floor ----------------------------------------------------

def test_min_endpointing_delay_raised(monkeypatch):
    monkeypatch.delenv("LILY_STT_MIN_ENDPOINTING_DELAY", raising=False)
    # above the 0.5 framework default so a slow STT lands before commit
    assert lily_config.stt_min_endpointing_delay() == 0.6
    monkeypatch.setenv("LILY_STT_MIN_ENDPOINTING_DELAY", "0.9")
    assert lily_config.stt_min_endpointing_delay() == 0.9


def test_max_endpointing_delay_default_pinned(monkeypatch):
    monkeypatch.delenv("LILY_STT_MAX_ENDPOINTING_DELAY", raising=False)
    assert lily_config.stt_max_endpointing_delay() == 6.0


def test_entrypoint_uses_non_deprecated_turn_handling_endpointing():
    import inspect
    import lily_agent

    source = inspect.getsource(lily_agent.entrypoint)
    assert "endpointing=EndpointingOptions(" in source
    assert "mode=\"fixed\"" in source
    assert "min_endpointing_delay=" not in source
    assert "max_endpointing_delay=" not in source
