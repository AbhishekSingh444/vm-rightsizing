"""Bundle the committed results, evidence and a raw-trace excerpt into website/data.js.

Read-only with respect to the rest of the repo: it reads results/, evidence/,
docs/, figures/, data/demo_input/ and git history, and writes only under
website/. Standard library only.

    python website/build_data.py
"""
import csv
import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(__file__).resolve().parent
RES = ROOT / "results"
EVI = ROOT / "evidence"


def rows(path, delimiter=","):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f, delimiter=delimiter))


def text(path):
    return Path(path).read_text(encoding="utf-8", errors="replace")


def num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return v
    return int(x) if x.is_integer() and "." not in str(v) else round(x, 5)


def table(path, keep=None, where=None, delimiter=","):
    out = []
    for r in rows(path, delimiter):
        if where and not where(r):
            continue
        out.append({k: num(v) for k, v in r.items() if keep is None or k in keep})
    return out


def raw_vm(path, step=3):
    """Raw header + first lines verbatim, then a peak-preserving series (max per `step` samples)."""
    lines = text(path).splitlines()
    head = lines[:21]
    recs = []
    for ln in lines[1:]:
        p = [c.strip() for c in ln.split(";")]
        if len(p) < 7:
            continue
        # Timestamp [ms] holds SECONDS; memory columns are KB.
        recs.append((float(p[0]), float(p[3]), float(p[2]), float(p[6]) / 1048576, float(p[5]) / 1048576, int(float(p[1]))))
    recs.sort()
    t0 = recs[0][0]
    series = []
    for i in range(0, len(recs), step):
        chunk = recs[i:i + step]
        series.append([
            round((chunk[0][0] - t0) / 86400, 4),
            round(max(c[1] for c in chunk) / chunk[0][2] * chunk[0][5], 3),  # CPU demand in cores
            round(max(c[3] for c in chunk), 3),                                # memory GB
        ])
    return {
        "head": head,
        "samples": len(recs),
        "cores": recs[0][5],
        "mem_cap_gb": round(recs[0][4], 2),
        "span_days": round((recs[-1][0] - t0) / 86400, 2),
        "series": series,
    }


