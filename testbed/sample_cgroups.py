#!/usr/bin/env python3
"""
sample_cgroups.py -- poll each replay pod's cgroup memory while a wave runs.

`memory.current` and `memory.peak` live in the pod's cgroup, which the kubelet
removes shortly after the container terminates. An OOMKilled pod therefore
takes its own peak with it: by the time evidence is collected, the only thing
left is the OOMKilled reason, not how close to the limit the pod actually ran.
This samples it while the wave is live, so a survivor's headroom and a
victim's last observed footprint are both on record.

`memory.peak` is a kernel-maintained high-water mark, so the poll interval
bounds only how late the final reading is taken, not how much of the peak is
missed -- except for a pod whose cgroup is torn down between two polls, whose
last reading is the last poll before it died. Both numbers are reported.

Writes TSV: iso_ts, elapsed_s, pod, vm_id, phase, limit_bytes, current_bytes,
peak_bytes. One row per pod per poll.

Usage:
    python3 sample_cgroups.py --out evidence/10_wave1_cgroup_samples.tsv
"""

import argparse
import json
import subprocess
import time
from datetime import datetime, timezone

CG = "/sys/fs/cgroup/kubelet.slice/kubelet-kubepods.slice"
LABEL = "app=rightsizing-replay"


def pods():
    """Current replay pods: name -> (uid, vm_id, phase, limit_bytes)."""
    out = subprocess.run(
        ["kubectl", "get", "pods", "-l", LABEL, "-o", "json"],
        capture_output=True, text=True)
    if out.returncode != 0:
        return {}
    result = {}
    for p in json.loads(out.stdout)["items"]:
        lim = p["spec"]["containers"][0]["resources"]["limits"]["memory"]
        result[p["metadata"]["name"]] = (
            p["metadata"]["uid"],
            p["metadata"]["labels"].get("vm", ""),
            p["status"]["phase"],
            int(lim),
        )
    return result


def read_cgroups(node, uids):
    """One exec for the whole wave: uid -> (current, peak), missing if gone."""
    if not uids:
        return {}
    parts = []
    for uid in uids:
        slice_dir = f"{CG}/kubelet-kubepods-pod{uid.replace('-', '_')}.slice"
        # Print uid, current, peak on one line; '-' for a cgroup already gone.
        parts.append(
            f'printf "%s\\t%s\\t%s\\n" "{uid}" '
            f'"$(cat {slice_dir}/memory.current 2>/dev/null || echo -)" '
            f'"$(cat {slice_dir}/memory.peak 2>/dev/null || echo -)"')
    out = subprocess.run(
        ["docker", "exec", node, "sh", "-c", "; ".join(parts)],
        capture_output=True, text=True)
    vals = {}
    for line in out.stdout.splitlines():
        f = line.split("\t")
        if len(f) == 3:
            vals[f[0]] = (f[1], f[2])
    return vals


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--node", default="rightsizing-control-plane")
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--max-seconds", type=float, default=14400)
    args = ap.parse_args()

    start = time.time()
    seen_any = False
    with open(args.out, "w", newline="\n") as fh:
        fh.write("iso_ts\telapsed_s\tpod\tvm_id\tphase\tlimit_bytes\t"
                 "current_bytes\tpeak_bytes\n")
        while time.time() - start < args.max_seconds:
            snap = pods()
            if snap:
                seen_any = True
            elif seen_any:
                break
            vals = read_cgroups(args.node, [u for u, _, _, _ in snap.values()])
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            elapsed = round(time.time() - start, 1)
            for name, (uid, vm_id, phase, lim) in sorted(snap.items()):
                cur, peak = vals.get(uid, ("-", "-"))
                fh.write(f"{now}\t{elapsed}\t{name}\t{vm_id}\t{phase}\t{lim}\t"
                         f"{cur}\t{peak}\n")
            fh.flush()

            # Stop once every pod has reached a terminal phase -- the cgroups
            # are gone and there is nothing further to sample.
            if snap and all(ph in ("Succeeded", "Failed")
                            for _, _, ph, _ in snap.values()):
                break
            time.sleep(args.interval)

    print(f"sampling finished after {round(time.time() - start)}s -> {args.out}")


if __name__ == "__main__":
    main()
