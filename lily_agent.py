"""
lily_agent.py — LILY: multi-player voice trivia host. Entrypoint + agent.

One node. Lily is a single LiveKit agent; game flow is managed inside the
prompt via a state block, not node transfers. Deliberate inversion of the
Lovebirds architecture: Lily speaks by default — no generation gate, no
trigger loop, no watchdog. The only hard logic outside the LLM is the
scorekeeper, the answer-window timer, SFX dispatch, checkpointing, and the
deterministic enforcement of the sticky player commands ("skip",
"back to normal").

Dual-brain LLM binding (spec §4.4): vocal node gemini-3.5-flash (this
session's LLM), reasoning node gemini-3.1-pro-preview (lily_reasoning,
own HTTP client).
"""

import asyncio
import datetime
import json
import logging
import re
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent / ".env", override=False)

from livekit import rtc, api
from livekit.agents import (
    AutoSubscribe,
    InterruptionOptions,
    JobContext,
    RunContext,
    TurnHandlingOptions,
    WorkerOptions,
    WorkerType,
    cli,
    function_tool,
)
from livekit.agents.llm import ChatMessage
from livekit.agents.voice import Agent, AgentSession
from livekit.agents.voice.agent_activity import _SpeechHandleContextVar
from livekit.agents.voice.background_audio import (
    AudioConfig,
    BackgroundAudioPlayer,
)
from livekit.agents.voice.events import UserInputTranscribedEvent
from livekit.plugins import noise_cancellation, silero
from livekit.plugins.google import LLM as GoogleLLM
from livekit.plugins.speechmatics import (
    STT as SpeechmaticsSTT,
    AdditionalVocabEntry,
    OperatingPoint,
    SpeakerIdentifier,
    TurnDetectionMode,
)

import lily_addressee
import lily_audeering_client
import lily_audeering_consumers
import lily_bank
import lily_bank_tuning
import lily_capabilities
import lily_config
import lily_evaluation
import lily_forget
import lily_memory
import lily_nbest
import lily_persistence
import lily_reasoning
import lily_say_gate
from lily_binding import (
    LilyFragmentAccumulator,
    lily_extract_name_from_fragments,
    lily_is_valid_name,
)
from lily_reasoning import LilyReasoning
from lily_scorekeeper import LilyScorekeeper, lily_detect_state_contradiction
import lily_images
import lily_vision
from lily_tts import LilyTTS, lily_prewarm_tts_connection
from lily_vision import lily_analyze_image
from lily_voice_switch import lily_list_voices, lily_switch_voice

logger = logging.getLogger("lily_agent")

