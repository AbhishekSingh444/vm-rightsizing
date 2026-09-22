# Risk-Quantified VM Right-Sizing for Private Cloud Capacity Reclamation

Right-sizing tools report the capacity they reclaim and stay silent about the
violations that reclamation causes. This project reports both on the same axes,
and validates the risk estimate against observed violations rather than
asserting it.

## Start here

| File | What it is |
|---|---|
| `CLAUDE.md` | Project rules and hard invariants. Read before changing anything. |
| `ARCHITECTURE.md` | How the pieces fit, and why each design decision was made. |
| `TASKS.md` | Ordered work queue with acceptance criteria. |
| `docs/HYPOTHESES.txt` | Pre-registered claims. Do not adjust after seeing results. |
| `docs/STEP3_RUNBOOK.txt` | Dataset acquisition and the screenshot checklist. |

## Quick start

```bash
make install
make test          # 16/16 expected
make synthetic     # fake traces, validates the pipeline without the download
make inspect TRACES=data/synthetic
make baselines TRACES=data/synthetic
```

Then get the real trace (`docs/STEP3_RUNBOOK.txt` Part A), unpack to
`data/fastStorage/`, and repeat without the `TRACES=` override.

## Layout

```
src/        offline pipeline: preflight, baselines, recommendations
tests/      unit tests for the violation counter and flavour snapping
testbed/    kind cluster, replay agent, manifest generator, evidence capture
data/       datasets (gitignored -- licensed, do not commit)
results/    generated outputs (gitignored)
evidence/   captured rubric artifacts (COMMITTED -- this is the deliverable)
docs/       hypotheses and runbooks
```

## Dataset

GWA-T-12 Bitbrains fastStorage, from the Grid Workloads Archive at TU Delft:
`http://gwa.ewi.tudelft.nl/datasets/gwa-t-12-bitbrains`

The licence requires acknowledgement in any published material:

> This trace was graciously provided by Bitbrains IT Services Inc.

Also cite the CCGrid 2015 analysis (Shen, van Beek, Iosup) and acknowledge the
Grid Workloads Archive.
