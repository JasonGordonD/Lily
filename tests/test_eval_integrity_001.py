"""WO-LILY-EVAL-INTEGRITY-001 — adjudication integrity: numeric answers,
the committed-answer-shape regression, the clarify one-way door, the
contest regex, uncorroborated correction grounds, solo miss lines.

Every test here DRIVES the classifiers / the clarify path / the ledger —
none asserts on source text. Auditor C's scratch script
(scratchpad/audit_scenarios.py, sections B and F) is the seed; the
utterance lists are the auditor's, executed here against the code.

E1  `_soundex` stripped non-letters, so every digit string keyed "" and
    1968 ≡ 1969 ≡ 1786 ≡ 1776 by "phonetic" agreement (sim ≥ 0.75).
E2  the WO-3 shape rule diverted confident answers with a trailing filler
    or a title ending in a function word to clarify+withdraw, while still
    hard-binding true fragments ("the Irish guy", "I don't know", "face").
E3  the shape clarify withdrew the bind and never put it back — the
    affirmative reply text became the recorded answer.
E4  `_VERDICT_CONTEST_RE` matched inside words ("Detro-it", "B-right").
E5  misheard / wrong_rule / out_of_window corrections were uncorroborated.
E6  solo tables heard "Nobody landed it".
"""

import asyncio
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_config
import lily_evaluation as E
import lily_say_gate
import lily_scorekeeper as S
from lily_scorekeeper import LilyScorekeeper
from test_bind_dispute_p0 import (
    FIXTURE_0814,
    FIXTURE_0815,
    Q_WILDE,
    _arm_wilde,
    _final,
    _fixture_line,
    _make_game,
    _run,
)

MCQ = {
    "id": "mc_1",
    "prompt": "Which planet is largest?",
    "canonical_answer": "Jupiter",
    "acceptable_answers": ["jupiter"],
    "choices": ["Mars", "Jupiter", "Venus", "Mercury"],
}


# ===========================================================================
# E1 — numeric adjudication
# ===========================================================================

NORMALIZER_TABLE = [
    ("nineteen sixty-eight", "1968"),
    ("nineteen sixty eight", "1968"),
    ("twenty twenty-four", "2024"),
    ("nineteen oh five", "1905"),
    ("fourteen ninety-two", "1492"),
    ("eighteen hundred", "1800"),
    ("two thousand and one", "2001"),
    ("one hundred", "100"),
    ("three thousand two hundred", "3200"),
    ("twelve", "12"),
    ("seven", "7"),
    ("twenty", "20"),
    ("one thousand", "1000"),
    ("fourth", "4"),
    ("twenty-first", "21"),
    ("1968", "1968"),
    ("1,968", "1968"),
    ("Canberra", None),
    ("nineteen Canberra", None),
    ("", None),
]


def test_spoken_number_normalizer_table():
    for spoken, digits in NORMALIZER_TABLE:
        assert E.lily_spoken_number_to_digits(spoken) == digits, spoken


def test_normalize_answer_applies_spoken_numbers():
    assert E.lily_normalize_answer("the fourth") == "4"
    assert E.lily_normalize_answer("um, nineteen sixty-eight") == "1968"
    assert E.lily_normalize_answer("in nineteen sixty eight") == "in 1968"
    # A single digit-word inside a longer phrase is left alone — it is a
    # word there ("Air Force One"), not a count.
    assert E.lily_normalize_answer("Air Force One") == "air force one"


SCORING_TABLE = [
    # (attempt, expected, must_be_correct)
    ("1968", "1969", False),
    ("1786", "1776", False),
    ("1000", "100", False),
    ("100", "1000", False),
    ("1969", "1996", False),
    ("42", "43", False),
    ("7", "8", False),
    ("nineteen sixty eight", "1969", False),
    ("Apollo 11", "Apollo 12", False),
    ("1968", "1968", True),
    ("nineteen sixty-eight", "1968", True),
    ("nineteen sixty eight", "1968", True),
    ("one hundred", "100", True),
    ("the fourth", "4", True),
    ("twelve", "12", True),
    ("in 1968", "1968", True),
    ("1968", "nineteen sixty-eight", True),
]


