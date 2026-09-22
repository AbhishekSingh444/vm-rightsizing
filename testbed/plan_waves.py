#!/usr/bin/env python3
"""
plan_waves.py -- split the cluster subset into memory-balanced waves.

The subset does not fit on the node all at once even at --scale 1/16, so the
run is executed as sequential waves. This script decides the split, so that
the split is reproducible from the margin and the node's free capacity rather
than being a hand-maintained list of VM ids.

Footprint per pod matches gen_manifests.py exactly:

    memory (MiB) = margin_mib + rec_mem_gb * scale * 1024
    cpu (cores)  = rec_cores  * scale

The margin is a FIXED cost (it does not shrink with --scale), so it dominates
the wave arithmetic at small scale: at scale 1/16 a 2 GB recommendation is
128 MiB of demand budget carrying an 80 MiB margin on top.

Packing is LPT (longest-processing-time-first greedy onto the least-loaded
wave), which is deterministic given the input order, with ties broken by
vm_id so a re-run reproduces the same split byte-for-byte. The wave count is
the smallest K whose largest wave fits the node's free capacity with
--headroom to spare, on BOTH memory and CPU.

Wave 1 is not just "the first 1/K of the subset". Given --traces, the VMs
whose outcome is decided by the margin are pinned to it. A VM OOMs iff

    need = peak_scaled_demand + retention_mib - scaled_budget  >  margin

so |need - margin| is exactly how wrong the margin would have to be to flip
that VM from OOMKilled to survived or back. Every VM within --boundary-mib of
the flip goes into wave 1, so a mis-set margin shows up in the first wave
rather than four waves later. VMs far from the boundary are insensitive to
the margin and can run in any wave.

Usage:
    python3 plan_waves.py results/subset_50.csv --out testbed \
        --margin-mib 80 --scale 0.0625 --traces data/traces
"""

import argparse
import csv
import json
import os
import subprocess


def node_free(node):
    """Free allocatable on `node`, as (mem_mib, cpu_cores).

    Free = allocatable - requests already booked by non-terminated pods, which
    is what the scheduler actually has left to give us. Falls back to raising
    if kubectl is unavailable -- the caller can pass the numbers explicitly.
    """
    alloc = json.loads(subprocess.run(
        ["kubectl", "get", "node", node, "-o", "jsonpath={.status.allocatable}"],
        check=True, capture_output=True, text=True).stdout)
    mem_mib = int(alloc["memory"].rstrip("Ki")) / 1024.0
    cpu = float(alloc["cpu"])

    # Sum requests over every non-terminated pod on the node.
    pods = json.loads(subprocess.run(
        ["kubectl", "get", "pods", "--all-namespaces", "-o", "json",
         "--field-selector", f"spec.nodeName={node},status.phase!=Succeeded,"
                             f"status.phase!=Failed"],
        check=True, capture_output=True, text=True).stdout)
    used_mem = used_cpu = 0.0
    for pod in pods["items"]:
        for c in pod["spec"]["containers"]:
            req = c.get("resources", {}).get("requests", {}) or {}
            used_mem += parse_mem_mib(req.get("memory", "0"))
            used_cpu += parse_cpu(req.get("cpu", "0"))
    return mem_mib - used_mem, cpu - used_cpu


def parse_mem_mib(q):
    q = str(q)
    for suffix, mult in (("Ki", 1 / 1024), ("Mi", 1.0), ("Gi", 1024.0)):
        if q.endswith(suffix):
            return float(q[: -len(suffix)]) * mult
    return float(q) / (1024 * 1024)


def parse_cpu(q):
    q = str(q)
    if q.endswith("m"):
        return float(q[:-1]) / 1000.0
    return float(q)


