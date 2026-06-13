# Agent Roster — beyondWatt × Energy Hack Munich (Enerparc Open Track)

Decided 2026-06-13 (hackathon night). Maps the proposed "O&M Command Crew" onto the
built beyondWatt graph and fixes the final 7-agent lineup. Hard rule #1 applies
throughout: **every outbound action terminates at the human-approval queue.**

## 1. What exists (beyondWatt prototype, `prototype/graph/`)

| Agent / node | Does | File |
|---|---|---|
| Ingest | Loads per-asset telemetry, mandatory `asset_id` filter | `nodes.py` |
| Expect | Expected-power baseline from irradiation/temp | `nodes.py` |
| Detect | Anomaly windows (actual vs expected) | `nodes.py` |
| Classify | Fault vs **curtailment/Redispatch** vs weather + severity | `nodes.py` |
| Report | Prices impact in EUR, drafts work order → approval UI | `nodes.py` |
| Balancing (module) | Battery dispatch on Chronos forecast, consumer/storage notices | `balancing.py` |
| Forecast (Lambda) | Chronos-Bolt, public API (`README.md` for URL) | `forecast.py` + `lambda/` |

## 2. Proposed hackathon crew → beyondWatt mapping

| Proposed | beyondWatt equivalent | Status |
|---|---|---|
| Triaging (error-code ↔ power-drop joins) | Ingest+Expect+Detect+Classify | ✅ built; error-code join CUT — codes have no legend (bare numbers, no manufacturer table). **DuckDB not used** — dataset is 105K rows, fits in pandas RAM in ms. DuckDB would be useful for the full 830 MB / 65-inverter file. |
| Impact (€ via FiT + pvlib) | Report | ✅ built; add FiT price source (per-inverter weekly FiT received) |
| Regulatory Compliance (BNetzA/EEG logger) | Classify already separates curtailment from fault | 🟡 add report drafting on top |
| Tariff Prioritizer (cross-plant repair order) | Balancing's EUR ranking, re-aimed | 🟡 small new loop over Impact output |
| Dispatch (RAG over historical service tickets) | Planned in architecture.md only | ❌ new build |
| Copilot (chat + viz) | Approval UI is a gate, not chat | ❌ new build |
| Warranty Auditor | — | ❌ new; lowest priority (needs component-age/brand data) |

Proposal rejected in part: its routing ("€>50 → JSON pushed to dispatch system")
bypasses the human gate. We route to the **approval queue** instead — the gate is
the EU-AI-Act advisory posture and a judge-facing differentiator.

The proposal has **no forecasting**. Chronos stays — it is our edge.

## 3. FINAL roster (updated 2026-06-13 morning) — 5 agents, 5 scenarios

| # | Agent | File | Covers | State |
|---|---|---|---|---|
| 1 | **Triage** | `agents/triage.py` | PR cascade (IEC 61724): Stage 1 fleet-median deviation + absolute floor; Stage 2 drill-down → subtype (curtailment/outage/string/ac/soiling/snow/dc_fault/plant_wide) | ✅ built, 6/6 eval |
| 2 | **Impact** | `agents/impact.py` | lost kWh × per-inverter FiT → EUR; severity label | ✅ built |
| 3 | **Orchestrator (LLM-as-Judge)** | `agents/orchestrator.py` | Tier-1 deterministic for direct evidence; Tier-2 LLM for ambiguous (dc_fault); EUR/day vs truck-roll cost weighting; fleet priority rank + briefing | ✅ built, LLM wired |
| 4 | **Action Drafter** | `agents/drafter.py` | fault → work order; curtailment → §15 EEG claim; grid-support → cos φ setpoint (approval-gated, never auto-written); matches historical tickets | ✅ built |
| 5 | **RAG Compliance** | `agents/rag_compliance.py` | Retrieves regulation chunks (EEG §15, IEC 61724, BNetzA MaStR, Redispatch 2.0, §14a EnWG, EU AI Act, NIS2); LLM generates 3-4 sentence citation grounded in actual text; appends to every draft as `compliance_note` | ✅ built 2026-06-13 |
| — | **Forecast** | `agents/forecast.py` | Chronos-Bolt Lambda, 24 h, quantile output (p10/p50/p90); live in dashboard | ✅ built |

**Pipeline order per incident window:**
```
Triage → Impact → Orchestrator → Action Drafter → RAG Compliance
                        ↓
                  fleet rank + briefing (Orchestrator)
```

**Forecast** is called live by the dashboard on demand (not part of the incident pipeline).

Cut from the 7: Copilot, Prioritizer (fleet table on dashboard covers the demo need),
Warranty. Compliance merged into dedicated RAG Compliance agent (not buried in Drafter).

