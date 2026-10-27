# Column semantics — `results/wave{1..5}_outcomes.csv`

One row per pod in the wave; 50 rows across the five files. Written by
`testbed/wave_report.py`. All memory figures are **MiB at scale S = 1/16**,
which is the scale the cluster actually ran at — no column here is a
full-scale number.

## Read this first: two columns that look interchangeable and are not

| Column | Contains | Use it for |
|---|---|---|
| `excess_over_budget_mib` | peak scaled demand − scaled budget. **No overhead constant.** | Everything. This is the column to derive results from. |
| `need_mib` | peak scaled demand **+ 59.11** − scaled budget | Provenance only. Do not derive results from it. |

The 59.11 MiB inside `need_mib` is the **superseded** standalone-probe overhead
constant. It was measured under a generous `--memory=1024m` limit, and
`memory.peak` is limit-dependent, so it is too high — the in-cluster figure is
49.5 MiB. See `docs/DATA_QUALITY.md`, "Measurement quality on the cluster
side". `need_mib` is retained because it is what the wave packing in
`testbed/wave_plan.json` and the original `artifact_affected` flag were computed
from; removing it would break the audit trail from those artifacts.

**Comparing `need_mib` against `margin_mib` does not reproduce the kernel's
verdict.** It reports the four margin-band VMs (297, 332, 971, 1229) as
mismatches. `need_mib − 59.11 == excess_over_budget_mib` holds on all 50 rows.

## The three results, and how to re-derive each

Each of these is reproducible from `excess_over_budget_mib` alone:

| Result | Derivation | Reproduces |
|---|---|---|
| Offline prediction | `excess_over_budget_mib > 0` | `pred_mem_violation`, 50/50 |
| Kernel verdict (like-with-like) | `excess_over_budget_mib > margin_mib` | `obs_oomkilled`, **50/50, zero mismatches** |
| Margin band | `0 < excess_over_budget_mib <= margin_mib` | `margin_band` → {297, 332, 971, 1229} |

The middle row is the headline finding of T8. The offline rule compares demand
against the **recommendation**; the kernel enforces the **recommendation +
margin**. Comparing like with like — demand against the limit actually
enforced — the offline definition and the kernel agree on every VM in the run.

## All columns

| Column | Meaning |
|---|---|
| `vm_id` | trace VM id; the pod is `rs-p95-<vm_id>` |
| `pred_mem_violation` | offline prediction, from `results/subset_50_predictions.csv`: demand exceeds allocation at any single sample |
| `obs_oomkilled` | the kernel's own verdict — cgroup OOM killer fired |
| `artifact_affected` | **superseded.** Whether the verdict flips between the 59.11 and 49.5 overhead constants. Caught 3 of the 4 undecidable VMs; kept for continuity |
| `margin_band` | **use this instead.** Peak demand falls between the recommendation and the enforced limit, so the outcome is decided by the harness margin rather than by the recommendation under test. Exactly 4 VMs |
| `pred_cpu_violation` | offline CPU prediction: ≥2 consecutive exhausted samples |
| `obs_cpu_violation` | same rule recomputed from the agent's per-sample `cpu_exhausted` flags |
| `max_consec_throttled` | longest run of consecutive exhausted samples observed |
| `samples_logged` / `samples_total` | how much of the trace the pod got through before dying. An OOMKilled pod is **censored on the CPU axis** — see `ARCHITECTURE.md`. VM 296 logged 0 of 2578 |
| `log_captured` | the pod's `replay.jsonl` was non-empty at teardown |
| `peak_demand_mib` | peak scaled memory demand, read straight off the staged trace |
| `budget_mib` | scaled p95 recommendation — what the offline rule compares against |
| `excess_over_budget_mib` | `peak_demand_mib − budget_mib` |
| `need_mib` | provenance only — see above |
| `need_mib_corrected` | `need_mib` shifted to the in-cluster 49.5 constant |
| `margin_mib` | the agent margin M = 80.0, fixed for the whole run |
| `limit_mib` | what the pod's cgroup actually enforced = `budget_mib + margin_mib` |
| `peak_cgroup_mib` | peak `memory.current`/`memory.peak` observed by `sample_cgroups.py`. **Counts reclaimable page cache**, so it overstates the anonymous footprint that drives an OOM. Blank where the cgroup was removed before the sampler read it |
| `peak_pct_of_limit` | `peak_cgroup_mib` as a percentage of `limit_mib`. **100.0% does not imply an OOMKill** — VM 297 hit 100.0% and survived; 387 and 883 hit 100.0% and died. The OOM killer fires only when reclaim fails |
| `headroom_mib` | `limit_mib − peak_cgroup_mib` |

## Provenance

The five files were written during the run (2026-09-08/09). The three columns
`peak_demand_mib`, `budget_mib` and `excess_over_budget_mib` were **backfilled
on 2026-09-09**, computed from the staged traces in `data/traces/` and the
recommendations in `results/subset_50.csv`. Backfill was additive — no
pre-existing column or value was modified — and the result was checked against
`need_mib − 59.11` on all 50 rows before being accepted. Runs from
`wave_report.py` onward emit all three natively.

The pooled, flat version of this data is `results/confusion_table.csv`; the
narrative version is `evidence/T8_confusion.md`.

This trace was graciously provided by Bitbrains IT Services Inc.
