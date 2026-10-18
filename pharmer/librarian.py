"""Librarian (stage 0): fetch a company's SEC periodic filings and build the
page-indexed corpus every downstream agent cites.

Writes four files into the run folder:
  manifest.json          Contract 1a - one entry per document read
  chunks.jsonl           Contract 1b - the only thing hunters may cite
  facts.jsonl            Contract 1c - XBRL facts for the number-tie rule, keyed
                         through CONCEPT_MAP; IR Excel workbooks (needed for
                         foreign filers' quarterlies) are still deferred
  librarian_report.json  non-contract audit sidecar: which 6-K candidates were
                         scored and why one was chosen, which page numbers are
                         the document's own vs inferred, and coverage warnings

Run one company from the CLI:  python3 -m pharmer.librarian LLY
"""
import asyncio
import html as html_lib
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# Deliberately not imported from pharmer.llm at module scope: this stage makes
# no Claude call, and importing llm would make `anthropic` a hard requirement
# for fetching filings and for running these tests.
REPO_ROOT = Path(__file__).resolve().parent.parent

# Entity resolution is by CIK only - substring matching on company names put
# "SINOPEC SHANGHAI PETROCHEMICAL" in the slot for Roche (docs/architecture.md
# section 8.2). Display names are the curated "small fixed thing".
CIKS = {
    "MRK": (310158, "Merck"),
    "BMY": (14272, "Bristol-Myers Squibb"),
    "PFE": (78003, "Pfizer"),
    "LLY": (59478, "Eli Lilly"),
    "NVO": (353278, "Novo Nordisk"),
    "ABBV": (1551152, "AbbVie"),
    "JNJ": (200406, "Johnson & Johnson"),
    "AZN": (901832, "AstraZeneca"),
    "AMGN": (318154, "Amgen"),
    "GILD": (882095, "Gilead Sciences"),
}

SEC_UA_VAR = "PHARMER_SEC_UA"
_MIN_INTERVAL = 0.11  # SEC fair access: stay under 10 requests/second
_RETRY_STATUS = {429, 503, 502, 504}

# A foreign filer's financial 6-K is found by reading the document: Novo filed
# 895 recent 6-Ks, and the financial ones have carried several different
# filenames and filer-agent accession prefixes (architecture section 8.1).
FINANCIAL_6K_MARKERS = {
    "financial report": 3,
    "income statement": 3,
    "balance sheet": 3,
    "cash flow": 2,
    "net sales": 2,
    "condensed": 1,
    "interim": 1,
    "trade receivables": 1,
    "operating profit": 1,
}
FINANCIAL_6K_THRESHOLD = 8
_6K_MIN_BYTES = 800_000       # cheap prefilter before spending a fetch
_6K_MAX_FETCHES = 14          # bound the scoring loop on a 895-filing history

CHUNK_BUDGET = 2000           # characters of text per chunk

# Novo's FY2025 annual-report exhibit carries no page-break markup of any kind -
# it is one continuous 10.6MB document. Without this fallback all 659 of its
# chunks would cite "p.1". Sections longer than a real printed page are divided
# into synthetic page-sized fragments, recorded as inferred, never as printed.
UNPAGINATED_MIN_CHARS = 20_000
SYNTHETIC_PAGE_CHARS = 3_000
_BLOCK_END_RE = re.compile(r"(?i)(</(?:p|div|tr|li|h[1-6])>)")
_HEADING_RE = re.compile(r"^(item|note|part)\s+\w", re.I)
# A filing prints its page number in one of two places, and which one is not a
# property of the form: Lilly's 10-Q puts a bare number at the foot of the page,
# Novo's 6-K puts "Page 3 of 37" in a running header. Both are checked.
_PAGE_FOOT_RE = re.compile(r"^(\d{1,4})$")
_PAGE_HEAD_RE = re.compile(r"(?i)\bpage\s+(\d{1,4})\s*(?:of\s+\d{1,4})?\b")

_last_request = 0.0

