# Review 1 — artifact index

Every rubric component, the file or screenshot that evidences it, and where that
file lives. Paths are relative to the repository root.

Assembled 2026-09-09, after the five-wave cluster run completed. Nothing in this
document is a claim; each row points at a file a reviewer can open.

## How the four components are read here

The repository does not carry a single verbatim copy of the rubric, so the
component names below are reconstructed from `docs/HYPOTHESES.txt` ("Review 1 —
Section 2: problem statement and hypothesis, 2 marks"),
`docs/STEP3_RUNBOOK.txt` ("Step 3 runbook — testbed / environment evidence, 3
marks"), and `CLAUDE.md` ("testbed evidence, 3 marks" / "baseline reproduced, 2
marks"). Component 1 is inferred; the runbook refers to a "gap table" but no
section-1 document exists in `docs/`.

If the marksheet numbers these differently, the headings can be renamed without
touching the artifact lists — the mapping from evidence to claim is unchanged.

| # | Component | Marks | Status |
|---|---|---|---|
| 1 | Problem framing and literature gap | — | **No repository artifact.** Prose in the report; the gap table has not been written into `docs/`. |
| 2 | Problem statement and falsifiable hypothesis | 2 | Complete — `docs/HYPOTHESES.txt` |
| 3 | Testbed / environment evidence | 3 | Complete — six artifacts, listed below |
| 4 | Baseline reproduced | 2 | Complete — offline run over 792 VMs plus passing tests |

---

## Component 1 — Problem framing and literature gap

No artifact in this repository. The framing prose exists in
`docs/HYPOTHESES.txt` section 1 ("The problem, restated") and in the opening of
`ARCHITECTURE.md`, but the comparison against prior work — the gap table the
runbook refers to at `docs/STEP3_RUNBOOK.txt:203` — has not been written down
anywhere under version control.

**Action for the report:** this is the one component with nothing to point at.
It is prose work, not a run, so it does not depend on any of the pipeline.

---

## Component 2 — Problem statement and falsifiable hypothesis (2 marks)

The rubric asks for the problem restated in the author's own words, converted
into a falsifiable claim of the form "Method X reduces metric M by at least N%
compared to baseline B under condition C."

| Artifact | Path | What it shows |
|---|---|---|
| Pre-registered hypotheses | `docs/HYPOTHESES.txt` | The restated problem, and H1–H3 with their pre-registered thresholds: 15% CPU / 10% memory reclamation gap, 25% / 20% zero-risk reclamation, ECE below 0.05. |
| Thresholds as enforced invariants | `CLAUDE.md` (hard invariant 2) | The thresholds are fixed before results were seen, which is what makes the claim falsifiable rather than fitted. |

The hypothesis document was written before the baselines were run and has not
been edited since. That ordering is the point of the component and is worth
stating explicitly in the report.

---

## Component 3 — Testbed / environment evidence (3 marks)

The rubric wants a screenshot or log proving the environment runs. Six artifacts
cover it. The runbook (`docs/STEP3_RUNBOOK.txt`, Part C) recommends putting
items 1, 5 and 6 in the slides and all six in the appendix.

### 3.1 — `kubectl get nodes -o wide`

| | |
|---|---|
| **Path** | `evidence/01_nodes.txt` (also `evidence/wave{1..5}/01_nodes.txt`) |
| **Shows** | `rightsizing-control-plane  Ready  control-plane  v1.33.1`, kernel `6.6.87.2-microsoft-standard-WSL2`, runtime `containerd://2.1.1`. |
| **Supporting** | `evidence/02_version.txt` — client v1.34.1, server v1.33.1, control plane reachable. |

This is the literal artifact the rubric names. A copy was captured at the start
of every wave, so the node's continuity across the whole run is on record.

### 3.2 — Preflight output on the real dataset

| | |
|---|---|
| **Path** | `evidence/02_preflight.txt` |
| **Shows** | 1,250 files found; 792 usable after four quality gates (103 bad CPU capacity, 347 below catalog floor, 2 mid-trace shift, 6 short span); median sampling interval 300 s; 8,610–23,692 samples per file; the `;`+TAB header confirmed. |
| **Backing data** | `results/dataset_report.csv` — one row per VM. |

This is what proves the real 1,250-VM trace is in hand rather than the synthetic
fixture. The exclusion cascade it reports is derived in `docs/DATA_QUALITY.md`.

### 3.3 — Image build and load

| | |
|---|---|
| **Path** | `evidence/03_image.txt` |
| **Shows** | The image present in Docker (`sha256:fdbafcda…`, built 2026-09-07T15:06:19Z), the same image loaded into the kind node's containerd (`de36bb7f…`, 46.2 MB), and a sha256 match between `/app/replay_agent.py` inside the image and `testbed/replay_agent.py` in the tree. |

**Caveat, stated because it matters:** this file was captured on 2026-09-09,
after the run, not during the build. `testbed/collect_evidence.sh` never had a
capture step for this slot — it writes `03_pods.txt` at that number — so no
contemporaneous build/load transcript exists.

What ties the artifact to the run rather than to today: `de36bb7f…` is the
`Image ID` recorded in every pod's describe output in all five waves
(`evidence/wave{1..5}/06_describe_oomkilled.txt`), so the image now sitting in
the node's containerd is demonstrably the one the pods ran. The Docker-side ID
differs from the containerd-side ID because `kind load docker-image`
re-serialises through an OCI archive and the config digest is recomputed; the
sizes agree (46,188,607 B against 46.2 MB).

### 3.4 — Pods running with enforced limits

| | |
|---|---|
| **Path** | `evidence/04_limits.txt` (per wave: `evidence/wave{1..5}/04_limits.txt`) |
| **Shows** | Per-pod `CPU_LIMIT` / `MEM_LIMIT` / `PHASE`, e.g. `rs-p95-143  p95  63m  218103808  Failed`. |
| **Supporting** | `evidence/03_pods.txt` — `kubectl get pods -l app=rightsizing-replay -o wide`, pods named and placed. |

These are `resources.limits`, not requests. Requests only steer scheduling;
limits are what the kernel enforces, and enforcement is the whole point of the
component. The limit values are the p95 recommendation scaled by S = 1/16 plus
the fixed M = 80 MiB agent margin.

### 3.5 — A pod describe showing `OOMKilled`

| | |
|---|---|
| **Path** | `evidence/06_describe_oomkilled.txt` (per wave: `evidence/wave{1..5}/06_describe_oomkilled.txt`) |
| **Shows** | `rs-p95-126`: `State: Terminated / Reason: OOMKilled / Exit Code: 137`, started 00:18:59, finished 00:32:23, against `Limits: cpu 63m`, memory 336 Mi. |
| **Machine-readable** | `evidence/05_oomkills.tsv` and `evidence/05_oomkill_count.txt` per wave; `evidence/07_pod_status.tsv` and `evidence/07_evictions.txt` distinguish OOMKills from evictions (zero evictions in every wave). |

This is the strongest single artifact in the submission: a memory exhaustion
caused by a right-sizing recommendation, killed by the kernel, not counted in a
dataframe. 26 of the 50 pods reached this state across the five waves.

`evidence/08_node_conditions.txt` records that node `MemoryPressure` never
fired — the kills were per-cgroup, so each is attributable to one VM's
recommendation rather than to node-wide contention.

### 3.6 — The T8 offline-versus-kernel table

| | |
|---|---|
| **Path** | `evidence/T8_confusion.md` |
| **Shows** | The memory confusion table over 50 VMs, the margin-band explanation of the four false negatives, the censoring accounting for the CPU axis, a per-VM appendix, and the methods paragraph. |
| **Backing data** | `results/wave{1..5}_outcomes.csv` — per-VM prediction, observation, peak demand, enforced limit, and both disagreement flags. |
| **Generator** | `testbed/t8_report.py --waves 1 2 3 4 5 --out evidence/T8_confusion.md` |

Headline: 50 pods, TP 26 / FN 4 / FP 0 / TN 20 (92.0%); excluding the four
margin-band VMs, 26 / 0 / 0 / 20 (100.0%). Comparing like with like — peak
scaled demand against the limit the kernel actually enforces — the offline rule
reproduces the kernel's verdict on **50 of 50 VMs with zero mismatches**.

**Use `excess_over_budget_mib`, not `need_mib`.** The wave outcome CSVs carry
`excess_over_budget_mib` — plain peak scaled demand minus scaled budget, no
overhead constant — and `excess_over_budget_mib > margin_mib` reproduces
`obs_oomkilled` on all 50 rows with zero mismatches. `need_mib` is retained for
provenance only: it bakes in the **superseded 59.11 MiB standalone-probe
constant**, and comparing it against `margin_mib` directly reports the four
margin-band VMs as mismatches. Full column semantics, including which columns
are superseded, are in `results/wave_outcomes_COLUMNS.md`.

The CPU axis is reported as a limitation, not a result: memory violations are
terminal, so an OOMKilled pod is censored on the CPU axis, and at S = 1/16 both
available CPU signals are harness artifacts.

**Machine-readable version:** `results/confusion_table.csv` — the flat pooled
table T8's acceptance criterion names, emitted by the same generator so it
cannot drift from the narrative. One row per (scope, population, axis,
comparison), with `disagreeing_vm_ids` listing every disagreement by id.

| Scope | Population | Axis | n | TP | FN | FP | TN |
|---|---|---|---|---|---|---|---|
| pooled | all pods | memory | 50 | 26 | 4 | 0 | 20 |
| pooled | excl. margin band | memory | 46 | 26 | 0 | 0 | 20 |
| pooled | all pods, **like-with-like** | memory | 50 | 26 | 0 | 0 | 24 |

The CPU rows in that file carry `status = CENSORED -- not a result`. They are
emitted for completeness only; the population that survived long enough to
produce CPU evidence is survivorship-selected, and at S = 1/16 both available
CPU signals are harness artifacts. Do not quote them as a result.

### Note on the top-level versus per-wave evidence files

The numbered files directly in `evidence/` (`00_`–`08_`, `99_summary.txt`) are
the **wave 1** capture, left in place from before the run was split into waves.
They are identical to `evidence/wave1/`. `evidence/99_summary.txt` accordingly
reads `pods: 14, oomkilled: 4` — those are wave 1's numbers, not the run's.

**The per-wave directories are canonical.** Any run-level total should be taken
from `evidence/T8_confusion.md` or the `results/wave{N}_outcomes.csv` files, not
from the top-level snapshot. Do not put `99_summary.txt` in the report.

---

## Component 4 — Baseline reproduced (2 marks)

Peak provisioning and the P95 heuristic run on the real trace and produce
numbers.

| Artifact | Path | What it shows |
|---|---|---|
| Summary table | `results/baseline_summary.csv`, captured to `evidence/06_baseline_summary.txt` | Both scopes x three policies, with reclamation and violation columns. |
| Per-VM results | `results/per_vm_results.csv` | One row per VM per method — the frontier behind the summary. |
| Run configuration | `results/run_config.json` | 21/9 day split, 6,048 obs / 2,592 eval samples, flavour catalog, `cpu_violation_min_run: 2`, 792 VMs used. Any number in the report traces back to this. |
| Recommendations | `results/recommendations.csv`, `results/subset_50.csv`, `results/subset_50_predictions.csv` | The bridge to the cluster, and the predictions the cluster was checked against. |
| Passing tests | `tests/test_baselines.py` (35/35), `tests/test_replay_agent.py` (3/3) | Re-run 2026-09-09, both clean. |

Headline numbers from `results/baseline_summary.csv` (792 usable VMs):

| Scope | Method | Reclaimed vCPU-h | Reclaimed GB-h | VMs violation-free |
|---|---|---|---|---|
| all VMs | peak | 259,416 (37.64%) | 795,941 (33.03%) | 92.8% |
| all VMs | p95 | 322,272 (46.76%) | 1,726,901 (71.67%) | 63.26% |
| non-idle | peak | 80,568 (20.38%) | 296,597 (18.27%) | 91.28% |
| non-idle | p95 | 120,096 (30.38%) | 1,125,605 (69.32%) | 37.06% |

The all-VMs versus non-idle gap is the number to foreground: p95 leaves 63% of
the fleet violation-free, but only 37% of the VMs that were doing any work. A
headline that includes idle machines is counting dead ones.

**Test count.** 38 tests (35 + 3), both suites clean. `CLAUDE.md` and `TASKS.md`
previously specified 16/16 — a figure that went stale as the suites grew during
the data-quality and margin investigations. Both now state the invariant as
"passes in full, and never shrinks" with the current count as a dated
observation, so the number cannot go stale again. The rule that matters is
unchanged: never edit a test to make it pass.

---

## Reproducing any of this

```
make test                # 38 tests, all passing (35 + 3)
make inspect             # -> evidence/02_preflight.txt, results/dataset_report.csv
make baselines           # -> results/baseline_summary.csv, per_vm_results.csv, run_config.json
make recommend           # -> results/recommendations.csv, subset_50*.csv
make cluster && make image
                         # per wave: stage, apply, sample cgroups, collect
python3 testbed/t8_report.py --waves 1 2 3 4 5 \
    --out evidence/T8_confusion.md --csv-out results/confusion_table.csv
```

The cluster (`rightsizing-control-plane`) was still running at the time this
index was assembled, which is what allowed `evidence/03_image.txt` to be
captured against the live node rather than reconstructed.

---

This trace was graciously provided by Bitbrains IT Services Inc.
