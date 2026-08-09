# Y6_DUPLICATE_CLASSIFICATION.md — duplicate/repeat and `[cut off]` classification

**WO:** WO-LILY-HOTFIX-007 (Deliverable 2)
**Sessions:** `lily-9337B1-331ff234`, `lily-1C53C6-a65aef35`, `lily-A070E8-939c67e4` (all 2026-08-09)
**Data source:** Supabase `svqbfxdhpsmioaosuhkb`, table `lily_transcripts`
**Status:** READ-ONLY analysis. No writes were performed to any table.

---

## ⚠️ CAVEAT — READ BEFORE USING ANY NUMBER IN THIS DOCUMENT

**The transcript table is proven unreliable (Y5, `POST_TTS_REWRITE` falsification).** Every count below
is a **bound, not a measurement**:

1. **`[cut off]` is appended to the *full intended* text, not to what aired.**
   `record_agent_turn` writes `clean + " …[cut off]"` (`lily_agent.py:2405`) where `clean` is the whole
   turn. Nothing in the row marks where audio actually stopped.
2. **LILY rows have no `segment_start`.** `record_agent_turn` passes only `segment_end`
   (`lily_agent.py:2404–2409`), so the audible interval of a Lily turn is unrecoverable from this table.
   Confirmed empirically: `segment_start IS NULL` for 100% of LILY rows in all three sessions.
3. **Suppressed speech is recorded as spoken.** Any `tts_node` early return (regen gate, air-dup guard,
   delivery-dup, transition-dup, unowned-kickoff, leak, empty-candidate) returns **before**
   `note_post_tts_text` (`lily_agent.py:12945`). `consume_post_tts_text` then falls back to the raw
   pre-TTS prose (`lily_agent.py:2157–2174`), and `on_agent_speech_finished`'s
   `if interrupted:` branch (`lily_agent.py:5461–5469`) writes it with `[cut off]`.
   `cancel_speech` (`lily_agent.py:4411–4437`) makes this worse: it marks a handle suppressed *and*
   calls `handle.interrupt(force=True)`, so a deliberately cancelled turn arrives as `interrupted=True`.
4. **The record-path dedup misses on exactly these rows.** `record_agent_turn`'s belt compares
   `clean in prior_turns[-6:]` (`lily_agent.py:2374`) — an exact match against **cleaned** text, while
   the row being written is **raw** text. So the guard never fires on the artifact rows.
5. **`created_at` is the batch-flush time (`DEFAULT now()`), not the event time.** For LILY rows the
   observed insert lag is ~0.00 s, so ordering is reliable to <1 s; but at least one **user** row lagged
   **12.84 s** (`lily-A070E8`, `"Correct."`, `segment_end` 11:03:53.24 → `created_at` 11:04:06.09),
   so `ORDER BY created_at` mis-orders user turns relative to agent turns. Timing arguments below use
   `segment_end` where it matters.
6. **Rows may be missing entirely.** `record_agent_turn` returns early on empty text (2349–2351) and on
   its own dedup hit (2374–2380); `TRANSCRIPT_PERSIST_FAILED` / `TRANSCRIPT_PERSIST_UNAVAILABLE`
   (2390–2414) are silent to the table. Flush retains rows in memory on failure and can drop them at
   session close.

**Therefore: duplicate counts are a LOWER bound on airings that were duplicated in the room, and an
UPPER bound on duplicated *rows* being real airings. `[cut off]` counts are an UPPER bound on real
truncation. Audio is required for ground truth and is not available from here.**

---

## 1. Method

1. Pulled every row for each session ordered by `created_at`, with `segment_start`, `segment_end`,
   insert lag, `speaker_label`, `speaker_name` (which carries the confirmed speech-act key for LILY
   rows — see `lily_agent.py:2344–2347`), length, and the `[cut off]` flag.
2. Identified duplicate / near-duplicate agent texts three ways:
   - **exact after normalization** (strip `" …[cut off]"`, collapse all whitespace, lowercase);
   - **prefix-superset** (one normalized text is a strict prefix of another);
   - **paraphrase** (manual reading — no automated detector is trustworthy here).
3. Classified each redundant copy using the discriminators in §2.

### 1.1 Corpus

| session | rows | LILY rows | `[cut off]` rows | act-keyed LILY rows | window |
|---|---|---|---|---|---|
| `lily-9337B1-331ff234` | 102 | 47 | 32 | 7 | 04:00:28 → 04:11:36 |
| `lily-1C53C6-a65aef35` | 53 | 25 | 17 | 3 | 07:37:13 → 07:43:22 |
| `lily-A070E8-939c67e4` | 36 | 19 | 11 | 3 | 11:03:01 → 11:07:30 |

---

## 2. Discriminators (why a copy lands in one class and not another)

