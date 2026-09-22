#!/usr/bin/env python3
"""
replay_agent.py — in-pod workload replayer for VM right-sizing evaluation.

Reads one VM's evaluation-window trace and reproduces its CPU and memory demand
inside a container whose limits have been set to a right-sizing recommendation.
Resource exhaustion then becomes physically observable:

  CPU    -> CFS throttling, read from the container's own cgroup
  Memory -> OOMKill, observed externally via the pod's termination state

Trace time is compressed: one trace sample (5 min of wall time in Bitbrains)
is replayed over SLICE_SECONDS seconds.

Env vars:
  TRACE_FILE      path to the per-VM CSV (semicolon-delimited Bitbrains format)
  SLICE_SECONDS   replay seconds per trace sample (default 1.0)
  VM_ID           label written into the log
  LOG_FILE        where to append the per-sample record (default /var/log/replay.jsonl)
  MEM_SAFETY      fraction of demanded memory actually touched (default 1.0)
"""

import json
import math
import multiprocessing as mp
import os
import signal
import sys
import time

# --- Bitbrains fastStorage column names -------------------------------------
COL_TS = "Timestamp [ms]"
COL_CORES = "CPU cores"
COL_CAP_MHZ = "CPU capacity provisioned [MHZ]"
COL_USE_MHZ = "CPU usage [MHZ]"
COL_MEM_CAP_KB = "Memory capacity provisioned [KB]"
COL_MEM_USE_KB = "Memory usage [KB]"


# --- cgroup throttle counters (v1 and v2) -----------------------------------
def _read_cpu_stat():
    """Return (nr_throttled, throttled_microseconds) or (None, None)."""
    v2 = "/sys/fs/cgroup/cpu.stat"
    v1 = "/sys/fs/cgroup/cpu/cpu.stat"
    try:
        if os.path.exists(v2):
            vals = {}
            with open(v2) as fh:
                for line in fh:
                    parts = line.split()
                    if len(parts) == 2:
                        vals[parts[0]] = int(parts[1])
            if "throttled_usec" in vals:
                return vals.get("nr_throttled", 0), vals.get("throttled_usec", 0)
        if os.path.exists(v1):
            vals = {}
            with open(v1) as fh:
                for line in fh:
                    parts = line.split()
                    if len(parts) == 2:
                        vals[parts[0]] = int(parts[1])
            # v1 reports nanoseconds
            return vals.get("nr_throttled", 0), vals.get("throttled_time", 0) // 1000
    except (OSError, ValueError):
        pass
    return None, None


# --- CPU burner -------------------------------------------------------------
def _burn_worker(duty_val, stop_evt, period=0.1):
    """Busy-loop for a shared, live-updatable duty fraction of each `period`.

    Workers are spawned once and never restarted; the parent changes duty_val
    between trace samples. Respawning per sample would add ~2s of process
    startup to every slice and destroy replay timing fidelity.
    """
    while not stop_evt.is_set():
        duty = max(0.0, min(1.0, duty_val.value))
        busy = period * duty
        idle = period - busy
        if busy > 0:
            end = time.perf_counter() + busy
            while time.perf_counter() < end:
                pass
        if idle > 0:
            time.sleep(idle)


class CpuLoad:
    """Persistent burner pool targeting a live-adjustable CPU demand in cores.

    Pool width is fixed at start from the container's CPU limit, since demand
    can never usefully exceed it. Demand above the limit saturates the pool and
    the shortfall shows up as CFS throttling, which is exactly the signal we
    are trying to observe.
    """

    def __init__(self, max_cores):
        self.n = max(1, math.ceil(max_cores))
        self.duty = mp.Value("d", 0.0)
        self.stop_evt = mp.Event()
        self.procs = []
        for _ in range(self.n):
            p = mp.Process(
                target=_burn_worker, args=(self.duty, self.stop_evt), daemon=True
            )
            p.start()
            self.procs.append(p)

    def set_target(self, cores):
        # Requesting more than the pool can deliver is intentional: it pins
        # every worker at 100% and lets the kernel throttle.
        self.duty.value = max(0.0, min(1.0, cores / self.n))

    def clear(self):
        self.stop_evt.set()
        for p in self.procs:
            p.join(timeout=2.0)
            if p.is_alive():
                p.terminate()
        self.procs = []


# --- Memory occupant --------------------------------------------------------
class MemLoad:
    """Holds a resident buffer of the requested size, touching every page."""

    PAGE = 4096

    def __init__(self, safety=1.0):
        self.buf = bytearray()
        self.safety = safety

    def set_target(self, mb):
        want = int(max(0.0, mb) * self.safety * 1024 * 1024)
        if want == len(self.buf):
            return
        # Release the old buffer BEFORE allocating the new one. Reassigning
        # `self.buf = bytearray(want)` directly evaluates the right-hand
        # side -- allocating and touching the new buffer -- while the old
        # one is still referenced by `self.buf`, so on any upward jump in
        # demand the container transiently holds both buffers at once
        # (old_size + new_size resident simultaneously). That is enough on
        # its own to exceed a limit that comfortably fits either buffer
        # individually -- reproduced standalone with no CPU quota involved
        # (see ENVIRONMENT_NOTES.md, tests/test_replay_agent.py).
        self.buf = bytearray()
        self.buf = bytearray(want)
        # Touch each page so the kernel actually commits it.
        for off in range(0, want, self.PAGE):
            self.buf[off] = 1


