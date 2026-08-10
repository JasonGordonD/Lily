"""WO-LILY-HOTFIX-009 W1 — the diamond fixture: an auditable path to
reverse a wrong verdict.

Evidence base — live session RM_RQTZZanrHURF (2026-08-10 05:30–05:41),
the real transcript rows (supabase/lily_transcripts.json):

* [48]/[53] Lily delivers "What is the hardest naturally occurring
  substance found on Earth?" — canonical answer: diamond.
* [56] 05:37:26 Rami: "Answer to your fucking question is diamond."
* [57] Lily denies it; [62] "diamond is right. You had it. Just past the
  window, so it doesn't score. Still two." — a clock/window applied on a
  RELAXED round (rows [22],[66] confirm relaxed, no timer).
* [67] 05:39:02 the lead-defect sentence: "Relaxed means no buzzer — I
  shouldn't have dinged you on a clock that isn't running. Diamond was the
  answer, you had it. Score stays two because the board already locked."
* [69] the contest: "you violated the rules that we had agreed upon, which
  was relaxed, aka no timer… and now you're gonna keep my score at two."

W4 fixes the CAUSE (a clock in relaxed pacing); W1 is the REMEDY: the
ledger could be apologised over but never corrected. These tests pin the
correction CONTRACT — append-only, grounds-bound, once-per-verdict,
standings == ledger-plus-corrections, narrated — and prove it separates a
correction (bound to a real prior verdict) from a fabrication.

The scorekeeper-contract tests import only lily_scorekeeper. The tool +
narration tests import lily_agent (livekit boundary, per test_award_gate).
"""

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_scorekeeper
from lily_scorekeeper import LilyScorekeeper


# -- real diamond-session identifiers (from the exported rows) ----------------
# lily_answers.json + SESSION_FACTS.md: the diamond question is q_7391
# (question_index 5); Rami's two prior points were kb_118 (Isaac Newton, q1)
# and q_3914 (Carbon, q3). The correct in-window answer
# (addressee_log.id=819, window_open=true, fuzzy_matched=true) was IGNORED;
# the filler "It's on me." was mis-taken as the answer and ruled incorrect
# (lily_answers.id=134, cause "answer"), and "I said diamond." was then ruled
# "late" (id=135, cause "late_answer") — both 0 pts. Rami's real final: 2.
DIAMOND_PROMPT = "What is the hardest naturally occurring substance found on Earth?"
DIAMOND_ANSWER = "diamond"
DIAMOND_TRUE_UTTERANCE = "Answer to your fucking question is diamond."   # id=819, ignored
DIAMOND_SCORED_UTTERANCE = "It's on me."                                 # id=134, mis-taken
DIAMOND_LATE_UTTERANCE = "I said diamond."                               # id=135, ruled late
DIAMOND_CONTEST = (
    "you violated the rules that we had agreed upon, which was relaxed, aka "
    "no timer. I had set it in time because time was infinite and I said it "
    "even faster than that henceforth. And now you're gonna keep my score at two"
)


def _rami_on_two_diamond_denied() -> LilyScorekeeper:
    """Reproduce the live pre-state through the production write path: Rami
    correct on two prior questions (kb_118, q_3914), then the diamond
    (q_7391, index 5) ruled INCORRECT on the mis-bound "It's on me." while
    the true in-window answer was ignored. Everything lands via
    record_result -> the single write path, so this is real ledger state
    keyed to the exported identifiers, not a hand-built dict."""
    sk = LilyScorekeeper("RM_RQTZZanrHURF")
    sk.bind_speaker("S1", "Rami")
    sk.set_pacing("relaxed")
    sk.start_question({"prompt": "Isaac Newton q", "canonical_answer": "-"})
    sk.record_result("Rami", correct=True, points=1, question_id="kb_118",
                     question_index=1, transcript="Isaac Newton.",
                     utterance_id="utt_kb_118")
    sk.start_question({"prompt": "Carbon q", "canonical_answer": "-"})
    sk.record_result("Rami", correct=True, points=1, question_id="q_3914",
                     question_index=3, transcript="Carbon.",
                     utterance_id="utt_q_3914")
    # The diamond question — correct answer denied; the wrong utterance was
    # scored incorrect (lily_answers.id=134). record_result(correct=False)
    # is exactly the row the live adjudication committed.
    sk.start_question({"prompt": DIAMOND_PROMPT, "canonical_answer": DIAMOND_ANSWER})
    sk.record_result(
        "Rami", correct=False, points=0, question_id="q_7391",
        question_index=5, transcript=DIAMOND_SCORED_UTTERANCE,
        utterance_id="utt_q_7391",
    )
    return sk


