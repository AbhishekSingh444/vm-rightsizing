#!/usr/bin/env python3
"""
diagnose_low_eps.py -- why the low end of the eps sweep is anti-conservative,
and whether any extremal index can close the gap to P95.

TWO SECTIONS, both read-only with respect to the method. Nothing here changes
an allocation rule; it re-derives the existing eps = 0.001 arm and then
characterises the VMs that violated anyway.

  DIAGNOSTIC   At eps = 0.001 the measured memory risk is 2.65% against a 0.1%
               target -- 26x anti-conservative. For every VM that violates on
               memory at that eps, report the three explanations that are
               distinguishable from the data:

                 drift        evaluation-window peak demand above the
                              observation-window peak. The recommender never
                              saw the level it was later asked to survive, so
                              no tail model fitted on the observation window
                              could have covered it. This is
                              non-stationarity, not a tail-fit failure.
                 bounded tail a negative fitted shape gives the GPD a FINITE
                              upper endpoint u - scale/xi. Demand above that
                              endpoint is assigned probability exactly zero by
                              the fitted model, so no eps, however small, would
                              have provisioned for it. This is the tail model
                              being confidently wrong, which is worse than
                              being uncertain.
                 idle split   whether the anti-conservatism is concentrated in
                              the idle half of the fleet.

               Also cross-tabulates the CPU fits with xi >= 1 (infinite fitted
               mean) against idle/non-idle, to see whether the explosive CPU
               shapes are a property of near-flat idle series.

  FEASIBILITY  The horizon formula maps eps to a per-sample target
               p = 1 - (1-eps)^(1/(theta*n_eval)). Before implementing T11 it
               is worth knowing whether ANY plausible theta brings that target
               near P95's per-sample level of 0.05. If it does not, H1 as
               pre-registered is not evaluable for this method -- which is a
               reportable result, not a reason to reformulate H1.

Usage:
    python3 diagnose_low_eps.py --traces data/fastStorage --out results
"""

import argparse
import math
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from baselines import FLAVOUR_MEM_GB, count_violations, evt_required_capacity, snap_up
from sweep_eps import load_fleet

# The per-sample quantile P95 operates at, by definition: it provisions the
# 95th percentile, so 5% of observation-window samples sit above it.
P95_PER_SAMPLE = 0.05

THETA_GRID = [1.0, 0.5, 0.2, 0.1, 0.05]


def gpd_upper_endpoint(u, shape, scale):
    """Finite upper endpoint of a fitted GPD, or inf when the tail is unbounded.

    For xi < 0 the Generalized Pareto has support [0, -scale/xi] on the
    excesses, so demand has support up to u - scale/xi and the model assigns
    probability exactly zero above it.
    """
    if shape >= 0:
        return float("inf")
    return float(u) - float(scale) / float(shape)


