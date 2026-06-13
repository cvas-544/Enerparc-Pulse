# Enerparc Open Track — System Design (as built)

Status 2026-06-13 ~03:00, hackathon night. Working demo lives in
`~/Downloads/energy_Hackathon/demo/` (outside this repo). This doc freezes the
system design; agent roster details in [agent.md](agent.md).

## 1. Scenario scope (cut for time — prove 4, not 7)

1. **Blackout/outage** — e.g. INV 002 long outage (€1,120 single incident); irradiation healthy, neighbors fine → fault, priced, work order
2. **Curtailment** — DV/EVU < 100 % days → NOT a fault, no truck roll, §15 EEG claim drafted
3. **Balancing/Forecast** — Chronos-Bolt (our Lambda, eu-north-1) 24 h ahead, battery charge/discharge narrative
4. **Reactive power** — cos φ < 0.90 observed → setpoint *recommendation* drafted; payload generated only after human approval — **never written back**

## 1b. Toolchain (what the demo actually uses)

| Layer | Tool | Why not the alternative |
|---|---|---|
| Data loading | `pandas.read_csv` | 105K rows fits in RAM in ms — no query engine needed |
| Detection / PR cascade | pandas rolling + numpy | Deterministic, testable, fast |
| EUR pricing | pandas arithmetic | Same frame, no join needed |
| Orchestrator routing | Pure Python dicts + LLM call | Tier-1 is just a dict lookup |
| Ticket matching | pandas string filter | 84 tickets, tiny |
| Forecast | HTTP → Chronos Lambda | Model lives on AWS, not local |
| Serving | FastAPI reads `out/*.json` | Pre-built at pipeline run time |

**DuckDB: not used.** The first-5-inverters extract (105K rows × 22 cols) fits in pandas.
DuckDB would be the right call for querying the full 830 MB / 65-inverter file without
pre-extracting — `duckdb.read_csv_auto('main_monitoring_data.csv')` scans only the
columns needed, no server. Pitched as the scale path in presentations.

**No vector DB.** 84 tickets fit in a pandas DataFrame; 7 regulation chunks fit in one
LLM context window. Vector DB would add latency and infra for no accuracy gain at this corpus size.

**Per-string MPPT channel data: not in this dataset.** Only `I_DC_SUM` is exported.
String fault detection uses the step-quantum heuristic: a ~1/N_strings drop in `I_DC_SUM`
(e.g. −20% for a 5-string inverter) with stable `U_DC` is conclusive. In production,
per-MPPT channel data from the inverter comms bus would pinpoint the exact string.

## 2. Data (all real, from organizers)

| File | Use |
|---|---|
| `raw_data/inverters_first5_2023.csv` | Extracted: first 5 inverters × 2023 from 830 MB main file (105,121 rows × 22 cols, plant cols incl. DV/EVU, irradiation, temps, cos φ) |
| `2. Additional Data/System_Overview.xlsx` | per-inverter kWp (001–003,005 = 30.6; 004 = 24.5) |
| `2. Additional Data/feed-in-tarrifs.xlsx` | per-inverter weekly FiT → 2023 mean 11.6 ct/kWh |
| `2. Additional Data/Tickets.xlsx` | 84 historical tickets → attached to drafts as precedent |
| errorcodes.csv | NOT used (codes have no legend); op-state possible neighbor up/down signal |

2023 fleet truth (pipeline output): 001 25.2 MWh/€315 · **002 16.4 MWh/€1,201** ·
003 23.3/€394 · **004 14.8/€1,111 (chronic, 36 incidents)** · 005 24.0/€388.

## 3. Detection design — PR cascade (IEC 61724 pattern)

### Stage 1 — PR screen (always on, per inverter, daily)

**Metric (IEC 61724-1):**

```
PR(i,d) = E_AC(i,d) / ( H_POA(d) × kWp_i )

E_AC   = Σ P_AC × 5min                      [kWh]   measured inverter output
H_POA  = Σ irradiation × 5min / 1000        [kWh/m²] plant sensor, shared by all
kWp_i  = from System_Overview (PR normalizes 004's 24.5 kWp away)
```

**Healthy-day filter before computing the reference distribution:** drop days with
any DV/EVU < 100 % (curtailed output ≠ capability), days with H_POA below a
minimum (dark winter days → PR is noise), and known outage windows.

**Trigger A — absolute floor (derived from data, not invented):**
`floor_i = median(PR_healthy_i) − 3 × MAD(PR_healthy_i)` → here ≈ **PR < 0.65–0.70 = alarm**.
Market anchor: EPC/O&M contracts carry guaranteed-PR clauses (typically 0.75–0.80
annual, with liquidated damages) — a floor breach is a *contractual* event, not just
an engineering alert. Peer benchmarking + PR alarms are core features of every
commercial PV monitoring platform (meteocontrol, GreenPowerMonitor, Power Factors)
— the metric is industry-standard; our contribution is what happens *after* the flag.