Two code facts make text-only classification possible at all:

- **F1 — blank lines prove "never reached TTS".** `lily_clean_for_speech`
  (`lily_say_gate.py:99–102`) drops whitespace-only lines. Therefore **a row containing `\n\n` never
  passed through `note_post_tts_text`** and is raw model prose recorded via the fallback at
  `lily_agent.py:2165–2166`.
- **F2 — a clipped/unclipped pair proves one generation, two records.**
  `lily_yield_after_first_question` (`lily_agent.py:12579–12587`) physically truncates a
  non-MC-delivery turn at the first completed question. So when row *X* ends exactly at a `?` and row
  *Y* is *X* plus the trailing sentences, *X* is the aired (clipped) text and *Y* is the **same
  generation's** unclipped prose.

| Class | Definition (WO) | Signature used |
|---|---|---|
| **1** | re-air after truncation (cut-recovery fired — rightly or wrongly) | The **earlier** member carries `[cut off]`, and the later member is a fresh-worded or repeated recomposition of the same act. Gap ≥ ~3 s (above `false_interruption_timeout`, i.e. reachable by `cut_recovery_grace` / a 10 s watchdog tick). Fresh wording is diagnostic: `_REGEN_REAIR_DIRECTIVE` and `_CUT_RECOVERY_DIRECTIVE` (`lily_speech_delivery.py:27–54`) explicitly order new phrasing. |
| **2** | speculation not discarded (preemptive invalidation leakage) | The **earlier** member aired cleanly (**no** `[cut off]`) — so nothing was truncated and class 1 is excluded by definition — and the later member is the same generation resurfacing (F1 and/or F2) within one turn-taking cycle. Also assigned to sub-3-second twin copies, which are **below every recovery timer** in the codebase and therefore cannot be a re-air. Corroborated by date: `set_game_live_preemptive`'s docstring records that in-game preemptive generation was re-enabled by default **on 2026-08-09** via the volatile-tail split (`lily_agent.py:1414–1419`) — the day of all three sessions. |
| **3** | two lanes narrating one event → **NEEDS_WS6** | Two **differently worded** performances of one game event, neither reducible to the other, with independent evidence both reached the room (player reaction, or contradictory verdicts). This is the HOTFIX-006 N12 signature. |

Where a single row could belong to two groups it is counted **once**, in the class its own strongest
signature dictates (noted inline).

---

## 3. Classification — `lily-9337B1-331ff234`

| group | rows (`segment_end`) | content | gap | class | reasoning |
|---|---|---|---|---|---|
| G1 | 04:01:57 `"Correct …[cut off]"` → 04:01:58 `"Correct! Edgar Allan Poe takes you to two for two.\n\n…"` | verdict for q2 | **1.3 s** | **2** | 1.3 s is below `cut_recovery_grace` and far below one 10 s watchdog tick — no recovery path can produce it. Later member carries `\n\n` (F1) ⇒ raw prose. Two handles, one verdict generation. **1 redundant copy** |
| G2 | 04:04:09, 04:04:10 `"You …[cut off]"` ×2 | truncated opener | **1.4 s** | **2** | same; also both < 15 chars so **both** dedup guards exempt them (`lily_agent.py:2374`, `4799`) → **1** |
| G3 | 04:05:18 `"…That's …[cut off]"` → 04:05:36 `"That's …[cut off]"` → 04:05:42 `"That's …[cut off]"` | resumption of a cut tail | 18 s, 5 s | **1** | Each attempt resumes at the exact word the previous was cut on — the `_CUT_RECOVERY_DIRECTIVE` "pick it straight back up" behaviour. No user row between 04:05:36 and 04:05:42 ⇒ fired into dead air → **2** |
| G4 | 04:08:09, 04:08:21, 04:08:27 `"Ah, …[cut off]"` ×3 → 04:08:35 `q_6_delivery "Ah, seeing the state block…"` | one turn, four attempts | 12 s, 6 s, 8 s | **1** | **No user row anywhere between 04:08:09 and 04:08:35.** Three re-airs into genuine dead air, each itself cut at 3 chars, before the fourth completed. The clearest cut-recovery loop in the corpus → **3** |
| G5 | 04:08:52, 04:08:55 `"I …[cut off]"` ×2 | after 04:08:42 and 04:08:50 cuts | 3.2 s | **1** | preceded by two cut turns; short-fragment exemption lets both through → **2** |
| G6 | 04:09:41, 04:09:49, 04:09:54, 04:09:55 `"I …[cut off]"` ×4 | after 04:09:34 (397 chars, `\n\n`, cut) | 8 s, 5 s, **1.2 s** | **1** ×3, **2** ×1 | the first three match the class-1 cadence; the 04:09:55 copy at **1.2 s** is below every timer → class 2 → **3 + 1** |
| G7 | player testimony, 04:06:08 / 04:08:42 / 04:08:50 / 04:09:49 | *"You just literally started asking me the key to question whatever the fuck it is. And it's not in the transcript"*; *"Shut the fuck up. You just asked me a question"* (0.3 s after `q_6_delivery`); *"You did not read it out loud"*; *"You were asking me a question, but you're typing something else in the transcript"* | — | **3 — NEEDS_WS6** | The audio lane and the glass lane narrated the same delivery differently. Both publish paths exist and are independent: `publish_question_to_glass` (screen) vs `publish_agent_transcription_nowait` (`lily_agent.py:2176–2267`, called on the *interrupted* branch at 5462 with the **raw** text). No second LILY row exists for it, so the evidence is testimony + the dual-publish code path → **1 event** |