def test_numeric_scoring_table():
    for attempt, expected, must in SCORING_TABLE:
        r = E.lily_tier1_evaluate(attempt, [expected])
        if must:
            assert r["verdict"] == "correct", (attempt, expected, r)
            assert r["method"] in ("exact", "numeric", "containment"), r
        else:
            # Tier 1 never rejects freeform; "not correct" is uncertain
            # (escalates to the judge). It must NOT be a phonetic hit and
            # must not sit in the clarify band (a confident "1968" is a
            # committed answer, not something to ask about).
            assert r["verdict"] == "uncertain", (attempt, expected, r)
            # method="numeric" is the operator receipt on a pure digit
            # mismatch; mixed phrases ("Apollo 11") report no method.
            assert r["method"] in (None, "numeric"), r
            assert r["similarity"] < 0.73, r
            if E._numeric_only(E.lily_normalize_answer(attempt)):
                assert r["method"] == "numeric", r


def test_numeric_mismatch_never_phonetic():
    for canon, att in [("1969", "1968"), ("1776", "1786"), ("100", "1000")]:
        r = E.lily_tier1_evaluate(att, [canon])
        assert r["method"] != "phonetic", (canon, att, r)
        assert r["verdict"] != "correct", (canon, att, r)


def test_non_numeric_near_miss_still_phonetic_eligible():
    r = E.lily_tier1_evaluate("Kanberra", ["canberra"])
    assert r["verdict"] == "correct"
    assert r["method"] in ("fuzzy", "phonetic")
    # Homophonic initial with a real letter body keys identically…
    assert E._soundex("Kanberra") == E._soundex("Canberra")
    # …while digit strings never key to a shared empty code.
    assert E._soundex("1968") != E._soundex("1969")
    assert E._soundex("1968") != ""


# ===========================================================================
# E2 — committed-answer shape: regression list binds, gap list clarifies
# ===========================================================================

REGRESSION_LIST = [
    # confident WRONG/RIGHT answers with a trailing filler
    "Sam Shepard, uh",
    "George Bernard Shaw, hmm.",
    "Oscar Wilde, um",
    "Wild, uh",
    # titles / answers ending in a function word
    "Rebel Without a",
    "Once Upon a Time in",
    "Queen Elizabeth the",
    # the design-intent class and plain committed answers
    "how many states",
    "Paris",
    "Benjamin. Franklin.",
    "Saturn?",
    "Michelangelo",
    "I'm thinking Aphrodite",
    "The. State.",
    "I never can remember the name of it. So I'm. I'm surrendering.",
]

GAP_LIST = [
    # search phrases
    "what's the guy",
    "who's the guy",
    "the Irish guy",
    "Oscar something",
    # explicit thinking / deferral
    "I don't know",
    "no idea",
    "let me think",
    "give me a second",
    "hold on",
    "I'm thinking",
    "thinking",
    # hint shape
    "It starts with an O",
    "tip of my tongue",
    # self-negation
    "Oscar… no wait",
    "Oscar, no",
    # interjections alone
    "oh god",
    "damn",
    "ugh",
    "shoot",
    # bare fragment token / the live fragment and its tail
    "face",
    "He. Face.",
    "What's he. Face. Uh.",
    "It was the. Um.",
    "it's the. the.",
]


def test_regression_list_keeps_committed_shape():
    for text in REGRESSION_LIST:
        assert E.lily_uncommitted_answer_shape(
            text, expected_answers=["Oscar Wilde"]
        ) is None, text
        assert E.lily_uncommitted_answer_shape(text) is None, text


def test_gap_list_has_uncommitted_shape():
    for text in GAP_LIST:
        assert E.lily_uncommitted_answer_shape(
            text, expected_answers=["Oscar Wilde"]
        ) is not None, text


