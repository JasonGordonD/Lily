"""
lily_agent.py — LILY: multi-player voice trivia host. Entrypoint + agent.

One node. Lily is a single LiveKit agent; game flow is managed inside the
prompt via a state block, not node transfers. Deliberate inversion of the
Lovebirds architecture: Lily speaks by default — no generation gate, no
trigger loop, no watchdog. The only hard logic outside the LLM is the
scorekeeper, the answer-window timer, SFX dispatch, checkpointing, and the
deterministic enforcement of the sticky player commands ("skip",
"back to normal").

Dual-brain LLM binding: front-facing vocal node Grok 4.5; background
reasoning remains isolated in lily_reasoning and migrates independently.
"""

import asyncio
import datetime
import hashlib
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Optional

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent / ".env", override=False)

from livekit import rtc, api
from livekit.agents import (
    APIConnectionError,
    APIConnectOptions,
    APIStatusError,
    AutoSubscribe,
    EndpointingOptions,
    InterruptionOptions,
    JobContext,
    RunContext,
    TurnHandlingOptions,
    WorkerOptions,
    WorkerType,
    StopResponse,
    cli,
    function_tool,
)
from livekit.agents.llm import ChatMessage
from livekit.agents.types import NOT_GIVEN
from livekit.agents.utils import is_given
from livekit.agents.voice import Agent, AgentSession
from livekit.agents.voice.agent_session import SessionConnectOptions
from livekit.agents.voice.room_io import AudioInputOptions, RoomOptions
from livekit.agents.voice.agent_activity import _SpeechHandleContextVar
from livekit.agents.voice.background_audio import (
    AudioConfig,
    BackgroundAudioPlayer,
)
from livekit.agents.voice.events import UserInputTranscribedEvent
from livekit.plugins import noise_cancellation, silero
from livekit.plugins.speechmatics import (
    AdditionalVocabEntry,
    SpeakerFocusMode,
    SpeakerIdentifier,
    TurnDetectionMode,
)

import lily_addressee
import lily_addressee_classifier
import lily_arsenal
import lily_assessment
import lily_audeering_client
import lily_audeering_consumers
import lily_bank
import lily_bank_tuning
import lily_capabilities
import lily_config
import lily_dereverb
import lily_evaluation
import lily_forget
import lily_memory
import lily_metrics
import lily_nbest
import lily_persistence
import lily_reasoning
import lily_game_control
import lily_say_gate
import lily_speech_delivery
import lily_transition
import lily_glass
import lily_floor
import lily_identity
import lily_supply
import lily_stt_tuning
from lily_speechmatics import LilySpeechmaticsSTT
from lily_binding import (
    LilyFragmentAccumulator,
    lily_extract_explicit_name,
    lily_is_valid_name,
    lily_names_probably_same,
)
from lily_reasoning import LilyReasoning
import lily_scorekeeper
from lily_scorekeeper import LilyScorekeeper, lily_detect_state_contradiction
import lily_images
import lily_vision
from lily_tts import LilyTTS, lily_prewarm_tts_connection
from lily_vision import lily_analyze_image
from lily_voice_switch import lily_list_voices, lily_switch_voice
import lily_voice_embedder
import lily_voice_identity

logger = logging.getLogger("lily_agent")

_PROMPTS_DIR = Path(__file__).parent / "prompts"
# Loader-level rubric append (WO-LILY-AUDEERING-001 Task 3): the room-read
# rubric is a separate prompt file, zero-scalar-linted at import of
# lily_audeering_consumers, appended here so lily_system.txt itself stays
# untouched.
LILY_SYSTEM_PROMPT = (
    (_PROMPTS_DIR / "lily_system.txt").read_text(encoding="utf-8")
    # Y1a: the appended rubric is its own addressable section, same as
    # every section in the file. Both inputs are static files/constants —
    # the assembled system prompt is byte-identical every turn by
    # construction (the cacheable prefix, Y1b).
    + "\n<room_read>\n"
    + lily_audeering_consumers.lily_audeering_rubric_block().strip("\n")
    + "\n</room_read>\n"
)
LILY_ADULT_LAYER = (_PROMPTS_DIR / "layer_lily_adult.md").read_text(encoding="utf-8")
_ADULT_LAYER_MARKER = "# ADULT MODE"
_STATE_BLOCK_MARKER = "[GAME STATE]"

CATEGORY_FAMILIES = ["academic", "pop culture", "wordplay", "lifestyle-potpourri"]
# Adult-deck round families (WO-LILY-DESYNC-HONESTY-001 D): the migration-014
# bank rows carry these as their OWN categories (adult_couples, adult_kink) —
# the general round-family rotation must never overwrite or announce over
# them (live defect: adult questions introduced as "academic category").
ADULT_CATEGORY_FAMILIES = ["adult_couples", "adult_kink"]


def lily_custom_round_line(result) -> str:
    """The ONE producer of anything Lily says about a requested custom round
    (WO-LILY-HOTFIX-006 N2).

    It takes a REGISTRATION RESULT — the dict LilyGame.build_custom_round
    returns after running the real supply path — and there is exactly one
    branch that produces a confirmation: the one where questions actually
    registered under the topic. Every other input, including a malformed or
    missing result, produces the refusal. That is what makes narrating a
    round that does not exist unproducible rather than merely discouraged.

    Live evidence this replaces (session lily-16A9AE): "I'm putting together
    a custom round all about Cape Cod for you right now", spoken while the
    engine had drawn Psycho, and again "I'm putting your round together right
    now" with still nothing built. Both sentences were generated from the
    operator's request and no other input, which is exactly the thing a
    result-derived line cannot do."""
    subject = str((result or {}).get("category") or "").strip()
    registered = list((result or {}).get("registered") or [])
    if not registered:
        topic = f"{subject} " if subject else ""
        # The refusal names what she CAN do. A bare "no" is honest but
        # useless, and a useless refusal is what tempts the next fiction.
        return (
            f"NOTHING WAS BUILT — say this plainly, in your own voice: "
            f"\"I can't build a custom {topic}round right now — want me to "
            "pull a lane from the deck instead?\" Zero questions are "
            f"registered for {subject!r}. Do NOT say you are building it, "
            "putting it together, or working on it; do not ask a question "
            "you made up about it; do not run the round under that name."
            if subject else
            "NOTHING WAS BUILT — say this plainly: \"I can't build a custom "
            "round right now — want me to pull a lane from the deck "
            "instead?\" Do NOT claim you are building one."
        )
    return (
        f"BUILT AND REGISTERED: {len(registered)} question(s) are in the "
        f"ledger under {subject!r} and the first one is in the state block. "
        f"Tell the table their {subject} round is ready and ask that "
        "question as written — nothing else about this round is true yet."
    )


# A round/questions claim. The topic alone is never the offence — she is free
# to chat about Cape Cod — so the detector needs the CLAIM in the same
# sentence as the topic before it calls anything a divergence.
_CUSTOM_ROUND_CLAIM_RE = re.compile(r"\b(rounds?|questions?)\b", re.I)


def lily_narrated_custom_round_divergence(text, unbuilt) -> str | None:
    """The X1 safety net for custom rounds (WO-LILY-HOTFIX-006 N2).

    HOTFIX-005 X1 puts the committed score in the state block AND logs
    SCORE_DIVERGENCE when a spoken turn narrates a number that is on no
    ledger — the field prevents, the log makes a prevention failure loud.
    Same construction here: the tool result and the state block feed her the
    registration truth; this catches a turn that claimed a round for a topic
    with nothing registered under it, which is verbatim what lily-16A9AE
    aired twice ("I'm putting together a custom round all about Cape Cod for
    you right now").

    Returns the offending topic, or None. Pure — no state, no raising."""
    if not text or not unbuilt:
        return None
    lowered = str(text).lower()
    for topic in unbuilt:
        needle = str(topic or "").strip().lower()
        if not needle or needle not in lowered:
            continue
        for sentence in re.split(r"[.!?]+", lowered):
            if needle in sentence and _CUSTOM_ROUND_CLAIM_RE.search(sentence):
                return topic
    return None


# Adaptive thinking (operator rule 2026-08-06): the vocal model runs LOW by
# default for fast hosting patter, and ESCALATES to HIGH on turns that need
# real reasoning — rules disputes, adjudication challenges, ambiguity,
# multi-step/long requests. (Content GENERATION — categories/questions — runs
# HIGH in the reasoning node, a separate model/call-site.) Gemini 3.x accepts
# only 'low'/'high'.
_COMPLEX_TURN_RE = re.compile(
    r"\b(wrong|isn'?t right|that'?s not|not correct|incorrect|actually|"
    r"why|how come|rule|rules|cheat|cheating|unfair|doesn'?t count|"
    r"does not count|dispute|disput\w+|challenge|overrule|reconsider|"
    r"object|explain|clarify|confus\w+|what if|too hard|too easy|redo|"
    # multi-step / multi-constraint requests (e.g. "... but only the "
    # emperors and also skip the obscure ones")
    r"and also|but only|but not|except for|only the)\b",
    re.IGNORECASE,
)


def _lily_thinking_level_for_text(text: str) -> str:
    """LOW for short reflexive banter; HIGH when a turn needs genuine
    reasoning (dispute/adjudication/ambiguity/multi-step). Bias HIGH on
    clearly non-trivial turns; stay LOW otherwise. Pure + testable."""
    t = (text or "").strip()
    if not t:
        return "low"
    if _COMPLEX_TURN_RE.search(t):
        return "high"
    # Long or multi-question turns carry real complexity.
    if len(t) > 140 or t.count("?") >= 2:
        return "high"
    return "low"


def lily_llm_chunk_signal(chunk) -> tuple[int, int]:
    """Return ``(text_chars, tool_call_count)`` contributed by one LLM
    stream chunk. Used by ``llm_node`` to detect empty STOP (no text and
    no tools) before TTS claims a silence turn. Pure + testable."""
    if chunk is None:
        return 0, 0
    if isinstance(chunk, str):
        return len(chunk.strip()), 0
    delta = getattr(chunk, "delta", None)
    if delta is None:
        # Some plugins yield ChatChunk with content at top level.
        content = getattr(chunk, "content", None)
        tools = getattr(chunk, "tool_calls", None) or []
        text_n = len(str(content).strip()) if content else 0
        return text_n, len(tools)
    content = getattr(delta, "content", None) or ""
    tools = getattr(delta, "tool_calls", None) or []
    return len(str(content).strip()), len(tools)


def lily_llm_stream_is_empty_stop(text_chars: int, tool_calls: int) -> bool:
    """True when an LLM stream finished with FinishReason.STOP semantics
    we care about: no speakable text and no tool calls. Tool-only turns
    are valid and must not be treated as empty."""
    return int(text_chars or 0) <= 0 and int(tool_calls or 0) <= 0


def lily_is_prohibited_content_error(error: BaseException) -> bool:
    """Provider's non-configurable block class (not configurable SAFETY)."""
    return "PROHIBITED_CONTENT" in str(error or "").upper()


def _lily_short_hash(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()[:16]


def lily_grok_conversation_id(session_id: str) -> str:
    """Opaque, stable cache-routing id for one live Grok conversation."""
    digest = hashlib.sha256((session_id or "").encode("utf-8")).hexdigest()[:32]
    return f"lily-{digest}"


def lily_temporal_context(
    session_started_at: float,
    *,
    now: float | None = None,
) -> str:
    """Current UTC and session age for the volatile per-generation tail."""
    current = time.time() if now is None else float(now)
    started = float(session_started_at or current)
    elapsed = max(0, int(current - started))
    hours, remainder = divmod(elapsed, 3600)
    minutes, seconds = divmod(remainder, 60)
    utc = datetime.datetime.fromtimestamp(
        current, tz=datetime.timezone.utc
    ).isoformat(timespec="seconds").replace("+00:00", "Z")
    return (
        f"temporal context: current UTC {utc}; session elapsed "
        f"{hours:02d}:{minutes:02d}:{seconds:02d} ({elapsed}s). "
        "Use this for time-aware pacing and relative-time questions; do not "
        "recite the timestamp unless the table asks."
    )


def lily_build_grok_vocal_llm(
    *,
    model: str,
    effort: str,
    api_key: str,
    conversation_id: str | None = None,
):
    """Construct the one Grok vocal transport with a live-safe read budget."""
    if not api_key:
        raise RuntimeError("XAI_API_KEY missing for Grok vocal model")
    import httpx
    import openai as openai_sdk
    from livekit.plugins import openai as openai_plugin

    llm = openai_plugin.LLM.with_x_ai(
        model=model,
        api_key=api_key,
        reasoning_effort=effort,
        client=openai_sdk.AsyncClient(
            api_key=api_key,
            base_url="https://api.x.ai/v1",
            max_retries=0,
            default_headers=(
                {"x-grok-conv-id": conversation_id}
                if conversation_id
                else None
            ),
            http_client=httpx.AsyncClient(
                timeout=httpx.Timeout(
                    connect=15.0,
                    read=lily_config.adult_vocal_read_timeout(),
                    write=15.0,
                    pool=15.0,
                ),
                follow_redirects=True,
            ),
        ),
    )
    # with_x_ai omits the constructor's max_completion_tokens argument;
    # retain Lily's bounded spoken-turn contract explicitly.
    llm._opts.max_completion_tokens = lily_config.vocal_max_output_tokens()
    return llm



# Contract-note packet-kind spellings for the `event` discriminator alias
# (seam contract: bind / reveal / callout / finale).


def _chat_items(chat_ctx) -> list:
    # ChatContext._items confirmed at 1.6.6 (fleet injection pattern).
    items = getattr(chat_ctx, "_items", None)
    if items is None:
        items = getattr(chat_ctx, "items", [])
    return items


def _message_text(msg) -> str:
    content = getattr(msg, "content", None)
    if isinstance(content, list):
        return " ".join(str(c) for c in content)
    return str(content or "")


def _handle_spoken_text(handle) -> tuple[str, bool]:
    """Assistant text a finished SpeechHandle actually carried, plus
    whether it carried ANY chat items. ("", False) is a handle that never
    materialized — at 1.6.8 the invalidated-preemptive shape (speculative
    reply discarded at user-turn commit, interrupted=True, no items). Its
    truth is empty; see the HOTFIX-008 Z1 note at the playout-watcher
    call site."""
    spoken = ""
    had_items = False
    try:
        for item in handle.chat_items:
            had_items = True
            if getattr(item, "role", None) == "assistant":
                spoken += " " + _message_text(item)
    except Exception:
        pass
    return spoken.strip(), had_items


def _current_speech_id() -> str | None:
    """Return the pinned LiveKit 1.6.6 SpeechHandle id for this TTS task."""
    try:
        handle = _SpeechHandleContextVar.get(None)
        return getattr(handle, "id", None)
    except Exception:
        return None


def _coerce_confidence(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return max(0.0, min(1.0, float(value)))
    return None


def lily_diarization_confidence_from_nbest(nbest: dict | None) -> float | None:
    """Best-effort diarization confidence from per-utterance n-best payload.

    Speechmatics exposes no explicit per-segment diarization confidence on
    UserInputTranscribedEvent; we blend:
      - speaker_consistency: share of recovered words whose speaker tag
        matched this segment's diarization label
      - top_hypothesis_confidence: ASR confidence on hypothesis slot 0
    """
    if not isinstance(nbest, dict):
        return None
    consistency = _coerce_confidence(nbest.get("speaker_consistency"))
    top = _coerce_confidence(nbest.get("top_hypothesis_confidence"))
    if consistency is None and top is None:
        return None
    if consistency is None:
        return round(top if top is not None else 0.0, 3)
    if top is None:
        return round(consistency, 3)
    return round(consistency * 0.75 + top * 0.25, 3)


def _aligned_acoustic_confidence(
    game: "LilyGame",
    segment_ts: float | None,
) -> float | None:
    """Return acoustic confidence only when its capture clock is aligned."""
    acoustic_inputs = {}
    try:
        acoustic_inputs = game.acoustic.addressee_fusion_inputs()
    except Exception:
        acoustic_inputs = {}
    acoustic = None
    acoustic_captured_at = acoustic_inputs.get("captured_at")
    max_staleness = lily_config.addressee_acoustic_max_staleness_seconds()
    max_future = lily_config.addressee_acoustic_max_future_seconds()
    if lily_addressee.lily_acoustic_sample_aligned(
        segment_ts,
        acoustic_captured_at,
        max_staleness_seconds=max_staleness,
        max_future_seconds=max_future,
    ):
        try:
            acoustic = game.acoustic.addressee_confidence()
        except Exception:
            acoustic = None
    else:
        skew_s = lily_addressee.lily_acoustic_alignment_skew_seconds(
            segment_ts, acoustic_captured_at
        )
        if skew_s is not None:
            logger.info(
                "LILY_SYNC | ACOUSTIC_SAMPLE_MISALIGNED | segment_ts=%.3f "
                "acoustic_ts=%.3f skew_s=%.3f bounds=[-%.3f,+%.3f] "
                "action=diarization_only",
                segment_ts, float(acoustic_captured_at), skew_s,
                max_staleness, max_future,
            )
    return acoustic


def _segment_addressee_confidence(
    game: "LilyGame",
    *,
    event=None,
    attribution: str | None = None,
    speaker_label: str | None = None,
    diarization_confidence: float | None = None,
    acoustic_confidence: float | None = None,
) -> float | None:
    """Fuse the best diarization signal with aligned acoustic confidence."""
    diar = _coerce_confidence(diarization_confidence)
    if diar is None:
        diar = lily_addressee.lily_extract_diarization_confidence(event)
    if diar is None:
        diar = lily_addressee.lily_fallback_diarization_confidence(
            attribution, speaker_label
        )
    return lily_addressee.lily_fuse_addressee_confidence(
        diar,
        acoustic_confidence,
        diarization_weight=lily_config.addressee_fusion_diarization_weight(),
        acoustic_weight=lily_config.addressee_fusion_acoustic_weight(),
    )


# Answer-window opening (persistence-audit root-cause fix): the old hard
# >=60% token-overlap gate meant a paraphrased question NEVER opened the
# window and the whole deterministic pipeline stalled. Overlap is now a
# preference, not a gate — the tiers (verbatim / paraphrase, with a min-2
# matched-token guard against incidental single-word hits) live in
# lily_evaluation.lily_question_spoken_ratio; after
# WINDOW_FALLBACK_AGENT_TURNS finished agent turns with a question armed
# in phase=question the window opens regardless.
WINDOW_FALLBACK_AGENT_TURNS = 2
# WS-2 registered-undelivered reconciliation: a delivery stuck registered
# but unplayed (claim never confirmed, never released) is re-fired this
# many times before the watchdog releases the question back to supply.
UNDELIVERED_MAX_REFIRES = 2

# Device metadata identifies a browser/table candidate, never the humans
# currently present. Its memories stay quarantined until a live voiceprint
# overlaps the stored group.
_DEVICE_CANDIDATE_SOURCES = (
    "participant_metadata",
    "participant_metadata_late",
    "dispatch_metadata",
)



# ---------------------------------------------------------------------------
# Regeneration directives (WS-3, WO-LILY-OMNIBUS-003 + AMENDMENT-001).
#
# A spoken turn that is cut short (barge-in) or suppressed will have its
# act re-dispatched. The live defect was that the re-dispatch replayed the
# SAME words (greet ×2, a tension line ×4, the Black Panther reveal ×5).
# These directives ride the re-dispatch so a barged turn REGENERATES
# instead of replaying: the conversational variant leads with the result
# and trims length; the delivery variant keeps the question exact (players
# need every word) but strips any restart preamble.
# ---------------------------------------------------------------------------

# Speech/delivery directives + stale-claim knobs live in
# lily_speech_delivery (voice-inventory extract). Re-export so existing
# test imports (`from lily_agent import _CUT_RECOVERY_DIRECTIVE`) stay.
from lily_speech_delivery import (  # noqa: E402
    _CUT_RECOVERY_DIRECTIVE,
    _CUT_RECOVERY_USER_TURN_LOOKBACK,
    _REGEN_DELIVERY_DIRECTIVE,
    _REGEN_REAIR_DIRECTIVE,
    _STALE_CLAIM_MAX_RECHECKS,
    _STALE_CLAIM_MAX_RETRIES,
    _STALE_CLAIM_SECONDS,
)


# HOTFIX-006 N13: spelled roster sizes, for the authoritative-count state
# field only (the field shows the phrasings the count must appear in, so it
# has to spell the number the way she would say it).
_ROSTER_COUNT_WORDS = {
    1: "one", 2: "two", 3: "three", 4: "four", 5: "five",
    6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten",
}


# Watchdog policy sentinel: a policy's run() returns this to end the tick,
# exactly reproducing a `continue` in the old nested _idle_watchdog. Any
# other return value (a label, or "") means "did my work, fall through to the
# next policy" — reproducing a branch that did NOT continue.
_WATCH_HALT = "__watch_halt__"


@dataclass
class WatchPolicy:
    """One row of the idle-watchdog policy table (W2b). The watchdog used to
    be one nested method where recovery for a dead supply line hid three
    `if` levels down behind a question-armed guard (session 2260354c). Each
    branch is now a row with an explicit `when` predicate, so what a policy
    fires on — and where it sits in the priority order — is data, not control
    flow buried in a story.

    when       — fires this tick iff when(game) is truthy.
    every_ticks— cadence (1 = every tick; all current rows are 1).
    run        — does the work; returns `_WATCH_HALT` to end the tick
                 (an old `continue`) or any other string to fall through.
    skip_if    — other policy names that, if they ran this tick, skip this
                 one. Unused under strict ordered halt (the halt-break
                 subsumes it); reserved for order-independent exclusions.

    `when` predicates are written so they could later delegate to W1a's
    GameControl.may(act), but do NOT depend on it (W1a is not a hard dep)."""

    name: str
    when: Callable[["LilyGame"], bool]
    every_ticks: int
    run: Callable[["LilyGame"], Awaitable[str]]
    skip_if: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Game director — the non-LLM surface: window timer, adjudication commit,
# SFX dispatch, state publication, checkpointing triggers.
# ---------------------------------------------------------------------------

class LilyGame(lily_transition.LilyTransitionMixin, lily_supply.LilySupplyMixin, lily_identity.LilyIdentityMixin, lily_floor.LilyFloorMixin, lily_glass.LilyGlassMixin, lily_speech_delivery.LilySpeechDeliveryMixin):
    # Class-level defaults so __new__-built test fixtures (and any partially
    # constructed instance) read sane state; __init__ re-declares them with
    # the full contract comments.
    _phase_hold: str | None = None
    # Per-question one-shots for the two things that now fire at AIR rather
    # than at window-open: the glass publish and the durable burn row.
    _glass_published_qnum: int | None = None
    _durable_asked_qnum: int | None = None
    # Self-knowledge Task 3: a lagged returning table's delta rode the
    # greeting; the stamp persists after the greet confirms.
    _whats_new_pending: bool = False
    # HOTFIX-010 V4 edge (A): the delta rides whichever carrier reaches it
    # first — the late-recognition beat, the game-start ride-along, or a
    # re-dispatched greet. The durable stamp only advances on greet-confirm,
    # so a cut/never-confirmed greet used to leave EVERY carrier composing the
    # delta (double-disclosure). This session-scoped latch — same idempotency
    # pattern as _prefs_offer_made / _memory_disclosure_offered — makes the
    # delta compose exactly once regardless of carrier; the stamp still gates
    # durable advance, so a session where the sole carrier was cut re-discloses
    # next session rather than losing it.
    _whats_new_emitted: bool = False
    # RECOGNITION-VARIETY Task 1: the mid-session recognition catch-up
    # acknowledgment fires at most once per session.
    _late_recognition_fired: bool = False
    _late_recognition_pending: bool = False
    # CLASS 7 (LIVEFIRE-001) 7a: latched True when start_game commits. After
    # the round has started, recognition speech ("welcome back, want relaxed
    # pacing?") is FORBIDDEN — the live beat aired as act=game_start and stole
    # q_1's kickoff. Distinct from game_started, which several call sites use
    # as "greeting dispatched"; this is the round-start commit and persists
    # across a recycle (once started, always started for recognition).
    _game_start_committed: bool = False
    # RECOGNITION-VARIETY fixture Q5: answers spoken while the delivery
    # turn is still playing out (window not yet open) buffer here and
    # replay at window open — the "no early buzz-ins" v1 concession cost
    # a correct final answer its adjudication row on 08-04.
    _pre_window_segments: list | None = None
    # Session availability flags (manifest availability layer): set by the
    # entrypoint once gates are known; None = unknown, inject nothing.
    availability_flags: dict | None = None
    # FL-1 (WO-LILY-FLOOR-001): per-utterance addressee classifier and the
    # latest judgment — the FL-2/FL-4 consumption surface. Class-level
    # defaults keep offline __new__ harnesses working; classify_addressee
    # lazily builds the classifier when a harness skipped __init__.
    addressee_classifier: "lily_addressee_classifier.LilyAddresseeClassifier | None" = None
    last_addressee_judgment: "lily_addressee_classifier.LilyAddresseeJudgment | None" = None

    def __init__(
        self,
        ctx: JobContext,
        scorekeeper: LilyScorekeeper,
        reasoning: LilyReasoning,
        supabase,
        transcript_batcher,
        group_id: str,
        group_id_source: str = "room_name",
    ) -> None:
        self.ctx = ctx
        self.sk = scorekeeper
        self.reasoning = reasoning
        self.supabase = supabase
        self.transcripts = transcript_batcher
        self.group_id = group_id
        self.group_id_source = group_id_source

        self.session: AgentSession | None = None
        self.agent: "LilyAgent | None" = None  # set at entrypoint start
        self.background_audio: BackgroundAudioPlayer | None = None
        self.stt: LilySpeechmaticsSTT | None = None
        # P2 preemptive repair: True while a deterministic between-turn
        # instruction speech (reveal/steal/skip/start/mode-revert) is in
        # flight — preemptive generation is paused for those turns.
        self._preemptive_paused = False

        self.fragments = LilyFragmentAccumulator()
        # P0-D 9337B1: only direct self-identification (or a biometric name
        # label) may create a durable roster row. Persist evidence beyond the
        # 2s fragment window so LLM/tool latency cannot erase it.
        self._confirmed_name_evidence: dict[str, str] = {}
        self._identity_required_before_start = True
        # HOTFIX-010 V5: the session's ONE name ask is a one-shot. Once a
        # present voice has taken the floor (on_user_turn_completed) the ask
        # is spent and the gate is satisfied for good — it can never re-fire.
        self._identity_ask_spent = False
        self.rounds_total = lily_config.rounds_total()
        self.sk.questions_per_round = lily_config.questions_per_round()
        self.sk.rounds_total = self.rounds_total

        # UI phase per the seam contract:
        # lobby | question | answering | reveal | scores | final
        self.ui_phase = "lobby"
        self.game_started = False
        self.game_over = False
        self.finale_sent = False
        self.prewager_standings: list[dict] | None = None

        # Say gate (say-gate WO): idempotent speech-act registry. Keys are
        # claimed AT DISPATCH TIME (atomic check-and-set) so a racing
        # second trigger path is physically silent, confirmed at playout
        # completion, and released on playback failure so a retry can
        # redeliver (the 19:27:52 swallowed-delivery class).
        self.say_registry = lily_say_gate.SpeechActRegistry()
        # Stale-claim recovery (WO-LILY-HOTFIX-001): speech ids whose TTS
        # playout actually STARTED (agent_state -> "speaking"), and the
        # per-key re-dispatch budget. A pending claim whose owner never
        # appears here inside the watchdog deadline was wedged before the
        # air — its retry must not be dup-suppressed.
        self._playout_started_ids: set = set()
        self._stale_retry_counts: dict = {}
        # P0-C 9337B1: tts_node can replace model prose with a deterministic
        # sheet (or an honesty rewrite). Preserve the exact post-transform
        # text per handle so every transcript records what reached TTS.
        self._post_tts_text_by_speech_id: dict[str, str] = {}
        # Set by the entrypoint BEFORE session.start so on_enter knows
        # whether to greet (session_greet) or rejoin (session_rejoin).
        self.reconnected = False
        # C7 (HOSTLOOP-001): a heard/engaged start intent owns the lobby —
        # the empty-STOP lobby recovery never re-greets past it. Set
        # synchronously at spoken-start detection and again in start_game
        # (all sources), so no recovery can interleave a "welcome back".
        self._start_intent_heard = False
        # F1: empty-STOP fail-closed in lobby schedules at most one keyed
        # opener re-dispatch (session_greet / session_rejoin). Cap prevents
        # a mute death spiral of greets×N.
        self._empty_stop_lobby_recover_count = 0

        self.next_question: dict | None = None      # prefetched N+1 (the head)
        # W2b item 3a: depth-2 supply. The reserve holds N+2 so consuming the
        # head never leaves an empty hand (the supply-stall / "putting it
        # together" vamp). INVARIANT: the reserve is non-None only while the
        # head is non-None — it is filled only when the head is full and is
        # promoted or cleared the instant the head empties, so every existing
        # `next_question is None` guard stays correct.
        self._next_question_reserve: dict | None = None
        self.armed_question: dict | None = None     # in state block, awaiting ask
        # Draw idempotency (WO-LILY-DESYNC-HONESTY-001 G2): every question
        # a prefetch DRAWS registers here the moment it lands — not at
        # serving (arm), which is where the live q_0492 double-draw slipped
        # through: the second draw ran before the first serving registered
        # in used_prompts/asked_history. Session-scoped; a discarded draw
        # stays excluded (no repeats either way).
        self._drawn_ids: set = set()
        self._drawn_hashes: set = set()
        # Operator-requested topic (WO-LILY-CAPABILITY-RESTORE-001): maps a
        # round number to a table-named subject ("Game of Thrones", "Japan").
        # _category_for_round consults it before the fixed family rotation,
        # so lily_set_category steers the generator without touching the
        # rotation. Non-adult only — the adult deck keeps its own families.
        self._category_override: dict[int, str] = {}
        # HOTFIX-006 N2 — the custom-round REGISTRATION ledger for this
        # session: normalized topic -> ids of the questions actually built
        # and committed to the supply line under it. Written at exactly one
        # place (_register_custom_question, at the prefetch commit) and read
        # by the only two things allowed to speak about a custom round:
        # lily_custom_round_line (the tool result) and
        # custom_round_state_line (the state block). An empty entry means
        # the round does not exist, and there is no code path that can say
        # otherwise — the lily-16A9AE failure was a confirmation produced
        # from the operator's own words with nothing behind it.
        self._custom_round_registered: dict[str, list[str]] = {}
        # Topics she has already refused this session. A refusal rolls the
        # override back, so without this the refused topic would vanish from
        # every honesty read the moment it was declined — and the second live
        # fabrication ("I'm putting your round together right now") came
        # AFTER the round had already failed to appear.
        self._custom_round_refused: list[str] = []
        # 50/50 lifeline (multiple-choice WO): eliminated choice indices for
        # the CURRENT question — reset at arm, rides publish_metadata.
        self.eliminated: list[int] = []
        self.used_prompts: list[str] = []
        # Bank curation (WO-LILY-OMNIBUS-002 D/F): the group's served-
        # question history (loaded at entrypoint, appended per serving)
        # and the promoted category-candidate names (F — the only
        # proposals Lily may ever announce).
        self.asked_history: list[dict] = []
        # Revealed-question burn (WS-4): ids and normalized-text hashes of
        # questions whose answer has gone to air this session. A burned
        # question can never re-arm and rides the no-repeat draw exclusion.
        # HOTFIX-006 N8: a reveal in her own conversational lane burns here
        # too, which is what makes "revealed" a state the steal path can
        # check at dispatch.
        self._burned_question_ids: set[str] = set()
        self._burned_question_hashes: set[str] = set()
        # HOTFIX-006 N12: the question-transition journal — qnum -> the
        # ordered stages (reveal, verdict, next_delivery) that transition
        # has run, and which question's transition is currently in flight.
        # One transition, one owner, one narration.
        self._transition_journal: dict[int, list] = {}
        self._open_transition_qnum: int | None = None
        self.promoted_categories: list[str] = []
        self._prefetch_task: asyncio.Task | None = None
        self._window_timer: asyncio.Task | None = None
        # Idle watchdog (live 2026-07-15 stall class): guarantees a live
        # game can never sit armed-less and silent.
        self._watchdog_task: asyncio.Task | None = None
        self._prefetch_stall_ticks = 0
        self._armed_limbo_ticks = 0
        # WS-2 registered-undelivered reconciliation: consecutive watchdog
        # ticks a delivery has been armed-but-unconfirmed, and how many
        # times it has already been re-fired this question. Both reset when
        # a question arms, when a window opens, and when the delivery
        # confirms.
        self._undelivered_ticks = 0
        self._undelivered_refires = 0
        # WS-6 supply-stall fallback: consecutive watchdog ticks the game
        # has sat live-idle with no question in hand (none armed, none
        # prefetched). Past the fallback window a curated-bank question
        # arms directly. Reset whenever a question is armed or prefetched.
        self._supply_stall_ticks = 0
        # True while the currently-open answer window is a steal window
        # (seam: rides answer_window JSON as the optional `steal` key).
        self._steal_window = False
        self._judged_keys: set[str] = set()
        self._spec_judge: dict[str, asyncio.Task] = {}
        # n-best ASR recovery (WO-LILY-ADDRESSEE-H1-001 Task 1): the
        # per-session collector (set by the entrypoint ONLY when the STT
        # patch armed; None = clean 1-best degradation) and the per-question
        # candidate-key -> drained n-best dict used by Tier-1/Tier-2.
        self.nbest_collector: "lily_nbest.LilyNBestCollector | None" = None
        self.timestamp_reconciler = lily_nbest.LilyTimestampReconciler()
        self._nbest_by_key: dict[str, dict] = {}
        self._adjudicating = False
        self._question_transitioning = False
        self._bed_handle = None
        self._pending_reveal_event: dict | None = None
        self._pending_unbound_award: dict | None = None
        # HOTFIX-008 Z1: her last committed turn, STAMPED with the chat-item
        # id it belongs to ("" = manual/unstamped write). One atomic tuple —
        # id and text can never be observed mismatched, so no reader can
        # pair one generation's id with a neighboring turn's text.
        self._last_assistant_turn: tuple[str, str] = ("", "")
        self._suppressed_speech_ids: set[str] = set()
        # PATCH-001 T1/T2 (RETIRE_WITH_WS6): live speech handles by id
        # (cancellation reach) + the answered-question set (an answered
        # question never re-airs).
        self._speech_handles: dict = {}
        self._answered_questions: set = set()
        # PATCH-002 M4: question numbers whose delivery stem reached the
        # air but whose window has not yet opened — an abandonment while a
        # number is in this set emits a stem-cancellation event.
        self._aired_stems: set = set()
        # HOTFIX-005 X3: question numbers whose delivery actually REACHED
        # PLAYOUT — recorded the instant the answer window opens (the
        # delivery turn's playout completion, open_window). Durable and
        # append-only, unlike _aired_stems which clears at completion: it
        # is the reveal-side mirror of the delivery-registration guard. A
        # reveal/verdict is dispatchable ONLY for a question in this set;
        # the 14:53:34 fixture aired a verdict for a question whose stem
        # never played, so nothing here ever contained it.
        self._delivered_to_playout: set = set()
        # PATCH-002 A4/A5 (RETIRE_WITH_WS6): the hold state binds EVERY
        # dispatch lane. A decline/wait/STOP puts the session in hold —
        # no unsolicited conversational turns AND no question deliveries
        # until user speech releases it, a hard game event fires, or the
        # generous timeout elapses. Her own "take your time" binds her too.
        self._hold_active = False
        self._hold_since = 0.0
        self._hold_reason: str | None = None
        # P0-B 9337B1: STOP is a persistent game-delivery freeze, distinct
        # from a temporary conversational wait. Ordinary follow-up speech
        # may resume conversation but never clears this latch.
        self._delivery_stop_sticky = False
        # PATCH-003 P6/P10: yield-after-question state.
        self._question_pending = False
        self._question_pending_since = 0.0
        self._question_pending_reoffered = False
        # PATCH-003 P9: responsiveness-floor clock (0 = nothing awaiting).
        self._awaiting_address_since = 0.0
        self._address_unanswered_warned = False
        self._enroll_started = False
        self._last_enroll_retry_ts = 0.0  # WS-8 under-threshold retry cooldown
        self._armed_speech_misses = 0  # agent turns finished w/o performing q
        # Regeneration gate (WS-3, WO-LILY-OMNIBUS-003): when a spoken turn
        # is cut short (barge-in) or suppressed, the act it was performing
        # re-dispatches. _reair_gate_armed marks that the NEXT dispatch is
        # that re-air — so it carries a regeneration directive instead of
        # replaying verbatim; _reair_turn_pending carries the signal one hop
        # further, to tts_node, where a verbatim leak is caught and
        # regenerated once. Both getattr-guarded for __new__ harnesses.
        self._reair_gate_armed = False
        self._reair_turn_pending = False
        # Structural delivery intent (desync WO Sub-agent B): question
        # number whose delivery the NEXT outbound spoken turn was
        # code-dispatched to perform — that turn claims q_{N}_delivery at
        # dispatch once it carries the question text (WS-1: drifted turns
        # are rewritten to the sheet first). None = no delivery in flight.
        self._pending_delivery_qnum: int | None = None
        # SpeechHandle ids explicitly dispatched to carry a question. This
        # prevents a racing recognition/preferences beat from consuming the
        # global delivery intent, and lets question-only turns be rewritten to
        # the deterministic sheet before any prior-answer commentary airs.
        self._delivery_speech_acts: dict[str, str] = {}
        # Every structural delivery is strict (WS-1): if the generated
        # turn does not contain the armed prompt (and every MC option),
        # tts_node replaces it with the deterministic question sheet
        # before any claim opens.
        # WS-5 (MC answer-aborts-read): the in-flight multiple-choice
        # delivery. Stem+options are ONE SpeechHandle turn (WS-1), so a
        # correct answer during the OPTIONS read truncates the remaining
        # options and jumps to adjudication rather than waiting out the
        # full playout. None = no MC delivery in flight. started_at models
        # the stem-completion boundary (1.6.6 exposes no per-sentence
        # playout signal — see _note_mc_delivery_start): the stem stays
        # protected until stem_words / mc_stem_protect_words_per_second()
        # has elapsed, so an early answer can never cut the question before
        # the table has heard it.
        self._mc_delivery_qnum: int | None = None
        self._mc_delivery_started_at: float | None = None
        self._mc_delivery_stem_words: int = 0
        # Any registered delivery, including freeform. This survives the
        # framework interruption callback long enough for the final STT
        # transcript to turn a shouted correct answer into an immediate
        # window open instead of a question re-read.
        self._active_delivery_qnum: int | None = None
        self._active_delivery_started_at: float | None = None
        self._active_delivery_ended_at: float | None = None
        # HOSTLOOP-001 C3c: the question whose read a deliberate barge-in
        # killed, and which therefore owes the table either a bound answer or
        # a resume of the remaining choices (Session B, lily-05BB92, is what
        # owing neither looks like). Cleared the moment either is satisfied.
        self._delivery_barge_cut_qnum: int | None = None
        # The exact text a resumed read must speak, staged for tts_node.
        self._pending_delivery_resume: str | None = None
        # Exact user turns whose response is already owned by deterministic
        # game speech. on_user_turn_completed consumes these and raises
        # StopResponse so LiveKit does not also generate a conversational
        # "Correct!" over the committed verdict.
        self._deterministic_reply_texts: list[str] = []
        self._prehook_answer_suppressions: set[tuple[int, str]] = set()
        # WS-5 buzz-buffer widening: rolling (ts, seg) of recent finals so
        # a final that landed up to buzz_prewindow_seconds() BEFORE the
        # delivery claim is back-filled into the pre-window buffer and
        # scored at window open, not left inert.
        self._recent_finals: list[tuple[float, dict]] = []
        # WS-1 intake gate: wall-clock of the most recent speaker bind.
        # While a bind is fresher than lily_config.intake_settle_seconds()
        # the round-robin is still growing and start_game defers.
        self._last_bind_at: float | None = None
        # Screen-sync phase hold (voice/glass sync fix, 2026-07-31): while
        # set, publish_attributes reports THIS phase instead of ui_phase.
        # Set to "lobby" when the FIRST question arms mid-greeting so the
        # board does not replace the lobby until the delivery turn actually
        # plays out (the window-open publish clears it). Internal turn
        # logic keeps reading self.ui_phase — only the published seam holds.
        self._phase_hold: str | None = None
        # Honesty assist (desync WO Sub-agent C): one grounded
        # `[state note: …]` line, set when a player's utterance makes a
        # checkable claim about published state, rendered into the state
        # block for her next turn, cleared when that turn finishes playing.
        # The leak filter keeps the note itself off the air.
        self._state_note: str | None = None
        # RECOGNITION-HONESTY: honesty conditioning for a mid-conversation
        # returner-claim when memory is empty (never deny prior contact).
        # Its own slot so it can coexist with a state-contradiction note on
        # the same turn; cleared per-turn like _state_note.
        self._returner_honesty_note: str | None = None
        # P0-1 BE8D8B: persistent for the whole session. A one-shot note can
        # be consumed by an interrupted greeting; the player's explicit
        # returner claim cannot. Once true, UNKNOWN/blank may never be
        # narrated as EMPTY even if the identity probe resolves empty.
        self._returner_claim_seen: bool = False
        # P0-B/C (live 2026-08-09): recognition dispute after a false
        # clean-slate. Locks start/category kickoff until one grounded
        # why-answer lands.
        self._recognition_dispute: bool = False
        self._recognition_dispute_why_answered: bool = False
        self._recognition_why_note: str | None = None
        # WO-2: bare "yes" after an A-or-B offer must not open round one.
        self._pending_or_choice_offer: bool = False
        self._ambiguous_yes_blocks_start: bool = False
        # Pictures-on offer: after she asks "want them on?", a short yes /
        # "live immediately" flips media_mode (bank was stocked; mode stuck).
        self._pending_picture_on_offer: bool = False
        # HOTFIX-005 X12: explain-on-request + verdict-contest conditioning —
        # same one-shot lifecycle as the notes above (context only, leak-
        # filtered, consumed at the turn's playout).
        self._explain_request_note: str | None = None
        self._contest_note: str | None = None
        # HOTFIX-006 N9: a correct answer that landed past the closed
        # window — announced once, with its reason, then consumed.
        self._late_answer_note: str | None = None
        # HOTFIX-004 Defect 1: deterministic 18+ consent floor. Set only by
        # the age-consent detector on a real user final; the adult-mode gate
        # requires it IN ADDITION to the model's confirmed_all_18_plus flag,
        # so a question ("Should I verify?") can never unlock the deck.
        self._age_consent_confirmed: bool = False
        # P0-2 BE8D8B: all setup intents in a final are non-exclusive.
        # Kickoff is blocked while any requested setup job remains.
        self._setup_requested: set[str] = set()
        self._setup_pending: set[str] = set()
        self._setup_heat_requested: str | None = None
        self._setup_start_requested: bool = False
        # VAD-backed floor: a start tool called while the player is still
        # speaking cannot race ahead of the rest of a split multi-intent.
        self._user_speaking: bool = False
        self._group_facts_written: set = set()  # per-session fact dedupe

        # Persistent cross-session memory (rematch): the [RETURNING TABLE]
        # block loaded at session start, and this session's callouts
        # collected for the lily_memories highlights column.
        self.memory_block: str = ""
        # Remembered player names for STT name-snapping at bind time
        # (the "Romney"-for-"Rami" class). NON-SPEECH consumer only
        # (lily_known_name_correction); never a source for spoken naming.
        self.memory_player_names: list[str] = []
        # V3 (HOTFIX-010): the cold opener is a bare self-intro + one
        # orienting beat, then silence. Recognition / walkthrough / prefs /
        # what's-new are deferred until a human has actually spoken; this
        # latch flips on the first user turn (on_user_turn_completed).
        self._first_human_utterance_seen = False
        # A device key may point at a returning table, but it does not prove
        # who is in the room now. Candidate data is quarantined from the
        # vocal context until a current voiceprint overlaps.
        self.device_candidate_group_id: str | None = None
        self.device_candidate_source: str | None = None
        self.device_identity_verified = False
        self.device_identity_rejected = False
        self._device_candidate_memory: dict | None = None
        self._device_candidate_memory_block = ""
        self._device_candidate_prefs: dict = {}
        self._device_candidate_voiceprints: list[dict] = []
        self._device_verify_task: asyncio.Task | None = None
        self.highlights: list[dict] = []
        # Memory at the door (WO-LILY-DESYNC-HONESTY-001 F): set once the
        # returning-table memory question has an ANSWER — a strong group id
        # loaded its memory (block or provably none), or a group-id upgrade
        # finished its reload. The greeting awaits this event up to
        # LILY_GREETING_MEMORY_BUDGET_SECONDS so a returning table is
        # recognized in the FIRST utterance instead of one turn late.
        self.memory_settled: asyncio.Event = asyncio.Event()
        # HOTFIX-006 N1: has the voice-identity probe FINISHED (matched or
        # definitively not)? Distinct from memory_settled, which only says
        # the greeting's budget elapsed. A probe still running means the
        # absence of memory is UNKNOWN, not established — and an unknown
        # may not be spoken as a fact.
        self._voice_identity_resolved: bool = False
        # HOTFIX-008 Z3: a biometric NO_MATCH is one route reporting, not
        # the identity question closing — the stated-name door is still an
        # untried probe route (name binding is mandatory lobby flow). The
        # stamp opens a bounded hold on memory-characterising speech; the
        # checked flag records that the name door has reported.
        self._voice_identity_no_match_at: float | None = None
        self._identity_name_door_checked: bool = False
        # GROUP PREFERENCES (group prefs WO): the OPAQUE per-group prefs
        # dict (lily_group_prefs.prefs, migration 013), loaded at session
        # start for a returning group and persisted whole on every
        # preference change. Keys are feature-owned — this WO owns
        # "pacing"; round_format / media_mode slot into the SAME dict when
        # their features land post-merge (nothing here enumerates keys).
        self.prefs: dict = {}
        # Ask-once latch: the "play the usual, or change anything?"
        # question is issued AT MOST once per session — whichever of the
        # greeting or the post-upgrade game-start beat meets stored prefs
        # first consumes it; it is never re-asked tonight.
        self._prefs_offer_made = False
        # WO-LILY-FORGETME-001: deletion right + memory transparency.
        # forget_state drives the deterministic spoken flow:
        #   idle -> pending_confirm (spoken request or confirm=false tool
        #   call) -> executing -> done (verified) | failed (retryable), or
        #   -> declined (a no drops it; only a fresh player request re-arms).
        self.forget_state: str = "idle"
        self.forget_spoken_confirmed = False
        # Who asked (player name or speaker label): only THEIR yes/no
        # resolves the pending confirmation; None accepts any voice
        # (tool-initiated flow, where the requester is unattributed).
        self.forget_requester: str | None = None
        # W3 confirmation beat: a pacing command that CONTRADICTS a pacing
        # the player already stated this session (self.prefs["pacing"]) is
        # held here instead of flipping silently — one beat asks, the next
        # assent applies. None = nothing awaiting. Requester scopes the
        # yes/no like the forget flow.
        self._pending_pacing: str | None = None
        self._pending_pacing_requester: str | None = None
        # W3 fix-loop (MEDIUM-2): provenance of the CURRENT prefs["pacing"].
        # True only once a live this-session action (set_pacing) wrote it;
        # a value merged from cross-session memory leaves it False. The
        # confirmation beat's wording derives from this so it never claims
        # "you chose X earlier this session" about a remembered preference.
        self._pacing_stated_this_session: bool = False
        # The group id the cascade targets — captured on the FIRST attempt,
        # BEFORE the fresh-anonymous re-bind, so a retry after partial
        # failure still deletes the ORIGINAL identity, never the fresh one.
        self._forget_target_group: str | None = None
        # Task 4 lobby disclosure: total stored games (the lily_memories
        # row count — the persistent disclosure counter) + once-per-session
        # latch so the clause is never repeated within a night.
        self.memory_total_games: int = 0
        self._memory_disclosure_offered = False

        # B3 session report: wall-clock duration baseline.
        self.session_started_at = time.time()
        # xAI Chat Completions caches exact prompt prefixes automatically.
        # Sticky routing is provider-recommended; hash the internal session
        # id so the transport header carries no room/player-readable value.
        self.grok_conversation_id = lily_grok_conversation_id(
            self.sk.session_id
        )

        # B1 addressee-label corpus: players awaiting a clarify reply
        # (player_name -> {"row_task": Task|None returning the logged row
        # id}), and the insert task per answer candidate this question
        # (candidate key -> Task returning row id) kept for the implicit
        # label UPDATE at adjudication commit.
        self.pending_clarify: dict[str, dict] = {}
        self._addressee_rows: dict[str, asyncio.Task] = {}

        # FL-1: the per-session addressee classifier (adjacency anchor +
        # side-cluster machine live inside it).
        self.addressee_classifier = (
            lily_addressee_classifier.LilyAddresseeClassifier()
        )
        self.last_addressee_judgment = None

        # Acoustic pipeline (WO-LILY-AUDEERING-001): room-read state store +
        # per-user-turn trajectory counter. The entrypoint passes the fresh
        # per-session state; the default keeps offline construction working.
        self.acoustic = lily_audeering_consumers.lily_get_acoustic_state()
        self.audeering_pipeline = None
        self._user_turn_index = 0

    # -- last-assistant-turn buffer (HOTFIX-008 Z1) --------------------------

    @property
    def _last_assistant_text(self) -> str:
        """Text of her most recent committed turn, stamp dropped — for
        readers that genuinely want "her previous turn" with no generation
        of their own (_verdict_already_spoken, _reveal_already_on_air,
        transition journaling)."""
        return getattr(self, "_last_assistant_turn", ("", ""))[1]

    @_last_assistant_text.setter
    def _last_assistant_text(self, text: str) -> None:
        self._last_assistant_turn = ("", text or "")

    def last_assistant_text_for(self, item_ids) -> str:
        """Stamp-checked read: the buffered text ONLY if it belongs to one
        of the caller's own chat items. A caller keyed to a different
        generation — or holding no items at all, the invalidated-preemptive
        shape — gets "", never a neighboring turn's words. Any future
        generation-scoped reader of this buffer MUST come through here;
        reading `_last_assistant_text` from a per-speech callback is the
        phantom-`[cut off]` bug class (GUARD_MAP: HOTFIX-008 Z1)."""
        item_id, text = getattr(self, "_last_assistant_turn", ("", ""))
        return text if item_id and item_id in item_ids else ""

    # -- state transport (seam contract) ------------------------------------

    def _players_payload(self) -> list[dict]:
        if not self.sk.players:
            return []
        # Scores derive from the ledger (WS-7) — the single write path's
        # sums, never the parallel per-player counters. The choke point
        # keeps both in sync; wrap-up reconciliation flags any drift.
        scores = self.sk.ledger_scores()
        top = max(scores.values())
        leaders = [n for n, v in scores.items() if v == top and top > 0]
        sole_leader = leaders[0] if len(leaders) == 1 else None
        display = self._surface_names()
        return [
            {
                "name": display[name],
                "score": scores[name],
                "streak": s["streak"],
                "leader": name == sole_leader,
            }
            for name, s in self.sk.players.items()
        ]

    def _surface_names(self) -> dict[str, str]:
        """HOTFIX-010 V5 fix-loop: the one naming authority for every name
        SURFACE — symmetry with the spoken authority lines, which recite
        real_player_names() only. A placeholder anchor is keyed by its raw
        diarizer label ("UU"/"S1"); that label is an attribution anchor, NOT
        an identity, so it must never be aired or persisted as a player name.
        This maps each roster key to the name it may surface as: real names
        pass through; placeholders become a neutral non-identity marker. Shared
        by every surface that would otherwise leak the raw label — the frontend
        scoreboard and finale/comeback/standings (via _players_payload) AND the
        per-player block persisted in game_stats (build_game_stats) — so the
        two persistence legs stay consistent (same placeholder → same marker,
        both iterate self.sk.players in insertion order). Scores/streak/leader
        stay keyed on the raw dict key; only the surfaced name is neutralized.
        A real name later migrates the placeholder's history via bind_speaker,
        after which the entry is real and surfaces as itself."""
        names: dict[str, str] = {}
        seq = 0
        for key, s in self.sk.players.items():
            if s.get("placeholder"):
                seq += 1
                names[key] = "Player" if seq == 1 else f"Player {seq}"
            else:
                names[key] = key
        return names


    def settle_context_nowait(self) -> None:
        """Y2 (HOTFIX-007) — SETTLE-ON-EVENT, the fix the first instrumented
        session demanded. Live lily-05BB92: preemptive invalidated=10,
        used=0 — every speculative reply was discarded at turn commit, so
        the table paid full LLM+TTS latency (~2.4s) on every turn. Cause
        (measured, not guessed): the framework compares the speculation's
        context snapshot against the committed context, transcript
        equivalence is word-level (the STT final at p50 1.57s beats the
        1.82s turn commit, so transcript drift is NOT the driver) — what
        differs is the STABLE state block, mutated by ASYNC game events
        between speculation and commit: a prefetch landing flips "next
        question: ready", adjudication commits scores, arming moves the
        counters. The volatile-tail split (P2) handles per-turn churn
        (clock/candidates); it cannot see event-driven stable changes.

        The settle: whenever stable inputs change, refresh the agent's
        PERSISTENT chat ctx immediately — the same _apply_context_blocks
        the turn hook uses, same idempotent replace-only-when-changed
        semantics — so the next speculation snapshots CURRENT state and
        the commit-time comparison finds nothing new. Events that land in
        the narrow window after speculation triggered still invalidate
        (correctly — the reply should see them); the Y2 counter measures
        the residue. No new mechanism: one extra caller of the existing
        injection, wired through the existing state-change chokepoint."""
        agent = getattr(self, "agent", None)
        ctx = getattr(agent, "_chat_ctx", None)
        apply_blocks = getattr(agent, "_apply_context_blocks", None)
        if apply_blocks is None or ctx is None:
            return  # bare fixtures / pre-session: the turn hook still applies
        try:
            apply_blocks(ctx, now=time.time())
        except Exception:
            logger.exception(
                "LILY_CTX | SETTLE_FAILED — the turn hook still applies blocks"
            )




    def game_control(self) -> "lily_game_control.GameControl | None":
        """Project the current latch state as a typed GameControl. Returns
        None only if the latches form a combination the machine refuses to
        store — recorded as a parity divergence, never raised into a live
        dispatch."""
        sk = self.sk
        try:
            delivery_confirmed = (
                self.say_registry.state(
                    f"q_{getattr(sk, 'question_number', None)}_delivery"
                )
                == lily_say_gate.CLAIM_CONFIRMED
            )
        except Exception:
            delivery_confirmed = False
        try:
            return lily_game_control.from_latches(
                game_started=getattr(self, "game_started", False),
                game_over=getattr(self, "game_over", False),
                delivery_stop_sticky=getattr(self, "_delivery_stop_sticky", False),
                adjudicating=getattr(self, "_adjudicating", False),
                question_transitioning=getattr(
                    self, "_question_transitioning", False
                ),
                hold_active=getattr(self, "_hold_active", False),
                hold_reason=getattr(self, "_hold_reason", None),
                answer_window_open=getattr(sk, "answer_window_open", False),
                active_delivery_qnum=getattr(self, "_active_delivery_qnum", None),
                pending_delivery_qnum=getattr(
                    self, "_pending_delivery_qnum", None
                ),
                open_transition_qnum=getattr(self, "_open_transition_qnum", None),
                armed_question=getattr(self, "armed_question", None),
                question_number=getattr(sk, "question_number", None),
                recognition_dispute=getattr(self, "_recognition_dispute", False),
                question_pending=getattr(self, "_question_pending", False),
                delivery_confirmed=delivery_confirmed,
                game_start_committed=getattr(
                    self, "_game_start_committed", False
                ),
            )
        except lily_game_control.IllegalControlState as exc:
            logger.warning(
                "LILY_GAMECONTROL | ILLEGAL_COMBO | session=%s q=%s reason=%s "
                "— latch forest stored a combination the typed machine "
                "forbids (surfaced by the W1a shadow, not fatal)",
                getattr(sk, "session_id", "-"),
                getattr(sk, "question_number", None), exc,
            )
            self._gamecontrol_divergence("illegal_combo", str(exc))
            return None

    def _gamecontrol_divergence(self, kind: str, detail: str) -> None:
        """Record a shadow-parity divergence. In parity mode (the test suite
        sets LILY_GAMECONTROL_PARITY) this raises so the mismatch is a hard
        failure; live, it only counts and logs (the latches stay
        authoritative this wave)."""
        counts = getattr(self, "_gamecontrol_divergences", None)
        if counts is None:
            counts = self._gamecontrol_divergences = {}
        counts[kind] = counts.get(kind, 0) + 1
        # An illegal-combo discovery is a separate signal (a latch state the
        # typed machine forbids), not a may()-vs-latch gate disagreement — it
        # is recorded and reported, never raised. Only a genuine gate-decision
        # divergence hard-fails the parity run.
        if kind.startswith("illegal_combo"):
            return
        if os.environ.get("LILY_GAMECONTROL_PARITY"):
            raise AssertionError(
                f"GameControl parity divergence [{kind}]: {detail}"
            )

    def _gamecontrol_parity(
        self, act: str, source: str, legacy_reason: str | None, site: str
    ) -> None:
        """Compare may(act) against the latch gate's decision at a wired call
        site and record any mismatch. Only the reasons may() models are
        compared (hold / game_stopped / no_live_game for dispatch acts; the
        stop / already_adjudicating / transitioning / no_armed_question set
        for the adjudicate act). Source-exempt dispatches skip the hold/stop
        comparison — the exemption is dispatch context may() does not model."""
        ctrl = self.game_control()
        if ctrl is None:
            return  # already recorded as an illegal-combo divergence
        may_reason = ctrl.may(act)
        # Exempt sources bypass the hold / game-lane state gates at the call
        # site, so may()'s state verdict is not expected to match there.
        if site == "gated_say" and (
            source in self._HOLD_EXEMPT_SOURCES or source == "question_reoffer"
        ):
            return
        if may_reason != legacy_reason:
            logger.warning(
                "LILY_GAMECONTROL | PARITY_DIVERGE | site=%s act=%s source=%s "
                "may=%s legacy=%s phase=%s delivery=%s hold=%s stop=%s",
                site, act, source, may_reason, legacy_reason,
                ctrl.phase.value, ctrl.delivery, ctrl.hold_reason,
                ctrl.stop_sticky,
            )
            self._gamecontrol_divergence(
                f"{site}:{may_reason}!={legacy_reason}",
                f"act={act} source={source}",
            )





    def _set_ui_phase(self, phase: str) -> None:
        if phase != "question":
            # Any non-question transition (answering/scores/final/lobby)
            # ends a pre-delivery screen hold — the hold only ever bridges
            # arm → first delivery playout.
            self._phase_hold = None
        if phase != self.ui_phase:
            self.ui_phase = phase
            self.publish_attributes_nowait()

    # -- deterministic instruction speech (P2 preemptive repair) -------------

    def instructed_reply(self, instructions: str):
        """Fire a deterministic between-turn speech (reveal, steal window,
        skip, game start, mode revert) via generate_reply(instructions=...).

        This is a mutation source that CANNOT move into
        on_user_turn_completed: it happens between user turns, driven by
        timers and tool commits, and its assistant items land in the
        persistent chat context whenever the speech is scheduled. Any
        preemptive user-turn run started while this speech is in flight is
        therefore dead by construction — the 1.6.6 equivalence check
        (agent_activity.py: preemptive.chat_ctx.is_equivalent(...)) will
        discard it. Rather than paying for that dead LLM run, preemptive
        generation is paused here and resumed on TTS playout completion
        (on_agent_speech_finished), using the live-read agent-level
        turn_handling["preemptive_generation"]["enabled"] flag that 1.6.6
        exposes."""
        if self.session is None:
            return None
        if self.agent is not None:
            self.agent.set_preemptive_generation(False)
            self._preemptive_paused = True
        return self.session.generate_reply(instructions=instructions)

    def direct_say(self, text: str):
        """HOSTLOOP-001 C6 — the DETERMINISTIC speech lane, sibling of
        instructed_reply above.

        instructed_reply is model-mediated (generate_reply), and the
        measured cost of that is the whole clause: 8–13s from a finished
        answer to a spoken verdict, acks landing a turn late. This lane
        hands FIXED words straight to the synthesizer, so the only latency
        left is TTS first-frame.

        AgentSession.say() at 1.6.8 still routes its text through
        Agent.tts_node (agent_activity._tts_task_impl ->
        perform_tts_inference(node=self._agent.tts_node, ...)) and still
        emits speech_created, so the say-gate hygiene, the leak filter, the
        delivery/transition claim decisions, the suppressed-id bookkeeping
        and the playout watcher all see this turn exactly as they see a
        generated one — and Y5 transcript truth holds without a special
        case: it is recorded at playout completion like any other aired
        turn. add_to_chat_ctx defaults True, so her own context carries what
        she just said (which is half of why the composite does not double
        it).

        The same preemptive pause/resume pair instructed_reply uses applies:
        this speech commits assistant items too, so a speculative user-turn
        run started under it is dead by construction."""
        session = getattr(self, "session", None)
        say = getattr(session, "say", None)
        if session is None or not callable(say):
            # No session, or a session without the say lane. Never raise
            # into the dispatch path: gated_say has already logged LILY_SAY
            # and the caller treats a handle-less dispatch the same way it
            # treats instructed_reply returning None.
            return None
        if getattr(self, "agent", None) is not None:
            self.agent.set_preemptive_generation(False)
            self._preemptive_paused = True
        try:
            return say(text)
        except Exception as e:
            logger.warning("LILY_SAY | DIRECT_SAY_FAILED | %s", e)
            return None

    def _resume_preemptive(self) -> None:
        # G1 (WO-LILY-DESYNC-HONESTY-001): while the game is LIVE the
        # playout-completion resume must not re-enable preemptive
        # generation — set_game_live_preemptive holds it off for the whole
        # game (see that method for why); the P2 pause/resume pair only
        # cycles it in the lobby and the wrapup.
        # getattr: test harnesses build LilyGame via __new__ without the
        # full __init__ attribute set.
        if self._preemptive_paused and self.agent is not None:
            self._preemptive_paused = False
            game_live = (
                getattr(self, "game_started", False)
                and not getattr(self, "game_over", False)
            )
            # 2026-08-09 volatile-tail split: live games run preemptive by
            # default again, so the resume re-enables it mid-game too —
            # unless LILY_LIVE_PREEMPTIVE restores the G1 hold.
            if (not game_live) or lily_config.live_preemptive_enabled():
                self.agent.set_preemptive_generation(True)

    def set_game_live_preemptive(self, live: bool) -> None:
        """G1 (WO-LILY-DESYNC-HONESTY-001): preemptive generation is OFF
        for the whole live game, ON in the lobby and after the finale.

        Why: the P2 injection-timing fix is correct (stable item ids,
        injected in on_user_turn_completed against both contexts), but
        during rounds nearly EVERY user turn still changes the state block
        honestly — the utterance being processed lands as an answer
        candidate line, and answer_window open/closed flips on the clock —
        so 1.6.6's equivalence check (preemptive.chat_ctx.is_equivalent)
        rightly discarded the speculative run anyway: 10 warnings/session
        and a dead LLM call each, zero realized latency win. 1.6.6 has no
        pre-snapshot injection hook (on_preemptive_generation copies
        agent.chat_ctx synchronously inside AudioRecognition), so the
        supported lever is the agent-level live-read enabled flag.

        2026-08-09 UPDATE (P2 volatile-tail split): the every-turn
        invalidators — the clock line, answer window and live candidates —
        now ride a per-generation volatile item the equivalence check
        never sees, so an ordinary turn boundary keeps the speculative run
        valid and in-game preemptive is worth its cost again. Live games
        therefore keep preemptive ON by default; LILY_LIVE_PREEMPTIVE=false
        restores the G1 behavior if the discard rate proves otherwise."""
        if self.agent is None:
            return
        enabled = (not live) or lily_config.live_preemptive_enabled()
        self.agent.set_preemptive_generation(enabled)
        logger.info(
            "LILY_STATE | PREEMPTIVE_%s | session=%s (game %s)",
            "ON" if enabled else "OFF", self.sk.session_id,
            "live" if live else "not live",
        )

    # -- gated speech dispatch (say-gate WO §1) -------------------------------






    # -- structural delivery claims (desync WO Sub-agent B) -------------------
    #
    # The q_{N}_delivery CLAIM is the delivery-registration event: the
    # answer window opens and the question marks delivered off THAT claim,
    # never off text similarity (the ratio matcher is telemetry only —
    # live evidence showed 0.00–0.15 ratios on questions the table heard,
    # and fallback windows opened against turns carrying no question).
    # Claim triggers, both in tts_node at speech dispatch:
    #   (a) STRUCTURAL — code dispatched a turn whose job is to perform
    #       the armed question (begin_round post-tool turn, the question
    #       nudges, skip/game-start follow-ups): expect_delivery() arms
    #       the flag; the turn claims only when it carries the question
    #       text — otherwise it is rewritten to the deterministic sheet
    #       first (WS-1: never claimed silently);
    #   (b) ORGANIC — any turn while the question is armed and undelivered
    #       that performs its core answer-bearing sentence as written
    #       (lily_evaluation.lily_turn_presents_question).
    # The window still opens at the delivery TURN's playout completion
    # (on_agent_speech_finished) — what changed is WHAT registers delivery.





    # -- regeneration gate (WS-3) --------------------------------------------







    # -- cut-recovery contract (WS-3, WO-LILY-STREAM-INTEGRITY-002) ----------
    #
    # A genuine barge-in or a mid-stream TTS failure ends an organic turn
    # partway through a sentence (the 08-06 live cutoffs: "...how does that
    # sound to you? If" [dead]). Keyed game acts (question delivery, reveal,
    # scores) already recover through the game loop's re-dispatch; ORGANIC
    # conversational turns have no such path, so resumption used to depend
    # on a player re-prompting ("If what?"). This contract makes the resume
    # automatic: when a cut leaves DEAD AIR with no natural follow-up, Lily
    # composes fresh from where meaning broke within one turn.
    #
    # One-emission mandate (omnibus delivery-gate invariant): the resume is
    # a WATCHDOG that fires ONLY into silence. A real barge-in carries a
    # user turn — the normal reply path handles that (with the re-air gate
    # making it fresh), and the user-turn recency guard below suppresses the
    # watchdog so it never double-speaks over a reply already in flight.
    #
    # FLOOR AMENDMENT (WO-LILY-HOTFIX-007 Y10, FLOOR-001 counterweight):
    # "fires only into silence" was never the same claim as "fires only when
    # the floor is hers", and this watchdog used to make the weaker one on
    # its own authority — it dispatched through instructed_reply, so the
    # hold/question-pending/progression/live-game gates in gated_say never
    # saw it (GUARD_MAP chain F). Two bindings now: the fire decision reads
    # LilyGame.floor_state() and chooses silence when the ROOM holds the
    # floor or a hold is active, and the dispatch itself goes through
    # gated_say like every other code-driven turn. Silence is a legitimate
    # outcome of this watchdog, not a failure of it.





    def mark_deterministic_reply(self, text: str) -> None:
        """Mark one user turn as fully handled by deterministic game speech."""
        normalized = lily_evaluation.lily_normalize_answer(text or "")
        if not normalized:
            return
        token = (self.sk.question_number, normalized)
        prehook = getattr(self, "_prehook_answer_suppressions", None)
        if prehook is not None and token in prehook:
            # on_user_turn_completed won the event-order race and already
            # stopped the organic reply. Consume that reservation instead of
            # leaving a stale marker that could suppress a later repeated word.
            prehook.discard(token)
            return
        pending = getattr(self, "_deterministic_reply_texts", None)
        if pending is None:
            pending = self._deterministic_reply_texts = []
        pending.append(normalized)
        del pending[:-6]

    def consume_deterministic_reply(self, text: str) -> bool:
        """Consume an exact handled-turn marker; never suppress a later turn."""
        normalized = lily_evaluation.lily_normalize_answer(text or "")
        pending = getattr(self, "_deterministic_reply_texts", None) or []
        try:
            index = pending.index(normalized)
        except ValueError:
            return False
        del pending[index]
        return True

    def correct_answer_owns_user_turn(self, text: str) -> bool:
        """True when this finalized turn must be answered only by adjudication.

        Runs in on_user_turn_completed, before LiveKit starts the default LLM
        reply. It intentionally duplicates the cheap Tier-1 check because the
        public transcript callback and this hook have no guaranteed ordering.
        """
        if (
            not getattr(self, "game_started", False)
            or getattr(self, "game_over", False)
        ):
            return False
        question = self.sk.current_question or self.armed_question or {}
        if not question:
            return False
        qnum = self.sk.question_number
        delivery_started = getattr(
            self, "_active_delivery_started_at", None
        )
        delivery_ended = getattr(self, "_active_delivery_ended_at", None)
        answer_live = (
            self.sk.answer_window_open
            or (
                getattr(self, "_active_delivery_qnum", None) == qnum
                and delivery_started is not None
                and delivery_ended is None
            )
        )
        if not answer_live:
            return False
        try:
            verdict = lily_evaluation.lily_tier1_evaluate_question(
                text or "", question
            )["verdict"]
        except Exception:
            return False
        if verdict != "correct":
            return False
        normalized = lily_evaluation.lily_normalize_answer(text or "")
        if normalized:
            reservations = getattr(
                self, "_prehook_answer_suppressions", None
            )
            if reservations is None:
                reservations = self._prehook_answer_suppressions = set()
            reservations.add((qnum, normalized))
        return True






    def memory_disclosure_instruction(self) -> str:
        """Task 4 lobby disclosure (WO-LILY-FORGETME-001) — RETURNING
        groups only, frequency-capped (first rematch, then every 5th; the
        persistent counter is the lily_memories row count itself, so no
        new column or migration is needed). Consumed at most once per
        session; cold groups return "" and disclose nothing."""
        if not self.memory_block or self._memory_disclosure_offered:
            return ""
        if not lily_forget.lily_should_disclose_memory(self.memory_total_games):
            return ""
        self._memory_disclosure_offered = True
        return (
            " Fold ONE natural disclosure clause into the same welcome-back "
            "breath — part of the charm, never a warning: you remember your "
            "regulars, and any time they want you to forget, they just say "
            "so ('...I remember my regulars — any time you want me to "
            "forget, just say so')."
        )

    def prefs_offer_instruction(self) -> str:
        """Group prefs WO ask-once flow: a returning group with stored
        preferences gets ONE simple question after the composed welcome —
        play the usual, or change anything? Consumed at most once per
        session (never re-asked tonight); cold groups (no stored prefs)
        return "" — their preferences get captured as they choose during
        the walkthrough/lobby, no extra ceremony."""
        if not self.prefs or self._prefs_offer_made:
            return ""
        usual = lily_memory.lily_prefs_summary(self.prefs)
        if not usual:
            return ""
        self._prefs_offer_made = True
        return (
            " AFTER the composed welcome, ask ONCE, simply — do they want "
            f"to play the usual, or change anything? (their usual: {usual} "
            "— the [RETURNING TABLE] context carries it too). A yes / 'the "
            "usual' needs NO announcement and no tool call: the stored "
            "preferences apply on their own when the game starts — just "
            "move on. If they change something, act on the change (the "
            "spoken choice or its tool saves it as the new usual). Never "
            "ask about preferences again tonight."
        )

    def whats_new_instruction(self) -> str:
        """WO-LILY-SELFKNOWLEDGE-INTAKE-001 Task 3 — the rematch delta.

        Called from greeting_instructions' RETURNING-TABLE branch only.
        Compares the group's stamped last_seen_feature_version (a key in
        the opaque prefs dict — joins the forget cascade and re-key like
        everything group-keyed) against the codebase manifest:

          - stamp missing (returning table from before stamping existed,
            or a cold row): stamp forward SILENTLY — we cannot know what
            they saw, and a false "new since last time" claim would be
            its own small fabrication. Real deltas start next rematch.
          - stamp current: silence.
          - stamp lagged: ONE casual, frequency-capped line about only
            the delta; the stamp moves forward AFTER the mention (the
            session_greet confirm in on_agent_speech_finished).
        """
        current = lily_capabilities.lily_feature_version()
        prefs = self.prefs or {}
        stamp = prefs.get("last_seen_feature_version")
        if stamp is None:
            self._stamp_feature_version()
            return ""
        try:
            seen = int(stamp)
        except (TypeError, ValueError):
            self._stamp_feature_version()
            return ""
        if seen >= current:
            return ""
        delta = lily_capabilities.lily_whats_new(seen)
        if not delta:
            self._stamp_feature_version()
            return ""
        # Edge (A): emit the delta through the FIRST carrier only. Without this
        # latch, a cut greet leaves the stamp lagged and both the
        # late-recognition beat and the game-start ride-along compose it.
        if getattr(self, "_whats_new_emitted", False):
            return ""
        self._whats_new_emitted = True
        self._whats_new_pending = True
        listed = "; ".join(delta)
        return (
            " ONE more beat, casual and quick, folded into the welcome — "
            "since this table last played you picked up something new: "
            f"{listed}. One light line ('since you were last here I "
            "picked up a couple of tricks'), never a feature list read "
            "aloud, and never mention it again tonight."
        )



    # -- WS-5: MC answer-aborts-read + buzz-buffer widening -------------------











    def note_post_tts_text(self, speech_id: str | None, text: str) -> None:
        """Bind the exact final TTS input to one speech handle."""
        if not speech_id:
            return
        mapping = getattr(self, "_post_tts_text_by_speech_id", None)
        if mapping is None:
            mapping = self._post_tts_text_by_speech_id = {}
        mapping[speech_id] = (text or "").strip()
        while len(mapping) > 32:
            mapping.pop(next(iter(mapping)))

    def peek_post_tts_text(self, speech_id: str | None) -> str | None:
        """Read the bound TTS text WITHOUT consuming it (transcript-sync:
        the playout-start interim publish needs the text; the one-shot
        consume stays with playout completion, Y5 contract untouched)."""
        if not speech_id:
            return None
        mapping = getattr(self, "_post_tts_text_by_speech_id", None) or {}
        return mapping.get(speech_id)

    def consume_post_tts_text(
        self, speech_id: str | None, fallback: str
    ) -> str:
        """Return and consume the text that actually entered TTS."""
        if not speech_id:
            return (fallback or "").strip()
        mapping = getattr(self, "_post_tts_text_by_speech_id", None) or {}
        final = mapping.pop(speech_id, None)
        if final is None:
            return (fallback or "").strip()
        if final != (fallback or "").strip():
            # Y5 (WO-LILY-HOTFIX-007): this is NOT a post-hoc rewrite of
            # aired speech — it is the record being bound TO the exact text
            # that entered TTS (P0-C), after every pre-synthesis clip and
            # substitution. The old tag POST_TTS_REWRITE read as
            # falsification and sent a whole day of transcript analysis
            # the wrong way; the raw text here is the model's PRE-clip
            # prose, which never aired. The record follows aired truth by
            # construction: clip (tts_node) -> note_post_tts_text ->
            # synthesis of that same text -> this consume at playout.
            logger.info(
                "LILY_TRANSCRIPT | RECORD_BOUND_TO_TTS_INPUT | session=%s "
                "speech_id=%s raw_len=%d final_len=%d raw=%r final=%r",
                self.sk.session_id, speech_id,
                len((fallback or "").strip()), len(final),
                (fallback or "")[:160], final[:160],
            )
        return final



    def record_agent_turn(
        self, text: str, *, act_keys: list, interrupted: bool
    ) -> None:
        """WO-LILY-RECOGNITION-VARIETY-001 Task 0 — persist Lily's OWN turn.

        Local first (scorekeeper: report transcript + SAID-ALREADY ledger +
        the repeat-lint window), then fire-and-forget to lily_transcripts
        as a speaker_label='LILY' row. Zero-migration note: the row's
        speaker_name slot (meaningless for an agent row) carries the
        primary speech-act key where one confirmed — e.g. 'q_3_delivery'.
        Post-forget, the batcher is disabled and the write is a no-op,
        same as player rows. Never raises."""
        clean = (text or "").strip()
        if not clean:
            return
        # HOTFIX-002 belt, WIDENED by PATCH-001 T3 (RETIRE_WITH_WS6): a
        # verbatim repeat of ANY recently-recorded turn is the echo/re-air
        # bug class — every live duplicate pair had a user row interleaved,
        # so last-turn-only matching missed all of them. Short turns are
        # exempt (an honest "Nice one!" may legitimately recur).
        prior_turns = getattr(self.sk, "agent_turns", None) or []
        # The `interrupted` exemption used to live here, on the reasoning
        # that a cut turn "partially played and belongs in the record,
        # marked". The first one does. The FOURTH does not.
        #
        # Live 2026-08-08 `lily-2C489B`, with every copy marked cut off:
        #   "Great to meet you, Rami! Are you flying solo tonight…"  x4
        #   the burlesque delivery                                   x3
        #   "Yeah"                                                   x3
        # Each re-air was cut by his next sentence and re-recorded, and
        # because record_agent_turn feeds sk.agent_turns — which IS her
        # conversational context — she then read her own line back four
        # times and said it again. "Okay, now you're repeating yourself."
        #
        # In a game built on shouting over the host, an exemption for cut
        # turns is an exemption for the common case. One record per
        # distinct line; the repeats are the same cut, not new speech.
        if len(clean) >= 15 and clean in prior_turns[-6:]:
            logger.warning(
                "LILY_TURNS | DUP_TURN_SKIPPED | path=record | session=%s "
                "— verbatim repeat of a recent recorded turn, not recorded",
                self.sk.session_id,
            )
            return
        try:
            self.sk.record_agent_turn(
                clean,
                timestamp=time.time(),
                act_keys=act_keys,
                interrupted=interrupted,
            )
        except Exception as e:
            logger.error("LILY_TURNS | LOCAL_RECORD_FAILED | %s", e)
        batcher = getattr(self, "transcripts", None)
        if batcher is None:
            # A live session always has the batcher; its absence means the
            # agent side of the record is dark — say so (HOTFIX-002: a
            # silent write failure blinded a full session).
            logger.error(
                "LILY_TURNS | TRANSCRIPT_PERSIST_UNAVAILABLE | session=%s "
                "— no batcher, LILY row not written", self.sk.session_id,
            )
            return
        try:
            # segment_end = playout completion (this method runs at
            # on_agent_speech_finished) — the LILY row's timing anchor,
            # same epoch clock as the player rows (HOTFIX-002).
            batcher.add(
                clean + (" …[cut off]" if interrupted else ""),
                speaker_label="LILY",
                speaker_name=(act_keys[0] if act_keys else None),
                segment_end=time.time(),
            )
        except Exception as e:
            logger.error(
                "LILY_TURNS | TRANSCRIPT_PERSIST_FAILED | session=%s | %s",
                self.sk.session_id, e,
            )

    def note_intake_overlap(
        self, label: str, start_ts: float, end_ts: float
    ) -> None:
        """Intake choreography (self-knowledge WO Task 4) — lobby overlap.

        Reuses the H1 Task 2 timestamp-overlap signal shape (same epsilon)
        OUTSIDE the answer window: pre-game, two final segments from
        different speaker labels overlapping in time means two people
        talked over each other during name intake. She must order, never
        guess — a one-shot state note hands her the repair. Rate-limited
        (one note per 20s) so a chatty lobby doesn't spam the context;
        the note is consumed by the next turn like every state note.
        """
        prev = getattr(self, "_intake_last_segment", None)
        self._intake_last_segment = (label, start_ts, end_ts)
        if prev is None or not label:
            return
        prev_label, _prev_start, prev_end = prev
        if not prev_label or prev_label == label:
            return
        epsilon = lily_config.overlap_epsilon_seconds()
        if start_ts >= (prev_end - epsilon):
            return
        now = time.monotonic()
        last_noted = getattr(self, "_intake_overlap_noted_at", 0.0)
        if now - last_noted < 20.0:
            return
        self._intake_overlap_noted_at = now
        logger.info(
            "LILY_INTAKE | OVERLAP | session=%s labels=%s/%s",
            self.sk.session_id, prev_label, label,
        )
        self.sk.set_status_note(
            "two voices just overlapped during intake — do not guess who "
            "was who: run the ordering repair ('two of you at once — you "
            "first, then you') and take the names one at a time"
        )

    async def show_demo_picture(self, adult: bool = False) -> tuple[bool, str]:
        """Self-knowledge WO Task 1 symmetry, live-fixture fix (12:47
        transcript): "show me" must GET SHOWN — before this existed, a
        skeptic asking to see picture rounds five times got a fabricated
        screen push ("I've just pushed a visual preview to your display").
        This is the real mechanism: put one actual image on the glass via
        the SAME metadata path picture questions use (the render path is
        not phase-gated; generation needs no EXA key).

        Cache-first: any bank image already in the bucket; else one
        generated demo image. Returns (landed, line_for_lily) — the tool
        result is the ONLY thing that makes "look at the screen" true.
        Never raises."""
        url = None
        supabase = getattr(self, "supabase", None)
        # Adult sample (owner directive 2026-08-06): skip the general bank
        # shortcut — the point is to show the ADULT deck's art direction
        # (realistic comic-book style), so it always rides the generated
        # rail with lily_adult_style applied.
        if supabase is not None and not adult:
            try:
                result = await asyncio.to_thread(
                    lambda: supabase.table("lily_questions")
                    .select("image_url")
                    .neq("image_url", "")
                    .not_.is_("image_url", "null")
                    .limit(1)
                    .execute()
                )
                rows = getattr(result, "data", None) or []
                if rows and rows[0].get("image_url"):
                    url = rows[0]["image_url"]
            except Exception as e:
                logger.warning("LILY_DEMO | bank image lookup failed: %s", e)
        if not url and getattr(self, "reasoning", None) is not None:
            # Generation rides the reasoning node — the one legal image
            # seam (web guardrail: the vocal module never touches the
            # image stack directly).
            url = await self.reasoning.generate_demo_image(
                supabase,
                session_id=self.sk.session_id,
                adult=adult,
                intensity=self.sk.adult_image_intensity,
            )
        if not url:
            logger.warning("LILY_DEMO | no image available")
            return (
                False,
                "no image could be produced right now — say exactly that, "
                "honestly ('the picture rail isn't cooperating right this "
                "second'), and do NOT claim anything reached the screen",
            )
        await self.publish_metadata("", image_url=url, category="demo")
        logger.info("LILY_DEMO | PUBLISHED | url=%s", url[:80])
        return (
            True,
            "a real demo image just landed on the screen — point the table "
            "at it ('there it is — that's the picture rail')",
        )

    def _stamp_feature_version(self) -> None:
        """Move the group's last_seen_feature_version stamp to the current
        manifest version and persist the whole prefs dict (fire-and-forget,
        same path as every preference write). Idempotent."""
        current = lily_capabilities.lily_feature_version()
        prefs = dict(self.prefs or {})
        if prefs.get("last_seen_feature_version") == current:
            return
        prefs["last_seen_feature_version"] = current
        self.prefs = prefs
        # getattr: test harnesses build LilyGame via __new__.
        logger.info(
            "LILY_PREFS | FEATURE_STAMP | session=%s group=%s version=%d",
            self.sk.session_id, getattr(self, "group_id", None), current,
        )
        if getattr(self, "supabase", None) is not None:
            self.persist_prefs()





    async def await_greeting_memory(self) -> None:
        """Memory at the door (WO-LILY-DESYNC-HONESTY-001 F): hold the
        composed greeting until group resolution + memory load have
        settled, within a hard budget (LILY_GREETING_MEMORY_BUDGET_SECONDS,
        default 1.5s). The live failure: the greeting fired one turn
        BEFORE [RETURNING TABLE] landed and a four-time table got the cold
        'who do we have at the table tonight?'. On timeout the greeting
        goes out cold exactly as before and recognition arrives naturally
        — the room is never blocked beyond the budget."""
        # getattr: test harnesses build LilyGame via __new__ without the
        # full __init__ attribute set.
        event = getattr(self, "memory_settled", None)
        if event is None or event.is_set():
            return
        budget = lily_config.greeting_memory_budget_seconds()
        if budget <= 0:
            return
        started = time.time()
        try:
            await asyncio.wait_for(event.wait(), timeout=budget)
            logger.info(
                "LILY_MEMORY | GREETING_AWAIT | settled in %.2fs "
                "(budget %.1fs) — recognition rides the first utterance",
                time.time() - started, budget,
            )
        except asyncio.TimeoutError:
            logger.info(
                "LILY_MEMORY | GREETING_AWAIT | timeout after %.1fs — "
                "greeting cold; recognition arrives naturally if memory "
                "resolves later", budget,
            )



    def can_claim_empty_memory(self) -> bool:
        """P0-A: True only when absence of memory is a *settled* fact.

        Clean-slate / nothing-on-file language is forbidden while the
        voice-identity probe is outstanding, a device candidate is pending,
        a memory block already exists, or the player explicitly asserted
        prior contact. Deterministic UNKNOWN≠EMPTY; a blank card never
        disproves the player's memory.
        """
        if getattr(self, "_returner_claim_seen", False):
            return False
        if getattr(self, "memory_block", None):
            return False
        if getattr(self, "device_candidate_group_id", None):
            return False
        if self.identity_probe_outstanding():
            return False
        if lily_config.voice_identity_enabled() and getattr(
            self, "supabase", None
        ) is not None:
            return bool(getattr(self, "_voice_identity_resolved", False))
        return True

    def must_rewrite_false_empty_claim(self, text: str) -> bool:
        """TTS hard gate for settled-absence claims.

        A forbidden phrase is rewritten whenever identity is unsettled OR
        this session has an explicit returner claim. The persistent claim
        bit closes the one-shot-note race from BE8D8B.
        """
        if not lily_say_gate.lily_false_clean_slate_claim(text):
            return False
        return bool(
            getattr(self, "_returner_claim_seen", False)
            or getattr(self, "_returner_honesty_note", None)
            or getattr(self, "_recognition_dispute", False)
            or not self.can_claim_empty_memory()
        )


    def ambiguous_yes_blocks_start(self) -> bool:
        """WO-2: bare yes after an A-or-B offer must not open the round."""
        return bool(getattr(self, "_ambiguous_yes_blocks_start", False))

    def pending_setup_jobs(self) -> set[str]:
        """Requested lobby setup not yet committed in code."""
        return set(getattr(self, "_setup_pending", set()) or set())

    def note_confirmed_name_evidence(
        self, speaker_label: str, player_name: str
    ) -> None:
        label = (speaker_label or "").strip().strip("[]")
        name = (player_name or "").strip()
        if not label or not lily_is_valid_name(name):
            return
        evidence = getattr(self, "_confirmed_name_evidence", None)
        if evidence is None:
            evidence = self._confirmed_name_evidence = {}
        evidence[label] = name
        logger.info(
            "LILY_BIND | NAME_EVIDENCE | session=%s label=%s name=%s",
            self.sk.session_id, label, name,
        )
        # HOTFIX-009 W7: a correction propagates to the glass in ONE seam
        # update. When this label is already bound under a plausibly-same
        # name, the player's own corrected words re-drive the EXISTING
        # bind path (rename-migrate + on_speaker_bound -> attribute
        # publish) instead of waiting on the model to re-call the tool —
        # live 2026-08-10, the NATO correction changed nothing on the
        # glass until the player complained a second time. A DISSIMILAR
        # name on a bound label (second voice on a reused label, mid-game
        # "call me X") is a new binding decision: evidence is recorded,
        # nothing auto-rebinds — the tool path owns that call.
        holder = next(
            (
                n for n, st in self.sk.players.items()
                if st.get("speaker_label") == label
            ),
            None,
        )
        if (
            holder is not None
            and holder != name
            and lily_names_probably_same(holder, name)
        ):
            self.sk.bind_speaker(label, name, rename=True)
            self._migrate_agent_name_refs(holder, name)
            note = self.on_speaker_bound(label, name)
            if note and note.strip():
                # A make-good committed during the auto-rebind must reach
                # the model — same one-shot state-note surface as the
                # honesty assists (context only, leak-filtered).
                self._state_note = f"[state note: {note.strip()}]"

    def _migrate_agent_name_refs(self, old: str, new: str) -> None:
        """Post-rename sweep of agent-side name snapshots living OUTSIDE
        sk.players. The derivation sweep found exactly one:
        prewager_standings, compared BY NAME at wrap-up — unmigrated, a
        rename between the final wager and wrap-up mints a false "took
        the crown" highlight. No-op when the migration writer refused
        (old still rostered) or nothing renamed."""
        if old == new or old in self.sk.players:
            return
        for row in getattr(self, "prewager_standings", None) or ():
            if row.get("name") == old:
                row["name"] = new

    def confirmed_name_for_label(self, speaker_label: str) -> str | None:
        label = (speaker_label or "").strip().strip("[]")
        evidence = getattr(self, "_confirmed_name_evidence", None) or {}
        return evidence.get(label)

    def mark_setup_applied(self, *jobs: str) -> None:
        """Clear setup jobs only after their real state mutation succeeds."""
        pending = getattr(self, "_setup_pending", None)
        if pending is None:
            self._setup_pending = set()
            pending = self._setup_pending
        before = set(pending)
        pending.difference_update(jobs)
        if before != pending:
            logger.info(
                "LILY_SETUP | APPLIED | session=%s jobs=%s pending=%s",
                getattr(self.sk, "session_id", "?"),
                ",".join(sorted(jobs)),
                ",".join(sorted(pending)) or "none",
            )

    def note_lobby_setup_intents(self, text: str) -> dict:
        """Parse and merge every setup intent before any kickoff dispatch.

        Deterministic jobs commit here when safe. Adult/voice/heat stay
        pending until their tools actually mutate state. P0-3 owns broader
        consent semantics; age mention is enough to hold start fail-closed.
        """
        intents = lily_scorekeeper.lily_parse_lobby_setup_intents(text)
        if getattr(self, "game_started", False):
            return intents
        requested = getattr(self, "_setup_requested", None)
        pending = getattr(self, "_setup_pending", None)
        if requested is None:
            self._setup_requested = set()
            requested = self._setup_requested
        if pending is None:
            self._setup_pending = set()
            pending = self._setup_pending

        if intents["start"]:
            self._setup_start_requested = True
        if intents["voice"]:
            requested.add("voice")
            pending.add("voice")
        if intents["adult"]:
            requested.add("adult")
            if self.sk.mode == "adult":
                pending.discard("adult")
            else:
                pending.add("adult")
        if intents["age_mentioned"]:
            requested.add("consent")
            if getattr(self, "_age_consent_confirmed", False):
                pending.discard("consent")
            else:
                pending.add("consent")
        if intents["media"] == "pictures":
            requested.add("pictures")
            # If adult/heat is also requested, do not draw a general image
            # while setup is incomplete (the BE8D8B Greece-on-general bug).
            if intents["adult"] or "adult" in pending or intents["heat"]:
                pending.add("pictures")
            else:
                outcome = self.try_activate_pictures(
                    source="multi_intent_setup", announce=False
                )
                if outcome in ("on", "already_on"):
                    pending.discard("pictures")
                else:
                    pending.add("pictures")
        elif intents["media"] == "voice_only":
            self.sk.set_media_mode("voice_only")
            self.publish_attributes_nowait()
            pending.discard("pictures")
        if intents["heat"]:
            self._setup_heat_requested = intents["heat"]
            requested.add("heat")
            if (
                self.sk.mode == "adult"
                and self.sk.adult_image_intensity == intents["heat"]
                and self.sk.media_mode == "pictures"
            ):
                pending.discard("heat")
                pending.discard("pictures")
            else:
                pending.add("heat")

        if any(
            (
                intents["start"],
                intents["voice"],
                intents["adult"],
                intents["media"],
                intents["heat"],
                intents["age_mentioned"],
            )
        ):
            logger.info(
                "LILY_SETUP | INTENTS | session=%s start=%s voice=%s "
                "adult=%s media=%s heat=%s consent_heard=%s pending=%s",
                getattr(self.sk, "session_id", "?"),
                intents["start"], intents["voice"], intents["adult"],
                intents["media"], intents["heat"],
                getattr(self, "_age_consent_confirmed", False),
                ",".join(sorted(pending)) or "none",
            )
        return intents




    def unowned_kickoff_must_suppress(
        self, text: str, delivery_result: str | None
    ) -> bool:
        """P0-5: kickoff words may air only with q_N_delivery ownership.

        WIDENED 2026-08-09 to the QUESTION ITSELF, which is the thing P0-5
        was really protecting. Live `lily-2C489B` 22:49:15: one turn carried
        the recognition beat, the offer "want a quick refresher on the
        options, or straight in?", AND the question — she asked him what he
        wanted and answered it herself in the same breath. He said so:
        "you asked me if I want a refresher. You didn't give me a chance to
        speak." A turn that does not own the delivery may not speak the
        armed question; the offer then has to wait for a reply, because
        nothing else can fill the silence with the question."""
        owns_delivery = delivery_result in (
            "claimed_structural",
            "claimed_core_sentence",
        )
        if owns_delivery:
            return False
        if lily_say_gate.lily_unowned_kickoff_fragment(text):
            return True
        # Same predicate the delivery path already uses to decide whether a
        # turn presented the armed question — asked here of a turn that has
        # no right to present it.
        if self.armed_question is None or self.sk.answer_window_open:
            return False
        return self._delivery_text_matches_armed(text or "")

    def clear_ambiguous_yes_block(self, *, reason: str) -> None:
        if getattr(self, "_ambiguous_yes_blocks_start", False) or getattr(
            self, "_pending_or_choice_offer", False
        ):
            logger.info(
                "LILY_STATE | AMBIGUOUS_YES_CLEAR | session=%s reason=%s",
                getattr(self.sk, "session_id", "?"), reason,
            )
        self._ambiguous_yes_blocks_start = False
        self._pending_or_choice_offer = False

    def note_or_choice_offer(self, spoken_text: str) -> None:
        """Arm after Lily asks ready-or-waiting / A-or-B (playout path)."""
        if not spoken_text or getattr(self, "game_started", False):
            return
        if lily_scorekeeper.lily_detect_or_choice_offer(spoken_text):
            self._pending_or_choice_offer = True
            logger.info(
                "LILY_STATE | OR_CHOICE_OFFER | session=%s — bare yes will "
                "not start",
                getattr(self.sk, "session_id", "?"),
            )

    def note_user_start_intent(self, text: str, command: str | None) -> None:
        """Consume an A-or-B pending offer against the user's reply."""
        if getattr(self, "game_started", False):
            self.clear_ambiguous_yes_block(reason="game_started")
            return
        if command == "start_game":
            self.clear_ambiguous_yes_block(reason="explicit_start_command")
            return
        if not getattr(self, "_pending_or_choice_offer", False):
            return
        if lily_scorekeeper.lily_is_bare_affirmative(text):
            self._ambiguous_yes_blocks_start = True
            self._pending_or_choice_offer = False
            logger.info(
                "LILY_STATE | AMBIGUOUS_YES_BLOCK | session=%s text=%r — "
                "kickoff locked until explicit start",
                getattr(self.sk, "session_id", "?"), str(text)[:80],
            )
            return
        # Any non-bare reply consumes the offer without locking (they
        # answered the choice conversationally).
        self._pending_or_choice_offer = False


    def greeting_instructions(self) -> str:
        """The fresh-room landing line (single source of truth — both the
        on_enter and entrypoint trigger paths dispatch THIS text under the
        session_greet key; the gate makes the second path silent).

        Composed from ordered parts, never one rigid line (principal
        correction: a live session opened with 'welcome back everyone' and
        NO self-intro): (1) ALWAYS the one-breath self-intro first — every
        session, returning or not; (2) then the recognition nuance,
        composed per-player from the memory/roster data (whole table
        returning / mixed table / all new); (3) the first-time question is
        asked only when memory gives no answer — when memory KNOWS, she
        acts on it. The walkthrough/refresher draws on the prompt's WHAT
        THE TABLE CAN ASK FOR block, at most once per session."""
        parts = [
            "The room just opened — this is your landing. Compose it from "
            "these parts, in this order, as ONE natural beat. PART ONE, "
            "always — every session, returning table or not: a very quick "
            "self-introduction in one breath — 'Hi, I'm Lily —' you host "
            "trivia. Never skip it, never stretch it into a monologue. "
        ]
        # V3 (HOTFIX-010): the OPENING turn is PART ONE + ONE bare orienting
        # beat, then silence until a human speaks. The orienting beat is
        # always NAME-SAFE — a device/group token identifies a device or a
        # GROUP, never the people at the mic, so it never recites remembered
        # names. Recognition, the walkthrough, prefs and what's-new are
        # deferred: they ride the persistent [RETURNING TABLE] context, the
        # late-recognition beat, and the game-start ride-along once a voice
        # is actually present.
        if getattr(self, "device_candidate_group_id", None):
            parts.append(
                "PART TWO — the DEVICE looks familiar, but no current voice "
                "has been verified. Say only that the device looks familiar "
                "and ask who is playing tonight. Do NOT say welcome back, "
                "do NOT call anyone a returner, and do NOT mention prior "
                "names, winners, counts, dates, preferences, or facts. "
                "Different people may share a device; voice verification "
                "must happen first."
            )
        else:
            parts.append(
                "PART TWO — one light orienting beat: ask who's at the mic "
                "tonight, then STOP and let them speak. Do NOT recite names, "
                "winners, counts, dates, or history, and do NOT announce "
                "whether it's their first time — no voice has been matched "
                "present yet, so there is no one to name."
            )
        # Deferred rich beats — recognition / walkthrough / claimed-returner /
        # prefs / what's-new — are NOT part of the cold opener. This block is
        # gated on the first human utterance, which is false at the cold-open
        # dispatch, so the opener stays a bare intro + orienting beat and the
        # beats land through their mid-session paths. No _recognized_at_greet
        # is set: recognition is single-sourced through the late-recognition /
        # game-start paths, which closes the greet-dispatch↔memory-promotion
        # race without a mirrored guard.
        # HOTFIX-010 V4 edge (B): when a DEVICE candidate is pending (device
        # looks familiar, no present voice verified), PART TWO above already
        # owns the turn — "ask who's playing, verify voice first". The
        # deferred first-time / claimed-returner framing below contradicts it
        # (it asks whether it's their first time, off an unverified device),
        # so it must NOT also compose. Recognition rides the mid-session
        # late-recognition / game-start beats once the candidate promotes or
        # clears. Gating here removes the contradictory co-composition rather
        # than layering a reconciler on top.
        if getattr(self, "_first_human_utterance_seen", False) and not getattr(
            self, "device_candidate_group_id", None
        ):
            if self.memory_block:
                parts.append(
                    "Your memory KNOWS this TABLE (the [RETURNING TABLE] "
                    "context carries this group's history), but that is a "
                    "GROUP match, not proof of who is at the mic right now. "
                    "Acknowledge the TABLE — 'I know this table', rematch "
                    "energy, that you've played here before — WITHOUT "
                    "reciting a roster of names from memory. Name a person "
                    "ONLY when THEIR voice is matched present this session "
                    "(the ROSTER field is the sole naming authority) or they "
                    "have stated their name tonight; a remembered name is NOT "
                    "a present person. If no voice is matched present yet, "
                    "name no one and ask who's at the mic. Do NOT ask if "
                    "it's their first time — your memory already answers "
                    "that. Lean into the rematch, but do NOT say 'welcome "
                    "back, <name>', and do NOT list prior players, winners, "
                    "or newcomers by name off the record. Returners get no "
                    "walkthrough — offer "
                    "ONCE, 'want a refresher on the options, or straight in?', "
                    "and respect the answer. The walkthrough or refresher "
                    "draws on WHAT THE TABLE CAN ASK FOR and happens at most "
                    "once tonight."
                    + self.memory_disclosure_instruction()
                    + self.prefs_offer_instruction()
                    + self.whats_new_instruction()
                )
            else:
                # Neutral-history rule: without memory data, never claim OR
                # deny prior contact (memory may still resolve mid-lobby via a
                # group-id upgrade).
                parts.append(
                    "Your memory gives no answer about this table, so ask: a "
                    "plain warm welcome, then whether it's their first time "
                    "playing with you. FIRST TIME: walk them through their "
                    "options naturally, drawing on the WHAT THE TABLE CAN ASK "
                    "FOR block — conversational, folded into the banter, "
                    "never a feature list read aloud. CLAIMED RETURNER — they "
                    "say it's NOT their first time but your memory has "
                    "nothing: BELIEVE THEM and name the gap plainly in ONE "
                    "light beat, WITHOUT diagnosing a cause — 'I don't "
                    "recognise the voice yet, and I don't know why' — never "
                    "'my table card doesn't have you', never 'new device', "
                    "never 'cleared browser', never anything on their end "
                    "(you cannot see which link dropped, and a confident "
                    "wrong cause blames a player for a backend fault) and IN "
                    "THE SAME TURN offer the refresher exactly as a "
                    "recognized returner would get it — 'want a refresher on "
                    "the options, or straight in?' — and respect the answer. "
                    "Never perform vague amnesia you could explain, never "
                    "claim recognition you don't have, and never argue with "
                    "their memory of you. If recognition catches up mid-game "
                    "(the [RETURNING TABLE] block appears), you'll get an "
                    "instruction beat for it. Either way the walkthrough or "
                    "refresher happens at most once tonight. Never claim you "
                    "remember them, and never announce it's their first time "
                    "— let them tell you."
                )
                if self.identity_probe_outstanding():
                    # HOTFIX-006 N1: the probe has NOT come back. "My card
                    # doesn't have you" is a claim about memory, and right now
                    # its truth is unknown. Override the gap-naming beat: say
                    # nothing about memory at all.
                    parts.append(
                        " OVERRIDE — MEMORY IS UNRESOLVED, NOT ABSENT. Your "
                        "recognition check has not finished. You therefore do "
                        "NOT know whether you have played with this table "
                        "before, and you must not speak as if you do. Say "
                        "NOTHING about your memory, your card, your ledger or "
                        "a clean slate — no 'blank slate', no 'clean slate', "
                        "no 'my card doesn't have you', not even to concede a "
                        "gap. If they say they have played before, take it at "
                        "face value warmly and move on ('good to have you "
                        "back') without characterising what you do or do not "
                        "hold. Recognition may land within the next minute and "
                        "you will get a beat for it; a denial spoken now is a "
                        "denial you will have to retract."
                    )
        parts.append(
            " Bind names as people speak. At least one confirmed name is "
            "required. Only clear start language authorizes calling "
            "lily_begin_round; a laugh, general energy, or a bare yes to "
            "another choice does not. Nothing scores until the round opens."
        )
        return "".join(parts)

    def rejoin_instructions(self) -> str:
        """The reconnect re-entry line — its own key (session_rejoin) and
        its own register; it must NOT trip session_greet."""
        instructions = (
            "You just reconnected mid-game — the state block has the "
            "scores intact. A quick in-character rejoin line ('lost you "
            "for a second — nobody touched the scores, I counted'), "
            "then pick the game back up."
        )
        if getattr(self, "armed_question", None) is not None:
            instructions += (
                " The interrupted current question is restored in the "
                "state block: ask it now, word for word, without advancing "
                "the question number first."
            )
        return instructions

    # -- question supply ------------------------------------------------------

    def _round_for_next_question(self) -> int:
        return min(
            self.rounds_total + 1,
            self.sk.question_number // self.sk.questions_per_round + 1,
        )

    def _category_for_round(self, rnd: int) -> str:
        # Adult identity (WO-LILY-DESYNC-HONESTY-001 D): the adult deck
        # rotates through ITS OWN families — the general round-family
        # rotation never labels an adult question (live defect: adult
        # questions announced as "academic category"). Bank rows keep the
        # category they carry (the fetch prefers a family match but always
        # serves the row's own label); this family is what generation and
        # bank preference ask for.
        if self.sk.mode == "adult":
            return ADULT_CATEGORY_FAMILIES[
                (rnd - 1) % len(ADULT_CATEGORY_FAMILIES)
            ]
        # Operator-requested topic wins over the family rotation for the
        # round it was set on (getattr: test harnesses build via __new__).
        override = getattr(self, "_category_override", {}).get(rnd)
        if override:
            return override
        return CATEGORY_FAMILIES[(rnd - 1) % len(CATEGORY_FAMILIES)]

    def _is_operator_category(self, category: str) -> bool:
        """True when `category` is a topic the table asked for on the fly
        (lily_set_category), not a fixed family-rotation slot. Operator
        topics prefer the bank (previously-generated questions for that
        topic) before regenerating — the compounding arsenal."""
        if not category:
            return False
        return category in set(getattr(self, "_category_override", {}).values())

    # -- custom rounds: registration is the only proof (HOTFIX-006 N2) --------
    #
    # The lily-16A9AE failure was not a bad question, a bad category or a bad
    # generator. It was a SEQUENCE: the confirmation went out first and the
    # round was expected to catch up, and when it didn't, nothing downstream
    # could tell. Same shape as X1 (a score narrated ahead of the ledger) and
    # X3 (a reveal aired ahead of its delivery), and the same cure: the claim
    # is derived from the record, so it cannot run ahead of it.

    def _register_custom_question(self, category: str, question: dict) -> None:
        """Record one built question against the topic it was built for.

        The ONLY writer of _custom_round_registered. Called at the prefetch
        commit — after verification, after the deck/category switch guards,
        with the question in hand and its category stamped — so an entry here
        means "a real question for this topic is committed to the supply
        line and will be written to lily_asked_history under it at arm"."""
        if not self._is_operator_category(category) or not isinstance(
            question, dict
        ):
            return
        if str(question.get("category") or "") != category:
            # A question that does not carry the topic is not evidence FOR
            # the topic, whatever it was drawn under.
            return
        key = lily_bank.lily_normalize_category_name(category)
        if not key:
            return
        registry = getattr(self, "_custom_round_registered", None)
        if registry is None:
            self._custom_round_registered = registry = {}
        ids = registry.setdefault(key, [])
        qid = str(question.get("id") or "")
        if qid and qid not in ids:
            ids.append(qid)
        logger.info(
            "LILY_CUSTOM_ROUND | REGISTERED | session=%s topic=%r id=%s "
            "total=%d",
            self.sk.session_id, category, qid, len(ids),
        )





    def _difficulty_for_round(self, rnd: int) -> int:
        if rnd <= 1:
            return 1
        if rnd > self.rounds_total:
            return 4  # final wager question runs mean
        return min(3, rnd)

    def _picture_kind_for_slot(self, rnd: int) -> str | None:
        """Which picture builder serves the NEXT question, if any
        (WO-LILY-OMNIBUS-002 K gating). Pictures are a lobby choice:
        media_mode='voice_only' (the default) excludes picture questions
        entirely. The final wager round stays text. Adult mode supplies
        picture rounds exactly like the other decks (generated images route
        to the Grok adult model downstream). In pictures mode, round
        REAL_OR_IMAGINED_ROUND is the reference picture round (every
        question); otherwise the first question of each round is a
        real-entity 'name this landmark' slot."""
        if self.sk.media_mode != "pictures":
            return None
        if rnd > self.rounds_total:
            return None  # the wager question is always text
        if rnd == lily_reasoning.REAL_OR_IMAGINED_ROUND:
            return "real_or_imagined"
        if self.sk.question_number % self.sk.questions_per_round == 0:
            # question_number counts STARTED questions, so the next one is
            # first-of-round exactly when the count is a whole number of
            # rounds.
            return "real_entity"
        return None



    WATCHDOG_INTERVAL_SECONDS = 10.0
    PREFETCH_HARD_TIMEOUT_TICKS = 9  # ~90s: past every internal timeout
    # Z2 (HOTFIX-008) — phase-independent supply recovery:
    SUPPLY_RETRY_MAX = 1          # de-escalated re-prefetch attempts per incident
    SUPPLY_SILENT_WARN_TICKS = 2  # silent-window ticks before WARN + recovery

    def start_idle_watchdog(self) -> None:
        # getattr: test harnesses build LilyGame via __new__ without the
        # full __init__ attribute set.
        task = getattr(self, "_watchdog_task", None)
        if task and not task.done():
            return
        self._prefetch_stall_ticks = 0
        self._armed_limbo_ticks = 0
        self._undelivered_ticks = 0
        self._undelivered_refires = 0
        self._supply_stall_ticks = 0
        self._supply_silent_ticks = 0
        self._supply_retry_attempts = 0
        self._supply_exhausted_notified = False
        self._watchdog_task = asyncio.ensure_future(self._idle_watchdog())

    def stop_idle_watchdog(self) -> None:
        """Session close: stop the watchdog for good (2026-08-06 log
        audit — the loop survived the AgentSession and its first
        post-close tick dispatched against a dead session: TICK_FAILED
        `AgentSession isn't running`, once per hangup). Cancel plus the
        _session_closed flag (belt for a tick already past the sleep)."""
        self._session_closed = True
        task = getattr(self, "_watchdog_task", None)
        if task is not None and not task.done():
            task.cancel()
        self._watchdog_task = None
        # Z2: a supply-recovery ladder must not outlive the session either.
        rec = getattr(self, "_supply_recovery_task", None)
        if rec is not None and not rec.done():
            rec.cancel()
        self._supply_recovery_task = None

    async def _idle_watchdog(self) -> None:
        while not self.game_over and not getattr(self, "_session_closed", False):
            await asyncio.sleep(self.WATCHDOG_INTERVAL_SECONDS)
            try:
                if getattr(self, "_session_closed", False):
                    return
                if not self.game_started or self.game_over:
                    continue
                if getattr(self, "_delivery_stop_sticky", False):
                    # Conversation may continue, but every game-plane owner
                    # stays frozen until explicit resume.
                    continue
                # W2b item 1: the tick is an ORDERED walk of the policy table.
                await self._run_watch_policies()
            except Exception:
                logger.exception("LILY_WATCHDOG | TICK_FAILED")

    # -- watchdog policy table (W2b item 1) -----------------------------------
    #
    # The tick, past the pre-gates above, walks _WATCH_POLICIES in order. Each
    # row fires when its `when` holds; a row that returns _WATCH_HALT ends the
    # tick — the exact semantics of a `continue` in the pre-refactor nested
    # watchdog. ORDER IS PRIORITY: an earlier row that halts hides the ones
    # below it this tick. That priority is the whole point of the refactor —
    # session 2260354c stalled because supply recovery sat BELOW the armed /
    # question halts and never ran while a question was in hand.

    async def _run_watch_policies(self) -> None:
        table = getattr(self, "_watch_policy_table", None)
        if table is None:
            table = self._make_watch_policies()
            self._watch_policy_table = table
        self._watchdog_tick = getattr(self, "_watchdog_tick", 0) + 1
        ran: set = set()
        for policy in table:
            if self._watchdog_tick % policy.every_ticks:
                continue
            if ran.intersection(policy.skip_if):
                continue
            if not policy.when(self):
                continue
            action = await policy.run(self)
            ran.add(policy.name)
            if action == _WATCH_HALT:
                break

    def _make_watch_policies(self) -> tuple:
        """Ordered policy table in the OPERATOR'S stated priority (W2b item 1,
        commit B): hold-timeout -> supply-silent -> armed recovery
        (undelivered-refire / armed-limbo, the two branches of `armed`) ->
        address-unanswered -> question-reoffer. The unnamed rows are slotted
        by their current semantics: the progression-paused and busy gates sit
        ahead of the armed/idle recovery they gate (never re-arm a paused or
        busy game); idle-rearm and supply-stall stay together, after armed, as
        the idle branch.

        The deliberate change from the pre-refactor order (commit A): supply-
        silent now runs BEFORE the question-reoffer and armed/paused/busy
        HALTS, so a dead supply line is never hidden behind them (the
        2260354c class). Consequence, enumerated in the CHANGELOG: address-
        unanswered and question-reoffer, being lowest priority, are suppressed
        on a tick where an earlier halt (paused/busy/armed) claims it."""
        return (
            WatchPolicy("hold_timeout", LilyGame._when_hold, 1,
                        LilyGame._wp_hold),
            WatchPolicy("supply_silent", LilyGame._when_always, 1,
                        LilyGame._wp_supply_silent),
            WatchPolicy("progression_paused", LilyGame._when_progression_paused,
                        1, LilyGame._wp_progression_paused),
            WatchPolicy("busy_reset", LilyGame._when_busy, 1,
                        LilyGame._wp_busy_reset),
            WatchPolicy("armed", LilyGame._when_armed, 1, LilyGame._wp_armed),
            WatchPolicy("address_unanswered", LilyGame._when_always, 1,
                        LilyGame._wp_address_unanswered),
            WatchPolicy("question_reoffer", LilyGame._when_question_pending, 1,
                        LilyGame._wp_question_reoffer),
            WatchPolicy("idle_rearm", LilyGame._when_idle, 1,
                        LilyGame._wp_idle_rearm),
            WatchPolicy("supply_stall", LilyGame._when_idle, 1,
                        LilyGame._wp_supply_stall),
        )

    # -- policy predicates ----------------------------------------------------

    def _when_always(self) -> bool:
        return True

    def _when_hold(self) -> bool:
        return getattr(self, "_hold_active", False)

    def _when_question_pending(self) -> bool:
        return getattr(self, "_question_pending", False)

    def _when_progression_paused(self) -> bool:
        return bool(self.progression_paused_reason())

    def _when_busy(self) -> bool:
        return (
            self.sk.answer_window_open
            or self._adjudicating
            or getattr(self, "_question_transitioning", False)
        )

    def _when_armed(self) -> bool:
        return self.armed_question is not None

    def _when_idle(self) -> bool:
        return self.armed_question is None

    # -- policy actions (one per pre-refactor branch) -------------------------

    async def _wp_hold(self) -> str:
        # PATCH-002 A4: the hold binds every lane. While held, the watchdog
        # itself must not refire/nudge/vamp. It lifts only on the generous
        # timeout (user speech lifts it sooner) — then the tick continues.
        if self.hold_timed_out():
            self.release_hold(reason="timeout")
            return "hold_released"
        return _WATCH_HALT

    async def _wp_address_unanswered(self) -> str:
        # PATCH-003 P9: a direct address unanswered past the budget trips the
        # ADDRESS_UNANSWERED warn once.
        if self.address_unanswered() and not getattr(
            self, "_address_unanswered_warned", False
        ):
            self._address_unanswered_warned = True
            logger.warning(
                "LILY_RESPONSIVENESS | ADDRESS_UNANSWERED | "
                "session=%s — a direct address went unanswered past "
                "the %.1fs budget",
                self.sk.session_id,
                lily_config.responsiveness_budget_seconds(),
            )
        elif not getattr(self, "_awaiting_address_since", 0.0):
            self._address_unanswered_warned = False
        return "address_checked"

    async def _wp_question_reoffer(self) -> str:
        # PATCH-003 P10: a pending conversational question unanswered past the
        # timeout gets ONE gentle re-offer, then holds. Always halts the tick.
        if self._question_pending_timed_out():
            if not getattr(self, "_question_pending_reoffered", False):
                self._question_pending_reoffered = True
                self.gated_say(
                    None, "question_reoffer",
                    "The table hasn't answered the question you "
                    "asked. REGISTER GUIDANCE (vary freely within "
                    "this length and temperature, never longer): "
                    "gently re-ask the SAME question once, no new "
                    "content stacked on it, then wait.",
                    source="question_reoffer",
                )
            else:
                # Re-offer already spent — convert to a hold.
                self.release_question_pending(reason="reoffer_timeout")
                self.enter_hold(reason="question_unanswered")
        return _WATCH_HALT

    async def _wp_supply_silent(self) -> str:
        # Z2 (HOTFIX-008): supply health on its OWN tick, independent of
        # delivery state — session 2260354c proved a dead supply line is
        # invisible while a question is armed or a window is open. This check
        # extends recovery's REACH into the non-idle phases; it only ever
        # lands SUPPLY (next_question). Falls through (never halts).
        if self._supply_silent_window():
            self._supply_silent_ticks = (
                getattr(self, "_supply_silent_ticks", 0) + 1
            )
            idle_phase = (
                self.armed_question is None
                and not self.sk.answer_window_open
                and not self._adjudicating
                and not getattr(self, "_question_transitioning", False)
            )
            if not idle_phase:
                if (
                    self._supply_silent_ticks
                    == self.SUPPLY_SILENT_WARN_TICKS
                ):
                    logger.warning(
                        "LILY_SUPPLY | SUPPLY_SILENT_WINDOW | "
                        "session=%s q=%d ticks=%d (~%ds) armed=%s "
                        "window=%s — no prefetched question and no "
                        "prefetch in flight while delivery is busy; "
                        "starting supply recovery",
                        self.sk.session_id, self.sk.question_number,
                        self._supply_silent_ticks,
                        int(
                            self._supply_silent_ticks
                            * self.WATCHDOG_INTERVAL_SECONDS
                        ),
                        self.armed_question is not None,
                        self.sk.answer_window_open,
                    )
                if (
                    self._supply_silent_ticks
                    >= self.SUPPLY_SILENT_WARN_TICKS
                ):
                    self.ensure_supply_recovery(
                        trigger="watchdog_silent_window"
                    )
        else:
            self._supply_silent_ticks = 0
        return "supply_silent_checked"

    async def _wp_progression_paused(self) -> str:
        paused = self.progression_paused_reason()
        logger.info(
            "LILY_PROGRESSION | WATCHDOG_PAUSED | session=%s "
            "q=%d reason=%s",
            self.sk.session_id, self.sk.question_number, paused,
        )
        return _WATCH_HALT

    async def _wp_busy_reset(self) -> str:
        self._prefetch_stall_ticks = 0
        self._armed_limbo_ticks = 0
        self._supply_stall_ticks = 0
        return _WATCH_HALT

    async def _wp_armed(self) -> str:
        # Armed with a CLOSED window and no ruling in flight. If the delivery
        # claim is already CONFIRMED, the post-delivery chain died (2026-07-15
        # 04:05) — recover deterministically. Always halts.
        self._prefetch_stall_ticks = 0
        self._supply_stall_ticks = 0
        claim = self.say_registry.state(
            f"q_{self.sk.question_number}_delivery"
        )
        if claim == lily_say_gate.CLAIM_CONFIRMED:
            self._armed_limbo_ticks += 1
            if self._armed_limbo_ticks >= 2:
                self._armed_limbo_ticks = 0
                if self.sk.answer_candidates:
                    logger.error(
                        "LILY_WATCHDOG | ARMED_LIMBO | session=%s "
                        "q=%d — delivery confirmed, window closed, "
                        "candidates waiting: forcing adjudication",
                        self.sk.session_id, self.sk.question_number,
                    )
                    asyncio.ensure_future(
                        # Recovery may reclaim a journaled transition whose
                        # speech never aired — without this the
                        # SECOND_LANE_REFUSED guard and this watchdog
                        # ping-pong forever over dead air (lily-1C53C6).
                        self.adjudicate(
                            steal_allowed=False,
                            reclaim_transition=True,
                        )
                    )
                else:
                    logger.error(
                        "LILY_WATCHDOG | ARMED_LIMBO | session=%s "
                        "q=%d — delivery confirmed, window closed, "
                        "no candidates: reopening the window",
                        self.sk.session_id, self.sk.question_number,
                    )
                    self.open_window()
            self._undelivered_ticks = 0
            self._undelivered_refires = 0
        else:
            # WS-2: armed, CLOSED window, delivery claim NOT confirmed —
            # never registered, or registered and stuck. Reconcile explicitly.
            self._armed_limbo_ticks = 0
            self.reconcile_undelivered_claim()
        return _WATCH_HALT

    async def _wp_idle_rearm(self) -> str:
        # Game live but idle: nothing armed, no window, no ruling. Reset the
        # limbo counter, then arm the prefetched question if one is in hand
        # (halt); otherwise fall through to the supply-stall ladder.
        self._armed_limbo_ticks = 0
        if self.next_question is not None:
            self._supply_stall_ticks = 0
            if self.arm_next_question() and self.session is not None:
                logger.warning(
                    "LILY_WATCHDOG | IDLE_REARM | session=%s q=%d",
                    self.sk.session_id, self.sk.question_number,
                )
                # Structural delivery claim (desync WO Sub-agent B): the
                # nudged turn registers the delivery.
                self.expect_delivery()
                self.gated_say(
                    None,
                    "question_nudge",
                    (
                        "The next question just landed in the state "
                        "block. Bridge in one short beat and ask it "
                        "now — its question sentence exactly as "
                        "written, whole, in one breath."
                    ),
                    source="idle_watchdog",
                )
            return _WATCH_HALT
        return "idle_no_supply"

    async def _wp_supply_stall(self) -> str:
        # WS-6 supply-stall fallback. Nothing armed, nothing prefetched: count
        # the stall independently of _prefetch_stall_ticks. Past the fallback
        # window, arm straight from the curated bank — but only once
        # reconciliation reports no stuck claim (WS-2).
        self._supply_stall_ticks = (
            getattr(self, "_supply_stall_ticks", 0) + 1
        )
        logger.warning(
            "LILY_WATCHDOG | SUPPLY_STALL | session=%s q=%d "
            "ticks=%d (~%ds) — no question in hand",
            self.sk.session_id, self.sk.question_number,
            self._supply_stall_ticks,
            int(self._supply_stall_ticks * self.WATCHDOG_INTERVAL_SECONDS),
        )
        if (
            self._supply_stall_ticks >= self._supply_fallback_ticks()
            and self.no_stuck_claims()
        ):
            if await self.arm_supply_fallback() == "armed":
                self._supply_stall_ticks = 0
                return _WATCH_HALT
            # empty / blocked / error: fall through so the normal
            # re-prefetch keeps trying and the honest vamp holds.
        task = self._prefetch_task
        if task is None or task.done():
            self._prefetch_stall_ticks = 0
            logger.warning(
                "LILY_WATCHDOG | IDLE_REPREFETCH | session=%s q=%d",
                self.sk.session_id, self.sk.question_number,
            )
            self.start_prefetch()
            return _WATCH_HALT
        # A prefetch task is alive but the game has been idle for this long —
        # it outlived every internal timeout, so treat it as hung.
        self._prefetch_stall_ticks += 1
        if self._prefetch_stall_ticks >= self.PREFETCH_HARD_TIMEOUT_TICKS:
            self._prefetch_stall_ticks = 0
            logger.error(
                "LILY_WATCHDOG | PREFETCH_HARD_TIMEOUT | session=%s "
                "q=%d — cancelling the stuck supply task",
                self.sk.session_id, self.sk.question_number,
            )
            task.cancel()
            self.sk.set_status_note(
                "the question machine stalled and was restarted — "
                "vamp honestly for a beat; the next question is on "
                "its way"
            )
        return "supply_stall_checked"


    # -- registered-undelivered reconciliation (WS-2, WO-LILY-OMNIBUS-003) -----
    #
    # WS-0 covers the FAILED generation (GENERATION_FAILED logs and the
    # suppressed path releases the claim). WS-2 covers the remaining stuck
    # class: a question armed and registered in asked_history whose delivery
    # never reaches playout — the claim stays PENDING (dispatched, no
    # completion, no exception) or was never registered at all — with no
    # ruling in flight. The 583a0f16 session held the round-2 loop for five
    # minutes on q_0001 (Jupiter) and q_2943 (Lisa), both in asked_history,
    # neither ever aired. This hangs off WS-1's single claim choke point
    # (`register_delivery_claim` / the q_{N}_delivery key), not a parallel
    # mechanism: re-fire the delivery, and after repeated re-fires release
    # the question back to supply so a fresh one can arm.

    def _undelivered_reconcile_ticks(self) -> int:
        """Watchdog ticks a delivery may stay armed-but-unconfirmed before
        it is treated as stuck (config seconds / the tick interval, floor
        one tick). Matches the ARMED_LIMBO two-tick idiom at the 20s
        default."""
        seconds = lily_config.undelivered_reconcile_seconds()
        return max(1, round(seconds / self.WATCHDOG_INTERVAL_SECONDS))

    def _delivery_confirmed(self) -> bool:
        key = f"q_{self.sk.question_number}_delivery"
        return self.say_registry.state(key) == lily_say_gate.CLAIM_CONFIRMED

    def _stuck_delivery_present(self) -> bool:
        """True iff the armed question's delivery has been re-fired at least
        once and STILL has not aired (WS-2's stuck class), measured against
        CURRENT state.

        The persistent signal is `_undelivered_refires`, NOT
        `_undelivered_ticks`: the tick counter resets to 0 inside the same
        synchronous reconcile call that crosses the threshold (before it
        re-fires), so a caller reading between watchdog ticks never catches
        it at/above threshold — the stuck window would have zero observable
        duration. `_undelivered_refires` is raised at the first re-fire and
        clears only when the delivery confirms (open_window), the question
        is released (_release_armed_question_to_supply), or a new question
        arms — so it holds False across the WHOLE active re-fire cycle,
        which is what WS-6 reads to keep its supply fallback off a ghost.
        A freshly-armed or normally in-flight delivery (no re-fire yet) is
        NOT stuck."""
        if not getattr(self, "game_started", False) or getattr(
            self, "game_over", False
        ):
            return False
        if self.armed_question is None or self.sk.answer_window_open:
            return False
        if self._adjudicating or getattr(self, "_question_transitioning", False):
            return False
        if self._delivery_confirmed():
            return False
        return getattr(self, "_undelivered_refires", 0) > 0

    def no_stuck_claims(self) -> bool:
        """WS-6 gate: True when reconciliation reports no registered-
        undelivered claim stuck past the reconcile window. WS-6's supply
        fallback arms only while this holds — a genuinely stuck delivery
        (armed, unconfirmed, past the reconcile window) blocks it until the
        watchdog re-fires or releases it; a normal in-flight delivery does
        not."""
        return not self._stuck_delivery_present()

    def force_confirm_delivery_heard(
        self, *, reason: str, ratio: float | None = None
    ) -> bool:
        """One-owner near-miss: the table already heard Q; do not re-read.

        Claims + confirms `q_N_delivery` if needed, then opens the answer
        window. Used when spoken/prompt ratio ≥ 0.9 but the structural
        claim missed — Gun 3 (NUDGE_NEAR_MISS) and Gun 1 false undelivered.
        Returns True when the window path was taken.
        """
        if self.armed_question is None or self.sk.answer_window_open:
            return False
        if self._adjudicating or getattr(self, "_question_transitioning", False):
            return False
        qnum = self.sk.question_number
        if self.question_already_answered(qnum):
            return False
        key = f"q_{qnum}_delivery"
        state = self.say_registry.state(key)
        if state is None:
            self.say_registry.claim(key, owner=f"near_miss:{reason}")
            state = self.say_registry.state(key)
        if state == lily_say_gate.CLAIM_PENDING:
            self.say_registry.confirm(key)
        elif state != lily_say_gate.CLAIM_CONFIRMED:
            return False
        self._armed_speech_misses = 0
        self._undelivered_ticks = 0
        self._undelivered_refires = 0
        logger.warning(
            "LILY_WINDOW | NEAR_MISS_CONFIRM | session=%s q=%d reason=%s "
            "ratio=%s — delivery confirmed without re-read; opening window",
            self.sk.session_id, qnum, reason,
            f"{ratio:.2f}" if ratio is not None else "?",
        )
        self.open_window_after_discharge()
        return True

    def reconcile_undelivered_claim(self) -> str:
        """One watchdog tick's reconciliation of the armed question's
        delivery (called from _idle_watchdog when armed with a closed
        window and an UNCONFIRMED delivery claim; safe to call standalone —
        it re-guards every precondition). Returns:
          "idle"     — not stuck yet (pre-game, no armed question, window
                       open, ruling in flight, delivery already confirmed,
                       or the reconcile window has not elapsed);
          "refired"  — the stuck delivery was re-dispatched (a stale
                       PENDING claim released first so the re-ask is not
                       suppressed as a duplicate);
          "released" — re-fires exhausted; the question was deregistered
                       from asked_history and dropped so a fresh one arms.
        """
        if not getattr(self, "game_started", False) or getattr(
            self, "game_over", False
        ):
            return "idle"
        if self.armed_question is None or self.sk.answer_window_open:
            self._undelivered_ticks = 0
            return "idle"
        if self._adjudicating or getattr(self, "_question_transitioning", False):
            self._undelivered_ticks = 0
            return "idle"
        if self._delivery_confirmed():
            self._undelivered_ticks = 0
            self._undelivered_refires = 0
            return "idle"
        # T2 (PATCH-001, RETIRE_WITH_WS6): an ANSWERED question never
        # re-airs. Once answer candidates exist (answer_heard) or its
        # verdict committed, every outstanding delivery attempt for it is
        # invalid — the live triples (Saturn re-read 4s after the answer,
        # Mitochondria 2s after the correct verdict) were this watchdog
        # re-airing a question the table had already answered.
        qnum = self.sk.question_number
        key = f"q_{qnum}_delivery"
        if self.question_already_answered(qnum):
            self._undelivered_ticks = 0
            self._undelivered_refires = 0
            if self.say_registry.release(key):
                logger.warning(
                    "LILY_WATCHDOG | ANSWERED_NO_REAIR | session=%s q=%d — "
                    "answer heard/verdict committed; outstanding delivery "
                    "attempt invalidated, not re-aired",
                    self.sk.session_id, qnum,
                )
            return "idle"
        # T1 (PATCH-001, RETIRE_WITH_WS6 — leases): re-verify "never
        # aired" against playout truth before ANY re-dispatch. A PENDING
        # claim whose speech started airing (playout-started ledger) or
        # while agent audio is live (host_speaking) is mid-flight, not
        # stuck — the live "Nobody…"/"Hey…" triples were false refires on
        # turns that actually played.
        owner = self.say_registry.owner_of(key)
        if owner and owner in getattr(self, "_playout_started_ids", set()):
            self._undelivered_ticks = 0
            return "idle"
        if getattr(self.sk, "host_speaking", False):
            self._undelivered_ticks = 0
            return "idle"
        # Do not re-air into active table talk. An interrupted delivery
        # releases its claim; if the room is still mid-side-chatter the
        # watchdog used to re-dispatch immediately and stack another copy
        # of the same question on top of the banter (RM_qs6 /
        # RM_VYp6 undelivered-refire loops). Hold until the room goes
        # quiet; ticks keep accumulating so a truly silent stall still
        # recovers once the quiet window elapses.
        last_user = getattr(self, "_last_user_turn_at", None)
        if last_user is not None:
            quiet_for = time.monotonic() - last_user
            if quiet_for < lily_config.undelivered_refire_quiet_seconds():
                return "idle"
        # Armed, window closed, delivery unconfirmed (never registered or
        # stuck PENDING): count consecutive stuck ticks.
        self._undelivered_ticks = getattr(self, "_undelivered_ticks", 0) + 1
        if self._undelivered_ticks < self._undelivered_reconcile_ticks():
            return "idle"
        self._undelivered_ticks = 0
        # Near-miss: table already heard a high-similarity performance —
        # confirm + open, never UNDELIVERED_REFIRE the same Q again.
        last_ratio = float(getattr(self, "_last_armed_speech_ratio", 0.0) or 0.0)
        if last_ratio >= 0.9:
            logger.warning(
                "LILY_WATCHDOG | UNDELIVERED_NEAR_MISS | session=%s q=%d "
                "ratio=%.2f — confirming delivery without re-air",
                self.sk.session_id, qnum, last_ratio,
            )
            self.force_confirm_delivery_heard(
                reason="undelivered_near_miss", ratio=last_ratio,
            )
            return "confirmed"
        if getattr(self, "_undelivered_refires", 0) < UNDELIVERED_MAX_REFIRES:
            self._undelivered_refires += 1
            # Drop a stale never-played claim so the re-dispatched delivery
            # re-claims cleanly (a confirmed claim can never reach here, so
            # this only ever releases a stuck PENDING one) — and CANCEL its
            # speech handle so a late start cannot double-air after the
            # release (T1: the starts-after-release hole).
            self.say_registry.release(key)
            self.cancel_speech(owner, reason="undelivered_refire")
            logger.error(
                "LILY_WATCHDOG | UNDELIVERED_REFIRE | session=%s q=%d "
                "attempt=%d — delivery registered but never aired; "
                "re-dispatching",
                self.sk.session_id, qnum, self._undelivered_refires,
            )
            self.expect_delivery()
            self.gated_say(
                None,
                "question_nudge",
                (
                    "The armed question has not reached the table yet. "
                    "Deliver it now: one short bridge if you need it, then "
                    "its question sentence exactly as written, whole, in "
                    "one breath — then stop and let the table answer."
                ),
                source="undelivered_reconcile",
            )
            return "refired"
        # Re-fires exhausted — release the question back to supply.
        self.say_registry.release(key)
        self._release_armed_question_to_supply()
        logger.error(
            "LILY_WATCHDOG | UNDELIVERED_RELEASE | session=%s q=%d — "
            "delivery never aired after %d re-fires; deregistering from "
            "asked_history and returning to supply",
            self.sk.session_id, qnum, UNDELIVERED_MAX_REFIRES,
        )
        return "released"

    # -- PATCH-001 T1/T2 helpers (RETIRE_WITH_WS6: the journal reducer's
    # leases + event-sourced question state replace all of this) ----------

    def question_already_answered(self, qnum: int) -> bool:
        """T2: True when question `qnum` has been ANSWERED — candidates
        heard for the current question, or its verdict/adjudication
        already ran (the answered-set survives the question transition,
        so a stale refire for N after the game moved to N+1 is caught
        too). An answered question never re-airs."""
        answered = getattr(self, "_answered_questions", None) or set()
        if qnum in answered:
            return True
        if qnum == self.sk.question_number and self.sk.answer_candidates:
            return True
        return False

    def question_is_terminal(self, qnum: int) -> bool:
        """True once adjudication owns the question.

        Unlike question_already_answered(), this deliberately ignores a
        merely-recorded candidate: clarify is still legal while the window
        is open. note_answer_heard() is the terminal boundary.
        """
        answered = getattr(self, "_answered_questions", None) or set()
        return qnum in answered

    def answered_closed_state_line(self) -> str | None:
        qnum = self.sk.question_number
        if not self.question_is_terminal(qnum):
            return None
        return (
            f"answered_closed: q{qnum} DONE — the result/reveal owns this "
            "question. Do not clarify an older fragment, ask for a final "
            "answer, reopen its window, or re-ask it. Move to the next "
            "question or yield."
        )


    def note_answer_heard(self, qnum: int) -> None:
        """T2 marking point: adjudication is starting (answer_heard) —
        every outstanding delivery attempt for this question is now
        invalid, in-flight playout included."""
        answered = getattr(self, "_answered_questions", None)
        if answered is None:
            answered = self._answered_questions = set()
        answered.add(qnum)
        self.clear_pending_clarify_for_question(
            qnum, reason="answer_heard"
        )
        self.invalidate_deliveries_for(qnum)

    def invalidate_deliveries_for(self, qnum: int) -> None:
        """Release a pending (unconfirmed) delivery claim for `qnum` and
        cancel its speech mid-playout — the fixture class where the
        question re-read aired seconds AFTER the correct answer."""
        key = f"q_{qnum}_delivery"
        if self.say_registry.state(key) != lily_say_gate.CLAIM_PENDING:
            return
        owner = self.say_registry.owner_of(key)
        self.say_registry.release(key)
        self.cancel_speech(owner, reason="question_answered")
        logger.warning(
            "LILY_DELIVERY | INVALIDATED | session=%s q=%d reason=answered "
            "— outstanding delivery attempt cancelled",
            self.sk.session_id, qnum,
        )

    def cancel_speech(self, speech_id: str | None, reason: str) -> None:
        """T1: cancel/invalidate one dispatched speech so a late start can
        never air after its claim was released. Marks it suppressed (the
        playout watcher routes it to the not-recorded path) and interrupts
        the live handle when we hold one. Safe on unknown ids."""
        if not speech_id:
            return
        suppressed = getattr(self, "_suppressed_speech_ids", None)
        if suppressed is None:
            suppressed = self._suppressed_speech_ids = set()
        suppressed.add(speech_id)
        handles = getattr(self, "_speech_handles", None) or {}
        handle = handles.get(speech_id)
        if handle is None:
            return
        try:
            handle.interrupt(force=True)
            logger.warning(
                "LILY_SPEECH | CANCELLED | speech_id=%s reason=%s",
                speech_id, reason,
            )
        except Exception as e:
            logger.warning(
                "LILY_SPEECH | CANCEL_FAILED | speech_id=%s reason=%s "
                "error=%s", speech_id, reason, e,
            )

    # -- PATCH-002 A4/A5: hold state + STOP primitive (RETIRE_WITH_WS6) ----

    # Sources allowed to speak WHILE HELD: the STOP/hold acknowledgment
    # itself and the deterministic mode-revert/hold-release beats. Question
    # deliveries, nudges, verdicts, reveals, and free conversation are all
    # blocked until the hold releases.
    _HOLD_EXEMPT_SOURCES = frozenset({
        "stop_primitive", "hold_ack", "hold_release",
    })

    # PATCH-003 P8: game-lane acts — deliveries, verdicts, reveals, the
    # steal/lockout opener, timers, scoreboards. None may air without a
    # live game (game_started and not game_over). The steal window
    # additionally requires an open answer window (a lockout with no
    # question is the exact "Nobody landed it" lobby fixture).
    _GAME_LANE_ACTS = frozenset({
        "question_delivery", "question_nudge", "verdict", "reveal",
        "reveal_flourish", "reveal_scores", "reveal_finale", "steal_window",
        # HOSTLOOP-001 C6: the instant answer receipt is a game-lane payload
        # like the verdict it precedes — it may not air into a lobby, a
        # finished game, or a STOP, and (like every other game act) its own
        # window governs it rather than the conversational question-pending
        # yield.
        "answer_receipt",
    })


    FLOOR_LILY_SPEAKING = "lily_speaking"
    FLOOR_PLAYER_SPEAKING = "player_speaking"
    FLOOR_OPEN = "open_floor"
    FLOOR_HOLD = "hold"



















    def back_hold_narration(self, spoken_text: str) -> bool:
        """W8 honest-narration integrity: a turn that narrates a stopped/
        hold state ("Stopped. I'm listening.", "Still stopped until you say
        go.") must be backed by an actual hold. If none is active, enter it
        so the spoken claim is true — a correction, never a silent
        confabulation of a state that does not exist. No-op when a hold is
        already active (the claim is already backed) or the game is under
        the sticky STOP latch (the stopped claim is backed by that), or the
        turn makes no such claim. Returns True when a hold was entered to
        back the narration. Sets _hold_active only; this in-flight turn
        already passed the gated_say gate, so backing the claim never
        suppresses the ack itself — it binds the NEXT dispatch (with W2's
        gate, the leaked delivery)."""
        if getattr(self, "_hold_active", False):
            return False
        if self.game_delivery_stopped():
            return False
        if not lily_scorekeeper.lily_detect_hold_narration(spoken_text or ""):
            return False
        self.enter_hold(reason="narrated_stop")
        logger.warning(
            "LILY_HOLD | NARRATION_BACKED | session=%s text=%r — outbound "
            "narrated a stopped state with no hold active; entering the hold "
            "so the claim is backed by state (W8)",
            self.sk.session_id, (spoken_text or "")[:80],
        )
        return True


    def mark_stem_aired(self, qnum: int) -> None:
        """A delivery stem for question `qnum` reached the air (its
        delivery playout started). Recorded so an abandonment before the
        window opens can be flagged as a cancelled stem rather than a
        silent vanish."""
        aired = getattr(self, "_aired_stems", None)
        if aired is None:
            aired = self._aired_stems = set()
        aired.add(qnum)

    def mark_stem_completed(self, qnum: int) -> None:
        """The window opened — the stem completed its promise. Terminal;
        clears the aired marker so it can't later read as abandoned."""
        (getattr(self, "_aired_stems", None) or set()).discard(qnum)

    def _terminate_aired_stem(self, reason: str) -> None:
        """If the CURRENT question's stem aired but never completed
        (window never opened), emit a cancellation event so the dispatch
        record terminates. Idempotent — the marker clears here."""
        qnum = self.sk.question_number
        aired = getattr(self, "_aired_stems", None) or set()
        if qnum not in aired:
            return
        if self.sk.answer_window_open:
            # It did complete — the window is open; not an orphan.
            aired.discard(qnum)
            return
        aired.discard(qnum)
        logger.warning(
            "LILY_STEM | CANCELLED | session=%s q=%d reason=%s — aired stem "
            "closed off without completing (no orphan)",
            self.sk.session_id, qnum, reason,
        )
        self.send_event_nowait("stem_cancelled", {
            "question_number": qnum, "reason": reason,
        })

    def note_speech_handle(self, handle) -> None:
        """Track live SpeechHandles by id (bounded) so cancel_speech can
        reach them. Wired from the speech_created watcher."""
        speech_id = getattr(handle, "id", None)
        if not speech_id:
            return
        handles = getattr(self, "_speech_handles", None)
        if handles is None:
            handles = self._speech_handles = {}
        handles[speech_id] = handle
        while len(handles) > 16:
            handles.pop(next(iter(handles)))

    def air_dup_guard(self, full: str, delivery: str | None) -> bool:
        """T3 air-path guard: True = this outbound turn is a verbatim
        repeat of a RECENTLY-PLAYED turn (last 6, interleaving ignored)
        and must be made silent. Delivery turns are exempt (a re-read of
        the sheet is deliberate); short turns are exempt (an honest "Nice
        one!" may legitimately recur). HOTFIX-002's guard only compared
        the immediately-preceding turn; every live duplicate pair had a
        user row interleaved."""
        if delivery is not None or len(full or "") < 15:
            return False
        recent = (getattr(self.sk, "agent_turns", None) or [])[-6:]
        return full in recent









    def flush_for_mode_switch(self, source: str) -> None:
        """Sticky mode just flipped (EITHER direction — adult entry,
        spoken "back to normal", child-signal veto, breaker trip): the
        armed and prefetched questions were drawn from the OLD deck, so
        they are dead. Flush them, cancel the in-flight draw, and start a
        fresh prefetch from the new deck immediately — the prefetch
        auto-advance re-arms and nudges when it lands, and the idle
        watchdog backstops. Flushed questions STAY in the drawn-set
        (_register_draw): a question that touched the wrong segment is
        never re-served this session. The state block is honest about the
        one-beat gap via a status note the next successful draw clears.

        Call AFTER sk.set_mode(...) — the flush reads the NEW mode."""
        # PATCH-002 M4: an aired stem is a promise. If the question being
        # flushed already put its stem on the air but never opened a
        # window (completed), that is an abandoned stem — terminate its
        # dispatch record with a cancellation event so it never vanishes
        # silently (the "name three of the—" orphan).
        self._terminate_aired_stem(reason=f"mode_flush:{source}")
        mode = self.sk.mode
        logger.info(
            "LILY_STATE | MODE_FLUSH | session=%s mode_now=%s source=%s "
            "flushed_armed=%s flushed_prefetched=%s",
            self.sk.session_id, mode, source,
            (self.armed_question or {}).get("id"),
            (self.next_question or {}).get("id"),
        )
        if self._window_timer and not self._window_timer.done():
            self._window_timer.cancel()
        self.sk.close_answer_window()
        self._stop_bed()
        self._steal_window = False
        self.armed_question = None
        self.next_question = None
        self.sk.current_question = None
        self._armed_speech_misses = 0
        # Cancel the in-flight draw: it is pulling from the wrong deck.
        # Clearing the handle lets start_prefetch() relaunch NOW instead
        # of waiting for the cancellation to land; if the stale task is
        # already past its last await, its own commit guard (supply_mode
        # check in _prefetch_inner) discards whatever it returns. Reset
        # the stall counter so the idle watchdog cooperates with the
        # fresh task instead of racing the cancelled one.
        task = self._prefetch_task
        if task is not None and not task.done():
            task.cancel()
        self._prefetch_task = None
        self._prefetch_stall_ticks = 0
        if self.game_started and not self.game_over:
            deck = "adult" if mode == "adult" else "general"
            self.sk.set_status_note(
                f"deck switch committed: the next question is being drawn "
                f"from the {deck} deck and lands in the state block in a "
                "beat. Vamp honestly until it does — never re-ask, finish, "
                "or reveal anything from the previous deck, and never "
                "invent a question"
            )
            self._set_ui_phase("question")
            # Clear the old deck's question off the glass — screen truth
            # must match the switch the room just heard.
            asyncio.ensure_future(self.publish_metadata(""))
        self.publish_attributes_nowait()
        if not self.game_over:
            self.start_prefetch()

    def _curate_generated_question(
        self,
        question: dict,
        family: str,
        history_hashes: set,
    ) -> dict | None:
        """Bank-curation gate for one VERIFIED generated question
        (WO-LILY-OMNIBUS-002 D/F). Returns the question ready to serve,
        or None when the group has already heard it (asked-history hash
        collision — the generator's textual avoid-list only carries this
        session's prompts, so cross-session repeats are caught here).

        Category proposals (F): a `proposed_category` is tallied in
        lily_category_candidates, but the question SERVES under its round
        family until the proposal is promoted (>=10 uses across >=3
        distinct groups). Lily never announces unpromoted categories.

        Banking (D): the surviving question is inserted into
        lily_questions (source='generated') with near-dup detection at
        insert — this is where the bank self-grows."""
        text_hash = lily_bank.lily_question_text_hash(question.get("prompt"))
        if text_hash in history_hashes:
            logger.info(
                "LILY_BANK | HISTORY_REPEAT_DISCARDED | group=%s hash=%s "
                "prompt=%r",
                self.group_id, text_hash[:12],
                str(question.get("prompt", ""))[:80],
            )
            return None
        # Answer-level no-repeat (live 2026-07-15 20:42, "the gold
        # question every time": regeneration rewrites the prompt — fresh
        # hash — but the ANSWER is the identity of the fact. A generated
        # question whose answer this group has already played is a repeat,
        # however it's worded.)
        answer_norm = lily_evaluation.lily_normalize_answer(
            str(question.get("canonical_answer", ""))
        )
        if answer_norm and answer_norm in lily_bank.lily_history_answers(
            self.asked_history
        ):
            logger.info(
                "LILY_BANK | ANSWER_REPEAT_DISCARDED | group=%s answer=%r "
                "prompt=%r",
                self.group_id, answer_norm[:40],
                str(question.get("prompt", ""))[:80],
            )
            return None
        proposed = lily_bank.lily_normalize_category_name(
            question.get("proposed_category")
        )
        if proposed:
            if self.supabase is not None:
                asyncio.ensure_future(lily_bank.lily_record_category_proposal(
                    self.supabase, proposed, family, self.group_id,
                ))
            # HOTFIX-006 N2: a topic the TABLE named outranks a category the
            # generator proposed, promoted or not. Letting a promoted
            # proposal relabel the question would re-open the defect from the
            # other side — a question built for the Cape Cod round arriving
            # labelled something else, failing its registration check, and
            # turning a round she really did build into a refusal.
            if not self._is_operator_category(family):
                question["category"] = (
                    proposed if proposed in self.promoted_categories else family
                )
        if self.supabase is not None:
            asyncio.ensure_future(lily_bank.lily_bank_generated_question(
                self.supabase, dict(question), self.sk.mode,
            ))
        return question



    def restore_reconnected_state(self) -> None:
        """Restore the unresolved checkpoint question without incrementing.

        ``arm_next_question`` cannot be used here because it calls
        ``start_question`` and advances the question number. A reconnect
        instead replays the checkpointed question into a fresh answer
        window, while the normal supply task prepares N+1.
        """
        self.game_started = True
        question = self.sk.current_question
        if question is not None and self._is_burned(question):
            # WS-4: a reconnect must never resurrect a question whose
            # answer already went to air. Drop it; the supply path serves
            # a fresh one.
            logger.info(
                "LILY_BURN | RECONNECT_SKIP | session=%s question_id=%s — "
                "burned question not restored",
                self.sk.session_id, question.get("id"),
            )
            self.armed_question = None
            self.sk.current_question = None
            self.ui_phase = "question"
            question = None
        if question is not None and self.sk.phase != "wrapup":
            self.armed_question = dict(question)
            self._register_draw(self.armed_question)
            prompt = self.armed_question.get("prompt", "")
            if prompt and prompt not in self.used_prompts:
                self.used_prompts.append(prompt)
            self._armed_speech_misses = 0
            self._judged_keys = set()
            self._spec_judge = {}
            self._nbest_by_key = {}
            self.ui_phase = "question"
            logger.info(
                "LILY_STATE | RECONNECT_RESTORED | session=%s q=%d id=%s",
                self.sk.session_id,
                self.sk.question_number,
                self.armed_question.get("id"),
            )
        else:
            self.armed_question = None
            if self.sk.phase == "wrapup":
                self.game_over = True
                self.ui_phase = "final"
            else:
                self.ui_phase = "question"

    # -- answer window ---------------------------------------------------------

    def on_agent_speech_finished(
        self,
        spoken_text: str,
        *,
        speech_id: str | None = None,
        interrupted: bool = False,
        suppressed: bool = False,
        failed: bool = False,
    ) -> None:
        """Called on TTS playback completion (agent stops speaking). If the
        armed question's delivery is REGISTERED (the q_{N}_delivery claim —
        structural, never text-similarity), the answer window opens HERE —
        never earlier. (The old "no early buzz-ins" v1 concession is
        retired: finals spoken during the delivery playout buffer via
        buffer_pre_window_answer and replay at open — fixture Q5.)"""
        delivery_key = f"q_{self.sk.question_number}_delivery"
        if (
            speech_id
            and self.say_registry.owner_of(delivery_key) == speech_id
            and getattr(self, "_active_delivery_started_at", None) is not None
        ):
            # Close the exact audible interval before any discharge delay.
            # Finals may arrive after this callback, but only a segment whose
            # captured timestamps overlap [started, ended] is an early answer.
            self._active_delivery_ended_at = time.time()
        # A deterministic instruction speech finished playing out — its
        # items are committed, the persistent context is stable again, so
        # preemptive generation can resume (P2).
        self._resume_preemptive()
        # FL-1 adjacency anchor: a Lily turn that actually played (fully
        # or partially) biases the next utterance host-directed and
        # supersedes any live side-cluster. A suppressed turn never hit
        # the air, so it moves no floor state.
        classifier = getattr(self, "addressee_classifier", None)
        if classifier is not None and not suppressed:
            classifier.note_agent_prompt(time.time())
        # Say gate: playout completed — pending speech-act claims are now
        # genuinely spoken. Confirmed acts never release (a confirmed act
        # can never be redelivered); only a claimed-but-never-played act
        # releases, on the tts_node playback-failure path.
        # Stale-claim recovery bookkeeping: this speech's playout lifecycle
        # is over either way — its airing marker and handle are spent.
        if speech_id:
            getattr(self, "_playout_started_ids", set()).discard(speech_id)
            (getattr(self, "_speech_handles", None) or {}).pop(speech_id, None)
            (getattr(self, "_delivery_speech_acts", None) or {}).pop(
                speech_id, None
            )
        # HOSTLOOP-001 C5: this turn's playout is over however it ended, so
        # the host-composite lane is free. Released BEFORE the recovery
        # branches below, which dispatch composites of their own (the C8
        # verdict re-air is itself act="verdict" and would otherwise be
        # refused as racing the very flight that just died).
        self.clear_composite_flight(speech_id)
        spoken_text = self.consume_post_tts_text(speech_id, spoken_text)
        if interrupted or suppressed:
            released = (
                self.say_registry.release_owner(speech_id)
                if speech_id
                else []
            )
            if released:
                logger.warning(
                    "LILY_SAY | RELEASED | keys=%s | reason=%s",
                    ",".join(sorted(released)),
                    "interrupted" if interrupted else "suppressed",
                )
                delivery_key = f"q_{self.sk.question_number}_delivery"
                if delivery_key in released:
                    # THE LOOP THIS BREAKS. A cut delivery released its
                    # claim and re-armed for another full read of the whole
                    # sheet, which the next sentence cut again. Live
                    # 2026-08-08 `lily-2C489B`: three identical re-reads
                    # (22:49:37 / 22:49:47 / 22:50:05), each dying on the
                    # same word, window never opened, question never asked.
                    #
                    # Silence used to be treated as worse than asking
                    # twice. In a game where the table talks over the host
                    # by design, that trade is inverted: re-reading is what
                    # produces the mess. Once the question sentence has
                    # actually reached the room, the table HAS it — confirm
                    # and open the window. Re-arming is only for a delivery
                    # that never got the question out.
                    # `interrupted` ONLY. A suppressed turn produced no
                    # audio at all (the #3418 Lisa-ghost signature: claim
                    # registered at dispatch, nothing ever aired), so it
                    # can never have put the question in the room —
                    # confirming it there would re-open the ghost-window
                    # hole this codebase already closed.
                    if interrupted and self.delivery_reached_the_table(
                        spoken_text
                    ):
                        pass  # table has the question; window already open
                    else:
                        self.expect_delivery()
            # Y7 (HOTFIX-007): decide the cut CAUSE once, here, and let both
            # arms below read that one decision. A DELIBERATE barge-in (the
            # VAD layer saw a human take the floor) is a CANCEL: she stops,
            # yields, and nothing brings the killed line back. Recovery
            # machinery is for a turn the ROOM did not end — a dead TTS
            # stream, a mid-air network death — which is `failed`, or an
            # `interrupted` with no human voice behind it.
            barge_in = (
                interrupted and not failed
                and self.cut_was_deliberate_barge_in()
            )
            # Regeneration gate (WS-3): the act just cut/suppressed will
            # re-dispatch — arm the gate so the retry is spoken fresh, not
            # replayed. An interrupted turn partially aired (a re-air is
            # coming); a suppressed turn only re-airs if a claim was freed.
            # Y7: NOT on a barge-in. The arm is Chain A's entry point — it
            # tells the next dispatch to say this again in fresh words, which
            # is precisely how a line the player killed comes back rephrased
            # ("say it again, differently" also defeats the exact-match dup
            # guards that would otherwise catch the copy: GUARD_MAP §7).
            if (interrupted or released) and not barge_in:
                self.arm_reair_gate()
            # HOSTLOOP-001 C3c: THE CARVE-OUT, and its whole scope.
            #
            # Y7's policy above is unchanged and stays correct for every
            # non-question phase: a line the room talked over is cancelled,
            # not recovered. But a question read is an obligation, not a
            # line — and Session B (lily-05BB92) is what cancelling one
            # costs: barge lands mid-choices, floor yielded, no resume, the
            # armed choices never air, the utterance never binds, and the
            # question sits half-delivered forever. So a barge that kills a
            # question delivery whose window never opened is MARKED here
            # (reading Y7's own `barge_in` decision, not a second one), and
            # the question is owed either a binding or a resume.
            # The window may already be open here (C3a arms it at core
            # completion), which is exactly why "window open" is not a guard:
            # what is owed is the REST OF THE CHOICES, not a window.
            #
            # HOSTLOOP-001 C8 widens the MARK, not the policy. C3c marked
            # only an in-flight MC options read; the clause is "any host
            # utterance in phase=question". The predicate that replaces the
            # MC-only test is exactly "the room does not have this question
            # yet": phase=question, a question armed, and its delivery not
            # CONFIRMED. That covers the freeform read, a nudge, and any
            # other phase=question beat cut before the question landed —
            # and it excludes the case that must never re-read, a question
            # already confirmed delivered (delivery_reached_the_table above
            # force-confirms exactly that case, so it self-excludes).
            # mcq_barge_resume resumes from the interrupted choice where a
            # resume point exists and re-offers the whole sheet where it
            # does not.
            if barge_in and interrupted and self._question_owed_recovery(
                released
            ):
                self.note_question_barge_cut(self.sk.question_number)
            # HOSTLOOP-001 C8: a VERDICT beat cut by a barge-in re-airs the
            # result as one deterministic line. Session B 36:25: the cut
            # released q_{N}_reveal and Y7 (correctly, for conversation)
            # recovered nothing — so the ruling was dropped silently AND
            # the released key wedged N+1 behind an unconfirmable verdict.
            # Y7's cancel-not-recover stays the policy outside
            # phase=question / the verdict result.
            if barge_in and interrupted:
                self.reair_cut_verdict(released)
            # Cut-recovery contract (WS-3, STREAM-INTEGRITY-002): a cut or
            # mid-stream-FAILED organic turn (no keyed claim to drive a
            # game-loop re-dispatch) arms the auto-resume watchdog. It fires
            # only if the cut leaves dead air with no follow-up — a real
            # barge-in carrying a user turn is answered by the normal path
            # and the watchdog stands down (one-emission preserved).
            if self._cut_recovery_should_arm(released, interrupted, failed):
                self.arm_cut_recovery(spoken_text)
            # Task 0 (RECOGNITION-VARIETY): an INTERRUPTED turn partially
            # played — it belongs in the record, marked. A suppressed turn
            # never reached air and is not recorded as said.
            if interrupted:
                self.publish_agent_transcription_nowait(
                    spoken_text,
                    speech_id=speech_id,
                    interrupted=True,
                )
                self.record_agent_turn(
                    spoken_text, act_keys=[], interrupted=True
                )
            return
        confirmed = (
            self.say_registry.confirm_owner(speech_id)
            if speech_id
            else self.say_registry.confirm_pending()
        )
        if confirmed:
            logger.info(
                "LILY_SAY | CONFIRMED | keys=%s", ",".join(sorted(confirmed))
            )
            # A confirmed act played — its stale-retry budget is spent
            # state, never carried into a later same-key claim cycle.
            retry_counts = getattr(self, "_stale_retry_counts", {})
            for k in confirmed:
                retry_counts.pop(k, None)
            # C14b: the delivery turn's playout completed — the second
            # persisted timestamp (window edges stamp in the scorekeeper).
            if any(str(k).endswith("_delivery") for k in confirmed):
                self.sk.note_question_time("delivery_confirmed_at")
        # Task 0 (RECOGNITION-VARIETY): BOTH sides of the call persist.
        # Recording at playout completion means the record holds what the
        # room actually heard — never a dispatched-but-swallowed turn.
        self.publish_agent_transcription_nowait(
            spoken_text,
            speech_id=speech_id,
            interrupted=False,
        )
        self.record_agent_turn(
            spoken_text, act_keys=sorted(confirmed or []), interrupted=False
        )
        # WO-2: remember A-or-B offers so a following bare yes cannot start.
        try:
            self.note_or_choice_offer(spoken_text)
        except Exception:
            pass
        # Pictures-on offer: arm so a short "yes" / "live immediately"
        # flips media_mode while the lane is healthy but still voice_only.
        try:
            self.note_picture_on_offer(spoken_text)
        except Exception:
            pass
        # PATCH-002 A4b: her own wait-promise binds her. A turn that just
        # played and said "take your time" enters the hold — no unsolicited
        # turn or delivery until the table speaks (the live "take your
        # time" followed by more talking 5s later).
        if lily_say_gate.lily_self_hold_phrase(spoken_text):
            self.enter_hold(reason="self_wait_promise")
        # PATCH-003 P6/P10: asking obligates listening. A CONVERSATIONAL
        # turn (not a game delivery — game questions carry their own
        # engine windows) that ends on a question yields the floor: her
        # queued beats hold until the table answers or the timeout gives
        # one gentle re-offer. Released the instant a user final lands
        # (on_transcript_event). game deliveries/verdicts/reveals are
        # exempt — their own machinery governs them.
        game_act = any(
            k in self._GAME_LANE_ACTS
            or k.endswith("_delivery") or k.endswith("_reveal")
            for k in (confirmed or [])
        )
        if not game_act and lily_say_gate.lily_stacked_question_flag(spoken_text) >= 1:
            self.enter_question_pending(spoken_text)
        # Honesty assist (desync WO Sub-agent C): the state note serviced
        # the turn that just finished playing — one-shot, consumed here.
        self._state_note = None
        # RECOGNITION-HONESTY: same one-shot lifecycle — the returner-claim
        # conditioning serviced this turn; a fresh claim re-sets it.
        self._returner_honesty_note = None
        # P0-C: a confirmed (non-suppressed) turn while a why-note was armed
        # counts as the why-beat landing — unlock kickoff for when he asks.
        if (
            not (interrupted or suppressed)
            and getattr(self, "_recognition_why_note", None)
            and getattr(self, "_recognition_dispute", False)
        ):
            self._recognition_dispute_why_answered = True
            logger.info(
                "LILY_HONESTY | RECOGNITION_WHY_ANSWERED | session=%s",
                self.sk.session_id,
            )
        self._recognition_why_note = None
        # HOTFIX-005 X12: the explain restatement and the contest re-check
        # each serviced this turn — one-shot, consumed here. A fresh explain
        # or contest utterance next turn re-arms.
        self._explain_request_note = None
        self._contest_note = None
        # HOTFIX-006 N9: the late-answer announcement rode this turn. Also
        # one-shot — the miss is stated once, warmly, and does not become a
        # thing she keeps bringing up.
        self._late_answer_note = None
        # Self-knowledge Task 3: the what's-new delta rode the greeting —
        # once the greet has genuinely played out, the stamp moves forward
        # (never before: an interrupted greet keeps the delta for a retry).
        if (
            getattr(self, "_whats_new_pending", False)
            and self.say_registry.state("session_greet")
            == lily_say_gate.CLAIM_CONFIRMED
        ):
            self._whats_new_pending = False
            self._stamp_feature_version()
        if self._pending_reveal_event is not None:
            # Reveal speech finished without a speaking-start hook having
            # fired the packet (safety net) — emit now so the UI never hangs.
            ev, self._pending_reveal_event = self._pending_reveal_event, None
            self.send_event_nowait("reveal", ev)
        transition_complete = any(
            key.endswith("_reveal")
            or (key.startswith("round_") and key.endswith("_scores"))
            for key in confirmed
        )
        if transition_complete:
            # P0-4: this is the explicit between-question seam. A recognition
            # match that landed during delivery/window/adjudication gets ONE
            # beat here, before N+1 dispatch. Return so it cannot stack over
            # the next question; the idle watchdog owns subsequent delivery.
            if self.flush_late_recognition_at_seam():
                return
            # Normal questions advance after their single verdict/reveal
            # beat. Round boundaries advance only after the separately keyed
            # standings flourish; q_N_verdict deliberately does not satisfy
            # this gate, so N+1 cannot queue over the previous round's scores.
            if self.dispatch_armed_question(source="post_reveal"):
                return
            # HOTFIX-008 Z2c: the beat finished on the air but there is no
            # armed question to deliver (supply was starved at arm time).
            # next_delivery must be reachable WITHOUT an armed question —
            # close the beat with its terminal marker and free the claim,
            # so the idle/supply recovery runs and a fresh transition
            # delivers N+1 when supply returns.
            open_qnum = getattr(self, "_open_transition_qnum", None)
            if open_qnum is not None and self.armed_question is None:
                self.release_completed_transition(
                    open_qnum, reason="supply_empty_post_reveal"
                )
        if (
            self.armed_question is not None
            and not self.sk.answer_window_open
            and not self._adjudicating
        ):
            # Text-similarity is TELEMETRY ONLY (desync WO Sub-agent B):
            # the ratio is logged for analysis and acted on by nothing.
            # Live evidence: 0.00–0.15 on questions the table demonstrably
            # heard — the matcher can never again decide game state.
            ratio = lily_evaluation.lily_question_spoken_ratio(
                self.armed_question.get("prompt", ""), spoken_text
            )
            self._last_armed_speech_ratio = ratio
            logger.info(
                "LILY_WINDOW | RATIO | session=%s q=%d ratio=%.2f | telemetry",
                self.sk.session_id, self.sk.question_number, ratio,
            )
            key = f"q_{self.sk.question_number}_delivery"
            if (
                self.say_registry.state(key)
                == lily_say_gate.CLAIM_CONFIRMED
            ):
                # HOTFIX-008 Z2c (lily-938EFF, the phase=reveal→answering
                # regression): a question adjudication already owns
                # (note_answer_heard ran — its ruling is committed or
                # committing) has NO answer window. The confirmed delivery
                # claim outlives the ruling, so without this check every
                # post-ruling agent turn re-opened the dead question's
                # window and fed candidates back into an adjudication
                # loop. The question's legitimate FIRST open lands in this
                # same branch BEFORE adjudication starts, when it is never
                # terminal — only the stale reopen is refused.
                if self.question_is_terminal(self.sk.question_number):
                    logger.warning(
                        "LILY_WINDOW | REOPEN_REFUSED_TERMINAL | "
                        "session=%s q=%d — adjudication owns this "
                        "question; a ruled question has no answer window "
                        "(HOTFIX-008 Z2c)",
                        self.sk.session_id, self.sk.question_number,
                    )
                    return
                # Delivery is registered (claimed at dispatch, confirmed
                # just above) and its turn finished playing — open.
                self._armed_speech_misses = 0
                logger.info(
                    "LILY_WINDOW | OPEN | session=%s q=%d reason=delivery_claim "
                    "ratio=%.2f",
                    self.sk.session_id, self.sk.question_number, ratio,
                )
                self.open_window_after_discharge()
                return
            # No registered delivery: NEVER open a ghost window (the old
            # fallback opened windows on questions nobody heard — the
            # ghost-game / "official re-run" theater). After N finished
            # agent turns with a question armed in phase=question,
            # dispatch ONE structural delivery nudge instead: the nudged
            # turn claims q_{N}_delivery at dispatch and the window opens
            # at ITS playout — the pipeline never stalls, and the window
            # only ever opens on a registered delivery.
            self._armed_speech_misses += 1
            if (
                self._armed_speech_misses < WINDOW_FALLBACK_AGENT_TURNS
                or self.ui_phase != "question"
            ):
                return
            # T2 (PATCH-001): an answered question never re-airs — the
            # nudge included.
            if self.question_already_answered(self.sk.question_number):
                self._armed_speech_misses = 0
                return
            self._armed_speech_misses = 0
            logger.warning(
                "LILY_WINDOW | DELIVERY_NUDGE | session=%s q=%d ratio=%.2f "
                "— no delivery claim after %d agent turns; dispatching a "
                "structural delivery turn",
                self.sk.session_id, self.sk.question_number, ratio,
                WINDOW_FALLBACK_AGENT_TURNS,
            )
            # Remember ratio for undelivered reconcile (near-miss must not
            # re-air after the table already heard the Q).
            self._last_armed_speech_ratio = ratio
            if ratio >= 0.9:
                # Gun 3 fix: high-similarity turn already aired the Q but
                # the structural claim missed — confirm + open, do NOT
                # full re-read (live double-question class).
                logger.warning(
                    "LILY_WINDOW | NUDGE_NEAR_MISS | session=%s q=%d "
                    "ratio=%.2f — confirming delivery without re-read | "
                    "spoken=%r | armed=%r",
                    self.sk.session_id, self.sk.question_number, ratio,
                    (spoken_text or "")[:220],
                    str((self.armed_question or {}).get("prompt", ""))[:180],
                )
                self.force_confirm_delivery_heard(
                    reason="nudge_near_miss", ratio=ratio,
                )
                return
            self.expect_delivery()
            self.gated_say(
                None,
                "question_nudge",
                (
                    "The armed question has not been asked cleanly yet. "
                    "Deliver it now: one short bridge if you need it, then "
                    "its question sentence exactly as written, whole, in "
                    "one breath — then stop and let the table answer."
                ),
                source="window_fallback",
            )

    def _answer_window_duration(self) -> float:
        """The standard answer-window duration for the CURRENT pacing
        (group prefs WO): timed = lily_config.answer_window_seconds()
        exactly (today's behavior); relaxed = that base ×
        LILY_RELAXED_WINDOW_MULTIPLIER (default 2.0). Explicitly-passed
        durations (the steal window) never go through this."""
        base = lily_config.answer_window_seconds()
        if self.sk.pacing == "relaxed":
            return base * lily_config.relaxed_window_multiplier()
        return base

    def open_window_after_discharge(
        self,
        duration: float | None = None,
        core_completion: bool = False,
    ) -> None:
        """Room-discharge pacing (WS-14, AMENDMENT-002): a structural gap
        of lily_config.room_discharge_seconds() between question-delivery
        playout completion and the window's mic-sensitive phase, letting
        the room's acoustic energy decay so player answers arrive at
        higher effective SNR. Gap 0 = open immediately (pre-WS-14
        behavior). Answers spoken during the gap keep buffering through
        buffer_pre_window_answer (window closed + delivery claimed = its
        exact buffering condition) and replay at open — nothing is lost
        to the pause.

        WS-5 interface: this is the pacing hook. When the window moves to
        stem completion (stem completes -> window arms -> options continue
        -> discharge gap governs adjudication sensitivity), call THIS at
        the arming point instead of open_window(); the gap and its config
        knob do not move. Steal windows never discharge — they open off a
        ruling, not a delivery playout.

        HOSTLOOP-001 C3a takes that interface up: `core_completion=True` is
        the arming call made when the core question sentence finishes, while
        the options read continues."""
        # core_completion is passed ONLY when set, so the ordinary discharge
        # call stays byte-identical for every existing caller and stub.
        extra = {"core_completion": True} if core_completion else {}
        gap = lily_config.room_discharge_seconds()
        if gap <= 0:
            self.open_window(duration=duration, **extra)
            return
        logger.info(
            "LILY_WINDOW | DISCHARGE | session=%s q=%d gap=%.2fs",
            self.sk.session_id, self.sk.question_number, gap,
        )

        async def _discharge() -> None:
            await asyncio.sleep(gap)
            # A racing open (ARMED_LIMBO reopen, adjudication start) wins.
            if self.sk.answer_window_open or self._adjudicating:
                return
            self.open_window(duration=duration, **extra)

        asyncio.ensure_future(_discharge())

    def open_window(
        self,
        duration: float | None = None,
        steal: bool = False,
        core_completion: bool = False,
    ) -> None:
        """`core_completion` (HOSTLOOP-001 C3a): this window is opening at
        the CORE QUESTION's completion, with the option read still airing.
        Everything about the window itself is identical — the only difference
        is that the in-flight MC read markers are NOT cleared, because the
        read really is still in flight and C3b/C3c still need to be able to
        truncate or resume it."""
        if getattr(self, "_delivery_stop_sticky", False):
            logger.info(
                "LILY_WINDOW | OPEN_BLOCKED | session=%s q=%d "
                "reason=game_stopped",
                self.sk.session_id, self.sk.question_number,
            )
            return
        if not getattr(self, "game_started", False):
            # WS-1: no window arming in any pre-game phase — the ghost
            # q_0001 window adjudicated Rhonda's self-introduction as a
            # wrong answer to a question never spoken.
            logger.error(
                "LILY_WINDOW | PRE_GAME_REFUSED | session=%s q=%d",
                self.sk.session_id, self.sk.question_number,
            )
            return
        # HOTFIX-006 N3 invariant 1: an adjudicable window may only ever
        # exist over a REGISTERED question. In lily-16A9AE Lily improvised
        # a question that was never registered; Chris answered it and his
        # "Cape Cod Canal." was adjudicated against kb_180 (Psycho) and
        # marked incorrect — while she told him "Chris, you got it!
        # Putting you right on the board." Nothing improvised gets a
        # window; speech arriving with no open registered window is logged
        # and never scored.
        registered = self.armed_question if not steal else (
            self.armed_question or self.sk.current_question
        )
        if registered is None:
            logger.error(
                "LILY_WINDOW | UNREGISTERED_REFUSED | session=%s q=%d — "
                "window requested with no registered question armed; "
                "refusing (N3 invariant 1)",
                self.sk.session_id, self.sk.question_number,
            )
            return
        # HOTFIX-006 N8: a REVEALED question cannot open a steal window.
        # Live: "No worries! The correct answer is Frankenstein" followed
        # immediately by "Frankenstein! That opens a five-second steal
        # window for Chris" — a steal offered on the answer she had just
        # given away. Reveal burns (WS-4), and this is the dispatch seam
        # where burn is checked, however the steal was arrived at. Ordinary
        # windows are untouched: a fresh question is never burned, and the
        # steal mechanic over an UNREVEALED question is unchanged.
        if steal and self._is_burned(registered):
            logger.error(
                "LILY_WINDOW | STEAL_REFUSED_REVEALED | session=%s q=%d "
                "question_id=%s — the answer has already gone to air; there "
                "is nothing left to steal (N8)",
                self.sk.session_id, self.sk.question_number,
                registered.get("id"),
            )
            return
        # HOTFIX-009 W4: relaxed pacing runs no clock. An ordinary relaxed
        # window (no explicit duration, not a steal) opens UNTIMED — no
        # deadline, no _expire task — and the beat closes on the roster
        # instead (_maybe_close_relaxed_beat). A steal window and any
        # explicitly-timed caller keep their clock; relaxed disarms the
        # steal branch upstream, so a relaxed steal window never opens here.
        relaxed_untimed = (
            self.sk.pacing == "relaxed" and duration is None and not steal
        )
        dur = (
            None
            if relaxed_untimed
            else (
                duration if duration is not None
                else self._answer_window_duration()
            )
        )
        # N3 invariant 2: the question id is CAPTURED HERE, at window-open,
        # and carried through to the ledger write — never inferred at
        # adjudication time from whatever is armed by then. A steal window
        # rides the same question, so it captures the same identity.
        self.sk.open_answer_window(
            duration=dur,
            reset_candidates=not steal,
            question_id=registered.get("id"),
            question_index=self.sk.question_number,
            registered=True,
            untimed=relaxed_untimed,
        )
        self._steal_window = steal
        # M4: the window opening IS the stem's completion — terminal.
        if not steal:
            self.mark_stem_completed(self.sk.question_number)
            # HOTFIX-005 X3: the window opens exactly when the delivery
            # turn's playout completes — so THIS is the durable "delivery
            # reached playout" record the reveal side gates on. A steal
            # window rides an already-delivered question, so it never adds.
            delivered = getattr(self, "_delivered_to_playout", None)
            if delivered is None:
                delivered = self._delivered_to_playout = set()
            delivered.add(self.sk.question_number)
        # WS-2: the delivery aired — its window is open, so no undelivered
        # claim is stuck for this question anymore.
        self._undelivered_ticks = 0
        self._undelivered_refires = 0
        # WS-5: the window is open — no MC options read is still in flight
        # to truncate (whether it played out fully or was answer-aborted).
        # C3a: unless the window opened AT CORE COMPLETION, in which case the
        # options are still airing and those markers are live state.
        if not core_completion:
            self._mc_delivery_qnum = None
            self._active_delivery_qnum = None
            self._active_delivery_started_at = None
            self._active_delivery_ended_at = None
        self._set_ui_phase("answering")
        self.publish_attributes_nowait()
        # Screen sync: idempotent backstop. The publish normally already
        # happened at playout START (publish_question_to_glass, wired from
        # note_playout_started) — this covers the paths that open a window
        # without a delivery turn ever airing, and is a no-op when the
        # question is already on the glass.
        if not steal:
            self.publish_question_to_glass(reason="window_open")
            # Same backstop for the burn: a window that opened without a
            # delivery playout (near-miss confirm, reconnect replay) still
            # means the table heard the question.
            self.record_question_asked(reason="window_open")
        self._start_bed()
        # HOTFIX-009 W6: same self-cancel class as the adjudicate head —
        # this runs from adjudicate's steal branch INSIDE the timer task
        # being replaced (it survived live only because the steal branch
        # returns synchronously, so the pending cancel never got a
        # suspension point to land on). The current task never needs
        # cancelling; the reassignment below already retires it.
        if (
            self._window_timer
            and not self._window_timer.done()
            and self._window_timer is not asyncio.current_task()
        ):
            self._window_timer.cancel()

        # HOTFIX-009 W4: an untimed relaxed window arms NO expiry task —
        # there is no clock to close it. The prior timer (if any) is
        # cancelled above and the handle is cleared, so nothing lingers to
        # expire a window that must stay open until the roster completes.
        if relaxed_untimed:
            self._window_timer = None
        else:
            async def _expire() -> None:
                await asyncio.sleep(dur)
                if self.sk.answer_window_open and not self._adjudicating:
                    await self.adjudicate(steal_allowed=not steal)

            self._window_timer = asyncio.ensure_future(_expire())
        # Early-buzz replay (fixture Q5): answers spoken during the
        # delivery playout become candidates NOW that the window is live.
        if not steal:
            self._replay_pre_window_answers()

    def _maybe_close_relaxed_beat(self) -> None:
        """HOTFIX-009 W4: with no clock, the relaxed answer beat closes on
        PEOPLE. Once every rostered player has an answer in and no clarify
        is pending, adjudication fires through the same seam the instant-
        Tier-1 path uses — right, wrong, or a pass, one per rostered voice
        closes the beat. Timed mode never reaches here in practice (its
        window closes on the _expire clock), and the pacing guard makes it
        a no-op there regardless. Runs on every final that recorded a
        candidate in an open relaxed window."""
        if self.sk.pacing != "relaxed":
            return
        if not self.sk.answer_window_open or self._adjudicating:
            return
        if getattr(self, "pending_clarify", None):
            # A clarify is out to a player — the beat waits on their reply,
            # not the roster count.
            return
        roster = self.sk.players
        if not roster:
            return
        if not all(name in self.sk.answer_candidates for name in roster):
            return
        logger.info(
            "LILY_WINDOW | RELAXED_BEAT_CLOSE | session=%s q=%d roster=%d — "
            "every rostered player answered; adjudicating with no clock",
            self.sk.session_id, self.sk.question_number, len(roster),
        )
        asyncio.ensure_future(self.adjudicate(steal_allowed=False))

    def _start_bed(self) -> None:
        path = lily_config.thinking_bed_path()
        if not path or self.background_audio is None:
            return
        try:
            self._bed_handle = self.background_audio.play(
                AudioConfig(path, volume=0.5), loop=True
            )
        except Exception as e:
            logger.warning("LILY_SFX | thinking bed failed: %s", e)

    def _stop_bed(self) -> None:
        if self._bed_handle is not None:
            try:
                self._bed_handle.stop()
            except Exception:
                pass
            self._bed_handle = None

    def _stinger(self, correct: bool) -> None:
        """Correct/incorrect stingers — the ding IS the ruling."""
        path = (
            lily_config.stinger_correct_path()
            if correct
            else lily_config.stinger_incorrect_path()
        )
        if not path or self.background_audio is None:
            return
        try:
            self.background_audio.play(AudioConfig(path, volume=0.8))
        except Exception as e:
            logger.warning("LILY_SFX | stinger failed: %s", e)

    # -- addressee classifier (WO-LILY-FLOOR-001 FL-1) -----------------------------


    def _nbest_lookup(self, key: str | None) -> dict | None:
        """The drained n-best dict recorded for a candidate key, tolerating
        the two unrostered spellings in play ("unrostered:S2" vs the
        scorekeeper's 'UU' fallback for label-less voices). Default-safe on
        partially-constructed games (offline test harnesses build LilyGame
        via __new__)."""
        if not key:
            return None
        store = getattr(self, "_nbest_by_key", None) or {}
        found = store.get(key)
        if found is None and key == "unrostered:None":
            found = store.get("unrostered:UU")
        return found

    def _tier1_question(
        self, text: str, question: dict, key: str | None = None,
        threshold: float | None = None,
    ) -> dict:
        """Tier-1 format dispatch, n-best aware: when a drained hypothesis
        set exists for this candidate, fuzzy matching runs across ALL
        hypotheses (correct > uncertain > incorrect precedence) and the
        dispersion gate can demote a definitive verdict to "uncertain"
        (deliberation escalates instead of scoring). Without hypotheses
        this is exactly the existing single-text path."""
        nbest = self._nbest_lookup(key)
        hyps = (nbest or {}).get("hypotheses") or []
        if nbest is not None and len(hyps) > 1:
            return lily_evaluation.lily_tier1_evaluate_nbest(
                text,
                question,
                hypotheses=hyps,
                dispersion=nbest.get("dispersion"),
                dispersion_threshold=lily_config.nbest_dispersion_threshold(),
                threshold=threshold,
            )
        return lily_evaluation.lily_tier1_evaluate_question(
            text, question, threshold=threshold
        )




    def _receipt_yields_to_clarify(
        self, t1: dict, tier1_threshold: float
    ) -> bool:
        """HOSTLOOP-001 C6: does the instant ack stand down in favour of the
        clarify question? True when this Tier-1 result sits in the middle
        BAND — the same read _maybe_fire_clarify makes, from the same values,
        so there is one band rule and not two. Fails OPEN (receipt speaks)
        on anything unreadable: the receipt existing is the clause."""
        if t1.get("verdict") != "uncertain":
            return False
        similarity = t1.get("similarity")
        if not isinstance(similarity, (int, float)):
            return False
        try:
            return lily_evaluation.lily_tier1_band(
                float(similarity), tier1_threshold,
                lily_config.tier1_clarify_margin(),
            ) == lily_evaluation.BAND_CLARIFY
        except Exception:
            return False






    async def _speculative_judge(
        self,
        question: dict,
        attempt_text: str,
        key: str,
        nbest: dict | None = None,
    ) -> dict | None:
        """One single-attempt Tier-2 call, fired mid-window. Returns the
        parsed verdict dict or None. Never raises. When an n-best dict is
        supplied its hypotheses ride the judge prompt as competing
        transcriptions of the same utterance (judge-never-invents:
        they widen what was SAID, never what the answer IS)."""
        try:
            hyps = (nbest or {}).get("hypotheses") or []
            raw = await self.reasoning.judge(
                lily_evaluation.LILY_JUDGE_INSTRUCTIONS,
                lily_evaluation.lily_build_judge_prompt(
                    question.get("prompt", ""),
                    str(question.get("canonical_answer", "")),
                    [(key, attempt_text)],
                    acceptable_answers=question.get("acceptable_answers") or [],
                    hypotheses_by_speaker=(
                        {key: hyps} if len(hyps) > 1 else None
                    ),
                ),
                adult=(self.sk.mode == "adult"),
            )
            verdict = lily_evaluation.lily_parse_judge_response(raw)
            logger.info(
                "LILY_JUDGE | SPECULATIVE | key=%s verdict=%s",
                key, (verdict or {}).get("verdict"),
            )
            return verdict
        except Exception as e:
            logger.warning("LILY_JUDGE | speculative failed key=%s: %s", key, e)
            return None

    async def skip_question(self, source: str) -> None:
        """Skip: identical for spoken 'skip' and the RPC tap — no comment,
        no spotlight on who asked."""
        if self._adjudicating or getattr(self, "_question_transitioning", False):
            logger.info(
                "LILY_STATE | SKIP_IGNORED | session=%s source=%s "
                "reason=question_transition",
                self.sk.session_id, source,
            )
            return
        if self.armed_question is None and not self.sk.answer_window_open:
            return
        self._question_transitioning = True
        try:
            logger.info("LILY_STATE | SKIP | session=%s source=%s q=%d",
                        self.sk.session_id, source, self.sk.question_number)
            # M4: a skip that abandons a stem which aired but never opened
            # a window terminates its dispatch record (cancellation).
            self._terminate_aired_stem(reason=f"skip:{source}")
            if self._window_timer and not self._window_timer.done():
                self._window_timer.cancel()
            self.sk.close_answer_window()
            self._stop_bed()
            self.armed_question = None
            self.sk.current_question = None
            self._set_ui_phase("question")
            # Hot-path: glass updates must not delay skip → next-question
            # speech. Same pattern as flush_for_mode_switch / on_answer_leak.
            asyncio.ensure_future(self.publish_metadata(""))
            self.publish_attributes_nowait()
            # Structural delivery claim: when the next question arms, the
            # skip follow-up turn is its delivery.
            if self.arm_next_question():
                self.expect_delivery()
            self.gated_say(
                None,
                "skip",
                "That question was skipped. Move straight to the next "
                "question with zero commentary about the skip and no "
                "spotlight on who asked. If the state block has the next "
                "question, ask it now — its question sentence exactly as "
                "written.",
                source=f"skip_{source}",
            )
        finally:
            self._question_transitioning = False

    # -- adjudication ---------------------------------------------------------

    async def adjudicate(
        self, steal_allowed: bool = True, reclaim_transition: bool = False
    ) -> None:
        """Close the window and commit results. The scorekeeper decided
        ORDER by timestamps; Tier-1/Tier-2 decide CORRECTNESS; this commit
        happens BEFORE Lily narrates (§11.3 event-bound truth).

        `reclaim_transition` is the watchdog recovery's flag ONLY: it lets
        open_question_transition release a journaled-but-never-aired
        transition (see RECLAIMED_UNAIRED) instead of dead-locking against
        it (lily-1C53C6)."""
        # REFACTOR WAVE 1a: shadow the typed GameControl gate. may("adjudicate")
        # is compared against this guard; the latch guard stays authoritative.
        if getattr(self, "_delivery_stop_sticky", False):
            _legacy_reason = "game_stopped"
        elif self._adjudicating:
            _legacy_reason = "already_adjudicating"
        elif getattr(self, "_question_transitioning", False):
            _legacy_reason = "transitioning"
        elif self.armed_question is None:
            _legacy_reason = "no_armed_question"
        else:
            _legacy_reason = None
        self._gamecontrol_parity(
            lily_game_control.ADJUDICATE_ACT, "adjudicate", _legacy_reason,
            "adjudicate",
        )
        if _legacy_reason is not None:
            return
        # HOTFIX-005 X3: no reveal without delivery. Adjudication ends in a
        # verdict/reveal act; a question whose delivery never reached
        # playout has nothing to reveal and no answer anyone could have
        # given (the 14:53:34 fixture aired "on the question: false" for a
        # question never spoken). Three independent proofs the stem reached
        # playout, ANY of which clears the guard:
        #   (a) the answer window is open right now (open_window only fires
        #       at the delivery turn's playout completion) — also covers
        #       reconnect, where the durable set is a fresh-process empty;
        #   (b) the q_{N}_delivery claim is CONFIRMED — the same signal the
        #       ARMED_LIMBO watchdog trusts to force a legitimate recovery
        #       adjudication on a played-out question whose post-delivery
        #       chain died;
        #   (c) a durable delivered-to-playout record from earlier this
        #       session (steal windows ride an already-recorded question).
        # None of the three means the stem never played: refuse and log —
        # the reveal-side mirror of the delivery-registration guard.
        _delivery_key = f"q_{self.sk.question_number}_delivery"
        _delivery_confirmed = (
            self.say_registry.state(_delivery_key)
            == lily_say_gate.CLAIM_CONFIRMED
        )
        if (
            not self.sk.answer_window_open
            and not _delivery_confirmed
            and self.sk.question_number
            not in getattr(self, "_delivered_to_playout", set())
        ):
            logger.error(
                "LILY_REVEAL | REFUSED_NO_DELIVERY | session=%s q=%d — "
                "adjudication requested for a question whose delivery never "
                "reached playout; refusing the reveal (X3)",
                self.sk.session_id, self.sk.question_number,
            )
            return
        self._adjudicating = True
        # T2 (PATCH-001): answer_heard — adjudication starting means this
        # question was answered; every outstanding delivery attempt for it
        # is invalidated NOW, in-flight playout included (the fixture
        # class: the question re-read airing seconds after the answer).
        self.note_answer_heard(self.sk.question_number)
        # SCORING prior (WO-ADDRESSEE-H1 Task 2): mirror the in-flight flag
        # onto the pure scorekeeper so segments arriving DURING adjudication
        # classify under SCORING (backchannels expected, nothing scoreable).
        self.sk.adjudicating = True
        try:
            # HOTFIX-009 W6: a window-timeout adjudication RUNS INSIDE the
            # timer task (open_window's _expire awaits adjudicate), so
            # cancelling the timer here cancelled the running adjudication
            # itself — CancelledError landed at the first await (the reveal
            # publish gather), the beat died between the reveal and verdict
            # journal entries with no CRASHED line, and the wedged
            # transition later re-narrated an already-aired reveal
            # (lily-5E3036 q4, "Nobody landed it. Marie Curie" 2m16s after
            # the organic reveal). The expired timer needs no cancel.
            if (
                self._window_timer
                and not self._window_timer.done()
                and self._window_timer is not asyncio.current_task()
            ):
                self._window_timer.cancel()
            self.sk.close_answer_window()
            self._stop_bed()

            # Candidates are evaluated under the prior their window was
            # CAPTURED in (OVERLAP if crosstalk flipped inside it, else
            # OPEN_WINDOW) — never under the SCORING state this evaluation
            # itself runs in. overlap_flag persists across the close above.
            window_prior = self.sk.window_prior_state()
            window_addressee_conf = self.sk.window_addressee_confidence()
            tier1_threshold = self.sk.tier1_threshold_for_state(
                window_prior,
                addressee_confidence=window_addressee_conf,
            )
            logger.info(
                "LILY_PRIOR | state=%s threshold=%.3f overlap=%s "
                "addressee_conf=%s | "
                "session=%s q=%d source=adjudicate",
                window_prior, tier1_threshold, self.sk.overlap_flag,
                (
                    f"{window_addressee_conf:.3f}"
                    if isinstance(window_addressee_conf, (int, float))
                    else "None"
                ),
                self.sk.session_id, self.sk.question_number,
            )

            question = self.armed_question
            acceptable = question.get("acceptable_answers") or [
                str(question.get("canonical_answer", "")).lower()
            ]
            # ── THE ADJUDICATION BOUNDARY (WO-LILY-HOTFIX-006 N3/N4) ──────
            # Everything past this point writes to the ledger, so this is
            # where WHAT may be adjudicated, and against WHICH question, is
            # decided once.
            #
            # N3 invariant 2 — WINDOW BINDING. The question is the one
            # captured AT WINDOW-OPEN, never re-derived from whatever is
            # armed by the time the reveal chain finally runs. In
            # lily-4FB3B2 two questions were asked and BOTH answer rows
            # were filed against q_4821: Rhonda's "We don't know." was
            # spoken to the Frankenstein question and adjudicated against
            # question one. The captured id/index below is what reaches
            # record_result and the lily_answers row.
            binding = self.sk.window_binding()
            bound_question_id = (
                binding.get("question_id")
                if binding.get("question_id") is not None
                else question.get("id")
            )
            bound_question_index = (
                binding.get("question_index")
                if binding.get("question_index") is not None
                else self.sk.question_number
            )
            # N3 invariant 1 — ADJUDICATION SCOPE. A window that was never
            # opened over a registered question produces nothing scoreable.
            if binding.get("registered") is False:
                logger.error(
                    "LILY_ADJUDICATE | UNREGISTERED_WINDOW | session=%s "
                    "q=%s — candidates arrived with no registered question; "
                    "logged, never scored (N3 invariant 1)",
                    self.sk.session_id, bound_question_index,
                )
            ordered = []
            for c in self.sk.ordered_candidates():
                key = c["player"] or f"unrostered:{c['speaker_label']}"
                if key in self._judged_keys:
                    continue
                if binding.get("registered") is False:
                    continue
                # Candidates carried over from an EARLIER question's window
                # (close_answer_window never cleared them) are not this
                # question's answers. They are logged and dropped, never
                # filed under a question they were not spoken to.
                if not self.sk.candidate_bound_to(
                    c, bound_question_id, bound_question_index
                ):
                    logger.error(
                        "LILY_ADJUDICATE | UNBOUND_CANDIDATE | session=%s "
                        "key=%s arrived_in=%s/%s adjudicating=%s/%s "
                        "text=%r — dropped, never filed against another "
                        "question (N3 invariant 2)",
                        self.sk.session_id, key,
                        c.get("window_question_id"),
                        c.get("window_question_index"),
                        bound_question_id, bound_question_index,
                        str(c.get("text"))[:60],
                    )
                    continue
                # N4 — the open window admits ANSWER-SHAPED utterances
                # only. Seven live rows across three sessions were
                # meta-speech about the game entered as answer attempts,
                # one of them scored a point ("Um. Why are we. Why are we
                # in Mumbai or Delhi?" -> kb_128, CORRECT, 1 point). The
                # intake filter is the first gate; this is the boundary
                # gate, so a candidate that arrived by any other route
                # (replay, steal carryover, a rehydrated checkpoint) still
                # cannot be adjudicated. Answer-surface matches always
                # pass, so a murmured real answer inside a complaint-heavy
                # turn keeps scoring.
                non_answer = lily_evaluation.lily_non_answer_utterance(
                    str(c.get("text") or ""), question, list(self.sk.players)
                )
                if non_answer:
                    logger.info(
                        "LILY_ADJUDICATE | NON_ANSWER_DROPPED | session=%s "
                        "key=%s reason=%s text=%r — routed to the "
                        "conversational lane, never adjudicated (N4)",
                        self.sk.session_id, key, non_answer,
                        str(c.get("text"))[:60],
                    )
                    continue
                # FL-1's fused judgment, where it exists, on top of the
                # deterministic floor above. Speech the floor machine read
                # as a SIDE CLUSTER — players deep in a conversation with
                # each other — is not an answer to the host, whatever shape
                # it has. (Plain side_chatter is NOT enough on its own: a
                # muttered real answer classifies that way, and the
                # answer-surface override is what protects it.)
                if c.get("fl1_classification") == (
                    lily_addressee_classifier.CLASS_SIDE_CLUSTER
                ):
                    logger.info(
                        "LILY_ADJUDICATE | SIDE_CLUSTER_DROPPED | session=%s "
                        "key=%s text=%r — FL-1 read the floor as a "
                        "player-to-player conversation (N4)",
                        self.sk.session_id, key, str(c.get("text"))[:60],
                    )
                    continue
                ordered.append(c)

            winner: str | None = None
            winner_candidate: dict | None = None
            eval_tier = 1
            judge_reason = ""
            uncertain: list[dict] = []

            # Self-correction (live 2026-07-15 fix): every in-window final a
            # player committed is an attempt; the earliest CORRECT attempt
            # across the whole table wins. A revision competes from its own
            # (later) timestamp, so it can never jump the queue — but it can
            # score, which "first final locks the slot" wrongly prevented
            # ("the spine… no, the femur" lost a point it had earned).
            # N9 part 1: capture binds the UTTERANCE, not a slot. Every
            # attempt in the timeline carries its own transcript id, and
            # the one that decides a candidate's verdict is recorded on the
            # candidate — so the ledger row names the utterance actually
            # judged. The live q_1052 row recorded Rami's earlier "Go."
            # while "Okay. It's Jupiter." never entered the ledger at all.
            # Each candidate's default binding is its FIRST answer-shaped
            # attempt (the one that claimed their place in the order),
            # never "most recent".
            timeline_entries = []
            for cand in ordered:
                attempts = cand.get("attempts") or [{
                    "text": cand["text"],
                    "segment_start_time": cand["segment_start_time"],
                    "utterance_id": cand.get("utterance_id"),
                }]
                for attempt in attempts:
                    attempt_text = attempt.get("text") or ""
                    # N4 at attempt granularity: one meta-speech fragment
                    # inside a player's turn must not become the utterance
                    # their row is written from.
                    if lily_evaluation.lily_non_answer_utterance(
                        attempt_text, question, list(self.sk.players)
                    ):
                        continue
                    if cand.get("_bound_attempt") is None:
                        cand["_bound_attempt"] = attempt
                    timeline_entries.append((
                        attempt.get(
                            "segment_start_time", cand["segment_start_time"]
                        ),
                        cand, attempt_text, attempt,
                    ))
                if cand.get("_bound_attempt") is None:
                    # Nothing answer-shaped survived — the candidate has no
                    # adjudicable utterance and is dropped with the rest of
                    # the meta-speech.
                    cand["_bound_attempt"] = None
            ordered = [c for c in ordered if c.get("_bound_attempt") is not None]
            timeline_entries = [
                e for e in timeline_entries if e[1] in ordered
            ]
            attempts_timeline = sorted(
                timeline_entries, key=lambda entry: entry[0]
            )
            for _, cand, attempt_text, attempt in attempts_timeline:
                attempt_conf = (
                    attempt.get("addressee_confidence")
                    if isinstance(attempt, dict)
                    else None
                )
                if attempt_conf is None:
                    attempt_conf = cand.get("addressee_confidence")
                attempt_threshold = self.sk.tier1_threshold_for_state(
                    window_prior,
                    addressee_confidence=(
                        attempt_conf
                        if attempt_conf is not None
                        else window_addressee_conf
                    ),
                )
                # Format dispatch (multiple-choice WO): MC questions match
                # letters / positions / option text and may return a
                # DEFINITIVE "incorrect" (clean wrong pick — no Tier-2);
                # only "uncertain" (mumbles) escalates to the judge.
                # n-best aware (WO-ADDRESSEE-H1 Task 1): fuzzy matching
                # runs across all hypotheses; high dispersion escalates.
                # Each ATTEMPT text (self-correction timeline) evaluates
                # under the state-prior threshold (Task 2).
                t1 = self._tier1_question(
                    attempt_text, question,
                    key=cand["player"] or f"unrostered:{cand['speaker_label']}",
                    threshold=attempt_threshold,
                )
                if t1["verdict"] == "correct":
                    cand["_tier1"] = t1
                    cand["text"] = attempt_text  # score the words that won
                    cand["_bound_attempt"] = attempt  # …and bind THAT one
                    winner_candidate = cand
                    break
                # Keep the strongest per-candidate verdict for audit and
                # Tier-2: an uncertain attempt outranks an incorrect one.
                prev = cand.get("_tier1")
                if prev is None or (
                    prev["verdict"] == "incorrect" and t1["verdict"] == "uncertain"
                ):
                    cand["_tier1"] = t1
                    if t1["verdict"] == "uncertain":
                        cand["text"] = attempt_text  # judge these words
                        cand["_bound_attempt"] = attempt
            if winner_candidate is None:
                uncertain = [
                    c for c in ordered
                    if c.get("_tier1", {}).get("verdict") == "uncertain"
                ]

            if winner_candidate is None and uncertain:
                # Tier 2. Prefer verdicts cached by speculative mid-window
                # judging (zero added reveal latency); order stays decided
                # by scorekeeper timestamps — earliest correct/partial wins.
                consumed_speculative = False
                for c in uncertain:
                    key = c["player"] or f"unrostered:{c['speaker_label']}"
                    task = self._spec_judge.get(key)
                    if task is None:
                        continue
                    try:
                        verdict = await asyncio.wait_for(
                            asyncio.shield(task), timeout=4.0
                        )
                    except (asyncio.TimeoutError, Exception):
                        verdict = None
                    consumed_speculative = True
                    if verdict and verdict["verdict"] in ("correct", "partial"):
                        eval_tier = 2
                        judge_reason = verdict.get("reason", "")
                        winner_candidate = c
                        break

                if winner_candidate is None and not consumed_speculative:
                    # Fallback: one batched non-spoken LLM turn at reveal
                    # time (speculation unavailable, e.g. window closed
                    # before any final landed).
                    attempts = [
                        (c["player"] or f"unbound voice {c['speaker_label']}", c["text"])
                        for c in uncertain
                    ]
                    # n-best hypotheses per attempt (WO-ADDRESSEE-H1 Task 1),
                    # keyed by the attempt's speaker string.
                    hyp_map: dict[str, list] = {}
                    for c in uncertain:
                        nb = self._nbest_lookup(
                            c["player"] or f"unrostered:{c['speaker_label']}"
                        )
                        nb_hyps = (nb or {}).get("hypotheses") or []
                        if len(nb_hyps) > 1:
                            hyp_map[
                                c["player"]
                                or f"unbound voice {c['speaker_label']}"
                            ] = nb_hyps
                    try:
                        raw = await self.reasoning.judge(
                            lily_evaluation.LILY_JUDGE_INSTRUCTIONS,
                            lily_evaluation.lily_build_judge_prompt(
                                question.get("prompt", ""),
                                str(question.get("canonical_answer", "")),
                                attempts,
                                acceptable_answers=acceptable,
                                hypotheses_by_speaker=hyp_map or None,
                            ),
                adult=(self.sk.mode == "adult"),
            )
                        verdict = lily_evaluation.lily_parse_judge_response(raw)
                    except Exception as e:
                        logger.error("LILY_JUDGE | call failed: %s", e)
                        verdict = None
                    if verdict and verdict["verdict"] in ("correct", "partial"):
                        eval_tier = 2
                        judge_reason = verdict.get("reason", "")
                        jwinner = verdict.get("winner")
                        for c in uncertain:
                            cname = c["player"] or f"unbound voice {c['speaker_label']}"
                            if jwinner and cname == jwinner:
                                winner_candidate = c
                                break
                        if winner_candidate is None:
                            winner_candidate = uncertain[0]

            points = (
                5 if self.sk.round > self.rounds_total else max(1, self.sk.round)
            )

            if getattr(self, "_delivery_stop_sticky", False):
                logger.warning(
                    "LILY_STOP | ADJUDICATION_ABORTED | session=%s q=%d — "
                    "STOP landed before score commit",
                    self.sk.session_id, self.sk.question_number,
                )
                return

            # Commit — scores land in the scorekeeper BEFORE Lily speaks.
            # T6 (PATCH-001): a failed commit means NO award narration —
            # an in-character hold plus an ERROR, never a celebration the
            # ledger can't back (the live "Saturn is correct — you're on
            # the board!" ×3 with zero answers rows).
            def _bound_utterance_id(cand: dict) -> str | None:
                """N9: the id of the utterance THIS row is about — the
                attempt that decided the verdict, falling back to the
                candidate's own first final. Never "most recent"."""
                attempt = cand.get("_bound_attempt") or {}
                return attempt.get("utterance_id") or cand.get("utterance_id")

            if winner_candidate is not None:
                if winner_candidate["player"]:
                    winner = winner_candidate["player"]
                    try:
                        self.sk.record_result(
                            winner, correct=True, points=points,
                            # N3: the question CAPTURED at window-open.
                            question_id=bound_question_id,
                            question_index=bound_question_index,
                            transcript=winner_candidate["text"],
                            utterance_id=_bound_utterance_id(winner_candidate),
                        )
                    except Exception:
                        logger.exception(
                            "LILY_AWARD | COMMIT_FAILED | session=%s q=%d "
                            "player=%s — award NOT committed; no narration",
                            self.sk.session_id, self.sk.question_number,
                            winner,
                        )
                        # REFACTOR W2a: the commit-failure hold is a
                        # DETERMINISTIC sheet — adjudicate never ends in
                        # instructed_reply, even on the error path. No verdict,
                        # points, or answer (the commit failed).
                        self.gated_say(
                            None,
                            "verdict_hold",
                            "Something needs a second look on that answer. "
                            "Hold warmly and in character — 'ooh, let me "
                            "double-check that one—' — and do NOT announce "
                            "any verdict, points, or the answer.",
                            source="adjudicate_commit_failed",
                            text="Ooh — let me double-check that one.",
                        )
                        return
                else:
                    # Open-floor winner: never silently attributed. Hold the
                    # award until lily_bind_speaker lands for that voice.
                    # Question context rides along so the eventual make-good
                    # commit lands a fully-traceable ledger entry + audit row
                    # (WS-7 — the live make-good had no lily_answers row).
                    self._pending_unbound_award = {
                        "speaker_label": winner_candidate["speaker_label"],
                        "points": points,
                        "question_id": bound_question_id,
                        "question_index": bound_question_index,
                        "transcript": winner_candidate["text"],
                        "utterance_id": _bound_utterance_id(winner_candidate),
                    }
            for cand in ordered:
                key = cand["player"] or f"unrostered:{cand['speaker_label']}"
                self._judged_keys.add(key)
                is_winner = cand is winner_candidate
                utterance_id = _bound_utterance_id(cand)
                if cand["player"] and not is_winner:
                    self.sk.record_result(
                        cand["player"], correct=False, points=0,
                        question_id=bound_question_id,
                        question_index=bound_question_index,
                        transcript=cand["text"],
                        utterance_id=utterance_id,
                    )
                if self.supabase is not None:
                    # N3: the audit row names the question whose window the
                    # utterance arrived in, and the index captured with it —
                    # not self.sk.question_number, which has already moved
                    # on in every race this WO documents.
                    asyncio.ensure_future(lily_persistence.lily_write_answer(
                        self.supabase,
                        self.sk.session_id,
                        cand["player"],
                        bound_question_id,
                        bound_question_index,
                        cand["text"],
                        "correct" if is_winner else "incorrect",
                        # Tier-1 decided verdicts (correct, or MC's
                        # definitive incorrect) audit as tier 1.
                        1 if cand.get("_tier1", {}).get("verdict")
                        in ("correct", "incorrect") else eval_tier,
                        points if is_winner else 0,
                        cause="answer",
                        utterance_id=utterance_id,
                    ))

            # B1 implicit weak labels at adjudication commit: the winning
            # utterance and every scored-incorrect utterance were treated as
            # answers -> label=host_directed, source=implicit_scored_unappealed
            # (UPDATE on the row id kept at insert time — never a second
            # insert). If an appeal path ever re-enters Tier-2 and overturns
            # an attribution, the corrected label goes through the same
            # _apply_addressee_label with LABEL_SOURCE_IMPLICIT_APPEALED —
            # today appeals are handled in-prompt with no code path, so this
            # is the documented hook.
            winner_key = (
                lily_addressee.lily_candidate_key(winner_candidate)
                if winner_candidate is not None else None
            )
            for key, (label, source) in lily_addressee.lily_labels_for_adjudication(
                ordered, winner_key
            ).items():
                self._apply_addressee_label(key, label, source)

            missed = winner_candidate is None
            # ── N8: REVEAL STATE, READ BEFORE THE STEAL BRANCH ────────────
            # Live, back to back:
            #   "No worries! The correct answer is Frankenstein"
            #   "Frankenstein! That opens a five-second steal window for
            #    Chris"
            # The answer was already on the air and a steal window was
            # offered ON THE REVEALED ANSWER — there was nothing left to
            # steal, and anyone "stealing" would only be repeating her.
            # Her own conversational lane can reveal, so reveal state is
            # read HERE, before any steal decision, and a reveal BURNS the
            # question through the existing WS-4 burn protocol: burned is
            # the one-way, already-checked state the steal path (and the
            # re-arm path, and the late-answer path) all gate on.
            if (
                self._reveal_already_on_air(question)
                and not self._is_burned(question)
            ):
                self._burn_question(question, reason="revealed_on_air")
            question_revealed = self._is_burned(question)
            # Steal needs someone who could actually steal (live 2026-07-15
            # fix): candidates persist through the steal window and judged
            # players are filtered, so with every rostered player already
            # judged the window can never record anything — it burned five
            # silent seconds and re-adjudicated an empty set. Solo tables
            # therefore never steal; multiplayer steals only while an
            # unjudged player exists.
            #
            # HOTFIX-009 W5: read the stealer pool by LIVE VOICEPRINT, not
            # by roster name. Only a player currently holding a diarization
            # label can be heard, so only such a player can answer or steal.
            # The 05:32:11 solo steal window armed because the roster carried
            # the real ghost shape — "Rummy" mis-captured at 05:29:30, then
            # NATO-corrected to "Rami": bind_speaker rebinds S1 to "Rami" and
            # NULLS "Rummy"'s label, leaving two rostered names but one
            # hearable person. Counting by name read stealers_exist True on a
            # table of one. Counting distinct non-null labels collapses the
            # ghost (label-less "Rummy" cannot produce audio) and a set folds
            # any pathological shared-label duplicate to one. A table of one
            # hearable person has no one to steal, in ANY pacing — the read
            # that fixes the spoken player count fixes the steal window.
            hearable_labels = {
                st.get("speaker_label")
                for st in self.sk.players.values()
                if st.get("speaker_label")
            }
            judged_labels = {
                (self.sk.players.get(name) or {}).get("speaker_label")
                for name in self.sk.players
                if name in self._judged_keys
            }
            stealers_exist = any(
                lbl not in judged_labels for lbl in hearable_labels
            )
            # A roster of one hearable person disarms the steal window
            # independent of pacing — the W4 relaxed gate and this gate are
            # siblings on the same steal expression, not alternatives.
            table_has_stealer_pool = len(hearable_labels) > 1
            # HOTFIX-009 W4: relaxed pacing arms no steal clock. The
            # 05:32:11 five-second steal window fired on a relaxed solo
            # table ("You asked for relaxed, and I tossed a five-second
            # steal clock at you anyway"). Relaxed reads the same steal
            # gate every other pacing does — it just never passes it: the
            # missed beat falls through to the ordinary reveal.
            steal_possible = (
                missed and ordered and steal_allowed
                and stealers_exist and table_has_stealer_pool
                and not self.game_over
                and self.sk.pacing != "relaxed"
            )
            if steal_possible and question_revealed:
                # N8: every steal precondition is met EXCEPT the one that
                # matters. Fall through to the reveal beat (which the
                # organic-preempt below keeps from repeating her) and log
                # the refusal loudly.
                logger.error(
                    "LILY_STEAL | REFUSED_REVEALED | session=%s q=%d "
                    "question_id=%s — the answer is already on the air; a "
                    "revealed question has nothing left to steal (N8)",
                    self.sk.session_id, self.sk.question_number,
                    question.get("id"),
                )
            elif steal_possible:
                # Missed question opens a 5-second steal window.
                self._stinger(correct=False)
                self._adjudicating = False
                self.sk.adjudicating = False
                self.open_window(
                    duration=lily_config.steal_window_seconds(), steal=True
                )
                # REFACTOR W2a: the steal opener is a DETERMINISTIC sheet
                # (direct_say), not an LLM composite — a fixed announcement so
                # adjudicate never ends in instructed_reply.
                self.gated_say(
                    None,
                    "steal_window",
                    "Nobody landed it. Announce a five-second steal "
                    "window — quick and hot — anyone who hasn't "
                    "answered can grab it.",
                    source="adjudicate",
                    text="Nobody landed it — five-second steal window! "
                    "Anyone who hasn't answered, grab it now.",
                )
                return

            # ── N12: THE QUESTION TRANSITION OPENS HERE ───────────────────
            # Everything from this line to the delivery of question N+1 is
            # ONE event: reveal, verdict, next-delivery, in that order,
            # under ONE owner. In lily-D99BE7 it was two: inside one beat
            # she said "Chris got it in right on time with Russia! That's a
            # point for Chris." AND "No points on that one — the answer was
            # Russia!" over a single committed row (q_8294, Chris,
            # "Russia.", correct, 1 point), and question FOUR was delivered
            # by one lane while the other was still revealing three —
            # Rhonda, aloud: "She's getting confused. Like she jumped from
            # a question to question and a third question." The repair
            # doubled too, because the loop it apologised for was itself
            # two lanes.
            #
            # The claim key is the mechanism (the same SpeechActRegistry
            # every other idempotent act uses): the second lane loses the
            # claim and never narrates.
            transition_qnum = self.sk.question_number
            transition_owner = f"transition_{uuid.uuid4().hex}"
            # HOTFIX-008 Z2c (lily-938EFF): recovery re-entry over a
            # transition whose narration ALREADY aired in full. The first
            # adjudication opened this transition and journaled
            # reveal+verdict (organic verdict, confirmed on the air) but
            # died before consuming the question, so every forced re-entry
            # found "already owned and narrated", refused, and abandoned
            # the beat — which could never finish on its own (its
            # next_delivery needed supply; supply recovery needed idle;
            # idle needed this transition closed). Narration-complete is
            # NOT the lily-1C53C6 reclaim case (there, nothing aired) and
            # must never re-narrate: RESUME the open transition instead
            # and run only the bookkeeping below — burn, consume, then
            # arm N+1 or release on empty supply.
            resumed_complete = (
                reclaim_transition
                and getattr(self, "_open_transition_qnum", None)
                == transition_qnum
                and self.transition_narration_complete(transition_qnum)
            )
            # HOTFIX-009 W6: the narration-PARTIAL sibling of Z2c's resume.
            # A first adjudication that dies between the reveal journal and
            # the verdict journal leaves stages=[reveal] with no air-proof,
            # so recovery re-entry fell through to the 1C53C6 reclaim and
            # RE-NARRATED the whole beat — re-revealing an answer that was
            # already on the air. Burn is the one-way "answer went to air"
            # state every other seam gates on (arm, draw, steal,
            # reconnect); the transition owner now gates on it too: a
            # BURNED question resumes as bookkeeping, never re-narrates.
            # An unburned dead journal keeps the reclaim — there, nothing
            # aired and re-narration is the cure.
            resumed_burned = (
                not resumed_complete
                and reclaim_transition
                and getattr(self, "_open_transition_qnum", None)
                == transition_qnum
                and self._is_burned(question)
            )
            if resumed_burned:
                logger.warning(
                    "LILY_TRANSITION | RESUMED_BURNED | session=%s q=%d "
                    "stages=%s — recovery re-entered a partial transition "
                    "for a question whose answer already went to air; "
                    "refusing re-narration, journaling the missing stages "
                    "as already-on-air and running bookkeeping only "
                    "(HOTFIX-009 W6)",
                    self.sk.session_id, transition_qnum,
                    ",".join(self.transition_stages(transition_qnum)),
                )
                # Complete the journal so the release seams (Z2c) read the
                # beat as narration-complete: the reveal content is on the
                # air even though no journaled stage can prove it. The
                # narration detail is the air-proof _transition_reached_air
                # keys on.
                for _stage in ("reveal", "verdict"):
                    if not self.transition_narrated(transition_qnum, _stage):
                        self.journal_transition(
                            transition_qnum, _stage,
                            detail={
                                "source": "already_on_air",
                                "narration": "(aired before recovery; "
                                "journaled at resume — HOTFIX-009 W6)",
                            },
                        )
                resumed_complete = True
            if resumed_complete:
                logger.warning(
                    "LILY_TRANSITION | RESUMED_COMPLETE | session=%s q=%d "
                    "— recovery resumes a fully-narrated transition; "
                    "nothing re-airs, only bookkeeping runs "
                    "(HOTFIX-008 Z2c)",
                    self.sk.session_id, transition_qnum,
                )
            elif not self.open_question_transition(
                transition_qnum, owner=transition_owner, source="adjudicate",
                reclaim_unaired=reclaim_transition,
            ):
                return

            if resumed_complete:
                # Nothing to narrate and nothing to journal: reveal and
                # verdict already sit on the journal with the verdict
                # provably on the air. Re-publishing here would also lie
                # — this re-entry recomputes winner=None because every
                # candidate is already judged, while the committed
                # scores went out with the original beat. Only the
                # bookkeeping below (burn, consume, arm or release)
                # remains.
                verdict_spoken_organically = True
            else:
                # Reveal — stinger is the ruling; packet fires on TTS playback.
                self._stinger(correct=winner_candidate is not None)
                self.journal_transition(
                    transition_qnum, "reveal", owner=transition_owner,
                    detail={
                        "answer": str(question.get("canonical_answer", "")),
                        "correct": winner_candidate is not None,
                        "winner": winner,
                        "question_id": bound_question_id,
                    },
                )
                # T4 (PATCH-001): VERDICT-FIRST. The measured live cost of the
                # single long reveal turn was 11–12s from commit to a spoken
                # verdict — long enough that players re-answered ("Saturn"
                # twice, "Kama Sutra" ×15). The verdict word now airs as its
                # own SHORT turn dispatched immediately at commit (budget:
                # dispatch within ~1.5s of commit, logged below; flourish and
                # standings follow as a separate turn). The organic-preempt
                # guard stands: a conversational turn that already performed
                # the verdict makes the beat silently done.
                verdict_commit_ts = time.monotonic()
                verdict_qnum = self.sk.question_number
                verdict_ends_round = (
                    verdict_qnum % self.sk.questions_per_round == 0
                    or self.sk.round > self.rounds_total
                )
                verdict_key = (
                    f"q_{verdict_qnum}_verdict"
                    if verdict_ends_round
                    else f"q_{verdict_qnum}_reveal"
                )
                verdict_spoken_organically = self._verdict_already_spoken(
                    question, winner_candidate
                )
                if verdict_spoken_organically:
                    if self.say_registry.claim(verdict_key):
                        self.say_registry.confirm(verdict_key)
                    logger.info(
                        "LILY_REVEAL | ORGANIC_PREEMPTED | session=%s q=%d — "
                        "verdict already spoken in her last turn",
                        self.sk.session_id, verdict_qnum,
                    )
                    # N12: the transition's ONE narration is the turn that
                    # already aired — bound here, so any later turn narrating
                    # this same transition is a second narration and loses.
                    self.journal_transition(
                        transition_qnum, "verdict", owner=transition_owner,
                        detail={
                            "key": verdict_key,
                            "narration": getattr(
                                self, "_last_assistant_text", ""
                            ) or "",
                            "source": "organic",
                        },
                    )
                reveal_payload = {
                    "correct": winner_candidate is not None,
                    "winner": winner,
                    # Score truth ON THE WIRE (08-04 screen bug): the beat now
                    # carries the winner's COMMITTED score. The frontend's old
                    # overlay GUESSED the increment and — because commits have
                    # published BEFORE the reveal beat since desync-E — its
                    # "attribute is lagging" check passed trivially and the
                    # chip rolled one point HIGH after every reveal, holding
                    # the wrong score until the next commit. With the real
                    # number on the beat there is nothing to guess.
                    "winner_score": (
                        (self.sk.players.get(winner) or {}).get("score")
                        if winner else None
                    ),
                }
                self._pending_reveal_event = reveal_payload
                self._set_ui_phase("reveal")
                # Score truth (desync WO Sub-agent E): the committed scores go
                # out in the SAME tick as the verdict. The attribute publish
                # (players/scores) is dispatched alongside the reveal metadata
                # — never queued behind its network round-trip (the old
                # sequential awaits held the scoreboard hostage to the
                # metadata call while Lily called the point "safe and sound"
                # over a screen still showing zero). Everything from here to
                # the reveal gated_say below is synchronous for a non-final
                # question, so the publish dispatch, the score commit above,
                # and the verdict speech dispatch share one tick.
                await asyncio.gather(
                    self.publish_metadata(
                        question.get("prompt", ""),
                        reveal={
                            "answer": str(question.get("canonical_answer", "")),
                            "winner": winner,
                            "correct": winner_candidate is not None,
                        },
                        choices=question.get("choices"),
                        eliminated=self.eliminated,
                        # B3: reveal clears the picture — leaving image_url here
                        # kept the prior Q on the glass through verdict / arm N+1
                        # until the next open_window (stale screen).
                        category=question.get("category"),
                    ),
                    self.publish_attributes(),
                )
                # T4 dispatch point: AFTER the score/reveal publishes (desync-E:
                # the committed score reaches the glass before the verdict
                # speaks) but before all remaining bookkeeping — the budget is
                # commit → dispatch within ~1.5s, logged for telemetry.
                if not verdict_spoken_organically:
                    answer_text = str(question.get("canonical_answer", ""))
                    # RULINGS-001 R1: ratified verdict-beat register anchor —
                    # verdict word first, then at most one short flourish.
                    # HOTFIX-006 N9 part 3: the verdict is GENERATED FROM the
                    # committed ledger row — the row that was actually written,
                    # naming the utterance actually bound. The live failure was
                    # narration and ledger describing two different utterances
                    # ("Jupiter was spot on, Rami" over a q_1052 row reading
                    # incorrect, transcript "Go."). On any disagreement the
                    # ledger wins; lily_narrated_verdict_divergence makes a
                    # disagreement loud after the fact.
                    ledger_row = self.sk.ledger_row_for(winner, bound_question_id)
                    if winner_candidate is not None:
                        committed = ""
                        if ledger_row is not None:
                            committed = (
                                " THE COMMITTED ROW you are speaking from: "
                                f"{ledger_row.get('player')} — "
                                f"{str(ledger_row.get('transcript') or '')!r} — "
                                "CORRECT. Those are the words that scored; do "
                                "NOT credit a different utterance or a "
                                "different player."
                            )
                        verdict_instr = (
                            "VERDICT BEAT. The ruling is COMMITTED. "
                            "REGISTER GUIDANCE (vary freely "
                            "within this length and temperature, never "
                            "longer): the verdict word FIRST, then at most one "
                            f"short flourish — 'Correct — {answer_text}!', "
                            f"point to {winner or 'the table'}. No trivia "
                            "color, no next question — those come in your next "
                            "turn." + committed
                        )
                    else:
                        verdict_instr = (
                            "VERDICT BEAT. The ruling is COMMITTED. "
                            "REGISTER GUIDANCE (vary freely "
                            "within this length and temperature, never "
                            "longer): the verdict first, then at most one "
                            f"short line — nobody landed it, it was "
                            f"{answer_text}. No next question — that comes in "
                            "your next turn. NO committed row for this question "
                            "is correct, so do NOT tell anyone their answer was "
                            "right — not as a consolation, not 'you were close', "
                            "not 'that was right but late'. If someone was right "
                            "after the buzzer you will have been given a late-"
                            "answer note; without one, nobody was right."
                        )
                    # HOSTLOOP-001 C6 — THE ANTI-DOUBLE. The instant receipt
                    # already put a word on the air for this ruling seconds
                    # ago, so the composite is TOLD, by name, what the room
                    # has already heard. Without this she rules twice ("Ooh,
                    # no—" … "Nope, no points"), which is the doubling the
                    # clause forbids; with it the receipt is the verdict word
                    # and the composite is the reveal.
                    receipt = self.answer_receipt_aired_for(verdict_qnum)
                    if receipt:
                        verdict_instr += (
                            " NOTE: you ALREADY said "
                            f"{receipt!r} out loud a moment ago, the instant "
                            "the answer landed — the room has heard the "
                            "verdict word. Do NOT open with it again and do "
                            "NOT rule a second time; carry straight on from "
                            "it to the answer and the point."
                        )
                    # REFACTOR W2a: the verdict beat is a DETERMINISTIC sheet
                    # composed from the committed ruling — direct_say via the
                    # text= lane, never the 8-13s instructed_reply composite.
                    # winner_scored is the ledger truth (a correct row was
                    # committed), receipt_aired carries the C6 anti-double.
                    # verdict_instr is retained as the fallback the regen gate
                    # would use only if the sheet came back empty.
                    verdict_sheet = lily_scorekeeper.lily_verdict_sheet(
                        answer=answer_text,
                        winner=winner,
                        winner_scored=winner_candidate is not None,
                        receipt_aired=bool(receipt),
                    )
                    self.gated_say(
                        verdict_key, "verdict", verdict_instr,
                        source="adjudicate_verdict",
                        text=verdict_sheet or None,
                    )
                    # N12: the verdict stage of THIS transition is now spoken
                    # for. Its narration is bound by the first turn that
                    # reaches the say gate performing it (register_transition_
                    # narration); the second one — the contradiction — is
                    # suppressed there.
                    self.journal_transition(
                        transition_qnum, "verdict", owner=transition_owner,
                        detail={
                            "key": verdict_key,
                            "narration": None,
                            "source": "adjudicate_verdict",
                        },
                    )
                    logger.info(
                        "LILY_VERDICT | COMMIT_TO_DISPATCH_MS | session=%s "
                        "q=%d ms=%.0f",
                        self.sk.session_id, verdict_qnum,
                        (time.monotonic() - verdict_commit_ts) * 1000.0,
                    )
            # Burn the revealed question (WS-4): its canonical answer is
            # now going to air (winner confirmation OR a timeout Lily
            # resolves by speaking the answer with nobody scoring). A
            # burned question can never re-arm and is excluded from every
            # future draw, so an echo of the just-revealed answer has no
            # live window to land in — and the idempotency key on
            # apply_score_event backstops any award that still fires.
            # (N8: an ORGANIC reveal already burned it above — burning is
            # one-way, so the guard just keeps the log and the persistence
            # write to one per question.)
            if not self._is_burned(question):
                self._burn_question(question, reason="revealed")
            # Consume the question; round/phase bookkeeping. Capture the
            # question/round numbers NOW — arm_next_question() below
            # advances both, and the say-gate keys must name the question
            # being revealed, not the one being armed.
            self.armed_question = None
            revealed_qnum = self.sk.question_number
            revealed_round = self.sk.round
            round_over = (
                self.sk.question_number % self.sk.questions_per_round == 0
            )
            was_final = self.sk.round > self.rounds_total
            if was_final:
                await self.finish_game()
                reveal_instr = self._reveal_instructions(
                    question, winner, winner_candidate, judge_reason,
                    points, final=True,
                )
            else:
                reveal_instr = self._reveal_instructions(
                    question, winner, winner_candidate, judge_reason,
                    points, round_over=round_over,
                )
                if round_over:
                    self._set_ui_phase("scores")
                if not self.arm_next_question():
                    # Nothing prefetched yet (live 2026-07-15: the stalled
                    # session re-asked the revealed question "for the
                    # official record"). Make the gap honest in the state
                    # block and clear the consumed question so nothing
                    # tells her to perform it again; the idle watchdog and
                    # prefetch auto-advance own recovery.
                    self.sk.current_question = None
                    self.sk.set_status_note(
                        "the next question is still being written — vamp "
                        "warmly for a beat; do NOT re-ask the last "
                        "question and do NOT invent one"
                    )
                    # HOTFIX-008 Z2c (lily-938EFF): with the narration on
                    # the air and nothing to deliver, the transition is
                    # waiting only on supply — held open it deadlocks the
                    # game (supply recovery needs idle; idle needs this
                    # released). Close the beat at source. When the
                    # verdict is still airing (non-organic dispatch just
                    # above) this returns False and the release instead
                    # happens at the verdict's playout completion
                    # (post_reveal seam) or on the recovery resume.
                    self.release_completed_transition(
                        transition_qnum, reason="supply_empty_at_arm"
                    )
            # Checkpoint only after the consumed question has either been
            # replaced by N+1 or explicitly cleared. A reconnect must never
            # resurrect a question whose result was already committed.
            if self.supabase is not None:
                asyncio.ensure_future(
                    lily_persistence.lily_checkpoint(self.supabase, self.sk)
                )
            # Gated reveal dispatch: a normal verdict claims q_{N}_reveal.
            # At a round/final boundary the short ruling instead claims
            # q_{N}_verdict, while the following transition owns
            # round_{N}_scores/finale. This keeps their playout completion
            # identities separate.
            # T4 (PATCH-001): the FLOURISH turn — reveal color, standings,
            # the bridge to N+1 — as a SEPARATE beat that never restates
            # the just-announced verdict. Round-closing and finale beats keep
            # their own keys, and only the scores beat releases N+1.
            # The verdict beat already contains the one allowed reaction.
            # A normal mid-round question needs no second acknowledgment;
            # only round standings and the finale require another turn.
            act: str | None = None
            flourish_key: str | None = None
            if was_final:
                flourish_key, act = "finale", "reveal_finale"
            elif round_over:
                flourish_key, act = (
                    f"round_{revealed_round}_scores", "reveal_scores"
                )
            elif verdict_spoken_organically:
                act = None
            if act is not None:
                # REFACTOR W2a: the flourish is a DETERMINISTIC standings /
                # finale composite from the ledger (direct_say) — adjudicate
                # ends in a template, never instructed_reply. Standings-only by
                # construction, so it cannot restate the verdict beat (that is
                # what deletes the N12 double-narration class). reveal_instr is
                # retained only as the empty-sheet fallback.
                _scores = self.sk.ledger_scores()
                _leaders = (
                    [n for n, s in _scores.items() if s == max(_scores.values())]
                    if _scores else []
                )
                scores_sheet = lily_scorekeeper.lily_scores_sheet(
                    ledger_scores=_scores,
                    ledger_streaks=self.sk.ledger_streaks(),
                    final=was_final,
                    winner=_leaders[0] if len(_leaders) == 1 else None,
                )
                self.gated_say(
                    flourish_key,
                    act,
                    reveal_instr
                    + "\n\nThe verdict word was JUST announced in your "
                    "previous beat — do NOT restate correct/incorrect and "
                    "do NOT re-award the point; go straight to the color "
                    "and onward.",
                    source="adjudicate",
                    text=scores_sheet or None,
                )
        except Exception:
            # Adjudication runs inside fire-and-forget timer tasks — an
            # unlogged exception here is a silently wedged game (the
            # 2026-07-15 stall class). Log loudly; the idle watchdog
            # recovers the pipeline.
            logger.exception(
                "LILY_ADJUDICATE | CRASHED | session=%s q=%d",
                self.sk.session_id, self.sk.question_number,
            )
        finally:
            self._adjudicating = False
            self.sk.adjudicating = False

    # -- N9: late-but-correct is a DEFINED outcome ---------------------------


    def record_group_fact(self, player_name: str, fact: str) -> None:
        """Persist one lily_group_facts row under the CURRENT (resolved)
        group id — lobby facts via the lily_note_fact tool, best-wrong-answer
        callouts via send_event. Deduped per session, fire-and-forget; a
        later group-id upgrade re-keys this session's rows."""
        fact = (fact or "").strip()
        name = (player_name or "").strip()
        if (
            not fact
            or self.supabase is None
            or not self.identity_persistence_allowed()
        ):
            return
        key = (name.lower(), fact.lower())
        if key in self._group_facts_written:
            return
        self._group_facts_written.add(key)
        logger.info(
            "LILY_MEMORY | GROUP_FACT | session=%s group=%s player=%s fact=%r",
            self.sk.session_id, self.group_id, name or None, fact[:80],
        )
        asyncio.ensure_future(lily_persistence.lily_write_group_fact(
            self.supabase, self.group_id, name or None, fact,
            self.sk.session_id,
        ))

    # -- group preferences (group prefs WO) -----------------------------------

    def set_pacing(self, pacing: str, source: str = "unspecified") -> bool:
        """Deterministic pacing flip (spoken command layer or the
        lily_set_pacing tool). Sets the scorekeeper's sticky flag, records
        the choice in the opaque prefs dict, and persists the WHOLE dict —
        it is this table's 'usual' next time. Returns False on an invalid
        value (nothing changes)."""
        if pacing not in ("timed", "relaxed"):
            return False
        changed = pacing != self.sk.pacing
        self.sk.set_pacing(pacing)
        self.prefs = dict(self.prefs or {})
        self.prefs["pacing"] = pacing
        # A live this-session write — the confirmation beat may now truthfully
        # say the table chose this pacing this session (MEDIUM-2 provenance).
        self._pacing_stated_this_session = True
        logger.info(
            "LILY_PREFS | PACING | session=%s group=%s pacing=%s source=%s "
            "changed=%s",
            self.sk.session_id, self.group_id, pacing, source, changed,
        )
        self.persist_prefs()
        if changed:
            # Seam: the `pacing` participant attribute updates immediately.
            self.publish_attributes_nowait()
        return True

    def persist_prefs(self) -> None:
        """Persist the whole opaque prefs dict on every preference change
        (lily_group_prefs whole-dict upsert under the CURRENT group id).
        Fire-and-forget; a later group-id upgrade re-keys the row."""
        if (
            self.supabase is None
            or not self.prefs
            or not self.identity_persistence_allowed()
        ):
            return
        asyncio.ensure_future(lily_persistence.lily_write_group_prefs(
            self.supabase, self.group_id, dict(self.prefs)
        ))

    def apply_prefs_at_game_start(self) -> None:
        """The stored 'usual' becomes live flags at game start — silently
        (the ask-once flow promised a yes/'the usual' needs no ceremony).
        Only the keys THIS feature owns are applied here (pacing);
        unknown keys (round_format / media_mode — other features') pass
        through untouched and get applied by their own features
        post-merge. A pacing already chosen out loud this session wrote
        itself into the prefs dict, so this read is always the latest
        word."""
        pacing = (self.prefs or {}).get("pacing")
        if pacing in ("timed", "relaxed") and pacing != self.sk.pacing:
            self.sk.set_pacing(pacing)
            logger.info(
                "LILY_PREFS | APPLIED | session=%s pacing=%s (the usual, "
                "at game start)", self.sk.session_id, pacing,
            )

    def _maybe_retry_enrollment(self, player: str | None) -> None:
        """WS-8: re-fire enrollment for a bound player still below the
        ~5-word floor. Bound off scorekeeper.unenrolled_bound_labels (set
        by the last enroll pass); rate-limited so a talkative but
        not-yet-crossed voice doesn't hammer the GET_SPEAKERS round-trip."""
        if not player:
            return
        unenrolled = getattr(self.sk, "unenrolled_bound_labels", None)
        if not unenrolled:
            return
        label = (self.sk.players.get(player) or {}).get("speaker_label")
        if not label or str(label) not in unenrolled:
            return
        now = time.time()
        if now - self._last_enroll_retry_ts < lily_config.enroll_retry_cooldown_seconds():
            return
        self._last_enroll_retry_ts = now
        logger.info(
            "LILY_ENROLL | RETRY | session=%s player=%s label=%s "
            "(under-threshold, more speech accrued)",
            self.sk.session_id, player, label,
        )
        self.fire_enrollment("under_threshold_retry")



















    async def start_game(self, source: str) -> None:
        if self.game_started:
            return
        blocked = self.start_blocked_reason()
        if blocked:
            logger.info(
                "LILY_STATE | START_DEFERRED | session=%s source=%s "
                "reason=%s",
                self.sk.session_id, source, blocked,
            )
            return
        if self.intake_roundrobin_active():
            # WS-1: every begin_round path (tool, voice, UI, auto-start)
            # converges here — while the intake round-robin is still
            # growing the start DEFERS, so no question can arm against a
            # half-built roster. The per-segment auto-start net retries
            # once names stop landing.
            logger.info(
                "LILY_STATE | START_DEFERRED | session=%s source=%s "
                "reason=intake_active",
                self.sk.session_id, source,
            )
            return
        self.game_started = True
        # CLASS 7 (LIVEFIRE-001) 7a: the round has started — recognition
        # speech is forbidden from here on (no welcome-back/pacing beat may
        # steal q_1's kickoff, the live act=game_start defect).
        self._game_start_committed = True
        # Explicit start won — drop any leftover yes-block / or-offer.
        self.clear_ambiguous_yes_block(reason=f"start_{source}")
        # C7: the start owns the lobby regardless of source (voice, tool,
        # RPC, auto) — no recovery may re-greet past this point.
        self._start_intent_heard = True
        logger.info("LILY_STATE | GAME_START | session=%s source=%s",
                    self.sk.session_id, source)
        # G1: speculative user-turn runs are dead weight during rounds
        # (every answer changes the state block) — off until the finale.
        self.set_game_live_preemptive(True)
        # Roster is as stable as it gets — resolve the durable group id
        # BEFORE the first question so memories/facts/voiceprints key on it
        # (and reload memory for a returning table while there's still a
        # greeting moment to use it in).
        try:
            await self.resolve_group_identity(trigger="game_start")
        except Exception as e:
            logger.warning("LILY_MEMORY | GROUP_ID_RESOLVE | failed: %s", e)
        # Group prefs WO: the stored 'usual' becomes live flags HERE —
        # silently (a "play the usual" needed no announcement). Runs after
        # identity resolution so a just-recognized table's prefs apply too.
        self.apply_prefs_at_game_start()
        self.sk.set_phase("round")
        self.start_prefetch()
        self.arm_next_question()
        self.start_idle_watchdog()
        # Hot-path: identity was awaited above (honesty); attribute publish
        # must not gate kickoff speech / host-tool return.
        self.publish_attributes_nowait()
        self._enroll_started = True
        self.fire_enrollment("game_start")
        if source == "host_tool":
            # BUG-2 (double question delivery) authoritative-delivery
            # contract: when the game starts via the lily_begin_round tool,
            # the tool RESULT carries the question payload and the
            # post-tool turn is the SOLE deliverer. Dispatching an
            # instructed reply here as well produced two racing
            # generations both told to ask the question — the observed
            # double delivery. The prompt states the contract; the
            # q_{N}_delivery claim in tts_node enforces it physically.
            return
        instructions = (
            "The table is ready to start. Kick off round one with "
            "energy. If the state block has the next question, set "
            "the round's category and ask it now; if it does not, "
            "banter for a beat — it is on its way."
        )
        if self.memory_block:
            # Live lily-639007 (2026-08-12): the late-recognition beat
            # welcomed the table back at 13:56:33 and this composite
            # welcomed them back AGAIN at 13:56:44 — "if you haven't done
            # one yet" left the decision to the model, which cannot
            # reliably know. Decide it in code: the fired flag is the
            # authority.
            if getattr(self, "_late_recognition_fired", False):
                instructions += (
                    " The welcome-back beat has ALREADY AIRED this session "
                    "— do NOT welcome the table back again, do not repeat "
                    "any recognition or 'last time' material; go straight "
                    "into the game."
                )
            else:
                instructions += (
                    " The [RETURNING TABLE] context shows this table has "
                    "played with you before — one quick welcome-back beat, "
                    "then into the game."
                )
            # Task 4 disclosure ride-along: covers memory that resolved
            # AFTER the greeting (mid-lobby group-id upgrade) — the
            # once-per-session latch inside makes this a no-op when the
            # greeting already carried it.
            instructions += self.memory_disclosure_instruction()
            # Group prefs ask-once ride-along, same latch pattern: if
            # the greeting never met stored prefs (they resolved with a
            # mid-lobby upgrade), this is the last natural moment to
            # offer "the usual, or change anything?" — already-applied
            # flags make a "the usual" answer a pure no-op.
            instructions += self.prefs_offer_instruction()
            # V3 (HOTFIX-010): what's-new no longer rides the pre-utterance
            # greet. For a table recognized at the door (no late-recognition
            # beat fires) this is its post-utterance carrier; the seen-stamp
            # advances after it airs, so a second call is a pure no-op.
            instructions += self.whats_new_instruction()
        # Structural delivery claim (desync WO Sub-agent B): when question
        # one is already armed, the kickoff turn IS its delivery.
        if self.armed_question is not None:
            self.expect_delivery()
        self.gated_say(
            None, "game_start", instructions, source=f"start_{source}"
        )

    def questions_asked_count(self) -> int:
        """CLASS 3 (LIVEFIRE-001): the count of questions actually DELIVERED
        to the table — the player-facing question count. Derived from
        asked_history (the session's delivered mirror), never from the
        armed/supply cursor question_number.

        Live lily-639007: five Greece questions were asked, but q6 was armed,
        burned by a STOP, and never delivered — question_number reached 6 and
        the winner write said "over 6 questions". asked_history drops a burned
        card at release, so a burned/discarded card never inflates this count
        (3b). This is the number every wrapup/winner/session write uses."""
        hist = getattr(self, "asked_history", None)
        return len(hist) if isinstance(hist, list) else 0

    async def finish_game(self) -> None:
        """Finale: event fires AT OR BEFORE the phase=final attribute flip,
        never after (frontend's single confetti trigger)."""
        if self.game_over:
            return
        self.game_over = True
        # G1: the game is no longer live — wrapup banter is quiet-context,
        # so speculative user-turn runs win latency again.
        self.set_game_live_preemptive(False)
        standings = sorted(
            self._players_payload(), key=lambda p: -p["score"]
        )
        # Wrap-up reconciliation (WS-7): standings (ledger-derived) vs the
        # per-player counters — any mismatch is hard-logged inside
        # reconcile_scores before anything persists.
        self.sk.reconcile_scores()
        # Comeback callout: the wager round flipped the leaderboard.
        if (
            standings
            and self.prewager_standings
            and standings[0]["name"] != self.prewager_standings[0]["name"]
        ):
            await self.send_event(
                "biggest_comeback",
                {
                    "player": standings[0]["name"],
                    "detail": "Took the crown on the final wager.",
                    # Contract-note spellings, tolerated as extra fields:
                    "name": standings[0]["name"],
                    "text": "Took the crown on the final wager.",
                },
            )
        if not self.finale_sent:
            self.finale_sent = True
            await self.send_event("finale", {"standings": standings})
        self.sk.set_phase("wrapup")
        self.ui_phase = "final"
        await self.publish_attributes()
        # B3: finale must not leave the last picture on the glass.
        await self.publish_metadata("")
        if self.supabase is not None and self.identity_persistence_allowed():
            asyncio.ensure_future(lily_persistence.lily_checkpoint(
                self.supabase, self.sk, final_standings=standings,
            ))
            # Session memory — idempotent (session_id upsert), so the
            # shutdown callback writing again is safe.
            asyncio.ensure_future(lily_memory.lily_write_session_memory(
                self.supabase, self.group_id, self.sk.session_id,
                # CLASS 3 (LIVEFIRE-001): delivered count, not the armed
                # cursor — a burned/never-asked q6 must not read as played.
                standings, self.questions_asked_count(), self.highlights,
                round_reached=self.sk.round,
            ))
            # WS-12 report pipeline: report row + assessment on the WRAP-UP
            # beat — the close/shutdown path fires in only 0-22% of sessions
            # fleet-wide, so the clinical-desk fill can never hang off it.
            # Idempotent with the close-path write (upsert on session_id;
            # the assessment fill is pending-guarded).
            asyncio.ensure_future(lily_assessment.lily_wrap_up_report(
                self.supabase, self.sk.session_id, self.group_id,
                transcript=list(self.sk.transcript_buffer),
                game_stats=self.build_game_stats(standings),
            ))

    # -- session report (B3) --------------------------------------------------------

    def build_game_stats(self, standings: list[dict]) -> dict:
        """game_stats jsonb for lily_session_reports: final standings,
        rounds/questions played, per-player answers attempted/correct,
        mode changes, callouts, duration."""
        # HOTFIX-010 V5 fix-loop: per_player is a durable identity map inside
        # the persisted game_stats blob — key it through the same surface-name
        # authority as final_standings so a placeholder's raw diarizer label is
        # never persisted as a player identity here either.
        surface = self._surface_names()
        per_player = {
            surface[name]: {
                "answers_attempted": s.get("answers_attempted", 0),
                "answers_correct": s.get("answers_correct", 0),
            }
            for name, s in self.sk.players.items()
        }
        # Score reconciliation (WS-7): the report records whether the
        # counters and the ledger agreed at report time — the three
        # surfaces (per_player, lily_answers, final_standings) disagreeing
        # pairwise is the live failure this audits.
        mismatches = self.sk.reconcile_scores()
        # WS-11: durable acoustic-lane health, so a zero-trajectory-row
        # session is self-explaining (offline vs running-without-persistence)
        # from the session report alone. Absent pipeline reads as a
        # deliberate offline state (no key / never constructed).
        pipeline = getattr(self, "audeering_pipeline", None)
        acoustic_lane = (
            pipeline.lane_health()
            if pipeline is not None
            else {"breaker_open": True, "reason": "pipeline_absent"}
        )
        return {
            "final_standings": standings,
            "rounds_played": self.sk.round,
            # CLASS 3 (LIVEFIRE-001): delivered count from asked_history, not
            # the armed/supply cursor (burned/discarded never increment it).
            "questions_played": self.questions_asked_count(),
            "per_player": per_player,
            "mode_changes": list(self.sk.mode_changes),
            "callouts": list(self.highlights),
            "score_reconciliation": {
                "ok": not mismatches,
                "mismatches": mismatches,
                "ledger_entries": len(self.sk.score_ledger),
            },
            "duration_s": round(time.time() - self.session_started_at, 1),
            "acoustic_lane": acoustic_lane,
        }

    # -- standing picture arsenal (PATCH-003 binding additions A/B/C) --------------



    _ARSENAL_PARTITION_INTENSITY = {
        "adult_suggestive": "suggestive",
        "adult_explicit": "explicit",
    }









    def set_delivery_pace(self, level: str) -> bool:
        """PATCH-003 P7: apply a delivery-rate request. Sets the session
        field, applies the TTS speed where the voice supports it, and
        returns True if accepted. Text-layer compensation (shorter
        sentences + more pause) rides the state block regardless — so
        'slower' always takes effect on both voices, even if the voice
        can't honor the speed change."""
        value = (level or "").strip().lower()
        if value not in ("normal", "slow"):
            return False
        self.sk.delivery_pace = value
        tts = getattr(self, "tts", None)
        applied_to_voice = False
        set_pace = getattr(tts, "set_pace", None)
        if callable(set_pace):
            try:
                applied_to_voice = bool(set_pace(value))
            except Exception as e:
                logger.warning("LILY_PACE | tts set_pace failed: %s", e)
        logger.info(
            "LILY_PACE | session=%s pace=%s tts_rate_applied=%s",
            self.sk.session_id, value, applied_to_voice,
        )
        return True

    def record_question_asked(self, *, reason: str) -> bool:
        """Write the DURABLE burn row — once the question has actually gone
        to air. Idempotent per question.

        `lily_asked_history` is the group's permanent no-repeat ledger: a
        row there means "this table has played this question". Writing it at
        ARM made that a lie in the one direction that costs content — a
        question drawn and then never delivered was spent forever. The
        picture arsenal makes the cost concrete: entries are generated,
        gated and paid for, and `lily-2C489B` burned one to serve nobody."""
        armed = getattr(self, "armed_question", None)
        supabase = getattr(self, "supabase", None)
        if armed is None or supabase is None:
            return False
        qnum = self.sk.question_number
        if getattr(self, "_durable_asked_qnum", None) == qnum:
            return False
        self._durable_asked_qnum = qnum
        asyncio.ensure_future(lily_bank.lily_record_asked(
            supabase, getattr(self, "group_id", None), dict(armed),
            self.sk.session_id,
        ))
        logger.info(
            "LILY_BURN | RECORDED | session=%s q=%d id=%s reason=%s",
            self.sk.session_id, qnum, armed.get("id"), reason,
        )
        return True











    def _score_authority_line(self) -> str | None:
        """HOTFIX-005 X1 (LEAD): the authoritative per-player score straight
        off the committed ledger, as a hard read-only state field. The live
        defect narrated 7->6->9->12->13 against a true 9 — the model carried a
        score forward from conversation. The glass already projects the same
        ledger_scores(), so state-block and glass agree by construction."""
        try:
            scores = self.sk.ledger_scores()
        except Exception:
            return None
        if not scores:
            return None
        # HOTFIX-010 V5: a placeholder anchor scores internally (its history
        # migrates to a real name on binding) but its raw label is never
        # aired — omit it from the recited detail.
        try:
            real = set(self.sk.real_player_names())
        except Exception:
            real = set(scores)
        pairs = ", ".join(f"{n} {v}" for n, v in scores.items() if n in real)
        if not pairs:
            return None
        return (
            "SCORES — AUTHORITATIVE, READ-ONLY (committed ledger, the ONLY "
            f"score truth): {pairs}. NEVER compute, add up, infer, or carry a "
            "score forward from the conversation; any score you speak must be "
            "EXACTLY the number here. Unsure? Read this field — do not guess."
        )

    def _roster_authority_line(self) -> str | None:
        """HOTFIX-006 N13: the authoritative ROSTER COUNT as a hard
        read-only state field — the same shape as the score field above,
        because it is the same disease. Live: "Whenever you four..."
        spoken to a table of THREE, in the same breath that correctly
        named Rami, Rhonda and Chris. She held the names and still
        GENERATED the number. A count of people is state; state is read."""
        try:
            count = self.sk.roster_size()
            names = self.sk.real_player_names()
        except Exception:
            return None
        if count < 1:
            return None
        word = _ROSTER_COUNT_WORDS.get(count, str(count))
        # HOTFIX-010 V5: placeholders count toward the head count but are
        # never recited — a raw speaker-label anchor must not reach the air.
        named = f" — {', '.join(names)}" if names else ""
        return (
            "ROSTER — AUTHORITATIVE, READ-ONLY (the enrolled table, the "
            f"ONLY roster truth): {count} player"
            f"{'' if count == 1 else 's'}{named}. NEVER "
            "compute, estimate, or carry a player count forward from the "
            "conversation; any count of people you speak ('you "
            f"{word}', 'the {word} of you', '{word} players') must be "
            f"EXACTLY {count}. Unsure? Read this field — do not guess."
        )

    def build_state_block(self, *, now: float | None = None) -> str:
        """Full render — historical shape (sk block, temporal line, extras)."""
        block = self.sk.build_state_block()
        extra = [
            lily_temporal_context(
                getattr(self, "session_started_at", time.time()),
                now=now,
            )
        ] + self._state_extra_lines(now=now)
        if extra:
            block += "\n" + "\n".join(extra)
        return block

    def build_state_block_split(
        self, *, now: float | None = None
    ) -> tuple[str, str]:
        """(stable, volatile) for the P2 volatile-tail split.

        stable  — everything the preemptive equivalence check may see:
                  changes only when the game genuinely moves.
        volatile — the per-generation tail (clock/session-age line, answer
                  window, live candidates): honest but changing on nearly
                  every in-game turn, which is exactly what forced
                  preemptive OFF for the whole live game (G1). Injected
                  per-generation only, so it can never invalidate a
                  speculative run."""
        stable = self.sk.build_state_block_stable()
        extras = self._state_extra_lines(now=now)
        if extras:
            stable += "\n" + "\n".join(extras)
        volatile = "\n".join(
            [
                lily_temporal_context(
                    getattr(self, "session_started_at", time.time()),
                    now=now,
                )
            ]
            + self.sk.volatile_state_lines()
        )
        return stable, volatile





    def _no_repeat_exclusion(self) -> tuple[set, set]:
        """The within-session no-repeat guard (WS-4): the id and
        text-hash sets a fresh draw must avoid — the group's served
        asked_history, this session's already-drawn set, AND every
        revealed/burned question. Returned as (ids, hashes)."""
        ids = (
            lily_bank.lily_history_question_ids(self.asked_history)
            | getattr(self, "_drawn_ids", set())
            | getattr(self, "_burned_question_ids", set())
        )
        hashes = (
            lily_bank.lily_history_hashes(self.asked_history)
            | getattr(self, "_drawn_hashes", set())
            | getattr(self, "_burned_question_hashes", set())
        )
        return ids, hashes


    def on_answer_leak(self) -> None:
        """Leak-filter hit while a question is armed/prefetched: its
        answer may have gone out on air, so the question is dead. Burn
        every question currently in flight (armed and prefetched — the
        filter cannot attribute the leaked fragment to one of them),
        then pull replacements through the existing bank/prefetch path."""
        burned = False
        if self.armed_question is not None:
            self._burn_question(self.armed_question, reason="answer_leak")
            if self._window_timer and not self._window_timer.done():
                self._window_timer.cancel()
            self.sk.close_answer_window()
            self._stop_bed()
            self.armed_question = None
            self.sk.current_question = None
            burned = True
        if self.next_question is not None:
            self._burn_question(self.next_question, reason="answer_leak")
            self.next_question = None
            burned = True
        if getattr(self, "_next_question_reserve", None) is not None:
            # Depth-2: the leak filter cannot attribute the fragment, so the
            # reserve is in-flight and dead too — burn it with the rest.
            self._burn_question(self._next_question_reserve, reason="answer_leak")
            self._next_question_reserve = None
            self._next_question_reserve_mode = None
            burned = True
        if not burned:
            return
        asyncio.ensure_future(self.publish_metadata(""))
        self.publish_attributes_nowait()
        if self.game_started and not self.game_over:
            # Replacement via the existing supply path (arm_next_question
            # falls through to start_prefetch when nothing is prefetched;
            # the prefetch auto-advance nudge delivers it when it lands).
            self.arm_next_question()

    # -- acoustic pipeline: child-signal veto + trajectory rows -----------------------

    def on_child_signal(self, event: dict) -> None:
        """SAFETY-CRITICAL adult-mode VETO (WO-LILY-AUDEERING-001 Task 4).

        Veto-only, BOTH tiers (high_halt and borderline_step_up): the
        child signal can EXIT or BLOCK adult mode, NEVER authorize it —
        whole-room verbal consensus remains necessary and is no longer
        sufficient. The module estimates how the speaker SOUNDS, not
        necessarily the actual attributes of the speaker (age MAE ±8.46yr).

        Adult mode ACTIVE + ladder trips -> exit through the SAME sticky-flag
        path as the spoken "back to normal" command: sk.set_mode("general")
        + attribute publish + deterministic revert flow. Instant, in-character
        line, no explanation of the mechanism, general category next.
        """
        if self.sk.mode != "adult":
            return
        # NO override exists for an active child signal — architect mode
        # included (RESTORED 2026-07-16; a concurrent rewrite added an
        # architect bypass here, letting an env flag disregard a detected
        # young-voice signal while adult content played. The invariant is
        # absolute: the signal can EXIT or BLOCK adult mode, and nothing
        # may authorize past it).
        logger.warning(
            "LILY_AUDEERING_VETO | ADULT_MODE_EXIT | session=%s tier=%s %s",
            self.sk.session_id, event.get("tier"),
            lily_audeering_consumers.PERCEIVED_FRAMING,
        )
        self.sk.set_mode("general")  # sticky flag flips instantly, in code
        getattr(self, "exit_adult_vocal", lambda: None)()  # restore the general vocal node
        # D: same flush as every other mode switch — the armed adult
        # question is dead and the general deck re-draws immediately.
        self.flush_for_mode_switch(source="child_signal")
        self.publish_attributes_nowait()
        if self.session is not None:
            self.gated_say(
                None,
                "mode_revert",
                "Adult mode is now OFF — committed, in code. Switch back "
                "to the regular deck instantly with a light, in-character "
                "pivot line. Do NOT explain why, do NOT mention any "
                "system, audio, detection, or safety mechanism — just "
                "change gears like a host reading the room. The general "
                "deck is re-drawing; ask the next question when it lands "
                "in the state block — never continue the adult one.",
                source="child_signal",
            )

    # -- adult-mode vocal swap (owner directive 2026-08-06) -------------------
    #
    # Both general and adult vocal lanes now use Grok 4.5. The hook remains
    # for an explicit adult model/effort override, while the adult prompt
    # layer still supplies the register.

    def enter_adult_vocal(self) -> bool:
        """Apply an optional adult Grok model/effort override."""
        agent = getattr(self, "agent", None)
        update = getattr(agent, "update_options", None)
        key = lily_config.xai_api_key()
        if update is None or not key:
            logger.error(
                "LILY_ADULT_VOCAL | SWAP_UNAVAILABLE | session=%s "
                "reason=%s — adult rounds stay on the general Grok node",
                self.sk.session_id,
                "no_xai_key" if update is not None else "no_agent_handle",
            )
            return False
        llm = getattr(self, "_adult_llm", None)
        if llm is None:
            try:
                llm = lily_build_grok_vocal_llm(
                    model=lily_config.adult_vocal_model(),
                    api_key=key,
                    effort=lily_config.adult_vocal_effort(),
                    conversation_id=self.grok_conversation_id,
                )
            except Exception as e:
                logger.error(
                    "LILY_ADULT_VOCAL | SWAP_UNAVAILABLE | session=%s "
                    "reason=llm_construction_failed error=%s",
                    self.sk.session_id, e,
                )
                return False
            self._adult_llm = llm
            # Y1c: the freshly built adult transport gets the same per-call
            # cache accounting as the general node.
            wire = getattr(self, "_llm_metrics_wire", None)
            if wire is not None:
                try:
                    wire(llm)
                except Exception:
                    pass
        try:
            update(llm=llm)
        except Exception as e:
            logger.error(
                "LILY_ADULT_VOCAL | SWAP_FAILED | session=%s error=%s",
                self.sk.session_id, e,
            )
            return False
        logger.warning(
            "LILY_ADULT_VOCAL | SWAPPED_IN | session=%s model=%s effort=%s",
            self.sk.session_id, lily_config.adult_vocal_model(),
            lily_config.adult_vocal_effort(),
        )
        return True

    def exit_adult_vocal(self) -> None:
        """Restore the general Grok vocal configuration on any adult exit —
        idempotent; a session that never swapped is a no-op."""
        agent = getattr(self, "agent", None)
        update = getattr(agent, "update_options", None)
        general = getattr(self, "_general_llm", None)
        if update is None or general is None:
            return
        try:
            update(llm=general)
            logger.warning(
                "LILY_ADULT_VOCAL | SWAPPED_OUT | session=%s — general "
                "vocal node restored", self.sk.session_id,
            )
        except Exception as e:
            logger.error(
                "LILY_ADULT_VOCAL | RESTORE_FAILED | session=%s error=%s",
                self.sk.session_id, e,
            )

    def on_child_gate_lost(self, reason: str) -> None:
        """Mid-session CLOSED->OPEN breaker transition while adult mode is
        active -> automatic exit through the SAME sticky-flag revert path
        as the spoken "back to normal" (WO-DESYNC-A, RESTORED 2026-07-16 —
        a concurrent rewrite demoted this to a log line, leaving the adult
        deck running with the child-signal sensor dead. The sensor and the
        deck deploy as one unit: sensor down means deck down, fail CLOSED,
        mid-session included)."""
        if self.sk.mode != "adult":
            return
        logger.warning(
            "LILY_ADULT_GATE | CHILD_GATE_LOST | session=%s "
            "breaker_reason=%s action=adult_mode_exit",
            self.sk.session_id, reason,
        )
        self.sk.set_mode("general")  # sticky flag flips instantly, in code
        getattr(self, "exit_adult_vocal", lambda: None)()  # restore the general vocal node
        self.flush_for_mode_switch(source="child_gate")
        self.publish_attributes_nowait()
        if self.session is not None:
            self.gated_say(
                None,
                "mode_revert",
                "Adult mode is now OFF — committed, in code. Switch back "
                "to the regular deck instantly with a light, in-character "
                "pivot line. Do NOT explain why, do NOT mention any "
                "system, audio, detection, or safety mechanism. The "
                "general deck is re-drawing; ask the next question when "
                "it lands in the state block.",
                source="child_gate",
            )

    def log_acoustic_trajectory(self) -> None:
        """One lily_acoustic_trajectories row per finalized user turn —
        fire-and-forget (to_thread inside the persistence helper)."""
        self._user_turn_index += 1
        if self.supabase is None:
            return
        snapshot = self.acoustic.latest_snapshot()
        if snapshot is None:
            return
        asyncio.ensure_future(lily_persistence.lily_write_acoustic_trajectory(
            self.supabase, self.sk.session_id, self._user_turn_index, snapshot,
        ))

    # -- binding side-effects --------------------------------------------------------

    def on_speaker_bound(self, speaker_label: str, player_name: str) -> str:
        """Post-bind side effects: pending open-floor award, max_speakers
        bump, bind event, attribute publish."""
        self._last_bind_at = time.time()
        # NAME-STATED RECOGNITION. The moment a name is bound is the moment
        # the room has told us who it is; a table whose file lists that name
        # should not then sit through three and a half minutes of biometric
        # warm-up being called a stranger. Fire-and-forget — recognition is
        # never allowed to block the bind or the turn.
        try:
            asyncio.get_running_loop().create_task(
                self.maybe_recognize_by_stated_name(player_name)
            )
        except RuntimeError:
            pass  # no loop (unit fixtures drive bind_speaker synchronously)
        note = ""
        pending = self._pending_unbound_award
        if pending and pending["speaker_label"] == speaker_label:
            self._pending_unbound_award = None
            # Make-good commit through the single write path (WS-7): the
            # held open-floor point lands with its own cause code and an
            # audit row — the live make-good had no lily_answers row.
            entry = self.sk.apply_score_event(
                player_name,
                cause="make_good",
                correct=True,
                points=pending["points"],
                question_id=pending.get("question_id"),
                question_index=pending.get("question_index"),
                transcript=pending.get("transcript"),
                # N9: the make-good names the same utterance the held
                # award was decided from.
                utterance_id=pending.get("utterance_id"),
            )
            if entry is not None and self.supabase is not None:
                asyncio.ensure_future(lily_persistence.lily_write_score_event(
                    self.supabase, self.sk.session_id, entry,
                ))
            note = f" Their held point ({pending['points']}) is now committed."
        # HOTFIX-005 X8: the Speechmatics plugin has NO in-flight setter for
        # max_speakers (only update_speakers(focus/ignore)). The cap is set
        # at StartRecognition; shrinking it to the real table size requires a
        # full live STT swap via Agent.update_options(stt=...). Wired now but
        # DEFAULT OFF (LILY_STT_ROSTER_RETUNE) — the reconnect is STT-001
        # Q4's to validate. Fires at most once, only to shrink the cap.
        self._maybe_retune_stt_for_roster()
        logger.info(
            "LILY_STT | roster=%d max_speakers=%d",
            self.sk.roster_size(),
            getattr(self, "_stt_max_speakers_applied", 7),
        )
        # Voiceprint enrollment fires on the first binding and every later
        # bind. The write is idempotent and this closes the late-binder gap:
        # a new guest joining after game start still lands under the
        # resolved group id for the next rematch. Speechmatics needs about
        # five words per voice, so game-start and group-upgrade retries remain.
        if not self._enroll_started:
            self._enroll_started = True
            self.fire_enrollment("first_bind")
        else:
            self.fire_enrollment("bind_refresh")
        self.request_device_verification("speaker_bind")
        # Packet kind `player_bind` per the shipped frontend parser
        # (contract note said `bind`; the canonical prmpt_ui parser accepts
        # chip_bind/name_chip/player_bind — drift recorded for Rami).
        # Contract-spelled fields ride along for any contract-faithful client.
        self.send_event_nowait(
            "player_bind",
            {
                "player": {"name": player_name},
                "name": player_name,
                "speaker_label": speaker_label,
            },
        )
        self.publish_attributes_nowait()
        # Safety-net auto-start: if the lobby has settled into a real table
        # (>=2 speakers bound, next question is prefetched, lobby grace
        # window elapsed) and neither Lily nor the UI has kicked off the
        # game, fire start_game so the tiered loop can engage. Without
        # this net the arm/ask/adjudicate pipeline never runs and Lily
        # freestyles bonus points forever.
        self._maybe_auto_start_after_lobby()
        return note

    def _maybe_retune_stt_for_roster(self) -> None:
        """HOTFIX-005 X8: shrink the STT max_speakers cap to the roster-aware
        size (roster+1) at game start, killing the phantom generic labels a
        too-wide cap mints in small/solo tables (the solo [S2] that spoke at
        14:49:49). Fires at most once, only to SHRINK the cap, only when
        explicitly enabled, and only if the rebuild+swap machinery is
        present. Fail-safe: any error leaves the live STT untouched."""
        if getattr(self, "_stt_roster_retuned", False):
            return
        if not lily_config.stt_roster_retune_enabled():
            return
        rebuild = getattr(self, "_stt_rebuild", None)
        agent = getattr(self, "agent", None)
        update = getattr(agent, "update_options", None) if agent else None
        if rebuild is None or not callable(update):
            return
        try:
            roster = int(self.sk.roster_size())
        except Exception:
            return
        if roster < 1:
            return
        target = lily_stt_tuning.lily_max_speakers_for(roster)
        current = int(getattr(self, "_stt_max_speakers_applied", 7))
        if target >= current:
            # The construction fallback already covers this table; a live
            # swap that doesn't shrink the cap buys a reconnect for nothing.
            self._stt_roster_retuned = True
            return
        try:
            new_stt = rebuild(target)
            update(stt=new_stt)
            self.stt = new_stt
            self._stt_max_speakers_applied = target
            self._stt_roster_retuned = True
            logger.warning(
                "LILY_STT | ROSTER_RETUNE | session=%s roster=%d "
                "max_speakers %d -> %d (live STT swap)",
                self.sk.session_id, roster, current, target,
            )
        except Exception:
            logger.exception(
                "LILY_STT | ROSTER_RETUNE_FAILED | session=%s — keeping the "
                "construction-time STT (max_speakers=%d)",
                self.sk.session_id, current,
            )

    # -- auto-start safety net --------------------------------------------------------

    def intake_roundrobin_active(self) -> bool:
        """True while the pre-game name round-robin is still growing: a
        speaker bind landed within the settle window, so more
        introductions are likely in flight (the 22:48 evidence session
        auto-started between Chris's bind and Rhonda's introduction)."""
        if self.game_started or self.game_over:
            return False
        last = getattr(self, "_last_bind_at", None)
        if last is None:
            return False
        return time.time() - last < lily_config.intake_settle_seconds()

    def _maybe_auto_start_after_lobby(self) -> None:
        """Kick off round one when the lobby has clearly settled but no
        one — neither Lily via lily_begin_round nor the UI via
        lily_control.start — has actually started the game. Guards ensure
        we never start while there is only one voice, before the first
        question has been prefetched, before the lobby grace period, or
        while the table is still mid-conversation."""
        if self.game_started or self.game_over:
            return
        blocked = self.start_blocked_reason()
        if blocked:
            logger.info(
                "LILY_STATE | START_DEFERRED | session=%s "
                "source=auto_after_lobby reason=%s",
                self.sk.session_id, blocked,
            )
            return
        if self.intake_roundrobin_active():
            # WS-1: a name just landed — the round-robin is still growing.
            # start_game carries the same gate (it is the choke point for
            # the tool/voice/UI paths); checking here keeps the AUTO_START
            # log truthful and skips scheduling a start that would defer.
            logger.info(
                "LILY_STATE | START_DEFERRED | session=%s "
                "source=auto_after_lobby reason=intake_active",
                self.sk.session_id,
            )
            return
        if self.sk.roster_size() < lily_config.auto_start_min_players():
            return
        if self.next_question is None:
            # Try again once prefetch completes.
            self.start_prefetch()
            return
        grace = lily_config.auto_start_lobby_grace_seconds()
        if time.time() - self.session_started_at < grace:
            return
        # Quiet-after-last-user-turn: ambient banter past the grace window
        # must not flip the game on. note_user_turn stamps monotonic time;
        # fall back to "never quiet enough" when no user turn has landed
        # yet (the greeting alone is not a settled table).
        quiet_needed = lily_config.auto_start_quiet_seconds()
        last_user = getattr(self, "_last_user_turn_at", None)
        if last_user is None:
            logger.info(
                "LILY_STATE | START_DEFERRED | session=%s "
                "source=auto_after_lobby reason=no_user_turn",
                self.sk.session_id,
            )
            return
        quiet_for = time.monotonic() - last_user
        if quiet_for < quiet_needed:
            logger.info(
                "LILY_STATE | START_DEFERRED | session=%s "
                "source=auto_after_lobby reason=lobby_active "
                "quiet_for=%.1fs need=%.1fs",
                self.sk.session_id, quiet_for, quiet_needed,
            )
            return
        if getattr(self.sk, "host_speaking", False):
            logger.info(
                "LILY_STATE | START_DEFERRED | session=%s "
                "source=auto_after_lobby reason=host_speaking",
                self.sk.session_id,
            )
            return
        logger.info(
            "LILY_STATE | AUTO_START | session=%s roster=%d elapsed=%.1fs "
            "quiet_for=%.1fs",
            self.sk.session_id, self.sk.roster_size(),
            time.time() - self.session_started_at, quiet_for,
        )
        asyncio.ensure_future(self.start_game(source="auto_after_lobby"))


# ---------------------------------------------------------------------------
# tts_node speech pipeline (REFACTOR W1b)
#
# tts_node was ~600 lines of sequential surgery — each block a live fix. They
# are extracted here as named, independently-testable SpeechTransform objects
# run in order by run_say_pipeline(). Behavior is byte-preserved: every gate
# calls the same lily_say_gate / lily_scorekeeper / LilyGame helpers with the
# same arguments and the same branch logic, in the same ORDER as the original
# method. Suppression bookkeeping (mark _suppressed_speech_ids, release the
# say_registry owner) is funnelled through SpeechTurn so a new guard cannot
# silently skip it (GUARD_MAP chain F). Each mutating/suppressing stage emits
# one uniform line: LILY_SAY | TRANSFORM | name=<stage> action=replace|suppress
# in addition to its original bespoke log.
# ---------------------------------------------------------------------------


def _lily_silence_frame() -> "rtc.AudioFrame":
    """The 2400-sample silence frame every suppressed turn yields (byte-for-byte
    the frame the inline method used before this refactor)."""
    return rtc.AudioFrame(
        data=b"\x00\x00" * 2400,
        sample_rate=24000,
        num_channels=1,
        samples_per_channel=2400,
    )


class Silence:
    """A pipeline stage returns this to end the turn with silence. `schedule`,
    if set, is a zero-arg callable returning the coroutine the orchestrator
    ensure_futures BEFORE yielding the silence frame (regen / empty retry)."""

    __slots__ = ("reason", "schedule")

    def __init__(self, reason, schedule=None):
        self.reason = reason
        self.schedule = schedule


class SpeechTurn:
    """Mutable per-turn context threaded through the pipeline. `text` is the
    candidate speech (rewritten in place by stages); `raw` is the original
    accumulated text (immutable — the hygiene stage diffs against it).
    `game`/`agent` are the LilyGame and LilyAgent; `speech_id` is pinned once
    (the SpeechHandle ContextVar is stable for the task). `delivery`,
    `n_questions`, `repeat_kind` carry values computed by one stage and read by
    a later one."""

    def __init__(self, text, raw, game, agent, speech_id):
        self.text = text
        self.raw = raw
        self.game = game
        self.agent = agent
        self.speech_id = speech_id
        self.delivery = None
        self.n_questions = 0
        self.repeat_kind = None

    def mark_suppressed(self):
        """Record this speech_id as suppressed so a never-aired turn is not
        journaled as said (guard-map chain D)."""
        if self.speech_id:
            suppressed_ids = getattr(self.game, "_suppressed_speech_ids", None)
            if suppressed_ids is None:
                self.game._suppressed_speech_ids = set()
                suppressed_ids = self.game._suppressed_speech_ids
            suppressed_ids.add(self.speech_id)

    def release_owner_or_pending(self):
        return (
            self.game.say_registry.release_owner(self.speech_id)
            if self.speech_id
            else self.game.say_registry.release_pending()
        )


class SpeechTransform:
    name = "transform"

    def apply(self, turn: "SpeechTurn"):
        raise NotImplementedError


class LeakFilter(SpeechTransform):
    """Say-gate leak filter (BEFORE hygiene): injected state-block context
    echoed into the outbound turn — the sentinel envelope, envelope fragments,
    or bracketed metadata lines — is deterministically stripped and triggers
    the burn protocol for any armed/prefetched question (its answer may have
    gone out on air). A leaked state note (holds scores, never answers) and a
    leaked tool-call JSON carry no answer, so stripping them must not burn."""

    name = "leak_filter"

    def apply(self, turn):
        filtered, leak_reasons = lily_say_gate.lily_filter_leaks(turn.text)
        if leak_reasons:
            logger.warning(
                "LILY_SAY_SUPPRESSED | reason=leak | markers=%s",
                ",".join(sorted(set(leak_reasons))),
            )
            _non_answer_reasons = {"metadata:[state note:", "tool_call"}
            if any(r not in _non_answer_reasons for r in leak_reasons):
                turn.game.on_answer_leak()
        turn.text = filtered
        return turn


class HygieneClean(SpeechTransform):
    """P4 spoken-markdown strip via the say gate (THE choke point for outbound
    speech hygiene). Markdown emphasis, headers, bullets and emoji are removed;
    [bracket] audio tags are load-bearing ElevenLabs v3 controls, preserved
    verbatim. Emoji-only turns strip to "" and fall to the empty-candidate
    retry below. (Not in the operator's stage list — a real block preserved.)"""

    name = "hygiene_clean"

    def apply(self, turn):
        full = lily_say_gate.lily_clean_for_speech(turn.text)
        if full != turn.raw:
            logger.info(
                "LILY_SAY_GATE | stripped %d chars of markdown/emoji/leaks",
                len(turn.raw) - len(full),
            )
        turn.text = full
        return turn


class RevealDeliveryFusionClip(SpeechTransform):
    """CLASS 2 (LIVEFIRE-001) — NO REVEAL/DELIVERY FUSION. While an open
    transition has aired its reveal but not delivered the next question, the
    floor belongs to the reveal; a fused next question in this turn is clipped.
    The real delivery fires on its own after the reveal confirms. (Real block,
    not named in the operator's list.)"""

    name = "reveal_delivery_fusion_clip"

    def apply(self, turn):
        if turn.game.transition_awaiting_delivery():
            _reveal_kept, _delivery_tail = (
                lily_say_gate.lily_clip_delivery_from_reveal(turn.text)
            )
            if _delivery_tail:
                logger.warning(
                    "LILY_SAY_SUPPRESSED | reason=reveal_delivery_fusion | "
                    "session=%s q=%d dropped=%r — reveal turn may not deliver "
                    "the next question; it fires after the reveal confirms",
                    turn.game.sk.session_id,
                    turn.game.sk.question_number, _delivery_tail[:120],
                )
                turn.text = _reveal_kept
        return turn


class ScoreLineGate(SpeechTransform):
    """CLASS 1 (LIVEFIRE-001) — SPOKEN SCORE = LEDGER ONLY. Any sentence
    narrating a total/streak/count is suppressed and the ONE authoritative line
    is re-emitted from the ledger. Suppress-and-reemit, never in-place rewrite.
    Only fires with a live ledger — pre-game greet/intake is untouched."""

    name = "score_line_gate"

    def apply(self, turn):
        try:
            _lscores = turn.game.sk.ledger_scores()
        except Exception:
            _lscores = {}
        if _lscores and any(v for v in _lscores.values()):
            kept_text, _suppressed, _ledger_line = (
                lily_scorekeeper.lily_score_line_gate(
                    turn.text, _lscores, turn.game.sk.ledger_streaks()
                )
            )
            if _suppressed:
                logger.warning(
                    "LILY_SAY_SUPPRESSED | reason=score_divergence | "
                    "session=%s suppressed=%r ledger_line=%r",
                    turn.game.sk.session_id,
                    [s[:80] for s in _suppressed], _ledger_line,
                )
                turn.text = (kept_text + " " + _ledger_line).strip() if kept_text \
                    else _ledger_line
        return turn


class FalseEmptyRewrite(SpeechTransform):
    """A claimed returner / unsettled identity must never be told no recorded
    game / clean slate exists. Rewrite whenever absence is not a settled fact —
    greet/intake included, not only when a returner note is armed."""

    name = "false_empty_rewrite"

    def apply(self, turn):
        if turn.game.must_rewrite_false_empty_claim(turn.text):
            logger.warning(
                "LILY_SAY_GATE | FALSE_CLEAN_SLATE_REWRITTEN | session=%s "
                "probe_out=%s dispute=%s returner_seen=%s",
                turn.game.sk.session_id,
                turn.game.identity_probe_outstanding(),
                bool(getattr(turn.game, "_recognition_dispute", False)),
                bool(getattr(turn.game, "_returner_claim_seen", False)),
            )
            turn.text = lily_say_gate.lily_still_checking_rewrite()
        return turn


class OnScreenClaimRewrite(SpeechTransform):
    """B4: "look at the screen" / "picture is up" only after image_shown
    confirmed the armed URL — not drawn is not on screen."""

    name = "on_screen_claim_rewrite"

    def apply(self, turn):
        if lily_say_gate.lily_false_on_screen_claim(turn.text) and not (
            turn.game.picture_on_glass_confirmed()
        ):
            if turn.game.picture_on_glass_failed():
                logger.warning(
                    "LILY_SAY_GATE | FALSE_ON_SCREEN_REWRITTEN | session=%s "
                    "reason=didnt_land",
                    turn.game.sk.session_id,
                )
                turn.text = lily_say_gate.lily_picture_didnt_land_rewrite()
            else:
                logger.warning(
                    "LILY_SAY_GATE | FALSE_ON_SCREEN_REWRITTEN | session=%s "
                    "reason=pending_confirm",
                    turn.game.sk.session_id,
                )
                turn.text = lily_say_gate.lily_picture_pending_rewrite()
        return turn


class DisputeSycophancyRewrite(SpeechTransform):
    """P0-C: while a recognition dispute needs its why-beat, ban sycophantic
    "you're right" openers — answer the why, don't agree."""

    name = "dispute_sycophancy_rewrite"

    def apply(self, turn):
        if (
            getattr(turn.game, "_recognition_dispute", False)
            and not getattr(turn.game, "_recognition_dispute_why_answered", False)
            and lily_say_gate.lily_mirror_flag(turn.text)
        ):
            logger.warning(
                "LILY_SAY_GATE | DISPUTE_SYCOPHANCY_REWRITTEN | session=%s",
                turn.game.sk.session_id,
            )
            turn.text = (
                "Because my first check looked empty and I treated that as "
                "final instead of still loading — that was wrong on my "
                "protocol. What do you want next — a refresher, or shall "
                "we wait?"
            )
        return turn


class YieldAfterFirstQuestion(SpeechTransform):
    """Asking obligates listening. Physically end conversational turns at the
    first completed question. Authoritative MC deliveries are exempt (options
    legitimately follow their stem); freeform deliveries and verdict-plus-next
    stacks are NOT exempt. Stashes n_questions for the repeat lints."""

    name = "yield_after_first_question"

    def apply(self, turn):
        n_questions = lily_say_gate.lily_stacked_question_flag(turn.text)
        turn.n_questions = n_questions
        armed = getattr(turn.game, "armed_question", None) or {}
        mc_delivery = (
            isinstance(armed.get("choices"), list)
            and bool(armed.get("choices"))
            and turn.game.is_question_delivery_turn(turn.text)
        )
        # CLASS 8 (LIVEFIRE-001) 8b: the yield clips STACKED questions only.
        # A single question with a plain declarative tail ("Got it, Rami?
        # Let's set you up.") is a natural turn, not two competing questions —
        # clipping it cut an 82-char non-question tail after name-bind. Only a
        # SECOND question in the turn (n_questions >= 2) creates the
        # unanswered-obligation the gate exists to end.
        if not mc_delivery and n_questions >= 2:
            clipped, yielded = lily_say_gate.lily_yield_after_first_question(turn.text)
            if yielded:
                logger.warning(
                    "LILY_SAY_GATE | YIELD_AFTER_QUESTION | session=%s "
                    "questions=%d removed_chars=%d",
                    turn.game.sk.session_id, n_questions,
                    len(turn.text) - len(clipped),
                )
                turn.text = clipped
        return turn


class RepeatLints(SpeechTransform):
    """LOG-ONLY telemetry lints (mirror / stacked-question / verbatim-repeat /
    semantic-paraphrase) over turns that actually PLAYED. Never mutates text —
    it computes repeat_kind, which the regen gate below promotes to a
    suppression on a genuine consecutive restatement. (Real block; folds the
    inline lints the operator's list omitted between yield and regen.)"""

    name = "repeat_lints"

    def apply(self, turn):
        mirror_pattern = lily_say_gate.lily_mirror_flag(turn.text)
        if mirror_pattern:
            logger.info(
                "LILY_SAY | MIRROR_FLAG | session=%s pattern=%r",
                turn.game.sk.session_id, mirror_pattern,
            )
        if turn.n_questions > 1:
            logger.info(
                "LILY_SAY | STACKED_QUESTION_FLAG | session=%s count=%d",
                turn.game.sk.session_id, turn.n_questions,
            )
        repeat_kind = lily_say_gate.lily_repeat_flag(
            turn.text, turn.game.sk.agent_turns
        )
        if repeat_kind:
            logger.info(
                "LILY_SAY | REPEAT_FLAG | session=%s kind=%s",
                turn.game.sk.session_id, repeat_kind,
            )
        paraphrase_kind = lily_say_gate.lily_paraphrase_repeat_flag(
            turn.text, turn.game.sk.agent_turns[-3:],
            threshold=lily_config.paraphrase_repeat_threshold(),
        )
        if paraphrase_kind and not repeat_kind:
            logger.info(
                "LILY_SAY | PARAPHRASE_FLAG | session=%s kind=%s",
                turn.game.sk.session_id, paraphrase_kind,
            )
            repeat_kind = repeat_kind or paraphrase_kind
        turn.repeat_kind = repeat_kind
        return turn


class RegenGate(SpeechTransform):
    """Regeneration GATE (WS-3): on a RE-AIR, a verbatim replay of an
    already-aired turn is SUPPRESSED and regenerated once with the fresh-words
    directive. Question deliveries exempt. Bounded to one retry; a stubborn
    repeat that comes back verbatim a second time yields the floor with silence
    rather than airing the third copy (the storm)."""

    name = "regen_gate"

    def apply(self, turn):
        if (
            not getattr(turn.agent, "_reair_regen_pending", False)
            and turn.game.reair_verbatim_should_regenerate(turn.text, turn.repeat_kind)
        ):
            turn.agent._reair_regen_pending = True
            # Guard-map chain D fix (HOTFIX-007): this suppressed turn never
            # airs, so the handle MUST be marked suppressed and its owner
            # released — otherwise it records its never-aired text as said.
            turn.mark_suppressed()
            released = turn.release_owner_or_pending()
            for k in released:
                logger.warning(
                    "LILY_SAY | RELEASED | key=%s | reason=regen_gate", k,
                )
            logger.warning(
                "LILY_REGEN_GATE | verbatim re-air suppressed | session=%s "
                "kind=%s — regenerating fresh",
                turn.game.sk.session_id, turn.repeat_kind,
            )
            return Silence(
                "regen_reair",
                schedule=lambda: turn.agent.session.generate_reply(
                    instructions=_REGEN_REAIR_DIRECTIVE.strip()
                ),
            )
        if (
            getattr(turn.agent, "_reair_regen_pending", False)
            and turn.repeat_kind
            and not turn.game.is_question_delivery_turn(turn.text)
        ):
            # WS-3 tightening: the one regen retry ALSO came back verbatim. The
            # room has already heard this twice — the third copy is the storm.
            # Yield the floor with silence. Question deliveries stay exempt.
            turn.agent._reair_regen_pending = False
            turn.mark_suppressed()
            released = turn.release_owner_or_pending()
            for k in released:
                logger.warning(
                    "LILY_SAY | RELEASED | key=%s | reason=stubborn_repeat", k,
                )
            logger.warning(
                "LILY_SAY_SUPPRESSED | reason=stubborn_repeat | session=%s "
                "kind=%s — regen retry repeated verbatim again; suppressing "
                "the third copy instead of airing the storm",
                turn.game.sk.session_id, turn.repeat_kind,
            )
            return Silence("stubborn_repeat")
        turn.agent._reair_regen_pending = False
        return turn


class EmptyCandidateRetry(SpeechTransform):
    """§11.1: an empty candidate (safety-filter mute, truncation) is a loggable
    event with a retry — never silence. First empty: release any pending
    speech-act claim (so the retry can redeliver), re-arm delivery, retry once.
    Second empty on a delivery: speak the deterministic armed sheet so the
    table still hears the question; otherwise give the turn back to the room.
    (Real block — distinct from FalseEmptyRewrite's clean-slate case.)"""

    name = "empty_candidate_retry"

    def apply(self, turn):
        if len(turn.text) >= 3:
            turn.agent._empty_retry_pending = False
            return turn
        # Say gate: this speech was dispatched but will never play — release
        # the pending claims so the retry can legitimately redeliver the act.
        released = turn.release_owner_or_pending()
        for k in released:
            logger.warning(
                "LILY_SAY | RELEASED | key=%s | reason=empty_candidate "
                "— retry may redeliver", k,
            )
        # Structural delivery retry: a released q_{N}_delivery claim re-arms the
        # one-shot delivery flag so the retry turn re-registers at its dispatch.
        if f"q_{turn.game.sk.question_number}_delivery" in released:
            turn.game.expect_delivery()
        if not turn.agent._empty_retry_pending:
            turn.agent._empty_retry_pending = True
            logger.warning(
                "LILY_EMPTY_CANDIDATE | empty/junk response (%r) — retrying",
                turn.text,
            )
            return Silence(
                "empty_candidate_retry",
                schedule=lambda: turn.agent.session.generate_reply(),
            )
        turn.agent._empty_retry_pending = False
        # Second empty on a question delivery: do not leave dead air. Speak the
        # deterministic armed sheet so the table still hears the question.
        sheet = ""
        try:
            if (
                getattr(turn.game, "game_started", False)
                and getattr(turn.game, "armed_question", None) is not None
                and not turn.game.sk.answer_window_open
            ):
                sheet = (turn.game.rendered_armed_question() or "").strip()
        except Exception:
            sheet = ""
        if sheet:
            logger.error(
                "LILY_EMPTY_CANDIDATE | second empty on delivery — "
                "forcing armed question sheet (%d chars)",
                len(sheet),
            )
            turn.game.expect_delivery()
            turn.text = sheet
            return turn
        logger.error(
            "LILY_EMPTY_CANDIDATE | second consecutive empty response — "
            "giving the turn back to the room"
        )
        return Silence("empty_candidate_giveup")


class BackHoldNarration(SpeechTransform):
    """W8 honest-narration integrity: if this turn narrates a stopped/hold state
    and no hold is active, enter the hold now so the spoken claim is backed by
    state — BEFORE the delivery claim, so the freshly-entered hold binds the
    same-turn / next delivery. State-only; text unchanged. (Real block.)"""

    name = "back_hold_narration"

    def apply(self, turn):
        turn.game.back_hold_narration(turn.text)
        return turn


class DeliveryClaim(SpeechTransform):
    """STRUCTURAL delivery registration: the q_{N}_delivery CLAIM is the
    delivery event; window-open and "delivered" key off it, never off text
    similarity. A staged mid-read RESUME speaks the remaining armed options
    verbatim (before the claim decision). rewrite_strict substitutes the
    deterministic sheet. A duplicate/held turn is made physically silent
    (suppressed at dispatch, no release). Stashes `delivery` for later gates."""

    name = "delivery_claim"

    def apply(self, turn):
        # HOSTLOOP-001 C3c: a staged mid-read RESUME speaks verbatim — the
        # remaining choices are already rendered deterministically and must air
        # exactly as armed, BEFORE the delivery-claim decision.
        _resume = turn.game.take_pending_delivery_resume()
        if _resume:
            logger.info(
                "LILY_DELIVERY | RESUME_VERBATIM | session=%s q=%d — reading "
                "the remaining options as armed",
                turn.game.sk.session_id, turn.game.sk.question_number,
            )
            turn.text = _resume
        delivery = turn.game.register_delivery_claim(
            turn.text, speech_id=turn.speech_id
        )
        if delivery == "rewrite_strict":
            turn.text = turn.game.rendered_armed_question()
            turn.game.expect_delivery()
            delivery = turn.game.register_delivery_claim(
                turn.text, speech_id=turn.speech_id
            )
            if delivery not in ("claimed_structural", "claimed_core_sentence"):
                logger.error(
                    "LILY_DELIVERY | STRICT_REWRITE_FAILED | session=%s q=%d",
                    turn.game.sk.session_id,
                    turn.game.sk.question_number,
                )
        turn.delivery = delivery
        if delivery in ("duplicate", "held"):
            # "held" (W2): a hold is active and this turn would air the armed
            # question. Suppressed at dispatch exactly like a duplicate.
            turn.mark_suppressed()
            return Silence("delivery_%s" % delivery)
        return turn


class UnownedKickoffSuppress(SpeechTransform):
    """P0-5: "Round" / "Let's do it!" / category teaser may not create a second
    start owner. Only the turn that structurally owns q_N_delivery may carry
    kickoff language. Suppress directly (no retry, no re-air) so setup/user
    holds cannot regenerate the same debris."""

    name = "unowned_kickoff_suppress"

    def apply(self, turn):
        if turn.game.unowned_kickoff_must_suppress(turn.text, turn.delivery):
            blocked = turn.game.start_blocked_reason() or "no_delivery_owner"
            logger.warning(
                "LILY_SAY_SUPPRESSED | reason=unowned_kickoff | session=%s "
                "q=%d blocked=%s text=%r",
                turn.game.sk.session_id,
                turn.game.sk.question_number,
                blocked,
                turn.text[:160],
            )
            turn.mark_suppressed()
            if turn.speech_id:
                turn.game.say_registry.release_owner(turn.speech_id)
            return Silence("unowned_kickoff")
        return turn


class TransitionNarration(SpeechTransform):
    """HOTFIX-006 N12 — ONE NARRATION PER TRANSITION. The transition of a
    question is claimed whole at the reveal; the first turn that performs its
    verdict IS its narration. A second, differently-worded narration of the
    same beat is made physically silent — suppressed, not swallowed."""

    name = "transition_narration"

    def apply(self, turn):
        transition = turn.game.register_transition_narration(
            turn.text, speech_id=turn.speech_id
        )
        if transition == "duplicate":
            turn.mark_suppressed()
            if turn.speech_id:
                turn.game.say_registry.release_owner(turn.speech_id)
            return Silence("transition_duplicate")
        return turn


class AirDupGuard(SpeechTransform):
    """T3 (PATCH-001) — AIR-path dup guard: a verbatim repeat of a recently
    PLAYED turn (interleaving ignored; delivery turns exempt — their re-reads
    are deliberate) never airs again."""

    name = "air_dup_guard"

    def apply(self, turn):
        if turn.game.air_dup_guard(turn.text, turn.delivery):
            logger.warning(
                "LILY_TURNS | DUP_TURN_SKIPPED | path=air | session=%s — "
                "verbatim repeat of a recently played turn suppressed",
                turn.game.sk.session_id,
            )
            turn.mark_suppressed()
            if turn.speech_id:
                turn.game.say_registry.release_owner(turn.speech_id)
            return Silence("air_dup")
        return turn


class PunctuationFlush(SpeechTransform):
    """MANDATORY punctuation-flush guard (Lovebirds fix): LilyTTS is
    streaming=False, wrapped in StreamAdapter gated by blingfire sentence
    tokenization. Suspense holds produce short unpunctuated fragments that
    deadlock the SegmentSynchronizer; append a terminal period so the tokenizer
    always flushes."""

    name = "punctuation_flush"

    def apply(self, turn):
        if turn.text[-1] not in ".!?":
            turn.text += "."
        return turn


SAY_PIPELINE = [
    LeakFilter(),
    HygieneClean(),
    RevealDeliveryFusionClip(),
    ScoreLineGate(),
    FalseEmptyRewrite(),
    OnScreenClaimRewrite(),
    DisputeSycophancyRewrite(),
    YieldAfterFirstQuestion(),
    RepeatLints(),
    RegenGate(),
    EmptyCandidateRetry(),
    BackHoldNarration(),
    DeliveryClaim(),
    UnownedKickoffSuppress(),
    TransitionNarration(),
    AirDupGuard(),
    PunctuationFlush(),
]


def run_say_pipeline(turn: "SpeechTurn"):
    """Run every stage in order over `turn`. Returns a Silence (turn ends with
    silence, possibly scheduling a regen/retry) or None (speak turn.text). Any
    stage that rewrites the text or suppresses the turn emits one uniform
    LILY_SAY | TRANSFORM line so the whole pipeline is observable and a new
    guard cannot silently skip the funnel (GUARD_MAP chain F)."""
    for transform in SAY_PIPELINE:
        before = turn.text
        result = transform.apply(turn)
        if isinstance(result, Silence):
            logger.info(
                "LILY_SAY | TRANSFORM | name=%s action=suppress",
                transform.name,
            )
            return result
        if turn.text != before:
            logger.info(
                "LILY_SAY | TRANSFORM | name=%s action=replace",
                transform.name,
            )
    return None
# ---------------------------------------------------------------------------
# The agent
# ---------------------------------------------------------------------------

class LilyAgent(Agent):
    def __init__(self, game: LilyGame, **kwargs) -> None:
        self._game = game
        self._empty_retry_pending = False
        self._reair_regen_pending = False  # WS-3 regen-gate one-retry bound
        super().__init__(**kwargs)

    async def on_enter(self) -> None:
        # First speech also flips agent state to "speaking", which is the
        # holding-music teardown signal SessionHoldingMusic listens for
        # (useVoiceAssistant agent state — WO-ZUNA-OMNIBUS-001 contract).
        #
        # Say gate: this is one of TWO trigger paths for the session
        # opener (the entrypoint dispatches the other after
        # session.start) — the live double greeting was both paths
        # speaking. Both now dispatch the SAME instructions through
        # gated_say under one key; whichever runs second is suppressed.
        # A reconnect uses its own key (session_rejoin) and its own line,
        # and must NOT trip session_greet.
        if self._game.reconnected:
            self._game.gated_say(
                "session_rejoin",
                "rejoin",
                self._game.rejoin_instructions(),
                source="on_enter",
            )
        else:
            # Memory at the door (F): give group resolution + memory load a
            # short budget so a returning table is recognized in the FIRST
            # utterance. greeting_instructions() composes AFTER the wait —
            # a block that just landed is picked up here. Timeout greets
            # cold exactly as before.
            await self._game.await_greeting_memory()
            self._game.gated_say(
                "session_greet",
                "greet",
                self._game.greeting_instructions(),
                source="on_enter",
            )

    def stt_node(self, audio, model_settings):
        # WS-16 (AMENDMENT-002): pre-STT dereverberation node, DEFAULT OFF
        # (LILY_DEREVERB_NODE) — enabling is gated on the decision memo +
        # operator sign-off. Off returns None here and the stream is passed
        # through untouched with zero new imports on the boot path. The
        # node preserves frame cadence 1:1 (constant in-stream algorithmic
        # delay only), and any processor failure degrades to passthrough.
        processor = lily_dereverb.lily_create_dereverb_processor()
        if processor is not None:
            audio = lily_dereverb.lily_dereverb_frames(audio, processor)
        return super().stt_node(audio, model_settings)

    # -- tools ------------------------------------------------------------------

    @function_tool()
    async def lily_bind_speaker(
        self, context: RunContext, speaker_label: str, player_name: str
    ) -> str:
        """Bind a diarization speaker label to a player's name the moment
        you learn who a voice belongs to.

        Args:
            speaker_label: The label from the transcript tag, e.g. "S2".
            player_name: The player's first name.
        """
        label = (speaker_label or "").strip().strip("[]")
        name = (player_name or "").strip()
        evidence = self._game.confirmed_name_for_label(label)
        if evidence is None:
            # Compatibility fallback for a tool call racing the transcript
            # event's evidence write; still explicit-only, never the broad
            # conversational-token extractor.
            combined = self._game.fragments.combined(label)
            evidence = lily_extract_explicit_name(combined)
            if evidence:
                self._game.note_confirmed_name_evidence(label, evidence)
        biometric_named_label = (
            bool(label)
            and not re.fullmatch(r"S\d+|UU", label)
            and lily_is_valid_name(label)
            and label.lower() == name.lower()
        )
        if evidence:
            # The player's own words outrank a model-proposed tool argument.
            name = evidence
        elif not biometric_named_label:
            return (
                f"Could not bind {label}: no explicit name was confirmed "
                "for this voice. Ask 'what should I call you?' and wait for "
                "their own name before binding or starting."
            )
        if not lily_is_valid_name(name):
            return (
                f"Could not bind {label}: {name!r} does not look like a "
                "confirmed player name."
            )
        # Known-name STT snap (live 2026-07-15, the "Romney" class): if the
        # heard name is almost certainly a garbled spelling of a name this
        # group's memory already knows, bind the REMEMBERED spelling —
        # recognition, memory continuity, and the scoreboard label all key
        # on it. Conservative exactly-one-candidate rule; the spoken
        # confirmation ("welcome back, Rami") lets the table correct a
        # wrong snap instantly.
        snapped = lily_memory.lily_known_name_correction(
            name, getattr(self._game, "memory_player_names", None)
        )
        snap_note = ""
        if snapped:
            logger.info(
                "LILY_BIND | NAME_SNAPPED | heard=%r -> remembered=%r",
                name, snapped,
            )
            snap_note = (
                f" (Heard {name!r}, but this table's memory knows "
                f"{snapped!r} — bound the remembered spelling; confirm the "
                "name lightly when you greet them.)"
            )
            name = snapped
        # HOTFIX-009 W7: every name that reaches this bind is anchored to
        # the voice itself (confirmed evidence, the snap of it, or a
        # biometric label) — so a different current holder of this label
        # that PLAUSIBLY matches (lily_names_probably_same, verified again
        # by the migration writer) is the same person under a wrong
        # spelling. Migrate, don't fork; a dissimilar name keeps release
        # semantics (a second voice on a reused label never takes the
        # first player's score history).
        holder = next(
            (
                n for n, st in self._game.sk.players.items()
                if st.get("speaker_label") == label
            ),
            None,
        )
        self._game.sk.bind_speaker(
            label,
            name,
            rename=bool(
                holder is not None
                and holder != name
                and lily_names_probably_same(holder, name)
            ),
        )
        if holder is not None:
            self._game._migrate_agent_name_refs(holder, name)
        note = self._game.on_speaker_bound(label, name)
        return f"Bound: voice {label} is {name}.{snap_note}{note}"

    @function_tool()
    async def lily_note_fact(
        self, context: RunContext, player_name: str, fact: str
    ) -> str:
        """Log a player's lobby fact or running-bit material the moment it
        lands — one short line ("owns 40 typewriters"). Call it when a
        player gives you their lobby fact, and for any detail worth a
        callback on a future night; it persists across sessions for this
        table.

        Args:
            player_name: The player the fact belongs to.
            fact: One short line, third person.
        """
        name = (player_name or "").strip()
        clean = (fact or "").strip()[:300]
        if not clean:
            return "No fact given — nothing logged."
        if name in self._game.sk.players:
            self._game.sk.set_lobby_fact(name, clean)
        self._game.record_group_fact(name, clean)
        return f"Noted for this table's file: {name} — {clean}."

    @function_tool()
    async def lily_log_clarify(
        self, context: RunContext, player_name: str
    ) -> str:
        """Log a clarify moment. Call this WHENEVER you ask a player whether
        what they just said was an answer or thinking out loud — e.g.
        "Sarah, is that your answer or are you two still arguing?". Call it
        the moment you ask; their next reply resolves it automatically, so
        just ask the question naturally and keep hosting.

        Args:
            player_name: The rostered player you are asking to clarify.
        """
        # Gate (callout-gating WO §2): clarify emits a `clarify` game-event
        # packet and only makes sense against a live answer flow. Same
        # game_started gate as lily_award_bonus — an LLM-readable refusal
        # naming the recovery path, never a silent no-op.
        if not self._game.game_started:
            return (
                "Clarify can only be logged once a round is underway — "
                "the game hasn't started. Call lily_begin_round first, "
                "then ask the player to clarify."
            )
        qnum = self._game.sk.question_number
        if (
            self._game.question_is_terminal(qnum)
            or not self._game.sk.answer_window_open
            or getattr(self._game, "_adjudicating", False)
        ):
            return (
                f"Clarify refused — question {qnum} is already closed. "
                "Do not ask for a final answer and do not re-ask it; move "
                "to the next question or yield."
            )
        name = (player_name or "").strip()
        if name not in self._game.sk.players:
            return f"No rostered player named {name!r} — clarify not logged."
        if not self._game.mark_pending_clarify(name):
            return (
                f"Clarify refused — question {qnum} is already terminal."
            )
        return (
            f"Clarify logged for {name}. Their next reply settles it — "
            "no follow-up tool call needed."
        )

    @function_tool()
    async def lily_enter_adult_mode(
        self,
        context: RunContext,
        confirmed_all_18_plus: bool = False,
    ) -> str:
        """Switch to the adult deck after every player verbally confirms 18+.

        Architect mode is a deployment-authenticated override. A player
        merely claiming to be the architect does not enable it.
        """
        # SENSOR GATE — "sensor" mode only (owner directive 2026-08-06:
        # the deck is OPEN by default; the Audeering age read is
        # unreliable in live rooms and the lane is quota-blocked, which
        # had made the deck permanently unavailable). In open mode the
        # spoken 18+ ceremony below is the gate; when the sensor IS
        # running, an ACTIVE child veto still blocks entry in every mode
        # (checked further down — that invariant is untouched).
        if (
            lily_config.adult_deck_gate_mode() == "sensor"
            and not lily_audeering_client.lily_child_gate_ready()
        ):
            logger.warning(
                "LILY_ADULT_GATE | ADULT_MODE_DECLINED | "
                "reason=child_gate_unavailable | session=%s",
                self._game.sk.session_id,
            )
            return (
                "The grown-up deck is NOT available tonight — one of the "
                "systems it depends on isn't running. Refuse warmly, in "
                "character ('the grown-up deck's taking the night off — "
                "general it is'), never name any mechanism, and do not "
                "retry this tool tonight. Consensus cannot change this."
            )
        # Degraded-persistence gate (2026-07-16): a memoryless session has
        # no persisted consent audit trail — the adult deck requires one.
        if self._game.supabase is None:
            logger.warning(
                "LILY_ADULT_GATE | ADULT_MODE_DECLINED | "
                "reason=no_persistence_no_consent_audit | session=%s",
                self._game.sk.session_id,
            )
            return (
                "The grown-up deck is NOT available tonight — refuse "
                "warmly in character, never name any mechanism, and do "
                "not retry this tool tonight."
            )
        architect = lily_config.architect_mode()
        # HOTFIX-004 / P0-3: the deterministic spoken latch is the authority.
        # A model boolean alone can never enter ("Should I verify?" remains
        # false), but once the real utterance latched, a false/missing model
        # flag must not force the player through the ceremony again.
        age_consent_heard = getattr(self._game, "_age_consent_confirmed", False)
        if not architect and not age_consent_heard:
            logger.warning(
                "LILY_ADULT_GATE | ADULT_MODE_DECLINED | "
                "reason=age_confirmation_required | model_flag=%s "
                "consent_heard=%s | session=%s",
                confirmed_all_18_plus, age_consent_heard,
                self._game.sk.session_id,
            )
            return (
                "Adult mode is NOT enabled yet. Ask every player directly: "
                "'Please confirm out loud that you are 18 or older and want "
                "the grown-up deck.' Wait for an explicit spoken YES from the "
                "table — a question like 'should I verify?' is NOT consent."
            )
        if architect:
            logger.warning(
                "LILY_ADULT_GATE | ARCHITECT_OVERRIDE | session=%s "
                "action=adult_mode_entry",
                self._game.sk.session_id,
            )
        # The child-signal veto is absolute: it may EXIT or BLOCK adult
        # mode, and NOTHING — architect mode included — may override an
        # ACTIVE signal to authorize entry (standing invariant from
        # WO-LILY-AUDEERING-001; the override applies to the age ceremony
        # only).
        if self._game.acoustic.child_veto_active():
            logger.warning(
                "LILY_AUDEERING_VETO | ADULT_MODE_DECLINED | session=%s %s",
                self._game.sk.session_id,
                lily_audeering_consumers.PERCEIVED_FRAMING,
            )
            return (
                "Adult mode is NOT available right now — keep the general "
                "deck. Deflect lightly and in character ('let's keep it "
                "classy tonight') and move on. Do NOT explain why, do NOT "
                "mention any system, audio, detection, or safety mechanism, "
                "and do not retry this tool this round."
            )
        self._game.sk.set_mode("adult")  # sticky flag — reverts only via
        # the deterministic "back to normal" path, a fresh consensus, the
        # child-signal ladder veto (on_child_signal), or a mid-session
        # breaker trip (on_child_gate_lost) — all the same sticky path.
        # Vocal swap (owner directive 2026-08-06): Grok voices the adult
        # deck — Gemini's hard filter blocks it. Failure keeps Gemini
        # (degraded, loud), never blocks entry.
        getattr(self._game, "enter_adult_vocal", lambda: None)()
        # D: no question survives the deck change — the leftover general
        # question is flushed (the live "powerhouse of the cell" defect)
        # and the adult deck starts drawing immediately.
        self._game.flush_for_mode_switch(source="enter_adult")
        getattr(self._game, "mark_setup_applied", lambda *_: None)(
            "adult", "consent"
        )
        # flush_for_mode_switch already published nowait; a second awaited
        # publish only delayed the tool result / follow-up turn.
        self._game.publish_attributes_nowait()
        return (
            "Adult mode is ON (sticky"
            + (", architect override" if architect else ", 18+ confirmed")
            + "). The layer is active; same house "
            "rules. The deck switched with it: any earlier question is "
            "flushed, and the first adult question is being drawn now — "
            "it lands in the state block in a beat. NEVER serve a "
            "leftover general question as adult material and NEVER "
            "freestyle one; vamp for a beat until the state block shows "
            "the adult question, then ask it word for word. "
            "ADULT PICTURES: adult image heat defaults to SUGGESTIVE. "
            "Ask the whole table clearly: for grown-up pictures do they "
            "want spicy-but-suggestive, full explicit, or a mix? (A mix "
            "varies the heat question to question.) Wait for the table "
            "(not one enthusiast) to agree, then call "
            "lily_set_adult_image_intensity with intensity="
            "'suggestive'|'explicit'|'mix' and confirmed_table=true — that "
            "call also turns picture rounds ON when the lane is healthy "
            "(media_mode=pictures). Do not assume explicit. Re-ask only if "
            "they change it. Heat alone without that tool does NOT make "
            "pictures live."
        )

    @function_tool()
    async def lily_set_adult_image_intensity(
        self,
        context: RunContext,
        intensity: str = "suggestive",
        confirmed_table: bool = False,
    ) -> str:
        """Set adult picture heat after the table agrees: 'suggestive',
        'explicit', or 'mix' (varies question to question, within an
        explicit ceiling — 'both' is a valid answer). Call only when adult
        mode is on and the table has clearly chosen. Default stays
        suggestive until they pick.
        """
        if self._game.sk.mode != "adult":
            return (
                "Adult image intensity is only available in adult mode. "
                "Enter the grown-up deck first, then ask the table."
            )
        level = (intensity or "").strip().lower()
        if level not in ("suggestive", "explicit", "mix"):
            return (
                "Invalid intensity. Use intensity='suggestive', "
                "'explicit', or 'mix' only."
            )
        if not confirmed_table and not lily_config.architect_mode():
            return (
                "Intensity is NOT changed yet. Ask every player at the "
                "table: 'For grown-up pictures — spicy-but-suggestive, "
                "full explicit, or a mix? Everyone has to be good with "
                "it.' Call again with confirmed_table=true only after the "
                "table agrees. If anyone wants softer, stay on suggestive."
            )
        if not self._game.sk.set_adult_image_intensity(level):
            return "Intensity not accepted — use suggestive, explicit, or mix."
        # Adult heat is a pictures request — flip media_mode when the lane
        # is healthy so the stocked arsenal can serve (E66E1B: mix set,
        # media stuck voice_only → "not switched on" while bank was ready).
        flip = self._game.try_activate_pictures(
            source="adult_image_intensity", announce=False
        )
        self._game.publish_attributes_nowait()
        if flip == "on" or flip == "already_on":
            getattr(self._game, "mark_setup_applied", lambda *_: None)(
                "heat", "pictures"
            )
            return (
                f"Adult image intensity is now {level.upper()} — sticky for "
                "this session until they change it or say back to normal. "
                "Picture rounds are ON (media_mode=pictures) — the bank will "
                "serve when a picture slot opens. One short confirmation, "
                "then keep the night moving. Only claim an image is on the "
                "screen once one actually lands there."
            )
        if flip == "unavailable_gen":
            return (
                f"Adult image intensity is now {level.upper()}, but pictures "
                "could NOT be switched on — the image generation key is "
                "missing. Heat is saved; stay voice-only and do not claim "
                "pictures are live."
            )
        return (
            f"Adult image intensity is now {level.upper()}, but pictures "
            "could NOT be switched on — the picture pipeline is unreachable. "
            "Heat is saved; stay voice-only and do not claim pictures are live."
        )

    @function_tool()
    async def lily_begin_round(self, context: RunContext) -> str:
        """Kick off round one and open the tiered question loop. Call this
        only after clear start language from the table and at least one
        confirmed bound name. A laugh, general energy, or a bare yes to
        another choice is not authorization. Once called, the state block
        starts serving [NEXT QUESTION] and the answer window opens on your
        first ask. No-op if the game is already running."""
        if self._game.game_started:
            return (
                "STARTED (already running) — the next question is in the "
                "state block; do not re-open Round One."
            )
        blocked = self._game.start_blocked_reason()
        if blocked == "game_stopped":
            return (
                "NOT STARTED (game_stopped) — the game is STOPPED. Do not "
                "open Round One or deliver a question. Wait for an explicit "
                "resume/continue command."
            )
        if blocked == "identity_unconfirmed":
            return (
                "NOT STARTED (identity_unconfirmed) — no player has confirmed "
                "a name yet. Ask 'what should I call you?', bind that explicit "
                "answer, then start. Do not treat a conversational word as a "
                "name; do not announce a category or a question."
            )
        if blocked == "recognition_dispute":
            return (
                "NOT STARTED (recognition_dispute) — a recognition question is "
                "still open. Answer WHY the clean-slate / empty claim happened "
                "first (one sentence from the state note), then follow their "
                "lead. Do NOT announce a category or say let's kick yet."
            )
        if blocked == "ambiguous_yes":
            return (
                "NOT STARTED (ambiguous_yes) — that bare yes answered a "
                "choice, not a start. Do NOT open Round One yet. Ask one clear "
                "confirm — ready to start the game? — or wait for an explicit "
                "'let's start' / 'let's play'."
            )
        if blocked == "user_speaking":
            return (
                "NOT STARTED (user_speaking) — the player is still speaking. "
                "Listen for the rest of the turn; do not announce Round One."
            )
        if blocked == "setup_pending":
            jobs = ", ".join(sorted(self._game.pending_setup_jobs()))
            return (
                "NOT STARTED (setup_pending) — requested setup is incomplete: "
                f"{jobs}. Apply those tools/latches first, then confirm "
                "ready. Do NOT announce Round One or a category."
            )
        intake_hold = (
            "NOT STARTED (intake_active) — a name just landed and the intake "
            "round-robin is still going. Finish collecting names "
            "('who's next?', then 'that everyone?'), and once the "
            "roster is settled call lily_begin_round again."
        )
        if self._game.intake_roundrobin_active():
            # WS-1: a bind just landed — the round-robin is still growing.
            return intake_hold
        await self._game.start_game(source="host_tool")
        if not self._game.game_started:
            blocked = self._game.start_blocked_reason()
            if blocked == "game_stopped":
                return (
                    "NOT STARTED (game_stopped) — wait for an explicit resume "
                    "before any question."
                )
            if blocked == "identity_unconfirmed":
                return (
                    "NOT STARTED (identity_unconfirmed) — get and bind one "
                    "explicit player name before Round One."
                )
            if blocked == "recognition_dispute":
                return (
                    "NOT STARTED (recognition_dispute) — a recognition "
                    "question is still open. Answer WHY the clean-slate / "
                    "empty claim happened first, then follow their lead."
                )
            if blocked == "ambiguous_yes":
                return (
                    "NOT STARTED (ambiguous_yes) — that bare yes answered a "
                    "choice, not a start. Wait for an explicit 'let's start' "
                    "/ 'let's play'."
                )
            if blocked == "user_speaking":
                return (
                    "NOT STARTED (user_speaking) — the player is still "
                    "speaking."
                )
            if blocked == "setup_pending":
                jobs = ", ".join(sorted(self._game.pending_setup_jobs()))
                return (
                    "NOT STARTED (setup_pending) — requested setup is "
                    f"incomplete: {jobs}. Apply it before Round One."
                )
            # A bind landed between the gate check above and start_game's
            # own gate — the start deferred (WS-1).
            return intake_hold
        # BUG-2 authoritative-delivery contract: this tool result carries
        # the question payload, and the post-tool turn (this result's
        # continuation) is the SOLE deliverer — start_game deliberately
        # dispatched no instructed reply for the host_tool source. The
        # q_{N}_delivery claim in tts_node makes any duplicate read of
        # the same question physically silent.
        q = self._game.armed_question
        if q is not None:
            # Structural delivery claim (desync WO Sub-agent B): the
            # post-tool turn is the sole deliverer BY CONTRACT — it claims
            # q_{N}_delivery at dispatch (WS-1: rewritten to the sheet
            # first when it drifts from the question text).
            self._game.expect_delivery()
            return (
                "STARTED — Round one is armed and YOU deliver the first "
                "question in this very turn; you are its sole deliverer. One "
                "short transition beat (set the round-one category: "
                f"{q.get('category', 'general')}), then ask exactly, word "
                f"for word: {q.get('prompt', '')!r} Never re-ask it in a "
                "later turn."
            )
        return (
            "STARTED — Round one is armed but the first question hasn't "
            "landed yet; banter for a beat, and deliver it only when it "
            "appears in the state block. Do not invent a question."
        )

    @function_tool()
    async def lily_award_bonus(
        self, context: RunContext, player_name: str, reason: str
    ) -> str:
        """Award one bonus point for a great moment — best wrong answer of
        the round, or a similar table-delighting play. Use occasionally,
        never as a pity point.

        Args:
            player_name: The rostered player receiving the point.
            reason: One short line on why (spoken back and shown on screen).
        """
        # Gate: bonuses are scoring events on a live round. Before the
        # arm/ask/adjudicate loop has engaged (game_started=False) every
        # award would silently backfill players[name].score and disguise a
        # stalled loop as a working game — exactly what the 2026-07-14 rows
        # showed. Refuse loudly so Lily's LLM adapts and any future stall
        # surfaces as empty scores instead of ghost bonuses.
        if not self._game.game_started:
            return (
                "NO BONUS — bonus points can only be awarded once a round is "
                "underway. Call lily_begin_round first or wait for auto-start; "
                "do not tell the table a point landed."
            )
        name = (player_name or "").strip()
        if name not in self._game.sk.players:
            return (
                f"NO BONUS — no rostered player named {name!r}; no point "
                "awarded. Do not tell the table a point landed."
            )
        clean_reason = (reason or "").strip()[:200] or None
        entry = self._game.sk.award_bonus(name, transcript=clean_reason)
        if entry is None:
            # RESULT-DERIVED SPEECH: the ledger (the sole score writer)
            # declined — no in-memory mutation happened, so no bonus is true.
            # The failure string is the ONLY speakable output; never narrate a
            # point the ledger did not commit, and do not emit the screen event.
            return (
                f"NO BONUS — the ledger did not record a point for {name!r}. "
                "Do not tell the table a bonus landed; carry on with the game."
            )
        # Bonus audit row (WS-7): every scoring mutation writes a
        # lily_answers row with a cause — the live bonus point had none.
        supabase = self._game.supabase
        if supabase is not None:
            asyncio.ensure_future(lily_persistence.lily_write_score_event(
                supabase, self._game.sk.session_id, entry,
            ))
        self._game.send_event_nowait(
            "best_wrong_answer",
            {
                "player": name,
                "answer": clean_reason,
                # Contract-note spellings, tolerated as extra fields:
                "name": name,
                "text": clean_reason,
            },
        )
        # Score is committed in-memory; event is nowait — don't block the
        # tool-return turn on attribute RTT.
        self._game.publish_attributes_nowait()
        # Lead with the committed ledger fact so the model cannot invent a
        # different number: the point is on the ledger, and the new total is
        # what the ledger says — not what the model remembers.
        new_score = self._game.sk.players[name]["score"]
        return (
            f"BONUS COMMITTED: +1 to {name}, now on {new_score}. Say the "
            "bonus landed and that total; state no other number."
        )

    @staticmethod
    def _answer_matches(attempt: str | None, canonical: str | None) -> bool:
        """W1: does a recorded utterance corroborate a canonical answer,
        via the EXISTING Tier-1 matcher (no new matching machinery)."""
        if not attempt or not canonical:
            return False
        return (
            lily_evaluation.lily_tier1_evaluate(attempt, [canonical]).get(
                "verdict"
            )
            == "correct"
        )

    @function_tool()
    async def lily_correct_verdict(
        self, context: RunContext, player_name: str, grounds: str, reason: str
    ) -> str:
        """Reverse a WRONG ruling and put a denied point back. Use ONLY when
        a verdict you already committed is established wrong — a correct
        answer was scored incorrect, an answer was misheard, a rule was
        misapplied (a clock on a relaxed round), or a question was judged
        outside its own window. This does NOT overwrite the old ruling: it
        appends an audited correction, so the point is restored and the trail
        gets longer, not rewritten. Never use it to invent a point that was
        never earned — it only amends a ruling that actually happened.

        Args:
            player_name: The rostered player whose verdict is being corrected.
            grounds: WHY the ruling was wrong — one of "answer_denied"
                (a correct answer was marked incorrect), "misheard" (you
                misheard the answer), "wrong_rule" (a rule was misapplied,
                e.g. a timer on a relaxed round), "out_of_window" (judged
                outside its own window).
            reason: One short line spoken back to the table ("diamond was
                yours, and I'd put a clock on a relaxed round").
        """
        if not self._game.game_started:
            return (
                "A verdict can only be corrected once a round is underway."
            )
        name = (player_name or "").strip()
        if name not in self._game.sk.players:
            return f"No rostered player named {name!r} — nothing corrected."
        ground = (grounds or "").strip().lower()
        sk = self._game.sk
        # answer_denied is mechanically corroborated in correct_verdict (the
        # recorded attempt must fuzzy-match canonical). Two legs:
        #  (1) in-session: the denied row's own transcript (a recorded-but-
        #      mis-ruled correct answer);
        #  (2) FALLBACK (HOTFIX-009 Option B): the ignored-answer class — the
        #      correct utterance was rejected by one-candidate-per-player and
        #      survives only in lily_addressee_log. A scoped session read
        #      pulls in-window fuzzy-matched transcripts; the SAME Tier-1
        #      matcher re-checks each against THIS question's canonical (the
        #      table has no question_id, so the match IS the association).
        # Other grounds ignore these kwargs.
        canonical = None
        corroborating_attempt = None
        if ground == "answer_denied":
            denied = sk.ledger_row_for(name, None)
            qid = denied.get("question_id") if denied else None
            for h in reversed(self._game.asked_history):
                if h.get("question_id") == qid:
                    canonical = h.get("canonical_answer")
                    break
            in_session = denied.get("transcript") if denied else None
            if canonical and self._answer_matches(in_session, canonical):
                corroborating_attempt = in_session
            elif canonical and self._game.supabase is not None:
                try:
                    transcripts = (
                        await lily_persistence
                        .lily_fetch_inwindow_fuzzy_transcripts(
                            self._game.supabase, sk.session_id
                        )
                    )
                except Exception as e:
                    logger.warning(
                        "LILY_SCORE | VERDICT_CORRECTION_DB_UNREACHABLE | "
                        "session=%s player=%s — refused honestly (%s)",
                        sk.session_id, name, e,
                    )
                    return (
                        "I can't reach the record I'd need to check that call "
                        "right now, so I'm not moving the score on a guess — "
                        "ask me again in a moment and I'll take another look."
                    )
                for t in transcripts:
                    if self._answer_matches(t, canonical):
                        corroborating_attempt = t
                        break
        entry = sk.correct_verdict(
            name,
            grounds=ground,
            actor="player_contest",
            delta=1,
            canonical_answer=canonical,
            corroborating_attempt=corroborating_attempt,
        )
        if entry is None:
            # Refused: no prior verdict to amend, unknown grounds, or already
            # corrected. Tell the LLM plainly so it states why it stands
            # rather than inventing a point.
            return (
                f"No correction made for {name} — there's no matching committed "
                "verdict to amend on those grounds, or it was already "
                "corrected. If the ruling stands, say so plainly and why; "
                "never invent a point."
            )
        supabase = self._game.supabase
        if supabase is not None:
            asyncio.ensure_future(lily_persistence.lily_write_score_event(
                supabase, self._game.sk.session_id, entry,
            ))
        clean_reason = (reason or "").strip()[:200] or None
        self._game.send_event_nowait(
            "verdict_corrected",
            {
                "player": name,
                "grounds": entry.get("grounds"),
                "delta": entry.get("points"),
                "score_after": entry.get("score_after"),
                "reason": clean_reason,
            },
        )
        self._game.publish_attributes_nowait()
        # Lead with the committed ledger fact (the anti-invention rule the
        # whole tool exists to serve): the score is read off the ledger row
        # this correction just appended, never reconstructed by the model.
        new_score = self._game.sk.players[name]["score"]
        return (
            f"CORRECTED: the point is back with {name}, now on {new_score}. "
            "Say the correction landed and that total; state no other number."
        )

    # Ungated by game_started (tool-gating principle: gate tools that
    # mutate game outcomes or emit game events — a pacing preference does
    # neither, and its primary habitat is the pre-game lobby).
    @function_tool()
    async def lily_set_pacing(self, context: RunContext, pacing: str) -> str:
        """Set the table's pacing: "timed" (the standard answer clock —
        the default) or "relaxed" (easygoing, no time pressure — answer
        windows run about twice as long). Call it whenever the table
        chooses a pacing in any phrasing — "let's keep it casual", "put
        the timer back on" — or answers your timed-or-relaxed offer.
        Pacing is about TIME only; a table asking for "freeform" vs
        "multiple choice" questions wants lily_set_round_format instead.
        The choice is saved as this table's usual for future nights.

        Args:
            pacing: "timed" or "relaxed".
        """
        choice = (pacing or "").strip().lower()
        if not self._game.set_pacing(choice, source="tool"):
            return (
                f"Unknown pacing {pacing!r} — the only choices are 'timed' "
                "and 'relaxed'. Nothing changed."
            )
        if choice == "relaxed":
            return (
                "Pacing is RELAXED — committed and saved as this table's "
                "usual. Answer windows now run about twice as long; keep "
                "the tempo loose — no countdown talk, no rushing anyone."
            )
        return (
            "Pacing is TIMED — committed and saved as this table's usual. "
            "The standard answer clock is on."
        )

    @function_tool()
    async def lily_set_round_format(
        self, context: RunContext, format: str
    ) -> str:
        """Switch the question format the moment the table asks — "can we
        do multiple choice" flips to four-option questions; "normal
        questions" goes back to open ones. Callable in any phase; it takes
        effect on the current/next round and sticks until changed again.
        (Round two runs multiple choice by default.)

        Args:
            format: "multiple_choice" or "freeform".
        """
        fmt = (format or "").strip().lower().replace("-", "_").replace(" ", "_")
        if fmt in ("mc", "multichoice", "multiple_choice"):
            fmt = "multiple_choice"
        elif fmt in ("normal", "open", "free_form", "freeform"):
            fmt = "freeform"
        game = self._game
        if not game.sk.set_round_format(fmt):
            return (
                f"Unknown format {format!r} — use 'multiple_choice' or "
                "'freeform'."
            )
        # In-flight questions follow the new format where that's still
        # safe: the armed question only changes shape while it has NOT been
        # delivered (no q_{N}_delivery claim, window shut).
        armed = game.armed_question
        armed_mutable = (
            armed is not None
            and not game.sk.answer_window_open
            and game.say_registry.state(
                f"q_{game.sk.question_number}_delivery"
            ) is None
        )
        if fmt == "multiple_choice":
            if game.next_question is not None:
                asyncio.ensure_future(
                    game.reasoning.ensure_choices(game.next_question)
                )
            if armed_mutable:
                asyncio.ensure_future(game.reasoning.ensure_choices(armed))
            return (
                "Round format is now multiple_choice (sticky). Questions "
                "carry four options: read the question, then the options "
                "once — A, B, C, D — and never re-read them unless asked. "
                "If the current question shows no choices yet, run it as "
                "asked; the next one will have them."
            )
        if game.next_question is not None:
            game.next_question.pop("choices", None)
        if armed_mutable and armed is not None:
            armed.pop("choices", None)
            game.eliminated = []
        return (
            "Round format is now freeform (sticky) — open questions, "
            "no options read out, from the next question you ask."
        )

    @function_tool()
    async def lily_set_category(self, context: RunContext, topic: str) -> str:
        """Build the round on a subject the table names — "give me a Game
        of Thrones round", "let's do Japan", "a round about 90s hip-hop".
        You write your own questions on the fly, so ANY topic is fair game:
        call this the moment a table asks for a specific subject, and NEVER
        tell them the deck is fixed.

        THIS TOOL BUILDS THE ROUND BEFORE IT ANSWERS YOU. It takes a beat,
        and its result is the ONLY thing that makes a custom round true:
        say NOTHING about the topic until it returns. If it reports the
        round is built, tell them and ask the question in the state block.
        If it reports nothing was built, say exactly that and offer a lane
        from the deck. Never say you're "putting their round together" —
        either it is built or it isn't, and this tool is what knows.

        Args:
            topic: The subject for the round, in the table's own words
                (e.g. "Game of Thrones", "Japan", "90s hip-hop").
        """
        game = self._game
        subject = " ".join((topic or "").split())[:80]
        if not subject:
            return (
                "No topic caught — ask the table what subject they want and "
                "call this again with it."
            )
        if game.sk.mode == "adult":
            # The adult deck rotates its OWN families and a custom label
            # must never ride an adult question (the deck-identity firewall:
            # adult questions announced as an academic category was a live
            # defect). Redirect honestly — never a flat denial.
            return (
                f"Custom topics run on the general deck. Say 'back to "
                f"normal' first, then name {subject!r} again and I'll build "
                "the round."
            )
        target_round = game._round_for_next_question()
        game._category_override[target_round] = subject
        # Persist the category as a first-class bank entry (idempotent by
        # name — no duplicate for one that already exists). Fire-and-forget:
        # the round serves regardless of the write, and a failed write logs
        # the full payload for recovery (Cardinal Rule). The category's
        # generated questions bank themselves through the normal curate path
        # (_curate_generated_question -> lily_bank_generated_question), so a
        # later request for this topic can draw them from the bank.
        if getattr(game, "supabase", None) is not None:
            asyncio.ensure_future(
                lily_bank.lily_register_operator_category(
                    game.supabase, subject, subject,
                    getattr(game, "group_id", None),
                )
            )
        # The prefetched next question was drawn on the OLD category — drop
        # it and redraw under the new topic. A live/delivered armed question
        # stays on the glass; the override takes the first UNDELIVERED slot
        # (same mutability guard as lily_set_round_format).
        game.next_question = None
        armed = game.armed_question
        armed_mutable = (
            armed is not None
            and not game.sk.answer_window_open
            and game.say_registry.state(
                f"q_{game.sk.question_number}_delivery"
            ) is None
        )
        if armed_mutable:
            game.armed_question = None
            game.sk.current_question = None
        task = game._prefetch_task
        if task is not None and not task.done():
            task.cancel()
        game._prefetch_task = None
        game._prefetch_stall_ticks = 0
        game.sk.set_status_note(
            f"custom round requested: {subject!r} is BUILDING right now and "
            "is NOT built yet. Say nothing about this round until the tool "
            "result comes back — not that it's coming, not that you're "
            "putting it together. Never invent a question about it."
        )
        # HOTFIX-006 N2 — the awaited build. Everything above this line only
        # POINTS the round at the topic; nothing above it has produced a
        # question, which is precisely the state session lily-16A9AE narrated
        # as a finished Cape Cod round. The result of the build is the only
        # input to what she says next, and an empty result has exactly one
        # rendering: the refusal.
        result = await game.build_custom_round(subject, target_round)
        return lily_custom_round_line(result)

    @function_tool()
    async def lily_show_demo_picture(
        self, context: RunContext, adult: bool = False
    ) -> str:
        """Put ONE real demo image on the screen right now — the lobby
        included. Call this whenever someone asks to SEE what picture
        rounds look like ("show me", "prove it", "I'll believe it when I
        see it") instead of describing pictures in words. Pass adult=true
        when they ask what the GROWN-UP deck's pictures look like — the
        sample uses the adult comic-book art direction at the session's
        sticky adult_image intensity (suggestive default, or explicit if
        the table set it). The tool result is the ONLY thing that makes
        "look at the screen" true: if it confirms the image landed, point
        the table at the screen; if it reports a failure, say exactly
        that — never claim anything reached the screen on your own.
        """
        if adult and not (
            (getattr(self._game, "availability_flags", None) or {}).get(
                "adult_deck", False
            )
        ):
            return (
                "NO IMAGE LANDED: the adult deck is not available in this "
                "session's configuration — offer the general demo instead."
            )
        landed, line = await self._game.show_demo_picture(adult=adult)
        prefix = "IMAGE ON SCREEN: " if landed else "NO IMAGE LANDED: "
        return prefix + line

    @function_tool()
    async def lily_use_fifty_fifty(
        self, context: RunContext, player_name: str
    ) -> str:
        """Spend a player's one 50/50 lifeline on the live multiple-choice
        question. Call it the moment a player asks for their 50/50; the
        result names the TWO eliminated options — cross exactly those two
        out aloud, once, and let the two survivors stand.

        Args:
            player_name: The rostered player spending their lifeline.
        """
        game = self._game
        if not game.game_started:
            return "The game hasn't started — no lifelines to spend yet."
        name = (player_name or "").strip()
        if name not in game.sk.players:
            return f"No rostered player named {name!r} — no lifeline spent."
        question = game.armed_question or game.sk.current_question
        choices = (question or {}).get("choices")
        if not isinstance(choices, list) or len(choices) != 4:
            return (
                "This question has no multiple-choice options — the 50/50 "
                "only works on a multiple-choice question. Lifeline NOT "
                "spent; tell them to save it."
            )
        if game.eliminated:
            return (
                "A 50/50 already ran on this question — the crossed-out "
                "options stand. Lifeline NOT spent."
            )
        if not game.sk.use_lifeline(name):
            return (
                f"{name} already used their lifeline this game — no dice, "
                "say so with love."
            )
        eliminated = lily_evaluation.lily_fifty_fifty_eliminations(
            choices, str(question.get("canonical_answer", ""))
        )
        if len(eliminated) != 2:
            # Never eliminate blind — refund the lifeline.
            game.sk.players[name]["lifeline_available"] = True
            return (
                "The 50/50 could not run on this question — lifeline NOT "
                "spent, they keep it."
            )
        game.eliminated = eliminated
        await game.publish_metadata(
            question.get("prompt", ""),
            choices=choices,
            eliminated=eliminated,
            image_url=question.get("image_url"),
            category=question.get("category"),
        )
        letters = lily_evaluation.MC_CHOICE_LETTERS
        gone = " and ".join(
            f"{letters[i]} ({choices[i]})" for i in eliminated
        )
        return (
            f"50/50 committed for {name} — eliminated: {gone}. Cross "
            "exactly those two out aloud, once; two options remain."
        )

    # -- memory transparency + deletion right (WO-LILY-FORGETME-001) ---------------
    #
    # Both tools are deliberately UNGATED by game_started, per the tool-
    # gating principle (README): gate tools that mutate game outcomes or
    # emit game events — explaining memory and deleting it do NEITHER
    # (memory_forgotten is a memory-transparency packet, not a game event),
    # and the deletion right must work from the lobby onward.

    @function_tool()
    async def lily_forget_group(self, context: RunContext, confirm: bool) -> str:
        """Delete everything you keep for this table — voices, games,
        facts — for good. Two-step: a call with confirm=false is refused;
        you must first ask the table out loud, naming the scope, and get a
        spoken yes. Tonight's game keeps going either way.

        Args:
            confirm: True ONLY after a player said yes out loud to your
                confirmation question. False to check the flow first.
        """
        game = self._game
        if not confirm:
            # Arm the deterministic pending-confirm state so the spoken
            # yes/no is parsed in code even if the follow-up tool call
            # never comes (requester unattributed -> any voice settles it).
            if game.forget_state in ("idle", "declined", "failed"):
                game.forget_state = "pending_confirm"
                game.forget_spoken_confirmed = False
                game.forget_requester = None
            return (
                "NOT deleted — deletion needs a spoken yes first. Ask the "
                "table ONE plain confirmation naming the scope: everything "
                "kept for this table — voices, games, facts — gone for "
                "good, and tonight's game keeps going. On a spoken yes, "
                "call lily_forget_group with confirm=true. On a no, drop "
                "it for the night — never ask twice, never argue for being "
                "remembered."
            )
        if (
            game.forget_state not in ("executing", "done")
            and not getattr(game, "forget_spoken_confirmed", False)
        ):
            return (
                "NOT deleted — no verified spoken yes was recorded for the "
                "pending request. Ask the confirmation once and wait for the "
                "requesting player to answer."
            )
        result = await game.execute_forget(source="tool")
        return lily_forget.lily_forget_result_message(result)

    @function_tool()
    async def lily_explain_memory(self, context: RunContext) -> str:
        """Explain honestly what you have on file for this table — counts
        only (voices remembered, games on record, facts kept) and how you
        recognized them tonight. Call it when someone presses on how you
        knew them, or asks what you remember. Read-only; stores nothing;
        never returns raw contents."""
        game = self._game
        if getattr(game, "device_candidate_group_id", None):
            return (
                "A device-linked table record exists, but no current voice "
                "has been verified against it. Do NOT disclose counts, "
                "dates, names, preferences, winners, or facts. Say only: "
                "'This device looks familiar, but I don't know who's here "
                "until you speak — who's playing tonight?' Never claim you "
                "recognized a person from the device."
            )
        counts = None
        if game.supabase is not None:
            counts = await lily_persistence.lily_count_group_memory(
                game.supabase, game.group_id
            )
        return lily_forget.lily_explain_memory_result(
            counts, game.group_id_source
        )

    # -- preemptive-generation control (P2) ----------------------------------------

    def set_preemptive_generation(self, enabled: bool) -> None:
        """Per-turn preemptive control. 1.6.6 reads the agent-level
        turn_handling["preemptive_generation"] dict LIVE at fire time
        (agent_activity.preemptive_generation_opts merges it over the
        session options on every access), so flipping this flag pauses /
        resumes preemptive runs without touching the session."""
        self._turn_handling.setdefault("preemptive_generation", {})[
            "enabled"
        ] = enabled

    # -- context blocks (P2 preemptive repair, 2026-07-14) --------------------------
    #
    # The state-block / adult-layer / memory-block injections used to live
    # ONLY in llm_node — i.e. AFTER preemptive generation snapshots the
    # agent's chat context, and on a per-generation copy the persistent
    # context never saw. Every user turn therefore ran against a context
    # the 1.6.6 equivalence check (preemptive.chat_ctx.is_equivalent(...))
    # could not certify, and the preemptive LLM run was discarded — the
    # observed "chat context changed after on_user_turn_completed"
    # warnings, 13/session, double LLM cost.
    #
    # Fix: the injections run in on_user_turn_completed against BOTH the
    # turn context (so this turn's generation sees FINAL context) and the
    # agent's persistent chat context (so the NEXT preemptive snapshot —
    # taken from agent.chat_ctx during user speech — already carries
    # identical blocks). Injected messages use STABLE item ids and are
    # rewritten only when their text actually changed, because
    # is_equivalent compares item ids + content: an unchanged game state
    # across the turn boundary now validates the preemptive run, and a
    # changed one invalidates it honestly (the preemptive run really did
    # see stale state).

    _CTX_ID_ADULT = "lily_ctx_adult_layer"
    _CTX_ID_MEMORY = "lily_ctx_memory_block"
    _CTX_ID_STATE = "lily_ctx_state_block"
    _CTX_ID_STATE_VOLATILE = "lily_ctx_state_volatile"

    def _apply_context_blocks(
        self, chat_ctx, *, now: float | None = None,
        include_volatile: bool = False,
    ) -> None:
        """Idempotent, deterministic injection of the three system blocks.
        Exact injection semantics preserved from the llm_node era: adult
        layer added/removed on the sticky mode flag, memory block once,
        state block replace-then-append — all keyed on the same dedupe
        markers (_ADULT_LAYER_MARKER, MEMORY_BLOCK_MARKER,
        _STATE_BLOCK_MARKER)."""
        items = _chat_items(chat_ctx)
        # BEHIND the agent's own instructions, never in front of them. Both
        # blocks used to insert(0, ...), which puts them ahead of the
        # ~8,000-token system prompt — and a provider's prompt cache keys on
        # the PREFIX, so every injection invalidated the largest stable
        # thing in the context. Live: 134,357 input tokens against 42,368
        # cached. The system prompt does not change within a session; the
        # adult layer and memory block do (mode swap, late recognition), so
        # they belong after it.
        anchor = 1 if items and getattr(items[0], "role", None) == "system" else 0

        # PROMPT-EVICTION FIX (2026-08-09, P0). These scans were keyed on
        # MARKER TEXT ("[GAME STATE]", "[RETURNING TABLE]"). 59c437b
        # (2026-08-08) added those literal strings to lily_system.txt
        # itself — from that deploy on, the state scan matched the
        # INSTRUCTIONS item (the framework injects the system prompt as a
        # ctx item, id lk.agent_task.instructions, at activity start;
        # Lily's instructions are a plain str so NOTHING re-adds them per
        # generation) and popped the entire persona/rules prompt on the
        # FIRST user turn of every session, while the memory scan matched
        # the prompt and skipped injecting the memory block. Every
        # 2026-08-09 call (incl. lily-A070E8) ran user turns with NO
        # system prompt. The scans now key on the exact injection ids —
        # every block this method has ever injected carries one, ids
        # survive ChatContext.copy(), and no foreign item can collide.
        def _idx_by_id(ctx_id: str):
            return next(
                (
                    i for i, m in enumerate(items)
                    if getattr(m, "id", None) == ctx_id
                ),
                None,
            )

        # Adult layer: additive injection/removal keyed on the sticky flag.
        adult_idx = _idx_by_id(self._CTX_ID_ADULT)
        if self._game.sk.mode == "adult" and adult_idx is None:
            items.insert(
                anchor,
                ChatMessage(
                    id=self._CTX_ID_ADULT,
                    role="system",
                    content=[LILY_ADULT_LAYER],
                ),
            )
        elif self._game.sk.mode != "adult" and adult_idx is not None:
            items.pop(adult_idx)  # removing the layer fully reverts her

        # Returning-table memory: one persistent [RETURNING TABLE] system
        # block, injected the same additive way as the adult layer
        # (re-inserted if history trimming ever drops it) — and REMOVED the
        # same way when memory_block is cleared (forget-me teardown:
        # injection stops immediately, WO-LILY-FORGETME-001).
        memory_idx = _idx_by_id(self._CTX_ID_MEMORY)
        if self._game.memory_block and memory_idx is None:
            items.insert(
                anchor,
                ChatMessage(
                    id=self._CTX_ID_MEMORY,
                    role="system",
                    content=[self._game.memory_block],
                ),
            )
        elif not self._game.memory_block and memory_idx is not None:
            items.pop(memory_idx)

        # State block: replace the previous injection, then append fresh —
        # but ONLY when the rendered text changed. Leaving an unchanged
        # block untouched (same item id, same content, same position) is
        # what lets the preemptive equivalence check pass on quiet turns.
        #
        # Say gate (leak filter): the block is wrapped in the
        # <lily_state>...</lily_state> sentinel envelope, and it rides as
        # SYSTEM-role context (ChatMessage(role="system") below — every
        # injection path in this method is system-role; nothing injects
        # ambient state as a user/assistant item). If any of it echoes
        # into an outbound turn, tts_node's lily_filter_leaks strips it
        # deterministically by that sentinel.
        # P2 volatile-tail split (2026-08-09): the equivalence-visible
        # injection carries the STABLE state only. The volatile tail
        # (clock, answer window, live candidates) — the lines that changed
        # on nearly every in-game turn and forced preemptive OFF for the
        # whole live game (G1) — rides a separate item injected ONLY on
        # per-generation copies (include_volatile=True from llm_node),
        # which the preemptive check never sees.
        stable_text, volatile_text = self._game.build_state_block_split(
            now=now
        )
        state = lily_say_gate.lily_wrap_state_block(stable_text)
        existing = [
            i for i, m in enumerate(items)
            if getattr(m, "id", None) == self._CTX_ID_STATE
        ]
        if len(existing) == 1 and _message_text(items[existing[0]]) == state:
            pass  # unchanged block stays put — the equivalence check passes
        else:
            for i in reversed(existing):
                items.pop(i)
            items.append(
                ChatMessage(
                    id=self._CTX_ID_STATE, role="system", content=[state]
                )
            )
        if not include_volatile:
            return
        volatile = lily_say_gate.lily_wrap_state_block(volatile_text)
        stale = [
            i for i, m in enumerate(items)
            if getattr(m, "id", None) == self._CTX_ID_STATE_VOLATILE
        ]
        for i in reversed(stale):
            items.pop(i)
        items.append(
            ChatMessage(
                id=self._CTX_ID_STATE_VOLATILE,
                role="system",
                content=[volatile],
            )
        )

    def _trim_history(self, chat_ctx) -> None:
        """Y3 (HOTFIX-007): bound conversation-history growth. Archaeology:
        nothing ever trimmed the chat context — a long session grew the
        prompt without limit (live: 134k input tokens on a trivia game).
        The framework already ships the mechanism (ChatContext.truncate:
        keeps the tail, drops orphaned function calls, re-adds the first
        instruction message) — this is a WIRE to it, not a new layer.

        Hysteresis by design: trimming slides the provider's cacheable
        prefix, so it fires in rare big steps (high -> low watermarks),
        never per turn. The injected system blocks (adult/memory/state)
        are re-inserted by _apply_context_blocks immediately after every
        trim site, exactly as its comment always promised. The one-turn
        preemptive invalidation a trim causes is expected and visible in
        the Y2 counter."""
        high = lily_config.history_trim_high()
        if high <= 0:
            return  # disabled
        truncate = getattr(chat_ctx, "truncate", None)
        items = _chat_items(chat_ctx)
        if not callable(truncate) or len(items) <= high:
            return
        low = min(lily_config.history_trim_low(), high)
        before = len(items)
        try:
            truncate(max_items=low)
        except Exception:
            logger.exception(
                "LILY_CTX | HISTORY_TRIM_FAILED — turn proceeds untrimmed"
            )
            return
        logger.info(
            "LILY_CTX | HISTORY_TRIMMED | items %d -> %d (high=%d low=%d) — "
            "one preemptive invalidation expected this turn",
            before, len(_chat_items(chat_ctx)), high, low,
        )

    # -- node overrides ------------------------------------------------------------

    async def on_user_turn_completed(self, turn_ctx, new_message) -> None:
        # Signature verified against livekit-agents 1.6.6
        # (voice/agent.py: async def on_user_turn_completed(self,
        # turn_ctx: llm.ChatContext, new_message: llm.ChatMessage)).
        # Runs after end-of-turn, before the reply generation is chosen —
        # the preemptive equivalence check compares its snapshot against
        # turn_ctx AFTER this hook, so this is where the context must
        # reach its final shape.
        #
        # Cut-recovery cancel (WS-3): the room re-engaged on its own, so any
        # pending auto-resume watchdog stands down — the reply this user
        # turn produces owns the recovery (no double-speak).
        self._game.note_user_turn()
        # V3 (HOTFIX-010): a human has now spoken. The cold opener was intro +
        # one orienting beat; recognition / walkthrough / prefs / what's-new
        # ride the mid-session paths from here on, never the pre-utterance
        # greet.
        self._game._first_human_utterance_seen = True
        # HOTFIX-010 V5: a present voice is now on the floor. The one name
        # ask has had its beat — spend it — and, while no name is bound,
        # stand up the speaker-label placeholder so hosting and scoring
        # proceed. Both satisfy the identity gate permanently, so the ask
        # can never re-fire; a real name still migrates the placeholder's
        # history through bind_speaker whenever it arrives.
        game = self._game
        if getattr(game, "_identity_required_before_start", False):
            game._identity_ask_spent = True
            if game.sk.roster_size(include_placeholder=False) < 1:
                game.sk.ensure_present_placeholder(
                    game.sk.present_placeholder_label()
                )
        consume_reply = getattr(self._game, "consume_deterministic_reply", None)
        message_text = _message_text(new_message)
        event_owned = (
            callable(consume_reply) and consume_reply(message_text)
        )
        prehook_check = getattr(
            self._game, "correct_answer_owns_user_turn", None
        )
        prehook_owned = bool(
            not event_owned
            and callable(prehook_check)
            and prehook_check(message_text)
        )
        # Data-side ownership (live 2026-08-12 lily-639007, the
        # double-verdict session): the two checks above are FLOW-dependent
        # — the exact-text mark rides one code path, and the prehook needs
        # the window still open at commit. W4's relaxed beat-close
        # adjudicates at the transcript seam and closes the window ~2s
        # BEFORE this hook runs, so on that build every answer aired TWO
        # verdicts: the organic reply (which fabricated "you're at three")
        # and adjudicate's ledger-true composite. If the scorekeeper
        # consumed this turn's text as an answer CANDIDATE, the game owns
        # the reply — whatever the ordering was.
        candidate_check = getattr(
            self._game.sk, "recent_answer_text_matches", None
        )  # getattr: fixture fakes predate the ledger
        candidate_owned = bool(
            not event_owned
            and not prehook_owned
            and callable(candidate_check)
            and candidate_check(message_text)
        )
        if event_owned or prehook_owned or candidate_owned:
            logger.info(
                "LILY_REPLY | ORGANIC_SUPPRESSED | session=%s "
                "reason=deterministic_game_reply event_owned=%s "
                "prehook_owned=%s candidate_owned=%s",
                self._game.sk.session_id, event_owned, prehook_owned,
                candidate_owned,
            )
            raise StopResponse()
        # VIDEOIN-001: if the camera lane is open and a frame is buffered,
        # attach the MOST-RECENT frame to THIS user turn as native
        # ImageContent (the multimodal vocal LLM sees it directly — no bucket,
        # no upload). take_camera_frame clears it, so it rides exactly one
        # turn and is never retained or re-attached. The describe constraints
        # (objects only, no person ID, person->redirect) ride the state block
        # via camera_lane_state_line.
        try:
            if getattr(self._game.sk, "camera_lane", "off") == "open":
                frame = self._game.take_camera_frame()
                if frame is not None:
                    from livekit.agents.llm import ImageContent
                    new_message.content.append(ImageContent(image=frame))
                    logger.info(
                        "LILY_CAMERA | FRAME_ATTACHED | session=%s — one turn, "
                        "not retained", self._game.sk.session_id,
                    )
        except Exception:
            logger.exception(
                "LILY_CAMERA | frame attach failed — turn proceeds without it"
            )
        try:
            context_now = time.time()
            # Y3: trim BEFORE block injection so a trim can never drop a
            # block that was just injected; both ctxs trim on the same
            # watermarks so the next equivalence check compares like shapes.
            self._trim_history(turn_ctx)
            self._trim_history(self._chat_ctx)
            self._apply_context_blocks(
                turn_ctx, now=context_now
            )  # this turn sees FINAL context
            self._apply_context_blocks(
                self._chat_ctx, now=context_now
            )  # next preemptive snapshot too
        except Exception:
            # An injection failure must never eat the turn (the framework
            # skips the reply if this hook raises).
            logger.exception(
                "LILY_CTX | context-block injection failed — replying with "
                "unmodified context"
            )

    async def llm_node(self, chat_ctx, tools, model_settings):
        # What REMAINS here after the P2 preemptive repair, and why:
        #
        # 1. _apply_context_blocks: instruction-driven generations
        #    (generate_reply(instructions=...) — reveal, steal, skip, game
        #    start, mode revert, greeting — and tool-call follow-ups) never
        #    pass through on_user_turn_completed, and the reveal turn MUST
        #    see the just-armed NEXT QUESTION / just-flipped mode. This
        #    call mutates only the per-generation copy created inside
        #    _pipeline_reply_task_impl (verified at 1.6.6), which is
        #    invisible to the preemptive equivalence check — it can never
        #    invalidate a preemptive run. For user turns the hook already
        #    ran, the blocks are current, and this is a no-op.
        # 2. publish_attributes_nowait: last_active_at heartbeat — fires a
        #    network write, mutates no chat context.
        # 3. Empty STOP intercept (post PR #12 residual P0): Gemini
        #    FinishReason.STOP with no text and no tools used to reach
        #    tts_node as silence and burn a turn/handle — fine for the
        #    armed-question sheet path, lethal for lobby/banter/reveal
        #    flavor where there is no sheet. Detect here, retry the LLM
        #    once inline (still streaming when content exists), then
        #    force the armed sheet or raise APIConnectionError so the
        #    GENERATION_FAILED path releases claims instead of airing
        #    dead air. Prompt text is never rewritten.
        self._apply_context_blocks(chat_ctx, include_volatile=True)
        self._game.publish_attributes_nowait()

        # Adaptive vocal depth: the vocal model stays LOW for routine host
        # reflexes and moves up for disputes, ambiguity and multi-intent.
        # W2b: this depth is now chosen PER CALL, not by mutating the shared
        # llm._opts in place. Two generations overlap on one _opts (a
        # preemptive speculative reply and the live user turn); the old
        # mutate-to-medium/restore-in-finally dance let one turn's depth leak
        # onto the other — a routine greeting rendered at "medium". The
        # per-turn override travels in extra_kwargs on THIS chat() alone; the
        # shared default is lifted off _opts once (below) so the plugin honors
        # the per-call value.
        self._ensure_vocal_depth_unshared()
        depth = self._vocal_depth_for_turn(chat_ctx)
        async for chunk in self._llm_node_with_empty_stop_guard(
            chat_ctx, tools, model_settings, vocal_depth=depth
        ):
            yield chunk

    def _ensure_vocal_depth_unshared(self) -> None:
        """Once per agent: lift the vocal reasoning-depth default OFF the
        shared llm._opts so every generation selects its own depth per call
        instead of racing on one mutable field. Snapshots the configured
        default (reasoning_effort for the OpenAI/Grok transport,
        thinking_config for the Google transport) and clears it to NOT_GIVEN;
        `_vocal_depth_for_turn` reapplies the right value per call. Idempotent
        and mutation-free after the first call — it never runs mid-generation
        against a live turn."""
        if getattr(self, "_vocal_depth_unshared", False):
            return
        self._vocal_depth_unshared = True
        self._vocal_effort_default = NOT_GIVEN
        self._vocal_thinking_default = NOT_GIVEN
        opts = getattr(getattr(self, "llm", None), "_opts", None)
        if opts is None:
            return
        if hasattr(opts, "reasoning_effort") and is_given(
            getattr(opts, "reasoning_effort", NOT_GIVEN)
        ):
            self._vocal_effort_default = opts.reasoning_effort
            opts.reasoning_effort = NOT_GIVEN
        if hasattr(opts, "thinking_config") and is_given(
            getattr(opts, "thinking_config", NOT_GIVEN)
        ):
            self._vocal_thinking_default = opts.thinking_config
            opts.thinking_config = NOT_GIVEN

    def _vocal_depth_for_turn(self, chat_ctx) -> dict:
        """The per-call depth kwargs for THIS generation. High turns move to
        the elevated tier (medium reasoning_effort / high thinking_level);
        every other turn reasserts the configured default snapshotted by
        `_ensure_vocal_depth_unshared`. Mirrors the original transport split
        (reasoning_effort for OpenAI/Grok, else thinking_config for Google),
        so a plugin never sees a field it does not accept. Empty dict when the
        transport carries no depth control."""
        opts = getattr(getattr(self, "llm", None), "_opts", None)
        if opts is None:
            return {}
        elevated = self._thinking_level_for_turn(chat_ctx) == "high"
        if hasattr(opts, "reasoning_effort"):
            # Front-facing high historically cost ~5s TTFT. Medium adds depth
            # for disputes/multi-intent without the slowest tier every turn.
            effort = "medium" if elevated else self._vocal_effort_default
            return {"reasoning_effort": effort} if is_given(effort) else {}
        if hasattr(opts, "thinking_config"):
            thinking = (
                {"thinking_level": "high"}
                if elevated
                else self._vocal_thinking_default
            )
            return {"thinking_config": thinking} if is_given(thinking) else {}
        return {}

    async def _vocal_llm_stream(
        self, chat_ctx, tools, model_settings, extra_kwargs: dict
    ):
        """Agent.default.llm_node, reimplemented only so the per-call vocal
        depth (W2b) rides in chat()'s extra_kwargs instead of a mutated
        shared _opts. Behaviour-identical to the default node otherwise: same
        activity llm, tool_choice, and conn_options; chunks streamed through
        untouched. Empty depth ⇒ NOT_GIVEN, i.e. exactly the default call."""
        activity = self._get_activity_or_raise()
        activity_llm = activity.llm
        tool_choice = model_settings.tool_choice if model_settings else NOT_GIVEN
        conn_options = activity.session.conn_options.llm_conn_options
        async with activity_llm.chat(
            chat_ctx=chat_ctx,
            tools=tools,
            tool_choice=tool_choice,
            conn_options=conn_options,
            extra_kwargs=extra_kwargs or NOT_GIVEN,
        ) as stream:
            async for chunk in stream:
                yield chunk

    async def _llm_node_with_empty_stop_guard(
        self, chat_ctx, tools, model_settings, *, vocal_depth: dict | None = None
    ):
        """Stream the vocal llm; on empty STOP (no text, no tools) retry once,
        then sheet-or-raise. Contentful streams pass through without buffering
        — TTFT is unchanged on the healthy path. `vocal_depth` carries the
        per-call reasoning-depth kwargs (W2b) so both the first attempt and
        the retry run at the same depth this turn selected."""
        vocal_depth = vocal_depth or {}
        text_chars = 0
        tool_calls = 0
        try:
            async for chunk in self._vocal_llm_stream(
                chat_ctx, tools, model_settings, vocal_depth
            ):
                t_n, tc_n = lily_llm_chunk_signal(chunk)
                text_chars += t_n
                tool_calls += tc_n
                yield chunk
        except APIStatusError as exc:
            if not lily_is_prohibited_content_error(exc):
                raise
            self._log_prohibited_content(exc, chat_ctx, tools)
            sheet = self._blocked_delivery_sheet()
            if sheet:
                logger.error(
                    "LILY_LLM | PROHIBITED_CONTENT_SHEET | session=%s "
                    "q=%d chars=%d — bypassing blocked model with vetted "
                    "deterministic delivery",
                    self._game.sk.session_id,
                    self._game.sk.question_number,
                    len(sheet),
                )
                self._game.expect_delivery()
                yield sheet
                return
            # Non-delivery conversation has no deterministic truth sheet.
            # Preserve the provider's non-retryable failure so the speech
            # handle releases/suppresses; never retry into a storm.
            raise

        if not lily_llm_stream_is_empty_stop(text_chars, tool_calls):
            return

        logger.warning(
            "LILY_LLM | EMPTY_STOP | session=%s attempt=1 — no text and no "
            "tools; retrying LLM once before TTS",
            getattr(self._game.sk, "session_id", "?"),
        )
        text_chars = 0
        tool_calls = 0
        try:
            async for chunk in self._vocal_llm_stream(
                chat_ctx, tools, model_settings, vocal_depth
            ):
                t_n, tc_n = lily_llm_chunk_signal(chunk)
                text_chars += t_n
                tool_calls += tc_n
                yield chunk
        except APIStatusError as exc:
            if not lily_is_prohibited_content_error(exc):
                raise
            self._log_prohibited_content(exc, chat_ctx, tools)
            sheet = self._blocked_delivery_sheet()
            if sheet:
                logger.error(
                    "LILY_LLM | PROHIBITED_CONTENT_SHEET | session=%s "
                    "q=%d chars=%d — deterministic delivery after retry block",
                    self._game.sk.session_id,
                    self._game.sk.question_number,
                    len(sheet),
                )
                self._game.expect_delivery()
                yield sheet
                return
            raise

        if not lily_llm_stream_is_empty_stop(text_chars, tool_calls):
            logger.info(
                "LILY_LLM | EMPTY_STOP_RECOVERED | session=%s chars=%d "
                "tools=%d",
                getattr(self._game.sk, "session_id", "?"),
                text_chars, tool_calls,
            )
            return

        # Still empty. Prefer the armed question sheet when one exists so
        # a delivery turn still reaches the table; otherwise fail the
        # generation so claims release (GENERATION_FAILED) instead of
        # tts_node airing silence into lobby/banter.
        sheet = ""
        try:
            game = self._game
            if (
                getattr(game, "game_started", False)
                and getattr(game, "armed_question", None) is not None
                and not game.sk.answer_window_open
            ):
                sheet = (game.rendered_armed_question() or "").strip()
        except Exception:
            sheet = ""
        if sheet:
            logger.error(
                "LILY_LLM | EMPTY_STOP_SHEET | session=%s chars=%d — "
                "forcing armed question sheet after empty LLM",
                getattr(self._game.sk, "session_id", "?"), len(sheet),
            )
            try:
                self._game.expect_delivery()
            except Exception:
                pass
            yield sheet
            return

        # F1: fail-closed is correct for the empty handle, but two empties
        # at cold open used to leave the room mute (checkers: "broken
        # agent"). Schedule one inventory-keyed opener re-dispatch before
        # raising — existing session_greet / session_rejoin copy only.
        self._maybe_schedule_lobby_empty_stop_recover()

        logger.error(
            "LILY_LLM | EMPTY_STOP_FAILED | session=%s — raising so the "
            "speech handle fails closed (claims release; no silent lobby)",
            getattr(self._game.sk, "session_id", "?"),
        )
        raise APIConnectionError(
            "lily empty STOP: LLM returned no text and no tools after retry",
            retryable=True,
        )

    def _blocked_delivery_sheet(self) -> str:
        game = self._game
        if (
            not getattr(game, "game_started", False)
            or getattr(game, "_delivery_stop_sticky", False)
            or getattr(game, "armed_question", None) is None
            or game.sk.answer_window_open
            or getattr(game, "_pending_delivery_qnum", None)
            != game.sk.question_number
        ):
            return ""
        return (game.rendered_armed_question() or "").strip()

    def _log_prohibited_content(self, exc, chat_ctx, tools) -> None:
        system_parts: list[str] = []
        state_parts: list[str] = []
        conversation_parts: list[str] = []
        for item in _chat_items(chat_ctx):
            text = _message_text(item)
            role = str(getattr(item, "role", "") or "")
            conversation_parts.append(f"{role}:{text}")
            if role == "system":
                system_parts.append(text)
                if _STATE_BLOCK_MARKER in text:
                    state_parts.append(text)
        tool_names = []
        for tool in tools or []:
            info = getattr(tool, "info", None)
            name = (
                getattr(info, "name", None)
                or getattr(tool, "name", None)
                or type(tool).__name__
            )
            tool_names.append(str(name))
        armed = getattr(self._game, "armed_question", None) or {}
        llm = getattr(self, "llm", None)
        model = (
            getattr(llm, "model", None)
            or getattr(getattr(llm, "_opts", None), "model", None)
            or "unknown"
        )
        logger.error(
            "LILY_LLM | PROHIBITED_CONTENT | session=%s model=%s "
            "request_id=%s q=%s category=%s system_hash=%s state_hash=%s "
            "conversation_hash=%s tools_hash=%s",
            self._game.sk.session_id,
            model,
            getattr(exc, "request_id", None) or "-",
            armed.get("id") or "-",
            armed.get("category") or "-",
            _lily_short_hash("\n".join(system_parts)),
            _lily_short_hash("\n".join(state_parts)),
            _lily_short_hash("\n".join(conversation_parts)),
            _lily_short_hash("|".join(sorted(tool_names))),
        )

    def _maybe_schedule_lobby_empty_stop_recover(self) -> bool:
        """F1 — after empty STOP fail-closed pre-game, re-open the night
        once via gated_say. Returns True when a recover task was armed.

        Caps at one attempt. Skips when the opener already CONFIRMED (later
        lobby banter empties must not re-greet). Uses the inventory key
        only — no new copy. One-emission vs cut recovery: keyed release on
        GENERATION_FAILED does not arm cut recovery; this recover is the
        sole second attempt for the opener.
        """
        game = self._game
        if getattr(game, "game_started", False):
            return False
        if int(getattr(game, "_empty_stop_lobby_recover_count", 0) or 0) >= 1:
            return False
        # HOSTLOOP-001 C7: once a start intent has been heard, the start
        # pipeline owns the lobby — a recovery here must not speak AT ALL
        # (the false "welcome back" of Session A was this path answering
        # the dead air after "Starts." with the rejoin script, because a
        # stale same-room checkpoint had set `reconnected`). The rejoin
        # script is reserved for the GENUINE reconnect re-entry at
        # on_enter; an in-session start never runs it.
        if getattr(game, "_start_intent_heard", False):
            logger.info(
                "LILY_LOBBY | RECOVER_SKIPPED | session=%s reason="
                "start_intent_heard — kickoff owns the floor, no re-greet",
                game.sk.session_id,
            )
            return False

        if getattr(game, "reconnected", False):
            key, act = "session_rejoin", "rejoin"
            instructions_fn = getattr(game, "rejoin_instructions", None)
        else:
            key, act = "session_greet", "greet"
            instructions_fn = getattr(game, "greeting_instructions", None)
        if not callable(instructions_fn):
            return False

        registry = getattr(game, "say_registry", None)
        if registry is not None:
            try:
                if registry.state(key) == lily_say_gate.CLAIM_CONFIRMED:
                    return False
            except Exception:
                pass

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False

        game._empty_stop_lobby_recover_count = (
            int(getattr(game, "_empty_stop_lobby_recover_count", 0) or 0) + 1
        )
        attempt = game._empty_stop_lobby_recover_count
        session_id = getattr(getattr(game, "sk", None), "session_id", "?")

        async def _recover() -> None:
            # Let GENERATION_FAILED release the failed handle's claim so
            # gated_say can re-claim (dup would otherwise suppress).
            await asyncio.sleep(0.15)
            try:
                if registry is not None and registry.state(key) == (
                    lily_say_gate.CLAIM_PENDING
                ):
                    registry.release(key)
                logger.error(
                    "LILY_LLM | EMPTY_STOP_LOBBY_RECOVER | session=%s "
                    "key=%s attempt=%d — re-dispatching opener after "
                    "empty STOP fail-closed",
                    session_id, key, attempt,
                )
                game.gated_say(
                    key,
                    act,
                    instructions_fn(),
                    source="empty_stop_lobby_recover",
                )
            except Exception as e:
                logger.exception(
                    "LILY_LLM | EMPTY_STOP_LOBBY_RECOVER_FAILED | "
                    "session=%s key=%s — %s",
                    session_id, key, e,
                )

        loop.create_task(_recover())
        return True

    def _thinking_level_for_turn(self, chat_ctx) -> str:
        """'high'/'low' for THIS turn from the last user message. Only user
        turns escalate; instruction-driven generations (reveal, greeting,
        tool follow-ups — no user turn) stay LOW."""
        try:
            items = (
                getattr(chat_ctx, "items", None)
                or getattr(chat_ctx, "messages", None)
                or []
            )
            for item in reversed(list(items)):
                if getattr(item, "role", None) != "user":
                    continue
                content = getattr(item, "content", None)
                if isinstance(content, (list, tuple)):
                    text = " ".join(str(c) for c in content if isinstance(c, str))
                else:
                    text = str(content or "")
                return _lily_thinking_level_for_text(text)
        except Exception:
            pass
        return "low"

    async def tts_node(self, text, model_settings):
        chunks = []
        async for chunk in text:
            chunks.append(chunk)
        raw = "".join(chunks).strip()

        # REFACTOR W1b: the ~600-line sequential surgery is now a named,
        # independently-testable pipeline (see SpeechTransform / SAY_PIPELINE
        # above). Behavior is byte-preserved; the speech_id is pinned once (the
        # SpeechHandle ContextVar is stable for this task).
        turn = SpeechTurn(
            text=raw,
            raw=raw,
            game=self._game,
            agent=self,
            speech_id=_current_speech_id(),
        )
        outcome = run_say_pipeline(turn)
        if isinstance(outcome, Silence):
            if outcome.schedule is not None:
                asyncio.ensure_future(outcome.schedule())
            yield _lily_silence_frame()
            return

        full = turn.text

        # P0-C: this is the exact text handed to TTS after every rewrite, clip
        # and strict delivery substitution. Playout completion consumes it for
        # both RTC and durable transcripts.
        self._game.note_post_tts_text(turn.speech_id, full)

        async def _replay():
            yield full

        async for frame in Agent.default.tts_node(self, _replay(), model_settings):
            yield frame


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def lily_noise_cancellation_options():
    """Resolve the Krisp model for the room input (WS-14). Returns None
    unless LILY_NOISE_CANCELLATION=nc is explicitly set — off is the
    DEFAULT since WO-LILY-HOTFIX-001: the 08-06 P0 (RoomIO audio setup
    wedged with NC on the join path; sessions opened deaf and mute) is
    NC's second documented kill after the 1.6.4 NcSession sample-rate
    SIGABRT. Re-enable path: pass the NC-BENCH-001 cold-join gate on an
    isolated slot first.

    BVC can NEVER come out of this function. BVC isolates the primary
    speaker and suppresses "background voices" — in Lily's one-mic
    multiplayer room those are the other players, so BVC would erase the
    table. lily_config coerces every unknown value (including "bvc") to
    "off"; this resolver only ever constructs noise_cancellation.NC()."""
    if lily_config.noise_cancellation_mode() == "off":
        logger.info(
            "LILY_NOISE | NC=off (default since HOTFIX-001; "
            "LILY_NOISE_CANCELLATION=nc opts in after the bench gate)"
        )
        return None
    logger.info("LILY_NOISE | Krisp ambient NC enabled (BVC excluded by design)")
    return noise_cancellation.NC()


def _setup_session_log(room_name: str) -> None:
    """Optional per-session log file (fleet pattern)."""
    log_dir = lily_config.session_log_dir()
    if not log_dir:
        return
    try:
        logs = Path(log_dir)
        logs.mkdir(parents=True, exist_ok=True)
        cutoff = time.time() - (30 * 86400)
        for old in logs.glob("session_*.log"):
            try:
                if old.stat().st_mtime < cutoff:
                    old.unlink()
            except Exception:
                pass
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        handler = logging.FileHandler(str(logs / f"session_{room_name}_{ts}.log"))
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
        ))
        logging.getLogger().addHandler(handler)
        logger.info("SESSION_LOG_STARTED | room=%s", room_name)
    except Exception as e:
        logger.warning("session log setup failed: %s", e)


# How long the entrypoint waits for the first non-agent participant (and
# their token metadata) to appear in remote_participants. Live evidence
# (2026-07-14 audit): at ctx.connect() return the joining participant is
# often NOT in remote_participants yet, which silently dropped the
# lily_group_id metadata and fell through to the room-random id.
PARTICIPANT_METADATA_WAIT_SECONDS = 3.0


def _quarantine_initial_device_identity(
    resolved_group_id: str,
    resolved_group_source: str,
    room_name: str,
) -> tuple[str, str, str | None]:
    """Return (live id, live source, optional device candidate id)."""
    if resolved_group_source in _DEVICE_CANDIDATE_SOURCES:
        return room_name, "room_name", resolved_group_id
    return resolved_group_id, resolved_group_source, None


async def _resolve_initial_group_id(ctx: JobContext, room_name: str) -> tuple[str, str]:
    """Group identity at job start (persistent memory / voiceprint re-key).
    Priority: (a) lily_group_id from dispatch/job metadata (the token route
    mirrors participant metadata into RoomAgentDispatch — available
    immediately), then from the first non-agent remote participant's token
    metadata (short poll: the participant may join AFTER ctx.connect
    returns); (b) LILY_GROUP_ID env override; (c) room name (legacy fallback
    — random per session; the mid-session upgrade path re-keys off it).
    Always returns (group_id, source) — the caller logs the mandatory
    LILY_MEMORY | GROUP_ID | source=... line."""
    # (a1) dispatch/job metadata — strongest immediately-available signal.
    unreadable_token_seen = False
    try:
        job_meta = getattr(getattr(ctx, "job", None), "metadata", None)
        candidate = lily_memory.lily_parse_group_id_from_metadata(job_meta)
        if candidate:
            return candidate, "dispatch_metadata"
        if job_meta:
            unreadable_token_seen = True
            logger.info(
                "LILY_MEMORY | GROUP_ID | dispatch metadata present but "
                "unparseable: %r", str(job_meta)[:120],
            )
    except Exception as e:
        logger.warning("LILY_MEMORY | GROUP_ID | dispatch metadata read failed: %s", e)

    # (a2) participant token metadata, with a short poll for late arrival.
    def _scan() -> tuple[str | None, int]:
        non_agents = 0
        try:
            for participant in ctx.room.remote_participants.values():
                if (
                    getattr(participant, "kind", None)
                    == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT
                ):
                    continue
                non_agents += 1
                meta = getattr(participant, "metadata", None)
                candidate = lily_memory.lily_parse_group_id_from_metadata(meta)
                if candidate:
                    return candidate, non_agents
                if meta:
                    nonlocal unreadable_token_seen
                    unreadable_token_seen = True
                    logger.info(
                        "LILY_MEMORY | GROUP_ID | participant %s metadata "
                        "present but unparseable: %r",
                        getattr(participant, "identity", "?"), str(meta)[:120],
                    )
        except Exception as e:
            logger.warning("LILY_MEMORY | GROUP_ID | participant scan failed: %s", e)
        return None, non_agents

    wait_seconds = lily_config.participant_metadata_wait_seconds()
    deadline = time.time() + wait_seconds
    saw_participant = False
    while True:
        candidate, non_agents = _scan()
        if candidate:
            return candidate, "participant_metadata"
        saw_participant = saw_participant or non_agents > 0
        # DO NOT break early just because a human is present without usable
        # metadata. That was this resolver's amnesia bug: presence and
        # metadata-readiness are different things. Token metadata is fixed
        # at join, so its VALUE never changes — but it PROPAGATES to the
        # agent asynchronously, and a participant object can appear in
        # room.remote_participants a beat before its `metadata` field
        # syncs. Breaking on presence turned that beat into a coin flip,
        # and a busy or slow-booting agent process loses the flip: the
        # room event loop falls behind, metadata lands late, and the
        # resolver has already given up and minted a throwaway group.
        #
        # The participant_connected upgrade hook does NOT cover this —
        # it only fires for participants who join AFTER it is registered,
        # and this participant is already in the room.
        #
        # Live evidence: 27 of 68 stored sessions ran under a
        # room-name-shaped throwaway group, first seen 2026-07-16, on a
        # deployment whose Silero VAD was measured 33s behind realtime.
        # Every one of those tables was greeted as a stranger with its
        # real history sitting in the database untouched.
        #
        # Polling the full window costs nothing in the common case: when
        # metadata is already there the FIRST scan returns it.
        if time.time() >= deadline:
            if saw_participant:
                logger.warning(
                    "LILY_MEMORY | GROUP_ID | participant(s) present but no "
                    "lily_group_id metadata within %.1fs — the token carried "
                    "none, or it never propagated. Raise "
                    "LILY_PARTICIPANT_METADATA_WAIT if this agent boots slow.",
                    wait_seconds,
                )
            else:
                logger.info(
                    "LILY_MEMORY | GROUP_ID | no non-agent participant within "
                    "%.1fs of connect", wait_seconds,
                )
            break
        await asyncio.sleep(0.25)

    if lily_config.group_id_override():
        return lily_config.group_id_override(), "env_override"
    # WO-LILY-HOTFIX-002 Defect 2: minting a throwaway group is ALWAYS
    # loud, with the discriminating reason on the line — silent amnesia
    # (a full session keyed to a random id with nobody told why) is the
    # defect class this WARN exists to make impossible.
    logger.warning(
        "LILY_MEMORY | THROWAWAY_GROUP_MINTED | group=%s reason=%s — "
        "session runs memoryless unless a voiceprint upgrade lands",
        room_name,
        "token_unreadable" if unreadable_token_seen else "no_token_present",
    )
    return room_name, "room_name"


async def _lily_voice_probe_fork(track, game) -> None:
    """Background frame sink for durable voice identity: read a participant's
    audio track, resample to 16 kHz mono, and keep a rolling PCM probe on the
    game for the embedder (match-at-start / enroll-at-close read it). Never
    on the vocal path, never raises into the session — a failure just leaves
    the probe empty and the feature stays inert. The one live-infra seam the
    unit tests can't exercise (no live audio); the buffer/gate it feeds is
    tested via LilyVoiceProbe."""
    try:
        probe = lily_voice_embedder.LilyVoiceProbe(
            target_seconds=lily_config.voice_identity_enroll_min_speech_seconds(),
            match_seconds=lily_config.voice_identity_match_min_speech_seconds(),
        )
        resampler = None
        stream = rtc.AudioStream(track)
        async for ev in stream:
            frame = getattr(ev, "frame", None) or ev
            in_rate = getattr(frame, "sample_rate", lily_voice_embedder.ECAPA_SAMPLE_RATE)
            if in_rate != lily_voice_embedder.ECAPA_SAMPLE_RATE:
                if resampler is None:
                    resampler = rtc.AudioResampler(
                        input_rate=in_rate,
                        output_rate=lily_voice_embedder.ECAPA_SAMPLE_RATE,
                        num_channels=1,
                    )
                for out in resampler.push(frame):
                    probe.add_samples(_frame_int16(out))
            else:
                probe.add_samples(_frame_int16(frame))
            if probe.match_ready() and not getattr(
                game, "_voice_identity_attempted", False
            ):
                # RECOGNITION at the low bar (~2.5s). Waiting for an
                # enrollment-grade sample put the match minutes into the
                # night: live 2026-08-08 it landed correctly at 3m36s,
                # long after the greeting had called a four-win regular a
                # blank slate.
                # V2 instrumentation: t0 = the FIRST match_ready crossing,
                # stamped once. embed_ms is measured from here, so a model
                # still warming when the voice arrives shows up in that delta
                # instead of hiding. Never re-stamped (the pipeline that reads
                # it wants the earliest ready-instant).
                if getattr(game, "_voice_identity_match_t0", None) is None:
                    game._voice_identity_match_t0 = time.monotonic()
                game._voice_identity_pcm = probe.match_pcm()
                game.maybe_start_voice_identity_match()
            if probe.ready():
                game._voice_identity_pcm = probe.pcm()
                game.maybe_start_voice_identity_match()
                # STOP. The probe needs ~8 seconds; this loop was running for
                # the WHOLE SESSION, resampling every frame and doing two
                # full copies of it (bytes(frame.data) -> array) on the
                # event loop, per participant, forever — for audio nothing
                # would ever read again. Enrollment at close reads the
                # captured PCM above, not the live stream.
                #
                # That waste is not free: it is on the same event loop as
                # the Silero VAD, and VAD is what drives barge-in and turn
                # commit. Live 2026-08-08 measured the VAD 24.9s behind
                # realtime, with TTS tail chunks undelivered and turns dying
                # mid-sentence — the choppiness. Holding the loop open past
                # the point of usefulness was buying nothing and costing
                # exactly the thing the agent cannot afford to lose.
                logger.info(
                    "LILY_VOICE_ID | PROBE_COMPLETE | captured=%.1fs — "
                    "closing the frame sink; it has what enrollment needs "
                    "and every further frame is loop time the VAD needs",
                    lily_config.voice_identity_enroll_min_speech_seconds(),
                )
                break
    except Exception as e:
        logger.warning("LILY_VOICE_ID | PROBE_FORK_ENDED | %s", e)


def _frame_int16(frame):
    """int16 samples from an rtc.AudioFrame's raw buffer (mono)."""
    try:
        import array
        return array.array("h", bytes(frame.data))
    except Exception:
        return []


def lily_claim_voice_probe(game) -> bool:
    """Atomically reserve the session's one durable-identity audio fork.

    Independent of audEERING by design: biometric capture is a memory input,
    not an acoustic-analytics feature.
    """
    # Capture readiness, NOT match readiness — the model may still be
    # warming. Requiring a loaded model here meant a cold worker never
    # started the fork and therefore never recognised anybody.
    if not game._voice_capture_allowed() or getattr(
        game, "_voice_probe_forked", False
    ):
        return False
    game._voice_probe_forked = True
    return True


async def _lily_camera_frame_fork(track, game) -> None:
    """VIDEOIN-001 frame sink: keep ONLY the most-recent video frame on the
    game while the camera lane is open, so on_user_turn_completed can attach
    it to one turn. No buffering of history, no storage — each frame
    overwrites the last, and a closed lane drops frames on the floor. Never
    on the vocal path, never raises into the session. The one live-video
    seam the unit tests can't exercise; the state/grounding it feeds is
    fully tested."""
    try:
        stream = rtc.VideoStream(track)
        async for ev in stream:
            if getattr(game.sk, "camera_lane", "off") != "open":
                # Lane closed — hold nothing (transient, user-initiated only).
                game._latest_video_frame = None
                continue
            game._latest_video_frame = getattr(ev, "frame", None) or ev
    except Exception as e:
        logger.warning("LILY_CAMERA | FRAME_FORK_ENDED | %s", e)


def lily_stt_focus_kwargs(known_speakers) -> dict:
    """WO-LILY-STT-001 Q0: the Speechmatics focus kwargs. Returns
    focus_speakers + focus_mode=IGNORE ONLY when focus is enabled AND the
    enrolled set has usable labels; {} otherwise. The non-empty guard is the
    safety invariant — focus_mode=IGNORE with no focus set drops every voice,
    muting the whole table, so it is withheld (loudly) rather than risked."""
    if lily_config.stt_focus_mode() != "ignore":
        return {}
    labels = [s.label for s in (known_speakers or []) if getattr(s, "label", None)]
    if not labels:
        logger.warning(
            "LILY_STT_FOCUS | WITHHELD | reason=no_enrolled_speakers — "
            "focus_mode=IGNORE never enabled on an empty set (would mute the "
            "table)"
        )
        return {}
    return {"focus_speakers": labels, "focus_mode": SpeakerFocusMode.IGNORE}


def lily_stt_config_applied(stt) -> dict:
    """WO-LILY-STT-001 Q3: the EFFECTIVE Speechmatics config, read off the
    constructed STT's _stt_options (what the wire will actually carry) — not
    what we intended to set. Logged at session start and asserted
    intended==applied by test, so the audit's claimed-but-unwired class (the
    max_speakers=7 ghost that was never wired to roster) reads red at build
    time instead of hiding live. Defensive: returns {} if the options object
    isn't present (test stubs)."""
    opts = getattr(stt, "_stt_options", None)
    if opts is None:
        return {}

    def _name(v):
        return getattr(v, "value", None) or getattr(v, "name", None) or str(v)

    return {
        "model": str(getattr(stt, "model", "enhanced")),
        "turn_detection_mode": _name(getattr(opts, "turn_detection_mode", None)),
        "max_delay": getattr(opts, "max_delay", None),
        "speaker_sensitivity": getattr(opts, "speaker_sensitivity", None),
        "max_speakers": getattr(opts, "max_speakers", None),
        "prefer_current_speaker": getattr(opts, "prefer_current_speaker", None),
        "enable_diarization": getattr(opts, "enable_diarization", None),
        "focus_mode": _name(getattr(opts, "focus_mode", None)),
        "focus_speakers": len(getattr(opts, "focus_speakers", None) or []),
        "known_speakers": len(getattr(opts, "known_speakers", None) or []),
        "additional_vocab": len(getattr(opts, "additional_vocab", None) or []),
    }


async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    room_name = ctx.room.name or "unknown"
    _setup_session_log(room_name)

    # --- Voiceprint model: start loading NOW, in a thread ---------------
    # The ECAPA load is a HuggingFace fetch plus a torch init. It used to
    # start on the FIRST TRANSCRIPT, which put a cold-container download
    # directly in front of recognition: live 2026-08-08 the match landed
    # correctly ("NOW I've got you: reigning champion, four wins") 3m36s
    # into the session, long after the greeting had called a four-win
    # regular a blank slate. Kicked here it warms during connect, group
    # resolution and the lobby, so by the time ~2.5s of speech exists the
    # model is ready and recognition is a cosine compare, not a download.
    # Fire-and-forget: failure leaves the feature inert, never blocks boot.
    if lily_config.voice_identity_enabled():
        async def _prewarm_embedder() -> None:
            try:
                ok = await lily_voice_embedder.lily_warm_voice_embedder()
                logger.info(
                    "LILY_VOICE_ID | EMBEDDER_PREWARM | loaded=%s — warmed at "
                    "session start, off the event loop", ok,
                )
            except Exception as e:
                logger.warning("LILY_VOICE_ID | EMBEDDER_PREWARM_FAILED | %s", e)
        asyncio.ensure_future(_prewarm_embedder())

    # --- Group identity resolution (observable: this line MUST appear
    # every session) ---
    resolved_group_id, resolved_group_source = await _resolve_initial_group_id(
        ctx, room_name
    )
    (
        group_id,
        group_id_source,
        device_candidate_group_id,
    ) = _quarantine_initial_device_identity(
        resolved_group_id, resolved_group_source, room_name
    )
    logger.info(
        "LILY_MEMORY | GROUP_ID | source=%s group_id=%s candidate_source=%s "
        "candidate_group=%s",
        group_id_source, group_id,
        resolved_group_source if device_candidate_group_id else "-",
        device_candidate_group_id or "-",
    )

    # --- Session-init: persist when the database answers, HOST regardless ---
    # (Policy change, live 2026-07-15 22:57: Supabase timed out through all
    # three boot retries and the fail-fast rule meant NO LILY AT ALL — a
    # silent no-show for the room. A database outage now costs her the
    # memory layer for the night, never her presence: supabase degrades to
    # None, every persistence path no-ops as designed, and the greeting
    # carries an honest one-liner. Fail-fast remains only for rooms where
    # persistence matters more than presence — none exist today.)
    supabase = lily_persistence.lily_create_supabase_client()
    degraded_no_persistence = False
    try:
        lily_persistence.lily_init_session(supabase, room_name, group_id)
    except RuntimeError as e:
        degraded_no_persistence = True
        supabase = None
        logger.error(
            "LILY_INIT | DEGRADED_NO_PERSISTENCE | room_id=%s — hosting "
            "without memory/persistence for this session (%s)",
            room_name, e,
        )

    # WS-12 reconciliation sweep: assess orphaned pending report rows from
    # prior sessions (aborted before their wrap-up beat, or past assessment
    # failures). Fire-and-forget off the boot path; the sweep never raises
    # and its min-age grace window keeps it off live sessions' rows.
    if supabase is not None:
        # T9 (PATCH-001): close any session that died mid-game BEFORE the
        # report sweep, so the crash-instant ghost (89A97A) is force-ended
        # and then assessed from its stored transcript.
        asyncio.ensure_future(
            lily_persistence.lily_sweep_abandoned_sessions(supabase)
        )
        asyncio.ensure_future(lily_assessment.lily_report_sweep(supabase))

    scorekeeper = LilyScorekeeper(
        session_id=room_name,  # session_id = room name, never random UUIDs
        answer_window_seconds=lily_config.answer_window_seconds(),
    )
    if degraded_no_persistence:
        scorekeeper.set_status_note(
            "memory systems are offline tonight — you can't remember past "
            "games or save this one. Host brilliantly anyway; if it comes "
            "up, one honest light line ('my memory's taking the night "
            "off') and move on. Never invent an explanation."
        )

    # Reconnection: rehydrate scores and round position from checkpoint.
    existing = lily_persistence.lily_check_existing_session(supabase, room_name)
    reconnected = False
    if existing and existing.get("scorekeeper_state"):
        snap = existing["scorekeeper_state"]
        if (snap.get("players") or {}) and snap.get("question_number", 0) > 0:
            scorekeeper.rehydrate(snap)
            reconnected = True

    reasoning = LilyReasoning()
    transcripts = lily_persistence.LilyTranscriptBatcher(supabase, room_name)
    game = LilyGame(
        ctx, scorekeeper, reasoning, supabase, transcripts,
        group_id, group_id_source=group_id_source,
    )

    # V2: preload the voice-identity centroid pool NOW (concurrent with the
    # embedder prewarm above, before any voice), so the first-utterance match
    # compares against an in-memory pool with no DB round-trip on the path.
    game._preload_voice_identities()

    # Acoustic pipeline state (WO-LILY-AUDEERING-001): fresh per session.
    # The child-signal veto callback is wired BEFORE any capture can land —
    # the safety action ships with the pipeline, never after.
    acoustic_state = lily_audeering_consumers.lily_reset_acoustic_state()
    game.acoustic = acoustic_state
    acoustic_state.on_child_signal = game.on_child_signal
    # Monitoring-status hook: audEERING availability never authorizes or
    # revokes adult mode; an actual young-voice signal remains veto-only.
    acoustic_state.on_breaker_open = game.on_child_gate_lost

    # Late device metadata is staged, never promoted directly. A current
    # voiceprint must verify it before any memory enters vocal context.
    def _on_participant_connected(participant) -> None:
        try:
            if game.group_id_source in lily_identity._STRONG_GROUP_SOURCES:
                return
            if (
                getattr(participant, "kind", None)
                == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT
            ):
                return
            candidate = lily_memory.lily_parse_group_id_from_metadata(
                getattr(participant, "metadata", None)
            )
            if (
                candidate
                and candidate != game.group_id
                and candidate != game.device_candidate_group_id
            ):
                asyncio.ensure_future(
                    game.stage_device_candidate(
                        candidate, "participant_metadata_late"
                    )
                )
        except Exception as e:
            logger.warning("LILY_MEMORY | GROUP_ID | late-join scan failed: %s", e)

    ctx.room.on("participant_connected", _on_participant_connected)

    if device_candidate_group_id:
        staged = await game.stage_device_candidate(
            device_candidate_group_id, resolved_group_source
        )
        if not staged:
            game.memory_settled.set()
        # No candidate preference, name, memory block, or known speaker is
        # activated before voice verification.
        game.prefs = {}
        group_memory = {}
    else:
        # Trusted/verified identity: returning memory may enter context.
        game.prefs = await lily_persistence.lily_load_group_prefs(
            supabase, group_id
        )
        group_memory = await lily_memory.lily_load_group_memory(
            supabase, group_id
        )
        game.memory_block = lily_memory.lily_build_memory_block(
            group_memory, prefs=game.prefs
        )
        game.memory_player_names = list(
            (group_memory or {}).get("player_names") or []
        )
        game.memory_total_games = int(
            (group_memory or {}).get("total_games") or 0
        )
        if game.memory_block:
            logger.info(
                "LILY_MEMORY | BLOCK_READY | group=%s chars=%d total_games=%s",
                group_id, len(game.memory_block),
                (group_memory or {}).get("total_games"),
            )
        if group_id_source in lily_identity._STRONG_GROUP_SOURCES or game.memory_block:
            game.memory_settled.set()

    # Bank curation (WO-LILY-OMNIBUS-002 D/F): the group's served-question
    # history (no-repeat guard on bank draws + generated output) and the
    # promoted category-candidate names (lobby state-block line).
    game.asked_history = await lily_bank.lily_load_asked_history(
        supabase, group_id
    )
    game.promoted_categories = await lily_bank.lily_load_promoted_categories(
        supabase
    )

    # Candidate voiceprints may be supplied to Speechmatics as matching
    # hints, but their names/memories remain absent from Lily's context until
    # someone actually speaks and the returned identifiers overlap.
    if device_candidate_group_id:
        known_rows = [
            {
                "label": row.get("player_name") or row.get("speaker_label"),
                "speaker_identifiers": row.get("speaker_identifiers"),
            }
            for row in game._device_candidate_voiceprints
        ]
    else:
        known_rows = await lily_persistence.lily_load_voiceprints(
            supabase, group_id
        )
    # WS-13: dunder-wrapped labels (`__ASSISTANT__`-style) are the engine's
    # ignore namespace — a corrupt voiceprint row must never enroll real
    # identifiers under one (it would turn the inert echo-guard label into
    # an active matcher for a real voice and silently drop that player).
    known_rows = lily_stt_tuning.lily_filter_enrollable_speakers(known_rows)
    known_speakers = [
        SpeakerIdentifier(
            label=row["label"], speaker_identifiers=row["speaker_identifiers"]
        )
        for row in known_rows
        if row.get("label") and row.get("speaker_identifiers")
    ]

    # --- n-best ASR recovery (WO-LILY-ADDRESSEE-H1-001 Task 1) ---
    # Armed BEFORE STT construction so the config injection covers the
    # session's StartRecognition. On any failure the installer logs
    # LILY_NBEST | patch=failed and returns False — the collector stays
    # detached and every downstream path runs clean 1-best.
    _nbest_max = lily_config.stt_max_alternatives()
    _nbest_collector = lily_nbest.LilyNBestCollector(max_hypotheses=_nbest_max)
    if lily_nbest.lily_install_nbest_stt_patch(_nbest_collector, _nbest_max):
        game.nbest_collector = _nbest_collector

    # --- STT tuning injection (WO-LILY-OMNIBUS-003 WS-13) ---
    # Armed BEFORE STT construction, same contract as the n-best patch.
    # Injects `speaker_diarization_config.get_speakers` (end-of-transcript
    # SpeakersResult push — the enrollment fallback that survives stream
    # teardown, replacing the ~5-word-starved GET_SPEAKERS polling as the
    # only source) and `audio_filtering_config.volume_threshold` (already on
    # every wire at 0.0 — schema-safe, unlike max_alternatives). Both fields
    # live-validated against the voice endpoint 2026-08-05: RecognitionStarted,
    # no 1003.
    lily_stt_tuning.lily_install_stt_tuning_patch(
        get_speakers=True,
        volume_threshold=float(
            lily_stt_tuning.LILY_STT_TUNED["wire_injection"]["volume_threshold"]
        ),
    )

    # --- STT: Speechmatics multi-speaker fleet profile (Part II §1.1) ---
    # livekit-plugins-speechmatics 1.6.8 still exposes only the deprecated
    # operating_point kwarg. LilySpeechmaticsSTT preserves the plugin surface
    # but maps ENHANCED onto speechmatics-rt TranscriptionConfig.model, so no
    # deprecated field reaches the SDK/wire.
    # WS-13: constructor values come from the tuned artifact
    # (lily_stt_tuning.LILY_STT_TUNED — rationale per lever in the README
    # STT stack table). speaker_sensitivity 0.5 -> 0.35: the echo-room
    # session minted 3 phantom labels + 1 continuity split for 4 players at
    # the default; lower sensitivity biases matching toward enrolled voices
    # over minting generic ones. max_speakers stays 7 at construction —
    # the table size is unknowable pre-bind; the roster-aware cap
    # (lily_max_speakers_for) is WS-8's to apply via the 1.6.6
    # Agent.update_options(stt=...) swap. Known player names ride
    # additional_vocab — the bounded stable set the constructor-only pin
    # allows (never answer nouns).
    _tuned = lily_stt_tuning.lily_tuned_stt_kwargs()
    # Voiceprint labels are player names when a binding existed; generic
    # engine labels (S1..Sn / UU) are not names and stay out of the vocab.
    _vocab_names = sorted({
        name
        for row in known_rows
        for name in [str(row.get("label") or "").strip()]
        if name and not re.fullmatch(r"S\d+|UU", name)
    })
    # WO-LILY-STT-001 Q0: speaker focus — enrolled players are the game,
    # every other voice is room. focus_mode=IGNORE drops unenrolled speech at
    # the engine. HARD-GUARDED: only when explicitly enabled AND the enrolled
    # set is non-empty (an empty/absent focus set under IGNORE would silently
    # delete the WHOLE table), so it can never mute a session by
    # misconfiguration. Default off (see lily_config.stt_focus_mode).
    _focus_kwargs = lily_stt_focus_kwargs(known_speakers)
    stt = LilySpeechmaticsSTT(
        prefer_current_speaker=True,  # [VERIFY live] rapid answer collisions
        turn_detection_mode=TurnDetectionMode.FIXED,
        additional_vocab=[
            AdditionalVocabEntry(content="Lily"),
            *(AdditionalVocabEntry(content=name) for name in _vocab_names),
        ],
        known_speakers=known_speakers,
        **_focus_kwargs,
        **{k: v for k, v in _tuned.items() if k != "prefer_current_speaker"},
    )
    game.stt = stt
    # HOTFIX-005 X8: capture a faithful STT rebuilder so a game-start,
    # roster-aware max_speakers retune can produce an otherwise-identical STT
    # with only the cap changed. Stored (not run) here; the game invokes it
    # at start behind the default-off LILY_STT_ROSTER_RETUNE flag, and only
    # ever to SHRINK the cap toward the real table size (fallback 7 already
    # covers large tables — this only kills phantom labels on small ones).
    def _rebuild_stt(max_speakers_override: int):
        _retuned = lily_stt_tuning.lily_tuned_stt_kwargs()
        _retuned["max_speakers"] = max_speakers_override
        return LilySpeechmaticsSTT(
            prefer_current_speaker=True,
            turn_detection_mode=TurnDetectionMode.FIXED,
            additional_vocab=[
                AdditionalVocabEntry(content="Lily"),
                *(AdditionalVocabEntry(content=name) for name in _vocab_names),
            ],
            known_speakers=known_speakers,
            **_focus_kwargs,
            **{k: v for k, v in _retuned.items() if k != "prefer_current_speaker"},
        )

    game._stt_rebuild = _rebuild_stt
    game._stt_max_speakers_applied = int(_tuned.get("max_speakers", 7))
    game._stt_roster_retuned = False
    # Q3 attestation: log the EFFECTIVE config as-applied (intended==applied
    # is asserted by test) and stash it for the session report — every knob
    # the build believes it set, proven at the wire.
    game.stt_config_applied = lily_stt_config_applied(stt)
    logger.info(
        "LILY_STT | CONFIG_APPLIED | %s",
        " ".join(f"{k}={v}" for k, v in game.stt_config_applied.items()),
    )
    if known_speakers:
        logger.info("VOICEPRINT | injected %d known speakers", len(known_speakers))
    if _focus_kwargs:
        logger.warning(
            "LILY_STT_FOCUS | ENABLED | focus_speakers=%d mode=IGNORE — "
            "unenrolled voices dropped at the engine",
            len(_focus_kwargs.get("focus_speakers") or []),
        )

    # --- Session: one Grok 4.5 vocal host across general + adult layers ---
    general_vocal_llm = lily_build_grok_vocal_llm(
        model=lily_config.vocal_model(),
        api_key=lily_config.xai_api_key(),
        effort=lily_config.vocal_effort(),
        conversation_id=game.grok_conversation_id,
    )
    game._general_llm = general_vocal_llm
    game._adult_llm = (
        general_vocal_llm
        if (
            lily_config.adult_vocal_model() == lily_config.vocal_model()
            and lily_config.adult_vocal_effort() == lily_config.vocal_effort()
        )
        else None
    )
    lily_tts_instance = LilyTTS()  # voice1 (primary)
    game.tts = lily_tts_instance  # P7: reachable for set_delivery_pace
    session = AgentSession(
        userdata={"scorekeeper": scorekeeper, "game": game},
        stt=stt,
        llm=general_vocal_llm,
        tts=lily_tts_instance,  # voice1 (primary) via lily_config.lily_voice_id()
        vad=silero.VAD.load(),  # barge-in enabled; no STT gating during TTS
        # THE READ TIMEOUT THAT ACTUALLY APPLIES. 0f31b71 raised the adult
        # lane's client read budget to 30s because livekit-plugins-openai
        # built its own AsyncClient with httpx.Timeout(read=5.0) — correct
        # diagnosis, ineffective fix. The plugin's LLMStream INHERITS from
        # livekit.agents.inference.llm.LLMStream, which passes
        # `timeout=httpx.Timeout(self._conn_options.timeout)` on every
        # create() — and in openai-python a per-request timeout REPLACES
        # the client's. So the real wall was DEFAULT_API_CONNECT_OPTIONS'
        # 10s, on both lanes, and the 30s client budget never applied.
        #
        # Worse, max_retry defaults to 3: one wedged first token became
        # ~40s of dead air on a voice-first game. Against grok-4.5 measured
        # at llm_ttft p95 8.3s and this session's 7.3s, a 10s wall is a
        # coin flip at the tail.
        conn_options=SessionConnectOptions(
            llm_conn_options=APIConnectOptions(
                timeout=lily_config.adult_vocal_read_timeout(),
                max_retry=1,        # one retry, not three: 2x the wall, not 4x
                retry_interval=0.5,
            ),
        ),
        # HOTFIX-005 X9: raise the endpointing floor so a slow enhanced-point
        # STT (max_delay 1.5s) delivers its final transcript BEFORE the turn
        # commits — the runtime's own remedy for the split-utterance class
        # ("consider raising min_delay in the endpointing options to
        # accommodate a slow stt", 14:33:18). Ceiling pinned to the framework
        # default; set together with the Speechmatics max_delay (STT-001).
        turn_handling=TurnHandlingOptions(
            endpointing=EndpointingOptions(
                # Preserve the existing FIXED behavior; this is an API
                # migration, not a Turn Detector/default-mode change.
                mode="fixed",
                min_delay=lily_config.stt_min_endpointing_delay(),
                max_delay=lily_config.stt_max_endpointing_delay(),
            ),
            interruption=InterruptionOptions(
                min_words=1,
                min_duration=lily_config.interruption_min_duration(),
                # WS-14: pinned explicitly (they match 1.6.6 defaults) —
                # a noise burst that produces NO transcript pauses playout
                # and resumes from the pause point after the timeout,
                # instead of hard-cutting the turn. This is the
                # framework-layer end of the verbatim-replay storm: the
                # cut→release→re-dispatch loop only starts when a trigger
                # actually interrupts. Requires can_pause audio output
                # (room output: pause=True, room_io/_output.py).
                resume_false_interruption=True,
                false_interruption_timeout=(
                    lily_config.false_interruption_timeout()
                ),
                # WS-14: adaptive interruption (GA on Cloud). Speechmatics
                # qualifies (capabilities.aligned_transcript="chunk",
                # streaming=True) and the session's turn detection resolves
                # to "vad" — all gates in 1.6.6's
                # _resolve_interruption_detection pass. On any detector
                # failure the framework logs and degrades to plain VAD
                # interruption by itself; "vad" here is the manual pin-back.
                mode=lily_config.interruption_mode(),
            ),
        ),
    )
    game.session = session

    # --- Shutdown gate: 30s for persistence before teardown ---
    shutdown_gate = asyncio.Event()
    heartbeat_stop = asyncio.Event()

    async def _wait_for_persistence() -> None:
        try:
            await asyncio.wait_for(
                shutdown_gate.wait(),
                timeout=lily_config.shutdown_timeout_seconds(),
            )
        except asyncio.TimeoutError:
            logger.warning("SHUTDOWN_GATE | timed out — proceeding with shutdown")

    ctx.add_shutdown_callback(_wait_for_persistence)

    # --- Latency metrics (§11.6): fire-and-forget capture ---
    metrics_raw: dict[str, list[float]] = {
        "first_token_latency_ms": [],
        "tts_first_frame_ms": [],
        "e2e_latency_ms": [],
        # V2: the two voice-identity stage timings (one match per session,
        # so single-element buckets). embed_ms = utterance-ready -> embedding;
        # resolve_ms = embedding -> match decision.
        "voice_id_embed_ms": [],
        "voice_id_resolve_ms": [],
    }
    _METRICS_CAP = 500

    # WO-LILY-UPGRADE-168 U3(b) + "use all the metrics she can": the 1.6.8
    # BLESSED metrics surface (the coupling audit confirmed `metrics_collected`
    # is deprecated since 1.6.0 and warns on every event — so we do NOT
    # subscribe to it). Two first-class sources: per-turn `ChatMessage.metrics`
    # (read below off conversation_item_added) for latency + turn-taking, and
    # `session_usage_updated` for the token/character/audio rollup. Both fold
    # into one session-report block; the turn-taking (transcription /
    # end-of-turn) delays double as WO-LILY-STT-001 Q2's incoming-quality
    # signals.
    session_metrics = lily_metrics.LilyMetricsCollector()

    # HOTFIX-007 Y1c: per-call cache accounting off the LLM COMPONENT's
    # `metrics_collected` (first-class at 1.6.8 — the U3(b) deprecation is
    # only on the AgentSession-level subscription, still avoided). Only the
    # per-call LLMMetrics carries prompt_cached_tokens, the number that
    # proves whether the Y1a static prefix actually cache-hits at Grok.
    def _wire_llm_metrics(llm) -> None:
        # collect_llm_call_soon, not collect_llm_call: the framework's
        # sibling subscriber stamps speech_id onto the event in place, and
        # emitter subscriber order is a coin flip — the deferred fold
        # always sees the stamp (wave-1 review, HIGH finding).
        llm.on(
            "metrics_collected",
            lambda m: session_metrics.collect_llm_call_soon(m),
        )

    _wire_llm_metrics(general_vocal_llm)
    game._llm_metrics_wire = _wire_llm_metrics
    # Y2 measurement gate: count the framework's preemptive-invalidation
    # warnings (and, when debug is on, the used-lines) off its own logger.
    # The settle-vs-volatile-split decision closes on this number.
    session_metrics.attach_preemptive_tap()
    # HOSTLOOP-001 C12: survival must be MEASURED — create the framework's
    # debug used-records for the tap to count, with root handlers shielded
    # from the debug flood (log output unchanged).
    session_metrics.enable_preemptive_used_capture()

    @session.on("session_usage_updated")
    def _on_session_usage(ev) -> None:
        session_metrics.collect_session_usage(getattr(ev, "usage", None))

    @session.on("conversation_item_added")
    def _on_item_added(ev) -> None:
        msg = ev.item
        role = getattr(msg, "role", None)
        # Every item (user AND assistant) carries a MetricsReport; feed the
        # WHOLE report so agent-turn latency and user-turn turn-taking both
        # land — "all the metrics she can".
        report = getattr(msg, "metrics", None)
        session_metrics.collect_turn(report)
        if role == "assistant":
            # HOTFIX-008 Z1: stamped write — the buffer carries the id of
            # the chat item it came from, so a reader keyed to any other
            # generation can never borrow this text (last_assistant_text_for).
            game._last_assistant_turn = (
                str(getattr(msg, "id", "") or ""), _message_text(msg)
            )
            # HOTFIX-005 X1: SCORE_DIVERGENCE — if her spoken turn narrates a
            # score matching no committed-ledger total, the number is
            # fabricated. Log at ERROR (the state block feeds her the truth;
            # this is the safety net that makes a divergence loud).
            try:
                div = lily_scorekeeper.lily_narrated_score_divergence(
                    game._last_assistant_text, game.sk.ledger_scores()
                )
                if div is not None:
                    logger.error(
                        "LILY_SCORE | SCORE_DIVERGENCE | session=%s spoken=%s "
                        "ledger=%s — narrated score off-ledger",
                        game.sk.session_id, div["spoken"], div["ledger_values"],
                    )
            except Exception:
                pass
            # HOTFIX-006 N13: the same safety net for the ROSTER COUNT. Live:
            # "Whenever you four..." to a table of three, right after naming
            # all three. The state block injects the authoritative count
            # (_roster_authority_line); this makes a prevention failure loud
            # in the session it happens in, exactly as X1 does for scores.
            try:
                rdiv = lily_scorekeeper.lily_narrated_roster_count_divergence(
                    game._last_assistant_text, list(game.sk.players)
                )
                if rdiv is not None:
                    logger.error(
                        "LILY_ROSTER | ROSTER_DIVERGENCE | session=%s "
                        "spoken=%s roster=%s names=%s — narrated a player "
                        "count that is not the enrolled table",
                        game.sk.session_id, rdiv["spoken"], rdiv["roster"],
                        ",".join(rdiv["names"]),
                    )
            except Exception:
                pass
            # HOTFIX-006 N2: the same safety net for CUSTOM ROUNDS. In
            # lily-16A9AE she narrated a Cape Cod round twice with nothing
            # registered under it, and no log said so — the fiction was only
            # discovered by reading lily_asked_history days later. The tool
            # result and the state block prevent; this makes a prevention
            # failure loud at ERROR, in the session it happens in.
            try:
                topic = lily_narrated_custom_round_divergence(
                    game._last_assistant_text,
                    game.custom_round_unbuilt_topics(),
                )
                if topic is not None:
                    logger.error(
                        "LILY_CUSTOM_ROUND | CUSTOM_ROUND_DIVERGENCE | "
                        "session=%s topic=%r — narrated a round with zero "
                        "registered questions",
                        game.sk.session_id, topic,
                    )
            except Exception:
                pass
            # HOTFIX-006 N9 part 3: the same safety net for VERDICTS. At
            # 21:10 she said "Jupiter was spot on, Rami, but just a split
            # second late!" while Rami's committed q_1052 row read
            # incorrect with transcript "Go." — the conversational lane
            # narrated correctness over a ledger recording a DIFFERENT
            # utterance as wrong. A verdict spoken about a player's answer
            # generates from that player's ledger row; on disagreement the
            # ledger wins and the divergence is made loud here.
            try:
                vdiv = lily_scorekeeper.lily_narrated_verdict_divergence(
                    game._last_assistant_text, game.sk.score_ledger
                )
                if vdiv is not None:
                    logger.error(
                        "LILY_SCORE | SCORE_DIVERGENCE | session=%s "
                        "player=%s spoken=%s ledger=%s question=%s "
                        "ledger_transcript=%r utterance=%s — narrated "
                        "verdict contradicts the committed row; the LEDGER "
                        "wins",
                        game.sk.session_id, vdiv["player"], vdiv["spoken"],
                        vdiv["ledger"], vdiv["question_id"],
                        str(vdiv["ledger_transcript"])[:80],
                        vdiv["utterance_id"],
                    )
            except Exception:
                pass
            m = report or {}
            get = (lambda k: m.get(k)) if isinstance(m, dict) else (
                lambda k: getattr(m, k, None)
            )
            # Legacy rolling averages kept for the mid-game heartbeat's
            # pipeline_latency line (bounded ring buffer).
            for key, field in (
                ("llm_node_ttft", "first_token_latency_ms"),
                ("tts_node_ttfb", "tts_first_frame_ms"),
                ("e2e_latency", "e2e_latency_ms"),
            ):
                val = get(key)
                if val and val > 0:
                    bucket = metrics_raw[field]
                    bucket.append(round(val * 1000, 1))
                    if len(bucket) > _METRICS_CAP:
                        bucket.pop(0)

    # --- Transcript-event layer: scorekeeper + deterministic enforcement ---
    @session.on("user_input_transcribed")
    def _on_transcribed(ev: UserInputTranscribedEvent) -> None:
        speaker_label = getattr(ev, "speaker_id", None)
        text = re.sub(r"^\s*\[S\d+\]\s*", "", ev.transcript or "").strip()
        if not text:
            return
        if not ev.is_final:
            return  # partials display, finals score — never the reverse
        # Glass transcript (2026-08-09): forward the final to the panel —
        # text_output=False silenced the framework's own forwarding.
        game.publish_user_transcript_nowait(
            text,
            speaker_label=speaker_label,
            utterance_id=(
                getattr(ev, "item_id", None)
                or getattr(ev, "id", None)
                or getattr(ev, "transcript_id", None)
            ),
        )
        # Event arrival wall-clock (created_at) plus recovered STT
        # stream-relative timings from the n-best collector feed the
        # timestamp reconciler for "first answered first" ordering under
        # jitter.
        created = getattr(ev, "created_at", None)
        arrival_ts = (
            created.timestamp() if hasattr(created, "timestamp") else time.time()
        )
        # HOTFIX-006 N9: the utterance's OWN transcript id, when the event
        # carries one. Answer capture binds this — never "most recent",
        # never "first-seen fragment for that speaker" (the live q_1052 row
        # recorded Rami's "Go." while "Okay. It's Jupiter." never entered
        # the ledger). The field name has drifted across plugin versions,
        # so read defensively; the scorekeeper mints a stable id when the
        # event supplies none, so the binding never degrades to a slot.
        utterance_id = (
            getattr(ev, "item_id", None)
            or getattr(ev, "id", None)
            or getattr(ev, "transcript_id", None)
        )
        # n-best (WO-ADDRESSEE-H1 Task 1): drain the per-word alternatives
        # buffered off raw AddTranscript for this speaker's finalized
        # utterance. None when the patch isn't armed or nothing buffered —
        # every consumer treats None as plain 1-best.
        nbest = (
            game.nbest_collector.drain(speaker_label=speaker_label)
            if game.nbest_collector is not None
            else None
        )
        reconciler = getattr(game, "timestamp_reconciler", None)
        if reconciler is None:
            reconciler = lily_nbest.LilyTimestampReconciler()
            game.timestamp_reconciler = reconciler
        timing = reconciler.reconcile(
            arrival_ts=arrival_ts,
            stream_start=(nbest or {}).get("stream_start_time"),
            stream_end=(nbest or {}).get("stream_end_time"),
        )
        try:
            seg_start_ts = float(timing.get("start_time"))
        except Exception:
            seg_start_ts = arrival_ts
        try:
            seg_end_ts = float(timing.get("end_time"))
        except Exception:
            seg_end_ts = seg_start_ts
        if nbest is not None:
            nbest["segment_timing"] = timing
        diarization_confidence = lily_diarization_confidence_from_nbest(nbest)
        acoustic_confidence = _aligned_acoustic_confidence(
            game, seg_start_ts
        )
        fused_conf = _segment_addressee_confidence(
            game,
            event=ev,
            speaker_label=speaker_label,
            diarization_confidence=diarization_confidence,
            acoustic_confidence=acoustic_confidence,
        )
        result = scorekeeper.on_transcript_segment(
            text=text,
            speaker_label=speaker_label,
            is_final=True,
            segment_start_time=seg_start_ts,
            segment_end_time=seg_end_ts,
            diarization_confidence=diarization_confidence,
            acoustic_confidence=acoustic_confidence,
            timestamp_source=timing.get("source"),
            timing_drift_seconds=timing.get("drift_seconds"),
            now=arrival_ts,
            addressee_confidence=fused_conf,
            utterance_id=utterance_id,
        )
        if result.get("quarantined"):
            # WS-10: an insane final (span/lag beyond the sanity gate) is
            # game-inert past this point — the scorekeeper logged it in
            # full; it must not buffer for replay, feed intake ordering,
            # or reach the enforcement layer. The raw text stays in the
            # session transcript store.
            transcripts.add(
                text,
                speaker_label=speaker_label,
                segment_start=seg_start_ts,
                segment_end=seg_end_ts,
            )
            return
        # Fragment accumulator (name extraction) sits BELOW the gate —
        # quarantined stale text never feeds intake name guesses.
        combined_name_fragments = game.fragments.add(
            speaker_label or "UU", text
        )
        explicit_name = lily_extract_explicit_name(combined_name_fragments)
        if explicit_name:
            game.note_confirmed_name_evidence(
                speaker_label or "UU", explicit_name
            )
        # Intake choreography (self-knowledge WO Task 4): pre-game only,
        # a timestamp overlap between two different voices feeds the
        # ordering-repair note — diarization binding degrades exactly
        # here (first contact, no voiceprints), so she orders, not guesses.
        if not game.game_started:
            game.note_intake_overlap(speaker_label, seg_start_ts, seg_end_ts)
        else:
            # Early-buzz capture (fixture Q5): a final landing while the
            # delivery turn is still playing buffers for replay at window
            # open — no-op unless a delivery is actually in flight.
            seg = {
                "text": text,
                "speaker_label": speaker_label,
                "segment_start_time": seg_start_ts,
                "segment_end_time": seg_end_ts,
                "diarization_confidence": diarization_confidence,
                "acoustic_confidence": acoustic_confidence,
                "timestamp_source": timing.get("source"),
                "timing_drift_seconds": timing.get("drift_seconds"),
                "addressee_confidence": fused_conf,
                # N9: the identity travels with the buffered final, so an
                # early answer replayed at window open binds to the SAME
                # utterance it was captured as.
                "utterance_id": utterance_id,
            }
            # A correct answer during an MC options read or a freeform
            # question truncates the remaining read and adjudicates early
            # (buffers this seg + opens the window itself). Otherwise buffer
            # for the normal replay-at-open path.
            if not game.early_answer_check(
                seg, now=arrival_ts, nbest=nbest
            ):
                game.buffer_pre_window_answer(seg)
        player = result.get("player")
        transcripts.add(
            text,
            speaker_label=speaker_label,
            speaker_name=player,
            segment_start=seg_start_ts,
            segment_end=seg_end_ts,
        )
        game.on_transcript_event(
            result, text, speaker_label=speaker_label, segment_ts=seg_start_ts,
            nbest=nbest,
        )

    # --- Answer window opens on TTS playback completion (per-utterance
    # precise via SpeechHandle.wait_for_playout; no dedicated
    # playout-finished session event exists at 1.6.6 either) ---
    @session.on("speech_created")
    def _on_speech_created(ev) -> None:
        handle = ev.speech_handle
        # T1 (PATCH-001): track the live handle so a released claim can
        # CANCEL its speech — a late start must never air after release.
        game.note_speech_handle(handle)

        async def _watch() -> None:
            await handle.wait_for_playout()
            # 1.6.6 semantic change: a failed generation no longer raises out
            # of wait_for_playout (the error moved to SpeechHandle.exception());
            # at 1.6.4 the raise killed this watcher, so a failed speech never
            # reached on_agent_speech_finished. Map failure to the suppressed
            # path — claims release instead of confirming, the turn is not
            # recorded as heard, and (better than 1.6.4) preemptive resume
            # still fires. getattr: test fakes predate exception().
            failed = False
            try:
                exc_fn = getattr(handle, "exception", None)
                speech_exc = exc_fn() if callable(exc_fn) else None
                if speech_exc is not None:
                    failed = True
                    logger.warning(
                        "LILY_SPEECH | GENERATION_FAILED | speech_id=%s exc=%r "
                        "— routing to suppressed path (claims release, turn "
                        "not recorded)",
                        getattr(handle, "id", "?"), speech_exc,
                    )
            except Exception as e:
                logger.warning(
                    "LILY_SPEECH | exception() probe failed on speech_id=%s: %r",
                    getattr(handle, "id", "?"), e,
                )
            spoken, _had_items = _handle_spoken_text(handle)
            # HOTFIX-008 Z1: the itemless fallback that stood here
            # (`if not spoken and not had_items: spoken =
            # game._last_assistant_text`, HOTFIX-002's narrowing of an
            # older unconditional one) is DELETED, not narrowed again. An
            # invalidated preemptive generation reaches this watcher
            # itemless with interrupted=True; the fallback fabricated the
            # PREVIOUS committed turn — whose item lands in the buffer at
            # generation commit, BEFORE its own playout record — so the
            # phantom recorded that turn's text marked "…[cut off]" and
            # then the real turn's own record died on the verbatim-dup
            # guard (20 phantom rows in lily-938EFF-2260354c, each
            # replacing the real row). Empty is the truth for a handle
            # that aired nothing: record_agent_turn and
            # publish_agent_transcription_nowait both no-op on empty
            # text, while a genuine barge-in still carries its real
            # partial (had_items=True).
            suppressed_ids = getattr(game, "_suppressed_speech_ids", set())
            suppressed = handle.id in suppressed_ids
            suppressed_ids.discard(handle.id)
            game.on_agent_speech_finished(
                spoken,
                speech_id=handle.id,
                interrupted=handle.interrupted,
                suppressed=suppressed or failed,
                failed=failed,
            )

        asyncio.ensure_future(_watch())

    # WS-14 validation surface: one line per false-interruption event so
    # barge-in-vs-backchannel behavior is a log query against live
    # sessions (resumed=True: noise burst paused-and-resumed playout;
    # resumed=False: pause window was superseded before resume).
    @session.on("agent_false_interruption")
    def _on_false_interruption(ev) -> None:
        logger.warning(
            "LILY_INTERRUPT | FALSE_INTERRUPTION | session=%s resumed=%s",
            scorekeeper.session_id, getattr(ev, "resumed", None),
        )

    @session.on("user_state_changed")
    def _on_user_state(ev) -> None:
        # P0-2 BE8D8B: the LLM tried lily_begin_round while the next
        # (18-second) setup segment was still being spoken. VAD state is the
        # only truth available before that final transcript lands.
        #
        # Y7 (HOTFIX-007) rides the SAME subscription: this is the VAD layer,
        # and it is the only place the cause of a cut is knowable before the
        # framework acts on it. note_user_speech_state keeps _user_speaking
        # and stamps the falling edge so `cut_was_deliberate_barge_in` can
        # answer "did a human end that turn?" after the fact.
        game.note_user_speech_state(ev.new_state == "speaking")
        if game._user_speaking:
            logger.info(
                "LILY_SETUP | USER_SPEAKING | session=%s — kickoff blocked",
                scorekeeper.session_id,
            )

    @session.on("agent_state_changed")
    def _on_agent_state(ev) -> None:
        # HOST_SPEAKING prior (WO-ADDRESSEE-H1 Task 2): the framework's
        # agent-state machine is the speech lifecycle at 1.6.6 — verified
        # in agent_activity.py: `speaking` is entered when TTS playout
        # actually starts (started_speaking_at) and left for
        # listening/thinking when playout ends or is interrupted. The pure
        # scorekeeper only holds the flag; this is the one wiring point.
        scorekeeper.host_speaking = ev.new_state == "speaking"
        if ev.new_state == "speaking":
            # Stale-claim recovery (WO-LILY-HOTFIX-001): mark the airing
            # speech so its pending claims read as in-flight, not wedged.
            current = getattr(session, "current_speech", None)
            game.note_playout_started(getattr(current, "id", None))
        if ev.new_state == "speaking" and game._pending_reveal_event is not None:
            # Reveal packet keyed to TTS PLAYBACK start of the reveal turn
            # (visuals may lead audio; never keyed to LLM generation).
            ev_payload, game._pending_reveal_event = (
                game._pending_reveal_event, None,
            )
            game.send_event_nowait("reveal", ev_payload)

    # --- Session close: final persistence, gate release ---
    @session.on("close")
    def _on_close(ev) -> None:
        async def _persist() -> None:
            try:
                heartbeat_stop.set()
                # 2026-08-06 log audit: the idle watchdog must die WITH the
                # session — its post-close ticks dispatched against a dead
                # AgentSession (TICK_FAILED once per hangup).
                game.stop_idle_watchdog()
                # Difficulty self-tuning + retirement (sub-agent E):
                # session-end job, fire-and-forget — it runs concurrently
                # with the awaited persistence writes below and is never
                # allowed to block (or fail) the shutdown gate.
                asyncio.ensure_future(
                    lily_bank_tuning.lily_run_bank_tuning(supabase)
                )
                if game.audeering_pipeline is not None:
                    try:
                        await game.audeering_pipeline.stop()
                    except Exception as e:
                        logger.warning("LILY_AUDEERING | stop failed: %s", e)
                await transcripts.flush()
                # WO-LILY-HOTFIX-002 Defect 2: a session ending with its
                # device candidate still quarantined is the silent-amnesia
                # outcome — say so, with the attempt count, so the next
                # log bundle discriminates "verification never ran" from
                # "ran and never matched".
                if getattr(game, "device_candidate_group_id", None) and not (
                    getattr(game, "device_identity_verified", False)
                ):
                    logger.warning(
                        "LILY_MEMORY | DEVICE_CANDIDATE_UNRESOLVED | "
                        "session=%s candidate=%s source=%s "
                        "verify_attempts=%d — session ends memoryless; "
                        "group stays %s",
                        scorekeeper.session_id,
                        game.device_candidate_group_id,
                        getattr(game, "device_candidate_source", "?"),
                        getattr(game, "_device_verify_attempts", 0),
                        game.group_id,
                    )
                standings = sorted(
                    game._players_payload(), key=lambda p: -p["score"]
                )
                # V2: fold the session's single voice-identity stage timings
                # into pipeline_latency (stamped on the game during the match).
                for _field, _attr in (
                    ("voice_id_embed_ms", "_voice_id_embed_ms"),
                    ("voice_id_resolve_ms", "_voice_id_resolve_ms"),
                ):
                    _v = getattr(game, _attr, None)
                    if _v is not None:
                        metrics_raw[_field].append(_v)
                metadata = {
                    "pipeline_latency": {
                        k: (round(sum(v) / len(v), 1) if v else None)
                        for k, v in metrics_raw.items()
                    },
                    # WO-LILY-UPGRADE-168: the full 1.6.8 metrics block —
                    # tokens (incl. cached), TTS characters, STT audio
                    # duration, and the whole latency/turn-taking family.
                    "session_metrics": session_metrics.summary(),
                    # C14b: per-question delivery timestamps
                    "question_timeline": getattr(
                        scorekeeper, "question_timeline", {}
                    ),
                    # Voice-ID closure: outcome + timing persist so a slow
                    # or missed recognition explains itself from the DB row.
                    "voice_identity": {
                        "outcome": getattr(game, "_voice_id_outcome", None)
                        or ("never_ran" if not getattr(
                            game, "_voice_identity_attempted", False
                        ) else "attempted_no_outcome"),
                        "embed_ms": getattr(game, "_voice_id_embed_ms", None),
                        "resolve_ms": getattr(
                            game, "_voice_id_resolve_ms", None
                        ),
                    },
                }
                await lily_persistence.lily_session_end(
                    supabase, scorekeeper,
                    final_standings=standings, metadata=metadata,
                )
                if not game.identity_persistence_allowed():
                    logger.info(
                        "LILY_FORGET | SESSION_CLOSE_IDENTITY_WRITES_SKIPPED "
                        "| session=%s state=%s",
                        scorekeeper.session_id, game.forget_state,
                    )
                    return
                # Session memory — idempotent with the finish_game write
                # (upsert on session_id); this path also covers sessions
                # that end without reaching the final question.
                # game.group_id (not the entrypoint local): a mid-session
                # upgrade may have re-keyed the group.
                await lily_memory.lily_write_session_memory(
                    supabase, game.group_id, scorekeeper.session_id,
                    # CLASS 3 (LIVEFIRE-001): delivered count, not the armed
                    # cursor — mirrors the finish_game write.
                    standings, game.questions_asked_count(), game.highlights,
                    round_reached=scorekeeper.round,
                )
                # B3 session report — one row per session, idempotent upsert
                # on session_id. Transcript is what's retained in memory (the
                # scorekeeper's rolling buffer) — never re-queried from the DB;
                # assessment is filled later by the clinical desk.
                await lily_persistence.lily_write_session_report(
                    supabase,
                    session_id=scorekeeper.session_id,
                    group_id=game.group_id,
                    transcript=list(scorekeeper.transcript_buffer),
                    game_stats=game.build_game_stats(standings),
                )
                # Late-binder voiceprint enrollment — AWAITED (not
                # fire-and-forget) so the shutdown gate can't tear the
                # process down mid-write; failures log LILY_ENROLL | FAILED.
                await lily_persistence.lily_enroll_voiceprints(
                    stt, supabase, lambda: game.group_id, scorekeeper,
                    trigger="session_close",
                )
                # Durable voice-identity enrollment (device-independent
                # recognition) — folds this session's voice into the group's
                # centroid. Inert unless the embedder + captured audio are
                # present; awaited on the same shutdown gate.
                await game._voice_identity_enroll_at_close()
            except Exception as e:
                logger.error("SESSION_CLOSE | persistence error: %s", e)
            finally:
                shutdown_gate.set()

        asyncio.ensure_future(_persist())

    # --- RPC handlers (frontend -> agent): exactly two methods ---
    # Both report the REAL outcome. They used to return ok:True
    # unconditionally, but start_game early-returns on start_blocked_reason
    # / intake_roundrobin_active / already-started, and skip_question
    # early-returns while adjudicating or transitioning. The client reads
    # ok to decide whether to show "Lily didn't catch that"
    # (lily-lobby.tsx / lily-game.tsx), so a deferred start looked
    # identical to a successful one: the player taps Start, the pill
    # returns to normal, nothing happens, and nothing is said.
    async def _rpc_start(data: rtc.RpcInvocationData) -> str:
        started = game.game_started
        await game.start_game(source="rpc")
        ok = game.game_started and not started
        return json.dumps({
            "ok": ok,
            "phase": game.ui_phase,
            "reason": None if ok else (
                game.start_blocked_reason() or "already_started"
            ),
        })

    async def _rpc_skip(data: rtc.RpcInvocationData) -> str:
        before = scorekeeper.question_number
        await game.skip_question(source="rpc")
        return json.dumps({
            "ok": scorekeeper.question_number != before,
            "question_number": scorekeeper.question_number,
        })

    async def _rpc_merge(data: rtc.RpcInvocationData) -> str:
        # WS-8 operator identity reconciliation: {"from_label":"S4",
        # "into_player":"Chris"} — folds a diarizer-split label back onto
        # the player it belongs to, roster + voiceprints + retro in one call.
        try:
            payload = json.loads(data.payload or "{}")
        except Exception:
            return json.dumps({"ok": False, "reason": "bad_payload"})
        outcome = await game.merge_speakers(
            payload.get("from_label", ""),
            payload.get("into_player", ""),
            source="rpc",
        )
        return json.dumps(outcome)

    async def _rpc_image_shown(data: rtc.RpcInvocationData) -> str:
        # HOTFIX-005 X4: render confirmation — the frontend's <img> onLoad
        # fired, so a generated picture is CONFIRMED on the glass. Records
        # the URL so 'the picture is up' is a readable, grounded state.
        try:
            payload = json.loads(data.payload or "{}")
        except Exception:
            return json.dumps({"ok": False, "reason": "bad_payload"})
        game.note_image_rendered(str(payload.get("url", "")))
        return json.dumps({"ok": True})

    ctx.room.local_participant.register_rpc_method("lily_control.start", _rpc_start)
    ctx.room.local_participant.register_rpc_method("lily_control.skip", _rpc_skip)
    ctx.room.local_participant.register_rpc_method("lily_control.merge", _rpc_merge)
    ctx.room.local_participant.register_rpc_method(
        "lily_control.image_shown", _rpc_image_shown
    )

    # --- Player photo ingest (vision, Zuna port — the 12:48 "you don't
    # have image ingestion" fix). Topic `lily.image.upload` carries the
    # UI image-picker's bytes; they land in the lily-images bucket
    # (content-addressed, "player" source), the public URL flows through
    # Grok vision, and Lily reacts to what is ACTUALLY in the photo — or
    # says honestly that she couldn't look (unconfigured provider,
    # storage failure, analysis error). She never describes an image the
    # tool did not confirm seeing.
    def _on_player_image(reader, participant_identity: str):
        async def _ingest() -> None:
            try:
                info = getattr(reader, "info", None)
                mime = (
                    (getattr(info, "mime_type", "") or "").lower()
                    if info else ""
                ) or "image/jpeg"
                buf = bytearray()
                async for chunk in reader:
                    buf.extend(chunk)
                    if len(buf) > lily_images.MAX_IMAGE_BYTES:
                        logger.warning(
                            "LILY_VISION | INGEST_OVERSIZE | bytes>%d sender=%s",
                            lily_images.MAX_IMAGE_BYTES, participant_identity,
                        )
                        game.sk.set_status_note(
                            "a player tried to share a photo but it was too "
                            "large (8MB cap) — tell them plainly, invite a "
                            "smaller one"
                        )
                        game.instructed_reply(
                            "A player's shared photo was too large to take "
                            "in — say so plainly and invite a smaller one."
                        )
                        return
                data = bytes(buf)
                if not data:
                    return
                logger.info(
                    "LILY_VISION | INGEST | bytes=%d mime=%s sender=%s",
                    len(data), mime, participant_identity,
                )
                url = await lily_images.lily_upload_image_bytes(
                    game.supabase, data, source="player", content_type=mime
                )
                if not url:
                    game.sk.set_status_note(
                        "a player shared a photo but storing it failed — "
                        "say the photo didn't make it through, honestly"
                    )
                    game.instructed_reply(
                        "A player tried to share a photo and it didn't come "
                        "through on your side — say so plainly, invite them "
                        "to try again. Never describe what you didn't see."
                    )
                    return
                result = await lily_vision.lily_describe_image(
                    url,
                    "A player at a live trivia night just shared this photo "
                    "with the host. Describe what's in it — objects, "
                    "settings, visible text, anything a playful host could "
                    "react to. Never guess who any person is.",
                )
                if result.get("status") == "ok":
                    description = str(result.get("description", ""))[:500]
                    game.sk.set_status_note(
                        "PLAYER PHOTO (verified through your vision tool — "
                        f"you really saw it): {description}"
                    )
                    game.instructed_reply(
                        "A player just shared a photo and the PLAYER PHOTO "
                        "state note carries what is actually in it. React "
                        "in character — specific and warm, grounded ONLY "
                        "in what the note says is there. One beat, then "
                        "back to the night."
                    )
                    return
                if result.get("status") == "unavailable":
                    game.sk.set_status_note(
                        "a player shared a photo but your vision provider "
                        "is not switched on tonight — capability yes, "
                        "availability no; say you can't look at pictures "
                        "tonight, honestly"
                    )
                else:
                    game.sk.set_status_note(
                        "a player shared a photo but the look failed "
                        f"({str(result.get('reason'))[:120]}) — say you "
                        "couldn't get a look, honestly"
                    )
                game.instructed_reply(
                    "A player shared a photo but you could NOT actually "
                    "see it (the state note says why) — say so plainly. "
                    "No fabricated description, no invented fixes."
                )
            except Exception as e:
                logger.warning("LILY_VISION | ingest failed: %s", e)

        asyncio.ensure_future(_ingest())

    ctx.room.register_byte_stream_handler("lily.image.upload", _on_player_image)
    logger.info("LILY_VISION | registered byte stream topic=lily.image.upload")

    # --- Start ---
    # Say gate: on_enter (fired inside session.start) must know whether
    # this is a fresh room (session_greet) or a reconnect (session_rejoin)
    # — the two openers carry distinct keys and must never trip each other.
    game.reconnected = reconnected
    if reconnected:
        game.restore_reconnected_state()
    agent = LilyAgent(
        game=game,
        instructions=LILY_SYSTEM_PROMPT,
        # Zuna voice-switch port: runtime preset switching between
        # voice1 (primary, LILY_VOICE_1 / hardcoded default) and voice2
        # (Raven's voice, LILY_VOICE_ID / RAVEN_VOICE_ID). Discovered by
        # the LLM via tool descriptions — no prompt-file edits.
        # `lily_list_voices` reports availability + active preset;
        # `lily_switch_voice("voice1|voice2")` mutates the session
        # LilyTTS `_opts.voice_id` so the next `synthesize()` targets
        # the new voice with no session teardown.
        tools=[
            lily_list_voices,
            lily_switch_voice,
            # Vision (Zuna port): player-shared photos + image URLs.
            # Ingest path: the lily.image.upload byte stream below.
            lily_analyze_image,
        ],
    )
    game.agent = agent  # P2: per-turn preemptive-generation control
    # WS-14: Krisp ambient NC on the room input. BVC stays structurally
    # excluded — in a one-mic multiplayer room the "background voices" ARE
    # the other players (lily_noise_cancellation_options coerces any
    # attempt, including LILY_NOISE_CANCELLATION=bvc, back to NC). The
    # 1.6.4 incident (NcSession SIGABRT `Input and output sample rates
    # must be equal`, every job accept dead in 2s) is why the kill switch
    # exists: LILY_NOISE_CANCELLATION=off drops NC via slot secret with no
    # redeploy if the abort recurs at 1.6.6. Metering: Krisp models bill
    # ~$0.002–0.004/min on Cloud from May 2026.
    #
    # WO-LILY-NC-BENCH-001 Task 2: 1.6.6-NATIVE RoomOptions, replacing the
    # deprecated RoomInputOptions shim (the 08-06 mute sessions logged the
    # shim's deprecation warning on the join path). _create_from_legacy
    # mapped the old form to AudioInputOptions with identical defaults —
    # this is the same configuration minus the legacy conversion layer.
    # NC itself stays behind the LILY_NOISE_CANCELLATION slot secret
    # (default off until the bench gate passes).
    await session.start(
        room=ctx.room,
        agent=agent,
        room_options=RoomOptions(
            audio_input=AudioInputOptions(
                noise_cancellation=lily_noise_cancellation_options(),
            ),
            # P0-C: RoomIO otherwise publishes the pre-TTS model stream.
            # Lily publishes one final transcription after playout from the
            # exact post-transform text that entered TTS.
            text_output=False,
        ),
    )

    # --- Acoustic pipeline: room audio -> devAIce (WO-LILY-AUDEERING-001) ---
    # Missing AUDEERING_API_KEY -> pipeline is None (breaker open, one
    # structured log line) and the session runs unaffected.
    audeering_pipeline = await lily_audeering_client.lily_start_audeering_pipeline(
        acoustic_state
    )
    game.audeering_pipeline = audeering_pipeline
    # Self-knowledge Task 3 availability layer: the session's gates, known
    # only now that the pipeline start has resolved. Capability lives in
    # the manifest; THESE flags say what is switched on tonight. Adult
    # deck deploys as one unit with the child-signal sensor (architect
    # mode is the server-authenticated testing override); real-entity
    # picture sourcing rides the EXA key — generated imagery needs neither.
    game.availability_flags = {
        # Owner directive 2026-08-06: open by default (spoken 18+ opt-in
        # remains the consent step); "sensor" mode restores the legacy
        # pipeline coupling.
        "adult_deck": (
            lily_config.adult_deck_gate_mode() == "open"
            or audeering_pipeline is not None
            or lily_config.architect_mode()
        ),
        "pictures_real_sourcing": bool(lily_config.exa_api_key()),
        # Vision (Zuna port): player-shared photo analysis rides XAI_API_KEY.
        "vision": lily_vision.lily_vision_available(),
    }
    def _on_track_subscribed(track, publication=None, participant=None) -> None:
        try:
            if (
                getattr(participant, "kind", None)
                == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT
            ):
                return
            # VIDEOIN-001: a published camera track IS an explicit
            # user-initiated open (the UI-control path; the spoken
            # "look at this" path opens the lane before the track lands).
            # Open the lane only when AVAILABLE — in the adult deck it
            # stays refused and the frame fork drops every frame. One sink
            # per camera track.
            if getattr(track, "kind", None) == rtc.TrackKind.KIND_VIDEO:
                if game.camera_lane_status()["available"]:
                    game.sk.set_camera_lane("open")
                else:
                    logger.info(
                        "LILY_CAMERA | TRACK_IGNORED | session=%s "
                        "reason=unavailable_adult", game.sk.session_id,
                    )
                if not getattr(game, "_camera_fork_started", False):
                    game._camera_fork_started = True
                    asyncio.ensure_future(_lily_camera_frame_fork(track, game))
                return
            if getattr(track, "kind", None) != rtc.TrackKind.KIND_AUDIO:
                return
            if audeering_pipeline is not None:
                asyncio.ensure_future(
                    lily_audeering_client.lily_audeering_audio_fork(
                        track, audeering_pipeline
                    )
                )
            # Voice-identity probe fork (device-independent recognition):
            # buffer this speaker's 16 kHz PCM for the embedder. Only when
            # the feature is ready (flag + model), and only the first mic
            # track, so it stays inert and single otherwise.
            if lily_claim_voice_probe(game):
                asyncio.ensure_future(_lily_voice_probe_fork(track, game))
        except Exception as e:
            logger.warning("LILY_MEDIA | track hook failed: %s", e)

    # Register independently of audEERING. Durable voice identity and camera
    # capture must still receive tracks when the optional acoustic provider is
    # unavailable; coupling this whole hook to audEERING silently disabled
    # biometric enrollment and matching on those deployments.
    ctx.room.on("track_subscribed", _on_track_subscribed)
    try:
        for _participant in ctx.room.remote_participants.values():
            for _pub in _participant.track_publications.values():
                _track = getattr(_pub, "track", None)
                if _track is not None:
                    _on_track_subscribed(_track, _pub, _participant)
    except Exception as e:
        logger.warning("LILY_MEDIA | already-subscribed scan failed: %s", e)

    # SFX: thinking bed + stingers ride BackgroundAudioPlayer.
    background_audio = BackgroundAudioPlayer(stream_timeout_ms=10000)
    await background_audio.start(room=ctx.room, agent_session=session)
    game.background_audio = background_audio

    # Prewarm the ElevenLabs connection so the greeting's first synthesis
    # skips the TCP+TLS handshake.
    asyncio.ensure_future(lily_prewarm_tts_connection())

    def _latency_metadata() -> dict:
        return {
            "pipeline_latency": {
                k: (round(sum(v) / len(v), 1) if v else None)
                for k, v in metrics_raw.items()
            },
            # Full 1.6.8 metrics ride the heartbeat too, so "is she lagging /
            # burning tokens" is a live SQL query mid-game, not a post-mortem.
            "session_metrics": session_metrics.summary(),
            # C14b: per-question delivery timestamps
            "question_timeline": getattr(
                scorekeeper, "question_timeline", {}
            ),
            "voice_identity": {
                "outcome": getattr(game, "_voice_id_outcome", None)
                or ("never_ran" if not getattr(
                    game, "_voice_identity_attempted", False
                ) else "attempted_no_outcome"),
                "embed_ms": getattr(game, "_voice_id_embed_ms", None),
                "resolve_ms": getattr(game, "_voice_id_resolve_ms", None),
            },
        }

    # Heartbeat checkpoint loop (60s) — carries rolling latency averages so
    # "is she lagging" is a SQL query mid-game, not a post-mortem.
    asyncio.ensure_future(lily_persistence.lily_heartbeat(
        supabase, scorekeeper, heartbeat_stop,
        metadata_provider=_latency_metadata,
    ))

    # Initial truth for late joiners / reconnect snap-restore.
    await game.publish_attributes()
    if game.armed_question is not None:
        # A WORKER RECONNECT restored a checkpointed question
        # (restore_reconnected_state). Room metadata is server-side and
        # survived the restart, so blanking it here actively ERASED a
        # question that was on the glass a moment ago and that the agent
        # still holds armed — picture and all. Republish what she actually
        # has instead.
        game.publish_question_to_glass(reason="reconnect")
    else:
        await game.publish_metadata("")
    game.start_prefetch()

    # Session opener — the FALLBACK trigger path. on_enter (inside
    # session.start, already run above) is the single PRIMARY greet source;
    # this path opens the night ONLY if on_enter did not claim the opener
    # (M1 gate — silence is her failure mode).
    #
    # CLASS 8 (LIVEFIRE-001) 8a: this used to dispatch unconditionally and
    # rely on the say-gate to SUPPRESS it as a dup — the anti-double-greeting
    # correctness was load-bearing on that suppression. It is now an explicit
    # fallback gated on the registry state, so the two paths never both
    # dispatch and the gate is a belt, not the whole braces.
    if reconnected:
        # G1: a reconnect resumes a LIVE game — same preemptive-off rule
        # as start_game (which this path bypasses).
        game.set_game_live_preemptive(True)
        game.start_idle_watchdog()
        # Initial glass truth already published above; don't await another
        # attribute RTT before the rejoin line.
        game.publish_attributes_nowait()
        if game.say_registry.state("session_rejoin") is None:
            game.gated_say(
                "session_rejoin",
                "rejoin",
                game.rejoin_instructions(),
                source="entrypoint",
            )
    else:
        # Fresh room: Lily speaks FIRST. on_enter normally claimed
        # session_greet already; only when it did NOT do we open here, and
        # only then do we spend the memory-at-the-door budget.
        if game.say_registry.state("session_greet") is None:
            await game.await_greeting_memory()
            game.gated_say(
                "session_greet",
                "greet",
                game.greeting_instructions(),
                source="entrypoint",
            )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    cli.run_app(WorkerOptions(
        entrypoint_fnc=entrypoint,
        worker_type=WorkerType.ROOM,
        agent_name="Lily",  # dispatch name — must match livekit.toml exactly
        # Explicit memory settings (smoke-test item 0). Default limit is 0
        # (monitor DISABLED); a nonzero limit deliberately enables the job
        # memory monitor, which KILLS the job process on breach — the known
        # 1.6.4 issue only bites when limit>0 (monitor code byte-identical
        # at 1.6.6, re-verified — limits stay explicit), so the ceiling is set
        # consciously high (default 2048MB, LILY_JOB_MEMORY_LIMIT_MB).
        job_memory_warn_mb=lily_config.job_memory_limit_mb() * 0.75,
        job_memory_limit_mb=lily_config.job_memory_limit_mb(),
    ))
