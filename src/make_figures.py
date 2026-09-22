#!/usr/bin/env python3
"""
make_figures.py -- report figures, generated from the results CSVs.

Every number plotted here is derived from a CSV under --results (or the trace
directory, for the exclusion cascade). Nothing is transcribed from a document:
if a figure disagrees with the prose, the prose is stale, not the figure.

Two derivations are checked against a published figure before being plotted, so
a silent formula error cannot reach the report:

  * pooled reclamation % is re-derived per VM from per_vm_results.csv and
    verified against baseline_summary.csv (all four scope/method rows, both
    resources) -- see `check_pooled_pct`.
  * the exclusion cascade is re-derived by replaying load_vm's gate ORDER over
    the per-gate scan CSVs, and the surviving set is verified to equal
    dataset_report.csv exactly -- see `exclusion_cascade`.

Both raise rather than plot a wrong number.

Greyscale: no figure uses colour as its only channel. Bars are separated by
hatch, lines by dash pattern, scatter classes by marker shape.

Usage:
    python3 src/make_figures.py --out figures/
    python3 src/make_figures.py --out figures/ --only 2 5
"""

import argparse
import csv
import json
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import Patch

# Greyscale-safe: distinct lightness AND a distinct hatch/dash/marker each.
GREYS = ["#1a1a1a", "#5c5c5c", "#8f8f8f", "#c4c4c4"]
HATCH = ["", "///", "...", "xxx"]
DASH = [(0, ()), (0, (5, 2)), (0, (1, 1.5)), (0, (6, 2, 1, 2))]
MARKER = ["o", "s", "^", "D"]

CAPTIONS = []


def figpath(out, n, name):
    return os.path.join(out, f"fig{n}_{name}")


def save(fig, out, n, name, caption, sources):
    """Write PNG + SVG and register the caption."""
    os.makedirs(out, exist_ok=True)
    base = figpath(out, n, name)
    fig.savefig(base + ".png", dpi=200, bbox_inches="tight")
    fig.savefig(base + ".svg", bbox_inches="tight")
    plt.close(fig)
    CAPTIONS.append((n, os.path.basename(base), caption, sources))
    print(f"  wrote {base}.png / .svg")


def read(path):
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


def pct_of(vals, q):
    """Percentile without numpy's interpolation ambiguity: nearest-rank."""
    s = sorted(vals)
    if not s:
        return float("nan")
    k = max(0, min(len(s) - 1, int(round(q / 100.0 * (len(s) - 1)))))
    return s[k]


def cdf(vals):
    s = sorted(vals)
    n = len(s)
    return s, [(i + 1) / n for i in range(n)]


def order_key(r):
    """Conceptual order, not alphabetical: conservative method first, whole
    fleet before the non-idle subset."""
    return ({"all_vms": 0, "non_idle": 1}.get(r["scope"], 9),
            {"as_provisioned": 0, "peak": 1, "p95": 2}.get(r["method"], 9))


def style(ax, xlabel, ylabel, title=None):
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    if title:
        ax.set_title(title, fontsize=10)
    ax.grid(True, axis="both", lw=0.4, alpha=0.35, ls=":")
    ax.set_axisbelow(True)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)


# ---------------------------------------------------------------- derivations

def check_pooled_pct(results):
    """Re-derive pooled reclamation % from per-VM rows; verify against summary.

    Pooled (capacity-hour-weighted), never an unweighted mean of per-VM ratios
    -- docs/DATA_QUALITY.md example C explains why the latter is unusable here.
    """
    cfg = json.load(open(os.path.join(results, "run_config.json")))
    eval_hours = float(cfg["eval_days"]) * 24.0
    rows = read(os.path.join(results, "per_vm_results.csv"))
    summary = {(r["scope"], r["method"]): r
               for r in read(os.path.join(results, "baseline_summary.csv"))}

    def pooled(sub, num_col, cap_col):
        den = sum(float(r[cap_col]) * eval_hours for r in sub)
        return 100.0 * sum(float(r[num_col]) for r in sub) / den if den else 0.0

    def subset(scope, method):
        return [r for r in rows if r["method"] == method
                and (scope != "non_idle" or r["idle"] == "False")]

    for scope in ("all_vms", "non_idle"):
        for method in ("peak", "p95"):
            sub = subset(scope, method)
            got = (pooled(sub, "recl_vcpu_hours", "orig_cores"),
                   pooled(sub, "recl_mem_gb_hours", "orig_mem_gb"))
            want = (float(summary[(scope, method)]["recl_vcpu_pct"]),
                    float(summary[(scope, method)]["recl_mem_pct"]))
            for g, w, what in zip(got, want, ("vcpu", "mem")):
                if abs(g - w) > 0.05:
                    raise SystemExit(
                        f"pooled {what} % for {scope}/{method} derives as "
                        f"{g:.2f} but baseline_summary.csv says {w:.2f}. "
                        f"Refusing to plot a number that does not reconcile.")
    return rows, eval_hours, pooled, subset


