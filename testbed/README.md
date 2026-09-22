# Testbed — Evidence Runbook

Rubric component: **Testbed / environment evidence (3 marks).** Full credit requires a screenshot or log proving the environment runs. Slides alone score zero.

This directory produces that proof, and does something more valuable at the same time: it makes resource exhaustion **physically observable** instead of simulated. Most work in the gap table counts violations by comparing two numbers in a dataframe. Here the kernel decides, and we record its verdict.

---

## What gets measured, and by whom

| Failure mode | Enforced by | Observed as | Terminal? |
|---|---|---|---|
| Memory exhaustion | cgroup memory limit | `OOMKilled` in the pod's `lastState.terminated.reason` | Yes — process dies |
| CPU exhaustion | CFS quota from `limits.cpu` | `nr_throttled` / `throttled_usec` rising in the container's own `cpu.stat` | No — degrades |

Reading throttling from the container's own cgroup rather than from Prometheus removes a whole dependency. There is no metrics stack to install, scrape, or debug. The agent reads `/sys/fs/cgroup/cpu.stat` (v2) or `/sys/fs/cgroup/cpu/cpu.stat` (v1) directly and writes a JSON line per trace sample.

---

## Files

| File | Role |
|---|---|
| `replay_agent.py` | Runs inside each pod. Reads one VM's evaluation-window CSV, reproduces its CPU and memory demand, self-reports throttling per sample. Standard library only. |
| `Dockerfile` | Builds the agent image. No pip install, so it builds offline. |
| `kind-config.yaml` | Single-node cluster, mounts `./traces` into the node. |
| `gen_manifests.py` | Turns a recommendations CSV into one Pod per (VM, method), with `limits` set to the recommendation. |
| `collect_evidence.sh` | Captures every artifact the rubric asks for into `evidence/`. |

---

## Runbook

### 0. Prerequisites

```bash
kind version && kubectl version --client && docker version --format '{{.Server.Version}}'
```

### 1. Stage the traces

One CSV per VM, named `<vm_id>.csv`, containing the **evaluation window only**:

```bash
mkdir -p traces
# populate traces/vm001.csv, traces/vm002.csv, ... from the Bitbrains split
ls traces | head
```

### 2. Create the cluster

```bash
kind create cluster --config kind-config.yaml
kubectl get nodes -o wide
```

Screenshot this. It is the literal artifact the rubric names.

### 3. Build and load the image

`kind` nodes do not see the host Docker daemon's images, so the load step is not optional:

```bash
docker build -t rightsizing/replay:latest .
kind load docker-image rightsizing/replay:latest --name rightsizing
```

### 4. Generate and apply manifests

The 50-VM subset requests ~149 vCPU / 280 GB simultaneously at full scale --
more than a single-node kind cluster on a typical Docker Desktop host
provides. Both the pod limits and the replayed demand are scaled down by the
same factor (default 1/16, see ENVIRONMENT_NOTES.md), which preserves every
VM's demand-to-limit ratio exactly, and the run is split into two
memory-balanced waves of 25 (`wave1_vms.txt` / `wave2_vms.txt`):

```bash
python3 stage_traces.py --subset ../results/subset_50.csv \
    --traces ../data/fastStorage --out ../data/traces --scale 0.0625
python3 gen_manifests.py ../results/recommendations.csv --method p95 \
    --out manifests/p951 --scale 0.0625 --vm-ids wave1_vms.txt
kubectl apply -f manifests/p951/
kubectl get pods -l app=rightsizing-replay -o wide
```

Capture evidence (step 6), `kubectl delete -f manifests/p951/`, then repeat
for `wave2_vms.txt` / `manifests/p952`. `make stage` and `make apply WAVE=1`
/ `make apply WAVE=2` do the same from the repo root.

A single-node kind cluster will not schedule 1,250 pods at any scale, and it
does not need to -- the cluster run is a validation subset, not the full
experiment.

### 5. Watch exhaustion happen

```bash
kubectl get pods -l app=rightsizing-replay -w
kubectl describe pod <a-pod-that-died> | grep -A5 "Last State"
```

### 6. Capture evidence

```bash
./collect_evidence.sh evidence_p95
cat evidence_p95/99_summary.txt
```

Repeat steps 4–6 per method (`peak`, `p95`, `evt_eps0.01`, ...). Each method gets its own evidence directory.

---

## What lands in `evidence/`

