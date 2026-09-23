#!/usr/bin/env python3
"""
sweep_eps.py -- T10: the eps sweep and the comparative frontier.

Sweeps M(eps) over a range of target exhaustion probabilities and reports, for
every eps, BOTH axes of the trade: reclaimed capacity and the OBSERVED fraction
of VMs with at least one violation in the evaluation window.

Reporting observed risk beside reclamation is the whole point. CLAUDE.md
invariant 7 compares at matched MEASURED risk, never at matched eps -- eps is a
target the method aims at, and whether it hits it is H2's question, not
something to assume while evaluating H1.

The baselines are emitted as rows in the same table on the same axes, so a
reader can see directly whether any baseline DOMINATES an M(eps) point:
lower-or-equal measured risk AND higher-or-equal reclamation. A method that is
dominated has not earned its complexity.

`--theta-method fixed` (the default, with `--theta 1`) applies no dependence
correction and is the "correction off" arm of the T12 ablation. T11 adds
`--theta-method intervals` (Ferro and Segers, 2003 -- primary, no free
parameter) and `--theta-method runs` (declustering at `--run-length`, fixed at
12 samples = one hour, as the sensitivity check). theta is estimated per VM per
resource on the OBSERVATION window only.

Usage:
    python3 sweep_eps.py --traces data/fastStorage --out results
    python3 sweep_eps.py --traces data/fastStorage --out results/unsnapped --unsnapped
    python3 sweep_eps.py --traces data/fastStorage --out results/theta_intervals \
        --theta-method intervals
"""

import argparse
import json
import glob
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from baselines import (
    CPU_VIOLATION_MIN_RUN,
    EVT_DEFAULT_RUN_LENGTH,
    EVT_DEFAULT_THETA,
    EVT_MIN_EXCEEDANCES,
    EVT_THRESHOLD_Q,
    FLAVOUR_MEM_GB,
    FLAVOUR_VCPU,
    SAMPLE_MINUTES,
    count_violations,
    estimate_theta,
    evt_required_capacity,
    fit_gpd_tail,
    load_vm,
    recommend,
    snap_up,
    split_obs_eval,
)

# TASKS.md T10. The upper values exist so the curve reaches P95's measured
# violation rate; without them the matched-risk comparison H1 requires would be
# an extrapolation rather than a measurement.
EPS_SWEEP = [0.001, 0.005, 0.01, 0.05, 0.1, 0.2, 0.4, 0.6]

RESOURCES = (
    # (name, demand column, catalog, violation min_run, orig column)
    ("cpu", "cpu_cores", FLAVOUR_VCPU, CPU_VIOLATION_MIN_RUN, "orig_cores"),
    ("mem", "mem_gb", FLAVOUR_MEM_GB, 1, "orig_mem_gb"),
)


def load_fleet(traces, obs_days, eval_days, limit=0,
               threshold_q=EVT_THRESHOLD_Q,
               min_exceedances=EVT_MIN_EXCEEDANCES):
    """Load and split every usable VM once.

    The GPD fit does not depend on eps, so it is done here too: sweeping eps
    then costs one inversion per VM per resource instead of one refit.
    """
    paths = sorted(glob.glob(os.path.join(traces, "*.csv")))
    if limit:
        paths = paths[:limit]
    if not paths:
        raise SystemExit(f"no CSVs found in {traces}")

    fleet = []
    skipped_short = 0
    for path in paths:
        loaded = load_vm(path)
        if loaded is None:
            continue
        df, meta = loaded
        split = split_obs_eval(df, obs_days, eval_days)
        if split is None:
            skipped_short += 1
            continue
        obs, ev = split
        if obs.empty or ev.empty:
            skipped_short += 1
            continue

        p99_cpu = obs["cpu_cores"].quantile(0.99)
        meta["idle"] = (
            (p99_cpu / meta["orig_cores"]) < 0.05 if meta["orig_cores"] else True
        )

        # Horizon length from the OBSERVATION window's own median sampling
        # gap -- never from the evaluation window (CLAUDE.md invariant 6).
        gap = obs["ts"].diff().median()
        if np.isfinite(gap) and gap > 0:
            meta["n_eval"] = max(1, int(round(eval_days * 86400.0 / float(gap))))
        else:
            meta["n_eval"] = int(eval_days * 24 * 60 / SAMPLE_MINUTES)

        meta["fits"] = {}
        for res, col, _cat, _mr, _oc in RESOURCES:
            fit, reason = fit_gpd_tail(
                obs[col].to_numpy(),
                threshold_q=threshold_q,
                min_exceedances=min_exceedances,
            )
            meta["fits"][res] = (fit, reason, float(obs[col].max()))

        fleet.append((obs, ev, meta))

    return fleet, skipped_short


