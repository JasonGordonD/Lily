# WO-LILY-UPGRADE-168 — close-out (livekit-agents 1.6.6 → 1.6.8)

**Status:** code complete, merged to `main`. Remaining gate is operator/deploy
only: deploy `main` and place one live session (U5 acceptance). Suite **1383
green** against a real in-env 1.6.8 install.

**Version:** 1.6.8 confirmed current on PyPI at execution (no 1.6.9+). Held at
1.6.8.

## Pins (U2 — plugin lockstep)

| Package | 1.6.6 → | Note |
|---|---|---|
| livekit-agents | **1.6.8** | core |
| livekit-plugins-speechmatics | **1.6.8** | monorepo lockstep (`>=1.6.8`; `speechmatics-voice[smart]>=0.2.8`) |
| livekit-plugins-google | **1.6.8** | monorepo lockstep |
| livekit-plugins-silero | **1.6.8** | monorepo lockstep |
| livekit-plugins-openai | **1.6.8** | monorepo lockstep |
| livekit-plugins-noise-cancellation | **0.2.6 (held)** | independently versioned, depends only on `livekit` core; `pip check` clean at 1.6.8. Not bumped — NC upgrades for compatibility only and 0.2.6 IS compatible. Stays OFF (code default + slot). |
| livekit-plugins-ai-coustics | **0.3.0 (held)** | already current; `>=1.4.2` core dep satisfied. |

Real install verified in-env; `pip check` no livekit conflicts; full suite green.
**NC stays off** — asserted post-upgrade by `test_interruption_layer` /
`test_wedge_recovery` (both green at 1.6.8) and `test_upgrade_168`.

## U1 — coupling checklist (git-verified against the 1.6.6/1.6.7/1.6.8 tags)

| Surface | Verdict |
|---|---|
| AgentSession lifecycle | **unchanged** (additive: `session.amd`/IVR props, internal hooks) |
| RoomInputOptions / RoomOutputOptions / AudioInputOptions | **unchanged** — `room_io/types.py` byte-identical 1.6.6→1.6.8; the HOTFIX-001 native-input-options move still correct |
| SpeechHandle & playout signalling | **unchanged** — `speech_handle.py` zero diff |
| agent_state_changed / speech_created | **unchanged** schemas/ordering; additive new `user_transcription_timeout` event; user-state transition on final STT refined (#6478) |
| Turn-detection interface | **interface unchanged**; behavior-only shift in dynamic endpointing (#6265, below) → handled/watch |
| STT / Speechmatics (known_speakers, focus mode) | **unchanged** — plugin functionally identical 1.6.6→1.6.8; `SpeakerFocusMode` / `focus_speakers` / `focus_mode=IGNORE` exposed (from `speechmatics-voice>=0.2.8`), available for STT-001 Q0 |
| Job accept / entrypoint | **unchanged** — `job.py` diff telemetry-only |

### Behavior-only shifts (no API change) — watch on the live session
1. **Endpointing `max_delay` is now a fixed ceiling** (#6265). At 1.6.6 it could
   drift upward over a session; at 1.6.8 only `min_delay` is learned and the
   effective delay is `min(learned, max_delay)`. Turn commits may feel snappier /
   more predictable. *Favourable for STT-001 Q4's answer-window `max_delay`
   tuning — the ceiling now holds.*
2. **`playback_started` no longer re-fires on mid-segment resume** (#6636).
   Affects `playback_latency` / `e2e_latency`. Our metrics now read these from
   `ChatMessage.metrics` (the blessed path), so the capture is correct; just be
   aware post-resume turns won't double-count.
3. **New chat-ctx-failure path** marks the `SpeechHandle` done-with-error (non-
   Realtime) or proceeds best-effort (RealtimeError). Opposite of the 1.6.6
   "errors stopped raising" regression — errors are now surfaced. Lily does not
   branch on SpeechHandle error state, so no code change; watch for new error
   logs.

## U3 — the three named deltas
- **(a) Activity-measurement fix — CONFIRMED** (1.6.7, commit `21938a524`, #6496).
  AMD now settles on the endpointing backstop (`max_endpointing_delay`) and can
  settle even if the participant never publishes audio — no more hang in a
  never-silent room. Active by default; verified our config does not override
  `max_endpointing_delay`.
- **(b) Metrics migration — DONE.** Correction from the audit: the deprecation
  landed in **1.6.0**, not 1.6.8, and `metrics_collected` warns per event. Lily
  now uses the two non-deprecated sources — per-turn `ChatMessage.metrics`
  (latency + turn-taking) and `session_usage_updated` (token/char/audio rollup),
  folded by `lily_metrics.LilyMetricsCollector` into the session report AND the
  heartbeat. Honors "she must use all the metrics she can."
- **(c) Issue #6504 (utterance-split) — STILL OPEN at 1.6.8.** No fix commit in
  the tag; the only `inference/eot/` change since 1.6.6 (#6719) is unrelated and
  lands after 1.6.8. Carried forward as the named suspect for **STT-001 Q5**;
  mitigation stays app-side (merge a late final into the just-committed turn).

## U4 — word-level TTS timestamps (assess only)
**Feasible, provider-side, NOT gated by the core bump.** `TimedString` /
`push_timed_transcript` exist since 1.6.6 (unchanged). ElevenLabs exposes
char-level alignment on its **WebSocket `/stream-input` (`sync_alignment=True`)**
and **`/with-timestamps` REST** endpoints — Lily's custom HTTP TTS (plain
`/stream`, audio-only) would need to switch to a timestamp-bearing endpoint and
aggregate chars→words itself. This would let MC answer windows arm on a real word
boundary instead of a modeled stem. **Scoped, not built here** (per U4 and
STT-001) — follow-up task if/when the MC contract is rewired.

## Tests
`test_upgrade_168` (pins==1.6.8, installed==1.6.8, blessed metrics surface +
field names pinned, NC off, real usage shape folds) + `test_metrics_collector`
(14 cases on the corrected API). Suite 1383 green.

## Open (operator/deploy)
- **U5 live session:** deploy `main` and place one session to confirm greet /
  hear / score / record. Deploy sha to be stated at deploy.
- Sequencing note: the migration is `main` commit `c076823` (metrics correction
  `4e0a7d0`); it can deploy as its own unit. Per operator "merge all updates to
  main," the inert voice-identity scaffolding is also on `main` HEAD but changes
  no runtime behavior (flag-gated, no model/DDL present).
