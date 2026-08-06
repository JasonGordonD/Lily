# LILY — Changelog

Reverse-chronological work-order, RFI, and fix entries for the Lily agent,
split out of README.md on 2026-07-31 (dated sections moved verbatim —
nothing removed or truncated). New dated/WO entries are appended at the
TOP of this file. Living documentation lives in [README.md](README.md).


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