def exclusion_cascade(results, traces):
    """Re-derive the 1250 -> 792 cascade in load_vm's gate order.

    The per-gate scan CSVs are NOT disjoint (a VM can be below the catalog floor
    AND short-span AND shift-flagged). The cascade counts each VM under the
    FIRST gate it fails, so gate order has to be replayed, not assumed. The
    surviving set is checked against dataset_report.csv.
    """
    def ids(path):
        rows = read(path)
        if not rows:
            return set()
        key = ("vm_id" if "vm_id" in rows[0]
               else "file" if "file" in rows[0] else list(rows[0])[0])
        return {str(r[key]).replace(".csv", "") + ".csv" for r in rows}

    if not os.path.isdir(traces):
        raise SystemExit(f"--traces {traces} not found; needed for the "
                         f"starting population of the cascade")
    start = {f for f in os.listdir(traces) if f.endswith(".csv")}
    usable = ids(os.path.join(results, "dataset_report.csv"))

    gates = [
        ("bad CPU\ncapacity", ids(os.path.join(
            results, "bad_capacity_characterisation.csv"))),
        ("below flavour\ncatalog floor", ids(os.path.join(
            results, "below_catalog_floor_scan.csv"))),
        ("mid-trace\ncapacity shift", ids(os.path.join(
            results, "capacity_shift_report.csv"))),
    ]

    remaining = set(start)
    caught = []
    for label, flagged in gates:
        hit = remaining & flagged
        remaining -= hit
        caught.append((label, len(hit)))
    # Short span has no scan CSV of its own; it is whatever the earlier gates
    # left behind that dataset_report.csv does not contain.
    span = remaining - usable
    caught.append(("short span", len(span)))
    survivors = remaining & usable

    if survivors != usable:
        raise SystemExit(
            f"cascade does not reconcile: {len(survivors)} survivors vs "
            f"{len(usable)} rows in dataset_report.csv")
    return len(start), caught, len(usable)


# ------------------------------------------------------------------- figures

def fig1_headroom(results, out):
    rows = read(os.path.join(results, "dataset_report.csv"))
    over = sum(1 for r in rows if str(r["cpu_over_100"]).lower() in ("true", "1"))
    fig, axes = plt.subplots(1, 2, figsize=(9.4, 3.9), sharey=True)
    handles = []

    for ax, (res, unit) in zip(axes, (("cpu", "CPU"), ("mem", "Memory"))):
        for i, stat in enumerate(("mean", "p95")):
            vals = [100.0 * float(r[f"{res}_{stat}_util"]) for r in rows]
            xs, ys = cdf(vals)
            ln, = ax.plot(xs, [100 * y for y in ys], color=GREYS[i * 2],
                          ls=DASH[i], lw=1.9,
                          label=f"per-VM {stat} utilisation")
            if not handles or len(handles) < 2:
                handles.append(ln)
            q = pct_of(vals, 95)
            ax.axvline(q, color=GREYS[i * 2], ls=DASH[i], lw=1.0, alpha=0.7)
            # Label along the top edge, rotated, so it never fights the curves
            # or the legend regardless of where the line lands.
            ax.annotate(f"95th pct, {stat}: {q:.0f}%",
                        xy=(q, 99), xytext=(-4, -2),
                        textcoords="offset points", fontsize=7.5,
                        color=GREYS[i * 2], rotation=90,
                        ha="right", va="top")
        style(ax, f"{unit} utilisation (% of provisioned)",
              "VMs at or below (%)" if res == "cpu" else "", unit)
        ax.set_xlim(0, 110)
        ax.set_ylim(0, 100)
        ax.axvline(100, color="black", lw=0.7, alpha=0.5)

    axes[0].annotate(f"{over} VMs report >100% CPU\n(telemetry artefact; demand\n"
                     f"is clipped to allocation on load)",
                     xy=(0.97, 0.06), xycoords="axes fraction", fontsize=7,
                     ha="right", va="bottom", style="italic")
    fig.legend(handles=handles, fontsize=9, frameon=False, ncol=2,
               loc="lower center", bbox_to_anchor=(0.5, -0.09))
    fig.suptitle(f"Objective O1 — allocated capacity vastly exceeds used, "
                 f"across {len(rows)} usable VMs", fontsize=11, y=1.0)
    save(fig, out, 1, "headroom",
         f"Cumulative distribution of per-VM CPU and memory utilisation across "
         f"the {len(rows)} usable VMs, each as a percentage of that VM's own "
         f"provisioned capacity; verticals mark the 95th percentile of each "
         f"distribution. The mass concentrated at low utilisation is the "
         f"headroom this project reclaims. {over} VMs report CPU above 100%, a "
         f"telemetry artefact that the baselines clip to the original "
         f"allocation on load.",
         "results/dataset_report.csv")


