# Risk-Quantified Virtual Machine Right-Sizing for Private Cloud Capacity Reclamation

**Authors:** Abhishek Singh (23BCE5044), Purusharth Kumar (23BCE1908)

**Faculty:** Indra Priyadharshini S

**Paper:** [paper/Risk_Quantified_Virtual_Machine_Right_Sizing_for_Private_Cloud_Capacity_Reclamation.pdf](paper/Risk_Quantified_Virtual_Machine_Right_Sizing_for_Private_Cloud_Capacity_Reclamation.pdf)
· **Slides:** [paper/Abhishek_Singh_P10_Review3.pptx](paper/Abhishek_Singh_P10_Review3.pptx)
· **Website:** open [website/index.html](website/index.html) in a browser

Contents: [Background](#background) · [Problem statement](#problem-statement) ·
[Objectives](#objectives) · [Scope of work](#scope-of-work) ·
[Baselines](#baselines-for-comparison) · [Metrics](#evaluation-metrics) ·
[Datasets and tools](#datasets-and-tools) · [Results](#results) ·
[Where the results do not hold](#where-the-results-do-not-hold) ·
[How to run it from scratch](#how-to-run-it-from-scratch)

---

## Background

Virtual machines in private clouds are usually sized for the peak demand
expected when they are requested. Actual demand is normally far below that
peak, so much of the allocated capacity sits idle. Right-sizing reduces
allocations to match observed use.

## Problem statement

Right-sizing recommendations are normally reported only by the capacity they
reclaim. That is incomplete: shrinking an allocation raises the chance that
the workload runs out of CPU or memory during a later demand spike. The risk a
reclamation introduces is not quantified, so an operator cannot tell whether a
recommendation is safe, and has no principled way to choose an operating point.

## Objectives

**O1.** To analyse utilisation telemetry from real production virtual machines
and characterise the gap between allocated and used capacity.

**O2.** To develop a right-sizing recommender that produces flavour
recommendations from utilisation history.

**O3.** To model, for each recommendation, the probability that the resized
machine will exhaust its resources within a defined future window.

**O4.** To present the outcome as a frontier relating reclaimed capacity to
induced violation risk, allowing an operator to select an acceptable point.

| Objective | Status | Evidence |
|---|---|---|
| O1 | Done | 1,250 VMs profiled, 792 usable; sizing to peak alone frees 37.6% of vCPU-hours and 33.0% of GB-hours |
| O2 | Done | Peak, P95, capped peak and M(ε) each pick an upward-snapped catalog flavour from the observation window only |
| O3 | Built; its calibration test (H2) fails | Every recommendation carries P(exhaustion within 9 days), tested on held-out data |
| O4 | Done | Frontier over ε = 0.001–0.6 against every baseline, with 95% intervals on both axes |

## Scope of work

1. Use the public Bitbrains GWA-T-12 traces (fastStorage as the main fleet,
   Rnd as a second fleet).
2. Split each trace into an observation period (first 21 days) and an
   evaluation period (last 9 days).
3. Generate recommendations from the observation period alone. No parameter
   is fitted on evaluation data.
4. Replay the evaluation period against each recommended allocation and
   record every resource-exhaustion event. A memory violation is demand above
   the allocation at any single sample (the process would be killed). A CPU
   violation is demand above the allocation for at least 2 consecutive
   samples (the VM slows down).
5. Repeat across aggressiveness settings (ε from 0.001 to 0.6) to trace the
   full frontier.

Every recommendation snaps **up** to a flavour catalog: vCPU {1, 2, 4, 8, 16}
and memory {2, 4, 8, 16, 32, 64} GB.

**Proposed method, M(ε).** The operator states ε, the acceptable chance of at
least one violation in the next 9 days. For each VM and resource, M(ε) fits a
Generalized Pareto tail above the 95th percentile of observed demand, corrects
for clustered spikes with an extremal index θ, and returns the smallest
catalog flavour whose exhaustion probability meets ε, capped at the original
allocation.

## Baselines for comparison

- **As-provisioned:** keep the original allocation (no reclamation).
- **Peak:** size to the observed peak, which is current practice.
- **P95:** size to the 95th percentile, the common heuristic.
- **Capped peak:** peak, never above the original allocation.

## Evaluation metrics

- Reclaimed vCPU-hours and memory GB-hours over the evaluation period.
- Count of resource-exhaustion events, and the share of VMs with at least one.
- The frontier relating reclaimed capacity to that risk.
- The capacity that can be reclaimed at zero induced risk.
- Calibration: does the stated risk ε match the measured violation rate?

## Datasets and tools

- **GWA-T-12 Bitbrains fastStorage:** 1,250 VMs, 5-minute samples, 30 days;
  792 usable after four data-quality checks (425 of them idle).
- **GWA-T-12 Bitbrains Rnd:** 500 VMs, August 2013; 316 usable.
- **Python 3.11+** with pandas, numpy, scipy (tail fitting) and matplotlib
  (figures).
- **kind** (Kubernetes in Docker), to check the offline violation rule against
  real kernel OOM kills on 50 replayed VMs.

Two changes from the original plan, both deliberate:
- **Azure VM traces were not used** for the results. They contain no memory
  demand series, so they cannot test any memory claim.
- **scikit-learn was not needed.** The method is statistical (extreme-value
  theory, via scipy), not a trained model.

---

## Results

The hypotheses were written down before any result existed
([docs/HYPOTHESES.txt](docs/HYPOTHESES.txt)) and were not changed afterwards.
Unless noted, numbers are for fastStorage, 792 VMs, 21/9-day split, with 95%
bootstrap intervals over VMs (10,000 resamples, seed 16).

### Reclamation against risk (memory, all VMs)

| Policy | GB-hours reclaimed | VMs with a memory violation |
|---|---|---|
| As-provisioned | 0% | 0% |
| Peak | 33.0% [22.7, 42.4] | 6.4% [4.8, 8.2] |
| Capped peak | 41.8% [33.3, 49.6] | 6.4% [4.8, 8.2] |
| P95 | 71.7% [66.7, 76.0] | 35.5% [32.2, 38.8] |
| M(ε = 0.001) | 28.9% [19.0, 38.0] | 2.9% [1.8, 4.2] |
| M(ε = 0.1) | 36.4% [27.5, 44.7] | 6.9% [5.2, 8.7] |
| M(ε = 0.6) | 46.7% [38.4, 54.2] | 11.9% [9.7, 14.1] |

P95 reclaims the most because it breaks the most: more than a third of VMs
run out of memory in 9 days. M(ε) gives the operator a dial into the low-risk
region, but it never beats a baseline where their curves overlap. The full
frontier, CPU included, is in
[figures/frontier_theta_intervals.png](figures/frontier_theta_intervals.png).

### Hypothesis verdicts

| Hypothesis | Threshold | Result | Verdict |
|---|---|---|---|
| **H1** Beat P95 at P95's risk | +15% vCPU-h and +10% GB-h | M(ε) never reaches P95's risk (max 3.5% CPU, 11.9% memory vs 9.0% and 35.5%) | **Not evaluable** |
| **H2** Stated risk is calibrated | < 2 of 5 points outside the binomial interval, ECE < 0.05 | 3 of 5 (CPU) and 4 of 5 (memory) outside. At ε = 0.001 the measured memory risk is 2.9% | **Not supported** |
| **H3** Reclaim at zero risk | ≥ 25% vCPU-h or ≥ 20% GB-h with no violations | 0%: only as-provisioned has zero violations | **Not supported** |

### Other findings

- **At equal risk, M(ε) reclaims less than capped peak**, by 2.2 to 6.3
  percentage points. The gap is negative in every cell and every split; three
  of four intervals exclude zero.
- **The cap is the whole gain** (ablation). Without the cap at the original
  allocation, the gap falls by 47 pp (CPU) and 68 pp (memory). Turning the
  clustering correction θ off changes nothing measurable.
- **The results replicate** on six observation/evaluation splits and on the
  second fleet (Rnd), with the same verdicts.
- **The offline memory rule matches the kernel** on 50 of 50 replayed VMs.
- **Five claims from Review 2 were withdrawn** once intervals were added. See
  slide 16 or the paper.

In short: the method fails its hypotheses, and the reason is measured. Demand
in the evaluation window often leaves the range the observation window saw
(30–38% of VMs exceed their observed memory peak), and no tail fitted to past
data can anticipate that.

## Where the results do not hold

- **CPU violations are not validated** against a real system. On the
  1/16-scale cluster the replay harness itself used the CPU quota, so CPU
  throttling did not track CPU demand. Only the memory rule is
  kernel-validated.
- **Demand is clipped** at the original allocation in the trace, so the true
  size of any spike above it is unknown.
- **One provider, one month per fleet, 2013 data.** Both fleets come from
  Bitbrains; results may not transfer to other clouds or newer workloads.
- **5-minute sampling hides shorter bursts**, so every violation count is a
  lower bound.
- **The intervals cover fleet composition only.** They treat VMs as
  independent and do not capture a demand shock shared across VMs. The six
  splits overlap, so their agreement is consistency, not independent proof.
- **The ECE clause of H2 has no power** on this sweep: a method that never
  violates would also pass it. H2 is decided by the binomial clause.
- **Azure was not used** (no memory series), so nothing here is replicated
  outside Bitbrains.

---

# How to run it from scratch

## What you need

- A laptop running Windows 10/11, macOS or Linux
- An internet connection for the one-time setup
- About 500 MB of free disk space (3 GB if you also download the real dataset)

You only need Python. Docker, Kubernetes, `make` and an IDE are **not**
required.

---

## Option 0: Look at the results without running anything

All results are already in this repository:

| What | Where |
|---|---|
| Final paper | `paper/` |
| Review 3 slides | `paper/Abhishek_Singh_P10_Review3.pptx` |
| Project website | Double-click `website/index.html`. It opens in any browser and works offline, with no server |
| All graphs | `figures/`. Start with `frontier_theta_intervals.png` (method vs baselines) and `ablation_mem.png` (which component causes the gain) |
| Result tables | `results/`, for example `results/baseline_summary.csv` |
| Hypothesis verdicts | `evidence/15_hypotheses.txt` |
| Kubernetes cluster logs | `evidence/` (captured from our test cluster) |

---

## Option 1: Run it on your own laptop (Windows, step by step)

### Step 1: Download the project

**If you are not familiar with Git:** on the GitHub page, click the green
**Code** button, then **Download ZIP**. Right-click the downloaded file,
choose **Extract All**, and open the extracted folder.

**If you have Git:**

```
git clone https://github.com/AbhishekSingh444/vm-rightsizing.git
cd vm-rightsizing
```

### Step 2: Install Python (skip this if you already have Python 3.11 or newer)

1. Open https://www.python.org/downloads/ and download Python 3.12.
2. Run the installer. **On the first screen, tick "Add python.exe to PATH"**,
   then click **Install Now**.
3. Restart your computer, or at least close every open terminal window.

To check: open **Command Prompt** (press the Windows key, type `cmd`, press
Enter) and type `python --version`. It should print `Python 3.12.x` or
similar.

### Step 3: Set up the project (once)

Open the project folder in File Explorer and **double-click `setup.bat`**.

It creates a private Python environment in a `.venv` folder inside the
project and installs the four libraries the project needs (pandas, numpy,
scipy, matplotlib). This takes 1–3 minutes. Nothing is installed outside the
project folder. When it prints **Setup complete**, press any key to close it.

### Step 4: Open a terminal in the project folder

In File Explorer, open the project folder, click in the **address bar** at
the top, type `cmd` and press Enter. A Command Prompt opens in that folder.

### Step 5: Run the tests

```
run.bat test
```

Each test file prints a line such as `35/35 passed`. The last line should be:

```
all test files passed
```

There are 125 tests. They take about a minute.

### Step 6: Run the demo

```
run.bat demo
```

This runs the whole pipeline (data split, baselines, the proposed method,
the risk sweep and the graph) on 60 generated VMs. It needs no download and
takes about 30–60 seconds. When it finishes, open:

```
demo_out\frontier_demo.png
```

How to read the graph:

- The **X-axis** is the share of VMs that broke. Further left is better.
- The **Y-axis** is the share of capacity saved. Higher is better.
- The **blue line** is the proposed method at different risk levels. The
  **orange** and **green** markers are the baselines.

The demo data is synthetic, so the numbers are not results. The demo shows
that the pipeline runs end to end. The real results are in `figures/` and
`results/`.

---

## Option 1 on macOS / Linux

Open a terminal in the project folder and run:

```bash
python3 --version                  # must be 3.11 or newer
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

make test PY=python                # 125 tests, ends with all passing
make demo PY=python                # writes demo_out/frontier_demo.png
```

If `python3` is missing or too old, install it from python.org (macOS) or
with your package manager, for example `sudo apt install python3 python3-venv`.
If `make` is missing on macOS, run `xcode-select --install`.

---

## Option 2: Run on the real dataset (optional)

The project uses the **GWA-T-12 Bitbrains** traces from the Grid Workloads
Archive (TU Delft). They are free to download, but they are **not in this
repository** and must never be committed: they are about 2.6 GB unpacked and
come with their own terms of use (any published material must acknowledge
the source), so each user downloads them from the archive.
`.gitignore` ignores everything under `data/` except `data/sample_vm.csv`, so
anything you download there stays on your machine.

| Trace | Download link | Zipped | Goes in |
|---|---|---|---|
| fastStorage (1,250 VMs, the main fleet) | https://atlarge-research.com/gwa-traces/gwa_t_12_fastStorage.zip | 128 MB | `data\fastStorage\` |
| Rnd (500 VMs, the second fleet; optional) | https://atlarge-research.com/gwa-traces/gwa_t_12_rnd.zip | 156 MB | `data\rnd\` |

The dataset page with the description and terms is
https://atlarge-research.com/gwa-t-12/. The old TU Delft address
(gwa.ewi.tudelft.nl) no longer resolves.

### Step 1: Download it

**Option A, in the browser.**

1. Click the fastStorage link in the table above and save the zip file.
2. Right-click the zip, choose **Extract All**, and open the extracted folder.
   Look for the folder that contains `1.csv`, `2.csv`, … `1250.csv`.
3. Inside the project, create the folder `data\fastStorage\` and copy all
   1,250 CSV files into it. The files go directly in that folder, not in a
   subfolder.

**Option B, from the command line.** Windows 10/11 include `curl` and `tar`.
Run these in a Command Prompt opened in the project folder (Step 4 of
Option 1). The download can take several minutes.

```
mkdir data\download data\fastStorage
curl -L -o data\download\fastStorage.zip https://atlarge-research.com/gwa-traces/gwa_t_12_fastStorage.zip
tar -xf data\download\fastStorage.zip -C data\download
for /r data\download %f in (*.csv) do @copy /y "%f" data\fastStorage\ >nul
```

On macOS/Linux:

```bash
mkdir -p data/download data/fastStorage
curl -L -o data/download/fastStorage.zip https://atlarge-research.com/gwa-traces/gwa_t_12_fastStorage.zip
unzip -q data/download/fastStorage.zip -d data/download
find data/download -name '*.csv' -exec cp {} data/fastStorage/ \;
```

You can delete `data\download\` afterwards.

**Check the download.** This counts the files and checks their format. It
should report `files : 1250`, `usable files : 792` and a span of 30.0 days:

```
mkdir my_results
.venv\Scripts\python src\inspect_traces.py --traces data\fastStorage --report my_results\dataset_report.csv
```

(macOS/Linux: `mkdir -p my_results && python src/inspect_traces.py --traces data/fastStorage --report my_results/dataset_report.csv`)

Inside the zip the CSVs sit in `fastStorage/2013-8/`; the copy commands above
move them up into `data\fastStorage\`. On Windows, run them in Command
Prompt, not Git Bash: Git Bash's own `tar` cannot open zip files.

**Rnd, the second fleet (optional).** It is only needed to reproduce the
replication results. Download `gwa_t_12_rnd.zip` the same way. It contains
three monthly folders, `2013-7`, `2013-8` and `2013-9`. Copy the folders as
they are into `data\rnd\`. The results in `results/rnd/` used
`data\rnd\2013-8` (August 2013, 500 VMs).

**Two format quirks**, in case you read the files yourself: the columns are
separated by `;` followed by a tab, and the column named `Timestamp [ms]`
actually holds seconds. The project's loaders already handle both.

### Step 2: Look at three real VMs one at a time

1. Create the folder `data\demo_input\`.
2. Copy `1007.csv`, `332.csv` and `1226.csv` from `data\fastStorage\` into it.
3. Run:

```
run.bat demo-vm
```

On macOS/Linux, with the environment from "Option 1 on macOS / Linux"
active, run `make demo-vm PY=python` instead.

For each VM it prints the original size, what the VM actually used, what
every method recommends, and whether that recommendation was exceeded in the
last 9 days. The output is also saved under `demo_out\vm\`.

### Step 3: Full pipeline on all 1,250 VMs

```
run.bat demo data\fastStorage
```

This produces `demo_out\frontier_demo.png` for the real fleet. It takes
several minutes.

To regenerate the individual result tables, run the scripts directly. They
write to `my_results\` so the committed results are not overwritten:

```
mkdir my_results
.venv\Scripts\python src\inspect_traces.py --traces data\fastStorage --report my_results\dataset_report.csv
.venv\Scripts\python src\baselines.py --traces data\fastStorage --out my_results --obs-days 21 --eval-days 9
.venv\Scripts\python src\sweep_eps.py --traces data\fastStorage --out my_results\theta_intervals --obs-days 21 --eval-days 9 --theta-method intervals --run-length 12
.venv\Scripts\python src\ablation.py --traces data\fastStorage --out my_results\ablation --obs-days 21 --eval-days 9 --figures my_results\figures
```

Every script accepts `--help`. On macOS/Linux, the `Makefile` has one target
per step (`make inspect`, `make baselines`, `make theta`, `make ablation`,
`make hypotheses`, `make bootstrap`, `make multisplit`). Those targets write
into `results/` and `evidence/` and overwrite the committed copies.

---

## Kubernetes testbed (optional, needs Docker)

Part of the project replays 50 VMs as containers on a local Kubernetes
cluster ([kind](https://kind.sigs.k8s.io/)), so that the Linux kernel enforces
memory limits and kills containers that exceed them (`OOMKilled`). This
**does** need Docker Desktop, kind and kubectl. You do not need it to run
anything above. Its captured output is already in `evidence/`, and
`evidence/T8_confusion.md` summarises it. To reproduce it, follow
[testbed/README.md](testbed/README.md).

---

## Troubleshooting

| Message | Fix |
|---|---|
| `'python' is not recognized` | Python is not installed, or "Add python.exe to PATH" was not ticked. Reinstall Python with the box ticked and open a new terminal. |
| `Python 3.11 or newer was not found` | Run `setup.bat`, which explains what to install. |
| `missing Python packages` | Run `setup.bat` again. Check your internet connection. |
| `dataset missing -- folder data\demo_input does not exist` | `run.bat demo-vm` needs the real dataset. Follow Option 2. `run.bat demo` and `run.bat test` work without it. |
| `run.bat` is not recognized | The terminal is not in the project folder. Repeat Step 4. |
| Windows SmartScreen warns about `setup.bat` | Click **More info**, then **Run anyway**. The file only creates `.venv` and runs `pip install`. You can open it in Notepad to check. |

---

## Project layout

```
src/        offline pipeline: dataset checks, baselines, proposed method M(eps),
            ablation, hypothesis tests, bootstrap, multi-split, figures
tests/      unit tests (125)
testbed/    Kubernetes (kind) testbed: replay agent, manifests, evidence capture
results/    result tables, each with the run_config.json that produced it
figures/    all graphs used in the paper (captions in figures/CAPTIONS.md)
evidence/   captured logs from every experiment
docs/       pre-registered hypotheses, literature gap table, data-quality notes
paper/      final paper (PDF), LaTeX source and the Review 3 slides
website/    static results site; open index.html (rebuild: python website/build_data.py)
data/       datasets (not committed, see Option 2); data/sample_vm.csv is one example VM
setup.bat   one-time setup on Windows
run.bat     test / demo / demo-vm / demo-kind on Windows
Makefile    every step of the pipeline (macOS/Linux)
```

`ARCHITECTURE.md` explains how the pieces fit together and why each design
decision was made.

---

## Dataset acknowledgement

GWA-T-12 Bitbrains, from the Grid Workloads Archive at TU Delft.

> This trace was graciously provided by Bitbrains IT Services Inc.

Trace analysis: S. Shen, V. van Beek, A. Iosup, "Statistical Characterization
of Business-Critical Workloads Hosted in Cloud Datacenters", CCGrid 2015.
