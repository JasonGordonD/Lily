"""WO-LILY-HOTFIX-006 N3 / N4 / N9 — the adjudication boundary.

One coherent area, three questions the old boundary could not answer:
WHAT may be adjudicated, against WHICH question, binding WHICH utterance.

All evidence below is verified against the production `lily_answers`
table, 2026-08-08.

N4 — complaints, questions and clarifications were entered as answers.
Seven confirmed rows across three sessions, every one meta-speech about
the game:

    "Sorry. We're talking about Cape Cod, Massachusetts. The peninsula."
        -> kb_14, incorrect
    "But what does that mean to do with Cape Cod?"
        -> kb_176, incorrect
    "Um. Why are we. Why are we in Mumbai or Delhi? Uh. And why are we
     talking..."                     -> kb_128, CORRECT, 1 POINT AWARDED
    "Oh. Why did you point at me? I wasn't even listening..."  -> q_1052
    "Like, can we just put it here? Yeah. I want to see if it's gonna
     work or..."                                              -> q_4819
    "Use me. The person. And now you telling her my answer."   -> q_8294
    "She's getting confused. Like she jumped from a question to question
     and..."                                                  -> q_8291

A player scored a point for complaining about the topic. Another said
aloud "Sorry, Lily. We're not talking to you. That was side banter" and
was scored anyway.

N3 — only registered questions are adjudicable, and answers bind to
their own window.

    lily-4FB3B2  two questions asked, BOTH answer rows filed against
                 q_4821. Rhonda's "We don't know." was spoken to the
                 Frankenstein question and adjudicated against question
                 one.
    lily-16A9AE  Chris's "Cape Cod Canal." — an answer to an
                 unregistered, improvised question — was adjudicated
                 against kb_180 (Psycho) and marked incorrect, while
                 Lily told him "Chris, you got it!"

N9 — the Jupiter case. Rami said "Okay. It's Jupiter." at 21:10:13.
Lily said "Jupiter was spot on, Rami, but just a split second late!" —
the conversational lane knew the answer was correct AND who said it. The
ledger for q_1052 records Rami's answer as "Go." (his earlier start
command), incorrect, zero points. His actual answer never entered the
ledger; a DIFFERENT utterance was captured in its place.
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_audeering_consumers
import lily_config
import lily_evaluation
import lily_persistence
import lily_say_gate
import lily_scorekeeper
from lily_agent import LilyGame
from lily_scorekeeper import LilyScorekeeper


# ---------------------------------------------------------------------------
# Harness — the same __new__-built LilyGame the stall-recovery fixtures
# drive, plus a capture seam on the lily_answers write so the LEDGER ROW is
# the assertion surface (these defects are all ledger defects).
# ---------------------------------------------------------------------------


class _FakeSession:
    def __init__(self) -> None:
        self.instructions: list[str] = []
        self.said: list[str] = []

    def generate_reply(self, instructions: str) -> None:
        self.instructions.append(instructions)

    def say(self, text, *a, **k):
        # REFACTOR W2a: the deterministic direct_say lane. The verdict beat is
        # now a fixed sheet, not an LLM instruction.
        self.said.append(text)
        return None


class _FakeAgentHandle:
    def set_preemptive_generation(self, enabled: bool) -> None:
        pass


class _FakeRoomAPI:
    def __init__(self) -> None:
        self.requests: list = []

    async def update_room_metadata(self, req) -> None:
        self.requests.append(req)


class _FakeLocalParticipant:
    def __init__(self) -> None:
        self.attributes: dict = {}

    async def set_attributes(self, attrs) -> None:
        self.attributes.update(attrs)


class _FakeCtx:
    def __init__(self) -> None:
        self.api = type("API", (), {"room": _FakeRoomAPI()})()
        self.room = type(
            "Room", (),
            {"name": "test-room", "local_participant": _FakeLocalParticipant()},
        )()


class _FakeReasoning:
    async def prefetch_question(self, sk, **kw):
        return None

    async def prefetch_picture_question(self, supabase, **kw):
        return None

    async def judge(self, *a, **kw):
        raise AssertionError(
            "Tier-2 must not be reached by these fixtures — every attempt "
            "here is either a clean answer or clean meta-speech."
        )


# The live 2026-08-08 questions, by their real ids.
Q_JUPITER = {
    "id": "q_1052",
    "prompt": "Which planet is the largest in our solar system?",
    "canonical_answer": "Jupiter",
    "acceptable_answers": ["jupiter"],
    "category": "science",
}
Q_CAPE_COD = {
    "id": "kb_14",
    "prompt": "Which Cape Cod town sits at the elbow of the peninsula?",
    "canonical_answer": "Chatham",
    "acceptable_answers": ["chatham"],
    "category": "geography",
}
Q_PSYCHO = {
    "id": "kb_180",
    "prompt": "Which 1960 Hitchcock film features the Bates Motel?",
    "canonical_answer": "Psycho",
    "acceptable_answers": ["psycho"],
    "category": "film",
}
Q_FRANKENSTEIN = {
    "id": "q_4822",
    "prompt": "Who wrote Frankenstein?",
    "canonical_answer": "Mary Shelley",
    "acceptable_answers": ["mary shelley", "shelley"],
    "category": "literature",
}
Q_4821 = {
    "id": "q_4821",
    "prompt": "Which country is the largest by land area?",
    "canonical_answer": "Russia",
    "acceptable_answers": ["russia"],
    "category": "geography",
}


def _make_game(session_id: str = "lily-fixture") -> LilyGame:
    game = LilyGame.bare()
    game.ctx = _FakeCtx()
    game.session = _FakeSession()
    game.agent = _FakeAgentHandle()
    game._preemptive_paused = False
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game.sk = LilyScorekeeper(session_id)
    game.rounds_total = 3
    game.ui_phase = "answering"
    game.memory_block = ""
    game.reconnected = False
    game.game_started = True
    game.game_over = False
    game.armed_question = None
    game.next_question = None
    game.eliminated = []
    game.used_prompts = []
    game.asked_history = []
    game.group_id = "grp_test"
    game.promoted_categories = set()
    game.prewager_standings = None
    game.highlights = []
    game.supabase = None
    game.reasoning = _FakeReasoning()
    game.background_audio = None
    game._bed_handle = None
    game._prefetch_task = None
    game._window_timer = None
    game._watchdog_task = None
    game._prefetch_stall_ticks = 0
    game._armed_limbo_ticks = 0
    game._steal_window = False
    game._adjudicating = False
    game._judged_keys = set()
    game._spec_judge = {}
    game._addressee_rows = {}
    game._pending_reveal_event = None
    game._pending_unbound_award = None
    game._user_turn_index = 0
    game._armed_speech_misses = 0
    game.pending_clarify = {}
    game.forget_state = "idle"
    game.forget_requester = None
    game._forget_target_group = None
    game.acoustic = lily_audeering_consumers.LilyAcousticState()
    return game


class _AnswerRows:
    """Capture seam on lily_persistence.lily_write_answer — the actual
    lily_answers insert, which is where every one of these defects was
    observed in production."""

    def __init__(self, monkeypatch) -> None:
        self.rows: list[dict] = []

        async def _capture(
            supabase, session_id, player_name, question_id, question_index,
            transcript, verdict, eval_tier, awarded_points, cause=None,
            utterance_id=None,
        ) -> None:
            self.rows.append({
                "session_id": session_id,
                "player_name": player_name,
                "question_id": question_id,
                "question_index": question_index,
                "transcript": transcript,
                "verdict": verdict,
                "eval_tier": eval_tier,
                "awarded_points": awarded_points,
                "cause": cause,
                "utterance_id": utterance_id,
            })

        async def _noop(*a, **kw):
            return None

        monkeypatch.setattr(lily_persistence, "lily_write_answer", _capture)
        monkeypatch.setattr(lily_persistence, "lily_checkpoint", _noop)
        monkeypatch.setattr(lily_persistence, "lily_log_addressee", _noop)

    def for_player(self, name: str) -> list[dict]:
        return [r for r in self.rows if r["player_name"] == name]


def _arm(game: LilyGame, question: dict) -> None:
    game.armed_question = dict(question)
    game.sk.start_question(game.armed_question)
    game.sk.round = 1
    game.sk.set_phase("round")


def _final(game: LilyGame, text: str, label: str, at: float, **kw) -> dict:
    return game.sk.on_transcript_segment(
        text=text, speaker_label=label, is_final=True,
        now=at, segment_start_time=at, segment_end_time=at + 0.5, **kw
    )


def _run(step):
    """Drive one scenario inside a live event loop, then cancel the window
    timers and metadata publishes it scheduled. `step` is a coroutine or a
    zero-arg callable — LilyGame.open_window schedules its expiry task with
    ensure_future, so it has to be called from inside the loop."""
    async def _scenario():
        out = step
        if callable(out):
            out = out()
        if asyncio.iscoroutine(out):
            out = await out
        # Let the fire-and-forget persistence writes actually start — the
        # lily_answers insert is scheduled with ensure_future, and these
        # fixtures assert on the row it writes.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        pending = [
            t for t in asyncio.all_tasks() if t is not asyncio.current_task()
        ]
        for task in pending:
            task.cancel()
        # Let the cancellations land before the loop closes — an
        # un-processed cancel prints "Task was destroyed but it is
        # pending!" and pollutes every later run's output.
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        return out

    return asyncio.run(_scenario())


# ===========================================================================
# N4 — the open window admits ANSWER-SHAPED utterances only
# ===========================================================================

# The seven production rows, verbatim. Every one entered lily_answers as an
# answer attempt; one of them scored a point.
LIVE_META_SPEECH = [
    "Sorry. We're talking about Cape Cod, Massachusetts. The peninsula.",
    "But what does that mean to do with Cape Cod?",
    "Um. Why are we. Why are we in Mumbai or Delhi? Uh. And why are we "
    "talking about that?",
    "Oh. Why did you point at me? I wasn't even listening. You point at me.",
    "Like, can we just put it here? Yeah. I want to see if it's gonna work.",
    "Use me. The person. And now you telling her my answer.",
    "She's getting confused. Like she jumped from a question to question.",
]


def test_live_meta_speech_rows_are_not_answer_shaped():
    """THE N4 fixture, at the filter. Each of these was scored against a
    real question id. None of them is an answer to anything."""
    for text in LIVE_META_SPEECH:
        reason = lily_evaluation.lily_non_answer_utterance(
            text, Q_CAPE_COD, ["Rami", "Rhonda", "Chris"]
        )
        assert reason is not None, f"still answer-shaped: {text!r}"


def test_the_complaint_that_scored_a_point_never_becomes_a_candidate():
    """kb_128: "Um. Why are we. Why are we in Mumbai or Delhi?" was marked
    CORRECT and awarded 1 point. A player scored for complaining about the
    topic. It must not even reach the candidate set."""
    game = _make_game("lily-kb128")
    game.sk.bind_speaker("S1", "Rami")
    _arm(game, Q_CAPE_COD)
    now = time.time()
    game.sk.open_answer_window(duration=30.0, now=now)

    result = _final(
        game,
        "Um. Why are we. Why are we in Mumbai or Delhi? Uh. And why are we "
        "talking about that?",
        "S1", now + 3,
    )

    assert result.get("candidate_recorded") is not True
    assert result.get("non_answer")
    assert not game.sk.answer_candidates
    assert game.sk.players["Rami"]["answers_attempted"] == 0


def test_spoken_floor_hold_is_never_adjudicated():
    """"Sorry, Lily. We're not talking to you. That was side banter" —
    said ALOUD, and scored anyway. FL-1 already has the floor-hold
    detector; the adjudication boundary must consult it."""
    game = _make_game("lily-floorhold")
    game.sk.bind_speaker("S2", "Rhonda")
    _arm(game, Q_CAPE_COD)
    now = time.time()
    game.sk.open_answer_window(duration=30.0, now=now)

    result = _final(
        game, "Sorry, Lily. We're not talking to you. That was side banter.",
        "S2", now + 2,
    )

    assert result.get("candidate_recorded") is not True
    assert not game.sk.answer_candidates


def test_meta_speech_is_dropped_at_the_adjudication_boundary(monkeypatch):
    """Defence in depth: even if meta-speech reaches the candidate set by
    some other route (replay, steal-window carryover), adjudication must
    refuse to enter it in the ledger."""
    rows = _AnswerRows(monkeypatch)
    game = _make_game("lily-boundary")
    game.supabase = object()
    game.sk.bind_speaker("S1", "Rami")
    _arm(game, Q_CAPE_COD)
    now = time.time()
    game.sk.open_answer_window(duration=30.0, now=now)
    # Force it in past the intake filter, exactly as the replay path could.
    game.sk.answer_candidates["Rami"] = {
        "player": "Rami",
        "speaker_label": "S1",
        "text": "But what does that mean to do with Cape Cod?",
        "segment_start_time": now + 2,
        "segment_end_time": now + 4,
        "timestamp": "2026-08-08T00:00:00+00:00",
        "unrostered": False,
        "attempts": [{
            "text": "But what does that mean to do with Cape Cod?",
            "segment_start_time": now + 2,
        }],
        "window_question_id": Q_CAPE_COD["id"],
        "window_question_index": game.sk.question_number,
    }

    _run(game.adjudicate(steal_allowed=False))

    assert rows.for_player("Rami") == []
    assert game.sk.players["Rami"]["score"] == 0


# -- MUST NOT REGRESS ---------------------------------------------------------


def test_genuine_short_answers_still_score():
    """The four live short answers that MUST keep working."""
    for text, question in (
        ("Chatham", Q_CAPE_COD),
        ("Psycho", Q_PSYCHO),
        ("Russia", Q_4821),
        ("Skin", {"id": "kb_9", "prompt": "Largest organ?",
                  "canonical_answer": "Skin", "acceptable_answers": ["skin"]}),
    ):
        assert lily_evaluation.lily_non_answer_utterance(
            text, question, ["Rami", "Rhonda", "Chris"]
        ) is None, f"{text!r} stopped being answer-shaped"


def test_procedural_go_is_never_an_answer_candidate():
    """RM_qs6YeUdkV7or: Rami's "Go." claimed the q_1 candidate slot while
    his real "Okay. It's Jupiter." arrived later. Procedural imperatives
    are not answer-shaped."""
    q = {
        "id": "q_1052",
        "prompt": "Recognizable by a raging storm known as the Great Red "
                  "Spot, name our solar system's largest planet.",
        "canonical_answer": "Jupiter",
        "acceptable_answers": ["jupiter"],
    }
    for text in ("Go.", "Continue.", "Next question.", "Go ahead."):
        assert lily_evaluation.lily_non_answer_utterance(
            text, q, ["Rami", "Rhonda", "Chris"]
        ) == "procedural", text

    game = _make_game("lily-go")
    game.sk.bind_speaker("S1", "Rami")
    _arm(game, q)
    now = time.time()
    game.sk.open_answer_window(duration=30.0, now=now)
    result = _final(game, "Go.", "S1", now + 1)
    assert result.get("candidate_recorded") is not True
    assert result.get("non_answer") == "procedural"
    assert not game.sk.answer_candidates


def test_interrogative_without_question_mark_is_meta_speech():
    """STT often drops the terminal '?'. Host-addressed wh-clauses must
    still be meta-speech."""
    text = "Why did you point at me I wasn't even listening"
    assert lily_evaluation.lily_non_answer_utterance(
        text, Q_CAPE_COD, ["Rami", "Rhonda"]
    ) is not None


def test_murmured_answer_inside_a_complaint_still_scores():
    """The answer-surface override is what keeps N4 conservative: a real
    answer buried in a complaint-heavy turn is still an answer."""
    text = "Why are we even talking about this? Chatham, I guess. Ridiculous."
    assert lily_evaluation.lily_non_answer_utterance(
        text, Q_CAPE_COD, ["Rami"]
    ) is None

    game = _make_game("lily-murmur")
    game.sk.bind_speaker("S1", "Rami")
    _arm(game, Q_CAPE_COD)
    now = time.time()
    game.sk.open_answer_window(duration=30.0, now=now)
    result = _final(game, text, "S1", now + 3)
    assert result.get("candidate_recorded") is True


def test_answer_shaped_guess_still_scores_end_to_end(monkeypatch):
    """A plain wrong guess is still an attempt and still audits — N4 must
    not silence ordinary play."""
    rows = _AnswerRows(monkeypatch)
    game = _make_game("lily-plain")
    game.supabase = object()
    game.sk.bind_speaker("S1", "Rami")
    _arm(game, Q_CAPE_COD)
    now = time.time()
    game.sk.open_answer_window(duration=30.0, now=now)
    _final(game, "Provincetown", "S1", now + 2)

    _run(game.adjudicate(steal_allowed=False))

    assert [r["verdict"] for r in rows.for_player("Rami")] == ["incorrect"]


# ===========================================================================
# N3 — registered question, own window
# ===========================================================================


def test_speech_with_no_open_registered_window_is_never_scored(monkeypatch):
    """lily-16A9AE: Chris answered an improvised, UNREGISTERED question.
    His "Cape Cod Canal." was filed against kb_180 (Psycho) and marked
    incorrect while Lily told him he'd got it. With no open registered
    window his speech is logged and nothing else."""
    rows = _AnswerRows(monkeypatch)
    game = _make_game("lily-16A9AE")
    game.supabase = object()
    game.sk.bind_speaker("S3", "Chris")
    _arm(game, Q_PSYCHO)
    now = time.time()
    game.sk.open_answer_window(duration=20.0, now=now)
    game.sk.close_answer_window()          # Psycho's window is over

    result = _final(game, "Cape Cod Canal.", "S3", now + 40)

    assert result.get("candidate_recorded") is not True
    assert not game.sk.answer_candidates
    _run(game.adjudicate(steal_allowed=False))
    assert rows.for_player("Chris") == []


def test_open_window_refuses_without_a_registered_question(caplog):
    """A window may only ever exist for a REGISTERED question. Nothing
    improvised gets an adjudicable window."""
    import logging

    game = _make_game("lily-unregistered")
    game.armed_question = None
    with caplog.at_level(logging.ERROR):
        game.open_window()
    assert game.sk.answer_window_open is False
    assert "UNREGISTERED" in caplog.text


def test_the_window_captures_its_question_at_open():
    """Invariant 2, at the capture point: the question id is taken AT
    WINDOW-OPEN, not inferred later."""
    game = _make_game("lily-capture")
    _arm(game, Q_4821)
    _run(lambda: game.open_window(duration=30.0))
    assert game.sk.answer_window_question_id == "q_4821"
    assert game.sk.answer_window_question_index == game.sk.question_number
    # And it survives the close — adjudication reads it after the window
    # is gone.
    game.sk.close_answer_window()
    assert game.sk.answer_window_question_id == "q_4821"


def test_a_candidate_carries_the_window_it_arrived_in():
    game = _make_game("lily-bind")
    game.sk.bind_speaker("S2", "Rhonda")
    _arm(game, Q_4821)
    now = time.time()
    game.sk.open_answer_window(duration=30.0, now=now)
    _final(game, "Russia", "S2", now + 2)
    cand = game.sk.answer_candidates["Rhonda"]
    assert cand["window_question_id"] == "q_4821"
    assert cand["window_question_index"] == game.sk.question_number


def test_answers_bind_to_their_own_question_not_the_previous_one(monkeypatch):
    """THE lily-4FB3B2 fixture. Two questions asked; BOTH answer rows were
    filed against q_4821. Rhonda's "We don't know." was spoken to the
    Frankenstein question and adjudicated against question one.

    Here the q_4821 candidate survives into the Frankenstein window (the
    live carryover: close_answer_window never cleared candidates). Each
    row must carry the question whose window it arrived in."""
    rows = _AnswerRows(monkeypatch)
    game = _make_game("lily-4FB3B2")
    game.supabase = object()
    game.sk.bind_speaker("S2", "Rhonda")
    now = time.time()

    def _scenario():
        # Question one — answered, window closes without adjudicating (the
        # live shape: the reveal chain died and the next question armed).
        _arm(game, Q_4821)
        game.open_window(duration=20.0)
        _final(game, "Russia", "S2", time.time() + 1)
        game.sk.close_answer_window()
        stale = dict(game.sk.answer_candidates)

        # Question two — Frankenstein. Rhonda gives up.
        _arm(game, Q_FRANKENSTEIN)
        game.open_window(duration=20.0)
        game.sk.answer_candidates.update(stale)   # the live carryover
        _final(game, "We don't know.", "S2", time.time() + 1)
        return game.adjudicate(steal_allowed=False)

    _run(_scenario)

    frank_rows = [
        r for r in rows.for_player("Rhonda")
        if r["question_id"] == "q_4822"
    ]
    assert frank_rows, "the Frankenstein answer was filed elsewhere"
    # And nothing spoken into the Frankenstein window may be filed under
    # q_4821 — that is exactly the production defect.
    misfiled = [
        r for r in rows.for_player("Rhonda")
        if r["question_id"] == "q_4821" and r["transcript"] == "We don't know."
    ]
    assert misfiled == []
    # The stale q_4821 candidate is not adjudicable here either: its
    # window is gone.
    assert all(
        r["question_id"] == "q_4822" for r in rows.for_player("Rhonda")
    )


def test_the_ledger_row_uses_the_captured_question_not_the_armed_one(
    monkeypatch,
):
    """Invariant 2 end to end: if the armed question moves under
    adjudication's feet, the row still names the window's question."""
    rows = _AnswerRows(monkeypatch)
    game = _make_game("lily-captured")
    game.supabase = object()
    game.sk.bind_speaker("S1", "Rami")
    _arm(game, Q_4821)
    captured_index = game.sk.question_number

    def _scenario():
        game.open_window(duration=20.0)
        _final(game, "Russia", "S1", time.time() + 1)
        # The live race: something advanced the question number before the
        # reveal chain ran.
        game.sk.question_number += 1
        return game.adjudicate(steal_allowed=False)

    _run(_scenario)

    row = rows.for_player("Rami")[0]
    assert row["question_id"] == "q_4821"
    assert row["question_index"] == captured_index
    entry = [e for e in game.sk.score_ledger if e["cause"] == "answer"][0]
    assert entry["question_id"] == "q_4821"
    assert entry["question_index"] == captured_index


# ===========================================================================
# N9 — the Jupiter case
# ===========================================================================


def test_answer_capture_binds_the_utterance_not_the_slot(monkeypatch):
    """THE N9 fixture. Rami's ledger row for q_1052 recorded "Go." — his
    earlier start command — and marked it incorrect. His real answer,
    "Okay. It's Jupiter.", never entered the ledger.

    Capture binds an UTTERANCE, identified by its own transcript id."""
    rows = _AnswerRows(monkeypatch)
    game = _make_game("lily-jupiter")
    game.supabase = object()
    game.sk.bind_speaker("S1", "Rami")
    _arm(game, Q_JUPITER)

    def _scenario():
        now = time.time()
        game.open_window(duration=30.0)
        # The stray earlier utterance that filled his slot in production.
        _final(game, "Go.", "S1", now + 0.5, utterance_id="u-go")
        # The answer he actually gave, 21:10:13.
        _final(game, "Okay. It's Jupiter.", "S1", now + 4.0,
               utterance_id="u-jupiter")
        return game.adjudicate(steal_allowed=False)

    _run(_scenario)

    row = rows.for_player("Rami")[0]
    assert "Jupiter" in row["transcript"], (
        f"the ledger captured a different utterance: {row['transcript']!r}"
    )
    assert row["utterance_id"] == "u-jupiter"
    assert row["verdict"] == "correct"
    assert game.sk.players["Rami"]["score"] > 0


def test_every_attempt_carries_its_own_transcript_id():
    game = _make_game("lily-utterance-ids")
    game.sk.bind_speaker("S1", "Rami")
    _arm(game, Q_JUPITER)
    now = time.time()
    game.sk.open_answer_window(duration=30.0, now=now)
    _final(game, "Saturn", "S1", now + 1, utterance_id="u-1")
    _final(game, "No, Jupiter.", "S1", now + 3, utterance_id="u-2")
    attempts = game.sk.answer_candidates["Rami"]["attempts"]
    assert [a["utterance_id"] for a in attempts] == ["u-1", "u-2"]


def test_an_utterance_id_is_minted_when_stt_supplies_none():
    """The binding may never fall back to "most recent" — an id is always
    present, even when the transcript event carries none."""
    game = _make_game("lily-minted")
    game.sk.bind_speaker("S1", "Rami")
    _arm(game, Q_JUPITER)
    now = time.time()
    game.sk.open_answer_window(duration=30.0, now=now)
    _final(game, "Jupiter", "S1", now + 1)
    attempt = game.sk.answer_candidates["Rami"]["attempts"][0]
    assert attempt.get("utterance_id")


def test_a_late_but_correct_answer_inside_the_grace_margin_scores(monkeypatch):
    """N9 part 2, first branch: a STATED, configurable grace margin. "Just
    a split second late" is a defined outcome, not a silent loss."""
    monkeypatch.setattr(lily_config, "late_answer_grace_seconds", lambda: 2.0)
    game = _make_game("lily-grace")
    game.sk.bind_speaker("S1", "Rami")
    _arm(game, Q_JUPITER)
    now = 1000.0
    game.sk.open_answer_window(duration=10.0, now=now)
    # Spoken 0.6s past the deadline — inside the margin.
    result = _final(game, "Okay. It's Jupiter.", "S1", now + 10.6)
    assert result.get("candidate_recorded") is True
    assert result.get("late_within_grace") is True


