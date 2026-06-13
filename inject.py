"""Fault injector — creates synthetic eval set from REAL inverter-005 telemetry.

Six scenarios with known ground truth, each a full-year copy of INV 01.01.005
with one signature injected. Output:
  raw_data/synthetic_eval.csv   (long format, scenario column)
  out/synthetic_truth.json      (ground truth labels + windows)

Synthetic data is for EVAL ONLY — the dashboard demos real data.
"""

import json
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).parent
BASE = HERE.parent
INV = "INV 01.01.005"

df = pd.read_csv(BASE / "raw_data/inverters_first5_2023.csv",
                 sep=";", decimal=",", parse_dates=["timestamp"],
                 date_format="%Y.%m.%d %H:%M")
df = df.groupby("timestamp").mean(numeric_only=True)
base = pd.DataFrame({
    "p_ac": df[f"{INV} / P_AC (kW)"].fillna(0.0),
    "i_dc": df[f"{INV} / I_DC_SUM (A)"].fillna(0.0),
    "u_dc": df[f"{INV} / U_DC (V)"].fillna(0.0),
    "dv": df["DRD11A / DV (%)"],
    "evu": df["DRD11A / EVU (%)"],
    "irr": df["Plant / Irradiation_average (W/m²)"],
    "t_mod": df["Temperature Sensor / Module (°C)"],
    "t_amb": df["Temperature Sensor / Ambient (°C)"],
})

truth, frames = {}, []


def add(name, frame, label, start, end):
    frame = frame.copy()
    frame["scenario"] = name
    frames.append(frame)
    truth[name] = {"label": label, "start": start, "end": end}


# 1. string failure: 1 of 5 strings out from Jul 1 — step on DC current AND power
s = base.copy()
m = s.index >= "2023-07-01"
s.loc[m, ["p_ac", "i_dc"]] *= 0.80
add("SYN_STRING", s, "string_fault", "2023-07-01", "2023-12-31")

# 2. soiling: slow drift 1.00 -> 0.85 over Aug-Oct, stays dirty
s = base.copy()
ramp = pd.Series(1.0, index=s.index)
win = (s.index >= "2023-08-01") & (s.index <= "2023-10-31")
ramp[win] = np.linspace(1.0, 0.85, win.sum())
ramp[s.index > "2023-10-31"] = 0.85
s["p_ac"] *= ramp
s["i_dc"] *= ramp
add("SYN_SOIL", s, "soiling", "2023-08-01", "2023-12-31")

# 3. AC-side fault: conversion loss from Jun 1 — P_AC down, DC side untouched
s = base.copy()
s.loc[s.index >= "2023-06-01", "p_ac"] *= 0.82
add("SYN_ACFAULT", s, "ac_fault", "2023-06-01", "2023-12-31")

# 4. curtailment: DV=60 % for two August days, output capped accordingly
s = base.copy()
m = (s.index >= "2023-08-10") & (s.index < "2023-08-12")
s.loc[m, "dv"] = 60.0
s.loc[m, "p_ac"] *= 0.60
add("SYN_CURT", s, "curtailment", "2023-08-10", "2023-08-11")

# 5. total outage: dead Sep 5-12, irradiation healthy
s = base.copy()
m = (s.index >= "2023-09-05") & (s.index < "2023-09-13")
s.loc[m, ["p_ac", "i_dc"]] = 0.0
add("SYN_OUTAGE", s, "outage_fault", "2023-09-05", "2023-09-12")

# 6. snow: 3 consecutive SUNNY winter days (PR undefined on dark days — pick
# days with enough irradiation to screen), dead until 13:00, freezing ambient
s = base.copy()
h_day = (s.irr.clip(lower=0) * 5 / 60 / 1000).resample("D").sum()
curt_free = ((s.dv.resample("D").min() >= 100) & (s.evu.resample("D").min() >= 100))
sunny_winter = h_day[(h_day.index.month.isin([1, 2])) & (h_day > 1.2)
                     & curt_free.reindex(h_day.index, fill_value=False)].index
snow_days = list(sunny_winter[:3])
for d in snow_days:
    m = (s.index >= d) & (s.index < d + pd.Timedelta(hours=13))
    s.loc[m, ["p_ac", "i_dc"]] = 0.0
    s.loc[s.index.normalize() == d, "t_amb"] = -2.0
add("SYN_SNOW", s, "snow", str(snow_days[0].date()), str(snow_days[-1].date()))

out = pd.concat(frames).reset_index().rename(columns={"index": "timestamp"})
out.to_csv(BASE / "raw_data/synthetic_eval.csv", index=False)
(HERE / "out").mkdir(exist_ok=True)
(HERE / "out/synthetic_truth.json").write_text(json.dumps(truth, indent=1))
print(f"6 scenarios x {len(base)} rows -> raw_data/synthetic_eval.csv")
for k, v in truth.items():
    print(f"  {k:13} {v['label']:13} {v['start']} -> {v['end']}")
