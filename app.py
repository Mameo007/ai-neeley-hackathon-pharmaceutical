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


def stage_problems(status):
    """Stages that did not simply succeed, for the warning above the stage table."""
    return [(name, e.get("status"), e.get("error")) for name, e in status.get("stages", {}).items()
            if e.get("status") in ("skipped", "degraded", "failed")]


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
    if clicked:
        corpus = corpus if use_prebuilt else None
        try:
            run_dir = orchestrator.prepare_run(ticker, corpus=corpus)
        except orchestrator.UsageError as exc:
            st.error(str(exc))
            return
        launch(run_dir, lambda: orchestrator.run_start_stages(run_dir, corpus))
        _open(run_dir.name)


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
        score = "–" if r["score"] is None else r["score"]
        materiality = "–" if r["materiality"] is None else r["materiality"]
        hits = "not checked" if r["priced_in_hits"] is None else r["priced_in_hits"]
        st.caption(f"Why selected: score {score} = materiality {materiality} "
                   f"(|EPS impact %|) × obscurity {r['obscurity']} (priced-in hits: {hits})"
                   f" · raised by {', '.join(h for h in r['hunters'] if h)}"
                   + (f" · confidence: {md(r['confidence_reason'])}"
                      if r.get("confidence_reason") else ""))
        for q in r["quotes"]:
            quote = md(q["text"]).replace("\n", "  \n> ")
            st.markdown(f"> “{quote}”  \n> — {md('; '.join(q['sources']))}")


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
                    st.error(str(exc))
                    return
                launch(run_dir, lambda: orchestrator.run_finish_stages(run_dir))
            _open(Path(run_dir).name)


def results_section(run_dir):
    synthesis = syn.read_json_or_none(Path(run_dir) / "synthesis.json") or {}
    move = syn.read_json_or_none(Path(run_dir) / "ceo_move.json") or {}
    st.subheader("Result")
    if synthesis.get("status") == "ok":
        st.markdown(f"**Verdict:** {md(synthesis['verdict'])}")
    elif synthesis:
        st.warning(f"No verdict: {synthesis.get('error') or 'Synthesizer failed'}")
    if move.get("status") == "ok":
        st.markdown(f"**CEO move:** {md(move['action'])} *({md(move['timeframe'])})*")
    elif move:
        st.warning(f"No CEO move: {move.get('error') or 'CEO Move failed'}")
    pdf = Path(run_dir) / "report.pdf"
    if pdf.is_file():
        st.download_button("Download report (PDF)", pdf.read_bytes(),
                           file_name=f"pharmer-{Path(run_dir).name}.pdf",
                           mime="application/pdf", type="primary")
    else:
        st.error("No report.pdf for this run (see the stage table).")


def show_run(run):
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
        st.error("This run is marked running, but no job for it is alive in this app (the app "
                 "was restarted mid-run, or it is running from the command line).")
        if st.button("Mark it as failed so it can be retried"):
            mark_interrupted(run_dir)
            st.rerun()
    if status.get("error"):
        st.error(f"The background job crashed: {status['error']}")
    if run["phase"] == "done":
        results_section(run_dir)
    stage_warning(status)
    trouble = run["phase"] in ("failed", "interrupted")
    with st.expander("The agents", expanded=trouble):
        agents_section(run_dir, status, live=False)
    with st.expander("Details", expanded=trouble):
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
