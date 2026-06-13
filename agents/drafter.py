"""Action Drafter agent — turns a classified, priced incident into a
human-approvable draft following SolarPower Europe O&M Guidelines v6.0.

Draft types:
  fault        -> Corrective Maintenance work order
  curtailment  -> §15 EEG regulatory compensation claim
  grid_support -> Reactive-power setpoint RECOMMENDATION

Nothing is ever sent or written back automatically — drafts terminate at the
approval queue (audit-logged in the app). Hard rule #1 stays intact.
"""

import pandas as pd

_HINTS = {"fault": ["Strangausfall", "Kondensatoren", "Störung", "Isolationswerte"],
          "curtailment": ["Curtailment", "Netzstörung"]}

_PARTS = {"string_fault": "Bring string fuses + MC4 connectors.",
          "ac_fault": "Bring DC-link capacitor kit; check fans/cooling.",
          "outage": "Check AC breaker, comms, inverter error display first.",
          "soiling": "Schedule panel wash; no urgent dispatch.",
          "dc_fault": "Inspect strings and combiner box."}

_SUBJECTS = {
    "outage":          "Total inverter outage — zero AC output with irradiation present",
    "string_fault":    "String fault — I_DC step drop ~20% with U_DC stable",
    "ac_fault":        "AC-side conversion fault — low efficiency η = P_AC / P_DC",
    "soiling":         "Soiling / panel degradation — gradual PR decline over weeks",
    "dc_fault":        "DC-side fault — combiner box or string anomaly",
    "curtailment":     "Grid curtailment — DV/EVU < 100 %, §15 EEG claim applicable",
    "snow":            "Snow coverage — weather event, no dispatch required",
    "plant_wide":      "Plant-wide PR drop — all inverters affected simultaneously",
    "reactive_power":  "Low power factor — cos φ below grid-code threshold (< 0.90)",
    "dc_fault":        "DC-side fault — combiner box or string anomaly",
}

_AVAILABILITY = {
    "outage":         "Fully offline",
    "string_fault":   "Partially degraded (~80% capacity)",
    "ac_fault":       "Partially degraded (AC conversion loss)",
    "soiling":        "Partially degraded (soiling loss, varying)",
    "dc_fault":       "Partially degraded (DC-side)",
    "curtailment":    "Curtailed by grid operator",
    "snow":           "Temporarily offline — weather",
    "plant_wide":     "Degraded — plant-wide event",
    "reactive_power": "Online — reactive power deviation",
}

_TICKET_TYPE = {
    "dispatch":  "Corrective Maintenance (Breakdown)",
    "schedule":  "Corrective Maintenance (Scheduled)",
    "claim":     "Regulatory Claim (§15 EEG 2023)",
    "monitor":   "Preventive Maintenance — Monitor",
    "setpoint":  "Preventive Maintenance — Setpoint",
    "log":       "Compliance Log",
}

_ASSIGNED = {
    "dispatch":  "O&M Field Team — pending dispatch",
    "schedule":  "O&M Field Team — next planned visit",
    "claim":     "Regulatory / Grid Relations Dept.",
    "monitor":   "Remote Monitoring Team",
    "setpoint":  "Plant Operator — setpoint review",
    "log":       "Compliance Officer",
}

_STATUS_LABEL = {
    "pending":  "Acknowledged",
    "approved": "Dispatched",
    "rejected": "Closed — rejected",
}


def _priority(subtype: str, eur_impact: float, days: int) -> str:
    eur_day = eur_impact / max(days, 1)
    if subtype in ("outage", "ac_fault") or eur_day > 150:
        return "Critical"
    if subtype in ("string_fault",) or eur_day > 50:
        return "High"
    if subtype in ("soiling", "dc_fault") or eur_day > 15:
        return "Medium"
    return "Low"


def matching_tickets(tickets: pd.DataFrame, kind: str, inverter: str) -> list[str]:
    if kind not in _HINTS:
        return []
    m = tickets[tickets.category.fillna("").str.contains("|".join(_HINTS[kind]))]
    own = m[m.component == inverter]
    m = pd.concat([own, m[m.component != inverter]])
    return [f"{r.start.date()} — {r.component}: {r.category}" for r in m.head(3).itertuples()]


