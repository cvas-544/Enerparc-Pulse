"""Generate interactive PR chart: actual vs ML-predicted PR for all 5 inverters.

Actual PR   = daily E_AC / (daily H_POA × kWp)   [IEC 61724-1]
Predicted PR = daily expected_kWh / (daily H_POA × kWp)  [ML model baseline]

Output: out/pr_chart.html
"""

import json
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).parent          # eval/
BASE = HERE.parent.parent             # dataset dir (raw_data/, "2. Additional Data/")
OUT  = HERE.parent / "out"            # repo out/
sys.path.insert(0, str(HERE.parent))  # repo root, so `agents` is importable

from agents import ml_api, triage

# ── load raw SCADA ────────────────────────────────────────────────
print("Loading SCADA …")
raw = pd.read_csv(
    BASE / "raw_data/inverters_first5_2023.csv",
    sep=";", decimal=",", parse_dates=["timestamp"],
    date_format="%Y.%m.%d %H:%M",
).groupby("timestamp").mean(numeric_only=True)

INVS = [f"INV 01.01.00{i}" for i in range(1, 6)]

# ── kWp from System_Overview ──────────────────────────────────────
so = pd.ExcelFile(BASE / "2. Additional Data/System_Overview.xlsx").parse("PV plant info")
desc_col, kwp_col = so.columns[2], so.columns[11]
KWP = {}
for inv in INVS:
    tag = ("WR " + inv.split("INV ")[1]).replace(".", " .").replace(" ", "")
    row = so[so[desc_col].astype(str).str.replace(" ", "") == tag]
    KWP[inv] = float(row[kwp_col].iloc[0]) if len(row) else 30.6

def frame_for(inv):
    return pd.DataFrame({
        "p_ac": raw[f"{inv} / P_AC (kW)"].fillna(0.0),
        "i_dc": raw[f"{inv} / I_DC_SUM (A)"].fillna(0.0),
        "u_dc": raw[f"{inv} / U_DC (V)"].fillna(0.0),
        "dv":   raw["DRD11A / DV (%)"],
        "evu":  raw["DRD11A / EVU (%)"],
        "cosphi": raw["Janitza UMG 604 - DRD11A / CosPhi_L1..L3"],
        "irr":  raw["Plant / Irradiation_average (W/m²)"],
        "t_mod": raw["Temperature Sensor / Module (°C)"],
        "t_amb": raw["Temperature Sensor / Ambient (°C)"],
    })

# ── per-inverter: actual PR + ML-predicted PR ─────────────────────
print("Computing PR series …")
CHART_DATA = {}

for inv in INVS:
    print(f"  {inv} …", end=" ", flush=True)
    f   = frame_for(inv)
    kwp = KWP[inv]

    # actual daily PR (IEC 61724-1)
    act_pr = triage.daily_pr(f, kwp)

    # ML predicted PR — expected_kw from model → daily expected kWh → PR
    expected_kw, ml_pr_daily, _ = ml_api.predict_for_triage(f, inv, kwp)
    # ml_pr_daily is already daily-smoothed from the triage module
    # also compute directly: expected_kwh / (H_POA × kWp)
    daily_exp_kwh = (expected_kw.resample("D").sum() * 5 / 60)
    daily_irr_kwh = (f.irr.resample("D").sum() * 5 / 60 / 1000)  # W/m² → kWh/m²
    pred_pr_direct = (daily_exp_kwh / (daily_irr_kwh * kwp)).replace([float("inf")], None)

    # curtailment flag
    curtail = ((f.dv.resample("D").min() < 100) | (f.evu.resample("D").min() < 100))

    # align all series on common date index, filter low-irr days (winter noise)
    df = pd.DataFrame({
        "act_pr":   act_pr,
        "pred_pr":  pred_pr_direct,
        "ml_pr":    ml_pr_daily,
        "irr_kwh":  daily_irr_kwh,
        "curtailed": curtail,
    }).dropna(subset=["act_pr", "pred_pr"])

    # only keep days with enough irradiation (> 0.5 kWh/m²) to avoid winter noise
    df = df[df["irr_kwh"] > 0.5]
    # cap PR at 1.3 (outliers from cloudy mornings / sensor noise)
    df["act_pr"]  = df["act_pr"].clip(0, 1.3)
    df["pred_pr"] = df["pred_pr"].clip(0, 1.3)

    CHART_DATA[inv] = {
        "dates":     [str(d.date()) for d in df.index],
        "actual":    [round(v, 3) for v in df["act_pr"]],
        "predicted": [round(v, 3) for v in df["pred_pr"]],
        "curtailed": [bool(b) for b in df["curtailed"]],
        "kwp":       kwp,
    }
    print(f"days={len(df)}  mean_act={df['act_pr'].mean():.3f}  mean_pred={df['pred_pr'].mean():.3f}")