def fig2_reclamation_vs_safety(results, out):
    rows = [r for r in read(os.path.join(results, "baseline_summary.csv"))
            if r["method"] != "as_provisioned"]
    rows.sort(key=order_key)
    labels = [f"{r['scope'].replace('_', ' ')}\n{r['method']}" for r in rows]
    series = [("vCPU reclaimed", "recl_vcpu_pct"),
              ("Memory reclaimed", "recl_mem_pct"),
              ("VMs violation-free", "pct_vms_safe")]

    fig, ax = plt.subplots(figsize=(8.0, 4.0))
    n, w = len(series), 0.26
    for i, (name, col) in enumerate(series):
        xs = [x + (i - (n - 1) / 2) * w for x in range(len(rows))]
        vals = [float(r[col]) for r in rows]
        ax.bar(xs, vals, width=w, color=GREYS[i], hatch=HATCH[i],
               edgecolor="black", lw=0.6, label=name)
        for x, v in zip(xs, vals):
            ax.text(x, v + 1.2, f"{v:.1f}", ha="center", fontsize=7.5)

    ax.set_xticks(range(len(rows)))
    ax.set_xticklabels(labels, fontsize=9)
    style(ax, "", "percent (%)",
          "Reclamation is bought with safety, and the price is not uniform")
    ax.set_ylim(0, 105)
    ax.legend(fontsize=8.5, frameon=False, ncol=3, loc="upper center",
              bbox_to_anchor=(0.5, -0.13))
    save(fig, out, 2, "reclamation_vs_safety",
         "Reclaimed vCPU, reclaimed memory and the share of VMs left "
         "violation-free, for each scope/method combination. p95 reclaims more "
         "than peak on both resources and pays for it in safety; the price is "
         "steepest for non-idle VMs.",
         "results/baseline_summary.csv")


def fig3_safety_split(results, out):
    rows = [r for r in read(os.path.join(results, "baseline_summary.csv"))
            if r["method"] != "as_provisioned"]
    rows.sort(key=order_key)
    labels = [f"{r['scope'].replace('_', ' ')}\n{r['method']}" for r in rows]
    series = [("CPU safe", "cpu_pct_safe"), ("Memory safe", "mem_pct_safe")]

    fig, ax = plt.subplots(figsize=(7.6, 4.0))
    w = 0.34
    for i, (name, col) in enumerate(series):
        xs = [x + (i - 0.5) * w for x in range(len(rows))]
        vals = [float(r[col]) for r in rows]
        ax.bar(xs, vals, width=w, color=GREYS[i * 2], hatch=HATCH[i * 2 + 1],
               edgecolor="black", lw=0.6, label=name)
        for x, v in zip(xs, vals):
            ax.text(x, v + 1.2, f"{v:.1f}", ha="center", fontsize=8)

    ax.set_xticks(range(len(rows)))
    ax.set_xticklabels(labels, fontsize=9)
    style(ax, "", "VMs with no violation (%)",
          "Memory safety collapses while CPU safety holds")
    ax.set_ylim(0, 105)
    ax.legend(fontsize=9, frameon=False, ncol=2, loc="lower center",
              bbox_to_anchor=(0.5, -0.28))
    save(fig, out, 3, "safety_split",
         "Share of VMs left violation-free, split by resource. Memory safety "
         "falls far faster than CPU safety under the same recommendation, "
         "which is what justifies the asymmetric violation rule (memory "
         "terminal at one sample; CPU degrading, two consecutive samples).",
         "results/baseline_summary.csv")