```
01_nodes.txt              kubectl get nodes -o wide      <- the rubric artifact
02_version.txt            cluster version and endpoints
03_pods.txt               pods with status and node placement
04_limits.txt             each pod's enforced CPU/memory limits
05_oomkills.tsv           per-pod termination reason and restart count
05_oomkill_count.txt      count of memory exhaustion events
06_describe_oomkilled.txt full describe of one OOMKilled pod
07_evictions.txt          kubectl events with reason=Evicted
07_pod_status.tsv         per-pod status.phase/reason/message
07_evicted_count.txt      count of kubelet-evicted pods (node pressure, NOT a violation -- exclude from T8, re-run)
08_node_conditions.txt    node conditions (MemoryPressure, etc.) at capture time
logs/<pod>.jsonl          per-sample throttling records
99_summary.txt            rollup, including an OOMKilled/Evicted overlap sanity check (should be 0)
```

Put `01_nodes.txt`, `04_limits.txt`, and `06_describe_oomkilled.txt` in the report as figures. The first proves the environment runs, the second proves the recommendation was actually enforced, and the third proves a violation was observed rather than computed.

**OOMKill vs. eviction.** A container's own cgroup OOM killer firing (`OOMKilled`, `05_oomkills.tsv`) means *that pod's* memory limit was exceeded -- a real violation. A kubelet eviction (`Evicted`, `07_pod_status.tsv`) means the *node* ran short of memory, which can happen even to a pod well under its own limit if enough neighbours are running hot -- an artifact of the wave's aggregate footprint, not that VM's prediction. `07_evicted_count.txt` and `08_node_conditions.txt` exist so an evicted pod can be identified and excluded from the T8 confusion table (and re-run in a later wave) rather than miscounted as a memory violation. `99_summary.txt` also cross-checks that no pod shows up as both -- if one does, the OOMKilled/Evicted distinction has broken down and needs investigating before trusting either.

---

## Design notes worth defending

**Time compression.** `SLICE_SECONDS=1` replays one 5-minute trace sample per second, so a 9-day evaluation window (~2,600 samples) runs in about 43 minutes. This compresses the *schedule*, not the intensity — instantaneous demand at each sample is reproduced faithfully. It does mean the memory-pressure duration is compressed 300×, so a slow leak that would OOM after hours of real accumulation may not here. State this as a limitation rather than letting a reviewer find it.

**Limits, not just requests.** Requests only influence scheduling. Limits are what the kernel enforces. Setting both to the recommendation is what makes exhaustion happen instead of being inferred.

**Persistent burner pool.** Workers are spawned once and read a shared duty value. An earlier version respawned them per sample and added roughly two seconds of process startup to every one-second slice, which silently destroyed replay timing. The loop also sleeps to absolute deadlines so page-touching cost does not accumulate across 2,600 samples.

**Pool width fixed at the CPU limit.** Demand above the limit saturates every worker and the shortfall appears as throttling — which is the signal being measured, not a bug.

**Per-sample records go to stdout, not just the log file.** `replay_agent.py` mirrors every JSONL line to stdout in addition to `LOG_FILE`. `kubectl exec ... cat LOG_FILE` only works while the container process is still running; a pod that OOMKills or simply completes (`restartPolicy: Never`) has none, silently losing the per-sample throttling records -- the CPU side of the T8 confusion table -- for any pod that finishes before evidence is collected. `kubectl logs` reads the kubelet's captured stdout and works after termination, so `collect_evidence.sh` treats `logs/<pod>.jsonl` (from `kubectl logs`) as authoritative and the exec-based copy as a best-effort extra only.

**Known caveat: memory demand fidelity.** The agent allocates and touches a buffer matching the trace's reported memory usage. Bitbrains reports what is best read as active memory, and a subset of VMs have values pinned at or near the provisioned figure. Apply the same cleaning rule here as in the offline pipeline, and document it once for both.

**The cluster run does not replace the offline replay.** The full 1,250-VM frontier is computed offline in pandas. The cluster subset exists to validate that offline-counted violations correspond to real kernel-enforced ones. If the two disagree, the offline violation definition is wrong — which is a finding worth reporting on its own.

---

## Fallback

If Docker or `kind` is unavailable, a CloudSim Plus build log is the accepted alternative under the rubric. It is a weaker artifact: it proves a simulator compiles, not that a scheduler enforced anything. Exhaust the `kind` route first — `k3s` or `minikube` are also fine substitutes and the manifests are portable to both.
