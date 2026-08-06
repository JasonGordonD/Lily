"""WS-15 bake-off scorer — incumbent (Speechmatics tuned) vs challenger
(pyannote Live-1), same fixture, same ground truth, same metric code.

Shared metric set (WS-13): lily_wer, lily_der from lily_stt_tuning. Game-level
metrics (WS-15): phantom count, answer-attribution accuracy, dropped-answer
rate, first-answer timestamp fidelity from game_metrics.

Both arms decode the SAME Speechmatics STT words. The incumbent keeps
Speechmatics' own speaker labels; the challenger replaces them with a pyannote
Live-1 diarization stream via game_metrics.word_align — isolating the
diarization difference (WER is therefore identical across arms by construction
and is reported once as the shared-STT floor).

Eval-only; not imported by the agent. Requires SPEECHMATICS_API_KEY for the
incumbent; PYANNOTEAI_API_KEY for the challenger (absent -> recorded UNAVAILABLE,
never fabricated).

Usage:
  python score_bakeoff.py --fixture <dir> --wav-name shout.wav --out card.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root
from lily_stt_tuning import lily_der, lily_wer  # noqa: E402

from game_metrics import (  # noqa: E402
    all_game_metrics,
    turns_to_segments,
    word_align,
    words_to_segments,
)
from diar_providers import (  # noqa: E402
    ChallengerUnavailable,
    PyannoteLive1Challenger,
    SpeechmaticsIncumbent,
)


def _read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        assert w.getframerate() == 16000 and w.getsampwidth() == 2
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


def _norm(text: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace — LibriSpeech ground
    truth is uppercase/unpunctuated, Speechmatics is mixed-case/punctuated, so
    lily_wer (a raw token scorer) needs both sides normalized to be meaningful."""
    return " ".join(
        "".join(c if c.isalnum() or c.isspace() else " " for c in text).lower().split()
    )


def _score_arm(words, turns) -> dict:
    ref_text = _norm(" ".join(t["text"] for t in turns))
    hyp_text = _norm(" ".join(w[2] for w in words))
    return {
        "wer": round(lily_wer(ref_text, hyp_text), 4),
        "der": round(lily_der(turns_to_segments(turns), words_to_segments(words)), 4),
        "n_hyp_words": len(words),
        "n_clusters": len({w[3] for w in words}),
        **all_game_metrics(words, turns),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixture", required=True, type=Path)
    ap.add_argument("--wav-name", default="shout.wav")
    ap.add_argument("--tuned", type=Path, default=None,
                    help="stt_tuned.json (WS-13 incumbent config)")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--all-turns-answers", action="store_true",
                    help="treat every turn as an answer (for turn-taking "
                         "fixtures that carry no is_answer flag, e.g. WS-16)")
    args = ap.parse_args()

    tuned = json.loads(args.tuned.read_text()) if args.tuned else {}
    incumbent = SpeechmaticsIncumbent(tuned=tuned)
    challenger = PyannoteLive1Challenger()

    scenes = []
    for scene_dir in sorted(args.fixture.glob("scene*")):
        wav = scene_dir / args.wav_name
        if not wav.exists():
            continue
        ref = json.loads((scene_dir / "ref.json").read_text())
        turns = ref["turns"]
        if args.all_turns_answers:
            turns = [{**t, "is_answer": t.get("is_answer", True)} for t in turns]
        pcm = _read_wav(wav)
        row: dict = {"scene": ref["scene"], "kind": ref.get("kind", "reverb")}

        # Incumbent arm (shared STT + Speechmatics labels).
        try:
            words = asyncio.run(incumbent.decode(pcm))
            row["incumbent"] = _score_arm(words, turns)
        except ChallengerUnavailable as e:
            row["incumbent"] = {"unavailable": str(e)}
            words = []

        # Challenger arm (same STT words, pyannote Live-1 labels via word_align).
        try:
            diar = asyncio.run(challenger.diarize(pcm))
            relabeled = word_align(words, diar)
            row["challenger"] = _score_arm(relabeled, turns)
        except ChallengerUnavailable as e:
            row["challenger"] = {"unavailable": str(e)}

        scenes.append(row)
        print(json.dumps(row, indent=1))

    args.out.write_text(json.dumps({"fixture": str(args.fixture), "scenes": scenes}, indent=1))


if __name__ == "__main__":
    main()
