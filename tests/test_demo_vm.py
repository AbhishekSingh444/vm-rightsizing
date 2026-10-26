#!/usr/bin/env python3
"""
test_demo_vm.py -- the per-VM demo shows every section and never tracebacks.

Run:  python3 test_demo_vm.py

`make demo-vm` runs on files handed over on the day, so the failure mode that
matters is a stack trace in front of an audience. These tests pin that a good
trace prints every section the walkthrough promises, that each kind of bad
file is named with its reason while the good file still runs, that a missing
or empty input folder is a one-line error, and that nothing is written
outside --out.
"""

import os
import subprocess
import sys
import tempfile

import numpy as np

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
DEMO = os.path.join(SRC, "demo_vm.py")
HEADER = ("Timestamp [ms];\tCPU cores;\tCPU capacity provisioned [MHZ];\t"
          "CPU usage [MHZ];\tCPU usage [%];\tMemory capacity provisioned [KB];\t"
          "Memory usage [KB]")


def write_trace(path, n=8640, step=300, t0=1376314846, seed=0):
    """30 days of 5-minute rows: 4 cores at 2,600 MHz, 16 GB, with a
    lognormal body and occasional spikes so both tails can be fitted."""
    rng = np.random.default_rng(seed)
    cpu = np.clip(rng.lognormal(np.log(1500), 0.5, n), 0, 10400)
    mem = np.clip(rng.lognormal(np.log(4 * 1048576), 0.3, n), 0, 16 * 1048576)
    with open(path, "w", newline="") as f:
        f.write(HEADER + "\n")
        for i in range(n):
            f.write(f"{t0 + i * step};\t4;\t10400;\t{cpu[i]:.3f};\t{100 * cpu[i] / 10400:.3f};\t"
                    f"{16 * 1048576};\t{mem[i]:.3f}\n")


def run(*args):
    p = subprocess.run([sys.executable, DEMO, *args], capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def check(ok, title, detail):
    print(f"  {'PASS' if ok else 'FAIL'}  {title}\n        {detail}")
    return 0 if ok else 1


def main():
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        good = os.path.join(tmp, "good")
        os.mkdir(good)
        write_trace(os.path.join(good, "7.csv"))
        out = os.path.join(tmp, "out")
        rc, so, se = run("--input", good, "--out", out)
        want = ["VM 7", "original allocation : 4 vCPU, 16.00 GB",
                "observed over the observation window", "threshold u", "shape xi",
                "theta", "pred P", "M(0.001)", "M(0.01)", "M(0.1)", "peak", "p95",
                "held-out evaluation window", "recl vCPU-h", "recl GB-h",
                "graciously provided by Bitbrains IT Services Inc."]
        missing = [w for w in want if w not in so]
        failures += check(rc == 0 and not missing and "Traceback" not in so + se,
                          "a good 30-day trace prints every section and exits 0",
                          f"rc={rc}, missing={missing}, stderr={se.strip()[-200:]!r}")

        written = sorted(os.listdir(out))
        failures += check(
            written == ["demo_vm_per_policy.csv", "demo_vm_report.txt", "run_config.json"]
            and sorted(os.listdir(tmp)) == ["good", "out"],
            "outputs land in --out only: report, per-policy CSV, run_config.json",
            f"--out holds {written}; tmp holds {sorted(os.listdir(tmp))}")

        mixed = os.path.join(tmp, "mixed")
        os.mkdir(mixed)
        write_trace(os.path.join(mixed, "7.csv"))
        write_trace(os.path.join(mixed, "ms.csv"), step=300_000, t0=1376314846000)
        write_trace(os.path.join(mixed, "short.csv"), n=2000)
        open(os.path.join(mixed, "empty.csv"), "w").close()
        with open(os.path.join(mixed, "header_only.csv"), "w") as f:
            f.write(HEADER + "\n")
        with open(os.path.join(mixed, "comma.csv"), "w") as f:
            f.write(HEADER.replace(";\t", ",") + "\n1,2,3,4,5,6,7\n")
        with open(os.path.join(mixed, "binary.csv"), "wb") as f:
            f.write(bytes(range(128, 256)) * 20)
        rc, so, se = run("--input", mixed, "--out", os.path.join(tmp, "out2"))
        expect = {
            "ms.csv": "timestamps look like milliseconds, not seconds",
            "short.csv": "trace too short",
            "empty.csv": "file is empty",
            "header_only.csv": "no row has a numeric value",
            "comma.csv": "not ';'-separated",
            "binary.csv": "unreadable",
        }
        lines = so.splitlines()
        wrong = [f for f, why in expect.items()
                 if not any(l.strip().startswith(f"skipped {f}: ") and why in l for l in lines)]
        failures += check(
            rc == 1 and not wrong and "VM 7" in so and "Traceback" not in so + se,
            "each bad file is skipped with its reason, the good one still runs, exit 1",
            f"rc={rc}, unmatched={wrong}, stderr={se.strip()[-200:]!r}")

        rc_a, so_a, se_a = run("--input", os.path.join(tmp, "nope"), "--out", os.path.join(tmp, "o3"))
        empty_dir = os.path.join(tmp, "empty_dir")
        os.mkdir(empty_dir)
        rc_b, so_b, se_b = run("--input", empty_dir, "--out", os.path.join(tmp, "o4"))
        failures += check(
            rc_a != 0 and "does not exist" in se_a and rc_b != 0
            and "no .csv files" in se_b and "Traceback" not in se_a + se_b,
            "a missing or empty input folder is a one-line error, not a traceback",
            f"missing: rc={rc_a} {se_a.strip()!r}; empty: rc={rc_b} {se_b.strip()!r}")

    total = 4
    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
