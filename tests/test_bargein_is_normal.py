"""Barge-in is the steady state of this game, not an error path.

Operator directive, 2026-08-09: "it's a game that's going to have a lot of
barging in, a lot of shouting out, there's going to be a lot of
interruptions like this — this is normal."

Live evidence, session `lily-2C489B-a61fb6d9` (2026-08-08 22:45:30 ->
22:52:31 ET, deployed sha 72fd25c). Seven minutes, ZERO questions played,
one arsenal entry burned:

  22:48:55  arsenal_861712c7 armed; lily_asked_history row written
  22:49:37  delivery cut     "…hinting at much more with a knowing"
  22:49:47  delivery cut     same words, from the top
  22:50:05  delivery cut     same words, from the top
  22:50:47  "when you load a picture… what the fuck is going on?"

`lily_answers`: no rows. `score_ledger`: []. `answer_window_open`: false on
all 25 addressee rows, including the ones stamped phase='question'. And the
screenshot he pasted, preserved in the session's status_notes, shows the
LOBBY — "She's listening / START THE QUESTIONS" — while she described a
photograph whose signed URL was sitting in `current_question.image_url`.

Three separate mechanisms all treated "she got cut off" as "it didn't
happen", and each one is pinned below.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_recognition_variety import _make_game

BURLESQUE = {
    "id": "arsenal_861712c7-cea1-40c7-beb1-d9566b4e7a87",
    "prompt": (
        "This image captures a burlesque performer mid-tease, her feather "
        "fans hinting at much more with a knowing smile. What decade is "
        "this from?"
    ),
    "canonical_answer": "the 1930s",
    "category": "pictures",
    "image_url": "https://example.test/lily-arsenal/burlesque.jpg?token=x",
}


class _Recorder:
    """Stands in for the two fire-and-forget writes so the fixtures can
    assert on WHETHER they happened, not on Supabase."""

    def __init__(self):
        self.published = []
        self.burned = []


def _armed_game(monkeypatch, *, ui_phase="lobby", phase_hold="lobby"):
    """monkeypatch, not module mutation: both seams patched here are shared
    global state (`asyncio.ensure_future`, `lily_bank.lily_record_asked`),
    and leaking either one out of this file silently rewrites what the
    HOTFIX-006 ledger fixtures observe."""
    import lily_agent
    import lily_bank

    g = _make_game()
    g.armed_question = dict(BURLESQUE)
    g.sk.question_number = 1
    g.eliminated = []
    g.ui_phase = ui_phase
    g._phase_hold = phase_hold
    g._glass_published_qnum = None
    g._durable_asked_qnum = None
    g.supabase = object()  # non-None: the burn path is live
    rec = _Recorder()
    g.publish_metadata = lambda *a, **k: rec.published.append((a, k)) or None
    g.publish_attributes_nowait = lambda: None
    # The production paths hand coroutines to ensure_future; these fixtures
    # replace the coroutine factories themselves, so nothing needs a loop.
    monkeypatch.setattr(lily_agent.asyncio, "ensure_future", lambda coro: coro)
    monkeypatch.setattr(
        lily_bank, "lily_record_asked",
        lambda sb, gid, q, sid: rec.burned.append(q.get("id")),
    )
    return g, rec


# -- the glass deadlock -------------------------------------------------------


def test_the_question_reaches_the_glass_when_it_starts_airing(monkeypatch):
    """THE fixture. The publish must not wait for the delivery to FINISH —
    a barged delivery never finishes, and the player is left staring at the
    lobby while she talks about a picture."""
    g, rec = _armed_game(monkeypatch)
    assert g.publish_question_to_glass(reason="playout_started") is True
    assert rec.published, "the question never reached the glass"
    kwargs = rec.published[0][1]
    assert kwargs["image_url"] == BURLESQUE["image_url"]


def test_the_lobby_hold_drops_when_the_question_starts_airing(monkeypatch):
    """`_phase_hold` pinned the published phase to "lobby" until the window
    opened. The window never opened, so the board never turned over."""
    g, rec = _armed_game(monkeypatch)
    g.publish_question_to_glass(reason="playout_started")
    assert g._phase_hold is None, (
        "the glass was still held on the lobby while the question aired"
    )


def test_the_glass_publish_is_idempotent_per_question(monkeypatch):
    """Three cut re-airs must not republish three times — and the
    window-open backstop must no-op when the question is already up."""
    g, rec = _armed_game(monkeypatch)
    g.publish_question_to_glass(reason="playout_started")
    assert g.publish_question_to_glass(reason="playout_started") is False
    assert g.publish_question_to_glass(reason="window_open") is False
    assert len(rec.published) == 1


def test_the_image_pending_confirm_arms_at_air_not_at_window_open(monkeypatch):
    """B4's speak-gate needs the pending URL armed when the image is
    actually on its way to the client, or she can never say it's coming."""
    g, rec = _armed_game(monkeypatch)
    g.publish_question_to_glass(reason="playout_started")
    assert g._glass_image_pending_url == BURLESQUE["image_url"]


