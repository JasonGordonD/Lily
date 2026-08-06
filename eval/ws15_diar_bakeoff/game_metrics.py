"""Game-level diarization metrics + STT-word/diarization-stream alignment.

Pure-python, no vendor deps — imported by the eval scorer AND by the test
suite. Everything here operates on already-decoded hypothesis words and the
fixture's exact ground-truth turns, so both bake-off arms are scored by the
identical code path.

Types
-----
Word:    (start: float, end: float, text: str, label: str)
RefTurn: {"speaker": str, "start": float, "end": float,
          "text": str, "is_answer": bool}

The four game-level metrics (AMENDMENT-002) are the ones that decide trivia
adjudication, not perceptual quality:
  - phantom_label_count       — extra speaker labels beyond the real roster
  - answer_attribution_accuracy — was each answer turn credited to the right player
  - dropped_answer_rate       — fraction of answers the diarizer lost (missed speech)
  - first_answer_timestamp_fidelity — how faithfully the first answer's onset survives
"""

from __future__ import annotations

from typing import Iterable, Optional


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------

def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def _dominant_true(start: float, end: float, turns: list[dict]) -> Optional[str]:
    """Ground-truth speaker whose turn overlaps [start, end] the most. With
    cross-talk (overlapping turns) a word can touch several turns; the maximum-
    overlap speaker wins, ties broken toward the SHORTER (more specific, usually
    the answer) turn. Nearest turn if nothing overlaps (inter-turn gaps)."""
    if not turns:
        return None
    best, best_ov, best_span = None, 0.0, float("inf")
    for tn in turns:
        ov = _overlap(start, end, tn["start"], tn["end"])
        span = tn["end"] - tn["start"]
        if ov > best_ov or (ov == best_ov and ov > 0 and span < best_span):
            best, best_ov, best_span = tn["speaker"], ov, span
    if best is not None:
        return best
    mid = (start + end) / 2.0
    return min(
        turns,
        key=lambda tn: min(abs(tn["start"] - mid), abs(tn["end"] - mid)),
    )["speaker"]


def cluster_true_map(words: list[tuple], turns: list[dict]) -> dict[str, str]:
    """Map each hypothesis speaker label to the ground-truth speaker it overlaps
    the most (max-overlap majority — robust to overlapping cross-talk turns)."""
    votes: dict[str, dict[str, float]] = {}
    for start, end, _txt, label in words:
        true = _dominant_true(start, end, turns)
        if true is None:
            continue
        votes.setdefault(label, {}).setdefault(true, 0.0)
        votes[label][true] += max(1e-3, end - start)
    return {lab: max(v, key=v.get) for lab, v in votes.items()}


# ---------------------------------------------------------------------------
# Segment adapters (for the shared WS-13 lily_der time-based scorer)
# ---------------------------------------------------------------------------

def words_to_segments(words: list[tuple]) -> list[dict]:
    """Collapse consecutive same-label words into speaker segments so the
    word-stream can be scored by the WS-13 time-based DER."""
    segs: list[dict] = []
    for start, end, _txt, label in words:
        if segs and segs[-1]["speaker"] == label and start <= segs[-1]["end"] + 0.75:
            segs[-1]["end"] = max(segs[-1]["end"], end)
        else:
            segs.append({"speaker": label, "start": start, "end": end})
    return segs


def turns_to_segments(turns: list[dict]) -> list[dict]:
    return [
        {"speaker": t["speaker"], "start": t["start"], "end": t["end"]}
        for t in turns
    ]


# ---------------------------------------------------------------------------
# The four game-level metrics
# ---------------------------------------------------------------------------

def phantom_label_count(words: list[tuple], turns: list[dict]) -> dict:
    """A phantom label is an EXCESS hypothesis speaker cluster beyond the real
    roster — the extra identities the diarizer invents (the session minted
    S5/S6/S7 for a 4-player table). Count = max(0, n_hyp_clusters - n_true
    speakers); the phantoms named are the lowest-speaking-time clusters beyond
    the top-n_true. (One-player-split-into-many is scored separately as label
    continuity, not here.)"""
    hyp_labels = {w[3] for w in words}
    if not hyp_labels:
        return {"phantom_label_count": 0, "phantom_labels": [], "n_hyp_labels": 0}
    n_true = len({t["speaker"] for t in turns})
    time_by_label: dict[str, float] = {}
    for start, end, _txt, label in words:
        time_by_label[label] = time_by_label.get(label, 0.0) + max(1e-3, end - start)
    ranked = sorted(time_by_label, key=time_by_label.get, reverse=True)
    phantoms = sorted(ranked[n_true:])
    return {
        "phantom_label_count": max(0, len(hyp_labels) - n_true),
        "phantom_labels": phantoms,
        "n_hyp_labels": len(hyp_labels),
    }


def _answer_turns(turns: list[dict]) -> list[dict]:
    return [t for t in turns if t.get("is_answer")]


