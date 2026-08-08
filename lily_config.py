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


def google_api_key_present() -> bool:
    """Non-raising presence check for the picture-lane grounding read
    (PATCH-003 P4) — google_api_key() raises, which the state-block read
    must never do."""
    return bool(_get("GOOGLE_API_KEY"))


def vocal_model() -> str:
    # gemini-3.6-flash (operator-directed upgrade 2026-08-06): 1M ctx,
    # function-calling + structured outputs + thinking, text-mode via the
    # LiveKit google plugin (NOT Gemini Live). Live-verified: chat + tool
    # call status:ok on the funded GOOGLE_API_KEY. Image gen is a SEPARATE
    # pin (imagegen_model) — 3.6-flash does NOT do image generation.
    return _get("LILY_VOCAL_MODEL", "gemini-3.6-flash")


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
    """Gemini image model for STANDARD-deck invented-content picture
    questions (sub-agent J; image_source='generated' only, prefetch-time
    only). gemini-3.1-flash-lite-image = Nano Banana 2 Lite (operator-
    directed 2026-08-06): fastest/cheapest for fun trivia cards, 1K.
    Live-verified via generate_content on the funded GOOGLE_API_KEY."""
    return _get("LILY_IMAGEGEN_MODEL", "gemini-3.1-flash-lite-image")


def adult_vocal_model() -> str:
    """Vocal LLM for ADULT mode (owner directive 2026-08-06). Gemini's
    non-overridable PROHIBITED_CONTENT filter blocks spoken turns around
    adult-deck material (live: the Kama Sutra answer at 21:33 — four
    blocked generations, ~58s of retry stall), so on adult entry the
    session's vocal LLM swaps to xAI Grok (the fleet's established
    adult-content provider: vision + adult imagegen already ride
    XAI_API_KEY) and swaps back on every adult exit."""
    return _get("LILY_ADULT_VOCAL_MODEL", "grok-4.5")


def adult_vocal_effort() -> str:
    """Grok reasoning effort for the front-facing adult VOCAL swap
    (grok-4.5). On grok-4.5 this is a thinking-DEPTH dial (low/medium/high).

    HOTFIX-005 X13 + operator directive 2026-08-07: the vocal lane is
    latency-critical — a player waits through every token — and running it
    at "high" cost ~5s to first token (session lily-FFDEAE: llm_ttft p50
    4,999ms, p95 8,254ms; "I've been a half-beat behind you tonight").

    Operator directive 2026-08-08: dropped again, medium -> LOW. Session
    lily-CE6FF4 on a starved slot showed the vocal lane losing races it
    cannot afford to lose (a direct address unanswered past the 3.0s
    budget at 12:52:58, VAD 33s behind realtime alongside it). Thinking
    depth is the cheapest thing to give back when the slot is contended —
    character lives in the prompt and the voice, not in the extra tokens.
    Set LILY_ADULT_VOCAL_EFFORT=medium or =high to restore depth once the
    slot has headroom. Anything else coerces to low."""
    effort = (_get("LILY_ADULT_VOCAL_EFFORT", "low") or "").lower()
    return effort if effort in ("low", "medium", "high") else "low"


def adult_vocal_read_timeout() -> float:
    """HTTP READ timeout for the adult vocal lane's streaming turn.

    livekit-plugins-openai defaults to read=5.0s and `with_x_ai` exposes no
    timeout, so the adult lane inherited a five-second wall. On a streaming
    response the read timeout is the gap BETWEEN chunks, and the first gap
    is the model's thinking time — so on grok-4.5, a reasoning model
    measured at llm_ttft p50 4,999ms / p95 8,254ms (HOTFIX-005 X13), the
    default killed the connection at the median and always at p95. With the
    plugin's max_retries=0 that is not a slow turn, it is dead air.

    30s is a BACKSTOP, not a latency control — latency is governed by
    adult_vocal_effort(), which is `low`. It sits far above p95 so a
    legitimately slow turn completes, and far below "the table has given up
    and started talking about something else" so a genuinely wedged socket
    still fails. Raise it only if a real turn is being cut; do not raise it
    to paper over a slow effort tier."""
    return max(5.0, _get_float("LILY_ADULT_VOCAL_READ_TIMEOUT", 30.0))


