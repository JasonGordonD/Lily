# GUARD_MAP.md — complete inventory of guards, watchdogs, gates, lints, recovery layers and suppressions

**WO:** WO-LILY-HOTFIX-007 (Deliverable 1 — prerequisite of the consolidation mandate)
**Scope:** `lily_agent.py` (14,659 lines) and `lily_speech_delivery.py` (1,111 lines), plus the
choke-point primitives they call in `lily_say_gate.py`.
**Status:** READ-ONLY analysis. No code was modified.
**Date:** 2026-08-09

---

## 0. How to read this document

Every mechanism below is one of six kinds:

**Amendment 2026-08-09 (WO-LILY-HOTFIX-007 Y10):** chain F is now CLOSED — see §8 chain F for the
disposition. Mechanism 21's "bypassing `gated_say`" and its "only the tts_node gates can stop it"
note are therefore superseded: the auto-resume dispatches through `gated_say` and consults a derived
floor read before firing. Everything else in this document remains as analysed.

| Kind | Meaning |
|---|---|
| **GATE** | Refuses/blocks an action at a choke point (returns False, returns early, yields silence) |
| **WATCHDOG** | A timer or tick loop that *acts* into silence or stall |
| **RECOVERY** | Re-dispatches, re-claims, re-airs, or releases-and-retries |
| **SUPPRESSION** | Makes an already-generated turn physically silent |
| **LINT** | Detects and logs only (telemetry); mutates nothing |
| **REWRITE** | Replaces the outbound words with deterministic copy |

`file:line` is the definition site. Where a mechanism is a *decision + call site* pair, both are given.
`A` = `lily_agent.py`, `SD` = `lily_speech_delivery.py`, `SG` = `lily_say_gate.py`.

**Mechanism count: 80** (see §9 for the tally by kind and by owning WO).

---

## 1. The speech-act registry — the substrate everything else keys off

| # | Name / log tag | file:line | Trigger | Action | WO | Interactions |
|---|---|---|---|---|---|---|
| 1 | `SpeechActRegistry` (claim / confirm / release) | SG:518–645 | Any keyed speech act | Three-state ledger: `None` → `CLAIM_PENDING` (at dispatch) → `CLAIM_CONFIRMED` (at playout completion); `release()` on failure | say-gate WO §1 | **THE shared substrate.** Mechanisms 2, 3, 4, 8, 9, 12, 13, 18–24, 27–31, 36, 44, 52, 62 all read or write it. A confirmed act is final forever — this is what makes "already spoken" decidable, and what makes a *wedged* PENDING claim a permanent silence (mech. 3 exists solely to unwedge it) |
| 2 | `gated_say` — THE code-dispatch choke point | SD:96–236 | Every code-triggered speech act | Runs 5 pre-gates (mechs. 5, 6, 7, 10, 11) → claims the key → appends a regen directive if armed (mech. 20) → `instructed_reply` → arms the stale-claim watchdog (mech. 4) | say-gate WO §1 | Single funnel. Anything that blocks here blocks *every* code-driven turn: hold, question-pending, P0-G, P8 game-lane, dup-claim |
| 3 | `_supersede_stale_claim` / `LILY_SAY \| STALE_CLAIM_SUPERSEDED` | SD:238–253 (call SD:184–191) | A dup-suppressed re-dispatch whose held claim is PENDING, older than 12 s, never started playout, nothing airing | Releases the frozen claim so the retry may speak | HOTFIX-001 (08-06 Krisp/RoomIO wedge) | **Directly opposes mech. 1's dup suppression.** Guarded by the playout-started ledger (mech. 12) and `host_speaking` so it cannot cancel a long turn mid-air |
| 4 | `_stale_claim_watch` / `STALE_CLAIM_RELEASED`, `STALE_CLAIM_EXHAUSTED` | SD:255–320 | Keyed dispatch whose claim is still PENDING after 12 s with no playout start | Releases the claim, re-`expect_delivery()`, re-`gated_say` (`+stale_retry`); bounded to 2 retries per key, 20 rechecks | HOTFIX-001 | **A second re-air producer.** It re-dispatches the *same instructions*, so its retry can collide with mech. 22 (regen gate) and mech. 24 (air dup guard), and it is one of the four actors in Chain A (§8) |

---

## 2. Dispatch-time gates inside `gated_say`

| # | Name / log tag | file:line | Trigger | Action | WO | Interactions |
|---|---|---|---|---|---|---|
| 5 | Hold gate / `LILY_SAY_SUPPRESSED \| reason=hold` | `hold_blocks_dispatch` A:4487–4495; call SD:119–124 | `_hold_active` and source not in `_HOLD_EXEMPT_SOURCES` (`stop_primitive`, `hold_ack`, `hold_release`, A:4443–4446) | Refuses the dispatch | PATCH-002 A4 | Binds **every** lane including the idle watchdog (mech. 15 checks it at A:3882–3886). Entered by mech. 6b (self wait-promise), the STOP primitive (mech. 33), a decline; released by any user final, a hard game event, or timeout |
| 6 | Question-pending gate / `reason=question_pending` | `question_pending_blocks_dispatch` A:4603; call SD:127–132 | Lily asked a conversational question and the table has not answered | Refuses the dispatch (game-lane acts exempt) | PATCH-003 P6/P10 | Entered at A:5525–5526 from `lily_stacked_question_flag` on a **played** turn. Its game-lane exemption is exactly what forced P0-G to be narrowed (mech. 7) |
| 7 | P0-G scoped progression pause / `LILY_PROGRESSION \| DISPATCH_PAUSED` | SD:133–158 | Act is `question_delivery`/`question_nudge` **and** (`_awaiting_address_since` or `pending_setup_jobs()`) | Refuses the dispatch | P0-G, scoped 2026-08-09 | **Deliberately narrower than `progression_paused_reason()` (mech. 14).** The unfiltered version re-blocked the acts mech. 6 exempts — `question_pending` refused the very nudge that resolves it, and `host_speaking` refused the nudge that fires as her own turn ends. The same narrowing is duplicated in `expect_delivery` (mech. 18) |
| 8 | Dup-claim suppression / `reason=dup` | SD:178–198 | `say_registry.claim(key)` fails | Refuses unless mech. 3 supersedes | say-gate WO §1 | The oldest suppression. Its false-positive mode (frozen claim) is why mechs. 3, 4 exist |
| 9 | P8 game-payload gate / `reason=no_live_game` / `reason=game_stopped` | `game_payload_blocked` A:4458–4470; `_GAME_LANE_ACTS` A:4452–4456; call SD:164–176 | Game-lane act with `game_started` False, `game_over` True, or STOP sticky | Refuses the dispatch | PATCH-003 P8 | Closes the "Nobody landed it" lockout-into-lobby class. Overlaps mech. 33 (STOP latch) and mech. 26 (pre-game window refusal) |
| 10 | STOP sticky freeze (dispatch side) | `_delivery_stop_sticky` checked SD:167, A:1882, A:3874, A:4459, A:5729, SD:383 | STOP primitive latched | Every game-plane owner frozen until explicit resume | PATCH-002 A5/T12 | Checked in **six** independent places; a genuinely global latch |
| 11 | Hold timeout release | `hold_timed_out` A:4525–4529; watchdog call A:3883–3884 | Hold older than `lily_config.hold_timeout_seconds()` | Releases the hold from inside the watchdog tick | PATCH-002 A4 | The **only** watchdog action permitted while held — everything else `continue`s |

---

## 3. Delivery-claim registration and its rewrites

