# Lily voice inventory (freeze)

**Frozen at:** `638dd71` (post PR #12 / HOTFIX-006 baseline)  
**Purpose:** catalog every spoken surface before any `speech/delivery` extract.
Do **not** rewrite copy in the same change that moves code. This file is the
inventory contract: when extracting modules, strings and act names below
must stay byte-stable unless a dedicated personality PR says otherwise.

Audeering / acoustic addressee remains **parked** and is out of scope here.

---

## Voices (TTS presets)

| Preset | Source | Notes |
|---|---|---|
| `voice1` (primary/default) | `lily_config.LILY_VOICE_1_DEFAULT` / `LILY_VOICE_1` | Boots every session |
| `voice2` | `LILY_VOICE_ID` → `RAVEN_VOICE_ID` | Switchable; may be unavailable |

Runtime switch: `lily_list_voices` / `lily_switch_voice` in `lily_voice_switch.py`.
Synthesis: `lily_tts.py` (ElevenLabs v3 stream). Audio tags in square brackets
(`[excited]`, `[pause]`, …) pass the say gate; markdown/emoji do not.

---

## Speech-act registry (`gated_say` acts)

Code-dispatched speech claims a key at **dispatch** (not playout). Acts
observed as string literals in `lily_agent.py`:

| Act | Typical key | Lane |
|---|---|---|
| `greet` | `session_greet` | session |
| `rejoin` | `session_rejoin` | session |
| `game_start` | (often keyless / start path) | game |
| `question_delivery` | `q_{N}_delivery` | game |
| `question_nudge` | (keyless or delivery-adjacent) | game |
| `question_reoffer` | — | game |
| `clarify_question` | `q_{N}_clarify` | game |
| `verdict` | `q_{N}_verdict` / reveal path | game |
| `verdict_hold` | — | game |
| `steal_window` | — | game |
| `skip` | — | game |
| `stop_ack` | — | hold exempt |
| `pace_ack` / `pacing_set` | — | prefs |
| `media_mode` / `media_mode_unavailable` | — | media |
| `mode_revert` | — | adult |
| `forget_confirm` / `forget_done` / `forget_declined` / `forget_already_done` | — | memory |

### Game-lane acts (`_GAME_LANE_ACTS`)

Blocked when no live game (`game_payload_blocked`):

`question_delivery`, `question_nudge`, `verdict`, `reveal`,
`reveal_flourish`, `reveal_scores`, `reveal_finale`, `steal_window`

Sticky STOP (`_delivery_stop_sticky`) blocks every game-lane act even after
ordinary conversation releases the temporary hold. `stop_ack` remains the
single hold-exempt acknowledgment; repeated STOP emits no second ack. Only an
explicit resume/continue command clears the game-delivery latch.

### Key patterns (idempotent claims)

- `session_greet`, `session_rejoin`
- `q_{N}_delivery`, `q_{N}_reveal`, `q_{N}_verdict`, `q_{N}_clarify`, `q_{N}_transition`
- `round_{R}_scores`

---

## Organic / LLM-spoken turns (not keyed at dispatch)

User-turn replies and instruction-driven `generate_reply` text that never
pass a `gated_say` key still hit `tts_node`:

- Lobby banter, fact collection, intake acknowledgments
- Reveal *flavor* around a keyed verdict
- Cut-recovery resume, empty-candidate retry, undelivered nudge copy

Empty STOP on these paths has **no armed sheet** to force — see
`llm_node` empty-STOP intercept (lobby-safe retry / error, not silence).

---

## Deterministic spoken sheets (must not drift on extract)

| Surface | Builder | Used when |
|---|---|---|
| Armed question sheet | `LilyGame.rendered_armed_question()` | Strict delivery rewrite; 2nd empty on delivery |
| MC options | same + `MC_CHOICE_LETTERS` | Delivery with `choices` |

---

## Say-gate transforms (behavior, not copy)

Owned by `lily_say_gate.py` — extract with tests, do not retune casually:

- Markdown/emoji strip; audio-tag preserve
- Leak filter + burn protocol
- Yield after first `?` (MC stem+options exempt)
- Mirror / repeat / paraphrase flags (mostly log-only; regen gate on re-air)
- Hold / question-pending / no-live-game dispatch blocks

---

## Prompt / personality (do not move casually)

- Agent instructions string in the `agent_config_update` / handoff path
  (Lily host prompt + `<tts_guidelines>` + `<voice_output>` + room-read rubric)
- Per-dispatch `instructions=` blobs inside `gated_say` / `instructed_reply`
  callers — **inventory by act**, edit only in personality PRs

SAID-ALREADY ledger: scorekeeper + state-block injection; variety law is
prompt + lint, not a second copy bank.

---

## Extraction order (when soak is clean)

```text
voice inventory freeze          ← this document
  → speech/delivery extract     ← lily_speech_delivery.py (done; zero string edits)
  → supply/custom-round
  → director (adjudicate/transcript)
  → identity (rekey-merge tests must travel with the move)
  → thin entrypoint
```

**Do not** unpark Audeering in the same PRs as extraction.

---

## Regression anchors (must stay green)

- HOTFIX-006 suite (`tests/test_hotfix006_*.py`)
- Claim integrity / undelivered reconcile / round-loop auto-start quiet
- Say-gate / small-sweep TTS hygiene
- Persistence rekey merge-on-conflict
- Empty STOP / empty-candidate paths (llm_node + tts_node)
