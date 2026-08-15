"""WO-LILY-BIND-DISPUTE-001 addendum — the lobby-settle start gate.

THE FALSE START (live lily-359C62-5613a25a, 2026-08-15 13:47 EDT call,
fixture tests/fixtures/live_20260815_1347_gameflow.txt): Lily started
round one UNPROMPTED — no start phrase exists anywhere in the session.
Root cause, named: the per-final AUTO-START safety net
(_maybe_auto_start_after_lobby) fired at the 17:48:46 joke-resolution
final because every legacy guard passed —
  * roster_size() read 2, but BOTH rows were PLACEHOLDERS ("Rami" +
    ghost "UU"; lily_sessions.scorekeeper_state has placeholder:true on
    both) — one hearable human;
  * quiet-after-last-user-turn read ≈28s, but the "quiet" was the
    player waiting out Lily's unanswered "Why are these three players
    here?" (17:48:18) — an UNRESOLVED player address, not a settled
    lobby (the stamp also lags the very final that triggers the check);
  * grace 98s ≥ 90s;
  * and NOTHING on the auto path required a player to have expressed
    start intent, ever.

Fix under test: round one dispatches ONLY on (a) a deterministic player
start-intent fact (spoken detector / UI control / setup parser — never
model judgment: lily_begin_round verifies the same fact) AND (b) a
settled lobby (no in-flight bind, no unresolved address/clarify, no
dispute-hold). A start REQUEST while unsettled gets one line ("One sec
— locking the table first.") and dispatches itself when settled.
Game-lifecycle reset paths are untouched (WO-5 owns restart).
"""

import asyncio
import time
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_config
import lily_scorekeeper
from lily_agent import LilyAgent, LilyGame
from lily_scorekeeper import LilyScorekeeper

FIXTURE_0815 = (
    Path(__file__).resolve().parent
    / "fixtures" / "live_20260815_1347_gameflow.txt"
)


def _game(session_id: str = "lily-359C62-5613a25a") -> LilyGame:
    g = LilyGame.bare()
    g.sk = LilyScorekeeper(session_id)
    g.game_started = False
    g.game_over = False
    g.next_question = {"id": "q1", "prompt": "x"}
    g.armed_question = None
    g.session_started_at = time.time() - (
        lily_config.auto_start_lobby_grace_seconds() + 10.0
    )
    g._last_user_turn_at = time.monotonic() - (
        lily_config.auto_start_quiet_seconds() + 10.0
    )
    g.ui_phase = "lobby"
    g.memory_block = ""
    g.supabase = None
    g.acoustic = None
    return g


def _roster_of_two(g: LilyGame) -> None:
    """The live shape: two rostered names, both effectively placeholders
    of one hearable human — every legacy roster/quiet/grace guard passes."""
    g.sk.bind_speaker("Rami", "Rami")
    g.sk.bind_speaker("UU", "UU")


def _fixture_player_lines() -> list:
    lines = []
    for line in FIXTURE_0815.read_text().splitlines():
        if line[:1].isdigit() and " Rami: " in line:
            lines.append(line.split(": ", 1)[1])
    return lines


# ---------------------------------------------------------------------------
# The 13:48–13:49 sequence, failing-first: no start intent -> no round one
# ---------------------------------------------------------------------------


def test_auto_start_never_fires_without_player_start_intent():
    """FAILING-FIRST (addendum): the exact 17:48:46 replay — every
    legacy guard satisfied, no start phrase anywhere. Pre-WO this called
    start_game; now the net verifies the intent fact and defers."""
    g = _game()
    _roster_of_two(g)
    calls = []

    async def _record_start(source: str) -> None:
        calls.append(source)

    g.start_game = _record_start

    g._maybe_auto_start_after_lobby()

    assert calls == []
    assert g.game_started is False


def test_no_0815_final_trips_any_start_detector():
    """Regression pin: none of the live session's player finals reads as
    start intent through ANY deterministic channel — the session truly
    contained no start ask."""
    lines = _fixture_player_lines()
    assert len(lines) >= 8
    for text in lines:
        assert lily_scorekeeper.lily_detect_control_command(text) != (
            "start_game"
        ), text
        assert lily_scorekeeper.lily_is_bare_start_intent(text) is False, text
        assert lily_scorekeeper.lily_parse_lobby_setup_intents(text)[
            "start"
        ] is False, text


def test_auto_start_fires_with_intent_and_settled_lobby():
    """The net still works — intent recorded, lobby settled: it starts."""
    g = _game()
    _roster_of_two(g)
    g.note_player_start_intent(source="voice_command", text="let's play")
    calls = []

    async def _record_start(source: str) -> None:
        calls.append(source)

    g.start_game = _record_start

    async def _go():
        g._maybe_auto_start_after_lobby()
        await asyncio.sleep(0)

    asyncio.run(_go())
    assert calls == ["auto_after_lobby"]


# ---------------------------------------------------------------------------
# The gate itself
# ---------------------------------------------------------------------------