def test_trailing_disfluency_is_stripped_before_similarity():
    assert E.lily_normalize_answer("Sam Shepard, uh") == "sam shepard"
    assert E.lily_normalize_answer("George Bernard Shaw, hmm.") == (
        "george bernard shaw"
    )
    assert E.lily_strip_trailing_disfluency("Wild, uh") == "Wild"
    assert E.lily_strip_trailing_disfluency("What's he. Face. Uh.") == (
        "What's he. Face."
    )
    r = E.lily_tier1_evaluate("Oscar Wilde, um", ["oscar wilde"])
    assert r["verdict"] == "correct" and r["method"] == "exact"


def test_known_answer_and_expected_token_overrides():
    # A function-word tail after a known answer is committed.
    assert E.lily_uncommitted_answer_shape(
        "Wilde, the", expected_answers=["Oscar Wilde"]
    ) is None
    # A bare token that IS the expected answer is committed.
    assert E.lily_uncommitted_answer_shape(
        "face", expected_answers=["Face"]
    ) is None


def test_mcq_letter_pick_with_trailing_filler_accepts_the_pick():
    for text, idx in [("B. Um.", 1), ("C, uh", 2), ("the first one, uh", 0),
                      ("B", 1)]:
        r = E.lily_tier1_evaluate_question(text, MCQ)
        assert r["selected_index"] == idx, (text, r)
        assert E.lily_answer_shaped(text, MCQ), text
    r = E.lily_tier1_evaluate_question("B. Um.", MCQ)
    assert r["verdict"] == "correct"


def test_regression_list_binds_on_the_live_path_none_clarify():
    """Drive the real record → clarify seam: a confident wrong answer with
    a trailing filler binds (disfluency-stripped) and no clarify fires."""
    for text, bound in [
        ("Sam Shepard, uh", "Sam Shepard"),
        ("George Bernard Shaw, hmm.", "George Bernard Shaw"),
        ("Rebel Without a", "Rebel Without a"),
        ("Wild, uh", "Wild"),
    ]:
        game = _make_game("lily-E2-bind")
        now = _arm_wilde(game)
        game.sk.set_pacing("relaxed")

        def _go():
            game.open_window()
            _final(game, text, now + 3)

        _run(_go)
        assert "Rami" not in game.pending_clarify, text
        assert "Rami" in game.sk.answer_candidates, text
        # The recorded transcript stays the true utterance; what is
        # EVALUATED (normalized) is the disfluency-stripped answer.
        recorded = game.sk.answer_candidates["Rami"]["text"]
        assert E.lily_normalize_answer(recorded) == E.lily_normalize_answer(bound), text
        assert E.lily_strip_trailing_disfluency(recorded) == bound, text


def test_gap_list_clarifies_on_the_live_path_none_bind():
    for text in ["the Irish guy", "I don't know", "It starts with an O",
                 "Oscar… no wait", "He. Face.", "What's he. Face. Uh."]:
        game = _make_game("lily-E2-gap")
        now = _arm_wilde(game)
        game.sk.set_pacing("relaxed")

        def _go():
            game.open_window()
            _final(game, text, now + 3)

        _run(_go)
        assert "Rami" in game.pending_clarify, text
        assert "Rami" not in game.sk.answer_candidates, text
        assert game.sk.answer_window_open, text


# ===========================================================================
# E3 — the clarify is a door that opens both ways
# ===========================================================================

FRAGMENT = "What's he. Face. Uh."


def _clarify_then(reply: str, first: str = FRAGMENT):
    game = _make_game("lily-E3")
    now = _arm_wilde(game)
    game.sk.set_pacing("relaxed")

    async def _go():
        game.open_window()
        _final(game, first, now + 3)
        assert "Rami" in game.pending_clarify
        assert "Rami" not in game.sk.answer_candidates
        assert game.pending_clarify["Rami"].get("withdrawn") is not None
        await asyncio.sleep(0.05)
        _final(game, reply, now + 8)
        await asyncio.sleep(0.05)

    _run(lambda: _go())
    return game


