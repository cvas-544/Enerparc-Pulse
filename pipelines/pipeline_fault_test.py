"""Fault-test pipeline — runs fault_test_dataset.csv through the full agentic system.

Dataset schema (clean):
  11 X_columns                 → ML API /predict/batch  → predicted_pr (fresh)
  inverter_performance_ratio   → actual PR (SCADA formula) → compared vs predicted_pr
  actual_kw                    → p_ac in frame  → EUR pricing
  fault_type / is_fault        → ground truth for scoring only

Flow:
  1. Call ML API batch  → predicted_pr + expected_kw per row
  2. pr_gap = predicted_pr − inverter_performance_ratio  → Stage 1 flag
  3. Reconstruct timestamps  (2017 + day_of_year + fractional hour)
  4. Mock missing SCADA signals (i_dc, u_dc, dv, evu) from fault_type signature
  5. Build per-inverter time-series frames  → triage.run()
  6. impact + orchestrator + drafter + RAG
  7. Score vs ground truth  → confusion matrix

Run: python pipeline_fault_test.py
Out: out/fault_test_incidents.json  out/fault_test_scores.json
"""

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).parent          # pipelines/
BASE = HERE.parent.parent             # dataset dir (raw_data/, "2. Additional Data/")
OUT  = HERE.parent / "out"            # repo out/
OUT.mkdir(exist_ok=True)

sys.path.insert(0, str(HERE.parent))  # repo root, so `agents` is importable
from agents import drafter, impact, ml_api, orchestrator, rag_compliance, triage

FIT          = 0.116   # EUR/kWh
U_DC_NOMINAL = 580.0   # V — typical MPPT voltage
ETA_NOMINAL  = 0.97    # inverter conversion efficiency


# ── 1. Load dataset ───────────────────────────────────────────────────────────
print("Loading fault_test_dataset.csv...")
df = pd.read_csv(BASE / "raw_data/fault_test_dataset.csv")
print(f"  {len(df)} rows · {df.inverter_id.nunique()} inverters")
print(f"  fault counts: {df[df.is_fault==1].fault_type.value_counts().to_dict()}")
print(f"  columns: {df.columns.tolist()}")


# ── 2. Call ML API — predict_pr fresh for every row ───────────────────────────
print("\nCalling ML API /predict/batch...")
h = ml_api.health_check()
print(f"  health: {h.get('status')} · model_loaded={h.get('model_loaded')}")

X_cols = ["irradiance_w_m2","module_temp_c","ambient_temp_c","hour","day_of_year",
          "month","inverter_id","inverter_pdc_kwp","strings","modules","module_type"]
records = df[X_cols].to_dict(orient="records")

preds, BATCH = [], 4000
n = (len(records) + BATCH - 1) // BATCH
for i in range(n):
    batch = records[i*BATCH:(i+1)*BATCH]
    preds.extend(ml_api._call_batch(batch))
    print(f"  batch {i+1}/{n} → {len(preds)} predictions total")

df["predicted_pr"] = [p["inverter_performance_ratio"] for p in preds]
df["expected_kw"]  = [p["expected_kw"]               for p in preds]

# ── pr_gap = predicted − actual (SCADA formula) ───────────────────────────────
df["pr_gap"] = df["predicted_pr"] - df["inverter_performance_ratio"]
print(f"\n  pr_gap — fault rows:   mean={df[df.is_fault==1].pr_gap.mean():.3f}")
print(f"  pr_gap — healthy rows: mean={df[df.is_fault==0].pr_gap.mean():.3f}")


# ── 3. Reconstruct timestamps (2017 validation window: Oct–Dec) ───────────────
def to_ts(row) -> pd.Timestamp:
    base = pd.Timestamp("2017-01-01") + pd.Timedelta(days=int(row.day_of_year) - 1)
    h, m = int(row.hour), round((row.hour - int(row.hour)) * 60 / 5) * 5
    return base.replace(hour=h, minute=min(m, 55), second=0, microsecond=0)

df["timestamp"] = df.apply(to_ts, axis=1)


# ── 4. Mock missing SCADA signals per fault signature ─────────────────────────
# p_ac  = actual_kw      (real measured output — this is what the inverter produced)
# i_dc  = actual_kw × 1000 / (U_DC × η)   (reflects actual output level)
# u_dc  = 0 for total_outage, else 580V   (U_DC collapses only on dead inverter)
# dv    = degradation_factor × 100 for curtailment (grid setpoint), else 100
# evu   = same as dv

df["p_ac"] = df["actual_kw"]
df["i_dc"] = (df["actual_kw"] * 1000 / (U_DC_NOMINAL * ETA_NOMINAL)).clip(lower=0)
df["u_dc"] = np.where(df["fault_type"] == "total_outage", 0.0, U_DC_NOMINAL)
df["dv"]   = np.where(df["fault_type"] == "curtailment",
                      (df["degradation_factor"] * 100).clip(0, 99), 100.0)
df["evu"]  = df["dv"]
df["irr"]  = df["irradiance_w_m2"]
df["t_mod"] = df["module_temp_c"]
df["t_amb"] = df["ambient_temp_c"]


# ── 5. Build per-inverter frames + fleet PR reference ─────────────────────────
def build_frame(sub: pd.DataFrame) -> pd.DataFrame:
    frame = pd.DataFrame({
        "p_ac": sub.p_ac.values,   "i_dc": sub.i_dc.values,
        "u_dc": sub.u_dc.values,   "dv":   sub.dv.values,
        "evu":  sub.evu.values,    "irr":  sub.irr.values,
        "t_mod": sub.t_mod.values, "t_amb": sub.t_amb.values,
        "cosphi": float("nan"),
    }, index=pd.DatetimeIndex(sub.timestamp.values))
    f = frame.sort_index()
    return f[~f.index.duplicated(keep="first")]


