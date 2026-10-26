# Risk-Quantified VM Right-Sizing -- canonical commands.
# Every number in the report should be reproducible from these targets.

PY        ?= python3
TRACES    ?= data/fastStorage
EVAL      ?= data/traces
RESULTS   ?= results
EVIDENCE  ?= evidence
FIGURES   ?= figures
CLUSTER   ?= rightsizing
IMAGE     ?= rightsizing/replay:latest
OBS_DAYS  ?= 21
EVAL_DAYS ?= 9
SUBSET    ?= 50
SCALE     ?= 0.0625
MARGIN    ?= 80
WAVE      ?=

EPS_OUT   ?= $(RESULTS)/theta_intervals

.PHONY: help install hooks check-attribution test synthetic inspect baselines recommend cluster image stage waves apply evidence figures sweep theta theta-sensitivity ablation hypotheses bootstrap multisplit demo demo-kind diagnose clean

help:
	@echo "install    install python dependencies"
	@echo "test       unit tests (must all pass, and never shrink, before any commit)"
	@echo "synthetic  generate fake traces to validate the pipeline"
	@echo "inspect    preflight the real dataset  (TRACES=$(TRACES))"
	@echo "baselines  run baselines               (OBS_DAYS=$(OBS_DAYS) EVAL_DAYS=$(EVAL_DAYS))"
	@echo "recommend  build recommendations + stratified subset (SUBSET=$(SUBSET))"
	@echo "cluster    create the kind cluster"
	@echo "image      build and load the replay image"
	@echo "waves      plan the wave split          (MARGIN=$(MARGIN) SCALE=$(SCALE))"
	@echo "apply      generate and apply pod manifests"
	@echo "evidence   capture rubric artifacts into $(EVIDENCE)/"
	@echo "figures    generate report figures into $(FIGURES)/"
	@echo "diagnose   low-eps violator diagnostic + theta feasibility arithmetic"
	@echo "sweep      T10 eps sweep + frontier at theta = 1 (correction off)"
	@echo "theta      T11 eps sweep + frontier at per-VM estimated theta"
	@echo "ablation   T12 one-switch-per-arm ablation + figures"
	@echo "hypotheses T15 H2 (ECE + binomial intervals) and H3 (zero-risk reclamation)"
	@echo "bootstrap  T16 95% intervals over VMs + every figure redrawn with them (SEED=$(SEED) B=$(B))"
	@echo "multisplit T17 multi-split sensitivity analysis (headline 21/9 unchanged)"
	@echo "demo       T19 synthetic end-to-end run, offline, under two minutes (DEMO_OUT=$(DEMO_OUT))"
	@echo "demo-kind  T19 optional: one pod OOMKilled on the kind cluster (needs cluster + image)"
	@echo "hooks      install the git hooks (run once per clone)"
	@echo "check-attribution  scan history for AI attribution trailers"

install:
	$(PY) -m pip install -r requirements.txt

hooks:
	git config core.hooksPath githooks
	chmod +x githooks/commit-msg githooks/commit-msg-filter
	@echo "hooks installed -- commit messages will be stripped of AI attribution"

check-attribution:
	@echo "scanning full history for attribution trailers..."
	@if git log --all --pretty=format:'%H %B' 2>/dev/null | grep -i -E 'Co-Authored-By:[[:space:]]*Claude|@anthropic\.com|Generated with \[?Claude Code|Claude-Session:' ; then \
		echo ""; echo "FOUND attribution in history -- see githooks/README.md to clean it"; exit 1; \
	else \
		echo "clean: no attribution trailers found"; \
	fi

test:
	cd tests && $(PY) test_baselines.py
	cd tests && $(PY) test_replay_agent.py
	cd tests && $(PY) test_evt.py
	cd tests && $(PY) test_hypotheses.py
	cd tests && $(PY) test_bootstrap.py
	cd tests && $(PY) test_multisplit.py
	cd tests && $(PY) test_demo_guard.py

