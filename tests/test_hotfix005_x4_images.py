"""WO-LILY-HOTFIX-005 X4 — successful images never reach the glass.

DB held four grok-imagine successes with URLs while the operator asked "how
come you're not showing me pictures?" and roi_0009 generated twice 19s
apart. Fixes here:
  - a per-(session, question) generation memo so a re-request for a slot
    that has no bank row re-serves the first URL (no double spend);
  - a render-confirmation recorder + grounded state readout so "the picture
    is up" is a readable state, not an assumption.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_imagegen
from lily_agent import LilyGame
from lily_scorekeeper import LilyScorekeeper


# -- double-spend memo --------------------------------------------------------

def test_session_image_memo_dedupes_and_evicts():
    lily_imagegen._SESSION_IMAGE_MEMO.clear()
    key = ("sess-1", "roi_0009")
    lily_imagegen._remember_session_image(key, "https://x/img.png")
    assert lily_imagegen._SESSION_IMAGE_MEMO.get(key) == "https://x/img.png"
    # empty url never stored
    lily_imagegen._remember_session_image(("sess-1", "roi_x"), "")
    assert ("sess-1", "roi_x") not in lily_imagegen._SESSION_IMAGE_MEMO


def test_session_image_memo_is_bounded():
    lily_imagegen._SESSION_IMAGE_MEMO.clear()
    cap = lily_imagegen._SESSION_IMAGE_MEMO_CAP
    for i in range(cap + 25):
        lily_imagegen._remember_session_image(("s", f"q{i}"), f"u{i}")
    assert len(lily_imagegen._SESSION_IMAGE_MEMO) <= cap
    # oldest evicted, newest retained
    assert ("s", "q0") not in lily_imagegen._SESSION_IMAGE_MEMO
    assert ("s", f"q{cap + 24}") in lily_imagegen._SESSION_IMAGE_MEMO


# -- render confirmation + grounded readout -----------------------------------

def _game_with_armed(image_url=None):
    game = LilyGame.bare()
    game.sk = LilyScorekeeper("x4")
    game.armed_question = {"prompt": "?", "image_url": image_url} if image_url else {}
    return game


def test_note_image_rendered_records_confirmation():
    game = _game_with_armed(image_url="https://x/a.png")
    game.note_image_rendered("https://x/a.png")
    assert game._glass_image_url == "https://x/a.png"
    line = game._glass_image_state_line()
    assert line is not None and "CONFIRMED" in line


def test_glass_line_flags_unconfirmed_intended_image():
    game = _game_with_armed(image_url="https://x/b.png")
    # nothing confirmed yet
    line = game._glass_image_state_line()
    assert line is not None and "NOT confirmed" in line


def test_glass_line_absent_when_no_image_anywhere():
    game = _game_with_armed(image_url=None)
    assert game._glass_image_state_line() is None


def test_note_image_rendered_ignores_empty():
    game = _game_with_armed(image_url="https://x/c.png")
    game.note_image_rendered("")
    assert getattr(game, "_glass_image_url", None) is None
