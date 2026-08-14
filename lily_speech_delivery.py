"""lily_speech_delivery.py — speech dispatch + delivery claim surfaces.

Extracted from lily_agent.LilyGame with zero string edits (voice inventory
freeze). LilyGame inherits LilySpeechDeliveryMixin so gated_say /
register_delivery_claim / expect_delivery remain the single choke points
on the game object.

Owns: gated_say + stale-claim watchdog, re-air / cut recovery,
expect_delivery / register_delivery_claim, MC abort + pre-window + early
answer. Does not own director pipelines, supply, or identity.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid

import lily_config
import lily_evaluation
import lily_say_gate
# C4: control-command detection lives in lily_scorekeeper (which imports
# lily_evaluation, never this module — no cycle). The pre-window buffer needs
# the same command exclusion the in-window candidate recorder already applies.
import lily_scorekeeper

logger = logging.getLogger("lily.agent")

_REGEN_REAIR_DIRECTIVE = (
    "\n\nYou were cut short before finishing that line. Say it again in "
    "fresh, shorter words — lead with the key result (the verdict, the "
    "answer, the point), keep it to one crisp beat, and choose new phrasing "
    "rather than your earlier wording."
)
_REGEN_DELIVERY_DIRECTIVE = (
    "\n\nYou were cut short mid-question and the table still does not have "
    "it. Pick up from where you broke off rather than starting the whole "
    "thing again — the part they already heard does not need saying twice. "
    "Get the question sentence itself out, and the options if there are "
    "any, then stop."
)

# Cut-recovery directive (WO-LILY-STREAM-INTEGRITY-002 WS-3). Rides the
# AUTO-resume that fires when a cut/failed organic turn left dead air with
# no natural follow-up. Her honest "my line cut off" voice, made automatic:
# a brief acknowledgement, then finish the thought FRESH from where meaning
# broke, and hand the floor back. Fresh phrasing (not a verbatim replay) is
# also enforced structurally by the re-air gate this dispatch consumes.
_CUT_RECOVERY_DIRECTIVE = (
    "\n\nYour last line cut off before you finished it, and the room's gone "
    "quiet waiting on you. Pick it straight back up in your own voice: a "
    "quick, warm 'sorry, looks like I cut out there' beat, then finish the "
    "point you were making — lead with the part that matters, keep it to one "
    "crisp beat in fresh words rather than repeating your earlier phrasing, "
    "and hand the floor back."
)
# A user turn within this many seconds of a cut is treated as the barge
# that caused it (or the room re-engaging): the normal reply path owns the
# recovery, so the auto-resume watchdog stands down.
_CUT_RECOVERY_USER_TURN_LOOKBACK = 2.0

# ---------------------------------------------------------------------------
# Stale-claim recovery (WO-LILY-HOTFIX-001).
#
# The 08-06 wedge: Krisp NC hung RoomIO audio setup, so the greet's
# dispatched speech never reached playout AND never failed — no confirm,
# no release, claim frozen PENDING. The entrypoint's belt-and-braces
# retry was then dup-suppressed against that frozen claim: permanent
# silence, exactly M1's failure mode. Dup suppression is only legitimate
# against an act that actually PLAYED within the session (CLAIM_CONFIRMED)
# or is genuinely in flight; a keyed dispatch therefore arms a watchdog
# that releases-and-retries a claim whose speech never started airing
# inside the deadline. Deadline sits far above healthy dispatch→playout
# latency (LLM first token + TTS first frame, seconds at p99) and far
# below the observed wedge; a long turn MID-playout is protected twice —
# by the playout-started ledger and by the host_speaking recheck.
# ---------------------------------------------------------------------------

# How many times one question may be re-read after being cut off before it
# goes back to supply. Live `lily-2C489B` read the same question three
# times — 22:49:37, 22:49:47, 22:50:05 — each cut on the same word, because
# the player was interrupting to say the picture wasn't on screen. The
# re-air path re-armed on every interrupt with no cap. Two is a real second
# chance; a fourth read of a question the room keeps talking over is the
# host losing an argument with the table.
_DELIVERY_MAX_CUT_REAIRS = 2

_STALE_CLAIM_SECONDS = 12.0
_STALE_CLAIM_MAX_RETRIES = 2   # re-dispatches per key before declaring the audio path down
_STALE_CLAIM_MAX_RECHECKS = 20  # bounded host_speaking re-check loop (no task leak)
# BARGE-RESILIENCE-001 P2 (R3): how many grace periods the barge-to-ask resume
# may defer while she is answering the interjected question, before giving up
# (bounded so the watchdog can never leak). Comfortably longer than any single
# "what are the rules?" answer.
_BARGE_RESUME_MAX_DEFERRALS = 20
_PROGRESSION_ACTS = ("question_delivery", "question_nudge")

# ---------------------------------------------------------------------------
# HOSTLOOP-001 C5 — EMISSION DISCIPLINE (single-flight on the progression
# loop).
#
# ARCHAEOLOGY. Four mechanisms already serialize parts of this, and the
# hole is what none of them covers:
#
#   * lily_say_gate.SpeechActRegistry + gated_say's claim (say-gate WO §1)
#     — idempotency PER KEY. Two dispatches of q_7_reveal cannot both
#     speak. But the progression loop's own beats are largely KEYLESS
#     (gated_say(None, ...) for question_nudge, steal_window, skip, the
#     pace/media acks, cut_recovery) or carry DIFFERENT keys for the same
#     beat (q_N_verdict vs q_N_reveal at a round boundary), and a keyless
#     dispatch claims nothing at all — so two of them race freely.
#   * N12's transition journal (open_question_transition /
#     journal_transition / register_transition_narration /
#     _transition_holds_next_delivery, HOTFIX-006) — one narration per
#     transition, and N+1 cannot overtake an airing verdict. It is the
#     right shape, but it only exists from the reveal onward: the journal
#     is opened inside adjudicate, register_transition_narration returns
#     None until the verdict stage is journaled, and it governs NARRATION
#     TEXT rather than dispatch. It does not cover the ack class, and it
#     covers nothing before a transition is open.
#   * the framework's speech scheduling queue (agent_activity
#     _schedule_speech, SPEECH_PRIORITY_NORMAL) — serializes PLAYOUT, not
#     generation. Two composites generated concurrently both reach the air,
#     back to back, each written against state the other has already
#     changed: Session A's 04:52:50 / 04:52:54 double emission.
#   * air_dup_guard / lily_repeat_flag / the WS-3 regen gate — catch the
#     SAME WORDS twice, never two differently-worded composites of one beat.
#
# THE HOLE, therefore: nothing refuses a second host composite while a
# first is in flight when the two are keyless or differently keyed. This
# closes exactly that, in the mechanism that already owns dispatch (the
# gated_say choke point + the registry's own claim lifecycle) — no
# scheduler, no queue, no second gate implementation.
#
# THE RULE (three cases, no others):
#   1. flight belongs to an EARLIER question  -> PREEMPT. The table has
#      lapped the queue; the stale composite is cancelled through the
#      existing T1 cancel_speech path and the new one speaks current state.
#   2. same question, SAME act               -> REFUSE. That beat's
#      composite is already in flight; this is the keyless sibling of the
#      registry's dup suppression.
#   3. same question, different act          -> ALLOW. This is the
#      DESIGNED staged pair (T4's verdict beat then its flourish/standings;
#      the reveal then N+1) and the framework's queue plus N12's journal
#      already order it.
#
# Failure direction is SPEAK: a flight whose claim was released, or whose
# playout never started inside the stale-claim deadline, is dead
# bookkeeping and is cleared — because silence is her failure mode and no
# in-flight token may be allowed to mute the loop (WO-LILY-HOTFIX-001's
# whole lesson, applied to this state too).
# ---------------------------------------------------------------------------

_HOST_COMPOSITE_ACTS = frozenset({
    # the question transition's own beats
    "question_delivery", "question_nudge", "question_reoffer",
    "verdict", "reveal", "reveal_flourish", "reveal_scores",
    "reveal_finale", "steal_window", "skip", "game_start",
})

# HOSTLOOP-001 C6: how many instant receipts one question may air. A table
# where four people answer gets four receipts; a stuck recognizer emitting
# finals in a loop does not get a machine gun.
_ANSWER_RECEIPT_MAX_PER_QUESTION = 4

# HOSTLOOP-001 C8: the verdict beat's own claim keys, as minted by the
# reveal path (q_{N}_verdict at a round/final boundary, q_{N}_reveal
# otherwise). The question number is read OUT of the key because by the time
# a verdict's playout ends, sk.question_number already names N+1.
_VERDICT_KEY_RE = re.compile(r"^q_(\d+)_(?:verdict|reveal)$")



class LilySpeechDeliveryMixin:
    """Mixin: speech/delivery methods for LilyGame."""

    def gated_say(
        self,
        key: str | None,
        act: str,
        instructions: str,
        source: str,
        extra_keys: tuple[str, ...] = (),
        text: str | None = None,
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
        reveal's dispatch) without gating it.

        HOSTLOOP-001 C6: `text` selects the DETERMINISTIC lane. With text
        given, the words are already decided and go to the synthesizer as
        written (direct_say / AgentSession.say) instead of through
        generate_reply — which is what makes a sub-2s receipt possible at
        all, since the 8–13s in the evidence is the LLM composite. It is
        the SAME funnel otherwise: every gate above and below runs, the
        claim happens here as usual, the stale-claim watchdog arms as
        usual, and tts_node's hygiene/leak/claim gates still see the turn
        (AgentSession.say routes through Agent.tts_node — verified in
        agent_activity._tts_task_impl's perform_tts_inference call). No new
        bypass: chain F stays closed."""
        # REFACTOR WAVE 1a: shadow the typed GameControl gate next to the
        # latch gates below. may(act) is consulted and compared; the latches
        # stay authoritative this wave. Only the state reasons may() models
        # (hold / game_stopped / no_live_game) are compared.
        if hasattr(self, "_gamecontrol_parity"):
            if self.hold_blocks_dispatch(act, source):
                _legacy_reason = "hold"
            elif self.game_payload_blocked(act, source):
                _legacy_reason = (
                    "game_stopped"
                    if self._delivery_stop_sticky
                    else "no_live_game"
                )
            else:
                _legacy_reason = None
            self._gamecontrol_parity(act, source, _legacy_reason, "gated_say")
        # PATCH-002 A4: the hold state binds every dispatch lane. While
        # held (a decline/wait/STOP), NO unsolicited conversational turn
        # and NO question delivery airs until the hold releases. The
        # release beats and the STOP acknowledgment are the sole exempt
        # sources (they carry hold_exempt via the allowlist below).
        if self.hold_blocks_dispatch(act, source):
            logger.warning(
                "LILY_SAY_SUPPRESSED | reason=hold | act=%s | source=%s",
                act, source,
            )
            return False
        # PATCH-003 P10: a pending conversational question yields the floor
        # — no follow-on beat until the table answers or the re-offer.
        if self.question_pending_blocks_dispatch(act, source):
            logger.warning(
                "LILY_SAY_SUPPRESSED | reason=question_pending | act=%s | "
                "source=%s", act, source,
            )
            return False
        if act in _PROGRESSION_ACTS:
            # P0-G, scoped (2026-08-09): of progression_paused_reason's
            # states, exactly the META ones pause DISPATCH here — an
            # unresolved direct address (the state P0-G's own fixtures
            # assert) and pending setup/ownership work. The rest are
            # already governed at this chokepoint by older, deliberately
            # narrower gates — hold (A4 above), question-pending with its
            # game-lane exemption (P10 above), stop and no-live-game (P8
            # below). The unfiltered check re-blocked the game-lane acts
            # P10 exempts: question_pending refused the very delivery
            # nudge that resolves it, and host_speaking refused the nudge
            # that fires as her own turn ends (both caught by the
            # desync/adult fixtures the original P0-G left red).
            paused = (
                "address_unanswered"
                if self._awaiting_address_since
                else "setup_pending" if self.pending_setup_jobs() else None
            )
            if paused:
                logger.info(
                    "LILY_PROGRESSION | DISPATCH_PAUSED | session=%s q=%d "
                    "act=%s source=%s reason=%s",
                    self.sk.session_id, self.sk.question_number,
                    act, source, paused,
                )
                return False
        # PATCH-003 P8: a game-lane payload (delivery, verdict, reveal,
        # steal/lockout, scores) requires a LIVE game state — the "Nobody
        # landed it" lockout aired into lobby conversation with no
        # question live. Validate at dispatch, the same chokepoint as the
        # hold gate.
        if self.game_payload_blocked(act, source):
            reason = (
                "game_stopped"
                if self._delivery_stop_sticky
                else "no_live_game"
            )
            logger.warning(
                "LILY_SAY_SUPPRESSED | reason=%s | act=%s | "
                "source=%s | game_started=%s q=%d window=%s",
                reason, act, source, getattr(self, "game_started", False),
                self.sk.question_number, self.sk.answer_window_open,
            )
            return False
        # HOSTLOOP-001 C5: exactly ONE host composite in flight. Runs after
        # the refusal gates above (a composite that is not allowed to speak
        # must not take the flight token either) and before the claim, so a
        # preemption cancels the stale speech before this one is recorded.
        if self.composite_flight_blocks_dispatch(act, source):
            return False
        reservation = f"dispatch_{uuid.uuid4().hex}"
        if key is not None and not self.say_registry.claim(key, owner=reservation):
            # WO-LILY-HOTFIX-001: dup suppression is only legitimate against
            # an act that actually played (CONFIRMED) or is genuinely in
            # flight. A STALE pending claim — dispatched long ago, playout
            # never started, nothing airing now — is a wedged speech, and
            # this retry is the recovery: supersede it and speak.
            if self._supersede_stale_claim(key) and self.say_registry.claim(
                key, owner=reservation
            ):
                logger.warning(
                    "LILY_SAY | STALE_CLAIM_SUPERSEDED | key=%s | act=%s | "
                    "source=%s — prior dispatch never reached playout; "
                    "retry speaks", key, act, source,
                )
            else:
                logger.warning(
                    "LILY_SAY_SUPPRESSED | reason=dup | key=%s | act=%s | "
                    "source=%s | state=%s",
                    key, act, source, self.say_registry.state(key),
                )
                return False
        for k in extra_keys:
            self.say_registry.claim(k, owner=reservation)
        # Regeneration gate (WS-3): a re-air (this act's prior airing was
        # cut/suppressed) carries a regeneration directive so the retry is
        # spoken fresh, never replayed. Question deliveries keep their exact
        # wording and take the clean-delivery variant. Consumed only once
        # the claim survived above — a dup dispatch never eats the arm.
        if text is None:
            if self.take_reair_dispatch():
                if act in ("question_delivery", "question_nudge"):
                    instructions = instructions + _REGEN_DELIVERY_DIRECTIVE
                else:
                    instructions = instructions + _REGEN_REAIR_DIRECTIVE
        elif self._reair_gate_armed:
            # A DETERMINISTIC line is fresh by construction and must reach
            # the air verbatim, so it neither needs the regeneration
            # directive nor may hand tts_node's regen GATE a turn it would
            # suppress. The arm is CLEARED rather than consumed (the Y10 F3
            # discipline): consuming it would set _reair_turn_pending, and
            # leaving it live would leak the "you were cut short" directive
            # to an unrelated later dispatch.
            self._reair_gate_armed = False
        logger.info(
            "LILY_SAY | act=%s | key=%s | source=%s | lane=%s",
            act, key or "-", source, "text" if text is not None else "llm",
        )
        handle = (
            self.direct_say(text) if text is not None
            else self.instructed_reply(instructions)
        )
        speech_id = getattr(handle, "id", None)
        if speech_id:
            self.say_registry.reassign_owner(reservation, speech_id)
            if act in (
                "question_delivery",
                "question_nudge",
                "game_start",
                "skip",
            ):
                delivery_acts = self._delivery_speech_acts
                if delivery_acts is None:
                    delivery_acts = self._delivery_speech_acts = {}
                delivery_acts[speech_id] = act
        # C5: this dispatch now OWNS the composite lane (recorded after the
        # claim survived, so a dup-suppressed dispatch never takes it).
        self._note_composite_flight(act, speech_id or reservation, key=key)
        # WO-LILY-HOTFIX-001: every keyed act gets a playout watchdog. If
        # this claim is still PENDING past the deadline with its speech
        # never having started airing (the Krisp RoomIO wedge: no playout,
        # no failure event, no confirm, no release), the watchdog releases
        # the claim and re-dispatches — silence is her failure mode and a
        # frozen claim must never enforce it.
        if key is not None:
            self._arm_stale_claim_watchdog(
                key, act, instructions, source, text=text
            )
        return True

    # -- HOSTLOOP-001 C5: the composite flight token ------------------------

    def _composite_flight(self) -> dict | None:
        """The live host-composite flight, or None. Self-cleaning in both
        directions the say gate already defines a dead dispatch: a RELEASED
        claim, and a dispatch whose speech never reached playout inside the
        stale-claim deadline. No wedged token can mute the progression loop."""
        flight = self._composite_flight_state
        if flight is None:
            return None
        owner = flight.get("owner")
        key = flight.get("key")
        if key is not None and self.say_registry.state(key) is None:
            # Its claim was RELEASED — by the swallowed-turn path, the regen
            # gate, a STOP, or the stale-claim watchdog. A released claim is
            # a dead dispatch by the say gate's own lifecycle, so the lane is
            # free and the legitimate redelivery is not refused as a race.
            self._composite_flight_state = None
            return None
        started = owner in self._playout_started_ids
        if not started and (
            time.monotonic() - float(flight.get("at") or 0.0)
        ) >= _STALE_CLAIM_SECONDS:
            logger.warning(
                "LILY_COMPOSITE | FLIGHT_STALE_CLEARED | session=%s act=%s "
                "owner=%s — dispatched but never reached playout; the lane is "
                "free (a flight token may never enforce silence)",
                self.sk.session_id, flight.get("act"), owner,
            )
            self._composite_flight_state = None
            return None
        return flight

    def composite_flight_blocks_dispatch(self, act: str, source: str) -> bool:
        """C5's whole decision. True = this composite must NOT dispatch
        (its beat already has one in flight). A flight belonging to an
        earlier question is PREEMPTED here — cancelled through the existing
        T1 cancel_speech path — and this dispatch proceeds."""
        if act not in _HOST_COMPOSITE_ACTS:
            return False
        flight = self._composite_flight()
        if flight is None:
            return False
        qnum = self.sk.question_number
        if flight.get("qnum") != qnum:
            logger.warning(
                "LILY_COMPOSITE | PREEMPTED | session=%s stale_act=%s "
                "stale_q=%s new_act=%s q=%d source=%s — the table lapped the "
                "queue; dropping the stale composite and emitting current "
                "state (C5)",
                self.sk.session_id, flight.get("act"), flight.get("qnum"),
                act, qnum, source,
            )
            self.cancel_speech(
                flight.get("owner"), reason="composite_preempted"
            )
            self._composite_flight_state = None
            return False
        if flight.get("act") == act:
            logger.warning(
                "LILY_SAY_SUPPRESSED | reason=composite_in_flight | act=%s | "
                "source=%s | q=%d | owner=%s — this beat's composite is "
                "already in flight; a second one would race it (C5)",
                act, source, qnum, flight.get("owner"),
            )
            return True
        # Same question, a DIFFERENT beat of the same transition (T4's
        # verdict then its flourish, a reveal then N+1): the designed
        # sequence. The framework's speech queue and N12's journal order it.
        return False

    def _note_composite_flight(
        self, act: str, owner: str, key: str | None = None
    ) -> None:
        if act not in _HOST_COMPOSITE_ACTS:
            return
        self._composite_flight_state = {
            "act": act,
            "owner": owner,
            "key": key,
            "qnum": self.sk.question_number,
            "at": time.monotonic(),
        }

    def clear_composite_flight(self, speech_id: str | None) -> None:
        """Release the composite lane at PLAYOUT COMPLETION (confirmed,
        interrupted or suppressed — all three end the flight). Wired from
        on_agent_speech_finished, the one place every outbound turn ends."""
        flight = self._composite_flight_state
        if flight is None:
            return
        if speech_id is not None and flight.get("owner") != speech_id:
            return
        self._composite_flight_state = None

    # -- HOSTLOOP-001 C6: the instant spoken receipt -------------------------
    #
    # WHY HERE. Adjudication is deterministic and instant at Tier 1 — the
    # 8–13s answer-to-verdict latency in Session B, and Session A's
    # one-turn-lag acks, are the LLM COMPOSITE, not the ruling. So the
    # receipt is split off the composite: the same Tier-1 verdict that
    # already exists at the moment the final lands is spoken immediately as
    # fixed words (the deterministic lane), and the styled reveal follows as
    # normal.
    #
    # WHY NOT A NEW LANE. It rides gated_say, so the hold, the
    # question-pending yield, the P0-G pause, the P8 live-game gate, the
    # claim lifecycle and every tts_node gate all bind it. Chain F was just
    # closed and this does not reopen it: there is no second dispatch path,
    # only a second SOURCE OF WORDS for the one that exists.
    #
    # NOT DOUBLING WITH THE COMPOSITE — three independent reasons, all
    # structural:
    #   (a) the receipt never contains the canonical answer, and BOTH
    #       "she already said it" detectors require the answer to be present
    #       (_verdict_already_spoken, lily_verdict_narration via
    #       register_transition_narration / _reveal_already_on_air) — so a
    #       receipt can never preempt, suppress or satisfy the reveal beat;
    #   (b) the composite is TOLD: answer_receipt_aired_for() feeds a line
    #       into the verdict instructions naming the exact words that already
    #       aired, so the beat opens on the reveal instead of ruling twice;
    #   (c) direct_say adds the receipt to the chat context, so her own
    #       transcript shows it before the composite generates.

    def _label_owner(self, speaker_label: str | None) -> str | None:
        """Rostered name currently holding one diarization label, else the
        label itself (an unbound voice still deserves a receipt, and the
        dedupe identity has to be stable for it too). Reads sk.players — the
        same primary-label field bind_speaker maintains."""
        if not speaker_label:
            return None
        for name, state in (getattr(self.sk, "players", None) or {}).items():
            if state.get("speaker_label") == speaker_label:
                return name
        return speaker_label

    def answer_receipt_aired_for(self, qnum: int) -> str | None:
        """The receipt text already aired for question `qnum`, or None —
        the input the verdict composite is conditioned on so it does not
        rule twice."""
        record = self._answer_receipt_aired or {}
        if record.get("qnum") != qnum:
            return None
        return record.get("text") or None

    # -- BARGE-RESILIENCE-001 P1: the RESULT-stated-on-air fact ------------
    #
    # The receipt above is a bare verdict WORD ("Correct!"), never the
    # answer; it exists to let the verdict beat still air the reveal. This
    # fact is the ANSWER (the full result) reaching the air, and it is the
    # anti-double gate for the doubling class (transcript 205/216/249):
    # every one of the three existing guards (C6 receipt-note, N12
    # register_transition_narration, ORGANIC_PREEMPTED) binds only at
    # on-air CONFIRM, and a barge cancels the airing turn BEFORE it confirms,
    # so none of them stamped. This binds at the airing itself (tts_node,
    # before the frames yield), so a barged reveal still records that the
    # room heard the result — and the keyed verdict SAY is gated on it.

    def note_result_aired(self, qnum: int, text: str) -> None:
        """Stamp that qN's RESULT (canonical answer + a verdict cue) has
        physically gone to air. Idempotent per question: the first airing
        wins and later airings of the same qnum do not overwrite the record
        (so the fact names the words the room first heard)."""
        record = self._result_aired or {}
        if record.get("qnum") == qnum:
            return
        self._result_aired = {
            "qnum": qnum,
            "text": (text or "").strip(),
            "at": time.monotonic(),
        }
        logger.warning(
            "LILY_RESULT | AIRED | session=%s q=%d text=%r — the result went "
            "to air; the keyed verdict SAY is now gated and organic turns are "
            "told not to restate it (BARGE-RESILIENCE-001 P1)",
            self.sk.session_id, qnum, (text or "")[:80],
        )

    def result_aired_for(self, qnum: int) -> str | None:
        """The result text already aired for question `qnum`, or None."""
        record = self._result_aired or {}
        if record.get("qnum") != qnum:
            return None
        return record.get("text") or None

    def result_aired_recent(self) -> dict | None:
        """The most recent result-aired record while it is still fresh —
        i.e. not yet cleared by the next question's window open. Read by the
        organic system-prompt wrap so a free conversational turn cannot
        re-announce the ruling the room just heard (transcript 249)."""
        return self._result_aired or None

    def clear_result_aired(self) -> None:
        """Drop the result-aired fact — called at the next non-steal window
        open, when the table has moved on to answering a new question and the
        prior ruling can no longer be doubled."""
        self._result_aired = None

    def stamp_result_aired_from_turn(self, text: str) -> None:
        """AIRING hook (tts_node): if this outbound turn narrates the open
        transition's result — its canonical answer alongside a verdict cue
        (lily_verdict_narration) — record that the result reached air for the
        transition's question. Runs at the say gate, before the TTS frames
        yield, so a barge that cancels the turn mid-playout cannot un-stamp
        what already aired. Never breaks the audio path."""
        try:
            qnum = self._open_transition_qnum
            if qnum is None:
                return
            reveal = self._transition_entry(qnum, "reveal") or {}
            answer = str((reveal.get("detail") or {}).get("answer") or "")
            if not answer:
                return
            if lily_scorekeeper.lily_verdict_narration(text, answer) is None:
                return
            self.note_result_aired(qnum, text)
        except Exception:
            pass

    def _answer_receipt_owed(self, verdict: str, receipt_id: str) -> bool:
        """Guards for one receipt, read at fire time. Deliberately narrow:
        the receipt exists to be FAST, and anything that makes it uncertain
        (no armed question, the composite already narrating this beat) means
        the composite owns the words."""
        if verdict not in ("correct", "incorrect", "uncertain"):
            return False
        if self._delivery_stop_sticky:
            return False
        if not getattr(self, "game_started", False):
            return False
        if getattr(self, "game_over", False):
            return False
        if self.armed_question is None:
            return False
        qnum = self.sk.question_number
        # The transition owns the beat from its reveal onward (N12). Once a
        # verdict stage is journaled the composite has been dispatched and a
        # receipt would be a second ruling of the same committed row.
        try:
            if self.transition_narrated(qnum, "verdict"):
                return False
        except Exception:
            pass
        fired = self._answer_receipts_fired
        if fired is None or self._answer_receipts_qnum != qnum:
            fired, self._answer_receipts_qnum = set(), qnum
            self._answer_receipts_fired = fired
        if receipt_id in fired:
            return False
        if len(fired) >= _ANSWER_RECEIPT_MAX_PER_QUESTION:
            logger.info(
                "LILY_RECEIPT | CAPPED | session=%s q=%d fired=%d — no further "
                "instant receipts on this question",
                self.sk.session_id, qnum, len(fired),
            )
            return False
        fired.add(receipt_id)
        return True

    def fire_answer_receipt(
        self,
        verdict: str,
        *,
        text: str,
        player: str | None = None,
        source: str = "instant_tier1",
    ) -> bool:
        """Voice the SHORT spoken receipt for one answer utterance, NOW.

        `verdict` is the Tier-1 verdict the caller already computed — this
        function never evaluates anything, so it adds no latency and owns no
        matching logic. An UNCERTAIN verdict (the judge has not ruled) gets
        the neutral ack, never a verdict word: lily_answer_receipt makes that
        mapping the only one available.

        Returns True when a receipt was dispatched."""
        receipt = lily_say_gate.lily_answer_receipt(verdict)
        if receipt is None:
            return False
        # Identity is (who, what they said) — NOT the utterance id: the two
        # deterministic forks that reach here (the instant Tier-1 path and
        # the C3b mid-read binding) see the same utterance through different
        # carriers, and this is what keeps one answer to one receipt.
        receipt_id = (
            f"{player or 'floor'}:"
            f"{lily_evaluation.lily_normalize_answer(text)}"
        )
        if not self._answer_receipt_owed(verdict, receipt_id):
            return False
        qnum = self.sk.question_number
        dispatched = self.gated_say(
            None,
            "answer_receipt",
            # The deterministic lane ignores instructions; this string is
            # the audit line a keyless dispatch leaves in the log.
            f"[deterministic receipt: {receipt!r}]",
            source=f"answer_receipt:{source}",
            text=receipt,
        )
        if not dispatched:
            logger.info(
                "LILY_RECEIPT | GATED | session=%s q=%d verdict=%s — the "
                "dispatch gate refused the receipt (hold/floor/stop); the "
                "composite still rules", self.sk.session_id, qnum, verdict,
            )
            return False
        self._answer_receipt_aired = {
            "qnum": qnum,
            "text": receipt,
            "verdict": verdict,
            "player": player,
            "at": time.monotonic(),
        }
        logger.warning(
            "LILY_RECEIPT | AIRED | session=%s q=%d verdict=%s player=%s "
            "source=%s text=%r — deterministic lane; the styled composite "
            "follows and is told this already aired (C6)",
            self.sk.session_id, qnum, verdict, player, source, receipt,
        )
        return True

    # -- HOSTLOOP-001 C8: a cut verdict re-airs, one line ------------------

    def _cut_verdict_keys(self, released: "list | None") -> list:
        """Which of the keys just released by a cut ARE a verdict beat, as
        (qnum, key) pairs. Reads the key vocabulary the reveal path already
        mints — q_{N}_verdict at a round/final boundary, q_{N}_reveal
        otherwise — and takes the question number FROM THE KEY.

        Never from sk.question_number: adjudicate advances it (via
        arm_next_question) in the same tick it dispatches the verdict, so by
        the time that verdict's playout ends the counter already names N+1.
        Reading current state here would look for the wrong key, find
        nothing, and silently reproduce the very defect this fixes."""
        found = []
        for key in (released or []):
            match = _VERDICT_KEY_RE.match(str(key))
            if match:
                found.append((int(match.group(1)), key))
        return found

    def reair_cut_verdict(self, released: "list | None") -> bool:
        """C8: the verdict beat was cut by a barge-in — re-air the RESULT as
        one deterministic line instead of dropping it.

        Session B, 36:25: the cut released q_{N}_reveal (LILY_SAY | RELEASED
        | reason=interrupted), Y7's BARGE_IN_CANCEL policy correctly declined
        to recover a conversational line, and the ruling simply never
        reached the room. Worse, the released claim wedged the beat: the
        transition's verdict entry still names that key, so
        _transition_holds_next_delivery reads state != CONFIRMED forever and
        question N+1 is held.

        Both are fixed by re-airing on the SAME KEY: the claim is retaken
        here and confirmed at this line's playout, so the journal reads
        narrated, N+1 is released by the existing seam, and
        register_transition_narration binds the fresh words as THE narration
        (the re-air holds the verdict key, which is the branch that gate
        already has for exactly this case).

        The RESULT itself comes from the transition journal's own reveal
        entry, not from sk.current_question: adjudicate has already consumed
        the revealed question and armed N+1 by the time this runs, so current
        state would hand out the NEXT question's answer. The journal is the
        record of what was committed for the question being revealed — and
        requiring it also means a re-air can only ever state a result that
        was actually journaled."""
        if self._delivery_stop_sticky:
            return False
        for qnum, key in self._cut_verdict_keys(released):
            try:
                entry = self._transition_entry(qnum, "reveal")
            except Exception:
                entry = None
            detail = (entry or {}).get("detail") or {}
            answer = str(detail.get("answer") or "").strip()
            if not answer:
                logger.warning(
                    "LILY_VERDICT | CUT_REAIR_SKIPPED | session=%s q=%d "
                    "key=%s reason=no_journaled_result — nothing committed to "
                    "re-air honestly", self.sk.session_id, qnum, key,
                )
                continue
            correct = bool(detail.get("correct"))
            winner = detail.get("winner") if correct else None
            line = lily_say_gate.lily_verdict_reair_line(
                correct=correct, answer=answer, winner=winner,
            )
            logger.error(
                "LILY_VERDICT | CUT_REAIR | session=%s q=%d key=%s correct=%s "
                "— the verdict beat was talked over and its claim released; "
                "re-airing the result as one deterministic line rather than "
                "dropping it (C8)",
                self.sk.session_id, qnum, key, correct,
            )
            return self.gated_say(
                key,
                "verdict",
                f"[deterministic verdict re-air: {line!r}]",
                source="verdict_cut_reair",
                text=line,
            )
        return False

    def _supersede_stale_claim(self, key: str) -> bool:
        """Release a PENDING claim that is provably wedged: older than the
        playout deadline, its owning speech never started airing, and no
        agent audio is playing right now. Confirmed acts are final forever;
        young or airing claims are genuine in-flight dispatches and stay.
        True = released (the caller may re-claim and speak)."""
        age = self.say_registry.pending_age(key)
        if age is None or age < _STALE_CLAIM_SECONDS:
            return False
        owner = self.say_registry.owner_of(key)
        started = self._playout_started_ids
        if owner is not None and owner in started:
            return False  # airing (long turn mid-playout) — leave it alone
        if getattr(self.sk, "host_speaking", False):
            return False  # something IS on the air — not the deaf case
        return self.say_registry.release(key)

    def _arm_stale_claim_watchdog(
        self, key: str, act: str, instructions: str, source: str,
        text: str | None = None,
    ) -> None:
        """Arm the never-reached-playout watchdog for one keyed dispatch.
        No-op without a running loop (offline tests drive the registry
        directly)."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        owner = self.say_registry.owner_of(key)
        if owner is None:
            return
        asyncio.ensure_future(
            self._stale_claim_watch(
                key, owner, act, instructions, source, text=text
            )
        )

    async def _stale_claim_watch(
        self, key: str, owner: str, act: str, instructions: str, source: str,
        text: str | None = None,
    ) -> None:
        """Release-and-retry a claim whose speech never started playout.

        Exits silently when the act confirmed (played), released (the
        failure paths worked), changed owner (a newer dispatch took over),
        or started airing (wait_for_playout owns its lifecycle). While
        OTHER audio is airing the check re-arms — a queued act behind a
        long monologue is late, not wedged. Retries are bounded per key:
        past the budget the audio path itself is down (the NC wedge class)
        and re-dispatching would only queue more silence."""
        for _ in range(_STALE_CLAIM_MAX_RECHECKS):
            await asyncio.sleep(_STALE_CLAIM_SECONDS)
            if self.say_registry.state(key) != lily_say_gate.CLAIM_PENDING:
                return
            if self.say_registry.owner_of(key) != owner:
                return
            if owner in self._playout_started_ids:
                return
            if getattr(self.sk, "host_speaking", False):
                continue
            age = self.say_registry.pending_age(key) or 0.0
            counts = self._stale_retry_counts
            if counts is None:
                counts = self._stale_retry_counts = {}
            attempts = counts.get(key, 0)
            if not self.say_registry.release(key):
                return
            logger.warning(
                "LILY_SAY | STALE_CLAIM_RELEASED | key=%s | act=%s | "
                "age=%.1fs | attempt=%d — playout never started; claim "
                "freed for retry", key, act, age, attempts + 1,
            )
            if key == f"q_{self.sk.question_number}_delivery":
                self.expect_delivery()
            if attempts >= _STALE_CLAIM_MAX_RETRIES:
                logger.error(
                    "LILY_SAY | STALE_CLAIM_EXHAUSTED | key=%s | act=%s — "
                    "%d dispatches never reached playout; audio path is "
                    "down (NC-wedge class), not re-dispatching",
                    key, act, attempts + 1,
                )
                return
            counts[key] = attempts + 1
            self.gated_say(
                key, act, instructions, source=f"{source}+stale_retry",
                text=text,
            )
            return

    def note_playout_started(self, speech_id: str | None) -> None:
        """Wired from agent_state_changed -> "speaking" (TTS playout
        actually started, per the 1.6.6 state machine): the current
        speech is ON THE AIR, so its pending claims are in-flight, never
        stale. One id per spoken turn; discarded at playout completion."""
        # New audio is on the air — any earlier cut's dead-air window is
        # void, so a pending auto-resume watchdog stands down (WS-3).
        self.cancel_cut_recovery()
        if not speech_id:
            return
        started = self._playout_started_ids
        if started is None:
            started = self._playout_started_ids = set()
        started.add(speech_id)
        # DISPATCH_TO_AIR (lily-639007: a verdict composite took ~17s from
        # answer to air and nothing said where the time went —
        # COMMIT_TO_DISPATCH_MS read 0, so the whole 17s hid between
        # dispatch and first audio frame: LLM generation + TTS + queue).
        # The flight record already carries the dispatch stamp; one line
        # here decomposes every composite's latency at zero new state.
        flight = self._composite_flight_state
        if flight is not None and flight.get("owner") == speech_id:
            logger.info(
                "LILY_LATENCY | DISPATCH_TO_AIR_MS | session=%s act=%s "
                "ms=%.0f",
                self.sk.session_id, flight.get("act"),
                (time.monotonic() - float(flight.get("at") or 0.0)) * 1000,
            )
        # Transcript sync (2026-08-09 live report): the glass used to see
        # Lily's line only at playout COMPLETION — a long read trailed the
        # voice by the whole turn. The exact aired text is already bound to
        # this speech handle (tts_node clips BEFORE synthesis, Y5), so show
        # it as an INTERIM segment the moment the audio starts; the
        # completion publish (same segment id, final, cut marker if any)
        # replaces it in place. Peek, never consume — the one-shot binding
        # still belongs to playout completion (the durable record).
        # WO-LILY-UI-SYNC-TYPEWRITER-001: with the framework driving the
        # progressive word-by-word display (RoomOptions text_output on,
        # sync_transcription), this full-line interim paste is exactly the
        # "whole string at once, then a cosmetic client stagger" it was a
        # stopgap for — drop it and let the framework's playout-synced
        # forwarding fill the panel and the board. The completion publish
        # (final=True) below still lands as the durable/cut record. When the
        # feature is off, the legacy interim paste stays.
        if not lily_config.voice_synced_transcript_enabled():
            try:
                airing = self.peek_post_tts_text(speech_id)
                if airing:
                    self.publish_agent_transcription_nowait(
                        airing, speech_id=speech_id, interrupted=False,
                        final=False,
                    )
            except Exception:
                logger.exception(
                    "LILY_TRANSCRIPT | INTERIM_PUBLISH_FAILED — completion "
                    "publish still covers this turn"
                )
        if self._awaiting_address_since:
            # A dispatch is only an intention. Clear at real playout so a
            # queued, wedged, or suppressed handle cannot hide the
            # ADDRESS_UNANSWERED signal.
            self._awaiting_address_since = 0.0
            self._address_unanswered_warned = False
            logger.info(
                "LILY_RESPONSIVENESS | RESPONSE_PLAYOUT | session=%s "
                "speech_id=%s — direct-address latch cleared",
                self.sk.session_id, speech_id,
            )
        # M4: if the speech now airing owns this question's delivery claim,
        # its STEM has reached the air — record it so an abandonment before
        # the window opens is flagged as cancelled, never a silent vanish.
        delivery_key = f"q_{self.sk.question_number}_delivery"
        if self.say_registry.owner_of(delivery_key) == speech_id:
            now = time.time()
            self._active_delivery_qnum = self.sk.question_number
            self._active_delivery_started_at = now
            self._active_delivery_ended_at = None
            if self._mc_delivery_qnum == self.sk.question_number:
                self._mc_delivery_started_at = now
                # HOSTLOOP-001 C3a: the answer window arms at the CORE
                # QUESTION's completion, not at full-choices playout. The
                # stem's spoken length is already modelled (WS-5's
                # stem-protection estimator, the same knob), and the core
                # sentence completing is exactly the boundary that estimator
                # names — so the arm rides it rather than adding a second
                # notion of "the question has been asked".
                self._schedule_core_completion_window(self.sk.question_number)
            self.mark_stem_aired(self.sk.question_number)
            # THE QUESTION IS NOW AUDIBLE — so it belongs on the glass and
            # in the group's burn ledger. Both used to wait for the delivery
            # to FINISH, which a barged delivery never does: live
            # 2026-08-08 `lily-2C489B` sat on the lobby screen for seven
            # minutes with a signed image URL in hand, and burned the
            # arsenal entry anyway. Airing is the honest seam for both —
            # the room has heard it, whether or not she got to the end.
            # Never raises into the speech path: this runs at the exact
            # moment audio starts, and a screen publish is not permitted to
            # take a spoken turn down with it.
            try:
                self.publish_question_to_glass(reason="playout_started")
                self.record_question_asked(reason="playout_started")
            except Exception as e:
                logger.warning(
                    "LILY_STATE | GLASS_AT_AIR_FAILED | %s", e
                )

    def expect_delivery(self) -> None:
        """Arm the structural delivery flag: the next outbound spoken turn
        was just dispatched to perform the armed question and will claim
        q_{N}_delivery at dispatch. No-op pre-game (WS-1: intake turns can
        never become deliveries), when nothing is armed, the window is
        already open, or the delivery is already claimed."""
        if self._delivery_stop_sticky:
            logger.info(
                "LILY_DELIVERY | EXPECT_BLOCKED | session=%s q=%d "
                "reason=game_stopped",
                self.sk.session_id, self.sk.question_number,
            )
            return
        # P0-G, scoped (2026-08-09) — same narrowing as the dispatch gate:
        # an unresolved direct address or pending setup/ownership work
        # blocks arming the delivery expectation; nothing else. The
        # unfiltered progression_paused_reason blocked the expectation for
        # the pending question's OWN nudge/re-offer
        # (reason=question_pending), so the nudge aired without ever
        # becoming a registered delivery — the desync fixture's exact
        # regression.
        paused = (
            "address_unanswered"
            if self._awaiting_address_since
            else "setup_pending" if self.pending_setup_jobs() else None
        )
        if paused:
            logger.info(
                "LILY_DELIVERY | EXPECT_BLOCKED | session=%s q=%d "
                "reason=%s",
                self.sk.session_id, self.sk.question_number, paused,
            )
            return
        if not getattr(self, "game_started", False):
            return
        if self.armed_question is None or self.sk.answer_window_open:
            return
        key = f"q_{self.sk.question_number}_delivery"
        if self.say_registry.state(key) is None:
            self._pending_delivery_qnum = self.sk.question_number

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

    def delivery_reached_the_table(self, spoken_text: str) -> bool:
        """Did this cut delivery still put the question in the room?

        Reuses `_delivery_text_matches_armed` — the predicate that already
        decides whether a turn presented the armed question and all its
        options. No new similarity rule; the existing one just never got
        asked at the one moment it mattered, the interrupt.

        Also carries the BOUND. Three identical re-reads of one question
        (live `lily-2C489B`, 22:49:37 / 22:49:47 / 22:50:05, each cut on
        the same word) is not recovery, it is a loop with no exit. Past the
        cap the question goes back to supply through the path that already
        exists for a delivery that never aired."""
        qnum = self.sk.question_number
        if self._delivery_text_matches_armed(spoken_text or ""):
            logger.info(
                "LILY_WINDOW | CUT_AFTER_QUESTION_AIRED | session=%s q=%d — "
                "the table has the question; confirming instead of re-reading",
                self.sk.session_id, qnum,
            )
            return self.force_confirm_delivery_heard(reason="cut_after_air")
        cuts = self._delivery_cuts
        if cuts is None or self._delivery_cuts_qnum != qnum:
            cuts, self._delivery_cuts_qnum = 0, qnum
        cuts += 1
        self._delivery_cuts = cuts
        if cuts < _DELIVERY_MAX_CUT_REAIRS:
            return False  # re-arm: they genuinely have not heard it yet
        logger.error(
            "LILY_WINDOW | DELIVERY_CUT_EXHAUSTED | session=%s q=%d cuts=%d "
            "— the room talks over every read of this one; returning it to "
            "supply rather than reading it a fourth time",
            self.sk.session_id, qnum, cuts,
        )
        self.say_registry.release(f"q_{qnum}_delivery")
        self._release_armed_question_to_supply()
        return True

    def arm_reair_gate(self) -> None:
        """Mark that the act just cut/suppressed will re-dispatch as a
        re-air: the next code-triggered speech regenerates rather than
        replays (WS-3)."""
        self._reair_gate_armed = True

    def peek_reair_gate(self) -> bool:
        """Read the re-air arm without consuming it."""
        return self._reair_gate_armed

    def take_reair_dispatch(self) -> bool:
        """Consume the re-air arm at DISPATCH time and hand the signal on
        to tts_node via _reair_turn_pending. True = this dispatch is a
        re-air and must carry a regeneration directive."""
        if not self._reair_gate_armed:
            return False
        self._reair_gate_armed = False
        self._reair_turn_pending = True
        return True

    def take_reair_turn(self) -> bool:
        """Consume the re-air signal at PLAYOUT (tts_node). True = the
        outbound turn is a re-air and its verbatim-replay lint is a GATE,
        not telemetry."""
        pending = self._reair_turn_pending
        self._reair_turn_pending = False
        return pending

    def is_question_delivery_turn(self, spoken_text: str) -> bool:
        """True when the outbound turn is performing the armed question. A
        barged re-read of the question is CORRECT verbatim (players need
        the whole question), so it is exempt from the conversational
        regeneration gate (WS-3)."""
        if self._pending_delivery_qnum is not None:
            return True
        armed = getattr(self, "armed_question", None)
        if armed is not None and not self.sk.answer_window_open:
            return self._delivery_text_matches_armed(spoken_text)
        return False

    def reair_verbatim_should_regenerate(
        self, spoken_text: str, repeat_kind: "str | None"
    ) -> bool:
        """tts_node gate decision (WS-3): consume the re-air turn signal;
        a re-air that STILL repeats an already-aired turn verbatim must be
        suppressed and regenerated. Question deliveries are exempt (a
        barged question is re-read verbatim on purpose)."""
        reair_turn = self.take_reair_turn()
        if not reair_turn or not repeat_kind:
            return False
        return not self.is_question_delivery_turn(spoken_text)

    def note_user_speech_state(self, speaking: bool) -> None:
        """The VAD user-speech edge — the ONE wiring point (session
        `user_state_changed`). Keeps `_user_speaking` (the P0-2 kickoff
        floor) and stamps the FALLING edge, which is what makes the cut
        CAUSE readable after the fact (Y7).

        Why here and not off the interrupt itself: `user_state_changed`
        fires from the framework's VAD `on_start_of_speech`, which
        necessarily precedes its interruption decision
        (`on_vad_inference_done` -> speech_duration >=
        interruption.min_duration -> `_interrupt_by_audio_activity`). By
        the time a cut reaches `on_agent_speech_finished` the human may
        already be back in `listening` and her transcript may have been
        dropped, so the timestamp is the only surviving evidence."""
        if not speaking and self._user_speaking:
            self._user_speech_ended_at = time.monotonic()
        self._user_speaking = bool(speaking)

    def cut_was_deliberate_barge_in(self) -> bool:
        """Y7 (WO-LILY-HOTFIX-007): did a HUMAN end this turn on purpose?

        `SpeechHandle.interrupted` is cause-blind — a barge-in, a
        `cancel_speech(force=True)`, an MC abort and a paused-then-
        committed turn all set the same flag — and recovery is legitimate
        for exactly one cause: the stream died on its own. This is the
        discriminator, and it reads the VAD layer (the human's own voice),
        not the interrupt.

        Two signals, one question ("has a human taken the floor?"):
        `_user_speaking` while the cut lands, and either edge —
        VAD end-of-speech or a COMMITTED user turn — inside the shared
        barge window. The committed-turn half is the pre-existing
        `_cut_recovery_should_fire` proxy; it stays because it is free,
        but it cannot carry this decision alone: (1) the framework drops
        the transcript that caused the barge when it falls inside
        `ignore_user_transcript_until` (AudioRecognition
        `_flush_held_transcripts`), so the utterance may never commit,
        and (2) `_on_end_of_turn` awaits `current_speech.interrupt()`
        BEFORE `on_user_turn_completed`, so even a committed turn can be
        stamped after this decision has already been made."""
        if self._user_speaking:
            return True
        for stamp in (
            self._user_speech_ended_at,
            self._last_user_turn_at,
        ):
            if stamp and (
                time.monotonic() - stamp <= _CUT_RECOVERY_USER_TURN_LOOKBACK
            ):
                return True
        return False

    def _cut_recovery_should_arm(
        self, released: "list | None", interrupted: bool, failed: bool
    ) -> bool:
        """Arm the auto-resume only for a cut/failed ORGANIC turn. A keyed
        game act (released non-empty) recovers through the game loop; the
        answer window / adjudication own their own timing; a finished game
        has nothing to resume.

        Y7: and only for the right CAUSE. A deliberate barge-in is a
        CANCEL — she stops and yields the floor — so it arms nothing.
        Recovery machinery exists for a turn the ROOM did not end: a dead
        TTS stream, a mid-air network death. `failed` names that cause
        outright and is never overridden by a voice near the cut."""
        if not (interrupted or failed):
            return False
        if released:  # a keyed game act — the game loop owns its re-dispatch
            return False
        if interrupted and not failed and self.cut_was_deliberate_barge_in():
            logger.info(
                "LILY_INTERRUPT | BARGE_IN_CANCEL | session=%s — human talked "
                "over her; yielding the floor (no auto-resume, no re-air, no "
                "regeneration)",
                self.sk.session_id,
            )
            return False
        if getattr(self, "game_over", False):
            return False
        if self._delivery_stop_sticky:
            return False
        if getattr(self.sk, "answer_window_open", False):
            return False
        return not self._adjudicating

    def arm_cut_recovery(self, tail_text: str) -> None:
        """Schedule the auto-resume watchdog for a cut organic turn. Bumps
        the recovery token so any earlier watchdog is superseded and stamps
        the arm time (the user-turn recency guard keys off it). No-op
        without a running loop — offline tests drive _cut_recovery_should_fire
        / trigger_cut_recovery directly."""
        self._cut_recovery_token = self._cut_recovery_token + 1
        self._cut_recovery_tail = tail_text or ""
        self._cut_recovery_armed_at = time.monotonic()
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        asyncio.ensure_future(self._cut_recovery_watch(self._cut_recovery_token))

    def cancel_cut_recovery(self) -> None:
        """Supersede any pending auto-resume (new speech started airing —
        one of the one-emission cancel points)."""
        self._cut_recovery_token = self._cut_recovery_token + 1

    def note_user_turn(self) -> None:
        """Stamp the last user-turn time. A user turn near a cut means the
        room re-engaged on its own — the normal reply path owns the
        recovery, so the auto-resume watchdog stands down (no double-speak).

        Y7 review F2 (slow-STT corner): when the barge's transcript commits
        LATER than the 2s VAD window (endpointing max 6s), the cut is
        misclassified as recoverable and ARMS the re-air gate — then this
        cancel kills the watchdog and nothing ever consumed the arm, so the
        next code dispatch (an IDLE_REARM nudge, minutes later) would carry
        the stale "you were cut short" directive. If a recovery from a
        recent cut is being cancelled here while its arm is still live, the
        stand-down cleans up — same single exit as the floor yield and the
        gated refusal. Scoped by _cut_recovery_armed_at recency so an arm
        belonging to anything other than the cut being cancelled is left
        alone. Only the ARM clears here — NOT the address debt: the user
        turn produces an organic reply, and the debt latch is what keeps
        code dispatches from jumping in front of that answer until its
        playout clears it (the designed path)."""
        self._last_user_turn_at = time.monotonic()
        armed_at = self._cut_recovery_armed_at
        arm_belongs_to_this_cut = (
            self._reair_gate_armed
            and armed_at > 0.0
            and (self._last_user_turn_at - armed_at)
            <= lily_config.cut_recovery_grace()
            + _CUT_RECOVERY_USER_TURN_LOOKBACK
        )
        self.cancel_cut_recovery()
        if arm_belongs_to_this_cut:
            self._reair_gate_armed = False
            logger.info(
                "LILY_CUT_RECOVERY | REAIR_ARM_CLEARED | session=%s "
                "reason=user_reengaged — the room answered the cut itself; "
                "no live arm may leak to an unrelated dispatch",
                self.sk.session_id,
            )

    def _floor_yields_recovery(self) -> "str | None":
        """The floor read for the auto-resume: the floor state to yield to,
        or None to proceed. Derived read only (LilyGame.floor_state).

        IN-GAME ONLY (Y10 review F2). Pre-game there is no machine path that
        can re-engage after a yield: the idle watchdog returns early when
        `game_started` is False, so a stood-down lobby/intake resume is
        one-shot dead air at the exact moment intake needs her to come back.
        There is also nothing to protect the room FROM pre-game — the
        restraint this gate exists for is her talking over the table's own
        conversation during a live game, not her finishing an intake line.
        A pre-game HOLD still refuses, one layer down: gated_say's hold gate
        binds the dispatch itself now that chain F is closed, so an explicit
        "give us a minute" is honoured in the lobby too."""
        if not getattr(self, "game_started", False):
            return None
        if getattr(self, "game_over", False):
            return None
        floor = self.floor_state()
        if floor in (self.FLOOR_PLAYER_SPEAKING, self.FLOOR_HOLD):
            return floor
        return None

    def _stand_down_cut_recovery(self, reason: str) -> None:
        """THE single exit for a recovery that will NOT speak — the floor
        yield and a gated dispatch both land here. Restraint has to clean up
        after itself; a stood-down recovery leaves no live arm and no
        unpayable debt behind (both found by the Y10 adversarial review).

        (F3) The re-air arm. The cut armed it (on_agent_speech_finished) so
        that the NEXT code-triggered turn regenerates rather than replays.
        With the resume stood down, "the next code dispatch" can be an
        unrelated act minutes later — an IDLE_REARM question_nudge would
        take the arm and air a never-cut question carrying the "you were cut
        short mid-question, pick up where you broke off" directive. The arm
        is cleared, not consumed: consuming it would set _reair_turn_pending
        and hand the regen GATE a turn that is not a re-air. There is
        nothing left to avoid replaying anyway — the abandoned clause is not
        owed to anyone.

        (F1) The address debt. `_awaiting_address_since` is set for every
        host-directed final and cleared ONLY at real playout
        (note_playout_started). A reply that died before playout, whose
        recovery then stands down, leaves that latch set with no actor left
        to clear it — and `progression_paused_reason` returns
        address_unanswered forever, so the idle watchdog logs
        WATCHDOG_PAUSED and skips the WHOLE game-lane recovery ladder on
        every tick: machine-unbounded in-game dead air, which is M1's
        failure mode arriving through the counterweight's own door. The
        address was answered — by a turn that died — so the debt dies with
        the recovery that would have paid it. Releasing it here restores
        exactly what the pre-Y10 code got by accident (the bypassing resume
        fired and its playout cleared the latch), without the bypass."""
        if self._reair_gate_armed:
            self._reair_gate_armed = False
            logger.info(
                "LILY_CUT_RECOVERY | REAIR_ARM_CLEARED | session=%s "
                "reason=%s — resume stood down; no live arm may leak to an "
                "unrelated dispatch", self.sk.session_id, reason,
            )
        if self._awaiting_address_since:
            self._awaiting_address_since = 0.0
            self._address_unanswered_warned = False
            logger.warning(
                "LILY_RESPONSIVENESS | ADDRESS_DEBT_CLEARED | session=%s "
                "reason=%s — the answering turn died and its recovery stood "
                "down; releasing the latch so progression is not paused on a "
                "debt nothing can pay", self.sk.session_id, reason,
            )

    def _cut_recovery_should_fire(self, token: int) -> bool:
        """True only if this token's watchdog is still live AND the cut left
        genuine dead air: not superseded, nobody speaking, no user turn in
        the lookback window (a real barge-in carried content the normal path
        answers), game still live and out of a scoring window."""
        if self._cut_recovery_token != token:
            return False  # superseded by a newer cut or an explicit cancel
        # FLOOR-001 counterweight (HOTFIX-007 Y10) — THE GRADED CHOICE.
        # This watchdog is the purest expression of the code-side push
        # mandate: a machine timer, firing into silence, with no user turn
        # asking for anything. Its five original pre-conditions all ask "is
        # the air free?" and none asks "is the floor MINE?". Dead air while
        # the table talks among itself is not her failure to fill (canon:
        # "Silence while the table plays without you is not your hosting
        # failing — it is your hosting, working"), and a cut that lands
        # inside a hold must not produce the auto-resume the hold exists to
        # prevent (GUARD_MAP chain F). Both cases choose SILENCE here: no
        # resume, no minimal ack, and nothing re-armed — her dropped clause
        # is not owed to anyone, and the idle watchdog still owns a genuine
        # game stall. The floor read is derived from state that already
        # existed (see LilyGame.floor_state).
        floor_yield = self._floor_yields_recovery()
        if floor_yield:
            logger.info(
                "LILY_FLOOR | RECOVERY_YIELDED | session=%s floor=%s — cut "
                "left dead air but the floor is not hers; staying quiet",
                self.sk.session_id, floor_yield,
            )
            self._stand_down_cut_recovery(f"floor:{floor_yield}")
            return False
        if getattr(self.sk, "host_speaking", False):
            return False  # audio already resumed / a new turn is airing
        armed_at = self._cut_recovery_armed_at
        last_user = self._last_user_turn_at
        if last_user >= armed_at - _CUT_RECOVERY_USER_TURN_LOOKBACK:
            # A user turn landed around/after the cut — a genuine barge with
            # content; the normal reply path (re-air-gated fresh) owns it.
            return False
        # Integration note (Y7+Y10): Y7 added a fire-time hold_blocks_dispatch
        # branch here ("HELD"). It is gone on purpose. In-game, the floor
        # gate above already yields on FLOOR_HOLD and runs the stand-down;
        # pre-game, the dispatch proceeds to gated_say whose hold gate
        # refuses it (chain F is closed, the resume rides the same funnel),
        # and the GATED path runs the same stand-down. Keeping the fire-time
        # branch would have returned False WITHOUT the stand-down —
        # re-opening the stranded-arm leak (Y10 review F3) for pre-game
        # holds, and logging a second line for the same in-game event.
        if getattr(self, "game_over", False):
            return False
        if getattr(self.sk, "answer_window_open", False):
            return False
        return not self._adjudicating

    def trigger_cut_recovery(self) -> bool:
        """Dispatch the fresh auto-resume. Arms the re-air gate first so the
        resume regenerates rather than replays (the shared WS-3 gate), then
        fires the cut-recovery directive THROUGH gated_say. Returns True if
        a resume was actually dispatched.

        HOTFIX-007 Y10 — CHAIN F CLOSED. This method used to call
        instructed_reply directly, so the auto-resume was the one outbound
        lane that skipped the dispatch choke point entirely: the hold gate,
        the question-pending gate, the P0-G progression pause and the P8
        live-game gate never ran on it (GUARD_MAP chain F). A cut landing
        inside a hold produced exactly the auto-resume the hold was built to
        prevent. It routes through the SAME funnel every other code-driven
        turn uses now — no second gate implementation, no new gate.
        Keyless by design (key=None): the resume claims no speech act, so it
        arms neither the stale-claim watchdog nor any retry ladder — a
        refused resume is simply silence, which is the point.

        Consuming its own re-air arm here also closes a latent leak: the arm
        set by the cut (on_agent_speech_finished) was previously eaten by
        whatever code dispatch came NEXT, which could hand the
        cut-short-mid-question directive to a question that was never cut."""
        self.arm_reair_gate()
        dispatched = self.gated_say(
            None,
            "cut_recovery",
            _CUT_RECOVERY_DIRECTIVE,
            source="cut_recovery",
        )
        if dispatched:
            logger.warning(
                "LILY_CUT_RECOVERY | RESUMED | session=%s — cut turn left "
                "dead air, auto-resuming fresh (no operator poke)",
                self.sk.session_id,
            )
        else:
            logger.info(
                "LILY_CUT_RECOVERY | GATED | session=%s floor=%s — the "
                "dispatch gate refused the auto-resume; the dropped clause "
                "is not owed", self.sk.session_id, self.floor_state(),
            )
            # A refused dispatch is a stood-down recovery: same cleanup as
            # the floor yield (arm cleared, address debt released). The arm
            # was set above and gated_say refuses BEFORE consuming it, so
            # without this it strands (Y10 review F3).
            self._stand_down_cut_recovery("gated")
        return dispatched

    async def _cut_recovery_watch(self, token: int) -> None:
        """Wait out the grace window, then auto-resume iff the cut still
        left dead air. Grace sits above false_interruption_timeout (the
        framework's own pause/resume gets first crack) and above healthy
        user-turn latency, so the watchdog only ever fires into silence."""
        await asyncio.sleep(lily_config.cut_recovery_grace())
        if not self._cut_recovery_should_fire(token):
            return
        self.trigger_cut_recovery()

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
          "held"                 — a hold is active and this turn would air
                                   the armed question; suppressed at
                                   dispatch, the caller makes it physically
                                   silent (W2);
          None                   — not a delivery event; speak normally.
        """
        if self._delivery_stop_sticky:
            return None
        armed = self.armed_question
        if armed is None or self.sk.answer_window_open:
            return None
        if not getattr(self, "game_started", False):
            # WS-1 pre-game gate: THE claim choke point. While the intake
            # round-robin runs, no outbound turn — structural flag or not
            # — may register a delivery (the 22:48 evidence session
            # claimed q_1_delivery on "Hi, Chris! Got you locked in.").
            return None
        qnum = self.sk.question_number
        key = f"q_{qnum}_delivery"
        # W2 (WO-LILY-HOTFIX-009): the hold binds the DELIVERY lane, not
        # just the gated_say (code-dispatch) lanes. gated_say already
        # refuses every code-triggered beat while held (hold_blocks_dispatch),
        # but a question can still reach the air through THIS lane — an
        # organic turn (or a nudge-induced turn) that performs the armed
        # question is registered here, at tts_node, having never passed the
        # gated_say hold gate. Live lily-5E3036 q_6: the Freudian question
        # aired at 05:40:11 as a structural tts_node claim. So the hold is
        # checked at THIS dispatch too: a turn that would air the armed
        # question while held is suppressed (physically silent), never
        # deferred and fired later. Keyed on the question actually being in
        # the turn's text — a conversational reply the player is owed (her
        # "still stopped until you say go") is not a delivery here and
        # speaks. STOP/sticky is stronger and returns above; this changes
        # only the plain-hold case (self-wait-promise / question-unanswered).
        if self._hold_active and (
            self._delivery_text_matches_armed(spoken_text)
        ):
            logger.warning(
                "LILY_SAY_SUPPRESSED | reason=hold | key=%s | "
                "act=question_delivery | source=tts_node", key,
            )
            return "held"
        delivery_acts = self._delivery_speech_acts or {}
        delivery_act = (
            delivery_acts.pop(speech_id, None) if speech_id else None
        )
        textual = self._delivery_text_matches_armed(spoken_text)
        # Explicit post-reveal/nudge turns are question-only handles. Always
        # replace their model prose with the deterministic sheet on the first
        # pass, even when the model included the question after another
        # celebration ("Gold is correct!" before Franklin).
        if delivery_act in ("question_delivery", "question_nudge"):
            logger.info(
                "LILY_DELIVERY | EXACT_SHEET_REWRITE | session=%s q=%d "
                "act=%s",
                self.sk.session_id, qnum, delivery_act,
            )
            return "rewrite_strict"
        pending_structural = self._pending_delivery_qnum == qnum
        structural = False
        if pending_structural and textual:
            structural = self.consume_pending_delivery(qnum)
        elif pending_structural and not textual:
            if speech_id is None:
                # Offline/direct callers predate handle-scoped intent; retain
                # the strict structural contract on that compatibility seam.
                structural = self.consume_pending_delivery(qnum)
            else:
                ratio = lily_evaluation.lily_question_spoken_ratio(
                    armed.get("prompt", ""), spoken_text
                )
                if ratio >= lily_evaluation.QUESTION_SPOKEN_NEAR_MISS_RATIO:
                    logger.warning(
                        "LILY_DELIVERY | STRICT_REWRITE | session=%s q=%d "
                        "reason=pending_near_verbatim ratio=%.2f",
                        self.sk.session_id, qnum, ratio,
                    )
                    return "rewrite_strict"
                # A racing recognition/preferences turn is not the question.
                # Preserve the delivery intent for the real post-tool/delivery
                # speech instead of consuming it and mislabeling this handle.
                return None
        if structural and not textual:
            # WS-1: the strict text-sanity rewrite applies to EVERY
            # structural claim, not just the post-reveal turn — a delivery
            # turn without the question text is rewritten to the sheet
            # before claiming, never claimed silently (the q_7 "My bad,
            # team!" apology claim).
            logger.error(
                "LILY_DELIVERY | STRICT_REWRITE | session=%s q=%d "
                "reason=spoken_question_mismatch",
                self.sk.session_id, qnum,
            )
            return "rewrite_strict"
        if not structural and not textual:
            # A near-verbatim organic performance (most commonly the exact
            # stem with one MC option missing) used to air first, fail the
            # strict claim, then trigger a second sheet-read nudge. Rewrite
            # BEFORE TTS instead: the table hears one authoritative question.
            ratio = lily_evaluation.lily_question_spoken_ratio(
                armed.get("prompt", ""), spoken_text
            )
            if ratio >= lily_evaluation.QUESTION_SPOKEN_NEAR_MISS_RATIO:
                logger.warning(
                    "LILY_DELIVERY | STRICT_REWRITE | session=%s q=%d "
                    "reason=near_verbatim_unregistered ratio=%.2f",
                    self.sk.session_id, qnum, ratio,
                )
                return "rewrite_strict"
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
            #
            # WS-5: this delivery is now registered. Mark an MC delivery in
            # flight (answer-aborts-read) and fold any finals that landed
            # in the buzz window just BEFORE this claim into the pre-window
            # buffer, so an early buzzer is scored at open, not left inert.
            self._note_mc_delivery_start(qnum)
            self._active_delivery_qnum = qnum
            # Registration happens in tts_node, which may precede actual
            # playout while other audio is queued. Only note_playout_started
            # opens the early-answer interval.
            self._active_delivery_started_at = None
            self._active_delivery_ended_at = None
            # PATCH-001 T5(a) / OMNIBUS-004 WS-2: the pre-window buffer
            # covers CLAIM-TO-OPEN ONLY. The old backfill folded finals
            # from BEFORE the delivery claim into the buffer — speech
            # predating the claim cannot answer a not-yet-aired question
            # (live: two pre-question Mars-conversation fragments scored
            # into the Socrates window, consuming Rami's judgment so his
            # real answer went inert). The rolling store is cleared so
            # pre-claim speech can never leak in.
            self._recent_finals = []
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

    def _segment_overlaps_active_delivery(self, seg: dict) -> bool:
        """True only when captured speech overlaps actual delivery playout."""
        qnum = self._active_delivery_qnum
        if qnum is None or qnum != self.sk.question_number:
            return False
        started = self._active_delivery_started_at
        if started is None:
            return False
        try:
            seg_start = float(seg["segment_start_time"])
            seg_end = float(seg.get("segment_end_time", seg_start))
        except (KeyError, TypeError, ValueError):
            return False
        ended = self._active_delivery_ended_at
        return seg_end >= started and (ended is None or seg_start <= ended)

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
        lobby chatter and post-window banter never buffer.

        HOSTLOOP-001 C4: and never unless the final is ANSWER-SHAPED. This
        buffer is the one lane that can put speech in front of adjudication
        without the speech ever having been inside an open window: the
        replay calls on_transcript_segment(assume_in_window=True), which by
        design bypasses BOTH the WS-10 sanity gate and window membership, so
        whatever lands here is a candidate by construction. It used to take
        every final that overlapped delivery playout, which is how the
        Session A (2026-08-12 04:50 UTC) complaint fragment "Like speaking
        at the." became the q6 answer and was scored incorrect. Speech that
        is not an answer attempt still reaches Lily conversationally — the
        caller records it into `transcripts` and runs on_transcript_event
        either way — it simply never becomes scoreable."""
        if (
            self._delivery_stop_sticky
            or self.sk.answer_window_open
            or self.armed_question is None
            or not self._segment_overlaps_active_delivery(seg)
        ):
            return
        key = f"q_{self.sk.question_number}_delivery"
        if self.say_registry.state(key) is None:
            return
        if not self._seg_answer_shaped(seg):
            logger.info(
                "LILY_ANSWER | PRE_WINDOW_NOT_ANSWER_SHAPED | session=%s "
                "q=%d speaker=%s text=%r — kept as conversation, never a "
                "candidate (C4)",
                self.sk.session_id, self.sk.question_number,
                seg.get("speaker_label"), (seg.get("text") or "")[:80],
            )
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
                # assume_in_window: buffered early answers were spoken
                # BEFORE the window opened by design — spoken-time
                # membership (WS-10) would reject them; the buffering
                # already gated on the armed question's claimed delivery.
                seg_obj = lily_scorekeeper.TranscriptSegment(
                    is_final=True, now=replay_ts, assume_in_window=True,
                    **seg
                )
                last_result = self.sk._dispatch_segment(seg_obj)
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
                threshold = self.sk.tier1_threshold(
                    now=replay_ts,
                    addressee_confidence=last_result.get(
                        "addressee_confidence"
                    ),
                )
                # BARGE-RESILIENCE-001 P0-4: scan EVERY replayed candidate for
                # a Tier-1 correct one, not just ordered[0]. A clipped/delayed
                # question (shown on screen, never voiced) opens its window
                # LATE — the early card-read answer may sit behind an unrelated
                # first buzz, and gating the fast path on ordered[0] alone left
                # a correct pre-window answer inert until the window-close path
                # (the very path that was unreliable for a clipped question:
                # the 12:28 Q3 "prostate" 0-score). Adjudicate on the first
                # correct so the captured answer is scored now, in a clean
                # window, exactly once (dedupe by candidate keeps it single).
                correct_cand = None
                for cand in ordered:
                    t1 = self._tier1_question(
                        cand["text"], question,
                        key=cand["player"]
                        or f"unrostered:{cand['speaker_label']}",
                        threshold=threshold,
                    )
                    if t1["verdict"] == "correct":
                        correct_cand = cand
                        break
                if correct_cand is not None:
                    self.send_event_nowait(
                        "lock", {"name": correct_cand.get("player")}
                    )
                    asyncio.ensure_future(
                        self.adjudicate(steal_allowed=False)
                    )
        except Exception as e:
            logger.warning(
                "LILY_ANSWER | PRE_WINDOW_TIER1_FAILED: %s", e
            )

    def note_recent_final(self, seg: dict, ts: float) -> None:
        """WS-5 buzz-buffer widening: remember recent finals so one that
        landed just BEFORE the delivery claim can be back-filled into the
        pre-window buffer at claim time. Trimmed to buzz_prewindow_seconds()
        (and a hard 12-item cap) so it never grows without bound."""
        buf = self._recent_finals
        if buf is None:
            buf = []
            self._recent_finals = buf
        buf.append((ts, dict(seg)))
        horizon = lily_config.buzz_prewindow_seconds()
        cutoff = ts - max(horizon, 0.0)
        self._recent_finals = [(t, s) for (t, s) in buf if t >= cutoff][-12:]

    def _backfill_prewindow_from_recent(self, now: float | None = None) -> None:
        """Retired: pre-claim speech can never answer a queued question."""
        self._recent_finals = []

    # -- HOSTLOOP-001 C3/C4: answer-shape + mid-read resume ----------------

    def core_completion_delay(self, now: float) -> float | None:
        """Seconds from `now` until the CORE question sentence is estimated
        to finish airing, or None when that is not knowable/applicable.

        Reuses WS-5's stem model exactly: stem word count / configured
        words-per-second, measured from actual playout start. Already
        elapsed = 0.0 (arm immediately)."""
        started = self._mc_delivery_started_at
        if started is None:
            return None
        wps = lily_config.mc_stem_protect_words_per_second()
        if wps <= 0:
            return None
        stem_words = self._mc_delivery_stem_words
        return max(0.0, (started + (stem_words / wps)) - now)

    def _core_completion_window_should_arm(self, qnum: int) -> bool:
        """Guards for the C3a arm, read at FIRE time (not schedule time)."""
        if qnum != self.sk.question_number:
            return False
        if self.armed_question is None:
            return False
        if self.sk.answer_window_open or self._adjudicating:
            return False
        if self._delivery_stop_sticky:
            return False
        if getattr(self, "game_over", False):
            return False
        if not getattr(self, "game_started", False):
            return False
        # The read must still be the live one; a delivery that already ended
        # goes through the normal playout-completion open.
        return self._mc_delivery_qnum == qnum

    def _schedule_core_completion_window(self, qnum: int) -> None:
        """C3a: arm the answer window when the core question sentence
        completes, leaving the options to keep airing into an OPEN window.

        Session B (lily-05BB92) is the cost of waiting for CONFIRMED: the
        window was still closed through the whole options read, so a barge
        during the choices could not bind an answer even in principle. The
        existing pre-window buffer covered the fast buzzer for a read that
        RAN TO COMPLETION; it could do nothing for a read that never
        completed, because the replay only ever happens at window open."""
        delay = self.core_completion_delay(time.time())
        if delay is None:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return  # offline/fixture: fixtures call open_window directly

        async def _arm() -> None:
            await asyncio.sleep(delay)
            if not self._core_completion_window_should_arm(qnum):
                return
            logger.info(
                "LILY_WINDOW | CORE_COMPLETION_ARM | session=%s q=%d — core "
                "question aired; window opens with options still reading "
                "(C3a)",
                self.sk.session_id, qnum,
            )
            # C14b: the core sentence has (estimatedly) finished airing —
            # the first of the four persisted delivery timestamps.
            self.sk.note_question_time("core_sentence_spoken_at")
            self.open_window_after_discharge(core_completion=True)

        asyncio.ensure_future(_arm())

    def _seg_answer_shaped(self, seg: dict) -> bool:
        """Is this captured final an answer ATTEMPT at the live question?

        Thin adapter over lily_evaluation.lily_answer_shaped: it supplies the
        armed question and the control-command flag (a "skip"/"back to
        normal" during a read is a command, never an answer — the same
        exclusion the in-window candidate recorder already applies in
        lily_scorekeeper.on_transcript_segment)."""
        armed = self.armed_question
        if armed is None:
            return False
        text = seg.get("text") or ""
        try:
            is_command = (
                lily_scorekeeper.lily_detect_control_command(text) is not None
            )
            return lily_evaluation.lily_answer_shaped(
                text, armed, is_command=is_command
            )
        except Exception as e:
            # Never let a shape check take down capture. Failing OPEN here
            # would re-open the Session A hole, so it fails CLOSED: the
            # utterance stays conversational.
            logger.warning("LILY_ANSWER | answer-shape check failed: %s", e)
            return False

    def _mc_choice_airing_index(self, now: float) -> int | None:
        """Which choice index (0-3) was on the air at `now`, or None if the
        read is not estimably inside the options yet.

        There is no per-sentence playout signal at livekit-agents 1.6.x —
        the whole stem+options delivery is ONE SpeechHandle (documented at
        lily_config.mc_stem_protect_words_per_second). WS-5 already had to
        model the stem/options boundary from a words-per-second estimate to
        keep the stem protected; resuming from the interrupted choice needs
        the same estimate carried one step further, per choice, so this
        reuses that knob rather than adding a second timing model or new
        playout instrumentation.

        Returns the index whose read had STARTED but (by estimate) not
        finished. A cut past the last choice returns 3 — nothing is owed."""
        started = self._mc_delivery_started_at
        if started is None:
            return None
        wps = lily_config.mc_stem_protect_words_per_second()
        if wps <= 0:
            return None
        armed = self.armed_question or {}
        choices = armed.get("choices")
        if not isinstance(choices, list) or not choices:
            return None
        words_aired = max(0.0, (now - started)) * wps
        # The stem is spoken first and is protected; anything inside it means
        # no choice has begun.
        cursor = float(self._mc_delivery_stem_words)
        if words_aired < cursor:
            return None
        for index, choice in enumerate(choices):
            # +1 for the spoken letter label ("B)") that rendered_armed_
            # question puts in front of every option.
            cursor += 1.0 + len(str(choice).split())
            if words_aired < cursor:
                return index
        return len(choices) - 1

    def rendered_armed_choices_from(self, index: int) -> str:
        """The armed question's choices from `index` onward, in the SAME
        deterministic spoken form rendered_armed_question uses (shared
        MC_CHOICE_LETTERS labels) — the resume sheet for a read that was cut
        part-way through the options."""
        armed = self.armed_question or {}
        choices = armed.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        labels = lily_evaluation.MC_CHOICE_LETTERS
        start = max(0, min(int(index), len(labels) - 1))
        return "\n".join(
            f"{labels[i]}) {choice}"
            for i, choice in enumerate(choices[: len(labels)])
            if i >= start
        )

    def arm_delivery_resume(self, text: str) -> None:
        """Stage the EXACT text the resumed read must speak. Consumed once,
        in tts_node, before the delivery-claim decision — so the resume is
        verbatim by construction and does not depend on the model choosing
        to honour a 'pick up where you left off' directive (the existing
        _REGEN_DELIVERY_DIRECTIVE is model-mediated, and Y7 disarms the gate
        that would carry it on a barge-in anyway)."""
        self._pending_delivery_resume = (text or "").strip() or None

    def take_pending_delivery_resume(self) -> str | None:
        """One-shot consume of the staged resume text (tts_node)."""
        text = self._pending_delivery_resume
        self._pending_delivery_resume = None
        return text

    def mcq_barge_resume(self, now: float) -> bool:
        """HOSTLOOP-001 C3c — a NON-answer-shaped barge during a question
        read: cancel the TTS and RESUME from the interrupted choice.

        This is the deliberate carve-out from Y7 (HOTFIX-007), and it is
        scoped exactly to phase=question / delivery=active. Y7's finding
        stands everywhere else: a barge-in is a CANCEL, she yields the floor,
        nothing brings the killed line back. But a question read is not a
        conversational line — it is an OBLIGATION. Session B (lily-05BB92)
        is what Y7's policy does to it: the barge cancelled the read, no
        resume was armed, the choices never aired, and the question sat
        half-delivered with nothing pending. Y7 stays as written; questions
        get their floor back.

        HOSTLOOP-001 C8 generalizes the same discipline to the rest of
        phase=question. When there is no per-choice position to resume from
        — a FREEFORM question read, or any other phase=question host beat
        cut while the armed question has not landed — the read cannot be
        picked up mid-list, so the question is RE-OFFERED whole from the
        deterministic sheet instead. Resume where a resume exists, re-offer
        where it does not; either way the question never ends half-aired.

        Returns True if a resume or re-offer was armed and dispatched."""
        qnum = self.sk.question_number
        index = self._mc_choice_airing_index(now)
        remaining = (
            self.rendered_armed_choices_from(index) if index is not None else ""
        )
        if remaining:
            logger.info(
                "LILY_BARGE | QUESTION_RESUME | session=%s q=%d from_choice=%s "
                "— non-answer barge during the read; cancelling TTS and "
                "resuming the options from here (C3c carve-out from Y7 "
                "BARGE_IN_CANCEL)",
                self.sk.session_id, qnum,
                lily_evaluation.MC_CHOICE_LETTERS[index:index + 1] or index,
            )
            instructions = (
                "You were talked over part-way through reading the options. "
                "Do not start the question again — the table already has the "
                "stem and the options before this one. Read exactly the "
                "remaining options, nothing else, then stop and let them "
                "answer:\n" + remaining
            )
            resume_text = remaining
            source = "mcq_barge_resume"
        else:
            # C8 re-offer arm. No estimable position inside the options (a
            # freeform read, a cut before the first option, or a
            # phase=question beat that was not the read at all), so the whole
            # armed question goes back out — the same deterministic sheet the
            # strict-rewrite path already speaks, never a model paraphrase.
            resume_text = (self.rendered_armed_question() or "").strip()
            if not resume_text:
                return False
            logger.info(
                "LILY_BARGE | QUESTION_REOFFER | session=%s q=%d — "
                "phase=question host speech cut by a barge with no resume "
                "point; re-offering the armed question whole (C8)",
                self.sk.session_id, qnum,
            )
            instructions = (
                "You were talked over before the table had the question. "
                "Ask it again now — its question sentence exactly as "
                "written, with every option when there are options — then "
                "stop and let them answer:\n" + resume_text
            )
            source = "question_barge_reoffer"
        self._interrupt_current_speech()
        self.arm_delivery_resume(resume_text)
        self.expect_delivery()
        self.gated_say(
            None, "question_nudge", instructions, source=source,
        )
        self._delivery_barge_cut_qnum = None
        return True

    def _question_owed_recovery(self, released: "list | None" = None) -> bool:
        """HOSTLOOP-001 C8 — is a phase=question host utterance that was just
        barge-cut owed a resume or a re-offer?

        C3c asked only "was an MC options read in flight?", which left the
        FREEFORM read (and a cut nudge that owned the delivery) falling
        through to Y7's blanket cancel. The generalized test is "the room
        does not have this question yet, and the speech that just died was
        the speech that owed it":

          * phase=question, a question armed, the game live;
          * the delivery claim for it is NOT confirmed — a question the room
            demonstrably heard is never re-read (delivery_reached_the_table
            force-confirms exactly that case one branch above in
            on_agent_speech_finished, so it self-excludes here);
          * and the dead turn owed the question: it held the delivery claim
            that was just released, or an MC read is still marked in flight.

        The last clause is what keeps this from becoming a floor grab: a cut
        conversational turn that never owed the question arms nothing, and
        the pre-existing window-fallback nudge / idle watchdog still own a
        question that stalls for any other reason."""
        if self._delivery_stop_sticky:
            return False
        if not getattr(self, "game_started", False):
            return False
        if getattr(self, "game_over", False):
            return False
        if self.armed_question is None:
            return False
        if getattr(self, "ui_phase", None) != "question":
            return False
        qnum = self.sk.question_number
        delivery_key = f"q_{qnum}_delivery"
        if (
            self.say_registry.state(delivery_key)
            == lily_say_gate.CLAIM_CONFIRMED
        ):
            return False
        if self._mc_delivery_qnum == qnum:
            return True
        return delivery_key in (released or [])

    def note_question_barge_cut(self, qnum: int) -> None:
        """A question delivery was cut by a DELIBERATE barge-in and its
        window never opened. Called from on_agent_speech_finished, at the
        exact point where Y7 decides the cut cause — so the carve-out reads
        Y7's own decision instead of re-deriving it.

        Marks the question as owing either a binding or a resume, and arms
        the fallback. The fallback matters because the utterance that caused
        the barge may never reach us: the framework drops a transcript that
        falls inside `ignore_user_transcript_until` (Y7's own docstring
        records this), so "wait for the final and then decide" cannot by
        itself satisfy the C3d invariant."""
        self._delivery_barge_cut_qnum = qnum
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # Offline/fixture context: the marker is set and the fixtures
            # drive the decision paths directly.
            return
        asyncio.ensure_future(self._question_barge_resume_watch(qnum))

    def _question_barge_resume_still_owed(self, qnum: int) -> bool:
        """True when question `qnum` is still half-aired with nothing
        pending: barge-cut, no answer bound, no resume already dispatched.
        This IS the C3d invariant, expressed once, in one place.

        Note what is NOT a guard here: an OPEN answer window. Under C3a the
        window opens at core completion, so a barge during the options read
        lands with the window already open — and an open window does not
        discharge the obligation to finish reading the choices the table was
        promised. Only a bound answer, or a resume already out, does."""
        if self._delivery_barge_cut_qnum != qnum:
            return False
        if qnum != self.sk.question_number:
            return False
        if self._adjudicating:
            return False
        if self._delivery_stop_sticky:
            return False
        if getattr(self, "game_over", False):
            return False
        if self.armed_question is None:
            return False
        # An answer that already bound sits either in the pre-window buffer
        # (window not yet open) or in the window's candidates (C3a span).
        if self._pre_window_segments or []:
            return False
        try:
            if self.sk.ordered_candidates():
                return False
        except Exception:
            pass
        return True

    async def _question_barge_resume_watch(self, qnum: int) -> None:
        """Fallback arm of the C3d invariant: if the barge's own utterance
        never arrives to be judged, resume the read anyway. Grace matches the
        cut-recovery watchdog's (existing knob), so a real answer landing
        just after the barge still wins the race and binds.

        BARGE-RESILIENCE-001 P2 (R3) — barge-to-ask ordering. A barge that is
        a QUESTION to the host ("wait, what are the rules?") leaves the read
        owed AND makes her speak an answer. The resume must land AFTER that
        answer, not race it: while she is on the air (host_speaking) the
        resume DEFERS one grace and re-checks, so it picks up the read only
        once she has finished replying. Bounded so the watchdog never leaks."""
        for _ in range(_BARGE_RESUME_MAX_DEFERRALS):
            await asyncio.sleep(lily_config.cut_recovery_grace())
            if not self._question_barge_resume_still_owed(qnum):
                return
            # R3: she is answering the interjected question right now — the
            # pending read resumes after her reply, never over it.
            if getattr(self.sk, "host_speaking", False):
                continue
            logger.warning(
                "LILY_BARGE | QUESTION_RESUME_FALLBACK | session=%s q=%d — "
                "barge utterance never reached adjudication; resuming the read "
                "so the question cannot end half-aired (C3d)",
                self.sk.session_id, qnum,
            )
            self.mcq_barge_resume(time.time())
            return

    def _maybe_resume_mcq_read(
        self, seg: dict, *, now: float | None = None
    ) -> bool:
        """C3c decision for one captured final during an MC question read.

        Only a NON-answer-shaped utterance gets here (the answer-shaped case
        bound in mc_early_answer_check). Resumes only when the read is
        genuinely dead — `note_question_barge_cut` marked it — so a read that
        is still airing, or that completed and is merely waiting out the
        room-discharge gap, is never re-read."""
        ref = now if now is not None else time.time()
        qnum = self.sk.question_number
        if not self._question_barge_resume_still_owed(qnum):
            return False
        if self._seg_answer_shaped(seg):
            # Answer-shaped but unbindable here (stem still protected, or
            # aborts-read disabled). Leave the marker: the fallback resolves
            # it, and the pre-window buffer still carries the answer.
            return False
        return self.mcq_barge_resume(ref)

    def _note_mc_delivery_start(self, qnum: int) -> None:
        """WS-5: mark a multiple-choice delivery (stem+options, one turn)
        in flight so a correct answer during the options read can truncate
        it. Records the playout-start clock and the stem word count for the
        stem-protection model. No-op (and clears any prior MC flag) for a
        freeform delivery — nothing to truncate there.

        The qnum is armed at claim, but started_at remains unset until
        note_playout_started receives the framework's speaking transition.
        A queued delivery claim is not an audible question."""
        armed = self.armed_question or {}
        choices = armed.get("choices")
        if not isinstance(choices, list) or len(choices) != 4:
            self._mc_delivery_qnum = None
            self._mc_delivery_started_at = None
            return
        self._mc_delivery_qnum = qnum
        self._mc_delivery_started_at = None
        self._mc_delivery_stem_words = len(
            str(armed.get("prompt") or "").split()
        )

    def _mc_stem_protected(self, now: float) -> bool:
        """WS-5: True while the MC stem is still (estimated to be) reading —
        the protected span. An early answer cannot truncate the question
        until the whole table has heard it."""
        started = self._mc_delivery_started_at
        if started is None:
            return True
        wps = lily_config.mc_stem_protect_words_per_second()
        if wps <= 0:
            return True
        stem_words = self._mc_delivery_stem_words
        return now < started + (stem_words / wps)

    def _interrupt_current_speech(self) -> None:
        """WS-5: halt the agent's current speech (the in-flight MC delivery),
        truncating whatever options remain. AgentSession.interrupt() at
        1.6.6 interrupts the active agent speech and returns a Future we do
        not await. Guarded: a race where nothing is speaking is a no-op."""
        session = getattr(self, "session", None)
        if session is None:
            return
        try:
            session.interrupt()
        except Exception as e:
            logger.warning("LILY_MC | interrupt failed: %s", e)

    def mc_early_answer_check(
        self,
        seg: dict,
        *,
        now: float | None = None,
        nbest: dict | None = None,
    ) -> bool:
        """WS-5 answer-aborts-read. A final landing DURING an in-flight MC
        options read that Tier-1-matches a read option (letters, positions,
        option text) or the canonical set TRUNCATES the remaining options
        and jumps to adjudication — the point goes to the answerer and
        options 3-4 never air. Returns True if it aborted the read.

        The STEM stays protected (_mc_stem_protected): an early answer
        cannot cut the question before the table has heard it. A protected
        or non-matching final does NOT abort — it still buffers via
        buffer_pre_window_answer and scores at the normal window open.

        Diagnosis (recorded in ws5-report): barge-in is NOT structurally
        disabled on MC deliveries — interruptions are enabled globally
        (min_words=1, min_duration=0.8) and the delivery is one interruptible
        SpeechHandle. What made barge-in look dead was (a) an interrupted
        delivery released its q_{N}_delivery claim and re-armed, so a
        resolution path re-dispatched the SAME read verbatim (the "aired
        twice" evidence — WS-3 owns the no-verbatim-replay gate), and (b)
        the window opened only at FULL playout, so the answer that caused
        the barge landed in a closed window. This path halts the read AND
        adjudicates the answer, so it neither replays nor goes inert."""
        if not lily_config.mc_answer_aborts_read():
            return False
        qnum = self._mc_delivery_qnum
        if qnum is None:
            return False
        if self.armed_question is None:
            return False
        # C3a: the window may legitimately be OPEN while the options are
        # still reading. That used to be impossible, so an open window meant
        # "the read is over, nothing to truncate" and this returned. Now the
        # open window is the normal state for the second half of an MC read:
        # the answer is already recorded as an in-window candidate by the
        # scorekeeper (it runs ahead of this fork), so all that is owed here
        # is TRUNCATION — recorded in `already_open` and honoured below.
        already_open = bool(self.sk.answer_window_open)
        if already_open and self._mc_delivery_qnum != qnum:
            return False
        if self._adjudicating:
            return False
        if not self._segment_overlaps_active_delivery(seg):
            return False
        ref = now if now is not None else time.time()
        if self._mc_stem_protected(ref):
            return False
        text = seg.get("text") or ""
        question = self.armed_question
        try:
            hyps = (nbest or {}).get("hypotheses") or []
            if nbest is not None and len(hyps) > 1:
                verdict = lily_evaluation.lily_tier1_evaluate_nbest(
                    text, question, hypotheses=hyps
                )["verdict"]
            else:
                verdict = lily_evaluation.lily_tier1_evaluate_question(
                    text, question
                )["verdict"]
        except Exception as e:
            logger.warning("LILY_MC | early-answer eval failed: %s", e)
            return False
        # HOSTLOOP-001 C3b: an ANSWER-SHAPED barge binds, right or wrong.
        #
        # WS-5 required verdict == "correct" here, which was sound for its
        # own goal (only a provably right answer may cut options short) but
        # left the wrong-but-committed pick with nowhere to go: "B" or
        # "Sydney" mid-read returned False, so the utterance never bound, and
        # the barge that produced it was handed to Y7 as an ordinary cancel.
        # Session B (lily-05BB92) is that gap — answer window closed, floor
        # yielded, choices dead, nothing re-aired.
        #
        # MC Tier-1 resolves a SELECTION independently of correctness, so
        # "answer-shaped" is a strictly wider set than "correct" and the
        # verdict is still what adjudication uses. A resolved wrong pick is
        # a committed answer; it ends the read and goes to verdict.
        if verdict != "correct" and not self._seg_answer_shaped(seg):
            return False
        logger.info(
            "LILY_MC | ANSWER_ABORTS_READ | session=%s q=%d speaker=%s "
            "verdict=%s — answer-shaped utterance during options read; "
            "binding it, truncating remaining options, adjudicating (C3b)",
            self.sk.session_id, qnum, seg.get("speaker_label"), verdict,
        )
        # Clear the flag first so a second final in the same turn cannot
        # re-enter, then halt the read and open the window early. Seeding
        # this answer as a pre-window segment + open replays it as the first
        # candidate and runs the same instant Tier-1 fast path a live
        # in-window final gets — adjudicate (steal_allowed=False). The
        # truncation's on_agent_speech_finished (interrupted=True) then
        # no-ops: expect_delivery skips (window open) and the window-open
        # block skips (window open).
        #
        # Seed DIRECTLY (not via buffer_pre_window_answer): if a framework
        # barge already fired and released the q_{N}_delivery claim before
        # this final arrived, the buffer's claim-present guard would drop a
        # confirmed-correct answer. We have already validated it here.
        self._mc_delivery_qnum = None
        # C3b/C3d: this question is answered — it can no longer be owed a
        # resume, whichever way the barge is reported.
        self._delivery_barge_cut_qnum = None
        self._interrupt_current_speech()
        # HOSTLOOP-001 C6: the SECOND deterministic fork an answer can bind
        # through. When the barge lands before the window opened, the
        # scorekeeper recorded no candidate, so on_transcript_event's receipt
        # seam never sees this utterance — yet this is the fastest binding in
        # the system and the one most owed an immediate word. The Tier-1
        # verdict is already in hand above; dedupe is by (who, what) so the
        # already_open case does not receipt twice.
        self.fire_answer_receipt(
            verdict, text=text,
            player=self._label_owner(seg.get("speaker_label")),
            source="mc_early_answer",
        )
        if already_open:
            # C3a span: the window is live and the scorekeeper already
            # recorded this utterance as a candidate. Truncate the read and
            # go to verdict — re-seeding + re-opening would double-count it.
            self._active_delivery_qnum = None
            self._active_delivery_started_at = None
            self._active_delivery_ended_at = None
            if not self._adjudicating:
                asyncio.ensure_future(self.adjudicate(steal_allowed=False))
            return True
        buf = self._pre_window_segments
        if buf is None:
            buf = []
            self._pre_window_segments = buf
        buf.append(dict(seg))
        del buf[:-6]
        self.open_window()
        # C3b: "bind it, skip the remaining choices, PROCEED TO VERDICT".
        # open_window's replay runs the instant Tier-1 fast path only for a
        # CORRECT answer — right for the ordinary early-buzz case it was built
        # for, but a committed WRONG pick would then sit out the whole window
        # clock after the read had already been cut short: dead air on a
        # question that is, as far as the table is concerned, over. The
        # correct case is left exactly as WS-5 had it (the replay adjudicates
        # it) so this never double-fires.
        if verdict != "correct" and not self._adjudicating:
            asyncio.ensure_future(self.adjudicate(steal_allowed=False))
        return True

    def early_answer_check(
        self,
        seg: dict,
        *,
        now: float | None = None,
        nbest: dict | None = None,
    ) -> bool:
        """Let a shouted answer end any in-flight question read.

        Multiple choice keeps its existing stem-protection rule. Freeform
        questions intentionally allow experts to jump on an early clue; only
        a deterministic Tier-1 correct match truncates the read, so table
        chatter and wrong guesses do not prematurely end the question.

        HOSTLOOP-001 C3: this is the barge fork for phase=question /
        delivery=active, and it now has BOTH arms the clause requires. An
        answer-shaped utterance binds (C3b, below / mc_early_answer_check).
        An MC barge that is NOT answer-shaped no longer just falls through to
        Y7's cancel — it resumes the read from the interrupted choice (C3c,
        mcq_barge_resume), which is what keeps the C3d invariant true: a
        question can never end half-aired with neither an answer bound nor a
        resume pending. Returns True when the read was ended by a binding
        answer (the caller then skips pre-window buffering); a RESUME returns
        False, because nothing was bound and the utterance stays ordinary
        conversation.
        """
        armed = self.armed_question or {}
        choices = armed.get("choices")
        if isinstance(choices, list) and len(choices) == 4:
            aborted = self.mc_early_answer_check(seg, now=now, nbest=nbest)
            if aborted:
                self.mark_deterministic_reply(seg.get("text") or "")
                return True
            self._maybe_resume_mcq_read(seg, now=now)
            return False
        qnum = self._active_delivery_qnum
        if qnum is None or qnum != self.sk.question_number:
            return False
        if not self._segment_overlaps_active_delivery(seg):
            return False
        if self.sk.answer_window_open or self._adjudicating:
            return False
        try:
            hyps = (nbest or {}).get("hypotheses") or []
            if nbest is not None and len(hyps) > 1:
                verdict = lily_evaluation.lily_tier1_evaluate_nbest(
                    seg.get("text") or "", armed, hypotheses=hyps
                )["verdict"]
            else:
                verdict = lily_evaluation.lily_tier1_evaluate_question(
                    seg.get("text") or "", armed
                )["verdict"]
        except Exception as e:
            logger.warning("LILY_BARGE | early-answer eval failed: %s", e)
            return False
        if verdict != "correct":
            return False
        logger.info(
            "LILY_BARGE | ANSWER_ABORTS_READ | session=%s q=%d speaker=%s",
            self.sk.session_id, qnum, seg.get("speaker_label"),
        )
        self.mark_deterministic_reply(seg.get("text") or "")
        self._active_delivery_qnum = None
        self._active_delivery_started_at = None
        self._active_delivery_ended_at = None
        self._interrupt_current_speech()
        buf = self._pre_window_segments
        if buf is None:
            buf = self._pre_window_segments = []
        buf.append(dict(seg))
        del buf[:-6]
        self.open_window()
        return True

