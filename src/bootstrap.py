#!/usr/bin/env python3
"""
bootstrap.py -- T16: 95% intervals over VMs for every frontier number.

Every number before T16 is a point estimate on one fleet of 792 VMs. Without
an interval there is no way to tell a 0.7 pp ablation shift from noise. This
script resamples that fleet and recomputes everything on each resample.

THE UNIT IS THE VM. A resample draws 792 VMs with replacement and keeps each
drawn VM's whole evaluation window intact. Samples within a VM are
autocorrelated, so resampling them individually would understate the
variance; they are never resampled. One resample matrix is drawn once and
shared by every frontier, arm, scope and resource, so differences between
arms (the ablation shifts) are PAIRED -- each arm is evaluated on the same
resampled fleet in the same replicate.

What the bootstrap varies and what it does not: it varies which VMs make up
the fleet. It does not vary the 21/9 observation/evaluation split, the trace,
the catalog or any setting -- those are fixed in every replicate. An interval
here is the uncertainty from fleet composition only (see ENVIRONMENT_NOTES.md,
"Threats to validity").

Nothing is refitted per resample: the per-VM allocations, violations and
reclaimed hours come from one pass of the same `sweep_eps.run_per_vm` that
the headline frontiers summarise, and a resample only reweights VMs. Before
any interval is computed, each frontier is rebuilt from those per-VM tables
and checked against its published CSV; a mismatch stops the run, because an
interval around a different number is meaningless.

Outputs, under --out:
  * frontier_<config>_ci.csv -- every frontier row with 95% percentile
    intervals on reclamation and violation fraction, plus the Wilson 95%
    interval on the violation fraction (k of n VMs).
  * matched_risk_gaps_ci.csv -- the matched-risk gap against peak_capped
    (and, for cap_off, against uncapped peak) for every config.
  * ablation_shifts_ci.csv -- every T12 shift against the reference arm.
  * run_config.json -- seed, resample count and everything else needed to
    reproduce the intervals.

UNDEFINED GAPS. A matched-risk gap only exists when the baseline's measured
risk falls inside the swept M(eps) range. On some resamples it does not, and
the gap is undefined there. Every gap and shift row reports `undefined_share`
-- the fraction of resamples with no matched point. A percentile interval is
reported ONLY when that share is zero: an interval over the defined resamples
alone would describe a conditional distribution while looking like the
unconditional one, and would hide exactly the cells where the comparison is
fragile.

Usage:
    python3 bootstrap.py --traces data/fastStorage --results results \
        --out results/bootstrap --seed 16 --resamples 10000
"""

import argparse
import json
import math
import os
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ablation import ARMS, BASELINE_MOVED, NOT_LIKE_FOR_LIKE, REFERENCE, set_theta
from baselines import EVT_MIN_EXCEEDANCES
from sweep_eps import (
    EPS_SWEEP,
    RESOURCES,
    frontier_from_per_vm,
    interp_at_risk,
    load_fleet,
    matched_risk_reclamation,
    run_per_vm,
)

DEFAULT_SEED = 16
DEFAULT_RESAMPLES = 10_000
LEVEL = 0.95
Z95 = 1.959963984540054          # standard normal 0.975 quantile
SCOPES = ("all_vms", "non_idle")
COMPARATOR = "peak_capped"

# Frontiers to put intervals on: the seven T12 arms, plus theta = 1 unsnapped,
# which is not an arm but is a published figure (figures/frontier_unsnapped).
CONFIGS = ARMS + [
    ("theta_1_unsnapped", "theta = 1, unsnapped (T10 diagnostic; figure only)",
     dict(theta_method="fixed", snap=False, no_cap=False, threshold_q=0.95)),
]

# The published frontier each config must reproduce exactly, relative to
# --results. Arms are also checked against their T12 frontier_<arm>.csv.
PUBLISHED = {
    "reference": "theta_intervals/frontier.csv",
    "theta_off": "frontier.csv",
    "theta_runs": "theta_runs/frontier.csv",
    "snap_off": "theta_intervals_unsnapped/frontier.csv",
    "cap_off": "ablation/frontier_cap_off.csv",
    "u_q90": "ablation/frontier_u_q90.csv",
    "u_q98": "ablation/frontier_u_q98.csv",
    "theta_1_unsnapped": "unsnapped/frontier.csv",
}


# ------------------------------------------------------------------ helpers

def resample_counts(n, resamples, seed):
    """`resamples` x `n` matrix: how many times each VM is drawn in each
    resample. Row sums are all `n`. Deterministic in `seed`."""
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, n, size=(resamples, n))
    flat = idx + n * np.arange(resamples)[:, None]
    return np.bincount(flat.ravel(), minlength=resamples * n).reshape(
        resamples, n)


