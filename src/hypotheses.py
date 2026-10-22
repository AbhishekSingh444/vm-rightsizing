#!/usr/bin/env python3
"""
hypotheses.py -- T15: H2 and H3 against their pre-registered statistics.

H1 was reported in T10-T12 (not evaluable at matched risk). H2 had only been
reported through a descriptive ratio (measured risk / eps, "29x"), which is not
the statistic `docs/HYPOTHESES.txt` names, and H3 had not been computed at all.
This script closes both gaps. It is post-processing of frontier CSVs already
produced by `sweep_eps.py`: nothing is refitted, and no threshold is an
argument -- the 0.05 ECE bound, the 95% interval and the 25% / 20% H3
thresholds are module constants copied from the pre-registration.

H2 (pre-registered): across eps in {0.001, 0.005, 0.01, 0.05, 0.10}, the
observed fraction of VMs with at least one violation in W_eval falls inside the
95% binomial interval of eps, and ECE over the sweep is below 0.05.
NULL: outside the interval at two or more points, or ECE >= 0.05.

  * The 95% binomial interval of eps is the central acceptance region of
    Binomial(n, eps): [ppf(0.025), ppf(0.975)] in VM counts, divided by n. A
    point is inside if its observed count lies in it.
  * ECE = (1/K) * sum_k |observed_k - eps_k| over the K = 5 sweep points. Each
    sweep point is one "bin" whose predicted probability is its eps. Every bin
    holds the same n VMs, so this equal-weighted mean is identical to the
    usual count-weighted ECE.

H3 (pre-registered): at least 25% of allocated vCPU-hours and 20% of allocated
GB-hours can be reclaimed relative to B0 with zero violation events in W_eval.
NULL: the largest reclamation achieving zero observed violations falls below
either threshold.

  * Candidates are every recommender row in the frontier -- the baselines,
    their capped diagnostics and every swept M(eps) point. All of them see the
    observation window only. B0 (as_provisioned) is included and is the floor:
    zero reclamation, zero violations by construction.
  * A per-VM oracle that keeps only the VMs that happened not to violate would
    choose its policy using W_eval, which invariant 4 forbids. It is not
    computed.

Usage:
    python3 hypotheses.py --out results/hypotheses \
        --frontier results/theta_intervals/frontier.csv:theta_intervals \
        --frontier results/frontier.csv:theta_1
The first --frontier is the headline (M(eps) as pre-registered includes the
extremal-index correction, so that is T11's intervals run); the rest are
reported beside it as context.
"""

import argparse
import json
import os

import numpy as np
import pandas as pd
from scipy.stats import binom

# docs/HYPOTHESES.txt, section 3. Pre-registered; never an argument.
H2_EPS = (0.001, 0.005, 0.01, 0.05, 0.10)
H2_ECE_BOUND = 0.05
H2_LEVEL = 0.95
H2_MAX_POINTS_OUTSIDE = 1          # null if outside at two or more points
H3_THRESHOLD_PCT = {"cpu": 25.0, "mem": 20.0}

SCOPES = ("all_vms", "non_idle")
RESOURCES = ("cpu", "mem")


def binomial_interval(n, eps, level=H2_LEVEL):
    """Central `level` acceptance region of Binomial(n, eps), in VM counts."""
    a = (1.0 - level) / 2.0
    return int(binom.ppf(a, n, eps)), int(binom.ppf(1.0 - a, n, eps))


def expected_calibration_error(eps, observed):
    """Equal-weighted mean absolute gap between target and observed rate."""
    eps = np.asarray(eps, dtype=float)
    observed = np.asarray(observed, dtype=float)
    return float(np.mean(np.abs(observed - eps)))


def h2_tables(frontier, label):
    """Per-point calibration rows and one summary row per scope x resource."""
    rows, summary = [], []
    evt = frontier[frontier.method == "evt"]
    for scope in SCOPES:
        for res in RESOURCES:
            sub = evt[(evt.scope == scope) & (evt.resource == res)]
            pts = []
            for e in H2_EPS:
                hit = sub[np.isclose(sub.eps.astype(float), e)]
                if hit.empty:
                    raise SystemExit(f"{label}: eps={e} missing for "
                                     f"{scope}/{res} -- H2 needs every "
                                     "pre-registered sweep point")
                r = hit.iloc[0]
                n, k = int(r.vms), int(r.vms_with_violation)
                lo, hi = binomial_interval(n, e)
                inside = lo <= k <= hi
                pts.append((e, k / n))
                rows.append({
                    "frontier": label, "scope": scope, "resource": res,
                    "eps": e, "vms": n, "vms_with_violation": k,
                    "observed": round(k / n, 5),
                    "interval_lo": round(lo / n, 5),
                    "interval_hi": round(hi / n, 5),
                    "inside": inside,
                    "side": ("inside" if inside else
                             "above (anti-conservative)" if k > hi else
                             "below (conservative)"),
                    "abs_gap": round(abs(k / n - e), 5),
                })
            ece = expected_calibration_error(*zip(*pts))
            outside = sum(1 for x in rows[-len(H2_EPS):] if not x["inside"])
            supported = (outside <= H2_MAX_POINTS_OUTSIDE
                         and ece < H2_ECE_BOUND)
            summary.append({
                "frontier": label, "scope": scope, "resource": res,
                "ece": round(ece, 5), "ece_bound": H2_ECE_BOUND,
                "points_outside": outside, "points": len(H2_EPS),
                "verdict": "SUPPORTED" if supported else "NOT SUPPORTED",
                "why": ("" if supported else "; ".join(filter(None, [
                    f"{outside} of {len(H2_EPS)} points outside the 95% "
                    "binomial interval" if outside > H2_MAX_POINTS_OUTSIDE
                    else "",
                    f"ECE {ece:.4f} >= {H2_ECE_BOUND}"
                    if ece >= H2_ECE_BOUND else "",
                ]))),
            })
    return pd.DataFrame(rows), pd.DataFrame(summary)


