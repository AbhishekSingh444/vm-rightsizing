# Tasks

Ordered work queue for steps 3 and 4. Each task has acceptance criteria — a task
is not done until the criteria are checkable by someone else.

Work top to bottom. Do not start a task whose predecessor is incomplete.

---

## T1 — Preflight the real dataset

**Why first:** every downstream assumption depends on the trace's actual shape,
including the observation/evaluation split baked into the pre-registered
hypotheses.

Download the fastStorage trace from
`http://gwa.ewi.tudelft.nl/datasets/gwa-t-12-bitbrains` and unpack it to
`data/fastStorage/`.

```
make inspect
```

Then run it on the full 1,250 files. If any file fails to parse, fix the loader
so it handles that file's format and re-run until every file either parses or
you can state exactly why it cannot. **Do not change the reported statistics to
make the output look tidier.**

**Acceptance:**
- `results/dataset_report.csv` exists with one row per usable VM.
- Console output captured to `evidence/02_preflight.txt`.
- If the median span is not ~30 days, or the interval is not 300s, you have
  **stopped and reported** rather than proceeding. This changes the split and
  therefore the hypotheses.

---

## T2 — Baselines on the real trace

```
make test          # must pass in full first
make baselines
```

Use the observation and evaluation day counts that T1 actually reported, not
the defaults.

**Acceptance:**
- `results/per_vm_results.csv`, `results/baseline_summary.csv`,
  `results/run_config.json` all exist and are non-empty.
- The summary table is captured to `evidence/06_baseline_summary.txt`.
- You have reported, in prose: total reclaimed vCPU-hours and GB-hours per
  method, the percentage of VMs left violation-free, and the all-VMs versus
  non-idle gap.
- `make test` still passes in full, with no test fewer than before (38 as of
  2026-09-09; the suite only grows).

**Watch for:** if `peak` produces zero violations on the real trace, that is
suspicious — memory demand normally drifts enough that the evaluation-window
maximum exceeds the observation-window maximum for some VMs. Investigate before
accepting it.

---

## T3 — Recommendations and stratified subset

```
make recommend
```

**Acceptance:**
- `results/recommendations.csv` has columns
  `vm_id,method,rec_cores,rec_mem_gb` and covers every VM and method.
- `results/subset_50.csv` contains 50 VMs, roughly half predicted to violate.
- `results/subset_50_predictions.csv` exists — this is the file the cluster run
  will be checked against.
- If fewer than 25 VMs are predicted to violate under p95, report that rather
  than silently producing an unbalanced subset.

---

## T4 — Cluster up

```
make cluster
kubectl get nodes -o wide
```

Follow `testbed/README.md`. If any command fails, fix it and record what you
changed and why in `ENVIRONMENT_NOTES.md`.

If `kind` will not run, fall back in this order: k3s, minikube, a free-tier
managed cluster, and only then a CloudSim Plus build log. The manifests are
portable across the first three; only the `/traces` hostPath mount needs
adjusting.

**Acceptance:**
- `kubectl get nodes -o wide` shows a Ready node, captured to
  `evidence/01_nodes.txt`.
- `ENVIRONMENT_NOTES.md` records the runtime used and anything that had to
  change.

---

## T5 — Image build and load

```
make image
```

`kind` nodes do not see the host Docker daemon's images, so the `kind load` step
is not optional.

**Acceptance:**
- `docker images | grep rightsizing` shows the image.
- Build and load output captured to `evidence/03_image.txt`.

---

## T6 — Stage traces and run the replay

Stage the evaluation-window slice for the 50 subset VMs into `data/traces/` as
`<vm_id>.csv`. **Evaluation window only** — the agent replays what it is given,
and handing it the full 30 days silently makes the run four times longer and
tests the wrong window.

```
python3 testbed/gen_manifests.py results/subset_50.csv \
    --method p95 --out testbed/manifests/p95
kubectl apply -f testbed/manifests/p95/
kubectl get pods -l app=rightsizing-replay -o wide
```

Expect roughly 45 minutes at `SLICE_SECONDS=1`.

**Acceptance:**
- 50 pods created; the phase of each recorded.
- At least one pod reaches a terminal state (Completed or OOMKilled).
- If zero pods OOMKill, report it. Either the subset stratification failed, the
  memory demand replay is not faithful, or the offline violation definition
  disagrees with the kernel — all three are worth knowing.

---

## T7 — Capture evidence

```
make evidence
```

