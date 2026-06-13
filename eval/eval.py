"""PR-cascade eval — Stage 1 (PR screen) + Stage 2 (root-cause drill-down)
run against the synthetic fault set. Prints per-scenario verdicts + score.

Stage 1: daily PR, absolute floor (median-3MAD of healthy ref) + fleet-median
deviation (>0.09 for >=3d, or >0.30 single day).
Stage 2, in cost order: curtailment -> outage -> snow -> AC/DC split (eta) ->
step (string) vs drift (soiling).
"""

import json
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).parent       # eval/
BASE = HERE.parent.parent          # dataset dir (raw_data/)
KWP = 30.6  # synthetic scenarios are clones of INV 01.01.005

syn = pd.read_csv(BASE / "raw_data/synthetic_eval.csv", parse_dates=["timestamp"])
truth = json.loads((HERE / "out/synthetic_truth.json").read_text())

# ---- fleet PR reference from the 5 REAL inverters (same sky for synthetic) ----
real = pd.read_csv(BASE / "raw_data/inverters_first5_2023.csv",
                   sep=";", decimal=",", parse_dates=["timestamp"],
                   date_format="%Y.%m.%d %H:%M").groupby("timestamp").mean(numeric_only=True)
KWPS = {1: 30.6, 2: 30.6, 3: 30.6, 4: 24.48, 5: 30.6}
h_poa = (real["Plant / Irradiation_average (W/m²)"].clip(lower=0) * 5 / 60 / 1000).resample("D").sum()
fleet_pr = pd.DataFrame({
    i: (real[f"INV 01.01.00{i} / P_AC (kW)"].fillna(0) * 5 / 60).resample("D").sum() / (h_poa * KWPS[i])
    for i in KWPS
})
fleet_pr = fleet_pr.where(h_poa > 0.5)          # PR is noise on dark days
fleet_median = fleet_pr.median(axis=1)


def daily_pr(s: pd.DataFrame) -> pd.Series:
    e = (s.p_ac * 5 / 60).resample("D").sum()
    h = (s.irr.clip(lower=0) * 5 / 60 / 1000).resample("D").sum()
    return (e / (h * KWP)).where(h > 0.5)


def classify(s: pd.DataFrame, win: pd.DatetimeIndex, ref: pd.DataFrame,
             dev: pd.Series) -> str:
    """Stage 2 on a flagged window. ref = pre-fault healthy slice of same scenario.
    dev = daily fleet-median deviation (weather/season already cancelled)."""
    w = s[s.index.normalize().isin(win)]
    sun = w[w.irr > 100]
    if len(sun) == 0:
        return "weather"
    # 1. external signal?
    if ((sun.dv < 100) | (sun.evu < 100)).mean() > 0.2:
        return "curtailment"
    # 2. dead vs degraded
    refsun = ref[ref.irr > 150]
    if (sun[sun.irr > 150].p_ac < 0.02 * KWP).mean() > 0.9:
        return "outage_fault"
    # 3. snow: cold + deficit concentrated in mornings, winter
    if win[0].month in (11, 12, 1, 2, 3) and sun.t_amb.mean() < 2.0:
        morning = sun[sun.index.hour < 11]
        if len(morning) and (morning.p_ac < 0.02 * KWP).mean() > 0.6:
            return "snow"
    # 4. AC or DC side?  eta = P_AC / (U_DC * I_DC), window vs healthy reference
    def eta(x):
        ok = x[(x.i_dc > 1) & (x.u_dc > 100) & (x.p_ac > 0.3)]
        return float((ok.p_ac * 1000 / (ok.u_dc * ok.i_dc)).median()) if len(ok) else np.nan
    e_now, e_ref = eta(sun), eta(refsun)
    if e_ref and e_now and e_now / e_ref < 0.93:
        return "ac_fault"
    # 5. DC side: step vs drift on fleet-normalized deviation (season cancels)
    # drift (soiling): deviation was already creeping up in the weeks BEFORE the
    # flag fired; step (string out): deviation jumps from ~0 to full within days
    pre_dev = dev[(dev.index < win[0]) &
                  (dev.index >= win[0] - pd.Timedelta(days=25))].dropna()
    creep = pre_dev.tail(14).mean() if len(pre_dev) >= 5 else 0.0
    if creep > 0.03:
        return "soiling"
    dwin = dev[dev.index.isin(win)].dropna()
    if len(dwin) >= 3 and dwin.head(5).mean() > 0.06:
        return "string_fault"
    return "dc_fault"


print(f"{'scenario':13} {'truth':13} {'flagged':>8} {'predicted':13} {'ok'}")
score = 0
for name, t in truth.items():
    s = syn[syn.scenario == name].set_index("timestamp")
    pr = daily_pr(s)
    dev = (fleet_median - pr).dropna()
    # Stage 1: persistent moderate deviation OR acute single-day deviation
    persistent = (dev > 0.09).rolling(3).sum() >= 3
    acute = dev > 0.22
    flagged = dev.index[(persistent | acute)]
    inj_start = pd.Timestamp(t["start"])
    win = flagged[(flagged >= inj_start) & (flagged <= pd.Timestamp(t["end"]) + pd.Timedelta(days=3))]
    if len(win) == 0:
        print(f"{name:13} {t['label']:13} {'MISSED':>8}")
        continue
    ref = s[s.index < inj_start - pd.Timedelta(days=2)]
    pred = classify(s, win, ref, dev)
    ok = pred == t["label"]
    score += ok
    print(f"{name:13} {t['label']:13} {len(win):>5} d  {pred:13} {'PASS' if ok else 'FAIL'}")

print(f"\nscore: {score}/6")
