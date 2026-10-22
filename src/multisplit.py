#!/usr/bin/env python3
"""
multisplit.py -- T17: multi-split sensitivity analysis.

A SENSITIVITY ANALYSIS, NOT THE HEADLINE. `docs/HYPOTHESES.txt` pre-registers
one 21/9 split, and that split stays the headline: it is reported on its own
line and never averaged into anything here. The question this script answers
is whether the headline numbers, and in particular the T12 drift finding, are
properties of the method or of days 21-30.

The split plan was approved before the first run (ENVIRONMENT_NOTES.md, "T17"):

  family A  rolling origin. Observation 14 days, evaluation 9 days, origins at
            days 0, 7/3, 14/3 and 7. Observation windows are whole weeks so
            moving the origin does not change how many weekend days the
            recommender sees.
  family B  history length. Evaluation fixed at days 21-30 (the headline's),
            observation 7, 14 and 21 days ending at day 21. B14 is A3 and B21
            is the headline; only B7 is a new run.

The evaluation window is 9 days in every split: eps is a probability over a
horizon of |W_eval|, so changing that length changes what eps means.

WHAT THE SPREAD IS NOT. The splits share VMs, days and a calendar, so they are
not independent replicates. Overlap makes neighbouring results correlated,
which shrinks their spread: the sd across family A is a LOWER BOUND on
between-split variation, not a standard error, and no interval or test is
built on it. Each split's value is printed beside the mean and sd, because
with k = 4 the sd is itself very imprecise. Family B is a dose-response in
history length, not a set of replicates, so it gets no mean or sd at all.

Everything per split goes through the existing code: `sweep_eps.build_fleet`
(idle membership and tail fits recomputed from that split's own observation
window -- invariant 4), `run_frontier`, `matched_risk_reclamation`,
`hypotheses.h2_tables`. Two configs per split:

  reference  theta per VM by the intervals estimator -- M(eps) as
             pre-registered, the frontier H2 and the matched-risk gaps are
             read from.
  theta_off  theta = 1 -- the configuration the T12 low-eps drift diagnostic
             was run in, so the drift test here is the same test.

The headline split must reproduce both published frontiers exactly; a
mismatch stops the run before any summary is written.

Nothing here is random, so there is no --seed.

Usage:
    python3 multisplit.py --traces data/fastStorage --results results \
        --out results/multisplit
"""

import argparse
import json
import os
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ablation import set_theta
from baselines import EVT_MIN_EXCEEDANCES, EVT_THRESHOLD_Q
from bootstrap import same_frontier
from diagnose_low_eps import gpd_upper_endpoint
from hypotheses import h2_tables
from sweep_eps import (
    EPS_SWEEP,
    RESOURCES,
    build_fleet,
    evaluate,
    evt_allocs,
    load_vms,
    matched_risk_reclamation,
    run_frontier,
)

# (name, origin_days, obs_days, eval_days, families). Approved 2026-10-22.
SPLITS = [
    ("headline", 0.0, 21.0, 9.0, ("B",)),
    ("A0", 0.0, 14.0, 9.0, ("A",)),
    ("A1", 7.0 / 3.0, 14.0, 9.0, ("A",)),
    ("A2", 14.0 / 3.0, 14.0, 9.0, ("A",)),
    ("A3", 7.0, 14.0, 9.0, ("A", "B")),
    ("B7", 14.0, 7.0, 9.0, ("B",)),
]
FAMILY_A = ["A0", "A1", "A2", "A3"]
# Family B by observation length; B14 is A3, B21 is the headline.
FAMILY_B = [("B7", "B7"), ("B14", "A3"), ("B21", "headline")]

CONFIGS = {
    "reference": dict(theta_method="intervals", snap=True, no_cap=False),
    "theta_off": dict(theta_method="fixed", snap=True, no_cap=False),
}
PUBLISHED = {
    "reference": "theta_intervals/frontier.csv",
    "theta_off": "frontier.csv",
}
# The strict end of the sweep, where T12 found every memory violator drifting.
DRIFT_EPS = 0.001
SCOPES = ("all_vms", "non_idle")
COMPARATOR = "peak_capped"


def summarise_family(values):
    """(n_defined, mean, sd) over the defined values; sd is the sample sd
    (ddof = 1). Undefined values are counted out, never imputed."""
    v = np.asarray([x for x in values if np.isfinite(x)], dtype=float)
    if len(v) == 0:
        return 0, np.nan, np.nan
    sd = float(np.std(v, ddof=1)) if len(v) > 1 else np.nan
    return len(v), float(np.mean(v)), sd


