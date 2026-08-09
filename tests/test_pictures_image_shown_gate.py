"""WO-B4: image_shown speak gate — not drawn ≠ on screen.

False "look at the screen" / "picture is up" rewrites until
lily_control.image_shown confirms the armed URL. Timeout ⇒ didn't-land.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_say_gate
from lily_agent import LilyGame
from lily_scorekeeper import LilyScorekeeper


def _game(image_url="https://x/pic.png"):
    game = LilyGame.__new__(LilyGame)
    game.sk = LilyScorekeeper("b4-gate")
    game.armed_question = {
        "prompt": "What is this?",
        "image_url": image_url,
    }
    game._glass_image_url = None
    game._glass_image_at = None
    game._glass_image_pending_url = image_url
    game._glass_image_pending_at = time.monotonic()
    return game


def test_false_on_screen_detector():
    assert lily_say_gate.lily_false_on_screen_claim(
        "Look at the screen — what's that?"
    )
    assert lily_say_gate.lily_false_on_screen_claim(
        "The picture is up!"
    )
    assert not lily_say_gate.lily_false_on_screen_claim(
        "What do you think the answer is?"
    )


def test_pending_rewrite_until_confirm():
    g = _game()
    assert g.picture_on_glass_confirmed() is False
    assert "NOT confirmed" in (g._glass_image_state_line() or "")
    g.note_image_rendered("https://x/pic.png")
    assert g.picture_on_glass_confirmed() is True
    assert "CONFIRMED" in (g._glass_image_state_line() or "")


def test_didnt_land_after_timeout():
    g = _game()
    g._glass_image_pending_at = time.monotonic() - 30.0
    assert g.picture_on_glass_failed(timeout_s=8.0) is True
    line = g._glass_image_state_line()
    assert line is not None and "DID NOT LAND" in line
    assert "didn't land" in lily_say_gate.lily_picture_didnt_land_rewrite().lower()


def test_confirm_clears_pending():
    g = _game()
    g.note_image_rendered("https://x/pic.png")
    assert g._glass_image_pending_url is None
    assert g.picture_on_glass_failed() is False
