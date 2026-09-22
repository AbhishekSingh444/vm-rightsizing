#!/usr/bin/env python3
"""
baselines.py -- reproduce the baselines named in the problem statement.

Computes three allocation policies from an observation window, replays them
against a held-out evaluation window, and reports reclaimed capacity alongside
the violations that reclamation caused.

    B0  as-provisioned  the trace's original allocation (reference)
    B1  peak            max demand over the observation window
    B2  p95             95th percentile of demand over the observation window

All three snap to a fixed flavour catalog. Recommenders see the observation
window only.

Usage:
    python3 baselines.py --traces ./traces --out ./results
    python3 baselines.py --traces ./traces --out ./results --obs-days 21 --eval-days 9
"""

import argparse
import glob
import json
import os
import sys

import numpy as np
import pandas as pd

# --- Bitbrains fastStorage columns ------------------------------------------
COL_TS = "Timestamp [ms]"
COL_CORES = "CPU cores"
COL_CAP_MHZ = "CPU capacity provisioned [MHZ]"
COL_USE_MHZ = "CPU usage [MHZ]"
COL_MEM_CAP_KB = "Memory capacity provisioned [KB]"
COL_MEM_USE_KB = "Memory usage [KB]"

# --- Flavour catalog (OpenStack-style) --------------------------------------
FLAVOUR_VCPU = [1, 2, 4, 8, 16]
FLAVOUR_MEM_GB = [2, 4, 8, 16, 32, 64]

SAMPLE_MINUTES = 5
CPU_VIOLATION_MIN_RUN = 2  # consecutive samples; memory needs only 1

# A capacity-provisioned column is expected to be constant (or near enough)
# for a given VM. If the first sample is off from the file's own median by
# more than this factor, the first sample is suspect -- either a corrupted
# reading (see capacity_glitch_ratio) or evidence of a genuine mid-trace
# resize (see detect_capacity_shift). Both are order-of-magnitude
# thresholds; see docs/DATA_QUALITY.md for what each has actually caught in
# this trace, including a real resize (133.csv, 12.7x) this factor does not
# distinguish from corruption on its own.
CAPACITY_GLITCH_FACTOR = 10.0
CAPACITY_SHIFT_FACTOR = 10.0
CAPACITY_SHIFT_LEVEL_TOL = 0.05  # values within this fraction of each other are "the same level"
CAPACITY_SHIFT_MIN_SHARE = 0.05  # a level must cover at least this share of rows to count as sustained


def snap_up(value, catalog):
    """Smallest catalog entry >= value. Returns the largest if value exceeds all."""
    for c in catalog:
        if c >= value:
            return c
    return catalog[-1]


def capacity_glitch_ratio(series):
    """Ratio of a capacity column's first sample to the file's own median.

    None when the median can't anchor a ratio (empty, non-finite, or <= 0).
    """
    if series.empty:
        return None
    first = float(series.iloc[0])
    median = float(series.median())
    if not np.isfinite(first) or not np.isfinite(median) or median <= 0:
        return None
    return first / median


def is_capacity_glitch(ratio, factor=CAPACITY_GLITCH_FACTOR):
    """True if a capacity-glitch ratio is more than `factor`x away from 1.0
    in either direction -- the first sample reads far above or far below
    what the rest of the file says the VM's capacity actually is.
    """
    return ratio is not None and (ratio > factor or ratio < 1.0 / factor)