def test_clarify_affirmative_rebinds_the_original_disfluency_stripped():
    game = _clarify_then("Yes, that's my answer.")
    assert "Rami" not in game.pending_clarify
    cand = game.sk.answer_candidates.get("Rami")
    assert cand is not None
    # The ORIGINAL candidate is the attempt — never the reply text.
    assert cand["text"] == "What's he. Face."
    assert "my answer" not in cand["text"].lower()
    # The reply never aired a receipt of its own.
    assert not any("locked in" in s.lower() for s in game.session.said)


def test_clarify_negative_drops_the_candidate():
    game = _clarify_then("No, just thinking.")
    assert "Rami" not in game.pending_clarify
    assert "Rami" not in game.sk.answer_candidates
    assert game.sk.answer_window_open
    aired = " ".join(game.session.said + game.session.instructions).lower()
    assert "oscar wilde" not in aired


def test_clarify_new_answer_binds_the_new_one():
    game = _clarify_then("Oscar Wilde")
    assert "Rami" not in game.pending_clarify
    ledger = [e for e in game.sk.score_ledger if e.get("cause") == "answer"]
    assert ledger and ledger[-1]["correct"] is True
    assert ledger[-1]["transcript"] == "Oscar Wilde"


def test_confirmation_is_never_an_answer_attempt():
    # Auditor C section B, re-run after E2: "Wild, uh" now BINDS (no
    # clarify) — and the follow-up "Yes, that's my answer." must not
    # REVISE that bind through the self-correction path either.
    for t in ["Yes, that's my answer.", "final answer", "yeah, lock it in",
              "That's my final answer!", "I'm sure."]:
        assert E.lily_confirmation_utterance(t), t
        assert E.lily_non_answer_utterance(t, Q_WILDE, ["Rami"]) == "confirmation", t
    for t in ["the answer is Paris", "Oscar Wilde", "yes, Oscar Wilde",
              "Sam Shepard, uh"]:
        assert not E.lily_confirmation_utterance(t), t
    # A yes/no question keeps "yes" scoreable (answer-surface override).
    assert E.lily_non_answer_utterance(
        "yes", {"canonical_answer": "Yes", "acceptable_answers": ["yes"]}
    ) is None

    game = _make_game("lily-E3-confirm")
    now = _arm_wilde(game)
    game.sk.set_pacing("relaxed")

    async def _go():
        game.open_window()
        _final(game, "Wild, uh", now + 3)
        await asyncio.sleep(0.05)
        _final(game, "Yes, that's my answer.", now + 8)
        await asyncio.sleep(0.05)

    _run(lambda: _go())
    assert game.sk.answer_candidates["Rami"]["text"] == "Wild, uh"


def test_clarify_negative_then_real_answer_scores():
    game = _make_game("lily-E3-recover")
    now = _arm_wilde(game)
    game.sk.set_pacing("relaxed")

    async def _go():
        game.open_window()
        _final(game, "the Irish guy", now + 3)
        await asyncio.sleep(0.05)
        _final(game, "no, still thinking", now + 6)
        await asyncio.sleep(0.05)
        assert "Rami" not in game.sk.answer_candidates
        _final(game, "Oscar Wilde", now + 9)
        await asyncio.sleep(0.05)

    _run(lambda: _go())
    ledger = [e for e in game.sk.score_ledger if e.get("cause") == "answer"]
    assert ledger and ledger[-1]["correct"] is True
    assert game.sk.players["Rami"]["score"] == 1


# ===========================================================================
# E4 — the contest regex
# ===========================================================================

CONTEST_FALSE_POSITIVES = [
    "I say Detroit",
    "I say Detroit, final.",
    "I said Bright",
    "I said it's Franklin",
    "the answer is a dog",
    "the answer is a horse",
    "the answer is A. Lincoln",
    "the answer is Paris",
    "hmm, I think it's oxygen",
    "I'm thinking Aphrodite",
    "diamond",
]


