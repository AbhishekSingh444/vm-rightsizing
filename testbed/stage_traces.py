#!/usr/bin/env python3
"""
stage_traces.py -- extract the evaluation-window slice for the cluster subset
and, optionally, scale the replayed demand.

The replay agent (testbed/replay_agent.py) reads whatever CSV it is handed
under /trace/<vm_id>.csv and reproduces "CPU usage [MHZ]" and "Memory usage
[KB]" verbatim as demand. Handing it the full 30-day file would replay four
times more than the evaluation window (see TASKS.md T6); handing it unscaled
demand against a scaled-down pod limit would change every demand-to-limit
ratio and invalidate the offline predictions the cluster run is meant to
validate.

This script does both pieces in one place so they cannot drift apart:
  1. slices each subset VM's raw trace down to the evaluation window, using
     the same wall-clock split as baselines.py (split_obs_eval), and
  2. multiplies "CPU usage [MHZ]" and "Memory usage [KB]" by --scale.

Scaling only the demand columns (not "CPU cores" / "*capacity provisioned*")
is deliberate: replay_agent.py derives demand_cores from
use_mhz / (cap_mhz / cores), so scaling use_mhz by S scales demand_cores by
exactly S without touching the per-core-MHz normalisation. Memory demand is
mem_kb / 1024 directly, so scaling mem_kb by S scales it by exactly S. The
pod's limit (set separately by gen_manifests.py --scale) must use the same S
for the demand-to-limit ratio to be preserved -- that is the property T8
depends on.

Usage:
    python3 testbed/stage_traces.py --subset results/subset_50.csv \\
        --traces data/fastStorage --out data/traces \\
        --obs-days 21 --eval-days 9 --scale 0.0625
"""

import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import baselines as bl  # noqa: E402

COL_USE_MHZ = "CPU usage [MHZ]"
COL_MEM_USE_KB = "Memory usage [KB]"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--subset", required=True, help="subset CSV with a vm_id column")
    ap.add_argument("--traces", default="data/fastStorage", help="raw per-VM CSV directory")
    ap.add_argument("--out", default="data/traces", help="directory to write sliced/scaled CSVs")
    ap.add_argument("--obs-days", type=float, default=21.0)
    ap.add_argument("--eval-days", type=float, default=9.0)
    ap.add_argument("--scale", type=float, default=1.0,
                     help="factor applied to CPU usage [MHZ] and Memory usage [KB] "
                          "(the demand columns); pass the SAME factor to "
                          "gen_manifests.py --scale so demand/limit ratios are preserved")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)

    vm_ids = []
    with open(args.subset, newline="") as fh:
        for row in csv.DictReader(fh):
            vm_ids.append(row["vm_id"])
    vm_ids = sorted(set(vm_ids), key=lambda x: int(x) if x.isdigit() else x)

    written = 0
    for vm_id in vm_ids:
        src_path = os.path.join(args.traces, f"{vm_id}.csv")
        if not os.path.exists(src_path):
            print(f"  skip {vm_id}: {src_path} not found", file=sys.stderr)
            continue

        with open(src_path, newline="") as fh:
            reader = csv.reader(fh, delimiter=";")
            header = [h.strip().strip('"') for h in next(reader)]
            idx = {name: i for i, name in enumerate(header)}
            missing = [c for c in [bl.COL_TS, COL_USE_MHZ, COL_MEM_USE_KB] if c not in idx]
            if missing:
                print(f"  skip {vm_id}: missing columns {missing}", file=sys.stderr)
                continue

            t0 = None
            obs_end = None
            eval_end = None
            rows_out = []
            ts_all = []
            for parts in reader:
                if len(parts) < len(header):
                    continue
                try:
                    ts = float(parts[idx[bl.COL_TS]])
                except ValueError:
                    continue
                ts_all.append((ts, parts))

            if not ts_all:
                print(f"  skip {vm_id}: no usable rows", file=sys.stderr)
                continue

            t0 = ts_all[0][0]
            obs_end = t0 + args.obs_days * 86400.0
            eval_end = obs_end + args.eval_days * 86400.0

            for ts, parts in ts_all:
                if obs_end <= ts < eval_end:
                    parts = list(parts)
                    parts[idx[COL_USE_MHZ]] = str(float(parts[idx[COL_USE_MHZ]]) * args.scale)
                    parts[idx[COL_MEM_USE_KB]] = str(float(parts[idx[COL_MEM_USE_KB]]) * args.scale)
                    rows_out.append(parts)

        if not rows_out:
            print(f"  skip {vm_id}: evaluation window empty (trace too short?)", file=sys.stderr)
            continue

        out_path = os.path.join(args.out, f"{vm_id}.csv")
        with open(out_path, "w", newline="") as out:
            writer = csv.writer(out, delimiter=";")
            writer.writerow(header)
            writer.writerows(rows_out)
        written += 1

    print(f"staged {written}/{len(vm_ids)} VMs into {args.out}/ "
          f"(evaluation window only, demand scaled by {args.scale})")


if __name__ == "__main__":
    main()
