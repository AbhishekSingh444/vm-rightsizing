#!/usr/bin/env python3
"""
inspect_traces.py -- preflight the real GWA-T-12 download before you build anything on it.

Answers the questions that decide your split, and produces a report worth
screenshotting for the testbed evidence section:

  - how many VM files are there, and how many are usable
  - how many samples does each cover, and what is the real sampling interval
  - what is the actual date span (this determines your 21/9 split)
  - how dirty is the memory column
  - how much headroom exists (allocated vs used) -- the motivating number

Usage:
    python3 inspect_traces.py --traces ./fastStorage
    python3 inspect_traces.py --traces ./fastStorage --sample 100
"""

import argparse
import datetime as dt
import glob
import os

import numpy as np
import pandas as pd

from baselines import (
    CAPACITY_GLITCH_FACTOR,
    CAPACITY_SHIFT_FACTOR,
    FLAVOUR_MEM_GB,
    FLAVOUR_VCPU,
    capacity_glitch_ratio,
    detect_capacity_shift,
    is_capacity_glitch,
    split_obs_eval,
)

COL_TS = "Timestamp [ms]"
COL_CORES = "CPU cores"
COL_CAP_MHZ = "CPU capacity provisioned [MHZ]"
COL_USE_MHZ = "CPU usage [MHZ]"
COL_MEM_CAP_KB = "Memory capacity provisioned [KB]"
COL_MEM_USE_KB = "Memory usage [KB]"


