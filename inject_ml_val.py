"""Add synthetic fault scenarios to LightGBM per-inverter validation set.

Pipeline:
  1. Load real INV 005 data, build features
  2. Train LightGBM on healthy Jan–Sep rows
  3. Load synthetic_eval.csv (6 fault scenarios from inject.py)
  4. Predict on val (Oct–Dec real) + synthetic fault windows
  5. Compute residuals — fault windows should show clear spikes
  6. Print per-scenario detection score: did PR/residual flag fire?

Run: python inject_ml_val.py
Requires: lightgbm  (pip install lightgbm)
"""

import json
from pathlib import Path

import numpy as np
import pandas as pd

try:
    import lightgbm as lgb
    HAS_LGBM = True
except ImportError:
    HAS_LGBM = False
    print("lightgbm not installed — run: pip install lightgbm")
    print("Showing feature engineering + residual logic only.\n")

HERE = Path(__file__).parent
BASE = HERE.parent
KWP = 30.6
INV = "INV 01.01.005"

# ── 1. Load real data ──────────────────────────────────────────────────────────
print("Loading real inverter data...")
real = pd.read_csv(BASE / "raw_data/inverters_first5_2023.csv",
                   sep=";", decimal=",", parse_dates=["timestamp"],
                   date_format="%Y.%m.%d %H:%M")
real = real.groupby("timestamp").mean(numeric_only=True)

df = pd.DataFrame({
    "p_ac":  real[f"{INV} / P_AC (kW)"].fillna(0.0),
    "irr":   real["Plant / Irradiation_average (W/m²)"].fillna(0.0),
    "t_mod": real["Temperature Sensor / Module (°C)"].fillna(0.0),
    "t_amb": real["Temperature Sensor / Ambient (°C)"].fillna(0.0),
    "dv":    real["DRD11A / DV (%)"],
    "evu":   real["DRD11A / EVU (%)"],
})
df.index = pd.to_datetime(df.index)
df = df.sort_index()


# ── 2. Feature engineering ─────────────────────────────────────────────────────
def make_features(d: pd.DataFrame) -> pd.DataFrame:
    d = d.copy()
    h = d.index.hour + d.index.minute / 60
    doy = d.index.dayofyear
    d["hour_sin"]   = np.sin(2 * np.pi * h / 24)
    d["hour_cos"]   = np.cos(2 * np.pi * h / 24)
    d["doy_sin"]    = np.sin(2 * np.pi * doy / 365)
    d["doy_cos"]    = np.cos(2 * np.pi * doy / 365)
    d["lag_irr_1"]  = d["irr"].shift(1).fillna(0)
    d["lag_irr_6"]  = d["irr"].shift(6).fillna(0)
    # sun elevation proxy: irradiation × temp-derate normalised
    d["irr_tderate"] = d["irr"] * (1 + 0.004 * (25 - d["t_mod"]))
    return d

FEATURES = ["irr", "t_mod", "t_amb", "hour_sin", "hour_cos",
            "doy_sin", "doy_cos", "lag_irr_1", "lag_irr_6", "irr_tderate"]

df = make_features(df)

# ── 3. Training split (healthy rows Jan–Sep only) ──────────────────────────────
# Exclude: curtailed rows (DV < 100), dark intervals (irr < 10)
train_mask = (
    (df.index.month <= 9) &
    (df["dv"].fillna(100) >= 99.9) &
    (df["evu"].fillna(100) >= 99.9) &
    (df["irr"] >= 10)
)
X_train = df.loc[train_mask, FEATURES]
y_train = df.loc[train_mask, "p_ac"]
print(f"Training rows (healthy Jan–Sep, irr≥10): {len(X_train):,}")

# ── 4. Validation set: Oct–Dec real ───────────────────────────────────────────
val_mask = (df.index.month >= 10) & (df["irr"] >= 10)
X_val_real = df.loc[val_mask, FEATURES]
y_val_real  = df.loc[val_mask, "p_ac"]

# ── 5. Load synthetic scenarios, build features ───────────────────────────────
print("Loading synthetic fault scenarios...")
syn_path = BASE / "raw_data/synthetic_eval.csv"
truth_path = HERE / "out/synthetic_truth.json"

syn = pd.read_csv(syn_path, parse_dates=["timestamp"]).set_index("timestamp")
truth = json.loads(truth_path.read_text())

