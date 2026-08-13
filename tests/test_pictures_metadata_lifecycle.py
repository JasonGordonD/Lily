"""WO-B3: metadata image_url lifecycle — set on picture open, clear elsewhere.

Failure class: reveal re-published the question's image_url, so the prior
picture stayed on the glass through verdict / arm N+1 / wrap-up.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lily_agent import LilyGame
from lily_scorekeeper import LilyScorekeeper


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _FakeRoomAPI:
    def __init__(self) -> None:
        self.requests: list = []

    async def update_room_metadata(self, req) -> None:
        self.requests.append(req)


class _FakeAPI:
    def __init__(self) -> None:
        self.room = _FakeRoomAPI()


class _FakeCtx:
    def __init__(self) -> None:
        self.api = _FakeAPI()
        self.room = type("R", (), {"name": "room-b3"})()


def _game():
    game = LilyGame.bare()
    game.sk = LilyScorekeeper("b3-meta")
    game.sk.question_number = 2
    game.ctx = _FakeCtx()
    game._glass_image_url = "https://old.example/stale.png"
    game._glass_image_at = 1.0
    return game


def _last_payload(game) -> dict:
    reqs = game.ctx.api.room.requests
    assert reqs, "expected a metadata publish"
    return json.loads(reqs[-1].metadata)


def test_publish_sets_image_url_when_present():
    g = _game()
    _run(g.publish_metadata(
        "What is this?",
        image_url="https://cdn.example/pic.png",
        category="landmarks",
    ))
    payload = _last_payload(g)
    assert payload["image_url"] == "https://cdn.example/pic.png"
    assert payload["category"] == "landmarks"
    assert payload["question_number"] == 2
    assert g._glass_image_url == "https://old.example/stale.png"  # set path keeps confirm


def test_publish_clears_image_url_and_glass_confirm():
    g = _game()
    _run(g.publish_metadata("Next text question"))
    payload = _last_payload(g)
    assert payload["image_url"] == ""
    assert g._glass_image_url is None
    assert g._glass_image_at is None


def test_reveal_publish_must_not_keep_picture():
    """Contract mirror of adjudicate reveal gather — no image_url kwarg."""
    g = _game()
    question = {
        "prompt": "What is this?",
        "canonical_answer": "x",
        "image_url": "https://cdn.example/pic.png",
        "choices": ["a", "b", "c", "d"],
        "category": "adult_couples",
    }
    # Same shape as the reveal publish after the B3 fix.
    _run(g.publish_metadata(
        question.get("prompt", ""),
        reveal={
            "answer": str(question.get("canonical_answer", "")),
            "winner": "Rami",
            "correct": True,
        },
        choices=question.get("choices"),
        eliminated=[],
        category=question.get("category"),
    ))
    payload = _last_payload(g)
    assert payload["image_url"] == ""
    assert payload["choices"] == ["a", "b", "c", "d"]
    assert payload["category"] == "adult_couples"
    assert payload["reveal"]["winner"] == "Rami"
    assert g._glass_image_url is None
