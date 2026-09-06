#!/usr/bin/env python3
"""
scripts/bank_replenish.py — top the question bank OUT OF SESSION
(WO-LILY-SUPPLY-001 S2, deliverable 2).

The same loop the in-session background author runs, with no session, no
room, and no table waiting on it. This is how tonight's bank gets stocked
from CI or cron:

    python3 scripts/bank_replenish.py --status
    python3 scripts/bank_replenish.py --dry-run
    python3 scripts/bank_replenish.py                     # every lane, to target
    python3 scripts/bank_replenish.py --lane general:academic
    python3 scripts/bank_replenish.py --depth 60 --max-new 20
    python3 scripts/bank_replenish.py --effort high       # the quality knob

WHY IT EXISTS SEPARATELY from the in-session job: the in-session job is
opportunistic — it tops the lanes a live session is draining, while that
session plays. This one is deliberate: run it at 4am against every lane
and the bank is deep before anyone sits down. Neither is on the delivery
path; the difference is only who starts them.

IDEMPOTENT. It tops up to target and sizes its work off what is already
servable (status 'ready' or 'active'), so a second run right after a first
creates nothing.

CONCURRENCY-SAFE, structurally. Each lane's run inserts a row into
lily_bank_replenish_runs, whose partial unique index allows exactly one
'running' row per lane — so a cron run launched while an in-session author
is working that lane is told to stand down instead of double-filling it.

WATERMARK BY DEFAULT, --all TO FORCE. Without --all the runner replenishes
only the lanes below their 40%-consumed watermark, which is what makes it
safe to run hourly. --all tops every lane to target regardless.

EXIT CODE is non-zero when any lane ran and finished short — a stocking run
that did not stock must not look like a success to a scheduler.
"""

import argparse
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lily_bank_replenish  # noqa: E402
import lily_config  # noqa: E402

logger = logging.getLogger("bank_replenish")


def _build_supabase():
    from supabase import create_client

    url = lily_config.supabase_url()
    key = lily_config.supabase_service_role_key()
    if not url or not key:
        raise RuntimeError(
            "SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing — the "
            "replenisher writes with the service role."
        )
    return create_client(url, key)


def _bind_usage_context(supabase):
    """Bind the durable LLM-usage lane for this process.

    WITHOUT THIS the runner authors for free and reports it. Every call
    through the streaming transport records its tokens via
    lily_metrics.record_llm_call, which routes through the CURRENT
    COLLECTOR — a module global bound by the agent's entrypoint. A CLI
    process has no entrypoint, so nothing is bound, `record_llm_call`
    returns False, no lily_llm_usage row is ever written, and every run
    receipt says cost_tokens=0 on a run that plainly cost tokens. The one
    number the operator needs to make the effort decision would have been
    a constant zero on the only job that will actually be run.

    session_id here is only the FALLBACK: each call passes
    usage_session_id=<run id>, so a run's rows are attributable to it."""
    import lily_metrics

    collector = lily_metrics.LilyMetricsCollector()
    collector.bind_usage_context(
        supabase=supabase, session_id="bank_replenish", phase="offline",
    )
    lily_metrics.set_current_collector(collector)
    return collector


def _build_reasoning():
    """Constructed only when the run will actually author, so --status and
    --dry-run need no provider credential at all."""
    import lily_reasoning

    return lily_reasoning.LilyReasoning()


