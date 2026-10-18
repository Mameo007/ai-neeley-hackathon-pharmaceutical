"""Pharmer demo UI (Streamlit): start a run, watch it, name the Sharpest Needle, get the PDF.

A thin screen over orchestrator.py. `start` and `finish` run in a background thread so the page
never blocks; the page reads progress from runs/<run_id>/pipeline.json. The helpers above
`main()` hold the logic and render nothing, so tests import this module without a page.

Run: .venv/bin/streamlit run app.py
"""
import asyncio
import html
import json
import logging
import re
import threading
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

import orchestrator
from pharmer import librarian, report, scoring
from pharmer import synthesizer as syn

REPO_ROOT = Path(__file__).resolve().parent
PRODUCTS_PATH = REPO_ROOT / "data" / "products.json"  # from the case appendix, hand-curated
THREADS_PATH = REPO_ROOT / "data" / "starter_threads.json"  # appendix starter threads, verbatim
SAMPLE_DIR = REPO_ROOT / "data" / "sample"
NOT_CONFIRMED = "Not confirmed"
NEW_RUN = ""  # the run picker's "start a new run" entry
POLL_SECONDS = 2

BADGE_COLORS = {"High": "green", "Medium": "orange", "Low": "red"}
STAGE_COLORS = {"ok": "green", "running": "blue", "skipped": "gray", "degraded": "orange",
                "failed": "red"}
PHASE_LABELS = {"running": "running", "awaiting_human": "needs your pick", "done": "done",
                "failed": "failed", "interrupted": "interrupted"}

# pipeline.json stage name -> (group, agent, one-line job), in run order. Stages that share an
# agent name share one card (scoring runs twice).
STAGE_AGENTS = {
    "corpus": ("Collect", "Librarian", "Pulls the company's filings into one source library."),
    "hunters": ("Hunt", "The hunters", "Six specialists read every page for what changed."),
    "dedup": ("Check", "Dedup", "Merges findings that say the same thing."),
    "fact_checker": ("Check", "Fact Checker",
                     "Matches every quote word for word against the filing."),
    "quant": ("Check", "Quant", "Puts a dollar and earnings-per-share number on each finding."),
    "priced_in": ("Check", "Priced-In Detector",
                  "Checks whether Wall Street is already talking about it."),
    "scoring": ("Rank", "Scoring", "Ranks findings by impact and by how hidden they are."),
    "peer": ("Check", "Peer Agent", "Compares each finding with rivals' filings."),
    "scoring_after_peer": ("Rank", "Scoring",
                           "Ranks findings by impact and by how hidden they are."),
    "comparison": ("After your pick", "Comparison Builder",
                   "Lines your product up against its rivals."),
    "synthesizer": ("After your pick", "Synthesizer", "Writes the verdict on what limits growth."),
    "ceo_move": ("After your pick", "CEO Move", "Turns the needle into one move for the CEO."),
    "report": ("After your pick", "Report", "Writes the PDF, every number footnoted."),
}
FINISH_STAGE_NAMES = ("comparison", "synthesizer", "ceo_move", "report")
HUNTER_NAMES = {"redliner": "Redliner", "channel_checker": "Channel Checker",
                "footnote_miner": "Footnote Miner", "gross_to_net": "Gross-to-Net",
                "table_archaeologist": "Table Archaeologist", "label_lawyer": "Label Lawyer"}
CARD_STATES = {"waiting": "waiting", "running": "working", "ok": "✓", "skipped": "not built",
               "degraded": "problem", "failed": "error", "stopped": "stopped",
               "not_reached": "not reached"}
CARD_PROBLEMS = {"skipped": "Not built yet, so this step was skipped.",
                 "stopped": "Stopped mid-step when the job ended (e.g. the app restarted).",
                 "not_reached": "The run ended before this step.",
                 "degraded": "Finished, but with a problem. See Details below.",
                 "failed": "Stopped with an error. See Details below."}

