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

## Search notes

All five WO-listed moments were found recorded (none had to be marked
canon-target). Wordings above are the exact table rows; the WO's remembered
phrasings differ slightly in two places: the basket-weaving callback is
part of the longer greet quoted in M1, and the no-homework line opens with
"Got it, Rami — ears open," before the WO's remembered fragment. Several
rows appear multiple times in the table with the same text and near-equal
timestamps (`…[cut off]` re-airs); the suite pins the text, the earliest
timestamp is cited.