def fig4_exclusion_cascade(results, traces, out):
    start, caught, final = exclusion_cascade(results, traces)

    fig, ax = plt.subplots(figsize=(8.4, 4.2))
    running = start
    ax.bar(0, start, width=0.62, color=GREYS[0], edgecolor="black", lw=0.7)
    ax.text(0, start + 14, f"{start}", ha="center", fontsize=9, weight="bold")

    for i, (label, n) in enumerate(caught, start=1):
        bottom = running - n
        ax.bar(i, n, bottom=bottom, width=0.62, color=GREYS[2],
               hatch="///", edgecolor="black", lw=0.7)
        ax.text(i, running + 14, f"-{n}", ha="center", fontsize=9)
        ax.plot([i - 0.31 - 0.38, i - 0.31], [running, running],
                color="black", lw=0.7, ls=(0, (2, 2)))
        running = bottom

    k = len(caught) + 1
    ax.bar(k, final, width=0.62, color=GREYS[0], edgecolor="black", lw=0.7)
    ax.text(k, final + 14, f"{final}", ha="center", fontsize=9, weight="bold")
    ax.plot([k - 0.31 - 0.38, k - 0.31], [final, final],
            color="black", lw=0.7, ls=(0, (2, 2)))

    ax.set_xticks(range(k + 1))
    ax.set_xticklabels(["all trace\nfiles"] + [c[0] for c in caught]
                       + ["usable"], fontsize=8.5)
    style(ax, "", "VMs remaining",
          "Every VM is counted under the first gate it fails")
    ax.set_ylim(0, start * 1.12)
    ax.legend(handles=[Patch(facecolor=GREYS[2], hatch="///",
                             edgecolor="black", label="excluded by this gate")],
              fontsize=8.5, frameon=False, loc="upper right")
    save(fig, out, 4, "exclusion_cascade",
         f"Data-quality exclusion cascade, {start} trace files down to {final} "
         f"usable VMs, one bar per gate in the order load_vm applies them. The "
         f"gates are not disjoint in the underlying data, so each VM is counted "
         f"under the first gate it fails.",
         "results/{dataset_report,bad_capacity_characterisation,"
         "below_catalog_floor_scan,capacity_shift_report}.csv "
         "+ file count in data/fastStorage/")


