#!/usr/bin/env python3
"""
test_baselines.py -- unit tests for the violation counter and flavour snapping.

Run:  python3 test_baselines.py

These are worth keeping and worth showing. The violation counter is the single
place where an off-by-one silently changes every number in the results table,
and "the totals looked plausible" is not evidence that it is right.
"""

import os
import sys
import tempfile

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from baselines import (
    capacity_glitch_ratio,
    count_violations,
    detect_capacity_shift,
    is_capacity_glitch,
    load_vm,
    snap_up,
    split_obs_eval,
)

BITBRAINS_HEADER = (
    "Timestamp [ms];CPU cores;CPU capacity provisioned [MHZ];CPU usage [MHZ];"
    "CPU usage [%];Memory capacity provisioned [KB];Memory usage [KB];"
    "Disk read throughput [KB/s];Disk write throughput [KB/s];"
    "Network received throughput [KB/s];Network transmitted throughput [KB/s]\n"
)


def make_vm_csv(path, n_rows, mem_cap_kb_fn, cores=2, cap_mhz=5000.0, interval_s=300):
    """Write a minimal synthetic Bitbrains-format CSV for load_vm tests.
    mem_cap_kb_fn(row_index) -> the memory-capacity-provisioned value for that row.
    """
    with open(path, "w") as fh:
        fh.write(BITBRAINS_HEADER)
        for i in range(n_rows):
            ts = 1_600_000_000 + i * interval_s
            mem_cap = mem_cap_kb_fn(i)
            fh.write(f"{ts};{cores};{cap_mhz};1000.0;20.0;{mem_cap};500000.0;0.0;0.0;0.0;0.0\n")

VIOLATION_CASES = [
    # (demand, allocation, min_run, expected_events, expected_samples, note)
    ([1, 1, 1], 2, 2, 0, 0, "never exceeds"),
    ([3, 1, 1], 2, 2, 0, 0, "single-sample CPU spike ignored at min_run=2"),
    ([3, 3, 1], 2, 2, 1, 2, "two-sample run counts as one event"),
    ([3, 3, 3], 2, 2, 1, 3, "longer run is still one event"),
    ([3, 3, 1, 3, 3], 2, 2, 2, 4, "two separate events"),
    ([3, 1, 1], 2, 1, 1, 1, "memory rule: one sample is enough"),
    ([1, 1, 3], 2, 2, 0, 0, "trailing single-sample spike ignored"),
    ([1, 3, 3], 2, 2, 1, 2, "run ending at the array boundary is counted"),
    ([1, 1, 1], 2, 1, 0, 0, "memory rule, no exceedance"),
]

SNAP_CASES = [
    (0.3, [1, 2, 4, 8, 16], 1, "below smallest snaps up to smallest"),
    (1.0, [1, 2, 4, 8, 16], 1, "exact match does not round up"),
    (1.1, [1, 2, 4, 8, 16], 2, "just above snaps to next"),
    (4.0, [1, 2, 4, 8, 16], 4, "exact match mid-catalog"),
    (4.1, [1, 2, 4, 8, 16], 8, "just above mid-catalog"),
    (99.0, [1, 2, 4, 8, 16], 16, "above catalog clamps to largest"),
    (3.0, [2, 4, 8, 16, 32, 64], 4, "memory catalog"),
]


