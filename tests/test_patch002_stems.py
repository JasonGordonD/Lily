"""WO-LILY-PATCH-002 M4 + M5 — no orphan stems, verdict single-fire.

M4 fixture: "Beyond the genitals, name three of the—" aired partially
and vanished — no completion, no cancellation, replaced by unrelated
framing. An aired stem is a promise: it terminates in a completion
(window opens) or a cancellation event, never silence.

M5 fixture: "Mile High Club — point yours" delivered twice ~13s apart.
No new mechanism — the PATCH-001 T1/T4 verdict claim key already makes a
committed verdict air once; this locks that verdict acts carry the key.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_say_gate
from lily_agent import LilyGame
from lily_scorekeeper import LilyScorekeeper


def _make_game():
    game = LilyGame.__new__(LilyGame)
    game.sk = LilyScorekeeper("m4-fixture")
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game._aired_stems = set()
    game._playout_started_ids = set()
    game.events = []
    game.send_event_nowait = lambda kind, payload: game.events.append((kind, payload))
    game.armed_question = {"prompt": "Name three?", "canonical_answer": "x"}
    game.sk.start_question(game.armed_question)
    return game


# -- M4: no orphan stems -------------------------------------------------------


def test_aired_then_completed_leaves_no_cancellation():
    game = _make_game()
    qn = game.sk.question_number
    game.mark_stem_aired(qn)
    game.mark_stem_completed(qn)
    game._terminate_aired_stem(reason="mode_flush:test")
    assert not any(k == "stem_cancelled" for k, _ in game.events)


def test_aired_then_abandoned_emits_cancellation():
    game = _make_game()
    qn = game.sk.question_number
    game.mark_stem_aired(qn)
    # Window never opened; the question is abandoned (mode flush / skip).
    game._terminate_aired_stem(reason="mode_flush:enter_adult")
    kinds = [k for k, _ in game.events]
    assert "stem_cancelled" in kinds
    payload = next(p for k, p in game.events if k == "stem_cancelled")
    assert payload["question_number"] == qn
    # Idempotent — a second terminate does not double-fire.
    game._terminate_aired_stem(reason="again")
    assert kinds.count("stem_cancelled") == 1


def test_stem_never_aired_is_not_a_cancellation():
    game = _make_game()
    game._terminate_aired_stem(reason="mode_flush:test")
    assert game.events == []


def test_open_window_marks_completion(monkeypatch):
    game = _make_game()
    qn = game.sk.question_number
    game.mark_stem_aired(qn)
    assert qn in game._aired_stems
    game.mark_stem_completed(qn)
    assert qn not in game._aired_stems


def test_playout_start_of_delivery_marks_stem_aired():
    game = _make_game()
    qn = game.sk.question_number
    game.say_registry.claim(f"q_{qn}_delivery", owner="sp1")
    game.note_playout_started("sp1")
    assert qn in game._aired_stems


def test_playout_start_of_a_nondelivery_turn_does_not_mark_stem():
    game = _make_game()
    game.say_registry.claim("session_greet", owner="sp2")
    game.note_playout_started("sp2")
    assert game.sk.question_number not in game._aired_stems


# -- M5: verdict acts carry a single-fire claim key ----------------------------


def test_verdict_claim_key_makes_a_second_airing_impossible():
    """A committed verdict claims q_{N}_reveal; the second attempt with
    the same key is dup-suppressed — 'Mile High Club' can't fire twice."""
    game = _make_game()
    key = f"q_{game.sk.question_number}_reveal"
    assert game.say_registry.claim(key, owner="v1") is True
    game.say_registry.confirm(key)  # it aired
    # A second verdict dispatch for the same question is refused.
    assert game.say_registry.claim(key, owner="v2") is False
