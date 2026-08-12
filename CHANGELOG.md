# LILY — Changelog

Reverse-chronological work-order, RFI, and fix entries for the Lily agent,
split out of README.md on 2026-07-31 (dated sections moved verbatim —
nothing removed or truncated). New dated/WO entries are appended at the
TOP of this file. Living documentation lives in [README.md](README.md).

## 2026-08-12 — WO-LILY-LIVEFIRE-001 CLASS 8: hygiene sweep

Fixture `lily-639007-f80aa6bf`.

- **8a**: `entrypoint` opener is now an explicit fallback gated on
  `say_registry.state(...) is None` (greet + rejoin) — on_enter is the single
  primary source; the anti-double-greeting is no longer load-bearing on
  say-gate dup suppression.
- **8b**: `tts_node` gates the `YIELD_AFTER_QUESTION` clip on `n_questions >=
  2` — a single question with a declarative tail is preserved (the live
  82-char cut after name-bind); the yield function itself is unchanged.
- **8c (flagged)**: lobby-vamp stacking is LLM-generated from the lobby
  directive + greeting options, no spine double-emitter — flagged with
  7b/7c/7d for persona-prompt review + live verification.
- **8d (report)**: 6a stands — prefetch timeouts (generation latency) vs
  memory warnings (fixed 120s monitor, static RSS baseline) are two
  independent problems; memory not tuned under this WO.
- Tests: `tests/test_livefire_class8_hygiene.py` (5). Suite green (2453).

## 2026-08-12 — WO-LILY-LIVEFIRE-001 CLASS 7: recognition latch (7a; 7b/7c/7d flagged)

Fixture `lily-639007-f80aa6bf`.

- **7a (shipped)**: `_game_start_committed` latch set at the `start_game`
  commit (distinct from `game_started` = "greeting dispatched"), persists
  across recycle. `late_recognition_blocked_reason` returns
  `game_start_committed` once set; `maybe_fire_late_recognition` RETIRES the
  beat (fired=True) rather than deferring it — the live act=game_start
  "welcome back... relaxed pacing?" that suppressed q_1's kickoff can no
  longer air. Double-greeting / second-welcome-back already closed by
  `ce76d06`/`6f31b26`. Tests: `tests/test_livefire_class7_recognition_latch.py`
  (3); 59-test recognition suite green; full suite 2448.
- **7b/7c/7d (flagged, NOT shipped)**: name double-ask (7b), fun-fact "still"
  demand without this-session provenance (7c), and the "say start" second gate
  after category+play (7d) are LLM-generated from the intake /
  `greeting_instructions` / `prompts/lily_system.txt` persona directives. A
  spine-authoritative fix needs persona-prompt review + a LIVE intake run to
  verify against the recognition suite; the deploy path is blocked this
  session (no live surface), so rushing fragile prompt-note layering into the
  heavily-tested intake was declined. Called out for an operator follow-up WO.

## 2026-08-12 — WO-LILY-LIVEFIRE-001 CLASS 6: named-category supply

Fixture `lily-639007-f80aa6bf`.

- **6a diagnosis — TWO independent resource problems.** 5 PREFETCH_FAILED
  TimeoutErrors are question-driven generation latency (17:54:15, 17:56:50,
  17:57:30, 17:58:12, 17:59:21 UTC, LILY_REASONING). 5 memory warnings fire on
  a FIXED 120s livekit.agents monitor (17:54:00, 17:56:00, 17:58:00, 18:00:00,
  18:02:00 UTC) — static RSS baseline crossed 8s post-connect (ECAPA pool
  preload), held all session, last one after play ended. Series do not align;
  the timeouts are latency, not memory. Memory tuning out of scope (see 8d).
- **6b/6c** (`lily_agent._bank_to_supply`): an operator topic whose bank is dry
  gets one fresh generation attempt (`prefetch_question(from_bank=None)`)
  before any flip — `LILY_CUSTOM_ROUND | TOPIC_BACKFILLED`; a dry bank/timeout
  is not "round over".
- **6d**: the flip's honest templated note is set AT the flip transition, so it
  survives the fixed-rotation-also-empty early return that previously dropped
  it — the "silent flip to academic" is closed.
- **6e** (`lily_reasoning._shape_question`): the model-supplied `q_<4 digits>`
  id (reused across generations — the q_4821 collision) is overridden with a
  process-unique `q_gen_...` id at allocation.
- Tests: `tests/test_livefire_class6_named_category.py` (4);
  `test_hotfix006_category.py` + `test_reasoning_schema.py` updated to the new
  contracts. Suite green (2445). Closes the Class-6 "done when": a named topic
  survives past 5 questions (backfill) or flips with an honest templated
  explanation; no silent category flip in code.

## 2026-08-12 — WO-LILY-LIVEFIRE-001 CLASS 5: resume is a command

Fixture `lily-639007-f80aa6bf` ~14:01: "Continue with Greece, dude. Make more
fucking questions." missed the anchored `^continue$` regex — sticky stayed
True, addressee filed it side_chatter, the LLM narrated an unexecuted resume,
the feed went quiet.

- `lily_scorekeeper`: `_RESUME_INTENT_RE` widens `lily_detect_resume_game` to
  find resume intent anywhere in the utterance (continue / continue with X /
  keep going / make more questions / resume + existing family),
  negation-guarded. The command path owns it before addressee routing (5c).
- `lily_agent`: `resume_game_delivery` clears sticky + releases hold
  atomically (5c) and dispatches the Class-4-preserved armed card via the
  deterministic sheet (`dispatch_armed_question(source="resume")`, 5d) so the
  spine — not the LLM — restarts delivery.
- Tests: `tests/test_livefire_class5_resume_command.py` (5). Suite green
  (2441). Closes the Class-5 "done when": the resume line clears sticky and
  delivery restarts; no narrated-but-frozen state is reachable.

## 2026-08-12 — WO-LILY-LIVEFIRE-001 CLASS 4: STOP is a brake, not a substring

Fixture `lily-639007-f80aa6bf` 17:59:55: "Why why why stop? Why?" fired the
stop primitive (bare stop, solo room) and the freeze burned kb_457 (next
Greece card).

