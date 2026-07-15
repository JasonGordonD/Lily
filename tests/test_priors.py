"""Tests for the dynamic state-prior thresholds (WO-LILY-ADDRESSEE-H1-001
Task 2) — pure scorekeeper / evaluation / config, no livekit, no network.

Covers: overlap detection from segment timestamps, the prior-state machine
(OPEN_WINDOW / OVERLAP / HOST_SPEAKING / SCORING / IDLE), the parametric
Tier-1 threshold, the middle-band helper Task 4 consumes, and the env knobs
through lily_config.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lily_config
from lily_evaluation import (
    BAND_ACCEPT,
    BAND_CLARIFY,
    BAND_REJECT,
    FUZZY_CORRECT_THRESHOLD,
    lily_tier1_band,
    lily_tier1_evaluate,
    lily_tier1_evaluate_question,
)
from lily_scorekeeper import (
    PRIOR_HOST_SPEAKING,
    PRIOR_IDLE,
    PRIOR_OPEN_WINDOW,
    PRIOR_OVERLAP,
    PRIOR_SCORING,
    LilyScorekeeper,
)


def make_sk(**kwargs):
    sk = LilyScorekeeper(session_id="test-room", **kwargs)
    sk.bind_speaker("S1", "Sarah")
    sk.bind_speaker("S2", "Dave")
    sk.bind_speaker("S3", "Priya")
    return sk


def seg(sk, text, label, start, end, now=None):
    return sk.on_transcript_segment(
        text=text,
        speaker_label=label,
        is_final=True,
        segment_start_time=start,
        segment_end_time=end,
        now=now if now is not None else end,
    )


# ---------------------------------------------------------------------------
# (1) Overlap: two speakers, temporally overlapping segments in an open
# window -> OVERLAP prior, sharply raised threshold, borderline answers
# escalate instead of auto-accepting.
# ---------------------------------------------------------------------------

def test_two_overlapping_speakers_flip_overlap_prior():
    sk = make_sk()
    sk.open_answer_window(now=100.0)
    r1 = seg(sk, "I want to say Canberra", "S1", 101.0, 104.0)
    assert r1["prior_state"] == PRIOR_OPEN_WINDOW
    assert r1["overlap_flag"] is False
    # Dave talks OVER Sarah: [102.5, 105.0] overlaps [101.0, 104.0] by 1.5s.
    r2 = seg(sk, "no no it's Sydney surely", "S2", 102.5, 105.0)
    assert r2["prior_state"] == PRIOR_OVERLAP
    assert r2["overlap_flag"] is True
    assert sk.overlap_flag is True
    # Crosstalk raises the bar sharply.
    assert sk.tier1_threshold(now=105.0) == lily_config.tier1_threshold_overlap()
    assert (
        lily_config.tier1_threshold_overlap()
        > lily_config.tier1_threshold_open_window()
    )


def test_borderline_answer_passes_open_window_but_not_overlap():
    # "Canbera" ~ 0.933 similarity to "canberra": comfortably above the
    # OPEN_WINDOW bar, below the OVERLAP bar (default > 1.0 = no Tier-1
    # auto-accept at all — escalate to the judge).
    open_thr = lily_config.tier1_threshold_open_window()
    overlap_thr = lily_config.tier1_threshold_overlap()
    r_open = lily_tier1_evaluate("Canbera", ["canberra"], threshold=open_thr)
    assert r_open["verdict"] == "correct"
    r_overlap = lily_tier1_evaluate(
        "Canbera", ["canberra"], threshold=overlap_thr
    )
    assert r_overlap["verdict"] == "uncertain"
    assert r_overlap["similarity"] > 0.9  # score preserved for the band


def test_correct_answer_inside_crosstalk_escalates_not_scores():
    # The WO verify scenario: the correct answer INSIDE deliberation
    # crosstalk (containment would fire at the open-window bar) must not
    # auto-score once OVERLAP has flipped — it escalates to Tier-2.
    sk = make_sk()
    sk.open_answer_window(now=100.0)
    seg(sk, "it's Madagascar I think", "S1", 101.0, 104.0)
    seg(sk, "no wait Mauritius maybe", "S2", 102.0, 105.0)
    assert sk.overlap_flag is True
    thr = sk.tier1_threshold(now=105.0)
    r = lily_tier1_evaluate(
        "it's Madagascar I think", ["madagascar"], threshold=thr
    )
    assert r["verdict"] == "uncertain"
    # Same containment hit auto-accepts under a clean window.
    r_clean = lily_tier1_evaluate(
        "it's Madagascar I think", ["madagascar"],
        threshold=lily_config.tier1_threshold_open_window(),
    )
    assert r_clean["verdict"] == "correct"


def test_candidates_still_recorded_under_overlap():
    # The prior gates ACCEPTANCE, never candidate capture — crosstalk
    # answers stay in the pool for the judge / clarify path.
    sk = make_sk()
    sk.open_answer_window(now=100.0)
    seg(sk, "Madagascar", "S1", 101.0, 104.0)
    r2 = seg(sk, "Mauritius", "S2", 102.0, 105.0)
    assert r2["prior_state"] == PRIOR_OVERLAP
    assert r2["candidate_recorded"] is True
    assert len(sk.answer_candidates) == 2


# ---------------------------------------------------------------------------
# (2) The same answer clean and solo in an open window scores on Tier-1.
# ---------------------------------------------------------------------------

def test_clean_solo_answer_scores_on_tier1():
    sk = make_sk()
    sk.current_question = {"acceptable_answers": ["canberra"]}
    sk.open_answer_window(now=100.0)
    r = seg(sk, "Canbera", "S1", 101.0, 103.0)
    assert r["prior_state"] == PRIOR_OPEN_WINDOW
    assert r["overlap_flag"] is False
    assert r["candidate_recorded"] is True
    thr = sk.tier1_threshold(now=103.0)
    assert thr == lily_config.tier1_threshold_open_window()
    t1 = lily_tier1_evaluate("Canbera", ["canberra"], threshold=thr)
    assert t1["verdict"] == "correct"


def test_open_window_favors_recall_below_baseline():
    # A mangle in the [open_window, baseline) gap: accepted right after
    # the ask, escalated anywhere else. "Canterra" ~ 0.875 vs "canberra"
    # (soundex-divergent, so the phonetic path stays out of it too).
    open_thr = lily_config.tier1_threshold_open_window()
    assert open_thr < FUZZY_CORRECT_THRESHOLD
    assert lily_tier1_evaluate(
        "Canterra", ["canberra"], threshold=open_thr
    )["verdict"] == "correct"
    assert lily_tier1_evaluate(
        "Canterra", ["canberra"]
    )["verdict"] == "uncertain"  # default threshold = pre-H1 behavior


# ---------------------------------------------------------------------------
# (3) HOST_SPEAKING / SCORING bias against acceptance.
# ---------------------------------------------------------------------------

def test_host_speaking_prior_biases_against_acceptance():
    sk = make_sk()
    sk.open_answer_window(now=100.0)
    sk.host_speaking = True
    assert sk.prior_state(now=101.0) == PRIOR_HOST_SPEAKING
    thr = sk.tier1_threshold(now=101.0)
    assert thr == lily_config.tier1_threshold_host_speaking()
    assert thr > lily_config.tier1_threshold_open_window()
    # Even a verbatim answer does not auto-accept while Lily is on air.
    assert lily_tier1_evaluate(
        "Canberra", ["canberra"], threshold=thr
    )["verdict"] == "uncertain"


def test_scoring_prior_biases_against_acceptance():
    sk = make_sk()
    sk.adjudicating = True
    assert sk.prior_state() == PRIOR_SCORING
    thr = sk.tier1_threshold()
    assert thr == lily_config.tier1_threshold_scoring()
    assert lily_tier1_evaluate(
        "Canberra", ["canberra"], threshold=thr
    )["verdict"] == "uncertain"


def test_prior_precedence_scoring_over_host_speaking_over_overlap():
    sk = make_sk()
    sk.open_answer_window(now=100.0)
    seg(sk, "Madagascar", "S1", 101.0, 104.0)
    seg(sk, "Mauritius", "S2", 102.0, 105.0)
    assert sk.prior_state(now=105.0) == PRIOR_OVERLAP
    sk.host_speaking = True
    assert sk.prior_state(now=105.0) == PRIOR_HOST_SPEAKING
    sk.adjudicating = True
    assert sk.prior_state(now=105.0) == PRIOR_SCORING


def test_segment_during_host_speaking_is_stamped_host_speaking():
    sk = make_sk()
    sk.open_answer_window(now=100.0)
    sk.host_speaking = True
    r = seg(sk, "yeah", "S1", 101.0, 101.5)
    assert r["prior_state"] == PRIOR_HOST_SPEAKING


# ---------------------------------------------------------------------------
# (4) Result-dict presence + sane defaults when the window is closed.
# ---------------------------------------------------------------------------

def test_prior_state_in_result_dict_defaults_idle_when_window_closed():
    sk = make_sk()
    r = seg(sk, "hello everyone", "S1", 10.0, 12.0)
    assert r["prior_state"] == PRIOR_IDLE
    assert r["overlap_flag"] is False
    # IDLE threshold is the pre-H1 baseline.
    assert sk.tier1_threshold() == lily_config.tier1_threshold_idle()
    assert lily_config.tier1_threshold_idle() == FUZZY_CORRECT_THRESHOLD


def test_prior_keys_present_on_partials_and_empty_segments():
    sk = make_sk()
    r_partial = sk.on_transcript_segment(
        text="canb", speaker_label="S1", is_final=False, now=10.0
    )
    assert r_partial["prior_state"] == PRIOR_IDLE
    assert r_partial["overlap_flag"] is False
    r_empty = sk.on_transcript_segment(text="", speaker_label="S1", now=10.0)
    assert r_empty["prior_state"] == PRIOR_IDLE


def test_expired_window_reads_idle():
    sk = make_sk(answer_window_seconds=5.0)
    sk.open_answer_window(now=100.0)
    assert sk.prior_state(now=102.0) == PRIOR_OPEN_WINDOW
    assert sk.prior_state(now=106.0) == PRIOR_IDLE


def test_window_prior_state_survives_close_resets_on_open():
    # Adjudication evaluates the just-closed window under the prior its
    # candidates were captured in.
    sk = make_sk()
    sk.open_answer_window(now=100.0)
    seg(sk, "Madagascar", "S1", 101.0, 104.0)
    seg(sk, "Mauritius", "S2", 102.0, 105.0)
    sk.close_answer_window()
    assert sk.window_prior_state() == PRIOR_OVERLAP
    sk.open_answer_window(now=200.0)  # fresh window, fresh assessment
    assert sk.overlap_flag is False
    assert sk.window_prior_state() == PRIOR_OPEN_WINDOW


def test_steal_reopen_resets_overlap():
    sk = make_sk()
    sk.open_answer_window(now=100.0)
    seg(sk, "Madagascar", "S1", 101.0, 104.0)
    seg(sk, "Mauritius", "S2", 102.0, 105.0)
    assert sk.overlap_flag is True
    sk.open_answer_window(now=110.0, reset_candidates=False)  # steal window
    assert sk.overlap_flag is False


# ---------------------------------------------------------------------------
# Overlap detection math — conservative by construction.
# ---------------------------------------------------------------------------

def test_overlap_below_epsilon_does_not_flip():
    sk = make_sk()
    sk.open_answer_window(now=100.0)
    seg(sk, "Canberra", "S1", 101.0, 103.0)
    # 0.2s of overlap < the 0.3s default epsilon.
    r = seg(sk, "Sydney", "S2", 102.8, 105.0)
    assert r["prior_state"] == PRIOR_OPEN_WINDOW
    assert sk.overlap_flag is False


def test_same_speaker_overlapping_spans_do_not_flip():
    sk = make_sk()
    sk.open_answer_window(now=100.0)
    seg(sk, "Canberra", "S1", 101.0, 104.0)
    r = seg(sk, "Canberra Australia", "S1", 102.0, 105.0)
    assert r["prior_state"] == PRIOR_OPEN_WINDOW
    assert sk.overlap_flag is False


def test_sequential_speakers_do_not_flip():
    sk = make_sk()
    sk.open_answer_window(now=100.0)
    seg(sk, "Canberra", "S1", 101.0, 103.0)
    r = seg(sk, "Sydney", "S2", 104.0, 106.0)
    assert r["prior_state"] == PRIOR_OPEN_WINDOW
    assert sk.overlap_flag is False


def test_degenerate_zero_length_spans_never_flip(monkeypatch):
    # Production reality at 1.6.4: no per-segment word timings, so
    # segment_start == arrival time. Point spans must never flip OVERLAP,
    # even at epsilon 0 (strict inequality).
    monkeypatch.setenv("LILY_OVERLAP_EPSILON_SECONDS", "0")
    sk = make_sk()
    sk.open_answer_window(now=100.0)
    seg(sk, "Madagascar", "S1", 103.0, 103.0)
    r = seg(sk, "Mauritius", "S2", 103.0, 103.0)
    assert r["prior_state"] == PRIOR_OPEN_WINDOW
    assert sk.overlap_flag is False


def test_overlap_outside_window_is_ignored():
    sk = make_sk()
    # No window open — overlapping table talk is game-inert.
    seg(sk, "Madagascar", "S1", 101.0, 104.0)
    r = seg(sk, "Mauritius", "S2", 102.0, 105.0)
    assert r["prior_state"] == PRIOR_IDLE
    assert sk.overlap_flag is False


def test_unrostered_voice_counts_toward_overlap():
    sk = make_sk()
    sk.open_answer_window(now=100.0)
    seg(sk, "Madagascar", "S1", 101.0, 104.0)
    r = seg(sk, "Mauritius", "S9", 102.0, 105.0)  # unbound label
    assert r["unrostered"] is True
    assert r["prior_state"] == PRIOR_OVERLAP


# ---------------------------------------------------------------------------
# Parametric threshold — default keeps pre-H1 behavior exactly.
# ---------------------------------------------------------------------------

def test_default_threshold_behavior_unchanged():
    assert lily_tier1_evaluate("Canberra", ["canberra"])["method"] == "exact"
    assert lily_tier1_evaluate(
        "Canberra, Australia", ["canberra"]
    )["method"] == "containment"
    assert lily_tier1_evaluate("Canbera", ["canberra"])["method"] == "fuzzy"
    q = {"acceptable_answers": ["canberra"]}
    assert lily_tier1_evaluate_question("Canberra", q)["verdict"] == "correct"


def test_question_dispatch_passes_threshold_through():
    q = {"acceptable_answers": ["canberra"]}
    r = lily_tier1_evaluate_question(
        "Canbera", q, threshold=lily_config.tier1_threshold_overlap()
    )
    assert r["verdict"] == "uncertain"


def test_mc_letter_pick_still_resolves_under_raised_threshold():
    # A bare letter is a committed selection — deterministic parse, not a
    # similarity match; the raised prior only gates option-TEXT resolvers.
    q = {
        "choices": ["Paris", "Canberra", "Oslo", "Lima"],
        "canonical_answer": "Canberra",
    }
    r = lily_tier1_evaluate_question(
        "b", q, threshold=lily_config.tier1_threshold_overlap()
    )
    assert r["verdict"] == "correct"
    assert r["method"] == "letter"
    # Option text under the raised bar escalates instead.
    r_text = lily_tier1_evaluate_question(
        "Canberra", q, threshold=lily_config.tier1_threshold_overlap()
    )
    assert r_text["verdict"] == "uncertain"


# ---------------------------------------------------------------------------
# Middle band (the Task 4 consumption surface).
# ---------------------------------------------------------------------------

def test_tier1_band_partition():
    margin = lily_config.tier1_clarify_margin()
    thr = lily_config.tier1_threshold_open_window()
    assert lily_tier1_band(thr, thr, margin) == BAND_ACCEPT
    assert lily_tier1_band(thr - 0.001, thr, margin) == BAND_CLARIFY
    assert lily_tier1_band(thr - margin, thr, margin) == BAND_CLARIFY
    assert lily_tier1_band(thr - margin - 0.001, thr, margin) == BAND_REJECT
    assert lily_tier1_band(0.0, thr, margin) == BAND_REJECT


def test_tier1_band_under_overlap_everything_below_accept():
    # OVERLAP default sits above 1.0: nothing reaches BAND_ACCEPT; a
    # near-exact hit lands in the clarify band for Task 4.
    thr = lily_config.tier1_threshold_overlap()
    margin = lily_config.tier1_clarify_margin()
    assert lily_tier1_band(1.0, thr, margin) == BAND_CLARIFY
    assert lily_tier1_band(0.5, thr, margin) == BAND_REJECT


def test_addressee_confidence_adds_penalty_to_active_prior_threshold():
    sk = make_sk()
    sk.open_answer_window(now=100.0)
    sk.on_transcript_segment(
        text="Canberra",
        speaker_label="S1",
        is_final=True,
        now=101.0,
        segment_start_time=101.0,
        addressee_confidence=0.2,
    )
    assert sk.tier1_threshold(now=101.0) > lily_config.tier1_threshold_open_window()


def test_high_addressee_confidence_keeps_open_window_baseline():
    sk = make_sk()
    sk.open_answer_window(now=100.0)
    sk.on_transcript_segment(
        text="Canberra",
        speaker_label="S1",
        is_final=True,
        now=101.0,
        segment_start_time=101.0,
        addressee_confidence=0.95,
    )
    assert sk.tier1_threshold(now=101.0) == lily_config.tier1_threshold_open_window()


# ---------------------------------------------------------------------------
# (5) Env knobs through lily_config — correct defaults, env-tunable.
# ---------------------------------------------------------------------------

def test_threshold_env_defaults(monkeypatch):
    for var in (
        "LILY_TIER1_THRESHOLD_OPEN_WINDOW",
        "LILY_TIER1_THRESHOLD_OVERLAP",
        "LILY_TIER1_THRESHOLD_HOST_SPEAKING",
        "LILY_TIER1_THRESHOLD_SCORING",
        "LILY_TIER1_THRESHOLD_IDLE",
        "LILY_OVERLAP_EPSILON_SECONDS",
        "LILY_TIER1_CLARIFY_MARGIN",
    ):
        monkeypatch.delenv(var, raising=False)
    assert lily_config.tier1_threshold_open_window() == 0.84
    assert lily_config.tier1_threshold_overlap() == 1.01
    assert lily_config.tier1_threshold_host_speaking() == 1.01
    assert lily_config.tier1_threshold_scoring() == 1.01
    assert lily_config.tier1_threshold_idle() == 0.88
    assert lily_config.overlap_epsilon_seconds() == 0.3
    assert lily_config.tier1_clarify_margin() == 0.15
    # Lowered / raised relative to the baseline, by construction.
    assert lily_config.tier1_threshold_open_window() < 0.88
    assert lily_config.tier1_threshold_overlap() > 1.0


def test_thresholds_read_from_env(monkeypatch):
    monkeypatch.setenv("LILY_TIER1_THRESHOLD_OPEN_WINDOW", "0.7")
    monkeypatch.setenv("LILY_TIER1_THRESHOLD_OVERLAP", "0.98")
    monkeypatch.setenv("LILY_OVERLAP_EPSILON_SECONDS", "0.5")
    monkeypatch.setenv("LILY_TIER1_CLARIFY_MARGIN", "0.2")
    assert lily_config.tier1_threshold_open_window() == 0.7
    assert lily_config.tier1_threshold_overlap() == 0.98
    assert lily_config.overlap_epsilon_seconds() == 0.5
    assert lily_config.tier1_clarify_margin() == 0.2
    # Scorekeeper picks the env value up live.
    sk = make_sk()
    sk.open_answer_window(now=100.0)
    assert sk.tier1_threshold(now=101.0) == 0.7
    # A tuned-down OVERLAP bar (<= 1.0) lets exact hits through again.
    r = lily_tier1_evaluate("Canberra", ["canberra"], threshold=0.98)
    assert r["verdict"] == "correct"


def test_threshold_for_prior_mapping_and_fallback():
    assert (
        lily_config.tier1_threshold_for_prior(PRIOR_OPEN_WINDOW)
        == lily_config.tier1_threshold_open_window()
    )
    assert (
        lily_config.tier1_threshold_for_prior(PRIOR_OVERLAP)
        == lily_config.tier1_threshold_overlap()
    )
    assert (
        lily_config.tier1_threshold_for_prior(PRIOR_HOST_SPEAKING)
        == lily_config.tier1_threshold_host_speaking()
    )
    assert (
        lily_config.tier1_threshold_for_prior(PRIOR_SCORING)
        == lily_config.tier1_threshold_scoring()
    )
    assert (
        lily_config.tier1_threshold_for_prior(PRIOR_IDLE)
        == lily_config.tier1_threshold_idle()
    )
    # Unknown / None never fail — they fall back to the IDLE baseline.
    assert (
        lily_config.tier1_threshold_for_prior("NOT_A_STATE")
        == lily_config.tier1_threshold_idle()
    )
    assert (
        lily_config.tier1_threshold_for_prior(None)
        == lily_config.tier1_threshold_idle()
    )