**Acceptance:** `evidence/` contains all of `01_nodes.txt`, `02_version.txt`,
`03_pods.txt`, `04_limits.txt`, `05_oomkills.tsv`, `06_describe_oomkilled.txt`,
`99_summary.txt`, and a non-empty `logs/` directory. Any missing or empty file
is reported, not silently accepted.

---

## T8 — Offline versus kernel confusion table

**This is the task that could become a paper contribution.** Everything above is
plumbing; this is the check nobody in the literature performs.

For the 50 subset VMs, compare the offline prediction in
`results/subset_50_predictions.csv` against what the cluster actually did:

- Memory: `OOMKilled` in `evidence/05_oomkills.tsv`
- CPU: `throttled_periods > 0` in `evidence/logs/*.jsonl`

Write `src/compare_offline_vs_kernel.py` producing a 2x2 confusion table
(predicted violate / predicted safe against observed violate / observed safe),
separately for CPU and memory, plus a list of every disagreeing VM.

**Acceptance:**
- `results/confusion_table.csv` and a readable console summary.
- Every disagreement is listed by VM id with both the prediction and the
  observation.
- You have written one paragraph on what the disagreements mean. If the offline
  count systematically exceeds the kernel count, the offline definition is too
  strict; if the reverse, the replay is not faithfully reproducing demand.
  Either way, say which and why.

---

# Phase 2 — the proposed method M(eps)

T9 and T10 were executed before this file recorded them; their full write-ups
are in `ENVIRONMENT_NOTES.md` under the matching headings. They are restated
here in one line each so the queue reads in order.

## T9 — Implement M(eps)  (done 2026-09-22)

Peaks-over-threshold recommender in `src/baselines.py`, opt-in behind `--evt`,
capped at the original allocation. Tests in `tests/test_evt.py`.

## T10 — eps sweep and comparative frontier  (done 2026-09-22, theta = 1)

`src/sweep_eps.py`, `src/plot_frontier.py`, `make sweep`. Reclamation reported
beside **measured** violation fraction, with the baselines as rows on the same
axes and a dominance test against both the uncapped and the capped baselines.

**Result to carry forward:** H1 is **not evaluable at matched risk** under
theta = 1. The M(eps) curve never reaches p95's measured violation rate on any
scope x resource combination, on either the snapped or the unsnapped arm.

---

## T11 — Estimate the extremal index per VM

**Why:** T10's horizon formula treats a 9-day window as `n_eval` independent
chances to exceed. Five-minute utilisation samples are strongly
autocorrelated, so that overstates exposure. theta is the correction.

**Method — decided before any T11 result existed.**

- **Primary: the intervals estimator (Ferro and Segers, 2003.)** Chosen
  because it has **no free parameter** — it is a function of the
  interexceedance times alone, so there is nothing in it that could be tuned,
  accidentally or otherwise, against the evaluation window (invariant 4).
- **Sensitivity check: runs declustering**, with the run length **fixed at 12
  samples (one hour)**. Runs declustering does have a free parameter, which is
  precisely why it is secondary. **The run length was fixed at 12 before any
  T11 result existed** — recorded in `EVT_DEFAULT_RUN_LENGTH` in
  `src/baselines.py` with the same statement, and asserted in
  `tests/test_evt.py`. Choosing it after seeing a frontier would make it a
  fitted parameter.
- theta is estimated **per VM and per resource, on the observation window
  only**, at the same threshold u the GPD was fitted over. A VM whose
  estimator cannot return a value takes theta = 1, which is the conservative
  direction (more effective trials, larger allocation), and is counted.

```
make theta                 # intervals, snapped + unsnapped, + figures
make theta-sensitivity     # runs declustering at run length 12
```

**Acceptance:**
- `results/theta_intervals/{frontier,dominance,theta_per_vm,eps_calibration,
  exploratory_matched_risk,gpd_shapes,sweep_config}.csv|json` exist and are
  non-empty, and the same under `results/theta_intervals_unsnapped/` and
  `results/theta_runs/`.
- Console output captured to `evidence/10_frontier_theta_intervals.txt`,
  `11_frontier_theta_intervals_unsnapped.txt`,
  `12_frontier_theta_runs.txt`.
- The per-VM theta distribution is reported per resource, including how many
  VMs could not be estimated.
- eps target against measured risk is reported across the whole sweep.
- `--theta-method fixed` still reproduces the T10 frontier exactly, so the
  T12 ablation compares two arms of one code path.
- `make test` passes in full and the suite has not shrunk.

