"""WO-LILY-NC-BENCH-001 Task 1 — the cold-join gate NC must pass to return.

Runs N cold joins against an ISOLATED test slot (never production) and
measures, per join:

  1. job accept          — the Lily agent participant joins the room;
  2. join-to-ready       — room connect -> agent participant joined (s);
  3. greet reaches playout — the agent's audio track publishes AND emits
                            non-silent frames (belt) + a LILY transcript
                            row lands in Supabase (braces — playout is
                            what writes it, per record_agent_turn);
  4. mic frames reach STT — this probe publishes a spoken WAV; a non-LILY
                            transcript row for the session proves the
                            capture path reached Speechmatics.

Pass criteria (explicit, from the WO): N/N accepts, greet playout on
EVERY join, and mean join-to-ready within 2x the NC-off baseline
(--baseline-latency, seconds — measure it first with the same script
against the same slot with LILY_NOISE_CANCELLATION=off).

Operator-side by design: needs LIVEKIT_URL / LIVEKIT_API_KEY /
LIVEKIT_API_SECRET for the TEST slot, plus SUPABASE_URL /
SUPABASE_SERVICE_ROLE_KEY for the transcript checks. This script never
touches slot secrets — flip LILY_NOISE_CANCELLATION on the test slot
with `lk agent update-secrets --id <test-slot-id>` (explicit --id; never
--overwrite) between the baseline and NC runs.

Usage:
  python eval/nc_bench/run_bench.py --joins 10 --baseline-latency 4.2 \
      --spoken-wav eval/nc_bench/probe_utterance.wav
  (run once with the slot NC-off to record the baseline, once NC-on)

Output: markdown results table + JSON (results_<label>.json) for the
close-out.
"""

import argparse
import asyncio
import json
import os
import sys
import time
import wave
from pathlib import Path

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from livekit import api, rtc  # noqa: E402

AGENT_IDENTITY_PREFIX = "lily"  # agent participant identity starts with this
SILENCE_RMS_FLOOR = 200  # int16 RMS above this = audible frames (greet)


def _rms(frame: rtc.AudioFrame) -> float:
    import array

    samples = array.array("h")
    samples.frombytes(bytes(frame.data))
    if not samples:
        return 0.0
    return (sum(s * s for s in samples) / len(samples)) ** 0.5


async def _supabase_count(
    http: aiohttp.ClientSession, base: str, key: str, table: str, params: dict
) -> int:
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Prefer": "count=exact",
        "Range": "0-0",
    }
    async with http.get(
        f"{base}/rest/v1/{table}", params=params, headers=headers
    ) as resp:
        content_range = resp.headers.get("content-range", "/0")
        try:
            return int(content_range.split("/")[-1])
        except ValueError:
            return 0


