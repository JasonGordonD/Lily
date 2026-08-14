"""LilyGlass -- what the room has been SHOWN: attributes, metadata, events,
image/camera lanes, the state-view honesty surface (W3 Cut 5).

INVARIANT: published attributes / metadata / events / image_shown reflect
COMMITTED state -- idempotent I/O, no state ownership. StateView stays the
module-level typed schema + render(); this mixin POPULATES it from game state
(_build_state_view) and publishes. Byte-identical MOVE (self.* unchanged)."""

from __future__ import annotations

import asyncio
import uuid
import json
import time

from dataclasses import dataclass, field

from livekit import rtc, api

import lily_addressee_classifier
import lily_capabilities
import lily_forget
import lily_memory
import lily_config
import lily_scorekeeper
from lily_scorekeeper import lily_detect_state_contradiction

import logging
logger = logging.getLogger("lily_agent")


def lily_spine_line(
    *,
    phase: str,
    q: int | str | None,
    delivery: str,
    window: str,
    hold: str,
    supply: str,
) -> str:
    """One-line operability spine: phase / q / delivery / window / hold /
    supply. Pure + testable; logged as ``LILY_SPINE``."""
    return (
        f"LILY_SPINE | phase={phase or '-'} q={q if q is not None else '-'} "
        f"delivery={delivery or '-'} window={window or '-'} "
        f"hold={hold or '-'} supply={supply or '-'}"
    )


EVENTS_TOPIC = "lily.events"
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


@dataclass
class StateView:
    """Typed schema for the STABLE half of the state block — the honesty
    surface and the prompt-cache prefix. Every field the prefix may carry is
    a named slot here (rendered line, or None/empty when absent); the block
    is built by populating slots, not by hand-appending to a list. `render()`
    walks the slots in a FIXED order so the same game state produces the same
    bytes every render — no accidental extra line, no reordered field can
    silently bust the cache. Slots are grouped by concern (phase / delivery /
    identity / score / picture / next-question / notes …), matching W3's
    intended owner split. A slot never holds answer material: the armed
    NEXT-QUESTION slot carries prompt/category/choices/image status only, so
    canonical_answer can never reach the stable block."""

    # Emission order below IS the render order — do not reorder without
    # updating the byte-identity fixture.
    answered_closed: Optional[str] = None
    delivery_or_hold: Optional[str] = None
    identity_intake: Optional[str] = None
    score: Optional[str] = None
    roster: Optional[str] = None
    picture_lane: Optional[str] = None
    camera_lane: Optional[str] = None
    glass_image: Optional[str] = None
    custom_round: Optional[str] = None
    delivery_pace: Optional[str] = None
    responsiveness: Optional[str] = None
    acoustic: list = field(default_factory=list)
    floor_read: Optional[str] = None
    architect: Optional[str] = None
    availability: Optional[str] = None
    said_already: Optional[str] = None
    device_candidate: Optional[str] = None
    next_question: Optional[str] = None
    unbound_award: Optional[str] = None
    state_note: Optional[str] = None
    returner_note: Optional[str] = None
    why_note: Optional[str] = None
    identity_probe: Optional[str] = None
    returner_claim: Optional[str] = None
    late_recognition: Optional[str] = None
    recognition_dispute: Optional[str] = None
    ambiguous_yes: Optional[str] = None
    setup_pending: Optional[str] = None
    adult_consent: Optional[str] = None
    floor_speaking: Optional[str] = None
    explain_note: Optional[str] = None
    contest_note: Optional[str] = None
    late_answer_note: Optional[str] = None
    lobby: list = field(default_factory=list)

    # The ONE authority for stable-block field order.
    _ORDER = (
        "answered_closed", "delivery_or_hold", "identity_intake", "score",
        "roster", "picture_lane", "camera_lane", "glass_image", "custom_round",
        "delivery_pace", "responsiveness", "acoustic", "floor_read",
        "architect", "availability", "said_already", "device_candidate",
        "next_question", "unbound_award", "state_note", "returner_note",
        "why_note", "identity_probe", "returner_claim", "late_recognition",
        "recognition_dispute", "ambiguous_yes", "setup_pending",
        "adult_consent", "floor_speaking", "explain_note", "contest_note",
        "late_answer_note", "lobby",
    )

    def render(self) -> list:
        """The stable extra-lines list, in fixed order. List-valued slots
        (acoustic, lobby) splice their items in place; empty slots are
        skipped. Deterministic over the populated view — the byte-stability
        contract lives here."""
        out: list = []
        for name in self._ORDER:
            value = getattr(self, name)
            if not value:
                continue
            if isinstance(value, list):
                out.extend(value)
            else:
                out.append(value)
        return out




