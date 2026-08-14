"""
lily_arsenal_seed.py — the standing picture arsenal's SEEDING JOB
(WO-LILY-ARSENAL-SEED-001 A6, A9, A10).

Standalone, out-of-session, operator-runnable. This is the thing that was
missing between PATCH-003 P2 and 2026-08-07: the supply logic was specified
and the shelf was built, but nothing ever put anything on it, and an empty
arsenal degrades silently to live generation — 67 seconds of dead air while
a table waits for a picture question.

    python3 -m lily_arsenal_seed --status
    python3 -m lily_arsenal_seed --partition all
    python3 -m lily_arsenal_seed --partition general --depth 10
    python3 -m lily_arsenal_seed --dry-run
    python3 -m lily_arsenal_seed --review adult_explicit
    python3 -m lily_arsenal_seed --promote <entry-id>

IDEMPOTENT. The job tops up to target; it does not deal a fresh batch. It
sizes its work off ready + pending-review (lily_arsenal_bank_depth), so
re-running is always safe and a second run right after a first one creates
nothing at all.

CONCURRENCY-SAFE, structurally. A run inserts a row into
lily_picture_arsenal_runs, and a partial unique index allows exactly one
'running' row per partition. Two operators launching this at once do not
double-fill a shelf; the second is told a run is already active and stands
down. Same class of guarantee as UNIQUE(arsenal_id, group_id) on the usage
table — impossible rather than unlikely.

RESUMABLE. An interrupted run leaves entries banked and its row 'running'
with a stale heartbeat. The next run reclaims the dead row, sees the
entries the dead run already banked still counted in the depth, and
generates only the remaining shortfall.

MODERATION IS EXPECTED (A9). A provider refusal is reworked a bounded
number of times, then skipped and COUNTED. If a partition's rejection rate
is high enough that it cannot reach target, that is REPORTED as a finding —
it means the configured heat exceeds what the provider will paint, and the
operator needs the number to decide. It is never hidden as a failure.

The orchestration below takes every external effect as an injected
callable, so the whole job is exercised by tests with fakes — which matters
more than usual here, because the provider this job depends on
(api.x.ai) is not reachable from anywhere except the deployment.
"""

import argparse
import asyncio
import logging
import sys
import time
from typing import Optional

import lily_arsenal
import lily_arsenal_content
import lily_arsenal_formats
import lily_arsenal_gen
import lily_config

# The live authoring and classification bindings live in the generation
# module — the agent's in-session replenishment needs them too, and it must
# not have to import a CLI job to get them.
lily_author_question = lily_arsenal_gen.lily_author_question
lily_classify_image = lily_arsenal_gen.lily_classify_image
lily_describe_image = lily_arsenal_gen.lily_describe_image

logger = logging.getLogger("lily_arsenal_seed")

# A partition rejecting at or above this rate is reported as a FINDING in
# the run summary: at two rejections in five the configured heat is
# fighting the provider, and the operator should see the number rather
# than a shelf that quietly came up short.
REJECTION_RATE_FINDING_THRESHOLD = 0.40

# argparse const for a bare `--promote-all` (no partition): sweep them all.
_PROMOTE_ALL_EVERY = "__all__"