synthetic:
	$(PY) src/make_synthetic_traces.py --vms 60 --days 30 --out data/synthetic

inspect:
	@mkdir -p $(RESULTS) $(EVIDENCE)
	$(PY) src/inspect_traces.py --traces $(TRACES) \
	    --report $(RESULTS)/dataset_report.csv \
	    --glitch-report $(RESULTS)/capacity_glitch_report.csv \
	    --shift-report $(RESULTS)/capacity_shift_report.csv | tee $(EVIDENCE)/02_preflight.txt

baselines: test
	@mkdir -p $(RESULTS) $(EVIDENCE)
	$(PY) src/baselines.py --traces $(TRACES) --out $(RESULTS) \
	    --obs-days $(OBS_DAYS) --eval-days $(EVAL_DAYS) \
	    | tee $(EVIDENCE)/06_baseline_summary.txt

recommend:
	$(PY) src/make_recommendations.py \
	    --results $(RESULTS)/per_vm_results.csv \
	    --out $(RESULTS)/recommendations.csv \
	    --subset $(SUBSET) --subset-method p95 \
	    --subset-strategy resource \
	    --subset-out $(RESULTS)/subset_$(SUBSET).csv

cluster:
	kind create cluster --config testbed/kind-config.yaml
	kubectl get nodes -o wide

image:
	docker build -t $(IMAGE) testbed/
	kind load docker-image $(IMAGE) --name $(CLUSTER)

stage:
	$(PY) testbed/stage_traces.py --subset $(RESULTS)/subset_$(SUBSET).csv \
	    --traces $(TRACES) --out $(EVAL) \
	    --obs-days $(OBS_DAYS) --eval-days $(EVAL_DAYS) --scale $(SCALE)

# Requested resources for the full 50-VM subset exceed a single-node kind
# cluster's typical allocatable capacity (see ENVIRONMENT_NOTES.md), so both
# the pod limits and the staged demand are scaled down by SCALE (default
# 1/16, applied to `make stage` above) and the run is split into sequential
# memory-balanced waves by `make waves` (5 waves at MARGIN=80). Run
# `make apply WAVE=1`, capture evidence, `kubectl delete -f ...`, then
# `make apply WAVE=2`, and so on.
waves:
	$(PY) testbed/plan_waves.py $(RESULTS)/subset_$(SUBSET).csv \
	    --out testbed --margin-mib $(MARGIN) --scale $(SCALE) --traces $(EVAL)

apply:
	$(PY) testbed/gen_manifests.py $(RESULTS)/subset_$(SUBSET).csv \
	    --method p95 --out testbed/manifests/p95$(WAVE) --scale $(SCALE) \
	    --margin-mib $(MARGIN) \
	    $(if $(WAVE),--vm-ids testbed/wave$(WAVE)_vms.txt,)
	kubectl apply -f testbed/manifests/p95$(WAVE)/
	kubectl get pods -l app=rightsizing-replay -o wide

evidence:
	bash testbed/collect_evidence.sh $(EVIDENCE)

figures:
	$(PY) src/make_figures.py --results $(RESULTS) --traces $(TRACES) \
	    --out $(FIGURES)

# --- the proposed method M(eps) ------------------------------------------
# Every frontier is produced both snapped (the reported policy) and unsnapped
# (the diagnostic counterfactual), per ARCHITECTURE.md; the unsnapped arm
# always goes to its own directory so its numbers can never be mistaken for a
# result.

diagnose:
	@mkdir -p $(RESULTS) $(EVIDENCE)
	$(PY) src/diagnose_low_eps.py --traces $(TRACES) --out $(RESULTS) \
	    --obs-days $(OBS_DAYS) --eval-days $(EVAL_DAYS) \
	    | tee $(EVIDENCE)/09_low_eps_diagnostic.txt

