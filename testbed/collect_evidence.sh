#!/usr/bin/env bash
# collect_evidence.sh -- capture the artifacts the rubric asks for.
# Produces evidence/ containing proof the environment runs and that
# resource exhaustion was observed rather than simulated.
set -euo pipefail

OUT="${1:-evidence}"
mkdir -p "$OUT"
STAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "capture started $STAMP" | tee "$OUT/00_timestamp.txt"

# 1. The cluster exists and is Ready -- the literal artifact named in the rubric.
kubectl get nodes -o wide            > "$OUT/01_nodes.txt" 2>&1
kubectl version                      > "$OUT/02_version.txt" 2>&1
kubectl cluster-info                 >> "$OUT/02_version.txt" 2>&1

# 2. The workload pods exist, with the limits that encode the recommendation.
kubectl get pods -l app=rightsizing-replay -o wide \
                                     > "$OUT/03_pods.txt" 2>&1
kubectl get pods -l app=rightsizing-replay \
  -o custom-columns=\
'NAME:.metadata.name,METHOD:.metadata.labels.method,'\
'CPU_LIMIT:.spec.containers[0].resources.limits.cpu,'\
'MEM_LIMIT:.spec.containers[0].resources.limits.memory,'\
'PHASE:.status.phase' \
                                     > "$OUT/04_limits.txt" 2>&1

# 3. Memory exhaustion: OOMKills, the terminal violation.
#
# The verdict is read from state.terminated, NOT lastState.terminated. These
# pods run under restartPolicy: Never, so a container that is OOMKilled is
# never restarted: its final state stays in `state.terminated` and `lastState`
# is left empty ({}). Reading lastState -- which is where the reason moves
# only once a container has been restarted at least once -- reports an empty
# reason for every pod in the wave and silently counts zero OOMKills. Both are
# emitted below, state first with lastState as the fallback, so the file is
# correct under either restart policy.
kubectl get pods -l app=rightsizing-replay \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.metadata.labels.method}{"\t"}{.status.containerStatuses[0].state.terminated.reason}{.status.containerStatuses[0].lastState.terminated.reason}{"\t"}{.status.containerStatuses[0].state.terminated.exitCode}{.status.containerStatuses[0].lastState.terminated.exitCode}{"\t"}{.status.containerStatuses[0].restartCount}{"\n"}{end}' \
                                     > "$OUT/05_oomkills.tsv" 2>&1
grep -c OOMKilled "$OUT/05_oomkills.tsv" > "$OUT/05_oomkill_count.txt" || echo 0 > "$OUT/05_oomkill_count.txt"

# 4. A describe for one OOMKilled pod, as human-readable proof.
FIRST_OOM=$(awk -F'\t' '$3=="OOMKilled"{print $1; exit}' "$OUT/05_oomkills.tsv" || true)
if [ -n "${FIRST_OOM:-}" ]; then
  kubectl describe pod "$FIRST_OOM"  > "$OUT/06_describe_oomkilled.txt" 2>&1
else
  echo "no OOMKilled pod at capture time" > "$OUT/06_describe_oomkilled.txt"
fi

# 4b. Evictions vs OOMKills. Both end a pod, but only an OOMKilled container
# is a real memory violation -- a kubelet eviction under node MemoryPressure
# means the NODE ran short, not that this pod's own limit was exceeded. A
# pod's OOMKilled reason (step 3, above) comes from
# status.containerStatuses[].lastState.terminated.reason, which is set only
# by the kernel's own cgroup OOM killer; an evicted pod's containers never
# reach that state, so the two signals should not overlap -- checked below.
# Any pod that shows Evicted here must be excluded from the T8 confusion
# table and re-run, not counted as a memory violation.
kubectl get events --field-selector reason=Evicted -o wide \
                                     > "$OUT/07_evictions.txt" 2>&1

kubectl get pods -l app=rightsizing-replay \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.phase}{"\t"}{.status.reason}{"\t"}{.status.message}{"\n"}{end}' \
                                     > "$OUT/07_pod_status.tsv" 2>&1
EVICTED_COUNT=$(awk -F'\t' '$3=="Evicted"' "$OUT/07_pod_status.tsv" | wc -l || true)
echo "${EVICTED_COUNT:-0}" > "$OUT/07_evicted_count.txt"

kubectl get nodes \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{range .status.conditions[*]}{.type}={.status}{" "}{end}{"\n"}{end}' \
                                     > "$OUT/08_node_conditions.txt" 2>&1

# 5. CPU exhaustion: per-sample throttling records written by each agent.
# replay_agent.py mirrors every record to stdout AND its LOG_FILE. `kubectl
# logs` reads stdout via the kubelet and works after the pod has terminated
# (OOMKilled or simply Completed under restartPolicy: Never); `kubectl exec`
# needs a running container process, which a terminated pod does not have --
# using exec here would silently lose the per-sample throttling records for
# every pod that finishes (or OOMKills) before this script runs, which is
# the CPU side of the T8 confusion table. `.log` (stdout capture) is
# therefore the authoritative JSONL; the exec attempt is kept only as a
# best-effort extra for a pod that happens to still be running.
mkdir -p "$OUT/logs"
for pod in $(kubectl get pods -l app=rightsizing-replay -o name); do
  name="${pod#pod/}"
  kubectl logs "$name" --tail=-1 > "$OUT/logs/${name}.jsonl" 2>&1 || true
  kubectl exec "$name" -- cat /var/log/replay.jsonl \
      > "$OUT/logs/${name}.container.jsonl" 2>/dev/null || true
done

# 6. Rollup.
MEM_PRESSURE=$(grep -o 'MemoryPressure=True' "$OUT/08_node_conditions.txt" || true)
OVERLAP=$(comm -12 \
  <(awk -F'\t' '$3=="OOMKilled"{print $1}' "$OUT/05_oomkills.tsv" | sort) \
  <(awk -F'\t' '$3=="Evicted"{print $1}' "$OUT/07_pod_status.tsv" | sort) \
  | wc -l)
{
  echo "capture completed $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "pods:              $(kubectl get pods -l app=rightsizing-replay --no-headers 2>/dev/null | wc -l)"
  echo "oomkilled:         $(cat "$OUT/05_oomkill_count.txt")"
  echo "evicted:           $(cat "$OUT/07_evicted_count.txt")  <- exclude from confusion table, re-run separately"
  echo "node MemoryPressure at capture: ${MEM_PRESSURE:-False}"
  echo "OOMKilled/Evicted overlap: $OVERLAP  <- should be 0; investigate if not"
  echo "logfiles:          $(ls "$OUT/logs" 2>/dev/null | wc -l)"
} | tee "$OUT/99_summary.txt"
