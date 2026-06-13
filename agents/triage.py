"""Triage agent — PR cascade (IEC 61724 pattern), proven 6/6 on injected faults.

Stage 1 (screen): daily PR per inverter; flag on
  - persistent deviation from fleet median (> 0.09 for >= 3 consecutive days)
  - acute deviation (> 0.22 single day)
Stage 2 (drill-down, cost order): curtailment -> outage -> snow -> AC/DC split
  (efficiency eta) -> creep (soiling) vs step (string out).

Input: per-inverter frame with columns p_ac, i_dc, u_dc, dv, evu, irr, t_mod, t_amb
Output: list of windows with classification, subtype and reasoning sentence.
"""

import pandas as pd

PERSIST_DEV = 0.09   # fleet-median deviation, sustained
PERSIST_DAYS = 3
ACUTE_DEV = 0.22     # single-day deviation
MIN_HPOA = 0.5       # kWh/m2 — below this daily PR is noise


def daily_pr(frame: pd.DataFrame, kwp: float) -> pd.Series:
    e = (frame.p_ac * 5 / 60).resample("D").sum()
    h = (frame.irr.clip(lower=0) * 5 / 60 / 1000).resample("D").sum()
    return (e / (h * kwp)).where(h > MIN_HPOA)


def stage1_flags(pr: pd.Series, fleet_median: pd.Series,
                 healthy_mask: pd.Series) -> tuple[pd.Series, pd.DatetimeIndex]:
    """Two triggers (doc §3): relative-to-fleet (machine problems — weather
    cancels) and absolute floor (catches fleet-wide events like curtailment,
    which relative deviation is blind to). Floor derived from the inverter's
    own healthy-day distribution: median - 3*MAD."""
    dev = (fleet_median - pr).dropna()
    persistent = (dev > PERSIST_DEV).rolling(PERSIST_DAYS).sum() >= PERSIST_DAYS
    acute = dev > ACUTE_DEV
    healthy_pr = pr[healthy_mask.reindex(pr.index, fill_value=False)].dropna()
    floor = healthy_pr.median() - 3 * (healthy_pr - healthy_pr.median()).abs().median()
    breach = pr.dropna() < floor
    days = dev.index[persistent | acute].union(breach.index[breach])
    return dev, days


def group_windows(days, max_gap_days: int = 2):
    windows, cur = [], None
    for d in sorted(days):
        if cur and (d - cur[-1]).days <= max_gap_days:
            cur.append(d)
        else:
            if cur:
                windows.append(cur)
            cur = [d]
    if cur:
        windows.append(cur)
    return windows


def _eta(x: pd.DataFrame) -> float | None:
    ok = x[(x.i_dc > 1) & (x.u_dc > 100) & (x.p_ac > 0.3)]
    if not len(ok):
        return None
    return float((ok.p_ac * 1000 / (ok.u_dc * ok.i_dc)).median())


def stage2_classify(frame: pd.DataFrame, win: list, dev: pd.Series,
                    kwp: float, fleet_pr: pd.DataFrame | None = None) -> tuple[str, str, str]:
    """Returns (classification, subtype, reasoning). classification is the
    dashboard category: fault | curtailment | weather."""
    w = frame[frame.index.normalize().isin(win)]
    sun = w[w.irr > 100]
    if len(sun) == 0:
        return "weather", "no_sun", "No usable irradiation in window — not assessable."

    # 1. external signal?
    if ((sun.dv < 100) | (sun.evu < 100)).mean() > 0.2:
        return ("curtailment", "curtailment",
                "DV/EVU setpoint below 100 % during the deficit — externally commanded, "
                "not an equipment fault. No truck roll; file compensation claim.")

    # 2. fleet-wide? (everyone down together = weather/plant-level)
    if fleet_pr is not None:
        fwin = fleet_pr.loc[fleet_pr.index.isin(win)]
        if len(fwin) and (fwin.median(axis=1) < 0.25).mean() > 0.8:
            return ("weather", "plant_wide",
                    "All inverters collapsed together — plant-level event "
                    "(grid, trafo or snow), not this machine.")

    # 3. dead or degraded?
    bright = sun[sun.irr > 150]
    if len(bright) and (bright.p_ac < 0.02 * kwp).mean() > 0.9:
        return ("fault", "outage",
                "Output at zero while irradiation is healthy and no curtailment "
                "signal is active — total outage.")

    # 4. snow: winter, freezing, mornings dead
    if win[0].month in (11, 12, 1, 2, 3) and sun.t_amb.mean() < 2.0:
        morning = sun[sun.index.hour < 11]
        if len(morning) and (morning.p_ac < 0.02 * kwp).mean() > 0.6:
            return ("weather", "snow",
                    "Freezing ambient, production dead in the morning and recovering "
                    "with the sun — snow cover, clears itself. No action.")

    # 5. AC or DC side? eta = P_AC / (U_DC x I_DC) vs pre-fault reference
    ref = frame[frame.index < win[0] - pd.Timedelta(days=2)]
    e_now, e_ref = _eta(sun), _eta(ref[ref.irr > 150])
    if e_ref and e_now and e_now / e_ref < 0.93:
        return ("fault", "ac_fault",
                f"DC input normal but conversion efficiency dropped to "
                f"{e_now/e_ref:.0%} of reference — AC-side fault (capacitors, "
                f"cooling). Matches 'Kondensatoren defekt' precedent.")

    # 6. DC side: creep (soiling) vs step (string out)
    pre_dev = dev[(dev.index < win[0]) &
                  (dev.index >= win[0] - pd.Timedelta(days=25))].dropna()
    creep = pre_dev.tail(14).mean() if len(pre_dev) >= 5 else 0.0
    if creep > 0.03:
        return ("fault", "soiling",
                "Deficit grew gradually over weeks before crossing the alarm "
                "threshold — soiling or panel degradation. Schedule a wash, "
                "no urgent dispatch.")
    dwin = dev[dev.index.isin(win)].dropna()
    if len(dwin) >= 3 and dwin.head(5).mean() > 0.06:
        return ("fault", "string_fault",
                "DC current stepped down abruptly with voltage stable — one or "
                "more strings out (Strangausfall). Bring string fuses/connectors.")
    return ("fault", "dc_fault",
            "DC-side underperformance, pattern inconclusive — inspect strings "
            "and combiner box.")


def run(frame: pd.DataFrame, kwp: float, fleet_median: pd.Series,
        fleet_pr: pd.DataFrame | None = None) -> list[dict]:
    """Full cascade for one inverter. Returns raw incident windows."""
    pr = daily_pr(frame, kwp)
    curtailed = ((frame.dv.resample("D").min() < 100)
                 | (frame.evu.resample("D").min() < 100))
    dev, flagged = stage1_flags(pr, fleet_median, ~curtailed)
    out = []
    for win in group_windows(flagged):
        cls, subtype, reasoning = stage2_classify(frame, win, dev, kwp, fleet_pr)
        out.append({
            "start": win[0], "end": win[-1],
            "days": (win[-1] - win[0]).days + 1,
            "classification": cls, "subtype": subtype, "reasoning": reasoning,
            "pr_inverter": round(float(pr[pr.index.isin(win)].mean()), 3),
            "pr_fleet": round(float(fleet_median[fleet_median.index.isin(win)].mean()), 3),
        })
    return out
