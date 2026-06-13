# beyondWatt — Demo Scenarios (Hackathon Pitch)

Judges see these 4 scenarios live in the Approval Queue at `:8080/draft`.
Each section shows: **what SCADA sends → what the system detects → what the draft says → what the operator does**.

---

## Scenario 1 — String Failure (Truck Roll)

### What SCADA sends
```
INV 01.01.002  |  2023-07-14 09:00–17:00
I_DC_SUM:  12.4 A  →  9.9 A   (−20 %, step change overnight)
U_DC:     590 V  →  591 V   (stable — DC voltage held)
P_AC:      17.8 kW →  14.2 kW  (−20 %, matching DC drop)
Irradiation: 820 W/m²           (sunny day, neighbors healthy)
DV/EVU:  100 %                  (no external curtailment)
```

### What the system detects
- **Stage 1 — PR screen fires:** PR(INV 002) = 0.61, fleet median = 0.79 → deviation 0.18 for 4 consecutive days → flag triggered
- **Stage 2 — root-cause drill-down:**
  - DV/EVU = 100 % → curtailment ruled out
  - Neighbors all PR ≈ 0.79 → not weather / plant-level
  - η (conversion efficiency) = 97 % → AC side healthy
  - I_DC step = −19.8 % with U_DC stable → **1 string out of 5 strings disconnected**
  - Tickets.xlsx match: `2021-03-11 Strangausfall INV 002 — fuse blown, string 3`

### Draft sent to Approval Queue
```
INCIDENT CARD  INV 01.01.002  #INC-2023-187
Classification : STRING FAULT (string 3)
Severity       : HIGH
Energy lost    : 88.4 kWh (14 days)
EUR impact     : €102.5 estimated (FiT 11.6 ct/kWh)
Precedent      : Ticket #2021-031 — same inverter, string fuse

RECOMMENDED ACTION
  → Dispatch field tech with 30 A string fuse + connector kit
  → Check string combiner box 3 for blown fuse / corroded MC4
  → On-site: measure string VOC before reconnect

COMPLIANCE NOTE (IEC 61724 / BNetzA MaStR)
  Outage > 8 h × kWp > 1 MW threshold crossed. MaStR 72-h reporting
  window starts 2023-07-14. Unit ID: DE-MaStR-XXXX. File via BNetzA portal.

[APPROVE]  [REJECT]  [DEFER 3 DAYS]
```

### After operator clicks APPROVE
- Work order payload generated and logged to `out/audit.jsonl`
- Incident status → "dispatched"
- Dashboard counter: Active Faults −1, Approved Actions +1

---

## Scenario 2 — Grid Curtailment (No Truck Roll, §15 EEG Claim)

### What SCADA sends
```
INV 01.01.001–005  |  2023-08-10 10:00–16:00
DRD11A / DV:   60 %   (demand-response signal from DSO)
DRD11A / EVU:  58 %
P_AC all 5 inv: ~40 % of expected output
Irradiation:   780 W/m²  (excellent solar day)
```

### What the system detects
- **Stage 1:** PR drops to 0.41 across all 5 inverters simultaneously
- **Stage 2, check 1:** DV = 60 % on 78 % of daytime intervals → **external curtailment**
- Fleet-wide flag → not an inverter fault; §14a EnWG / Redispatch 2.0 signal confirmed
- Lost generation calculated: 312 kWh across fleet × 2 days = 624 kWh = **€72.4**

### Draft sent to Approval Queue
```
INCIDENT CARD  PLANT-WIDE  #INC-2023-201
Classification : CURTAILMENT — §14a EnWG / Redispatch 2.0
Severity       : LOW (regulatory, not a fault)
Energy lost    : 624 kWh  |  EUR impact: €72.4 (FiT)
DV signal low  : 60 %  (10:00–16:00, 2023-08-10 & 11)

RECOMMENDED ACTION
  → NO truck roll — external grid signal, equipment healthy
  → File §15 EEG 2023 compensation claim
  → Claim value: 95 % × 624 kWh × 11.6 ct = €68.8
  → Deadline: 12-month filing window (by 2024-08-10)
  → Attach: DV signal log + Irradiation export as evidence

COMPLIANCE NOTE (§15 EEG 2023)
  Operator entitled to 95 % of foregone FiT for curtailment ordered
  by grid operator under §13 EnWG. Filing via EEG clearing portal.
  Retain SCADA export for ≥ 5 years (§ 100 EEG).

[APPROVE — FILE CLAIM]  [REJECT]
```