def test_contest_regex_false_positive_list_never_fires():
    for t in CONTEST_FALSE_POSITIVES:
        assert not S.lily_detect_verdict_contest(t), t
        assert not S.lily_detect_verdict_contest(t, multiple_choice=True), t


def test_contest_regex_four_live_lines_and_true_contests_fire():
    for t in [
        _fixture_line(FIXTURE_0814, "21:54:50 Rami"),
        _fixture_line(FIXTURE_0814, "21:54:54 Rami"),
        _fixture_line(FIXTURE_0815, "17:49:27 Rami"),
        _fixture_line(FIXTURE_0815, "17:49:47 Rami"),
        "you misheard me",
        "I said Mercury, not Mars",
        "I said it right",
        "that's wrong, I was right",
        "the correct answer is A",
        "go back to my answer",
    ]:
        assert S.lily_detect_verdict_contest(t), t


def test_bare_letter_contest_only_on_multiple_choice():
    assert S.lily_detect_verdict_contest("the answer was B", multiple_choice=True)
    assert not S.lily_detect_verdict_contest(
        "the answer was B", multiple_choice=False
    )
    # Unknown format keeps the anchored letter arm (the X12 live line).
    assert S.lily_detect_verdict_contest("the answer was B")


# ===========================================================================
# E5 — corroborated correction grounds
# ===========================================================================


def _denied(transcript: str, canonical: str = "Oscar Wilde", *,
            pacing: str = "timed") -> LilyScorekeeper:
    sk = LilyScorekeeper("lily-E5")
    sk.bind_speaker("S1", "Rami")
    sk.set_pacing(pacing)
    sk.start_question({"id": "q_w", "prompt": "p", "canonical_answer": canonical})
    sk.record_result("Rami", correct=False, points=0, question_id="q_w",
                     question_index=1, transcript=transcript, utterance_id="u1")
    return sk


def test_misheard_accepts_a_corroborating_near_miss():
    sk = _denied("Wild")
    entry = sk.correct_verdict(
        "Rami", grounds="misheard", delta=1, canonical_answer="Oscar Wilde",
        corroborating_attempt="Wild",
    )
    assert entry is not None and entry["grounds"] == "misheard"
    assert sk.players["Rami"]["score"] == 1


def test_misheard_refuses_without_corroboration(caplog):
    sk = _denied("Shaw")
    with caplog.at_level(logging.WARNING):
        assert sk.correct_verdict(
            "Rami", grounds="misheard", delta=1, canonical_answer="Oscar Wilde",
            corroborating_attempt="Shaw",
        ) is None
        assert sk.correct_verdict(
            "Rami", grounds="misheard", delta=1, canonical_answer="Oscar Wilde",
        ) is None
    assert sk.players["Rami"]["score"] == 0
    assert sk.last_correction_refusal["reason"] == "uncorroborated_misheard"
    assert any("VERDICT_CORRECTION_UNCORROBORATED" in r.getMessage()
               and "misheard" in r.getMessage() for r in caplog.records)


def test_misheard_once_per_question_and_never_on_a_scored_row():
    sk = _denied("Wild")
    assert sk.correct_verdict(
        "Rami", grounds="misheard", delta=1, canonical_answer="Oscar Wilde",
        corroborating_attempt="Wild",
    ) is not None
    assert sk.correct_verdict(
        "Rami", grounds="misheard", delta=1, canonical_answer="Oscar Wilde",
        corroborating_attempt="Wild",
    ) is None
    assert sk.players["Rami"]["score"] == 1


