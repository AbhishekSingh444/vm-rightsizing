#!/usr/bin/env python3
"""
rescore_cpu.py -- rescore a wave's CPU outcomes from kernel throttle counters.

The default cluster CPU verdict comes from `cpu_exhausted`, which replay_agent
raises on a deadline overrun -- the agent noticing it did not finish a slice in
time. That is a property of the harness keeping pace, not of the kernel, and at
S=1/16 it produced 5 false positives in wave 1.

`throttled_periods` / `throttled_us` come from the container's own cgroup
(`cpu.stat`), so they do not depend on the agent keeping pace, the way an
OOMKill does not. This script asks whether they give a kernel-enforced CPU
signal that separates predicted violators from non-violators better.

Scorings compared (all use src/baselines.py's >= 2-consecutive-sample rule):
  deadline   cpu_exhausted, as recorded          -- the current signal
  any        throttled_us > 0
  saturated  throttled in EVERY period of the slice (periods >= slice/100ms)
  heavy      throttled_us > slice_seconds * 1e6  (throttle stall exceeds the
             wall-clock slice itself, aggregated over the pool)

Offline rescoring only. Reads captured evidence; changes nothing.

Usage:
    python3 rescore_cpu.py --wave 1 --logs evidence/wave1/logs
"""

import argparse
import csv
import json
import os

PERIOD_US = 100_000  # CFS default period


def consecutive(flags, k=2):
    run = best = 0
    for f in flags:
        run = run + 1 if f else 0
        best = max(best, run)
    return best >= k, best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wave", type=int, default=1)
    ap.add_argument("--plan", default="testbed/wave_plan.json")
    ap.add_argument("--subset", default="results/subset_50.csv")
    ap.add_argument("--predictions", default="results/subset_50_predictions.csv")
    ap.add_argument("--logs", required=True)
    ap.add_argument("--scale", type=float, default=0.0625)
    ap.add_argument("--slice-seconds", type=float, default=1.0)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    plan = json.load(open(args.plan))
    vm_ids = next(w for w in plan["waves"] if w["wave"] == args.wave)["vm_ids"]

    cores = {r["vm_id"]: float(r["rec_cores"]) * args.scale
             for r in csv.DictReader(open(args.subset))}
    pred = {r["vm_id"]: r for r in csv.DictReader(open(args.predictions))
            if r["method"] == "p95"}

    periods_per_slice = max(1, int(args.slice_seconds * 1e6 / PERIOD_US))
    rows = []
    for vm_id in sorted(vm_ids, key=int):
        path = os.path.join(args.logs, f"rs-p95-{vm_id}.jsonl")
        if not os.path.exists(path):
            continue
        recs, total = [], None
        for line in open(path, encoding="utf-8", errors="replace"):
            if not line.startswith("{"):
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("event") == "start":
                total = r.get("samples")
            elif "sample" in r:
                recs.append(r)
        if not recs:
            continue

        lim = cores.get(vm_id, 0)
        scorings = {
            "deadline": [bool(r.get("cpu_exhausted")) for r in recs],
            "any": [r.get("throttled_us", 0) > 0 for r in recs],
            "saturated": [r.get("throttled_periods", 0) >= periods_per_slice
                          for r in recs],
            "heavy": [r.get("throttled_us", 0) > args.slice_seconds * 1e6
                      for r in recs],
        }
        row = {
            "vm_id": vm_id,
            "pred_cpu": pred.get(vm_id, {}).get("cpu_violation", "?"),
            "samples": len(recs),
            "samples_total": total,
            # A pod OOMKilled early has censored CPU evidence: absence of a
            # violation in 30 of 5242 samples is not evidence of absence.
            "censored": bool(total) and len(recs) < 0.95 * total,
            "limit_cores": round(lim, 4),
            "max_demand_cores": round(max(r.get("demand_cores", 0) for r in recs), 4),
            "pct_demand_over_limit": round(
                100 * sum(1 for r in recs if r.get("demand_cores", 0) > lim)
                / len(recs), 1),
        }
        for name, flags in scorings.items():
            viol, run = consecutive(flags)
            row[f"obs_{name}"] = viol
            row[f"pct_{name}"] = round(100 * sum(flags) / len(flags), 1)
            row[f"maxrun_{name}"] = run
        rows.append(row)

    print(f"{'vm':>5} {'pred':>5} {'cens':>4} {'samples':>11} {'lim':>7} "
          f"{'maxdem':>7} {'%dem>lim':>8} | "
          + " ".join(f"{n:>10}" for n in ("deadline", "any", "saturated", "heavy")))
    print("-" * 118)
    for r in rows:
        cells = " ".join(
            f"{('Y' if r['obs_' + n] else 'n') + ' ' + format(r['pct_' + n], '.0f') + '%':>10}"
            for n in ("deadline", "any", "saturated", "heavy"))
        print(f"{r['vm_id']:>5} {str(r['pred_cpu'])[:5]:>5} "
              f"{'YES' if r['censored'] else '':>4} "
              f"{str(r['samples']) + '/' + str(r['samples_total']):>11} "
              f"{r['limit_cores']:>7} {r['max_demand_cores']:>7} "
              f"{r['pct_demand_over_limit']:>8} | {cells}")

    def confusion(subset, key):
        def cell(pv, ov):
            return sum(1 for r in subset
                       if str(r["pred_cpu"]).lower() == pv
                       and str(r[key]).lower() == ov)
        return cell("true", "true"), cell("true", "false"), \
            cell("false", "true"), cell("false", "false")

    uncensored = [r for r in rows if not r["censored"]]
    for label, subset in (("all pods", rows),
                          ("excluding censored (died early)", uncensored)):
        print(f"\ncpu confusion, {label} (n={len(subset)}):")
        print(f"  {'scoring':>10}  {'TP':>3} {'FN':>3} {'FP':>3} {'TN':>3}")
        for name in ("deadline", "any", "saturated", "heavy"):
            tp, fn, fp, tn = confusion(subset, f"obs_{name}")
            print(f"  {name:>10}  {tp:>3} {fn:>3} {fp:>3} {tn:>3}")

    if args.out:
        with open(args.out, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