# The theme's colours are in .streamlit/config.toml; this adds what the theme can't set: the
# amber monospace masthead, the header bar, panel-coloured cards and monospace numbers.
CSS = """<style>
:root { --pharmer-mono: 'SF Mono', Menlo, 'JetBrains Mono', monospace; }
[data-testid="stHeader"] { background: #0d1117; border-bottom: 1px solid #232b37; }
[data-testid="stMainBlockContainer"] { padding-top: 4.5rem; }  /* header bar + a little air */
h1 { font-family: var(--pharmer-mono) !important; color: #ffb020 !important;
     letter-spacing: 3px; font-size: 1.7rem !important; padding-bottom: 0 !important; }
h2, h3 { letter-spacing: -0.3px; }
[class*="st-key-card-"], details { background: #11161d; }
[data-testid="stCaptionContainer"], [data-testid="stMetricValue"], code {
  font-variant-numeric: tabular-nums; }
[data-testid="stMetricValue"] { font-family: var(--pharmer-mono); }
[data-testid^="stBaseButton-primary"], [data-testid^="stBaseButton-primary"] p {
  color: #111 !important; font-weight: 700; }
/* Company tiles: the tile's own button is stretched invisibly over it, so the whole tile clicks. */
[class*="st-key-tile-"] { position: relative; min-height: 236px; background: #11161d;
  border-radius: 12px; }
[class*="st-key-tile-"]:hover { border-color: #3a4556; }
[class*="st-key-tile-"]:has(button:focus-visible) { outline: 2px solid #ffb020; }
[class*="st-key-pick-"] { position: absolute !important; inset: 0; z-index: 2; }
[class*="st-key-pick-"] [data-testid="stButton"], [class*="st-key-pick-"] button {
  width: 100%; height: 100%; opacity: 0; cursor: pointer; }
.pharmer-tile { display: flex; flex-direction: column; gap: 10px; }
.pharmer-tile .row { display: flex; justify-content: space-between; align-items: center; }
.pharmer-tile .name { font-size: 1.15rem; font-weight: 650; color: #e8eaed; }
.pharmer-tile .thread { color: #8a94a3; font-size: 0.9rem; line-height: 1.45; }
.pharmer-ticker { font-family: var(--pharmer-mono); font-size: 0.8rem; font-weight: 700;
  letter-spacing: 1px; background: #1d2632; color: #ffb020; border-radius: 5px; padding: 3px 7px; }
.pharmer-pill { font-family: var(--pharmer-mono); font-size: 0.72rem; border-radius: 99px;
  padding: 3px 9px; background: rgba(53, 208, 127, .14); color: #35d07f; }
.pharmer-sub { color: #8a94a3; font-size: 1.05rem; margin-top: -0.6rem; }
/* Agent cards */
.pharmer-run-head { display: flex; align-items: center; gap: 14px; }
.pharmer-run-head .title { font-size: 1.9rem; font-weight: 650; letter-spacing: -0.5px; }
.pharmer-bar { height: 6px; border-radius: 99px; background: #232b37; margin: 14px 0 18px; }
.pharmer-bar div { height: 100%; border-radius: 99px;
  background: linear-gradient(90deg, #35d07f, #ffb020); }
.pharmer-cards { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; }
.pharmer-card { background: #11161d; border: 1px solid #232b37; border-radius: 12px;
  padding: 16px 18px; display: flex; flex-direction: column; gap: 6px; }
.pharmer-card .group { font-family: var(--pharmer-mono); font-size: 0.72rem; letter-spacing: 2px;
  text-transform: uppercase; color: #8a94a3; }
.pharmer-card .head { display: flex; justify-content: space-between; align-items: baseline; }
.pharmer-card .name { font-size: 1.1rem; font-weight: 650; }
.pharmer-card .state { font-family: var(--pharmer-mono); font-size: 0.8rem; color: #8a94a3; }
.pharmer-card .job { color: #b4bcc8; font-size: 0.9rem; line-height: 1.4; }
.pharmer-card .who { color: #8a94a3; font-size: 0.8rem; }
.pharmer-card .found { font-family: var(--pharmer-mono); font-size: 0.8rem; color: #35d07f; }
.pharmer-card .problem { font-size: 0.85rem; color: #ff5d5d; }
.pharmer-card.waiting, .pharmer-card.skipped, .pharmer-card.not_reached { opacity: 0.45; }
.pharmer-card.skipped .problem, .pharmer-card.not_reached .problem { color: #8a94a3; }
.pharmer-card.stopped { border-color: #ff5d5d; }
.pharmer-card.stopped .state { color: #ff5d5d; }
@media (max-width: 1000px) { .pharmer-cards { grid-template-columns: repeat(2, 1fr); } }
/* Result screen */
.st-key-needle-card, .st-key-ceo-card { background: #11161d; border: 1px solid #232b37;
  border-radius: 14px; padding: 26px 30px; }
.st-key-ceo-card { background: #161c25; }
.pharmer-label { font-family: var(--pharmer-mono); font-size: 0.8rem; letter-spacing: 2.5px;
  color: #ffb020; text-transform: uppercase; }
.pharmer-headline { font-size: 1.9rem; font-weight: 650; line-height: 1.25; margin-top: 8px;
  letter-spacing: -0.3px; }
.pharmer-number-caption { color: #8a94a3; font-size: 0.95rem; margin-top: -6px; }
.pharmer-verdict { font-size: 1.05rem; line-height: 1.5; }
.pharmer-chips { display: flex; flex-wrap: wrap; gap: 8px; margin: 10px 0 4px; }
.pharmer-chip { font-family: var(--pharmer-mono); font-size: 0.75rem; border-radius: 99px;
  padding: 4px 11px; background: #1b222c; color: #8a94a3; }
.pharmer-chip.g { background: rgba(53, 208, 127, .14); color: #35d07f; }
.pharmer-chip.a { background: rgba(255, 176, 32, .14); color: #ffb020; }
.pharmer-chip.r { background: rgba(255, 93, 93, .14); color: #ff5d5d; }
.pharmer-quote { font-family: Georgia, 'Times New Roman', serif; font-size: 1.15rem;
  line-height: 1.5; border-left: 3px solid #ffb020; background: #0d1117; padding: 14px 20px;
  border-radius: 0 10px 10px 0; margin: 12px 0 8px; color: #e8eaed; }
.pharmer-source { display: inline-block; font-family: var(--pharmer-mono); font-size: 0.78rem;
  color: #4ea1ff; border: 1px solid #23406a; border-radius: 6px; padding: 4px 10px; }
.pharmer-ceo .title { font-size: 1.35rem; font-weight: 650; margin: 6px 0 4px; }
.pharmer-ceo .row { border-top: 1px solid #232b37; padding: 11px 0; }
.pharmer-ceo .key { font-family: var(--pharmer-mono); font-size: 0.72rem; letter-spacing: 2px;
  color: #8a94a3; text-transform: uppercase; }
.pharmer-ceo .val { font-size: 0.98rem; line-height: 1.45; margin-top: 3px; }
.pharmer-why { font-size: 0.92rem; color: #b4bcc8; }
.pharmer-card.ok .state { color: #35d07f; font-size: 1.1rem; }
.pharmer-card.running { border: 2px solid #ffb020; background: #17140c;
  animation: pharmer-pulse 1.6s ease-in-out infinite; }
.pharmer-card.running .state { color: #ffb020; }
.pharmer-card.degraded, .pharmer-card.failed { border-color: #ff5d5d; }
.pharmer-card.degraded .state, .pharmer-card.failed .state { color: #ff5d5d; }
@keyframes pharmer-pulse { 0%, 100% { box-shadow: 0 0 0 0 rgba(255,176,32,.25); }
  50% { box-shadow: 0 0 0 7px rgba(255,176,32,0); } }
.pharmer-after { margin-top: 14px; color: #b4bcc8; font-size: 0.95rem; }
.pharmer-after span { font-family: var(--pharmer-mono); font-size: 0.8rem; color: #8a94a3;
  background: #1b222c; border-radius: 99px; padding: 3px 10px; margin-right: 8px; }
</style>"""


# --- helpers (no rendering) ------------------------------------------------------------------

_MARKDOWN = re.compile(r"([\\`*_{}\[\]()#+\-.!|$<>~])")


def md(text):
    """Filing text made literal for st.markdown: "$1.2 billion" must not become LaTeX, and no
    "*", "_" or "[" in a quote may turn into formatting (quotes are shown verbatim)."""
    return _MARKDOWN.sub(r"\\\1", str(text))


def mark_interrupted(run_dir):
    """Record a run whose job died with the server (phase still "running", no live thread) as
    failed, so a finished `start` can be finished again. Only on the analyst's request."""
    status = orchestrator.read_status(run_dir)
    for entry in status.get("stages", {}).values():
        if entry.get("status") == "running":
            entry.update(status="failed", error="interrupted (app restarted)")
    status["phase"] = "failed"
    orchestrator._save(run_dir, status)


