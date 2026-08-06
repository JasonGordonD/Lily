# LILY — Changelog

Reverse-chronological work-order, RFI, and fix entries for the Lily agent,
split out of README.md on 2026-07-31 (dated sections moved verbatim —
nothing removed or truncated). New dated/WO entries are appended at the
TOP of this file. Living documentation lives in [README.md](README.md).

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
  (vocative/command/window-match/gap/a played Lily turn).
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
- Fixture replay (`tests/test_addressee_classifier.py`) pins the WO's
  verification: both 81BCB0 derailment beats classify side-cluster
  before Lily would have spoken; every scored answer classifies
  host-directed with no name; the log populates per utterance with no
  nulls. `LILY_FEATURE_VERSION` unchanged — the single whole-WO bump is
  assigned to a later workstream, and the capability-manifest entry for
  the floor feature rides with it.

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
