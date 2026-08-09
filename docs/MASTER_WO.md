# LILY — Master Work Order

Document version: 2026-08-09
Pinned at P0-5 start: `main @ 1f2ccef`
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

## Epic P0 — BE8D8B live fire

Execution is strictly top-to-bottom; each row is its own merged PR.

| ID | Ticket | Done when | Status |
|---|---|---|---|
| P0-1 | False clean slate impossible | Returner/unsettled cannot air clean slate, no saved voices, or no past games; exact 22:03 fixture | Shipped `aa5afb6` |
| P0-2 | Multi-intent before start | Voice + adult + pictures + consent + play are parsed before any Round One; setup blocks start until committed | Shipped `148e9ac` |
| P0-3 | Explicit 18+ consumes gate | Clear 18+/age/birth-year text latches consent; no same-session re-ask without cause | Shipped `eee74ca` |
| P0-4 | Late recognition seam only | Never fires over claimed delivery, open window, adjudication, or mid-question | Shipped `1f2ccef` |
| P0-5 | One start owner | No "Round" debris/free kickoff while setup is pending; one keyed start | **Completed in this change** |

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

## Later / parked

- D1 schema-safe writes; D2 session audit pull; persistence work only from
  observed failures.
- Structural extracts only when P0+A+C are boringly green.
- Audeering, default Turn Detector, prompt polish, model fallback, and
  multi-agent hosting remain out of scope.

## Greppable live tails

`LILY_SPINE` · `LILY_LLM | EMPTY_STOP*` · `LILY_SAY` · `LILY_DELIVERY` ·
`LILY_REGEN` · `LILY_CUT_RECOVERY` · `LILY_MEDIA` · `LILY_SUPPLY` ·
`LILY_MEMORY` · `start_blocked_reason` · `LILY_SCORE` ·
`SCORE_DIVERGENCE` · `CUSTOM_ROUND_DIVERGENCE`