# ── build HTML chart ──────────────────────────────────────────────
colors = {
    "INV 01.01.001": "#FE5102",
    "INV 01.01.002": "#ef4444",
    "INV 01.01.003": "#4ade80",
    "INV 01.01.004": "#facc15",
    "INV 01.01.005": "#60a5fa",
}

# build per-inverter dataset blocks for Chart.js
datasets_js = []
for inv, d in CHART_DATA.items():
    col = colors[inv]
    short = inv.split("INV ")[1]
    datasets_js.append(f"""
    {{
      label: 'Actual PR — {short}',
      data: {json.dumps([{{"x": dt, "y": v}} for dt, v in zip(d["dates"], d["actual"])])},
      borderColor: '{col}',
      backgroundColor: '{col}22',
      borderWidth: 1.5,
      pointRadius: 0,
      tension: 0.3,
      fill: false,
    }},
    {{
      label: 'Predicted PR — {short}',
      data: {json.dumps([{{"x": dt, "y": v}} for dt, v in zip(d["dates"], d["predicted"])])},
      borderColor: '{col}',
      backgroundColor: 'transparent',
      borderWidth: 1,
      borderDash: [4, 3],
      pointRadius: 0,
      tension: 0.3,
      fill: false,
    }},""")

datasets_str = "\n".join(datasets_js)

# summary table
rows_html = ""
for inv, d in CHART_DATA.items():
    act_mean  = sum(d["actual"])  / len(d["actual"])
    pred_mean = sum(d["predicted"]) / len(d["predicted"])
    gap = act_mean - pred_mean
    gap_color = "#ef4444" if gap < -0.05 else "#4ade80" if gap > 0.02 else "#6b7280"
    rows_html += f"""<tr>
      <td style="font-family:monospace;font-size:12px">{inv}</td>
      <td>{d["kwp"]}</td>
      <td>{len(d["dates"])}</td>
      <td style="font-weight:600">{act_mean:.3f}</td>
      <td style="color:#FE5102">{pred_mean:.3f}</td>
      <td style="color:{gap_color};font-weight:600">{gap:+.3f}</td>
    </tr>"""

