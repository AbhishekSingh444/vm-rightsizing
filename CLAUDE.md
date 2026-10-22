# CLAUDE.md

Project rules for agentic work in this repo. Read this before touching anything.

## What this project is

Deliverables are graded against a rubric. Current phase is **Review 2 —
Implementation and Preliminary Results**:

- **O1 and O2 evidenced (4 marks)** — working code in a version-controlled
  repository.
- **Comparative results (3 marks)** — the proposed method and the baselines
  on the same axes.
- **Ablation or parameter sweep (2 marks)** — which component causes the gain.
- **Threats to validity (1 mark)** — what the setup cannot prove.

Review 1 (testbed, baselines) is complete. Do not modify its evidence.

See `ARCHITECTURE.md` for how the pieces fit and `TASKS.md` for the work queue.

## Hard invariants — never change these without asking

These encode decisions that are defended in the written report. Changing them
silently invalidates the writeup and every number in the results table.

1. **Violation definitions in `src/baselines.py`.**
   - Memory: demand exceeds allocation at **any single sample**. Terminal, the
     process dies.
   - CPU: demand exceeds allocation across **at least 2 consecutive samples**.
     Degrading, not terminal.
   The asymmetry is deliberate and defended in the hypothesis document. Do not
   "simplify" it into one rule.

2. **Hypothesis thresholds** (15% CPU / 10% memory reclamation gap, 25% / 20%
   zero-risk reclamation, ECE below 0.05). These are pre-registered. Adjusting
   them after seeing results destroys the falsifiability the rubric awards
   marks for.

3. **`tests/test_baselines.py`.** Never edit a test to make it pass. If a test
   fails, either the code under test is wrong or the test encodes a decision —
   stop and ask. Do not weaken assertions, do not delete cases, do not add
   `skip`.

4. **The information constraint.** Recommenders see the observation window
   only. No parameter, threshold, or hyperparameter may be fitted against the
   evaluation window. If you find yourself passing evaluation data into a
   recommender, that is a bug regardless of what it does to the results.

5. **Flavour catalog** — vCPU `[1,2,4,8,16]`, memory `[2,4,8,16,32,64]` GB, and
   snapping is always upward. Without a catalog, right-sizing becomes unbounded
   continuous shrinking, which no real private cloud permits.

## Dataset traps

The GWA-T-12 Bitbrains fastStorage trace has two format quirks that cost hours
if you hit them cold.

**Delimiter is `;` followed by a TAB.** Plain `sep=";"` leaves a tab glued to
every column name and `df["CPU cores"]` raises KeyError. Always:

```python
df = pd.read_csv(path, sep=r";\s*", engine="python")
df.columns = [c.strip().strip('"').strip() for c in df.columns]
```

**The column named `Timestamp [ms]` holds SECONDS.** Dividing by 1000 gives a
fleet that lived 43 minutes.

**The trace is already one CSV per VM** (`1.csv` … `1250.csv`). Do not write a
splitter.

**Licence requires acknowledgement.** Any published material must include:
"This trace was graciously provided by Bitbrains IT Services Inc."

## Git rules — read before any commit

This is graded academic work submitted under my name. Commits must carry my
authorship only.

**Never add AI attribution to a commit message.** No `Co-Authored-By: Claude`,
no "Generated with Claude Code" line, no `Claude-Session:` trailer, no robot
emoji. A `commit-msg` hook strips these, but do not rely on it — do not write
them in the first place. Never attempt to work around the hook.

**Never push.** Do not run `git push`, do not create or modify remotes, do not
open pull requests, do not create repositories. `.claude/settings.json` denies
these, and that denial is intentional. I handle all publishing myself.

**Ask before committing.** Stage changes and propose a commit message; wait for
me to approve it. Do not commit automatically after finishing a task.

**Commit message style:** imperative subject under 60 characters, blank line,
then a short body explaining why the change was made. No trailers except a
human `Co-Authored-By` if a teammate genuinely worked on it.

Run `make hooks` once per clone. `make check-attribution` scans the full
history and should always report clean.

## Commands

```
make install        # dependencies
make test           # unit tests — must pass fully before any commit
make synthetic      # generate fake traces for pipeline validation
make inspect        # preflight the real dataset
make baselines      # run the baselines, write results/
make recommend      # convert results into recommendations + cluster subset
make cluster        # create the kind cluster
make image          # build and load the replay image
make evidence       # capture rubric artifacts into evidence/
make clean
```

Always run `make test` before and after changing anything in `src/`.

## Conventions

- Python 3.11+, standard library plus pandas and numpy. `replay_agent.py` is
  **standard library only** — it runs inside a slim container and must not
  require a pip install.
- No new dependencies without asking. This runs on a student laptop and in a
  minimal container.
- Deterministic by default: seed anything random and expose `--seed`.
- Every script takes `--help` and writes its outputs under a `--out` directory.
  Nothing writes to a hardcoded absolute path.
- Log the config used for a run alongside its results (`run_config.json`), so a
  number in the report can always be traced to the parameters that produced it.

## Definition of done

A task is done when:

1. `make test` passes in full, with **no test fewer than the run before**. The
   suite only ever grows. As of 2026-09-09 it stands at **38 (35 + 3)** —
   `test_baselines.py` 35, `test_replay_agent.py` 3 — but the invariant is
   "everything passes and nothing was dropped", not the number, which goes stale
   every time a test is legitimately added.
2. The command in the task's acceptance criteria runs clean from a fresh shell.
3. Any file the task was supposed to produce exists and is non-empty.
4. If you changed behaviour, `ENVIRONMENT_NOTES.md` records what changed and why.

"It ran on my machine once" is not done. Re-run from a clean state.

## When you get stuck

Prefer stopping and reporting over guessing. Specifically, stop and ask if:

- A test fails and the fix would require editing the test.
- The real dataset's shape contradicts an assumption in the docs (for example
  the trace span is not ~30 days, which changes the observation/evaluation
  split and therefore the pre-registered hypotheses).
- A fix would require changing one of the hard invariants above.
- You need a new dependency.

Report what you found and what you would do. Do not proceed.

## Tone for reports back

Say what ran, what the numbers were, and what surprised you. Flag anything that
looked wrong even if you worked around it — a discrepancy between the offline
violation count and the cluster's kernel-enforced count is a research finding,
not a nuisance to smooth over.
