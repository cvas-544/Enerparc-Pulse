"""Orchestrator — sits BETWEEN the math and the actions (supervisor pattern).

After Triage classifies and Impact prices a window, the orchestrator decides
WHICH action path to trigger:

  dispatch  -> work order (stop an ongoing EUR/day burn)
  claim     -> §15 EEG compensation (recover curtailment money)
  setpoint  -> reactive-power recommendation (grid-code compliance)
  schedule  -> non-urgent maintenance (wash) on next planned visit
  monitor   -> re-check in N days (avoid a ~EUR 400 truck roll on weak evidence)
  log       -> compliance record only (weather / plant-wide)

Two-tier routing: DIRECT evidence routes deterministically (no LLM — fast,
reproducible). AMBIGUOUS evidence (inconclusive dc_fault, conflicting signals)
goes to the LLM, which weighs EUR/day burn vs truck-roll cost and explains.

The orchestrator decides the path; it never approves — every route still
terminates at the human queue. LLM backend: Bedrock -> Anthropic -> rules.
"""

import importlib.util
import json
import sys
from pathlib import Path

_PROTO = Path.home() / "Documents/Projects/Re-new/prototype"
TRUCK_ROLL_EUR = 400  # assumed cost of one technician dispatch (label as assumption)


def _load_llm():
    spec = importlib.util.spec_from_file_location("bw_llm", _PROTO / "graph/llm.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["bw_llm"] = mod
    spec.loader.exec_module(mod)
    return mod


try:
    _llm = _load_llm()
except Exception:
    _llm = None


def _llm_ready() -> bool:
    return _llm is not None and _llm.backend_name() != "mock"


# ---- tier 1: deterministic routes for direct evidence --------------------
_DIRECT = {
    "curtailment": ("claim", 0.95, "DV/EVU signal is direct evidence — recover the money, no truck."),
    "outage": ("dispatch", 0.95, "Dead inverter with healthy sun — every day costs money, go."),
    "string_fault": ("dispatch", 0.85, "Step signature is clear — bring string fuses."),
    "ac_fault": ("dispatch", 0.85, "Conversion efficiency drop is measurable — capacitor/cooling check."),
    "soiling": ("schedule", 0.75, "Gradual loss — a wash on the next planned visit beats an extra truck roll."),
    "snow": ("log", 0.9, "Clears itself; record for availability reporting."),
    "plant_wide": ("log", 0.9, "Plant-level event — record for compliance, not an inverter dispatch."),
    "no_sun": ("log", 0.9, "Not assessable — log only."),
    "reactive_power": ("setpoint", 0.9, "Grid-code compliance — draft the Q-setpoint for approval."),
}

_SYSTEM = """You are the O&M routing orchestrator for a German solar plant.
The math has already detected and priced an incident, but the evidence is
AMBIGUOUS. Decide the action route, weighing money: an unnecessary truck roll
costs ~EUR 400; an unfixed fault keeps burning eur_per_day; missed EEG
compensation claims expire. Routes: "dispatch" (send technician now),
"monitor" (re-check in 3 days), "schedule" (bundle with next planned visit),
"claim", "log". You only route — a human approves every action.
Reply ONLY JSON: {"route": "...", "confidence": 0.0-1.0,
"note": "<one sentence: the EUR trade-off that decided it>"}"""


def _llm_route(inc: dict, meta: dict) -> dict | None:
    if not _llm_ready():
        return None
    eur_day = round(inc.get("eur_impact", 0) / max(inc.get("days", 1), 1), 2)
    user = json.dumps({
        "incident": {k: inc[k] for k in
                     ("inverter", "start", "end", "days", "classification",
                      "subtype", "reasoning", "lost_kwh", "eur_impact") if k in inc},
        "eur_per_day_if_ongoing": eur_day,
        "truck_roll_cost_eur": TRUCK_ROLL_EUR,
        "inverter_meta": meta.get(inc.get("inverter", ""), {}),
    }, default=str)
    try:
        out = _llm.call_llm_json(_SYSTEM, user, max_tokens=200)
        if out.get("route") in ("dispatch", "monitor", "schedule", "claim", "log"):
            return {"route": out["route"],
                    "confidence": float(out.get("confidence", 0.5)),
                    "note": str(out.get("note", ""))[:220],
                    "by": _llm.backend_name()}
    except Exception:
        pass
    return None


def route(inc: dict, meta: dict) -> dict:
    """Tier-1 deterministic for direct evidence; tier-2 LLM for the ambiguous
    middle; rule fallback keeps the pipeline running without credentials."""
    sub = inc.get("subtype", "")
    if sub in _DIRECT:
        r, conf, note = _DIRECT[sub]
        return {"route": r, "confidence": conf, "note": note, "by": "rules-direct"}
    # ambiguous (dc_fault and anything unknown) -> LLM weighs the euros
    decided = _llm_route(inc, meta)
    if decided:
        return decided
    eur_day = inc.get("eur_impact", 0) / max(inc.get("days", 1), 1)
    if eur_day > TRUCK_ROLL_EUR / 10:  # pays back a truck roll within ~10 days
        return {"route": "dispatch", "confidence": 0.6,
                "note": f"Inconclusive, but EUR {eur_day:.0f}/day burn pays back "
                        f"a EUR {TRUCK_ROLL_EUR} truck roll within 10 days.",
                "by": "rules-fallback"}
    return {"route": "monitor", "confidence": 0.55,
            "note": f"Evidence inconclusive and burn rate low — re-check in 3 days, "
                    f"save the EUR {TRUCK_ROLL_EUR} truck roll.",
            "by": "rules-fallback"}


def rank(incidents: list[dict]) -> list[dict]:
    """Fleet priority by EUR burn — 'fix 002 first'."""
    ranked = sorted([i for i in incidents if i.get("eur_impact", 0) > 0],
                    key=lambda i: -i["eur_impact"])
    for r, inc in enumerate(ranked, 1):
        inc["fleet_rank"] = r
    return incidents


def briefing(incidents: list[dict], meta: dict) -> str:
    """Fleet summary: recoverable vs burning vs avoided-cost euros."""
    claim = sum(i["eur_impact"] for i in incidents if i.get("route") == "claim")
    burn = sum(i["eur_impact"] for i in incidents if i.get("route") == "dispatch")
    saved = TRUCK_ROLL_EUR * sum(1 for i in incidents if i.get("route") in ("monitor", "schedule"))
    fallback = (f"Fleet 2023: EUR {burn:.0f} fault losses routed to dispatch, "
                f"EUR {claim:.0f} curtailment recoverable via EEG claims, "
                f"~EUR {saved:.0f} truck rolls avoided by monitor/schedule routing. "
                f"{sum(1 for i in incidents if i.get('route')=='dispatch')} dispatches "
                f"pending human approval.")
    if not _llm_ready():
        return fallback
    try:
        return _llm.call_llm(
            "You write a 2-sentence O&M fleet briefing. EUR-first, factual: money "
            "recoverable (claims), money burning (dispatches), cost avoided (monitor/schedule).",
            json.dumps({"totals": {"claimable": claim, "burning": burn, "avoided": saved},
                        "incidents": [{k: i.get(k) for k in
                                       ("inverter", "subtype", "route", "eur_impact")}
                                      for i in incidents if i.get("eur_impact", 0) > 20]}),
            max_tokens=150).strip()
    except Exception:
        return fallback