def baseline_allocs(policy, snap=True, cap=False):
    """`cap=True` is a LIKE-FOR-LIKE diagnostic, not a redefinition of the
    Review 1 baselines. M(eps) is capped at the original allocation, so
    comparing it against an uncapped baseline is not a fair comparison:
    upward snapping pushes `peak` above the original allocation on a large
    minority of VMs, booking negative reclamation the cap would have
    removed. Violations are unaffected -- a VM allocated at or above its
    original cannot violate, since load_vm clipped demand there -- so the
    capped rows move only the reclamation axis. Both versions are reported
    and the dominance test is run against both.
    """
    def f(meta, obs):
        cpu, mem = recommend(obs, policy, snap=snap)
        c_cap, m_cap = False, False
        if cap:
            if cpu > meta["orig_cores"]:
                cpu, c_cap = meta["orig_cores"], True
            if mem > meta["orig_mem_gb"]:
                mem, m_cap = meta["orig_mem_gb"], True
        return {"cpu": (cpu, False, c_cap), "mem": (mem, False, m_cap)}
    return f


def provisioned_allocs(meta, obs):
    return {
        "cpu": (meta["orig_cores"], False, False),
        "mem": (meta["orig_mem_gb"], False, False),
    }


def evt_allocs(eps, snap=True, no_cap=False):
    """M(eps) allocations for one eps, as an `allocs_for_vm` callable.

    Module level, and the ONLY implementation: `main()` and `ablation.py`
    both go through it, so an ablation arm can never quietly differ from the
    headline sweep in how an allocation is formed.

    Reads `meta["fits"]`, `meta["theta"]` and `meta["n_eval"]`, all of which
    were derived from the observation window alone.
    """
    def f(meta, obs):
        got = {}
        for res, _col, catalog, _mr, orig_col in RESOURCES:
            fit, _reason, peak = meta["fits"][res]
            cap = meta[orig_col]
            if fit is None:
                raw, fell_back = peak, True
            else:
                raw = evt_required_capacity(
                    fit, eps, meta["n_eval"], theta=meta["theta"][res]
                )
                fell_back = False
                if not np.isfinite(raw):
                    raw, fell_back = peak, True
            alloc = snap_up(raw, catalog) if snap else float(raw)
            # `capped` is recorded whether or not the cap is APPLIED, so the
            # no-cap arm still reports how often the fitted tail left the
            # support of the distribution it was fitted to.
            capped = alloc > cap
            if capped and not no_cap:
                alloc = float(cap)
            got[res] = (alloc, fell_back, capped)
        return got
    return f


def run_frontier(fleet, eps_sweep, eval_hours, snap=True, no_cap=False):
    """Baselines plus the whole M(eps) sweep over an already-loaded fleet,
    returned as a frontier DataFrame. Factored out so an ablation arm is one
    call against a fleet loaded once, rather than a re-parse of 1,250 CSVs.

    The fleet must already carry `meta["theta"]` (see `attach_theta`).
    """
    frontier = []
    for method, fn in (
        ("as_provisioned", provisioned_allocs),
        ("peak", baseline_allocs("peak", snap=snap)),
        ("p95", baseline_allocs("p95", snap=snap)),
        ("peak_capped", baseline_allocs("peak", snap=snap, cap=True)),
        ("p95_capped", baseline_allocs("p95", snap=snap, cap=True)),
    ):
        per_vm = evaluate(fleet, fn, eval_hours)
        frontier += summarise(per_vm, "all_vms", method, np.nan, eval_hours)
        frontier += summarise(per_vm[~per_vm["idle"]], "non_idle", method,
                              np.nan, eval_hours)
    for eps in eps_sweep:
        per_vm = evaluate(fleet, evt_allocs(eps, snap=snap, no_cap=no_cap),
                          eval_hours)
        frontier += summarise(per_vm, "all_vms", "evt", eps, eval_hours)
        frontier += summarise(per_vm[~per_vm["idle"]], "non_idle", "evt", eps,
                              eval_hours)
    return pd.DataFrame(frontier)


