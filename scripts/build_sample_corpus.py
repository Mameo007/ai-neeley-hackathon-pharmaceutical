"""Build the frozen Novo Nordisk sample corpus (Contract 1) from real SEC EDGAR documents.

A dev fixture that stands in for the Librarian until Partner A's version exists: it fetches two
half-year 6-Ks and the XBRL companyfacts, splits the 6-Ks into the filing's printed pages and
into chunks, and writes `data/sample/NVO/{manifest.json,chunks.jsonl,facts.jsonl}`.

Usage:
    .venv/bin/python scripts/build_sample_corpus.py               # fetch from SEC, then build
    .venv/bin/python scripts/build_sample_corpus.py --from-cache  # rebuild from _raw/ only
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from html.parser import HTMLParser
from pathlib import Path

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "data" / "sample" / "NVO"
RAW_DIR = OUT_DIR / "_raw"

CIK = 353278
COMPANY = "Novo Nordisk"
TICKER = "NVO"
# SEC asks for a descriptive User-Agent with a contact; requests stay well under 10/s.
SEC_USER_AGENT = "NeeleyHackathon research admin@example.com"
SEC_SLEEP_S = 0.15

SUBMISSIONS_URL = f"https://data.sec.gov/submissions/CIK{CIK:010d}.json"
COMPANYFACTS_URL = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{CIK:010d}.json"

# Fixed manifest order. accession/document identify the filing; everything else in the manifest
# row is read from the submissions JSON.
SIX_KS = [
    {"doc_id": "NVO-6K-2026Q2", "fiscal_period": "Q2 2026",
     "accession": "0000353278-26-000023", "document": "caq22026.htm"},
    {"doc_id": "NVO-6K-2025Q2", "fiscal_period": "Q2 2025",
     "accession": "0001628280-25-038087", "document": "caq22025.htm"},
]
XBRL_DOC_ID = "NVO-XBRL"

# Concept map: our metric key -> ifrs-full concept.
CONCEPTS = {
    "revenue": "Revenue",
    "trade_receivables": "CurrentTradeReceivables",
    "inventory": "Inventories",
    "operating_profit": "ProfitLossFromOperatingActivities",
    "net_profit": "ProfitLoss",
}
FACT_YEARS = range(2023, 2026)

CHUNK_LIMIT = 1500
HEADER_MAX_ROWS = 3
HEADING_MAX_LEN = 120

CACHE_FILES = ["submissions.json", "companyfacts.json", "cache_meta.json"] + [
    k["document"] for k in SIX_KS]
OUTPUT_FILES = ["manifest.json", "chunks.jsonl", "facts.jsonl"]

PAGE_NO_RE = re.compile(r"\bPage (\d+) of \d+\b")
AMOUNT_RE = re.compile(r"^[(\-–]?\d[\d,.]*\)?\s?%?$")
YEAR_RE = re.compile(r"^(19|20)\d\d$")
NAV_MARKER = "Company announcement No"
BANNER_MARKER = "Financial report for the period"
DATELINE_RE = re.compile(r"^([A-Z][\w ,]*, )?\d{1,2} [A-Z][a-z]+ \d{4}$")
FOOTNOTE_RE = re.compile(r"^\d+\)")


# ---------------------------------------------------------------- fetch


def _sec_get(url: str) -> bytes:
    """GET one SEC URL with curl (the dev machine's python.org build has no CA bundle)."""
    time.sleep(SEC_SLEEP_S)
    proc = subprocess.run(
        ["curl", "-sSfL", "--max-time", "60", "-A", SEC_USER_AGENT, url],
        capture_output=True, check=False)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"curl exit {proc.returncode} for {url}: {err}")
    return proc.stdout


def _doc_url(accession: str, document: str) -> str:
    return (f"https://www.sec.gov/Archives/edgar/data/{CIK}/"
            f"{accession.replace('-', '')}/{document}")