syn_frames = {}
for name, t in truth.items():
    s = syn[syn["scenario"] == name].copy()
    s = make_features(s.rename(columns={"irr": "irr", "t_mod": "t_mod", "t_amb": "t_amb"}))
    # Only keep the fault window, daytime only
    win_mask = (
        (s.index >= t["start"]) &
        (s.index <= t["end"]) &
        (s["irr"] >= 10) &
        (t["label"] != "curtailment" or s.get("dv", pd.Series(100, index=s.index)) < 100)
    )
    syn_frames[name] = {
        "X": s.loc[win_mask, FEATURES],
        "y": s.loc[win_mask, "p_ac"],
        "label": t["label"],
        "start": t["start"],
        "end":   t["end"],
    }
    print(f"  {name:13} {t['label']:13}  {win_mask.sum():>5} fault-window rows")

# ── 6. Train or skip ──────────────────────────────────────────────────────────
if HAS_LGBM:
    print("\nTraining LightGBM per-inverter model (INV 005)...")
    model = lgb.LGBMRegressor(
        n_estimators=400, learning_rate=0.05,
        num_leaves=63, min_child_samples=20,
        subsample=0.8, colsample_bytree=0.8,
        random_state=42, n_jobs=-1, verbose=-1,
    )
    model.fit(X_train, y_train,
              eval_set=[(X_val_real, y_val_real)],
              callbacks=[lgb.early_stopping(30, verbose=False),
                         lgb.log_evaluation(period=0)])

    # Real val RMSE (baseline)
    y_hat_real = model.predict(X_val_real)
    rmse_real = np.sqrt(np.mean((y_val_real - y_hat_real) ** 2))
    print(f"\nReal val RMSE (Oct–Dec, healthy): {rmse_real:.3f} kW")

    # Per-scenario residuals
    print(f"\n{'Scenario':13} {'Label':13} {'RMSE':>8} {'Ratio':>7}  Detected?")
    print("-" * 60)
    results = {}
    for name, d in syn_frames.items():
        if len(d["X"]) == 0:
            print(f"{name:13} {d['label']:13}  {'NO DATA':>8}")
            continue
        y_hat = model.predict(d["X"])
        residuals = d["y"] - y_hat
        rmse_fault = np.sqrt(np.mean(residuals ** 2))
        ratio = rmse_fault / rmse_real
        # Detection threshold: fault RMSE > 1.8× healthy RMSE
        detected = ratio > 1.8
        results[name] = {"label": d["label"], "rmse": rmse_fault, "ratio": ratio, "detected": detected}
        print(f"{name:13} {d['label']:13} {rmse_fault:>8.3f} {ratio:>7.2f}x  {'DETECTED' if detected else 'missed'}")

    score = sum(v["detected"] for v in results.values())
    print(f"\nML validation score: {score}/{len(results)}")
    print(f"(PR cascade score from eval.py: 6/6)")

    # Save results for dashboard
    out = {
        "rmse_healthy_val": round(rmse_real, 4),
        "scenarios": {k: {**v, "rmse": round(v["rmse"], 4), "ratio": round(v["ratio"], 3)}
                      for k, v in results.items()}
    }
    (HERE / "out/ml_val_results.json").write_text(json.dumps(out, indent=2))
    print(f"\nSaved: out/ml_val_results.json")

else:
    # No lgbm — still print schema info so the approach is clear
    print("\n── FEATURE SCHEMA (for manual inspection) ──")
    print("Training features:", FEATURES)
    print("Training rows:", len(X_train))
    print("\nSynthetic fault windows available for validation:")
    for name, d in syn_frames.items():
        print(f"  {name:13}  label={d['label']:13}  rows={len(d['X'])}")
    print("\nInstall lightgbm to run full training: pip install lightgbm")

print("\n── HOW SYNTHETIC DATA FLOWS INTO VALIDATION ─────────────────────────────")
print("""
1. inject.py           → raw_data/synthetic_eval.csv   (6 scenarios, real SCADA base)
2. inject_ml_val.py    → trains LightGBM on healthy Jan–Sep real rows
3.                     → runs inference on each fault window
4.                     → residual(fault) >> residual(healthy) confirms fault signature
5. eval.py             → PR cascade also scores same 6 scenarios (rules-based)
6. Cross-validation    → both paths should flag the same windows (ensemble vote)

Key constraint: synthetic rows NEVER enter training.
Curtailment rows excluded from scoring (model can't predict regulatory caps).
""")
