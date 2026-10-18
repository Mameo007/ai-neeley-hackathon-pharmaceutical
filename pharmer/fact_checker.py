"""Fact Checker (architecture §4.3): re-verification, not re-discovery.

For every reasoning in runs/<run_id>/findings.json this stage re-opens the cited chunk,
re-matches the quote, ties the anchor value to facts.jsonl at the precision the sources
themselves support, searches the corpus - comparative columns included - for corroboration
and the trend strip, and writes the Contract 2 fields it owns: `confidence`,
`confidence_reason`, `trend`, and the flags `reversal` / `possibly_seasonal`.

Deterministic checks are authoritative. One Haiku call per reasoning judges the single
question code cannot - does this evidence support this claim - and may only lower a
verdict, never raise one. Per-check detail goes to the non-contract sidecar
runs/<run_id>/fact_check_report.json, following runs/<run_id>/librarian_report.json.
"""
import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import anthropic

from pharmer import llm

log = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
STAGE = "fact-checker"
AGENT = "fact_checker"
MAX_TOKENS = 1000

# Byte-identical on every call; the reasoning under review goes in the user message.
SYSTEM = (PROMPTS_DIR / "fact_checker.md").read_text(encoding="utf-8")

OUTPUT_CONFIG = {
    "format": {
        "type": "json_schema",
        "schema": {
            "type": "object",
            "properties": {
                "claim_support": {"type": "string",
                                  "enum": ["supported", "contradicted", "unclear"]},
                "metric_label_matches": {"type": ["boolean", "null"]},
                "reason": {"type": "string"},
            },
            "required": ["claim_support", "metric_label_matches", "reason"],
            "additionalProperties": False,
        },
    },
}

# The only flags this stage owns; anything else on a reasoning (Dedup's `contested`,
# Quant's `unmapped_metric`, the Peer Agent's `peer_check_incomplete`) is left untouched.
OWNED_FLAGS = ("reversal", "possibly_seasonal")

# A direction change counts as a reversal only when both opposing moves clear this much
# of the earlier value, so rounding noise in a flat series is not read as a story.
REVERSAL_MIN_PCT = 1.0

# `reversal` describes the anchor's own recent direction, not the filer's whole history:
# the flip must appear among the last few moves of the strip, which are the ones the
# Synthesizer can recast as the story. A wobble eight periods back is not this finding's
# reversal. Three moves keeps Novo's documented FY2024 -> FY2025 -> Q2 2026 flip.
REVERSAL_WINDOW_MOVES = 3

# One Haiku judge call per reasoning, but not all at once: a shared cap would belong in
# pharmer/llm.py, which both partners use, so the limit is kept local to this stage until
# that interface is agreed (docs/build-log.md, 2026-10-18). The first reasoning is judged
# on its own so the cached system prefix is written before the rest fan out.
JUDGE_CONCURRENCY = 4

# XBRL companyfacts carries a filer's whole history - Lilly's revenue reaches back to
# FY2007 - and a 100-point strip is neither readable in the report nor informative. The
# strip keeps the points nearest the anchor, plus the anchor and its prior-year comparator.
TREND_MAX_POINTS = 8

# Balance-sheet items are point-in-time, so an FY, H and Q figure for one of them are all
# comparable balances and belong in one strip (the Contract 2 example mixes FY2024, H1 2025,
# FY2025 and H1 2026 receivables for exactly that reason). Flow items are not: a quarter's
# revenue against a full year's is a four-fold step that reads as a reversal every time, so
# a flow strip stays inside the anchor's own period family (FY with FY, Q2 with Q2).
BALANCE_METRICS = frozenset({"trade_receivables", "inventory", "cash", "total_assets",
                             "total_equity", "trade_payables"})

# Printed row labels for each concept-map metric (pharmer/librarian.py CONCEPT_MAP holds
# the XBRL side of the same mapping). A row label must *equal* one of these, because a
# label match is what grants a corpus hit: `Revenue from collaborations`, `Total revenues
# by segment`, `Receivables from related parties` and `Cash flow from operations` are not
# the statement lines the anchor names, and a prefix rule would hand each of them a hit
# (spec Boundaries: "same statement line, not a segment/product subtotal").
METRIC_LABELS = {
    "revenue": ("revenue", "revenues", "net revenue", "net revenues", "total revenue",
                "total revenues", "net sales", "total net sales", "sales", "turnover"),
    "trade_receivables": ("trade receivables", "accounts receivable", "receivables",
                          "trade and other receivables"),
    "inventory": ("inventory", "inventories"),
    "operating_profit": ("operating income", "operating profit", "income from operations",
                         "profit from operating activities"),
    "net_profit": ("net income", "net profit", "profit for the period", "profit for the year",
                   "net profit for the period"),
    "cost_of_sales": ("cost of sales", "cost of goods sold", "cost of revenue"),
    "gross_profit": ("gross profit", "gross margin"),
    "research_development": ("research and development", "research and development expenses"),
    "cash": ("cash and cash equivalents", "cash at bank", "cash"),
    "total_assets": ("total assets",),
    "total_equity": ("total equity", "stockholders equity", "shareholders equity"),
    "trade_payables": ("trade payables", "accounts payable", "trade and other payables"),
}

# Presentation words a filing adds to the same statement line, not a different concept.
_LABEL_SUFFIXES = frozenset({"net", "gross", "total"})

_YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")
_NUMERIC_RE = re.compile(r"\d+(?:\.\d+)?")
_SPAN_RE = re.compile(r"(?i)\b(three|six|nine|twelve)\s+months?\s+ended\b|\byears?\s+ended\b")
_NONPERIOD_RE = re.compile(r"(?i)\b(?:percent|%)\s*change\b|\bchange\b")
_FOOTNOTE_RE = re.compile(r"\(\d+\)")
_SPAN_KINDS = {"three": "Q", "six": "H", "twelve": "FY", "nine": None}
_MONTHS = {name: n for n, name in enumerate(
    ("january", "february", "march", "april", "may", "june",
     "july", "august", "september", "october", "november", "december"), start=1)}
