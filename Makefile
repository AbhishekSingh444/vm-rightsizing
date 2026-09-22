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

.PHONY: help install hooks check-attribution test synthetic inspect baselines recommend cluster image stage waves apply evidence figures clean

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

clean:
	rm -rf $(RESULTS)/* testbed/manifests __pycache__ */__pycache__
	@echo "left $(EVIDENCE)/ and data/ alone -- delete those by hand"
