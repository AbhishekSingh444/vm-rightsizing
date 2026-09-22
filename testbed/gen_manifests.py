#!/usr/bin/env python3
"""
gen_manifests.py — turn right-sizing recommendations into Kubernetes pod specs.

Input: a recommendations CSV with columns
    vm_id,method,rec_cores,rec_mem_gb
where `method` is one of the baselines (peak, p95) or the proposed method at a
given aggressiveness (evt_eps0.01, ...).

Output: one Pod manifest per (vm_id, method), with resources.limits set to the
recommendation. Limits, not just requests -- limits are what the kernel
actually enforces, and enforcement is the entire point of the testbed.

Usage:
    python3 gen_manifests.py recommendations.csv --method p95 --out manifests/
"""

import argparse
import csv
import math
import os
import re

# Fixed memory margin (MiB) added on top of every pod's scaled demand budget.
# This is a *fixed* cost that does not shrink with --scale, so it is added to
# the scaled limit rather than folded into the multiplicative scaling -- see
# --scale's docstring below for why.
#
# 80 MiB, chosen from the measured safe band [57.06, 82.66) MiB -- see
# ENVIRONMENT_NOTES.md, "Additivity of the two overheads" and the M=80 row.
# The band's floor is what VM 324 needs (peak demand + 59.11 MiB of measured
# retention overhead) and its ceiling is where predicted violators 332/1229
# start falsely surviving. Below the floor 324 falsely OOMs; above the ceiling
# true violators are lost. No subset VM sits between those two, so any value
# in the band gives the same confusion table.
#
# This supersedes the per-worker-pool-width WORKER_OVERHEAD_MIB table (19.708
# MiB at pool width 1, ..., 29.871 at 8). That table measured only the
# interpreter baseline against a near-zero-demand trace that never
# reallocates, and it was wrong twice over: it missed the allocator retention
# transient entirely, and the transient and the baseline turned out to overlap
# rather than stack. The 59.11 MiB figure measures both together, under load,
# in one measurement.
MARGIN_MIB = 80.0

# The margin was measured at CpuLoad pool width 1, which is what every VM in
# the subset gets at scale 1/16. A wider pool forks more interpreters and the
# margin would have to be re-measured, so generating manifests outside that
# regime is refused rather than extrapolated.
MEASURED_POOL_WIDTH = 1

POD_TEMPLATE = """---
apiVersion: v1
kind: Pod
metadata:
  name: {pod_name}
  labels:
    app: rightsizing-replay
    method: {method_label}
    vm: "{vm_label}"
spec:
  restartPolicy: Never
  containers:
    - name: replay
      image: {image}
      imagePullPolicy: IfNotPresent
      env:
        - name: TRACE_FILE
          value: /trace/{vm_id}.csv
        - name: VM_ID
          value: "{vm_id}"
        - name: SLICE_SECONDS
          value: "{slice_seconds}"
        - name: LIMIT_CORES
          value: "{rec_cores}"
        - name: LOG_FILE
          value: /var/log/replay.jsonl
      resources:
        requests:
          cpu: "{rec_cores}"
          memory: "{rec_mem_bytes}"
        limits:
          cpu: "{rec_cores}"
          memory: "{rec_mem_bytes}"
      volumeMounts:
        - name: traces
          mountPath: /trace
          readOnly: true
  volumes:
    - name: traces
      hostPath:
        path: /traces
        type: Directory
"""