# ============================================================================
# The contest trigger (the intent surface — shared with W3, see report)
# ============================================================================

def test_contest_detector_fires_on_the_real_rule_violation_contest():
    # The diamond contest is a RULE-violation form, not "I was misheard".
    # On base this returns False (the regex had no branch for it) — a
    # biting needle. After W1 it fires.
    assert lily_scorekeeper.lily_detect_verdict_contest(DIAMOND_CONTEST) is True


def test_contest_detector_fires_on_the_held_score_phrasing():
    assert lily_scorekeeper.lily_detect_verdict_contest(
        "you're gonna keep my score at two"
    ) is True
    assert lily_scorekeeper.lily_detect_verdict_contest(
        "we agreed relaxed, no timer"
    ) is True


def test_contest_detector_stays_conservative():
    # A fresh trivia answer to a live question must never read as a contest.
    assert lily_scorekeeper.lily_detect_verdict_contest("the answer is diamond") is False
    assert lily_scorekeeper.lily_detect_verdict_contest("diamond") is False
    assert lily_scorekeeper.lily_detect_verdict_contest(
        "what is the hardest substance on earth"
    ) is False


# ============================================================================
# The correction contract (scorekeeper level)
# ============================================================================

def test_diamond_correction_restores_the_point_appends_row_with_grounds():
    # Canonical grounds = "answer_denied". The durable record (addressee_log
    # id=819: window_open=true, seconds_into_window=0.0, fuzzy_matched=true,
    # agent_action=ignored) proves the correct answer arrived IN window and
    # was ignored — Lily's "past the window" narration was confabulated. The
    # re-adjudication anchors on that row, not on her narration.
    sk = _rami_on_two_diamond_denied()
    assert sk.players["Rami"]["score"] == 2
    original = sk.ledger_row_for("Rami", "q_7391")
    assert original["correct"] is False and original["points"] == 0

    entry = sk.correct_verdict(
        "Rami", grounds="answer_denied", actor="player_contest", delta=1
    )
    assert entry is not None
    # Append-only: the original denial row is untouched.
    assert sk.ledger_row_for("Rami", "q_7391")["correct"] is False
    # A NEW correction row carries the delta, grounds, actor and provenance.
    assert entry["cause"] == "verdict_correction"
    assert entry["points"] == 1
    assert entry["grounds"] == "answer_denied"
    assert entry["actor"] == "player_contest"
    assert entry["corrects"]["question_id"] == "q_7391"
    assert entry["corrects"]["utterance_id"] == "utt_q_7391"
    # The point is back.
    assert sk.players["Rami"]["score"] == 3


def test_standings_equal_ledger_plus_corrections_and_reconcile_clean():
    sk = _rami_on_two_diamond_denied()
    sk.correct_verdict("Rami", grounds="answer_denied", delta=1)
    assert sk.ledger_scores()["Rami"] == 3
    assert sk.players["Rami"]["score"] == 3
    # The counter and the ledger-including-corrections never diverge.
    assert sk.reconcile_scores() == []


def test_correction_requires_a_prior_verdict_no_fabrication():
    # A correction can only amend a ruling that happened. With no verdict on
    # the ledger there is nothing to correct — the point cannot be minted.
    sk = LilyScorekeeper("RM_RQTZZanrHURF")
    sk.bind_speaker("S1", "Rami")
    assert sk.correct_verdict("Rami", grounds="answer_denied", delta=1) is None
    assert sk.players["Rami"]["score"] == 0
    assert sk.score_ledger == []


