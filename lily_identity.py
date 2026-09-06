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

import lily_bank
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

# WO-LILY-VOICE-TRUTH-001 V3: promotion sources that CONFIRM identity (the
# operator's PAIR 1 binding — a name may be spoken only under one of these).
# voice: the ECAPA/vendor biometric; name_stated / device_plus_name: the
# player gave their name THIS session. Everything else (a staged or promoted
# device candidate with no stated name, memory present with verified=False)
# is GUESSED: no name, no "welcome back".
_CONFIRMED_IDENTITY_SOURCES = (
    "voice_identity_match",
    "voiceprint_match",
    "name_stated",
    "device_plus_name",
)

# V3: a dispatched late beat / an armed carry watch that never reaches the air
# inside this many seconds is dead (a wedged or invalidated flight); the beat
# re-arms as OWED instead of holding the seam forever.
_RECOG_FLIGHT_STALE_SECONDS = 30.0

# OPERATOR DECISION wording (WO-LILY-VOICE-TRUTH-001 PAIR 1), VERBATIM — the
# same text lily_agent._PAIR1_CONFIRMED_VS_GUESSED and prompts/lily_system.txt
# carry (a top-level import of lily_agent would cycle; the prompt pin test
# asserts the three copies are byte-identical).
_PAIR1_CONFIRMED_VS_GUESSED = (
    "If identity is CONFIRMED (voice match, or the player gave their name "
    "this session), greet the returner by name ONCE, one beat, then move on "
    "— 'welcome back, Rami' is allowed here and only here. If identity is "
    "only GUESSED (known device, partial history), do NOT use a name and do "
    "NOT say 'welcome back'; open as a fresh table. Never list prior "
    "players, winners, or newcomers by name off the record. The continuity "
    "rail's 'first welcome-back is owed' applies only to the CONFIRMED case."
)


def _fmt_score(value) -> str:
    return f"{value:.4f}" if isinstance(value, (int, float)) else "-"


def _history_label(h) -> str:
    h = h or {}
    return f"{int(h.get('sessions') or 0)}s/{int(h.get('questions') or 0)}q"