def draft(inc: dict, inverter: str, kwp: float, fit_eur_kwh: float,
          tickets: pd.DataFrame) -> dict:
    """Renders the O&M ticket. Returns both a legacy 'draft' string (backward compat)
    and structured fields for each of the 5 SolarPower Europe O&M sections."""
    s, e = inc["start"].date(), inc["end"].date()
    pr_line = f"PR {inc['pr_inverter']} vs fleet {inc['pr_fleet']}"
    r = inc.get("route", {})
    route, why = r.get("route", "dispatch"), r.get("note", "")
    eur_day = inc["eur_impact"] / max(inc["days"], 1)
    subtype = inc.get("subtype", "")
    kind = "curtailment" if route == "claim" else "fault"

    # ── legacy draft string (used by compliance agent + fallback UI) ─────────
    if route == "claim":
        text = (f"EEG COMPENSATION CLAIM (draft)\nInverter: {inverter} | {s} – {e}\n"
                f"{pr_line}. {inc['reasoning']}\n"
                f"Recoverable: {inc['lost_kwh']:.0f} kWh @ {fit_eur_kwh*100:.1f} ct/kWh "
                f"= EUR {inc['eur_impact']}\n"
                f"Action: file §15 EEG compensation with grid operator. {why}")
    elif route == "monitor":
        text = (f"MONITOR (no dispatch)\nInverter: {inverter} | {s} – {e} | {subtype}\n"
                f"{pr_line}. {inc['reasoning']}\n"
                f"Burn if ongoing: EUR {eur_day:.0f}/day. {why}\n"
                f"Action: automatic re-check in 3 days; escalate to dispatch if deficit persists.")
    elif route == "schedule":
        text = (f"SCHEDULED MAINTENANCE (next planned visit)\nInverter: {inverter} | "
                f"{s} – {e} | {subtype}\n{pr_line}. {inc['reasoning']}\n"
                f"Loss to date: {inc['lost_kwh']:.0f} kWh = EUR {inc['eur_impact']}. {why}\n"
                f"{_PARTS.get(subtype, '')}")
    elif route == "log":
        text = (f"COMPLIANCE LOG (no action)\nInverter: {inverter} | {s} – {e} | "
                f"{subtype}\n{pr_line}. {inc['reasoning']}\n"
                f"Recorded for availability/BNetzA reporting. {why}")
    else:  # dispatch
        text = (f"WORK ORDER (dispatch)\nInverter: {inverter} ({kwp} kWp) | {s} – {e} "
                f"({inc['days']} d) | {subtype}\n"
                f"{pr_line}. {inc['reasoning']}\n"
                f"Burning: EUR {eur_day:.0f}/day (EUR {inc['eur_impact']} so far "
                f"@ {fit_eur_kwh*100:.1f} ct/kWh). {why}\n"
                f"{_PARTS.get(subtype, '')}")

    # ── structured O&M ticket fields (SolarPower Europe v6.0) ────────────────
    # Section 1: Header & General
    plant_block = inverter.split("INV ")[1].rsplit(".", 1)[0] if "INV " in inverter else "01"

    # Section 2: Classification & Priority
    prio = _priority(subtype, inc["eur_impact"], inc["days"])

    return {
        "draft": text,
        "similar_tickets": matching_tickets(tickets, kind, inverter),
        # -- §1 Header & General Information --
        "plant_name":         f"Enerparc Solar Plant · Block {plant_block}",
        "plant_capacity_mw":  1.897,
        "location":           "Bavaria, Germany  (48°N, 11°E)",
        "reporter":           "beyondWatt SCADA Monitor (automated)",
        # -- §2 Classification & Priority --
        "asset_category":     "Inverter",
        "ticket_type":        _TICKET_TYPE.get(route, "Corrective Maintenance (Breakdown)"),
        "priority":           prio,
        # -- §3 Event Details --
        "failure_timestamp":  f"{s} 00:00 UTC",
        "status_label":       _STATUS_LABEL.get("pending", "Acknowledged"),
        "subject":            _SUBJECTS.get(subtype, f"{subtype} — performance deviation detected"),
        # -- §4 Financial & Production Impact --
        "availability":       _AVAILABILITY.get(subtype, "Partially degraded"),
        # -- §5 Resolution & Execution --
        "assigned_to":        _ASSIGNED.get(route, "O&M Field Team"),
        "resolution_notes":   "",
        "resolved_at":        None,
        "sign_off":           None,
        "parts_hint":         _PARTS.get(subtype, ""),
    }


def reactive_power(inverter: str, frame: pd.DataFrame, kwp: float) -> dict | None:
    """Grid-support scenario: sustained low cos-phi during feed-in."""
    lowpf = frame[(frame.p_ac > kwp * 0.3) & (frame.cosphi < 0.90)]
    if len(lowpf) <= 12:
        return None
    return {
        "id": f"{inverter}-Q", "inverter": inverter,
        "start": str(lowpf.index.min().date()), "end": str(lowpf.index.max().date()),
        "days": int((lowpf.index.max() - lowpf.index.min()).days) + 1,
        "classification": "grid_support", "subtype": "reactive_power",
        "severity": "low", "lost_kwh": 0.0, "eur_impact": 0.0, "similar_tickets": [],
        "route": {"route": "setpoint", "confidence": 0.9, "by": "rules-direct",
                  "note": "Grid-code compliance — draft the Q-setpoint for approval."},
        "reasoning": f"cos φ below 0.90 during {len(lowpf)} feed-in intervals.",
        "draft": (f"REACTIVE POWER SETPOINT (recommendation — requires approval)\n"
                  f"Inverter: {inverter}\nObservation: cos φ below 0.90 during "
                  f"{len(lowpf)} feed-in intervals.\n"
                  f"Recommend: Q-mode cos φ = 0.95 inductive for voltage support.\n"
                  f"NO automatic write-back — command payload is generated only "
                  f"after human approval."),
        "status": "pending",
        # O&M ticket fields
        "plant_name": f"Enerparc Solar Plant · Block {inverter.split('INV ')[1].rsplit('.',1)[0] if 'INV ' in inverter else '01'}",
        "plant_capacity_mw": 1.897,
        "location": "Bavaria, Germany  (48°N, 11°E)",
        "reporter": "beyondWatt SCADA Monitor (automated)",
        "asset_category": "Inverter",
        "ticket_type": "Preventive Maintenance — Setpoint",
        "priority": "Low",
        "failure_timestamp": str(lowpf.index.min().date()) + " 00:00 UTC",
        "status_label": "Acknowledged",
        "subject": _SUBJECTS["reactive_power"],
        "availability": _AVAILABILITY["reactive_power"],
        "assigned_to": _ASSIGNED["setpoint"],
        "resolution_notes": "", "resolved_at": None, "sign_off": None,
        "parts_hint": "",
    }
