"""ML API client — calls the deployed LightGBM PR model on Render.

Endpoint: https://plant-a-pr-api.onrender.com
Trained:  Plant A, 2017 (Jan–Sep train, Oct–Dec val)
Output:   inverter_performance_ratio + expected_kw per timestamp

Public interface (matches impact.physics_expected signature):
    health_check()                          -> dict
    predict_expected_series(frame, ...)     -> (pd.Series[expected_kw], plant_factor)

Fallback: if API unreachable, silently falls back to physics_expected().
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import pandas as pd
import requests

BASE_URL  = "https://plant-a-pr-api.onrender.com"
TIMEOUT_S = 90      # Render free tier cold-start can take 30–60 s
BATCH_SIZE = 4000   # API max is 5000; stay below to avoid edge cases
MIN_IRR    = 5.0    # W/m² — below this set expected=0 (night/near-dark)

log = logging.getLogger(__name__)


# ── per-inverter static metadata (from System_Overview.xlsx) ──────────────────
# INV 001–003, 005: 30.6 kWp, 5 strings, 120 modules, Module Type 1
# INV 004:          24.48 kWp, 4 strings,  96 modules, Module Type 1
INV_META: dict[str, dict] = {
    "INV 01.01.001": {"strings": 5, "modules": 120, "module_type": "Module Type 1"},
    "INV 01.01.002": {"strings": 5, "modules": 120, "module_type": "Module Type 1"},
    "INV 01.01.003": {"strings": 5, "modules": 120, "module_type": "Module Type 1"},
    "INV 01.01.004": {"strings": 4, "modules":  96, "module_type": "Module Type 1"},
    "INV 01.01.005": {"strings": 5, "modules": 120, "module_type": "Module Type 1"},
}
_DEFAULT_META = {"strings": 5, "modules": 120, "module_type": "Module Type 1"}


# ── health check ──────────────────────────────────────────────────────────────
def health_check() -> dict:
    """GET /health — returns status dict or error dict."""
    try:
        r = requests.get(f"{BASE_URL}/health", timeout=TIMEOUT_S)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        return {"status": "error", "detail": str(exc)}


def model_metrics() -> dict:
    """GET /metrics — returns model validation metrics."""
    try:
        r = requests.get(f"{BASE_URL}/metrics", timeout=TIMEOUT_S)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        return {"error": str(exc)}


# ── batch prediction ───────────────────────────────────────────────────────────
def _build_records(frame: pd.DataFrame, inv_id: str, kwp: float,
                   strings: int, modules: int, module_type: str) -> list[dict]:
    """Convert monitoring DataFrame rows → API records (daytime only)."""
    mask = frame["irr"] > MIN_IRR
    sub  = frame[mask]
    records = []
    for ts, row in sub.iterrows():
        records.append({
            "irradiance_w_m2":   float(row["irr"]),
            "module_temp_c":     float(row["t_mod"]) if pd.notna(row.get("t_mod")) else 25.0,
            "ambient_temp_c":    float(row["t_amb"]) if pd.notna(row.get("t_amb")) else 20.0,
            "hour":              float(ts.hour + ts.minute / 60),
            "day_of_year":       int(ts.dayofyear),
            "month":             int(ts.month),
            "inverter_id":       inv_id,
            "inverter_pdc_kwp":  kwp,
            "strings":           strings,
            "modules":           modules,
            "module_type":       module_type,
            "timestamp":         str(ts),
        })
    return records


def _inv_slug(inv_id: str) -> str:
    """'INV 01.01.001' → 'INV_01_01_001' (API slug format)."""
    return inv_id.replace(" ", "_").replace(".", "_")


def _call_slug(record: dict, slug: str) -> dict | None:
    """POST /predict/{slug} with one InverterFeatures record."""
    try:
        r = requests.post(f"{BASE_URL}/predict/{slug}", json=record, timeout=TIMEOUT_S)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        log.debug(f"ML slug call failed ({slug}): {exc}")
        return None


def _daily_noon_records(
    frame: pd.DataFrame, inv_id: str, kwp: float,
    strings: int, modules: int, module_type: str,
) -> list[tuple[pd.Timestamp, dict]]:
    """One record per calendar day at the peak-irradiance moment (noon proxy).
    Used for ML daily-PR predictions — keeps total API calls to ≤365 per inverter.
    """
    daytime = frame[frame["irr"] > MIN_IRR]
    if daytime.empty:
        return []
    # peak irradiance row per day via groupby
    daily_peak = (
        daytime["irr"]
        .groupby(daytime.index.date)
        .idxmax()
    )
    out = []
    for day, ts in daily_peak.items():
        row = frame.loc[ts]
        out.append((ts, {
            "irradiance_w_m2":   float(row["irr"]),
            "module_temp_c":     float(row["t_mod"]) if pd.notna(row.get("t_mod")) else 25.0,
            "ambient_temp_c":    float(row["t_amb"]) if pd.notna(row.get("t_amb")) else 20.0,
            "hour":              float(ts.hour + ts.minute / 60),
            "day_of_year":       int(ts.dayofyear),
            "month":             int(ts.month),
            "inverter_id":       inv_id,
            "inverter_pdc_kwp":  kwp,
            "strings":           strings,
            "modules":           modules,
            "module_type":       module_type,
            "timestamp":         str(ts),
        }))
    return out


def _raw_predictions(
    frame: pd.DataFrame, inv_id: str, kwp: float,
    strings: int, modules: int, module_type: str,
) -> tuple[list[dict], list[pd.Timestamp]] | tuple[None, None]:
    """Daily ML predictions via per-inverter slug endpoint.

    Uses one API call per day (peak-irradiance moment) rather than per-5min.
    Returns (predictions_list, timestamps) indexed to those daily peak timestamps.
    Falls back to (None, None) if all calls fail.
    """
    slug    = _inv_slug(inv_id)
    records = _daily_noon_records(frame, inv_id, kwp, strings, modules, module_type)
    if not records:
        return None, None

    preds, timestamps = [], []
    failures = 0
    for ts, rec in records:
        result = _call_slug(rec, slug)
        if result is not None:
            preds.append(result)
            timestamps.append(ts)
        else:
            failures += 1

    if not preds or failures > len(records) * 0.5:
        log.warning(f"ML API failed for {inv_id} ({failures}/{len(records)} calls failed)")
        return None, None

    log.info(f"ML API [{inv_id}] slug predictions: {len(preds)}/{len(records)} days OK")
    return preds, timestamps


def predict_for_triage(
    frame: pd.DataFrame,
    inv_id: str,
    kwp: float,
    strings:     Optional[int] = None,
    modules:     Optional[int] = None,
    module_type: Optional[str] = None,
) -> tuple[pd.Series, pd.Series, float]:
    """One API call → returns THREE things for the pipeline:

        expected_kw     pd.Series  — ML-predicted expected power (kW), feeds impact.price()
        ml_pr_daily     pd.Series  — ML-predicted daily PR, feeds triage Stage 1 as reference
        plant_factor    float      — median predicted PR at irr > 300 W/m²

    This is the CORRECT architecture:
        triage Stage 1: deviation = ml_pr_daily − actual_pr  (not fleet_median)
        impact.price:   lost_kwh  = (expected_kw − p_ac) × 5min

    Falls back to physics on API error (ml_pr_daily = physics-based daily PR).
    """
    meta        = INV_META.get(inv_id, _DEFAULT_META)
    strings     = strings     or meta["strings"]
    modules     = modules     or meta["modules"]
    module_type = module_type or meta["module_type"]

    expected    = pd.Series(0.0, index=frame.index)
    daytime_mask = frame["irr"] > MIN_IRR

    preds, timestamps = _raw_predictions(frame, inv_id, kwp, strings, modules, module_type)

    # ── expected_kw always from physics (per-5min resolution needed for pricing) ──
    exp_kw, pf_phys = _physics_fallback(frame, kwp)

    if preds is None:
        # full physics fallback
        pr_daily = _physics_pr_daily(frame, kwp)
        return exp_kw, pr_daily, pf_phys

    # ── ML daily PR — one prediction per day at peak irradiance ──────────────
    ml_pr_ts = pd.Series(
        [p.get("inverter_performance_ratio", float("nan")) for p in preds],
        index=pd.DatetimeIndex(timestamps), dtype=float,
    )
    ml_pr_daily = ml_pr_ts.resample("D").first()  # one value per day

    # plant_factor from ML PR median
    pf = round(float(ml_pr_daily.dropna().median()), 3) if len(ml_pr_daily.dropna()) else pf_phys

    return exp_kw, ml_pr_daily, pf


# ── backwards-compat wrapper (used by existing callers) ───────────────────────
def predict_expected_series(
    frame: pd.DataFrame, inv_id: str, kwp: float,
    strings: Optional[int] = None, modules: Optional[int] = None,
    module_type: Optional[str] = None,
) -> tuple[pd.Series, float]:
    """Legacy 2-tuple interface — returns (expected_kw, plant_factor).
    Prefer predict_for_triage() in new code."""
    exp_kw, _, pf = predict_for_triage(frame, inv_id, kwp, strings, modules, module_type)
    return exp_kw, pf


# ── physics fallbacks ─────────────────────────────────────────────────────────
def _physics_fallback(frame: pd.DataFrame, kwp: float) -> tuple[pd.Series, float]:
    raw     = kwp * (frame["irr"] / 1000) * (1 - 0.004 * (frame["t_mod"].fillna(25) - 25))
    healthy = (frame["irr"] > 300) & (frame["p_ac"] > kwp * 0.02)
    pf      = float((frame["p_ac"][healthy] / raw[healthy]).median()) if healthy.any() else 1.0
    return (raw * pf).clip(lower=0), round(pf, 3)


def _physics_pr_daily(frame: pd.DataFrame, kwp: float) -> pd.Series:
    """Physics-based daily PR — used when API is down."""
    from agents import triage as _triage
    return _triage.daily_pr(frame, kwp)