**Result:** per-VM theta lowers the median effective trial count from 2,592 to
1,456 on memory (1.8x) and 660 on CPU (3.9x), moves the frontier up and to the
right,
and **does not make H1 evaluable** — the curve still never reaches p95's
measured risk. See `ENVIRONMENT_NOTES.md`, "T11".

---

## T12 — Ablation

Isolate what each component of M(eps) contributes. Each arm is one switch
flipped against the same pipeline, reported on the same two axes.

**The measured quantity is the shift in reclamation at matched measured risk
against `peak_capped`** — not reclamation alone. An arm that reclaims more
because it takes more risk has gained nothing, and reporting reclamation on
its own is the exact failure this project exists to correct.

```
make ablation
```

Ablation list:

- **dependence correction on/off** — per-VM theta (T11) against theta = 1
  (T10).
- **estimator** — intervals against runs declustering at the fixed run
  length.
- **snapping on/off** — catalog flavours against continuous allocations
  (diagnostic only; never a reported policy).
- **cap on/off** — allocation `min(requirement, a_orig)` against the uncapped
  requirement. The cap is doing a great deal of work (at eps = 0.01 it moved
  fleet reclamation from -100.3% to +30.5% on memory), so the ablation has to
  say how much of M(eps)'s reported position is the cap rather than the tail
  model. The baselines must be capped too in that arm, or the comparison is
  not like-for-like.
- **threshold quantile** — u at q0.95 against neighbouring choices.

**Acceptance:**
- `results/ablation/ablation_matched_risk.csv` exists, with one row per
  arm x scope x resource and a `shift_vs_reference_pp` column.
- `results/ablation/frontier_<arm>.csv` for every arm, plus
  `ablation_theta.csv` and `ablation_config.json`.
- `figures/ablation_cpu.png` and `figures/ablation_mem.png`, arms overlaid.
- Console output captured to `evidence/13_ablation.txt`.
- The `reference` arm reproduces `results/theta_intervals/frontier.csv` and
  the `theta_off` arm reproduces `results/frontier.csv`, exactly — otherwise
  the arms are not the same code path as the headline runs.
- Arms whose comparator moves (`snap_off`, `cap_off`) are flagged in the
  table, not silently compared.
- `make test` passes in full and the suite has not shrunk.

**Result:** the **cap is the whole gain**. Removing it costs 47 to 68
percentage points of matched-risk reclamation; no other switch moves the
answer by more than 1.9 pp. The dependence correction (T11) has a *negative*
shift of 0.16 to 0.93 pp — it bought its extra reclamation entirely with
risk. Threshold choice is the only other switch with real sign: u at q0.98
gains 5.3 pp on memory (all VMs) but loses 1.1 pp on CPU. See
`ENVIRONMENT_NOTES.md`, "T12".
**Corrected after T16:** the theta_off shift and the q0.98 CPU loss are both
inside intervals that contain zero. Use the wording in `ENVIRONMENT_NOTES.md`,
"Corrections to Review 2 claims", not this paragraph.

**Carried forward from the T11 diagnostic, and not fixable by any arm:** all
21 memory violators at eps = 0.001 have evaluation-window peaks above their
observation-window peaks, and 18 of them violate above the finite upper
endpoint of their own fitted GPD. The low end fails because demand drifted
between the windows, not because the tail was modelled loosely. No switch in
this ablation addresses non-stationarity.

---

# Phase 3 — Review 3 (Monday 26 October 2026)

Decisions taken 2026-10-22, before any T15-T20 result existed:

- **Second workload is GWA-T-12 Rnd, not Azure.** Azure was the
  pre-registered replication set (`docs/HYPOTHESES.txt`, condition C). It was
  checked and has no memory demand time series, so it cannot replicate any
  memory claim. The paper says so.
- **Repeats are offline only.** Section 3 stays closed -- no new cluster runs.
  Bootstrap (T16) plus multi-split (T17) is the evidence.
- **Drift-aware sizing is dropped.** Anything built now is post hoc and cannot
  test the pre-registered hypotheses. T17 carries the drift finding.
- **Paper is Markdown in the repo, exported to PDF.**

Priority if time runs short: T15, T16, T19, then T20, then T17. T18 last --
drop Rnd entirely rather than compromise the others, and state in the paper
that replication is pending, with the reason.

**Schedule, revised 2026-10-22:** T18 is deferred, not dropped -- "multiple
workloads" is named in the Review 3 rubric. T19 on Thu 22 Oct, T20 draft on
Fri 23 Oct, T18 (Rnd) on Sat 24 Oct if the draft is on track. If it is not,
the fallback above applies and the paper states replication as pending.

