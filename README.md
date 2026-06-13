# Enerparc Pulse

**AI-powered O&M command crew for utility-scale solar.** SCADA telemetry → ML performance-ratio baseline → multi-stage fault detection → EUR impact pricing → LLM-as-judge routing → human-approved action drafts, grounded in EU/DE energy regulation.

Built for the **Energy Hack Munich 2026** (Enerparc Open Track).

> ⚖️ **Advisory only — never autonomous control.** Every outbound action stops at a human-approval gate. This is a deliberate design choice to stay out of EU AI Act high-risk classification.

![Enerparc Pulse demo](assets/demo.gif)

---

## What it does

Enerparc Pulse watches a fleet of PV inverters and turns raw SCADA into *decisions a human can approve in one click*:

1. **Predicts the healthy-state Performance Ratio (PR)** with a LightGBM model — the baseline an inverter *should* hit given weather + time.
2. **Detects deviations** with a PR cascade (IEC 61724 pattern), then drills down to root cause.
3. **Prices the impact** in EUR (lost kWh × feed-in tariff).
4. **Routes** each incident (dispatch a truck / file a §15 EEG claim / log only) via an LLM-as-judge.
5. **Drafts the action** + a compliance citation, and parks it in an approval queue.

### The four demo scenarios

| Scenario | Signal | Outcome |
|---|---|---|
| **Blackout / outage** | long zero-output window, irradiation healthy, neighbours fine | fault → priced → work order |
| **Curtailment** | DV/EVU < 100 % | **not a fault** → no truck roll → §15 EEG claim drafted |
| **Balancing / forecast** | Chronos-Bolt 24 h day-ahead | battery charge/discharge narrative |
| **Reactive power** | cos φ < 0.90 | setpoint *recommendation* — payload generated only after human approval, never written back |

---

## The ML model — PR predictor

A **LightGBM** model deployed on Render predicts expected PR per interval from weather + time features (irradiance, module/ambient temp, hour, day-of-year, month + engineered sin/cos & interaction terms). It **never** sees `I_DC`/`U_DC` — those are reserved for Stage-2 root-cause diagnosis (no leakage).

- **Endpoint:** `POST https://plant-a-pr-api.onrender.com/predict`
- **Health:** `GET /health` · **Metrics:** `GET /metrics`
- **Trained:** Plant A 2017 (Jan–Sep) · **Validated:** held-out 2018

**2018 validation (500 sampled daytime points, 60 inverters):**

| Metric | Value | Reading |
|---|---|---|
| **MAE** | 0.049 | ~5 PR-points typical error |
| **RMSE** | 0.080 | outlier-sensitive; RMSE≈1.6×MAE → a few hard low-PR points |
| **R²** | 0.773 | 77 % of PR variance explained on a held-out *year*, across *unseen* inverters |
| **bias** | +0.008 | effectively unbiased |

The chart (Overview page → *Performance Ratio*) plots measured vs predicted PR with hover inspection. When the model can't be reached it falls back to a calibrated **physics baseline** (irr × kWp × temp-derate).

---

## Detection — PR cascade (IEC 61724)

**Stage 1 — screen:** per-inverter daily PR vs the ML-predicted healthy baseline (and fleet-relative deviation), with a 3-day persistence filter and a data-derived absolute floor (~PR < 0.65–0.70).

**Stage 2 — root cause** (runs only when Stage 1 fires, cheapest check first):

| Check | Verdict |
|---|---|
| DV/EVU < 100 % | **Curtailment** → §15 EEG claim, stop |
| all PRs drop together | **Plant-level / weather** |
| η = P_AC/(U_DC·I_DC) low | **AC/conversion fault** |
| I_DC step ≈ 1/n_strings, U_DC stable | **String out** |
| slow drift, temp-independent | **Soiling / degradation** |
| deficit locked to sun angle | **Shading** |
| seen in ticket history | **Repeat offender** → root-cause visit |

---

## Agents

| Agent | Role |
|---|---|
| `agents/triage.py` | PR cascade — Stage 1 screen + Stage 2 root cause |
| `agents/impact.py` | EUR pricing (lost kWh × FiT) |
| `agents/ml_api.py` | LightGBM PR client (Render) + physics fallback |
| `agents/ml.py` | local per-inverter LightGBM (offline training/eval) |
| `agents/forecast.py` | Chronos-Bolt 24 h forecast client (AWS Lambda, eu-north-1) |
| `agents/orchestrator.py` | LLM-as-judge — 2-tier incident routing |
| `agents/drafter.py` | action / claim / work-order drafts |
| `agents/rag_compliance.py` | keyword-keyed RAG over 7 EU/DE regulation chunks → citation per incident |

---

## Run it

```bash
pip install -r requirements.txt
uvicorn app:app --port 8080
```

Open **http://127.0.0.1:8080/draft** — the primary dashboard (Overview · Agents · Compliance Chat · Tickets · Approval Queue).

The dashboard serves **pre-built artifacts** from `out/*.json`, so the demo runs **without** the raw datasets. To regenerate them:

```bash
python pipeline.py          # 5-inverter run → out/incidents.json, timeseries.json, ...
python pipeline_2018.py     # 2018 validation → out/pr_validation.json (the PR chart)
python pipeline_fault_test.py   # synthetic fault-injection eval
```

### API

`GET /api/inverters · /api/timeseries · /api/incidents · /api/tickets · /api/briefing · /api/forecast · /api/pr_validation` · `POST /api/decide` (audit → `out/audit.jsonl`) · `POST /api/chat`

---

## Repo layout

```
app.py                  FastAPI server + dashboard routes
dashboard_draft.html    primary UI (Enerparc Pulse)
agents/                 the 8 agents above
pipeline*.py            data pipelines (main / 2018 validation / fault test / synthetic)
eval.py inject*.py      fault injection + scoring
out/                    pre-built artifacts the dashboard reads (+ eval results, charts)
data/                   small organizer reference files (System_Overview, FiT, Tickets .xlsx)
docs/                   Enerparc-task (system design) · agent · regulations-eu-de · demo-scenarios
assets/demo.gif         walkthrough recording
```

---

## Honesty notes

- **EUR figures are estimates** (physics/ML baseline, not certified meter) — "estimated revenue impact".
- **Raw datasets are not committed** (organizer files are 100s of MB; see `.gitignore`). The demo runs off `out/*.json`; full pipeline re-runs need the original Enerparc datasets placed locally.
- **Runtime paths** in `pipeline*.py` / `/api/tickets` assume the original hackathon folder layout for raw data + `2. Additional Data/`.
- **LLM features** (Compliance Chat, judge reasoning) call an Anthropic-backed helper and **degrade gracefully to deterministic mock/static text** when no key/module is present — the committed `out/*.json` already contain baked compliance notes, so the demo is fully offline-safe.
- The cos φ = 0.95 reactive-power recommendation is a domain rule, not from the dataset; the forecast price narrative has no live price feed behind it.
- **Hard rule:** every outbound action stops at the approval queue (EU AI Act advisory posture).

---

## Team

Built by **Nico Junkers · Pavan Kumar · Rebecca Riedmayer · Vasu Chukka** at Energy Hack Munich 2026.

**Vasu Chukka**
📬 chukka.vasu@outlook.com
💻 https://www.linkedin.com/in/vasu-chukka-1a3569116/
