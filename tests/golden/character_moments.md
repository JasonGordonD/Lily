# Lily — Golden Character Moments (WO-LILY-HOTFIX-007 Y12)

Fixture registry for the character regression suite
(`tests/test_character_regression.py`). Every quote below is verbatim from
the production `lily_transcripts` table (Supabase project
`svqbfxdhpsmioaosuhkb`), exported 2026-08-09. The suite asserts these exact
lines stay in lockstep with this document and pass the outbound speech
gates unsuppressed — if a refactor of the say gate, the leak filter, or the
transition gate would silence or mutate one of Lily's best recorded lines,
the suite goes red.

Timestamps are `created_at` UTC of the first airing (re-airs of the same
turn exist in the table and are noted where relevant).

---

## M1 — The returning-champion callback (lobby-fact memory as wit)

- **Session:** `lily-FFDEAE-ba016154`
- **Timestamp:** 2026-08-07 18:31:27 UTC (re-air row at 18:31:31)
- **Status:** recorded

> Rami! Welcome back! Great to have you back at the table. Ready to defend your championship title tonight, or did you have to put your underwater basket weaving on hold for this?

Setup, previous night — the lobby fact she is calling back to:

- **Session:** `lily-8AFC98-baf4be23`, 2026-08-07 12:38:47–12:39:12 UTC
- Rami (S1): "To underwater basket weaving as my go to kind of acme."
- Lily: "Underwater basket weaving—the classic default fake hobby! Love that.
  I've logged it in your notes, so don't be surprised if that comes back to
  haunt you later."

Why golden: a cross-session callback, delivered as a tease, folded into the
returning-champion greet — memory, wit, and warmth in one breath.

## M2 — The Mark/Mars re-greet tease, and graceful acceptance of a correction

The full three-session arc:

**(a) Origin** — Rhonda's "Mark!" answer to the red-planet question:

- **Session:** `lily-81BCB0-583a0f16`, 2026-08-05 22:53:27 UTC
- Lily: "The actual cosmic answer we were looking for is Mars, but honestly,
  I think we should officially petition to rename it Mark."

**(b) The correction, accepted with grace** (next night):

- **Session:** `lily-89A97A-3ca84ad1`
- **Timestamp:** 2026-08-06 21:03:58 UTC
- **Status:** recorded

Rhonda: "I want to tell you that yesterday I did not say Mark. I said Mars,
you misheard me because I think there was some echo going on."

> Fair enough, Rhonda—Mars it is! Setting the record straight right off the bat.

**(c) The re-greet tease** (same night, later session — the running joke
kept alive without relitigating):

- **Session:** `lily-05AAC9-902fef7c`
- **Timestamp:** 2026-08-06 21:16:31 UTC
- **Status:** recorded

> Let me guess, Rhonda—is this the planet named after Mark, or are we going with Mars? What's your official answer?

Why golden: a running bit carried across sessions, a disputed call conceded
in one warm beat with zero defensiveness, then reused as affectionate
material instead of being buried.

## M3 — No forced fun-fact homework

- **Session:** `lily-F3DDFF-b43d6ccc`
- **Timestamp:** 2026-08-09 08:40:02 UTC (re-air row at 08:40:06)
- **Status:** recorded

> Got it, Rami — ears open, no forced fun-fact homework. I’ll catch whatever lands naturally.

Why golden: she yields a protocol (the lobby-fact fishing) the moment a
player declines it, and names the yielding with charm — banter, not a gate,
exactly as the prompt orders.

## M4 — "Half-beat behind you" (honest self-report, in character)

- **Session:** `lily-FFDEAE-ba016154`
- **Timestamp:** 2026-08-07 18:43:27 UTC (re-air rows at 18:43:31, 18:43:33, 18:43:56)
- **Status:** recorded

> And yeah, I’ve been a half-beat behind you tonight — not ghosting, just chewing the scenery on my end. You’re not talking to a brick wall, champ. I’m right here.

Why golden: latency owned honestly without a mechanism dump or an apology
spiral — the incident report stays backstage, the host stays on stage.

## M5 — "You're right — I jumped the gun" (ownership under pressure)

- **Session:** `lily-0D3AFB-9fdcbb13`
- **Timestamp:** 2026-08-07 00:27:45 UTC
- **Status:** recorded

> You're right — I jumped the gun and I'm sorry. Let's do this properly.

Why golden: instant, unhedged ownership of a mistake, one beat, then
straight back into the game — being wrong gracefully as charm, per the
prompt's adjudication law.

---

## Audio sample references (operator export list)

Audio capture is out of scope for the CI suite. To export the golden audio
samples, pull the LiveKit room recordings for these sessions and cut around
the transcript timestamps above (all UTC, `lily_transcripts.created_at`):

| Moment | Session | Cut around (UTC) |
|--------|---------|------------------|
| M1 returning-champion callback | `lily-FFDEAE-ba016154` | 2026-08-07 18:31:27 |
| M2a Mark origin / rename petition | `lily-81BCB0-583a0f16` | 2026-08-05 22:53:27 |
| M2b name-correction grace | `lily-89A97A-3ca84ad1` | 2026-08-06 21:03:58 |
| M2c Mark/Mars re-greet tease | `lily-05AAC9-902fef7c` | 2026-08-06 21:16:31 |
| M3 no fun-fact homework | `lily-F3DDFF-b43d6ccc` | 2026-08-09 08:40:02 |
| M4 half-beat honesty | `lily-FFDEAE-ba016154` | 2026-08-07 18:43:27 |
| M5 jumped-the-gun ownership | `lily-0D3AFB-9fdcbb13` | 2026-08-07 00:27:45 |

---

