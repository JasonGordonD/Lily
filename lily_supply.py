"""LilySupply -- question supply: prefetch, reserve, draw, bank, arsenal, burn (W3 Cut 2).

INVARIANT: there is always a truthful answer to "is a deliverable question in
hand?" -- armed / prefetched / depth-2 reserve / curated bank / arsenal
pipeline -- ids unique and burn-once. Byte-identical MOVE from LilyGame (self.*
unchanged). The watchdog POLICY rows stay in the director with the WatchPolicy
table; only their supply CALL TARGETS live here. Reserve fields stay in
LilyGame.__init__ until the bare()/getattr-fog step."""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid

import lily_arsenal
import lily_bank
import lily_bank_tuning
import lily_config
import lily_evaluation
import lily_images
import lily_persistence
import lily_reasoning
import lily_say_gate
import lily_scorekeeper

import logging
logger = logging.getLogger("lily_agent")


class LilySupplyMixin:
    def next_question_ready(self) -> bool:
        """WS-6 seam predicate (published as the `next_question_ready`
        attribute). True when a deliverable question is in hand — armed
        (playing or awaiting) or prefetched and ready to arm — or a ruling
        is in flight. False ONLY in the live supply-stall state: the game
        is running, the window is closed, no ruling is adjudicating or
        transitioning, and neither an armed nor a prefetched question
        exists — the starvation the 583a0f16 session sat in with no screen
        cue. Pre-game and post-game report ready (the lobby/final screens
        carry their own state, not a supply cue)."""
        if getattr(self, "_delivery_stop_sticky", False):
            return False
        if not getattr(self, "game_started", False) or getattr(
            self, "game_over", False
        ):
            return True
        if (
            getattr(self, "armed_question", None) is not None
            or getattr(self, "next_question", None) is not None
        ):
            return True
        if (
            self.sk.answer_window_open
            or getattr(self, "_adjudicating", False)
            or getattr(self, "_question_transitioning", False)
        ):
            return True
        return False

    def custom_round_registrations(self, category: str) -> list:
        """Question ids registered for `category` this session (read-only)."""
        key = lily_bank.lily_normalize_category_name(category)
        return list(
            getattr(self, "_custom_round_registered", {}).get(key, [])
        )

    def custom_round_unbuilt_topics(self) -> list:
        """Every topic this session has been asked for that has NO registered
        question behind it: the ones still building, and the ones already
        refused. Saying "your Cape Cod round" about anything on this list is
        a fabrication — it is the read the state block and the divergence
        detector share.

        Refused topics stay on the list for the whole session on purpose.
        The second live line ("I'm putting your round together right now")
        landed AFTER the round had already failed to materialise, so a check
        that only watched the build window would have missed half the
        defect."""
        override = getattr(self, "_category_override", {})
        unbuilt = [
            topic for topic in override.values()
            if not self.custom_round_registrations(topic)
        ]
        for topic in getattr(self, "_custom_round_refused", []):
            if topic not in unbuilt and not self.custom_round_registrations(
                topic
            ):
                unbuilt.append(topic)
        return unbuilt

    async def build_custom_round(self, subject: str, rnd: int) -> dict:
        """Actually BUILD the round the table just asked for, and return the
        registration result the confirmation is allowed to read.

        This is the awaited part of lily_set_category. It runs the real
        supply path for the topic — strict bank draw first (a topic with
        banked questions serves them; the arsenal compounds), then the
        reasoning lane's generate+verify — and reports what registered.

        The wait is deliberate and it is the whole fix. The old tool returned
        "I'm generating those questions now" synchronously, which made the
        confirmation a statement about intent while the table heard it as a
        statement about the round. Nothing downstream ever checked, so when
        the supply path quietly served the generic deck instead, six generic
        questions went out under a Cape Cod banner.

        Bounded by lily_config.custom_round_build_seconds(). On timeout or
        failure the topic override is ROLLED BACK — a round she could not
        build must not keep running under its name — and the caller gets an
        empty registration, which can only produce a refusal."""
        subject = str(subject or "").strip()
        result = {"category": subject, "round": rnd, "registered": []}
        if not subject:
            return result
        budget = lily_config.custom_round_build_seconds()
        task = None
        try:
            self.start_prefetch()
            task = self._prefetch_task
            if task is not None and not task.done():
                # shield: wait_for cancels what it waits on, and a draw
                # killed mid-commit is worse than one left to finish (the
                # rollback below re-points the round, and the existing
                # CATEGORY_SWITCH_DISCARD guard drops the late arrival).
                await asyncio.wait_for(asyncio.shield(task), timeout=budget)
        except asyncio.TimeoutError:
            logger.error(
                "LILY_CUSTOM_ROUND | BUILD_TIMEOUT | session=%s topic=%r "
                "budget=%.1fs — refusing rather than narrating",
                self.sk.session_id, subject, budget,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "LILY_CUSTOM_ROUND | BUILD_FAILED | session=%s topic=%r",
                self.sk.session_id, subject,
            )
        result["registered"] = self.custom_round_registrations(subject)
        if result["registered"]:
            self.sk.clear_status_notes()
            return result
        # Nothing was built. Roll the round back to the fixed rotation so the
        # game cannot keep serving under a name she is about to disown, then
        # relaunch supply normally — the refusal offers the deck, so the deck
        # has to actually be coming.
        if getattr(self, "_category_override", {}).get(rnd) == subject:
            self._category_override.pop(rnd, None)
        refused = getattr(self, "_custom_round_refused", None)
        if refused is None:
            self._custom_round_refused = refused = []
        if subject not in refused:
            refused.append(subject)
        if task is not None and not task.done():
            task.cancel()
        self._prefetch_task = None
        self._prefetch_stall_ticks = 0
        self.sk.set_status_note(
            f"custom round NOT built: {subject!r} produced no registered "
            "question. Say plainly that you can't build it right now and "
            "offer a lane from the deck. Do NOT say you're putting it "
            "together, do not invent a question about it, and do not run "
            "the round under that name."
        )
        if self.game_started and not self.game_over:
            self.start_prefetch()
        logger.error(
            "LILY_CUSTOM_ROUND | BUILD_EMPTY | session=%s topic=%r round=%d "
            "— override rolled back, refusal is the only honest line",
            self.sk.session_id, subject, rnd,
        )
        return result

    def custom_round_state_line(self) -> str | None:
        """HOTFIX-006 N2, the X1-shaped half of the fix: the grounded read of
        every topic this session has been asked for, as a hard read-only
        state field.

        The tool is not the only channel — she can also just talk, and in
        lily-16A9AE she did ("I'm putting your round together right now").
        This is the truth sitting in front of that sentence."""
        override = getattr(self, "_category_override", {})
        refused = list(getattr(self, "_custom_round_refused", []))
        if not override and not refused:
            return None
        parts = []
        for rnd in sorted(override):
            topic = override[rnd]
            count = len(self.custom_round_registrations(topic))
            if count:
                parts.append(
                    f"{topic!r} round {rnd}: BUILT, {count} registered "
                    "question(s)"
                )
            else:
                parts.append(
                    f"{topic!r} round {rnd}: NOT BUILT — zero registered "
                    "questions"
                )
        for topic in refused:
            if self.custom_round_registrations(topic):
                continue  # asked again later and built that time
            parts.append(
                f"{topic!r}: NOT BUILT — you already told the table you "
                "can't build it"
            )
        return (
            "CUSTOM ROUNDS — AUTHORITATIVE, READ-ONLY (the registration "
            f"ledger, the ONLY truth about custom rounds): {'; '.join(parts)}. "
            "A round marked NOT BUILT does not exist: never say you are "
            "building it, putting it together, or working on it, and never "
            "ask a question you invented about it. Say plainly that you "
            "can't build it and offer a lane from the deck instead."
        )

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
        out. Failure writes an honest status note (§11.2). Z2 (HOTFIX-008):
        a supply-recovery retry sets `_prefetch_effort_override` (one-shot,
        consumed here) to de-escalate authoring effort on the retry draw."""
        effort = getattr(self, "_prefetch_effort_override", None)
        self._prefetch_effort_override = None
        if getattr(self, "_delivery_stop_sticky", False):
            return
        if self._prefetch_task and not self._prefetch_task.done():
            return
        if self.game_over:
            return
        # Depth-2 (W2b item 3a): draw while there is room in the hand — the
        # head OR the reserve is open. Only a full pair (head AND reserve)
        # short-circuits. Filling the head keeps its full commit machinery;
        # filling the reserve lands N+2 behind it (see the commit below).
        if (
            self.next_question is not None
            and getattr(self, "_next_question_reserve", None) is not None
        ):
            return

        async def _prefetch() -> None:
            # Z2: distinguishes a genuine supply failure (generation AND the
            # inline insurance produced nothing) from a deliberate discard
            # (mode/category switch, duplicate) — only the former self-heals.
            self._prefetch_supply_failed = False
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
                self._prefetch_supply_failed = True
            # Z2 (HOTFIX-008): the failure schedules its OWN recovery. The
            # 2260354c session proved the alternative — every recovery rung
            # lived behind the watchdog's idle guards, q1 stayed armed and
            # window-cycling all session, so a failed q2 prefetch was never
            # retried and the game starved with the bank full.
            if (
                getattr(self, "_prefetch_supply_failed", False)
                and self.next_question is None
                and self.game_started and not self.game_over
            ):
                self.ensure_supply_recovery(trigger="prefetch_failed")

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
            # double-draw ran through. WS-4 adds the revealed/burned set so
            # a timed-out-and-revealed question is never re-drawn either.
            if getattr(self, "_drawn_ids", None) is None:
                # Test harnesses build LilyGame via __new__ without the
                # full __init__ attribute set.
                self._drawn_ids = set()
                self._drawn_hashes = set()
            history_ids, history_hashes = self._no_repeat_exclusion()
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
                # Rung 1 — the standing arsenal (PATCH-003 binding A): a
                # pre-generated pair served with ZERO generation wait, so
                # pictures-on at game start is instant. A hit fires
                # watermark replenishment in the background. Only when the
                # arsenal is empty for this partition do we fall to live
                # generation (rung 2), and only at PREFETCH — the delivery
                # path itself never generates.
                question = await self._arsenal_picture_draw(supply_mode)
                if question is None:
                    question = await self.reasoning.prefetch_picture_question(
                        self.supabase,
                        kind=picture_kind,
                        question_index=self.sk.question_number,
                        session_id=self.sk.session_id,
                        mode=supply_mode,
                        intensity=self.sk.adult_image_intensity,
                        exclude_ids=history_ids, exclude_hashes=history_hashes,
                    )

            # Runbook fallback: LILY_KB_ONLY flips supply to the curated
            # bank; bank questions bypass verification (§4.5). Text supply
            # only runs when no picture question landed above.
            if question is None:
                from_bank = None
                # Operator-requested topics prefer the bank: serve a
                # previously-generated question for this topic before
                # regenerating, and only generate (and bank a fresh one)
                # when the bank runs dry — the arsenal compounds per topic.
                # The fixed family rotation still generates-first (freshness).
                prefer_bank = (
                    self._is_operator_category(category)
                    and self.supabase is not None
                    and self.sk.media_mode != "pictures"
                )
                # HOTFIX-006 N2: a topic the table NAMED draws strictly.
                # This preference is what turned "build me a Cape Cod round"
                # into "serve me anything" in session lily-16A9AE — the bank
                # had no Cape Cod rows, so the any-category fallback stage
                # handed back Psycho and the round ran generic under the
                # narration. Strict here means the bank either has that
                # topic or gets out of the generator's way.
                strict = self._is_operator_category(category)
                if (lily_config.kb_only() or prefer_bank) and (
                    self.supabase is not None
                ):
                    # Bounded: the sync client has no HTTP timeout of its
                    # own — an unbounded hang here wedges the supply line.
                    from_bank = await asyncio.wait_for(
                        lily_persistence.lily_fetch_bank_question(
                            self.supabase, category, tier, self.used_prompts,
                            mode=supply_mode,
                            exclude_ids=history_ids,
                            exclude_hashes=history_hashes,
                            exclude_answers=set(history_answers),
                            strict_category=strict,
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
                    effort=effort,
                )
                if question is not None and not str(
                    question.get("id", "")
                ).startswith("kb_"):
                    # HOTFIX-006 N2: a question generated FOR a named topic
                    # is labelled with that topic before anything else looks
                    # at it. _shape_question defaults an unlabelled question
                    # to "potpourri", so the Cape Cod round's own questions
                    # used to bank under a category no later Cape Cod
                    # request could ever find — the compounding arsenal
                    # never compounded for operator topics, and the ledger
                    # row could not name the round either.
                    if strict:
                        question["category"] = category
                    # Generated (verified) question: asked-history check,
                    # category-proposal gating (F), then banking (D).
                    question = self._curate_generated_question(
                        question, category, history_hashes
                    )
            if question is None and self.supabase is not None:
                # Generation failed — curated bank is the insurance policy.
                # Bounded for the same reason as above: the insurance line
                # must never hang the supply task. N2: the insurance draw is
                # the OTHER door the generic round came through — for a
                # named topic it stays strict, so a failed build surfaces as
                # a refusal instead of a stranger's question wearing the
                # topic's name. Z2 (HOTFIX-008): the draw logs its outcome —
                # the 2260354c RCA found this leg ran inside a dead task and
                # left zero telemetry about what it did — and a failure here
                # no longer kills the whole task (the recovery ladder in the
                # wrapper owns what happens next).
                try:
                    question = await asyncio.wait_for(
                        lily_persistence.lily_fetch_bank_question(
                            self.supabase, category, tier, self.used_prompts,
                            mode=supply_mode,
                            exclude_ids=history_ids, exclude_hashes=history_hashes,
                            exclude_answers=set(history_answers),
                            strict_category=strict,
                        ),
                        timeout=20.0,
                    )
                except Exception:
                    logger.exception(
                        "LILY_PREFETCH | INSURANCE_BANK_ERROR | session=%s "
                        "q=%d category=%r — generation failed and the "
                        "insurance draw itself failed",
                        self.sk.session_id, self.sk.question_number, category,
                    )
                    question = None
                if question is not None:
                    logger.warning(
                        "LILY_PREFETCH | INSURANCE_BANK_HIT | session=%s q=%d "
                        "id=%s — generation failed; the curated bank covered "
                        "the draw",
                        self.sk.session_id, self.sk.question_number,
                        question.get("id"),
                    )
                    self.sk.clear_status_notes()
                    if mc:
                        # Bank rows carry no choices — synthesize here too.
                        await self.reasoning.ensure_choices(question)
                else:
                    logger.error(
                        "LILY_PREFETCH | INSURANCE_BANK_EMPTY | session=%s "
                        "q=%d category=%r mode=%s — generation failed and "
                        "the curated bank has no eligible row (supply low)",
                        self.sk.session_id, self.sk.question_number,
                        category, supply_mode,
                    )
            if question is None:
                # Z2: nothing landed from generation, pictures, or the
                # insurance bank — a genuine supply failure (the discard
                # guards below deliberately do NOT set this).
                self._prefetch_supply_failed = True
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
            # A custom-category request can cancel a draw after its final
            # await has already completed. Task cancellation alone cannot
            # stop that stale coroutine from committing the old category.
            if question is not None and self._category_for_round(rnd) != category:
                logger.info(
                    "LILY_PREFETCH | CATEGORY_SWITCH_DISCARD | session=%s "
                    "round=%d drawn_for=%r category_now=%r id=%s",
                    self.sk.session_id, rnd, category,
                    self._category_for_round(rnd), question.get("id"),
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
            if (
                question is not None
                and not getattr(self, "_delivery_stop_sticky", False)
                and self.next_question is not None
            ):
                # Depth-2 (W2b item 3a): the head is already in hand — land
                # N+2 in the reserve. The head-only machinery (settle, THE
                # registration point, auto-advance) is deliberately skipped
                # here; it runs when this question is PROMOTED to the head at
                # arm. Stamp the draw mode so promotion can reject a deck that
                # flipped while the reserve sat.
                self._next_question_reserve = question
                self._next_question_reserve_mode = self.sk.mode
                self._note_supply_landed()
            elif (
                question is not None
                and not getattr(self, "_delivery_stop_sticky", False)
            ):
                self.next_question = question
                # Z2: supply landed — the incident (if any) is over.
                self._note_supply_landed()
                # Y2: an ASYNC landing flips the stable block's "next
                # question: ready" line — settle now or the next preemptive
                # speculation snapshots stale state and dies at commit.
                self.settle_context_nowait()
                # HOTFIX-006 N2 — THE registration point. This is the moment
                # a named topic stops being a promise: a real, verified,
                # category-matched question is committed to the supply line
                # and the very next arm writes it to lily_asked_history under
                # that category. Everything allowed to SAY the round exists
                # reads what this writes.
                self._register_custom_question(category, question)
                # Auto-advance (frozen-reveal deadlock fix): when the reveal
                # consumed the previous question BEFORE this prefetch landed,
                # arm_next_question() at reveal time returned False and no
                # later code path arms or asks — Lily only takes a turn when
                # someone speaks, so a quiet table stares at a stale reveal
                # forever. If the game is live and idle, arm and nudge now.
                paused = self.progression_paused_reason()
                if paused == "address_unanswered":
                    # P0-G, scoped (2026-08-09): an unresolved direct
                    # address owns the FLOOR, not the supply line. Arming
                    # writes the state block silently; the nudge below
                    # still yields at the dispatch chokepoint until her
                    # response plays out. Without this, "back to normal"
                    # (itself host-directed, so the latch is up) blocked
                    # the general deck from re-arming — the exact contract
                    # the adult-identity fixture pins.
                    paused = None
                if (
                    self.game_started
                    and not self.game_over
                    and self.armed_question is None
                    and not self.sk.answer_window_open
                    and not self._adjudicating
                    and not getattr(self, "_question_transitioning", False)
                    and paused is None
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

    def _release_armed_question_to_supply(self) -> None:
        """Deregister the armed question from the in-memory asked_history
        mirror (the session's own no-repeat gate — comment at
        arm_next_question), clear its stale delivery intent, and drop it so
        the idle path arms the NEXT question on the following tick. The DB
        asked_history row is left intact (it truthfully records the draw;
        removing it is a DB delete gated on Doc); only the in-memory mirror
        governs re-supply this session."""
        armed = self.armed_question or {}
        qid = armed.get("id")
        hist = getattr(self, "asked_history", None)
        if isinstance(hist, list) and hist:
            for index in range(len(hist) - 1, -1, -1):
                if hist[index].get("question_id") == qid:
                    hist.pop(index)
                    break
            else:
                # Reconstructed draw with no id match: the armed question is
                # the most recent served row.
                hist.pop()
        self.armed_question = None
        self._pending_delivery_qnum = None
        self._armed_speech_misses = 0
        self._undelivered_ticks = 0
        self._undelivered_refires = 0

    # -- supply-stall fallback (WS-6, WO-LILY-OMNIBUS-003) ---------------------
    #
    # WS-2 heals a delivery that armed but never aired. WS-6 heals the class
    # UPSTREAM of it: generation itself starves — the supply line returns
    # nothing tick after tick, so nothing ever arms. The idle watchdog
    # already re-prefetches every idle tick, but a genuinely dead generator
    # re-prefetches into the same void; the 583a0f16 session sat there for
    # five minutes, vamping, with no fallback and no screen cue. Past the
    # fallback window, arm a question straight from the curated bank (the
    # SAME lily_questions source the LILY_KB_ONLY runbook and the
    # generation-failed insurance path draw from — not a new store). Ordered
    # reconciliation-first: arm only when no_stuck_claims() holds, so a
    # fallback never queues behind a WS-2 ghost.

    def _supply_fallback_ticks(self) -> int:
        """Watchdog ticks the game may sit live-idle with no question in
        hand before the curated-bank fallback arms one (config seconds /
        the tick interval, floor one tick). Independent of the prefetch
        hard timeout: a generator returning nothing every tick keeps
        re-prefetching (task.done() each tick) so the hard timeout never
        climbs — this window fires anyway."""
        seconds = lily_config.supply_fallback_seconds()
        return max(1, round(seconds / self.WATCHDOG_INTERVAL_SECONDS))

    async def arm_supply_fallback(self) -> str:
        """Arm a question straight from the curated bank when generation
        has starved the supply line (WS-6). Re-guards every precondition,
        so it is safe to call standalone. Returns:
          "armed"   — a bank question armed and one structural delivery
                      dispatched (exactly one active claim);
          "blocked" — a stuck delivery claim is present; WS-2 reconciliation
                      must clear it first (reconciliation-first ordering);
          "empty"   — no eligible bank row (deck exhausted for this group,
                      no supabase, or the only draw was a duplicate) — the
                      game keeps its honest vamp;
          "idle"    — not in the live supply-stall state;
          "error"   — the draw path raised; the game keeps vamping.
        """
        if not getattr(self, "game_started", False) or getattr(
            self, "game_over", False
        ):
            return "idle"
        # Reconciliation-first (the WO's explicit ordering): never queue a
        # fallback behind a ghost. A stuck registered-undelivered claim
        # always has an armed question, so it is reported here as "blocked"
        # (WS-2 reconciliation must re-fire or release it first) rather than
        # falling through to the armed-guard "idle" below.
        if not self.no_stuck_claims():
            return "blocked"
        if getattr(self, "_delivery_stop_sticky", False):
            return "idle"
        if (
            self.armed_question is not None
            or self.next_question is not None
            or self.sk.answer_window_open
            or self._adjudicating
            or getattr(self, "_question_transitioning", False)
        ):
            return "idle"
        if self.supabase is None:
            return "empty"
        # Z2 (HOTFIX-008): the draw itself is delivery-state-independent —
        # _bank_to_supply lands the row on the supply line; only the ARM +
        # nudge below stay behind this method's idle guards.
        drew = await self._bank_to_supply(trigger="idle_watchdog")
        if drew != "supplied":
            return drew
        armed_id = (self.next_question or {}).get("id")
        if not self.arm_next_question():
            return "idle"
        logger.error(
            "LILY_WATCHDOG | SUPPLY_FALLBACK_ARMED | session=%s q=%d id=%s — "
            "generation starved; armed a curated-bank question",
            self.sk.session_id, self.sk.question_number, armed_id,
        )
        if self.session is not None:
            # Structural delivery claim: the nudged turn IS the delivery and
            # claims q_{N}_delivery at dispatch — exactly one active claim.
            self.expect_delivery()
            self.gated_say(
                None,
                "question_nudge",
                (
                    "The next question just landed in the state block from "
                    "the curated bank. Bridge in one short beat and ask it "
                    "now — its question sentence exactly as written, whole, "
                    "in one breath."
                ),
                source="supply_fallback",
            )
        return "armed"

    # -- Z2 phase-independent supply recovery (WO-LILY-HOTFIX-008) -------------
    #
    # RCA, session lily-938EFF-2260354c: q2's prefetch failed loudly at
    # 03:43:12 (PREFETCH_FAILED, TimeoutError, honest status note set) and
    # NOTHING recovered — every supply-recovery rung (IDLE_REARM, the WS-6
    # SUPPLY_STALL fallback, IDLE_REPREFETCH, PREFETCH_HARD_TIMEOUT) lived
    # exclusively in the watchdog's idle branch, reachable only with nothing
    # armed and no window open. q1 stayed armed/window-cycling the whole
    # session, so the watchdog took the healthy path every tick while the
    # supply line was dead and the fallback bank sat full (464 active rows).
    # Supply health is now its own concern: a failed prefetch schedules its
    # own bounded recovery, and the watchdog detects the silent window from
    # ANY phase. Recovery lands questions on the SUPPLY line only
    # (next_question) — delivery stays owned by the phase machinery.

    def _note_supply_landed(self) -> None:
        """A question landed on the supply line: the supply incident (if
        any) is over — reset the retry budget, the silent-window counter,
        and the one-shot exhaustion line."""
        self._supply_retry_attempts = 0
        self._supply_silent_ticks = 0
        self._supply_exhausted_notified = False

    def _supply_silent_window(self) -> bool:
        """True when the supply line is silently dead: no prefetched
        question, no prefetch task in flight, game live past the first
        question arm. Deliberately phase-independent — an armed question or
        an open window says nothing about supply health (the 2260354c
        blindness)."""
        if not getattr(self, "game_started", False) or self.game_over:
            return False
        if getattr(self, "_delivery_stop_sticky", False):
            return False
        if self.next_question is not None or self.sk.question_number < 1:
            return False
        task = self._prefetch_task
        return task is None or task.done()

    def ensure_supply_recovery(self, trigger: str) -> None:
        """Start the supply-recovery ladder unless one is already running,
        supply is actually fine, or the incident already ended in the
        honest exhaustion line (organic supply events reset that). Safe to
        call from any phase, including from inside the dying prefetch task
        itself."""
        if not getattr(self, "game_started", False) or self.game_over:
            return
        if getattr(self, "_delivery_stop_sticky", False):
            return
        if self.next_question is not None:
            return
        if getattr(self, "_supply_exhausted_notified", False):
            return
        task = getattr(self, "_supply_recovery_task", None)
        if task is not None and not task.done():
            return
        pf = self._prefetch_task
        if pf is not None and not pf.done() and pf is not asyncio.current_task():
            return  # a live prefetch owns supply; the hard timeout owns hangs
        self._supply_recovery_task = asyncio.ensure_future(
            self._recover_supply(trigger)
        )

    async def _recover_supply(self, trigger: str) -> None:
        """The recovery ladder: bounded de-escalated re-prefetch → curated
        bank → ONE honest line with an explicit pause offer. Never an
        open-ended wait."""
        try:
            # Let a prefetch task that scheduled this recovery from its own
            # tail finish before the ladder inspects/relaunches it.
            await asyncio.sleep(0)
            retry_budget = lily_config.prefetch_total_budget_seconds() + 25.0
            while (
                getattr(self, "_supply_retry_attempts", 0)
                < self.SUPPLY_RETRY_MAX
            ):
                if getattr(self, "_session_closed", False):
                    return
                if self.next_question is not None or self.game_over:
                    return
                self._supply_retry_attempts = (
                    getattr(self, "_supply_retry_attempts", 0) + 1
                )
                # De-escalate: adult authoring runs high by default, general
                # runs medium — the retry drops a band so a hard draw does
                # not reproduce the stall verbatim.
                effort = "medium" if self.sk.mode == "adult" else "low"
                logger.warning(
                    "LILY_SUPPLY | RETRY | session=%s q=%d attempt=%d/%d "
                    "trigger=%s effort=%s — re-running the failed prefetch",
                    self.sk.session_id, self.sk.question_number,
                    self._supply_retry_attempts, self.SUPPLY_RETRY_MAX,
                    trigger, effort,
                )
                self._prefetch_effort_override = effort
                self.start_prefetch()
                task = self._prefetch_task
                if task is not None and not task.done():
                    try:
                        await asyncio.wait_for(task, timeout=retry_budget)
                    except asyncio.TimeoutError:
                        pass  # wait_for cancelled the hung task
                if self.next_question is not None:
                    return  # landed; the commit path reset the incident
            drew = await self._bank_to_supply(trigger=f"recovery:{trigger}")
            if drew in ("supplied", "idle"):
                return
            # Ladder exhausted: generation retries and the bank both came
            # back empty. One honest line + an explicit pause offer — the
            # table never waits open-ended on silence.
            if getattr(self, "_supply_exhausted_notified", False):
                return
            self._supply_exhausted_notified = True
            logger.error(
                "LILY_SUPPLY | SUPPLY_EXHAUSTED | session=%s q=%d trigger=%s "
                "bank=%s — retries and the curated bank both failed; "
                "delivering the honest line and pause offer",
                self.sk.session_id, self.sk.question_number, trigger, drew,
            )
            self.sk.set_status_note(
                "the question machine is down and the backup deck came back "
                "empty — be honest in one sentence and offer the table a "
                "short pause while it recovers; never stall wordlessly"
            )
            self.gated_say(
                None,
                "supply_exhausted",
                (
                    "The question supply is genuinely stuck: say ONE honest "
                    "sentence that the next question is delayed, then "
                    "explicitly offer the table a short pause or a breather "
                    "while it recovers. One line total, then yield — no "
                    "vamping loop, no invented explanation."
                ),
                source="supply_recovery",
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "LILY_SUPPLY | RECOVERY_FAILED | session=%s q=%d trigger=%s",
                self.sk.session_id, self.sk.question_number, trigger,
            )

    async def _bank_to_supply(self, *, trigger: str) -> str:
        """Draw one curated-bank question and land it on the SUPPLY line
        (`next_question`) — the delivery-state-independent half of the
        WS-6 fallback, factored out of arm_supply_fallback by Z2 so the
        recovery ladder can reach the bank while a question is armed or a
        window is open. Arming/nudging stays with callers that own a phase
        where delivery is legal. Returns "supplied", "empty", "error", or
        "idle" (nothing to do)."""
        if getattr(self, "_delivery_stop_sticky", False) or self.game_over:
            return "idle"
        if self.next_question is not None:
            return "idle"
        if self.supabase is None:
            return "empty"
        rnd = self._round_for_next_question()
        category = self._category_for_round(rnd)
        tier = self._difficulty_for_round(rnd)
        mc = self.sk.format_for_round(rnd) == "multiple_choice"
        if getattr(self, "_drawn_ids", None) is None:
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
        # PATCH-001 T7: the group's cross-session answers also exclude —
        # a differently-worded bank question with a played answer repeats.
        history_answers = set(lily_bank.lily_history_answers(self.asked_history))
        released_note = None
        try:
            # Bounded: the sync client has no HTTP timeout of its own — an
            # unbounded hang here would wedge the very watchdog that is
            # supposed to be un-wedging the game.
            question = await asyncio.wait_for(
                lily_persistence.lily_fetch_bank_question(
                    self.supabase, category, tier, self.used_prompts,
                    mode=self.sk.mode,
                    exclude_ids=history_ids, exclude_hashes=history_hashes,
                    exclude_answers=history_answers,
                    # HOTFIX-006 N2: the third door into the generic round.
                    # This fallback exists to break a supply stall, and for
                    # the fixed families "anything" is exactly right — but
                    # inside a round the table NAMED it is how Psycho ends up
                    # in a Cape Cod round ("what does that have to do with
                    # Cape Cod?").
                    strict_category=self._is_operator_category(category),
                ),
                timeout=20.0,
            )
            if question is None and self._is_operator_category(category):
                # CLASS 6 (LIVEFIRE-001) 6b/6c: a dry bank is a SUPPLY DEFECT,
                # not an end state, and an upstream timeout is not "round
                # over". A named topic KEEPS GENERATING — try one fresh
                # generation for the topic before surrendering its name. Only
                # a true generation failure AFTER this retry flips the round,
                # and the flip is announced (6d, the released_note below).
                gen = None
                try:
                    gen = await asyncio.wait_for(
                        self.reasoning.prefetch_question(
                            self.sk,
                            category=category,
                            difficulty_tier=tier,
                            avoid_questions=self.used_prompts,
                            from_bank=None,
                            multiple_choice=mc,
                            avoid_answers=sorted(history_answers),
                        ),
                        timeout=20.0,
                    )
                except Exception:
                    gen = None
                if gen is not None and not str(
                    gen.get("id", "")
                ).startswith("kb_"):
                    gen["category"] = category
                    gen = self._curate_generated_question(
                        gen, category, history_hashes
                    )
                if gen is not None:
                    logger.warning(
                        "LILY_CUSTOM_ROUND | TOPIC_BACKFILLED | session=%s "
                        "topic=%r q=%d — bank dry, generation refilled the "
                        "named topic; NO flip",
                        self.sk.session_id, category,
                        self.sk.question_number,
                    )
                    # Fall through to the shared landing tail (register,
                    # choices, next_question, settle) — the topic survives.
                    question = gen
            if question is None and self._is_operator_category(category):
                # Generation ALSO failed after the backfill retry — the topic
                # genuinely cannot serve. Only NOW does the round lose its
                # NAME rather than its honesty: drop the override, tell the
                # table the custom round is out of questions (templated, 6d),
                # and pull from the fixed rotation like any other stalled
                # round. There is no silent flip: this path always sets the
                # released_note the landing tail speaks.
                logger.error(
                    "LILY_CUSTOM_ROUND | TOPIC_EXHAUSTED | session=%s "
                    "topic=%r round=%d — bank dry AND generation failed; "
                    "releasing the round to the fixed rotation rather than "
                    "serving a stranger under its name",
                    self.sk.session_id, category, rnd,
                )
                self._category_override.pop(rnd, None)
                # Held aside for the landing tail (which clears stall vamps
                # then re-sets this — a different fact with a different
                # lifetime, so it outlives the clear).
                released_note = (
                    f"the {category!r} round is out of questions — say so "
                    "plainly ('that's everything I've got on "
                    f"{category}') and carry on with the regular deck. The "
                    "next question is NOT about that topic; never introduce "
                    "it as one."
                )
                # CLASS 6 (LIVEFIRE-001) 6d: announce the flip AT the
                # transition, not only if the fixed-rotation draw then lands.
                # If that draw also comes up empty the method returns "empty"
                # before the landing tail — without this the flip would be
                # SILENT (the live "silent flip to academic"). Set the honest
                # note here so the announcement survives the early return.
                self.sk.set_status_note(released_note)
                category = self._category_for_round(rnd)
                question = await asyncio.wait_for(
                    lily_persistence.lily_fetch_bank_question(
                        self.supabase, category, tier, self.used_prompts,
                        mode=self.sk.mode,
                        exclude_ids=history_ids,
                        exclude_hashes=history_hashes,
                        exclude_answers=history_answers,
                    ),
                    timeout=20.0,
                )
        except Exception:
            logger.exception(
                "LILY_WATCHDOG | SUPPLY_FALLBACK_ERROR | session=%s q=%d "
                "trigger=%s", self.sk.session_id, self.sk.question_number,
                trigger,
            )
            return "error"
        if question is None:
            logger.error(
                "LILY_WATCHDOG | SUPPLY_BANK_EMPTY | session=%s q=%d "
                "trigger=%s — supply stalled and the curated bank has no "
                "eligible row; holding the honest vamp",
                self.sk.session_id, self.sk.question_number, trigger,
            )
            return "empty"
        # G2 idempotency: a bank row already drawn this session is a
        # duplicate — discard rather than serve it twice.
        if not self._register_draw(question):
            return "empty"
        if mc and not question.get("choices"):
            # Bank rows outside the adult MC deck carry no choices —
            # synthesize them the same way the insurance path does.
            try:
                await self.reasoning.ensure_choices(question)
            except Exception:
                logger.exception(
                    "LILY_WATCHDOG | SUPPLY_FALLBACK_CHOICES | session=%s",
                    self.sk.session_id,
                )
        if self.sk.media_mode != "pictures":
            # Voice-only never rides a bank row's cached image (sub-agent K).
            question.pop("image_url", None)
            question.pop("image_license_note", None)
            question.pop("image_prompt", None)
            if question.get("image_source"):
                question["image_source"] = "none"
        self.next_question = question
        self._note_supply_landed()
        # Y2: same async-landing settle as the prefetch path.
        self.settle_context_nowait()
        # The stall is over: clear the honest-vamp status note the watchdog
        # or prefetch-crash path may have set.
        self.sk.clear_status_notes()
        if released_note:
            # ...but a released custom round is not a stall note, and the
            # table is owed the sentence (N2).
            self.sk.set_status_note(released_note)
        logger.warning(
            "LILY_SUPPLY | BANK_TO_SUPPLY | session=%s q=%d id=%s trigger=%s "
            "— curated-bank row landed on the supply line",
            self.sk.session_id, self.sk.question_number, question.get("id"),
            trigger,
        )
        return "supplied"

    # -- mode-switch flush + re-arm (WO-LILY-DESYNC-HONESTY-001 D) -------------
    #
    # The 2026-07-15 adult segment opened on a LEFTOVER general question
    # ("powerhouse of the cell" — user: "wait, THAT's the adult section?")
    # because the armed queue survived the mode switch, and then served
    # identity-less freestyle questions because the supply gap left nothing
    # armed. The flush closes both: no question survives a deck change, and
    # the immediate re-prefetch keeps the gap to one honest beat.

    def _promote_reserve(self) -> None:
        """Depth-2 supply (W2b item 3a): pull the reserve (N+2) into the head
        the instant the head empties, so the hand is only empty when BOTH
        slots are spent. This is the CENTRALISED Class-6 guard: a reserve
        drawn under a deck that has since flipped, or one already burned, is
        DISCARDED here — never served — and the head is left empty for the
        caller to re-prefetch. The register/settle the prefetch commit skips
        for a reserve run HERE, at promotion, so a promoted question is
        registered exactly once, as the head. No-op when there is no reserve
        (the invariant: a reserve exists only while the head was full)."""
        reserve = getattr(self, "_next_question_reserve", None)
        if reserve is None:
            return
        self._next_question_reserve = None
        reserve_mode = getattr(self, "_next_question_reserve_mode", None)
        self._next_question_reserve_mode = None
        if self._is_burned(reserve) or (
            reserve_mode is not None and reserve_mode != self.sk.mode
        ):
            logger.info(
                "LILY_SUPPLY | RESERVE_DISCARD | session=%s id=%s "
                "drawn_mode=%s mode_now=%s — deck flipped or answer aired "
                "while the reserve sat; discarding (head left empty to "
                "re-prefetch)",
                self.sk.session_id, reserve.get("id"), reserve_mode,
                self.sk.mode,
            )
            return
        if self.sk.media_mode != "pictures":
            # Mirror the prefetch commit's voice_only picture exclusion.
            reserve.pop("image_url", None)
            reserve.pop("image_license_note", None)
            reserve.pop("image_prompt", None)
            if reserve.get("image_source"):
                reserve["image_source"] = "none"
        self.next_question = reserve
        self._register_custom_question(reserve.get("category") or "", reserve)
        self.settle_context_nowait()
        logger.info(
            "LILY_SUPPLY | RESERVE_PROMOTED | session=%s id=%s — N+2 is now "
            "the head",
            self.sk.session_id, reserve.get("id"),
        )

    def arm_next_question(self) -> bool:
        """Move the prefetched question into the state block for Lily to
        perform. Returns True if a question is armed."""
        if getattr(self, "_delivery_stop_sticky", False):
            return False
        if self.armed_question is not None:
            return True
        if self.next_question is None:
            self.start_prefetch()
            return False
        # Burn guard (WS-4): a prefetched question whose answer already
        # went to air this session is dead — discard it and pull a
        # replacement rather than re-perform the revealed question (the
        # live "re-ask the timed-out question, score the echo" defect).
        if self._is_burned(self.next_question):
            logger.info(
                "LILY_BURN | REARM_BLOCKED | session=%s question_id=%s — "
                "answer already revealed; discarding and re-prefetching",
                self.sk.session_id, self.next_question.get("id"),
            )
            self.next_question = None
            # Head burned: pull the reserve forward (or discard it if it is
            # itself stale) before falling back to a fresh prefetch.
            self._promote_reserve()
            self.start_prefetch()
            return False
        self.armed_question = self.next_question
        self.next_question = None
        # Depth-2: refill the head from the reserve immediately, then top the
        # reserve back up — the hand never goes empty between arms.
        self._promote_reserve()
        self.start_prefetch()
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
        # The category rides the row (migration 023, HOTFIX-006 N2): the
        # lily-16A9AE ledger could say WHICH questions were served but not
        # what round they belonged to, so a narrated Cape Cod round and a
        # real one were indistinguishable in the only durable record.
        # The IN-SESSION mirror stays at arm: it exists so this session's
        # own draws are excluded from the next draw, and that must be true
        # the instant a question is in hand.
        self.asked_history.append({
            "question_id": self.armed_question.get("id"),
            "question_text_hash": lily_bank.lily_question_text_hash(
                self.armed_question.get("prompt")
            ),
            "canonical_answer": self.armed_question.get("canonical_answer"),
            "category": self.armed_question.get("category"),
        })
        # The DURABLE burn does NOT. It moved to playout start
        # (record_question_asked, wired from note_playout_started): a
        # question the table never heard must not be spent forever.
        # Live 2026-08-08 `lily-2C489B` wrote arsenal entry 861712c7 to
        # lily_asked_history at 22:48:55, twenty seconds before the
        # delivery began and three cut attempts before it gave up — the
        # session played zero questions and consumed one anyway.
        self._durable_asked_qnum = None
        self._glass_published_qnum = None
        self._armed_speech_misses = 0
        self._pending_delivery_qnum = None  # stale delivery intent dies at arm
        self._active_delivery_qnum = None
        self._active_delivery_started_at = None
        self._active_delivery_ended_at = None
        getattr(self, "_prehook_answer_suppressions", set()).clear()
        self._undelivered_ticks = 0  # WS-2: reconcile counters are per-question
        self._undelivered_refires = 0
        self._supply_stall_ticks = 0  # WS-6: a question is in hand now
        self._pre_window_segments = []  # early-buzz buffer is per-question
        self._judged_keys = set()
        self._addressee_rows = {}  # B1: row-id tasks are per-question
        for _task in self._spec_judge.values():
            if not _task.done():
                _task.cancel()
        self._spec_judge = {}
        self._nbest_by_key = {}  # n-best dicts are per-question
        if self.ui_phase in ("lobby", "reveal", "scores"):
            # Voice/glass sync (2026-07-31 live report): the FIRST question
            # arms while Lily is still greeting — flipping the published
            # phase here replaced the lobby with the game board mid-
            # salutation. Hold the published phase until the delivery turn
            # actually airs (publish_question_to_glass clears the hold).
            #
            # WIDENED 2026-08-09 from "lobby" to every phase the arm can
            # interrupt, because the same defect was eating the reveal.
            # adjudicate publishes phase=reveal + the reveal metadata and
            # then runs synchronously to the end — arming N+1 queues
            # phase=question in the same tick, ~1 RTT later. The frontend
            # gates ALL reveal rendering on the phase
            # (`revealPhase = phase === 'reveal' || 'scores'`), and the
            # `reveal` data packet only fires at the verdict turn's playout
            # start — an LLM round-trip AFTER the phase already went back.
            # So the answer never stamped and "X got it" never appeared:
            # she said "Correct — Saturn! Point to Maya" over a board
            # showing neither. "Go back" showed answers the live screen
            # never displayed, because history folds state.reveal.
            #
            # Same hold also stops the PREVIOUS question being re-chalked
            # as the live one: between arm and air the metadata still holds
            # question N's prompt and choices while question_number reads
            # N+1, so the just-answered question was freshly animated in
            # under the new number for the whole flourish + LLM + TTS.
            self._phase_hold = self.ui_phase
        self._set_ui_phase("question")
        # Metadata publish moved to WINDOW-OPEN time (delivery playout
        # completion): the old arm-time publish clobbered the reveal
        # milliseconds after adjudication, and the interim dispatch-time
        # publish still led the voice by the length of any audio queued
        # ahead of the delivery turn (greetings, celebration) — screen
        # truth must equal SPOKEN truth, so the glass syncs at playout.
        #
        # HOSTLOOP-001 C9: that playout-start gating is now behind ONE
        # reversible flag. LILY_BOARD_ON_PLAYOUT_START=true (default)
        # keeps it; false reverts to the legacy ARM-TIME post (the glass
        # leads the voice by the queued-audio length — the pre-2C489B
        # behavior, kept only as the rollback position the WO requires).
        if not lily_config.board_on_playout_start():
            self._phase_hold = None
            self.publish_question_to_glass(reason="serve_time_flag")
        self.start_prefetch()  # N+2 begins while N+1 plays out
        return True

    async def _arsenal_picture_draw(self, supply_mode: str) -> dict | None:
        """First rung of the picture supply ladder: draw a pre-generated
        pair from the standing arsenal. ZERO generation on this path — the
        whole point is cold-start pictures with no Grok wait. Returns the
        §4.2 question shape, or None so the caller falls to the next rung
        (live generation at prefetch, then the truthful pictureless line).

        Heat is read HERE (binding C): a mid-session heat flip changes the
        partition this draws from on the very next question — no stale
        partition. 'mix' draws from whichever adult partition still has a
        pair this group has not seen."""
        if getattr(self, "supabase", None) is None:
            logger.warning(
                "LILY_SUPPLY | PICTURE_DRAW | id=none url=no mode=%s "
                "reason=no_supabase",
                supply_mode,
            )
            return None
        group_id = getattr(self, "group_id", None)
        if not group_id:
            logger.warning(
                "LILY_SUPPLY | PICTURE_DRAW | id=none url=no mode=%s "
                "reason=no_group_id",
                supply_mode,
            )
            return None
        intensity = getattr(self.sk, "adult_image_intensity", "suggestive")
        partitions = lily_arsenal.lily_partitions_for(supply_mode, intensity)
        # Answer-level belt over the DB group no-repeat: steer clear of
        # answers this group has already played on any supply path.
        try:
            excl = set(lily_bank.lily_history_answers(self.asked_history))
        except Exception:
            excl = set()
        for partition in partitions:
            q = await lily_arsenal.lily_arsenal_draw(
                self.supabase,
                partition=partition,
                group_id=group_id,
                session_id=self.sk.session_id,
                exclude_answers=excl,
            )
            if q is not None:
                # The arsenal bucket is PRIVATE (adult_explicit entries live
                # in it), so the row carries a storage path, not a URL. Sign
                # it for this serve. A signing failure degrades to the
                # truthful pictureless line rather than a broken frame.
                signed = await lily_images.lily_arsenal_image_url(
                    self.supabase, q.get("image_storage_path")
                )
                qid = q.get("id") or q.get("arsenal_id") or "?"
                if not signed:
                    logger.warning(
                        "LILY_SUPPLY | PICTURE_DRAW | id=%s url=no mode=%s "
                        "partition=%s reason=sign_failed — skip row, try next",
                        qid, supply_mode, partition,
                    )
                    continue
                q["image_url"] = signed
                self._kick_arsenal_replenish(partition, supply_mode, intensity)
                logger.info(
                    "LILY_SUPPLY | PICTURE_DRAW | id=%s url=yes mode=%s "
                    "partition=%s",
                    qid, supply_mode, partition,
                )
                return q
        # Every candidate partition came up empty. This is the exact moment
        # the 2026-08-07 session hit — a player asking for picture trivia
        # against a shelf with nothing on it — and it must never again pass
        # in silence. WARN here, then fall to the next supply rung.
        logger.warning(
            "LILY_SUPPLY | PICTURE_DRAW | id=none url=no mode=%s "
            "partitions=%s — arsenal empty for group; fall through to live "
            "generation. Seed: python3 -m lily_arsenal_seed --partition all",
            supply_mode, ",".join(partitions),
        )
        logger.warning(
            "ARSENAL_LOW | partitions=%s group=%s — no ready entry for this "
            "group; falling through to live generation. Seed the bank: "
            "python3 -m lily_arsenal_seed --partition all",
            ",".join(partitions), group_id,
        )
        return None

    def _kick_arsenal_replenish(
        self, partition: str, supply_mode: str, intensity: str
    ) -> None:
        """Fire watermark-gated replenishment in the BACKGROUND — never
        awaited, never on the delivery path. Checks the served/ready
        watermark first; only when the pool has crossed it does it spawn a
        generate-toward-ten task. Fire-and-forget: a failure logs and dies,
        it never touches the spoken lane."""
        if getattr(self, "supabase", None) is None:
            return

        async def _run() -> None:
            try:
                served = await lily_arsenal.lily_arsenal_served_count(
                    self.supabase, session_id=self.sk.session_id, partition=partition
                )
                ready = await lily_arsenal.lily_arsenal_ready_count(
                    self.supabase, partition=partition
                )
                # Availability to THIS group is the number that actually
                # runs out at a table: entries stay 'ready' after a serve
                # because they are still new to every other group, so the
                # global count alone would read full while this table
                # exhausted what it had not already seen.
                available = await lily_arsenal.lily_arsenal_available_to_group(
                    self.supabase, partition=partition,
                    group_id=getattr(self, "group_id", "") or "",
                )
                if not lily_arsenal.lily_should_replenish(
                    served, ready, partition=partition,
                    available_to_group=available,
                ):
                    return
                await lily_arsenal.lily_arsenal_replenish(
                    self.supabase,
                    partition=partition,
                    generate_one=self._arsenal_generate_one(supply_mode, intensity),
                )
            except Exception as e:
                logger.warning(
                    "LILY_ARSENAL | REPLENISH_KICK_FAILED | partition=%s: %s",
                    partition, e,
                )

        asyncio.ensure_future(_run())

    def _arsenal_generate_one(self, supply_mode: str, intensity: str):
        """Build the generate_one callable the replenisher pumps: one
        fresh picture pair per call, or None when generation is
        unavailable so the replenisher stops cleanly. Each partition banks
        at its OWN fixed heat — adult_explicit rows are explicit,
        adult_suggestive rows are suggestive — so a 'mix' session draws
        true-to-partition pairs from either pool.

        WO-LILY-ARSENAL-SEED-001: this used to pump
        prefetch_picture_question(kind='real_or_imagined'), and that could
        never have filled the bank. Every real-or-imagined question carries
        the SAME spoken stem ('Eyes on the screen. One photograph, one
        question — is it real, or imagined?'), so every generated pair
        hashed to the same question_text_hash and lily_arsenal_insert's
        dedup rejected all but the FIRST row in each partition. The shelf
        was not merely unstocked; the code that was supposed to stock it
        was capped at one entry per partition. It now runs the same
        format-and-subject-aware pipeline the seeding job runs, so in-
        session replenishment produces varied shapes and actually
        accumulates."""

        async def _one(partition: str) -> dict | None:
            part_intensity = self._ARSENAL_PARTITION_INTENSITY.get(
                partition, intensity
            )
            try:
                # Generation lives on the REASONING node — the vocal path
                # never touches the image stack (hard guardrail, enforced by
                # inspection in tests/test_web_guardrails.py). This seam,
                # lily_agent -> lily_reasoning, is the only legal route.
                depth = await lily_arsenal.lily_arsenal_bank_depth(
                    self.supabase, partition=partition
                )
                entry = await self.reasoning.generate_arsenal_entry(
                    self.supabase,
                    partition=partition,
                    start_index=depth["depth"],
                )
            except Exception as e:
                logger.warning(
                    "LILY_ARSENAL | REPLENISH_ONE_FAILED | partition=%s: %s",
                    partition, e,
                )
                return None
            if entry is None:
                # None stops the replenish loop — correct for an
                # unavailable provider, and cheap for a one-off rejection
                # (the next watermark crossing tries again).
                return None
            entry["_arsenal_intensity"] = part_intensity
            return entry

        return _one

    # -- state block --------------------------------------------------------------

    def _refresh_supply_for_pictures_on(self, *, source: str) -> None:
        """Invalidate pictureless prefetch after media_mode flips to pictures.

        Lobby/auto-start often fills next_question under voice_only;
        start_prefetch early-returns while that row sits, so the arsenal
        never runs. Clear pictureless next_question, cancel in-flight
        prefetch, and relaunch. Does not touch an already-armed in-window
        question (that arm finishes as text; the following slot draws).
        """
        nq = getattr(self, "next_question", None)
        url = str((nq or {}).get("image_url") or "")
        if url.startswith(("http://", "https://")):
            return
        logger.info(
            "LILY_SUPPLY | PICTURE_REFRESH | session=%s source=%s "
            "dropped_prefetched=%s",
            self.sk.session_id, source, (nq or {}).get("id"),
        )
        self.next_question = None
        # Depth-2: the reserve was drawn under the same pictureless deck — it
        # is invalid for pictures too. Clear it with the head (invariant: head
        # cleared => reserve cleared).
        self._next_question_reserve = None
        self._next_question_reserve_mode = None
        task = getattr(self, "_prefetch_task", None)
        if task is not None and not task.done():
            task.cancel()
        self._prefetch_task = None
        if getattr(self, "_prefetch_stall_ticks", None) is not None:
            self._prefetch_stall_ticks = 0
        if getattr(self, "game_over", False):
            return
        # Unit harnesses call try_activate_pictures outside an event loop;
        # clearing next_question is enough — live entrypoints always have a
        # loop and will prefetch on the next tick / start_game.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            logger.info(
                "LILY_SUPPLY | PICTURE_REFRESH | session=%s source=%s "
                "prefetch_deferred=no_loop",
                self.sk.session_id, source,
            )
            return
        self.start_prefetch()

    def _burn_question(
        self, question: dict, reason: str, *, persist: bool = True
    ) -> None:
        """Mark one question burned: LILY_BURN log, status='burned' for
        bank rows (kb_ ids — generated questions have no DB row and are
        simply discarded), and the prompt joins used_prompts so the
        generator never re-produces it this session. Scope is GLOBAL
        today (migration 009 status column); per-group burn rides
        lily_asked_history later.

        WS-4: the burn also joins an in-session dead set (id + normalized
        text hash) so a burned question can never re-arm and is excluded
        from every future draw — the reveal path burns here too (a
        timed-out question whose answer Lily spoke aloud is dead), so its
        echo has no live window to score into."""
        qid = question.get("id")
        logger.warning(
            "LILY_BURN | question_id=%s | session=%s | reason=%s",
            qid, self.sk.session_id, reason,
        )
        prompt = question.get("prompt", "")
        if prompt and prompt not in self.used_prompts:
            self.used_prompts.append(prompt)
        burned_ids = getattr(self, "_burned_question_ids", None)
        if burned_ids is None:
            self._burned_question_ids = burned_ids = set()
            self._burned_question_hashes = set()
        if qid:
            burned_ids.add(str(qid))
        if prompt:
            self._burned_question_hashes.add(
                lily_bank.lily_question_text_hash(prompt)
            )
        if persist and self.supabase is not None:
            asyncio.ensure_future(
                lily_persistence.lily_burn_question(self.supabase, qid)
            )

    def _is_burned(self, question: dict | None) -> bool:
        """True if this question's answer has already gone to air this
        session (WS-4) — matched by id or normalized-text hash."""
        if not question:
            return False
        qid = str(question.get("id") or "")
        qhash = lily_bank.lily_question_text_hash(question.get("prompt"))
        return (
            (bool(qid) and qid in getattr(self, "_burned_question_ids", set()))
            or qhash in getattr(self, "_burned_question_hashes", set())
        )

    def _burn_pending_adult_questions(self, reason: str) -> bool:
        """HOTFIX-004 Defect 2: burn the armed + queued questions so
        objected-to adult material can never re-air. Adds each to the dead
        set (id + text hash) via _burn_question, closes any open window, and
        clears the slots — the same one-way retirement on_answer_leak uses.
        Returns True if anything was burned."""
        burned = False
        if self.armed_question is not None:
            self._burn_question(self.armed_question, reason=reason)
            timer = getattr(self, "_window_timer", None)
            if timer is not None and not timer.done():
                timer.cancel()
            self.sk.close_answer_window()
            self.armed_question = None
            self.sk.current_question = None
            burned = True
        if self.next_question is not None:
            self._burn_question(self.next_question, reason=reason)
            self.next_question = None
            burned = True
        if getattr(self, "_next_question_reserve", None) is not None:
            # Depth-2: the reserve is another objected-to adult question in
            # flight — retire it with the rest, never serve it.
            self._burn_question(self._next_question_reserve, reason=reason)
            self._next_question_reserve = None
            self._next_question_reserve_mode = None
            burned = True
        if burned:
            logger.warning(
                "LILY_ADULT_GATE | PENDING_BURNED | session=%s reason=%s — "
                "objected-to adult question retired, cannot re-air",
                self.sk.session_id, reason,
            )
            self.publish_attributes_nowait()
        return burned