_MONTH_RE = re.compile(r"(?i)\b(" + "|".join(_MONTHS) + r")\b")


# --------------------------------------------------------------------------
# numbers


def _parse_number(token):
    """(value, decimals) for one printed figure, or None if the token is not a figure.

    `(1,234)` is how filings print a negative. Trailing zeros are not precision: XBRL's
    45,043,000,000 arrives in facts.jsonl as 45043.0, an integer figure, so it must not
    claim one decimal place of accuracy it does not have.
    """
    text = str(token).strip()
    negative = text.startswith("(") and text.endswith(")")
    body = text.strip("()").replace(",", "").replace("$", "").replace("%", "").strip()
    if body.startswith("-"):
        negative, body = True, body[1:].strip()
    if not _NUMERIC_RE.fullmatch(body):
        return None
    fraction = body.split(".")[1].rstrip("0") if "." in body else ""
    value = float(body)
    return (-value if negative else value), len(fraction)


def _number_of(value):
    """(value, decimals) for a JSON number, or None for anything else."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return _parse_number(f"{float(value):.6f}".rstrip("0").rstrip(".") or "0")


def _ties(left, right):
    """Numeric equality at the precision both sources support.

    Tolerance is half a unit of the *coarser* value's last decimal place, which is exactly
    what rounding one of them could have changed. Lilly's FY2024 revenue prints as 45,042.7
    in the FY2024 10-K and 45,043 in the FY2025 10-K; min(dp) is 0, so a 0.5 tolerance ties
    them, while 45,142 (99.3 away) stays a contradiction. A broad percentage tolerance would
    have accepted a genuinely different number.
    """
    (left_value, left_dp), (right_value, right_dp) = left, right
    tolerance = 0.5 * 10 ** -min(left_dp, right_dp)
    return abs(left_value - right_value) <= tolerance + 1e-9


def _norm_space(text):
    """Collapse every run of whitespace to one space."""
    return " ".join(str(text).split())


def _norm_period(label):
    """`q2 2026` -> `Q2 2026`; the label vocabulary of Contract 1a."""
    return " ".join(str(label or "").upper().split())


def _norm_label(label):
    """Row label to its comparable form: no footnote markers, no punctuation, casefolded."""
    text = _FOOTNOTE_RE.sub(" ", str(label or ""))
    text = re.sub(r"[^\w\s]", " ", text)
    return _norm_space(text).casefold()


def _label_matches(metric, label):
    """Whether a printed row label names `metric`. None when neither can be judged.

    Equality after normalization, with only the presentation words filings add to the same
    line removed (`Accounts receivable, net` -> `accounts receivable`). Anything that adds
    a qualifier - a product, a segment, a counterparty, a different statement - is a
    different row and gets no hit; the Haiku judge, not this table, settles the genuinely
    ambiguous labels.
    """
    aliases = METRIC_LABELS.get(metric)
    norm = _norm_label(label)
    if aliases is None or not norm:
        return None
    words = norm.split()
    while words and words[-1] in _LABEL_SUFFIXES:
        words.pop()
    return bool(words) and " ".join(words) in aliases


# --------------------------------------------------------------------------
# quote verification


def _chunk_text(chunk):
    """A chunk's text, tolerant of a malformed row.

    Contract 1b requires `text`, but this stage's contract is that it never raises: a
    corpus row missing or mistyping the field must come back as unverifiable evidence,
    not as a KeyError that takes the whole run down.
    """
    text = (chunk or {}).get("text")
    return text if isinstance(text, str) else ""


def _verify_quote(quote, chunk):
    """How well `quote` reproduces in the chunk: exact, normalized, partial or absent.

    `normalized` collapses whitespace runs on both sides, and is the explicit fix for the
    two corpora disagreeing on table-cell separators - Partner B's sample corpus joins
    cells with spaces, the Librarian with tabs, so the same table yields different `text`
    for the same `chunk_id` (docs/build-log.md, 2026-10-18). A tabs-vs-spaces difference
    loses no evidence, so it still allows High; `partial`, which needs punctuation and case
    to be discarded too, does not.
    """
    text = _chunk_text(chunk)
    if not text or not quote:
        return "absent"
    if quote in text:
        return "exact"
    if chunk.get("is_table") and _norm_space(quote) in _norm_space(text):
        return "normalized"
    loose_quote = re.sub(r"[^\w.,()%-]+", " ", quote).casefold()
    loose_text = re.sub(r"[^\w.,()%-]+", " ", text).casefold()
    if _norm_space(loose_quote) and _norm_space(loose_quote) in _norm_space(loose_text):
        return "partial"
    return "absent"


def _value_in_text(value, text):
    """Whether `value` is printed in `text`, at either source's precision.

    The corpus keeps cell text verbatim, so the figure may appear as `45,042.7`, `(1,234)`
    or `88,949`; each candidate is compared numerically rather than as a string. This is
    asked of the *quote*, never of the whole chunk: a figure that merely sits somewhere on
    the same page is not evidence the analyst cited it.
    """
    target = _number_of(value)
    if target is None:
        return False
    for token in re.findall(r"\(?\$?\s?-?\d[\d,]*(?:\.\d+)?\)?", text):
        parsed = _parse_number(token)
        if parsed is not None and _ties(parsed, target):
            return True
    return False


# --------------------------------------------------------------------------
# table columns


def _cells(line):
    return [cell.strip() for cell in line.split("\t")]


def _is_header_line(cells):
    """True when no cell is a bare figure other than a four-digit year.

    `Year Ended December 31,` and `U.S.` are header text; `65,179` and `45` are data, and
    the first of them ends the header.
    """
    for cell in cells:
        if _parse_number(cell) is not None and not _YEAR_RE.match(cell.strip()):
            return False
    return True


def _period_label(kind, month, year):
    """Contract 1a label for a column, matching librarian._fiscal_period()."""
    if kind == "FY":
        return f"FY{year}"
    quarter = (month - 1) // 3 + 1
    if kind == "H":
        return f"H{1 if quarter <= 2 else 2} {year}"
    return f"Q{quarter} {year}"


def _header_tokens(text):
    """Header cells in reading order as ('span', kind, month) / ('year', y) / ('gap',).

    `gap` is a declared non-period column (`Percent Change`). None when a span cannot be
    given a Contract 1a label at all, which abstains on the whole table.
    """
    tokens = []
    for line in text.split("\n"):
        cells = _cells(line)
        if not _is_header_line(cells):
            break
        for cell in cells:
            if _YEAR_RE.match(cell.strip()):
                tokens.append(("year", int(cell.strip())))
                continue
            span = _SPAN_RE.search(cell)
            if span:
                kind = _SPAN_KINDS.get((span.group(1) or "").lower(), "FY")
                found = _MONTH_RE.search(cell)
                month = _MONTHS[found.group(1).lower()] if found else None
                if kind is None or month is None:
                    # A nine-month column has no Contract 1a label, and a span with no
                    # month end cannot be placed in a quarter - abstain on the table.
                    return None
                tokens.append(("span", kind, month))
            elif _NONPERIOD_RE.search(cell):
                tokens.append(("gap",))
    return tokens


def _column_periods(text):
    """Ordered periods for a table's value columns, or None when the header is unreadable.

    `None` entries are declared non-period columns such as `Percent Change`, **kept in
    their own column position**: `Revenue 65,179 45 45,043` under
    `2025 | Percent Change | 2024` must not hand the percent-change cell to FY2024. A
    non-period column named before any year belongs after the year columns of the span it
    follows, which is how `Year Ended December 31, | Percent Change` over `2024 | 2023`
    prints (LLY FY2024 10-K p.41).

    Returning None for the whole table is deliberate and common: a nested table
    (`U.S. / Outside U.S. / Total`) prints more value columns than its header declares
    periods, and attributing a value to the wrong column is the precise error the
    number-tie rule exists to catch (docs/architecture.md §2).
    """
    tokens = _header_tokens(text)
    if tokens is None:
        return None
    spans = [token for token in tokens if token[0] == "span"]
    years = [token[1] for token in tokens if token[0] == "year"]
    if not spans or not years or len(years) % len(spans):
        return None
    per_span = len(years) // len(spans)
    labels = [_period_label(kind, month, year)
              for index, (_tag, kind, month) in enumerate(spans)
              for year in years[index * per_span:(index + 1) * per_span]]

    periods, gaps, year_i, spans_seen = [], [], 0, 0
    for token in tokens:
        if token[0] == "year":
            periods.append(labels[year_i])
            year_i += 1
        elif token[0] == "span":
            spans_seen += 1
        else:
            # Interleaved with the years: it holds the column it was printed in. Named
            # before any year: it follows the year columns of the spans declared so far.
            gaps.append(year_i if year_i else spans_seen * per_span)
    for offset, index in enumerate(gaps):
        periods.insert(min(index + offset, len(periods)), None)
    return periods


def _row_values(cells):
    """[(value, decimals)] for the value cells of a tab-joined row, in column order."""
    parsed = [_parse_number(cell) for cell in cells]
    return [number for number in parsed if number is not None]


def _spaced_row(line):
    """(label, [(value, decimals)]) for a space-joined row, or ("", []) if it is not one.

    Partner B's sample corpus joins table cells with spaces, so a row arrives as
    `Trade receivables 88,949 70,856`: the label is the leading words and the figures are
    the trailing run. A row whose words resume after the figures (`30 Jun 2026 31 Dec
    2025`) is not a label-then-figures row and is refused rather than guessed at.
    """
    tokens = line.split()
    values, label_end = [], len(tokens)
    for index, token in enumerate(tokens):
        number = _parse_number(token)
        if number is not None:
            if not values:
                label_end = index
            values.append(number)
        elif values and token not in ("$", "%"):
            return "", []
    return " ".join(tokens[:label_end]), values


def _table_rows(chunk, doc_period):
    """[(label, period_or_None, (value, decimals))] for every figure in a table chunk.

    Column periods come from the header when it reads cleanly. When it does not - including
    every space-joined chunk - the row's first figure is attributed to the document's own
    reporting period, because financial statements print the reporting period first and
    comparatives after it; the remaining figures are left unattributed. When the header
    *was* read but its column count disagrees with the row, nothing is attributed: that
    disagreement means the table is nested.
    """
    text = _chunk_text(chunk)
    tabbed = "\t" in text
    periods = _column_periods(text) if tabbed else None
    rows, in_header = [], tabbed
    for line in text.split("\n"):
        if tabbed:
            cells = _cells(line)
            if in_header and _is_header_line(cells):
                continue
            in_header = False
            label, values = (cells[0] if cells else ""), _row_values(cells)
        else:
            label, values = _spaced_row(line)
        if not values:
            continue
        if periods is not None and len(values) == len(periods):
            columns = periods
        elif periods is not None:
            columns = [None] * len(values)
        else:
            columns = [doc_period] + [None] * (len(values) - 1)
        rows.extend((label, period, value) for period, value in zip(columns, values))
    return rows


# --------------------------------------------------------------------------
# corpus


class CorpusIndex:
    """One run's Contract 1 files, indexed once and shared read-only by every reasoning.

    Named apart from `pharmer.hunters.harness.Corpus`, which is Partner B's search-side
    view of the same files; this one answers verification questions.

    Carries the per-document ticker as well as the period, because the corpus is not
    single-company for long: architecture §4.0 adds rival filings and rival annual
    reports, and a tie or a trend point must never be drawn from another filer's
    evidence just because the metric, period, unit and currency happen to agree.
    """

    def __init__(self, chunks, facts, manifest, warnings):
        self.chunks = chunks
        self.facts = facts
        self.manifest = [entry for entry in manifest if isinstance(entry, dict)]
        self.warnings = warnings
        self.doc_periods = {entry.get("doc_id"): _norm_period(entry.get("fiscal_period"))
                            for entry in self.manifest}
        self.doc_tickers = {entry.get("doc_id"): str(entry.get("ticker") or "").upper()
                            for entry in self.manifest}
        self._hits = {}
        self._facts = {}

    def owner(self, doc_id):
        """The ticker a doc_id belongs to, or "" when the manifest does not say."""
        return self.doc_tickers.get(doc_id, "")

    def is_other_company(self, doc_id, ticker):
        """True only when both sides are known and they disagree."""
        owner = self.owner(doc_id)
        return bool(owner and ticker) and owner != str(ticker).upper()

    def places(self, doc_id, ticker):
        """True when the manifest positively places `doc_id` with `ticker`.

        The mirror of `is_other_company`, for the one decision that must not be made on
        an absence: granting High. Unknown ownership is read, but never certified.
        """
        owner = self.owner(doc_id)
        return bool(owner and ticker) and owner == str(ticker).upper()

    def facts_for(self, ticker):
        """The subject company's facts; rows with an unattributable doc_id are kept."""
        key = str(ticker or "").upper()
        if key not in self._facts:
            self._facts[key] = [fact for fact in self.facts
                                if not self.is_other_company(fact.get("doc_id"), ticker)]
        return self._facts[key]

    def hits(self, metric, ticker):
        """Table figures naming `metric` in the subject company's documents.

        [(period, (value, decimals), chunk_id, label)]. Searches every table chunk of that
        company, so a figure found only in a *comparative column of another document* is
        still found: Lilly's FY2024 revenue is printed in the FY2025 10-K's comparative
        column, not in the FY2024 10-K's own column of the newer presentation
        (docs/build-log.md, 2026-10-18). Memoized because every reasoning of a composite
        finding asks the same question of the same corpus.
        """
        key = (metric, str(ticker or "").upper())
        if key not in self._hits:
            hits = []
            for chunk_id, chunk in self.chunks.items():
                if not chunk.get("is_table"):
                    continue
                if self.is_other_company(chunk.get("doc_id"), ticker):
                    continue
                doc_period = self.doc_periods.get(chunk.get("doc_id"))
                for label, period, value in _table_rows(chunk, doc_period):
                    if period and _label_matches(metric, label):
                        hits.append((period, value, chunk_id, label))
            self._hits[key] = hits
        return self._hits[key]