### After operator clicks APPROVE
- §15 claim draft PDF generated (placeholder)
- No field dispatch — truck roll cost saved: ~€400
- Audit trail: decision timestamped, DV evidence attached

---

## Scenario 3 — Chronic Soiling (Scheduled Maintenance)

### What SCADA sends
```
INV 01.01.004  |  2023-08-01 → 2023-10-31 (slow drift)
P_AC:   gradual decline, −15 % over 90 days
I_DC:   matching gradual decline (no step)
U_DC:   stable
Module temp vs P_AC: normal −0.4 %/°C correlation intact
Irradiation: varying normally
Tickets.xlsx: INV 004 — 4 prior incidents, "Verschmutzung" (soiling) noted 2021, 2022
```

### What the system detects
- **Stage 1:** PR deviation grows slowly — first fires at day 22 when 3-day mean > 0.09
- **Stage 2:**
  - DV = 100 % → not curtailment
  - η normal → AC side OK
  - I_DC drift = no step (string_fault ruled out)
  - Pre-flag deviation was already creeping 0.03/week → **soiling pattern confirmed**
  - 4 prior soiling tickets on INV 004 → chronic repeat offender flag

### Draft sent to Approval Queue
```
INCIDENT CARD  INV 01.01.004  #INC-2023-247
Classification : SOILING / PANEL DEGRADATION (drift pattern)
Severity       : MEDIUM
Drift detected : Aug 01 → Oct 31  |  PR drop: 0.79 → 0.67
Energy lost    : 310 kWh  |  EUR impact: €36.0
Prior tickets  : 4× Verschmutzung (2020–2022) — chronic soiling site

RECOMMENDED ACTION
  → Bundle with next planned maintenance (avoid €400 unplanned truck roll)
  → Schedule panel wash: Oct maintenance window
  → Post-wash PR target: ≥ 0.76 (restore to fleet median)
  → If post-wash PR < 0.72 after 2 clean days: open degradation investigation

COMPLIANCE NOTE (IEC 61724-1)
  Soiling loss > 0.5 %/yr triggers degradation clause in EPC performance
  guarantee. Document wash date + pre/post PR for contract evidence.

[APPROVE — SCHEDULE WASH]  [REJECT]  [ESCALATE TO ROOT-CAUSE VISIT]
```

---

## Scenario 4 — Reactive Power Setpoint (Advisory, Never Auto-Written)

### What SCADA sends
```
Janitza UMG 604 — DRD11A  |  2023-11-05 08:00–20:00
CosPhi_L1..L3:  0.87  (below grid code minimum 0.90)
P_AC_L1..L3:    42 kW (all inverters active)
S_AC:           48 kVA
DV/EVU:        100 %  (no curtailment)
```

### What the system detects
- cos φ = 0.87 for 3 consecutive daytime hours → below §9 EEG grid-code threshold (0.90 inductive/capacitive)
- No concurrent fault on any inverter → reactive power issue, not a generation fault
- Stage 2 skip: not a PR event, routed directly by orchestrator as reactive-power advisory

### Draft sent to Approval Queue
```
ADVISORY CARD  PLANT-WIDE  #ADV-2023-089
Classification : REACTIVE POWER — cos φ below grid code
Severity       : MEDIUM (grid-code compliance risk)
Observed       : cos φ = 0.87  |  Threshold: 0.90 (§9 EEG)
Duration       : 3 h 20 min, 2023-11-05

RECOMMENDED SETPOINT (advisory — NOT auto-sent)
  cos φ target: 0.95 (capacitive)
  Inverter setpoint command: { "cos_phi": 0.95, "mode": "capacitive" }
  Applies to: INV 001–005 via site SCADA bus
  Verification: re-read Janitza meter in 15 min after apply

⚠️  HUMAN APPROVAL REQUIRED — this setpoint payload is generated
    but NEVER written back autonomously. EU AI Act advisory posture.
    Approve to copy payload to clipboard / dispatch log only.

COMPLIANCE NOTE (§9 EEG 2023 / EU AI Act)
  Grid operators may mandate reactive power per §9 EEG at 250 kW threshold.
  Advisory system only — no autonomous grid interaction (EU AI Act Art.6,
  Annex III high-risk boundary maintained).

[APPROVE — COPY PAYLOAD]  [REJECT]
```