# Hand-built US-GAAP <-> IFRS concept map: our metric key -> candidate XBRL tags in
# preference order, first match wins. Hand-curated on purpose (architecture section 2,
# "curate the small fixed things"): automation picks the wrong tag when a filer reports
# several. The first five keys are the ones Partner B's merged hunters and scoring
# already use, so they must keep these exact names. Metrics with no tag for a given
# filer are logged as `unmapped_metric` rather than guessed.
CONCEPT_MAP = {
    "revenue": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                "Revenue"],
    "trade_receivables": ["AccountsReceivableNetCurrent", "CurrentTradeReceivables",
                          "TradeAndOtherCurrentReceivables"],
    "inventory": ["InventoryNet", "Inventories"],
    "operating_profit": ["OperatingIncomeLoss", "ProfitLossFromOperatingActivities"],
    "net_profit": ["NetIncomeLoss", "ProfitLoss"],
    "cost_of_sales": ["CostOfGoodsAndServicesSold", "CostOfRevenue", "CostOfSales"],
    "gross_profit": ["GrossProfit"],
    "research_development": ["ResearchAndDevelopmentExpense", "ResearchAndDevelopmentExpenseIFRS"],
    "cash": ["CashAndCashEquivalentsAtCarryingValue", "CashAndCashEquivalents"],
    "total_assets": ["Assets"],
    "total_equity": ["StockholdersEquity", "Equity"],
    "trade_payables": ["AccountsPayableCurrent", "TradeAndOtherCurrentPayables"],
}

# XBRL reports absolute amounts; Contract 1c carries millions, which is also the unit
# the filing tables print - that equality is what the number-tie rule checks.
FACT_SCALE = 1_000_000
_SPAN_LABELS = [(80, 100, "Q"), (170, 190, "H"), (350, 380, "FY")]


# --------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------

def _sec_user_agent():
    """SEC requires a declared contact; refuse to fetch anonymously."""
    from pharmer.llm import _load_dotenv  # lazy: keeps `anthropic` off the import path

    _load_dotenv()
    ua = os.environ.get(SEC_UA_VAR, "").strip()
    if not ua:
        raise RuntimeError(
            f"{SEC_UA_VAR} is not set. SEC fair-access rules require a real "
            f"name and email in the User-Agent. Copy .env.example to .env and "
            f"set it, e.g. {SEC_UA_VAR}=\"Jane Doe jane@example.com\""
        )
    return ua


def _sec_get(url, tries=3):
    """One rate-limited GET against sec.gov, retried on transient statuses."""
    global _last_request
    # No Accept-Encoding: urllib does not transparently decompress, so asking
    # for gzip would hand back compressed bytes.
    headers = {"User-Agent": _sec_user_agent()}
    for attempt in range(1, tries + 1):
        wait = _MIN_INTERVAL - (time.monotonic() - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=90) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code in _RETRY_STATUS and attempt < tries:
                log.warning("SEC %s on %s, retry %d/%d", exc.code, url, attempt, tries)
                time.sleep(2 ** attempt)
                continue
            raise
        except urllib.error.URLError:
            if attempt < tries:
                time.sleep(2 ** attempt)
                continue
            raise


def _sec_json(url):
    return json.loads(_sec_get(url))


def _sec_text(url):
    return _sec_get(url).decode("utf-8", "replace")


# --------------------------------------------------------------------------
# periods and identifiers
# --------------------------------------------------------------------------

def _quarter(period_end):
    """Fiscal quarter from a period-end date.

    Keyed on the month, not the day, so Pfizer's and J&J's 52/53-week quarters
    (2026-06-28 against 2025-06-29) land in the same quarter instead of being
    reported as a missing prior-year filing.
    """
    month = int(period_end[5:7])
    return (month - 1) // 3 + 1


def _fiscal_period(form, period_end, half_year=False):
    """Contract 1a `fiscal_period`: FY2025, Q2 2026, or H1 2026."""
    year = period_end[:4]
    if form in ("10-K", "20-F"):
        return f"FY{year}"
    if half_year:
        return f"H{1 if _quarter(period_end) <= 2 else 2} {year}"
    return f"Q{_quarter(period_end)} {year}"


def _doc_period(form, period_end, half_year=False):
    """The `doc_id` period segment: FY2025 or 2026Q2 (docs/conventions.md)."""
    year = period_end[:4]
    if form in ("10-K", "20-F"):
        return f"FY{year}"
    if half_year:
        return f"{year}H{1 if _quarter(period_end) <= 2 else 2}"
    return f"{year}Q{_quarter(period_end)}"


def _doc_id(ticker, form, period_end, half_year=False):
    return f"{ticker}-{form.replace('-', '')}-{_doc_period(form, period_end, half_year)}"


def _archive_url(cik, accession, document):
    return (f"https://www.sec.gov/Archives/edgar/data/{cik}/"
            f"{accession.replace('-', '')}/{document}")


def run_id_for(ticker, now=None):
    """`<TICKER>-<YYYYMMDD-HHMM>` in UTC (docs/conventions.md)."""
    now = now or datetime.now(timezone.utc)
    return f"{ticker}-{now.strftime('%Y%m%d-%H%M')}"