def main():
    failures = 0

    print("count_violations:")
    for demand, alloc, min_run, exp_e, exp_s, note in VIOLATION_CASES:
        got_e, got_s = count_violations(pd.Series(demand), alloc, min_run)
        ok = (got_e, got_s) == (exp_e, exp_s)
        failures += 0 if ok else 1
        print(
            f"  {'PASS' if ok else 'FAIL'}  {note}\n"
            f"        demand={demand} alloc={alloc} min_run={min_run} "
            f"-> events={got_e}, samples={got_s} (expected {exp_e}, {exp_s})"
        )

    print("\nsnap_up:")
    for value, catalog, expected, note in SNAP_CASES:
        got = snap_up(value, catalog)
        ok = got == expected
        failures += 0 if ok else 1
        print(
            f"  {'PASS' if ok else 'FAIL'}  {note}\n"
            f"        snap_up({value}) -> {got} (expected {expected})"
        )

    print("\nsplit_obs_eval:")
    split_failures = 0
    DAY = 86400.0
    OBS_DAYS, EVAL_DAYS = 21, 9

    def check(note, ok, detail):
        nonlocal split_failures
        split_failures += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  {note}\n        {detail}")

    # Uniformly sampled at the nominal 5-minute interval, spanning exactly
    # obs_days+eval_days worth of samples -- like the real fastStorage trace,
    # whose last sample sits one interval short of the nominal day boundary.
    # A strict "last sample >= eval_end" check rejects this real-world case;
    # this must still produce a full split.
    interval = 300.0
    n = int((OBS_DAYS + EVAL_DAYS) * DAY / interval)  # last sample one interval short
    ts = [i * interval for i in range(n)]
    df = pd.DataFrame({"ts": ts})
    result = split_obs_eval(df, OBS_DAYS, EVAL_DAYS)
    ok = result is not None
    if ok:
        obs, ev = result
        exp_obs = int(OBS_DAYS * DAY / interval)
        exp_eval = int(EVAL_DAYS * DAY / interval)
        ok = len(obs) == exp_obs and len(ev) == exp_eval
        detail = (
            f"5-min interval, span one interval short of {OBS_DAYS + EVAL_DAYS}d -> "
            f"obs={len(obs)} eval={len(ev)} (expected {exp_obs}, {exp_eval})"
        )
    else:
        detail = "5-min interval, tight span -> got None, expected a split"
    check("uniformly sampled VM at the nominal 5-minute interval", ok, detail)

    # Sampled finer than 5 minutes. A row-count split using SAMPLE_MINUTES=5
    # would grab far too little wall-clock time; the time-based split must
    # still land on exactly obs_days/eval_days regardless of interval.
    interval = 60.0
    n = int(35 * DAY / interval) + 1
    ts = [i * interval for i in range(n)]
    df = pd.DataFrame({"ts": ts})
    result = split_obs_eval(df, OBS_DAYS, EVAL_DAYS)
    ok = result is not None
    if ok:
        obs, ev = result
        exp_obs = int(OBS_DAYS * DAY / interval)
        exp_eval = int(EVAL_DAYS * DAY / interval)
        ok = len(obs) == exp_obs and len(ev) == exp_eval
        detail = (
            f"1-min interval, 35d span -> obs={len(obs)} eval={len(ev)} "
            f"(expected {exp_obs}, {exp_eval})"
        )
    else:
        detail = "1-min interval, 35d span -> got None, expected a split"
    check("VM sampled finer than 5 minutes", ok, detail)

    # Too short to cover the full obs+eval span -- must be skipped, not
    # silently given a truncated evaluation window.
    interval = 300.0
    n = int(10 * DAY / interval)  # 10 days, less than the 30-day requirement
    ts = [i * interval for i in range(n)]
    df = pd.DataFrame({"ts": ts})
    result = split_obs_eval(df, OBS_DAYS, EVAL_DAYS)
    ok = result is None
    check(
        "VM too short to cover both windows",
        ok,
        f"10d span, needs {OBS_DAYS}+{EVAL_DAYS}d -> result={result!r} (expected None)",
    )

    print("\ncapacity_glitch:")
    glitch_failures = 0

    def check_glitch(note, series, expect_glitch):
        nonlocal glitch_failures
        ratio = capacity_glitch_ratio(series)
        got = is_capacity_glitch(ratio)
        ok = got == expect_glitch
        glitch_failures += 0 if ok else 1
        print(
            f"  {'PASS' if ok else 'FAIL'}  {note}\n"
            f"        first={series.iloc[0] if len(series) else None} "
            f"ratio={ratio} -> glitch={got} (expected {expect_glitch})"
        )

    # The 314.csv pattern: a corrupted first-sample memory-capacity reading
    # (6144 KB) against a file whose real capacity is ~8.39M KB throughout.
    check_glitch(
        "314.csv-shaped: first sample ~1365x below the file median",
        pd.Series([6144.0] + [8388608.0] * 198),
        True,
    )

    # The 103-VM pattern: first sample reads exactly 0 while the VM has a
    # real, positive capacity elsewhere in the file.
    check_glitch(
        "zero first sample against a positive median (the 103-VM pattern)",
        pd.Series([0.0] + [4.0] * 10),
        True,
    )

    # A steady VM: first sample close to its own file's median -- must not
    # be flagged.
    check_glitch(
        "steady VM: first sample near the file median",
        pd.Series([8000.0, 8000.0, 8100.0, 8000.0, 7900.0]),
        False,
    )

    # Exactly at the factor boundary (ratio == 10.0 exactly) is not a glitch;
    # only strictly beyond it is.
    check_glitch(
        "exactly at the 10x boundary is not flagged",
        pd.Series([1000.0] + [100.0] * 10),
        False,
    )
    check_glitch(
        "just past the 10x boundary is flagged",
        pd.Series([1000.1] + [100.0] * 10),
        True,
    )

    # A degenerate all-zero (or empty) series can't anchor a ratio -- must
    # not be flagged, since "differs from the median" is meaningless here.
    check_glitch(
        "all-zero series has no usable median -- not flagged",
        pd.Series([5.0, 0.0, 0.0, 0.0, 0.0]),
        False,
    )
    check_glitch("empty series -- not flagged", pd.Series([], dtype=float), False)

    print("\ndetect_capacity_shift:")
    shift_failures = 0

    def check_shift(note, series, expect_shift):
        nonlocal shift_failures
        r = detect_capacity_shift(series)
        got = r is not None
        ok = got == expect_shift
        shift_failures += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  {note}\n        {r} -> shift={got} (expected {expect_shift})")

    # 133.csv-shaped: a genuine mid-trace resize -- roughly half the file at
    # one level, half at another, ~13x apart. Must be detected.
    check_shift(
        "133.csv-shaped: two large sustained levels, ~13x apart",
        pd.Series([268974080.0] * 4297 + [21000000.0] * 4321),
        True,
    )

    # A steady VM with ordinary sample-to-sample jitter (well under the 5%
    # level tolerance) is one level, not two -- must not be flagged.
    steady = pd.Series([8000.0, 8050.0, 7980.0, 8010.0, 7995.0] * 200)
    check_shift("steady VM with small jitter: one level", steady, False)

    # 314.csv-shaped: a single corrupted sample against an otherwise
    # constant file. The corrupted sample is a cluster of count 1, well
    # under min_share, so this must NOT register as a sustained second
    # level -- that is capacity_glitch_ratio's job, not this function's.
    check_shift(
        "314.csv-shaped: one corrupted sample, not a sustained second level",
        pd.Series([6144.0] + [8388608.0] * 198),
        False,
    )

    # Two sustained levels that exist but are close together (below the 10x
    # shift factor) -- a real but small difference, not what this check is
    # for.
    check_shift(
        "two sustained levels only 3x apart: below the shift factor",
        pd.Series([100.0] * 50 + [300.0] * 50),
        False,
    )

    check_shift("empty series -- no shift", pd.Series([], dtype=float), False)

    print("\nload_vm mid-trace capacity shift gate:")
    load_vm_failures = 0

    with tempfile.TemporaryDirectory() as tmpdir:
        # A VM whose memory capacity shifts partway through, 133.csv-shaped
        # (roughly half the file at one level, half at another, well past
        # the 10x factor) -- must be excluded, not silently loaded with a
        # stale first-sample capacity.
        shifted_path = os.path.join(tmpdir, "shifted.csv")
        make_vm_csv(
            shifted_path,
            n_rows=200,
            mem_cap_kb_fn=lambda i: 25_165_824.0 if i < 100 else 2_097_152.0,  # 24GB then 2GB
        )
        result = load_vm(shifted_path)
        ok = result is None
        load_vm_failures += 0 if ok else 1
        print(
            f"  {'PASS' if ok else 'FAIL'}  VM with a mid-trace memory-capacity shift is excluded\n"
            f"        load_vm(...) -> {result!r} (expected None)"
        )

        # An ordinary VM with constant capacity throughout must still load
        # normally -- the new gate must not over-trigger on a steady VM.
        steady_path = os.path.join(tmpdir, "steady.csv")
        make_vm_csv(steady_path, n_rows=200, mem_cap_kb_fn=lambda i: 8_388_608.0)  # constant 8GB
        result = load_vm(steady_path)
        ok = result is not None
        load_vm_failures += 0 if ok else 1
        print(
            f"  {'PASS' if ok else 'FAIL'}  VM with constant capacity still loads\n"
            f"        load_vm(...) -> {'(df, meta)' if result else None} (expected not None)"
        )

    print("\nload_vm below-flavour-catalog-floor gate:")

    with tempfile.TemporaryDirectory() as tmpdir:
        # Memory below the 2GB catalog floor (1GB constant, cores fine) --
        # no smaller flavour exists, so this VM is not right-sizeable and
        # must be excluded.
        low_mem_path = os.path.join(tmpdir, "low_mem.csv")
        make_vm_csv(low_mem_path, n_rows=50, mem_cap_kb_fn=lambda i: 1_048_576.0, cores=2)  # 1GB
        result = load_vm(low_mem_path)
        ok = result is None
        load_vm_failures += 0 if ok else 1
        print(
            f"  {'PASS' if ok else 'FAIL'}  VM with memory below the 2GB catalog floor is excluded\n"
            f"        load_vm(...) -> {result!r} (expected None)"
        )

        # Cores below the 1-vCPU catalog floor (0.5 cores, memory fine).
        low_cpu_path = os.path.join(tmpdir, "low_cpu.csv")
        make_vm_csv(low_cpu_path, n_rows=50, mem_cap_kb_fn=lambda i: 8_388_608.0, cores=0.5)
        result = load_vm(low_cpu_path)
        ok = result is None
        load_vm_failures += 0 if ok else 1
        print(
            f"  {'PASS' if ok else 'FAIL'}  VM with cores below the 1-vCPU catalog floor is excluded\n"
            f"        load_vm(...) -> {result!r} (expected None)"
        )

        # Exactly at the floor (2GB, 1 core) is not below it -- must still load.
        at_floor_path = os.path.join(tmpdir, "at_floor.csv")
        make_vm_csv(at_floor_path, n_rows=50, mem_cap_kb_fn=lambda i: 2_097_152.0, cores=1)  # exactly 2GB
        result = load_vm(at_floor_path)
        ok = result is not None
        load_vm_failures += 0 if ok else 1
        print(
            f"  {'PASS' if ok else 'FAIL'}  VM exactly at the catalog floor still loads\n"
            f"        load_vm(...) -> {'(df, meta)' if result else None} (expected not None)"
        )

    total = len(VIOLATION_CASES) + len(SNAP_CASES) + 3 + 6 + 5 + 2 + 3
    failures += split_failures + glitch_failures + shift_failures + load_vm_failures
    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