def _load_corpus(run_dir):
    """A `Corpus` for one run folder, with a warning per file the stage had to do without.

    A missing corpus is reported, never inferred as bad evidence: the warnings travel to
    `fact_check_report.json` so "the Librarian did not run here" cannot be read off the
    findings as "the hunters cited things that are not there".
    """
    warnings = []

    def jsonl(name):
        path = run_dir / name
        if not path.exists():
            warnings.append(f"{name} is missing from the run folder")
            return []
        rows = []
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                warnings.append(f"{name} line {number} is not valid JSON and was skipped")
        if not rows:
            warnings.append(f"{name} has no usable rows")
        return rows

    chunks = {chunk["chunk_id"]: chunk for chunk in jsonl("chunks.jsonl")
              if isinstance(chunk, dict) and chunk.get("chunk_id")}
    facts = [fact for fact in jsonl("facts.jsonl") if isinstance(fact, dict)]
    manifest_path = run_dir / "manifest.json"
    manifest = []
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except ValueError:
            warnings.append("manifest.json is not valid JSON and was ignored")
        if not isinstance(manifest, list):
            warnings.append("manifest.json is not a list of Contract 1a rows")
            manifest = []
    else:
        warnings.append("manifest.json is missing, so column periods and document owners "
                        "cannot be resolved")
    for warning in warnings:
        log.warning("fact checker: %s", warning)
    return CorpusIndex(chunks, facts, manifest, warnings)


