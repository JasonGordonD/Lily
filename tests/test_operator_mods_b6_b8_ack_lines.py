"""WO-LILY-OPERATOR-MODS-001 addendum — the operator's wording for the B6
acknowledgment, verbatim, the whole register:

  Success: "Got it, Rami — done."
  Failure: "Tried, and it didn't take — that's on my side."
  Rule: "One sentence, names the operator, confirms or owns. The worker
  may not extend either."

"Rami" in the operator's text is the name the operator door confirmed —
the bound roster player behind the voice door — never a literal. Every
test drives the real claim path (transcript final → maybe_route_stop →
handle_operator_claim → gated_say); none asserts on source text.
"""

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_bind_dispute_p0 import (  # noqa: E402
    _final, _make_game as _dispute_game, _run,
)
from test_operator_mods_b6_b8 import (  # noqa: E402
    LIVE_CLAIM, OPERATOR_GROUP, _armed_next, _confirm_operator,
)

SUCCESS_RAMI = "Got it, Rami — done."
FAILURE = "Tried, and it didn't take — that's on my side."


def _claim_game(monkeypatch, *, name="Rami"):
    game = _dispute_game()
    _armed_next(game)
    if name != "Rami":
        game.sk.bind_speaker("S1", name)
    _confirm_operator(game, monkeypatch)
    return game


def test_success_line_names_the_operator_the_door_confirmed(
    monkeypatch, caplog,
):
    game = _claim_game(monkeypatch)
    at = time.time()

    def _go():
        with caplog.at_level(logging.INFO):
            _final(game, LIVE_CLAIM, at)
        assert game.session.said == [SUCCESS_RAMI]
        assert game.pause_sticky() is True
        assert game.operator_display_name() == "Rami"
        assert any(
            "LILY_SAY | act=operator_ack" in r.getMessage()
            for r in caplog.records
        )

    _run(_go)


def test_success_line_uses_the_bound_name_never_a_literal(monkeypatch):
    game = _claim_game(monkeypatch, name="Dana")
    at = time.time()

    def _go():
        _final(game, LIVE_CLAIM, at)
        assert game.session.said == ["Got it, Dana — done."]

    _run(_go)


def test_failure_line_airs_when_the_hold_action_fails(monkeypatch, caplog):
    """The routed action (the sticky hold) raises: the claim is still the
    operator's (accepted, counted), the failure line airs INSTEAD of the
    success line, and the failure is on the record."""
    game = _claim_game(monkeypatch)

    def _boom(reason):
        raise RuntimeError("hold machinery down")

    monkeypatch.setattr(game, "enter_hold", _boom)
    at = time.time()

    def _go():
        with caplog.at_level(logging.INFO):
            _final(game, LIVE_CLAIM, at)
        assert game.session.said == [FAILURE]
        assert game.voice_identity_receipt()["operator"]["claims"] == 1
        assert any(
            "LILY_OPERATOR | ACTION_FAILED" in r.getMessage()
            for r in caplog.records
        )

    _run(_go)


def test_a_refused_claim_airs_neither_line(monkeypatch):
    """A claim from a speaker the voice door did not confirm is not an
    operator action that failed — it is refused, and the register stays
    silent (the prompt rail answers)."""
    game = _dispute_game()
    _armed_next(game)
    game.group_id = OPERATOR_GROUP
    monkeypatch.setenv("LILY_OPERATOR_GROUP_IDS", OPERATOR_GROUP)
    game.prefs = {}
    game.identity_confirmed_source = "name_stated"
    at = time.time()

    def _go():
        _final(game, LIVE_CLAIM, at)
        assert game.session.said == []

    _run(_go)


def test_no_confirmed_name_means_no_code_ack(monkeypatch):
    """The register REQUIRES a name. With the door confirmed but no bound
    roster name behind it (an unbound label, an empty memory), nothing is
    invented: no code ack, the organic lane answers under the directive."""
    game = _dispute_game()
    game.game_started = True
    game.sk.round = 1
    game.sk.set_phase("round")
    _confirm_operator(game, monkeypatch)
    game.memory_player_names = []
    at = time.time()

    def _go():
        _final(game, LIVE_CLAIM, at)
        assert game.operator_display_name() is None
        assert game.session.said == []
        assert "OPERATOR" in (game._explain_request_note or "")

    _run(_go)