# T10: theta = 1, the "correction off" arm of the T12 ablation.
sweep:
	@mkdir -p $(RESULTS)/unsnapped $(EVIDENCE) $(FIGURES)
	$(PY) src/sweep_eps.py --traces $(TRACES) --out $(RESULTS) \
	    --obs-days $(OBS_DAYS) --eval-days $(EVAL_DAYS) \
	    | tee $(EVIDENCE)/07_frontier_snapped.txt
	$(PY) src/sweep_eps.py --traces $(TRACES) --out $(RESULTS)/unsnapped \
	    --obs-days $(OBS_DAYS) --eval-days $(EVAL_DAYS) --unsnapped \
	    | tee $(EVIDENCE)/08_frontier_unsnapped.txt
	$(PY) src/plot_frontier.py --frontier $(RESULTS)/frontier.csv \
	    --out $(FIGURES) --name frontier_snapped

# T11: per-VM theta on the observation window. The intervals estimator is the
# primary method because it has no free parameter. RUN_LENGTH belongs to the
# runs estimator only, is fixed at 12 samples (one hour), and is not a tuning
# knob -- see EVT_DEFAULT_RUN_LENGTH in src/baselines.py.
THETA_METHOD ?= intervals
RUN_LENGTH   ?= 12
theta:
	@mkdir -p $(EPS_OUT) $(EPS_OUT)_unsnapped $(EVIDENCE) $(FIGURES)
	$(PY) src/sweep_eps.py --traces $(TRACES) --out $(EPS_OUT) \
	    --obs-days $(OBS_DAYS) --eval-days $(EVAL_DAYS) \
	    --theta-method $(THETA_METHOD) --run-length $(RUN_LENGTH) \
	    | tee $(EVIDENCE)/10_frontier_theta_intervals.txt
	$(PY) src/sweep_eps.py --traces $(TRACES) --out $(EPS_OUT)_unsnapped \
	    --obs-days $(OBS_DAYS) --eval-days $(EVAL_DAYS) --unsnapped \
	    --theta-method $(THETA_METHOD) --run-length $(RUN_LENGTH) \
	    | tee $(EVIDENCE)/11_frontier_theta_intervals_unsnapped.txt
	$(PY) src/plot_frontier.py --frontier $(EPS_OUT)/frontier.csv \
	    --out $(FIGURES) --name frontier_theta_intervals \
	    --series-label "M(eps), per-VM theta ($(THETA_METHOD))" \
	    --title "Risk-quantified right-sizing frontier (T11: per-VM theta, $(THETA_METHOD) estimator; capped at original allocation)"
	$(PY) src/plot_frontier.py --frontier $(EPS_OUT)_unsnapped/frontier.csv \
	    --out $(FIGURES) --name frontier_theta_intervals_unsnapped \
	    --series-label "M(eps), per-VM theta ($(THETA_METHOD))" \
	    --title "Frontier, per-VM theta ($(THETA_METHOD)) -- UNSNAPPED diagnostic counterfactual"

# T11 sensitivity check: runs declustering at the pre-fixed run length. Not an
# H1 result, so it is reported snapped only; its job is to show whether the
# frontier moves when theta comes from a different estimator.
theta-sensitivity:
	@mkdir -p $(RESULTS)/theta_runs $(EVIDENCE)
	$(PY) src/sweep_eps.py --traces $(TRACES) --out $(RESULTS)/theta_runs \
	    --obs-days $(OBS_DAYS) --eval-days $(EVAL_DAYS) \
	    --theta-method runs --run-length $(RUN_LENGTH) \
	    | tee $(EVIDENCE)/12_frontier_theta_runs.txt

# T12: one switch flipped per arm, all measured on the same quantity --
# reclamation at matched measured risk against peak_capped. The table is the
# deliverable; the figures support it.
ablation:
	@mkdir -p $(RESULTS)/ablation $(EVIDENCE) $(FIGURES)
	$(PY) src/ablation.py --traces $(TRACES) --out $(RESULTS)/ablation \
	    --obs-days $(OBS_DAYS) --eval-days $(EVAL_DAYS) \
	    --figures $(FIGURES) | tee $(EVIDENCE)/13_ablation.txt