def load_products(path=PRODUCTS_PATH):
    """{ticker: [{"product", "rivals"}]} from data/products.json; {} if the file is missing."""
    data = syn.read_json_or_none(path)
    return data if isinstance(data, dict) else {}


def load_threads(path=THREADS_PATH):
    """{ticker: starter thread} from data/starter_threads.json; {} if the file is missing."""
    data = syn.read_json_or_none(path)
    return data if isinstance(data, dict) else {}


def company_tiles(threads=None, sample_dir=SAMPLE_DIR):
    """One tile per shortlist company, in librarian.CIKS order:
    [{"ticker", "name", "thread", "ready"}]; "ready" = a prebuilt corpus is on disk."""
    threads = load_threads() if threads is None else threads
    return [{"ticker": t, "name": name, "thread": threads.get(t, ""),
             "ready": prebuilt_corpus(t, sample_dir) is not None}
            for t, (_, name) in librarian.CIKS.items()]


def default_company(tiles):
    """The tile selected on first load: the first with filings ready (the company the demo
    uses), else the first tile."""
    return next((t["ticker"] for t in tiles if t["ready"]), tiles[0]["ticker"])


def tile_html(tile):
    """A tile's text as HTML (every value escaped; the click target is a separate button)."""
    pill = '<span class="pharmer-pill">● filings ready</span>' if tile["ready"] else ""
    return (f'<div class="pharmer-tile"><div class="row"><span class="pharmer-ticker">'
            f'{html.escape(tile["ticker"])}</span>{pill}</div>'
            f'<div class="name">{html.escape(tile["name"])}</div>'
            f'<div class="thread">{html.escape(tile["thread"])}</div></div>')


def product_options(ticker, products=None):
    """The product dropdown for a ticker: "Not confirmed" first, then its appendix products."""
    products = load_products() if products is None else products
    return [NOT_CONFIRMED] + [p["product"] for p in products.get(ticker, [])]


def prebuilt_corpus(ticker, sample_dir=SAMPLE_DIR):
    """data/sample/<TICKER> when it holds a complete corpus of that ticker, else None."""
    folder = Path(sample_dir) / ticker
    if not all((folder / name).is_file() for name in orchestrator.CORPUS_FILES):
        return None
    return folder if ticker in orchestrator._corpus_tickers(folder) else None


def list_runs(runs_dir=None, live=frozenset()):
    """Run folders newest first: [{"name", "path", "ticker", "phase", "label"}].

    A run left at phase "running" with no live job in this server (e.g. after a restart) is
    shown as "interrupted" so nobody waits on it.
    """
    runs_dir = Path(runs_dir or orchestrator.RUNS_DIR)
    out = []
    for path in runs_dir.glob("*") if runs_dir.is_dir() else []:
        status = syn.read_json_or_none(path / "pipeline.json")
        if not path.is_dir() or not isinstance(status, dict):
            continue
        phase = status.get("phase")
        if phase == "running" and str(path) not in live:
            phase = "interrupted"
        out.append({"name": path.name, "path": path, "ticker": status.get("ticker"),
                    "phase": phase, "started_at": status.get("started_at") or "",
                    "label": f"{path.name} – {PHASE_LABELS.get(phase, phase)}"})
    return sorted(out, key=lambda r: (r["started_at"], r["name"]), reverse=True)


def breakdown(reasoning):
    """The "why selected" numbers for one reasoning (§5.1.6), from the scoring functions."""
    hits = (reasoning.get("priced_in") or {}).get("hits")
    return {"materiality": scoring.materiality(reasoning),
            "obscurity": round(scoring.obscurity(reasoning), 4),
            "score": scoring.score(reasoning),
            "priced_in_hits": hits,
            "hunters": reasoning.get("hunters") or [reasoning.get("hunter")],
            "confidence_reason": reasoning.get("confidence_reason")}


def review(run_dir):
    """(report model, rows) for the review screen: the report's numbered rows and sources, each
    reasoning extended with its score breakdown."""
    model = report.build_model(run_dir)
    findings = syn.read_json_or_none(Path(run_dir) / "findings.json") or []
    by_id = {r["reasoning_id"]: r for f in findings for r in f.get("reasonings") or []}
    sources = {n["number"]: n["text"] for n in model["footnotes"]}
    rows = []
    for row in model["findings"]:
        reasonings = []
        for r in row["reasonings"]:
            quotes = [{"text": q["text"], "sources": [sources[n] for n in q["notes"]]}
                      for q in r["quotes"]]
            reasonings.append({**r, "quotes": quotes,
                               **breakdown(by_id.get(r["reasoning_id"], {}))})
        rows.append({**row, "reasonings": reasonings})
    return model, rows


def default_needle(run_dir, rows):
    """The radio's starting choice: run.json's pick if still selected, else Recommended."""
    ids = [row["finding_id"] for row in rows]
    run_info = syn.read_json_or_none(Path(run_dir) / "run.json")
    pick = run_info.get("sharpest_needle") if isinstance(run_info, dict) else None
    if pick in ids:
        return pick
    return next((row["finding_id"] for row in rows if "Recommended" in row["labels"]),
                ids[0] if ids else None)


def default_product(run_dir, options):
    run_info = syn.read_json_or_none(Path(run_dir) / "run.json")
    product = run_info.get("product") if isinstance(run_info, dict) else None
    return product if product in options else NOT_CONFIRMED


def _card_status(statuses, live=True):
    """One card's status from its stages' pipeline.json statuses (None = not started yet).
    With no live job, a stage left "running" stopped and a stage never started was not reached."""
    for bad in ("failed", "degraded"):
        if bad in statuses:
            return bad
    if "running" in statuses:
        return "running" if live else "stopped"
    if all(s is None for s in statuses):
        return "waiting" if live else "not_reached"
    if all(s == "skipped" for s in statuses):
        return "skipped"
    return "ok"  # also between Scoring's two passes: the first one is done


def _n(count, noun):
    """"1 lead" / "3 leads"."""
    return f"{count} {noun}" + ("" if count == 1 else "s")


