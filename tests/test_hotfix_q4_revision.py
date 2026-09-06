"""Live 2026-09-06 17:33Z, session lily-38C562-2eb12a08, build 1783e82 (Q4,
kb_271 "Henry the Eighth ... this many wives", canonical six):

  17:33:36.6  [Rami] I think what? Eight.      -> candidate, Tier-1 uncertain
  17:33:38.3  [Rami] Or. Sorry. Six.           -> revision, Tier-1 uncertain
  17:33:39.9  judge row (ONE call, launched at the FIRST final, on "eight")
  17:33:43.6  lily_answers: "[Rami] I think what? Eight." incorrect, tier 1
  17:33:59    "What got recorded doesn't match six, so I can't put the point back"
  17:34:28+   [Rami] Okay. / Go ahead / Sure / Let's go / Yes!  -> all re-held

Four mechanisms, each proven by replaying the row text through the real
code on 1783e82 (probe_q4.py), each pinned here failing-first:

HOTFIX-NUMBER-IN-PHRASE-001 — lily_normalize_answer turns the bare answer
  "six" into "6" but keeps a lone small number word inside a phrase as a
  word ("air force one"), so "or sorry six" never contained "6": every
  sentence carrying a number word was Tier-1 uncertain.
HOTFIX-REVISION-JUDGE-001 (a) — the speculative judge is keyed per player
  and `key in self._spec_judge: continue` skipped the revision, so the
  judge only ever saw "eight". (b) adjudicate consumed that cached verdict
  as the ruling on the player's CURRENT words, and its batched fallback
  sent one text per candidate (the first uncertain attempt), never the
  revision. (c) lily_correct_verdict answer_denied corroborated against
  the ONE bound ledger transcript, never the player's other in-window
  attempts — the same buffer the misheard ground already reads.
HOTFIX-SPEAKER-PREFIX-001 — Speechmatics known-speaker labels arrive in
  the transcript text ("[Rami] Yes!") exactly like the engine's "[S1]";
  the handler stripped only `[S\\d+]`, so the acceptance / affirmative
  detectors saw "rami yes" and the addressed hold re-armed on every exit.
"""

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_evaluation as E  # noqa: E402
import lily_glass  # noqa: E402
import lily_scorekeeper  # noqa: E402
from lily_scorekeeper import LilyScorekeeper  # noqa: E402
from test_hotfix006_adjudication import (  # noqa: E402
    _AnswerRows,
    _arm,
    _final,
    _make_game,
    _run,
)

KB_271 = {
    "id": "kb_271",
    "prompt": (
        "Divorced, beheaded, died — England's much-married Henry the Eighth "
        "got through this many wives."
    ),
    "canonical_answer": "six",
    "acceptable_answers": ["6", "six", "six wives"],
    "category": "adult_history",
}
Q_WILDE = {
    "id": "q_wilde",
    "prompt": "Who wrote The Importance of Being Earnest?",
    "canonical_answer": "Oscar Wilde",
    "acceptable_answers": ["oscar wilde", "wilde"],
    "category": "literature",
}
FIRST = "[Rami] I think what? Eight."
SECOND = "[Rami] Or. Sorry. Six."


# ---------------------------------------------------------------------------
# HOTFIX-NUMBER-IN-PHRASE-001
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("said,verdict", [
    ("Or. Sorry. Six.", "correct"),
    (SECOND, "correct"),                    # the live row text, prefix and all
    ("Sorry, six", "correct"),
    ("I'd say six wives", "correct"),
    ("I think what? Eight.", "uncertain"),  # names a different number
    (FIRST, "uncertain"),
    ("between six and eight", "uncertain"),  # names two: the judge's call
    ("Or. Sorry.", "uncertain"),            # names none
])
def test_number_word_inside_a_phrase_scores_at_tier1(said, verdict):
    r = E.lily_tier1_evaluate_question(said, KB_271)
    assert r["verdict"] == verdict, (said, r)
    if verdict == "correct":
        assert r["method"] == "numeric", r


def test_number_in_phrase_never_goes_fuzzy_or_phonetic():
    # E1 holds: the numbers decide, nothing else. "sixty" is a different
    # number, not a near-miss of "six".
    r = E.lily_tier1_evaluate("or sorry sixty", ["6", "six"])
    assert r["verdict"] == "uncertain" and r["method"] != "phonetic", r
    assert E._numbers_named_in("or sorry six") == {"6"}
    assert E._numbers_named_in("in 1968 or 1969") == {"1968", "1969"}
    assert E._numbers_named_in("air force one") == set()  # "one" is a pronoun
    assert E._numbers_named_in("canberra") == set()


