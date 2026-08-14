"""
lily_vision.py — image analysis (vision) for LILY.

Native lift of Zuna's vision tool (WO-ZUNA-SOCIAL-001 sub-agent 3 shape):
xAI Grok via the OpenAI-shaped chat-completions endpoint with `image_url`
content parts — xAI fetches the image itself, no client-side download or
base64 round-trip. Same provider as the rest of the fleet's vision
surface (JRVS `mjrvs_vision`, dr_tijoux, Zuna) — one mental model for
the operator, and the `XAI_API_KEY` secret shape is fleet-standard.

Two consumers:
  - `lily_analyze_image` (module-level function tool, registered on
    LilyAgent via tools=[...]): a player names an image URL and asks
    about it.
  - the `lily.image.upload` byte-stream ingest in the entrypoint: a
    player shares a photo from the UI's image button; the bytes land in
    the lily-images bucket (content-addressed, cache-first) and the
    public URL flows through `lily_describe_image` — this is the "you
    don't have image ingestion" fix from the 12:48 live fixture.

Failure semantics (Zuna contract, kept verbatim — every path returns a
structured dict, never raises):
  - missing XAI_API_KEY   -> {"status": "unavailable", "reason": "vision provider unconfigured"}
  - empty image_url       -> {"status": "unavailable", "reason": "empty image_url"}
  - non-http(s) scheme    -> {"status": "error", "reason": "invalid image_url scheme"}
  - HTTP non-2xx          -> {"status": "error", "reason": "HTTP <n>"}
  - timeout               -> {"status": "error", "reason": "timeout after <n>s"}
  - unexpected/empty body -> {"status": "error", "reason": "unexpected response" | "empty response"}

Injection caution: the description text reaches the vocal LLM as tool /
context material. It is DESCRIPTIVE input, never instructions — the
system prompt's self-knowledge and say-gate rules apply to whatever she
says about it.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Annotated, Optional
from urllib.parse import urlparse

import aiohttp
from livekit.agents import RunContext, function_tool

import lily_config

logger = logging.getLogger("lily_vision")

_XAI_URL = f"{lily_config.xai_base_url()}/chat/completions"
_DEFAULT_PROMPT = "Describe this image in detail."
_TIMEOUT_S = 30.0


def lily_vision_available() -> bool:
    return bool(lily_config.xai_api_key())


def _valid_http_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


async def lily_describe_image(
    image_url: str, prompt: Optional[str] = None
) -> dict:
    """Analyze one image URL with Grok vision. Structured dict contract
    (see module docstring); never raises."""
    url = (image_url or "").strip()
    if not url:
        return {"status": "unavailable", "reason": "empty image_url"}
    if not _valid_http_url(url):
        return {"status": "error", "reason": "invalid image_url scheme"}
    api_key = lily_config.xai_api_key()
    if not api_key:
        return {
            "status": "unavailable",
            "reason": "vision provider unconfigured",
        }
    ask = (prompt or "").strip() or _DEFAULT_PROMPT
    return await _grok_vision_text(url, ask)


async def _grok_vision_text(
    image_url: str, prompt: str, *, json_mode: bool = False
) -> dict:
    """One Grok 4.5 image→text call for URLs or data URLs."""
    api_key = lily_config.xai_api_key()
    if not api_key:
        return {
            "status": "unavailable",
            "reason": "vision provider unconfigured",
        }
    payload = {
        "model": lily_config.vision_model(),
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": image_url, "detail": "high"},
                    },
                ],
            }
        ],
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    try:
        timeout = aiohttp.ClientTimeout(total=_TIMEOUT_S)
        async with aiohttp.ClientSession(timeout=timeout) as http:
            async with http.post(
                _XAI_URL,
                json=payload,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            ) as resp:
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    data = {"raw": await resp.text()}
                if resp.status < 200 or resp.status >= 300:
                    return {"status": "error", "reason": f"HTTP {resp.status}"}
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return {"status": "error", "reason": "unexpected response"}
        if not isinstance(text, str) or not text.strip():
            return {"status": "error", "reason": "empty response"}
        logger.info(
            "LILY_VISION | complete | model=%s chars=%d",
            lily_config.vision_model(), len(text),
        )
        return {"status": "ok", "description": text.strip()}
    except asyncio.TimeoutError:
        logger.warning("LILY_VISION | timeout")
        return {"status": "error", "reason": f"timeout after {_TIMEOUT_S:.0f}s"}
    except Exception as exc:
        reason = str(exc) or type(exc).__name__
        logger.warning("LILY_VISION | error | error=%s", reason)
        return {"status": "error", "reason": reason}


async def lily_describe_image_bytes(
    image_bytes: bytes,
    content_type: str,
    prompt: str,
    *,
    json_mode: bool = False,
) -> dict:
    if not image_bytes:
        return {"status": "error", "reason": "empty image bytes"}
    mime = (content_type or "image/jpeg").split(";", 1)[0].strip()
    data_url = (
        f"data:{mime};base64,"
        + base64.b64encode(image_bytes).decode("ascii")
    )
    return await _grok_vision_text(
        data_url, prompt, json_mode=json_mode
    )


async def lily_classify_image_bytes(
    image_bytes: bytes, content_type: str, prompt: str
) -> tuple[bool, str]:
    result = await lily_describe_image_bytes(
        image_bytes, content_type, prompt + "\nReturn JSON: "
        '{"approved": true|false, "reason": "short reason"}.',
        json_mode=True,
    )
    if result.get("status") != "ok":
        return False, str(result.get("reason") or "vision unavailable")
    try:
        verdict = json.loads(result["description"])
    except (json.JSONDecodeError, TypeError, KeyError):
        return False, "vision returned unparseable JSON"
    return bool(verdict.get("approved")), str(
        verdict.get("reason") or ""
    )[:300]


@function_tool(
    name="lily_analyze_image",
    description=(
        "Look at an image and describe / analyze / answer questions about "
        "it. Use this when a player shares a photo or image URL and asks "
        "what's in it, or when a shared photo's URL appears in your "
        "context and the table wants your take. Returns "
        "{status:'ok', description} on success or {status, reason} on "
        "failure — if it fails, say so honestly; never describe an image "
        "you could not actually see."
    ),
)
async def lily_analyze_image(
    context: RunContext,
    image_url: Annotated[
        str,
        "Public http(s) URL of the image to analyze (a shared photo's "
        "URL from your context, or one a player names).",
    ],
    prompt: Annotated[
        str,
        "Optional question or instruction about the image.",
    ] = _DEFAULT_PROMPT,
) -> dict:
    """Grok vision analysis — graceful on every failure mode."""
    return await lily_describe_image(image_url, prompt)


__all__ = [
    "lily_analyze_image",
    "lily_describe_image",
    "lily_describe_image_bytes",
    "lily_classify_image_bytes",
    "lily_vision_available",
]