def test_a_late_correct_answer_past_the_margin_is_announced_not_lost(
    monkeypatch,
):
    """N9 part 2, second branch: past the margin it is an EXPLICIT
    announced miss with a reason — never silence, and never a different
    utterance recorded as theirs."""
    monkeypatch.setattr(lily_config, "late_answer_grace_seconds", lambda: 1.0)
    rows = _AnswerRows(monkeypatch)
    game = _make_game("lily-late-miss")
    game.supabase = object()
    game.sk.bind_speaker("S1", "Rami")
    _arm(game, Q_JUPITER)
    now = 1000.0

    def _scenario():
        game.open_window(duration=10.0)
        game.sk.answer_window_opened_at = now
        game.sk.answer_window_deadline = now + 10.0
        game.sk.close_answer_window()
        return game.note_late_answer(
            "Okay. It's Jupiter.", player="Rami", speaker_label="S1",
            segment_ts=now + 14.0, utterance_id="u-jupiter",
        )

    late = _run(_scenario)

    assert late is not None
    assert late["verdict"] == "correct"
    assert late["within_grace"] is False
    assert round(late["seconds_late"], 1) == 4.0
    # It is ON THE LEDGER as a late answer — the real utterance, zero
    # points, its own cause. Silence is what produced the Jupiter row.
    row = rows.for_player("Rami")[0]
    assert row["cause"] == "late_answer"
    assert row["transcript"] == "Okay. It's Jupiter."
    assert row["utterance_id"] == "u-jupiter"
    assert row["awarded_points"] == 0
    assert row["question_id"] == "q_1052"
    # REFACTOR W2a: the miss is a DETERMINISTIC direct_say beat (not an
    # organic-lane note) — it names the player, confirms the answer, and awards
    # no point.
    late = " ".join(game.session.said)
    assert "Jupiter" in late
    assert "Rami" in late
    assert "point" in late.lower() and "no point" in late.lower()


