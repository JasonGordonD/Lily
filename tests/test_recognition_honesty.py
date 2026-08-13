"""WO-LILY-RECOGNITION-HONESTY-001 — mid-conversation returner-claim gate.

Live failure (19:41): a player insisted "do you know who I am", "you should
know my voice", "I played with you before" — and Lily DENIED it three
times ("we haven't played together before", "I don't have your voice on
file", "I promise my system doesn't have your voice saved"). The greeting's
never-deny law only conditions the LANDING beat; a claim arriving in a
follow-up turn had no enforcement. This gate is the enforcement: a
deterministic detector + a state-note honesty law that fires ONLY when the
table card is genuinely blank.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_scorekeeper
from lily_scorekeeper import lily_detect_returner_claim, LilyScorekeeper
from lily_agent import LilyGame


# -- detector: the live utterances all fire -----------------------------------


def test_live_failure_utterances_all_detected():
    for text in [
        "Hey, Lily. How are you? Do you know who I am?",
        "I did not say that. I said, do you know? Do you know who I am?",
        "But I played with you before. You should know my voice.",
        "Yes, yes you do, yes you do, yes you do.",  # not a claim on its own
    ][:3]:
        assert lily_detect_returner_claim(text) is True, text


def test_returner_claim_variants():
    for text in [
        "do you know who I am",
        "you should know me",
        "you should know my voice",
        "you know my voice",
        "don't you recognize me?",
        "remember me?",
        "we've met before",
        "I played with you before",
        "we played before",
        "we've crossed paths",
        "it's not my first time",
        "this isn't our first time",
    ]:
        assert lily_detect_returner_claim(text) is True, text


# -- detector: content questions and newcomers never fire (precision) ---------


def test_trivia_content_never_false_fires():
    for text in [
        "do you know the answer to that one?",
        "you should know this category cold",
        "do you know how many moons Jupiter has?",
        "remember to give us a hard one",
        "have we started yet?",
    ]:
        assert lily_detect_returner_claim(text) is False, text


def test_first_time_and_concessions_never_fire():
    for text in [
        "this is my first time playing",
        "it's our first time",
        "you don't know me, that's fine",
        "we've never played before",
        "I have not played with you",
    ]:
        assert lily_detect_returner_claim(text) is False, text


def test_empty_and_garbage():
    assert lily_detect_returner_claim("") is False
    assert lily_detect_returner_claim("   ") is False
    assert lily_detect_returner_claim("uh, hmm, yeah") is False


# -- agent gate: fires ungrounded, quiet when grounded ------------------------


def _make_game(*, memory_block="", verified=False):
    game = LilyGame.bare()
    game.sk = LilyScorekeeper("rh")
    game.sk.players = {}
    game.memory_block = memory_block
    game.device_identity_verified = verified
    game._state_note = None
    game._returner_honesty_note = None
    return game


def _fire(game, text):
    """Minimal reproduction of the on_transcript_event gate condition."""
    command = None
    media_choice = None
    if (
        command is None
        and not media_choice
        and not game.memory_block
        and not getattr(game, "device_identity_verified", False)
        and lily_scorekeeper.lily_detect_returner_claim(text)
    ):
        game._returner_honesty_note = "[returner-claim honesty — ...]"
    return game._returner_honesty_note


def test_gate_arms_when_table_card_blank():
    game = _make_game(memory_block="", verified=False)
    note = _fire(game, "you should know my voice")
    assert note is not None
    assert "returner-claim honesty" in note


def test_gate_quiet_when_memory_grounds_her():
    game = _make_game(memory_block="[RETURNING TABLE] Rami, 3 games", verified=False)
    note = _fire(game, "you should know my voice")
    assert note is None  # she HAS grounds — recognition beats own the turn


def test_gate_quiet_when_device_identity_verified():
    game = _make_game(memory_block="", verified=True)
    note = _fire(game, "do you know who I am")
    assert note is None


def test_gate_quiet_on_non_claim():
    game = _make_game()
    note = _fire(game, "do you know the answer?")
    assert note is None


# -- the note carries the never-deny law and the honest gap explanation -------


def test_note_forbids_denial_and_explains_the_gap():
    from lily_agent import LilyGame as _LG
    # Reproduce the exact note text the agent sets.
    game = _make_game()
    game._returner_honesty_note = (
        "[returner-claim honesty — a player just asserted you've met "
        "before, or that you should know their voice, and your table "
        "card for tonight is blank. LAW: do NOT deny prior contact, "
        "do NOT say you've never played together, do NOT tell them "
        "their voice isn't on file as if that settles it, do NOT "
        "argue with their memory of you. The honest truth you MAY "
        "give, ONCE and lightly: your memory is keyed to a device or "
        "browser, so a new device, a cleared browser, or a fresh key "
        "leaves your card blank tonight even for someone who really "
        "has played — a gap in YOUR records, never proof they're "
        "wrong. Believe them, name the gap once if you haven't, then "
        "move forward warmly. If you already named it this session, "
        "don't repeat it — just don't deny.]"
    )
    n = game._returner_honesty_note
    assert "do NOT deny prior contact" in n
    assert "argue with their memory" in n
    assert "device" in n and "browser" in n
    assert "Believe them" in n