_PROMPTS_DIR = Path(__file__).parent / "prompts"
# Loader-level rubric append (WO-LILY-AUDEERING-001 Task 3): the room-read
# rubric is a separate prompt file, zero-scalar-linted at import of
# lily_audeering_consumers, appended here so lily_system.txt itself stays
# untouched.
LILY_SYSTEM_PROMPT = (
    (_PROMPTS_DIR / "lily_system.txt").read_text(encoding="utf-8")
    + lily_audeering_consumers.lily_audeering_rubric_block()
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

EVENTS_TOPIC = "lily.events"

# Contract-note packet-kind spellings for the `event` discriminator alias
# (seam contract: bind / reveal / callout / finale).
_EVENT_CONTRACT_ALIAS = {
    "player_bind": "bind",
    "best_wrong_answer": "callout",
    "biggest_comeback": "callout",
    # 2026-07-14 amendment: clarify carries `clarify` on BOTH discriminators.
    "clarify": "clarify",
    # WO-LILY-FORGETME-001: memory_forgotten carries the same spelling on
    # BOTH discriminators (the shipped frontend clears the device
    # localStorage group id and shows a transient confirmation).
    "memory_forgotten": "memory_forgotten",
}


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

# Device metadata identifies a browser/table candidate, never the humans
# currently present. Its memories stay quarantined until a live voiceprint
# overlaps the stored group.
_DEVICE_CANDIDATE_SOURCES = (
    "participant_metadata",
    "participant_metadata_late",
    "dispatch_metadata",
)

# Sources that identify the live table strongly enough to skip fallback.
_STRONG_GROUP_SOURCES = (
    "env_override",
    "voiceprint_match",
)


# ---------------------------------------------------------------------------
# Game director — the non-LLM surface: window timer, adjudication commit,
# SFX dispatch, state publication, checkpointing triggers.
# ---------------------------------------------------------------------------

class LilyGame:
    # Class-level defaults so __new__-built test fixtures (and any partially
    # constructed instance) read sane state; __init__ re-declares them with
    # the full contract comments.
    _phase_hold: str | None = None
    # Self-knowledge Task 3: a lagged returning table's delta rode the
    # greeting; the stamp persists after the greet confirms.
    _whats_new_pending: bool = False
    # RECOGNITION-VARIETY Task 1: the mid-session recognition catch-up
    # acknowledgment fires at most once per session.
    _late_recognition_fired: bool = False
    # RECOGNITION-VARIETY fixture Q5: answers spoken while the delivery
    # turn is still playing out (window not yet open) buffer here and
    # replay at window open — the "no early buzz-ins" v1 concession cost
    # a correct final answer its adjudication row on 08-04.
    _pre_window_segments: list | None = None
    # Session availability flags (manifest availability layer): set by the
    # entrypoint once gates are known; None = unknown, inject nothing.
    availability_flags: dict | None = None

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
        self.stt: SpeechmaticsSTT | None = None
        # P2 preemptive repair: True while a deterministic between-turn
        # instruction speech (reveal/steal/skip/start/mode-revert) is in
        # flight — preemptive generation is paused for those turns.
        self._preemptive_paused = False

        self.fragments = LilyFragmentAccumulator()
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
        # Set by the entrypoint BEFORE session.start so on_enter knows
        # whether to greet (session_greet) or rejoin (session_rejoin).
        self.reconnected = False

        self.next_question: dict | None = None      # prefetched N+1
        self.armed_question: dict | None = None     # in state block, awaiting ask
        # Draw idempotency (WO-LILY-DESYNC-HONESTY-001 G2): every question
        # a prefetch DRAWS registers here the moment it lands — not at
        # serving (arm), which is where the live q_0492 double-draw slipped
        # through: the second draw ran before the first serving registered
        # in used_prompts/asked_history. Session-scoped; a discarded draw
        # stays excluded (no repeats either way).
        self._drawn_ids: set = set()
        self._drawn_hashes: set = set()
        # 50/50 lifeline (multiple-choice WO): eliminated choice indices for
        # the CURRENT question — reset at arm, rides publish_metadata.
        self.eliminated: list[int] = []
        self.used_prompts: list[str] = []
        # Bank curation (WO-LILY-OMNIBUS-002 D/F): the group's served-
        # question history (loaded at entrypoint, appended per serving)
        # and the promoted category-candidate names (F — the only
        # proposals Lily may ever announce).
        self.asked_history: list[dict] = []
        self.promoted_categories: list[str] = []
        self._prefetch_task: asyncio.Task | None = None
        self._window_timer: asyncio.Task | None = None
        # Idle watchdog (live 2026-07-15 stall class): guarantees a live
        # game can never sit armed-less and silent.
        self._watchdog_task: asyncio.Task | None = None
        self._prefetch_stall_ticks = 0
        self._armed_limbo_ticks = 0
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
        self._last_assistant_text = ""
        self._suppressed_speech_ids: set[str] = set()
        self._enroll_started = False
        self._armed_speech_misses = 0  # agent turns finished w/o performing q
        # Structural delivery intent (desync WO Sub-agent B): question
        # number whose delivery the NEXT outbound spoken turn was
        # code-dispatched to perform — that turn claims q_{N}_delivery at
        # dispatch regardless of phrasing. None = no delivery in flight.
        self._pending_delivery_qnum: int | None = None
        # Post-reveal delivery is strict: if the generated turn does not
        # contain the armed prompt (and every MC option), tts_node replaces
        # it with the deterministic question sheet before any claim opens.
        self._strict_delivery_qnum: int | None = None
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
        self._group_facts_written: set = set()  # per-session fact dedupe

        # Persistent cross-session memory (rematch): the [RETURNING TABLE]
        # block loaded at session start, and this session's callouts
        # collected for the lily_memories highlights column.
        self.memory_block: str = ""
        # Remembered player names for STT name-snapping at bind time
        # (the "Romney"-for-"Rami" class).
        self.memory_player_names: list[str] = []
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

        # B1 addressee-label corpus: players awaiting a clarify reply
        # (player_name -> {"row_task": Task|None returning the logged row
        # id}), and the insert task per answer candidate this question
        # (candidate key -> Task returning row id) kept for the implicit
        # label UPDATE at adjudication commit.
        self.pending_clarify: dict[str, dict] = {}
        self._addressee_rows: dict[str, asyncio.Task] = {}

        # Acoustic pipeline (WO-LILY-AUDEERING-001): room-read state store +
        # per-user-turn trajectory counter. The entrypoint passes the fresh
        # per-session state; the default keeps offline construction working.
        self.acoustic = lily_audeering_consumers.lily_get_acoustic_state()
        self.audeering_pipeline = None
        self._user_turn_index = 0

    # -- state transport (seam contract) ------------------------------------

    def _players_payload(self) -> list[dict]:
        if not self.sk.players:
            return []
        top = max(s["score"] for s in self.sk.players.values())
        leaders = [n for n, s in self.sk.players.items() if s["score"] == top and top > 0]
        sole_leader = leaders[0] if len(leaders) == 1 else None
        return [
            {
                "name": name,
                "score": s["score"],
                "streak": s["streak"],
                "leader": name == sole_leader,
            }
            for name, s in self.sk.players.items()
        ]

    async def publish_attributes(self) -> None:
        """LWW participant attributes — updated on every phase transition
        and score change. Exact key spellings per the seam contract."""
        try:
            window = {
                "open": self.sk.is_window_open(),
                "duration_ms": int(
                    ((self.sk.answer_window_deadline or 0)
                     - (self.sk.answer_window_opened_at or 0)) * 1000
                ),
                "opened_at": int((self.sk.answer_window_opened_at or 0) * 1000),
            }
            # Seam contract: optional `steal` key — present and true only
            # while the open window is a steal window (frontend renders
            # the hot steal bar off this flag).
            if window["open"] and self._steal_window:
                window["steal"] = True
            attrs = {
                # Published phase honors the pre-delivery screen hold: the
                # glass must never lead the voice (first-question arm fires
                # mid-greeting; the hold keeps the lobby up until playout).
                "phase": self._phase_hold or self.ui_phase,
                "round": str(self.sk.round),
                "question_number": str(self.sk.question_number),
                "mode": self.sk.mode,  # deterministic sticky flag (§11.4)
                # SEAM ADDITION (group prefs WO): `pacing` — "timed" |
                # "relaxed" — joins the LWW attribute set so the frontend
                # can style/omit its countdown UI; answer_window.duration_ms
                # already carries the stretched window when relaxed.
                "pacing": self.sk.pacing,
                # Lobby media choice (sub-agent K): voice_only | pictures —
                # sticky, deterministic, default voice_only.
                "media_mode": self.sk.media_mode,
                "players": json.dumps(self._players_payload()),
                "answer_window": json.dumps(window),
                "last_active_at": str(int(time.time())),
            }
            await self.ctx.room.local_participant.set_attributes(attrs)
        except Exception as e:
            logger.warning("LILY_STATE | attribute publish failed: %s", e)

    def publish_attributes_nowait(self) -> None:
        asyncio.ensure_future(self.publish_attributes())

    async def publish_metadata(
        self,
        question_text: str | None,
        reveal: dict | None = None,
        choices: list[str] | None = None,
        eliminated: list[int] | None = None,
        image_url: str | None = None,
        category: str | None = None,
    ) -> None:
        """Room metadata: current question text + reveal payload. Seam
        addition (multiple-choice WO): when the armed question carries
        choices, they ride here too — `choices` (the 4 option strings) and
        `eliminated` (50/50: indices into choices crossed out). Both keys
        are ABSENT for freeform questions (optional fields, no
        restructuring of the existing document). image_url (sub-agent H)
        is an OPTIONAL additive field — picture questions put their public
        bucket URL into the question payload; every other publish clears
        it (the frontend already renders it). `category` is the question's
        category family for the frontend's eyebrow line — optional key,
        ABSENT when unknown (the parser tolerates absence)."""
        try:
            payload = {
                "question": question_text or "",
                "reveal": reveal
                or {"answer": "", "winner": None, "correct": False},
                # Drives the frontend's high-contrast wager palette shift.
                "wager": self.sk.phase == "final",
                "image_url": image_url or "",
            }
            if category:
                payload["category"] = category
            if choices:
                payload["choices"] = list(choices)
                payload["eliminated"] = list(eliminated or [])
            metadata = json.dumps(payload)
            # Room metadata has no rtc-side setter — server API only.
            await self.ctx.api.room.update_room_metadata(
                api.UpdateRoomMetadataRequest(
                    room=self.ctx.room.name, metadata=metadata
                )
            )
        except Exception as e:
            logger.warning("LILY_STATE | metadata publish failed: %s", e)

    async def send_event(self, event_type: str, payload: dict) -> None:
        """Reliable ordered data packet on topic lily.events (<=15KiB)."""
        if event_type in ("best_wrong_answer", "biggest_comeback"):
            # Callouts double as session-memory highlights (lily_memories).
            self.highlights.append({
                "type": event_type,
                "player": payload.get("player"),
                "detail": payload.get("detail") or payload.get("answer"),
            })
            self.highlights = self.highlights[-lily_memory.MEMORY_HIGHLIGHTS_CAP:]
            # Best wrong answers double as persistent running-bit material
            # (lily_group_facts) — callback fuel for the next night.
            if event_type == "best_wrong_answer":
                detail = payload.get("detail") or payload.get("answer")
                if payload.get("player") and detail:
                    self.record_group_fact(
                        payload["player"], f"best wrong answer: {detail}"
                    )
        try:
            # Payload spreads first so the packet discriminator can never be
            # clobbered by a payload key (the callout payload carries its own
            # kind under "callout_type" for the same reason).
            # Dual discriminators: the shipped parser generation reads `type`
            # first, the contract-note generation reads `event` first — carry
            # both so either frontend parses every packet.
            packet = {
                **payload,
                "type": event_type,
                "event": _EVENT_CONTRACT_ALIAS.get(event_type, event_type),
                "ts": int(time.time() * 1000),
            }
            await self.ctx.room.local_participant.publish_data(
                json.dumps(packet).encode("utf-8"),
                reliable=True,
                topic=EVENTS_TOPIC,
            )
        except Exception as e:
            logger.warning("LILY_EVENTS | %s emit failed: %s", event_type, e)

    def send_event_nowait(self, event_type: str, payload: dict) -> None:
        asyncio.ensure_future(self.send_event(event_type, payload))

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
            if not (
                getattr(self, "game_started", False)
                and not getattr(self, "game_over", False)
            ):
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
        supported lever is the agent-level live-read enabled flag."""
        if self.agent is None:
            return
        self.agent.set_preemptive_generation(not live)
        logger.info(
            "LILY_STATE | PREEMPTIVE_%s | session=%s (game %s)",
            "OFF" if live else "ON", self.sk.session_id,
            "live" if live else "not live",
        )

    # -- gated speech dispatch (say-gate WO §1) -------------------------------

    def gated_say(
        self,
        key: str | None,
        act: str,
        instructions: str,
        source: str,
        extra_keys: tuple[str, ...] = (),
    ) -> bool:
        """THE dispatch helper for code-triggered speech. Game-critical
        acts pass a state key (session_greet, session_rejoin,
        q_{N}_reveal, round_{N}_scores, finale); the claim happens HERE,
        at dispatch time — not at playback completion, which is the race
        window that produced the live double greeting. A second claim on
        the same key logs LILY_SAY_SUPPRESSED | reason=dup and does NOT
        speak. Keyless dispatches (steal window, skip, mode reverts,
        prefetch nudge) still log LILY_SAY for the audit trail.
        extra_keys are claimed alongside (e.g. the finale rides the final
        reveal's dispatch) without gating it."""
        reservation = f"dispatch_{uuid.uuid4().hex}"
        if key is not None and not self.say_registry.claim(key, owner=reservation):
            logger.warning(
                "LILY_SAY_SUPPRESSED | reason=dup | key=%s | act=%s | source=%s",
                key, act, source,
            )
            return False
        for k in extra_keys:
            self.say_registry.claim(k, owner=reservation)
        logger.info(
            "LILY_SAY | act=%s | key=%s | source=%s", act, key or "-", source
        )
        handle = self.instructed_reply(instructions)
        speech_id = getattr(handle, "id", None)
        if speech_id:
            self.say_registry.reassign_owner(reservation, speech_id)
        return True

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
    #       the flag, and that turn claims regardless of phrasing;
    #   (b) ORGANIC — any turn while the question is armed and undelivered
    #       that performs its core answer-bearing sentence as written
    #       (lily_evaluation.lily_turn_presents_question).
    # The window still opens at the delivery TURN's playout completion
    # (on_agent_speech_finished) — what changed is WHAT registers delivery.

    def expect_delivery(self, *, strict: bool = False) -> None:
        """Arm the structural delivery flag: the next outbound spoken turn
        was just dispatched to perform the armed question and will claim
        q_{N}_delivery at dispatch. No-op when nothing is armed, the
        window is already open, or the delivery is already claimed."""
        if self.armed_question is None or self.sk.answer_window_open:
            return
        key = f"q_{self.sk.question_number}_delivery"
        if self.say_registry.state(key) is None:
            self._pending_delivery_qnum = self.sk.question_number
            self._strict_delivery_qnum = (
                self.sk.question_number if strict else None
            )

    def consume_pending_delivery(self, qnum: int) -> bool:
        """One-shot consume of the structural delivery flag for question
        `qnum` (tts_node, at speech dispatch). True = this turn is the
        code-dispatched delivery turn."""
        if self._pending_delivery_qnum == qnum:
            self._pending_delivery_qnum = None
            return True
        return False

    def rendered_armed_question(self) -> str:
        """Deterministic spoken sheet used when strict delivery drifts."""
        armed = self.armed_question or {}
        prompt = str(armed.get("prompt") or "").strip()
        choices = armed.get("choices")
        if not isinstance(choices, list) or not choices:
            return prompt
        labels = lily_evaluation.MC_CHOICE_LETTERS
        rendered = [
            f"{labels[index]}) {choice}"
            for index, choice in enumerate(choices[: len(labels)])
        ]
        return "\n".join([prompt, *rendered])

    def _delivery_text_matches_armed(self, spoken_text: str) -> bool:
        armed = self.armed_question or {}
        if not lily_evaluation.lily_turn_presents_question(
            armed.get("prompt", ""), spoken_text
        ):
            return False
        choices = armed.get("choices")
        if not isinstance(choices, list) or not choices:
            return True
        normalized = re.sub(r"[^a-z0-9]+", " ", spoken_text.lower()).strip()
        return all(
            re.sub(r"[^a-z0-9]+", " ", str(choice).lower()).strip()
            in normalized
            for choice in choices
        )

    def dispatch_armed_question(self, *, source: str) -> bool:
        """Dispatch one question-only turn after a completed reveal.

        Keeping reveal and delivery on separate handles prevents a round
        transition from registering an invented or stale question as N+1.
        Strict TTS validation rewrites any drift to the deterministic sheet.
        """
        if (
            self.armed_question is None
            or self.sk.answer_window_open
            or getattr(self, "game_over", False)
        ):
            return False
        key = f"q_{self.sk.question_number}_delivery"
        if self.say_registry.state(key) is not None:
            return False
        self.expect_delivery(strict=True)
        sheet = self.rendered_armed_question()
        return self.gated_say(
            None,
            "question_delivery",
            (
                "The previous reveal is complete. Deliver ONLY the armed "
                "question now. Read this sheet exactly, with every option "
                f"when present, then stop for answers:\n{sheet}"
            ),
            source=source,
        )

    def register_delivery_claim(
        self,
        spoken_text: str,
        *,
        speech_id: str | None = None,
    ) -> str | None:
        """The delivery-registration decision for ONE outbound spoken turn
        (called from tts_node at speech dispatch; pure decision + claim +
        screen publish, no audio plumbing — offline-testable). Returns:
          "claimed_structural"   — this is the code-dispatched delivery turn
                                   (claims regardless of phrasing);
          "claimed_core_sentence" — organic turn performing the question's
                                   core answer-bearing sentence as written;
          "duplicate"            — the turn textually re-performs an
                                   already-delivered question (BUG-2: the
                                   caller makes it physically silent);
          None                   — not a delivery event; speak normally.
        """
        armed = self.armed_question
        if armed is None or self.sk.answer_window_open:
            return None
        qnum = self.sk.question_number
        key = f"q_{qnum}_delivery"
        strict = getattr(self, "_strict_delivery_qnum", None) == qnum
        structural = self.consume_pending_delivery(qnum)
        if structural:
            self._strict_delivery_qnum = None
        textual = self._delivery_text_matches_armed(spoken_text)
        if structural and strict and not textual:
            logger.error(
                "LILY_DELIVERY | STRICT_REWRITE | session=%s q=%d "
                "reason=spoken_question_mismatch",
                self.sk.session_id, qnum,
            )
            return "rewrite_strict"
        if not structural and not textual:
            return None
        if self.say_registry.claim(key, owner=speech_id):
            trigger = "structural" if structural else "core_sentence"
            logger.info(
                "LILY_SAY | act=question_delivery | key=%s | "
                "source=tts_node | trigger=%s", key, trigger,
            )
            # Screen sync moved to WINDOW OPEN (playout completion) — the
            # dispatch-time publish here still led the voice by the length
            # of any audio queued ahead of this turn (a greeting or
            # celebration mid-playout while the question hit the glass).
            # open_window's publish is now the one screen-sync source; MC
            # choices and picture images ride it (seam unchanged).
            return f"claimed_{trigger}"
        if textual:
            logger.warning(
                "LILY_SAY_SUPPRESSED | reason=dup | key=%s | "
                "act=question_delivery | source=tts_node", key,
            )
            return "duplicate"
        # Structural flag met an already-claimed key with no textual
        # re-ask in the turn — banter after a registered delivery speaks
        # normally.
        return None

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
        self._whats_new_pending = True
        listed = "; ".join(delta)
        return (
            " ONE more beat, casual and quick, folded into the welcome — "
            "since this table last played you picked up something new: "
            f"{listed}. One light line ('since you were last here I "
            "picked up a couple of tricks'), never a feature list read "
            "aloud, and never mention it again tonight."
        )

    def buffer_pre_window_answer(self, seg: dict) -> None:
        """RECOGNITION-VARIETY fixture Q5 fix — capture the early buzz.

        The answer window opens at the delivery turn's PLAYOUT COMPLETION
        (v1 concession: no early buzz-ins). A fast caller answering while
        Lily is still reading the question landed in a closed window and
        vanished — the 08-04 session's Q5 ("The Nile is just a river in
        Egypt", correct, inside a pun) wrote no adjudication row because
        it never became a candidate. Finals arriving between the delivery
        CLAIM and window open now buffer (last 6) and replay at open.
        No-op unless a question is armed and its delivery is claimed —
        lobby chatter and post-window banter never buffer."""
        if self.sk.answer_window_open or self.armed_question is None:
            return
        key = f"q_{self.sk.question_number}_delivery"
        if self.say_registry.state(key) is None:
            return
        buf = self._pre_window_segments
        if buf is None:
            buf = []
            self._pre_window_segments = buf
        buf.append(dict(seg))
        del buf[:-6]
        logger.info(
            "LILY_ANSWER | PRE_WINDOW_BUFFERED | session=%s q=%d speaker=%s",
            self.sk.session_id, self.sk.question_number,
            seg.get("speaker_label"),
        )

    def _replay_pre_window_answers(self) -> None:
        """At window open (non-steal): replay buffered early answers as
        candidates, then run the same instant Tier-1 fast path a live
        in-window final gets — a correct early answer adjudicates now
        instead of waiting out the window (the wait is what the 08-04
        hangup raced). Commands/corpus enforcement do NOT re-run — the
        original pass already handled them."""
        buffered = list(self._pre_window_segments or [])
        self._pre_window_segments = []
        if not buffered:
            return
        replay_ts = time.time()
        last_result = None
        last_text = ""
        for seg in buffered:
            try:
                last_result = self.sk.on_transcript_segment(
                    is_final=True, now=replay_ts, **seg
                )
                last_text = seg.get("text") or ""
                logger.info(
                    "LILY_ANSWER | PRE_WINDOW_REPLAY | session=%s q=%d "
                    "text=%r",
                    self.sk.session_id, self.sk.question_number,
                    last_text[:60],
                )
            except Exception as e:
                logger.warning(
                    "LILY_ANSWER | PRE_WINDOW_REPLAY_FAILED: %s", e
                )
        try:
            question = self.sk.current_question or {}
            acceptable = question.get("acceptable_answers") or []
            ordered = self.sk.ordered_candidates()
            if ordered and acceptable and last_result is not None:
                first = ordered[0]
                threshold = self.sk.tier1_threshold(
                    now=replay_ts,
                    addressee_confidence=last_result.get(
                        "addressee_confidence"
                    ),
                )
                t1 = self._tier1_question(
                    first["text"], question,
                    key=first["player"]
                    or f"unrostered:{first['speaker_label']}",
                    threshold=threshold,
                )
                if t1["verdict"] == "correct":
                    self.send_event_nowait(
                        "lock", {"name": first.get("player")}
                    )
                    asyncio.ensure_future(
                        self.adjudicate(steal_allowed=False)
                    )
        except Exception as e:
            logger.warning(
                "LILY_ANSWER | PRE_WINDOW_TIER1_FAILED: %s", e
            )

    def maybe_fire_late_recognition(self) -> None:
        """WO-LILY-RECOGNITION-VARIETY-001 Task 1 — the catch-up path.

        Fires when group resolution lands on an EXISTING group AFTER the
        greeting has already gone out (the 08-04 fixture: a cold device,
        the name-hash resolving a six-session regular mid-call, and then
        NOTHING — she stayed amnesiac to a regular the whole game).
        One acknowledgment beat, once per session; the delta line, prefs
        offer, and refresher rules then apply exactly as if recognition
        had landed at the door. A genuinely new group has no memory block
        and triggers nothing. Device-id resolution at the door remains
        the fast path — if the greeting hasn't gone out yet, the greeting
        itself acts on the memory and this stays silent."""
        if not self.memory_block or self._late_recognition_fired:
            return
        # getattr: test harnesses build LilyGame via __new__.
        registry = getattr(self, "say_registry", None)
        greeted = (
            registry is not None
            and registry.state("session_greet") is not None
        )
        if not greeted and not getattr(self, "game_started", False):
            return  # door path: greeting_instructions will act on it
        self._late_recognition_fired = True
        # Stored 'usual' honored for the remainder: apply stored pacing
        # only when this session hasn't spoken its own choice.
        try:
            stored_pacing = (self.prefs or {}).get("pacing")
            if stored_pacing in ("timed", "relaxed") and (
                stored_pacing != self.sk.pacing
            ):
                self.sk.set_pacing(stored_pacing)
                self.publish_attributes_nowait()
        except Exception:
            pass
        names = ", ".join((self.memory_player_names or [])[:4])
        ack = (
            "Recognition just landed MID-SESSION: the [RETURNING TABLE] "
            "block now carries who this table really is"
            + (f" ({names})" if names else "")
            + ". ONE warm, specific acknowledgment beat — the shape of "
            "'wait — Rami! NOW I've got you: reigning champion, four "
            "wins.' Own the late catch lightly ('took me a second'); "
            "never pretend you knew all along, and never apologize in a "
            "spiral. Then fold in, same breath or the next, what a "
            "recognized returner would have gotten at the door: no "
            "walkthrough — offer ONCE 'want a refresher on the options, "
            "or straight in?' and respect the answer."
            + self.prefs_offer_instruction()
            + self.whats_new_instruction()
        )
        logger.info(
            "LILY_MEMORY | LATE_RECOGNITION | session=%s group=%s names=%s",
            self.sk.session_id, getattr(self, "group_id", None), names or "-",
        )
        self.instructed_reply(ack)

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
        try:
            self.sk.record_agent_turn(
                clean,
                timestamp=time.time(),
                act_keys=act_keys,
                interrupted=interrupted,
            )
        except Exception as e:
            logger.warning("LILY_TURNS | local record failed: %s", e)
        batcher = getattr(self, "transcripts", None)
        if batcher is None:
            return
        try:
            batcher.add(
                clean + (" …[cut off]" if interrupted else ""),
                speaker_label="LILY",
                speaker_name=(act_keys[0] if act_keys else None),
            )
        except Exception as e:
            logger.warning("LILY_TURNS | transcript persist failed: %s", e)

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

    async def show_demo_picture(self) -> tuple[bool, str]:
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
        if supabase is not None:
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
                supabase, session_id=self.sk.session_id
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

    async def stage_device_candidate(
        self,
        candidate_group_id: str,
        source: str,
    ) -> bool:
        """Load device-linked data into quarantine, never vocal context."""
        if (
            not candidate_group_id
            or self.supabase is None
            or self.forget_state in ("executing", "done", "failed")
        ):
            return False
        memory, prefs, voiceprints = await asyncio.gather(
            lily_memory.lily_load_group_memory(
                self.supabase, candidate_group_id
            ),
            lily_persistence.lily_load_group_prefs(
                self.supabase, candidate_group_id
            ),
            lily_persistence.lily_load_voiceprints(
                self.supabase, candidate_group_id
            ),
        )
        block = lily_memory.lily_build_memory_block(memory, prefs=prefs)
        if not block and not prefs and not voiceprints:
            logger.info(
                "LILY_MEMORY | DEVICE_CANDIDATE_EMPTY | source=%s group=%s",
                source, candidate_group_id,
            )
            return False
        self.device_candidate_group_id = candidate_group_id
        self.device_candidate_source = source
        self._device_candidate_memory = memory or {}
        self._device_candidate_memory_block = block
        self._device_candidate_prefs = dict(prefs or {})
        self._device_candidate_voiceprints = [
            {
                "group_id": candidate_group_id,
                "player_name": row.get("label"),
                "speaker_label": row.get("label"),
                "speaker_identifiers": row.get("speaker_identifiers"),
            }
            for row in voiceprints or []
            if row.get("speaker_identifiers")
        ]
        self.memory_settled.set()
        logger.info(
            "LILY_MEMORY | DEVICE_CANDIDATE_STAGED | source=%s group=%s "
            "memory=%s voices=%d — quarantined until voice match",
            source, candidate_group_id, bool(block),
            len(self._device_candidate_voiceprints),
        )
        return True

    def request_device_verification(self, trigger: str) -> None:
        """Schedule one best-effort candidate voice check."""
        if (
            not getattr(self, "device_candidate_group_id", None)
            or getattr(self, "device_identity_verified", False)
            or getattr(self, "device_identity_rejected", False)
            or getattr(self, "stt", None) is None
        ):
            return
        task = getattr(self, "_device_verify_task", None)
        if task is not None and not task.done():
            return
        self._device_verify_task = asyncio.ensure_future(
            self.verify_device_candidate(trigger)
        )

    async def verify_device_candidate(self, trigger: str) -> bool | None:
        """Promote candidate memory on voice overlap; reject on mismatch.

        None means Speechmatics has not produced identifiers yet, so the
        candidate remains quarantined and a later finalized turn retries.
        """
        candidate = getattr(self, "device_candidate_group_id", None)
        if not candidate or getattr(self, "stt", None) is None:
            return None
        get_ids = getattr(self.stt, "get_speaker_ids", None)
        if get_ids is None:
            return None
        try:
            current = get_ids()
            if asyncio.iscoroutine(current) or isinstance(
                current, asyncio.Future
            ):
                current = await asyncio.wait_for(current, timeout=3.0)
        except Exception as exc:
            logger.warning(
                "LILY_MEMORY | DEVICE_VERIFY_PENDING | trigger=%s error=%s",
                trigger, exc,
            )
            return None
        if not current:
            logger.info(
                "LILY_MEMORY | DEVICE_VERIFY_PENDING | trigger=%s "
                "reason=no_current_voice_ids",
                trigger,
            )
            return None
        matched = lily_memory.lily_match_group_by_voiceprints(
            current, self._device_candidate_voiceprints
        )
        if matched == candidate:
            await self._promote_device_candidate(trigger)
            return True
        # Label round-trip (2026-07-16 fix): exact identifier-string
        # overlap can NEVER match across sessions — Speechmatics refreshes
        # the identifier blobs each session for the same voice (verified
        # in production: 7 same-voice rows, 7 distinct strings). The
        # durable confirmation is the label: known_speakers were injected
        # under this candidate's player-name labels, and the engine
        # assigns one of those labels only when ITS biometric match
        # recognizes the live voice.
        if lily_memory.lily_candidate_labels_confirmed(
            current, self._device_candidate_voiceprints
        ):
            logger.info(
                "LILY_MEMORY | DEVICE_VERIFY_LABEL_MATCH | trigger=%s "
                "group=%s — vendor recognition via injected label",
                trigger, candidate,
            )
            await self._promote_device_candidate(trigger)
            return True
        roster_size = 1
        try:
            roster_size = max(1, int(self.sk.roster_size()))
        except Exception:
            pass
        current_speakers = len(current) if isinstance(current, (list, tuple)) else 1
        if trigger != "game_start" or current_speakers < roster_size:
            logger.info(
                "LILY_MEMORY | DEVICE_VERIFY_PENDING | trigger=%s "
                "reason=no_overlap_yet current_speakers=%d roster=%d",
                trigger, current_speakers, roster_size,
            )
            return None
        self.device_identity_rejected = True
        self.device_candidate_group_id = None
        self.device_candidate_source = None
        self._device_candidate_memory = None
        self._device_candidate_memory_block = ""
        self._device_candidate_prefs = {}
        self._device_candidate_voiceprints = []
        logger.warning(
            "LILY_MEMORY | DEVICE_CANDIDATE_REJECTED | trigger=%s "
            "reason=voice_mismatch — live session remains a new table",
            trigger,
        )
        return False

    async def _promote_device_candidate(self, trigger: str) -> None:
        candidate = self.device_candidate_group_id
        if not candidate:
            return
        memory = dict(self._device_candidate_memory or {})
        block = self._device_candidate_memory_block
        staged_prefs = dict(self._device_candidate_prefs)
        await self.upgrade_group_id(candidate, "voiceprint_match")
        merged_prefs = staged_prefs
        merged_prefs.update(self.prefs or {})
        self.prefs = merged_prefs
        self.memory_block = block
        self.memory_total_games = int(memory.get("total_games") or 0)
        self.memory_player_names = list(memory.get("player_names") or [])
        self.device_identity_verified = True
        self.device_candidate_group_id = None
        self.device_candidate_source = None
        self._device_candidate_memory = None
        self._device_candidate_memory_block = ""
        self._device_candidate_prefs = {}
        self._device_candidate_voiceprints = []
        self.memory_settled.set()
        # Task 1 (RECOGNITION-VARIETY): a voiceprint verification landing
        # after the greeting is the same late-recognition moment as a
        # name-hash upgrade — same acknowledgment beat, same one-shot.
        self.maybe_fire_late_recognition()
        logger.info(
            "LILY_MEMORY | DEVICE_CANDIDATE_VERIFIED | trigger=%s group=%s "
            "— returning memory promoted",
            trigger, candidate,
        )

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
        elif self.memory_block:
            parts.append(
                "PART TWO — your memory KNOWS this table (the "
                "[RETURNING TABLE] context has who they are), so act on it "
                "instead of asking: compose the recognition per player. "
                "Whole table returning: '...welcome back, all of you.' "
                "MIXED table (voices or names the memory doesn't list, "
                "alongside the regulars): welcome the returners BY NAME "
                "and the newcomers separately — '...welcome back, Rami — "
                "and hello to the new faces.' Reference last game's winner "
                "and lean into the rematch energy. Do NOT ask if it's "
                "their first time. Returners get no walkthrough — offer "
                "ONCE, 'want a refresher on the options, or straight in?', "
                "and respect the answer; newcomers at a mixed table get "
                "the short version of the options, aimed at them. The "
                "walkthrough or refresher draws on WHAT THE TABLE CAN ASK "
                "FOR and happens at most once tonight."
                + self.memory_disclosure_instruction()
                + self.prefs_offer_instruction()
                + self.whats_new_instruction()
            )
        else:
            # Neutral-history rule: without memory data, never claim OR deny
            # prior contact (memory may still resolve mid-lobby via a
            # group-id upgrade).
            parts.append(
                "PART TWO — your memory gives no answer about this table, "
                "so ask: a plain warm welcome, then whether it's their "
                "first time playing with you. FIRST TIME: walk them "
                "through their options naturally, drawing on the WHAT THE "
                "TABLE CAN ASK FOR block — conversational, folded into the "
                "banter, never a feature list read aloud. CLAIMED "
                "RETURNER — they say it's NOT their first time but your "
                "memory has nothing: name the gap plainly and honestly in "
                "ONE light beat ('my table card doesn't have you tonight "
                "— new device, maybe') and IN THE SAME TURN offer the "
                "refresher exactly as a recognized returner would get it "
                "— 'want a refresher on the options, or straight in?' — "
                "and respect the answer. Never perform vague amnesia you "
                "could explain, never claim recognition you don't have, "
                "and never argue with their memory of you. If recognition "
                "catches up mid-game (the [RETURNING TABLE] block "
                "appears), you'll get an instruction beat for it. Either "
                "way the walkthrough or refresher happens at most once "
                "tonight. Never claim you remember them, and never "
                "announce it's their first time — let them tell you."
            )
        parts.append(
            " Bind names as people speak. When the table feels ready — "
            "the first genuine group laugh, or a clear 'start' — call "
            "lily_begin_round to open round one; nothing scores until "
            "you do."
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
        return CATEGORY_FAMILIES[(rnd - 1) % len(CATEGORY_FAMILIES)]

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
        entirely. Adult mode and the final wager round stay text. In
        pictures mode, round REAL_OR_IMAGINED_ROUND is the reference
        picture round (every question); otherwise the first question of
        each round is a real-entity 'name this landmark' slot."""
        if self.sk.media_mode != "pictures" or self.sk.mode == "adult":
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

    def _register_draw(self, question: dict) -> bool:
        """Draw idempotency (G2): claim a freshly drawn question by id +
        normalized-text hash the moment the supply line lands it. Returns
        False when this exact question was already drawn this session —
        the caller discards the duplicate instead of prefetching it twice
        (the live q_0492 class: draw two ran before serving one
        registered). getattr: test harnesses build LilyGame via __new__."""
        drawn_ids = getattr(self, "_drawn_ids", None)
        if drawn_ids is None:
            self._drawn_ids = drawn_ids = set()
            self._drawn_hashes = set()
        qid = str(question.get("id") or "")
        qhash = lily_bank.lily_question_text_hash(question.get("prompt"))
        if (qid and qid in drawn_ids) or qhash in self._drawn_hashes:
            return False
        if qid:
            drawn_ids.add(qid)
        self._drawn_hashes.add(qhash)
        return True

    def start_prefetch(self) -> None:
        """Prefetch N+1 in the background while the current question plays
        out. Failure writes an honest status note (§11.2)."""
        if self._prefetch_task and not self._prefetch_task.done():
            return
        if self.next_question is not None or self.game_over:
            return

        async def _prefetch() -> None:
            try:
                await _prefetch_inner()
            except asyncio.CancelledError:
                raise
            except Exception:
                # This coroutine runs as a fire-and-forget task: an escaped
                # exception used to vanish ("Task exception was never
                # retrieved") and the guard in start_prefetch would then
                # block every retry — the 2026-07-15 silent-stall class.
                logger.exception(
                    "LILY_PREFETCH | CRASHED | session=%s q=%d",
                    self.sk.session_id, self.sk.question_number,
                )
                self.sk.set_status_note(
                    "question machine failure: the next question did not "
                    "arrive — tell the table honestly and vamp; do not "
                    "invent an explanation"
                )

        async def _prefetch_inner() -> None:
            # Deck identity for THIS draw (WO-LILY-DESYNC-HONESTY-001 D):
            # captured once at the top — if the sticky mode flips while the
            # draw is in flight (adult entry / back-to-normal), the commit
            # guard below discards the wrong-deck question instead of
            # serving it (the "wait, THAT's the adult section?" class).
            supply_mode = self.sk.mode
            rnd = self._round_for_next_question()
            category = self._category_for_round(rnd)
            tier = self._difficulty_for_round(rnd)
            # Multiple-choice WO: the format of the round this question is
            # FOR (schedule/override), decided at prefetch time.
            mc = self.sk.format_for_round(rnd) == "multiple_choice"

            # Per-group asked history (migration 010): bank draws exclude
            # the group's served kb_ ids and normalized-text hashes — PLUS
            # this session's already-drawn set (G2): a question drawn but
            # not yet served (or discarded) must never be drawn again;
            # arm-time registration alone left the window the live q_0492
            # double-draw ran through.
            if getattr(self, "_drawn_ids", None) is None:
                # Test harnesses build LilyGame via __new__ without the
                # full __init__ attribute set.
                self._drawn_ids = set()
                self._drawn_hashes = set()
            history_ids = (
                lily_bank.lily_history_question_ids(self.asked_history)
                | self._drawn_ids
            )
            history_hashes = (
                lily_bank.lily_history_hashes(self.asked_history)
                | self._drawn_hashes
            )
            # Answer-level no-repeat (migration 017): steer generation away
            # from facts this group has already played, in any wording.
            history_answers = sorted(
                lily_bank.lily_history_answers(self.asked_history)
            )

            # Picture supply (WO-LILY-OMNIBUS-002 H/I/J/K): pictures-mode
            # slots are served by the REASONING node's picture builders —
            # the web/image stacks never touch the vocal path. ANY failure
            # falls through to the standard text supply below (text-only
            # fallback; a broken image pipeline never stalls the game).
            question = None
            picture_kind = self._picture_kind_for_slot(rnd)
            if picture_kind is not None:
                question = await self.reasoning.prefetch_picture_question(
                    self.supabase,
                    kind=picture_kind,
                    question_index=self.sk.question_number,
                    session_id=self.sk.session_id,
                    mode=supply_mode,
                    exclude_ids=history_ids, exclude_hashes=history_hashes,
                )

            # Runbook fallback: LILY_KB_ONLY flips supply to the curated
            # bank; bank questions bypass verification (§4.5). Text supply
            # only runs when no picture question landed above.
            if question is None:
                from_bank = None
                if lily_config.kb_only() and self.supabase is not None:
                    # Bounded: the sync client has no HTTP timeout of its
                    # own — an unbounded hang here wedges the supply line.
                    from_bank = await asyncio.wait_for(
                        lily_persistence.lily_fetch_bank_question(
                            self.supabase, category, tier, self.used_prompts,
                            mode=supply_mode,
                            exclude_ids=history_ids,
                            exclude_hashes=history_hashes,
                        ),
                        timeout=20.0,
                    )
                question = await self.reasoning.prefetch_question(
                    self.sk,
                    category=category,
                    difficulty_tier=tier,
                    avoid_questions=self.used_prompts,
                    from_bank=from_bank,
                    multiple_choice=mc,
                    avoid_answers=history_answers,
                )
                if question is not None and not str(
                    question.get("id", "")
                ).startswith("kb_"):
                    # Generated (verified) question: asked-history check,
                    # category-proposal gating (F), then banking (D).
                    question = self._curate_generated_question(
                        question, category, history_hashes
                    )
            if question is None and self.supabase is not None:
                # Generation failed — curated bank is the insurance policy.
                # Bounded for the same reason as above: the insurance line
                # must never hang the supply task.
                question = await asyncio.wait_for(
                    lily_persistence.lily_fetch_bank_question(
                        self.supabase, category, tier, self.used_prompts,
                        mode=supply_mode,
                        exclude_ids=history_ids, exclude_hashes=history_hashes,
                    ),
                    timeout=20.0,
                )
                if question is not None:
                    self.sk.clear_status_notes()
                    if mc:
                        # Bank rows carry no choices — synthesize here too.
                        await self.reasoning.ensure_choices(question)
            # G2 final idempotency gate: whatever the supply source, a
            # question that was already drawn this session is a duplicate
            # — discard it rather than prefetch it twice. (Generated
            # questions are usually caught upstream by the hash exclusion;
            # this gate is what covers bank/picture rows and any supply
            # path that ignored the exclusion lists.)
            if question is not None and not self._register_draw(question):
                logger.warning(
                    "LILY_PREFETCH | DUPLICATE_DRAW_DISCARDED | session=%s "
                    "id=%s prompt=%r",
                    self.sk.session_id, question.get("id"),
                    str(question.get("prompt", ""))[:80],
                )
                question = None
            # Mode-switch commit guard (WO-LILY-DESYNC-HONESTY-001 D): the
            # sticky mode flipped while this draw was in flight — the
            # question came from the OLD deck. Discard it; the mode-switch
            # flush already relaunched a fresh draw from the new deck (and
            # the idle watchdog backstops). The question stays in the
            # drawn-set on purpose: a flushed/discarded draw is never
            # re-served this session.
            if question is not None and self.sk.mode != supply_mode:
                logger.info(
                    "LILY_PREFETCH | MODE_SWITCH_DISCARD | session=%s "
                    "drawn_for=%s mode_now=%s id=%s",
                    self.sk.session_id, supply_mode, self.sk.mode,
                    question.get("id"),
                )
                question = None
            if question is not None and self.sk.media_mode != "pictures":
                # Picture exclusion in voice_only (sub-agent K): a bank
                # row's cached image never rides into a voice-only session.
                question.pop("image_url", None)
                question.pop("image_license_note", None)
                question.pop("image_prompt", None)
                if question.get("image_source"):
                    question["image_source"] = "none"
            if question is not None:
                self.next_question = question
                # Auto-advance (frozen-reveal deadlock fix): when the reveal
                # consumed the previous question BEFORE this prefetch landed,
                # arm_next_question() at reveal time returned False and no
                # later code path arms or asks — Lily only takes a turn when
                # someone speaks, so a quiet table stares at a stale reveal
                # forever. If the game is live and idle, arm and nudge now.
                if (
                    self.game_started
                    and not self.game_over
                    and self.armed_question is None
                    and not self.sk.answer_window_open
                    and not self._adjudicating
                    and not getattr(self, "_question_transitioning", False)
                ):
                    if self.arm_next_question() and self.session is not None:
                        logger.info(
                            "LILY_STATE | PREFETCH_AUTO_ADVANCE | session=%s q=%d",
                            self.sk.session_id, self.sk.question_number,
                        )
                        # Keyless nudge; structural delivery claim (desync
                        # WO Sub-agent B): the nudged turn IS the delivery
                        # — it claims q_{N}_delivery in tts_node at
                        # dispatch regardless of phrasing, so a racing
                        # second deliverer stays silent.
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
                            source="prefetch_auto_advance",
                        )
            self.publish_attributes_nowait()

        self._prefetch_task = asyncio.ensure_future(_prefetch())

    # -- idle watchdog (live 2026-07-15 stall class) --------------------------
    #
    # The supply line is fire-and-forget tasks gated on one-shot triggers
    # (reveal-time arm, prefetch-completion auto-advance). The 2026-07-15
    # session proved one silent task death wedges the whole game: nothing
    # armed, nothing prefetching, no error spoken — Lily freestyles and the
    # scoreboard freezes. The watchdog makes "game live but idle" a state
    # that always self-heals within one tick.

    WATCHDOG_INTERVAL_SECONDS = 10.0
    PREFETCH_HARD_TIMEOUT_TICKS = 9  # ~90s: past every internal timeout

    def start_idle_watchdog(self) -> None:
        # getattr: test harnesses build LilyGame via __new__ without the
        # full __init__ attribute set.
        task = getattr(self, "_watchdog_task", None)
        if task and not task.done():
            return
        self._prefetch_stall_ticks = 0
        self._armed_limbo_ticks = 0
        self._watchdog_task = asyncio.ensure_future(self._idle_watchdog())

    async def _idle_watchdog(self) -> None:
        while not self.game_over:
            await asyncio.sleep(self.WATCHDOG_INTERVAL_SECONDS)
            try:
                if not self.game_started or self.game_over:
                    continue
                if (
                    self.sk.answer_window_open
                    or self._adjudicating
                    or getattr(self, "_question_transitioning", False)
                ):
                    self._prefetch_stall_ticks = 0
                    self._armed_limbo_ticks = 0
                    continue
                if self.armed_question is not None:
                    # Armed with a CLOSED window and no ruling in flight.
                    # Pre-delivery that's normal (the delivery-nudge
                    # machinery owns it) — but if the delivery claim is
                    # already CONFIRMED, this question was asked, played
                    # out, and then the post-delivery chain died (live
                    # 2026-07-15 04:05: adjudication crashed between the
                    # answer commit and the reveal publish; the game
                    # parked on Q3 forever while the watchdog trusted
                    # "armed = in progress"). Recover deterministically.
                    self._prefetch_stall_ticks = 0
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
                                    self.adjudicate(steal_allowed=False)
                                )
                            else:
                                logger.error(
                                    "LILY_WATCHDOG | ARMED_LIMBO | session=%s "
                                    "q=%d — delivery confirmed, window closed, "
                                    "no candidates: reopening the window",
                                    self.sk.session_id, self.sk.question_number,
                                )
                                self.open_window()
                    else:
                        self._armed_limbo_ticks = 0
                    continue
                self._armed_limbo_ticks = 0
                # Game live but idle: nothing armed, no window, no ruling
                # in flight. Someone must move the game — that someone is
                # now guaranteed to exist.
                if self.next_question is not None:
                    if self.arm_next_question() and self.session is not None:
                        logger.warning(
                            "LILY_WATCHDOG | IDLE_REARM | session=%s q=%d",
                            self.sk.session_id, self.sk.question_number,
                        )
                        # Structural delivery claim (desync WO Sub-agent
                        # B): the nudged turn registers the delivery.
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
                    continue
                task = self._prefetch_task
                if task is None or task.done():
                    self._prefetch_stall_ticks = 0
                    logger.warning(
                        "LILY_WATCHDOG | IDLE_REPREFETCH | session=%s q=%d",
                        self.sk.session_id, self.sk.question_number,
                    )
                    self.start_prefetch()
                    continue
                # A prefetch task is alive but the game has been idle for
                # this long — it outlived every internal timeout, so treat
                # it as hung: cancel and let the next tick relaunch.
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
            except Exception:
                logger.exception("LILY_WATCHDOG | TICK_FAILED")

    # -- mode-switch flush + re-arm (WO-LILY-DESYNC-HONESTY-001 D) -------------
    #
    # The 2026-07-15 adult segment opened on a LEFTOVER general question
    # ("powerhouse of the cell" — user: "wait, THAT's the adult section?")
    # because the armed queue survived the mode switch, and then served
    # identity-less freestyle questions because the supply gap left nothing
    # armed. The flush closes both: no question survives a deck change, and
    # the immediate re-prefetch keeps the gap to one honest beat.

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
            question["category"] = (
                proposed if proposed in self.promoted_categories else family
            )
        if self.supabase is not None:
            asyncio.ensure_future(lily_bank.lily_bank_generated_question(
                self.supabase, dict(question), self.sk.mode,
            ))
        return question

    def arm_next_question(self) -> bool:
        """Move the prefetched question into the state block for Lily to
        perform. Returns True if a question is armed."""
        if self.armed_question is not None:
            return True
        if self.next_question is None:
            self.start_prefetch()
            return False
        self.armed_question = self.next_question
        self.next_question = None
        self.sk.start_question(self.armed_question)
        self.sk.round = self._round_for_next_question()
        # Correct for start_question incrementing question_number first:
        self.sk.round = min(
            self.rounds_total + 1,
            (self.sk.question_number - 1) // self.sk.questions_per_round + 1,
        )
        if self.sk.round > self.rounds_total:
            if self.prewager_standings is None:
                # Entering the wager round: freeze standings so the finale
                # can detect a comeback (winner ≠ pre-wager leader).
                self.prewager_standings = sorted(
                    self._players_payload(), key=lambda p: -p["score"]
                )
            self.sk.set_phase("final")
        else:
            self.sk.set_phase("round")
        # Round format follows the round (multiple-choice WO): schedule or
        # sticky override; 50/50 eliminations are per-question.
        self.sk.apply_round_format_for_round(self.sk.round)
        self.eliminated = []
        self.used_prompts.append(self.armed_question.get("prompt", ""))
        # Asked history (migration 010): one row per question SERVED,
        # keyed to the resolved group id; the in-memory mirror keeps this
        # session's own draws excluded without a re-read.
        self.asked_history.append({
            "question_id": self.armed_question.get("id"),
            "question_text_hash": lily_bank.lily_question_text_hash(
                self.armed_question.get("prompt")
            ),
            "canonical_answer": self.armed_question.get("canonical_answer"),
        })
        if self.supabase is not None:
            asyncio.ensure_future(lily_bank.lily_record_asked(
                self.supabase, self.group_id, dict(self.armed_question),
                self.sk.session_id,
            ))
        self._armed_speech_misses = 0
        self._pending_delivery_qnum = None  # stale delivery intent dies at arm
        self._pre_window_segments = []  # early-buzz buffer is per-question
        self._strict_delivery_qnum = None
        self._judged_keys = set()
        self._addressee_rows = {}  # B1: row-id tasks are per-question
        for _task in self._spec_judge.values():
            if not _task.done():
                _task.cancel()
        self._spec_judge = {}
        self._nbest_by_key = {}  # n-best dicts are per-question
        if self.ui_phase == "lobby":
            # Voice/glass sync (2026-07-31 live report): the FIRST question
            # arms while Lily is still greeting — flipping the published
            # phase here replaced the lobby with the game board mid-
            # salutation. Hold the published phase on the lobby until the
            # delivery turn's playout opens the window (open_window
            # publishes phase=answering and clears the hold).
            self._phase_hold = "lobby"
        self._set_ui_phase("question")
        # Metadata publish moved to WINDOW-OPEN time (delivery playout
        # completion): the old arm-time publish clobbered the reveal
        # milliseconds after adjudication, and the interim dispatch-time
        # publish still led the voice by the length of any audio queued
        # ahead of the delivery turn (greetings, celebration) — screen
        # truth must equal SPOKEN truth, so the glass syncs at playout.
        self.start_prefetch()  # N+2 begins while N+1 plays out
        return True

    def restore_reconnected_state(self) -> None:
        """Restore the unresolved checkpoint question without incrementing.

        ``arm_next_question`` cannot be used here because it calls
        ``start_question`` and advances the question number. A reconnect
        instead replays the checkpointed question into a fresh answer
        window, while the normal supply task prepares N+1.
        """
        self.game_started = True
        question = self.sk.current_question
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
    ) -> None:
        """Called on TTS playback completion (agent stops speaking). If the
        armed question's delivery is REGISTERED (the q_{N}_delivery claim —
        structural, never text-similarity), the answer window opens HERE —
        never earlier. (The old "no early buzz-ins" v1 concession is
        retired: finals spoken during the delivery playout buffer via
        buffer_pre_window_answer and replay at open — fixture Q5.)"""
        # A deterministic instruction speech finished playing out — its
        # items are committed, the persistent context is stable again, so
        # preemptive generation can resume (P2).
        self._resume_preemptive()
        # Say gate: playout completed — pending speech-act claims are now
        # genuinely spoken. Confirmed acts never release (a confirmed act
        # can never be redelivered); only a claimed-but-never-played act
        # releases, on the tts_node playback-failure path.
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
                    self.expect_delivery()
            # Task 0 (RECOGNITION-VARIETY): an INTERRUPTED turn partially
            # played — it belongs in the record, marked. A suppressed turn
            # never reached air and is not recorded as said.
            if interrupted:
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
        # Task 0 (RECOGNITION-VARIETY): BOTH sides of the call persist.
        # Recording at playout completion means the record holds what the
        # room actually heard — never a dispatched-but-swallowed turn.
        self.record_agent_turn(
            spoken_text, act_keys=sorted(confirmed or []), interrupted=False
        )
        # Honesty assist (desync WO Sub-agent C): the state note serviced
        # the turn that just finished playing — one-shot, consumed here.
        self._state_note = None
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
        if any(key.endswith("_reveal") for key in confirmed):
            # The next armed question gets its own strict delivery handle.
            # Do not let the reveal/score turn also freestyle N+1.
            if self.dispatch_armed_question(source="post_reveal"):
                return
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
            logger.info(
                "LILY_WINDOW | RATIO | session=%s q=%d ratio=%.2f | telemetry",
                self.sk.session_id, self.sk.question_number, ratio,
            )
            key = f"q_{self.sk.question_number}_delivery"
            if (
                self.say_registry.state(key)
                == lily_say_gate.CLAIM_CONFIRMED
            ):
                # Delivery is registered (claimed at dispatch, confirmed
                # just above) and its turn finished playing — open.
                self._armed_speech_misses = 0
                logger.info(
                    "LILY_WINDOW | OPEN | session=%s q=%d reason=delivery_claim "
                    "ratio=%.2f",
                    self.sk.session_id, self.sk.question_number, ratio,
                )
                self.open_window()
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
            self._armed_speech_misses = 0
            logger.warning(
                "LILY_WINDOW | DELIVERY_NUDGE | session=%s q=%d ratio=%.2f "
                "— no delivery claim after %d agent turns; dispatching a "
                "structural delivery turn",
                self.sk.session_id, self.sk.question_number, ratio,
                WINDOW_FALLBACK_AGENT_TURNS,
            )
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

    def open_window(
        self, duration: float | None = None, steal: bool = False
    ) -> None:
        dur = duration if duration is not None else self._answer_window_duration()
        self.sk.open_answer_window(duration=dur, reset_candidates=not steal)
        self._steal_window = steal
        self._set_ui_phase("answering")
        self.publish_attributes_nowait()
        # THE screen-sync publish (voice/glass sync fix): the question
        # reaches the glass here — at the delivery turn's playout
        # completion, exactly when answers go live — never at arm or
        # dispatch, both of which led the spoken question. Also drops any
        # pre-delivery phase hold via _set_ui_phase above.
        if not steal and self.armed_question is not None:
            asyncio.ensure_future(
                self.publish_metadata(
                    self.armed_question.get("prompt", ""),
                    choices=self.armed_question.get("choices"),
                    eliminated=self.eliminated,
                    image_url=self.armed_question.get("image_url"),
                    category=self.armed_question.get("category"),
                )
            )
        self._start_bed()
        if self._window_timer and not self._window_timer.done():
            self._window_timer.cancel()

        async def _expire() -> None:
            await asyncio.sleep(dur)
            if self.sk.answer_window_open and not self._adjudicating:
                await self.adjudicate(steal_allowed=not steal)

        self._window_timer = asyncio.ensure_future(_expire())
        # Early-buzz replay (fixture Q5): answers spoken during the
        # delivery playout become candidates NOW that the window is live.
        if not steal:
            self._replay_pre_window_answers()

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

    # -- addressee-label corpus (B1) ----------------------------------------------

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

    def _addressee_row(
        self,
        text: str,
        speaker_label: str | None,
        player: str | None,
        segment_ts: float,
        agent_action: str,
        system_directed: bool,
        prior_state: str | None = None,
        overlap_flag: bool | None = None,
        nbest: dict | None = None,
    ) -> dict:
        """Build one lily_addressee_log row. fuzzy_matched_answer is the
        Tier-1 verdict against the LIVE question's acceptable_answers (null
        when no live question); seconds_into_window comes off the
        scorekeeper's window opened_at. prior_state / overlap_flag
        (WO-ADDRESSEE-H1 Task 2, schema amendment 5a) are passed through
        from the scorekeeper's per-segment result when available, else
        recomputed from the live scorekeeper state at segment_ts. When an
        n-best dict is supplied (Task 1) the fuzzy flag is
        hypothesis-aware (a hit in ANY slot counts — no dispersion gate
        here, the corpus wants the raw match signal) and the asr_n_best /
        n_best_dispersion columns are written; absent -> the keys are
        omitted (SQL NULL, schema amendment 5a)."""
        question = self.sk.current_question or {}
        acceptable = question.get("acceptable_answers") or []
        fuzzy = None
        if self.sk.current_question is not None and acceptable:
            hyps = (nbest or {}).get("hypotheses") or []
            if nbest is not None and len(hyps) > 1:
                fuzzy = (
                    lily_evaluation.lily_tier1_evaluate_nbest(
                        text, {"acceptable_answers": acceptable},
                        hypotheses=hyps,
                    )["verdict"]
                    == "correct"
                )
            else:
                fuzzy = (
                    lily_evaluation.lily_tier1_evaluate(text, acceptable)["verdict"]
                    == "correct"
                )
        window_open = self.sk.is_window_open(now=segment_ts)
        row = {
            "session_id": self.sk.session_id,
            "utterance_ts": datetime.datetime.fromtimestamp(
                segment_ts, datetime.timezone.utc
            ).isoformat(),
            "speaker_label": speaker_label,
            "player_name": player,
            "transcript": text,
            "is_final": True,  # finals only reach this layer
            "phase": self.ui_phase,
            "answer_window_open": window_open,
            "seconds_into_window": (
                lily_addressee.lily_seconds_into_window(
                    self.sk.answer_window_opened_at, segment_ts
                )
                if window_open
                else None
            ),
            "fuzzy_matched_answer": fuzzy,
            "system_directed_hit": bool(system_directed),
            "agent_action": agent_action,
            # WO-ADDRESSEE-H1 Task 2 (columns exist per schema amendment
            # 5a): the prior state the utterance was classified under and
            # whether crosstalk had flipped inside its window.
            "prior_state": (
                prior_state
                if prior_state is not None
                else self.sk.prior_state(now=segment_ts)
            ),
            "overlap_flag": (
                bool(overlap_flag)
                if overlap_flag is not None
                else bool(self.sk.overlap_flag)
            ),
            "label": None,
            "label_source": None,
            # Task 6 addressee synergy: the key is ALWAYS present — a dict
            # when the pipeline is healthy, an explicit SQL null (not an
            # absent column) when the circuit breaker is open or nothing
            # has been captured.
            "acoustic_snapshot": self.acoustic.addressee_snapshot(),
        }
        # Task 1 (WO-ADDRESSEE-H1): hypotheses + dispersion, only when the
        # n-best pipeline delivered for this utterance — absent means the
        # columns stay NULL, never an empty-list sentinel.
        if nbest is not None and nbest.get("hypotheses"):
            row["asr_n_best"] = nbest["hypotheses"]
            row["n_best_dispersion"] = nbest.get("dispersion")
        return row

    def _log_addressee_segment(
        self,
        result: dict,
        text: str,
        speaker_label: str | None,
        segment_ts: float,
        nbest: dict | None = None,
    ) -> None:
        """B1: one insert per finalized segment while an answer window is
        open, plus any finalized segment the agent acts on (scored,
        skipped-on, system-directed). All writes fire-and-forget via
        asyncio.to_thread inside the persistence helper — zero hot-path
        cost. Clarified rows are inserted by mark_pending_clarify."""
        if self.supabase is None:
            return
        window_open = self.sk.is_window_open(now=segment_ts)
        acted_on = (
            result.get("candidate_recorded")
            or result.get("control_command")
            or result.get("system_directed")
        )
        if not window_open and not acted_on:
            return
        if result.get("candidate_recorded"):
            action = lily_addressee.AGENT_ACTION_SCORED
        elif result.get("control_command"):
            # Skipped-on / mode-revert segments: acted on, not scored.
            action = lily_addressee.AGENT_ACTION_ADJUDICATED_OTHER
        else:
            action = lily_addressee.AGENT_ACTION_IGNORED
        row = self._addressee_row(
            text=text,
            speaker_label=speaker_label,
            player=result.get("player"),
            segment_ts=segment_ts,
            agent_action=action,
            system_directed=result.get("system_directed", False),
            prior_state=result.get("prior_state"),
            overlap_flag=result.get("overlap_flag"),
            nbest=nbest,
        )
        task = asyncio.ensure_future(
            lily_persistence.lily_log_addressee(self.supabase, row)
        )
        if result.get("candidate_recorded"):
            # Keep the row-id task so adjudication commit can UPDATE the
            # label instead of double-inserting.
            key = result.get("player") or f"unrostered:{speaker_label or 'UU'}"
            self._addressee_rows[key] = task

    def _apply_addressee_label(
        self, key: str, label: str, label_source: str
    ) -> None:
        """UPDATE the earlier corpus row for a candidate (matched by the
        row id kept at insert time). Fire-and-forget."""
        task = self._addressee_rows.get(key)
        if task is None or self.supabase is None:
            return

        async def _apply() -> None:
            try:
                row_id = await asyncio.shield(task)
            except Exception:
                row_id = None
            if row_id is not None:
                await lily_persistence.lily_update_addressee_label(
                    self.supabase, row_id, label, label_source
                )

        asyncio.ensure_future(_apply())

    def _maybe_fire_clarify(
        self,
        cand: dict,
        t1: dict,
        tier1_threshold: float,
        prior_state: str,
    ) -> None:
        """WO-ADDRESSEE-H1 Task 4 — the explicit-label engine. Fires the
        clarify question when Tier-1 similarity lands in the middle band
        (lily_tier1_band == clarify) under the ACTIVE threshold. Bound:
        at most once per question, at most
        lily_config.clarify_max_per_session() per session; rostered
        players only (the clarify addresses someone by name)."""
        player = cand.get("player")
        if not player:
            return
        similarity = t1.get("similarity")
        if not isinstance(similarity, (int, float)):
            return
        band = lily_evaluation.lily_tier1_band(
            float(similarity), tier1_threshold,
            lily_config.tier1_clarify_margin(),
        )
        if band != lily_evaluation.BAND_CLARIFY:
            return
        if not hasattr(self, "_clarify_fired_questions"):
            self._clarify_fired_questions = set()
            self._session_clarify_count = 0
        qnum = self.sk.question_number
        if qnum in self._clarify_fired_questions:
            return
        if self._session_clarify_count >= lily_config.clarify_max_per_session():
            return
        if player in self.pending_clarify:
            return
        self._clarify_fired_questions.add(qnum)
        self._session_clarify_count += 1
        logger.info(
            "LILY_CLARIFY | BAND_TRIGGER | session=%s q=%d player=%s "
            "similarity=%.3f threshold=%.3f prior=%s count=%d",
            self.sk.session_id, qnum, player, similarity,
            tier1_threshold, prior_state, self._session_clarify_count,
        )
        self.mark_pending_clarify(player)
        self.gated_say(
            f"q_{qnum}_clarify",
            "clarify_question",
            (
                f"{player} just said something that lands close to an "
                "answer but not committed. Ask them the binary, by name, "
                f"quick and light: '{player} — answer, or thinking out "
                "loud?' One short question, nothing else; their reply "
                "settles it."
            ),
            source="clarify_band",
        )

    def mark_pending_clarify(self, player_name: str) -> None:
        """The clarify moment (lily_log_clarify tool): mark the player
        pending-clarify, emit the `clarify` packet, and log the clarified
        utterance row with agent_action=clarified. The player's NEXT
        finalized segment resolves the label (explicit ground truth)."""
        # The utterance being clarified: their committed answer candidate
        # if one exists, else their most recent transcript-buffer line.
        cand = self.sk.answer_candidates.get(player_name)
        if cand is not None:
            clarified_text = cand["text"]
            clarified_label = cand.get("speaker_label")
            clarified_ts = cand.get("segment_start_time") or time.time()
        else:
            clarified_text, clarified_label, clarified_ts = None, None, time.time()
            for entry in reversed(self.sk.transcript_buffer):
                if entry.get("speaker") == player_name:
                    clarified_text = entry.get("text")
                    clarified_label = entry.get("speaker_label")
                    break
        row_task = None
        if self.supabase is not None and clarified_text:
            row = self._addressee_row(
                text=clarified_text,
                speaker_label=clarified_label,
                player=player_name,
                segment_ts=clarified_ts,
                agent_action=lily_addressee.AGENT_ACTION_CLARIFIED,
                system_directed=False,
            )
            row_task = asyncio.ensure_future(
                lily_persistence.lily_log_addressee(self.supabase, row)
            )
        self.pending_clarify[player_name] = {"row_task": row_task}
        self.send_event_nowait("clarify", {"name": player_name})
        logger.info(
            "LILY_CLARIFY | PENDING | session=%s player=%s utterance=%r",
            self.sk.session_id, player_name, (clarified_text or "")[:80],
        )

    def _resolve_clarify(self, player_name: str, reply_text: str) -> None:
        """The clarified player's next finalized segment: parse the reply
        (pure, lily_addressee) and UPDATE the clarified row's label with
        label_source=explicit_clarify, then clear pending."""
        pending = self.pending_clarify.pop(player_name, None)
        if pending is None:
            return
        label = lily_addressee.lily_parse_clarify_reply(reply_text)
        logger.info(
            "LILY_CLARIFY | RESOLVED | session=%s player=%s label=%s reply=%r",
            self.sk.session_id, player_name, label, reply_text[:80],
        )
        row_task = pending.get("row_task")
        if row_task is None or self.supabase is None:
            return

        async def _apply() -> None:
            try:
                row_id = await asyncio.shield(row_task)
            except Exception:
                row_id = None
            if row_id is not None:
                await lily_persistence.lily_update_addressee_label(
                    self.supabase, row_id, label,
                    lily_addressee.LABEL_SOURCE_EXPLICIT_CLARIFY,
                )

        asyncio.ensure_future(_apply())

    # -- transcript-event layer --------------------------------------------------

    def on_transcript_event(
        self,
        result: dict,
        text: str,
        speaker_label: str | None = None,
        segment_ts: float | None = None,
        nbest: dict | None = None,
    ) -> None:
        """Deterministic enforcement layer (§11.4) — runs on every final.
        `nbest` is the per-utterance hypothesis dict drained from the
        LilyNBestCollector (None when the recovery patch is not armed or
        nothing was buffered) — optional and additive, existing callers
        unchanged."""
        ts = segment_ts if segment_ts is not None else time.time()
        self.request_device_verification("final_transcript")

        # n-best telemetry (WO-ADDRESSEE-H1 Task 1): dispersion logged per
        # utterance — high dispersion is a deliberation signal.
        if nbest is not None:
            timing = nbest.get("segment_timing") or {}
            logger.info(
                "LILY_NBEST | dispersion=%s hypotheses=%d words=%d speaker=%s "
                "timing_source=%s drift_s=%s",
                nbest.get("dispersion"),
                len(nbest.get("hypotheses") or []),
                nbest.get("word_count", 0),
                result.get("player") or speaker_label or "UU",
                timing.get("source"),
                timing.get("drift_seconds"),
            )

        # B1 corpus row — logged BEFORE command handling so skipped-on and
        # system-directed segments are captured too (fire-and-forget).
        self._log_addressee_segment(result, text, speaker_label, ts, nbest=nbest)

        # Task 6: one acoustic-trajectory row per finalized user turn
        # (fire-and-forget; no-op when no capture has landed).
        self.log_acoustic_trajectory()

        # B1 explicit ground truth: a pending clarify is resolved by the
        # NEXT finalized segment from that player, whatever it says.
        player = result.get("player")
        if player and player in self.pending_clarify:
            self._resolve_clarify(player, text)

        command = result.get("control_command")

        # Honesty assist (desync WO Sub-agent C): a player calling out the
        # board/score gets answered from PUBLISHED truth, never guesswork.
        # The detector is conservative (score/board anchor + a checkable
        # claim); commands and media choices never double as callouts. The
        # note rides the state block as context — the say-gate leak filter
        # keeps it off the air if echoed.
        if command is None and not result.get("media_choice"):
            note = lily_detect_state_contradiction(
                text, player, self.sk.players
            )
            if note is not None:
                self._state_note = f"[state note: {note}]"
                logger.info(
                    "LILY_HONESTY | STATE_NOTE | session=%s player=%s "
                    "note=%r",
                    self.sk.session_id, player, note[:120],
                )

        # WO-LILY-FORGETME-001: pending forget confirmation resolves
        # DETERMINISTICALLY (same pattern as the clarify resolution) — the
        # requester's next parseable yes fires the cascade, a no drops it
        # for the night, anything ambiguous does nothing destructive and
        # stays pending. Runs before command dispatch so a "yes, delete
        # it" can never be mis-read as a fresh request.
        if self.forget_state == "pending_confirm" and command != "forget_me":
            speaker_key = player or speaker_label
            if self.forget_requester is None or speaker_key == self.forget_requester:
                verdict = lily_forget.lily_parse_forget_confirmation(text)
                if verdict == "yes":
                    self.forget_spoken_confirmed = True
                    logger.info(
                        "LILY_FORGET | CONFIRMED | session=%s by=%s",
                        self.sk.session_id, speaker_key,
                    )
                    asyncio.ensure_future(
                        self._forget_confirmed(source="voice_confirm")
                    )
                    return
                if verdict == "no":
                    self.forget_state = "declined"
                    self.forget_spoken_confirmed = False
                    logger.info(
                        "LILY_FORGET | DECLINED | session=%s by=%s",
                        self.sk.session_id, speaker_key,
                    )
                    self.gated_say(
                        None,
                        "forget_declined",
                        "They said no — the deletion is dropped and nothing "
                        "was touched. One light line acknowledging it, then "
                        "straight back into the game. Never re-raise it "
                        "this session, never ask twice, and never argue for "
                        "being remembered.",
                        source="voice_confirm",
                    )
                    return

        if command == "forget_me":
            self._on_forget_requested(player or speaker_label)
            return
        if command == "back_to_normal":
            if self.sk.mode == "adult":
                self.sk.set_mode("general")  # sticky flag flips instantly
                # D: no question survives the deck change — the armed
                # adult question is flushed and the general deck re-draws
                # immediately.
                self.flush_for_mode_switch(source="back_to_normal")
                self.publish_attributes_nowait()
                self.gated_say(
                    None,
                    "mode_revert",
                    "A player said 'back to normal'. Adult mode is now "
                    "OFF — committed, in code. Switch registers "
                    "instantly, no ceremony, no residue. The general "
                    "deck is re-drawing: the next question lands in the "
                    "state block in a beat — never re-ask, finish, or "
                    "reveal the adult question; vamp lightly until the "
                    "new one appears.",
                    source="voice_command",
                )
            return
        if command in ("pacing_relaxed", "pacing_timed"):
            # Group prefs WO: the spoken pacing choice is committed in code
            # (flag + persisted prefs) before Lily says a word about it —
            # same contract as "back to normal".
            pacing = "relaxed" if command == "pacing_relaxed" else "timed"
            if self.set_pacing(pacing, source="voice_command"):
                if pacing == "relaxed":
                    note = (
                        "answer windows now run about twice as long. Keep "
                        "the tempo loose from here: no countdown talk, no "
                        "rushing anyone."
                    )
                else:
                    note = "the standard answer clock is back on."
                self.gated_say(
                    None,
                    "pacing_set",
                    f"A player just chose {pacing} pacing — committed, in "
                    f"code, and saved as this table's usual: {note} One "
                    "light line acknowledging it, then keep the night "
                    "moving.",
                    source="voice_command",
                )
            return
        if command == "skip":
            asyncio.ensure_future(self.skip_question(source="voice"))
            return
        if command == "start_game":
            # Spoken start path — the deterministic pipeline must engage
            # even if Lily never calls the lily_begin_round tool.
            if not self.game_started:
                asyncio.ensure_future(self.start_game(source="voice"))
            return

        # Media-mode spoken choice (sub-agent K): sticky flag flips
        # instantly, in code — same discipline as "back to normal".
        media_choice = result.get("media_choice")
        if media_choice and media_choice != self.sk.media_mode:
            self.sk.set_media_mode(media_choice)
            self.publish_attributes_nowait()
            if media_choice == "pictures":
                line = (
                    "The table asked for pictures. Picture rounds are ON — "
                    "committed, in code. One short confirmation (the screen "
                    "is in the game now), then keep moving."
                )
            else:
                line = (
                    "The table asked for voice only. Pictures are OFF — "
                    "committed, in code. One short confirmation, no "
                    "ceremony, keep moving."
                )
            self.gated_say(None, "media_mode", line, source="voice_command")

        # Safety-net auto-start: cheap gate check on every user segment so
        # the game can start off the ambient chatter of a settled lobby —
        # useful for tables that never touched the UI start button and
        # where Lily hasn't yet called lily_begin_round.
        if not self.game_started and not self.game_over:
            self._maybe_auto_start_after_lobby()

        # Instant Tier-1 path: a clean earliest answer scores immediately.
        if result.get("candidate_recorded") and self.sk.answer_window_open:
            # n-best (WO-ADDRESSEE-H1 Task 1): the drained hypothesis set
            # for THIS utterance rides with the candidate key so Tier-1 /
            # Tier-2 at adjudication time can widen the match.
            cand_key = (
                result.get("player") or f"unrostered:{speaker_label or 'UU'}"
            )
            if nbest is not None:
                if not hasattr(self, "_nbest_by_key"):
                    self._nbest_by_key = {}  # __new__-built test harnesses
                self._nbest_by_key[cand_key] = nbest
            # Seam contract `lock` beat: one packet per recorded candidate
            # (first final per player — the scorekeeper dedupes) so the
            # frontend can mark the answer as locked in. Carries the name
            # only; the ANSWER TEXT never rides this packet (need-to-know:
            # nothing spoilable on the wire before the reveal).
            self.send_event_nowait("lock", {"name": result.get("player")})
            question = self.sk.current_question or {}
            acceptable = question.get("acceptable_answers") or []
            ordered = self.sk.ordered_candidates()
            # State-prior threshold (WO-ADDRESSEE-H1 Task 2): OPEN_WINDOW
            # lowers the bar; OVERLAP / HOST_SPEAKING raise it past
            # auto-accept, so crosstalk candidates escalate instead of
            # instantly scoring.
            prior_now = self.sk.prior_state(now=ts)
            seg_addressee_conf = result.get("addressee_confidence")
            tier1_threshold = self.sk.tier1_threshold(
                now=ts,
                addressee_confidence=seg_addressee_conf,
            )
            logger.info(
                "LILY_PRIOR | state=%s threshold=%.3f overlap=%s "
                "addressee_conf=%s | "
                "session=%s q=%d source=instant_tier1",
                prior_now, tier1_threshold, self.sk.overlap_flag,
                (
                    f"{seg_addressee_conf:.3f}"
                    if isinstance(seg_addressee_conf, (int, float))
                    else "None"
                ),
                self.sk.session_id, self.sk.question_number,
            )
            if ordered and acceptable:
                first = ordered[0]
                if first.get("text") == text or len(ordered) == 1:
                    # Format dispatch (multiple-choice WO): a question with
                    # four choices runs the MC matcher (letters, positions,
                    # option text); freeform runs acceptable_answers.
                    # n-best aware: fuzzy matching runs across ALL
                    # hypotheses, and high dispersion demotes a definitive
                    # verdict to "uncertain" (deliberation escalates); the
                    # state-prior threshold rides every evaluation.
                    t1 = self._tier1_question(
                        first["text"], question,
                        key=first["player"]
                        or f"unrostered:{first['speaker_label']}",
                        threshold=tier1_threshold,
                    )
                    if t1["verdict"] == "correct":
                        asyncio.ensure_future(self.adjudicate(steal_allowed=False))
                        return
                    # Formalized clarify trigger (WO-ADDRESSEE-H1 Task 4):
                    # a similarity in the ambiguous MIDDLE BAND under the
                    # active state-prior threshold fires the binary clarify
                    # question DETERMINISTICALLY — named player, "answer,
                    # or thinking out loud?" — and the reply writes an
                    # explicit label through the existing pending-clarify
                    # machinery. Rate-limited: once per question, capped
                    # per session, so the repair stays charming. Outside
                    # the band the classification stands (implicit labels
                    # as wired in B1).
                    self._maybe_fire_clarify(
                        first, t1, tier1_threshold, prior_now
                    )
            # Speculative Tier-2: judge ambiguous candidates DURING the
            # window so the verdict is cached by reveal time — the judge
            # round trip comes off the reveal path (prefetch trick, applied
            # to judging). Only "uncertain" escalates: an MC letter pick of
            # a wrong option is a definitive Tier-1 "incorrect" and never
            # burns a judge call (Tier-2 stays for mumbles).
            if acceptable:
                for cand in ordered:
                    key = cand["player"] or f"unrostered:{cand['speaker_label']}"
                    if key in self._spec_judge or cand.get("text") != text:
                        continue
                    t1 = self._tier1_question(
                        cand["text"], question, key=key,
                        threshold=tier1_threshold,
                    )
                    if t1["verdict"] == "uncertain":
                        self._spec_judge[key] = asyncio.ensure_future(
                            self._speculative_judge(
                                question, cand["text"], key,
                                nbest=self._nbest_lookup(key),
                            )
                        )

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
            if self._window_timer and not self._window_timer.done():
                self._window_timer.cancel()
            self.sk.close_answer_window()
            self._stop_bed()
            self.armed_question = None
            self.sk.current_question = None
            self._set_ui_phase("question")
            await self.publish_metadata("")
            await self.publish_attributes()
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

    async def adjudicate(self, steal_allowed: bool = True) -> None:
        """Close the window and commit results. The scorekeeper decided
        ORDER by timestamps; Tier-1/Tier-2 decide CORRECTNESS; this commit
        happens BEFORE Lily narrates (§11.3 event-bound truth)."""
        if (
            self._adjudicating
            or getattr(self, "_question_transitioning", False)
            or self.armed_question is None
        ):
            return
        self._adjudicating = True
        # SCORING prior (WO-ADDRESSEE-H1 Task 2): mirror the in-flight flag
        # onto the pure scorekeeper so segments arriving DURING adjudication
        # classify under SCORING (backchannels expected, nothing scoreable).
        self.sk.adjudicating = True
        try:
            if self._window_timer and not self._window_timer.done():
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
            ordered = [
                c for c in self.sk.ordered_candidates()
                if (c["player"] or f"unrostered:{c['speaker_label']}")
                not in self._judged_keys
            ]

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
            attempts_timeline = sorted(
                (
                    (attempt.get("segment_start_time", cand["segment_start_time"]),
                     cand, attempt["text"], attempt)
                    for cand in ordered
                    for attempt in (
                        cand.get("attempts")
                        or [{"text": cand["text"],
                             "segment_start_time": cand["segment_start_time"]}]
                    )
                ),
                key=lambda entry: entry[0],
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

            # Commit — scores land in the scorekeeper BEFORE Lily speaks.
            if winner_candidate is not None:
                if winner_candidate["player"]:
                    winner = winner_candidate["player"]
                    self.sk.record_result(winner, correct=True, points=points)
                else:
                    # Open-floor winner: never silently attributed. Hold the
                    # award until lily_bind_speaker lands for that voice.
                    self._pending_unbound_award = {
                        "speaker_label": winner_candidate["speaker_label"],
                        "points": points,
                    }
            for cand in ordered:
                key = cand["player"] or f"unrostered:{cand['speaker_label']}"
                self._judged_keys.add(key)
                is_winner = cand is winner_candidate
                if cand["player"] and not is_winner:
                    self.sk.record_result(cand["player"], correct=False, points=0)
                if self.supabase is not None:
                    asyncio.ensure_future(lily_persistence.lily_write_answer(
                        self.supabase,
                        self.sk.session_id,
                        cand["player"],
                        question.get("id"),
                        self.sk.question_number,
                        cand["text"],
                        "correct" if is_winner else "incorrect",
                        # Tier-1 decided verdicts (correct, or MC's
                        # definitive incorrect) audit as tier 1.
                        1 if cand.get("_tier1", {}).get("verdict")
                        in ("correct", "incorrect") else eval_tier,
                        points if is_winner else 0,
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
            # Steal needs someone who could actually steal (live 2026-07-15
            # fix): candidates persist through the steal window and judged
            # players are filtered, so with every rostered player already
            # judged the window can never record anything — it burned five
            # silent seconds and re-adjudicated an empty set. Solo tables
            # therefore never steal; multiplayer steals only while an
            # unjudged player exists.
            stealers_exist = any(
                name not in self._judged_keys for name in self.sk.players
            )
            if (
                missed and ordered and steal_allowed
                and stealers_exist and not self.game_over
            ):
                # Missed question opens a 5-second steal window.
                self._stinger(correct=False)
                self._adjudicating = False
                self.sk.adjudicating = False
                self.open_window(
                    duration=lily_config.steal_window_seconds(), steal=True
                )
                self.gated_say(
                    None,
                    "steal_window",
                    "Nobody landed it. Announce a five-second steal "
                    "window — quick and hot — anyone who hasn't "
                    "answered can grab it.",
                    source="adjudicate",
                )
                return

            # Reveal — stinger is the ruling; packet fires on TTS playback.
            self._stinger(correct=winner_candidate is not None)
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
                    image_url=question.get("image_url"),
                    category=question.get("category"),
                ),
                self.publish_attributes(),
            )
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
            # Checkpoint only after the consumed question has either been
            # replaced by N+1 or explicitly cleared. A reconnect must never
            # resurrect a question whose result was already committed.
            if self.supabase is not None:
                asyncio.ensure_future(
                    lily_persistence.lily_checkpoint(self.supabase, self.sk)
                )
            # Gated reveal dispatch: the reveal claims q_{N}_reveal; a
            # round-closing reveal also claims round_{N}_scores and the
            # final reveal claims finale — one speech, every act it
            # performs claimed, so no other path can re-deliver them.
            extra: tuple[str, ...] = ()
            act = "reveal"
            if was_final:
                extra, act = ("finale",), "reveal_finale"
            elif round_over:
                extra, act = (f"round_{revealed_round}_scores",), "reveal_scores"
            # Organic-preempt echo guard (live 2026-07-15 20:41): her
            # conversational reply often performs the verdict the moment
            # the instant path commits ("Spot on! Jupiter is correct —
            # 2 for 2"); the scripted reveal then queues behind it and
            # replays the settled ruling seconds later ("Rami came in
            # first and said... Jupiter"), sometimes mid-next-question.
            # If her LAST finished turn already carries this question's
            # answer plus a verdict cue, the reveal act is DONE — claim
            # and confirm the key silently so nothing re-delivers it.
            # Round-closing and finale reveals always dispatch (the
            # standings/finale beats are theirs alone).
            if (
                act == "reveal"
                and self._verdict_already_spoken(question, winner_candidate)
            ):
                key = f"q_{revealed_qnum}_reveal"
                if self.say_registry.claim(key):
                    self.say_registry.confirm(key)
                logger.info(
                    "LILY_REVEAL | ORGANIC_PREEMPTED | session=%s q=%d — "
                    "verdict already spoken in her last turn; scripted "
                    "reveal suppressed",
                    self.sk.session_id, revealed_qnum,
                )
            else:
                self.gated_say(
                    f"q_{revealed_qnum}_reveal",
                    act,
                    reveal_instr,
                    source="adjudicate",
                    extra_keys=extra,
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

    def _verdict_already_spoken(
        self, question: dict, winner_candidate: dict | None
    ) -> bool:
        """True when Lily's most recent finished turn already performed
        this question's verdict — the canonical answer appears in it
        alongside a verdict cue. Compares the answer we hold against words
        SHE already said (need-to-know safe: nothing new can leak)."""
        last = getattr(self, "_last_assistant_text", "") or ""
        if not last.strip():
            return False
        normalized_last = lily_evaluation.lily_normalize_answer(last)
        answer_norm = lily_evaluation.lily_normalize_answer(
            str(question.get("canonical_answer", ""))
        )
        if not answer_norm or answer_norm not in normalized_last:
            return False
        cues = (
            "correct", "right", "spot on", "got it", "nailed", "indeed",
            "that is it", "thats it", "exactly", "well done",
            # miss-side cues — she sometimes reveals the answer while
            # ruling a miss ("ah, it was actually Verona")
            "actually", "not quite", "wrong", "missed", "the answer is",
            "it was",
        )
        return any(c in normalized_last for c in cues)

    def _reveal_instructions(
        self,
        question: dict,
        winner: str | None,
        winner_candidate: dict | None,
        judge_reason: str,
        points: int,
        round_over: bool = False,
        final: bool = False,
    ) -> str:
        answer = question.get("canonical_answer", "")
        color = question.get("reveal_color", "")
        parts = ["The answer window is closed and the ruling is COMMITTED:"]
        if self.sk.pacing == "relaxed":
            # Live 2026-07-15 22:57: "just missed the buzzer" spoken on a
            # relaxed table — there is no buzzer to miss. The reveal
            # framing must match the pacing the table chose.
            parts.append(
                "(The table plays RELAXED — never mention buzzers, timers, "
                "clocks, or anyone being 'too late'. A miss is just a miss.)"
            )
        if winner:
            if len(self.sk.players) <= 1:
                # Solo table (live 2026-07-15 20:41: "Rami came in first"
                # to a table of one — first against whom?). No ordering
                # language, no competitors implied.
                parts.append(
                    f"{winner} got it — {points} point(s) already committed "
                    "to the score. Confirm it warmly, one beat on the "
                    f"answer: {answer!r}. It's just {winner} tonight — "
                    "never say 'first', 'fastest', or anything implying "
                    "other answerers."
                )
            else:
                parts.append(
                    f"{winner} answered first and correctly — {points} point(s) "
                    "already committed to the score. Narrate the call (who was "
                    "first, what they said), suspense hold, then the reveal: "
                    f"the answer is {answer!r}."
                )
        elif winner_candidate is not None and not winner_candidate.get("player"):
            parts.append(
                f"An UNBOUND voice ({winner_candidate['speaker_label']}) got it "
                f"right: the answer is {answer!r}. Do the bit: 'great answer — "
                "and you are?' Get the name, call lily_bind_speaker, and the "
                "point lands automatically once bound."
            )
        else:
            parts.append(
                "Nobody got it (or nobody committed an answer). Reveal with "
                f"suspense: the answer is {answer!r}. Make the miss funny, "
                "never mean. No score changes were committed."
            )
        if judge_reason:
            parts.append(f"Your judge's one-line reasoning: {judge_reason}")
        if color:
            parts.append(f"Reveal color you can use: {color}")
        if final:
            parts.append(
                "That was the FINAL question. Announce the winner from the "
                "state block standings, one redemption beat per player, "
                "repair the loser, offer a rematch."
            )
        elif round_over:
            parts.append(
                "That closes the round. Read FULL standings now (one of your "
                "two allowed spreadsheet moments), then STOP. Do not ask or "
                "invent the next question in this reveal turn; a separate "
                "authoritative delivery turn follows."
            )
        else:
            parts.append(
                "Then one relational score line (deltas, not standings) and "
                "STOP. Do not ask or invent the next question in this reveal "
                "turn; a separate authoritative delivery turn follows."
            )
        return " ".join(parts)

    # -- group identity (persistent memory re-key) -------------------------------

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

    def fire_enrollment(self, trigger: str) -> None:
        """Voiceprint enrollment, fire-and-forget. group_id is passed as a
        callable so the upsert lands under whatever id is resolved by the
        time Speechmatics returns identifiers."""
        if (
            self.supabase is None
            or self.stt is None
            or not self.identity_persistence_allowed()
        ):
            return
        asyncio.ensure_future(lily_persistence.lily_enroll_voiceprints(
            self.stt, self.supabase, lambda: self.group_id, self.sk,
            trigger=trigger,
        ))

    async def upgrade_group_id(self, new_group_id: str, source: str) -> None:
        """Mid-session group-id upgrade: re-key this session's rows to the
        resolved id, reload the [RETURNING TABLE] memory when the game
        hasn't effectively started (no questions played), and re-enroll
        voiceprints under the resolved id."""
        old = self.group_id
        if not new_group_id or new_group_id == old:
            return
        # WO-LILY-FORGETME-001: after a deletion, the fresh anonymous
        # binding is FINAL for this session — no late device metadata, no
        # voiceprint match, no name-set hash may re-key toward (or rebuild)
        # the deleted identity.
        if self.forget_state in ("executing", "done", "failed"):
            logger.info(
                "LILY_FORGET | GROUP_UPGRADE_SUPPRESSED | session=%s "
                "source=%s candidate=%s (post-forget anonymous binding)",
                self.sk.session_id, source, new_group_id,
            )
            return
        self.group_id = new_group_id
        self.group_id_source = source
        logger.info(
            "LILY_MEMORY | GROUP_ID_UPGRADE | session=%s source=%s old=%s new=%s",
            self.sk.session_id, source, old, new_group_id,
        )
        if self.supabase is None:
            self.memory_settled.set()  # nothing to load — greeting unblocks
            return
        await lily_persistence.lily_rekey_group(
            self.supabase, old, new_group_id, self.sk.session_id
        )
        # Asked history follows the resolved id (rekey moved this
        # session's rows; the reload pulls the group's PRIOR sessions so
        # the no-repeat guard covers rematches immediately).
        self.asked_history = await lily_bank.lily_load_asked_history(
            self.supabase, new_group_id
        )
        # Refresh known_speakers under the resolved id. 1.6.6 applies this
        # list at stream start, so this primarily protects reconnect paths.
        if self.stt is not None:
            try:
                known_rows = await lily_persistence.lily_load_voiceprints(
                    self.supabase, new_group_id
                )
                known_speakers = [
                    SpeakerIdentifier(
                        label=row["label"],
                        speaker_identifiers=row["speaker_identifiers"],
                    )
                    for row in known_rows
                    if row.get("label") and row.get("speaker_identifiers")
                ]
                opts = getattr(self.stt, "_stt_options", None)
                if opts is not None:
                    opts.known_speakers = known_speakers
                    logger.info(
                        "VOICEPRINT | refreshed known_speakers=%d group=%s",
                        len(known_speakers),
                        new_group_id,
                    )
            except Exception as e:
                logger.warning(
                    "VOICEPRINT | known_speakers refresh failed group=%s: %s",
                    new_group_id,
                    e,
                )
        # RECOGNITION-VARIETY Task 1: recognition is CONTINUOUS, not a
        # door-check. This block was gated on question_number == 0 —
        # the 08-04 fixture's name-hash resolved a six-session regular
        # MID-CALL and nothing happened: no recall, no acknowledgment,
        # amnesia for the whole game. The load now runs whenever the
        # upgrade lands; maybe_fire_late_recognition() below turns a
        # late resolution into a recovery moment.
        #
        # Group prefs WO: the resolved group may have a stored 'usual'.
        # The re-key above already merged any session-written row under
        # the new id (session choices winning); reconcile the in-memory
        # dict the same way — stored keys slot in UNDER this session's
        # spoken choices, opaquely (round_format / media_mode included).
        stored_prefs = await lily_persistence.lily_load_group_prefs(
            self.supabase, new_group_id
        )
        if stored_prefs:
            merged = dict(stored_prefs)
            merged.update(self.prefs or {})
            self.prefs = merged
            logger.info(
                "LILY_PREFS | RECONCILED | group=%s keys=%s "
                "(post-upgrade)",
                new_group_id, ",".join(sorted(merged.keys())),
            )
        memory = await lily_memory.lily_load_group_memory(
            self.supabase, new_group_id
        )
        block = lily_memory.lily_build_memory_block(
            memory, prefs=self.prefs
        )
        if block:
            self.memory_block = block  # llm_node injects it next turn
            self.memory_total_games = int(
                (memory or {}).get("total_games") or 0
            )
            self.memory_player_names = list(
                (memory or {}).get("player_names") or []
            )
            logger.info(
                "LILY_MEMORY | BLOCK_READY | group=%s chars=%d "
                "total_games=%s (post-upgrade)",
                new_group_id, len(block),
                (memory or {}).get("total_games"),
            )
        # Memory at the door (F): the upgrade's reload is the answer the
        # greeting may be waiting on (the live race: participant metadata
        # landed AFTER the entrypoint's initial load gave up).
        self.memory_settled.set()
        # Task 1: recognition that arrives AFTER the door becomes a
        # recovery moment, not a silent nothing.
        self.maybe_fire_late_recognition()
        self.fire_enrollment("group_id_upgrade")

    async def resolve_group_identity(self, trigger: str) -> None:
        """Re-resolve the group id once the roster has stabilized (game
        start). Only runs when the current id is weak (room-random or a
        prior name-set hash). Order: (b) stored-voiceprint identifier match
        -> (c) normalized sorted player-name-set hash -> keep current."""
        # WO-LILY-FORGETME-001: post-deletion the session stays on its
        # fresh anonymous id — re-resolving would re-run the name-set hash
        # into a new memory-keyed group and silently rebuild the identity
        # the table just deleted.
        if self.forget_state in ("executing", "done", "failed"):
            logger.info(
                "LILY_FORGET | GROUP_RESOLVE_SUPPRESSED | session=%s "
                "trigger=%s (post-forget anonymous binding)",
                self.sk.session_id, trigger,
            )
            return
        if getattr(self, "device_candidate_group_id", None):
            verified = await self.verify_device_candidate(trigger)
            if verified is True:
                return
            if verified is None:
                # Voice evidence is not ready; keep the candidate quarantined
                # rather than falling through to weaker name-only memory.
                return
        if self.group_id_source in _STRONG_GROUP_SOURCES:
            return
        names = list(self.sk.players.keys())
        if not names:
            logger.info(
                "LILY_MEMORY | GROUP_ID_RESOLVE | trigger=%s no players bound "
                "— keeping %s", trigger, self.group_id,
            )
            return
        new_id, source = None, None
        # (b) voiceprint identifier match against prior groups
        try:
            get_ids = getattr(self.stt, "get_speaker_ids", None)
            if get_ids is not None and self.supabase is not None:
                current = await asyncio.wait_for(get_ids(), timeout=3.0)
                if current:
                    stored = await lily_persistence.lily_load_voiceprints_by_players(
                        self.supabase, names
                    )
                    matched = lily_memory.lily_match_group_by_voiceprints(
                        current, stored
                    )
                    if matched and matched != self.sk.session_id:
                        new_id, source = matched, "voiceprint_match"
        except Exception as e:
            logger.warning(
                "LILY_MEMORY | GROUP_ID_RESOLVE | voiceprint match failed: %s", e
            )
        # (c) name-set hash fallback — deterministic across sessions
        if new_id is None:
            hashed = lily_memory.lily_name_set_group_id(names)
            if hashed:
                new_id, source = hashed, "name_set_hash"
        if new_id is None or new_id == self.group_id:
            return
        await self.upgrade_group_id(new_id, source)

    # -- the right to be forgotten (WO-LILY-FORGETME-001) ------------------------

    def identity_persistence_allowed(self) -> bool:
        """Whether recognition/report data may still be written this session."""
        return self.forget_state not in ("executing", "done", "failed")

    def _on_forget_requested(self, requester_key: str | None) -> None:
        """Spoken deletion request (deterministic scorekeeper detection —
        "forget me"/"forget us"/paraphrases): arm the pending-confirm
        state and dispatch ONE plain confirmation naming the scope. Never
        asks twice: a pending flow ignores re-requests, a completed
        deletion answers plainly, and a declined flow only re-arms on THIS
        path — a fresh player-initiated request, never Lily re-raising."""
        if self.forget_state in ("pending_confirm", "executing"):
            return  # never ask twice / cascade already running
        if self.forget_state == "done":
            self.gated_say(
                None,
                "forget_already_done",
                "They asked you to forget them, but the deletion already "
                "ran this session — everything is gone and the night is "
                "running on a clean anonymous slate. Say exactly that in "
                "one plain warm line and keep the game moving.",
                source="voice_command",
            )
            return
        self.forget_state = "pending_confirm"
        self.forget_spoken_confirmed = False
        self.forget_requester = requester_key
        logger.info(
            "LILY_FORGET | REQUESTED | session=%s group=%s requester=%s",
            self.sk.session_id, self.group_id, requester_key,
        )
        self.gated_say(
            None,
            "forget_confirm",
            "A player just asked you to forget them. Ask for ONE plain "
            "spoken confirmation, naming the full scope: everything you "
            "keep for this table — voices as you know them, games, facts "
            "— gone for good, and tonight's game keeps going. Something "
            "like: 'Happy to. That wipes everything I keep for this "
            "table — your voices as I know them, your games, your facts "
            "— gone for good. Tonight's game keeps going. Say yes and "
            "it's done.' The scope is always what YOU keep — you speak "
            "for your own memory, never for other systems. One question "
            "only — never ask twice, never argue for being remembered.",
            source="voice_command",
        )

    async def _forget_confirmed(self, source: str) -> None:
        """The deterministic yes landed: run the cascade, then speak the
        outcome — the SAME message shape the tool returns, so honest
        partial-failure reporting is identical on both paths."""
        result = await self.execute_forget(source=source)
        self.gated_say(
            None,
            "forget_done",
            lily_forget.lily_forget_result_message(result),
            source=source,
        )

    async def execute_forget(self, source: str) -> dict:
        """Task 1: the delete cascade + in-session teardown. Awaited and
        verified — NEVER fire-and-forget: the acknowledgment only goes out
        after the deletes completed and count-queries confirmed zero rows
        under the old identity (capped ~10s in lily_persistence; partial
        failure is reported honestly and stays retryable). Idempotent:
        done -> already_done, executing -> in_progress."""
        if self.forget_state == "done":
            return {"ok": True, "already_done": True}
        if self.forget_state == "executing":
            return {"ok": False, "in_progress": True}
        self.forget_state = "executing"
        # The cascade targets the ORIGINAL identity, captured once — the
        # teardown below re-binds the session to a fresh anonymous id, so
        # a retry after partial failure must not target the fresh id.
        first_attempt = self._forget_target_group is None
        target = (
            self._forget_target_group
            or getattr(self, "device_candidate_group_id", None)
            or self.group_id
        )
        self._forget_target_group = target
        logger.info(
            "LILY_FORGET | EXECUTE | session=%s group=%s source=%s retry=%s",
            self.sk.session_id, target, source, not first_attempt,
        )
        if first_attempt:
            discard = getattr(
                getattr(self, "transcripts", None), "discard_pending", None
            )
            if discard is not None:
                await discard(disable=True)
        if self.supabase is None:
            # Nothing was ever persisted (offline/dev session) — the
            # in-session teardown still runs so recognition state clears.
            result: dict = {
                "ok": True, "deleted": {}, "rekeyed": {},
                "skipped": ["all tables (no supabase client — nothing persisted)"],
                "failed": {}, "verified": [],
            }
        else:
            result = await lily_persistence.lily_forget_group_data(
                self.supabase, target, self.sk.session_id
            )
        if first_attempt:
            self._teardown_group_identity()
        if result.get("ok"):
            self.forget_state = "done"
            # Emitted AFTER the cascade succeeded, never before: the
            # frontend clears the device localStorage group id and shows a
            # transient confirmation on this packet.
            await self.send_event("memory_forgotten", {"scope": "all"})
        else:
            # Retryable: a fresh spoken request or lily_forget_group
            # (confirm=true) re-runs the cascade against the ORIGINAL id.
            self.forget_state = "failed"
        return result

    def _teardown_group_identity(self) -> None:
        """In-session teardown after the cascade ran: the game continues
        under a FRESH ANONYMOUS binding (WO: "the current game continues
        under fresh anonymous binding") — a new random id, not the device
        metadata one. Memory/fact/voiceprint WRITES continue under the
        fresh id (record_group_fact, fire_enrollment, and the session-close
        memory write all read self.group_id live), but the deleted identity
        is unreachable: the device id is dead (frontend cleared it on
        memory_forgotten) and resolve_group_identity / upgrade_group_id are
        suppressed for the rest of the session, so the name-set hash can
        never silently rebuild the deleted group."""
        old = self.group_id
        fresh = "anon_" + uuid.uuid4().hex[:16]
        self.group_id = fresh
        self.group_id_source = "post_forget_anonymous"
        # [RETURNING TABLE] injection stops immediately: the block is
        # cleared here and _apply_context_blocks REMOVES the stale system
        # item on the next turn (symmetric removal path).
        self.memory_block = ""
        self.memory_total_games = 0
        self.memory_player_names = []
        self.device_candidate_group_id = None
        self.device_candidate_source = None
        self.device_identity_rejected = True
        self._device_candidate_memory = None
        self._device_candidate_memory_block = ""
        self._device_candidate_prefs = {}
        self._device_candidate_voiceprints = []
        self.sk.transcript_buffer = []
        self.highlights = []
        # Group prefs WO interlock: the stored preferences were deleted by
        # the cascade (lily_group_prefs) — clear the in-session dict too,
        # so nothing re-persists the deleted 'usual' under the fresh id.
        # The LIVE pacing flag stays: tonight's tempo is tonight's choice,
        # not identity; if the table picks a pacing again later it writes
        # fresh under the anonymous id like the other post-forget writes.
        self.prefs = {}
        # STT: clear the enrolled speakers so no future STT stream this
        # session re-injects the deleted voiceprints. 1.6.6 NOTE:
        # livekit-plugins-speechmatics 1.6.6 has NO live de-enrollment
        # path — update_speakers() only takes focus/ignore/focus_mode, and
        # known_speakers ride the one-shot StartRecognition message.
        # Clearing _stt_options.known_speakers guarantees any STT
        # websocket reconnect starts with zero enrolled voices; the
        # already-open stream keeps its labels until it closes (documented
        # limitation) — but the stored identifiers behind them are gone
        # and nothing new is written under the deleted identity.
        if self.stt is not None:
            try:
                opts = getattr(self.stt, "_stt_options", None)
                if opts is not None:
                    opts.known_speakers = []
            except Exception as e:
                logger.warning("LILY_FORGET | STT_TEARDOWN | failed: %s", e)
        # The CURRENT session row follows the live game onto the fresh
        # anonymous id (the cascade tombstoned it with the rest of the
        # group's history; tonight's operational row is the one exception
        # — the game it describes is still running, anonymously).
        if self.supabase is not None:
            supabase = self.supabase
            session_id = self.sk.session_id

            async def _rekey_current() -> None:
                try:
                    await asyncio.to_thread(
                        lambda: supabase.table("lily_sessions")
                        .update({"group_id": fresh})
                        .eq("session_id", session_id)
                        .execute()
                    )
                except Exception as e:
                    logger.warning(
                        "LILY_FORGET | SESSION_REKEY | failed: %s", e
                    )

            asyncio.ensure_future(_rekey_current())
        logger.info(
            "LILY_FORGET | TEARDOWN | session=%s old_group=%s fresh_group=%s "
            "(memory block cleared, known_speakers cleared, resolve/upgrade "
            "suppressed, writes continue under the fresh id)",
            self.sk.session_id, old, fresh,
        )

    # -- game lifecycle ---------------------------------------------------------

    async def start_game(self, source: str) -> None:
        if self.game_started:
            return
        self.game_started = True
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
        await self.publish_attributes()
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
            instructions += (
                " The [RETURNING TABLE] context shows this table has "
                "played with you before — one quick welcome-back beat "
                "if you haven't done one yet, then into the game."
                # Task 4 disclosure ride-along: covers memory that resolved
                # AFTER the greeting (mid-lobby group-id upgrade) — the
                # once-per-session latch inside makes this a no-op when the
                # greeting already carried it.
                + self.memory_disclosure_instruction()
                # Group prefs ask-once ride-along, same latch pattern: if
                # the greeting never met stored prefs (they resolved with a
                # mid-lobby upgrade), this is the last natural moment to
                # offer "the usual, or change anything?" — already-applied
                # flags make a "the usual" answer a pure no-op.
                + self.prefs_offer_instruction()
            )
        # Structural delivery claim (desync WO Sub-agent B): when question
        # one is already armed, the kickoff turn IS its delivery.
        if self.armed_question is not None:
            self.expect_delivery()
        self.gated_say(
            None, "game_start", instructions, source=f"start_{source}"
        )

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
        if self.supabase is not None and self.identity_persistence_allowed():
            asyncio.ensure_future(lily_persistence.lily_checkpoint(
                self.supabase, self.sk, final_standings=standings,
            ))
            # Session memory — idempotent (session_id upsert), so the
            # shutdown callback writing again is safe.
            asyncio.ensure_future(lily_memory.lily_write_session_memory(
                self.supabase, self.group_id, self.sk.session_id,
                standings, self.sk.question_number, self.highlights,
                round_reached=self.sk.round,
            ))

    # -- session report (B3) --------------------------------------------------------

    def build_game_stats(self, standings: list[dict]) -> dict:
        """game_stats jsonb for lily_session_reports: final standings,
        rounds/questions played, per-player answers attempted/correct,
        mode changes, callouts, duration."""
        per_player = {
            name: {
                "answers_attempted": s.get("answers_attempted", 0),
                "answers_correct": s.get("answers_correct", 0),
            }
            for name, s in self.sk.players.items()
        }
        return {
            "final_standings": standings,
            "rounds_played": self.sk.round,
            "questions_played": self.sk.question_number,
            "per_player": per_player,
            "mode_changes": list(self.sk.mode_changes),
            "callouts": list(self.highlights),
            "duration_s": round(time.time() - self.session_started_at, 1),
        }

    # -- state block --------------------------------------------------------------

    def build_state_block(self) -> str:
        block = self.sk.build_state_block()
        extra = []
        # Room-temperature read (WO-LILY-AUDEERING-001 Task 3): NL descriptor
        # lines only — zero scalars; neutral room injects NOTHING. The [env:]
        # line appears at most once per refresh. Synchronous read — never
        # blocks the turn.
        try:
            extra.extend(self.acoustic.state_block_lines())
        except Exception:
            pass  # acoustic read is enrichment; never breaks the state block
        if lily_config.architect_mode():
            extra.append(
                "architect mode: server-authenticated override ACTIVE — "
                "operator testing may bypass adult age/signal vetoes"
            )
        # Self-knowledge Task 3, availability layer: capability vs what's
        # switched on TONIGHT. Only gated features that are OFF inject —
        # so "picture rounds are one of mine, but they're not switched on
        # tonight" is grounded, never the fixtures' present-tense
        # overclaim. None = gates unknown (entrypoint hasn't set them);
        # inject nothing rather than guess.
        if self.availability_flags is not None:
            gated_off = lily_capabilities.lily_availability_lines(
                self.availability_flags
            )
            if gated_off:
                extra.append(
                    "tonight's availability (capability vs switched-on — "
                    "name the difference honestly if asked): "
                    + "; ".join(gated_off)
                )
        # SAID-ALREADY ledger (RECOGNITION-VARIETY Task 3a): what she has
        # already delivered this session. The prompt law forbids re-serving
        # any of it unprompted — repetition was named live by the player
        # ("I know", the mock-echoed "Fantastic.").
        said = self.sk.said_already_lines()
        if said:
            extra.append(
                "SAID-ALREADY (re-deliver NOTHING on this ledger unless a "
                "player asks; mint fresh words instead): " + " || ".join(said)
            )
        if getattr(self, "device_candidate_group_id", None):
            extra.append(
                "device memory candidate: UNVERIFIED — the device looks "
                "familiar, but disclose no returning-table details until "
                "a current voiceprint verifies"
            )
        if self.armed_question is not None and not self.sk.answer_window_open:
            # NEED-TO-KNOW (say-gate WO): the ambient context carries the
            # PROMPT TEXT (+category/format) only — never canonical_answer,
            # acceptable_answers, reveal_color, or the full question JSON.
            # The reveal turn receives the answer via its instructed reply
            # at reveal time; the Tier-2 judge gets it in its dedicated
            # call. The vocal node cannot leak what it does not hold.
            q = self.armed_question
            need_to_know = {
                "prompt": q.get("prompt", ""),
                "category": q.get("category", ""),
            }
            if q.get("choices"):
                # Multiple-choice format: the choices are spoken content,
                # not answer material — they ride along.
                need_to_know["choices"] = q["choices"]
            if q.get("image_url"):
                # Picture question: the vocal node needs the FLAG only —
                # the URL is screen transport (published at delivery),
                # never speech material.
                need_to_know["image"] = (
                    "picture question — the image lands on the screen as "
                    "you ask; point the table at the screen"
                )
            extra.append(
                "NEXT QUESTION (perform it when the table is ready, "
                "faithfully): " + json.dumps(need_to_know, ensure_ascii=False)
            )
        elif self.next_question is not None:
            extra.append("next question: prefetched and ready")
        elif self.game_started and not self.game_over:
            extra.append(
                "next question: NOT ready yet — do not claim it is; "
                "banter until it lands"
            )
        if self._pending_unbound_award is not None:
            extra.append(
                "an unbound voice "
                f"({self._pending_unbound_award['speaker_label']}) has a "
                "point waiting — get their name and bind them"
            )
        # Honesty assist (desync WO Sub-agent C): the grounded truth for a
        # player's state callout — context only, never speech (the leak
        # filter drops the line if it ever echoes outbound). getattr: test
        # harnesses build LilyGame via __new__.
        state_note = getattr(self, "_state_note", None)
        if state_note:
            extra.append(state_note)
        if not self.game_started:
            extra.append(
                "game not started: you are in the lobby — bind names, fish "
                "for lobby facts, start on the first genuine group laugh"
            )
            if self.promoted_categories:
                # Gated category proposals (F): PROMOTED extras only —
                # unpromoted candidates are never announced.
                extra.append(
                    "extra categories in tonight's rotation (promoted by "
                    "player demand — you may mention these): "
                    + ", ".join(self.promoted_categories)
                )
        if extra:
            block += "\n" + "\n".join(extra)
        return block

    # -- burn protocol (say-gate WO §1) ------------------------------------------

    def _burn_question(self, question: dict, reason: str) -> None:
        """Mark one question burned: LILY_BURN log, status='burned' for
        bank rows (kb_ ids — generated questions have no DB row and are
        simply discarded), and the prompt joins used_prompts so the
        generator never re-produces it this session. Scope is GLOBAL
        today (migration 009 status column); per-group burn rides
        lily_asked_history later."""
        qid = question.get("id")
        logger.warning(
            "LILY_BURN | question_id=%s | session=%s | reason=%s",
            qid, self.sk.session_id, reason,
        )
        prompt = question.get("prompt", "")
        if prompt and prompt not in self.used_prompts:
            self.used_prompts.append(prompt)
        if self.supabase is not None:
            asyncio.ensure_future(
                lily_persistence.lily_burn_question(self.supabase, qid)
            )

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
        note = ""
        pending = self._pending_unbound_award
        if pending and pending["speaker_label"] == speaker_label:
            self._pending_unbound_award = None
            self.sk.record_result(player_name, correct=True, points=pending["points"])
            note = f" Their held point ({pending['points']}) is now committed."
        # NOTE (supersedes the spec's dynamic max_speakers idea): the 1.6.6
        # Speechmatics plugin has NO in-flight update path for max_speakers
        # (only update_speakers(focus/ignore/focus_mode)). The cap is set at
        # construction to product max (6 players + 1). 1.6.6 does add
        # Agent.update_options(stt=...) — a full live STT swap (fresh
        # StartRecognition) that COULD carry a new max_speakers; not wired
        # here (ghost-speaker WS decision).
        logger.info(
            "LILY_STT | roster=%d (max_speakers fixed at construction)",
            self.sk.roster_size(),
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

    # -- auto-start safety net --------------------------------------------------------

    def _maybe_auto_start_after_lobby(self) -> None:
        """Kick off round one when the lobby has clearly settled but no
        one — neither Lily via lily_begin_round nor the UI via
        lily_control.start — has actually started the game. Guards ensure
        we never start while there is only one voice, before the first
        question has been prefetched, or before the lobby grace period."""
        if self.game_started or self.game_over:
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
        logger.info(
            "LILY_STATE | AUTO_START | session=%s roster=%d elapsed=%.1fs",
            self.sk.session_id, self.sk.roster_size(),
            time.time() - self.session_started_at,
        )
        asyncio.ensure_future(self.start_game(source="auto_after_lobby"))


# ---------------------------------------------------------------------------
# The agent
# ---------------------------------------------------------------------------

class LilyAgent(Agent):
    def __init__(self, game: LilyGame, **kwargs) -> None:
        self._game = game
        self._empty_retry_pending = False
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
        if not lily_is_valid_name(name):
            # Fragmented-STT fallback: extract from the speaker's 2-second
            # accumulated fragment window.
            extracted = lily_extract_name_from_fragments(
                self._game.fragments, label
            )
            if extracted:
                name = extracted
            else:
                return (
                    f"Could not bind {label}: {player_name!r} does not look "
                    "like a name. Ask again naturally."
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
        self._game.sk.bind_speaker(label, name)
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
        name = (player_name or "").strip()
        if name not in self._game.sk.players:
            return f"No rostered player named {name!r} — clarify not logged."
        self._game.mark_pending_clarify(name)
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
        # SAFETY GATE (WO-DESYNC-A, RESTORED 2026-07-16 — a concurrent
        # rewrite dropped it): the child-signal sensor and the adult deck
        # deploy as ONE UNIT. Sensor down — missing AUDEERING_API_KEY,
        # failed preflight, breaker OPEN — means deck down, fail CLOSED.
        # NOTHING overrides this check: architect mode may stand in for
        # the spoken age ceremony during controlled testing, but it never
        # substitutes for the sensor, and consensus stays necessary,
        # never sufficient.
        if not lily_audeering_client.lily_child_gate_ready():
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
        if not architect and not confirmed_all_18_plus:
            logger.warning(
                "LILY_ADULT_GATE | ADULT_MODE_DECLINED | "
                "reason=age_confirmation_required | session=%s",
                self._game.sk.session_id,
            )
            return (
                "Adult mode is NOT enabled yet. Ask every player directly: "
                "'Please confirm that you are 18 or older and want the "
                "grown-up deck.' Call this tool again with "
                "confirmed_all_18_plus=true only after every player gives "
                "an explicit verbal yes."
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
        # D: no question survives the deck change — the leftover general
        # question is flushed (the live "powerhouse of the cell" defect)
        # and the adult deck starts drawing immediately.
        self._game.flush_for_mode_switch(source="enter_adult")
        await self._game.publish_attributes()
        return (
            "Adult mode is ON (sticky"
            + (", architect override" if architect else ", 18+ confirmed")
            + "). The layer is active; same house "
            "rules. The deck switched with it: any earlier question is "
            "flushed, and the first adult question is being drawn now — "
            "it lands in the state block in a beat. NEVER serve a "
            "leftover general question as adult material and NEVER "
            "freestyle one; vamp for a beat until the state block shows "
            "the adult question, then ask it word for word."
        )

    @function_tool()
    async def lily_begin_round(self, context: RunContext) -> str:
        """Kick off round one and open the tiered question loop. Call this
        the moment the lobby has real energy — a genuine group laugh, or a
        clear "let's play" from the table. Once called, the state block
        starts serving [NEXT QUESTION] and the answer window opens on your
        first ask. No-op if the game is already running."""
        if self._game.game_started:
            return "Already running — the next question is in the state block."
        await self._game.start_game(source="host_tool")
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
            # q_{N}_delivery at dispatch regardless of phrasing.
            self._game.expect_delivery()
            return (
                "Round one is armed and YOU deliver the first question in "
                "this very turn — you are its sole deliverer. One short "
                "transition beat (set the round-one category: "
                f"{q.get('category', 'general')}), then ask exactly, word "
                f"for word: {q.get('prompt', '')!r} Never re-ask it in a "
                "later turn."
            )
        return (
            "Round one is armed but the first question hasn't landed yet — "
            "banter for a beat; deliver it when it appears in the state "
            "block."
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
                "Bonus points can only be awarded once a round is underway. "
                "Call lily_begin_round first or wait for auto-start."
            )
        name = (player_name or "").strip()
        if name not in self._game.sk.players:
            return f"No rostered player named {name!r} — no point awarded."
        self._game.sk.award_bonus(name)
        clean_reason = (reason or "").strip()[:200] or None
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
        await self._game.publish_attributes()
        return f"Bonus point to {name}."

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
    async def lily_show_demo_picture(self, context: RunContext) -> str:
        """Put ONE real demo image on the screen right now — the lobby
        included. Call this whenever someone asks to SEE what picture
        rounds look like ("show me", "prove it", "I'll believe it when I
        see it") instead of describing pictures in words. The tool result
        is the ONLY thing that makes "look at the screen" true: if it
        confirms the image landed, point the table at the screen; if it
        reports a failure, say exactly that — never claim anything
        reached the screen on your own.
        """
        landed, line = await self._game.show_demo_picture()
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

    def _apply_context_blocks(self, chat_ctx) -> None:
        """Idempotent, deterministic injection of the three system blocks.
        Exact injection semantics preserved from the llm_node era: adult
        layer added/removed on the sticky mode flag, memory block once,
        state block replace-then-append — all keyed on the same dedupe
        markers (_ADULT_LAYER_MARKER, MEMORY_BLOCK_MARKER,
        _STATE_BLOCK_MARKER)."""
        items = _chat_items(chat_ctx)

        # Adult layer: additive injection/removal keyed on the sticky flag.
        adult_idx = next(
            (
                i for i, m in enumerate(items)
                if getattr(m, "role", None) == "system"
                and _ADULT_LAYER_MARKER in _message_text(m)
            ),
            None,
        )
        if self._game.sk.mode == "adult" and adult_idx is None:
            items.insert(
                0,
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
        memory_idx = next(
            (
                i for i, m in enumerate(items)
                if getattr(m, "role", None) == "system"
                and lily_memory.MEMORY_BLOCK_MARKER in _message_text(m)
            ),
            None,
        )
        if self._game.memory_block and memory_idx is None:
            items.insert(
                0,
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
        state = lily_say_gate.lily_wrap_state_block(
            self._game.build_state_block()
        )
        existing = [
            i for i, m in enumerate(items)
            if getattr(m, "role", None) == "system"
            and _STATE_BLOCK_MARKER in _message_text(m)
        ]
        if len(existing) == 1 and _message_text(items[existing[0]]) == state:
            return
        for i in reversed(existing):
            items.pop(i)
        items.append(
            ChatMessage(id=self._CTX_ID_STATE, role="system", content=[state])
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
        try:
            self._apply_context_blocks(turn_ctx)   # this turn sees FINAL context
            self._apply_context_blocks(self._chat_ctx)  # next preemptive snapshot too
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
        self._apply_context_blocks(chat_ctx)
        self._game.publish_attributes_nowait()

        async for chunk in Agent.default.llm_node(
            self, chat_ctx, tools, model_settings
        ):
            yield chunk

    async def tts_node(self, text, model_settings):
        chunks = []
        async for chunk in text:
            chunks.append(chunk)
        raw = "".join(chunks).strip()

        # Say-gate leak filter (BEFORE hygiene): injected state-block
        # context echoed into the outbound turn — the sentinel envelope,
        # envelope fragments, or bracketed metadata lines ([GAME STATE],
        # [room read:, [env:, [RETURNING TABLE]) — is deterministically
        # stripped and triggers the burn protocol for any armed/prefetched
        # question (its answer may have gone out on air).
        filtered, leak_reasons = lily_say_gate.lily_filter_leaks(raw)
        if leak_reasons:
            logger.warning(
                "LILY_SAY_SUPPRESSED | reason=leak | markers=%s",
                ",".join(sorted(set(leak_reasons))),
            )
            # Burn protocol: only for leaks that could carry answer
            # material. The honesty state note (desync WO Sub-agent C)
            # holds committed scores, never answers — it is stripped
            # above, and burning a live question over it would punish the
            # table for calling out the board.
            if any(
                r != "metadata:[state note:" for r in leak_reasons
            ):
                self._game.on_answer_leak()

        # P4 spoken-markdown strip: deterministic hygiene via the say gate
        # (lily_say_gate — THE choke point for outbound speech hygiene)
        # BEFORE the punctuation-flush guard. Markdown emphasis, headers,
        # bullets and emoji are removed; [bracket] audio tags ([excited],
        # [whispering], [pause], ...) are load-bearing ElevenLabs v3
        # controls and are preserved verbatim. Emoji-only turns strip to
        # "" and fall into the empty-candidate retry below.
        full = lily_say_gate.lily_clean_for_speech(filtered)
        if full != raw:
            logger.info(
                "LILY_SAY_GATE | stripped %d chars of markdown/emoji/leaks",
                len(raw) - len(full),
            )

        # Mirror lint (self-knowledge WO Task 2a) — LOG-ONLY: the ban is
        # prompt-enforced; this makes drift measurable in telemetry.
        # Never mutates or suppresses the turn.
        mirror_pattern = lily_say_gate.lily_mirror_flag(full)
        if mirror_pattern:
            logger.info(
                "LILY_SAY | MIRROR_FLAG | session=%s pattern=%r",
                self._game.sk.session_id, mirror_pattern,
            )
        # Repetition lint (RECOGNITION-VARIETY Task 3b) — LOG-ONLY, against
        # turns that actually PLAYED (sk.agent_turns records at playout, so
        # a dispatched-then-swallowed turn never counts as said).
        repeat_kind = lily_say_gate.lily_repeat_flag(
            full, self._game.sk.agent_turns
        )
        if repeat_kind:
            logger.info(
                "LILY_SAY | REPEAT_FLAG | session=%s kind=%s",
                self._game.sk.session_id, repeat_kind,
            )

        if len(full) < 3:
            # §11.1: an empty candidate (safety-filter mute, truncation) is a
            # loggable event with a retry — never silence.
            #
            # Say gate (19:27:52 swallowed-delivery fix): this speech was
            # dispatched but will never play — any speech-act claims made
            # for it must NOT stay claimed, or the retry below regenerates
            # into a gate that suppresses the redelivery as a "duplicate".
            # Claims are claim-at-dispatch / confirm-at-playout /
            # release-on-failure: release the pending ones here and relog,
            # so the retry can legitimately redeliver the act.
            speech_id = _current_speech_id()
            released = (
                self._game.say_registry.release_owner(speech_id)
                if speech_id
                else self._game.say_registry.release_pending()
            )
            for k in released:
                logger.warning(
                    "LILY_SAY | RELEASED | key=%s | reason=empty_candidate "
                    "— retry may redeliver", k,
                )
            # Structural delivery retry (desync WO Sub-agent B): a released
            # q_{N}_delivery claim re-arms the one-shot delivery flag so
            # the retry turn re-registers the delivery at its dispatch.
            if (
                f"q_{self._game.sk.question_number}_delivery" in released
            ):
                self._game.expect_delivery()
            if not self._empty_retry_pending:
                self._empty_retry_pending = True
                logger.warning(
                    "LILY_EMPTY_CANDIDATE | empty/junk response (%r) — retrying",
                    full,
                )
                asyncio.ensure_future(self.session.generate_reply())
            else:
                self._empty_retry_pending = False
                logger.error(
                    "LILY_EMPTY_CANDIDATE | second consecutive empty response "
                    "— giving the turn back to the room"
                )
            yield rtc.AudioFrame(
                data=b"\x00\x00" * 2400,
                sample_rate=24000,
                num_channels=1,
                samples_per_channel=2400,
            )
            return

        self._empty_retry_pending = False

        # STRUCTURAL delivery registration (desync WO Sub-agent B) — the
        # q_{N}_delivery CLAIM is the delivery event; window-open and
        # "delivered" marking key off it, never off text similarity. This
        # turn claims when (a) code dispatched it to deliver the armed
        # question (the one-shot pending-delivery flag: begin_round
        # post-tool turn, question nudges, skip/game-start follow-ups) —
        # regardless of phrasing; or (b) it organically performs the
        # question's core answer-bearing sentence as written
        # (lily_turn_presents_question). BUG-2 duplicate suppression
        # stands: a turn that textually re-performs an already-claimed
        # question is made physically silent (no retry: suppressed, not
        # swallowed) — but a turn that merely banters after the delivery
        # registered speaks normally. Decision + claim + screen publish
        # live in LilyGame.register_delivery_claim (offline-tested).
        speech_id = _current_speech_id()
        delivery = self._game.register_delivery_claim(
            full, speech_id=speech_id
        )
        if delivery == "rewrite_strict":
            full = self._game.rendered_armed_question()
            self._game.expect_delivery(strict=True)
            delivery = self._game.register_delivery_claim(
                full, speech_id=speech_id
            )
            if delivery not in (
                "claimed_structural",
                "claimed_core_sentence",
            ):
                logger.error(
                    "LILY_DELIVERY | STRICT_REWRITE_FAILED | session=%s q=%d",
                    self._game.sk.session_id,
                    self._game.sk.question_number,
                )
        if delivery == "duplicate":
            if speech_id:
                suppressed_ids = getattr(
                    self._game, "_suppressed_speech_ids", None
                )
                if suppressed_ids is None:
                    self._game._suppressed_speech_ids = set()
                    suppressed_ids = self._game._suppressed_speech_ids
                suppressed_ids.add(speech_id)
            yield rtc.AudioFrame(
                data=b"\x00\x00" * 2400,
                sample_rate=24000,
                num_channels=1,
                samples_per_channel=2400,
            )
            return

        # MANDATORY punctuation-flush guard (Lovebirds fix): LilyTTS is
        # streaming=False, so the framework wraps it in StreamAdapter gated
        # by blingfire sentence tokenization. Lily's suspense holds produce
        # exactly the short unpunctuated fragments that deadlock the
        # SegmentSynchronizer (text_done=false / audio_done=true). Append a
        # terminal period so the tokenizer always flushes. (The node
        # accumulated every chunk above, so replaying the cleaned text as
        # one chunk is equivalent — the sentence tokenizer re-splits it.)
        if full[-1] not in ".!?":
            full += "."

        async def _replay():
            yield full

        async for frame in Agent.default.tts_node(self, _replay(), model_settings):
            yield frame


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

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
    try:
        job_meta = getattr(getattr(ctx, "job", None), "metadata", None)
        candidate = lily_memory.lily_parse_group_id_from_metadata(job_meta)
        if candidate:
            return candidate, "dispatch_metadata"
        if job_meta:
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
                    logger.info(
                        "LILY_MEMORY | GROUP_ID | participant %s metadata "
                        "present but unparseable: %r",
                        getattr(participant, "identity", "?"), str(meta)[:120],
                    )
        except Exception as e:
            logger.warning("LILY_MEMORY | GROUP_ID | participant scan failed: %s", e)
        return None, non_agents

    deadline = time.time() + PARTICIPANT_METADATA_WAIT_SECONDS
    while True:
        candidate, non_agents = _scan()
        if candidate:
            return candidate, "participant_metadata"
        if non_agents > 0:
            # A human is here with no usable metadata — waiting won't help
            # (token metadata is fixed at join). Late joiners WITH metadata
            # are handled by the participant_connected upgrade hook.
            logger.info(
                "LILY_MEMORY | GROUP_ID | %d participant(s) present, no "
                "lily_group_id metadata", non_agents,
            )
            break
        if time.time() >= deadline:
            logger.info(
                "LILY_MEMORY | GROUP_ID | no non-agent participant within "
                "%.1fs of connect", PARTICIPANT_METADATA_WAIT_SECONDS,
            )
            break
        await asyncio.sleep(0.25)

    if lily_config.group_id_override():
        return lily_config.group_id_override(), "env_override"
    return room_name, "room_name"


async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    room_name = ctx.room.name or "unknown"
    _setup_session_log(room_name)

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
            if game.group_id_source in _STRONG_GROUP_SOURCES:
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
        if group_id_source in _STRONG_GROUP_SOURCES or game.memory_block:
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

    # --- STT: Speechmatics multi-speaker fleet profile (Part II §1.1) ---
    # NOTE: livekit-plugins-speechmatics 1.6.6 does not expose a `model=`
    # kwarg — `operating_point` is the only path to select ENHANCED, and the
    # SDK-level deprecation warning about `TranscriptionConfig.operating_point`
    # is emitted from inside the plugin wrapper. Fix requires an upstream
    # plugin bump; no fleet member has migrated yet. Keep as-is.
    stt = SpeechmaticsSTT(
        language="en",
        operating_point=OperatingPoint.ENHANCED,
        enable_diarization=True,
        speaker_active_format="[{speaker_id}] {text}",
        speaker_sensitivity=0.5,
        prefer_current_speaker=True,  # [VERIFY live] rapid answer collisions
        # No in-flight max_speakers update exists at 1.6.6 — fixed at
        # construction to product cap (2-6 players) + 1. Applies only to
        # generic speakers on top of enrolled ones.
        max_speakers=7,
        max_delay=1.5,
        end_of_utterance_silence_trigger=0.8,
        turn_detection_mode=TurnDetectionMode.FIXED,
        include_partials=True,
        ignore_speakers=["__ASSISTANT__"],  # echo guard, fleet standard
        additional_vocab=[
            AdditionalVocabEntry(content="Lily"),
        ],
        known_speakers=known_speakers,
    )
    game.stt = stt
    if known_speakers:
        logger.info("VOICEPRINT | injected %d known speakers", len(known_speakers))

    # --- Session: vocal node gemini-3.5-flash, explicit safety settings ---
    session = AgentSession(
        userdata={"scorekeeper": scorekeeper, "game": game},
        stt=stt,
        llm=GoogleLLM(
            model=lily_config.vocal_model(),
            # Default sampling — never set temperature/top_p/top_k on 3.x.
            thinking_config={"thinking_level": "low"},
            max_output_tokens=lily_config.vocal_max_output_tokens(),
            # §11.1 CRITICAL: without these, adult mode silently dies
            # (empty candidate, no error).
            safety_settings=[
                {"category": "HARM_CATEGORY_HARASSMENT",        "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_HATE_SPEECH",       "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
            ],
            api_key=lily_config.google_api_key(),
        ),
        tts=LilyTTS(),  # voice1 (primary) via lily_config.lily_voice_id()
        vad=silero.VAD.load(),  # barge-in enabled; no STT gating during TTS
        turn_handling=TurnHandlingOptions(
            interruption=InterruptionOptions(
                min_words=1,
                min_duration=0.8,
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
    }
    _METRICS_CAP = 500

    @session.on("conversation_item_added")
    def _on_item_added(ev) -> None:
        msg = ev.item
        role = getattr(msg, "role", None)
        if role == "assistant":
            game._last_assistant_text = _message_text(msg)
            m = getattr(msg, "metrics", None) or {}
            get = (lambda k: m.get(k)) if isinstance(m, dict) else (
                lambda k: getattr(m, k, None)
            )
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
        # Event arrival wall-clock (created_at) plus recovered STT
        # stream-relative timings from the n-best collector feed the
        # timestamp reconciler for "first answered first" ordering under
        # jitter.
        created = getattr(ev, "created_at", None)
        arrival_ts = (
            created.timestamp() if hasattr(created, "timestamp") else time.time()
        )
        game.fragments.add(speaker_label or "UU", text)
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
            game.buffer_pre_window_answer({
                "text": text,
                "speaker_label": speaker_label,
                "segment_start_time": seg_start_ts,
                "segment_end_time": seg_end_ts,
                "diarization_confidence": diarization_confidence,
                "acoustic_confidence": acoustic_confidence,
                "timestamp_source": timing.get("source"),
                "timing_drift_seconds": timing.get("drift_seconds"),
                "addressee_confidence": fused_conf,
            })
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
            spoken = ""
            try:
                for item in handle.chat_items:
                    if getattr(item, "role", None) == "assistant":
                        spoken += " " + _message_text(item)
            except Exception:
                pass
            suppressed_ids = getattr(game, "_suppressed_speech_ids", set())
            suppressed = handle.id in suppressed_ids
            suppressed_ids.discard(handle.id)
            game.on_agent_speech_finished(
                spoken.strip() or game._last_assistant_text,
                speech_id=handle.id,
                interrupted=handle.interrupted,
                suppressed=suppressed or failed,
            )

        asyncio.ensure_future(_watch())

    @session.on("agent_state_changed")
    def _on_agent_state(ev) -> None:
        # HOST_SPEAKING prior (WO-ADDRESSEE-H1 Task 2): the framework's
        # agent-state machine is the speech lifecycle at 1.6.6 — verified
        # in agent_activity.py: `speaking` is entered when TTS playout
        # actually starts (started_speaking_at) and left for
        # listening/thinking when playout ends or is interrupted. The pure
        # scorekeeper only holds the flag; this is the one wiring point.
        scorekeeper.host_speaking = ev.new_state == "speaking"
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
                standings = sorted(
                    game._players_payload(), key=lambda p: -p["score"]
                )
                metadata = {
                    "pipeline_latency": {
                        k: (round(sum(v) / len(v), 1) if v else None)
                        for k, v in metrics_raw.items()
                    }
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
                    standings, scorekeeper.question_number, game.highlights,
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
            except Exception as e:
                logger.error("SESSION_CLOSE | persistence error: %s", e)
            finally:
                shutdown_gate.set()

        asyncio.ensure_future(_persist())

    # --- RPC handlers (frontend -> agent): exactly two methods ---
    async def _rpc_start(data: rtc.RpcInvocationData) -> str:
        await game.start_game(source="rpc")
        return json.dumps({"ok": True, "phase": game.ui_phase})

    async def _rpc_skip(data: rtc.RpcInvocationData) -> str:
        await game.skip_question(source="rpc")
        return json.dumps({"ok": True, "question_number": scorekeeper.question_number})

    ctx.room.local_participant.register_rpc_method("lily_control.start", _rpc_start)
    ctx.room.local_participant.register_rpc_method("lily_control.skip", _rpc_skip)

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
    # NOTE: `noise_cancellation.NC()` aborts the worker at Krisp init on
    # livekit-agents==1.6.4 + livekit-plugins-noise-cancellation==0.2.6
    # (historical incident; NOT re-tested at 1.6.6, NC stays out) —
    # SIGABRT with `NcSession::initSession: Input and output sample rates
    # must be equal`. Every job accept died 2s in until this was dropped.
    # BVC() would ship but is designed to isolate the primary speaker and
    # would clip other players at the table; Lily is multi-mic-per-table
    # by design. Speechmatics ENHANCED already does its own denoise
    # server-side, so no NC on the client audio path is the safe default
    # until upstream fixes the NC model or exposes matching I/O rates.
    await session.start(room=ctx.room, agent=agent)

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
        "adult_deck": (
            audeering_pipeline is not None or lily_config.architect_mode()
        ),
        "pictures_real_sourcing": bool(lily_config.exa_api_key()),
        # Vision (Zuna port): player-shared photo analysis rides XAI_API_KEY.
        "vision": lily_vision.lily_vision_available(),
    }
    if audeering_pipeline is not None:
        def _on_track_subscribed(track, publication=None, participant=None) -> None:
            try:
                if (
                    getattr(participant, "kind", None)
                    == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT
                ):
                    return
                if getattr(track, "kind", None) != rtc.TrackKind.KIND_AUDIO:
                    return
                asyncio.ensure_future(
                    lily_audeering_client.lily_audeering_audio_fork(
                        track, audeering_pipeline
                    )
                )
            except Exception as e:
                logger.warning("LILY_AUDEERING | track hook failed: %s", e)

        # Tijoux wiring lesson (JRVS): register the handler AFTER
        # session.start() returns — handlers registered earlier fire into a
        # half-initialized session during the start window — AND run a
        # safety-net scan over ALREADY-subscribed tracks, which the handler
        # alone would miss.
        ctx.room.on("track_subscribed", _on_track_subscribed)
        try:
            for _participant in ctx.room.remote_participants.values():
                for _pub in _participant.track_publications.values():
                    _track = getattr(_pub, "track", None)
                    if _track is not None:
                        _on_track_subscribed(_track, _pub, _participant)
        except Exception as e:
            logger.warning("LILY_AUDEERING | already-subscribed scan failed: %s", e)

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
            }
        }

    # Heartbeat checkpoint loop (60s) — carries rolling latency averages so
    # "is she lagging" is a SQL query mid-game, not a post-mortem.
    asyncio.ensure_future(lily_persistence.lily_heartbeat(
        supabase, scorekeeper, heartbeat_stop,
        metadata_provider=_latency_metadata,
    ))

    # Initial truth for late joiners / reconnect snap-restore.
    await game.publish_attributes()
    await game.publish_metadata("")
    game.start_prefetch()

    # Session opener — SECOND trigger path (on_enter, inside session.start,
    # was the first). Both route through gated_say under one key per act,
    # so whichever path dispatched first wins and this one logs
    # LILY_SAY_SUPPRESSED | reason=dup instead of producing the live
    # double greeting. Kept as a belt-and-braces net: if on_enter ever
    # fails to dispatch (M1 gate — silence is her failure mode), this
    # path still opens the night.
    if reconnected:
        # G1: a reconnect resumes a LIVE game — same preemptive-off rule
        # as start_game (which this path bypasses).
        game.set_game_live_preemptive(True)
        game.start_idle_watchdog()
        await game.publish_attributes()
        game.gated_say(
            "session_rejoin",
            "rejoin",
            game.rejoin_instructions(),
            source="entrypoint",
        )
    else:
        # Fresh room: Lily speaks FIRST (M1 gate — silence is her failure
        # mode). Short lobby landing, then conversational name-fishing.
        # Memory at the door (F): same budgeted wait as on_enter, but only
        # when this path is actually going to win the dispatch race —
        # on_enter normally claimed session_greet already (this dispatch
        # then logs LILY_SAY_SUPPRESSED | reason=dup with zero extra wait).
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
