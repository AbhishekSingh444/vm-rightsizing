#!/usr/bin/env python3
"""
ablation.py -- T12: which component of M(eps) causes the gain?

Each arm flips exactly ONE switch against the same reference configuration and
is measured on the same quantity, so the arms are comparable by construction:

    shift in reclamation at MATCHED MEASURED RISK against peak_capped.

That number is the primary output and the reason this file exists. The frontier
figures support it; they are not the answer on their own. Reading the gain off
reclamation alone would be the exact failure this project is about -- a method
that reclaims more because it takes more risk has not gained anything.

Why `peak_capped` is the yardstick: it is the baseline the M(eps) frontier
actually overlaps. p95 sits at a measured risk no arm reaches (see
ENVIRONMENT_NOTES.md, "Feasibility check before T11"), so there is no matched
point against p95 to read. This is NOT a substitute for H1 -- H1 names p95 and
is unevaluable, which is itself the reported result. Everything here is
descriptive.

    ARM         SWITCH FLIPPED
    reference   the full method: intervals theta, snapped, capped, u = q0.95
    theta_off   dependence correction off -- theta = 1 for every VM (T10)
    theta_runs  extremal index from runs declustering at r = 12, not intervals
    snap_off    no catalog snapping (continuous allocations)
    cap_off     no cap at the VM's original allocation
    u_q90       GPD threshold at the 90th percentile of observed demand
    u_q98       GPD threshold at the 98th percentile

Two arms move the BASELINE as well as the method, and are flagged in the table:

  * `snap_off` -- peak_capped is itself unsnapped in that arm, so its matched
    risk and reclamation both move. Its shift is not directly comparable with
    the snapped arms'; it is reported because the size of the move is the
    measurement of how much of everything here is catalog quantisation.
  * `cap_off` -- M(eps) is uncapped while peak_capped is still capped, so the
    peak_capped column is no longer like-for-like. The like-for-like reading
    against uncapped `peak` is reported beside it.

The other five arms share identical baselines, so their shifts are directly
comparable with one another.

Usage:
    python3 ablation.py --traces data/fastStorage --out results/ablation \
        --figures figures
"""

import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from baselines import EVT_DEFAULT_RUN_LENGTH, EVT_MIN_EXCEEDANCES, EVT_THRESHOLD_Q
from sweep_eps import (
    EPS_SWEEP,
    RESOURCES,
    attach_theta,
    load_fleet,
    matched_risk_reclamation,
    run_frontier,
)

REFERENCE = "reference"

# (name, one-line description of the switch, settings)
ARMS = [
    (REFERENCE, "full method: intervals theta, snapped, capped, u = q0.95",
     dict(theta_method="intervals", snap=True, no_cap=False, threshold_q=0.95)),
    ("theta_off", "dependence correction OFF (theta = 1, the T10 arm)",
     dict(theta_method="fixed", snap=True, no_cap=False, threshold_q=0.95)),
    ("theta_runs", f"theta from runs declustering, r = {EVT_DEFAULT_RUN_LENGTH}",
     dict(theta_method="runs", snap=True, no_cap=False, threshold_q=0.95)),
    ("snap_off", "catalog snapping OFF (continuous allocations)",
     dict(theta_method="intervals", snap=False, no_cap=False, threshold_q=0.95)),
    ("cap_off", "cap at the original allocation OFF",
     dict(theta_method="intervals", snap=True, no_cap=True, threshold_q=0.95)),
    ("u_q90", "GPD threshold u at q0.90",
     dict(theta_method="intervals", snap=True, no_cap=False, threshold_q=0.90)),
    ("u_q98", "GPD threshold u at q0.98",
     dict(theta_method="intervals", snap=True, no_cap=False, threshold_q=0.98)),
]

# Arms whose peak_capped baseline is not the reference arm's. Flagged in the
# table rather than quietly compared.
BASELINE_MOVED = {"snap_off"}
# Arms where peak_capped is no longer the like-for-like comparator.
NOT_LIKE_FOR_LIKE = {"cap_off": "peak"}


