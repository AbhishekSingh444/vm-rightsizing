#!/usr/bin/env python3
"""
test_bootstrap.py -- unit tests for the T16 bootstrap.

Run:  python3 test_bootstrap.py

What these tests are actually defending:

  * The seed is real. The same seed gives the same resamples bit for bit; a
    different seed gives different ones. A seed that is accepted but does not
    reach the generator would make "reproducible" vacuous.
  * The VM is the unit: every resample draws exactly n VMs.
  * Wilson and percentile intervals are specific numbers, checked against
    values derived on paper.
  * A resample that draws every VM once reproduces `summarise` exactly, so the
    bootstrap's pooled reclamation and violating fraction are the same
    definitions as the published frontier, not a re-derivation of them.
  * An undefined matched-risk gap is counted, never dropped, and suppresses
    the interval rather than shrinking the sample it is computed over.
  * Ablation shifts are paired: an arm compared against itself shifts by
    exactly zero on every resample.
"""

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from bootstrap import (  # noqa: E402
    DEFAULT_SEED,
    defined_interval,
    gap_samples,
    percentile_interval,
    resample_counts,
    resampled_stats,
    wilson_interval,
)
from sweep_eps import (  # noqa: E402
    interp_at_risk,
    matched_risk_reclamation,
    summarise,
)

TOL = 1e-12
EVAL_HOURS = 10.0


def check(ok, title, detail):
    print(f"  {'PASS' if ok else 'FAIL'}  {title}\n        {detail}")
    return 0 if ok else 1


def per_vm(orig_cpu, alloc_cpu, viol_cpu, idle):
    """A per-VM table in `sweep_eps.evaluate`'s layout. Memory mirrors CPU
    with every number doubled so the two resources are distinguishable."""
    rows = []
    for i, (o, a, v, d) in enumerate(zip(orig_cpu, alloc_cpu, viol_cpu, idle)):
        row = {"vm_id": str(i), "idle": d}
        for res, k in (("cpu", 1.0), ("mem", 2.0)):
            row[f"{res}_alloc"] = a * k
            row[f"{res}_orig"] = o * k
            row[f"{res}_recl_hours"] = (o - a) * k * EVAL_HOURS
            row[f"{res}_events"] = int(v)
            row[f"{res}_exhausted_minutes"] = 5 * int(v)
            row[f"{res}_violated"] = bool(v)
            row[f"{res}_fallback"] = False
            row[f"{res}_capped"] = False
        rows.append(row)
    return pd.DataFrame(rows)