# --------------------------------------------------------------------------
# number tie


def _tie(anchor, facts, attributed=None):
    """Number-tie verdict for one anchor against facts.jsonl.

    `attributed(fact)` says whether the manifest places that fact's document with the
    subject company. A fact nobody can place is still read - not knowing the owner is not
    evidence of a different owner - but it cannot *grant* a tie, because the whole value of
    the number-tie rule is that the second artifact is known to be about this company.

    Returns (status, fact_id, detail) where status is:
      `reproduced`   - a fact for the same metric, period, unit and currency agrees
                       numerically at the precision the two sources support;
      `unattributed` - such a fact agrees, but the manifest cannot place its document with
                       the subject company, so the agreement is unconfirmable: Medium, and
                       never a High that rests on evidence of unknown ownership;
      `contradicted` - such a fact exists and disagrees beyond that precision;
      `mismatch`     - the value ties to a fact of a *different* period, unit or currency,
                       which is the wrong-column / wrong-period extraction error the rule
                       exists to catch (docs/architecture.md §2);
      `unavailable`  - this company has no second source for that metric and period, as
                       with a foreign filer's quarterly figures (annual-only XBRL);
      `not_applicable` - the anchor carries no number, so there is nothing to tie. A
                       label/product finding is about breadth or wording, not a figure
                       (Contract 2 `is_label_finding`, architecture §5's label
                       guarantee); a missing number is unconfirmable, never a
                       contradiction, so such a finding stays eligible for Medium instead
                       of being dropped by the scoring gate.
    """
    target = _number_of(anchor.get("value"))
    if target is None:
        return "not_applicable", None, "the anchor carries no number to tie"
    metric = anchor.get("metric")
    period = _norm_period(anchor.get("fiscal_period"))
    unit = str(anchor.get("unit") or "").casefold()
    currency = str(anchor.get("currency") or "").upper()

    candidates = [fact for fact in facts if fact.get("metric") == metric]
    same_period = [fact for fact in candidates
                   if _norm_period(fact.get("fiscal_period")) == period]
    for fact in same_period:
        fact_value = _number_of(fact.get("value"))
        if fact_value is None or not _ties(fact_value, target):
            continue
        fact_unit = str(fact.get("unit") or "").casefold()
        fact_currency = str(fact.get("currency") or "").upper()
        if fact_unit != unit:
            return ("mismatch", fact.get("fact_id"),
                    f"{fact.get('fact_id')} is in {fact.get('unit')}, the anchor in "
                    f"{anchor.get('unit')}")
        if fact_currency != currency:
            return ("mismatch", fact.get("fact_id"),
                    f"{fact.get('fact_id')} is in {fact.get('currency')}, the anchor in "
                    f"{anchor.get('currency')}")
        if not unit or not currency:
            # Equal-but-absent is not agreement. Contract 1c requires both on every fact,
            # so a blank here means one side never stated its scale - and a figure whose
            # unit is unknown could be thousands or millions. Unconfirmable, not a tie.
            return ("unavailable", fact.get("fact_id"),
                    f"{fact.get('fact_id')} and the anchor agree numerically, but the "
                    f"unit or currency is missing on one of them")
        if attributed is not None and not attributed(fact):
            return ("unattributed", fact.get("fact_id"),
                    f"{fact.get('fact_id')} = {fact.get('value')} agrees with the anchor, "
                    f"but manifest.json does not place {fact.get('doc_id')} with "
                    f"{anchor.get('entity') or 'the subject company'}")
        return ("reproduced", fact.get("fact_id"),
                f"{fact.get('fact_id')} = {fact.get('value')} reproduces the anchor at "
                f"{min(fact_value[1], target[1])} dp")
    for fact in candidates:
        fact_value = _number_of(fact.get("value"))
        if fact_value is not None and _ties(fact_value, target):
            return ("mismatch", fact.get("fact_id"),
                    f"the anchor value is {metric} for {fact.get('fiscal_period')} "
                    f"({fact.get('fact_id')}), not {period}")
    if same_period:
        shown = ", ".join(f"{f.get('fact_id')} = {f.get('value')}" for f in same_period[:3])
        return "contradicted", same_period[0].get("fact_id"), f"{shown} contradict the anchor"
    if not candidates:
        return "unavailable", None, f"facts.jsonl has no {metric} fact"
    return "unavailable", None, f"facts.jsonl has no {metric} fact for {period}"


