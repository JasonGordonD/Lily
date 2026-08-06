"""WO-LILY-PATCH-001 T1–T3 — the retry re-air class, from the Aug 6
evening fixtures (sessions 89A97A / 48630B / 05AAC9 / 105865).

Live evidence: "Nobody…" ×3, "Hey…" ×3, "Saturn is correct" ×3, the
doubled 18+ prompt (T1 — the never-aired watchdog false-firing on turns
that PLAYED); Mitochondria re-aired 2s after the correct answer, Saturn
re-read 4s after the answer, Kama Sutra re-read post-answer (T2 — an
answered question re-airing); greet/"my bad"/Miranda duplicate pairs
with user rows interleaved (T3 — the HOTFIX-002 last-turn-only guard
missing every real dup).

All of this is RETIRE_WITH_WS6 scaffolding — the journal reducer's
leases replace it structurally.
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_say_gate
from lily_agent import UNDELIVERED_MAX_REFIRES, LilyGame
from lily_scorekeeper import LilyScorekeeper


class _Handle:
    def __init__(self, speech_id):
        self.id = speech_id
        self.interrupts = []

    def interrupt(self, *, force=False):
        self.interrupts.append(force)
        return self


def _make_game() -> LilyGame:
    game = LilyGame.__new__(LilyGame)
    game.sk = LilyScorekeeper("patch001-fixture")
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game.game_started = True
    game.game_over = False
    game._adjudicating = False
    game._playout_started_ids = set()
    game._suppressed_speech_ids = set()
    game._speech_handles = {}
    game._answered_questions = set()
    game._undelivered_ticks = 99  # past the reconcile threshold
    game._undelivered_refires = 0
    game.armed_question = {"prompt": "Which planet?", "canonical_answer": "Saturn"}
    game.sk.start_question(game.armed_question)
    game._pending_delivery_qnum = None
    game.instructed_replies = []
    game.instructed_reply = lambda text: game.instructed_replies.append(text)
    return game


def _claim_delivery(game, speech_id="speech_d1"):
    key = f"q_{game.sk.question_number}_delivery"
    game.say_registry.claim(key, owner=speech_id)
    handle = _Handle(speech_id)
    game.note_speech_handle(handle)
    return key, handle


# -- T1: no refire against a turn that actually played -------------------------


def test_no_refire_while_the_delivery_is_airing():
    """The false-refire fixture: claim PENDING because playout hasn't
    COMPLETED — but it started. The watchdog must not re-air."""
    game = _make_game()
    key, handle = _claim_delivery(game)
    game._playout_started_ids.add("speech_d1")
    assert game.reconcile_undelivered_claim() == "idle"
    assert game.instructed_replies == []
    assert game.say_registry.state(key) == lily_say_gate.CLAIM_PENDING
    assert handle.interrupts == []


def test_no_refire_while_agent_audio_is_live():
    game = _make_game()
    _claim_delivery(game)
    game.sk.host_speaking = True
    assert game.reconcile_undelivered_claim() == "idle"
    assert game.instructed_replies == []


def test_refire_cancels_the_original_handle():
    """The starts-after-release hole: a genuine refire must invalidate
    the original speech so a late start cannot double-air."""
    game = _make_game()
    key, handle = _claim_delivery(game)
    assert game.reconcile_undelivered_claim() == "refired"
    assert len(game.instructed_replies) == 1
    assert handle.interrupts == [True]  # force-interrupted
    assert "speech_d1" in game._suppressed_speech_ids


# -- T2: an answered question never re-airs ------------------------------------


def test_answered_question_blocks_refire_and_releases_the_claim():
    """Saturn fixture: answer candidates exist — the outstanding delivery
    attempt is invalidated, never re-aired."""
    game = _make_game()
    key, handle = _claim_delivery(game)
    game.sk.answer_candidates["S1"] = {"text": "Saturn", "speaker_label": "S1"}
    assert game.reconcile_undelivered_claim() == "idle"
    assert game.instructed_replies == []
    assert game.say_registry.state(key) is None  # claim invalidated


def test_note_answer_heard_cancels_in_flight_delivery():
    """Mitochondria fixture: adjudication starting (answer_heard) cancels
    a mid-playout delivery of the same question."""
    game = _make_game()
    key, handle = _claim_delivery(game)
    game.note_answer_heard(game.sk.question_number)
    assert game.say_registry.state(key) is None
    assert handle.interrupts == [True]
    assert game.question_already_answered(game.sk.question_number)


def test_answered_survives_the_question_transition():
    game = _make_game()
    qnum = game.sk.question_number
    game.note_answer_heard(qnum)
    # The game moves on; a stale refire for the OLD number is still caught.
    assert game.question_already_answered(qnum) is True


def test_dispatch_armed_question_refuses_an_answered_question():
    game = _make_game()
    game.game_over = False
    game.note_answer_heard(game.sk.question_number)
    assert game.dispatch_armed_question(source="test") is False
    assert game.instructed_replies == []


# -- T3: widened dup guard (record + air) --------------------------------------


class _FakeBatcher:
    def __init__(self):
        self.rows = []

    def add(self, text, **kw):
        self.rows.append(text)


def test_record_dedupe_catches_interleaved_duplicates(caplog):
    """Every live duplicate pair had a user row between the copies —
    last-turn-only matching missed them all."""
    game = _make_game()
    game.transcripts = _FakeBatcher()
    game.record_agent_turn("Hi! I'm Lily, and I host trivia. Welcome!",
                           act_keys=[], interrupted=False)
    game.record_agent_turn("Great to meet you, Rami! How's your evening?",
                           act_keys=[], interrupted=False)
    with caplog.at_level(logging.WARNING):
        game.record_agent_turn("Hi! I'm Lily, and I host trivia. Welcome!",
                               act_keys=[], interrupted=False)
    assert len(game.transcripts.rows) == 2  # the interleaved dup skipped
    assert any("DUP_TURN_SKIPPED" in r.message and "record" in r.message
               for r in caplog.records)


def test_short_turns_may_legitimately_repeat():
    game = _make_game()
    game.transcripts = _FakeBatcher()
    game.record_agent_turn("Nice one!", act_keys=[], interrupted=False)
    game.record_agent_turn("Here's your next question, team.",
                           act_keys=[], interrupted=False)
    game.record_agent_turn("Nice one!", act_keys=[], interrupted=False)
    assert len(game.transcripts.rows) == 3


def test_air_dup_guard_matches_recent_played_turns_only():
    game = _make_game()
    game.sk.agent_turns.extend([
        "Hi! I'm Lily, and I host trivia. Welcome!",
        "Great to meet you, Rami!",
    ])
    # Interleaved verbatim repeat of a played turn: suppress.
    assert game.air_dup_guard(
        "Hi! I'm Lily, and I host trivia. Welcome!", None
    ) is True
    # Fresh text: airs.
    assert game.air_dup_guard("Round two, question one!", None) is False
    # Delivery turns are exempt — sheet re-reads are deliberate.
    assert game.air_dup_guard(
        "Hi! I'm Lily, and I host trivia. Welcome!", "claimed_structural"
    ) is False
    # Short repeats are exempt.
    game.sk.agent_turns.append("Spot on!")
    assert game.air_dup_guard("Spot on!", None) is False
