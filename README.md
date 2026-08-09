# LILY — Multi-Player Voice Trivia Host

Lily hosts live trivia nights for 2–6 people sharing one room and one microphone.
She knows who is speaking (real-time diarization), addresses players by name, scores
per player, adjudicates who answered first, generates her own questions on the fly,
and runs an opt-in, consent-gated 18+ mode. One agent, one prompt file plus one
additive adult layer, one game-state object.

Built from the PRMPT fleet's paid-for lessons: Lovebirds' production one-mic
diarization, name binding, voiceprint persistence, TTS wrapper and its bug fixes —
every lift lands as a native `lily_`-prefixed implementation (prmpt_common is
do-not-touch: no imports, no vendoring).

Living documentation lives here. Dated work-order and fix entries live in
[CHANGELOG.md](CHANGELOG.md) — new WO/RFI entries go THERE, not here.

Spoken-surface freeze before any speech/delivery extract:
[docs/voice_inventory.md](docs/voice_inventory.md).

## Stack

| Layer | Choice |
|---|---|
| Framework | `livekit-agents==1.6.6` (plugin family pinned to match; upgraded from 1.6.4 under WO-LILY-OMNIBUS-003 WS-0 with the full migration audit below; JRVS stays the fleet 1.6.4 reference install) |
| STT | Speechmatics — `en`, diarization, ENHANCED; tuned under WS-13 (artifact `stt_tuned.json` / `lily_stt_tuning.LILY_STT_TUNED`, full lever audit in the WS-13 close-out table below): `speaker_sensitivity=0.35` (0.5 minted 3 phantoms + 1 continuity split for 4 players in the echo-room evidence session), `prefer_current_speaker=True`, `max_speakers=7` (fixed at construction — table size is unknowable pre-bind; roster-aware cap `lily_max_speakers_for` is WS-8's to apply via the 1.6.6 `Agent.update_options(stt=...)` swap), `ignore_speakers=["__ASSISTANT__"]`, player-name `additional_vocab` at construction when voiceprints exist, StartRecognition wire injection `get_speakers=true` + `audio_filtering_config.volume_threshold` (lily_stt_tuning patch, live-schema-validated) |
| Vocal LLM | `gemini-3.5-flash` — every spoken turn; explicit `safety_settings` (adult-product context), `thinking_config={"thinking_level": "low"}`, `max_output_tokens ≥ 600`, default sampling |
| Reasoning LLM | `gemini-3.1-pro-preview` — background node, own google-genai client (HTTP isolation): question prefetch (N+1) + verification at prefetch time; never speaks |
| TTS | ElevenLabs v3 via `lily_tts.py` (`/v1/text-to-speech/{voice_id}/stream`; the dialogue endpoint stays off per fleet revert). Two voice presets, runtime-switchable (`lily_voice_switch.py`): voice1 primary/default `W3C2vBPukr5b5jvoXhPK` (hardcoded, `LILY_VOICE_1` override), voice2 Raven's (env `LILY_VOICE_ID`, falls back to `RAVEN_VOICE_ID`) |
| VAD | Silero — barge-in enabled; STT is never gated during TTS |
| Persistence | Supabase (`lily_*` tables), fail-fast init, checkpoint on score change / 60s / key events |

## Architecture invariants

- **No generation gate or trigger loop.** Lily speaks by default — silence is
  her failure mode (the deliberate inversion of Lovebirds' Raven). A bounded
  supply watchdog exists only to recover a stalled question pipeline.
- **The scorekeeper owns order; the LLM owns correctness.** First-committed-answer
  is a timestamp comparison in `lily_scorekeeper.py`, never an LLM judgment.
- **Answer window** opens on question TTS playout completion (SpeechHandle — 1.6.6
  has no dedicated playout event either; 1.6.6 change: a failed generation
  no longer raises out of `wait_for_playout()` — the error moved to
  `SpeechHandle.exception()`, and the playout watcher maps a failed handle
  onto the suppressed path so say-gate claims release instead of confirming), runs a bounded duration; finals-only scoring, one
  candidate per player, segments outside the window are game-inert.
  Non-answers (backchannels, bare roster names, procedural imperatives like
  `"Go."` / `"Next question."`, and HOTFIX-006 N4 meta-speech) are logged
  never scored. Host-addressed interrogatives do not need a terminal `?`.
- **Lobby auto-start is a quiet-room safety net, not a timer.** It needs
  min roster, a prefetched question, lobby grace
  (`LILY_AUTO_START_LOBBY_GRACE_SECONDS`, default 90s), intake settle after
  the last bind, **and** quiet after the last user turn
  (`LILY_AUTO_START_QUIET_SECONDS`, default 20s). Active banter or host
  speech defers it; `lily_begin_round` / UI start still start immediately
  once intake has settled — except when a kickoff lock is active (below).
- **Kickoff locks (code, not prompt).** `start_game`, lobby auto-start, and
  `lily_begin_round` share `start_blocked_reason()`: (1) `recognition_dispute`
  after a false clean-slate / empty-memory claim until one grounded why-beat
  lands; (2) `ambiguous_yes` after an A-or-B offer (ready vs waiting, etc.)
  when the table answers with a bare "yes" / "yes I am" — that answers the
  choice, not a start; (3) `setup_pending` while requested voice/adult/media/
  heat/consent jobs are incomplete; (4) `user_speaking` while VAD says the
  player is still talking. Explicit start language clears only the yes-lock;
  it never skips setup.
- **Multi-intent setup is non-exclusive and precedes Round One.**
  `lily_parse_lobby_setup_intents()` extracts start, voice, adult, pictures,
  heat, and age-presence from the same final before any start dispatch.
  Requested jobs enter a setup ledger and clear only after their real state
  mutation succeeds. Adult+picture setup does not draw from the general
  partition while adult/heat tools are pending. The state block lists pending
  jobs and forbids `lily_begin_round`; VAD blocks the split-final race where
  "I want to play" is followed immediately by a longer setup utterance.
- **One start owner.** Standalone kickoff fragments/category teasers
  (`"Round"`, `"Let's do it"`, `"Round One is …"`) are physically suppressed
  at TTS unless that same turn owns `q_N_delivery`. Suppression returns silent
  playout directly—no empty retry/re-air. A full keyed/organic question
  delivery remains legal; its `q_N_delivery` claim is the sole Round One
  speech owner.
- **Outbound speech yields after the first question** except for MC
  deliveries (stem + options). Freeform deliveries and verdict-plus-next
  stacks clip at the first `?`. Undelivered-delivery re-fires wait for
  table quiet (`LILY_UNDELIVERED_REFIRE_QUIET_SECONDS`) before re-asking.
  **Near-miss (≥0.9 spoken/prompt ratio) confirms delivery and opens the
  window — it does not full re-read the same Q** (`force_confirm_delivery_heard`).
- **Two-tier adjudication:** Tier-1 conservative fuzzy/phonetic match against
  `acceptable_answers` (uncertainty escalates, never rejects); Tier-2 is one
  non-spoken LLM call that judges against the supplied canonical answer only — it
  never re-derives the fact.
- **Sticky commands enforced in code:** "skip", "back to normal" (adult-mode
  revert), and the pacing choices ("let's play relaxed" / "timed rounds") flip
  deterministic flags at the transcript-event layer; the prompt is
  texture, not the mechanism. The adult layer is additively injected/removed on the
  sticky `mode` flag — removal fully reverts her.
- **tts_node punctuation-flush guard is mandatory** — suspense holds ("The answer
  is…") deadlock the SegmentSynchronizer without it.
- **Outbound speech passes the say gate** (`lily_say_gate.py`): the designated
  choke point for everything Lily may say out loud — the P4 markdown/emoji
  strip (emphasis asterisks, backticks, headers, bullets and emoji never reach
  the synthesizer, while `[bracket]` audio tags — ElevenLabs v3 controls —
  pass through verbatim; emoji-only turns strip to empty and fall into the
  empty-candidate retry), plus the say-gate WO extensions: idempotent
  speech-act keys (double greeting / BUG-2 double question delivery), the
  state-block leak filter, need-to-know ambient context, and the burn
  protocol. See "The say gate" below.
- **Tool gating principle:** gate tools that mutate game outcomes or emit
  game events; leave observational/memory writes free. `lily_note_fact` is
  deliberately ungated — its primary habitat is the pre-game lobby, and a
  fact noted during phase confusion mutates nothing.
- **Reasoning-node calls are structured output** (P1): generation and
  verification set `response_mime_type="application/json"` **and** a
  `response_schema` (question schema carries current + reserved fields:
  `choices`, `image_url`/`image_source`, `proposed_category`), so the output is
  parsed with a plain `json.loads` — the regex/fence-stripping path
  (`lily_parse_question_json`) is retired to a defensive last resort.
- **Honest failure:** prefetch/verification failures write a status note into the
  state block; Lily reports the breakage in character rather than confabulating.
- **Event-bound truth:** she never announces a score the scorekeeper hasn't
  committed, never claims the next question is ready unless prefetch landed.

## Voice presets + runtime switching (Zuna port)

Lily carries two ElevenLabs voice presets (`lily_config.py`):

- **voice1 — primary/default:** `W3C2vBPukr5b5jvoXhPK`, hardcoded as
  `LILY_VOICE_1_DEFAULT` (overridable via `LILY_VOICE_1`). Always
  populated; `lily_voice_id()` resolves here, so `LilyTTS()` boots every
  session on voice1. Own TTS tuning: stability 0.5, speed 0.87
  (voice2 keeps the 0.4 / 0.90 baseline; settings resolve per active
  voice at request time in `lily_tts.py`).
- **voice2 — Raven's voice (the former default):** `LILY_VOICE_ID` with
  `RAVEN_VOICE_ID` fallback; unconfigured means the switch tool reports
  it unavailable rather than erroring.

`lily_voice_switch.py` (port of Zuna's voice_switch_tool) registers two
docstring-discovered tools on `LilyAgent` — `lily_list_voices` and
`lily_switch_voice("voice1"|"voice2")`. Switching mutates the live
`LilyTTS._opts.voice_id` (`LilyTTS.set_voice()`), so the next
`synthesize()` targets the new voice with no session teardown; the
change lands on the NEXT spoken turn. Every failure path (unconfigured
preset, no LilyTTS on the session, rejected voice id) returns a plain
string — the tools never raise. Tests: `tests/test_voice_switch.py`.

## Self-knowledge + the capabilities manifest (WO-LILY-SELFKNOWLEDGE-INTAKE-001)

`lily_capabilities.py` is THE source of truth for what Lily can do,
player-facing — versioned (`lily_feature_version()`), audited against the
codebase, availability-aware. It exists because the architect probes
(2026-07-15 fixtures) showed "how does a returning user learn about new
features?" had no true answer in the build, and under pressure she
fabricated one. The mechanisms:

- **The rematch delta ("table card").** `last_seen_feature_version` is a
  key in the opaque `lily_group_prefs.prefs` jsonb (migration 013's
  documented extension path — no schema change; it joins the forget
  cascade and the group-id re-key like everything group-keyed). A
  returning table with a lagged stamp gets ONE casual greeting line
  naming only the delta; the stamp moves forward AFTER the greet's
  playout confirms. An unstamped returning table stamps forward
  SILENTLY — claiming "new since last time" about features they may
  know would be its own small fabrication.
- **Availability layer.** Capability (what she can do) and session
  availability (what's switched on tonight) are separate claims: the
  entrypoint computes `availability_flags` (adult deck = child-signal
  sensor up or architect mode; real-photo sourcing = EXA key) and the
  state block injects only the OFF caveats — "one of mine, but not
  switched on tonight," never the fixtures' present-tense overclaim.
  Generated imagery is gated by NOTHING (the render path is not
  phase-gated and `lily_imagegen` needs no key).
- **Enumeration from the manifest.** Every askable manifest entry pins a
  `prompt_marker` that MUST appear in the prompt's WHAT THE TABLE CAN
  ASK FOR block — `tests/test_selfknowledge.py` CI-checks it, so the
  options block and the manifest can never disagree and feature
  rundowns are complete-by-construction.
- **Prompt contract** (`prompts/lily_system.txt`): WHAT YOU KNOW ABOUT
  YOURSELF (honest-gap outranks any fabricated answer; the symmetric
  capability rule — never claim what she lacks, never deny what she
  has; "show me" gets shown; the manifest waterline), NO MIRROR (the
  flattery-opener/agreement-echo reflex is banned in EVERY register —
  warmth lives in reaction and substance), WHEN THE TABLE GOES META
  (workshop register: one notch down, BLUF — the answer lands in the
  first sentence), and INTAKE — NAMES, ONE AT A TIME (multi-player
  intake is conducted round-robin with per-bind acknowledgment; lobby
  voice-overlap triggers the ordering repair via a state note reusing
  the H1 overlap epsilon; solo tables get zero protocol theater).
- **Mirror lint** (`lily_say_gate.lily_mirror_flag`, log-only v1):
  tts_node logs `LILY_SAY | MIRROR_FLAG | pattern=...` when a turn
  OPENS with a known flattery/echo pattern — drift is measurable in
  telemetry, never suppressed (the fixtures show the mirror is
  situational, not constant).
- **Recognition ordering** (Task 5, verify): the greeting composes
  AFTER the bounded memory await (Memory-at-the-door F), the memory
  branch never asks the first-time question, and
  `tests/test_selfknowledge.py` carries an ordering tripwire on
  `on_enter` source.

## On-the-fly categories + the capability truth test (WO-LILY-CAPABILITY-RESTORE-001)

The 2026-08-06 session had Lily DENY a table-named topic ("my general
deck is pre-loaded... can't do a specific custom topic") — contradicting
her own core behavior (she "generates her own questions on the fly") and
the fact that the reasoning generator already accepts any `category`
string. The gap was wiring, not capability: the round category came only
from the fixed family rotation, with no path for a requested subject to
reach it.

- **`lily_set_category(topic)`** (LilyAgent tool): a table asks for "a
  Game of Thrones round" / "let's do Japan" and the tool records the
  subject on `_category_override[target_round]`. `_category_for_round`
  consults that override before the family rotation, so the exact seam
  `_prefetch_inner` reads (`category = self._category_for_round(rnd)`,
  passed straight to `reasoning.prefetch_question(category=...)`) now
  carries the requested topic. The stale prefetched question (drawn on
  the old category) is dropped and re-issued; a beat-to-build is covered
  by an honest "putting your round together" status note — never a
  denial. Adult mode redirects to the general deck rather than crossing
  the deck-identity firewall (an adult question must never wear a custom
  label). Manifest entry `custom_category` (v5), option line "Any topic,
  on the fly".
- **The capability truth test** (`tests/test_capability_truth.py`): what
  she CLAIMS (prompt) == what the manifest SAYS == what ACTUALLY WORKS.
  Beyond the capability lint's one direction (every registered tool maps
  to an entry; every code_ref resolves), it pins the ones the lint can't
  see: every manifest-NAMED tool is a really-registered function tool (a
  manifest can name a tool that was never wired — a lie the lint never
  sees); every askable feature is claimed in the prompt and every
  non-askable one is deliberately unclaimed; every `availability_key` is
  computed at runtime from a REAL dependency check (never a hardcoded
  literal — the anti-"honest relaying of a lying config" core), with a
  runtime flip test proving vision availability tracks the XAI key; and
  the two capabilities that regressed (vision, custom category) are
  INVOKED against their real contracts. Live proof that the generator
  honors an arbitrary topic (real Gemini "Game of Thrones" → *Drogon*,
  "Japan" → *Edo*) and that Grok vision analyzes a seeded photo is in the
  WO record; those need funded keys and run against the deployed agent,
  not CI.
- **Generated categories persist to a compounding bank.** An on-the-fly
  topic is saved so future rounds draw from it. The bank already
  self-grows — every armed question banks into `lily_questions` tagged by
  category (`_curate_generated_question` -> `lily_bank_generated_question`),
  and `lily_questions` carries the image triplet (migration 012) — so
  generated on-the-fly questions already persist with `category=<topic>`.
  Added: `lily_bank.lily_register_operator_category` upserts the topic into
  `lily_category_candidates` by name (idempotent, no duplicate), marked
  `operator_requested=true` so it is first-class immediately (it skips the
  use_count>=10 / >=3-groups promotion gate model proposals must clear;
  migration `020`). `lily_set_category` fires the register (fire-and-forget
  — the round serves regardless). For an operator topic, `_prefetch_inner`
  prefers `lily_fetch_bank_question(category)` before regenerating (the
  compounding arsenal), generating and banking a fresh one only when the
  bank runs dry; the fixed family rotation still generates-first. A failed
  bank write logs the COMPLETE payload (`RECOVERY_PAYLOAD`) so a dropped
  generation is recoverable (Cardinal Rule), never gating the live round.
- **Pictures / vision were never an omnibus code regression.** The
  availability gating predates the 1.6.4→1.6.6 rebuild (`40c71e1` for the
  EXA-gated real-photo sourcing, `3a17144` for the XAI-gated vision); WS-0
  never touched the vision or capabilities modules. The "photo/picture
  modules aren't switched on tonight" line is the availability layer
  telling the truth — it fires only when `XAI_API_KEY` / `EXA_API_KEY`
  are absent at runtime. Both secrets are present at repo scope and
  forwarded by `deploy.yml` (`XAI_API_KEY` docker `-e` + job env; seeded
  2026-07-31), and the funded GCP `XAI_API_KEY` passes a live vision call.
  A session that still heard "off" was a deployed-runtime key gap, not
  code — a redeploy carries the keys.

## Model stack + thinking policy (operator-directed, 2026-08-06)

Every model ID below was verified live on the funded keys before wiring.

- **Brain (vocal LLM):** `gemini-3.6-flash` (`lily_config.vocal_model`), text-mode
  via the LiveKit `GoogleLLM` plugin — NOT Gemini Live. Streaming is on by
  default (plugin streams into TTS). `previous_interaction_id` is not used —
  local `chat_ctx` stays authoritative for the game's structural/desync/floor
  logic.
- **Image gen — standard deck:** `gemini-3.1-flash-lite-image` (Nano Banana 2
  Lite; `lily_config.imagegen_model`) on the classic `generate_content` path
  (no Interactions API migration needed).
- **Image gen — adult deck:** xAI Grok Imagine `grok-imagine-image`
  (`lily_config.adult_imagegen_model`) via `_generate_image_bytes_xai`, because
  Gemini refuses adult content. `lily_generate_image_bytes(..., mode=...)` picks
  the provider READ-ONLY on deck — the adult gate is untouched. Adult
  picture-trivia is LIVE (WO-LILY-ADULT-PICTURES-001): the adult deck supplies
  picture rounds exactly like the other decks. `_picture_kind_for_slot` gates
  on `media_mode` only, and `prefetch_picture_question` threads `mode='adult'`
  through `lily_build_real_or_imagined_question` so its GENERATED branch routes
  to Grok. The web-sourced (real-entity / real-photo) branches are unchanged.
- **thinking_level** is per call-site, never global: content GENERATION
  (`REASONING_THINKING_LEVEL`) and close-answer ADJUDICATION
  (`JUDGE_THINKING_LEVEL`) run HIGH; hosting banter runs LOW (the `GoogleLLM`
  default); complex conversational turns (disputes/adjudication/ambiguity/
  multi-step) escalate to HIGH via `_thinking_level_for_turn` in `llm_node`,
  which overrides `_opts.thinking_config` for that turn and restores in
  `finally`. Gemini 3.x accepts only `low`/`high`.
- **Adult-deck reasoning effort per lane (HOTFIX-005 X13).** On
  `grok-4.20-multi-agent`, `reasoning.effort` is an **agent-count dial**, not
  thinking depth: `low`=4-agent, `high`=16-agent — a 4× fan-out on latency
  AND spend. Effort is matched to task complexity, not lane importance:

  | Lane | Model | Effort | Rationale |
  | --- | --- | --- | --- |
  | Adult vocal (front-facing) | `grok-4.5` (`adult_vocal_effort`) | **`medium`** | Latency-critical — a player waits through every token. `high` cost ~5s TTFT in `lily-FFDEAE` (llm_ttft p50 4,999ms / p95 8,254ms; non-adult vocal ~1,102ms). `low` for max speed, `high` to restore deep banter. |
  | Adult question generation | `grok-4.20-multi-agent` (`adult_reasoning_effort`) | **`low`** (4-agent) | One trivia line is a TCP-vs-UDP-class task; 16 agents is disproportionate in time and spend. |
  | Hard synthesis (future) | `grok-4.20-multi-agent` | `high` (16-agent) | Reserved for tool-enabled current-events / corpus building where the arsenal absorbs the wait. |

  `xhigh` is accepted but its exact agent-count/cost mapping is **unconfirmed**
  against the vendor — treat as ≥16-agent, not for use until measured. Effort
  is a spend multiplier; record measured cost per question when the
  multi-agent lane is exercised. Overrides: `LILY_ADULT_VOCAL_EFFORT`,
  `LILY_ADULT_REASONING_EFFORT` (`off` disables the param for ids that reject
  it).

## Both-sides record, continuous recognition, variety (WO-LILY-RECOGNITION-VARIETY-001)

From the 2026-08-04 solo-probe fixture (`lily-CC9E19-19c2b804`) — the
machine ran clean; the defects were experiential:

- **Both sides of the call persist.** Lily's own turns (post-say-gate
  final text, recorded at PLAYOUT so a swallowed turn never counts as
  said) write to `lily_transcripts` as `speaker_label='LILY'` rows (the
  `speaker_name` slot carries the primary speech-act key) and interleave
  into the session report. Interrupted turns carry `…[cut off]`.
- **Recognition is continuous, not a door-check.** A group resolution
  landing on an existing group MID-CALL (name-hash or voiceprint) loads
  memory/prefs/version-stamp and fires one acknowledgment beat
  (`maybe_fire_late_recognition`, once per session) with the refresher
  offer, prefs-usual, and what's-new delta folded in — but only at a safe
  seam. Delivery claim/pending playout, open answer window, adjudication,
  clarify, host speech, or question transition sets
  `late_recognition: DEFERRED`; the reveal/round-score completion seam flushes
  it before N+1. Recognition never overlays a live question.
- **Claimed returner is persistent session truth.** Phrases including
  "not my first time" and "I have been at/on your table before" set
  `_returner_claim_seen` for the whole call. A blank lookup never disproves
  the player's memory: `can_claim_empty_memory()` stays false and the TTS
  choke rewrites `clean slate`, `no saved voices`, `no past games`, and
  equivalent settled-absence claims to the inventory-stable still-checking
  sheet. Do not guess a cause (device/browser/etc.); believe the player and
  keep checking.
- **SAID-ALREADY ledger + variety law.** The scorekeeper tracks praise
  words spent, openers used, and topics already explained (from the
  agent-turn record); the state block carries the ledger and the prompt
  (NEVER THE SAME BEAT TWICE) forbids re-delivery — fresh-minted
  celebrations every beat. Say-gate `REPEAT_FLAG` lint (log-only) makes
  cycling a telemetry count.
- **Early buzz-ins are captured** (the fixture's Q5 root cause,
  DB-audited): finals spoken between the delivery claim and window open
  buffer per-question and replay at open, then run the instant Tier-1
  fast path — the "no early buzz-ins" v1 concession is retired.

### WO checklist (standing rules)

- **Manifest rule:** any WO shipping a player-facing feature appends its
  one-line description to `lily_capabilities.py` and bumps
  `LILY_FEATURE_VERSION` — and direct changes outside the WO trail
  (Rami's voice presets are the precedent) get added at commit time.
  The manifest is built from the CODEBASE, the only complete record.
- **Options-block rule:** askable features carry a `prompt_marker`; the
  WHAT THE TABLE CAN ASK FOR block must contain it (CI-enforced).
- **Tool-lint rule (WO-LILY-CAPABILITY-LINT-001):** new player-facing
  tool → manifest entry (listed in that entry's `tools`) or an explicit
  `LILY_INTERNAL_TOOLS` flag, enforced by CI both directions
  (`tests/test_capability_lint.py` — every registered tool accounted
  for; every manifest `code_ref` resolves to living code).
- **Audio-pipeline enable gate (WO-LILY-NC-BENCH-001 Task 5, permanent):**
  any WO that enables or re-enables an audio-pipeline component (noise
  cancellation, dereverb, VAD/interruption machinery, STT/TTS transport
  changes) MUST (a) search the repo history for prior removal/kill
  records of that exact component (`git log -S`, README/CHANGELOG grep)
  and quote the findings in the WO, and (b) pass a bench join-path test
  on an isolated slot before production contact. **No exceptions for
  components believed fixed by upstream version changes** — "believed
  fixed" is precisely the case the gate exists for; WS-14 re-enabled
  Krisp NC at 1.6.6 against a documented in-repo kill history, and it
  cost four production sessions.

## The say gate (speech-boundary bug class, 2026-07-14 WO)

**Standing suppression-health rule (OR amendment R6):** the post-session
health criterion is `LILY_SAY_SUPPRESSED ≈ zero`, and the corollary is
policy — EVERY suppression in any log bundle is a defect report against
the upstream path that generated the duplicate, root-caused, never
accepted as cost of business. The gate's job is to make failures
survivable, not invisible; a working safety net is exactly the mechanism
by which the bugs it catches stop getting fixed (the Lovebirds
generation-gate lesson). Post-DESYNC Sub-agent B, the known suppression
source is gone — anything remaining is new signal.

The live bug class — double greeting, double question delivery (BUG-2), a
state block read aloud, and an answer leak (the Bosporus answer spoken
BEFORE its question) — is closed by one gateway: `lily_say_gate.py`
(pure stdlib, offline-tested) plus its wiring in `lily_agent.py`.

### Idempotent speech acts

Game-critical speech acts carry state keys — `session_greet`,
`session_rejoin`, `q_{N}_delivery`, `q_{N}_reveal`, `q_{N}_verdict`,
`round_{N}_scores`, `finale` — in a per-session `SpeechActRegistry` on
`LilyGame`. Keys are
claimed with an atomic check-and-set **at dispatch time** — never at
playback completion, which is exactly the race window that produced the
live duplicates. Every claim is then bound to its concrete `SpeechHandle`.
Lifecycle: **claim at dispatch → bind to handle → confirm that handle at
playout completion → release that handle on playback failure** (a
claimed-but-never-played act releases so a retry can redeliver — the
19:27:52 swallowed-delivery class; a silent duplicate cannot confirm the
original handle, and a confirmed act stays done forever). All code-triggered speech
routes through `LilyGame.gated_say(key, act, instructions, source)`:
a first claim logs `LILY_SAY | act=... | key=... | source=...`, a second
claim logs `LILY_SAY_SUPPRESSED | reason=dup | key=...` and does NOT
speak. The session opener has two trigger paths (agent `on_enter` and the
entrypoint) — both dispatch the same instructions under `session_greet`,
so the loser of the race is silent; a reconnect uses its own
`session_rejoin` key and never trips `session_greet`. Keyless dispatches
(steal window, skip, mode reverts, the prefetch nudge, game start) still
log `LILY_SAY` for the audit trail. Note: `key=None` on `question_nudge`
bypasses the dup key — barge release of a pending claim also reopens the
door (see triage table below).

### Delivery re-fire triage (double question / double congrats)

Silence was treated as worse than a second ask — several watchdogs can
re-speak the same `q_N`. Grep the bad minute for `source=` on
`LILY_SAY` / `LILY_WATCHDOG` / `LILY_WINDOW`:

| Log / source | Gun |
|---|---|
| `UNDELIVERED_REFIRE` / `source=undelivered_reconcile` | Watchdog thinks Q never aired |
| `IDLE_REARM` / `source=idle_watchdog` | Idle re-arm nudge |
| `DELIVERY_NUDGE` / `NUDGE_NEAR_MISS` / `source=window_fallback` | Claim missed after organic ask |
| `NEAR_MISS_CONFIRM` / `UNDELIVERED_NEAR_MISS` | Fixed path: confirm + open, **no** re-read |
| `source=post_reveal` | Legitimate N+1 |
| Barge `RELEASED` then another delivery/verdict | Re-air after interrupt |
| Two `act=verdict` same `q_N_reveal` | Organic+code or key released |
| `COMMIT_FAILED` / praise without `record_result` | Score path |

**Near-miss contract (≥0.9 spoken/prompt):** `force_confirm_delivery_heard`
claims+confirms `q_N_delivery` and opens the window — never dispatches a
full sheet re-read. Open symbols: `reconcile_undelivered_claim`,
`on_agent_speech_finished` (nudge), `adjudicate` (verdict), `record_result`.

**Cut-delivery contract (2026-08-09, barge-in is normal).** Every gun above
descends from "silence is worse than asking twice". In a game where the
table talks over the host by design that trade is inverted — re-reading is
what produces the mess. `lily-2C489B` read one question three times
(22:49:37 / 22:49:47 / 22:50:05), each cut on the same word, and never
asked it. Two rules bound the loop, both in `delivery_reached_the_table`:

- An **interrupted** delivery whose aired text already presented the
  question (`_delivery_text_matches_armed` — the predicate the delivery
  path already owned) confirms and opens the window instead of re-arming.
  `interrupted` only: a **suppressed** turn aired nothing, and confirming
  it there reopens the #3418 ghost-window hole.
- `_DELIVERY_MAX_CUT_REAIRS` (2) caps re-reads; past it the question goes
  back to supply via `_release_armed_question_to_supply`
  (`DELIVERY_CUT_EXHAUSTED`).

**One owner speaks the question.** `unowned_kickoff_must_suppress` covers
the armed question itself, not just kickoff debris: a turn without
`q_N_delivery` ownership may not present it. Live 22:49:15 put the
recognition beat, "want a refresher, or straight in?", and the question in
one turn — asking the table what it wanted and answering for them.

**The glass never waits for the voice to finish.** `publish_question_to_glass`
fires at delivery playout START (`note_playout_started`), not at
window-open, and drops `_phase_hold` there. Both used to bind on playout
COMPLETION, which a barged delivery never reaches — the board sat on the
lobby for a whole session with a signed image URL in hand. Window-open
still calls it as an idempotent backstop. `record_question_asked` moved to
the same seam: the durable `lily_asked_history` burn happens when the table
HEARS a question, never at arm.

**Stale-claim recovery (WO-LILY-HOTFIX-001).** The 08-06 P0 exposed a
third lifecycle state with no exit: claimed, never played, never failed.
Krisp NC wedged RoomIO audio setup, so the greet's dispatched speech
never reached playout AND produced no failure event — no confirm, no
release, `session_greet` frozen `pending` — and the entrypoint's
belt-and-braces retry was dup-suppressed against that frozen claim.
Four consecutive sessions opened permanently deaf and mute, the silence
enforced by the say gate itself. The rule now enforced: **dup
suppression applies only to re-air of an utterance that actually played
within the same session (`confirmed`), or one genuinely in flight.**
Two mechanisms, both in `gated_say`:

- **Supersede on retry:** a retry that hits a pending claim older than
  the playout deadline (12s), whose speech never started airing
  (`agent_state → "speaking"` tracking) while nothing else is on the
  air, releases the wedged claim and speaks
  (`LILY_SAY | STALE_CLAIM_SUPERSEDED`).
- **Watchdog:** every keyed dispatch arms a watcher; if the claim is
  still pending past the deadline with playout never started, it
  releases and re-dispatches (`LILY_SAY | STALE_CLAIM_RELEASED`),
  bounded to 2 retries per key before declaring the audio path down
  (`LILY_SAY | STALE_CLAIM_EXHAUSTED`) — exhaustion leaves the key FREE,
  never poisoned. A long monologue mid-playout is protected twice: by
  the playout-started ledger and by the `host_speaking` re-check.

The registry remains **per-session in-memory state** — no persisted
consumed-keys ledger exists, so a cross-session claim leak is
structurally impossible (regression-pinned in
`tests/test_wedge_recovery.py` along with the whole recovery contract).

### BUG-2: one authoritative question delivery

The `lily_begin_round` tool result carries the question payload (prompt +
category) and the post-tool turn is the SOLE deliverer — `start_game`
dispatches no racing instructed reply for the `host_tool` source, and the
prompt states the contract (tool-call turn: transition line only; the
next turn asks the question exactly once; the reveal repeats the answer,
never the question). Enforcement is physical, and since the desync WO it
is structural: the tool arms `expect_delivery()`, the post-tool turn
claims `q_{N}_delivery` in `tts_node` at dispatch (regardless of
phrasing), and any later turn that textually re-performs a delivered
question fails the claim and is replaced with silence (no retry —
suppressed, not swallowed). Banter after a registered delivery is never
suppressed — only textual re-asks are. See "Loop engagement" in
[CHANGELOG.md](CHANGELOG.md) for the full claim-trigger table
(`LilyGame.register_delivery_claim`).

### Voice-game turn ownership (2026-08-07)

One user turn has one speech owner. During a live answer window, an exact
Tier-1-correct transcript is marked for deterministic adjudication; the
matching `on_user_turn_completed` hook raises LiveKit `StopResponse`, so
the ordinary conversational LLM cannot race the committed verdict with
its own “correct” or congratulation. A normal question has one short
verdict/reveal beat and then advances. At round boundaries the short
verdict uses `q_{N}_verdict`, while the separately keyed
`round_{N}_scores` flourish owns the transition to the next question.
Per-handle confirmation therefore cannot start the new category while
the previous round's standings are still playing.

The ownership check exists on BOTH event-order paths. The public
`user_input_transcribed` callback marks a committed answer when it arrives
first; `on_user_turn_completed` independently performs the same cheap
Tier-1 check when it wins the race, before default reply generation starts.
Their one-shot reservations consume each other. Explicit question-only
SpeechHandles are separately tagged and rewritten to
`rendered_armed_question()` on first pass, so a recognition beat cannot take
the delivery claim and prior-answer praise cannot ride into the next ask.

Asking also transfers the floor physically, not just by prompt. For
non-game-delivery speech, `tts_node` clips everything after the first
completed question (`lily_yield_after_first_question`): “Want science?
Or history?” becomes one question, and a question followed by unsolicited
explanation ends at the question. Game deliveries are exempt because MC
options legitimately follow the stem.

The input path is tuned for quiz answers: `InterruptionOptions.min_duration`
comes from `LILY_INTERRUPTION_MIN_DURATION` (default 0.25s, minimum 0.1s)
instead of the former 0.8s floor. A Tier-1-correct freeform answer heard
during delivery follows the same answer-aborts-read contract as MC:
interrupt the active handle, seed the pre-window answer even if the claim
was already released, open the window, and adjudicate without re-reading.
Adaptive false-interruption pause/resume remains enabled, so lowering the
floor does not turn every noise burst into a permanent cut.

### Leak filter

The injected state block rides as SYSTEM-role context wrapped in a
sentinel envelope (`<lily_state>…</lily_state>`). `tts_node` runs
`lily_filter_leaks` before the hygiene strip: whole envelopes, envelope
fragments (chunk-boundary partials like `<lily_state`), and any line
carrying a bracketed metadata marker (`[GAME STATE]`, `[room read:`,
`[env:`, `[RETURNING TABLE]`, `[state note:`) are deterministically
removed and logged as `LILY_SAY_SUPPRESSED | reason=leak`. Ordinary
`[bracket]` audio tags and clean text pass untouched. A leak whose ONLY
marker is `[state note:` (the honesty assist below — committed scores,
never answer material) is stripped but does NOT trigger the burn
protocol.

### The honesty rule (WO-LILY-DESYNC-HONESTY-001 C)

When state and experience visibly disagree — a lagging board, a
repeated-feeling question, a score a player disputes — Lily says the
honest minimal thing ("you're right — let me re-sync, one sec") and
moves on. She may acknowledge a hiccup; she may never explain one with
fiction (live evidence, twice: "the digital board takes a second to
refresh once I submit to the database", "my system had to do a little
reboot" — both invented). The prompt contract is the `WHEN THE ROOM SEES
A GLITCH` block (positive framing, zero scalars — same lint discipline
as the room-read rubric); it also forbids confirming or denying what's
on a screen she cannot see (the 23:04 "Romney misspelling" class).

**Grounded-claims canon (RULINGS-001 R2, operator-approved verbatim;
structural teeth in PATCH-003 P4).** This is a truthfulness guardrail,
not a voice edit — a live fixture settled the urgency: a fabricated
refusal ("picture search is off tonight") that was false against the
`lily_image_attempts` ledger passed supervisory review because it wore
the honesty register. Tone is not a mechanism. The ratified canon:

> Lily speaks about the system from what she can actually see. What she
> perceives, has fixed, has enabled, or can receive comes from real
> results and live channels. When something didn't work, she says what
> actually happened and moves to fix it. When nothing has arrived, she
> says so. When a lane is off tonight, she names it plainly and offers
> what is available instead. When a player states a fact about the game
> record, she engages the fact itself. Being straight about the system
> is part of being a good host — the game is only fun if the table can
> trust what she says about it.

The structural half — state-read templating where a claim or refusal
takes its verb from a field-granular read (`media_mode`, generation-key
presence, heat, pipeline health separately readable), so both
fabrication directions are unproducible — lands in PATCH-003 P4.

Paired deterministic assist: when a player's utterance makes a checkable
claim about their published score
(`lily_scorekeeper.lily_detect_state_contradiction` — conservative:
score/board anchor word plus a concrete value claim or a stuck/desync
phrase, resolved rostered player required), the agent layer injects one
grounded `[state note: …]` line into the next turn's state block
(`LILY_HONESTY | STATE_NOTE` log), built from the score the scorekeeper
actually committed: "player is correct — …" when the claim matches
committed truth, the committed number with an explicit
never-validate-uncommitted-numbers instruction when it doesn't. The note
is one-shot (consumed when her acknowledging turn finishes playing) and
is context only — the leak filter above keeps it off the air.

### Score truth (WO-LILY-DESYNC-HONESTY-001 E)

Scores commit at adjudication, BEFORE Lily narrates (unchanged), and the
committed truth goes out **in the same tick as the verdict**: `adjudicate`
dispatches the attribute publish (players/scores) and the reveal metadata
together (`asyncio.gather`) — the scoreboard is never queued behind the
metadata network round-trip, and everything between that dispatch and the
reveal `gated_say` is synchronous for a non-final question, so score
commit, publish dispatch, and verdict-speech dispatch share one tick
(live 01:37:54: the screen showed zero while she called the point "safe
and sound"). Sub-agent B's structural claims make committing at
adjudication safe — delivery, answer, and verdict share one
question-number identity chain. Frontend half (prmpt_ui
`lily-surface.tsx`): if the `players` attribute is still stale 2s after a
spoken correct verdict (the `reveal` beat fires at TTS playback), the
winner's chip count-rolls optimistically to a display-only round-based
estimate; committed attributes reconcile on arrival and always win — a
timely backend never sees the overlay. Fixture:
`tests/test_desync_fixture.py` (attributes published within the
adjudication tick; non-zero on the board at the moment she says "on the
board").

### Need-to-know ambient context

The ambient state block **never** carries `canonical_answer`,
`acceptable_answers`, `reveal_color`, or the full prefetched-question
JSON: the scorekeeper's `current_question` line is prompt-only, and the
`NEXT QUESTION` line carries prompt + category (+ `choices` when the
format has them — spoken content, not answer material). The answer
reaches the vocal node only through the reveal-time instructed reply, and
the Tier-2 judge receives it in its dedicated non-spoken call. The vocal
node cannot leak what it does not hold.

### Burn protocol

A leak-filter hit while a question is armed/prefetched means its answer
may have gone out on air: the question is burned — `LILY_BURN |
question_id=...`, bank rows (`kb_` ids) marked `lily_questions.status =
'burned'` (migration 009), generated questions discarded, the prompt
added to the session's avoid list — and a replacement is pulled through
the existing bank/prefetch path. The bank fetcher serves only
`status='active'` rows (a missing column reads as active). The status
column is SHARED with the future tier-retirement sub-agent (e.g.
`status='retired'`); scope is global today — per-group burn rides
`lily_asked_history` later.

### Callout gating

Every tool that emits game-event packets on a live round carries the same
`game_started=True` gate as `lily_award_bonus`, refusing with an
LLM-readable recovery path ("call lily_begin_round first"), never a
silent no-op: `lily_award_bonus` and `lily_log_clarify`.
`lily_bind_speaker`'s `player_bind` packet is a roster event, not a game
outcome — binding is core lobby behavior and stays ungated, per the tool
gating principle above (as does `lily_note_fact`, deliberately, and the
WO-LILY-FORGETME-001 pair `lily_explain_memory` / `lily_forget_group`:
explaining memory and deleting it neither mutate game outcomes nor emit
game events — `memory_forgotten` is a memory-transparency packet — and the
deletion right must work from the lobby onward).

Tests: `tests/test_say_gate.py` (registry + leak filter, pure) and
`tests/test_say_gate_dispatch.py` (dispatch dedupe, BUG-2 contract,
need-to-know, burn, clarify gate).

## Mid-sentence cut integrity (WO-LILY-STREAM-INTEGRITY-002)

The 08-06 session `lily-0BD414-ba80eb97` cut off mid-sentence three times
in six minutes ("…how does that sound to you? If" [dead]; "…but I can't"
[dead]; "…so we won't" [dead]). **Root cause (WS-1), from the runtime
logs + transcripts: genuine player barge-ins mid-playout, not a stream
bug.** Each cut coincides within ~40ms with a player STT final that
answers the exact word she died on — the tell is instance 1, where she
cut on "If" and the player immediately said "If what?". The 1.6.6
speaking-state-no-audio ghost bug was ruled OUT (TTS completed normally
through every window; the only false-interruptions logged both
`resumed=True`, nowhere near a cut), as was the character-cap theory (the
framework's `StreamAdapter` sentence-splits every reply first). The real
defect: keyed game acts recover through the game loop, but **organic
conversational turns had no cut-recovery path** — resumption depended on a
player re-prompting.

**WS-2 — chunk-safe TTS dispatch (`lily_tts.py`).** `MAX_CHUNK_SIZE` sits
at 3,800, comfortably below the ElevenLabs per-request cap
(`ELEVENLABS_REQUEST_CHAR_CAP = 4200`) — a chunk over the cap 4xx's
mid-turn and, since earlier chunks already aired, kills the turn
mid-sentence. `_split_text` prefers a sentence boundary and, for a
boundary-less long sentence, breaks at the last whitespace (never
mid-word); every emitted chunk is `<= MAX_CHUNK_SIZE`. Dispatch is now
**claim-vs-delivery tracked** (the maya_jrvs model): a chunk counts
delivered only once its audio flushed, and if synthesis dies after the
first chunk aired, `undelivered_remainder` is recorded and logged
(`LILY_TTS | TAIL_CHUNK_UNDELIVERED | delivered=X/Y`) so the tail surfaces
as a partial-delivery gap the cut-recovery path regenerates — never silent
mid-sentence death.

**WS-3 — the cut-recovery contract (`lily_agent.py`).** A cut or
mid-stream-failed **organic** turn (no keyed claim to drive a game-loop
re-dispatch) arms an auto-resume watchdog (`arm_cut_recovery`). After a
grace window (`LILY_CUT_RECOVERY_GRACE`, default 3.5s — above
`false_interruption_timeout` so the framework's own pause/resume gets
first crack, and above healthy user-turn latency) it composes a **fresh**
resume from where meaning broke, in her honest "sorry, looks like I cut
out there" voice (`_CUT_RECOVERY_DIRECTIVE`), within one turn and with no
operator poke (`LILY_CUT_RECOVERY | RESUMED`). WS-2's failed-tail and
WS-3's barge/cut resolve through this **one** recovery path.

The **one-emission mandate is preserved**: the watchdog fires ONLY into
dead air. A real barge-in carries a user turn — the normal reply path
answers it (re-air-gated fresh, `arm_reair_gate`) and the watchdog stands
down. `on_user_turn_completed` stamps user-turn recency
(`note_user_turn`), any new speech starting cancels the pending resume
(`note_playout_started` → `cancel_cut_recovery`), and a newer cut
supersedes the older via a monotonic token. So the auto-resume never
double-speaks over a reply already in flight — it targets exactly the
false-interruption / provider-hiccup / dead-air case. Tests:
`tests/test_tts_chunk_safety.py` (split correctness, tail-chunk delivery
tracking) and `tests/test_cut_recovery.py` (arm/fire decision matrix,
one-emission guards, async watchdog).

## Multiple-choice rounds (WO-LILY-OMNIBUS-002, sub-agent G)

Two round formats: `freeform` (the classic open ask) and `multiple_choice`
(four options read aloud). The default session runs exactly ONE
multiple-choice round — round 2 — and the table can ask for the format
any time ("can we do multiple choice"): Lily grants it via the
`lily_set_round_format` tool, callable in any phase, effective on the
current/next round and sticky until changed again.

- **Flag**: `LilyScorekeeper.round_format` (`freeform|multiple_choice`) plus
  the sticky `round_format_override` set by the tool; both ride
  snapshot/rehydrate, and the state block's header line carries
  `format=...`. Round boundaries apply the schedule/override in
  `arm_next_question` (`apply_round_format_for_round`).
- **Generation**: when the target round runs MC, the generation prompt
  demands a `choices` array — exactly 4, canonical answer verbatim among
  them, two plausible distractors plus exactly ONE clearly-comically-wrong
  laugh option (pub convention), order randomized. The `choices` slot in
  the question `response_schema` (reserved since the P1 fix) is now
  active. KB-bank questions (and any generated MC question whose choices
  fail validation) get 3 synthesized distractors at prefetch — reasoning
  node only (`LilyReasoning.ensure_choices`); synthesis failure degrades
  the question honestly to freeform, never a broken 3-option read.
- **Delivery**: the prompt contract has Lily read the question and the four
  options exactly ONCE (never re-read unless asked). The room-metadata
  document gains two optional keys when the armed question carries
  choices: `choices` (the 4 option strings) and `eliminated` (50/50
  indices into `choices`) — published at window open (the delivery turn's
  playout completion; the earlier dispatch-time claim publish led the
  voice) and the reveal; absent for freeform questions.
- **50/50 lifeline**: `lily_use_fifty_fifty(player_name)` spends the
  player's one lifeline on the live MC question —
  `lily_fifty_fifty_eliminations` keeps the canonical answer plus one
  random distractor and eliminates the other two (never blind: if the
  canonical answer can't be located among the choices the lifeline is
  refunded). The eliminated indices ride the metadata; the tool result
  names the two dead options for Lily to cross out aloud, once.
- **Tier-1 MC matching** (`lily_tier1_evaluate_mc`, dispatched via
  `lily_tier1_evaluate_question`): letters ("B", "letter b", "option c" —
  a bare "a" survives article stripping), positions ("the second one",
  "number three"), or fuzzy/phonetic option text. A resolved wrong pick is
  a DEFINITIVE Tier-1 `incorrect` (no judge call); only mumbles escalate
  to Tier-2, and a malformed sheet (answer missing from choices) always
  escalates rather than ruling.

Tests: `tests/test_multiple_choice.py` (generation shape + synthesis,
letter/positional/text matching, 50/50, flag snapshot, metadata seam,
both tools).

## Latency discipline

Nothing blocking runs on the event loop's hot path, and slow calls are moved
off the reveal moment:

- **Checkpoints are threaded** (`asyncio.to_thread`) — the synchronous
  Supabase client on the event loop stalled the live audio pipeline a full
  cross-region round trip per score change before this.
- **Speculative Tier-2 judging:** ambiguous answers are judged in the
  background DURING the answer window (single-attempt calls, cached per
  candidate); the reveal consumes cached verdicts, so the judge round trip
  is off the reveal path. The batched at-reveal call survives only as a
  fallback when no speculation ran. Order stays timestamp-decided either way.
- **TTS connection pre-warm** at session start — the greeting's first
  synthesis skips the TCP+TLS handshake to ElevenLabs.
- **Preemptive generation repair (P2):** the state-block/adult-layer/memory
  injections moved from `llm_node` into `on_user_turn_completed`, applied to
  both the turn context and the persistent context with stable item ids and
  rewrite-only-on-change — so 1.6.6's equivalence check validates the
  preemptive LLM run whenever game state held still across the turn boundary
  (previously every run was discarded: 13 "chat context changed" warnings per
  session, double LLM cost). `llm_node` keeps one documented injection for
  instruction-driven turns (reveal/skip/steal/start — they bypass the hook and
  must see the just-armed question); those turns also pause preemptive
  generation (`LilyGame.instructed_reply`) instead of paying for runs that are
  dead by construction, resuming on playout completion.
- **Dedicated reasoning-node token budget (P1 truncation root cause):** on
  Gemini 3.x thinking tokens count toward `max_output_tokens`; the reasoning
  node shared the vocal node's 800-token budget and 3.1-pro's medium thinking
  starved the question JSON into mid-object truncation. Generation and
  verification now run on `LILY_REASONING_MAX_OUTPUT_TOKENS` (default 4096 —
  prefetch is off the hot path), the Tier-2 judge on
  `LILY_JUDGE_MAX_OUTPUT_TOKENS` (default 1024 — latency-relevant, small
  verdict).
- **Rolling latency telemetry:** per-turn `llm_node_ttft` / `tts_node_ttfb` /
  `e2e_latency` averages ride the 60s heartbeat into
  `lily_sessions.metadata.pipeline_latency` (and the final row at close), so
  lag is a SQL query mid-game:
  `select session_id, updated_at, metadata->'pipeline_latency' from
  lily_sessions order by updated_at desc limit 10;`
- **Hot-path glass publishes:** skip / start-game attributes / adult-enter /
  bonus / reconnect-rejoin use `publish_attributes_nowait` (or
  `ensure_future(publish_metadata)`). Adjudicate still **awaits** the
  metadata+attributes gather before verdict speech (screen-truth honesty).
- Known remaining baseline: ElevenLabs v3 sentence-chunked HTTP synthesis is
  the fleet standard (the low-latency model families don't support the v3
  audio tags her register depends on; the dialogue endpoint stays off per
  the fleet revert).

## Files

```
lily_agent.py        entrypoint + LilyAgent + LilyGame (windows, prefetch, publishing, tools)
lily_speech_delivery.py  gated_say + delivery claims, re-air / cut recovery, MC abort /
                     pre-window / early answer (LilySpeechDeliveryMixin; zero copy edits)
lily_scorekeeper.py  N-player roster, per-player state, answer windows, system-directed
                     classifier, state-block builder — pure local state, zero LLM calls
lily_binding.py      lily_bind_speaker name extraction (2s fragment accumulation, stopwords)
lily_addressee.py    addressee-label corpus (B1) pure logic: clarify-reply parser,
                     seconds-into-window, implicit label derivation (stdlib-only)
lily_evaluation.py   Tier-1 matcher (incl. n-best wrapper) + Tier-2 judge contract
lily_nbest.py        n-best ASR recovery (WO-ADDRESSEE-H1 Task 1): per-word
                     alternatives tap + config injection, utterance synthesis,
                     dispersion signal (stdlib-only core; defensive installer)
lily_reasoning.py    background node: prefetch + verification + judge transport +
                     picture-question dispatch (the ONE legal web/image seam)
lily_images.py       lily-images bucket storage ({source}/{sha1}.{ext}), cache-first
                     bank helpers, visible lily_image_attempts rows
lily_search.py       Exa + Tavily native lifts (httpx) — REASONING NODE ONLY (import
                     tripwire); conservative real-entity image sourcing
lily_imagegen.py     JRVS image-gen clone: aspect clamp, Gemini generation for
                     invented content, 'real or imagined' reference round
lily_persistence.py  Supabase: sessions, transcripts, answers audit, voiceprints, KB bank
lily_memory.py       persistent cross-session memory: session summaries, the
                     [RETURNING TABLE] block, KB-bank adult-mode guard (stdlib-only)
lily_bank.py         bank curation: banking-on-generation with near-dup detection,
                     per-group asked history, gated category proposals (stdlib-only
                     pure logic + thin Supabase I/O)
lily_bank_tuning.py  difficulty self-tuning + retirement decisions (pure) and the
                     session-end tuning run (thin DB I/O)
lily_say_gate.py     outbound-speech gate: markdown/emoji strip ([tag]-preserving),
                     SpeechActRegistry (idempotent speech acts), state-block leak
                     filter + sentinel envelope (the designated choke point; stdlib-only)
lily_forget.py       right-to-be-forgotten pure logic: tombstone, cascade plan,
                     yes/no confirm parser, explain-memory + result shapes,
                     disclosure cap (stdlib-only)
lily_tts.py          ElevenLabs v3 wrapper (lbs_tts lift; byte-alignment carry, 5K split,
                     set_voice runtime swap)
lily_voice_switch.py voice-preset switching tools (Zuna port): lily_list_voices +
                     lily_switch_voice over voice1 (primary) / voice2 (Raven's)
lily_capabilities.py the capabilities manifest: versioned feature list, rematch
                     delta, availability layer, options-block CI markers
                     (stdlib-only, pure)
lily_vision.py       xAI Grok vision (Zuna port): lily_analyze_image tool +
                     the player-photo ingest's describe call (structured
                     failure contract, never raises)
lily_config.py       ALL env access lives here
lily_audeering_client.py     devAIce Web API client + room-audio capture pipeline
                             (native lift of mjrvs_audeering_client + pipeline)
lily_audeering_consumers.py  gates, room-read banding, child-signal ladder, scene,
                             rubric loader + import-time zero-scalar lint, state store
prompts/lily_system.txt      the whole character (incl. <tts_guidelines>, <voice_output>)
prompts/layer_lily_adult.md  additive adult layer (register shift; group-directed only)
prompts/lily_room_read_rubric.txt  room-read rubric (loader-appended; zero-scalar linted)
migrations/001_lily_schema.sql          six lily_ tables (no pgvector)
migrations/002_lily_questions_seed.sql  30 curated questions (demo insurance)
migrations/003_lily_memory.sql          lily_memories + lily_questions.adult guard column
migrations/004_lily_questions_expansion.sql  200 curated_v2 bank questions
migrations/005_lily_addressee_log.sql   addressee-label corpus (applied to production 2026-07-14)
migrations/006_lily_session_reports.sql lily_session_reports (write side; assessment filled later)
migrations/007_lily_memories_player_names.sql  lily_memories.player_names audit column (+GIN index)
migrations/008_lily_acoustic_trajectories.sql  per-turn acoustic snapshots + addressee
                                               acoustic_snapshot column
migrations/009_lily_question_status.sql  lily_questions.status lifecycle column
                                         (burn protocol; shared with tier retirement)
migrations/010_lily_asked_history.sql    per-group served-question ledger (no-repeat
                                         guard + tuning exposure floor)
migrations/011_lily_category_candidates.sql  gated category proposals tally
migrations/012_lily_question_images.sql  lily_questions image_url/image_source/
                                         image_license_note + lily_image_attempts
                                         (visible error rows)
migrations/013_lily_group_prefs.sql      lily_group_prefs (opaque per-group prefs jsonb;
                                         forget-cascade + re-key interlocked)
migrations/014_lily_adult_bank.sql       principal adult bank + MC/image prompt columns
migrations/015_lily_transcript_event_id.sql  idempotent transcript retry keys
migrations/016_lily_question_draw_index.sql  bounded bank-draw composite index
migrations/021_lily_voice_identity.sql  RLS-protected ECAPA group centroids
                                        for device-independent recognition
tests/               1450+ tests, run with `python -m pytest tests/` — no network; needs
                     livekit-agents 1.6.6 + google-genai installed
                     (test_award_gate.py / test_context_blocks.py /
                     test_say_gate_dispatch.py / test_forget_flow.py /
                     test_multiple_choice.py / test_group_prefs.py import livekit)
```

## Persistent memory (rematch)

Lily remembers a table across sessions, keyed on a stable **group identity**.
The resolution chain (every step observable — `LILY_MEMORY | GROUP_ID |
source=... group_id=...` is logged every session, upgrades as
`LILY_MEMORY | GROUP_ID_UPGRADE | source=... old=... new=...`):

1. **(a) `lily_group_id` metadata** — device candidate only. Read from BOTH the
   dispatch/job metadata (`ctx.job.metadata`; the token route mirrors
   participant metadata into `RoomAgentDispatch`, available immediately) and
   the first non-agent participant's token metadata (JSON
   `{"lily_group_id": "<uuid>"}`). Live evidence showed the participant is
   often NOT in `remote_participants` yet when `ctx.connect()` returns, so
   the scan polls up to 3s for the first participant, and a
   `participant_connected` hook stages a late device candidate if a
   metadata-carrying participant joins. Metadata never activates memory.
2. **(b) voiceprint match** (as current speech lands, and at game start):
   this session's `stt.get_speaker_ids()` identifiers are matched by exact
   string overlap against the staged candidate voiceprints first, then
   against roster-name lookup for ordinary weak-id resolution. A hit reuses
   that prior `group_id`
   (`source=voiceprint_match`). Best-effort: it only hits when Speechmatics
   returns stable identifier strings for a returning voice.
   **Identifier-refresh reality (2026-07-16, verified in production):**
   Speechmatics REFRESHES the identifier blobs every session for the same
   voice (seven same-voice rows, seven distinct strings, one shared prefix
   family) — exact identifier-string overlap can never match across
   sessions. The durable confirmation is the LABEL ROUND-TRIP: stored
   identifiers are injected as `known_speakers` under player-name labels,
   and the engine assigns one of those labels to a live stream only when
   its own biometric match recognizes the voice
   (`lily_candidate_labels_confirmed`, logged as
   `DEVICE_VERIFY_LABEL_MATCH`). Transient S-number labels never count.
3. **(c) name-set hash fallback**: `grp_` + sha1 of the normalized sorted
   player-name set joined with `|` (e.g. `sha1("carly|kali|rami")`) —
   deterministic, so the same table of names re-keys to the same group every
   night (`source=name_set_hash`). This resolves only after names bind, so
   it runs as a **mid-session upgrade at game start**: the session begins
   under the room name, re-resolves when `start_game` fires, re-keys this
   session's rows already written (`lily_sessions`, this session's
   `lily_group_facts`, and — only when upgrading off the room-random id —
   `lily_speaker_voiceprints`; `LILY_MEMORY | REKEY | table=...`), reloads
   the `[RETURNING TABLE]` memory if no questions have been played, and
   re-fires enrollment so voiceprints land under the resolved id.
4. **(d)** `LILY_GROUP_ID` env override, then room name (random per session
   — nothing re-keys on it; the upgrade path exists exactly to escape it).

**Tables:** `lily_memories` (one row per session, upserted idempotently on
`session_id` from both `finish_game` and the shutdown callback unless forget
disabled identity persistence: final
`players [{name,score,streak}]`, `winner`, `question_count` — read straight
off `scorekeeper.question_number` — `highlights` callouts, a deterministic
template `summary`, and `player_names`, the normalized sorted name-set audit
column from migration 007; if production hasn't applied 007 yet the write
retries once without the column) plus `lily_group_facts` (running-bit
material) and `lily_speaker_voiceprints`.

**Group facts now have writers:** the `lily_note_fact(player_name, fact)`
tool (the prompt tells Lily to log each player's lobby fact and any
callback-worthy detail) and `best_wrong_answer` callouts both persist to
`lily_group_facts` under the RESOLVED group id (deduped per session,
fire-and-forget, `LILY_MEMORY | GROUP_FACT`); `lily_load_group_memory`
already reads them back into the `[RETURNING TABLE]` block on the next
night.

**Voiceprint enrollment** (`lily_speaker_voiceprints`) fires on the first
binding and every later binding (late guests included), again at game start /
group-id upgrade, and once more
— awaited, inside the shutdown gate, so teardown can't race it — at session
close for late binders (unless forget ran, which disables enrollment for the
remainder of that session). The upsert is idempotent on
`(group_id, speaker_label)`, and the group id is read at write time (a
callable), so a mid-session upgrade re-keys in-flight enrollments too. Weak
provisional room/name-set identities are re-keyed to the resolved group;
refreshes preserve an existing player-name mapping when rebinding has not yet
caught up, and lookup checks both `player_name` and enrolled speaker labels.
No-silent-crash: every failure path logs a structured
`LILY_ENROLL | FAILED | reason=...` (no client, plugin API drift, no
identifiers yet, no usable rows, exception); success logs
`LILY_ENROLL | OK | trigger=... group=... speakers=...`.
**1.6.6 API notes (re-verified — plugin source byte-identical to 1.6.4):**
`stt.get_speaker_ids()` is **async** at `livekit-plugins-speechmatics 1.6.6` and returns useful identifiers only
after ~5 spoken words per speaker (the multi-trigger schedule exists
precisely so an early empty result self-heals); the code tolerates a future
plugin making it synchronous, and logs `reason=no_get_speaker_ids_api` if a
bump removes it. On a rematch, injected `known_speakers` carry player-name
labels, so returning voices may surface with the player name as the label —
the enrollment row mapper handles both spellings.

**Durable voice identity is active in production.** Speechmatics' refreshed
identifier blobs still support same-device candidate verification, while the
device-independent path uses an ECAPA embedding of the shared-mic probe and
matches it against the RLS-protected `lily_voice_identity` pgvector centroid
for the group. The Docker image installs CPU torch/torchaudio + SpeechBrain
and prewarms the model during build; a broken model fails the build rather
than silently shipping device-only recognition. Audio capture is independent
of the optional audEERING pipeline. The one session match is not consumed
until enough PCM exists, then runs immediately; close-time enrollment folds
the session embedding into the running centroid inside the shutdown gate.
`forget me` disables further identity persistence for the session, deletes
the ordinary voiceprints/memory rows, and retires the durable centroid so it
cannot match again.
If a short earlier session enrolled a named voiceprint but did not qualify
for a game-memory row, verification still restores that name in a
names-only `[RETURNING TABLE]` block; Lily may recognize the player but must
not invent a prior game, winner, score, or fact.

**What Lily remembers:** on a returning group, the last 3 games + group facts
are compiled into a compact `[RETURNING TABLE]` system block (~600 chars max,
injected in `llm_node` the same additive way as the adult layer): returning
player names, who won last time, running bits, total games — so she greets
players back by name and does callbacks with rematch energy. A block that
arrives via the mid-session group-id upgrade injects on her next turn the
same way. **Neutral-greeting rule:** with no memory data she is instructed
never to claim she remembers the table AND never to announce it's their
first time — history is referenced only when a `[RETURNING TABLE]` block
actually exists.

### Memory at the door (WO-LILY-DESYNC-HONESTY-001 F)

Two gates keep memory honest at the session boundary:

- **Greeting budget.** The composed greeting awaits group resolution +
  memory load up to `LILY_GREETING_MEMORY_BUDGET_SECONDS` (default 1.5s;
  `<=0` disables the wait) before dispatching under `session_greet` — the
  live failure was `[RETURNING TABLE]` landing one turn AFTER the greeting
  fired, cold-greeting a four-time returning table. An operator-pinned id or
  voice-verified identity may release returning memory. Dispatch/participant
  metadata settles only to the soft "device looks familiar" candidate
  greeting; a weak room id remains neutral. Timeout greets cold and lets
  recognition arrive naturally — the room is never blocked beyond the
  budget. Observable as `LILY_MEMORY | GREETING_AWAIT | settled/timeout`.
- **Write threshold.** A `lily_memories` narrative row is written only when
  the session played at least `LILY_MEMORY_MIN_QUESTIONS` questions
  (default 3 — the same count the summary reports) OR reached round 2.
  Below threshold the session row still writes through its own path but no
  narrative lands (`LILY_MEMORY | WRITE_SKIPPED | ... below threshold`) —
  an aborted one-question session ("No sole winner over 1 question(s).
  Final scores: Rami 0") must never come back as "last game" material.
  Existing sub-threshold rows are purged by a one-time production cleanup
  (delete `lily_memories` where `question_count < 3`).

Tests: `tests/test_memory_gate.py`.

### Group preferences (the "usual")

Lily remembers HOW a table likes to play, not just who they are.
`lily_group_prefs` (migration 013) holds one row per group: an **opaque
`prefs` jsonb dict**, persisted whole (upsert on `group_id`) on every
preference change. Keys are feature-owned — this WO owns `"pacing"`;
`round_format` / `media_mode` slot into the same dict when their features
land, with zero schema or plumbing changes (`lily_memory.lily_prefs_summary`
renders unknown keys generically).

**Pacing (`timed` | `relaxed`):** a sticky scorekeeper flag beside `mode`.
`timed` is exactly the previous behavior; `relaxed` stretches the standard
answer window by `LILY_RELAXED_WINDOW_MULTIPLIER` (default 2.0 —
`LilyGame._answer_window_duration`; the steal window keeps its own tunable)
and adds a looser-tempo note to the state block (no countdown talk, no
rushing anyone). The choice flips deterministically at the command layer
("let's play relaxed", "timed rounds", "no timer", negation-guarded — checked
BEFORE the start-game phrases so "let's play relaxed" is a pacing choice, not
a game start) or via the `lily_set_pacing(pacing)` tool (ungated — a
preference mutates no game outcome). Snapshot/rehydrate carry it across
reconnects; **seam addition:** the participant attribute `pacing` joins the
LWW set (`answer_window.duration_ms` already reflects the stretched window).

**The ask-once flow:** a returning group's prefs load at session start with
the memory; the `[RETURNING TABLE]` block gains a compact `usual:` line
("usual: relaxed pacing") and the greeting asks ONCE, after the composed
welcome — play the usual, or change anything? A yes/"the usual" needs no
ceremony and no tool call: `apply_prefs_at_game_start` sets flags from the
stored dict silently when the game starts. Changes update the stored usual.
The offer is latched (`_prefs_offer_made`) — never re-asked in a session; a
mid-lobby group-id upgrade that resolves prefs late rides the game-start
beat under the same latch. Cold groups get no ceremony at all: choices are
captured as they make them in the lobby.

**Interlocks:** `lily_group_prefs` is part of the forget cascade
(`lily_forget.OPTIONAL_GROUP_TABLES` — deleted and verified when present,
absent-table-skipped only for migration-013 lag; a forgotten table's
preferences are recognition data, and the in-session teardown clears the
prefs dict while tonight's live pacing survives as tonight's choice). On a
mid-session group-id upgrade the row **merge-re-keys**
(`lily_rekey_group_prefs`): `group_id` is the PK, so old and new dicts merge
key-by-key with this session's choices winning; the provisional row is
deleted only when it was the room-random session id.

**Adult-column guard (consent-safety):** `lily_questions.adult` marks
adult-register bank rows; `lily_fetch_bank_question` takes the session `mode`
and hard-excludes `adult=true` rows unless `mode == 'adult'` — an adult
question can never surface at a general-mode table. The deck cut is
two-directional (WO-LILY-DESYNC-HONESTY-001 D): in adult mode the bank serves
`adult=true` rows ONLY — a general row never surfaces in the adult segment
(the live "wait, THAT's the adult section?" defect); adult-deck exhaustion
falls through to mode-aware generation, never to the general bank.

**Adult deck = same armed pipeline (WO-LILY-DESYNC-HONESTY-001 D):** adult
questions flow through the identical identity chain as general — prefetch →
`arm_next_question` → `q_{N}_delivery` claim in `tts_node` → answer window →
`q_{N}_reveal` in `adjudicate`. No question reaches speech without an armed
`q_N` identity, so every adult reveal is keyed and dedup-able (the 2026-07-15
double-played reveals came from identity-less freestyle presentation during a
supply gap). **Mode switches flush and re-arm** (`flush_for_mode_switch`, both
directions — `lily_enter_adult_mode`, spoken "back to normal", the
child-signal veto, and the breaker-trip auto-revert): the armed and prefetched
questions were drawn from the old deck, so they are flushed (and stay in the
drawn-set — never re-served), the in-flight draw is cancelled with a
`supply_mode` commit guard discarding any straggler
(`LILY_PREFETCH | MODE_SWITCH_DISCARD`), and `start_prefetch()` relaunches
immediately — the prefetch auto-advance re-arms and the idle watchdog
backstops. The one-beat gap is honest: a status note in the state block says
the new deck is drawing (cleared by the next successful draw), and Lily is
told to vamp, never to re-ask the old deck or invent a question. **Categories
follow the bank:** adult rows carry their own categories (`adult_couples`,
`adult_kink`, migration 014); the round-family rotation is mode-aware
(`_category_for_round` rotates the adult families in adult mode) and never
overwrites or announces over a served question's own label — adult questions
are never introduced as "academic category". She can style the category out
loud; the published label follows the row.

**Session reports:** one `lily_session_reports` row per session at close
(idempotent upsert on `session_id`): the in-memory transcript (the
scorekeeper's rolling buffer — never re-queried from the DB) plus `game_stats`
(final standings, rounds/questions played, per-player answers
attempted/correct, mode changes, callouts, `duration_s`). A session that
executed or partially executed forget does not write a report.

**Clinical desk (WS-12, WO-LILY-OMNIBUS-003):** the assessment fill is now
built (`lily_assessment.py` — it never existed before; every production row
sat at `'pending'`). Two triggers, neither on the shutdown/close path
(fleet: shutdown callbacks fire in 0-22% of sessions): the **wrap-up beat**
(`finish_game` writes the report row and assesses it immediately — exit
bar: assessed within `LILY_REPORT_DEADLINE_S`, default 5 min) and a
**reconciliation sweep** at session start for orphaned pending rows
(aborted sessions / past failures; assessed from stored data,
`LILY_REPORT_SWEEP_MIN_AGE_S` grace, `LILY_REPORT_SWEEP_LIMIT` per boot).
The desk runs on the reasoning model (`LILY_ASSESSMENT_MODEL` to pin), its
own genai client (§11.5 isolation). The fill is pending-guarded (UPDATE
WHERE `report_status='pending'` → sets `assessment` +
`report_status='complete'`), so the close path's later transcript re-upsert
(which omits both columns) and any re-run never clobber it. Failure is
fail-visible — `LILY_REPORT | ASSESS_FAILED` at ERROR — and leaves the row
pending for the sweep to retry.

### Addressee-label corpus (B1)

The training-data flywheel for "was that an answer or table talk?":
`lily_addressee_log` gets one row per **finalized** segment while an answer
window is open, plus any finalized segment the agent acts on (scored,
skipped-on, clarified, system-directed). Every write is fire-and-forget via
`asyncio.to_thread` — zero hot-path cost. Each row carries the context a
future classifier needs: `phase`, `answer_window_open`,
`seconds_into_window` (off the scorekeeper's window `opened_at`),
`fuzzy_matched_answer` (Tier-1 verdict against the live question's
`acceptable_answers`; null with no live question), `system_directed_hit`
(the existing vocative-"Lily" classifier), and
`agent_action ∈ scored|ignored|clarified|adjudicated_other`.

Labels land three ways (`label_source`):

- **`implicit_scored_unappealed`** — at adjudication commit, the winning
  utterance and every scored-incorrect utterance get `label=host_directed`
  (an UPDATE on the row id kept at insert time, never a double insert).
- **`implicit_appealed`** — the documented hook for an appeal that re-enters
  Tier-2 and overturns an attribution: the corrected label replaces the
  implicit one (appeals are currently handled in-prompt; no code path yet).
- **`explicit_clarify`** — ground truth from the clarify moment: the
  `lily_log_clarify(player_name)` tool fires whenever Lily asks a player
  "is that your answer or are you thinking out loud?". It marks the player
  pending-clarify, emits the `clarify` `{name}` packet on `lily.events`
  (frontend pulses that player's chip), and logs the clarified utterance
  with `agent_action=clarified`. The player's NEXT finalized segment
  resolves it: affirmative ("yes", "that's my answer", "final answer") →
  `label=host_directed`; negative/thinking ("no", "just thinking",
  "talking to him") → `label=deliberation`; unparseable → `label=unknown`.
  The reply parser is pure and offline-tested (`lily_addressee.py`).

### State-prior thresholds (WO-ADDRESSEE-H1 Task 2)

The Tier-1 acceptance threshold is no longer static — a scorekeeper-owned
**prior-state machine** (`lily_scorekeeper.PRIOR_*`, pure, zero new models,
zero LLM) moves it with game state, per the report's strongest cheap
finding (the host's last dialogue act is a top addressee predictor):

- **`OPEN_WINDOW`** — a question was just asked, the answer window is open,
  no crosstalk → threshold **lowered** (`0.84` vs the `0.88` baseline;
  favor recall — a bare mangled proper noun right after the ask is almost
  certainly an answer).
- **`OVERLAP`** — diarization shows ≥2 speakers with temporally overlapping
  segments inside the window → threshold **raised sharply** (default
  `1.01`; any value above 1.0 disables Tier-1 auto-accept entirely — even
  exact/containment hits, whose similarity is 1.0, escalate to the Tier-2
  judge). Crosstalk is a deliberation prior: the right answer shouted into
  an argument gets checked, never auto-scored.
- **`HOST_SPEAKING` / `SCORING`** — Lily is on air (wired off the
  framework's `agent_state_changed` → `speaking` transition, which at
  livekit-agents 1.6.6 fires on actual TTS playout start and leaves on
  playout end/interrupt) or adjudication is in flight
  (`LilyGame._adjudicating`, mirrored onto `sk.adjudicating`) → biased
  against acceptance (default `1.01`): backchannels expected, nothing
  scoreable. `IDLE` (window closed, nothing special) keeps the pre-H1
  baseline exactly.

Precedence: `SCORING > HOST_SPEAKING > OVERLAP > OPEN_WINDOW > IDLE`.
Adjudication evaluates candidates under the prior their window was
**captured** in (`sk.window_prior_state()` — `overlap_flag` persists across
the window close), never under the SCORING state the evaluation itself
runs in.

**Overlap detection math** (`sk._note_speaker_span`): each final segment
inside the open window records a per-speaker span
`[segment_start_time, segment_end_time≈now]`; two spans from **different**
speaker identities flip `OVERLAP` when
`min(end₁,end₂) − max(start₁,start₂) > LILY_OVERLAP_EPSILON_SECONDS`
(default `0.3`, strict inequality — conservative by construction).
`UserInputTranscribedEvent` still lacks word timing at 1.6.6 (it gained only
an `item_id` field), but the raw
Speechmatics n-best tap now carries stream-relative word spans. A bounded
`LilyTimestampReconciler` maps those spans onto the event-arrival wall clock,
records drift/source telemetry, and falls back to arrival time when the stream
clock is unavailable. The flag resets on every window open (steal windows
included).

**Confidence fusion:** diarization confidence (speaker consistency plus top
ASR confidence, with event/attribution fallbacks) is combined with the latest
audEERING trajectory confidence only when the acoustic capture timestamp is
aligned with the reconciled segment clock. Misaligned samples log
`LILY_SYNC | ACOUSTIC_SAMPLE_MISALIGNED` and fall back to diarization only.
The fused value adds a bounded Tier-1 threshold penalty and, under
`PRIOR_OVERLAP`, conservatively demotes low-confidence roster attribution to
the open floor rather than crediting the wrong player. Separate diarization,
acoustic, fused, timestamp-source, and drift values remain on candidate audit
records.

Every classification decision logs its prior:
`LILY_PRIOR | state=OVERLAP threshold=1.010 overlap=True | ...` (scorekeeper
per-final, plus the agent's `source=instant_tier1` / `source=adjudicate`
call sites, and `LILY_PRIOR | OVERLAP_DETECTED` on the flip). Each logged
utterance persists its `prior_state` and `overlap_flag` to
`lily_addressee_log` (columns per schema amendment 5a).

**Env knobs** (all through `lily_config`): `LILY_TIER1_THRESHOLD_OPEN_WINDOW`
(0.84), `LILY_TIER1_THRESHOLD_OVERLAP` (1.01),
`LILY_TIER1_THRESHOLD_HOST_SPEAKING` (1.01), `LILY_TIER1_THRESHOLD_SCORING`
(1.01), `LILY_TIER1_THRESHOLD_IDLE` (0.88), `LILY_OVERLAP_EPSILON_SECONDS`
(0.3), `LILY_TIER1_CLARIFY_MARGIN` (0.15); fusion/alignment:
`LILY_ADDRESSEE_FUSION_DIARIZATION_WEIGHT` (0.75),
`LILY_ADDRESSEE_FUSION_ACOUSTIC_WEIGHT` (0.25),
`LILY_ADDRESSEE_ACOUSTIC_MAX_STALENESS_SECONDS`,
`LILY_ADDRESSEE_ACOUSTIC_MAX_FUTURE_SECONDS` (0.75),
`LILY_ADDRESSEE_CONFIDENCE_NEUTRAL` (0.65),
`LILY_ADDRESSEE_CONFIDENCE_PENALTY_MAX` (0.10), and
`LILY_OVERLAP_FUSION_MIN_CONFIDENCE` (0.42).

**Middle band (Task 4 surface):** `lily_evaluation.lily_tier1_band(similarity,
threshold, clarify_margin)` splits Tier-1 similarity space into
`BAND_ACCEPT` (≥ threshold), `BAND_CLARIFY` (within
`LILY_TIER1_CLARIFY_MARGIN` below it — where the deterministic clarify
question fires), and `BAND_REJECT` (below the band — act on the
classification, write the implicit label as wired in B1).

H1 honesty note: these priors **narrow the clarifying question's workload**
— they do not retire it. Text+context plateaus around 24–27% EER, so the
house rule (answers are said TO Lily) stays live and stated in the lobby.

### Judgment rubric + formalized clarify trigger (WO-ADDRESSEE-H1 Tasks 3–4)

Task 3 is texture: a `COMMITTED, OR THINKING OUT LOUD?` prompt block
(positive framing, zero scalars) grounded in pragmatic completeness — a
bare name right after the ask is a complete turn; the same words hedged
or fractured are deliberation; overlap or fractured syntax means check,
never score. Task 4 is the mechanism under it: when Tier-1 similarity
lands in the ambiguous middle band under the ACTIVE state-prior
threshold (`lily_tier1_band` == clarify;
`[threshold − LILY_TIER1_CLARIFY_MARGIN, threshold)`), the binary
clarify question fires deterministically — named player, "answer, or
thinking out loud?" — and the reply writes an EXPLICIT label to
`lily_addressee_log` through the existing pending-clarify machinery.
Rate-limited so the repair stays charming: once per question, at most
`LILY_CLARIFY_MAX_PER_SESSION` (default 3) per session
(`LILY_CLARIFY | BAND_TRIGGER` logs each firing). Outside the band the
classification stands and the implicit label writes as before.

Task 5b (privacy posture): `lily_set_training_optin` sets the
`lily_sessions.training_optin` flag (default FALSE) with a logged
timestamp — explicit host action only. It gates nothing yet (no audio
retention exists to gate); the flag and audit trail exist BEFORE any H3
audio-retention work can begin. Consent stack for that future work:
(1) the lobby memory disclosure, (2) `training_optin` per session,
(3) vendor-side truth as documented in the forget-arc section — all
three before a byte of audio is ever retained.

### n-best adjudication (WO-ADDRESSEE-H1 Task 1)

> **INCIDENT 2026-07-14 23:31 — injection DEFAULT IS NOW OFF.** Session
> `lily-B0CB8B-13a65381`: the Speechmatics VOICE endpoint's schema
> rejects the injected field at the protocol level ("Additional property
> max_alternatives is not allowed" → websocket 1003 → AgentSession
> unrecoverable close ~8s in — every new session died at startup). The
> defensive fallback covered plugin-shape drift, not a server-side
> schema rejection after the handshake, which no client-side guard can
> intercept. `LILY_STT_MAX_ALTERNATIVES` now defaults to **1** (patch
> disarmed, clean 1-best; the whole n-best pipeline no-ops on
> single-hypothesis sets). Do not raise it until the injected config is
> validated against the live voice-endpoint schema.

Tier-2 used to judge the 1-best transcript; deliberation and STT mangling
produce exactly the high-variance hypothesis sets where 1-best fails
("mad at gas car" / "madagascar"). Task 1 recovers the recognizer's
alternatives and runs BOTH tiers across the whole set (`lily_nbest.py`).

**Verified plugin behavior** (installed source of
livekit-plugins-speechmatics 1.6.6 / speechmatics-voice 0.2.8 /
speechmatics-rt 1.1.0 — read, not recalled):

- **There is NO per-utterance n-best anywhere in the stack.** The voice
  client collapses every recognition result to `alternatives[0]`
  (`speechmatics/voice/_client.py`, `_add_speech_fragments`) and the
  LiveKit plugin emits exactly one `SpeechData` per segment
  (`stt.py::_send_frames`). What IS recoverable is **per-WORD
  alternatives**: raw `AddTranscript` messages carry the full
  `results[i].alternatives` list (content + confidence + speaker), and the
  client's EventEmitter re-emits the raw message by name, so an extra
  `client.on("AddTranscript", ...)` handler taps them losslessly. **The
  per-word→per-utterance delta matters:** utterance-level hypotheses are
  SYNTHESIZED here, not recognizer-ranked — a 1-best backbone plus bounded
  single-word substitutions ranked by mean word confidence (no
  combinatorial paths, hard ceiling 8). Treat the synthesized set as a
  recall-widener, never as a true lattice.
- **There is no supported config knob for the count.** The plugin's
  `transcription_config=` kwarg is deprecated and ignored at 1.6.6, and the
  `VoiceAgentConfig.advanced_engine_control` merge is silently dropped from
  the wire (`TranscriptionConfig.to_dict()` is `dataclasses.asdict` —
  declared fields only, and `max_alternatives` is not declared anywhere in
  the installed SDKs). The working injection point is the StartRecognition
  message builder (`speechmatics.rt._base_client
  .build_start_recognition_message`), wrapped to add `max_alternatives` to
  the outgoing `transcription_config` dict. **Ceiling:** no client-side cap
  exists to document; the count on the wire is exactly
  `LILY_STT_MAX_ALTERNATIVES` (default 3, clamped to 1..8). Whether the RT
  service honors >1 per word is a server-side contract we cannot verify
  offline — if the server ever rejects the field, set
  `LILY_STT_MAX_ALTERNATIVES=1` (the injection kill switch).
- **Defensive by contract:** the installer (`lily_install_nbest_stt_patch`)
  never raises — any shift in plugin internals logs
  `LILY_NBEST | patch=failed` and returns False, and every downstream
  consumer treats the absent hypothesis dict as plain 1-best.

**Adjudication semantics:** Tier-1 fuzzy matching runs across all
hypotheses (`lily_tier1_evaluate_nbest`; precedence correct > uncertain >
incorrect — existing single-text functions untouched). Tier-2 receives the
set in the judge prompt as "the player may have said any of" with
confidences. **The judge-never-invents rule is unchanged:** the n-best list
widens what counts as the player having SAID the answer, never what the
answer IS — the judge still rules only against the supplied canonical
answer / acceptable variants, and the prompt restates that rule whenever
hypotheses appear.

**Dispersion signal:** `n_best_dispersion` (confidence variance across the
hypothesis set) is computed per utterance and logged
(`LILY_NBEST | dispersion=… hypotheses=…`). High dispersion is a
deliberation signal (report Track 1): above
`LILY_NBEST_DISPERSION_THRESHOLD` (default 0.02) a definitive Tier-1
verdict is demoted to "uncertain" — fractured deliberation escalates to the
judge instead of scoring. Edge contract: single hypothesis → 0.0, no
hypotheses → null. Both the hypothesis set (`asr_n_best`) and the
dispersion land on the `lily_addressee_log` row when available (absent →
SQL NULL; columns from schema amendment 5a).

## Noise cancellation — WS-14 memo (status: OFF, bench-gated return)

**Status (2026-08-06, WO-LILY-HOTFIX-001 / WO-LILY-NC-BENCH-001):**
`LILY_NOISE_CANCELLATION=off` is BOTH the slot-secret state and the code
default (`lily_config.noise_cancellation_mode`). NC's production record
is two documented kills in two deployments:

1. **1.6.4 — NcSession sample-rate SIGABRT** (`Input and output sample
   rates must be equal`): every job accept dead in ~2s. Origin of the
   kill switch.
2. **2026-08-06, 04:21–04:30 UTC — the RoomIO wedge at 1.6.6:** four
   consecutive sessions (`lily-813B86`, `lily-F70BF5`, `lily-90DAE0`,
   `lily-A7DAD8`) opened deaf and mute — NC on the join path wedged room
   audio setup; the greet never reached TTS playout
   (`tts_first_frame_ms` null on every row), zero mic frames reached
   Speechmatics (`stt_stream_disconnected_and_no_captured_speakers`,
   zero transcript rows — verified "nothing aired", not a persistence
   break), ~45s join delay, and the frozen `session_greet` claim
   dup-suppressed the greet retry (fixed — see stale-claim recovery in
   the say-gate section). The deprecated-`RoomInputOptions` shim warning
   fired on the same join path; the shim is now swapped for 1.6.6-native
   `RoomOptions`/`AudioInputOptions` as hygiene regardless of NC's fate.

**Return path (NC-BENCH-001 Task 1) — NC does not get another production
attempt on belief.** Isolated test slot, `LILY_NOISE_CANCELLATION=nc`,
prod pins (`livekit-agents==1.6.6`, `noise-cancellation==0.2.6`), ≥10
cold joins via `eval/nc_bench/` (see its README for the runbook). Pass
criteria, explicit: **10/10 job accepts · greet reaches playout on every
join (LILY transcript row present) · mic frames reach Speechmatics ·
join-to-ready within 2× the NC-off baseline.** Pass → re-enable in
production behind `tests/test_wedge_recovery.py`'s join-path regression,
one session watched live. Fail → NC stays off and the plugin is
convicted: file the upstream issue with the wedge evidence; a newer
plugin release claiming the fix re-benches first, never straight to
prod.

**Open empirical question (Task 4 rider):** whether NC is needed at all.
The Aug 5 baseline session ran NC-off, and every improvement since was
built against an NC-off world. The next live session's numbers
(dropped-answer rate, phantom-label count, attribution accuracy,
segment-span sanity — `eval/nc_bench/baseline_rider.sql`) get compared
against the Aug 5 audited numbers; if the non-NC stack carries the room,
NC's return becomes optional rather than pending. Record the verdict
here.

**Permanent, independent of the bench outcome:** BVC is prohibited in
shared-mic mode — in a one-mic multiplayer room the "background voices"
are the other players; `lily_config` coerces every unknown value
(including `bvc`) to `off`, and `lily_noise_cancellation_options()` can
only ever construct the ambient `NC()` model
(`tests/test_interruption_layer.py::test_bvc_is_unreachable`).

## STT tuning — echo-room study close-out (WO-LILY-OMNIBUS-003 WS-13)

Evidence session `lily-81BCB0-583a0f16` (2026-08-05, 4 players, reverberant
room) ran on effective Speechmatics defaults and produced 3 phantom labels
(S5–S7), a label-continuity split (Chris S1→S4), and two corrupted spans
(104.1s / 206.0s under S2). The record is frozen as
`tests/fixtures/echo_room_81BCB0.json` (RECORD-DERIVED — no session audio
was recorded anywhere: no egress in lily, `call-audio` bucket empty; the
fixture is the persisted transcript record, and acoustic replay of matrix
cells is impossible until a recorded-audio fixture exists — WS-15's
bake-off owns that leg). Baseline machine metrics (AMENDMENT-002 standard —
WER/DER + phantom/attribution/span scoring, `lily_stt_tuning` scorers):
phantom_label_count=3, label_continuity_splits=1, attribution_accuracy=0.91,
span_violations=2 at the 30s threshold.

**Chosen config** ships as a loadable artifact — `stt_tuned.json` (mirrors
`lily_stt_tuning.LILY_STT_TUNED`, drift-tested) — and is the incumbent arm
of the WS-15 diarization bake-off. Matrix axes for the acoustic sweep:
`speaker_sensitivity` [0.3, 0.4, 0.5] × `max_speakers` [5, 6, 7] ×
`volume_threshold` [0.0, 1.6, 3.2] (27 cells, `lily_matrix_cells()`).

### Config audit: every plugin kwarg at livekit-plugins-speechmatics 1.6.6

| Kwarg | Session value | Chosen | Rationale / installed-surface fact |
|---|---|---|---|
| `language` | `en` | keep | product language |
| `output_locale` | unset | keep unset | no locale need; available |
| `domain` | unset | keep unset | no domain pack applies to trivia |
| `operating_point` | ENHANCED | keep | accuracy over latency; only model-selection path at this pin (no `model=` kwarg) |
| `turn_detection_mode` | FIXED | keep | `end_of_utterance_mode` kwarg is REMOVED at 1.6.6 (deprecated shim warns); modes are now presets — FIXED/ADAPTIVE/SMART_TURN/EXTERNAL. ADAPTIVE + SMART_TURN require the `speechmatics-voice[smart]` extra (not installed) and flip to client-side forced-EOU; EXTERNAL would double-drive finalize with the session Silero VAD. Room-profile sensor recommends SMART_TURN for high-RT60 rooms — adoption is a WS-8 stream-swap decision |
| `include_partials` | True | keep | partials feed binding/continuous recognition (`enable_partials` is the deprecated alias, migrated by the plugin shim) |
| `enable_diarization` | True | keep | multiplayer core |
| `max_delay` | 1.5 | keep | validator bounds [0.7, 4.0]; word-emission latency is not the corruption mechanism |
| `end_of_utterance_silence_trigger` | 0.8 | keep 0.8; room profile maps high-RT60 → 1.0 | validator bounds (0, 2). FIXED preset default is 0.5; 0.8 tolerates table deliberation. Server-side trigger — see ceiling finding below |
| `end_of_utterance_max_delay` | unset | keep unset — **inert in FIXED mode** | the clamp lives in `_calculate_finalize_delay`, which returns early through `_calculate_fixed_finalize_delay` for FIXED; the RT API's `conversation_config` has no such field either. The 206s span happened WITH a nominal 10s default ceiling because that ceiling never applies in FIXED mode: finalization waits on the SERVER's silence-triggered EndOfUtterance, and 4-player cross-talk + reverb tails starve 0.8s of global silence indefinitely. No config value caps span length at this pin → span sanity is WS-10 quarantine (threshold below) |
| `additional_vocab` | `["Lily"]` | + player names at construction | constructor-only (StartRecognition; `update_speakers()` carries focus/ignore/focus_mode ONLY) so "names at bind" is impossible without a full STT swap; known-voiceprint names ARE available pre-construction and ride along. NEVER answer nouns — expectation-primed matching (n-best × `acceptable_answers`) is the generalizing mechanism |
| `punctuation_overrides` | unset | keep unset | no failure mode implicates punctuation |
| `speaker_sensitivity` | 0.5 (default) | **0.35** | validator bounds (0, 1). Higher mints more unique speakers; reverb reflections minted phantoms at 0.5. With enrolled speakers present, lower sensitivity biases toward matching enrolled voices over minting generic ones. 0.35 = matrix-center of the 0.3–0.5 band, pending WS-15 acoustic sweep |
| `max_speakers` | 7 at construction; roster-aware post-bind (HOTFIX-005 X8) | 7 pre-bind, then `roster + 1` (clamp [2,7]) at game start | validator bounds 2–100. S7-dumping-ground = the generic 7-cap exhausting (enrolled speakers do NOT consume the generic budget). Table size is unknowable pre-bind, so construction stays 7; at game start the roster-aware cap (`lily_max_speakers_for`: bound+1, clamp [2,7]) is applied via an `Agent.update_options(stt=...)` swap (solo ⇒ 2, killing the phantom [S2]). Wired behind `LILY_STT_ROSTER_RETUNE` (**DEFAULT OFF** — a live STT reconnect, STT-001 Q4's to validate); shrink-only |
| `min_endpointing_delay` (session, not STT) | 0.5 (framework default) | **0.6** (HOTFIX-005 X9) | the runtime's own remedy for the split-utterance class ("consider raising min_delay in the endpointing options to accommodate a slow stt"): the 0.5 floor commits the turn before the enhanced point's 1.5s `max_delay` delivers the final transcript. `LILY_STT_MIN_ENDPOINTING_DELAY`; ceiling (`max_endpointing_delay`) pinned to the 6.0 framework default. Set WITH `max_delay`, not against it |
| `prefer_current_speaker` | True | keep | echo copies arriving close behind the true speaker get folded into the current speaker instead of minting a phantom |
| `speaker_active_format` | `[{speaker_id}] {text}` | keep | attribution rides the transcript |
| `speaker_passive_format` | unset | keep unset | passive frames unused |
| `focus_speakers` / `focus_mode` | unset / RETAIN | keep | focusing would drop unbound guests |
| `ignore_speakers` | `["__ASSISTANT__"]` | keep (dormant backstop — see playback-path verdict) | plugin excludes dunder-wrapped labels by default; entry kept explicit |
| `known_speakers` | voiceprint rows | keep + dunder filter | `lily_filter_enrollable_speakers` drops dunder labels on the way in; enrollment write refuses them on the way out |
| `vad` | unset | keep unset | FIXED mode: server endpoints; session Silero VAD handles barge-in only |
| `sample_rate` / `audio_encoding` | 16000 / PCM_S16LE | keep | plugin defaults |
| — `transcription_config=` | — | — | dead at 1.6.6 (deprecated, ignored); `advanced_engine_control` survives serialization ONLY for declared TranscriptionConfig fields |
| — wire injection | none | `get_speakers=true`, `audio_filtering_config.volume_threshold=0.0` | see below |

**Wire injection (`lily_stt_tuning.lily_install_stt_tuning_patch`)** — the
StartRecognition wire dict is the only injection point that survives
`TranscriptionConfig.to_dict()` (same finding as lily_nbest). Both fields
live-validated against the voice endpoint 2026-08-05 (RecognitionStarted, no
1003-close; unsolicited `SpeakersResult` observed before `EndOfTranscript`)
— the `max_alternatives` incident class does not apply:

- `speaker_diarization_config.get_speakers=true` — server pushes speaker
  identifiers at end of transcript; captured into a teardown-surviving store
  (`lily_captured_speakers`) that `lily_enroll_voiceprints` uses as fallback.
  This closes the 2026-07-15 session-close enrollment hole (dead websocket →
  GET_SPEAKERS unusable) and retires the ~5-word polling as the ONLY source.
- `audio_filtering_config.volume_threshold` — the SDK already hardcodes
  `0.0` onto every wire, so overriding the VALUE is schema-safe. Live value
  stays **0.0**: with no recoverable session audio there is no calibration
  ground truth, and an uncalibrated pre-ASR floor risks dropping quiet REAL
  speech (the session already lost answers to client-side mic-ducking).
  The lever is wired for WS-15's sweep; per-word `volume` labels ride
  AddTranscript results for WS-8's ghost-fold heuristic (echo copies run
  quieter).

### Playback-path verdict (item 1 — verification, not repair)

Record-corroborated clean: zero assistant-speech runs (≥8-word verbatim,
`lily_assistant_leak_scan`) in any user-attributed row of the evidence
session. The protecting mechanism, in order: (1) STRUCTURAL — the agent
subscribes to remote participant tracks only; its own TTS is published, not
fed back to its STT input; (2) DEVICE — client-side AEC removes Lily's
playback from the players' shared mic (policy below keeps it on); (3) the
`ignore_speakers=["__ASSISTANT__"]` entry is a DORMANT backstop: no
voiceprint row carries that label (verified in `lily_speaker_voiceprints`),
so the engine can never assign it — it carries no real identifiers and
filters nothing today. Regression guards: the leak scan runs in tests
against the fixture; the enrollment path now refuses dunder labels in both
directions, so the backstop can never become an active matcher for a real
voice.

### Enrollment (item 2, joint with WS-8)

`get_speakers=true` auto-retrieval + `known_speakers` at StartRecognition is
the enrollment loop. Identifiers are minted by the server from LIVE session
audio — i.e., in-room reverberant audio — so the AMENDMENT-002 clean/reverb
mismatch is avoided BY CONSTRUCTION for players enrolled in-room (there is
no clean-audio enrollment path in this stack). Cross-room reuse (enrolled in
a quiet room, returning in an echo room) is the residual mismatch case;
lowered `speaker_sensitivity` (0.35) is the compensating choice, and the
WS-15 fixture should include one cross-room arm. WS-8 interfaces:
`lily_stt_tuning.lily_captured_speakers()` (teardown-surviving identifiers),
`lily_max_speakers_for(roster)` (post-bind swap cap), per-word `volume`
labels (ghost-fold), `Agent.update_options(stt=...)` (the swap lever — 
recommended, deliberately unwired here).

### WS-10 handoff — span quarantine

`ws10_span_quarantine_seconds = 30.0` (artifact key): longest legitimate
player span in evidence is 20.1s; corrupted spans were 104.1s/206.0s. Since
no config lever caps FIXED-mode span length at this pin, spans past the
threshold are a quarantine concern, not a tuning concern.

### Room-profile sensor (item 6 — AMENDMENT-002)

`lily_room_profile.py`: blind RT60 (decay-run slope fitting) + DRR proxy
(peak/shadow energy ratio) on the FIRST audeering capture window — same
bytes the upload path already holds; capture coverage untouched; devAIce
`audio_quality` stays a coarse quality gate only (it measures SNR/clipping,
not reverberation physics). Estimates on speech carry real variance, so the
mapping uses coarse bands (`RT60 ≥ 0.6s` reverberant, `DRR < 2dB` low):
reverberant → `end_of_utterance_silence_trigger` 1.0 + SMART_TURN
recommendation; low-DRR → Tier-1 threshold delta −0.05 + a positively-framed
state-block line (`[room profile: …listen generously…]`) via the acoustic
state's `set_room_profile`. Advisory: estimator failure never touches the
session.

### Client capture policy (AMENDMENT-002, program-wide)

- Never force `echoCancellation=false` on Android; platform APM defaults
  stay ON, per-platform behavior documented as encountered.
- iOS wrapper pre-warms the audio context on a user interaction before
  session start.
- Client-side Krisp: always BVC-OFF in shared-mic mode.
- Browser AEC needs 2–5s convergence — the session-start greeting/intro
  choreography deliberately provides it; keep that choreography.
- Double-talk mic-ducking means some of the evidence session's answer loss
  was likely client-side, before any wire — server-side tuning cannot
  recover it, which is one more reason `volume_threshold` stays 0.0 until
  calibrated.

## Bank curation loop (WO-LILY-OMNIBUS-002 D/E/F)

The curated bank (`lily_questions`) is a living asset: it grows from
generation, forgets nothing per group, tunes its own difficulty labels, and
graduates player-demanded categories — all observable via `LILY_BANK |` and
`LILY_TUNE |` log markers.

### Banking-on-generation + dedup (D, `lily_bank.py`)

Every generated question that passes verification is inserted into
`lily_questions` (`source='generated'`, `status='active'`, `adult` set from
session mode) — this is the path by which the bank self-grows. Near-dup
detection runs at EVERY bank insert:

- **exact**: identical normalized-text hash (lowercase,
  punctuation/whitespace-stripped, sha1) against any existing row, any
  category;
- **fuzzy**: `difflib` ratio >= 0.87 on normalized texts, same category only.

Dups are discarded and logged `LILY_BANK | DUP_DISCARDED`, never inserted.

Bank reads never load the full self-growing table: PostgREST applies
`status`, consent mode, category, and difficulty filters server-side, caps
each fallback stage at 100 rows, and randomly chooses from the surviving
history-safe candidates. Migration 016 supplies the matching composite index.

### Per-group asked history (D, migration 010)

`lily_asked_history` gets one row per question SERVED (armed for delivery):
resolved `group_id`, `question_id`, normalized-text hash, `session_id`.
Loaded at session start (and reloaded on a group-id upgrade; a rekey moves
this session's rows), it drives the no-repeat guard:

- bank draws exclude the group's served `kb_` ids and text hashes
  (`lily_fetch_bank_question` `exclude_ids`/`exclude_hashes`);
- generated output is hash-checked against the history — a cross-session
  repeat is discarded (`LILY_BANK | HISTORY_REPEAT_DISCARDED`) and the bank
  fallback serves instead (the generator's textual avoid-list only carries
  this session's prompts);
- **draw idempotency (WO-LILY-DESYNC-HONESTY-001 G2):** every question a
  prefetch DRAWS also registers in a session-scoped set the moment it
  lands — not at serving/arm, which left a window where a second draw ran
  before the first serving registered (the live `q_0492` double-prefetch).
  The drawn set rides the same exclusion lists, and a duplicate that slips
  through any supply source is discarded at a final gate
  (`LILY_PREFETCH | DUPLICATE_DRAW_DISCARDED`). A drawn-then-discarded
  question stays excluded for the session.

### Difficulty self-tuning + retirement (E, `lily_bank_tuning.py`)

Session-end job, fire-and-forget (never blocks or fails the shutdown gate).
Aggregates `lily_answers` per bank question across ALL sessions, with an
exposure floor of **5 servings** counted from `lily_asked_history`. Per
question, ONE move per run:

- success > 75% → `difficulty_tier` down one (min 1)
- success < 30% → `difficulty_tier` up one (max 4)
- success < 10% or > 95% → `status='retired'` (outranks a tier move; rides
  the shared migration-009 status column, so retired rows are unservable)

Decisions are pure functions (offline-testable decision table); applied
moves log `LILY_TUNE | TIER_DOWN/TIER_UP/RETIRE | ...`.

### Gated category proposals (F, migration 011)

Generation may return `proposed_category` (reserved field in the question
schema). Each proposal upserts `lily_category_candidates` (use_count +
distinct proposing groups), but the question SERVES under its round FAMILY
until the candidate is **promoted: use_count >= 10 AND >= 3 distinct
groups**. Promoted extras appear as one lobby state-block line; Lily never
announces unpromoted categories.

## The right to be forgotten (WO-LILY-FORGETME-001)

**Principle stamp: recognition data is held FOR the players, at their
pleasure — the explanation is honest and plain when pressed; deletion is
immediate, complete, verified, and costs only the recognition itself.**
Known granularity limit: identity is group-keyed, so deletion is
all-or-nothing per table of players — per-player deletion is v2. Lily
remembers tables, not individuals.

**Transparency (`lily_explain_memory`, read-only, ungated):** counts only,
never raw contents — voiceprints (voices remembered), `lily_memories` rows
(games, plus last `played_at`), facts kept, and how recognition happened
THIS session (plain-language mapping of `resolve_group_identity`'s logged
source chain: device metadata / voiceprint match / name-set match / fresh /
post-forget anonymous — never naming vendors, tables, or mechanisms beyond
device-and-voice). A cold group gets the honest "nothing yet" shape; a
failed read gets an honest can't-check shape, never guessed counts. No new
storage anywhere in the path.

**The prompt contract** (`## MEMORY, HONESTY, AND FORGETTING` in
`lily_system.txt`, say-gate compatible): the first "how did you know?" may
keep the mystery ONCE; pressed, serious, or any discomfort → plain honesty
in one beat ("your device and your voice — I'm built to remember my
regulars") with the standing offer in the SAME breath ("say 'Lily, forget
me' and it's all gone"). Never argue for being remembered, never re-raise
after a no, never ask twice. Post-deletion: one warm line, zero mourning,
the game continues.

**The spoken flow is deterministic, not prompt-whim:** the scorekeeper
command layer detects the request (`lily_detect_control_command` →
`forget_me`) — paraphrase-tolerant ("forget me/us", "delete what you know
about us", "erase my data", ...), negation-guarded ("don't forget us" never
fires), fragment-proof (same 2s join as "back to normal"). Detection only
ARMS a pending-confirm state and dispatches ONE plain confirmation naming
the scope ("everything — voices, games, facts — gone for good, tonight's
game keeps going"); the requester's next parseable yes/no is resolved in
code (`lily_forget.lily_parse_forget_confirmation`, same pattern as the
clarify resolution — ambiguity does nothing destructive). A yes runs the
cascade; a no drops it for the night (only a fresh player-initiated request
re-arms).

**Vendor-side voiceprint truth (OR amendment R4, verified against the
installed SDK 2026-07-15):** Speechmatics speaker identifiers are
client-held opaque strings — the SDK docs describe them as "any stable
identifiers relevant to your application (for example device IDs, prior
session speaker IDs)"; we mint nothing vendor-side and re-inject them
each session as `known_speakers` hints. The installed
`speechmatics.rt`/`livekit-plugins-speechmatics` surface exposes **no
delete/deregister API** for identifiers. What the forget cascade
provably deletes is OUR copy (`lily_speaker_voiceprints`), which removes
Lily's ability to ever re-identify the voice; whether Speechmatics
retains derived data account-side is a vendor-policy question no code
path here can answer. Consequences: (a) the spoken scope line is
deliberately phrased as "everything I keep — your voices as I know
them" — Lily speaks for her own memory, never for other systems; (b)
run-sheet note: if a vendor-side deletion request is ever needed,
snapshot the `lily_speaker_voiceprints` identifiers BEFORE running the
forget arc — post-cascade they are untargetable.

**Device identity quarantine (OR amendment W2):** `lily_group_id` metadata
identifies a browser/device candidate, never the humans currently present.
The live session remains keyed to its room-random provisional id; candidate
memories, preferences, names, counts, dates, facts, and asked history are
quarantined from vocal context. Stored voiceprints may enter Speechmatics as
matching hints, but no label can surface until a current person speaks. The
opening may say only "this device looks familiar — who's playing tonight?"
A live Speechmatics identifier overlap promotes the candidate through
`source=voiceprint_match`, re-keys the session, and releases returning-table
memory. A current identifier set with no overlap rejects the candidate and
keeps a new-table path. Until verification, `lily_explain_memory` discloses
no counts or dates; a forget request still targets and deletes the staged
device identity.

**The cascade (`lily_forget_group(confirm)`, two-step, UNGATED):** per the
tool-gating principle above, deletion neither mutates game outcomes nor
emits game events, so the tool carries NO `game_started` gate — the right
works from the lobby onward. `confirm=false` is refused (and arms the
deterministic yes/no parse); `confirm=true` is accepted only after that
pending request recorded a spoken yes from the requester. The verified path runs
`lily_persistence.lily_forget_group_data`: HARD-DELETE of all group rows in
`lily_speaker_voiceprints`, `lily_memories`, `lily_group_facts`, plus the
session-keyed `lily_transcripts`, `lily_answers`, `lily_addressee_log`,
`lily_acoustic_trajectories`, `lily_session_reports`, and
`lily_image_attempts` via the group's session ids (from `lily_sessions`, its
deterministic tombstone for retry recovery, plus the current session), plus
`lily_asked_history` and `lily_group_prefs` IF the tables exist (the former
lands with a future WO, the latter only skips on migration-013 lag — a
forgotten table's preferences are recognition data; absent-table errors are
skipped and logged, never failed). `lily_sessions`
is RETAINED but re-keyed to the tombstone `forgotten_<sha1-12 of the old
group id>` only after every required delete verifies — operational records
survive without linked content, and a partial failure keeps the original key
discoverable for retry. Deletion is awaited, count-VERIFIED
per table (zero rows under the old key), capped at ~20s, and reports
honestly on partial failure — the tool result names which tables succeeded
and tells Lily what to say; a partial failure stays retryable against the
ORIGINAL id. Logs `LILY_FORGET | group=... | tables=... | rows=...` with
per-table counts.

**In-session teardown:** STT `_stt_options.known_speakers` is cleared
(1.6.6 limitation, documented in code: the Speechmatics plugin has no live
de-enrollment API — `update_speakers()` only takes focus/ignore/focus_mode
and `known_speakers` ride the one-shot StartRecognition message, so the
clear guarantees any STT reconnect starts unenrolled while the open stream
keeps its labels until it closes); the game re-binds to a FRESH ANONYMOUS
group id (`anon_<random>`, source `post_forget_anonymous`) and the current
game continues normally. Pending/future transcript writes are disabled,
remembered player names and the rolling transcript are cleared, and identity
reports, memories, facts, preferences, and voiceprints stay off for the rest
of the session. The device metadata id is dead (the frontend cleared it) and
`resolve_group_identity` / `upgrade_group_id` are suppressed for the rest
of the session so the name-set hash can never silently rebuild the deleted
group; `memory_block` is cleared and `_apply_context_blocks` now REMOVES
the stale `[RETURNING TABLE]` item (symmetric with the adult layer), so
injection stops immediately. The `memory_forgotten` packet (both
discriminators spelled `memory_forgotten`) is emitted on `lily.events` only
AFTER the cascade verified — the frontend clears the device localStorage
group id and shows a transient confirmation.

**Lobby disclosure (frequency-capped):** on a RETURNING-group greet only,
one natural clause folds disclosure into the charm ("...I remember my
regulars — any time you want me to forget, just say so") — on the first
rematch, then every 5th stored game. The persistent counter is the
`lily_memories` row count itself (`total_games`) — one row lands per played
session, so it survives sessions at zero new columns: **no migration 010
needed**. Cold groups disclose nothing; the clause is latched to at most
once per session.

**Dynamic session greeting (principal addendum + correction, rides the
same WO):** the landing is COMPOSED from ordered parts, never one rigid
line. Part one, ALWAYS — every session, returning table or not — a very
quick one-breath self-intro ("Hi, I'm Lily —"); the observed live failure
("welcome back everyone" with no intro) is exactly what this pins. Part
two, the recognition nuance composed per-player from the memory/roster
data: whole table returning → "...welcome back, all of you"; MIXED table →
returners greeted BY NAME, newcomers separately ("...welcome back, Rami —
and hello to the new faces"); all new → plain warm welcome. The
first-time-vs-returning question is asked ONLY when memory gives no
answer — when memory KNOWS, she acts on it: returners get the one-time
refresher offer ("want a refresher on the options, or straight in?"),
first-timers (and the new faces at a mixed table, short version) get the
natural walkthrough. Both draw exclusively on the prompt's
**`## WHAT THE TABLE CAN ASK FOR`** block — the single options inventory
(freeform play, multiple-choice on request, the grown-up deck, skip, the
steal window, the 50/50 lifeline, "back to normal", "Lily, forget me",
timed-vs-relaxed pacing) that future WOs extend one line each (the queued
multiple-choice round format and picture rounds land there). The walkthrough/refresher happens
at most once per session, and the Task-4 disclosure clause lands inside
the same welcome-back beat (one natural breath; the `session_greet`
say-gate key covers the whole landing).

Tests: `tests/test_forget.py` (tombstone, cascade plan + executor against a
fake postgrest client, yes/no parser, explain-memory shapes, disclosure
cap), `tests/test_forget_flow.py` (spoken flow end-to-end, tool two-step,
teardown, suppression, greeting interlock), and the `forget_me` detection
suite in `tests/test_commands.py`.

## Acoustic pipeline (Audeering devAIce)

Lily reads the ROOM, not the transcript: rolling ≥5s windows of room audio
are uploaded to the audEERING devAIce Web API (v4.9.0) and consumed as
natural-language hosting signals plus one safety-critical action surface.
Native lift of the JRVS canary (`mjrvs_audeering_client.py` /
`mjrvs_audeering_consumers.py`, WO-JRVS-AUDEERING-MODULES-D-001) — Lily
files are `lily_audeering_client.py` (HTTP client + capture pipeline) and
`lily_audeering_consumers.py` (gates, banding, ladder, rubric, state store).

**Modules requested** (exact upload config; billing is audio-seconds — JRVS
Probe-C confirmed per-second, not per-module, so this full set is 1× quota):

```json
{"expression": {"expressionModel": "large"}, "prosody": {}, "audioQuality": {},
 "aed": {}, "scene": {"outputSubScene": true},
 "speakerAttributes": {"speakerAttributesModel": "large"}}
```

`asr` and `speakerVerification` are EXCLUDED. `speakerAttributesModel`
"large" is MANDATORY (the small model returns null child scores on short
segments and would starve the safety ladder). The capture window is floored
at 5s in `lily_config` — the scene model is optimized for >5s windows, one
classification per window (no continuous sub-windowing).

**Capture wiring (the Tijoux lesson):** the `track_subscribed` handler is
registered AFTER `session.start()` returns (handlers registered earlier fire
into a half-initialized session), and a safety-net scan attaches
already-subscribed tracks that landed during the start window. Multi-mic
tables attach every non-agent audio track.

**D-cross rules (JRVS, carried verbatim):**

1. **Zero scalars in the prompt.** The LLM never sees `arousal=0.71` — only
   `[room read: agitated / on edge]`. The rubric
   (`prompts/lily_room_read_rubric.txt`, appended to `lily_system.txt` at
   loader level) is a PHRASE LIST ONLY, and an import-time lint in
   `lily_audeering_consumers` raises on ANY digit in it — a scalar in the
   rubric fails the container at boot.
2. **Quality gate first.** `audioQuality.snr < AUDEERING_MIN_SNR_DB`
   (default 12) suppresses all affect lines for that window. Doc caveat
   encoded in the gate: the SNR model is strong on background noise
   (CCC 0.94) and weak on speech-distortion (CCC 0.38) — it is a
   background-noise gate only; do not tighten it chasing distortion.
   Transit scenes loosen the bar by `AUDEERING_SNR_TRANSIT_ADJUST`
   (default −2; the JRVS D2b scene→gate pattern).
3. **AED gate:** music co-active with speech suppresses affect reads
   (party rooms — singing must never read as an emotional event). AED
   thresholds ride the doc defaults (applied server-side).
4. **Baseline-relative reads / smoothing on descriptors ONLY.** The
   room-level AVD (`expression.dimension`, −1..1; `category` scores are NOT
   consumed) is smoothed over `AUDEERING_AVD_SMOOTH_WINDOW` (default 4)
   bounded deques, banded into descriptors, injected as
   `[room read: ...]` lines in the `[GAME STATE]` block. All axes inside
   `AUDEERING_AVD_NEUTRAL_BAND` → inject NOTHING.
5. **Safety triggers run OUTSIDE the smoother.** The child ladder keeps its
   own per-segment streak counters — widening the smooth window cannot
   delay it (regression-pinned in `tests/test_audeering.py`).
6. **Never block a turn.** Uploads are fire-and-forget; the agent reads the
   latest state synchronously in `build_state_block`. Accepted asynchronous
   uploads (`202`) are polled with bounded backoff instead of being dropped
   after the first pending response.
7. **Consumer exceptions never stop raw-signal recording.**
8. **Circuit breaker on missing `AUDEERING_API_KEY`** — best-effort: one
   structured `LILY_AUDEERING_BREAKER` log line, uploads disabled, the
   session runs unaffected.

**Room read → hosting moves** (rubric mapping): "flat / low energy" →
easier question + spotlight move; "hot / riding high" → tighten and ride
it; "valence sagging" (on a hard-question streak) → drop a gimme;
"agitated / on edge" → cool it down. Scene sub-labels map to hosting
calibration: small indoor → living-room intimacy, large indoor → bar
energy, transport → shorter questions + more repetition tolerance — as a
low-priority `[env: ...]` line, at most once per state-block refresh.

**Child-signal ladder → adult-mode veto (SAFETY-CRITICAL; ships with the
pipeline, never after).** Signal: `speakerAttributes.gender.child`
(schema `{gender: {female, male, child}` summing to 1, `age: number|null}`),
one result per VAD speech segment — a young voice produces its own
segment-level scores, and the sustained-N streak counts segments.
Tiers: HIGH ≥ `AUDEERING_CHILD_HALT_THRESHOLD_HIGH` (0.85) sustained N=2;
BORDERLINE ≥ `AUDEERING_CHILD_HALT_THRESHOLD_BORDERLINE` (0.5) sustained
N=2. HIGH segments also advance the borderline streak (JRVS ladder fix —
oscillation around the high threshold cannot evade both tiers).
**Null-safe:** a null child score (too-short segment) neither advances NOR
resets a streak. The ladder runs BEFORE the quality/music gates — a child
on noisy audio or a child singing is never a blind spot. Enable flags
default ON for Lily (`AUDEERING_CHILD_HALT_ENABLED`,
`AUDEERING_CHILD_STEP_UP_ENABLED`; JRVS shipped them false pending an
action surface — Lily has one).

Actions (veto-only, BOTH tiers):

- **Adult mode ACTIVE + ladder trips** → exit through the SAME sticky-flag
  path as the spoken "back to normal": `sk.set_mode("general")` +
  attribute publish + deterministic revert flow (`LilyGame.on_child_signal`,
  wired as the acoustic state's `on_child_signal` callback). Instant,
  in-character line WITHOUT explaining the mechanism, general category next.
- **Adult mode REQUESTED while tripped** → the `lily_enter_adult_mode` tool
  checks `acoustic.child_veto_active()` and refuses, telling Lily to keep
  the general deck with a light in-character deflection, unless
  server-authenticated architect mode is active.

**Adult entry policy:** audEERING is optional veto telemetry, never an
authorization service. `lily_enter_adult_mode` requires Lily to ask every
player to explicitly confirm aloud that they are 18 or older and want the
grown-up deck. The deterministic spoken latch is authoritative: explicit
18+/above-18 wording, an unambiguously adult first-person age, or an
unambiguously adult birth year consumes the gate for the session. The model's
`confirmed_all_18_plus` argument cannot authorize by itself and cannot force a
second ceremony after the latch is true. State carries
`adult_consent: CONFIRMED — do NOT ask again`.

- Missing `AUDEERING_API_KEY`, failed preflight, quota exhaustion, or a
  breaker opening never blocks entry and never exits an active adult game.
  Monitoring loss logs
  `LILY_ADULT_GATE | MONITORING_UNAVAILABLE | adult_mode_continues=true`.
- An actual sustained young-voice signal may still block entry or exit an
  active adult game through `LilyGame.on_child_signal`.
- `LILY_ARCHITECT_MODE=1` is a deployment-authenticated testing override:
  it bypasses verbal confirmation only; it never overrides an active
  young-voice veto. Merely saying "I'm the architect" never activates it. Every use logs
  `LILY_ADULT_GATE | ARCHITECT_OVERRIDE`.

Framing, stamped doc-verbatim at every emit site
(`lily_audeering_consumers.PERCEIVED_FRAMING`): the module estimates "how
the speaker sounds, not necessarily the actual attributes of the speaker";
age MAE is ±8.46yr — the signal can EXIT or BLOCK adult mode, NEVER
authorize it. Explicit whole-table 18+ verbal confirmation authorizes entry.
Age/gender are otherwise telemetry-only, stamped
`PERCEIVED_NOT_VERIFIED`, never spoken, never in the prompt.

**Persistence (migration 008):** one `lily_acoustic_trajectories` row per
finalized user turn (fire-and-forget, `to_thread`) carrying the latest
capture's `category` / `dimension` / `prosody` / `features` jsonb (index on
`session_id, turn_index`; drt_acoustic_trajectories clone). Every
`lily_addressee_log` row now also carries `acoustic_snapshot` — non-null
jsonb when the pipeline is healthy, an EXPLICIT SQL null (key always set,
never absent) when the breaker is open.

**Env** (all via `lily_config`; also threaded through
`.github/workflows/deploy.yml` — key from secrets, tunables from vars):
`AUDEERING_API_KEY`, `AUDEERING_MAX_UPLOADS_PER_SESSION` (default 240 —
20 min billable ceiling), `AUDEERING_WINDOW_SECONDS_F` (default 5, floored
at 5), `AUDEERING_CAPTURE_INTERVAL_SECONDS` (5),
`AUDEERING_MIN_SNR_DB` (12), `AUDEERING_SNR_TRANSIT_ADJUST` (−2),
`AUDEERING_AVD_SMOOTH_WINDOW` (4), `AUDEERING_AVD_NEUTRAL_BAND` (0.15),
`AUDEERING_CHILD_HALT_THRESHOLD_HIGH` (0.85),
`AUDEERING_CHILD_HALT_THRESHOLD_BORDERLINE` (0.5),
`AUDEERING_CHILD_HALT_SUSTAINED_N` (2), `AUDEERING_CHILD_HALT_ENABLED`
(true), `AUDEERING_CHILD_STEP_UP_ENABLED` (true). Testing override:
`LILY_ARCHITECT_MODE` (default false; server/deployment configuration only).

Out of scope (deliberate): per-speaker AVD attribution, the `asr` module,
speaker verification, Hume, any gender-conditional behavior.

## Frontend seam (prmpt_ui `(lily)` route group)

- **Participant attributes** (LWW, per question beat): `phase`
  (`lobby|question|answering|reveal|scores|final`), `round`, `question_number`,
  `mode` (`general|adult`, sticky), `media_mode` (`voice_only|pictures`, sticky
  lobby choice), `players` (JSON `[{name,score,streak,leader}]`),
  `answer_window` (JSON `{open,duration_ms,opened_at}` + optional
  `steal: true` while the open window is a 5-second steal window),
  `last_active_at` (epoch-seconds heartbeat), `pacing` (`timed|relaxed`,
  sticky group pref).
- **Room metadata**: `{question, reveal:{answer,winner,correct}, wager,
  image_url}` via `ctx.api.room.update_room_metadata` (rtc has no
  room-metadata setter); `wager` drives the frontend's final-round palette
  shift; `image_url` (additive, optional — empty when not a picture question)
  is published alongside the question text at WINDOW OPEN — the delivery
  turn's playout completion, so the glass never leads the voice (the
  published `phase` likewise holds on `lobby` from first-question arm
  until that playout). Seam addition
  (multiple-choice WO): when the armed question is multiple choice the
  document also carries `choices` (array of exactly 4 strings) and
  `eliminated` (array of 0-based indices into `choices` crossed out by a
  50/50, `[]` until one fires); both keys are absent for freeform
  questions. `category` (optional, absent when unknown) names the
  question's category family for the frontend's eyebrow line.
- **`lily.events`** reliable packets (discriminator key `type`, matched to the
  shipped prmpt_ui parser — kind-name drift from the original contract note is
  deliberate): `player_bind` `{player:{name},name,speaker_label}`; `reveal`
  `{correct,winner}` keyed to TTS playback; `best_wrong_answer`
  `{player,answer}`; `biggest_comeback` `{player,detail}`; `finale`
  `{standings}` — fired at or before the `phase=final` flip; `clarify`
  `{name}` when Lily asks the "answer or thinking out loud?" question
  (2026-07-14 amendment — carries `clarify` on both discriminators);
  `lock` `{name}` the moment a player's answer candidate is recorded
  (name only — the answer text never rides the wire before the reveal).
- **RPCs registered**: `lily_control.start`, `lily_control.skip` (identical to the
  spoken "skip": no commentary, no spotlight — the adult-mode consent affordance).

## Picture rounds & web tools (WO-LILY-OMNIBUS-002 H/I/J/K)

### Media mode — the lobby choice

`media_mode` is a **sticky, deterministic flag** on the scorekeeper
(`voice_only` default | `pictures`). It flips in code — never by LLM whim —
via `LilyGame.try_activate_pictures` (dependency-checked: generation key +
Supabase pipeline must be up, or the honest unavailability line fires and
the flag stays `voice_only`).

**Ways it turns ON (any one is enough):**
- Spoken detector (`lily_detect_media_choice`): "pictures on" / "picture
  rounds" / "use the screen" / "pictures in mixed mode" / "images live" →
  `pictures`; "voice only" / "no pictures" / "pictures off" → `voice_only`
  (OFF wins a collision).
- Adult heat tool: successful `lily_set_adult_image_intensity` also flips
  pictures ON when the lane is healthy (heat alone used to leave the bank
  dark while `media_mode` stayed `voice_only`).
- Confirm after her offer: if she asks "want them on?" while still
  voice-only, a short "yes" / "live immediately" / "turn them on" flips
  the same path.

Published as the agent attribute `media_mode`.
**Picture questions are excluded entirely in voice_only** — the supply path
never calls a picture builder and strips any cached bank image before arming.
Once ON, prefetch draws the standing arsenal first (`lily_picture_arsenal`),
then live generation.

### Image storage (cache-first)

`lily_images.py` puts image bytes (or a fetched web image) into the public
Supabase Storage bucket **`lily-images`** at the content-addressed path
`{source}/{sha1}.{ext}` and returns the public URL. Uploads are idempotent —
an already-exists conflict is a cache hit. Migration 012 adds
`image_url` / `image_source` (`generated|web|none`) / `image_license_note` to
`lily_questions`: **the bank row is the cache** — any image need checks the
row's `image_url` BEFORE generating or fetching, and successful
sourcing/generation writes back so the next session cache-hits. Picture
questions carry `image_url` into the room-metadata payload at delivery time
(screen truth = spoken truth; the frontend already renders it).

### Real-image sourcing — Exa (real entities)

`lily_search.py` is a NATIVE lift of the `prmpt_common` Exa/Tavily modules
(do-not-import donor) on httpx. At prefetch, real-entity picture questions
("name this landmark", curated subject list) source ONE candidate image via
Exa under a **conservative, reject-on-doubt filter**: safelisted hosts only
(wikipedia/wikimedia/britannica/nasa/si/nps/loc), https direct image, every
significant entity token on the page, no bare-number entities. Provenance
lands in `image_license_note` (`web image via Exa: page=... image=...`).
Any failure is a **text-only fallback**; real entities are **NEVER
generated** — a plausible-but-wrong landmark is a lie on the screen.

### Image generation — JRVS clone (invented content only)

`lily_imagegen.py` is a native lift of the maya_jrvs image stack onto Lily's
Gemini infra (`LILY_IMAGEGEN_MODEL`, default `gemini-2.5-flash-image`):

- **background-task pattern** — generation runs at prefetch time only, never
  inside a live turn;
- **aspect-ratio clamp** (fleet WO) — off-list ratios clamp to the
  orientation-preserving nearest supported value before the wire (the donor
  crash class: an off-list value 400'd AFTER a successful render and the
  image vanished); garbage falls back to `auto`;
- **no-silent-crash / visible error rows** — the rule originated in this
  donor stack: EVERY generation/fetch attempt writes one
  `lily_image_attempts` row (migration 012); rejection/error rows carry the
  actual provider message in `failure_reason`.

`image_source='generated'` only. **Reference round: "real or imagined"**
(round 2 in pictures mode) — a generated plausible-fake photo alternates
with an Exa-sourced real photo; the table guesses; adjudication accepts
real/fake/imagined variants via the existing tier-1 matcher. Chosen over
"emoji story" because the donor is a photorealistic single-image pipeline —
exactly the plausible-fake generator the round needs — and it composes with
the real-photo sourcing above. In pictures mode the first question of every
other round is a real-entity picture slot; the wager round is always text.

### HARD GUARDRAIL — no web tool on the vocal path, ever

Exa and Tavily are bound to the **reasoning node ONLY** (question
verification + current-events sourcing at prefetch; `verify_question` gets a
bounded Tavily fact block, current-events categories get a fresh-facts
brief). A web round-trip on the vocal path is a multi-second stall in live
audio, and raw web text reaching the vocal LLM is an injection surface.
**Web results reach Lily only as bank rows or state-block facts** prepared
by the reasoning node. Enforced twice: an import tripwire in
`lily_search.py` raises if the vocal node (`lily_agent`) ever directly
imports it (the `lily_agent -> lily_reasoning -> lily_search` seam is the
one legal path), and `tests/test_web_guardrails.py` inspects the vocal
module for any web/image-stack reference. Missing `EXA_API_KEY` /
`TAVILY_API_KEY` simply disables the tool — text-only behavior, never a
boot failure.

## The standing picture arsenal (WO-LILY-PATCH-003 A/B/C; stocked by WO-LILY-ARSENAL-SEED-001)

A player says "picture trivia" and the first question airs **instantly** —
from a cold start, on the first session, in all three registers. That only
works if the bank is already stocked, which is the part that did not happen
between PATCH-003 P2 and 2026-08-07: the supply logic was specified and the
shelf was built, but nothing was ever put on it. An empty arsenal degrades
*silently* to live generation, which is exactly the dead air it exists to
prevent — the operator hit **67 seconds of silence** waiting for a picture
question.

Three register partitions, each tracking consumption independently:
`general` · `adult_suggestive` · `adult_explicit`.

### What an arsenal entry is (A1)

Not "a picture". A complete, self-contained picture question that can be
reconstructed and served from its stored fields alone — no re-derivation, no
regeneration. `lily_arsenal._row_to_question` is the round trip.

| field | what it carries |
| --- | --- |
| `question_text` / `canonical_answer` / `acceptable_answers` | the question and what counts as right |
| `options` | four phonetically distinct spoken options (multiple-choice formats), else null |
| `generation_prompt` / `generation_model` / `intensity` | how the image was made, at what heat |
| `image_storage_path` / `image_source` / `is_real_image` | the image and its provenance |
| `format` | its SHAPE (A2) — what lets a round mix deliberately |
| `binding_direction` | `image_first` or `question_first` (A1) |
| `subject_area` / `difficulty_tier` / `reveal_color` | the spread (A3) and the spoken reveal |
| `question_text_hash` | the exact-dup key |
| `status` / `gate_mode` / `classifier_verdict` / `reviewed_by` | the quality gate (A5) |
| `generation_cost_usd` / `generation_attempts` / `run_id` | cost and provenance (A10) |

**Binding direction changes the generation order**, and it is recorded per
entry because correspondence failures cluster by direction — a cluster you
cannot see if nobody wrote down which way each entry went:

- **image-first** — generate the image, then write the question about what
  is actually in it. Correspondence is nearly free; the image drives.
- **question-first** — write the question, then generate an image to
  complete it. Riskier: the image has to show what the stem *claims*, so
  this path verifies correspondence at the classifier before banking.

The picture contract holds either way: **the image is the question's own
image**, generated from the question's content, never decoration attached
afterward.

### Format taxonomy (A2) — `lily_arsenal_formats.py`

"Picture trivia" is not one shape. Every entry is format-tagged so a round
can mix shapes instead of serving one forever. Each format carries a spoken
template, an image-introduction line, its answer style and its binding
direction. Multiple-choice options are **spoken words, never bare letters** —
"A/B/C/D" is unusable in a noisy room, and options must be phonetically
distinct from one another.

`real_or_imagined` requires **both** real and generated images in the bank.
The real half is web-sourced through Exa, never generated, so the format is
excluded from the seeding plan unless `LILY_ARSENAL_REAL_IMAGES` /
`EXA_API_KEY` make it honestly buildable — half-building it is how Lily
ended up improvising a format live on 2026-08-07 that she could not explain
when asked.

### Partition content briefs (A3) — `lily_arsenal_content.py`

Per partition: a subject-area list broad enough that the bank does not feel
repetitive, a difficulty spread (not every question a gimme), and a house
visual style so the rail looks coherent rather than like a stock-photo grab
bag. `lily_plan_entries` walks the spread **deterministically** (index
arithmetic, no `random`, no time seed — fleet rule) and takes a
`start_index` so a top-up run continues the spread instead of re-clustering
on the same subjects.

Adult art direction is **not** duplicated here: the comic-book house look
lives in `lily_imagegen.LILY_ADULT_IMAGE_STYLE` and is applied at the wire.
The briefs carry composition and legibility direction only.

### Generation (A4) — `lily_arsenal_gen.py`

Every image goes through `lily_imagegen.lily_generate_image_bytes` with the
same `mode` and `intensity` live generation uses. **The arsenal must not
become a second, looser path to image generation** — it has no
image-provider code of its own, so it inherits the same provider routing,
art direction, aspect clamp and structural floor.

Answer sets are built at **generation** time, not serve time: canonical plus
article- and possessive-stripped forms plus author-supplied near-misses. The
acoustic manglings are handled downstream by `lily_evaluation`'s phonetic
tier, which already collapses homophonic initials.

> **Deliberate divergence from the work order.** A4 asks that answer sets
> ride `additional_vocab`. They do not. That slot is pinned by
> WO-LILY-STT-001 to the assistant name and player names — *"never answer
> nouns (expectation-primed matching is the generalizing mechanism;
> preloading answers does not generalize)"* — with
> `tests/test_stt_tuning.py` asserting it. The answer set does its work in
> `acceptable_answers`, which the Tier-1 evaluator already matches exactly,
> by containment, fuzzily and phonetically.

The outbound classifier is **register-aware**, and had to be:
`lily_reasoning.approve_entity_image` is hardcoded family-friendly ("no
nudity, gore, violence"), which is right for a web-sourced general
photograph and would have refused *every* adult entry — leaving those two
partitions permanently empty in a new costume. What does not vary by
register is the structural floor: **nothing involving minors, nothing
non-consensual, nothing outside legal hard limits**, hardcoded into every
brief at every heat. The gate judges register *and* correspondence, and
**fails closed** — a missing classifier is a configuration state, not a
pass.

### Dedup and the quality gate (A5)

Exact hash blocks repeats; a difflib similarity pass (ratio `0.82`, tighter
than the text bank's `0.87` — ten entries cannot afford two questions that
rhyme) blocks near-duplicates, checked against both the arsenal *and*
`lily_questions`, since an arsenal question duplicating a KB question is a
repeat waiting to happen at the table.

Entries land `generating` and are **promoted** to `ready`. Two modes, per
partition, configurable without a deploy:

- `auto` — the classifier plus automated checks promote. Default for `general`.
- `review` — nothing serves until the operator passes it. **Default for both
  adult partitions**: the first batch sets the bar, and a bad explicit image
  reaching a table is expensive in a way a bad picture of a lighthouse is not.

### The seeding job (A6) — `python3 -m lily_arsenal_seed`

```
python3 -m lily_arsenal_seed --status                     # bank health readout
python3 -m lily_arsenal_seed --partition all              # top every shelf up
python3 -m lily_arsenal_seed --partition general --depth 10
python3 -m lily_arsenal_seed --dry-run                    # plan, cost nothing
python3 -m lily_arsenal_seed --review adult_explicit      # list pending entries
python3 -m lily_arsenal_seed --promote <entry-id>
```

- **Idempotent** — tops up to target, sized off `ready + pending-review`, so
  a re-run under a review gate creates nothing rather than piling a second
  batch on top of the one awaiting review.
- **Concurrency-safe, structurally** — a partial unique index on
  `lily_picture_arsenal_runs(partition) WHERE status='running'` means the
  second concurrent run cannot insert its row and stands down. Same class of
  guarantee as `UNIQUE(arsenal_id, group_id)`.
- **Resumable** — an interrupted run leaves its entries banked and its row
  stale-hearted; the next run reclaims the dead row (heartbeat older than
  15 min — a *live* run is left alone) and generates only the shortfall.
- **Reports** what it created per partition and **why anything was skipped**
  — printed, not counted-and-forgotten.

### Draw, burn and replenishment (A7 / A8)

The draw takes `partition + status='ready' + NOT EXISTS` a usage row for
this group. A repeat to the same table is **structurally impossible**:
`UNIQUE(arsenal_id, group_id)` makes a second serve a database error, not an
unlikely event. Entries **retire, never delete**, so provenance survives.

The arsenal bucket is **private** — `adult_explicit` images live in it, and a
public content-addressed URL is unprotected by anything once the path leaks.
Rows store a *path*; it is signed for one serve inside a gate-cleared
session.

Replenishment fires at **40% consumed** (`LILY_ARSENAL_REPLENISH_RATIO`),
per partition independently, in the background, **never on the delivery
path**. It is expressed as a ratio rather than a count on purpose: a
hardcoded "fire at 4" silently becomes "fire when the shelf is 80% gone" the
moment depth drops from 10 to 5.

### Moderation is an expected outcome (A9)

On record: `xAI image HTTP 400: Generated image rejected by content
moderation` (2026-08-07 18:47:48). Seeding is exactly where that friction
belongs — offline, where a refusal costs a retry, rather than live at a
table where it costs silence. A refusal is reworked a bounded number of
times down a heat ladder that **keeps the slot's subject**, then skipped and
**counted**. A transport failure is *not* mistaken for a refusal: reworking
a prompt does not fix a missing key.

If a partition's rejection rate crosses 40% the run reports it as a
**finding**, not a shrug: it means the configured heat exceeds what the
provider will paint, and the operator needs the number to decide between a
lower heat and a shallower depth.

### Observability and cost (A10)

`--status` prints per-partition counts by status, pending-review depth,
rejection rate, format spread, oldest ready entry and standing spend.
`ARSENAL_LOW` is raised when a partition falls below its watermark, when a
refill banks nothing, and at the exact moment a draw finds every candidate
partition empty — **an empty shelf is never again discovered by a player.**

Cost counts **attempts, not entries**: a refused generation still bills, and
a summary that only prices what it kept understates the shelf. The bank is a
standing spend against the xAI account rather than a per-game cost, which is
why depth is a config knob (`LILY_ARSENAL_TARGET_DEPTH`, per-partition
overrides) rather than a redeploy.

### The voiceprint is the signature — never found an identity on a room name (2026-08-08)

The identity chain runs **device/connection (step 0, a hint only) → voiceprint
(step 1, the signature) → group (step 2, derived when two or more known voices
play together)**. `_voice_identity_enroll_at_close` violated it by writing to
`self.group_id` unconditionally.

When group resolution had fallen back to the room name, that minted a fresh
**1-sample orphan centroid** rather than adding a sample to the real one. An
orphan keyed to a room name can never be matched TO — the room never recurs —
so it survives only as an extra candidate thinning the margin check for every
genuine match afterwards. The system got measurably worse per broken session.

Live 2026-08-08: three rows where there should have been one —
`grp_0b07f989` (4 samples, 08-07) plus 1-sample orphans at 07:30 and 18:59,
both written while the operator was telling Lily she ought to know his voice.
Nothing in the embedder was broken; it was writing to the wrong key.

Now: on a **room-name** group with no centroid of its own, the voice is matched
FIRST and the sample folds into whatever identity it matches
(`ENROLL_REDIRECTED`); no confident match means **no write**
(`ENROLL_SKIPPED_ORPHAN`) — a sample with nowhere real to go is dropped rather
than orphaned. The guard is deliberately narrow: a group id off participant or
dispatch metadata recurs, so it may still found its first centroid.

**Known modelling limit (not yet addressed).** `lily_voice_identity` is keyed
`unique(group_id, model_tag)` — **one centroid per GROUP, not per person** — and
the probe is raw rolling PCM with no speaker segmentation, even though
Speechmatics diarization is already flowing through the transcript path. Two
people at one table average into a single centroid that matches neither well,
and the same person playing solo and in a group cannot be represented as one
identity. Correcting this means re-keying the centroid to a person and
segmenting the probe by speaker label.

### The frame sink must close when the probe is full (2026-08-08)

`_lily_voice_probe_fork` ran for the entire session — per participant,
resampling every audio frame and doing two full copies of it
(`bytes(frame.data)` → `array`) **on the event loop** — for audio nothing would
ever read again. The probe needs ~8 seconds and enrollment reads the captured
PCM, not the live stream.

That loop is shared with the Silero VAD, and **VAD is what drives barge-in and
turn commit**. Measured live: VAD **24.9s behind realtime**, TTS tail chunks
undelivered (`TAIL_CHUNK_UNDELIVERED | delivered=0/1`), turns dying
mid-sentence, `CUT_RECOVERY | RESUMED` firing on the dead air. A cut turn is
also re-dispatched, which is where the *repetition* comes from — so barge-in
failure and repeating herself are one fault, not two. The sink now breaks once
the probe is full (`PROBE_COMPLETE`).


## Environment

`LIVEKIT_URL` / `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` · `GOOGLE_API_KEY` ·
`ELEVEN_API_KEY` (never `ELEVENLABS_API_KEY`) · `LILY_VOICE_1` (voice1
primary override; defaults to the hardcoded `W3C2vBPukr5b5jvoXhPK`) ·
`LILY_VOICE_ID` (voice2 — Raven's; falls back to `RAVEN_VOICE_ID`; unset =
voice2 unavailable to the switch tool) · `SUPABASE_URL` /
`SUPABASE_SERVICE_ROLE_KEY` · optional:
`LILY_KB_ONLY=1` (curated-bank-only question supply — the demo-day fallback),
`LILY_ANSWER_WINDOW_SECONDS`, `LILY_ROUNDS`, `LILY_QUESTIONS_PER_ROUND`,
the `LILY_TIER1_THRESHOLD_*` / `LILY_OVERLAP_EPSILON_SECONDS` /
`LILY_TIER1_CLARIFY_MARGIN` state-prior tunables (see the state-prior
thresholds section),
`LILY_AUTO_START_MIN_PLAYERS` / `LILY_AUTO_START_LOBBY_GRACE_SECONDS`
(default 90s) / `LILY_AUTO_START_QUIET_SECONDS` (default 20s — quiet after
last user turn before auto-start) (lobby auto-start safety net),
`LILY_UNDELIVERED_REFIRE_QUIET_SECONDS` (default 8s — hold re-asks while
the table is mid-turn), `LILY_GROUP_ID` (group-identity override),
`LILY_ARCHITECT_MODE` (server-authenticated adult-mode testing override),
`LILY_GREETING_MEMORY_BUDGET_SECONDS` (default 1.5) /
`LILY_MEMORY_MIN_QUESTIONS` (default 3) — memory-at-the-door gates,
`LILY_THINKING_BED_PATH`, `LILY_STINGER_CORRECT_PATH`, `LILY_STINGER_INCORRECT_PATH`,
`LILY_STT_MAX_ALTERNATIVES` (default 3; 1 = n-best injection kill switch) /
`LILY_NBEST_DISPERSION_THRESHOLD` (default 0.02 — deliberation escalation),
`LILY_ADULT_DECK` (**default `open`** — adult deck available, spoken 18+ opt-in still required; `sensor` restores the legacy Audeering coupling),
`LILY_NOISE_CANCELLATION` (**default `off`** since WO-LILY-HOTFIX-001;
`nc` opts back in only after the NC-BENCH-001 bench gate — see the WS-14
memo section; `bvc` and every unknown value coerce to `off`),
`LILY_JOB_MEMORY_LIMIT_MB`, `LILY_REASONING_MAX_OUTPUT_TOKENS` (default 4096) /
`LILY_JUDGE_MAX_OUTPUT_TOKENS` (default 1024) — dedicated reasoning/judge budgets
(thinking tokens count toward `max_output_tokens` on Gemini 3.x) · web tools
(reasoning node only): `EXA_API_KEY` / `TAVILY_API_KEY` (missing key = tool
disabled, text-only behavior) · `XAI_API_KEY` (Grok vision — player-photo
analysis via `lily_vision`; missing key = she receives photos but honestly
says she can't look tonight) · `LILY_IMAGEGEN_MODEL` (default
`gemini-2.5-flash-image`, invented-content picture questions) · acoustic
pipeline: `AUDEERING_API_KEY` plus the `AUDEERING_*` tunables listed in the
acoustic-pipeline section (missing key = breaker open, session unaffected).
No secrets in this repo — configure via the deployment
secrets manager (`lk agent update-secrets` with an explicit `--id`; `--overwrite`
is banned in the shared project — LuvByrds is production tenancy).

**Dispatch name is exactly `Lily` (capital L)** in both `WorkerOptions(agent_name=...)`
and `livekit.toml` — a mismatch means rooms spin forever with no agent joining.

## Pre-demo smoke test

0. One full session watching worker memory (1.6.4 memory-monitor issue — monitor code unchanged at 1.6.6; limits set
   explicitly in WorkerOptions). 1. Dispatch targets `Lily` exactly. 2. Cold session:
   3 speakers bound conversationally in the lobby. 3. Collision: narrated order call
   matches reality. 4. "Lily, are you there?" during an open window must NOT score.
5. Funny wrong answer celebrated, not corrected flatly. 6. Suspense hold renders as a
   pause, not a stall. 7. Adult mode: five consecutive adult questions, all NON-EMPTY
   (empty = safety-settings regression); "back to normal" reverts instantly.
8. Reconnect: scores intact from checkpoint. 9. Second browser tab mid-game:
   scoreboard populates from state sync on arrival.
