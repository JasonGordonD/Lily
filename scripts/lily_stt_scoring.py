"""
scripts/lily_stt_scoring.py — OFFLINE STT scoring tooling (WS-13 / AMENDMENT-002).

Moved out of lily_stt_tuning.py (REFACTOR Stage 1a item 2): nothing on the
live agent path calls these — they are the machine-metric scorers (WER, DER),
the fixture scorer, the assistant-leak scan and the WS-15 matrix grid used by
tests and the eval/ bake-off scripts. lily_stt_tuning keeps thin lazy shims
under the old names so `from lily_stt_tuning import lily_wer` keeps working.

Scoring (AMENDMENT-002, program-wide): matrix scoring uses machine metrics —
WER and DER against fixture ground truth — never perceptual quality.
"""

from __future__ import annotations

import itertools
import os
import sys
from typing import Any, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lily_stt_tuning import LILY_STT_MATRIX_AXES, LILY_STT_TUNED  # noqa: E402


def lily_matrix_cells() -> list[dict[str, Any]]:
    """The full tuning-matrix grid (cartesian product of the axes)."""
    keys = sorted(LILY_STT_MATRIX_AXES)
    return [
        dict(zip(keys, values))
        for values in itertools.product(*(LILY_STT_MATRIX_AXES[k] for k in keys))
    ]


def lily_wer(reference: str, hypothesis: str) -> float:
    """Word error rate: word-level Levenshtein distance / reference length.
    Empty reference: 0.0 when hypothesis is also empty, else 1.0."""
    ref = (reference or "").split()
    hyp = (hypothesis or "").split()
    if not ref:
        return 0.0 if not hyp else 1.0
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i] + [0] * len(hyp)
        for j, h in enumerate(hyp, 1):
            cur[j] = min(
                prev[j] + 1,
                cur[j - 1] + 1,
                prev[j - 1] + (0 if r == h else 1),
            )
        prev = cur
    return prev[-1] / len(ref)