def git_log():
    try:
        out = subprocess.run(
            ["git", "log", "--date=short", "--format=%h\t%ad\t%an\t%s"],
            cwd=ROOT, capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return []
    return [dict(zip(("hash", "date", "author", "subject"), ln.split("\t", 3)))
            for ln in out.splitlines() if ln]


def line_count(p):
    return sum(1 for _ in open(p, encoding="utf-8", errors="replace"))


def main():
    d = {}
    d["gap"] = table(ROOT / "docs/gap_table.tsv", delimiter="\t")
    d["hypothesesDoc"] = text(ROOT / "docs/HYPOTHESES.txt")
    d["runConfig"] = json.loads(text(RES / "run_config.json"))
    d["sweepConfig"] = json.loads(text(RES / "sweep_config.json"))

    d["baseline"] = table(RES / "baseline_summary.csv")
    d["frontierCI"] = table(RES / "bootstrap/frontier_reference_ci.csv")
    d["frontier"] = table(RES / "theta_intervals/frontier.csv")
    d["gaps"] = table(RES / "bootstrap/matched_risk_gaps_ci.csv", where=lambda r: r["config"] == "reference")
    d["dominance"] = table(RES / "theta_intervals/dominance.csv")

    d["ablationShifts"] = table(RES / "bootstrap/ablation_shifts_ci.csv")
    d["ablationMatched"] = table(RES / "ablation/ablation_matched_risk.csv")
    d["ablationFrontier"] = {}
    for arm in ("reference", "theta_off", "theta_runs", "snap_off", "cap_off", "u_q90", "u_q98"):
        d["ablationFrontier"][arm] = table(
            RES / f"ablation/frontier_{arm}.csv",
            keep={"scope", "resource", "method", "eps", "recl_pct", "viol_frac"},
            where=lambda r: r["method"] in ("evt", "peak_capped"))

    d["h2"] = table(RES / "hypotheses/h2_calibration.csv", where=lambda r: r["frontier"] == "theta_intervals")
    d["h2summary"] = table(RES / "hypotheses/h2_summary.csv")
    d["h3"] = table(RES / "hypotheses/h3_zero_risk.csv")

    d["splitPlan"] = table(RES / "multisplit/split_plan.csv")
    d["splitGaps"] = table(RES / "multisplit/matched_risk_gaps_summary.csv")
    d["splitEce"] = table(RES / "multisplit/h2_ece_summary.csv")

    d["rndFrontier"] = table(RES / "rnd/compare_frontier.csv")
    d["rndH2"] = table(RES / "rnd/compare_h2.csv")
    d["rndH3"] = table(RES / "rnd/compare_h3.csv")

    d["confusion"] = table(RES / "confusion_table.csv")
    d["waves"] = []
    for w in range(1, 6):
        for r in table(RES / f"wave{w}_outcomes.csv",
                       keep={"vm_id", "pred_mem_violation", "obs_oomkilled", "margin_band",
                             "peak_demand_mib", "budget_mib", "limit_mib", "peak_cgroup_mib",
                             "samples_logged", "samples_total"}):
            r["wave"] = w
            d["waves"].append(r)
    d["cpuRescore"] = table(RES / "wave1_cpu_rescore.csv")

    # processed input: per-VM preflight characterisation of all 1,250 files
    d["dataset"] = table(RES / "dataset_report.csv")
    # features: per-VM tail-fit parameters (observation window only)
    feats = {}
    for r in rows(RES / "theta_intervals/theta_per_vm.csv"):
        f = feats.setdefault(r["vm_id"], {"vm_id": int(r["vm_id"]), "idle": r["idle"] == "True"})
        k = r["resource"]
        f[f"{k}_theta"] = num(r["theta_used"])
        f[f"{k}_est"] = r["status"] == "ok"
        f[f"{k}_u"] = num(r["u"])
        f[f"{k}_nexc"] = num(r["n_exceed"])
        f[f"{k}_trials"] = num(r["effective_trials"])
    for r in rows(RES / "theta_intervals/gpd_shapes.csv"):
        f = feats.get(r["vm_id"])
        if f is not None:
            f[f"{r['resource']}_xi"] = num(r["shape"])
    d["features"] = sorted(feats.values(), key=lambda f: f["vm_id"])
    # output: per-VM allocations and violations for every baseline
    d["perVm"] = table(RES / "per_vm_results.csv")

    demo = ROOT / "data/demo_input"
    d["rawVms"] = {}
    if demo.is_dir():
        for p in sorted(demo.glob("*.csv")):
            d["rawVms"][p.stem] = raw_vm(p)
    vm_dir = ROOT / "demo_out/vm"
    d["demoPolicies"] = table(vm_dir / "demo_vm_per_policy.csv") if (vm_dir / "demo_vm_per_policy.csv").exists() else []
    d["demoReport"] = text(vm_dir / "demo_vm_report.txt") if (vm_dir / "demo_vm_report.txt").exists() else ""

    ev = {}
    for name in ("review1_screens/01_get_nodes.txt", "review1_screens/02_get_pods_all.txt",
                 "review1_screens/03_describe_rs-p95-143.txt", "review1_screens/04_limits_custom_columns.txt",
                 "02_version.txt", "02_preflight.txt", "03_image.txt", "05_oomkills.tsv", "99_summary.txt",
                 "19_demo_kind.txt", "T8_confusion.md", "REVIEW1_ARTIFACTS.md"):
        p = EVI / name
        if p.exists():
            ev[name] = text(p)
    for w in range(1, 6):
        for name in ("01_nodes.txt", "03_pods.txt", "99_summary.txt"):
            p = EVI / f"wave{w}/{name}"
            if p.exists():
                ev[f"wave{w}/{name}"] = text(p)
    d["evidence"] = ev

    d["git"] = git_log()
    d["sources"] = [{"path": str(p.relative_to(ROOT)).replace("\\", "/"), "lines": line_count(p)}
                    for pat in ("src/*.py", "testbed/*.py", "tests/*.py") for p in sorted(ROOT.glob(pat))]
    d["captions"] = text(ROOT / "figures/CAPTIONS.md")

    (OUT / "data.js").write_text("window.DATA = " + json.dumps(d, separators=(",", ":")) + ";\n", encoding="utf-8")

    fig = OUT / "figures"
    fig.mkdir(exist_ok=True)
    for p in (ROOT / "figures").glob("*.png"):
        shutil.copy2(p, fig / p.name)
    print(f"wrote {OUT / 'data.js'} ({(OUT / 'data.js').stat().st_size // 1024} KB) and {len(list(fig.glob('*.png')))} figures")


if __name__ == "__main__":
    main()