def test_start_gate_reads():
    g = _game()
    assert g.start_intent_present() is False
    assert g.start_gate_blocked_reason() == "no_start_intent"

    g.note_player_start_intent(source="voice_command", text="start the game")
    assert g.start_intent_present() is True
    assert g.start_gate_blocked_reason() is None

    # Each unsettled arm is named (and each is self-clearing state):
    g._question_pending = True
    assert g.start_gate_blocked_reason() == "lobby_unsettled:question_pending"
    g._question_pending = False
    g.pending_clarify = {"Rami": {"question_number": 0}}
    assert g.start_gate_blocked_reason() == "lobby_unsettled:pending_clarify"
    g.pending_clarify = {}
    g._awaiting_address_since = time.time()
    assert g.start_gate_blocked_reason() == (
        "lobby_unsettled:address_unanswered"
    )
    g._awaiting_address_since = 0.0
    g._last_bind_at = time.time()  # a name bind just landed
    assert g.start_gate_blocked_reason() == "lobby_unsettled:intake_active"


def test_setup_parser_start_counts_as_intent():
    """'I want to play' (the multi-intent setup parser's start flag) is a
    deterministic channel too."""
    g = _game()
    g._setup_start_requested = True
    assert g.start_intent_present() is True


def test_unresolved_player_address_blocks_the_start():
    """The live 17:48:18 state pinned: 'Why are these three players
    here?' unanswered = an unsettled lobby, whatever the quiet clock
    says."""
    g = _game()
    _roster_of_two(g)
    g.note_player_start_intent(source="voice_command", text="let's play")
    g._awaiting_address_since = time.time()  # the unanswered address
    calls = []

    async def _record_start(source: str) -> None:
        calls.append(source)

    g.start_game = _record_start
    g._maybe_auto_start_after_lobby()
    assert calls == []


# ---------------------------------------------------------------------------
# Start request while unsettled: one line, then dispatch when settled
# ---------------------------------------------------------------------------


class _FakeSession:
    def __init__(self) -> None:
        self.instructions: list[str] = []
        self.said: list[str] = []

    def generate_reply(self, instructions: str) -> None:
        self.instructions.append(instructions)

    def say(self, text, *a, **k):
        self.said.append(text)
        return None


def test_unsettled_start_request_says_one_line_and_dispatches_on_settle():
    import lily_say_gate

    g = _game()
    _roster_of_two(g)
    g.session = _FakeSession()
    g.agent = None
    g.say_registry = lily_say_gate.SpeechActRegistry()
    g.pending_clarify = {"Rami": {"question_number": 0}}  # unsettled

    async def _go():
        await g.start_game(source="voice")  # the player asked to start
        # Not started; the one-line hold aired; the watcher is armed.
        assert g.game_started is False
        assert any("locking the table" in s.lower() for s in g.session.said)
        assert g._pending_start_task is not None

        # From here the watcher owns the dispatch — swap in a recorder so
        # the settle fire is observable without start_game's heavy deps.
        calls = []

        async def _record_start(source: str) -> None:
            calls.append(source)
            g.game_started = True

        g.start_game = _record_start
        g.pending_clarify = {}  # the lobby settles
        await asyncio.sleep(1.2)  # one watcher poll
        assert calls == ["voice_settled"]

    asyncio.run(_go())


def test_second_unsettled_request_does_not_restack_the_line():
    import lily_say_gate

    g = _game()
    g.session = _FakeSession()
    g.agent = None
    g.say_registry = lily_say_gate.SpeechActRegistry()
    g.pending_clarify = {"Rami": {"question_number": 0}}

    g._defer_start_until_settled("voice")
    g._defer_start_until_settled("voice")
    holds = [s for s in g.session.said if "locking the table" in s.lower()]
    assert len(holds) == 1


# ---------------------------------------------------------------------------
# The tool path verifies the detector-set fact (never model judgment)
# ---------------------------------------------------------------------------


def test_begin_round_refuses_without_detector_set_intent():
    g = _game()
    _roster_of_two(g)
    agent = LilyAgent.__new__(LilyAgent)
    agent._game = g
    msg = asyncio.run(LilyAgent.lily_begin_round.__wrapped__(agent, None))
    assert "no_start_intent" in msg
    assert g.game_started is False


def test_begin_round_queues_settled_start_when_unsettled():
    import lily_say_gate

    g = _game()
    _roster_of_two(g)
    g.session = _FakeSession()
    g.agent = None
    g.say_registry = lily_say_gate.SpeechActRegistry()
    g.note_player_start_intent(source="voice_command", text="let's play")
    g._question_pending = True  # she asked something; nobody answered
    agent = LilyAgent.__new__(LilyAgent)
    agent._game = g

    async def _go():
        msg = await LilyAgent.lily_begin_round.__wrapped__(agent, None)
        assert "lobby_unsettled" in msg
        assert g.game_started is False
        assert g._pending_start_task is not None
        # The tool reply carries the hold line — the watcher stays silent.
        assert g.session.said == []
        g._pending_start_task.cancel()
        try:
            await g._pending_start_task
        except asyncio.CancelledError:
            pass

    asyncio.run(_go())


def test_voice_and_rpc_sources_note_their_own_intent():
    """A player-initiated source IS the intent: the deterministic spoken
    command path records the fact before dispatch."""
    g = _game()
    g.note_user_start_intent("Let's start the game", command="start_game")
    assert g.start_intent_present() is True
    assert (g._player_start_intent or {}).get("source") == "voice_command"