def test_wrong_rule_requires_relaxed_and_a_clock_denial():
    # Relaxed table, a late_answer row on the question: the clock denied a
    # relaxed answer — the diamond class. Accepted.
    sk = _denied("It's on me.", canonical="diamond", pacing="relaxed")
    sk.apply_score_event("Rami", cause="late_answer", correct=False, points=0,
                         question_id="q_w", question_index=1,
                         transcript="I said diamond", utterance_id="u2")
    assert sk.correct_verdict("Rami", grounds="wrong_rule", delta=1) is not None
    # Timed table: a clock is the rule — refused.
    sk = _denied("It's on me.", canonical="diamond", pacing="timed")
    sk.apply_score_event("Rami", cause="late_answer", correct=False, points=0,
                         question_id="q_w", question_index=1,
                         transcript="I said diamond", utterance_id="u2")
    assert sk.correct_verdict("Rami", grounds="wrong_rule", delta=1) is None
    assert sk.last_correction_refusal["reason"] == "wrong_rule_not_relaxed"
    # Relaxed but a plain wrong answer (no clock denial): refused.
    sk = _denied("Shaw", pacing="relaxed")
    assert sk.correct_verdict("Rami", grounds="wrong_rule", delta=1) is None
    assert sk.last_correction_refusal["reason"] == "wrong_rule_no_clock_denial"


def test_out_of_window_requires_a_late_record_inside_the_grace():
    sk = _denied("Oscar Wilde")
    assert sk.correct_verdict("Rami", grounds="out_of_window", delta=1) is None
    assert sk.last_correction_refusal["reason"] == "out_of_window_no_late_record"
    grace = lily_config.late_answer_grace_seconds()
    sk.late_answers.append({
        "player": "Rami", "question_id": "q_w", "question_index": 1,
        "seconds_late": grace + 5.0, "within_grace": False,
    })
    assert sk.correct_verdict("Rami", grounds="out_of_window", delta=1) is None
    assert sk.last_correction_refusal["reason"] == "out_of_window_past_grace"
    sk.late_answers.append({
        "player": "Rami", "question_id": "q_w", "question_index": 1,
        "seconds_late": max(0.0, grace - 0.5), "within_grace": True,
    })
    assert sk.correct_verdict("Rami", grounds="out_of_window", delta=1) is not None
    assert sk.players["Rami"]["score"] == 1


def _agent_with(sk, asked_history=None):
    from lily_agent import LilyAgent

    class _FakeGame:
        def __init__(self, sk):
            self.game_started = True
            self.sk = sk
            self.supabase = None
            self.events = []
            self.asked_history = list(asked_history or [])
            self._contest_note = "[verdict contest — live]"

        def send_event_nowait(self, event_type, payload):
            self.events.append((event_type, dict(payload)))

        def publish_attributes_nowait(self):
            pass

    agent = LilyAgent.__new__(LilyAgent)
    game = _FakeGame(sk)
    agent._game = game
    return agent, game


def _call(coro):
    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(coro)
        pending = asyncio.all_tasks(loop)
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        return result
    finally:
        loop.close()


def test_tool_misheard_corroborates_from_the_in_window_transcript_log():
    from lily_agent import LilyAgent

    sk = LilyScorekeeper("lily-E5-tool")
    sk.bind_speaker("S1", "Rami")
    sk.start_question({"id": "q_w", "prompt": "p", "canonical_answer": "Oscar Wilde"})
    now = time.time()
    sk.open_answer_window(now=now, untimed=True)
    # The real in-window utterance, through the production recording path.
    sk.on_transcript_segment(text="Wild", speaker_label="S1", is_final=True,
                             now=now + 1, segment_start_time=now + 1,
                             segment_end_time=now + 2)
    sk.close_answer_window()
    # …but a filler was what got bound and denied.
    sk.record_result("Rami", correct=False, points=0, question_id="q_w",
                     question_index=sk.question_number,
                     transcript="It's on me.", utterance_id="u1")
    agent, game = _agent_with(
        sk, asked_history=[{"question_id": "q_w", "canonical_answer": "Oscar Wilde"}]
    )
    msg = _call(LilyAgent.lily_correct_verdict.__wrapped__(
        agent, None, "Rami", "misheard", "you had Wilde",
    ))
    assert "back with Rami" in msg
    assert sk.players["Rami"]["score"] == 1