def peak_demand_mib(path):
    """Peak scaled memory demand (MiB) in a staged trace.

    Staged traces carry the dataset's delimiter quirk (';' followed by a TAB),
    so the columns are split on the same regex-free equivalent used elsewhere:
    split on ';', then strip whitespace and quotes off every field.
    """
    with open(path, newline="") as fh:
        header = [c.strip().strip('"').strip() for c in fh.readline().split(";")]
        col = header.index("Memory usage [KB]")
        peak = 0.0
        for line in fh:
            if not line.strip():
                continue
            peak = max(peak, float(line.split(";")[col].strip()))
    return peak / 1024.0


def pack(items, k, pinned=()):
    """LPT greedy: heaviest first onto the least-loaded wave. Deterministic.

    `pinned` vm_ids are placed in wave 1 before anything else is packed; the
    remainder is then balanced across all K waves, with wave 1 already
    carrying its pinned load so the balancer compensates for it.
    """
    waves = [[] for _ in range(k)]
    load = [0.0] * k
    pinned = set(pinned)
    for vm_id, mem, cpu in sorted(items, key=lambda r: (-r[1], r[0])):
        if vm_id in pinned:
            waves[0].append((vm_id, mem, cpu))
            load[0] += mem
    for vm_id, mem, cpu in sorted(items, key=lambda r: (-r[1], r[0])):
        if vm_id in pinned:
            continue
        i = min(range(k), key=lambda j: (load[j], j))
        waves[i].append((vm_id, mem, cpu))
        load[i] += mem
    return waves


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("subset", help="subset CSV (vm_id,method,rec_cores,rec_mem_gb)")
    ap.add_argument("--out", default="testbed", help="directory for waveN_vms.txt")
    ap.add_argument("--margin-mib", type=float, default=80.0,
                    help="fixed memory margin added to every pod's scaled budget; "
                         "must match gen_manifests.py --margin-mib")
    ap.add_argument("--scale", type=float, default=0.0625)
    ap.add_argument("--headroom", type=float, default=0.30,
                    help="fraction of free capacity to leave unbooked (default 0.30). "
                         "Requests equal limits, so the scheduler alone would accept "
                         "far tighter packing; the headroom is against node-level "
                         "MemoryPressure, which evicts pods that are under their own "
                         "limit and produces exactly the Evicted-vs-OOMKilled "
                         "confusion the evidence collector has to screen out.")
    ap.add_argument("--node", default="rightsizing-control-plane")
    ap.add_argument("--node-mem-mib", type=float, default=0.0,
                    help="override measured free memory instead of querying the node")
    ap.add_argument("--node-cpu", type=float, default=0.0,
                    help="override measured free vCPU instead of querying the node")
    ap.add_argument("--waves", type=int, default=0,
                    help="force a wave count instead of solving for the smallest")
    ap.add_argument("--traces", default="",
                    help="staged trace directory; enables boundary-VM pinning")
    ap.add_argument("--retention-mib", type=float, default=59.11,
                    help="measured peak(memory.current - running-max demand), the "
                         "overhead that actually governs OOM (see ENVIRONMENT_NOTES.md)")
    ap.add_argument("--boundary-mib", type=float, default=40.0,
                    help="a VM is margin-decided, and pinned to wave 1, if the margin "
                         "would have to be wrong by no more than this to flip its "
                         "OOM/survive outcome (default 40, comfortably wider than the "
                         "25.6 MiB measured safe band)")
    args = ap.parse_args()

    if args.node_mem_mib and args.node_cpu:
        free_mem, free_cpu = args.node_mem_mib, args.node_cpu
    else:
        free_mem, free_cpu = node_free(args.node)

    items = []
    with open(args.subset, newline="") as fh:
        for row in csv.DictReader(fh):
            items.append((
                row["vm_id"],
                args.margin_mib + float(row["rec_mem_gb"]) * args.scale * 1024,
                float(row["rec_cores"]) * args.scale,
            ))

    budget_mem = free_mem * (1 - args.headroom)
    budget_cpu = free_cpu * (1 - args.headroom)

    # Which VMs the margin actually decides. `need` is how much fixed overhead
    # this VM must be granted on top of its scaled budget to survive; it OOMs
    # iff need > margin, so |need - margin| is the margin error that flips it.
    boundary, needs = [], {}
    if args.traces:
        with open(args.subset, newline="") as fh:
            for row in csv.DictReader(fh):
                vm_id = row["vm_id"]
                budget = float(row["rec_mem_gb"]) * args.scale * 1024
                need = (peak_demand_mib(os.path.join(args.traces, f"{vm_id}.csv"))
                        + args.retention_mib - budget)
                needs[vm_id] = round(need, 2)
                if abs(need - args.margin_mib) <= args.boundary_mib:
                    boundary.append(vm_id)
        boundary.sort(key=lambda s: abs(needs[s] - args.margin_mib))
        print(f"margin-decided VMs pinned to wave 1 "
              f"(|need - {args.margin_mib}| <= {args.boundary_mib} MiB):")
        for vm_id in boundary:
            verdict = "OOM" if needs[vm_id] > args.margin_mib else "survive"
            print(f"  vm {vm_id:>5}  need {needs[vm_id]:7.2f} MiB  "
                  f"-> predicted {verdict} (flips at {abs(needs[vm_id] - args.margin_mib):5.2f} MiB error)")
        print()

    if args.waves:
        k = args.waves
        waves = pack(items, k, boundary)
    else:
        k = 1
        while True:
            waves = pack(items, k, boundary)
            if all(sum(m for _, m, _ in w) <= budget_mem
                   and sum(c for _, _, c in w) <= budget_cpu for w in waves):
                break
            k += 1
            if k > len(items):
                raise SystemExit("no wave count fits; lower --margin-mib or --scale")

    os.makedirs(args.out, exist_ok=True)
    # Clear any stale wave files from a previous (different-K) split, so a
    # leftover wave5_vms.txt can never be applied against a 4-wave plan.
    for stale in os.listdir(args.out):
        if stale.startswith("wave") and stale.endswith("_vms.txt"):
            os.remove(os.path.join(args.out, stale))

    plan = {"subset": args.subset, "margin_mib": args.margin_mib,
            "scale": args.scale, "headroom": args.headroom,
            "retention_mib": args.retention_mib,
            "boundary_mib": args.boundary_mib if args.traces else None,
            "boundary_vm_ids": boundary, "need_mib": needs,
            "free_mem_mib": round(free_mem, 2), "free_cpu": round(free_cpu, 3),
            "waves": []}
    for i, w in enumerate(waves, 1):
        ids = sorted((vm for vm, _, _ in w), key=lambda s: int(s))
        path = os.path.join(args.out, f"wave{i}_vms.txt")
        with open(path, "w", newline="\n") as fh:
            fh.write("\n".join(ids) + "\n")
        mem = sum(m for _, m, _ in w)
        cpu = sum(c for _, _, c in w)
        plan["waves"].append({
            "wave": i, "n": len(ids), "vm_ids": ids,
            "mem_mib": round(mem, 1), "cpu_cores": round(cpu, 4),
            "mem_headroom_pct": round(100 * (1 - mem / free_mem), 1),
            "cpu_headroom_pct": round(100 * (1 - cpu / free_cpu), 1),
        })
        print(f"wave {i}: {len(ids):2d} pods  {mem:7.1f} Mi "
              f"({100 * (1 - mem / free_mem):4.1f}% headroom)  "
              f"{cpu:5.3f} vCPU ({100 * (1 - cpu / free_cpu):4.1f}% headroom)")

    with open(os.path.join(args.out, "wave_plan.json"), "w") as fh:
        json.dump(plan, fh, indent=2)
    print(f"\n{k} waves, margin {args.margin_mib} MiB, scale {args.scale}, "
          f"free {free_mem:.1f} Mi / {free_cpu:.2f} vCPU -> {args.out}/wave_plan.json")


if __name__ == "__main__":
    main()