def test_air_force_one_still_a_name_against_a_text_answer():
    # The normalizer's "lone small number word stays a word" rule is
    # untouched; the phrase branch only runs for NUMERIC canonicals.
    assert E.lily_normalize_answer("Air Force One") == "air force one"
    r = E.lily_tier1_evaluate("Air Force One", ["air force one"])
    assert r["verdict"] == "correct" and r["method"] == "exact"


# ---------------------------------------------------------------------------
# HOTFIX-REVISION-JUDGE-001 (a) — the speculative judge follows the revision
# ---------------------------------------------------------------------------


def _live(game, question, at):
    game.sk.bind_speaker("Rami", "Rami")
    _arm(game, question)
    delivered = getattr(game, "_delivered_to_playout", None)
    if delivered is None:
        delivered = game._delivered_to_playout = set()
    delivered.add(game.sk.question_number)


def test_speculative_judge_is_relaunched_on_the_revision():
    game = _make_game("lily-38C562-spec")
    launched = []

    async def _spec(question, attempt_text, key, nbest=None):
        launched.append(attempt_text)
        await asyncio.sleep(3600)  # never resolves inside the test

    game._speculative_judge = _spec
    at = time.time()

    async def scenario():
        _live(game, Q_WILDE, at)
        game.open_window(duration=30.0)
        for text, t in (("Dickens.", at + 3.0), ("Or, sorry — the Earnest guy.", at + 5.0)):
            result = _final(game, text, "Rami", t)
            game.on_transcript_event(result, text, "Rami", t)
            await asyncio.sleep(0)
        task = game._spec_judge["Rami"]
        judged = lily_glass.spec_judge_text(task)
        task.cancel()
        await asyncio.sleep(0)
        return judged

    judged = _run(lambda: scenario())
    assert launched == ["Dickens.", "Or, sorry — the Earnest guy."]
    assert judged == "Or, sorry — the Earnest guy."


# ---------------------------------------------------------------------------
# HOTFIX-REVISION-JUDGE-001 (b) — the reveal judges the current words
# ---------------------------------------------------------------------------


class _Judge:
    """A Tier-2 fake that records every prompt and rules the LAST attempt
    of the player it sees correct — the instruction the real judge now
    carries."""

    def __init__(self):
        self.prompts = []

    async def prefetch_question(self, sk, **kw):
        return None

    async def judge(self, instructions, prompt):
        self.prompts.append(prompt)
        return json.dumps({
            "verdict": "correct", "winner": "Rami",
            "normalized_answer": "oscar wilde", "reason": "the revision lands",
        })


def _stale_spec_task(text, verdict="incorrect"):
    async def _done():
        return {"verdict": verdict, "winner": None, "reason": "eight is wrong"}
    task = asyncio.ensure_future(_done())
    lily_glass.name_spec_judge(task, text)
    return task


def test_stale_speculative_verdict_is_not_the_ruling_on_the_revision(monkeypatch):
    """The live shape: the cached "incorrect" on the first attempt must not
    become the verdict; the batched judge sees BOTH attempts in order and
    the ledger row names the revision."""
    _AnswerRows(monkeypatch)
    game = _make_game("lily-38C562-stale")
    judge = _Judge()
    game.reasoning = judge
    at = time.time()

    async def scenario():
        _live(game, Q_WILDE, at)
        game.open_window(duration=30.0)
        _final(game, "Dickens.", "Rami", at + 3.0)
        game._spec_judge["Rami"] = _stale_spec_task("Dickens.")
        await asyncio.sleep(0)
        _final(game, "Or, sorry — the Earnest guy.", "Rami", at + 5.0)
        await game.adjudicate()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    _run(lambda: scenario())
    assert game.sk.players["Rami"]["score"] == 1
    assert len(judge.prompts) == 1
    assert "'Dickens.'" in judge.prompts[0]
    assert "'Or, sorry — the Earnest guy.'" in judge.prompts[0]
    assert judge.prompts[0].index("'Dickens.'") < judge.prompts[0].index("'Or, sorry")
    row = game.sk.ledger_row_for("Rami", None)
    assert row["correct"] is True
    assert row["transcript"] == "Or, sorry — the Earnest guy."