async def lily_seed_partition(
    supabase,
    *,
    partition: str,
    author,
    imagegen,
    upload,
    classify=None,
    describe=None,
    source_real=None,
    target: Optional[int] = None,
    dry_run: bool = False,
    max_slots: Optional[int] = None,
) -> dict:
    """Top ONE partition up to target. Returns the run summary.

    Never raises: a seeding job that dies halfway through leaving no record
    is how you end up not knowing whether the shelf is stocked."""
    started = time.monotonic()
    tgt = lily_config.arsenal_target_depth(partition) if target is None else target
    summary = {
        "partition": partition,
        "target": tgt,
        "created": 0,
        "skipped_duplicate": 0,
        "rejected_moderation": 0,
        "rejected_gate": 0,
        "errors": 0,
        "cost_usd": 0.0,
        "attempts": 0,
        "duration_seconds": 0.0,
        "started_ready": 0,
        "final_ready": 0,
        "gate_mode": lily_config.arsenal_gate_mode(partition),
        "run_id": None,
        "status": "completed",
        "findings": [],
        "skipped_reasons": [],
    }

    depth = await lily_arsenal.lily_arsenal_bank_depth(supabase, partition=partition)
    summary["started_ready"] = depth["ready"]
    summary["started_pending"] = depth["pending"]
    shortfall = max(0, tgt - depth["depth"])
    if max_slots is not None:
        shortfall = min(shortfall, max_slots)
    summary["planned"] = shortfall

    if shortfall <= 0:
        summary["notes"] = (
            f"already at depth ({depth['ready']} ready + {depth['pending']} "
            f"pending >= target {tgt}) — nothing to do"
        )
        summary["final_ready"] = depth["ready"]
        summary["duration_seconds"] = round(time.monotonic() - started, 2)
        logger.info(
            "LILY_ARSENAL_SEED | NOOP | partition=%s %s", partition,
            summary["notes"],
        )
        return summary

    # Plan the slots BEFORE claiming a run, so --dry-run costs nothing and
    # holds no lock.
    formats = lily_arsenal_formats.lily_formats_for_partition(
        partition,
        real_images_available=lily_config.arsenal_real_images_enabled(),
    )
    plan = lily_arsenal_content.lily_plan_entries(
        partition, shortfall, formats=formats, start_index=depth["depth"],
    )
    summary["plan"] = plan
    if dry_run:
        summary["status"] = "dry_run"
        summary["notes"] = (
            f"dry run: would generate {len(plan)} entries "
            f"({depth['depth']} -> {tgt})"
        )
        summary["final_ready"] = depth["ready"]
        summary["duration_seconds"] = round(time.monotonic() - started, 2)
        return summary

    # Reclaim a dead predecessor, then claim the partition. If the claim
    # fails a live run owns this shelf and we stand down rather than
    # double-fill it.
    await lily_arsenal.lily_run_reclaim_stale(supabase, partition=partition)
    run_id = await lily_arsenal.lily_run_start(
        supabase, partition=partition, target=tgt
    )
    if run_id is None:
        summary["status"] = "skipped_concurrent"
        summary["notes"] = (
            "another seeding run is already active for this partition — "
            "stood down rather than double-fill"
        )
        summary["final_ready"] = depth["ready"]
        summary["duration_seconds"] = round(time.monotonic() - started, 2)
        logger.warning(
            "LILY_ARSENAL_SEED | CONCURRENT | partition=%s — stood down",
            partition,
        )
        return summary
    summary["run_id"] = run_id

    try:
        for index, slot in enumerate(plan):
            result = await lily_arsenal_gen.lily_generate_entry(
                partition=partition,
                plan=slot,
                author=author,
                imagegen=imagegen,
                upload=upload,
                classify=classify,
                describe=describe,
                source_real=source_real,
            )
            summary["attempts"] += result.get("attempts", 0)
            summary["cost_usd"] += result.get("cost_usd", 0.0)
            outcome = result.get("outcome")

            if outcome == lily_arsenal_gen.OUTCOME_CREATED:
                banked = await lily_arsenal.lily_arsenal_insert(
                    supabase,
                    partition=partition,
                    question=result["entry"],
                    run_id=run_id,
                    classifier_ok=True,
                    cost_usd=result.get("cost_usd"),
                    attempts=result.get("attempts", 1),
                )
                if banked:
                    summary["created"] += 1
                else:
                    # The insert's own dedup caught it — a near-duplicate of
                    # something already banked, including something banked
                    # earlier in THIS run.
                    summary["skipped_duplicate"] += 1
                    summary["skipped_reasons"].append(
                        f"slot {index}: near-duplicate, not banked"
                    )
            elif outcome == lily_arsenal_gen.OUTCOME_MODERATION:
                summary["rejected_moderation"] += 1
                summary["skipped_reasons"].append(
                    f"slot {index} ({slot.get('subject_area')}): provider "
                    f"moderation refused after reworks — {result.get('reason', '')[:160]}"
                )
            elif outcome == lily_arsenal_gen.OUTCOME_CLASSIFIER:
                summary["rejected_gate"] += 1
                summary["skipped_reasons"].append(
                    f"slot {index} ({slot.get('subject_area')}): classifier "
                    f"refused — {result.get('reason', '')[:160]}"
                )
            elif outcome == lily_arsenal_gen.OUTCOME_UNAVAILABLE:
                # Generation is DOWN, not refusing. Burning the rest of the
                # plan against a dead provider produces nothing but cost.
                summary["errors"] += 1
                summary["status"] = "halted_generation_unavailable"
                summary["findings"].append(
                    "generation unavailable — halted early: "
                    f"{result.get('reason', '')[:200]}"
                )
                logger.error(
                    "LILY_ARSENAL_SEED | HALTED | partition=%s reason=%s",
                    partition, result.get("reason"),
                )
                break
            else:
                summary["errors"] += 1
                summary["skipped_reasons"].append(
                    f"slot {index}: {outcome} — {result.get('reason', '')[:160]}"
                )

            if index % 3 == 2:
                await lily_arsenal.lily_run_heartbeat(supabase, run_id=run_id)
    except Exception as e:
        summary["status"] = "failed"
        summary["errors"] += 1
        summary["findings"].append(f"run aborted: {type(e).__name__}: {e}")
        logger.error(
            "LILY_ARSENAL_SEED | RUN_FAILED | partition=%s: %s", partition, e
        )

    final = await lily_arsenal.lily_arsenal_bank_depth(supabase, partition=partition)
    summary["final_ready"] = final["ready"]
    summary["final_pending"] = final["pending"]
    summary["duration_seconds"] = round(time.monotonic() - started, 2)
    summary["cost_usd"] = round(summary["cost_usd"], 5)

    # -- A9: report the rejection rate as a finding, never as a shrug ------
    produced = (
        summary["created"] + summary["rejected_moderation"]
        + summary["rejected_gate"]
    )
    rate = (summary["rejected_moderation"] / produced) if produced else 0.0
    summary["moderation_rejection_rate"] = round(rate, 3)
    if rate >= REJECTION_RATE_FINDING_THRESHOLD:
        summary["findings"].append(
            f"moderation rejection rate {rate:.0%} on {partition} — the "
            "configured heat exceeds what the provider will paint. Reaching "
            "target here needs either a lower heat or a smaller depth; this "
            "is an operator decision, not a bug to retry around."
        )
    if final["depth"] < tgt and summary["status"] == "completed":
        summary["status"] = "short_of_target"
        summary["findings"].append(
            f"reached {final['depth']}/{tgt} — short by {tgt - final['depth']}. "
            f"created={summary['created']} "
            f"moderation={summary['rejected_moderation']} "
            f"gate={summary['rejected_gate']} errors={summary['errors']}"
        )
    if summary["gate_mode"] == "review" and final["pending"]:
        summary["findings"].append(
            f"{final['pending']} entries are held at 'generating' awaiting "
            f"operator review — they CANNOT serve until passed "
            f"(python3 -m lily_arsenal_seed --review {partition})"
        )

    summary["notes"] = (
        f"created={summary['created']} dup={summary['skipped_duplicate']} "
        f"moderation={summary['rejected_moderation']} "
        f"gate={summary['rejected_gate']} errors={summary['errors']} "
        f"cost=${summary['cost_usd']:.2f}"
    )
    # PERSIST the per-slot reasons, not just the counts. A6 said the run
    # "reports why anything was skipped" and it did — to stdout, which is
    # gone the moment the terminal scrolls. The first live runs recorded
    # gate=9/created=1 with no surviving record of WHY, so diagnosing a
    # 30:1 rejection rate meant reasoning from the code rather than reading
    # the run. Counts tell you something is wrong; reasons tell you what.
    detail = (summary.get("skipped_reasons") or []) + [
        f"FINDING: {f}" for f in (summary.get("findings") or [])
    ]
    if detail:
        summary["notes"] += "\n" + "\n".join(detail[:40])
    await lily_arsenal.lily_run_finish(
        supabase,
        run_id=run_id,
        summary=summary,
        status="completed" if summary["status"] in ("completed", "short_of_target")
        else "failed",
    )
    logger.info(
        "LILY_ARSENAL_SEED | RUN_DONE | partition=%s %s ready=%d/%d",
        partition, summary["notes"], final["ready"], tgt,
    )
    return summary


