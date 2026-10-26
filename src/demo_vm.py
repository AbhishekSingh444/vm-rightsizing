#!/usr/bin/env python3
"""
demo_vm.py -- walk one real VM at a time through every policy, on screen.

For `make demo-vm` only; the research pipeline does not call this. It reads
every CSV in the input folder (real Bitbrains traces, supplied on the day),
runs the millisecond guard over all of them first, then prints per VM:

    original allocation and observed mean / p95 / peak (observation window)
    what peak, p95 and M(eps) at each eps recommend
    for M(eps): threshold u, shape, scale, theta, and the predicted
        probability of at least one exhaustion over the evaluation horizon
    what actually happened in the held-out evaluation window
    reclaimed vCPU-hours and GB-hours per policy

Nothing is re-implemented. Loading, the 21/9 split, the tail fit, theta
(intervals estimator, the headline), the M(eps) allocation and violation
counting all go through the same functions as `make theta`, so a VM's numbers
here are the numbers it contributes to the published frontier. peak and p95
are the Review 1 baselines as defined, snapped and uncapped.

A file that cannot be used is reported with the reason and skipped; the
others still run. Exit status is 1 if any file was skipped.

Usage:
    python3 demo_vm.py --input data/demo_input --out demo_out/vm
"""

import argparse
import contextlib
import glob
import io
import json
import os
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from baselines import (  # noqa: E402
    COL_CAP_MHZ,
    COL_CORES,
    COL_MEM_CAP_KB,
    COL_TS,
    EVT_DEFAULT_RUN_LENGTH,
    EVT_MIN_EXCEEDANCES,
    EVT_THRESHOLD_Q,
    FLAVOUR_MEM_GB,
    FLAVOUR_VCPU,
    detect_capacity_shift,
    evt_required_capacity,
    horizon_violation_prob,
    load_vm,
    tail_exceedance_prob,
)
from demo_guard import check_file  # noqa: E402
from sweep_eps import (  # noqa: E402
    RESOURCES,
    attach_theta,
    baseline_allocs,
    build_fleet,
    evaluate,
    evt_allocs,
)

DEMO_EPS = [0.001, 0.01, 0.1]
THETA_METHOD = "intervals"
ACK = "This trace was graciously provided by Bitbrains IT Services Inc."
RES_LABEL = {"cpu": "CPU", "mem": "memory"}


def read_raw(path):
    """The file as written, read independently of load_vm so a rejection can
    quote what the file actually contains. None if it cannot be parsed."""
    try:
        df = pd.read_csv(path, sep=r";\s*", engine="python")
    except Exception:
        return None
    df.columns = [c.strip().strip('"').strip() for c in df.columns]
    return df


def dup_timestamps(raw):
    if raw is None or COL_TS not in raw.columns:
        return 0
    return int(pd.to_numeric(raw[COL_TS], errors="coerce").dropna().duplicated().sum())


def explain(reason, raw):
    """load_vm's one-line skip reason, plus the values that triggered it."""
    if raw is None:
        return reason
    num = {c: pd.to_numeric(raw[c], errors="coerce") for c in raw.columns}

    def first(col):
        s = num[col].dropna() if col in num else pd.Series(dtype=float)
        return float(s.iloc[0]) if len(s) else float("nan")

    if reason.startswith("missing"):
        if len(raw.columns) == 1:
            return (f"{reason} -- only one column was found, so the file is not "
                    f"';'-separated like the Bitbrains trace (header starts "
                    f"{raw.columns[0][:60]!r})")
        return f"{reason} -- columns found: {list(raw.columns)}"
    if reason == "bad CPU capacity":
        return (f"bad CPU capacity -- '{COL_CORES}' = {first(COL_CORES):g}, "
                f"'{COL_CAP_MHZ}' = {first(COL_CAP_MHZ):g}, so there is no per-core "
                f"clock rate to convert MHz demand into cores")
    if reason == "below flavour catalog floor":
        return (f"below flavour catalog floor -- provisioned {first(COL_CORES):g} vCPU, "
                f"{first(COL_MEM_CAP_KB) / 1048576.0:.2f} GB; the smallest flavour is "
                f"{min(FLAVOUR_VCPU)} vCPU / {min(FLAVOUR_MEM_GB)} GB, so there is "
                f"nothing smaller to right-size to")
    if reason == "mid-trace capacity shift":
        parts = []
        for col, label, scale, unit in ((COL_CAP_MHZ, "CPU capacity", 1.0, "MHz"),
                                        (COL_MEM_CAP_KB, "memory capacity", 1048576.0, "GB")):
            s = detect_capacity_shift(num[col].dropna()) if col in num else None
            if s:
                parts.append(f"{label} sits at {s['low_level'] / scale:,.2f} {unit} for "
                             f"{100 * s['low_share']:.0f}% of rows and "
                             f"{s['high_level'] / scale:,.2f} {unit} for "
                             f"{100 * s['high_share']:.0f}% ({s['ratio']:.1f}x)")
        return (f"mid-trace capacity shift -- {'; '.join(parts)}; the observation "
                f"window describes a machine that no longer exists by the evaluation window")
    return reason