def test_an_echo_of_the_revealed_answer_is_not_a_late_answer(monkeypatch):
    """The late path must not turn every reveal into a "you were right"
    announcement: once the answer has gone to air (WS-4 burn), a player
    repeating it is an echo, not an attempt."""
    rows = _AnswerRows(monkeypatch)
    game = _make_game("lily-echo")
    game.supabase = object()
    game.sk.bind_speaker("S1", "Rami")
    _arm(game, Q_JUPITER)
    now = 1000.0

    def _scenario():
        game.open_window(duration=10.0)
        game.sk.answer_window_opened_at = now
        game.sk.answer_window_deadline = now + 10.0
        game.sk.close_answer_window()
        game._burn_question(game.armed_question, reason="revealed")
        return game.note_late_answer(
            "Jupiter!", player="Rami", speaker_label="S1",
            segment_ts=now + 14.0,
        )

    assert _run(_scenario) is None
    assert rows.rows == []
    assert getattr(game, "_late_answer_note", None) is None


def test_a_late_wrong_guess_is_just_conversation(monkeypatch):
    """Only a CORRECT late answer is a loss worth announcing."""
    rows = _AnswerRows(monkeypatch)
    game = _make_game("lily-late-wrong")
    game.supabase = object()
    game.sk.bind_speaker("S1", "Rami")
    _arm(game, Q_JUPITER)
    now = 1000.0

    def _scenario():
        game.open_window(duration=10.0)
        game.sk.answer_window_opened_at = now
        game.sk.answer_window_deadline = now + 10.0
        game.sk.close_answer_window()
        return game.note_late_answer(
            "Saturn.", player="Rami", speaker_label="S1",
            segment_ts=now + 14.0,
        )

    assert _run(_scenario) is None
    assert rows.rows == []