html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>beyondWatt — PR: Actual vs ML-Predicted</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js"></script>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;600;700&family=Space+Mono&family=Inter:wght@400;500&display=swap" rel="stylesheet">
<style>
  * {{ box-sizing:border-box; margin:0; padding:0 }}
  body {{ background:#0f0f0f; color:#FFFAEE; font-family:'Inter',sans-serif; padding:40px 48px }}
  h1 {{ font-family:'Space Grotesk',sans-serif; font-size:24px; font-weight:700; margin-bottom:4px }}
  .sub {{ color:#6b7280; font-size:13px; margin-bottom:32px }}
  .card {{ background:#191919; border:1px solid #2a2a2a; border-radius:16px; padding:24px; margin-bottom:24px }}
  .chart-wrap {{ position:relative; height:420px }}
  h2 {{ font-family:'Space Grotesk',sans-serif; font-size:14px; font-weight:600;
        margin-bottom:16px; color:#FFFAEE }}
  table {{ width:100%; border-collapse:collapse; font-size:13px }}
  th {{ font-family:'Space Mono',monospace; font-size:9px; letter-spacing:1px;
        text-transform:uppercase; color:#6b7280; padding:8px 12px;
        border-bottom:1px solid #2a2a2a; text-align:left }}
  td {{ padding:9px 12px; border-bottom:1px solid #151515; color:#d1d5db }}
  tr:last-child td {{ border-bottom:none }}
  .legend {{ display:flex; gap:20px; flex-wrap:wrap; margin-bottom:16px; font-size:12px }}
  .leg {{ display:flex; align-items:center; gap:6px }}
  .leg-line {{ width:24px; height:2px }}
  .leg-dash {{ width:24px; height:0; border-top:2px dashed }}
</style>
</head>
<body>

<div style="font-family:'Space Mono',monospace;font-size:10px;letter-spacing:2px;color:#6b7280;margin-bottom:8px">BEYONDWATT</div>
<h1>Performance Ratio — Actual vs <span style="color:#FE5102">ML-Predicted</span></h1>
<div class="sub">IEC 61724-1 · 5 inverters · full-year 2023 · days with irradiation &gt; 0.5 kWh/m² only</div>

<div class="card">
  <h2>Daily PR — all inverters</h2>
  <div class="legend">
    <div class="leg"><div class="leg-line" style="background:#FE5102"></div> Actual PR (solid)</div>
    <div class="leg"><div class="leg-dash" style="border-color:#FE5102"></div> ML-Predicted PR (dashed)</div>
    <div class="leg" style="color:#6b7280">Fault windows: actual drops below predicted</div>
  </div>
  <div class="chart-wrap">
    <canvas id="prChart"></canvas>
  </div>
</div>

<div class="card">
  <h2>Fleet Summary — mean annual PR</h2>
  <table>
    <thead><tr>
      <th>Inverter</th><th>kWp</th><th>Valid Days</th>
      <th>Mean Actual PR</th><th>Mean Predicted PR</th><th>Gap (act − pred)</th>
    </tr></thead>
    <tbody>{rows_html}</tbody>
  </table>
</div>

<script>
const ctx = document.getElementById('prChart').getContext('2d');
new Chart(ctx, {{
  type: 'line',
  data: {{
    datasets: [{datasets_str}]
  }},
  options: {{
    responsive: true,
    maintainAspectRatio: false,
    interaction: {{ mode: 'index', intersect: false }},
    plugins: {{
      legend: {{
        labels: {{
          color: '#9ca3af',
          font: {{ family: 'Space Mono', size: 10 }},
          boxWidth: 20,
          padding: 12,
        }}
      }},
      tooltip: {{
        backgroundColor: '#191919',
        borderColor: '#2a2a2a',
        borderWidth: 1,
        titleColor: '#FFFAEE',
        bodyColor: '#9ca3af',
        titleFont: {{ family: 'Space Mono', size: 10 }},
      }}
    }},
    scales: {{
      x: {{
        type: 'time',
        time: {{ unit: 'month', displayFormats: {{ month: 'MMM' }} }},
        grid: {{ color: '#1e1e1e' }},
        ticks: {{ color: '#6b7280', font: {{ family: 'Space Mono', size: 10 }} }}
      }},
      y: {{
        min: 0,
        max: 1.3,
        title: {{ display: true, text: 'Performance Ratio', color: '#6b7280',
                  font: {{ family: 'Space Mono', size: 10 }} }},
        grid: {{ color: '#1e1e1e' }},
        ticks: {{ color: '#6b7280', font: {{ family: 'Space Mono', size: 10 }} }}
      }}
    }}
  }}
}});
</script>

<div style="margin-top:24px;font-family:'Space Mono',monospace;font-size:10px;color:#374151;text-align:center">
  beyondWatt · IEC 61724-1 PR = E_AC / (H_POA × kWp) · ML model: LightGBM on Render · Advisory only
</div>
</body>
</html>"""

out_path = OUT / "pr_chart.html"
out_path.write_text(html)
print(f"\nChart → {out_path}")
print("Open in browser: open out/pr_chart.html")
