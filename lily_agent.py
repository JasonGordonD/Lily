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
import lily_config
import lily_evaluation
import lily_forget
import lily_memory
import lily_persistence
import lily_say_gate
from lily_binding import (
    LilyFragmentAccumulator,
    lily_extract_name_from_fragments,
    lily_is_valid_name,
)
from lily_reasoning import LilyReasoning
from lily_scorekeeper import LilyScorekeeper
from lily_tts import LilyTTS, lily_prewarm_tts_connection

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
    # ChatContext._items confirmed at 1.6.4 (fleet injection pattern).
    items = getattr(chat_ctx, "_items", None)
    if items is None:
        items = getattr(chat_ctx, "items", [])
    return items


def _message_text(msg) -> str:
    content = getattr(msg, "content", None)
    if isinstance(content, list):
        return " ".join(str(c) for c in content)
    return str(content or "")


# Answer-window opening (persistence-audit root-cause fix): the old hard
# >=60% token-overlap gate meant a paraphrased question NEVER opened the
# window and the whole deterministic pipeline stalled. Overlap is now a
# preference, not a gate — the tiers (verbatim / paraphrase, with a min-2
# matched-token guard against incidental single-word hits) live in
# lily_evaluation.lily_question_spoken_ratio; after
# WINDOW_FALLBACK_AGENT_TURNS finished agent turns with a question armed
# in phase=question the window opens regardless.
WINDOW_FALLBACK_AGENT_TURNS = 2

# Group-id sources that are already stable — never overridden by the
# mid-session (voiceprint / name-set) upgrade path. Late participant
# metadata is the strongest signal and MAY override a name-set hash.
_STRONG_GROUP_SOURCES = (
    "participant_metadata",
    "participant_metadata_late",
    "dispatch_metadata",
    "env_override",
)


# ---------------------------------------------------------------------------
# Game director — the non-LLM surface: window timer, adjudication commit,
# SFX dispatch, state publication, checkpointing triggers.
# ---------------------------------------------------------------------------