# --------------------------------------------------------------------------
# trend, reversal, seasonality


def _period_sort_key(label):
    """Chronological key over FY2025 / Q2 2026 / H1 2026 labels."""
    label = _norm_period(label)
    if label.startswith("FY"):
        return (int(label[2:]), 4, 1, label)
    kind, year = label.split()
    index = int(kind[1:])
    return (int(year), index if kind[0] == "Q" else index * 2, 0, label)


def _period_family(label):
    """`FY2025` -> `FY`, `Q2 2026` -> `Q2`, `H1 2026` -> `H1` - the comparable series."""
    label = _norm_period(label)
    return "FY" if label.startswith("FY") else label.split()[0]


def _trend(anchor, facts, hits):
    """Contract 2 `trend`: the anchor's metric across the corpus's periods, oldest first.

    facts.jsonl wins over a filing table for the same period - it is the structured source
    and carries the newest presentation of a restated figure. A flow metric is kept inside
    the anchor's period family so the strip is a like-for-like series; a balance metric
    keeps every period, since all of them are comparable balances. The strip is then capped
    to the points nearest the anchor, which always keeps the anchor and the prior-year
    comparator the seasonality check needs.
    """
    values = {}
    for period, value, _chunk_id, _label in hits:
        values.setdefault(_norm_period(period), value[0])
    metric = anchor.get("metric")
    for fact in facts:
        if fact.get("metric") != metric:
            continue
        number = _number_of(fact.get("value"))
        if number is not None:
            values[_norm_period(fact.get("fiscal_period"))] = number[0]
    anchor_period = _norm_period(anchor.get("fiscal_period"))
    anchor_value = _number_of(anchor.get("value"))
    if anchor_value is not None:
        values.setdefault(anchor_period, anchor_value[0])

    try:
        if metric not in BALANCE_METRICS and anchor_period:
            family = _period_family(anchor_period)
            values = {period: value for period, value in values.items()
                      if _period_family(period) == family}
        periods = sorted(values, key=_period_sort_key)
        if len(periods) > TREND_MAX_POINTS:
            kept = {anchor_period, _prior_year(anchor_period)} & set(periods)
            anchor_key = _period_sort_key(anchor_period)
            nearest = sorted(periods, key=lambda p: abs(_period_sort_key(p)[0] - anchor_key[0]))
            for period in nearest:
                if len(kept) >= TREND_MAX_POINTS:
                    break
                kept.add(period)
            periods = [period for period in periods if period in kept]
    except (ValueError, IndexError, KeyError):  # an odd period label must not kill the run
        log.warning("fact checker: unusable period label in %s", sorted(values))
        periods = sorted(values)
    return [{"fiscal_period": period, "value": values[period]} for period in periods]