def adult_reasoning_model() -> str:
    """Question/verification generation model for the ADULT deck.

    HOTFIX-005 X2: the id is `grok-4.20-multi-agent` (operator-supplied).
    The former default `grok-4.2` was TRUNCATED and does not exist — it
    400'd 'Model not found: grok-4.2' every prefetch (the 67s dead-air at
    14:33). This model speaks the xAI Responses API only (lily_reasoning
    routes `*-multi-agent` there); Chat Completions returns 400. Generation
    failures stay visible and fall back to the bank, never silent."""
    return _get("LILY_ADULT_REASONING_MODEL", "grok-4.20-multi-agent")


def adult_reasoning_effort(override: Optional[str] = None) -> Optional[str]:
    """Reasoning effort for adult question generation. On
    grok-4.20-multi-agent this is an AGENT-COUNT dial, not thinking depth:
    low/medium = 4 agents, high/xhigh = 16 agents (operator docs) — a 4x
    fan-out on latency AND spend.

    Operator directive 2026-08-08: default raised low -> MEDIUM. Note what
    this does and does not buy: per the vendor's own mapping low and medium
    are BOTH 4-agent, so this is not a fan-out change and costs neither
    latency nor spend — the 4x cliff is at `high`. It is a quality dial
    inside the same agent budget, which is exactly why it is safe to raise
    on the same day the vocal lane is being dropped to `low`.

    `xhigh` is ACCEPTED but its exact agent-count/cost mapping is
    UNCONFIRMED against the vendor (X13 open item) — treat it as ≥16-agent
    and do not enable it until measured.

    INJECTABLE (operator directive 2026-08-08): `override` lets a CALLER
    pin the effort for one call instead of every lane reading the same
    global. That matters because the lanes have genuinely different
    economics — a live prefetch a player is waiting on wants the cheap
    tier, while out-of-session corpus building (the arsenal seeding job)
    can absorb `high` because nobody is waiting on it. An unrecognised
    override falls through to the configured default rather than raising:
    a bad string must never take a question lane down. "off" (from either
    source) disables the parameter entirely, for model ids that reject
    it."""
    raw = (override if override is not None else
           _get("LILY_ADULT_REASONING_EFFORT", "medium")) or ""
    effort = raw.strip().lower()
    if effort in ("off", "none", "0"):
        return None
    if effort in ("low", "medium", "high", "xhigh"):
        return effort
    if override is not None:
        # Unrecognised injection — fall back to the configured value rather
        # than silently substituting a tier the operator never chose.
        return adult_reasoning_effort()
    return "medium"


def adult_imagegen_model() -> str:
    """Image model for the ADULT deck. Gemini refuses adult content, so the
    adult picture path routes to xAI Grok Imagine (grok-imagine-image, via
    xai_api_key()). Live-verified: POST /v1/images/generations returns a
    url. Adult picture-trivia is LIVE upstream
    (lily_reasoning.prefetch_picture_question threads mode='adult' to the
    picture builders); this pin routes those generated images to Grok."""
    return _get("LILY_ADULT_IMAGEGEN_MODEL", "grok-imagine-image")


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


def interruption_min_duration() -> float:
    """Minimum player-speech duration that can barge into Lily.

    Quiz answers are often a single short word ("B", "Mars", "Hydrogen").
    The previous 0.8s threshold excluded many of them before the adaptive
    detector could make its interruption decision. Keep a small acoustic
    floor for clicks/coughs; false triggers still use the framework's
    pause-and-resume path below.
    """
    return max(0.1, _get_float("LILY_INTERRUPTION_MIN_DURATION", 0.25))


def false_interruption_timeout() -> float:
    """Seconds of post-trigger silence before an interruption with no
    transcript is classified FALSE and the paused speech resumes from its
    pause point (framework default 2.0). The 4-player echo room's noise
    bursts land here: pause-and-resume instead of a hard cut."""
    return _get_float("LILY_FALSE_INTERRUPTION_TIMEOUT", 2.0)


def stt_min_endpointing_delay() -> float:
    """HOTFIX-005 X9: minimum silence the session waits before committing a
    user turn (LiveKit endpointing `min_endpointing_delay`, framework
    default 0.5s).

    The runtime named both the defect and its remedy live at 14:33:18:
    `transcript arrives after turn has been committed. consider raising
    min_delay in the endpointing options to accommodate a slow stt.` The
    enhanced Speechmatics point runs a `max_delay` of 1.5s (STT-001), so a
    0.5s endpointing floor commits the turn before the final transcript
    lands — splitting one utterance across the boundary. Raise the floor to
    0.6s so a slow-but-not-pathological STT delivers inside the window; the
    max endpointing delay still bounds the wait. Set WITH the Speechmatics
    max_delay, never against it (STT-001 coordination)."""
    return max(0.1, _get_float("LILY_STT_MIN_ENDPOINTING_DELAY", 0.6))


