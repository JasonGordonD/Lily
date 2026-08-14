"""LilyTransition — the reveal->verdict->next transition journal (W3 Cut 1).

INVARIANT: one transition per question, one owner, a monotonic stage journal
(reveal -> verdict -> next_delivery) where no stage is narrated twice. This is
already a closed algebra; the extract is a byte-identical MOVE from LilyGame to
a mixin (self.* bindings unchanged), so LilyGame stays the director.
"""

from __future__ import annotations

import logging
import time

import lily_evaluation
import lily_say_gate
import lily_scorekeeper

logger = logging.getLogger("lily_agent")


# HOTFIX-006 N12: how long a question transition owns its narration. The
# live contradiction ("That's a point for Chris." / "No points on that one")
# landed inside ONE beat, and the beat normally closes on its own when the
# next question is delivered. This bound covers the transitions that have
# no next delivery — the finale, a stalled supply line — so that talking
# ABOUT a past ruling later in the session is never mistaken for narrating
# it a second time. Comfortably longer than a verdict + flourish playout.
_TRANSITION_NARRATION_WINDOW_SECONDS = 30.0


class LilyTransitionMixin:
    # -- N12: the question transition is ONE journaled event ------------------
    #
    # Live (lily-D99BE7, question three, inside a single beat):
    #   "Chris got it in right on time with Russia! That's a point for Chris."
    #   "No points on that one — the answer was Russia!"
    # One committed row (q_8294, Chris, "Russia.", correct, 1 point), two
    # narrations, opposite verdicts — plus question FOUR delivered by one
    # lane while the other was still revealing three, and an apology that
    # doubled because the loop it apologised for was itself two lanes.
    #
    # A transition is therefore claimed WHOLE, at the reveal, by one owner,
    # and its three stages are journaled in order: reveal -> verdict ->
    # next_delivery. The claim key rides the SpeechActRegistry that already
    # makes greetings and deliveries idempotent — no parallel mechanism. The
    # journal is the record the fixtures and the logs read: which lane owns
    # this transition, which stages have gone to air, and with what words.
    TRANSITION_STAGES = ("reveal", "verdict", "next_delivery")

    def transition_key(self, qnum: int) -> str:
        """The say-registry key that makes one question transition one act."""
        return f"q_{qnum}_transition"

    def _transition_journals(self) -> dict:
        journals = self._transition_journal
        if journals is None:
            journals = self._transition_journal = {}
        return journals

    def _transition_reached_air(self, qnum: int) -> bool:
        """True when any stage of q_{qnum}'s journaled transition provably
        reached the air: a bound narration (tts_node binds words at the say
        gate), a CONFIRMED stage key (confirm fires at playout), or a
        journaled next_delivery (the beat finished — N+1 went out). A
        journal none of whose entries carries any of the three is dispatch
        bookkeeping for speech that never played."""
        for entry in self._transition_journals().get(qnum) or []:
            if entry["stage"] == "next_delivery":
                return True
            detail = entry.get("detail") or {}
            if detail.get("narration"):
                return True
            key = detail.get("key")
            if (
                key
                and self.say_registry.state(key)
                == lily_say_gate.CLAIM_CONFIRMED
            ):
                return True
        return False

    def open_question_transition(
        self, qnum: int, *, owner: str, source: str,
        reclaim_unaired: bool = False,
    ) -> bool:
        """Claim the WHOLE q_{N} transition for one owner. True = the caller
        owns reveal, verdict and next-delivery for this question; False =
        another lane already owns it and this narration is suppressed.

        Two independent refusals, because a released claim must not hand a
        second lane a transition that already spoke: an existing journal for
        this question, and the claim key itself.

        `reclaim_unaired` (watchdog recovery ONLY — lily-1C53C6 deadlock):
        a transition that was opened and journaled but whose speech never
        reached playout (the q2 reveal generation timed out; the verdict
        turn died silent) used to satisfy the "journal exists" refusal
        forever — ARMED_LIMBO forced adjudication every ~20s and this guard
        refused it every ~20s, a permanent dead-air loop. When the caller is
        the recovery path and NO journaled stage reached air (see
        _transition_reached_air), the dead journal and its stale claims are
        released and the transition is claimed fresh. A transition with any
        aired stage keeps the original refusal — recovery must never grant
        a second narration of something that actually played (N12)."""
        journals = self._transition_journals()
        if qnum in journals:
            if reclaim_unaired and not self._transition_reached_air(qnum):
                stale_entries = journals.pop(qnum)
                self.say_registry.release(self.transition_key(qnum))
                for entry in stale_entries:
                    stale_key = (entry.get("detail") or {}).get("key")
                    if stale_key:
                        self.say_registry.release(stale_key)
                if self._open_transition_qnum == qnum:
                    self._open_transition_qnum = None
                logger.error(
                    "LILY_TRANSITION | RECLAIMED_UNAIRED | session=%s q=%d "
                    "source=%s stages=%s — journaled transition never "
                    "reached playout; releasing the dead claim so recovery "
                    "can narrate (lily-1C53C6)",
                    self.sk.session_id, qnum, source,
                    ",".join(e["stage"] for e in stale_entries),
                )
            else:
                logger.error(
                    "LILY_TRANSITION | SECOND_LANE_REFUSED | session=%s q=%d "
                    "source=%s stages=%s — this transition is already owned "
                    "and narrated; the second lane does not speak (N12)",
                    self.sk.session_id, qnum, source,
                    ",".join(e["stage"] for e in journals[qnum]),
                )
                return False
        if not self.say_registry.claim(self.transition_key(qnum), owner=owner):
            logger.error(
                "LILY_TRANSITION | SECOND_LANE_REFUSED | session=%s q=%d "
                "source=%s state=%s — transition claim held elsewhere (N12)",
                self.sk.session_id, qnum, source,
                self.say_registry.state(self.transition_key(qnum)),
            )
            return False
        journals[qnum] = []
        self._open_transition_qnum = qnum
        logger.info(
            "LILY_TRANSITION | OPEN | session=%s q=%d owner=%s source=%s",
            self.sk.session_id, qnum, owner, source,
        )
        return True

    def journal_transition(
        self,
        qnum: int,
        stage: str,
        *,
        owner: str | None = None,
        detail: dict | None = None,
    ) -> bool:
        """Append ONE stage to the q_{N} transition journal. False when the
        stage is already journaled (a second narration of the same beat) or
        the caller does not own the transition — both are the N12 defect,
        and both are logged rather than silently tolerated."""
        if stage not in self.TRANSITION_STAGES:
            raise ValueError(f"unknown transition stage: {stage!r}")
        entries = self._transition_journals().get(qnum)
        if entries is None:
            logger.warning(
                "LILY_TRANSITION | NO_OPEN_TRANSITION | session=%s q=%d "
                "stage=%s — stage journaled with no transition open",
                self.sk.session_id, qnum, stage,
            )
            return False
        if any(e["stage"] == stage for e in entries):
            logger.error(
                "LILY_TRANSITION | STAGE_DUP | session=%s q=%d stage=%s — "
                "a second lane tried to narrate a stage this transition has "
                "already run (N12)",
                self.sk.session_id, qnum, stage,
            )
            return False
        entries.append({
            "stage": stage,
            "owner": owner,
            "detail": dict(detail or {}),
            "at": time.monotonic(),
        })
        logger.info(
            "LILY_TRANSITION | STAGE | session=%s q=%d stage=%s owner=%s",
            self.sk.session_id, qnum, stage, owner,
        )
        return True

    def transition_journal(self, qnum: int) -> list:
        """The journal for one question's transition (empty when none)."""
        return list(self._transition_journals().get(qnum) or [])

    def transition_stages(self, qnum: int) -> list:
        """The stages this transition has run, in the order they ran."""
        return [e["stage"] for e in self.transition_journal(qnum)]

    def transition_narrated(self, qnum: int, stage: str) -> bool:
        return stage in self.transition_stages(qnum)

    def _transition_entry(self, qnum: int, stage: str) -> dict | None:
        for entry in self._transition_journals().get(qnum) or []:
            if entry["stage"] == stage:
                return entry
        return None

    def register_transition_narration(
        self, spoken_text: str, *, speech_id: str | None = None
    ) -> str | None:
        """The ONE-NARRATION decision for one outbound spoken turn (called
        from tts_node, pure decision + journal bind — offline-testable).

          "narration" — this turn IS the transition's narration (first one
                        through the gate; its words are bound to the beat);
          "duplicate" — the transition was already narrated by different
                        words: the caller makes this turn physically silent
                        (THE contradictory pair — "That's a point for
                        Chris." then "No points on that one");
          None        — not a narration of an open transition; speak
                        normally.

        Narrow on purpose. The turn only counts as a narration when it
        carries the transitioning question's own answer alongside a verdict
        cue (lily_verdict_narration), so banter, encouragement and answers
        to the table are untouched — a false suppression would be a worse
        defect than the one this fixes."""
        qnum = self._open_transition_qnum
        if qnum is None:
            return None
        entry = self._transition_entry(qnum, "verdict")
        if entry is None:
            # The verdict stage has not run: nothing has been narrated yet,
            # so this turn cannot be a SECOND narration of it. (Her organic
            # verdict lands here, and the adjudicate lane detects it
            # separately via _verdict_already_spoken.)
            return None
        if self.transition_narrated(qnum, "next_delivery"):
            # The beat is OVER — question N+1 is on the air. Talking about
            # the last ruling now ("did Chris get that one?" — "he did,
            # Russia, that was his point") is conversation, not a second
            # narration, and must never be silenced.
            return None
        if (
            time.monotonic() - entry["at"]
            > _TRANSITION_NARRATION_WINDOW_SECONDS
        ):
            # Same reasoning, for the transitions that have no next
            # delivery (the finale, a stalled supply line): the live
            # contradiction landed INSIDE one beat. Past a beat's length
            # she is answering questions about the ruling.
            return None
        detail = entry["detail"]
        text = (spoken_text or "").strip()
        held = self.say_registry.keys_for_owner(speech_id)
        verdict_key = detail.get("key")
        if verdict_key and verdict_key in held:
            # THE verdict beat of this transition (or its re-air after a
            # cut/regeneration, which legitimately carries fresh words):
            # this turn IS the narration, so it binds — never suppressed
            # by its own earlier binding.
            detail["narration"] = text
            detail["narration_speech_id"] = speech_id
            logger.info(
                "LILY_TRANSITION | NARRATION_BOUND | session=%s q=%d "
                "speech=%s", self.sk.session_id, qnum, speech_id,
            )
            return "narration"
        if held:
            # Another KEYED act of the same lane — the standings flourish,
            # the finale, the next delivery. Part of this transition by
            # construction, never a stray second narration of it.
            return None
        reveal = self._transition_entry(qnum, "reveal") or {}
        answer = str((reveal.get("detail") or {}).get("answer") or "")
        if lily_scorekeeper.lily_verdict_narration(spoken_text, answer) is None:
            return None
        bound = detail.get("narration")
        if not bound:
            detail["narration"] = text
            detail["narration_speech_id"] = speech_id
            logger.info(
                "LILY_TRANSITION | NARRATION_BOUND | session=%s q=%d "
                "speech=%s", self.sk.session_id, qnum, speech_id,
            )
            return "narration"
        if bound.strip() == text:
            return "narration"  # the same turn re-entering the gate
        logger.error(
            "LILY_SAY_SUPPRESSED | reason=dup_transition | session=%s q=%d | "
            "already_narrated=%r | suppressed=%r — one transition, one "
            "narration (N12)",
            self.sk.session_id, qnum, bound[:80], text[:80],
        )
        return "duplicate"

    def _transition_holds_next_delivery(self, source: str) -> bool:
        """True when the open transition is NOT ready to release question
        N+1: its verdict has not been narrated, or that narration has not
        finished playing out, or the next delivery already ran.

        This is the "she jumped from a question to question and a third
        question" guard, moved off timing and onto the journal: the next
        delivery is the LAST stage of the previous question's transition,
        so no lane can deliver N+1 over a reveal still on the air."""
        qnum = self._open_transition_qnum
        if qnum is None:
            return False  # no transition in flight (skip, game start, nudge)
        stages = self.transition_stages(qnum)
        if "next_delivery" in stages:
            logger.error(
                "LILY_TRANSITION | DELIVERY_DUP | session=%s q=%d source=%s "
                "— this transition already delivered the next question; a "
                "second lane does not deliver it again (N12)",
                self.sk.session_id, qnum, source,
            )
            return True
        entry = self._transition_entry(qnum, "verdict")
        if entry is None:
            logger.warning(
                "LILY_TRANSITION | DELIVERY_HELD | session=%s q=%d "
                "source=%s reason=verdict_unnarrated — the previous question "
                "has not been ruled on yet (N12)",
                self.sk.session_id, qnum, source,
            )
            return True
        verdict_key = (entry["detail"] or {}).get("key")
        if (
            verdict_key
            and self.say_registry.state(verdict_key)
            != lily_say_gate.CLAIM_CONFIRMED
        ):
            logger.warning(
                "LILY_TRANSITION | DELIVERY_HELD | session=%s q=%d "
                "source=%s reason=verdict_airing key=%s — the reveal of the "
                "previous question is still on the air (N12)",
                self.sk.session_id, qnum, source, verdict_key,
            )
            return True
        return False

    def dispatch_armed_question(self, *, source: str) -> bool:
        """Dispatch one question-only turn after a completed reveal.

        Keeping reveal and delivery on separate handles prevents a round
        transition from registering an invented or stale question as N+1.
        Strict TTS validation rewrites any drift to the deterministic sheet.
        """
        if (
            self._delivery_stop_sticky
            or self.armed_question is None
            or self.sk.answer_window_open
            or getattr(self, "game_over", False)
        ):
            return False
        paused = self.progression_paused_reason()
        if paused:
            logger.info(
                "LILY_PROGRESSION | PAUSED | session=%s q=%d "
                "source=%s reason=%s",
                self.sk.session_id, self.sk.question_number, source, paused,
            )
            return False
        # T2 (PATCH-001): an answered question never re-airs.
        if self.question_already_answered(self.sk.question_number):
            return False
        key = f"q_{self.sk.question_number}_delivery"
        if self.say_registry.state(key) is not None:
            return False
        # N12: the next delivery is the FINAL stage of the previous
        # question's transition — it belongs to that beat and cannot
        # overtake it.
        if self._transition_holds_next_delivery(source):
            return False
        self.expect_delivery()
        sheet = self.rendered_armed_question()
        dispatched = self.gated_say(
            None,
            "question_delivery",
            (
                "The previous reveal is complete. Deliver ONLY the armed "
                "question now. Read this sheet exactly, with every option "
                f"when present, then stop for answers:\n{sheet}"
            ),
            source=source,
        )
        if dispatched:
            # The transition closes on its own last stage. A dispatch that
            # was gated (hold, no live game) journals nothing, so the
            # legitimate retry still owns the beat.
            qnum = self._open_transition_qnum
            if qnum is not None:
                self.journal_transition(
                    qnum, "next_delivery",
                    detail={"source": source, "delivered_q": self.sk.question_number},
                )
        return dispatched

    def transition_awaiting_delivery(self) -> bool:
        """CLASS 2 (LIVEFIRE-001): True while an open transition has aired its
        reveal+verdict but has NOT yet delivered the next question. In this
        window the floor belongs to the reveal — any question in the outbound
        turn is a fused premature delivery (the live "Crete… Next up. What…
        agoge?"). A turn dispatched by dispatch_armed_question journals
        next_delivery BEFORE its tts_node, so the real delivery reads False
        here and is never clipped."""
        qnum = self._open_transition_qnum
        if qnum is None:
            return False
        stages = self.transition_stages(qnum)
        return (
            "reveal" in stages
            and "verdict" in stages
            and "next_delivery" not in stages
        )

    def transition_narration_complete(self, qnum: int) -> bool:
        """True when q_{qnum}'s transition has narrated its whole beat to
        air — reveal and verdict journaled, the verdict provably played
        (_transition_reached_air) — and only next_delivery remains. In
        this state the beat owes the game nothing but bookkeeping: supply
        permitting, the N+1 delivery closes it; supply empty, it must be
        released, never held (HOTFIX-008 Z2c)."""
        stages = self.transition_stages(qnum)
        return (
            "next_delivery" not in stages
            and "reveal" in stages
            and "verdict" in stages
            and self._transition_reached_air(qnum)
        )

    def release_completed_transition(self, qnum: int, *, reason: str) -> bool:
        """Close out a narration-complete transition that cannot reach its
        next_delivery stage because there is no question to deliver.

        HOTFIX-008 Z2c — the lily-938EFF circular wait: transition
        completion needed supply (next_delivery only journals when a
        question dispatches), supply recovery needed the idle watchdog,
        and idle needed this transition released. Held open, a transition
        whose narration fully aired guards nothing — it only pins the
        game.

        The journal is KEPT (never popped) and gains the terminal
        next_delivery marker with delivered_q=None: every completion
        consumer reads the beat as over, while open_question_transition's
        journal refusal still prevents any lane from re-narrating the
        aired verdict. The claim key is released and the open-transition
        slot cleared so a FRESH transition can open for the next question
        the moment supply recovers."""
        if not self.transition_narration_complete(qnum):
            return False
        self.journal_transition(
            qnum, "next_delivery",
            detail={"source": reason, "delivered_q": None},
        )
        self.say_registry.release(self.transition_key(qnum))
        if self._open_transition_qnum == qnum:
            self._open_transition_qnum = None
        logger.warning(
            "LILY_TRANSITION | RELEASED_COMPLETE | session=%s q=%d "
            "reason=%s — narration aired in full, no question to deliver; "
            "claim released so supply recovery can run and a fresh "
            "transition can open (HOTFIX-008 Z2c)",
            self.sk.session_id, qnum, reason,
        )
        return True

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

    def _reveal_already_on_air(self, question: dict) -> bool:
        """HOTFIX-006 N8: has this question's ANSWER already gone out in
        her own words?

        THE evidence, back to back in the live session:
            "No worries! The correct answer is Frankenstein"
            "Frankenstein! That opens a five-second steal window for Chris"
        The first line is a reveal; the second offered a steal on it.

        Deliberately NOT _verdict_already_spoken (which governs whether the
        verdict beat is redundant): this decision retires a question, so it
        carries one extra guard — the QUESTION DELIVERY is never a reveal.
        A multiple-choice stem carries the correct answer among its options
        and a picture stem names its subject; reading the question out is
        not giving the answer away, and mistaking one for the other would
        kill a legitimate steal window."""
        last = getattr(self, "_last_assistant_text", "") or ""
        if not last.strip():
            return False
        try:
            ratio = lily_evaluation.lily_question_spoken_ratio(
                question.get("prompt", ""), last
            )
        except Exception:
            ratio = 0.0
        if ratio >= lily_evaluation.QUESTION_SPOKEN_PARAPHRASE_RATIO:
            # That turn WAS the question (or a recognisable performance of
            # it), not its answer.
            return False
        return lily_scorekeeper.lily_verdict_narration(
            last, str(question.get("canonical_answer", ""))
        ) is not None

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
