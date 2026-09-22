# Data quality: the fastStorage trace, and the cluster that validates against it

Two instruments produce numbers in this project: the trace, and the Kubernetes
testbed. This note records what is wrong with each and what was decided about
it. Everything up to "Measurement quality on the cluster side" is about
capacity-column corruption in the trace; that section onward is about cgroup
memory accounting in the testbed.

This note exists because a single corrupted VM silently moved a headline
number in the report by more than half, and because building a
general-purpose check — rather than patching around that one VM — turned up
a second problem that was, at the time, live in numbers already reported.
Both are recorded here, including the decision made about the second
problem and what it cost, so neither has to be rediscovered.

## The short version

Two columns are supposed to be constant (or near enough) for a given VM
across its whole trace: `CPU capacity provisioned [MHZ]` and `Memory capacity
provisioned [KB]`. `src/baselines.py`'s `load_vm` and `src/inspect_traces.py`
both read that "constant" from the **first sample** of the file (`iloc[0]`).
That is a reasonable simplification only if the first sample is actually
representative — and for a meaningful slice of this trace, it is not.

Investigating why (patterns 1-3) produced **three distinct corruption
patterns**, not one. A fourth issue (pattern 4) is a different kind of
problem — not corrupted data, but data the flavour catalog cannot represent
at all:

1. **First-row corruption** (64 VMs: 54 of the 103 "bad CPU capacity" VMs,
   plus `314.csv`) — the file is otherwise consistent, but the very first
   sample is garbage, poisoning any function that trusts `iloc[0]`.
2. **Sustained majority-zero** (49 of the 103 "bad CPU capacity" VMs) — the
   capacity column reads zero for the *majority* of the file, not just the
   first sample. This is not corruption of a single reading; the VM was very
   likely powered off, deallocated, or migrated for most of the 30-day
   window.
3. **Genuine mid-trace capacity shift** (4 VMs, found with a dedicated
   detector, not previously known) — the capacity column holds two
   materially different values, each sustained for a real share of the
   file, rather than one value plus noise or a single bad sample. Not
   corruption — a violation of the "capacity is constant" assumption the
   loaders make.
4. **Below the flavour catalog floor** (347 VMs, found while investigating
   why per-VM reclamation percentages were unusable — see "Worked example C"
   below) — the VM's own original allocation is smaller than the smallest
   flavour in the catalog (2 GB memory / 1 vCPU). Not corrupted data at all;
   the data is fine, but no catalog entry exists to right-size it *to*.

Pattern 1 is safely recoverable by substituting the file's median for the
first sample, but that recovery has not been implemented — pattern 1 VMs
remain excluded via the same gate that originally caught them. Pattern 2 is
not recoverable this way (see below). **Patterns 3 and 4 are both decided:
excluded**, alongside patterns 1 and 2 — see their sections below for the
rationale.

## The exclusion counts, and the exact rule producing each

Starting population: **1250** files in `data/fastStorage/`.

| Stage | Rule | Excludes | Where |
|---|---|---|---|
| "bad CPU capacity" | `cores <= 0 or cap_mhz <= 0` on the first sample (after numeric coercion and dropping rows with any NaN in the required columns) | 103 | `baselines.load_vm`, mirrored in `inspect_traces.py` |
| "below flavour catalog floor" | `cores < min(FLAVOUR_VCPU)` (1) `or mem_cap_gb < min(FLAVOUR_MEM_GB)` (2), on the first sample — checked after the bad-capacity gate, so a VM already caught there isn't double-counted | 347 | `baselines.load_vm`, mirrored in `inspect_traces.py` |
| "mid-trace capacity shift" | `detect_capacity_shift()` on either capacity column returns a shift (see below) — checked after both gates above | 2 | `baselines.load_vm`, mirrored in `inspect_traces.py` |
| "short span" | The VM's own timestamp range does not cover `obs_days + eval_days` (21 + 9 = 30 days), allowing a shortfall of up to one sampling interval for boundary rounding | 6 | `baselines.split_obs_eval`, mirrored in `inspect_traces.py` |
| **usable** | passes all four | **792** | `results/dataset_report.csv`, `results/per_vm_results.csv` |

