"""Impact agent — prices a triage window in euros.

lost_kwh = sum(expected - actual) over the window's sun hours
eur      = lost_kwh x that inverter's real feed-in tariff
Expected comes from the physics baseline (irr x kWp x temp-derate, calibrated);
teammates' per-inverter ML models plug in via the same `expected` series.
"""

import pandas as pd


def physics_expected(frame: pd.DataFrame, kwp: float) -> pd.Series:
    """Calibrated physics baseline P_exp(t) in kW."""
    raw = kwp * (frame.irr / 1000) * (1 - 0.004 * (frame.t_mod.fillna(25) - 25))
    healthy = (frame.irr > 300) & (frame.p_ac > kwp * 0.02)
    pf = float((frame.p_ac[healthy] / raw[healthy]).median())
    return (raw * pf).clip(lower=0), pf


def price(frame: pd.DataFrame, expected: pd.Series, win_start, win_end,
          fit_eur_kwh: float) -> dict:
    w = frame[win_start:win_end + pd.Timedelta(days=1)]
    sun = w[w.irr > 100]
    lost_kwh = float((expected[sun.index] - sun.p_ac).clip(lower=0).sum() * 5 / 60)
    eur = round(lost_kwh * fit_eur_kwh, 2)
    return {"lost_kwh": round(lost_kwh, 1), "eur_impact": eur,
            "severity": "high" if eur > 50 else "low"}
