# NC bench — WO-LILY-NC-BENCH-001 Task 1

The cold-join gate Krisp NC must pass to return to production. NC's
production record is two kills in two deployments (1.6.4 NcSession
SIGABRT; the 2026-08-06 RoomIO deaf-mute wedge at 1.6.6) — it does not
get another production attempt on belief. See the WS-14 memo section in
the repo README for the full record.

## Runbook (operator)

1. **Isolated test slot.** Never production. Same pins as prod:
   `livekit-agents==1.6.6`, `livekit-plugins-noise-cancellation==0.2.6`
   (the Dockerfile builds this already — deploy the current main to the
   test slot).
2. **Baseline first (NC off).** On the test slot:
   `lk agent update-secrets --id <test-slot-id> LILY_NOISE_CANCELLATION=off`
   (explicit `--id`, never `--overwrite`), then

   ```
   export LIVEKIT_URL=... LIVEKIT_API_KEY=... LIVEKIT_API_SECRET=...   # TEST slot
   export SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=...
   python eval/nc_bench/run_bench.py --joins 10 --label off-baseline
   ```

   Note the reported `mean join-to-ready`.
3. **NC run.** Flip the test slot:
   `lk agent update-secrets --id <test-slot-id> LILY_NOISE_CANCELLATION=nc`,
   then

   ```
   python eval/nc_bench/run_bench.py --joins 10 --label nc \
       --baseline-latency <mean from step 2>
   ```

4. **Probe WAV.** `--spoken-wav` needs a short 16-bit PCM WAV of a
   spoken phrase (e.g. "hi Lily, my name is Bench"); any recorded
   utterance works — it exists so the capture path has real speech to
   carry to Speechmatics.

## Pass criteria (from the WO, encoded in the script's verdict)

- 10/10 job accepts
- greet reaches playout on EVERY join (audible frames or LILY
  transcript row)
- mic frames reach Speechmatics on every join (non-LILY transcript row)
- mean join-to-ready within 2× the NC-off baseline

**PASS** → re-enable in production (`LILY_NOISE_CANCELLATION=nc` on the
prod slot) behind `tests/test_wedge_recovery.py`'s join-path regression;
watch one live session. **FAIL** → NC stays off, plugin convicted: file
the upstream issue attaching `results_nc.json` and the 08-06 wedge
evidence; if a newer `noise-cancellation` release claims the fix,
re-bench against it — never straight to prod. Record either outcome in
the WS-14 memo (README), and paste both results tables into the WO
close-out.

## Baseline rider (Task 4)

`baseline_rider.sql` computes the NC-off live-session quality numbers
(dropped-answer rate, phantom labels, attribution spread, segment-span
sanity) for comparison against the Aug 5 audited session — run it after
the next real table session and record the verdict in the WS-14 memo.
