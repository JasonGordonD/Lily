"""WO-LILY-VOICE-IDENTITY-001 — durable device-independent voice matcher.

The pure matching core: cosine similarity + margin gate + running-mean
centroid. Model-agnostic (embeddings are plain float vectors), so these
tests pin the recognition LOGIC independent of whichever speaker-
verification model and storage backend get wired later.
"""

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_voice_identity as vid


def _unit(vec):
    n = math.sqrt(sum(x * x for x in vec))
    return [x / n for x in vec]


# -- normalization / similarity primitives ------------------------------------


def test_l2_normalize_makes_unit_length():
    out = vid.lily_l2_normalize([3.0, 4.0])
    assert out is not None
    assert abs(math.sqrt(sum(x * x for x in out)) - 1.0) < 1e-9
    assert abs(out[0] - 0.6) < 1e-9 and abs(out[1] - 0.8) < 1e-9


def test_l2_normalize_rejects_degenerate():
    assert vid.lily_l2_normalize([]) is None
    assert vid.lily_l2_normalize([0.0, 0.0]) is None
    assert vid.lily_l2_normalize([float("nan"), 1.0]) is None
    assert vid.lily_l2_normalize(["x", 1.0]) is None


def test_cosine_identical_is_one_orthogonal_is_zero():
    assert abs(vid.lily_cosine_similarity([1, 0, 0], [2, 0, 0]) - 1.0) < 1e-9
    assert abs(vid.lily_cosine_similarity([1, 0], [0, 5]) - 0.0) < 1e-9


def test_cosine_shape_mismatch_is_none():
    assert vid.lily_cosine_similarity([1, 0], [1, 0, 0]) is None


# -- matching: the returning voice is recognized ------------------------------


def test_returning_voice_matches_its_own_centroid():
    me = _unit([0.9, 0.1, 0.2, 0.05])
    other = _unit([0.1, 0.9, 0.1, 0.3])
    # A fresh probe close to `me`.
    probe = _unit([0.88, 0.12, 0.22, 0.06])
    result = vid.lily_match_voice(
        probe,
        [{"group_id": "me", "centroid": me}, {"group_id": "other", "centroid": other}],
    )
    assert result is not None
    assert result["group_id"] == "me"
    assert result["score"] >= vid.DEFAULT_MATCH_THRESHOLD


def test_stranger_does_not_match():
    a = _unit([1.0, 0.0, 0.0, 0.0])
    b = _unit([0.0, 1.0, 0.0, 0.0])
    stranger = _unit([0.0, 0.0, 1.0, 0.0])  # orthogonal to both
    assert vid.lily_match_voice(
        stranger,
        [{"group_id": "a", "centroid": a}, {"group_id": "b", "centroid": b}],
    ) is None


def test_ambiguous_between_two_similar_voices_is_withheld():
    # Two near-identical stored centroids; a probe close to both. A false
    # merge (greeting a stranger by a housemate's name) is the costly error
    # the margin gate prevents.
    base = _unit([0.7, 0.7, 0.1, 0.1])
    twinnish = _unit([0.69, 0.71, 0.1, 0.1])
    probe = _unit([0.70, 0.70, 0.11, 0.1])
    result = vid.lily_match_voice(
        probe,
        [{"group_id": "one", "centroid": base}, {"group_id": "two", "centroid": twinnish}],
        margin=0.06,
    )
    assert result is None  # within-margin → withheld


def test_threshold_and_margin_are_tunable():
    a = _unit([0.8, 0.2, 0.1])
    probe = _unit([0.6, 0.4, 0.2])  # moderately close
    # A permissive threshold matches; a strict one does not.
    assert vid.lily_match_voice(
        probe, [{"group_id": "a", "centroid": a}], threshold=0.5
    ) is not None
    assert vid.lily_match_voice(
        probe, [{"group_id": "a", "centroid": a}], threshold=0.999
    ) is None


def test_single_candidate_has_no_runner_up():
    a = _unit([0.9, 0.1, 0.1])
    probe = _unit([0.88, 0.12, 0.1])
    result = vid.lily_match_voice(probe, [{"group_id": "a", "centroid": a}])
    assert result is not None
    assert result["runner_up"] is None


def test_empty_and_bad_inputs_never_raise():
    assert vid.lily_match_voice([], []) is None
    assert vid.lily_match_voice([1, 0], []) is None
    assert vid.lily_match_voice(None, [{"group_id": "a", "centroid": [1, 0]}]) is None
    # Candidate with no group_id / bad centroid is skipped, not fatal.
    assert vid.lily_match_voice(
        [1, 0],
        [{"group_id": "", "centroid": [1, 0]}, {"group_id": "a", "centroid": None}],
    ) is None


# -- centroid: more speech sharpens the identity ------------------------------


def test_first_enrollment_seeds_centroid():
    c, n = vid.lily_update_centroid(None, 0, [3.0, 4.0])
    assert n == 1
    assert abs(math.sqrt(sum(x * x for x in c)) - 1.0) < 1e-9


def test_centroid_running_mean_moves_toward_new_samples():
    # Seed on one voice sample, fold in several near-copies; the centroid
    # stays unit-length and tracks the cluster.
    c, n = vid.lily_update_centroid(None, 0, _unit([1.0, 0.0, 0.0]))
    for _ in range(5):
        c, n = vid.lily_update_centroid(c, n, _unit([0.98, 0.1, 0.0]))
    assert n == 6
    assert abs(math.sqrt(sum(x * x for x in c)) - 1.0) < 1e-9
    # A fresh sample from the same cluster now matches strongly.
    sim = vid.lily_cosine_similarity(c, _unit([0.97, 0.12, 0.0]))
    assert sim >= 0.99


def test_centroid_bad_input_returns_prior_unchanged():
    c0 = _unit([1.0, 0.0])
    c, n = vid.lily_update_centroid(c0, 3, [float("inf"), 0.0])
    assert c == c0 and n == 3
    c, n = vid.lily_update_centroid(c0, 3, [])
    assert c == c0 and n == 3


def test_centroid_dim_mismatch_reseeds_rather_than_corrupts():
    c0 = _unit([1.0, 0.0])
    c, n = vid.lily_update_centroid(c0, 3, [0.0, 1.0, 0.0])  # wrong dim
    assert n == 1
    assert len(c) == 3
