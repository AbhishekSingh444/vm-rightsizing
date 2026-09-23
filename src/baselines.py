#!/usr/bin/env python3
"""
baselines.py -- reproduce the baselines named in the problem statement.

Computes three allocation policies from an observation window, replays them
against a held-out evaluation window, and reports reclaimed capacity alongside
the violations that reclamation caused.

    B0  as-provisioned  the trace's original allocation (reference)
    B1  peak            max demand over the observation window
    B2  p95             95th percentile of demand over the observation window

All three snap to a fixed flavour catalog. Recommenders see the observation
window only.

Usage:
    python3 baselines.py --traces ./traces --out ./results
    python3 baselines.py --traces ./traces --out ./results --obs-days 21 --eval-days 9
"""

import argparse
import glob
import json
import os
import sys

import numpy as np
import pandas as pd
from scipy.stats import genpareto

# --- Bitbrains fastStorage columns ------------------------------------------
COL_TS = "Timestamp [ms]"
COL_CORES = "CPU cores"
COL_CAP_MHZ = "CPU capacity provisioned [MHZ]"
COL_USE_MHZ = "CPU usage [MHZ]"
COL_MEM_CAP_KB = "Memory capacity provisioned [KB]"
COL_MEM_USE_KB = "Memory usage [KB]"

# --- Flavour catalog (OpenStack-style) --------------------------------------
FLAVOUR_VCPU = [1, 2, 4, 8, 16]
FLAVOUR_MEM_GB = [2, 4, 8, 16, 32, 64]

SAMPLE_MINUTES = 5
CPU_VIOLATION_MIN_RUN = 2  # consecutive samples; memory needs only 1

# --- M(eps): peaks-over-threshold tail model --------------------------------
# Defaults for the proposed recommender. See ARCHITECTURE.md, "The proposed
# method M(eps)", and TASKS.md T9.
EVT_THRESHOLD_Q = 0.95      # threshold u at this quantile of observed demand
EVT_MIN_EXCEEDANCES = 20    # below this, the tail fit is not trusted
EVT_DEFAULT_EPS = 0.01
EVT_DEFAULT_THETA = 1.0     # extremal index; 1.0 = no dependence correction (T11)
# T11 runs-declustering run length, in samples. 12 samples = one hour at the
# trace's 5-minute interval. FIXED BEFORE ANY T11 RESULT EXISTED -- runs
# declustering has a free parameter and the intervals estimator does not,
# which is exactly why the intervals estimator is the primary method and runs
# is only the sensitivity check. Choosing r after seeing the frontier would
# make it a fitted parameter, and fitting anything against the evaluation
# window breaks CLAUDE.md invariant 4.
EVT_DEFAULT_RUN_LENGTH = 12

# A capacity-provisioned column is expected to be constant (or near enough)
# for a given VM. If the first sample is off from the file's own median by
# more than this factor, the first sample is suspect -- either a corrupted
# reading (see capacity_glitch_ratio) or evidence of a genuine mid-trace
# resize (see detect_capacity_shift). Both are order-of-magnitude
# thresholds; see docs/DATA_QUALITY.md for what each has actually caught in
# this trace, including a real resize (133.csv, 12.7x) this factor does not
# distinguish from corruption on its own.
CAPACITY_GLITCH_FACTOR = 10.0
CAPACITY_SHIFT_FACTOR = 10.0
CAPACITY_SHIFT_LEVEL_TOL = 0.05  # values within this fraction of each other are "the same level"
CAPACITY_SHIFT_MIN_SHARE = 0.05  # a level must cover at least this share of rows to count as sustained


def snap_up(value, catalog):
    """Smallest catalog entry >= value. Returns the largest if value exceeds all."""
    for c in catalog:
        if c >= value:
            return c
    return catalog[-1]


def capacity_glitch_ratio(series):
    """Ratio of a capacity column's first sample to the file's own median.

    None when the median can't anchor a ratio (empty, non-finite, or <= 0).
    """
    if series.empty:
        return None
    first = float(series.iloc[0])
    median = float(series.median())
    if not np.isfinite(first) or not np.isfinite(median) or median <= 0:
        return None
    return first / median


def is_capacity_glitch(ratio, factor=CAPACITY_GLITCH_FACTOR):
    """True if a capacity-glitch ratio is more than `factor`x away from 1.0
    in either direction -- the first sample reads far above or far below
    what the rest of the file says the VM's capacity actually is.
    """
    return ratio is not None and (ratio > factor or ratio < 1.0 / factor)


