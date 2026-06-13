"""Fault-test runner — point-by-point PR prediction against the live ML API.

Reads the cleaned fault-injection test set (predictions stripped), pushes every
row ONE RECORD PER REQUEST to POST /predict, collects predicted_pr + expected_kw,
recomputes pr_gap / pr_ratio, and scores per scenario.

  POST /predict  body = single InverterFeatures  -> PredictionResponse
  (the deployed /predict/batch falls through to /predict/{slug}; no real batch.)

Usage:
    python run_fault_test.py [--workers N] [--limit N]

Each request is a single point. --workers only controls how many of those
single-point requests are in flight at once (default 16). --workers 1 = strictly
sequential one-at-a-time.
"""

from __future__ import annotations

import argparse
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd
import requests

BASE_URL = "https://plant-a-pr-api.onrender.com"
TIMEOUT_S = 90
INPUT = "/Users/vasuchukka/Downloads/fault_test_dataset2_input.csv"
OUTPUT = "out/fault_test_predictions.csv"

X_COLS = [
    "irradiance_w_m2", "module_temp_c", "ambient_temp_c", "hour", "day_of_year",
    "month", "inverter_id", "inverter_pdc_kwp", "strings", "modules", "module_type",
]


def _record(row: pd.Series) -> dict:
    """One DataFrame row -> single InverterFeatures payload."""
    d = {k: row[k] for k in X_COLS}
    for k in ("day_of_year", "month", "strings", "modules"):
        d[k] = int(d[k])
    for k in ("irradiance_w_m2", "module_temp_c", "ambient_temp_c", "hour", "inverter_pdc_kwp"):
        d[k] = float(d[k])
    return d


def _predict_one(idx: int, payload: dict, session: requests.Session) -> tuple[int, dict | None]:
    """POST /predict with a single record. Returns (row_index, response | None)."""
    try:
        r = session.post(f"{BASE_URL}/predict", json=payload, timeout=TIMEOUT_S)
        r.raise_for_status()
        return idx, r.json()
    except Exception as exc:  # noqa: BLE001
        print(f"  row {idx} failed: {exc}")
        return idx, None


def run(workers: int, limit: int | None) -> None:
    df = pd.read_csv(INPUT)
    if limit:
        df = df.head(limit).copy()
    n = len(df)
    print(f"loaded {n} rows from {INPUT}")
    print(f"pushing point-by-point to {BASE_URL}/predict  (workers={workers})\n")

    pred_pr = np.full(n, np.nan)
    exp_kw = np.full(n, np.nan)
    model_used = [None] * n

    session = requests.Session()
    t0 = time.time()
    done = 0

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(_predict_one, i, _record(row), session): i
            for i, (_, row) in enumerate(df.iterrows())
        }
        for fut in as_completed(futures):
            i, res = fut.result()
            if res is not None:
                pred_pr[i] = res["inverter_performance_ratio"]
                exp_kw[i] = res["expected_kw"]
                model_used[i] = res["model_used"]
            done += 1
            if done % 500 == 0 or done == n:
                print(f"  {done}/{n}  ({time.time() - t0:.1f}s)")

    df["predicted_pr"] = pred_pr
    df["expected_kw_pred"] = exp_kw
    df["model_used"] = model_used
    df["pr_gap"] = df["predicted_pr"] - df["inverter_performance_ratio"]
    df["pr_ratio"] = df["inverter_performance_ratio"] / df["predicted_pr"]

    import os
    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    df.to_csv(OUTPUT, index=False)
    fails = int(np.isnan(pred_pr).sum())
    print(f"\nsaved -> {OUTPUT}  ({n - fails}/{n} predicted, {fails} failed)")

    _summary(df.dropna(subset=["predicted_pr"]))


def _summary(df: pd.DataFrame) -> None:
    print("\n=== PR-gap by scenario  (gap = predicted - actual PR) ===")
    print(f"{'scenario':16}{'n':>6}{'mean_gap':>10}{'mean|gap|':>10}{'actual_PR':>10}{'pred_PR':>10}")
    for sc, g in df.groupby("scenario"):
        print(f"{sc:16}{len(g):>6}{g.pr_gap.mean():>10.3f}"
              f"{g.pr_gap.abs().mean():>10.3f}"
              f"{g.inverter_performance_ratio.mean():>10.3f}{g.predicted_pr.mean():>10.3f}")

    # Detection check: a fault should show a large positive gap (model expects more PR than observed).
    print("\n=== detection separation (model predicts healthy; fault depresses actual PR) ===")
    healthy = df[df.is_fault == 0].pr_gap
    faulty = df[df.is_fault == 1].pr_gap
    print(f"  healthy gap: mean {healthy.mean():.3f}  p95 {healthy.quantile(.95):.3f}")
    print(f"  faulty  gap: mean {faulty.mean():.3f}  p50 {faulty.quantile(.50):.3f}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    run(args.workers, args.limit)