def drift_rows(fleet, per_vm, split, config, eps):
    """Per violating VM at `eps`: did evaluation demand peak above anything in
    the observation window? Plus, for memory, whether the eval peak sits above
    the finite upper endpoint of a bounded fitted tail -- T12's two tests."""
    rows = []
    for (obs, ev, meta), (_i, r) in zip(fleet, per_vm.iterrows()):
        assert r.vm_id == meta["vm_id"]
        for res, col, _cat, _mr, _oc in RESOURCES:
            if not r[f"{res}_violated"]:
                continue
            fit, _reason, obs_peak = meta["fits"][res]
            eval_peak = float(ev[col].max())
            endpoint = (gpd_upper_endpoint(fit["u"], fit["shape"], fit["scale"])
                        if fit is not None else np.nan)
            rows.append({
                "split": split, "config": config, "eps": eps,
                "resource": res, "vm_id": meta["vm_id"],
                "idle": bool(meta["idle"]),
                "alloc": r[f"{res}_alloc"],
                "fell_back": bool(r[f"{res}_fallback"]),
                "obs_peak": obs_peak, "eval_peak": eval_peak,
                "drift": bool(eval_peak > obs_peak),
                "drift_ratio": (eval_peak / obs_peak if obs_peak > 0
                                else np.nan),
                "shape": fit["shape"] if fit is not None else np.nan,
                "bounded_tail": bool(fit is not None and fit["shape"] < 0),
                "above_endpoint": bool(np.isfinite(endpoint)
                                       and eval_peak > endpoint),
            })
    return rows


def fleet_drift_base_rate(fleet, res, col, idle=None):
    """Share of ALL VMs (violating or not) whose eval peak exceeds their obs
    peak. Without it, "every violator drifts" cannot be told apart from
    "every VM drifts"."""
    hits = [float(ev[col].max()) > m["fits"][res][2]
            for _o, ev, m in fleet if idle is None or m["idle"] == idle]
    return int(sum(hits)), len(hits)