def test_narration_cannot_contradict_the_ledger():
    """N9 part 3. "Jupiter was spot on, Rami, but just a split second
    late!" was spoken while Rami's q_1052 row said incorrect. That is a
    SCORE_DIVERGENCE — the ledger wins."""
    sk = LilyScorekeeper("lily-divergence")
    sk.bind_speaker("S1", "Rami")
    sk.start_question(dict(Q_JUPITER))
    sk.record_result(
        "Rami", correct=False, points=0, question_id="q_1052",
        transcript="Go.",
    )

    div = lily_scorekeeper.lily_narrated_verdict_divergence(
        "Jupiter was spot on, Rami, but just a split second late!",
        sk.score_ledger,
    )
    assert div is not None
    assert div["player"] == "Rami"
    assert div["spoken"] == "correct"
    assert div["ledger"] == "incorrect"


def test_a_verdict_that_agrees_with_the_ledger_is_not_a_divergence():
    sk = LilyScorekeeper("lily-agrees")
    sk.bind_speaker("S1", "Rami")
    sk.start_question(dict(Q_JUPITER))
    sk.record_result(
        "Rami", correct=True, points=1, question_id="q_1052",
        transcript="Okay. It's Jupiter.",
    )
    assert lily_scorekeeper.lily_narrated_verdict_divergence(
        "Jupiter — spot on, Rami! Point is yours.", sk.score_ledger
    ) is None


