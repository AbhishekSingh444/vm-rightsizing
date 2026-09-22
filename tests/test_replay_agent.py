#!/usr/bin/env python3
"""
test_replay_agent.py -- regression test for the MemLoad transient-double-
allocation bug (testbed/replay_agent.py).

Run:  python3 test_replay_agent.py

Background: MemLoad.set_target used to do `self.buf = bytearray(want)`
directly. That reassignment evaluates its right-hand side -- allocating and
zero-touching the NEW buffer -- before the OLD one's refcount drops, so on
any upward jump in demand the process transiently holds both buffers at
once. A demand ramp whose successive targets each individually fit under a
limit can still overshoot that limit at the moment of transition, purely
from allocation timing -- no CPU throttling required (reproduced standalone
against the real cluster image; see ENVIRONMENT_NOTES.md "transient double
allocation").

This is checked with tracemalloc rather than an OS-level RSS/cgroup read so
the test is deterministic and runs on any platform (including native
Windows, where this suite is normally invoked -- see ENVIRONMENT_NOTES.md).
tracemalloc's traced-memory peak tracks the same PyObject allocator calls
that back a bytearray's buffer, so it reproduces the coexistence effect
without needing a container or a memory cgroup.

The test asserts on PEAK traced memory, not on whether the ramp "completes"
-- a completion-only assertion would pass even with the bug present (nothing
here can literally OOM the test process), so it would fail to fail for the
right reason.
"""

import os
import sys
import tracemalloc

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "testbed"))

from replay_agent import MemLoad  # noqa: E402

PAGE = 4096


class _BuggyMemLoad:
    """The pre-fix implementation, kept here as a negative control so this
    test demonstrates the property it's named for (fails against the old
    code, passes against the new) without needing git history."""

    def __init__(self, safety=1.0):
        self.buf = bytearray()
        self.safety = safety

    def set_target(self, mb):
        want = int(max(0.0, mb) * self.safety * 1024 * 1024)
        if want == len(self.buf):
            return
        self.buf = bytearray(want)
        for off in range(0, want, PAGE):
            self.buf[off] = 1


def peak_traced_bytes(cls, targets_mb):
    """Run a demand ramp through `cls` and return tracemalloc's peak."""
    tracemalloc.stop()  # in case a previous run left it started
    mem = cls()
    tracemalloc.start()
    try:
        for mb in targets_mb:
            mem.set_target(mb)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return peak


def main():
    failures = 0

    # Ramp shape: each individual target fits comfortably under LIMIT_MB
    # (60MB and 80MB both well under 100MB), but 60+80=140MB does not.
    # This is the exact shape that killed VM 324/344 in the wave-1 run:
    # predicted memory-safe under the offline demand-vs-allocation rule
    # (every individual sample <= allocation), but the container's
    # transient double buffer briefly exceeds the limit anyway.
    LIMIT_MB = 100
    LIMIT_BYTES = LIMIT_MB * 1024 * 1024
    targets_mb = [10, 60, 80, 20, 5]  # 60 -> 80 is the overshoot transition

    print("MemLoad transient-double-allocation regression:")

    buggy_peak = peak_traced_bytes(_BuggyMemLoad, targets_mb)
    buggy_over = buggy_peak > LIMIT_BYTES
    ok = buggy_over
    failures += 0 if ok else 1
    print(
        f"  {'PASS' if ok else 'FAIL'}  pre-fix MemLoad overshoots a limit neither "
        f"target alone would breach\n"
        f"        peak={buggy_peak / 1024 / 1024:.1f}MiB limit={LIMIT_MB}MiB "
        f"(expected peak > limit)"
    )

    fixed_peak = peak_traced_bytes(MemLoad, targets_mb)
    ok = fixed_peak <= LIMIT_BYTES
    failures += 0 if ok else 1
    print(
        f"  {'PASS' if ok else 'FAIL'}  fixed MemLoad (testbed/replay_agent.py) stays "
        f"under the same limit\n"
        f"        peak={fixed_peak / 1024 / 1024:.1f}MiB limit={LIMIT_MB}MiB "
        f"(expected peak <= limit)"
    )

    # Sanity check: the fixed peak should track the LARGEST single target
    # (80MB), not the sum of any two -- confirms the fix isn't just
    # coincidentally under LIMIT_MB for this particular ramp.
    max_target_bytes = max(targets_mb) * 1024 * 1024
    ok = fixed_peak < max_target_bytes * 1.5
    failures += 0 if ok else 1
    print(
        f"  {'PASS' if ok else 'FAIL'}  fixed peak tracks the largest single target, "
        f"not a sum\n"
        f"        peak={fixed_peak / 1024 / 1024:.1f}MiB "
        f"max_target={max(targets_mb)}MiB (expected peak < 1.5x max_target)"
    )

    total = 3
    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