# --------------------------------------------------------------------------
# HTML to text
# --------------------------------------------------------------------------

def _plain_text(fragment):
    """Readable text from an HTML fragment, cell and row structure preserved.

    Cells are separated by tabs and rows by newlines, so a table chunk keeps
    its row/column associations. Cell content itself is never rewritten -
    `(1,234)` stays `(1,234)` - because the Fact Checker string-matches hunter
    quotes against this text.
    """
    text = re.sub(r"(?is)<(script|style|ix:header).*?</\1>", " ", fragment)
    text = re.sub(r"(?i)</t[dh]>", "\t", text)
    text = re.sub(r"(?i)</tr>", "\n", text)
    text = re.sub(r"(?i)<(br|/p|/div|/h[1-6]|/li)[^>]*>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html_lib.unescape(text).replace("\xa0", " ")
    text = re.sub(r"[ \t]+\t", "\t", text)
    text = re.sub(r"\t[ ]+", "\t", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def _table_rows(table_html):
    """(header_rows, body_rows) as tab-joined strings for one `<table>`."""
    rows = []
    for row_html in re.findall(r"(?is)<tr\b.*?</tr>", table_html):
        cells = re.findall(r"(?is)<t[dh]\b.*?</t[dh]>", row_html)
        values = [_plain_text(cell).replace("\n", " ").strip() for cell in cells]
        if any(values):
            rows.append(("<th" in row_html.lower(), "\t".join(values).strip()))
    headers = [text for is_header, text in rows if is_header]
    body = [text for is_header, text in rows if not is_header]
    # A filing table often has no <th> at all; its first row still names the
    # columns (the reporting periods and the units), so it travels as a header.
    if not headers and body:
        headers, body = body[:1], body[1:]
    return headers, body


def _html_pieces(payload):
    """Split a text block at its closing block tags, delimiters kept."""
    pieces, buffer = [], ""
    for part in _BLOCK_END_RE.split(payload):
        buffer += part
        if _BLOCK_END_RE.fullmatch(part):
            pieces.append(buffer)
            buffer = ""
    if buffer.strip():
        pieces.append(buffer)
    return pieces or [payload]


def _subdivide(section_html):
    """Divide one unpaginated section into synthetic page-sized fragments.

    Tables stay whole; prose is divided at paragraph boundaries, because a
    continuous annual report is often a single text block with no table in it
    to break on.
    """
    units = []
    for kind, payload in _blocks(section_html):
        units.extend([payload] if kind == "table" else _html_pieces(payload))

    fragments, current, size = [], [], 0
    for unit in units:
        length = len(_plain_text(unit))
        if current and size + length > SYNTHETIC_PAGE_CHARS:
            fragments.append("".join(current))
            current, size = [], 0
        current.append(unit)
        size += length
    if current:
        fragments.append("".join(current))
    return fragments or [section_html]


def _split_pages(page_html):
    """Split a filing into (fragment, printed_page) on its page-break rules.

    EDGAR filings mark page boundaries with an `<hr>` carrying
    `page-break-after`. The page's own printed number appears either as a bare
    number in its last line or as "Page N of M" in its running header.
    `printed_page` is None when neither is present (cover pages); the caller
    then falls back to the ordinal and records the page as inferred.
    """
    sections = re.split(r"(?i)<hr\b[^>]*>", page_html)
    pages = []
    for section in sections:
        text = _plain_text(section)
        if not text:
            continue
        lines = text.splitlines()
        printed = None
        foot = _PAGE_FOOT_RE.match(lines[-1].strip())
        if foot:
            printed = int(foot.group(1))
        else:
            for line in lines[:2]:
                head = _PAGE_HEAD_RE.search(line)
                if head:
                    printed = int(head.group(1))
                    break
        if printed is None and len(text) > UNPAGINATED_MIN_CHARS:
            pages.extend((fragment, None) for fragment in _subdivide(section))
        else:
            pages.append((section, printed))
    return pages


def _blocks(section_html):
    """Ordered (kind, payload) blocks for one page: tables kept whole."""
    blocks = []
    position = 0
    for match in re.finditer(r"(?is)<table\b.*?</table>", section_html):
        before = section_html[position:match.start()]
        if before.strip():
            blocks.append(("text", before))
        blocks.append(("table", match.group(0)))
        position = match.end()
    tail = section_html[position:]
    if tail.strip():
        blocks.append(("text", tail))
    return blocks


def _chunk_page(section_html, section_name):
    """Chunk one page into (text, is_table, section) triples.

    Paragraphs are never split mid-paragraph. A table too long for one chunk is
    split across chunks with its header rows repeated on each, so no chunk
    loses the column meaning (which period, which units).
    """
    chunks = []
    pending = []
    pending_len = 0

    def flush():
        nonlocal pending, pending_len
        if pending:
            chunks.append(("\n".join(pending), False, section_name))
            pending, pending_len = [], 0

    for kind, payload in _blocks(section_html):
        if kind == "table":
            flush()
            headers, body = _table_rows(payload)
            header_text = "\n".join(headers)
            current, current_len = [], len(header_text)
            for row in body:
                if current and current_len + len(row) + 1 > CHUNK_BUDGET:
                    chunks.append(("\n".join(headers + current), True, section_name))
                    current, current_len = [], len(header_text)
                current.append(row)
                current_len += len(row) + 1
            if current or headers:
                chunks.append(("\n".join(headers + current), True, section_name))
            continue

        text = _plain_text(payload)
        if not text:
            continue
        for line in text.splitlines():
            if _HEADING_RE.match(line) and len(line) < 120:
                flush()
                section_name = line.strip()
            if pending and pending_len + len(line) + 1 > CHUNK_BUDGET:
                flush()
            pending.append(line)
            pending_len += len(line) + 1
    flush()
    return [c for c in chunks if c[0].strip()], section_name


def chunk_document(doc_id, document_html):
    """Page-index one document into Contract 1b chunk records.

    Returns (chunks, page_provenance). `chunk_id` is derived only from the
    document's own structure, so re-running on the same document reproduces
    every id byte-for-byte - a hunter's citation has to stay re-openable.
    """
    chunks = []
    provenance = {"verified_pages": [], "inferred_pages": []}
    per_page_counter = {}
    section_name = ""
    for ordinal, (section_html, printed) in enumerate(_split_pages(document_html), start=1):
        page = printed if printed is not None else ordinal
        if printed is not None:
            provenance["verified_pages"].append(page)
        else:
            provenance["inferred_pages"].append(page)
        page_chunks, section_name = _chunk_page(section_html, section_name)
        for text, is_table, section in page_chunks:
            index = per_page_counter.get(page, 0) + 1
            per_page_counter[page] = index
            chunks.append({
                "chunk_id": f"{doc_id}-p{page}-c{index:02d}",
                "doc_id": doc_id,
                "page": page,
                "section": section,
                "is_table": is_table,
                "text": text,
            })
    return chunks, provenance


# --------------------------------------------------------------------------
# XBRL facts (Contract 1c)
# --------------------------------------------------------------------------

def _period_from_fact(row):
    """Measurement period for one XBRL row, from its own dates.

    Returns `FY2025`, `Q2 2026`, `H1 2026`, or None for a span we cannot label.

    The row's `fy`/`fp` fields are deliberately ignored: they describe the filing,
    not the measurement. Lilly's six-month revenue row carries `fp: Q2` while
    actually covering H1, so trusting it would file the H1 figure under Q2 and the
    number-tie would compare two different numbers.
    """
    end = row["end"]
    start = row.get("start")
    if not start:
        # An instant: a balance-sheet item. A year-end instant belongs to the
        # fiscal year; any other belongs to the quarter it closes. J&J's year ends
        # 2025-12-28 and a 52/53-week year can spill into early January, so both
        # late December and the first days of January count as a year end.
        month, day = int(end[5:7]), int(end[8:10])
        if month == 12:
            return f"FY{end[:4]}"
        if month == 1 and day <= 7:
            return f"FY{int(end[:4]) - 1}"
        return f"Q{_quarter(end)} {end[:4]}"
    span = (date.fromisoformat(end) - date.fromisoformat(start)).days
    for low, high, kind in _SPAN_LABELS:
        if low <= span <= high:
            if kind == "FY":
                return f"FY{end[:4]}"
            if kind == "H":
                return f"H{1 if _quarter(end) <= 2 else 2} {end[:4]}"
            return f"Q{_quarter(end)} {end[:4]}"
    return None


def _fact_period_key(fiscal_period):
    """`Q2 2026` -> `2026Q2`, `H1 2026` -> `2026H1`, `FY2025` unchanged (for fact_id)."""
    if fiscal_period.startswith("FY"):
        return fiscal_period
    label, year = fiscal_period.split()
    return f"{year}{label}"


def _xbrl_facts(ticker):
    """Contract 1c facts for one ticker from SEC XBRL companyfacts.

    Returns (facts, misses, skipped): the fact rows, the concept-map metrics this
    filer does not tag, and a count of rows dropped for an unlabelable span or a
    non-currency unit.
    """
    cik = CIKS[ticker][0]
    payload = _sec_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json")
    taxonomies = {name: tags for name, tags in payload["facts"].items() if name != "dei"}

    facts, misses, skipped = [], [], 0
    for metric, candidates in CONCEPT_MAP.items():
        entry = None
        for tag in candidates:                     # preference order, first match wins
            for taxonomy, tags in taxonomies.items():
                if tag in tags:
                    entry = (taxonomy, tag, tags[tag])
                    break
            if entry:
                break
        if entry is None:
            misses.append({"metric": metric, "candidates_tried": candidates})
            continue

        taxonomy, tag, concept = entry
        currencies = {unit: rows for unit, rows in concept["units"].items()
                      if re.fullmatch(r"[A-Z]{3}", unit)}
        skipped += sum(len(rows) for unit, rows in concept["units"].items()
                       if unit not in currencies)   # shares, USD/shares, ratios
        # `fact_id` carries no currency segment, so one reporting currency per metric
        # keeps ids unique. No shortlist company currently reports a mapped metric in
        # two currencies, but nothing guarantees that for a judge-picked live run.
        if len(currencies) > 1:
            primary = max(currencies, key=lambda u: len(currencies[u]))
            skipped += sum(len(rows) for unit, rows in currencies.items() if unit != primary)
            currencies = {primary: currencies[primary]}

        best = {}
        for unit, rows in currencies.items():
            for row in rows:
                period = _period_from_fact(row)
                if period is None:
                    skipped += 1
                    continue
                key = (period, unit)
                # Restatements repeat a period; the newest filing wins.
                if key not in best or row.get("filed", "") > best[key].get("filed", ""):
                    best[key] = row
        for (period, unit), row in sorted(best.items()):
            facts.append({
                "fact_id": f"{ticker}-xbrl-{metric}-{_fact_period_key(period)}",
                "doc_id": f"{ticker}-XBRL",
                "metric": metric,
                "value": row["val"] / FACT_SCALE,
                "unit": "millions",
                "currency": unit,
                "fiscal_period": period,
                "period_end": row["end"],
                "source_kind": "xbrl",
            })
        log.info("%s: %s -> %s:%s, %d facts", ticker, metric, taxonomy, tag, len(best))
    return facts, misses, skipped


def _xbrl_manifest_entry(ticker):
    """The manifest row every facts.jsonl `doc_id` resolves to."""
    cik, company = CIKS[ticker]
    return {
        "doc_id": f"{ticker}-XBRL",
        "company": company,
        "ticker": ticker,
        "doc_type": "xbrl",
        "fiscal_period": "",          # spans every period the company has tagged
        "period_end": None,
        "filed_date": None,
        "url": f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json",
        "accession": None,
        "source": "live",
        "collected_date": None,
        "application_no": None,
        "labeler_code": None,
    }


# --------------------------------------------------------------------------
# document selection
# --------------------------------------------------------------------------

def _recent_filings(cik):
    """Flatten the submissions API's parallel arrays into one row per filing."""
    data = _sec_json(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
    recent = data["filings"]["recent"]
    return [dict(zip(recent.keys(), values)) for values in zip(*recent.values())]


def _score_financial_6k(text):
    lowered = text.lower()
    hits = {marker: lowered.count(marker) for marker in FINANCIAL_6K_MARKERS
            if marker in lowered}
    score = sum(FINANCIAL_6K_MARKERS[marker] for marker in hits)
    return score, hits


def _is_half_year(text):
    """True when a foreign filer reports the period as a half-year, not a quarter."""
    lowered = text.lower()
    quarterly = any(phrase in lowered for phrase in
                    ("three months ended", "three months of", "the quarter",
                     "first quarter", "second quarter", "third quarter",
                     "fourth quarter", "q1", "q2", "q3", "q4"))
    half = any(phrase in lowered for phrase in
               ("six months ended", "first six months", "half-year", "half year",
                "first half"))
    return half and not quarterly


def _pick_annuals(filings, form):
    return [f for f in filings if f["form"] == form][:2]


def _incorporated_exhibit(cik, filing, audit):
    """Resolve a 20-F's incorporated-by-reference annual report.

    Novo's 20-F primary document carries no financials: the annual report is a
    separate exhibit (architecture section 8.1). The exhibit is found by size
    and confirmed by content, never by its filename - filer-agent naming is not
    a rule (section 8.2).
    """
    accession = filing["accessionNumber"].replace("-", "")
    listing = _sec_json(f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/index.json")
    candidates = []
    for item in listing["directory"]["item"]:
        name = item["name"]
        if not name.lower().endswith((".htm", ".html")) or name == filing["primaryDocument"]:
            continue
        candidates.append((int(item.get("size") or 0), name))
    if not candidates:
        audit.append({"filing": filing["accessionNumber"], "exhibit": None,
                      "reason": "no non-primary .htm exhibit in the accession"})
        return None
    candidates.sort(reverse=True)
    size, name = candidates[0]
    text = _sec_text(_archive_url(cik, filing["accessionNumber"], name))
    score, hits = _score_financial_6k(_plain_text(text)[:400_000])
    if score < FINANCIAL_6K_THRESHOLD:
        audit.append({"filing": filing["accessionNumber"], "exhibit": name,
                      "bytes": size, "marker_score": score,
                      "reason": "largest exhibit does not read as the annual report; "
                                "kept the primary document instead"})
        return None
    audit.append({"filing": filing["accessionNumber"], "exhibit": name, "bytes": size,
                  "marker_score": score, "markers": hits,
                  "reason": "largest non-primary .htm and reads as the annual report"})
    return name, text


def _select_financial_6ks(cik, filings, audit, warnings):
    """Content-score 6-K candidates and keep the ones that are real financial reports.

    Size alone is not enough: Novo's 4,389KB remuneration report is larger than
    its 2,784KB Q2 2026 financial report.
    """
    candidates = [f for f in filings
                  if f["form"] == "6-K" and int(f.get("size") or 0) >= _6K_MIN_BYTES]
    selected = []
    for filing in candidates[:_6K_MAX_FETCHES]:
        url = _archive_url(cik, filing["accessionNumber"], filing["primaryDocument"])
        try:
            raw = _sec_text(url)
        except Exception as exc:                      # a noise 6-K is not worth failing on
            audit.append({"document": filing["primaryDocument"], "error": str(exc),
                          "reason": "fetch failed; candidate skipped"})
            warnings.append(f"could not fetch 6-K candidate {filing['primaryDocument']}: {exc}")
            continue
        text = _plain_text(raw)
        score, hits = _score_financial_6k(text[:400_000])
        keep = score >= FINANCIAL_6K_THRESHOLD
        audit.append({
            "document": filing["primaryDocument"],
            "filed_date": filing["filingDate"],
            "period_end": filing["reportDate"],
            "bytes": int(filing.get("size") or 0),
            "marker_score": score,
            "markers": hits,
            "selected": keep,
            "reason": ("content markers identify a financial report" if keep else
                       f"score {score} below threshold {FINANCIAL_6K_THRESHOLD}; "
                       "reads as a non-financial 6-K"),
        })
        if keep:
            selected.append((filing, raw, _is_half_year(text[:400_000])))
            # Novo has 73 oversized 6-Ks; stop as soon as the latest financial
            # report and its prior-year counterpart are both in hand, so a live
            # run does not fetch a dozen multi-megabyte press releases.
            if len(selected) >= 2:
                latest = selected[0]
                if _prior_year_match(latest[0], [f for f, _, _ in selected[1:]],
                                     latest[2]) is not None:
                    break
    return selected


def _prior_year_match(target, options, half_year=False):
    """Same fiscal period one year earlier, matched on period not on date."""
    year = int(target["reportDate"][:4]) - 1
    quarter = _quarter(target["reportDate"])
    for option in options:
        end = option["reportDate"]
        if int(end[:4]) == year and _quarter(end) == quarter:
            return option
    if half_year:
        for option in options:
            end = option["reportDate"]
            if int(end[:4]) == year and (_quarter(end) <= 2) == (quarter <= 2):
                return option
    return None


def select_documents(ticker):
    """Choose the filings this company's corpus is built from.

    Returns (documents, audit, warnings). Each document is a dict with the
    manifest fields plus the raw `html` it was read from.
    """
    cik, company = CIKS[ticker]
    filings = _recent_filings(cik)
    forms = {f["form"] for f in filings}
    foreign = "20-F" in forms and "10-K" not in forms
    audit = {"filer_type": "20-F/6-K" if foreign else "10-K/10-Q",
             "exhibits": [], "sixk_candidates": []}
    warnings = []
    documents = []

    annual_form = "20-F" if foreign else "10-K"
    annuals = _pick_annuals(filings, annual_form)
    if len(annuals) < 2:
        warnings.append(f"expected 2 {annual_form} filings, found {len(annuals)}")
    for filing in annuals:
        url = _archive_url(cik, filing["accessionNumber"], filing["primaryDocument"])
        raw = _sec_text(url)
        if foreign:
            exhibit = _incorporated_exhibit(cik, filing, audit["exhibits"])
            if exhibit:
                name, raw = exhibit
                url = _archive_url(cik, filing["accessionNumber"], name)
        documents.append({"form": annual_form, "filing": filing, "url": url,
                          "html": raw, "half_year": False})

    if foreign:
        selected = _select_financial_6ks(cik, filings, audit["sixk_candidates"],
                                         warnings)
        audit["sixk_selected"] = [f["primaryDocument"] for f, _, _ in selected]
        if not selected:
            warnings.append("no financial 6-K passed content scoring; "
                            "the corpus has no quarterly report")
        else:
            # Ambiguity means two candidates competing to *be* the same period's
            # report. Comparing the top two scores outright would fire on every
            # foreign run, because the prior-year report scores just as highly.
            by_period = {}
            for filing, _, half in selected:
                key = _fiscal_period("6-K", filing["reportDate"], half)
                by_period.setdefault(key, []).append(filing["primaryDocument"])
            for period, docs in by_period.items():
                if len(docs) > 1:
                    warnings.append(
                        f"{len(docs)} 6-K candidates both read as the {period} "
                        f"financial report ({', '.join(docs)}); the most recent was "
                        "used - see librarian_report.json before trusting it")
            latest, latest_raw, latest_half = selected[0]
            documents.append({"form": "6-K", "filing": latest, "html": latest_raw,
                              "url": _archive_url(cik, latest["accessionNumber"],
                                                  latest["primaryDocument"]),
                              "half_year": latest_half})
            others = [f for f, _, _ in selected[1:]]
            prior = _prior_year_match(latest, others, latest_half)
            if prior is None:
                warnings.append(
                    f"no prior-year same-quarter 6-K found for "
                    f"{_fiscal_period('6-K', latest['reportDate'], latest_half)}; "
                    "like-for-like seasonality checks will be unavailable")
            else:
                raw = next(r for f, r, _ in selected if f is prior)
                half = next(h for f, _, h in selected if f is prior)
                documents.append({"form": "6-K", "filing": prior, "html": raw,
                                  "url": _archive_url(cik, prior["accessionNumber"],
                                                      prior["primaryDocument"]),
                                  "half_year": half})
    else:
        quarterlies = [f for f in filings if f["form"] == "10-Q"]
        if not quarterlies:
            warnings.append("no 10-Q found; the corpus has no quarterly report")
        else:
            latest = quarterlies[0]
            prior = _prior_year_match(latest, quarterlies[1:])
            if prior is None:
                warnings.append(
                    f"no prior-year same-quarter 10-Q found for "
                    f"{_fiscal_period('10-Q', latest['reportDate'])}; "
                    "like-for-like seasonality checks will be unavailable")
            for filing in [f for f in (latest, prior) if f]:
                url = _archive_url(cik, filing["accessionNumber"], filing["primaryDocument"])
                documents.append({"form": "10-Q", "filing": filing, "url": url,
                                  "html": _sec_text(url), "half_year": False})

    for document in documents:
        filing, form = document["filing"], document["form"]
        half = document["half_year"]
        document["manifest"] = {
            "doc_id": _doc_id(ticker, form, filing["reportDate"], half),
            "company": company,
            "ticker": ticker,
            "doc_type": form,
            "fiscal_period": _fiscal_period(form, filing["reportDate"], half),
            "period_end": filing["reportDate"],
            "filed_date": filing["filingDate"],
            "url": document["url"],
            "accession": filing["accessionNumber"],
            "source": "live",
            "collected_date": None,
            "application_no": None,
            "labeler_code": None,
        }
    return documents, audit, warnings


# --------------------------------------------------------------------------
# stage entry point
# --------------------------------------------------------------------------

def _write_corpus(run_dir, manifest, chunks, facts, report):
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / "manifest.json").open("w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=False)
    with (run_dir / "chunks.jsonl").open("w", encoding="utf-8") as fh:
        for chunk in chunks:
            fh.write(json.dumps(chunk, ensure_ascii=False) + "\n")
    # facts.jsonl is written even when empty, so Partner B's readers never hit a
    # missing path. An empty file means XBRL had nothing for this company - a
    # placeholder fact would break the no-fact-from-memory rule.
    with (run_dir / "facts.jsonl").open("w", encoding="utf-8") as fh:
        for fact in facts:
            fh.write(json.dumps(fact, ensure_ascii=False) + "\n")
    with (run_dir / "librarian_report.json").open("w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)


def build_corpus(ticker, run_dir):
    """Fetch, chunk, and write the corpus for one ticker. Returns the sidecar report."""
    if ticker not in CIKS:
        raise ValueError(f"unknown ticker {ticker!r}; supported: {', '.join(sorted(CIKS))}")
    documents, audit, warnings = select_documents(ticker)

    manifest, chunks, pages = [], [], {}
    for document in documents:
        entry = document["manifest"]
        doc_chunks, provenance = chunk_document(entry["doc_id"], document["html"])
        if not doc_chunks:
            warnings.append(f"{entry['doc_id']} produced no chunks")
        manifest.append(entry)
        chunks.extend(doc_chunks)
        verified, inferred = provenance["verified_pages"], provenance["inferred_pages"]
        pages[entry["doc_id"]] = {
            "chunks": len(doc_chunks),
            "attribution": ("printed" if not inferred else
                            "inferred-only" if not verified else "mixed"),
            "verified_printed_pages": len(verified),
            "inferred_page_positions": len(inferred),
            "inferred_pages": inferred,
        }
        if doc_chunks and not verified:
            # The report must not print "p.14" for a document that never
            # numbered its pages; cite the section instead.
            warnings.append(
                f"{entry['doc_id']} has no printed page numbers anywhere in the "
                f"document; its {len(inferred)} page positions are inferred, so "
                "footnotes for it must cite the section, not a printed page")
        log.info("%s: %d chunks, %d verified printed pages, %d inferred",
                 entry["doc_id"], len(doc_chunks),
                 len(provenance["verified_pages"]), len(provenance["inferred_pages"]))

    try:
        facts, misses, skipped = _xbrl_facts(ticker)
    except Exception as exc:
        # No XBRL is a coverage gap, not a reason to lose the filings corpus.
        facts, misses, skipped = [], [], 0
        warnings.append(f"XBRL companyfacts unavailable ({exc}); facts.jsonl is empty "
                        "and the number-tie rule cannot run for this company")
    if facts:
        manifest.append(_xbrl_manifest_entry(ticker))
    else:
        warnings.append("facts.jsonl is empty: no XBRL facts for this company, so the "
                        "Fact Checker's number-tie rule cannot run against this corpus")
    for miss in misses:
        warnings.append(f"unmapped_metric {miss['metric']}: this filer tags none of "
                        f"{', '.join(miss['candidates_tried'])}; findings on it fall "
                        "back to the filing table and cap at Medium confidence")
    quarterly = {f["metric"] for f in facts if not f["fiscal_period"].startswith("FY")}
    if facts and not quarterly:
        warnings.append("XBRL has annual facts only for this company (typical of foreign "
                        "filers); quarterly figures need the deferred IR-workbook slice "
                        "before they can reach High confidence")
    for warning in warnings:
        log.warning("coverage: %s", warning)

    per_metric = {}
    for fact in facts:
        per_metric[fact["metric"]] = per_metric.get(fact["metric"], 0) + 1
    report = {
        "ticker": ticker,
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scope": "EDGAR periodic filings plus XBRL companyfacts for the subject company",
        "selection": audit,
        "page_attribution": pages,
        "facts": {
            "total": len(facts),
            "per_metric": per_metric,
            "unmapped_metrics": misses,
            "rows_skipped_unlabelable_or_non_currency": skipped,
        },
        "coverage_warnings": warnings,
        "deferred": ["facts.jsonl from IR Excel workbooks (foreign quarterlies)",
                     "DailyMed labels", "Orange/Purple Book", "rival filings",
                     "transcripts"],
    }
    _write_corpus(run_dir, manifest, chunks, facts, report)
    return report


async def run(run_dir):
    """Build the Contract 1 corpus for the ticker named by the run folder.

    Reads nothing; writes manifest.json, chunks.jsonl, facts.jsonl, and the
    librarian_report.json audit sidecar into `run_dir`.
    """
    ticker = Path(run_dir).name.split("-")[0]
    return await asyncio.to_thread(build_corpus, ticker, Path(run_dir))


def main(argv):
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if len(argv) != 1:
        print(f"usage: python3 -m pharmer.librarian <TICKER>\n"
              f"tickers: {', '.join(sorted(CIKS))}", file=sys.stderr)
        return 2
    ticker = argv[0].upper()
    run_dir = REPO_ROOT / "runs" / run_id_for(ticker)
    report = asyncio.run(run(run_dir))
    print(f"\ncorpus written to {run_dir}")
    for warning in report["coverage_warnings"]:
        print(f"  warning: {warning}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
