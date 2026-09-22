#!/usr/bin/env python3
"""
wave_report.py -- turn one wave's captured evidence into the T8 comparison.

Joins three sources for every pod in a wave:

  * the offline prediction   (results/subset_50_predictions.csv)
  * what the kernel did      (evidence/logs/*.jsonl, written by replay_agent)
  * how close it ran         (evidence/1N_waveN_cgroup_samples.tsv)

The cluster's CPU verdict is recomputed here from the agent's per-sample
`cpu_exhausted` flags using the SAME rule as src/baselines.py -- at least 2
consecutive exhausted samples -- so the cluster and offline columns are
comparable. The memory verdict is the kernel's own OOMKill, which needs no
recomputation: it either fired or it did not.

Two memory columns look interchangeable and are not. Read this before deriving
anything from either:

  * `excess_over_budget_mib` = peak scaled demand - scaled budget. Plain,
    no overhead constant. Positive means the offline rule predicts a memory
    violation. `excess_over_budget_mib > margin_mib` means the pod exceeds the
    limit the kernel actually enforced -- that is the like-with-like comparison,
    and it reproduces `obs_oomkilled` on all 50 VMs of the run.

  * `need_mib` = peak scaled demand + 59.11 - scaled budget, carried over from
    `wave_plan.json`. The 59.11 MiB is the SUPERSEDED standalone-probe overhead
    constant, measured under generous headroom and therefore too high (see
    docs/DATA_QUALITY.md, "Measurement quality on the cluster side"). It is kept
    only for provenance -- it is what the wave packing and the original
    `artifact_affected` flag were computed from. Comparing it against
    `margin_mib` directly does NOT reproduce the kernel's verdict; it reports
    the four margin-band VMs as mismatches.

Use `excess_over_budget_mib`. Column semantics are also written to
`results/wave_outcomes_COLUMNS.md`.

Usage:
    python3 wave_report.py --wave 1 --logs evidence/logs \
        --samples evidence/10_wave1_cgroup_samples.tsv --out results/wave1_outcomes.csv
"""

import argparse
import csv
import json
import os

MIB = 1024 * 1024


def cpu_violation(flags, consecutive=2):
    """src/baselines.py's CPU rule: >= `consecutive` exhausted samples in a row."""
    run = best = 0
    for f in flags:
        run = run + 1 if f else 0
        best = max(best, run)
    return best >= consecutive, best


