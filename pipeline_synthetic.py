"""Synthetic fault pipeline — runs all 6 injected scenarios through the full
O&M agentic system with LightGBM expected-power replacing the physics baseline.

Flow per scenario:
  synthetic frame  →  triage (PR cascade)
                   →  ML expected  →  impact.price (EUR)
                   →  orchestrator.route
                   →  drafter.draft
                   →  rag_compliance.annotate_draft
                   →  out/synthetic_incidents.json

Dashboard reads this file via GET /api/synthetic_incidents (add to app.py).
Run: python pipeline_synthetic.py
"""

import json
from pathlib import Path

import pandas as pd

from agents import drafter, impact, ml_api, orchestrator, rag_compliance, triage

HERE = Path(__file__).parent
BASE = HERE.parent
OUT  = HERE / "out"
OUT.mkdir(exist_ok=True)

KWP  = 30.6          # synthetic scenarios are all clones of INV 01.01.005
FIT  = 0.116         # EUR/kWh — 2023 mean FiT for INV 005

# ── 1. Health check ML API ────────────────────────────────────────────────────
_health = ml_api.health_check()
if _health.get("status") == "ok":
    print(f"[ml_api] model loaded OK — best_iteration={_health.get('best_iteration')}")
else:
    print(f"[ml_api] unavailable ({_health.get('detail','?')}) — physics fallback active")

# ── 2. Load real INV 005 data (fleet PR reference only) ───────────────────────
print("Loading real INV 005 telemetry for fleet PR reference...")
raw = pd.read_csv(
    BASE / "raw_data/inverters_first5_2023.csv",
    sep=";", decimal=",", parse_dates=["timestamp"],
    date_format="%Y.%m.%d %H:%M",
).groupby("timestamp").mean(numeric_only=True)

INV = "INV 01.01.005"
real_frame = pd.DataFrame({
    "p_ac":   raw[f"{INV} / P_AC (kW)"].fillna(0.0),
    "i_dc":   raw[f"{INV} / I_DC_SUM (A)"].fillna(0.0),
    "u_dc":   raw[f"{INV} / U_DC (V)"].fillna(0.0),
    "dv":     raw["DRD11A / DV (%)"],
    "evu":    raw["DRD11A / EVU (%)"],
    "cosphi": raw["Janitza UMG 604 - DRD11A / CosPhi_L1..L3"],
    "irr":    raw["Plant / Irradiation_average (W/m²)"],
    "t_mod":  raw["Temperature Sensor / Module (°C)"],
    "t_amb":  raw["Temperature Sensor / Ambient (°C)"],
})


# ── 3. Fleet PR reference — real inverters, real sky (weather cancels) ─────────
KWPS = {1: 30.6, 2: 30.6, 3: 30.6, 4: 24.48, 5: 30.6}

def _frame(inv_num: int) -> pd.DataFrame:
    inv = f"INV 01.01.00{inv_num}"
    return pd.DataFrame({
        "p_ac":  raw[f"{inv} / P_AC (kW)"].fillna(0.0),
        "i_dc":  raw[f"{inv} / I_DC_SUM (A)"].fillna(0.0),
        "u_dc":  raw[f"{inv} / U_DC (V)"].fillna(0.0),
        "dv":    raw["DRD11A / DV (%)"],
        "evu":   raw["DRD11A / EVU (%)"],
        "cosphi": raw["Janitza UMG 604 - DRD11A / CosPhi_L1..L3"],
        "irr":   raw["Plant / Irradiation_average (W/m²)"],
        "t_mod": raw["Temperature Sensor / Module (°C)"],
        "t_amb": raw["Temperature Sensor / Ambient (°C)"],
    })

fleet_pr = pd.DataFrame({
    i: triage.daily_pr(_frame(i), KWPS[i]) for i in KWPS
})
fleet_median = fleet_pr.median(axis=1)

# ── 4. Load synthetic scenarios ───────────────────────────────────────────────
print("Loading synthetic fault scenarios...")
syn_path   = BASE / "raw_data/synthetic_eval.csv"
truth_path = OUT / "synthetic_truth.json"

syn   = pd.read_csv(syn_path, parse_dates=["timestamp"]).set_index("timestamp")
truth = json.loads(truth_path.read_text())

# ── 5. Run each scenario through the full pipeline ───────────────────────────
incidents = []
results   = []