### ML plan — single power prediction model

One **LightGBM power predictor** deployed on Render, called via `agents/ml_api.py`.

| What | Detail |
|---|---|
| Task | Predict expected P_AC (kW) per 5-min interval given weather + time features |
| Endpoint | `POST /predict` on Render (health: `GET /health`) |
| Features | irr, t_mod, t_amb, hour+doy (sin/cos), lagged irradiation |
| Output | `expected_kw` series → used by Triage (deviation baseline) + Impact (EUR pricing) |
| Training | Exclude curtailment rows + known outages; time split Jan–Sep / Oct–Dec; never use I_DC/U_DC (reserved for Stage-2 diagnosis) |
| Fallback | Physics baseline (irr × kWp × temp-derate) when API offline |

**Chronos-Bolt** (Amazon Lambda, eu-north-1) handles zero-shot 24 h day-ahead forecasting for the balancing/overview panel — separate from the power model.

## 3b. Original 7-agent list (pitch slide — "full vision")

1. **Triage** — existing ingest→expect→detect→classify on telemetry only. Error-code feature cut — no code legend provided. **Tool stack: pandas + numpy** (not DuckDB — the first-5-inverters extract is 105K rows, pandas handles it in memory; DuckDB would be the right call if querying the full 830 MB file without pre-extracting)
2. **Impact** — existing EUR pricing; + feed-in-tariff rates per plant
3. **Dispatch** — NEW: vector RAG (ChromaDB) over historical service tickets; attaches past fix + parts list to the draft work order
4. **Compliance** — extends Classify: drafts EEG/BNetzA compensation report for curtailment/outage windows
5. **Prioritizer** — NEW (small): ranks open incidents across plants by €/hour from Impact output
6. **Forecast** — existing Chronos-Bolt; gives Impact its "should have produced" baseline
7. **Copilot** — NEW: plain-language chat over graph state + viz scripts (pattern: `viz_inverter_2023.py` in the hackathon folder)

### Build state (03:00) — demo + UI shipped

Done: 5-inverter pipeline, FastAPI app, design-system dashboard with inverter
selector (`:8080/draft`), approval queue + audit log, live Chronos panel.
Remaining (see Enerparc-task.md OPEN): PR cascade, DC/AC discriminator,
fault-injection eval, LightGBM training.

## 4. Relation to Invertix (judge framing)

Invertix = "22 specialized AI workers in 7 departments" for solar/wind/storage O&M
(1.8 GW, €1.7M Vireo pre-seed). Our roster is the same workforce metaphor at demo scale:

| Ours | Invertix department |
|---|---|
| Triage | SCADA monitoring + alarm classification |
| Impact | Live kWh→EUR translation (their core differentiator) |
| Compliance | Bank-grade reporting (theirs: DSCR/LLCR for funds; ours: BNetzA/EEG claims) |
| Prioritizer | Portfolio reporting / fleet view |
| Dispatch | Maintenance/service workers |
| Copilot | Workforce interface |
| Forecast | **no equivalent — our addition** |

Pitch line: *"We applied your workforce thesis to Enerparc's raw dataset in 24 hours —
and added the one worker you don't have: a forecasting agent that prices anomalies
against what the plant should have earned."*

Positioning (from domain-knowledge.md): every energy-AI layer has an incumbent except
the **institutional back office** — Invertix's gap, and ours. Advisory only, never control.

## 5. Hackathon dataset facts (full inventory, received 2026-06-13 01:18)

Folder: `~/Downloads/energy_Hackathon/`

| File | Contents |
|---|---|
| `raw_data/inverter_INV_01_01_001_2023.csv` | 105,120 rows × 14 cols, 5-min, full 2023, one inverter |
| `main_monitoring_data.csv` (stored separately, 830 MB) | 206 cols = 11 plant-level + **65 inverters** × (I_DC, P_AC, U_DC) |
| `3. Errorcodes/errorcodes.csv` | 990K rows, 5-min, **2017→Jun 2026**, 65 × (Error, Operational State). **UNUSED — codes have no legend.** Op-state (0–6) may serve as up/down signal |
| `2. Additional Data/Tickets.xlsx` | 84 service tickets 2019–2026, German categories (`Netzstörung`, `Curtailment`, `Schnee`, `Strangausfall`…); many plant-level (`component="Plant"`) — Dispatch must match both inverter + plant scope |
| `2. Additional Data/feed-in-tarrifs.xlsx` | Per-inverter **weekly** FiT 2016→2026 (65 × 574 weeks), ct/kWh |
| `2. Additional Data/System_Overview.xlsx` | Plant design: 1,897 kWp, 7,747 modules, per-inverter kWp/module type/manufacturer (anonymized), strings |