**Session totals — class 1: 10 · class 2: 3 · class 3: 1** (14 redundant copies/events).

---

## 4. Classification — `lily-1C53C6-a65aef35`

| group | rows | content | gap | class | reasoning |
|---|---|---|---|---|---|
| A | 07:37:13 `session_greet` (clean, ends at `?`) → 07:37:23 same + `" And what should I call you?"` `[cut off]` | greeting | 10.0 s | **2** | F2: the first ends exactly at the first question mark (`YIELD_AFTER_QUESTION`); the second is the unclipped generation. Earlier member **not** cut ⇒ class 1 excluded → **1** |
| B | 07:37:28 `"Hey, welcome back!… What should I call you?"` `[cut off]` → 07:37:31 `"Hey again — still need a name for you! What should I call you?"` `[cut off]` → 07:37:37 `"No worries at all. What should I call you?"` (clean) | three attempts at "ask the name" | 3 s, 6 s | **1** | earlier members cut; each retry is **freshly worded** — exactly `_REGEN_REAIR_DIRECTIVE`'s instruction → **2** |
| C | 07:37:55 (clean, ends at `?`) → 07:38:06 same + 2 sentences, `\n\n`, `[cut off]` | lobby fun-fact ask | 10.9 s | **2** | F1 + F2 both hold; earlier not cut → **1** |
| D | 07:38:19 `"Alright, Rami — challenge accepted…"` (`\n`, cut) → 07:38:26 **identical modulo newlines** (`\n\n`, cut) | one beat | 7.0 s | **1** | earlier member **is** cut ⇒ a re-air was armed; the re-air came back **verbatim**, was caught by the regen gate (`lily_agent.py:12640`), and the suppressed copy was mis-recorded with `[cut off]`. Textbook Chain D → **1** |
| E | 07:39:08 (clean, ends at `?`) → 07:39:10 same + 1 sentence, `\n\n`, `[cut off]` | adult-deck spice choice | 2.9 s | **2** | F1 + F2; earlier not cut → **1** |
| F | 07:40:59 `"Alright Rami — next one. Which erogenous zone…"` cut → 07:41:11 `"Alright Rami — still on this one. Picture's up: which erogenous zone…"` cut → 07:41:27 `"Still with you, Rami — no rush. Picture's up: …"` clean | three freshly-worded attempts at one q2 delivery | 12 s, 16 s | **1** | earlier members cut, fresh wording each time → **2** |
| F′ | 07:41:27 (clean, ends at `?`) → 07:41:53 same + `"Whenever you're ready."`, `\n\n`, `[cut off]` | same turn resurfacing | 25.4 s | **2** | earlier not cut; F1 + F2 → **1** |
| G | 07:39:46 `"Hey Rami — still here, still jacket-off, still waiting on you…"` cut → 07:40:07 `"Yeah, Rami — right here. Still jacket-off, still waiting on you…"` cut | paraphrase of one "still waiting" beat | 20.6 s | **1** | earlier cut, later is the fresh-worded retry → **1** |
| **H** | 07:40:43 `"Close decade energy, wrong side of mid-century — that one's the **1950s**. **Zero for Rami** so far."` cut → 07:40:51 `"The **1920s** — nailed it, Rami. **Point's yours.** You're on the board at 1."` cut → 07:40:55 `q_1_reveal "Correct — the 1920s. Point to Rami."` clean | **three narrations of one verdict, two of them contradictory** | 8 s, 4 s | **3 — NEEDS_WS6** | This is the HOTFIX-006 N12 defect verbatim (`lily_agent.py:1566–1572`), on a *live* session, after N12 shipped. Two lanes ruled the same answer opposite ways and a third keyed lane then published the reveal. Not reducible to a re-air: the wordings are semantically contradictory, not regenerated → **2** |
| I | 07:43:05 (`\n`, cut) → 07:43:22 **identical modulo newlines** (`\n\n`, cut) | picture-didn't-land explanation | 16.2 s | **1** | same shape as D → **1** |