def diagnostic(fleet, eps, out_dir):
    rows = []
    for obs, ev, meta in fleet:
        fit, reason, obs_peak = meta["fits"]["mem"]
        cap = meta["orig_mem_gb"]

        if fit is None:
            raw, fell_back = obs_peak, True
        else:
            raw = evt_required_capacity(fit, eps, meta["n_eval"], theta=1.0)
            fell_back = False
            if not np.isfinite(raw):
                raw, fell_back = obs_peak, True
        alloc = snap_up(raw, FLAVOUR_MEM_GB)
        capped = alloc > cap
        if capped:
            alloc = float(cap)

        events, samples = count_violations(ev["mem_gb"], alloc, 1)
        if events == 0:
            continue

        eval_peak = float(ev["mem_gb"].max())
        over = ev["mem_gb"][ev["mem_gb"] > alloc]
        endpoint = (
            gpd_upper_endpoint(fit["u"], fit["shape"], fit["scale"])
            if fit is not None
            else float("nan")
        )
        rows.append(
            {
                "vm_id": meta["vm_id"],
                "idle": bool(meta["idle"]),
                "orig_mem_gb": cap,
                "alloc_gb": alloc,
                "capped_at_orig": bool(capped),
                "fell_back": bool(fell_back),
                "fallback_reason": reason,
                "obs_peak_gb": obs_peak,
                "eval_peak_gb": eval_peak,
                "drift": bool(eval_peak > obs_peak),
                "drift_ratio": eval_peak / obs_peak if obs_peak > 0 else float("nan"),
                "shape": fit["shape"] if fit is not None else float("nan"),
                "scale": fit["scale"] if fit is not None else float("nan"),
                "u": fit["u"] if fit is not None else float("nan"),
                "n_exceed": fit["n_exceed"] if fit is not None else 0,
                "gpd_upper_endpoint_gb": endpoint,
                # The violating demand the model called impossible.
                "max_violating_gb": float(over.max()) if len(over) else float("nan"),
                "above_endpoint": bool(np.isfinite(endpoint) and eval_peak > endpoint),
                "n_violating_samples": int(samples),
                "events": int(events),
            }
        )

    d = pd.DataFrame(rows)
    path = os.path.join(out_dir, "diag_low_eps_mem_violators.csv")
    d.to_csv(path, index=False)

    n_fleet = len(fleet)
    n = len(d)
    print("=" * 78)
    print(f"DIAGNOSTIC -- memory violators at eps = {eps:g}, theta = 1")
    print("=" * 78)
    print(f"fleet: {n_fleet} VMs   violating on memory: {n} "
          f"({100.0 * n / n_fleet:.2f}% measured, target {100.0 * eps:.2f}%)")
    if n == 0:
        return d

    # Bracket access throughout: `d.shape` is the DataFrame's own attribute.
    neg = d[d["shape"] < 0]
    print(f"\n  drift (eval peak > obs peak):        {int(d['drift'].sum())}/{n}")
    if d["drift"].any():
        dr = d[d["drift"]]["drift_ratio"]
        print(f"      drift ratio eval/obs peak:       median {dr.median():.3f}  "
              f"max {dr.max():.3f}")
    print(f"  no drift (eval peak <= obs peak):    {int((~d['drift']).sum())}/{n}")
    print(f"\n  negative fitted shape (bounded tail): {len(neg)}/{n}")
    print(f"      of those, eval peak above the fitted finite upper endpoint: "
          f"{int(neg['above_endpoint'].sum())}/{len(neg)}")
    print(f"  non-negative shape (unbounded tail):  {int((d['shape'] >= 0).sum())}/{n}")

    idle_n = int(d["idle"].sum())
    fleet_idle = sum(1 for _o, _e, m in fleet if m["idle"])
    fleet_nonidle = n_fleet - fleet_idle
    print(f"\n  idle:     {idle_n}/{n}  "
          f"({100.0 * idle_n / fleet_idle:.2f}% of the {fleet_idle} idle VMs)")
    print(f"  non-idle: {n - idle_n}/{n}  "
          f"({100.0 * (n - idle_n) / fleet_nonidle:.2f}% of the "
          f"{fleet_nonidle} non-idle VMs)")

    print(f"\n  allocation capped at original:       {int(d['capped_at_orig'].sum())}/{n}")
    print(f"  fell back to peak:                   {int(d['fell_back'].sum())}/{n}")

    print("\n  cross-tab, drift x bounded tail:")
    ct = pd.crosstab(d["drift"], d["shape"] < 0)
    ct.index.name = "drift"
    ct.columns.name = "shape<0"
    print("    " + ct.to_string().replace("\n", "\n    "))

    print("\n  per-VM detail:")
    cols = ["vm_id", "idle", "orig_mem_gb", "alloc_gb", "obs_peak_gb", "eval_peak_gb",
            "drift", "shape", "gpd_upper_endpoint_gb", "above_endpoint",
            "n_violating_samples"]
    with pd.option_context("display.width", 200, "display.max_rows", 300):
        print("    " + d[cols].to_string(index=False).replace("\n", "\n    "))

    # --- CPU shape >= 1 against idle/non-idle -------------------------------
    cpu_rows = [
        {"vm_id": m["vm_id"], "idle": bool(m["idle"]),
         "shape": m["fits"]["cpu"][0]["shape"]}
        for _o, _e, m in fleet if m["fits"]["cpu"][0] is not None
    ]
    cpu = pd.DataFrame(cpu_rows)
    cpu["infinite_mean"] = cpu["shape"] >= 1.0
    cpu.to_csv(os.path.join(out_dir, "diag_cpu_shapes_idle.csv"), index=False)

    print("\n" + "-" * 78)
    print("CPU fits with xi >= 1 (infinite fitted mean), against idle/non-idle")
    print("-" * 78)
    ct = pd.crosstab(cpu["idle"], cpu["infinite_mean"], margins=True)
    ct.index.name = "idle"
    ct.columns.name = "xi>=1"
    print(ct.to_string())
    for flag, label in ((True, "idle"), (False, "non-idle")):
        sub = cpu[cpu["idle"] == flag]
        if len(sub):
            print(f"  {label}: {int(sub['infinite_mean'].sum())}/{len(sub)} fitted VMs "
                  f"have xi >= 1 ({100.0 * sub['infinite_mean'].mean():.1f}%); "
                  f"median xi {sub['shape'].median():.3f}, max {sub['shape'].max():.3f}")
    return d


