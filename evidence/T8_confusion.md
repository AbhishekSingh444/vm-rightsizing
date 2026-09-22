# T8 -- offline violation definition vs kernel enforcement

Cluster run of the p95 right-sizing baseline over 50 VMs, 5 sequential waves, scale S = 1/16, fixed memory margin M = 80 MiB.


## 1. Pooled memory confusion

Offline prediction is `mem_violation` from `src/baselines.py` (demand exceeds allocation at any single sample). Observed is the kernel's own cgroup OOM killer.

| population | n | TP | FN | FP | TN | accuracy |
|---|---|---|---|---|---|---|
| all pods | 50 | 26 | 4 | 0 | 20 | 92.0% |
| excluding artifact-affected (overhead-constant criterion) | 47 | 26 | 1 | 0 | 20 | 97.9% |
| excluding margin-band (recommended) | 46 | 26 | 0 | 0 | 20 | 100.0% |
| margin-band only | 4 | 0 | 4 | 0 | 0 | 0.0% |

Per wave, excluding margin-band VMs:

| wave | n | TP | FN | FP | TN |
|---|---|---|---|---|---|
| 1 | 11 | 4 | 0 | 0 | 7 |
| 2 | 8 | 5 | 0 | 0 | 3 |
| 3 | 9 | 4 | 0 | 0 | 5 |
| 4 | 9 | 7 | 0 | 0 | 2 |
| 5 | 9 | 6 | 0 | 0 | 3 |

### Why the false negatives are structurally undecidable

The offline rule compares demand against the **recommendation**. The cluster enforces the **recommendation plus the agent margin M**, because the replay agent's own footprint has to fit inside the pod alongside the replayed demand. A VM whose peak demand lands between those two bounds exceeds its recommendation -- so the offline rule predicts a violation -- but never exceeds what the kernel actually enforces, so the kernel has no reason to kill it. Its outcome is decided by M, a property of the harness, not by the recommendation under test.

| vm_id | wave | scaled budget MiB | peak demand MiB | enforced limit MiB | observed |
|---|---|---|---|---|---|
| 297 | 5 | 128 | 190.29 | 208.0 | survived |
| 332 | 1 | 128 | 151.55 | 208.0 | survived |
| 971 | 1 | 128 | 155.31 | 208.0 | survived |
| 1229 | 1 | 256 | 279.55 | 336.0 | survived |

All 4 are false negatives, and they are the ONLY false negatives in the run. Comparing like with like -- peak scaled demand against the limit the kernel actually enforces -- the agreement is exact:

> Across all 50 VMs, `peak scaled demand > enforced limit` predicts the kernel's OOMKill with **0 mismatches**.


So the testbed does not show the offline definition disagreeing with the kernel. It shows that a margin added for harness reasons shifts the enforced threshold, and that the four VMs sitting in the shifted gap cannot be adjudicated here. The error direction is conservative: they are missed violations, so the cluster under-counts the violations that right-sizing causes rather than inventing any.


An earlier, narrower criterion flagged only 3 of these 4 (`artifact_affected`, based on which overhead constant the margin was derived from). It is retained in the per-VM CSVs for continuity but is superseded by the margin-band criterion, which follows from the design rather than from a measured constant and catches all four.


## 2. Censoring accounting

A memory violation is terminal: the OOMKill ends the observation that would have produced the rest of that pod's CPU evidence. The CPU rule (2 consecutive samples) needs the observation to continue, so a pod that dies early is censored on the CPU axis while remaining fully valid on the memory axis. This is structural to the two definitions, not a defect in the testbed.

| wave | pods | OOMKilled | truncated | unscoreable (<2 samples) | median % of trace observed, OOMKilled pods |
|---|---|---|---|---|---|
| 1 | 14 | 4 | 4 | 1 | 15.4% |
| 2 | 8 | 5 | 5 | 0 | 14.9% |
| 3 | 9 | 4 | 4 | 0 | 75.6% |
| 4 | 9 | 7 | 7 | 1 | 5.3% |
| 5 | 10 | 6 | 6 | 0 | 4.7% |
| **all** | **50** | **26** | **26** | **2** | |

The limiting case is VM **296** (wave 4), whose log contains 0 sample records of 2578: it was OOMKilled before completing a single sample. Its memory verdict is nonetheless kernel-attested and sound -- the OOMKill *is* the measurement -- but it contributes no CPU evidence at all. Reporting a CPU confusion table over a population censored this way would be reporting on the pods that survived long enough to be observed, which is not the population of interest.