def fig5_t8_validation(results, out, seed):
    import random
    rows = []
    for w in range(1, 6):
        p = os.path.join(results, f"wave{w}_outcomes.csv")
        if not os.path.exists(p):
            raise SystemExit(f"missing {p}")
        rows += read(p)
    if "excess_over_budget_mib" not in rows[0]:
        raise SystemExit("wave outcome CSVs lack excess_over_budget_mib; "
                         "re-run testbed/wave_report.py")

    margin = float(rows[0]["margin_mib"])
    rnd = random.Random(seed)

    fig, ax = plt.subplots(figsize=(9.0, 3.9))
    groups = [
        ("OOMKilled", lambda r: r["obs_oomkilled"] == "True", 1.0, GREYS[0], "o"),
        ("Survived", lambda r: r["obs_oomkilled"] != "True", 0.0, GREYS[2], "s"),
    ]
    for name, pred, y, col, mk in groups:
        sub = [r for r in rows if pred(r)]
        xs = [float(r["excess_over_budget_mib"]) for r in sub]
        ys = [y + rnd.uniform(-0.13, 0.13) for _ in sub]
        ax.scatter(xs, ys, s=34, marker=mk, facecolor=col, edgecolor="black",
                   lw=0.6, zorder=3, label=f"{name} (n={len(sub)})")

    band = [r for r in rows if r["margin_band"] == "True"]
    bx = [float(r["excess_over_budget_mib"]) for r in band]
    by = [0.0 + rnd.uniform(-0.13, 0.13) for _ in band]
    ax.scatter(bx, by, s=150, marker="o", facecolor="none",
               edgecolor="black", lw=1.6, zorder=4,
               label=f"margin band (n={len(band)}), undecidable")
    # The band VMs sit within a few MiB of each other; fan the labels out
    # vertically so they stay legible instead of overprinting.
    for j, r in enumerate(sorted(band,
                                 key=lambda r: float(r["excess_over_budget_mib"]))):
        x = float(r["excess_over_budget_mib"])
        ax.annotate(f"VM {r['vm_id']} ({x:.0f})", xy=(x, -0.14),
                    xytext=(-38 + 26 * j, -18 - 13 * (len(band) - 1 - j)),
                    textcoords="offset points", ha="center", fontsize=7,
                    arrowprops=dict(arrowstyle="-", lw=0.5, alpha=0.7))

    ax.axvline(0, color="black", lw=1.0, ls=(0, (5, 2)))
    ax.axvline(margin, color="black", lw=1.4, ls=(0, (1, 1.2)))
    ax.text(0, 1.32, " offline threshold\n (recommendation)", fontsize=7.5,
            va="top")
    ax.text(margin, 1.32, f" enforced threshold\n (rec. + M = {margin:g} MiB)",
            fontsize=7.5, va="top")

    ax.set_xscale("symlog", linthresh=100)
    ax.set_yticks([0, 1])
    ax.set_yticklabels(["Survived", "OOMKilled"])
    ax.set_ylim(-1.05, 1.45)
    style(ax, "peak scaled demand minus scaled budget (MiB, symlog scale)", "",
          "Every pod right of the enforced threshold died; every pod left of "
          "it lived")
    ax.legend(fontsize=8, frameon=False, loc="lower left",
              bbox_to_anchor=(0.01, 0.56))

    mism = [r for r in rows
            if (float(r["excess_over_budget_mib"]) > margin)
            != (r["obs_oomkilled"] == "True")]
    if mism:
        raise SystemExit(f"figure 5 would misrepresent the result: "
                         f"{len(mism)} mismatches found")
    save(fig, out, 5, "t8_validation",
         f"Offline prediction against kernel outcome for the {len(rows)} "
         f"cluster-replayed VMs. The like-with-like rule -- demand above the "
         f"limit actually enforced -- reproduces the kernel's verdict with zero "
         f"mismatches. The {len(band)} VMs between the two thresholds exceed "
         f"their recommendation but not the enforced limit, so this testbed "
         f"cannot adjudicate them.",
         "results/wave{1..5}_outcomes.csv")


def fig6_idle_contamination(results, out):
    rows, eval_hours, pooled, _ = check_pooled_pct(results)
    methods = ["peak", "p95"]
    classes = [("idle", "True"), ("non-idle", "False")]

    fig, axes = plt.subplots(1, 2, figsize=(9.0, 3.8), sharey=True)
    panels = [("vCPU", "recl_vcpu_hours", "orig_cores"),
              ("Memory", "recl_mem_gb_hours", "orig_mem_gb")]
    gaps = {}  # (resource, method) -> idle minus non-idle, in percentage points

    for ax, (res, num, cap) in zip(axes, panels):
        w = 0.34
        for i, (cname, flag) in enumerate(classes):
            vals, counts = [], []
            for m in methods:
                sub = [r for r in rows if r["method"] == m and r["idle"] == flag]
                vals.append(pooled(sub, num, cap))
                counts.append(len(sub))
            xs = [x + (i - 0.5) * w for x in range(len(methods))]
            ax.bar(xs, vals, width=w, color=GREYS[i * 2],
                   hatch=HATCH[i * 2 + 1], edgecolor="black", lw=0.6,
                   label=f"{cname} (n={counts[0]})")
            for x, v in zip(xs, vals):
                ax.text(x, v + 1.4, f"{v:.1f}", ha="center", fontsize=8)
        ax.set_xticks(range(len(methods)))
        ax.set_xticklabels(methods)
        style(ax, "", f"reclaimed (% of provisioned, pooled)"
              if res == "vCPU" else "", f"{res}")
        ax.set_ylim(0, 118)
        # Gap annotation: the size of the idle contamination, per method.
        for x, m in enumerate(methods):
            i_sub = [r for r in rows if r["method"] == m and r["idle"] == "True"]
            n_sub = [r for r in rows if r["method"] == m and r["idle"] == "False"]
            gap = pooled(i_sub, num, cap) - pooled(n_sub, num, cap)
            gaps[(res, m)] = gap
            ax.annotate(f"gap {gap:+.1f} pp", xy=(x, 110), ha="center",
                        fontsize=7.5, style="italic")
    h, lb = axes[0].get_legend_handles_labels()
    fig.legend(h, lb, fontsize=9, frameon=False, ncol=2, loc="lower center",
               bbox_to_anchor=(0.5, -0.06))

    fig.suptitle("Idle VMs inflate reclamation on BOTH resources under peak; "
                 "only memory/p95 is nearly clean", fontsize=10.5, y=1.02)
    save(fig, out, 6, "idle_contamination",
         "Pooled (capacity-hour-weighted) reclamation for idle versus non-idle "
         f"VMs. Idle machines inflate the headline saving on both resources "
         f"under peak (CPU {gaps[('vCPU', 'peak')]:+.1f}pp, memory "
         f"{gaps[('Memory', 'peak')]:+.1f}pp); under p95 the CPU gap stays wide "
         f"({gaps[('vCPU', 'p95')]:+.1f}pp) while the memory gap nearly closes "
         f"({gaps[('Memory', 'p95')]:+.1f}pp), because an idle VM still carries "
         f"a real p95 memory floor even with near-zero CPU. Note that idle "
         f"inflates the POOLED figure while slightly depressing the unweighted "
         f"per-VM mean, because idle VMs cluster at small sizes where the 2 GB "
         f"catalog floor caps reclamation; docs/DATA_QUALITY.md example C "
         f"derives this.",
         "results/per_vm_results.csv + results/run_config.json "
         "(verified against results/baseline_summary.csv)")