def load(path, obs_days, eval_days):
    """Returns (fleet_entry, None) or (None, reason). Never raises."""
    if os.path.getsize(path) == 0:
        return None, "file is empty (0 bytes)"

    # load_vm reports its own skip reason on stderr; capture it rather than
    # restating its rules here, then add the values behind it.
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        loaded = load_vm(path)
    if loaded is None:
        msg = err.getvalue().strip()
        if msg.startswith("skip ") and ":" in msg:
            msg = msg.split(":", 1)[1].strip()
        if msg:
            return None, explain(msg, read_raw(path))
        raw = read_raw(path)
        return None, (f"no row has a numeric value in every required column "
                      f"({0 if raw is None else len(raw)} data rows read)")

    fleet, skipped = build_fleet([loaded], obs_days, eval_days)
    if skipped or not fleet:
        df = loaded[0]
        span = (df["ts"].max() - df["ts"].min()) / 86400.0 if len(df) >= 2 else 0.0
        return None, (f"trace too short: spans {span:.2f} days over {len(df)} rows; "
                      f"the split needs {obs_days:g} observation + {eval_days:g} "
                      f"evaluation = {obs_days + eval_days:g} days")
    return fleet, None


def fmt(x, nd=3):
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return "-"
    return f"{x:.{nd}f}"