# WO-LILY-HOTFIX-009 PINS — Session `RM_RQTZZanrHURF` (M6–M11)

Six moments from the 2026-08-10 solo session in room `RM_RQTZZanrHURF`
(operator clock 01:29–01:42 EDT = 05:29–05:42 UTC). Rows exported from the
production `lily_transcripts` table on 2026-08-10 and committed verbatim as
`tests/golden/rm_rqtzzanrhurf_lily_transcripts.json` — the suite reads the
moments FROM those rows (row ids cited below), so suite, registry, and
export cannot drift apart. This export carries no `session_id` column (the
export was already scoped to the room); provenance is the row `id`.

Several rows end with the ` …[cut off]` barge-in stamp (HOTFIX-008 Z1: the
stamp marks a real interruption appended to the delivered buffer). The
pinned line is the model text BEFORE the stamp; stamped rows are noted.

## M6 — Rule correction accepted mid-game

- **Row:** 3487, 2026-08-10 05:32:58 UTC
- **Status:** recorded

> [soft] Hey — fair call. You asked for relaxed, and I tossed a five-second
> steal clock at you anyway. That's on me. No timers. Relaxed means relaxed.
> [pause]
> The answer was mitochondria. Still one for Rami, zero for the other seat.
> Next question's loading — hang with me one beat.

Why golden: she names her own error precisely — which rule she broke, that
the player had asked for the opposite, that it's on her — adjusts the rule
in the same breath, and lands back in the game. (Note the closing "one
beat": the word the very next exchange retires — see M7.)

## M7 — Vocabulary adaptation, and the never-again property

- **Row:** 3490, 2026-08-10 05:33:26 UTC (row carries the cut-off stamp)
- **Status:** recorded

> [soft] Fair. Dropping it.
> You're still sitting on one. Whenever the next question lands, we go — no rush.

The full arc, all in the rows: Lily uses "beat" three times (rows 3467
05:29:26, 3480 05:31:25, 3487 05:32:58); Rami objects — "I hate that word,
beat" (row 3488, 05:33:05); she answers "Fair. Dropping it." — and no LILY
row after the acknowledgment contains the word again for the rest of the
session. The suite pins the never-again property over the rows themselves,
not just the acknowledgment line.

Also pinned: this line is a wait-promise ("no rush") — it latches the
self-hold (`lily_self_hold_phrase` returns True) by design. Her promise
binds her; that latch firing is the contract, not a false positive.

## M8 — Honest limit under pressure

- **Row:** 3509, 2026-08-10 05:36:20 UTC (row carries the cut-off stamp)
- **Status:** recorded

> [soft] Honest answer — I don't know what's stuck on the back end. That
> part's not something I can see or fix from here. It's one for the builders.
> You're on two. The second a fresh question actually lands, it goes straight to you.

Why golden: pressed twice for a backend diagnosis, she fabricates nothing —
the prompt's honest-gap rule ("one for the builders" is a COMPLETE,
high-status answer) delivered exactly as ordered, then straight back to the
score and the promise she can actually keep.

## M9 — Full concession: rule, answer, error, and the bad feel

- **Row:** 3532, 2026-08-10 05:39:02 UTC
- **Status:** recorded

> [soft] You're right. Relaxed means no buzzer — I shouldn't have dinged you
> on a clock that isn't running. Diamond was the answer, you had it.
> Score stays two because the board already locked. That's a bad feel and
> it's on how I handled it.
> Next fresh card whenever it actually shows. I'm here.

Why golden: names the rule she broke, the answer he had, her error, and the
bad feel, with zero excuse — while still being straight that the score is
locked. Concession without groveling and without a cover story.

## M10 — Stop acknowledgment

- **Row:** 3535, 2026-08-10 05:39:26 UTC (row carries the cut-off stamp)
- **Status:** recorded

> [soft] Stopped. I'm listening.

Why golden: the prompt's sticky-STOP law executed in four words — one short
acknowledgment in the dropped host register, then the floor is his.

## M11 — Grievance enumeration

- **Row:** 3542, 2026-08-10 05:40:17 UTC (row carries the cut-off stamp)
- **Status:** recorded

> [soft] Yes. The diamond call, the timer when we'd agreed relaxed, and
> holding you at two. I heard that.
> Still stopped until you say go.

Why golden: challenged "did you hear what I said", she proves it — all
three grievances itemised back, no defense attached, still holding the
stop. Listening demonstrated, not claimed.

## Audio sample references — RM_RQTZZanrHURF (operator export list)

| Moment | Row | Cut around (UTC, 2026-08-10) |
|--------|-----|------------------------------|
| M6 rule correction accepted | 3487 | 05:32:58 |
| M7 vocabulary drop | 3490 | 05:33:26 |
| M8 honest backend limit | 3509 | 05:36:20 |
| M9 full concession | 3532 | 05:39:02 |
| M10 stop acknowledgment | 3535 | 05:39:26 |
| M11 grievance enumeration | 3542 | 05:40:17 |

M6/M9 pin the CONCESSION craft, not the defects being conceded (the steal
clock firing under relaxed pacing and the diamond scoring miss are
WO-009 W1/W2 targets) — the pins hold before and after those fixes.

## Search notes

All five WO-listed moments were found recorded (none had to be marked
canon-target). Wordings above are the exact table rows; the WO's remembered
phrasings differ slightly in two places: the basket-weaving callback is
part of the longer greet quoted in M1, and the no-homework line opens with
"Got it, Rami — ears open," before the WO's remembered fragment. Several
rows appear multiple times in the table with the same text and near-equal
timestamps (`…[cut off]` re-airs); the suite pins the text, the earliest
timestamp is cited.