**Trigger B — relative to fleet median (the smarter one):**

```
fleet_median(d) = median over i of PR(i,d)
deviation(i,d)  = fleet_median(d) − PR(i,d)
flag if deviation > 0.08–0.10 for ≥ 3 consecutive days
```

Same sky for all inverters → weather cancels exactly; an inverter trailing its
siblings is a machine problem regardless of conditions. The 3-day persistence
filter kills single-day sensor-noise false alarms.

**Refinement:** temperature-corrected PR (NREL / IEC 61724-1) — divide out the
−0.4 %/°C module-temp effect so winter doesn't look healthy and summer sick.
Module temp is in the data; correction is free.

### Stage 2 — root-cause drill-down (runs only when Stage 1 fires)

Checks ordered by cost; first decisive answer wins:

| # | Check | Signal | Verdict |
|---|---|---|---|
| 1 | External? | DV/EVU < 100 % during deficit | **Curtailment** — draft §15 EEG claim, STOP (no truck roll) |
| 2 | Fleet-wide? | all 5 PRs dropped together | **Plant-level/weather** (grid, trafo, snow) — not this inverter |
| 3 | Which side? | η = P_AC / (U_DC × I_DC) low | **AC/conversion fault** — capacitors (`Kondensatoren defekt` precedent), cooling; η normal → DC side |
| 4 | Shape of DC loss? | I_DC step ≈ 1/n_strings (001: 5 strings → −20 % quantum), U_DC stable | **String out** (`Strangausfall`) — fuse/connector, parts hint |
| 4b | | slow drift over weeks/months, all hours equally | **Soiling / panel degradation** — >0.5 %/yr ⇒ wash |
| 5 | Time pattern? | deficit locked to specific hours / sun angles | **Shading** — vegetation, new obstruction |
| 5b | | deficit grows with module temp beyond −0.4 %/°C | **Cooling/ventilation fault**, hot connector |
| 5c | | winter morning zero with irradiation present, clears ~noon, ambient < 0 °C | **Snow** (`Schnee` tickets) — weather, no action |
| 6 | Seen before? | Tickets.xlsx entries for this inverter | **Repeat offender** (e.g. 004: 4 tickets, chronic) ⇒ root-cause visit, not patch |

