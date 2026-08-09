"""Live-fire cache canary (WO-LILY-HOTFIX-007 Y1b) — proves the deployed
prompt actually prefix-caches at x.ai, BEFORE a live call bets on it.

Two consecutive streaming calls with the PRODUCTION system prompt (the
real assembled LILY_SYSTEM_PROMPT) and a short user turn. The second
call must report prompt_tokens_details.cached_tokens > 0 — that is
Grok's automatic prefix cache hitting on the static prefix Y1a built.
Prints prompt/cached/TTFT for both calls; exits non-zero when the second
call shows no cache hit.

Runs from CI (cache-canary.yml, workflow_dispatch) where XAI_API_KEY
lives. Cost: two ~9k-token-prompt calls with a 60-token output cap.
"""

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import openai as openai_sdk

import lily_config
from lily_agent import LILY_SYSTEM_PROMPT


async def _call(client, messages, label):
    t0 = time.monotonic()
    ttft = None
    usage = None
    stream = await client.chat.completions.create(
        model=lily_config.vocal_model(),
        messages=messages,
        max_completion_tokens=60,
        stream=True,
        stream_options={"include_usage": True},
        extra_body={"reasoning_effort": lily_config.vocal_effort()},
    )
    async for chunk in stream:
        if (
            ttft is None
            and chunk.choices
            and chunk.choices[0].delta
            and chunk.choices[0].delta.content
        ):
            ttft = time.monotonic() - t0
        if getattr(chunk, "usage", None):
            usage = chunk.usage
    details = getattr(usage, "prompt_tokens_details", None)
    cached = getattr(details, "cached_tokens", 0) or 0
    prompt = getattr(usage, "prompt_tokens", 0) or 0
    print(
        f"CANARY | {label} | prompt={prompt} cached={cached} "
        f"hit={100.0 * cached / prompt if prompt else 0.0:.1f}% "
        f"ttft_ms={round(ttft * 1000, 1) if ttft else None}"
    )
    return prompt, cached, ttft


async def main() -> int:
    api_key = lily_config.xai_api_key()
    if not api_key:
        print("CANARY | SKIP | XAI_API_KEY not set")
        return 2
    client = openai_sdk.AsyncClient(
        api_key=api_key, base_url="https://api.x.ai/v1", max_retries=0
    )
    messages = [
        {"role": "system", "content": LILY_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": "Say exactly: cache canary check, one short sentence.",
        },
    ]
    p1, c1, t1 = await _call(client, messages, "call_1_cold")
    # Same prefix, different tail — the prefix is what must cache-hit.
    messages2 = messages[:1] + [
        {
            "role": "user",
            "content": "Say exactly: second canary check, one short sentence.",
        }
    ]
    p2, c2, t2 = await _call(client, messages2, "call_2_warm")
    if c2 <= 0:
        print(
            "CANARY | FAIL | second call reported zero cached tokens — the "
            "static prefix is NOT cache-hitting at x.ai"
        )
        return 1
    speedup = (
        f"{t1 / t2:.2f}x" if (t1 and t2 and t2 > 0) else "n/a"
    )
    print(
        f"CANARY | PASS | warm hit {100.0 * c2 / p2:.1f}% of prompt; "
        f"TTFT cold/warm speedup {speedup}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
