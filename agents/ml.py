"""Per-inverter LightGBM expected-power model.

Replaces physics_expected() in impact.price() with a learned baseline.
Train on healthy rows (no curtailment, irr >= 10), validate on Oct-Dec.
Predict on any frame (including synthetic fault scenarios).

Requires: lightgbm  (pip install lightgbm)
Falls back to physics_expected() if lgbm not installed.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

try:
    import lightgbm as lgb
    HAS_LGBM = True
except ImportError:
    HAS_LGBM = False

FEATURES = [
    "irr", "t_mod", "t_amb",
    "hour_sin", "hour_cos",
    "doy_sin",  "doy_cos",
    "lag_irr_1", "lag_irr_6",
    "irr_tderate",
]


def _featurise(frame: pd.DataFrame) -> pd.DataFrame:
    d = frame.copy()
    h   = d.index.hour + d.index.minute / 60
    doy = d.index.dayofyear
    d["hour_sin"]    = np.sin(2 * np.pi * h   / 24)
    d["hour_cos"]    = np.cos(2 * np.pi * h   / 24)
    d["doy_sin"]     = np.sin(2 * np.pi * doy / 365)
    d["doy_cos"]     = np.cos(2 * np.pi * doy / 365)
    d["lag_irr_1"]   = d["irr"].shift(1).fillna(0)
    d["lag_irr_6"]   = d["irr"].shift(6).fillna(0)
    d["irr_tderate"] = d["irr"] * (1 + 0.004 * (25 - d["t_mod"].fillna(25)))
    return d


def train(frame: pd.DataFrame, kwp: float) -> object | None:
    """Train LightGBM on healthy Jan-Sep rows of `frame`.

    Returns fitted model or None if lgbm unavailable.
    frame must have: p_ac, irr, t_mod, t_amb, dv, evu columns.
    """
    if not HAS_LGBM:
        return None

    d = _featurise(frame)

    # Healthy training mask: Jan-Sep, no curtailment, daytime
    train_mask = (
        (d.index.month <= 9)
        & (d["dv"].fillna(100)  >= 99.9)
        & (d["evu"].fillna(100) >= 99.9)
        & (d["irr"] >= 10)
        & (d["p_ac"] >= kwp * 0.01)   # exclude stuck-at-zero nights
    )
    val_mask = (
        (d.index.month >= 10)
        & (d["irr"] >= 10)
    )

    X_tr, y_tr = d.loc[train_mask, FEATURES], d.loc[train_mask, "p_ac"]
    X_va, y_va = d.loc[val_mask,   FEATURES], d.loc[val_mask,   "p_ac"]

    model = lgb.LGBMRegressor(
        n_estimators=400, learning_rate=0.05,
        num_leaves=63, min_child_samples=20,
        subsample=0.8, colsample_bytree=0.8,
        random_state=42, n_jobs=-1, verbose=-1,
    )
    model.fit(
        X_tr, y_tr,
        eval_set=[(X_va, y_va)],
        callbacks=[
            lgb.early_stopping(30, verbose=False),
            lgb.log_evaluation(period=0),
        ],
    )
    return model


def predict_expected(frame: pd.DataFrame, model: object, kwp: float) -> tuple[pd.Series, float]:
    """Return (expected_kw_series, plant_factor) matching impact.physics_expected() signature.

    If model is None (lgbm missing), falls back to physics baseline.
    expected is clipped to [0, kwp] and set to 0 on dark intervals.
    """
    if model is None:
        # physics fallback
        raw = kwp * (frame["irr"] / 1000) * (1 - 0.004 * (frame["t_mod"].fillna(25) - 25))
        healthy = (frame["irr"] > 300) & (frame["p_ac"] > kwp * 0.02)
        pf = float((frame["p_ac"][healthy] / raw[healthy]).median())
        return (raw * pf).clip(lower=0), pf

    d = _featurise(frame)
    X = d[FEATURES].fillna(0)
    pred = pd.Series(model.predict(X), index=frame.index, dtype=float)
    pred = pred.clip(lower=0, upper=kwp * 1.05)
    pred[frame["irr"] < 5] = 0.0          # hard-zero at night
    # plant_factor proxy: ratio of median healthy prediction to raw kWp
    pf = float(pred[frame["irr"] > 300].median() / (kwp * 0.8)) if (frame["irr"] > 300).any() else 1.0
    return pred, round(pf, 3)
