#!/usr/bin/env python3
"""
test_hypotheses.py -- unit tests for the T15 H2 / H3 statistics.

Run:  python3 test_hypotheses.py

What these tests are actually defending:

  * The thresholds are the pre-registered ones. They are module constants,
    not arguments, and a test pins each value so that moving one shows up as a
    failing test rather than as a quietly different verdict.
  * The binomial interval and ECE are specific numbers, checked against values
    derived on paper rather than against the implementation's own output.
  * Each leg of H2's null fires on its own. A verdict that only ever looked at
    ECE would pass a method whose points all sit outside their intervals.
  * H3 never reports a policy that violated, and B0 is the floor.
"""

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from hypotheses import (  # noqa: E402
    H2_ECE_BOUND,
    H2_EPS,
    H2_LEVEL,
    H2_MAX_POINTS_OUTSIDE,
    H3_THRESHOLD_PCT,
    binomial_interval,
    expected_calibration_error,
    h2_tables,
    h3_table,
)

TOL = 1e-12


def check(ok, title, detail):
    print(f"  {'PASS' if ok else 'FAIL'}  {title}\n        {detail}")
    return 0 if ok else 1


def frontier(evt_counts, n=1000, baselines=None):
    """A minimal frontier: evt rows at the H2 sweep with the given violator
    counts (same for both scopes and resources), plus baseline rows given as
    {method: (recl_pct, vms_with_violation)}. B0 is always present."""
    rows = []
    base = {"as_provisioned": (0.0, 0)}
    base.update(baselines or {})
    for scope in ("all_vms", "non_idle"):
        for res in ("cpu", "mem"):
            for m, (recl, k) in base.items():
                rows.append({"scope": scope, "resource": res, "method": m,
                             "eps": float("nan"), "vms": n, "recl_pct": recl,
                             "vms_with_violation": k, "events": k})
            for e, k in zip(H2_EPS, evt_counts):
                rows.append({"scope": scope, "resource": res, "method": "evt",
                             "eps": e, "vms": n, "recl_pct": 10.0 + 100 * e,
                             "vms_with_violation": k, "events": 2 * k})
    return pd.DataFrame(rows)


