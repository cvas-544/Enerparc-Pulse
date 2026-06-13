"""Orchestrator — runs the O&M Command Crew over the first 5 inverters, 2023.

  Triage (PR cascade) -> Impact (EUR via FiT) -> Action Drafter -> out/*.json
  Forecast agent is called live by the dashboard (agents/forecast.py).

Run: python pipeline.py  ->  out/incidents.json, out/timeseries.json, out/meta.json
"""

import json
from pathlib import Path

import pandas as pd

from agents import drafter, impact, ml_api, orchestrator, rag_compliance, triage

HERE = Path(__file__).parent
BASE = HERE.parent
OUT = HERE / "out"
OUT.mkdir(exist_ok=True)

INVS = [f"INV 01.01.00{i}" for i in range(1, 6)]

# ---- per-inverter kWp from System_Overview
so = pd.ExcelFile(BASE / "2. Additional Data/System_Overview.xlsx").parse("PV plant info")
desc_col, kwp_col = so.columns[2], so.columns[11]
KWP = {}
for inv in INVS:
    tag = ("WR " + inv.split("INV ")[1]).replace(".", " .").replace(" ", "")
    row = so[so[desc_col].astype(str).str.replace(" ", "").str.replace("WR0", "WR 0", regex=False)
             .str.replace(" ", "") == tag]
    row = so[so[desc_col].astype(str).str.replace(" ", "") == tag]
    KWP[inv] = float(row[kwp_col].iloc[0]) if len(row) else 30.6

# ---- per-inverter 2023 FiT (EUR/kWh)
fit_df = pd.ExcelFile(BASE / "2. Additional Data/feed-in-tarrifs.xlsx").parse("feed-in-tarrifs")
dates = pd.to_datetime(fit_df.iloc[0, 1:], errors="coerce")
cols_2023 = [c for c, d in zip(fit_df.columns[1:], dates) if pd.notna(d) and d.year == 2023]
FIT = {}
for inv in INVS:
    row = fit_df[fit_df.iloc[:, 0].astype(str).str.strip() == inv]
    FIT[inv] = float(pd.to_numeric(row[cols_2023].iloc[0], errors="coerce").mean()) / 100 if len(row) else 0.115

# ---- telemetry
raw = pd.read_csv(BASE / "raw_data/inverters_first5_2023.csv",
                  sep=";", decimal=",", parse_dates=["timestamp"],
                  date_format="%Y.%m.%d %H:%M").groupby("timestamp").mean(numeric_only=True)


def frame_for(inv: str) -> pd.DataFrame:
    return pd.DataFrame({
        "p_ac": raw[f"{inv} / P_AC (kW)"].fillna(0.0),
        "i_dc": raw[f"{inv} / I_DC_SUM (A)"].fillna(0.0),
        "u_dc": raw[f"{inv} / U_DC (V)"].fillna(0.0),
        "dv": raw["DRD11A / DV (%)"], "evu": raw["DRD11A / EVU (%)"],
        "cosphi": raw["Janitza UMG 604 - DRD11A / CosPhi_L1..L3"],
        "irr": raw["Plant / Irradiation_average (W/m²)"],
        "t_mod": raw["Temperature Sensor / Module (°C)"],
        "t_amb": raw["Temperature Sensor / Ambient (°C)"],
    })


FRAMES = {inv: frame_for(inv) for inv in INVS}

# ---- fleet PR reference (shared sky -> weather cancels)
fleet_pr = pd.DataFrame({inv: triage.daily_pr(FRAMES[inv], KWP[inv]) for inv in INVS})
fleet_median = fleet_pr.median(axis=1)

tickets = pd.ExcelFile(BASE / "2. Additional Data/Tickets.xlsx").parse("2020-2026")
tickets["start"] = pd.to_datetime(tickets["startdate"], utc=True).dt.tz_localize(None)

# ---- ML API health check on startup
_health = ml_api.health_check()
if _health.get("status") == "ok":
    print(f"[ml_api] model loaded, best_iteration={_health.get('best_iteration')}")
else:
    print(f"[ml_api] unavailable ({_health.get('detail','?')}) — physics fallback active")

incidents, timeseries, meta = [], {}, {}
for inv in INVS:
    f, kwp, fit = FRAMES[inv], KWP[inv], FIT[inv]

    # ML API: one call → expected_kw (EUR pricing) + ml_pr_daily (triage Stage 1 reference)
    expected, ml_pr_daily, pf = ml_api.predict_for_triage(f, inv, kwp)

    daily_a = (f.p_ac.resample("D").sum() * 5 / 60)
    meta[inv] = {"kwp": kwp, "fit_ct_kwh": round(fit * 100, 1),
                 "year_mwh": round(daily_a.sum() / 1000, 1), "plant_factor": round(pf, 3)}

    # 1. Triage agent — ml_pr_daily replaces fleet_median as Stage 1 reference
    #    deviation = ml_predicted_PR − actual_PR  (per-inverter ML baseline, not peer median)
    windows = triage.run(f, kwp, ml_pr_daily, fleet_pr)

    for i, inc in enumerate(windows):
        # 2. Impact agent — price first; routing weighs the euros
        inc.update(impact.price(f, expected, inc["start"], inc["end"], fit))
        if inc["lost_kwh"] < 10 and inc["classification"] != "weather":
            continue
        inc["inverter"] = inv
        # 3. Orchestrator — decides the action path (LLM for ambiguous evidence)
        inc["route"] = orchestrator.route(inc, meta)
        # 4. Action Drafter — renders whatever was routed
        inc.update(drafter.draft(inc, inv, kwp, fit, tickets))
        # 5. RAG Compliance — appends regulation citation to the draft
        rag_compliance.annotate_draft(inc)
        inc.update({"id": f"{inv}-{i}",
                    "start": str(inc["start"].date()), "end": str(inc["end"].date()),
                    "status": "pending"})
        incidents.append(inc)

    # grid-support recommendation (cos phi)
    q = drafter.reactive_power(inv, f, kwp)
    if q and inv == INVS[0]:           # one card is enough for the demo
        incidents.append(q)

    daily_e = (expected.resample("D").sum() * 5 / 60)
    curtail_days = (f.dv.resample("D").min() < 100) | (f.evu.resample("D").min() < 100)
    timeseries[inv] = {
        "dates": [str(d.date()) for d in daily_a.index],
        "actual_kwh": [round(v, 1) for v in daily_a],
        "expected_kwh": [round(v, 1) for v in daily_e],
        "curtailed": [bool(b) for b in curtail_days.reindex(daily_a.index, fill_value=False)],
    }

# fleet priority + briefing (orchestrator, post-routing)
incidents = orchestrator.rank(incidents)
(OUT / "briefing.txt").write_text(orchestrator.briefing(incidents, meta))

(OUT / "incidents.json").write_text(json.dumps(incidents, indent=1))
(OUT / "timeseries.json").write_text(json.dumps(timeseries))
(OUT / "meta.json").write_text(json.dumps(meta, indent=1))

print(f"{'inv':14} {'MWh':>6} {'incidents':>9} {'EUR':>9}   subtypes")
for inv in INVS:
    ii = [x for x in incidents if x["inverter"] == inv]
    subs = {}
    for x in ii:
        subs[x["subtype"]] = subs.get(x["subtype"], 0) + 1
    print(f"{inv:14} {meta[inv]['year_mwh']:6.1f} {len(ii):9} "
          f"{sum(x['eur_impact'] for x in ii):9.2f}   {subs}")
