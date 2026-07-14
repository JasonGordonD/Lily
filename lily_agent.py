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

import lily_config
import lily_evaluation
import lily_persistence
from lily_binding import (
    LilyFragmentAccumulator,
    lily_extract_name_from_fragments,
    lily_is_valid_name,
)
from lily_reasoning import LilyReasoning
from lily_scorekeeper import LilyScorekeeper
from lily_tts import LilyTTS

logger = logging.getLogger("lily_agent")

_PROMPTS_DIR = Path(__file__).parent / "prompts"
LILY_SYSTEM_PROMPT = (_PROMPTS_DIR / "lily_system.txt").read_text(encoding="utf-8")
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


def _question_was_spoken(question_prompt: str, spoken_text: str) -> bool:
    """Heuristic: did this agent turn perform the armed question? Token
    overlap on distinctive words (>= 60% of prompt tokens present)."""
    if not question_prompt or not spoken_text:
        return False
    strip = lambda s: re.sub(r"[^a-z0-9\s]", " ", s.lower())
    q_tokens = [t for t in strip(question_prompt).split() if len(t) > 3]
    if not q_tokens:
        return False
    spoken = set(strip(spoken_text).split())
    hits = sum(1 for t in q_tokens if t in spoken)
    return hits / len(q_tokens) >= 0.6


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
    ) -> None:
        self.ctx = ctx
        self.sk = scorekeeper
        self.reasoning = reasoning
        self.supabase = supabase
        self.transcripts = transcript_batcher
        self.group_id = group_id

        self.session: AgentSession | None = None
        self.background_audio: BackgroundAudioPlayer | None = None
        self.stt: SpeechmaticsSTT | None = None

        self.fragments = LilyFragmentAccumulator()
        self.rounds_total = lily_config.rounds_total()
        self.sk.questions_per_round = lily_config.questions_per_round()

        # UI phase per the seam contract:
        # lobby | question | answering | reveal | scores | final
        self.ui_phase = "lobby"
        self.game_started = False
        self.game_over = False
        self.finale_sent = False
        self.prewager_standings: list[dict] | None = None

        self.next_question: dict | None = None      # prefetched N+1
        self.armed_question: dict | None = None     # in state block, awaiting ask
        self.used_prompts: list[str] = []
        self._prefetch_task: asyncio.Task | None = None
        self._window_timer: asyncio.Task | None = None
        self._judged_keys: set[str] = set()
        self._adjudicating = False
        self._bed_handle = None
        self._pending_reveal_event: dict | None = None
        self._pending_unbound_award: dict | None = None
        self._last_assistant_text = ""
        self._enroll_started = False

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
    ) -> None:
        """Room metadata: current question text + reveal payload."""
        try:
            metadata = json.dumps({
                "question": question_text or "",
                "reveal": reveal
                or {"answer": "", "winner": None, "correct": False},
                # Drives the frontend's high-contrast wager palette shift.
                "wager": self.sk.phase == "final",
            })
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

            # Runbook fallback: LILY_KB_ONLY flips supply to the curated
            # bank; bank questions bypass verification (§4.5).
            from_bank = None
            if lily_config.kb_only() and self.supabase is not None:
                from_bank = await lily_persistence.lily_fetch_bank_question(
                    self.supabase, category, tier, self.used_prompts
                )
            question = await self.reasoning.prefetch_question(
                self.sk,
                category=category,
                difficulty_tier=tier,
                avoid_questions=self.used_prompts,
                from_bank=from_bank,
            )
            if question is None and self.supabase is not None:
                # Generation failed — curated bank is the insurance policy.
                question = await lily_persistence.lily_fetch_bank_question(
                    self.supabase, category, tier, self.used_prompts
                )
                if question is not None:
                    self.sk.clear_status_notes()
            if question is not None:
                self.next_question = question
            self.publish_attributes_nowait()

        self._prefetch_task = asyncio.ensure_future(_prefetch())

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
        self.used_prompts.append(self.armed_question.get("prompt", ""))
        self._judged_keys = set()
        self._set_ui_phase("question")
        asyncio.ensure_future(
            self.publish_metadata(self.armed_question.get("prompt", ""))
        )
        self.start_prefetch()  # N+2 begins while N+1 plays out
        return True

    # -- answer window ---------------------------------------------------------

    def on_agent_speech_finished(self, spoken_text: str) -> None:
        """Called on TTS playback completion (agent stops speaking). If the
        armed question was just performed, the answer window opens HERE —
        never earlier (known v1 concession: no early buzz-ins)."""
        if self._pending_reveal_event is not None:
            # Reveal speech finished without a speaking-start hook having
            # fired the packet (safety net) — emit now so the UI never hangs.
            ev, self._pending_reveal_event = self._pending_reveal_event, None
            self.send_event_nowait("reveal", ev)
        if (
            self.armed_question is not None
            and not self.sk.answer_window_open
            and not self._adjudicating
            and _question_was_spoken(
                self.armed_question.get("prompt", ""), spoken_text
            )
        ):
            self.open_window()

    def open_window(
        self, duration: float | None = None, steal: bool = False
    ) -> None:
        dur = duration if duration is not None else lily_config.answer_window_seconds()
        self.sk.open_answer_window(duration=dur, reset_candidates=not steal)
        self._set_ui_phase("answering")
        self.publish_attributes_nowait()
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

    # -- transcript-event layer --------------------------------------------------

    def on_transcript_event(self, result: dict, text: str) -> None:
        """Deterministic enforcement layer (§11.4) — runs on every final."""
        command = result.get("control_command")
        if command == "back_to_normal":
            if self.sk.mode == "adult":
                self.sk.set_mode("general")  # sticky flag flips instantly
                self.publish_attributes_nowait()
                if self.session is not None:
                    self.session.generate_reply(
                        instructions=(
                            "A player said 'back to normal'. Adult mode is now "
                            "OFF — committed, in code. Switch registers "
                            "instantly, no ceremony, no residue, straight into "
                            "a regular category like nothing happened."
                        )
                    )
            return
        if command == "skip":
            asyncio.ensure_future(self.skip_question(source="voice"))
            return

        # Instant Tier-1 path: a clean earliest answer scores immediately.
        if result.get("candidate_recorded") and self.sk.answer_window_open:
            question = self.sk.current_question or {}
            acceptable = question.get("acceptable_answers") or []
            ordered = self.sk.ordered_candidates()
            if ordered and acceptable:
                first = ordered[0]
                if first.get("text") == text or len(ordered) == 1:
                    t1 = lily_evaluation.lily_tier1_evaluate(
                        first["text"], acceptable
                    )
                    if t1["verdict"] == "correct":
                        asyncio.ensure_future(self.adjudicate(steal_allowed=False))

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
        if self.session is not None:
            self.session.generate_reply(
                instructions=(
                    "That question was skipped. Move straight to the next "
                    "question with zero commentary about the skip and no "
                    "spotlight on who asked. If the state block has the next "
                    "question, ask it now."
                )
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
                t1 = lily_evaluation.lily_tier1_evaluate(cand["text"], acceptable)
                cand["_tier1"] = t1
                if t1["verdict"] == "correct":
                    winner_candidate = cand
                    break
                uncertain.append(cand)

            if winner_candidate is None and uncertain:
                # Tier 2 — one non-spoken LLM turn on the vocal model.
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
                        eval_tier if not cand.get("_tier1", {}).get("verdict") == "correct" else 1,
                        points if is_winner else 0,
                    ))

            missed = winner_candidate is None
            if missed and ordered and steal_allowed and not self.game_over:
                # Missed question opens a 5-second steal window.
                self._stinger(correct=False)
                self._adjudicating = False
                self.open_window(
                    duration=lily_config.steal_window_seconds(), steal=True
                )
                if self.session is not None:
                    self.session.generate_reply(
                        instructions=(
                            "Nobody landed it. Announce a five-second steal "
                            "window — quick and hot — anyone who hasn't "
                            "answered can grab it."
                        )
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
            )
            await self.publish_attributes()
            if self.supabase is not None:
                asyncio.ensure_future(
                    lily_persistence.lily_checkpoint(self.supabase, self.sk)
                )

            # Consume the question; round/phase bookkeeping.
            self.armed_question = None
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
            if self.session is not None:
                self.session.generate_reply(instructions=reveal_instr)
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

    # -- game lifecycle ---------------------------------------------------------

    async def start_game(self, source: str) -> None:
        if self.game_started:
            return
        self.game_started = True
        logger.info("LILY_STATE | GAME_START | session=%s source=%s",
                    self.sk.session_id, source)
        self.sk.set_phase("round")
        self.start_prefetch()
        self.arm_next_question()
        await self.publish_attributes()
        if not self._enroll_started and self.supabase is not None and self.stt is not None:
            self._enroll_started = True
            asyncio.ensure_future(lily_persistence.lily_enroll_voiceprints(
                self.stt, self.supabase, self.group_id, self.sk
            ))
        if self.session is not None:
            self.session.generate_reply(
                instructions=(
                    "The table is ready to start. Kick off round one with "
                    "energy. If the state block has the next question, set "
                    "the round's category and ask it now; if it does not, "
                    "banter for a beat — it is on its way."
                )
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

    # -- state block --------------------------------------------------------------

    def build_state_block(self) -> str:
        block = self.sk.build_state_block()
        extra = []
        if self.armed_question is not None and not self.sk.answer_window_open:
            extra.append(
                "NEXT QUESTION (perform it when the table is ready, "
                "faithfully): " + json.dumps(self.armed_question, ensure_ascii=False)
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
        if extra:
            block += "\n" + "\n".join(extra)
        return block

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
        return note


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
        self.session.generate_reply(
            instructions=(
                "You just joined the room. Say a short, warm, playful hello "
                "as the trivia host and get the table talking — who's here "
                "tonight?"
            )
        )

    # -- tools ------------------------------------------------------------------

    @function_tool()
    def lily_bind_speaker(
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
    async def lily_enter_adult_mode(self, context: RunContext) -> str:
        """Switch the game to the adult deck. Call ONLY after the whole
        table has verbally agreed — you asked the room directly and got a
        yes from the table, not one enthusiast."""
        self._game.sk.set_mode("adult")  # sticky flag — reverts only via
        # the deterministic "back to normal" path or a fresh consensus.
        await self._game.publish_attributes()
        return "Adult mode is ON (sticky). The layer is active; same house rules."

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

    # -- node overrides ------------------------------------------------------------

    async def llm_node(self, chat_ctx, tools, model_settings):
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
                0, ChatMessage(role="system", content=[LILY_ADULT_LAYER])
            )
        elif self._game.sk.mode != "adult" and adult_idx is not None:
            items.pop(adult_idx)  # removing the layer fully reverts her

        # State block: replace the previous injection, then append fresh.
        for i in range(len(items) - 1, -1, -1):
            m = items[i]
            if (
                getattr(m, "role", None) == "system"
                and _STATE_BLOCK_MARKER in _message_text(m)
            ):
                items.pop(i)
        chat_ctx.add_message(
            role="system", content=self._game.build_state_block()
        )

        self._game.publish_attributes_nowait()  # last_active_at heartbeat

        async for chunk in Agent.default.llm_node(
            self, chat_ctx, tools, model_settings
        ):
            yield chunk

    async def tts_node(self, text, model_settings):
        chunks = []
        async for chunk in text:
            chunks.append(chunk)
        full = "".join(chunks).strip()

        if len(full) < 3:
            # §11.1: an empty candidate (safety-filter mute, truncation) is a
            # loggable event with a retry — never silence.
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

        # MANDATORY punctuation-flush guard (Lovebirds fix): LilyTTS is
        # streaming=False, so the framework wraps it in StreamAdapter gated
        # by blingfire sentence tokenization. Lily's suspense holds produce
        # exactly the short unpunctuated fragments that deadlock the
        # SegmentSynchronizer (text_done=false / audio_done=true). Append a
        # terminal period so the tokenizer always flushes.
        last = chunks[-1]
        if isinstance(last, str):
            stripped = last.rstrip()
            if stripped and stripped[-1] not in ".!?":
                chunks[-1] = stripped + "."

        async def _replay():
            for c in chunks:
                yield c

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


async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    room_name = ctx.room.name or "unknown"
    _setup_session_log(room_name)

    # --- Session-init hardening: fail-fast on unpersistable rooms ---
    supabase = lily_persistence.lily_create_supabase_client()
    group_id = lily_config.group_id_override() or room_name
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
    game = LilyGame(ctx, scorekeeper, reasoning, supabase, transcripts, group_id)

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
        game.on_transcript_event(result, text)

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
                asyncio.ensure_future(lily_persistence.lily_enroll_voiceprints(
                    stt, supabase, group_id, scorekeeper
                ))
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
    agent = LilyAgent(
        game=game,
        instructions=LILY_SYSTEM_PROMPT,
    )
    from livekit.agents.voice.room_io import AudioInputOptions, RoomOptions
    await session.start(
        room=ctx.room,
        agent=agent,
        room_options=RoomOptions(
            audio_input=AudioInputOptions(
                noise_cancellation=noise_cancellation.NC()
            ),
        ),
    )

    # SFX: thinking bed + stingers ride BackgroundAudioPlayer.
    background_audio = BackgroundAudioPlayer(stream_timeout_ms=10000)
    await background_audio.start(room=ctx.room, agent_session=session)
    game.background_audio = background_audio

    # Heartbeat checkpoint loop (60s).
    asyncio.ensure_future(lily_persistence.lily_heartbeat(
        supabase, scorekeeper, heartbeat_stop
    ))

    # Initial truth for late joiners / reconnect snap-restore.
    await game.publish_attributes()
    await game.publish_metadata("")
    game.start_prefetch()

    if reconnected:
        game.game_started = True
        game.ui_phase = "question"
        await game.publish_attributes()
        session.generate_reply(
            instructions=(
                "You just reconnected mid-game — the state block has the "
                "scores intact. A quick in-character rejoin line ('lost you "
                "for a second — nobody touched the scores, I counted'), "
                "then pick the game back up."
            )
        )
    else:
        # Fresh room: Lily speaks FIRST (M1 gate — silence is her failure
        # mode). Short lobby landing, then conversational name-fishing.
        session.generate_reply(
            instructions=(
                "The room just opened — this is your landing line. Greet the "
                "table as Lily: two or three short, warm, excited sentences. "
                "Tell them the deal (you host, they shout answers, the screen "
                "keeps score) and ask who you've got at the table tonight — "
                "conversationally, no roll-call. Bind names as people speak."
            )
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