def fetch_raw(raw_dir: Path) -> None:
    """Download every source into raw_dir and record the UTC fetch date in cache_meta.json."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / "submissions.json").write_bytes(_sec_get(SUBMISSIONS_URL))
    (raw_dir / "companyfacts.json").write_bytes(_sec_get(COMPANYFACTS_URL))
    for k in SIX_KS:
        logger.info("fetching %s", k["document"])
        (raw_dir / k["document"]).write_bytes(_sec_get(_doc_url(k["accession"], k["document"])))
    meta = {"collected_date": dt.datetime.now(dt.timezone.utc).date().isoformat()}
    _write_json(raw_dir / "cache_meta.json", meta)


def _check_cache(raw_dir: Path) -> None:
    for name in CACHE_FILES:
        if not (raw_dir / name).is_file():
            raise FileNotFoundError(f"missing cache file {raw_dir / name}")


# ---------------------------------------------------------------- HTML -> blocks


def _clean(text: str) -> str:
    """Unescape entities, drop zero-width spaces, collapse whitespace."""
    text = html.unescape(text).replace("\u200b", "")
    return re.sub(r"\s+", " ", text).strip()


class _BlockParser(HTMLParser):
    """Turn the Workiva 6-K HTML into pages of blocks.

    A block is ("p", text) or ("table", rows), rows being lists of cleaned non-empty cells.
    Inline <font> runs are joined without spaces (Workiva splits words across runs); a <div>
    or <br> outside tables ends a paragraph, inside a table cell it becomes a space.
    `<hr style="page-break-after:always">` starts a new page segment.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.segments: list[list[tuple]] = [[]]
        self._text: list[str] = []
        self._table_depth = 0
        self._rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def _flush_para(self) -> None:
        text = _clean("".join(self._text))
        self._text = []
        if text:
            self.segments[-1].append(("p", text))

    def handle_starttag(self, tag, attrs):
        style = (dict(attrs).get("style") or "").replace(" ", "")
        if tag == "table":
            if self._table_depth == 0:
                self._flush_para()
                self._rows = []
            self._table_depth += 1
        elif self._table_depth:
            if tag == "tr":
                self._row = []
            elif tag == "td":
                self._cell = []
            elif tag in ("div", "br") and self._cell is not None:
                self._cell.append(" ")
        elif tag in ("div", "br"):
            self._flush_para()
        if "page-break-after:always" in style:
            self._flush_para()
            self.segments.append([])

    def handle_endtag(self, tag):
        if self._table_depth:
            if tag == "td" and self._cell is not None and self._row is not None:
                cell = _clean("".join(self._cell))
                if cell:
                    self._row.append(cell)
                self._cell = None
            elif tag == "tr" and self._row is not None:
                if self._row:
                    self._rows.append(self._row)
                self._row = None
            elif tag == "table":
                self._table_depth -= 1
                if self._table_depth == 0 and self._rows:
                    self.segments[-1].append(("table", self._rows))
                    self._rows = []
        elif tag == "div":
            self._flush_para()

    def handle_data(self, data):
        if self._table_depth:
            if self._cell is not None:
                self._cell.append(data)
        else:
            self._text.append(data)

    def close(self):
        super().close()
        self._flush_para()


def _block_text(block: tuple) -> str:
    if block[0] == "p":
        return block[1]
    return "\n".join(" ".join(row) for row in block[1])


def parse_pages(raw_html: str) -> list[dict]:
    """Split a 6-K into printed pages: [{"page", "blocks"}], cover and boilerplate removed."""
    parser = _BlockParser()
    parser.feed(raw_html)
    parser.close()
    pages: list[dict] = []
    prev = 0
    for segment in parser.segments[1:]:  # segment 0 is the SEC cover
        printed = None
        blocks = []
        for block in segment:
            text = _block_text(block)
            if BANNER_MARKER in text and PAGE_NO_RE.search(text):
                printed = int(PAGE_NO_RE.search(text).group(1))
                continue
            if NAV_MARKER in text:
                continue
            if block[0] == "p" and text.startswith(BANNER_MARKER):
                continue  # the front page carries the banner text as a plain paragraph
            blocks.append(block)
        if not blocks:
            continue
        page = printed if printed is not None else prev + 1
        prev = page
        pages.append({"page": page, "blocks": blocks})
    return pages


# ---------------------------------------------------------------- chunking


def _is_heading(text: str) -> bool:
    return (len(text) <= HEADING_MAX_LEN and text[-1] not in ".,;:"
            and not FOOTNOTE_RE.match(text) and not DATELINE_RE.match(text))