def percentile_interval(samples, level=LEVEL):
    a = 100.0 * (1.0 - level) / 2.0
    lo, hi = np.percentile(np.asarray(samples, dtype=float), [a, 100.0 - a])
    return float(lo), float(hi)


def wilson_interval(k, n, z=Z95):
    """Wilson score interval for k successes in n trials."""
    if n == 0:
        return np.nan, np.nan
    p = k / n
    d = 1.0 + z * z / n
    centre = (p + z * z / (2.0 * n)) / d
    half = z * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n)) / d
    # The bounds are exactly 0 at k = 0 and exactly 1 at k = n; computed as
    # centre - half they come out as rounding residue (~1e-17), not zero.
    lo = 0.0 if k == 0 else max(0.0, centre - half)
    hi = 1.0 if k == n else min(1.0, centre + half)
    return lo, hi


def defined_interval(samples, level=LEVEL):
    """(undefined_share, lo, hi). The interval is NaN unless every resample is
    defined -- see the module docstring on undefined gaps."""
    s = np.asarray(samples, dtype=float)
    undefined = float(np.mean(np.isnan(s)))
    if undefined > 0:
        return undefined, np.nan, np.nan
    lo, hi = percentile_interval(s, level)
    return 0.0, lo, hi


def resampled_stats(per_vm_tables, idle, counts, eval_hours):
    """Pooled reclamation % and violating fraction on every resample, for
    every (scope, method, eps, resource) of one frontier.

    Returns {(scope, method, eps_or_None, res): (recl_pct[B], viol_frac[B])}.
    Each is a ratio of count-weighted sums, the same pooled definition
    `summarise` uses on the full fleet.
    """
    masks = {"all_vms": np.ones(len(idle)), "non_idle": (~idle).astype(float)}
    keys, cols = [], []
    for (method, eps), per_vm in per_vm_tables:
        e = None if (isinstance(eps, float) and np.isnan(eps)) else eps
        for res, *_r in RESOURCES:
            recl = per_vm[f"{res}_recl_hours"].to_numpy(dtype=float)
            orig = per_vm[f"{res}_orig"].to_numpy(dtype=float) * eval_hours
            viol = per_vm[f"{res}_violated"].to_numpy(dtype=float)
            for scope in SCOPES:
                m = masks[scope]
                keys.append((scope, method, e, res))
                cols += [recl * m, orig * m, viol * m, m]
    sums = counts @ np.column_stack(cols)
    out = {}
    for i, key in enumerate(keys):
        recl, orig, viol, n = (sums[:, 4 * i + j] for j in range(4))
        with np.errstate(invalid="ignore", divide="ignore"):
            out[key] = (100.0 * recl / orig, viol / n)
    return out


def gap_samples(stats, scope, res, eps_sweep, comparator):
    """Matched-risk gap (pp) against `comparator` on every resample; NaN where
    the comparator's risk is outside that resample's M(eps) range."""
    xs = np.column_stack([stats[(scope, "evt", e, res)][1] for e in eps_sweep])
    ys = np.column_stack([stats[(scope, "evt", e, res)][0] for e in eps_sweep])
    by, bx = stats[(scope, comparator, None, res)]
    out = np.empty(len(bx))
    for b in range(len(bx)):
        out[b] = interp_at_risk(xs[b], ys[b], bx[b]) - by[b]
    return out


def same_frontier(got, path):
    want = pd.read_csv(path)
    got = pd.read_csv(pd.io.common.StringIO(got.to_csv(index=False)))
    try:
        pd.testing.assert_frame_equal(got, want, check_exact=True)
        return True
    except AssertionError:
        return False


