"""WO-LILY-OMNIBUS-003 WS-6 — supply-stall visibility + curated-bank fallback.

Evidence session `lily-81BCB0-583a0f16`: five minutes of generation
starvation filled with vamping — the supply line returned nothing tick
after tick, so nothing armed, no fallback fired, and the screen carried no
cue. WS-2 heals a delivery that armed but never aired; WS-6 heals the class
UPSTREAM of it — generation itself starving.

Contract pinned here:
  1. `next_question_ready()` (published as the `next_question_ready` seam
     attribute) is True when a deliverable question is in hand — armed or
     prefetched — or a ruling is in flight, and False ONLY in the live
     supply-stall state (game running, window closed, neither armed nor
     prefetched). Pre-game and post-game report ready.
  2. `arm_supply_fallback()` draws from the EXISTING curated bank
     (`lily_persistence.lily_fetch_bank_question` over `lily_questions` —
     the same source LILY_KB_ONLY and the generation-failed insurance path
     use), registers the draw, arms it, and dispatches exactly one
     structural delivery. It arms ONLY when `no_stuck_claims()` holds
     (reconciliation-first): a stuck claim reports "blocked".
  3. A starved supply line arms a bank question within the fallback window;
     the seam flag toggles False → True across the recovery.

Same import boundary as test_undelivered_reconcile.py (pulls in livekit
via lily_agent).
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_audeering_consumers
import lily_config
import lily_persistence
import lily_say_gate
from lily_agent import LilyGame
from lily_scorekeeper import LilyScorekeeper

SESSION_ID = "lily-81BCB0-583a0f16"

BANK_Q = {
    "id": "kb_777",
    "prompt": "What is the tallest mountain on Earth above sea level?",
    "canonical_answer": "Mount Everest",
    "acceptable_answers": ["mount everest", "everest"],
    "category": "academic",
    "difficulty_tier": 1,
    "reveal_color": "",
}


class _FakeSession:
    def __init__(self) -> None:
        self.instructions: list[str] = []

    def generate_reply(self, instructions: str) -> None:
        self.instructions.append(instructions)


def _make_game(game_started: bool = True) -> LilyGame:
    game = LilyGame.bare()
    game.session = _FakeSession()
    game.say_registry = lily_say_gate.SpeechActRegistry()
    game.sk = LilyScorekeeper(SESSION_ID)
    game.game_started = game_started
    game.game_over = False
    game.armed_question = None
    game.next_question = None
    game.asked_history = []
    game.used_prompts = []
    game.supabase = object()  # non-None sentinel; the bank fetch is patched
    game.reasoning = None
    game.group_id = "grp_test"
    game.rounds_total = 3
    game.prewager_standings = None
    game.eliminated = []
    game.ui_phase = "question"
    game._phase_hold = None
    game._adjudicating = False
    game._question_transitioning = False
    game._armed_speech_misses = 0
    game._pending_delivery_qnum = None
    game._undelivered_ticks = 0
    game._undelivered_refires = 0
    game._supply_stall_ticks = 0
    game._prefetch_stall_ticks = 0
    game._armed_limbo_ticks = 0
    game._drawn_ids = set()
    game._drawn_hashes = set()
    game._judged_keys = set()
    game._spec_judge = {}
    game._nbest_by_key = {}
    game._addressee_rows = {}
    game._pre_window_segments = []
    game._prefetch_task = None
    game.acoustic = lily_audeering_consumers.LilyAcousticState()
    game.session_started_at = time.time() - 300.0
    # gated_say -> instructed_reply: capture instead of speaking.
    game.instructed_replies: list[str] = []
    game.instructed_reply = lambda text: game.instructed_replies.append(text)
    # UI/network plumbing is not part of the WS-6 claim contract: keep the
    # arm real, but no-op the publish/prefetch side effects.
    game._set_ui_phase = lambda phase: None
    game.start_prefetch = lambda: None
    game.publish_attributes_nowait = lambda: None
    return game


def _arm_stuck(game: LilyGame) -> None:
    """Put the game in a WS-2 stuck-delivery state: armed, registered,
    delivery claimed PENDING, and re-fired at least once with the delivery
    still not aired. Per the hardened WS-2 contract no_stuck_claims() keys
    on `_undelivered_refires > 0` (the signal that persists across ticks,
    not the self-resetting tick counter), so it is then False for the whole
    active re-fire cycle."""
    game.armed_question = dict(BANK_Q)
    game.sk.start_question(game.armed_question)
    game.asked_history.append({
        "question_id": BANK_Q["id"],
        "question_text_hash": "hash_stuck",
        "canonical_answer": BANK_Q["canonical_answer"],
    })
    game.say_registry.claim(f"q_{game.sk.question_number}_delivery")
    game._undelivered_refires = 1  # re-fired once, still unaired -> stuck


def _patch_bank(monkeypatch, result):
    calls: list[dict] = []

    async def _fake_fetch(supabase, category, difficulty_tier,
                          exclude_prompts, mode="general",
                          exclude_ids=None, exclude_hashes=None,
                          exclude_answers=None, strict_category=False):
        # strict_category (HOTFIX-006 N2): the fallback draws strictly inside
        # a round the table NAMED, so an operator topic can never be filled
        # with a stranger's question. These fixtures all run the fixed
        # rotation, where it is False and the behaviour is unchanged.
        calls.append({
            "category": category, "tier": difficulty_tier, "mode": mode,
            "exclude_ids": exclude_ids, "exclude_hashes": exclude_hashes,
            "exclude_answers": exclude_answers,
            "strict_category": strict_category,
        })
        return dict(result) if isinstance(result, dict) else result

    monkeypatch.setattr(
        lily_persistence, "lily_fetch_bank_question", _fake_fetch
    )
    return calls


def _run(coro):
    return asyncio.run(coro)


# -- seam flag: next_question_ready() ------------------------------------------


def test_seam_ready_true_when_armed():
    game = _make_game()
    game.armed_question = dict(BANK_Q)
    assert game.next_question_ready() is True


def test_seam_ready_true_when_prefetched():
    game = _make_game()
    game.next_question = dict(BANK_Q)
    assert game.next_question_ready() is True


def test_seam_ready_false_during_supply_stall():
    # Game live, window closed, no ruling, nothing armed OR prefetched:
    # the starvation the 583a0f16 session sat in with no screen cue.
    game = _make_game()
    assert game.armed_question is None and game.next_question is None
    assert game.next_question_ready() is False


def test_seam_ready_true_pre_and_post_game():
    pre = _make_game(game_started=False)
    assert pre.next_question_ready() is True
    post = _make_game()
    post.game_over = True
    assert post.next_question_ready() is True


def test_seam_ready_true_while_adjudicating():
    game = _make_game()
    game._adjudicating = True
    assert game.next_question_ready() is True


# -- curated-bank fallback -----------------------------------------------------


def test_fallback_arms_from_bank_with_one_active_claim(monkeypatch):
    game = _make_game()
    _patch_bank(monkeypatch, BANK_Q)
    assert game.next_question_ready() is False  # starved before

    assert _run(game.arm_supply_fallback()) == "armed"

    # A bank question is armed and consumed off the prefetch slot.
    assert game.armed_question is not None
    assert game.armed_question.get("id") == "kb_777"
    assert game.next_question is None
    # Exactly ONE active delivery claim/intent for the new question, and
    # exactly one nudge dispatched.
    assert game._pending_delivery_qnum == game.sk.question_number
    assert len(game.instructed_replies) == 1
    # Registered in the session's no-repeat mirrors.
    assert "kb_777" in game._drawn_ids
    assert any(
        r["question_id"] == "kb_777" for r in game.asked_history
    )
    # Seam flag flips ready once the fallback arms.
    assert game.next_question_ready() is True


def test_fallback_draws_from_existing_curated_bank(monkeypatch):
    # Proves the fallback source is the EXISTING lily_questions bank (the
    # WS-6 constraint: draw from an existing KB source, do not invent one),
    # and that the group/session no-repeat exclusion is honored.
    game = _make_game()
    game.asked_history.append({
        "question_id": "kb_1",
        "question_text_hash": "prior_hash",
        "canonical_answer": "prior",
    })
    calls = _patch_bank(monkeypatch, BANK_Q)
    assert _run(game.arm_supply_fallback()) == "armed"
    assert len(calls) == 1
    # The draw excluded the already-served row.
    assert "kb_1" in calls[0]["exclude_ids"]
    assert calls[0]["mode"] == "general"


def test_fallback_blocked_by_stuck_claim(monkeypatch):
    # Reconciliation-first: a stuck registered-undelivered claim (WS-2)
    # must be re-fired/released before a fallback arms — never queued
    # behind the ghost.
    game = _make_game()
    _arm_stuck(game)
    assert game.no_stuck_claims() is False
    calls = _patch_bank(monkeypatch, BANK_Q)

    assert _run(game.arm_supply_fallback()) == "blocked"
    # Nothing new armed; the bank was never touched.
    assert game.armed_question.get("id") == "kb_777"  # the stuck one, intact
    assert game.next_question is None
    assert calls == []
    assert game.instructed_replies == []


def test_fallback_holds_off_stuck_then_arms_on_clear(monkeypatch):
    # Full reconciliation-first cycle, meaningful ONLY on the hardened WS-2
    # predicate (no_stuck_claims keys on _undelivered_refires, which holds
    # False for the whole re-fire cycle — on the pre-hardening predicate the
    # False window had zero observable duration and this guard was toothless):
    #   stuck (predicate False)  -> fallback HOLDS OFF
    #   claim releases (True)    -> fallback then ARMS
    game = _make_game()
    _arm_stuck(game)
    assert game.no_stuck_claims() is False
    calls = _patch_bank(monkeypatch, BANK_Q)

    # Phase 1 — genuinely stuck: the fallback must NOT arm over the ghost.
    assert _run(game.arm_supply_fallback()) == "blocked"
    assert calls == []
    assert game.instructed_replies == []
    assert game.armed_question.get("id") == "kb_777"  # the stuck one, intact

    # Phase 2 — WS-2 releases the ghost (armed dropped, re-fire signal
    # cleared): the real release helper produces exactly this state.
    game._release_armed_question_to_supply()
    assert game.no_stuck_claims() is True
    assert game.armed_question is None

    # Phase 3 — predicate clear: the fallback now arms a fresh bank question.
    assert _run(game.arm_supply_fallback()) == "armed"
    assert game.armed_question is not None
    assert len(calls) == 1
    assert game._pending_delivery_qnum == game.sk.question_number
    assert len(game.instructed_replies) == 1


def test_fallback_empty_when_bank_exhausted(monkeypatch):
    game = _make_game()
    _patch_bank(monkeypatch, None)
    assert _run(game.arm_supply_fallback()) == "empty"
    assert game.armed_question is None
    # Still starved — the honest vamp holds, seam stays not-ready.
    assert game.next_question_ready() is False


def test_fallback_empty_when_no_supabase(monkeypatch):
    game = _make_game()
    game.supabase = None
    calls = _patch_bank(monkeypatch, BANK_Q)
    assert _run(game.arm_supply_fallback()) == "empty"
    assert calls == []
    assert game.armed_question is None


def test_fallback_idle_when_question_already_in_hand(monkeypatch):
    game = _make_game()
    game.next_question = dict(BANK_Q)  # a prefetched question just landed
    calls = _patch_bank(monkeypatch, BANK_Q)
    assert _run(game.arm_supply_fallback()) == "idle"
    assert calls == []  # never drew — not actually starved


def test_fallback_idle_pre_game(monkeypatch):
    game = _make_game(game_started=False)
    calls = _patch_bank(monkeypatch, BANK_Q)
    assert _run(game.arm_supply_fallback()) == "idle"
    assert calls == []


def test_fallback_no_duplicate_draw(monkeypatch):
    # A bank row already drawn this session is discarded (G2 idempotency),
    # not served twice.
    game = _make_game()
    game._register_draw(dict(BANK_Q))  # already drawn
    _patch_bank(monkeypatch, BANK_Q)
    assert _run(game.arm_supply_fallback()) == "empty"
    assert game.armed_question is None


# -- fallback window -----------------------------------------------------------


def test_fallback_window_ticks_default():
    game = _make_game()
    # Default 30s at the 10s watchdog interval = 3 ticks (~30s), inside a
    # minute of stall.
    assert game._supply_fallback_ticks() == 3
    assert (
        game._supply_fallback_ticks() * game.WATCHDOG_INTERVAL_SECONDS
        <= 60.0
    )


def test_fallback_window_honors_config(monkeypatch):
    monkeypatch.setattr(
        lily_config, "supply_fallback_seconds", lambda: 50.0
    )
    game = _make_game()
    assert game._supply_fallback_ticks() == 5


# -- starvation integration: the watchdog arms the fallback --------------------


def test_starved_supply_arms_fallback_within_window(monkeypatch):
    # Drive the REAL idle watchdog against a starved supply line: prefetch
    # never produces a question, so nothing arms — until the fallback
    # window elapses and a curated-bank question arms itself. Compresses
    # the watchdog interval + fallback window so the loop runs in-process.
    monkeypatch.setattr(
        lily_config, "supply_fallback_seconds", lambda: 0.03
    )
    _patch_bank(monkeypatch, BANK_Q)

    async def scenario():
        game = _make_game()
        game.WATCHDOG_INTERVAL_SECONDS = 0.01  # 3 ticks -> ~0.03s to fire
        task = asyncio.ensure_future(game._idle_watchdog())
        try:
            for _ in range(200):  # up to ~2s wall clock
                if game.armed_question is not None:
                    break
                await asyncio.sleep(0.005)
        finally:
            game.game_over = True
            await task
        return game

    game = _run(scenario())
    assert game.armed_question is not None
    assert game.armed_question.get("id") == "kb_777"
    # Exactly one delivery intent + one nudge across the recovery.
    assert game._pending_delivery_qnum == game.sk.question_number
    assert len(game.instructed_replies) == 1
    assert game.next_question_ready() is True
