"""Demo dashboard — FastAPI, single page, 5 inverters.

Run:  uvicorn app:app --port 8080   (from demo/)
Approve/reject is audit-logged to out/audit.jsonl. Advisory only — no write-back.
"""

import json
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

HERE = Path(__file__).parent
OUT = HERE / "out"
CHRONOS_URL = "https://3juzm47gye.execute-api.eu-north-1.amazonaws.com/"
INVS = [f"INV 01.01.00{i}" for i in range(1, 6)]

app = FastAPI(title="beyondWatt x Enerparc demo")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])

# hourly P_AC per inverter for the live forecast panel (3 clear July days)
_df = pd.read_csv(HERE.parent / "raw_data/inverters_first5_2023.csv",
                  sep=";", decimal=",", parse_dates=["timestamp"],
                  date_format="%Y.%m.%d %H:%M")
_df = _df.groupby("timestamp").mean(numeric_only=True)
HIST = {inv: _df[f"{inv} / P_AC (kW)"].fillna(0.0).resample("1h").mean()
        .fillna(0.0)["2023-07-01":"2023-07-03"] for inv in INVS}


@app.get("/api/inverters")
def inverters():
    return json.loads((OUT / "meta.json").read_text())


@app.get("/api/briefing")
def briefing():
    p = OUT / "briefing.txt"
    return {"text": p.read_text() if p.exists() else ""}


@app.get("/api/timeseries")
def timeseries(inv: str = INVS[0]):
    return json.loads((OUT / "timeseries.json").read_text())[inv]


@app.get("/api/incidents")
def incidents(inv: str | None = None):
    incs = json.loads((OUT / "incidents.json").read_text())
    return [i for i in incs if inv is None or i["inverter"] == inv]


@app.get("/api/tickets")
def tickets():
    """Real Enerparc service tickets from Tickets.xlsx, filtered to first 5 inverters + Plant."""
    df = pd.ExcelFile(HERE.parent / "2. Additional Data/Tickets.xlsx").parse("2020-2026")
    df["startdate"] = pd.to_datetime(df["startdate"], utc=True, errors="coerce").dt.tz_localize(None)
    df["enddate"]   = pd.to_datetime(df["enddate"],   utc=True, errors="coerce").dt.tz_localize(None)
    keep = INVS + ["Plant"]
    df = df[df["component"].isin(keep)].copy()
    df = df.sort_values("startdate", ascending=False)
    rows = []
    for _, r in df.iterrows():
        rows.append({
            "component": r["component"],
            "start":    r["startdate"].strftime("%Y-%m-%d") if pd.notna(r["startdate"]) else None,
            "end":      r["enddate"].strftime("%Y-%m-%d")   if pd.notna(r["enddate"])   else None,
            "category": r["category"] if pd.notna(r.get("category")) else "—",
        })
    return rows


@app.post("/api/decide")
def decide(payload: dict):
    incs = json.loads((OUT / "incidents.json").read_text())
    for inc in incs:
        if inc["id"] == payload.get("id"):
            inc["status"] = payload.get("decision", "pending")
    (OUT / "incidents.json").write_text(json.dumps(incs, indent=1))
    with (OUT / "audit.jsonl").open("a") as f:
        f.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(),
                            "id": payload.get("id"),
                            "decision": payload.get("decision"),
                            "reason": payload.get("reason", "")}) + "\n")
    return {"ok": True}


@app.get("/api/pr_validation")
def pr_validation():
    """Actual vs ML-predicted daily PR over the 2018 validation run.

    Produced by pipeline_2018.py. Returns {available:false} until validation runs.
    """
    p = OUT / "pr_validation.json"
    if not p.exists():
        return {"available": False}
    data = json.loads(p.read_text())
    data["available"] = True
    return data


