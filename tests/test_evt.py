#!/usr/bin/env python3
"""
test_evt.py -- unit tests for the proposed recommender M(eps).

Run:  python3 test_evt.py

A new file rather than additions to test_baselines.py, which encodes Review 1
decisions and is left alone (CLAUDE.md invariant 3).

What these tests are actually defending:

  * The tail probability is a specific number, not "something small". The
    exponential special case of the GPD (shape = 0) is hand-computable, so
    the arithmetic in tail_exceedance_prob can be checked against a value
    derived on paper rather than against the implementation's own output.
  * Monotonicity. The whole flavour search assumes P(c) falls as c rises and
    that a stricter eps never buys a smaller machine. If either breaks, the
    frontier stops being a frontier and the sweep in T10 is meaningless.
  * The fallback fires. A tail model that silently fits garbage on a flat
    series would quietly become the headline result.
"""

import math
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from baselines import (  # noqa: E402
    EVT_DEFAULT_RUN_LENGTH,
    EVT_MIN_EXCEEDANCES,
    FLAVOUR_MEM_GB,
    FLAVOUR_VCPU,
    estimate_theta,
    estimate_theta_intervals,
    estimate_theta_runs,
    evt_allocation,
    evt_required_capacity,
    exceedance_indices,
    fit_gpd_tail,
    horizon_violation_prob,
    recommend,
    recommend_evt,
    tail_exceedance_prob,
)

TOL = 1e-9


def check(ok, title, detail):
    print(f"  {'PASS' if ok else 'FAIL'}  {title}\n        {detail}")
    return 0 if ok else 1


def exponential_fit(u=10.0, scale=2.0, rate=0.05, n_obs=1000):
    """A hand-checkable fit: GPD with shape = 0 is Exponential(scale), whose
    survival function is exp(-x / scale). No scipy needed to predict it.
    """
    return {
        "u": u,
        "shape": 0.0,
        "scale": scale,
        "n_exceed": int(round(rate * n_obs)),
        "n_obs": n_obs,
        "rate": rate,
        "sorted": np.linspace(0.0, u, n_obs),
    }


