"""P0 false clean-slate / recognition dispute — Rami session script.

Live failure class (trust-killer):
  returner: "It is absolutely not [first time]"
  Lily:     "clean slate… no saved stats or facts on file yet"  ← FALSE
  later:    "NOW I've got you: reigning champion, four wins!"
  he asks why → sycophancy + "Let's kick" instead of one grounded why

Deterministic UNKNOWN must never be spoken as EMPTY. Kickoff locks until
the why-beat lands.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_config
import lily_say_gate
import lily_scorekeeper
from lily_agent import LilyAgent, LilyGame
from lily_scorekeeper import LilyScorekeeper


def _game(*, resolved=False, memory_block="", supabase=object()):
    game = LilyGame.bare()
    game.sk = LilyScorekeeper("rami-false-slate")
    game.memory_block = memory_block
    game.memory_total_games = 0
    game.memory_player_names = []
    game.device_candidate_group_id = None
    game.device_identity_verified = False
    game._voice_identity_resolved = resolved
    game.supabase = supabase
    game.game_started = False
    game.game_over = False
    game.session_started_at = 0.0
    game.next_question = {"id": "q1", "prompt": "x"}
    game.armed_question = None
    game._returner_honesty_note = None
    game._recognition_dispute = False
    game._recognition_dispute_why_answered = False
    game._recognition_why_note = None
    game._state_note = None
    game._last_bind_at = None
    game._drawn_ids = set()
    game._phase_hold = None
    game.ui_phase = "lobby"
    game.promoted_categories = []
    game.acoustic = None
    game._pending_unbound_award = None
    game._late_answer_note = None
    game._explain_request_note = None
    game._contest_note = None
    return game


# -- detectors -----------------------------------------------------------------


def test_rami_clean_slate_phrases_are_forbidden_claims():
    for text in [
        "clean slate — I don't have any saved stats or facts on file yet",
        "my memory bank is sitting on a completely clean slate for you all",
        "tonight is actually a clean slate",
        "nothing on file yet",
        "no saved stats",
        "no saved voices",
        "no past games on file",
        "no prior games",
        "nothing is saved",
        (
            "It looks like we're starting with a completely clean slate "
            "tonight—no saved voices or past games on file for us yet."
        ),
    ]:
        assert lily_say_gate.lily_false_clean_slate_claim(text), text


def test_recognition_dispute_phrases():
    for text in [
        "Why did you say clean slate?",
        "Why didn't you say you're still pulling?",
        "How come you said blank?",
        "you said nothing on file",
    ]:
        assert lily_scorekeeper.lily_detect_recognition_dispute(text), text


# -- P0-A ---------------------------------------------------------------------


def test_cannot_claim_empty_while_probe_outstanding(monkeypatch):
    monkeypatch.setattr(lily_config, "voice_identity_enabled", lambda: True)
    g = _game(resolved=False)
    assert g.identity_probe_outstanding() is True
    assert g.can_claim_empty_memory() is False


def test_can_claim_empty_only_when_resolved_empty(monkeypatch):
    monkeypatch.setattr(lily_config, "voice_identity_enabled", lambda: True)
    g = _game(resolved=True)
    assert g.can_claim_empty_memory() is True
    g.memory_block = "[RETURNING TABLE]"
    assert g.can_claim_empty_memory() is False


def test_state_block_says_still_checking_while_probe_out(monkeypatch):
    monkeypatch.setattr(lily_config, "voice_identity_enabled", lambda: True)
    g = _game(resolved=False)
    g.sk.build_state_block = lambda: "STATE"
    g.acoustic = None
    block = g.build_state_block()
    assert "identity: STILL CHECKING" in block
    assert "do not claim empty memory" in block


def test_tts_rewrites_false_clean_slate_while_probe_out(monkeypatch):
    monkeypatch.setattr(lily_config, "voice_identity_enabled", lambda: True)
    game = _game(resolved=False)
    agent = LilyAgent.__new__(LilyAgent)
    agent._game = game
    # Exercise the rewrite predicate the way tts_node does.
    full = "It's a clean slate — no saved stats or facts on file yet."
    assert lily_say_gate.lily_false_clean_slate_claim(full)
    assert not game.can_claim_empty_memory()
    rewritten = lily_say_gate.lily_still_checking_rewrite()
    assert "still checking" in rewritten.lower()
    assert "clean slate" not in rewritten.lower()


# -- P0-B / P0-C --------------------------------------------------------------


def test_returner_claim_arms_dispute_while_probe_out(monkeypatch):
    monkeypatch.setattr(lily_config, "voice_identity_enabled", lambda: True)
    g = _game(resolved=False)
    assert lily_scorekeeper.lily_detect_returner_claim(
        "It is absolutely not my first time"
    )
    g.arm_recognition_dispute(reason="returner_claim")
    assert g.recognition_dispute_blocks_start() is True
    assert g._recognition_why_note is not None
    assert "Answer WHY" in g._recognition_why_note


def test_be8d8b_returner_phrase_is_detected():
    assert lily_scorekeeper.lily_detect_returner_claim(
        "I certainly have been on your table before. Lily."
    )


def test_returner_claim_persistently_forbids_empty_even_after_empty_resolution(
    monkeypatch,
):
    """BE8D8B: one-shot note may clear; explicit returner truth may not."""
    monkeypatch.setattr(lily_config, "voice_identity_enabled", lambda: True)
    g = _game(resolved=True)
    g._returner_claim_seen = True
    g._returner_honesty_note = None
    g._recognition_dispute = False
    attempted = (
        "Ah, welcome back! It looks like we're starting with a completely "
        "clean slate tonight—no saved voices or past games on file for us yet."
    )
    assert g.can_claim_empty_memory() is False
    assert g.must_rewrite_false_empty_claim(attempted) is True
    aired = lily_say_gate.lily_still_checking_rewrite()
    assert "still checking" in aired.lower()
    assert "clean slate" not in aired.lower()
    assert "no saved voices" not in aired.lower()


def test_start_game_blocked_during_dispute():
    g = _game(resolved=True)
    g.arm_recognition_dispute(reason="clean_slate_challenge")
    assert g.recognition_dispute_blocks_start() is True

    async def _run():
        await g.start_game(source="voice")
        return g.game_started

    assert asyncio.run(_run()) is False


def test_why_answered_unlocks_start():
    g = _game(resolved=True)
    g.arm_recognition_dispute(reason="clean_slate_challenge")
    g._recognition_dispute_why_answered = True
    assert g.recognition_dispute_blocks_start() is False


def test_begin_round_tool_refuses_during_dispute():
    g = _game(resolved=True)
    g.arm_recognition_dispute(reason="clean_slate_challenge")
    # Minimal surfaces start_game / intake need.
    g.intake_roundrobin_active = lambda: False
    agent = LilyAgent.__new__(LilyAgent)
    agent._game = g
    msg = asyncio.run(
        LilyAgent.lily_begin_round.__wrapped__(agent, None)
    )
    assert "recognition" in msg.lower() or "why" in msg.lower()
    assert g.game_started is False


def test_rami_script_end_to_end_forbidden_air(monkeypatch):
    """Transcript → forbidden phrases cannot air while probe outstanding."""
    monkeypatch.setattr(lily_config, "voice_identity_enabled", lambda: True)
    g = _game(resolved=False)
    # 20:51:13 returner claim
    assert lily_scorekeeper.lily_detect_returner_claim(
        "It is absolutely not my first time"
    )
    g.arm_recognition_dispute(reason="returner_claim")
    # 20:51:19 model tries clean slate — must be rewritten
    attempted = (
        "Alright — clean slate, no saved stats or facts on file yet."
    )
    assert lily_say_gate.lily_false_clean_slate_claim(attempted)
    assert not g.can_claim_empty_memory()
    aired = lily_say_gate.lily_still_checking_rewrite()
    assert "clean slate" not in aired.lower()
    assert "still checking" in aired.lower()
    # Kickoff blocked through the dispute
    assert g.recognition_dispute_blocks_start() is True
    # 20:52:14 why challenge keeps dispute open
    assert lily_scorekeeper.lily_detect_recognition_dispute(
        "Why didn't you say you're still pulling?"
    )