def _moves(trend):
    """Signed direction of each period-to-period change that clears REVERSAL_MIN_PCT."""
    moves = []
    for earlier, later in zip(trend, trend[1:]):
        before, after = earlier["value"], later["value"]
        if not before:
            continue
        percent = (after - before) / abs(before) * 100
        if abs(percent) >= REVERSAL_MIN_PCT:
            moves.append(1 if percent > 0 else -1)
    return moves


def _reversal(trend, anchor_period=None):
    """True when the direction flips among the moves that end at the anchor.

    The architecture keeps a reversal as the story rather than resolving it away, so the
    flag is derived from the trend rather than from a special-case rule (§4.3). Two
    restrictions keep the flag about *this* finding: only moves up to and including the
    anchor's own count (a later period's behaviour is not this anchor's story), and only
    the last REVERSAL_WINDOW_MOVES of them, so a wobble years before the anchor does not
    put "reversal" on a finding whose own direction never changed. Both opposing moves
    must clear REVERSAL_MIN_PCT so a flat series does not flap.
    """
    periods = [point["fiscal_period"] for point in trend]
    anchor_period = _norm_period(anchor_period) if anchor_period else ""
    if anchor_period in periods:
        trend = trend[:periods.index(anchor_period) + 1]
    moves = _moves(trend)[-REVERSAL_WINDOW_MOVES:]
    return any(first != second for first, second in zip(moves, moves[1:]))


def _prior_year(label):
    """`Q2 2026` -> `Q2 2025`, `FY2025` -> `FY2024`."""
    label = _norm_period(label)
    if label.startswith("FY"):
        return f"FY{int(label[2:]) - 1}"
    kind, year = label.split()
    return f"{kind} {int(year) - 1}"


def _like_for_like(anchor, trend, reversal):
    """(status, detail) over `confirmed` / `unconfirmed` / `not_applicable`.

    The check exists for one failure: reading a part-year figure against a year end and
    calling the difference a story when it is seasonality (docs/architecture.md §4.3 -
    "H1 2026 vs. H1 2025, using the prior-year same-quarter filing"). So it is asked of a
    part-year anchor, and of any anchor whose direction reversed. A full-year figure has
    no seasonality to confirm: FY2024 against FY2023 is already like-for-like, and
    flagging `possibly_seasonal` there - which the first implementation did for every
    anchor at the oldest end of the corpus - both mislabels the finding and capped an
    otherwise complete verification at Medium for a reason that cannot be cured.
    """
    period = _norm_period(anchor.get("fiscal_period"))
    try:
        wanted = _prior_year(period)
    except (ValueError, IndexError):
        return "unconfirmed", f"cannot name the prior-year period for {period or 'the anchor'}"
    if any(point["fiscal_period"] == wanted for point in trend):
        return "confirmed", f"like-for-like against {wanted} confirmed"
    if not reversal and _period_family(period) == "FY":
        return ("not_applicable",
                "a full-year figure needs no seasonality check")
    return "unconfirmed", f"no {wanted} comparator in the corpus"


# --------------------------------------------------------------------------
# claim support


def claim_message(anchor, reasoning, chunks, trend):
    """User message for one reasoning: the anchor, the claim, the quotes, the trend."""
    lines = ["# Anchor", "", json.dumps(anchor, ensure_ascii=False), "", "# Claim", "",
             json.dumps(reasoning.get("claim"), ensure_ascii=False), "", "# Quotes"]
    for citation in reasoning.get("citations") or []:
        chunk = chunks.get(citation.get("chunk_id"))
        where = (f"{chunk.get('doc_id', citation.get('chunk_id'))} "
                 f"p.{chunk.get('page', '?')}, {chunk.get('section', '')}"
                 if chunk else "chunk not in the corpus")
        lines.append(f"- {json.dumps(citation.get('quote'), ensure_ascii=False)} ({where})")
    if trend:
        lines += ["", "# Trend", "",
                  json.dumps(trend, ensure_ascii=False)]
    return "\n".join(lines)


def parse_support(message):
    """(claim_support, metric_label_matches, reason) from the reply.

    A refusal, truncation or malformed reply leaves claim support `unverified`, which caps
    the finding at Medium: an unavailable judge is not evidence against the claim.
    """
    stop = getattr(message, "stop_reason", None)
    if stop != "end_turn":
        log.warning("fact checker judge stop_reason=%s", stop)
        return "unverified", None, f"claim support unverified (stop_reason={stop})"
    text = "".join(getattr(block, "text", "") for block in message.content
                   if block.type == "text")
    try:
        reply = json.loads(text)
        support = reply["claim_support"]
        label_matches = reply["metric_label_matches"]
    except (ValueError, KeyError, TypeError) as exc:
        log.warning("fact checker judge reply is not valid JSON: %s", exc)
        return "unverified", None, "claim support unverified (malformed reply)"
    if support not in ("supported", "contradicted", "unclear") or not (
            label_matches is None or isinstance(label_matches, bool)):
        log.warning("fact checker judge reply is out of schema: %s", text)
        return "unverified", None, "claim support unverified (reply out of schema)"
    reason = reply.get("reason") or ""
    if support == "unclear":
        return "unverified", label_matches, f"claim support unclear: {reason}"
    return support, label_matches, reason


async def judge_claim(run_id, anchor, reasoning, chunks, trend):
    """Ask Haiku whether the quoted evidence supports the claim; never raises."""
    # Built outside the try, so the broad catch below can only be about reaching the API -
    # a TypeError from assembling our own message is our bug and must not be swallowed.
    content = claim_message(anchor, reasoning, chunks, trend)
    try:
        message = await llm.call(
            run_id=run_id, stage=STAGE, agent=AGENT, model=llm.HAIKU, max_tokens=MAX_TOKENS,
            system=SYSTEM, messages=[{"role": "user", "content": content}],
            output_config=OUTPUT_CONFIG)
    except (anthropic.AnthropicError, TypeError, OSError) as exc:
        # AnthropicError covers API failures; constructing the client with no credentials
        # raises TypeError ("Could not resolve authentication method"), which is the most
        # likely cause of all in practice - the acceptance criterion for a key-less run is
        # that the stage completes with every reasoning capped at Medium.
        log.warning("fact checker judge request failed: %s", exc)
        return "unverified", None, "claim support unverified (judge unavailable)"
    return parse_support(message)


