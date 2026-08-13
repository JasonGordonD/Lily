"""Depth-2 supply — the reserve slot (W2b item 3a).

The prefetch keeps a queue of TWO: the head (`next_question`) plus a reserve
(`_next_question_reserve`, the N+2), so consuming the head never leaves an
empty hand. INVARIANT: the reserve is non-None only while the head is non-None.

The load-bearing safety is `_promote_reserve` — the CENTRALISED Class-6 guard:
a reserve drawn under a deck that has since flipped (or one already burned)
must be DISCARDED at promotion, never served cross-deck. And the two burn
paths (adult objection, answer leak) retire the reserve with everything else
in flight.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lily_agent import LilyGame


def _reserve_game() -> LilyGame:
    g = LilyGame.__new__(LilyGame)
    g.sk = SimpleNamespace(
        mode="general", media_mode="voice_only", session_id="reserve"
    )
    g.next_question = None
    g._next_question_reserve = None
    g._next_question_reserve_mode = None
    g._is_burned = lambda q: False
    g._register_custom_question = lambda category, question: None
    g.settle_context_nowait = lambda: None
    return g


# -- promotion (the Class-6 guard) ----------------------------------------


def test_promote_reserve_moves_reserve_to_head():
    g = _reserve_game()
    g._next_question_reserve = {"id": "q9", "category": "space"}
    g._next_question_reserve_mode = "general"
    g._promote_reserve()
    assert g.next_question == {"id": "q9", "category": "space"}
    assert g._next_question_reserve is None
    assert g._next_question_reserve_mode is None


def test_promote_reserve_discards_a_cross_deck_reserve():
    # THE cross-deck test: the reserve was drawn under the general deck, but
    # the deck has flipped to adult while it sat. It must be DISCARDED, never
    # promoted — a general question can never surface in an adult round.
    g = _reserve_game()
    g.sk.mode = "adult"                       # deck flipped after the draw
    g._next_question_reserve = {"id": "q9", "category": "space"}
    g._next_question_reserve_mode = "general"
    g._promote_reserve()
    assert g.next_question is None            # discarded, head left empty
    assert g._next_question_reserve is None


def test_promote_reserve_discards_a_burned_reserve():
    g = _reserve_game()
    g._is_burned = lambda q: True             # answer aired while it sat
    g._next_question_reserve = {"id": "q9"}
    g._next_question_reserve_mode = "general"
    g._promote_reserve()
    assert g.next_question is None
    assert g._next_question_reserve is None


def test_promote_reserve_is_a_noop_without_a_reserve():
    g = _reserve_game()
    g._promote_reserve()
    assert g.next_question is None
    assert g._next_question_reserve is None


def test_promote_reserve_strips_image_in_voice_only():
    # Mirror the prefetch commit's picture exclusion at promotion time.
    g = _reserve_game()
    g.sk.media_mode = "voice_only"
    g._next_question_reserve = {
        "id": "q9", "image_url": "http://x/y.png", "image_source": "exa"
    }
    g._next_question_reserve_mode = "general"
    g._promote_reserve()
    assert "image_url" not in g.next_question
    assert g.next_question["image_source"] == "none"


def test_promote_reserve_registers_the_promoted_head():
    # Registration is skipped at reserve-fill and runs HERE, once, as the head.
    g = _reserve_game()
    registered = []
    g._register_custom_question = lambda category, question: registered.append(
        (category, question.get("id"))
    )
    g._next_question_reserve = {"id": "q9", "category": "cape cod"}
    g._next_question_reserve_mode = "general"
    g._promote_reserve()
    assert registered == [("cape cod", "q9")]


# -- burn paths retire the reserve ----------------------------------------


def test_adult_objection_burns_the_reserve():
    g = _reserve_game()
    g.armed_question = None
    g.next_question = None
    g._next_question_reserve = {"id": "q9"}
    g._next_question_reserve_mode = "adult"
    burned = []
    g._burn_question = lambda q, reason: burned.append((q.get("id"), reason))
    g.publish_attributes_nowait = lambda: None
    assert g._burn_pending_adult_questions(reason="age_unconfirmed") is True
    assert ("q9", "age_unconfirmed") in burned
    assert g._next_question_reserve is None
    assert g._next_question_reserve_mode is None