for name, t in truth.items():
    s = syn[syn["scenario"] == name].copy()
    s.index = pd.to_datetime(s.index)
    s = s.sort_index()

    # Build frame identical to pipeline.py frame_for()
    # (synthetic CSV uses short column names from inject.py)
    frame = pd.DataFrame({
        "p_ac":   s["p_ac"].fillna(0.0),
        "i_dc":   s["i_dc"].fillna(0.0),
        "u_dc":   s["u_dc"].fillna(0.0),
        "dv":     s["dv"],
        "evu":    s["evu"],
        "cosphi": pd.Series(dtype=float),   # not in synthetic — fill NaN
        "irr":    s["irr"],
        "t_mod":  s["t_mod"],
        "t_amb":  s["t_amb"],
    }).reindex(s.index)

    # fill cosphi column with NaN (not in synthetic data)
    frame["cosphi"] = float("nan")

    # ── ML API: expected_kw + ml_pr_daily in one call ─────────────────────────
    # synthetic inv_id not in INV_META — pass INV 005 metadata explicitly
    ml_expected, ml_pr_daily, pf = ml_api.predict_for_triage(
        frame, "INV 01.01.005", KWP)

    # ── Triage: PR cascade — ML predicted PR as Stage 1 reference ─────────────
    windows = triage.run(frame, KWP, ml_pr_daily, fleet_pr)

    if not windows:
        print(f"  {name:13} [{t['label']:13}]  MISSED by PR cascade")
        results.append({"scenario": name, "label": t["label"], "detected": False,
                        "incidents": 0})
        continue

    scenario_incs = []
    for i, inc in enumerate(windows):
        # Impact — ML predicted expected replaces physics
        price_info = impact.price(frame, ml_expected, inc["start"], inc["end"], FIT)
        inc.update(price_info)

        if inc["lost_kwh"] < 10 and inc["classification"] != "weather":
            continue

        inc["inverter"] = f"SYN_{name}"

        # Orchestrator route
        meta = {f"SYN_{name}": {"kwp": KWP, "fit_ct_kwh": round(FIT * 100, 1),
                                 "year_mwh": round((frame.p_ac * 5/60).sum() / 1000, 1),
                                 "plant_factor": pf}}
        inc["route"] = orchestrator.route(inc, meta)

        # Drafter
        inc.update(drafter.draft(inc, f"SYN_{name}", KWP, FIT,
                                 pd.DataFrame(columns=["start","inverter","component",
                                                        "description","category"])))

        # RAG compliance annotation
        rag_compliance.annotate_draft(inc)

        # Ground-truth label for eval
        inc["ground_truth"] = t["label"]
        inc["scenario"] = name
        inc.update({
            "id":     f"{name}-{i}",
            "start":  str(inc["start"].date()),
            "end":    str(inc["end"].date()),
            "status": "pending",
        })
        scenario_incs.append(inc)

    # triage returns "outage" but truth label is "outage_fault" — normalise
    def _norm(s): return s.replace("_fault", "").replace("_", "")
    correct = any(_norm(i["subtype"]) == _norm(t["label"]) for i in scenario_incs)
    print(f"  {name:13} [{t['label']:13}]  "
          f"{len(scenario_incs)} incident(s)  "
          f"route={scenario_incs[0]['route']['route'] if scenario_incs else 'n/a':8}  "
          f"{'PASS' if correct else 'FAIL (subtype mismatch)'}")

    results.append({
        "scenario": name,
        "label":    t["label"],
        "detected": len(scenario_incs) > 0,
        "correct":  correct,
        "incidents": len(scenario_incs),
        "route":    scenario_incs[0]["route"]["route"] if scenario_incs else None,
        "eur":      round(sum(i["eur_impact"] for i in scenario_incs), 2),
    })
    incidents.extend(scenario_incs)

# ── 6. Rank + save ────────────────────────────────────────────────────────────
incidents = orchestrator.rank(incidents)
(OUT / "synthetic_incidents.json").write_text(json.dumps(incidents, indent=1))
(OUT / "synthetic_results.json").write_text(json.dumps(results, indent=1))

print(f"\nSaved: out/synthetic_incidents.json  ({len(incidents)} incidents)")
print(f"Saved: out/synthetic_results.json   ({len(results)} scenarios)")

detected = sum(r["detected"] for r in results)
correct  = sum(r.get("correct", False) for r in results)
print(f"\nDetection: {detected}/{len(results)}   Classification: {correct}/{len(results)}")
print(f"Expected: 6/6 (matches eval.py PR cascade score)\n")
print(f"{'Scenario':13} {'Label':13} {'EUR':>8}  Route")
for r in sorted(results, key=lambda x: -x.get("eur", 0)):
    print(f"  {r['scenario']:13} {r['label']:13} "
          f"{r.get('eur', 0):>8.2f}  {r.get('route', 'missed')}")