# --------------------------------------------------------------------------
# confidence


def _confidence(checks):
    """(confidence, reason) from the completed checks - the whole rule, in one place.

    High needs every check to pass. A check that is *unconfirmable* caps the finding at
    Medium; a check that is *contradicted or unsupported* makes it Low, which Partner B's
    scoring gate then excludes.
    """
    reasons, fatal = [], []
    quote = checks["quote"]
    if quote == "exact":
        reasons.append("verbatim quote match")
    elif quote == "normalized":
        reasons.append("quote matches once table whitespace is normalized")
    elif quote == "partial":
        reasons.append("only a partial quote match")
    elif quote == "corpus_missing":
        # Not the hunter's fault and not a contradiction: the evidence was never offered
        # to the check. Unconfirmable caps at Medium; Low would read as "the citation is
        # wrong" and the scoring gate would drop a finding nobody has examined.
        reasons.append(checks["quote_detail"])
    else:
        fatal.append(checks["quote_detail"])

    if checks.get("anchor_has_value", True):
        if quote in ("exact", "normalized", "partial") and not checks["value_in_evidence"]:
            fatal.append("the anchor value is not printed in the cited evidence")
    else:
        reasons.append("the anchor carries no figure to locate in the evidence")

    tie = checks["tie"]
    if tie == "reproduced":
        reasons.append(f"number-tie reproduces ({checks['tie_detail']})")
    elif tie == "unavailable":
        reasons.append(f"no second source ({checks['tie_detail']})")
    elif tie == "unattributed":
        # Agreement from a document of unknown ownership is not the cross-source
        # verification Contract 2 asks for, so it cannot carry a finding to High.
        reasons.append(f"second source unattributed ({checks['tie_detail']})")
    elif tie == "not_applicable":
        reasons.append(f"no number-tie applies ({checks['tie_detail']})")
    else:
        fatal.append(checks["tie_detail"])

    like_for_like = checks["like_for_like"]
    if like_for_like == "confirmed":
        reasons.append(checks["like_for_like_detail"])
    elif like_for_like == "not_applicable":
        reasons.append(checks["like_for_like_detail"])
    else:
        reasons.append(f"possibly seasonal: {checks['like_for_like_detail']}")

    support = checks["claim_support"]
    if support == "supported":
        reasons.append(f"claim supported: {checks['claim_detail']}")
    elif support == "contradicted":
        fatal.append(f"claim contradicted by its own evidence: {checks['claim_detail']}")
    else:
        reasons.append(checks["claim_detail"])

    if checks.get("metric_label_matches") is False:
        fatal.append("the cited row names a different financial concept than the anchor")

    if fatal:
        return "Low", "; ".join(fatal) + "."
    value_ok = checks["value_in_evidence"] or not checks.get("anchor_has_value", True)
    passed = (quote in ("exact", "normalized") and value_ok and tie == "reproduced"
              and like_for_like in ("confirmed", "not_applicable") and support == "supported")
    return ("High" if passed else "Medium"), "; ".join(reasons) + "."


def _check_citations(reasoning, corpus, anchor):
    """(quote status, detail, anchor value found in the cited evidence).

    A reasoning can carry citations from several hunters and documents, so the best quote
    match wins and the anchor value may be printed in any of them. With no corpus to open
    at all the status is `corpus_missing`, which is unconfirmable rather than fatal - the
    citations were never checked, so they cannot be called wrong.
    """
    citations = reasoning.get("citations") or []
    if not citations:
        return "absent", "the reasoning cites no evidence", False
    if not corpus.chunks:
        return ("corpus_missing",
                "chunks.jsonl holds no rows, so the citations could not be re-opened", False)
    order = {"exact": 0, "normalized": 1, "partial": 2, "absent": 3}
    best, detail, value_found = None, "", False
    for citation in citations:
        quote = citation.get("quote") or ""
        chunk = corpus.chunks.get(citation.get("chunk_id"))
        if chunk is None:
            status = "absent"
            why = f"{citation.get('chunk_id')} is not in chunks.jsonl"
        else:
            status = _verify_quote(quote, chunk)
            why = (f"the quote is not in {citation.get('chunk_id')}" if status == "absent"
                   else f"{citation.get('chunk_id')} matched ({status})")
            # The value must be carried by a quote that verified, not merely by the page.
            if status != "absent" and _value_in_text(anchor.get("value"), quote):
                value_found = True
        if best is None or order[status] < order[best]:
            best, detail = status, why
    return best, detail, value_found


# --------------------------------------------------------------------------
# stage


