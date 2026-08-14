"""Live-fire ElevenLabs alignment gate (WO-LILY-UI-SYNC-TYPEWRITER-001) —
proves that `/v1/text-to-speech/{voice}/stream/with-timestamps` actually
returns per-CHARACTER alignment for the LOCKED production config, BEFORE
LILY_VOICE_SYNCED_TRANSCRIPT bets a live session on it.

Modelled on scripts/grok_cache_canary.py: one manual-dispatch CI run that
spends a trivial amount of real vendor credit and prints a verdict.

WHY THIS EXISTS. The word-level path in lily_tts.LilyChunkedStream._run
swaps the endpoint from `/stream` (raw PCM) to `/stream/with-timestamps`
(newline-delimited JSON: base64 PCM + an `alignment` block) and reads
per-word timings out of that alignment. eleven_v3 is an alpha model and
alignment support is a VENDOR-SIDE property that can differ by model, by
plan and over time — nothing in the request tells you whether it will
come back. This gate answers exactly that question against the real
account, so the flag flip is evidence-based rather than hopeful.

WHAT IS LOCKED. The request body here is assembled from lily_tts's OWN
constants and per-voice resolver — MODEL_ID, OUTPUT_FORMAT and
`_voice_settings_for(voice)` — not from a copy. The operator-locked
voice / model / voice_settings are therefore verified as SHIPPED: if
anyone edits those values in lily_tts.py, this gate follows them
automatically and cannot drift into gating a config production does not
use. The body is byte-identical to the one the raw `/stream` path sends;
only the endpoint differs, which is the entire premise of the swap.

OUTPUT. `GATE | GREEN` when per-character alignment came back (plus a few
timing samples and the derived first words, so a human can eyeball that
onsets are monotonic and plausible), `GATE | RED` when it did not.

EXIT CODE IS ALWAYS 0 — this is INFORMATIONAL, a non-gating job. A RED
here is a signal to keep/flip LILY_VOICE_SYNCED_TRANSCRIPT off, not a
reason to fail an unrelated pipeline. The only non-zero exits are 2 for
"no key, nothing was measured" (never mistake a skip for a GREEN).

THE KEY IS NEVER PRINTED. It is read from ELEVEN_API_KEY via
lily_config, sent in the `xi-api-key` header, and no code path echoes it
— error bodies are truncated and headers are never dumped.
"""

import asyncio
import base64
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiohttp

import lily_config
from lily_tts import (
    ELEVENLABS_API_BASE,
    MODEL_ID,
    OUTPUT_FORMAT,
    SAMPLE_RATE,
    _voice_settings_for,
)

# A short line with clean word boundaries: enough characters for the
# alignment arrays to be obviously per-character, short enough to cost
# essentially nothing.
PROBE_TEXT = "Which planet in our solar system is the largest?"


