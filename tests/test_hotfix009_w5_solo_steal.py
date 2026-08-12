"""WO-LILY-HOTFIX-009 W5 — steal windows read roster state at dispatch: a
table of one hearable person never arms a steal window and never generates
steal language, in ANY pacing; multi-player behaviour is unaffected.

THE live incident (session RM_RQTZZanrHURF, solo mic):
  01:32:06  she says the right thing — "No steal hanging in the air tonight
            with just you at the mic."
  01:32:11  the machinery fires one anyway — "Nobody locked it. Steal
            window — five seconds, anyone else want it?" — to a table of
            one. Conversation knew the roster; the mechanism did not.

Root cause (archaeology): the steal gate read the stealer pool by ROSTER
NAME (`any(name not in judged_keys for name in players)`). The session
carried the real ghost shape — "Rummy" mis-captured at 01:29:30, then
NATO-corrected to "Rami": bind_speaker rebinds the S1 label to "Rami" and
NULLS "Rummy"'s label, leaving TWO rostered names but ONE hearable person.
Counting by name read `stealers_exist` True on a table of one. W5 counts
the pool by LIVE VOICEPRINT (non-null speaker_label) instead: the ghost
carries no label, so it cannot produce audio and cannot steal, and a table
of one hearable person disarms the window before it arms — the same read
that fixes the spoken player count.

W5 is a SIBLING gate to W4's relaxed condition on the same `steal_possible`
expression, not a replacement: steal arms only when pacing != relaxed AND
the table has more than one hearable person AND an unjudged one remains.

Same import boundary / harness as test_hotfix009_w4_relaxed_pacing.py — its
fixtures are reused so the two workstreams share one steal harness.
"""

import pytest

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_hotfix009_w4_relaxed_pacing import (
    _make_game,
    _arm_q2,
    _run,
    Q_MITO,
    SURRENDER,
)


def _steal_beats(game) -> list:
    return [i for i in game.session.instructions if "steal" in i.lower()]


# ===========================================================================
# 1. The 01:32:11 defect, in TIMED mode: the real ghost roster (one voice,
#    two rostered names) must NOT arm a steal window. This is the exact case
#    W4's test 5 used to assert AS a steal — W5 flips it: a table of one
#    hearable person never steals, whatever the pacing.
# ===========================================================================


def test_timed_ghost_solo_missed_question_never_arms_steal():
    game = _make_game()
    now = _arm_q2(game, ghost_roster=True)
    assert game.sk.pacing == "timed"
    # The ghost shape that read stealers_exist True: two rostered names, but
    # "Rummy"'s label was nulled by the NATO correction — one hearable voice.
    assert len(game.sk.players) == 2
    assert game.sk.players["Rummy"]["speaker_label"] is None
    assert game.sk.players["Rami"]["speaker_label"] == "S1"

    game.sk.open_answer_window(
        duration=15.0, now=now, question_id=Q_MITO["id"],
        question_index=game.sk.question_number, registered=True,
    )
    game.sk.on_transcript_segment(
        text=SURRENDER, speaker_label="S1", is_final=True,
        now=now + 4, segment_start_time=now + 4, segment_end_time=now + 6,
    )

    _run(lambda: game.adjudicate(steal_allowed=True))

    assert game._steal_window is False
    assert _steal_beats(game) == []          # no "Steal window — five seconds"
    # Fell through to the ordinary reveal instead of parking on a clock.
    assert not game.sk.answer_window_open
    assert any(
        "mitochondria" in s.lower() for s in game.session.said
    )


# ===========================================================================
# 2. A genuinely solo table (one voiceprint, no ghost) in timed mode: same
#    outcome — a missed question reveals, it never steals.
# ===========================================================================


def test_timed_true_solo_missed_question_never_arms_steal():
    game = _make_game()
    now = _arm_q2(game, ghost_roster=False)
    assert game.sk.pacing == "timed"
    assert len(game.sk.players) == 1

    game.sk.open_answer_window(
        duration=15.0, now=now, question_id=Q_MITO["id"],
        question_index=game.sk.question_number, registered=True,
    )
    game.sk.on_transcript_segment(
        text=SURRENDER, speaker_label="S1", is_final=True,
        now=now + 4, segment_start_time=now + 4, segment_end_time=now + 6,
    )

    _run(lambda: game.adjudicate(steal_allowed=True))

    assert game._steal_window is False
    assert _steal_beats(game) == []
    assert not game.sk.answer_window_open
    assert any(
        "mitochondria" in s.lower() for s in game.session.said
    )


# ===========================================================================
# 3. Multi-player is unaffected: two distinct voiceprints, one answers and
#    misses, the other stays silent -> a real steal window arms. This is the
#    guarantee W4's test 5 also holds; W5 keeps it independently on a clean
#    (no-ghost) roster so a regression here is unambiguous.
# ===========================================================================


def test_timed_multiplayer_missed_question_still_arms_steal():
    game = _make_game()
    now = _arm_q2(game, ghost_roster=False)          # Rami on S1
    game.sk.bind_speaker("S2", "Maria")              # a distinct second voice
    assert game.sk.pacing == "timed"
    assert len(game.sk.players) == 2

    game.sk.open_answer_window(
        duration=15.0, now=now, question_id=Q_MITO["id"],
        question_index=game.sk.question_number, registered=True,
    )
    # Rami answers and misses; Maria never answers -> eligible stealer.
    game.sk.on_transcript_segment(
        text=SURRENDER, speaker_label="S1", is_final=True,
        now=now + 4, segment_start_time=now + 4, segment_end_time=now + 6,
    )

    _run(lambda: game.adjudicate(steal_allowed=True))

    assert game._steal_window is True
    assert game.sk.answer_window_open
    assert _steal_beats(game) != []


# ===========================================================================
# 4. The ghost does not count as the second person even when a real second
#    player is also present-and-judged: ghost "Rummy" (no label) + real
#    "Rami" (S1, judged) with NO third voice -> the table has one hearable
#    person left unjudged... none, and one hearable person total besides the
#    misser: no steal. A ghost never manufactures a stealer.
# ===========================================================================


def test_ghost_duplicate_is_not_counted_as_a_second_stealer():
    game = _make_game()
    now = _arm_q2(game, ghost_roster=True)   # Rummy(None) + Rami(S1)
    assert game.sk.pacing == "timed"
    # Exactly one hearable voiceprint despite two roster rows.
    hearable = {
        st.get("speaker_label")
        for st in game.sk.players.values()
        if st.get("speaker_label")
    }
    assert hearable == {"S1"}

    game.sk.open_answer_window(
        duration=15.0, now=now, question_id=Q_MITO["id"],
        question_index=game.sk.question_number, registered=True,
    )
    game.sk.on_transcript_segment(
        text=SURRENDER, speaker_label="S1", is_final=True,
        now=now + 4, segment_start_time=now + 4, segment_end_time=now + 6,
    )

    _run(lambda: game.adjudicate(steal_allowed=True))

    assert game._steal_window is False
    assert _steal_beats(game) == []