def read_trace(path):
    """Read one Bitbrains CSV. The real files use ';\\t' as the separator."""
    df = pd.read_csv(path, sep=r";\s*", engine="python")
    df.columns = [c.strip().strip('"').strip() for c in df.columns]
    return df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--traces", required=True)
    ap.add_argument("--sample", type=int, default=0,
                    help="inspect only the first N files (0 = all)")
    ap.add_argument("--report", default="dataset_report.csv")
    ap.add_argument("--glitch-report", default="capacity_glitch_report.csv",
                     help="where to write flagged (not excluded) capacity-glitch VMs")
    ap.add_argument("--shift-report", default="capacity_shift_report.csv",
                     help="where to write flagged (not excluded) mid-trace capacity-shift VMs")
    ap.add_argument("--obs-days", type=float, default=21.0,
                    help="must match baselines.py --obs-days, so 'usable' here "
                         "agrees with 'usable' there")
    ap.add_argument("--eval-days", type=float, default=9.0,
                    help="must match baselines.py --eval-days")
    args = ap.parse_args()

    paths = sorted(glob.glob(os.path.join(args.traces, "*.csv")))
    if args.sample:
        paths = paths[: args.sample]
    if not paths:
        raise SystemExit(f"no CSV files found in {args.traces}")

    print("=" * 78)
    print("GWA-T-12 DATASET PREFLIGHT")
    print("=" * 78)
    print(f"directory : {os.path.abspath(args.traces)}")
    print(f"files     : {len(paths)}")

    with open(paths[0]) as fh:
        first_line = fh.readline().rstrip("\n")
    print(f"\nheader of {os.path.basename(paths[0])}:")
    print(f"  {first_line[:150]}")
    print(f"  tab after semicolon: {';\t' in first_line}")

    rows = []
    bad = []
    glitches = []
    shifts = []
    for path in paths:
        name = os.path.basename(path)
        try:
            df = read_trace(path)
        except Exception as exc:
            bad.append((name, f"unreadable: {exc}"))
            continue

        # Same "needed" set as baselines.load_vm -- COL_TS is required here
        # too, both because interval/span depend on it and because the
        # obs+eval span check below (mirroring baselines.split_obs_eval)
        # needs it.
        needed = [COL_TS, COL_CORES, COL_CAP_MHZ, COL_USE_MHZ, COL_MEM_CAP_KB, COL_MEM_USE_KB]
        missing = [c for c in needed if c not in df.columns]
        if missing:
            bad.append((name, f"missing {missing}"))
            continue

        for c in needed:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.dropna(subset=needed)
        if df.empty:
            bad.append((name, "no numeric rows"))
            continue

        cores = float(df[COL_CORES].iloc[0])
        cap_mhz = float(df[COL_CAP_MHZ].iloc[0])
        mem_cap_gb = float(df[COL_MEM_CAP_KB].iloc[0]) / (1024 * 1024)

        # Capacity-glitch check -- see docs/DATA_QUALITY.md. Flags, does not
        # exclude: a VM whose first sample is >10x off its own file median on
        # a capacity-provisioned column is corrupted at that sample, not
        # actually resized. Run on every file regardless of the hard gates
        # below, so this is a census across the full 1250, not just the
        # already-usable subset.
        cpu_cap_ratio = capacity_glitch_ratio(df[COL_CAP_MHZ])
        mem_cap_ratio = capacity_glitch_ratio(df[COL_MEM_CAP_KB])
        cpu_cap_glitch = is_capacity_glitch(cpu_cap_ratio)
        mem_cap_glitch = is_capacity_glitch(mem_cap_ratio)
        if cpu_cap_glitch or mem_cap_glitch:
            glitches.append(
                {
                    "file": name,
                    "cpu_cap_glitch": cpu_cap_glitch,
                    "cpu_cap_ratio": cpu_cap_ratio,
                    "mem_cap_glitch": mem_cap_glitch,
                    "mem_cap_ratio": mem_cap_ratio,
                }
            )

        # Mid-trace capacity-shift check -- see docs/DATA_QUALITY.md. Also
        # flags, does not exclude, and also runs on every file regardless of
        # the hard gates below. Distinct from the glitch check above: this
        # looks for two (or more) each-sustained values across the file --
        # e.g. a VM genuinely resized partway through -- rather than a
        # single bad sample.
        cpu_shift = detect_capacity_shift(df[COL_CAP_MHZ])
        mem_shift = detect_capacity_shift(df[COL_MEM_CAP_KB])
        if cpu_shift or mem_shift:
            shifts.append(
                {
                    "file": name,
                    "cpu_shift": cpu_shift is not None,
                    "cpu_low": cpu_shift["low_level"] if cpu_shift else None,
                    "cpu_high": cpu_shift["high_level"] if cpu_shift else None,
                    "cpu_ratio": cpu_shift["ratio"] if cpu_shift else None,
                    "mem_shift": mem_shift is not None,
                    "mem_low_gb": (mem_shift["low_level"] / (1024 * 1024)) if mem_shift else None,
                    "mem_high_gb": (mem_shift["high_level"] / (1024 * 1024)) if mem_shift else None,
                    "mem_ratio": mem_shift["ratio"] if mem_shift else None,
                }
            )

        # Same gate as baselines.load_vm: a zero/negative first-sample
        # cores or provisioned-MHz value makes per-core MHz undefined, and
        # baselines.py skips the VM entirely rather than guess. Preflight
        # must skip it too, or "usable" here won't agree with "usable" in
        # the baseline run downstream.
        if cores <= 0 or cap_mhz <= 0:
            bad.append((name, "bad CPU capacity (cores or provisioned MHz <= 0 on first sample)"))
            continue

        # Same gate as baselines.load_vm (decided, docs/DATA_QUALITY.md
        # pattern 4): a VM already provisioned below the smallest flavour in
        # the catalog cannot be right-sized -- no smaller entry exists to
        # snap to.
        if cores < min(FLAVOUR_VCPU) or mem_cap_gb < min(FLAVOUR_MEM_GB):
            bad.append((name, "below flavour catalog floor (see docs/DATA_QUALITY.md pattern 4)"))
            continue

        # Same gate as baselines.load_vm (decided, docs/DATA_QUALITY.md
        # pattern 3): a VM whose provisioned capacity changes mid-trace is
        # not one right-sizeable entity, so it is excluded here too, not
        # just flagged.
        if cpu_shift or mem_shift:
            bad.append((name, "mid-trace capacity shift (see docs/DATA_QUALITY.md pattern 3)"))
            continue

        # Same gate as baselines.split_obs_eval: skip VMs that don't run
        # long enough to cover the full observation+evaluation window.
        split = split_obs_eval(pd.DataFrame({"ts": df[COL_TS].to_numpy()}), args.obs_days, args.eval_days)
        if split is None:
            bad.append(
                (name, f"short span (< {args.obs_days:g}+{args.eval_days:g} obs+eval days)")
            )
            continue

        per_core = cap_mhz / cores

        cpu_util = df[COL_USE_MHZ] / cap_mhz
        mem_util = (
            df[COL_MEM_USE_KB] / (mem_cap_gb * 1024 * 1024) if mem_cap_gb > 0
            else pd.Series(dtype=float)
        )

        interval_s = np.nan
        span_days = np.nan
        if len(df) > 1:
            ts = df[COL_TS].to_numpy()
            diffs = np.diff(ts)
            diffs = diffs[diffs > 0]
            if len(diffs):
                interval_s = float(np.median(diffs))
                span_days = (ts[-1] - ts[0]) / 86400.0

        rows.append(
            {
                "file": name,
                "samples": len(df),
                "interval_s": interval_s,
                "span_days": span_days,
                "cores": cores,
                "per_core_mhz": per_core,
                "mem_cap_gb": mem_cap_gb,
                "cpu_mean_util": cpu_util.mean(),
                "cpu_p95_util": cpu_util.quantile(0.95),
                "cpu_max_util": cpu_util.max(),
                "mem_mean_util": mem_util.mean(),
                "mem_p95_util": mem_util.quantile(0.95),
                "mem_max_util": mem_util.max(),
                "mem_pinned": bool(mem_util.min() > 0.98) if len(mem_util) else False,
                "mem_near_zero": bool(mem_util.max() < 0.02) if len(mem_util) else False,
                "cpu_over_100": bool(cpu_util.max() > 1.0) if len(cpu_util) else False,
            }
        )

    if not rows:
        raise SystemExit("no usable files")

    rep = pd.DataFrame(rows)
    rep.to_csv(args.report, index=False)

    print("\n" + "-" * 78)
    print("COVERAGE")
    print("-" * 78)
    print(f"usable files      : {len(rep)}")
    print(f"excluded          : {len(bad)}")

    reason_order = [
        "unreadable", "missing", "no numeric rows", "bad CPU capacity",
        "below flavour catalog floor", "mid-trace capacity shift", "short span",
    ]

    def reason_key(why):
        for r in reason_order:
            if why.startswith(r):
                return r
        return why

    reason_counts = {}
    for _, why in bad:
        key = reason_key(why)
        reason_counts[key] = reason_counts.get(key, 0) + 1
    for r in reason_order:
        if reason_counts.get(r):
            print(f"    {r:<26s}: {reason_counts[r]}")

    print("\n  examples:")
    for name, why in bad[:5]:
        print(f"    {name}: {why}")
    if len(bad) > 5:
        print(f"    ... and {len(bad) - 5} more")

    med_int = rep["interval_s"].median()
    print(f"\nmedian sampling interval : {med_int:.0f} s ({med_int/60:.1f} min)")
    print(f"samples per file         : min {rep['samples'].min()}, "
          f"median {int(rep['samples'].median())}, max {rep['samples'].max()}")
    print(f"span in days             : min {rep['span_days'].min():.1f}, "
          f"median {rep['span_days'].median():.1f}, max {rep['span_days'].max():.1f}")

    if np.isfinite(med_int) and med_int > 0:
        per_day = 86400 / med_int
        median_days = rep["span_days"].median()
        print(f"\n=> samples per day at this interval: {per_day:.0f}")
        print(f"=> a 70/30 split of the median span is "
              f"{median_days*0.7:.0f} obs days / {median_days*0.3:.0f} eval days")
        print("   ADJUST YOUR --obs-days / --eval-days TO MATCH. Do not assume 21/9.")

    print("\n" + "-" * 78)
    print("DATA QUALITY FLAGS")
    print("-" * 78)
    print(f"memory pinned at provisioned (suspect) : {int(rep['mem_pinned'].sum())}")
    print(f"memory effectively zero (suspect)      : {int(rep['mem_near_zero'].sum())}")
    print(f"CPU usage exceeding provisioned        : {int(rep['cpu_over_100'].sum())}")
    print("\nThese are the VMs whose cleaning rule you must state in the paper.")

    print("\n" + "-" * 78)
    print("CAPACITY GLITCH FLAGS (flagged, not excluded -- see docs/DATA_QUALITY.md)")
    print("-" * 78)
    n_cpu_glitch = sum(1 for g in glitches if g["cpu_cap_glitch"])
    n_mem_glitch = sum(1 for g in glitches if g["mem_cap_glitch"])
    print(f"first-sample CPU capacity >{CAPACITY_GLITCH_FACTOR:g}x off file median    : {n_cpu_glitch}")
    print(f"first-sample memory capacity >{CAPACITY_GLITCH_FACTOR:g}x off file median : {n_mem_glitch}")
    print(f"either flagged (of {len(paths)} files scanned)          : {len(glitches)}")
    if glitches:
        glitch_df = pd.DataFrame(glitches)
        glitch_df.to_csv(args.glitch_report, index=False)
        print(f"wrote flagged VMs to {args.glitch_report} -- not dropped from anywhere;"
              " a human decides what to do with them.")

    print("\n" + "-" * 78)
    print("MID-TRACE CAPACITY SHIFT CENSUS (decided: hard-excluded -- see docs/DATA_QUALITY.md pattern 3)")
    print("-" * 78)
    n_cpu_shift = sum(1 for s in shifts if s["cpu_shift"])
    n_mem_shift = sum(1 for s in shifts if s["mem_shift"])
    print(f"CPU capacity: two sustained levels >{CAPACITY_SHIFT_FACTOR:g}x apart    : {n_cpu_shift}")
    print(f"memory capacity: two sustained levels >{CAPACITY_SHIFT_FACTOR:g}x apart : {n_mem_shift}")
    print(f"either flagged (of {len(paths)} files scanned)             : {len(shifts)}")
    print("A VM whose provisioned capacity changes mid-trace is not one right-sizeable")
    print("entity -- these are excluded above (some also caught earlier by the bad-CPU-")
    print("capacity gate), not merely flagged. Detail (levels, ratios) written for the")
    print("record, not as an open question.")
    if shifts:
        shift_df = pd.DataFrame(shifts)
        shift_df.to_csv(args.shift_report, index=False)
        print(f"wrote flagged VMs to {args.shift_report}")

    print("\n" + "-" * 78)
    print("HEADROOM -- THE MOTIVATING NUMBER FOR OBJECTIVE O1")
    print("-" * 78)
    print(f"mean CPU utilisation across fleet : {rep['cpu_mean_util'].mean()*100:.1f}%")
    print(f"mean CPU p95 across fleet         : {rep['cpu_p95_util'].mean()*100:.1f}%")
    print(f"mean memory utilisation           : {rep['mem_mean_util'].mean()*100:.1f}%")
    print(f"mean memory p95                   : {rep['mem_p95_util'].mean()*100:.1f}%")
    idle = int((rep["cpu_p95_util"] < 0.05).sum())
    print(f"\nVMs with p95 CPU below 5% (idle)  : {idle} "
          f"({100*idle/len(rep):.1f}% of fleet)")

    print(f"\nwrote per-VM report to {args.report}")
    print(f"generated {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    main()
