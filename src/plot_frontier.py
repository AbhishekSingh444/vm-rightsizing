#!/usr/bin/env python3
"""
plot_frontier.py -- T10 figure: the risk/reclamation frontier.

x = % of VMs with at least one violation in the evaluation window (MEASURED,
    not the eps target -- CLAUDE.md invariant 7)
y = % of allocated capacity reclaimed

The M(eps) curve and the baselines are drawn on the same axes, which is the
whole point of the project: reporting both sides of the trade rather than the
savings alone. Up and to the left is better.

Baselines appear twice. Hollow markers are the Review 1 policies as published;
filled markers are the same policies capped at the original allocation, which
is the like-for-like comparison against M(eps) (itself capped). On memory the
two differ substantially, because upward snapping pushes `peak` above the
original allocation on a large minority of VMs.

With `--intervals` (a `results/bootstrap/frontier_<config>_ci.csv` from T16)
every point carries a cross of 95% bootstrap percentile intervals over VMs:
horizontal on measured risk, vertical on reclamation. The interval file must
describe the same frontier -- its point estimates are checked against
`--frontier` and the figure is refused if they differ.

Usage:
    python3 plot_frontier.py --frontier results/frontier.csv --out figures
    python3 plot_frontier.py --frontier results/frontier.csv --out figures \
        --intervals results/bootstrap/frontier_theta_off_ci.csv
"""

import argparse
import json
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
from matplotlib.lines import Line2D

# Categorical slots 1-3 of the validated default palette, assigned in fixed
# order. Baseline families are separated from each other by hue and from their
# own capped variant by fill, so identity never rests on colour alone.
C_EVT = "#2a78d6"   # slot 1, blue
C_PEAK = "#eb6834"  # slot 2, orange
C_P95 = "#1baf7a"   # slot 3, aqua
INK = "#0b0b0b"
INK_2 = "#52514e"
GRID = "#e6e5e2"
SURFACE = "#fcfcfb"

RESOURCE_LABEL = {"cpu": "CPU (vCPU-hours)", "mem": "Memory (GB-hours)"}
SCOPE_LABEL = {"all_vms": "All VMs", "non_idle": "Non-idle VMs"}


def error_cross(ax, ci, method, eps, colour):
    """95% bootstrap intervals on both axes for one frontier point."""
    if ci is None:
        return
    hit = ci[(ci.method == method)
             & ((ci.eps == eps) if eps == eps else ci.eps.isna())]
    if hit.empty:
        return
    r = hit.iloc[0]
    x, y = r.viol_frac * 100, r.recl_pct
    ax.errorbar(x, y,
                xerr=[[x - r.viol_lo * 100], [r.viol_hi * 100 - x]],
                yerr=[[y - r.recl_lo], [r.recl_hi - y]],
                fmt="none", ecolor=colour, elinewidth=1.1, capsize=2.5,
                alpha=0.75, zorder=2)