def detect_capacity_shift(
    series,
    level_tol=CAPACITY_SHIFT_LEVEL_TOL,
    min_share=CAPACITY_SHIFT_MIN_SHARE,
    shift_factor=CAPACITY_SHIFT_FACTOR,
):
    """Detect a capacity column holding two (or more) materially different
    values that are each sustained for a meaningful share of the file --
    e.g. a VM genuinely resized partway through collection -- as opposed to
    one value plus noise, or a single corrupted sample.

    Values are sorted and greedily merged into "levels": each new value
    joins the current level if it is within `level_tol` of that level's
    running mean, else it starts a new level. A level only counts as
    "sustained" if it covers at least `min_share` of all rows -- this is
    what excludes a single bad sample (see capacity_glitch_ratio for that
    case) or a brief transition blip from counting as a real level.

    Returns None if fewer than two sustained levels are found, or if the
    highest and lowest sustained levels differ by less than `shift_factor`.
    Otherwise returns a dict with the low/high levels, each one's share of
    total rows, and their ratio.
    """
    vals = series.dropna().to_numpy()
    n = len(vals)
    if n == 0:
        return None

    sorted_vals = np.sort(vals)
    clusters = []  # each entry: [running_sum, count]
    for v in sorted_vals:
        if clusters:
            mean = clusters[-1][0] / clusters[-1][1]
            if mean > 0 and abs(v - mean) <= level_tol * mean:
                clusters[-1][0] += v
                clusters[-1][1] += 1
                continue
        clusters.append([v, 1])

    sustained = [(total / count, count) for total, count in clusters if count / n >= min_share]
    if len(sustained) < 2:
        return None

    lo_level, lo_count = min(sustained, key=lambda x: x[0])
    hi_level, hi_count = max(sustained, key=lambda x: x[0])
    if lo_level <= 0:
        return None
    ratio = hi_level / lo_level
    if ratio < shift_factor:
        return None

    return {
        "low_level": lo_level,
        "low_share": lo_count / n,
        "high_level": hi_level,
        "high_share": hi_count / n,
        "ratio": ratio,
    }


def fit_gpd_tail(values, threshold_q=EVT_THRESHOLD_Q, min_exceedances=EVT_MIN_EXCEEDANCES):
    """Fit a Generalized Pareto tail to exceedances over a high threshold.

    OBSERVATION WINDOW ONLY (CLAUDE.md invariant 6). `values` must never be
    drawn from the evaluation window.

    The threshold u is the `threshold_q` quantile of `values`; exceedances are
    the amounts by which samples exceed u. The GPD location is fixed at 0
    (`floc=0`) because the excesses are already measured relative to u --
    letting scipy fit a location as well would double-count the threshold and
    silently shift the tail.

    Returns (fit, None) on success or (None, reason) when the tail cannot be
    trusted. The caller is expected to fall back to `peak` on a reason, which
    is the conservative direction: peak reclaims less, so a fallback can only
    make M(eps) look worse, never better.
    """
    v = np.asarray(values, dtype=float)
    v = v[np.isfinite(v)]
    n_obs = int(v.size)
    if n_obs == 0:
        return None, "no_samples"

    u = float(np.quantile(v, threshold_q))
    excesses = v[v > u] - u
    n_exceed = int(excesses.size)

    # Too thin a tail to estimate a shape parameter from. This fires on flat
    # or heavily quantised series -- a VM pinned at one memory value has a
    # 95th percentile equal to its maximum and no exceedances at all.
    if n_exceed < min_exceedances:
        return None, "few_exceedances"

    try:
        shape, _loc, scale = genpareto.fit(excesses, floc=0.0)
    except Exception:
        return None, "fit_raised"

    if not np.isfinite(shape) or not np.isfinite(scale) or scale <= 0:
        return None, "fit_degenerate"

    return (
        {
            "u": u,
            "shape": float(shape),
            "scale": float(scale),
            "n_exceed": n_exceed,
            "n_obs": n_obs,
            # P(X > u) estimated empirically, not from the GPD. This is what
            # makes p(c) continuous at c = u: rate * sf(0) = rate.
            "rate": n_exceed / n_obs,
            "sorted": np.sort(v),
        },
        None,
    )


def tail_exceedance_prob(c, fit):
    """Per-sample probability that demand exceeds capacity c.

        c >= u :  p(c) = rate * GPD_survival(c - u)
        c <  u :  empirical fraction of observed samples strictly above c

    The two branches agree at c = u, so p(c) is non-increasing in c across the
    join -- which is what makes the flavour search below well defined.
    """
    u = fit["u"]
    if c >= u:
        return float(fit["rate"]) * float(
            genpareto.sf(c - u, fit["shape"], loc=0.0, scale=fit["scale"])
        )
    sv = fit["sorted"]
    return float(np.count_nonzero(sv > c)) / float(sv.size)


def exceedance_indices(values, u):
    """Positions (in sample order) of the samples strictly above u.

    The threshold is the same u the GPD was fitted over, so the tail model and
    the dependence correction describe the same set of extremes. OBSERVATION
    WINDOW ONLY (CLAUDE.md invariant 4).
    """
    v = np.asarray(values, dtype=float)
    return np.flatnonzero(np.isfinite(v) & (v > float(u)))