def test_a_miss_narrated_as_a_miss_is_not_a_divergence():
    sk = LilyScorekeeper("lily-miss")
    sk.bind_speaker("S1", "Rami")
    sk.start_question(dict(Q_JUPITER))
    sk.record_result(
        "Rami", correct=False, points=0, question_id="q_1052",
        transcript="Saturn.",
    )
    assert lily_scorekeeper.lily_narrated_verdict_divergence(
        "Not quite, Rami — the answer was Jupiter.", sk.score_ledger
    ) is None


def test_the_ledger_entry_names_the_utterance_it_bound():
    """The ledger row itself carries the utterance id, so a q_1052-class
    row can be read after the fact: WHICH spoken thing is this about?"""
    sk = LilyScorekeeper("lily-ledger-id")
    sk.bind_speaker("S1", "Rami")
    sk.start_question(dict(Q_JUPITER))
    sk.record_result(
        "Rami", correct=True, points=1, question_id="q_1052",
        question_index=7, transcript="Okay. It's Jupiter.",
        utterance_id="u-jupiter",
    )
    entry = sk.score_ledger[-1]
    assert entry["utterance_id"] == "u-jupiter"
    assert entry["question_index"] == 7


# ===========================================================================
# Supporting invariants the three defects rest on
# ===========================================================================