def _turn_hyp_coverage(turn: dict, words: list[tuple]) -> float:
    dur = max(1e-6, turn["end"] - turn["start"])
    covered = sum(
        _overlap(w[0], w[1], turn["start"], turn["end"]) for w in words
    )
    return min(1.0, covered / dur)


def answer_attribution_accuracy(
    words: list[tuple], turns: list[dict]
) -> dict:
    """For each answer turn, the majority hypothesis label over the turn span is
    mapped to its true speaker; correct iff that speaker is the answer's speaker.
    An answer with no hypothesis words counts as a miss (also a dropped answer)."""
    answers = _answer_turns(turns)
    if not answers:
        return {"answer_attribution_accuracy": 1.0, "n_answers": 0, "correct": 0}
    cmap = cluster_true_map(words, turns)
    correct = 0
    for a in answers:
        label_time: dict[str, float] = {}
        for start, end, _txt, label in words:
            ov = _overlap(start, end, a["start"], a["end"])
            if ov > 0:
                label_time[label] = label_time.get(label, 0.0) + ov
        if not label_time:
            continue  # miss
        top = max(label_time, key=label_time.get)
        if cmap.get(top) == a["speaker"]:
            correct += 1
    return {
        "answer_attribution_accuracy": correct / len(answers),
        "n_answers": len(answers),
        "correct": correct,
    }


def dropped_answer_rate(
    words: list[tuple], turns: list[dict], coverage_threshold: float = 0.5
) -> dict:
    """An answer is DROPPED when the hypothesis covers less than
    `coverage_threshold` of its span with any word — the diarizer/decoder lost
    the player's answer to cross-talk (the session's characteristic failure)."""
    answers = _answer_turns(turns)
    if not answers:
        return {"dropped_answer_rate": 0.0, "n_answers": 0, "dropped": 0}
    dropped = [
        a for a in answers if _turn_hyp_coverage(a, words) < coverage_threshold
    ]
    return {
        "dropped_answer_rate": len(dropped) / len(answers),
        "n_answers": len(answers),
        "dropped": len(dropped),
        "coverage_threshold": coverage_threshold,
    }


def first_answer_timestamp_fidelity(
    words: list[tuple], turns: list[dict], tolerance_s: float = 2.0
) -> dict:
    """Onset delta between the first answer's true start and the earliest
    hypothesis word overlapping (or nearest) that answer span. Fidelity is a
    0..1 score, linearly decaying to 0 at `tolerance_s`. First-buzz adjudication
    hinges on this: a late/early onset can hand the point to the wrong player."""
    answers = sorted(_answer_turns(turns), key=lambda t: t["start"])
    if not answers:
        return {"first_answer_timestamp_fidelity": 1.0, "delta_s": None}
    a = answers[0]
    window = [w for w in words if _overlap(w[0], w[1], a["start"], a["end"]) > 0]
    if not window:
        return {"first_answer_timestamp_fidelity": 0.0, "delta_s": None,
                "note": "no hypothesis word in first-answer span"}
    hyp_start = min(w[0] for w in window)
    delta = abs(hyp_start - a["start"])
    fidelity = max(0.0, 1.0 - delta / tolerance_s)
    return {
        "first_answer_timestamp_fidelity": round(fidelity, 4),
        "delta_s": round(delta, 4),
        "tolerance_s": tolerance_s,
    }


# ---------------------------------------------------------------------------
# Word-alignment: the challenger integration path being priced
# ---------------------------------------------------------------------------

def word_align(words: list[tuple], diar_turns: Iterable[dict]) -> list[tuple]:
    """Re-label STT words from an EXTERNAL diarization stream (the pyannote
    Live-1 integration): each word takes the diar speaker with the greatest
    temporal overlap to the word span, nearest-boundary if none overlaps.

    This is the exact glue a Live-1 swap requires — a parallel diarization
    stream reconciled against the STT word timeline — so its correctness is
    unit-tested here even though the live challenger is credential-gated.
    """
    turns = list(diar_turns)
    if not turns:
        return list(words)
    out = []
    for start, end, text, _old in words:
        best, best_ov = None, 0.0
        for t in turns:
            ov = _overlap(start, end, t["start"], t["end"])
            if ov > best_ov:
                best, best_ov = t["speaker"], ov
        if best is None:
            mid = (start + end) / 2.0
            best = min(
                turns,
                key=lambda t: min(abs(t["start"] - mid), abs(t["end"] - mid)),
            )["speaker"]
        out.append((start, end, text, best))
    return out


def all_game_metrics(words: list[tuple], turns: list[dict]) -> dict:
    """Bundle the four game-level metrics for one arm on one scene."""
    return {
        **phantom_label_count(words, turns),
        **answer_attribution_accuracy(words, turns),
        **dropped_answer_rate(words, turns),
        **first_answer_timestamp_fidelity(words, turns),
    }