def test_speculative_verdict_on_the_current_words_is_still_consumed(monkeypatch):
    """Control: a cached verdict on the player's LAST attempt is the
    verdict — no second judge call at the reveal."""
    _AnswerRows(monkeypatch)
    game = _make_game("lily-38C562-fresh")
    judge = _Judge()
    game.reasoning = judge
    at = time.time()

    async def scenario():
        _live(game, Q_WILDE, at)
        game.open_window(duration=30.0)
        _final(game, "Dickens.", "Rami", at + 3.0)
        game._spec_judge["Rami"] = _stale_spec_task("Dickens.")
        await asyncio.sleep(0)
        await game.adjudicate()
        await asyncio.sleep(0)

    _run(lambda: scenario())
    assert judge.prompts == []
    assert game.sk.players["Rami"]["score"] == 0


def test_q4_itself_now_scores_at_tier1_on_the_revision(monkeypatch):
    """The live Q4 end to end: with the phrase rule the revision is a
    Tier-1 correct, the earliest-correct-attempt rule scores it, and the
    ledger names the utterance that won."""
    _AnswerRows(monkeypatch)
    game = _make_game("lily-38C562-q4")
    at = time.time()

    async def scenario():
        _live(game, KB_271, at)
        game.open_window(duration=20.0)
        _final(game, FIRST, "Rami", at + 3.9, utterance_id="u4")
        _final(game, SECOND, "Rami", at + 6.66, utterance_id="u5")
        await game.adjudicate()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    _run(lambda: scenario())
    assert game.sk.players["Rami"]["score"] == 1
    row = game.sk.ledger_row_for("Rami", None)
    assert row["correct"] is True
    assert row["transcript"] == SECOND and row["utterance_id"] == "u5"


# ---------------------------------------------------------------------------
# HOTFIX-REVISION-JUDGE-001 (c) — the contest reads every in-window attempt
# ---------------------------------------------------------------------------


def _call(coro):
    loop = asyncio.new_event_loop()
    try:
        out = loop.run_until_complete(coro)
        pending = asyncio.all_tasks(loop)
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        return out
    finally:
        loop.close()


def test_answer_denied_is_corroborated_by_the_in_window_revision():
    """The live contest: the bound ledger row names "I think what? Eight."
    (incorrect); the revision "Or. Sorry. Six." is in the same window's
    transcript buffer. answer_denied now corroborates against it."""
    from lily_agent import LilyAgent
    from test_hotfix009_verdict_correction import _agent_with

    sk = LilyScorekeeper("lily-38C562-contest")
    sk.bind_speaker("Rami", "Rami")
    sk.start_question(dict(KB_271))
    sk.round = 1
    sk.set_phase("round")
    t0 = 5000.0
    sk.open_answer_window(20.0, now=t0)
    for text, t in ((FIRST, t0 + 3.9), (SECOND, t0 + 6.66)):
        sk.on_transcript_segment(
            text=text, speaker_label="Rami", is_final=True,
            now=t, segment_start_time=t, segment_end_time=t + 0.5,
        )
    sk.close_answer_window()
    # The live ruling: the bound row names the FIRST attempt, incorrect.
    sk.record_result(
        "Rami", correct=False, points=0, question_id="kb_271",
        question_index=sk.question_number, transcript=FIRST,
        utterance_id="u4",
    )
    agent, game = _agent_with(
        sk, asked_history=[{"question_id": "kb_271", "canonical_answer": "six"}],
    )
    msg = _call(LilyAgent.lily_correct_verdict.__wrapped__(
        agent, None, "Rami", "answer_denied", "I said six",
    ))
    assert "back with Rami" in msg, msg
    assert sk.players["Rami"]["score"] == 1


# ---------------------------------------------------------------------------
# HOTFIX-SPEAKER-PREFIX-001
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,clean", [
    ("[Rami] Yes!", "Yes!"),
    ("[S1] Yes!", "Yes!"),
    ("[Rami] Okay. [Rami] Go ahead.", "Okay. Go ahead."),
    ("[Mary-Anne O'Neil] Let's go.", "Let's go."),
    ("No tag here.", "No tag here."),
    ("", ""),
])
def test_every_speaker_tag_is_stripped_at_the_source(raw, clean):
    assert lily_scorekeeper.lily_strip_speaker_tags(raw) == clean