**Session totals — class 1: 7 · class 2: 4 · class 3: 2** (13 redundant copies).

---

## 5. Classification — `lily-A070E8-939c67e4`

| group | rows | content | gap | class | reasoning |
|---|---|---|---|---|---|
| a | 11:03:01 `session_greet` (clean, ends at `?`) → 11:03:05 same + `" And what should I call you?"` `[cut off]` | greeting | 4.5 s | **2** | F2; earlier not cut → **1** |
| b | 11:04:06 `"Rami it is — sorry about the Robin mix-up. Anyone else joining, or just you?"` (clean, ends at `?`) → 11:04:15 same **+ two more sentences** `[cut off]` | one generation | 9.6 s | **2** | F2. (The 11:04:11 cut row `"Alright — whenever you're ready to kick things off, just say so."` is a paraphrase of the 11:04:15 tail; counted once here, under class 2, per §2) → **1** |
| c | 11:04:37 `"Sorry about that — fixing the name now."` cut → 11:04:42 `"You're right — let me fix that now."` cut → 11:04:53 `"Got it — Robin it is. Fixing the screen now."` cut | three freshly-worded acknowledgements of one fix-the-name event | 4.3 s, 11 s | **1** | **no user row between 11:04:37.75 and 11:04:53.16** — the 11:04:42 copy fired into dead air. Fresh wording each time = regen/cut-recovery directive behaviour → **2** |
| d | 11:05:25 (clean, `\n`) → 11:05:36 **identical modulo newlines** (`\n\n`, cut) | "name's locked in / fun fact" | 11.4 s | **2** | earlier not cut ⇒ class 1 excluded; F1 → **1** |
| **e** | 11:05:50 `q_1_delivery` **"Alright Rami — here's your first one. / Written by William Shakespeare, what is the name of the famously indecisive Danish prince?"** (clean, confirmed) → 11:05:59 **"Round one — academic. Shakespeare's famously indecisive Danish prince: what's his name?"** `[cut off]` | **two differently-worded performances of ONE question delivery** | 8.2 s (0.35 s after the player answered) | **3 — NEEDS_WS6** | **Independently confirmed by the player at 11:06:02: *"Why did you ask the question twice?"*** — and Lily concedes it at 11:06:23 (*"sorry on the double-ask — glitch on my end"*). Escapes the BUG-2 duplicate gate because `_delivery_text_matches_armed` (`lily_speech_delivery.py:441–455`) requires the question to be **presented**, and a paraphrase fails it → `register_delivery_claim` returns `None` and the turn speaks. → **1** |
| f | 11:06:23 `q_2_delivery` (clean, `\n`) → 11:06:30 **identical modulo newlines** (`\n\n`, cut) | verdict + apology + q2 stem | 6.5 s | **2** | F1; earlier not cut. Corroborated by the player at 11:07:00: *"You keep repeating the question. This is shit."* → **1** |

**Session totals — class 1: 2 · class 2: 4 · class 3: 1** (7 redundant copies).

---

## 6. COUNTS TABLE (the deliverable)

### 6.1 Duplicate / repeat classification

| session | (1) re-air after truncation | (2) speculation not discarded | (3) two lanes / **NEEDS_WS6** | total redundant copies |
|---|---:|---:|---:|---:|
| `lily-9337B1-331ff234` | **10** | **3** | **1** | 14 |
| `lily-1C53C6-a65aef35` | **7** | **4** | **2** | 13 |
| `lily-A070E8-939c67e4` | **2** | **4** | **1** | 7 |
| **TOTAL** | **19** | **11** | **4** | **34** |

Distinct duplicate *groups*: 7 (9337B1) + 10 (1C53C6) + 6 (A070E8) = **23 groups**.

**NEEDS_WS6 items (4):**

| # | session | event | evidence |
|---|---|---|---|
| WS6-1 | `lily-9337B1` | audio lane vs glass lane narrating one delivery differently (q6 era, 04:06–04:09) | player testimony ×4 + independent publish paths |
| WS6-2 | `lily-1C53C6` | q1 verdict narrated **three times, twice contradictorily** (1950s/zero vs 1920s/point), then the keyed reveal | contradictory verdicts in-table, 07:40:43 / 07:40:51 / 07:40:55 |
| WS6-3 | `lily-1C53C6` | (same beat) the keyed `q_1_reveal` as a third lane over an already-narrated verdict | `speaker_name='q_1_reveal'` 4 s after the second narration |
| WS6-4 | `lily-A070E8` | q1 delivered twice in two different wordings | player: *"Why did you ask the question twice?"*; Lily concedes the double-ask |

### 6.2 `[cut off]` marker classification