def feasibility(fleet, out_dir):
    n_evals = sorted(m["n_eval"] for _o, _e, m in fleet)
    n_med = int(np.median(n_evals))

    print("\n" + "=" * 78)
    print("FEASIBILITY -- can any theta bring M(eps) to P95's per-sample level?")
    print("=" * 78)
    print(f"n_eval over the fleet: median {n_med}, min {n_evals[0]}, max {n_evals[-1]}")
    print(f"P95 operates at a per-sample exceedance target of "
          f"p = {P95_PER_SAMPLE:g} (a 1-in-{1 / P95_PER_SAMPLE:.0f} quantile).\n")

    rows = []
    for theta in THETA_GRID:
        trials = theta * n_med
        # Per-sample target at the loosest eps the sweep uses.
        p_at_06 = 1.0 - (1.0 - 0.6) ** (1.0 / trials)
        # eps that would put the per-sample target at P95's 0.05.
        # 1 - (1-eps)^(1/trials) = 0.05  =>  eps = 1 - 0.95^trials
        log1m = trials * math.log1p(-P95_PER_SAMPLE)
        eps_for_p95 = -math.expm1(log1m)  # = 1 - 0.95**trials, accurately
        rows.append({
            "theta": theta,
            "effective_trials": round(trials, 2),
            "p_target_at_eps_0.6": p_at_06,
            "quantile_at_eps_0.6": f"1-in-{1.0 / p_at_06:,.0f}",
            "x_more_extreme_than_p95": round(P95_PER_SAMPLE / p_at_06, 1),
            "eps_to_reach_p95": eps_for_p95,
            "1_minus_eps_to_reach_p95": math.exp(log1m),
        })

    t = pd.DataFrame(rows)
    t.to_csv(os.path.join(out_dir, "theta_feasibility.csv"), index=False)
    with pd.option_context("display.width", 200, "display.float_format",
                           lambda v: f"{v:.6g}"):
        print(t.to_string(index=False))

    # The theta that would land exactly on P95's level at eps = 0.6.
    trials_needed = math.log(1.0 - 0.6) / math.log(1.0 - P95_PER_SAMPLE)
    theta_needed = trials_needed / n_med
    print(f"\nTo put the per-sample target at P95's 0.05 while holding eps = 0.6 "
          f"(the loosest\neps in the sweep) would need theta = "
          f"{trials_needed:.2f}/{n_med} = {theta_needed:.5f}.")
    print("The extremal index is a probability in (0, 1]; a value that small means "
          "each\nindependent extreme episode spans roughly "
          f"{1.0 / theta_needed:,.0f} samples "
          f"({1.0 / theta_needed * 5 / 60:,.0f} hours) of a 9-day window.")
    return t


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--traces", required=True)
    ap.add_argument("--out", default="results")
    ap.add_argument("--obs-days", type=float, default=21.0)
    ap.add_argument("--eval-days", type=float, default=9.0)
    ap.add_argument("--eps", type=float, default=0.001)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    fleet, skipped = load_fleet(args.traces, args.obs_days, args.eval_days,
                                limit=args.limit)
    print(f"usable VMs: {len(fleet)} (skipped short: {skipped})\n")

    diagnostic(fleet, args.eps, args.out)
    feasibility(fleet, args.out)
    print(f"\nwrote {args.out}/diag_low_eps_mem_violators.csv, "
          f"diag_cpu_shapes_idle.csv, theta_feasibility.csv")


if __name__ == "__main__":
    main()