def test_a_new_windows_answer_never_revises_the_previous_windows_candidate():
    """The mechanism behind lily-4FB3B2: close_answer_window never cleared
    candidates, so the next question's answer was absorbed as a REVISION
    of the previous question's candidate and inherited its question id."""
    sk = LilyScorekeeper("lily-carryover")
    sk.bind_speaker("S2", "Rhonda")
    sk.start_question(dict(Q_4821))
    sk.open_answer_window(duration=20.0, now=100.0)
    sk.on_transcript_segment(
        text="Russia", speaker_label="S2", is_final=True,
        now=102.0, segment_start_time=102.0,
    )
    stale = dict(sk.answer_candidates)
    sk.close_answer_window()

    sk.start_question(dict(Q_FRANKENSTEIN))
    sk.open_answer_window(duration=20.0, now=200.0)
    sk.answer_candidates.update(stale)          # the live carryover
    sk.on_transcript_segment(
        text="Mary Shelley", speaker_label="S2", is_final=True,
        now=202.0, segment_start_time=202.0,
    )

    cand = sk.answer_candidates["Rhonda"]
    assert cand["window_question_id"] == "q_4822"
    assert cand["text"] == "Mary Shelley"
    # …and it is NOT carrying the q_4821 answer as an earlier "attempt".
    assert [a["text"] for a in cand["attempts"]] == ["Mary Shelley"]