def _discovery(name, run_dir):
    """A done card's latest discovery: counts read from the run's own files, or ""."""
    run_dir = Path(run_dir)
    if name == "Librarian":
        docs = syn.read_json_or_none(run_dir / "manifest.json")
        return f"{_n(len(docs), 'document')} in the library" if isinstance(docs, list) else ""
    if name == "The hunters":
        leads = syn.read_json_or_none(run_dir / "leads.json")
        if not isinstance(leads, list):
            return ""
        counts = {}
        for lead in leads:
            first = ((lead.get("reasonings") or [{}]) if isinstance(lead, dict) else [{}])[0]
            hunter = (first.get("hunter") if isinstance(first, dict) else None) or "unknown"
            counts[hunter] = counts.get(hunter, 0) + 1
        parts = [f"{HUNTER_NAMES.get(h, h)} {n}" for h, n in counts.items()]
        return _n(len(leads), "lead") + (": " + ", ".join(parts) if parts else "")
    findings = syn.read_json_or_none(run_dir / "findings.json")
    if not isinstance(findings, list):
        return ""
    findings = [f for f in findings if isinstance(f, dict)]
    reasonings = [r for f in findings for r in f.get("reasonings") or []]
    if name == "Dedup":
        return _n(len(findings), "distinct finding")
    if name == "Fact Checker":
        rated = [r.get("confidence") for r in reasonings if r.get("confidence")]
        if not rated:
            return ""
        return " · ".join(f"{level} {rated.count(level)}" for level in ("High", "Medium", "Low")
                          if level in rated)
    if name == "Quant":
        sized = sum(1 for f in findings
                    if any((r.get("impact") or {}).get("usd_m") is not None
                           for r in f.get("reasonings") or []))
        return f"{sized} of {len(findings)} findings sized in dollars"
    if name == "Scoring":
        selected = sum(1 for f in findings if f.get("status") == "selected")
        return f"{_n(selected, 'finding')} selected for your pick"
    return ""


def agent_cards(status, run_dir, live=True, every=False):
    """The agent cards for one run, in run order: [{"group", "name", "job", "status", "state",
    "who", "found", "problem"}]. After-your-pick cards appear once the finish phase started
    (every=True: always). live=False: no job is working on the run any more."""
    stages = status.get("stages", {})
    finishing = every or any(name in stages for name in FINISH_STAGE_NAMES)
    by_name = {}
    for stage, (group, name, job) in STAGE_AGENTS.items():
        if stage in orchestrator.DISABLED_STAGES:
            continue  # not built, never scheduled: it is not one of this run's agents
        if stage in FINISH_STAGE_NAMES and not finishing:
            continue
        card = by_name.setdefault(name, {"group": group, "name": name, "job": job,
                                         "stages": []})
        card["stages"].append((stages.get(stage) or {}).get("status"))
    cards = []
    for card in by_name.values():
        state = _card_status(card.pop("stages"), live)
        if (state == "not_reached" and card["group"] == "After your pick"
                and status.get("phase") == "awaiting_human"):
            state = "waiting"  # paused for the analyst's pick: these steps still lie ahead
        cards.append({**card, "status": state, "state": CARD_STATES[state],
                      "who": ", ".join(HUNTER_NAMES.values()) if card["name"] == "The hunters"
                      else "",
                      "found": _discovery(card["name"], run_dir) if state == "ok" else "",
                      "problem": CARD_PROBLEMS.get(state, "")})
    return cards


def progress_fraction(cards):
    """Share of cards whose step ended (done, skipped or with a problem), for the progress bar;
    a stopped or never-reached step does not count."""
    done = sum(1 for c in cards if c["status"] in ("ok", "skipped", "degraded", "failed"))
    return done / len(cards) if cards else 0.0


def cards_html(cards):
    """The card grid as HTML (every value escaped)."""
    out = []
    for c in cards:
        e = {k: html.escape(str(v)) for k, v in c.items()}
        out.append(f'<div class="pharmer-card {e["status"]}"><div class="group">{e["group"]}</div>'
                   f'<div class="head"><span class="name">{e["name"]}</span>'
                   f'<span class="state">{e["state"]}</span></div>'
                   f'<div class="job">{e["job"]}</div>'
                   + (f'<div class="who">{e["who"]}</div>' if c["who"] else "")
                   + (f'<div class="found">↳ {e["found"]}</div>' if c["found"] else "")
                   + (f'<div class="problem">{e["problem"]}</div>' if c["problem"] else "")
                   + "</div>")
    return '<div class="pharmer-cards">' + "".join(out) + "</div>"


def _num(value, decimals=1):
    """1234.5 -> "1,234.5"; 420.0 -> "420" (no trailing zeros)."""
    text = f"{value:,.{decimals}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _signed(value):
    return ("−" if value < 0 else "+") + _num(abs(value), 2)


def anchor_number(anchor):
    """The anchor's value as reported, e.g. "DKK 87,123m"; None when it has no number."""
    anchor = anchor or {}
    value = anchor.get("value")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    currency = anchor.get("currency")
    currency = "" if not currency or str(currency).lower() == "none" else f"{currency} "
    unit = anchor.get("unit") or ""
    short = {"millions": "m", "billions": "bn", "thousands": "k", "percent": "%"}.get(unit)
    return (f"{currency}{_num(value, 2)}{short}" if short is not None
            else f"{currency}{_num(value, 2)} {unit}".strip())


def big_number(reasoning, anchor):
    """The result's one big number: Quant's impact.usd_m, else impact.eps_pct, else the
    finding's anchor value as reported. {"text", "caption", "basis"}, or None — never made up."""
    impact = reasoning.get("impact") or {}
    usd, eps = impact.get("usd_m"), impact.get("eps_pct")
    if usd is not None:
        sign, usd_abs = "−" if usd < 0 else "+", round(abs(usd), 1)
        text = (f"{sign}${_num(usd_abs / 1000)}bn" if usd_abs >= 1000
                else f"{sign}${_num(usd_abs)}m")
        return {"text": text, "basis": "quant",
                "caption": "estimated impact, US dollars"
                           + (f" · {_signed(eps)}% of earnings per share" if eps is not None
                              else "")}
    if eps is not None:
        return {"text": f"{_signed(eps)}%", "basis": "quant",
                "caption": "estimated impact on earnings per share"}
    text = anchor_number(anchor)
    if text is None:
        return None
    what = ", ".join(str(anchor[k]).replace("_", " ") for k in ("metric", "fiscal_period")
                     if anchor.get(k))
    return {"text": text, "basis": "anchor",
            "caption": f"{what} · as reported; impact not sized yet"}


def _strongest(finding):
    """The reasoning scoring ranks the finding by (highest score), else the lead reasoning;
    only selected reasonings count when the finding has any."""
    reasonings = [r for r in finding.get("reasonings") or [] if isinstance(r, dict)]
    reasonings = [r for r in reasonings if r.get("selected") is True] or reasonings
    scored = [r for r in reasonings if isinstance(r.get("score"), (int, float))]
    if scored:
        return max(scored, key=lambda r: r["score"])
    return next((r for r in reasonings if r.get("reasoning_id") == finding.get("lead_reasoning_id")),
                reasonings[0] if reasonings else {})