| # | Name / log tag | file:line | Trigger | Action | WO | Interactions |
|---|---|---|---|---|---|---|
| 12 | Playout-started ledger (`_playout_started_ids`) / `note_playout_started` | A wiring 14136–14141 → SD:322–340 | `agent_state_changed → "speaking"` | Marks the airing speech id; **also cancels any pending cut recovery** (SD:329) | HOTFIX-001 | Read by mechs. 3, 4, 30 to distinguish "airing" from "wedged". Its cut-recovery cancel is a one-emission cancel point (mech. 21) |
| 13 | `register_delivery_claim` — THE delivery event | SD:638–780; call A:12814–12832 | Every outbound turn while a question is armed and the window is closed | Returns `claimed_structural` / `claimed_core_sentence` / `rewrite_strict` / `duplicate` / `None`; claims `q_{N}_delivery` | desync WO Sub-agent B, WS-1 | Text similarity is **telemetry only** (mech. 43); the claim is the sole delivery truth. Feeds mech. 26 (window open), mech. 30 (reconcile), mech. 47 (MC start) |
| 14 | `progression_paused_reason` (full) | A:4471–4485 | Any of: `game_stopped`, `hold`, `question_pending`, `address_unanswered`, `host_speaking`, `setup_pending` | Names the reason a delivery must not take the floor | PATCH-003 / P0-G | Used **unfiltered** by mech. 16 (`dispatch_armed_question`, A:1888–1895) and mech. 15 (watchdog, A:3924–3931), but **filtered to 2 of 6 states** by mechs. 7 and 18. This asymmetry is a live inconsistency worth consolidating |
| 15 | `LILY_PROGRESSION \| WATCHDOG_PAUSED` | A:3924–3931 | `progression_paused_reason()` truthy on a watchdog tick | `continue` — skips the whole tick | P0-G | Because it uses the **unfiltered** predicate, `host_speaking` alone parks every watchdog recovery for the duration of any Lily turn |
| 16 | `LILY_PROGRESSION \| PAUSED` (dispatch_armed_question) | A:1888–1895 | Same, at post-reveal N+1 dispatch | Returns False | P0-G | Combines with mech. 17 and mech. 19: three independent refusals stack on the same N+1 dispatch |
| 17 | T2 answered-question re-air ban | `question_already_answered` A:4319–4330; call sites A:1897, A:4222, A:5636, A:4163 | Answer candidates exist for the current Q, or the Q is in `_answered_questions` | Blocks dispatch / nudge / refire; releases the outstanding claim (`ANSWERED_NO_REAIR`) | PATCH-001 T2 | Closed the Saturn-re-read-4s-after-the-answer and Mitochondria-2s-after-verdict class. Interacts with mech. 30 (it is the first thing reconcile checks) |
| 18 | `expect_delivery` gate / `LILY_DELIVERY \| EXPECT_BLOCKED` | SD:377–416 | STOP sticky, address unanswered, setup pending, pre-game, nothing armed, window open, or already claimed | Refuses to arm the one-shot structural intent | WS-1 + P0-G scoped | Carries the **same narrowed** P0-G filter as mech. 7 and for the same reason: the unfiltered check made the pending question's own nudge air without ever registering as a delivery |
| 19 | N12 next-delivery hold / `LILY_TRANSITION \| DELIVERY_DUP`, `DELIVERY_HELD` | `_transition_holds_next_delivery` A:1829–1872; call A:1905 | Open transition whose verdict is unnarrated, still airing, or whose `next_delivery` already ran | Blocks N+1 | HOTFIX-006 N12 | Moved the "she jumped from a question to a question and a third question" guard off timing and onto the journal (mech. 51) |
| 20 | Regeneration gate — dispatch half / `_REGEN_DELIVERY_DIRECTIVE`, `_REGEN_REAIR_DIRECTIVE` | `arm_reair_gate` SD:495–499, `take_reair_dispatch` SD:505–513; consumed SD:206–210; directives SD:27–39 | The prior airing of this act was cut or suppressed | Appends a fresh-words directive to the re-dispatched instructions | WS-3, OMNIBUS-003 + AMENDMENT-001 | Armed at A:5448–5449 and by mech. 21 (SD:619). Consumed **only after the claim survives** mech. 8, so a dup dispatch never eats the arm. Hands the signal to mech. 22 via `_reair_turn_pending` |
| 21 | Cut-recovery contract / `LILY_CUT_RECOVERY \| RESUMED` | `_cut_recovery_should_arm` SD:547–564, `arm_cut_recovery` SD:566–579, `_cut_recovery_should_fire` SD:593–612, `trigger_cut_recovery` SD:614–626, `_cut_recovery_watch` SD:628–636; contract note A:1462–1477; arm site A:5456–5457 | Cut/mid-stream-failed **organic** turn (no keyed claim released), game live, window closed, not adjudicating; then after `lily_config.cut_recovery_grace()` still: token current, nobody speaking, no user turn within 2 s of the arm (`_CUT_RECOVERY_USER_TURN_LOOKBACK`, SD:58) | Arms the re-air gate (mech. 20) and dispatches `_CUT_RECOVERY_DIRECTIVE` via `instructed_reply` — **bypassing `gated_say`** | WS-3, STREAM-INTEGRITY-002 | **The dead-air detector named in the WO.** Cancel points: `note_playout_started` (SD:329), `note_user_turn` (SD:591), a newer cut bumping the token. Because it goes through `instructed_reply` rather than `gated_say`, mechs. 5–11 do **not** gate it — only the tts_node gates (mechs. 22–25, 34–41) can stop it |
| 22 | Regeneration GATE — playout half / `LILY_REGEN_GATE` | `reair_verbatim_should_regenerate` SD:535–545; enforcement A:12640–12671 | This turn is a re-air (mech. 20 signal) **and** the repeat/paraphrase lint tripped **and** it is not a question delivery | Releases the owner's claims (`reason=regen_gate`), yields 100 ms silence, fires `generate_reply(_REGEN_REAIR_DIRECTIVE)`; bounded to one retry via `_reair_regen_pending` | WS-3 | Promotes mechs. 39/40 from lint to suppression. Its released claims then satisfy mech. 8's re-claim, and its `generate_reply` is a **fourth** re-air producer |
| 23 | Stubborn-repeat suppression / `reason=stubborn_repeat` | A:12672–12709 | `_reair_regen_pending` set **and** the regen retry *also* tripped the repeat lint **and** not a question delivery | Releases claims (`reason=stubborn_repeat`), yields silence, **gives up the floor** | WS-3 tightening, live 2026-08-09 `lily-A070E8` (the same cut greeting aired up to four times) | Terminates the mech. 21 → 22 → 21 loop. The comment at A:12677–12684 is the explicit admission that the pre-existing contract ("a stubborn repeat still yields the floor") aired the third copy |
| 24 | T3 air-path dup guard / `LILY_TURNS \| DUP_TURN_SKIPPED \| path=air` | `air_dup_guard` A:4791–4802; enforcement A:12910–12932 | Outbound cleaned text is an **exact** member of the last 6 played turns, length ≥ 15, and it is not a delivery turn | Marks the speech id suppressed, releases the owner's claims, yields silence | PATCH-001 T3 (widened from HOTFIX-002's last-turn-only compare) | **Exact match only.** Defeated by a single differing character — an appended sentence or a `\n` vs `\n\n` difference. See §7 for the falsification |
| 25 | BUG-2 delivery duplicate suppression / `reason=dup \| act=question_delivery` | SD:771–776; enforcement A:12833–12848 | Turn textually re-performs an already-claimed question | Physically silent, no retry | desync WO Sub-agent B | Only fires on `_delivery_text_matches_armed` — a **paraphrased** re-delivery returns `None` and speaks. This is the A070E8 q1 double-ask (§7) |
| 26 | Window-open guards / `OPEN_BLOCKED`, `PRE_GAME_REFUSED`, `UNREGISTERED_REFUSED`, `STEAL_REFUSED_REVEALED` | `open_window` A:5727–5800 | STOP sticky / pre-game / no registered question / steal over a burned question | Refuses to open an adjudicable window | WS-1; HOTFIX-006 N3 inv.1; HOTFIX-006 N8 | `PRE_GAME_REFUSED` is the guard that stops "Rhonda's self-introduction adjudicated as a wrong answer to a question never spoken" (A:5735–5741). Window open is also the durable "delivery reached playout" record X3 gates the reveal on (A:5798–5804) |
| 27 | `force_confirm_delivery_heard` / `NEAR_MISS_CONFIRM` | A:4148–4184 | Spoken/prompt ratio ≥ 0.9 but the structural claim missed | Claims+confirms `q_N_delivery`, opens the window — **never re-reads** | Gun 1 / Gun 3 | The anti-re-air branch of mechs. 29 and 30. Also reached from mech. 28 |
| 28 | `delivery_reached_the_table` / `CUT_AFTER_QUESTION_AIRED`, `DELIVERY_CUT_EXHAUSTED` | SD:457–493; call A:5438–5443 | A cut delivery whose text still presented the whole question | Confirms instead of re-reading; past `_DELIVERY_MAX_CUT_REAIRS = 2` (SD:84) releases the claim and returns the question to supply | HOTFIX-001-era live `lily-2C489B` (three identical re-reads, each cut on the same word) | `interrupted` **only** — a suppressed turn produced no audio and can never have put the question in the room (A:5432–5437) |
| 29 | Delivery nudge / `LILY_WINDOW \| DELIVERY_NUDGE`, `NUDGE_NEAR_MISS` | A:5588–5677; `WINDOW_FALLBACK_AGENT_TURNS = 2` A:503 | Armed, window closed, no delivery claim after 2 finished agent turns, `ui_phase == "question"` | Dispatches ONE structural delivery nudge (or confirms via mech. 27 if ratio ≥ 0.9) | desync WO Sub-agent B | Replaced the old ghost-window fallback. Another re-air producer feeding Chain A |
| 30 | WS-2 registered-undelivered reconciliation / `UNDELIVERED_NEAR_MISS`, `UNDELIVERED_REFIRE`, `UNDELIVERED_RELEASE`, `ANSWERED_NO_REAIR` | A:4186–4314; contract note A:4084–4096; `UNDELIVERED_MAX_REFIRES = 2` A:507 | Armed, window closed, delivery claim unconfirmed, ≥ `undelivered_reconcile_seconds()/10` ticks, room quiet for `undelivered_refire_quiet_seconds()` | Re-fires the delivery (releasing the stale claim + `cancel_speech` first), then after 2 re-fires releases the question to supply | WS-2, OMNIBUS-003 | Guarded in sequence by mech. 17 (T2), the playout ledger (T1, A:4239–4245), `host_speaking`, and the room-quiet hold (A:4246–4257, added because the watchdog used to stack copies onto banter). Feeds mech. 31 |
| 31 | `_stuck_delivery_present` / `no_stuck_claims` | A:4110–4146 | Delivery re-fired ≥ once and still unconfirmed | Blocks WS-6's supply fallback (mech. 32) | WS-2/WS-6 | Deliberately keys off `_undelivered_refires`, not `_undelivered_ticks`, because the tick counter resets inside the same synchronous call that crosses the threshold — a zero-duration observable window |
| 32 | WS-6 supply-stall fallback / `SUPPLY_STALL`, `SUPPLY_FALLBACK_ARMED`, `SUPPLY_BANK_EMPTY`, `SUPPLY_FALLBACK_ERROR` | A:4027–4054, `arm_supply_fallback` A:4830–5011 | Nothing armed, nothing prefetched, past `_supply_fallback_ticks()` **and** `no_stuck_claims()` | Arms straight from the curated bank | WS-6, OMNIBUS-003 | Explicitly ordered behind mech. 31 "so a fallback never queues behind a ghost". **HOTFIX-008 Z2:** this rung is no longer the only reach into the bank — its draw half is factored into `_bank_to_supply` (delivery-state-independent, mech. 79); the ARM + nudge stay behind this mechanism's idle guards. The 2260354c stall class (supply dead while a question is armed / a window cycles) is now mech. 79's trigger, not a gap |
| 33 | STOP primitive / `LILY_STOP \| PRIMITIVE`, `RESUMED` | `handle_stop_primitive` A:4680–4737; `_freeze_game_delivery_for_stop` A:4616 | STOP utterance | Retires armed/prefetched content, kills the watchdog's ability to resurrect the turn, enters the hold, one brief ack | PATCH-002 A5/T12, P0-B | Checked at the very top of the user-final path (A:6471) so it bypasses every other gate |
| 34 | `invalidate_deliveries_for` / `LILY_DELIVERY \| INVALIDATED` | A:4395–4409 | Question answered while a delivery claim is still PENDING | Releases the claim and `cancel_speech` mid-playout | PATCH-001 T1 | With mech. 35 closes the "starts-after-release" hole |
| 35 | `cancel_speech` / `LILY_SPEECH \| CANCELLED` | A:4411–4437 | Any released claim whose handle may still start | Marks suppressed + `handle.interrupt(force=True)` | PATCH-001 T1 | **A suppression that produces an `interrupted=True` handle** — see §7, this is how a cancelled turn still lands in the transcript as `[cut off]` |