def test_the_boundary_refuses_an_unregistered_window(monkeypatch):
    """N3 invariant 1 at the boundary: candidates recorded in a window
    that was never opened over a registered question are logged and
    dropped, never entered in the ledger."""
    rows = _AnswerRows(monkeypatch)
    game = _make_game("lily-unregistered-boundary")
    game.supabase = object()
    game.sk.bind_speaker("S3", "Chris")
    _arm(game, Q_PSYCHO)
    now = time.time()
    # A window opened WITHOUT a registered question (the improvised-round
    # shape) — the scorekeeper marks it unregistered.
    game.sk.current_question = None
    game.sk.open_answer_window(duration=20.0, now=now)
    _final(game, "Cape Cod Canal.", "S3", now + 2)
    game.sk.current_question = dict(Q_PSYCHO)

    _run(game.adjudicate(steal_allowed=False))

    assert rows.for_player("Chris") == []
    assert game.sk.players["Chris"]["score"] == 0


def test_every_final_reports_its_utterance_id():
    """N9: the id is on the segment result, not only on the candidate —
    the late-answer path and the audit rows both read it from there."""
    sk = LilyScorekeeper("lily-result-id")
    sk.bind_speaker("S1", "Rami")
    sk.start_question(dict(Q_JUPITER))
    sk.open_answer_window(duration=20.0, now=100.0)
    supplied = sk.on_transcript_segment(
        text="Jupiter", speaker_label="S1", is_final=True,
        now=102.0, segment_start_time=102.0, utterance_id="u-stt-7",
    )
    assert supplied["utterance_id"] == "u-stt-7"


def test_the_verdict_beat_is_written_from_the_committed_row(monkeypatch):
    """N9 part 3, the generation side: the spoken verdict is produced FROM
    the ledger row — naming the player and the utterance that actually
    scored — so it cannot describe a different utterance than the ledger."""
    _AnswerRows(monkeypatch)
    game = _make_game("lily-verdict-from-ledger")
    game.supabase = object()
    game.sk.bind_speaker("S1", "Rami")
    _arm(game, Q_JUPITER)

    def _scenario():
        now = time.time()
        game.open_window(duration=30.0)
        _final(game, "Go.", "S1", now + 0.5, utterance_id="u-go")
        _final(game, "Okay. It's Jupiter.", "S1", now + 4.0,
               utterance_id="u-jupiter")
        return game.adjudicate(steal_allowed=False)

    _run(_scenario)

    # REFACTOR W2a: the verdict is a DETERMINISTIC sheet composed from the
    # committed ruling — it names the canonical answer and the winner, and
    # never quotes an utterance at all, so it cannot describe a different one
    # than the ledger (a strictly stronger guarantee than the old instruction
    # that had to be TOLD not to credit "Go.").
    verdict_says = [s for s in game.session.said if "Jupiter" in s]
    assert verdict_says, "no verdict beat naming the committed answer"
    assert "Point to Rami" in verdict_says[-1]
    assert not any("Go." in s for s in game.session.said)


