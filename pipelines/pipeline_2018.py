"""2018 Validation Pipeline — runs the O&M Command Crew over all 65 inverters.

Drop inverter_performance_ratio (calculated by ML).
Map column names to match the triage/ml_api contract.
Stub dv=100 / evu=100 (no curtailment signal in 2018 dataset).
Rules-only orchestrator routing (no per-incident LLM for 65 inverters).

Run: python pipeline_2018.py
Outputs: out/report_2018.html, out/incidents_2018.json, out/timeseries_2018.json
"""

import json
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).parent          # pipelines/
BASE = HERE.parent.parent             # dataset dir (raw_data/, "2. Additional Data/")
OUT  = HERE.parent / "out"            # repo out/
OUT.mkdir(exist_ok=True)

sys.path.insert(0, str(HERE.parent))  # repo root, so `agents` is importable
from agents import drafter, impact, ml_api, orchestrator, triage

RAW_CSV = BASE / "raw_data/raw_validation_2018.csv"

# ── 1. load & clean ──────────────────────────────────────────────
print("Loading 2018 validation data …")
raw = pd.read_csv(RAW_CSV, parse_dates=["timestamp"])
raw = raw.drop(columns=["inverter_performance_ratio"], errors="ignore")
raw = raw.sort_values(["inverter_id", "timestamp"])
raw = raw.set_index("timestamp")

INVS = sorted(raw["inverter_id"].unique())
print(f"  {len(raw):,} rows · {len(INVS)} inverters · "
      f"{raw.index.min().date()} → {raw.index.max().date()}")

# ── 2. kWp per inverter (from dataset) ──────────────────────────
KWP = raw.groupby("inverter_id")["inverter_pdc_kwp"].first().to_dict()

# ── 3. FiT for 2018 ─────────────────────────────────────────────
try:
    fit_df = pd.ExcelFile(BASE / "2. Additional Data/feed-in-tarrifs.xlsx").parse("feed-in-tarrifs")
    dates  = pd.to_datetime(fit_df.iloc[0, 1:], errors="coerce")
    cols18 = [c for c, d in zip(fit_df.columns[1:], dates) if pd.notna(d) and d.year == 2018]
    FIT = {}
    for inv in INVS:
        row = fit_df[fit_df.iloc[:, 0].astype(str).str.strip() == inv]
        if len(row) and cols18:
            FIT[inv] = float(pd.to_numeric(row[cols18].iloc[0], errors="coerce").mean()) / 100
        else:
            FIT[inv] = 0.115          # fallback 11.5 ct/kWh
    print(f"  FiT loaded · sample INV 001: {FIT.get('INV 01.01.001',0)*100:.2f} ct/kWh")
except Exception as e:
    print(f"  FiT load failed ({e}) — using 11.5 ct/kWh flat")
    FIT = {inv: 0.115 for inv in INVS}

# ── 4. frame builder ─────────────────────────────────────────────
def frame_for(inv: str) -> pd.DataFrame:
    sub = raw[raw["inverter_id"] == inv].copy()
    sub = sub[~sub.index.duplicated(keep="first")]
    return pd.DataFrame({
        "p_ac":   sub["P_AC_kw"].fillna(0.0),
        "i_dc":   sub["I_DC_A"].fillna(0.0),
        "u_dc":   sub["U_DC_V"].fillna(0.0),
        "irr":    sub["irradiance_w_m2"].fillna(0.0),
        "t_mod":  sub["module_temp_c"].fillna(20.0),
        "t_amb":  sub["ambient_temp_c"].fillna(15.0),
        # no curtailment signal in 2018 dataset — assume no curtailment
        "dv":     100.0,
        "evu":    100.0,
        "cosphi": 1.0,
    })