def matched_risk_reclamation(frontier, scope, resource, baseline="peak_capped"):
    """Reclamation the M(eps) curve achieves at the BASELINE's measured risk.

    This is the primary T12 number, so it lives in one tested function rather
    than being re-derived per arm. Returns a dict with the baseline's own
    (risk, reclamation), the interpolated M(eps) reclamation at that same
    measured risk, and the gap in percentage points.

    The sweep samples eps at eight points, so the reading is a LINEAR
    INTERPOLATION along the (measured risk, reclamation) curve between the two
    bracketing points -- not a measured operating point, and reported as such.
    When the baseline's risk falls outside the swept range there is nothing to
    interpolate between and the gap is NaN with a note; extrapolating off the
    end of a frontier would invent an operating point the method was never
    observed at.

    `baseline` names the comparator explicitly because it is not always
    `peak_capped`: an arm in which M(eps) is uncapped has to be read against
    uncapped `peak` to be like-for-like.
    """
    sub = frontier[(frontier.scope == scope) & (frontier.resource == resource)]
    base = sub[sub.method == baseline]
    evt = sub[sub.method == "evt"].sort_values("viol_frac")
    if base.empty or evt.empty:
        return {"baseline": baseline, "baseline_viol_frac": np.nan,
                "baseline_recl_pct": np.nan, "evt_recl_pct": np.nan,
                "gap_pp": np.nan, "note": "baseline or M(eps) rows missing"}

    b = base.iloc[0]
    x = evt.viol_frac.to_numpy(dtype=float)
    y = evt.recl_pct.to_numpy(dtype=float)
    target = float(b.viol_frac)
    out = {"baseline": baseline, "baseline_viol_frac": target,
           "baseline_recl_pct": float(b.recl_pct)}

    if target < x.min() or target > x.max():
        out.update({"evt_recl_pct": np.nan, "gap_pp": np.nan,
                    "note": ("matched risk outside the swept M(eps) range "
                             f"[{x.min():.5f}, {x.max():.5f}]")})
        return out

    interp = float(np.interp(target, x, y))
    out.update({"evt_recl_pct": round(interp, 3),
                "gap_pp": round(interp - float(b.recl_pct), 3),
                "note": "linear interpolation between bracketing eps points"})
    return out


def attach_theta(fleet, method, run_length):
    """Estimate the extremal index per VM per resource, OBSERVATION WINDOW
    ONLY, at the same threshold u the GPD was fitted over.

    A VM with no usable tail fit has no u to decluster against; it falls back
    to peak anyway, so theta never enters its allocation. A VM whose estimator
    cannot return a value takes theta = 1, which is the conservative
    direction (more effective trials -> larger allocation), and is counted.
    """
    rows = []
    for _obs, _ev, meta in fleet:
        meta["theta"] = {}
        for res, col, _cat, _mr, _oc in RESOURCES:
            fit, _reason, _peak = meta["fits"][res]
            if fit is None:
                meta["theta"][res] = EVT_DEFAULT_THETA
                rows.append({"vm_id": meta["vm_id"], "resource": res,
                             "idle": meta["idle"], "theta": np.nan,
                             "theta_used": EVT_DEFAULT_THETA,
                             "status": "no_fit", "u": np.nan, "n_exceed": 0,
                             "n_eval": meta["n_eval"],
                             "effective_trials": np.nan})
                continue
            th, why = estimate_theta(_obs[col].to_numpy(), fit["u"],
                                     method=method, run_length=run_length)
            used = EVT_DEFAULT_THETA if th is None else th
            meta["theta"][res] = used
            rows.append({"vm_id": meta["vm_id"], "resource": res,
                         "idle": meta["idle"],
                         "theta": np.nan if th is None else th,
                         "theta_used": used,
                         "status": "ok" if th is not None else why,
                         "u": fit["u"], "n_exceed": fit["n_exceed"],
                         "n_eval": meta["n_eval"],
                         "effective_trials": used * meta["n_eval"]})
    return pd.DataFrame(rows)


