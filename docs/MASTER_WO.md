# LILY — Master Work Order

Document version: 2026-08-09
Pinned at post-TTS truth P0-C start: `main @ 377e496`
Product: Multiplayer voice trivia host (LiveKit agent)

This is the repository's single backlog source. Chat status is not a second
work order. Re-pin `main` at the start of every ticket and update this file in
the completing PR.

## North star and process lock

- Personality: do not rewrite; `docs/voice_inventory.md` is law.
- Honesty: ledger, registration, gated speech, media/render truth stay
  single-writer. Unknown is not empty; drawn is not rendered.
- Host first: silence, false memory, false start, and false picture claims
  are client-killers.
- One failure class per PR. Target `main`; update CHANGELOG in every PR and
  README when the operator/product contract changes; merge; delete branch.
- Remote should return to `main` only after each merge.
- Audeering is parked pending an explicit isolated bench PR.

## Live evidence

| Session | Evidence |
|---|---|
| `lily-191313-cdd7c84b` | Original false-slate trust-killer |
| `lily-E66E1B-babe62dc` | Honesty pass post-P0; pictures mode gap |
| `lily-BE8D8B-a19913e2` | 22:03 regression: returner phrase missed; false slate; multi-intent lost; general Q started; late recognition over open Q; consent re-asked |
| `lily-9337B1-331ff234` | Q3 scored/revealed then stale clarify/re-ask; STOP released by follow-up speech; Q5/Q6 continued; glass/TTS diverged from transcript; `Playing` voice identity persisted |

## Epic P0 — BE8D8B live fire

Execution is strictly top-to-bottom; each row is its own merged PR.

| ID | Ticket | Done when | Status |
|---|---|---|---|
| P0-1 | False clean slate impossible | Returner/unsettled cannot air clean slate, no saved voices, or no past games; exact 22:03 fixture | Shipped `aa5afb6` |
| P0-2 | Multi-intent before start | Voice + adult + pictures + consent + play are parsed before any Round One; setup blocks start until committed | Shipped `148e9ac` |
| P0-3 | Explicit 18+ consumes gate | Clear 18+/age/birth-year text latches consent; no same-session re-ask without cause | Shipped `eee74ca` |
| P0-4 | Late recognition seam only | Never fires over claimed delivery, open window, adjudication, or mid-question | Shipped `1f2ccef` |
| P0-5 | One start owner | No "Round" debris/free kickoff while setup is pending; one keyed start | Shipped `72fd25c` |

## Epic P0 — 9337B1 client fire

Each row is a separate merged PR.

| ID | Ticket | Done when | Status |
|---|---|---|---|
| P0-A | Answered question dead forever | Result/reveal clears clarify; no final-answer check, window reopen, or re-ask; Freud fixture | Shipped `bb0077c` |
| P0-B | Sticky global STOP | One STOP freezes game delivery/supply/window until explicit resume | Shipped `377e496` |
| P0-C | Post-TTS truth | Transcript, TTS and glass share the actual post-transform delivery text | **Completed in this change** |
| P0-D | Confirmed identity before Q1 | No semantic-name placeholder (`Playing`) can satisfy start gate | Next |
| P0-E | Voiceprint rekey/identity | Production-schema rekey succeeds; temporary samples quarantine/promote; poisoned rows repaired | Queued |
| P0-F | Pre-window scope | Only speech during actual delivery playout can become an early answer | Queued |
| P0-G | Meta/progression ownership | Delivery watchdog pauses for intake/meta; responsiveness latch clears; one-emission cut behavior | Queued |

## Epic A — live proof after P0

| ID | Proof |
|---|---|
| A1 | Recognition: no false empty; challenge → one why; explicit start only |
| A2 | Pictures: mode ON → seeded URL → metadata → `image_shown`; no false on-screen |
| A3 | Multi-intent: voice + adult + pictures + consent + play all honored before Q1 |

Score only sessions run against the merged `main` SHA.

Next action after P0-5 merges: run A1+A2+A3 on that exact main tip. Any
failure becomes the sole hotfix ticket.

## Shipped picture track

| ID | Main merge | Contract |
|---|---|---|
| B2 | `9740977` | Pictures ON refreshes supply; seeded arsenal draw requires signed URL |
| B3 | `e3e395d` | Metadata `image_url` clears on reveal/finale |
| B4 | `7efb9ca` | On-screen speech requires `image_shown` confirm |
| B1/B5 | Conditional | Activation edges/lane honesty only if A2 exposes gaps |

## Epic C — queued after P0 + live proof

| ID | Ticket |
|---|---|
| C0 | Near-miss delivery confirms + opens, no full re-nudge (shipped `c9e64ff`) |
| C1 | Fragment suppression + one-emission recovery |
| C2 | Utterance-bound score commit |
| C3 | Single verdict key; no double congrats |
| C4 | Delivery lifecycle single owner; watchdogs advance state only |

## Platform migration track (after lifecycle P0s)

| ID | Ticket |
|---|---|
| M1 | Disable every configurable Gemini filter; deterministic fallback for non-configurable `PROHIBITED_CONTENT`; blocked-request observability |
| M2 | LiveKit endpointing → `TurnHandlingOptions`, preserving current detector |
| M3 | Speechmatics `operating_point` → supported `model`, preserving enhanced STT |
| M4 | General/adult vocal → Grok 4.5 with deterministic low/medium effort router |
| M5 | Reasoning/judge/vision/assessment → Grok 4.5, structured Responses API |
| M6 | Prompt caching + append-only context + per-turn temporal context |
| M7 | Prompt contract correction (`lily_system.txt` + voice inventory) |

## Durable content workers

| ID | Ticket |
|---|---|
| W1 | Durable arsenal worker with leases, heartbeat, partition depth and retries |
| W2 | Claude Opus 4.7 general image/category author; deterministic prompt compiler |
| W3 | **All adult sub-theme/category/arsenal authoring: Grok 4.5 High only** |
| W4 | Dedicated renderer + Grok 4.5 Vision correspondence/alt-description review |
| W5 | Start-of-session image draw/sign/preload; visible only at owned delivery |
| W6 | Grok/Opus whole-pack category builder, persistent bank and provenance |
| W7 | Duplicate-question complaint detector + semantic auditor + bank quarantine |

## Later / parked

- D1 universal schema-safe reads/writes; D2 session audit pull.
- Structural extracts only when lifecycle and live proof are boringly green.
- Audeering and the LiveKit Turn Detector default remain parked.

## Greppable live tails

`LILY_SPINE` · `LILY_LLM | EMPTY_STOP*` · `LILY_SAY` · `LILY_DELIVERY` ·
`LILY_REGEN` · `LILY_CUT_RECOVERY` · `LILY_MEDIA` · `LILY_SUPPLY` ·
`LILY_MEMORY` · `start_blocked_reason` · `LILY_SCORE` ·
`SCORE_DIVERGENCE` · `CUSTOM_ROUND_DIVERGENCE`