def detect_capacity_shift(
    series,
    level_tol=CAPACITY_SHIFT_LEVEL_TOL,
    min_share=CAPACITY_SHIFT_MIN_SHARE,
    shift_factor=CAPACITY_SHIFT_FACTOR,
):
    """Detect a capacity column holding two (or more) materially different
    values that are each sustained for a meaningful share of the file --
    e.g. a VM genuinely resized partway through collection -- as opposed to
    one value plus noise, or a single corrupted sample.

    Values are sorted and greedily merged into "levels": each new value
    joins the current level if it is within `level_tol` of that level's
    running mean, else it starts a new level. A level only counts as
    "sustained" if it covers at least `min_share` of all rows -- this is
    what excludes a single bad sample (see capacity_glitch_ratio for that
    case) or a brief transition blip from counting as a real level.

    Returns None if fewer than two sustained levels are found, or if the
    highest and lowest sustained levels differ by less than `shift_factor`.
    Otherwise returns a dict with the low/high levels, each one's share of
    total rows, and their ratio.
    """
    vals = series.dropna().to_numpy()
    n = len(vals)
    if n == 0:
        return None

    sorted_vals = np.sort(vals)
    clusters = []  # each entry: [running_sum, count]
    for v in sorted_vals:
        if clusters:
            mean = clusters[-1][0] / clusters[-1][1]
            if mean > 0 and abs(v - mean) <= level_tol * mean:
                clusters[-1][0] += v
                clusters[-1][1] += 1
                continue
        clusters.append([v, 1])

    sustained = [(total / count, count) for total, count in clusters if count / n >= min_share]
    if len(sustained) < 2:
        return None

    lo_level, lo_count = min(sustained, key=lambda x: x[0])
    hi_level, hi_count = max(sustained, key=lambda x: x[0])
    if lo_level <= 0:
        return None
    ratio = hi_level / lo_level
    if ratio < shift_factor:
        return None

    return {
        "low_level": lo_level,
        "low_share": lo_count / n,
        "high_level": hi_level,
        "high_share": hi_count / n,
        "ratio": ratio,
    }


def load_vm(path):
    """Parse one Bitbrains CSV into a demand frame. Returns (df, meta) or None."""
    try:
        df = pd.read_csv(path, sep=";", engine="python")
    except Exception as exc:
        print(f"  skip {os.path.basename(path)}: unreadable ({exc})", file=sys.stderr)
        return None

    df.columns = [c.strip().strip('"') for c in df.columns]
    needed = [COL_TS, COL_CORES, COL_CAP_MHZ, COL_USE_MHZ, COL_MEM_CAP_KB, COL_MEM_USE_KB]
    missing = [c for c in needed if c not in df.columns]
    if missing:
        print(f"  skip {os.path.basename(path)}: missing {missing}", file=sys.stderr)
        return None

    for c in needed:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=needed)
    if df.empty:
        return None

    cores = float(df[COL_CORES].iloc[0])
    cap_mhz = float(df[COL_CAP_MHZ].iloc[0])
    mem_cap_gb = float(df[COL_MEM_CAP_KB].iloc[0]) / (1024.0 * 1024.0)

    # Convert MHz demand to core-equivalents using the VM's own clock rate.
    per_core_mhz = (cap_mhz / cores) if (cap_mhz > 0 and cores > 0) else np.nan
    if not np.isfinite(per_core_mhz) or per_core_mhz <= 0:
        print(f"  skip {os.path.basename(path)}: bad CPU capacity", file=sys.stderr)
        return None

    # Decided (docs/DATA_QUALITY.md, pattern 4): a VM already provisioned
    # below the smallest flavour in the catalog cannot be right-sized -- no
    # smaller entry exists to snap to, so snap_up forces its allocation
    # upward under every method, producing negative reclamation that
    # measures the catalog floor, not the recommender.
    if cores < min(FLAVOUR_VCPU) or mem_cap_gb < min(FLAVOUR_MEM_GB):
        print(f"  skip {os.path.basename(path)}: below flavour catalog floor", file=sys.stderr)
        return None

    # Decided (docs/DATA_QUALITY.md, pattern 3): a VM whose provisioned
    # capacity changes mid-trace is not one right-sizeable entity -- the
    # observation window describes a machine that no longer exists by the
    # evaluation window. Excluded rather than split at the shift point
    # (leaves sub-windows too short for a 21/9 split) or given post-shift
    # capacity (pairs demand history with the wrong allocation).
    if detect_capacity_shift(df[COL_CAP_MHZ]) or detect_capacity_shift(df[COL_MEM_CAP_KB]):
        print(f"  skip {os.path.basename(path)}: mid-trace capacity shift", file=sys.stderr)
        return None

    out = pd.DataFrame(
        {
            # Column is named "[ms]" but holds seconds -- see CLAUDE.md dataset traps.
            "ts": df[COL_TS].to_numpy(),
            "cpu_cores": df[COL_USE_MHZ].to_numpy() / per_core_mhz,
            "mem_gb": df[COL_MEM_USE_KB].to_numpy() / (1024.0 * 1024.0),
        }
    )

    # Demand above the original allocation is a measurement artefact, not a
    # right-sizing opportunity. Clip so baselines are not distorted by it.
    out["cpu_cores"] = out["cpu_cores"].clip(lower=0.0, upper=cores)
    out["mem_gb"] = out["mem_gb"].clip(lower=0.0, upper=mem_cap_gb)

    meta = {
        "vm_id": os.path.splitext(os.path.basename(path))[0],
        "orig_cores": cores,
        "orig_mem_gb": mem_cap_gb,
        "n_samples": len(out),
    }
    return out, meta


