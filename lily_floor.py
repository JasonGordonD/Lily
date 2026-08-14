"""LilyFloor -- who may speak right now: hold / stop / question-pending / addressee
/ clarify (W3 Cut 4).

INVARIANT: "who holds the floor?" resolves to exactly ONE owner (host, room, or
a pending clarify). Post-W1a this is GameControl's CLIENT, not a rival --
game_control()/may() stay the typed authority in the director; this mixin is the
hold/stop/pending/addressee surface may() reads. Byte-identical MOVE (self.*
unchanged); the FLOOR_*/_HOLD_EXEMPT_SOURCES/_GAME_LANE_ACTS class attrs resolve
via the MRO and stay on LilyGame for this cut."""

from __future__ import annotations

import asyncio
import datetime
import time

import lily_addressee
import lily_addressee_classifier
import lily_config
import lily_evaluation
import lily_nbest
import lily_persistence
import lily_say_gate
import lily_scorekeeper

import logging
logger = logging.getLogger("lily_agent")


class LilyFloorMixin:
    def start_blocked_reason(self) -> str | None:
        """Single choke for kickoff gates. None = start allowed."""
        if self._delivery_stop_sticky:
            return "game_stopped"
        if self.recognition_dispute_blocks_start():
            return "recognition_dispute"
        if self.ambiguous_yes_blocks_start():
            return "ambiguous_yes"
        if (
            self._identity_required_before_start
            and not self._identity_gate_satisfied()
        ):
            return "identity_unconfirmed"
        if self._user_speaking:
            return "user_speaking"
        if self.pending_setup_jobs():
            return "setup_pending"
        return None

    def clear_pending_clarify_for_question(
        self, qnum: int, *, reason: str
    ) -> list[str]:
        """Kill every clarify tied to a question that became terminal."""
        pending_all = getattr(self, "pending_clarify", None) or {}
        cleared: list[str] = []
        for player, pending in list(pending_all.items()):
            pending_qnum = (pending or {}).get("question_number")
            # Legacy/in-flight entries predate question_number; they can only
            # belong to the current question and must die at this boundary.
            if pending_qnum is not None and pending_qnum != qnum:
                continue
            pending_all.pop(player, None)
            cleared.append(player)

        key = f"q_{qnum}_clarify"
        owner = self.say_registry.owner_of(key)
        if self.say_registry.release(key):
            self.cancel_speech(owner, reason="question_answered")
        if cleared or owner:
            logger.warning(
                "LILY_CLARIFY | CLEARED | session=%s q=%d players=%s "
                "reason=%s",
                self.sk.session_id, qnum,
                ",".join(sorted(cleared)) or "-",
                reason,
            )
        return cleared

    def game_payload_blocked(self, act: str, source: str) -> bool:
        """True when this is a game-lane payload dispatched with no live
        game — the "Nobody landed it" lockout that aired into lobby
        conversation. game_started (and not game_over) is the live-game
        gate; a lobby/ended state blocks every game-lane act."""
        if act not in self._GAME_LANE_ACTS:
            return False
        if self._delivery_stop_sticky:
            return True
        return not getattr(self, "game_started", False) or getattr(
            self, "game_over", False
        )

    # -- WO-LILY-FLOOR-001 counterweight (HOTFIX-007 Y10) ------------------
    #
    # ARCHAEOLOGY — what already existed, and why none of it sufficed.
    # Four surfaces already held a PIECE of "whose floor is it":
    #
    #   * SpeechActRegistry claims + _playout_started_ids (lily_say_gate,
    #     lily_speech_delivery) — the substrate for WHICH ACT is airing or
    #     owed. That is act identity, not floor ownership: a PENDING claim
    #     says "this reveal is in flight", never "the room is talking".
    #   * _hold_active / hold_blocks_dispatch (PATCH-002 A4) — a genuine,
    #     already-global YIELD that binds every gated_say lane. But it is
    #     only ever ENTERED by an explicit event: STOP, a player's
    #     decline, her own wait-promise, a timed-out question. Nothing
    #     enters it because the table is simply enjoying itself.
    #   * _question_pending (PATCH-003 P10) — the floor yielded after SHE
    #     asks. Blind to the room talking on its own.
    #   * last_addressee_judgment (FLOOR-001 FL-1) — the ONLY per-utterance
    #     read of player-to-player talk, and until Y10 it was consumed
    #     EXCLUSIVELY as two prompt context lines in build_state_block
    #     ("floor read: ..."). It conditioned her words and gated nothing.
    #     FL-2, the floor-state machine named as FL-1's own downstream
    #     contract (lily_addressee_classifier.py:31), was never built.
    #
    # So the states existed, scattered across four owners, and the one
    # surface that can detect the ROOM holding the floor had no authority
    # over dispatch at all. Meanwhile the push mandate is enforced in code
    # (responsiveness budget, the M1 "silence is her failure mode" gate,
    # the auto-resume watchdog) with no counterweight.
    #
    # Y10 adds NO fifth state and no parallel floor manager. floor_state()
    # is a pure DERIVED READ over the four surfaces above — no new
    # attribute, nothing to keep in sync, nothing to reset — and it is what
    # finally gives the FL-1 judgment authority over a dispatch decision.
    #
    # DELIBERATELY NOT BUILT (see the Y10 commit message): the full graded
    # -response ladder (per-act "full turn vs minimal acknowledgment vs
    # silence" selection on every lane) and a stateful FL-2 machine with
    # transitions of its own. Both would be the second layer the mandate
    # forbids. The minimal counterweight is: one derived read, one push
    # path that can now choose silence, and chain F closed.

    def room_holds_floor(self, now: float | None = None) -> bool:
        """True when the ROOM owns the floor: she asked and they have not
        answered (P10), or FL-1's latest judgment read the last utterance
        as player-to-player talk and that read is still live.

        A HOST_DIRECTED judgment deliberately does NOT hold the floor — the
        canon keeps the responsiveness budget for a direct address ("she
        never has to be told twice"). This yields only where the canon says
        yield: table talk and side clusters.

        KNOWN SLACK (Y10 review F4, accepted): the classifier kills a live
        side-cluster on `note_agent_prompt` (any Lily turn that aired), but
        `last_addressee_judgment` itself is never cleared — so a cluster
        read can still say player_speaking for up to
        cluster_max_gap_seconds after the cluster machine has moved on. Two
        things bound it: the recency window above, and floor_state's
        precedence (her own live audio outranks the room read). The cost is
        confined to this one lane and is at most a few extra seconds of
        silence on an auto-resume; clearing the judgment on note_agent_prompt
        would be a second write path into FL-1 state, which is the layer
        Y10 is under mandate not to add."""
        if self._question_pending:
            return True
        judgment = getattr(self, "last_addressee_judgment", None)
        if judgment is None:
            return False
        cluster = judgment.classification == (
            lily_addressee_classifier.CLASS_SIDE_CLUSTER
        )
        if not cluster and judgment.classification != (
            lily_addressee_classifier.CLASS_SIDE_CHATTER
        ):
            return False
        ref = now if now is not None else time.time()
        age = ref - float(getattr(judgment, "ts", 0.0) or 0.0)
        # Deliberate failure direction: an unusable timestamp (missing, or
        # ahead of the clock) reads as NO room floor, so the lane falls back
        # to its pre-Y10 behaviour and speaks. A restraint counterweight
        # must never be able to fail into a permanent mute.
        return 0.0 <= age <= self._room_talk_recency_seconds(cluster=cluster)

    def floor_state(self) -> str:
        """Whose floor is it — derived, never stored. Precedence is the
        precedence the existing gates already enforce: an explicit hold
        outranks everything (it is the one state that already binds every
        dispatch lane), her own live audio outranks a stale room read, and
        the room's talk outranks the default. FLOOR_OPEN is the residual —
        a genuine lull, which is exactly when the canon says the floor
        comes back to her."""
        if self._hold_active:
            return self.FLOOR_HOLD
        if getattr(getattr(self, "sk", None), "host_speaking", False):
            return self.FLOOR_LILY_SPEAKING
        if self.room_holds_floor():
            return self.FLOOR_PLAYER_SPEAKING
        return self.FLOOR_OPEN

    def progression_paused_reason(self) -> str | None:
        """Why a new question delivery must not take the floor right now."""
        if self._delivery_stop_sticky:
            return "game_stopped"
        if self._hold_active:
            return "hold"
        if self._question_pending:
            return "question_pending"
        if self._awaiting_address_since:
            return "address_unanswered"
        if getattr(self, "_user_speaking", False):
            # D-E (live lily-A9B757 2026-08-13, 04:40:17): a new question must
            # never take the floor while a human is mid-turn — after a timeout
            # verdict the next delivery fired over the operator's active
            # complaint. VAD's _user_speaking self-clears on the falling edge,
            # so the advance resumes the instant they stop (no timer, no dead
            # game). This gates STARTING a delivery only; the barge-in cancel
            # path (Y7) is untouched.
            return "user_speaking"
        if getattr(self.sk, "host_speaking", False):
            return "host_speaking"
        if self.pending_setup_jobs():
            return "setup_pending"
        return None

    def hold_blocks_dispatch(self, act: str, source: str) -> bool:
        """True when the hold state must suppress this dispatch. Exempt
        sources (the STOP ack, hold-release) always pass; everything else
        — conversational turns AND question deliveries — is blocked while
        held."""
        if not self._hold_active:
            return False
        return source not in self._HOLD_EXEMPT_SOURCES

    def enter_hold(self, reason: str) -> None:
        """Bind every dispatch lane: one acknowledgment then yield. A
        player's decline/wait, Lily's own 'take your time', and STOP all
        route here. Idempotent — re-entering while held only refreshes the
        clock."""
        already = self._hold_active
        self._hold_active = True
        self._hold_since = time.time()
        self._hold_reason = reason
        if not already:
            logger.warning(
                "LILY_HOLD | ENTERED | session=%s reason=%s — all dispatch "
                "lanes yield until user release / hard event / timeout",
                self.sk.session_id, reason,
            )

    def release_hold(self, reason: str) -> bool:
        """Lift the hold (user spoke, a hard game event fired, or the
        timeout elapsed). Returns True if a hold was actually lifted."""
        if not self._hold_active:
            return False
        self._hold_active = False
        self._hold_reason = None
        logger.info(
            "LILY_HOLD | RELEASED | session=%s reason=%s",
            self.sk.session_id, reason,
        )
        return True

    def hold_timed_out(self, now: float | None = None) -> bool:
        if not self._hold_active:
            return False
        ref = now if now is not None else time.time()
        return (ref - self._hold_since) >= lily_config.hold_timeout_seconds()

    def game_delivery_stopped(self) -> bool:
        """Persistent STOP latch; conversation may resume, game delivery may not."""
        return bool(self._delivery_stop_sticky)

    def resume_game_delivery(self, *, reason: str) -> bool:
        """Clear sticky STOP after an explicit resume command. CLASS 5
        (LIVEFIRE-001): sticky-clear and hold-release are ONE atomic
        operation (5c), and the state machine — not the LLM — restarts
        delivery. Class 4 preserves the armed card across a STOP, so the
        resume dispatches it deterministically here (5d: the resume
        transition emits the templated delivery; the LLM never announces a
        resume the machine has not executed). If no card is armed, prefetch
        refills the supply and the tick loop delivers when it lands."""
        if not self.game_delivery_stopped():
            return False
        # 5c — atomic: sticky clear + hold release, no window between them.
        self._delivery_stop_sticky = False
        self.release_hold(reason=f"explicit_resume:{reason}")
        self.sk.clear_status_notes()
        logger.warning(
            "LILY_STOP | RESUMED | session=%s reason=%s — game delivery "
            "restarting",
            self.sk.session_id, reason,
        )
        self.publish_attributes_nowait()
        if self.game_started and not self.game_over:
            self.start_prefetch()
            # 5d — restart delivery from the spine. The preserved armed card
            # (Class 4) delivers now via the deterministic sheet; a gated
            # dispatch (open transition, no card yet) journals nothing, so the
            # tick loop's own retry still owns the beat.
            if self.armed_question is not None:
                self.dispatch_armed_question(source="resume")
        return True

    # -- PATCH-003 P6/P10: yield-after-question ------------------------------
    #
    # A conversational question Lily poses opens a question-pending state:
    # she yields the floor (no follow-on content, no second question, no
    # queued beat) until the table answers (a user final releases it) or a
    # generous timeout gives ONE gentle re-offer, then a hold. Composes
    # with P6 — the user's next turn IS the response, engaged first.

    def enter_question_pending(self, question_text: str) -> None:
        already = self._question_pending
        self._question_pending = True
        self._question_pending_since = time.time()
        self._question_pending_reoffered = False
        self._question_pending_text = (question_text or "")[:300]
        if not already:
            logger.info(
                "LILY_ASK | QUESTION_PENDING | session=%s — floor yielded "
                "until the table answers", self.sk.session_id,
            )

    def release_question_pending(self, reason: str) -> bool:
        if not self._question_pending:
            return False
        self._question_pending = False
        logger.info(
            "LILY_ASK | QUESTION_PENDING_RELEASED | session=%s reason=%s",
            self.sk.session_id, reason,
        )
        return True

    def address_unanswered(self, now: float | None = None) -> bool:
        """PATCH-003 P9: True when a direct address has gone unanswered
        past the responsiveness budget. The watchdog WARNs once
        (ADDRESS_UNANSWERED) — the verifiable floor; the grounded holding
        beat ('One sec — checking [the real thing]') is a state-block
        template so any beat she takes names the actual check, never a
        vamp."""
        since = self._awaiting_address_since
        if not since:
            return False
        ref = now if now is not None else time.time()
        return (ref - since) >= lily_config.responsiveness_budget_seconds()

    # -- WO-LILY-NEVER-SILENT-001: the anti-silence floor -------------------
    #
    # A host-directed final is answered, or the room hears dead air — a party
    # host's one unforgivable failure. This is the single counterweight wired
    # at every silence outcome on that path: an empty/fail-closed generation
    # (the empty-STOP guard yields the line), a say-pipeline suppression
    # (tts_node schedules it), and the address-unanswered deadline (the
    # watchdog fires it).
    #
    # `_awaiting_address_since` IS the verify-nothing-aired surface: FL-1 sets
    # it on every host-directed final and note_playout_started clears it the
    # instant real audio starts — so a truthy latch means "a host-directed
    # final is owed a response and none has aired". `sk.host_speaking` is the
    # second surface (her audio live right now). The floor stands down under
    # a STOP/hold/question-pending (operator-ruled or room-owned silences —
    # the same states gated_say already refuses). `_floor_fired_for_ts` keeps
    # it to ONE line per outstanding address: a barge-in that cancels the
    # floor before playout leaves the latch set, so there is no retry storm;
    # a NEW host-directed final mints a new `_awaiting_address_since` and the
    # floor may speak once for that one too.

    def floor_line_owed(self) -> bool:
        """True when a host-directed final is outstanding with nothing on the
        air — the one condition under which the floor speaks. False while a
        STOP/hold/question-pending binds the floor (those are operator-ruled
        or room-owned silences), while her own audio is live, or once the
        floor already fired for this address."""
        since = self._awaiting_address_since
        if not since:
            return False
        if (
            self._delivery_stop_sticky
            or self._hold_active
            or self._question_pending
        ):
            return False
        if getattr(self.sk, "host_speaking", False):
            return False
        if getattr(self, "_floor_fired_for_ts", 0.0) == since:
            return False
        return True

    def _floor_context(self) -> str:
        """"game" mid-game, else "lobby" (the pre-game hail case)."""
        return (
            "game"
            if getattr(self, "game_started", False)
            and not getattr(self, "game_over", False)
            else "lobby"
        )

    def next_floor_line(self) -> str:
        """The next rotated floor line for the current game context; marks
        this outstanding address as floored (one line per address)."""
        nonce = int(getattr(self, "_floor_line_nonce", 0) or 0)
        self._floor_line_nonce = nonce + 1
        self._floor_fired_for_ts = self._awaiting_address_since
        return lily_say_gate.lily_floor_line(self._floor_context(), nonce)

    def floor_line_if_owed(self, reason: str) -> str | None:
        """The synchronous floor. If a host-directed final is owed a response
        and nothing has aired, return the line to speak (marking the address
        floored) and log it; else None. The empty-STOP guard yields this line
        as its generation content instead of failing closed into silence."""
        if not self.floor_line_owed():
            return None
        line = self.next_floor_line()
        logger.warning(
            "LILY_FLOOR | NEVER_SILENT | session=%s reason=%s context=%s "
            "line=%r — host-directed final left silent; airing floor line",
            self.sk.session_id, reason, self._floor_context(), line,
        )
        return line

    def fire_floor_line(self, reason: str) -> bool:
        """The dispatched floor: air the floor line as its own deterministic
        turn — the say-pipeline-suppressed and watchdog paths, where there is
        no live generation to yield into. Rides gated_say(text=...) so every
        hygiene/leak/claim gate and the barge-in cancel path govern it exactly
        like any other line. Returns True when a line was dispatched."""
        line = self.floor_line_if_owed(reason)
        if line is None:
            return False
        return self.gated_say(None, "floor", "", source=reason, text=line)

    def _question_pending_timed_out(self, now: float | None = None) -> bool:
        if not self._question_pending:
            return False
        ref = now if now is not None else time.time()
        return (
            ref - self._question_pending_since
        ) >= lily_config.hold_timeout_seconds()

    def question_pending_blocks_dispatch(self, act: str, source: str) -> bool:
        """While a conversational question is pending, unsolicited beats
        hold. Exempt: the same sources the hold exempts (STOP/hold/release
        acks) plus game-lane acts (their own windows govern them) and the
        pending re-offer itself."""
        if not self._question_pending:
            return False
        if source in self._HOLD_EXEMPT_SOURCES or source == "question_reoffer":
            return False
        if act in self._GAME_LANE_ACTS:
            return False
        return True

    def _freeze_game_delivery_for_stop(self) -> None:
        """Retire every current delivery surface without ending conversation."""
        self._delivery_stop_sticky = True

        timer = self._window_timer
        if timer is not None and not timer.done():
            timer.cancel()
        self._window_timer = None
        self.sk.close_answer_window()
        self.sk.answer_candidates = {}
        self.sk.answer_window_question_id = None
        self.sk.answer_window_question_index = None
        self.clear_pending_clarify_for_question(
            self.sk.question_number, reason="stop_primitive"
        )
        if self._bed_handle is not None:
            self._stop_bed()
        self._steal_window = False

        # CLASS 4 (LIVEFIRE-001) 4b: STOP FREEZES supply; it never burns or
        # consumes armed/supplied content. The live stop burned kb_457 (the
        # next Greece card) and nulled the armed question — so a genuine STOP
        # cost the table its queued content. The armed and prefetched cards
        # now SURVIVE the freeze (the sticky latch blocks delivery until an
        # explicit resume, Class 5); the card is delivered when play resumes.
        # Adult content keeps its consent-driven hard burn
        # (_burn_pending_adult_questions, above) — that is a safety semantic,
        # not the general kb_* retirement this clause removes.
        self.sk.current_question = None

        task = self._prefetch_task
        if task is not None and not task.done():
            task.cancel()
        self._prefetch_task = None
        self._pending_delivery_qnum = None
        self._active_delivery_qnum = None
        self._active_delivery_started_at = None
        self._active_delivery_ended_at = None
        self._mc_delivery_qnum = None
        self._mc_delivery_started_at = None
        # C3c: a resume can never outlive the question that owed it.
        self._delivery_barge_cut_qnum = None
        self._pending_delivery_resume = None
        self._delivery_speech_acts = {}
        self._pre_window_segments = []
        self._recent_finals = []
        self._pending_reveal_event = None
        self._armed_speech_misses = 0
        self._undelivered_ticks = 0
        self._undelivered_refires = 0
        self._supply_stall_ticks = 0
        self._prefetch_stall_ticks = 0
        self._phase_hold = None
        self.sk.clear_status_notes()
        publish_nowait = getattr(self, "publish_attributes_nowait", None)
        if callable(publish_nowait):
            publish_nowait()
        try:
            publish_metadata = getattr(self, "publish_metadata", None)
            if callable(publish_metadata):
                asyncio.get_running_loop().create_task(publish_metadata(""))
        except RuntimeError:
            pass

    def maybe_route_stop(self, text: str) -> bool:
        """W8: the STOP consult on every final. Reads the roster (a bare
        stop counts only in a solo room), detects the runaway-agent brake,
        and routes a detected stop into handle_stop_primitive so the hold
        is entered MECHANICALLY — detection is never left as a value the
        LLM narrates without the state existing. Returns True when a stop
        was handled (the caller returns; the halt bypasses the LLM).

        The live lily-5E3036 leak routed through here: "Stop stop stop…"
        did not fire because a phantom second player made roster_size()
        read 2 (solo=False) and a bare stop counts only in solo. The
        emphatic-repetition register now fires regardless of roster
        (lily_detect_stop), so this consult routes it without depending on
        the roster being clean (W7's territory)."""
        solo = self.sk.roster_size() <= 1
        if lily_scorekeeper.lily_detect_stop(text, solo=solo):
            self.handle_stop_primitive(text)
            return True
        # HOSTLOOP-001 C13: the softer equivalents ("hold on", "wait",
        # "pause", "one sec") halt within the SAME utterance, through the
        # same deterministic consult — but as a HOLD, not the sticky STOP:
        # no content retirement, no explicit-resume requirement; the
        # existing hold release paths (player speaks on, timeout) apply.
        # Utterance-shaped only — "wait, is it Saturn?!" is an answer and
        # never fires (lily_detect_hold_request).
        if lily_scorekeeper.lily_detect_hold_request(text):
            self.handle_hold_request(text)
            return True
        return False

    def handle_hold_request(self, source_text: str) -> None:
        """C13: a spoken hold-equivalent binds within one utterance —
        interrupt anything airing, enter the hold (every dispatch lane
        yields, W2/V4/Y10 gates), one short acknowledgment. The softer
        sibling of handle_stop_primitive: no sticky latch, no claim
        release, no content retirement — the table asked for a beat, not
        a brake."""
        already_held = self._hold_active
        logger.warning(
            "LILY_HOLD | REQUESTED | session=%s text=%r already_held=%s — "
            "player hold-equivalent; yielding within this utterance (C13)",
            self.sk.session_id, (source_text or "")[:60], already_held,
        )
        session = getattr(self, "session", None)
        interrupt = getattr(session, "interrupt", None)
        if callable(interrupt):
            try:
                interrupt()
            except Exception as e:
                logger.warning("LILY_HOLD | session interrupt failed: %s", e)
        self.enter_hold(reason="player_hold_request")
        if already_held:
            return  # one acknowledgment per hold, whichever lane got there
        self.gated_say(
            None,
            "hold_ack",
            "The table asked for a moment — committed, in code: you are "
            "holding. ONE short warm acknowledgment (a few words, e.g. "
            "'Take your time.') and then silence until they come back.",
            source="hold_request",
            # REFACTOR W2a: the hold ack is a DETERMINISTIC sheet (direct_say)
            # — one warm line, then silence until they return.
            text="Take your time.",
        )

    def handle_stop_primitive(self, source_text: str) -> None:
        """A5/T12: the dispatch-gate STOP reflex — the runaway-agent
        brake, called BEFORE the LLM ever sees the turn. Halt playout,
        cancel every queued/in-flight dispatch for the turn (no re-fire,
        no watchdog resurrection), enter the hold, one brief
        acknowledgment, then yield."""
        already_stopped = self.game_delivery_stopped()
        # HOTFIX-010 V4 item 2: another lane may already have reached this
        # stop and aired its acknowledgment. The narration lane — an outbound
        # turn that narrated a stopped state, backed by back_hold_narration —
        # enters a `narrated_stop` hold WITHOUT setting the sticky latch, so
        # the mechanical brake's old idempotency read (game_delivery_stopped,
        # sticky only) missed it and added a SECOND acknowledgment. That is
        # the live double-ack: two lanes each discovering the stop and each
        # acking. Read the SHARED hold state (no new flag) so whichever lane
        # reached the stop first owns the single acknowledgment.
        already_acked = already_stopped or (
            self._hold_active
            and self._hold_reason == "narrated_stop"
        )
        logger.warning(
            "LILY_STOP | PRIMITIVE | session=%s text=%r — halting playout, "
            "cancelling dispatches, entering hold sticky=%s already_acked=%s",
            self.sk.session_id, (source_text or "")[:60], already_stopped,
            already_acked,
        )
        # 1. Halt anything airing + cancel every tracked handle.
        for speech_id in list(self._speech_handles):
            self.cancel_speech(speech_id, reason="stop_primitive")
        # 2. Kill the delivery watchdog's ability to resurrect the turn.
        released = self.say_registry.release_pending()
        if released:
            logger.info(
                "LILY_STOP | CLAIMS_RELEASED | keys=%s", ",".join(sorted(released))
            )
        self._armed_speech_misses = 0
        self._undelivered_ticks = 0
        # P0-B: every STOP freezes every future delivery owner until explicit
        # resume. Armed/prefetched content SURVIVES the freeze and delivers on
        # resume (LIVEFIRE-001 CLASS 4b freeze-not-burn) — the content-mode
        # gate's adult hard-burn was removed with the gate (no revert deck to
        # protect against).
        self._freeze_game_delivery_for_stop()
        # 3. Interrupt the live session speech if the framework holds one.
        session = getattr(self, "session", None)
        interrupt = getattr(session, "interrupt", None)
        if callable(interrupt):
            try:
                interrupt()
            except Exception as e:
                logger.warning("LILY_STOP | session interrupt failed: %s", e)
        # 4. Enter the hold and speak ONE short acknowledgment.
        self.enter_hold(reason="stop_primitive")
        if already_acked:
            logger.info(
                "LILY_STOP | REASSERTED | session=%s — stop already "
                "acknowledged (sticky=%s narrated_stop_hold=%s); no second "
                "acknowledgment",
                self.sk.session_id, already_stopped,
                already_acked and not already_stopped,
            )
            return
        self.gated_say(
            None,
            "stop_ack",
            # RULINGS-001 R1: ratified STOP-ack register anchor.
            "The player just told you to stop. Stop immediately and say "
            "nothing more than a brief acknowledgment. REGISTER GUIDANCE "
            "(vary freely within this length and temperature, never "
            "longer): one or two calm words — 'Stopped.' / 'Say the "
            "word.' No question, no recap, no next move — wait for them.",
            source="stop_primitive",
            # REFACTOR W2a: the STOP ack is a DETERMINISTIC sheet (direct_say),
            # not the 8-13s composite — one calm line, no question, no recap.
            text="Stopped. Say the word when you're ready.",
        )

    # -- PATCH-002 M4: no orphan stems (RETIRE_WITH_WS6: 004's journal
    # makes completion|cancellation a reducer invariant) ------------------

    def classify_addressee(
        self,
        result: dict,
        text: str,
        *,
        speaker_label: str | None = None,
        segment_ts: float,
        nbest: dict | None = None,
    ) -> "lily_addressee_classifier.LilyAddresseeJudgment":
        """Per-utterance addressee judgment, computed in the transcript
        layer BEFORE any reply generation (the reply's context blocks
        condition on it via build_state_block). Fuses deterministic game
        priors, name evidence, and the acoustic register; the structured
        result lands on last_addressee_judgment for FL-2/FL-4 and in the
        lily_addressee_log row for this utterance."""
        classifier = self.addressee_classifier
        if classifier is None:
            classifier = lily_addressee_classifier.LilyAddresseeClassifier()
            self.addressee_classifier = classifier

        # Expectation-primed match on the ACTIVE registered question: the
        # scorekeeper recorded it as an answer candidate, or Tier-1 fuzzy
        # lands "correct" against the live acceptable answers.
        question = self.sk.current_question or {}
        acceptable = question.get("acceptable_answers") or []
        expectation = bool(result.get("candidate_recorded"))
        if not expectation and acceptable:
            try:
                hyps = (nbest or {}).get("hypotheses") or []
                if nbest is not None and len(hyps) > 1:
                    expectation = (
                        lily_evaluation.lily_tier1_evaluate_nbest(
                            text, {"acceptable_answers": acceptable},
                            hypotheses=hyps,
                        )["verdict"] == "correct"
                    )
                else:
                    expectation = (
                        lily_evaluation.lily_tier1_evaluate(text, acceptable)[
                            "verdict"
                        ] == "correct"
                    )
            except Exception:
                expectation = False

        window_open = self.sk.is_window_open(now=segment_ts)
        delivery_key = f"q_{self.sk.question_number}_delivery"
        delivery_in_flight = (
            self._active_delivery_qnum
            == self.sk.question_number
            or self.say_registry.state(delivery_key) is not None
        )
        if getattr(self, "ui_phase", None) == "lobby":
            phase = "lobby"
        elif window_open or (
            getattr(self, "ui_phase", None) == "question"
            and expectation
            and delivery_in_flight
        ):
            phase = "question"
        elif self.sk.phase == "wrapup":
            phase = "wrapup"
        else:
            # Between questions / supply stall: the gap belongs to the
            # table — the classifier default flips toward side chatter.
            phase = "idle"

        # Acoustic register: WS-11/WS-13 features via the narrow snapshot
        # adapter, only when the capture clock is aligned with this
        # segment (same alignment contract as confidence fusion).
        register = None
        try:
            inputs = self.acoustic.addressee_fusion_inputs()
            if not inputs.get("breaker_open") and (
                lily_addressee.lily_acoustic_sample_aligned(
                    segment_ts,
                    inputs.get("captured_at"),
                    max_staleness_seconds=(
                        lily_config.addressee_acoustic_max_staleness_seconds()
                    ),
                    max_future_seconds=(
                        lily_config.addressee_acoustic_max_future_seconds()
                    ),
                )
            ):
                register = (
                    lily_addressee_classifier.LilyAcousticRegister
                    .from_snapshot(self.acoustic.addressee_snapshot())
                )
        except Exception:
            register = None

        judgment = classifier.classify(
            lily_addressee_classifier.LilyUtteranceSignals(
                text=text,
                speaker_label=speaker_label,
                ts=segment_ts,
                window_open=window_open,
                expectation_match=expectation,
                phase=phase,
                command_shaped=bool(
                    result.get("control_command")
                    or result.get("system_directed")
                ),
                register=register,
            )
        )
        self.last_addressee_judgment = judgment
        # PATCH-003 P9: a host-directed final IS a direct address — start
        # the responsiveness clock. Cleared only when real response playout
        # starts; the watchdog WARNs if dispatch queued but never reached air
        # past the budget (the "Hey... Hello?" -> 34s silence fixture).
        if judgment.classification == (
            lily_addressee_classifier.CLASS_HOST_DIRECTED
        ):
            self._awaiting_address_since = time.time()
        logger.info(
            "LILY_ADDRESSEE | CLASSIFIED | session=%s %s",
            self.sk.session_id, judgment.log_json(),
        )
        return judgment

    # -- addressee-label corpus (B1) ----------------------------------------------

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
        judgment: "lily_addressee_classifier.LilyAddresseeJudgment | None" = None,
        timing_source: str | None = None,
        timing_drift_seconds: float | None = None,
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
        # FL-1: the per-utterance addressee judgment (migration 018) —
        # agent_classification and the fused-score/side-cluster trail. FL-1
        # is the SINGLE writer of agent_classification. Clarify rows re-log
        # an EARLIER utterance and pass no judgment — their columns stay
        # NULL; the utterance's primary row carries it.
        if judgment is not None:
            row.update(judgment.row_fields())
        # WS-11 (migration 019 columns): the STT stream-clock reconciler's
        # source/drift audit trail — the overlap side's provenance. Set only
        # when known so a pre-019 environment's fail-soft retry strips a
        # minimal, contiguous set. Distinct from FL-1's columns.
        if timing_source is not None:
            row["timing_source"] = timing_source
        if timing_drift_seconds is not None:
            row["timing_drift_seconds"] = timing_drift_seconds
        return row

    def _log_addressee_segment(
        self,
        result: dict,
        text: str,
        speaker_label: str | None,
        segment_ts: float,
        nbest: dict | None = None,
        judgment: "lily_addressee_classifier.LilyAddresseeJudgment | None" = None,
    ) -> None:
        """B1 corpus + FL-1 runtime record: one insert per finalized
        segment — every utterance carries its addressee judgment
        (agent_classification is never null again; the 81BCB0 root
        condition was every heard utterance defaulting to host-directed
        with a null classification). All writes fire-and-forget via
        asyncio.to_thread inside the persistence helper — zero hot-path
        cost. Clarified rows are inserted by mark_pending_clarify."""
        if self.supabase is None:
            return
        if result.get("candidate_recorded"):
            action = lily_addressee.AGENT_ACTION_SCORED
        elif result.get("control_command"):
            # Skipped-on / mode-revert segments: acted on, not scored.
            action = lily_addressee.AGENT_ACTION_ADJUDICATED_OTHER
        else:
            action = lily_addressee.AGENT_ACTION_IGNORED
        # WS-11: STT-timing telemetry off the reconciler (migration 019
        # columns). FL-1 owns agent_classification via `judgment`; WS-11
        # only adds the overlap-side timing provenance. lily_log_addressee
        # strips these keys and retries if the DDL is not yet applied (no
        # corpus row is lost).
        timing = (nbest or {}).get("segment_timing") or {}
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
            judgment=judgment,
            timing_source=timing.get("source"),
            timing_drift_seconds=timing.get("drift_seconds"),
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
        if self.question_is_terminal(qnum):
            logger.warning(
                "LILY_CLARIFY | SUPPRESSED | session=%s q=%d player=%s "
                "reason=question_terminal",
                self.sk.session_id, qnum, player,
            )
            return
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
        if not self.mark_pending_clarify(player):
            return
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

    def _maybe_fire_confidence_clarify(
        self,
        result: dict,
        speaker_label: str | None,
        segment_ts: float,
        nbest: dict | None,
    ) -> bool:
        """WS-11 — garble gate. A final whose per-word confidences were low
        across a multi-word utterance (tap-only mode synthesizes ONE
        hypothesis, so the dispersion gate stays 0.000 and the Tier-1 path
        engages the content anyway — the live "Ninja girl, 5050 first
        dates" case) fires the SAME light clarify posture as the Task-4
        band trigger instead of scoring garble as an answer.

        Shares the per-question / per-session caps and the pending-clarify
        machinery with _maybe_fire_clarify. Rostered players only (the
        clarify addresses someone by name). Honest within the synthesized
        set's limits: it widens recall, it is not a true lattice, so this
        keys on the recognizer's own word confidences, nothing invented."""
        if not lily_nbest.lily_nbest_garbled(
            nbest,
            min_mean_confidence=lily_config.garble_clarify_min_confidence(),
        ):
            return False
        player = result.get("player")
        if not player:
            return False
        if not self.sk.is_window_open(now=segment_ts):
            return False
        if not hasattr(self, "_clarify_fired_questions"):
            self._clarify_fired_questions = set()
            self._session_clarify_count = 0
        qnum = self.sk.question_number
        if self.question_is_terminal(qnum):
            logger.warning(
                "LILY_CLARIFY | SUPPRESSED | session=%s q=%d player=%s "
                "reason=question_terminal",
                self.sk.session_id, qnum, player,
            )
            return False
        if qnum in self._clarify_fired_questions:
            return False
        if self._session_clarify_count >= lily_config.clarify_max_per_session():
            return False
        if player in self.pending_clarify:
            return
        self._clarify_fired_questions.add(qnum)
        self._session_clarify_count += 1
        logger.info(
            "LILY_CLARIFY | GARBLE_TRIGGER | session=%s q=%d player=%s "
            "mean_word_conf=%s words=%d count=%d",
            self.sk.session_id, qnum, player,
            nbest.get("mean_word_confidence"), nbest.get("word_count", 0),
            self._session_clarify_count,
        )
        if not self.mark_pending_clarify(player):
            return False
        self.gated_say(
            f"q_{qnum}_clarify",
            "clarify_question",
            (
                f"You caught the shape of {player}'s answer but not cleanly. "
                "Ask them to run it back once, by name, quick and warm: "
                f"'{player}, say that one more time for me?' One short line, "
                "nothing else; their repeat settles it. Stay purely a host "
                "who wants to get their answer right — light, in-character, "
                "and leave it at the ask."
            ),
            source="clarify_garble",
        )
        return True

    def mark_pending_clarify(self, player_name: str) -> bool:
        """The clarify moment (lily_log_clarify tool): mark the player
        pending-clarify, emit the `clarify` packet, and log the clarified
        utterance row with agent_action=clarified. The player's NEXT
        finalized segment resolves the label (explicit ground truth)."""
        qnum = self.sk.question_number
        if self.question_is_terminal(qnum):
            logger.warning(
                "LILY_CLARIFY | SUPPRESSED | session=%s q=%d player=%s "
                "reason=question_terminal",
                self.sk.session_id, qnum, player_name,
            )
            return False
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
        self.pending_clarify[player_name] = {
            "row_task": row_task,
            "question_number": qnum,
        }
        self.send_event_nowait("clarify", {"name": player_name})
        logger.info(
            "LILY_CLARIFY | PENDING | session=%s player=%s utterance=%r",
            self.sk.session_id, player_name, (clarified_text or "")[:80],
        )
        return True

    def _resolve_clarify(self, player_name: str, reply_text: str) -> None:
        """The clarified player's next finalized segment: parse the reply
        (pure, lily_addressee) and UPDATE the clarified row's label with
        label_source=explicit_clarify, then clear pending."""
        pending = self.pending_clarify.pop(player_name, None)
        if pending is None:
            return
        pending_qnum = pending.get("question_number")
        if pending_qnum is not None and self.question_is_terminal(pending_qnum):
            logger.warning(
                "LILY_CLARIFY | STALE_REPLY_IGNORED | session=%s q=%d "
                "player=%s reply=%r",
                self.sk.session_id, pending_qnum, player_name,
                reply_text[:80],
            )
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