**Combinations → distinct actions:** low PR + DC + step + temp-independent = string
failure (bring fuses); low PR + drift + worst at high sun = soiling (schedule wash);
low PR + AC-side + temp-correlated = electronics/cooling (capacitor kit). The math
computes the discriminators; the LLM in Classify writes the reasoning sentence on
the incident card ("I_DC stepped −19 % with U_DC stable → one string out; matches
2021 Strangausfall ticket").

## 4. Prediction model (ML power predictor)

One **LightGBM power predictor** deployed on Render, called via `agents/ml_api.py`.

- **Task:** predict expected P_AC (kW) per 5-min interval given weather + time features
- **Endpoint:** `POST /predict` on Render · health check: `GET /health`
- **Features:** irr, t_mod, t_amb, hour+doy (sin/cos), lagged irradiation. Never I_DC/U_DC (leakage — reserved for Stage-2 root-cause diagnosis).
- **Training:** INV 01.01.001, Jan–Sep 2023. Exclude curtailment rows (DV/EVU < 100 %) + known outage windows. Time split only — no random shuffle.
- **Output → pipeline:** `expected_kw` series → Triage uses it as the expected-power baseline for PR deviation; Impact uses it to price lost kWh × FiT.
- **Fallback:** physics baseline (irr × kWp × temp-derate, calibrated) when API offline.

**Chronos-Bolt** (Amazon Lambda, eu-north-1) handles zero-shot 24 h day-ahead for the balancing/overview panel — separate concern from the power model.

## 5. Eval — fault injection on real telemetry

Mock = inject known signatures into healthy inverter (005): string-out step (×0.8),
soiling drift (3-month ramp), AC-only loss (P_AC×0.85, DC real), fake curtailment
(DV=60 %), total outage, snow morning. Ground truth by construction → confusion
matrix slide ("6/6 classified, € error < 5 %"). Synthetic file kept separate
(`raw_data/synthetic_eval.csv` — planned); dashboard demos REAL data only.

## 5b. RAG Compliance agent (added 2026-06-13)

`demo/agents/rag_compliance.py` — called after the Action Drafter on every incident.

**Why not a vector DB:** 84 tickets + 7 regulation chunks fit in one LLM context window
(< 2 KB). Retrieval is keyword-keyed by route+subtype — fast, deterministic, zero infra.

**Corpus (7 chunks, embedded in code):**

| Key | Regulation |
|---|---|
| EEG_15 | §15 EEG 2023 — 95% FiT compensation, 12-month filing window |
| REDISPATCH2 | §13/§14 EnWG, BNetzA REGENT — Redispatch 2.0 obligations |
| PARA14A_ENWG | §14a EnWG — §14a dimming ≠ fault, reduced grid fee |
| IEC61724 | IEC 61724-1:2021 — PR definition, measurement intervals, temp correction |
| BNETZ_MASTR | BNetzA MaStR — annual yield, 72-h outage reporting, MaStR unit ID |
| EU_AI_ACT | EU AI Act Art.6/Annex III — advisory-only posture, audit trail |
| NIS2_KRITIS | NIS2 + KRITIS-DachG — 24-h BSI early warning, ≥ 1 MW × 1 h threshold |

**Flow:**
1. `retrieve(route, subtype)` → selects relevant chunks (2-3 per incident)
2. `enrich(inc)` → LLM generates 3-4 sentence citation grounded in retrieved text
3. Fallback `_static_note()` runs without LLM — safe for offline demo

**Output:** `inc["compliance_note"]` — shown in dashboard approval queue as a dimmed
citation block below the judge route line.

## 6. Built artifacts (demo/)

| File | What |
|---|---|
| `pipeline.py` | 5-inverter run: physics baseline → windows → classify → € (FiT) → tickets → orchestrator route → drafts → **RAG compliance note** → `out/*.json` |
| `app.py` | FastAPI: `GET /api/inverters · timeseries · incidents · tickets · briefing · forecast` + `POST /api/decide`; audit → `out/audit.jsonl` |
| `dashboard.html` | **Primary UI** at `:8080/draft` — sidebar layout, 5 pages (see below) |
| `index.html` | plain fallback UI at `:8080/` |
| Chronos Lambda | `POST {"series":[...], "horizon":N}` → `quantiles {0.1,0.5,0.9}` |

### Dashboard pages (`:8080/draft`)

Design system: charcoal `#191919` / vanilla `#FFFAEE` / accent `#FE5102` · Space Grotesk / Inter / Space Mono · grain overlay · 20px border-radius · inlined brand SVGs.

**Sidebar** (228px fixed): beyondWatt logo · nav sections Main / Actions · inverter selector footer.

**Inverter selector** — "All Inverters" (default) + INV 001–005. Every page reacts to the selection:

| Page | All Inverters | Single inverter |
|---|---|---|
| **Overview** | Fleet totals: summed MWh/incidents/EUR, avg PR + 4 agent crew cards + Chronos strip | Single inverter metrics |
| **Analytics** | 128-row table with Inverter column + aggregated monthly bars | Filtered to that inverter |
| **Compliance Chat** | Keyword-matched RAG corpus (EEG §15 / IEC 61724 / NIS2 / EU AI Act) — offline-safe | Same |
| **Tickets** | All 34 real `Tickets.xlsx` rows (first 5 inverters + Plant) | That inverter + Plant tickets |
| **Approval Queue** | All 128 incidents, EUR-ranked, with judge route + compliance citation + Approve/Reject | That inverter's incidents |

**`GET /api/tickets`** (added 2026-06-13): parses `Tickets.xlsx`, filters to `INVS + ["Plant"]`, returns JSON with component/start/end/category fields.

Run: `cd ~/Downloads/energy_Hackathon/demo && <repo>/prototype/.venv/bin/python -m uvicorn app:app --port 8080`

## 7. Honesty notes for the pitch

- € figures are estimates (physics baseline, not certified meter) — say "estimated revenue impact"
- cos φ = 0.95 recommendation is our domain rule, not from dataset
- forecast panel price narrative has no live price feed behind it
- hard rule #1 intact: every outbound action stops at approval queue (EU AI Act advisory posture)

## OPEN

- LightGBM per-inverter training + 3-way MAE table (physics vs Chronos vs LGBM)
- Expand RAG corpus to include actual EEG/BNetzA PDF text for deeper citations

## DONE (as of 2026-06-13 morning)

- PR cascade (6/6 injected faults) wired into `agents/triage.py`
- Orchestrator (LLM-as-judge, 2-tier routing) in `agents/orchestrator.py`
- RAG compliance agent in `agents/rag_compliance.py`
- Fault injector (`inject.py`) + eval (`eval.py`) — synthetic 6-scenario eval
