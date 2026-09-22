#!/usr/bin/env python3
"""
make_synthetic_traces.py -- generate Bitbrains-format traces for smoke testing.

Lets you validate the whole pipeline before the real GWA-T-12 download lands.
Produces a mix of VM archetypes so the baselines have something to separate:

    steady   low, flat utilisation             -> large safe reclamation
    diurnal  daily cycle with modest amplitude -> P95 close to peak
    bursty   low baseline, rare tall spikes    -> P95 will cause violations
    idle     almost nothing                    -> dominates naive savings totals

The spikes are deliberately placed in BOTH windows, so the evaluation window
contains events the observation window did not fully predict. That is the whole
phenomenon under study; a generator without it would make every baseline look
perfect.
"""

import argparse
import os

import numpy as np

HEADER = [
    "Timestamp [ms]",
    "CPU cores",
    "CPU capacity provisioned [MHZ]",
    "CPU usage [MHZ]",
    "CPU usage [%]",
    "Memory capacity provisioned [KB]",
    "Memory usage [KB]",
    "Disk read throughput [KB/s]",
    "Disk write throughput [KB/s]",
    "Network received throughput [KB/s]",
    "Network transmitted throughput [KB/s]",
]

SAMPLES_PER_DAY = 288  # 5-minute sampling


def make_vm(rng, archetype, n_samples):
    cores = int(rng.choice([2, 4, 8]))
    per_core_mhz = 2600.0
    cap_mhz = cores * per_core_mhz
    mem_cap_gb = int(rng.choice([4, 8, 16, 32]))

    t = np.arange(n_samples)
    day = 2 * np.pi * t / SAMPLES_PER_DAY

    if archetype == "steady":
        base = rng.uniform(0.10, 0.25)
        util = base + rng.normal(0, 0.02, n_samples)
        spike_rate = 0.0005
        spike_size = rng.uniform(0.15, 0.30)
    elif archetype == "diurnal":
        base = rng.uniform(0.15, 0.30)
        util = base + 0.15 * np.sin(day) + rng.normal(0, 0.03, n_samples)
        spike_rate = 0.001
        spike_size = rng.uniform(0.20, 0.35)
    elif archetype == "bursty":
        base = rng.uniform(0.05, 0.12)
        util = base + rng.normal(0, 0.02, n_samples)
        spike_rate = 0.004
        spike_size = rng.uniform(0.45, 0.75)
    else:  # idle
        base = rng.uniform(0.005, 0.02)
        util = base + rng.normal(0, 0.003, n_samples)
        spike_rate = 0.0002
        spike_size = rng.uniform(0.05, 0.10)

    # Spikes last 2-6 samples so CPU runs clear the 2-sample violation floor.
    n_spikes = max(1, int(spike_rate * n_samples))
    for _ in range(n_spikes):
        start = rng.integers(0, n_samples - 8)
        length = rng.integers(2, 7)
        util[start : start + length] += spike_size

    util = np.clip(util, 0.001, 1.0)

    # Memory: slower-moving, correlated with CPU but with its own drift.
    mem_frac = np.clip(
        rng.uniform(0.25, 0.55)
        + 0.20 * (util - util.mean())
        + np.cumsum(rng.normal(0, 0.0008, n_samples)),
        0.02,
        0.98,
    )

    ts0 = 1376314846
    lines = []
    for i in range(n_samples):
        use_mhz = util[i] * cap_mhz
        mem_kb = mem_frac[i] * mem_cap_gb * 1024 * 1024
        lines.append(
            ";".join(
                [
                    str(ts0 + i * 300),
                    str(cores),
                    f"{cap_mhz:.3f}",
                    f"{use_mhz:.3f}",
                    f"{util[i] * 100:.3f}",
                    f"{mem_cap_gb * 1024 * 1024:.3f}",
                    f"{mem_kb:.3f}",
                    "0.0",
                    "0.0",
                    "0.0",
                    "0.0",
                ]
            )
        )
    return lines


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="traces_synthetic")
    ap.add_argument("--vms", type=int, default=60)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    rng = np.random.default_rng(args.seed)
    n_samples = args.days * SAMPLES_PER_DAY

    # Skewed toward idle and steady, as real private-cloud fleets are.
    archetypes = ["idle"] * 3 + ["steady"] * 4 + ["diurnal"] * 2 + ["bursty"] * 2

    counts = {}
    for i in range(args.vms):
        arch = archetypes[i % len(archetypes)]
        counts[arch] = counts.get(arch, 0) + 1
        lines = make_vm(rng, arch, n_samples)
        with open(os.path.join(args.out, f"vm{i:04d}_{arch}.csv"), "w") as fh:
            fh.write(";".join(f'"{h}"' for h in HEADER) + "\n")
            fh.write("\n".join(lines) + "\n")

    print(f"wrote {args.vms} traces x {n_samples} samples ({args.days}d) to {args.out}/")
    print("archetype mix:", counts)


if __name__ == "__main__":
    main()