def estimate_theta_intervals(values, u):
    """Extremal index by the intervals estimator (Ferro and Segers, 2003).

    PRIMARY method for T11, because it has no free parameter: the estimator is
    a function of the interexceedance times alone, so nothing about it can be
    tuned -- accidentally or otherwise -- against the evaluation window.

    Given exceedance positions S_1 < ... < S_N over u, the interexceedance
    times are T_i = S_{i+1} - S_i for i = 1..N-1. Ferro and Segers give

        max T <= 2:  theta = min(1,  2 (sum T)^2 / ((N-1) sum T^2))
        max T >  2:  theta = min(1,  2 (sum (T-1))^2
                                     / ((N-1) sum (T-1)(T-2)))

    The second form is the bias-corrected one, used whenever any gap exceeds
    two samples -- which, on five-minute utilisation data, is essentially
    always. theta near 1 means exceedances arrive independently; theta near 0
    means they arrive in long clusters, so a 9-day window contains far fewer
    independent chances to exceed than it has samples.

    Returns (theta, None) or (None, reason) when it cannot be estimated. The
    caller is expected to fall back to theta = 1, which is the conservative
    direction: more effective trials means a larger allocation.
    """
    idx = exceedance_indices(values, u)
    n_exceed = int(idx.size)
    if n_exceed < 2:
        return None, "too_few_exceedances"

    t = np.diff(idx).astype(float)
    n = float(n_exceed - 1)

    if t.max() <= 2.0:
        denom = n * float(np.sum(t * t))
        numer = 2.0 * float(np.sum(t)) ** 2
    else:
        tm1 = t - 1.0
        denom = n * float(np.sum(tm1 * (t - 2.0)))
        numer = 2.0 * float(np.sum(tm1)) ** 2

    if not np.isfinite(numer) or not np.isfinite(denom) or denom <= 0:
        return None, "degenerate"

    theta = numer / denom
    if not np.isfinite(theta) or theta <= 0:
        return None, "degenerate"
    return float(min(1.0, theta)), None


def estimate_theta_runs(values, u, run_length=EVT_DEFAULT_RUN_LENGTH):
    """Extremal index by runs declustering -- the SENSITIVITY CHECK, not the
    primary estimate.

    Exceedances are grouped into clusters; a gap of more than `run_length`
    samples between consecutive exceedances starts a new cluster. The estimate
    is (number of clusters) / (number of exceedances): if every exceedance is
    its own cluster theta = 1, and if they all fall in one episode
    theta = 1/N.

    `run_length` is a free parameter, which is the method's weakness and the
    reason it is secondary. It is fixed at `EVT_DEFAULT_RUN_LENGTH` (one hour)
    before any T11 result existed; see that constant.

    Returns (theta, None) or (None, reason).
    """
    idx = exceedance_indices(values, u)
    n_exceed = int(idx.size)
    if n_exceed < 1:
        return None, "too_few_exceedances"
    if n_exceed == 1:
        return 1.0, None

    gaps = np.diff(idx)
    clusters = 1 + int(np.count_nonzero(gaps > float(run_length)))
    return float(min(1.0, clusters / n_exceed)), None


def estimate_theta(values, u, method="intervals", run_length=EVT_DEFAULT_RUN_LENGTH):
    """Dispatch to a named extremal-index estimator.

    "fixed" returns None so the caller uses its own theta unchanged -- that is
    the theta = 1 arm of the T12 ablation, not an estimate.
    """
    if method == "fixed":
        return None, "fixed"
    if method == "intervals":
        return estimate_theta_intervals(values, u)
    if method == "runs":
        return estimate_theta_runs(values, u, run_length=run_length)
    raise ValueError(method)


def horizon_violation_prob(p, n_eval, theta=EVT_DEFAULT_THETA):
    """Probability of at least one exceedance over an n_eval-sample horizon.

        P = 1 - (1 - p) ** (theta * n_eval)

    theta is the extremal index. theta = 1 treats every sample as an
    independent chance to exceed; runs declustering (T11) lowers it, because
    one 40-minute spike is one event rather than eight. theta is a parameter
    here precisely so T11 can change it without restructuring anything.
    """
    p = min(max(float(p), 0.0), 1.0)
    trials = float(theta) * float(n_eval)
    if trials <= 0:
        return 0.0
    return float(1.0 - (1.0 - p) ** trials)


def evt_required_capacity(fit, eps, n_eval, theta=EVT_DEFAULT_THETA):
    """Smallest continuous capacity c whose horizon probability P(c) <= eps.

    Inverts horizon_violation_prob analytically rather than scanning, so the
    snapped and unsnapped (T12) answers come from the same computation:

        p_target = 1 - (1 - eps) ** (1 / (theta * n_eval))

    then invert p(c). Above the threshold that is the GPD inverse survival
    function; below it -- which happens when eps is loose enough that the
    target sits inside the body of the distribution -- it is an empirical
    quantile, since there is no tail model down there to extrapolate with.

    Returns inf when no finite capacity satisfies eps (eps <= 0, or a
    negative-shape GPD whose support ends before the requirement).
    """
    trials = float(theta) * float(n_eval)
    if trials <= 0:
        return fit["u"]
    if eps <= 0:
        return float("inf")
    if eps >= 1:
        return 0.0

    p_target = 1.0 - (1.0 - float(eps)) ** (1.0 / trials)

    if p_target >= fit["rate"]:
        # Target lies below the threshold: use the empirical distribution.
        # Smallest observed value c with #{x > c} / n <= p_target.
        sv = fit["sorted"]
        n = sv.size
        k = int(np.floor(p_target * n))  # samples we are allowed to leave above c
        if k <= 0:
            return float(sv[-1])
        if k >= n:
            return float(sv[0])
        return float(sv[n - 1 - k])

    q = p_target / fit["rate"]
    excess = float(genpareto.isf(q, fit["shape"], loc=0.0, scale=fit["scale"]))
    if not np.isfinite(excess):
        return float("inf")
    return fit["u"] + excess


