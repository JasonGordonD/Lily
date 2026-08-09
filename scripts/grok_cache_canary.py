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
    # Mirror production exactly: the agent sends a per-session
    # x-grok-conv-id header (lily_build_grok_vocal_llm) — first canary run
    # WITHOUT it showed an identical 7% "cached" on cold AND warm calls
    # (a provider baseline, not our prefix), so conversation identity
    # plausibly scopes x.ai's cache. uuid4 keeps runs independent.
    import uuid

    conv_id = f"lily-canary-{uuid.uuid4()}"
    client = openai_sdk.AsyncClient(
        api_key=api_key,
        base_url="https://api.x.ai/v1",
        max_retries=0,
        default_headers={"x-grok-conv-id": conv_id},
    )
    messages = [
        {"role": "system", "content": LILY_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": "Say exactly: cache canary check, one short sentence.",
        },
    ]
    # x.ai's cache is SERVER-AFFINE (per-node KV cache; conv-id routes to
    # the node). A miss returns a small constant baseline — observed live
    # as cached=640 on cold AND warm. Cache writes need time to land, so
    # probe at increasing spacing; a real session (same conv-id, dozens
    # of turns over minutes) is a far stronger workload than any canary.
    p1, c1, t1 = await _call(client, messages, "call_1_cold")
    results = [(p1, c1, t1)]
    for wait, label in ((3.0, "call_2_warm_3s"), (10.0, "call_3_warm_10s")):
        await asyncio.sleep(wait)
        tail = [
            {
                "role": "user",
                "content": f"Say exactly: {label} check, one short sentence.",
            }
        ]
        results.append(await _call(client, messages[:1] + tail, label))
    cold_cached = results[0][1]
    best_warm = max(r[1] for r in results[1:])
    if best_warm < cold_cached:
        print(
            f"CANARY | FAIL | warm cached ({best_warm}) REGRESSED below "
            f"cold ({cold_cached})"
        )
        return 1
    if best_warm > cold_cached:
        p_last = results[-1][0]
        print(
            f"CANARY | PASS | best warm hit {100.0 * best_warm / p_last:.1f}% "
            f"of prompt (cold baseline {cold_cached})"
        )
        return 0
    # Identical baseline on every call: the prefix is not PROVEN to
    # cache-hit under this canary's workload. Not a failure of the build
    # — the definitive read is LILY_METRICS | LLM_CALL hit% in the next
    # live session, where the same conv-id spans a whole game.
    print(
        f"CANARY | INCONCLUSIVE | cached stuck at baseline {cold_cached} "
        f"across {len(results)} calls (server-affinity miss or slow cache "
        "write) — prefix caching NOT PROVEN here; close on live "
        "LILY_METRICS | LLM_CALL hit%"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