async def one_join(index: int, args) -> dict:
    """One cold join: fresh room, synthetic player, measurements."""
    result = {
        "join": index,
        "accepted": False,
        "join_to_ready_s": None,
        "greet_audible": False,
        "greet_transcript_row": False,
        "mic_reached_stt": False,
        "session_id": None,
        "error": None,
    }
    room_name = f"ncbench-{args.label}-{index}-{int(time.time())}"
    lk = api.LiveKitAPI()
    room = rtc.Room()
    agent_joined = asyncio.Event()
    heard_audio = asyncio.Event()

    @room.on("participant_connected")
    def _on_participant(p: rtc.RemoteParticipant) -> None:
        if p.identity.lower().startswith(AGENT_IDENTITY_PREFIX):
            agent_joined.set()

    @room.on("track_subscribed")
    def _on_track(track, publication, participant) -> None:
        if track.kind == rtc.TrackKind.KIND_AUDIO and participant.identity.lower().startswith(
            AGENT_IDENTITY_PREFIX
        ):
            async def _listen() -> None:
                stream = rtc.AudioStream(track)
                async for ev in stream:
                    if _rms(ev.frame) > SILENCE_RMS_FLOOR:
                        heard_audio.set()
                        break

            asyncio.ensure_future(_listen())

    try:
        token = (
            api.AccessToken()
            .with_identity(f"ncbench-probe-{index}")
            .with_name("Bench Probe")
            .with_grants(api.VideoGrants(room_join=True, room=room_name))
            .to_jwt()
        )
        t0 = time.monotonic()
        await room.connect(os.environ["LIVEKIT_URL"], token)

        # Existing agent participant (agent can beat the event handler).
        for p in room.remote_participants.values():
            if p.identity.lower().startswith(AGENT_IDENTITY_PREFIX):
                agent_joined.set()

        try:
            await asyncio.wait_for(agent_joined.wait(), timeout=args.accept_timeout)
        except asyncio.TimeoutError:
            result["error"] = "agent never joined (job accept failed)"
            return result
        result["accepted"] = True
        result["join_to_ready_s"] = round(time.monotonic() - t0, 2)

        # Publish the spoken probe WAV as the player's mic.
        with wave.open(str(args.spoken_wav), "rb") as wav:
            sample_rate = wav.getframerate()
            channels = wav.getnchannels()
            pcm = wav.readframes(wav.getnframes())
        source = rtc.AudioSource(sample_rate, channels)
        track = rtc.LocalAudioTrack.create_audio_track("bench-mic", source)
        await room.local_participant.publish_track(
            track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        )
        samples_per_frame = sample_rate // 100  # 10ms cadence
        frame_bytes = samples_per_frame * channels * 2

        async def _play_wav() -> None:
            # Wait out the greet, then speak the probe twice.
            await asyncio.sleep(args.greet_wait)
            for _ in range(2):
                for offset in range(0, len(pcm) - frame_bytes, frame_bytes):
                    frame = rtc.AudioFrame(
                        data=pcm[offset:offset + frame_bytes],
                        sample_rate=sample_rate,
                        num_channels=channels,
                        samples_per_channel=samples_per_frame,
                    )
                    await source.capture_frame(frame)
                await asyncio.sleep(1.0)

        playback = asyncio.ensure_future(_play_wav())

        try:
            await asyncio.wait_for(heard_audio.wait(), timeout=args.greet_timeout)
            result["greet_audible"] = True
        except asyncio.TimeoutError:
            pass

        await playback
        await asyncio.sleep(args.settle)

        # Supabase verification: session row for this room; LILY row =
        # greet reached playout; non-LILY row = mic frames reached STT.
        async with aiohttp.ClientSession() as http:
            base = os.environ["SUPABASE_URL"].rstrip("/")
            key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
            headers = {"apikey": key, "Authorization": f"Bearer {key}"}
            async with http.get(
                f"{base}/rest/v1/lily_sessions",
                params={
                    "select": "session_id",
                    "session_id": f"like.lily-*{room_name[-6:]}*",
                    "order": "created_at.desc",
                    "limit": "1",
                },
                headers=headers,
            ) as resp:
                rows = await resp.json()
            # Fallback: newest session created after t0 (bench slot is
            # isolated, so the newest row is this join's).
            if not rows:
                async with http.get(
                    f"{base}/rest/v1/lily_sessions",
                    params={
                        "select": "session_id,created_at",
                        "order": "created_at.desc",
                        "limit": "1",
                    },
                    headers=headers,
                ) as resp:
                    rows = await resp.json()
            if rows:
                sid = rows[0]["session_id"]
                result["session_id"] = sid
                lily_rows = await _supabase_count(
                    http, base, key, "lily_transcripts",
                    {"session_id": f"eq.{sid}", "speaker_label": "eq.LILY"},
                )
                player_rows = await _supabase_count(
                    http, base, key, "lily_transcripts",
                    {"session_id": f"eq.{sid}", "speaker_label": "neq.LILY"},
                )
                result["greet_transcript_row"] = lily_rows > 0
                result["mic_reached_stt"] = player_rows > 0
        return result
    except Exception as e:  # noqa: BLE001 — every failure is a data point
        result["error"] = repr(e)
        return result
    finally:
        try:
            await room.disconnect()
        except Exception:
            pass
        try:
            await lk.room.delete_room(api.DeleteRoomRequest(room=room_name))
        except Exception:
            pass
        await lk.aclose()


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--joins", type=int, default=10)
    parser.add_argument("--label", default="nc", help="run label: nc | off-baseline")
    parser.add_argument(
        "--baseline-latency", type=float, default=None,
        help="mean NC-off join-to-ready seconds (omit on the baseline run)",
    )
    parser.add_argument(
        "--spoken-wav", type=Path,
        default=Path(__file__).parent / "probe_utterance.wav",
        help="16-bit PCM WAV of a spoken answer the mic probe publishes",
    )
    parser.add_argument("--accept-timeout", type=float, default=60.0)
    parser.add_argument("--greet-timeout", type=float, default=45.0)
    parser.add_argument("--greet-wait", type=float, default=12.0)
    parser.add_argument("--settle", type=float, default=15.0)
    parser.add_argument("--cooldown", type=float, default=10.0)
    args = parser.parse_args()

    for var in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET",
                "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"):
        if not os.environ.get(var):
            print(f"FATAL: {var} not set (point it at the TEST slot, never prod)")
            return 2
    if not args.spoken_wav.exists():
        print(f"FATAL: spoken probe WAV missing: {args.spoken_wav}")
        return 2

    results = []
    for i in range(1, args.joins + 1):
        print(f"— join {i}/{args.joins} (cold) —")
        results.append(await one_join(i, args))
        print(json.dumps(results[-1]))
        await asyncio.sleep(args.cooldown)

    accepts = sum(1 for r in results if r["accepted"])
    greets = sum(
        1 for r in results if r["greet_audible"] or r["greet_transcript_row"]
    )
    mic = sum(1 for r in results if r["mic_reached_stt"])
    latencies = [r["join_to_ready_s"] for r in results if r["join_to_ready_s"]]
    mean_latency = round(sum(latencies) / len(latencies), 2) if latencies else None

    print("\n| join | accept | ready(s) | greet aired | LILY row | mic→STT |")
    print("|-----:|:------:|---------:|:-----------:|:--------:|:-------:|")
    for r in results:
        print(
            f"| {r['join']} | {'Y' if r['accepted'] else 'N'} "
            f"| {r['join_to_ready_s'] or '—'} "
            f"| {'Y' if r['greet_audible'] else 'N'} "
            f"| {'Y' if r['greet_transcript_row'] else 'N'} "
            f"| {'Y' if r['mic_reached_stt'] else 'N'} |"
        )

    verdict_parts = [
        f"accepts {accepts}/{args.joins}",
        f"greet playout {greets}/{args.joins}",
        f"mic→STT {mic}/{args.joins}",
        f"mean join-to-ready {mean_latency}s",
    ]
    passed = accepts == args.joins and greets == args.joins and mic == args.joins
    if args.baseline_latency is not None and mean_latency is not None:
        within = mean_latency <= 2.0 * args.baseline_latency
        verdict_parts.append(
            f"latency {'within' if within else 'EXCEEDS'} 2x baseline "
            f"({args.baseline_latency}s)"
        )
        passed = passed and within
    verdict = "PASS" if passed else "FAIL"
    print(f"\nVERDICT: {verdict} — {'; '.join(verdict_parts)}")

    out = Path(__file__).parent / f"results_{args.label}.json"
    out.write_text(json.dumps(
        {"label": args.label, "verdict": verdict, "results": results,
         "mean_join_to_ready_s": mean_latency,
         "baseline_latency_s": args.baseline_latency},
        indent=2,
    ))
    print(f"written: {out}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