# --- Trace loading ----------------------------------------------------------
def load_trace(path):
    """Parse a semicolon-delimited Bitbrains CSV into demand rows."""
    rows = []
    with open(path, newline="") as fh:
        header = fh.readline().rstrip("\n").rstrip("\r").split(";")
        header = [h.strip().strip('"') for h in header]
        idx = {name: i for i, name in enumerate(header)}

        required = [COL_USE_MHZ, COL_MEM_USE_KB]
        missing = [c for c in required if c not in idx]
        if missing:
            raise SystemExit(f"trace {path} missing columns: {missing}\nsaw: {header}")

        for line in fh:
            parts = line.rstrip("\n").rstrip("\r").split(";")
            if len(parts) < len(header):
                continue
            try:
                use_mhz = float(parts[idx[COL_USE_MHZ]])
                mem_kb = float(parts[idx[COL_MEM_USE_KB]])
                cap_mhz = float(parts[idx[COL_CAP_MHZ]]) if COL_CAP_MHZ in idx else 0.0
                cores = float(parts[idx[COL_CORES]]) if COL_CORES in idx else 1.0
                ts = float(parts[idx[COL_TS]]) if COL_TS in idx else len(rows)
            except ValueError:
                continue

            # Convert MHz demand into core-equivalents using the VM's own
            # provisioned clock rate. Falls back to 1 core = cap/cores.
            per_core_mhz = (cap_mhz / cores) if (cap_mhz > 0 and cores > 0) else 0.0
            demand_cores = (use_mhz / per_core_mhz) if per_core_mhz > 0 else 0.0

            rows.append(
                {
                    "ts": ts,
                    "cpu_cores": demand_cores,
                    "mem_mb": mem_kb / 1024.0,
                }
            )
    return rows


# --- Main loop --------------------------------------------------------------
def main():
    trace_file = os.environ.get("TRACE_FILE", "/trace/vm.csv")
    slice_s = float(os.environ.get("SLICE_SECONDS", "1.0"))
    vm_id = os.environ.get("VM_ID", os.path.basename(trace_file))
    log_file = os.environ.get("LOG_FILE", "/var/log/replay.jsonl")
    mem_safety = float(os.environ.get("MEM_SAFETY", "1.0"))

    rows = load_trace(trace_file)
    if not rows:
        raise SystemExit(f"no usable rows in {trace_file}")

    # Pool width comes from the container's own CPU limit, injected by the
    # manifest generator as LIMIT_CORES.
    limit_cores = float(os.environ.get("LIMIT_CORES", str(os.cpu_count() or 1)))
    cpu = CpuLoad(limit_cores)
    mem = MemLoad(safety=mem_safety)

    def _shutdown(signum, frame):
        cpu.clear()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    prev_n, prev_us = _read_cpu_stat()
    started = time.time()

    # Every record is written to LOG_FILE *and* stdout. LOG_FILE lives in the
    # container's writable layer, only reachable via `kubectl exec cat ...`,
    # which requires a running container process -- a pod that OOMKills or
    # simply completes (restartPolicy: Never) has none, so that path alone
    # loses the very data T8 needs (per-sample throttling records) for any
    # pod evidence isn't collected from before it terminates. `kubectl logs`
    # reads the pod's captured stdout via the kubelet and works after
    # termination as long as the pod object still exists, so mirroring to
    # stdout removes the race entirely.
    def emit(log_fh, obj):
        line = json.dumps(obj)
        log_fh.write(line + "\n")
        print(line, flush=True)

    with open(log_file, "a", buffering=1) as log:
        emit(
            log,
            {
                "event": "start",
                "vm_id": vm_id,
                "samples": len(rows),
                "slice_seconds": slice_s,
                "cgroup_visible": prev_n is not None,
            },
        )

        for i, row in enumerate(rows):
            deadline = started + (i + 1) * slice_s

            cpu.set_target(row["cpu_cores"])
            mem.set_target(row["mem_mb"])

            # Sleep to an absolute deadline rather than a fixed duration, so
            # the cost of page-touching does not accumulate across samples.
            remaining = deadline - time.time()
            if remaining > 0:
                time.sleep(remaining)

            n, us = _read_cpu_stat()
            d_n = (n - prev_n) if (n is not None and prev_n is not None) else None
            d_us = (us - prev_us) if (us is not None and prev_us is not None) else None
            prev_n, prev_us = n, us

            emit(
                log,
                {
                    "vm_id": vm_id,
                    "sample": i,
                    "trace_ts": row["ts"],
                    "elapsed_s": round(time.time() - started, 2),
                    "demand_cores": round(row["cpu_cores"], 4),
                    "demand_mem_mb": round(row["mem_mb"], 1),
                    "throttled_periods": d_n,
                    "throttled_us": d_us,
                    "cpu_exhausted": bool(d_n) and d_n > 0,
                },
            )

        cpu.clear()
        emit(log, {"event": "complete", "vm_id": vm_id})


if __name__ == "__main__":
    main()