Stop and report after each task. Propose a commit message; never commit.

---

## T15 — Close the pre-registered reporting gap  (done 2026-10-22)

**Why:** H1 and H2 were reported, H3 was not, and H2 was reported through a
descriptive ratio (29x) rather than its pre-registered statistic. A hypothesis
whose named statistic is never computed has not been tested.

- **H3** against its 25% vCPU-hour / 20% GB-hour thresholds: the largest
  fleet reclamation, relative to B0, among the evaluated recommenders that
  record **zero** violation events in W_eval. Reported per resource, all VMs
  and non-idle, with a plain SUPPORTED / NOT SUPPORTED verdict whatever it is.
- **H2**'s named statistic: expected calibration error over the pre-registered
  sweep eps in {0.001, 0.005, 0.01, 0.05, 0.10}, and, for each point, whether
  the observed violating fraction lies inside the 95% binomial interval of
  eps. Verdict against the pre-registered null (outside at two or more points,
  or ECE >= 0.05).
- Post-processing of existing frontier CSVs only. No threshold is touched.

```
make hypotheses
```

**Acceptance:**
- `results/hypotheses/{h2_calibration,h2_summary,h3_zero_risk}.csv` exist and
  are non-empty.
- Console output captured to `evidence/15_hypotheses.txt`.
- H2 and H3 each have a one-line verdict in `ENVIRONMENT_NOTES.md`, stated as
  plainly as H1's.
- The ECE definition used is written down next to the number.
- `make test` passes in full and the suite has not shrunk.

**Result:** **H2 NOT SUPPORTED** -- 3 (CPU) or 4 (memory) of 5 points lie
outside their binomial interval; ECE 0.025-0.029 passes its bound, but the
bound cannot reject on this sweep (a never-violating method scores 0.033).
**H3 NOT SUPPORTED** -- only B0 has zero violations, so zero-risk reclamation
is 0%. See `ENVIRONMENT_NOTES.md`, "T15".

---

## T16 — Bootstrap over VMs, fixed and exposed `--seed`  (done 2026-10-22)

**Why:** every number so far is a point estimate on one fleet. Without an
interval there is no way to tell a 0.7 pp ablation shift from noise.

- Resample VMs with replacement (the VM is the unit; samples within a VM are
  autocorrelated and are never resampled individually). Fixed default seed,
  exposed as `--seed`, recorded in `run_config.json`.
- 95% percentile intervals on: reclamation, violation fraction, the
  matched-risk gaps against `peak_capped`, and every T12 ablation shift.
- Wilson 95% intervals on every violation fraction.
- Every figure redrawn with bands or error bars.

**Acceptance:**
- `results/bootstrap/` holds interval tables for frontier, matched-risk gaps
  and ablation shifts, plus `run_config.json` carrying the seed and resample
  count.
- Re-running with the same `--seed` reproduces the intervals exactly; a
  different seed moves them only in the last reported digit or two.
- Every figure under `figures/` used in the paper shows intervals.
- Console output captured to `evidence/16_bootstrap.txt`.
- `make test` passes in full and the suite has not shrunk.

**Result:** B = 10,000, seed 16, VM as the unit, paired across arms. M(eps)
reclaims **less** than `peak_capped` at matched risk, with intervals excluding
zero (all VMs: CPU -5.56 [-11.07, -2.47], memory -6.30 [-9.18, -3.45]). The
cap remains the whole gain (-47 to -68 pp, every interval 30+ pp below zero).
**The 0.7 pp theta_off shift is not resolvable:** on CPU (all VMs) the
theta = 1 curve misses `peak_capped`'s risk in 40% of resamples, and the one
cell defined on every seed-16 resample is +0.16 [-0.79, 0.96]. Same seed
reproduces exactly; seed 17 moves endpoints by <= 0.4 pp except `cap_off`
(up to 1.3 pp on 35-70 pp wide intervals), and flips two cells from defined to
1-in-10,000 undefined. u = q0.98's memory gain is real
(+5.26 [3.40, 7.62]); its CPU loss is not distinguishable from zero.
Undefined-gap shares are reported for every cell, and intervals only where
the share is zero. See `ENVIRONMENT_NOTES.md`, "T16".

---

## T17 — Multi-split sensitivity analysis  (done 2026-10-22)

**Why:** the T12 diagnostic says the low end fails on drift. One split cannot
say whether that is a property of the method or of the particular 21/9 days.