All four gates are **hard exclusions** — the VM does not appear in the
usable set at all. Gate order matters for which reason gets recorded, not
for the final usable count: `331.csv` (one of the 4 VMs the shift detector
flags) is intercepted by the bad-capacity gate before reaching the shift
check; `752.csv` (also shift-flagged) has memory below the catalog floor and
is now intercepted by the floor gate instead, which is why the mid-trace-shift
count is 2, not the 4 raised by the detector itself (see "Scanning
specifically for mid-trace shifts" below). The floor gate's introduction
also intercepts 36 VMs that were previously counted under "short span" —
their true problem was that they were both short *and* below the floor, and
the floor gate now runs first — which is why that count dropped from 42 to
6.

### The exclusion cascade

The four gates are applied in a fixed order inside `load_vm` (bad-capacity →
below-floor → mid-trace-shift → short-span), and a VM is removed by the
**first** gate it fails — it is never evaluated against the later gates.
The categories are not disjoint in the underlying data (`752.csv` fails both
the floor gate and the short-span gate; 36 other VMs are both short *and*
below the floor; `331.csv` fails both the bad-capacity gate and the shift
detector), so the counts in the table above are not "the number of VMs with
this problem" in isolation — they are "the number of VMs whose *first*
disqualifying problem, in this check order, was this one." The cascade below
makes that explicit as a running total, so the arithmetic can be checked
directly rather than trusted:

| Step | Gate | VMs entering this gate | Caught by this gate | VMs remaining |
|---|---|---|---|---|
| 0 | (start: every file in `data/fastStorage/`) | — | — | 1250 |
| 1 | bad CPU capacity (`cores <= 0 or cap_mhz <= 0`) | 1250 | 103 | 1147 |
| 2 | below flavour catalog floor (`cores < 1 or mem_cap_gb < 2`) | 1147 | 347 | 800 |
| 3 | mid-trace capacity shift (`detect_capacity_shift`) | 800 | 2 | 798 |
| 4 | short span (`split_obs_eval` returns `None`) | 798 | 6 | 792 |
| — | **usable** | | | **792** |

Total caught: 103 + 347 + 2 + 6 = **458**. 1250 − 458 = **792**, matching the
usable count exactly. Every excluded VM is counted in exactly one row (the
first gate it failed); no VM is counted twice, and no VM is uncounted — a
reviewer can re-derive 792 from this table alone without reading any other
section of this document.

### Why the exclusions are not selection bias

Four rounds of exclusion, cutting the usable fleet from 1250 to 792, is a
legitimate question to ask a skeptic's version of: were VMs removed because
they were inconvenient for the results?

- **(a) Every gate was derived from a data-quality investigation, not from
  its effect on the results.** The bad-capacity and short-span gates came
  from T1 preflight and the observation/evaluation split, both fixed before
  any baseline was run. The mid-trace-shift detector was built and validated
  against known cases (`133.csv`, `314.csv`) before it was run against the
  full trace to see what it would find. The below-floor gate was found while
  debugging why a diagnostic *percentage* was returning -360% for idle VMs —
  a data-integrity question, not a results-tuning one — and only after
  finding the cause was its effect on the fleet totals computed. In no case
  was a gate's threshold or condition chosen, or adjusted, after seeing what
  it would do to a reclamation or safety figure.
- **(b) The exclusions made the headline safety figure worse for
  right-sizing, not better.** If exclusions were selection bias in favour of
  the methods under test, the retained population would look safer than the
  full one. It doesn't: non-idle `p95` safety **fell** from 42.82% (the
  1102-VM population, after pattern 3 alone) to **37.06%** (the current
  792-VM population, after pattern 4). Removing VMs that could not have been
  fairly measured did not flatter the result — it made the risk this project
  exists to quantify look larger, not smaller.
- **(c) The retained 792 are the VMs that could actually have been
  right-sized.** Every excluded VM fails one of two tests: its capacity
  telemetry is not trustworthy enough to know what it was allocated (patterns
  1/2/3 — corrupted, mostly-zero, or non-constant), or it was already smaller
  than the smallest flavour this catalog offers (pattern 4), so no
  right-sizing decision was even available to make. Neither is a judgment
  about whether a VM's *behaviour* was favourable or unfavourable to any
  method — both are prior questions about whether the VM is a valid unit of
  analysis at all under this project's stated assumptions (a real capacity
  value, and a catalog with a floor). The 792 that remain are exactly the
  VMs for which "right-size this VM" was a well-posed question in the first
  place.