def set_theta(fleet, settings, theta_fixed=1.0):
    """Put an arm's theta on every VM. Shared with src/bootstrap.py so the
    intervals are drawn around exactly the arms this file reports."""
    if settings["theta_method"] == "fixed":
        for _o, _e, m in fleet:
            m["theta"] = {res: theta_fixed for res, *_r in RESOURCES}
        return None
    return attach_theta(fleet, settings["theta_method"], EVT_DEFAULT_RUN_LENGTH)


def run_arm(fleet, settings, eps_sweep, eval_hours, theta_fixed=1.0):
    """One ablation arm over an already-loaded fleet. Returns its frontier."""
    theta_df = set_theta(fleet, settings, theta_fixed)
    fr = run_frontier(fleet, eps_sweep, eval_hours,
                      snap=settings["snap"], no_cap=settings["no_cap"])
    return fr, theta_df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--traces", required=True)
    ap.add_argument("--out", default="results/ablation")
    ap.add_argument("--figures", default="figures")
    ap.add_argument("--obs-days", type=float, default=21.0)
    ap.add_argument("--eval-days", type=float, default=9.0)
    ap.add_argument("--eps", type=float, nargs="+", default=EPS_SWEEP)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    eval_hours = args.eval_days * 24.0

    # The GPD fit depends on the threshold, so the fleet is re-loaded once per
    # distinct threshold and shared by every arm that uses it -- five of the
    # seven arms differ only in what happens AFTER the fit.
    thresholds = sorted({s["threshold_q"] for _n, _d, s in ARMS})
    fleets = {}
    for q in thresholds:
        print(f"loading fleet at threshold u = q{q:g} ...")
        fleet, skipped = load_fleet(args.traces, args.obs_days, args.eval_days,
                                    limit=args.limit, threshold_q=q,
                                    min_exceedances=EVT_MIN_EXCEEDANCES)
        fleets[q] = fleet
        print(f"  usable VMs: {len(fleet)} (skipped short: {skipped})")

    n_vms = len(fleets[EVT_THRESHOLD_Q])
    n_idle = sum(1 for _o, _e, m in fleets[EVT_THRESHOLD_Q] if m["idle"])

    rows = []
    frontiers = {}
    theta_summaries = []
    for name, desc, settings in ARMS:
        fr, theta_df = run_arm(fleets[settings["threshold_q"]], settings,
                               args.eps, eval_hours)
        frontiers[name] = fr
        fr.to_csv(os.path.join(args.out, f"frontier_{name}.csv"), index=False)
        if theta_df is not None:
            ok = theta_df[theta_df.status == "ok"]
            for res, *_r in RESOURCES:
                s = ok[ok.resource == res]["theta"]
                if len(s):
                    theta_summaries.append({
                        "arm": name, "resource": res, "n": len(s),
                        "theta_median": round(float(s.median()), 4),
                        "theta_q25": round(float(s.quantile(.25)), 4),
                        "theta_q75": round(float(s.quantile(.75)), 4),
                    })
        print(f"  arm {name}: done  ({desc})")

        for scope in ("all_vms", "non_idle"):
            for res, *_r in RESOURCES:
                m = matched_risk_reclamation(fr, scope, res, "peak_capped")
                row = {
                    "arm": name, "switch": desc, "scope": scope,
                    "resource": res,
                    "peak_capped_viol_frac": m["baseline_viol_frac"],
                    "peak_capped_recl_pct": m["baseline_recl_pct"],
                    "evt_recl_pct_at_matched_risk": m["evt_recl_pct"],
                    "gap_pp": m["gap_pp"],
                    "baseline_moved": name in BASELINE_MOVED,
                    "note": m["note"],
                }
                if name in NOT_LIKE_FOR_LIKE:
                    alt = matched_risk_reclamation(
                        fr, scope, res, NOT_LIKE_FOR_LIKE[name])
                    row["like_for_like_baseline"] = alt["baseline"]
                    row["like_for_like_gap_pp"] = alt["gap_pp"]
                else:
                    row["like_for_like_baseline"] = "peak_capped"
                    row["like_for_like_gap_pp"] = m["gap_pp"]
                rows.append(row)

    tab = pd.DataFrame(rows)

    # The shift: how far this arm's matched-risk gap sits from the reference
    # arm's. Positive means flipping that switch MOVED M(eps) TOWARDS
    # peak_capped (reclaimed more at the same measured risk).
    ref = tab[tab.arm == REFERENCE].set_index(["scope", "resource"])["gap_pp"]
    tab["reference_gap_pp"] = [
        ref.get((r.scope, r.resource), np.nan) for r in tab.itertuples()
    ]
    tab["shift_vs_reference_pp"] = (tab["gap_pp"] - tab["reference_gap_pp"]).round(3)
    tab.to_csv(os.path.join(args.out, "ablation_matched_risk.csv"), index=False)

    th = pd.DataFrame(theta_summaries)
    if len(th):
        th.to_csv(os.path.join(args.out, "ablation_theta.csv"), index=False)

    with pd.option_context("display.width", 220, "display.max_rows", 200,
                           "display.max_columns", 30):
        print("\n" + "=" * 110)
        print("T12 ABLATION -- PRIMARY TABLE")
        print("Shift in reclamation at MATCHED MEASURED RISK against "
              "peak_capped, one switch flipped per arm.")
        print("gap_pp  = M(eps) reclamation minus peak_capped's, at "
              "peak_capped's own measured risk (negative = worse).")
        print("shift   = this arm's gap minus the reference arm's. Positive = "
              "flipping this switch helped.")
        print("=" * 110)
        cols = ["arm", "scope", "resource", "peak_capped_viol_frac",
                "peak_capped_recl_pct", "evt_recl_pct_at_matched_risk",
                "gap_pp", "shift_vs_reference_pp", "baseline_moved",
                "like_for_like_gap_pp"]
        for res, *_r in RESOURCES:
            for scope in ("all_vms", "non_idle"):
                sub = tab[(tab.resource == res) & (tab.scope == scope)]
                print(f"\n--- {res.upper()}, {scope} ---")
                print(sub[cols].drop(columns=["scope", "resource"])
                      .to_string(index=False))

        print("\n" + "=" * 110)
        print("SWITCH LEGEND")
        print("=" * 110)
        for name, desc, _s in ARMS:
            flag = ""
            if name in BASELINE_MOVED:
                flag = "   [baseline moves too -- shift not directly comparable]"
            if name in NOT_LIKE_FOR_LIKE:
                flag = ("   [peak_capped not like-for-like here; see "
                        "like_for_like_gap_pp]")
            print(f"  {name:11s} {desc}{flag}")

        if len(th):
            print("\n" + "=" * 110)
            print("THETA BY ARM (estimated arms only)")
            print("=" * 110)
            print(th.to_string(index=False))

    with open(os.path.join(args.out, "ablation_config.json"), "w") as fh:
        json.dump({
            "obs_days": args.obs_days, "eval_days": args.eval_days,
            "eps_sweep": list(args.eps),
            "reference_arm": REFERENCE,
            "arms": {n: {"switch": d, **s} for n, d, s in ARMS},
            "baseline_moved": sorted(BASELINE_MOVED),
            "not_like_for_like": NOT_LIKE_FOR_LIKE,
            "run_length": EVT_DEFAULT_RUN_LENGTH,
            "run_length_fixed_before_any_t11_result": True,
            "min_exceedances": EVT_MIN_EXCEEDANCES,
            "vms_used": n_vms, "vms_idle": n_idle,
            "matched_risk_is_interpolated": True,
        }, fh, indent=2)

    if args.figures:
        from plot_ablation import plot
        os.makedirs(args.figures, exist_ok=True)
        for res, *_r in RESOURCES:
            path = plot(frontiers, ARMS, res, args.figures)
            print(f"wrote {path}")

    print(f"\nwrote {args.out}/ablation_matched_risk.csv, "
          f"ablation_theta.csv, frontier_<arm>.csv, ablation_config.json")


if __name__ == "__main__":
    main()
