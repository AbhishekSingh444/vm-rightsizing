#!/usr/bin/env python3
"""
demo_guard.py -- refuse a demo trace whose timestamps are not in seconds.

For `make demo` only; the research pipeline does not call this. A file handed
over on the day goes through the guard before anything else, so a unit
mistake ends in a clear rejection instead of a frontier computed on garbage.

The column is named "Timestamp [ms]" but holds seconds (CLAUDE.md, dataset
traps), and the loader reads it as seconds without checking. A file that
really is in milliseconds then shows a median gap ~1000x too large -- 300,000
"seconds" (3.5 days) for 5-minute sampling -- and the 21+9 day split keeps
about 9 rows. Nothing downstream errors. The guard rejects any file whose
median gap implies fewer than one sample per day, which catches milliseconds
at any real sampling interval of 86.4 s or slower.

Exits 1 if any file is rejected, so `make demo` stops before the pipeline.

Usage:
    python3 demo_guard.py --traces demo_in/
"""

import argparse
import glob
import os
import sys

import numpy as np
import pandas as pd

from baselines import COL_TS

MAX_MEDIAN_GAP_S = 86400.0


def check_file(path):
    """Return a rejection reason for one CSV, or None if it passes.

    Only the timestamp unit is judged here. Unreadable files, missing
    columns and short spans are left to load_vm and split_obs_eval, which
    already report them.
    """
    try:
        df = pd.read_csv(path, sep=r";\s*", engine="python")
    except Exception:
        return None
    df.columns = [c.strip().strip('"').strip() for c in df.columns]
    if COL_TS not in df.columns:
        return None
    ts = pd.to_numeric(df[COL_TS], errors="coerce").dropna().to_numpy()
    if len(ts) < 2:
        return None
    gap = float(np.median(np.diff(np.sort(ts))))
    if gap > MAX_MEDIAN_GAP_S:
        return (f"timestamps look like milliseconds, not seconds "
                f"(median gap {gap:,.0f} s = {gap / 86400.0:.1f} days)")
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--traces", required=True,
                    help="directory of per-VM CSVs, as passed to baselines.py")
    args = ap.parse_args()

    paths = sorted(glob.glob(os.path.join(args.traces, "*.csv")))
    if not paths:
        raise SystemExit(f"no CSVs found in {args.traces}")

    rejected = 0
    for path in paths:
        reason = check_file(path)
        if reason:
            rejected += 1
            print(f"  REJECT {os.path.basename(path)}: {reason}", file=sys.stderr)
    if rejected:
        raise SystemExit(f"demo_guard: {rejected} of {len(paths)} file(s) rejected -- demo stopped")
    print(f"demo_guard: {len(paths)} file(s) passed the timestamp-unit check")


if __name__ == "__main__":
    main()