# --------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--traces", required=True)
    ap.add_argument("--results", default="results",
                    help="where the published frontiers live; each config is "
                         "checked against its CSV before any interval is drawn")
    ap.add_argument("--out", default="results/bootstrap")
    ap.add_argument("--obs-days", type=float, default=21.0)
    ap.add_argument("--eval-days", type=float, default=9.0)
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--resamples", type=int, default=DEFAULT_RESAMPLES)
    ap.add_argument("--limit", type=int, default=0,
                    help="smoke test on the first N trace files; the "
                         "published-frontier check is then skipped and "
                         "reported as skipped")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    eval_hours = args.eval_days * 24.0
    t0 = time.time()

    thresholds = sorted({s["threshold_q"] for _n, _d, s in CONFIGS})
    fleets = {}
    for q in thresholds:
        print(f"loading fleet at threshold u = q{q:g} ...")
        fleets[q], skipped = load_fleet(
            args.traces, args.obs_days, args.eval_days, limit=args.limit,
            threshold_q=q, min_exceedances=EVT_MIN_EXCEEDANCES)
        print(f"  usable VMs: {len(fleets[q])} (skipped short: {skipped})")

    # Pairing requires every fleet to list the same VMs in the same order.
    order = {q: [(m["vm_id"], m["idle"]) for _o, _e, m in f]
             for q, f in fleets.items()}
    first = order[thresholds[0]]
    if any(o != first for o in order.values()):
        raise SystemExit("fleets loaded at different thresholds do not list "
                         "the same VMs in the same order; resamples would "
                         "not be paired across arms")
    idle = np.array([i for _v, i in first], dtype=bool)
    n_vms = len(idle)

    tables, frontiers, checks = {}, {}, {}
    for name, desc, s in CONFIGS:
        fleet = fleets[s["threshold_q"]]
        set_theta(fleet, s)
        tables[name] = run_per_vm(fleet, EPS_SWEEP, eval_hours,
                                  snap=s["snap"], no_cap=s["no_cap"])
        frontiers[name] = frontier_from_per_vm(tables[name], eval_hours)
        paths = [PUBLISHED[name]]
        if (name in {n for n, _d, _s in ARMS}
                and f"ablation/frontier_{name}.csv" not in paths):
            paths.append(f"ablation/frontier_{name}.csv")
        for rel in paths:
            if args.limit:
                checks[rel] = "skipped (--limit)"
                continue
            ok = same_frontier(frontiers[name], os.path.join(args.results, rel))
            checks[rel] = "identical" if ok else "MISMATCH"
        print(f"  {name:18s} per-VM tables built  "
              + "  ".join(f"{p}: {checks[p]}" for p in paths))
    if any(v == "MISMATCH" for v in checks.values()):
        raise SystemExit("a rebuilt frontier does not match its published "
                         "CSV; refusing to draw intervals around a different "
                         "number")

    print(f"\ndrawing {args.resamples} resamples of {n_vms} VMs "
          f"(seed {args.seed}) ...")
    counts = resample_counts(n_vms, args.resamples, args.seed).astype(float)

    # --- frontier intervals -------------------------------------------------
    stats = {}
    for name, _d, _s in CONFIGS:
        stats[name] = resampled_stats(tables[name], idle, counts, eval_hours)
        rows = []
        for r in frontiers[name].itertuples():
            e = None if np.isnan(r.eps) else r.eps
            recl_b, viol_b = stats[name][(r.scope, r.method, e, r.resource)]
            rl, rh = percentile_interval(recl_b)
            vl, vh = percentile_interval(viol_b)
            wl, wh = wilson_interval(r.vms_with_violation, r.vms)
            rows.append({
                "scope": r.scope, "resource": r.resource, "method": r.method,
                "eps": r.eps, "vms": r.vms,
                "recl_pct": r.recl_pct,
                "recl_lo": round(rl, 2), "recl_hi": round(rh, 2),
                "vms_with_violation": r.vms_with_violation,
                "viol_frac": r.viol_frac,
                "viol_lo": round(vl, 4), "viol_hi": round(vh, 4),
                "wilson_lo": round(wl, 4), "wilson_hi": round(wh, 4),
            })
        pd.DataFrame(rows).to_csv(
            os.path.join(args.out, f"frontier_{name}_ci.csv"), index=False)

    # --- matched-risk gaps --------------------------------------------------
    gaps, gap_rows = {}, []
    for name, _d, _s in CONFIGS:
        comparators = [COMPARATOR]
        if name in NOT_LIKE_FOR_LIKE:
            comparators.append(NOT_LIKE_FOR_LIKE[name])
        for scope in SCOPES:
            for res, *_r in RESOURCES:
                for comp in comparators:
                    g = gap_samples(stats[name], scope, res, EPS_SWEEP, comp)
                    gaps[(name, scope, res, comp)] = g
                    point = matched_risk_reclamation(frontiers[name], scope,
                                                     res, comp)
                    und, lo, hi = defined_interval(g)
                    gap_rows.append({
                        "config": name, "scope": scope, "resource": res,
                        "comparator": comp, "gap_pp": point["gap_pp"],
                        "undefined_share": round(und, 4),
                        "gap_lo": round(lo, 2), "gap_hi": round(hi, 2),
                        "excludes_zero": (bool(lo > 0 or hi < 0)
                                          if und == 0 else np.nan),
                        "point_note": point["note"],
                    })
    gap_df = pd.DataFrame(gap_rows)
    gap_df.to_csv(os.path.join(args.out, "matched_risk_gaps_ci.csv"),
                  index=False)

    # --- ablation shifts (paired: same resample in both arms) ---------------
    shift_rows = []
    gap_point = gap_df[gap_df.comparator == COMPARATOR].set_index(
        ["config", "scope", "resource"])["gap_pp"]
    for name, desc, _s in ARMS:
        if name == REFERENCE:
            continue
        for scope in SCOPES:
            for res, *_r in RESOURCES:
                sh = (gaps[(name, scope, res, COMPARATOR)]
                      - gaps[(REFERENCE, scope, res, COMPARATOR)])
                und, lo, hi = defined_interval(sh)
                pt = gap_point[(name, scope, res)] - gap_point[(REFERENCE, scope, res)]
                shift_rows.append({
                    "arm": name, "switch": desc, "scope": scope,
                    "resource": res,
                    "shift_vs_reference_pp": round(float(pt), 3),
                    "undefined_share": round(und, 4),
                    "shift_lo": round(lo, 2), "shift_hi": round(hi, 2),
                    "excludes_zero": (bool(lo > 0 or hi < 0)
                                      if und == 0 else np.nan),
                    "baseline_moved": name in BASELINE_MOVED,
                })
    shift_df = pd.DataFrame(shift_rows)
    shift_df.to_csv(os.path.join(args.out, "ablation_shifts_ci.csv"),
                    index=False)

    # --- console ------------------------------------------------------------
    ref = pd.read_csv(os.path.join(args.out, f"frontier_{REFERENCE}_ci.csv"))
    with pd.option_context("display.width", 220, "display.max_rows", 300,
                           "display.max_columns", 30):
        print("\n" + "=" * 110)
        print(f"T16 BOOTSTRAP -- {args.resamples} resamples of {n_vms} VMs, "
              f"seed {args.seed}, VM is the unit, 95% percentile intervals")
        print("Varies fleet composition only. The 21/9 split, trace and "
              "settings are fixed in every resample.")
        print("=" * 110)
        print("\nHEADLINE FRONTIER (reference = intervals theta, snapped, "
              "capped, u = q0.95)")
        print("recl_lo/hi, viol_lo/hi: bootstrap.  wilson_lo/hi: Wilson score "
              "on k of n VMs.")
        print(ref[ref.method != "as_provisioned"].to_string(index=False))
        print("\n" + "=" * 110)
        print("MATCHED-RISK GAPS (pp; M(eps) reclamation minus the "
              "comparator's, at the comparator's measured risk)")
        print("undefined_share = fraction of resamples where the comparator's "
              "risk lies outside the swept M(eps) range.")
        print("An interval is reported only where undefined_share is 0.")
        print("=" * 110)
        print(gap_df.drop(columns=["point_note"]).to_string(index=False))
        print("\n" + "=" * 110)
        print("T12 ABLATION SHIFTS (pp, arm gap minus reference gap, paired "
              "within each resample)")
        print("=" * 110)
        print(shift_df.drop(columns=["switch"]).to_string(index=False))

    with open(os.path.join(args.out, "run_config.json"), "w") as fh:
        json.dump({
            "seed": args.seed, "resamples": args.resamples,
            "rng": f"numpy.random.default_rng (PCG64), numpy {np.__version__}",
            "unit": "VM, with replacement; samples within a VM never resampled",
            "paired_across_configs": True,
            "level": LEVEL,
            "interval": "percentile (numpy.percentile, linear) on resamples; "
                        "Wilson score on every violation fraction",
            "gap_interval_rule": "reported only when undefined_share == 0",
            "comparator": COMPARATOR,
            "not_like_for_like": NOT_LIKE_FOR_LIKE,
            "varies": "fleet composition only",
            "fixed": "obs/eval split, trace, catalog, every recommender setting",
            "obs_days": args.obs_days, "eval_days": args.eval_days,
            "eps_sweep": EPS_SWEEP,
            "configs": {n: {"switch": d, **s} for n, d, s in CONFIGS},
            "published_check": checks,
            "vms_used": n_vms, "vms_idle": int(idle.sum()),
            "limit": args.limit,
        }, fh, indent=2)

    print(f"\nwrote {args.out}/frontier_<config>_ci.csv, "
          f"matched_risk_gaps_ci.csv, ablation_shifts_ci.csv, run_config.json "
          f"({time.time() - t0:.0f} s)")


if __name__ == "__main__":
    main()