async def main() -> int:
    try:
        api_key = lily_config.eleven_api_key()
    except Exception:
        api_key = ""
    if not api_key:
        print("GATE | SKIP | ELEVEN_API_KEY not set — nothing measured")
        return 2

    voice_id = lily_config.lily_voice_id()
    # Assembled from lily_tts's own constants/resolver so the gate can
    # never drift from what production actually sends (see module docstring).
    voice_settings = dict(_voice_settings_for(voice_id))
    body = {
        "text": PROBE_TEXT,
        "model_id": MODEL_ID,
        "voice_settings": voice_settings,
        "apply_text_normalization": "auto",
    }
    url = (
        f"{ELEVENLABS_API_BASE}/text-to-speech/{voice_id}"
        f"/stream/with-timestamps?output_format={OUTPUT_FORMAT}"
    )
    print(
        f"GATE | REQUEST | voice={voice_id} model={MODEL_ID} "
        f"format={OUTPUT_FORMAT} settings={json.dumps(voice_settings, sort_keys=True)}"
    )

    objects = 0
    audio_bytes = 0
    chars: list[str] = []
    starts: list[float] = []
    ends: list[float] = []
    align_kind = None
    unparsed_lines = 0

    async with aiohttp.ClientSession() as session:
        async with session.post(
            url,
            headers={"xi-api-key": api_key, "Content-Type": "application/json"},
            json=body,
            timeout=aiohttp.ClientTimeout(total=60),
        ) as resp:
            if resp.status != 200:
                # Truncated body only — never headers, never the key.
                err = (await resp.text())[:300]
                print(f"GATE | RED | HTTP {resp.status} — {err}")
                return 0

            ctype = resp.headers.get("Content-Type", "?")
            print(f"GATE | RESPONSE | status=200 content_type={ctype}")

            # The production reader iterates lines; mirror it so this gate
            # exercises the same framing assumption (NDJSON) rather than a
            # friendlier parse that could pass where production fails.
            async for raw_line in resp.content:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    unparsed_lines += 1
                    continue
                objects += 1
                b64 = obj.get("audio_base64") or obj.get("audio")
                if b64:
                    audio_bytes += len(base64.b64decode(b64))
                align = obj.get("alignment")
                kind = "alignment"
                if not align:
                    align = obj.get("normalized_alignment")
                    kind = "normalized_alignment"
                if align:
                    align_kind = align_kind or kind
                    chars.extend(align.get("characters") or [])
                    starts.extend(align.get("character_start_times_seconds") or [])
                    ends.extend(align.get("character_end_times_seconds") or [])

    duration = audio_bytes / float(SAMPLE_RATE * 2)
    print(
        f"GATE | STREAM | objects={objects} unparsed_lines={unparsed_lines} "
        f"audio_bytes={audio_bytes} (~{duration:.2f}s) aligned_chars={len(chars)}"
    )

    if unparsed_lines and not objects:
        # The endpoint answered 200 with something that is not NDJSON at
        # all. This is the shape production silently mis-reads, so name it.
        print(
            "GATE | RED | 200 but no parseable NDJSON objects — the "
            "with-timestamps framing is NOT what the reader expects; "
            "keep LILY_VOICE_SYNCED_TRANSCRIPT off"
        )
        return 0

    if not chars or len(starts) != len(chars) or len(ends) != len(chars):
        print(
            f"GATE | RED | no usable per-character alignment for {MODEL_ID} "
            f"(chars={len(chars)} starts={len(starts)} ends={len(ends)}) — "
            "word-level sync is NOT available on this config; keep "
            "LILY_VOICE_SYNCED_TRANSCRIPT off"
        )
        return 0

    monotonic = all(starts[i] <= starts[i + 1] for i in range(len(starts) - 1))
    covered = "".join(chars)
    print(
        f"GATE | GREEN | per-character alignment returned for {MODEL_ID} "
        f"(field={align_kind}, {len(chars)} chars, monotonic={monotonic})"
    )
    print(f"GATE | TEXT_COVERAGE | sent={len(PROBE_TEXT)} aligned={len(covered)}")
    if covered.strip() != PROBE_TEXT.strip():
        # Not a RED: normalization legitimately rewrites text. Surfaced so
        # a human sees that the aligned string is not the sent string.
        print(f"GATE | NOTE | aligned text differs from sent: {covered[:120]!r}")

    print("GATE | SAMPLES | first characters (char, start_s, end_s):")
    for i in range(min(8, len(chars))):
        print(f"  {chars[i]!r:>6}  {starts[i]:7.3f}  {ends[i]:7.3f}")

    # Derived words, the actual product of the alignment: this is what
    # lily_tts._WordTimingAggregator builds and hands the framework.
    words: list[tuple[str, float, float]] = []
    i = 0
    while i < len(chars) and len(words) < 6:
        while i < len(chars) and chars[i].isspace():
            i += 1
        if i >= len(chars):
            break
        j = i
        while j < len(chars) and not chars[j].isspace():
            j += 1
        words.append(("".join(chars[i:j]), starts[i], ends[j - 1]))
        i = j
    print("GATE | SAMPLES | first words (word, start_s, end_s):")
    for w, s, e in words:
        print(f"  {w!r:>14}  {s:7.3f}  {e:7.3f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