def evt_allocation(
    obs_values,
    catalog,
    eps,
    n_eval,
    theta=EVT_DEFAULT_THETA,
    threshold_q=EVT_THRESHOLD_Q,
    min_exceedances=EVT_MIN_EXCEEDANCES,
    snap=True,
    cap=None,
):
    """M(eps) allocation for one VM on one resource, observation window only.

    Returns (allocation, diagnostics). `diagnostics["fallback"]` is None on a
    normal fit, or the reason the VM fell back to peak on this resource.
    Fallback is per resource: a VM may take a fitted memory allocation and a
    peak CPU allocation.

    `cap` is the VM's original allocation on this resource. It is the support
    of the demand distribution, not a tuning knob: `load_vm` clips demand to
    the original allocation, so demand has bounded support there and
    P(demand > c) = 0 for every c at or above it. A GPD extrapolated past the
    cap is therefore predicting mass where the data model permits none, and
    the capped answer is the one consistent with the model. `capped` in the
    diagnostics counts how often that happened -- a high count means the
    fitted tail is badly mis-specified, which is a result worth reporting
    rather than a number to suppress.
    """
    v = np.asarray(obs_values, dtype=float)
    v = v[np.isfinite(v)]
    peak = float(v.max()) if v.size else 0.0

    def _apply_cap(alloc):
        """Returns (allocation, was_capped). Applied after snapping, so the
        capped result is min(snapped requirement, original allocation) and
        reclamation on this resource can never go negative: at worst M(eps)
        recommends leaving the VM exactly as provisioned.
        """
        if cap is None or not np.isfinite(cap) or alloc <= cap:
            return alloc, False
        return float(cap), True

    def _fallback(reason):
        alloc, was_capped = _apply_cap(snap_up(peak, catalog) if snap else peak)
        return alloc, {
            "fallback": reason,
            "capped": was_capped,
            "u": float("nan"),
            "shape": float("nan"),
            "scale": float("nan"),
            "n_exceed": 0,
            "n_obs": int(v.size),
            "required_capacity": peak,
            "peak": peak,
        }

    fit, reason = fit_gpd_tail(v, threshold_q=threshold_q, min_exceedances=min_exceedances)
    if fit is None:
        return _fallback(reason)

    c = evt_required_capacity(fit, eps, n_eval, theta=theta)
    if not np.isfinite(c):
        return _fallback("capacity_unbounded")

    alloc, was_capped = _apply_cap(snap_up(c, catalog) if snap else float(c))
    return alloc, {
        "fallback": None,
        "capped": was_capped,
        "u": fit["u"],
        "shape": fit["shape"],
        "scale": fit["scale"],
        "n_exceed": fit["n_exceed"],
        "n_obs": fit["n_obs"],
        "required_capacity": float(c),
        "peak": peak,
    }


def load_vm(path):
    """Parse one Bitbrains CSV into a demand frame. Returns (df, meta) or None."""
    try:
        df = pd.read_csv(path, sep=";", engine="python")
    except Exception as exc:
        print(f"  skip {os.path.basename(path)}: unreadable ({exc})", file=sys.stderr)
        return None

    df.columns = [c.strip().strip('"') for c in df.columns]
    needed = [COL_TS, COL_CORES, COL_CAP_MHZ, COL_USE_MHZ, COL_MEM_CAP_KB, COL_MEM_USE_KB]
    missing = [c for c in needed if c not in df.columns]
    if missing:
        print(f"  skip {os.path.basename(path)}: missing {missing}", file=sys.stderr)
        return None

    for c in needed:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=needed)
    if df.empty:
        return None

    cores = float(df[COL_CORES].iloc[0])
    cap_mhz = float(df[COL_CAP_MHZ].iloc[0])
    mem_cap_gb = float(df[COL_MEM_CAP_KB].iloc[0]) / (1024.0 * 1024.0)

    # Convert MHz demand to core-equivalents using the VM's own clock rate.
    per_core_mhz = (cap_mhz / cores) if (cap_mhz > 0 and cores > 0) else np.nan
    if not np.isfinite(per_core_mhz) or per_core_mhz <= 0:
        print(f"  skip {os.path.basename(path)}: bad CPU capacity", file=sys.stderr)
        return None

    # Decided (docs/DATA_QUALITY.md, pattern 4): a VM already provisioned
    # below the smallest flavour in the catalog cannot be right-sized -- no
    # smaller entry exists to snap to, so snap_up forces its allocation
    # upward under every method, producing negative reclamation that
    # measures the catalog floor, not the recommender.
    if cores < min(FLAVOUR_VCPU) or mem_cap_gb < min(FLAVOUR_MEM_GB):
        print(f"  skip {os.path.basename(path)}: below flavour catalog floor", file=sys.stderr)
        return None

    # Decided (docs/DATA_QUALITY.md, pattern 3): a VM whose provisioned
    # capacity changes mid-trace is not one right-sizeable entity -- the
    # observation window describes a machine that no longer exists by the
    # evaluation window. Excluded rather than split at the shift point
    # (leaves sub-windows too short for a 21/9 split) or given post-shift
    # capacity (pairs demand history with the wrong allocation).
    if detect_capacity_shift(df[COL_CAP_MHZ]) or detect_capacity_shift(df[COL_MEM_CAP_KB]):
        print(f"  skip {os.path.basename(path)}: mid-trace capacity shift", file=sys.stderr)
        return None

    out = pd.DataFrame(
        {
            # Column is named "[ms]" but holds seconds -- see CLAUDE.md dataset traps.
            "ts": df[COL_TS].to_numpy(),
            "cpu_cores": df[COL_USE_MHZ].to_numpy() / per_core_mhz,
            "mem_gb": df[COL_MEM_USE_KB].to_numpy() / (1024.0 * 1024.0),
        }
    )

    # Demand above the original allocation is a measurement artefact, not a
    # right-sizing opportunity. Clip so baselines are not distorted by it.
    out["cpu_cores"] = out["cpu_cores"].clip(lower=0.0, upper=cores)
    out["mem_gb"] = out["mem_gb"].clip(lower=0.0, upper=mem_cap_gb)

    meta = {
        "vm_id": os.path.splitext(os.path.basename(path))[0],
        "orig_cores": cores,
        "orig_mem_gb": mem_cap_gb,
        "n_samples": len(out),
    }
    return out, meta


