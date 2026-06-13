"""RAG Compliance agent — retrieves regulation snippets and enriches action drafts
with exact citations.

Corpus is embedded in-context (tiny: 7 regulation chunks < 2 KB).  No vector DB
needed at hackathon scale — stuff-in-context beats retrieval latency here.

Called by: drafter.py (enrich) and orchestrator.py (claim notes).
LLM backend: same gateway as orchestrator (Bedrock → Anthropic → rules fallback).

Compliance scope (Germany / EU, 2026):
  EEG 2023 §15        — curtailment compensation claims
  BNetzA MaStR        — availability & fault reporting obligations
  IEC 61724-1:2021    — PR definition, measurement intervals, uncertainty
  Redispatch 2.0      — §14 EnWG grid-operator curtailment remuneration
  §14a EnWG           — controllable consumption / smart-meter dimming rules
  EU AI Act Art.6/9   — advisory-only posture, high-risk classification boundary
  NIS2 / KRITIS       — incident logging, 24 h notification threshold
"""

import importlib.util
import sys
from pathlib import Path

_PROTO = Path.home() / "Documents/Projects/Re-new/prototype"

# ── in-context compliance corpus ──────────────────────────────────────────────
_CORPUS: dict[str, str] = {
    "EEG_15": (
        "EEG 2023 §15 Entschädigung bei Abregelung (curtailment compensation): "
        "Grid operators must financially compensate renewable plant operators for "
        "feed-in energy curtailed under §13 or §14 EnWG. Compensation = 95% of "
        "the lost revenue (actual production shortfall × applicable feed-in tariff). "
        "Operators must document: (a) curtailment signal timestamps (DV or EVU "
        "signal < 100%), (b) irradiation data confirming producible energy, "
        "(c) inverter output before and during curtailment. Claims must be filed "
        "within 12 months of the curtailment event with the responsible grid "
        "operator (Netzbetreiber). Disputes go to BNetzA arbitration."
    ),
    "REDISPATCH2": (
        "Redispatch 2.0 (§13 / §14 EnWG, BNetzA Festlegung REGENT): Since "
        "October 2021 all plants ≥ 100 kW (and since 2023 all plants ≥ 7 kW with "
        "smart meter) must participate in automated redispatch. Grid operator sends "
        "set-point signals; plant must comply within ramp-time spec. Operator "
        "receives market-value compensation (Marktwert) for curtailed energy — NOT "
        "the feed-in tariff. beyondWatt must log every redispatch event: start/end "
        "timestamp, requested set-point, actual output, and signal origin (SMGW or "
        "direct RTU). Non-compliance risk: BNetzA fine up to EUR 100,000."
    ),
    "PARA14A_ENWG": (
        "§14a EnWG — Steuerbare Verbrauchseinrichtungen: Applies to grid-connected "
        "storage and controllable loads. Grid operator may temporarily dim (dimmen) "
        "charging power to max 4.2 kW per asset. Plant must not interpret §14a "
        "dimming as an equipment fault — classify separately. Storage operator "
        "receives a reduced grid fee (Netzentgeltreduzierung) as compensation. "
        "beyondWatt anomaly engine must check DV/EVU signal source: §14a dimming "
        "arrives via SMGW with specific command code, distinct from §13 redispatch."
    ),
    "IEC61724": (
        "IEC 61724-1:2021 — Photovoltaic system performance monitoring: "
        "Performance Ratio (PR) = E_AC / (H_poa / G_STC × P_nom). "
        "H_poa = in-plane irradiation [kWh/m²], G_STC = 1000 W/m², P_nom = nameplate. "
        "Standard mandates: minimum 1-min measurement intervals for Class A, "
        "10-min acceptable for Class B (utility scale). PR < 0.70 sustained over "
        "> 3 days warrants formal investigation. Temperature correction: "
        "PR_T = PR / (1 + γ×(T_cell − 25)), γ ≈ −0.004/°C for monocrystalline Si. "
        "Measurement uncertainty must be stated in reports; IEC 61724-3 covers "
        "energy assessment for bankability."
    ),
    "BNETZ_MASTR": (
        "BNetzA MaStR (Marktstammdatenregister) reporting obligations: All plants "
        "must report: (1) annual yield within 90 days of year-end; (2) outages "
        "> 72 h within 30 days of restoration; (3) permanent decommissioning "
        "within 4 weeks. beyondWatt dispatch work orders must note MaStR unit ID "
        "on the fault log. Failure to report: BNetzA may suspend EEG payment. "
        "Data fields: MaStR-Einheit-Nr, Betriebsstatus, Jahresarbeit [MWh]."
    ),
    "EU_AI_ACT": (
        "EU AI Act (Regulation 2024/1689) Art. 6 / Annex III: AI systems used in "
        "critical infrastructure (energy grid management) are HIGH-RISK and require: "
        "conformity assessment, CE marking, human oversight, audit trail, and "
        "registration in EU database. beyondWatt avoids high-risk classification by "
        "maintaining ADVISORY-ONLY posture — all outbound actions require human "
        "approval before execution. The system must log: model ID, confidence score, "
        "human decision, and timestamp for every recommendation. Autonomous control "
        "of grid-connected assets without human-in-the-loop would trigger Art.9 "
        "risk management obligations and potential market withdrawal."
    ),
    "NIS2_KRITIS": (
        "NIS2 Directive (EU 2022/2555) & German KRITIS-DachG: Energy operators "
        "above thresholds (≥ 200 employees OR ≥ EUR 10M turnover in energy sector) "
        "are 'Important Entities'. Obligations: (1) report significant incidents to "
        "BSI within 24 hours (early warning) and 72 hours (full report); "
        "(2) implement security measures for OT/SCADA systems; "
        "(3) supply chain risk management. beyondWatt audit.jsonl satisfies the "
        "logging requirement. Significant incident threshold: plant outage > 1 MW "
        "for > 1 hour. Fine for non-compliance: up to EUR 10M or 2% global turnover."
    ),
}