async def lily_seed_all(
    supabase,
    *,
    author,
    imagegen,
    upload,
    classify=None,
    describe=None,
    source_real=None,
    partitions=None,
    target: Optional[int] = None,
    dry_run: bool = False,
    max_slots: Optional[int] = None,
) -> list:
    """Seed every partition in turn. Sequential on purpose: the partitions
    share one provider account and one rate limit, and a seeding job is not
    latency-critical — nobody is waiting on it at a table. Running them in
    parallel would buy minutes and risk 429s across all three at once."""
    results = []
    for partition in (partitions or lily_arsenal.PARTITIONS):
        results.append(
            await lily_seed_partition(
                supabase,
                partition=partition,
                author=author,
                imagegen=imagegen,
                upload=upload,
                classify=classify,
                describe=describe,
                source_real=source_real,
                target=target,
                dry_run=dry_run,
                max_slots=max_slots,
            )
        )
    return results


async def lily_promote_all(supabase, *, partition: Optional[str] = None) -> dict:
    """Bulk operator pass: promote EVERY pending-review ('generating') entry
    to 'ready'. partition=None sweeps every partition.

    Reuses the exact single-entry code path (lily_arsenal_pending_review to
    find them, lily_arsenal_promote to transition each), so the state change
    is identical to running --promote <id> once per entry — just without the
    operator copying UUIDs one at a time."""
    partitions = [partition] if partition else list(lily_arsenal.PARTITIONS)
    summary = {"promoted": 0, "failed": 0, "per_partition": {}}
    for part in partitions:
        pending = await lily_arsenal.lily_arsenal_pending_review(
            supabase, partition=part
        )
        count = 0
        for row in pending:
            ok = await lily_arsenal.lily_arsenal_promote(
                supabase, arsenal_id=row.get("id")
            )
            if ok:
                count += 1
                summary["promoted"] += 1
            else:
                summary["failed"] += 1
        summary["per_partition"][part] = count
    return summary