---

## 4. The tts_node say gate (ordered pipeline — order is load-bearing)

Executed top to bottom in `tts_node` (A:12466–12960). Each numbered step can rewrite, suppress, or pass.

| # | Name / log tag | file:line | Trigger | Action | WO | Interactions |
|---|---|---|---|---|---|---|
| 36 | Leak filter / `LILY_SAY_SUPPRESSED \| reason=leak` + burn protocol | A:12478–12492; `lily_filter_leaks` SG:145 | State-block sentinel/envelope/metadata markers in the outbound text | Strips them; if the leak could carry answer material, `on_answer_leak()` burns the armed/prefetched question | say-gate WO §1 | `[state note:` is excluded from the burn (A:12489–12491) so calling out the board cannot punish the table. Feeds mech. 42 (burn) |
| 37 | Speech hygiene / `LILY_SAY_GATE \| stripped N chars` | A:12501–12506; `lily_clean_for_speech` SG:89–103 | Always | Strips markdown/emoji, collapses multi-space **and blank lines**, preserves `[bracket]` audio tags | P4 | **Critical to §7:** the blank-line collapse is why the aired record has `\n` where the raw model prose had `\n\n` |
| 38 | False clean-slate rewrite / `FALSE_CLEAN_SLATE_REWRITTEN` | A:12514–12523; `must_rewrite_false_empty_claim` A:2847; `can_claim_empty_memory` A:2824 | A "no saved stats / clean slate" claim while absence is not a settled fact (probe outstanding, dispute open, returner claim seen) | Replaces with `lily_still_checking_rewrite()` | P0-A/P0-B/P0-C, HOTFIX-006 N1 | One of four REWRITEs at this layer; each changes the words *after* the delivery claim decision, which is why post-TTS text and pre-TTS prose diverge |
| 39 | False on-screen rewrite / `FALSE_ON_SCREEN_REWRITTEN` | A:12527–12543 | "look at the screen"/"picture is up" without `picture_on_glass_confirmed()` | `lily_picture_didnt_land_rewrite()` or `lily_picture_pending_rewrite()` | B4 | — |
| 40 | Dispute-sycophancy rewrite / `DISPUTE_SYCOPHANCY_REWRITTEN` | A:12545–12561 | Recognition dispute open, why-beat unanswered, mirror phrase detected | Substitutes a fixed honest explanation | P0-C | Uses mech. 45's detector as a gate rather than a lint |
| 41 | Yield-after-first-question enforcement / `YIELD_AFTER_QUESTION` | A:12563–12587; `lily_yield_after_first_question` SG:338 | ≥1 completed question in a non-MC-delivery turn | Physically clips the turn at the first question mark | PATCH-003 P10 | MC deliveries exempt (options legitimately follow the stem). Verdict-plus-next-question stacks are **not** exempt — that was the cartography→mitochondria leap |
| 42 | Mirror lint / `MIRROR_FLAG` | A:12589–12597; `lily_mirror_flag` SG:476 | Sycophantic opener | **LOG ONLY** | self-knowledge WO Task 2a | Same detector, gate at mech. 40, lint here |
| 43 | Stacked-question lint / `STACKED_QUESTION_FLAG` | A:12598–12604 | >1 question, exempt delivery | **LOG ONLY** | PATCH-003 P10 | Enforcement is mech. 41 |
| 44 | Repeat lint / `REPEAT_FLAG` | A:12605–12615; `lily_repeat_flag` SG:242 | Verbatim repeat against `sk.agent_turns` (turns that actually played) | **LOG ONLY** — but promoted to suppression by mechs. 22/23 | RECOGNITION-VARIETY Task 3b | Its input is `sk.agent_turns`, which mech. 52 writes. **Polluting that list disables this lint** (§7) |
| 45 | Paraphrase repeat lint / `PARAPHRASE_FLAG` | A:12616–12631; `lily_paraphrase_repeat_flag` SG:286 | Token-overlap ≥ `paraphrase_repeat_threshold()` over the last 3 played turns | **LOG ONLY** — promoted by mechs. 22/23 | PATCH-002 A4a (reassurance storm) | Folded into `repeat_kind`, so mech. 22 fires on *semantic* repeats too |
| 46 | Regen gate | (mech. 22) | — | — | — | — |
| 47 | Stubborn-repeat suppression | (mech. 23) | — | — | — | — |
| 48 | Empty-candidate retry / `LILY_EMPTY_CANDIDATE` | A:12712–12797 | Cleaned text < 3 chars | Releases pending claims (`reason=empty_candidate`), re-arms `expect_delivery`, one `generate_reply()` retry; on the **second** empty forces the deterministic armed sheet, else yields the floor | §11.1 + the 19:27:52 swallowed-delivery fix | The comment at A:12716–12722 states the reason: without the release, "the retry regenerates into a gate that suppresses the redelivery as a duplicate" — an explicit acknowledgement of Chain A |
| 49 | Strict delivery rewrite / `EXACT_SHEET_REWRITE`, `STRICT_REWRITE`, `STRICT_REWRITE_FAILED` | SD:678–735; call A:12818–12832 | Structural delivery act, or near-verbatim (ratio ≥ 0.9) unregistered performance | Replaces the model prose with `rendered_armed_question()` before TTS | WS-1 | Guarantees "never claimed silently". Mutates the words after the LLM produced them — another post-TTS/pre-TTS divergence source |
| 50 | P0-5 unowned-kickoff suppression / `reason=unowned_kickoff` | `unowned_kickoff_must_suppress` A:3021–3049; `lily_unowned_kickoff_fragment` SG:461; enforcement A:12850–12880 | Kickoff language on a turn that does not structurally own `q_N_delivery` | Physically silent, no retry, no re-air | P0-5 BE8D8B (widened 2026-08-09 to the question itself) | "Suppress directly … so setup/user-speech holds cannot regenerate the same debris" — an explicit anti-Chain-A carve-out |
| 51 | N12 transition-narration suppression / `reason=dup_transition` | `register_transition_narration` A:1740–1827; enforcement A:12882–12908 | Second, differently-worded narration of the same verdict beat, inside `_TRANSITION_NARRATION_WINDOW_SECONDS = 30.0` (A:580) | Physically silent | HOTFIX-006 N12 | Deliberately narrow (requires the transitioning question's own answer + a verdict cue) because "a false suppression would be a worse defect". Exempts the turn holding the verdict key — including "its re-air after a cut/regeneration, which legitimately carries fresh words" (A:1790–1793): **the re-air carve-out is written into the dup gate** |
| 52 | Punctuation-flush guard | A:12934–12943 | Final char not `.!?` | Appends a period so the blingfire tokenizer flushes | Lovebirds fix | Prevents SegmentSynchronizer deadlock (`text_done=false/audio_done=true`) |
| 53 | `note_post_tts_text` — the P0-C authoritative text bind | A:12945–12947 → A:2146–2155 | End of tts_node, after every rewrite/clip/substitution | Binds the exact TTS input to the speech id | P0-C | **Every early return above (mechs. 22, 23, 24, 25, 36, 48, 50, 51) skips this bind.** That is the falsification vector in §7 |

---

## 5. Watchdogs and recovery layers

| # | Name / log tag | file:line | Trigger | Action | WO | Interactions |
|---|---|---|---|---|---|---|
| 54 | Idle watchdog loop / `LILY_WATCHDOG \| TICK_FAILED` | A:3866–4082; `WATCHDOG_INTERVAL_SECONDS = 10.0` A:3838; start A:3841–3852, A:9245; stop A:3854–3864 | Every 10 s while the game is live | Runs the whole recovery ladder below | live 2026-07-15 stall class | `stop_idle_watchdog` exists because the loop outlived the AgentSession and dispatched against a dead session once per hangup (A:3855–3859) |
| 55 | `ARMED_LIMBO` | A:3941–4000 | Armed, window closed, delivery claim **CONFIRMED**, for 2 consecutive ticks (~20 s) | Candidates waiting → forces `adjudicate(steal_allowed=False, reclaim_transition=True)`; no candidates → `open_window()` | live 2026-07-15 04:05 (adjudication crashed between commit and reveal; the game parked on Q3) | **The `reclaim_transition=True` is load-bearing:** without it mech. 56's `SECOND_LANE_REFUSED` refused the recovery every ~20 s while ARMED_LIMBO forced it every ~20 s — "a permanent dead-air loop (lily-1C53C6)" (A:1626–1636, A:3968–3972). This is Chain B in §8. **HOTFIX-008 Z2c:** the forced adjudication now has a completion path through the AIRED-verdict gap too — a narration-complete open transition is RESUMED (mech. 80), not refused, so the limbo it forces can actually end |
| 56 | Transition claim / `LILY_TRANSITION \| OPEN`, `SECOND_LANE_REFUSED`, `RECLAIMED_UNAIRED` | `open_question_transition` A:1614–1679; `_transition_reached_air` A:1592–1612 | A lane opening a question transition | Two independent refusals (existing journal, claim key); `reclaim_unaired=True` from the recovery path releases a dead journal whose stages never reached air | HOTFIX-006 N12 + `lily-1C53C6` deadlock | `_transition_reached_air` is the arbiter: a bound narration, a CONFIRMED stage key, or a journaled `next_delivery`. A transition with **any** aired stage keeps the original refusal. **HOTFIX-008 Z2c:** that refusal held a NARRATION-COMPLETE transition (reveal+verdict aired, question never consumed) hostage — Chain B reopened through the aired-verdict gap. The recovery caller now RESUMES such a transition instead of reopening it (mech. 80); the refusal itself is unmodified and still guards every non-recovery lane and every partially-aired journal |
| 57 | Transition journal / `STAGE`, `STAGE_DUP`, `NO_OPEN_TRANSITION` | `journal_transition` A:1681–1721; stages `("reveal","verdict","next_delivery")` A:1580 | Each stage of one question transition | Appends once; a second attempt at the same stage is logged and refused | HOTFIX-006 N12 | Read by mechs. 19, 51, 56 |
| 58 | P9 responsiveness floor / `ADDRESS_UNANSWERED` | A:3887–3902; `address_unanswered` A:4583 | A direct address unanswered past `responsiveness_budget_seconds()` | WARN once | PATCH-003 P9 | Sets `_awaiting_address_since`, which is one of the **two** states mechs. 7/18 still pause on |
| 59 | P10 question re-offer + hold conversion / `QUESTION_PENDING` | A:3903–3923 | Pending conversational question timed out | ONE gentle re-offer (exempt from mech. 6), then `release_question_pending` + `enter_hold` | PATCH-003 P10 | The re-offer is a re-air producer that is **deliberately exempt** from the gate that would block it |
| 60 | `IDLE_REARM` | A:4005–4026 | Nothing armed, no window, a prefetched question in hand | `arm_next_question()` + `expect_delivery()` + `question_nudge` | 2026-07-15 stall class | Another re-air producer feeding Chain A |
| 61 | `IDLE_REPREFETCH` / `PREFETCH_HARD_TIMEOUT` | A:4055–4080; `PREFETCH_HARD_TIMEOUT_TICKS = 9` (~90 s) A:3839 | Prefetch task absent/done, or alive past 90 s | Restarts / cancels the supply task; sets an honest vamp status note | 2026-07-15 stall class | The 583a0f16 five-minute stall lived in the interaction between this and mech. 32 (`task.done()` each tick kept the hard timeout from ever climbing). **HOTFIX-008 Z2:** no longer the only re-prefetch — supply recovery is decoupled from the idle branch: a failed prefetch schedules its own bounded retry (mech. 79) instead of waiting for an idle tick that may never come (the 2260354c session had zero idle ticks all session) |
| 62 | Mode-switch flush + re-arm / `MODE_FLUSH`, `MODE_SWITCH_DISCARD`, `CATEGORY_SWITCH_DISCARD`, `REARM_BLOCKED` | A:5032–5136 | Deck/mode/category switch | Flushes armed+prefetched, keeps them in the drawn-set, resets the stall counter "so the idle watchdog cooperates" | DESYNC-HONESTY-001 D | Explicit cooperation contract with mech. 54 |
| 63 | Lobby empty-STOP recover / `EMPTY_STOP_LOBBY_RECOVER` | A:12368–12441; `_llm_node_with_empty_stop_guard` A:12185 | Gemini empty/STOP fail-closed pre-game | Sleeps 150 ms so `GENERATION_FAILED` releases the claim, then one re-`gated_say` of the opener | F1 | Documents its own one-emission reasoning: "keyed release on GENERATION_FAILED does not arm cut recovery; this recover is the sole second attempt for the opener" (A:12374–12376) |
| 64 | WS-8 enrollment retry / `ENROLL_*` | `_maybe_retry_enrollment` A:8354–8407; driver A:6581 | Bound player below the voiceprint threshold, past cooldown | Re-fires enrollment | WS-8 | — |
| 65 | Wrapup/score reconciliation / `SCORE_DIVERGENCE`, `ROSTER_DIVERGENCE`, `CUSTOM_ROUND_DIVERGENCE` | A:9304–9372 | Wrap-up | Compares ledger-derived standings to the live board and records the divergence | WS-7 | Detection only at this layer |
| 79 | Z2 phase-independent supply recovery / `SUPPLY_SILENT_WINDOW`, `LILY_SUPPLY \| RETRY`, `BANK_TO_SUPPLY`, `SUPPLY_EXHAUSTED`, `RECOVERY_FAILED`, plus `INSURANCE_BANK_HIT/EMPTY/ERROR` on the prefetch insurance leg | `ensure_supply_recovery` / `_recover_supply` / `_bank_to_supply` / `_supply_silent_window`; watchdog check ahead of the armed/window branches; failure hook at the tail of `start_prefetch`'s wrapper | (a) A prefetch that produced nothing (genuine failure, not a mode/category discard) schedules recovery from the failure itself; (b) watchdog: `next_question` None + prefetch task done/absent + past the first arm, for 2 consecutive ticks, in a NON-idle phase (idle keeps mechs. 60/32/61) | Ladder: one de-escalated re-prefetch (adult high→medium, general medium→low) → `_bank_to_supply` (lands the row on `next_question` WITHOUT arming — delivery stays phase-owned) → ONE honest line + explicit pause offer (`supply_exhausted` act via `gated_say`), never an open-ended wait | HOTFIX-008 Z2, session `lily-938EFF-2260354c` | Exists because mechs. 32/60/61 were ALL idle-gated: q1 armed/window-cycling made a dead supply line invisible for 4.5 min with the bank full. Incident state (`_supply_retry_attempts`, `_supply_exhausted_notified`, `_supply_silent_ticks`) resets when any question lands on the supply line; `stop_idle_watchdog` cancels the ladder with the loop |
| 80 | Z2c transition release / `RESUMED_COMPLETE`, `RELEASED_COMPLETE`, `REOPEN_REFUSED_TERMINAL` | `transition_narration_complete` / `release_completed_transition` (after `dispatch_armed_question`); release call sites: adjudicate arm-failed branch (`supply_empty_at_arm`), post_reveal playout seam (`supply_empty_post_reveal`); resume: adjudicate transition-open site (`reclaim_transition=True` only); terminal-window refusal: the delivery_claim reopen branch | A transition whose narration fully aired (reveal+verdict journaled, verdict provably played per mech. 56's arbiter) that cannot arm/deliver N+1; and any delivery_claim window (re)open for a question `question_is_terminal` already owns | Journals the terminal `next_delivery` marker (`delivered_q=None` — journal KEPT, never popped), frees the claim, clears the open slot; recovery adjudication RESUMES the aired beat and runs only bookkeeping (burn/consume/arm-or-release), re-airing nothing; a ruled question's window never reopens | HOTFIX-008 Z2c, session `lily-938EFF-2260354c` | Closes Chain B's aired-verdict gap (§8). The circular wait: next_delivery needed supply, supply recovery (mech. 79's arm rung + mechs. 32/60/61) needed idle, idle needed the transition closed. `_transition_reached_air`, SECOND_LANE_REFUSED and ARMED_LIMBO are deliberately unmodified — with the beat resumable/releasable they behave correctly |
| 81 | W6 timer self-cancel guard + burned-partial resume / `RESUMED_BURNED` | adjudicate head (timer cancel now skipped when `self._window_timer is asyncio.current_task()`); adjudicate transition-open site (`resumed_burned`, `reclaim_transition=True` only) | (a) Any window-timeout adjudication — it RUNS INSIDE the timer task (`open_window._expire` awaits it), so the old unconditional timer cancel cancelled the running adjudication itself at its first await (the reveal-publish gather): stages=[reveal], no verdict, no CRASHED line; (b) recovery re-entry over a narration-PARTIAL open transition whose question is BURNED (answer provably on air) | (a) the expired timer is not cancelled — the timeout beat completes; (b) the missing reveal/verdict stages are journaled `source=already_on_air` (narration detail = mech. 56 air-proof) and the beat resumes as mech. 80 bookkeeping — nothing re-airs; an UNBURNED dead journal keeps the mech. 56 reclaim (nothing aired there; re-narration is the cure) | HOTFIX-009 W6, session `lily-5E3036-b56b5eb4` q4 (organic reveal 05:34:35; self-cancelled adjudication 05:34:52; `RECLAIMED_UNAIRED` re-narration "Nobody landed it. Marie Curie" 05:36:51 colliding with the q5 delivery) | Burn was already authoritative at draw (`_no_repeat_exclusion`), arm (`REARM_BLOCKED`), steal and reconnect — the transition owner was the one seam that never consulted it. No guard deleted: no existing mechanism covered self-cancellation (the class was invisible — CancelledError skips `except Exception` and the CRASHED log) |

---

## 6. Content, identity, honesty and safety gates (complete but compressed)

| # | Name / log tag | file:line | Trigger | Action | WO |
|---|---|---|---|---|---|
| 66 | WS-4 burn protocol / `LILY_BURN \| PENDING_BURNED`, `BURN` | `_burn_question` A:10461, `_is_burned` A:10499 | The answer has gone to air (reveal, leak, echo) | Question + normalized-text hash added to the burn set; blocks re-serve and steal (mech. 26) | WS-4 |
| 67 | N8 reveal-in-her-own-lane burn | A:742–746, `answer_already_aired` A:8160 | Reveal in the conversational lane | Burns the question | HOTFIX-006 N8 |
| 68 | Adult objection retire | `_burn_pending_adult_questions` A:10528–10556 | Objected-to adult material | Retired to the dead set — "can never re-air" | HOTFIX-004 |
| 69 | HOTFIX-004 D1 18+ consent floor / `AGE_CONSENT_DETECTED`, `ADULT_MODE_DECLINED` | A:934–938, A:6496 | Adult-deck request | Deterministic spoken-consent latch before the switch flips | HOTFIX-004 D1 |
| 70 | `on_child_gate_lost` / `CHILD_GATE_LOST` | A:10707 | Child-safety signal | Reverts mode | — |
| 71 | WS-11 garble gate / `GARBLE_TRIGGER`, `BAND_TRIGGER` | A:6300–6364; call A:7006 | Per-word confidences low | Refuses to score the final; checked **before** the band trigger | WS-11 |
| 72 | Clarify gates / `LILY_CLARIFY \| REQUEST`, `AMBIGUOUS_YES_BLOCK/CLEAR`, `NON_ANSWER_DROPPED` | `_maybe_fire_clarify` A:6228, `_maybe_fire_confidence_clarify` A:6293, `mark_pending_clarify` A:6365, `_resolve_clarify` A:6416, `clear_pending_clarify_for_question` A:4353 | Ambiguous/low-confidence answer | One clarify beat; `ambiguous_yes_blocks_start` A:2869 prevents a bare "yes" starting a round | WO-2, PATCH-003 |
| 73 | N3/N4 adjudication boundary / `ADJUDICATION_ABORTED`, `UNREGISTERED_WINDOW` | A:7237ff; N3 inv.2 question-id capture A:5780–5790; N4 candidate binding A:6552 | Adjudication entry | Question identity captured at window-open, never re-inferred; FL-1 judgment rides the candidate it was computed for | HOTFIX-006 N3/N4 |
| 74 | X3 no-reveal-without-delivery / `REFUSED_NO_DELIVERY`, `_delivered_to_playout` | A:7159ff; set A:5798–5804 | Reveal attempt | Refused unless the delivery reached playout | HOTFIX-005 X3 |
| 75 | X1 committed-score state block / X6 metadata q-stamp | A:176–200, A:1264 | Score narration | Committed score in the state block + logged; metadata stamped with its question number | HOTFIX-005 X1/X6 |
| 76 | N2 custom-round registration ledger / `LILY_CUSTOM_ROUND \| REGISTERED`, `TOPIC_EXHAUSTED` | A:713–738, A:3291–3500, A:3770ff | Custom/topic round | Registration is the only proof a custom round exists | HOTFIX-006 N2 |
| 77 | N9 late-answer / `LATE_MISS`, `LATE_CHECK_FAILED` | A:6919ff, A:8017 | Correct answer past the closed window | One warm announcement, one-shot | HOTFIX-006 N9 |
| 78 | X12 explain-on-request / verdict contest | A:926–930, A:6688–6740 | Player asks why / contests | One-shot conditioning notes consumed at A:5546–5550 | HOTFIX-005 X12 |
| 79 | Z3 no-match identity hold / `LILY_VOICE_ID \| NO_MATCH` + open probe | `identity_probe_outstanding` A:2879, `_no_match_awaiting_name_door` A:2906; stamps A:8788 (no-match), A:8804 (probe failure); release A:9165 (name door reported), memory landing, or `identity_no_match_hold_seconds` C:577 (default 180s) | Biometric probe returns NO_MATCH while the stated-name door is untried | The probe stays OUTSTANDING — every N1/Y9 hold surface (greet override A:3333, still-checking state note A:10638, false-clean-slate TTS rewrite via `can_claim_empty_memory`) remains in force; no line may characterise memory, positive or negative, until the name door reports, memory lands, or the bounded hold expires | HOTFIX-008 Z3 |

Also present and load-bearing but not separately numbered: the WS-5 MC answer-aborts-read pair
(`_note_mc_delivery_start` SD:915, `_mc_stem_protected` SD:937, `mc_early_answer_check` SD:963,
`_interrupt_current_speech` SD:950), the pre-window answer buffer (SD:798–911, whose rolling store is
cleared at claim time per PATCH-001 T5(a)), WS-14 room-discharge pacing (`open_window_after_discharge`
A:5690–5726), the G1 preemptive-generation switch (`set_game_live_preemptive` A:1391–1419,
`_resume_preemptive` A:1370–1389), and the `FALSE_INTERRUPTION` validation surface (A:14113–14120).

---

## 7. Two exact-match dedup guards, and the record path that defeats both

**Amendment 2026-08-10 (WO-LILY-HOTFIX-008 Z1): the itemless-fallback arm of this record path is
RESOLVED — by deletion, not by another guard.** What was deleted: the playout watcher's
last-assistant-text fabrication (`if not spoken and not had_items: spoken = game._last_assistant_text`,
HOTFIX-002's narrowing of an older unconditional `spoken.strip() or game._last_assistant_text`). At
1.6.8 an invalidated preemptive generation reaches the watcher ITEMLESS with `interrupted=True`; the
fallback fabricated the PREVIOUS committed turn — whose item lands in the buffer at generation commit,
BEFORE that turn's own playout record — so the phantom recorded that text marked `…[cut off]`
(record-only, no TTS) and the real turn's record then died on `record_agent_turn`'s verbatim-dup belt
against the phantom already in `sk.agent_turns`. Live: 20 phantom `…[cut off]` rows in
`lily-938EFF-2260354c` vs `PREEMPTIVE_INVALIDATED total=15`, each phantom REPLACING the real row and
riding her own context/repeat-lint window. Now an itemless handle records and publishes nothing (both
sinks no-op on empty text); a genuine barge-in (`had_items=True`) still records its real partial,
marked. The buffer itself is STAMPED: `_last_assistant_turn = (chat_item_id, text)`, written at
`conversation_item_added`; any generation-scoped reader MUST use `last_assistant_text_for(item_ids)`,
which returns `""` for any other generation — reading `_last_assistant_text` from a per-speech
callback is this bug class. Fixture: `test_hotfix008_z1_phantom_cutoff.py`. The trace below is kept
for the record; its "consume_post_tts_text falls back to RAW prose" and suppressed-AND-interrupted
consequences still stand (out of Z1's scope).

This is the structural finding that Deliverable 2 depends on, so it is stated here in full.

**Both duplicate guards compare for exact equality:**

- `air_dup_guard` (A:4799–4802): `return full in recent` — `recent` is `sk.agent_turns[-6:]`.
- `record_agent_turn`'s belt (A:2374): `if len(clean) >= 15 and clean in prior_turns[-6:]`.

Both therefore fail on:

1. **an appended sentence** — a re-air that says the same thing plus one more clause;
2. **whitespace differences** — because `lily_clean_for_speech` (mech. 37) collapses blank lines,
   the *aired* text has `\n` where the raw model prose had `\n\n`;
3. **anything under 15 characters** — the short-turn exemption both guards carry;
4. **paraphrase** — neither guard is semantic (mech. 45 detects paraphrase but is LOG-ONLY unless a
   re-air is in flight).

**And the record path writes suppressed turns as `[cut off]`.** Trace:

```
tts_node early-return  (mech. 22 / 23 / 24 / 25 / 36 / 48 / 50 / 51)
        -> yields 100ms of silence and RETURNS
        -> skipping note_post_tts_text  (A:12945, mech. 53)
speech watcher (A:14104-14108)
        -> spoken = assembled from handle.chat_items  (RAW pre-TTS prose, blank lines intact)
        -> interrupted = handle.interrupted ;  suppressed = suppressed or failed
on_agent_speech_finished (A:5403)
        -> consume_post_tts_text() finds NO bound text -> falls back to the RAW prose
        -> A:5404  `if interrupted or suppressed:`   ... releases claims
        -> A:5461  `if interrupted:`                 ... records the turn
record_agent_turn (A:2337-2414)
        -> its own dedup compares the RAW text against agent_turns (which hold CLEANED text) -> MISS
        -> writes `raw_text + " …[cut off]"` to lily_transcripts AND to sk.agent_turns
```

Three consequences:

- **`[cut off]` is appended to the full intended text, not to what aired** (A:2207, A:2405). The row
  contains everything the model produced; nothing marks where audio actually stopped. `segment_start`
  is never set for LILY rows, so the audible interval is unrecoverable from the table.
- **A turn that was correctly suppressed can still be recorded as spoken-and-cut** — including a turn
  cancelled by `cancel_speech` (mech. 35), which sets `_suppressed_speech_ids` *and* calls
  `handle.interrupt(force=True)`, producing `interrupted=True`.
- **The raw variant then poisons `sk.agent_turns`**, which is simultaneously (a) the repeat-lint window
  (mechs. 44/45), (b) the dedup window (mech. 24), and (c) per the comment at A:2367, *her
  conversational context*. So the pollution disables the very lint that would catch the next copy, and
  feeds the duplicate back to the model as something she has said.

---

## 8. The interaction chains that compose into defects

### Chain A — the WO's named chain, with line numbers

> "one path cuts a turn, another detects dead air and re-airs, a third suppresses the re-air as a
> duplicate, a fourth recovers it."

```
[1] CUT
    Barge-in / false interruption / mid-stream TTS failure
    -> session watcher A:14104   interrupted=handle.interrupted
    -> on_agent_speech_finished A:5404
         releases the owner's claims          A:5405-5415   LILY_SAY | RELEASED | reason=interrupted
         re-arms expect_delivery              A:5443        (unless mech. 28 confirmed instead)
         ARMS THE RE-AIR GATE                A:5448-5449   arm_reair_gate()
         ARMS CUT RECOVERY                   A:5456-5457   arm_cut_recovery(spoken_text)
         records the turn as "[cut off]"      A:5461-5469

[2] DEAD-AIR DETECTION -> RE-AIR   (four independent producers, all reachable from one cut)
    (a) cut-recovery watchdog   SD:628-636 -> SD:614-626  trigger_cut_recovery()
                                 fires after cut_recovery_grace() iff SD:593-612 still sees silence
                                 dispatches via instructed_reply -> BYPASSES gated_say's mechs. 5-11
    (b) WS-2 reconcile refire   A:4277-4304  UNDELIVERED_REFIRE  (idle watchdog, A:3999)
    (c) delivery nudge          A:5640-5677  DELIVERY_NUDGE      (2 finished agent turns)
    (d) stale-claim watchdog    SD:299-319   STALE_CLAIM_RELEASED -> re-gated_say

[3] SUPPRESSION OF THE RE-AIR   (four independent suppressors in tts_node)
    (a) regen gate              A:12640-12671  LILY_REGEN_GATE           <- consumes the mech. 20 arm
    (b) air dup guard           A:12910-12932  DUP_TURN_SKIPPED|path=air
    (c) delivery duplicate      A:12833-12848  reason=dup|act=question_delivery
    (d) transition duplicate    A:12892-12908  reason=dup_transition

[4] RECOVERY OF THE SUPPRESSED RE-AIR   (each closes the loop back to [1] or [2])
    (a) regen gate's own re-generate   A:12660-12664  generate_reply(_REGEN_REAIR_DIRECTIVE)
    (b) empty-candidate retry          A:12741-12747  generate_reply()  -- and A:12716-12722 states
                                       outright that without releasing claims first, "the retry
                                       regenerates into a gate that suppresses the redelivery as a
                                       duplicate"
    (c) stale-claim watchdog           SD:317-319     re-gated_say(same instructions)
    (d) lobby empty-STOP recover       A:12412-12440  releases the claim, re-dispatches the opener
    (e) THE RECORD PATH                A:5461-5469 -> A:2374 (dedup MISSES on the raw variant)
                                       writes the suppressed copy into sk.agent_turns, which is
                                       her context AND the lint window for mechs. 24/44/45
```

**Why it closes into a cycle rather than settling:** step [4e] guarantees that the suppressed copy
becomes an input to step [3]'s detectors *in the wrong form* (raw, not cleaned), so the next
generation's exact-match dedup misses, the re-air airs, and the loop advances one turn. Only mech. 23
(stubborn-repeat, A:12672–12709) breaks it, and only after the room has already heard the content
twice — its own comment names the live case: *"the same cut greeting aired up to FOUR times"*
(A:12677–12681, `lily-A070E8`).

### Chain B — ARMED_LIMBO ↔ SECOND_LANE_REFUSED (fixed, documented in situ)

```
adjudication dies silently between the answer commit and the reveal publish
 -> the transition is journaled but no stage ever reaches air
 -> ARMED_LIMBO (A:3956-3977) sees delivery CONFIRMED + window closed + candidates waiting
      -> forces adjudicate() every 2 ticks (~20 s)
 -> open_question_transition (A:1638-1664) sees "a journal exists for this question"
      -> SECOND_LANE_REFUSED every ~20 s
 -> permanent dead-air ping-pong  (named in situ: lily-1C53C6, A:1630-1631, A:3970-3972)
```

Broken by `reclaim_unaired` (A:1639–1655) plus `_transition_reached_air` (A:1592–1612). **The cycle is
only broken for the recovery caller** — any other lane still hits the unconditional refusal, so the
fix is a special case, not a removal of the cycle.

**Reopened 2026-08-10 through the AIRED-verdict gap (lily-938EFF-2260354c), resolved at source
(HOTFIX-008 Z2c, mech. 80).** `reclaim_unaired` assumed a stuck transition never aired — but an
adjudication that dies AFTER its organic verdict is confirmed on the air (the 03:43:38 task died
inside the reveal-publish await; CancelledError skips the CRASHED log) leaves a journal
[reveal, verdict] that `_transition_reached_air` correctly calls aired, so recovery hit
SECOND_LANE_REFUSED forever while the confirmed delivery claim reopened the dead question's
window every agent turn (12× in 4.5 min). The circular wait: the beat's completion marker
(next_delivery) needed supply; supply recovery needed idle; idle needed the beat closed. Z2c
closes it WITHOUT touching this chain's guards: a narration-complete transition is RESUMED by the
recovery caller (bookkeeping only, nothing re-airs) and RELEASED when there is nothing to deliver
(terminal next_delivery marker, delivered_q=None), and a ruled question's window can no longer
reopen off its delivery claim (`REOPEN_REFUSED_TERMINAL`). Both cycle directions are now pinned by
fixtures: aired-organic (releases) and never-aired (still reclaims).

### Chain C — re-air gate ↔ N12 transition dup gate

`register_transition_narration` must exempt the verdict-key holder because *"its re-air after a
cut/regeneration … legitimately carries fresh words"* (A:1790–1793). So mech. 51 contains a hole
carved specifically for mech. 20's output. Conversely, mech. 20's regeneration directive
(`_REGEN_REAIR_DIRECTIVE`, SD:27–32) instructs *fresh phrasing*, which is precisely the input that
makes mech. 51's "differently-worded second narration" detector fire, and precisely the input that
makes mech. 24's exact-match dedup miss. **The two mechanisms' contracts are mutually
counter-productive: one demands new words, the other treats new words as the defect signature.**

### Chain D — regen gate ↔ stubborn repeat ↔ record path

```
cut -> arm_reair_gate -> re-dispatch
  -> regen gate fires (repeat lint tripped)         A:12640
      -> claims released, silence yielded, generate_reply(fresh words)
      -> the SUPPRESSED handle is still recorded as "[cut off]"  (§7)
          -> sk.agent_turns now holds the RAW variant
              -> the repeat lint (A:12608) compares the next CLEANED text against it -> MISS
                  -> the copy airs
                      -> the next cut re-enters at the top
  -> if the regen retry also repeats: stubborn-repeat suppression  A:12672  -> floor yielded
```

### Chain E — progression pauses vs. the pauses that must not apply

`progression_paused_reason()` (mech. 14) has six states. Three consumers read it **unfiltered**
(A:1888, A:3924) and two read a **two-state subset** (SD:146–150, SD:398–402). The narrowing exists
because the unfiltered predicate deadlocked against mechs. 6 and 5:

- `question_pending` refused *the delivery nudge that resolves question_pending*;
- `host_speaking` refused *the nudge that fires as her own turn ends* — i.e. it can never be false at
  the moment the nudge is dispatched from `on_agent_speech_finished`.

Both are self-blocking loops (A:141–145, SD:390–397). The watchdog's `WATCHDOG_PAUSED` (mech. 15)
still uses the unfiltered predicate, so **`host_speaking` alone parks the entire recovery ladder for
the duration of any Lily turn** — including the ladder that exists to recover a Lily turn that never
finished.

### Chain F — cut recovery bypasses the dispatch gate that would stop it

**CLOSED 2026-08-09 by WO-LILY-HOTFIX-007 Y10 (FLOOR-001 counterweight).** The analysis below stands
as the diagnosis; the two bindings that close it are recorded after it.

`trigger_cut_recovery` (SD:625) calls `self.instructed_reply(...)`, not `gated_say`. So mechs. 5–11
(hold, question-pending, P0-G, P8 game-lane, dup-claim, stale-claim) never run on the auto-resume.
Its only pre-conditions are the five checks in `_cut_recovery_should_fire` (SD:593–612). A cut that
lands while a hold is active therefore produces an auto-resume that the hold was specifically
designed to prevent — the hold check happens in the *watchdog* (A:3882) but not in the *cut-recovery
watchdog*.

**Y10 disposition (two bindings, no new gate):**

1. `trigger_cut_recovery` now dispatches through `gated_say(None, "cut_recovery", …,
   source="cut_recovery")`, so mechs. 5, 6, 7, 9, 10 run on the auto-resume like every other
   *code-dispatched game/recovery* turn. Keyless by design: no claim, so mechs. 4/8 (dup-claim,
   stale-claim watchdog) stay out of it — a refused resume is silence, not a queued retry.
   `cut_recovery` is deliberately NOT in `_HOLD_EXEMPT_SOURCES`. Side effect: the resume now consumes
   its own re-air arm (mech. 20) instead of leaving it for the next code dispatch, which could
   previously hand the cut-short-mid-question directive to a question that was never cut.

   **This is NOT a claim that every outbound lane is funneled.** Still outside `gated_say`, all
   pre-existing and out of Y10's scope: the late-recognition ack (`A:2154`, gated instead by
   `late_recognition_blocked_reason()`), four player-photo/vision reactions in the entrypoint
   (`A:14582`, `A:14602`, `A:14621`, `A:14642`) — those five are all user-initiated — and the
   tts_node regeneration / empty-candidate retries, which re-enter through raw
   `session.generate_reply()` (mechs. 22, 48). Chain F was the *machine-into-silence* bypass; these
   are not, and consolidating them belongs to the WS-6 tranche in §10.
2. `_cut_recovery_should_fire` reads the new derived `LilyGame.floor_state()` and returns False
   (`LILY_FLOOR | RECOVERY_YIELDED`) when the ROOM holds the floor or a hold is active — the graded
   silence choice. `floor_state()` adds no state: it derives `hold` / `lily_speaking` /
   `player_speaking` / `open_floor` from `_hold_active`, `sk.host_speaking`, `_question_pending`
   and FL-1's `last_addressee_judgment` (whose judgment had until then gated nothing — it was
   consumed only as build_state_block context lines). A HOST_DIRECTED judgment deliberately does not
   yield: the responsiveness budget for a direct address is canon-correct.

Both refusal paths (floor yield and a gated dispatch) run one shared stand-down —
`_stand_down_cut_recovery` — which clears the cut's re-air arm (so mech. 20's arm can never leak to
an unrelated later dispatch) and releases the `_awaiting_address_since` latch (mech. 58). The latch
release is load-bearing: that latch is cleared only at real playout (mech. 12), so a reply that died
before playout plus a stood-down recovery would otherwise leave `progression_paused_reason()`
returning `address_unanswered` forever, and mech. 15 would `continue` past the entire recovery ladder
on every tick. The pre-Y10 code got this for free from the chain-F bypass itself (the resume always
fired, and its playout cleared the latch).

