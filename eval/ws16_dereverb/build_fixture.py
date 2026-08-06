"""WS-16 fixture builder — constructed reverberant multi-speaker scenes.

The evidence session's audio (lily-81BCB0-583a0f16) is unrecoverable: the
repo wires no egress/recording and the DB holds text transcripts only. This
builder constructs the fixture instead — real human speech (mini
LibriSpeech dev-clean-2) composed into 4-speaker shared-mic scenes and
convolved with simulated room impulse responses. Ground truth (words, turn
boundaries, speakers) is exact by construction; the named boundary is that
this is not the session's room, speakers, or client processing chain.

Eval-only deps (NOT in requirements.txt): pyroomacoustics, soundfile, numpy.

Usage:
  python build_fixture.py --libri <LibriSpeech/dev-clean-2 dir> --out <fixture dir>

Outputs per scene: clean.wav, reverb_rt<NN>.wav (+ ref.json ground truth).
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

import numpy as np
import pyroomacoustics as pra
import soundfile as sf

SR = 16000
SCENES = 3
SPEAKERS_PER_SCENE = 4
TURNS_PER_SPEAKER = 3
GAP_RANGE_S = (0.3, 0.8)
UTT_LEN_RANGE_S = (2.0, 9.0)
RT60S = [0.4, 0.8]
ROOM_DIM = [6.0, 5.0, 2.7]
MIC_POS = [3.0, 2.5, 1.0]  # shared device on the table
NOISE_SNR_DB = 30.0


def load_utterances(libri: Path):
    by_speaker: dict[str, list[tuple[Path, str]]] = {}
    for trans in sorted(libri.rglob("*.trans.txt")):
        spk = trans.parts[-3]
        for line in trans.read_text().splitlines():
            utt_id, text = line.split(" ", 1)
            flac = trans.parent / f"{utt_id}.flac"
            if flac.exists():
                by_speaker.setdefault(spk, []).append((flac, text.strip()))
    return by_speaker


def pick(by_speaker, rng):
    eligible = {}
    for spk, utts in by_speaker.items():
        good = []
        for flac, text in utts:
            info = sf.info(str(flac))
            dur = info.frames / info.samplerate
            if UTT_LEN_RANGE_S[0] <= dur <= UTT_LEN_RANGE_S[1]:
                good.append((flac, text, dur))
        if len(good) >= TURNS_PER_SPEAKER:
            eligible[spk] = good
    speakers = sorted(eligible)
    rng.shuffle(speakers)
    needed = SCENES * SPEAKERS_PER_SCENE
    assert len(speakers) >= needed, f"only {len(speakers)} eligible speakers"
    return {s: eligible[s] for s in speakers[:needed]}


def build_scene(scene_idx, speakers, pool, rng):
    """Round-robin turns; returns per-speaker dry tracks + ground truth."""
    turns = []
    cursor = 0.0
    order = list(speakers)
    for round_i in range(TURNS_PER_SPEAKER):
        rng.shuffle(order)
        for spk in order:
            flac, text, dur = pool[spk].pop(rng.randrange(len(pool[spk])))
            audio, sr = sf.read(str(flac), dtype="float64")
            assert sr == SR
            turns.append(
                {
                    "speaker": spk,
                    "start": round(cursor, 3),
                    "end": round(cursor + dur, 3),
                    "text": text,
                    "samples": audio,
                }
            )
            cursor += dur + rng.uniform(*GAP_RANGE_S)
    total = int(np.ceil(cursor * SR)) + SR
    tracks = {s: np.zeros(total) for s in speakers}
    for t in turns:
        i0 = int(t["start"] * SR)
        tracks[t["speaker"]][i0 : i0 + len(t["samples"])] += t["samples"]
    ref = {
        "scene": scene_idx,
        "sample_rate": SR,
        "speakers": list(speakers),
        "turns": [
            {k: t[k] for k in ("speaker", "start", "end", "text")} for t in turns
        ],
    }
    return tracks, ref


def reverberate(tracks, rt60, rng):
    """Each speaker speaks from a fixed seat; one shared mic. Summed at mic."""
    e_absorption, max_order = pra.inverse_sabine(rt60, ROOM_DIM)
    out = None
    for i, (spk, dry) in enumerate(sorted(tracks.items())):
        room = pra.ShoeBox(
            ROOM_DIM,
            fs=SR,
            materials=pra.Material(e_absorption),
            max_order=max_order,
        )
        angle = 2 * np.pi * i / len(tracks) + rng.uniform(-0.3, 0.3)
        radius = rng.uniform(1.2, 2.5)
        src = [
            min(max(MIC_POS[0] + radius * np.cos(angle), 0.3), ROOM_DIM[0] - 0.3),
            min(max(MIC_POS[1] + radius * np.sin(angle), 0.3), ROOM_DIM[1] - 0.3),
            1.2,
        ]
        room.add_source(src, signal=dry)
        room.add_microphone(MIC_POS)
        room.simulate()
        sig = room.mic_array.signals[0]
        out = sig if out is None else out[: len(sig)] + sig[: len(out)]
    # mild sensor-noise floor for a realistic decode (documented in memo)
    speech_rms = np.sqrt(np.mean(out[np.abs(out) > 1e-6] ** 2))
    noise = rng.normal(0, speech_rms / (10 ** (NOISE_SNR_DB / 20)), len(out))
    out = out + noise
    return out / max(1e-9, np.max(np.abs(out))) * 0.7


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--libri", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--seed", type=int, default=16)
    args = ap.parse_args()
    rng = random.Random(args.seed)
    np_rng = np.random.default_rng(args.seed)

    by_speaker = load_utterances(args.libri)
    pool = pick(by_speaker, rng)
    all_speakers = list(pool)
    args.out.mkdir(parents=True, exist_ok=True)

    for scene_idx in range(SCENES):
        speakers = all_speakers[
            scene_idx * SPEAKERS_PER_SCENE : (scene_idx + 1) * SPEAKERS_PER_SCENE
        ]
        tracks, ref = build_scene(scene_idx, speakers, pool, rng)
        scene_dir = args.out / f"scene{scene_idx}"
        scene_dir.mkdir(exist_ok=True)
        clean = sum(tracks.values())
        clean = clean / max(1e-9, np.max(np.abs(clean))) * 0.7
        sf.write(scene_dir / "clean.wav", clean, SR, subtype="PCM_16")
        for rt60 in RT60S:
            wet = reverberate(tracks, rt60, np_rng)
            sf.write(
                scene_dir / f"reverb_rt{int(rt60 * 100):02d}.wav",
                wet,
                SR,
                subtype="PCM_16",
            )
        (scene_dir / "ref.json").write_text(json.dumps(ref, indent=1))
        print(f"scene{scene_idx}: {ref['turns'][-1]['end']:.1f}s, {speakers}")


if __name__ == "__main__":
    main()