CEO_LABELS = {"Action": "The move", "Trade-off": "Trade-off",
              "Downside if wrong": "What would prove us wrong", "Why it works": "Why it works"}


def result_model(run_dir):
    """Everything the result screen shows, from the run's files via report.build_model:
    {"needle", "headline", "number", "verdict", "priced_in", "badge", "quote", "ceo"}."""
    model = report.build_model(run_dir)
    findings = syn.read_json_or_none(Path(run_dir) / "findings.json") or []
    needle = model["thesis"]["needle"]
    finding = next((f for f in findings if isinstance(f, dict) and f.get("finding_id") == needle),
                   {})
    reasoning = _strongest(finding) if finding else {}
    row = next((r for r in model["findings"] if r["finding_id"] == needle), None)
    shown = None
    if row and row["reasonings"]:
        shown = next((r for r in row["reasonings"]
                      if r["reasoning_id"] == reasoning.get("reasoning_id")), row["reasonings"][0])
    sources = {n["number"]: n["text"] for n in model["footnotes"]}
    quote = None
    if shown and shown["quotes"]:
        q = shown["quotes"][0]
        quote = {"text": q["text"], "source": "; ".join(sources[n] for n in q["notes"])}
    callout = model["ceo_move"]
    fields = dict(callout["fields"])
    return {"needle": needle,
            "headline": reasoning.get("claim") or model["thesis"]["headline"],
            "number": big_number(reasoning, finding.get("anchor")) if finding else None,
            "verdict": model["thesis"]["verdict"], "priced_in": model["thesis"]["priced_in"],
            "verdict_note": next((w for w in model["warnings"]
                                  if w.startswith(("Synthesizer", "synthesis.json"))), None),
            "ai_pick": bool(needle) and model["thesis"]["needle_source"] != "human",
            "badge": shown["badge"] if shown else None, "quote": quote,
            "ceo": {"status": callout["status"], "error": callout["error"],
                    "stale": callout["stale"], "timeframe": fields.get("Timeframe") or "",
                    "fields": [(CEO_LABELS[label], text) for label, text in callout["fields"]
                               if label in CEO_LABELS and text]}}


def quote_html(text, source=None):
    """A verbatim quote as a serif blockquote (escaped, line breaks kept) plus its source chip."""
    body = html.escape(text).replace("\n", "<br>")
    return (f'<div class="pharmer-quote">“{body}”</div>'
            + (f'<span class="pharmer-source">{html.escape(source)}</span>' if source else ""))


def why_line(r):
    """The one-line verdict on why a reasoning was selected, in plain words from its numbers."""
    impact = ("impact not sized yet" if r["materiality"] is None
              else f"moves earnings per share by about {_num(r['materiality'], 2)}%")
    hits = r["priced_in_hits"]
    news = ("not yet checked against the news" if hits is None
            else "no matching news stories found" if hits == 0
            else f"already in {hits} news {'story' if hits == 1 else 'stories'}")
    return f"{impact[0].upper()}{impact[1:]}; {news}."


def usage_error_text(message):
    """A refused start/finish request (orchestrator.UsageError) in one plain sentence."""
    if "already exists" in message:
        return "A run for this company started less than a minute ago. Try again in a minute."
    if "already being processed" in message or "phase 'running'" in message:
        return "This run is already working. Wait for it to finish, then try again."
    if "not a selected finding" in message:
        return "That finding is no longer on the shortlist. Pick another one."
    if "--corpus" in message:
        return "The prebuilt filings for this company are incomplete. Retry without them."
    if "no scored findings.json" in message or "reached the human pause" in message:
        return ("This run never finished reading the filings, so it has no report to write. "
                "Start the company again.")
    return "That request could not be started. Try again."


def used_prebuilt(run_dir, ticker):
    """Whether this run's corpus is a copy of the prebuilt one (the orchestrator copies the
    files as they are, so the manifests are byte-identical)."""
    prebuilt = prebuilt_corpus(ticker) if ticker else None
    mine = Path(run_dir) / "manifest.json"
    return (prebuilt is not None and mine.is_file()
            and mine.read_bytes() == (prebuilt / "manifest.json").read_bytes())


def recovery(phase, status, run_dir, ticker):
    """What went wrong with a run and the one button that recovers it:
    {"text", "label", "action"} with action "start" / "start_prebuilt" / "finish", or None."""
    stages = status.get("stages", {})
    company = librarian.CIKS.get(ticker, (None, ticker or "this company"))[1]
    start_done = (stages.get("scoring_after_peer") or {}).get("status") == "ok"
    finish = {"label": "Retry the report", "action": "finish"}
    if phase == "done":
        if (Path(run_dir) / "report.pdf").is_file():
            return None
        return {"text": "The report step finished without writing the PDF.", **finish}
    if phase != "failed":
        return None
    if start_done:
        return {"text": "The report step stopped before it finished. Retry with the same pick, "
                        "or change it below.", **finish}
    if used_prebuilt(run_dir, ticker):  # keep the offline choice: never switch to live SEC
        return {"text": f"The agents stopped before they finished reading {company}'s filings.",
                "label": "Retry", "action": "start_prebuilt"}
    if (stages.get("corpus") or {}).get("status") == "failed":
        if ticker and prebuilt_corpus(ticker) is not None:
            return {"text": f"Couldn't collect {company}'s filings from SEC.",
                    "label": "Use prebuilt filings", "action": "start_prebuilt"}
        return {"text": f"Couldn't collect {company}'s filings from SEC. Check the connection, "
                        "then retry.", "label": "Retry", "action": "start"}
    return {"text": f"The agents stopped before they finished reading {company}'s filings.",
            "label": "Retry", "action": "start"}


def stage_problems(status):
    """Stages that did not simply succeed, for the warning above the stage table.

    A `disabled` stage is not a problem with this run - its agent is not built and was
    never scheduled - so it raises no warning. Its entry stays in pipeline.json.
    """
    return [(name, e.get("status"), e.get("error")) for name, e in status.get("stages", {}).items()
            if e.get("status") in ("skipped", "degraded", "failed")
            and name not in orchestrator.DISABLED_STAGES]


# --- background jobs --------------------------------------------------------------------------