# Fleet PR from actual_kw (real measurements, all inverters)
KWPS = df.groupby("inverter_id")["inverter_pdc_kwp"].first().to_dict()
fleet_pr_dict = {}
for inv, sub in df.groupby("inverter_id"):
    fr = build_frame(sub)
    if len(fr) >= 5:
        fleet_pr_dict[inv] = triage.daily_pr(fr, KWPS[inv])

fleet_pr     = pd.DataFrame(fleet_pr_dict)
fleet_median = fleet_pr.median(axis=1)

# Only run pipeline on inverters that have fault rows
FAULT_INVS = df[df.is_fault == 1]["inverter_id"].unique().tolist()
print(f"\nFault inverters: {FAULT_INVS}")


# ── 6. Full pipeline per inverter ─────────────────────────────────────────────
print("\nRunning full pipeline...")
incidents, scores = [], []
empty_tickets = pd.DataFrame(columns=["start","inverter","component",
                                       "description","category"])

for inv in FAULT_INVS:
    sub  = df[df.inverter_id == inv].copy().sort_values("timestamp")
    kwp  = KWPS[inv]
    expected_fault_types = sub[sub.is_fault == 1]["fault_type"].unique().tolist()
    print(f"\n  {inv}  kWp={kwp}  expected={expected_fault_types}")

    frame = build_frame(sub)

    # ML predicted PR daily → Stage 1 reference
    # Aggregate per-row predicted_pr to daily median (irr > 100 only — skip sunrise noise)
    pr_ts       = pd.Series(sub.predicted_pr.values,
                            index=pd.DatetimeIndex(sub.timestamp.values))
    bright_mask = sub.irradiance_w_m2.values > 100
    ml_pr_daily = pr_ts[bright_mask].resample("D").median()

    # expected_kw series for EUR pricing (from ML API)
    expected_kw = pd.Series(sub.expected_kw.values,
                            index=pd.DatetimeIndex(sub.timestamp.values))

    pf   = round(float(ml_pr_daily.dropna().median()), 3)
    meta = {inv: {"kwp": kwp, "fit_ct_kwh": round(FIT*100, 1),
                  "year_mwh": round((frame.p_ac * 5/60).sum()/1000, 1),
                  "plant_factor": pf}}

    # Triage: ml_pr_daily as Stage 1 reference, actual p_ac in frame
    windows = triage.run(frame, kwp, ml_pr_daily, fleet_pr)
    print(f"    triage: {len(windows)} windows → {[w['subtype'] for w in windows]}")

    inv_incidents = []
    for i, inc in enumerate(windows):
        inc.update(impact.price(frame, expected_kw, inc["start"], inc["end"], FIT))
        if inc["lost_kwh"] < 1 and inc["classification"] != "weather":
            continue

        inc["inverter"] = inv
        inc["route"]    = orchestrator.route(inc, meta)
        inc.update(drafter.draft(inc, inv, kwp, FIT, empty_tickets))
        rag_compliance.annotate_draft(inc)

        # match ground truth labels to this window
        win_mask = (
            (pd.to_datetime(sub.timestamp) >= inc["start"]) &
            (pd.to_datetime(sub.timestamp) <= inc["end"] + pd.Timedelta(days=1))
        )
        truth = sub.loc[win_mask & (sub.is_fault == 1), "fault_type"].unique().tolist()
        norm  = lambda s: s.replace("_fault","").replace("_","")
        correct = any(norm(inc["subtype"]) == norm(t) for t in truth) if truth else False

        inc.update({
            "id":          f"{inv}-ft-{i}",
            "start":       str(inc["start"].date()),
            "end":         str(inc["end"].date()),
            "status":      "pending",
            "truth_types": truth,
            "correct":     correct,
        })
        inv_incidents.append(inc)
        print(f"      window {i}: subtype={inc['subtype']:15} truth={truth} "
              f"{'PASS' if correct else 'FAIL'}  "
              f"route={inc['route']['route']:8}  €{inc['eur_impact']:.1f}")

    incidents.extend(inv_incidents)

    # per-inverter score
    detected = {i["subtype"] for i in inv_incidents}
    covered  = [t for t in expected_fault_types
                if any(norm(t) == norm(d) for d in detected)]
    missed   = [t for t in expected_fault_types if t not in covered]
    scores.append({"inverter": inv, "expected": expected_fault_types,
                   "detected_subtypes": list(detected),
                   "covered": covered, "missed": missed,
                   "eur_total": round(sum(i["eur_impact"] for i in inv_incidents), 2)})


# ── 7. Save + summary ─────────────────────────────────────────────────────────
incidents = orchestrator.rank(incidents)
(OUT / "fault_test_incidents.json").write_text(json.dumps(incidents, indent=1))
(OUT / "fault_test_scores.json").write_text(json.dumps(scores, indent=1))

total_correct = sum(1 for i in incidents if i.get("correct"))
print(f"\n{'─'*60}")
print(f"Incidents: {len(incidents)}  |  Correct classification: {total_correct}/{len(incidents)}")
print(f"Saved: out/fault_test_incidents.json")
print(f"Saved: out/fault_test_scores.json\n")

print(f"{'Inverter':16} {'Expected':35} {'Covered':25} {'Missed':20} {'EUR':>8}")
for s in scores:
    print(f"  {s['inverter']:16} {str(s['expected']):35} "
          f"{str(s['covered']):25} {str(s['missed']):20} €{s['eur_total']:>7.1f}")