def stt_max_endpointing_delay() -> float:
    """HOTFIX-005 X9: the ceiling on the endpointing wait (LiveKit
    `max_endpointing_delay`, framework default 6.0s). Pinned explicitly so
    the min-floor raise above can never be read as also loosening the
    ceiling; the ceiling is unchanged from the framework default."""
    return _get_float("LILY_STT_MAX_ENDPOINTING_DELAY", 6.0)


def cut_recovery_grace() -> float:
    """Seconds a cut/failed organic turn waits for a natural follow-up
    before Lily auto-resumes it herself (WO-LILY-STREAM-INTEGRITY-002
    WS-3). Must sit ABOVE false_interruption_timeout so the framework's own
    pause-and-resume gets first crack, and above healthy user-turn latency
    so a real barge-in's reply supersedes the auto-resume (no double-speak).
    Below it, a cut that leaves dead air self-heals without an operator
    poke."""
    return _get_float("LILY_CUT_RECOVERY_GRACE", 3.5)


def noise_cancellation_mode() -> str:
    """Krisp noise cancellation on the room input: "off" (DEFAULT) or
    "nc" (ambient model, opt-in via slot secret).

    Default flipped nc->off by WO-LILY-HOTFIX-001/WO-LILY-NC-BENCH-001:
    the 08-06 P0 (four consecutive sessions opened deaf and mute — RoomIO
    audio setup wedged, greet never reached playout, zero mic frames)
    joined the 1.6.4 NcSession sample-rate SIGABRT as NC's second
    documented kill. NC's entire production record is failed sessions; it
    returns ONLY by passing the NC-BENCH-001 cold-join gate on an
    isolated slot, then explicit LILY_NOISE_CANCELLATION=nc. A safety
    default must fail to silence-of-the-feature, never silence-of-the-
    agent. BVC is NOT a value: in a one-mic multiplayer room the
    "background voices" are the other players — BVC would erase the
    table. Any unknown value (including "bvc") coerces to "off"."""
    mode = _get("LILY_NOISE_CANCELLATION", "off")
    return mode if mode in ("nc", "off") else "off"


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


def late_answer_grace_seconds() -> float:
    """WO-LILY-HOTFIX-006 N9: the STATED grace margin on the answer
    window. Speech spoken up to this many seconds past the deadline still
    counts — "just a split second late" is a defined outcome, not a coin
    flip against the expiry task's scheduling.

    Live evidence, 2026-08-08 21:10:13: Rami said "Okay. It's Jupiter."
    and Lily replied "Jupiter was spot on, Rami, but just a split second
    late!" — while the ledger recorded a DIFFERENT utterance ("Go.") as
    his answer, marked incorrect. Beyond this margin the miss is announced
    with its reason and audited as cause="late_answer"; what may never
    happen again is a correct answer disappearing silently."""
    return _get_float("LILY_LATE_ANSWER_GRACE_SECONDS", 1.5)


def hold_timeout_seconds() -> float:
    """PATCH-002 A4: how long the hold state (a decline/wait/'take your
    time') binds every dispatch lane before it lifts on its own. Generous
    by design — the point is to yield to the table, not to time them out
    quickly. User speech or a hard game event releases it sooner."""
    return _get_float("LILY_HOLD_TIMEOUT_SECONDS", 90.0)


def responsiveness_budget_seconds() -> float:
    """PATCH-003 P9: a direct address gets a response within this budget;
    past it, unanswered silence trips the ADDRESS_UNANSWERED warn (the
    'Hey... Hello?' -> 34s silence fixture). ~3s target."""
    return _get_float("LILY_RESPONSIVENESS_BUDGET_SECONDS", 3.0)


# -- device-independent voice identity (WO-LILY-VOICE-IDENTITY-001) -----------
# The durable speaker-embedding recognizer that closes the "know my voice"
# gap the device-linked group_id can't (Speechmatics refreshes its blobs per
# session). Inert until BOTH the embedder dependency is present in the image
# AND the lily_voice_identity table exists — every consumer checks
# voice_identity_enabled() and the embedder's availability first, so a deploy
# without the model or the DDL runs exactly as today.


def voice_identity_enabled() -> bool:
    """Master switch for durable voice recognition. Default ON (operator
    decision: enroll-by-default, opt-out) — but the feature still no-ops
    unless the embedder model is installed and the identity table exists,
    so flipping this alone never risks a session."""
    return _get_bool("LILY_VOICE_IDENTITY_ENABLED", True)


