"""
lily_config.py — LILY environment configuration.

All environment access for the Lily agent lives in this module (ambient
discipline: no raw os.environ reads scattered through the tree). Values are
read lazily so the module imports cleanly in test environments with no env
configured. Anything required at session start is validated fail-fast in
lily_agent.py / lily_persistence.py via the require_* helpers here.
"""

import os
from typing import Optional


def _get(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


def _get_float(name: str, default: float) -> float:
    raw = _get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _get_int(name: str, default: int) -> int:
    raw = _get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _require(name: str) -> str:
    value = _get(name)
    if value is None:
        raise RuntimeError(f"LILY_INIT | missing required env var: {name}")
    return value


# ---------------------------------------------------------------------------
# LiveKit
# ---------------------------------------------------------------------------

def livekit_url() -> Optional[str]:
    return _get("LIVEKIT_URL")


def livekit_api_key() -> Optional[str]:
    return _get("LIVEKIT_API_KEY")


def livekit_api_secret() -> Optional[str]:
    return _get("LIVEKIT_API_SECRET")


# ---------------------------------------------------------------------------
# ElevenLabs TTS — env var is ELEVEN_API_KEY (never ELEVENLABS_API_KEY)
# ---------------------------------------------------------------------------

def eleven_api_key() -> str:
    return _require("ELEVEN_API_KEY")


# Voice preset 1 — Lily's primary and default voice. Hardcoded ID (same
# pattern as Zuna's VOICE_NADIA), overridable via LILY_VOICE_1.
LILY_VOICE_1_DEFAULT = "W3C2vBPukr5b5jvoXhPK"


def lily_voice_1() -> str:
    """Voice preset 1 — primary/default. Always populated."""
    return _get("LILY_VOICE_1") or LILY_VOICE_1_DEFAULT


def lily_voice_2() -> Optional[str]:
    """Voice preset 2 — Raven's voice (the former default):
    LILY_VOICE_ID with RAVEN_VOICE_ID fallback. None when unconfigured."""
    return _get("LILY_VOICE_ID") or _get("RAVEN_VOICE_ID")


def lily_voice_id() -> str:
    """Voice active at session start: preset 1 (primary)."""
    return lily_voice_1()


# ---------------------------------------------------------------------------
# Google / Gemini
# ---------------------------------------------------------------------------

def google_api_key() -> str:
    return _require("GOOGLE_API_KEY")


def vocal_model() -> str:
    return _get("LILY_VOCAL_MODEL", "gemini-3.5-flash")


def reasoning_model() -> str:
    return _get("LILY_REASONING_MODEL", "gemini-3.1-pro-preview")


def assessment_model() -> str:
    # WS-12 clinical desk: post-session assessment runs on the reasoning
    # model unless pinned separately.
    return _get("LILY_ASSESSMENT_MODEL", reasoning_model())


def report_deadline_seconds() -> float:
    # WS-12 exit bar M: wrap-up beat -> assessed report, and the per-call
    # generation timeout.
    return _get_float("LILY_REPORT_DEADLINE_S", 300.0)


def report_sweep_min_age_seconds() -> float:
    return _get_float("LILY_REPORT_SWEEP_MIN_AGE_S", 600.0)


def report_sweep_limit() -> int:
    return _get_int("LILY_REPORT_SWEEP_LIMIT", 10)


def vocal_max_output_tokens() -> int:
    # Spec §4.4: max_output_tokens >= 600 on live calls.
    return max(600, _get_int("LILY_MAX_OUTPUT_TOKENS", 800))


def reasoning_max_output_tokens() -> int:
    """Dedicated budget for reasoning-node generation/verification calls
    (P1 root cause, 2026-07-14 19:27 logs: on Gemini 3.x THINKING TOKENS
    COUNT toward max_output_tokens — 3.1-pro at thinking_level=medium ate
    most of the shared 800-token vocal budget before the JSON body,
    truncating it mid-object). Prefetch is off the hot path; latency is
    irrelevant there, so the default is generous."""
    return max(600, _get_int("LILY_REASONING_MAX_OUTPUT_TOKENS", 4096))


def judge_max_output_tokens() -> int:
    """Tier-2 judge budget: runs on the vocal model at thinking low and
    IS latency-relevant (mid-window / reveal path), but its verdict JSON
    is small — a middle default covers thinking + verdict."""
    return max(600, _get_int("LILY_JUDGE_MAX_OUTPUT_TOKENS", 1024))


# ---------------------------------------------------------------------------
# Supabase
# ---------------------------------------------------------------------------

def supabase_url() -> Optional[str]:
    return _get("SUPABASE_URL")


def supabase_service_role_key() -> Optional[str]:
    return _get("SUPABASE_SERVICE_ROLE_KEY")


# ---------------------------------------------------------------------------
# Web tools (WO-LILY-OMNIBUS-002 I/K) — REASONING NODE ONLY, see lily_search.py.
# Missing keys disable the corresponding tool (text-only fallback); never
# required at boot.
# ---------------------------------------------------------------------------

def xai_api_key() -> Optional[str]:
    """xAI Grok vision (lily_vision — the fleet's consolidated vision
    surface, ported from Zuna). Missing key = vision unavailable, honest
    availability caveat, never a boot failure."""
    return _get("XAI_API_KEY")


def exa_api_key() -> Optional[str]:
    return _get("EXA_API_KEY")


def tavily_api_key() -> Optional[str]:
    return _get("TAVILY_API_KEY")


def imagegen_model() -> str:
    """Gemini image model for invented-content picture questions
    (sub-agent J; image_source='generated' only, prefetch-time only)."""
    return _get("LILY_IMAGEGEN_MODEL", "gemini-2.5-flash-image")


# ---------------------------------------------------------------------------
# Interruption + noise layer (WS-14)
# ---------------------------------------------------------------------------

def interruption_mode() -> str:
    """Session interruption strategy: "adaptive" (LiveKit Cloud ML model —
    distinguishes true barge-ins from backchannels/coughs/reverberant
    cross-talk) or "vad" (plain energy VAD). Default adaptive; the
    framework degrades to VAD on its own when the detector is unavailable
    or errors (agent_activity._resolve_interruption_detection), so
    adaptive is never a hard dependency."""
    mode = _get("LILY_INTERRUPTION_MODE", "adaptive")
    return mode if mode in ("adaptive", "vad") else "adaptive"


def false_interruption_timeout() -> float:
    """Seconds of post-trigger silence before an interruption with no
    transcript is classified FALSE and the paused speech resumes from its
    pause point (framework default 2.0). The 4-player echo room's noise
    bursts land here: pause-and-resume instead of a hard cut."""
    return _get_float("LILY_FALSE_INTERRUPTION_TIMEOUT", 2.0)


def noise_cancellation_mode() -> str:
    """Krisp noise cancellation on the room input: "nc" (ambient model,
    default) or "off" (kill switch — the 1.6.4 NcSession sample-rate
    SIGABRT killed every job accept; if that recurs at 1.6.6 this disables
    NC via slot secret, no redeploy). BVC is NOT a value: in a one-mic
    multiplayer room the "background voices" are the other players — BVC
    would erase the table. Any unknown value (including "bvc") coerces to
    "nc"."""
    mode = _get("LILY_NOISE_CANCELLATION", "nc")
    return mode if mode in ("nc", "off") else "nc"


def room_discharge_seconds() -> float:
    """Room-discharge pacing gap (AMENDMENT-002): structural pause between
    question-delivery playout completion and the answer window's
    mic-sensitive phase, letting the room's acoustic energy decay so
    answers arrive at higher effective SNR. 0 disables (window opens
    immediately, pre-WS-14 behavior)."""
    return _get_float("LILY_ROOM_DISCHARGE_SECONDS", 0.5)


def mc_answer_aborts_read() -> bool:
    """WS-5 (WO-LILY-OMNIBUS-003): a final landing DURING an in-flight
    multiple-choice options read that Tier-1-matches a read option (or the
    canonical set) TRUNCATES the remaining options and jumps to
    adjudication — options 3-4 never air, the point goes to the answerer.
    Default on; LILY_MC_ANSWER_ABORTS_READ=off restores the
    wait-for-full-options-playout behavior."""
    return _get_bool("LILY_MC_ANSWER_ABORTS_READ", True)


def buzz_prewindow_seconds() -> float:
    """WS-5: the buzz buffer widens to cover this many seconds BEFORE the
    delivery claim, so a final that landed just as the question was being
    asked is folded into the pre-window buffer and scored at window open,
    not left inert. 0 disables the pre-claim backfill (buffering still runs
    from the claim forward, exactly as before)."""
    return _get_float("LILY_BUZZ_PREWINDOW_SECONDS", 3.0)


def mc_stem_protect_words_per_second() -> float:
    """WS-5: estimated spoken words/second used to MODEL when the MC stem
    finishes reading, so the stem stays protected — an early answer can
    only truncate the OPTIONS, never cut the question before the table has
    heard it. There is no per-sentence playout signal at livekit-agents
    1.6.6 (the delivery stem+options are one SpeechHandle), so the
    stem-completion boundary is estimated from the stem word count. Lower =
    a longer protected stem span."""
    return _get_float("LILY_MC_STEM_PROTECT_WPS", 3.0)


# ---------------------------------------------------------------------------
# Game tunables
# ---------------------------------------------------------------------------

def answer_window_seconds() -> float:
    """Bounded answer window duration (default 15s, per-round configurable)."""
    return _get_float("LILY_ANSWER_WINDOW_SECONDS", 15.0)


def relaxed_window_multiplier() -> float:
    """Group prefs WO: relaxed pacing stretches the standard answer window
    by this factor (default 2.0). Timed pacing is exactly today's behavior
    — the multiplier never applies to it, nor to explicitly-passed
    durations (the steal window keeps its own tunable)."""
    return _get_float("LILY_RELAXED_WINDOW_MULTIPLIER", 2.0)


def steal_window_seconds() -> float:
    return _get_float("LILY_STEAL_WINDOW_SECONDS", 5.0)


def segment_max_span_seconds() -> float:
    """Segment sanity gate S (WO-LILY-OMNIBUS-003 WS-10): finals whose
    spoken span exceeds this are quarantined — session lily-81BCB0 shipped
    corrupted 104s/206s spans, while a real table answer lives well under
    the 15s window. Provisional default; WS-13's segmentation audit binds
    the tuned value here."""
    return _get_float("LILY_SEGMENT_MAX_SPAN_SECONDS", 30.0)


def segment_max_finalization_lag_seconds() -> float:
    """Segment sanity gate L (WS-10): finals arriving more than this many
    seconds after the speech ended are quarantined — the stale utterance
    that scored into the Black Panther window finalized ~3.5 minutes late;
    healthy reconciler drift is under a couple of seconds. Provisional
    default; WS-13 binds the tuned value here."""
    return _get_float("LILY_SEGMENT_MAX_FINALIZATION_LAG_SECONDS", 20.0)


def checkpoint_interval_seconds() -> float:
    return _get_float("LILY_CHECKPOINT_INTERVAL_SECONDS", 60.0)


def shutdown_timeout_seconds() -> float:
    return _get_float("LILY_SHUTDOWN_TIMEOUT_SECONDS", 30.0)


def rounds_total() -> int:
    return _get_int("LILY_ROUNDS", 3)


def kb_only() -> bool:
    """Demo-day fallback: flip question supply to the curated bank only
    (runbook: 'flip to KB-bank-only via the state block')."""
    return (_get("LILY_KB_ONLY", "") or "").strip().lower() in ("1", "true", "yes", "on")


def questions_per_round() -> int:
    return _get_int("LILY_QUESTIONS_PER_ROUND", 6)


def auto_start_min_players() -> int:
    """Roster size at or above which the lobby auto-start safety net can
    fire. Guards single-voice tune-ups from being flipped into game mode."""
    return max(1, _get_int("LILY_AUTO_START_MIN_PLAYERS", 2))


def auto_start_lobby_grace_seconds() -> float:
    """Wall-clock grace inside the lobby before the auto-start safety net
    is allowed to fire. Long enough for names + one lobby fact per player;
    short enough that a table that never touches the UI start button still
    reaches question one."""
    return _get_float("LILY_AUTO_START_LOBBY_GRACE_SECONDS", 60.0)


def intake_settle_seconds() -> float:
    """Quiet period after the most recent speaker bind before the game may
    start (WS-1 claim integrity): while the intake round-robin is still
    growing — a name just landed — begin_round and the auto-start net
    hold, so question one never arms against a half-built roster. The
    22:48 evidence session started between two introductions and turned
    an intake acknowledgment into q_1_delivery."""
    return _get_float("LILY_INTAKE_SETTLE_SECONDS", 20.0)


def undelivered_reconcile_seconds() -> float:
    """How long a delivery may stay registered-but-unplayed before the
    idle watchdog reconciles it (WS-2 registered-undelivered class): a
    question armed with a delivery claim that never confirmed and never
    failed — no playout, no exception. Past this many seconds of stuck
    ticks the watchdog re-fires the delivery, and after repeated re-fires
    releases the question back to supply. The 583a0f16 session held the
    loop for five minutes on q_0001/q_2943, both registered in
    asked_history and never aired."""
    return _get_float("LILY_UNDELIVERED_RECONCILE_SECONDS", 20.0)


def supply_fallback_seconds() -> float:
    """How long the game may sit live-idle with no question in hand (none
    armed, none prefetched) before the idle watchdog arms one straight from
    the curated bank (WS-6 supply-stall fallback). Independent of the
    prefetch hard timeout: a generator that returns nothing every tick
    keeps re-prefetching so the hard timeout never climbs — this window
    fires anyway. The 583a0f16 session sat starved for five minutes filled
    with vamping and never fell back."""
    return _get_float("LILY_SUPPLY_FALLBACK_SECONDS", 30.0)


def group_id_override() -> Optional[str]:
    """Stable group id for voiceprint rematch (v2); defaults to room name."""
    return _get("LILY_GROUP_ID")


def greeting_memory_budget_seconds() -> float:
    """Memory at the door (WO-LILY-DESYNC-HONESTY-001 F): how long the
    composed greeting may wait for group resolution + memory load before
    greeting cold. The live failure: [RETURNING TABLE] landed one turn
    AFTER the greeting fired, so a four-time table got 'who do we have at
    the table tonight?'. Never blocks the room beyond this budget; on
    timeout the greeting goes out cold and recognition arrives naturally.
    <=0 disables the wait entirely."""
    return _get_float("LILY_GREETING_MEMORY_BUDGET_SECONDS", 1.5)


def memory_min_questions() -> int:
    """Memory write threshold (WO-LILY-DESYNC-HONESTY-001 F): a
    lily_memories narrative row is written only when the session played
    at least this many questions OR reached round 2. Below it the session
    row still writes — no memory narrative (the live junk row: 'No sole
    winner over 1 question(s). Final scores: Rami 0')."""
    return max(0, _get_int("LILY_MEMORY_MIN_QUESTIONS", 3))


# ---------------------------------------------------------------------------
# State-prior Tier-1 thresholds (WO-LILY-ADDRESSEE-H1-001 Task 2)
# ---------------------------------------------------------------------------
# The scorekeeper's prior state (lily_scorekeeper.PRIOR_*) picks the Tier-1
# acceptance threshold. Semantics: a similarity-based Tier-1 match accepts
# only at or above the active threshold; any value ABOVE 1.0 disables Tier-1
# auto-accept entirely for that state (even exact/containment hits — max
# similarity is 1.0 — escalate to the Tier-2 judge). Defaults are deliberate:
# OPEN_WINDOW lowers the bar below the 0.88 baseline (favor recall right
# after the ask); OVERLAP / HOST_SPEAKING / SCORING sit above 1.0 (crosstalk
# is a deliberation prior, backchannels during host speech / adjudication
# are not scoreable — nothing auto-accepts, the judge or the clarify
# question decides).

# Mirrors lily_evaluation.FUZZY_CORRECT_THRESHOLD (the no-prior baseline).
TIER1_BASELINE_THRESHOLD = 0.88


def tier1_threshold_open_window() -> float:
    """OPEN_WINDOW: question just asked, floor is clean — LOWERED bar."""
    return _get_float("LILY_TIER1_THRESHOLD_OPEN_WINDOW", 0.84)


def tier1_threshold_overlap() -> float:
    """OVERLAP: >=2 speakers overlapping inside the window — RAISED sharply.
    Default > 1.0 = no Tier-1 auto-accept at all (everything escalates)."""
    return _get_float("LILY_TIER1_THRESHOLD_OVERLAP", 1.01)


def tier1_threshold_host_speaking() -> float:
    """HOST_SPEAKING: Lily is on air — backchannels expected, biased
    against acceptance."""
    return _get_float("LILY_TIER1_THRESHOLD_HOST_SPEAKING", 1.01)


def tier1_threshold_scoring() -> float:
    """SCORING: adjudication in flight — nothing new is scoreable."""
    return _get_float("LILY_TIER1_THRESHOLD_SCORING", 1.01)


def tier1_threshold_idle() -> float:
    """IDLE (window closed, nothing special): the pre-H1 baseline."""
    return _get_float("LILY_TIER1_THRESHOLD_IDLE", TIER1_BASELINE_THRESHOLD)


def tier1_threshold_for_prior(state: Optional[str]) -> float:
    """Threshold for a scorekeeper prior state. Unknown/None states fall
    back to the IDLE baseline (never fail, never loosen by accident).
    Keys are the lily_scorekeeper.PRIOR_* string values — spelled literally
    here because lily_scorekeeper imports this module."""
    return {
        "OPEN_WINDOW": tier1_threshold_open_window(),
        "OVERLAP": tier1_threshold_overlap(),
        "HOST_SPEAKING": tier1_threshold_host_speaking(),
        "SCORING": tier1_threshold_scoring(),
    }.get(state or "", tier1_threshold_idle())


def addressee_fusion_diarization_weight() -> float:
    """Speechmatics diarization contribution to fused addressee confidence."""
    return max(0.0, _get_float("LILY_ADDRESSEE_FUSION_DIARIZATION_WEIGHT", 0.75))


def addressee_fusion_acoustic_weight() -> float:
    """Acoustic (room-read + child ladder) contribution to fused confidence."""
    return max(0.0, _get_float("LILY_ADDRESSEE_FUSION_ACOUSTIC_WEIGHT", 0.25))


def addressee_acoustic_max_staleness_seconds() -> float:
    """Largest allowed transcript->acoustic lag for confidence fusion."""
    default = (
        audeering_window_seconds()
        + audeering_capture_interval_seconds()
        + 1.5
    )
    return max(
        0.5,
        _get_float("LILY_ADDRESSEE_ACOUSTIC_MAX_STALENESS_SECONDS", default),
    )


def addressee_acoustic_max_future_seconds() -> float:
    """Largest allowed acoustic lead over transcript timestamps."""
    return max(
        0.0,
        _get_float("LILY_ADDRESSEE_ACOUSTIC_MAX_FUTURE_SECONDS", 0.75),
    )


def addressee_confidence_neutral() -> float:
    """Confidence level where no threshold penalty is applied."""
    return max(0.05, min(_get_float("LILY_ADDRESSEE_CONFIDENCE_NEUTRAL", 0.65), 1.0))


def addressee_confidence_penalty_max() -> float:
    """Maximum additive threshold penalty at very low confidence."""
    return max(0.0, _get_float("LILY_ADDRESSEE_CONFIDENCE_PENALTY_MAX", 0.10))


def tier1_addressee_penalty(addressee_confidence: Optional[float]) -> float:
    """Additive Tier-1 penalty from fused addressee confidence.

    The score-prior machine remains intact; this simply adds a confidence
    penalty on top of whichever PRIOR_* base threshold is active.
    """
    if addressee_confidence is None:
        return 0.0
    try:
        conf = float(addressee_confidence)
    except (TypeError, ValueError):
        return 0.0
    conf = max(0.0, min(conf, 1.0))
    neutral = addressee_confidence_neutral()
    if conf >= neutral:
        return 0.0
    span = max(neutral, 1e-6)
    return ((neutral - conf) / span) * addressee_confidence_penalty_max()


def overlap_epsilon_seconds() -> float:
    """Conservative overlap gate: two different speakers' segment spans
    must overlap by MORE than this many seconds inside the open window to
    flip OVERLAP. Pure timestamp arithmetic — zero new models. Note the
    strict inequality: degenerate zero-length spans (no per-segment word
    timings from the STT event) can never flip it, even at epsilon 0."""
    return _get_float("LILY_OVERLAP_EPSILON_SECONDS", 0.3)


def clarify_max_per_session() -> int:
    """WO-ADDRESSEE-H1 Task 4: session-wide cap on band-triggered clarify
    questions — the repair stays charming, never bureaucratic."""
    return max(0, _get_int("LILY_CLARIFY_MAX_PER_SESSION", 3))


def tier1_clarify_margin() -> float:
    """Width of the ambiguous MIDDLE BAND below the active Tier-1
    threshold (Task 4 consumes this): similarity in
    [threshold - margin, threshold) is clarify territory; below it, the
    classification stands. See lily_evaluation.lily_tier1_band."""
    return _get_float("LILY_TIER1_CLARIFY_MARGIN", 0.15)


# ---------------------------------------------------------------------------
# Overlap addressee fusion (WO-LILY-CROSSTALK-FUSION)
# ---------------------------------------------------------------------------

def overlap_fusion_diarization_weight() -> float:
    """Weight of diarization confidence in overlap addressee fusion.
    Acoustic confidence receives the remaining mass (1 - weight)."""
    return max(
        0.0,
        min(1.0, _get_float("LILY_OVERLAP_FUSION_DIARIZATION_WEIGHT", 0.75)),
    )


def overlap_fusion_min_confidence() -> float:
    """Minimum fused confidence required to trust a rostered attribution
    inside PRIOR_OVERLAP. Below this, the segment is conservatively treated
    as open-floor (unrostered) rather than mis-attributed."""
    return max(
        0.0,
        min(1.0, _get_float("LILY_OVERLAP_FUSION_MIN_CONFIDENCE", 0.42)),
    )


def overlap_fusion_neutral_confidence() -> float:
    """Fallback confidence when no overlap-time confidence signal is
    available from either diarization or acoustics."""
    return max(
        0.0,
        min(1.0, _get_float("LILY_OVERLAP_FUSION_NEUTRAL_CONFIDENCE", 0.5)),
    )


def enroll_retry_cooldown_seconds() -> float:
    """WS-8: minimum spacing between under-threshold voiceprint enrollment
    retries. Long enough that a bound-but-quiet player accrues real words
    between GET_SPEAKERS round-trips; short enough that they cross the
    ~5-word floor within a normal round rather than never enrolling."""
    return max(0.0, _get_float("LILY_ENROLL_RETRY_COOLDOWN_SECONDS", 15.0))


def ghost_fold_window_seconds() -> float:
    """WS-8 ghost-label posture: an unbound single-utterance diarization
    label whose text duplicates a bound player's just-recorded answer
    within this window is a diarizer echo phantom (the max_speakers=7
    ceiling spawning S5/S6/S7 to absorb a reverberant copy) and folds
    instead of scoring. Bounds the window so a genuine second player
    legitimately repeating a short answer well after the fact is never
    swallowed."""
    return max(0.0, _get_float("LILY_GHOST_FOLD_WINDOW_SECONDS", 8.0))


# ---------------------------------------------------------------------------
# n-best ASR recovery (WO-LILY-ADDRESSEE-H1-001 Task 1)
# ---------------------------------------------------------------------------

def stt_max_alternatives() -> int:
    """Alternatives count injected into the Speechmatics StartRecognition
    transcription_config (per-word alternatives; there is no per-utterance
    n-best at plugin 1.6.6 — see lily_nbest.py). 1 disables the injection
    patch entirely. Bounded to the lily_nbest synthesis ceiling.

    DEFAULT IS 1 (OFF) — LIVE INCIDENT 2026-07-14 23:31 (session
    lily-B0CB8B-13a65381): the Speechmatics VOICE endpoint schema REJECTS
    the field at the protocol level ("Additional property
    max_alternatives is not allowed" -> websocket 1003 -> AgentSession
    unrecoverable close, ~8s into every session). The injection was
    defensive against plugin-shape drift but the rejection happens
    SERVER-side after the handshake, where no client-side fallback can
    catch it. Do not raise this above 1 until the injected config has
    been validated against the live voice-endpoint schema; the whole
    n-best pipeline no-ops cleanly at 1 (single-hypothesis sets)."""
    return max(1, min(_get_int("LILY_STT_MAX_ALTERNATIVES", 1), 8))


def garble_clarify_min_confidence() -> float:
    """WS-11 garble gate: a finalized answer whose MEAN per-word recognizer
    confidence falls below this over a multi-word utterance fires the light
    clarify posture instead of scoring the garble. Tap-only mode (default
    LILY_STT_MAX_ALTERNATIVES=1) synthesizes one hypothesis, so the
    dispersion gate reads 0.000 even on torn speech — this is the honest
    single-hypothesis backstop. Live garbled finals sat at 0.57–0.63 mean
    word confidence; clean answers at 0.85+."""
    return _get_float("LILY_GARBLE_CLARIFY_MIN_CONFIDENCE", 0.65)


def nbest_dispersion_threshold() -> float:
    """Confidence-variance threshold above which a definitive Tier-1
    verdict on an n-best set is demoted to "uncertain" (escalate to the
    Tier-2 judge — high dispersion is a deliberation signal, report
    Track 1). Variance of [0,1] confidences: tight sets sit under ~0.002,
    fractured ones above ~0.05."""
    return _get_float("LILY_NBEST_DISPERSION_THRESHOLD", 0.02)


# SFX assets — optional file paths; hooks are wired but silent when unset.
def thinking_bed_path() -> Optional[str]:
    """60 BPM ticking thinking-bed played during answer windows."""
    return _get("LILY_THINKING_BED_PATH")


def stinger_correct_path() -> Optional[str]:
    return _get("LILY_STINGER_CORRECT_PATH")


def stinger_incorrect_path() -> Optional[str]:
    return _get("LILY_STINGER_INCORRECT_PATH")


def session_log_dir() -> Optional[str]:
    """Optional per-session log file directory (fleet pattern). None disables."""
    return _get("LILY_SESSION_LOG_DIR")


def job_memory_limit_mb() -> float:
    """Explicit worker memory limit (1.6.4 memory-monitor hardening; monitor
    code unchanged at 1.6.6, limits stay explicit)."""
    return _get_float("LILY_JOB_MEMORY_LIMIT_MB", 2048.0)


# ---------------------------------------------------------------------------
# Audeering devAIce acoustic pipeline (WO-LILY-AUDEERING-001)
# ---------------------------------------------------------------------------
# Billing is audio-seconds, not per-module (JRVS Probe-C Q1 finding): the full
# module set costs 1× quota. All tunables are STARTING POINTS.

def audeering_api_key() -> Optional[str]:
    """Missing key opens the circuit breaker (best-effort pipeline; the
    session runs unaffected). Never required at boot."""
    raw = _get("AUDEERING_API_KEY")
    if raw is None:
        return None
    return raw.strip().strip('"').strip("'") or None


def audeering_max_uploads_per_session() -> int:
    """Hard cap on per-session uploads. 240 captures × 5s window / 60 =
    20 minutes of billable audio even on a runaway session (same 20-minute
    ceiling rationale as the JRVS donor cap)."""
    return _get_int("AUDEERING_MAX_UPLOADS_PER_SESSION", 240)


def audeering_window_seconds() -> float:
    """Capture window. MUST stay >=5s — the devAIce scene model is
    optimized for windows longer than 5 seconds (doc finding)."""
    return max(5.0, _get_float("AUDEERING_WINDOW_SECONDS_F", 5.0))


def audeering_capture_interval_seconds() -> float:
    return _get_float("AUDEERING_CAPTURE_INTERVAL_SECONDS", 5.0)


def audeering_min_snr_db() -> float:
    """Reliability gate: affect lines are suppressed under this SNR."""
    return _get_float("AUDEERING_MIN_SNR_DB", 12.0)


def audeering_snr_transit_adjust() -> float:
    """Transit scenes loosen the SNR bar (JRVS D2b pattern — scene feeds
    the reliability gate)."""
    return _get_float("AUDEERING_SNR_TRANSIT_ADJUST", -2.0)


def audeering_avd_smooth_window() -> int:
    """Rolling window (segments) for the room-level AVD smoother.
    Descriptors only — safety triggers run OUTSIDE the smoother."""
    return max(1, _get_int("AUDEERING_AVD_SMOOTH_WINDOW", 4))


def audeering_avd_neutral_band() -> float:
    """Per-axis neutral band: all axes inside it -> inject NOTHING."""
    return _get_float("AUDEERING_AVD_NEUTRAL_BAND", 0.15)


def audeering_child_halt_threshold_high() -> float:
    return _get_float("AUDEERING_CHILD_HALT_THRESHOLD_HIGH", 0.85)


def audeering_child_halt_threshold_borderline() -> float:
    return _get_float("AUDEERING_CHILD_HALT_THRESHOLD_BORDERLINE", 0.5)


def audeering_child_halt_sustained_n() -> int:
    return max(1, _get_int("AUDEERING_CHILD_HALT_SUSTAINED_N", 2))


def _get_bool(name: str, default: bool) -> bool:
    raw = _get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "off", "no", "")