# T15: H2 and H3 against their pre-registered statistics. Post-processing of
# the frontiers written by `theta`, `theta-sensitivity` and `sweep`. The first
# frontier is the headline -- M(eps) as pre-registered includes the
# extremal-index correction; the other two are context.
hypotheses:
	@mkdir -p $(RESULTS)/hypotheses $(EVIDENCE)
	$(PY) src/hypotheses.py --out $(RESULTS)/hypotheses \
	    --frontier $(EPS_OUT)/frontier.csv:theta_intervals \
	    --frontier $(RESULTS)/theta_runs/frontier.csv:theta_runs \
	    --frontier $(RESULTS)/frontier.csv:theta_1 \
	    | tee $(EVIDENCE)/15_hypotheses.txt

# T16: bootstrap over VMs. Needs the frontiers from `sweep`, `theta`,
# `theta-sensitivity` and `ablation` -- every rebuilt frontier is checked
# against its published CSV before an interval is drawn. Then every figure is
# redrawn with intervals: Review 2 figures in place, Review 1 figures as
# fig<n>_<name>_ci (the originals are closed-review evidence and stay as they
# are). SEED and B were fixed before the first run (16, 10,000).
SEED ?= 16
B    ?= 10000
BOOT  = $(RESULTS)/bootstrap
bootstrap:
	@mkdir -p $(BOOT) $(EVIDENCE) $(FIGURES)
	$(PY) src/bootstrap.py --traces $(TRACES) --results $(RESULTS) --out $(BOOT) \
	    --obs-days $(OBS_DAYS) --eval-days $(EVAL_DAYS) \
	    --seed $(SEED) --resamples $(B) | tee $(EVIDENCE)/16_bootstrap.txt
	$(PY) src/plot_frontier.py --frontier $(RESULTS)/frontier.csv \
	    --out $(FIGURES) --name frontier_snapped \
	    --intervals $(BOOT)/frontier_theta_off_ci.csv
	$(PY) src/plot_frontier.py --frontier $(RESULTS)/unsnapped/frontier.csv \
	    --out $(FIGURES) --name frontier_unsnapped \
	    --title "Risk-quantified right-sizing frontier (theta = 1, UNSNAPPED diagnostic)" \
	    --intervals $(BOOT)/frontier_theta_1_unsnapped_ci.csv
	$(PY) src/plot_frontier.py --frontier $(EPS_OUT)/frontier.csv \
	    --out $(FIGURES) --name frontier_theta_intervals \
	    --series-label "M(eps), per-VM theta ($(THETA_METHOD))" \
	    --title "Risk-quantified right-sizing frontier (T11: per-VM theta, $(THETA_METHOD) estimator; capped at original allocation)" \
	    --intervals $(BOOT)/frontier_reference_ci.csv
	$(PY) src/plot_frontier.py --frontier $(EPS_OUT)_unsnapped/frontier.csv \
	    --out $(FIGURES) --name frontier_theta_intervals_unsnapped \
	    --series-label "M(eps), per-VM theta ($(THETA_METHOD))" \
	    --title "Frontier, per-VM theta ($(THETA_METHOD)) -- UNSNAPPED diagnostic counterfactual" \
	    --intervals $(BOOT)/frontier_snap_off_ci.csv
	$(PY) src/plot_ablation.py --ablation $(RESULTS)/ablation --out $(FIGURES) \
	    --intervals $(BOOT)
	$(PY) src/make_figures.py --results $(RESULTS) --traces $(TRACES) \
	    --out $(FIGURES) --ci --ci-seed $(SEED) --resamples $(B)