def evaluate(fleet, allocs_for_vm, eval_hours):
    """Replay one policy over the fleet. `allocs_for_vm(meta, obs)` returns
    {resource: (allocation, fell_back, capped)}.

    Returns a per-VM DataFrame. Violation counting goes through the existing
    tested `count_violations`, with each resource's own min_run -- the
    asymmetry between the memory rule (any single sample) and the CPU rule
    (two consecutive) is CLAUDE.md invariant 1 and is not re-implemented here.
    """
    rows = []
    for obs, ev, meta in fleet:
        got = allocs_for_vm(meta, obs)
        row = {"vm_id": meta["vm_id"], "idle": meta["idle"]}
        for res, col, _cat, min_run, orig_col in RESOURCES:
            alloc, fell_back, capped = got[res]
            events, samples = count_violations(ev[col], alloc, min_run)
            orig = meta[orig_col]
            row[f"{res}_alloc"] = alloc
            row[f"{res}_orig"] = orig
            row[f"{res}_recl_hours"] = (orig - alloc) * eval_hours
            row[f"{res}_events"] = events
            row[f"{res}_exhausted_minutes"] = samples * SAMPLE_MINUTES
            row[f"{res}_violated"] = events > 0
            row[f"{res}_fallback"] = fell_back
            row[f"{res}_capped"] = capped
        rows.append(row)
    return pd.DataFrame(rows)