Separately, one **soft flag** (reports, does not exclude — because pattern
1/2's disposition is still open) remains from this pass:

| Check | Rule | Flags (of 1250 scanned) | Where |
|---|---|---|---|
| capacity glitch | `capacity_glitch_ratio(series) = first_sample / file_median`; flagged when the ratio is `> 10.0` or `< 0.1`; not flagged when the median is non-positive/non-finite (nothing to compare against) | 65 total (63 CPU, 64 memory) | `baselines.capacity_glitch_ratio` / `is_capacity_glitch` → `results/capacity_glitch_report.csv` |

A second detector, `detect_capacity_shift` (sort all values, greedily merge
into "levels" each within 5% of its level's running mean, keep only levels
covering ≥5% of rows as "sustained", flag if the highest and lowest sustained
levels differ by more than 10x), found the pattern-3 VMs and is **no longer
a soft flag** — it is now also wired into `load_vm` as a hard exclusion (see
above). `inspect_traces.py` still writes its detail (levels, ratios) to
`results/capacity_shift_report.csv` for the record, not as an open question.

The glitch check asks "does the first sample match the rest of the file?" —
it is blind to anything that isn't about row 0. The shift check asks "does
this file hold two different real values?" regardless of which one comes
first or how large each segment is — it is the one that found the pattern-3
VMs, because none of them have a corrupted first sample; they have two
genuine values.

Both checks use a 10x threshold: a coarse, order-of-magnitude line, chosen
because no genuine capacity resize this large was expected in this trace and
no corruption smaller than that would be worth chasing. The shift check
turned out to find genuine resizes at exactly this scale (11.8x–27.7x), so
the threshold is doing real work, not just filtering noise.

### Confirming the glitch-check overlap, not assuming it

Before adding the glitch check, the expectation was that it would catch
"314.csv plus the 103 bad-capacity VMs" — i.e. that all of this was one
failure mode. Running it against all 1250 files showed that expectation was
half right:

| | count | why |
|---|---|---|
| Of the 103 "bad CPU capacity" VMs, caught by the glitch check | 63 | first sample is 0 (or near it) against a file whose median is a clean, positive value — pattern 1 |
| Of the 103, **missed** by the glitch check | 40 | all of these are pattern 2 (sustained majority-zero) — 40 of that pattern's 49 VMs; the other 9 slipped past into the row above. The capacity column is zero for more than half the file, so the file's own median is *also* ~0. A first-vs-median ratio has nothing usable to compare against — this is not a shortcoming of the 10x threshold, it is a structurally different problem that a median-based check cannot see, by construction. |
| `314.csv` | caught, as expected | memory ratio 0.000732 (first sample 6,144 KB vs. file median ≈8.39M KB, ≈1365x off). CPU side is fine (ratio 1.0) — the corruption is memory-only. |
| `133.csv` | caught, **not previously known** | memory ratio 12.4 — but not a corrupted first sample. Pattern 3, below. |

So the glitch check catches all 54 pattern-1 VMs of the 103, plus 9 pattern-2
VMs whose zero/non-zero split happened to land the right side of 50%, plus
`314.csv` and one pattern-3 VM as side effects. It does not explain the
remaining 40 of the 103 (all pattern 2), and a dedicated check was needed to
find the rest of pattern 3.

The three counts are checkable against
`results/bad_capacity_characterisation.csv`, which classifies all 103:
`mostly_valid_bad_first_row` 54, `intermittent_zero` 49.

### Scanning specifically for mid-trace shifts

A second, purpose-built detector (`detect_capacity_shift`) was run across
all 1250 files, independent of the glitch check, looking for two sustained
levels rather than a corrupted first sample. It found exactly **4 VMs**, not
just `133.csv`:

| VM | Column | Low level | High level | Ratio | Low-level share of file | Excluded via... |
|---|---|---|---|---|---|---|
| `133.csv` | memory | 20.16 GB | 256.51 GB | 12.7x | 43.9% | mid-trace capacity shift gate (new) |
| `295.csv` | memory | 1.15 GB | 31.87 GB | 27.7x | 16.1% | mid-trace capacity shift gate (new) |
| `331.csv` | memory | 1.35 GB | 15.87 GB | 11.8x | 6.3% | bad-CPU-capacity gate (pre-existing) — also has `cores=0` on row 0, one of the 103 "bad CPU capacity" (`mostly_valid_bad_first_row`, pattern 1); the shift check never runs on it because the bad-capacity gate returns first |
| `752.csv` | CPU | 2600 MHz | 41,600 MHz | 16.0x | 15.8% | mid-trace capacity shift gate (new) — previously excluded via "short span" (18.1 days) instead, now caught earlier; also shows multiple change points on both CPU and memory, not one clean step like the others |

Before this pass, `133.csv` and `295.csv` were in the usable set and in
`results/per_vm_results.csv` with their stale, pre-shift `iloc[0]` capacity
live in reported T2 numbers (see "Example B" below for exactly how much).
`331.csv` is a **compound case** — it independently exhibits both pattern 1
(corrupted first row) and pattern 3 (a separate, real memory resize later in
the file); it was already excluded before this pass via the bad-capacity
gate, and remains excluded via the same gate now. `752.csv` was already
excluded (short span) before this pass; it is now also caught by the new
mid-trace-shift gate before reaching the span check, which is why the
"short span" count dropped from 43 to 42 **at that point in the
investigation**. The pattern-4 floor gate, added afterwards, took the same
count from 42 to its final value of **6** — and intercepted `752.csv` itself,
which is why the final mid-trace-shift count is 2 rather than 3. The table at
the top of this document is the final state; the 43 and 42 figures are kept
here only to show the order in which the gates were added.

## Pattern 1: first-row corruption (recoverable)

54 of the 103 "bad CPU capacity" VMs have a valid, consistent capacity value
in ≥95% of rows (e.g. `113.csv`: 8 cores, valid in 8,337 of 8,344 rows), with
only the first sample reading zero — plausibly a boot/provisioning-time
telemetry blip at the start of the collection window. `314.csv` shows the
identical shape on the memory column specifically: every row reads ≈8.2–8.4M
KB except the first, which reads 6,144 KB.

**These are recoverable**: reading the capacity from the file's median
(or mode) instead of `iloc[0]` would restore a correct value for all 54,
without needing to exclude them. This has **not been done** — see "Open
decisions" below.

## Pattern 2: sustained majority-zero (not recoverable this way)

The remaining 49 of the 103 "bad CPU capacity" VMs read zero for a large
majority of the file — `10.csv` is representative: only 3.37% of its 16,140
rows are non-zero, with capacity reading 4 cores when it does show a value.
This is consistent with the VM being powered off, deallocated, or migrated
for most of the 30-day window, not a single bad reading.

Reading the median would not recover a meaningful capacity for these VMs —
the median itself is zero (or absent of signal) for most of them, which is
exactly why only 9 of the 49 were even caught by the glitch check (their
zero/non-zero split happened to land just past 50/50 in the non-zero
direction, giving a technically-positive but not necessarily meaningful
median). Median substitution is not proposed for this pattern.

## Pattern 3: genuine mid-trace capacity shift (decided: excluded)

Four VMs hold two materially different, each-sustained capacity values
across the file — not a corrupted single sample, but a clean transition from
one real value to another partway through collection.

`133.csv` is the clearest example: `cores` (32) and `cap_mhz` (≈86,400 MHz)
are constant throughout, but memory capacity is 268,974,080 KB (256 GB) for
the first ≈4,297 of 8,618 rows, transitions over roughly a dozen samples,
and then holds in the 19–22M KB range (≈20 GB) — fluctuating slightly, 167
small change points in total — for the rest of the file.

`load_vm` and `inspect_traces.py` both read the **first** sample, so before
this decision, `133.csv` and `295.csv`'s provisioned-memory baseline was
whichever value happened to come first — in both cases, the *high* one —
regardless of which value actually covers more of the trace or which one the
evaluation window falls into. Both are now excluded rather than assigned
either value.

### It was live in numbers already reported, before this decision

Before this pass, `133.csv` and `295.csv` were both in
`results/per_vm_results.csv` from the T2 baseline run, using their pre-shift
`iloc[0]` capacity:

| VM | `orig_mem_gb` used | Real segments |
|---|---|---|
| `133.csv` | 256.51 GB | 43.9% of file at 20.16 GB, 49.9% at 256.51 GB |
| `295.csv` | 31.87 GB | 16.1% of file at 1.15 GB, 83.9% at 31.87 GB |

Excluding just these two VMs and recomputing the fleet totals
(`results/capacity_shift_exclusion_impact.csv`) — this is what was actually
applied, not a hypothetical — gives:

| Scope | Method | VMs before → after | `recl_vcpu_hours` before → after (Δ%) | `recl_mem_gb_hours` before → after (Δ%) | `vms_with_violation` before → after |
|---|---|---|---|---|---|
| all_vms | peak | 1104 → 1102 | 269,784.0 → 264,600.0 (−1.9%) | 738,155.3 → 696,599.8 (**−5.6%**) | 57 → 57 |
| all_vms | p95 | 1104 → 1102 | 339,984.0 → 333,936.0 (−1.8%) | 1,687,691.3 → 1,627,559.8 (**−3.6%**) | 293 → 291 |
| non_idle | peak | 406 → 404 | 86,400.0 → 81,216.0 (−6.0%) | 329,359.2 → 287,803.7 (**−12.6%**) | 32 → 32 |
| non_idle | p95 | 406 → 404 | 126,792.0 → 120,744.0 (−4.8%) | 1,176,943.2 → 1,116,811.7 (**−5.1%**) | 233 → 231 |

Two VMs out of 1104 (0.18% of the then-usable fleet) accounted for a
3.6–12.6% swing in reported memory-reclamation totals depending on scope and
method — the non-idle/peak figure moved the most because the denominator
there is smaller and both VMs are non-idle. CPU totals moved much less
(≈2–6%), because the shift on both VMs is memory-only.

`results/per_vm_results.csv` reflected this exclusion alone at the time
(1102 usable VMs; 792 today, after pattern 4 below was also decided); the
table above is kept as the record of what the pattern-3 decision cost on its
own, computed before it was applied.

### The decision: exclude

All four mid-trace-shift VMs (`133.csv`, `295.csv`, `331.csv`, `752.csv`) are
excluded, alongside patterns 1 and 2, via `detect_capacity_shift` wired into
`baselines.load_vm` (and mirrored in `inspect_traces.py`).

**Rationale.** A VM whose provisioned capacity changes mid-trace is not one
right-sizeable entity — the observation window describes a machine that no
longer exists by the evaluation window. The other two options considered
were rejected on the same grounds:

- **Splitting at the shift point** leaves sub-windows too short for a 21/9
  observation/evaluation split — `133.csv`'s two segments are each roughly
  15 days, `295.csv`'s low segment is under 5 days, nowhere near enough for
  either half alone to support the pre-registered split.
- **Using post-shift capacity** pairs the VM's demand history — which spans
  both the pre- and post-shift period — with an allocation that was only
  ever true for part of that history, which is the same category of error
  this whole investigation started from (a capacity value that doesn't match
  the demand it's being divided against or measured relative to).

Exclusion is the option that doesn't manufacture a number for a situation
the trace doesn't actually contain data to support.

## Pattern 4: below the flavour catalog floor (decided: excluded)

This pattern was found while trying to directly verify (rather than infer
from an aggregate) the mean per-VM memory-reclamation percentage for idle
vs. non-idle VMs — see "Worked example C" below. The per-VM figures for idle
VMs came out as roughly -360%, which is not a computation bug; it is what
happens when a VM's own original allocation is smaller than the smallest
flavour in the catalog.

**Rule:** `cores < min(FLAVOUR_VCPU)` (i.e. below 1 vCPU) `or mem_cap_gb <
min(FLAVOUR_MEM_GB)` (below 2 GB), measured on the first sample, checked
after the bad-capacity gate (so a VM with `cores == 0` is still attributed
to that gate, not this one).

**Count:** **347 VMs**, or **27.8% of the full 1250-file population** (and
31.5% of the 1102 VMs usable before this gate was added). Confirmed by
direct scan, not assumed. All 347 fail on the *memory* side — every VM in
this trace with `cores` below 1 already had `cores == 0` and was already
caught by the bad-capacity gate, so pattern 4 is, in this trace, entirely a
memory-floor phenomenon. The 347 VMs' original memory allocation ranges from
0 GB up to just under 2 GB, median ≈0.14 GB (`1009.csv`: 0.29 GB; `1008.csv`:
1.87 GB — both genuine, unremarkable readings, not corruption; `mem_cap_gb`
is constant and sane throughout each file). Full scan in
`results/below_catalog_floor_scan.csv`.

**Rationale.** A VM already smaller than the smallest available flavour
cannot be right-sized — there is no smaller catalog entry to snap it to.
Every method (`peak`, `p95`, and implicitly `as_provisioned` if it were ever
compared against the catalog) is forced by `snap_up` to allocate *more* than
the VM already had, for every one of these 347 VMs, regardless of how little
they actually use. Measuring "reclamation" against a VM like this doesn't
measure whether the recommender is good at right-sizing; it measures the gap
between this trace's smallest real VMs and this project's catalog's smallest
flavour — i.e. it measures the catalog, not the method. Excluding them keeps
the reported reclamation figures about right-sizing decisions that could
actually occur in this catalog.

This interacts with pattern 3: `752.csv` (previously attributed to the
mid-trace-shift gate) has first-sample memory of 0.73 GB, below the floor,
so it is now excluded here instead — still excluded, same VM, different
(and, once both gates exist, more fundamental) reason.

## Worked examples: why per-VM quality gates must come before fleet aggregates

### Example A — one corrupted VM moved a headline percentage by more than half

T1's original preflight (before either hard gate existed, all 1250 files
included) reported:

- mean memory utilisation: **24.2%**
- mean memory p95: **65.3%**

After the span and bad-capacity gates were added (1104 usable VMs), the same
computation gives:

- mean memory utilisation: **9.6%**
- mean memory p95: **18.0%**

The entire gap is explained by one VM: `314.csv`. In the original 1250-file
scan, its own `mem_mean_util` was 160.6 (16,060%) and `mem_p95_util` was
530.8 (53,080%) — direct consequences of dividing real memory-usage readings
by its corrupted 6,144 KB first-sample capacity. Averaged unweighted across
1250 VMs, that one file contributed ≈12.8 percentage points to the 24.2%
mean and ≈42.5 percentage points to the 65.3% p95 figure — i.e. it accounts
for roughly half the old mean and nearly all of the old p95 headline number.
`314.csv` is now excluded (via the unrelated short-span gate, as a side
effect), and checking the remaining 1104 usable VMs (the count at that point
in the investigation, before the pattern-3 and pattern-4 decisions below;
792 today) found no comparable outlier (worst `mem_max_util` is 4.2x,
nowhere near 1327x).

### Example B — two live VMs were inflating currently-reported reclamation totals

See "It was live in numbers already reported" above: before their exclusion,
`133.csv` and `295.csv` contributed a combined 3.6–12.6% of the reported
memory-reclamation totals depending on scope and method, computed against a
capacity value that was only true for part of each VM's life. Both are now
excluded (see "The decision: exclude").

### Example C — an unweighted per-VM mean was actively misleading, not just noisy

Trying to directly verify "does idle pull memory reclamation up or down?"
(rather than infer it from the all-VMs-vs-non-idle aggregate) by computing
the mean, across VMs, of each VM's own `recl_mem_gb_hours / (orig_mem_gb *
eval_hours)` gave: **-360.70%** (idle, `peak`) and **-355.54%** (idle,
`p95`). Not a bug — 273 of 698 idle VMs (at the time, pre-pattern-4) had
`orig_mem_gb` below the catalog floor, so `snap_up` forced their allocation
*upward*, producing negative reclamation that, divided by a near-zero
original allocation (one VM: 58 MB), blew up to numbers as extreme as
-419,749% for that single VM. **This finding is what led to pattern 4.**

The fix is not a better mean — it's excluding the VMs for which "percentage
of *my own* allocation" is not a meaningful question, and reporting a
GB-hour-weighted (pooled) figure instead, which is immune to any single VM's
denominator.

#### Current figures (792-VM population)

Pooled, GB-hour-weighted memory reclamation, computed from
`results/per_vm_results.csv` and reconciled against
`results/baseline_summary.csv`:

| Method | idle (n=425) | non-idle (n=367) | gap |
|---|---|---|---|
| `peak` | **63.55%** | 18.27% | **+45.28pp** |
| `p95` | **76.52%** | 69.32% | **+7.20pp** |

**Idle VMs inflate pooled memory reclamation under both methods.** The
inflation is large under `peak` (+45pp) and small under `p95` (+7pp), because
`p95` already reclaims most of what a non-idle VM's headroom offers, leaving
less room for idle machines to widen the gap.

#### The direction reversed when pattern 4 was excluded

The previous version of this section reported idle `p95` = 62.81% against
non_idle `p95` = 68.48% and concluded that idle **depresses** `p95` memory
reclamation. **That conclusion no longer holds.** Those figures were computed
on the pre-pattern-4 1102-VM population, which still contained the 347
below-floor VMs — VMs that `snap_up` forces *upward*, contributing negative
reclamation, and which are disproportionately idle (the idle count fell
698 → 425 when they were excluded, against 404 → 367 for non-idle). Removing
that negative contribution raised the idle figure from 62.81% to 76.52% and
flipped it above non-idle. The historical numbers are retained here only as an
illustration of how large the pattern-4 effect was; they are not a live claim.

The reversal is not only historical, either: on the current population the
pooled and unweighted statistics *still* disagree in sign with each other. That
is a reporting-convention question rather than a data-quality one, so it has
its own section — see **"Pooled versus unweighted reclamation"** below.

The pathology that produced example C is gone with pattern 4 excluded: the
worst per-VM memory reclamation on the current population is **-94.12%**
(idle, `peak`), not -419,749%, and no VM in `results/per_vm_results.csv` now
has `orig_mem_gb` below the 2 GB floor.

### The general lesson

An unweighted fleet-wide mean (or sum) over per-VM figures has no protection
against a single VM with a corrupted, non-representative, or structurally
unmeasurable input — example A is a >50% swing in a headline percentage from
one file, example B is a multi-percent swing in reclamation totals from two
files out of 1104 (0.18% of the fleet), example C is a statistic so
distorted (-360%) it couldn't be reported at all. Per-VM data-quality gates
have to run *before* any fleet aggregate is computed and reported, not after
a suspicious number prompts someone to go looking; and the aggregate itself
has to be pooled/weighted, not an unweighted mean of per-VM ratios, because
a per-VM ratio's denominator can be arbitrarily close to zero. All three
examples in this trace were found in the order stated, each only after the
previous number was already public.

## Pooled versus unweighted reclamation

Two reasonable ways to average a per-VM reclamation percentage across a fleet
give **opposite answers on the same 792 VMs**. This section records which one
this project uses and why, because the choice is load-bearing: it decides the
sign of the idle-versus-non-idle comparison.

| Method | Statistic | idle (n=425) | non-idle (n=367) | Direction |
|---|---|---|---|---|
| `p95` | **pooled** (GB-hour-weighted) | **76.52%** | **69.32%** | idle **higher** |
| `p95` | unweighted mean of per-VM % | 49.60% | 56.84% | idle **lower** |

- **Pooled** = `sum(reclaimed GB-hours) / sum(original GB × eval hours)`. One
  fleet-level ratio; each VM contributes in proportion to its size.
- **Unweighted** = the mean, across VMs, of each VM's own
  `recl_mem_gb_hours / (orig_mem_gb × eval_hours)`. Each VM counts once
  regardless of size.

### The mechanism

Reclamation percentage rises steeply with VM size, and much more steeply for
idle VMs. Mean per-VM `p95` memory reclamation, bucketed by original
allocation:

| Original size | idle n | idle mean | non-idle n | non-idle mean |
|---|---|---|---|---|
| 0–4 GB | 216 | 25.9% | 40 | 27.1% |
| 4–8 GB | 114 | 63.5% | 75 | 56.9% |
| 8–16 GB | 70 | 84.0% | 106 | 62.6% |
| 16–32 GB | 9 | 90.9% | 99 | 58.5% |
| 32+ GB | 16 | 96.0% | 47 | 65.7% |

A large idle VM collapses almost all the way to the 2 GB catalog floor, so its
reclamation approaches 96%. A VM already at 2 GB cannot be reclaimed at all and
scores 0%. Idle VMs cluster at the small end — 216 of 425 sit below 4 GB,
median 4.0 GB against 15.6 GB for non-idle — so:

- the **unweighted** mean is dominated by the many small idle VMs that can
  barely be reclaimed, dragging idle *below* non-idle;
- the **pooled** figure weights by GB-hours, so the 16 idle VMs above 32 GB
  reclaiming ~96% dominate the idle numerator, lifting idle *above* non-idle.

Neither statistic is wrong. They answer different questions: "what share of
this fleet's provisioned memory can be reclaimed?" (pooled) versus "what
happens to a typical VM?" (unweighted).

### The convention this project uses

**Every reclamation figure reported anywhere in this project is the pooled,
GB-hour-weighted one** — `results/baseline_summary.csv`, every table in this
document, every figure in `figures/`, and the numbers quoted in the report.
The unweighted mean appears nowhere as a result.

Two reasons:

1. **It answers the operator's question.** The project exists to quantify
   reclaimable capacity in a private cloud; capacity is measured in GB-hours,
   not in VMs. A 64 GB VM freeing 60 GB matters 32x more than a 2 GB VM
   freeing 2 GB, and the pooled figure says so.
2. **It has no exploding denominator.** A per-VM ratio divides by that VM's own
   allocation, which can be arbitrarily close to zero — this is exactly what
   produced the -419,749% figure in worked example C above and led to the
   pattern-4 exclusion. Pooling divides by a fleet-wide total, so no single VM
   can distort it.

The unweighted column is shown here, once, solely to document that the
convention is doing real work. Anyone comparing this project's idle-versus-
non-idle numbers against another study should check which convention that
study used before concluding the results disagree.

## Limitation: flavour catalog quantisation inflates the peak-vs-p95 gap

Once pattern 4 made clear that catalog granularity can distort a
reclamation figure enough to flip its sign, the same question was asked of
the headline peak-vs-p95 comparison: how much of "p95 reclaims far more
memory than peak" is workload behaviour, and how much is an artefact of
`snap_up` rounding `peak` further above its raw value than `p95` is, on
average?

Computed on the **current 792-VM population** by running the baselines twice —
once normally, and once with `--unsnapped`, which allocates the raw continuous
`max()` / `quantile(0.95)` instead of the smallest catalog entry at or above
it. Pooled/GB-hour-weighted throughout. Sources:
`results/baseline_summary.csv` (snapped),
`results/unsnapped/baseline_summary.csv` (counterfactual), compared in
`results/memory_snap_comparison.csv`.

| Scope | Method | Snapped % | Unsnapped % | peak→p95 gap, snapped | gap, unsnapped | Gap narrowing from removing snapping |
|---|---|---|---|---|---|---|
| all_vms | peak | 33.03% | 52.67% | 38.64pp | 32.08pp | **17.0%** |
| all_vms | p95 | 71.67% | 84.75% | | | |
| non_idle | peak | 18.27% | 41.07% | 51.05pp | 38.11pp | **25.3%** |
| non_idle | p95 | 69.32% | 79.18% | | | |

The "Snapped %" column now matches `results/baseline_summary.csv` exactly, so
the table can be cross-referenced against the headline results.

**Reproduce with:**

```
python3 src/baselines.py --traces data/fastStorage --out results \
    --obs-days 21 --eval-days 9
