"""WO-LILY-PATCH-001 T5 — window hygiene, from the Aug 6 Socrates fixture.

Live evidence: two PRE-question Mars-conversation fragments were folded
into the Socrates window at the delivery claim (the WS-5 backfill) and
scored, consuming Rami's judgment — his clean "Socrates" final then went
inert. Separately, "Yeah" (a backchannel) was formally scored incorrect
and a bare "Chris." fragment wrote a null-player answers row.

Pinned: (a) the pre-window buffer covers CLAIM-TO-OPEN only — pre-claim
speech can never enter a question's adjudication; (c) non-answer-shaped
utterances in an open window are logged, never adjudicated as attempts —
with the answer-surface override keeping yes/no questions honest.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_evaluation
from lily_scorekeeper import LilyScorekeeper

SOCRATES_Q = {
    "prompt": "Which philosopher drank the hemlock?",
    "canonical_answer": "Socrates",
    "acceptable_answers": ["socrates"],
}


def _sk_with_open_window():
    sk = LilyScorekeeper("t5-fixture")
    sk.bind_speaker("S1", "Rami")
    sk.bind_speaker("S2", "Chris")
    sk.start_question(dict(SOCRATES_Q))
    sk.open_answer_window(duration=30.0, now=100.0)
    return sk


# -- (c) evaluator hygiene -----------------------------------------------------


def test_backchannel_is_logged_never_scored():
    sk = _sk_with_open_window()
    result = sk.on_transcript_segment(
        text="Yeah.", speaker_label="S1", is_final=True,
        now=105.0, segment_start_time=105.0,
    )
    assert result.get("non_answer") == "backchannel"
    assert result.get("candidate_recorded") is not True
    assert not sk.answer_candidates
    assert sk.players["Rami"]["answers_attempted"] == 0


def test_bare_roster_name_fragment_is_not_an_attempt():
    sk = _sk_with_open_window()
    result = sk.on_transcript_segment(
        text="Chris.", speaker_label="S2", is_final=True,
        now=105.0, segment_start_time=105.0,
    )
    assert result.get("non_answer") == "bare_name"
    assert not sk.answer_candidates


def test_real_answer_still_scores_after_a_backchannel():
    """The Socrates repair: with junk no longer consuming his judgment,
    the clean final becomes the candidate."""
    sk = _sk_with_open_window()
    sk.on_transcript_segment(
        text="Yeah.", speaker_label="S1", is_final=True,
        now=104.0, segment_start_time=104.0,
    )
    result = sk.on_transcript_segment(
        text="Socrates", speaker_label="S1", is_final=True,
        now=116.0, segment_start_time=116.0,
    )
    assert result.get("candidate_recorded") is True
    assert "Rami" in sk.answer_candidates
    verdict = lily_evaluation.lily_tier1_evaluate_question(
        sk.answer_candidates["Rami"]["text"], SOCRATES_Q
    )
    assert verdict["verdict"] == "correct"


def test_answer_surface_override_keeps_yes_no_questions_scoreable():
    q = {
        "prompt": "Is the Great Wall visible from low orbit?",
        "canonical_answer": "yes",
        "acceptable_answers": ["yes", "yeah"],
    }
    assert lily_evaluation.lily_non_answer_utterance("Yeah", q, ["Rami"]) is None
    # And an MC letter/choice is always an answer.
    mc = {"prompt": "?", "canonical_answer": "Saturn",
          "acceptable_answers": ["saturn"],
          "choices": ["Jupiter", "Saturn", "Uranus", "Neptune"]}
    assert lily_evaluation.lily_non_answer_utterance("Saturn", mc, []) is None


# -- (a) claim-to-open only ----------------------------------------------------


def test_pre_claim_finals_never_enter_the_buffer():
    """The contamination fixture: finals recorded BEFORE the delivery
    claim are dropped at the claim, not folded into the buffer."""
    import lily_say_gate
    from lily_agent import LilyGame

    game = LilyGame.bare()
    game.sk = _sk_with_open_window()
    game.sk.close_answer_window()
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game.game_started = True
    game.armed_question = dict(SOCRATES_Q)
    game._pending_delivery_qnum = game.sk.question_number
    game._pre_window_segments = []
    game._recent_finals = [
        (99.0, {"text": "I love Mars talk", "speaker_label": "S1",
                "segment_start_time": 99.0, "segment_end_time": 99.5}),
        (99.6, {"text": "Mars was yesterday's question", "speaker_label": "S2",
                "segment_start_time": 99.6, "segment_end_time": 99.9}),
    ]
    game._note_mc_delivery_start = lambda qnum: None
    outcome = game.register_delivery_claim(
        SOCRATES_Q["prompt"], speech_id="sp1"
    )
    assert outcome in ("claimed_structural", "claimed_core_sentence")
    assert game._pre_window_segments == []  # nothing backfilled
    assert game._recent_finals == []        # and the store is drained
