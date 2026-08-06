"""WS-15 shout-over-chatter fixture builder.

WS-16 built reverberant round-robin turn-taking scenes; the evidence session's
actual failure shape is different — an answer SHOUTED over simultaneous table
CHATTER (cross-talk), which is where Speechmatics dropped answers and near-zeroed
one player's attribution. This builder constructs that overlap case: a foreground
"answer" utterance at full gain with 1-2 background speakers talking through it
at reduced gain, then a light synthetic echo-room reverb + noise floor.

Ground truth (words, turn spans, speaker, is_answer) is exact by construction.

Named boundary (identical to WS-13/WS-16): this is NOT the session's room,
speakers, device, or client processing chain. No session audio is recoverable
(no recorder wired; DB is text-only). It is a faithful *class* of the failure,
suitable for a relative A/B, with honest ground truth — not the session.

Deps: numpy + stdlib wave + ffmpeg (flac decode). No torch / pyroomacoustics /
soundfile. Eval-only; not imported by the agent.

Usage:
  python build_shout_fixture.py --libri <LibriSpeech/dev-clean-2> --out <dir>
"""

from __future__ import annotations

import argparse
import json
import random
import subprocess
import wave
from pathlib import Path

import numpy as np

SR = 16000
SCENES = 3
SPEAKERS_PER_SCENE = 4
ANSWERS_PER_SCENE = 5
# Per-answer background chatter gain (linear). Later answers get louder chatter
# to force the missed-speech / dropped-answer failure the session showed.
BG_GAIN_DB = [-12.0, -10.0, -8.0, -6.0, -4.0]
UTT_LEN_RANGE_S = (2.0, 7.0)
NOISE_SNR_DB = 30.0
REVERB_RT60_S = 0.5  # synthetic exp-decay IR (echo room)


def _decode_flac(flac: Path) -> np.ndarray:
    """flac -> mono 16 kHz int16 via ffmpeg (no soundfile dependency)."""
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(flac), "-ar", str(SR),
         "-ac", "1", "-f", "s16le", "-"],
        capture_output=True, check=True,
    ).stdout
    return np.frombuffer(out, dtype=np.int16).astype(np.float64) / 32768.0


def load_utterances(libri: Path) -> dict[str, list[tuple[Path, str, float]]]:
    by_speaker: dict[str, list[tuple[Path, str, float]]] = {}
    for trans in sorted(libri.rglob("*.trans.txt")):
        spk = trans.parts[-3]
        for line in trans.read_text().splitlines():
            utt_id, text = line.split(" ", 1)
            flac = trans.parent / f"{utt_id}.flac"
            if not flac.exists():
                continue
            # frames from the flac header is cheap via ffprobe-free heuristic:
            # decode length is only needed for gating; decode once lazily below.
            by_speaker.setdefault(spk, []).append((flac, text.strip(), 0.0))
    return by_speaker


def _synthetic_reverb(x: np.ndarray, rt60: float, rng: np.random.Generator) -> np.ndarray:
    """Convolve with a short exponentially-decaying noise IR — models an echo
    room's late tail without pyroomacoustics. RT60 sets the decay constant."""
    ir_len = int(rt60 * SR)
    t = np.arange(ir_len) / SR
    decay = np.exp(-6.908 * t / rt60)  # -60 dB at t = rt60
    ir = rng.standard_normal(ir_len) * decay
    ir[0] += 1.0  # direct path
    ir /= np.sqrt(np.sum(ir ** 2))
    wet = np.convolve(x, ir)[: len(x)]
    return wet


