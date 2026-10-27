# Risk-Quantified VM Right-Sizing for Private Cloud Capacity Reclamation

Right-sizing tools report how much capacity they reclaim, but not how many
VMs that reclamation breaks. This project reports both numbers on the same
axes. It then checks the predicted risk against violations observed on
held-out data.

**Authors:** Abhishek Singh (23BCE5044), Purusharth Kumar (23BCE1908)

**Paper:** [paper/Risk_Quantified_Virtual_Machine_Right_Sizing_for_Private_Cloud_Capacity_Reclamation.pdf](paper/Risk_Quantified_Virtual_Machine_Right_Sizing_for_Private_Cloud_Capacity_Reclamation.pdf)

---

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
git clone <this repository's URL>
cd <folder name>
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

The real dataset is about 2.5 GB unpacked. It is not in this repository
because its licence does not allow redistribution.

### Step 1: Download it

1. Go to http://gwa.ewi.tudelft.nl/datasets/gwa-t-12-bitbrains (mirror:
   https://atlarge-research.com/gwa-t-12/).
2. Download **GWA-T-12 fastStorage** (about 128 MB zipped).
3. Unzip it. Find the folder that contains `1.csv`, `2.csv`, … `1250.csv`.
4. Copy those CSV files into `data\fastStorage\` inside the project. Create
   the folder if it does not exist.

### Step 2: Look at three real VMs one at a time

1. Create the folder `data\demo_input\`.
2. Copy `1007.csv`, `332.csv` and `1226.csv` from `data\fastStorage\` into it.
3. Run:

```
run.bat demo-vm
```

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
paper/      final paper (PDF) and LaTeX source
data/       datasets (not committed, see Option 2)
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
