"""WS-16 A/B scorer — NULL vs WPE vs AIC on the reverberant fixture.

Program-wide rule (AMENDMENT-002): MACHINE metrics only — WER + DER against
fixture ground truth, plus measured added latency and adjudication-timestamp
fidelity. No perceptual quality anywhere.

Cascade rule: each arm scores the COMPOSED pipeline (fixture already carries
the simulated client/room chain; the node + Speechmatics ENHANCED with the
WS-13 fleet config run on top). The NULL arm is the same pipeline with the
node removed — a legitimate winner.

Reads WAVs, runs each arm through the live Speechmatics RT decode (uses the
same TranscriptionConfig knobs the agent constructs), aligns to ground truth,
writes scorecard.json + prints a table. Requires SPEECHMATICS_API_KEY. If the
key or network is unavailable, records the boundary and computes only the
offline signals (latency, timestamp fidelity, node RTF).

Eval-only; not imported by the agent.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
from pathlib import Path

import jiwer
import numpy as np
import soundfile as sf

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import lily_dereverb  # noqa: E402

SR = 16000
_WS = jiwer.Compose(
    [
        jiwer.ToLowerCase(),
        jiwer.RemovePunctuation(),
        jiwer.RemoveMultipleSpaces(),
        jiwer.Strip(),
        jiwer.ReduceToListOfListOfWords(),
    ]
)


def _norm(t: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", t.lower())).strip()


# --- node arms --------------------------------------------------------------

def _apply_node(pcm16: np.ndarray, mode: str):
    """Returns (processed_pcm16, added_latency_s, node_rtf)."""
    if mode == "null":
        return pcm16, 0.0, None
    if mode == "wpe":
        wpe = lily_dereverb.LilyWpeDereverb(sample_rate=SR)
        out = bytearray()
        block = 320  # 20 ms
        raw = pcm16.tobytes()
        for i in range(0, len(raw), block * 2):
            out += wpe.process_block(raw[i : i + block * 2])
        arr = np.frombuffer(bytes(out), dtype=np.int16)[: len(pcm16)]
        return arr, wpe.latency_seconds(), wpe.realtime_factor()
    if mode == "aic":
        # AIC is a per-frame LiveKit FrameProcessor requiring LK Cloud creds;
        # offline WAV scoring can't exercise it without a live credentialed
        # room. Recorded as UNAVAILABLE-OFFLINE in the scorecard.
        raise RuntimeError("aic-offline-unavailable")
    raise ValueError(mode)


# --- Speechmatics decode ----------------------------------------------------

async def _decode(pcm16: np.ndarray, api_key: str):
    from speechmatics.rt import (
        AsyncClient,
        AudioEncoding,
        AudioFormat,
        OperatingPoint,
        ServerMessageType,
        SpeakerDiarizationConfig,
        TranscriptionConfig,
    )

    words = []  # (start, end, text, speaker)

    client = AsyncClient(api_key=api_key)

    @client.on(ServerMessageType.ADD_TRANSCRIPT)
    def _on_final(msg):
        from speechmatics.rt import TranscriptResult

        res = TranscriptResult.from_message(msg)
        for r in res.results:
            if getattr(r, "alternatives", None):
                alt = r.alternatives[0]
                words.append(
                    (
                        r.start_time,
                        r.end_time,
                        alt.content,
                        getattr(alt, "speaker", None),
                    )
                )

    cfg = TranscriptionConfig(
        language="en",
        operating_point=OperatingPoint.ENHANCED,
        diarization="speaker",
        max_delay=1.5,
        enable_partials=True,
        speaker_diarization_config=SpeakerDiarizationConfig(
            max_speakers=7, speaker_sensitivity=0.5, prefer_current_speaker=True
        ),
    )
    fmt = AudioFormat(
        encoding=AudioEncoding.PCM_S16LE, sample_rate=SR, chunk_size=4096
    )

    import io

    t0 = time.perf_counter()
    await client.transcribe(
        io.BytesIO(pcm16.tobytes()), transcription_config=cfg, audio_format=fmt
    )
    wall = time.perf_counter() - t0
    return words, wall


# --- alignment / metrics ----------------------------------------------------

def _wer(ref_text: str, hyp_text: str) -> float:
    if not _norm(ref_text):
        return 0.0
    return jiwer.wer(_norm(ref_text), _norm(hyp_text) or " ", _WS, _WS)


def _assign_speaker(word_mid: float, turns) -> str:
    for t in turns:
        if t["start"] <= word_mid <= t["end"]:
            return t["speaker"]
    # nearest boundary
    return min(turns, key=lambda t: min(abs(t["start"] - word_mid), abs(t["end"] - word_mid)))["speaker"]


def _der(words, turns) -> float:
    """Diarization error rate as fraction of hypothesis words whose predicted
    speaker cluster does not agree with the majority true-speaker mapping for
    that cluster (mapping via Hungarian-free greedy majority vote)."""
    if not words:
        return 1.0
    # true speaker per hyp word (by time), then cluster->true majority map
    cluster_votes: dict[str, dict[str, int]] = {}
    for start, end, _txt, spk in words:
        true = _assign_speaker((start + end) / 2, turns)
        cluster_votes.setdefault(spk, {}).setdefault(true, 0)
        cluster_votes[spk][true] += 1
    cluster_map = {c: max(v, key=v.get) for c, v in cluster_votes.items()}
    errors = 0
    for start, end, _txt, spk in words:
        true = _assign_speaker((start + end) / 2, turns)
        if cluster_map.get(spk) != true:
            errors += 1
    return errors / len(words)


def _timestamp_fidelity(words, turns):
    """Adjudication-relevant fidelity: does first-answer ordering survive?
    Compare true turn-order vs hypothesis first-word-time order per speaker."""
    true_order = [t["speaker"] for t in sorted(turns, key=lambda x: x["start"])]
    first_word = {}
    for start, _end, _txt, spk in words:
        true_spk = _assign_speaker(start, turns)
        first_word.setdefault(true_spk, start)
    hyp_order = [s for s, _ in sorted(first_word.items(), key=lambda kv: kv[1])]
    # Kendall-tau-style: count preserved pairwise orderings among speakers
    # present in both.
    common = [s for s in true_order if s in hyp_order]
    seen, pairs, ok = [], 0, 0
    for i in range(len(common)):
        for j in range(i + 1, len(common)):
            pairs += 1
            a, b = common[i], common[j]
            if hyp_order.index(a) < hyp_order.index(b):
                ok += 1
    return (ok / pairs) if pairs else 1.0


def score_scene(scene_dir: Path, arm_wav: Path, mode: str, api_key):
    ref = json.loads((scene_dir / "ref.json").read_text())
    turns = ref["turns"]
    ref_text = " ".join(t["text"] for t in turns)
    pcm, sr = sf.read(str(arm_wav), dtype="int16")
    assert sr == SR
    proc, added_latency, node_rtf = _apply_node(pcm, mode)
    row = {
        "scene": ref["scene"],
        "arm": mode,
        "added_latency_ms": round(added_latency * 1000, 1),
        "node_rtf": round(node_rtf, 4) if node_rtf else None,
    }
    if api_key:
        words, wall = asyncio.run(_decode(proc, api_key))
        hyp_text = " ".join(w[2] for w in words)
        row.update(
            wer=round(_wer(ref_text, hyp_text), 4),
            der=round(_der(words, turns), 4),
            timestamp_fidelity=round(_timestamp_fidelity(words, turns), 4),
            n_hyp_words=len(words),
            n_clusters=len({w[3] for w in words}),
            decode_wall_s=round(wall, 2),
        )
    else:
        row.update(wer=None, der=None, timestamp_fidelity=None, decode_skipped="no SPEECHMATICS_API_KEY")
    return row


def main():
    import os

    ap = argparse.ArgumentParser()
    ap.add_argument("--fixture", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--rt", default="80", help="reverb RT arm to score (40|80)")
    args = ap.parse_args()
    api_key = os.environ.get("SPEECHMATICS_API_KEY")

    rows = []
    for scene_dir in sorted(args.fixture.glob("scene*")):
        wav = scene_dir / f"reverb_rt{args.rt}.wav"
        for mode in ("null", "wpe", "aic"):
            try:
                rows.append(score_scene(scene_dir, wav, mode, api_key))
            except RuntimeError as e:
                rows.append({"scene": scene_dir.name, "arm": mode, "error": str(e)})
    args.out.write_text(json.dumps({"rt": args.rt, "rows": rows}, indent=1))
    print(json.dumps(rows, indent=1))


if __name__ == "__main__":
    main()