- Telemetry German format (`;` sep, `,` decimal). 2023 totals INV 01.01.001: 25.2 MWh, peak May, best day 2023-05-30.
- Demo-ready anomalies: ~3-week outage around day 280–300 (Oct); 26 curtailment days (`DV < 100 %`), clustered Jan–Mar.
- Cut features: error-code correlation (no legend), Warranty Auditor (no warranty terms/component ages).

## 6. RAG Compliance Agent — retrieval design

### Corpus
7 in-context regulation chunks (~2 KB total): EEG_15, REDISPATCH2, PARA14A_ENWG, IEC61724, BNETZ_MASTR, EU_AI_ACT, NIS2_KRITIS. Stored as plain strings in `agents/rag_compliance.py:_CORPUS`.

**No vector DB at hackathon scale** — stuffing all 7 chunks into a single LLM context window is faster and more reliable than retrieval latency over a 7-document store. At production scale (100+ regulation docs) this would switch to a proper vector index.

### Retrieval technique (incident pipeline)
Route-keyed retrieval: `_ROUTE_CHUNKS` maps route (claim/dispatch/log/monitor/setpoint) → relevant chunk keys. `_SUBTYPE_CHUNKS` adds subtype-specific chunks (e.g. `curtailment → [EEG_15, REDISPATCH2]`). Union of both sets → 2–3 chunks passed to LLM. No embeddings; deterministic lookup.

### Retrieval technique (compliance chat `/api/chat`)
TF-IDF–lite keyword overlap: tokenize query → count token overlap with each corpus chunk → rank by overlap size → pass top-3 chunks to LLM. No vector embeddings; no external dependencies. Handles short queries (1-2 words) and long questions equally.

### Ticket-grounded chat — tickets as EXTERNAL knowledge (DECIDED 2026-06-13)
Tickets (= pipeline incidents in `out/incidents.json`) are deliberately **kept out of the vector DB**. The chat can discuss a specific ticket and explain *why it was classified* and *why the action was proposed*, but it loads that knowledge by **selection, not similarity search**:

- **Trigger:** the "💬 Know more" button on any incident card calls `discussTicket(id)` → the chat POSTs `{query, incident_id}` to `/api/chat`. A banner pins the active ticket; "✕ exit ticket" returns to general mode.
- **Retrieval = fetch by id.** Endpoint loads the incident from `incidents.json` by `id` and builds a `ticket_ctx` block from the reasoning chain the pipeline already produced (subtype, PR gap, EUR, route + confidence + `by`, draft, `compliance_note`, similar tickets). **No embeddings, no re-indexing** — a newly created ticket is queryable the instant the pipeline writes it.
- **Regulation chunks** in ticket mode come from the incident's route via `rag_compliance.retrieve(route, subtype)` (the same `_ROUTE_CHUNKS`/`_SUBTYPE_CHUNKS` map the pipeline used), not keyword scoring — guarantees the chat cites the same regulation the draft was built on.
- **Why no vectors:** tickets are enumerable and id-keyed; the user explicitly picks one. Similarity search only earns its cost when you can't enumerate and must fuzzy-match across thousands. Same threshold as the regulation corpus: add `sentence-transformers` + ChromaDB only past ~50 docs you can't select directly.
- **Zero new ML:** the "why" is already in the incident record; the LLM narrates the existing reasoning chain conversationally. `max_tokens` is raised to 500 in ticket mode (explanations run longer than general Q&A).

### Embeddings — why not used
At 7 chunks, cosine similarity over embeddings adds model download latency (~100ms) with zero retrieval quality gain over exact keyword overlap. DECISION: add `sentence-transformers` embeddings + ChromaDB only if corpus exceeds ~50 documents. Flag: `OPEN` in `docs/memory-architecture.md`.

### LLM backend (same gateway as orchestrator)
Anthropic API (`ANTHROPIC_API_KEY`, `BEYONDWATT_MODEL`) → rule-based fallback (`_static_note`). System prompt enforces: domain-scoped answers (solar O&M / EU regulation only), grounded in corpus, 3–5 sentence max, English unless query is German. Out-of-scope queries are redirected to topics list.

### Scope guard (chat endpoint)
`/api/chat` system prompt explicitly restricts to: solar energy, O&M, IEC 61724, EEG, BNetzA, NIS2, EU AI Act, §14a, Redispatch, PR, string faults, soiling, forecasting, beyondWatt features. General coding / finance / unrelated questions get a polite redirect.