@app.get("/api/forecast")
def forecast(inv: str = INVS[0]):
    hist = HIST[inv]
    body = json.dumps({"series": [round(v, 2) for v in hist.tolist()],
                       "horizon": 24}).encode()
    req = urllib.request.Request(CHRONOS_URL, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        q = json.loads(r.read())["quantiles"]
    clip = lambda xs: [max(0.0, round(v, 2)) for v in xs]
    return {"history": [round(v, 2) for v in hist.tolist()],
            "history_labels": [t.strftime("%d %Hh") for t in hist.index],
            "forecast": {"low": clip(q["0.1"]), "median": clip(q["0.5"]),
                         "high": clip(q["0.9"])}}


@app.post("/api/chat")
def chat(payload: dict):
    """RAG compliance chat endpoint.

    Two modes, one endpoint:
      • General mode  — TF-IDF keyword overlap over the 7 regulation chunks.
      • Ticket mode   — payload carries `incident_id`; the incident's full reasoning
                        chain (from incidents.json) is loaded as EXTERNAL knowledge
                        and joined with the regulation chunks its route maps to.
                        Tickets are NOT vectorized — fetched by id (retrieval by
                        selection), so a new ticket needs no re-indexing.
    LLM: same gateway as agents (Anthropic API → rule-based fallback).
    Scope guard: system prompt restricts answers to solar O&M / EU regulation domain.
    """
    from agents import rag_compliance

    q = (payload.get("query") or "").strip()
    if not q:
        return {"answer": "Please send a question."}

    # ── conversation memory: prior turns, sanitized + capped to last 10 ─────────
    raw_hist = payload.get("history") or []
    history = [
        {"role": m["role"], "content": str(m["content"])[:2000]}
        for m in raw_hist
        if isinstance(m, dict) and m.get("role") in ("user", "assistant") and m.get("content")
    ][-10:]
    # the messages list must start with a user turn (Anthropic requirement)
    while history and history[0]["role"] != "user":
        history.pop(0)

    # ── ticket mode: load the incident as external knowledge (no vector DB) ─────
    incident_id = (payload.get("incident_id") or "").strip()
    ticket_ctx = ""
    ticket_route, ticket_subtype = "", ""
    if incident_id:
        try:
            incs = json.loads((OUT / "incidents.json").read_text())
        except Exception:
            incs = []
        inc = next((i for i in incs if i.get("id") == incident_id), None)
        if inc:
            ticket_route   = inc.get("route", {}).get("route", "")
            ticket_subtype = inc.get("subtype", "")
            ticket_ctx = (
                f"TICKET {incident_id} — {inc.get('inverter','')}\n"
                f"Subject: {inc.get('subject','')}\n"
                f"Classification: {inc.get('classification','')} / {ticket_subtype} "
                f"(severity {inc.get('severity','')})\n"
                f"Why classified this way: {inc.get('reasoning','')}\n"
                f"PR inverter {inc.get('pr_inverter','?')} vs fleet {inc.get('pr_fleet','?')}; "
                f"lost {inc.get('lost_kwh','?')} kWh ≈ €{inc.get('eur_impact','?')}.\n"
                f"Routed to: {ticket_route} "
                f"(confidence {inc.get('route',{}).get('confidence','?')}, "
                f"decided by {inc.get('route',{}).get('by','?')}). "
                f"Reason: {inc.get('route',{}).get('note','')}\n"
                f"Proposed action draft:\n{inc.get('draft','')}\n"
                f"Compliance note: {inc.get('compliance_note','')}\n"
                f"Similar past tickets: {'; '.join(inc.get('similar_tickets', []) or ['none'])}"
            )

    # ── retrieval: keyword overlap scoring (stop-word filtered) ─────────────────
    import re
    _STOP = {"the","a","an","is","are","was","were","be","been","being","have","has",
             "had","do","does","did","will","would","shall","should","may","might",
             "can","could","of","in","on","at","to","for","with","and","or","but",
             "not","this","that","it","its","tell","me","give","show","explain",
             "about","please","hi","hello","hey","what","when","where","who","how","why"}
    tokens = {t for t in re.findall(r"[a-zäöüß§0-9]+", q.lower()) if t not in _STOP and len(t) > 2}
    scored: list[tuple[int, str, str]] = []
    for key, text in rag_compliance._CORPUS.items():
        chunk_tokens = set(re.findall(r"[a-zäöüß§0-9]+", text.lower()))
        overlap_count = len(tokens & chunk_tokens)
        scored.append((overlap_count, key, text))
    scored.sort(key=lambda x: -x[0])
    top_chunks = "\n\n---\n\n".join(text for cnt, _k, text in scored[:3] if cnt >= 1)

    # ticket mode: prefer the regulation chunks the incident's route already maps to
    if ticket_ctx:
        route_chunks = rag_compliance.retrieve(ticket_route, ticket_subtype)
        top_chunks = route_chunks + ("\n\n---\n\n" + top_chunks if top_chunks else "")

    # ── system prompt — domain guard + grounding ───────────────────────────────
    SYSTEM = (
        "You are beyondWatt RAG, a compliance assistant for German solar O&M operators. "
        "You speak conversationally, like a knowledgeable colleague explaining things in chat.\n\n"
        "## Response Style\n"
        "- Write in natural prose, full sentences grouped into paragraphs. Never robotic.\n"
        "- Default: one short paragraph, 2 to 4 sentences.\n"
        "- If the answer is genuinely long, break it into at least 2 paragraphs separated by a blank line. "
        "Never one giant wall of text.\n"
        "- Greetings: 1 to 2 sentences. Say who you are and what you cover.\n"
        "- No bullet lists or numbered lists unless the user explicitly asks for a breakdown.\n"
        "- Use the retrieved regulation excerpts to answer accurately. Cite the reference inline, "
        "e.g. 'under §15 EEG' or 'per IEC 61724', never as a separate list.\n"
        "- If the excerpts don't cover it, say so honestly. Don't invent regulation text.\n\n"
        "## Formatting (strict)\n"
        "- Plain text only. No markdown whatsoever.\n"
        "- No #, *, _, +, backticks, >, |, or any header/bold/italic markers.\n"
        "- No em dashes or en dashes. Use a comma, period, or rephrase.\n"
        "- No semicolons. Use a period or a conjunction like 'and' or 'but'.\n\n"
        "## Language\n"
        "- No filler openers: 'Great question', 'Absolutely', 'Certainly'. Just answer.\n"
        "- No hedging: 'it's worth noting', 'it should be mentioned'. Be direct.\n"
        "- Use contractions: 'don't' not 'do not'. Use simple words: 'use' not 'utilize'.\n\n"
        "## Scope\n"
        "You cover: EEG §15, Redispatch 2.0, §14a EnWG, BNetzA MaStR, NIS2, EU AI Act, "
        "IEC 61724, string faults, soiling, inverter monitoring, beyondWatt features. "
        "Out-of-scope questions: one sentence saying you only cover solar O&M and German energy regulations.\n\n"
        + ("## Ticket context\n"
           "The user is asking about a specific incident ticket, shown below. "
           "Explain in plain language why beyondWatt classified it this way (use the PR gap and reasoning), "
           "and why the proposed action fits, tying it to the regulation that applies. "
           "Talk about THIS ticket's facts, not generic theory.\n\n" if ticket_ctx else "")
        + "Reply in English unless the user writes in German."
    )

    llm_user = (
        f"User question: {q}\n\n"
        + (f"Incident ticket under discussion:\n{ticket_ctx}\n\n" if ticket_ctx else "")
        + (f"Relevant regulation excerpts:\n{top_chunks}" if top_chunks
           else "No specific regulation matched — answer from general solar O&M knowledge only.")
    )

    # ── LLM call (Bedrock → Anthropic → rule-based) ───────────────────────────
    import importlib.util, sys
    _proto = Path.home() / "Documents/Projects/Re-new/prototype"
    try:
        spec = importlib.util.spec_from_file_location("bw_llm", _proto / "graph/llm.py")
        mod  = importlib.util.module_from_spec(spec)
        sys.modules.setdefault("bw_llm", mod)
        spec.loader.exec_module(mod)
        _max = 500 if ticket_ctx else 300  # ticket explanations run longer
        answer = _clean_chat(mod.call_llm(SYSTEM, llm_user, max_tokens=_max, history=history))
        if len(answer) < 10:
            raise ValueError("too short")
    except Exception:
        answer = _chat_fallback(q, top_chunks)

    return {"answer": answer, "chunks_used": [k for cnt, k, _ in scored[:3] if cnt > 0]}


def _clean_chat(text: str) -> str:
    """Strip markdown/special chars but keep plain-prose paragraphs."""
    import re
    t = text.strip()
    # unwrap paired markers: **x**, *x*, __x__, _x_, ++x++, `x`
    for pat in [r"\*{1,3}", r"_{1,2}", r"\+{2}", r"`"]:
        t = re.sub(pat + r"([^*_+`\n]{1,300})" + pat, r"\1", t)
    # strip headers + stray leftover symbols
    t = re.sub(r"#{1,6}\s*", "", t)
    t = re.sub(r"[*_+`]{1,3}", "", t)
    t = re.sub(r"-{2,}", "", t)
    # strip bullet / numbered list prefixes -> plain lines
    t = re.sub(r"^\s*[-•]\s+", "", t, flags=re.MULTILINE)
    t = re.sub(r"^\s*\d+\.\s+", "", t, flags=re.MULTILINE)
    # collapse 3+ newlines to a paragraph break; single newline -> space
    t = re.sub(r"\n{2,}", "\n\n", t)
    t = re.sub(r"(?<!\n)\n(?!\n)", " ", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    return t.strip()


def _chat_fallback(q: str, chunks: str) -> str:
    """Rule-based answer when LLM is unavailable — grounded in corpus text."""
    ql = q.lower()
    if any(w in ql for w in ["hi", "hello", "hey", "hallo", "help", "what can"]):
        return (
            "Hello! I'm the beyondWatt compliance assistant. "
            "I can answer questions about: EEG §15 curtailment compensation, "
            "Redispatch 2.0, §14a EnWG storage dimming, BNetzA MaStR reporting, "
            "NIS2 incident notification, EU AI Act advisory posture, "
            "IEC 61724 performance ratio, string faults, soiling, and forecasting. "
            "What would you like to know?"
        )
    if not chunks:
        return (
            "I'm specialized in German solar O&M compliance. "
            "Try asking about EEG §15, Redispatch 2.0, §14a EnWG, MaStR, "
            "NIS2, EU AI Act, performance ratio, or inverter faults."
        )
    # return first two sentences of best chunk as a grounded fallback
    sentences = [s.strip() for s in chunks.split(".") if len(s.strip()) > 20][:3]
    return ". ".join(sentences) + "."


@app.get("/", response_class=HTMLResponse)
def index():
    return (HERE / "index.html").read_text()


@app.get("/draft", response_class=HTMLResponse)
def draft():
    return (HERE / "dashboard.html").read_text()