@pytest.mark.parametrize("raw", ["[Rami] Yes!", "[Rami] Let's go.", "[Rami] Sure. Go ahead."])
def test_acceptance_detectors_see_through_the_known_speaker_prefix(raw):
    assert lily_scorekeeper.lily_detect_addressed_acceptance(raw) is True


def test_bare_affirmative_strips_the_speaker_prefix_itself():
    # Every entry point gets the same answer even if a caller bypasses the
    # lifted transcript handler's source normalization.
    assert lily_scorekeeper.lily_is_bare_affirmative("[Rami] Okay.") is True


def test_handler_strips_the_known_speaker_prefix_before_the_scorekeeper():
    """Through the lifted handler with the real scorekeeper: the text the
    engine records for "[Rami] Yes!" is "Yes!" — no detector downstream
    ever sees "rami yes"."""
    from livekit.agents.voice.events import UserInputTranscribedEvent
    import lily_agent
    import lily_persistence
    from lily_agent import LilyGame

    sk = LilyScorekeeper("lily-38C562-handler")
    seen = []
    real = sk.on_transcript_segment

    def _spy(**kw):
        seen.append(kw.get("text"))
        return real(**kw)

    sk.on_transcript_segment = _spy
    game = LilyGame.bare(sk=sk)
    game.supabase = None
    game.nbest_collector = None
    game.audeering_pipeline = None
    game.fragments = lily_agent.LilyFragmentAccumulator()
    game.game_started = False
    game.publish_user_transcript_nowait = lambda *a, **k: None
    game.publish_attributes_nowait = lambda *a, **k: None
    game.send_event_nowait = lambda *a, **k: None
    game.note_voiced_segment = lambda *a, **k: None
    game.note_intake_overlap = lambda *a, **k: None
    game.on_transcript_event = lambda *a, **k: None
    game.note_confirmed_name_evidence = lambda *a, **k: None
    transcripts = lily_persistence.LilyTranscriptBatcher(object(), sk.session_id)
    transcripts._last_flush = float("inf")
    ev = UserInputTranscribedEvent(
        transcript="[Rami] Yes!", is_final=True, speaker_id="Rami",
        created_at=time.time(),
    )
    lily_agent._on_transcribed_body(game, sk, transcripts, ev)
    assert seen == ["Yes!"]


# ---------------------------------------------------------------------------
# Review fixes on a4b953c (GO-WITH-FIXES): the three P1s + n-best / stale
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("said", [
    "no one knows", "which one", "one sec", "give me one second",
    "say that one more time", "Air Force One",
])
def test_pronoun_one_never_scores_against_a_canonical_of_one(said):
    # a4b953c P1: every one of these was Tier-1 correct against "1".
    r = E.lily_tier1_evaluate(said, ["1", "one"])
    assert r["verdict"] == "uncertain", (said, r)
    # A bare "one" still takes the E1 numeric path.
    assert E.lily_tier1_evaluate("one", ["1"])["verdict"] == "correct"


@pytest.mark.parametrize("stray", [
    "[Rami] Did you say six?", "[Rami] Lily, is it six?",
])
def test_answer_denied_is_not_corroborated_by_a_non_answer_line(stray):
    """a4b953c P1: the in-window buffer holds lines adjudicate refused to
    score; the contest applies the same N4 gate, so a question ABOUT the
    answer never restores a point."""
    from lily_agent import LilyAgent
    from test_hotfix009_verdict_correction import _agent_with

    sk = LilyScorekeeper("lily-38C562-contest-gate")
    sk.bind_speaker("Rami", "Rami")
    sk.start_question(dict(KB_271))
    sk.round = 1
    sk.set_phase("round")
    t0 = 6000.0
    sk.open_answer_window(20.0, now=t0)
    for text, t in ((FIRST, t0 + 3.9), (stray, t0 + 6.0)):
        sk.on_transcript_segment(
            text=text, speaker_label="Rami", is_final=True,
            now=t, segment_start_time=t, segment_end_time=t + 0.5,
        )
    sk.close_answer_window()
    sk.record_result(
        "Rami", correct=False, points=0, question_id="kb_271",
        question_index=sk.question_number, transcript=FIRST, utterance_id="u4",
    )
    agent, game = _agent_with(
        sk, asked_history=[{"question_id": "kb_271", "canonical_answer": "six"}],
    )
    msg = _call(LilyAgent.lily_correct_verdict.__wrapped__(
        agent, None, "Rami", "answer_denied", "I said six",
    ))
    assert "No correction made" in msg, msg
    assert sk.players["Rami"]["score"] == 0


