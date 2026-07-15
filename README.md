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

## Stack

| Layer | Choice |
|---|---|
| Framework | `livekit-agents==1.6.4` (plugin family pinned to match; JRVS is the fleet reference install) |
| STT | Speechmatics — `en`, diarization, ENHANCED, `speaker_sensitivity=0.5`, `prefer_current_speaker=True`, `max_speakers=7` (fixed at construction — no in-flight update path exists at 1.6.4), `ignore_speakers=["__ASSISTANT__"]` |
| Vocal LLM | `gemini-3.5-flash` — every spoken turn; explicit `safety_settings` (adult-product context), `thinking_config={"thinking_level": "low"}`, `max_output_tokens ≥ 600`, default sampling |
| Reasoning LLM | `gemini-3.1-pro-preview` — background node, own google-genai client (HTTP isolation): question prefetch (N+1) + verification at prefetch time; never speaks |
| TTS | ElevenLabs v3 via `lily_tts.py` (`/v1/text-to-speech/{voice_id}/stream`; the dialogue endpoint stays off per fleet revert). Voice: Raven's (env `LILY_VOICE_ID`, falls back to `RAVEN_VOICE_ID`) |
| VAD | Silero — barge-in enabled; STT is never gated during TTS |
| Persistence | Supabase (`lily_*` tables), fail-fast init, checkpoint on score change / 60s / key events |

## Architecture invariants