| session | (a) genuinely undelivered audio | (b) post-hoc trim artifact | (c) recovery artifact | indeterminate without audio | total `[cut off]` rows |
|---|---:|---:|---:|---:|---:|
| `lily-9337B1-331ff234` | 0 provable | 0 | **5** | **27** (14 of them sub-20-char fragments) | 32 |
| `lily-1C53C6-a65aef35` | 0 provable | **1** | **8** | **8** | 17 |
| `lily-A070E8-939c67e4` | 0 provable | **2** | **2** | **7** | 11 |
| **TOTAL** | **0 provable** | **3** | **15** | **42** | **60** |

**How each bucket was decided:**

- **(c) recovery artifact — 15 rows.** Every `[cut off]` row that contains a blank line (`\n\n`).
  Per F1 this is provably raw pre-TTS prose, so the row never passed `note_post_tts_text`, so it exited
  `tts_node` through a suppression/recovery early return. The `[cut off]` marker on these rows is
  produced by `handle.interrupted` on a handle whose speech the say gate had already silenced.
  Per session: 5 / 8 / 2.
- **(b) post-hoc trim artifact — 3 rows.** `[cut off]` rows with **no** blank line whose text is the
  unclipped superset of a sibling row that ends exactly at a question mark (F2): `lily-1C53C6` 07:37:23;
  `lily-A070E8` 11:03:05 and 11:04:15. The marked text provably contains material
  `lily_yield_after_first_question` removed before TTS, so the row does not describe what aired.
  *(Three further rows — `lily-1C53C6` 07:38:06, 07:39:10, 07:41:53 — satisfy **both** (b) and (c);
  they are counted once, under (c), because F1 is the stronger proof.)*
- **(a) genuinely undelivered audio — 0 provable.** **This is the honest answer, not a null result.**
  There is no text-only evidence that can establish that audio was cut, because the row text is the
  full intended turn either way (Y5). The 14 sub-20-character fragments in `lily-9337B1`
  (`"You"`, `"I"` ×6, `"Ah,"` ×3, `"That's"` ×2, `"Correct"`) are the **most likely** (a) candidates —
  a turn cut a few hundred ms in — but they are equally consistent with a handle whose `chat_items`
  held only the first token, or with the `_last_assistant_text` fallback at `lily_agent.py:14098`.
  **Classified "indeterminate without audio."**
- **indeterminate — 42 rows.** 14 short fragments (leaning (a)) + 28 rows with no discriminator at all.

**Bound restatement:** of 60 `[cut off]` rows, **at least 18 (30%) provably do not describe truncated
audio**, and **0 provably do**. Real truncation is somewhere in 0–42.

---

## 7. Mechanism notes (what these classes actually are, in code)

**Class 2 has a single mechanical shape** and it is not "the model repeated itself" — it is one
generation reaching the record twice:

```
generation G  ──> tts_node ──> YIELD_AFTER_QUESTION clips G at the first '?'   (A:12579)
                          ──> note_post_tts_text(clipped)                       (A:12945)
                          ──> airs; on_agent_speech_finished(interrupted=False)
                          ──> row 1: CLIPPED text, no marker, act key present
generation G's second handle (speculative run / racing dispatch)
                          ──> exits tts_node early (or never enters)
                          ──> note_post_tts_text NEVER runs
                          ──> consume_post_tts_text falls back to handle.chat_items = RAW G  (A:2165)
                          ──> record_agent_turn dedup compares RAW vs CLEANED -> MISS       (A:2374)
                          ──> row 2: RAW text (blank lines intact) + " …[cut off]"           (A:2405)
```

**Class 1's loop is the WO's named chain**, and the codebase already documents its own failure:
`lily_agent.py:12677–12684` records that on **`lily-A070E8`, 2026-08-09** *"the same cut greeting aired
up to FOUR times"* because the one regen retry also came back verbatim and the old contract aired it
anyway. `lily-9337B1` group G4 (three `"Ah, …[cut off]"` re-airs into dead air, 04:08:09 → 04:08:35) is
the same loop with the fragment length reduced to three characters.

**Why the dedup guards did not stop any of this:** both are exact-match with a 15-character floor —
`air_dup_guard` (`lily_agent.py:4799–4802`) and the record belt (`2374`). Of the 34 redundant copies,
**14 are under 15 characters** (exempt), **15 differ only by whitespace** from their sibling (exact
match fails), and **5 are paraphrases** (no semantic guard is enforced outside a re-air).
**Zero of the 34 were reachable by either guard.**

**Why the duplicate classes leak into the room and the glass:** the same `interrupted` branch that
writes the artifact row also calls `publish_agent_transcription_nowait`
(`lily_agent.py:5462–5466`) with the raw text, publishing it to both the legacy `rtc.Transcription`
wire and the `lk.transcription` text stream (`2210–2265`). That is why `lily-9337B1`'s player kept
reporting a mismatch between what he heard and what the transcript panel showed
(04:06:08, 04:06:27, 04:09:49).