# T17: sensitivity of the headline numbers to the observation/evaluation split.
# The split plan is fixed in src/multisplit.py and was approved before the
# first run (ENVIRONMENT_NOTES.md, "T17"); it is deliberately not a variable.
# The 21/9 headline is rerun and must reproduce the published frontiers
# exactly. Not the headline -- docs/HYPOTHESES.txt pre-registers 21/9.
multisplit:
	@mkdir -p $(RESULTS)/multisplit $(EVIDENCE)
	$(PY) src/multisplit.py --traces $(TRACES) --results $(RESULTS) \
	    --out $(RESULTS)/multisplit | tee $(EVIDENCE)/17_multisplit.txt

# T19: the whole offline pipeline on synthetic traces -- no Docker, no kind,
# no dataset. Writes only under DEMO_OUT, never results/, so a synthetic number
# can never land beside a real one. The frontier table is printed by the sweep.
#
# DEMO_TRACES=<dir> runs the demo on a supplied trace directory instead of
# synthetic traces. Every demo input goes through src/demo_guard.py first,
# which stops the demo on a file whose timestamps are in milliseconds.
DEMO_OUT    ?= demo_out
DEMO_SEED   ?= 42
DEMO_TRACES ?=
demo:
	@start=$$(date +%s); \
	rm -rf $(DEMO_OUT) && mkdir -p $(DEMO_OUT) && \
	if [ -n "$(DEMO_TRACES)" ]; then \
	    T="$(DEMO_TRACES)"; \
	    TITLE="DEMO -- frontier on supplied traces ($(notdir $(patsubst %/,%,$(DEMO_TRACES))))"; \
	    SRC="This trace was graciously provided by Bitbrains IT Services Inc. Supplied at demo time."; \
	else \
	    $(PY) src/make_synthetic_traces.py --vms 60 --days 30 --seed $(DEMO_SEED) \
	        --out $(DEMO_OUT)/traces || exit 1; \
	    T="$(DEMO_OUT)/traces"; \
	    TITLE="SYNTHETIC DEMO -- frontier on 60 generated VMs (seed $(DEMO_SEED)), not a result"; \
	    SRC="Traces: synthetic, src/make_synthetic_traces.py --seed $(DEMO_SEED). No real data."; \
	fi && \
	$(PY) src/demo_guard.py --traces "$$T" && \
	$(PY) src/baselines.py --traces "$$T" --out $(DEMO_OUT)/baselines \
	    --obs-days $(OBS_DAYS) --eval-days $(EVAL_DAYS) && \
	$(PY) src/sweep_eps.py --traces "$$T" --out $(DEMO_OUT)/frontier \
	    --obs-days $(OBS_DAYS) --eval-days $(EVAL_DAYS) \
	    --theta-method $(THETA_METHOD) --run-length $(RUN_LENGTH) && \
	$(PY) src/plot_frontier.py --frontier $(DEMO_OUT)/frontier/frontier.csv \
	    --out $(DEMO_OUT) --name frontier_demo \
	    --series-label "M(eps), per-VM theta ($(THETA_METHOD))" \
	    --title "$$TITLE" --source "$$SRC" && \
	echo "" && echo "demo wall clock: $$(( $$(date +%s) - start )) s (target: under 120 s)"

# T19, optional and separate: one pod OOMKilled by the kernel, on screen.
# Needs `make cluster image` first. The trace ships in a ConfigMap, so nothing
# is staged into data/traces and the real waves are untouched.
demo-kind:
	kubectl delete -f testbed/demo/oom-demo.yaml --ignore-not-found --wait=true
	kubectl apply -f testbed/demo/oom-demo.yaml
	kubectl wait pod/rs-demo-oom --timeout=120s \
	    --for=jsonpath='{.status.containerStatuses[0].state.terminated.reason}'=OOMKilled
	kubectl get pods -l app=rightsizing-demo

clean:
	rm -rf $(RESULTS)/* testbed/manifests __pycache__ */__pycache__
	@echo "left $(EVIDENCE)/ and data/ alone -- delete those by hand"