async def check_reasoning(run_id, finding, reasoning, corpus, gate=None):
    """Verify one reasoning and write the Fact Checker's Contract 2 fields onto it.

    `gate` is an optional asyncio.Semaphore capping concurrent judge calls. Returns the
    per-reasoning row for the sidecar report; every reasoning of a composite finding is
    verified on its own, against the same shared `corpus` index.
    """
    anchor = finding.get("anchor") or {}
    ticker = finding.get("ticker")
    facts = corpus.facts_for(ticker)
    quote, quote_detail, value_in_evidence = _check_citations(reasoning, corpus, anchor)
    hits = corpus.hits(anchor.get("metric"), ticker)
    trend = _trend(anchor, facts, hits)
    tie, tie_fact_id, tie_detail = _tie(
        anchor, facts, attributed=lambda fact: corpus.places(fact.get("doc_id"), ticker))
    anchor_period = _norm_period(anchor.get("fiscal_period"))
    reversal = _reversal(trend, anchor_period)
    like_for_like, like_detail = _like_for_like(anchor, trend, reversal)
    anchor_number = _number_of(anchor.get("value"))

    # Nothing to judge when the evidence could not be opened or does not carry the figure:
    # either the deterministic verdict is already Low, or there is no corpus to judge
    # against. A label finding carries no figure, so only the quote has to verify.
    judged = quote in ("exact", "normalized", "partial") and (
        value_in_evidence or anchor_number is None)
    if not judged:
        support, label_matches, claim_detail = (
            "unverified", None, "claim support not judged (evidence unverified)")
    elif gate is not None:
        async with gate:
            support, label_matches, claim_detail = await judge_claim(
                run_id, anchor, reasoning, corpus.chunks, trend)
    else:
        support, label_matches, claim_detail = await judge_claim(
            run_id, anchor, reasoning, corpus.chunks, trend)

    corroboration = [
        {"chunk_id": chunk_id, "fiscal_period": period, "value": value[0], "label": label}
        for period, value, chunk_id, label in hits
        if period == anchor_period and anchor_number is not None and _ties(value, anchor_number)
    ]

    checks = {
        "quote": quote, "quote_detail": quote_detail,
        "anchor_has_value": anchor_number is not None,
        "value_in_evidence": value_in_evidence,
        "tie": tie, "tie_detail": tie_detail,
        "like_for_like": like_for_like, "like_for_like_detail": like_detail,
        "claim_support": support, "claim_detail": claim_detail,
        "metric_label_matches": label_matches,
    }
    confidence, reason = _confidence(checks)

    reasoning["confidence"] = confidence
    reasoning["confidence_reason"] = reason
    reasoning["trend"] = trend
    flags = [flag for flag in reasoning.get("flags") or [] if flag not in OWNED_FLAGS]
    if reversal:
        flags.append("reversal")
    if like_for_like == "unconfirmed":
        flags.append("possibly_seasonal")
    reasoning["flags"] = flags

    return {"finding_id": finding.get("finding_id"),
            "reasoning_id": reasoning.get("reasoning_id"),
            "anchor": {"metric": anchor.get("metric"), "fiscal_period": anchor_period,
                       "value": anchor.get("value"), "ticker": ticker},
            "confidence": confidence, "flags": flags, "checks": checks,
            "judged": judged, "tie_fact_id": tie_fact_id, "corroboration": corroboration,
            "trend_periods": [point["fiscal_period"] for point in trend]}


def _write_json(path, payload):
    """Write JSON atomically, so an interrupted run cannot truncate the pipeline's state.

    findings.json is the only copy of everything the hunters and Dedup produced; a crash
    halfway through `json.dump` over the live file would destroy it.
    """
    tmp = path.with_name(path.name + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)  # leave no half-written sibling behind
        raise


async def run(run_dir: Path) -> None:
    """Read run_dir/findings.json, assign the Fact Checker's fields, write it back.

    Touches only `reasonings[].confidence`, `confidence_reason`, `trend` and the
    `reversal` / `possibly_seasonal` flags; every other Contract 2 field belongs to
    another stage and is left exactly as found. Also writes the non-contract sidecar
    run_dir/fact_check_report.json.
    """
    run_dir = Path(run_dir)
    findings = json.loads((run_dir / "findings.json").read_text(encoding="utf-8"))
    corpus = _load_corpus(run_dir)
    warnings = list(corpus.warnings)
    if not corpus.facts:
        warnings.append("no facts.jsonl rows, so no number-tie can reproduce and every "
                        "reasoning caps at Medium")
        log.warning("fact checker: %s", warnings[-1])

    pairs = [(finding, reasoning) for finding in findings
             for reasoning in finding.get("reasonings") or []]
    gate = asyncio.Semaphore(JUDGE_CONCURRENCY)
    # The first reasoning goes alone so the cached system prefix is written before the
    # rest fan out; otherwise every concurrent first call pays the full prompt
    # (docs/contracts.md Contract 3, `cache_read_input_tokens`).
    rows = []
    for finding, reasoning in pairs[:1]:
        rows.append(await check_reasoning(run_dir.name, finding, reasoning, corpus))
    rows += await asyncio.gather(*(
        check_reasoning(run_dir.name, finding, reasoning, corpus, gate)
        for finding, reasoning in pairs[1:]))

    _write_json(run_dir / "findings.json", findings)

    counts = {level: sum(row["confidence"] == level for row in rows)
              for level in ("High", "Medium", "Low")}
    report = {
        "stage": STAGE,
        "run_id": run_dir.name,
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "findings": len(findings),
        "reasonings": len(rows),
        "confidence_counts": counts,
        "normalized_quote_matches": sum(row["checks"]["quote"] == "normalized"
                                        for row in rows),
        "second_source_unavailable": sum(row["checks"]["tie"] == "unavailable" for row in rows),
        "claim_support_unverified": sum(row["checks"]["claim_support"] == "unverified"
                                        for row in rows),
        "reasonings_judged": sum(row["judged"] for row in rows),
        "corpus_warnings": warnings,
        "reasoning_checks": list(rows),
    }
    _write_json(run_dir / "fact_check_report.json", report)
    log.info("fact checker: %d reasoning(s) -> High %d / Medium %d / Low %d",
             len(rows), counts["High"], counts["Medium"], counts["Low"])


def main(argv=None):
    """`python -m pharmer.fact_checker runs/<run_id>`."""
    logging.basicConfig(level=logging.INFO)
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: python -m pharmer.fact_checker <run_dir>")
        return 2
    asyncio.run(run(Path(args[0])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