def test_spurious_contest_on_a_correct_answer_changes_nothing():
    # The answer already won its point — a restoration is refused, so a
    # contest can never stack a second point onto a right answer.
    sk = LilyScorekeeper("RM_RQTZZanrHURF")
    sk.bind_speaker("S1", "Rami")
    sk.start_question({"prompt": "p", "canonical_answer": "x"})
    sk.record_result("Rami", correct=True, points=1, question_id="q_ok",
                     transcript="x", utterance_id="utt_ok")
    before = list(sk.score_ledger)
    assert sk.correct_verdict("Rami", grounds="answer_denied", delta=1) is None
    assert sk.players["Rami"]["score"] == 1
    assert sk.score_ledger == before  # no ledger change


def test_double_correction_is_refused():
    sk = _rami_on_two_diamond_denied()
    assert sk.correct_verdict("Rami", grounds="wrong_rule", delta=1) is not None
    n = len(sk.score_ledger)
    assert sk.correct_verdict("Rami", grounds="wrong_rule", delta=1) is None
    assert len(sk.score_ledger) == n
    assert sk.players["Rami"]["score"] == 3


def test_unknown_grounds_is_refused():
    sk = _rami_on_two_diamond_denied()
    assert sk.correct_verdict("Rami", grounds="because_i_said_so", delta=1) is None
    assert sk.players["Rami"]["score"] == 2


def test_verdict_corrected_is_logged_with_grounds_actor_delta(caplog):
    sk = _rami_on_two_diamond_denied()
    with caplog.at_level(logging.INFO):
        sk.correct_verdict("Rami", grounds="wrong_rule",
                           actor="player_contest", delta=1)
    line = [r.getMessage() for r in caplog.records if "VERDICT_CORRECTED" in r.getMessage()]
    assert line, "the correction must hard-log VERDICT_CORRECTED"
    msg = line[0]
    assert "grounds=wrong_rule" in msg
    assert "actor=player_contest" in msg
    assert "delta=+1" in msg


def test_correction_survives_a_checkpoint():
    sk = _rami_on_two_diamond_denied()
    sk.correct_verdict("Rami", grounds="wrong_rule", delta=1)
    snap = sk.snapshot()

    restored = LilyScorekeeper("RM_RQTZZanrHURF")
    restored.bind_speaker("S1", "Rami")
    restored.rehydrate(snap)
    # Standings and the correction provenance both ride the checkpoint.
    assert restored.ledger_scores()["Rami"] == 3
    assert restored.players["Rami"]["score"] == 3
    assert restored.reconcile_scores() == []
    corr = [e for e in restored.score_ledger if e.get("cause") == "verdict_correction"]
    assert corr and corr[0]["grounds"] == "wrong_rule"
    assert corr[0]["corrects"]["question_id"] == "q_7391"


def test_ws4_idempotency_belt_is_intact_for_answer_cause():
    # The correction cause's belt-exemption must not have loosened the belt
    # for ordinary awards: a second positive answer-award on the same
    # (question_id, player) is still refused.
    sk = LilyScorekeeper("RM_RQTZZanrHURF")
    sk.bind_speaker("S1", "Rami")
    sk.start_question({"prompt": "p", "canonical_answer": "x"})
    sk.record_result("Rami", correct=True, points=1, question_id="q_dup",
                     transcript="x", utterance_id="u1")
    assert sk.players["Rami"]["score"] == 1
    # Duplicate dispatch of the same award — belt refuses, no double count.
    sk.record_result("Rami", correct=True, points=1, question_id="q_dup",
                     transcript="x", utterance_id="u2")
    assert sk.players["Rami"]["score"] == 1


# ============================================================================
# The tool + narration (agent level — she says what she is fixing)
# ============================================================================

