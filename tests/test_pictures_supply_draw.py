"""WO-B2: supply draws seeded arsenal when media_mode=pictures.

Failure class: after pictures ON, a stale voice-only next_question blocked
prefetch; arsenal never ran. Sign failure must not return a storage path
as image_url. Logs: LILY_SUPPLY | PICTURE_DRAW | id= url=yes|no mode=
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_arsenal
from lily_agent import LilyGame
from lily_scorekeeper import LilyScorekeeper


def _run(coro):
    return asyncio.run(coro)


def _game(*, mode="general", media="voice_only"):
    game = LilyGame.bare()
    game.sk = LilyScorekeeper("b2-supply")
    game.sk.mode = mode
    game.sk.media_mode = media
    game.sk.adult_image_intensity = "mix"
    game.supabase = object()
    game.group_id = "g-b2"
    game.asked_history = []
    game.next_question = None
    game.armed_question = None
    game.game_started = False
    game.game_over = False
    game._prefetch_task = None
    game._prefetch_stall_ticks = 0
    game._pending_picture_on_offer = False
    game.session = None
    game.gated_say = lambda *a, **k: None
    game.publish_attributes_nowait = lambda: None
    game._kick_arsenal_replenish = lambda *a, **k: None
    return game


def test_arsenal_draw_sets_signed_image_url(monkeypatch):
    g = _game(mode="adult", media="pictures")

    async def fake_draw(*_a, **_k):
        return {
            "id": "ars_1",
            "prompt": "What is this?",
            "canonical_answer": "x",
            "image_storage_path": "adult_suggestive/ars_1.png",
            "image_url": "adult_suggestive/ars_1.png",
        }

    async def fake_sign(_sb, path):
        assert path.endswith("ars_1.png")
        return "https://signed.example/ars_1.png"

    monkeypatch.setattr(lily_arsenal, "lily_arsenal_draw", fake_draw)
    monkeypatch.setattr(
        "lily_images.lily_arsenal_image_url", fake_sign
    )
    q = _run(g._arsenal_picture_draw("adult"))
    assert q is not None
    assert q["image_url"] == "https://signed.example/ars_1.png"


def test_arsenal_sign_failure_does_not_return_storage_path(monkeypatch):
    g = _game(mode="adult", media="pictures")

    async def fake_draw(*_a, **_k):
        return {
            "id": "ars_bad",
            "prompt": "x",
            "canonical_answer": "y",
            "image_storage_path": "adult_suggestive/bad.png",
            "image_url": "adult_suggestive/bad.png",
        }

    async def fake_sign(_sb, _path):
        return None

    monkeypatch.setattr(lily_arsenal, "lily_arsenal_draw", fake_draw)
    monkeypatch.setattr(
        "lily_images.lily_arsenal_image_url", fake_sign
    )
    assert _run(g._arsenal_picture_draw("adult")) is None


def test_voice_only_slot_gate_blocks_picture_kind():
    g = _game(media="voice_only")
    g.rounds_total = 3
    assert g._picture_kind_for_slot(1) is None
    g.sk.set_media_mode("pictures")
    # First question of a round is a picture slot in pictures mode.
    kind = g._picture_kind_for_slot(1)
    assert kind is not None


def test_activate_pictures_drops_stale_voice_prefetch(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "x")
    g = _game(mode="adult", media="voice_only")
    g.next_question = {
        "id": "text_only",
        "prompt": "No image",
        "canonical_answer": "z",
    }
    assert g.try_activate_pictures(source="test", announce=False) == "on"
    assert g.sk.media_mode == "pictures"
    assert g.next_question is None


def test_refresh_clears_pictureless_next_and_restarts_prefetch():
    g = _game(media="pictures")
    g.next_question = {"id": "stale", "prompt": "text"}
    calls = []

    def start_prefetch():
        calls.append("prefetch")

    g.start_prefetch = start_prefetch
    # Outside a running loop, refresh clears stale Q and defers prefetch.
    g._refresh_supply_for_pictures_on(source="unit")
    assert g.next_question is None
    assert calls == []

    async def _with_loop():
        g.next_question = {"id": "stale2", "prompt": "text"}
        g._refresh_supply_for_pictures_on(source="unit-loop")
        assert g.next_question is None
        assert calls == ["prefetch"]

    _run(_with_loop())


def test_refresh_keeps_question_that_already_has_http_url():
    g = _game(media="pictures")
    g.next_question = {
        "id": "pic",
        "image_url": "https://cdn.example/pic.png",
    }
    g.start_prefetch = lambda: (_ for _ in ()).throw(
        AssertionError("must not prefetch")
    )
    g._refresh_supply_for_pictures_on(source="unit")
    assert g.next_question["id"] == "pic"