def panel(ax, sub, resource, scope, annotate_eps, _seen, series_label,
          ci=None):
    evt = sub[sub.method == "evt"].sort_values("eps")
    ax.set_facecolor(SURFACE)

    for _i, r in evt.iterrows():
        error_cross(ax, ci, "evt", r.eps, C_EVT)

    # M(eps) frontier.
    ax.plot(evt.viol_frac * 100, evt.recl_pct, "-", color=C_EVT, lw=2, zorder=3)
    ax.plot(evt.viol_frac * 100, evt.recl_pct, "o", color=C_EVT, ms=8,
            markeredgecolor=SURFACE, markeredgewidth=2, zorder=4,
            label=series_label)

    # Selective direct labels -- the ends and a couple of interior points, not
    # a number on every marker.
    for _i, r in evt.iterrows():
        if r.eps in annotate_eps:
            ax.annotate(f"{r.eps:g}", (r.viol_frac * 100, r.recl_pct),
                        textcoords="offset points", xytext=(6, -11),
                        fontsize=8, color=INK_2)

    for method, colour, marker, label in (
        ("peak", C_PEAK, "s", "peak"),
        ("p95", C_P95, "^", "p95"),
    ):
        plain = sub[sub.method == method]
        capped = sub[sub.method == method + "_capped"]
        if plain.empty:
            continue
        rp = plain.iloc[0]
        # On CPU the cap moves the baseline by almost nothing, so plotting
        # both would stack two markers and two labels on one point. Draw the
        # pair only where the cap actually separates them.
        separated = (not capped.empty
                     and abs(capped.iloc[0].recl_pct - rp.recl_pct) > 0.25)

        variants = []
        if separated:
            variants.append((rp, "none", f"{label}"))
            variants.append((capped.iloc[0], colour, f"{label} (capped)"))
        else:
            variants.append((rp, colour, label))

        for r, face, text in variants:
            error_cross(ax, ci, r.method, r.eps, colour)
            ax.plot(r.viol_frac * 100, r.recl_pct, marker, ms=11,
                    markerfacecolor=face, markeredgecolor=colour,
                    markeredgewidth=2.2, zorder=5,
                    label=text if text not in _seen else None)
            _seen.add(text)
            ax.annotate(text, (r.viol_frac * 100, r.recl_pct),
                        textcoords="offset points", xytext=(10, 4),
                        fontsize=8, color=INK_2)

    ax.set_title(f"{RESOURCE_LABEL[resource]} — {SCOPE_LABEL[scope]}",
                 fontsize=10, color=INK, loc="left")
    ax.grid(True, color=GRID, lw=0.8, zorder=0)
    ax.set_axisbelow(True)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(GRID)
    ax.tick_params(colors=INK_2, labelsize=8)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--frontier", default="results/frontier.csv")
    ap.add_argument("--out", default="figures")
    ap.add_argument("--name", default="frontier")
    ap.add_argument("--title", default=None)
    ap.add_argument("--series-label", default="M(eps), theta = 1",
                    help="legend label for the M(eps) curve. Must name the "
                         "theta in use -- a frontier drawn at a per-VM "
                         "estimated theta is not the same curve as one at "
                         "theta = 1, and the two are routinely compared.")
    ap.add_argument("--intervals", default=None,
                    help="T16 frontier_<config>_ci.csv; draws 95%% bootstrap "
                         "intervals on every point. Its run_config.json "
                         "(same directory) supplies the seed and resample "
                         "count printed on the figure.")
    ap.add_argument("--source", default="Trace: Bitbrains GWA-T-12 fastStorage, "
                                        "graciously provided by Bitbrains IT "
                                        "Services Inc.",
                    help="data-source line printed under the title. The default "
                         "is the licence acknowledgement for the real trace; "
                         "override it for any figure not drawn from that trace "
                         "(the T19 synthetic demo) so it credits what it used.")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    fr = pd.read_csv(args.frontier)
    ci, ci_note = None, ""
    if args.intervals:
        ci = pd.read_csv(args.intervals)
        key = ["scope", "resource", "method", "eps"]
        both = fr.merge(ci, on=key, suffixes=("", "_ci"), how="outer",
                        indicator=True)
        if ((both._merge != "both").any()
                or (both.recl_pct != both.recl_pct_ci).any()
                or (both.viol_frac != both.viol_frac_ci).any()):
            raise SystemExit(f"{args.intervals} does not describe the "
                             f"frontier in {args.frontier}; refusing to draw "
                             f"intervals around different points")
        with open(os.path.join(os.path.dirname(args.intervals),
                               "run_config.json")) as fh:
            cfg = json.load(fh)
        ci_note = (f"Bars: 95% bootstrap percentile intervals over VMs on "
                   f"both axes ({cfg['resamples']:,} resamples, seed "
                   f"{cfg['seed']}); fleet composition only, not the "
                   f"21/9-day split.\n")
    annotate_eps = {0.001, 0.1, 0.6}
    seen = set()

    fig, axes = plt.subplots(2, 2, figsize=(11, 8.5), facecolor=SURFACE)
    for row, resource in enumerate(("cpu", "mem")):
        for col, scope in enumerate(("all_vms", "non_idle")):
            sub = fr[(fr.resource == resource) & (fr.scope == scope)
                     & (fr.method != "as_provisioned")]
            panel(axes[row][col], sub, resource, scope, annotate_eps, seen,
                  args.series_label,
                  None if ci is None else ci[(ci.resource == resource)
                                             & (ci.scope == scope)])

    for ax in axes[1]:
        ax.set_xlabel("% of VMs with at least one violation (measured)",
                      fontsize=9, color=INK_2)
    for ax in (axes[0][0], axes[1][0]):
        ax.set_ylabel("% of allocated capacity reclaimed", fontsize=9, color=INK_2)

    # Built explicitly rather than harvested from the axes: a panel where the
    # cap changes nothing draws a single marker, so a harvested legend would
    # show two identical swatches for "peak" and "peak (capped)".
    handles = [
        Line2D([], [], color=C_EVT, lw=2, marker="o", ms=8,
               markeredgecolor=SURFACE, markeredgewidth=2,
               label=args.series_label),
        Line2D([], [], color=C_PEAK, lw=0, marker="s", ms=11,
               markerfacecolor=C_PEAK, markeredgecolor=C_PEAK,
               markeredgewidth=2.2, label="peak"),
        Line2D([], [], color=C_P95, lw=0, marker="^", ms=11,
               markerfacecolor=C_P95, markeredgecolor=C_P95,
               markeredgewidth=2.2, label="p95"),
        Line2D([], [], color=INK_2, lw=0, marker="s", ms=11,
               markerfacecolor="none", markeredgecolor=INK_2,
               markeredgewidth=2.2,
               label="hollow = as published (uncapped), where it differs"),
    ]
    fig.legend(handles=handles, loc="lower center", ncol=4, frameon=False,
               fontsize=9, bbox_to_anchor=(0.5, -0.005))

    title = args.title or ("Risk-quantified right-sizing frontier "
                           "(theta = 1, capped at original allocation)")
    fig.suptitle(title, fontsize=12, color=INK, x=0.02, y=0.985, ha="left")
    fig.text(0.02, 0.918 if ci_note else 0.935,
             "Up and to the left is better. Labels on the blue curve are eps. "
             "Filled baseline markers are capped at the original allocation, "
             "matching M(eps); hollow are uncapped.\n"
             + ci_note + args.source,
             fontsize=8.5, color=INK_2, ha="left")

    fig.tight_layout(rect=[0, 0.045, 1, 0.885 if ci_note else 0.905])
    path = os.path.join(args.out, f"{args.name}.png")
    fig.savefig(path, dpi=160, facecolor=SURFACE)
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