def test_a_missed_question_forbids_narrating_anyone_correct(monkeypatch):
    """The other half: with no correct committed row, the verdict beat is
    told in so many words not to call anyone right. "Jupiter was spot on,
    Rami, but just a split second late!" is exactly that mistake."""
    _AnswerRows(monkeypatch)
    game = _make_game("lily-missed-verdict")
    game.supabase = object()
    game.sk.bind_speaker("S1", "Rami")
    _arm(game, Q_JUPITER)

    def _scenario():
        now = time.time()
        game.open_window(duration=30.0)
        _final(game, "Saturn.", "S1", now + 2.0)
        return game.adjudicate(steal_allowed=False)

    _run(_scenario)

    # REFACTOR W2a: with no correct committed row the deterministic sheet is
    # the nobody-landed-it beat — it credits no one and names no player.
    said = game.session.said
    assert any("Nobody landed it" in s for s in said), said
    assert not any("Rami" in s for s in said)
    assert not any("Correct" in s or "Point to" in s for s in said)


def test_the_ledger_row_lookup_is_per_player_and_per_question():
    sk = LilyScorekeeper("lily-rowlookup")
    sk.bind_speaker("S1", "Rami")
    sk.start_question(dict(Q_4821))
    sk.record_result("Rami", correct=False, points=0, question_id="q_4821",
                     transcript="Canada")
    sk.start_question(dict(Q_JUPITER))
    sk.record_result("Rami", correct=True, points=1, question_id="q_1052",
                     transcript="Okay. It's Jupiter.")

    assert sk.ledger_row_for("Rami", "q_4821")["transcript"] == "Canada"
    assert sk.ledger_row_for("Rami", "q_1052")["correct"] is True
    assert sk.ledger_row_for("Rhonda", "q_1052") is None


# -- the anti-regression sweep, made structural -------------------------------

# Ordinary play that MUST stay adjudicable. N4 filters meta-speech; it may
# not quietly start eating hedged guesses, wrong answers, or answers about
# third parties. Every entry here is a shape a real table produces.
ANSWER_SHAPED = [
    "Jupiter", "It's Jupiter", "Okay. It's Jupiter.", "Is it Jupiter?",
    "Saturn", "Is it Saturn?", "Saturn?", "Uh, Saturn I think",
    "The answer is Saturn", "I think it's Saturn", "Maybe Neptune?",
    "It's going to be Saturn", "Was it Shelley?", "He got the Nobel Prize",
    "She wrote it", "He read it aloud", "Mary Shelley", "Chatham",
    "Psycho", "Russia", "Skin", "the femur", "no wait, the femur",
    "B", "Cape Cod Canal.", "Provincetown", "We don't know.",
    "I'm going with Venus", "Venus, final answer",
]


def test_ordinary_play_is_never_filtered_as_meta_speech():
    for text in ANSWER_SHAPED:
        assert lily_evaluation.lily_non_answer_utterance(
            text, Q_JUPITER, ["Rami", "Rhonda", "Chris"]
        ) is None, f"N4 over-reached on ordinary play: {text!r}"


def test_the_audit_row_survives_a_pre_ddl_database(monkeypatch):
    """lily_answers.utterance_id / cause are new columns. Until the DDL
    lands the insert fails and is retried with optional keys stripped
    one at a time — the row is never lost, and a mixed-migration
    environment (one column present, the other not) keeps whichever
    column the database already accepts."""
    attempts: list[dict] = []

    class _Table:
        def insert(self, row):
            attempts.append(dict(row))
            self._row = row
            return self

        def execute(self):
            if "utterance_id" in self._row or "cause" in self._row:
                raise RuntimeError(
                    "PGRST204: column lily_answers.utterance_id does not exist"
                )
            return None

    class _DB:
        def table(self, name):
            assert name == "lily_answers"
            return _Table()

    asyncio.run(lily_persistence.lily_write_answer(
        _DB(), "lily-preddl", "Rami", "q_1052", 3, "Okay. It's Jupiter.",
        "correct", 1, 1, cause="answer", utterance_id="u-jupiter",
    ))

    assert len(attempts) == 3
    assert attempts[0]["utterance_id"] == "u-jupiter"
    assert "cause" in attempts[0]
    # First retry drops cause; utterance_id still rejected on a pre-024 DB.
    assert "cause" not in attempts[1]
    assert attempts[1]["utterance_id"] == "u-jupiter"
    # Second retry drops utterance_id; the base row lands.
    assert "utterance_id" not in attempts[2]
    assert attempts[2]["transcript"] == "Okay. It's Jupiter."