def voice_identity_model_tag() -> str:
    """Provenance tag pinned to the embedding space (model + dim). Matching
    only ever compares centroids sharing this tag, so a model swap can never
    compare across incompatible embedding spaces — it starts a fresh pool."""
    return _get("LILY_VOICE_IDENTITY_MODEL_TAG", "ecapa-192-v1")


def voice_identity_match_min_speech_seconds() -> float:
    """Speech needed before RECOGNITION is attempted — deliberately far
    below the enrollment minimum.

    Enrollment folds a sample into a stored centroid and wants a long clean
    take. Recognition only has to clear a cosine threshold, which ECAPA
    does on a couple of seconds. Sharing one floor made a returning player
    wait for an enrollment-grade sample before the match could even be
    tried; live 2026-08-08 the match landed correctly 3m36s into the
    session, long after the greeting had called a four-win regular a blank
    slate."""
    return max(1.0, _get_float("LILY_VOICE_IDENTITY_MATCH_MIN_SPEECH", 2.5))


def voice_identity_match_threshold() -> float:
    """Absolute cosine floor for a confident voice match. Conservative: a
    false merge (greeting a stranger by a housemate's name) is far costlier
    than a miss, which degrades to 'ask who's playing'."""
    return _get_float("LILY_VOICE_IDENTITY_MATCH_THRESHOLD", 0.75)


def voice_identity_match_margin() -> float:
    """The winning candidate must beat the runner-up by at least this, or
    the match is withheld as ambiguous (multi-person household safety)."""
    return _get_float("LILY_VOICE_IDENTITY_MATCH_MARGIN", 0.06)


def voice_identity_enroll_min_speech_seconds() -> float:
    """A player is enrolled at session close only above this much captured
    speech — short utterances yield noisy embeddings that would blur the
    centroid."""
    return _get_float("LILY_VOICE_IDENTITY_ENROLL_MIN_SPEECH_SECONDS", 8.0)


def paraphrase_repeat_threshold() -> float:
    """PATCH-002 A4: cosine-ish token-overlap ratio above which a
    consecutive agent turn counts as a semantic repeat of a recent one
    (the reassurance-storm class — three ways of saying 'take your time').
    Deliberately high so only genuine restatements flag."""
    return _get_float("LILY_PARAPHRASE_REPEAT_THRESHOLD", 0.6)


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


def custom_round_build_seconds() -> float:
    """How long lily_set_category may block while the requested round is
    ACTUALLY built (WO-LILY-HOTFIX-006 N2).

    The live session lily-16A9AE is why this wait exists at all. The tool
    used to return "I'm generating those questions now" the instant it was
    called, so the confirmation was produced from the operator's own words
    and nothing else; underneath, the supply path served the generic deck.
    A confirmation that is a claim about the ledger has to WAIT for the
    ledger, so the build is awaited and only its result can speak.

    The wait is the price of the honesty, so it is bounded: past this
    budget the build is abandoned, the topic override rolls back, and she
    refuses plainly. One generate+verify pair on the reasoning node runs
    well inside 40s at p95 (PREFETCH_TIMEOUT_SECONDS is 30s per leg), and a
    dead provider therefore costs one bounded pause, never the table."""
    return _get_float("LILY_CUSTOM_ROUND_BUILD_SECONDS", 40.0)


def participant_metadata_wait_seconds() -> float:
    """How long the group-id resolver polls for a participant's
    `lily_group_id` token metadata before minting a throwaway group.

    This is a PROPAGATION window, not a "will the value change" window —
    token metadata is fixed at join, but it reaches the agent
    asynchronously, and a participant object can land in
    room.remote_participants a beat before its metadata field syncs. On a
    slow-booting or CPU-contended agent (model loads, frame sinks) that
    beat stretches, and every session that loses the race is greeted as a
    stranger with its real history sitting untouched in the database.

    3.0s is the shipped default. Raise it on a deployment that boots slow;
    the greeting memory budget still bounds how long a cold greeting
    waits, so this cannot strand a table in silence."""
    return max(0.5, _get_float("LILY_PARTICIPANT_METADATA_WAIT", 3.0))


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


def google_grounding_enabled() -> bool:
    """Additive Google Search grounding (Gemini's built-in google_search
    tool) alongside Exa/Tavily — an EXTRA reasoning-node source, never a
    replacement. On by default when a Google key is present; set
    LILY_GOOGLE_GROUNDING=off to disable. Reasoning-node only (same web
    guardrail as Exa/Tavily — never the vocal path)."""
    return _get_bool("LILY_GOOGLE_GROUNDING", True) and bool(_get("GOOGLE_API_KEY"))


