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

## Not in scope yet

Do not start these without asking:

- Implementing the EVT recommender `M(eps)`. That is the next phase, and
  `ARCHITECTURE.md` describes where it slots in.
- The Azure replication dataset.
- Any change to the pre-registered hypothesis thresholds.
