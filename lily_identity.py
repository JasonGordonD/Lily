"""LilyIdentity -- recognition, device quarantine, voiceprint, name door, forget,
group upgrade (W3 Cut 3).

INVARIANT: a name maps to at most one CONFIRMED biometric identity; device
candidates are quarantined until a live voice overlaps; minting is biometric-
only (W1c). Byte-identical MOVE from LilyGame (self.* unchanged) that
consolidates the recognition surface -- it does not re-litigate W1c's mint gate."""

from __future__ import annotations

import asyncio
import re
import time
import uuid

import lily_config
import lily_evaluation
import lily_forget
import lily_memory
import lily_persistence
import lily_voice_embedder
import lily_voice_identity

import logging
logger = logging.getLogger("lily_agent")


# Sources that identify the live table strongly enough to skip fallback.
# Group-id sources that OUTRANK a later re-resolution. A binding from one
# of these is settled; the name-set hash may not move it.
#
# HOTFIX-006 N5: "voice_identity_match" — the source the ECAPA matcher
# stages under — was missing from this tuple, and that omission is the whole
# defect. On 2026-08-08 the same three humans bound to two different groups
# three minutes apart (grp_20427c69 and grp_f76e6116) because STT heard
# "Hi, I'm Miranda"; a changed name set changes the hash, and the hash was
# the identity. The matcher meanwhile had found the real table with TWELVE
# games on file. Two identity systems ran side by side — one correct, one
# authoritative — and the correct one was not the authoritative one.
#
# The biometric is the signature. A mishearing may not overrule it.
_STRONG_GROUP_SOURCES = (
    "env_override",
    "voiceprint_match",       # Speechmatics identifier overlap (legacy path)
    "voice_identity_match",   # ECAPA centroid match — the one that works
)

# Sources whose LABEL survives promotion. Strong sources plus the weak ones
# that still deserve honest provenance in the ledger: promotion used to
# coerce anything non-strong to "voiceprint_match", which would have filed
# a name-stated recognition as a biometric one — inventing evidence that
# never existed, in the one table an operator reads to debug recognition.
_KNOWN_GROUP_SOURCES = _STRONG_GROUP_SOURCES + (
    "name_stated",           # the player said a name this group's file knows
)

# ANTIREPEAT-PROTOCOL-001: promotion triggers that ARE the stated-name door
# (maybe_recognize_by_stated_name's two exits). A promotion under one of
# these answers a name the player JUST said, so the ORGANIC reply already
# in flight picks up the promoted memory_block (_apply_context_blocks
# injects it into that turn's context) and carries the welcome-back BY
# CONSTRUCTION — the promotion tail must not also fire the late beat.
_NAME_DOOR_TRIGGERS = ("name_stated", "device_plus_name")

# RECONCILE-001 (e): the name-door serves fragmented returners, for whom >1
# same-name candidate is the COMMON case (one person split across many
# groups), so it stages the most-recent candidate weakly instead of refusing.
# Flat refusal survives only past this ceiling — a crowd of same-name
# candidates too large to be one fragmented person, where a wrong guess is
# likely and the voice is the only safe arbiter. Sits above the observed worst
# case (7 memory groups for a single individual) so real returners are served.
_NAME_DOOR_AMBIGUOUS_CEILING = 12