- **No generation gate, no trigger loop, no watchdog.** Lily speaks by default —
  silence is her failure mode (the deliberate inversion of Lovebirds' Raven).
- **The scorekeeper owns order; the LLM owns correctness.** First-committed-answer
  is a timestamp comparison in `lily_scorekeeper.py`, never an LLM judgment.
- **Answer window** opens on question TTS playout completion (SpeechHandle — 1.6.4
  has no dedicated playout event), runs a bounded duration; finals-only scoring, one
  candidate per player, segments outside the window are game-inert.
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

The question-spoken gate (which decides "did Lily just perform the armed
question, so open the answer window?") is now a tiered preference, not a
hard gate (`lily_evaluation.lily_question_spoken_ratio`, logged as
`LILY_WINDOW | OPEN | reason=... ratio=...`): ≥0.6 distinctive-token
overlap opens as `verbatim`, ≥0.3 as `paraphrase` (both with a floor of
two matched tokens so a single incidental word never opens it), and after
2 finished agent turns with a question armed in phase `question` the
window opens as `fallback_any_agent_speech` (warning-logged) — the
pipeline can never again stall on Lily's phrasing. The old 60% hard gate
blocked the window on her paraphrase habit and left the game running
through `lily_award_bonus` only. The prompt now also requires her to
perform the NEXT QUESTION word-for-word and never to invent questions
mid-game (the old "generating questions" section is scoped to the
broken-question-machine fallback only).

With the pipeline engaged, `lily_answers` rows (one per adjudicated
attempt, schema `(session_id, player_name, question_id, question_index,
transcript, verdict, eval_tier, awarded_points, ts)`) and real
`question_count` values in `lily_memories` (read straight off
`scorekeeper.question_number`) flow from the existing write paths.
Tests: `tests/test_round_loop.py`.

## The say gate (speech-boundary bug class, 2026-07-14 WO)

The live bug class — double greeting, double question delivery (BUG-2), a
state block read aloud, and an answer leak (the Bosporus answer spoken
BEFORE its question) — is closed by one gateway: `lily_say_gate.py`
(pure stdlib, offline-tested) plus its wiring in `lily_agent.py`.

### Idempotent speech acts

Game-critical speech acts carry state keys — `session_greet`,
`session_rejoin`, `q_{N}_delivery`, `q_{N}_reveal`, `round_{N}_scores`,
`finale` — in a per-session `SpeechActRegistry` on `LilyGame`. Keys are
claimed with an atomic check-and-set **at dispatch time** — never at
playback completion, which is exactly the race window that produced the
live duplicates. Lifecycle: **claim at dispatch → confirm at playout
completion → release on playback failure** (a claimed-but-never-played
act releases so a retry can redeliver — the 19:27:52 swallowed-delivery
class; a confirmed act stays done forever). All code-triggered speech
routes through `LilyGame.gated_say(key, act, instructions, source)`:
a first claim logs `LILY_SAY | act=... | key=... | source=...`, a second
claim logs `LILY_SAY_SUPPRESSED | reason=dup | key=...` and does NOT
speak. The session opener has two trigger paths (agent `on_enter` and the
entrypoint) — both dispatch the same instructions under `session_greet`,
so the loser of the race is silent; a reconnect uses its own
`session_rejoin` key and never trips `session_greet`. Keyless dispatches
(steal window, skip, mode reverts, the prefetch nudge, game start) still
log `LILY_SAY` for the audit trail.

### BUG-2: one authoritative question delivery

The `lily_begin_round` tool result carries the question payload (prompt +
category) and the post-tool turn is the SOLE deliverer — `start_game`
dispatches no racing instructed reply for the `host_tool` source, and the
prompt states the contract (tool-call turn: transition line only; the
next turn asks the question exactly once; the reveal repeats the answer,
never the question). Enforcement is physical: `tts_node` detects an
outbound question performance with the same verbatim detector that opens
the answer window (`lily_question_spoken_ratio ≥ 0.6`) and claims
`q_{N}_delivery`; a failed claim means question N was already delivered,
and the duplicate turn is replaced with silence (no retry — suppressed,
not swallowed).

### Leak filter

The injected state block rides as SYSTEM-role context wrapped in a
sentinel envelope (`<lily_state>…</lily_state>`). `tts_node` runs
`lily_filter_leaks` before the hygiene strip: whole envelopes, envelope
fragments (chunk-boundary partials like `<lily_state`), and any line
carrying a bracketed metadata marker (`[GAME STATE]`, `[room read:`,
`[env:`, `[RETURNING TABLE]`) are deterministically removed and logged as
`LILY_SAY_SUPPRESSED | reason=leak`. Ordinary `[bracket]` audio tags and
clean text pass untouched.

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
  indices into `choices`) — published at the `q_{N}_delivery` claim, the
  window-open fallback, and the reveal; absent for freeform questions.
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
  rewrite-only-on-change — so 1.6.4's equivalence check validates the
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
- Known remaining baseline: ElevenLabs v3 sentence-chunked HTTP synthesis is
  the fleet standard (the low-latency model families don't support the v3
  audio tags her register depends on; the dialogue endpoint stays off per
  the fleet revert).

## Files

```
lily_agent.py        entrypoint + LilyAgent + LilyGame (windows, prefetch, publishing, tools)
lily_scorekeeper.py  N-player roster, per-player state, answer windows, system-directed
                     classifier, state-block builder — pure local state, zero LLM calls
lily_binding.py      lily_bind_speaker name extraction (2s fragment accumulation, stopwords)
lily_addressee.py    addressee-label corpus (B1) pure logic: clarify-reply parser,
                     seconds-into-window, implicit label derivation (stdlib-only)
lily_evaluation.py   Tier-1 matcher + Tier-2 judge contract
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
lily_tts.py          ElevenLabs v3 wrapper (lbs_tts lift; byte-alignment carry, 5K split)
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
tests/               568 tests, run with `python -m pytest tests/` — no network; needs
                     livekit-agents 1.6.4 + google-genai installed
                     (test_award_gate.py / test_context_blocks.py /
                     test_say_gate_dispatch.py / test_forget_flow.py /
                     test_multiple_choice.py / test_group_prefs.py import livekit)
```

## Persistent memory (rematch)

Lily remembers a table across sessions, keyed on a stable **group identity**.
The resolution chain (every step observable — `LILY_MEMORY | GROUP_ID |
source=... group_id=...` is logged every session, upgrades as
`LILY_MEMORY | GROUP_ID_UPGRADE | source=... old=... new=...`):

1. **(a) `lily_group_id` metadata** — strongest signal. Read from BOTH the
   dispatch/job metadata (`ctx.job.metadata`; the token route mirrors
   participant metadata into `RoomAgentDispatch`, available immediately) and
   the first non-agent participant's token metadata (JSON
   `{"lily_group_id": "<uuid>"}`). Live evidence showed the participant is
   often NOT in `remote_participants` yet when `ctx.connect()` returns, so
   the scan polls up to 3s for the first participant, and a
   `participant_connected` hook upgrades a weak id if a metadata-carrying
   participant joins later (`source=participant_metadata_late`).