class LilyGlassMixin:
    async def publish_attributes(self, phase: str | None = None) -> None:
        """LWW participant attributes — updated on every phase transition
        and score change. Exact key spellings per the seam contract.

        `phase` is the phase this publish was SCHEDULED for. Reading it
        live here loses any transition that a later synchronous statement
        overwrote before the loop ran — see publish_attributes_nowait."""
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
                "phase": phase or self._phase_hold or self.ui_phase,
                "round": str(self.sk.round),
                "question_number": str(self.sk.question_number),
                "mode": "adult",  # unified deck (§11.4)
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
                # SEAM ADDITION (WS-6, WO-LILY-OMNIBUS-003): supply
                # readiness. "false" only in the live supply-stall state
                # (game running, no question armed OR prefetched, no ruling
                # in flight) — the starvation the 583a0f16 session sat in
                # with no screen cue. WS-9 renders a "next question loading"
                # cue off this flag. String-valued like the rest of the LWW
                # attribute set; read as `attributes.next_question_ready ===
                # "true"`.
                "next_question_ready": (
                    "true" if self.next_question_ready() else "false"
                ),
                "last_active_at": str(int(time.time())),
            }
            await self.ctx.room.local_participant.set_attributes(attrs)
            self.log_spine()
        except Exception as e:
            logger.warning("LILY_STATE | attribute publish failed: %s", e)

    def publish_attributes_nowait(self) -> None:
        # Y2: every state change that is worth showing the glass is a state
        # change the next speculation must see — settle rides the same
        # chokepoint (synchronous + idempotent, so fixtures and the llm_node
        # heartbeat are no-ops when nothing changed).
        self.settle_context_nowait()
        # Build the coroutine only when a loop exists. Several pure unit
        # fixtures exercise state transitions synchronously; constructing it
        # first and then failing in ensure_future leaked an unawaited coroutine.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        # BIND THE PHASE AT SCHEDULE TIME, not at task-run time. The
        # coroutine used to read self.ui_phase when the loop got round to
        # it, so two consecutive SYNCHRONOUS transitions collapsed to
        # whichever ran last. adjudicate does exactly that:
        #
        #     if round_over: self._set_ui_phase("scores")   # queues A
        #     if not self.arm_next_question():              # -> "question", queues B
        #
        # Both A and B published "question". `phase=scores` has never once
        # reached the wire, so LilyStandings — a whole designed screen —
        # has never rendered on a normal round boundary, only when supply
        # was starved enough for arm to fail. Verified with a repro against
        # the real publish path: ['question', 'question'].
        loop.create_task(
            self.publish_attributes(phase=self._phase_hold or self.ui_phase)
        )

    def spine_fields(self) -> dict:
        """Snapshot the operability spine for logs / tests."""
        sk = self.sk
        phase = self._phase_hold or getattr(self, "ui_phase", None) or "-"
        q = getattr(sk, "question_number", None)
        pending = self._pending_delivery_qnum
        active = self._active_delivery_qnum
        if pending is not None:
            delivery = f"pending:{pending}"
        elif active is not None:
            delivery = f"active:{active}"
        elif getattr(self, "armed_question", None) is not None:
            delivery = "armed"
        else:
            delivery = "none"
        if getattr(sk, "answer_window_open", False):
            window = "steal" if self._steal_window else "open"
        else:
            window = "closed"
        if self._delivery_stop_sticky:
            hold = "stop_sticky"
        elif self._hold_active:
            hold = "wait"
        elif self._question_pending:
            hold = "q_pending"
        else:
            hold = "clear"
        if not getattr(self, "game_started", False):
            supply = "lobby"
        elif getattr(self, "game_over", False):
            supply = "over"
        elif self._delivery_stop_sticky:
            supply = "stopped"
        elif self.next_question_ready():
            supply = "ready"
        else:
            supply = "stall"
        return {
            "phase": str(phase),
            "q": q,
            "delivery": delivery,
            "window": window,
            "hold": hold,
            "supply": supply,
        }

    def log_spine(self) -> str:
        """Emit ``LILY_SPINE`` once per distinct snapshot (deduped)."""
        fields = self.spine_fields()
        line = lily_spine_line(**fields)
        if self._last_spine_line == line:
            return line
        self._last_spine_line = line
        logger.info("%s", line)
        return line

    # -- REFACTOR WAVE 1a: GameControl typed control plane ----------------
    #
    # The spine (spine_fields, above) is a log line. GameControl is the same
    # snapshot promoted to a typed decision object with one gate — may(act).
    # This wave DERIVES the control from the existing latches and runs it in
    # SHADOW next to the latch gates (gated_say, adjudicate): the latches stay
    # authoritative, may() is consulted, and a parity check records any
    # divergence. Once may() reproduces the latch decisions across the suite
    # (zero divergences), the next wave flips authority to may() and deletes
    # the latches. Direction is control-FROM-latches so behavior is
    # byte-identical while parity is proven.

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
                # HOTFIX-005 X6: stamp the question number this metadata
                # projects, so the glass can detect a stale render (displayed
                # question id ≠ active question id) against the attribute
                # question number and log it. Same ledger/journal source as
                # the spoken lane — no independent client state.
                "question_number": int(self.sk.question_number),
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
            # B3: empty image_url clears glass confirm so "picture up" cannot
            # trail a cleared publish (render gate reads this state).
            if not payload.get("image_url"):
                self._glass_image_url = None
                self._glass_image_at = None
                self._glass_image_pending_url = None
                self._glass_image_pending_at = None
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

    def publish_agent_transcription_nowait(
        self, text: str, *, speech_id: str | None, interrupted: bool,
        final: bool = True,
    ) -> None:
        """Publish one RTC transcript from authoritative TTS text.

        Default RoomIO agent text output is disabled; otherwise the client
        sees the pre-TTS model prose and this corrected transcript as two
        different Lily turns.

        Transcript sync (2026-08-09 live report: "what is being said and
        what is displayed is off"): publishing ONLY at playout completion
        meant a long turn showed nothing until she finished it — the panel
        trailed the voice by the whole turn. note_playout_started now
        publishes the same bound text as an INTERIM segment (final=False)
        the moment audio starts; this completion publish (same
        lk.segment_id, final=True, with any "…[cut off]" marker) replaces
        it in place — the glass upserts interims by segment id and locks
        on final. The durable record still binds at completion only (Y5).
        """
        clean = (text or "").strip()
        if not clean:
            return
        ctx = getattr(self, "ctx", None)
        room = getattr(ctx, "room", None)
        participant = getattr(room, "local_participant", None)
        if participant is None:
            return
        publications = getattr(participant, "track_publications", {}) or {}
        track_sid = ""
        for publication in publications.values():
            if getattr(publication, "kind", None) == rtc.TrackKind.KIND_AUDIO:
                track_sid = str(getattr(publication, "sid", "") or "")
                if track_sid:
                    break
        if not track_sid:
            logger.warning(
                "LILY_TRANSCRIPT | PUBLISH_SKIPPED | session=%s "
                "speech_id=%s reason=no_audio_track",
                self.sk.session_id, speech_id,
            )
            return
        value = clean + (" …[cut off]" if interrupted else "")
        segment_id = speech_id or f"lily-{uuid.uuid4().hex}"
        final_attr = "true" if final else "false"

        async def _publish() -> None:
            try:
                await participant.publish_transcription(
                    rtc.Transcription(
                        participant_identity=participant.identity,
                        track_sid=track_sid,
                        segments=[
                            rtc.TranscriptionSegment(
                                id=segment_id,
                                text=value,
                                start_time=0,
                                end_time=0,
                                language="en",
                                final=final,
                            )
                        ],
                    )
                )
            except Exception as exc:
                logger.warning(
                    "LILY_TRANSCRIPT | PUBLISH_FAILED | session=%s "
                    "speech_id=%s error=%s",
                    self.sk.session_id, speech_id, exc,
                )

        # Glass transcript (2026-08-09 live report: transcript panel ran
        # EMPTY): the panel consumes lk.transcription TEXT STREAMS
        # (useTranscriptions), while the publish above speaks only the
        # LEGACY rtc.Transcription API. Mirror the same final text onto
        # the stream wire — same segment id, marked final — so the glass
        # renders Lily's turns again. Legacy publish stays for older
        # clients; P0-C is preserved (this is the corrected post-TTS text,
        # RoomIO's pre-TTS prose stays off).
        async def _publish_stream() -> None:
            try:
                writer = await participant.stream_text(
                    topic="lk.transcription",
                    attributes={
                        "lk.transcription_final": final_attr,
                        "lk.segment_id": segment_id,
                        "lk.transcribed_track_id": track_sid,
                    },
                )
                await writer.write(value)
                await writer.aclose()
            except Exception as exc:
                logger.warning(
                    "LILY_TRANSCRIPT | STREAM_PUBLISH_FAILED | session=%s "
                    "speech_id=%s error=%s",
                    self.sk.session_id, speech_id, exc,
                )

        try:
            loop = asyncio.get_running_loop()
            # Legacy rtc.Transcription: durable record + older clients. It
            # surfaces via the deprecated TranscriptionReceived event, which
            # the modern useTranscriptions panel does NOT read, so it never
            # duplicates the framework's text-stream lines — always scheduled.
            loop.create_task(_publish())
            # WO-LILY-UI-SYNC-TYPEWRITER-001: when the framework drives the
            # agent transcript (RoomOptions text_output on), IT owns the
            # lk.transcription text stream. Publishing this manual stream too
            # would put a second, differently-segmented copy of every Lily
            # turn on the wire — a duplicate line in the panel and a rival
            # segment for the board. Suppress the manual stream leg; the
            # framework's own final chunk (transcription_final=true) is the
            # board's snap-complete signal, and this call's legacy publish +
            # record_agent_turn remain the durable/cut record. Off ⇒ the
            # manual stream stays the single source (legacy path).
            if not lily_config.voice_synced_transcript_enabled():
                loop.create_task(_publish_stream())
        except RuntimeError:
            pass

    def publish_user_transcript_nowait(
        self, text: str, *, speaker_label: str | None, utterance_id: str | None
    ) -> None:
        """Forward one FINAL user utterance to the glass transcript.

        P0-C set RoomOptions text_output=False so the agent's pre-TTS
        prose can't race the corrected transcript — but that switch ALSO
        turned off the framework's USER transcript forwarding
        (_ParticipantTranscriptionOutput only builds when text output is
        on), so the glass transcript ran completely empty (live
        2026-08-09 report). This publishes the same wire shape the
        framework would: a lk.transcription text stream with
        sender_identity impersonating the speaking device's participant,
        so the glass attributes the line to the table — never to Lily.
        The bound player name rides prmpt.speaker_label when the roster
        resolves one. Fire-and-forget; never blocks the STT hot path."""
        clean = (text or "").strip()
        if not clean:
            return
        ctx = getattr(self, "ctx", None)
        room = getattr(ctx, "room", None)
        local = getattr(room, "local_participant", None)
        if local is None:
            return
        user_identity = ""
        for remote in (getattr(room, "remote_participants", {}) or {}).values():
            if (
                getattr(remote, "kind", None)
                != rtc.ParticipantKind.PARTICIPANT_KIND_AGENT
            ):
                user_identity = str(getattr(remote, "identity", "") or "")
                if user_identity:
                    break
        if not user_identity:
            return
        attributes = {
            "lk.transcription_final": "true",
            "lk.segment_id": str(utterance_id or f"user-{uuid.uuid4().hex}"),
        }
        try:
            resolved, _method = self.sk.resolve_speaker(
                None, speaker_label, None, clean
            )
            if resolved:
                attributes["prmpt.speaker_label"] = resolved
        except Exception:
            pass  # display-only nicety — never blocks the forward

        async def _forward() -> None:
            try:
                writer = await local.stream_text(
                    topic="lk.transcription",
                    sender_identity=user_identity,
                    attributes=attributes,
                )
                await writer.write(clean)
                await writer.aclose()
            except Exception as exc:
                logger.warning(
                    "LILY_TRANSCRIPT | USER_FORWARD_FAILED | session=%s "
                    "error=%s", self.sk.session_id, exc,
                )

        try:
            asyncio.get_running_loop().create_task(_forward())
        except RuntimeError:
            pass

    def _room_talk_recency_seconds(self, *, cluster: bool) -> float:
        """How long a player-to-player read stays live, in the CLASSIFIER's
        own units — no new tuning knob. A locked side-cluster is a running
        conversation and keeps the floor for its own liveness bound
        (cluster_max_gap_seconds); a lone table-talk line goes stale as
        fast as the adjacency window it was scored against."""
        classifier = getattr(self, "addressee_classifier", None)
        if cluster:
            return float(getattr(classifier, "cluster_max_gap_seconds", 15.0))
        return float(getattr(classifier, "adjacency_seconds", 4.0))

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

        # PATCH-002 A5/T12 — STOP primitive, at the very top so it bypasses
        # the LLM and can never be answered by a re-aired question. The
        # consult (roster read → detect → route into the hold) lives in
        # maybe_route_stop so the routing is offline-testable on the real
        # production path (W8).
        if self.maybe_route_stop(text):
            return
        if (
            self.game_delivery_stopped()
            and lily_scorekeeper.lily_detect_resume_game(text)
        ):
            self.resume_game_delivery(reason="spoken_resume")
        # PATCH-002 A4 — any user final RELEASES the hold (they've spoken;
        # conversation may resume). Sticky STOP remains an independent game
        # delivery freeze unless the explicit resume detector above fired.
        if self._hold_active:
            self.release_hold(reason="user_speech")
        # PATCH-003 P6 — the table answered the question she asked: release
        # the pending state so her normal speak-by-default engages this
        # turn as the response (she finishes the conversation she started).
        if self._question_pending:
            self.release_question_pending(reason="user_answered")

        self.request_device_verification("final_transcript")

        # HOTFIX-004 Defect 1: latch an explicit spoken 18+ consent. Once a
        # real affirmative is heard it stays latched for the session (a
        # table doesn't un-consent by chatting on); the adult-mode gate
        # reads this deterministic floor, so "Should I verify?" — a question,
        # not consent — can never unlock the deck even if the model sets its
        # flag.
        if not getattr(
            self, "_age_consent_confirmed", False
        ) and lily_scorekeeper.lily_detect_age_consent(text):
            self._age_consent_confirmed = True
            self.mark_setup_applied("consent")
            logger.info(
                "LILY_ADULT_GATE | AGE_CONSENT_DETECTED | session=%s text=%r",
                self.sk.session_id, str(text)[:80],
            )

        # VIDEOIN-001 V1: an explicit spoken "look at this" opens the sparse
        # camera lane (and signals the frontend to publish) — but only when
        # available (never in the adult deck) and not already open. The
        # camera never publishes without this trigger or the UI control.
        if (
            self.camera_lane_status()["available"]
            and self.sk.camera_lane != "open"
            and lily_scorekeeper.lily_detect_camera_request(text)
        ):
            asyncio.ensure_future(self.open_camera_lane(source="spoken_request"))

        # Durable voice-identity probe. Do not spend the session's one attempt
        # until the bounded PCM probe is actually ready: the former first-final
        # trigger usually fired after <4s of speech, saw no PCM, and permanently
        # disabled matching for the rest of the session.
        self.maybe_start_voice_identity_match()

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

        # FL-1 (WO-LILY-FLOOR-001): the addressee judgment for this
        # utterance — structured output emitted here, ahead of any reply
        # generation, so the reply is conditioned on it (build_state_block
        # carries the floor read) and the corpus row below records it.
        judgment = self.classify_addressee(
            result, text, speaker_label=speaker_label, segment_ts=ts,
            nbest=nbest,
        )
        # HOTFIX-006 N4: FL-1's judgment rides the CANDIDATE it was computed
        # for, so the adjudication boundary can consult the fused
        # classification and not only the deterministic answer-shape floor.
        # The classifier runs after the scorekeeper's recording pass, which
        # is why the judgment is stamped here rather than at record time.
        if result.get("candidate_recorded") and judgment is not None:
            cand = self.sk.answer_candidates.get(
                result.get("player") or f"unrostered:{speaker_label or 'UU'}"
            )
            if cand is not None:
                cand["fl1_classification"] = judgment.classification
                cand["fl1_score"] = judgment.score

        # B1 corpus row — logged BEFORE command handling so skipped-on and
        # system-directed segments are captured too (fire-and-forget).
        self._log_addressee_segment(
            result, text, speaker_label, ts, nbest=nbest, judgment=judgment
        )

        # Task 6: one acoustic-trajectory row per finalized user turn
        # (fire-and-forget; no-op when no capture has landed).
        self.log_acoustic_trajectory()

        # B1 explicit ground truth: a pending clarify is resolved by the
        # NEXT finalized segment from that player, whatever it says.
        player = result.get("player")
        if player and player in self.pending_clarify:
            self._resolve_clarify(player, text)

        # WS-8 enrollment retry driver: a bound player who was below the
        # ~5-word Speechmatics floor at the last enrollment pass is tracked
        # in scorekeeper.unenrolled_bound_labels. Every further final from
        # them adds words toward the floor, so re-fire enrollment (cooldown
        # so a chatty under-threshold voice doesn't spam get_speaker_ids)
        # — the point that never enrolled silently now keeps retrying.
        self._maybe_retry_enrollment(player)

        command = result.get("control_command")

        # P0-2: non-exclusive setup parse BEFORE any start dispatch. The
        # scorekeeper result is command-or-media for scoring, but setup must
        # retain voice + adult + pictures + consent + play from one final.
        setup_intents = self.note_lobby_setup_intents(text)

        # WO-2: if her last turn was an A-or-B offer, a bare yes must not
        # open round one — lock kickoff until explicit start language.
        try:
            self.note_user_start_intent(text, command)
        except Exception:
            pass

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

        # RECOGNITION-HONESTY: a player asserting prior contact / that Lily
        # should recognize them (the 19:41 live failure — she DENIED it
        # three times). When her table card is genuinely blank (no memory
        # block, no verified device identity), she must NOT deny or argue;
        # the note conditions the reply toward the honest gap line. When she
        # DOES have grounds (memory/verified), the recognition beats own the
        # turn and this gate stays quiet.
        returner_claim = lily_scorekeeper.lily_detect_returner_claim(text)
        if returner_claim:
            # Persistent session truth, independent of whether this same
            # utterance also carries a command/media choice. Multi-intent
            # must never make the identity guard disappear.
            self._returner_claim_seen = True
            logger.info(
                "LILY_MEMORY | RETURNER_CLAIM_SEEN | session=%s player=%s "
                "text=%r",
                self.sk.session_id, player, str(text)[:120],
            )
        if (
            returner_claim
            and not self.memory_block
            and not getattr(self, "device_identity_verified", False)
        ):
            self._returner_honesty_note = (
                "[returner-claim honesty — a player just asserted you've met "
                "before, or that you should know their voice, and your table "
                "card for tonight is blank. LAW: do NOT deny prior contact, "
                "do NOT say you've never played together, do NOT tell them "
                "their voice isn't on file as if that settles it, do NOT "
                "argue with their memory of you. The honest truth you MAY "
                "give ONLY IF your recognition check has already come back "
                "(if it has not, say nothing about memory at all — see "
                "below): a blank card is a gap in YOUR records "
                "and you do not know what caused it. Do NOT name a cause on "
                "their end — not a new device, not a cleared browser. You "
                "cannot see which link dropped, and on 2026-08-08 that guess "
                "was WRONG: the card was blank because the group id never "
                "reached the agent, and a returning player was told his own "
                "setup was at fault. Never proof they're "
                "wrong. Believe them, name the gap once if you haven't, then "
                "move forward warmly. If you already named it this session, "
                "don't repeat it — just don't deny. AND IF THE "
                "RECOGNITION CHECK IS STILL OUT: you do not know whether "
                "you know them. Say nothing about your memory, your card "
                "or a clean slate — not even to concede a gap. Take their "
                "word warmly and carry on; recognition may land in the "
                "next minute and a denial spoken now is one you will have "
                "to retract.]"
            )
            logger.info(
                "LILY_HONESTY | RETURNER_CLAIM | session=%s player=%s "
                "text=%r — ungrounded, honesty gate armed",
                self.sk.session_id, player, str(text)[:120],
            )
            # A returner claim while the card looks blank is itself a
            # recognition dispute — lock kickoff until honesty lands.
            # can_claim_empty_memory is now always false after this claim.
            self.arm_recognition_dispute(reason="returner_claim")

        # P0-B/C: explicit challenge to a false clean-slate / "why blank?"
        if (
            command is None
            and not result.get("media_choice")
            and lily_scorekeeper.lily_detect_recognition_dispute(text)
        ):
            self.arm_recognition_dispute(reason="clean_slate_challenge")

        # HOTFIX-005 X12(1): explain-on-request. A player asking for the
        # ACTIVE question to be restated in plain words (operator asked twice
        # at 14:40 and got nothing) arms a one-shot directive: restate THIS
        # question plainly before anything else. Only meaningful with a
        # question on the table; the answer is never leaked.
        if (
            command is None
            and not result.get("media_choice")
            and self.armed_question is not None
            and lily_scorekeeper.lily_detect_explain_request(text)
        ):
            prompt_text = str(self.armed_question.get("prompt", "")).strip()
            self._explain_request_note = (
                "[explain request — a player asked you to explain the CURRENT "
                "question. Before anything else, restate THIS question in "
                "plainer, simpler words (same meaning, do NOT leak or hint the "
                "answer), then let them answer. Do not skip it, do not move "
                "on, do not reveal. The question on the table is: "
                f"\"{prompt_text}\".]"
            )
            logger.info(
                "LILY_EXPLAIN | REQUEST | session=%s q=%d — restate armed",
                self.sk.session_id, self.sk.question_number,
            )

        # HOTFIX-005 X12(2): verdict contest. A player asserting they were
        # misheard / were right (operator said "the correct answer is A"
        # three times and was brushed off with "we're past that") earns ONE
        # grounded re-check against the committed record. One-shot per
        # contest; the directive names the committed truth so the reply
        # corrects or explains, never dismisses.
        if (
            command is None
            and not result.get("media_choice")
            and not self._contest_note
            and lily_scorekeeper.lily_detect_verdict_contest(text)
        ):
            self._contest_note = (
                "[verdict contest — a player says they were misheard, that "
                "their answer was right, or that a rule was misapplied. Give "
                "them ONE honest re-check against the committed record (the "
                "SCORES field and the last ruling) and the recorded utterance. "
                "If the ruling was wrong — a correct answer denied, an answer "
                "misheard, a rule misapplied (a clock on a relaxed round), or "
                "a call made outside its own window — put it right with "
                "lily_correct_verdict (grounds = answer_denied / misheard / "
                "wrong_rule / out_of_window). That tool APPENDS an audited "
                "correction and restores the point; it will refuse if there is "
                "no committed verdict to amend, so you can never invent a "
                "point. Say what you're fixing and why ('that one was yours — "
                "putting the point back'). If the ruling stands, tell them "
                "exactly why in one line. Never brush it off with 'we're past "
                "that' or 'the board is locked'. One re-check only.]"
            )
            logger.info(
                "LILY_CONTEST | REQUEST | session=%s player=%s text=%r",
                self.sk.session_id, player, str(text)[:120],
            )

        # W3 confirmation beat resolution: a pacing change was held because
        # it contradicted a stated preference. The requester's next
        # parseable yes applies it, a no keeps the current pacing, anything
        # ambiguous stays pending. Runs before command dispatch (like the
        # forget flow) so "yes" resolves the beat rather than reading as a
        # fresh command. A brand-new pacing command (not yes/no) falls
        # through to dispatch, which clears the pending slot on apply.
        if (
            self._pending_pacing is not None
            and command not in ("pacing_relaxed", "pacing_timed")
        ):
            speaker_key = player or speaker_label
            if (
                self._pending_pacing_requester is None
                or speaker_key == self._pending_pacing_requester
            ):
                verdict = lily_forget.lily_parse_forget_confirmation(text)
                if verdict == "yes":
                    target = self._pending_pacing
                    self._pending_pacing = None
                    self._pending_pacing_requester = None
                    if self.set_pacing(target, source="voice_confirm"):
                        note = (
                            "answer windows now run about twice as long — "
                            "loose tempo from here, no countdown talk"
                            if target == "relaxed"
                            else "the standard answer clock is back on"
                        )
                        self.gated_say(
                            None,
                            "pacing_set",
                            f"They confirmed the switch to {target} pacing — "
                            f"committed, in code, saved as this table's "
                            f"usual: {note}. One light line, then keep the "
                            "night moving.",
                            source="voice_confirm",
                        )
                    return
                if verdict == "no":
                    kept = self.sk.pacing
                    self._pending_pacing = None
                    self._pending_pacing_requester = None
                    self.gated_say(
                        None,
                        "pacing_kept",
                        f"They said no — pacing stays {kept}, nothing "
                        "changed. One light line honoring the earlier "
                        "choice, then straight back into the game. Never "
                        "re-raise it.",
                        source="voice_confirm",
                    )
                    return

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
        if command in ("pacing_relaxed", "pacing_timed"):
            # Group prefs WO: the spoken pacing choice is committed in code
            # (flag + persisted prefs) before Lily says a word about it —
            # same contract as "back to normal".
            pacing = "relaxed" if command == "pacing_relaxed" else "timed"
            # W3 confirmation beat: if this contradicts a pacing the player
            # already stated this session, do NOT flip silently — hold it
            # and ask once (the incident: a relaxed table should never have
            # a clock re-enabled without asking). A re-stated command while
            # a beat is already pending reads as assent (falls through to
            # apply below and clears the slot).
            stated = (self.prefs or {}).get("pacing")
            if (
                self._pending_pacing is None
                and stated in ("timed", "relaxed")
                and stated != pacing
            ):
                self._pending_pacing = pacing
                self._pending_pacing_requester = player or speaker_label
                # MEDIUM-2: wording must match the pref's real provenance —
                # "chose this session" only if a live write set it; a value
                # merged from cross-session memory is "the usual on file",
                # never a this-session claim.
                if self._pacing_stated_this_session:
                    provenance = (
                        f"already chose {stated} pacing earlier this session"
                    )
                else:
                    provenance = (
                        f"has {stated} pacing on file as its usual from before"
                    )
                self.gated_say(
                    None,
                    "pacing_confirm",
                    f"A player asked for {pacing} pacing, but this table "
                    f"{provenance} — that is a contradiction, so confirm "
                    "before switching, never flip it silently. One light "
                    "line naming that earlier preference and asking if they "
                    f"want to switch to {pacing}. Nothing changes until they "
                    "say yes.",
                    source="voice_command",
                )
                return
            self._pending_pacing = None
            self._pending_pacing_requester = None
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
                # C7: a heard start intent OWNS the lobby from here on. The
                # empty-STOP lobby recovery must never answer the dead air
                # that follows a start with a rejoin/"welcome back" script
                # (Session A: "Starts." -> 13s dead -> false re-greet off a
                # stale same-room checkpoint).
                self._start_intent_heard = True
                asyncio.ensure_future(self.start_game(source="voice"))
            return

        # Media-mode spoken choice (sub-agent K): sticky flag flips
        # instantly, in code — same discipline as "back to normal".
        # PATCH-003 P7: a delivery-rate request ('speak slower') is applied
        # in code BEFORE the ack — the fixture was a fragment + no change.
        # One warm ack (operator anchor 'Slower it is.'); the state block
        # carries the pace so sentences shorten and pauses lengthen on both
        # voices whether or not the voice honors the speed param.
        pace_request = lily_scorekeeper.lily_detect_pace_request(text)
        if pace_request and pace_request != self.sk.delivery_pace:
            self.set_delivery_pace(pace_request)
            self.publish_attributes_nowait()
            if pace_request == "slow":
                self.gated_say(
                    None, "pace_ack",
                    "The player asked you to slow down — it's applied in "
                    "code and takes effect on your next turn. REGISTER "
                    "GUIDANCE (vary freely within this length and "
                    "temperature, never longer): one short warm sentence — "
                    "'Slower it is.' Then, from here on, shorter sentences "
                    "and a beat more pause between them.",
                    source="voice_command",
                )
            else:
                self.gated_say(
                    None, "pace_ack",
                    "The player asked you to speed back up — applied in "
                    "code. REGISTER GUIDANCE (vary freely within this "
                    "length and temperature, never longer): one short warm "
                    "sentence acknowledging it, then keep moving at your "
                    "normal clip.",
                    source="voice_command",
                )

        # Full setup parse wins over the scorekeeper's command/media XOR.
        media_choice = setup_intents.get("media") or result.get("media_choice")
        if media_choice and media_choice != self.sk.media_mode:
            if media_choice == "pictures":
                if "pictures" in self.pending_setup_jobs():
                    # Adult/heat setup owns activation; do not draw from the
                    # general partition while those jobs are pending.
                    logger.info(
                        "LILY_SETUP | MEDIA_DEFERRED | session=%s pending=%s",
                        self.sk.session_id,
                        ",".join(sorted(self.pending_setup_jobs())),
                    )
                    return
                # PATCH-003 P1: activation is a REAL flip, dependency-checked.
                # A down lane never flips to a false "ON" — the honest,
                # specific unavailability line names the real cause (P4
                # grounding) and media_mode stays voice_only.
                self.try_activate_pictures(source="voice_command")
            else:
                self.sk.set_media_mode(media_choice)
                self._pending_picture_on_offer = False
                self.publish_attributes_nowait()
                self.gated_say(
                    None, "media_mode",
                    "The table asked for voice only. Pictures are OFF — "
                    "committed, in code. One short confirmation, no "
                    "ceremony, keep moving.",
                    source="voice_command",
                )
        elif (
            not media_choice
            and self._pending_picture_on_offer
        ):
            # She offered "want them on?" — short yes / live flips the flag
            # so the stocked arsenal can serve (E66E1B: bank ready, mode stuck).
            try:
                self.note_picture_on_confirm(text)
            except Exception:
                pass

        # Safety-net auto-start: cheap gate check on every user segment so
        # the game can start off the ambient chatter of a settled lobby —
        # useful for tables that never touched the UI start button and
        # where Lily hasn't yet called lily_begin_round.
        if not self.game_started and not self.game_over:
            self._maybe_auto_start_after_lobby()

        # HOTFIX-006 N9 part 2: a CORRECT answer that arrived after the
        # window closed is a defined outcome, not a silent loss. Inside the
        # stated grace margin the window itself already admitted it (it
        # would be a candidate above); past the margin this announces the
        # miss with its reason and audits the real utterance. Never runs
        # while a window is open, and never for meta-speech or a wrong
        # guess — see note_late_answer.
        if (
            not result.get("candidate_recorded")
            and not self.sk.answer_window_open
            and not result.get("control_command")
            and not result.get("system_directed")
        ):
            try:
                self.note_late_answer(
                    text,
                    player=result.get("player"),
                    speaker_label=speaker_label,
                    segment_ts=ts,
                    utterance_id=result.get("utterance_id"),
                )
            except Exception as e:
                logger.warning("LILY_ANSWER | LATE_CHECK_FAILED: %s", e)

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
            # ── HOSTLOOP-001 C6: THE RECEIPT SEAM ─────────────────────────
            # THIS is the moment "an answer utterance completed" and its
            # Tier-1 verdict is knowable — deterministic string matching,
            # microseconds. Everything downstream (Tier-2, the commit, the
            # reveal publishes, the LLM composite) is what put the spoken
            # verdict 8–13s behind the answer in Session B and put the ack
            # for answer N behind answer N+1 in Session A. So the SHORT
            # receipt is voiced here, ahead of all of it, and the styled
            # composite follows unchanged.
            #
            # Keyed on THIS utterance's own candidate, not ordered[0]: the
            # instant-scoring path below deliberately only looks at the
            # earliest candidate (only it can win), but every answerer is
            # owed a receipt for the words they just said — a second player
            # answering into an open window used to hear nothing at all
            # until the composite.
            if acceptable:
                mine = next(
                    (c for c in ordered if c.get("text") == text), None
                )
                if mine is not None:
                    receipt_t1 = self._tier1_question(
                        mine["text"], question,
                        key=mine["player"]
                        or f"unrostered:{mine['speaker_label']}",
                        threshold=tier1_threshold,
                    )
                    # ...unless she is about to ASK about this utterance
                    # instead of acknowledging it. The Task-4 clarify fires
                    # exactly on the middle BAND, which is a subset of the
                    # uncertain verdict, and "Locked in—" followed by "was
                    # that an answer, or thinking out loud?" contradicts
                    # itself. The clarify question is that utterance's
                    # receipt — deterministic, same sub-2s window — so the
                    # ack stands down and the band read is the existing one,
                    # not a second rule.
                    if not self._receipt_yields_to_clarify(
                        receipt_t1, tier1_threshold
                    ):
                        self.fire_answer_receipt(
                            receipt_t1["verdict"],
                            text=mine["text"],
                            player=mine.get("player"),
                            source="instant_tier1",
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
                        self.mark_deterministic_reply(text)
                        asyncio.ensure_future(self.adjudicate(steal_allowed=False))
                        return
                    # WS-11 garble gate — checked BEFORE the band trigger and
                    # AFTER the clean-correct guard (so a garbled final that
                    # still fuzzy-matches adjudicates and never double-speaks
                    # a clarify over a reveal). A low mean per-word confidence
                    # over a multi-word final fires the light repeat-request
                    # posture; tap-only mode's single hypothesis keeps the
                    # dispersion gate blind to this, which is why it keys on
                    # the recognizer's own word confidences. Returns early
                    # when it fires so the band trigger doesn't stack.
                    if self._maybe_fire_confidence_clarify(
                        result, speaker_label, ts, nbest
                    ):
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
            # HOTFIX-009 W4: relaxed pacing has no clock to close this beat,
            # so it closes on the roster instead — once this candidate
            # completes the rostered set, adjudicate now (no-op in timed
            # mode, and when the roster is not yet complete).
            self._maybe_close_relaxed_beat()

    def picture_lane_status(self) -> dict:
        """PATCH-003 P4: field-granular picture-lane truth. Each field is
        SEPARATELY readable so a claim or refusal cites only the field that
        actually reads off — the anti-fabrication mechanism ('picture
        search is off tonight' was false against the ledger; tone cannot
        be the mechanism). Pure read, no side effects."""
        adult = True
        gen_key = bool(
            lily_config.xai_api_key() if adult else lily_config.google_api_key_present()
        )
        return {
            "media_mode": getattr(self.sk, "media_mode", "voice_only"),
            "pictures_on": getattr(self.sk, "media_mode", "voice_only") == "pictures",
            "generation_available": gen_key,
            "pipeline_available": getattr(self, "supabase", None) is not None,
            "deck": "adult" if adult else "general",
            "heat": self.sk.adult_image_intensity if adult else None,
        }

    def picture_lane_state_line(self) -> str | None:
        """One grounded state-block line built from picture_lane_status —
        the verb of any picture claim/refusal must match these reads.
        Returns None when nothing about the lane needs grounding (voice-
        only and no missing dependency to be honest about)."""
        s = self.picture_lane_status()
        if not s["pictures_on"] and s["generation_available"] and s["pipeline_available"]:
            # Voice-only with a healthy lane: only worth grounding the
            # switched-off truth so 'pictures are live' can't be fabricated.
            return (
                "picture lane read — pictures are NOT switched on "
                "(media_mode=voice_only); the lane is healthy, so if asked, "
                "the true line is 'not switched on yet — want them on?', "
                "never 'off tonight' and never 'already live'"
            )
        missing = []
        if not s["generation_available"]:
            missing.append("image generation key is not configured")
        if not s["pipeline_available"]:
            missing.append("the picture pipeline is unreachable")
        if missing:
            return (
                "picture lane read — a real dependency is down: "
                + "; ".join(missing)
                + ". THIS is the only honest reason for no pictures; name "
                "exactly it if asked, never a different cause"
            )
        if s["pictures_on"]:
            return (
                f"picture lane read — pictures ARE on (media_mode=pictures, "
                f"deck={s['deck']}"
                + (f", heat={s['heat']}" if s["heat"] else "")
                + "); a picture claim is true only for an image that "
                "actually reached the screen this question"
            )
        return None

    # -- camera lane (WO-LILY-VIDEOIN-001 — sparse show-and-tell) ---------------

    def camera_lane_status(self) -> dict:
        """V2: field-granular camera-lane truth for the state block. The
        anti-fabrication read behind every camera claim — she offers the
        camera only when it is genuinely available, and says she sees
        something only when a frame has actually been attached this turn."""
        return {
            # Camera lane is available regardless of deck (the adult-deck
            # mutual-exclusion was removed with the content-mode gate).
            "available": True,
            "open": getattr(self.sk, "camera_lane", "off") == "open",
            "frame_pending": self._latest_video_frame is not None,
            "unavailable_reason": None,
        }

    def camera_lane_state_line(self) -> str | None:
        """V2 + V3: the grounded camera line + the hardcoded describe
        constraints. Rides the state block whenever the lane is open or an
        offer would be dishonest, so 'hold it up to the camera' is
        unproducible while unavailable and 'I see it' is unproducible with no
        attached frame."""
        s = self.camera_lane_status()
        if not s["available"]:
            # In adult mode the camera is off the table — never offer it.
            return (
                "camera lane read — the camera is NOT available in the "
                "grown-up deck; do not offer it, and if asked say plainly "
                "it's off while the adult deck is on."
            )
        if s["open"] and s["frame_pending"]:
            return (
                "camera lane read — a live camera frame IS attached to THIS "
                "turn: describe only the OBJECT or SCENE actually shown. "
                "HARD RULES: never identify, name, or guess WHO a person is; "
                "compute no facial match. If what's held up is a person "
                "rather than an object, do NOT describe them — one warm line "
                "and steer back to the game."
            )
        if s["open"]:
            return (
                "camera lane read — the camera is ON but no frame has come "
                "through yet; if asked what you see, say honestly 'nothing's "
                "come through yet', never pretend to see something."
            )
        # Closed + available: only worth grounding so 'I can see it' can't be
        # fabricated before anyone opens the camera.
        return (
            "camera lane read — the camera is NOT on (nothing has been "
            "shared); you may OFFER it ('want to hold it up to the camera?'), "
            "but you cannot claim to see anything until a frame arrives."
        )

    def take_camera_frame(self):
        """Consume the most-recent camera frame for attachment to ONE turn,
        then clear it — a frame lives in its turn and is gone (no retention,
        never re-attached to a later turn). None when nothing is buffered."""
        frame = self._latest_video_frame
        self._latest_video_frame = None
        if frame is not None:
            self._camera_frame_shown = True
        return frame

    async def open_camera_lane(self, source: str) -> bool:
        """Open the sparse lane on an explicit player trigger and signal the
        frontend to publish the camera. Refused (False) in the adult deck."""
        if not self.sk.set_camera_lane("open"):
            return False
        logger.info(
            "LILY_CAMERA | LANE_OPEN | session=%s source=%s",
            self.sk.session_id, source,
        )
        await self.send_event("camera_open", {"source": source})
        return True

    async def close_camera_lane(self, source: str) -> None:
        """Auto-close after the exchange (or on request): stop the camera and
        drop any buffered frame so nothing lingers."""
        self.sk.set_camera_lane("off")
        self._latest_video_frame = None
        await self.send_event("camera_close", {"source": source})

    def publish_question_to_glass(self, *, reason: str) -> bool:
        """Put the armed question — and its picture — on the glass.

        THE DEADLOCK THIS BREAKS (live 2026-08-08 `lily-2C489B`). Two
        separate gates both bound on the delivery turn's playout COMPLETING:
        the published phase (`_phase_hold`, pinned to "lobby" at arm) and
        this metadata publish (moved to window-open so the screen could
        never lead the voice). A barged delivery completes neither. So:

          arm 22:48:55 -> delivery cut at 22:49:37, 22:49:47, 22:50:05
          -> window never opens -> phase stays "lobby", image_url never
          published -> the player stares at "START THE QUESTIONS" while
          she describes a burlesque photograph -> he interrupts to say the
          picture isn't there -> the delivery is cut again.

        His reason for interrupting WAS the thing the interruption
        prevented from being fixed. Seven minutes, zero questions played,
        and the arsenal entry burned. Barge-in is the steady state of this
        game, not an error path, so nothing the player can see may be
        gated on the host getting an uninterrupted run at a sentence.

        The screen still never LEADS the voice: this fires when the
        delivery turn's audio starts airing, so the question appears as
        the room begins hearing it. Idempotent per question — the
        window-open path calls it again as a backstop and it no-ops."""
        armed = getattr(self, "armed_question", None)
        if armed is None:
            return False
        qnum = self.sk.question_number
        if self._glass_published_qnum == qnum:
            return False
        self._glass_published_qnum = qnum
        image_url = armed.get("image_url")
        if image_url:
            # B4: arm the pending confirm — image_shown must land before
            # "look at the screen" is speakable. Armed HERE now, because
            # this is when the image is actually on its way to the client.
            self._glass_image_pending_url = str(image_url)
            self._glass_image_pending_at = time.monotonic()
        # Drop the pre-delivery lobby hold: the question is airing, so the
        # board is no longer ahead of the voice.
        self._phase_hold = None
        self.publish_attributes_nowait()
        asyncio.ensure_future(
            self.publish_metadata(
                armed.get("prompt", ""),
                choices=armed.get("choices"),
                eliminated=getattr(self, "eliminated", None) or [],
                image_url=image_url,
                category=armed.get("category"),
            )
        )
        logger.info(
            "LILY_STATE | GLASS_PUBLISHED | session=%s q=%d reason=%s image=%s",
            self.sk.session_id, qnum, reason, "yes" if image_url else "no",
        )
        return True

    def picture_activation_outcome(self) -> str:
        """PATCH-003 P1: can pictures-on actually flip right now? Reads the
        picture lane and returns 'on' (flip + confirm), 'unavailable_gen'
        (no generation key), or 'unavailable_pipeline' (pipeline down).
        Generation is checked first — no lane exists without it."""
        status = self.picture_lane_status()
        if not status["generation_available"]:
            return "unavailable_gen"
        if not status["pipeline_available"]:
            return "unavailable_pipeline"
        return "on"

    def try_activate_pictures(
        self, *, source: str, announce: bool = True
    ) -> str:
        """Dependency-checked flip to media_mode=pictures.

        Returns 'on', 'already_on', 'unavailable_gen', or
        'unavailable_pipeline'. Shared by spoken media choice, adult heat
        set, and picture-on confirm — so adult+mix never leaves the bank
        dark while the lane is healthy.

        When announce=False (tool result will speak the truth), still flip
        and publish but skip gated_say so heat+pictures don't double-air.
        """
        if getattr(self.sk, "media_mode", "voice_only") == "pictures":
            return "already_on"
        outcome = self.picture_activation_outcome()
        if outcome == "unavailable_gen":
            if announce:
                self.gated_say(
                    None, "media_mode_unavailable",
                    "The table asked for pictures but the image "
                    "GENERATION lane is not configured (no key). Do "
                    "NOT say pictures are on. Say plainly that pictures "
                    "aren't available tonight because the image "
                    "generator isn't switched on, offer to keep going "
                    "voice-only, and move on. Name no other cause.",
                    source=source,
                )
            return outcome
        if outcome == "unavailable_pipeline":
            if announce:
                self.gated_say(
                    None, "media_mode_unavailable",
                    "The table asked for pictures but the picture "
                    "PIPELINE is unreachable right now. Do NOT say "
                    "pictures are on. Say plainly the picture system "
                    "isn't reachable this session, offer voice-only, "
                    "and move on. Name no other cause.",
                    source=source,
                )
            return outcome
        self.sk.set_media_mode("pictures")
        self._pending_picture_on_offer = False
        self.publish_attributes_nowait()
        # Drop stale voice-only prefetch so the arsenal rung can stock the
        # next Q (B2: mode ON with a bank full of ready rows must not keep
        # serving a pre-flip text next_question).
        self._refresh_supply_for_pictures_on(source=source)
        if announce:
            self.gated_say(
                None, "media_mode",
                "Picture rounds are ON — committed, in code, the "
                "lane is healthy. One short confirmation (the "
                "screen is in the game now), then keep moving. Only "
                "claim an image is on the screen once one actually "
                "lands there.",
                source=source,
            )
        logger.info(
            "LILY_STATE | PICTURES_ON | session=%s source=%s",
            self.sk.session_id, source,
        )
        return "on"

    def note_picture_on_offer(self, spoken_text: str) -> None:
        """Arm when Lily offers to switch pictures on while still voice_only."""
        if getattr(self.sk, "media_mode", "voice_only") == "pictures":
            self._pending_picture_on_offer = False
            return
        if lily_scorekeeper.lily_detect_picture_on_offer(spoken_text):
            self._pending_picture_on_offer = True
            logger.info(
                "LILY_STATE | PICTURE_ON_OFFER | session=%s — short yes/"
                "live will flip media_mode",
                self.sk.session_id,
            )

    def note_picture_on_confirm(self, text: str) -> bool:
        """If a picture-on offer is pending and the user confirmed, flip."""
        if not self._pending_picture_on_offer:
            return False
        if getattr(self.sk, "media_mode", "voice_only") == "pictures":
            self._pending_picture_on_offer = False
            return False
        if not lily_scorekeeper.lily_is_picture_on_confirm(text):
            # Non-confirm reply consumes the offer (they answered the choice).
            if text and text.strip():
                self._pending_picture_on_offer = False
            return False
        self.try_activate_pictures(source="picture_on_confirm")
        return True

    def note_image_rendered(self, url: str) -> None:
        """HOTFIX-005 X4: the frontend confirmed a picture actually LOADED
        on the glass (its <img> onLoad fired), reported over the
        lily_control.image_shown RPC. Records the confirmed URL + timestamp
        so 'the picture is up' becomes a READABLE state rather than an
        assumption — grounding her picture claims and giving the DB→glass
        break (four generated images that never surfaced) an observable
        endpoint. Idempotent; a repeat confirm of the same URL just
        refreshes the timestamp."""
        if not url:
            return
        self._glass_image_url = url
        self._glass_image_at = time.monotonic()
        self._glass_image_pending_url = None
        self._glass_image_pending_at = None
        logger.info(
            "LILY_IMAGE | RENDER_CONFIRMED | session=%s url=%s",
            self.sk.session_id, url[:120],
        )

    def picture_on_glass_confirmed(self) -> bool:
        """True only when image_shown matched the armed question's URL."""
        armed = getattr(self, "armed_question", None)
        intended = None
        if isinstance(armed, dict):
            intended = armed.get("image_url") or None
        confirmed = self._glass_image_url
        return bool(intended and confirmed and confirmed == intended)

    def picture_on_glass_failed(self, *, timeout_s: float = 8.0) -> bool:
        """True when an intended image was published but never confirmed
        within timeout_s (honest didn't-land path)."""
        if self.picture_on_glass_confirmed():
            return False
        armed = getattr(self, "armed_question", None)
        intended = None
        if isinstance(armed, dict):
            intended = armed.get("image_url") or None
        if not intended:
            return False
        pending_at = self._glass_image_pending_at
        if pending_at is None:
            return False
        return (time.monotonic() - pending_at) >= timeout_s

    def _glass_image_state_line(self) -> str | None:
        """HOTFIX-005 X4: grounded readout of what is CONFIRMED on the glass
        right now. The armed question's intended image vs the last image the
        frontend confirmed loading — so a 'here's a picture' claim is true
        only when the two agree, and a generated-but-unrendered image is
        visible as a divergence instead of a silent stale rail."""
        intended = None
        armed = getattr(self, "armed_question", None)
        if isinstance(armed, dict):
            intended = armed.get("image_url") or None
        confirmed = self._glass_image_url
        if not intended and not confirmed:
            return None
        if intended and confirmed == intended:
            return (
                "PICTURE ON GLASS: CONFIRMED up (the frontend reported it "
                "loaded). Safe to reference the picture."
            )
        if intended and self.picture_on_glass_failed():
            return (
                "PICTURE ON GLASS: DID NOT LAND — the image was published "
                "but image_shown never confirmed. Do NOT claim it is on "
                "screen; say honestly it didn't land and keep going."
            )
        if intended and confirmed != intended:
            return (
                "PICTURE ON GLASS: this question HAS an image but the glass "
                "has NOT confirmed it loaded yet — do NOT claim the picture "
                "is up; if asked, say it's coming up rather than assert it's "
                "there."
            )
        return None

    def _state_extra_lines(self, *, now: float | None = None) -> list:
        """The stable extra lines, rendered from the typed StateView schema
        (W2b — schema, then render; no hand concatenation)."""
        return self._build_state_view(now=now).render()

    def _build_state_view(self, *, now: float | None = None) -> "StateView":
        view = StateView()
        answered_line = self.answered_closed_state_line()
        if answered_line:
            view.answered_closed = answered_line
        if self._delivery_stop_sticky:
            view.delivery_or_hold = (
                "game_delivery: STOPPED — conversation may continue, but "
                "do not ask, arm, reveal, score, nudge, or promise another "
                "question. Only an explicit resume/continue command clears "
                "this state."
            )
        elif self._hold_active:
            # W8: a plain hold (a self-wait-promise, a decline, P10, or a
            # backed stopped-state narration) is not the sticky STOP latch,
            # so the STOPPED directive above does not fire — yet the delivery
            # lane only mechanically suppresses a turn that performs the
            # ARMED QUESTION (mech. 13 + W2). An organic turn narrating a
            # reveal/verdict/steal/score in PROSE would slip that gate and
            # air under the hold (W2 review Residual i). Make the held state
            # explicit so the model does not produce any game payload while
            # held; the mechanical gate stays the backstop for the armed
            # question. Same context-only, leak-filtered contract.
            view.delivery_or_hold = (
                "held: you have PAUSED at the player's stop and are waiting "
                "for their go. Do not ask, arm, reveal, score, nudge, open a "
                "steal window, or promise another question — in prose or as "
                "the question itself. Acknowledge only that you've stopped "
                "and are listening; the game resumes when they say so."
            )
        intake_line = self.identity_intake_line()
        if intake_line:
            view.identity_intake = intake_line
        score_line = self._score_authority_line()
        if score_line:
            view.score = score_line
        # HOTFIX-006 N13: the roster count rides the same lane as the score
        # — injected truth, never a computed number. Same never-break,
        # context-only, leak-filtered contract as every field below.
        try:
            roster_line = self._roster_authority_line()
            if roster_line:
                view.roster = roster_line
        except Exception:
            pass
        # PATCH-003 P4: field-granular picture-lane truth — claims and
        # refusals about pictures take their verb from THIS read, never
        # from conversational momentum (the dual fabrication: 'pictures
        # are live' then 'picture search is off', both false). Context
        # only; the leak filter keeps it off the air.
        try:
            lane_line = self.picture_lane_state_line()
            if lane_line:
                view.picture_lane = lane_line
        except Exception:
            pass  # grounding is enrichment; never breaks the state block
        # VIDEOIN-001 V2/V3: the grounded camera read + the hardcoded describe
        # constraints (objects only, no person ID, person->redirect). Context
        # only; leak-filtered. Same never-break contract as the picture lane.
        try:
            cam_line = self.camera_lane_state_line()
            if cam_line:
                view.camera_lane = cam_line
        except Exception:
            pass
        # HOTFIX-005 X4: grounded glass-render readout — a picture claim is
        # true only when the frontend confirmed the image loaded. Same
        # never-break, context-only, leak-filtered contract.
        try:
            glass_line = self._glass_image_state_line()
            if glass_line:
                view.glass_image = glass_line
        except Exception:
            pass
        # HOTFIX-006 N2: the custom-round registration ledger. The tool
        # result governs the turn that calls it; this governs every turn
        # after, which is where "I'm putting your round together right now"
        # came from in lily-16A9AE. Same never-break, context-only contract.
        try:
            custom_line = self.custom_round_state_line()
            if custom_line:
                view.custom_round = custom_line
        except Exception:
            pass
        # PATCH-003 P7: a slow delivery pace shapes the TEXT on both voices
        # (the voice speed change is best-effort; this always applies).
        if getattr(self.sk, "delivery_pace", "normal") == "slow":
            view.delivery_pace = (
                "delivery pace: SLOW (the table asked) — keep sentences "
                "short and add a beat more pause between them; unhurried, "
                "never clipped"
            )
        # PATCH-003 P9: if a real state check will make an answer slow,
        # air a GROUNDED holding beat inside the budget — name the actual
        # thing being checked, never a vamp.
        if self._awaiting_address_since:
            view.responsiveness = (
                "responsiveness: someone addressed you directly — answer "
                "promptly. If the true answer needs a moment (a real check "
                "is running), say one grounded holding line naming exactly "
                "what you're checking ('one sec — checking the picture "
                "lane'), never filler"
            )
        # Room-temperature read (WO-LILY-AUDEERING-001 Task 3): NL descriptor
        # lines only — zero scalars; neutral room injects NOTHING. The [env:]
        # line appears at most once per refresh. Synchronous read — never
        # blocks the turn.
        try:
            view.acoustic = list(self.acoustic.state_block_lines())
        except Exception:
            pass  # acoustic read is enrichment; never breaks the state block
        # FL-1 floor read (WO-LILY-FLOOR-001): the latest addressee
        # judgment conditions the reply BEFORE it is generated. Context
        # only, positive framing, never spoken (10% principle — she never
        # narrates her classification state; the leak filter backstops).
        # Host-directed injects nothing: speak-by-default is unchanged
        # inside its scope.
        judgment = getattr(self, "last_addressee_judgment", None)
        if judgment is not None:
            if judgment.classification == (
                lily_addressee_classifier.CLASS_SIDE_CLUSTER
            ):
                view.floor_read = (
                    "floor read: the players are in a conversation with "
                    "each other right now — the floor is theirs. Stay "
                    "warm and quiet; rejoin the moment someone addresses "
                    "you or the game needs its host"
                )
            elif judgment.classification == (
                lily_addressee_classifier.CLASS_SIDE_CHATTER
            ):
                view.floor_read = (
                    "floor read: that last line was table talk between "
                    "players — let it breathe; respond only to what is "
                    "asked of the host"
                )
        if lily_config.architect_mode():
            view.architect = (
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
                view.availability = (
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
            view.said_already = (
                "SAID-ALREADY (re-deliver NOTHING on this ledger unless a "
                "player asks; mint fresh words instead): " + " || ".join(said)
            )
        if getattr(self, "device_candidate_group_id", None):
            view.device_candidate = (
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
                # Picture question: URL is screen transport, never speech.
                # B4: only invite "look at the screen" after image_shown.
                if self.picture_on_glass_confirmed():
                    need_to_know["image"] = (
                        "picture question — glass CONFIRMED the image "
                        "loaded; safe to point the table at the screen"
                    )
                elif self.picture_on_glass_failed():
                    need_to_know["image"] = (
                        "picture question — image DID NOT LAND on glass; "
                        "do not claim it is on screen"
                    )
                else:
                    need_to_know["image"] = (
                        "picture question — image published but NOT yet "
                        "confirmed on glass; do not claim it is up"
                    )
            view.next_question = (
                "NEXT QUESTION (perform it when the table is ready, "
                "faithfully): " + json.dumps(need_to_know, ensure_ascii=False)
            )
        elif self.next_question is not None:
            view.next_question = "next question: prefetched and ready"
        elif self.game_started and not self.game_over:
            view.next_question = (
                "next question: NOT ready yet — do not claim it is; "
                "banter until it lands"
            )
        if self._pending_unbound_award is not None:
            view.unbound_award = (
                "an unbound voice "
                f"({self._pending_unbound_award['speaker_label']}) has a "
                "point waiting — get their name and bind them"
            )
        # Honesty assist (desync WO Sub-agent C): the grounded truth for a
        # player's state callout — context only, never speech (the leak
        # filter drops the line if it ever echoes outbound). getattr: test
        # harnesses build LilyGame via __new__.
        state_note = self._state_note
        if state_note:
            view.state_note = state_note
        # RECOGNITION-HONESTY: returner-claim conditioning — context only,
        # never spoken (leak-filtered like every state note); keeps a blank
        # table card from becoming a denial of the player's own memory.
        returner_note = self._returner_honesty_note
        if returner_note:
            view.returner_note = returner_note
        why_note = self._recognition_why_note
        if why_note:
            view.why_note = why_note
        # P0-A: while the probe is outstanding, identity absence is UNKNOWN.
        if self.identity_probe_outstanding():
            view.identity_probe = (
                "identity: STILL CHECKING — do not claim empty memory or "
                "clean slate, and do not say your card/ledger doesn't have "
                "anyone (absence is UNKNOWN, not established — not even to "
                "concede a gap to a claimed returner); say you are still "
                "checking / the card is not connected yet if asked"
            )
        if self._returner_claim_seen:
            view.returner_claim = (
                "identity: RETURNER CLAIMED — the player explicitly says "
                "you have met before. A blank lookup never proves otherwise; "
                "do not say clean slate / no saved voices / no past games."
            )
        if self._late_recognition_pending:
            view.late_recognition = (
                "late_recognition: DEFERRED — a live question owns the floor. "
                "Do not mention recognition/refresher/usual until the engine "
                "releases it at the between-question seam."
            )
        if self._recognition_dispute and not getattr(
            self, "_recognition_dispute_why_answered", False
        ):
            view.recognition_dispute = (
                "recognition_dispute: ACTIVE — lily_begin_round / kickoff / "
                "category announce blocked until the why-beat lands"
            )
        if self._ambiguous_yes_blocks_start:
            view.ambiguous_yes = (
                "ambiguous_yes: ACTIVE — their last yes answered an A-or-B "
                "choice, NOT a start. Do NOT call lily_begin_round. Ask one "
                "clear confirm or wait for 'let's start' / 'let's play'."
            )
        pending_setup = self.pending_setup_jobs()
        if pending_setup:
            view.setup_pending = (
                "setup_pending: ACTIVE — finish these requested jobs BEFORE "
                "Round One: "
                + ", ".join(sorted(pending_setup))
                + ". Do NOT call lily_begin_round or announce a category. "
                "Use the matching setup tools; then confirm ready."
            )
        if self._age_consent_confirmed:
            view.adult_consent = (
                "adult_consent: CONFIRMED this session — do NOT ask for "
                "18+ confirmation again."
            )
        if self._user_speaking:
            view.floor_speaking = (
                "floor: USER SPEAKING — do not call lily_begin_round or "
                "start; listen for the rest of the turn."
            )
        # HOTFIX-005 X12: explain-on-request and verdict-contest conditioning
        # — context only, leak-filtered like every note above.
        explain_note = self._explain_request_note
        if explain_note:
            view.explain_note = explain_note
        contest_note = self._contest_note
        if contest_note:
            view.contest_note = contest_note
        # HOTFIX-006 N9: a correct answer that landed past the window. It
        # rides here so the miss is ANNOUNCED with its reason — the live
        # alternative was Rami's "Okay. It's Jupiter." vanishing while
        # "Go." took the blame on his q_1052 row.
        late_note = self._late_answer_note
        if late_note:
            view.late_answer_note = late_note
        if not self.game_started:
            lobby = [
                "game not started: you are in the lobby — bind names, fish "
                "for lobby facts, and wait for clear start language"
            ]
            if self.promoted_categories:
                # Gated category proposals (F): PROMOTED extras only —
                # unpromoted candidates are never announced.
                lobby.append(
                    "extra categories in tonight's rotation (promoted by "
                    "player demand — you may mention these): "
                    + ", ".join(self.promoted_categories)
                )
            view.lobby = lobby
        return view

    # -- burn protocol (say-gate WO §1) ------------------------------------------