def lily_format_run_report(results) -> str:
    """The operator-facing run summary (A6): what was created per
    partition, and WHY anything was skipped. Skipped reasons are printed,
    not counted-and-forgotten — 'created 6 of 10' without the four reasons
    is the report that sends someone to the logs."""
    lines = ["", "PICTURE ARSENAL — seeding run summary", "=" * 60]
    total_cost = 0.0
    for r in results or []:
        total_cost += r.get("cost_usd", 0.0)
        lines.append(
            f"\n  {r['partition']}  [{r.get('status')}]  gate={r.get('gate_mode')}"
        )
        lines.append(
            f"    ready {r.get('started_ready', 0)} -> {r.get('final_ready', 0)}"
            f"  (target {r.get('target')})"
            f"  pending_review={r.get('final_pending', 0)}"
        )
        lines.append(
            f"    created={r.get('created', 0)}"
            f"  duplicate={r.get('skipped_duplicate', 0)}"
            f"  moderation={r.get('rejected_moderation', 0)}"
            f"  gate={r.get('rejected_gate', 0)}"
            f"  errors={r.get('errors', 0)}"
        )
        lines.append(
            f"    attempts={r.get('attempts', 0)}"
            f"  moderation_rate={r.get('moderation_rejection_rate', 0):.0%}"
            f"  cost=${r.get('cost_usd', 0.0):.2f}"
            f"  {r.get('duration_seconds', 0):.1f}s"
        )
        for reason in r.get("skipped_reasons") or []:
            lines.append(f"      skipped: {reason}")
        for finding in r.get("findings") or []:
            lines.append(f"      FINDING: {finding}")
    lines.append("")
    lines.append(f"  total run cost: ${total_cost:.2f}")
    lines.append("=" * 60)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Live wiring — only touched by __main__, so importing this module in a test