python3 src/baselines.py --traces data/fastStorage --out results/unsnapped \
    --obs-days 21 --eval-days 9 --unsnapped
```

`--unsnapped` is a **diagnostic counterfactual, not a policy** — no private
cloud provisions arbitrary fractional capacity, which is exactly why the
catalog exists (CLAUDE.md invariant 5). Its `run_config.json` records
`"snapped_to_catalog": false` so its outputs can never be mistaken for a
result, and it writes to its own directory. **Only the reclamation columns of
the unsnapped run are meaningful here.** Its violation columns are an artefact
of the counterfactual: allocating exactly the observation-window p95 leaves
roughly 5% of samples above the allocation by construction, so unsnapped `p95`
shows 10.61% of VMs violation-free against 63.26% snapped. That is the
counterfactual behaving as defined, not a finding about right-sizing.

<a id="historical-1102"></a>
Historical note, retained because it shows which population the quantisation
effect lives in: on the pre-pattern-4 1102-VM population the same comparison
gave snapped 28.50 / 66.59 (all_vms) and 17.65 / 68.48 (non_idle), unsnapped
52.66 / 84.75 and 41.12 / 79.19, for narrowings of 15.8% and 25.1%. The
**unsnapped** figures barely moved between populations (52.66→52.67,
84.75→84.75, 41.12→41.07, 79.19→79.18) while the **snapped** ones moved
several points. That is the pattern-4 signature: the 347 below-floor VMs were
distorted specifically by snapping — they were the VMs `snap_up` forced
upward — so excluding them changes the snapped figure and leaves the
continuous one alone.

The gap narrows but does not collapse: **17.0% of the all-VMs peak-vs-p95
memory gap, and 25.3% of the non-idle gap, is attributable to catalog
granularity rather than to any real difference in workload behaviour
between the two methods.** `peak` values sit further below catalog
boundaries on average than `p95` values do, so `snap_up` inflates `peak`'s
apparent allocation more, which mechanically widens the measured gap beyond
what the raw demand difference alone would produce.

**Implication for future work.** When the EVT method `M(eps)` (see
`ARCHITECTURE.md`) is evaluated, **H1 must be reported both snapped and
unsnapped**, the same way this comparison was done for the peak/p95
baselines — `src/baselines.py --unsnapped` exists precisely so that
counterfactual is a flag rather than a fresh analysis. If a quarter of a *baseline* method's gap against another
baseline turns out to be quantisation rather than method behaviour, the
same is plausibly true of `M(eps)`'s claimed advantage over `peak` or
`p95` — reporting only the snapped figure risks attributing a catalog
artefact to the method being evaluated. This is a stated limitation of
every reclamation percentage in this project that uses the flavour catalog,
not specific to peak/p95.

## Measurement quality on the cluster side

Everything above is about the trace. This section is about the other
measurement instrument in the project — the Kubernetes testbed — because the
same lesson applies there and was learned the same way: by a number coming out
wrong and the cause turning out to be the instrument rather than the workload.

Three properties of cgroup v2 memory accounting shape what the cluster numbers
can and cannot support.

### `memory.current` counts page cache, so `memory.peak` is an upper bound

The replay agent reads its trace CSV through its own cgroup, and cgroup v2
counts reclaimable page cache in `memory.current`. The kernel-maintained
high-water mark `memory.peak`, sampled by `testbed/sample_cgroups.py` and
recorded in `evidence/1{0..4}_wave{N}_cgroup_samples.tsv`, therefore
**overstates the anonymous footprint that actually drives an OOM**. It is an
upper bound on memory pressure, not a measure of live heap. Any reading of
those TSVs has to carry that qualification.

The consequence is visible directly in the results: VM **297** reached
**100.0% of its limit and survived**, while VMs **387** and **883** reached
100.0% and **died**. `memory.peak == limit` does not imply an OOMKill — the OOM
killer fires only when reclaim fails, and page cache from reading the trace file
is reclaimable.

### `memory.peak` is limit-dependent, so a generous probe overestimates overhead

cgroup v2 reclaim is *triggered by the limit*. A cgroup with room to breathe
accumulates footprint that the same workload under a tight limit would have had
reclaimed out from under it, so `memory.peak` measures the workload **and the
headroom it was given**.

Same VM, same trace, same agent, only the limit differs:

| Condition | `memory.peak` | Overhead above 125.95 MiB peak demand |
|---|---|---|
| Standalone probe, `--memory=1024m` | 185.14 MiB | 59.11 MiB |
| In-cluster, real 208 MiB limit | 175.6 MiB | **49.70 MiB** |

**Correct method for anything measuring headroom this way: measure under the
limit the workload will actually run with, or measure in-cluster.** A probe
given room to breathe answers a question nobody asked. This was an error in the
instrument, not in the workload, and it is the direct analogue of example A
above — one badly-conditioned measurement propagating into a derived constant.

### What this qualifies about flavour-independence

The memory margin was found to be **flavour-independent**: measured in-cluster
across five VMs spanning three flavours (2, 4 and 8 GB), it sits at 47.5–49.9
MiB — a spread of 2.4 MiB across a 4x range of buffer sizes. Flat, not
proportional. That is a positive result, and it is what licenses a single
additive constant across the whole catalog rather than a per-flavour table.

**The qualification:** all five of those pods had slack. A pod pressing its
limit has its reclaimable footprint evicted and its overhead compressed toward
the irreducible anonymous floor, exactly as the limit-dependence above
predicts. So 47.5–49.9 MiB is the **slack-case** figure, not a universal
constant, and it is an upper bound on what a tightly-limited pod needs. The
margin M = 80 MiB was chosen from a measured safe *band*, not from that point
estimate. The result was also measured at CPU pool width 1 only;
`MEASURED_POOL_WIDTH` in `gen_manifests.py` refuses generation outside that
regime rather than extrapolating.

### The margin band, and what the cluster cannot adjudicate

Because the enforced limit is the recommendation **plus** M, four of the 50
subset VMs (297, 332, 971, 1229) have peak demand falling between their
recommendation and their enforced limit. They exceed the quantity the offline
rule tests and never exceed the quantity the kernel tests, so their outcome is
decided by a harness property rather than by the recommendation under test.
**This testbed cannot adjudicate them.** They are exactly the run's four false
negatives — the error direction is conservative (missed violations, not
invented ones), and comparing like with like the offline rule reproduces the
kernel's verdict on 50 of 50 VMs. Full derivation in
`evidence/T8_confusion.md`; design rationale in `ARCHITECTURE.md`.

### The CPU axis is censored, not merely noisy

A memory violation is terminal, so an OOMKill ends the observation that would
have produced the rest of that pod's CPU evidence. 26 of 50 pods were
OOMKilled and all 26 are truncated; 2 are unscoreable on CPU entirely (fewer
than the 2 consecutive samples the CPU rule needs), the limiting case being VM
296 with 0 of 2,578 sample records. Their memory verdicts remain sound — the
OOMKill *is* the measurement — but a CPU confusion table over this population
would describe the pods that survived long enough to be observed, which is a
survivorship-selected population, not the one of interest. The CPU axis is
reported as a limitation rather than a result, and separately, at S = 1/16 both
available CPU signals are harness artifacts.

### The general lesson, restated for the cluster

Same shape as the trace-side lesson: a measurement's conditioning has to be
checked before the number it produces is used to derive anything else. The
59.11 MiB figure was not noisy — it was precise, reproducible, and measured
under conditions that did not match the ones it would be applied in. Precision
is not validity.

## Open decisions (not made in this pass)

1. **Pattern 1 (54 recoverable VMs + `314.csv`'s memory column):** switch
   `load_vm` / `inspect_traces.py` to read capacity from the file's median
   instead of `iloc[0]`? This would move 54 VMs from "excluded" to "usable"
   and fix `314.csv`'s memory baseline (`314.csv` would still be excluded
   separately by the short-span gate — its problem there is unrelated, only
   0.69 days of data). Not yet decided; needs the before/after VM count put
   in front of the person deciding.
2. **Pattern 2 (49 VMs):** no recovery proposed. Recorded here as correctly
   excluded, for the reason stated, so nobody re-derives "why 40 VMs are
   still gone even with a glitch gate" from scratch.

**Patterns 3 and 4 are no longer open** — see their "decision" sections
above. Current usable-VM count, all four gates applied: **792** (of 1250).
