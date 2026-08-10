"""WO-LILY-HOTFIX-008 Z3 — a biometric NO_MATCH is not the probe resolving.

Live evidence, 2026-08-10, session `lily-938EFF-2260354c` (room
RM_V7MnLQBeFMi9):

  +3s    LILY_VOICE_ID | NO_MATCH | best=0.6968 threshold=0.75 — and
         `_voice_identity_resolved` flipped True, closing every N1/Y9
         hold surface.
  +43s   "You say you've played before, and my table card doesn't have
         you tonight, and I don't know why." A NEGATIVE memory claim,
         aired with the identity question still genuinely open.
  +125s  "call me Rami" — the stated-name door matched grp_0b07f989.
  +128s  "Wait — Rami! NOW I've got you. Reigning champ, last time you
         took it with three questions, underwater basket weaving and all."

Ninety seconds of "I don't know you" followed by knowing him completely.
The recognition beat was correct and simply arrived too late to be used.

Z3 pins HOTFIX-007 Y9's hold clause at the source: the no-match was one
route reporting, not the question closing. Name binding is mandatory
lobby flow, so the stated-name lookup is a probe route that WILL run —
until it reports (or memory lands, or a bounded hold expires) the probe
stays OUTSTANDING and no line may characterise memory at all. These
fixtures assert ORDERING, not wording: the first memory-bearing
statement must be the true one.
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_config
from test_hotfix006_n1_identity_race import _game as _greet_game
from test_false_clean_slate_p0 import _game as _state_game

_FIXTURE = Path(__file__).parent / "fixtures" / (
    "lily-938EFF-2260354c.transcripts.json"
)


def _rows():
    return json.loads(_FIXTURE.read_text())


def _live_gap_line() -> str:
    """The real aired defect line, straight from the session transcript."""
    for row in _rows():
        if "table card doesn't have you" in (row.get("text") or ""):
            return row["text"]
    raise AssertionError("fixture lost the live gap line")


def _live_callback_line() -> str:
    """The real recognition callback that landed 90 seconds too late."""
    for row in _rows():
        if "NOW I've got you" in (row.get("text") or ""):
            return row["text"]
    raise AssertionError("fixture lost the live callback line")


def _held_game(monkeypatch, **kw):
    """The session's state at +43s: probe reported NO_MATCH, name door
    untried, no memory landed."""
    monkeypatch.setattr(lily_config, "voice_identity_enabled", lambda: True)
    g = _greet_game(resolved=True, **kw)
    g._voice_identity_no_match_at = time.time()
    g._identity_name_door_checked = False
    return g


# -- the defect ---------------------------------------------------------------


def test_no_match_keeps_the_probe_open_while_the_name_door_is_untried(
    monkeypatch,
):
    """THE fixture. At +43s the probe had 'resolved' on a 0.6968 no-match
    while the route that would land the match at +128s had not run. The
    question was open; the hold must be too."""
    g = _held_game(monkeypatch)
    assert g.identity_probe_outstanding() is True
    assert g.can_claim_empty_memory() is False


def test_the_live_gap_line_airs_under_an_active_override(monkeypatch):
    """Ordering: at the moment the real line aired, the greet override
    forbidding memory-characterising speech must be in force and must
    come AFTER the gap-naming beat it countermands."""
    assert "table card doesn't have you" in _live_gap_line()
    out = _held_game(monkeypatch).greeting_instructions()
    assert "MEMORY IS UNRESOLVED" in out
    assert out.index("MEMORY IS UNRESOLVED") > out.index("table card")


def test_the_match_landing_closes_the_hold_and_recognition_speaks_first(
    monkeypatch,
):
    """Ordering, not wording: while held, no memory-characterising line;
    when the name door lands the match, the FIRST memory-bearing surface
    is the recognition beat — the true thing."""
    g = _held_game(monkeypatch)
    assert g.identity_probe_outstanding() is True

    # +125s: "call me Rami" → name door match → memory lands.
    g.memory_block = "[RETURNING TABLE] " + _live_callback_line()
    g.memory_total_games = 12
    g.memory_player_names = ["Rami", "Rhonda", "Chris"]
    assert g.identity_probe_outstanding() is False
    assert g.can_claim_empty_memory() is False
    text = g.greeting_instructions()
    assert "memory KNOWS this TABLE" in text
    assert "MEMORY IS UNRESOLVED" not in text


def test_state_block_holds_the_card_gap_class_while_checking(monkeypatch):
    """The per-turn conditioning (the surface live at +43s) must name the
    card-gap class, not only clean-slate phrasing."""
    monkeypatch.setattr(lily_config, "voice_identity_enabled", lambda: True)
    g = _state_game(resolved=True)
    g._voice_identity_no_match_at = time.time()
    g._identity_name_door_checked = False
    g.sk.build_state_block = lambda: "STATE"
    g.acoustic = None
    block = g.build_state_block()
    assert "identity: STILL CHECKING" in block
    assert "do not claim empty memory" in block
    assert "card/ledger doesn't have" in block


# -- Y9's honesty is timing-scoped, never regressed ---------------------------


def test_name_door_reported_empty_permits_the_honest_gap_line(monkeypatch):
    """Negative case. Once the stated-name lookup has RUN and found
    nothing, the gap is a known fact — Y9's honest 'card doesn't have
    you' class is permitted again, claimed returner included."""
    g = _held_game(monkeypatch)
    g._identity_name_door_checked = True
    g._returner_claim_seen = True
    assert g.identity_probe_outstanding() is False
    # The live line's class must NOT be rewritten post-resolution — it was
    # a real improvement over false clean-slate claims.
    assert g.must_rewrite_false_empty_claim(_live_gap_line()) is False
    out = g.greeting_instructions()
    assert "table card doesn't have you" in out
    assert "MEMORY IS UNRESOLVED" not in out


def test_a_plain_resolved_probe_without_a_no_match_stamp_is_closed(
    monkeypatch,
):
    """PROTECTED (N1 semantics). A resolved probe with no no-match stamp —
    a match, or a probe that never ran — stays closed exactly as before."""
    monkeypatch.setattr(lily_config, "voice_identity_enabled", lambda: True)
    g = _greet_game(resolved=True)
    assert g.identity_probe_outstanding() is False


# -- the hold is bounded ------------------------------------------------------


def test_the_hold_expires_rather_than_gagging_greeting_content_forever(
    monkeypatch,
):
    """Timeout case. An anonymous table that never states a name must not
    be held about memory indefinitely — on expiry the question resolves
    empty and honest gap-naming is available."""
    g = _held_game(monkeypatch)
    g._voice_identity_no_match_at = (
        time.time() - lily_config.identity_no_match_hold_seconds() - 1.0
    )
    assert g.identity_probe_outstanding() is False
    assert g.can_claim_empty_memory() is True


def test_the_default_hold_outlasts_the_live_sessions_gap(monkeypatch):
    """The observed no-match→match gap was 125 seconds; a default hold
    shorter than that re-ships the defect."""
    assert lily_config.identity_no_match_hold_seconds() > 125.0


def test_hold_disabled_by_config_restores_pre_z3_behaviour(monkeypatch):
    g = _held_game(monkeypatch)
    monkeypatch.setattr(
        lily_config, "identity_no_match_hold_seconds", lambda: 0.0
    )
    assert g.identity_probe_outstanding() is False