def report_vm(fleet, eps_list, eval_days, path):
    """Print one VM's walkthrough; return its per-policy rows for the CSV."""
    theta_df = attach_theta(fleet, THETA_METHOD, EVT_DEFAULT_RUN_LENGTH)
    obs, ev, meta = fleet[0]
    eval_hours = eval_days * 24.0
    n_dup = dup_timestamps(read_raw(path))

    print(f"\n{'=' * 96}\nVM {meta['vm_id']}   ({os.path.basename(path)})"
          f"{'   [idle: p99 CPU < 5% of allocation]' if meta['idle'] else ''}\n{'=' * 96}")
    print(f"original allocation : {meta['orig_cores']:g} vCPU, {meta['orig_mem_gb']:.2f} GB")
    gap = obs["ts"].diff().median()
    print(f"observation window  : {len(obs)} samples, median gap {gap:.0f} s   "
          f"| evaluation window: {len(ev)} samples, {eval_days:g} days held out")
    if n_dup:
        in_obs = int(obs["ts"].duplicated().sum())
        effect = ("they shorten the observation median gap and so lengthen the "
                  "horizon n_eval below" if in_obs else
                  "none fall in the observation window, so n_eval is unaffected")
        print(f"note                : {n_dup} duplicate timestamps in this file "
              f"(zero-demand padding, ENVIRONMENT_NOTES.md), {in_obs} in the "
              f"observation window.\n                      Kept, as the pipeline "
              f"keeps them; {effect}")

    print("\nobserved over the observation window")
    print(f"  {'resource':<8} {'mean':>9} {'p95':>9} {'peak':>9}   allocation")
    for res, col, _cat, _mr, orig_col in RESOURCES:
        v = obs[col]
        unit = "cores" if res == "cpu" else "GB"
        print(f"  {RES_LABEL[res]:<8} {v.mean():>9.3f} {v.quantile(0.95):>9.3f} "
              f"{v.max():>9.3f}   {meta[orig_col]:.2f} {unit}")

    print(f"\nM(eps) tail fit -- observation window only; u = q{EVT_THRESHOLD_Q:g}, "
          f"theta by the {THETA_METHOD} estimator, horizon n_eval = {meta['n_eval']} samples")
    print(f"  {'resource':<8} {'threshold u':>12} {'shape xi':>9} {'scale':>9} "
          f"{'exceed':>7} {'theta':>7} {'trials':>9}")
    for res, *_ in RESOURCES:
        fit, why, _peak = meta["fits"][res]
        if fit is None:
            print(f"  {RES_LABEL[res]:<8} no tail fit ({why}; fewer than "
                  f"{EVT_MIN_EXCEEDANCES} exceedances needed) -- M(eps) falls back to peak")
            continue
        th = theta_df[theta_df.resource == res].iloc[0]
        th_note = "" if th.status == "ok" else f" (estimator: {th.status}, using 1)"
        print(f"  {RES_LABEL[res]:<8} {fit['u']:>12.4f} {fit['shape']:>9.4f} "
              f"{fit['scale']:>9.4f} {fit['n_exceed']:>7d} {th.theta_used:>7.4f} "
              f"{th.theta_used * meta['n_eval']:>9.1f}{th_note}")

    policies = [("peak", None, baseline_allocs("peak")),
                ("p95", None, baseline_allocs("p95"))]
    policies += [(f"M({eps:g})", eps, evt_allocs(eps)) for eps in eps_list]

    rows = []
    for name, eps, fn in policies:
        got = fn(meta, obs)
        out = evaluate(fleet, fn, eval_hours).iloc[0]
        row = {"vm_id": meta["vm_id"], "policy": name, "eps": eps,
               "idle": bool(meta["idle"]), "n_eval": meta["n_eval"]}
        for res, _col, _cat, _mr, orig_col in RESOURCES:
            alloc, fell_back, capped = got[res]
            fit, _why, _peak = meta["fits"][res]
            pred = need = np.nan
            if eps is not None and fit is not None and not fell_back:
                need = evt_required_capacity(fit, eps, meta["n_eval"], theta=meta["theta"][res])
                pred = horizon_violation_prob(tail_exceedance_prob(alloc, fit),
                                              meta["n_eval"], meta["theta"][res])
            if eps is None:
                # Baselines are uncapped (Review 1 definition), so snapping
                # can put them above the original allocation; say so.
                capped = alloc > meta[orig_col]
            row.update({f"{res}_alloc": alloc, f"{res}_fallback": bool(fell_back),
                        f"{res}_capped": bool(capped), f"{res}_required": need,
                        f"{res}_pred_p": pred,
                        f"{res}_events": int(out[f"{res}_events"]),
                        f"{res}_exhausted_minutes": int(out[f"{res}_exhausted_minutes"]),
                        f"{res}_recl_hours": float(out[f"{res}_recl_hours"])})
        rows.append(row)

    print("\nrecommendations -- 'needs' is the continuous capacity M(eps) asks for "
          "before snapping;\n  'pred P' is the fitted probability of at least one "
          "exhaustion over the horizon at the allocation given")
    print(f"  {'policy':<9} {'vCPU':>5} {'GB':>6}  {'needs CPU':>9} {'needs GB':>9}  "
          f"{'pred P CPU':>10} {'pred P mem':>10}  note")
    any_capped = False
    for r in rows:
        notes, pred = [], {}
        for res, *_ in RESOURCES:
            pred[res] = fmt(r[f"{res}_pred_p"], 5)
            if r[f"{res}_fallback"] and r["eps"] is not None:
                notes.append(f"{RES_LABEL[res]} fell back to peak")
            if r[f"{res}_capped"]:
                if r["eps"] is None:
                    notes.append(f"{RES_LABEL[res]} snapped ABOVE original")
                else:
                    notes.append(f"{RES_LABEL[res]} capped at original")
                    pred[res] += "*"
                    any_capped = True
        print(f"  {r['policy']:<9} {r['cpu_alloc']:>5g} {r['mem_alloc']:>6.2f}  "
              f"{fmt(r['cpu_required'], 2):>9} {fmt(r['mem_required'], 2):>9}  "
              f"{pred['cpu']:>10} {pred['mem']:>10}  {'; '.join(notes)}")
    if any_capped:
        print("  * at the original allocation, where load_vm clips demand: exhaustion "
              "there cannot be observed,\n    so this is the fitted tail's mass beyond "
              "the data's support, not a forecast")

    print(f"\nheld-out evaluation window ({eval_days:g} days) -- CPU: >= 2 consecutive "
          f"samples over allocation; memory: any single sample")
    print(f"  {'policy':<9} {'outcome':<24} {'CPU ev':>6} {'mem ev':>6} "
          f"{'recl vCPU-h':>12} {'recl GB-h':>10}")
    for r in rows:
        hit = [RES_LABEL[res] for res, *_ in RESOURCES if r[f"{res}_events"] > 0]
        outcome = f"VIOLATED ({' + '.join(hit)})" if hit else "no violation"
        print(f"  {r['policy']:<9} {outcome:<24} {r['cpu_events']:>6d} {r['mem_events']:>6d} "
              f"{r['cpu_recl_hours']:>12.1f} {r['mem_recl_hours']:>10.1f}")
    return rows


class Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, s):
        for st in self.streams:
            st.write(s)

    def flush(self):
        for st in self.streams:
            st.flush()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--input", default="data/demo_input",
                    help="folder of real per-VM trace CSVs")
    ap.add_argument("--out", default="demo_out/vm",
                    help="report, per-policy CSV and run_config.json go here")
    ap.add_argument("--obs-days", type=float, default=21.0)
    ap.add_argument("--eval-days", type=float, default=9.0)
    ap.add_argument("--eps", type=float, nargs="+", default=DEMO_EPS,
                    help="M(eps) targets to show")
    args = ap.parse_args()

    if not os.path.isdir(args.input):
        raise SystemExit(f"demo-vm: input folder {args.input} does not exist -- "
                         f"put one or more real trace CSVs in it")
    paths = sorted(glob.glob(os.path.join(args.input, "*.csv")))
    if not paths:
        others = sorted(os.listdir(args.input))
        raise SystemExit(f"demo-vm: no .csv files in {args.input}"
                         + (f" (found: {', '.join(others[:10])})" if others else " (folder is empty)"))

    os.makedirs(args.out, exist_ok=True)
    report_path = os.path.join(args.out, "demo_vm_report.txt")
    with open(report_path, "w", encoding="utf-8") as rep:
        tee = Tee(sys.stdout, rep)
        with contextlib.redirect_stdout(tee):
            rows, skipped, timings = run(args, paths)

    if rows:
        pd.DataFrame(rows).to_csv(os.path.join(args.out, "demo_vm_per_policy.csv"), index=False)
    with open(os.path.join(args.out, "run_config.json"), "w") as fh:
        json.dump({"input": args.input, "files": [os.path.basename(p) for p in paths],
                   "skipped": skipped, "obs_days": args.obs_days,
                   "eval_days": args.eval_days, "eps": args.eps,
                   "theta_method": THETA_METHOD, "threshold_q": EVT_THRESHOLD_Q,
                   "min_exceedances": EVT_MIN_EXCEEDANCES,
                   "baselines": "peak and p95, snapped, uncapped (Review 1 definition)",
                   "m_eps": "snapped, capped at original allocation (headline)",
                   "seconds_per_vm": timings}, fh, indent=2)
    print(f"\nwrote {report_path}, demo_vm_per_policy.csv, run_config.json")
    return 1 if skipped else 0


def run(args, paths):
    print(f"demo-vm: {len(paths)} file(s) in {args.input}")
    print("guard: timestamp-unit check on every file first")
    guard_fail = {}
    for p in paths:
        try:
            r = check_file(p)
        except Exception as exc:  # the guard must never be the traceback
            r = f"could not be checked ({type(exc).__name__}: {exc})"
        if r:
            guard_fail[p] = r
            print(f"  REJECT {os.path.basename(p)}: {r}")
    print(f"  {len(paths) - len(guard_fail)} of {len(paths)} passed")

    rows, skipped, timings = [], {}, {}
    for p in paths:
        name = os.path.basename(p)
        if p in guard_fail:
            skipped[name] = guard_fail[p]
            continue
        t0 = time.perf_counter()
        try:
            fleet, reason = load(p, args.obs_days, args.eval_days)
            if reason:
                skipped[name] = reason
                print(f"\n{'=' * 96}\nSKIP {name}: {reason}")
                continue
            rows += report_vm(fleet, args.eps, args.eval_days, p)
        except Exception as exc:
            skipped[name] = f"could not be processed ({type(exc).__name__}: {exc})"
            print(f"\n{'=' * 96}\nSKIP {name}: {skipped[name]}")
            continue
        dt = time.perf_counter() - t0
        timings[name] = round(dt, 2)
        print(f"\n  ({dt:.2f} s for this VM)")

    print(f"\n{'=' * 96}")
    print(f"{len(timings)} VM(s) shown, {len(skipped)} file(s) skipped")
    for name, why in skipped.items():
        print(f"  skipped {name}: {why}")
    print(f"\n{ACK}")
    return rows, skipped, timings


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