def google_grounding_model() -> str:
    """Model for the Google Search grounding call. Gemini 3.x supports the
    google_search tool; defaults to the flash vocal model (fast + cheap for
    the reasoning node's grounding calls). Grounding runs OFF the vocal
    path — this is just the model id it targets."""
    return _get("LILY_GOOGLE_GROUNDING_MODEL", vocal_model())


def url_context_enabled() -> bool:
    """Additive URL-context reading (Gemini's built-in url_context tool) —
    the model fetches and reasons over URLs embedded in a prompt (up to 20
    public URLs). Another EXTRA reasoning-node capability, never a
    replacement. On by default when a Google key is present; set
    LILY_URL_CONTEXT=off to disable."""
    return _get_bool("LILY_URL_CONTEXT", True) and bool(_get("GOOGLE_API_KEY"))


def stt_focus_mode() -> str:
    """WO-LILY-STT-001 Q0 — Speechmatics speaker focus. "off" (DEFAULT) keeps
    every voice transcribed; "ignore" wires the enrolled players into
    focus_speakers with focus_mode=IGNORE, so speech from UNENROLLED voices
    is dropped at the engine (never transcribed, never triggers
    end-of-utterance, never reaches context) — enrolled = players, everything
    else = room.

    DEFAULT OFF, deliberately: focus_mode=IGNORE can SILENTLY DELETE a valid
    player whose live voice drifts from their calm enrollment print (Lombard
    effect / shouting / intoxication push the embedding away, so a real
    player reads as a bystander). It ships inert until that shouted-utterance
    acceptance risk is measured on the captured fixtures, and it NEVER
    activates with an empty enrolled set (that would delete the whole
    table) — both guards enforced at the construction site."""
    v = (_get("LILY_STT_FOCUS_MODE", "off") or "").strip().lower()
    return v if v in ("off", "ignore") else "off"


def stt_roster_retune_enabled() -> bool:
    """HOTFIX-005 X8 — roster-aware max_speakers retune at game start.

    The Speechmatics `max_speakers` cap is a StartRecognition-time value;
    the plugin has no in-flight setter, so shrinking it to a small table's
    size (solo ⇒ roster+1 = 2, killing the phantom [S2] that spoke in a
    single-player session) requires a full live STT swap via
    `Agent.update_options(stt=...)` — a fresh StartRecognition, i.e. a
    reconnect.

    DEFAULT OFF, deliberately: a live STT swap at game start reconnects the
    recognition websocket, and the ghost-speaker WS previously chose NOT to
    wire it for that reason. The mechanism ships inert here so STT-001 Q4
    can activate it once the reconnect is validated on the captured
    fixtures. The roster-aware cap math (`lily_max_speakers_for`) is proven
    by fixture regardless of this flag."""
    return (_get("LILY_STT_ROSTER_RETUNE", "off") or "").strip().lower() in (
        "on", "true", "1", "yes"
    )


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


def adult_deck_gate_mode() -> str:
    """Adult-deck availability gating: "open" (DEFAULT) or "sensor".

    Owner directive 2026-08-06: the deck is open by default — the
    Audeering child-signal sensor is NOT the gate (its age estimation is
    unreliable in live rooms per the owner's own testing, and the lane
    has been quota-blocked fleet-wide since WS-11, which made the deck
    permanently unavailable in production). The spoken 18+ opt-in
    ceremony REMAINS required in open mode — every player confirms
    aloud before the deck switches on; only the sensor coupling is
    removed. "sensor" restores the legacy fail-closed one-unit coupling
    (deck available only while the acoustic pipeline is live), and an
    ACTIVE child veto still blocks entry in every mode whenever the
    sensor happens to be running."""
    mode = _get("LILY_ADULT_DECK", "open")
    return mode if mode in ("open", "sensor") else "open"


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


# ---------------------------------------------------------------------------
# Standing picture arsenal (WO-LILY-ARSENAL-SEED-001)
#
# Depth is CONFIGURATION, never a redeploy. The bank is a standing spend
# against the xAI account rather than a per-game cost, so the operator sets
# how deep it runs with the cost figure in front of him — and changes it
# from the environment when a game night calls for more.
# ---------------------------------------------------------------------------