def summarise(per_vm, scope_label, method, eps, eval_hours):
    out = []
    for res, _col, _cat, _mr, _orig in RESOURCES:
        n = len(per_vm)
        if n == 0:
            continue
        total_alloc = per_vm[f"{res}_orig"].sum() * eval_hours
        recl = per_vm[f"{res}_recl_hours"].sum()
        violated = per_vm[f"{res}_violated"]
        out.append(
            {
                "scope": scope_label,
                "resource": res,
                "method": method,
                "eps": eps,
                "vms": n,
                "recl_hours": round(float(recl), 1),
                "recl_pct": round(100.0 * recl / total_alloc, 3) if total_alloc else 0.0,
                # THE RISK AXIS: measured, not predicted.
                "vms_with_violation": int(violated.sum()),
                "viol_frac": round(float(violated.mean()), 5),
                "events": int(per_vm[f"{res}_events"].sum()),
                "exhausted_minutes": int(per_vm[f"{res}_exhausted_minutes"].sum()),
                "n_fallback": int(per_vm[f"{res}_fallback"].sum()),
                "n_capped": int(per_vm[f"{res}_capped"].sum()),
            }
        )
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--traces", required=True, help="directory of per-VM CSVs")
    ap.add_argument("--out", default="results")
    ap.add_argument("--obs-days", type=float, default=21.0)
    ap.add_argument("--eval-days", type=float, default=9.0)
    ap.add_argument("--theta", type=float, default=EVT_DEFAULT_THETA,
                    help="fixed extremal index, used when --theta-method is "
                         "'fixed'. 1.0 = no dependence correction, which is "
                         "the 'correction off' arm of T12")
    ap.add_argument("--theta-method", choices=("fixed", "intervals", "runs"),
                    default="fixed",
                    help="T11: how theta is obtained per VM per resource. "
                         "'intervals' is Ferro and Segers (2003), the primary "
                         "method (no free parameter); 'runs' is declustering "
                         "at --run-length, the sensitivity check; 'fixed' "
                         "uses --theta for every VM (the T12 correction-off "
                         "arm). Estimated on the observation window only.")
    ap.add_argument("--no-cap", action="store_true",
                    help="T12 ablation arm: do NOT cap the M(eps) allocation "
                         "at the VM's original allocation. The cap follows "
                         "from the data model (load_vm clips demand to "
                         "a_orig), so this arm is a diagnostic that measures "
                         "how much of M(eps)'s position the cap is holding "
                         "up -- not an alternative policy. Write it to its "
                         "own --out directory.")
    ap.add_argument("--run-length", type=int, default=EVT_DEFAULT_RUN_LENGTH,
                    help="runs-declustering run length in samples. Fixed at "
                         "12 (one hour) before any T11 result existed; do not "
                         "tune it.")
    ap.add_argument("--threshold-q", type=float, default=EVT_THRESHOLD_Q)
    ap.add_argument("--min-exceedances", type=int, default=EVT_MIN_EXCEEDANCES)
    ap.add_argument("--eps", type=float, nargs="+", default=EPS_SWEEP)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--unsnapped", action="store_true",
                    help="DIAGNOSTIC: continuous allocations instead of "
                         "catalog flavours. Required by invariant 7, which "
                         "asks for every H1 result both snapped and "
                         "unsnapped. Write to a separate --out directory.")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    snap = not args.unsnapped
    eval_hours = args.eval_days * 24.0

    print(f"loading traces from {args.traces} "
          f"({args.obs_days:g} obs / {args.eval_days:g} eval days)")
    fleet, skipped_short = load_fleet(
        args.traces, args.obs_days, args.eval_days, limit=args.limit,
        threshold_q=args.threshold_q, min_exceedances=args.min_exceedances,
    )
    n_vms = len(fleet)
    n_idle = sum(1 for _o, _e, m in fleet if m["idle"])
    print(f"usable VMs: {n_vms}  (idle {n_idle} / non-idle {n_vms - n_idle}, "
          f"skipped short: {skipped_short})")
    if args.theta_method == "fixed":
        print(f"snapped: {snap}   theta: fixed {args.theta:g}   "
              f"u: q{args.threshold_q:g}   eps: {args.eps}")
        for _o, _e, m in fleet:
            m["theta"] = {res: args.theta for res, *_r in RESOURCES}
        theta_df = pd.DataFrame(
            [{"vm_id": m["vm_id"], "resource": res, "idle": m["idle"],
              "theta": args.theta, "theta_used": args.theta, "status": "fixed",
              "u": np.nan, "n_exceed": 0, "n_eval": m["n_eval"],
              "effective_trials": args.theta * m["n_eval"]}
             for _o, _e, m in fleet for res, *_r in RESOURCES]
        )
    else:
        print(f"snapped: {snap}   theta: per-VM via {args.theta_method}"
              f"{f' (r={args.run_length})' if args.theta_method == 'runs' else ''}"
              f"   u: q{args.threshold_q:g}   eps: {args.eps}")
        theta_df = attach_theta(fleet, args.theta_method, args.run_length)

    theta_df.to_csv(os.path.join(args.out, "theta_per_vm.csv"), index=False)
    print("\n" + "=" * 96)
    print(f"EXTREMAL INDEX theta -- per VM, observation window only "
          f"(method: {args.theta_method})")
    print("=" * 96)
    for res, _c, _cat, _mr, _oc in RESOURCES:
        sub = theta_df[(theta_df.resource == res) & (theta_df.status == "ok")]
        bad = theta_df[(theta_df.resource == res) & (theta_df.status != "ok")]
        if len(sub):
            s = sub["theta"]
            print(f"  {res}: n={len(s)} estimated   min={s.min():.4f} "
                  f"q25={s.quantile(.25):.4f} median={s.median():.4f} "
                  f"q75={s.quantile(.75):.4f} max={s.max():.4f}")
            et = sub["effective_trials"]
            print(f"       effective trials (theta * n_eval): "
                  f"median={et.median():.1f} min={et.min():.1f} max={et.max():.1f} "
                  f"(uncorrected n_eval median 2592)")
            for lo, hi in ((0.0, 0.05), (0.05, 0.1), (0.1, 0.25),
                           (0.25, 0.5), (0.5, 1.0000001)):
                n_in = int(((s >= lo) & (s < hi)).sum())
                print(f"         {lo:.2f} <= theta < {hi:.2f}: {n_in}")
        if len(bad):
            tally = bad["status"].value_counts().to_dict()
            print(f"       not estimated, theta=1 used: {len(bad)} {tally}")

    # --- baselines and the M(eps) sweep, on the same axes ------------------
    # Both go through the module-level helpers, which are also what
    # src/ablation.py calls -- one implementation, so no arm can drift.
    fr = run_frontier(fleet, args.eps, eval_hours, snap=snap, no_cap=args.no_cap)
    print(f"  {len(args.eps)} M(eps) points and 5 baselines: done")

    shape_rows = []

    for res, _col, _cat, _mr, _oc in RESOURCES:
        shapes = [m["fits"][res][0]["shape"] for _o, _e, m in fleet
                  if m["fits"][res][0] is not None]
        shape_rows += [{"resource": res, "vm_id": m["vm_id"],
                        "shape": m["fits"][res][0]["shape"],
                        "scale": m["fits"][res][0]["scale"],
                        "u": m["fits"][res][0]["u"],
                        "n_exceed": m["fits"][res][0]["n_exceed"]}
                       for _o, _e, m in fleet if m["fits"][res][0] is not None]
        if shapes:
            s = pd.Series(shapes)
            print(f"\nGPD shape xi ({res}): n={len(s)} min={s.min():.3f} "
                  f"q25={s.quantile(.25):.3f} median={s.median():.3f} "
                  f"q75={s.quantile(.75):.3f} max={s.max():.3f}")
            print(f"    xi<0 (bounded tail): {(s < 0).sum()}   "
                  f"xi>=0.5 (infinite variance): {(s >= 0.5).sum()}   "
                  f"xi>=1 (infinite mean): {(s >= 1).sum()}")

    fr.to_csv(os.path.join(args.out, "frontier.csv"), index=False)
    pd.DataFrame(shape_rows).to_csv(
        os.path.join(args.out, "gpd_shapes.csv"), index=False
    )

    # --- dominance: does any baseline beat M(eps) on BOTH axes? ------------
    dom_rows = []
    for scope in ("all_vms", "non_idle"):
        for res, _c, _cat, _mr, _oc in RESOURCES:
            sub = fr[(fr.scope == scope) & (fr.resource == res)]

            def dominators_among(names, row):
                b = sub[sub.method.isin(names)]
                hit = b[
                    (b.viol_frac <= row.viol_frac + 1e-12)
                    & (b.recl_pct >= row.recl_pct - 1e-12)
                ]
                return ",".join(sorted(hit.method)) or "none"

            for _i, row in sub[sub.method == "evt"].iterrows():
                dom_rows.append({
                    "scope": scope, "resource": res, "eps": row.eps,
                    "recl_pct": row.recl_pct, "viol_frac": row.viol_frac,
                    # As published in Review 1 (uncapped).
                    "dominated_by": dominators_among(["peak", "p95"], row),
                    # Like-for-like: both sides capped at the original
                    # allocation. This is the honest comparison.
                    "dominated_by_capped": dominators_among(
                        ["peak_capped", "p95_capped"], row),
                })
    dom = pd.DataFrame(dom_rows)
    dom.to_csv(os.path.join(args.out, "dominance.csv"), index=False)

    # --- calibration: eps TARGET against MEASURED risk ----------------------
    # eps is what the method aims at. viol_frac is what happened. The ratio is
    # the honest statement of whether the target means anything; it is H2's
    # question and is reported, never used to adjust eps.
    cal_rows = []
    for scope in ("all_vms", "non_idle"):
        for res, _c, _cat, _mr, _oc in RESOURCES:
            sub = fr[(fr.scope == scope) & (fr.resource == res)
                     & (fr.method == "evt")].sort_values("eps")
            for _i, row in sub.iterrows():
                cal_rows.append({
                    "scope": scope, "resource": res, "eps_target": row.eps,
                    "measured_viol_frac": row.viol_frac,
                    "vms_with_violation": int(row.vms_with_violation),
                    "vms": int(row.vms),
                    "ratio_measured_over_target": round(
                        float(row.viol_frac) / float(row.eps), 2) if row.eps else np.nan,
                    "direction": ("anti-conservative"
                                  if row.viol_frac > row.eps else "conservative"),
                    "recl_pct": row.recl_pct,
                })
    cal = pd.DataFrame(cal_rows)
    cal.to_csv(os.path.join(args.out, "eps_calibration.csv"), index=False)

    # --- EXPLORATORY -- NOT PRE-REGISTERED ----------------------------------
    # Reclamation at MATCHED MEASURED RISK against peak_capped, the baseline
    # the M(eps) frontier actually overlaps. This is NOT H1: H1 names p95 as
    # the comparator, p95 sits at a measured risk the M(eps) curve never
    # reaches, and invariant 7 forbids substituting a different comparator to
    # manufacture a comparison. This is reported as an exploratory
    # observation about where the two curves do meet, and nothing in the
    # pre-registered hypothesis set is adjusted in light of it.
    #
    # The M(eps) curve is sampled at eight eps values, so a matched-risk
    # reading is a linear interpolation between the two bracketing points on
    # the (measured risk, reclamation) curve -- stated as an interpolation,
    # not as a measured operating point.
    # In the --no-cap arm M(eps) is uncapped, so the like-for-like comparator
    # is uncapped `peak`. Both readings are emitted; the peak_capped one is
    # the common yardstick across arms, the like-for-like one is the fair
    # comparison within this arm.
    exp_rows = []
    for scope in ("all_vms", "non_idle"):
        for res, _c, _cat, _mr, _oc in RESOURCES:
            row = {"scope": scope, "resource": res}
            common = matched_risk_reclamation(fr, scope, res, "peak_capped")
            row.update({
                "peak_capped_viol_frac": common["baseline_viol_frac"],
                "peak_capped_recl_pct": common["baseline_recl_pct"],
                "evt_recl_pct_at_matched_risk": common["evt_recl_pct"],
                "gap_pp": common["gap_pp"],
                "note": common["note"],
            })
            like = matched_risk_reclamation(
                fr, scope, res, "peak" if args.no_cap else "peak_capped")
            row["like_for_like_baseline"] = like["baseline"]
            row["like_for_like_gap_pp"] = like["gap_pp"]
            exp_rows.append(row)
    exp = pd.DataFrame(exp_rows)
    exp.to_csv(os.path.join(args.out, "exploratory_matched_risk.csv"), index=False)

    with pd.option_context("display.width", 200, "display.max_rows", 300):
        print("\n" + "=" * 96)
        print("FRONTIER  -- reclamation beside OBSERVED violation fraction")
        print("=" * 96)
        print(fr.to_string(index=False))
        print("\n" + "=" * 96)
        print("DOMINANCE  -- a baseline dominates if it has <= risk AND >= reclamation")
        print("=" * 96)
        print(dom.to_string(index=False))
        print("\n" + "=" * 96)
        print("CALIBRATION  -- eps TARGET against MEASURED violation fraction")
        print("=" * 96)
        print(cal.to_string(index=False))
        print("\n" + "=" * 96)
        print("EXPLORATORY -- NOT PRE-REGISTERED")
        print("Reclamation at matched MEASURED risk against peak_capped, the "
              "baseline the")
        print("frontier actually overlaps. H1 names p95 and is unchanged; this "
              "is an observation,")
        print("not a hypothesis test, and no pre-registered threshold is "
              "adjusted in light of it.")
        print("=" * 96)
        print(exp.to_string(index=False))

    with open(os.path.join(args.out, "sweep_config.json"), "w") as fh:
        json.dump({
            "obs_days": args.obs_days, "eval_days": args.eval_days,
            "eps_sweep": list(args.eps), "theta": args.theta,
            "theta_method": args.theta_method,
            "run_length": args.run_length,
            "run_length_fixed_before_any_t11_result": True,
            "threshold_q": args.threshold_q,
            "min_exceedances": args.min_exceedances,
            "snapped_to_catalog": snap,
            "capped_at_original_allocation": not args.no_cap,
            "vms_used": n_vms, "vms_idle": n_idle,
            "vms_skipped_short": skipped_short,
            "flavour_vcpu": FLAVOUR_VCPU, "flavour_mem_gb": FLAVOUR_MEM_GB,
        }, fh, indent=2)

    print(f"\nwrote {args.out}/frontier.csv, dominance.csv, gpd_shapes.csv, "
          f"theta_per_vm.csv, eps_calibration.csv, "
          f"exploratory_matched_risk.csv, sweep_config.json")


if __name__ == "__main__":
    main()