---

## APPENDIX (Y9 prep) — group `grp_0b07f989673dcf11e62da96343a39fd4006c1405`

**RECOMMEND ONLY. No writes were performed. Every row below is a proposal for operator review.**

### A.1 Current contents of the consolidated group

**`lily_memories` — 18 rows** (2026-07-15 → 2026-08-08). Consolidation of the memory layer succeeded:
only 2 memory rows for this human remain outside the group
(`grp_8fdc8713…`, 1 row, player `Meg`; `grp_91940d8f…`, 1 row, player `Romi` — itself an STT variant of
Rami and a likely 8th merge candidate).

| memory id | session | played_at | `player_names` |
|---|---|---|---|
| 11 | `lily-B97241-66a6b597` | 07-15 01:54 | `{rami}` |
| 12 | `lily-BBD306-d2153aa7` | 07-15 03:05 | `{rami}` |
| 13 | `lily-E2347F-7599f00d` | 07-15 04:09 | `{rami}` |
| 14 | `lily-A4E142-140a3e25` | 07-15 04:35 | `{rami}` |
| 15 | `lily-8BDA3A-f9ea3f3d` | 07-15 06:13 | `{rami}` |
| 16 | `lily-9B6371-3cc7e2b8` | 07-15 18:21 | `{rami}` |
| 17 | `lily-BEC6C3-36b1cd15` | 07-16 00:45 | `{rami,ramyeon}` |
| 19 | `lily-CC9E19-19c2b804` | 08-04 01:27 | `{rami}` |
| **20** | `lily-81BCB0-583a0f16` | 08-05 23:08 | `{chris,paige,rami,`**`rhonda`**`}` |
| **21** | `lily-05AAC9-902fef7c` | 08-06 21:19 | `{chris,rami,`**`rhonda`**`}` |
| 22 | `lily-105865-04d6622f` | 08-06 21:37 | `{rami,romy,ronnie}` |
| 24 | `lily-0BF814-2feeda45` | 08-07 09:01 | `{rami,rummy}` |
| 25 | `lily-F0417E-6dfc70ef` | 08-07 11:45 | `{rami}` |
| 26 | `lily-8AFC98-baf4be23` | 08-07 12:43 | `{rami}` |
| 27 | `lily-F7E113-90b61665` | 08-07 12:54 | `{rami}` |
| 28 | `lily-FFDEAE-ba016154` | 08-07 18:57 | `{rami}` |
| **29** | `lily-16A9AE-e14aef96` | 08-08 20:49 | `{chris,rami,`**`rhonda`**`}` |
| **30** | `lily-D99BE7-69362716` | 08-08 21:12 | `{chris,`**`miranda`**`,rami,`**`rhonda`**`}` |

**`lily_group_facts` — 11 rows.** 9× `Rami`, 1× `Ramyeon`, **1× `Rhonda`** (id 7:
*"best wrong answer: Naming the red planet after some guy named Mark."*, from `lily-81BCB0-583a0f16`).

**`lily_speaker_voiceprints` — 1 row.** id 337, `speaker_label='S1'`, `player_name='Rami'`,
created 08-09 19:09, 1 embedding.

### A.2 🚩 Finding: the consolidation moved memories but **not** voiceprints

The group holds **1** voiceprint for a roster that its own memories show as at least
{Rami, Rhonda, Chris, Paige, Miranda}. The other identities' voiceprints are still scattered:

| group_id | vp rows | player_names | speaker_labels | note |
|---|---:|---|---|---|
| `grp_0b07f989…` (**target**) | 1 | Rami | S1 | ← the consolidated group |
| `grp_369b8b39…` | 8 | (null), Chris, Rami, **Rhonda** | Chris, Paige, Rami, **Rhonda**, S1–S4 | pre-merge home; `updated_at` on id 49 is **08-09 11:07** — i.e. session `lily-A070E8` was *still binding here* |
| `grp_cd4d1529…` | 4 | (null), Chris, Rami, **Rhonda** | S1–S4 | second fragment |
| **`lily-D99BE7-69362716`** | 3 | Chris, Rami, **Rhonda** | S1–S3 | 🚩 **`group_id` is a SESSION id** — a throwaway group that was never upgraded |
| **`lily-5776D6-e029015a`** | 4 | (null) | Chris, Paige, Rami, **Rhonda** | 🚩 same defect; identities live only in `speaker_label` |
| + 20 further `lily-*` group_ids | 1–3 each | mostly Rami / (null) | S1–S3 | 🚩 24 of 33 distinct `group_id` values in the table are session ids |