2. **(b) voiceprint match** (at game start, roster stabilized): this
   session's `stt.get_speaker_ids()` identifiers are matched by exact string
   overlap against `lily_speaker_voiceprints` rows loaded by roster
   player-name — a hit reuses that prior `group_id`
   (`source=voiceprint_match`). Best-effort: it only hits when Speechmatics
   returns stable identifier strings for a returning voice.
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
`session_id` from both `finish_game` and the shutdown callback: final
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

**Voiceprint enrollment** (`lily_speaker_voiceprints`) fires the moment the
FIRST binding commits, again at game start / group-id upgrade, and once more
— awaited, inside the shutdown gate, so teardown can't race it — at session
close for late binders. The upsert is idempotent on
`(group_id, speaker_label)`, and the group id is read at write time (a
callable), so a mid-session upgrade re-keys in-flight enrollments too.
No-silent-crash: every failure path logs a structured
`LILY_ENROLL | FAILED | reason=...` (no client, plugin API drift, no
identifiers yet, no usable rows, exception); success logs
`LILY_ENROLL | OK | trigger=... group=... speakers=...`.
**1.6.4 API notes:** `stt.get_speaker_ids()` is **async** at
`livekit-plugins-speechmatics 1.6.4` and returns useful identifiers only
after ~5 spoken words per speaker (the multi-trigger schedule exists
precisely so an early empty result self-heals); the code tolerates a future
plugin making it synchronous, and logs `reason=no_get_speaker_ids_api` if a
bump removes it. On a rematch, injected `known_speakers` carry player-name
labels, so returning voices may surface with the player name as the label —
the enrollment row mapper handles both spellings.

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
  fired, cold-greeting a four-time returning table. A STRONG group id
  (dispatch/participant metadata, env override) settles the wait the moment
  its memory load returns (block or provably no history); a weak id (room
  name) leaves it pending so the `participant_metadata_late` upgrade can
  land within the budget. Timeout greets cold exactly as before and lets
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
question can never surface at a general-mode table.

