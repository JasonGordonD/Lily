"""LILY_SPINE operability line."""

from __future__ import annotations

from types import SimpleNamespace

from lily_agent import LilyGame, lily_spine_line


def test_spine_line_format():
    line = lily_spine_line(
        phase="answering",
        q=3,
        delivery="active:3",
        window="open",
        hold="clear",
        supply="ready",
    )
    assert line == (
        "LILY_SPINE | phase=answering q=3 delivery=active:3 "
        "window=open hold=clear supply=ready"
    )


def test_spine_fields_and_dedupe():
    game = LilyGame.__new__(LilyGame)
    game.sk = SimpleNamespace(
        question_number=1,
        answer_window_open=False,
    )
    game.ui_phase = "question"
    game._phase_hold = None
    game._pending_delivery_qnum = 1
    game._active_delivery_qnum = None
    game.armed_question = {"id": "q1"}
    game._steal_window = False
    game._hold_active = False
    game._question_pending = False
    game.game_started = True
    game.game_over = False
    game.next_question_ready = lambda: True
    game._last_spine_line = None

    fields = game.spine_fields()
    assert fields["phase"] == "question"
    assert fields["q"] == 1
    assert fields["delivery"] == "pending:1"
    assert fields["window"] == "closed"
    assert fields["hold"] == "clear"
    assert fields["supply"] == "ready"

    logged = []
    import lily_agent
    real = lily_agent.logger.info

    def _capture(fmt, *args):
        logged.append(fmt % args if args else fmt)

    lily_agent.logger.info = _capture
    try:
        first = game.log_spine()
        second = game.log_spine()  # dedupe
        assert first == second
        assert len(logged) == 1
        assert logged[0].startswith("LILY_SPINE |")
        game._pending_delivery_qnum = None
        game._active_delivery_qnum = 1
        game.log_spine()
        assert len(logged) == 2
        assert "delivery=active:1" in logged[1]
    finally:
        lily_agent.logger.info = real