class LilyIdentityMixin:
    # -- WO-LILY-VOICE-TRUTH-001 V3: the carrier registry --------------------
    #
    # WO-2 (RECOG-DELIVERY-001) claimed "the stamp lands on the carrying
    # turn's playout CONFIRM". Auditor B executed the interleavings: the
    # watch was keyed by ORDER, not by speech id — resolve_recognition_carry
    # (confirmed=True) ran on EVERY finished speech, and note_generation_
    # snapshot marked the NEXT generation, preemptive ones included. I1: a
    # greeting still in flight when the fast door armed the watch confirmed
    # first -> "uncarried" -> beat re-armed -> the organic reply carried the
    # welcome-back AND the seam beat aired a second one. I2: a preemptive
    # generation marked inflight, an unrelated deterministic line confirmed
    # -> recognition stamped by a speech that never carried it -> the real
    # reply was cut -> PERMANENT blackout.
    #
    # Now every generation that snapshots WITH the memory block while a
    # recognition lane is owed/armed is registered as a CARRIER under its
    # OWN speech id (llm_node reads the framework's SpeechHandle context
    # var; a deterministic `say` never passes llm_node and can never be a
    # carrier). Only that speech id's CONFIRM stamps; a cut carrier that
    # reached the air re-arms the beat; a carrier that never aired
    # (invalidated preemptive) is dropped silently; a suppression re-arms.

    # -- PAIR 1 mechanical binding: CONFIRMED vs GUESSED from STATE ---------

    def identity_status_line(self) -> str:
        """The operator's PAIR 1 binding: CONFIRMED = the promotion source is
        a voice match, name_stated, or device_plus_name (device + the
        player gave their name this session); GUESSED = a device candidate
        staged/promoted without a stated name, or memory present with
        verified=False. Derived from state, never from model judgment, and
        injected in front of the verbatim rule so the model cannot guess."""
        source = getattr(self, "identity_confirmed_source", None)
        if source in ("voice_identity_match", "voiceprint_match"):
            return (
                "IDENTITY STATUS (from state, not judgment): CONFIRMED — "
                "voice match."
            )
        if source == "name_stated":
            return (
                "IDENTITY STATUS (from state, not judgment): CONFIRMED — the "
                "player gave their name this session."
            )
        if source == "device_plus_name":
            return (
                "IDENTITY STATUS (from state, not judgment): CONFIRMED — "
                "known device and the player gave their name this session."
            )
        return (
            "IDENTITY STATUS (from state, not judgment): GUESSED — no voice "
            "match and no name stated this session (known device / partial "
            "history only)."
        )

    def _carriers(self) -> dict:
        carriers = self._recognition_carriers
        if carriers is None:
            carriers = self._recognition_carriers = {}
        return carriers

    def _dispatched_act_for(self, speech_id) -> str | None:
        acts = getattr(self, "_dispatched_act_by_speech", None) or {}
        return acts.get(speech_id) if speech_id else None

    def _latest_dispatched_speech_for_act(self, act: str) -> str | None:
        acts = getattr(self, "_dispatched_act_by_speech", None) or {}
        for sid in reversed(list(acts)):
            if acts[sid] == act:
                return sid
        return None

    def _rearm_owed_recognition(self, reason: str, event=None) -> None:
        """The welcome-back never reached the air: the beat is OWED again."""
        watch = self._name_door_watch
        if watch is not None and watch.get("source"):
            # The lane that owes it survives the re-arm, so a later carrier
            # (I1: the organic reply after the greeting) is filed under it.
            self._owed_recognition_source = watch.get("source")
        self._name_door_watch = None
        self._late_recognition_flight = None
        self._late_recognition_promotion_owed = True
        self._late_recognition_fired = False
        self._late_recognition_pending = True
        if event is not None:
            event["short_circuit_decision"] = "beat_armed_after_flight"
            event["carried_memory"] = False
        logger.info(
            "LILY_MEMORY | RECOGNITION_CARRY_UNRESOLVED | session=%s "
            "reason=%s — the carrying speech never played out with the "
            "memory block; the beat is re-armed as owed",
            getattr(self.sk, "session_id", "?"), reason,
        )

    def note_recognition_playout_started(self, speech_id) -> None:
        """SEAM (consumed from W1's canonical first-frame hook,
        note_playout_started(speech_id) — wired one line after it in the
        agent_state_changed handler): this speech is ON THE AIR. A carrier
        is marked aired (so a later cut re-arms instead of being dropped
        as an invalidated preemptive); a dispatched late beat binds to its
        speech id here when the dispatch record names it."""
        if not speech_id:
            return
        entry = self._carriers().get(speech_id)
        if entry is not None and entry.get("aired_at") is None:
            entry["aired_at"] = time.monotonic()
        flight = self._late_recognition_flight
        if flight is not None:
            if flight.get("speech_id") is None and (
                self._dispatched_act_for(speech_id) == "late_recognition"
            ):
                flight["speech_id"] = speech_id
            if flight.get("speech_id") == speech_id:
                flight["aired_at"] = time.monotonic()

    def note_recognition_dispatch_suppressed(
        self, act: str | None, speech_id, reason: str | None = None
    ) -> None:
        """SEAM (consumed from W1's on_dispatch_suppressed(act, speech_id,
        reason)): a dispatch died before/without airing (freshness gate,
        flush, hold). A suppressed late beat or carrier is OWED again —
        never stamped, never silently lost."""
        if self._recognition_aired is not None:
            return
        carriers = self._carriers()
        was_carrier = speech_id in carriers if speech_id else False
        if was_carrier:
            carriers.pop(speech_id, None)
        flight = self._late_recognition_flight
        flight_hit = flight is not None and (
            act == "late_recognition"
            or (speech_id and flight.get("speech_id") == speech_id)
        )
        if flight_hit or (was_carrier and not carriers):
            watch = self._name_door_watch
            self._rearm_owed_recognition(
                f"dispatch_suppressed:{reason or 'unknown'}",
                event=(watch or {}).get("event") if watch else None,
            )

    def _recognition_on_dispatch_suppressed(
        self, act, speech_id, reason=None, **_facts
    ) -> None:
        """LISTENER on LilySpeechDeliveryMixin.on_dispatch_suppressed (the
        dispatcher; registered in _init_all_game_state). Integration note:
        first shipped as a same-named method that shadowed the dispatcher
        in the MRO (Identity precedes SpeechDelivery) and raised on W1's
        keyword facts. A consumer is a listener, never the hook itself."""
        self.note_recognition_dispatch_suppressed(act, speech_id, reason)

    def _prune_dead_recognition_flights(self) -> None:
        """Bounded liveness: a carrier/beat/watch that never reached the
        air inside _RECOG_FLIGHT_STALE_SECONDS is dead — re-arm as owed
        rather than hold the seam forever (blackout by wedge)."""
        now = time.monotonic()
        carriers = self._carriers()
        for sid, entry in list(carriers.items()):
            if entry.get("aired_at") is None and (
                now - float(entry.get("at") or now)
            ) > _RECOG_FLIGHT_STALE_SECONDS:
                carriers.pop(sid, None)
                logger.info(
                    "LILY_MEMORY | RECOGNITION_CARRIER_STALE | session=%s "
                    "speech=%s — never reached the air; dropped",
                    getattr(self.sk, "session_id", "?"), sid,
                )
        flight = self._late_recognition_flight
        if flight is not None and flight.get("aired_at") is None and (
            now - float(flight.get("at") or now)
        ) > _RECOG_FLIGHT_STALE_SECONDS and not carriers:
            self._rearm_owed_recognition("late_beat_never_aired")
        watch = self._name_door_watch
        if watch is not None and not carriers and (
            now - float(watch.get("armed_at") or now)
        ) > _RECOG_FLIGHT_STALE_SECONDS:
            self._rearm_owed_recognition(
                "carry_watch_timeout", event=watch.get("event")
            )

    def late_recognition_blocked_reason(self) -> str | None:
        """Return the live beat that makes recognition speech unsafe."""
        # WO-LILY-RECOG-DELIVERY-001 / VOICE-TRUTH-001 V3: a carried-
        # recognition watch, a registered carrier, or a dispatched late
        # beat is in flight — a turn composed WITH the memory block will
        # stamp on ITS playout confirm. The beat holds so two lanes can
        # never stack (the 11:31 double and Auditor B's I1, kept dead);
        # a cut or suppressed flight re-arms the beat.
        self._prune_dead_recognition_flights()
        if self._carriers():
            return "recognition_carry_inflight"
        if self._late_recognition_flight is not None:
            return "recognition_beat_inflight"
        if self._name_door_watch is not None:
            return "recognition_carry_inflight"
        # CLASS 7 (LIVEFIRE-001) 7a: once the round has started, recognition
        # speech is forbidden outright — it belongs to the greeting/intake
        # window, never inside or after game start. The live beat aired as
        # act=game_start and suppressed q_1's kickoff.
        # WO-LILY-RECOG-DELIVERY-001 exemption: a PROMOTION-OWED beat (a
        # slow name-door landing after start — 17:51 — or a cut carrying
        # turn) is deferred to a between-questions seam instead, where it
        # airs ONE compact welcome-back; the forbid stays absolute for
        # every other lane.
        if self._game_start_committed and not (
            self._late_recognition_promotion_owed
        ):
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
        if self._game_start_committed:
            # WO-LILY-RECOG-DELIVERY-001: the promotion-owed beat delivering
            # BETWEEN QUESTIONS (the CLASS 7 exemption) is ONE compact beat
            # — no refresher offer, no prefs question, no what's-new: the
            # game is running and the seam is borrowed, not owned.
            ack = (
                "Recognition landed LATE, mid-game: the [RETURNING TABLE] "
                "block now confirms this is a TABLE you have played with "
                "before. That is a match on the TABLE, not proof of who is "
                "on the mic right now. Between questions, ONE compact "
                "welcome-back beat — own the late catch lightly ('took me a "
                "second'). Name a person ONLY when THEIR voice is matched "
                "present this session (the ROSTER field is the sole naming "
                "authority) or they have stated their name tonight; if no "
                "voice is matched present, name no one — just welcome the "
                "table back. "
                + self.identity_status_line() + " "
                + _PAIR1_CONFIRMED_VS_GUESSED +
                " Do NOT re-introduce yourself, do NOT repeat "
                "any line of your opener, do NOT offer a refresher or ask "
                "about preferences, and do NOT ask a question of your own — "
                "one warm beat, then hand straight back to the game."
            )
            present = ",".join(
                list(getattr(self.sk, "players", []) or [])
            ) or "-"
            logger.info(
                "LILY_MEMORY | LATE_RECOGNITION | session=%s group=%s "
                "present=%s (compact, between questions — promotion-owed)",
                self.sk.session_id, getattr(self, "group_id", None), present,
            )
            dispatched = self.gated_say(
                None, "late_recognition", ack, source="late_recognition"
            )
            if not dispatched:
                self._late_recognition_fired = False
                self._late_recognition_pending = True
            else:
                self._arm_late_recognition_flight()
            return dispatched
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
            "table back. "
            + self.identity_status_line() + " "
            + _PAIR1_CONFIRMED_VS_GUESSED +
            " Never pretend you knew all along, and never "
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
            self._arm_late_recognition_flight()
        return dispatched

    def _arm_late_recognition_flight(self) -> None:
        """VOICE-TRUTH-001 V3 (Auditor D P1-2): the late beat used to
        stamp recognition_aired AT DISPATCH, keyless — a beat the
        freshness gate or a flush then suppressed had already retired every
        other lane: blackout. The dispatch now arms a FLIGHT; the fact
        stamps only when the beat's own generation (a carrier under its
        speech id) CONFIRMS; a suppressed/cut/never-aired beat re-arms as
        owed (note_recognition_dispatch_suppressed / resolve_recognition_
        carry / the stale prune)."""
        self._late_recognition_flight = {
            "source": "late_recognition_beat",
            "speech_id": self._latest_dispatched_speech_for_act(
                "late_recognition"
            ),
            "at": time.monotonic(),
            "aired_at": None,
        }
        logger.info(
            "LILY_MEMORY | LATE_RECOGNITION_DISPATCHED | session=%s "
            "speech=%s — stamped on ITS playout confirm, never at dispatch",
            getattr(self.sk, "session_id", "?"),
            self._late_recognition_flight.get("speech_id"),
        )

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
        # WO-LILY-RECOG-DELIVERY-001: the owed marker and any pending
        # confirm watch are settled by the airing — recognition is paid.
        self._late_recognition_promotion_owed = False
        self._name_door_watch = None
        self._late_recognition_flight = None
        self._recognition_carriers = {}
        logger.info(
            "LILY_MEMORY | RECOGNITION_AIRED | session=%s source=%s — "
            "recognition is on air; every other recognition lane is retired "
            "for the session (ANTIREPEAT-PROTOCOL-001)",
            getattr(self.sk, "session_id", "?"), source,
        )

    def recognition_aired(self) -> dict | None:
        """The recognition-aired record ({source, text, at}), or None."""
        return self._recognition_aired

    # -- WO-LILY-RECOG-DELIVERY-001: un-lie the name-door stamp --------------
    #
    # bfadc42 stamped note_recognition_aired("name_door_organic") at both
    # promotion tails UNCONDITIONALLY — "the organic turn carries the memory
    # by construction". Live 2026-08-14 17:51 (lily-FD3994-358c0ac8) proved
    # the construction false: the name-door promotion is a fire-and-forget
    # task whose sequential Supabase awaits completed ~80s AFTER the organic
    # reply to "this is Rami" aired memory-blind (lily_llm_usage: the reply's
    # generation ran at 13.4k prompt tokens at 21:51:25; the ~437-token
    # [RETURNING TABLE] block first appears at 21:52:51). The stamp was a
    # receipt that lied (S2), and maybe_fire_late_recognition hard-returns on
    # it — every stated-name returner on a cold device got PERMANENT zero
    # recognition.
    #
    # The mechanical truth signal: _apply_context_blocks injects the memory
    # block into every per-generation context copy, so "did the organic turn
    # carry the memory" == "was memory_block set before that turn's context
    # snapshot". note_generation_snapshot (called from the include_volatile
    # injection in llm_node's path — one call per real generation) is the
    # per-turn marker; the door stamps the counter at entry, and the tail
    # compares:
    #   * no generation snapshot since door entry  -> the organic reply's
    #     snapshot is still AHEAD and will include the just-set memory_block
    #     -> CARRIED. Do not stamp yet: arm a confirm watch and stamp on
    #     that turn's speech-finished CONFIRM (the greet-leg discipline) —
    #     a cut turn re-arms the beat instead of burning recognition.
    #   * a generation snapshot already happened since door entry (17:51)
    #     -> the reply aired memory-blind -> UNCARRIED. Leave the beat ARMED
    #     (owed) so maybe_fire_late_recognition delivers at the next seam —
    #     including BETWEEN QUESTIONS after game start (the promotion-owed
    #     exemption from the CLASS 7 forbid).

    def note_generation_snapshot(self, speech_id=None) -> int:
        """The per-turn context marker: one call per real generation, at the
        moment the per-generation context copy is finalized (llm_node's
        include_volatile _apply_context_blocks). Returns the new sequence.

        VOICE-TRUTH-001 V3: `speech_id` is the framework SpeechHandle this
        generation belongs to (llm_node reads _SpeechHandleContextVar). If
        a recognition lane is owed/armed and the memory block is in THIS
        snapshot, the generation is registered as a CARRIER under its own
        id — only that id's playout confirm stamps. A snapshot with no id
        cannot be keyed and is never a carrier (honest: no receipt without
        an identity)."""
        self._ctx_snapshot_seq += 1
        seq = self._ctx_snapshot_seq
        if self._recognition_aired is not None or not self.memory_block:
            return seq
        watch = self._name_door_watch
        flight = self._late_recognition_flight
        lane_open = (
            watch is not None
            or flight is not None
            or self._late_recognition_pending
        )
        if not lane_open:
            return seq
        if not speech_id:
            logger.info(
                "LILY_MEMORY | RECOGNITION_CARRY_UNKEYED | session=%s seq=%d "
                "— a generation snapshotted WITH the memory block but no "
                "speech id reached the marker; it cannot be a carrier",
                getattr(self.sk, "session_id", "?"), seq,
            )
            return seq
        act = self._dispatched_act_for(speech_id)
        if act == "late_recognition":
            source = "late_recognition_beat"
        elif act == "game_start":
            source = "game_start_ride_along"
        elif watch is not None:
            source = watch.get("source") or "name_door_organic"
        else:
            source = self._owed_recognition_source or "memory_turn_organic"
        carriers = self._carriers()
        if speech_id not in carriers:
            carriers[speech_id] = {
                "source": source,
                "seq": seq,
                "at": time.monotonic(),
                "aired_at": None,
            }
            if watch is not None and watch.get("inflight_seq") is None:
                watch["inflight_seq"] = seq
            if flight is not None and flight.get("speech_id") is None and (
                act == "late_recognition"
            ):
                flight["speech_id"] = speech_id
            logger.info(
                "LILY_MEMORY | RECOGNITION_CARRY_INFLIGHT | session=%s "
                "source=%s seq=%d speech=%s — a generation snapshotted WITH "
                "the memory block; ITS playout confirm stamps "
                "recognition_aired",
                getattr(self.sk, "session_id", "?"), source, seq, speech_id,
            )
        return seq

    def _record_identity_promotion(
        self, source: str, group_id, *, decision: str, carried_memory,
        **extra,
    ) -> dict:
        """S1/S16 telemetry: identity-promotion events persist in
        lily_sessions.metadata.identity_promotions (both existing metadata
        write sites — session end + the 60s heartbeat), so promotion timing
        and the short-circuit decision are a SQL query, never a
        reconstruction from prompt-token deltas. Idempotent per
        (source, group_id): the double promotion tail (_promote awaits
        upgrade_group_id first) records once.

        VOICE-TRUTH-001 V4: name-door promotions also carry ts_start (door
        entry), ts_resolved (memory block visible) and door_ms — the door
        latency is a SQL query."""
        events = self._identity_promotion_events
        if events is None:
            events = self._identity_promotion_events = []
        for ev in events:
            if ev.get("source") == source and ev.get("group_id") == group_id:
                return ev
        ev = {
            "source": source,
            "group_id": group_id,
            "ts": round(time.time(), 3),
            "short_circuit_decision": decision,
            "carried_memory": carried_memory,
        }
        opened = self._name_door_opened_at
        if source in _NAME_DOOR_TRIGGERS and opened is not None:
            ev["ts_start"] = round(float(opened), 3)
            ev["ts_resolved"] = ev["ts"]
            ev["door_ms"] = round((ev["ts"] - float(opened)) * 1000, 1)
        ev.update(extra)
        events.append(ev)
        logger.info(
            "LILY_MEMORY | IDENTITY_PROMOTION | session=%s source=%s "
            "group=%s decision=%s carried_memory=%s",
            getattr(self.sk, "session_id", "?"), source, group_id,
            decision, carried_memory,
        )
        return ev

    def _name_door_promotion_tail(self, source: str, group_id) -> None:
        """The name-door promotion tail decision (replaces bfadc42's
        unconditional stamp). Runs at BOTH tails; the first decides."""
        if self._recognition_aired is not None:
            return  # recognition already on air — nothing owed
        if self._name_door_watch is not None:
            return  # the upgrade tail (awaited first) already decided
        if self._late_recognition_promotion_owed and (
            self._late_recognition_pending
        ):
            return  # already decided: uncarried, beat armed
        entry = self._name_door_entry_seq
        carried = entry is None or self._ctx_snapshot_seq <= entry
        event = self._record_identity_promotion(
            source, group_id,
            decision="carried_pending_confirm" if carried else "beat_armed",
            carried_memory=carried,
        )
        if carried:
            # The organic reply's context snapshot is still ahead — it WILL
            # pick up the just-set memory block. Not stamped here: the stamp
            # lands on that turn's speech-finished CONFIRM
            # (resolve_recognition_carry), the greet-leg discipline. While
            # the watch is armed the late beat holds (blocked reason), so
            # the 11:31 double stays dead in both directions.
            self._name_door_watch = {
                "source": "name_door_organic",
                "group_id": group_id,
                "armed_at_seq": self._ctx_snapshot_seq,
                "armed_at": time.monotonic(),
                "inflight_seq": None,
                "event": event,
            }
            self._late_recognition_pending = False
            logger.info(
                "LILY_MEMORY | LATE_RECOGNITION_SHORT_CIRCUIT | session=%s "
                "trigger=%s group=%s — the organic turn carries the "
                "recognition; stamped on its playout CONFIRM, not here",
                getattr(self.sk, "session_id", "?"), source, group_id,
            )
        else:
            # THE 17:51 CASE: a generation already snapshotted since the
            # door opened — the organic reply aired memory-blind. The
            # welcome-back is OWED: arm the beat and mark it
            # promotion-carried so the CLASS 7 game-start forbid defers it
            # to a between-questions seam instead of retiring it.
            self._late_recognition_promotion_owed = True
            self._late_recognition_fired = False
            self._late_recognition_pending = True
            logger.warning(
                "LILY_MEMORY | NAME_DOOR_UNCARRIED | session=%s trigger=%s "
                "group=%s entry_seq=%s now_seq=%d — the organic reply aired "
                "memory-blind (slow promotion); the welcome-back beat is "
                "ARMED, never stamped as aired",
                getattr(self.sk, "session_id", "?"), source, group_id,
                entry, self._ctx_snapshot_seq,
            )
            self.maybe_fire_late_recognition()

    def resolve_recognition_carry(
        self, *, confirmed: bool, speech_id=None, suppressed: bool = False
    ) -> None:
        """A speech playout ended — resolve recognition carry BY SPEECH ID.
        Called from on_agent_speech_finished on BOTH exits (confirm and
        interrupted/suppressed), with the finishing speech's id.

        VOICE-TRUTH-001 V3 (replaces WO-2's order-keyed resolution, whose
        "known approximation" Auditor B executed into a double (I1) and a
        permanent blackout (I2)):
          * the finishing speech IS a registered carrier (its own context
            snapshot held the memory block):
              - CONFIRMED -> stamp recognition_aired under the carrier's
                source; every other lane retires;
              - cut after reaching the air, or suppressed -> re-arm OWED
                (unless another carrier is still in flight);
              - cut WITHOUT ever airing and not suppressed -> an
                invalidated preemptive generation; dropped silently, the
                real reply follows and registers itself.
            Older carriers that never aired are pruned with it.
          * the finishing speech is NOT a carrier: it says nothing about
            recognition while a carrier is in flight; a dispatched late
            beat that died without a carrier snapshot re-arms; a
            memory-blind turn finishing under an armed watch with no
            carrier registered is the 17:51 shape (uncarried) -> re-arm."""
        if self._recognition_aired is not None:
            self._name_door_watch = None
            self._late_recognition_flight = None
            self._recognition_carriers = {}
            return
        carriers = self._carriers()
        watch = self._name_door_watch
        flight = self._late_recognition_flight
        event = watch.get("event") if watch else None
        act = self._dispatched_act_for(speech_id)
        if speech_id and speech_id in carriers:
            entry = carriers.pop(speech_id)
            for sid, other in list(carriers.items()):
                if other.get("seq", 0) < entry.get("seq", 0) and (
                    other.get("aired_at") is None
                ):
                    carriers.pop(sid, None)  # an older never-aired generation
            if confirmed:
                self._name_door_watch = None
                self._late_recognition_flight = None
                if event is not None:
                    event["short_circuit_decision"] = "organic_confirmed"
                    event["carried_memory"] = True
                    event["confirmed_at"] = round(time.time(), 3)
                    event["carrier_speech_id"] = speech_id
                self.note_recognition_aired(entry.get("source") or "memory_turn_organic")
                return
            if entry.get("aired_at") is None and not suppressed:
                logger.info(
                    "LILY_MEMORY | RECOGNITION_CARRY_INVALIDATED | session=%s "
                    "speech=%s — a carrier generation was cancelled before "
                    "airing (invalidated preemptive); the real reply follows",
                    getattr(self.sk, "session_id", "?"), speech_id,
                )
                return
            if carriers:
                return  # another carrier still in flight decides
            self._rearm_owed_recognition(
                "carrier_suppressed" if suppressed else "carrier_cut",
                event=event,
            )
            return
        if flight is not None and (
            (speech_id and flight.get("speech_id") == speech_id)
            or act == "late_recognition"
        ):
            if confirmed:
                # The beat played out but never snapshotted with the block
                # (memory cleared mid-flight) — it still aired as the beat.
                self._late_recognition_flight = None
                self.note_recognition_aired("late_recognition_beat")
                return
            self._rearm_owed_recognition(
                "late_beat_suppressed" if suppressed else "late_beat_cut"
            )
            return
        if carriers:
            return  # a carrier is still in flight; this speech is unrelated
        if watch is not None:
            # No carrier ever registered: the turn the watch expected to
            # carry aired memory-BLIND (a preemptive that snapshotted before
            # the promotion) — uncarried, the beat is owed.
            self._rearm_owed_recognition(
                "memory_blind_turn_finished", event=event
            )

    def note_game_start_carries_recognition(self) -> None:
        """The game-start composite composed the one welcome-back ride-along
        beat (start_game's memory branch) and its dispatch was accepted.
        Same confirm discipline as the organic lane: the kickoff turn's
        snapshot carries the memory block, so its playout CONFIRM stamps —
        a cut kickoff re-arms the beat instead of losing recognition."""
        if self._recognition_aired is not None:
            return
        if self._name_door_watch is not None:
            return  # an organic carry is already pending confirm
        self._late_recognition_pending = False
        self._late_recognition_fired = False
        self._name_door_watch = {
            "source": "game_start_ride_along",
            "group_id": self.group_id,
            "armed_at_seq": self._ctx_snapshot_seq,
            "armed_at": time.monotonic(),
            "inflight_seq": None,
            "event": None,
        }
        logger.info(
            "LILY_MEMORY | GAME_START_CARRIES_RECOGNITION | session=%s — "
            "the kickoff composite carries the welcome-back; stamped on its "
            "playout CONFIRM",
            getattr(self.sk, "session_id", "?"),
        )

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
        # VOICE-TRUTH-001 V5 provenance: the block states HOW the table was
        # recognized; the staging source is a placeholder until promotion
        # rebuilds it under the real trigger (voice / device+name / name).
        block = lily_memory.lily_build_memory_block(
            memory, prefs=prefs, recognized_by=source
        )
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
        staged_prefs = dict(self._device_candidate_prefs)
        # VOICE-TRUTH-001 V5 provenance: rebuild the block under the REAL
        # promotion trigger so the [RETURNING TABLE] block states the true
        # door (voice / device+name / stated name) — never "voice
        # recognition matched" for a name-door promotion (S2).
        block = lily_memory.lily_build_memory_block(
            memory, prefs=staged_prefs, recognized_by=trigger
        ) or self._device_candidate_memory_block
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
        label = trigger if trigger in _KNOWN_GROUP_SOURCES else "voiceprint_match"
        # VOICE-TRUTH-001 V4: MEMORY FIRST. The staged block is already in
        # hand — inject it NOW, before the rekey/reload awaits (the 17:51
        # door's ~80s of sequential Supabase round-trips used to sit
        # between "group resolved" and "block visible to the next
        # generation"). The carried/uncarried tail is decided at THIS
        # moment, the moment the block became visible.
        merged_prefs = staged_prefs
        merged_prefs.update(self.prefs or {})
        self.prefs = merged_prefs
        self.memory_block = block
        self.memory_total_games = int(memory.get("total_games") or 0)
        self.memory_player_names = list(memory.get("player_names") or [])
        self.identity_confirmed_source = (
            trigger if trigger in _CONFIRMED_IDENTITY_SOURCES else None
        )
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
        name_door = trigger in _NAME_DOOR_TRIGGERS and bool(self.memory_block)
        if name_door:
            # Name-door tail (WO-LILY-RECOG-DELIVERY-001, un-lying
            # ANTIREPEAT-PROTOCOL-001's stamp): the organic reply answering
            # the name utterance carries the just-promoted memory block ONLY
            # when its context snapshot came after the promotion — a slow
            # promotion (17:51, ~80s of Supabase awaits) aired it
            # memory-blind. The tail decides mechanically: carried →
            # stamp on that turn's playout CONFIRM; uncarried → the beat
            # stays ARMED and delivers at the next seam. The prefs offer
            # still needs no beat: the memory block's "usual:" line plus the
            # system prompt's standing instruction carry it.
            self._name_door_promotion_tail(trigger, candidate)
        # Rekey + reloads run AFTER the block is visible (concurrently
        # inside upgrade_group_id); its own tail call no-ops.
        await self.upgrade_group_id(candidate, label)
        if not name_door:
            # Task 1 (RECOGNITION-VARIETY): a voiceprint verification landing
            # after the greeting is the same late-recognition moment as a
            # name-hash upgrade — same acknowledgment beat, same one-shot.
            self._record_identity_promotion(
                trigger, candidate,
                decision="late_beat_path", carried_memory=None,
            )
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

    # -- WO-LILY-VOICE-TRUTH-001 V1: the speech-gated probe lifecycle -------
    #
    # The probe object (lily_voice_embedder.LilyVoiceProbe) is attached by
    # the track frame sink; the transcript handler feeds it human STT
    # segments (rule (a), primary) and the sink feeds it the VAD flag
    # (fallback). Every voiced increment re-evaluates the match schedule
    # (rules (b)/(c)); the window (c) closes the biometric question; the
    # receipt (f) rides lily_sessions.metadata.voice_identity at both write
    # sites; enrollment (d)/(e) reads the voiced UNION only.

    def attach_voice_probe(self, probe) -> None:
        """The frame sink hands over its probe at fork start."""
        self._voice_probe = probe
        self._voice_identity_window_started_at = time.monotonic()
        self._voice_identity_gate_source = getattr(probe, "gate_source", None)

    def note_voiced_segment(self, start, end, speaker_label=None) -> float:
        """SEAM (one line in the transcript handler): a human (non-LILY)
        STT final [start, end] landed — rule (a), the primary voiced
        signal. Returns voiced seconds added."""
        if speaker_label == "LILY":
            return 0.0
        probe = self._voice_probe
        if probe is None:
            return 0.0
        added = probe.note_voiced_segment(start, end)
        self._sync_voice_probe()
        if added > 0:
            self.maybe_start_voice_identity_match()
        return added

    def note_voice_probe_vad(self, speaking: bool) -> float:
        """Per-frame from the sink: the framework VAD user_speaking flag
        (rule (a) fallback). Also the tick that closes the window (c)
        when the room has gone quiet."""
        probe = self._voice_probe
        if probe is None:
            return 0.0
        added = probe.note_vad_state(speaking)
        if added > 0:
            self._sync_voice_probe()
            self.maybe_start_voice_identity_match()
        elif (
            not self._voice_identity_resolved
            and not self._voice_identity_inflight
            and self.voice_probe_window_elapsed()
        ):
            self._sync_voice_probe()
            self._voice_identity_close_window_nowait("window_elapsed")
        return added

    def _sync_voice_probe(self) -> None:
        probe = self._voice_probe
        if probe is None:
            return
        self._voice_identity_voiced_seconds = float(probe.voiced_seconds)
        self._voice_identity_gate_source = probe.gate_source
        # A truthy marker only: the match/enroll PCM is built lazily off
        # the event loop (see _voice_identity_audio_probe).
        self._voice_identity_pcm = True if probe.ready() else None

    def voice_probe_window_elapsed(self) -> bool:
        started = self._voice_identity_window_started_at
        if started is None:
            return False
        return (
            time.monotonic() - started
        ) >= lily_config.voice_probe_window_seconds()

    def voice_probe_sink_should_close(self) -> bool:
        """The sink may stop resampling/holding frames once the biometric
        question is closed AND the enrollment union is full (d)."""
        probe = self._voice_probe
        if probe is None:
            return True
        matching_done = (
            self._voice_identity_matched
            or self._voice_identity_resolved
            or self.voice_probe_window_elapsed()
        )
        return matching_done and (
            probe.union_seconds >= lily_config.voice_enroll_max_seconds()
        )

    def _voice_identity_audio_probe(self):
        """Captured VOICED PCM for a match, or None when unavailable. With a
        live probe attached this returns a zero-arg builder (the float list
        is built inside the embedder thread, off the event loop); tests
        inject `_voice_identity_pcm` directly."""
        probe = self._voice_probe
        if probe is not None:
            return probe.match_pcm if probe.ready() else None
        return self._voice_identity_pcm

    def _voice_identity_enroll_probe(self):
        """The voiced UNION for enrollment (rule (d)) — never the first 8s."""
        probe = self._voice_probe
        if probe is not None:
            return probe.enroll_pcm if probe.ready() else None
        enroll = self._voice_identity_enroll_pcm
        return enroll if enroll is not None else self._voice_identity_pcm

    def maybe_start_voice_identity_match(self) -> bool:
        """Schedule a match attempt when the gate says one is due.

        Rules (b)/(c): the first attempt needs min_voiced seconds of VOICED
        audio; each further attempt needs retry_voiced more; attempts stop
        at a match or when the probe window elapses. Never one-shot: the
        old `_voice_identity_attempted` latch fired ONE match at 2.5s of
        wall-clock frames (room tone) and never tried again. Returns True
        when an attempt was scheduled."""
        # Warming is what makes _voice_identity_ready() cheap: the load runs
        # in a thread while this call returns immediately. The trigger is
        # retryable by design, so a not-yet-warm model simply means "next
        # voiced chunk".
        self._warm_voice_embedder()
        if (
            self._voice_identity_matched
            or self._voice_identity_resolved
            or self._voice_identity_inflight
            or not self._voice_identity_ready()
        ):
            return False
        probe = self._voice_probe
        min_voiced = lily_config.voice_min_voiced_seconds()
        retry_voiced = lily_config.voice_retry_voiced_seconds()
        if probe is not None:
            if self.voice_probe_window_elapsed():
                self._voice_identity_close_window_nowait("window_elapsed")
                return False
            if not probe.ready() or not probe.match_due():
                return False
            probe.mark_attempt()
        else:
            if self._voice_identity_pcm is None:
                return False
            voiced = float(self._voice_identity_voiced_seconds or 0.0)
            if voiced < min_voiced:
                return False
            last = self._voice_identity_last_attempt_voiced
            if self._voice_identity_attempts > 0 and last is not None and (
                voiced - last
            ) < retry_voiced:
                return False
            self._voice_identity_last_attempt_voiced = voiced
        self._voice_identity_attempts += 1
        self._voice_identity_attempted = True
        self._voice_identity_inflight = True
        if self._voice_identity_match_t0 is None:
            self._voice_identity_match_t0 = time.monotonic()
        asyncio.ensure_future(self._voice_identity_match_at_start())
        return True

    def _voice_identity_close_window_nowait(self, reason: str) -> None:
        asyncio.ensure_future(self._voice_identity_close_window(reason))

    async def _voice_identity_close_window(self, reason: str) -> None:
        """The biometric question CLOSES (rule (c)): the window elapsed, the
        session ended, or — with no live probe to bring new voiced audio —
        the one attempt reported. Sets the FINAL outcome honestly
        (no_match / insufficient_voiced / embedder_unavailable), stamps the
        Z3 hold, and re-invokes the deferred name-set proposal exactly as
        the old single no-match branch did."""
        if self._voice_identity_resolved or self._voice_identity_matched:
            return
        self._voice_identity_resolved = True
        voiced = float(self._voice_identity_voiced_seconds or 0.0)
        min_voiced = lily_config.voice_min_voiced_seconds()
        if self._voice_identity_attempts == 0:
            if voiced < min_voiced:
                outcome = "insufficient_voiced"
            elif not lily_voice_embedder.lily_voice_embedder_loaded():
                outcome = "embedder_unavailable"
            else:
                outcome = "no_attempt"
        else:
            outcome = self._voice_id_outcome or "no_match"
            if not str(outcome).startswith(("match:", "failed:")):
                outcome = "no_match"
        self._voice_id_outcome = outcome
        # Z3: no-match / never-ran is not resolution while the name door is
        # untried — hold memory-characterising speech.
        if self._voice_identity_no_match_at is None:
            self._voice_identity_no_match_at = time.time()
        logger.info(
            "LILY_VOICE_ID | PROBE_RESOLVED | session=%s outcome=%s reason=%s "
            "voiced=%.1fs min_voiced=%.1fs attempts=%d best=%s runner_up=%s "
            "threshold=%.2f gate=%s",
            self.sk.session_id, outcome, reason, voiced, min_voiced,
            self._voice_identity_attempts,
            _fmt_score(self._voice_identity_best_score),
            _fmt_score(self._voice_identity_runner_up),
            lily_config.voice_identity_match_threshold(),
            self._voice_identity_gate_source,
        )
        # V7/V1c resolve-before-propose: the enrolled-voice route has now
        # REPORTED. If the roster is already stable (game started) on a weak
        # group, resolve_group_identity may have DEFERRED the name-set
        # proposal waiting on exactly this answer — re-invoke it so the
        # name-set hash is quarantined now (never ahead of the biometric,
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

    async def _voice_identity_match_at_start(self) -> bool:
        """ONE match attempt on the voiced probe: embed, rank against the
        centroid pool, decide against threshold + margin, and LOG THE
        DECISION WITH ITS NUMBERS (rule (f): best, runner-up, threshold,
        voiced seconds, gate source). On a confident match, stage+promote
        that group's memory through the existing candidate path (the
        biometric match IS the proof). A no-match leaves the window OPEN
        for a retry on more voiced audio (rule (c)); with no live probe the
        attempt is final. Returns True on a promotion."""
        if not self._voice_identity_inflight:
            # Called directly (tests / a caller bypassing the scheduler):
            # it is still an attempt and the receipt counts it.
            self._voice_identity_inflight = True
            self._voice_identity_attempts += 1
            self._voice_identity_attempted = True
        try:
            if not self._voice_identity_ready() or getattr(
                self, "device_identity_verified", False
            ):
                # Not going to run at all — nothing is outstanding.
                self._voice_identity_resolved = True
                return False
            probe = self._voice_identity_audio_probe()
            if probe is None:
                return False
            attempt = self._voice_identity_attempts
            emb = await lily_voice_embedder.lily_extract_embedding_async(probe)
            if emb is None:
                logger.warning(
                    "LILY_VOICE_ID | EMBED_NONE | session=%s attempt=%d — "
                    "the embedder returned nothing; retry on more voiced "
                    "audio", self.sk.session_id, attempt,
                )
                return False
            # V2 instrumentation: t1 = embedding produced. t0 was stamped at
            # the first scheduled attempt, so embed_ms spans utterance-ready
            # -> embedding and folds in any wait on a still-warming model.
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
            threshold = lily_config.voice_identity_match_threshold()
            margin = lily_config.voice_identity_match_margin()
            ranked = lily_voice_identity.lily_rank_voice(emb, identities)
            best_score = ranked[0][0] if ranked else None
            best_gid = ranked[0][1] if ranked else None
            runner_up = ranked[1][0] if len(ranked) > 1 else None
            match = lily_voice_identity.lily_match_voice(
                emb, identities, threshold=threshold, margin=margin,
            )
            # The receipt keeps the BEST attempt's numbers (S2: the number
            # rides every outcome, match or not).
            if best_score is not None and (
                self._voice_identity_best_score is None
                or best_score > self._voice_identity_best_score
            ):
                self._voice_identity_best_score = round(best_score, 4)
                self._voice_identity_best_group = best_gid
                self._voice_identity_runner_up = (
                    round(runner_up, 4) if runner_up is not None else None
                )
            if match is None:
                decision = (
                    "no_candidates" if best_score is None
                    else "below_threshold" if best_score < threshold
                    else "ambiguous_margin"
                )
            elif match["group_id"] == self.group_id:
                decision = "match_is_current_group"
            else:
                decision = "match"
            voiced = float(self._voice_identity_voiced_seconds or 0.0)
            logger.info(
                "LILY_VOICE_ID | THRESHOLD_DECISION | session=%s attempt=%d "
                "decision=%s best=%s best_group=%s runner_up=%s threshold=%.2f "
                "margin=%.3f voiced=%.1fs gate=%s pool=%d tag=%s",
                self.sk.session_id, attempt, decision,
                _fmt_score(best_score), str(best_gid)[:16] if best_gid else "-",
                _fmt_score(runner_up), threshold, margin, voiced,
                self._voice_identity_gate_source, len(identities),
                lily_config.voice_identity_model_tag(),
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
            if match is None or match["group_id"] == self.group_id:
                self._voice_id_outcome = "no_match"
                if self._voice_identity_no_match_at is None:
                    self._voice_identity_no_match_at = time.time()
                # Rule (c): NOT resolved while a live probe can still bring
                # new voiced audio inside the window. Without a live probe
                # (injected PCM), this attempt is the only one there is.
                if self._voice_probe is None or self.voice_probe_window_elapsed():
                    self._voice_identity_inflight = False
                    await self._voice_identity_close_window(
                        "attempt_final" if self._voice_probe is None
                        else "window_elapsed"
                    )
                return False
            self._voice_identity_matched = True
            self._voice_identity_resolved = True
            if self._voice_probe is not None:
                self._voice_probe.mark_matched()
            # Recognition-latency closure (lily-639007: 2.5 min to know a
            # player whose centroid was 2.5h fresh — and nothing persisted
            # said whether the match MISSED or never RAN). The outcome now
            # rides the session report; no log export needed to tell.
            self._voice_id_outcome = (
                f"match:{str(match['group_id'])[:16]}:{match['score']:.3f}"
            )
            logger.info(
                "LILY_VOICE_ID | MATCH_AT_START | session=%s group=%s score=%.3f "
                "attempt=%d voiced=%.1fs",
                self.sk.session_id, match["group_id"], match["score"],
                attempt, voiced,
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
            self.identity_confirmed_source = "voice_identity_match"
            self._schedule_fragment_merge(match["group_id"], emb, identities)
            return True
        except Exception as e:
            self._voice_identity_resolved = True
            self._voice_id_outcome = f"failed:{type(e).__name__}"
            # Z3: a failed probe gave no answer either — same hold shape.
            self._voice_identity_no_match_at = time.time()
            logger.warning("LILY_VOICE_ID | MATCH_AT_START_FAILED | %s", e)
            return False
        finally:
            self._voice_identity_inflight = False

    def _voice_identity_finalize(self) -> None:
        """Session close: the window closes now if it is still open (the
        final outcome is written before the receipt)."""
        if self._voice_identity_resolved or self._voice_identity_matched:
            return
        self._sync_voice_probe()
        # Synchronous close: the re-resolve arm is meaningless at close.
        self._voice_identity_resolved = True
        voiced = float(self._voice_identity_voiced_seconds or 0.0)
        if self._voice_identity_attempts == 0:
            if voiced < lily_config.voice_min_voiced_seconds():
                outcome = "insufficient_voiced"
            elif not lily_config.voice_identity_enabled() or (
                getattr(self, "supabase", None) is None
            ):
                outcome = "disabled"
            elif not lily_voice_embedder.lily_voice_embedder_loaded():
                outcome = "embedder_unavailable"
            else:
                outcome = "no_attempt"
        else:
            outcome = self._voice_id_outcome or "no_match"
            if not str(outcome).startswith(("match:", "failed:")):
                outcome = "no_match"
        self._voice_id_outcome = outcome
        logger.info(
            "LILY_VOICE_ID | PROBE_RESOLVED | session=%s outcome=%s "
            "reason=session_close voiced=%.1fs attempts=%d best=%s",
            self.sk.session_id, outcome, voiced,
            self._voice_identity_attempts,
            _fmt_score(self._voice_identity_best_score),
        )

    def voice_identity_receipt(self) -> dict:
        """Rule (f): the receipt that rides lily_sessions.metadata.
        voice_identity at BOTH write sites (close + 60s heartbeat). Every
        outcome carries the numbers; `not_attempted`-class values are
        first-class (S2)."""
        self._sync_voice_probe()
        voiced = float(self._voice_identity_voiced_seconds or 0.0)
        attempts = int(self._voice_identity_attempts or 0)
        outcome = self._voice_id_outcome
        if not outcome:
            if attempts > 0:
                outcome = "attempted_no_outcome"
            elif not lily_config.voice_identity_enabled() or (
                getattr(self, "supabase", None) is None
            ):
                outcome = "disabled"
            elif self._voice_probe is None and self._voice_identity_pcm is None:
                outcome = "never_ran"
            elif voiced < lily_config.voice_min_voiced_seconds():
                outcome = "insufficient_voiced"
            else:
                outcome = "pending"
        probe = self._voice_probe
        receipt = {
            "outcome": outcome,
            "voiced_seconds": round(voiced, 3),
            "attempts": attempts,
            "best_score": self._voice_identity_best_score,
            "best_group": (
                str(self._voice_identity_best_group)[:16]
                if self._voice_identity_best_group else None
            ),
            "runner_up": self._voice_identity_runner_up,
            "threshold": lily_config.voice_identity_match_threshold(),
            "margin": lily_config.voice_identity_match_margin(),
            "model_tag": lily_config.voice_identity_model_tag(),
            "gate_source": self._voice_identity_gate_source,
            "min_voiced_seconds": lily_config.voice_min_voiced_seconds(),
            "window_seconds": lily_config.voice_probe_window_seconds(),
            "embed_ms": getattr(self, "_voice_id_embed_ms", None),
            "resolve_ms": getattr(self, "_voice_id_resolve_ms", None),
            "enrollment": self._voice_identity_enrollment,
        }
        if probe is not None:
            receipt["probe"] = probe.receipt()
        return receipt

    async def _voice_identity_enroll_at_close(self) -> bool:
        """Fold this session's captured VOICED speech into the group's stored
        centroid so the next session (any device) recognizes it. Runs at
        close, off the vocal path; skipped when identity persistence is
        disallowed (forget).

        VOICE-TRUTH-001 rules (d)/(e): the sample is the UNION of the
        session's voiced chunks (bounded), never the first 8s of wall-clock
        audio; a session under min_voiced seconds enrolls NOTHING and the
        receipt says so ("skipped_insufficient_voiced"); the enrollment
        quality floor (enroll_min_speech_seconds) still applies above it.

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
        self._sync_voice_probe()
        voiced = float(self._voice_identity_voiced_seconds or 0.0)
        min_voiced = lily_config.voice_min_voiced_seconds()
        gate = self._voice_identity_gate_source
        if voiced < min_voiced:
            self._voice_identity_enrollment = {
                "status": "skipped_insufficient_voiced",
                "voiced_seconds": round(voiced, 3),
                "min_voiced_seconds": min_voiced,
                "gate_source": gate,
            }
            logger.info(
                "LILY_VOICE_ID | ENROLL_SKIPPED_INSUFFICIENT_VOICED | "
                "session=%s voiced=%.1fs min=%.1fs — nothing is ever enrolled "
                "under the minimum", self.sk.session_id, voiced, min_voiced,
            )
            return False
        enroll_floor = lily_config.voice_identity_enroll_min_speech_seconds()
        if voiced < enroll_floor:
            self._voice_identity_enrollment = {
                "status": "skipped_below_enroll_floor",
                "voiced_seconds": round(voiced, 3),
                "enroll_min_seconds": enroll_floor,
                "gate_source": gate,
            }
            logger.info(
                "LILY_VOICE_ID | ENROLL_SKIPPED_SHORT | session=%s voiced=%.1fs "
                "floor=%.1fs", self.sk.session_id, voiced, enroll_floor,
            )
            return False
        probe = self._voice_identity_enroll_probe()
        if probe is None:
            return False
        try:
            emb = await lily_voice_embedder.lily_extract_embedding_async(probe)
            if emb is None:
                self._voice_identity_enrollment = {
                    "status": "failed_embedding", "voiced_seconds": round(voiced, 3),
                    "gate_source": gate,
                }
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
            redirect_score = None
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
                    redirect_score = round(match["score"], 4)
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
                    self._voice_identity_enrollment = {
                        "status": "skipped_orphan_weak_group",
                        "voiced_seconds": round(voiced, 3), "gate_source": gate,
                    }
                    return False
                elif source not in ("participant_metadata", "env_override"):
                    logger.warning(
                        "LILY_VOICE_ID | ENROLL_SKIPPED_UNVERIFIED | "
                        "session=%s group=%s source=%s — no existing voice "
                        "match and group provenance cannot found an identity",
                        self.sk.session_id, self.group_id, source,
                    )
                    self._voice_identity_enrollment = {
                        "status": "skipped_unverified_group",
                        "voiced_seconds": round(voiced, 3), "gate_source": gate,
                    }
                    return False
            prior = next(
                (r for r in existing if r["group_id"] == enroll_group), None
            )
            centroid, count = lily_voice_identity.lily_update_centroid(
                prior["centroid"] if prior else None,
                prior["sample_count"] if prior else 0,
                emb,
            )
            ok = await lily_persistence.lily_upsert_voice_identity(
                self.supabase, group_id=enroll_group, centroid=centroid,
                sample_count=count, model_tag=tag,
            )
            self._voice_identity_enrollment = {
                "status": "enrolled" if ok else "failed_write",
                "group_id": enroll_group,
                "sample_count": count,
                "voiced_seconds": round(voiced, 3),
                "gate_source": gate,
                "model_tag": tag,
                "redirected_from": (
                    self.group_id if enroll_group != self.group_id else None
                ),
                "redirect_score": redirect_score,
            }
            logger.info(
                "LILY_VOICE_ID | ENROLLED_FROM_VOICED | session=%s group=%s "
                "n=%d voiced=%.1fs gate=%s tag=%s ok=%s",
                self.sk.session_id, enroll_group, count, voiced, gate, tag, ok,
            )
            return ok
        except Exception as e:
            self._voice_identity_enrollment = {
                "status": f"failed:{type(e).__name__}",
                "voiced_seconds": round(voiced, 3), "gate_source": gate,
            }
            logger.warning("LILY_VOICE_ID | ENROLL_AT_CLOSE_FAILED | %s", e)
            return False

    def persistence_group_id(self) -> str:
        """VOICE-TRUTH-001 V2: the ONE group id this session's durable
        memory AND voiceprints write under. The session memory used to
        write under the live group (the room name on a cold session) while
        the voiceprints redirected to the device-stable id — two halves of
        one night filed under two keys, so the next session staged a device
        fragment with voices but no memory and the name door short-circuited
        onto it. Same id, both writers."""
        return self._effective_enroll_group_id()

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
        # VOICE-TRUTH-001 V4: the rekey and the four reloads (asked history,
        # known-speaker voiceprints, stored prefs, group memory) are
        # independent reads/writes — they used to run as five SEQUENTIAL
        # asyncio.to_thread hops on a sync client that shares its executor
        # with the ECAPA forward pass (~80s live on 17:51). Gathered now;
        # the wall-clock is logged (GROUP_ID_UPGRADE_LOADS_MS) and a failed
        # rekey degrades to a warning instead of killing the door task.
        async def _none():
            return None

        loads_t0 = time.monotonic()
        rekey_res, asked_res, known_res, stored_prefs, memory = (
            await asyncio.gather(
                lily_persistence.lily_rekey_group(
                    self.supabase, old, new_group_id, self.sk.session_id
                ),
                lily_bank.lily_load_asked_history(self.supabase, new_group_id),
                (
                    lily_persistence.lily_load_voiceprints(
                        self.supabase, new_group_id
                    )
                    if self.stt is not None else _none()
                ),
                lily_persistence.lily_load_group_prefs(
                    self.supabase, new_group_id
                ),
                lily_memory.lily_load_group_memory(self.supabase, new_group_id),
                return_exceptions=True,
            )
        )
        logger.info(
            "LILY_MEMORY | GROUP_ID_UPGRADE_LOADS_MS | session=%s group=%s "
            "ms=%.0f (rekey + asked + voiceprints + prefs + memory, gathered)",
            self.sk.session_id, new_group_id,
            (time.monotonic() - loads_t0) * 1000,
        )
        for label_, res in (
            ("rekey", rekey_res), ("asked_history", asked_res),
            ("voiceprints", known_res), ("prefs", stored_prefs),
            ("memory", memory),
        ):
            if isinstance(res, BaseException):
                logger.warning(
                    "LILY_MEMORY | GROUP_ID_UPGRADE_LOAD_FAILED | session=%s "
                    "load=%s error=%s", self.sk.session_id, label_, res,
                )
        if isinstance(stored_prefs, BaseException):
            stored_prefs = None
        if isinstance(memory, BaseException):
            memory = None
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
        if not isinstance(asked_res, BaseException) and asked_res is not None:
            self.asked_history = asked_res
        # Refresh known_speakers under the resolved id. 1.6.6 applies this
        # list at stream start, so this primarily protects reconnect paths.
        if self.stt is not None:
            try:
                if isinstance(known_res, BaseException):
                    raise known_res
                # Lazy import (WO-LILY-RECOG-DELIVERY-001): the W3 Cut 3
                # mixin extraction moved this method here WITHOUT its
                # lily_agent-module names — SpeakerIdentifier and
                # lily_stt_focus_kwargs resolved fine in lily_agent.py and
                # NameError'd here, silently downgraded to the except below
                # ("known_speakers refresh failed") on every live upgrade.
                # Imported at call time because lily_agent imports this
                # module at its own top (a top-level import is a cycle).
                from lily_agent import (  # noqa: PLC0415
                    SpeakerIdentifier,
                    lily_stt_focus_kwargs,
                )
                known_rows = known_res or []
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
        if stored_prefs:
            merged = dict(stored_prefs)
            merged.update(self.prefs or {})
            self.prefs = merged
            logger.info(
                "LILY_PREFS | RECONCILED | group=%s keys=%s "
                "(post-upgrade)",
                new_group_id, ",".join(sorted(merged.keys())),
            )
        block = lily_memory.lily_build_memory_block(
            memory, prefs=self.prefs, recognized_by=source
        )
        if block:
            self.memory_block = block  # llm_node injects it next turn
            if source in _CONFIRMED_IDENTITY_SOURCES:
                self.identity_confirmed_source = source
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
            # Name-door tail, upgrade leg (WO-LILY-RECOG-DELIVERY-001): a
            # name-door promotion awaits THIS upgrade before its own tail
            # runs, so the guard here keeps the beat from firing from inside
            # the upgrade — same 11:31 double, one call frame earlier. The
            # carried/uncarried decision (stamp on confirm vs. beat stays
            # armed) is the shared helper's; the promote tail then no-ops.
            self._name_door_promotion_tail(source, new_group_id)
        else:
            # Task 1: recognition that arrives AFTER the door becomes a
            # recovery moment, not a silent nothing.
            self._record_identity_promotion(
                source, new_group_id,
                decision="late_beat_path", carried_memory=None,
            )
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
        # WO-LILY-RECOG-DELIVERY-001: stamp where the generation counter
        # stood when the door opened. The promotion tail compares against
        # this to decide whether the organic reply's context snapshot
        # already happened (uncarried — the 17:51 memory-blind reply) or is
        # still ahead (carried). Stamped here, before the first await, so
        # the door task's own Supabase latency is exactly what the
        # comparison measures.
        self._name_door_entry_seq = self._ctx_snapshot_seq
        # VOICE-TRUTH-001 V4: door latency is measured — ts_start here,
        # ts_resolved when the block becomes visible (identity_promotions).
        self._name_door_opened_at = time.time()
        if self.group_id_source in _STRONG_GROUP_SOURCES:
            return False
        # VOICE-TRUTH-001 V2 (Auditor B's fragmentation finding): a STAGED
        # device candidate used to short-circuit this door to
        # device_plus_name on the THIN device fragment WITHOUT consulting
        # the name index — the most-recent rule below never ran, so a
        # returner whose device had one thin session was re-keyed to that
        # fragment every night while his 23-session group sat untouched
        # (Rami: 30 voiceprint groups). The name index is ALWAYS consulted;
        # when several groups know the stated name, the one with the MOST
        # HISTORY (sessions, then questions) wins, with the device candidate
        # as the tie-break and recency as the last resort. The HOTFIX-010
        # identity boundary holds: a stated name IS verification for this
        # door (verified=False — the biometric still outranks); a name NOT
        # on any file promotes nothing.
        staged = getattr(self, "device_candidate_group_id", None) or None
        staged_names = {
            str(n).strip().casefold()
            for n in (
                self._device_candidate_memory or {}
            ).get("player_names") or []
            if str(n).strip()
        } if staged else set()
        name_on_staged = bool(staged) and name.casefold() in staged_names
        try:
            groups = await lily_persistence.lily_groups_for_player_name(
                self.supabase, name
            )
        except Exception as e:
            logger.warning("LILY_MEMORY | NAME_DOOR_FAILED | %s", e)
            groups = []
        # Z3: the stated-name route has now REPORTED for this table —
        # whatever the result, the identity question is no longer waiting
        # on this door, so the no-match hold may release.
        self._identity_name_door_checked = True
        groups = [g for g in groups if g and g != self.group_id]
        if not groups:
            if name_on_staged:
                # The device fragment is the ONLY file that knows this name
                # (the index query failed or the thin fragment is all
                # there is): device history + a name ON that history.
                logger.info(
                    "LILY_MEMORY | NAME_DOOR_DEVICE_MATCH | session=%s "
                    "name=%s group=%s — stated name is on this device's "
                    "staged file and the index names no richer group; "
                    "promoting weakly (voice still outranks)",
                    self.sk.session_id, name, staged,
                )
                await self._promote_device_candidate(
                    "device_plus_name", verified=False
                )
                return True
            return False
        history: dict = {}
        if len(groups) > 1:
            try:
                history = await lily_persistence.lily_group_history(
                    self.supabase, groups
                )
            except Exception as e:
                logger.warning("LILY_MEMORY | NAME_DOOR_HISTORY_FAILED | %s", e)
                history = {}
        if len(groups) > _NAME_DOOR_AMBIGUOUS_CEILING and staged not in groups:
            # Pathological: too many same-name candidates to be one fragmented
            # person and no device evidence to disambiguate; a wrong guess is
            # likely, so wait for the voice.
            logger.info(
                "LILY_MEMORY | NAME_DOOR_AMBIGUOUS | name=%s groups=%d — past "
                "the ambiguity ceiling (%d); waiting for the voice rather than "
                "guessing",
                name, len(groups), _NAME_DOOR_AMBIGUOUS_CEILING,
            )
            return False
        candidate = self._pick_name_door_candidate(
            name, groups, history, staged
        )
        if staged and candidate == staged:
            logger.info(
                "LILY_MEMORY | NAME_DOOR_DEVICE_MATCH | session=%s "
                "name=%s group=%s — the index and this device's staged file "
                "agree; promoting weakly (voice still outranks)",
                self.sk.session_id, name, staged,
            )
            await self._promote_device_candidate(
                "device_plus_name", verified=False
            )
            return True
        if staged and candidate != staged:
            logger.warning(
                "LILY_MEMORY | NAME_DOOR_PREFERS_HISTORY | session=%s name=%s "
                "staged_device=%s (%s) picked=%s (%s) — the device fragment "
                "is thinner than the group the name index knows; the device "
                "quarantine is released in favour of the richer group "
                "(fragment merge is a documented follow-up, not done here)",
                self.sk.session_id, name, staged,
                _history_label(history.get(staged)),
                candidate, _history_label(history.get(candidate)),
            )
        staged_ok = await self.stage_device_candidate(candidate, "name_stated")
        if not staged_ok:
            return False
        logger.info(
            "LILY_MEMORY | NAME_DOOR_OPENED | session=%s name=%s group=%s — "
            "recognised off a stated name; the voice matcher still runs and "
            "still outranks this",
            self.sk.session_id, name, candidate,
        )
        await self._promote_device_candidate("name_stated", verified=False)
        return True

    @staticmethod
    def _pick_name_door_candidate(name, groups, history, staged):
        """V2 ranking: most history (sessions, then questions) wins; the
        staged device candidate breaks ties; recency (the index returns
        most-recent-first) is the last resort. Pure."""
        def key(gid):
            h = history.get(gid) or {}
            return (int(h.get("sessions") or 0), int(h.get("questions") or 0))

        best = max(key(g) for g in groups)
        tied = [g for g in groups if key(g) == best]
        if staged in tied:
            pick = staged
        else:
            pick = tied[0]
        if len(groups) > 1:
            logger.info(
                "LILY_MEMORY | AMBIGUOUS_PICKED_HISTORY | name=%s groups=%d "
                "picked=%s sessions=%d questions=%d tie_break=%s — staged "
                "weakly; the voice matcher still outranks",
                name, len(groups), pick, best[0], best[1],
                "device" if pick == staged else "recency",
            )
        return pick

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