def _page_section(page: dict, previous: str) -> str:
    """First short non-table paragraph on the page, else the previous page's section."""
    for block in page["blocks"]:
        if block[0] == "table":
            return previous
        if DATELINE_RE.match(block[1]):
            continue
        # The front page's headline is a long sentence; it is still the page's section.
        if _is_heading(block[1]) or (page["page"] == 1 and not previous):
            return block[1]
        return previous
    return previous


def _is_amount(cell: str) -> bool:
    return bool(AMOUNT_RE.match(cell)) and not YEAR_RE.match(cell)


def _table_chunks(rows: list[list[str]]) -> list[str]:
    """Rows -> chunk texts, split at row boundaries; continuation chunks repeat the header."""
    # A table with no amount cells (text grids) has no column header to repeat.
    first_amount = next((i for i, row in enumerate(rows) if any(_is_amount(c) for c in row)),
                        0)
    n_header = min(first_amount, HEADER_MAX_ROWS)
    lines = [" ".join(row) for row in rows]
    header, body = lines[:n_header], lines[n_header:]
    groups: list[list[str]] = []
    size = 0
    for line in body:
        if groups and size + 1 + len(line) > CHUNK_LIMIT:
            groups.append([])
            size = 0
        if not groups:
            groups.append([])
        size += len(line) + (1 if groups[-1] else 0)
        groups[-1].append(line)
    if not groups:
        return ["\n".join(header)]
    return ["\n".join(header + g) for g in groups]


def _prose_chunks(paras: list[str]) -> list[str]:
    out: list[str] = []
    cur = ""
    for para in paras:
        if cur and len(cur) + 1 + len(para) > CHUNK_LIMIT:
            out.append(cur)
            cur = para
        else:
            cur = f"{cur}\n{para}" if cur else para
    if cur:
        out.append(cur)
    return out


def build_chunks(doc_id: str, pages: list[dict]) -> list[dict]:
    """Pages -> Contract 1b chunk rows, in page order."""
    chunks: list[dict] = []
    section = ""
    for page in pages:
        section = _page_section(page, section)
        texts: list[tuple[str, bool]] = []
        paras: list[str] = []
        for block in page["blocks"]:
            if block[0] == "p":
                paras.append(block[1])
                continue
            texts += [(t, False) for t in _prose_chunks(paras)]
            paras = []
            texts += [(t, True) for t in _table_chunks(block[1])]
        texts += [(t, False) for t in _prose_chunks(paras)]
        for n, (text, is_table) in enumerate(texts, start=1):
            chunks.append({
                "chunk_id": f"{doc_id}-p{page['page']}-c{n:02d}",
                "doc_id": doc_id,
                "page": page["page"],
                "section": section,
                "is_table": is_table,
                "text": text,
            })
    return chunks


# ---------------------------------------------------------------- manifest + facts


def build_facts(companyfacts: dict) -> tuple[list[dict], str, str]:
    """XBRL companyfacts -> Contract 1c rows (annual, FY2023-FY2025, latest filing wins).

    Returns (facts, latest period_end, filed date of the newest filing that supplied a fact).
    """
    ifrs = companyfacts["facts"]["ifrs-full"]
    facts: list[dict] = []
    filed_dates: list[str] = []
    for metric, concept in CONCEPTS.items():
        best: dict[str, dict] = {}
        for x in ifrs[concept]["units"]["DKK"]:
            if x.get("fp") != "FY" or int(x["end"][:4]) not in FACT_YEARS:
                continue
            if "start" in x:
                days = (dt.date.fromisoformat(x["end"]) - dt.date.fromisoformat(x["start"])).days
                if days < 360:
                    continue
            cur = best.get(x["end"])
            if cur is None or (x["filed"], x["accn"]) > (cur["filed"], cur["accn"]):
                best[x["end"]] = x
        for end, x in best.items():
            period = f"FY{end[:4]}"
            filed_dates.append(x["filed"])
            facts.append({
                "fact_id": f"{TICKER}-xbrl-{metric}-{period}",
                "doc_id": XBRL_DOC_ID,
                "metric": metric,
                "value": round(x["val"] / 1e6),
                "unit": "millions",
                "currency": "DKK",
                "fiscal_period": period,
                "period_end": end,
                "source_kind": "xbrl",
            })
    facts.sort(key=lambda f: f["fact_id"])
    return facts, max(f["period_end"] for f in facts), max(filed_dates)