- Several observation/evaluation splits, mean +- sd across them for
  reclamation, violation fraction, H2's ECE and the matched-risk gaps.
- **Labelled a sensitivity analysis.** `docs/HYPOTHESES.txt` pre-registers a
  single 21/9 split; that stays the headline and is not replaced.
- **Window lengths, origins and overlap are proposed to the user and approved
  before anything runs.**

**Acceptance:**
- Approved split plan recorded in `ENVIRONMENT_NOTES.md` before the first run.
- `results/multisplit/` with per-split frontiers and a summary table.
- Console output captured to `evidence/17_multisplit.txt`.
- `make test` passes in full and the suite has not shrunk.

**Result:** families A (14/9, origins 0, 7/3, 14/3, 7) and B (7/14/21-day
observation, eval days 21-30) as approved; `make multisplit`. The headline
reproduces both published frontiers byte-identically. The matched-risk gap
against `peak_capped` is negative in every split and every cell, and **H2 is
NOT SUPPORTED in every split**. The low end is 19-30x anti-conservative in
every split, so its failure is not specific to days 21-30. **Every eps = 0.001
violator in every split has an eval peak above its obs peak, but that is close
to an identity**: the allocation already covers the observation peak on all
but 1-3 VMs. T12's "21 of 21 drift" has the same weakness. See
`ENVIRONMENT_NOTES.md`, "T17", for the wording that survives.

---

## T18 — Replication on GWA-T-12 Rnd  (deferred to Sat 24 Oct, not dropped)

Download Rnd, preflight it, run the data-quality gates and the full offline
pipeline (baselines, sweep, theta, hypotheses, bootstrap).

**Acceptance:**
- Rnd lives under `data/rnd/` (gitignored), never mixed with fastStorage.
- Preflight captured to `evidence/18_rnd_preflight.txt`, including the span
  and sampling interval. If the span is not ~30 days, **stop and report**.
- A table of every data-quality gate with its exclusion count on fastStorage
  beside Rnd, and a sentence on each gate that behaves differently.
- Pipeline outputs under `results/rnd/`.
- `make test` passes in full and the suite has not shrunk.

---

## T19 — `make demo`, under two minutes  (done 2026-10-22)

Synthetic traces through baselines, M(eps) sweep, and a frontier plot.

- The default path is offline and standalone: no Docker, no kind, no dataset.
- `make demo-kind` is optional and separate: it OOMKills one pod on screen.
  If Docker misbehaves, the two-minute path still works.

**Acceptance:**
- `make demo` from a fresh shell finishes in under 120 s wall clock on this
  laptop, and the time is recorded.
- It produces a frontier PNG and prints the frontier table.
- `make demo-kind` shows one pod reaching `OOMKilled` in `kubectl get pods`.
- `make test` passes in full and the suite has not shrunk.

**Result:** `make demo` takes 31 s from a fresh shell (`evidence/19_demo.txt`)
and writes `demo_out/frontier_demo.png`. `make demo-kind` takes 22.6 s, and
`rs-demo-oom` reaches `OOMKilled` with exit code 137
(`evidence/19_demo_kind.txt`). Suite: 117/117 passed. `plot_frontier.py`
gained `--source`, so the synthetic figure no longer credits Bitbrains. The
default is unchanged and checked byte-identical. Surprise: the 32 to 64 MiB
step logged CPU throttling at 0.1 core of demand, which is page-touch cost.
See `ENVIRONMENT_NOTES.md`, "T19".

---

## T20 — Paper draft, 6-8 pages

Markdown in the repo (`paper/paper.md`), exported to PDF.

Sections: abstract; related work (from the gap table); method; evaluation with
the T16 intervals; limitations (from "Threats to validity" in
`ENVIRONMENT_NOTES.md` and the testbed-scale section); references; and the
acknowledgement line "This trace was graciously provided by Bitbrains IT
Services Inc."

**Acceptance:**
- `paper/paper.md` and `paper/paper.pdf` exist; the PDF is 6-8 pages.
- Every number in the paper traces to a file under `results/` or `evidence/`.
- H1, H2 and H3 are each reported with a verdict, including the ones that
  fail.
- Azure is recorded as the pre-registered replication set, checked and found
  unable to replicate any memory claim. If T18 was dropped, replication is
  stated as pending, with the reason.

---

## Not in scope yet

Do not start these without asking:

- Any change to the pre-registered hypothesis thresholds.
- Re-running the cluster testbed. Section 3 is closed.
- Drift-aware sizing (dropped 2026-10-22; post hoc).