# ── 5. fleet PR reference (shared sky) ──────────────────────────
print("Building fleet PR reference …")
FRAMES = {}
fleet_pr_dict = {}
for inv in INVS:
    f = frame_for(inv)
    FRAMES[inv] = f
    fleet_pr_dict[inv] = triage.daily_pr(f, KWP[inv])

fleet_pr = pd.DataFrame(fleet_pr_dict)

# ── 6. ML API health check ───────────────────────────────────────
_health = ml_api.health_check()
if _health.get("status") == "ok":
    print(f"[ml_api] online · best_iteration={_health.get('best_iteration')}")
else:
    print(f"[ml_api] offline ({_health.get('detail','?')}) — physics fallback active")

# ── 7. pipeline per inverter ─────────────────────────────────────
incidents, timeseries, meta = [], {}, {}
ACT_PR, PRED_PR = {}, {}   # daily PR per inverter: measured vs ML-predicted

for idx, inv in enumerate(INVS, 1):
    print(f"[{idx:2}/{len(INVS)}] {inv} …", end=" ", flush=True)
    f, kwp, fit = FRAMES[inv], KWP[inv], FIT[inv]

    expected, ml_pr_daily, pf = ml_api.predict_for_triage(f, inv, kwp)
    ACT_PR[inv]  = fleet_pr_dict[inv]   # measured daily PR (triage.daily_pr)
    PRED_PR[inv] = ml_pr_daily          # ML-predicted healthy-state daily PR

    daily_a = (f.p_ac.resample("D").sum() * 5 / 60)
    meta[inv] = {
        "kwp":        kwp,
        "fit_ct_kwh": round(fit * 100, 1),
        "year_mwh":   round(daily_a.sum() / 1000, 1),
        "plant_factor": round(pf, 3),
    }

    windows = triage.run(f, kwp, ml_pr_daily, fleet_pr)

    inv_incs = []
    for i, inc in enumerate(windows):
        inc.update(impact.price(f, expected, inc["start"], inc["end"], fit))
        if inc["lost_kwh"] < 10 and inc["classification"] != "weather":
            continue
        inc["inverter"] = inv
        # rules-only routing — no LLM per incident (65 inverters × N incidents too slow)
        sub = inc.get("subtype", "")
        if sub in orchestrator._DIRECT:
            r, conf, note = orchestrator._DIRECT[sub]
            inc["route"] = {"route": r, "confidence": conf, "note": note, "by": "rules-direct"}
        else:
            eur_day = inc.get("eur_impact", 0) / max(inc.get("days", 1), 1)
            if eur_day > orchestrator.TRUCK_ROLL_EUR / 10:
                inc["route"] = {"route": "dispatch", "confidence": 0.6,
                                "note": f"EUR {eur_day:.0f}/day burn", "by": "rules-fallback"}
            else:
                inc["route"] = {"route": "monitor", "confidence": 0.55,
                                "note": "Low burn rate — re-check in 3 days", "by": "rules-fallback"}
        inc.update(drafter.draft(inc, inv, kwp, fit,
                                 pd.DataFrame(columns=["category","component","start"])))
        inc.update({
            "id":     f"{inv}-{i}",
            "start":  str(inc["start"].date()),
            "end":    str(inc["end"].date()),
            "status": "pending",
        })
        inv_incs.append(inc)
        incidents.append(inc)

    daily_e = (expected.resample("D").sum() * 5 / 60)
    timeseries[inv] = {
        "dates":        [str(d.date()) for d in daily_a.index],
        "actual_kwh":   [round(v, 1) for v in daily_a],
        "expected_kwh": [round(v, 1) for v in daily_e],
        "curtailed":    [False] * len(daily_a),   # no DV/EVU in 2018
    }

    mwh    = meta[inv]["year_mwh"]
    eur    = sum(x["eur_impact"] for x in inv_incs)
    subs   = {}
    for x in inv_incs:
        subs[x.get("subtype", "?")] = subs.get(x.get("subtype", "?"), 0) + 1
    print(f"{mwh:.1f} MWh · {len(inv_incs)} incidents · €{eur:.0f}  {subs}")