def _overlap(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


def lily_der(
    reference_segments: list[dict],
    hypothesis_segments: list[dict],
) -> float:
    """Diarization error rate over labeled time segments.

    Segments are {"speaker": str, "start": float, "end": float}. The
    hypothesis-to-reference label mapping is chosen OPTIMALLY (exhaustive
    assignment — label counts here are single digits) to maximize matched
    time; DER = (missed + false-alarm + confusion time) / reference time.
    Single-stream approximation: overlapping reference speech is scored
    per-segment, which matches the fixture's record shape (the recorded
    stream is itself single-attribution per span)."""
    ref_time = sum(max(0.0, s["end"] - s["start"]) for s in reference_segments)
    if ref_time <= 0:
        return 0.0

    ref_labels = sorted({s["speaker"] for s in reference_segments})
    hyp_labels = sorted({s["speaker"] for s in hypothesis_segments})

    # Matched-overlap matrix per (hyp label, ref label) pair.
    pair_overlap: dict[tuple[str, str], float] = {}
    for h in hypothesis_segments:
        for r in reference_segments:
            ov = _overlap(h["start"], h["end"], r["start"], r["end"])
            if ov > 0:
                key = (h["speaker"], r["speaker"])
                pair_overlap[key] = pair_overlap.get(key, 0.0) + ov

    # Optimal injective mapping hyp->ref maximizing matched time.
    best_matched = 0.0
    if hyp_labels and ref_labels:
        smaller, larger, hyp_first = (
            (hyp_labels, ref_labels, True)
            if len(hyp_labels) <= len(ref_labels)
            else (ref_labels, hyp_labels, False)
        )
        for perm in itertools.permutations(larger, len(smaller)):
            matched = 0.0
            for s_label, l_label in zip(smaller, perm):
                key = (s_label, l_label) if hyp_first else (l_label, s_label)
                matched += pair_overlap.get(key, 0.0)
            best_matched = max(best_matched, matched)

    hyp_time = sum(max(0.0, s["end"] - s["start"]) for s in hypothesis_segments)
    total_overlap = 0.0
    for h in hypothesis_segments:
        for r in reference_segments:
            total_overlap += _overlap(h["start"], h["end"], r["start"], r["end"])

    missed = ref_time - total_overlap  # reference time no hypothesis covers
    false_alarm = hyp_time - total_overlap  # hypothesis time outside reference
    confusion = total_overlap - best_matched  # covered but mislabeled
    return max(0.0, missed + false_alarm + confusion) / ref_time


def lily_score_fixture(
    rows: list[dict],
    ground_truth: dict,
    span_quarantine_seconds: Optional[float] = None,
) -> dict[str, Any]:
    """Score one transcript record against the fixture's ground truth.

    `rows`: [{speaker_label, segment_start, segment_end, text}].
    `ground_truth`: {"roster": [names], "label_map": {label: name|null},
    "assistant_label": str}. Labels mapped to null (and labels absent from
    the map) are PHANTOMS. Returns machine metrics only."""
    quarantine = (
        float(span_quarantine_seconds)
        if span_quarantine_seconds is not None
        else float(LILY_STT_TUNED["ws10_span_quarantine_seconds"])
    )
    label_map: dict = ground_truth.get("label_map") or {}
    assistant_label = ground_truth.get("assistant_label")
    roster = list(ground_truth.get("roster") or [])

    user_rows = [r for r in rows if r.get("speaker_label") != assistant_label]
    labels = {r["speaker_label"] for r in user_rows}
    phantom_labels = sorted(
        l for l in labels if label_map.get(l) is None
    )
    mapped_players = {label_map[l] for l in labels if label_map.get(l)}
    # A player split across N labels contributes N-1 continuity errors.
    label_splits = sum(
        max(0, n - 1)
        for n in (
            sum(1 for l in labels if label_map.get(l) == p)
            for p in mapped_players
        )
    )
    attributed_rows = sum(1 for r in user_rows if label_map.get(r["speaker_label"]))
    span_violations = [
        {
            "speaker_label": r["speaker_label"],
            "span_seconds": round(r["segment_end"] - r["segment_start"], 2),
            "text_chars": len(r.get("text") or ""),
        }
        for r in user_rows
        if (r["segment_end"] - r["segment_start"]) > quarantine
    ]
    return {
        "rows": len(user_rows),
        "phantom_label_count": len(phantom_labels),
        "phantom_labels": phantom_labels,
        "label_continuity_splits": label_splits,
        "attribution_accuracy": (
            attributed_rows / len(user_rows) if user_rows else 1.0
        ),
        "players_covered": len(mapped_players),
        "roster_size": len(roster),
        "span_quarantine_seconds": quarantine,
        "span_violations": span_violations,
    }


def lily_assistant_leak_scan(
    rows: list[dict],
    assistant_label: str,
    min_words: int = 8,
) -> list[dict]:
    """Playback-path regression check: find assistant speech leaking into
    user-attributed rows. Flags any user row whose normalized text contains
    a >= `min_words` word run from any assistant row. Empty result =
    playback path clean. The default run length is calibrated on the
    evidence session: players legitimately REPEAT short assistant phrases
    (answers — "The Wizard of Oz" — and listed category names), which are
    conversation, not echo; acoustic playback leak transcribes long
    verbatim runs of Lily's sentences."""
    def _norm(t: str) -> list[str]:
        return "".join(
            ch.lower() if ch.isalnum() or ch.isspace() else " "
            for ch in (t or "")
        ).split()

    assistant_runs: set[tuple[str, ...]] = set()
    for r in rows:
        if r.get("speaker_label") != assistant_label:
            continue
        words = _norm(r.get("text") or "")
        for i in range(len(words) - min_words + 1):
            assistant_runs.add(tuple(words[i : i + min_words]))

    leaks = []
    for r in rows:
        if r.get("speaker_label") == assistant_label:
            continue
        words = _norm(r.get("text") or "")
        for i in range(len(words) - min_words + 1):
            if tuple(words[i : i + min_words]) in assistant_runs:
                leaks.append(
                    {
                        "speaker_label": r.get("speaker_label"),
                        "text": (r.get("text") or "")[:120],
                    }
                )
                break
    return leaks