**Recommendation R0 (blocking, before any rename):** the voiceprint layer needs the same
consolidation the memory layer already got, and the 24 session-id-shaped `group_id` values need to be
resolved or retired. Renaming Rhonda→Randa in one fragment while four others keep `Rhonda` will
produce a fifth spelling of the same person rather than fewer.

### A.3 Rhonda / Miranda occurrence census — **73 occurrences across 9 locations**

| location | value | occurrences | row ids | in target group? |
|---|---|---:|---|---|
| `lily_memories.player_names[]` | `rhonda` | **4** | 20, 21, 29, 30 | ✅ all |
| `lily_memories.player_names[]` | `miranda` | **1** | 30 | ✅ |
| `lily_memories.players[].name` | `Rhonda` | **4** | 20, 21, 29, 30 | ✅ all |
| `lily_memories.players[].name` | `Miranda` | **1** | 30 | ✅ |
| `lily_memories.summary` | `Rhonda` | **4** | 20, 21, 29, 30 | ✅ all |
| `lily_memories.summary` | `Miranda` | **1** | 30 | ✅ |
| `lily_memories.highlights` | `Rhonda` | **1** | 20 | ✅ |
| `lily_group_facts.player_name` | `Rhonda` | **1** | 7 | ✅ |
| `lily_speaker_voiceprints.player_name` | `Rhonda` | **4** | 97, 247, 264, 281 | ❌ **0 of 4** (3 other groups) |
| `lily_speaker_voiceprints.speaker_label` | `Rhonda` | **2** | 97, 121 | ❌ **0 of 2** (2 other groups) |
| `lily_transcripts.speaker_name` | `Rhonda` | **41** | 6 sessions* | n/a (session-scoped) |
| `lily_transcripts.speaker_name` | `Miranda` | **2** | `lily-4FB3B2-b6bed65e` | n/a |
| `lily_transcripts.speaker_label` | `Rhonda` | **7** | 3 sessions† | n/a |

\* `lily-81BCB0` 14, `lily-D99BE7` 11, `lily-16A9AE` 7, `lily-4FB3B2` 5, `lily-89A97A` 3, `lily-05AAC9` 1.
† `lily-05AAC9` 2, `lily-89A97A` 4, `lily-48630B` 1.

**Summary of the count:** **73 total** = 23 in the memory/identity layer (16 in `lily_memories`,
1 in `lily_group_facts`, 6 in `lily_speaker_voiceprints`) + 50 in `lily_transcripts`.
Of the four locations the WO names explicitly
(`memories.player_names`, `voiceprints.player_name`, `voiceprints.speaker_label`, `group_facts`):
**12 occurrences** — 5 of which are inside the target group and 6 of which are **not**.

### A.4 Proposed corrections to **Randa** — operator review table

The WO states the ground truth: **the player recorded as "Rhonda" / "Miranda" is RANDA**; both are STT
mishearings. The corpus independently supports one-person-many-spellings: the same table also produced
`Romy`, `Ronnie`, `Rummy`, `Ramyeon`, `Romi`, `Robin` and `Correct` as *player names* for the operator
himself (see `lily-A070E8` rows 5–21, where the on-screen name went Robin → Correct → Rami).