@st.cache_resource
def _jobs():
    """run folder -> Thread, shared by every session of this server process."""
    return {}


@st.cache_resource
def _launch_lock():
    """Serializes check-then-launch across sessions (two tabs pressing the same button)."""
    return threading.Lock()


def _job(run_dir, make_coroutine):
    """Thread body: a crash outside the stages still ends the run as "failed", with the error
    in pipeline.json, instead of leaving it at "running"."""
    try:
        asyncio.run(make_coroutine())
    except BaseException as exc:
        logging.getLogger("app").exception("job for %s crashed", run_dir)
        status = orchestrator.read_status(run_dir)
        status.update(phase="failed", error=f"{type(exc).__name__}: {exc}"[:500])
        orchestrator._save(run_dir, status)


def live_runs():
    return frozenset(path for path, thread in _jobs().items() if thread.is_alive())


def launch(run_dir, make_coroutine):
    """Run make_coroutine() on its own event loop in a daemon thread."""
    thread = threading.Thread(target=_job, args=(run_dir, make_coroutine), daemon=True,
                              name=f"pharmer-{Path(run_dir).name}")
    _jobs()[str(run_dir)] = thread
    thread.start()


# --- page -------------------------------------------------------------------------------------

def _open(run_name):
    """Switch the picker to run_name on the next rerun (a widget's key can't be set after the
    widget has rendered in this run)."""
    st.session_state["pending_run"] = run_name
    st.rerun()


def past_runs(runs):
    """The run picker, tucked into a "Past runs" expander so the main view stays one screen."""
    labels = {r["name"]: r["label"] for r in runs}
    if "pending_run" in st.session_state:
        st.session_state["run"] = st.session_state.pop("pending_run")
    elif "run" not in st.session_state and st.query_params.get("run") in labels:
        st.session_state["run"] = st.query_params["run"]  # ?run=<name> opens a run directly
    with st.expander(f"Past runs ({len(runs)})"):
        # Names only: a keyed selectbox keeps its first-rendered option labels, so a phase in
        # the label would go stale. Phases are listed below as plain text instead.
        chosen = st.selectbox("Open a run", [NEW_RUN] + list(labels), key="run",
                              format_func=lambda n: n or "➕ New run")
        for r in runs[:15]:
            st.caption(r["label"])
        return chosen


def company_grid(tiles):
    """The 5-wide tile grid; returns the selected ticker (kept in session_state["company"])."""
    if st.session_state.get("company") not in {t["ticker"] for t in tiles}:
        st.session_state["company"] = default_company(tiles)
    chosen = st.session_state["company"]
    st.html(f"<style>.st-key-tile-{chosen} {{ border: 2px solid #ffb020 !important; "
            "background: #17140c !important; box-shadow: 0 0 0 6px rgba(255,176,32,.08); }"
            "</style>")
    for start in range(0, len(tiles), 5):
        for col, tile in zip(st.columns(5), tiles[start:start + 5]):
            with col, st.container(border=True, key=f"tile-{tile['ticker']}"):
                st.html(tile_html(tile))
                label = f"{tile['name']} (selected)" if tile["ticker"] == chosen else tile["name"]
                if st.button(label, key=f"pick-{tile['ticker']}"):
                    st.session_state["company"] = tile["ticker"]
                    st.rerun()
    return chosen


def new_run_form():
    st.header("Pick a company. Our agents read its filings.")
    st.html('<div class="pharmer-sub">Annual and quarterly reports from SEC EDGAR: every '
            "finding comes with its source.</div>")
    tiles = company_tiles()
    ticker = company_grid(tiles)
    name = librarian.CIKS[ticker][1]
    corpus = prebuilt_corpus(ticker)
    use_prebuilt = False
    if corpus is not None:
        use_prebuilt = st.toggle("Use the prebuilt filings", value=False,
                                 help="Use filings already downloaded instead of fetching "
                                      "them from SEC. Works offline.")
    clicked = st.button(f"Find the needle in {name} →", type="primary")
    st.caption("Takes a few minutes. You pick the final finding.")
    corpus = corpus if use_prebuilt else None
    if clicked:
        start_run(ticker, corpus)
    error = st.session_state.get("start_error")
    if error and error["ticker"] != ticker:
        st.session_state.pop("start_error")  # another company picked: the old refusal is moot
    elif error:
        st.error(error["text"])
        if error["existing"]:  # the same company started this minute: open it, don't retry
            if st.button("Open that run", key="open-existing"):
                st.session_state.pop("start_error")
                _open(error["existing"])
        elif st.button("Retry", key="retry-start"):
            start_run(ticker, corpus)


def start_run(ticker, corpus=None):
    """Start a run in the background and open it; a refusal is kept for the Retry button."""
    with _launch_lock():
        try:
            run_dir = orchestrator.prepare_run(ticker, corpus=corpus)
        except orchestrator.UsageError as exc:
            logging.getLogger("app").warning("start refused: %s", exc)
            existing = re.match(r"(\S+) already exists", str(exc))
            st.session_state["start_error"] = {
                "ticker": ticker, "text": usage_error_text(str(exc)),
                "existing": Path(existing.group(1)).name if existing else None}
            return
        st.session_state.pop("start_error", None)
        launch(run_dir, lambda: orchestrator.run_start_stages(run_dir, corpus))
    _open(run_dir.name)


def finish_again(run_dir):
    """Re-run the report step with the pick already in run.json."""
    with _launch_lock():
        try:
            if str(run_dir) in live_runs():
                raise orchestrator.UsageError("this run is already being processed")
            orchestrator.prepare_finish(run_dir)
        except orchestrator.UsageError as exc:
            logging.getLogger("app").warning("report retry refused: %s", exc)
            st.error(usage_error_text(str(exc)))
            return
        launch(run_dir, lambda: orchestrator.run_finish_stages(run_dir))
    _open(Path(run_dir).name)


def recovery_box(run, status):
    """The plain-English error and its one recovery button, for a failed or incomplete run."""
    fix = recovery(run["phase"], status, run["path"], run["ticker"])
    if fix is None:
        return
    st.error(fix["text"])
    if st.button(fix["label"], type="primary", key="recover"):
        if fix["action"] == "finish":
            finish_again(run["path"])
        else:
            start_run(run["ticker"],
                      prebuilt_corpus(run["ticker"]) if fix["action"] == "start_prebuilt" else None)
            refused = st.session_state.pop("start_error", None)  # set only if refused
            if refused:
                st.error(refused["text"])


