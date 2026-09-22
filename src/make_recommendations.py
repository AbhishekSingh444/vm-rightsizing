#!/usr/bin/env python3
"""
make_recommendations.py -- bridge the offline pipeline to the cluster testbed.

baselines.py writes per_vm_results.csv (one row per VM per method, with the
allocation it chose and the violations that followed). gen_manifests.py wants
recommendations.csv (vm_id, method, rec_cores, rec_mem_gb). This converts one
to the other.

It also picks the cluster subset. That choice matters: 50 randomly sampled VMs
from a fleet that is mostly idle will almost all be quiet, and a cluster run
where nothing happens proves nothing. So the subset is STRATIFIED -- half the
VMs are ones the offline replay predicts WILL violate, half are ones it
predicts will not. If the cluster agrees with the prediction on both groups,
that is a real validation of the offline violation definition.

Usage:
    python3 make_recommendations.py --results results/per_vm_results.csv \\
        --out results/recommendations.csv

    python3 make_recommendations.py --results results/per_vm_results.csv \\
        --out results/recommendations.csv \\
        --subset 50 --subset-method p95 --subset-out results/subset_50.csv
"""

import argparse
import os

import pandas as pd

REQUIRED = ["vm_id", "method", "alloc_cores", "alloc_mem_gb"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", required=True, help="per_vm_results.csv from baselines.py")
    ap.add_argument("--out", required=True, help="recommendations.csv to write")
    ap.add_argument("--subset", type=int, default=0,
                    help="also write a stratified cluster subset of this size")
    ap.add_argument("--subset-method", default="p95",
                    help="which method's predictions to stratify on")
    ap.add_argument("--subset-out", default="")
    ap.add_argument("--subset-strategy", choices=["any", "resource"], default="any",
                    help="'any' stratifies on any_violation only (may starve one "
                         "resource by chance); 'resource' stratifies on the "
                         "CPU/memory violation cross so both confusion tables are "
                         "meaningful")
    ap.add_argument("--subset-cpu-only", type=int, default=10,
                    help="resource strategy: VMs with CPU violation, no mem violation")
    ap.add_argument("--subset-both", type=int, default=15,
                    help="resource strategy: VMs with both CPU and mem violation")
    ap.add_argument("--subset-mem-only", type=int, default=15,
                    help="resource strategy: VMs with mem violation, no CPU violation")
    ap.add_argument("--subset-safe", type=int, default=10,
                    help="resource strategy: VMs with neither violation")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    df = pd.read_csv(args.results)
    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        raise SystemExit(f"{args.results} missing columns: {missing}")

    recs = df[["vm_id", "method", "alloc_cores", "alloc_mem_gb"]].rename(
        columns={"alloc_cores": "rec_cores", "alloc_mem_gb": "rec_mem_gb"}
    )
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    recs.to_csv(args.out, index=False)

    print(f"wrote {len(recs)} recommendations to {args.out}")
    for method, group in recs.groupby("method"):
        print(f"  {method:16s} {len(group)} VMs")

    if not args.subset:
        return

    if "any_violation" not in df.columns:
        raise SystemExit("results file has no any_violation column; cannot stratify")
    if "idle" not in df.columns:
        raise SystemExit("results file has no idle column; cannot exclude idle VMs")

    # The cluster subset is drawn from non-idle VMs only. An idle VM replays
    # ~45 minutes of near-zero demand and proves nothing either way -- it
    # would pad the subset without exercising the thing T8 is checking.
    sub_src = df[(df["method"] == args.subset_method) & (~df["idle"])]
    if sub_src.empty:
        raise SystemExit(f"no non-idle rows for method '{args.subset_method}'")

    if args.subset_strategy == "any":
        will_violate = sub_src[sub_src["any_violation"]]
        will_not = sub_src[~sub_src["any_violation"]]

        half = args.subset // 2
        take_v = min(half, len(will_violate))
        take_s = min(args.subset - take_v, len(will_not))

        picked = pd.concat(
            [
                will_violate.sample(n=take_v, random_state=args.seed) if take_v else will_violate.head(0),
                will_not.sample(n=take_s, random_state=args.seed) if take_s else will_not.head(0),
            ]
        )
    else:
        # 'any_violation' is the OR of CPU and memory violations. Stratifying
        # on it alone lets a random draw starve one resource by chance -- the
        # subset must be meaningful for BOTH per-resource confusion tables
        # (T8 compares CPU against throttling and memory against OOMKills
        # separately), so cross-stratify on the CPU x memory violation grid.
        cpu_v = sub_src["cpu_events"] > 0
        mem_v = sub_src["mem_events"] > 0

        strata = {
            "cpu_only": (sub_src[cpu_v & ~mem_v], args.subset_cpu_only),
            "both": (sub_src[cpu_v & mem_v], args.subset_both),
            "mem_only": (sub_src[~cpu_v & mem_v], args.subset_mem_only),
            "safe": (sub_src[~cpu_v & ~mem_v], args.subset_safe),
        }

        parts = []
        for name, (pool, want) in strata.items():
            take = min(want, len(pool))
            if take < want:
                print(f"  NOTE: stratum '{name}' has only {len(pool)} eligible VMs, "
                      f"wanted {want}")
            parts.append(pool.sample(n=take, random_state=args.seed) if take else pool.head(0))
        picked = pd.concat(parts)

    out_path = args.subset_out or args.out.replace(".csv", f"_subset{args.subset}.csv")
    picked_recs = picked[["vm_id", "method", "alloc_cores", "alloc_mem_gb"]].rename(
        columns={"alloc_cores": "rec_cores", "alloc_mem_gb": "rec_mem_gb"}
    )
    picked_recs.to_csv(out_path, index=False)

    # The prediction is what the cluster run will be checked against, and it
    # must be checkable per resource: T8 compares CPU against throttling
    # records and memory against OOMKills separately, not against a single
    # combined flag. cpu_violation / mem_violation are the explicit
    # per-resource booleans; any_violation and the raw event/exhausted-minute
    # counts are kept alongside for the combined view and for severity.
    pred_path = out_path.replace(".csv", "_predictions.csv")
    predictions = picked[
        [
            "vm_id", "method", "any_violation",
            "cpu_events", "cpu_exhausted_minutes",
            "mem_events", "mem_exhausted_minutes",
        ]
    ].copy()
    predictions.insert(3, "cpu_violation", predictions["cpu_events"] > 0)
    predictions.insert(4, "mem_violation", predictions["mem_events"] > 0)
    predictions.to_csv(pred_path, index=False)

    print(f"\nstratified subset ({args.subset_method}, non-idle VMs only, "
          f"{len(sub_src)} eligible, strategy={args.subset_strategy}):")
    if args.subset_strategy == "any":
        print(f"  predicted to violate : {take_v}")
        print(f"  predicted safe       : {take_s}")
        if take_v < half:
            print(f"  NOTE: only {len(will_violate)} non-idle VMs are predicted to violate "
                  f"under '{args.subset_method}'. Subset is unbalanced; consider a more "
                  f"aggressive method or a smaller subset.")
    else:
        cpu_pos = int(predictions["cpu_violation"].sum())
        mem_pos = int(predictions["mem_violation"].sum())
        print(f"  CPU predicted-violate : {cpu_pos}  /  predicted-safe : {len(picked) - cpu_pos}")
        print(f"  mem predicted-violate : {mem_pos}  /  predicted-safe : {len(picked) - mem_pos}")
    print(f"  wrote {out_path}")
    print(f"  wrote {pred_path}  <- per-VM cpu_violation/mem_violation, compare "
          f"against the cluster's OOMKills (memory) and throttling records (CPU)")


if __name__ == "__main__":
    main()