def read_log(path):
    """Parse one pod's JSONL. Returns (start_record, sample_records)."""
    start, samples = None, []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("event") == "start":
                start = rec
            elif "sample" in rec:
                samples.append(rec)
    return start, samples


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wave", type=int, required=True)
    ap.add_argument("--plan", default="testbed/wave_plan.json")
    ap.add_argument("--predictions", default="results/subset_50_predictions.csv")
    ap.add_argument("--logs", default="evidence/logs")
    ap.add_argument("--samples", default="")
    ap.add_argument("--status", default="evidence/05_oomkills.tsv")
    ap.add_argument("--probe-overhead-mib", type=float, default=59.11,
                    help="overhead constant the margin band was derived from, "
                         "measured standalone under a generous --memory limit")
    ap.add_argument("--scale", type=float, default=0.0625)
    ap.add_argument("--subset", default="results/subset_50.csv")
    ap.add_argument("--traces", default="data/traces",
                    help="staged traces, for the margin-band flag (see below)")
    ap.add_argument("--observed-overhead-mib", type=float, default=49.5,
                    help="overhead actually observed in-cluster, under the limit the "
                         "workload really runs with. A VM whose predicted verdict "
                         "differs between the two constants is flagged "
                         "artifact_affected -- its prediction is an artifact of the "
                         "probe's headroom, not of the method under test.")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    plan = json.load(open(args.plan))
    wave = next(w for w in plan["waves"] if w["wave"] == args.wave)
    vm_ids = wave["vm_ids"]
    need = plan.get("need_mib", {})
    margin = plan["margin_mib"]

    pred = {}
    with open(args.predictions, newline="") as fh:
        for row in csv.DictReader(fh):
            if row["method"] == "p95":
                pred[row["vm_id"]] = row

    # Kernel verdict per pod, from the terminated state captured by
    # collect_evidence.sh (state.terminated, not lastState -- see that script).
    status = {}
    if os.path.exists(args.status):
        with open(args.status) as fh:
            for line in fh:
                f = line.rstrip("\n").split("\t")
                if len(f) >= 3 and f[0].startswith("rs-"):
                    status[f[0]] = f[2]

    # Peak cgroup memory per pod, and the limit it ran against.
    peak, limit = {}, {}
    if args.samples and os.path.exists(args.samples):
        with open(args.samples) as fh:
            rd = csv.DictReader(fh, delimiter="\t")
            for row in rd:
                pod = row["pod"]
                limit[pod] = int(row["limit_bytes"])
                for col in ("current_bytes", "peak_bytes"):
                    if row[col] not in ("-", ""):
                        peak[pod] = max(peak.get(pod, 0), int(row[col]))

    # `need` in the plan was computed with the probe constant. Recomputing it
    # with the in-cluster constant shifts every VM by the same amount, so a
    # verdict only changes for VMs lying between the two thresholds -- those
    # are the ones whose cluster outcome is decided by the measurement
    # artifact rather than by the recommendation under test.
    shift = args.probe_overhead_mib - args.observed_overhead_mib

    def artifact_affected(vm_id):
        n = need.get(vm_id)
        if n == "" or n is None:
            return False
        return (n > margin) != ((n - shift) > margin)

    # The offline definition compares demand against the *recommendation*; the
    # cluster enforces the recommendation PLUS the agent margin M. A VM whose
    # peak demand lands between the two exceeds its recommendation (so the
    # offline rule predicts a violation) but never exceeds what the kernel
    # actually enforces, so the kernel has no reason to kill it. Its outcome is
    # decided by M -- a property of the harness -- rather than by the
    # recommendation under test, and is therefore structurally undecidable in
    # this testbed. This flag is derived from the design, not fitted to
    # results, and it subsumes the overhead-constant flag above.
    budgets = {r["vm_id"]: float(r["rec_mem_gb"]) * args.scale * 1024
               for r in csv.DictReader(open(args.subset))} if os.path.isdir(
                   args.traces) else {}

    _peak_cache = {}

    def peak_demand_mib(vm_id):
        """Peak scaled memory demand from the staged trace, in MiB.

        Read straight off the trace the pod actually replayed -- no overhead
        constant of any kind enters here. This is the quantity the offline
        violation rule compares against the recommendation.
        """
        if vm_id in _peak_cache:
            return _peak_cache[vm_id]
        path = os.path.join(args.traces, f"{vm_id}.csv")
        pk = None
        if os.path.exists(path):
            with open(path, newline="") as fh:
                hdr = [c.strip().strip('"').strip()
                       for c in fh.readline().split(";")]
                col = hdr.index("Memory usage [KB]")
                pk = max(float(l.split(";")[col].strip())
                         for l in fh if l.strip()) / 1024.0
        _peak_cache[vm_id] = pk
        return pk

    def excess_over_budget(vm_id):
        """Peak scaled demand minus scaled budget. Positive => the offline rule
        predicts a memory violation. Compare against `margin_mib` to get the
        kernel's verdict: excess > margin means the pod exceeds the limit that
        was actually enforced, which is the like-with-like comparison."""
        b = budgets.get(vm_id)
        pk = peak_demand_mib(vm_id)
        if b is None or pk is None:
            return ""
        return round(pk - b, 2)

    def margin_band(vm_id):
        b = budgets.get(vm_id)
        pk = peak_demand_mib(vm_id)
        if b is None or pk is None:
            return ""
        return b < pk <= b + margin

    fields = ["vm_id", "pred_mem_violation", "obs_oomkilled", "artifact_affected",
              "margin_band",
              "pred_cpu_violation", "obs_cpu_violation", "max_consec_throttled",
              "samples_logged", "samples_total", "log_captured",
              "peak_demand_mib", "budget_mib", "excess_over_budget_mib",
              "need_mib", "need_mib_corrected", "margin_mib", "limit_mib",
              "peak_cgroup_mib", "peak_pct_of_limit", "headroom_mib"]
    rows = []
    for vm_id in sorted(vm_ids, key=int):
        pod = f"rs-p95-{vm_id}"
        log = os.path.join(args.logs, f"{pod}.jsonl")
        captured = os.path.exists(log) and os.path.getsize(log) > 0
        start, samples = read_log(log) if captured else (None, [])
        cpu_obs, consec = cpu_violation([s.get("cpu_exhausted") for s in samples])
        lim = limit.get(pod, 0)
        pk = peak.get(pod, 0)
        p = pred.get(vm_id, {})
        rows.append({
            "vm_id": vm_id,
            "pred_mem_violation": p.get("mem_violation", "?"),
            "obs_oomkilled": status.get(pod, "") == "OOMKilled",
            "artifact_affected": artifact_affected(vm_id),
            "margin_band": margin_band(vm_id),
            "need_mib_corrected": (round(need[vm_id] - shift, 2)
                                   if vm_id in need else ""),
            "pred_cpu_violation": p.get("cpu_violation", "?"),
            "obs_cpu_violation": cpu_obs,
            "max_consec_throttled": consec,
            "samples_logged": len(samples),
            "samples_total": (start or {}).get("samples", ""),
            "log_captured": captured,
            "peak_demand_mib": (round(peak_demand_mib(vm_id), 2)
                                if peak_demand_mib(vm_id) is not None else ""),
            "budget_mib": budgets.get(vm_id, ""),
            "excess_over_budget_mib": excess_over_budget(vm_id),
            "need_mib": need.get(vm_id, ""),
            "margin_mib": margin,
            "limit_mib": round(lim / MIB, 1) if lim else "",
            "peak_cgroup_mib": round(pk / MIB, 1) if pk else "",
            "peak_pct_of_limit": round(100 * pk / lim, 1) if lim and pk else "",
            "headroom_mib": round((lim - pk) / MIB, 1) if lim and pk else "",
        })

    with open(args.out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    hdr = (f"{'vm':>5} {'pred_mem':>8} {'oomkill':>7} {'pred_cpu':>8} "
           f"{'obs_cpu':>7} {'consec':>6} {'samples':>12} {'need':>7} "
           f"{'limit':>7} {'peak':>7} {'%lim':>6} {'log':>4} {'art':>4}")
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        print(f"{r['vm_id']:>5} {str(r['pred_mem_violation']):>8} "
              f"{str(r['obs_oomkilled']):>7} {str(r['pred_cpu_violation']):>8} "
              f"{str(r['obs_cpu_violation']):>7} {r['max_consec_throttled']:>6} "
              f"{str(r['samples_logged']) + '/' + str(r['samples_total']):>12} "
              f"{r['need_mib']:>7} {r['limit_mib']:>7} {r['peak_cgroup_mib']:>7} "
              f"{r['peak_pct_of_limit']:>6} {'yes' if r['log_captured'] else 'NO':>4} "
              f"{'*' if r['artifact_affected'] else '':>4}")

    def confusion(subset, pk, ok):
        def cell(pv, ov):
            return sum(1 for r in subset
                       if str(r[pk]).lower() == pv and str(r[ok]).lower() == ov)
        return cell("true", "true"), cell("true", "false"), \
            cell("false", "true"), cell("false", "false")

    affected = [r for r in rows if r["artifact_affected"]]
    clean = [r for r in rows if not r["artifact_affected"]]

    # Reported twice. The margin was derived from an overhead constant measured
    # standalone under generous headroom; `memory.peak` is limit-dependent, so
    # that constant is too high and the VMs between the two thresholds have a
    # cluster outcome decided by the probe's headroom rather than by the
    # recommendation. Excluding them measures the method; including them
    # measures the testbed as actually run. Both belong in the report.
    for label, subset in (("all pods", rows),
                          ("excluding artifact-affected", clean)):
        tp, fn, fp, tn = confusion(subset, "pred_mem_violation", "obs_oomkilled")
        print(f"\nmemory confusion, {label} (offline prediction vs kernel OOMKill):")
        print(f"  TP {tp:>2}  FN {fn:>2}  FP {fp:>2}  TN {tn:>2}   (n={len(subset)})")
        tp, fn, fp, tn = confusion(subset, "pred_cpu_violation", "obs_cpu_violation")
        print(f"cpu confusion, {label} (offline prediction vs 2-consecutive rule):")
        print(f"  TP {tp:>2}  FN {fn:>2}  FP {fp:>2}  TN {tn:>2}   (n={len(subset)})")

    if affected:
        print(f"\nartifact-affected ({len(affected)}): verdict flips between the "
              f"probe constant ({args.probe_overhead_mib}) and the in-cluster "
              f"constant ({args.observed_overhead_mib}) at M={margin}")
        for r in affected:
            print(f"  vm {r['vm_id']:>5}  need {r['need_mib']:>6} -> "
                  f"{r['need_mib_corrected']:>6} corrected  "
                  f"(predicted OOM, observed "
                  f"{'OOMKilled' if r['obs_oomkilled'] else 'survived'})")
    missing = [r["vm_id"] for r in rows if not r["log_captured"]]
    print(f"\nreplay.jsonl captured: {len(rows) - len(missing)}/{len(rows)}"
          + (f"  MISSING: {missing}" if missing else "  (all pods)"))
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