**Session reports:** one `lily_session_reports` row per session at close
(idempotent upsert on `session_id`): the in-memory transcript (the
scorekeeper's rolling buffer — never re-queried from the DB) plus `game_stats`
(final standings, rounds/questions played, per-player answers
attempted/correct, mode changes, callouts, `duration_s`); `report_status`
stays `'pending'` and `assessment` is filled later by the clinical desk,
never by agent code.

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

**The cascade (`lily_forget_group(confirm)`, two-step, UNGATED):** per the
tool-gating principle above, deletion neither mutates game outcomes nor
emits game events, so the tool carries NO `game_started` gate — the right
works from the lobby onward. `confirm=false` is refused (and arms the
deterministic yes/no parse); `confirm=true` runs
`lily_persistence.lily_forget_group_data`: HARD-DELETE of all group rows in
`lily_speaker_voiceprints`, `lily_memories`, `lily_group_facts`, plus the
session-keyed `lily_addressee_log` and `lily_acoustic_trajectories` via the
group's session ids (from `lily_sessions`, plus the current session), plus
`lily_asked_history` and `lily_group_prefs` IF the tables exist (the former
lands with a future WO, the latter only skips on migration-013 lag — a
forgotten table's preferences are recognition data; absent-table errors are
skipped and logged, never failed). `lily_sessions`
is RETAINED but re-keyed to the tombstone `forgotten_<sha1-12 of the old
group id>` — operational records survive without linkable identity;
`lily_answers` is retained untouched because it has no `group_id` column
(migration 001 — session-keyed only). Deletion is awaited, count-VERIFIED
per table (zero rows under the old key), capped at ~10s, and reports
honestly on partial failure — the tool result names which tables succeeded
and tells Lily what to say; a partial failure stays retryable against the
ORIGINAL id. Logs `LILY_FORGET | group=... | tables=... | rows=...` with
per-table counts.

**In-session teardown:** STT `_stt_options.known_speakers` is cleared
(1.6.4 limitation, documented in code: the Speechmatics plugin has no live
de-enrollment API — `update_speakers()` only takes focus/ignore/focus_mode
and `known_speakers` ride the one-shot StartRecognition message, so the
clear guarantees any STT reconnect starts unenrolled while the open stream
keeps its labels until it closes); the game re-binds to a FRESH ANONYMOUS
group id (`anon_<random>`, source `post_forget_anonymous`) and the current
game continues normally — writes continue under the fresh id, but the
device metadata id is dead (the frontend cleared it) and
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
   latest state synchronously in `build_state_block`.
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
  the general deck with a light in-character deflection.

Framing, stamped doc-verbatim at every emit site
(`lily_audeering_consumers.PERCEIVED_FRAMING`): the module estimates "how
the speaker sounds, not necessarily the actual attributes of the speaker";
age MAE is ±8.46yr — the signal can EXIT or BLOCK adult mode, NEVER
authorize it; whole-room verbal consensus remains necessary and is no
longer sufficient. Age/gender are otherwise telemetry-only, stamped
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
(true), `AUDEERING_CHILD_STEP_UP_ENABLED` (true).

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
  is published at delivery time alongside the question text. Seam addition
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
(`voice_only` default | `pictures`), offered once in the lobby and flipped in
code by the spoken-choice detector (`lily_scorekeeper.lily_detect_media_choice`
— same punctuation/fragment-proof command-layer pattern as "skip"/"back to
normal"): "pictures on" / "picture rounds" / "use the screen" → `pictures`;
"voice only" / "no pictures" / "pictures off" → `voice_only` (the OFF
direction wins a collision). Published as the agent attribute `media_mode`.
**Picture questions are excluded entirely in voice_only** — the supply path
never calls a picture builder and strips any cached bank image before arming.

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

## Environment

`LIVEKIT_URL` / `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` · `GOOGLE_API_KEY` ·
`ELEVEN_API_KEY` (never `ELEVENLABS_API_KEY`) · `LILY_VOICE_ID` (falls back to
`RAVEN_VOICE_ID`) · `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` · optional:
`LILY_KB_ONLY=1` (curated-bank-only question supply — the demo-day fallback),
`LILY_ANSWER_WINDOW_SECONDS`, `LILY_ROUNDS`, `LILY_QUESTIONS_PER_ROUND`,
`LILY_AUTO_START_MIN_PLAYERS` / `LILY_AUTO_START_LOBBY_GRACE_SECONDS`
(lobby auto-start safety net), `LILY_GROUP_ID` (group-identity override),
`LILY_GREETING_MEMORY_BUDGET_SECONDS` (default 1.5) /
`LILY_MEMORY_MIN_QUESTIONS` (default 3) — memory-at-the-door gates,
`LILY_THINKING_BED_PATH`, `LILY_STINGER_CORRECT_PATH`, `LILY_STINGER_INCORRECT_PATH`,
`LILY_JOB_MEMORY_LIMIT_MB`, `LILY_REASONING_MAX_OUTPUT_TOKENS` (default 4096) /
`LILY_JUDGE_MAX_OUTPUT_TOKENS` (default 1024) — dedicated reasoning/judge budgets
(thinking tokens count toward `max_output_tokens` on Gemini 3.x) · web tools
(reasoning node only): `EXA_API_KEY` / `TAVILY_API_KEY` (missing key = tool
disabled, text-only behavior) · `LILY_IMAGEGEN_MODEL` (default
`gemini-2.5-flash-image`, invented-content picture questions) · acoustic
pipeline: `AUDEERING_API_KEY` plus the `AUDEERING_*` tunables listed in the
acoustic-pipeline section (missing key = breaker open, session unaffected).
No secrets in this repo — configure via the deployment
secrets manager (`lk agent update-secrets` with an explicit `--id`; `--overwrite`
is banned in the shared project — LuvByrds is production tenancy).

**Dispatch name is exactly `Lily` (capital L)** in both `WorkerOptions(agent_name=...)`
and `livekit.toml` — a mismatch means rooms spin forever with no agent joining.

## Pre-demo smoke test

0. One full session watching worker memory (1.6.4 memory-monitor issue; limits set
   explicitly in WorkerOptions). 1. Dispatch targets `Lily` exactly. 2. Cold session:
   3 speakers bound conversationally in the lobby. 3. Collision: narrated order call
   matches reality. 4. "Lily, are you there?" during an open window must NOT score.
5. Funny wrong answer celebrated, not corrected flatly. 6. Suspense hold renders as a
   pause, not a stall. 7. Adult mode: five consecutive adult questions, all NON-EMPTY
   (empty = safety-settings regression); "back to normal" reverts instantly.
8. Reconnect: scores intact from checkpoint. 9. Second browser tab mid-game:
   scoreboard populates from state sync on arrival.