# ── 8. save JSON ─────────────────────────────────────────────────
(OUT / "incidents_2018.json").write_text(json.dumps(incidents, indent=1))
(OUT / "timeseries_2018.json").write_text(json.dumps(timeseries))
(OUT / "meta_2018.json").write_text(json.dumps(meta, indent=1))
print(f"\nSaved {len(incidents)} incidents across {len(INVS)} inverters.")

# ── 8b. PR validation: fleet daily-mean measured vs predicted ────
act_df  = pd.DataFrame(ACT_PR)
pred_df = pd.DataFrame(PRED_PR)
act_mean  = act_df.mean(axis=1, skipna=True)
pred_mean = pred_df.mean(axis=1, skipna=True)
pr_cmp = pd.concat({"act": act_mean, "pred": pred_mean}, axis=1).dropna()
pr_cmp = pr_cmp[(pr_cmp["act"] > 0.05) & (pr_cmp["pred"] > 0.05)]  # drop dead-of-winter noise
mae  = float((pr_cmp["act"] - pr_cmp["pred"]).abs().mean()) if len(pr_cmp) else 0.0
bias = float((pr_cmp["pred"] - pr_cmp["act"]).mean()) if len(pr_cmp) else 0.0
(OUT / "pr_validation.json").write_text(json.dumps({
    "dates":        [str(d.date()) for d in pr_cmp.index],
    "actual_pr":    [round(v, 3) for v in pr_cmp["act"]],
    "predicted_pr": [round(v, 3) for v in pr_cmp["pred"]],
    "n_inverters":  len(INVS),
    "n_days":       len(pr_cmp),
    "mae":          round(mae, 4),
    "bias":         round(bias, 4),
    "ml_backend":   "ml-api" if _health.get("status") == "ok" else "physics-fallback",
}))
print(f"PR validation: {len(pr_cmp)} days · MAE {mae:.3f} · bias {bias:+.3f} → out/pr_validation.json")

# ── 9. report card ───────────────────────────────────────────────
total_mwh   = sum(m["year_mwh"]   for m in meta.values())
total_eur   = sum(i["eur_impact"] for i in incidents)
total_incs  = len(incidents)
dispatch_n  = sum(1 for i in incidents if i.get("route", {}).get("route") == "dispatch")
claim_n     = sum(1 for i in incidents if i.get("route", {}).get("route") == "claim")
monitor_n   = sum(1 for i in incidents if i.get("route", {}).get("route") == "monitor")

subtype_counts: dict = {}
for inc in incidents:
    k = inc.get("subtype", "unknown")
    subtype_counts[k] = subtype_counts.get(k, 0) + 1
subtype_sorted = sorted(subtype_counts.items(), key=lambda x: -x[1])
max_sub = max(v for _, v in subtype_sorted) if subtype_sorted else 1

per_inv_rows = []
for inv in INVS:
    ii   = [x for x in incidents if x["inverter"] == inv]
    eur  = sum(x["eur_impact"] for x in ii)
    mwh  = meta[inv]["year_mwh"]
    pf   = meta[inv]["plant_factor"]
    kwp  = meta[inv]["kwp"]
    disp = sum(1 for x in ii if x.get("route", {}).get("route") == "dispatch")
    subs = {}
    for x in ii:
        k = x.get("subtype", "?")
        subs[k] = subs.get(k, 0) + 1
    sub_str = ", ".join(f"{k}×{v}" for k, v in sorted(subs.items(), key=lambda x: -x[1])[:3])
    status_color = "#ef4444" if disp > 0 else "#4ade80"
    status_dot   = f'<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:{status_color};margin-right:6px"></span>'
    per_inv_rows.append(f"""
      <tr>
        <td style="font-family:monospace;font-size:12px">{status_dot}{inv}</td>
        <td>{kwp}</td>
        <td style="font-weight:600">{mwh:.1f}</td>
        <td>{pf:.3f}</td>
        <td>{len(ii)}</td>
        <td style="color:#FE5102;font-weight:600">€ {eur:,.0f}</td>
        <td style="color:{"#ef4444" if disp else "#6b7280"}">{disp}</td>
        <td style="font-size:11px;color:#6b7280">{sub_str or "—"}</td>
      </tr>""")