def window_calendar(vms, origin, obs_days, eval_days):
    """Calendar dates and weekend-day counts of each window, from the modal
    first sample (over 90% of traces start at the same moment)."""
    t0 = pd.Series([df["ts"].min() for df, _m in vms]).mode().iloc[0]
    out = {}
    for name, a, b in (("obs", origin, origin + obs_days),
                       ("eval", origin + obs_days,
                        origin + obs_days + eval_days)):
        lo = pd.to_datetime(t0 + a * 86400.0, unit="s", utc=True)
        hi = pd.to_datetime(t0 + b * 86400.0, unit="s", utc=True)
        hours = pd.date_range(lo, hi, freq="h", inclusive="left")
        out[f"{name}_start"] = lo.strftime("%Y-%m-%d %H:%M")
        out[f"{name}_end"] = hi.strftime("%Y-%m-%d %H:%M")
        out[f"{name}_weekend_days"] = round(
            float((hours.dayofweek >= 5).sum()) / 24.0, 2)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--traces", required=True)
    ap.add_argument("--results", default="results",
                    help="where the published headline frontiers live")
    ap.add_argument("--out", default="results/multisplit")
    ap.add_argument("--limit", type=int, default=0,
                    help="smoke test on the first N trace files; the "
                         "reproduction check is then skipped and says so")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    started = time.time()
    print("T17 -- MULTI-SPLIT SENSITIVITY ANALYSIS (not the headline; "
          "docs/HYPOTHESES.txt pre-registers 21/9)")
    print(f"parsing traces from {args.traces} once ...")
    vms = load_vms(args.traces, args.limit)
    print(f"  {len(vms)} VMs pass load_vm")

    plan, frontiers, gaps, ece, drift, fallbacks = [], {}, [], [], [], []
    membership, checks, idle_by_split = [], {}, {}
    for name, origin, obs_days, eval_days, fams in SPLITS:
        fleet, skipped = build_fleet(vms, obs_days, eval_days,
                                     origin_days=origin)
        eval_hours = eval_days * 24.0
        n = len(fleet)
        idle = {m["vm_id"]: bool(m["idle"]) for _o, _e, m in fleet}
        idle_by_split[name] = idle
        n_idle = sum(idle.values())
        plan.append({"split": name, "families": "+".join(fams),
                     "origin_days": round(origin, 4),
                     "obs_days": obs_days, "eval_days": eval_days,
                     "obs_window_days": f"{origin:.2f}-{origin + obs_days:.2f}",
                     "eval_window_days": (f"{origin + obs_days:.2f}-"
                                          f"{origin + obs_days + eval_days:.2f}"),
                     **window_calendar(vms, origin, obs_days, eval_days)})
        membership.append({"split": name, "vms": n, "skipped_short": skipped,
                           "idle": n_idle, "non_idle": n - n_idle})
        print(f"\n[{name}] obs days {origin:.2f}-{origin + obs_days:.2f}, "
              f"eval {origin + obs_days:.2f}-{origin + obs_days + eval_days:.2f}"
              f"  VMs {n} (idle {n_idle} / non-idle {n - n_idle}, "
              f"skipped short {skipped})")

        for res, _c, _cat, _mr, _oc in RESOURCES:
            reasons = pd.Series([m["fits"][res][1] for _o, _e, m in fleet
                                 if m["fits"][res][0] is None])
            for scope, keep in (("all_vms", None), ("non_idle", False)):
                sub = [m for _o, _e, m in fleet
                       if keep is None or m["idle"] == keep]
                nofit = [m["fits"][res][1] for m in sub
                         if m["fits"][res][0] is None]
                fallbacks.append({
                    "split": name, "scope": scope, "resource": res,
                    "vms": len(sub), "no_tail_fit": len(nofit),
                    "reasons": "; ".join(f"{k}: {v}" for k, v in
                                         pd.Series(nofit).value_counts()
                                         .items()) if nofit else "",
                })
            if len(reasons):
                print(f"  {res}: no tail fit (falls back to peak) on "
                      f"{len(reasons)} VMs")

        for config, settings in CONFIGS.items():
            set_theta(fleet, settings)
            fr = run_frontier(fleet, EPS_SWEEP, eval_hours,
                              snap=settings["snap"], no_cap=settings["no_cap"])
            frontiers[(name, config)] = fr
            os.makedirs(os.path.join(args.out, name), exist_ok=True)
            fr.to_csv(os.path.join(args.out, name, f"frontier_{config}.csv"),
                      index=False)
            if name == "headline":
                rel = PUBLISHED[config]
                checks[rel] = ("skipped (--limit)" if args.limit else
                               "identical" if same_frontier(
                                   fr, os.path.join(args.results, rel))
                               else "MISMATCH")
                print(f"  {config}: reproduces {rel}: {checks[rel]}")

            evt = fr[fr.method == "evt"]
            for scope in SCOPES:
                for res, *_r in RESOURCES:
                    sub = evt[(evt.scope == scope) & (evt.resource == res)]
                    m = matched_risk_reclamation(fr, scope, res, COMPARATOR)
                    gaps.append({"split": name, "config": config,
                                 "scope": scope, "resource": res,
                                 "comparator": COMPARATOR,
                                 "comparator_viol_frac": m["baseline_viol_frac"],
                                 "comparator_recl_pct": m["baseline_recl_pct"],
                                 "evt_recl_pct": m["evt_recl_pct"],
                                 "gap_pp": m["gap_pp"], "note": m["note"],
                                 "n_fallback_min": int(sub.n_fallback.min()),
                                 "n_fallback_max": int(sub.n_fallback.max())})
            _cal, summ = h2_tables(fr, f"{name}:{config}")
            summ.insert(0, "split", name)
            summ.insert(1, "config", config)
            ece.append(summ.drop(columns="frontier"))

            per_vm = evaluate(fleet, evt_allocs(DRIFT_EPS, snap=settings["snap"],
                                                no_cap=settings["no_cap"]),
                              eval_hours)
            drift += drift_rows(fleet, per_vm, name, config, DRIFT_EPS)

        if name == "headline" and any(v == "MISMATCH" for v in checks.values()):
            raise SystemExit("the headline split does not reproduce its "
                             "published frontier; refusing to summarise "
                             "sensitivity around a different number")

        base = {}
        for res, col, *_r in RESOURCES:
            for scope, keep in (("all_vms", None), ("non_idle", False)):
                base[(res, scope)] = fleet_drift_base_rate(fleet, res, col, keep)
        membership[-1].update({
            f"fleet_drift_{res}_{scope}": f"{k}/{n_}"
            for (res, scope), (k, n_) in base.items()})

    # --- idle membership against the headline's --------------------------
    head_idle = idle_by_split["headline"]
    for row in membership:
        cur = idle_by_split[row["split"]]
        common = set(cur) & set(head_idle)
        row["idle_flag_differs_from_headline"] = sum(
            cur[v] != head_idle[v] for v in common)
        row["became_non_idle"] = sum(head_idle[v] and not cur[v] for v in common)
        row["became_idle"] = sum(cur[v] and not head_idle[v] for v in common)

    plan = pd.DataFrame(plan)
    membership = pd.DataFrame(membership)
    gaps = pd.DataFrame(gaps)
    ece = pd.concat(ece, ignore_index=True)
    drift = pd.DataFrame(drift)
    fallbacks = pd.DataFrame(fallbacks)

    # --- drift: per split x config x resource x scope ---------------------
    drift_sum = []
    for name, *_s in SPLITS:
        mrow = membership[membership.split == name].iloc[0]
        for config in CONFIGS:
            for res, *_r in RESOURCES:
                for scope in SCOPES:
                    d = drift[(drift.split == name) & (drift.config == config)
                              & (drift.resource == res)] if len(drift) else drift
                    if scope == "non_idle" and len(d):
                        d = d[~d.idle]
                    n_v = len(d)
                    k_d = int(d.drift.sum()) if n_v else 0
                    bt = d[d.bounded_tail] if n_v else d
                    drift_sum.append({
                        "split": name, "config": config, "resource": res,
                        "scope": scope, "eps": DRIFT_EPS,
                        "violators": n_v, "drifting": k_d,
                        "not_drifting": n_v - k_d,
                        "all_violators_drift": bool(n_v and k_d == n_v),
                        "median_drift_ratio": (round(float(
                            d[d.drift].drift_ratio.median()), 3)
                            if k_d else np.nan),
                        "max_drift_ratio": (round(float(
                            d[d.drift].drift_ratio.max()), 3)
                            if k_d else np.nan),
                        "bounded_tail": len(bt),
                        "bounded_above_endpoint": (int(bt.above_endpoint.sum())
                                                   if len(bt) else 0),
                        "fell_back": int(d.fell_back.sum()) if n_v else 0,
                        "fleet_drift_base_rate": mrow[
                            f"fleet_drift_{res}_{scope}"],
                    })
    drift_sum = pd.DataFrame(drift_sum)

    # --- reclamation and violation fraction, every frontier row -----------
    keys = ["config", "scope", "resource", "method", "eps"]
    long = pd.concat([fr.assign(split=n, config=c)
                      for (n, c), fr in frontiers.items()], ignore_index=True)
    long["eps"] = long["eps"].fillna(-1.0)

    def wide(metric):
        w = long.pivot_table(index=keys, columns="split", values=metric,
                             aggfunc="first").reset_index()
        stats = w[FAMILY_A].apply(lambda r: summarise_family(r.to_numpy()),
                                  axis=1)
        w["A_n"] = [s[0] for s in stats]
        w["A_mean"] = [round(s[1], 5) for s in stats]
        w["A_sd"] = [round(s[2], 5) for s in stats]
        for b, src in FAMILY_B:
            w[b] = w[src]
        w = w.rename(columns={"headline": "headline_21_9"})
        w["eps"] = w["eps"].replace(-1.0, np.nan)
        w.insert(len(keys), "metric", metric)
        return w[keys + ["metric"] + FAMILY_A + ["A_n", "A_mean", "A_sd"]
                 + [b for b, _s in FAMILY_B] + ["headline_21_9"]]

    metrics = pd.concat([wide("recl_pct"), wide("viol_frac")],
                        ignore_index=True)

    def wide_from(df, value, index):
        w = df.pivot_table(index=index, columns="split", values=value,
                           aggfunc="first").reset_index()
        stats = w[FAMILY_A].apply(lambda r: summarise_family(r.to_numpy()),
                                  axis=1)
        w["A_defined"] = [f"{s[0]} of {len(FAMILY_A)}" for s in stats]
        w["A_mean"] = [round(s[1], 4) for s in stats]
        w["A_sd"] = [round(s[2], 4) for s in stats]
        for b, src in FAMILY_B:
            w[b] = w[src]
        w["B_defined"] = [f"{int(np.isfinite(r[[b for b, _s in FAMILY_B]].astype(float)).sum())} of {len(FAMILY_B)}"
                          for _i, r in w.iterrows()]
        w = w.rename(columns={"headline": "headline_21_9"})
        return w[index + FAMILY_A + ["A_defined", "A_mean", "A_sd"]
                 + [b for b, _s in FAMILY_B] + ["B_defined", "headline_21_9"]]

    gap_wide = wide_from(gaps, "gap_pp", ["config", "scope", "resource"])
    ece_wide = wide_from(ece, "ece", ["config", "scope", "resource"])

    # --- write -------------------------------------------------------------
    out = lambda f: os.path.join(args.out, f)  # noqa: E731
    plan.to_csv(out("split_plan.csv"), index=False)
    membership.to_csv(out("membership.csv"), index=False)
    fallbacks.to_csv(out("fallbacks.csv"), index=False)
    gaps.to_csv(out("matched_risk_gaps.csv"), index=False)
    gap_wide.to_csv(out("matched_risk_gaps_summary.csv"), index=False)
    ece.to_csv(out("h2_ece.csv"), index=False)
    ece_wide.to_csv(out("h2_ece_summary.csv"), index=False)
    drift.to_csv(out("drift_violators.csv"), index=False)
    drift_sum.to_csv(out("drift_summary.csv"), index=False)
    metrics.to_csv(out("summary.csv"), index=False)

    # --- print -------------------------------------------------------------
    pd.set_option("display.width", 250)
    pd.set_option("display.max_rows", 500)
    pd.set_option("display.max_columns", 50)
    bar = "=" * 110
    print("\n" + bar)
    print("SPLIT PLAN (approved before the first run; ENVIRONMENT_NOTES.md, T17)")
    print(bar)
    print(plan.to_string(index=False))
    print("\nREPRODUCTION -- the headline split against the published frontiers")
    for rel, v in checks.items():
        print(f"  {rel}: {v}")

    print("\n" + bar)
    print("MEMBERSHIP -- idle / non-idle recomputed from each split's own "
          "observation window")
    print(bar)
    print(membership.to_string(index=False))

    print("\n" + bar)
    print("FALLBACKS -- VMs with no usable tail fit (allocation falls back to "
          "observation peak)")
    print(bar)
    print(fallbacks.to_string(index=False))

    print("\n" + bar)
    print("THE SD IS A LOWER BOUND ON BETWEEN-SPLIT VARIATION, NOT A STANDARD "
          "ERROR.")
    print("Splits overlap, so their results are correlated and their spread is "
          "shrunk. No interval")
    print("or test is built on it. Family A: k = 4, sample sd (ddof = 1). "
          "Family B is a history-length")
    print("dose-response and gets no mean or sd. The headline is its own "
          "column and is never averaged in.")
    print(bar)

    print("\nMATCHED-RISK GAP vs peak_capped (pp) -- 'defined on k of n "
          "splits'")
    print(gap_wide.to_string(index=False))
    print("\nH2 ECE over the pre-registered sweep (bound 0.05; see the "
          "threat on this bound's power)")
    print(ece_wide.to_string(index=False))
    print("\nH2 verdict per split (reference config)")
    print(ece[ece.config == "reference"][[
        "split", "scope", "resource", "ece", "points_outside", "verdict"
    ]].to_string(index=False))

    show = metrics[(metrics.config == "reference")
                   & ((metrics.method != "evt")
                      | metrics.eps.isin([0.001, 0.01, 0.1, 0.6]))
                   & (metrics.method != "as_provisioned")]
    for metric in ("recl_pct", "viol_frac"):
        print(f"\n{metric.upper()} -- reference config (full table, both "
              f"configs, every eps: summary.csv)")
        print(show[show.metric == metric].drop(columns=["config", "metric"])
              .to_string(index=False))

    print("\n" + bar)
    print(f"DRIFT -- violators at eps = {DRIFT_EPS:g}: is the eval-window peak "
          "above the obs-window peak?")
    print("fleet_drift_base_rate = share of ALL VMs in scope whose eval peak "
          "exceeds their obs peak")
    print(bar)
    print(drift_sum.to_string(index=False))

    print(f"\nwrote {args.out}/: split_plan, membership, fallbacks, "
          "matched_risk_gaps(+_summary), h2_ece(+_summary), drift_violators, "
          "drift_summary, summary .csv; <split>/frontier_<config>.csv")
    print(f"elapsed {time.time() - started:.0f} s")

    with open(out("run_config.json"), "w") as fh:
        json.dump({
            "task": "T17 multi-split sensitivity analysis (not the headline)",
            "splits": [{"split": n, "origin_days": o, "obs_days": a,
                        "eval_days": b, "families": list(f)}
                       for n, o, a, b, f in SPLITS],
            "family_a": FAMILY_A,
            "family_b": dict(FAMILY_B),
            "configs": CONFIGS,
            "eps_sweep": EPS_SWEEP,
            "drift_eps": DRIFT_EPS,
            "comparator": COMPARATOR,
            "threshold_q": EVT_THRESHOLD_Q,
            "min_exceedances": EVT_MIN_EXCEEDANCES,
            "sd": "sample sd (ddof = 1) over family A only; a lower bound on "
                  "between-split variation, not a standard error; no "
                  "interval or test is built on it",
            "headline_reproduction": checks,
            "limit": args.limit,
            "random": "nothing in this script is random; no seed",
        }, fh, indent=2)


if __name__ == "__main__":
    main()
