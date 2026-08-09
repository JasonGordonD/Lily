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
_PROGRESSION_ACTS = ("question_delivery", "question_nudge")



class LilySpeechDeliveryMixin:
    """Mixin: speech/delivery methods for LilyGame."""

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
                if getattr(self, "_awaiting_address_since", 0.0)
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
                if getattr(self, "_delivery_stop_sticky", False)
                else "no_live_game"
            )
            logger.warning(
                "LILY_SAY_SUPPRESSED | reason=%s | act=%s | "
                "source=%s | game_started=%s q=%d window=%s",
                reason, act, source, getattr(self, "game_started", False),
                self.sk.question_number, self.sk.answer_window_open,
            )
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
        if self.take_reair_dispatch():
            if act in ("question_delivery", "question_nudge"):
                instructions = instructions + _REGEN_DELIVERY_DIRECTIVE
            else:
                instructions = instructions + _REGEN_REAIR_DIRECTIVE
        logger.info(
            "LILY_SAY | act=%s | key=%s | source=%s", act, key or "-", source
        )
        handle = self.instructed_reply(instructions)
        speech_id = getattr(handle, "id", None)
        if speech_id:
            self.say_registry.reassign_owner(reservation, speech_id)
            if act in (
                "question_delivery",
                "question_nudge",
                "game_start",
                "skip",
            ):
                delivery_acts = getattr(self, "_delivery_speech_acts", None)
                if delivery_acts is None:
                    delivery_acts = self._delivery_speech_acts = {}
                delivery_acts[speech_id] = act
        # WO-LILY-HOTFIX-001: every keyed act gets a playout watchdog. If
        # this claim is still PENDING past the deadline with its speech
        # never having started airing (the Krisp RoomIO wedge: no playout,
        # no failure event, no confirm, no release), the watchdog releases
        # the claim and re-dispatches — silence is her failure mode and a
        # frozen claim must never enforce it.
        if key is not None:
            self._arm_stale_claim_watchdog(key, act, instructions, source)
        return True

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
        started = getattr(self, "_playout_started_ids", set())
        if owner is not None and owner in started:
            return False  # airing (long turn mid-playout) — leave it alone
        if getattr(self.sk, "host_speaking", False):
            return False  # something IS on the air — not the deaf case
        return self.say_registry.release(key)

    def _arm_stale_claim_watchdog(
        self, key: str, act: str, instructions: str, source: str
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
            self._stale_claim_watch(key, owner, act, instructions, source)
        )

    async def _stale_claim_watch(
        self, key: str, owner: str, act: str, instructions: str, source: str
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
            if owner in getattr(self, "_playout_started_ids", set()):
                return
            if getattr(self.sk, "host_speaking", False):
                continue
            age = self.say_registry.pending_age(key) or 0.0
            counts = getattr(self, "_stale_retry_counts", None)
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
                key, act, instructions, source=f"{source}+stale_retry"
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
        started = getattr(self, "_playout_started_ids", None)
        if started is None:
            started = self._playout_started_ids = set()
        started.add(speech_id)
        if getattr(self, "_awaiting_address_since", 0.0):
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
            if getattr(self, "_mc_delivery_qnum", None) == self.sk.question_number:
                self._mc_delivery_started_at = now
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
        if getattr(self, "_delivery_stop_sticky", False):
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
            if getattr(self, "_awaiting_address_since", 0.0)
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
        cuts = getattr(self, "_delivery_cuts", None)
        if cuts is None or getattr(self, "_delivery_cuts_qnum", None) != qnum:
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
        return getattr(self, "_reair_gate_armed", False)

    def take_reair_dispatch(self) -> bool:
        """Consume the re-air arm at DISPATCH time and hand the signal on
        to tts_node via _reair_turn_pending. True = this dispatch is a
        re-air and must carry a regeneration directive."""
        if not getattr(self, "_reair_gate_armed", False):
            return False
        self._reair_gate_armed = False
        self._reair_turn_pending = True
        return True

    def take_reair_turn(self) -> bool:
        """Consume the re-air signal at PLAYOUT (tts_node). True = the
        outbound turn is a re-air and its verbatim-replay lint is a GATE,
        not telemetry."""
        pending = getattr(self, "_reair_turn_pending", False)
        self._reair_turn_pending = False
        return pending

    def is_question_delivery_turn(self, spoken_text: str) -> bool:
        """True when the outbound turn is performing the armed question. A
        barged re-read of the question is CORRECT verbatim (players need
        the whole question), so it is exempt from the conversational
        regeneration gate (WS-3)."""
        if getattr(self, "_pending_delivery_qnum", None) is not None:
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

    def _cut_recovery_should_arm(
        self, released: "list | None", interrupted: bool, failed: bool
    ) -> bool:
        """Arm the auto-resume only for a cut/failed ORGANIC turn. A keyed
        game act (released non-empty) recovers through the game loop; the
        answer window / adjudication own their own timing; a finished game
        has nothing to resume."""
        if not (interrupted or failed):
            return False
        if released:  # a keyed game act — the game loop owns its re-dispatch
            return False
        if getattr(self, "game_over", False):
            return False
        if getattr(self, "_delivery_stop_sticky", False):
            return False
        if getattr(self.sk, "answer_window_open", False):
            return False
        return not getattr(self, "_adjudicating", False)

    def arm_cut_recovery(self, tail_text: str) -> None:
        """Schedule the auto-resume watchdog for a cut organic turn. Bumps
        the recovery token so any earlier watchdog is superseded and stamps
        the arm time (the user-turn recency guard keys off it). No-op
        without a running loop — offline tests drive _cut_recovery_should_fire
        / trigger_cut_recovery directly."""
        self._cut_recovery_token = getattr(self, "_cut_recovery_token", 0) + 1
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
        self._cut_recovery_token = getattr(self, "_cut_recovery_token", 0) + 1

    def note_user_turn(self) -> None:
        """Stamp the last user-turn time. A user turn near a cut means the
        room re-engaged on its own — the normal reply path owns the
        recovery, so the auto-resume watchdog stands down (no double-speak)."""
        self._last_user_turn_at = time.monotonic()
        self.cancel_cut_recovery()

    def _cut_recovery_should_fire(self, token: int) -> bool:
        """True only if this token's watchdog is still live AND the cut left
        genuine dead air: not superseded, nobody speaking, no user turn in
        the lookback window (a real barge-in carried content the normal path
        answers), game still live and out of a scoring window."""
        if getattr(self, "_cut_recovery_token", 0) != token:
            return False  # superseded by a newer cut or an explicit cancel
        if getattr(self.sk, "host_speaking", False):
            return False  # audio already resumed / a new turn is airing
        armed_at = getattr(self, "_cut_recovery_armed_at", 0.0)
        last_user = getattr(self, "_last_user_turn_at", 0.0)
        if last_user >= armed_at - _CUT_RECOVERY_USER_TURN_LOOKBACK:
            # A user turn landed around/after the cut — a genuine barge with
            # content; the normal reply path (re-air-gated fresh) owns it.
            return False
        if getattr(self, "game_over", False):
            return False
        if getattr(self.sk, "answer_window_open", False):
            return False
        return not getattr(self, "_adjudicating", False)

    def trigger_cut_recovery(self) -> bool:
        """Dispatch the fresh auto-resume. Arms the re-air gate first so the
        resume regenerates rather than replays (the shared WS-3 gate), then
        fires the cut-recovery directive through the between-turns speech
        path. Returns True if a resume was dispatched."""
        self.arm_reair_gate()
        logger.warning(
            "LILY_CUT_RECOVERY | RESUMED | session=%s — cut turn left dead "
            "air, auto-resuming fresh (no operator poke)",
            self.sk.session_id,
        )
        handle = self.instructed_reply(_CUT_RECOVERY_DIRECTIVE)
        return handle is not None

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
          None                   — not a delivery event; speak normally.
        """
        if getattr(self, "_delivery_stop_sticky", False):
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
        delivery_acts = getattr(self, "_delivery_speech_acts", None) or {}
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
                if ratio >= 0.9:
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
            if ratio >= 0.9:
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
        qnum = getattr(self, "_active_delivery_qnum", None)
        if qnum is None or qnum != self.sk.question_number:
            return False
        started = getattr(self, "_active_delivery_started_at", None)
        if started is None:
            return False
        try:
            seg_start = float(seg["segment_start_time"])
            seg_end = float(seg.get("segment_end_time", seg_start))
        except (KeyError, TypeError, ValueError):
            return False
        ended = getattr(self, "_active_delivery_ended_at", None)
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
        lobby chatter and post-window banter never buffer."""
        if (
            getattr(self, "_delivery_stop_sticky", False)
            or self.sk.answer_window_open
            or self.armed_question is None
            or not self._segment_overlaps_active_delivery(seg)
        ):
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
                # assume_in_window: buffered early answers were spoken
                # BEFORE the window opened by design — spoken-time
                # membership (WS-10) would reject them; the buffering
                # already gated on the armed question's claimed delivery.
                last_result = self.sk.on_transcript_segment(
                    is_final=True, now=replay_ts, assume_in_window=True,
                    **seg
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

    def note_recent_final(self, seg: dict, ts: float) -> None:
        """WS-5 buzz-buffer widening: remember recent finals so one that
        landed just BEFORE the delivery claim can be back-filled into the
        pre-window buffer at claim time. Trimmed to buzz_prewindow_seconds()
        (and a hard 12-item cap) so it never grows without bound."""
        buf = getattr(self, "_recent_finals", None)
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
        started = getattr(self, "_mc_delivery_started_at", None)
        if started is None:
            return True
        wps = lily_config.mc_stem_protect_words_per_second()
        if wps <= 0:
            return True
        stem_words = getattr(self, "_mc_delivery_stem_words", 0)
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
        qnum = getattr(self, "_mc_delivery_qnum", None)
        if qnum is None:
            return False
        if self.armed_question is None or self.sk.answer_window_open:
            return False
        if getattr(self, "_adjudicating", False):
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
        if verdict != "correct":
            return False
        logger.info(
            "LILY_MC | ANSWER_ABORTS_READ | session=%s q=%d speaker=%s — "
            "correct answer during options read; truncating options and "
            "adjudicating",
            self.sk.session_id, qnum, seg.get("speaker_label"),
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
        self._interrupt_current_speech()
        buf = self._pre_window_segments
        if buf is None:
            buf = []
            self._pre_window_segments = buf
        buf.append(dict(seg))
        del buf[:-6]
        self.open_window()
        return True

    def early_answer_check(
        self,
        seg: dict,
        *,
        now: float | None = None,
        nbest: dict | None = None,
    ) -> bool:
        """Let a shouted correct answer end any in-flight question read.

        Multiple choice keeps its existing stem-protection rule. Freeform
        questions intentionally allow experts to jump on an early clue; only
        a deterministic Tier-1 correct match truncates the read, so table
        chatter and wrong guesses do not prematurely end the question.
        """
        armed = self.armed_question or {}
        choices = armed.get("choices")
        if isinstance(choices, list) and len(choices) == 4:
            aborted = self.mc_early_answer_check(seg, now=now, nbest=nbest)
            if aborted:
                self.mark_deterministic_reply(seg.get("text") or "")
            return aborted
        qnum = getattr(self, "_active_delivery_qnum", None)
        if qnum is None or qnum != self.sk.question_number:
            return False
        if not self._segment_overlaps_active_delivery(seg):
            return False
        if self.sk.answer_window_open or getattr(self, "_adjudicating", False):
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