# never constructs a provider client.
# ---------------------------------------------------------------------------


def _build_supabase():
    from supabase import create_client

    url = lily_config.supabase_url()
    key = lily_config.supabase_service_role_key()
    if not url or not key:
        raise RuntimeError(
            "SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing — the seeding "
            "job writes with the service role."
        )
    return create_client(url, key)


def _build_bindings():
    """Bind the live author / imagegen / upload / classify callables.

    imagegen routes through lily_imagegen.lily_generate_image_bytes — the
    SAME chokepoint live generation uses, with mode and intensity threaded
    exactly as the delivery path threads them. The arsenal deliberately has
    no image-provider code of its own."""
    import lily_images
    import lily_imagegen
    import lily_reasoning

    reasoning = lily_reasoning.LilyReasoning()

    async def imagegen(prompt, partition, intensity):
        return await lily_imagegen.lily_generate_image_bytes(
            prompt,
            aspect_ratio="16:9",
            intensity=intensity or "suggestive",
        )

    async def upload(data, mime, partition):
        return await lily_images.lily_upload_arsenal_image(
            _SUPABASE, data, partition=partition, content_type=mime
        )

    async def author(partition, plan, image_description):
        return await lily_author_question(
            reasoning, partition=partition, plan=plan,
            image_description=image_description,
        )

    async def classify(image_bytes, content_type, claim, brief):
        return await lily_classify_image(
            reasoning, image_bytes=image_bytes, content_type=content_type,
            claim=claim, brief=brief,
        )

    async def describe(image_bytes, content_type):
        return await lily_describe_image(
            reasoning, image_bytes=image_bytes, content_type=content_type,
        )

    import lily_search

    async def source_real(partition, plan):
        # Real-image sourcing for era_or_origin: genuinely-dated archival
        # photos via Exa (same conservative filter + safelist as the live
        # real-entity path). Returns None -> caller falls back to generation.
        return await lily_search.lily_source_period_entry(partition, plan)

    return author, imagegen, upload, classify, describe, source_real


_SUPABASE = None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def lily_preflight() -> dict:
    """What this host can actually do, checked BEFORE anything is spent.

    A seeding run needs four things and fails differently for each missing
    one, so guessing from a traceback is a poor use of an operator's
    evening. In particular a missing XAI_API_KEY does not fail loudly at
    import — it surfaces as the first adult generation raising 'adult image
    provider unconfigured', which the job correctly treats as
    generation-unavailable and halts on. That is the right behaviour and a
    terrible first experience, so it is reported up front instead."""
    def _real(value) -> bool:
        """A credential that is present but obviously a PLACEHOLDER is not
        a credential. Copy-pasting a documented example leaves things like
        `SUPABASE_SERVICE_ROLE_KEY='...'` in the environment, which passes
        a bare truthiness check and then fails at the provider as a bare
        401 — the least informative possible moment to find out. Catch it
        here instead, where the message can say which key."""
        text = str(value or "").strip()
        if len(text) < 12:
            return False
        return not (
            set(text) <= set(".") or text.lower() in {
                "changeme", "your-key-here", "xxx", "todo", "placeholder",
            } or text.startswith("<") and text.endswith(">")
        )

    checks = {
        "supabase": bool(
            _real(lily_config.supabase_url())
            and _real(lily_config.supabase_service_role_key())
        ),
        "google": _real(lily_config.google_api_key()),
        "xai": _real(lily_config.xai_api_key()),
        "exa": _real(lily_config.exa_api_key()),
    }
    checks["can_seed_general"] = checks["supabase"] and checks["google"]
    # Adult partitions render on Grok AND are authored on Grok; the
    # classifier is Gemini either way, so both keys are required.
    checks["can_seed_adult"] = (
        checks["supabase"] and checks["xai"] and checks["google"]
    )
    return checks