# ── route → which corpus chunks are relevant ─────────────────────────────────
_ROUTE_CHUNKS: dict[str, list[str]] = {
    "claim":    ["EEG_15", "REDISPATCH2", "BNETZ_MASTR"],
    "dispatch": ["IEC61724", "BNETZ_MASTR", "NIS2_KRITIS"],
    "log":      ["BNETZ_MASTR", "NIS2_KRITIS", "EU_AI_ACT"],
    "monitor":  ["IEC61724", "EU_AI_ACT"],
    "schedule": ["IEC61724", "BNETZ_MASTR"],
    "setpoint": ["PARA14A_ENWG", "REDISPATCH2", "EU_AI_ACT"],
}

_SUBTYPE_CHUNKS: dict[str, list[str]] = {
    "curtailment": ["EEG_15", "REDISPATCH2"],
    "plant_wide":  ["REDISPATCH2", "NIS2_KRITIS"],
    "outage":      ["BNETZ_MASTR", "NIS2_KRITIS"],
    "reactive_power": ["PARA14A_ENWG"],
}

_RAG_SYSTEM = """You are a German solar O&M compliance advisor. Given an incident summary
and exact regulation excerpts, write ONE short paragraph (3-4 sentences) that:
1. Names the specific regulation (§ number or standard) that applies.
2. States what the operator MUST do (deadline, data to log, who to notify).
3. Gives the financial or legal consequence of inaction.
Be factual, cite only the provided text — no parametric knowledge.
Reply in English. Do not repeat the incident description."""


def _load_llm():
    spec = importlib.util.spec_from_file_location("bw_llm", _PROTO / "graph/llm.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("bw_llm", mod)
    spec.loader.exec_module(mod)
    return mod


try:
    _llm = _load_llm()
except Exception:
    _llm = None


def retrieve(route: str, subtype: str) -> str:
    """Return the relevant regulation chunks as a single string (retrieval step)."""
    keys = set(_ROUTE_CHUNKS.get(route, [])) | set(_SUBTYPE_CHUNKS.get(subtype, []))
    if not keys:
        keys = {"IEC61724", "EU_AI_ACT"}
    return "\n\n---\n\n".join(_CORPUS[k] for k in keys if k in _CORPUS)


def enrich(inc: dict) -> str:
    """Generate a compliance note grounded in the retrieved regulation text.

    Returns a plain-text paragraph to append to the draft.  Falls back to a
    static citation string when the LLM is unavailable (demo-safe).
    """
    route   = inc.get("route", {}).get("route", "log")
    subtype = inc.get("subtype", "")
    context = retrieve(route, subtype)

    fallback = _static_note(route, subtype)

    if _llm is None or _llm.backend_name() == "mock":
        return fallback

    user_msg = (
        f"Incident: inverter {inc.get('inverter','?')}, subtype={subtype}, "
        f"route={route}, EUR impact={inc.get('eur_impact', 0):.0f}, "
        f"days={inc.get('days', 1)}.\n\n"
        f"Regulation excerpts:\n{context}"
    )
    try:
        note = _llm.call_llm(_RAG_SYSTEM, user_msg, max_tokens=220).strip()
        return note if len(note) > 40 else fallback
    except Exception:
        return fallback


def _static_note(route: str, subtype: str) -> str:
    """Rule-based citation — shown when LLM is unavailable."""
    if route == "claim" or subtype == "curtailment":
        return (
            "Compliance note [EEG §15]: File compensation claim with grid operator "
            "within 12 months. Attach DV/EVU signal log + irradiation data. "
            "Recoverable: 95% of lost FiT revenue."
        )
    if subtype == "outage":
        return (
            "Compliance note [BNetzA MaStR + NIS2]: Log outage against MaStR unit ID. "
            "If > 72 h, file restoration report within 30 days. "
            "If > 1 MW for > 1 h, BSI early warning within 24 h (NIS2)."
        )
    if subtype == "reactive_power":
        return (
            "Compliance note [§14a EnWG + Redispatch 2.0]: Q-setpoint changes must "
            "be approved by human operator before write-back. Log command payload, "
            "timestamp, and approver ID in audit.jsonl (EU AI Act requirement)."
        )
    if route == "dispatch":
        return (
            "Compliance note [IEC 61724-1 + BNetzA MaStR]: Document PR evidence and "
            "fault classification method. Include in annual yield report. "
            "Outages > 72 h require MaStR status update."
        )
    return (
        "Compliance note [IEC 61724-1 + EU AI Act]: All recommendations are advisory. "
        "Log model confidence, human decision, and timestamp per EU AI Act audit trail "
        "requirements. Annual PR report due within 90 days of year-end (BNetzA MaStR)."
    )


def annotate_draft(inc: dict) -> dict:
    """Add a 'compliance_note' key to the incident dict in-place. Returns inc."""
    inc["compliance_note"] = enrich(inc)
    return inc
