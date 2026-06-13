# EU & Germany: Regulations for AI Tools in Solar / Renewable / Storage

> Research snapshot: 2026-06-10. Laws move — re-verify deadlines before any compliance commitment.
> Scope: what applies to **us** building an AI-powered asset-management tool (advisory, not grid control) for solar + storage operators in Germany.

---

## 1. EU AI Act (Regulation 2024/1689) — the big one

**Classification is everything.** Annex III §2 makes an AI system high-risk when it is a **"safety component" in the management/operation of critical infrastructure** (electricity, gas, heating). Failure of the system must be able to cause physical damage or harm.

- AI that **optimizes energy costs / detects anomalies / drafts reports** → **not** high-risk (advisory).
- AI that **prevents blackouts, dispatches assets, sends control signals** → high-risk.

**Our position:** keep the system advisory with a human in the loop. No autonomous control actions toward inverters/grid. Document the classification rationale (Art. 6(3) assessment) in writing.

**Deadlines (updated 2026):** high-risk Annex III obligations pushed from Aug 2026 to **Dec 2, 2027**; Annex I products to Aug 2, 2028. GPAI and prohibited-practices rules already in force. Penalties up to €15M / 3% global turnover.

**If we ever become high-risk, obligations include:** risk-management system, data governance, technical documentation, automatic logging, human oversight design, accuracy/robustness/cybersecurity evidence, conformity assessment, registration in the EU high-risk AI database.

**Even as non-high-risk:** transparency duties (users must know they interact with AI / AI-generated content), and our existing guardrail + observability design (LangSmith traces, structured logs) doubles as compliance evidence. Build logging as if we might be reclassified.

**GPAI energy/carbon disclosure (Annex XI):** general-purpose AI model *providers* must document and disclose energy consumption / carbon footprint of training and inference. Obligation sits on model providers (Anthropic/OpenAI/Meta), not on us as deployers — but "our agent tracks its own token carbon footprint" is a cheap, on-trend feature and a pitch line (compliance-by-design).

### EU Strategic Roadmap for Digitalisation in Energy (June 2026) — per Vasu's research, verify numbers before quoting on stage

- Commission roadmap prioritizing digital/tech sovereignty, grid-enhancing technologies, demand-side flexibility; claims AI-driven O&M can save **up to €94bn annually by 2035** — strong pitch statistic if verified.
- **TwinEU**: EU-funded federated digital twins of the European grid; core requirement is **interoperability** through a unified energy data space.
- Implication for beyondWatt: design agents to plug into open European data interfaces (energy data space / TwinEU-style APIs) rather than proprietary lock-in — sovereignty-aligned positioning that lands well with EU-minded judges and customers.

