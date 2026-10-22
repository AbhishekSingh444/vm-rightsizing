#!/usr/bin/env python3
"""
test_multisplit.py -- unit tests for the T17 multi-split windows.

Run:  python3 test_multisplit.py

What these tests are actually defending:

  * Origin 0 is the published split, sample for sample. If it were not, the
    headline row of the sensitivity table would be a different number from
    the headline it sits beside.
  * A non-zero origin moves the observation START and nothing else: the
    observation window is [t0 + origin, t0 + origin + obs), evaluation is the
    eval_days after it, and the end-of-trace tolerance stays anchored at the
    VM's true first sample, so a 30-day trace still covers origin 7 + 14 + 9.
  * Idle membership is recomputed from each split's own observation window
    (invariant 4) -- a VM busy only before the origin is idle after it.
  * One parse feeds several splits without one split's meta leaking into
    another's.
  * The family summary counts undefined values out instead of imputing them,
    and its sd is the sample sd.
"""

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from baselines import split_obs_eval  # noqa: E402
from multisplit import summarise_family  # noqa: E402
from sweep_eps import build_fleet, split_from_origin  # noqa: E402

DAY = 86400.0
GAP = 300.0
T0 = 1_000.0


def trace(days=30.0, busy_until_day=None):
    """One VM at 5-minute samples, last sample one gap short of `days`."""
    ts = T0 + np.arange(0.0, days * DAY, GAP)
    cpu = np.full(len(ts), 0.01)
    if busy_until_day is not None:
        cpu[ts < T0 + busy_until_day * DAY] = 3.0
    rng = np.random.default_rng(0)
    df = pd.DataFrame({"ts": ts, "cpu_cores": cpu,
                       "mem_gb": 1.0 + rng.random(len(ts))})
    meta = {"vm_id": "1", "orig_cores": 4.0, "orig_mem_gb": 8.0,
            "n_samples": len(df)}
    return df, meta


def check(ok, title, detail):
    print(f"  {'PASS' if ok else 'FAIL'}  {title}\n        {detail}")
    return 0 if ok else 1


def main():
    failures = 0
    df, meta = trace()

    o0, e0 = split_from_origin(df, 0.0, 21.0, 9.0)
    p0, q0 = split_obs_eval(df, 21.0, 9.0)
    failures += check(
        o0.equals(p0) and e0.equals(q0),
        "origin 0 is the published split, sample for sample",
        f"obs {len(o0)}/{len(p0)} eval {len(e0)}/{len(q0)}",
    )

    obs, ev = split_from_origin(df, 7.0, 14.0, 9.0)
    failures += check(
        obs.ts.min() == T0 + 7 * DAY and obs.ts.max() == T0 + 21 * DAY - GAP
        and len(obs) == int(14 * DAY / GAP),
        "origin 7, obs 14: observation is exactly [t0+7d, t0+21d)",
        f"first {(obs.ts.min() - T0) / DAY:.4f} d, last "
        f"{(obs.ts.max() - T0) / DAY:.4f} d, n={len(obs)}",
    )
    failures += check(
        ev.equals(q0),
        "origin 7, obs 14 has the headline's evaluation window (days 21-30)",
        f"eval n={len(ev)} first {(ev.ts.min() - T0) / DAY:.4f} d",
    )

    obs, ev = split_from_origin(df, 7.0 / 3.0, 14.0, 9.0)
    failures += check(
        abs(obs.ts.min() - (T0 + 7 / 3 * DAY)) < GAP
        and obs.ts.min() >= T0 + 7 / 3 * DAY
        and abs(ev.ts.min() - (T0 + (7 / 3 + 14) * DAY)) < GAP
        and abs(ev.ts.max() - (T0 + (7 / 3 + 23) * DAY)) <= GAP,
        "a fractional origin (7/3 d) places both windows to within one sample",
        f"obs from {(obs.ts.min() - T0) / DAY:.4f} d, eval "
        f"{(ev.ts.min() - T0) / DAY:.4f}-{(ev.ts.max() - T0) / DAY:.4f} d",
    )

    short, _m = trace(days=29.0)
    failures += check(
        split_from_origin(short, 7.0, 14.0, 9.0) is None
        and split_from_origin(df, 14.0, 7.0, 9.0) is not None,
        "the span check is on origin + obs + eval: 29 days fails 7+14+9, "
        "30 days passes 14+7+9",
        "",
    )

    busy, bmeta = trace(busy_until_day=5.0)
    vms = [(busy, bmeta)]
    f0, _s = build_fleet(vms, 14.0, 9.0, origin_days=0.0)
    f7, _s = build_fleet(vms, 14.0, 9.0, origin_days=7.0)
    failures += check(
        (not f0[0][2]["idle"]) and f7[0][2]["idle"],
        "idle is recomputed per split: busy only in days 0-5 is non-idle at "
        "origin 0 and idle at origin 7",
        f"origin 0 idle={f0[0][2]['idle']}  origin 7 idle={f7[0][2]['idle']}",
    )
    failures += check(
        "idle" not in bmeta and f0[0][2] is not f7[0][2],
        "meta is copied per split; the parsed meta is never written to",
        f"parsed meta keys: {sorted(bmeta)}",
    )

    n, mean, sd = summarise_family([1.0, 2.0, np.nan, 4.0])
    failures += check(
        n == 3 and abs(mean - 7 / 3) < 1e-12
        and abs(sd - np.std([1.0, 2.0, 4.0], ddof=1)) < 1e-12,
        "family summary drops undefined values and uses the sample sd",
        f"n={n} mean={mean:.6f} sd={sd:.6f}",
    )
    n, mean, sd = summarise_family([np.nan, 5.0])
    failures += check(
        n == 1 and mean == 5.0 and np.isnan(sd),
        "one defined value gives a mean and no sd",
        f"n={n} mean={mean} sd={sd}",
    )

    total = 9
    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
