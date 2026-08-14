"""Consolidate fragmented returning-player identity groups
(WO-PRMPT-LILY-GROUP-RECONCILE-001, Deliverable 2).

One INDIVIDUAL who is never recognized fragments across many groups (prod:
31 voiceprint groups / 7 memory groups for a single player). This script
finds every SINGLE-player group that shares one sole player name and folds
the fragments into a canonical group via lily_persistence.lily_merge_groups.

INDIVIDUAL-LEVEL by construction: only SINGLE-player groups (a group whose
memory lists exactly one distinct name) are eligible, and they cluster by
that sole name. A group that ever played multi-player is a different
COLLECTION and is left alone — never merged into an individual on a name.

Canonical pick per cluster: richest memory (largest summed question_count),
tie-break most-recent play.

  --dry-run   (DEFAULT) read-only; prints and saves the full merge plan.
  --execute   explicit opt-in; actually runs lily_merge_groups per cluster.

DB connection is sourced exactly like the rest of the repo:
lily_persistence.lily_create_supabase_client() (SUPABASE_URL +
SUPABASE_SERVICE_ROLE_KEY via lily_config; anon/service-role, JWT never
enabled). Run --dry-run first; the coordinator runs --execute after review.
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lily_persistence  # noqa: E402

# Group-keyed tables whose rows carry a group_id (counted per fragment for the
# plan). Session-keyed tables have no group_id and follow lily_sessions, so
# they are reported as a session count, not a rekey.
GROUP_KEYED_COUNT_TABLES = (
    "lily_sessions",
    "lily_memories",
    "lily_group_facts",
    "lily_asked_history",
    "lily_speaker_voiceprints",
    "lily_group_prefs",
    "lily_voice_identity",
)

DEFAULT_PLAN_PATH = os.path.expanduser(
    "~/lily-evidence/group-reconcile-001/dry_run_plan.json"
)


def _casefold_set(names):
    return {str(n).strip().casefold() for n in (names or []) if str(n).strip()}


async def _load_all_memories(supabase):
    """Page through lily_memories (PostgREST caps at 1000 rows/response)."""
    rows = []
    page = 0
    size = 1000
    while True:
        start = page * size
        res = await asyncio.to_thread(
            lambda s=start: supabase.table("lily_memories")
            .select("group_id, player_names, played_at, question_count")
            .order("played_at", desc=True)
            .range(s, s + size - 1)
            .execute()
        )
        batch = res.data or []
        rows.extend(batch)
        if len(batch) < size:
            break
        page += 1
    return rows


def _build_single_player_index(memory_rows):
    """group_id -> {names:set(casefold), display:str, q_sum:int, latest:str}
    for groups whose memory lists exactly ONE distinct player name."""
    agg = {}
    for row in memory_rows:
        gid = row.get("group_id")
        if not gid:
            continue
        names = [str(n).strip() for n in (row.get("player_names") or []) if str(n).strip()]
        entry = agg.setdefault(
            gid, {"names": set(), "display": None, "q_sum": 0, "latest": ""}
        )
        entry["names"].update(_casefold_set(names))
        if names and entry["display"] is None:
            entry["display"] = names[0]
        entry["q_sum"] += int(row.get("question_count") or 0)
        pa = str(row.get("played_at") or "")
        if pa > entry["latest"]:
            entry["latest"] = pa
    # Keep only single-player groups.
    return {
        gid: e for gid, e in agg.items()
        if len(e["names"]) == 1 and e["display"]
    }


def _cluster_by_name(single_player):
    """sole-name(casefold) -> [group entries], for names with >1 group."""
    clusters = {}
    for gid, e in single_player.items():
        name = next(iter(e["names"]))
        clusters.setdefault(name, []).append({"group_id": gid, **e})
    return {name: groups for name, groups in clusters.items() if len(groups) > 1}


def _pick_canonical(groups):
    """Richest memory (q_sum), tie-break most-recent play."""
    return sorted(
        groups, key=lambda g: (g["q_sum"], g["latest"]), reverse=True
    )[0]


async def _count(supabase, table, group_id):
    try:
        res = await asyncio.to_thread(
            lambda: supabase.table(table)
            .select("*", count="exact").eq("group_id", group_id).limit(1)
            .execute()
        )
        return int(getattr(res, "count", None) or 0)
    except Exception as e:
        if lily_persistence.lily_forget.lily_is_absent_table_error(str(e)):
            return None  # table not yet migrated
        return f"error:{type(e).__name__}"


async def _session_ids(supabase, group_id):
    try:
        res = await asyncio.to_thread(
            lambda: supabase.table("lily_sessions")
            .select("session_id").eq("group_id", group_id).execute()
        )
        return [r.get("session_id") for r in (res.data or []) if r.get("session_id")]
    except Exception:
        return []


async def build_plan(supabase):
    memory_rows = await _load_all_memories(supabase)
    single_player = _build_single_player_index(memory_rows)
    clusters = _cluster_by_name(single_player)

    plan = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "dry-run",
        "source": "lily_memories (single-player groups, clustered by sole name)",
        "clusters": [],
        "summary": {},
    }
    total_groups_to_merge = 0
    for name in sorted(clusters):
        groups = clusters[name]
        canonical = _pick_canonical(groups)
        dups = [g for g in groups if g["group_id"] != canonical["group_id"]]
        per_group_counts = {}
        rekey_totals = {t: 0 for t in GROUP_KEYED_COUNT_TABLES}
        for g in dups:
            gid = g["group_id"]
            counts = {}
            for table in GROUP_KEYED_COUNT_TABLES:
                c = await _count(supabase, table, gid)
                counts[table] = c
                if isinstance(c, int):
                    rekey_totals[table] += c
            sids = await _session_ids(supabase, gid)
            counts["session_ids"] = len(sids)
            per_group_counts[gid] = counts
        total_groups_to_merge += len(dups)
        plan["clusters"].append({
            "player_name": canonical["display"],
            "player_name_key": name,
            "canonical_group_id": canonical["group_id"],
            "canonical_pick": (
                f"richest_memory q_sum={canonical['q_sum']} "
                f"latest={canonical['latest']} "
                f"(tie-break most-recent play)"
            ),
            "duplicate_group_ids": [g["group_id"] for g in dups],
            "planned_rekey_totals": rekey_totals,
            "per_group_counts": per_group_counts,
            "session_keyed_retained": list(
                lily_persistence.MERGE_SESSION_KEYED_RETAINED
            ),
        })
    plan["summary"] = {
        "clusters": len(plan["clusters"]),
        "groups_to_merge": total_groups_to_merge,
        "names": [c["player_name"] for c in plan["clusters"]],
        "single_player_groups_scanned": len(single_player),
        "memory_rows_scanned": len(memory_rows),
    }
    return plan


async def execute_plan(supabase, plan):
    results = []
    for cluster in plan["clusters"]:
        canonical = cluster["canonical_group_id"]
        dups = cluster["duplicate_group_ids"]
        if not dups:
            continue
        res = await lily_persistence.lily_merge_groups(
            supabase, canonical, dups,
            reason=f"consolidation_script:{cluster['player_name_key']}",
        )
        results.append(res)
        print(
            f"MERGED name={cluster['player_name']} canonical={canonical} "
            f"duplicates={len(dups)} ok={res.get('ok')}"
        )
    return results


async def _amain():
    ap = argparse.ArgumentParser(description=__doc__)
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", default=True,
                       help="(default) read-only; print & save the merge plan")
    group.add_argument("--execute", action="store_true",
                       help="ACTUALLY merge — explicit opt-in only")
    ap.add_argument("--out", default=DEFAULT_PLAN_PATH,
                    help=f"plan JSON path (default {DEFAULT_PLAN_PATH})")
    args = ap.parse_args()

    supabase = lily_persistence.lily_create_supabase_client()
    if supabase is None:
        print("FATAL: no Supabase client (set SUPABASE_URL + "
              "SUPABASE_SERVICE_ROLE_KEY)", file=sys.stderr)
        return 2

    plan = await build_plan(supabase)

    if args.execute:
        plan["mode"] = "execute"
        print(f"EXECUTE: {plan['summary']['groups_to_merge']} group(s) across "
              f"{plan['summary']['clusters']} cluster(s) will be merged.")
        await execute_plan(supabase, plan)
        print("EXECUTE complete.")
        return 0

    # Dry-run (default): print + persist the plan, mutate nothing.
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as fh:
        json.dump(plan, fh, indent=2, default=str)
    print(json.dumps(plan, indent=2, default=str))
    print(f"\nDRY-RUN plan written to {args.out}", file=sys.stderr)
    print(
        f"clusters={plan['summary']['clusters']} "
        f"groups_to_merge={plan['summary']['groups_to_merge']}",
        file=sys.stderr,
    )
    return 0


def main():
    raise SystemExit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
