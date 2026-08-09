"""Wire pictures ON when the bank is ready — E66E1B regression.

Live: adult deck + pictures in mixed mode; intensity landed as mix but
media_mode stayed voice_only. Lily said "lane's healthy, just not live"
while lily_picture_arsenal had ready adult rows. Root cause: heat tool
never flipped media_mode; spoken detector missed "pictures in mixed mode"
and "live immediately" after her want-them-on offer.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_scorekeeper
from lily_agent import LilyAgent, LilyGame
from lily_scorekeeper import (
    LilyScorekeeper,
    lily_detect_media_choice,
    lily_detect_picture_on_offer,
    lily_is_picture_on_confirm,
)


def _game(*, mode="adult", supabase=object()):
    game = LilyGame.__new__(LilyGame)
    game.sk = LilyScorekeeper("pictures-wire")
    game.sk.mode = mode
    game.sk.media_mode = "voice_only"
    game.sk.set_adult_image_intensity("suggestive")
    game.supabase = supabase
    game._pending_picture_on_offer = False
    game.game_started = False
    game.game_over = False
    game._say_log = []

    def _gated_say(_player, key, text, *, source=None):
        game._say_log.append((key, text, source))

    game.gated_say = _gated_say
    game.publish_attributes_nowait = lambda: None
    return game


# -- detectors (live phrases) --------------------------------------------------


def test_detect_pictures_in_mixed_mode():
    assert lily_detect_media_choice("Pictures in mixed mode.") == "pictures"
    assert lily_detect_media_choice(
        "I want the adult deck with pictures in mixed mode"
    ) == "pictures"


def test_detect_images_live_phrases():
    assert lily_detect_media_choice("get the images live") == "pictures"
    assert lily_detect_media_choice("pictures live") == "pictures"


def test_picture_on_offer_and_confirm():
    assert lily_detect_picture_on_offer(
        "Want me to keep chasing that, or start voice-only — "
        "pictures are not switched on yet, want them on?"
    )
    assert lily_is_picture_on_confirm("Yes")
    assert lily_is_picture_on_confirm("Live immediately.")
    assert lily_is_picture_on_confirm("turn them on")
    assert not lily_is_picture_on_confirm("no pictures please")


# -- activation ----------------------------------------------------------------


def test_try_activate_pictures_flips_when_healthy(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "x")
    g = _game()
    assert g.try_activate_pictures(source="test") == "on"
    assert g.sk.media_mode == "pictures"


def test_try_activate_pictures_refuses_without_key(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    g = _game()
    assert g.try_activate_pictures(source="test") == "unavailable_gen"
    assert g.sk.media_mode == "voice_only"


def test_picture_on_confirm_after_offer(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "x")
    g = _game()
    g.note_picture_on_offer(
        "Pictures still aren't switched on yet — want them on?"
    )
    assert g._pending_picture_on_offer is True
    assert g.note_picture_on_confirm("Live immediately.") is True
    assert g.sk.media_mode == "pictures"


def test_adult_intensity_tool_flips_media_mode(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "x")
    monkeypatch.setattr(
        "lily_config.architect_mode", lambda: True
    )
    g = _game()
    agent = LilyAgent.__new__(LilyAgent)
    agent._game = g
    msg = asyncio.run(
        LilyAgent.lily_set_adult_image_intensity.__wrapped__(
            agent, None, intensity="mix", confirmed_table=True
        )
    )
    assert g.sk.adult_image_intensity == "mix"
    assert g.sk.media_mode == "pictures"
    assert "Picture rounds are ON" in msg
    assert "MIX" in msg