def test_tool_misheard_refused_writes_the_reason_back_to_the_contest_note():
    from lily_agent import LilyAgent

    sk = _denied("Shaw")
    agent, game = _agent_with(
        sk, asked_history=[{"question_id": "q_w", "canonical_answer": "Oscar Wilde"}]
    )
    msg = _call(LilyAgent.lily_correct_verdict.__wrapped__(
        agent, None, "Rami", "misheard", "you misheard me",
    ))
    assert "No correction made" in msg
    assert "misheard" in msg
    assert sk.players["Rami"]["score"] == 0
    assert game.events == []
    # S6 closed loop: the refusal and its reason ride the contest note.
    assert game._contest_note and "refused" in game._contest_note.lower()
    assert "misheard" in game._contest_note


def test_misheard_after_every_miss_does_not_convert_misses():
    """The exploit: 'you misheard me' after a plain wrong answer."""
    from lily_agent import LilyAgent

    sk = _denied("Sam Shepard")
    agent, game = _agent_with(
        sk, asked_history=[{"question_id": "q_w", "canonical_answer": "Oscar Wilde"}]
    )
    for _ in range(3):
        msg = _call(LilyAgent.lily_correct_verdict.__wrapped__(
            agent, None, "Rami", "misheard", "you misheard me",
        ))
        assert "No correction made" in msg
    assert sk.players["Rami"]["score"] == 0


# ===========================================================================
# E6 — solo miss lines
# ===========================================================================


def test_solo_verdict_sheet_never_says_nobody():
    s = S.lily_verdict_sheet(answer="Oscar Wilde", winner=None,
                             winner_scored=False, solo=True)
    assert s == "Not this one — it was Oscar Wilde."
    assert "nobody" not in s.lower()
    # Receipt-aired variant is already solo-neutral; multiplayer unchanged.
    assert S.lily_verdict_sheet(answer="X", winner=None, winner_scored=False) == (
        "Nobody landed it — it was X."
    )
    assert S.lily_verdict_sheet(answer="", winner=None, winner_scored=False,
                                solo=True) == "Not this one."


def test_solo_reair_line_never_says_nobody():
    line = lily_say_gate.lily_verdict_reair_line(
        correct=False, answer="Oscar Wilde", solo=True
    )
    assert line == "Not this one — Oscar Wilde."
    assert lily_say_gate.lily_verdict_reair_line(
        correct=False, answer="Oscar Wilde"
    ) == "Nobody had it — Oscar Wilde."


def test_solo_reveal_instructions_miss_branch_names_the_player():
    game = _make_game("lily-E6")
    _arm_wilde(game)
    instr = game._reveal_instructions(Q_WILDE, None, None, "", 1)
    assert "Nobody got it" not in instr
    assert "Rami didn't land this one" in instr
    assert "never say 'nobody'" in instr
    # An EMPTY roster is not a table of one — the generic line stands.
    empty = _make_game("lily-E6-empty")
    empty.armed_question = dict(Q_WILDE)
    empty.sk.start_question(empty.armed_question)
    assert "Nobody got it" in empty._reveal_instructions(Q_WILDE, None, None, "", 1)
    # Two players: the multiplayer phrasing stands.
    game.sk.bind_speaker("S2", "Deb")
    instr = game._reveal_instructions(Q_WILDE, None, None, "", 1)
    assert "Nobody got it" in instr


def test_solo_miss_airs_the_solo_line_on_the_live_path(monkeypatch):
    """A solo TIMED table lets the clock run out with no answer: no
    receipt aired, so the sheet takes the no-receipt miss branch — the
    one that said "Nobody landed it" to a table of one."""
    monkeypatch.setenv("LILY_ANSWER_WINDOW_SECONDS", "0.3")
    game = _make_game("lily-E6-live")
    _arm_wilde(game)
    game.sk.set_pacing("timed")

    async def _go():
        game.open_window()
        await asyncio.sleep(0.8)

    _run(lambda: _go())
    said = " || ".join(game.session.said)
    assert "Not this one — it was Oscar Wilde." in said
    assert "nobody" not in said.lower()