def _agent_with(sk):
    from lily_agent import LilyAgent

    class _FakeGame:
        def __init__(self, sk):
            self.game_started = True
            self.sk = sk
            self.supabase = None
            self.events = []
            self.publish_nowait_calls = 0

        def send_event_nowait(self, event_type, payload):
            self.events.append((event_type, dict(payload)))

        def publish_attributes_nowait(self):
            self.publish_nowait_calls += 1

    agent = LilyAgent.__new__(LilyAgent)
    game = _FakeGame(sk)
    agent._game = game
    return agent, game


def _call(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_tool_corrects_the_diamond_verdict_and_narrates_the_fix():
    from lily_agent import LilyAgent

    sk = _rami_on_two_diamond_denied()
    agent, game = _agent_with(sk)
    msg = _call(LilyAgent.lily_correct_verdict.__wrapped__(
        agent, None, "Rami", "wrong_rule",
        "diamond was yours, and I'd put a clock on a relaxed round",
    ))
    # She names the fix and the new standing — justice done, not "locked".
    assert "goes back to Rami" in msg
    assert "On 3" in msg
    assert sk.players["Rami"]["score"] == 3
    assert game.publish_nowait_calls == 1
    assert game.events and game.events[0][0] == "verdict_corrected"
    assert game.events[0][1]["player"] == "Rami"
    assert game.events[0][1]["grounds"] == "wrong_rule"
    assert game.events[0][1]["delta"] == 1


def test_tool_refuses_a_spurious_contest_and_states_it_stands():
    from lily_agent import LilyAgent

    sk = LilyScorekeeper("RM_RQTZZanrHURF")
    sk.bind_speaker("S1", "Rami")
    sk.start_question({"prompt": "p", "canonical_answer": "x"})
    sk.record_result("Rami", correct=True, points=1, question_id="q_ok",
                     transcript="x", utterance_id="u")
    agent, game = _agent_with(sk)
    msg = _call(LilyAgent.lily_correct_verdict.__wrapped__(
        agent, None, "Rami", "answer_denied", "I swear I was robbed",
    ))
    assert "No correction made" in msg
    assert "never invent a point" in msg
    assert sk.players["Rami"]["score"] == 1  # no ledger change
    assert game.events == []
    assert game.publish_nowait_calls == 0


def test_tool_refuses_before_game_started():
    from lily_agent import LilyAgent

    sk = _rami_on_two_diamond_denied()
    agent, game = _agent_with(sk)
    game.game_started = False
    msg = _call(LilyAgent.lily_correct_verdict.__wrapped__(
        agent, None, "Rami", "wrong_rule", "x",
    ))
    assert "once a round is underway" in msg
    assert sk.players["Rami"]["score"] == 2


def test_tool_persists_the_correction_audit_row(monkeypatch):
    import lily_agent as _la
    from lily_agent import LilyAgent

    sk = _rami_on_two_diamond_denied()
    agent, game = _agent_with(sk)
    game.supabase = object()
    written = []

    async def _fake_write(supabase, session_id, entry):
        written.append((session_id, dict(entry)))

    monkeypatch.setattr(_la.lily_persistence, "lily_write_score_event", _fake_write)
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(LilyAgent.lily_correct_verdict.__wrapped__(
            agent, None, "Rami", "wrong_rule", "diamond was yours",
        ))
        loop.run_until_complete(asyncio.sleep(0))
    finally:
        loop.close()
    assert written, "a correction must persist an audit row"
    row = written[0][1]
    assert row["cause"] == "verdict_correction"
    assert row["points"] == 1
    assert "grounds=wrong_rule" in row["transcript"]


def test_contest_note_points_at_the_correction_tool():
    # The wiring: a detected contest arms a directive that names the real
    # correction tool and its grounds (pre-W1 it pointed at a capability
    # that did not exist).
    import inspect
    from lily_agent import LilyGame
    src = inspect.getsource(LilyGame.on_transcript_event)
    assert "lily_correct_verdict" in src
    assert "answer_denied" in src and "wrong_rule" in src