def write_captions(out):
    path = os.path.join(out, "CAPTIONS.md")
    lines = ["# Figure captions", "",
             "Generated by `src/make_figures.py`. Every value is derived from "
             "the listed source; none is transcribed from a document.", ""]
    for n, base, cap, src in sorted(CAPTIONS):
        lines += [f"## Figure {n} — `{base}.png` / `.svg`", "",
                  cap, "", f"**Source:** `{src}`", ""]
    lines += ["---", "",
              "This trace was graciously provided by Bitbrains IT Services "
              "Inc.", ""]
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines))
    print(f"  wrote {path}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results", default="results")
    ap.add_argument("--traces", default="data/fastStorage",
                    help="trace directory; only used for the cascade's "
                         "starting population (figure 4)")
    ap.add_argument("--out", default="figures")
    ap.add_argument("--seed", type=int, default=0,
                    help="jitter seed for figure 5 (deterministic by default)")
    ap.add_argument("--only", nargs="+", type=int, default=[],
                    help="generate only these figure numbers")
    args = ap.parse_args()

    plt.rcParams.update({
        "font.size": 9.5,
        "axes.titlesize": 10,
        "figure.facecolor": "white",
        "savefig.facecolor": "white",
        "hatch.linewidth": 0.6,
        "pdf.fonttype": 42,
    })

    os.makedirs(args.out, exist_ok=True)
    print("verifying derivations against published results...")
    check_pooled_pct(args.results)
    print("  pooled reclamation % reconciles with baseline_summary.csv")

    jobs = [
        (1, lambda: fig1_headroom(args.results, args.out)),
        (2, lambda: fig2_reclamation_vs_safety(args.results, args.out)),
        (3, lambda: fig3_safety_split(args.results, args.out)),
        (4, lambda: fig4_exclusion_cascade(args.results, args.traces, args.out)),
        (5, lambda: fig5_t8_validation(args.results, args.out, args.seed)),
        (6, lambda: fig6_idle_contamination(args.results, args.out)),
    ]
    failed = []
    for n, fn in jobs:
        if args.only and n not in args.only:
            continue
        print(f"figure {n}:")
        try:
            fn()
        except SystemExit as exc:
            failed.append((n, str(exc)))
            print(f"  SKIPPED: {exc}", file=sys.stderr)

    # CAPTIONS.md describes the whole set. Rewriting it from a partial run would
    # silently drop the captions for figures this run did not regenerate.
    if args.only:
        print(f"\n--only given: left {os.path.join(args.out, 'CAPTIONS.md')} "
              f"untouched (it describes the full set)")
    else:
        write_captions(args.out)
    if failed:
        print("\nnot generated:")
        for n, why in failed:
            print(f"  figure {n}: {why}")
        return 1
    print(f"\nall figures written to {args.out}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
