# Environment notes

Record here anything that had to change to make the environment run: OS,
container runtime, cluster flavour, versions, and any command from
`testbed/README.md` that failed and how it was fixed.

This file is evidence. A reviewer asking "does this actually run on a real
machine" is answered by a populated version of it.

## Environment

- OS / kernel:
- Container runtime and version:
- Cluster (kind / k3s / minikube / managed) and version:
- kubectl version:
- Python version:
- cgroup version (v1 or v2):

## Deviations from the documented runbook

| Date | Command that failed | What changed and why |
|---|---|---|
| 2026-09-07 | `make test` / `make inspect` | `make` and `python3` are not on PATH on this Windows machine. Ran the underlying commands directly instead: `python tests/test_baselines.py` and `python src/inspect_traces.py --traces data/fastStorage --report results/dataset_report.csv`. Same commands the Makefile runs, just invoked without `make`. |
| 2026-09-07 | fetching the trace from `http://gwa.ewi.tudelft.nl/datasets/gwa-t-12-bitbrains` | The TU Delft GWA site refused the connection (ECONNRESET on both http and https). Downloaded the identical archive from the `@Large` research group's mirror instead: `https://atlarge-research.com/gwa-traces/gwa_t_12_fastStorage.zip` (134,047,207 bytes, same CCGrid 2015 Bitbrains dataset, same licence/attribution terms). Unzipped and flattened `fastStorage/2013-8/*.csv` up one level so `data/fastStorage/` holds `1.csv` … `1250.csv` directly, matching what the docs and loader expect. |
| 2026-09-07 | `src/baselines.py` obs/eval split | Was slicing by row count (`df.iloc[:obs_samples]`) assuming uniform 5-minute sampling; samples-per-file actually range 5 to 23,692 for a 30-day span, so the split was silently wrong in wall-clock terms for many VMs. Replaced with `split_obs_eval()`, which slices by each VM's own `Timestamp` column (first `obs_days` from that VM's first sample, next `eval_days` after). Also had to tolerate a boundary rounding case: a discretely-sampled 30-day trace's last sample sits ~one sampling interval short of the exact 30-day mark, so a strict `t_last >= eval_end` check rejected every VM in the real trace — added a shortfall tolerance of one sampling interval (median `diff` of that VM's own timestamps). Added 3 unit tests (`tests/test_baselines.py`) covering nominal 5-min sampling at this tight boundary, finer-than-5-min sampling, and a too-short VM. Did not touch the 16 existing tests. |
| 2026-09-07 | `src/inspect_traces.py` vs `src/baselines.py` disagreement | Preflight reported 1250/1250 usable while baselines.py's `load_vm`/`split_obs_eval` excluded 146 (103 "bad CPU capacity", 43 "short span") — evidence/02_preflight.txt cannot contradict results/. Added the same two gates to inspect_traces.py (imports `split_obs_eval` from baselines.py rather than reimplementing it, so the two cannot drift apart again) and added `--obs-days`/`--eval-days` CLI flags (defaults 21/9, matching baselines.py) so the span check uses the same window. Preflight now reports 1104/1250 usable, agreeing with baselines.py exactly. |
| 2026-09-07 | explicit capacity data-quality gates (soft flags, not exclusions) | The `314.csv` memory glitch (see below) was only excluded as a side effect of the unrelated short-span gate — nothing would have caught it if it had run long enough to be "usable". Added `baselines.capacity_glitch_ratio`/`is_capacity_glitch` (flags a VM whose first sample is >10x off its own file's median on a capacity column) and `baselines.detect_capacity_shift` (flags a VM whose capacity column holds two materially different values each sustained for ≥5% of the file, >10x apart — catches genuine mid-trace resizes, which the glitch check cannot see by design). Both wired into `inspect_traces.py`, reporting to `results/capacity_glitch_report.csv` / `results/capacity_shift_report.csv` without excluding anything. Confirmed by running against all 1250 real files rather than assumed: glitch check flags 65 (not the ~104 expected — 40 of the 103 "bad CPU capacity" VMs are structurally invisible to a median-based check, see docs/DATA_QUALITY.md pattern 2); shift check flags 4, of which 2 (`133.csv`, `295.csv`) were live in the usable set at that point and already affecting T2's reported reclamation totals. Superseded by the next row. |
| 2026-09-07 | mid-trace capacity shift (pattern 3): decided to exclude | Decision: a VM whose provisioned capacity changes mid-trace is not one right-sizeable entity — the observation window describes a machine that no longer exists by the evaluation window. Splitting at the shift point leaves sub-windows too short for a 21/9 split (`133.csv`'s segments are ~15 days each, `295.csv`'s low segment is under 5 days); using post-shift capacity pairs demand history with the wrong allocation for part of it — the same category of error as the corruption this investigation started from. Made permanent in `baselines.load_vm` (mirrored in `inspect_traces.py`), alongside the existing bad-CPU-capacity and short-span gates. Excludes 4 VMs total (`133.csv`, `295.csv`, `331.csv`, `752.csv`), of which 3 show up as a distinct "mid-trace capacity shift" reason at the time (`331.csv` intercepted earlier by bad-CPU-capacity). Added a `load_vm`-level unit test — `tests/test_baselines.py` was 32/32 at this point. Usable VMs was **1102** (was 1104) after this change alone; superseded by the next row. Full rationale: `docs/DATA_QUALITY.md` pattern 3. |
| 2026-09-07 | below flavour catalog floor (pattern 4): decided to exclude | While directly verifying idle-vs-non-idle mean memory reclamation percentage (rather than inferring it from the aggregate), the unweighted per-VM mean came out at -360.70% (idle, peak) — not a bug: 347 VMs (27.8% of the full 1250, 31.5% of the then-1102-usable fleet) have an original allocation below the flavour catalog's smallest entry (2GB memory / 1 vCPU; all 347 fail on memory specifically — no VM in this trace has 0 < cores < 1). `snap_up` forces such a VM's "right-sized" allocation *above* its original, so reclamation goes negative and, normalized by a near-zero original allocation, blows up arbitrarily (one VM: -419,749%, on a 58MB original allocation). Decision: exclude — a VM already smaller than the smallest flavour cannot be right-sized, so including it measures the catalog, not the method. Added gate `cores < min(FLAVOUR_VCPU) or mem_cap_gb < min(FLAVOUR_MEM_GB)` to `baselines.load_vm`, checked after the bad-capacity gate (mirrored in `inspect_traces.py`), with 3 new `load_vm`-level unit tests (below-floor memory, below-floor cores, exactly-at-floor control) — `tests/test_baselines.py` now **35/35**. This gate also intercepts `752.csv` (previously attributed to "mid-trace capacity shift") and 36 VMs previously attributed to "short span", ahead of those gates in `load_vm`'s check order — same VMs, more fundamental reason once both gates exist. Re-ran `inspect` and `baselines` end to end: usable VMs is now **792** (was 1102), idle count 698→425, non-idle 404→367. `results/`, `evidence/02_preflight.txt`, `evidence/06_baseline_summary.txt` all regenerated. Also added, as a stated limitation (not a code change): pooled snapped-vs-unsnapped memory reclamation shows 15.8% (all_vms) / 25.1% (non_idle) of the peak-vs-p95 memory gap is catalog-quantisation, not workload behaviour (`results/memory_snap_comparison.csv`, computed on the 1102-VM population before this gate) — noted as a requirement that the future EVT method `M(eps)` be reported both snapped and unsnapped against H1 for the same reason. Full detail, all reclamation figures pooled/GB-hour-weighted throughout (no unweighted per-VM means reported anywhere): `docs/DATA_QUALITY.md` pattern 4 and the "flavour catalog quantisation" limitation section. |
| 2026-09-07 | T3 cluster subset: any_violation stratification starved the CPU confusion table | `make_recommendations.py`'s original `--subset` logic stratified only on `any_violation` (CPU OR memory). A random draw of 25 "will violate" VMs from the 231 non-idle p95 violators landed on only 8 CPU-violators (of 64 available) by chance — T8's CPU confusion table would have had almost no positive cases to check against throttling records. Added `--subset-strategy resource`, which cross-stratifies on the CPU x memory violation grid (`cpu_only`, `both`, `mem_only`, `safe`) with configurable counts per stratum (default 10/15/15/10, chosen to give a ~25/25 CPU split and a ~30/20 memory split from the available pools: 10 cpu_only, 54 both, 167 mem_only, 136 safe among 367 non-idle VMs under p95). Made the new default in `make recommend`. Regenerated `results/subset_50.csv` / `subset_50_predictions.csv` — composition confirmed: CPU predicted-violate 25 / safe 25, mem predicted-violate 30 / safe 20. peak was checked as an alternative and rejected: only 10 CPU-violators and 26 mem-violators exist non-idle under peak, too thin to stratify on at all. `tests/test_baselines.py` unaffected (35/35); `make_recommendations.py` has no test file. |
| 2026-09-07 | T3/T6: 50-pod subset resource requests exceed single-node kind capacity | `gen_manifests.py` sets pod `requests == limits` to the recommendation (testbed/gen_manifests.py — deliberate, limits are what the kernel enforces). Summed across the stratified 50-VM subset that is **149 vCPU / 280 GB** requested simultaneously — no single-node kind cluster on a typical Docker-Desktop-on-Windows host has that much allocatable capacity (commonly 4-8 vCPU / 4-8 GiB, and `kind` does not inflate that). Fix: scale the pod limit AND the replayed demand by the same factor `S`, so `demand'/limit' = (demand·S)/(limit·S) = demand/limit` is preserved exactly for every VM — the offline prediction (violate or not) is unchanged at any `S`, only the absolute scale of the cluster run changes. Chose `S = 1/16` (not 1/32, which was rejected — see below). New `testbed/stage_traces.py` slices each subset VM's raw trace to the evaluation window (same `split_obs_eval` as `baselines.py`) and multiplies `CPU usage [MHZ]` / `Memory usage [KB]` by `S`, writing to `data/traces/`; `gen_manifests.py` gained `--scale` (multiplies `rec_cores`/`rec_mem_gb` before templating the pod) and `--vm-ids` (restrict manifests to a wave's VM list). At `S = 1/16`: recomputed each of the 50 VMs' actual evaluation-window peak demand (via `baselines.load_vm`/`split_obs_eval`) against its p95 limit, before and after scaling — max \|Δratio\| across all 50 VMs was 0.0 (CPU) and 4.4e-16 (memory, floating-point noise). Zero VMs fell below either practical floor (128 MiB memory, 5 millicores CPU) at this factor. Scaled totals: 9.31 vCPU / 17.5 GB — still too much for one wave on most Docker Desktop configs, so the run is split into two memory-balanced waves of 25 (greedy longest-first packing on scaled memory): wave 1 = 4.44 vCPU / 8.75 GB, wave 2 = 4.875 vCPU / 8.75 GB, recorded in `testbed/wave1_vms.txt` / `wave2_vms.txt`. `Makefile` gained `stage` and `WAVE=`/`SCALE=` on `apply`. |
| 2026-09-07 | 1/32 rejected in favour of 1/16: floor margin, not just floor | 1/32 was the first factor proposed (memory-floor-driven: smallest catalog flavour is 2GB, and 2GB/32 = 64 MiB, chosen as the minimum a `python:3.12-slim` container plus the burner-worker process needs above its own baseline RSS). The problem: 64 MiB is the floor itself, zero margin above it — if the measured baseline sat anywhere near that line, safe-predicted VMs would OOMKill on interpreter overhead rather than trace demand, a false positive landing in exactly the T8 confusion-table cell being measured, and biased in one direction only (inflating apparent memory violations, never CPU). Measured the actual baseline before deciding: built `rightsizing/replay:latest`, ran it in a container against a 60-sample near-zero-demand trace (`SLICE_SECONDS=1`, `--memory=512m` so the probe itself wasn't constrained), read `/sys/fs/cgroup/memory.peak` mid-run (the authoritative cgroup figure, not `docker stats`' periodic sample) — **peak RSS = 30,957,568 bytes (29.5 MiB)**. At 1/16 the smallest flavour's limit is 128 MiB, ~4.3x the measured baseline — real headroom. Confirmed via the same ratio check as above that 1/16 still preserves every VM's demand/limit ratio exactly and clears both practical floors with room to spare. |
| 2026-09-07 | T4/T6: multiplicative memory scaling caused real false-positive OOMKills — found live, wave 1 aborted | Created the kind cluster (installed `kind` v0.29.0 — not present on this machine, downloaded the official Windows binary to `~/.local/bin/kind.exe`, no package manager (choco/scoop/winget) available). `kubectl describe node` gave the real allocatable: 8 vCPU / 7,071,968 Ki (6.74 GiB), 950m CPU / 290Mi memory already requested by kube-system + local-path-provisioner, leaving **~7.05 vCPU / ~6616 Mi** for replay pods -- less than assumed, so "two waves of 25" (8.75 Gi/wave) did not fit; re-split into 4 waves (11/11/14/14, target ≤5.5Gi with a 4.4-4.6Gi actual, ~32% headroom) using a stratum-quota + memory-greedy assignment (`testbed/wave1-4_vms.txt`) so every wave contains a mix of all four CPU-x-memory strata, not a whole stratum concentrated in one wave. Fixed an unrelated pre-existing bug hit along the way: `gen_manifests.py`'s pod template left the `vm:` label unquoted, so a numeric vm_id (e.g. `143`) parsed as a YAML integer and `kubectl apply` rejected every manifest (`ObjectMeta.metadata.labels of type string`) -- quoted it. Ran wave 1 (11 pods): all scheduled (0 Pending), 0 evictions, node `MemoryPressure` stayed False, but **8/11 OOMKilled within 6 minutes, 2 of which were predicted memory-safe** (`324`: predicted ratio 0.984; `344`: predicted ratio 0.967) -- false positives in exactly the T8 confusion-table cell being validated. Root cause: the demand/limit ratio-preservation math (`demand'/limit' = demand/limit`) assumed container memory = demand buffer only. Real container memory = fixed interpreter/worker overhead (~29.5 MiB, measured previously) + demand buffer. At full scale that overhead is noise against a multi-GB limit; at scale=1/16 a 2GB VM's limit shrinks to 128 MiB and the same ~29.5 MiB overhead silently eats ~23% of it, pushing any VM whose demand sat close under its ratio (324, 344 both >96%) over the edge -- overhead never scaled down with everything else. Wave 1 torn down (`kubectl delete -f`) rather than let it run ~130 more minutes on invalidated data. Fix: **additive margin**, not multiplicative -- pod memory limit = `WORKER_OVERHEAD_MIB[pool_width] + rec_mem_gb*scale*1024`, replayed demand unchanged (`stage_traces.py` was already correct). This makes the OOM condition `O + S*demand > margin + S*limit`; with `margin == O` exactly, the `O` cancels and the condition reduces algebraically to `demand > limit` -- the offline condition, independent of scale. `pool_width = max(1, ceil(scaled_cores))` mirrors `replay_agent.py`'s `CpuLoad.__init__` exactly, because each forked burner worker adds its own interpreter, so overhead is not a single constant -- see next row for the per-worker-count measurements. Never touched CPU scaling (`rec_cores*scale`, pure multiplicative) -- CFS throttling has no comparable fixed floor and showed no evidence of a parallel problem. |
| 2026-09-07 | Per-worker-count overhead measurement, with variance | Measured `/sys/fs/cgroup/memory.peak` for the replay agent at pool widths 1, 2, 4, 8 (`LIMIT_CORES=1/2/4/8`, near-zero-demand 30-sample trace, `--memory=1024m` so the probe wasn't itself constrained), 3 runs each. First-ever run (workers=1, run 1) measured 22.04 MiB against a steady-state ~19.7 MiB -- a one-time cold-start artifact (image/page-cache warm-up), confirmed by 3 additional workers=1 runs (19.56, 19.80, 19.54 MiB) clustering tightly with the other steady runs; excluded from the reported mean. Steady-state means (MiB, n=3 except workers=1 n=5): **1 → 19.708 (range 0.29), 2 → 21.178 (range 0.22), 4 → 24.238 (range 0.20), 8 → 29.871 (range 0.19)**. Overhead does grow with worker count (~1.5-3 MiB per doubling, as expected -- each fork gets its own interpreter), confirming margin must be keyed by pool width, not a single constant; encoded as `WORKER_OVERHEAD_MIB` in `gen_manifests.py`. Variance is small: run-to-run range ≤0.29 MiB at every worker count, amplified by 1/S=16x that is a ≤2.34 MiB implied demand-side error band -- well under the ~2 MiB/run flag and negligible against GB-scale allocations (e.g. 0.12% of a 2GB limit in the worst case). **Conclusion: variance does not argue for a larger S; a per-worker additive margin at S=1/16 is sound.** Re-verified the OOM threshold algebraically for all 50 subset VMs (`threshold_ratio` = the demand level that actually triggers OOM, divided by the offline allocation -- should be 1.000): all 50 landed within [1.000002, 1.000016] of 1.000 (max deviation 0.0016%, pure 2-decimal-Mi rounding noise, not a bug) -- and all 50 have `pool_width=1` under this subset/catalog at scale=1/16 (max catalog flavour is 16 vCPU, scaled to exactly 1.0, `ceil(1.0)=1`), so only the workers=1 margin is actually exercised by this cluster run; the 2/4/8 rows exist for defensibility and any future subset/scale combination that does cross a worker boundary. Recomputed wave memory totals with the margin added: wave 1/2 (11 pods each) 4824.81 Mi, wave 3/4 (14 pods each) 4627.94 Mi -- all still comfortably under the 6616.2 Mi free reported by `kubectl describe node` (≥27% headroom remaining after the margin term). |
| 2026-09-07 | Wave 1, margin fix applied: SAME two VMs (324, 344) still OOMKilled -- margin wasn't the cause, fixed a YAML quoting bug and a k8s memory-quantity precision bug along the way | Ran wave 1 with the additive-margin fix (previous row). 8/11 OOMKilled within 6 minutes; 6 were correctly-predicted violators, but **324 and 344 -- the identical two false positives from before the margin fix -- OOMKilled again**, ruling out margin sizing as the cause (a margin bug would not reproduce identically after the margin changed). Live `cgroup memory.current` reads while pods were still running were the key diagnostic: VM 643 (near-zero demand, no CPU throttling) tracked its ~19MiB baseline flatly with no anomaly; VM 344 (CPU-throttled, `rec_cores=1.0` -> scaled to 0.0625 core, the smallest catalog flavour) showed `throttled_us: 18,668,893` -- 18.7s of accumulated throttling inside one ~1s replay slice, ~84s behind the trace schedule, with reported demand (315MB) well under its 512MB scaled budget when it died shortly after. Both false positives share the tightest CPU quota in the subset, pointing at a CPU-throttling interaction rather than the memory margin itself. Along the way, fixed two unrelated bugs hit while iterating: (1) `gen_manifests.py`'s pod template left the `vm:` label unquoted, so numeric vm_ids parsed as YAML integers and `kubectl apply` rejected every manifest; quoted it. (2) The 2-decimal-Mi memory value (`147.71Mi`) doesn't reduce to a whole number of bytes, which the Kubernetes API accepts only by re-expressing it in milli-bytes (`154885160960m`, with an admission warning) -- switched `gen_manifests.py` to compute a whole byte count and emit a bare integer (no suffix), eliminating the warning; the ~187-byte discrepancy this fixed was negligible (not the cause of the OOMs) but the milli-byte warning was a real code smell worth removing before trusting the run. Also added stdout-mirroring to every JSONL record in `replay_agent.py` (LOG_FILE alone requires `kubectl exec`, which needs a running container -- loses data for any pod that OOMKills or completes before evidence collection runs) and updated `collect_evidence.sh` to treat `kubectl logs` as the authoritative JSONL source. Tore wave 1 down again rather than let it run ~2 more hours on data known to contain false positives. |
| 2026-09-07 | Root cause found and reproduced standalone: MemLoad transient double allocation, CPU-independent -- throttling-feedback hypothesis tested and falsified | `MemLoad.set_target` (`testbed/replay_agent.py`) did `self.buf = bytearray(want)` -- a plain reassignment, which evaluates its right-hand side (allocating and zero-touching the NEW buffer) before the OLD buffer's refcount drops. On any upward jump in demand this transiently holds BOTH buffers in memory simultaneously (old_size + new_size), which can exceed a limit that comfortably fits either individually -- exactly the shape of 324/344's failure (each individual sample was under the limit; a transition between two samples was not). **Test A (standalone, zero CPU involvement):** ran VM 344's actual scaled trace against `MemLoad` outside the cluster (`docker run --cpus=4`, no throttling, no real-time pacing) -- the original implementation OOMKilled before 200 of 2580 samples, nowhere near the 494.9MB trace peak against the 532MB limit; a one-line fix (`self.buf = bytearray()` to release the old buffer *before* allocating the new one) ran all 2580 samples to completion, peak 532.02 MiB, no OOM. This alone fully reproduces the bug with CPU removed as a variable. **Test B (falsification of the alternative hypothesis):** with Test A's fix already applied (to isolate this hypothesis cleanly), replayed VM 344's actual trace in real time under an explicit `--cpus=0.0625` quota matching the cluster's scaled CPU limit, logging elapsed-vs-deadline lag, RSS, and throttled_us per sample. Lag grew but **decelerated** (5.7s -> 12.5s -> 16.4s -> 22.7s -> 26.6s -> 29.9s -> 31.4s -> 32.5s -> ... -> 78.8s over 99 samples), converging rather than diverging; RSS tracked demand (rising to ~175MB during a 118.8MB demand spike, falling back to ~50-94MB as demand receded), landing consistently ~20-30MB above the naive margin+demand estimate -- a bounded, repeatable transient overshoot, not runaway growth. No OOM after 99 samples (~180s wall time) with peak RSS ~175MB against the 532MB limit. **The deadline-overrun/CPU-throttling-feedback hypothesis is therefore falsified as an independent OOM-causing mechanism** -- heavy throttling and growing lag are real (matches what wave 1 showed) but do not by themselves drive memory past the limit once the double-allocation bug is fixed. Recorded here specifically so this doesn't get re-investigated later: the two mechanisms were tested independently and only one (transient double allocation) explains the failure. Fix applied to `testbed/replay_agent.py`'s shipped `MemLoad.set_target`. Regression test added: `tests/test_replay_agent.py`, which keeps an inline copy of the pre-fix implementation as a negative control and asserts on `tracemalloc` peak (not on ramp completion, which would pass either way) -- 3/3 passing, wired into `make test`. Re-measured the per-worker overhead against the fixed image to confirm the margin constants still hold (they should be unaffected: the near-zero-demand baseline trace never triggers a reallocation after the first sample, so this bug's code path was never exercised by that measurement) -- max delta 0.108 MiB across all four worker counts, well within prior run-to-run noise; `WORKER_OVERHEAD_MIB` left unchanged. |
| 2026-09-07 | Wave 1, fix rebuilt and reloaded: fix confirmed necessary but NOT sufficient -- 324 OOMKilled a third time | Rebuilt `rightsizing/replay:latest` with the `MemLoad` fix, reloaded into kind, regenerated wave 1, applied. Verified the running pod's `imageID` (`sha256:de36bb7f...`) matched this exact build's config digest, and separately extracted `/app/replay_agent.py` from the image (`docker run --entrypoint /bin/cat ...`) to confirm the fixed code was actually what shipped -- not a stale-image false confidence. Result: 6/11 OOMKilled, all correctly-predicted violators (143, 185, 215, 820, 883 -- as before -- plus this time **324 again**); 344 survived the full observation window this run (a clear improvement -- previously died in under a minute both times). 324 itself survived far longer than before (~133s vs ~27-45s pre-fix) but still died, at sample ~92 with demand only 71-100MB against a 128MB demand budget (well under), during a period of heavy CPU throttling (throttled_us up to 9.88M microseconds in one ~1s slice). **Conclusion: the transient-double-allocation fix is real, necessary, and measurably improved the outcome (one of two false positives eliminated entirely, the other survived ~4x longer), but does not fully explain 324's failure.** A third mechanism is still at play, most likely allocator/kernel memory retention under sustained heavy CPU starvation (glibc arena or page-cache pages not being returned/reclaimed promptly while the process gets almost no CPU time to service munmap/reclaim, distinct from both the transient-double-buffer mechanism (Test A, fixed) and the bounded demand-tracking behavior observed in Test B up to ~180s/99 samples for VM 344's trace) -- not yet reproduced standalone or confirmed. Wave 1 torn down again. Stopped to report this rather than keep iterating live against the cluster, consistent with investigating standalone first. |
| 2026-09-07 | Additivity of the two overheads checked directly on VM 324 (full agent, full trace) -- assumption wrong in both directions, margin band measured | The margin had been reasoned about as two separately-measured constants added together (45.4 MiB observed transient + 19.7 MiB interpreter baseline = 65.1 MiB assumed). Neither constant was measured under the conditions the cluster run actually imposes (both came from short probes; the 19.7 figure specifically from a near-zero-demand trace that never reallocates). Checked it with one measurement instead of reasoning further: ran the **unmodified shipped agent** (`MemLoad` + `CpuLoad` both live, `ra.main()` called directly, not `margin_probe.py`) against VM 324's real scaled evaluation trace, full 2591 samples, `SLICE_SECONDS=1.0`, under a cluster-matching `--cpus=0.0625` quota, with `--memory=1024m` so the probe could not itself OOM. A 20 Hz sampler thread recorded the container's own `/sys/fs/cgroup/memory.current` against the demand `MemLoad` was currently targeting (`set_target` wrapped so the *new* target is recorded before the allocation, attributing any transition transient to the level being moved to -- the conservative attribution). Probe: `testbed/margin_probe_full.py`; summary `evidence/09_margin_probe_324.json`, raw 20 Hz samples `evidence/09_margin_probe_324_samples.csv`, agent JSONL `evidence/09_margin_probe_324_replay.jsonl`; 52,813 samples over 2716 s wall, ran to `complete`, no OOM. **Results, and why the metric matters:** peak `memory.current` - *concurrent* demand = **88.19 MiB** (at t=1584 s, sample 1578, mem 116.52 MiB against demand 28.33 MiB); peak `memory.current` - *running-max* demand = **59.11 MiB**; `memory.peak` 185.14 MiB against a 125.95 MiB peak demand. The 88.19 and the 59.11 are the same phenomenon seen two ways: memory tracks demand upward promptly but lags it downward (retained after a spike recedes -- the allocator/kernel-retention mechanism hypothesised in the row above, now observed directly and quantified). Because the pod's limit is a *constant*, the retention tail is harmless: what can breach the limit is the peak, and the peak sits at **peak demand + 59.11 MiB**, i.e. 324 needs `M >= 185.07 - 128 = 57.06 MiB`. **So the additivity assumption fails in both directions**: against the concurrent-demand metric the real figure is 88.19, well *above* the assumed 65.1 (the two overheads do not compose additively), while the figure that actually governs OOM is 59.11, *below* 65.1 (the 45.4 and 19.7 constants overlap rather than stack -- the interpreter baseline is already inside the transient). **Margin band, computed across all 50 subset VMs** by comparing each VM's peak scaled demand + 59.11 against its scaled budget: `M >= 57.06 MiB` or safe-predicted VM **324** falsely OOMs (it clears its budget by only 2.05 MiB); `M < 82.66 MiB` or predicted violators **332** and **1229** falsely survive (they exceed budget by only 23.55 MiB), with **971** next at 86.42. Band width 25.60 MiB, midpoint 69.9. This retires two candidate values on measurement rather than argument: **55 MiB is below the floor** and reproduces exactly the 324 false positive that cost three waves, and **109 MiB** (the literal "measurement + 20" applied to the 88.19 figure) is above the ceiling and converts three true violators into false negatives. **Methodology decision (stated deliberately, not an artifact of tuning):** the margin is set *high within the measured safe band* rather than close to the measured overhead. The asymmetry is the reason -- a margin below true overhead produces **false positives** at the confusion-table boundary, on VMs sitting within a few MiB of their budget (324 at 2.05 MiB, 142 at 5.46 MiB), which is precisely the cell T8 is trying to measure and is biased in one direction only; a margin above true overhead produces **false negatives** only on VMs clearing their budget by 23+ MiB, and no VM in the subset sits between those two, so the whole band is confusion-table-neutral. Precision within the band buys nothing, and wave count is unchanged across it (5 waves at every value from 55 to 109). Chosen value pending confirmation -- see the report accompanying this row. Wave arithmetic at S=1/16 against the 6616.2 Mi / 7.05 vCPU the node actually has free: 5 waves at any in-band margin (largest wave 4537.0 Mi / 31.4% headroom at M=75, 4592.0 Mi / 30.6% at M=80), up from 4 at the current 19.708 MiB; CPU never binds (peak 29.3% of free vCPU). |

| 2026-09-07 | Margin set to **M = 80 MiB**, and the reason the "measurement + 20" rule was not applied to 88.19 | The +20 safety rule had been specified against the wrong metric. It was written against `peak(memory.current - concurrent demand)` = **88.19 MiB**, which would have given 108-109 MiB -- above the band's ceiling, converting three true violators into false negatives. But that metric is not the one that governs an OOM. `peak(memory.current - concurrent demand)` is maximised at t=1584 s, where `memory.current` is 116.52 MiB against a demand of only 28.33 MiB: that gap is the **retention tail**, memory held after a spike has receded. It occurs at *low* demand, and the pod's limit is a *constant*, so the tail cannot breach it -- a process sitting at 116 MiB under a 208 MiB limit is not close to anything, no matter how small the demand it is holding that memory for. What can breach a constant limit is the absolute peak, and the peak is bounded by `peak(memory.current - running-max demand)` = **59.11 MiB** added to peak demand. Applying a safety factor to 88.19 would therefore have been marking up a number that already contains ~29 MiB of slack which is harmless by construction -- double-counting it. The band **[57.06, 82.66) MiB** was computed from the OOM-governing metric (59.11) across all 50 subset VMs: below 57.06, VM 324 falsely OOMs; at 82.66 and above, predicted violators 332 and 1229 falsely survive. **M = 80** sits 2.66 MiB below the ceiling and 22.94 MiB above the floor. The asymmetric placement is deliberate and is the methodology decision recorded in the row above: a margin below true overhead produces false *positives* on VMs sitting within a few MiB of budget (324 at 2.05 MiB, 142 at 5.46 MiB) -- exactly the confusion-table cell T8 measures, biased in one direction -- while a margin above true overhead produces false *negatives* only on VMs clearing budget by 23+ MiB. No subset VM lies between those two groups, so the entire band yields an identical confusion table and precision within it buys nothing. Implemented as `MARGIN_MIB = 80.0` in `testbed/gen_manifests.py` (`--margin-mib` to override), replacing the `WORKER_OVERHEAD_MIB` pool-width table; `MEASURED_POOL_WIDTH = 1` now refuses manifest generation outside the regime the margin was measured in, rather than interpolating. |
| 2026-09-07 | Additivity of the two overheads: they **overlap rather than stack** -- the assumed 45.4 + 19.7 = 65.1 MiB was not a sum | Recorded explicitly because the arithmetic was wrong in a way that is easy to re-derive by accident. The margin had been reasoned about as two independently-measured constants added: a **45.4 MiB** observed allocation transient plus a **19.7 MiB** interpreter baseline. The single full-agent measurement on VM 324 (previous row, `evidence/09_margin_probe_324*`) shows they are not additive: the OOM-governing overhead is **59.11 MiB**, *below* the assumed 65.1, because the interpreter baseline is already **inside** the transient -- the transient is measured as total `memory.current` above demand, and the interpreter's own RSS is part of that total, not something sitting beside it. Counting both is counting the interpreter twice. In the other direction, against the concurrent-demand metric the figure is **88.19 MiB**, well *above* 65.1, so the two constants do not compose additively in that direction either. The general lesson for the writeup: overheads measured under different conditions (a short probe at near-zero demand, and a transient during reallocation) do not compose, and the only defensible figure came from measuring the shipped agent, on the real trace, under the cluster's actual CPU quota, in one run. |
| 2026-09-07 | Wave split regenerated as **5 waves at M = 80** (was 4 hand-maintained files at M = 19.708), and made reproducible | The wave membership had been four hand-maintained `waveN_vms.txt` files. Since the margin is a fixed per-pod cost that does not shrink with `--scale`, raising it from 19.708 to 80 MiB adds 60.3 MiB to every pod and changes the packing outright, so the split is now computed rather than maintained: `testbed/plan_waves.py` (`make waves`). It packs LPT (heaviest-first onto the least-loaded wave, ties broken by vm_id, so a re-run reproduces the split byte-for-byte), solving for the smallest wave count whose largest wave fits the node's *free* capacity -- allocatable minus already-booked requests, queried live -- with `--headroom` to spare. Headroom defaults to 0.30: requests equal limits so the scheduler alone would accept far tighter packing, but the headroom is against node-level `MemoryPressure`, which evicts pods that are under their own limit and manufactures exactly the Evicted-vs-OOMKilled ambiguity `collect_evidence.sh` has to screen out. Result at M=80, S=1/16, against the 6616.2 Mi / 7.05 vCPU free: **5 waves**, largest 4448.0 Mi (32.8% headroom), CPU never binding (peak 2.19 of 7.05 vCPU). **Wave 1 is not simply the first fifth.** Given `--traces`, the VMs whose outcome the margin actually decides are pinned to it: a VM OOMs iff `need = peak_scaled_demand + 59.11 - scaled_budget > M`, so `|need - M|` is precisely how wrong the margin would have to be to flip that VM, and everything within `--boundary-mib` (default 40, comfortably wider than the 25.60 MiB band) goes in wave 1. That puts all six margin-decided VMs in the first wave -- **1229** (need 82.66, flips at 2.66), **332** (82.66, 2.66), **971** (86.42, 6.42), **324** (57.06, 22.94), **142** (53.65, 26.35), **344** (42.04, 37.96) -- so a mis-set margin is visible in wave 1 rather than four waves later. Plan and per-VM `need` recorded in `testbed/wave_plan.json`. |
| 2026-09-07 | **Evidence bug found and fixed: `collect_evidence.sh` was reading OOM verdicts from the wrong field and would have reported zero OOMKills for the whole wave** | Found while checking wave 1's first two deaths. `05_oomkills.tsv` read `.status.containerStatuses[0].lastState.terminated.reason`. These pods run under `restartPolicy: Never`, so an OOMKilled container is never restarted and its verdict stays in **`state.terminated`**; `lastState` is where the reason moves only *after* a restart, and here it is `{}` for every pod. Verified directly on `rs-p95-143`: `state.terminated` = `{"reason":"OOMKilled","exitCode":137,...}`, `lastState` = `{}`. The script would therefore have emitted an empty reason for all 14 pods, `grep -c OOMKilled` would have written **0**, and the memory column of the T8 confusion table would have read as "no violations observed" on a wave where violations demonstrably did occur -- a false negative in the evidence rather than in the method. Fixed to emit `state.terminated` with `lastState` as fallback, so the file is correct under either restart policy. Worth noting for the report that this is the failure mode the testbed exists to catch: the offline definition said these VMs violate, the kernel agreed, and only the *instrumentation* disagreed. |
| 2026-09-07 | Added `testbed/sample_cgroups.py` -- per-pod `memory.current` / `memory.peak` sampled while a wave runs | An OOMKilled pod takes its own cgroup with it: the kubelet removes it shortly after termination, so by the time `make evidence` runs, the only surviving fact is the OOMKilled reason -- not how close to its limit the pod actually ran, which is the quantity that says whether the margin is comfortable or lucky. The sampler polls every replay pod's `memory.current` and `memory.peak` (kernel-maintained high-water mark) once a second in a single `docker exec` per poll, writing TSV to `evidence/`, and exits when every pod reaches a terminal phase. Caveat to carry into any reading of this file: cgroup v2 counts reclaimable **page cache** in `memory.current`, and the agent reads its trace CSV through that cgroup, so `memory.peak` overstates the anonymous footprint that actually drives an OOM -- it is an upper bound on pressure, not a measure of live heap. |

| 2026-09-07 | **Measurement artifact found: `memory.peak` is limit-dependent, so a margin probe run with generous headroom systematically overestimates overhead** | The 59.11 MiB the margin band was derived from is too high, and the reason is a property of how the kernel accounts memory, not a mistake in the probe's arithmetic. `testbed/margin_probe_full.py` ran VM 324 under `--memory=1024m` deliberately, so that the probe could not itself OOM and truncate the measurement. But cgroup v2 reclaim is *triggered by the limit*: page cache and other reclaimable pages are only evicted as the cgroup approaches its own ceiling, so a cgroup with 1024 MiB of room accumulates footprint that the same workload under a 208 MiB limit would have had reclaimed out from under it. `memory.peak` therefore measures the workload **and the headroom it was given**. **Evidence, same VM, same trace, same agent, only the limit differs:** VM 324 standalone under `--memory=1024m` peaked at **185.14 MiB**; in-cluster under its real 208 MiB limit it peaked at **175.6 MiB** -- 9.5 MiB lower. Against a peak scaled demand of 125.95 MiB that is an overhead of 59.11 MiB standalone versus **49.70 MiB** in-cluster. **Correct method, for anything measuring headroom this way in future: measure under the limit the workload will actually run with, or measure in-cluster.** A probe given room to breathe answers a question nobody asked. This is the third distinct mechanism found in this margin (after the `MemLoad` transient double allocation and the allocator retention tail) and the only one that was an error in the *instrument* rather than in the workload. |
| 2026-09-07 | **Flavour-independence of the memory margin confirmed -- 47.5-49.9 MiB across a 4x buffer range. One constant covers the catalog.** | Reported as a positive result, not merely as a refuted alternative. The worry was that retention might scale with buffer size, which would have made the band's ceiling flavour-dependent and forced a per-flavour re-derivation before waves 2-5 -- a real risk given that 26 of the 50 subset VMs (52%) sit at a scaled budget above the 128 MiB flavour the margin was measured on (subset flavour mix: 24 VMs at 2 GB/128 MiB, 6 at 4 GB/256 MiB, 14 at 8 GB/512 MiB, 6 at 16 GB/1024 MiB). Measured directly in-cluster, as kernel `memory.peak` minus peak scaled demand, across five wave-1 VMs spanning three flavours: **324** (2 GB) 49.70, **971** (2 GB) 49.90, **142** (4 GB) 49.45, **1229** (4 GB) 47.48, **344** (8 GB) 49.51 MiB. Spread **2.4 MiB across a 4x range of buffer sizes** -- flat, not proportional. The overhead is a property of the agent (interpreter, allocator arenas, the transition transient), not of the size of the buffer it manages, which is what an additive rather than multiplicative margin already assumed. **Consequence: a single margin constant is valid across the whole flavour catalog**, `gen_manifests.py` needs no per-flavour table, and the earlier `WORKER_OVERHEAD_MIB` design -- keyed by worker pool width -- was keying on the one dimension that does vary while the dimension that does not (buffer size) was never the problem. Also note what this does *not* license: it was measured at CpuLoad pool width 1, and `MEASURED_POOL_WIDTH` still refuses generation outside that regime. |
| 2026-09-07 | Three false negatives at M = 80 (**971, 332, 1229**), attributable to the probe artifact rather than to the method -- error direction is conservative for the thesis | With the corrected in-cluster constant (~49.5 MiB rather than 59.11), every VM's `need` drops by ~9.6 MiB and the band edges move with it: floor 57.06 -> ~47.5, ceiling 82.66 -> ~73.1. **M = 80 therefore sits above the corrected ceiling**, and exactly three subset VMs lie between the two thresholds: **971** (need 86.42 -> 76.81), **332** (82.66 -> 73.05), **1229** (82.66 -> 73.05). All three are offline-predicted memory violators that the kernel did not kill -- false negatives. Confirmed by observation, not inferred: 971 completed with `memory.peak` **205.2 MiB against its 208 MiB limit** (2.8 MiB of headroom -- it survived by less than one allocation), 1229 completed at **327.0 against 336**. **Why this is acceptable and was not corrected mid-run:** (1) the margin is held fixed at 80 across all five waves so the waves stay comparable -- re-deriving it mid-run would make wave 1 incommensurable with waves 2-5 and is exactly the post-hoc adjustment the pre-registration exists to prevent; (2) the error direction is **conservative for the thesis** -- the claim under test is that right-sizing causes violations that tools stay silent about, and a false *negative* under-counts observed violations, so the testbed understates the very effect being argued for rather than manufacturing it; (3) all three are in wave 1 by construction, because `plan_waves.py` pins every VM within `--boundary-mib` of the flip to the first wave, so **waves 2-5 contain no VM within 40 MiB of the threshold** and are unaffected. Marked in the T8 confusion table by an `artifact_affected` column (`testbed/wave_report.py`, `--probe-overhead-mib` / `--observed-overhead-mib`), which flags any VM whose verdict differs between the two constants and prints the memory and CPU confusion matrices twice -- all pods, and excluding artifact-affected -- so the table can be read both ways. Excluding them measures the method; including them measures the testbed as actually run. |

| 2026-09-08 | **Wave 1 ran to completion at M = 80 -- 14/14 pods terminal, 4 OOMKilled, 10 Completed, zero evictions, zero false positives on memory** | First wave to finish without a false-positive OOMKill after three that did not. Applied 18:48Z, last pod terminal 04:2xZ. Memory confusion vs the kernel, **excluding the three artifact-affected VMs: TP 4, FN 0, FP 0, TN 7 (n=11)** -- exact agreement between `src/baselines.py`'s any-single-sample memory rule and the kernel's own OOM killer. Including them: TP 4, FN 3, FP 0, TN 7 (n=14). **324 survived**, completing all 2591 samples with `memory.peak` 175.6 MiB against its 208 MiB limit (84.4%) -- the outcome the margin work was aimed at. True positives 143, 215, 126, 340 died at samples 1/2576, 30/5242, 396/2576 and 1785/2580 respectively; 143 and 340 reached exactly 100.0% of limit at the sampler's resolution. Node `MemoryPressure` never fired and **0 pods were evicted**, so no pod needed excluding from the table on eviction grounds -- the 30% headroom in `plan_waves.py` did its job. `replay.jsonl` captured for 14/14 pods before teardown. Artifacts archived under `evidence/wave1/` (the collector writes fixed filenames, so each wave is archived to its own subdirectory before the next runs); per-VM table in `results/wave1_outcomes.csv`. |
| 2026-09-08 | **The CPU column of the wave-1 confusion table is not trustworthy at S = 1/16 -- the agent cannot keep pace with its own trace, so `cpu_exhausted` measures the testbed, not the workload** | Flagged rather than smoothed over. CPU confusion for wave 1 was **TP 7, FN 1, FP 5 (n=14)** -- five VMs where the cluster reported a CPU violation the offline run did not predict. The cause is visible in the JSONL: at a 1/16 CPU limit the replay agent runs far behind wall-clock, and the lag does not stay bounded over thousands of samples the way it appeared to over the 99-sample Test B window. VM 332 took **~9.3 hours of wall time to replay a trace spanning 2591 one-second slices**, VM 344 similar; both eventually caught up (rates recovered to 2-4 s/sample once the wave thinned out from 14 pods to 3, confirming the lag is contention- and quota-driven rather than a hang). Because `cpu_exhausted` is raised on a deadline overrun, sustained lag manufactures CPU-exhaustion flags that reflect the agent's inability to finish its slice under a 0.0625-core quota, not the VM's demand exceeding its allocation. **Consequence for the report:** the memory side of T8 is kernel-enforced and sound (an OOMKill is unforgeable), but the CPU side at this scale measures the testbed's own pacing and should be reported as such -- either with the caveat attached, or re-run at a scale where the agent keeps up. This does not affect the memory findings or the margin, and it does not touch the violation definitions in `src/baselines.py`, which are unchanged. |

| 2026-09-08 | **CPU rescored from kernel throttle counters instead of deadline overruns -- does NOT separate either. Verdict: memory validated, CPU not validated at S = 1/16.** | Tested the hypothesis that `throttled_periods`/`throttled_us` (from the container's own `cpu.stat`, kernel-accounted and independent of whether the agent keeps pace) could do for CPU what OOMKill does for memory. Offline rescoring of wave 1's captured JSONL only -- no re-run, no agent change: `testbed/rescore_cpu.py`, per-VM table in `results/wave1_cpu_rescore.csv`. Four scorings, all under `src/baselines.py`'s 2-consecutive-sample rule: `deadline` (`cpu_exhausted`, the current signal), `any` (`throttled_us > 0`), `saturated` (throttled in every 100 ms period of the slice), `heavy` (`throttled_us` exceeding the wall-clock slice). **Confusion tables (all pods, n=14):** deadline 7/1/**5**/1, any 7/1/**5**/1, saturated 7/1/**3**/3, heavy 7/1/**3**/3. Excluding the 4 pods whose CPU evidence is censored by an early OOMKill (n=10): deadline 6/0/**3**/1, any 6/0/**3**/1, saturated 6/0/**2**/2, heavy 6/0/**2**/2. So the stricter throttle scorings do cut false positives (5 -> 3, or 3 -> 2), but **they do not separate the classes**, and the residual false positives are fatal rather than marginal. **The decisive evidence is an inversion, not a correlation:** VM **332** never exceeds its CPU allocation in any of 2591 samples (peak demand 0.0269 of a 0.0625-core limit, 43%) yet is throttled in **97.7%** of slices -- the *highest* throttle rate of any uncensored pod in the wave -- while VM **951**, which genuinely exceeds its allocation (peak 0.1331 cores, 213% of limit), is throttled in only **11.4%**. The pod that most exceeds its CPU allocation is throttled least; a pod that never exceeds it is throttled almost always. No threshold on a monotone throttle statistic can fix that ordering. **Mechanism:** at S=1/16 the CPU limit is 0.0625 cores = 6250 us of quota per 100 ms period, and the agent's *non-CPU* work -- `MemLoad` touching every page of a buffer up to hundreds of MiB each sample, plus JSON logging and interpreter overhead -- exceeds that quota on its own. The cgroup is therefore throttled as a function of how much **memory** work the trace demands, not how much CPU. Across the 10 uncensored pods, throttle rate correlates more strongly with peak memory demand (**+0.553**) than with CPU demand relative to limit (**+0.407**), and both are weak -- consistent with throttling being driven by a confound rather than by the quantity being measured. **Conclusion for the write-up: the memory side of T8 is validated (kernel-enforced, unforgeable, TP 4 / FN 0 / FP 0 / TN 7 excluding artifact-affected), and the CPU side is NOT validated at this scale.** Both candidate CPU signals are harness artifacts: `cpu_exhausted` measures the agent failing to meet a deadline, and throttling measures the memory occupant consuming a CPU quota shrunk 16x while the fixed per-sample memory work was not shrunk with it. State it as a scale limitation with the mechanism, not as a null result about the method. Validating CPU would need a testbed where the CPU quota is not the binding constraint on the agent's own bookkeeping -- i.e. a larger scale, or a memory occupant whose per-sample page-touching cost is decoupled from the CPU limit. `src/baselines.py` untouched; the CPU violation definition is not in question, only our ability to observe it here. |

| 2026-09-08 | **Waves 2-4 complete. Pooled memory result across waves 1-4: 37 VMs, TP 20 / FN 0 / FP 0 / TN 17 -- 100% agreement with the kernel.** Run paused before wave 5; nothing left running. | Wave 2 (8 pods): memory TP 5 / FN 0 / FP 0 / TN 3, 5 OOMKilled all at 100.0% of limit. Wave 3 (9 pods): TP 4 / FN 0 / FP 0 / TN 5, 4 OOMKilled. Wave 4 (9 pods): TP 7 / FN 0 / FP 0 / TN 2, 7 OOMKilled. **Pooled waves 1-4, excluding the 3 artifact-affected VMs: n=37, TP 20, FN 0, FP 0, TN 17 -- accuracy 100.0%.** Every wave: node `MemoryPressure` never fired, **0 evictions**, and `replay.jsonl` verified non-empty for every pod *before* teardown. Per-wave artifacts in `evidence/wave1..wave4/` (the collector writes fixed filenames, so each wave is captured into its own subdirectory), per-VM tables in `results/wave{1,2,3,4}_outcomes.csv`. **Pooled CPU across waves 1-4 was TP 16 / FN 2 / FP 13 / TN 6** -- reported for completeness only; see the rescoring row above for why the CPU axis is not validated at S=1/16 and must not be read as a method result. **Censoring worth stating in the write-up:** a pod OOMKilled early has truncated CPU evidence, and the extreme case is wave 4's VM **296**, whose log contains only the `start` record -- it was OOMKilled before completing a single sample. Its *memory* verdict is still kernel-attested and sound (the OOMKill is the measurement), but it contributes no CPU evidence at all. This asymmetry is intrinsic to the design rather than a defect: the memory rule is terminal, so the violation ends the observation, while the CPU rule is degrading and needs the observation to continue. **Wave 5 not run** -- paused at the user's request with the cluster left up and all replay pods deleted. Wave 5's VM list is on disk at `testbed/wave5_vms.txt` (189, 199, 273, 297, 348, 387, 641, 883, 980, 1228; 4384.0 Mi, 2.19 vCPU, 33.7% headroom) alongside the full `testbed/wave_plan.json`, so resuming needs nothing from the session that planned it. |

| 2026-09-09 | **Wave 5 complete; run finished. Final T8: 50 VMs, and `peak scaled demand > enforced limit` predicts the kernel's OOMKill with ZERO mismatches across all 50.** | Wave 5 (10 pods): memory TP 6 / FN 1 / FP 0 / TN 3, 6 OOMKilled, 0 evicted, `replay.jsonl` non-empty for 10/10 verified before teardown. Pooled over all five waves: **all 50 pods TP 26 / FN 4 / FP 0 / TN 20 (92.0%); excluding the 4 margin-band VMs, TP 26 / FN 0 / FP 0 / TN 20 (100.0%)**. Zero false positives in every wave, zero evictions, node `MemoryPressure` never fired. Deliverable: `evidence/T8_confusion.md`; per-VM CSVs `results/wave{1..5}_outcomes.csv`; per-wave artifacts `evidence/wave{1..5}/`. |
| 2026-09-09 | **The false negatives are a margin-band artifact, not a disagreement between the offline definition and the kernel -- and the earlier `artifact_affected` criterion under-counted them (3 of 4).** | Found when wave 5's VM **297** came out a false negative despite not being flagged: predicted violation (peak scaled demand 190.29 MiB vs a 128 MiB recommendation), ran all 2577 samples, `memory.peak` reached **exactly 208.0 MiB = 100.0% of its limit**, and survived. The explanation is structural. The offline rule compares demand against the **recommendation**; the cluster enforces **recommendation + M**, because the agent's own footprint has to fit in the pod beside the replayed demand. A VM whose peak demand falls in that band exceeds its recommendation but never exceeds what the kernel enforces, so the kernel has no reason to kill it -- its outcome is decided by M, a harness property, not by the recommendation under test. Checked across all 50 VMs: the band `budget < peak demand <= budget + 80` contains **exactly 4 VMs -- 297, 332, 971, 1229 -- and those 4 are exactly the run's 4 false negatives**, with no VM in the band OOMKilled and no VM outside it misclassified. Comparing like with like, **`peak scaled demand > enforced limit` reproduces the kernel's verdict on 50/50 VMs, 0 mismatches**. So the testbed does not show the definition and the kernel disagreeing; it shows a shifted threshold and four VMs that this testbed cannot adjudicate. The direction is conservative: missed violations, so the cluster under-counts the violations right-sizing causes rather than inventing any. **This supersedes the earlier `artifact_affected` flag** (which asked whether a VM's verdict flips between the probe overhead constant 59.11 and the in-cluster 49.5): that criterion caught 332, 971 and 1229 but missed 297, whose `need` exceeds M under both constants. The margin-band criterion follows from the design rather than from a measured constant, needs no overhead estimate at all, and catches all four. Both columns are kept in the per-VM CSVs (`margin_band` and `artifact_affected`) and `evidence/T8_confusion.md` reports the confusion table over all three populations (n=50 all, n=47 old criterion, n=46 margin-band) so the table can be read any of the three ways. **Also worth a line in the paper:** 297 reaching 100.0% of its limit *and surviving* while 387 and 883 reached 100.0% and *died* shows directly that `memory.peak == limit` does not imply an OOMKill -- the OOM killer fires only when reclaim fails, and page cache from reading the trace file is reclaimable. This is the same limit-dependence that made the standalone probe overestimate the margin, seen from the other side, and it further qualifies the flavour-independence result: the 47.5-49.9 MiB figures were measured on pods with slack; a pod pressing its limit has its overhead compressed toward the irreducible anonymous footprint. |
| 2026-09-09 | **Report artifacts assembled; `ARCHITECTURE.md` and `docs/DATA_QUALITY.md` brought to final state. No code or behaviour changed.** | Wrote `evidence/REVIEW1_ARTIFACTS.md` — per-rubric-component index of which file evidences what. Created the one missing section-3 artifact, `evidence/03_image.txt`: `collect_evidence.sh` never had a capture step for the image build/load slot (it writes `03_pods.txt` at that number), so this was captured post-run against the still-running cluster — Docker image `sha256:fdbafcda…` built 2026-09-07T15:06:19Z, the same image in the kind node's containerd as `de36bb7f…` (which is the `Image ID` in every pod describe across all five waves), and a sha256 match between `/app/replay_agent.py` inside the image and `testbed/replay_agent.py`. The two IDs differ only because `kind load docker-image` re-serialises through an OCI archive; sizes agree (46,188,607 B vs 46.2 MB). Docs updated for: the margin-band criterion superseding `artifact_affected`, the 50/50 like-with-like result, the page-cache/limit-dependence qualification on flavour-independence, the CPU censoring structure, and the 5-wave S=1/16 run shape. Three stale figures corrected in `DATA_QUALITY.md`: pattern 2 is **49** VMs not 40 (40 was the count the glitch check *missed*; verified against `results/bad_capacity_characterisation.csv`, which classifies all 103 as 54 `mostly_valid_bad_first_row` + 49 `intermittent_zero`), and the "short span dropped 43→42" sentence now says explicitly that the floor gate later took it to 6. **Independently re-verified before writing:** TP 26 / FN 4 / FP 0 / TN 20; margin band = exactly {297, 332, 971, 1229}; `(need_mib − 59.11) > margin_mib` reproduces `obs_oomkilled` on 50/50 rows. **Trap found while verifying:** `need_mib` in `results/wave{N}_outcomes.csv` is peak scaled demand **plus the superseded 59.11 MiB probe constant**, minus budget — not a plain excess-over-budget. Taken at face value it inverts the like-with-like check (the 4 band VMs read as mismatches). Recorded in `evidence/REVIEW1_ARTIFACTS.md`. Tests re-run: 35/35 and 3/3. |
| 2026-09-09 | **Section 3 closed. `excess_over_budget_mib` added, test-count wording de-staled, `results/confusion_table.csv` created.** | (1) `testbed/wave_report.py` now emits `peak_demand_mib`, `budget_mib` and `excess_over_budget_mib` — plain peak scaled demand minus scaled budget, **no overhead constant** — so the 50/50 like-with-like result is re-derivable without knowing about the superseded 59.11. `need_mib` kept unchanged for provenance (it is what `wave_plan.json`'s packing and the original `artifact_affected` flag were computed from); its contents documented in the module docstring and in a new sidecar `results/wave_outcomes_COLUMNS.md`. The five existing CSVs were **backfilled additively** rather than regenerated — no pre-existing value touched — and the backfill was checked against `need_mib − 59.11` on all 50 rows before acceptance (exact on all 50). Verified the new column alone reproduces all three findings: `excess > 0` → `pred_mem_violation` 50/50; `excess > margin_mib` → `obs_oomkilled` 50/50, zero mismatches; `0 < excess <= margin_mib` → the band {297, 332, 971, 1229}. (2) `CLAUDE.md` and `TASKS.md` no longer name a test count that goes stale — the invariant is now "passes in full and never shrinks", with 38 (35 + 3) as a dated observation. The "never edit a test to make it pass" invariant is untouched. (3) `results/confusion_table.csv` created via a new `--csv-out` flag on `testbed/t8_report.py`, so the machine-readable and narrative versions share one generator and cannot drift. 13 rows: one per (scope, population, axis, comparison), with `disagreeing_vm_ids` per row. Every count matches `T8_confusion.md` exactly. **CPU rows carry `status = CENSORED -- not a result`** with the censoring counts in `notes`, so the FP=18 cannot be quoted innocently. Regenerating `T8_confusion.md` after the backfill produced a **byte-identical** file, confirming the added columns changed nothing. Tests: 35/35 and 3/3. |
| 2026-09-09 | **Report figures generated: `src/make_figures.py` -> `figures/` (6 figures, PNG + SVG + CAPTIONS.md). NEW DEPENDENCY: matplotlib.** | `matplotlib>=3.8` installed and added to `requirements.txt`, marked figures-only (the pipeline and `replay_agent.py` do not need it). Added a `make figures` target. Every plotted value derives from a CSV; two derivations are self-checking and **raise rather than plot** if they disagree with published results: pooled reclamation % is re-derived per VM and reconciled against all four scope/method rows of `baseline_summary.csv`, and the exclusion cascade is re-derived by replaying `load_vm`'s gate ORDER over the per-gate scan CSVs (the scans are NOT disjoint -- the naive row counts are 103/450/4, the gate-order counts are 103/347/2/6) with the surviving set checked to equal `dataset_report.csv` exactly. Figure 5 additionally refuses to render if the like-with-like rule shows any mismatch. **Finding that contradicts a stated result:** the brief for figure 6 was "idle inflates CPU savings and does not inflate memory". On the current 792-VM population that is false for `peak` -- idle inflates memory by **+45.3pp** (63.5 vs 18.3), slightly MORE than it inflates CPU (+40.5pp). It holds only for `p95`, where the memory gap narrows to **+7.2pp** against a CPU gap of +38.4pp. Further, `docs/DATA_QUALITY.md` example C states idle *depresses* p95 memory reclamation (62.81 vs 68.48 on the old 1102-VM population); on the 792-VM population the sign is **flipped** (76.5 vs 69.3). Example C's numbers are pre-pattern-4 and are labelled as such in that document, but the stated *direction* no longer holds and should not be repeated in the paper without recomputation. The figure is titled to what the data shows, not to the brief, and the discrepancy is recorded in `figures/CAPTIONS.md`. Also fixed: `--only` used to rewrite `CAPTIONS.md` from a partial run, silently dropping the other captions; it now leaves the file alone. Stale "16/16" in the Makefile help text corrected to match CLAUDE.md. Tests: 35/35 and 3/3. |
| 2026-09-09 | **Worked example C rewritten on the live 792-VM population; the ">100% CPU" count audited across all 1250 files and fully reconciled (no bug).** | (1) `docs/DATA_QUALITY.md` example C now reports the live population: pooled memory reclamation idle vs non-idle is **63.55 / 18.27 (peak)** and **76.52 / 69.32 (p95)** — idle inflates BOTH, contradicting the previous "idle slightly depresses p95" claim, which was computed pre-pattern-4 on 1102 VMs and is now kept only as a labelled illustration of how large the pattern-4 effect was. **Root cause of the reversal, derived rather than asserted:** the 347 below-floor VMs are disproportionately idle (idle fell 698→425 on exclusion, non-idle only 404→367) and contributed *negative* reclamation via `snap_up`; removing them lifted idle p95 from 62.81% to 76.52%. **Second finding, on the current population:** the pooled and unweighted statistics still disagree in direction (pooled idle 76.52 > non-idle 69.32; unweighted idle 49.60 < non-idle 56.84). Cause is the catalog floor crossed with VM size — mean per-VM p95 reclamation rises 25.9%→96.0% across size buckets for idle but only 27.1%→65.7% for non-idle, and idle VMs cluster small (median 4.0 GB vs 15.6 GB; 216 of 425 below 4 GB), so pooling by GB-hours promotes the few large idle VMs. Weighting, not data, decides the direction; only the pooled figure is reported. The pathology is gone: worst per-VM figure is now **-94.12%**, not -419,749%, and no VM in `per_vm_results.csv` is below the 2 GB floor. (2) **`results/idle_vs_nonidle_memory_reclamation.csv` was stale** — 3306 rows = the old 1102-VM population, i.e. the data example C's wrong direction rested on. Regenerated from `per_vm_results.csv` (2376 rows = 792 x 3). (3) Figure 6's caption no longer claims a contradiction with example C; it now points at it. (4) **>100% CPU count audited by re-scanning all 1250 traces** with `inspect_traces.py`'s own definition (`max(usage/row-0 capacity) > 1.0`). Reconciles exactly: **174** across all files with a computable ratio (the 103 bad-capacity VMs have `cap_mhz = 0`, so the ratio is undefined and they are not counted at all) → minus `752.csv`, short-span → **173** on the pre-pattern-4 1102-VM population → minus the 9 span-valid below-floor VMs (389, 859, 902, 973, 978, 1025, 1188, 1202, 1214) → **164** on the 792 usable set, matching `dataset_report.csv` and `evidence/02_preflight.txt`. Every one of the 10 removed is an exclusion, none is a recount: no bug. |
| 2026-09-09 | **`--unsnapped` added to `baselines.py`; flavour-quantisation table recomputed on the 792-VM population. Narrowing is 17.0% / 25.3% (was 15.8% / 25.1%).** | `recommend()` takes `snap=True` by default and returns the raw continuous max/p95 when false; `--unsnapped` threads it through and stamps `"snapped_to_catalog": false` into `run_config.json`, prints a three-line warning, and is documented as a diagnostic counterfactual rather than a policy (CLAUDE.md invariant 5 untouched — snapping is still the only mode any reported result uses). **Verified the default path is unchanged**: re-ran the normal baseline and diffed `per_vm_results.csv` and `baseline_summary.csv` against the pre-change files — byte-identical. Counterfactual written to `results/unsnapped/`. New table (pooled memory reclamation, 792 VMs): all_vms snapped 33.03/71.67 vs unsnapped 52.67/84.75, gap 38.64→32.08pp, **narrowing 17.0%**; non_idle snapped 18.27/69.32 vs unsnapped 41.07/79.18, gap 51.05→38.11pp, **narrowing 25.3%**. `results/memory_snap_comparison.csv` regenerated. **Worth noting:** the *unsnapped* figures barely moved between the 1102- and 792-VM populations (52.66→52.67, 84.75→84.75, 41.12→41.07, 79.19→79.18) while the *snapped* ones moved several points — the pattern-4 signature, since the 347 below-floor VMs were distorted specifically by `snap_up` forcing them upward. Also flagged in the doc: only the reclamation columns of an unsnapped run are meaningful; allocating exactly the observation p95 leaves ~5% of samples above allocation by construction, so its `pct_vms_safe` reads 10.61% against 63.26% snapped — the counterfactual behaving as defined, not a finding. Stale 15.8%/25.1% corrected in `ARCHITECTURE.md` and in the preflight-facts summary below. New section "Pooled versus unweighted reclamation" added to `docs/DATA_QUALITY.md` recording the weighting finding, the size-bucket mechanism, and the project's convention (pooled, GB-hour-weighted, used consistently everywhere). Tests: 35/35 and 3/3. |

## Testbed scale — stated limitation for the report

The cluster run replays the 50-VM subset at **S = 1/16** of the offline
recommendation and demand (both scaled together, so every VM's
demand-to-limit ratio — the thing that determines whether a violation fires —
is preserved exactly; see the two deviation rows above for the derivation
and the measured 29.5 MiB baseline RSS that ruled out 1/32). Consequently:

- The cluster run validates **enforcement and violation timing** — does the
  kernel actually OOMKill / throttle when the offline definition says it
  should, and does the CPU 2-consecutive-sample rule versus the memory
  any-single-sample rule play out as designed.
- It does **not** validate absolute reclaimed capacity. vCPU-hours and
  GB-hours reported in the paper come from the full-scale offline run
  (`results/per_vm_results.csv`, `results/baseline_summary.csv`), not from
  the cluster.
- Two sequential waves of 25 pods are used rather than one wave of 50,
  because even at 1/16 the subset needs 9.31 vCPU / 17.5 GB simultaneously —
  still more than most single-node kind clusters expose. State this as a
  testbed-scale limitation, not a change to the violation definitions in
  `src/baselines.py`, which are untouched.

## Dataset facts confirmed at preflight

Fill from `make inspect` — these override the assumptions in the docs.

- Files found / usable: 1250 / **792 usable**, 458 excluded (103 "bad CPU capacity", 347 "below flavour catalog floor", 2 "mid-trace capacity shift", 6 "short span" — see below and `docs/DATA_QUALITY.md`). This is the current, final figure as of 2026-09-07. It supersedes three earlier reported counts, in order: 1250/1250 (T1, before inspect_traces.py had any gates) → 1104/1250 (bad-capacity/short-span gates agreeing with baselines.py) → 1102/1250 (mid-trace-shift pattern decided) → **792/1250** (below-catalog-floor pattern decided).
- Median sampling interval: 300 s (5.0 min), over the 792 usable VMs
- Median span (days): 30.0 (min 30.0, max 30.0 among usable VMs)
- Observation / evaluation split used: 21 obs days / 9 eval days, by wall-clock time via `split_obs_eval()` (not row count — see deviations above)
- Memory-column cleaning rule applied: not yet applied as a transform for patterns 1/2 (open decision, see `docs/DATA_QUALITY.md`). Patterns 3 (mid-trace capacity shift) and 4 (below flavour catalog floor) are both decided and applied: excluded. Data-quality flag counts among the 792 usable VMs: 0 pinned at provisioned, 5 effectively-zero memory, 164 show CPU usage exceeding provisioned MHz. Idle split: 425 idle / 367 non-idle (was 698/404 before pattern 4 — most of the 347 excluded VMs were idle). **Findings worth a line in the paper**, all in `docs/DATA_QUALITY.md`: (1) one now-excluded VM (`314.csv`, span 0.69 days) had a corrupted first-sample "Memory capacity provisioned" reading (6,144 KB vs. ~8.39M KB in every other row of that file) that alone inflated the original 1250-VM fleet-wide mean memory utilisation from ~9.6% to a previously-reported 24.2%, and p95 from ~18.0% to 65.3%; (2) two VMs (`133.csv`, `295.csv`) had provisioned memory capacity that genuinely changed mid-trace (12.7x and 27.7x), contributing a combined 3.6-12.6% of T2's reclamation totals before being excluded; (3) 347 VMs (27.8% of the fleet) were originally provisioned below the flavour catalog's 2GB/1vCPU floor, making per-VM reclamation percentages meaningless for them (one VM: -419,749% of its own 58MB allocation) — now excluded, pooled/GB-hour-weighted figures used everywhere instead of unweighted per-VM means; (4) **17.0%/25.3%** (all_vms/non_idle) of the peak-vs-p95 memory reclamation gap is flavour-catalog quantisation, not workload behaviour — recomputed on the 792-VM population 2026-09-09 via `src/baselines.py --unsnapped` (was 15.8%/25.1% on the old 1102-VM population); a stated limitation for any future method comparison, including the planned EVT method `M(eps)`. (5) On the same 792 VMs, pooled and unweighted reclamation averages disagree in **sign** for the idle-vs-non-idle comparison — see "Pooled versus unweighted reclamation" in `docs/DATA_QUALITY.md`; this project reports pooled (GB-hour-weighted) everywhere. Full per-VM breakdowns: `results/bad_capacity_characterisation.csv`, `results/below_catalog_floor_scan.csv`, `results/memory_snap_comparison.csv`.

## T9 — M(eps) implemented (2026-09-22)

**What changed.** `src/baselines.py` gains the proposed recommender M(eps) as a
fourth policy alongside `as_provisioned`, `peak` and `p95`: `fit_gpd_tail`,
`tail_exceedance_prob`, `horizon_violation_prob`, `evt_required_capacity`,
`evt_allocation`, `recommend_evt`, and a `policy="evt"` branch in `recommend()`.
`scipy.stats.genpareto` is now imported by `baselines.py` (already an approved
dependency; `requirements.txt` unchanged). New tests in `tests/test_evt.py`,
added to the `make test` target. `tests/test_baselines.py` is untouched.

**Why it is opt-in.** M(eps) runs only under `--evt`. Without the flag the
three-baseline run is byte-identical to the Review 1 result — verified by
diffing `per_vm_results.csv` and `baseline_summary.csv` against the committed
`results/` (both identical). Review 1 evidence is therefore not disturbed by
this change.

**Why `evt_required_capacity` inverts the horizon formula instead of scanning
the catalog.** One computation serves both the snapped and the unsnapped (T12)
paths, so the two can never drift apart. `snap_up` of the continuous answer is
provably the smallest catalog entry satisfying eps, because p(c) is
non-increasing in c — the empirical branch below the threshold and the GPD
branch above it are constructed to agree exactly at c = u (`rate * sf(0) =
rate`), which `tests/test_evt.py` checks over a 400-point grid.

**n_eval is derived per VM from the OBSERVATION window's median sampling gap**,
not from the evaluation window and not from the nominal 2,592. Sampling
intervals vary across this trace, so a fleet constant would be wrong for some
VMs; taking the count from the evaluation window would break invariant 6.
Observed range: 2,592 (median, the uniform-5-minute case) up to 10,099.

**Preliminary behaviour at theta = 1 — flagged, not worked around.** At
eps = 0.01 M(eps) reclaims **negative** capacity fleet-wide (-78.9% vCPU,
-100.3% memory): it upsizes 468 of 792 VMs on memory and 310 on CPU. Cause is
twofold and is the pre-registered H2 failure mode in `docs/HYPOTHESES.txt`
("treating a 9-day window as roughly 2600 independent draws will badly
overstate exposure"): with theta = 1 the per-sample target is
p = 1-(1-0.01)^(1/2592) = 3.88e-06, a 1-in-257,902-sample event, and
extrapolating a fitted GPD that far into the tail is violent where the shape is
large (fitted memory shape ranges to 1.77, mean 0.20; required capacity reaches
392,883x the observed peak). Loosening to eps = 0.4 moves reclamation in the
right direction (-18.5% vCPU, +6.4% memory) and raises violations, so the sweep
is monotone as designed, but M(eps) at theta = 1 is dominated by both baselines
across the whole range. This is a result about the uncorrected method, not a
defect to tune away, and it is the specific thing T11's dependence correction
exists to address.

**Open design question, deliberately not decided here.** Nothing caps an M(eps)
allocation at the VM's original allocation, so the recommender can return more
capacity than the VM already has. Demand is clipped to the original allocation
on load (`load_vm`), so such a VM cannot violate in the evaluation window
either — it just books negative reclamation. Capping at `a_orig` is arguable
(right-sizing should not up-size) but is not in the T9 specification and would
change the reported frontier, so it is left alone pending a decision.

## Cap at the original allocation (2026-09-22, before any frontier was run)

M(eps) allocations are now `min(snapped requirement, original allocation)`, per
resource. Rationale and the record that it was decided before T10 was ever
executed are in `ARCHITECTURE.md`, "The cap at the original allocation". In
short: `load_vm` clips demand to `a_orig`, so demand has bounded support there
and `P(demand > c) = 0` for any `c >= a_orig`; an uncapped GPD extrapolation
predicts mass the data model forbids. The GPD shape is left unconstrained and
its distribution is reported instead.

Effect at eps = 0.01: fleet reclamation goes from **-78.9% vCPU / -100.3%
memory** (uncapped, T9) to **+20.3% / +30.5%** (capped). The cap is doing a
great deal of work, which is itself the finding -- it says the uncorrected tail
model is badly mis-specified, not that the cap is a clever trick.

## T10 -- eps sweep and comparative frontier (2026-09-22, theta = 1)

`src/sweep_eps.py` (new) sweeps eps over {0.001, 0.005, 0.01, 0.05, 0.1, 0.2,
0.4, 0.6} and writes `results/frontier.csv`, `dominance.csv`, `gpd_shapes.csv`
and `sweep_config.json`; `src/plot_frontier.py` (new) draws
`figures/frontier_snapped.png` and `figures/frontier_unsnapped.png`. Console
output captured to `evidence/07_frontier_snapped.txt` and
`evidence/08_frontier_unsnapped.txt`. This is the **correction-off arm** of the
T12 ablation.

The GPD fit is eps-independent, so the sweep fits once per VM per resource and
only re-inverts per eps. Violation counting goes through the existing tested
`count_violations` with each resource's own `min_run`, so invariant 1 is not
re-implemented.

**Capped baselines added as a diagnostic.** M(eps) is capped; `peak` and `p95`
as published are not. Upward snapping pushes `peak` above the original
allocation on **210 of 792 VMs** on memory, booking negative reclamation that
the cap would remove. Capping `peak` lifts its memory reclamation from
**33.03% to 41.81%** with violations completely unchanged (a VM allocated at or
above its original cannot violate, since demand was clipped there). Comparing
capped M(eps) against uncapped `peak` therefore flatters M(eps) by ~8.8
percentage points on the memory axis. `peak_capped` / `p95_capped` rows are
emitted alongside, the dominance test is run against both, and the Review 1
baselines themselves are untouched. On CPU the correction is negligible
(+0.06 pp) and unsnapped it is exactly zero, confirming it is purely a
snapping artefact.

**H1 is NOT EVALUABLE at matched risk under theta = 1.** On all four
scope x resource combinations, and on both the snapped and unsnapped arms, the
M(eps) curve never reaches P95's measured violation rate -- so there is no
matched-risk point at which to compare, and invariant 7 forbids comparing at
matched eps instead. Snapped, all VMs: P95 sits at 0.0896 CPU / 0.3548 memory
measured risk, while M(eps) tops out at 0.0164 / 0.0821 at eps = 0.6.

The cause is structural, not a sweep-range oversight. With theta = 1 the
horizon formula maps eps to a per-sample target
`p = 1 - (1-eps)^(1/n_eval)`. At eps = 0.6 and n_eval = 2592 that is
p = 3.53e-04, a 1-in-2,829-sample quantile. P95 is p = 0.05, a 1-in-20
quantile. Reaching it would require
`eps = 1 - 0.95^2592 = 1 - 1.8e-58` -- indistinguishable from 1 in double
precision. **Under theta = 1 the entire usable eps range maps into quantiles
far more extreme than P95**, so no eps sweep, however wide, makes the
comparison possible. Extending the sweep past 0.6 would not help and was not
done.

What M(eps) does do at theta = 1: it extends the frontier **leftward into a
low-risk region no baseline occupies**. Between 0 and 0.064 measured memory
risk the only non-trivial operating points available are M(eps)'s. Where the
baselines actually sit, it does not beat them -- against like-for-like capped
baselines, `peak_capped` dominates M(eps) at eps = 0.4 (all VMs, memory) and at
eps = 0.2/0.4 (non-idle, memory), and `peak_capped` dominates on CPU at
eps = 0.6. No M(eps) point dominates any baseline on either arm.

**GPD shape, unconstrained, as fitted:**

| resource | n | median xi | max xi | xi < 0 | xi >= 0.5 | xi >= 1 |
|---|---|---|---|---|---|---|
| CPU | 652 | 0.549 | 16.583 | 139 | 345 | 152 |
| memory | 792 | 0.105 | 1.772 | 309 | 211 | 41 |

152 CPU fits have an infinite mean (xi >= 1) and one reaches 16.6. That is the
quantitative statement of tail mis-specification the cap was introduced to
contain, and it is the strongest available evidence that theta = 1 is the wrong
model rather than that EVT is the wrong tool.

Fallbacks are unchanged from T9: CPU 140/792 (17.7%, all `few_exceedances`),
memory 0/792. Cap hits fall as eps rises, from 495 to 253 of 792 on memory.

Suite: **38 -> 57 -> 63** (`test_baselines.py` 35, `test_replay_agent.py` 3,
`test_evt.py` 25). `test_baselines.py` remains untouched.

## Low-eps diagnostic (2026-09-23, before T11 was implemented)

`src/diagnose_low_eps.py` (new, `make diagnose`) re-derives the eps = 0.001 arm
and characterises the VMs that violated on memory anyway. Nothing in it changes
an allocation rule. Output: `evidence/09_low_eps_diagnostic.txt`,
`results/diag_low_eps_mem_violators.csv`, `results/diag_cpu_shapes_idle.csv`,
`results/theta_feasibility.csv`.

**21 of 792 VMs (2.65%) violate on memory at a 0.1% target -- 26x
anti-conservative. All 21 are explained by the observation window, not by the
tail fit being too loose:**

- **21 of 21 drift.** Every one has an evaluation-window peak above its
  observation-window peak, median ratio 3.43x and up to 18.6x. The recommender
  was never shown the level it was later asked to survive. Zero violators had
  `eval peak <= obs peak`.
- **19 of 21 have a negative fitted shape**, giving the GPD a *finite* upper
  endpoint `u - scale/xi`; **18 of those 19 violate above that endpoint**. The
  fitted model assigns those demands probability exactly zero, so no eps,
  however small, would have provisioned for them. The failure is not
  under-provisioning within the model -- it is the model being confidently
  wrong outside its support.
- **Idle split is essentially flat**: 10 idle (2.35% of 425) and 11 non-idle
  (3.00% of 367). This is not an artefact of idle VMs.
- 0 of 21 were capped at the original allocation and 0 fell back to peak, so
  neither of those mechanisms is implicated.

**CPU shape xi >= 1 against idle/non-idle.** The 152 infinite-mean CPU fits are
concentrated in the idle half: 117 of 420 fitted idle VMs (27.9%, median
xi 0.641) against 35 of 232 fitted non-idle VMs (15.1%, median xi 0.147). The
explosive CPU shapes are largely a property of near-flat idle series, where the
few samples above q0.95 carry no usable shape information.

**This is the finding that matters for the method:** at the low end, M(eps)
fails for a reason `theta` cannot fix. theta rescales the trial count; it does
nothing about non-stationarity or a mis-specified bounded tail. Lowering theta
makes the low end worse, as predicted -- see T11 below.

## Feasibility check before T11 (2026-09-23)

The horizon formula maps eps to a per-sample target
`p = 1 - (1-eps)^(1/(theta * n_eval))`. At the fleet median n_eval = 2,592
(range 2,592 to 10,099):

| theta | effective trials | p at eps = 0.6 | quantile | x more extreme than P95 | eps needed to reach p = 0.05 |
|---|---|---|---|---|---|
| 1.00 | 2592 | 3.53e-04 | 1-in-2,829 | 141.5x | 1 - 1.8e-58 |
| 0.50 | 1296 | 7.07e-04 | 1-in-1,415 | 70.7x | 1 - 1.3e-29 |
| 0.20 | 518 | 1.77e-03 | 1-in-566 | 28.3x | 1 - 2.8e-12 |
| 0.10 | 259 | 3.53e-03 | 1-in-283 | 14.2x | 0.9999983 |
| 0.05 | 130 | 7.05e-03 | 1-in-142 | 7.1x | 0.99870 |

P95 operates at a per-sample target of exactly 0.05, a 1-in-20 quantile.

**No sensible theta brings M(eps) within reach of P95's per-sample level.**
Even at theta = 0.05 -- an extremal index implying clusters of 20 samples, well
below anything the estimators actually return -- the loosest eps in the sweep
still targets a quantile 7.1x more extreme than P95's, and matching P95 would
need eps = 0.9987, i.e. an operating point that declares a 99.87% chance of at
least one violation. Holding eps = 0.6 and solving for theta instead gives
theta = 17.86/2592 = **0.0069**, which would mean each independent extreme
episode spans ~145 samples (12 hours) of a 9-day window.

**Consequence, stated before T11 was run:** H1 as pre-registered is not
evaluable for this method, and stays that way. H1 is not reformulated, the
comparator is not changed from p95, and the sweep is not extended past
eps = 0.6. That the method cannot be driven to the baseline's risk level is
itself the reportable result.

## 2026-09-23 -- TASKS.md gained its T9-T12 sections partway through T11

Stated plainly because it bears on how the pre-registration claims should be
read. `TASKS.md` ran T1-T8 and then "Not in scope yet"; it gained its Phase 2
block (T9, T10, T11, T12) **during T11, after T9 and T10 had already been run**.
Until then the work queue still listed implementing M(eps) as future work while
M(eps), the cap and the eps sweep all existed. The T9 and T10 entries in that
block are retrospective one-liners pointing at the write-ups below; the T11 and
T12 entries were written as the work was done.

**The decisions those sections describe were nonetheless fixed before the
results they govern**, and the evidence for that ordering is documentary, not
in the commit graph:

- **The cap at the original allocation.** `ARCHITECTURE.md`, "The cap at the
  original allocation", closes with: *"Decided 2026-09-22, before any frontier
  was computed. The eps sweep (T10) was first run only after this rule was in
  place and its tests passed."* The dated sections of this file corroborate the
  sequence independently: the T9 entry records the cap as an **open question,
  deliberately not decided** ("Nothing caps an M(eps) allocation at the VM's
  original allocation... left alone pending a decision"); the next entry,
  "Cap at the original allocation (2026-09-22, before any frontier was run)",
  decides it; the T10 entry, which contains the first frontier, comes after
  both.
- **The runs-declustering run length of 12.** Fixed before any T11 result
  existed, and recorded in three independent places written before the sweep
  was run: the comment on `EVT_DEFAULT_RUN_LENGTH` in `src/baselines.py`,
  `TASKS.md` T11, and an assertion in `tests/test_evt.py` that fails if the
  constant moves.

**What the commit history can and cannot show.** There is exactly one commit,
`202550e` (2026-09-22 08:29 +0530, "Offline right-sizing pipeline, baselines,
cluster testbed and Review 1 evidence"). Its tree contains no `sweep_eps.py`,
no cap and no theta estimation, so it bounds the whole M(eps) phase as
**after** Review 1 — but every subsequent step is still uncommitted working
tree, so **commit history does not order T9, T10 and T11 against each other**.
Anyone checking the pre-registration claims should read the dated entries in
this file and the `ARCHITECTURE.md` note above, not `git log`. Committing this
phase in the order the work was done would have made the ordering
independently verifiable; it was not, and that is a real weakness in the
provenance record rather than something the documentation fully repairs.

## T11 -- per-VM extremal index (2026-09-23)

**What changed.** `src/baselines.py` gains `exceedance_indices`,
`estimate_theta_intervals`, `estimate_theta_runs`, `estimate_theta` and the
constant `EVT_DEFAULT_RUN_LENGTH = 12`. `src/sweep_eps.py` gains
`--theta-method {fixed,intervals,runs}`, `--run-length`, an `attach_theta`
pass, and three new outputs (`theta_per_vm.csv`, `eps_calibration.csv`,
`exploratory_matched_risk.csv`). `src/plot_frontier.py` gains
`--series-label`, because a frontier drawn at an estimated theta is not the
same curve as one at theta = 1 and the two are routinely shown together.
`Makefile` gains `diagnose`, `sweep`, `theta`, `theta-sensitivity`.
`tests/test_baselines.py` untouched. As on every previous run, `make` is not on
PATH on this machine, so each target's commands were invoked directly (see the
2026-09-07 row of the deviations table); the targets exist so the numbers are
reproducible from the documented entry points.

**The intervals estimator is primary because it has no free parameter.** Runs
declustering does, so it is the sensitivity check only, and **its run length
was fixed at 12 samples (one hour) before any T11 result existed** -- recorded
in the constant's own comment, in `TASKS.md` T11, and asserted by a test.

theta is estimated per VM per resource on the **observation window only**, at
the same threshold u the GPD was fitted over. 140 CPU VMs have no usable tail
fit, so no u to decluster against; they fall back to peak regardless and take
theta = 1. No other VM failed to produce an estimate.

**Regression check:** `--theta-method fixed` reproduces `results/frontier.csv`,
`dominance.csv` and `gpd_shapes.csv` exactly, so T10 and T11 are two arms of
one code path rather than two implementations.

**Per-VM theta distribution (intervals, 792 VMs):**

| resource | n | min | q25 | median | q75 | max | median effective trials (theta * n_eval) |
|---|---|---|---|---|---|---|---|
| CPU | 652 | 0.0085 | 0.1431 | 0.2509 | 0.4273 | 1.0000 | 660 (from 2,592) |
| memory | 792 | 0.0098 | 0.1961 | 0.5617 | 0.7819 | 1.0000 | 1,456 (from 2,592) |

Runs declustering at r = 12 gives systematically lower medians (CPU 0.1880,
memory 0.3717) but the same picture; the frontier it produces sits within
1.6 percentage points of reclamation and 0.006 of measured risk of the
intervals frontier at every eps, so the choice of estimator is not what drives
the result.

**Frontier (snapped, all VMs, memory) -- T10 theta = 1 against T11 per-VM
theta:**

| eps | recl % (theta=1) | risk (theta=1) | recl % (T11) | risk (T11) |
|---|---|---|---|---|
| 0.001 | 28.64 | 0.0265 | 28.85 | 0.0290 |
| 0.010 | 30.50 | 0.0316 | 31.35 | 0.0391 |
| 0.100 | 33.85 | 0.0467 | 36.41 | 0.0694 |
| 0.600 | 42.43 | 0.0821 | 46.67 | 0.1187 |

The correction does what it was built to do -- it buys real reclamation
(+4.2 pp on memory at eps = 0.6, +7.2 pp on CPU) -- and it moves risk up with
it. **It does not rescue the low end.** At eps = 0.001 measured memory risk
rises from 2.65% to 2.90%, exactly as the diagnostic predicted: the low-end
failures are drift and bounded mis-specified tails, and theta only reduces the
trial count.

**Calibration (eps target against measured risk), all VMs, intervals:** the
method is anti-conservative below eps ~ 0.01 on CPU and below eps ~ 0.1 on
memory, and conservative above. Worst ratios: memory 29x at eps = 0.001
(measured 0.029 against target 0.001), CPU 5.05x. On non-idle VMs memory is
35x at eps = 0.001 and stays anti-conservative out to eps = 0.1. Full table in
`results/theta_intervals/eps_calibration.csv`.

**H1 remains NOT EVALUABLE.** On all four scope x resource combinations, on
both the snapped and unsnapped arms, and under both estimators, the M(eps)
curve still never reaches p95's measured violation rate (snapped, all VMs:
p95 sits at 0.0897 CPU / 0.3548 memory; M(eps) tops out at 0.0354 / 0.1187).
The feasibility arithmetic above says why, and says no theta the estimators
could plausibly return would change it. H1 is left as pre-registered.

**Dominance.** Against like-for-like capped baselines, `peak_capped` now
dominates M(eps) at eps = 0.2 and 0.4 on CPU (all VMs), at eps = 0.2 on CPU
(non-idle), at eps = 0.1 and 0.2 on memory (all VMs) and at eps = 0.05 and 0.1
on memory (non-idle). No M(eps) point dominates any baseline. The dependence
correction moved M(eps) into the region where `peak_capped` lives, and it
loses there.

### EXPLORATORY -- NOT PRE-REGISTERED

Reclamation at matched **measured** risk against `peak_capped`, the baseline
the frontier actually overlaps. This is *not* H1: H1 names p95, and
substituting a different comparator to manufacture a comparison is exactly
what invariant 7 forbids. It is reported as an observation about where the two
curves meet. Because the sweep samples eight eps values, the matched-risk
reading is a linear interpolation between the bracketing points, not a
measured operating point.

| scope | resource | peak_capped risk | peak_capped recl % | M(eps) recl % at matched risk | gap (pp) |
|---|---|---|---|---|---|
| all VMs | CPU | 0.0164 | 37.70 | 32.15 | **-5.56** |
| all VMs | memory | 0.0644 | 41.81 | 35.52 | **-6.30** |
| non-idle | CPU | 0.0273 | 20.49 | 18.34 | **-2.15** |
| non-idle | memory | 0.0708 | 30.97 | 27.36 | **-3.61** |

M(eps) reclaims **less** than `peak_capped` at the same measured risk on all
four, by 2.2 to 6.3 percentage points. Under runs declustering the gaps are
the same to within 0.3 pp (-5.21, -5.92, -2.38, -3.33), so this is not an
estimator artefact. Unsnapped the gaps narrow sharply on memory (-0.83 pp all
VMs, -1.07 pp non-idle) while widening on CPU (-14.06 pp, -6.07 pp), which
says most of M(eps)'s memory disadvantage against `peak_capped` is catalog
quantisation rather than the tail model -- consistent with the 17.0%/25.3%
quantisation share already measured for the peak-vs-p95 memory gap.

**Suite: 63 -> 74** (`test_baselines.py` 35, `test_replay_agent.py` 3,
`test_evt.py` 36). The 11 new tests check both estimators against
hand-computed values (two clusters of five, 996 samples apart: Ferro-Segers
gives 2*995^2 / (9*995*994) = 0.222446; runs at r = 12 gives 2/10 = 0.2
exactly), that evenly spaced exceedances clip to theta = 1, that the run
length is fixed at 12, and that a lower theta never raises an allocation.

## T12 -- ablation (2026-09-23)

**What changed.** `src/ablation.py` and `src/plot_ablation.py` (both new,
`make ablation`). `src/sweep_eps.py` gains `--no-cap`, has its allocation
closures (`baseline_allocs`, `provisioned_allocs`, `evt_allocs`) and its
frontier loop (`run_frontier`) lifted to module level so an ablation arm runs
the same code as the headline sweep, and gains
`matched_risk_reclamation`, which is now the single implementation of the
matched-risk reading used by both files. `tests/test_baselines.py` untouched.

**Bug found and fixed while wiring the threshold arm.** `sweep_eps.load_fleet`
hardcoded `EVT_THRESHOLD_Q` and `EVT_MIN_EXCEEDANCES`, so `--threshold-q` and
`--min-exceedances` were accepted, written into `sweep_config.json`, and
silently ignored. No published number is wrong -- every run so far used the
default q0.95, which is what the fit actually applied -- but a threshold
ablation would have produced three identical arms and been reported as
"threshold does not matter". Fixed, and `tests/test_evt.py` now pins that
`threshold_q` reaches the fit.

**The measured quantity.** Each arm flips exactly one switch against the same
reference and is scored on the same number: reclamation at **matched measured
risk** against `peak_capped`, minus `peak_capped`'s own reclamation
(`gap_pp`), and then relative to the reference arm's gap
(`shift_vs_reference_pp`). Reclamation alone would rank an arm that simply
took more risk as the winner, which is the failure mode this project exists to
correct. The reading is a linear interpolation between the two bracketing eps
points, not a measured operating point.

**Cross-check:** the `reference` arm reproduces
`results/theta_intervals/frontier.csv` and the `theta_off` arm reproduces
`results/frontier.csv` byte-identically, so the ablation is the same code path
as T10 and T11 rather than a second implementation of it.

**PRIMARY TABLE -- shift in matched-risk reclamation against `peak_capped`,
in percentage points. Positive means flipping that switch helped.**

| arm | switch | CPU all | CPU non-idle | mem all | mem non-idle |
|---|---|---|---|---|---|
| reference | intervals theta, snapped, capped, u = q0.95 | 0 | 0 | 0 | 0 |
| theta_off | dependence correction OFF (theta = 1) | **+0.70** | n/a | **+0.93** | **+0.16** |
| theta_runs | theta from runs declustering, r = 12 | +0.35 | -0.22 | +0.38 | +0.28 |
| snap_off | catalog snapping OFF *(baseline moves)* | -8.50 | -3.92 | +5.47 | +2.54 |
| cap_off | cap at original allocation OFF | **-47.03** | **-27.49** | **-67.84** | **-53.44** |
| u_q90 | GPD threshold at q0.90 | +1.20 | -0.72 | -0.56 | -1.41 |
| u_q98 | GPD threshold at q0.98 | -1.09 | +1.83 | +5.26 | +2.26 |

`theta_off` on CPU non-idle is **n/a**: at theta = 1 the M(eps) curve tops out
at 0.0218 measured risk, below `peak_capped`'s 0.0273, so there is no matched
point. That is the same unreachability that makes H1 unevaluable, showing up
inside the ablation.

**Which component causes the gain: the cap, and essentially nothing else.**
Removing it costs 47 to 68 percentage points. Every other switch except the
threshold moves the answer by under 2 pp. Uncapped, M(eps) does not merely
reclaim less -- it goes **negative**, to -14.9% vCPU and -32.3% GB-hours at
matched risk, because the fitted tail asks for more capacity than the VM was
ever provisioned and the method up-sizes. This is the quantitative version of
what the capped-VM counts have been saying since T9: the GPD extrapolation
leaves the support of the distribution it was fitted to on a large fraction of
VMs, and the cap is what contains it.

Read honestly, that is not a flattering result for M(eps). The cap is a
constraint from the data model, not a contribution of extreme-value theory. A
method whose entire measurable advantage comes from "do not allocate more than
the VM already had" has not yet demonstrated that the tail fit earns its
complexity. The like-for-like column (uncapped M(eps) against uncapped `peak`)
gives -52.5 / -29.5 / -65.4 / -44.3 pp, so the conclusion does not depend on
comparing a capped baseline against an uncapped method.

**The dependence correction does not improve the frontier.** `theta_off` has a
*positive* shift on three of four combinations: turning T11 OFF is worth
+0.70 pp (CPU all VMs), +0.93 pp (memory all VMs) and +0.16 pp (memory
non-idle). T11's headline gain -- +4.2 pp memory and +7.2 pp CPU reclamation
at eps = 0.6 -- was bought entirely with risk. At matched risk the corrected
and uncorrected methods are the same curve to within one percentage point, and
if anything the uncorrected one is marginally ahead. The extremal index is
still the statistically correct thing to do, and the estimates themselves are
sound (CPU median theta 0.25, memory 0.56); it simply does not move this
frontier. Reporting the reclamation gain without the matched-risk column would
have turned a null result into a claimed improvement.

`theta_runs` agrees with `theta_intervals` to within 0.4 pp everywhere, so the
choice of estimator is not a lever either.

**Threshold quantile is the only other switch with real sign, and it is not
consistent across resources.** u = q0.98 gains +5.26 pp on memory (all VMs) and
+2.26 pp (non-idle) but loses -1.09 pp on CPU (all VMs); u = q0.90 is worse
than the reference on three of four. A higher threshold fits a thinner, more
genuinely extreme tail, which helps where the memory series has one and hurts
on CPU where 140 VMs already have too few exceedances to fit at all. **q0.95
was fixed before any of this and is not being changed** -- picking q0.98 now,
on the basis of this table, would be fitting a hyperparameter against the
evaluation window and would break invariant 4. It is recorded as a direction
for future work on a separate dataset, not adopted.

**Snapping.** `snap_off` moves the baseline as well as the method
(`peak_capped` is itself unsnapped there), so its shift is not directly
comparable with the other arms' and is flagged as such in the table. The size
of the move is still informative: unsnapped, the memory gap against
`peak_capped` nearly closes (+5.47 / +2.54 pp) while the CPU gap widens
(-8.50 / -3.92 pp), consistent with the 17.0%/25.3% catalog-quantisation share
already measured for the peak-vs-p95 memory gap. Most of M(eps)'s memory
disadvantage is the flavour catalog, not the tail model.

**What no arm can fix, and the most important sentence in T12.** The low end
of the sweep fails for a reason that is not in the ablation list at all. At
eps = 0.001, **all 21 memory violators have an evaluation-window peak above
their observation-window peak** (median 3.43x, up to 18.6x), and **18 of the
19 with a negative fitted shape violate above the finite upper endpoint of
their own fitted GPD** -- demand the model assigns probability exactly zero.
That is non-stationarity, not tail mis-specification. Cap, snapping, theta and
threshold are all statements about how to extrapolate the observation window;
none of them makes the observation window describe a VM whose demand later
tripled. Lowering theta makes it strictly worse (2.65% -> 2.90% measured
memory risk at eps = 0.001), and raising the threshold does not help either,
because the problem is not where the tail was cut. **Any future work on the
low end has to address drift -- window length, re-fitting cadence, a
change-point test -- not the tail model.** Full evidence:
`evidence/09_low_eps_diagnostic.txt`.

**Figures.** `figures/ablation_cpu.png` and `figures/ablation_mem.png`, one per
resource, all seven arms overlaid on measured risk against reclamation, with
`peak_capped` marked. They support the table and are not the answer on their
own: `u_q98` and `theta_off` sit visibly above the reference curve while being
level with or behind it at matched risk, which is exactly the confusion the
matched-risk column exists to prevent. Colour is the validated categorical
palette in fixed slot order, one slot per arm, never cycled.

**Suite: 74 -> 79** (`test_baselines.py` 35, `test_replay_agent.py` 3,
`test_evt.py` 41). The five new tests pin the matched-risk reading against a
hand-computed frontier (two sweep points at risk 0.02/0.06 and reclamation
20/40, a baseline at risk 0.04 and reclamation 35, giving an interpolated 30
and a gap of -5), that a baseline outside the swept range returns NaN rather
than an extrapolation, that the comparator is selectable, and that
`threshold_q` reaches the fit.