class LilyIdentityMixin:
    def late_recognition_blocked_reason(self) -> str | None:
        """Return the live beat that makes recognition speech unsafe."""
        # CLASS 7 (LIVEFIRE-001) 7a: once the round has started, recognition
        # speech is forbidden outright — it belongs to the greeting/intake
        # window, never inside or after game start. The live beat aired as
        # act=game_start and suppressed q_1's kickoff.
        if self._game_start_committed:
            return "game_start_committed"
        if self.sk.answer_window_open:
            return "answer_window_open"
        if self._adjudicating:
            return "adjudicating"
        if self._question_transitioning:
            return "question_transitioning"
        if getattr(self, "pending_clarify", None):
            return "pending_clarify"
        if getattr(self.sk, "host_speaking", False):
            return "host_speaking"
        if self._active_delivery_qnum is not None:
            return "delivery_active"
        if self._pending_delivery_qnum is not None:
            return "delivery_pending"
        armed = getattr(self, "armed_question", None)
        registry = getattr(self, "say_registry", None)
        if armed is not None and registry is not None:
            key = f"q_{self.sk.question_number}_delivery"
            claim = registry.state(key)
            answered = False
            try:
                answered = self.question_already_answered(
                    self.sk.question_number
                )
            except Exception:
                pass
            if claim is not None and not answered:
                return f"delivery_claim_{claim}"
        return None

    def maybe_fire_late_recognition(self) -> bool:
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
        # ANTIREPEAT-PROTOCOL-001 (supersedes P5's _recognized_at_greet, an
        # inert flag that was initialized and never set): recognition content
        # has already reached the air — the confirmed greet, the organic
        # name-door turn, or an earlier beat. The durable fact (the
        # _result_aired pattern generalized) retires this beat PERMANENTLY:
        # the room heard the welcome-back once, and no later promotion (an
        # ECAPA match converging on a fragmented returner's second group —
        # the match-time group-equality guard's blind spot) may air it again.
        # Checked FIRST so a stray pending bit is cleared too.
        if self._recognition_aired is not None:
            self._late_recognition_fired = True
            self._late_recognition_pending = False
            return False
        if not self.memory_block or self._late_recognition_fired:
            return False
        # getattr: test harnesses build LilyGame via __new__.
        registry = getattr(self, "say_registry", None)
        greeted = (
            registry is not None
            and registry.state("session_greet") is not None
        )
        if not greeted and not getattr(self, "game_started", False):
            return False  # door path: greeting_instructions will act on it
        # P0-4 BE8D8B: never fire over delivery/window/adjudication. Keep a
        # pending bit and flush it only at an explicit between-question seam.
        blocked = self.late_recognition_blocked_reason()
        if blocked == "game_start_committed":
            # CLASS 7 (LIVEFIRE-001) 7a: forbidden, not deferred — the round
            # has started, so this beat is retired for the session rather than
            # held for a seam it can never safely take.
            self._late_recognition_fired = True
            self._late_recognition_pending = False
            logger.info(
                "LILY_MEMORY | LATE_RECOGNITION_FORBIDDEN | session=%s "
                "reason=game_start_committed — recognition retired post-start",
                self.sk.session_id,
            )
            return False
        if blocked:
            self._late_recognition_pending = True
            logger.info(
                "LILY_MEMORY | LATE_RECOGNITION_DEFERRED | session=%s "
                "reason=%s — holding for between-question seam",
                self.sk.session_id, blocked,
            )
            return False
        self._late_recognition_fired = True
        self._late_recognition_pending = False
        # Stored 'usual' honored for the remainder. The application now
        # ALSO runs at every promotion tail (_apply_stored_pacing,
        # ANTIREPEAT-PROTOCOL-001) so a short-circuited or refused beat
        # never drops it; kept here too for beats reached without a
        # promotion (idempotent — applies only on a difference).
        self._apply_stored_pacing()
        # V1 (HOTFIX-010): a match on the GROUP is not per-person recognition.
        # The old beat injected memory_player_names[:4] — a multi-session
        # union never scrubbed of STT conflations — and recited it as the
        # present table (the "Rami, Rhonda, Chris, Miranda" leak). Delete the
        # roster injection: name a person ONLY from the present-voice source
        # (sk.players, read from the ROSTER field in the state block), never
        # from memory.
        ack = (
            "Recognition just landed MID-SESSION: the [RETURNING TABLE] "
            "block now confirms this is a TABLE you have played with before. "
            "That is a match on the TABLE, not proof of who is on the mic "
            "right now. ONE warm acknowledgment beat that you know this "
            "table — own the late catch lightly ('took me a second'). Name a "
            "person ONLY when THEIR voice is matched present this session "
            "(the ROSTER field is the sole naming authority) or they have "
            "stated their name tonight; a remembered name is NOT a present "
            "person, so do not read any roster of names from memory. If no "
            "voice is matched present yet, name no one — just welcome the "
            "table back. Never pretend you knew all along, and never "
            "apologize in a spiral. THEN STOP AND LET THEM ANSWER. This turn "
            "is the acknowledgment and at most ONE offer ('want a refresher "
            "on the options, or straight in?') — it does NOT contain a "
            "question from the game, and it does not answer its own offer. "
            "Asking someone what they want and then telling them is worse "
            "than never asking."
            + self.prefs_offer_instruction()
            + self.whats_new_instruction()
            # Live 2026-08-12 15:26 ET (the double greeting): this beat
            # aired as a FULL second greeting — "Hi, I'm Lily — I host
            # trivia..." verbatim again, with the recognition bolted on.
            # Nothing forbade the reprise. Now something does.
            + " CRITICAL: the table has ALREADY heard your greeting this "
            "session — do NOT re-introduce yourself, do NOT repeat 'Hi, "
            "I'm Lily' or any line of your opener, and do NOT re-ask a "
            "question you already asked (like what to call them) unless it "
            "is still unanswered. This beat STARTS at the recognition."
        )
        present = ",".join(list(getattr(self.sk, "players", []) or [])) or "-"
        logger.info(
            "LILY_MEMORY | LATE_RECOGNITION | session=%s group=%s present=%s",
            self.sk.session_id, getattr(self, "group_id", None), present,
        )
        # Through the FUNNEL, not instructed_reply: the Y10 review's F5
        # listed this as one of the five raw lanes skipping every dispatch
        # gate — a late beat must respect a hold ("give us a minute")
        # exactly like everything else. Keyless: no claim, no retry ladder;
        # a refused beat re-arms via _late_recognition_pending as before.
        dispatched = self.gated_say(
            None, "late_recognition", ack, source="late_recognition"
        )
        if not dispatched:
            # The gate refused (hold/floor/flight) — the beat is not
            # burned; the seam flush retries it.
            self._late_recognition_fired = False
            self._late_recognition_pending = True
        else:
            # ANTIREPEAT-PROTOCOL-001: the beat carried the welcome-back
            # to dispatch — stamp the durable fact so no other lane (a
            # later promotion, the game-start ride-along) re-airs it.
            self.note_recognition_aired("late_recognition_beat")
        return dispatched

    def flush_late_recognition_at_seam(self) -> bool:
        """Emit a deferred recognition beat only when the game is between Qs."""
        if not self._late_recognition_pending:
            return False
        return self.maybe_fire_late_recognition()

    # -- ANTIREPEAT-PROTOCOL-001: the RECOGNITION-stated-on-air fact ---------
    #
    # BARGE-RESILIENCE-001 P1's _result_aired pattern, generalized to the
    # recognition lane. Live 2026-08-14 11:31: a name-door promotion BOTH
    # fed the in-flight organic reply (memory_block → _apply_context_blocks)
    # AND fired the late-recognition beat from the promotion tail — the same
    # welcome-back content through two independent lanes, ten seconds apart,
    # because no shared "recognition stated on air" fact existed (the beat
    # dispatches keyless, so the say-gate registry never deduped it, and
    # _recognized_at_greet was initialized but never set — an inert
    # kill-switch, now deleted in favor of this fact). Session-scoped and
    # permanent: recognition happens once a night by definition, so unlike
    # _result_aired there is no clear.

    def note_recognition_aired(
        self, source: str, text: str | None = None
    ) -> None:
        """Stamp that recognition/welcome-back content has gone to air (or
        is carried by a turn already in flight, for the name-door organic
        case). Idempotent: the first airing wins, so the record names the
        lane the room actually heard. Stamping retires the late beat and
        its pending bit — every recognition producer consults this fact."""
        if self._recognition_aired is not None:
            return
        self._recognition_aired = {
            "source": source,
            "text": (text or "").strip(),
            "at": time.monotonic(),
        }
        self._late_recognition_fired = True
        self._late_recognition_pending = False
        logger.info(
            "LILY_MEMORY | RECOGNITION_AIRED | session=%s source=%s — "
            "recognition is on air; every other recognition lane is retired "
            "for the session (ANTIREPEAT-PROTOCOL-001)",
            getattr(self.sk, "session_id", "?"), source,
        )

    def recognition_aired(self) -> dict | None:
        """The recognition-aired record ({source, text, at}), or None."""
        return self._recognition_aired

    def _apply_stored_pacing(self) -> None:
        """Apply the group's stored 'usual' pacing when it differs from the
        live flag. Extracted from the late-recognition beat (ANTIREPEAT-
        PROTOCOL-001): the application used to run ONLY when the beat fired,
        so a short-circuited (name-door) or refused beat silently dropped
        the table's saved pacing. Trigger-independent — called from every
        promotion tail and from the beat itself; idempotent (set_pacing is
        a no-op on equality). Session-spoken choices still win: the prefs
        merge at promotion keys session values over stored ones."""
        try:
            stored_pacing = (self.prefs or {}).get("pacing")
            if stored_pacing in ("timed", "relaxed") and (
                stored_pacing != self.sk.pacing
            ):
                self.sk.set_pacing(stored_pacing)
                self.publish_attributes_nowait()
        except Exception:
            pass

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
        memory = dict(memory or {})
        voiceprint_names = sorted({
            str(row.get("label") or "").strip()
            for row in voiceprints or []
            if str(row.get("label") or "").strip()
            and not re.fullmatch(
                r"S\d+|UU", str(row.get("label") or "").strip()
            )
        })
        if voiceprint_names and not memory.get("player_names"):
            # A short prior session may have enrolled names but not crossed
            # the game-memory write threshold. Voice verification still earns
            # those names; it does not earn invented scores/history.
            memory["player_names"] = voiceprint_names
        block = lily_memory.lily_build_memory_block(memory, prefs=prefs)
        if not block and not prefs and not voiceprints:
            logger.info(
                "LILY_MEMORY | DEVICE_CANDIDATE_EMPTY | source=%s group=%s",
                source, candidate_group_id,
            )
            return False
        self.device_candidate_group_id = candidate_group_id
        self.device_candidate_source = source
        self._device_candidate_memory = memory
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
        task = self._device_verify_task
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
        # HOTFIX-002 observability: attempts counted so a session that
        # ends still-quarantined can say how hard it tried (the WARN in
        # the close handler) instead of leaving silent amnesia.
        self._device_verify_attempts = (
            self._device_verify_attempts + 1
        )
        get_ids = getattr(self.stt, "get_speaker_ids", None)
        if get_ids is None:
            logger.warning(
                "LILY_MEMORY | DEVICE_VERIFY_UNAVAILABLE | trigger=%s "
                "reason=stt_has_no_get_speaker_ids — candidate can never "
                "promote this session", trigger,
            )
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

    async def _promote_device_candidate(
        self, trigger: str, *, verified: bool = True
    ) -> None:
        """`verified=False` promotes the memory WITHOUT closing identity.

        The name-stated door (below) resolves a returner off something they
        said, which is weaker than a voice. Setting device_identity_verified
        there would have latched the session shut and permanently blocked
        the ECAPA matcher — the exact shape of N5, where one misheard name
        outranked a biometric with twelve games behind it. So the name path
        hands over the memory and leaves the door open behind it."""
        candidate = self.device_candidate_group_id
        if not candidate:
            return
        memory = dict(self._device_candidate_memory or {})
        block = self._device_candidate_memory_block
        staged_prefs = dict(self._device_candidate_prefs)
        # Record HOW the table was recognised. This hardcoded
        # "voiceprint_match" and discarded its own `trigger`, so an ECAPA
        # centroid match and a Speechmatics identifier overlap were written
        # to the ledger identically. Those are different mechanisms with
        # very different reliability — the identifier blobs REFRESH every
        # session and can never match across sessions (README, verified
        # 2026-07-16), while the ECAPA centroid is the one that actually
        # found a twelve-game table on 2026-08-08. Collapsing them cost the
        # provenance an operator needs to debug exactly this class of
        # problem. Both remain strong sources; only the label changes.
        await self.upgrade_group_id(
            candidate, trigger if trigger in _KNOWN_GROUP_SOURCES
            else "voiceprint_match"
        )
        merged_prefs = staged_prefs
        merged_prefs.update(self.prefs or {})
        self.prefs = merged_prefs
        self.memory_block = block
        self.memory_total_games = int(memory.get("total_games") or 0)
        self.memory_player_names = list(memory.get("player_names") or [])
        if verified:
            self.device_identity_verified = True
        self.device_candidate_group_id = None
        self.device_candidate_source = None
        self._device_candidate_memory = None
        self._device_candidate_memory_block = ""
        self._device_candidate_prefs = {}
        self._device_candidate_voiceprints = []
        self.memory_settled.set()
        # ANTIREPEAT-PROTOCOL-001: stored pacing applies at the PROMOTION,
        # not inside the beat — trigger-independent, so a short-circuited
        # beat never costs the table its saved 'usual'.
        self._apply_stored_pacing()
        if trigger in _NAME_DOOR_TRIGGERS and self.memory_block:
            # Name-door short-circuit (ANTIREPEAT-PROTOCOL-001): the organic
            # reply answering the name utterance carries the just-promoted
            # memory block by construction — firing the late beat too was
            # the 2026-08-14 11:31 double welcome-back (same content, two
            # lanes, ten seconds apart). Stamp the durable fact and retire
            # the beat instead of dispatching it. The prefs offer needs no
            # beat either: the memory block's "usual:" line plus the system
            # prompt's standing instruction carry it.
            self.note_recognition_aired("name_door_organic")
            logger.info(
                "LILY_MEMORY | LATE_RECOGNITION_SHORT_CIRCUIT | session=%s "
                "trigger=%s group=%s — the organic turn carries the "
                "recognition; the late beat is retired",
                getattr(self.sk, "session_id", "?"), trigger, candidate,
            )
        else:
            # Task 1 (RECOGNITION-VARIETY): a voiceprint verification landing
            # after the greeting is the same late-recognition moment as a
            # name-hash upgrade — same acknowledgment beat, same one-shot.
            self.maybe_fire_late_recognition()
        logger.info(
            "LILY_MEMORY | DEVICE_CANDIDATE_VERIFIED | trigger=%s group=%s "
            "— returning memory promoted",
            trigger, candidate,
        )

    def identity_probe_outstanding(self) -> bool:
        """Is a voice-identity probe still running?

        HOTFIX-006 N1. This is the difference between "I have no memory of
        you" and "I do not know yet whether I have memory of you", and only
        the second was ever true at greeting time. Live 2026-08-08, three
        sessions in a row: Lily said "my memory bank is sitting on a
        completely clean slate for you all" and "tonight is actually a
        clean slate" — and roughly two and a half minutes later the matcher
        landed [RETURNING TABLE] with TWELVE games on file. The matcher was
        never wrong. It was simply not waited for, and its absence was
        narrated as a fact.

        While this is True, no line may assert the absence of memory —
        not "clean slate", not "blank card", not "my card doesn't have
        you". Saying nothing about memory is always available and always
        honest."""
        if self._voice_identity_resolved:
            return self._no_match_awaiting_name_door()
        if not lily_config.voice_identity_enabled():
            return False
        if getattr(self, "supabase", None) is None:
            return False
        # A device candidate already staged means recognition is in flight
        # by another route; either way the question is open, not closed.
        return True

    def _no_match_awaiting_name_door(self) -> bool:
        """HOTFIX-008 Z3: a biometric NO_MATCH keeps the probe OPEN while
        the stated-name door is untried.

        Live 2026-08-10, `lily-938EFF-2260354c` (RM_V7MnLQBeFMi9): the
        embedder returned NO_MATCH at 0.6968 against a 0.75 threshold three
        seconds into the session, `_voice_identity_resolved` flipped True,
        and forty seconds later the greeting said "my table card doesn't
        have you tonight, and I don't know why" — with every N1/Y9 hold
        surface dark because the probe read as closed. At +125s the player
        said "call me Rami", the name door matched grp_0b07f989, and the
        full callback landed ("reigning champ … underwater basket
        weaving"). Ninety seconds of "I don't know you" followed by
        knowing him completely.

        The no-match was one route reporting, not the question closing:
        name binding is mandatory lobby flow, so the stated-name lookup is
        a probe route that WILL run. Until it reports (or memory lands, or
        the bounded hold expires), absence of memory is still UNKNOWN and
        may not be spoken as a fact. The hold is time-bounded so an
        anonymous table is never gagged about memory forever — on expiry
        the question resolves empty and Y9's honest gap-naming is
        permitted exactly as before."""
        stamp = self._voice_identity_no_match_at
        if stamp is None:
            return False
        if getattr(self, "memory_block", None):
            return False
        if self._identity_name_door_checked:
            return False
        hold = lily_config.identity_no_match_hold_seconds()
        if hold <= 0:
            return False
        return (time.time() - stamp) < hold

    def recognition_dispute_blocks_start(self) -> bool:
        """P0-B: kickoff locked until the why-beat has landed."""
        if not self._recognition_dispute:
            return False
        return not self._recognition_dispute_why_answered

    def _identity_gate_satisfied(self) -> bool:
        """HOTFIX-010 V5: the name gate is a ONE-SHOT, never a standing
        block. Hosting requires no name first, so the gate is satisfied —
        and can no longer re-fire — the moment ANY of the WO's three
        conditions holds:
          * a name is captured (a real, non-placeholder roster entry);
          * a placeholder is in use (a present unnamed voice is hosting and
            scoring under its speaker-label anchor);
          * the session's one identity ask has been spent.
        Live 2026-08-10: "what should I call you?" fired seven times in
        3.5 min — appended to every turn AFTER the player had given the
        name and she had echoed it — because both gate sites keyed only on
        roster_size()<1 with no satisfaction path, and the name never bound
        to the roster. A name binds OPPORTUNISTICALLY whenever it arrives;
        once the gate is satisfied it is never re-requested."""
        if self.sk.roster_size(include_placeholder=False) >= 1:
            return True
        if self.sk.has_active_placeholder():
            return True
        if self._identity_ask_spent:
            return True
        return False

    def identity_intake_line(self) -> str | None:
        """HOTFIX-010 V5: the ONE name ask, folded into the opening beat —
        never appended to every turn. It offers itself only while the gate
        is unsatisfied (no name, no placeholder, ask unspent) and the game
        has not started; the moment a present voice takes the floor the
        gate satisfies (placeholder + ask-spent, set in
        on_user_turn_completed) and this returns None for the rest of the
        session. Replaces the old 'do not start Round One yet' hostage
        clause: hosting never waits on a name."""
        if getattr(self, "game_started", False):
            return None
        if not self._identity_required_before_start:
            return None
        if self._identity_gate_satisfied():
            return None
        return (
            "identity_intake: this is the ONE time to ask a name — no one "
            "has spoken yet. Fold a single short 'what should I call you?' "
            "into your opening beat. You will not ask again: whatever comes "
            "back — a name, or nothing — you host anyway. A name binds "
            "whenever it arrives (now or later); until then the voice plays "
            "and scores under its own place at the table."
        )

    def arm_recognition_dispute(self, *, reason: str) -> None:
        """Open a recognition dispute: inject the why-directive and lock
        start. Idempotent while already open."""
        already = bool(self._recognition_dispute)
        self._recognition_dispute = True
        if not already:
            self._recognition_dispute_why_answered = False
        self._recognition_why_note = (
            "[recognition dispute — a player challenged your clean-slate / "
            "empty-memory claim or asked WHY you spoke as if the record were "
            "final. Answer WHY in ONE sentence from this note before anything "
            "else. Grounded cause: your first identity/memory check looked "
            "empty and the protocol treated UNKNOWN as 'nothing on file' "
            "instead of 'still loading.' That was a bug in how you talk "
            "before the match finishes — not the player. Ban openers like "
            "'you\\'re right' / 'you\\'re completely right'. No category "
            "announce, no 'let\\'s kick', no lily_begin_round until this "
            "why-beat lands. Then follow their lead (refresher / start only "
            "when they ask).]"
        )
        logger.info(
            "LILY_HONESTY | RECOGNITION_DISPUTE | session=%s reason=%s "
            "why_answered=%s",
            getattr(self.sk, "session_id", "?"), reason,
            self._recognition_dispute_why_answered,
        )

    def note_late_answer(
        self,
        text: str,
        *,
        player: str | None,
        speaker_label: str | None = None,
        segment_ts: float | None = None,
        utterance_id: str | None = None,
    ) -> dict | None:
        """A correct answer that arrived after the window closed
        (WO-LILY-HOTFIX-006 N9 part 2).

        THE fixture: at 21:10:13 Rami said "Okay. It's Jupiter." and Lily
        replied "Jupiter was spot on, Rami, but just a split second late!"
        — the conversational lane knew the answer was correct AND who said
        it. The ledger for q_1052 recorded his answer as "Go." (his earlier
        start command), incorrect, zero points. His actual answer never
        entered the ledger; a different utterance was captured in its
        place.

        Late-but-correct is now one of two stated outcomes, never a silent
        loss: inside lily_config.late_answer_grace_seconds() the window
        itself still admits the speech (window_contains), and past that
        margin THIS records an explicit announced miss WITH ITS REASON and
        an audit row carrying the real utterance and its id. What is no
        longer possible is narrating correctness while recording a
        different utterance as wrong.

        Returns the late-answer record, or None when nothing applies.
        """
        # HOTFIX-009 W4: relaxed pacing files NOTHING late. The gate behind
        # "diamond is right. You had it. Just past the window, so it doesn't
        # score." rejects on timing; relaxed has no window to be past, so
        # this path is closed entirely in relaxed mode — no record, no
        # ledger row, no announced miss.
        if self.sk.pacing == "relaxed":
            return None
        binding = self.sk.window_binding()
        if not binding.get("registered"):
            return None
        question = self.sk.current_question or self.armed_question
        # The window that just closed belongs to the question it captured;
        # if the game has already moved on to a DIFFERENT question, this
        # utterance is not a late answer to anything adjudicable.
        if question is None or (
            binding.get("question_id") is not None
            and question.get("id") is not None
            and binding.get("question_id") != question.get("id")
        ):
            return None
        if self.sk.answer_window_open:
            return None  # the live window owns it, not this path
        if self._adjudicating or self.sk.adjudicating:
            return None  # the ruling is mid-commit; it owns the outcome
        if self._is_burned(question):
            # WS-4: the answer has already gone to air. A player echoing
            # the just-revealed answer is not a late attempt, and calling
            # it one would turn every reveal into a "you were right"
            # announcement.
            return None
        if lily_evaluation.lily_non_answer_utterance(
            text, question, list(self.sk.players)
        ):
            return None
        try:
            verdict = self._tier1_question(text, question)["verdict"]
        except Exception:
            return None
        if verdict != "correct":
            # Only a CORRECT late answer is a loss worth announcing; a
            # late wrong guess is just conversation.
            return None
        seconds_late = self.sk.seconds_past_deadline(segment_ts)
        if seconds_late is None or seconds_late <= 0:
            return None
        grace = max(0.0, lily_config.late_answer_grace_seconds())
        # N9: this row is ABOUT an utterance, so it gets an id even off the
        # late path — a row with no utterance identity is exactly what made
        # the q_1052 defect unreadable after the fact.
        utterance_id = utterance_id or self.sk._mint_utterance_id(
            speaker_label, segment_ts or 0.0
        )
        record = {
            "player": player,
            "speaker_label": speaker_label,
            "text": text,
            "utterance_id": utterance_id,
            "verdict": "correct",
            "seconds_late": seconds_late,
            "within_grace": seconds_late <= grace,
            "grace_seconds": grace,
            "question_id": binding.get("question_id"),
            "question_index": binding.get("question_index"),
        }
        self.sk.late_answers.append(record)
        logger.error(
            "LILY_ANSWER | LATE_MISS | session=%s q=%s player=%s late=%.3fs "
            "grace=%.3fs utterance=%s text=%r — correct after the window "
            "closed; announced as a miss WITH its reason, never silent",
            self.sk.session_id, record["question_index"], player,
            seconds_late, grace, utterance_id, str(text)[:80],
        )
        # The ledger carries the fact. Zero points (the window closed), its
        # own cause, and — critically — the REAL utterance with its id, so
        # nothing has to guess later what the player actually said.
        if self.supabase is not None:
            asyncio.ensure_future(lily_persistence.lily_write_answer(
                self.supabase,
                self.sk.session_id,
                player,
                record["question_id"],
                record["question_index"] or 0,
                text,
                "late",
                1,
                0,
                cause="late_answer",
                utterance_id=utterance_id,
            ))
        # REFACTOR W2a: an announced miss is verdict/score speech, so it is a
        # DETERMINISTIC direct_say beat — never an organic-lane note the LLM
        # weaves (the organic lane is forbidden from verdict/score speech). It
        # names them, confirms the answer was right, gives the just-past-the-
        # buzzer reason, and awards NO point (the ledger says zero).
        who = player or "that voice"
        self.gated_say(
            f"q_{self.sk.question_number}_late_answer",
            "late_answer",
            "A correct answer arrived just after the window closed. Name the "
            "player, confirm it was right, say it landed just past the buzzer "
            "so it doesn't score, and do NOT award a point.",
            source="late_answer",
            text=(
                f"Quick one — {who} said {text.strip()}, and that was right, "
                "just past the buzzer. No point this time, but nice one."
            ),
        )
        return record


    # -- group identity (persistent memory re-key) -------------------------------

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
            self.stt, self.supabase, self._effective_enroll_group_id, self.sk,
            trigger=trigger,
        ))

    def _effective_enroll_group_id(self) -> str:
        """Group id voiceprint enrollment writes under. RECONCILE-001 (d) —
        stop the bleeding: when the LIVE group is the ephemeral room-name
        fallback but a device-STABLE candidate group was carried in
        (dispatch/participant token metadata — the browser's localStorage id),
        enroll under the DEVICE group. That stable key recurs every session
        from the same browser, so the individual's voiceprints ACCUMULATE in
        one place and the next session's matcher has real centroids to compare
        — instead of a throwaway room name that mints a fresh fragment per
        unrecognized session (the 31-group split). Same-device is an approved
        merge basis; only the voiceprint WRITE is redirected — reading that
        group's MEMORY stays quarantined until a voice match. A voice-REJECTED
        candidate (a stranger on a shared device) is never written under, and
        once identity is verified/upgraded self.group_id already IS the real
        group, so the live id is used unchanged."""
        live = self.group_id
        if getattr(self, "group_id_source", "") != "room_name":
            return live
        if getattr(self, "device_identity_verified", False):
            return live
        if getattr(self, "device_identity_rejected", False):
            return live
        carried = getattr(self, "_carried_device_group_id", None)
        if carried and carried != live:
            return carried
        return live

    def _schedule_fragment_merge(self, canonical_group_id, emb, identities) -> None:
        """RECONCILE-001 (b) background arm: after a voice-verified match,
        sweep OTHER single-player fragments of the SAME individual into the
        canonical group. Fire-and-forget, off the vocal path."""
        if (
            self.supabase is None
            or not canonical_group_id
            or emb is None
            or not lily_config.voice_identity_enabled()
        ):
            return
        asyncio.ensure_future(
            self._merge_name_fragments_bg(
                canonical_group_id, emb, list(identities or [])
            )
        )

    async def _merge_name_fragments_bg(
        self, canonical_group_id, emb, identities
    ) -> None:
        """Find single-player fragments sharing this individual's name(s),
        confirm each by a voice link (its stored centroid vs the live
        embedding), merge the voice-linked ones, and log the rest as
        MERGE_CANDIDATE (name-only overlap never merges — safety bar)."""
        try:
            names = {
                str(n).strip()
                for n in (self.memory_player_names or [])
                if str(n).strip()
            }
            if not names:
                return
            threshold = lily_config.voice_identity_match_threshold()
            by_group: dict = {}
            for row in identities or []:
                gid = row.get("group_id") if isinstance(row, dict) else None
                if gid and gid not in by_group:
                    by_group[gid] = row.get("centroid")
            to_merge: list = []
            for name in names:
                fragments = await lily_persistence.lily_find_name_fragments(
                    self.supabase, canonical_group_id, name
                )
                for frag in fragments:
                    gid = frag["group_id"]
                    centroid = by_group.get(gid)
                    sim = (
                        lily_voice_identity.lily_cosine_similarity(emb, centroid)
                        if centroid is not None else None
                    )
                    frag_voice = sim is not None and sim >= threshold
                    frag["voice_match"] = frag_voice
                    if lily_persistence.lily_reconcile_safety_bar(
                        canonical_group_id, frag, voice_linked=frag_voice
                    ):
                        if gid not in to_merge:
                            to_merge.append(gid)
                    else:
                        logger.info(
                            "LILY_MERGE_GROUPS | MERGE_CANDIDATE | canonical=%s "
                            "fragment=%s name=%s voice_sim=%s — name match "
                            "without a confirmed voice link; NOT merging",
                            canonical_group_id, gid, name,
                            f"{sim:.3f}" if sim is not None else "none",
                        )
            if to_merge:
                await lily_persistence.lily_merge_groups(
                    self.supabase, canonical_group_id, to_merge,
                    reason="background_name_fragment_sweep",
                )
        except Exception as e:
            logger.warning(
                "LILY_MERGE_GROUPS | BACKGROUND_SWEEP_FAILED | canonical=%s: %s",
                canonical_group_id, e,
            )

    # -- durable voice identity (WO-LILY-VOICE-IDENTITY-001) --------------------
    #
    # The device-independent recognizer: OUR OWN ECAPA embedding, matched by
    # cosine + margin across devices, so "you should know my voice" works even
    # on a new device / cleared browser (the vendor blobs can't bridge
    # sessions). The whole feature stays INERT until three things are present —
    # the flag, the embedder model in the image, and captured audio — so a
    # deploy without them behaves exactly as today. The audio probe is the one
    # remaining live-infra seam (a track frame sink populates
    # `_voice_identity_pcm`); everything else is wired and tested.

    def _voice_capture_allowed(self) -> bool:
        """May we CAPTURE audio for the voiceprint? Deliberately does NOT
        require the embedding model to be loaded.

        HOTFIX-006, regression introduced 2c8ecf5: lily_claim_voice_probe
        gated the audio fork on _voice_identity_ready(), and that check
        became "is the model loaded?" when the load moved off the event
        loop. The claim fires on track-subscribe, at connect, before the
        prewarm can possibly have finished — so on a cold worker the fork
        was never claimed, no audio was ever captured, and recognition
        could not happen at all. On a warm worker (module-level model
        cached from an earlier session in the same process) it worked,
        which is exactly the kind of intermittency that hides a bug.

        Capture and match have different prerequisites. Capture needs a
        session and a destination; matching needs the model. Conflating
        them made the cheap half wait on the expensive half."""
        return (
            lily_config.voice_identity_enabled()
            and getattr(self, "supabase", None) is not None
        )

    def _voice_identity_ready(self) -> bool:
        """Cheap, NON-BLOCKING readiness. This used to end in
        the embedder's blocking availability check, which loads the model — and the
        first such call downloads spkrec-ecapa-voxceleb and loads a torch
        model, multi-second work. It is reached from the transcript handler
        on the EVENT LOOP, so the first player utterance blocked the loop
        for the entire load, and the Silero VAD sharing that loop fell
        behind and never caught up (24.9s and 33s measured live). Barge-in,
        turn commit and TTS delivery all ride on that same loop.

        The load now happens in a thread (_warm_voice_embedder); this only
        READS whether it finished."""
        return (
            lily_config.voice_identity_enabled()
            and self.supabase is not None
            and lily_voice_embedder.lily_voice_embedder_loaded()
        )

    def _warm_voice_embedder(self) -> None:
        """Kick the one-time model load OFF the event loop. Fire-and-forget
        and idempotent; failure just leaves the feature inert."""
        if self._voice_embedder_warming:
            return
        if not lily_config.voice_identity_enabled() or self.supabase is None:
            return
        if lily_voice_embedder.lily_voice_embedder_load_attempted():
            return
        self._voice_embedder_warming = True

        async def _warm() -> None:
            try:
                ok = await lily_voice_embedder.lily_warm_voice_embedder()
                if ok:
                    logger.info(
                        "LILY_VOICE_ID | EMBEDDER_WARM | loaded=True (off-loop)"
                    )
                else:
                    # A misconfiguration must be LOUD, not a shrug: the
                    # feature is enabled, the deploy can't run it, and the
                    # visible symptom is Lily forgetting returning players
                    # (live 2026-08-09 complaint). Say exactly what to do.
                    logger.error(
                        "LILY_VOICE_ID | EMBEDDER_UNAVAILABLE | voice "
                        "recognition is DISABLED this session — cross-device "
                        "memory cannot match and a staged device candidate "
                        "can only promote via the vendor-label or stated-"
                        "name doors. Install requirements-voice-identity.txt "
                        "in the deploy image (and apply migrations/021), or "
                        "set LILY_VOICE_IDENTITY_ENABLED=false to make this "
                        "degraded mode an explicit choice."
                    )
            except Exception as e:
                logger.warning("LILY_VOICE_ID | EMBEDDER_WARM_FAILED | %s", e)

        asyncio.ensure_future(_warm())

    def _preload_voice_identities(self) -> None:
        """V2: fetch the centroid pool at CONNECT, off the first-utterance
        path. The pool DB round-trip used to sit INSIDE
        _voice_identity_match_at_start, AFTER the embedding — a serial fetch on
        the recognition critical path that grows with fleet enrollment. Kicked
        here (concurrent with the embedder prewarm, before any voice), the
        match reads an in-memory pool and does no DB round-trip. Fire-and-
        forget and idempotent; a slow load just leaves the match's own
        cold-path fetch as the fallback."""
        if self._voice_identity_pool_loading:
            return
        if not lily_config.voice_identity_enabled() or self.supabase is None:
            return
        self._voice_identity_pool_loading = True
        tag = lily_config.voice_identity_model_tag()

        async def _load() -> None:
            try:
                pool = await lily_persistence.lily_load_voice_identities(
                    self.supabase, tag
                )
                self._voice_identity_pool = pool
                self._voice_identity_pool_loaded = True
                logger.info(
                    "LILY_VOICE_ID | POOL_PRELOAD | count=%d tag=%s — centroid "
                    "pool cached at connect, off the first-utterance path",
                    len(pool), tag,
                )
            except Exception as e:
                logger.warning("LILY_VOICE_ID | POOL_PRELOAD_FAILED | %s", e)

        asyncio.ensure_future(_load())

    def _voice_identity_audio_probe(self):
        """Captured mono PCM for embedding, or None when unavailable. Reads a
        buffer a track frame sink fills (`_voice_identity_pcm`); None keeps the
        feature inert until that sink lands. Injected directly in tests."""
        return self._voice_identity_pcm

    def maybe_start_voice_identity_match(self) -> bool:
        """Start the one session match only after captured PCM is ready.

        Returns True when scheduled. A pre-probe call remains retryable; this
        is the key distinction from the former first-final one-shot.
        """
        # Warming is what makes _voice_identity_ready() cheap: the load runs
        # in a thread while this call returns immediately. The trigger is
        # retryable by design, so a not-yet-warm model simply means "next
        # transcript".
        self._warm_voice_embedder()
        if (
            self._voice_identity_attempted
            or not self._voice_identity_ready()
            or self._voice_identity_audio_probe() is None
        ):
            return False
        self._voice_identity_attempted = True
        asyncio.ensure_future(self._voice_identity_match_at_start())
        return True

    async def _voice_identity_match_at_start(self) -> bool:
        """Probe the joining voice against stored centroids; on a confident
        match, stage+promote that group's memory through the existing
        candidate path (the biometric match IS the proof — no vendor-label
        round-trip needed). Returns True on a promotion."""
        if not self._voice_identity_ready() or getattr(
            self, "device_identity_verified", False
        ):
            # Not going to run at all — nothing is outstanding.
            self._voice_identity_resolved = True
            return False
        probe = self._voice_identity_audio_probe()
        if probe is None:
            return False
        try:
            emb = await lily_voice_embedder.lily_extract_embedding_async(probe)
            if emb is None:
                return False
            # V2 instrumentation: t1 = embedding produced. t0 was stamped in
            # the frame sink at the first match_ready crossing, so embed_ms
            # spans utterance-ready -> embedding and folds in any wait on a
            # still-warming model (a large embed_ms points straight at the
            # model-load/STT dependency).
            t1 = time.monotonic()
            # V2: the centroid pool is preloaded at CONNECT (in-memory, no DB
            # round-trip on the recognition path). Cold-path fallback ONLY
            # when the first utterance beat the preload — fetch inline so the
            # feature is never silently inert.
            if self._voice_identity_pool_loaded:
                identities = self._voice_identity_pool or []
            else:
                tag = lily_config.voice_identity_model_tag()
                identities = await lily_persistence.lily_load_voice_identities(
                    self.supabase, tag
                )
                logger.info(
                    "LILY_VOICE_ID | POOL_COLD_FETCH | session=%s — preload "
                    "not ready at first utterance; fetched inline",
                    self.sk.session_id,
                )
            match = lily_voice_identity.lily_match_voice(
                emb, identities,
                threshold=lily_config.voice_identity_match_threshold(),
                margin=lily_config.voice_identity_match_margin(),
            )
            # V2 instrumentation: t2 = identity resolved. resolve_ms spans
            # embedding -> match decision; a large resolve_ms is the DB
            # round-trip on the path (exactly what the preload above removes).
            t2 = time.monotonic()
            t0 = self._voice_identity_match_t0
            if t0 is not None:
                embed_ms = round((t1 - t0) * 1000, 1)
                resolve_ms = round((t2 - t1) * 1000, 1)
                self._voice_id_embed_ms = embed_ms
                self._voice_id_resolve_ms = resolve_ms
                logger.info(
                    "LILY_VOICE_ID | LATENCY | embed_ms=%s resolve_ms=%s "
                    "session=%s", embed_ms, resolve_ms, self.sk.session_id,
                )
            self._voice_identity_resolved = True
            # Recognition-latency closure (lily-639007: 2.5 min to know a
            # player whose centroid was 2.5h fresh — and nothing persisted
            # said whether the match MISSED or never RAN). The outcome now
            # rides the session report; no log export needed to tell.
            self._voice_id_outcome = (
                "no_match" if match is None
                else f"match:{str(match['group_id'])[:16]}:{match['score']:.3f}"
            )
            if match is None or match["group_id"] == self.group_id:
                if match is None:
                    # Z3: no-match is not resolution while the name door
                    # is untried — hold memory-characterising speech.
                    self._voice_identity_no_match_at = time.time()
                    # V7/V1c resolve-before-propose: the enrolled-voice route
                    # has now REPORTED (no match). If the roster is already
                    # stable (game started) on a weak group, resolve_group_
                    # identity may have DEFERRED the name-set proposal waiting
                    # on exactly this answer — re-invoke it so the name-set
                    # hash is quarantined now (never ahead of the biometric,
                    # and never minted from a heard name alone).
                    if (
                        getattr(self, "game_started", False)
                        and self.group_id_source not in _STRONG_GROUP_SOURCES
                        and not getattr(self, "device_candidate_group_id", None)
                    ):
                        try:
                            await self.resolve_group_identity("voice_no_match")
                        except Exception as e:
                            logger.warning(
                                "LILY_MEMORY | GROUP_ID_RESOLVE | "
                                "voice_no_match re-resolve failed: %s", e,
                            )
                return False
            logger.info(
                "LILY_VOICE_ID | MATCH_AT_START | session=%s group=%s score=%.3f",
                self.sk.session_id, match["group_id"], match["score"],
            )
            staged = await self.stage_device_candidate(
                match["group_id"], "voice_identity_match"
            )
            if staged:
                await self._promote_device_candidate("voice_identity_match")
                self._schedule_fragment_merge(match["group_id"], emb, identities)
                return True
            # V7: the biometric resolved an identity but the matched group
            # has no memory to stage (a thin prior table below the
            # game-memory write threshold, or a group carrying only a
            # centroid). stage_device_candidate returns False for empty
            # memory, and returning False HERE dropped the resolved identity
            # — so a session that booted onto the room-name fallback kept
            # group_id == session_id, a throwaway surviving a session in
            # which a KNOWN voice spoke. The centroid match IS the proof of
            # identity independent of how much memory is on file: bind to it
            # so this session's rows and its fresh voiceprint sample land on
            # the real group (upgrade_group_id rekeys + re-enrolls under it),
            # never on a throwaway the name-set hash would later fragment.
            await self.upgrade_group_id(
                match["group_id"], "voice_identity_match"
            )
            self.device_identity_verified = True
            self._schedule_fragment_merge(match["group_id"], emb, identities)
            return True
        except Exception as e:
            self._voice_identity_resolved = True
            self._voice_id_outcome = f"failed:{type(e).__name__}"
            # Z3: a failed probe gave no answer either — same hold shape.
            self._voice_identity_no_match_at = time.time()
            logger.warning("LILY_VOICE_ID | MATCH_AT_START_FAILED | %s", e)
            return False

    async def _voice_identity_enroll_at_close(self) -> bool:
        """Fold this session's captured voice into the group's stored centroid
        so the next session (any device) recognizes it. Runs at close, off the
        vocal path; skipped when identity persistence is disallowed (forget).

        NEVER ENROLLS INTO A THROWAWAY GROUP. This wrote to self.group_id
        unconditionally, and when group resolution had fallen back to the
        room name that minted a brand-new orphan centroid instead of adding
        a sample to the real one. An orphan keyed to a room name is worse
        than useless: the room never recurs, so nothing can ever match TO
        it, and it survives only as an extra candidate that thins the
        margin check for every genuine match afterwards. The voiceprint was
        not missing — it was being shredded, one orphan per broken session.

        Live 2026-08-08: three rows where there should have been one.
        grp_0b07f989 held 4 samples from 08-07; sessions at 07:30 and 18:59
        each wrote a fresh 1-sample orphan under a room-name group while
        the operator was telling Lily she ought to know his voice.

        On a weak group, the voice is matched FIRST and the sample folds
        into whatever identity it matches — the biometric is the signature,
        so it decides where its own sample lands. No match means no write:
        a sample with nowhere real to go is dropped rather than orphaned."""
        if not self._voice_identity_ready() or not self.identity_persistence_allowed():
            return False
        probe = self._voice_identity_audio_probe()
        if probe is None:
            return False
        try:
            emb = await lily_voice_embedder.lily_extract_embedding_async(probe)
            if emb is None:
                return False
            tag = lily_config.voice_identity_model_tag()
            existing = await lily_persistence.lily_load_voice_identities(
                self.supabase, tag
            )
            enroll_group = self.group_id
            source = getattr(self, "group_id_source", "")
            weak_source = source in ("room_name", "name_set_hash")
            prior_self = next(
                (r for r in existing if r["group_id"] == self.group_id),
                None,
            )
            # Every not-yet-enrolled group asks the GLOBAL biometric pool
            # first. A weak room/name-set identity may never found or
            # reinforce its own centroid: 9337B1's bogus `Playing` name-set
            # minted a rival beside the seven-sample canonical Rami row.
            must_match_existing = weak_source or prior_self is None
            if must_match_existing:
                candidates = [
                    r for r in existing
                    if not (weak_source and r["group_id"] == self.group_id)
                ]
                match = lily_voice_identity.lily_match_voice(
                    emb, candidates,
                    threshold=lily_config.voice_identity_match_threshold(),
                    margin=lily_config.voice_identity_match_margin(),
                )
                if match is not None:
                    enroll_group = match["group_id"]
                    logger.info(
                        "LILY_VOICE_ID | ENROLL_REDIRECTED | session=%s "
                        "from=%s to=%s score=%.3f — folding the sample into "
                        "the identity the voice actually matches",
                        self.sk.session_id, self.group_id, enroll_group,
                        match["score"],
                    )
                elif weak_source:
                    logger.warning(
                        "LILY_VOICE_ID | ENROLL_SKIPPED_ORPHAN | session=%s "
                        "group=%s source=%s — weak group and no voice match; "
                        "quarantining the sample rather than minting or "
                        "reinforcing a rival centroid",
                        self.sk.session_id, self.group_id, source,
                    )
                    return False
                elif source not in ("participant_metadata", "env_override"):
                    logger.warning(
                        "LILY_VOICE_ID | ENROLL_SKIPPED_UNVERIFIED | "
                        "session=%s group=%s source=%s — no existing voice "
                        "match and group provenance cannot found an identity",
                        self.sk.session_id, self.group_id, source,
                    )
                    return False
            prior = next(
                (r for r in existing if r["group_id"] == enroll_group), None
            )
            centroid, count = lily_voice_identity.lily_update_centroid(
                prior["centroid"] if prior else None,
                prior["sample_count"] if prior else 0,
                emb,
            )
            return await lily_persistence.lily_upsert_voice_identity(
                self.supabase, group_id=enroll_group, centroid=centroid,
                sample_count=count, model_tag=tag,
            )
        except Exception as e:
            logger.warning("LILY_VOICE_ID | ENROLL_AT_CLOSE_FAILED | %s", e)
            return False

    async def merge_speakers(
        self, from_label: str, into_player: str, source: str = "operator"
    ) -> dict:
        """WS-8 operator identity reconciliation — ONE transaction across
        roster and voiceprints, retro-attributing the merged label's prior
        utterances. The diarizer split one person across two labels (the
        live S1/S4 = Chris case); this folds them back together so roster,
        transcripts, and voiceprints agree and no duplicate voiceprint row
        survives.

        Roster side runs synchronously (in-memory, immediate); the durable
        side (transcript/addressee retro + voiceprint dedupe) is awaited so
        the caller can confirm the reconciliation actually landed."""
        label = (from_label or "").strip().strip("[]")
        into = (into_player or "").strip()
        if not label or not into:
            return {"ok": False, "reason": "missing_label_or_player"}
        roster = self.sk.merge_speakers(label, into)
        logger.info(
            "LILY_MERGE | ROSTER | session=%s source=%s from_label=%s into=%s "
            "candidates=%d",
            self.sk.session_id, source, label, into,
            roster.get("candidates_moved", 0),
        )
        # A held open-floor award for the merged label commits now that the
        # voice has an owner — same path as a late bind.
        pending = self._pending_unbound_award
        if pending and pending.get("speaker_label") == label:
            self._pending_unbound_award = None
            self.sk.record_result(
                into, correct=True, points=pending["points"],
                # N3/N9: the merge commits the SAME question and the SAME
                # utterance the held award was decided from — never
                # whatever the scorekeeper happens to be on now.
                question_id=pending.get("question_id"),
                question_index=pending.get("question_index"),
                transcript=pending.get("transcript"),
                utterance_id=pending.get("utterance_id"),
            )
        durable = {}
        if self.supabase is not None:
            durable = await lily_persistence.lily_merge_speaker(
                self.supabase, self.sk.session_id, self.group_id, label, into
            )
        self.publish_attributes_nowait()
        # Re-enroll so the surviving single voiceprint row carries the
        # merged label's identifiers under the resolved name.
        self.fire_enrollment("speaker_merge")
        return {"ok": True, "roster": roster, "durable": durable}

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
        # RECONCILE-001 (b): heal as we recognize. When a VOICE-VERIFIED match
        # binds this session to an existing group and the old id was the
        # EPHEMERAL room-name orphan this session minted (old == session_id),
        # fold that whole fragment into the canonical group so it stops
        # existing — the conservative rekey above moved only a subset. Gated to
        # the room-name throwaway so a multi-player name-set family is NEVER
        # swept into one member's individual group on a single voice match
        # (individual vs. collection). Voice verification linking the two
        # groups is the safety bar.
        if (
            old
            and old != new_group_id
            and source in _STRONG_GROUP_SOURCES
            and old == self.sk.session_id
        ):
            try:
                await lily_persistence.lily_merge_groups(
                    self.supabase, new_group_id, [old],
                    reason=f"verify_time_heal:{source}",
                )
            except Exception as e:
                logger.warning(
                    "LILY_MERGE_GROUPS | VERIFY_TIME_HEAL_FAILED | old=%s "
                    "new=%s: %s", old, new_group_id, e,
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
                    # Q0: keep the focus set in lockstep with the enrolled set
                    # (mid-game enrollment / reconnect) so a newly-enrolled
                    # player is heard on the next StartRecognition and the set
                    # never goes empty under IGNORE.
                    _fk = lily_stt_focus_kwargs(known_speakers)
                    if _fk:
                        opts.focus_speakers = _fk["focus_speakers"]
                        opts.focus_mode = _fk["focus_mode"]
                    logger.info(
                        "VOICEPRINT | refreshed known_speakers=%d group=%s "
                        "focus=%s",
                        len(known_speakers), new_group_id,
                        "ignore" if _fk else "off",
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
        # ANTIREPEAT-PROTOCOL-001: pacing application is promotion-side and
        # trigger-independent (the stored prefs were just reconciled above).
        self._apply_stored_pacing()
        if source in _NAME_DOOR_TRIGGERS and self.memory_block:
            # Name-door short-circuit, upgrade-tail leg: a name-door
            # promotion awaits THIS upgrade before its own tail runs, so
            # without the guard here the beat fired from inside the upgrade
            # — same 11:31 double, one call frame earlier. The organic turn
            # answering the name utterance carries the memory.
            self.note_recognition_aired("name_door_organic")
            logger.info(
                "LILY_MEMORY | LATE_RECOGNITION_SHORT_CIRCUIT | session=%s "
                "trigger=%s group=%s — the organic turn carries the "
                "recognition; the late beat is retired (upgrade tail)",
                getattr(self.sk, "session_id", "?"), source, new_group_id,
            )
        else:
            # Task 1: recognition that arrives AFTER the door becomes a
            # recovery moment, not a silent nothing.
            self.maybe_fire_late_recognition()
        self.fire_enrollment("group_id_upgrade")

    async def maybe_recognize_by_stated_name(self, player_name: str) -> bool:
        """A returner who SAYS their name must not wait on a biometric.

        Live 2026-08-08 `lily-2C489B`: the player said "My name is Rami" at
        twenty-two seconds. Recognition landed at 3m31s — SIXTEEN player
        turns later — because the only door open was the ECAPA matcher, and
        the matcher was behind a cold model load. In between he said "I have
        met you a million times", "you still don't remember me", and "I just
        told you my name. You forgot my name already." The information was
        in the room the whole time; nothing was listening for it.

        This is a WEAK door and is built like one:
          - it needs an UNAMBIGUOUS single group for the name — two tables
            with a Rami resolve to neither, because merging two families'
            histories is worse than being slow;
          - it does not set device_identity_verified, so the ECAPA matcher
            still runs and its verdict still outranks this one;
          - "name_stated" is deliberately absent from _STRONG_GROUP_SOURCES,
            so a later biometric may overwrite it (N5 in the correct
            direction: voice beats name, never the reverse).

        Returns True when memory was promoted."""
        name = (player_name or "").strip()
        if not name or self.supabase is None:
            return False
        # Post-forget, recognition stays shut (WO-LILY-FORGETME-001).
        if not self.identity_persistence_allowed():
            return False
        # Already known, already resolving, or already bound by something
        # stronger — this door has nothing to add.
        if self.memory_block or getattr(self, "device_identity_verified", False):
            return False
        if getattr(self, "device_candidate_group_id", None):
            # 2026-08-09 amnesia fix: a STAGED device candidate used to slam
            # this door shut unconditionally — promotion then waited on
            # voice verification alone, and on a deploy without the ECAPA
            # deps (or before the vendor labels resolve) that wait was
            # FOREVER. Absurd ordering: a stated name ALONE opened the door
            # below, but device-match PLUS the same stated name stayed
            # quarantined. When the stated name is on the staged file, the
            # combined evidence (this device's own history + a matching
            # name) promotes exactly as WEAKLY as the name-only door:
            # verified=False, so the biometric still runs and still
            # outranks it (N5 direction preserved). A name NOT on the
            # staged file keeps the quarantine — a stranger on a shared
            # device stays a fresh table.
            staged_names = {
                str(n).strip().casefold()
                for n in (
                    self._device_candidate_memory or {}
                ).get("player_names") or []
                if str(n).strip()
            }
            if name.casefold() in staged_names:
                logger.info(
                    "LILY_MEMORY | NAME_DOOR_DEVICE_MATCH | session=%s "
                    "name=%s group=%s — stated name is on this device's "
                    "staged file; promoting weakly (voice still outranks)",
                    self.sk.session_id, name,
                    self.device_candidate_group_id,
                )
                await self._promote_device_candidate(
                    "device_plus_name", verified=False
                )
                return True
            return False
        if self.group_id_source in _STRONG_GROUP_SOURCES:
            return False
        try:
            groups = await lily_persistence.lily_groups_for_player_name(
                self.supabase, name
            )
        except Exception as e:
            logger.warning("LILY_MEMORY | NAME_DOOR_FAILED | %s", e)
            return False
        # Z3: the stated-name route has now REPORTED for this table —
        # whatever the result, the identity question is no longer waiting
        # on this door, so the no-match hold may release.
        self._identity_name_door_checked = True
        groups = [g for g in groups if g and g != self.group_id]
        if not groups:
            return False
        if len(groups) == 1:
            candidate = groups[0]
        elif len(groups) > _NAME_DOOR_AMBIGUOUS_CEILING:
            # Pathological: too many same-name candidates to be one fragmented
            # person; a wrong guess is likely, so wait for the voice.
            logger.info(
                "LILY_MEMORY | NAME_DOOR_AMBIGUOUS | name=%s groups=%d — past "
                "the ambiguity ceiling (%d); waiting for the voice rather than "
                "guessing",
                name, len(groups), _NAME_DOOR_AMBIGUOUS_CEILING,
            )
            return False
        else:
            # RECONCILE-001 (e): >1 candidate is the COMMON case for a returner
            # fragmented across groups. Stage the MOST RECENT candidate WEAKLY
            # (verified=False below) — the ECAPA matcher still runs and still
            # outranks/rejects it, and a confirmed match then folds the split
            # groups together via the background merge. lily_groups_for_player_
            # name returns most-recent-first, so groups[0] is the freshest.
            candidate = groups[0]
            logger.info(
                "LILY_MEMORY | AMBIGUOUS_PICKED_RECENT | name=%s groups=%d "
                "picked=%s — staged weakly; the voice matcher still outranks "
                "and a confirmed match heals the fragments",
                name, len(groups), candidate,
            )
        staged = await self.stage_device_candidate(candidate, "name_stated")
        if not staged:
            return False
        logger.info(
            "LILY_MEMORY | NAME_DOOR_OPENED | session=%s name=%s group=%s — "
            "recognised off a stated name; the voice matcher still runs and "
            "still outranks this",
            self.sk.session_id, name, candidate,
        )
        await self._promote_device_candidate("name_stated", verified=False)
        return True

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
        # (c) name-set hash fallback — deterministic across sessions, but
        # minted only AFTER the enrolled-voice route has reported. V7
        # resolve-before-mint: the ECAPA matcher is the cross-session signal
        # that actually holds a table together; the name-set hash keys on
        # THIS session's HEARD names, so a single mishearing mints a fresh
        # group and one table fractures across sessions (three grp_ hashes
        # for one table, live 2026-08-10). While a voice match is in flight,
        # do NOT mint — the no-match branch of _voice_identity_match_at_start
        # re-invokes this resolver once the voice has reported, so a genuine
        # new table still mints, just never AHEAD of the biometric.
        if new_id is None:
            if (
                lily_config.voice_identity_enabled()
                and self._voice_identity_attempted
                and not self._voice_identity_resolved
            ):
                logger.info(
                    "LILY_MEMORY | GROUP_ID_RESOLVE | trigger=%s deferring "
                    "name-set proposal — enrolled-voice match in flight "
                    "(resolve before propose)", trigger,
                )
                return
            # V1c IDENTITY — ONE AUTHORITY: a heard name set may PROPOSE a
            # group, never MINT or SWITCH one. This is N5's structural half.
            # The symptom (voice_identity_match missing from
            # _STRONG_GROUP_SOURCES) was patched, but a name-set hash could
            # still slam group_id from under a live biometric — whoever wrote
            # last won, except when it didn't. A single mishearing ("Hi, I'm
            # Miranda") changes the heard set, changes the hash, and minted a
            # SECOND memory for one table. The name-set hash is now quarantined
            # exactly like late device metadata: stage_device_candidate loads
            # history ONLY when that group already exists on file and returns
            # False on an empty/new hash, so a genuinely-new table — or a
            # one-off mishearing — creates NOTHING and stays on its anonymous
            # session id. Only a biometric confirmation (verify_device_
            # candidate) may promote the candidate to the authoritative
            # group_id. Env-override and voice remain the SOLE authorities
            # that create or switch a group.
            hashed = lily_memory.lily_name_set_group_id(names)
            if (
                hashed
                and hashed != self.group_id
                and not getattr(self, "device_candidate_group_id", None)
            ):
                staged = await self.stage_device_candidate(
                    hashed, "name_set_hash"
                )
                if staged:
                    logger.info(
                        "LILY_MEMORY | NAME_SET_QUARANTINED | trigger=%s "
                        "group=%s — heard name set matches a table on file; "
                        "quarantined until a voice confirms it, never minted "
                        "from a name alone", trigger, hashed,
                    )
                    self.request_device_verification(trigger)
                else:
                    logger.info(
                        "LILY_MEMORY | NAME_SET_NO_TABLE | trigger=%s "
                        "group=%s — heard name set has no table on file; "
                        "creating nothing, staying anonymous on %s",
                        trigger, hashed, self.group_id,
                    )
            return
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
            # Biometric arm of the cascade: retire the durable voiceprint so a
            # forgotten voice stops matching (matching reads active only).
            await lily_persistence.lily_retire_voice_identity(self.supabase, target)
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