### After operator clicks APPROVE
- Setpoint JSON copied to clipboard / logged — operator pastes into SCADA manually
- **System never writes back autonomously** — this is the EU AI Act guardrail
- Audit trail: advisory, timestamp, approved by [user], executed externally

---

## Scenario 5 — Inverter Shutdown / Total Outage (Immediate Dispatch)

### What SCADA sends
```
INV 01.01.002  |  2023-09-05 06:00 → 2023-09-12 18:00  (7 days 12 h)
P_AC:       0.0 kW  (flat zero all day — not just low)
I_DC_SUM:   0.0 A
U_DC:       0 V     (inverter not energised at all)
Irradiation: 650–820 W/m²  (neighbouring inverters producing normally)
DV/EVU:    100 %            (no external curtailment signal)
```

### What the system detects
- **Stage 1 — PR fires immediately:** PR = 0.0 for day 1 → acute single-day deviation = 0.79 (entire fleet median) → flag
- **Stage 2:**
  - DV = 100 % → curtailment ruled out
  - All neighbours PR ≈ 0.78 → plant-level/weather ruled out
  - η check: I_DC = 0, U_DC = 0 → inverter not powered, not a partial fault
  - `(sun.p_ac < 0.02 × kWp).mean() = 1.0` → **total outage, not degradation**
  - Irradiation healthy on 7 of 8 days → not weather/snow
  - Duration: 7.5 days → MaStR 72-h reporting threshold exceeded on day 3

### Draft sent to Approval Queue
```
INCIDENT CARD  INV 01.01.002  #INC-2023-311
Classification : TOTAL OUTAGE — inverter shutdown
Severity       : CRITICAL
Energy lost    : 1,120 kWh (7.5 days × ~150 kWh/sunny day)
EUR impact     : €1,299 estimated (FiT 11.6 ct/kWh)
Outage started : 2023-09-05 06:00  |  Duration: 7d 12h (still running)

RECOMMENDED ACTION
  → IMMEDIATE dispatch — CRITICAL, €1,299 active loss
  → Check: DC isolator, AC contactor, comms cable (inverter silent)
  → Bring: laptop with inverter config software, spare fuse set, multimeter
  → Verify grid connection at AC output before reset

COMPLIANCE NOTE (BNetzA MaStR / NIS2)
  72-h reporting window BREACHED at 2023-09-08 06:00.
  MaStR mandatory outage report due NOW — Unit ID DE-MaStR-XXXX.
  File via: marktstammdatenregister.de → Betreiber → Meldung Ausfall.
  NIS2 / KRITIS: total capacity lost = 30.6 kWp × 1 unit — below 1 MW
  threshold for BSI early warning. No BSI report required.

[APPROVE — DISPATCH NOW]  [REJECT]
```

### After operator clicks APPROVE
- Work order generated with parts list + MaStR filing reminder
- Dashboard: incident moves to "dispatched", EUR clock shows live loss accumulating
- If not resolved within 24 h → auto-escalation card generated

---

## How to Add Synthetic Data to LightGBM Validation

See `demo/inject_ml_val.py` for the full script. Summary:

```
real training data (Jan–Sep 2023, healthy rows only)
          ↓
   LightGBM fit (irr, T_mod, T_amb, sun_elev, hour sin/cos, lag_irr)
          ↓
   Validation set = Oct–Dec 2023 (real) + synthetic fault windows
          ↓
   Residual = P_AC_actual − P_AC_predicted
   On fault windows: residual should SPIKE (model predicts healthy, actuals are low)
   On healthy windows: residual stays small
          ↓
   Confusion matrix: did Stage-1 PR flag fire when residual > threshold?
```

**Key rules when injecting:**
1. Synthetic rows get `fault_type` label column (string_fault / soiling / curtailment / etc.)
2. Curtailment rows excluded from both training AND target residual scoring (model can't predict regulatory curtailment)
3. Scenario column kept — lets you slice metrics per fault type
4. Never mix synthetic rows into training — ground truth is only valid for eval

**Score expected:** if LightGBM residuals on fault windows > 2× healthy-window RMSE for ≥ 3 days → PR cascade cross-confirmed. Target: 6/6 fault types detected.