def count_violations(demand, allocation, min_run):
    """Return (event_count, total_samples_exhausted) for runs >= min_run."""
    over = (demand > allocation).to_numpy()
    if not over.any():
        return 0, 0

    events = 0
    exhausted = 0
    run = 0
    for flag in over:
        if flag:
            run += 1
        else:
            if run >= min_run:
                events += 1
                exhausted += run
            run = 0
    if run >= min_run:
        events += 1
        exhausted += run
    return events, exhausted


def split_obs_eval(df, obs_days, eval_days):
    """Split a VM's demand frame into (observation, evaluation) by wall-clock
    time, using the "ts" column (seconds since epoch) rather than row count.

    Observation is the first `obs_days` from the VM's own first sample;
    evaluation is the `eval_days` immediately after that. Row count is not a
    reliable proxy for elapsed time -- sampling interval varies per VM -- so
    this must not assume SAMPLE_MINUTES-spaced rows.

    Returns (obs, eval) or None if the trace does not run long enough to
    cover the full obs_days + eval_days span.
    """
    if df.empty or len(df) < 2:
        return None

    t0 = df["ts"].min()
    t_last = df["ts"].max()
    obs_end = t0 + obs_days * 86400.0
    eval_end = obs_end + eval_days * 86400.0

    # A discretely-sampled trace essentially never lands exactly on the
    # nominal day boundary -- its last sample sits up to one sampling
    # interval short of it. Requiring t_last >= eval_end exactly would
    # reject almost every real trace over a rounding artefact, so allow a
    # shortfall of up to this VM's own median sampling gap.
    gap = df["ts"].diff().median()
    if not np.isfinite(gap) or gap <= 0:
        gap = 0.0

    if t_last < eval_end - gap:
        return None

    obs = df[(df["ts"] >= t0) & (df["ts"] < obs_end)]
    ev = df[(df["ts"] >= obs_end) & (df["ts"] < eval_end)]
    return obs, ev