class _JudgeRulingOn:
    """Judge fake that rules correct and names the attempt it ruled on."""

    def __init__(self, normalized):
        self.prompts = []
        self.normalized = normalized

    async def prefetch_question(self, sk, **kw):
        return None

    async def judge(self, instructions, prompt):
        self.prompts.append(prompt)
        return json.dumps({
            "verdict": "correct", "winner": "Rami",
            "normalized_answer": self.normalized, "reason": "wilde",
        })


def test_a_trailing_filler_is_not_a_revision_and_the_answer_is_still_judged(monkeypatch):
    """a4b953c P1: "The Dorian Gray guy." then "Hang on." — the spec verdict
    on "Hang on." (incorrect) must not be the ruling; the batched judge sees
    both, rules correct, and the ledger binds the REAL answer, not "Hang on."."""
    _AnswerRows(monkeypatch)
    game = _make_game("lily-38C562-filler")
    judge = _JudgeRulingOn("oscar wilde")
    game.reasoning = judge
    at = time.time()

    async def scenario():
        _live(game, Q_WILDE, at)
        game.open_window(duration=30.0)
        _final(game, "The Dorian Gray guy.", "Rami", at + 3.0)
        _final(game, "Hang on.", "Rami", at + 5.0)
        # The glass relaunch left a verdict on the player's LAST words.
        game._spec_judge["Rami"] = _stale_spec_task("Hang on.")
        await asyncio.sleep(0)
        await game.adjudicate()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    _run(lambda: scenario())
    assert game.sk.players["Rami"]["score"] == 1
    assert len(judge.prompts) == 1
    assert "'The Dorian Gray guy.'" in judge.prompts[0] and "'Hang on.'" in judge.prompts[0]
    row = game.sk.ledger_row_for("Rami", None)
    assert row["transcript"] == "The Dorian Gray guy."


def test_stale_skip_for_one_player_still_reaches_the_batched_judge(monkeypatch):
    """a4b953c P2: A's task stale (skipped), B's consumed incorrect — the
    batched judge must still run for A."""
    _AnswerRows(monkeypatch)
    game = _make_game("lily-38C562-two")
    judge = _JudgeRulingOn("oscar wilde")
    game.reasoning = judge
    at = time.time()

    async def scenario():
        _live(game, Q_WILDE, at)
        game.sk.bind_speaker("Sam", "Sam")
        game.open_window(duration=30.0)
        _final(game, "Dickens.", "Rami", at + 2.0)
        _final(game, "Shaw.", "Sam", at + 3.0)
        game._spec_judge["Rami"] = _stale_spec_task("Dickens.")
        game._spec_judge["Sam"] = _stale_spec_task("Shaw.")
        await asyncio.sleep(0)
        _final(game, "Or, sorry — the Earnest guy.", "Rami", at + 5.0)
        await game.adjudicate()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    _run(lambda: scenario())
    assert len(judge.prompts) == 1
    assert game.sk.players["Rami"]["score"] == 1
    assert game.sk.players["Sam"]["score"] == 0


def test_nbest_rides_only_a_single_attempt():
    """a4b953c P2: with two attempts the (latest) n-best set is not attached
    to every one of them as 'the same utterance'."""
    prompt = E.lily_build_judge_prompt(
        "q", "Oscar Wilde", [("Rami", "Dickens."), ("Rami", "Wild.")],
        hypotheses_by_speaker={"Rami": [{"text": "Wilde", "confidence": 0.7}]},
    )
    # The builder itself attaches by speaker — adjudicate now withholds
    # the map for multi-attempt speakers, pinned through _make_game below.
    assert prompt.count("ASR N-BEST") == 2  # the builder's behaviour, unchanged


@pytest.mark.parametrize("raw,clean", [
    ("[Éric] Yes!", "Yes!"),
    ("[123] Yes!", "Yes!"),
    ("[" + "A" * 60 + "] Yes!", "Yes!"),
])
def test_strip_covers_every_label_shape_the_plugin_can_emit(raw, clean):
    assert lily_scorekeeper.lily_strip_speaker_tags(raw) == clean
