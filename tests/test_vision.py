"""lily_vision — the Zuna vision port (the 12:48 "you don't have image
ingestion" live fixture). Offline: the structured failure contract, the
availability gate, and the manifest/options integration. The live Grok
call itself is network and runs against the deployed agent."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_capabilities
import lily_config
import lily_vision


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_empty_url_is_unavailable():
    assert _run(lily_vision.lily_describe_image("")) == {
        "status": "unavailable",
        "reason": "empty image_url",
    }
    assert _run(lily_vision.lily_describe_image("   "))["status"] == "unavailable"


def test_non_http_scheme_is_an_error():
    result = _run(lily_vision.lily_describe_image("ftp://example.com/x.png"))
    assert result == {"status": "error", "reason": "invalid image_url scheme"}
    assert _run(lily_vision.lily_describe_image("not a url"))["status"] == "error"


def test_missing_key_is_honest_unavailable(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    assert lily_vision.lily_vision_available() is False
    result = _run(
        lily_vision.lily_describe_image("https://example.com/photo.jpg")
    )
    assert result == {
        "status": "unavailable",
        "reason": "vision provider unconfigured",
    }


def test_key_flips_availability(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "test-key")
    assert lily_vision.lily_vision_available() is True


class _VisionResponse:
    def __init__(self, body):
        self.status = 200
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def json(self, content_type=None):
        return self._body

    async def text(self):
        return ""


class _VisionSession:
    captured = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    def post(self, url, json=None, **kwargs):
        _VisionSession.captured = {"url": url, "body": json}
        content = (
            '{"approved":true,"reason":"matches"}'
            if json.get("response_format")
            else "A literal image description."
        )
        return _VisionResponse({
            "choices": [{"message": {"content": content}}]
        })


def test_grok_4_5_url_vision_payload(monkeypatch):
    monkeypatch.setattr(lily_config, "xai_api_key", lambda: "x")
    monkeypatch.setattr(
        lily_vision.aiohttp, "ClientSession", _VisionSession
    )
    result = _run(
        lily_vision.lily_describe_image(
            "https://example.com/image.jpg", "Describe literally."
        )
    )
    assert result == {
        "status": "ok",
        "description": "A literal image description.",
    }
    body = _VisionSession.captured["body"]
    assert body["model"] == "grok-4.5"
    assert body["messages"][0]["content"][1]["image_url"]["detail"] == "high"


def test_grok_4_5_bytes_classifier_uses_data_url(monkeypatch):
    monkeypatch.setattr(lily_config, "xai_api_key", lambda: "x")
    monkeypatch.setattr(
        lily_vision.aiohttp, "ClientSession", _VisionSession
    )
    approved, reason = _run(
        lily_vision.lily_classify_image_bytes(
            b"jpeg-bytes", "image/jpeg", "Does this match?"
        )
    )
    assert approved is True and reason == "matches"
    body = _VisionSession.captured["body"]
    image_url = body["messages"][0]["content"][1]["image_url"]["url"]
    assert image_url.startswith("data:image/jpeg;base64,")
    assert body["response_format"] == {"type": "json_object"}


def test_manifest_carries_image_ingestion_at_v3():
    entry = next(
        e
        for e in lily_capabilities.LILY_CAPABILITIES
        if e["key"] == "image_ingestion"
    )
    assert entry["since"] == 3
    assert lily_capabilities.lily_feature_version() >= 3
    assert "lily_analyze_image" in entry["tools"]
    assert entry["availability_key"] == "vision"
    # A voice-presets-era (v2) table hears about image sharing on rematch —
    # and every feature newer than v2 (custom categories landed at v5),
    # nothing v2-or-older.
    delta = lily_capabilities.lily_whats_new(2)
    assert any("photo" in line for line in delta)
    expected = [
        e["description"]
        for e in lily_capabilities.LILY_CAPABILITIES
        if int(e.get("since", 1)) > 2
    ]
    assert delta == expected


def test_vision_off_yields_honest_availability_line():
    lines = lily_capabilities.lily_availability_lines(
        {"adult_deck": True, "pictures_real_sourcing": True, "vision": False}
    )
    assert any("image_ingestion" in line for line in lines)
    lines_on = lily_capabilities.lily_availability_lines(
        {"adult_deck": True, "pictures_real_sourcing": True, "vision": True}
    )
    assert lines_on == []


def test_player_source_is_a_legal_bucket_source():
    import lily_images

    assert "player" in lily_images.IMAGE_SOURCES