def h3_table(frontier, label):
    """Largest zero-violation reclamation per scope x resource, and the
    lowest-risk non-B0 point beside it so a reader can see how close the
    nearest miss came."""
    out = []
    for scope in SCOPES:
        for res in RESOURCES:
            sub = frontier[(frontier.scope == scope)
                           & (frontier.resource == res)].copy()
            sub["policy"] = sub.apply(
                lambda r: r.method if r.method != "evt"
                else f"evt(eps={r.eps:g})", axis=1)
            zero = sub[sub.events == 0].sort_values("recl_pct",
                                                    ascending=False)
            best = zero.iloc[0]
            rest = sub[sub.method != "as_provisioned"].sort_values(
                ["vms_with_violation", "recl_pct"], ascending=[True, False])
            near = rest.iloc[0]
            thr = H3_THRESHOLD_PCT[res]
            out.append({
                "frontier": label, "scope": scope, "resource": res,
                "threshold_pct": thr,
                "zero_risk_recl_pct": float(best.recl_pct),
                "zero_risk_policy": best.policy,
                "zero_risk_candidates": ",".join(zero.policy),
                "verdict": ("SUPPORTED" if best.recl_pct >= thr
                            else "NOT SUPPORTED"),
                "nearest_miss_policy": near.policy,
                "nearest_miss_recl_pct": float(near.recl_pct),
                "nearest_miss_vms_violating": int(near.vms_with_violation),
                "nearest_miss_events": int(near.events),
                "vms": int(near.vms),
            })
    return pd.DataFrame(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--frontier", action="append", required=True,
                    metavar="PATH:LABEL",
                    help="frontier.csv from sweep_eps.py, with a label. "
                         "Repeatable; the first is the headline.")
    ap.add_argument("--out", default="results/hypotheses")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    cal, h2s, h3s = [], [], []
    for spec in args.frontier:
        path, _, label = spec.rpartition(":")
        if not path:
            path, label = spec, os.path.basename(os.path.dirname(spec))
        fr = pd.read_csv(path)
        c, s = h2_tables(fr, label)
        cal.append(c), h2s.append(s), h3s.append(h3_table(fr, label))
    cal, h2s, h3s = (pd.concat(x, ignore_index=True) for x in (cal, h2s, h3s))

    cal.to_csv(os.path.join(args.out, "h2_calibration.csv"), index=False)
    h2s.to_csv(os.path.join(args.out, "h2_summary.csv"), index=False)
    h3s.to_csv(os.path.join(args.out, "h3_zero_risk.csv"), index=False)

    head = h2s.frontier.iloc[0]
    with pd.option_context("display.width", 200, "display.max_rows", 200):
        print("=" * 96)
        print("H2 -- CALIBRATION, pre-registered statistic")
        print(f"  ECE = mean |observed - eps| over eps in {list(H2_EPS)}; "
              f"bound {H2_ECE_BOUND}")
        print(f"  interval = central {H2_LEVEL:.0%} region of Binomial(n, eps)"
              f"; null if >= {H2_MAX_POINTS_OUTSIDE + 1} points outside")
        print("=" * 96)
        print(cal[cal.frontier == head][[
            "scope", "resource", "eps", "vms_with_violation", "observed",
            "interval_lo", "interval_hi", "side"]].to_string(index=False))
        print()
        print(h2s.to_string(index=False))
        print("\n" + "=" * 96)
        print("H3 -- ZERO-RISK RECLAMATION, thresholds 25% vCPU-h / 20% GB-h "
              "relative to B0")
        print("=" * 96)
        print(h3s[["frontier", "scope", "resource", "threshold_pct",
                   "zero_risk_recl_pct", "zero_risk_policy", "verdict",
                   "nearest_miss_policy", "nearest_miss_recl_pct",
                   "nearest_miss_vms_violating"]].to_string(index=False))

    with open(os.path.join(args.out, "run_config.json"), "w") as fh:
        json.dump({
            "frontiers": args.frontier,
            "h2_eps": list(H2_EPS), "h2_ece_bound": H2_ECE_BOUND,
            "h2_level": H2_LEVEL,
            "h2_null_if_points_outside_at_least": H2_MAX_POINTS_OUTSIDE + 1,
            "h2_ece_definition": "mean |observed - eps| over the sweep points "
                                 "(equal n per point, so equal to the "
                                 "count-weighted ECE)",
            "h3_threshold_pct": H3_THRESHOLD_PCT,
            "h3_candidates": "every recommender row in each frontier, "
                             "B0 included",
        }, fh, indent=2)
    print(f"\nwrote {args.out}/h2_calibration.csv, h2_summary.csv, "
          "h3_zero_risk.csv, run_config.json")


if __name__ == "__main__":
    main()