subtype_bars = ""
for sub, cnt in subtype_sorted:
    pct = cnt / max_sub * 100
    color = "#FE5102" if "fault" in sub or "outage" in sub else \
            "#4ade80" if sub in ("curtailment",) else "#6b7280"
    subtype_bars += f"""
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px">
        <div style="font-family:monospace;font-size:11px;width:130px;text-align:right;color:#9ca3af">{sub}</div>
        <div style="flex:1;background:#1e1e1e;border-radius:4px;height:18px;position:relative">
          <div style="width:{pct:.1f}%;background:{color};border-radius:4px;height:100%"></div>
        </div>
        <div style="font-family:monospace;font-size:11px;color:#d1d5db;width:32px">{cnt}</div>
      </div>"""

html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>beyondWatt — 2018 Validation Report</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;600;700&family=Space+Mono:wght@400;700&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  * {{ box-sizing:border-box; margin:0; padding:0 }}
  body {{ background:#0f0f0f; color:#FFFAEE; font-family:'Inter',sans-serif; padding:40px 48px; }}
  h1 {{ font-family:'Space Grotesk',sans-serif; font-size:28px; font-weight:700; letter-spacing:-.5px }}
  h2 {{ font-family:'Space Grotesk',sans-serif; font-size:16px; font-weight:700; margin-bottom:16px }}
  .mono {{ font-family:'Space Mono',monospace }}
  .dim  {{ color:#6b7280 }}
  .accent {{ color:#FE5102 }}
  .header {{ display:flex; justify-content:space-between; align-items:flex-start; margin-bottom:36px }}
  .badge {{ background:#FE510222; border:1px solid #FE510244; color:#FE5102;
            font-family:'Space Mono',monospace; font-size:10px; letter-spacing:1px;
            text-transform:uppercase; border-radius:999px; padding:4px 12px }}
  .grid4 {{ display:grid; grid-template-columns:repeat(4,1fr); gap:16px; margin-bottom:32px }}
  .card {{ background:#191919; border:1px solid #2a2a2a; border-radius:16px; padding:20px 22px }}
  .card .label {{ font-family:'Space Mono',monospace; font-size:9px; letter-spacing:1.5px;
                  text-transform:uppercase; color:#6b7280; margin-bottom:8px }}
  .card .val {{ font-family:'Space Grotesk',sans-serif; font-size:30px; font-weight:700; letter-spacing:-1px }}
  .card .sub  {{ font-size:12px; color:#6b7280; margin-top:4px }}
  .section {{ background:#191919; border:1px solid #2a2a2a; border-radius:16px;
              padding:24px 26px; margin-bottom:24px }}
  table {{ width:100%; border-collapse:collapse; font-size:13px }}
  th {{ font-family:'Space Mono',monospace; font-size:9px; letter-spacing:1px;
        text-transform:uppercase; color:#6b7280; text-align:left;
        padding:8px 10px; border-bottom:1px solid #2a2a2a }}
  td {{ padding:9px 10px; border-bottom:1px solid #151515; vertical-align:middle }}
  tr:last-child td {{ border-bottom:none }}
  tr:hover td {{ background:#FFFFFF05 }}
  .route-chips {{ display:flex; gap:10px; flex-wrap:wrap }}
  .chip {{ border-radius:999px; padding:5px 14px; font-family:'Space Mono',monospace;
           font-size:10px; letter-spacing:.5px; border:1px solid }}
  .chip-dispatch {{ color:#ef4444; border-color:#ef444444; background:#ef444411 }}
  .chip-claim     {{ color:#4ade80; border-color:#4ade8044; background:#4ade8011 }}
  .chip-monitor   {{ color:#FE5102; border-color:#FE510244; background:#FE510211 }}
  .footer {{ margin-top:40px; font-family:'Space Mono',monospace; font-size:10px;
             color:#374151; text-align:center }}
</style>
</head>
<body>

<div class="header">
  <div>
    <div style="font-family:'Space Mono',monospace;font-size:10px;letter-spacing:2px;color:#6b7280;margin-bottom:8px">BEYONDWATT</div>
    <h1>2018 Validation <span style="color:#FE5102">Report Card</span></h1>
    <div style="color:#6b7280;font-size:13px;margin-top:6px">
      Full-fleet · 65 inverters · Jan–Dec 2018 · Advisory only
    </div>
  </div>
  <div style="text-align:right">
    <div class="badge">validation run</div>
    <div style="font-family:'Space Mono',monospace;font-size:11px;color:#6b7280;margin-top:10px">
      generated {pd.Timestamp.now().strftime("%Y-%m-%d %H:%M")}
    </div>
  </div>
</div>

<!-- fleet KPIs -->
<div class="grid4">
  <div class="card">
    <div class="label">Fleet Yield</div>
    <div class="val">{total_mwh:.1f}<span style="font-size:16px;font-weight:400;color:#6b7280"> MWh</span></div>
    <div class="sub">{len(INVS)} inverters · 2018</div>
  </div>
  <div class="card">
    <div class="label">Total Incidents</div>
    <div class="val" style="color:#FE5102">{total_incs}</div>
    <div class="sub">{dispatch_n} dispatch · {claim_n} claim · {monitor_n} monitor</div>
  </div>
  <div class="card">
    <div class="label">Revenue Impact</div>
    <div class="val" style="color:#ef4444">€ {total_eur:,.0f}</div>
    <div class="sub">fault losses + claimable curtailment</div>
  </div>
  <div class="card">
    <div class="label">Avg Plant Factor</div>
    <div class="val">{sum(m["plant_factor"] for m in meta.values())/len(meta):.3f}</div>
    <div class="sub">fleet PR average</div>
  </div>
</div>

<!-- route distribution -->
<div class="section">
  <h2>Action Route Distribution</h2>
  <div class="route-chips">
    <div class="chip chip-dispatch">🔴 Dispatch — send technician &nbsp; <strong>{dispatch_n}</strong></div>
    <div class="chip chip-claim">🟢 Claim — EEG §15 compensation &nbsp; <strong>{claim_n}</strong></div>
    <div class="chip chip-monitor">🟡 Monitor — re-check in 3 days &nbsp; <strong>{monitor_n}</strong></div>
    <div class="chip" style="color:#6b7280;border-color:#2a2a2a">Log / Other &nbsp; <strong>{total_incs - dispatch_n - claim_n - monitor_n}</strong></div>
  </div>
</div>

<!-- subtype breakdown -->
<div class="section">
  <h2>Incident Subtype Breakdown</h2>
  {subtype_bars}
</div>

<!-- per-inverter table -->
<div class="section">
  <h2>Per-Inverter Summary</h2>
  <table>
    <thead><tr>
      <th>Inverter</th><th>kWp</th><th>MWh</th><th>PR</th>
      <th>Incidents</th><th>EUR Impact</th><th>Dispatch</th><th>Top Subtypes</th>
    </tr></thead>
    <tbody>
      {''.join(per_inv_rows)}
    </tbody>
  </table>
</div>

<div class="footer">
  beyondWatt · O&amp;M Command Crew · Advisory only — no action taken without human approval
</div>
</body>
</html>"""

report_path = OUT / "report_2018.html"
report_path.write_text(html)
print(f"Report card → {report_path}")
