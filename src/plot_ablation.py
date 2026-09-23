#!/usr/bin/env python3
"""
plot_ablation.py -- T12 figure: one per resource, the ablation arms overlaid.

x = % of VMs with at least one violation in the evaluation window (MEASURED)
y = % of allocated capacity reclaimed

This figure SUPPORTS `results/ablation/ablation_matched_risk.csv`; the table is
the answer to "which component causes the gain". Curves show where each arm
sits over the whole sweep rather than only at the one matched-risk point, which
is what makes it visible that some arms move along the frontier (buying
reclamation with risk) rather than moving it outwards.

Colour comes from the validated categorical palette in FIXED SLOT ORDER, one
slot per arm, never cycled -- seven arms, seven slots. Lines carry the adjacent
pairlist, which this order clears. Three of these slots sit below 3:1 contrast
on the light surface, so the relief rule applies: the accompanying table in
`ablation_matched_risk.csv` is the required table view, and the legend is
always present.

The `peak_capped` reference is drawn once, because six of the seven arms share
it exactly; `snap_off` moves the baseline as well as the method and gets its
own marker in its own hue.
"""

import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D

# Categorical slots 1-7 of the validated default palette, in fixed order.
SLOTS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100",
         "#e87ba4", "#008300", "#4a3aa7"]
INK = "#0b0b0b"
INK_2 = "#52514e"
GRID = "#e6e5e2"
SURFACE = "#fcfcfb"

RESOURCE_LABEL = {"cpu": "CPU (vCPU-hours)", "mem": "Memory (GB-hours)"}
SCOPE_LABEL = {"all_vms": "All VMs", "non_idle": "Non-idle VMs"}


def plot(frontiers, arms, resource, out_dir, name=None):
    """One figure for `resource`, two panels (all VMs / non-idle), every arm
    overlaid. `frontiers` maps arm name -> that arm's frontier DataFrame.
    """
    fig, axes = plt.subplots(1, 2, figsize=(12, 6.4), facecolor=SURFACE)

    for col, scope in enumerate(("all_vms", "non_idle")):
        ax = axes[col]
        ax.set_facecolor(SURFACE)

        for i, (arm, _desc, settings) in enumerate(arms):
            fr = frontiers[arm]
            sub = fr[(fr.resource == resource) & (fr.scope == scope)]
            evt = sub[sub.method == "evt"].sort_values("eps")
            colour = SLOTS[i % len(SLOTS)]
            ax.plot(evt.viol_frac * 100, evt.recl_pct, "-",
                    color=colour, lw=2, zorder=3)
            # 2px surface ring so overlapping markers stay separable.
            ax.plot(evt.viol_frac * 100, evt.recl_pct, "o", color=colour,
                    ms=8, markeredgecolor=SURFACE, markeredgewidth=2,
                    zorder=4, label=arm if col == 0 else None)

            # The baseline. Drawn once for the arms that share it, plus its
            # own marker for any arm that moves it.
            base = sub[sub.method == "peak_capped"]
            if base.empty:
                continue
            b = base.iloc[0]
            if arm == "reference":
                ax.plot(b.viol_frac * 100, b.recl_pct, "s", ms=13,
                        markerfacecolor="none", markeredgecolor=INK_2,
                        markeredgewidth=2.2, zorder=6)
                ax.annotate("peak_capped", (b.viol_frac * 100, b.recl_pct),
                            textcoords="offset points", xytext=(0, -22),
                            fontsize=8.5, color=INK_2, ha="center",
                            # The M(eps) arms cluster tightly around this
                            # point, so the label needs a surface plate to
                            # stay readable over them.
                            bbox=dict(facecolor=SURFACE, edgecolor="none",
                                      pad=1.5),
                            zorder=7)
            elif not settings["snap"]:
                ax.plot(b.viol_frac * 100, b.recl_pct, "s", ms=13,
                        markerfacecolor="none", markeredgecolor=colour,
                        markeredgewidth=2.2, zorder=6)
                ax.annotate("peak_capped\n(unsnapped)",
                            (b.viol_frac * 100, b.recl_pct),
                            textcoords="offset points", xytext=(0, -30),
                            fontsize=8.5, color=colour, ha="center")

        ax.set_title(f"{RESOURCE_LABEL[resource]} — {SCOPE_LABEL[scope]}",
                     fontsize=10, color=INK, loc="left")
        ax.set_xlabel("% of VMs with at least one violation (measured)",
                      fontsize=9, color=INK_2)
        ax.grid(True, color=GRID, lw=0.8, zorder=0)
        ax.set_axisbelow(True)
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
        for s in ("left", "bottom"):
            ax.spines[s].set_color(GRID)
        ax.tick_params(colors=INK_2, labelsize=8)

    axes[0].set_ylabel("% of allocated capacity reclaimed",
                       fontsize=9, color=INK_2)

    handles = [
        Line2D([], [], color=SLOTS[i % len(SLOTS)], lw=2, marker="o", ms=8,
               markeredgecolor=SURFACE, markeredgewidth=2, label=arm)
        for i, (arm, _d, _s) in enumerate(arms)
    ] + [
        Line2D([], [], color=INK_2, lw=0, marker="s", ms=11,
               markerfacecolor="none", markeredgecolor=INK_2,
               markeredgewidth=2.2, label="peak_capped baseline"),
    ]
    fig.legend(handles=handles, loc="lower center", ncol=4, frameon=False,
               fontsize=9, bbox_to_anchor=(0.5, -0.01))

    fig.suptitle(f"T12 ablation — {RESOURCE_LABEL[resource]}: "
                 "one switch flipped per arm",
                 fontsize=12, color=INK, x=0.02, y=0.975, ha="left")
    fig.text(0.02, 0.925,
             "Up and to the left is better; each curve is one eps sweep. "
             "Which component causes the gain is answered by the matched-risk "
             "table\n(results/ablation/ablation_matched_risk.csv), not by the "
             "curves — an arm that slides along the frontier bought "
             "reclamation with risk.\n"
             "Trace: Bitbrains GWA-T-12 fastStorage, graciously provided by "
             "Bitbrains IT Services Inc.",
             fontsize=8.5, color=INK_2, ha="left", va="top")

    fig.tight_layout(rect=[0, 0.10, 1, 0.84])
    path = os.path.join(out_dir, name or f"ablation_{resource}.png")
    fig.savefig(path, dpi=160, facecolor=SURFACE)
    plt.close(fig)
    return path


def main():
    """Re-draw the figures from an ablation run's saved frontiers, without
    re-running the sweep. The arm order and settings come from the run's own
    `ablation_config.json`, so the figure can never assign colours in a
    different order than the run that produced the data.
    """
    import argparse
    import json

    import pandas as pd

    ap = argparse.ArgumentParser()
    ap.add_argument("--ablation", default="results/ablation",
                    help="directory holding frontier_<arm>.csv and "
                         "ablation_config.json")
    ap.add_argument("--out", default="figures")
    args = ap.parse_args()

    with open(os.path.join(args.ablation, "ablation_config.json")) as fh:
        cfg = json.load(fh)
    arms = [(name, spec["switch"], spec) for name, spec in cfg["arms"].items()]

    frontiers = {
        name: pd.read_csv(os.path.join(args.ablation, f"frontier_{name}.csv"))
        for name, _d, _s in arms
    }
    os.makedirs(args.out, exist_ok=True)
    for resource in ("cpu", "mem"):
        print(f"wrote {plot(frontiers, arms, resource, args.out)}")


if __name__ == "__main__":
    main()
