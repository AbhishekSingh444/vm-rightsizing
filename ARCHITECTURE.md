# Architecture

## The claim the system exists to test

Right-sizing buys reclaimed capacity by spending safety margin. Nobody reports
the price. This system computes both sides of that trade and checks that the
predicted price matches the observed one.

Two independent measurement paths, deliberately:

- **Offline path** — 1,250 VMs, replayed in pandas. Fast, complete, cheap. This
  produces the frontier and every number in the results table.
- **Cluster path** — 50 VMs, replayed as real containers under real cgroup
  limits, in 5 sequential waves at scale S = 1/16. Slow, partial, expensive.
  This exists to check that the offline path's violation definition corresponds
  to something a kernel would actually do.

If the two disagree, the offline definition is wrong. That disagreement is worth
reporting; it is the check nobody in the literature performs.

**Outcome (run complete, 2026-09-09).** They do not disagree. Compared like with
like — each VM's peak scaled demand against the limit the kernel actually
enforced — the offline memory rule reproduces the kernel's verdict on **50 of 50
VMs, zero mismatches**. Compared naively, against the recommendation rather than
the enforced limit, the table reads 26 TP / 4 FN / 0 FP / 20 TN; all four false
negatives are VMs sitting inside the agent margin band (see "The margin band"
below), and there are no false positives anywhere in the run. The full result is
`evidence/T8_confusion.md`.

## Data flow

```
GWA-T-12 fastStorage (1250 CSVs, one per VM)
          |
          v
  src/inspect_traces.py ................ preflight: coverage, sampling interval,
          |                              true span, dirty columns, headroom
          |                              -> dataset_report.csv  [SCREENSHOT]
          v
  src/baselines.py ..................... split obs/eval, compute B0/B1/B2,
          |                              replay eval, count violations
          |                              -> results/per_vm_results.csv
          |                              -> results/baseline_summary.csv  [FIGURE 1]
          |                              -> results/run_config.json
          v
  src/make_recommendations.py .......... reshape for the cluster; pick a
          |                              STRATIFIED subset (half predicted to
          |                              violate, half predicted safe)
          |                              -> results/recommendations.csv
          |                              -> results/subset_50.csv
          |                              -> results/subset_50_predictions.csv
          v
  testbed/stage_traces.py .............. evaluation-window slice only, scaled
          |                              by S, one CSV per VM -> data/traces/
          v
  testbed/plan_waves.py ................ pack the 50 VMs into waves that fit the
          |                              node -> testbed/wave{N}_vms.txt
          v
  testbed/gen_manifests.py ............. one Pod per (VM, method), limits set to
          |                              the scaled recommendation + margin M
          v
  kind cluster + testbed/replay_agent.py  real load, real cgroup enforcement
          |                                    |
          |                                    +-- testbed/sample_cgroups.py
          |                                        memory.current / memory.peak
          |                                        sampled while the wave runs
          v
  testbed/collect_evidence.sh .......... -> evidence/wave{N}/  [RUBRIC ARTIFACTS]
          |
          v
  testbed/t8_report.py ................. offline prediction vs kernel reality
                                         -> evidence/T8_confusion.md
                                         -> results/wave{N}_outcomes.csv
                                         [THE INTERESTING BIT]
```

## Modules

### `src/inspect_traces.py`
Preflight. Run this **before** anything else and before trusting any assumption
in the written documents.

It answers three questions that change downstream decisions:
- What is the true sampling interval and span? This sets the observation and
  evaluation window sizes. The docs assume ~30 days at 5 minutes; verify it.
- Which VMs have dirty memory columns (pinned at provisioned, or near zero)?
  The cleaning rule you choose must be stated in the paper.
- What is the fleet-wide allocated-versus-used gap? This is Objective O1 and the
  motivating number for the whole project.

Output is designed to be screenshotted as-is.

### `src/baselines.py`
The core offline engine. Three allocation policies:

| Policy | Rule |
|---|---|
| `as_provisioned` | the trace's original allocation; reclaims nothing |
| `peak` | max demand over the observation window |
| `p95` | 95th percentile over the observation window |

All snap upward to the flavour catalog. All see the observation window only.

Two functions carry the correctness burden:

- `count_violations(demand, allocation, min_run)` — returns event count and
  exhausted sample count for runs of at least `min_run`. Called with
  `min_run=1` for memory and `min_run=2` for CPU. An off-by-one here silently
  changes every number in the report, which is why it has its own tests.
- `snap_up(value, catalog)` — smallest catalog entry at or above the value.

Design decisions worth knowing before you edit:

- **Demand is clipped to the original allocation on load.** Telemetry reporting
  above 100% utilisation is an artefact, not a right-sizing opportunity, and
  unclipped it inflates the peak baseline.
- **CPU is converted to core-equivalents using each VM's own clock rate**
  (`capacity MHz / cores`), not a fleet constant. Provisioned clock rates differ
  across the trace.
- **Everything is reported twice** — all VMs, and non-idle only. Idle VMs
  dominate aggregate savings; a headline number that includes them is counting
  dead machines.

### `src/make_recommendations.py`
Bridge between the offline results and the cluster. The non-obvious part is
subset selection: a random 50 from a mostly-idle fleet will be almost entirely
quiet, and a cluster run where nothing happens proves nothing. The subset is
stratified so half the VMs are predicted to violate.

It also writes `*_predictions.csv` — the offline prediction for exactly those
VMs, which is what the cluster's actual OOMKills get compared against.

### `testbed/replay_agent.py`
Runs inside each pod. Reads one VM's evaluation-window CSV, reproduces its CPU
and memory demand, and self-reports throttling.

- **Standard library only.** No pip install inside the container.
- **Throttle detection reads the container's own cgroup** (`cpu.stat`, v1 and v2
  both handled) rather than scraping Prometheus. This deletes an entire metrics
  stack from the dependency list.
- **Persistent burner pool.** Workers spawn once and read a shared duty value.
  An earlier version respawned per sample, adding ~2s of process startup to
  every 1s slice and silently destroying replay timing.
- **Absolute deadlines**, so page-touching cost does not accumulate across 2,600
  samples.
- **Pool width is fixed at the CPU limit.** Demand above the limit saturates
  every worker and the shortfall appears as throttling — the signal we want.

### `testbed/gen_manifests.py`
Emits one Pod per (VM, method) with `resources.limits` set to the scaled
recommendation **plus a fixed memory margin M = 80 MiB**. **Limits, not just
requests** — requests only influence scheduling, limits are what the kernel
enforces, and enforcement is the point.

The margin exists because the replay agent's own footprint (interpreter,
allocator arenas, the allocation transient) has to fit inside the pod alongside
the demand it is replaying. M is a single constant across the whole flavour
catalog, not a per-flavour table — see "The margin band" below for why that is
sound, and for the one qualification on it. `MEASURED_POOL_WIDTH` refuses
generation outside the pool-width-1 regime the margin was measured in.

### `testbed/stage_traces.py`
Slices each subset VM's **evaluation window only** and scales demand by S.
Handing the agent the full 30 days silently quadruples the run and tests the
wrong window.

### `testbed/plan_waves.py`
The 50-VM subset does not fit a single-node kind cluster even at S = 1/16.
This packs the subset into waves that fit the node's actual free capacity, with
headroom. The run used 5 waves; memory binds, CPU never does (peak ~29% of free
vCPU).

### `testbed/sample_cgroups.py`
Polls every replay pod's `memory.current` and `memory.peak` once a second while
a wave runs. Necessary because **an OOMKilled pod takes its own cgroup with
it** — the kubelet removes it shortly after termination, so by the time
`collect_evidence.sh` runs, the only surviving fact is the `OOMKilled` reason,
not how close to its limit the pod actually ran. Output is
`evidence/1{0..4}_wave{N}_cgroup_samples.tsv`.

### `testbed/t8_report.py`
Joins the offline predictions against the observed per-wave outcomes and emits
the confusion table, the margin-band analysis, the censoring accounting, and the
per-VM appendix as `evidence/T8_confusion.md`.

### `testbed/collect_evidence.sh`
Captures the rubric artifacts. The important ones:

| File | Proves |
|---|---|
| `01_nodes.txt` | the cluster runs (the literal rubric artifact) |
| `04_limits.txt` | the recommendation was actually enforced |
| `06_describe_oomkilled.txt` | a violation was observed, not computed |
| `logs/*.jsonl` | per-sample CPU throttling records |