def main():
    failures = 0

    # --- 1. tail probability against a hand-computed value ------------------
    print("tail_exceedance_prob against hand-computed values:")
    fit = exponential_fit(u=10.0, scale=2.0, rate=0.05)

    # p(16) = rate * exp(-(16 - 10) / 2) = 0.05 * exp(-3)
    expected = 0.05 * math.exp(-3.0)
    got = tail_exceedance_prob(16.0, fit)
    failures += check(
        abs(got - expected) < 1e-12,
        "p(c) = rate * exp(-(c-u)/scale) at c = 16, u = 10, scale = 2",
        f"p={got!r} (expected {expected!r} = 0.05*exp(-3))",
    )

    # At c = u exactly the GPD contributes sf(0) = 1, so p(u) = rate.
    got = tail_exceedance_prob(10.0, fit)
    failures += check(
        abs(got - 0.05) < 1e-12,
        "p(u) equals the exceedance rate exactly",
        f"p={got!r} (expected 0.05)",
    )

    # Below u the empirical branch takes over. `sorted` is 1000 points evenly
    # spanning [0, 10], so the fraction strictly above 5.0 is very near 0.5.
    got = tail_exceedance_prob(5.0, fit)
    failures += check(
        abs(got - 0.5) < 0.01,
        "below u, p(c) is the empirical exceedance fraction",
        f"p(5.0)={got!r} (expected ~0.5 for values uniform on [0,10])",
    )

    # --- 2. the horizon formula ---------------------------------------------
    print("\nhorizon_violation_prob:")
    # 1 - (1 - 0.1)^2 = 1 - 0.81 = 0.19, exactly.
    got = horizon_violation_prob(0.1, 2, theta=1.0)
    failures += check(
        abs(got - 0.19) < 1e-12,
        "P = 1 - (1-p)^(theta*n) with p=0.1, n=2, theta=1",
        f"P={got!r} (expected 0.19)",
    )
    # theta halves the effective number of trials: theta=0.5, n=4 -> 2 trials.
    got = horizon_violation_prob(0.1, 4, theta=0.5)
    failures += check(
        abs(got - 0.19) < 1e-12,
        "theta scales the trial count (theta=0.5, n=4 matches theta=1, n=2)",
        f"P={got!r} (expected 0.19)",
    )

    # --- 3. P(c) decreases as c increases -----------------------------------
    print("\nP(c) is non-increasing in c:")
    grid = np.linspace(0.0, 40.0, 400)
    probs = [horizon_violation_prob(tail_exceedance_prob(c, fit), 2592) for c in grid]
    diffs = np.diff(probs)
    worst = float(diffs.max())
    failures += check(
        worst <= TOL,
        "P(c) never rises across c in [0, 40] (400 points, n_eval=2592)",
        f"largest step up = {worst:.3e} (expected <= {TOL:g})",
    )
    failures += check(
        probs[0] > probs[-1],
        "P(c) is strictly lower at the top of the range than the bottom",
        f"P(0)={probs[0]:.6f} > P(40)={probs[-1]:.3e}",
    )

    # --- 4. flavour never shrinks as eps shrinks ----------------------------
    print("\nallocation is non-increasing in eps (stricter eps never buys less):")
    rng = np.random.default_rng(20260922)
    # A spiky-but-not-degenerate series: lognormal body with a real tail.
    demand = rng.lognormal(mean=0.0, sigma=0.7, size=4000)
    eps_sweep = [0.6, 0.4, 0.2, 0.1, 0.05, 0.01, 0.005, 0.001]

    # Each series is scaled so the required capacities land INSIDE its
    # catalog rather than clamping at the top entry -- a sweep that returns
    # the largest flavour throughout is trivially monotone and tests nothing.
    for catalog, name, scale in (
        (FLAVOUR_VCPU, "vCPU", 0.2),
        (FLAVOUR_MEM_GB, "memory", 1.0),
    ):
        allocs = [
            evt_allocation(demand * scale, catalog, eps, 2592)[0] for eps in eps_sweep
        ]
        ok = all(allocs[i] <= allocs[i + 1] for i in range(len(allocs) - 1))
        failures += check(
            ok and len(set(allocs)) > 1,
            f"{name} flavour never shrinks as eps shrinks (and the grid moves)",
            f"eps {eps_sweep} -> {allocs}",
        )

    # The same must hold for the unsnapped continuous capacity, or the
    # snapped result is only monotone by accident of the grid.
    raw = [evt_allocation(demand, FLAVOUR_VCPU, eps, 2592, snap=False)[0]
           for eps in eps_sweep]
    ok = all(raw[i] <= raw[i + 1] + TOL for i in range(len(raw) - 1))
    failures += check(
        ok,
        "unsnapped continuous capacity is also non-increasing in eps",
        f"{[round(x, 4) for x in raw]}",
    )

    # --- 5. the inversion agrees with the forward computation ---------------
    print("\nevt_required_capacity inverts horizon_violation_prob:")
    fitted, reason = fit_gpd_tail(demand)
    failures += check(
        fitted is not None,
        "a 4000-sample lognormal series yields a usable tail fit",
        f"reason={reason!r}, n_exceed={None if not fitted else fitted['n_exceed']}",
    )
    if fitted is not None:
        for eps in (0.01, 0.1):
            c = evt_required_capacity(fitted, eps, 2592)
            achieved = horizon_violation_prob(tail_exceedance_prob(c, fitted), 2592)
            failures += check(
                achieved <= eps + 1e-9,
                f"capacity chosen for eps={eps} actually achieves P(c) <= eps",
                f"c={c:.6f} -> P(c)={achieved:.6g} (target {eps})",
            )

    # --- 6. fallback to peak ------------------------------------------------
    print("\nfallback to peak:")
    # A flat series has no exceedances above its own 95th percentile.
    flat = np.full(500, 3.0)
    alloc, diag = evt_allocation(flat, FLAVOUR_VCPU, 0.01, 2592)
    failures += check(
        diag["fallback"] == "few_exceedances" and alloc == 4,
        "flat series falls back to peak and snaps up (peak 3.0 -> flavour 4)",
        f"fallback={diag['fallback']!r}, alloc={alloc}",
    )

    # Just under the minimum exceedance count must still fall back.
    body = np.full(1000, 1.0)
    thin = np.concatenate([body, np.full(EVT_MIN_EXCEEDANCES - 1, 9.0)])
    _alloc, diag = evt_allocation(thin, FLAVOUR_VCPU, 0.01, 2592)
    failures += check(
        diag["fallback"] == "few_exceedances",
        f"{EVT_MIN_EXCEEDANCES - 1} exceedances is below the minimum and falls back",
        f"fallback={diag['fallback']!r}",
    )

    # An empty series cannot be fitted at all.
    _alloc, diag = evt_allocation(np.array([]), FLAVOUR_VCPU, 0.01, 2592)
    failures += check(
        diag["fallback"] == "no_samples",
        "empty series falls back rather than raising",
        f"fallback={diag['fallback']!r}",
    )

    # eps = 0 demands certainty, which no finite capacity provides.
    _alloc, diag = evt_allocation(demand, FLAVOUR_VCPU, 0.0, 2592)
    failures += check(
        diag["fallback"] == "capacity_unbounded",
        "eps = 0 is unsatisfiable and falls back to peak",
        f"fallback={diag['fallback']!r}",
    )

    # --- 6b. cap at the original allocation ---------------------------------
    # load_vm clips demand to the original allocation, so demand has bounded
    # support there and P(demand > c) = 0 for any c at or above it. An
    # uncapped GPD extrapolation predicts mass the data model forbids; the
    # cap is the model's support, not a tuning choice.
    print("\ncap at the original allocation:")

    # A heavy tail that extrapolates far past anything observed.
    heavy = rng.pareto(1.2, size=4000) + 1.0
    uncapped, d_un = evt_allocation(heavy, FLAVOUR_MEM_GB, 0.001, 2592)
    capped, d_cap = evt_allocation(heavy, FLAVOUR_MEM_GB, 0.001, 2592, cap=8.0)
    failures += check(
        uncapped > 8.0 and capped == 8.0 and d_cap["capped"] and not d_un["capped"],
        "a runaway extrapolation is clamped to the cap and flagged",
        f"uncapped={uncapped} (capped={d_un['capped']}), "
        f"capped={capped} (capped={d_cap['capped']})",
    )

    # A cap above the requirement must not disturb the answer.
    loose, d_loose = evt_allocation(heavy, FLAVOUR_MEM_GB, 0.001, 2592, cap=1024.0)
    failures += check(
        loose == uncapped and not d_loose["capped"],
        "a cap above the requirement changes nothing and is not flagged",
        f"cap=1024 -> {loose} (uncapped {uncapped}), capped={d_loose['capped']}",
    )

    # The cap also binds on the peak fallback: snap_up can push peak above the
    # original allocation even though demand never exceeded it.
    flat_high = np.full(500, 15.9)
    alloc, diag = evt_allocation(flat_high, FLAVOUR_MEM_GB, 0.01, 2592, cap=15.9)
    failures += check(
        diag["fallback"] == "few_exceedances" and alloc == 15.9 and diag["capped"],
        "cap binds on the peak fallback too (snap_up 15.9 -> 16 would exceed it)",
        f"fallback={diag['fallback']!r}, alloc={alloc}, capped={diag['capped']}",
    )

    # Capping can never make reclamation negative: allocation <= original.
    caps_ok = True
    for eps in eps_sweep:
        a, _ = evt_allocation(heavy, FLAVOUR_MEM_GB, eps, 2592, cap=8.0)
        caps_ok = caps_ok and a <= 8.0
    failures += check(
        caps_ok,
        "capped allocation never exceeds the original across the whole eps sweep",
        f"all eps in {eps_sweep} gave allocation <= 8.0",
    )

    # Monotonicity in eps must survive capping. The cap is chosen so the
    # sweep still moves across the grid before it binds -- a sweep pinned at
    # the cap throughout would be trivially monotone and test nothing.
    capped_sweep = [
        evt_allocation(demand, FLAVOUR_MEM_GB, eps, 2592, cap=32.0)[0]
        for eps in eps_sweep
    ]
    failures += check(
        all(capped_sweep[i] <= capped_sweep[i + 1] for i in range(len(capped_sweep) - 1))
        and len(set(capped_sweep)) > 1
        and max(capped_sweep) == 32.0,
        "capped allocation is still non-increasing in eps (and the grid moves)",
        f"{capped_sweep}",
    )

    # recommend_evt caps each resource against its own original allocation.
    obs_heavy = pd.DataFrame({
        "cpu_cores": rng.pareto(1.2, 4000) + 1.0,
        "mem_gb": rng.pareto(1.2, 4000) + 1.0,
    })
    a, diag = recommend_evt(obs_heavy, 0.001, 2592, cap_cores=2.0, cap_mem_gb=64.0)
    failures += check(
        a[0] <= 2.0 and diag["cpu"]["capped"],
        "recommend_evt applies the per-resource cap independently",
        f"cpu={a[0]} (cap 2.0, capped={diag['cpu']['capped']}), mem={a[1]} (cap 64.0)",
    )

    # --- 7. fallback is per resource ----------------------------------------
    print("\nfallback is independent per resource:")
    obs = pd.DataFrame({
        "cpu_cores": np.full(4000, 2.0),          # flat -> must fall back
        "mem_gb": rng.lognormal(0.0, 0.7, 4000),  # spiky -> must fit
    })
    _alloc, diag = recommend_evt(obs, 0.01, 2592)
    failures += check(
        diag["cpu"]["fallback"] == "few_exceedances" and diag["mem"]["fallback"] is None,
        "a VM can fall back on CPU while taking a fitted memory allocation",
        f"cpu={diag['cpu']['fallback']!r}, mem={diag['mem']['fallback']!r}",
    )

    # --- 8. recommend() exposes evt as a fourth policy ----------------------
    print("\nrecommend() policy wiring:")
    via_recommend = recommend(obs, "evt", eps=0.01, n_eval=2592)
    via_direct, _ = recommend_evt(obs, 0.01, 2592)
    failures += check(
        via_recommend == via_direct,
        "recommend(obs, 'evt', ...) matches recommend_evt(...)",
        f"{via_recommend} == {via_direct}",
    )
    failures += check(
        all(a in FLAVOUR_VCPU for a in [via_recommend[0]])
        and via_recommend[1] in FLAVOUR_MEM_GB,
        "evt allocations are drawn from the flavour catalog",
        f"cpu={via_recommend[0]} in {FLAVOUR_VCPU}, mem={via_recommend[1]} in {FLAVOUR_MEM_GB}",
    )

    # theta must be switchable without restructuring (T11 depends on this).
    strict = recommend(obs, "evt", eps=0.01, n_eval=2592, theta=1.0)
    loose = recommend(obs, "evt", eps=0.01, n_eval=2592, theta=0.25)
    failures += check(
        loose[1] <= strict[1],
        "lowering theta (fewer effective trials) never raises the allocation",
        f"theta=1.0 -> mem {strict[1]}, theta=0.25 -> mem {loose[1]}",
    )

    # --- 9. T11: extremal index estimators ----------------------------------
    # theta is the only thing standing between "2,592 samples" and "2,592
    # independent chances to exceed". Both estimators are checked against
    # values derived on paper, not against their own output.
    print("\nextremal index (T11):")

    def series_with_exceedances(positions, n, u_val=10.0, base=0.0):
        """A flat series at `base` with `u_val` at the named positions. With
        a threshold of 1.0 the exceedance set is exactly `positions`."""
        v = np.full(n, base)
        v[list(positions)] = u_val
        return v

    # Exceedance positions must come back exactly, in order.
    v = series_with_exceedances([3, 7, 8], 20)
    got = exceedance_indices(v, 1.0)
    failures += check(
        list(got) == [3, 7, 8],
        "exceedance_indices returns the positions strictly above u",
        f"{list(got)} (expected [3, 7, 8])",
    )

    # Evenly spaced exceedances are as independent as exceedances get. Every
    # interexceedance time is 2, so the short-gap branch applies and gives
    # 2*(2m)^2 / (m * 4m) = 2, clipped to 1.
    even = series_with_exceedances(range(0, 200, 2), 200)
    th, why = estimate_theta_intervals(even, 1.0)
    failures += check(
        th == 1.0 and why is None,
        "intervals: evenly spaced exceedances give theta = 1 (clipped)",
        f"theta={th!r}, reason={why!r}",
    )

    # Two tight clusters of 5, 996 samples apart. Hand-computed with the
    # bias-corrected branch (max gap > 2):
    #   sum(T-1) = 995, so numerator = 2 * 995^2      = 1,980,050
    #   sum (T-1)(T-2) = 995*994 = 989,030, N-1 = 9   = 8,901,270
    #   theta = 1,980,050 / 8,901,270                 = 0.2224468...
    clustered = series_with_exceedances(
        [0, 1, 2, 3, 4, 1000, 1001, 1002, 1003, 1004], 1010
    )
    expected = 2.0 * 995.0 ** 2 / (9.0 * 995.0 * 994.0)
    th, why = estimate_theta_intervals(clustered, 1.0)
    failures += check(
        th is not None and abs(th - expected) < 1e-12,
        "intervals: two clusters of 5 give the hand-computed Ferro-Segers value",
        f"theta={th!r} (expected {expected!r}), reason={why!r}",
    )
    failures += check(
        th < 0.25,
        "intervals: clustered exceedances give theta well below 1",
        f"theta={th:.6f} vs the true cluster ratio 2/10 = 0.2",
    )

    # Runs declustering on the same series: one gap exceeds r = 12, so there
    # are 2 clusters over 10 exceedances -- exactly 0.2, by construction.
    th, why = estimate_theta_runs(clustered, 1.0, run_length=12)
    failures += check(
        th == 0.2 and why is None,
        "runs (r=12): 2 clusters / 10 exceedances = 0.2 exactly",
        f"theta={th!r}, reason={why!r}",
    )

    # A run length longer than the gap merges the clusters into one.
    th, _why = estimate_theta_runs(clustered, 1.0, run_length=2000)
    failures += check(
        th == 0.1,
        "runs: a run length above every gap collapses to a single cluster",
        f"theta={th!r} (expected 1/10)",
    )
    failures += check(
        EVT_DEFAULT_RUN_LENGTH == 12,
        "the runs run length is fixed at 12 samples (one hour at 5-min sampling)",
        f"EVT_DEFAULT_RUN_LENGTH={EVT_DEFAULT_RUN_LENGTH}",
    )

    # Fewer than two exceedances gives no interexceedance time at all.
    lonely = series_with_exceedances([5], 100)
    th, why = estimate_theta_intervals(lonely, 1.0)
    failures += check(
        th is None and why == "too_few_exceedances",
        "intervals: a single exceedance cannot be estimated from",
        f"theta={th!r}, reason={why!r}",
    )

    # Dispatch, including the 'fixed' arm that declines to estimate.
    th, why = estimate_theta(clustered, 1.0, method="fixed")
    failures += check(
        th is None and why == "fixed",
        "estimate_theta('fixed') returns no estimate, leaving theta to the caller",
        f"theta={th!r}, reason={why!r}",
    )
    failures += check(
        estimate_theta(clustered, 1.0, method="runs", run_length=12)[0] == 0.2
        and estimate_theta(clustered, 1.0, method="intervals")[0] == expected,
        "estimate_theta dispatches to each named estimator",
        "runs -> 0.2, intervals -> hand-computed value",
    )

    # The point of all of it: a lower theta must buy a smaller machine.
    heavy_obs = pd.DataFrame({
        "cpu_cores": rng.lognormal(0.0, 0.7, 4000),
        "mem_gb": rng.lognormal(0.0, 0.7, 4000),
    })
    big = recommend(heavy_obs, "evt", eps=0.01, n_eval=2592, theta=1.0)
    small = recommend(heavy_obs, "evt", eps=0.01, n_eval=2592, theta=0.05)
    failures += check(
        small[0] <= big[0] and small[1] <= big[1],
        "an estimated theta below 1 never raises the allocation on either resource",
        f"theta=1 -> {big}, theta=0.05 -> {small}",
    )

    # --- 10. T12: the matched-risk reading, and the threshold parameter -----
    # `matched_risk_reclamation` produces the primary T12 number, so it is
    # checked against a frontier whose answer can be read off by hand rather
    # than against its own output.
    print("\nmatched-risk reading (T12):")

    from sweep_eps import matched_risk_reclamation  # noqa: E402

    # M(eps) runs from (risk 0.02, recl 20) to (risk 0.06, recl 40).
    # peak_capped sits at risk 0.04 -- exactly halfway -- so the interpolated
    # M(eps) reclamation is 30, and the gap against its 35 is -5.
    synth = pd.DataFrame([
        {"scope": "all_vms", "resource": "mem", "method": "evt",
         "eps": 0.01, "viol_frac": 0.02, "recl_pct": 20.0},
        {"scope": "all_vms", "resource": "mem", "method": "evt",
         "eps": 0.10, "viol_frac": 0.06, "recl_pct": 40.0},
        {"scope": "all_vms", "resource": "mem", "method": "peak_capped",
         "eps": float("nan"), "viol_frac": 0.04, "recl_pct": 35.0},
        {"scope": "all_vms", "resource": "mem", "method": "peak",
         "eps": float("nan"), "viol_frac": 0.06, "recl_pct": 50.0},
        # A baseline beyond the swept range -- nothing to interpolate between.
        {"scope": "non_idle", "resource": "mem", "method": "evt",
         "eps": 0.01, "viol_frac": 0.02, "recl_pct": 20.0},
        {"scope": "non_idle", "resource": "mem", "method": "evt",
         "eps": 0.10, "viol_frac": 0.06, "recl_pct": 40.0},
        {"scope": "non_idle", "resource": "mem", "method": "peak_capped",
         "eps": float("nan"), "viol_frac": 0.30, "recl_pct": 60.0},
    ])

    got = matched_risk_reclamation(synth, "all_vms", "mem", "peak_capped")
    failures += check(
        abs(got["evt_recl_pct"] - 30.0) < 1e-9,
        "matched risk halfway between two sweep points interpolates halfway",
        f"evt_recl_pct={got['evt_recl_pct']!r} (expected 30.0)",
    )
    failures += check(
        abs(got["gap_pp"] - (-5.0)) < 1e-9,
        "gap_pp is M(eps) reclamation minus the baseline's, at matched risk",
        f"gap_pp={got['gap_pp']!r} (expected -5.0 = 30 - 35)",
    )

    out = matched_risk_reclamation(synth, "non_idle", "mem", "peak_capped")
    failures += check(
        np.isnan(out["gap_pp"]) and "outside the swept" in out["note"],
        "a baseline beyond the swept range gives NaN, not an extrapolation",
        f"gap_pp={out['gap_pp']!r}, note={out['note']!r}",
    )

    alt = matched_risk_reclamation(synth, "all_vms", "mem", "peak")
    failures += check(
        alt["baseline"] == "peak" and abs(alt["gap_pp"] - (-10.0)) < 1e-9,
        "the comparator is selectable (the cap_off arm needs uncapped peak)",
        f"baseline={alt['baseline']!r}, gap_pp={alt['gap_pp']!r} "
        "(expected -10.0 = 40 - 50)",
    )

    # The threshold quantile is a T12 ablation arm, so it has to actually
    # reach the fit. It was silently ignored by sweep_eps.load_fleet until
    # T12; this pins the parameter that bug hid behind.
    vals = np.arange(1000, dtype=float)
    f90, _r = fit_gpd_tail(vals, threshold_q=0.90)
    f98, _r = fit_gpd_tail(vals, threshold_q=0.98)
    failures += check(
        f90 is not None and f98 is not None
        and abs(f90["u"] - float(np.quantile(vals, 0.90))) < 1e-9
        and f98["u"] > f90["u"] and f98["n_exceed"] < f90["n_exceed"],
        "threshold_q sets u to that empirical quantile and thins the tail",
        f"u(q0.90)={f90['u']:.1f} n={f90['n_exceed']}, "
        f"u(q0.98)={f98['u']:.1f} n={f98['n_exceed']}",
    )

    total = 41
    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