def build_manifest(submissions: dict, collected_date: str, xbrl_period_end: str,
                   xbrl_filed: str) -> list[dict]:
    """Contract 1a rows in fixed order; 6-K dates and URLs come from the submissions JSON."""
    recent = submissions["filings"]["recent"]
    rows = {acc: dict(zip(recent.keys(), vals))
            for acc, vals in zip(recent["accessionNumber"], zip(*recent.values()))}
    common = {"company": COMPANY, "ticker": TICKER}
    tail = {"source": "pre-collected", "collected_date": collected_date,
            "application_no": None, "labeler_code": None}
    manifest = []
    for k in SIX_KS:
        sub = rows[k["accession"]]
        if sub["form"] != "6-K" or sub["primaryDocument"] != k["document"]:
            raise ValueError(f"submissions mismatch for {k['accession']}: {sub}")
        manifest.append({
            "doc_id": k["doc_id"], **common, "doc_type": "6-K",
            "fiscal_period": k["fiscal_period"], "period_end": sub["reportDate"],
            "filed_date": sub["filingDate"],
            "url": _doc_url(sub["accessionNumber"], sub["primaryDocument"]),
            "accession": sub["accessionNumber"], **tail,
        })
    manifest.append({
        "doc_id": XBRL_DOC_ID, **common, "doc_type": "xbrl",
        "fiscal_period": f"FY{xbrl_period_end[:4]}", "period_end": xbrl_period_end,
        "filed_date": xbrl_filed, "url": COMPANYFACTS_URL, "accession": None, **tail,
    })
    return manifest


# ---------------------------------------------------------------- output


def _write_json(path: Path, obj) -> None:
    with path.open("w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def build_outputs(raw_dir: Path, out_dir: Path) -> None:
    """Read the raw cache and write manifest.json, chunks.jsonl, facts.jsonl into out_dir."""
    _check_cache(raw_dir)
    meta = json.loads((raw_dir / "cache_meta.json").read_text(encoding="utf-8"))
    submissions = json.loads((raw_dir / "submissions.json").read_text(encoding="utf-8"))
    companyfacts = json.loads((raw_dir / "companyfacts.json").read_text(encoding="utf-8"))
    facts, xbrl_end, xbrl_filed = build_facts(companyfacts)
    manifest = build_manifest(submissions, meta["collected_date"], xbrl_end, xbrl_filed)
    chunks: list[dict] = []
    for k in SIX_KS:
        raw_html = (raw_dir / k["document"]).read_text(encoding="utf-8")
        chunks += build_chunks(k["doc_id"], parse_pages(raw_html))
    doc_order = {m["doc_id"]: i for i, m in enumerate(manifest)}
    chunks.sort(key=lambda c: (doc_order[c["doc_id"]], c["page"],
                               int(c["chunk_id"].rsplit("-c", 1)[1])))
    _write_json(out_dir / "manifest.json", manifest)
    _write_jsonl(out_dir / "chunks.jsonl", chunks)
    _write_jsonl(out_dir / "facts.jsonl", facts)
    logger.info("wrote %d docs, %d chunks, %d facts", len(manifest), len(chunks), len(facts))


def main(argv: list[str] | None = None) -> int:
    """Fetch (unless --from-cache) and build into temp dirs.

    The cache and the three outputs are only replaced after the whole build succeeds; they are
    then renamed into OUT_DIR one at a time (not as one atomic set).
    """
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--from-cache", action="store_true", help="build from _raw/ only")
    args = ap.parse_args(argv)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp_out = Path(tempfile.mkdtemp(prefix=".build-", dir=OUT_DIR))
    tmp_raw = None
    try:
        raw_dir = RAW_DIR
        if not args.from_cache:
            tmp_raw = Path(tempfile.mkdtemp(prefix=".raw-", dir=OUT_DIR))
            fetch_raw(tmp_raw)
            raw_dir = tmp_raw
        build_outputs(raw_dir, tmp_out)
        if tmp_raw is not None:
            if RAW_DIR.exists():
                shutil.rmtree(RAW_DIR)
            os.replace(tmp_raw, RAW_DIR)
            tmp_raw = None
        for name in OUTPUT_FILES:
            os.replace(tmp_out / name, OUT_DIR / name)
    finally:
        shutil.rmtree(tmp_out, ignore_errors=True)
        if tmp_raw is not None:
            shutil.rmtree(tmp_raw, ignore_errors=True)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sys.exit(main())