def recommend(obs, policy, snap=True):
    """Allocation from the observation window under the named policy.

    `snap=False` returns the raw continuous value instead of the smallest
    catalog entry at or above it. That is NOT a valid right-sizing policy --
    no real private cloud provisions arbitrary fractional capacity, which is
    why the catalog exists (see CLAUDE.md invariant 5). It exists solely as
    the counterfactual for measuring how much of a reported reclamation gap is
    catalog quantisation rather than workload behaviour. Snapping remains the
    default and the only mode any reported result uses.
    """
    if policy == "peak":
        cpu = obs["cpu_cores"].max()
        mem = obs["mem_gb"].max()
    elif policy == "p95":
        cpu = obs["cpu_cores"].quantile(0.95)
        mem = obs["mem_gb"].quantile(0.95)
    else:
        raise ValueError(policy)
    if not snap:
        return float(cpu), float(mem)
    return snap_up(cpu, FLAVOUR_VCPU), snap_up(mem, FLAVOUR_MEM_GB)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--traces", required=True, help="directory of per-VM CSVs")
    ap.add_argument("--out", default="results")
    ap.add_argument("--obs-days", type=float, default=21.0)
    ap.add_argument("--eval-days", type=float, default=9.0)
    ap.add_argument("--idle-p99", type=float, default=0.05,
                    help="VMs with p99 CPU below this fraction of allocation "
                         "are flagged idle and reported separately")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--unsnapped", action="store_true",
                    help="DIAGNOSTIC ONLY: allocate the raw continuous "
                         "max/p95 instead of snapping up to the flavour "
                         "catalog. Not a valid policy -- used only as the "
                         "counterfactual for measuring how much of a "
                         "reclamation gap is catalog quantisation. Write it "
                         "to a separate --out directory; never mix its "
                         "outputs with a reported run.")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)

    if args.unsnapped:
        print("*** --unsnapped: allocations are CONTINUOUS, not catalog "
              "flavours.\n*** This is a diagnostic counterfactual, not a "
              "result. Do not report these\n*** numbers as reclamation "
              "achieved by any method.\n")

    # Nominal counts assuming uniform 5-minute sampling -- printed for
    # reference only. The actual split below is by wall-clock time, since
    # sampling interval varies per VM (5s to well over 5min in this trace).
    obs_samples = int(args.obs_days * 24 * 60 / SAMPLE_MINUTES)
    eval_samples = int(args.eval_days * 24 * 60 / SAMPLE_MINUTES)
    eval_hours = args.eval_days * 24.0

    paths = sorted(glob.glob(os.path.join(args.traces, "*.csv")))
    if args.limit:
        paths = paths[: args.limit]
    if not paths:
        raise SystemExit(f"no CSVs found in {args.traces}")

    print(f"loading {len(paths)} traces from {args.traces}")
    print(f"split: {args.obs_days} observation days / {args.eval_days} evaluation days "
          f"(~{obs_samples} / ~{eval_samples} samples at uniform 5-min sampling)")

    rows = []
    skipped_short = 0

    for path in paths:
        loaded = load_vm(path)
        if loaded is None:
            continue
        df, meta = loaded

        split = split_obs_eval(df, args.obs_days, args.eval_days)
        if split is None:
            skipped_short += 1
            continue
        obs, ev = split
        if obs.empty or ev.empty:
            skipped_short += 1
            continue

        p99_cpu = obs["cpu_cores"].quantile(0.99)
        is_idle = (p99_cpu / meta["orig_cores"]) < args.idle_p99 if meta["orig_cores"] else True

        allocations = {
            "as_provisioned": (meta["orig_cores"], meta["orig_mem_gb"]),
            "peak": recommend(obs, "peak", snap=not args.unsnapped),
            "p95": recommend(obs, "p95", snap=not args.unsnapped),
        }

        for method, (cpu_alloc, mem_alloc) in allocations.items():
            cpu_events, cpu_samples = count_violations(
                ev["cpu_cores"], cpu_alloc, CPU_VIOLATION_MIN_RUN
            )
            mem_events, mem_samples = count_violations(ev["mem_gb"], mem_alloc, 1)

            rows.append(
                {
                    "vm_id": meta["vm_id"],
                    "method": method,
                    "idle": is_idle,
                    "orig_cores": meta["orig_cores"],
                    "orig_mem_gb": meta["orig_mem_gb"],
                    "alloc_cores": cpu_alloc,
                    "alloc_mem_gb": mem_alloc,
                    "recl_vcpu_hours": (meta["orig_cores"] - cpu_alloc) * eval_hours,
                    "recl_mem_gb_hours": (meta["orig_mem_gb"] - mem_alloc) * eval_hours,
                    "cpu_events": cpu_events,
                    "cpu_exhausted_minutes": cpu_samples * SAMPLE_MINUTES,
                    "mem_events": mem_events,
                    "mem_exhausted_minutes": mem_samples * SAMPLE_MINUTES,
                    "any_violation": (cpu_events + mem_events) > 0,
                }
            )

    if not rows:
        raise SystemExit("no VMs had enough samples for the requested split")

    per_vm = pd.DataFrame(rows)
    per_vm.to_csv(os.path.join(args.out, "per_vm_results.csv"), index=False)

    n_vms = per_vm["vm_id"].nunique()
    n_idle = per_vm[per_vm["idle"]]["vm_id"].nunique()
    print(
        f"\nusable VMs: {n_vms}  (idle: {n_idle}, "
        f"skipped: {skipped_short} -- do not cover the full "
        f"{args.obs_days:g}+{args.eval_days:g} day obs+eval span)"
    )

    def summarise(frame, label):
        total_alloc_cpu = (
            frame[frame["method"] == "as_provisioned"]["orig_cores"].sum() * eval_hours
        )
        total_alloc_mem = (
            frame[frame["method"] == "as_provisioned"]["orig_mem_gb"].sum() * eval_hours
        )
        out = []
        for method in ["as_provisioned", "peak", "p95"]:
            sub = frame[frame["method"] == method]
            if sub.empty:
                continue
            n = len(sub)
            recl_cpu = sub["recl_vcpu_hours"].sum()
            recl_mem = sub["recl_mem_gb_hours"].sum()
            cpu_violated = sub["cpu_events"] > 0
            mem_violated = sub["mem_events"] > 0
            out.append(
                {
                    "scope": label,
                    "method": method,
                    "vms": n,
                    "recl_vcpu_hours": round(recl_cpu, 1),
                    "recl_vcpu_pct": round(100 * recl_cpu / total_alloc_cpu, 2)
                    if total_alloc_cpu
                    else 0.0,
                    "recl_mem_gb_hours": round(recl_mem, 1),
                    "recl_mem_pct": round(100 * recl_mem / total_alloc_mem, 2)
                    if total_alloc_mem
                    else 0.0,
                    # Combined (both resources) -- kept for backward comparability.
                    "cpu_events": int(sub["cpu_events"].sum()),
                    "mem_events": int(sub["mem_events"].sum()),
                    "exhausted_minutes": int(
                        sub["cpu_exhausted_minutes"].sum()
                        + sub["mem_exhausted_minutes"].sum()
                    ),
                    "vms_with_violation": int(sub["any_violation"].sum()),
                    "pct_vms_safe": round(100 * (1 - sub["any_violation"].mean()), 2),
                    # Split by resource -- the CPU rule (>=2 consecutive
                    # samples, degrading) and the memory rule (any single
                    # sample, terminal) are asymmetric by design and must be
                    # reported separately, not just merged into "a
                    # violation".
                    "cpu_exhausted_minutes": int(sub["cpu_exhausted_minutes"].sum()),
                    "cpu_vms_with_violation": int(cpu_violated.sum()),
                    "cpu_pct_safe": round(100 * (1 - cpu_violated.mean()), 2),
                    "mem_exhausted_minutes": int(sub["mem_exhausted_minutes"].sum()),
                    "mem_vms_with_violation": int(mem_violated.sum()),
                    "mem_pct_safe": round(100 * (1 - mem_violated.mean()), 2),
                }
            )
        return out

    summary = pd.DataFrame(
        summarise(per_vm, "all_vms") + summarise(per_vm[~per_vm["idle"]], "non_idle")
    )
    summary.to_csv(os.path.join(args.out, "baseline_summary.csv"), index=False)

    with pd.option_context("display.width", 200, "display.max_columns", 50):
        print("\n" + "=" * 100)
        print("BASELINE SUMMARY  (this table is Figure 1 of the paper)")
        print("=" * 100)
        print(summary.to_string(index=False))

    with open(os.path.join(args.out, "run_config.json"), "w") as fh:
        json.dump(
            {
                "obs_days": args.obs_days,
                "eval_days": args.eval_days,
                "obs_samples": obs_samples,
                "eval_samples": eval_samples,
                "flavour_vcpu": FLAVOUR_VCPU,
                "flavour_mem_gb": FLAVOUR_MEM_GB,
                "cpu_violation_min_run": CPU_VIOLATION_MIN_RUN,
                "idle_p99_threshold": args.idle_p99,
                "vms_used": int(n_vms),
                "vms_skipped_short": skipped_short,
                # False marks a diagnostic counterfactual run, not a result.
                "snapped_to_catalog": not args.unsnapped,
            },
            fh,
            indent=2,
        )

    print(f"\nwrote {args.out}/per_vm_results.csv, baseline_summary.csv, run_config.json")


if __name__ == "__main__":
    main()