def audeering_child_halt_enabled() -> bool:
    """Lily default TRUE (JRVS shipped these false pending an action
    surface; Lily HAS the action surface — the adult-mode veto — so the
    ladder ships armed)."""
    return _get_bool("AUDEERING_CHILD_HALT_ENABLED", True)


def audeering_child_step_up_enabled() -> bool:
    """Borderline tier — also TRUE for Lily (veto-only, both tiers)."""
    return _get_bool("AUDEERING_CHILD_STEP_UP_ENABLED", True)


def architect_mode() -> bool:
    """Server-authenticated operator override for controlled testing.

    This can only be enabled through deployment configuration; a player
    saying "I'm the architect" never changes it.
    """
    return _get_bool("LILY_ARCHITECT_MODE", False)


# ---------------------------------------------------------------------------
# Dereverberation node (WO-LILY-OMNIBUS-003 WS-16 / AMENDMENT-002)
# ---------------------------------------------------------------------------

def dereverb_node_mode() -> str:
    """Pre-STT dereverberation node: "off" (default) | "wpe" | "aic".

    DEFAULT OFF — enabling is gated on the WS-16 decision memo and operator
    sign-off. Unknown values resolve to "off"."""
    raw = (_get("LILY_DEREVERB_NODE") or "off").strip().lower()
    return raw if raw in ("off", "wpe", "aic") else "off"