def lily_format_preflight(checks: dict) -> str:
    def mark(ok: bool) -> str:
        return "OK     " if ok else "MISSING"

    lines = [
        "",
        "PICTURE ARSENAL — preflight",
        "-" * 52,
        f"  [{mark(checks['supabase'])}] SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY",
        f"  [{mark(checks['google'])}] GOOGLE_API_KEY   (general images + the content gate)",
        f"  [{mark(checks['xai'])}] XAI_API_KEY      (adult images + adult authoring)",
        f"  [{mark(checks['exa'])}] EXA_API_KEY      (optional — real images: real_or_imagined + era_or_origin dating)",
        "",
        f"  general partition:  {'ready to seed' if checks['can_seed_general'] else 'CANNOT SEED'}",
        f"  adult partitions:   {'ready to seed' if checks['can_seed_adult'] else 'CANNOT SEED'}",
        "-" * 52,
    ]
    return "\n".join(lines)


async def _amain(args) -> int:
    global _SUPABASE
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )

    checks = lily_preflight()
    if not checks["supabase"]:
        print(lily_format_preflight(checks))
        print(
            "\nSUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY are required for every\n"
            "mode, including --status. Nothing was attempted."
        )
        return 2
    _SUPABASE = _build_supabase()

    # A real seeding run announces what it can and cannot do before it
    # claims a partition or bills a single image.
    if not (
        args.status or args.review or args.promote or args.reject
        or args.promote_all is not None
    ):
        print(lily_format_preflight(checks))
        wanted = (
            list(lily_arsenal.PARTITIONS)
            if args.partition in (None, "all")
            else [args.partition]
        )
        blocked = [
            p for p in wanted
            if (p in lily_arsenal.ADULT_PARTITIONS and not checks["can_seed_adult"])
            or (p == "general" and not checks["can_seed_general"])
        ]
        if blocked and not args.dry_run:
            print(
                f"\nCannot seed {', '.join(blocked)} — the keys above are "
                "missing. Nothing was attempted for them, nothing was billed."
            )
            if len(blocked) == len(wanted):
                print("There is nothing this host can seed. Set the keys and "
                      "re-run.")
                return 2
            # Seed what we CAN rather than refusing wholesale: a stocked
            # general shelf is worth having even on a night the adult keys
            # are absent, and the job is idempotent so the rest tops up
            # later without duplicating any of this.
            args.partition_filter = [p for p in wanted if p not in blocked]
            print(
                f"Proceeding with: {', '.join(args.partition_filter)}"
            )

    if args.status:
        health = await lily_arsenal.lily_arsenal_health(_SUPABASE)
        print(lily_arsenal.lily_format_health_readout(health))
        for line in lily_arsenal.lily_arsenal_low_warnings(health):
            logger.warning(line)
        return 0 if health.get("healthy") else 1

    if args.review:
        pending = await lily_arsenal.lily_arsenal_pending_review(
            _SUPABASE, partition=args.review
        )
        if not pending:
            print(f"no entries awaiting review in {args.review}")
            return 0
        print(f"{len(pending)} entries awaiting review in {args.review}:\n")
        for row in pending:
            print(f"  id={row.get('id')}")
            print(f"    format={row.get('format')} "
                  f"subject={row.get('subject_area')} "
                  f"tier={row.get('difficulty_tier')}")
            print(f"    Q: {row.get('question_text')}")
            print(f"    A: {row.get('canonical_answer')}")
            print(f"    image: {row.get('image_storage_path')}\n")
        print("promote with:  python3 -m lily_arsenal_seed --promote <id>")
        return 0

    if args.promote:
        ok = await lily_arsenal.lily_arsenal_promote(
            _SUPABASE, arsenal_id=args.promote
        )
        print("promoted" if ok else "promote failed")
        return 0 if ok else 1

    if args.promote_all is not None:
        part = None if args.promote_all == _PROMOTE_ALL_EVERY else args.promote_all
        if part is not None and part not in lily_arsenal.PARTITIONS:
            print(
                f"unknown partition '{part}' — choose from "
                f"{', '.join(lily_arsenal.PARTITIONS)}, or pass --promote-all "
                "with no argument to sweep every partition"
            )
            return 2
        summary = await lily_promote_all(_SUPABASE, partition=part)
        scope = part or "all partitions"
        detail = ", ".join(
            f"{k}={v}" for k, v in summary["per_partition"].items()
        )
        noun = "entry" if summary["promoted"] == 1 else "entries"
        print(
            f"promoted {summary['promoted']} pending-review {noun} to ready "
            f"({scope}: {detail})"
            + (f"; {summary['failed']} failed" if summary["failed"] else "")
        )
        return 0 if summary["failed"] == 0 else 1

    if args.reject:
        ok = await lily_arsenal.lily_arsenal_reject(
            _SUPABASE, arsenal_id=args.reject, reason=args.reason or "operator reject"
        )
        print("rejected" if ok else "reject failed")
        return 0 if ok else 1

    partitions = getattr(args, "partition_filter", None) or (
        list(lily_arsenal.PARTITIONS)
        if args.partition in (None, "all")
        else [args.partition]
    )
    author = imagegen = upload = classify = describe = source_real = None
    if not args.dry_run:
        (author, imagegen, upload, classify, describe,
         source_real) = _build_bindings()
    else:
        async def _noop(*a, **k):
            return None
        author = imagegen = upload = classify = describe = source_real = _noop

    results = await lily_seed_all(
        _SUPABASE,
        author=author, imagegen=imagegen, upload=upload, classify=classify,
        describe=describe, source_real=source_real,
        partitions=partitions, target=args.depth, dry_run=args.dry_run,
        max_slots=args.max_slots,
    )
    print(lily_format_run_report(results))
    health = await lily_arsenal.lily_arsenal_health(_SUPABASE)
    print(lily_arsenal.lily_format_health_readout(health))
    # Non-zero when any partition finished short — a seeding run that did
    # not stock the shelf must not look like a success to a caller.
    return 0 if all(
        r.get("status") in ("completed", "dry_run") for r in results
    ) else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Seed and inspect the standing picture arsenal.",
    )
    parser.add_argument(
        "--partition", choices=[*lily_arsenal.PARTITIONS, "all"], default="all"
    )
    parser.add_argument(
        "--depth", type=int, default=None,
        help="target ready entries per partition (default: config)",
    )
    parser.add_argument(
        "--max-slots", type=int, default=None, dest="max_slots",
        help="cap generations this run (a cheap first taste of a partition)",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--status", action="store_true", help="print the bank health readout"
    )
    parser.add_argument(
        "--review", metavar="PARTITION", help="list entries awaiting review"
    )
    parser.add_argument("--promote", metavar="ID", help="promote one entry to ready")
    parser.add_argument(
        "--promote-all", metavar="PARTITION", nargs="?",
        const=_PROMOTE_ALL_EVERY, default=None, dest="promote_all",
        help="promote ALL pending-review entries in PARTITION to ready "
        "(omit PARTITION to sweep every partition)",
    )
    parser.add_argument("--reject", metavar="ID", help="reject one entry")
    parser.add_argument("--reason", default=None)
    args = parser.parse_args()
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    sys.exit(main())
