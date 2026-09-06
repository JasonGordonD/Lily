"""Composition review of integ/w6 (428bff1) — GO-WITH-FIXES, applied here.

P1-1  The B1 terminal-letter regex listed articles-adjacent lead-ins
      (`it's`, `is`, `with`, `for`, `and`, `or` …) with the lead-in
      optional, so any utterance ending in the word "a" bound to option A:
      "I think it's a", "give me a", "or a". With the 2.5 s endpointing cap
      committing the first clause of "I think it's a … gas giant", the
      question burns and B1 precedence refuses the real answer. Operator
      rule: conversational speech never binds.
P2-1  The per-turn EOT receipt key is `source` (operator: `eot_probability:
      null, source: "no_debug_record"`), not `eot_source`.
P2-2  The on-demand choices re-ask (act `question_reask`) could be muted by
      RegenGate's stubborn-repeat branch when the previous turn left
      `_reair_regen_pending` set — it repeats the question by design.
P2-3  "let's play" / "play on" / "resume" did not lift a sticky pause.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_evaluation  # noqa: E402
import lily_scorekeeper  # noqa: E402

_CHOICES = ["Jupiter", "Saturn", "Neptune", "Uranus"]


def _selected_index(text):
    r = lily_evaluation.lily_tier1_evaluate_mc(text, _CHOICES, "Jupiter")
    return r["selected_index"], r["method"]


def test_trailing_article_a_never_binds_as_option_a():
    for text in (
        "I think it's a",
        "it's a",
        "is it a",
        "what about a",
        "give me a",
        "that was a",
        "or a",
        "and a",
        "I think it's a.",
    ):
        assert lily_evaluation.lily_mc_unresolved(
            text, {"choices": _CHOICES, "canonical_answer": "Jupiter"}
        ), text


def test_real_letter_picks_still_bind():
    assert _selected_index("I would comfortably say a.") == (0, "letter")
    assert _selected_index("a") == (0, "letter")
    assert _selected_index("a.") == (0, "letter")
    assert _selected_index("say b.") == (1, "letter")
    assert _selected_index("I'd say c") == (2, "letter")
    assert _selected_index("A lot of people think B") == (1, "letter")
    assert _selected_index("it's d") == (3, "letter")
    assert _selected_index("I think a") == (0, "letter")
    assert _selected_index("I'd go A") == (0, "letter")


def test_choices_reask_is_never_muted_as_a_stubborn_repeat():
    """Real say pipeline, real game: the previous turn left the regen flag
    set and the question is already in agent_turns — the re-ask (act
    question_reask) still airs; an ordinary verbatim repeat is still muted."""
    import logging

    from lily_agent import Silence, SpeechTurn, run_say_pipeline
    from test_airgate_001 import _pipeline_agent
    from test_bind_dispute_p0 import _make_game
    from test_composition_followup_w6 import Q_NUMERIC, _arm

    logging.disable(logging.CRITICAL)
    try:
        q = Q_NUMERIC["prompt"]
        reask = f"Same question, now with options. {q} A) 1963  B) 1966  C) 1969  D) 1972"

        def build(act):
            game = _make_game()
            _arm(game, dict(Q_NUMERIC), window=30.0)
            game.sk.current_question["choices"] = ["1963", "1966", "1969", "1972"]
            if getattr(game.sk, "agent_turns", None) is None:
                game.sk.agent_turns = []
            game.sk.agent_turns.append(q)
            game._dispatched_act_by_speech = {"speech_x": act} if act else {}
            agent = _pipeline_agent()
            agent._reair_regen_pending = True
            return game, agent

        game, agent = build("question_reask")
        turn = SpeechTurn(text=reask, raw=reask, game=game, agent=agent, speech_id="speech_x")
        out = run_say_pipeline(turn)
        assert not isinstance(out, Silence), getattr(out, "reason", None)

        game, agent = build(None)
        turn = SpeechTurn(text=reask, raw=reask, game=game, agent=agent, speech_id="speech_x")
        out = run_say_pipeline(turn)
        assert isinstance(out, Silence) and out.reason == "stubborn_repeat"
    finally:
        logging.disable(logging.NOTSET)


def test_explicit_play_words_release_a_sticky_pause():
    for text in ("let's play", "okay let's play", "play on", "resume", "lily, resume"):
        assert lily_scorekeeper.lily_detect_pause_release(text), text


def test_bare_acknowledgements_do_not_release_a_pause():
    # B2: explicit resume only — an "okay" answering something else must
    # not lift the hold. (Operator decision to widen; not widened here.)
    for text in ("okay", "yes", "alright"):
        assert not lily_scorekeeper.lily_detect_pause_release(text), text