def count_violations(demand, allocation, min_run):
    """Return (event_count, total_samples_exhausted) for runs >= min_run."""
    over = (demand > allocation).to_numpy()
    if not over.any():
        return 0, 0

    events = 0
    exhausted = 0
    run = 0
    for flag in over:
        if flag:
            run += 1
        else:
            if run >= min_run:
                events += 1
                exhausted += run
            run = 0
    if run >= min_run:
        events += 1
        exhausted += run
    return events, exhausted


def split_obs_eval(df, obs_days, eval_days):
    """Split a VM's demand frame into (observation, evaluation) by wall-clock
    time, using the "ts" column (seconds since epoch) rather than row count.

    Observation is the first `obs_days` from the VM's own first sample;
    evaluation is the `eval_days` immediately after that. Row count is not a
    reliable proxy for elapsed time -- sampling interval varies per VM -- so
    this must not assume SAMPLE_MINUTES-spaced rows.

    Returns (obs, eval) or None if the trace does not run long enough to
    cover the full obs_days + eval_days span.
    """
    if df.empty or len(df) < 2:
        return None

    t0 = df["ts"].min()
    t_last = df["ts"].max()
    obs_end = t0 + obs_days * 86400.0
    eval_end = obs_end + eval_days * 86400.0

    # A discretely-sampled trace essentially never lands exactly on the
    # nominal day boundary -- its last sample sits up to one sampling
    # interval short of it. Requiring t_last >= eval_end exactly would
    # reject almost every real trace over a rounding artefact, so allow a
    # shortfall of up to this VM's own median sampling gap.
    gap = df["ts"].diff().median()
    if not np.isfinite(gap) or gap <= 0:
        gap = 0.0

    if t_last < eval_end - gap:
        return None

    obs = df[(df["ts"] >= t0) & (df["ts"] < obs_end)]
    ev = df[(df["ts"] >= obs_end) & (df["ts"] < eval_end)]
    return obs, ev


def recommend_evt(
    obs,
    eps,
    n_eval,
    theta=EVT_DEFAULT_THETA,
    threshold_q=EVT_THRESHOLD_Q,
    min_exceedances=EVT_MIN_EXCEEDANCES,
    snap=True,
    cap_cores=None,
    cap_mem_gb=None,
):
    """M(eps) for one VM: returns ((cpu_alloc, mem_alloc), diagnostics).

    Each resource is fitted, capped and falls back independently. Kept
    separate from `recommend()` so the per-VM tail parameters survive to the
    diagnostics file -- a frontier point with no record of which VMs fell
    back or hit the cap is not interpretable.

    `cap_cores` / `cap_mem_gb` are the VM's original allocation; see
    `evt_allocation` for why the cap is the demand model's support rather
    than a tuning choice.
    """
    cpu_alloc, cpu_diag = evt_allocation(
        obs["cpu_cores"].to_numpy(), FLAVOUR_VCPU, eps, n_eval,
        theta=theta, threshold_q=threshold_q,
        min_exceedances=min_exceedances, snap=snap, cap=cap_cores,
    )
    mem_alloc, mem_diag = evt_allocation(
        obs["mem_gb"].to_numpy(), FLAVOUR_MEM_GB, eps, n_eval,
        theta=theta, threshold_q=threshold_q,
        min_exceedances=min_exceedances, snap=snap, cap=cap_mem_gb,
    )
    return (cpu_alloc, mem_alloc), {"cpu": cpu_diag, "mem": mem_diag}