## How exhaustion is enforced

| Failure mode | Enforced by | Observed as | Terminal |
|---|---|---|---|
| Memory | cgroup memory limit | `OOMKilled` in `lastState.terminated.reason` | yes |
| CPU | CFS quota from `limits.cpu` | rising `nr_throttled` / `throttled_usec` | no |

### The margin band

The offline rule compares demand against the **recommendation**. The cluster
enforces the **recommendation + M**. A VM whose peak demand lands between those
two bounds exceeds its recommendation — so the offline rule predicts a
violation — but never exceeds what the kernel enforces, so the kernel has no
reason to kill it. Its outcome is decided by M, a property of the harness, not
by the recommendation under test.

The band `budget < peak demand <= budget + M` contains **exactly 4 of the 50
VMs (297, 332, 971, 1229), and those 4 are exactly the run's 4 false
negatives.** No VM in the band was OOMKilled; no VM outside it was
misclassified. The error direction is conservative — missed violations, so the
cluster under-counts the violations right-sizing causes rather than inventing
any.

This is the criterion to use. An earlier `artifact_affected` flag, based on
which overhead constant the margin was derived from, caught only 3 of the 4.
The margin-band criterion follows from the design rather than from a measured
constant, needs no overhead estimate, and catches all four. Both columns are
retained in `results/wave{N}_outcomes.csv` for continuity.

### Page cache, and what it qualifies

`memory.current` under cgroup v2 counts reclaimable **page cache**, and the
agent reads its trace CSV through that cgroup. Two consequences, both load-
bearing:

- **`memory.peak` overstates the anonymous footprint that actually drives an
  OOM.** It is an upper bound on pressure, not a measure of live heap.
- **Reclaim is triggered by the limit**, so a cgroup with room to breathe
  accumulates footprint that the same workload under a tight limit would have
  had reclaimed out from under it. `memory.peak` therefore measures the
  workload *and the headroom it was given*. Anything measuring overhead this
  way must be measured under the limit the workload will really run with, or
  in-cluster. A standalone probe of VM 324 at `--memory=1024m` read 59.11 MiB
  of overhead; the same VM in-cluster under its real 208 MiB limit read 49.70.

This directly qualifies the **flavour-independence** result. Measured
in-cluster across five VMs spanning three flavours, the margin is 47.5–49.9 MiB
over a 4x range of buffer sizes — flat, not proportional, which is what licenses
one constant across the whole catalog. But those five pods all had slack. **A
pod pressing its limit has its overhead compressed toward the irreducible
anonymous footprint**, so 47.5–49.9 MiB is the slack-case figure, not a
universal constant. The band, not the point estimate, is what M was chosen
from.

The same mechanism shows up directly in the results: VM 297 reached **100.0% of
its limit and survived** while VMs 387 and 883 reached 100.0% and **died**.
`memory.peak == limit` does not imply an OOMKill — the OOM killer fires only
when reclaim fails, and page cache from reading the trace file is reclaimable.

### Censoring on the CPU axis

A memory violation is terminal, and the OOMKill ends the observation that would
have produced the rest of that pod's CPU evidence. The CPU rule needs 2
consecutive samples, so **a pod that dies early is censored on the CPU axis
while remaining fully valid on the memory axis.** This is structural to the two
definitions, not a defect in the testbed.

Across the run: 26 of 50 pods OOMKilled, all 26 truncated, 2 unscoreable on CPU
(fewer than 2 samples). The limiting case is VM 296, whose log holds 0 of 2,578
sample records — killed before completing a single sample. Its memory verdict is
still kernel-attested (the OOMKill *is* the measurement); it contributes no CPU
evidence at all.

A CPU confusion table over this population would be a table about the pods that
survived long enough to be observed, which is not the population of interest.
The CPU axis is therefore reported as a **limitation, not a result** — and
separately, at S = 1/16 both available CPU signals are harness artifacts.

## Scale boundary

The cluster path is a **validation subset**, not a scaled-down experiment. A
single-node kind cluster will not schedule 1,250 pods and does not need to. The
full frontier is computed offline. State this explicitly in the report so nobody
reads it as a failed attempt at scale.