## 3. Per-VM appendix

| wave | vm_id | flavour (vCPU/GB) | scaled budget MiB | scaled limit MiB | peak demand MiB | predicted mem_violation | observed | peak memory.current MiB | peak % of limit | margin_band | artifact_affected |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 126 | 1/4 | 256 | 336.0 | 651.26 | True | OOMKilled | 336.0 | 100.0% |  |  |
| 1 | 142 | 1/4 | 256 | 336.0 | 250.54 | False | Completed | 300.0 | 89.3% |  |  |
| 1 | 143 | 1/2 | 128 | 208.0 | 2771.63 | True | OOMKilled | -- | -- |  |  |
| 1 | 215 | 8/8 | 512 | 592.0 | 1189.55 | True | OOMKilled | -- | -- |  |  |
| 1 | 324 | 1/2 | 128 | 208.0 | 125.95 | False | Completed | 175.6 | 84.4% |  |  |
| 1 | 332 | 1/2 | 128 | 208.0 | 151.55 | True | Completed | 169.1 | 81.3% | yes | yes |
| 1 | 340 | 1/2 | 128 | 208.0 | 269.99 | True | OOMKilled | 208.0 | 100.0% |  |  |
| 1 | 344 | 1/8 | 512 | 592.0 | 494.93 | False | Completed | 544.4 | 92.0% |  |  |
| 1 | 643 | 4/8 | 512 | 592.0 | 0.0 | False | Completed | 19.9 | 3.4% |  |  |
| 1 | 697 | 1/2 | 128 | 208.0 | 33.11 | False | Completed | 75.1 | 36.1% |  |  |
| 1 | 951 | 1/2 | 128 | 208.0 | 106.5 | False | Completed | 156.4 | 75.2% |  |  |
| 1 | 971 | 2/2 | 128 | 208.0 | 155.31 | True | Completed | 205.2 | 98.7% | yes | yes |
| 1 | 987 | 1/2 | 128 | 208.0 | 68.27 | False | Completed | 118.2 | 56.8% |  |  |
| 1 | 1229 | 1/4 | 256 | 336.0 | 279.55 | True | Completed | 327.0 | 97.3% | yes | yes |
| 2 | 248 | 8/16 | 1024 | 1104.0 | 2333.01 | True | OOMKilled | 1104.0 | 100.0% |  |  |
| 2 | 289 | 8/16 | 1024 | 1104.0 | 1960.62 | True | OOMKilled | 1104.0 | 100.0% |  |  |
| 2 | 369 | 1/2 | 128 | 208.0 | 488.79 | True | OOMKilled | 208.0 | 100.0% |  |  |
| 2 | 371 | 1/8 | 512 | 592.0 | 481.28 | False | Completed | 529.2 | 89.4% |  |  |
| 2 | 593 | 2/4 | 256 | 336.0 | 1900.54 | True | OOMKilled | 336.0 | 100.0% |  |  |
| 2 | 645 | 4/8 | 512 | 592.0 | 0.0 | False | Completed | 21.1 | 3.6% |  |  |
| 2 | 820 | 1/2 | 128 | 208.0 | 314.71 | True | OOMKilled | 208.0 | 100.0% |  |  |
| 2 | 976 | 1/2 | 128 | 208.0 | 64.0 | False | Completed | 113.6 | 54.6% |  |  |
| 3 | 46 | 1/2 | 128 | 208.0 | 67.33 | False | Completed | 116.5 | 56.0% |  |  |
| 3 | 258 | 8/16 | 1024 | 1104.0 | 2365.44 | True | OOMKilled | 1104.0 | 100.0% |  |  |
| 3 | 292 | 8/16 | 1024 | 1104.0 | 2278.4 | True | OOMKilled | 1104.0 | 100.0% |  |  |
| 3 | 321 | 1/2 | 128 | 208.0 | 94.46 | False | Completed | 144.2 | 69.3% |  |  |
| 3 | 623 | 4/8 | 512 | 592.0 | 0.0 | False | Completed | 22.8 | 3.9% |  |  |
| 3 | 654 | 4/8 | 512 | 592.0 | 0.0 | False | Completed | 22.8 | 3.9% |  |  |
| 3 | 903 | 1/2 | 128 | 208.0 | 386.18 | True | OOMKilled | 208.0 | 100.0% |  |  |
| 3 | 981 | 1/2 | 128 | 208.0 | 81.75 | False | Completed | 130.2 | 62.6% |  |  |
| 3 | 1230 | 1/2 | 128 | 208.0 | 251.9 | True | OOMKilled | 208.0 | 100.0% |  |  |
| 4 | 185 | 8/8 | 512 | 592.0 | 1182.72 | True | OOMKilled | 592.0 | 100.0% |  |  |
| 4 | 194 | 8/8 | 512 | 592.0 | 1177.6 | True | OOMKilled | 592.0 | 100.0% |  |  |
| 4 | 269 | 8/16 | 1024 | 1104.0 | 1915.56 | True | OOMKilled | 1104.0 | 100.0% |  |  |
| 4 | 296 | 1/2 | 128 | 208.0 | 316.07 | True | OOMKilled | 208.0 | 100.0% |  |  |
| 4 | 349 | 1/2 | 128 | 208.0 | 768.0 | True | OOMKilled | 208.0 | 100.0% |  |  |
| 4 | 626 | 4/8 | 512 | 592.0 | 0.0 | False | Completed | 23.6 | 4.0% |  |  |
| 4 | 817 | 1/2 | 128 | 208.0 | 753.66 | True | OOMKilled | 208.0 | 100.0% |  |  |
| 4 | 942 | 2/8 | 512 | 592.0 | 712.7 | True | OOMKilled | 592.0 | 100.0% |  |  |
| 4 | 954 | 1/2 | 128 | 208.0 | 68.44 | False | Completed | 118.8 | 57.1% |  |  |
| 5 | 189 | 8/8 | 512 | 592.0 | 1219.41 | True | OOMKilled | 592.0 | 100.0% |  |  |
| 5 | 199 | 8/8 | 512 | 592.0 | 1134.08 | True | OOMKilled | 592.0 | 100.0% |  |  |
| 5 | 273 | 8/16 | 1024 | 1104.0 | 1955.16 | True | OOMKilled | 1104.0 | 100.0% |  |  |
| 5 | 297 | 1/2 | 128 | 208.0 | 190.29 | True | Completed | 208.0 | 100.0% | yes |  |
| 5 | 348 | 1/4 | 256 | 336.0 | 709.29 | True | OOMKilled | 336.0 | 100.0% |  |  |
| 5 | 387 | 1/2 | 128 | 208.0 | 271.7 | True | OOMKilled | 208.0 | 100.0% |  |  |
| 5 | 641 | 4/8 | 512 | 592.0 | 0.0 | False | Completed | 24.5 | 4.1% |  |  |
| 5 | 883 | 1/2 | 128 | 208.0 | 251.9 | True | OOMKilled | 208.0 | 100.0% |  |  |
| 5 | 980 | 2/2 | 128 | 208.0 | 108.54 | False | Completed | 158.7 | 76.3% |  |  |
| 5 | 1228 | 1/4 | 256 | 336.0 | 219.14 | False | Completed | 237.6 | 70.7% |  |  |