- `lily_scorekeeper`: `_STOP_META_RE` guard in `lily_detect_stop` — an
  interrogative/reason lead-in before the stop-word ("why stop", "why did you
  stop", "did you stop") is meta, never the brake; polite imperatives still
  fire; "stop, why…" is untouched.
- `lily_agent`: `_freeze_game_delivery_for_stop` no longer burns/nulls
  `armed_question`/`next_question` — STOP freezes supply (sticky, closed
  window, cancelled prefetch) but the armed and prefetched cards survive and
  deliver on resume. Adult consent-burn unchanged.
- Tests: `tests/test_livefire_class4_stop_brake.py` (6);
  `test_patch002_hold_stop.py` updated to the freeze-not-burn contract; the
  51 barge-in contract tests stay green. Suite green (2436). Closes the
  Class-4 "done when": the "Why…stop?" line does not trigger the primitive;
  an armed card survives a genuine STOP.

## 2026-08-12 — WO-LILY-LIVEFIRE-001 CLASS 3: session write truth

Fixture `lily-639007-f80aa6bf`: 5 delivered Greece questions, but q6 was
armed, burned by a STOP, and never asked — `question_number` reached 6 and
the winner write said "over 6 questions".

- `lily_agent`: new `LilyGame.questions_asked_count()` = `len(asked_history)`
  (delivered mirror; burned/discarded dropped at release so they never
  increment). `finish_game` `questions_played`, the finish-time and
  shutdown-path `lily_write_session_memory` calls now use it; the live
  checkpoint keeps `question_number` as its resume cursor.
- Tests: `tests/test_livefire_class3_session_write_truth.py` (4). Suite green
  (2430). Closes the Class-3 "done when": session close writes
  "over 5 question(s)".

## 2026-08-12 — WO-LILY-LIVEFIRE-001 CLASS 2: one speech owner after verdict

Fixture `lily-639007-f80aa6bf`. 2a (organic double verdict) is owned by the
data-side `StopResponse` landed in `000bfc2`; 2b (reveal/delivery fusion)
is fixed here — the 17:59:34 reveal fused the q_5 delivery into one utterance
("Crete… Next up. What… agoge?").

- `lily_say_gate`: new pure `lily_clip_delivery_from_reveal` /
  `_is_delivery_sentence` split a fused reveal+delivery turn at the first
  sentence that opens the next question; a pure delivery turn (question at
  sentence 0) is never clipped.
- `lily_agent`: new `LilyGame.transition_awaiting_delivery()` (reveal+verdict
  journaled, next_delivery not yet); `tts_node` clips a fused question in
  that window (`LILY_SAY_SUPPRESSED | reason=reveal_delivery_fusion`). The
  real delivery fires via the post_reveal seam, which journals next_delivery
  before its tts_node and so reads False here.
- Tests: `tests/test_livefire_class2_reveal_delivery.py` (7). Suite green
  (2423). Closes the Class-2 "done when": one ruling per question; no reveal
  content inside any delivery utterance.

## 2026-08-12 — WO-LILY-LIVEFIRE-001 CLASS 1: spoken score = ledger only

Fixture `lily-639007-f80aa6bf`. The 17:58:45 reveal aired "you're at three,
streak of three" over a committed ledger of 2/2 — the model narrated state
one answer ahead of the spine, and the X1 detector only logged it post-hoc.

- `lily_scorekeeper`: committed rows carry `streak_after`; new
  `ledger_streaks()` read-only authority (parallel to `ledger_scores()`);
  new pure `lily_score_line_gate` / `lily_ledger_score_line` /
  `_sentence_narrates_state` covering her full score grammar (1c).
- `lily_agent.tts_node`: Class-1 gate after `lily_clean_for_speech` —
  suppresses any sentence narrating a total/streak/count and re-emits ONE
  template line from the ledger authority (`LILY_SAY_SUPPRESSED |
  reason=score_divergence`). Suppress-and-reemit, never in-place rewrite;
  fires only with a live ledger.
- Tests: `tests/test_livefire_class1_score_ledger.py` (8). Full suite green
  (2416). Closes the Class-1 "done when": the Athens 3-vs-2 line cannot air.

## 2026-08-12 — WO-LILY-HOSTLOOP-001: host-loop overhaul, all clauses closed

Full clause-by-clause detail in README.md ("Host-loop overhaul") and the
per-clause entries below/in git history. Summary of dispositions:

- **C3+C4** (`57e2ea4`): window arms at core-sentence; answer-shaped
  barges bind (NATO/letter/text via existing matchers); non-answer
  barges resume from the interrupted choice; C3d invariant pinned; the
  pre-window buffer requires answer shape. Walked example in the C3+C4
  commit message (778735a). KNOWN LIMIT reported, not improvised:
  Session A's q6 was FREEFORM (kb_428) and the WO's own "any content
  answer" definition still admits fragments there — a
  fragment/completeness rule needs its own clause.
- **C5+C6+C8** (this merge): single-flight composites (keyless beats
  carry a flight token; preempt-or-queue, never race); deterministic
  ≤1.5s spoken receipt via gated_say(text=) → session.say → tts_node
  (all gates kept, three structural receipt sheets, never a verdict
  word on uncertain, composite told the receipt aired); barged verdicts
  re-air one line keyed off the RELEASED claim (un-wedges N+1).
- **C7** (`c44aca2`): bare start intent (utterance-shaped) fires;
  recovery never re-greets past a start; rejoin reserved for genuine
  reconnects.
- **C9** (`9e45fb1`): board-on-playout-start behind
  LILY_BOARD_ON_PLAYOUT_START (default true; archaeology: gating itself
  shipped with the lily-2C489B fix).
- **C11** (`29187ea`): audEERING behind LILY_AUDEERING_ENABLED (default
  false); pipeline still registers so the child gate fails closed in
  sensor mode; no code deleted.
- **C12** (`78414a0`): settle-on-event fixes the 100% preemptive
  discard; survival measurable at INFO deploys.
- **C13** (`e48bf1e`): hold-equivalents ("hold on"/"wait"/"pause"/"one
  sec") halt within one utterance, utterance-shaped only; a beat, not a
  brake. W8/W2/V4 audited first — nothing duplicated.
- **C14a** (REPORT-ONLY, confirmed live): lily_answers.question_id
  (text: kb_428/q_4821) does not join lily_questions.id (bigint).
  Proposed migration for the operator: nullable bigint question_ref
  populated by the writer for kb_/bank ids + index; text column stays
  for generated ids. NOT executed.
- **C14b** (`e968c7d`): question_timeline (core_sentence_spoken_at,
  delivery_confirmed_at, window_opened_at/closed_at, reopens separate)
  persists via lily_sessions.metadata — no DDL.
- **C10** (READ-ONLY, delivered as report): two unreconciled text lanes
  — the LLM's context receives raw pre-gate text on every turn
  (silence frames count as playback), the room receives tts_node
  truth; four file:line-mapped defects incl. cancel_speech's
  suppressed-then-interrupted override writing phantom transcript
  rows, and the record-path dup guard lacking the air-path's delivery
  exemption. The divergence safety nets audit Lane A (the phantom).
  Recommended as the next work order. No fixes under this clause.
- **C15** honored: zero STT surface touched. **C16** honored: no
  features beyond clauses; .env greps files_with_matches only.
- **C17** deviation, disclosed: implementation order differed (C12 and
  the small clauses landed while C3+C4 was in its worktree) — every
  clause still integrated and verified in sequence on main.

Suite over the WO: 2305 → 2390 green (85 new tests; the 133-pin
character suite untouched). Sub-agent slots used: 2 (C3+C4 implementer;
C5+C6+C8 implementer) + the read-only C10 investigator.

## 2026-08-12 — WO-LILY-HOSTLOOP-001 C12: preemptive waste diagnosed and fixed (settle-on-event)

Evidence (Session B lily-05BB92, first instrumented session): preemptive
invalidated=10, used=0 — 100% discard, full LLM+TTS latency (~2.4s) paid
on every turn. **Diagnosis against the WO's two hypotheses:** the
equality check is NOT too strict (word-level, punctuation/case-blind —
verified in installed agent_activity), and the trigger is NOT premature
(STT final p50 1.57s beats the 1.82s turn commit, so the last
speculation sees final words). The cause is a third thing: ASYNC game
events mutate the STABLE state block between speculation and commit —
a prefetch landing flips "next question: ready", adjudication commits
scores — so the framework's ChatContext.is_equivalent correctly fails.
The volatile-tail split cannot see these: they are stable-block changes
by design; the game just moves asynchronously.

**Fix (the cheap one):** settle_context_nowait() refreshes the agent's
persistent ctx the moment stable inputs change — the same idempotent
_apply_context_blocks the turn hook uses — wired through the existing
publish_attributes_nowait chokepoint (27 state-change sites) plus the
two async supply-landing sites that bypass it. Speculation then
snapshots current state; commit finds nothing new. Reproduced offline
with the framework's real is_equivalent: invalidates without settle,
survives with it (tests/test_context_settle.py, 5 tests). Events landing
after speculation still invalidate — correctly — and the counter
measures the residue.

**Survival made measurable (C12's success bar):** the framework
announces a USED speculation only at DEBUG — at a production INFO
deploy the record never existed, so `used` read 0 by construction.
enable_preemptive_used_capture() creates those records for the tap to
count while shielding every root handler from the debug flood (output
unchanged; only counting changes). Success reads from the next
session's `preemptive` block: used > 0.

Deletions: none — the volatile split handles per-turn churn, settle
handles event-driven stable churn; the Y2 counter arbitrates if either
becomes redundant. Suite 2299 → 2305 green.

## 2026-08-09 — Transcript sync: the glass shows the line when she STARTS saying it

Live report: "the sync between what is being said and what is being
displayed is off." **Archaeology:** the glass received Lily's turn ONLY
from on_agent_speech_finished — playout COMPLETION — so a long read
showed nothing while she spoke, then the whole paragraph landed late.
The UI was already correct (segment-id upsert, finals lock; verified in
agent-session-block.tsx) — it was simply never sent an interim. The
aired text is knowable at playout START because tts_node binds the
exact TTS input to the speech handle BEFORE synthesis (Y5's
construction).

**Fix (agent-side by necessity — the UI cannot display text it has not
received):** note_playout_started publishes the bound text as an
INTERIM segment (same lk.segment_id = speech id, final=false) the
moment audio starts; the existing completion publish (final=true, with
the "…[cut off]" marker when interrupted) replaces it in place. The
interim path PEEKS the binding (new peek_post_tts_text) — Y5's one-shot
consume still belongs to completion, so the durable record is
unchanged. Turns with no binding (queued audio, SFX) publish nothing at
start; completion still covers them.

**Deletions:** none — one accessor, one guarded call, a `final=` param
on the existing publisher. Suite 2078 → 2082 green.

## 2026-08-10 — WO-LILY-HOTFIX-009 W6: the timeout adjudication survives its own timer; a burned question never re-reveals

P0. Live `lily-5E3036-b56b5eb4` (RAN ON the deployed 4e902cb tip —
HOTFIX-008 was live, contrary to the WO's stated assumption), q4 kb_491
Marie Curie: organic reveal 05:34:35 ("Pass locked. Marie Curie" — no
verdict cue, so `lily_verdict_narration` matched nothing and no burn was
written), steal-timer adjudication 05:34:52 journaled `reveal` and died
silently, ARMED_LIMBO recovery 05:36:42 hit `RECLAIMED_UNAIRED` (the
[reveal]-only journal carries no air-proof) and RE-NARRATED the beat —
"Nobody landed it. Marie Curie." aired 05:36:51, 2m16s after the answer
went to air, with the q5 diamond delivery landing on top of it 4s later.

Root cause, closing the Z2c changelog's open question of WHO cancelled
the 938EFF adjudication inside the reveal-publish await: adjudicate
cancelled ITSELF. A window-timeout adjudication runs INSIDE the timer
task (`open_window._expire` awaits `adjudicate`), and adjudicate's own
head unconditionally cancelled `self._window_timer` — the task it was
running in — so CancelledError landed at the first await (the reveal
publish gather). Every timeout adjudication that reached an await died
there; the 05:34:47 steal branch survived only because it returns before
any await. Fix 1: the timer is not cancelled when it IS the current
task. Fix 2 (mech 81, the Z2c residue): recovery re-entry over a
narration-PARTIAL transition of a BURNED question — burn being the
one-way "answer went to air" state every other seam already gates on —
resumes as bookkeeping (`RESUMED_BURNED`: missing stages journaled
`source=already_on_air`, which is mech 56 air-proof, so the Z2c release
seams work), never re-narrates. An UNBURNED dead journal keeps the
1C53C6 reclaim unchanged — there nothing aired and re-narration is the
cure. Draw-side verified, no code: `lily_asked_history` records Marie
Curie exactly once; burn was already authoritative at draw, arm, steal
and reconnect — the transition owner was the one seam that never
consulted it. Fixture `tests/test_hotfix009_w6_burned_reserve.py` from
the real session rows; needles proven to bite on 4e902cb.

## 2026-08-10 — WO-LILY-HOTFIX-009 W1: an auditable path to reverse a wrong verdict

P0 LEAD DEFECT. Live `RM_RQTZZanrHURF` 05:37→05:41: Rami answered the
diamond question ("hardest naturally occurring substance") correctly on a
RELAXED round, Lily denied it on a clock/window that should not have been
running, then — even after conceding both the rule violation and the
correct answer — held him at two: "Score stays two because the board
already locked" (05:39:02). The contest that followed ("you violated the
rules we agreed on… and now you're gonna keep my score at two") earned an
apology and no fix.

Root cause (logical/contract, not plumbing): every ledger mechanism was
built to stop the score being FABRICATED (the WS-4 idempotency belt, the
Z1 dup belt, board-lock). Nothing was built to let it be CORRECTED.
Immutability was the right instinct against invention and the wrong
instinct against injustice. The HOTFIX-005 X12 verdict-contest path
already detected the contest and injected a directive to "make it right
via your scoring tool" — but no such tool existed: the only writable
scoring tool, `lily_award_bonus`, mints a free bonus with no reference to
the wrong ruling.

Fix — the correction concept, added without loosening the ledger:
- `LilyScorekeeper.correct_verdict()` — the sole writer of a new
  `"verdict_correction"` cause. It REQUIRES a prior `"answer"` verdict row
  to amend, refuses grounds outside the closed set (`wrong_rule`,
  `answer_denied`, `misheard`, `out_of_window`), refuses a second
  correction of the same verdict, and requires a positive restoration
  against an actual denial (a spurious contest on a correct answer cannot
  stack a point). This is the line that separates a correction from a
  fabrication: a correction can only amend a ruling that happened.
- HARDEN (fix-loop): grounds="answer_denied" — the highest-risk ground — is
  MECHANICALLY corroborated, not LLM-asserted: the recorded attempt must
  fuzzy-match the canonical answer through the EXISTING Tier-1 matcher
  (lily_evaluation.lily_tier1_evaluate); no match ⇒ refuse. Two legs feed the
  corroborating attempt: (1) in-session — the denied row's own transcript (a
  recorded-but-mis-ruled correct answer); (2) FALLBACK (Option B) — a scoped
  session read of lily_addressee_log (answer_window_open + fuzzy_matched_answer)
  for the IGNORED-answer class (the diamond: the correct "diamond" was rejected
  by one-candidate-per-player and survives only there, id=819). The same Tier-1
  matcher re-checks each fetched transcript against canonical; the table has no
  question_id, so the match IS the question association. DB read failure ⇒
  honest "evidence unavailable" refusal, never a restore. A rightly-denied WRONG
  answer can no longer be restored on any leg. The unreachable negative-reversal
  branch was deleted (restoration-only); negative reversal is named as a future
  surface. Residual: only misheard / out_of_window grounds remain caller-judged.
- Corrections are APPENDED, never overwritten — the original row stands, a
  new row carries the delta, grounds, actor, and a `corrects` reference to
  the amended verdict. Standings already derive from the ledger-including-
  corrections (`ledger_scores` sums every row) and the counter stays in
  step, so reconcile stays clean and the correction rides the checkpoint.
  Every correction hard-logs `VERDICT_CORRECTED` with grounds/actor/delta.
- `apply_score_event` gains the `"verdict_correction"` cause and skips the
  WS-4 belt for that cause ONLY (a correction is an intentional second
  movement); the belt is otherwise untouched and still guards `"answer"`.
- `lily_correct_verdict` tool exposes it to the LLM; the X12 contest
  directive now names it and its grounds, so the re-check produces a
  narrated correction or a stated reason it stands — never "the board is
  locked".
- `_VERDICT_CONTEST_RE` gained a rule-violation branch so the diamond
  contest form ("you violated the rules", "we agreed relaxed/no timer",
  "keep my score at…") fires the path. This is the shared contest-intent
  surface (see W3 seam).

Persistence needs no DDL: `lily_write_score_event` already forwards any
cause, so a correction lands as a `lily_answers` row (`verdict=
"verdict_correction"`, `awarded_points=delta`), grounds+actor in its
`transcript`. No mechanism was removed; the anti-fabrication belt guards a
different cause and stays whole. GUARD_MAP mechanism count 83 → 87
(mechs. 84–87; renumbered from 81–84 on rebase — W6/W7/W3 landed 81–83 first).

## 2026-08-10 — WO-LILY-HOTFIX-008 Z2c: release the question transition when narration is complete but supply is empty

P0. Live `lily-938EFF-2260354c` 03:42:56→03:47:28, the other half of the
Z2 incident: ONE circular deadlock around `q_1_transition`. Rami
answered q1 correctly, Lily's own conversational turn performed the
verdict organically, q2's prefetch had already failed. The 03:43:38
adjudication opened the transition, journaled reveal + verdict
(ORGANIC_PREEMPTED — `q_1_reveal` claimed+confirmed, narration bound)
and then DIED before consuming the question. Evidence correction to the
WO's RCA, from the room log: the arm-failed branch never executed —
`LILY_SPINE` at 03:43:44.668 still reads `delivery=armed` and no
CRASHED line exists, so the adjudicate task was cancelled inside the
reveal-publish `await` (CancelledError skips `except Exception`;
`finally` cleared `_adjudicating`), leaving `armed_question` SET, the
transition open at `[reveal, verdict]`, and zero `ARMED_LIMBO` lines
(the ~40s SECOND_LANE_REFUSED cadence was the window-close adjudication
lane, not the watchdog). The wedge then self-sustained: the confirmed
`q_1_delivery` claim made every post-ruling agent turn REOPEN the ruled
question's window (`LILY_WINDOW | OPEN reason=delivery_claim`, the
phase=reveal→answering regression), candidates fed adjudication,
adjudication hit `SECOND_LANE_REFUSED` (the aired-verdict gap in
`_transition_reached_air`'s `reclaim_unaired` — dd2a56e assumed a stuck
transition never aired) and returned with the question unconsumed —
12×, ~4.5 min honest vamp, zero questions. Chain B (mechs 55/56)
reopened through the gap `053cc44` (next_delivery = completion AND sole
release) + `dd2a56e` + `bba7427` left.

Fix at source — the beat becomes closeable; NO new refusal guards, and
`_transition_reached_air` / SECOND_LANE_REFUSED / ARMED_LIMBO untouched
(mech 80, extends 55/56/57):

1. **Chosen shape: keep the journal, journal the terminal stage** (over
   popping the claim/journal): `transition_narration_complete()` +
   `release_completed_transition()` close a reveal+verdict-aired
   transition with the terminal `next_delivery` marker
   (`delivered_q=None`), free the claim key and clear the open slot.
   Chosen because every completion consumer already keys on the
   journaled `next_delivery` stage and the kept journal is precisely
   what keeps `open_question_transition`'s refusal protecting the aired
   verdict from ever re-narrating — release without amnesia. Nothing is
   deleted; what is RELEASED: the `q_N_transition` claim and the
   `_open_transition_qnum` slot.
2. Release fires from BOTH supply-empty seams: the adjudicate
   arm-failed branch (`supply_empty_at_arm` — the WO's item 1, live
   when adjudication completes) and the post_reveal playout seam
   (`supply_empty_post_reveal` — the WO's "next_delivery reachable
   without an armed question", live when the verdict confirms after a
   non-organic dispatch).
3. `RESUMED_COMPLETE`: the recovery caller (`reclaim_transition=True`
   only, same scoping as `reclaim_unaired`) finding its own open
   transition narration-complete RESUMES it instead of reopening —
   skips the entire narration/publish block (re-publishing would lie:
   the re-entry recomputes winner=None over already-judged candidates)
   and runs only bookkeeping: burn, consume, arm N+1 or release. This
   is what unwedges the REAL incident state, where the release seams of
   (2) are unreachable because the first adjudication never got there.
4. The window-reopen regression, at ITS source: the delivery_claim
   reopen branch refuses a question `question_is_terminal()` already
   owns (`REOPEN_REFUSED_TERMINAL`) — a ruled question has no answer
   window. `question_is_terminal` (the `note_answer_heard` boundary),
   NOT `_is_burned`/`question_already_answered`: the incident row shows
   the durable-asked burn (03:42:56) and pre-window buffered candidates
   both predate the question's legitimate FIRST open (03:43:07), which
   lands in this same branch and stays untouched.

Fixtures (`tests/test_hotfix008_z2c_transition_release.py`, 9 tests,
real q_1732 data): completed-adjudication WO replay (organic verdict +
arm failure → releases at `supply_empty_at_arm`, second lane still
refused in both modes); fresh-transition-can-open; the TRUE wedge
(publish await cancelled mid-beat → no window reopen for the ruled
question; recovery resumes, re-airs NOTHING, releases, no double award,
question burned); resume requires the reclaim flag (normal lane keeps
today's refusal); mech-55/56 regression (never-aired journal still
RECLAIMED_UNAIRED, never "released complete" — Chain B pinned in both
directions); release preconditions (incomplete/already-closed refuse);
first-open-untouched vs terminal-reopen-refused; combined Z2+Z2c
end-to-end (prefetch starved → transition releases → `_bank_to_supply`
lands q2 → arm → q2 delivers → fresh q2 transition opens: the
03:42:56→03:47:28 stall is unreproducible).

Residual, named: the wedge CREATOR — an adjudicate task cancelled
mid-await after opening a transition (no CRASHED log; CancelledError)
— still exists; Z2c makes every such wedge recoverable on the next
forced adjudication instead of permanent. Cancellation policy for
in-flight adjudication is Y7/barge-in territory, out of Z2c scope.

Suite: 2110 → 2119 green (Z2c +9 on top of Z2/Z3/hygiene after rebase,
zero regressions; all 83 character pins green, unmodified). Mandate
numbers: lily_agent.py 15,320 → 15,451 lines; prompt untouched; main
tip `a668062` → this commit; no deploy (per WO).

## 2026-08-10 — WO-LILY-HOTFIX-008 hygiene: call-time prefetch walls; live `_watch`-body needle

Two small closures out of the HOTFIX-008 reviews — no behavior change on
any healthy path.

1. **Prefetch walls read at call time** (Doc-directed hygiene).
   `lily_reasoning` froze `lily_config.prefetch_timeout_seconds()` /
   `prefetch_total_budget_seconds()` into module constants at import, so
   a live env change to `LILY_PREFETCH_TIMEOUT_SECONDS` /
   `LILY_PREFETCH_TOTAL_BUDGET_SECONDS` did nothing until the next
   deploy — not a factor in lily-938EFF-2260354c, but an hour-shaped
   trap for a future incident. Archaeology: the module constant dates to
   `4cbd0b9` (M3, literal 30s); `82ad673` moved the values into
   lily_config accessors but kept the import-time freeze, defeating the
   env-tunability it introduced. Now every wall site (the three per-call
   `asyncio.wait_for` legs, the overall-budget guard, and
   `_generate_grok_json`'s default — resolved `None -> accessor` at call
   time, explicit timeouts like the judge's 12s unchanged) reads the
   accessor when it runs. Defaults identical (20.0/45.0).
   `test_transition_reclaim` now pins BOTH walls by patching the
   accessors after import — proving call-time reads — instead of
   poking module globals.

2. **Watcher-body needle** (Z1 review Note A). Z1's tests drive the
   extracted `_handle_spoken_text`, not the live `_watch` closure — a
   regression re-adding a `_last_assistant_text` fallback between
   `_handle_spoken_text` and `on_agent_speech_finished` would slip past
   them. `test_hotfix008_z1_phantom_cutoff` now needles the closure body
   itself on unparsed AST (comments stripped — the Z1 deletion note
   quotes the old fallback in prose): no buffer reference, no rewrite of
   `spoken`, and `spoken` reaches `on_agent_speech_finished` as a bare
   name. Non-vacuous by construction: a sibling test runs the same
   checker against the embedded pre-Z1 two-line shape and requires it to
   fail on both counts.

## 2026-08-10 — WO-LILY-HOTFIX-008 Z3: a biometric NO_MATCH is not the probe resolving — the Y9 hold now outlasts it

P0 honesty-timing. Live `lily-938EFF-2260354c` (RM_V7MnLQBeFMi9): the
embedder returned `NO_MATCH best=0.6968 threshold=0.75` three seconds
into the session and `_voice_identity_match_at_start` flipped
`_voice_identity_resolved` True — closing `identity_probe_outstanding()`
and with it EVERY N1/Y9 hold surface. Forty seconds later a reply turn,
conditioned by the CLAIMED RETURNER gap-naming beat with no override in
force, aired "You say you've played before, and my table card doesn't
have you tonight, and I don't know why" — a NEGATIVE memory claim made
while the identity question was still genuinely open. At +125s the
player said "call me Rami", the stated-name door matched
`grp_0b07f989`, and the full callback landed ("Reigning champ, last
time you took it with three questions, underwater basket weaving and
all"). Ninety seconds of "I don't know you" followed by knowing him
completely; the recognition beat was correct and simply arrived too
late to be used. (The claim detector never fired on the live claim
"No. It's not." — so no claim-gated fix could have held; the hold has
to ride the probe itself.)

Fix is resolution semantics at the emitting source — no new text-filter
suppressor, no prompt-copy rewrite:

1. **The no-match keeps the probe OPEN.** `identity_probe_outstanding()`
   now defers to `_no_match_awaiting_name_door()` when resolved: a
   biometric NO_MATCH (stamped `_voice_identity_no_match_at`, also on
   probe failure) is one route reporting, not the question closing —
   name binding is mandatory lobby flow, so the stated-name lookup is a
   probe route that WILL run. The hold releases when the name door
   reports (`_identity_name_door_checked`, stamped when
   `maybe_recognize_by_stated_name`'s lookup completes), when memory
   lands, or when `LILY_IDENTITY_NO_MATCH_HOLD_SECONDS` (default 180s —
   the live gap was 125s) expires. All existing hold surfaces (greet
   OVERRIDE, still-checking state note, false-clean-slate TTS rewrite)
   re-arm for free; none was modified in mechanism.
2. **The still-checking state note names the card-gap class** — "do not
   say your card/ledger doesn't have anyone … not even to concede a gap
   to a claimed returner" — since the per-turn note was the only hold
   text live on the defect turn and "empty memory / clean slate" did
   not obviously cover Y9's own gap-naming phrasing.
3. **Y9's honesty is timing-scoped, never regressed**: once the name
   door reports empty (or the hold expires), the honest "table card
   doesn't have you" beat is permitted exactly as before — the fixtures
   pin that the live line's class is NOT rewritten post-resolution, and
   that a match landing means the FIRST memory-bearing statement is the
   callback. Nothing was deleted.

Fixtures (REAL data, reusing Z1's `tests/fixtures/lily-938EFF-2260354c
.transcripts.json`): `tests/test_hotfix008_z3_identity_hold.py` (9
tests) — probe held at the moment the live line aired (override
ordering asserted, not wording), match-landing closes the hold with
recognition first, name-door-empty permits the honest gap line under a
returner claim, per-turn note carries the card-gap class, hold bounded
(expiry + default-outlasts-125s + config-off restores pre-Z3), plain
resolved-no-stamp probe unchanged (N1 protected).

Suite: 2098 → 2107 green (Z3 +9, zero regressions; all 83 character
pins green and unmodified). Mandate numbers: lily_agent.py 15,266 →
15,320 lines; prompt copy untouched (state-note conditioning text only);
main tip `bc36e4a` → this commit. NOT deployed (WO: push only).

## 2026-08-10 — WO-LILY-HOTFIX-008 Z2: supply recovery decoupled from delivery state

P0. Live `lily-938EFF-2260354c` (RCA at `c6769f6`): q2's prefetch
FAILED loudly and bounded at 03:43:12 (`PREFETCH_FAILED
error_class=TimeoutError`, 20s wall, honest status note set, returned
None) — and NOTHING recovered for the rest of the session. ALL supply
recovery (IDLE_REARM, the WS-6 SUPPLY_STALL→`arm_supply_fallback` rung,
IDLE_REPREFETCH, PREFETCH_HARD_TIMEOUT) lived ONLY in the watchdog's
idle branch, reachable when nothing is armed and no window is open. q1
stayed `delivery=armed`/window-cycling the whole session (its
transition deadlock is a separate RCA, untouched here), so the watchdog
took the healthy path every tick; the fire-and-forget prefetch task was
done-and-dead with `next_question=None`, invisible — zero WATCHDOG
lines, exactly one prefetch attempt all session, while `lily_questions`
held 464 active rows. The inline insurance draw ran inside the same
dead task and emitted zero telemetry.

The fix moves existing recovery out from behind the wrong gate — no new
suppressors:

1. **Self-heal from the failure itself.** The prefetch wrapper marks a
   genuine supply failure (generation AND insurance produced nothing —
   deliberate mode/category/duplicate discards deliberately do NOT
   count) and schedules `ensure_supply_recovery` directly; no idle tick
   required. Ladder (`_recover_supply`): ONE de-escalated re-prefetch
   (adult high → medium, general medium → low, threaded as a one-shot
   `_prefetch_effort_override` → `prefetch_question(effort=)`) → curated
   bank → ONE honest line + explicit pause offer (`supply_exhausted`
   act through the gated_say funnel), never an open-ended wait.
2. **Watchdog supply-health check on its own tick**, ahead of the
   armed/window branches: `next_question` None + prefetch task
   done/absent + past the first arm, for 2 consecutive ticks, in a
   non-idle phase → `SUPPLY_SILENT_WINDOW` WARN (once) + recovery. The
   idle branch keeps its existing ladder unchanged.
3. **`_bank_to_supply`** — the draw half of `arm_supply_fallback`,
   factored out delivery-state-independent (MOVED, not rewritten: same
   draw, same N2 strict-topic release, same G2 idempotency, same
   image-strip). It lands the row on `next_question` ONLY; arming +
   nudging stay behind `arm_supply_fallback`'s idle guards. One
   behavior delta: the honest-vamp status note now clears when supply
   LANDS (matching the insurance path) rather than only after a
   successful arm.
4. **Telemetry**: `INSURANCE_BANK_HIT/EMPTY/ERROR` on the insurance
   leg (an insurance exception no longer kills the task trackless),
   `SUPPLY_BANK_EMPTY` carries the trigger, `BANK_TO_SUPPLY`,
   `LILY_SUPPLY | RETRY`, `SUPPLY_EXHAUSTED`, `RECOVERY_FAILED`.
   Incident state resets when any question lands on the supply line;
   `stop_idle_watchdog` cancels the ladder with the loop.

Deletions: none — `arm_supply_fallback`'s draw body moved verbatim into
`_bank_to_supply`; its guards, return codes, and arm/nudge tail are
unchanged (all 17 pre-existing WS-6 fixtures pass unmodified).

Recovery budget (asserted): from the failure event a fallback question
lands on the supply line within one retry
(`prefetch_total_budget_seconds` ≈ 45s) + one 20s bank draw ≈ 65s; the
watchdog backstop adds ≤ 2 ticks (~20s) of detection when the failure
event itself was missed. The 03:42:56 → 03:47:28 spine is encoded in
`tests/test_supply_recovery_z2.py` (9 tests): spine regression (q2 arms
on the supply line while q1's window cycles, delivery untouched),
de-escalation pin, total-failure honest line + pause offer (exactly
one, no re-fire), discard-is-not-failure, insurance telemetry ×2,
watchdog silent-window WARN + recovery, 28-tick no-unbounded-wait
timeline, predicate boundaries. GUARD_MAP: mechs 32/61 amended
(recovery no longer idle-gated), new mech 79.

Suite: 2089 → 2098 green (Z2 +9, zero regressions; all 83 character
pins green, unmodified). Mandate numbers: lily_agent.py 14,976 →
15,266 lines; prompt untouched; main tip `194889e` → this commit. NOT
deployed (WO: push only).

## 2026-08-10 — WO-LILY-HOTFIX-008 Z1: phantom `…[cut off]` double-record — the itemless fallback DELETED

P0. Live `lily-938EFF-2260354c`: 20 of 36 LILY rows ended `…[cut off]`
against `PREEMPTIVE_INVALIDATED total=15`, none a verbatim dup of a
prior row — because each phantom REPLACED the real row. Mechanism (RCA,
all at `c6769f6`): an invalidated preemptive generation reaches the
`speech_created` playout watcher ITEMLESS with `interrupted=True`; the
watcher's fallback (`if not spoken and not had_items: spoken =
game._last_assistant_text`, 14357–14358) fabricated the PREVIOUS
committed turn — whose item hits the buffer at `conversation_item_added`
(generation commit), BEFORE its own playout record — and the
`interrupted` branch recorded/published it marked cut off (record-only,
no TTS). The real turn's record then died on the verbatim-dup belt
against the phantom already sitting in `sk.agent_turns` — which is also
her own context and the repeat-lint window.

Fix is elimination at source, no new suppressor (the fallback was
already one narrowing — `541dcff` — and HOTFIX-007 forbids stacking
another):

1. **Deleted** the fallback. An itemless handle's truth is empty:
   `record_agent_turn` and `publish_agent_transcription_nowait` both
   no-op on empty text, so invalidated-preemptive handles record and
   publish NOTHING; a genuine barge-in (`had_items=True`) still records
   its real partial, marked. Item collection extracted to module-level
   `_handle_spoken_text(handle)` so the fixture drives the exact
   watcher-tail path.
2. **Stamped the buffer**: `_last_assistant_turn = (chat_item_id, text)`
   written at `conversation_item_added`; `_last_assistant_text` stays as
   a property for the readers that genuinely want "her previous turn"
   (verdict/reveal/journal), and `last_assistant_text_for(item_ids)` is
   the only generation-keyed read — any other generation gets `""`, so
   the stale-read race is closed at the buffer, not the reader.
3. `[cut off]` append sites untouched (agent:2234/:2432,
   scorekeeper:3030) — after the deletion the marker only ever attaches
   to genuinely truncated aired text. The suppressed-AND-interrupted
   `cancel_speech` residual documented in GUARD_MAP §7 stands, out of
   Z1 scope.

Fixtures (REAL data only): `tests/fixtures/lily-938EFF-2260354c
.transcripts.json` (all 59 rows) + `.preemptive_log.json` (the 15
invalidation events), driven by
`tests/test_hotfix008_z1_phantom_cutoff.py` (11 tests): live-shape pin,
itemless/tool-call-only/aired handle truth, phantom records-and-
publishes-nothing, full-session replay (one record per generation, zero
phantoms, context window clean), exhaustive-interleaving stale-read
race, stamp scoping, genuine-barge-in marker, no-marker-without-
truncation.

Suite: 2078 → 2089 green (Z1 +11, zero regressions; all 83 character
pins green). Mandate numbers: lily_agent.py 14,926 → 14,976 lines;
prompt untouched; main tip `c6769f6` → this commit. NOT deployed (WO:
push only).
## 2026-08-09 — WO-LILY-HOTFIX-007 phase 2 integration: Y7 + Y10 composed, both independently reviewed

Both items implemented by sub-agents in isolated worktrees, each passed
an independent adversarial review (Y7: PASS with caveats; Y10: PASS
after three review findings — F1 address-debt dead air, F2 pre-game
coverage, F3 stranded re-air arm — were fixed and mutation-checked).
Integration reconciled the two per the Y7 reviewer's conflict map:

1. Y10's gated_say funnel SUPERSEDES Y7's instructed_reply route for
   trigger_cut_recovery (chain F stays closed); Y7's dispatch-path pin
   rewritten to hold the surviving invariant (one dispatch path, the
   shared funnel, no raw bypass).
2. Y7's fire-time HELD branch removed — dead code in-game (Y10's floor
   gate yields on hold WITH the stand-down) and harmful pre-game (it
   returned False without the stand-down, re-opening the stranded-arm
   leak; gated_say's hold gate + GATED stand-down covers pre-game).
3. Y7 review F2 (slow-STT corner) closed at integration: a user-turn
   cancel of a recent cut's recovery now clears that cut's re-air arm
   (REAIR_ARM_CLEARED reason=user_reengaged) — scoped by armed-at
   recency, and deliberately NOT releasing the address debt (the organic
   reply pays it at playout; early release would let code dispatches
   jump the queue). 3 integration tests added.

Semantics composed (per review): Y7 arms nothing on deliberate barges,
so Y10's cut_recovery lane fires only for failed/silent-room cuts —
each item narrows the other.

**Live log lines to watch (from both reviews):**
- `LILY_INTERRUPT | BARGE_IN_CANCEL` — expected on STOP/MC-abort/
  cancel_speech cuts near voice, not only human barges.
- `LILY_CUT_RECOVERY | RESUMED` should drop sharply; one within ~2s of
  room voice = failed-path (expected) or classification miss.
- `LILY_FLOOR | RECOVERY_YIELDED` then recurring `WATCHDOG_PAUSED
  reason=address_unanswered` with no playout = F1 signature (should be
  impossible now; ADDRESS_DEBT_CLEARED should appear instead).
- `LILY_INTERRUPT | FALSE_INTERRUPTION resumed=False` — disclosed
  residual: pause unresumed with no auto-resume backup when voice was
  inside the window.
- Absence check: no "cut short" directive text on a dispatch following
  BARGE_IN_CANCEL.
- Baseline health: RESUMED must not disappear entirely in sessions with
  genuine lull cuts — total absence means the counterweight over-fires.

Suite: 2016 → 2078 green (Y7 +20, Y10 +39, integration +3, incl. all
83 character pins). Mandate numbers: lily_agent.py 14,830 lines; prompt
~8,880 tokens; main tip `8a295de` → this merge; deployed follows on
merge (auto-deploy).

## 2026-08-09 — WO-LILY-HOTFIX-007 Y10: the FLOOR-001 counterweight (code-side restraint)

Y11's canon draft found the push mandate is enforced in CODE, not in the
prompt — the 3.0s responsiveness budget, the M1 "silence is her failure
mode" gate, the auto-resume watchdog — and that FLOOR-001's restraint
counterweight was never built. This is that counterweight, minimal.

**Archaeology (what existed, why it did not suffice).** Four surfaces each
held a piece of "whose floor is it": the `SpeechActRegistry` claims plus
`_playout_started_ids` (act identity — which act is airing or owed, never
"the room is talking"); `_hold_active` / `hold_blocks_dispatch` (a real
global yield, but only ever entered by an explicit event — STOP, a
decline, her own wait-promise, a question timeout — never because the
table is enjoying itself); `_question_pending` (the floor yielded after
SHE asks, blind to the room talking on its own); and FL-1's
`last_addressee_judgment`, the only per-utterance read of
player-to-player talk, which was consumed EXCLUSIVELY as two
`build_state_block` context lines. It conditioned her words and gated
nothing — FL-2, the floor-state machine named as FL-1's own downstream
contract, was never built.

**The fix (extends the say-gate/claims system; no second floor manager).**
1. `LilyGame.floor_state()` — a pure DERIVED read (`hold` /
   `lily_speaking` / `player_speaking` / `open_floor`) over those four
   existing surfaces. No new attribute, nothing to keep in sync, no
   transitions. Room-talk liveness comes from the classifier's OWN knobs
   (`adjacency_seconds` for a lone aside, `cluster_max_gap_seconds` for a
   locked side-cluster) rather than a new tuning constant. A HOST_DIRECTED
   judgment deliberately does NOT yield — the canon keeps the
   responsiveness budget for a direct address.
2. **The graded choice.** `_cut_recovery_should_fire` — a machine timer
   speaking into silence with nobody asking, the purest push path in the
   codebase — now chooses SILENCE (`LILY_FLOOR | RECOVERY_YIELDED`) when
   the room holds the floor or a hold is active. No resume, no minimal
   ack, nothing re-armed.
3. **GUARD_MAP chain F CLOSED.** `trigger_cut_recovery` dispatched via
   `instructed_reply`, skipping every gate in `gated_say`; it now routes
   through that funnel (`act=cut_recovery`, keyless, not hold-exempt), so
   hold / question-pending / P0-G / P8 bind it. Closing it also fixed a
   latent leak: the cut's re-air arm was previously eaten by whatever code
   dispatch came next, which could hand the cut-short-mid-question
   directive to a question that was never cut.

**Deliberately NOT built** (would be the second layer the mandate
forbids): the full graded-response ladder (per-act "full turn vs minimal
acknowledgment vs silence" on every lane) and a stateful FL-2 machine.
Also deliberately unchanged: the game-lane recovery producers (delivery
nudge, WS-2 refire, IDLE_REARM, the P10 re-offer) still progress the game
while the table talks — on game beats the floor IS hers, and yielding
those would park the night. No prompt edits (Y11/Y12 own those).

**Deletions:** none — net addition of one derived read plus one gate
binding; the only removal is `trigger_cut_recovery`'s direct
`instructed_reply` call, replaced by the shared funnel. Failure direction
is deliberate: an unusable judgment timestamp reads as no room floor, so
the lane falls back to speaking — a restraint counterweight must not be
able to fail into a permanent mute.

**Adversarial-review fixes, folded in (three findings, all verified).**
Restraint has to clean up after itself, and the first cut of Y10 let two
pieces of state outlive the recovery it stood down:

* **F1 — the address debt (MEDIUM).** `_awaiting_address_since` is set for
  every host-directed final and cleared ONLY at real playout. A reply cut
  BEFORE playout whose recovery then yields left that latch set with no
  actor left to clear it: `progression_paused_reason()` returns
  `address_unanswered` forever, so the idle watchdog logs
  `WATCHDOG_PAUSED` and `continue`s past the WHOLE game-lane recovery
  ladder every tick — machine-unbounded in-game dead air, M1's failure
  mode arriving through the counterweight's own door. Pre-Y10 the chain-F
  bypass covered this by accident (the resume always fired and its playout
  cleared the latch). Both refusal paths now release it
  (`LILY_RESPONSIVENESS | ADDRESS_DEBT_CLEARED`): the address WAS answered,
  by a turn that died, so the debt dies with the recovery that would have
  paid it. No new watchdog.
* **F2 — pre-game yields (MEDIUM-LOW).** Decision: the floor yield is
  **in-game only**. Pre-game no machine path can re-engage (the idle
  watchdog returns early when `game_started` is False), so a stood-down
  lobby/intake resume would be one-shot dead air exactly where intake
  needs her back — and pre-game there is no game progression to protect
  the room from. An explicit pre-game hold is still honoured one layer
  down, by `gated_say`'s hold gate.
* **F3 — the stranded re-air arm (LOW-MEDIUM).** On either refusal the
  cut's arm stayed live, so an unrelated later dispatch (an IDLE_REARM
  `question_nudge` minutes on) could take it and air a never-cut question
  carrying the "you were cut short mid-question" directive. Both paths now
  CLEAR the arm (`REAIR_ARM_CLEARED`) — cleared, not consumed: consuming
  would hand the tts_node regen gate a turn that is not a re-air.

All three land in one shared exit, `_stand_down_cut_recovery`, so the two
refusal paths can never diverge (pinned by a source test). Also recorded
in comments, no code change: the FL-1 judgment is never cleared on
`note_agent_prompt`, so a cluster read can outlive its cluster by up to
`cluster_max_gap_seconds` (bounded by recency + floor precedence, costing
at most a few seconds of silence on this one lane); and GUARD_MAP now
states explicitly that five direct `instructed_reply` lanes plus the
tts_node `generate_reply` retries remain outside the funnel — chain F was
the machine-into-silence bypass, not the last one.

**Tests:** tests/test_floor_state.py (39 fixture-first tests: floor
derivation and precedence, staleness in classifier units, direct-address
non-yield, the three chain-F gate bindings, keyless/no-retry-ladder, the
F1 latch sequence end to end, the F2 pre-game carve-out, the F3 arm
cleanup on both refusal paths, plus source pins that `gated_say` is the
mixin's only `instructed_reply` call site, that both refusal paths run the
one shared stand-down, and that `floor_state` stores nothing). One
existing WS-3 assertion updated (`test_trigger_dispatches_fresh_resume`):
the resume now consumes its own re-air arm, so the pin moved to
`_reair_turn_pending`. Suite 2016 → 2055 green; the 83 character-regression
pins untouched and green.

## 2026-08-09 — WO-LILY-HOTFIX-007 Y7: a barge-in is a CANCEL, not a cut to recover

When a human deliberately talks over Lily she now stops and yields the
floor: the cut turn arms no auto-resume, no re-air, no regeneration. The
line the player killed does not come back.

**Archaeology (mandate rule 1) — what existed and why it didn't cover
this.** Barge-in DETECTION was already correct and already at the VAD
layer: `InterruptionOptions(min_words=1, min_duration=0.25,
resume_false_interruption=True, false_interruption_timeout=2.0,
mode="adaptive")` with silero VAD — the framework interrupts once VAD
`speech_duration >= min_duration` (`on_vad_inference_done` ->
`_interrupt_by_audio_activity`), which is the WO's ~200ms budget, and a
noise burst with no transcript pauses-and-resumes instead of cutting
(WS-14, pinned in tests/test_interruption_layer.py). What was missing was
the CAUSE reaching the recovery machinery:

- `SpeechHandle.interrupted` — the single cut signal fed to
  `on_agent_speech_finished` (A:14224) — is **cause-blind**. A human
  barge-in, `cancel_speech(force=True)` (mech. 35), the MC abort, the
  STOP primitive and a paused-then-committed turn all set the same flag.
- `_cut_recovery_should_arm` (WS-3) already refused keyed acts, open
  windows, adjudication, STOP and game-over — but armed on ANY
  `interrupted`.
- One barge-in proxy DID exist: `_cut_recovery_should_fire`'s user-turn
  recency guard. It keys on `_last_user_turn_at`, stamped by
  `note_user_turn()` from `on_user_turn_completed` — i.e. **only when a
  transcript commits**. Two facts in the installed livekit-agents 1.6.8
  make that insufficient: (1) `_interrupt_by_audio_activity` pauses and
  sets `ignore_user_transcript_until=now`, and
  `AudioRecognition._flush_held_transcripts` **drops** transcripts whose
  end time falls in that window — so the very utterance that caused the
  barge often never commits as a user turn (routine in a game whose
  steady state is shouting over the host); (2) `_on_end_of_turn` awaits
  `current_speech.interrupt()` **before** `on_user_turn_completed`, so
  even a committed turn can be stamped after the cut has been processed —
  the proxy can work at fire time, never at arm time.
- Net effect, live defect class: a barge with no committed transcript left
  `_last_user_turn_at` stale, the auto-resume fired 3.5s later, and the
  re-air arm told the next code-dispatched act to say the killed line
  again in fresh words — which is also exactly the input that defeats the
  exact-match dup guards (GUARD_MAP §7). Chain A from there.

**The fix (gates on existing arms, no new layer).** One discriminator,
`cut_was_deliberate_barge_in()`, reads the VAD layer — the only place the
cause is knowable before the framework acts on it — off the
`user_state_changed` subscription that ALREADY existed for the P0-2
kickoff floor (`_user_speaking`); `note_user_speech_state` keeps that flag
and stamps the falling edge. It gates:
1. `_cut_recovery_should_arm` — the auto-resume (`LILY_INTERRUPT |
   BARGE_IN_CANCEL`). `failed` (dead TTS/network stream) is never
   overridden by a voice near the cut: that is the cause recovery is for.
2. `arm_reair_gate` in `on_agent_speech_finished` — cause decided ONCE,
   both arms read it (source-order pinned). Bonus: the arm is consumed
   only by `gated_say` (`take_reair_dispatch`, sole call site), so a
   barged CONVERSATIONAL turn used to leave it armed for the next
   code-dispatched act — handing a never-heard question delivery "you
   were cut short mid-question, pick up from where you broke off". Gone
   for the barge case.
3. Chain F, the yield case: `_cut_recovery_should_fire` now consults the
   EXISTING `hold_blocks_dispatch` predicate (mech. 5), so a cut followed
   by "hang on a sec" no longer auto-resumes over the player's own
   request for the floor (`LILY_CUT_RECOVERY | HELD`). No new bypass was
   added — `trigger_cut_recovery` still routes through the same single
   `instructed_reply` it always used (pinned); re-routing it through
   gated_say is a separate consolidation, not smuggled in here.

Y5's transcript contract is untouched: a barged turn still records once,
marked `…[cut off]` (pinned again here). **Deletions:** none — declared
**net addition**: one predicate + one edge stamp + three gates (~55 lines
in lily_speech_delivery.py, +26 in lily_agent.py, all but 12 comment).
Nothing could be deleted honestly: every existing arm still covers the
stream-death cause, which is the one recovery was built for.

**Residual risk (for the reviewer to attack):** WS-3's founding case
(lily-0BD414, "…how does that sound to you? If" [dead]) was itself a
barge-in with no follow-up — Y7 deliberately hands that shape to the
framework's pause-and-resume (which resumes from the pause point rather
than regenerating, and needs RoomIO's `can_pause`, true today) instead of
to the auto-resume. If a deployment ever chains an audio output without
pause, that shape becomes dead air. And the barge window is the existing
2.0s `_CUT_RECOVERY_USER_TURN_LOOKBACK`: a cut with no human voice inside
2s of a committed user turn is still classified as a barge — behavior-
identical at fire time (the same 2s guard already stood the watchdog
down), newly affecting only the re-air arm.

Tests: tests/test_bargein_cancels.py (20) — cause discriminator, the
no-committed-turn reproduction, end-to-end arms-nothing + BARGE_IN_CANCEL
log, no-regeneration, no stale re-air arm, PROTECTED network-cut/failed/
keyed-act paths, the `…[cut off]` row, the hold stand-down, and four
structural pins (VAD budget, single VAD wiring point, no new dispatch
path, cause-decided-once source order). Suite 2016 → 2036 green.

Mandate numbers: lily_agent.py 14,801 lines (14,779 + 22, comments
included); prompt ~8,880 tokens UNCHANGED (no prompt touched); main tip /
deployed sha: integrator's concern.
## 2026-08-09 — P0 PROMPT EVICTION: every 2026-08-09 call ran user turns with NO system prompt

Found by the new pre-call readiness simulation on its FIRST run — not by
a transcript, not by a live call.

**The defect:** `_apply_context_blocks`' dedup scans were keyed on
MARKER TEXT. 59c437b (2026-08-08, HOTFIX-006 N2) added the literal
strings `[GAME STATE]` and `[RETURNING TABLE]` to lily_system.txt
itself. From that deploy on: the state scan matched the INSTRUCTIONS
item — the framework injects the system prompt as a chat item
(id `lk.agent_task.instructions`) once at activity start, and
plain-string instructions are never re-added per generation — and
POPPED THE ENTIRE PERSONA/RULES PROMPT on the first user turn of every
session, while the memory scan matched the prompt text and SKIPPED
injecting the returning-table memory block. Every call on 2026-08-09
(including lily-A070E8, the "Correct"/"Supposed" name-chaos call) ran
its user-turn generations with no system prompt: no persona doc, no
game rules, no tool guidance, no memory block on turn one. Reproduced
production-exact (framework `update_instructions` + real ChatContext)
before fixing.

**The fix:** all three scans now key on the exact injection ids
(_CTX_ID_ADULT / _CTX_ID_MEMORY / _CTX_ID_STATE) — every block this
method ever injected carries one, ids survive ChatContext.copy(), and
no foreign item (the instructions above all) can ever collide again.
Marker text remains only in the say-gate leak filter and a diagnostics
logger, where text-matching is the point.

**New pre-call measures (the operator's ask — "test before another
disappointing call"):**
1. tests/test_precall_cache_readiness.py — scripted-game simulation
   through the REAL hooks (real ChatContext, real injection, real
   framework is_equivalent): instructions survive (the regression pin),
   system prefix byte-stable across quiet turns, preemptive equivalence
   survives the killer churn (candidate landing mid-window) and
   recovers after a history trim, 30 quiet turns = 0 would-be
   invalidations. Runs in every CI test job from now on.
2. .github/workflows/cache-canary.yml + scripts/grok_cache_canary.py —
   live-fire canary (manual dispatch; CI holds the key): two real Grok
   calls with the production prompt; FAILS unless the second call
   reports cached_tokens > 0. Proves the Y1b premise against the real
   provider before any phone call.

**Deletions:** the three marker-text scans (replaced by id lookups —
net simpler). Suite 2011 → 2016 green.

Mandate numbers: lily_agent.py 14,790 lines; prompt ~8,880 tokens; main
tip `71e7af2` (deploying); deployed `1e1c192` at time of writing.

## 2026-08-09 — WO-LILY-HOTFIX-007 wave-1 review: findings fixed (nothing waved through)

Independent adversarial review of the five wave-1 commits (b0da435,
46b2f42, 76dc872, 32eb2d6, 377f69d) against the installed framework
source. Verdicts: Y1a/Y1c/Y2/Y3 PASS (Y1a's byte-preservation and the
Y1c framework claims re-verified independently); **Y4 FAILED as a
reliable gate** and is fixed here:

**HIGH (fixed):** LLMMetrics.speech_id is None at emit time — the
framework's sibling subscriber on the SAME emitter stamps it in place,
and rtc.EventEmitter keeps subscribers in a set, so ordering vs Lily's
handler was an address-hash coin flip: ~50% of sessions would have
silently produced NO per-turn grouping (indistinguishable from "no
multi-call turns"). Fix: `collect_llm_call_soon` defers the fold one
event-loop tick (call_soon runs after the whole emit pass, so the stamp
always lands first); race reproduced and pinned in test.

**MEDIUM (covered + documented):** the Y2 `invalidated` counter sees
only the equivalence-mismatch warning — the framework's ~8 other
preemptive-discard sites (interruptions, pauses) are silent. That is
the RIGHT number for the settle-vs-split decision (it isolates
context-churn invalidation), and the interrupt-path waste now surfaces
separately as `cancelled_calls` in the llm_cache block. Docstring
states the full contract.

**LOW (fixed):** all-empty payloads no longer fold as zero-token calls
(denominator inflation). **LOW (documented):** `used` reads 0 at the
production INFO log level — decision closes on `invalidated` +
`cancelled_calls`, which are always counted. **Nit (recorded):**
b0da435's message says "nine sections"; 7 were added, 2 pre-existed.

**Deletions:** none. Suite 2008 → 2011 green.

## 2026-08-09 — WO-LILY-HOTFIX-007 Y3: conversation history bounded (framework truncate, wired)

**Archaeology (mandate rule 0):** nothing anywhere trimmed the chat
context — a long session grew the prompt without limit (live evidence:
134,357 input tokens against 42,368 cached; the operator's "why is it
115k for a trivia agent"). The framework already ships the mechanism —
`ChatContext.truncate(max_items=)` keeps the tail, drops orphaned
function-call items, re-adds the first instruction message — and
`_apply_context_blocks`' own comment has promised since the anchor fix
that its blocks get "re-inserted if history trimming ever drops it".
Y3 is therefore a WIRE, not a mechanism: `_trim_history()` calls the
framework's truncate under hysteresis watermarks
(LILY_HISTORY_TRIM_HIGH=120 items → trim to LILY_HISTORY_TRIM_LOW=60;
0 disables), invoked in on_user_turn_completed on BOTH the turn context
and the preemptive snapshot, BEFORE block injection (source-order
pinned).

**Why hysteresis:** a trim slides the provider's cacheable prefix, so it
must fire rarely in big steps — never per turn. The one-turn preemptive
invalidation a trim causes is expected and visible in the Y2 counter;
the token ceiling it buys is visible in the Y1c per-call prompt_tokens.
Durable truth (memory block, state block, asked-history ledger, scores)
rides system blocks and the DB — only conversational color ages out.

**Deletions:** none. **Net addition declared:** one method + 2 hook
lines + 2 config accessors + deploy forwarding + 6 tests (real
ChatContext fixtures; block re-insertion proven). Suite 2002 → 2008
green.

Mandate numbers: lily_agent.py 14,765 lines; prompt ~8,880 tokens; main
tip `1e1c192`; deployed `1e1c192`.

## 2026-08-09 — WO-LILY-HOTFIX-007 Y4 measurement gate: round-trips per spoken turn

**Archaeology (mandate rule 0):** Y1c's per-call log already tags each
vocal LLM call with its speech_id; nothing aggregated it, so "how many
serialized calls hide inside one spoken turn" (Y4's target: tool
follow-ups, regenerations) still needed hand-grepping. The AUTHORING
lane (lily_reasoning: generate/verify/judge over aiohttp + genai) is a
different pipeline off the vocal path and is already budget-instrumented
(prefetch timeout/total-budget) — Y4's serialized-micro-call concern is
the VOCAL turn, measured here. **What changed:** collect_llm_call groups
calls by speech_id (bounded, 200 turns); the `llm_cache` summary block
gains calls_per_turn_p50 / calls_per_turn_max /
turns_with_multiple_calls. **Deletions:** none. **Net addition
declared:** ~12 lines in the existing method + 1 test. Suite 2001 →
2002 green.

With this, ALL THREE phase-1 measurement gates are live: Y1b cache hit
rate (per call), Y2 invalidation count, Y4 round-trips per turn. The
next live session on this build closes the Y2/Y3/Y4 design decisions on
numbers, per the mandate.

Mandate numbers: lily_agent.py 14,725 lines; prompt ~8,880 tokens; main
tip `1e1c192`; deployed `1e1c192`.

## 2026-08-09 — WO-LILY-HOTFIX-007 Y2 measurement gate: preemptive-outcome counters

**Archaeology (mandate rule 0):** the framework itself already announces
both preemptive outcomes on its own logger (agent_activity →
`livekit.agents`): "preemptive generation invalidated ..." at WARNING
(always emitted) and "using preemptive generation" at DEBUG. Nothing in
Lily counted them, so the invalidation RATE — the number the Y2
settle-vs-volatile-split decision closes on — was unmeasurable without
grepping deploy logs by hand. **What changed:** `attach_preemptive_tap()`
adds a logging.Filter to that exact logger (no private API, no
monkeypatch; a Filter observes records and never alters/suppresses them),
folding counts into the existing collector; summary gains a `preemptive`
block ({used, invalidated}); each invalidation also logs
`LILY_METRICS | PREEMPTIVE_INVALIDATED | total=N`. **Honest limit,
declared:** `used` populates only when the deploy log level allows DEBUG
— `invalidated` (the decision number) is WARNING and always counted. A
test pins the matched strings against the INSTALLED framework source so a
rewording upstream fails loudly instead of the counter reading zero
forever.

**Decision protocol this enables (Y2):** run the next live session on
this build, read `invalidated` + the Y1c `llm_cache` hit rate. If
invalidations are ~0, the volatile-tail split already settled the
context and Y2's settle-at-turn-boundary variant is unnecessary; if
they're material, the settle variant gets built and the split DELETED
per the mandate (one mechanism, not two).

**Deletions:** none. **Net addition declared:** one collector method +
one wire line + 4 tests. Suite 1997 → 2001 green.

Mandate numbers: lily_agent.py 14,725 lines (14,721 at Y1c — the prior
entry said 14,724, miscounted by 3); prompt ~8,880 tokens; main tip
`1e1c192`; deployed `1e1c192`.

## 2026-08-09 — WO-LILY-HOTFIX-007 Y1c: per-call LLM cache metrics (measure before touching)

**Archaeology (mandate rule 0):** U3(b) built LilyMetricsCollector on the
blessed surfaces (per-turn ChatMessage.metrics + session_usage_updated) —
but the per-turn report carries NO token counts and the usage rollup is
cumulative-only, so nothing could answer "did THIS call cache-hit?" The
per-call LLMMetrics (prompt_cached_tokens, ttft, request_id, speech_id)
is emitted by the LLM COMPONENT's `metrics_collected` — first-class at
1.6.8; the deprecation U3(b) dodged is only the AgentSession-level
subscription, which stays avoided (pinned by test). **What changed:**
`collect_llm_call()` folds into the EXISTING collector (no new module, no
new layer); one INFO line per call (`LILY_METRICS | LLM_CALL |
request/speech/ttft_ms/prompt/cached/hit%/completion/cancelled`); summary
gains an `llm_cache` block (calls, totals, hit rate, per-call TTFT
p50/p95) in the session report + heartbeat. Wired on the general vocal
node at session build and on the adult transport at swap-in.

**Why it gates everything after it:** Y1b's claim (static prefix →
prefix-cache hits at Grok) and Y2's settle-vs-volatile-split decision
both close on these numbers, per the mandate's "measurement closes on
traces/metrics, never transcript rows."

**Deletions:** none. **Net addition declared:** one collector method +
summary block + 2 wires + 5 tests. Suite 1992 → 1997 green.

Mandate numbers: lily_agent.py 14,724 lines; prompt ~8,880 tokens; main
tip `1e1c192`; deployed `1e1c192`.

## 2026-08-09 — WO-LILY-HOTFIX-007 Y1a: system prompt XML-sectioned, assembly pinned static

**Archaeology (mandate rule 0):** the prompt was one flat markdown file
plus a loader-level rubric append (WO-LILY-AUDEERING-001 Task 3) — no
prior sectioning attempt existed to consolidate; this is structure over
existing text, not a new mechanism. **What changed:** prompts/
lily_system.txt is now nine top-level XML sections in canonical order
(identity → voice → game_rules → state → tools → memory →
self_knowledge → tts_guidelines → voice_output; the last two already
existed as tags and were kept top-level, not nested). One relocation:
"## HOW A QUESTION RUNS" moved before "## YOUR TABLE STATE" inside
game_rules so procedure precedes state-legend (static-first ordering,
Y1's cacheable-prefix goal). All interior directive text byte-preserved —
Y12's 83 character pins prove no directive changed. prompts/
layer_lily_adult.md wrapped in `<adult_layer>` (the `# ADULT MODE`
dedup marker preserved inside). The loader's rubric append now wraps the
audeering rubric in `<room_read>` so the assembled prompt is fully
sectioned end-to-end.

**Static-assembly proof (Y1 verify clause):** the assembly is exactly
static-file text + import-time rubric constant — byte-identical across
turns by construction. tests/test_prompt_structure.py pins it: sections
present/balanced/ordered, adult layer tagged with marker intact,
assembled prompt == file + tagged rubric (the equality IS the stability
proof), room_read appears exactly once, rebuild == module constant.

**Deletions:** none. **Net addition declared:** structure tags only
(~40 tag lines across two prompt files) + 6 structure tests; no
directive text added or removed. Honest cost: assembled prompt grows
8,400 → ~8,880 tokens (35,533 chars) from tag overhead — the buy is
addressable sections for the Y12-gated prompt audit and a provably
static cacheable prefix (Y1b measures the hit rate once Y1c
instrumentation lands).

Mandate numbers: lily_agent.py 14,699 lines (+comment lines only);
prompt ~8,880 tokens (was 8,400 — tag overhead, declared above); main
tip `1e1c192`; deployed `1e1c192`. Suite 1987 → 1992 green.

## 2026-08-09 — WO-LILY-HOTFIX-007 wave 1: Y5 transcript truth (diagnosis inverted), Y11 canon draft

**Y5 archaeology (mandate rule 0):** the mechanism the WO asked for already
exists and is correct by construction — the one-question yield clip runs in
tts_node BEFORE synthesis, the clipped text binds to the speech handle
(note_post_tts_text), that exact text synthesizes, and playout consumes the
binding for both transcripts (P0-C). What failed was OBSERVABILITY: the
consume log was named `POST_TTS_REWRITE`, which reads as post-hoc
falsification when it in fact corrects the record TOWARD the TTS input
(the "raw" it replaces is the model's pre-clip prose, which never aired).
That name sent a day of transcript analysis the wrong way. Renamed to
`RECORD_BOUND_TO_TTS_INPUT` (info level, with lengths). The audio-vs-record
divergence in the evidence session is therefore most plausibly Y6
cross-turn interleaving (audio of turn A read against the DB row of
re-asked turn B) — flagged for re-verification against speech_ids + audio.
**Deletions:** none — the claimed post-hoc rewrite path does not exist as
diagnosed. **Net addition:** tests only (tests/test_transcript_truth.py:
source-order pin trim→bind→synthesis; record==TTS-input; one-shot binding;
fallback). **Documented limit (Y7's scope):** with text_output=False there
is no transcript synchronizer, so no text source knows the aired portion
of an INTERRUPTED turn — "…[cut off]" is the honest partial-airing marker;
audio stays the only ground truth there.

**Y11:** docs/HOST_CANON_DRAFT.md — host-first canon draft around the
approved spine, explicit PRESERVED section (wit/warmth/timing/callbacks/
v3 audio tags quoted from the live prompt), and a 7-row conflict table.
Key finding: the push mandate lives mostly in CODE, not the prompt
(responsiveness budget 3.0s, the M1 silence gate, speak-by-default) —
FLOOR-001's restraint counterweight remains unbuilt (Y10). DRAFT —
operator approval required; nothing installed.

Mandate numbers: lily_agent.py 14,659 → 14,670 lines (log rename +
contract comment); prompt 8,400 tokens unchanged; suite 1900 → 1904.

## 2026-08-09 — lily-A070E8: the name-fix death spiral and the repeat storm

Live 11:03 UTC. STT heard "Robin" for Rami; the fix exchange then bound
him as "Correct" (his one-word confirmation) and "Supposed" (a fragment
of "it's supposed to be"), while the same cut greeting aired up to FOUR
times around it. Two contained fixes:

- **Adjudication vocabulary is never a name** (`lily_binding.py`):
  verdict words, spelling-fix words and screen-complaint words
  ("correct", "supposed", "wrong", "screen", "spelled", "put", "fixing",
  "latency"…) join the bind stoplist. Exact lowercase match — Wright and
  Newman stay bindable. Pinned in `tests/test_bind_stoplist.py` against
  the live utterances.
- **The stubborn-repeat third copy is silence, not another replay**
  (`lily_agent.py` tts_node): the WS-3 regen gate suppressed a verbatim
  re-air once and regenerated — but when the regen ALSO came back
  verbatim, the old contract aired it anyway. That third copy (and the
  fourth) was the storm. It now yields the floor silently
  (`LILY_SAY_SUPPRESSED | reason=stubborn_repeat`), claims released;
  question deliveries stay verbatim-exempt.

Deploy note: pushes to main auto-deploy (deploy.yml on: push). Suite
1900 green.

## 2026-08-09 — P2 volatile-tail split: preemptive generation back ON in-game

G1 turned preemptive generation OFF for the whole live game for a sound
reason: the state block honestly changed on nearly every turn — the
just-heard utterance lands as an answer-candidate line, the answer window
flips on the clock, and the temporal-context line is a literal clock — so
the 1.6.x equivalence check discarded almost every speculative run
(double LLM cost, zero win). The operator's read was also right: turning
the feature off traded ~1.5s/turn of hideable LLM latency away instead of
fixing the invalidation.

The split (the fix lily_temporal_context's docstring always promised):

- `build_state_block_split()` → (stable, volatile). Stable — injected in
  on_user_turn_completed under the stable item id, equivalence-visible —
  changes only when the game genuinely moves. Volatile — the clock line,
  answer window, live candidates — rides a SEPARATE item injected only on
  per-generation copies (`llm_node`'s `include_volatile=True`), which the
  equivalence check never sees and every real generation refreshes
  in place. Full renders stay byte-identical.
- Live games keep preemptive ON by default; the playout pause/resume pair
  re-enables it mid-game too. `LILY_LIVE_PREEMPTIVE=false` restores the
  G1 hold if the live discard rate proves the split insufficient.
- Speculative runs see volatile state as of ~a beat earlier; verdicts and
  scoring are unaffected (they ride instructed replies and the committed
  ledger, never user-turn generations).

Also: `LILY_VOCAL_MODEL` env knob (A/B without redeploy), prefetch walls
relaxed to 20s/45s and env-tunable. Pinned in
`tests/test_preemptive_volatile_split.py`; suite 1897 green.

## 2026-08-09 — device+name amnesia: the staged candidate slammed the name door

Operator escalation (and they were right to be angry): a returning player
on the SAME device, saying their OWN name, stayed unrecognized forever on
any deploy without the ECAPA voice deps. The ordering was absurd — a
stated name ALONE opened the recognition door
(`maybe_recognize_by_stated_name`), but a staged device candidate
unconditionally slammed that door shut and left promotion waiting on
voice verification alone, which without the embedder can never arrive.
Device history + a name ON that history is strictly stronger evidence
than the name alone.

- The staged-candidate guard now checks the stated name against the
  staged file's `player_names`: a match promotes exactly as weakly as the
  name-only door (`device_plus_name`, verified=False — the biometric
  still runs and still outranks it, N5 preserved). A name NOT on the
  staged file keeps the quarantine: a stranger on a shared device stays a
  fresh table.
- The missing-embedder warm failure is now a loud ERROR naming the
  install (`requirements-voice-identity.txt` + migrations/021) instead of
  an info-level shrug while Lily visibly forgets returning players.

Pinned in `tests/test_name_stated_recognition.py` (promote-on-match,
quarantine-on-stranger; all prior weakness fixtures unchanged). Suite
1889 green.

## 2026-08-09 — the empty glass transcript: P0-C's silent casualty

Live report: the transcript panel sat open and completely EMPTY through a
whole call. P0-C set RoomOptions `text_output=False` so RoomIO's pre-TTS
prose can't race the corrected transcript — correct — but that switch
ALSO disabled the framework's USER transcript forwarding, and Lily's own
final transcript went out on the LEGACY `rtc.Transcription` API only,
while the glass consumes `lk.transcription` TEXT STREAMS
(`useTranscriptions`). Neither lane reached the panel.

- `publish_agent_transcription_nowait` now mirrors the same corrected
  final text onto the stream wire (same segment id, final=true) alongside
  the legacy publish. P0-C preserved: pre-TTS prose stays off.
- New `publish_user_transcript_nowait`, hooked at the STT final handler:
  publishes the user's final utterance as a text stream with
  `sender_identity` impersonating the device's participant (the same
  mechanism the framework's own forwarder uses), so the glass attributes
  the line to the table, never to Lily. The bound player name rides
  `prmpt.speaker_label` when the roster resolves one.

Pinned offline in `tests/test_transcript_forwarding.py`. Suite 1887 green.

## 2026-08-09 — lily-1C53C6: the journaled-but-silent transition deadlock

Live 07:38–07:43 UTC, single caller, adult mode: q1 perfect, then ~92s of
dead air on q2 until the caller hung up. The q2 reveal generation
(grok-4.5) timed out twice at the 30s prefetch timeout; the transition had
been OPENED and JOURNALED but its verdict turn died before playout. The
watchdog then fired ARMED_LIMBO → forced adjudication every ~20s, and the
N12 SECOND_LANE_REFUSED guard refused every one of them because "a journal
already exists" — journaled-but-unspoken, unreclaimable, permanent dead
air. Same class as STREAM-INTEGRITY-002, which patched real barge-in
cutoffs but not this path.

- **The deadlock** (`lily_agent.py`): the N12 guard now distinguishes
  journaled from AIRED, using state the class already tracks. New
  `_transition_reached_air()`: a bound narration, a CONFIRMED stage key,
  or a journaled next_delivery = the transition spoke.
  `open_question_transition(reclaim_unaired=True)` — passed ONLY by the
  watchdog's recovery adjudication (`adjudicate(reclaim_transition=True)`)
  — releases a dead journal and its stale claims (`RECLAIMED_UNAIRED`) so
  recovery can narrate. A transition with ANY aired stage keeps the
  refusal: N12 stands, recovery never re-narrates something that played.
- **The trigger** (`lily_reasoning.py`): PREFETCH_TIMEOUT_SECONDS 30→15
  (a stalled grok call is pathological well before 15s), plus one overall
  PREFETCH_TOTAL_BUDGET_SECONDS=30 across generate → verify → choices —
  stacked per-call timeouts could previously wait ~90s before
  PREFETCH_FAILED fired.
- **P0-G scoped to its own contract** (`lily_speech_delivery.py`,
  `lily_agent.py`): the unfiltered `progression_paused_reason()` checks
  d233e3c added at the dispatch chokepoint, `expect_delivery`, and the
  prefetch auto-advance re-blocked the game-lane acts the older P10 gate
  deliberately exempts — `question_pending` refused the very delivery
  nudge that resolves it (both fixtures P0-G left red pinned exactly
  this), and the address latch blocked silent re-ARMING after "back to
  normal" when it only owns the floor. Dispatch/expect now pause on
  address-unanswered + setup-pending only (hold, question-pending, stop
  are already governed at the same chokepoints by their own gates);
  auto-advance arms silently under an address latch while its nudge
  still yields at dispatch. P0-G's own tests pass unchanged.

Regression suite: `tests/test_transition_reclaim.py` (deadlock repro,
reclaim, three N12-stands cases, prefetch budget). Full suite 1884 green —
including the two fixtures red on main since P0-G landed.

## 2026-08-09 — P0-G: meta and intake pause game progression

Question delivery could still take the floor while Lily owed the table a
direct/meta response or setup completion. The idle watchdog, prefetch
auto-advance and post-reveal path each had their own progression trigger, so
a local conversational hold did not veto every gun.

One `progression_paused_reason()` now covers unanswered direct address,
active host speech, conversational hold/question and pending setup. It gates
delivery expectation, the `gated_say` delivery choke point, post-reveal
dispatch, prefetch auto-advance and watchdog reconciliation/re-arm. Pauses are
greppable as `LILY_PROGRESSION`; no question number advances merely because
the host is handling meta.

## 2026-08-09 — P0-H: responsiveness latch clears on playout

The direct-address responsiveness clock used to clear when `gated_say`
dispatched a response. Dispatch is not delivery: a queued, suppressed or
wedged handle could therefore erase the clock without the room hearing Lily
and hide the `ADDRESS_UNANSWERED` evidence.

The latch now survives generation and dispatch. It clears only on the
framework's real playout-start transition, alongside the existing speech-id
airing marker; the one-shot warning resets only after that audible response.

## 2026-08-09 — P0-F: pre-window answers require actual delivery playout

A `q_N_delivery` claim is created in `tts_node`, before its audio necessarily
starts. The old early-answer gate treated that queued claim as if the table
had already heard the question, so lobby/meta speech between claim and
playout—or speech in the post-playout discharge gap—could be replayed into
the answer window.

Delivery registration now opens no answer scope. The framework's real
`speaking` transition records the playout start, completion records its end,
and only transcript segments whose captured interval overlaps that audible
interval may buffer or trigger answer-aborts-read. The old pre-claim backfill
is retired. Window-open answers are unchanged.

## 2026-08-09 — M7: model-visible prompt contract matches runtime truth

The host prompt, `lily_begin_round` tool description and voice inventory now
agree with deterministic gates. Speaker labels are best-effort rather than
identity; one confirmed bound name precedes Q1; clear start language—not a
laugh, room energy or ambiguous yes—authorizes the host tool; explicit age
statements consume consent without a repeat prompt.

Custom rounds are claimed only after registration, ambient state no longer
claims to expose canonical answers, UTC/session age is documented as volatile
context, and picture speech requires `image_shown` before any on-screen claim.
STOP now has one prompt-level acknowledgment followed by silence from the game
lane until explicit resume. Board-sync language cannot invent which side is
ahead.

## 2026-08-09 — M6: Grok prompt-cache routing and temporal context

The vocal Chat Completions client now sends xAI's recommended stable
`x-grok-conv-id`, derived opaquely per session, so consecutive requests route
to the same prompt-cache server. Lily remains correct on cache eviction or a
miss; cached-token metrics continue through the existing session rollup.

Static instructions remain the cacheable prefix while volatile state remains
at the tail. Every generation now receives current UTC plus exact session
elapsed time in that tail, enabling time-aware pacing and relative-time
answers without freezing a timestamp into the system prompt.

## 2026-08-09 — M5d: post-session assessment on Grok 4.5 High

The offline clinical-desk assessment and reconciliation sweep now use Grok
4.5 at `high` effort through the same structured Responses transport. The
pending/complete/failed report state machine and retry discipline are
unchanged; no live player turn waits on this lane.

## 2026-08-09 — M5c: vision and image correspondence on Grok 4.5

Player photo analysis, real-entity image approval, arsenal correspondence and
image-first descriptions now share Grok 4.5 image→text transport. URL and
byte inputs use the same grounded failure contract; classifiers request
structured JSON and fail closed.

General/adult image rendering remains on dedicated image models. The vocal
host receives only verified descriptions/results and never authors visual
facts.

## 2026-08-09 — M5b: Tier-2 judge on Grok 4.5

General and adult ambiguous-answer adjudication now share Grok 4.5 at
`medium` effort through the structured Responses transport. The 12s bound and
Tier-1 fail-closed behavior remain unchanged; the model proposes a verdict
and the scorekeeper remains the only score writer.

## 2026-08-09 — M5a: question reasoning on Grok 4.5

Question generation, verification and multiple-choice distractor synthesis
now use Grok 4.5 through the Responses API. General work runs `medium`;
adult sub-theme/category/question authoring and verification is always
`high` and cannot be downgraded by an environment override.

The Gemini client remains temporarily isolated for the not-yet-migrated
Tier-2 judge and multimodal image gate. Prefetch failure behavior is
unchanged: visible status + bank/freeform fallback, never vocal invention.

## 2026-08-09 — M4: one Grok 4.5 vocal host

General vocal moved from Gemini 3.6 Flash to Grok 4.5, joining the adult
lane on one provider/model. Routine turns run `low`; disputes, ambiguity,
multi-intent and meta turns escalate to `medium` for that turn, then restore.

The existing adult prompt layer remains the register switch; an optional
adult model/effort override still works. The xAI client keeps the 30s read
backstop, zero SDK retries, and Lily’s spoken-turn token cap. General
adjudication remains on its explicit Gemini judge model until the separate
reasoning/judge migration.

## 2026-08-09 — M1b: deterministic `PROHIBITED_CONTENT` fallback

In `lily-9337B1-331ff234`, a post-STOP curated-bank `question_nudge` spent
11.2s in Gemini and failed non-retryably with opaque
`PROHIBITED_CONTENT` (request `9392c0887a68`).

**Fix.** A blocked turn with an explicit pending delivery intent bypasses the
model and emits the already-vetted deterministic question sheet exactly
once. Blocked conversation has no invented fallback and fails closed without
retry. Every event logs model/request/question/category plus hashes of system,
state, conversation and tool components for provider escalation without
logging raw private context.

## 2026-08-09 — M1a: every configurable Gemini filter is non-blocking

While the Grok migration proceeds, Gemini remains on several lanes. Vocal,
reasoning and assessment already used `BLOCK_NONE`, but general image
generation and Google grounding/search omitted safety settings.

**Fix.** One shared policy now supplies `BLOCK_NONE` for harassment, hate,
sexually explicit and dangerous content to every remaining Gemini text/image
request. Runtime tests audit vocal, reasoning, judge/assessment, image and
grounding call sites. Provider-controlled `PROHIBITED_CONTENT` remains a
separate deterministic-fallback ticket.

## 2026-08-09 — Speechmatics `operating_point` deprecation removed

Production warned that `TranscriptionConfig.operating_point` will be removed.
The LiveKit 1.6.8 plugin still exposes only that legacy constructor field.

**Fix.** `LilySpeechmaticsSTT` preserves the plugin’s diarization, FIXED
turn mode and tuning surface, but hands the RT SDK
`TranscriptionConfig.model=Model.ENHANCED` with no `operating_point` on the
wire. Runtime/eval paths and the tuned artifact now use the supported model
property; tests assert no deprecation warning.

## 2026-08-09 — LiveKit endpointing deprecation removed

Production warned that top-level `min_endpointing_delay` /
`max_endpointing_delay` will be removed in v2.0.

**Fix.** The same 0.6s/6.0s bounds now live under
`TurnHandlingOptions(endpointing=EndpointingOptions(mode="fixed", ...))`.
FIXED turn detection and adaptive interruption behavior are unchanged; this
does not enable the LiveKit Turn Detector default.

## 2026-08-09 — P0-E3: retire the `Playing` identity poison

After the binding/rekey/quarantine fixes, production evidence showed the
bad one-sample `Playing` centroid at cosine 0.7518736 to canonical Rami:
barely over the absolute floor, not safe to merge without a fresh biometric
verification.

**Cleanup.** Migration 025 retires that centroid, deletes only voiceprints
315/316 and wrong memory row 31, and preserves the session, transcript,
answers and report as audit evidence. Predicates are exact and idempotent.

## 2026-08-09 — P0-E2: weak voice identities cannot found rivals

Live `lily-9337B1-331ff234`: the bad `Playing` name-set group failed to
match the canonical seven-sample Rami centroid, then close-time enrollment
founded a competing one-sample ECAPA identity.

**Fix.** Every group without an identity checks the global biometric pool
first. Room-name and name-set-hash groups may only redirect into an existing
confident match; they can neither found nor reinforce their own centroid.
Stable participant/env groups may still create a first identity. Existing
weak self-orphans are excluded from matching so they cannot win by
self-similarity.

## 2026-08-09 — P0-E1: voiceprint rekey uses the production schema

Live `lily-9337B1-331ff234`: provisional→resolved voiceprint rekey aborted
with PostgreSQL `42703` because it selected
`lily_speaker_voiceprints.sample_count`. That column exists only on
`lily_voice_identity`; the failure stranded one `Playing` row and later
enrollment wrote another.

**Fix.** Rekey selects/updates only the migration-001 voiceprint columns,
merges identifier lists into the resolved row before deleting the provisional
row, and is safe to rerun without duplicate rows. Tests assert the production
migration contract directly.

## 2026-08-09 — P0-D: confirmed identity before Q1

Live `lily-9337B1-331ff234`: “not my first time playing with you” was
misread as player name `Playing`; that synthetic roster row unlocked Round
One and later poisoned voiceprint, centroid, memory and standings.

**Fix.** Conversational fallback tokens no longer authorize durable binding.
Only direct self-identification (“my name is / call me / this is”), a bare
name, or a biometric name label can bind. Evidence persists beyond the 2s
fragment window and outranks tool arguments. Production kickoff now blocks
with `identity_unconfirmed` until one confirmed player exists.

## 2026-08-09 — P0-C: transcript equals post-TTS truth

Live `lily-9337B1-331ff234`: a `q_6_delivery` conversational model turn was
strictly rewritten to the Caesar sheet for TTS/glass, while the transcript
kept the original conversation. The false-clean-slate guard produced the
opposite split: raw text appeared in transcript while audio used the honesty
sheet.

**Fix.** `tts_node` now binds its exact final post-transform text to the
speech handle. Playout completion consumes that text for Supabase and a
single manual RTC transcription; RoomIO’s pre-TTS agent text output is
disabled. Rewrites, clipping and punctuation therefore share one transcript
truth with TTS.

## 2026-08-09 — P0-B: STOP is global and sticky

Live `lily-9337B1-331ff234`: “stop the quiz” entered a temporary hold, then
the next complaint released it. Q5/Q6 armed after STOP; a question nudge hit
Gemini safety, and Q6 later opened/scored meta speech.

**Fix.** STOP now sets a persistent game-delivery latch, session-retires
armed/prefetched questions, cancels prefetch/window/delivery/early-answer
state, clears the glass, and freezes every game-plane owner. Conversation may
continue, but only an explicit resume/continue command clears the latch.
Repeated STOP does not repeat the acknowledgment.

## 2026-08-09 — P0-A: answered/revealed question is dead forever

Live `lily-9337B1-331ff234`: Q3 ("Sigmund Freud") committed and revealed,
then a stale clarification for the earlier fragment "He said it" asked for
a final answer and re-asked the already-scored question.

**Fix.** `note_answer_heard` now clears/cancels every clarify owned by that
question. Deterministic clarify triggers and `lily_log_clarify` refuse once
the question is terminal; racing clarify replies are ignored. The state block
marks the current question `answered_closed` until N+1 owns the turn.

## 2026-08-09 — Latency: the read timeout that actually applies, and the cache prefix

Live `lily-2C489B`: `e2e_latency` p50 4,374ms / **p95 13,610ms**, `llm_ttft`
p50 1,954ms / p95 7,301ms, `tts_ttfb` p50 412ms. TTS is not the problem.

**The 30s adult read budget was dead config, on both lanes.** `0f31b71`
raised it because `livekit-plugins-openai` builds its own AsyncClient with
`httpx.Timeout(read=5.0)` — correct diagnosis, ineffective fix. The
plugin's `LLMStream` **inherits** from
`livekit.agents.inference.llm.LLMStream`, which passes
`timeout=httpx.Timeout(self._conn_options.timeout)` on every `create()`,
and in openai-python a per-request timeout REPLACES the client's. So the
real wall was `DEFAULT_API_CONNECT_OPTIONS`' **10s** the whole time — a
coin flip against a lane measured at p95 7.3s. Worse, `max_retry` defaults
to **3**: one wedged first token was ~40s of dead air on a voice-first
game. Now set where the framework reads it, via the supported
`AgentSession(conn_options=SessionConnectOptions(llm_conn_options=...))`,
with `max_retry=1`. The external fact is asserted against the INSTALLED
package, so a version bump that changes it fails loudly.

**The prompt-cache prefix was invalidated on every injection.** The adult
layer and memory block both `insert(0, ...)` — in FRONT of the ~8,000-token
system prompt. A provider's cache keys on the prefix, so the largest stable
thing in the context was pushed out of it every time. Live: 134,357 input
tokens against 42,368 cached. Both now anchor behind the agent's own
instructions; the volatile per-turn state block already appended at the
tail and stays there.

**Four premises in the original brief were wrong and are corrected here so
they are not re-derived:** (1) "43 turns" is ~21 agent turns —
`lily_metrics.py:97` counts one per `MetricsReport` and the handler feeds
it both user and assistant items, so per-turn token maths was off 2x;
(2) the 134k input tokens are the VOCAL lane only — `lily_reasoning`
builds its own clients and never reaches `session_usage_updated`; (3) the
state block was already correctly at the tail, not the head — the
cache-hostile calls were the two `insert(0, ...)`; (4)
`on_user_turn_completed_delay` p50 0.5ms is measured proof the injection
path is not on the critical path.

**Deliberately NOT changed: turn-taking.** `end_of_turn − transcription =
150ms`, so `min_endpointing_delay` costs nothing; the 1,361ms is
Speechmatics finalization, driven by `max_delay: 1.5` and
`end_of_utterance_silence_trigger: 0.8` in `stt_tuned.json`. Those came out
of the WS-13 echo-room study against a named evidence session, and trading
transcript accuracy for latency is not a call to make from a metrics table.
Flagged as the lever; left alone.

**Open, not closed:** `tts_node` drains the ENTIRE LLM stream into a list
before TTS is called, which the reported stages cannot see (`tts_ttfb` is
anchored to the moment text reaches ElevenLabs) — ~496ms unaccounted for at
p50, ≥3,975ms at p95. The buffer is load-bearing: the delivery-claim and
dup guards are whole-turn decisions, so it must be predicated, not removed.
Preemptive generation is also off for the whole live game, putting up to
1,511ms on the critical path. Neither is safe to change without first
instrumenting `tts_node`'s first-chunk and yield timestamps — without that,
the two are indistinguishable from outside and their fixes differ.

1809 passed.

## 2026-08-09 — Glass truth: the phase that was reached is the phase that is published

A UI-sync audit of the agent↔glass seam. Three HIGH findings, **one root
cause**, verified with a repro against the real publish path.

**`publish_attributes_nowait` bound the phase at TASK-RUN time, not at
schedule time.** The coroutine read `self.ui_phase` when the loop got round
to it, so two consecutive SYNCHRONOUS transitions collapsed into whichever
ran last. `adjudicate` does exactly that:

    if round_over: self._set_ui_phase("scores")   # queues publish A
    if not self.arm_next_question():              # -> "question", queues B

Measured before the fix: `PHASES ON THE WIRE: ['question', 'question']`.

- **`phase="scores"` had never reached the wire** on a normal round
  boundary. `LilyStandings` — a whole designed screen — only rendered when
  supply was starved enough for `arm_next_question` to FAIL.
- **The reveal was erased ~1 RTT after it published.** The frontend gates
  every part of the reveal on the phase (`lily-game.tsx:377/396/419-427`),
  and the `reveal` data packet only fires at the VERDICT TURN'S PLAYOUT —
  an LLM round-trip plus TTS TTFB after the phase reverted. She said
  "Correct — Saturn! Point to Maya" over a board showing no answer and no
  verdict. "Go back" showed answers the live screen never displayed,
  because history folds `state.reveal`.
- **The previous question was re-chalked as the live one.** Between arm and
  air, metadata still held question N's prompt and choices while
  `question_number` read N+1 — so the just-answered question was freshly
  animated in under the new number for the whole flourish + LLM + TTS.

The phase is now bound where the publish is queued, and `_phase_hold` —
built this morning as a lobby→first-delivery bridge — is widened to every
phase an arm can interrupt (`lobby`, `reveal`, `scores`). Same defect, same
seam; the earlier fixture asserting "mid-game arm does not hold phase"
encoded the bug and is rewritten with the evidence. The frontend's 12s
reveal TTL is the backstop if the next delivery never airs.

**Also closed from the same audit:**

- `lily_control.start` / `.skip` returned `ok:true` unconditionally, while
  `start_game` early-returns on `start_blocked_reason` / intake round-robin
  / already-started and `skip_question` early-returns while adjudicating.
  The player tapped Start, the pill returned to normal, nothing happened
  and nothing was said. Both now report the real outcome with a reason.
- A worker reconnect published EMPTY room metadata over a restored
  question. Room metadata is server-side and survived the restart, so this
  erased a question the agent still held armed — picture and all. It now
  republishes what she actually has.
- The supply-stall hold was gated on `revealPhase`, but most stall entries
  leave the phase on `question` (`skip_question`, `flush_for_mode_switch`,
  `on_answer_leak`, and a delivery released after too many cuts). The flag
  was on the wire and the board rendered nothing for all of them.
- `LILY_HEARTBEAT_STALE_MS` (60s) sat BELOW the agent's hold timeout (90s),
  and there is no periodic attribute publisher — so a healthy "take your
  time" tripped the watchdog into "Connection paused. Scores may be out of
  date.", resting the timer bar and dropping the skip affordance while the
  agent was alive and counting. Raised to 120s.

**Audited and found clean** (recorded so they are not re-audited): the
per-key attribute merge, `applyRoomMetadata` clearing, the 50/50 republish
carrying `image_url`, the glass image pending/confirmed lifecycle, and the
client reconnect/late-join snapshot restore.

**Open from the same audit, not closed here:** an unbound-voice award is
invisible on the glass (`_players_payload` has no branch that can emit the
`unclaimed` row the client was extended to render); review-history keys
collide past the 60-snapshot cap; a re-bind orphans a scored roster row and
the client collapses with `max()` rather than `sum()`; and the metadata
`question_number` stale-render guard the agent publishes is never read by
the frontend — the guard that would have caught the re-chalk bug above.

1802 passed.

## 2026-08-09 — Barge-in is the steady state, not the error path

Operator directive: *"it's a game that's going to have a lot of barging in,
a lot of shouting out, there's going to be a lot of interruptions like this
— this is normal."* Every defect below is the system treating an
interruption as a failure to retry.

Live session `lily-2C489B-a61fb6d9`, 22:45:30 → 22:52:31 ET. **Deployed sha
at session time: `72fd25c`** (deploy succeeded 02:33:01 UTC, twelve minutes
before the call — the first session to run the full B2/B3/B4 + P0-1…P0-5
series). Outcome: **seven minutes, zero questions played, one arsenal entry
burned.** `lily_answers` empty, `score_ledger` `[]`, `answer_window_open`
false on all 25 addressee rows including the ones stamped `phase=question`.

**The glass deadlock.** Two gates both bound on the delivery turn's playout
COMPLETING: the published phase (`_phase_hold`, pinned to lobby at arm) and
the metadata publish carrying `image_url` (moved to window-open by the
2026-07-31 "screen never leads the voice" fix). A barged delivery completes
neither. He stared at "START THE QUESTIONS" — his screenshot is preserved in
the session's `status_notes` — while she described a photograph whose signed
URL was sitting in `current_question.image_url`. So he interrupted to say the
picture wasn't there, which cut the delivery, which kept the picture off the
glass. **His reason for interrupting was the thing the interruption prevented
from being fixed.** One publisher now, `publish_question_to_glass`, fired when
the delivery STARTS airing; the screen still never leads the voice.

**The burn.** `lily_record_asked` fired at ARM — twenty seconds before the
delivery began and three cut attempts before it gave up. Arsenal entry
`861712c7` can never be served to that table again, for a question nobody
heard. The durable row now lands at air; the in-session mirror stays at arm
so one session still cannot draw the same question twice.

**The self-repetition.** `record_agent_turn`'s dup guard read
`if not interrupted and ...`. Live, every copy marked cut off: the "Great to
meet you, Rami" turn ×4, the burlesque delivery ×3, "Yeah" ×3. Because the
record feeds `sk.agent_turns` — her own conversational context — she read her
line back four times and said it again: *"Okay, now you're repeating
yourself."* PATCH-001 T3 had already widened the guard to the last 6 turns
"regardless of interleaving" because every live dup pair had a user row
between the copies — which IS the interrupted case. The exemption was the
hole in T3, not a decision; removing it completes T3 and leaves
RECOGNITION-VARIETY's marked-first-record behaviour intact.

**Re-reading a question the room talks over.** Seven guns can re-speak the
same `q_N`, all descended from "silence is worse than asking twice". A cut
delivery released its claim, re-armed through `expect_delivery`, re-read the
whole sheet, got cut again — no cap. An interrupted delivery whose aired
text already presented the question now confirms and opens the window
(reusing `_delivery_text_matches_armed`, which the delivery path already
owned and simply never got asked at the interrupt). `interrupted` only — a
suppressed turn aired nothing, and confirming there would reopen the #3418
ghost-window hole. Two cut re-airs, then back to supply.
`_REGEN_DELIVERY_DIRECTIVE` said "exactly as written, in one unbroken beat";
now it resumes from the break.

**Recognition at sixteen turns.** He said "My name is Rami" at 22:46:04 —
twenty-two seconds in. Recognition landed at 22:49:15, **3m31s and sixteen
player turns later**, through "I have met you a million times", "you still
don't remember me", "I just told you my name. You forgot my name already."
The only door open was the ECAPA matcher, and the matcher was behind a cold
model load: the Dockerfile baked ECAPA to `/tmp`, which container runtimes
mount as tmpfs — shadowing the baked copy — and nothing forbade the Hugging
Face round-trip `from_hparams` makes even when every file is cached. Savedir
moved to `/app/.cache`, `HF_HUB_OFFLINE` defaulted on, and the image now
proves the offline load at build time.

New **name-stated door**, built weak on purpose: an unambiguous single group
only (two tables with a Rami resolve to neither), it does not set
`device_identity_verified` so the matcher still runs, and `name_stated` stays
out of `_STRONG_GROUP_SOURCES` so a voice still outranks a name. N5 in the
correct direction.

**The steamroll.** 22:49:15 was ONE turn carrying the recognition beat, the
offer "want a quick refresher on the options, or straight in?", and the
question. She asked him what he wanted and answered it herself; he said so
at 22:49:37. P0-5's unowned-kickoff gate is widened to the question itself —
the thing it was really protecting — so a turn that does not own
`q_N_delivery` cannot speak the armed question, and the offer has to wait.

**Narrating her own internals.** 22:47:50: *"my first memory check came back
blank, and my setup treated 'unknown' like 'nothing on file' instead of
waiting for the lookup to finish — that was a bug in how I handle the start
of a session."* A release note read aloud to someone who came to play
trivia. The 08-08 honesty fix stopped her denying mechanisms and diagnosing a
blank card; she started anatomising them instead. Owning a fault is one
sentence.

**Also:** `lily_voice_identity.updated_at` had a DEFAULT but no trigger and
was never written on update, so it froze at insert — reading as "enrollment
stopped" while `sample_count` climbed 4 → 7. (Enrollment was working; the
column lied.)

**Open, under investigation, not closed:** ~110 seconds of dead audio at
session open (agent published 295s of TTS with `tts_ttfb` p50 412ms and
`playback_latency` p50 0.3ms; he heard nothing until 22:47:42 and never
clicked anything — needs the LiveKit track publish/subscribe timeline);
`e2e_latency_ms` p95 **13,610** with `llm_ttft` p95 7,301 (TTS is not the
problem); and a broader UI-sync audit beyond the phase-hold deadlock.

42 new fixtures across `tests/test_bargein_is_normal.py` and
`tests/test_name_stated_recognition.py`; full suite 1795 passed.

## 2026-08-09 — P0-5: One start owner

Live `lily-BE8D8B-a19913e2`: free LLM kickoff fragments ("Round",
"Let's do it") aired before the keyed Q1 delivery while setup was pending.

**Fix.** TTS suppresses standalone kickoff/category fragments unless the
same turn structurally owns `q_N_delivery`. Suppression is terminal silent
playout—not an empty-candidate retry—so debris cannot regenerate. Full
question deliveries remain unchanged.

## 2026-08-09 — P0-4: Late recognition at seams only

Live `lily-BE8D8B-a19913e2`: recognition landed after Q1 opened and spoke
"NOW I've got you / refresher / usual" over the Greece question.

**Fix.** Late recognition defers across delivery claim/pending playout,
open window, adjudication, clarify, host speech, and question transition.
The pending beat flushes once at the reveal/round-score completion seam,
before N+1; it never consumes the once-token while blocked.

## 2026-08-09 — P0-3: Explicit adult consent consumes the gate

Live `lily-BE8D8B-a19913e2`: the player said "above 18," "I'm 43," and
"born in 1983"; the old yes-shaped regex missed it, then Lily re-asked.

**Fix.** Declarative above/over-18, adult first-person age, and
unambiguously adult birth year latch consent (questions, negations, minors,
and boundary birth years remain false). The spoken latch—not the model
boolean—is authoritative for `lily_enter_adult_mode`. Confirmed consent rides
the state block with a do-not-re-ask rule.

## 2026-08-09 — P0-2: Multi-intent setup before start

Live `lily-BE8D8B-a19913e2`: "I want to play" won the LLM/tool race while
the player was still speaking an 18-second voice + adult + pictures + heat +
age setup turn. A general-picture Q started before setup landed.

**Fix.** A non-exclusive parser records every setup intent before command
dispatch. `start_blocked_reason()` now blocks on `user_speaking` and
`setup_pending`. Voice/adult/picture/heat jobs clear only after successful
tool/state mutations; adult-picture setup cannot activate the general image
partition. State block lists pending jobs and forbids Round One.

## 2026-08-09 — P0-1: False clean slate impossible after returner claim

Live regression `lily-BE8D8B-a19913e2`: "I certainly have been on your
table before" missed the returner detector; after the lookup resolved empty,
Lily aired "completely clean slate — no saved voices or past games."

**Fix.** The live returner phrase is detected and persisted for the entire
session (`_returner_claim_seen`). A returner claim permanently makes
`can_claim_empty_memory()` false. The TTS choke now covers `no saved voices`,
`no past/prior games`, and `nothing saved`; the exact 22:03 line rewrites to
the existing still-checking sheet even after an empty lookup resolves.

## 2026-08-09 — One-owner delivery: near-miss confirms, no re-read

Gun 3 (`NUDGE_NEAR_MISS`): table already heard a ≥0.9 similarity Q, then
`question_nudge` full re-read → double question. Same trap via
`UNDELIVERED_REFIRE` when ratio was already high.

**Fix.** `force_confirm_delivery_heard` claims+confirms `q_N_delivery` and
opens the window. Window-fallback and undelivered reconcile take that path
when ratio ≥ 0.9 — no sheet re-air. README: delivery re-fire triage table
(`source=` → gun).

## 2026-08-09 — B4: image_shown speak gate

Not drawn ≠ on screen. TTS rewrites "look at the screen" / "picture is
up" unless `lily_control.image_shown` confirmed the armed URL. Pending
sheet while waiting; didn't-land sheet after timeout. Need-to-know image
flag matches confirm state. Open-window arms pending confirm.

## 2026-08-09 — B3: Metadata image_url lifecycle

Reveal re-published the prior question's `image_url`, so the picture
stayed on the glass through verdict / arm N+1 / wrap-up.

**Fix.** Reveal publish clears `image_url` (choices/category intact).
Empty-image publishes clear `_glass_image_url` confirm. Finale
`publish_metadata("")` so wrap-up is pictureless.

## 2026-08-09 — B2: Pictures ON refreshes supply from seeded arsenal

After `media_mode` flipped to pictures, a stale voice-only `next_question`
blocked prefetch (`start_prefetch` early-return), so the stocked arsenal
never drew. Signing failure could also leave a storage path as `image_url`.

**Fix.** `try_activate_pictures` drops pictureless prefetch and relaunches
supply. `_arsenal_picture_draw` requires a signed http(s) URL before
return; logs `LILY_SUPPLY | PICTURE_DRAW | id= url=yes|no mode=`.

## 2026-08-09 — Pictures: flip media_mode when the bank is ready

Live (`lily-E66E1B`): adult + "pictures in mixed mode" set heat to `mix`
but left `media_mode=voice_only`. Lily correctly grounded "lane healthy,
not live" — the arsenal already had ready adult rows; the sticky flag
never flipped, so supply never drew.

**Fix.** Shared `try_activate_pictures` (dependency-checked). Spoken
detector covers "pictures in mixed mode" / "images live". Adult
`lily_set_adult_image_intensity` flips pictures ON when the lane is
healthy. After her "want them on?" offer, short yes / "live immediately"
flips the same path. Heat alone no longer implies pictures without the
flag.

No prompt polish. Arsenal seed unchanged (already stocked).

## 2026-08-09 — WO-2: Ambiguous yes ≠ start

After Lily offers an A-or-B (ready vs waiting, voice-only vs chase
pictures, refresher vs start), a bare "yes / yeah / yes I am / yes ma'am"
answers the choice — it must not open round one.

**Arm.** `note_or_choice_offer` on agent speech finished when
`lily_detect_or_choice_offer` hits. **Lock.** Bare affirmative via
`note_user_start_intent` sets `ambiguous_yes` on `start_blocked_reason`.
**Choke.** `start_game`, lobby auto-start, and `lily_begin_round` all
defer/refuse until explicit start language (`start_game` command /
let's start / let's play / dive in). Conversational choice replies
consume the offer without locking.

No prompt polish. No extracts. Audeering parked.

## 2026-08-09 — P0: Ban false clean slate + recognition dispute lock

Live trust-killer: returner said it was not their first time; Lily aired
"clean slate / no saved stats… on file" while the voice-identity probe
was still outstanding; ~50s later the match landed ("NOW I've got you").
He asked why; she agreed instead of answering and drove toward kickoff.

**P0-A.** `can_claim_empty_memory()` — clean-slate / nothing-on-file
language only when identity is resolved empty (no memory block, no device
candidate, probe not outstanding). State block injects
`identity: STILL CHECKING…`. `tts_node` rewrites false empty claims to
the still-checking sheet whenever absence is unsettled (not only when a
returner note is armed). Detector covers the live phrases
("no saved stats", "completely clean slate", …).

**P0-B.** `recognition_dispute` locks `start_game`, auto-start, and
`lily_begin_round` until the why-beat lands. Armed on returner claim
while unsettled, or on explicit clean-slate / "why still pulling" challenge.

**P0-C.** One-shot why directive in the state block; sycophantic
"you're right" openers rewrite to one grounded why sentence while the
dispute is open. Why-answered unlocks kickoff when he asks.

No prompt polish. No extracts. Audeering parked.

## 2026-08-09 — F1: Lobby empty-STOP opener recover

Fail-closed on empty LLM STOP is correct for the dead handle, but two
empties at cold open left the room mute (checkers: broken agent). After
`EMPTY_STOP_FAILED`, schedule **one** inventory-keyed `session_greet`
(or `session_rejoin`) re-dispatch via `gated_say` — existing instructions
only, no new copy. Cap = 1; skip when the opener is already CONFIRMED.
Log `EMPTY_STOP_LOBBY_RECOVER`. Does not arm a second cut-recovery path
(keyed release already owns re-dispatch).

## 2026-08-09 — Spine log (`LILY_SPINE`)

One operability line on each distinct glass publish:
`phase / q / delivery / window / hold / supply`. Deduped so identical
snapshots do not spam. Pure `lily_spine_line` + `LilyGame.spine_fields`
/ `log_spine`.

## 2026-08-09 — Extract speech/delivery (zero copy edits)

Main structural cut against the voice-inventory freeze. Moved
`gated_say` + stale-claim watchdog, re-air / cut recovery,
`expect_delivery` / `register_delivery_claim`, and MC abort + pre-window
+ early answer into `lily_speech_delivery.LilySpeechDeliveryMixin`.
`LilyGame` inherits the mixin so those names stay the single choke
points on the game object. Directive strings and act/key behavior are
byte-stable; tests that monkeypatched stale-claim knobs now target
`lily_speech_delivery`. Audeering stays parked. Supply / director /
identity / thin shell left for follow-on PRs.

## 2026-08-09 — Hot-path await audit (glass publishes)

Persistence writes on adjudicate / reveal / answer / transcript were
already fire-and-forget. Residual turn blockers were `await
publish_attributes` / `await publish_metadata` sitting in front of
`gated_say` or tool returns. Converted to the existing nowait /
`ensure_future(metadata)` idiom on:

- `skip_question` (skip → next delivery speech)
- `start_game` attributes (identity resolve stays awaited)
- `lily_enter_adult_mode` / `lily_award_bonus` tool returns
- reconnect `session_rejoin` (initial glass publish at entry still awaited)

Honesty awaits unchanged: adjudicate gather before verdict, custom-round
build, forget cascade, greeting memory, group identity resolve, 50/50
screen, finale events. Audeering stays parked; no module extraction.

## 2026-08-09 — Empty STOP at llm_node + voice inventory freeze

Post-PR-#12 residual P0 and the structure gate for extraction.

**Empty STOP intercept (lobby-safe).** PR #12 forced the armed question
sheet on a second empty at `tts_node`; lobby / banter / reveal-flavor
turns still had no sheet and could air silence after Gemini
`FinishReason.STOP` with no text and no tools. `llm_node` now detects
that class, retries the LLM once inline (contentful streams still
pass through without buffering), then either yields
`rendered_armed_question()` when a delivery is armed or raises
`APIConnectionError` so the speech handle fails closed and claims
release — never a silent lobby turn. Prompt copy is untouched.

**Voice inventory freeze.** `docs/voice_inventory.md` catalogs acts,
keys, voices, sheets, and say-gate surfaces at the `638dd71` baseline.
Extraction of `speech/delivery` must move code with **zero** string
edits against this inventory; Audeering stays parked.

## 2026-08-09 — Live-session repair: lobby control, answer shape, delivery

Sessions `RM_qs6YeUdkV7or` and `RM_VYp6bgALNC4L`: the table watched Lily
auto-start mid-banter, treat "Go." / spotlight complaints as answers,
stack questions, re-ask over side chatter, and lose a voiceprint rekey
on a returning group. Acoustic addressee (`AUDEERING_API_KEY`) is parked.

**Auto-start will not fire under active lobby talk.** Grace alone was not
enough — both rooms flipped into game mode while players were still
collecting facts / correcting names. Default lobby grace is now 90s, and
a new quiet-after-last-user-turn gate (`LILY_AUTO_START_QUIET_SECONDS`,
default 20s) plus a host-speaking hold keep the safety net off until the
table actually settles. Explicit `lily_begin_round` / UI start are
unchanged.

**Procedural imperatives and mark-less host questions are not answers.**
`"Go."`, `"Continue."`, `"Next question."`, `"Go ahead."` join the
non-answer filter as `procedural`. Interrogatives aimed at the host no
longer require a terminal `?` (STT drops them), and game-talk patterns
cover `"you guys said…"` / `"pointed at me"`. Answer-surface override
still wins, so a murmured real answer inside a complaint keeps scoring.

**Stacked freeform questions yield; undelivered re-fires wait for quiet.**
Only MC deliveries (stem + options) stay exempt from
`lily_yield_after_first_question` — verdict-plus-next-question stacks and
freeform deliveries clip at the first `?`. The undelivered-delivery
watchdog holds re-asks while the table is mid-turn
(`LILY_UNDELIVERED_REFIRE_QUIET_SECONDS`, default 8s).

**Empty Gemini completions force the armed sheet.** A second consecutive
empty/junk TTS candidate during an armed delivery speaks
`rendered_armed_question()` instead of leaving dead air
(`FinishReason.STOP` class from RM_qs6).

**Voiceprint rekey merges on label conflict.** Upgrading a provisional
session/name-hash id onto an existing `grp_*` that already has `S1`
merges identifiers and deletes the provisional row instead of
`REKEY_FAILED` on the unique `(group_id, speaker_label)` constraint.

**`lily_answers.cause` / `utterance_id` strip one optional column at a
time.** A mixed-migration database keeps whichever column has already
landed. Operators should still apply `migrations/024_lily_answers_utterance_binding.sql`.

## 2026-08-08 — WO-LILY-HOTFIX-006: identity race, custom rounds, question binding

Three consecutive three-player sessions (Rami, Rhonda, Chris) —
`lily-16A9AE`, `lily-4FB3B2`, `lily-D99BE7`. Deployed sha at session time:
**2c8ecf52**. Every database claim below verified against prod before any
code was written, and every defect reproduced as a failing test first.

**N1 — the greeting raced the identity match.** `lily-4FB3B2` carries
`[RETURNING TABLE] … 12 game(s) played` landing ~2.5 minutes AFTER Lily had
already said "tonight is actually a clean slate". The matcher was never
wrong; it was not waited for, and its absence was narrated as a fact. The
root cause was not the budgeted await: the CLAIMED RETURNER beat under the
neutral branch instructed her to "name the gap plainly", which is itself a
memory claim, fired the moment a player said "it's not my first time". New
`identity_probe_outstanding()` — while a probe is unresolved, no line may
characterise memory at all. "I have no memory of you" and "I do not know YET
whether I have memory of you" are different statements and only the second
was ever true at greeting time.

**N2 — custom rounds were narrated, not built.** The operator asked for Cape
Cod; the six `lily_asked_history` rows were Gold, Vatican City, Psycho, One
Direction, rupee, Rehab. The mechanism was worse than the symptom:
`lily_set_category` returned its confirmation SYNCHRONOUSLY before any
question existed, and `lily_fetch_bank_question`'s third fallback stage
`(None, None)` DROPPED THE CATEGORY FILTER ENTIRELY — so the compounding-
arsenal optimisation silently rewrote "build me a Cape Cod round" into
"serve me anything", and generation never ran. The confirmation line now
generates only from a non-empty registration result, registered before
anything is spoken, with `CUSTOM_ROUND_DIVERGENCE` as the X1-shaped net.

**N3/N4/N9 — the adjudication boundary.** Seven confirmed rows of
meta-speech entered as answers, including "Um. Why are we in Mumbai or
Delhi?" adjudicated **CORRECT, one point** — a player scored for complaining
about the topic, having said aloud "we're not talking to you". Answers filed
against the wrong question. And the Jupiter case: Rami said "It's Jupiter",
Lily said "Jupiter was spot on", and the ledger recorded "Go." — his start
command — as his answer, marked wrong.

The fixtures exposed the live mechanism nobody had named: a stale candidate
ABSORBED the next window's answer as a "revision" and inherited the old
question id. Question identity is now captured at window-open and carried to
the ledger; `utterance_id` binds the verdict to the utterance that actually
arrived; late-but-correct is a defined outcome with a grace margin or an
announced miss; and a spoken verdict generates from the committed row.

**N12/N8/N13.** Two lanes narrated the same transition with opposite
verdicts while question four was delivered over question three's reveal — a
transition is now one journaled event with one owner. A revealed answer can
no longer open a steal window. Spoken player counts are read from roster
state, not generated ("whenever you four" to a table of three).

**Migrations 023 and 024** (asked-history category; answer utterance
binding) applied to prod. Both nullable with no default — pre-migration rows
genuinely do not know these values, and backfilling a guess is the invented
certainty this WO exists to remove.

**Not closed:** N5 is partial (the biometric-outranks-hash guard is
defensive; the name-hash fallback and name-keyed voiceprint lookup remain,
so one misheard name still defeats both paths). N11, N6, N7 and N10 are not
started.

## 2026-08-08 — Live-session repair: memory, honesty, latency, and the voiceprint

Session `lily-1D27C8` (14:56 local): a returning operator was greeted as a
stranger, told his memory was "strictly device and browser based", and heard
her turns die mid-sentence. Four independent defects, all found from the
live logs and the run tables.

**The voiceprint was being SHREDDED, not lost.** `_voice_identity_enroll_at_close`
wrote to `self.group_id` unconditionally. When group resolution had fallen
back to the room name, that minted a fresh **1-sample orphan centroid**
instead of adding to the real one. An orphan keyed to a room name can never
be matched TO — that room never recurs — so it survives only as an extra
candidate thinning the margin check for every genuine match afterwards.
Three rows existed where there should have been one: `grp_0b07f989` with 4
samples from 08-07, plus 1-sample orphans from 07:30 and 18:59, both written
while the operator was telling Lily she ought to know his voice. The
embedder was never broken — torch loaded, model loaded, 8s probe captured,
embedding extracted, row written. It was writing to the wrong key.
Enrollment on a room-name group now matches FIRST and folds the sample into
whatever identity the voice actually matches; no match means no write. The
guard is narrow on purpose — a real group id off participant metadata
recurs, so it may still found its first centroid.

**Choppiness: the frame sink never stopped.** `_lily_voice_probe_fork` ran
for the WHOLE session — per participant, resampling every audio frame and
doing two full copies of it (`bytes(frame.data)` -> `array`) on the event
loop — for audio nothing would ever read again. The probe needs ~8 seconds;
enrollment reads the captured PCM, not the live stream. That waste shares
the event loop with the Silero VAD, which drives barge-in and turn commit.
Measured live: VAD **24.9s behind realtime**, TTS tail chunks undelivered
(`delivered=0/1`), turns dying mid-sentence. The sink now closes once the
probe is full.

**A 5-second wall on the adult vocal lane.** `livekit-plugins-openai` builds
its own AsyncClient with `httpx.Timeout(read=5.0)` and `with_x_ai` exposes
no timeout parameter. On a streaming response the read timeout is the gap
between chunks, and the first gap is the model's thinking time — against
grok-4.5 measured at llm_ttft p50 4,999ms / p95 8,254ms, with
`max_retries=0`. A coin flip at the median, a certainty at p95, and a killed
turn is dead air. This reframes HOTFIX-005 X13: effort tiers were partly
being used to dodge a timeout nobody knew was there.

**The Tier-2 judge ran on the wrong provider.** `judge()` used the Gemini
vocal model unconditionally, including mid-adult-session — sending adult
content to the one provider the session had already swapped away from.
PROHIBITED_CONTENT is not a settable safety category, so it blocked, the 12s
bound fired, and adjudication silently degraded to Tier-1 on every close
call.

**She was not lying about her memory — she was following instructions off a
cliff.** The prompt says never NAME mechanisms beyond device-and-voice; she
converted that into a claim about what EXISTS, asserting a negative about a
backend she cannot see, to the person who built it. Three sites also shipped
a scripted guess ("new device, maybe") that she delivered as diagnosis — and
here it was wrong, blaming a returning player's own setup for a backend
fault. She now says the true thing: the card is blank, the gap is hers, and
she does not know why.

**Also:** the arsenal content gate passed no `safety_settings`, so it ran
Gemini under default thresholds and could not see adult images at all — 30
gate rejections against 1 provider moderation rejection, at ~$0.02 a frame.
The group-id resolver stopped breaking its metadata poll the moment a
participant was present without metadata (presence and metadata-readiness
are different things). Run summaries now persist per-slot skip reasons
rather than printing them to a terminal that scrolls.

## 2026-08-07 — WO-LILY-ARSENAL-SEED-001: stock the picture bank

`lily_picture_arsenal` had the applied schema, the private `lily-arsenal`
bucket existed, and every partition was empty — zero rows, zero usage. The
supply logic was specified in PATCH-003 P2 and the shelf was built; nothing
was ever put on it. An empty arsenal degrades *silently* to live
generation, which is exactly the dead air it exists to prevent: the
operator hit **67 seconds of silence on 2026-08-07** waiting for a picture
question.

**Root cause found during the work — the shelf could not have been stocked.**
The in-session replenisher pumped
`prefetch_picture_question(kind='real_or_imagined')`, and every
real-or-imagined question carries the *same* spoken stem ("Eyes on the
screen. One photograph, one question — is it real, or imagined?"). Every
generated pair therefore hashed to the same `question_text_hash`, and
`lily_arsenal_insert`'s dedup rejected all but the **first row per
partition**. The bank was not merely unstocked; the code meant to stock it
was structurally capped at one entry per partition, forever.

**A1 — entry anatomy.** Migration `022_lily_picture_arsenal_entry.sql`
completes the row: `format`, `options`, `is_real_image`, `image_source`,
`binding_direction`, `subject_area`, `difficulty_tier`, `reveal_color`,
classifier/gate/review columns, cost and attempt counters, `run_id`.
`difficulty_tier` and `reveal_color` were already *read* by
`_row_to_question` against columns that did not exist — silently defaulting
on every row. `binding_direction` (image-first vs question-first) is
recorded per entry because correspondence failures cluster by direction.

**A2/A3 — content design** (`lily_arsenal_formats.py`,
`lily_arsenal_content.py`). Six formats, five in scope, 15 authored
exemplars across three partitions; 18 subject areas per partition, weighted
difficulty spreads, and a deterministic spread planner
(`lily_plan_entries`) that continues rather than restarts on a top-up run.
`odd_one_out` is out of scope — single-shot models do not reliably produce
a legible multi-panel grid with exactly one rule-breaker, and the failure is
*silent*. `real_or_imagined` is excluded from the adult partitions because
`LILY_ADULT_IMAGE_STYLE` pins them to comic-book illustration, which gives
away the answer to "is this a real photograph?".

**A4 — generation** (`lily_arsenal_gen.py`). Every image goes through
`lily_imagegen.lily_generate_image_bytes` at the same mode and intensity
live generation uses — the arsenal gets no looser route to a provider. The
outbound classifier is **register-aware**: the existing web-image gate is
hardcoded family-friendly and would have refused every adult entry, leaving
those partitions permanently empty in a new costume. The structural floor
(no minors, nothing non-consensual, nothing outside legal hard limits) is
hardcoded into every register at every heat, and the gate fails closed.

**A5 — dedup and the quality gate.** Exact hash plus a similarity pass
(ratio 0.82) against both the arsenal and `lily_questions`. Entries land
`generating` and are **promoted**; adult partitions default to operator
review.

**A6/A9 — the seeding job** (`lily_arsenal_seed.py`). Standalone,
operator-runnable, idempotent (sized off ready + pending-review, so a
re-run under a review gate creates nothing), concurrency-safe via a partial
unique index on `lily_picture_arsenal_runs`, resumable via heartbeat
staleness, with a run summary that names **why** anything was skipped.
Provider moderation is an expected outcome: bounded prompt reworks down a
heat ladder that keeps the slot's subject, then skip-and-count, with the
rejection rate reported as a finding when it crosses 40%.

**A7/A8/A10 — draw, refill, observability.** The arsenal bucket is private,
so rows store a path and it is signed per serve inside a gate-cleared
session. The watermark is a **ratio** (40% consumed), not a count — a
hardcoded "fire at 4" becomes "fire at 80% empty" the moment depth drops to
5 — and it now also fires on *availability to the playing group*, since
entries never deplete globally and the old rule would read 10/10 while the
table in front of you ran out. `ARSENAL_LOW` is raised at the exact moment
a draw finds every candidate partition empty.

**Guardrail preserved.** Arsenal generation lives on the reasoning node
(`LilyReasoning.generate_arsenal_entry`); the vocal path still references
neither `lily_search` nor the image stack.

**Known divergence from the work order.** A4 asks that answer sets ride
`additional_vocab`. They do not — that slot is pinned by WO-LILY-STT-001 to
the assistant name and player names ("never answer nouns"), with a test
asserting it. Answer sets do their work in `acceptable_answers`, which the
Tier-1 evaluator already matches phonetically.

Depth, watermark ratio, gate mode, per-image cost, moderation retries and
real-image availability are all env knobs (`LILY_ARSENAL_*`), forwarded in
`deploy.yml`. Full suite green: 1537 passed.

## 2026-08-07 — WO-LILY-HOTFIX-005: score integrity, reasoning model, delivery & display

Session `lily-FFDEAE-ba016154` (~14:31–14:56): the spoken score was
fabricated, the reasoning model string was wrong (67s dead-air), a verdict
aired for a question never spoken, and four generated pictures never
reached the glass. Tiered fix; T1 shipped first.

**T1 (Tier 1)**

- **X1 — score integrity (lead defect).** The spoken score is now read from
  the committed ledger at generation time and injected as a hard read-only
  state field (`SCORES — AUTHORITATIVE, READ-ONLY`); the model may never
  compute, infer, or carry a score forward. The glass already projects the
  same `ledger_scores()`, so state-block and glass agree by construction. A
  narrated-divergence detector (`lily_narrated_score_divergence`) raises
  `SCORE_DIVERGENCE` at ERROR when a spoken score matches no ledger total.
- **X2 — reasoning model string.** `grok-4.2` (truncated, nonexistent → 400
  "Model not found" → dead air) corrected to `grok-4.20-multi-agent`, routed
  to the xAI Responses API (`/v1/responses`) that the multi-agent tier
  requires.
- **X3 — no reveal without delivery.** Adjudication (the verdict/reveal act)
  is refused and logged (`LILY_REVEAL | REFUSED_NO_DELIVERY`) for a question
  whose delivery never reached playout — the reveal-side mirror of the
  delivery-registration guard. Proof of playout is any of: the answer window
  open now, the `q_N_delivery` claim CONFIRMED (the ARMED_LIMBO recovery
  signal), or a durable `_delivered_to_playout` record (`open_window`
  populates it). The 14:53:34 "on the question: false" fixture is
  unreproducible.
- **X4 — generated images reaching the glass.** A per-`(session, question)`
  generation memo stops the double-spend for slots without a bank row
  (roi_0009 generated twice, 19s apart). A render-confirmation channel
  (`lily_control.image_shown` RPC, fired from the frontend `<img>` onLoad)
  records the confirmed URL so "the picture is up" is a readable, grounded
  state (`PICTURE ON GLASS: CONFIRMED / NOT confirmed`) instead of an
  assumption.

**T2 — delivery and display integrity**

- **X5 — dropped TTS tail chunks.** Each chunk is a separate ElevenLabs
  POST; a pooled-connection teardown mid-chunk dropped the tail to the
  cut-recovery backstop as the routine path (`TAIL_CHUNK_UNDELIVERED
  delivered=0/1`, 3× in the session). A chunk whose stream is torn having
  pushed ZERO bytes is now re-fetched once in-place — a clean re-fetch with
  no audio-duplication risk — keeping the tail on the primary path. A
  partial push or a second failure still falls to cut-recovery; a genuine
  barge (CancelledError) still drops the tail as intended.
- **X6 — stale glass.** Published room metadata now stamps the
  `question_number` it projects; the frontend logs a stale-render warning
  when that diverges from the active attribute question number (displayed id
  ≠ active id). The glass already renders the same agent-pushed projection
  as the spoken lane with no independent client state, so this makes the
  desync readable rather than silent.
- **X7 — explicit-heat moderation is a first-class outcome.** A provider
  content rejection ("Generated image rejected by content moderation",
  safety block, PROHIBITED_CONTENT) is classified as REJECTED, not
  ATTEMPT_ERROR — the classifier only matched "safety" before, so a routine
  top-of-dial refusal read as a system fault. Rejections fall back
  pictureless (the existing None path) and log per partition, so the
  explicit-tier rejection rate is a readable tuning signal.

**T3 — recognition, telemetry, infrastructure**

- **X8 — phantom speaker in a solo session.** The roster-aware max_speakers
  cap (`roster + 1`; solo ⇒ 2) is wired to a game-start live STT swap that
  shrinks the construction fallback (7) to the real table size, killing the
  phantom [S2]. Behind `LILY_STT_ROSTER_RETUNE` (DEFAULT OFF — the reconnect
  is STT-001 Q4's to validate); the cap math is proven by fixture regardless.
- **X9 — split-utterance remedy.** The runtime named it live ("consider
  raising min_delay in the endpointing options to accommodate a slow stt").
  `min_endpointing_delay` raised to 0.6 (from the 0.5 framework default) so
  the enhanced-point STT (max_delay 1.5) delivers its final transcript
  before the turn commits; the ceiling stays at the framework default. Set
  with the Speechmatics max_delay, not against it.
- **X11 — assessment JSON parse.** Lenient extraction via `raw_decode` from
  the first `{` (trailing prose no longer poisons the parse), one repair
  retry, then a terminal `report_status='failed'` so the reconciliation
  sweep stops re-running a permanent parse error forever.
- **X12 — two conversational gaps.** Detectors for explain-on-request (a
  player asking for the ACTIVE question in plain words) and verdict-contest
  (asserting they were misheard / were right) arm one-shot state-block
  directives: the reply restates the question before proceeding, or gives
  one grounded re-check against the committed record (correct via the
  scoring tool or state why it stands) instead of "we're past that".
- **X10 — VAD slower than realtime (report).** `silero: inference is slower
  than realtime, delay 0.367` (14:33:21) coincides with the two frame sinks
  added since (voice-identity probe, camera video-in) competing for the same
  CPU slot as the adult vocal lane's ~5s-TTFT reasoning. Recommendation
  recorded for ops: size the deploy slot with headroom for the VAD forward
  pass plus the optional frame sinks, or gate the sinks off on a starved
  slot; no code change lands here — it is a provisioning decision.

**X13 — reasoning effort per lane (agent-count dial on `grok-4.20-multi-agent`).**
`reasoning.effort` is an agent-count dial, not thinking depth: `low`=4-agent,
`high`=16-agent — a 4× fan-out on latency AND spend. Effort is matched to
task, not lane importance:

| Lane | Model | Effort | Why |
| --- | --- | --- | --- |
| Adult vocal (front-facing) | grok-4.5 | `medium` | Latency-critical; `high` cost ~5s TTFT (p50 4,999ms / p95 8,254ms in lily-FFDEAE). Non-adult vocal TTFT ~1,102ms for reference. |
| Adult question generation | grok-4.20-multi-agent | `low` (4-agent) | Writing one trivia line is a TCP-vs-UDP-class task; 16 agents is disproportionate in time and spend. |
| Hard synthesis (future) | grok-4.20-multi-agent | `high` (16-agent) | Reserved for tool-enabled current-events / corpus building where the arsenal absorbs the wait. |

`xhigh` is accepted but its exact agent-count/cost mapping is unconfirmed —
treat as ≥16-agent, not for use until measured. Effort is a spend multiplier;
record measured cost per question when the multi-agent lane is exercised.

Regression fixtures: `test_hotfix005_score.py`, `test_hotfix005_x3_reveal.py`,
`test_hotfix005_x4_images.py`, `test_hotfix005_x11_assess.py`,
`test_hotfix005_x8_x9_stt.py`, `test_hotfix005_x12_convo.py`,
`test_hotfix005_x5_x6_x7.py`, `test_adult_grok.py`.

## 2026-08-07 — Current-deploy follow-up: pre-generation answer ownership

Post-deploy session `lily-F7E113-90b61665` proved the transcript event and
LiveKit's `on_user_turn_completed` hook have no guaranteed ordering. The
organic reply could start before the event callback marked a correct answer
as adjudication-owned, producing organic praise + deterministic reveal and
letting an invented next question leak between them.

- `on_user_turn_completed` now runs its own cheap Tier-1 check against the
  live question and raises `StopResponse` before default generation, whether
  the transcript event landed first or not. Event/prehook reservations
  consume each other so a repeated answer cannot inherit a stale suppression.
- Delivery intent is bound to explicitly dispatched SpeechHandle ids.
  Recognition/preferences speech can no longer consume `q_N_delivery`.
- Post-reveal and nudge handles are physically rewritten to the exact
  deterministic question sheet on first pass. Previous-answer celebrations
  cannot prefix the next question.
- While a claimed returner's card is still resolving, outbound claims that
  deny recorded history or announce a clean slate are rewritten to the
  grounded “I believe you; my card hasn't connected yet” line.

Regression fixtures come directly from the deployed Freud/Uranus/Gold/
Franklin trace: event-order inversion, correction turns, q1 ownership on a
recognition beat, and Gold commentary contaminating the Franklin delivery.

## 2026-08-07 — Persistent voice memory activation and answer-routing cleanup

The durable voice-identity design existed in code but was inert in the
production path. Three independent activation gaps are closed:

- Migration 021 (`lily_voice_identity`, pgvector centroid storage) is now
  applied in production with RLS enabled; service-role code is the only
  access path, and `forget me` retires the active centroid.
- The production image installs CPU-only torch/torchaudio plus SpeechBrain
  and prewarms ECAPA at build time. A missing model now fails the image
  build instead of silently reducing recognition to device-only memory.
- The voice-probe audio fork is registered independently of the optional
  audEERING pipeline. Capture and matching no longer disappear whenever
  acoustic analytics is unavailable.
- CI now applies migrations against a pgvector-enabled PostgreSQL 17 image
  and builds the full production container on pull requests, including the
  ECAPA preload assertion.

The first finalized utterance also no longer consumes the session's only
biometric match attempt before enough PCM exists. Matching remains
retryable until the bounded probe is ready and starts immediately at that
point; close-time centroid enrollment stays inside the shutdown gate.
Verified vendor voiceprint names now seed a names-only returning-table
block when a short prior session enrolled the player but did not cross the
game-memory write threshold—recognize the name without inventing a prior
winner, score, or game.

Related surgical fixes: near-verbatim/incomplete question performances are
rewritten to the authoritative sheet before first playout (no second nudge
read); a validated answer shouted during delivery is definitionally
host-directed even before the answer window opens; and synchronous test
fixtures no longer leak an unawaited attribute-publish coroutine.

## 2026-08-07 — Voice-game turn-taking: barge-in, single acknowledgments, and ordered rounds

Player feedback exposed a shared root class behind several “bizarre”
hosting habits: framework interruption thresholds, the quiz engine's
closed pre-window, and parallel organic/deterministic speech paths each
owned part of the same turn. The repair makes the ownership structural:

- **Short answers can barge in.** The interruption floor moved from 0.8s
  to 0.25s (`LILY_INTERRUPTION_MIN_DURATION`, clamped to at least 0.1s),
  so “B”, “Mars”, and “Hydrogen” are not excluded before LiveKit's
  adaptive detector runs. False triggers retain the existing
  pause-and-resume protection.
- **Freeform answers abort the read.** The MC answer-aborts-read path now
  covers Tier-1-correct freeform shouts too: interrupt the active speech,
  seed the pre-window buffer even if the framework released the delivery
  claim first, open the answer window, and adjudicate without re-reading
  the question.
- **One answer, one acknowledgment.** A correct-answer transcript marks
  its turn as deterministically owned; `on_user_turn_completed` raises
  LiveKit `StopResponse` for that exact turn, preventing the parallel
  organic “Correct!” response. Normal mid-round questions emit only the
  short committed verdict beat—no redundant reveal flourish.
- **Asking yields the floor.** The outbound speech gate physically ends
  conversational turns at the first completed question. Stacked asks and
  question-then-explanation monologues no longer talk over the table.
  Authoritative game deliveries are exempt so MC options remain intact.
- **Round transitions are ordered by playout.** Normal verdicts use
  `q_N_reveal`; round/final verdicts use `q_N_verdict`, while
  `round_N_scores` owns the transition. The next round's first question
  cannot queue until the standings flourish actually finishes.
- **Category switches cannot resurrect stale supply.** A cancelled
  prefetch that returns after its final await is discarded when its
  captured category no longer matches the round's authoritative category.

Regression coverage spans interruption configuration, freeform mid-read
answers, exact-turn organic-reply suppression, physical yield after a
question, stale category coroutine completion, one normal verdict beat,
and per-SpeechHandle round ordering. Full suite: **1450 passed**.

## 2026-08-06 — WO-LILY-PATCH-001: evening-defect pack (sessions 89A97A/48630B/05AAC9/105865)

Ten tactical fixes on the current architecture (each marked
`RETIRE_WITH_WS6` where OMNIBUS-004's journal reducer supersedes it),
fixture-first from the four live evening sessions.

- **T1 — retry re-airs killed.** The never-aired watchdog false-fired on
  turns that PLAYED ("Nobody…" ×3, "Hey…" ×3, the doubled 18+ prompt).
  It now re-verifies playout-start truth (agent_state→speaking ledger +
  host_speaking) before any refire and force-cancels the original
  SpeechHandle so a late start can't double-air after release.
- **T2 — an answered question never re-airs.** answer_heard (adjudication
  start) / a committed verdict invalidates every outstanding delivery
  attempt for that question — watchdog, nudge, and post-reveal dispatch —
  cancelling an in-flight delivery mid-playout (Mitochondria re-read 2s
  after the answer; Saturn 4s after; Kama Sutra post-answer).
- **T3 — dup guard widened.** Verbatim repeats now match the last 6
  recorded turns regardless of interleaving (every live dup pair had a
  user row between the copies), on both the record and air paths; short
  turns exempt.
- **T4 — verdict-first acks.** The verdict word airs as its own short
  beat right after the score publish (COMMIT_TO_DISPATCH_MS telemetry,
  ~1.5s budget); the flourish/standings turn follows and never restates
  it. Live ack latency was 11–12s, causing re-answers ("Saturn" ×2,
  "Kama Sutra" ×15).
- **T5 — window hygiene.** The pre-window buffer is claim-to-open only
  (pre-claim speech can't enter a not-yet-aired question — the Mars
  fragments that ate the Socrates window); backchannels ("Yeah") and
  bare roster-name fragments ("Chris.") are logged (NON_ANSWER_LOGGED),
  never scored, with an answer-surface override keeping yes/no + MC
  honest; Rami's dropped "Socrates" now scores.
- **T6 — no spoken award without a committed row.** A failed score
  commit produces an in-character hold + COMMIT_FAILED ERROR, never the
  celebration ("Saturn is correct — you're on the board!" ×3, zero rows).
- **T7 — cross-session no-repeat, answer-level.** Bank draws now exclude
  the group's played canonical answers, not just ids/text-hashes — the
  kb_469 Mars repeat (same answer, new wording/id) is closed. (PATCH-002
  A1 extends this to group-scoped burn.)
- **T9 — abandoned-session sweeper.** A non-ended session inactive past
  15 min is force-closed (phase→ended, ABANDONED_SESSION_CLOSED WARN)
  before the report sweep assesses it — the 89A97A crash-instant ghost
  (q_3812 registered at death) leaves no active trace.
- **T10 — grounded-claims canon** (README): claims about what Lily
  perceives/fixed/enabled/can-receive are grounded in tool results and
  enabled channels; when wrong she owns it plainly, never inventing a
  cause. PATCH-002 A3 + M1 add the structural teeth.

T8 (name-binding single-writer) folds into PATCH-002 A2/M1. Pinned in
tests/test_patch001_reairs.py, _verdict.py, _window.py, _norepeat.py;
full suite 1249 green.

## 2026-08-06 — HOTFIX-003: adult deck runs on Grok (voice text + question gen)

Live root cause (session lily-105865, 21:33): `gemini-3.6-flash` refused
the spoken turn around the Kama Sutra answer — `FinishReason.
PROHIBITED_CONTENT`, Gemini's NON-overridable filter (the §11.1
BLOCK_NONE settings cannot reach it). Four blocked generations, a 4x
re-acknowledged answer, ~58s of retry stall. The deck opens; Gemini
won't write its words.

Owner directive, implemented (TTS untouched — ElevenLabs v3 remains the
only voice; these models only produce the TEXT the TTS speaks):

- **Adult vocal text → Grok 4.5** (`LILY_ADULT_VOCAL_MODEL`, default
  `grok-4.5`; `LILY_ADULT_VOCAL_EFFORT` low|medium|high, default
  **high**). Adult entry swaps the session LLM via
  `Agent.update_options(llm=openai.LLM.with_x_ai(...))`; every adult
  exit (back-to-normal, child-signal veto, gate-lost) restores the
  general Gemini node. Missing XAI key = loud degradation
  (`ADULT_VOCAL | SWAP_UNAVAILABLE`), never a blocked entry. Per the
  xAI docs, reasoning_effort accepts low/medium/high, defaults high,
  and cannot be disabled on grok-4.5.
- **Adult question generation + verification → Grok 4.2**
  (`LILY_ADULT_REASONING_MODEL` default `grok-4.2`,
  `LILY_ADULT_REASONING_EFFORT` default **high** — the multi-agent
  tier's lever; heavy-tier model id pin-able via slot secret). Gemini
  refuses to even VERIFY adult material, which would have rejected
  every good adult question. JSON-mode transport with shape addenda;
  same honest-failure contract (visible errors, bank fallback).
- New dep `livekit-plugins-openai==1.6.6`; all four pins forwarded in
  deploy.yml. Pinned in tests/test_adult_grok.py; full suite 1227.

## 2026-08-06 — Live log-audit fixes (post-deploy hygiene)

Evening log review of the freshly deployed build (session lily-05AAC9)
walked the full say-gate lifecycle live — every branch held. Two real
findings, both fixed:

- **Idle watchdog outlived the session** (`TICK_FAILED |
  AgentSession isn't running`, once per hangup): the loop only checked
  `game_over`, so its first post-close tick dispatched against a dead
  session. `stop_idle_watchdog()` now cancels the task in the close
  handler, with a `_session_closed` flag as the in-loop belt.
- **NUDGE_NEAR_MISS diagnostic** (05AAC9 q=2: `DELIVERY_NUDGE` at
  `ratio=1.00`): a near-verbatim performance the strict claim matcher
  rejected means the table likely heard the question twice (organic +
  nudged sheet-read) — most plausibly an MC read missing options. A
  nudge at ratio ≥ 0.9 now logs the exact spoken vs armed text so the
  matcher gap is diagnosable from the log alone before any loosening.

Noted, no action: ARMED_LIMBO q=3 (watchdog forced a late adjudication —
recovery worked; recurring occurrences would warrant a hunt), the lobby
preemptive-generation draft discards (latency-only), the Speechmatics
`operating_point` deprecation (upstream; plugin ≥1.6.8 migration is
bench-gated per the audio-pipeline enable rule), and the framework's
segment-synchronizer duplicate playback-start line (benign).
Pinned in tests/test_hotfix002.py; full suite green (1219).


## 2026-08-06 — WO-LILY-ADULT-PICTURES-001: adult deck picture rounds enabled (generated images -> Grok)

Operator-directed: turn ON picture rounds for the adult deck, end to end,
like the other decks. Two adult-mode picture suppressions were removed (the
adult GATE, STT/TTS, and the safety ladder were NOT touched):

- **`lily_agent._picture_kind_for_slot`** no longer returns `None` for
  `mode == "adult"` — the slot gate now keys on `media_mode` only. Adult
  sessions in `media_mode='pictures'` get the same picture slots as normal
  mode (reference `real_or_imagined` round + first-of-round `real_entity`
  landmark slots; the wager round stays text).
- **`lily_reasoning.prefetch_picture_question`** dropped the
  `mode == "adult"` early-return; only the `supabase is None` guard remains.
  It now threads `mode` into `lily_build_real_or_imagined_question`.
- **Generated adult images route to Grok:** the builder's GENERATED branch
  passes `mode` to `lily_generate_question_image` ->
  `lily_generate_image_bytes(mode='adult')` -> `_generate_image_bytes_xai`
  (`grok-imagine-image`). The `image_license_note` now names the actual
  model per deck. Web-sourced (real-photo / landmark) branches unchanged.

Tests: `test_media_mode.test_picture_slots_enabled_in_adult_mode` (slot gate
now enables adult), `test_media_mode.test_adult_mode_reaches_builder_and_
threads_mode` (prefetch reaches the builder with `mode='adult'`),
`test_imagegen.test_generation_routes_to_grok_in_adult_mode` (provider
routing) and `test_imagegen.test_real_or_imagined_generated_adult_threads_
mode_and_names_grok` (end-to-end thread + license note). Full suite green
(1212 passed, 5 pre-existing skips) on the livekit-agents 1.6.6 venv.


## 2026-08-06 — WO-LILY-CAPABILITY-RESTORE-001 (model addenda): 3.6-flash brain, Nano Banana 2 Lite, provider routing, thinking policy

Operator-directed model upgrades (override the fleet no-model-pin rule for
these). **Every model ID was verified live on the funded keys before
wiring — no blind swaps.**

**Brain LLM -> `gemini-3.6-flash`** (`lily_config.vocal_model`). Text-mode
via the LiveKit `GoogleLLM` plugin (NOT Gemini Live — Lily is Speechmatics
STT + ElevenLabs TTS with the LLM in generateContent mode; confirmed). Live
proof: chat status:ok ("OK") and a function call fired correctly
(`lily_set_category(topic="Japan")`). Streaming was ALREADY on (the plugin
streams by default into TTS) — confirmed, not re-plumbed. `previous_
interaction_id` deliberately NOT adopted: Lily's LOCAL `chat_ctx` is the
authoritative context (structural q_{N}_delivery claims, desync integrity,
floor/addressee logic); server-side chaining would conflict with those.

**Standard-deck image gen -> `gemini-3.1-flash-lite-image`** (Nano Banana 2
Lite; `lily_config.imagegen_model`). Fastest/cheapest for fun trivia cards.
Kept on the existing `client.models.generate_content` path — live-verified
it works there, so NO migration to the Interactions API was needed. Live
proof: 1.0 MB JPEG generated through `lily_generate_image_bytes`.

**Adult-deck image gen -> xAI Grok Imagine** (`grok-imagine-image`;
`lily_config.adult_imagegen_model`). Gemini refuses adult content, so
`lily_generate_image_bytes(..., mode="adult")` routes to
`POST /v1/images/generations` (new `_generate_image_bytes_xai`), fetches the
returned `imgen.x.ai` URL, returns bytes on the same contract. Live proof:
343 KB JPEG. **Read-only on mode — the adult GATE is untouched.** NOTE:
adult picture-trivia is currently gated OFF upstream by a documented
safe-for-table rule (`lily_reasoning.prefetch_picture_question` returns
None on adult mode); this routing is correct-and-ready but latent. Enabling
adult pictures is a separate behavior decision (touches the safe-for-table
rule) and was NOT made here.

**thinking_level policy** (adaptive, per call-site — never a global
default):
- Content GENERATION (categories, questions, round-building) -> **HIGH**
  (`REASONING_THINKING_LEVEL` medium->high). Live-verified HIGH + response_
  schema returns valid structured JSON (finishReason STOP — the old
  starvation trap was thinking_BUDGET, not level).
- Tier-2 adjudication of a close/ambiguous answer -> **HIGH**
  (`JUDGE_THINKING_LEVEL` low->high; the 12s bound + Tier-1 fallback
  protects the critical path).
- Reflexive hosting banter -> **LOW** (the `GoogleLLM` construction default).
- Complex/high-stakes conversational turns (disputes, adjudication
  challenges, ambiguity, multi-step) -> escalate to **HIGH** via a
  lightweight heuristic in `llm_node` (`_lily_thinking_level_for_text` +
  `_thinking_level_for_turn`) that overrides the plugin's
  `_opts.thinking_config` for that turn and restores in `finally` (the
  plugin reads it per `chat()` call). Contained — never rips out the
  LiveKit LLM integration; thinking_level affects reasoning depth/latency
  only, so a rare overlap with a preemptive turn is a latency detail, not a
  correctness bug. Gemini 3.x accepts only `low`/`high`.

**Vision/EXA availability reconciled** (so she stops blanket-denying
pictures): both `XAI_API_KEY` (vision) and `EXA_API_KEY` (real-photo
sourcing) are present + funded (live-checked) and forwarded, so at runtime
both flags are ON. The runtime `vision` flag is bound to
`lily_vision.lily_vision_available()` in the entrypoint (a truth-test
mutation guard now fails if that's ever hardcoded off — the "manifest lies,
she relays it" defect). Added a SCOPED `availability_partial` to the vision
entry: an off vision flag now says only that *looking at a shared photo*
needs the key, and affirms picture rounds + generated images still work —
it can never collapse into "pictures are off tonight."

**Tests:** full suite **1214 passed** on the isolated 1.6.6 venv
(`tests/test_model_and_thinking.py` 13 cases: model pins, thinking policy,
provider routing; plus vision mutation guards in `test_capability_truth.py`;
`LILY_ADULT_IMAGEGEN_MODEL` registered local-only in the env/deploy lint).
Zero regressions. STT/TTS config, the adult gate, and the safety ladder
were not touched.

## 2026-08-06 — WO-LILY-STREAM-INTEGRITY-002: mid-sentence cut recovery

**WS-1 root cause (from real runtime logs + transcripts, session
`lily-0BD414-ba80eb97`, retrieved via `lk agent logs --log-type deploy`).**
All three reported cutoffs = **genuine player barge-ins mid-playout**, not
a stream/chunk bug. Each cut coincides within ~40ms with a player STT
final answering the exact word she died on:

- 18:19:36 "…how does that sound to you? If" [dead] → player 18:19:41.154
  "If what?" (they heard her cut on "If" and re-poked). `handle.interrupted`
  true; recorded `chat_items` ends at "If".
- 18:21:12 "…but I can't" [dead] → player 18:21:12.235 "What? So like what
  if we want to play a lot…".
- 18:21:20 "…so we won't" [dead] → player 18:21:20.545 "How big is your
  library?".

The 1.6.6 speaking-state-no-audio ghost bug is **ruled OUT**: TTS
synthesis completed normally and repeatedly through every window; the only
two false-interruptions in the session both logged `resumed=True` at
18:17:04, nowhere near a cut; zero `GENERATION_FAILED`. The character-cap
theory is also ruled out — the framework's `StreamAdapter` sentence-splits
every reply first, so the 1,306-char capabilities reply was many small
per-sentence syntheses cut by the barge, not a >4,200 tail-chunk failure.
The real defect: **organic (keyless) conversational turns had no
cut-recovery path** — keyed game acts recover via the game loop, but an
organic turn's resumption depended on a player re-prompting.

**WS-2 — chunk-safe TTS dispatch (`lily_tts.py`).**
- `MAX_CHUNK_SIZE` lowered 5000 → **3800**, comfortably under the new
  named `ELEVENLABS_REQUEST_CHAR_CAP = 4200` (a chunk over the cap 4xx's
  mid-turn and kills the turn mid-sentence after earlier chunks aired).
- `_split_text` now breaks a boundary-less long sentence at the last
  whitespace instead of slicing mid-word; every chunk `<= MAX_CHUNK_SIZE`.
- **Tail-chunk delivery tracking** (claim-vs-delivery, maya_jrvs model): a
  chunk counts delivered only once its audio flushed; on a mid-stream
  failure the `undelivered_remainder` is recorded and logged
  (`LILY_TTS | TAIL_CHUNK_UNDELIVERED | delivered=X/Y`) so the tail is a
  visible partial-delivery gap, not silent death.

**WS-3 — cut-recovery contract (`lily_agent.py`, `lily_config.py`).**
- A cut or mid-stream-**failed** organic turn arms an auto-resume watchdog
  (`arm_cut_recovery`); after `LILY_CUT_RECOVERY_GRACE` (default 3.5s,
  above `false_interruption_timeout`) it composes a **fresh** resume from
  where meaning broke, in her honest "sorry, looks like I cut out there"
  voice (`_CUT_RECOVERY_DIRECTIVE`), within one turn, no operator poke
  (`LILY_CUT_RECOVERY | RESUMED`). WS-2's failed-tail and WS-3's barge/cut
  resolve through this one recovery path.
- **One-emission preserved**: the watchdog fires ONLY into dead air. A
  real barge carries a user turn → the normal reply path answers it
  (re-air-gated fresh) and the watchdog stands down. `note_user_turn`
  (stamped from `on_user_turn_completed`) suppresses via a user-turn
  recency guard; `note_playout_started` cancels on any new speech; a newer
  cut supersedes via a monotonic token. No double-speak over an in-flight
  reply.

**Tests:** `tests/test_tts_chunk_safety.py` (7: split correctness under
cap, no mid-word cut, tail-chunk delivery tracking) and
`tests/test_cut_recovery.py` (16: arm/fire decision matrix, one-emission
guards, async watchdog fires-into-silence / stands-down-on-user-turn).
Full suite **1172 passed** on the pinned livekit-agents 1.6.6 venv; ruff
clean (source at pre-existing baseline; no new error classes). Model and
voices untouched — chunking/dispatch/recovery mechanics only.

## 2026-08-06 — WO-LILY-CAPABILITY-RESTORE-001: on-the-fly categories + capability truth test

**Restored on-the-fly category creation and pinned a three-way capability
truth test; audited the picture/vision "regression" to ground.**

**Audit — the flipping commit for each claim.**
- *Custom category:* never a code regression. `_category_for_round` has
  been fixed family-rotation since the original wired agent (`0a502a8`),
  only ever touched by omnibus WS-6 (`1b1af93`, supply-stall visibility) —
  no operator-request topic path ever existed (pickaxe on
  `requested_category` / `lily_set_category` / "custom category" across
  ALL history: nothing). The generator already accepted any `category`;
  the wiring to steer it did not. This WO adds it.
- *Pictures / vision:* also NOT an omnibus flip. The availability gating
  predates the 1.6.4→1.6.6 rebuild — `40c71e1`
  (WO-LILY-SELFKNOWLEDGE-INTAKE-001) introduced `availability_flags` and
  the EXA-gated `pictures_real_sourcing`; `3a17144` (Zuna vision port)
  introduced the XAI-gated `vision` flag. Omnibus WS-0 (`36a7795`) touched
  neither `lily_vision.py` nor `lily_capabilities.py`. "Not switched on
  tonight" is the availability layer telling the truth when `XAI_API_KEY`
  / `EXA_API_KEY` are absent at runtime. Both are present at repo scope
  and forwarded by `deploy.yml` (XAI seeded 2026-07-31); the funded GCP
  `XAI_API_KEY` passes a live vision call. A live "off" was a
  deployed-runtime key gap — a redeploy carries the keys.

**Category creation restored.**
- `lily_set_category(topic)` (LilyAgent function tool) records a
  table-named subject on `_category_override[target_round]`;
  `_category_for_round` consults it before the rotation, so the requested
  topic reaches the generator seam (`reasoning.prefetch_question(
  category=...)`). Stale prefetch dropped and re-issued; honest
  "building your round" status note covers the build beat; NEVER a denial.
  Adult mode redirects to the general deck (deck-identity firewall).
- Manifest entry `custom_category` (v5, `LILY_FEATURE_VERSION` 4→5); option
  line "Any topic, on the fly" in WHAT THE TABLE CAN ASK FOR.
- **Live proof:** real Gemini generation on arbitrary topics — "Game of
  Thrones" → *Daenerys rides Drogon* (category="Game of Thrones"), "Japan"
  → *Tokyo was Edo* (category="Japan").

**Picture capabilities verified live.**
- Grok vision (`lily_describe_image`) analyzed a seeded public photo →
  `status:ok`, "black Labrador Retriever puppy" — funded `XAI_API_KEY`
  (team not blocked, key not disabled). Picture-trivia round is wired
  end-to-end already: `media_mode="pictures"` voice command →
  `prefetch_picture_question` → `image_url`/`image_source` on the question
  → `publish_metadata(image_url=...)` to the companion display.

**Capability truth test — permanent (`tests/test_capability_truth.py`).**
CLAIMS == manifest SAYS == ACTUALLY WORKS, failing on mismatch in EITHER
direction: every manifest-named tool is a really-registered function tool
(a lie the capability lint never sees); askable↔prompt both directions;
every `availability_key` computed at runtime from a real dependency check,
never a literal (with a vision-flag flip test); and the two regressed
capabilities INVOKED against their contracts.

**Tests:** full suite **1166 passed** on an isolated livekit-agents 1.6.6
venv (new: `tests/test_custom_category.py` 8 cases,
`tests/test_capability_truth.py` 10 cases; updated `test_vision.py` v2
delta to include custom categories). Zero regressions.

**Secrets:** none needed — `XAI_API_KEY` and `EXA_API_KEY` are present at
`JasonGordonD/Lily` scope and forwarded by `deploy.yml`. If a future
deploy predates the 2026-07-31 XAI seed, redeploy so the agent container
picks it up.

### Scope addition — generated categories persist to a compounding bank

Operator ask: an on-the-fly category should be SAVED (category + its
questions) so future rounds draw from it instead of regenerating.

**Ground truth first (prod svqbfxdhpsmioaosuhkb, verified vs Lily's
deployed SUPABASE_URL).** The bank already self-grows: `lily_agent.py`
banks every armed question into `lily_questions` tagged by category
(`_curate_generated_question` -> `lily_bank_generated_question`), and
`lily_questions` already carries the image triplet (migration 012). So
generated on-the-fly questions already persist with `category=<topic>`.
What was missing: the category was never registered as first-class, and
the supply path regenerated every time instead of drawing the topic's
banked questions.

- **Category registration (first-class, idempotent):**
  `lily_bank.lily_register_operator_category` upserts the operator topic
  into `lily_category_candidates` by name (no duplicate), marked
  `operator_requested=true` so it is first-class immediately — it does not
  wait on the use_count>=10 / >=3-groups promotion gate that model
  proposals must clear. `lily_load_promoted_categories` now surfaces
  operator categories too. `lily_set_category` fires this
  register (fire-and-forget); the round serves regardless of the write.
- **Serve-from-bank (compounding arsenal):** for an operator topic,
  `_prefetch_inner` now prefers `lily_fetch_bank_question(category)` before
  regenerating (reusing the verified `from_bank` path); generation only
  runs — and banks a fresh question — when the bank runs dry. The fixed
  family rotation still generates-first (freshness). Gated by
  `_is_operator_category`.
- **Never-lose (Cardinal Rule):** the write is additive — the live round
  never blocks on it — and a failed bank write now logs the COMPLETE
  category+question payload (`LILY_BANK | BANK_FAILED | RECOVERY_PAYLOAD`),
  not just the 80-char prompt head, so a dropped generation is recoverable.
- **Schema:** migration `020_lily_category_operator_requested.sql` adds
  `lily_category_candidates.operator_requested boolean default false`
  (additive, idempotent `add column if not exists`). Applied live to
  svqbfxdhpsmioaosuhkb and verified; no other DDL needed (question bank +
  image columns already existed).
- **Live proof:** registered "Game of Thrones" and "Japan" (the genuine
  topics Rami asked for at 18:20) via the real function against prod ->
  both rows land `operator_requested=true`, use_count 1, no duplicate, and
  `lily_load_promoted_categories` returns both. Suite 1176 passed
  (+`tests/test_category_bank.py`).

**The 18:17 picture "regression" — reconciled to ground truth.** The
operator saw a picture earlier today; the logs confirm it: `lily_image_
attempts` holds exactly ONE row ever — 05:23:49 UTC, session
lily-AAC431-6208ff7c, the "show me" DEMO picture (`lily_show_demo_picture`,
qid `demo_lily-AAC431-6208ff7c`), a GENERATED image via
`gemini-2.5-flash-image`, published to the display rail. That path is
UNGATED (generates regardless of the XAI/EXA flags). The later 18:21
denial (session lily-0BD414-ba80eb97) was the CATEGORY denial ("my general
deck is already pre-loaded... I can't") — no tool existed, now fixed. The
two are different paths with different gates, not a deploy/secret flip
between calls; image generation demonstrably ran at runtime today (gemini).
## 2026-08-06 — Adult deck open by default + comic-book adult imagery (owner directive)

The Audeering child-signal sensor was the adult deck's availability gate
— and the Audeering lane has been quota-blocked fleet-wide since WS-11,
which made the deck PERMANENTLY unavailable in production (the 16:48
session: repeated refusals with no path to enable). Owner directive:
the sensor's age estimation is unreliable in live rooms and is not the
gate.

- **`LILY_ADULT_DECK` — "open" (DEFAULT) | "sensor"** (new,
  `lily_config.adult_deck_gate_mode`, forwarded by deploy.yml). Open:
  the deck is available; the SPOKEN 18+ opt-in ceremony remains required
  before it switches on (consent step unchanged), and an ACTIVE child
  veto still blocks entry whenever the sensor happens to be running.
  Sensor: the legacy fail-closed one-unit coupling, now opt-in.
  Availability flag + `lily_enter_adult_mode` both honor the mode.
- **Adult imagery: realistic comic-book style.**
  `lily_imagegen.lily_adult_style` is the single style chokepoint (bold
  inked linework, painterly shading, stylized — never photorealistic).
  `lily_show_demo_picture(adult=true)` shows the grown-up deck's sample
  in that style (suggestive, tasteful, never explicit; gated on the
  deck's availability flag); the general demo is unchanged. Structured
  picture slots stay text in adult mode — the real-photo web rail never
  serves the adult deck.
- Legacy sensor-mode behavior pinned in `tests/test_adult_safety_gate.py`
  (the three fail-closed tests now run under `LILY_ADULT_DECK=sensor`).

## 2026-08-06 — WO-LILY-HOTFIX-002: transcript echo-dups + group-binding loudness

**Evidence re-audit first (session `lily-AAC431-6208ff7c`, 05:19–05:25).**
The reported "zero LILY rows / 25 user rows" was a miscount: the session
holds **25 rows total — 14 LILY + 11 user**, live-interleaved with
correct timestamps. Agent-transcript persistence was working. The REAL
Defect-1-class regression the rows exposed: **four verbatim duplicate
LILY rows**, each landing right after a tool-call-only turn. Root cause:
the playout watcher's `spoken or _last_assistant_text` fallback — a
handle carrying chat items but no assistant text (a tool turn) aired no
new words, and the fallback fabricated a re-record of the PREVIOUS
spoken turn (also silently double-feeding the SAID-ALREADY ledger and
repeat lint).

- **Fixed:** the fallback now applies only to a genuinely unreadable
  handle (no chat items at all); tool-call-only turns record nothing.
  Belt: a verbatim repeat of the immediately-preceding recorded turn is
  skipped with `LILY_TURNS | DUP_TURN_SKIPPED`. Loud-fail: transcript
  persist failures are ERROR logs (`TRANSCRIPT_PERSIST_FAILED` /
  `TRANSCRIPT_PERSIST_UNAVAILABLE`), never silence. LILY rows now carry
  a playout-completion `segment_end` anchor (same epoch clock as player
  rows).

**Defect 2 — group binding.** Trace results: the frontend is
**exonerated** — the lobby still posts `lily_group_id` into `/api/token`,
and the token route embeds it in both participant metadata and
`RoomAgentDispatch` (the WS-9 frontend commit touched only game-state UI
files). `group_id == session_id` at session start is the device-identity
quarantine's designed resting state (July 15, `f4e4e42`); the severed
link is PROMOTION — voiceprint verification never re-keyed any
post-deploy session (last successful bind 22:47 Aug 5, old build). The
live discriminator: the engine labeled the returning speaker `S1`, not
their enrolled name, so enrolled-voice matching failed at the engine.
Two concrete faults fixed, everything else made loud so the next log
bundle pins the remainder:

- **Duplicate enrollment labels merge** (`lily_filter_enrollable_speakers`):
  the group's voiceprint table carried the same player under two engine
  labels (Chris via S1 AND S4, written by the old build's session-close
  enrollment at 22:51) — duplicate labels inside StartRecognition's
  speakers list are undefined engine behaviour; same-name rows are the
  same human and their identifier blobs now merge under one label.
- **Wire-level enrollment truth:** the WS-13 StartRecognition wrap logs
  `wire_known_speakers=N` — the discriminator between "injection broken"
  and "engine didn't match" that tonight's logs couldn't answer.
- **Throwaway mint is loud:** `LILY_MEMORY | THROWAWAY_GROUP_MINTED |
  reason=no_token_present|token_unreadable` WARN on the room-name
  fallback.
- **Quarantined-to-the-end is loud:** session close WARNs
  `DEVICE_CANDIDATE_UNRESOLVED` with staged source + verify-attempt
  count; an STT surface that can never verify (`get_speaker_ids`
  missing) WARNs `DEVICE_VERIFY_UNAVAILABLE` instead of returning an
  eternal silent None.
- **Forward check:** the group token rides job/participant metadata,
  fully independent of the RoomOptions swap shipping in this same deploy
  — token resolution regression-tested in `tests/test_hotfix002.py`
  (dispatch metadata, both throwaway reasons, unverifiable candidate).

Version audit en route: prod's fresh image build resolves the same
`speechmatics-voice 0.2.8` / `speechmatics-rt 1.1.0` as the bench env
(plugin 1.6.6 floors, no drift); the promotion-chain suite passes under
the real plugin 1.6.6. Cleanup: tonight's throwaway sessions carry
nothing worth migrating (per the WO); the established group is intact.
Full suite green (1149). Ships in ONE deploy with HOTFIX-001's wedge
protections.

## 2026-08-06 — WO-LILY-HOTFIX-001 + WO-LILY-NC-BENCH-001: the deaf-mute wedge

**P0 (04:21–04:30 UTC): four consecutive sessions opened deaf and mute**
(`lily-813B86`, `lily-F70BF5`, `lily-90DAE0`, `lily-A7DAD8` — all
round 0, zero transcript rows, `tts_first_frame_ms` null). Root cause:
WS-14 re-enabled Krisp NC on the join path at 1.6.6 against a documented
in-repo kill history; NC wedged RoomIO audio setup, so the greet never
reached TTS playout and zero mic frames reached Speechmatics
(`stt_stream_disconnected_and_no_captured_speakers`). Service restored
operator-side (`LILY_NOISE_CANCELLATION=off` slot secret, no redeploy) —
the 1.6.6 omnibus build itself is healthy and stays.

Close-out findings (WO-LILY-HOTFIX-001):

- **Discriminating check:** the say-gate registry is per-session
  in-memory state (`LilyGame.__init__`), no consumed-keys ledger is
  persisted, and each dead session was its own throwaway group
  (`group_id == session_id`) — a cross-session consumed-key leak is
  structurally impossible. The observed `session_greet` dup was
  dispatch-then-suppressed-retry INSIDE each session: wedge chain alone.
- **Second defect, fixed (the P0's enforcement arm):** a claim frozen
  `pending` (dispatched, never played, never failed — the wedge created
  this state; nothing in the lifecycle could exit it) dup-suppressed its
  own retry. `gated_say` now supersedes a stale never-aired pending
  claim on retry (`STALE_CLAIM_SUPERSEDED`) and arms a per-dispatch
  watchdog that releases-and-retries a claim whose speech never started
  airing (`STALE_CLAIM_RELEASED`, bounded, `STALE_CLAIM_EXHAUSTED` with
  the key left free). Playout-start truth rides
  `agent_state_changed → "speaking"` + `session.current_speech`;
  confirmed acts stay final forever; the double-greet race protection is
  untouched. Pinned by `tests/test_wedge_recovery.py` (13 regressions:
  wedge recovery, consecutive-session double-greet, greet-barge
  regenerate-not-replay, join-path NC branches).
- **Persistence verdict:** zero transcript rows in the mute sessions is
  "nothing aired" (correct behaviour — `record_agent_turn` writes at
  playout), not a persistence break. Session-count discrepancy resolved:
  the DB holds all four dead sessions; the earlier 2-row read was a
  too-narrow query window.

WO-LILY-NC-BENCH-001 (codebase side):

- **NC default flipped nc→off** in `lily_config.noise_cancellation_mode`
  — a safety default fails to silence-of-the-feature, never
  silence-of-the-agent. Unknown values (including `bvc`) now coerce to
  `off`. NC returns only by passing the cold-join bench gate
  (`eval/nc_bench/` — harness, runbook, pass criteria: 10/10 accepts,
  greet playout every join, mic→STT every join, join latency ≤2×
  NC-off baseline), then explicit `LILY_NOISE_CANCELLATION=nc`.
- **Deprecated `RoomInputOptions` shim swapped** for 1.6.6-native
  `RoomOptions`/`AudioInputOptions` on `session.start` (the mute
  sessions logged the shim's deprecation warning on the join path;
  `_create_from_legacy` maps identically — hygiene, landed regardless
  of the bench outcome).
- **Task 4 rider:** `eval/nc_bench/baseline_rider.sql` computes the
  NC-off quality numbers (dropped answers, phantom labels, attribution
  spread, span sanity) from the next live session for comparison with
  the Aug 5 audit; verdict slot lives in the README WS-14 memo.
- **Task 5, permanent process gate:** README WO checklist now requires,
  for ANY audio-pipeline enable/re-enable, a repo kill-history search
  quoted in the WO plus a bench join-path test before production
  contact — no "believed fixed by upstream" exception.

WS-14 memo addendum recorded in README: NC stays `off` until the
Krisp/1.6.6 join-path interaction passes the bench; BVC remains
prohibited in shared-mic mode permanently. Full suite green (1140).

## 2026-08-05 — WS-11 telemetry restoration (WO-LILY-OMNIBUS-003)

Session lily-81BCB0-583a0f16 landed 14 addressee rows but zero
`lily_acoustic_trajectories` rows (fleet-wide), every `acoustic_snapshot`
explicit-null, `n_best_dispersion` 0.000 on every row, and `overlap_flag`
never firing in an echo room. Root causes, three distinct:

- **Audeering lane was OFFLINE, not running-without-persistence.** devAIce
  `durationQuota=0` on the shared fleet account (verified live) opens the
  circuit breaker at preflight, so nothing was ever captured — the zero
  rows are correct behaviour for an offline sensor. The gap was that a
  zero-row session said nothing about WHY. `LilyAudeeringPipeline.lane_health()`
  now surfaces breaker state + reason + upload count into
  `build_game_stats()["acoustic_lane"]`, so offline-vs-no-persistence is
  self-evident from the session report.
- **Dispersion structurally 0.000 in tap-only mode.** With
  `LILY_STT_MAX_ALTERNATIVES=1` (the live-incident default) the collector
  synthesizes exactly one hypothesis, and the variance of one confidence is
  zero even on torn speech. `drain()` now also computes per-word-confidence
  variance (`dispersion_source`, `mean_word_confidence`,
  `min_word_confidence`); a low mean over a multi-word final fires the light
  clarify posture (`_maybe_fire_confidence_clarify`) instead of engaging the
  garble — the "Ninja girl, 5050 first dates" case (mean word conf 0.57–0.63)
  now asks for a repeat.
- **Overlap never fired because empty labeled drains collapsed the span.**
  When per-word speaker tags disagreed with the event's speaker_id (echo
  room), `drain(speaker_label=...)` returned None, losing the stream times;
  the overlap span fell back to a degenerate arrival point the strict
  epsilon gate can never flip. `drain()` now falls back to an unfiltered
  drain (`speaker_filter_fallback`) so real timings survive.

Reconciled with WO-LILY-FLOOR-001 FL-1 (which landed on origin/main during
this work and owns `agent_classification` via its classifier): WS-11 defers
the classification column entirely to FL-1 and adds ONLY the STT stream-clock
timing trail (`timing_source`, `timing_drift_seconds`, migration 019) — the
overlap side's provenance. `lily_log_addressee` gains a strip-and-retry
covering BOTH migrations' telemetry columns so no corpus row is lost before
either DDL lands (FL-1 shipped no fail-soft; cardinal rule: no memory is bad
memory).

Live-session confirmation of the repaired trajectory/overlap/clarify paths
is a close-out item owed once the Audeering quota is restored (the lane is
quota-blocked, so a live acoustic round-trip cannot be exercised now).
Pinned by `tests/test_ws11_telemetry.py`; full suite green.

## 2026-08-05 — WO-LILY-FLOOR-001 FL-1: the per-utterance addressee classifier

Evidence base: session `lily-81BCB0-583a0f16` — Lily barged into a
player-to-player tangent ("Carry on, Lily. We're not talking to you")
and into the players' feedback conversation. Root conditions:
`agent_classification` null on every utterance (every heard utterance
defaulted to host-directed), no scope boundary on speak-by-default,
supply stalls pushing her to fill gaps that belonged to the table.

- **`lily_addressee_classifier.py`** (pure stdlib): per-utterance
  addressee judgment fusing three signal families. Deterministic game
  priors (open window + expectation-primed match on the active
  registered question = host-directed BY DEFINITION, no name needed;
  idle/vamp flips the default toward side chatter unless named or
  command-shaped; adjacency to a Lily prompt biases host). Name
  evidence, not a wake word ("Lily" anywhere spikes host-directed;
  vocative "Carry on, Lily" is address, referential "Lily is a joke" is
  mild side evidence — split at the language layer). Acoustic register
  via the narrow `LilyAcousticRegister` interface (WS-11/WS-13 features:
  arousal/energy, per-word volume, rate, articulation — those Omnibus-003
  surfaces are not live yet, so the snapshot adapter reads what exists
  today and fixture-recorded features drive the replay tests).
- **Side-cluster machine**: rapid player-to-player alternation with
  content cohering BETWEEN the players locks a cluster — utterances
  inside it classify as a cluster, not one by one, until it breaks
  (plain vocative/command/window-match/gap/a played Lily turn). Two
  additions the REAL 81BCB0 session forced: (a) diarization only
  captures the audible side of a room conversation, so a SOLO-attributed
  run anchored by a table-address ("Have you guys seen Loki?") or fully
  self-cohering also locks; (b) a FLOOR-HOLD declaration — host-directed
  speech whose content asserts the side conversation ("we're not talking
  to you" / "we're having a conversation", both recorded verbatim) —
  locks or sustains the cluster instead of breaking it, unlike a bare
  vocative. Real intra-run gaps reach 12.5s, so the gap bounds are 15s
  intra-run / 25s break.
- **Emission BEFORE the reply**: `classify_addressee` runs in the
  transcript layer (`on_transcript_event`), lands on
  `last_addressee_judgment` (the FL-2/FL-4 consumption surface), logs
  `LILY_ADDRESSEE | CLASSIFIED` structured JSON, and conditions the next
  reply via a positive-framing floor-read line in the state block
  (host-directed injects nothing; she never narrates her classification
  — 10% principle).
- **Per-utterance log coverage** (migration 018): every finalized
  segment now writes its `lily_addressee_log` row carrying
  `agent_classification` (never null), `addressee_score`,
  `addressee_score_components`, `side_cluster_id`, `side_cluster_event`.
  The old window-open/acted-on gate is gone; clarify re-log rows keep
  NULL judgment columns (the utterance's primary row carries it).
- **Real fixture** (`tests/fixtures/lily-81BCB0-583a0f16.*.json`):
  the actual session extracted verbatim from Supabase `lily_transcripts`
  (170 rows) + `lily_addressee_log` (14 rows) and committed in-repo per
  the desync/recognition-variety fixture idiom (no conftest, per-file
  fakes). `tests/test_addressee_classifier.py` replays all 78 player
  utterances through the production path — answer windows reconstructed
  from the addressee log's own ground truth (`utterance_ts −
  seconds_into_window`), driven on the STT `segment_start` clock (the
  same clock the addressee log stamps, verified equal). Pins the WO's
  verification: both recorded derailment beats classify as table talk /
  floor-held side-cluster before Lily would have spoken; every one of
  the 12 scored answers classifies host-directed with no name; the log
  populates per utterance (78/78) with `agent_classification` never null
  (the live session had it null on all 14 rows). `LILY_FEATURE_VERSION`
  unchanged — the single whole-WO bump is assigned to a later
  workstream, and the capability-manifest entry rides with it.
## 2026-08-05 — WO-LILY-OMNIBUS-003 WS-13: STT echo-room study + tuning matrix (as amended by AMENDMENT-001/-002)

Evidence: session `lily-81BCB0-583a0f16` (4-player echo room) ran on
effective Speechmatics defaults — 3 phantom labels (S5–S7), a Chris
S1→S4 continuity split, and 104s/206s corrupted spans under S2. Full
study + per-lever audit in the README "STT tuning — echo-room study
close-out" section. Shipped:

- **`lily_stt_tuning.py` + `stt_tuned.json`** — the tuned-config artifact
  (WS-15 bake-off incumbent arm; drift-tested), 27-cell matrix grid,
  machine-metric scorers (WER/DER/phantom/attribution/span —
  AMENDMENT-002 standard), and the StartRecognition wire-injection patch:
  `speaker_diarization_config.get_speakers=true` (server pushes
  SpeakersResult at end of transcript into a teardown-surviving store) +
  `audio_filtering_config.volume_threshold` (SDK already sends 0.0 on
  every wire — value override is schema-safe). Both fields live-validated
  against the voice endpoint (RecognitionStarted, no 1003 — the
  `max_alternatives` incident class does not apply).
- **Tuned constructor** (`lily_agent.py`): `speaker_sensitivity`
  0.5→0.35; player-name `additional_vocab` at construction when
  voiceprints exist (constructor-only pin — names at bind require WS-8's
  `Agent.update_options(stt=...)` swap); dunder-label filter on
  known-speaker enrollment both directions.
- **Enrollment fallback** (`lily_persistence.py`): session-close
  enrollment no longer dies with the websocket — captured SpeakersResult
  is the fallback source, closing the 2026-07-15 dead-stream hole.
- **`lily_room_profile.py`** (AMENDMENT-002 item 6): blind RT60/DRR from
  the first audeering capture window (coverage untouched; devAIce stays
  a coarse quality gate); reverberant→longer EOU trigger + SMART_TURN
  recommendation, low-DRR→Tier-1 threshold delta + positively-framed
  state-block line.
- **Playback-path verdict (item 1, verification only):** record-clean;
  protection is structural (agent never subscribes to its own track) +
  client AEC; `__ASSISTANT__` ignore is a dormant backstop with no real
  identifiers. Regression: `lily_assistant_leak_scan` pinned in tests.
- **Findings of record:** FIXED-mode `end_of_utterance_max_delay` is
  INERT at this pin (clamp lives on the non-FIXED path; RT
  conversation_config has no such field) — nothing config-side caps span
  length, so spans >30s are WS-10 quarantine
  (`ws10_span_quarantine_seconds` in the artifact). No session audio was
  recorded anywhere → the fixture
  (`tests/fixtures/echo_room_81BCB0.json`) is RECORD-DERIVED and
  acoustic matrix replay is deferred to WS-15's recorded fixture.

Tests: +27 (`tests/test_stt_tuning.py`), suite 885 green.
## 2026-08-05 — WS-12 (WO-LILY-OMNIBUS-003): report pipeline unstick — the clinical desk gets built

Root cause was NOT the primed fleet trap (shutdown-callback unreliability):
the close-path report WRITE works — all 41 production `lily_session_reports`
rows existed with transcripts. What never existed was the assessment layer
itself; `assessment` had no producer anywhere, so 41/41 rows sat at
`report_status='pending'` forever.

Built `lily_assessment.py` (reasoning model, own genai client, JSON
assessment: summary / group_dynamics / per_player / host_performance /
flags) with two shutdown-independent triggers: the wrap-up beat in
`finish_game` (report row + assessment within `LILY_REPORT_DEADLINE_S`,
default 5 min) and a session-start reconciliation sweep for orphaned
pending rows (stored transcript/game_stats; min-age grace + per-boot
limit). Fill is pending-guarded so the close path's later re-upsert and
re-runs never clobber a completed assessment; failure logs
`LILY_REPORT | ASSESS_FAILED` at ERROR and leaves the row pending for the
sweep (fail-visible, retryable). Session `lily-81BCB0-583a0f16` backfilled
through the production assess path (row id 42 now `complete`). Pinned by
`tests/test_report_assessment.py` (11 tests, incl. the wrap-up-beat
deadline exit bar driven directly).

## 2026-08-04 — Wrong score on the screen: score truth rides the reveal beat (live report)

Principal's report from the 08-04 call; committed truth was RIGHT
(DB-audited: 4 correct rows, 4 points, standings 4) — the defect was the
frontend's score-truth optimism inverted by our own ordering fix. The
overlay (desync-E) was designed for a LAGGING backend: snapshot the
winner's score at the reveal beat, and if the attribute hasn't moved 2s
later, roll optimistically by a guessed increment. Since desync-E,
commits publish BEFORE the reveal speech — so the beat-time snapshot
already held the new score, the "attribute is lagging" check passed
trivially (nothing more was coming), and the chip rolled ONE POINT HIGH
after every reveal, holding the wrong score until the next commit.

Fix: the reveal beat now carries `winner_score` — the winner's COMMITTED
score at adjudication. The frontend targets that real number (overlay
only when the board genuinely trails the wire value; the guessed
increment is deleted; a beat without the key applies no overlay at all).
Seam addition documented; pinned by
`test_reveal_beat_carries_committed_winner_score` + updated adult-identity
and frontend seam-parser expectations. Companion prmpt_ui change in the
same push.

## 2026-08-04 — WO-LILY-RECOGNITION-VARIETY-001: both-sides record, continuous recognition, claimed-returner, variety + the Q5 root cause

Source: Tijoux analysis of session `lily-CC9E19-19c2b804` (solo probe —
the machine ran clean; every defect experiential, all three named live).

- **Task 0 — both sides persist.** Lily's own turns (post-say-gate final
  text, at PLAYOUT — a swallowed turn is never recorded as said) now
  write to `lily_transcripts` as `speaker_label='LILY'` rows (the
  otherwise-meaningless `speaker_name` slot carries the primary
  speech-act key, e.g. `q_3_delivery` — zero-migration) and interleave
  into the session report via the scorekeeper's transcript buffer.
  Interrupted turns record with an explicit `…[cut off]` marker;
  suppressed turns never reached air and don't record. Fire-and-forget;
  post-forget the batcher is disabled, same as player rows.
- **Task 1 — recognition is continuous.** The post-upgrade memory load
  was gated on `question_number == 0` — the fixture's name-hash resolved
  a six-session regular MID-CALL and nothing happened ("you have like
  amnesia"). The gate is gone; `maybe_fire_late_recognition()` (both
  upgrade paths: name-hash and voiceprint promote) injects memory +
  prefs + the version stamp and fires ONE acknowledgment beat ("wait —
  Rami! NOW I've got you"), once per session, with the refresher offer,
  prefs-usual, and what's-new delta folded in as if recognized at the
  door. Stored pacing applies for the remainder unless the session
  already chose. New groups and door-path resolutions stay silent.
- **Task 2 — the claimed-returner state.** "Not my first time" from an
  unrecognized player is now its own greeting branch (prompt + composed
  instructions): the gap named plainly and honestly ("my table card
  doesn't have you tonight — new device, maybe"), the refresher offered
  IN THE SAME TURN exactly as a recognized returner gets it, never
  performed amnesia, never claimed recognition.
- **Task 3 — SAID-ALREADY + variety.** The scorekeeper keeps a rolling
  ledger from Task 0's turn record — praise words spent, turn openers
  used, rules/features already explained — injected compactly into the
  state block; prompt law (NEVER THE SAME BEAT TWICE): nothing on the
  ledger re-delivered unprompted, celebrations minted fresh from the
  answer's content and the running bits, re-asked explanations answered
  in fresh words. Say-gate repetition lint (`lily_repeat_flag`,
  log-only `LILY_SAY | REPEAT_FLAG`, opener 4-gram / content 6-gram vs
  turns that actually played) makes cycling a telemetry count — a fresh-
  words re-answer never flags.
- **Q5 root cause (DB-audited, not guessed).** `lily_answers` holds
  Q1–Q4; game_stats shows `questions_played: 5` but `answers_attempted:
  4` — the correct answer at 01:27:11 ("The Nile is just a river in
  Egypt", Tier-1 verified correct by direct test) NEVER became a
  candidate: it was spoken during the delivery playout, before the
  window opened. Not a Tier-1 miss, not a hangup-raced write — the
  documented "no early buzz-ins" v1 concession. THE CONCESSION IS
  RETIRED: finals landing between the delivery claim and window open
  buffer (last 6, per-question) and replay at open, then run the same
  instant Tier-1 fast path an in-window final gets — a correct early
  answer adjudicates immediately instead of waiting out the window
  (the wait is what the hangup would have raced). Commands and corpus
  enforcement do not re-run on replay.

Tests: `tests/test_recognition_variety.py` (15 — turn persistence,
ledger, repeat lint, late recognition one-shot + silences, claimed-
returner prompt contracts, Tier-1-matches-the-pun, buffer/replay/
adjudication, buffer hygiene). Suite 847.


## 2026-07-31 — Image ingestion: the Zuna vision port (12:48 live fixture, manifest v3)

"I can't show you because you don't have like image ingestion" — the
probe's parting shot, and it was true. Now she does:

- `lily_vision.py` — native lift of Zuna's vision tool: xAI Grok
  (`grok-4.3`, the fleet-consolidated vision surface) via the
  chat-completions endpoint with `image_url` content parts; xAI fetches
  the image itself. Zuna's structured failure contract kept verbatim —
  every path returns `{status, ...}`, never raises; missing
  `XAI_API_KEY` is an honest `unavailable`.
- `lily_analyze_image` function tool (registered via tools=[], mapped
  to the new manifest entry per the lint rule): a player names an image
  URL, she looks.
- Player-photo ingest: byte-stream topic `lily.image.upload` (the UI's
  image picker) → `lily-images` bucket under the new "player" source
  (content-addressed, 8MB cap) → Grok describe → a PLAYER PHOTO state
  note carrying what is ACTUALLY in it, and one in-character reaction
  grounded only in that note. Oversize, storage failure, unconfigured
  provider, and analysis errors each produce an honest spoken line —
  never a fabricated description (the say-gate self-knowledge rules
  apply to the description text like everything else).
- Manifest v3: `image_ingestion` entry (availability_key `vision` —
  the state block carries the OFF caveat when the key is unset), so a
  voice-presets-era table hears about photo sharing in its rematch
  delta. Options block gains the "Show her things" line (CI-checked).
- Companion prmpt_ui change: the Lily session view enables the fleet
  image picker on topic `lily.image.upload`.

## 2026-07-31 — "Show me" gets shown for real: the demo picture tool (12:47 live fixture)

Live probe, same afternoon the self-knowledge WO landed: a skeptic asked
to SEE picture rounds five times ("seeing is believing") and got words,
then a fabricated screen push ("I've just pushed a visual preview of what
our game board looks like directly to your display") and invented
pairing troubleshooting. The prompt demanded "show me gets shown" but no
MECHANISM existed — the same make-the-honest-answer-exist gap as the
manifest, one layer down.

- `lily_show_demo_picture` (new tool, mapped to the manifest's
  `pictures` entry per the lint rule): puts ONE real image on the screen
  via the same metadata path picture questions use, lobby included.
  Cache-first (any bank image), else one generated tabletop image
  through `LilyReasoning.generate_demo_image` — the one legal image seam
  (web guardrail respected; the vocal module still never references the
  image stack). Honest failure line when nothing can be produced.
- Prompt (WHAT YOU KNOW ABOUT YOURSELF): SCREEN ACTIONS EXIST ONLY
  THROUGH TOOLS — no tool confirmation, nothing reached the screen;
  never narrate an imagined push, never invent pairing steps for a
  screen she cannot see, never blame the player's device.
- Companion prmpt_ui change: the lobby renders the metadata image in a
  demo frame (the render path is now genuinely not phase-gated
  frontend-side too).

## 2026-07-31 — WO-LILY-CAPABILITY-LINT-001: bidirectional tool↔manifest lint

Follow-up to SELFKNOWLEDGE-INTAKE-001, dispatched on its landing. The
manifest stays true only if it can't drift: `tests/test_capability_lint.py`
now asserts on every merge that (1) every registered function tool —
the LilyAgent `@function_tool` methods plus the module-level voice-switch
tools — maps to a `lily_capabilities` entry via its `tools` list or is
declared in `LILY_INTERNAL_TOOLS` (invisible by declaration, not
omission: `lily_begin_round`, `lily_bind_speaker`, `lily_log_clarify`),
and (2) every manifest entry's new `code_ref` resolves to living code
(module / `module:attr` / `module:Class.attr`) — no orphaned claims.
Failure messages name the offender and the two legal fixes. The WO's
verify criteria run synthetically in the same file: a dummy unflagged
tool fails by name, flagging it internal clears it, an orphan ref fails.
README WO checklist gains the rule. Out of scope per the WO: runtime
reflection, architecture self-description, prompt changes.

## 2026-07-31 — WO-LILY-SELFKNOWLEDGE-INTAKE-001: self-knowledge, the manifest, the mirror ban, intake choreography

Source: Tijoux analysis of the two architect-probe sessions (11:56 and
12:08, 2026-07-15). The one-line finding: pressed on how returning users
learn about new features, Lily mirrored four turns, then — called out —
fabricated architecture to the person who wrote the code. The honest
answer didn't exist in the build; this WO makes it exist and makes her
prefer it. DESYNC Sub-agents C and F had already landed, so Tasks 1–2
extend C's prompt contract in place and Task 5 is a verify against F.

- **Task 1 — self-knowledge honesty** (`prompts/lily_system.txt`, WHAT
  YOU KNOW ABOUT YOURSELF): "I honestly don't know how that part works —
  that's one for the builders" is a COMPLETE, high-status answer; no
  invented mechanisms, flags, changelogs, or architecture, ever; under
  challenge the honest gap explicitly outranks any fabricated answer.
  Symmetric rule (the 12:11 false-incapability fixture): never claim a
  capability she lacks AND never deny one she has — "show me" gets
  shown. Waterline: the options block, her game rules, tonight's
  context. Capability vs availability are separate honest claims.
- **Task 2 — the mirror dies everywhere** (NO MIRROR section, global —
  per Rami, not workshop-only): flattery openers and agreement-echoes
  banned in every register; warmth lives in reaction and substance.
  Workshop register (WHEN THE TABLE GOES META): one notch down, BLUF —
  the four barged-mid-list fixture turns cost decoration next time, not
  content. Enforcement: prompt primary + log-only say-gate lint
  (`lily_mirror_flag`, `LILY_SAY | MIRROR_FLAG` telemetry, opener-scoped
  and conservative; celebrating an ANSWER never flags).
- **Task 3 — the capabilities manifest** (`lily_capabilities.py`, new):
  versioned, codebase-audited backfill (freeform, MC + 50/50, skip,
  steal, bonus/wager structure, adult deck, pacing, pictures,
  forget-me, group memory = v1; voice presets = v2, Rami's direct
  change — her fixture claim was TRUE). `last_seen_feature_version`
  rides the opaque `lily_group_prefs.prefs` jsonb (no migration; forget
  cascade + re-key inherited — interlock pinned in tests). Lagged
  rematch → ONE casual delta line at the door, stamp forward after the
  greet confirms; unstamped table → silent stamp (never fabricate "new
  since last time"). Availability layer: entrypoint-computed flags
  (adult deck = sensor up or architect mode; real-photo sourcing = EXA
  key; generated imagery gated by NOTHING per the corrected analyst
  finding) inject only OFF caveats into the state block. Options-block
  CI: every askable manifest entry's `prompt_marker` must appear in
  WHAT THE TABLE CAN ASK FOR — the check caught its first real gap on
  arrival (voice switching, shipped this morning, was absent from the
  options block; the line is added). Standing rule in the README's WO
  checklist.
- **Task 4 — intake choreography** (INTAKE — NAMES, ONE AT A TIME +
  `note_intake_overlap`): multi-player intake is conducted — protocol
  set once, round-robin run with per-bind acknowledgment and a "that
  everyone?" close; a pre-game timestamp overlap between two voices
  (H1 epsilon, reused outside the window) injects the ordering-repair
  state note ("two of you at once — you first, then you"), rate-limited
  to one per 20s; late joiners arriving together get the same frame;
  solo tables get zero protocol theater.
- **Task 5 — recognition before the first-time question** (verify
  against landed DESYNC-F): the greeting composes after the bounded
  memory await; the memory branch never asks the first-time question;
  ordering tripwire + branch assertions added.

Tests: `tests/test_selfknowledge.py` (19 — manifest↔options CI, delta/
stamp flow, mirror lint against the fixtures' quoted turns, greeting
branches + ordering tripwire, intake overlap mechanics, forget-cascade
interlock); suite 819. The WO's live-replay criteria (10× exchange
replays, scripted probes) are LLM evals against the deployed agent and
sit outside the offline suite; the quoted fixture moments are encoded in
the test file. Full transcripts were not attached to the WO — when they
arrive they drop into tests/fixtures/ as the regression corpus.

## 2026-07-31 — Voice/glass sync: the screen never leads the voice (live report)

Live report: during greetings/orientation the first question was "just
slapped on the screen" — no coordination between what Lily was saying and
what the table saw. Two publish-timing leaks, both fixed in
`lily_agent.py`:

1. **Phase flipped at ARM.** `arm_next_question` runs while Lily is still
   greeting (the supply pipeline pre-arms so she never stalls), and its
   `_set_ui_phase("question")` published immediately — the frontend
   swapped the lobby for the game board mid-salutation. Now: a new
   `_phase_hold` keeps the PUBLISHED phase on `lobby` from first-question
   arm until the delivery turn's playout (internal `ui_phase` still flips
   for turn logic; `publish_attributes` reports the hold).
2. **Question text published at TTS DISPATCH.** The screen-sync publish in
   `register_delivery_claim` fired when the delivery turn was handed to
   the synthesizer — leading the audible voice by the length of anything
   queued ahead (greeting, celebration). The claim stays at dispatch (say
   gate unchanged); the SCREEN publish moved to `open_window`, i.e. the
   delivery turn's playout completion, exactly when answers go live.
   MC choices / picture image ride the same publish; seam keys unchanged.

Screen truth now equals SPOKEN truth for question delivery, matching the
reveal's playback-beat contract. Frontend-compatible: same attributes,
same metadata document, later timing. Tests:
`tests/test_desync_fixture.py` (three new: no-publish-at-dispatch,
lobby hold through first delivery, no hold mid-game); suite 801.

## 2026-07-31 — Per-voice TTS tuning: voice1 stability 0.5 / speed 0.87

`voice_settings` are now resolved per ACTIVE voice at request time
(`_voice_settings_for()` in `lily_tts.py`) instead of one global dict.
Voice1 (primary, `W3C2vBPukr5b5jvoXhPK`) gets its own tuning on the
principal's adjustment: **stability 0.5, speed 0.87**. Voice2 (Raven's)
and any other id keep the 2026-07-15 baseline (stability 0.4, speed
0.90). Shared invariants unchanged: similarity_boost 0.9, style 0.0,
speaker_boost on, eleven_v3, pcm_24000. The lookup keys on
`lily_config.lily_voice_1()` at request time, so an `LILY_VOICE_1` env
override carries the voice1 tuning with it. Tests:
`tests/test_voice_switch.py::test_per_voice_settings_resolution`.

## 2026-07-31 — Voice presets + runtime switching (Zuna WO-ZUNA-VOICE-SWITCH-TOOL-001 port)

Lily gains runtime voice switching, ported from Zuna's voice_switch_tool
and trimmed to two presets:

- **voice1 (primary/default):** `W3C2vBPukr5b5jvoXhPK`, hardcoded as
  `LILY_VOICE_1_DEFAULT` in `lily_config.py` (same hardcoded-ID pattern
  as Zuna's `VOICE_NADIA`), overridable via the `LILY_VOICE_1` env var.
  `lily_voice_id()` — what `LilyTTS()` boots with — now resolves to
  voice1, so every session starts in the new primary voice. Side effect:
  boot no longer hard-fails when `LILY_VOICE_ID`/`RAVEN_VOICE_ID` are
  unset.
- **voice2:** Raven's voice, the former default — `LILY_VOICE_ID` with
  `RAVEN_VOICE_ID` fallback, `None` when unconfigured (the tool reports
  "not configured" rather than erroring).

New module `lily_voice_switch.py` exposes two docstring-discovered
`function_tool`s (no prompt-file edits), registered on `LilyAgent` via
`tools=[]`: `lily_list_voices` (reports configured presets + which is
active on the live TTS) and `lily_switch_voice("voice1"|"voice2")`
(mutates the session `LilyTTS._opts.voice_id` so the NEXT `synthesize()`
targets the new voice — no session teardown, no reconnect). `LilyTTS`
gains `set_voice()` mirroring Zuna's contract (non-empty validation,
locked invariants untouched). Every tool failure path returns a plain
string; the tools never raise. Tests: `tests/test_voice_switch.py`
(config contract pure; tool/TTS tests livekit-gated).

## Live-session fixes (2026-07-15, the femur game)

One live solo session exposed four deterministic failures; all are fixed
with regression coverage in `tests/test_stall_recovery.py`:

- **Self-correction scores.** Every in-window final a player commits is an
  attempt; adjudication scores the earliest CORRECT attempt across the
  table. A revision ("the spine… no, the femur") competes from its own
  later timestamp — it can never jump the queue, but it can score, which
  the old "first final locks the player's slot" wrongly prevented.
- **Steal only with a possible stealer.** Candidates persist through the
  steal window and judged players are filtered, so a solo table (or a
  table where everyone answered) could never record a steal — the window
  burned five silent seconds and re-adjudicated an empty set. The steal
  now opens only while an unjudged rostered player exists.
- **Idle watchdog.** The supply line is fire-and-forget tasks gated on
  one-shot triggers; one silent task death used to wedge the whole game
  (nothing armed, nothing prefetching, Lily freestyling over a frozen
  scoreboard). A 10s watchdog makes "game live but idle" self-healing:
  loaded question → arm + nudge; dead supply task → relaunch; supply task
  alive past ~90s → cancel, honest status note, relaunch. All watchdog
  actions log as `LILY_WATCHDOG | ...`.
- **Armed-limbo recovery (04:05 session).** Adjudication can die between
  the answer commit and the reveal publish; the game then sits with a
  confirmed-delivered question armed, window closed, nothing adjudicating
  — a state the watchdog used to trust as "in progress". It now detects
  the limbo (delivery claim CONFIRMED + closed window + idle ≥2 ticks)
  and recovers deterministically: candidates waiting → force
  adjudication; none → reopen the window. The Tier-2 judge call is also
  hard-bounded (12s) so a hung judge can never wedge `_adjudicating`.
- **Arm-failure honesty.** When the reveal can't arm a next question, the
  consumed question is cleared from the state block and a status note
  tells Lily to vamp — never to re-ask the revealed question "for the
  official record" and never to invent one. Bank fetches on the supply
  path are also time-bounded (20s) so the insurance line can't hang, and
  prefetch/adjudication crashes now log loudly instead of vanishing into
  their tasks.

### Persistence + enrollment hardening (same live session, log sweep)

- **Supabase client now has HTTP timeouts** (`postgrest_client_timeout=10`);
  one hanging postgrest call froze the heartbeat loop at 01:40 — checkpoints
  and the `last_active_at` beat stopped for the rest of the session while
  the game ran on. Checkpoints also carry a 15s `wait_for` belt, and the
  heartbeat loop survives any single bad beat
  (`LILY_HEARTBEAT | BEAT_FAILED — continuing`).
- **Voiceprint enrollment names the dead-stream case.** The plugin's
  `get_speaker_ids()` returns empty for a disconnected STT stream — at the
  `session_close` trigger that is always true, so 21 minutes of speech read
  as "not enough words". The failure now logs
  `reason=stt_stream_disconnected`; the mid-game triggers (first_bind /
  game_start / group_id_upgrade) are the ones that must land.
- **Known-noise log lines** (framework-internal, benign):
  - "preemptive generation enabled but chat context or tools have changed
    after `on_user_turn_completed`" — was 10/session during live games
    because in-round state honestly changes on nearly every user turn (the
    answer being spoken lands as a candidate line; the window flips on the
    clock), so 1.6.4's equivalence check rightly discarded the speculative
    run. Preemptive generation is now OFF while the game is live
    (`set_game_live_preemptive`, logged as `LILY_STATE | PREEMPTIVE_OFF/ON`)
    and ON in the lobby/wrapup where the check passes — a live session
    should log ~zero of these; a rare one outside the game window is still
    benign.
  - "on_playback_started called after start_fut is set" (double
    playback-start notification in the transcript synchronizer).
  - "inference is slower than realtime" (silero VAD under momentary CPU
    contention) — a one-off per session is noise; investigate only if it
    repeats or latency metrics degrade alongside it.
  - "silence has been prepended" (`recorder_io` aligning a track that
    started mid-frame) — cosmetic recorder bookkeeping, not an audio-path
    problem. Neither one-off is worth chasing.

## Reliability and privacy audit hardening (2026-07-15)

The repository-wide audit closed the remaining state, privacy, schema, and
deployment gaps:

- picture prefetch now accepts the shared history-exclusion contract, so a
  picture slot can fall back to text instead of crashing on unexpected kwargs;
- reconnect restores the checkpointed question without incrementing it and
  starts the supply watchdog;
- speech-act claims are owned by their concrete `SpeechHandle`; a silent,
  interrupted, or duplicate handle cannot confirm another turn or open an
  answer window;
- skip and adjudication use a single transition guard, and media-mode commands
  are never answer candidates;
- forget requires a recorded spoken yes, deletes every session-linked content
  table, disables queued transcript writes, clears in-memory names/transcripts,
  and suppresses identity reports, memories, facts, preferences, and
  voiceprints for the remainder of the session;
- transcript batches retry idempotently through `event_id`, devAIce `202`
  results are polled to a bounded completion, and bank draws use bounded,
  server-filtered randomized candidate sets;
- migrations `001` through `016` now apply to an empty PostgreSQL database in
  order; pull requests and main deployments run all unit tests plus that
  greenfield migration check before deployment.

## Loop engagement (2026-07-14 persistence-audit root-cause fix)

The live audit found every session with `round=0` / `question_number=0`,
`lily_answers` empty, and scores committed only through `lily_award_bonus`:
**the deterministic pipeline (start_game → arm → ask → window → adjudicate)
never engaged** — `start_game` was RPC-only and Lily freestyled the quiz.
The pipeline now has four entry points, in order of preference:

1. **`lily_begin_round` function tool** — Lily calls this the moment the
   lobby has real energy (first genuine group laugh). This is the primary
   in-character way to flip out of the lobby; the prompt contract tells
   her the engine only runs through it.
2. **Deterministic spoken path** — "start the game" / "start the quiz" /
   "start the trivia" / "let's start" / "let's play" / "start round one"
   fire a fragment-proof `start_game` control command at the
   transcript-event layer (ignored once running).
3. **`lily_control.start` RPC** — the frontend "start" button. Kept for
   any UI that wants an explicit host-side gate.
4. **Auto-start safety net** — if ≥2 speakers are bound
   (`LILY_AUTO_START_MIN_PLAYERS`), the first question is prefetched, and
   the lobby grace window has elapsed
   (`LILY_AUTO_START_LOBBY_GRACE_SECONDS`, default 60s), the game starts
   automatically. This exists so a voice-only table that never calls the
   tool AND never touches the UI still reaches question one — the exact
   failure class that produced 15+ 2026-07-14 sessions with
   `lily_sessions.round=0` and `question_number=0` despite hours of
   audio and populated `final_standings`.

**Delivery registration is STRUCTURAL** (WO-LILY-DESYNC-HONESTY-001
Sub-agent B; supersedes the tiered ratio gate). "Did Lily just perform
the armed question?" is answered by the `q_{N}_delivery` CLAIM, never by
text similarity: the answer window opens (at the delivery TURN's playout
completion, as always — `LILY_WINDOW | OPEN | reason=delivery_claim`)
and the question marks delivered off that claim event. The claim fires
in `tts_node` at speech dispatch on either trigger
(`LilyGame.register_delivery_claim`):

- **structural** — code dispatched this turn to deliver the armed
  question (`expect_delivery()` arms a one-shot flag: the
  `lily_begin_round` post-tool turn, both question nudges, the skip and
  voice game-start follow-ups) — the turn claims regardless of phrasing;
- **core sentence** — an organic turn performs the question's core
  answer-bearing sentence as written
  (`lily_evaluation.lily_turn_presents_question`: word-boundary
  containment of the prompt's final sentence, TTS tags stripped —
  flourish before and after, never inside; the prompt states that
  contract as texture).

Post-adjudication delivery is stricter still. The reveal/score turn now
STOPS before N+1; only after its playout does a separate question-only turn
dispatch. That turn must contain the armed prompt and every multiple-choice
option. If the vocal model resurrects a stale question, invents another one,
or emits an incomplete option sheet, `tts_node` replaces it with the
deterministic armed sheet before claiming delivery or opening the window
(`LILY_DELIVERY | STRICT_REWRITE`). This pins the live 00:07 failure where
“Jupiter” was answered to a moons question but evaluated against Verona.

The old text-ratio matcher (`lily_question_spoken_ratio`, verbatim ≥0.6 /
paraphrase ≥0.3 tiers) is **telemetry only** — logged per playout as
`LILY_WINDOW | RATIO | … telemetry` and acted on by nothing. Two live
sessions (2026-07-15 01:33 and 22:54) proved it can never decide game
state: conversationally woven questions the table demonstrably heard
scored 0.00–0.15, so deliveries never registered, and the
`fallback_any_agent_speech` opener then opened windows against turns
carrying no question at all — the ghost game (engine at q=5 while Lily
ran a different quiz by voice; "official re-runs" of already-answered
questions). The fallback is gone: after `WINDOW_FALLBACK_AGENT_TURNS`
finished agent turns with a question armed in phase `question` and no
claim, ONE structural delivery nudge dispatches instead
(`LILY_WINDOW | DELIVERY_NUDGE`) — the nudged turn claims at dispatch
and the window opens on ITS playout. The pipeline never stalls, and a
window can never again open on a question nobody was delivered. Fixture:
`tests/test_desync_fixture.py`.

With the pipeline engaged, `lily_answers` rows (one per adjudicated
attempt, schema `(session_id, player_name, question_id, question_index,
transcript, verdict, eval_tier, awarded_points, ts)`) and real
`question_count` values in `lily_memories` (read straight off
`scorekeeper.question_number`) flow from the existing write paths.
Tests: `tests/test_round_loop.py`.