def recommend(obs, policy, snap=True, **evt_kwargs):
    """Allocation from the observation window under the named policy.

    `snap=False` returns the raw continuous value instead of the smallest
    catalog entry at or above it. That is NOT a valid right-sizing policy --
    no real private cloud provisions arbitrary fractional capacity, which is
    why the catalog exists (see CLAUDE.md invariant 5). It exists solely as
    the counterfactual for measuring how much of a reported reclamation gap is
    catalog quantisation rather than workload behaviour. Snapping remains the
    default and the only mode any reported result uses.

    Policy "evt" is the proposed method M(eps) and requires `eps` and
    `n_eval`; use `recommend_evt` directly when the tail diagnostics are
    wanted as well.
    """
    if policy == "peak":
        cpu = obs["cpu_cores"].max()
        mem = obs["mem_gb"].max()
    elif policy == "p95":
        cpu = obs["cpu_cores"].quantile(0.95)
        mem = obs["mem_gb"].quantile(0.95)
    elif policy == "evt":
        alloc, _diag = recommend_evt(obs, snap=snap, **evt_kwargs)
        return alloc
    else:
        raise ValueError(policy)
    if not snap:
        return float(cpu), float(mem)
    return snap_up(cpu, FLAVOUR_VCPU), snap_up(mem, FLAVOUR_MEM_GB)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--traces", required=True, help="directory of per-VM CSVs")
    ap.add_argument("--out", default="results")
    ap.add_argument("--obs-days", type=float, default=21.0)
    ap.add_argument("--eval-days", type=float, default=9.0)
    ap.add_argument("--idle-p99", type=float, default=0.05,
                    help="VMs with p99 CPU below this fraction of allocation "
                         "are flagged idle and reported separately")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--unsnapped", action="store_true",
                    help="DIAGNOSTIC ONLY: allocate the raw continuous "
                         "max/p95 instead of snapping up to the flavour "
                         "catalog. Not a valid policy -- used only as the "
                         "counterfactual for measuring how much of a "
                         "reclamation gap is catalog quantisation. Write it "
                         "to a separate --out directory; never mix its "
                         "outputs with a reported run.")
    ap.add_argument("--evt", action="store_true",
                    help="also run the proposed method M(eps) as a fourth "
                         "policy. Off by default so the three-baseline run "
                         "stays byte-identical to the Review 1 result.")
    ap.add_argument("--eps", type=float, default=EVT_DEFAULT_EPS,
                    help="M(eps) target probability of at least one "
                         "exceedance over the evaluation horizon")
    ap.add_argument("--theta", type=float, default=EVT_DEFAULT_THETA,
                    help="extremal index for the horizon formula. 1.0 = no "
                         "dependence correction (T11 makes this per-VM)")
    ap.add_argument("--evt-threshold-q", type=float, default=EVT_THRESHOLD_Q,
                    help="quantile of observed demand used as the GPD threshold u")
    ap.add_argument("--evt-min-exceedances", type=int, default=EVT_MIN_EXCEEDANCES,
                    help="fall back to peak below this many exceedances over u")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)

    if args.unsnapped:
        print("*** --unsnapped: allocations are CONTINUOUS, not catalog "
              "flavours.\n*** This is a diagnostic counterfactual, not a "
              "result. Do not report these\n*** numbers as reclamation "
              "achieved by any method.\n")

    # Nominal counts assuming uniform 5-minute sampling -- printed for
    # reference only. The actual split below is by wall-clock time, since
    # sampling interval varies per VM (5s to well over 5min in this trace).
    obs_samples = int(args.obs_days * 24 * 60 / SAMPLE_MINUTES)
    eval_samples = int(args.eval_days * 24 * 60 / SAMPLE_MINUTES)
    eval_hours = args.eval_days * 24.0

    paths = sorted(glob.glob(os.path.join(args.traces, "*.csv")))
    if args.limit:
        paths = paths[: args.limit]
    if not paths:
        raise SystemExit(f"no CSVs found in {args.traces}")

    print(f"loading {len(paths)} traces from {args.traces}")
    print(f"split: {args.obs_days} observation days / {args.eval_days} evaluation days "
          f"(~{obs_samples} / ~{eval_samples} samples at uniform 5-min sampling)")

    rows = []
    evt_rows = []
    skipped_short = 0
    # Fallback tally, per resource -- reported per run (TASKS.md T9).
    evt_fallbacks = {"cpu": {}, "mem": {}}
    # Cap tally, per resource. A VM hits the cap when the fitted tail asks for
    # more capacity than the VM was ever provisioned -- i.e. the extrapolation
    # left the support of the demand distribution. The count diagnoses tail
    # mis-specification, so it is reported, not smoothed over.
    evt_capped = {"cpu": 0, "mem": 0}

    for path in paths:
        loaded = load_vm(path)
        if loaded is None:
            continue
        df, meta = loaded

        split = split_obs_eval(df, args.obs_days, args.eval_days)
        if split is None:
            skipped_short += 1
            continue
        obs, ev = split
        if obs.empty or ev.empty:
            skipped_short += 1
            continue

        p99_cpu = obs["cpu_cores"].quantile(0.99)
        is_idle = (p99_cpu / meta["orig_cores"]) < args.idle_p99 if meta["orig_cores"] else True

        allocations = {
            "as_provisioned": (meta["orig_cores"], meta["orig_mem_gb"]),
            "peak": recommend(obs, "peak", snap=not args.unsnapped),
            "p95": recommend(obs, "p95", snap=not args.unsnapped),
        }

        if args.evt:
            # Horizon length in samples, derived from the OBSERVATION
            # window's own median sampling gap (CLAUDE.md invariant 6). The
            # nominal 2592 assumes uniform 5-minute sampling, which this
            # trace does not honour -- intervals vary per VM -- and using a
            # per-VM count taken from the evaluation window would leak
            # evaluation data into the recommender.
            obs_gap = obs["ts"].diff().median()
            if np.isfinite(obs_gap) and obs_gap > 0:
                n_eval_vm = max(1, int(round(args.eval_days * 86400.0 / float(obs_gap))))
            else:
                n_eval_vm = eval_samples

            evt_alloc, evt_diag = recommend_evt(
                obs, args.eps, n_eval_vm, theta=args.theta,
                threshold_q=args.evt_threshold_q,
                min_exceedances=args.evt_min_exceedances,
                snap=not args.unsnapped,
                cap_cores=meta["orig_cores"],
                cap_mem_gb=meta["orig_mem_gb"],
            )
            allocations["evt"] = evt_alloc

            row = {"vm_id": meta["vm_id"], "eps": args.eps, "theta": args.theta,
                   "n_eval": n_eval_vm}
            for res in ("cpu", "mem"):
                d = evt_diag[res]
                if d["fallback"]:
                    evt_fallbacks[res][d["fallback"]] = (
                        evt_fallbacks[res].get(d["fallback"], 0) + 1
                    )
                if d["capped"]:
                    evt_capped[res] += 1
                for k, v in d.items():
                    row[f"{res}_{k}"] = v
            row["cpu_alloc"] = evt_alloc[0]
            row["mem_alloc"] = evt_alloc[1]
            evt_rows.append(row)

        for method, (cpu_alloc, mem_alloc) in allocations.items():
            cpu_events, cpu_samples = count_violations(
                ev["cpu_cores"], cpu_alloc, CPU_VIOLATION_MIN_RUN
            )
            mem_events, mem_samples = count_violations(ev["mem_gb"], mem_alloc, 1)

            rows.append(
                {
                    "vm_id": meta["vm_id"],
                    "method": method,
                    "idle": is_idle,
                    "orig_cores": meta["orig_cores"],
                    "orig_mem_gb": meta["orig_mem_gb"],
                    "alloc_cores": cpu_alloc,
                    "alloc_mem_gb": mem_alloc,
                    "recl_vcpu_hours": (meta["orig_cores"] - cpu_alloc) * eval_hours,
                    "recl_mem_gb_hours": (meta["orig_mem_gb"] - mem_alloc) * eval_hours,
                    "cpu_events": cpu_events,
                    "cpu_exhausted_minutes": cpu_samples * SAMPLE_MINUTES,
                    "mem_events": mem_events,
                    "mem_exhausted_minutes": mem_samples * SAMPLE_MINUTES,
                    "any_violation": (cpu_events + mem_events) > 0,
                }
            )

    if not rows:
        raise SystemExit("no VMs had enough samples for the requested split")

    per_vm = pd.DataFrame(rows)
    per_vm.to_csv(os.path.join(args.out, "per_vm_results.csv"), index=False)

    methods = ["as_provisioned", "peak", "p95"] + (["evt"] if args.evt else [])

    if args.evt:
        evt_df = pd.DataFrame(evt_rows)
        evt_df.to_csv(os.path.join(args.out, "evt_diagnostics.csv"), index=False)
        n_evt = len(evt_df)
        print(f"\nM(eps) tail fits at eps={args.eps:g}, theta={args.theta:g}, "
              f"u=q{args.evt_threshold_q:g}, min_exceedances={args.evt_min_exceedances}")
        for res in ("cpu", "mem"):
            tally = evt_fallbacks[res]
            total = sum(tally.values())
            detail = ", ".join(f"{k}={v}" for k, v in sorted(tally.items())) or "none"
            pct = (100.0 * total / n_evt) if n_evt else 0.0
            cap_n = evt_capped[res]
            cap_pct = (100.0 * cap_n / n_evt) if n_evt else 0.0
            print(f"  {res}: {total}/{n_evt} fell back to peak ({pct:.1f}%) [{detail}]; "
                  f"{cap_n}/{n_evt} capped at original allocation ({cap_pct:.1f}%)")
            shapes = evt_df[f"{res}_shape"].dropna()
            if len(shapes):
                # The GPD shape is deliberately UNCONSTRAINED. xi >= 0.5 means
                # infinite variance and xi >= 1 an infinite mean, so the count
                # above those points is the honest measure of how far the tail
                # model is being pushed.
                print(f"        shape xi: n={len(shapes)} min={shapes.min():.3f} "
                      f"q25={shapes.quantile(.25):.3f} median={shapes.median():.3f} "
                      f"q75={shapes.quantile(.75):.3f} max={shapes.max():.3f} | "
                      f"xi<0: {(shapes < 0).sum()}, xi>=0.5: {(shapes >= 0.5).sum()}, "
                      f"xi>=1 (infinite mean): {(shapes >= 1).sum()}")

    n_vms = per_vm["vm_id"].nunique()
    n_idle = per_vm[per_vm["idle"]]["vm_id"].nunique()
    print(
        f"\nusable VMs: {n_vms}  (idle: {n_idle}, "
        f"skipped: {skipped_short} -- do not cover the full "
        f"{args.obs_days:g}+{args.eval_days:g} day obs+eval span)"
    )

    def summarise(frame, label):
        total_alloc_cpu = (
            frame[frame["method"] == "as_provisioned"]["orig_cores"].sum() * eval_hours
        )
        total_alloc_mem = (
            frame[frame["method"] == "as_provisioned"]["orig_mem_gb"].sum() * eval_hours
        )
        out = []
        for method in methods:
            sub = frame[frame["method"] == method]
            if sub.empty:
                continue
            n = len(sub)
            recl_cpu = sub["recl_vcpu_hours"].sum()
            recl_mem = sub["recl_mem_gb_hours"].sum()
            cpu_violated = sub["cpu_events"] > 0
            mem_violated = sub["mem_events"] > 0
            out.append(
                {
                    "scope": label,
                    "method": method,
                    "vms": n,
                    "recl_vcpu_hours": round(recl_cpu, 1),
                    "recl_vcpu_pct": round(100 * recl_cpu / total_alloc_cpu, 2)
                    if total_alloc_cpu
                    else 0.0,
                    "recl_mem_gb_hours": round(recl_mem, 1),
                    "recl_mem_pct": round(100 * recl_mem / total_alloc_mem, 2)
                    if total_alloc_mem
                    else 0.0,
                    # Combined (both resources) -- kept for backward comparability.
                    "cpu_events": int(sub["cpu_events"].sum()),
                    "mem_events": int(sub["mem_events"].sum()),
                    "exhausted_minutes": int(
                        sub["cpu_exhausted_minutes"].sum()
                        + sub["mem_exhausted_minutes"].sum()
                    ),
                    "vms_with_violation": int(sub["any_violation"].sum()),
                    "pct_vms_safe": round(100 * (1 - sub["any_violation"].mean()), 2),
                    # Split by resource -- the CPU rule (>=2 consecutive
                    # samples, degrading) and the memory rule (any single
                    # sample, terminal) are asymmetric by design and must be
                    # reported separately, not just merged into "a
                    # violation".
                    "cpu_exhausted_minutes": int(sub["cpu_exhausted_minutes"].sum()),
                    "cpu_vms_with_violation": int(cpu_violated.sum()),
                    "cpu_pct_safe": round(100 * (1 - cpu_violated.mean()), 2),
                    "mem_exhausted_minutes": int(sub["mem_exhausted_minutes"].sum()),
                    "mem_vms_with_violation": int(mem_violated.sum()),
                    "mem_pct_safe": round(100 * (1 - mem_violated.mean()), 2),
                }
            )
        return out

    summary = pd.DataFrame(
        summarise(per_vm, "all_vms") + summarise(per_vm[~per_vm["idle"]], "non_idle")
    )
    summary.to_csv(os.path.join(args.out, "baseline_summary.csv"), index=False)

    with pd.option_context("display.width", 200, "display.max_columns", 50):
        print("\n" + "=" * 100)
        print("BASELINE SUMMARY  (this table is Figure 1 of the paper)")
        print("=" * 100)
        print(summary.to_string(index=False))

    with open(os.path.join(args.out, "run_config.json"), "w") as fh:
        json.dump(
            {
                "obs_days": args.obs_days,
                "eval_days": args.eval_days,
                "obs_samples": obs_samples,
                "eval_samples": eval_samples,
                "flavour_vcpu": FLAVOUR_VCPU,
                "flavour_mem_gb": FLAVOUR_MEM_GB,
                "cpu_violation_min_run": CPU_VIOLATION_MIN_RUN,
                "idle_p99_threshold": args.idle_p99,
                "vms_used": int(n_vms),
                "vms_skipped_short": skipped_short,
                # False marks a diagnostic counterfactual run, not a result.
                "snapped_to_catalog": not args.unsnapped,
                "evt_enabled": bool(args.evt),
                **(
                    {
                        "evt_eps": args.eps,
                        "evt_theta": args.theta,
                        "evt_threshold_q": args.evt_threshold_q,
                        "evt_min_exceedances": args.evt_min_exceedances,
                        "evt_fallbacks_cpu": evt_fallbacks["cpu"],
                        "evt_fallbacks_mem": evt_fallbacks["mem"],
                        "evt_capped_at_orig_cpu": evt_capped["cpu"],
                        "evt_capped_at_orig_mem": evt_capped["mem"],
                        "evt_capped_at_orig": True,
                    }
                    if args.evt
                    else {}
                ),
            },
            fh,
            indent=2,
        )

    print(f"\nwrote {args.out}/per_vm_results.csv, baseline_summary.csv, run_config.json")


if __name__ == "__main__":
    main()