Flavour catalog: vCPU [1,2,4,8,16], memory [2,4,8,16,32,64] GB, snapped upward. Scaled budget is the p95 memory recommendation at S = 0.0625; scaled limit is that budget plus the fixed M = 80 MiB agent margin, which is what the pod's cgroup actually enforces. A blank peak means the pod's cgroup was removed before the sampler could read it.


## 4. Methods

The Kubernetes testbed validates the *offline violation definition* against kernel enforcement; it does not produce a reclamation figure. Each VM in a stratified 50-VM subset is replayed as a pod whose cgroup limits encode the p95 right-sizing recommendation, so that a memory violation under the offline definition in `src/baselines.py` (demand exceeds allocation at any single sample) should coincide with the kernel's own OOM killer firing. The comparison is the confusion table in section 1. Because the subset's aggregate footprint exceeds a single-node kind cluster even at S = 1/16, the run is split into 5 sequential waves; demand and limits are scaled by the same factor, so each VM's demand-to-limit ratio -- the quantity that determines whether a violation fires -- is preserved exactly. **No absolute capacity number is taken from the cluster.** All reclaimed vCPU-hours and GB-hours reported in this work come from the full-scale offline run over the 792 usable VMs of the GWA-T-12 Bitbrains fastStorage trace (`results/per_vm_results.csv`, `results/baseline_summary.csv`). The cluster answers one question only: when the offline definition says a right-sized VM violates, does the kernel agree. On the memory axis it does. The CPU axis is not validated at S = 1/16 -- both available CPU signals are harness artifacts at this scale (see ENVIRONMENT_NOTES.md), and the CPU column is therefore reported as a limitation rather than as a result.


This trace was graciously provided by Bitbrains IT Services Inc.
