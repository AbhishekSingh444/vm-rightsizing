#!/usr/bin/env python3
"""
test_demo_guard.py -- the demo refuses millisecond timestamps.

Run:  python3 test_demo_guard.py

The loader reads "Timestamp [ms]" as seconds and never checks, so a file in
real milliseconds runs end to end and produces a frontier on ~9 rows. The
guard in front of `make demo` is the only thing that stops it. These tests
pin that it fires on that file, stays quiet on a correct one, and exits
non-zero so the Makefile chain halts.
"""

import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from demo_guard import check_file  # noqa: E402

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
HEADER = ("Timestamp [ms];\tCPU cores;\tCPU capacity provisioned [MHZ];\t"
          "CPU usage [MHZ];\tCPU usage [%];\tMemory capacity provisioned [KB];\t"
          "Memory usage [KB]")


def write_trace(path, step, n=8640, t0=1376314846):
    """30 days of 5-minute rows; `step` is the timestamp increment per row."""
    with open(path, "w", newline="") as f:
        f.write(HEADER + "\n")
        for i in range(n):
            f.write(f"{t0 + i * step};\t2;\t5200;\t1300;\t25;\t8388608;\t2097152\n")


def check(ok, title, detail):
    print(f"  {'PASS' if ok else 'FAIL'}  {title}\n        {detail}")
    return 0 if ok else 1


def main():
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        sec = os.path.join(tmp, "seconds.csv")
        ms = os.path.join(tmp, "millis.csv")
        fast = os.path.join(tmp, "fast.csv")
        write_trace(sec, 300)
        write_trace(ms, 300_000, t0=1376314846000)
        write_trace(fast, 30, n=86400)

        r = check_file(sec)
        failures += check(r is None, "a 5-minute trace in seconds passes",
                          f"reason={r!r}")

        r = check_file(ms)
        failures += check(
            r is not None and r.startswith("timestamps look like milliseconds, not seconds"),
            "the same trace in milliseconds is rejected with the stated message",
            f"reason={r!r}")

        r = check_file(fast)
        failures += check(r is None, "30-second sampling in seconds is not mistaken for ms",
                          f"reason={r!r}")

        good = os.path.join(tmp, "good")
        os.mkdir(good)
        write_trace(os.path.join(good, "1.csv"), 300)
        bad = os.path.join(tmp, "bad")
        os.mkdir(bad)
        write_trace(os.path.join(bad, "1.csv"), 300)
        write_trace(os.path.join(bad, "2.csv"), 300_000, t0=1376314846000)
        rc_good = subprocess.run([sys.executable, os.path.join(SRC, "demo_guard.py"),
                                  "--traces", good], capture_output=True).returncode
        p = subprocess.run([sys.executable, os.path.join(SRC, "demo_guard.py"),
                            "--traces", bad], capture_output=True, text=True)
        failures += check(
            rc_good == 0 and p.returncode != 0 and "REJECT 2.csv" in p.stderr
            and "REJECT 1.csv" not in p.stderr,
            "the CLI exits non-zero on one bad file and names only that file",
            f"good rc={rc_good}, bad rc={p.returncode}, stderr={p.stderr.strip()!r}")

    total = 4
    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