| # | table.column | row / key | current | **proposed** | risk / note |
|---|---|---|---|---|---|
| C1 | `lily_memories.player_names[]` | id 20 | `rhonda` | `randa` | array element; lowercase convention |
| C2 | `lily_memories.player_names[]` | id 21 | `rhonda` | `randa` | |
| C3 | `lily_memories.player_names[]` | id 29 | `rhonda` | `randa` | |
| C4 | `lily_memories.player_names[]` | id 30 | `rhonda` | `randa` | ⚠️ **row 30 holds BOTH `rhonda` and `miranda`** — if both are Randa, the array must **collapse to one** `randa` element, not two |
| C5 | `lily_memories.player_names[]` | id 30 | `miranda` | *(merge into C4)* | ⚠️ **needs operator confirmation that Miranda ≠ a real 4th player.** `lily-D99BE7` shows Rhonda 1 pt / Chris 1 pt / Rami 0 / Miranda 0 — a merge changes the recorded roster size from 4 to 3 |
| C6 | `lily_memories.players[].name` | id 20 | `Rhonda` (score 2) | `Randa` | jsonb; preserve score/streak |
| C7 | `lily_memories.players[].name` | id 21 | `Rhonda` (score 1) | `Randa` | |
| C8 | `lily_memories.players[].name` | id 29 | `Rhonda` (score 0) | `Randa` | |
| C9 | `lily_memories.players[].name` | id 30 | `Rhonda` (1), `Miranda` (0) | `Randa` (1) | ⚠️ **score merge**: 1 + 0 = 1. Same confirmation gate as C5 |
| C10 | `lily_memories.summary` | id 20 | `…Rami 4, Rhonda 2, Chris 1, Paige 1.` | `…Rami 4, Randa 2, Chris 1, Paige 1.` | derived text; regenerate rather than string-replace if a generator exists |
| C11 | `lily_memories.summary` | id 21 | `…Rami 1, Rhonda 1, Chris 1.` | `…Rami 1, Randa 1, Chris 1.` | |
| C12 | `lily_memories.summary` | id 29 | `…Rami 2, Rhonda 0, Chris 0.` | `…Rami 2, Randa 0, Chris 0.` | |
| C13 | `lily_memories.summary` | id 30 | `…Rhonda 1, Chris 1, Rami 0, Miranda 0.` | `…Randa 1, Chris 1, Rami 0.` | ⚠️ roster size changes 4→3 |
| C14 | `lily_memories.highlights` | id 20 | `{"player": "Rhonda", …}` | `{"player": "Randa", …}` | jsonb array element |
| C15 | `lily_group_facts.player_name` | id 7 | `Rhonda` | `Randa` | fact text mentions no name — safe |
| C16 | `lily_speaker_voiceprints.player_name` | id 247 (`grp_369b8b39…`, S2) | `Rhonda` | `Randa` | 🚩 **outside the target group** — do R0 first |
| C17 | `lily_speaker_voiceprints.player_name` | id 264 (`lily-D99BE7-69362716`, S2) | `Rhonda` | `Randa` | 🚩 outside target group **and** the group_id is a session id |
| C18 | `lily_speaker_voiceprints.player_name` | id 281 (`grp_cd4d1529…`, S2) | `Rhonda` | `Randa` | 🚩 outside target group |
| C19 | `lily_speaker_voiceprints.player_name` | id 97 (`grp_369b8b39…`) | `Rhonda` | `Randa` | 🚩 outside target group |
| C20 | `lily_speaker_voiceprints.speaker_label` | id 97 (`grp_369b8b39…`) | `Rhonda` | `Randa` | ⚠️ **label, not name.** A name-shaped `speaker_label` is itself a defect (labels should be `S1..Sn`); prefer resolving the label than renaming it. Also duplicates id 247 (same person, two rows, one keyed `Rhonda` and one keyed `S2`) — **dedupe before rename** |
| C21 | `lily_speaker_voiceprints.speaker_label` | id 121 (`lily-5776D6-e029015a`) | `Rhonda` (player_name NULL) | `Randa` **or** resolve to `S{n}` + set `player_name='Randa'` | 🚩 group_id is a session id; `player_name` is NULL, so the identity exists **only** in the label |
| C22 | `lily_transcripts.speaker_name` | 41 rows across 6 sessions | `Rhonda` | `Randa` | retro-relabel; `lily_persistence.py:1596–1605` already implements exactly this update as merge leg 1 |
| C23 | `lily_transcripts.speaker_name` | 2 rows, `lily-4FB3B2-b6bed65e` | `Miranda` | `Randa` | same confirmation gate as C5 |
| C24 | `lily_transcripts.speaker_label` | 7 rows across 3 sessions | `Rhonda` | prefer resolve-to-`S{n}`; else `Randa` | same label-vs-name concern as C20 |
| C25 | `lily_addressee_log.player_name` | not queried in this pass | — | audit for `Rhonda`/`Miranda` before any rename | `lily_persistence.py:1612–1621` treats this as merge leg 2 — **it must not be skipped**, or the addressee log will disagree with the transcripts |

### A.5 Recommended sequence (recommend only)

1. **R0** — consolidate the voiceprint layer and resolve the 24 session-id-shaped `group_id` values.
   Renaming before this creates a fifth spelling instead of removing four.
2. **Confirm with the operator whether `Miranda` is Randa or a distinct 4th player at
   `lily-D99BE7-69362716`.** C5/C9/C13/C23 all depend on it, and getting it wrong silently changes a
   recorded roster size and a score total.
3. **Dedupe** voiceprint rows 97 ↔ 247 (one person, two rows in one group) before relabelling —
   `lily_merge_speaker_into_player` already has a dedupe leg (`lily_persistence.py:1578–1580`).
4. Apply C1–C15 (in-group memory/fact layer) as one transaction.
5. Apply C16–C21 (voiceprints) only after R0.
6. Apply C22–C25 (retro-relabel) via the existing merge helper so transcripts, addressee log and
   voiceprints move together; that helper logs `LILY_MERGE | PARTIAL | safe_to_rerun=true` on
   partial failure (`lily_persistence.py:1570–1583`), so a failed leg is re-fireable.
7. Prefer `speaker_label` values of the form `S{n}` throughout; a name-shaped label is what let one
   human accumulate rows under `Rhonda`, `S2` and `Chris`-adjacent labels in three separate groups.