Sources: [Baker Botts — AI Act for energy execs](https://www.bakerbotts.com/thought-leadership/publications/2026/march/the-eu-ai-act), [Baker Botts — new EU timelines (May 2026)](https://www.bakerbotts.com/thought-leadership/publications/2026/may/ai-regulatory-update-for-energy-new-timelines-in-the-eu-new-standards-in-the-us), [Annex III text](https://artificialintelligenceact.eu/annex/3/), [AI Act high-level summary](https://artificialintelligenceact.eu/high-level-summary/)

---

## 2. NIS2 / KRITIS — cybersecurity of our customers (and us as vendor)

- **NIS2 (EU 2022/2555):** energy is an "essential" sector. Germany implements via **NIS2-Umsetzungsgesetz**; the §30 BSIG security measures are mirrored into **§5c EnWG** for energy. Oversight split between **BSI** and **BNetzA**.
- **KRITIS:** operators above BSI-KritisV thresholds are critical-infrastructure operators; the **KRITIS-Dachgesetz** adds physical-resilience duties.
- **IT-Sicherheitskatalog (§11(1a/1b) EnWG):** grid/plant operators need an ISMS certified to **ISO 27001 + ISO 27019** (energy-specific controls). BNetzA updated the catalog ahead of NIS2 alignment.

**What this means for us:** we are (initially) not a KRITIS operator — but our **customers are**. NIS2 supply-chain security flows down to vendors: expect security questionnaires, ISO 27001 expectations, incident-notification clauses in contracts. Design for: hardened APIs, role-based access, audit logs, EU data residency.

Sources: [OpenKRITIS — EnWG & NIS2](https://www.openkritis.de/it-sicherheitsgesetz/enwg-nis2.html), [OpenKRITIS — NIS2 in Germany](https://www.openkritis.de/eu/eu-nis-2-germany.html), [OpenKRITIS — energy sector catalogs](https://www.openkritis.de/it-sicherheitsgesetz/energie-katalog-kritis.html), [BNetzA IT-Sicherheitskatalog update](https://intrapol.org/2025/05/30/bundesnetzagentur-passt-den-it-sicherheitskatalog-an-noch-vor-nis2-und-kritis-dachg/), [NIS2-KRITIS guide 2026](https://www.kertos.io/en/blog/nis2-kritis-complete-implementation-guide-2026)

---

## 3. Cyber Resilience Act (CRA) — applies to OUR software

CRA covers all commercial "products with digital elements" sold in the EU — explicitly including energy management/control software (SCADA, EMS, DMS, HMI class tools).

- **From Sept 2026:** 24-hour reporting of actively exploited vulnerabilities.
- **From Dec 2027:** full compliance — secure-by-design development, SBOM, vulnerability handling process, security updates over product lifetime, CE marking.
- Penalties up to €15M / 2.5% turnover.
- Open-source carve-out: non-commercial OSS is largely exempt; the moment we monetize, CRA applies.

**Action for us:** keep an SBOM from day one (trivial with `uv lock`), document a vulnerability-disclosure policy, pin dependencies.

Sources: [TTMS — CRA in the energy sector](https://ttms.com/the-cyber-resilience-act-in-the-energy-sector-obligations-risks-and-how-to-prepare/), [Cycode — CRA compliance guide](https://cycode.com/blog/cyber-resilience-act/), [Wirtek — CRA for software companies](https://www.wirtek.com/blog/cyber-resilience-act-explained-for-software-and-iot-companies)

---

## 4. GDPR + EU Data Act — data layer

- **GDPR:** household/prosumer consumption data (15-min load profiles) is **personal data** — it reveals behavior. Fines €20M / 4%.
  **Our mitigation:** start with **utility-scale plant SCADA only** (no natural persons → largely out of GDPR scope). If we ever touch residential/prosumer data, full GDPR program needed (DPIA, legal basis, minimization).
- **EU Data Act (applies since Sept 12, 2025):** users of connected devices (inverters, batteries, wallboxes) get the right to access and share device data with third parties. **Opportunity for us** — a legal route to pull asset data from OEM platforms on the operator's behalf. Obligations fall mainly on device manufacturers, not on us.

Sources: [Enode — EU Data Act for energy](https://enode.com/blog/evolving-energy/the-eu-data-act-what-the-energy-industry-needs-to-know-before-september-12)

---

## 5. Germany — energy-market rules our tool must understand

These are not compliance burdens on our software directly, but **domain logic the product must model correctly:**

| Rule | What it does | Product impact |
|---|---|---|
| **EEG 2023** | Feed-in tariffs / market premium / direct marketing. EU state-aid approval **expires 31 Dec 2026** → reform incoming | Revenue calculations must be versioned per legal regime |
| **Solarspitzengesetz (2025)** | §51 EEG: **no subsidy during negative-price 15-min intervals** (new PV); iMSys smart meter mandatory from 7 kWp; without iMSys feed-in capped at 60% | Negative-price detection = a core anomaly/revenue feature, not an edge case |
| **§14a EnWG** | Grid operator may dim controllable devices (storage, wallboxes); reduced grid fees in return | Dimming events must not be flagged as faults by our anomaly engine |
| **Redispatch 2.0** | Assets ≥100 kW participate in congestion management; curtailment by grid operator | Same: curtailment ≠ equipment failure. Needs separate classification |
| **Marktstammdatenregister (MaStR)** | Mandatory registration of all generation/storage assets | **Free public master-data API** — use it for asset metadata enrichment |
| **EnWG amendment (Nov 2025)** | Eases storage project development (building code, grid connection) | Storage co-location is a growth segment — keep storage in the data model |
| **MsbG + BSI TR-03109** | Smart-meter-gateway data: BSI-certified gateways, §50 MsbG data minimization, only authorized roles get data; third-party access requires certified Gateway Administrator route (TR-03109-6 v2.0 by 2027) | We do **not** pull smart-meter data directly; we ingest operator-provided SCADA. Avoid the GWA certification path entirely in v1 |

Sources: [ICLG — Renewable Energy Germany 2026](https://iclg.com/practice-areas/renewable-energy-laws-and-regulations/germany/), [Energy-Storage.News — 2026 German storage agenda](https://www.energy-storage.news/moment-of-truth-the-2026-regulatory-agenda-for-large-battery-storage-in-germany/), [RatedPower — Solarspitzengesetz](https://ratedpower.com/blog/how-solarspitzengesetz-affects-solar/), [Eversheds — storage expansion act](https://www.eversheds-sutherland.com/en/united-states/insights/germany-parliament-adopts-act-to-facilitate-energy-storage-expansion), [TÜViT — BSI TR-03109](https://www.tuvit.de/en/services/norms-standards-guidelines/bsi-tr-03109/), [datenschutz-notizen — MsbG](https://www.datenschutz-notizen.de/alles-zum-messstellenbetriebsgesetz-msbg-3015165/)

---

## 6. Compliance posture — summary decisions

1. **Stay advisory.** Human approves every outbound action (emails, tickets, curtailment recommendations). No control signals → not an AI Act safety component.
2. **Utility-scale data only in v1.** No household/smart-meter data → GDPR and MsbG/GWA certification avoided.
3. **Log like high-risk anyway.** Traces, decisions, model versions — cheap now, mandatory if reclassified, sells to KRITIS customers.
4. **CRA readiness:** SBOM, pinned deps, vuln-disclosure policy from day one.
5. **EU data residency** for anything customer-facing (Frankfurt region or EU sovereign cloud) — a sales requirement in the German energy market regardless of strict legal need.
6. **Domain correctness = compliance:** negative-price intervals, §14a dimming, and Redispatch curtailments must be first-class concepts in the anomaly engine, or the tool produces false alarms and wrong revenue numbers.