def main():
    failures = 0

    print("pre-registered constants (docs/HYPOTHESES.txt, section 3):")
    failures += check(
        tuple(H2_EPS) == (0.001, 0.005, 0.01, 0.05, 0.10)
        and H2_ECE_BOUND == 0.05 and H2_LEVEL == 0.95
        and H2_MAX_POINTS_OUTSIDE == 1,
        "H2 sweep, ECE bound, interval level and null count are unchanged",
        f"eps={H2_EPS} ece<{H2_ECE_BOUND} level={H2_LEVEL} "
        f"null at >= {H2_MAX_POINTS_OUTSIDE + 1} points outside",
    )
    failures += check(
        H3_THRESHOLD_PCT == {"cpu": 25.0, "mem": 20.0},
        "H3 thresholds are 25% vCPU-hours and 20% GB-hours",
        f"got {H3_THRESHOLD_PCT}",
    )

    print("\nbinomial_interval against hand-computed values:")
    # Binomial(792, 0.001) ~ Poisson(0.792): P(X<=2) = 0.954 < 0.975 and
    # P(X<=3) = 0.991, so the upper end is 3; P(X=0) = 0.453 > 0.025, so the
    # lower end is 0.
    got = binomial_interval(792, 0.001)
    failures += check(got == (0, 3),
                      "Binomial(792, 0.001) central 95% region is [0, 3]",
                      f"got {got}")
    # Binomial(100, 0.5): the symmetric 95% region is [40, 60].
    got = binomial_interval(100, 0.5)
    failures += check(got == (40, 60),
                      "Binomial(100, 0.5) central 95% region is [40, 60]",
                      f"got {got}")

    print("\nexpected_calibration_error:")
    got = expected_calibration_error([0.1, 0.2], [0.15, 0.1])
    failures += check(abs(got - 0.075) < TOL,
                      "mean |observed - eps| = (0.05 + 0.10) / 2",
                      f"got {got!r} (expected 0.075)")
    got = expected_calibration_error(H2_EPS, H2_EPS)
    failures += check(abs(got) < TOL, "a perfectly calibrated sweep has ECE 0",
                      f"got {got!r}")

    print("\nh2_tables verdicts:")
    n = 1000
    exact = [round(e * n) for e in H2_EPS]           # 1, 5, 10, 50, 100
    _c, s = h2_tables(frontier(exact, n), "t")
    failures += check(
        (s.verdict == "SUPPORTED").all() and (s.points_outside == 0).all(),
        "observed == eps at every point -> SUPPORTED, nothing outside",
        f"verdicts={sorted(set(s.verdict))} outside={sorted(set(s.points_outside))}",
    )
    # Two low points far above their intervals; ECE stays tiny (~0.017), so
    # only the interval leg can reject.
    two_out = [30, 30] + exact[2:]
    _c, s = h2_tables(frontier(two_out, n), "t")
    failures += check(
        (s.verdict == "NOT SUPPORTED").all() and (s.points_outside == 2).all()
        and (s.ece < H2_ECE_BOUND).all(),
        "two points outside rejects even when ECE is below the bound",
        f"outside={s.points_outside.iloc[0]} ece={s.ece.iloc[0]}",
    )
    one_out = [30] + exact[1:]
    _c, s = h2_tables(frontier(one_out, n), "t")
    failures += check(
        (s.verdict == "SUPPORTED").all() and (s.points_outside == 1).all(),
        "one point outside alone does not reject",
        f"outside={s.points_outside.iloc[0]} verdict={s.verdict.iloc[0]}",
    )
    # Every point sits inside its interval only if the interval is wide; here
    # observed is 0.3 everywhere, which is both outside and ECE-violating.
    _c, s = h2_tables(frontier([300] * 5, n), "t")
    failures += check(
        (s.verdict == "NOT SUPPORTED").all() and (s.ece >= H2_ECE_BOUND).all(),
        "ECE at or above the bound rejects",
        f"ece={s.ece.iloc[0]} why={s.why.iloc[0]!r}",
    )
    fr = frontier(exact, n)
    fr = fr[~((fr.method == "evt") & (fr.eps == 0.05))]
    try:
        h2_tables(fr, "t")
        raised = False
    except SystemExit:
        raised = True
    failures += check(raised, "a missing pre-registered eps point is an error",
                      "not silently skipped")

    print("\nh3_table:")
    h = h3_table(frontier([5, 6, 7, 8, 9], n,
                          {"peak": (30.0, 0), "p95": (60.0, 40)}), "t")
    cpu = h[(h.scope == "all_vms") & (h.resource == "cpu")].iloc[0]
    failures += check(
        cpu.zero_risk_policy == "peak" and cpu.zero_risk_recl_pct == 30.0
        and cpu.verdict == "SUPPORTED",
        "the largest zero-violation reclamation is chosen, never a violator",
        f"policy={cpu.zero_risk_policy} recl={cpu.zero_risk_recl_pct} "
        f"(p95 reclaims 60 but violates)",
    )
    h = h3_table(frontier([5, 6, 7, 8, 9], n,
                          {"peak": (19.0, 0), "p95": (60.0, 40)}), "t")
    failures += check(
        (h.verdict == "NOT SUPPORTED").all()
        and (h.zero_risk_recl_pct == 19.0).all(),
        "19% at zero risk fails both the 25% CPU and the 20% memory bar",
        f"verdicts={sorted(set(h.verdict))}",
    )
    h = h3_table(frontier([5, 6, 7, 8, 9], n, {"peak": (40.0, 3)}), "t")
    row = h.iloc[0]
    failures += check(
        row.zero_risk_policy == "as_provisioned"
        and row.zero_risk_recl_pct == 0.0 and row.verdict == "NOT SUPPORTED"
        and row.nearest_miss_policy == "peak"
        and row.nearest_miss_vms_violating == 3,
        "with only B0 at zero risk the answer is 0%, and the nearest miss is "
        "the fewest-violator policy",
        f"zero={row.zero_risk_policy}@{row.zero_risk_recl_pct} "
        f"near={row.nearest_miss_policy} ({row.nearest_miss_vms_violating} VMs)",
    )

    total = 13
    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