def stage_warning(status):
    """Plain-English list of agents that didn't simply succeed; raw errors stay in Details."""
    lines = {}
    for name, state, _ in stage_problems(status):
        agent = STAGE_AGENTS.get(name, ("", name, ""))[1]
        lines.setdefault(agent, f"- **{agent}**: {CARD_PROBLEMS[state].split('.')[0].lower()}")
    if lines:
        st.warning("\n".join(lines.values()))


def agents_section(run_dir, status, live=True):
    """Header, progress bar and agent cards for one run (live: a job is working on it)."""
    ticker = status.get("ticker") or ""
    company = librarian.CIKS.get(ticker, (None, ticker))[1]
    cards = agent_cards(status, run_dir, live)
    # The bar counts every card, after-your-pick ones too, so it never jumps backward.
    progress = progress_fraction(agent_cards(status, run_dir, live, every=True))
    st.html(f'<div class="pharmer-run-head"><span class="pharmer-ticker">{html.escape(ticker)}'
            f'</span><span class="title">{html.escape(company)} · '
            f'{"the agents are reading" if live else "what the agents did"}</span>'
            f'</div><div class="pharmer-bar"><div style="width: '
            f'{progress * 100:.0f}%"></div></div>' + cards_html(cards)
            + ("" if any(c["group"] == "After your pick" for c in cards) else
               '<div class="pharmer-after"><span>after your pick</span>Product comparison → '
               "Synthesizer → CEO Move → PDF report</div>"))


def stage_table(status):
    for name, entry in status.get("stages", {}).items():
        cols = st.columns([2, 1, 1, 5])
        cols[0].write(name)
        with cols[1]:
            st.badge(entry.get("status") or "?",
                     color=STAGE_COLORS.get(entry.get("status"), "gray"))
        cols[2].write(f"{entry.get('seconds', 0):.1f}s")
        cols[3].caption(entry.get("error") or "")


def show_progress(run_dir):
    """Stage table refreshed every POLL_SECONDS while the job runs; a full rerun when it ends."""
    @st.fragment(run_every=POLL_SECONDS)
    def progress():
        status = orchestrator.read_status(run_dir)
        if status.get("phase") != "running" or str(run_dir) not in live_runs():
            st.rerun()
        st.info("Working… this page updates by itself.")
        agents_section(run_dir, status)
        with st.expander("Details"):
            stage_table(status)
    progress()


def reasoning_block(r):
    with st.container():
        cols = st.columns([1, 9])
        with cols[0]:
            st.badge(r["badge"], color=BADGE_COLORS.get(r["badge"], "gray"))
        cols[1].markdown(md(r["claim"]) + (f"  \n*{', '.join(r['labels'])}*"
                                           if r["labels"] else ""))
        st.html(f'<div class="pharmer-why">{html.escape(why_line(r))}</div>')
        score = "–" if r["score"] is None else r["score"]
        materiality = "–" if r["materiality"] is None else r["materiality"]
        hits = r["priced_in_hits"]
        with st.expander("How we scored it"):
            # With the Priced-In Detector disabled every obscurity is the unchecked
            # default, so naming it as a factor would claim a check nobody ran.
            basis = (f"Score {score} = materiality {materiality} (|EPS impact %|)"
                     " · market pricing not assessed, so this ranks on materiality alone"
                     if hits is None else
                     f"Score {score} = materiality {materiality} (|EPS impact %|) × obscurity "
                     f"{r['obscurity']} (priced-in hits: {hits})")
            st.caption(basis
                       + f" · raised by {', '.join(h for h in r['hunters'] if h)}"
                       + (f" · confidence: {md(r['confidence_reason'])}"
                          if r.get("confidence_reason") else ""))
        for q in r["quotes"]:
            st.html(quote_html(q["text"], "; ".join(q["sources"])))


def review_section(run_dir, ticker):
    model, rows = review(run_dir)
    st.subheader("Selected findings")
    if model["warnings"]:
        with st.expander(f"Report warnings ({len(model['warnings'])})"):
            for warning in model["warnings"]:
                st.write(warning)
    if not rows:
        st.info("No findings were selected. You can still write the report; it explains why.")
    for row in rows:
        with st.container(border=True, key=f"card-{row['finding_id']}"):
            tags = "".join(f" :violet-badge[{t}]" for t in row["labels"]
                           if t != "Sharpest Needle")
            st.markdown(f"**{row['number']}. {row['finding_id']} · {md(row['anchor'])}**{tags}")
            for r in row["reasonings"]:
                reasoning_block(r)

    st.subheader("Your pick")
    options = product_options(ticker)
    with st.form("finish"):
        needle = None
        if rows:
            ids = [row["finding_id"] for row in rows]
            claims = {row["finding_id"]: row["reasonings"][0]["claim"] for row in rows}
            needle = st.radio("Sharpest Needle", ids,
                              index=ids.index(default_needle(run_dir, rows)),
                              format_func=lambda i: f"{i}: {claims[i][:110]}")
        product = st.selectbox("Product", options,
                               index=options.index(default_product(run_dir, options)))
        rivals = {p["product"]: p["rivals"] for p in load_products().get(ticker, [])}
        st.caption("Rivals per the case appendix: "
                   + ("; ".join(f"{p}: {r}" for p, r in rivals.items()) or "none listed"))
        if st.form_submit_button("Write the report", type="primary"):
            with _launch_lock():
                try:
                    if str(run_dir) in live_runs():
                        raise orchestrator.UsageError("this run is already being processed")
                    orchestrator.prepare_finish(  # "" clears an earlier product choice
                        run_dir, needle=needle, product="" if product == NOT_CONFIRMED else product)
                except orchestrator.UsageError as exc:
                    logging.getLogger("app").warning("finish refused: %s", exc)
                    st.error(usage_error_text(str(exc)))  # the form's button is the retry
                    return
                launch(run_dir, lambda: orchestrator.run_finish_stages(run_dir))
            _open(Path(run_dir).name)