It does not even schedule 50 pods at once. The run used **scale S = 1/16 and 5
sequential waves**: demand and limits are scaled by the same factor, so each
VM's demand-to-limit ratio — the quantity that decides whether a violation
fires — is preserved exactly. **No absolute capacity number is taken from the
cluster.** Every reclaimed vCPU-hour and GB-hour in this work comes from the
full-scale offline run over the 792 usable VMs
(`results/baseline_summary.csv`). The cluster answers one question: when the
offline definition says a right-sized VM violates, does the kernel agree.

## Time compression

`SLICE_SECONDS=1` replays one 5-minute trace sample per second, so a 9-day
evaluation window (2,592 samples) runs in roughly 43 minutes per wave — about
3.5 hours of wall clock across the five waves, plus teardown between them.

This compresses the schedule, not the intensity — instantaneous demand at each
sample is faithful. It does mean memory pressure duration is compressed 300x, so
a slow leak that would OOM after hours of real accumulation may not fire here.
That is a stated limitation, not a bug to fix.

## The proposed method M(eps)

A fourth policy inside `recommend()`. For each VM and resource, using the
observation window only:

1. Threshold u at a high quantile of observed demand (default 95th).
2. Generalized Pareto fit to exceedances above u.
3. Per-sample exceedance probability for capacity c:
   p(c) = (n_exceed / n_obs) * GPD_survival(c - u).
4. Probability of at least one exceedance over the evaluation horizon:
   P(c) = 1 - (1 - p(c)) ** (theta * n_eval).
5. The smallest flavour with P(c) <= eps.

theta is the extremal index. theta = 1 treats every 5-minute sample as an
independent chance to exceed; runs declustering sets theta = clusters /
exceedances, because one 40-minute spike is one event, not eight. Getting
theta wrong is the most likely reason H2's calibration would fail.

Known approximation: p(c) counts single-sample exceedances. That matches the
memory violation rule exactly, and is conservative for CPU, whose rule needs
two consecutive samples.

Everything downstream — replay, violation counting, aggregation — is the
existing tested harness, unchanged.

### The cap at the original allocation

The allocation is `min(snapped requirement, original allocation)`, per
resource.

**This follows from the data model, not from tuning.** `load_vm` clips demand
to the original allocation, because telemetry reporting above 100% utilisation
is an artefact rather than a right-sizing opportunity. Demand therefore has
bounded support at `a_orig`, and `P(demand > c) = 0` for every `c >= a_orig`.
A GPD extrapolated past `a_orig` predicts exceedance mass where the data model
permits none, so the uncapped answer contradicts the same clipping rule the
rest of the pipeline is built on. The cap is that support, expressed in the
recommender.

Two consequences worth stating in the report:

- Reclamation per resource can never go negative. At worst M(eps) recommends
  leaving the VM exactly as provisioned.
- The cap is **not** a floor on risk. It cannot manufacture a violation-free
  result, because a VM held at `a_orig` is a VM that was never shrunk — and a
  VM that was never shrunk cannot violate, since demand was clipped to
  `a_orig` on load.

**The count of capped VMs is a diagnostic, and is reported per resource per
run.** A VM hits the cap exactly when the fitted tail asks for more capacity
than the VM was ever provisioned — that is, when the extrapolation has left the
support of the distribution it was fitted to. A high capped count means the
tail model is mis-specified for that population, which is a finding about the
method, not a number to suppress.

**The GPD shape parameter is deliberately left unconstrained.** Clamping `xi`
would hide exactly the mis-specification the capped count exists to expose. Its
distribution is reported instead, including the counts above `xi = 0.5`
(infinite variance) and `xi = 1` (infinite mean).

One asymmetry to disclose: `peak` and `p95` are **not** capped, because they
are Review 1 results and are frozen. Both can exceed `a_orig` through upward
snapping (a VM provisioned at 15.93 GB whose p95 snaps to 16). Any comparison
must account for this; the counts are reported alongside the frontier.

Decided **2026-09-22, before any frontier was computed.** The eps sweep (T10)
was first run only after this rule was in place and its tests passed, so no
version of the frontier influenced the choice.