def build_scene(scene_idx: int, speakers: list[str], pool: dict, master: dict,
                rng: random.Random, np_rng: np.random.Generator):
    """Foreground answers with overlapping attenuated background chatter."""
    turns: list[dict] = []
    tracks: dict[str, list[tuple[int, np.ndarray]]] = {s: [] for s in speakers}
    cursor = 0.0

    def take(spk: str) -> tuple[np.ndarray, str]:
        for _ in range(64):
            if not pool[spk]:
                pool[spk] = list(master[spk])  # refill; reuse allowed if depleted
            flac, text, _ = pool[spk].pop(rng.randrange(len(pool[spk])))
            audio = _decode_flac(flac)
            dur = len(audio) / SR
            if UTT_LEN_RANGE_S[0] <= dur <= UTT_LEN_RANGE_S[1]:
                return audio, text
        return audio, text  # last decoded, if none hit the duration window

    order = list(speakers)
    for a_i in range(ANSWERS_PER_SCENE):
        fg = order[a_i % len(order)]
        fg_audio, fg_text = take(fg)
        fg_dur = len(fg_audio) / SR
        fg_start = cursor
        i0 = int(fg_start * SR)
        tracks[fg].append((i0, fg_audio))
        turns.append({"speaker": fg, "start": round(fg_start, 3),
                      "end": round(fg_start + fg_dur, 3), "text": fg_text,
                      "is_answer": True})

        # 1-2 background chatterers overlapping the foreground answer.
        bg_gain = 10 ** (BG_GAIN_DB[a_i % len(BG_GAIN_DB)] / 20.0)
        bgs = [s for s in speakers if s != fg]
        rng.shuffle(bgs)
        for bg in bgs[: rng.choice([1, 2])]:
            bg_audio, bg_text = take(bg)
            bg_dur = len(bg_audio) / SR
            # start the chatter partway into the answer so it overlaps.
            bg_start = fg_start + rng.uniform(0.2, max(0.3, fg_dur * 0.4))
            j0 = int(bg_start * SR)
            tracks[bg].append((j0, bg_audio * bg_gain))
            turns.append({"speaker": bg, "start": round(bg_start, 3),
                          "end": round(bg_start + bg_dur, 3), "text": bg_text,
                          "is_answer": False})

        cursor = fg_start + fg_dur + rng.uniform(0.4, 1.0)

    total = int(np.ceil(cursor * SR)) + SR
    mix = np.zeros(total)
    for placements in tracks.values():
        for i0, seg in placements:
            mix[i0 : i0 + len(seg)] += seg[: total - i0]

    wet = _synthetic_reverb(mix, REVERB_RT60_S, np_rng)
    speech_rms = np.sqrt(np.mean(wet[np.abs(wet) > 1e-6] ** 2))
    noise = np_rng.normal(0, speech_rms / (10 ** (NOISE_SNR_DB / 20)), len(wet))
    wet = wet + noise
    wet = wet / max(1e-9, np.max(np.abs(wet))) * 0.7

    ref = {
        "scene": scene_idx,
        "sample_rate": SR,
        "kind": "shout-over-chatter",
        "speakers": speakers,
        "turns": sorted(turns, key=lambda t: t["start"]),
    }
    return wet, ref


def _write_wav(path: Path, x: np.ndarray) -> None:
    pcm = np.clip(x * 32768.0, -32768, 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--libri", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--seed", type=int, default=15)
    args = ap.parse_args()
    rng = random.Random(args.seed)
    np_rng = np.random.default_rng(args.seed)

    by_speaker = load_utterances(args.libri)
    speakers = sorted(s for s, u in by_speaker.items() if len(u) >= 6)
    rng.shuffle(speakers)
    need = SCENES * SPEAKERS_PER_SCENE
    assert len(speakers) >= need, f"only {len(speakers)} eligible speakers"
    chosen = speakers[:need]
    pool = {s: list(by_speaker[s]) for s in chosen}
    args.out.mkdir(parents=True, exist_ok=True)

    for scene_idx in range(SCENES):
        scene_speakers = chosen[scene_idx * SPEAKERS_PER_SCENE:
                                (scene_idx + 1) * SPEAKERS_PER_SCENE]
        wet, ref = build_scene(scene_idx, scene_speakers, pool, by_speaker, rng, np_rng)
        scene_dir = args.out / f"scene{scene_idx}"
        scene_dir.mkdir(exist_ok=True)
        _write_wav(scene_dir / "shout.wav", wet)
        (scene_dir / "ref.json").write_text(json.dumps(ref, indent=1))
        n_ans = sum(1 for t in ref["turns"] if t["is_answer"])
        print(f"scene{scene_idx}: {ref['turns'][-1]['end']:.1f}s, "
              f"{len(scene_speakers)} spk, {n_ans} answers")


if __name__ == "__main__":
    main()