def count_up(text):
    """The big number, counting up from 0 to its final text (which is shown verbatim at the
    end, and straight away for viewers who prefer reduced motion)."""
    components.html(f"""<div id="n">{html.escape(text)}</div>
<style>body {{ margin: 0; background: transparent; }}
#n {{ font: 700 min(76px, 11vw) 'SF Mono', Menlo, 'JetBrains Mono', monospace;
      color: #ffb020; letter-spacing: -2px; line-height: 1.1; white-space: nowrap; }}</style>
<script>
const el = document.getElementById("n"), final = el.textContent;
const m = final.match(/\d[\d,]*(\.\d+)?/);
if (m && !matchMedia("(prefers-reduced-motion: reduce)").matches) {{
  const d = m[1] ? m[1].length - 1 : 0, v = parseFloat(m[0].replace(/,/g, "")), t0 = performance.now();
  const step = (t) => {{
    const k = Math.min(1, (t - t0) / 1200), e = 1 - Math.pow(1 - k, 3);
    el.textContent = k < 1 ? final.replace(m[0], (v * e).toLocaleString("en-US",
      {{minimumFractionDigits: d, maximumFractionDigits: d}})) : final;
    if (k < 1) requestAnimationFrame(step);
  }};
  requestAnimationFrame(step);
}}
</script>""", height=84)


def ceo_html(ceo):
    rows = "".join(f'<div class="row"><div class="key">{html.escape(k)}</div>'
                   f'<div class="val">{html.escape(v)}</div></div>' for k, v in ceo["fields"])
    title = "CEO decision" + (f" · {ceo['timeframe']}" if ceo["timeframe"] else "")
    return (f'<div class="pharmer-ceo"><div class="pharmer-label">◆ If we ran the company</div>'
            f'<div class="title">{html.escape(title)}</div>{rows}</div>')


def results_section(run_dir, ticker):
    """The hero result: the Sharpest Needle, its big number and evidence, and the CEO card."""
    try:
        res = result_model(run_dir)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        # A broken findings.json must not hide the stage diagnostics below, or the PDF.
        logging.getLogger("app").exception("result screen for %s", run_dir)
        st.error("This run's result files couldn't be read. The PDF report and the agents' "
                 "details are below.")
        _pdf_button(run_dir)
        return
    company = librarian.CIKS.get(ticker, (None, ticker or ""))[1]
    st.html(f'<div class="pharmer-run-head"><span class="pharmer-ticker">{html.escape(ticker or "")}'
            f'</span><span class="title">{html.escape(company)} · the result</span></div>')
    left, right = st.columns([3, 2], gap="medium")
    with left, st.container(key="needle-card"):
        if res["needle"]:
            pick = (" · AI pick, not yet confirmed by the analyst" if res["ai_pick"] else "")
            st.html(f'<div class="pharmer-label">◆ Sharpest Needle · {html.escape(res["needle"])}'
                    f'{html.escape(pick)}</div>'
                    f'<div class="pharmer-headline">{html.escape(res["headline"])}</div>')
            if res["number"]:
                count_up(res["number"]["text"])
                st.html(f'<div class="pharmer-number-caption">'
                        f'{html.escape(res["number"]["caption"])}</div>')
        else:
            st.warning("No Sharpest Needle for this run: no findings were selected.")
        if res["verdict"]:
            st.html(f'<div class="pharmer-verdict">{html.escape(res["verdict"])}</div>')
        elif res["verdict_note"]:
            st.warning(f"No verdict: {res['verdict_note']}")
        chips = []
        if res["badge"] == "Unverified":
            chips.append('<span class="pharmer-chip r">not yet fact-checked</span>')
        elif res["badge"]:
            tone = {"High": "g", "Low": "r"}.get(res["badge"], "")
            chips.append(f'<span class="pharmer-chip {tone}">'
                         f'Fact Checker: {html.escape(res["badge"])} confidence</span>')
        if res["priced_in"]:
            chips.append(f'<span class="pharmer-chip a">market: {html.escape(res["priced_in"])}'
                         "</span>")
        if chips or res["quote"]:
            st.html(f'<div class="pharmer-chips">{"".join(chips)}</div>'
                    + (quote_html(res["quote"]["text"], res["quote"]["source"])
                       if res["quote"] else ""))
    with right, st.container(key="ceo-card"):
        ceo = res["ceo"]
        if ceo["status"] == "ok":
            st.html(ceo_html(ceo))
            if ceo["stale"]:
                st.warning(ceo["stale"])
        else:
            st.warning(f"No CEO move: {ceo['error']}")
        _pdf_button(run_dir)


def _pdf_button(run_dir):
    pdf = Path(run_dir) / "report.pdf"
    if pdf.is_file():
        st.download_button("⬇ Download the CEO report (PDF)", pdf.read_bytes(),
                           file_name=f"pharmer-{Path(run_dir).name}.pdf",
                           mime="application/pdf", type="primary", use_container_width=True)
    # No PDF: recovery_box at the top of the run offers "Retry the report".


def show_run(run):
    st.session_state.pop("start_error", None)  # a refusal on the new-run form is moot here
    run_dir = run["path"]
    status = orchestrator.read_status(run_dir)
    if st.button("➕ New run"):  # the picker is tucked away in "Past runs"; keep the way back visible
        _open(NEW_RUN)
    st.subheader(run["name"])
    st.caption(f"Phase: {PHASE_LABELS.get(run['phase'], run['phase'])}")
    if run["phase"] == "running":
        show_progress(run_dir)
        return
    if run["phase"] == "interrupted":
        st.error("This run stopped when the app restarted (or it is running from the command "
                 "line). Mark it as failed to retry it.")
        if st.button("Mark as failed", type="primary"):
            mark_interrupted(run_dir)
            st.rerun()
    recovery_box(run, status)
    if run["phase"] == "done":
        results_section(run_dir, run["ticker"])
    stage_warning(status)
    trouble = run["phase"] in ("failed", "interrupted")
    with st.expander("The agents", expanded=trouble):
        agents_section(run_dir, status, live=False)
    with st.expander("Details", expanded=trouble):
        if status.get("error"):
            st.caption(f"Background job error: {status['error']}")
        stage_table(status)
    start_done = (status.get("stages", {}).get("scoring_after_peer") or {}).get("status") == "ok"
    if start_done and run["phase"] != "interrupted":
        review_section(run_dir, run["ticker"])


def main():
    st.set_page_config(page_title="Pharmer Research", page_icon="\U0001faa1", layout="wide")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    st.html(CSS)
    st.title("PHARMER RESEARCH")
    st.caption("We find the needle in the haystack.")
    runs = list_runs(live=live_runs())
    chosen = past_runs(runs)
    run = next((r for r in runs if r["name"] == chosen), None)
    if run is None:
        new_run_form()
    else:
        show_run(run)


if __name__ == "__main__":
    # Streamlit runs this file as __main__; run the importable `app` module's main() instead,
    # so the page, the job registry and the tests (which patch `app`) share one copy.
    import app as _app

    _app.main()