def arsenal_target_depth(partition: Optional[str] = None) -> int:
    """Ready entries the bank holds per partition.

    Default 10 — the earlier standing ruling. The operator has since
    floated 5; either is one env var away, which is the whole point of
    this being a knob. A per-partition override wins over the global one,
    so `general` can run deep (it plays at every table) while an expensive
    or moderation-heavy adult partition runs shallower:

      LILY_ARSENAL_TARGET_DEPTH=10
      LILY_ARSENAL_TARGET_DEPTH_ADULT_EXPLICIT=5

    Floors at 1: a zero-depth arsenal is just the empty shelf again."""
    if partition:
        specific = _get_int(
            f"LILY_ARSENAL_TARGET_DEPTH_{partition.strip().upper()}", -1
        )
        if specific > 0:
            return specific
    return max(1, _get_int("LILY_ARSENAL_TARGET_DEPTH", 10))


def arsenal_replenish_ratio() -> float:
    """Fraction of a partition's target that must be CONSUMED before
    background replenishment fires — 0.40 (40%) per the work order,
    tracked per partition independently.

    At depth 10 that is the 4th entry served; at depth 5, the 2nd. Stating
    it as a ratio rather than a count is what keeps the watermark correct
    when the operator changes depth: a hardcoded "fire at 4" silently
    becomes "fire after the bank is already 80% gone" at depth 5.
    Clamped to (0, 1] — 0 or negative would fire the refill forever."""
    raw = _get_float("LILY_ARSENAL_REPLENISH_RATIO", 0.40)
    if raw <= 0.0 or raw > 1.0:
        return 0.40
    return raw


def arsenal_gate_mode(partition: Optional[str] = None) -> str:
    """Quality gate for entries landing in a partition: 'auto' or 'review'.

    'auto'   — the outbound classifier plus the automated checks promote
               an entry from 'generating' to 'ready'.
    'review' — nothing serves until the operator passes it. Entries sit at
               'generating' and the draw cannot see them.

    ADULT PARTITIONS DEFAULT TO 'review'. The first batch sets the bar for
    the whole bank, and a bad explicit image reaching a table is expensive
    in a way a bad picture of a lighthouse is not. `general` defaults to
    'auto'. Override per partition without a deploy:

      LILY_ARSENAL_GATE_MODE_ADULT_EXPLICIT=auto
      LILY_ARSENAL_GATE_MODE=review          # everything, including general
    """
    if partition:
        specific = (
            _get(f"LILY_ARSENAL_GATE_MODE_{partition.strip().upper()}") or ""
        ).strip().lower()
        if specific in ("auto", "review"):
            return specific
    blanket = (_get("LILY_ARSENAL_GATE_MODE") or "").strip().lower()
    if blanket in ("auto", "review"):
        return blanket
    return "auto" if (partition or "general") == "general" else "review"


def arsenal_image_cost_usd() -> float:
    """Billed cost of ONE generated arsenal image, in USD, used to price a
    seeding run before the operator commits to a depth (A10).

    This is a PRICE SHEET value, not a measurement — the provider does not
    return per-image cost on the images endpoint, so the run summary
    multiplies attempts by this. Keep it in step with the xAI price sheet:

      LILY_ARSENAL_IMAGE_COST_USD=0.02

    A rejected generation still bills, so the run's cost counts ATTEMPTS,
    not banked entries."""
    return max(0.0, _get_float("LILY_ARSENAL_IMAGE_COST_USD", 0.02))


def arsenal_moderation_retries() -> int:
    """How many times a moderation-rejected prompt is REWORKED and retried
    before the seeding job skips that slot and moves on (A9).

    Provider moderation refusing an explicit prompt is an EXPECTED outcome
    at seeding time, not an error — on record:
    `xAI image HTTP 400: Generated image rejected by content moderation`.
    Bounded because an unbounded retry against a prompt the provider will
    never paint just burns the account. 0 disables reworking entirely."""
    return max(0, _get_int("LILY_ARSENAL_MODERATION_RETRIES", 2))


def arsenal_real_images_enabled() -> bool:
    """Whether the 'real or imagined' format may be seeded.

    That format needs BOTH halves in the bank — a real photograph and a
    generated one — and the real half is web-sourced through Exa, not
    generated. With no Exa key the format cannot be honestly built, so it
    is excluded from the seeding plan rather than half-built. Defaults on
    only when a key exists; force it off with
    LILY_ARSENAL_REAL_IMAGES=0."""
    raw = (_get("LILY_ARSENAL_REAL_IMAGES") or "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    return bool(exa_api_key())