def sanitize(s):
    """Kubernetes names: lowercase alphanumeric and '-' only."""
    s = re.sub(r"[^a-z0-9-]", "-", str(s).lower())
    return re.sub(r"-+", "-", s).strip("-")[:40]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("recommendations")
    ap.add_argument("--method", required=True, help="method column value to emit")
    ap.add_argument("--out", default="manifests")
    ap.add_argument("--image", default="rightsizing/replay:latest")
    ap.add_argument("--slice-seconds", default="1.0")
    ap.add_argument("--limit", type=int, default=0, help="cap number of pods (0=all)")
    ap.add_argument("--scale", type=float, default=1.0,
                     help="factor applied to rec_cores/rec_mem_gb before setting pod "
                          "limits. Pass the SAME factor to stage_traces.py --scale, "
                          "which scales the replayed demand identically. CPU limit is "
                          "pure rec_cores*scale -- CFS throttling has no comparable "
                          "fixed floor. Memory limit is --margin-mib + "
                          "rec_mem_gb*scale*1024: the replay agent's own memory "
                          "overhead is a FIXED cost that does not shrink with scale, "
                          "so at small scale it must be added on top of the scaled "
                          "limit, not multiplied into it -- multiplying it in let two "
                          "predicted-safe VMs OOM on interpreter overhead alone at "
                          "scale=1/16 with no margin term (see ENVIRONMENT_NOTES.md). "
                          "With the margin at or above the agent's true overhead, the "
                          "OOM condition reduces to demand > limit, independent of "
                          "scale -- this is what makes the cluster run's violations "
                          "comparable to the offline predictions.")
    ap.add_argument("--margin-mib", type=float, default=MARGIN_MIB,
                     help=f"fixed memory margin in MiB added to every pod's scaled "
                          f"budget (default {MARGIN_MIB}, from the measured safe band "
                          f"-- see MARGIN_MIB in this file). Must match the value "
                          f"passed to plan_waves.py.")
    ap.add_argument("--vm-ids", default="",
                     help="optional file, one vm_id per line, restricting which VMs "
                          "get a manifest (used to split a subset into waves)")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    # Drop manifests left by a previous run into this directory. `kubectl apply
    # -f <dir>` applies everything it finds, so a stale pod from an earlier,
    # differently-composed wave would be started and then counted in that
    # wave's confusion table.
    for stale in os.listdir(args.out):
        if stale.endswith(".yaml"):
            os.remove(os.path.join(args.out, stale))

    method_label = sanitize(args.method)

    wanted_ids = None
    if args.vm_ids:
        with open(args.vm_ids) as fh:
            wanted_ids = {line.strip() for line in fh if line.strip()}

    written = 0
    with open(args.recommendations, newline="") as fh:
        for row in csv.DictReader(fh):
            if row["method"] != args.method:
                continue
            if wanted_ids is not None and row["vm_id"] not in wanted_ids:
                continue
            if args.limit and written >= args.limit:
                break

            vm_id = row["vm_id"]
            vm_label = sanitize(vm_id)
            rec_cores = float(row["rec_cores"]) * args.scale

            # Pool width must match replay_agent.py's CpuLoad.__init__ exactly:
            # self.n = max(1, math.ceil(max_cores)), where max_cores is this
            # same scaled rec_cores value (injected as LIMIT_CORES below).
            pool_width = max(1, math.ceil(rec_cores))
            if pool_width != MEASURED_POOL_WIDTH:
                raise SystemExit(
                    f"vm {vm_id}: pool_width={pool_width}, but the margin was measured "
                    f"at pool width {MEASURED_POOL_WIDTH}. Each extra forked worker "
                    f"adds interpreter overhead the margin does not account for -- "
                    f"re-measure it (see ENVIRONMENT_NOTES.md) before generating "
                    f"manifests at this scale/flavour combination, do not extrapolate."
                )
            rec_mem_mi = args.margin_mib + float(row["rec_mem_gb"]) * args.scale * 1024
            # Kubernetes memory quantities must reduce to a whole number of
            # bytes -- "147.71Mi" does not (147.71*1024*1024 is fractional),
            # which the API only accepts by re-expressing it in milli-bytes
            # ("154885160960m", with an admission warning). Round to whole
            # bytes explicitly and emit a bare byte count instead.
            rec_mem_bytes = round(rec_mem_mi * 1024 * 1024)

            pod_name = f"rs-{method_label}-{vm_label}"[:63].strip("-")

            manifest = POD_TEMPLATE.format(
                pod_name=pod_name,
                method_label=method_label,
                vm_label=vm_label,
                vm_id=vm_id,
                image=args.image,
                slice_seconds=args.slice_seconds,
                rec_cores=rec_cores,
                rec_mem_bytes=rec_mem_bytes,
            )
            path = os.path.join(args.out, f"{pod_name}.yaml")
            with open(path, "w") as out:
                out.write(manifest)
            written += 1

    print(f"wrote {written} manifests for method '{args.method}' to {args.out}/ "
          f"(scale={args.scale})")


if __name__ == "__main__":
    main()