# -- the burn -----------------------------------------------------------------


def _arm_next_question_source() -> str:
    """The body of arm_next_question, read off the module. These two
    fixtures are STRUCTURAL: the defect was not a wrong value, it was a
    write living at the wrong seam, and the only way to pin that is to
    assert on where the call sites are."""
    src = (
        Path(__file__).resolve().parent.parent / "lily_supply.py"
    ).read_text(encoding="utf-8")
    start = src.index("    def arm_next_question(")
    end = src.index("\n    def ", start + 10)
    return src[start:end]


def test_a_question_that_never_aired_is_not_burned():
    """THE fixture for the arsenal. `lily_record_asked` fired at ARM. Entry
    861712c7 is in lily_asked_history for grp_0b07f989 stamped 22:48:55 and
    can never be served to that table again — for a question nobody heard.

    Seven minutes, zero questions played, one generated-and-gated picture
    entry spent on nobody."""
    assert "lily_record_asked" not in _arm_next_question_source(), (
        "the durable burn is back at arm time — a question the table never "
        "heard is being spent forever"
    )


def test_the_burn_lands_once_the_question_has_gone_to_air(monkeypatch):
    g, rec = _armed_game(monkeypatch)
    assert g.record_question_asked(reason="playout_started") is True
    assert rec.burned == [BURLESQUE["id"]]


def test_the_burn_is_idempotent_across_cut_reairs(monkeypatch):
    """Three cut deliveries of one question are one asked-history row."""
    g, rec = _armed_game(monkeypatch)
    g.record_question_asked(reason="playout_started")
    g.record_question_asked(reason="playout_started")
    g.record_question_asked(reason="window_open")
    assert rec.burned == [BURLESQUE["id"]]


def test_the_in_session_mirror_still_fills_at_arm():
    """PROTECTED. Moving the DURABLE burn must not let the same question be
    drawn twice inside one session — the in-memory mirror still fills at
    arm, and that is what the next draw reads."""
    assert "self.asked_history.append(" in _arm_next_question_source(), (
        "the in-session no-repeat mirror left arm time; this session can "
        "now draw the same question twice"
    )


# -- the self-repetition ------------------------------------------------------


def _turn_game():
    g = _make_game()
    g.transcripts = None  # persistence is not what these fixtures measure
    return g


def test_a_cut_turn_is_recorded_once_not_once_per_interruption():
    """THE fixture. Every copy below is marked cut off, and every one was a
    separate row in lily_transcripts AND a separate entry in sk.agent_turns
    — which is her own conversational context. She read her line back four
    times and said it again: "Okay, now you're repeating yourself." """
    g = _turn_game()
    line = (
        "Great to meet you, Rami! Are you flying solo tonight, or is there "
        "anyone else hanging out with you around the"
    )
    for _ in range(4):
        g.record_agent_turn(line, act_keys=[], interrupted=True)
    assert g.sk.agent_turns.count(line) == 1, (
        "one cut turn became four entries in her own context"
    )


def test_the_first_cut_turn_is_still_recorded_and_still_marked():
    """PROTECTED. A cut turn partially played and belongs in the record,
    marked — that behaviour is why the exemption existed and it stays."""
    g = _turn_game()
    g.record_agent_turn("Round one, pictures — jacket's off.",
                        act_keys=[], interrupted=True)
    assert g.sk.agent_turns == ["Round one, pictures — jacket's off."]
    assert g.sk.transcript_buffer[-1]["text"].endswith("…[cut off]")


def test_distinct_cut_turns_all_survive():
    """PROTECTED. The guard is verbatim-repeat, not "drop cut turns"."""
    g = _turn_game()
    for line in (
        "Round one, pictures — jacket's off, here we go with the first one.",
        "This image captures a burlesque performer mid-tease, feather fans.",
    ):
        g.record_agent_turn(line, act_keys=[], interrupted=True)
    assert len(g.sk.agent_turns) == 2


def test_short_turns_are_still_exempt():
    """PROTECTED. An honest "Yeah" or "Nice one!" may legitimately recur;
    the 15-character floor is unchanged."""
    g = _turn_game()
    for _ in range(3):
        g.record_agent_turn("Yeah", act_keys=[], interrupted=True)
    assert g.sk.agent_turns.count("Yeah") == 3