def main():
    failures = 0
    print("test_bootstrap.py")

    # ---- resampling --------------------------------------------------------
    a = resample_counts(50, 200, DEFAULT_SEED)
    b = resample_counts(50, 200, DEFAULT_SEED)
    c = resample_counts(50, 200, DEFAULT_SEED + 1)
    failures += check(
        np.array_equal(a, b) and not np.array_equal(a, c),
        "the same seed reproduces the resamples exactly; another seed does not",
        f"same={np.array_equal(a, b)} different-seed-equal={np.array_equal(a, c)}",
    )
    failures += check(
        a.shape == (200, 50) and (a.sum(axis=1) == 50).all() and (a >= 0).all(),
        "each resample draws exactly n VMs with replacement",
        f"shape={a.shape} row sums={sorted(set(a.sum(axis=1)))}",
    )
    failures += check(
        DEFAULT_SEED == 16,
        "the default seed is the approved 16",
        f"DEFAULT_SEED={DEFAULT_SEED}",
    )

    # ---- intervals ---------------------------------------------------------
    lo, hi = wilson_interval(5, 10)
    failures += check(
        abs(lo - 0.236593) < 1e-6 and abs(hi - 0.763407) < 1e-6,
        "Wilson 95% for 5 of 10 is (0.2366, 0.7634)",
        f"got ({lo:.6f}, {hi:.6f})",
    )
    lo, hi = wilson_interval(0, 50)
    z2 = 1.959963984540054 ** 2
    failures += check(
        lo == 0.0 and abs(hi - z2 / (50 + z2)) < TOL,
        "Wilson 95% for 0 of 50 is (0, z^2/(n+z^2)) = (0, 0.0713)",
        f"got ({lo}, {hi:.6f})",
    )
    lo, hi = percentile_interval(np.arange(101.0))
    failures += check(
        abs(lo - 2.5) < TOL and abs(hi - 97.5) < TOL,
        "95% percentile interval of 0..100 is (2.5, 97.5)",
        f"got ({lo}, {hi})",
    )
    s = np.arange(10.0)
    s[[1, 4, 7]] = np.nan
    und, lo, hi = defined_interval(s)
    und0, lo0, hi0 = defined_interval(np.arange(101.0))
    failures += check(
        abs(und - 0.3) < TOL and np.isnan(lo) and np.isnan(hi)
        and und0 == 0.0 and abs(lo0 - 2.5) < TOL,
        "undefined resamples are counted and withhold the interval; none "
        "undefined gives the plain percentile interval",
        f"3 of 10 NaN -> share={und} interval=({lo}, {hi}); "
        f"none NaN -> ({lo0}, {hi0})",
    )

    # ---- matched-risk reading ----------------------------------------------
    failures += check(
        abs(interp_at_risk([0.06, 0.02], [40.0, 20.0], 0.04) - 30.0) < TOL
        and np.isnan(interp_at_risk([0.02, 0.06], [20.0, 40.0], 0.07)),
        "interp_at_risk: 30 halfway between (0.02, 20) and (0.06, 40), "
        "whatever the input order; NaN outside the swept range",
        "",
    )
    fr = pd.DataFrame([
        {"scope": "all_vms", "resource": "cpu", "method": "evt", "eps": e,
         "viol_frac": x, "recl_pct": y}
        for e, x, y in ((0.01, 0.02, 20.0), (0.1, 0.06, 40.0))
    ] + [{"scope": "all_vms", "resource": "cpu", "method": "peak_capped",
          "eps": np.nan, "viol_frac": 0.05, "recl_pct": 41.0}])
    m = matched_risk_reclamation(fr, "all_vms", "cpu")
    failures += check(
        abs(m["evt_recl_pct"] - 35.0) < TOL and abs(m["gap_pp"] + 6.0) < TOL,
        "matched_risk_reclamation goes through the same interpolation "
        "(35 at risk 0.05, gap -6)",
        f"got {m['evt_recl_pct']} / {m['gap_pp']}",
    )

    # ---- resampled statistics ----------------------------------------------
    idle = np.array([False, False, True])
    tables = [(("peak_capped", np.nan),
               per_vm([4, 8, 2], [2, 4, 1], [0, 1, 0], idle))]
    ones = np.ones((1, 3))
    st = resampled_stats(tables, idle, ones, EVAL_HOURS)
    want = {r["scope"] + r["resource"]: r for r in
            summarise(tables[0][1], "all_vms", "peak_capped", np.nan,
                      EVAL_HOURS)
            + summarise(tables[0][1][~tables[0][1]["idle"]], "non_idle",
                        "peak_capped", np.nan, EVAL_HOURS)}
    ok = all(
        abs(st[(sc, "peak_capped", None, res)][0][0]
            - want[sc + res]["recl_pct"]) < 1e-3
        and abs(st[(sc, "peak_capped", None, res)][1][0]
                - want[sc + res]["viol_frac"]) < 1e-5
        for sc in ("all_vms", "non_idle") for res in ("cpu", "mem"))
    failures += check(
        ok,
        "drawing every VM once reproduces summarise() on both scopes and "
        "both resources",
        f"all-VMs cpu recl={st[('all_vms', 'peak_capped', None, 'cpu')][0][0]:.3f} "
        f"want {want['all_vmscpu']['recl_pct']}",
    )

    counts = np.array([[3.0, 0.0, 0.0], [0.0, 2.0, 1.0]])
    st = resampled_stats(tables, idle, counts, EVAL_HOURS)
    r_all, v_all = st[("all_vms", "peak_capped", None, "cpu")]
    r_ni, v_ni = st[("non_idle", "peak_capped", None, "cpu")]
    # resample 0: VM0 x3 -> recl 3*2 / 3*4 = 50%, no violator.
    # resample 1: VM1 x2 + VM2 -> recl (2*4 + 1) / (2*8 + 2) = 50%,
    #             2 of 3 drawn VMs violate; non-idle keeps VM1 x2 only.
    failures += check(
        np.allclose(r_all, [50.0, 50.0]) and np.allclose(v_all, [0.0, 2 / 3])
        and np.allclose(r_ni, [50.0, 50.0]) and np.allclose(v_ni, [0.0, 1.0]),
        "a resample weights each VM by how often it was drawn, and the "
        "non-idle scope keeps only the drawn non-idle VMs",
        f"all: recl={r_all} viol={v_all}; non-idle: recl={r_ni} viol={v_ni}",
    )

    # ---- gaps: undefined counted, shifts paired ----------------------------
    # Two VMs. M(eps) at two eps; the comparator violates on VM1 only.
    idle2 = np.array([False, False])
    tabs = [
        (("peak_capped", np.nan), per_vm([4, 4], [1, 1], [0, 1], idle2)),
        (("evt", 0.01), per_vm([4, 4], [3, 3], [0, 0], idle2)),
        (("evt", 0.1), per_vm([4, 4], [2, 2], [1, 1], idle2)),
    ]
    # resample 0 draws both VMs: comparator risk 0.5 inside [0, 1] -> defined.
    # resample 1 draws VM0 twice: comparator risk 0, M(eps) range [0, 1]
    #   -> defined at the low end.
    # resample 2 draws VM1 twice: comparator risk 1, inside -> defined.
    counts = np.array([[1.0, 1.0], [2.0, 0.0], [0.0, 2.0]])
    st = resampled_stats(tabs, idle2, counts, EVAL_HOURS)
    g = gap_samples(st, "all_vms", "cpu", [0.01, 0.1], "peak_capped")
    # resample 0: M(eps) at risk 0.5 = midpoint of 25% and 50% = 37.5,
    # comparator reclaims 75% -> gap -37.5.
    failures += check(
        abs(g[0] + 37.5) < TOL and not np.isnan(g).any(),
        "gap on a resample is the interpolated M(eps) reclamation minus the "
        "comparator's, on that resample's own risks",
        f"gaps={g}",
    )
    tabs_out = [
        (("peak_capped", np.nan), per_vm([4, 4], [1, 1], [1, 1], idle2)),
        (("evt", 0.01), per_vm([4, 4], [3, 3], [0, 0], idle2)),
        (("evt", 0.1), per_vm([4, 4], [2, 2], [0, 1], idle2)),
    ]
    # comparator violates on both VMs (risk 1); M(eps) tops out at the share
    # of drawn VM1s, so only the resample of VM1 twice reaches it.
    st = resampled_stats(tabs_out, idle2, counts, EVAL_HOURS)
    g = gap_samples(st, "all_vms", "cpu", [0.01, 0.1], "peak_capped")
    und, lo, hi = defined_interval(g)
    failures += check(
        np.isnan(g[0]) and np.isnan(g[1]) and not np.isnan(g[2])
        and abs(und - 2 / 3) < TOL and np.isnan(lo),
        "a resample whose comparator risk is off the end of the curve is "
        "undefined, counted in undefined_share, and withholds the interval",
        f"gaps={g} undefined_share={und:.4f}",
    )
    st = resampled_stats(tabs, idle2, counts, EVAL_HOURS)
    g1 = gap_samples(st, "all_vms", "cpu", [0.01, 0.1], "peak_capped")
    g2 = gap_samples(st, "all_vms", "cpu", [0.01, 0.1], "peak_capped")
    failures += check(
        np.array_equal(g1 - g2, np.zeros_like(g1)),
        "paired shift: an arm against itself shifts by exactly 0 on every "
        "resample",
        f"max |shift|={np.max(np.abs(g1 - g2))}",
    )

    total = 14
    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