async def _amain(args) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    supabase = _build_supabase()
    lanes = [args.lane] if args.lane else list(lily_bank_replenish.LANES)

    if args.status:
        health = await lily_bank_replenish.lily_bank_health(supabase, lanes=lanes)
        print(lily_bank_replenish.lily_format_bank_health(health))
        return 0

    if args.effort:
        # The quality knob, for this process only. The accessor reads the
        # environment, so setting it here is exactly what the operator
        # would set in deploy vars — no second code path.
        os.environ["LILY_BANK_REPLENISH_EFFORT"] = args.effort

    marks = []
    for lane in lanes:
        mark = await lily_bank_replenish.lily_bank_watermark(
            supabase, lane=lane, target=args.depth
        )
        marks.append(mark)

    todo = [m for m in marks if args.all or m["below_watermark"]]
    if args.dry_run:
        print("DRY RUN — nothing authored, nothing written.")
        for m in marks:
            fire = "REPLENISH" if m in todo else "hold"
            shortfall = max(0, m["target"] - m["ready"])
            print(
                f"  {m['lane']:<28} servable={m['ready']}/{m['target']} "
                f"consumed={m['consumed_pct']:.0%} shortfall={shortfall} -> {fire}"
            )
        return 0
    if not todo:
        print("every lane is above its watermark — nothing to do.")
        return 0

    collector = _bind_usage_context(supabase)
    reasoning = _build_reasoning()
    author = lily_bank_replenish.lily_live_author(reasoning, supabase=supabase)
    verify = lily_bank_replenish.lily_live_verify(reasoning)

    results = []
    for mark in todo:
        lane = mark["lane"]
        await lily_bank_replenish.lily_run_reclaim_stale(supabase, lane=lane)
        run_id = await lily_bank_replenish.lily_run_start(
            supabase, lane=lane, target=mark["target"],
            ready_at_start=mark["ready"],
        )
        if run_id is None:
            print(f"  {lane}: a run is already active — standing down.")
            continue
        results.append(await lily_bank_replenish.lily_replenish_lane(
            supabase,
            lane=lane,
            author=author,
            verify=verify,
            target=mark["target"],
            max_new=args.max_new,
            run_id=run_id,
        ))

    print(_format_report(results))
    health = await lily_bank_replenish.lily_bank_health(supabase, lanes=lanes)
    print(lily_bank_replenish.lily_format_bank_health(health))
    failures = collector.llm_usage_write_failures
    if failures:
        print(
            f"  WARN {failures} usage row(s) failed to write — the cost "
            "figures above understate this run."
        )
    return 0 if all(r.get("status") == "completed" for r in results) else 1


def _format_report(results) -> str:
    lines = ["=" * 60, "BANK REPLENISHMENT — run report", "=" * 60]
    total = 0
    tokens = 0
    for r in results:
        total += int(r.get("accepted", 0))
        tokens += int(r.get("cost_tokens", 0))
        lines.append(
            f"  {r.get('lane'):<28} {r.get('status')}"
            f"  authored={r.get('authored', 0)}"
            f"  accepted={r.get('accepted', 0)}"
            f"  dup={r.get('dup', 0)}"
            f"  verify_rejected={r.get('rejected_verify', 0)}"
            f"  moderation_rejected={r.get('rejected_moderation', 0)}"
            f"  errors={r.get('errors', 0)}"
            f"  cost_tokens={r.get('cost_tokens', 0)}"
            f"  per_question={r.get('cost_tokens_per_question')}"
        )
        if r.get("notes"):
            lines.append(f"      note: {r['notes']}")
    lines.append("")
    lines.append(
        f"  banked this run: {total}    tokens: {tokens}    effort: "
        f"{lily_config.bank_replenish_effort()}"
    )
    lines.append("=" * 60)
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Top the standing question bank, out of session.",
    )
    parser.add_argument(
        "--lane", choices=list(lily_bank_replenish.LANES), default=None,
        help="one lane (default: every lane)",
    )
    parser.add_argument(
        "--depth", type=int, default=None,
        help="target servable rows per lane (default: config)",
    )
    parser.add_argument(
        "--max-new", type=int, default=None, dest="max_new",
        help="cap rows banked per lane this run (default: config)",
    )
    parser.add_argument(
        "--effort", choices=("low", "medium", "high"), default=None,
        help="reasoning effort for THIS run's authoring (quality knob — "
             "the author is off the delivery path, so this costs latency "
             "nobody is waiting on)",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="top every lane to target, not only those below watermark",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--status", action="store_true", help="print the lane health readout"
    )
    args = parser.parse_args()
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    sys.exit(main())