class LilyGame:
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
        self._judged_keys: set[str] = set()
        self._spec_judge: dict[str, asyncio.Task] = {}
        self._adjudicating = False
        self._bed_handle = None
        self._pending_reveal_event: dict | None = None
        self._pending_unbound_award: dict | None = None
        self._last_assistant_text = ""
        self._enroll_started = False
        self._armed_speech_misses = 0  # agent turns finished w/o performing q
        self._group_facts_written: set = set()  # per-session fact dedupe

        # Persistent cross-session memory (rematch): the [RETURNING TABLE]
        # block loaded at session start, and this session's callouts
        # collected for the lily_memories highlights column.
        self.memory_block: str = ""
        self.highlights: list[dict] = []
        # WO-LILY-FORGETME-001: deletion right + memory transparency.
        # forget_state drives the deterministic spoken flow:
        #   idle -> pending_confirm (spoken request or confirm=false tool
        #   call) -> executing -> done (verified) | failed (retryable), or
        #   -> declined (a no drops it; only a fresh player request re-arms).
        self.forget_state: str = "idle"
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
            attrs = {
                "phase": self.ui_phase,
                "round": str(self.sk.round),
                "question_number": str(self.sk.question_number),
                "mode": self.sk.mode,  # deterministic sticky flag (§11.4)
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
    ) -> None:
        """Room metadata: current question text + reveal payload. Seam
        addition (multiple-choice WO): when the armed question carries
        choices, they ride here too — `choices` (the 4 option strings) and
        `eliminated` (50/50: indices into choices crossed out). Both keys
        are ABSENT for freeform questions (optional fields, no
        restructuring of the existing document)."""
        try:
            payload = {
                "question": question_text or "",
                "reveal": reveal
                or {"answer": "", "winner": None, "correct": False},
                # Drives the frontend's high-contrast wager palette shift.
                "wager": self.sk.phase == "final",
            }
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
        if phase != self.ui_phase:
            self.ui_phase = phase
            self.publish_attributes_nowait()

    # -- deterministic instruction speech (P2 preemptive repair) -------------

    def instructed_reply(self, instructions: str) -> None:
        """Fire a deterministic between-turn speech (reveal, steal window,
        skip, game start, mode revert) via generate_reply(instructions=...).

        This is a mutation source that CANNOT move into
        on_user_turn_completed: it happens between user turns, driven by
        timers and tool commits, and its assistant items land in the
        persistent chat context whenever the speech is scheduled. Any
        preemptive user-turn run started while this speech is in flight is
        therefore dead by construction — the 1.6.4 equivalence check
        (agent_activity.py: preemptive.chat_ctx.is_equivalent(...)) will
        discard it. Rather than paying for that dead LLM run, preemptive
        generation is paused here and resumed on TTS playout completion
        (on_agent_speech_finished), using the live-read agent-level
        turn_handling["preemptive_generation"]["enabled"] flag that 1.6.4
        exposes."""
        if self.session is None:
            return
        if self.agent is not None:
            self.agent.set_preemptive_generation(False)
            self._preemptive_paused = True
        self.session.generate_reply(instructions=instructions)

    def _resume_preemptive(self) -> None:
        if self._preemptive_paused and self.agent is not None:
            self._preemptive_paused = False
            self.agent.set_preemptive_generation(True)

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
        if key is not None and not self.say_registry.claim(key):
            logger.warning(
                "LILY_SAY_SUPPRESSED | reason=dup | key=%s | act=%s | source=%s",
                key, act, source,
            )
            return False
        for k in extra_keys:
            self.say_registry.claim(k)
        logger.info(
            "LILY_SAY | act=%s | key=%s | source=%s", act, key or "-", source
        )
        self.instructed_reply(instructions)
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
        if self.memory_block:
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
                "banter, never a feature list read aloud. RETURNING (they "
                "say so, or a [RETURNING TABLE] block appears later): no "
                "walkthrough — offer a refresher ONCE and respect the "
                "answer. Either way the walkthrough or refresher happens "
                "at most once tonight. Never claim you remember them, and "
                "never announce it's their first time — let them tell you."
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
        return (
            "You just reconnected mid-game — the state block has the "
            "scores intact. A quick in-character rejoin line ('lost you "
            "for a second — nobody touched the scores, I counted'), "
            "then pick the game back up."
        )

    # -- question supply ------------------------------------------------------

    def _round_for_next_question(self) -> int:
        return min(
            self.rounds_total + 1,
            self.sk.question_number // self.sk.questions_per_round + 1,
        )

    def _category_for_round(self, rnd: int) -> str:
        return CATEGORY_FAMILIES[(rnd - 1) % len(CATEGORY_FAMILIES)]

    def _difficulty_for_round(self, rnd: int) -> int:
        if rnd <= 1:
            return 1
        if rnd > self.rounds_total:
            return 4  # final wager question runs mean
        return min(3, rnd)

    def start_prefetch(self) -> None:
        """Prefetch N+1 in the background while the current question plays
        out. Failure writes an honest status note (§11.2)."""
        if self._prefetch_task and not self._prefetch_task.done():
            return
        if self.next_question is not None or self.game_over:
            return

        async def _prefetch() -> None:
            rnd = self._round_for_next_question()
            category = self._category_for_round(rnd)
            tier = self._difficulty_for_round(rnd)
            # Multiple-choice WO: the format of the round this question is
            # FOR (schedule/override), decided at prefetch time.
            mc = self.sk.format_for_round(rnd) == "multiple_choice"

            # Per-group asked history (migration 010): bank draws exclude
            # the group's served kb_ ids and normalized-text hashes.
            history_ids = lily_bank.lily_history_question_ids(self.asked_history)
            history_hashes = lily_bank.lily_history_hashes(self.asked_history)

            # Runbook fallback: LILY_KB_ONLY flips supply to the curated
            # bank; bank questions bypass verification (§4.5).
            from_bank = None
            if lily_config.kb_only() and self.supabase is not None:
                from_bank = await lily_persistence.lily_fetch_bank_question(
                    self.supabase, category, tier, self.used_prompts,
                    mode=self.sk.mode,
                    exclude_ids=history_ids, exclude_hashes=history_hashes,
                )
            question = await self.reasoning.prefetch_question(
                self.sk,
                category=category,
                difficulty_tier=tier,
                avoid_questions=self.used_prompts,
                from_bank=from_bank,
                multiple_choice=mc,
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
                question = await lily_persistence.lily_fetch_bank_question(
                    self.supabase, category, tier, self.used_prompts,
                    mode=self.sk.mode,
                    exclude_ids=history_ids, exclude_hashes=history_hashes,
                )
                if question is not None:
                    self.sk.clear_status_notes()
                    if mc:
                        # Bank rows carry no choices — synthesize here too.
                        await self.reasoning.ensure_choices(question)
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
                ):
                    if self.arm_next_question() and self.session is not None:
                        logger.info(
                            "LILY_STATE | PREFETCH_AUTO_ADVANCE | session=%s q=%d",
                            self.sk.session_id, self.sk.question_number,
                        )
                        # Keyless nudge: the q_{N}_delivery claim happens in
                        # tts_node when the question text actually goes out,
                        # so a racing second deliverer stays silent.
                        self.gated_say(
                            None,
                            "question_nudge",
                            (
                                "The next question just landed in the state "
                                "block. Bridge in one short beat and ask it "
                                "now, word for word."
                            ),
                            source="prefetch_auto_advance",
                        )
            self.publish_attributes_nowait()

        self._prefetch_task = asyncio.ensure_future(_prefetch())

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
        })
        if self.supabase is not None:
            asyncio.ensure_future(lily_bank.lily_record_asked(
                self.supabase, self.group_id, dict(self.armed_question),
                self.sk.session_id,
            ))
        self._armed_speech_misses = 0
        self._judged_keys = set()
        self._addressee_rows = {}  # B1: row-id tasks are per-question
        for _task in self._spec_judge.values():
            if not _task.done():
                _task.cancel()
        self._spec_judge = {}
        self._set_ui_phase("question")
        # Metadata publish moved to DELIVERY time (the q_{N}_delivery claim
        # in tts_node, with a window-open fallback): publishing here clobbered
        # the reveal metadata milliseconds after adjudication (no visible
        # verdict) and put the next question on screen while Lily was still
        # mid-celebration — screen truth must equal spoken truth.
        self.start_prefetch()  # N+2 begins while N+1 plays out
        return True

    # -- answer window ---------------------------------------------------------

    def on_agent_speech_finished(self, spoken_text: str) -> None:
        """Called on TTS playback completion (agent stops speaking). If the
        armed question was just performed, the answer window opens HERE —
        never earlier (known v1 concession: no early buzz-ins)."""
        # A deterministic instruction speech finished playing out — its
        # items are committed, the persistent context is stable again, so
        # preemptive generation can resume (P2).
        self._resume_preemptive()
        # Say gate: playout completed — pending speech-act claims are now
        # genuinely spoken. Confirmed acts never release (a confirmed act
        # can never be redelivered); only a claimed-but-never-played act
        # releases, on the tts_node playback-failure path.
        confirmed = self.say_registry.confirm_pending()
        if confirmed:
            logger.info(
                "LILY_SAY | CONFIRMED | keys=%s", ",".join(sorted(confirmed))
            )
        if self._pending_reveal_event is not None:
            # Reveal speech finished without a speaking-start hook having
            # fired the packet (safety net) — emit now so the UI never hangs.
            ev, self._pending_reveal_event = self._pending_reveal_event, None
            self.send_event_nowait("reveal", ev)
        if (
            self.armed_question is not None
            and not self.sk.answer_window_open
            and not self._adjudicating
        ):
            ratio = lily_evaluation.lily_question_spoken_ratio(
                self.armed_question.get("prompt", ""), spoken_text
            )
            if ratio >= lily_evaluation.QUESTION_SPOKEN_VERBATIM_RATIO:
                reason = "verbatim"
            elif ratio >= lily_evaluation.QUESTION_SPOKEN_PARAPHRASE_RATIO:
                reason = "paraphrase"
            else:
                # Overlap is a preference, not a gate: after N finished
                # agent turns with a question armed in phase=question, open
                # anyway — the pipeline must never stall on phrasing.
                self._armed_speech_misses += 1
                if (
                    self._armed_speech_misses < WINDOW_FALLBACK_AGENT_TURNS
                    or self.ui_phase != "question"
                ):
                    return
                reason = "fallback_any_agent_speech"
                logger.warning(
                    "LILY_WINDOW | FALLBACK_OPEN | session=%s q=%d ratio=%.2f "
                    "— question likely paraphrased beyond recognition",
                    self.sk.session_id, self.sk.question_number, ratio,
                )
            logger.info(
                "LILY_WINDOW | OPEN | session=%s q=%d reason=%s ratio=%.2f",
                self.sk.session_id, self.sk.question_number, reason, ratio,
            )
            self.open_window()

    def open_window(
        self, duration: float | None = None, steal: bool = False
    ) -> None:
        dur = duration if duration is not None else lily_config.answer_window_seconds()
        self.sk.open_answer_window(duration=dur, reset_candidates=not steal)
        self._set_ui_phase("answering")
        self.publish_attributes_nowait()
        # Fallback screen sync (LWW-idempotent with the delivery-claim
        # publish): a paraphrased ask below the verbatim ratio never fires
        # the claim, but the tiered window-open detector still caught it —
        # the question must be on the glass once answers are live.
        if not steal and self.armed_question is not None:
            asyncio.ensure_future(
                self.publish_metadata(
                    self.armed_question.get("prompt", ""),
                    choices=self.armed_question.get("choices"),
                    eliminated=self.eliminated,
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

    def _addressee_row(
        self,
        text: str,
        speaker_label: str | None,
        player: str | None,
        segment_ts: float,
        agent_action: str,
        system_directed: bool,
    ) -> dict:
        """Build one lily_addressee_log row. fuzzy_matched_answer is the
        Tier-1 verdict against the LIVE question's acceptable_answers (null
        when no live question); seconds_into_window comes off the
        scorekeeper's window opened_at."""
        question = self.sk.current_question or {}
        acceptable = question.get("acceptable_answers") or []
        fuzzy = None
        if self.sk.current_question is not None and acceptable:
            fuzzy = (
                lily_evaluation.lily_tier1_evaluate(text, acceptable)["verdict"]
                == "correct"
            )
        window_open = self.sk.is_window_open(now=segment_ts)
        return {
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
            "label": None,
            "label_source": None,
            # Task 6 addressee synergy: the key is ALWAYS present — a dict
            # when the pipeline is healthy, an explicit SQL null (not an
            # absent column) when the circuit breaker is open or nothing
            # has been captured.
            "acoustic_snapshot": self.acoustic.addressee_snapshot(),
        }

    def _log_addressee_segment(
        self,
        result: dict,
        text: str,
        speaker_label: str | None,
        segment_ts: float,
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
    ) -> None:
        """Deterministic enforcement layer (§11.4) — runs on every final."""
        ts = segment_ts if segment_ts is not None else time.time()

        # B1 corpus row — logged BEFORE command handling so skipped-on and
        # system-directed segments are captured too (fire-and-forget).
        self._log_addressee_segment(result, text, speaker_label, ts)

        # Task 6: one acoustic-trajectory row per finalized user turn
        # (fire-and-forget; no-op when no capture has landed).
        self.log_acoustic_trajectory()

        # B1 explicit ground truth: a pending clarify is resolved by the
        # NEXT finalized segment from that player, whatever it says.
        player = result.get("player")
        if player and player in self.pending_clarify:
            self._resolve_clarify(player, text)

        command = result.get("control_command")

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
                self.publish_attributes_nowait()
                self.gated_say(
                    None,
                    "mode_revert",
                    "A player said 'back to normal'. Adult mode is now "
                    "OFF — committed, in code. Switch registers "
                    "instantly, no ceremony, no residue, straight into "
                    "a regular category like nothing happened.",
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

        # Safety-net auto-start: cheap gate check on every user segment so
        # the game can start off the ambient chatter of a settled lobby —
        # useful for tables that never touched the UI start button and
        # where Lily hasn't yet called lily_begin_round.
        if not self.game_started and not self.game_over:
            self._maybe_auto_start_after_lobby()

        # Instant Tier-1 path: a clean earliest answer scores immediately.
        if result.get("candidate_recorded") and self.sk.answer_window_open:
            question = self.sk.current_question or {}
            acceptable = question.get("acceptable_answers") or []
            ordered = self.sk.ordered_candidates()
            if ordered and acceptable:
                first = ordered[0]
                if first.get("text") == text or len(ordered) == 1:
                    # Format dispatch (multiple-choice WO): a question with
                    # four choices runs the MC matcher (letters, positions,
                    # option text); freeform runs acceptable_answers.
                    t1 = lily_evaluation.lily_tier1_evaluate_question(
                        first["text"], question
                    )
                    if t1["verdict"] == "correct":
                        asyncio.ensure_future(self.adjudicate(steal_allowed=False))
                        return
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
                    t1 = lily_evaluation.lily_tier1_evaluate_question(
                        cand["text"], question
                    )
                    if t1["verdict"] == "uncertain":
                        self._spec_judge[key] = asyncio.ensure_future(
                            self._speculative_judge(question, cand["text"], key)
                        )

    async def _speculative_judge(
        self, question: dict, attempt_text: str, key: str
    ) -> dict | None:
        """One single-attempt Tier-2 call, fired mid-window. Returns the
        parsed verdict dict or None. Never raises."""
        try:
            raw = await self.reasoning.judge(
                lily_evaluation.LILY_JUDGE_INSTRUCTIONS,
                lily_evaluation.lily_build_judge_prompt(
                    question.get("prompt", ""),
                    str(question.get("canonical_answer", "")),
                    [(key, attempt_text)],
                    acceptable_answers=question.get("acceptable_answers") or [],
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
        if self.armed_question is None and not self.sk.answer_window_open:
            return
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
        self.arm_next_question()
        self.gated_say(
            None,
            "skip",
            "That question was skipped. Move straight to the next "
            "question with zero commentary about the skip and no "
            "spotlight on who asked. If the state block has the next "
            "question, ask it now.",
            source=f"skip_{source}",
        )

    # -- adjudication ---------------------------------------------------------

    async def adjudicate(self, steal_allowed: bool = True) -> None:
        """Close the window and commit results. The scorekeeper decided
        ORDER by timestamps; Tier-1/Tier-2 decide CORRECTNESS; this commit
        happens BEFORE Lily narrates (§11.3 event-bound truth)."""
        if self._adjudicating or self.armed_question is None:
            return
        self._adjudicating = True
        try:
            if self._window_timer and not self._window_timer.done():
                self._window_timer.cancel()
            self.sk.close_answer_window()
            self._stop_bed()

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

            for cand in ordered:
                # Format dispatch (multiple-choice WO): MC questions match
                # letters / positions / option text and may return a
                # DEFINITIVE "incorrect" (clean wrong pick — no Tier-2);
                # only "uncertain" (mumbles) escalates to the judge.
                t1 = lily_evaluation.lily_tier1_evaluate_question(
                    cand["text"], question
                )
                cand["_tier1"] = t1
                if t1["verdict"] == "correct":
                    winner_candidate = cand
                    break
                if t1["verdict"] == "uncertain":
                    uncertain.append(cand)

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
                    try:
                        raw = await self.reasoning.judge(
                            lily_evaluation.LILY_JUDGE_INSTRUCTIONS,
                            lily_evaluation.lily_build_judge_prompt(
                                question.get("prompt", ""),
                                str(question.get("canonical_answer", "")),
                                attempts,
                                acceptable_answers=acceptable,
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
            if missed and ordered and steal_allowed and not self.game_over:
                # Missed question opens a 5-second steal window.
                self._stinger(correct=False)
                self._adjudicating = False
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
            }
            self._pending_reveal_event = reveal_payload
            self._set_ui_phase("reveal")
            await self.publish_metadata(
                question.get("prompt", ""),
                reveal={
                    "answer": str(question.get("canonical_answer", "")),
                    "winner": winner,
                    "correct": winner_candidate is not None,
                },
                choices=question.get("choices"),
                eliminated=self.eliminated,
            )
            await self.publish_attributes()
            if self.supabase is not None:
                asyncio.ensure_future(
                    lily_persistence.lily_checkpoint(self.supabase, self.sk)
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
                self.arm_next_question()
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
            self.gated_say(
                f"q_{revealed_qnum}_reveal",
                act,
                reveal_instr,
                source="adjudicate",
                extra_keys=extra,
            )
        finally:
            self._adjudicating = False

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
        if winner:
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
                "two allowed spreadsheet moments), then roll into the next "
                "round — tighter and faster."
            )
        else:
            parts.append(
                "Then one relational score line (deltas, not standings) and "
                "straight into the next question from the state block."
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
        if not fact or self.supabase is None:
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

    def fire_enrollment(self, trigger: str) -> None:
        """Voiceprint enrollment, fire-and-forget. group_id is passed as a
        callable so the upsert lands under whatever id is resolved by the
        time Speechmatics returns identifiers."""
        if self.supabase is None or self.stt is None:
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
        if self.sk.question_number == 0:
            memory = await lily_memory.lily_load_group_memory(
                self.supabase, new_group_id
            )
            block = lily_memory.lily_build_memory_block(memory)
            if block:
                self.memory_block = block  # llm_node injects it next turn
                self.memory_total_games = int(
                    (memory or {}).get("total_games") or 0
                )
                logger.info(
                    "LILY_MEMORY | BLOCK_READY | group=%s chars=%d "
                    "total_games=%s (post-upgrade)",
                    new_group_id, len(block),
                    (memory or {}).get("total_games"),
                )
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
            "keep for this table — voices, games, facts — gone for good, "
            "and tonight's game keeps going. Something like: 'Happy to. "
            "That wipes everything I keep for this table — voices, games, "
            "facts — gone for good. Tonight's game keeps going. Say yes "
            "and it's done.' One question only — never ask twice, never "
            "argue for being remembered.",
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
        target = self._forget_target_group or self.group_id
        self._forget_target_group = target
        logger.info(
            "LILY_FORGET | EXECUTE | session=%s group=%s source=%s retry=%s",
            self.sk.session_id, target, source, not first_attempt,
        )
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
        # STT: clear the enrolled speakers so no future STT stream this
        # session re-injects the deleted voiceprints. 1.6.4 NOTE:
        # livekit-plugins-speechmatics 1.6.4 has NO live de-enrollment
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
        # Roster is as stable as it gets — resolve the durable group id
        # BEFORE the first question so memories/facts/voiceprints key on it
        # (and reload memory for a returning table while there's still a
        # greeting moment to use it in).
        try:
            await self.resolve_group_identity(trigger="game_start")
        except Exception as e:
            logger.warning("LILY_MEMORY | GROUP_ID_RESOLVE | failed: %s", e)
        self.sk.set_phase("round")
        self.start_prefetch()
        self.arm_next_question()
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
            )
        self.gated_say(
            None, "game_start", instructions, source=f"start_{source}"
        )

    async def finish_game(self) -> None:
        """Finale: event fires AT OR BEFORE the phase=final attribute flip,
        never after (frontend's single confetti trigger)."""
        if self.game_over:
            return
        self.game_over = True
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
        if self.supabase is not None:
            asyncio.ensure_future(lily_persistence.lily_checkpoint(
                self.supabase, self.sk, final_standings=standings,
            ))
            # Session memory — idempotent (session_id upsert), so the
            # shutdown callback writing again is safe.
            asyncio.ensure_future(lily_memory.lily_write_session_memory(
                self.supabase, self.group_id, self.sk.session_id,
                standings, self.sk.question_number, self.highlights,
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
        logger.warning(
            "LILY_AUDEERING_VETO | ADULT_MODE_EXIT | session=%s tier=%s %s",
            self.sk.session_id, event.get("tier"),
            lily_audeering_consumers.PERCEIVED_FRAMING,
        )
        self.sk.set_mode("general")  # sticky flag flips instantly, in code
        self.publish_attributes_nowait()
        if self.session is not None:
            self.gated_say(
                None,
                "mode_revert",
                "Adult mode is now OFF — committed, in code. Switch back "
                "to the regular deck instantly with a light, in-character "
                "pivot line. Do NOT explain why, do NOT mention any "
                "system, audio, detection, or safety mechanism — just "
                "change gears like a host reading the room. Ask a "
                "general-category question next.",
                source="child_signal",
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
        # NOTE (supersedes the spec's dynamic max_speakers idea): the 1.6.4
        # Speechmatics plugin has NO in-flight update path for max_speakers
        # (only update_speakers(focus/ignore/focus_mode)). The cap is set at
        # construction to product max (6 players + 1).
        logger.info(
            "LILY_STT | roster=%d (max_speakers fixed at construction)",
            self.sk.roster_size(),
        )
        # Voiceprint enrollment fires the moment the FIRST binding commits
        # (the speaker has spoken; Speechmatics needs ~5 words per voice —
        # re-fired at game start, group-id upgrade, and session close for
        # late binders, so an early empty result self-heals).
        if not self._enroll_started:
            self._enroll_started = True
            self.fire_enrollment("first_bind")
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
        self._game.sk.bind_speaker(label, name)
        note = self._game.on_speaker_bound(label, name)
        return f"Bound: voice {label} is {name}.{note}"

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
    async def lily_enter_adult_mode(self, context: RunContext) -> str:
        """Switch the game to the adult deck. Call ONLY after the whole
        table has verbally agreed — you asked the room directly and got a
        yes from the table, not one enthusiast."""
        # WO-LILY-AUDEERING-001 Task 4 decline gate: the child-signal ladder
        # VETOES entry. The acoustic signal can EXIT or BLOCK adult mode,
        # NEVER authorize it — whole-room verbal consensus remains necessary
        # and is no longer sufficient. The module estimates how the speaker
        # sounds, not necessarily the actual attributes of the speaker
        # (age MAE ±8.46yr).
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
        # the deterministic "back to normal" path, a fresh consensus, or the
        # child-signal ladder veto (on_child_signal — same sticky path).
        await self._game.publish_attributes()
        return "Adult mode is ON (sticky). The layer is active; same house rules."

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
        """Per-turn preemptive control. 1.6.4 reads the agent-level
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
    # the 1.6.4 equivalence check (preemptive.chat_ctx.is_equivalent(...))
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
        # Signature verified against livekit-agents 1.6.4
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
        #    _pipeline_reply_task_impl (verified at 1.6.4), which is
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
            released = self._game.say_registry.release_pending()
            for k in released:
                logger.warning(
                    "LILY_SAY | RELEASED | key=%s | reason=empty_candidate "
                    "— retry may redeliver", k,
                )
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

        # BUG-2 enforcement — ONE authoritative question delivery: if this
        # outbound turn performs the armed question (same verbatim
        # detector that opens the answer window), it must claim
        # q_{N}_delivery at dispatch. A failed claim means another turn
        # already delivered question N — this duplicate is made physically
        # silent (no retry: the turn was suppressed, not swallowed).
        game = self._game
        armed = getattr(game, "armed_question", None)
        if armed is not None and not game.sk.answer_window_open:
            ratio = lily_evaluation.lily_question_spoken_ratio(
                armed.get("prompt", ""), full
            )
            if ratio >= lily_evaluation.QUESTION_SPOKEN_VERBATIM_RATIO:
                key = f"q_{game.sk.question_number}_delivery"
                if game.say_registry.claim(key):
                    logger.info(
                        "LILY_SAY | act=question_delivery | key=%s | "
                        "source=tts_node", key,
                    )
                    # Screen syncs to the spoken question at delivery, not
                    # at arm (arm-time publish spoiled the reveal and led
                    # the voice by a whole celebration beat). MC choices
                    # ride the same publish (seam addition).
                    asyncio.ensure_future(
                        game.publish_metadata(
                            armed.get("prompt", ""),
                            choices=armed.get("choices"),
                            eliminated=game.eliminated,
                        )
                    )
                else:
                    logger.warning(
                        "LILY_SAY_SUPPRESSED | reason=dup | key=%s | "
                        "act=question_delivery | source=tts_node", key,
                    )
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
    group_id, group_id_source = await _resolve_initial_group_id(ctx, room_name)
    logger.info(
        "LILY_MEMORY | GROUP_ID | source=%s group_id=%s",
        group_id_source, group_id,
    )

    # --- Session-init hardening: fail-fast on unpersistable rooms ---
    supabase = lily_persistence.lily_create_supabase_client()
    lily_persistence.lily_init_session(supabase, room_name, group_id)

    scorekeeper = LilyScorekeeper(
        session_id=room_name,  # session_id = room name, never random UUIDs
        answer_window_seconds=lily_config.answer_window_seconds(),
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

    # Late-arrival upgrade hook: if the initial resolution fell through to a
    # weak id (room name / name-set hash) and a participant with
    # lily_group_id metadata joins later, upgrade to it — the metadata UUID
    # is the strongest signal.
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
            if candidate and candidate != game.group_id:
                asyncio.ensure_future(
                    game.upgrade_group_id(candidate, "participant_metadata_late")
                )
        except Exception as e:
            logger.warning("LILY_MEMORY | GROUP_ID | late-join scan failed: %s", e)

    ctx.room.on("participant_connected", _on_participant_connected)

    # Returning-table memory: last games + group facts -> [RETURNING TABLE]
    # system block (injected in llm_node alongside the adult layer).
    group_memory = await lily_memory.lily_load_group_memory(supabase, group_id)
    game.memory_block = lily_memory.lily_build_memory_block(group_memory)
    # Task 4 disclosure counter (WO-LILY-FORGETME-001): the lily_memories
    # row count doubles as the persistent disclosure counter.
    game.memory_total_games = int((group_memory or {}).get("total_games") or 0)
    if game.memory_block:
        logger.info(
            "LILY_MEMORY | BLOCK_READY | group=%s chars=%d total_games=%s",
            group_id, len(game.memory_block),
            (group_memory or {}).get("total_games"),
        )

    # Bank curation (WO-LILY-OMNIBUS-002 D/F): the group's served-question
    # history (no-repeat guard on bank draws + generated output) and the
    # promoted category-candidate names (lobby state-block line).
    game.asked_history = await lily_bank.lily_load_asked_history(
        supabase, group_id
    )
    game.promoted_categories = await lily_bank.lily_load_promoted_categories(
        supabase
    )

    # Returning group: stored voiceprints -> known_speakers (instant
    # recognition on rematch). Loaded before STT construction so the
    # identifiers ride the constructor.
    known_rows = await lily_persistence.lily_load_voiceprints(supabase, group_id)
    known_speakers = [
        SpeakerIdentifier(
            label=row["label"], speaker_identifiers=row["speaker_identifiers"]
        )
        for row in known_rows
        if row.get("label") and row.get("speaker_identifiers")
    ]

    # --- STT: Speechmatics multi-speaker fleet profile (Part II §1.1) ---
    # NOTE: livekit-plugins-speechmatics 1.6.4 does not expose a `model=`
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
        # No in-flight max_speakers update exists at 1.6.4 — fixed at
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
        tts=LilyTTS(),  # Raven's voice via LILY_VOICE_ID/RAVEN_VOICE_ID
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
        # UserInputTranscribedEvent carries no per-segment word timings at
        # 1.6.4 — created_at (event arrival) decides answer order.
        created = getattr(ev, "created_at", None)
        seg_ts = created.timestamp() if hasattr(created, "timestamp") else time.time()
        game.fragments.add(speaker_label or "UU", text)
        result = scorekeeper.on_transcript_segment(
            text=text,
            speaker_label=speaker_label,
            is_final=True,
            segment_start_time=seg_ts,
        )
        player = result.get("player")
        transcripts.add(
            text,
            speaker_label=speaker_label,
            speaker_name=player,
            segment_start=seg_ts,
        )
        game.on_transcript_event(
            result, text, speaker_label=speaker_label, segment_ts=seg_ts
        )

    # --- Answer window opens on TTS playback completion (per-utterance
    # precise via SpeechHandle.wait_for_playout; no dedicated
    # playout-finished session event exists at 1.6.4) ---
    @session.on("speech_created")
    def _on_speech_created(ev) -> None:
        handle = ev.speech_handle

        async def _watch() -> None:
            await handle.wait_for_playout()
            spoken = ""
            try:
                for item in handle.chat_items:
                    if getattr(item, "role", None) == "assistant":
                        spoken += " " + _message_text(item)
            except Exception:
                pass
            game.on_agent_speech_finished(
                spoken.strip() or game._last_assistant_text
            )

        asyncio.ensure_future(_watch())

    @session.on("agent_state_changed")
    def _on_agent_state(ev) -> None:
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
                # Session memory — idempotent with the finish_game write
                # (upsert on session_id); this path also covers sessions
                # that end without reaching the final question.
                # game.group_id (not the entrypoint local): a mid-session
                # upgrade may have re-keyed the group.
                await lily_memory.lily_write_session_memory(
                    supabase, game.group_id, scorekeeper.session_id,
                    standings, scorekeeper.question_number, game.highlights,
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

    # --- Start ---
    # Say gate: on_enter (fired inside session.start) must know whether
    # this is a fresh room (session_greet) or a reconnect (session_rejoin)
    # — the two openers carry distinct keys and must never trip each other.
    game.reconnected = reconnected
    agent = LilyAgent(
        game=game,
        instructions=LILY_SYSTEM_PROMPT,
    )
    game.agent = agent  # P2: per-turn preemptive-generation control
    # NOTE: `noise_cancellation.NC()` aborts the worker at Krisp init on
    # livekit-agents==1.6.4 + livekit-plugins-noise-cancellation==0.2.6 —
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
        game.game_started = True
        game.ui_phase = "question"
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
        # 1.6.4 issue only bites when limit>0, so the ceiling is set
        # consciously high (default 2048MB, LILY_JOB_MEMORY_LIMIT_MB).
        job_memory_warn_mb=lily_config.job_memory_limit_mb() * 0.75,
        job_memory_limit_mb=lily_config.job_memory_limit_mb(),
    ))