The floor yield is IN-GAME ONLY: pre-game the idle watchdog does not run (A:3899), so a stood-down
lobby/intake resume would be unrecoverable dead air. A pre-game hold still refuses one layer down, at
the dispatch gate.

Not changed by Y10, deliberately: the game-lane recovery producers (mechs. 29, 30, 59, 60) still
progress the game while the table talks — on game beats the floor IS hers, and yielding those would
park the night. §10's consolidation notes are otherwise untouched.

### Chain G — WS-2 ↔ WS-6 ordering dependency

`no_stuck_claims()` (mech. 31) gates WS-6's fallback (mech. 32) on a signal (`_undelivered_refires`)
that only exists because the natural signal (`_undelivered_ticks`) resets inside the same synchronous
call that crosses its own threshold (A:4114–4126). A supply fallback and a delivery refire are
therefore serialized through a counter whose observable lifetime had to be hand-engineered.

---

## 9. Tally

**By kind:** GATE 31 · WATCHDOG 8 · RECOVERY 15 · SUPPRESSION 9 · LINT 5 · REWRITE 6
(some mechanisms count in two kinds; the 79 rows are the authoritative count).

**By owning WO:**

| WO / defect tag | mechanisms |
|---|---|
| HOTFIX-001 (stale claim / Krisp wedge) | 3, 4, 12 |
| HOTFIX-002 (transcript blindness, dup rows) | 24 (predecessor), 52-adjacent belts at A:2352–2380, A:2625, A:14088–14098 |
| HOTFIX-004 (adult consent) | 68, 69, 70 |
| HOTFIX-005 X1/X3/X6/X12 | 74, 75, 78 |
| HOTFIX-006 N1/N2/N3/N4/N8/N9/N12 | 19, 26 (N3/N8), 38 (N1), 51, 56, 57, 67, 73, 76, 77 |
| PATCH-001 T1/T2/T3/T4/T5/T6/T7 | 17, 24, 30, 34, 35, and the pre-window buffer clear SD:761–769 |
| PATCH-002 A4/A4a/A4b/A5/M4/T12 | 5, 10, 11, 33, 45 |
| PATCH-003 P1/P4/P5/P6/P7/P8/P9/P10 | 6, 9, 41, 43, 58, 59, 72 |
| WS-1 … WS-14 (OMNIBUS-003 / STREAM-INTEGRITY-002 / AMENDMENT-001/002) | 13, 18, 20, 21, 22, 23, 26, 30, 31, 32, 49, 64, 65, 66, 71, and WS-14 pacing A:5690 |
| P0-A…P0-G, P0-1…P0-5 (BE8D8B / 9337B1 live) | 7, 14, 15, 16, 18, 38, 40, 50, and the P0-4 seam A:5576–5581 |
| G1 (DESYNC-HONESTY-001) | preemptive switch A:1370–1419 |
| desync WO (Sub-agents B/C/E) | 13, 29, 43, 48-adjacent, honesty note lifecycle A:5527–5545 |
| 2026-07-15 stall class | 54, 55, 60, 61 |
| lily-1C53C6 deadlock | 55, 56 |
| HOTFIX-008 Z2 (2260354c supply starvation) | 79 (extends 32, 61) |
| HOTFIX-008 Z2c (2260354c transition deadlock) | 80 (extends 55, 56, 57) |
| HOTFIX-009 W6 (5E3036 timer self-cancel + burned re-reveal) | 81 (extends 80; gates 56's reclaim on burn) |
| lily-2C489B triple re-read | 28 |
| lily-A070E8 quadruple greeting | 23 |

---

## 10. Consolidation notes (for the mandate this document is a prerequisite for)

1. **Four re-air producers and four re-air suppressors, none of which know about each other.**
   The producers (Chain A step 2) and suppressors (Chain A step 3) share only the
   `SpeechActRegistry` and `sk.agent_turns` — neither of which carries *provenance* (which producer
   emitted this attempt, and how many times).
2. **Exact-match dedup cannot survive a regeneration mandate.** Mech. 20 requires fresh words; mechs.
   24, 25 and the record belt at A:2374 all compare for equality. These are irreconcilable as
   written; consolidation must move dedup onto the *act identity* the registry already has, not onto
   the text.
3. **The record path must not write suppressed speech.** `if interrupted:` at A:5461 needs to be
   `if interrupted and not suppressed:` in effect, and the `[cut off]` marker needs a
   playout-truth source (the `_playout_started_ids` ledger at mech. 12 already has one) instead of
   `handle.interrupted` alone.
4. **`note_post_tts_text` must be unconditional.** Every early return in tts_node currently produces
   a row containing pre-TTS prose that the room never heard.
5. **`progression_paused_reason` should be split, not filtered at call sites.** Chain E shows the same
   predicate meaning three different things at three call sites; two of the six states are
   self-blocking for the acts that resolve them.
6. **`RETIRE_WITH_WS6` markers already name the intended terminus** for mechs. 17, 24, 30, 33, 34, 35
   and the hold/STOP primitives — the journal reducer's leases plus event-sourced question state.
   Nine mechanisms carry that marker; they are the consolidation's first tranche.
