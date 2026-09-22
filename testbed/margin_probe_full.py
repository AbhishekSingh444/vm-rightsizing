#!/usr/bin/env python3
"""
additivity_probe.py -- measure the FULL replay agent's memory overhead above
replayed demand, under cluster-matching CPU quota.

Runs replay_agent.main() unmodified (MemLoad + CpuLoad both live) and, from a
sampler thread, records the container's own cgroup memory.current alongside the
memory demand currently targeted by MemLoad. The quantity of interest is

    margin_required = max_t ( memory.current(t) - demand(t) )

which is exactly the additive term gen_manifests.py must add on top of the
scaled limit for the OOM condition to reduce to `demand > limit`.

MemLoad.set_target is wrapped so the *new* target is recorded BEFORE the
allocation happens: any transient held during a transition is attributed to
the demand level being moved to, which is the conservative attribution (the
limit has to cover that transition).

Outputs (into /out):
  probe_samples.csv   t_rel,mem_current_mib,demand_mib,diff_mib,sample_idx
  probe_summary.json  peak diff, memory.peak, argmax context
"""

import json
import os
import sys
import threading
import time

sys.path.insert(0, "/app")
import replay_agent as ra  # noqa: E402

OUT = os.environ.get("PROBE_OUT", "/out")
HZ = float(os.environ.get("PROBE_HZ", "20"))

STATE = {"demand_mib": 0.0, "sample": -1}


def _read_int(path):
    try:
        with open(path) as fh:
            return int(fh.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


MEM_CURRENT = "/sys/fs/cgroup/memory.current"
MEM_PEAK = "/sys/fs/cgroup/memory.peak"

# --- wrap MemLoad.set_target: record the target before allocating -----------
_orig_set_target = ra.MemLoad.set_target


def _patched_set_target(self, mb):
    STATE["demand_mib"] = float(mb)
    STATE["sample"] += 1
    _orig_set_target(self, mb)
    STATE["demand_mib"] = float(mb)


ra.MemLoad.set_target = _patched_set_target

# --- sampler thread ---------------------------------------------------------
stop = threading.Event()
best = {"diff": -1e9, "t": 0.0, "mem": 0.0, "demand": 0.0, "sample": -1}
rows = []
t0 = time.time()


def sampler():
    period = 1.0 / HZ
    nxt = time.perf_counter()
    while not stop.is_set():
        cur = _read_int(MEM_CURRENT)
        if cur is not None:
            mem = cur / 1048576.0
            dem = STATE["demand_mib"]
            diff = mem - dem
            rows.append((round(time.time() - t0, 3), round(mem, 3),
                         round(dem, 3), round(diff, 3), STATE["sample"]))
            if diff > best["diff"]:
                best.update(diff=diff, t=time.time() - t0, mem=mem,
                            demand=dem, sample=STATE["sample"])
        nxt += period
        time.sleep(max(0.0, nxt - time.perf_counter()))


def write_out():
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "probe_samples.csv"), "w") as fh:
        fh.write("t_rel,mem_current_mib,demand_mib,diff_mib,sample_idx\n")
        for r in rows:
            fh.write(",".join(str(x) for x in r) + "\n")
    peak = _read_int(MEM_PEAK)
    summary = {
        "vm_id": os.environ.get("VM_ID"),
        "trace_file": os.environ.get("TRACE_FILE"),
        "limit_cores": os.environ.get("LIMIT_CORES"),
        "slice_seconds": os.environ.get("SLICE_SECONDS"),
        "probe_hz": HZ,
        "n_probe_samples": len(rows),
        "wall_seconds": round(time.time() - t0, 1),
        "max_diff_mib": round(best["diff"], 3),
        "at_t_rel": round(best["t"], 2),
        "at_mem_current_mib": round(best["mem"], 3),
        "at_demand_mib": round(best["demand"], 3),
        "at_replay_sample": best["sample"],
        "max_mem_current_mib": round(max((r[1] for r in rows), default=0.0), 3),
        "max_demand_mib": round(max((r[2] for r in rows), default=0.0), 3),
        "cgroup_memory_peak_mib": round(peak / 1048576.0, 3) if peak else None,
    }
    with open(os.path.join(OUT, "probe_summary.json"), "w") as fh:
        json.dump(summary, fh, indent=2)
    print("PROBE_SUMMARY " + json.dumps(summary), flush=True)


def main():
    th = threading.Thread(target=sampler, daemon=True)
    th.start()
    try:
        ra.main()
    except SystemExit:
        pass
    finally:
        stop.set()
        th.join(timeout=3.0)
        write_out()


if __name__ == "__main__":
    main()
