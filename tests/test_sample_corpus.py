"""Offline checks on the committed Novo sample corpus (data/sample/NVO, Contract 1).

Run: .venv/bin/python -m unittest tests.test_sample_corpus
"""
import json
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CORPUS = REPO_ROOT / "data" / "sample" / "NVO"

MANIFEST_KEYS = {"doc_id", "company", "ticker", "doc_type", "fiscal_period", "period_end",
                 "filed_date", "url", "accession", "source", "collected_date",
                 "application_no", "labeler_code"}
CHUNK_KEYS = {"chunk_id", "doc_id", "page", "section", "is_table", "text"}
FACT_KEYS = {"fact_id", "doc_id", "metric", "value", "unit", "currency", "fiscal_period",
             "period_end", "source_kind"}
DOC_TYPES = {"10-K", "10-Q", "20-F", "6-K", "transcript", "label", "orange_book",
             "purple_book", "xbrl", "ir_workbook", "rival_annual_report"}
SOURCE_KINDS = {"xbrl", "ir_workbook", "filing_table"}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
AMOUNT_RE = re.compile(r"^[(\-–]?\d[\d,.]*\)?\s?%?$")
YEAR_RE = re.compile(r"^(19|20)\d\d$")
NAV = "Financials Cash flow and capital allocation Outlook"
RECEIVABLES_Q2_2026 = "Trade receivables 88,949 70,856"


def _jsonl(name):
    with (CORPUS / name).open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh]


MANIFEST = json.loads((CORPUS / "manifest.json").read_text(encoding="utf-8"))
CHUNKS = _jsonl("chunks.jsonl")
FACTS = _jsonl("facts.jsonl")
DOCS = {m["doc_id"]: m for m in MANIFEST}


def _chunks(doc_id, page=None):
    return [c for c in CHUNKS if c["doc_id"] == doc_id and (page is None or c["page"] == page)]


def _fact(metric, period):
    [fact] = [f for f in FACTS if f["metric"] == metric and f["fiscal_period"] == period]
    return fact["value"]


def _comparative(doc_id, row_label):
    """Last number on the balance-sheet row `row_label` of a 6-K (the prior year-end column)."""
    rows = [line for c in _chunks(doc_id) if c["is_table"]
            for line in c["text"].split("\n") if line.startswith(row_label + " ")
            and c["section"].startswith("APPENDIX") and "BALANCE SHEET" in c["section"]]
    assert len(rows) == 1, rows
    return int(rows[0].split()[-1].replace(",", ""))


def _is_amount_line(line):
    return any(AMOUNT_RE.match(t) and not YEAR_RE.match(t) for t in line.split())


class ManifestTest(unittest.TestCase):
    def test_rows_have_contract_keys_and_enums(self):
        self.assertEqual([m["doc_id"] for m in MANIFEST],
                         ["NVO-6K-2026Q2", "NVO-6K-2025Q2", "NVO-XBRL"])
        for m in MANIFEST:
            self.assertEqual(set(m), MANIFEST_KEYS, m["doc_id"])
            self.assertIn(m["doc_type"], DOC_TYPES)
            self.assertIn(m["source"], {"live", "pre-collected"})
            if m["source"] == "pre-collected":
                self.assertRegex(m["collected_date"], DATE_RE)
            self.assertIsNone(m["application_no"])
            self.assertIsNone(m["labeler_code"])
            self.assertEqual(m["ticker"], "NVO")

    def test_six_k_provenance(self):
        expected = {"NVO-6K-2026Q2": ("2026-06-30", "2026-08-04", "Q2 2026"),
                    "NVO-6K-2025Q2": ("2025-06-30", "2025-08-06", "Q2 2025")}
        for doc_id, (period_end, filed, fiscal) in expected.items():
            m = DOCS[doc_id]
            self.assertEqual(m["doc_type"], "6-K")
            self.assertEqual((m["period_end"], m["filed_date"], m["fiscal_period"]),
                             (period_end, filed, fiscal))
            self.assertRegex(m["accession"], r"^\d{10}-\d{2}-\d{6}$")
            document = m["url"].rsplit("/", 1)[1]
            self.assertTrue(document.endswith(".htm"))
            self.assertEqual(m["url"], "https://www.sec.gov/Archives/edgar/data/353278/"
                             f"{m['accession'].replace('-', '')}/{document}")

    def test_xbrl_row(self):
        m = DOCS["NVO-XBRL"]
        self.assertEqual(m["doc_type"], "xbrl")
        self.assertEqual(m["fiscal_period"], "FY2025")
        self.assertIsNone(m["accession"])
        self.assertEqual(m["url"],
                         "https://data.sec.gov/api/xbrl/companyfacts/CIK0000353278.json")
        self.assertRegex(m["filed_date"], DATE_RE)
        self.assertGreaterEqual(m["filed_date"], m["period_end"])


class ChunksTest(unittest.TestCase):
    def test_ids_keys_and_doc_ids(self):
        ids = [c["chunk_id"] for c in CHUNKS]
        self.assertEqual(len(ids), len(set(ids)))
        for c in CHUNKS:
            self.assertEqual(set(c), CHUNK_KEYS)
            self.assertIn(c["doc_id"], DOCS)
            self.assertRegex(c["chunk_id"], rf"^{re.escape(c['doc_id'])}-p\d+-c\d{{2}}$")
            self.assertTrue(c["chunk_id"].startswith(f"{c['doc_id']}-p{c['page']}-c"))
            self.assertIsInstance(c["is_table"], bool)
            self.assertTrue(c["text"])

    def test_sections_never_empty(self):
        for c in CHUNKS:
            self.assertTrue(c["section"].strip(), c["chunk_id"])

    def test_no_nav_menu(self):
        for c in CHUNKS:
            self.assertNotIn(NAV, c["text"], c["chunk_id"])
            self.assertNotIn("Company announcement No", c["text"], c["chunk_id"])

    def test_receivables_table_chunk(self):
        hits = [c for c in _chunks("NVO-6K-2026Q2")
                if c["is_table"] and RECEIVABLES_Q2_2026 in c["text"]]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["section"], "APPENDIX 3: CONDENSED BALANCE SHEET")

    def test_page_14_cash_flow(self):
        page14 = _chunks("NVO-6K-2026Q2", 14)
        self.assertTrue(page14)
        for c in page14:
            self.assertEqual(c["section"], "CASH FLOW AND CAPITAL ALLOCATION")

    def test_pages_are_contiguous(self):
        # 37 printed pages + the SIGNATURES page (Q2 2026); 39 + SIGNATURES (Q2 2025).
        for doc_id, last in (("NVO-6K-2026Q2", 38), ("NVO-6K-2025Q2", 40)):
            pages = sorted({c["page"] for c in _chunks(doc_id)})
            self.assertEqual(pages, list(range(1, last + 1)), doc_id)

    def test_split_table_repeats_header(self):
        tables = [c for c in _chunks("NVO-6K-2026Q2", 30) if c["is_table"]]
        self.assertGreaterEqual(len(tables), 2)
        first = tables[0]["text"].split("\n")
        header = []
        for line in first[:3]:
            if _is_amount_line(line):
                break
            header.append(line)
        self.assertTrue(header)
        for c in tables[1:]:
            self.assertEqual(c["text"].split("\n")[:len(header)], header, c["chunk_id"])


class FactsTest(unittest.TestCase):
    def test_keys_and_ids(self):
        ids = [f["fact_id"] for f in FACTS]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(ids, sorted(ids))
        for f in FACTS:
            self.assertEqual(set(f), FACT_KEYS)
            self.assertIn(f["doc_id"], DOCS)
            self.assertIn(f["source_kind"], SOURCE_KINDS)
            self.assertEqual(f["fact_id"],
                             f"NVO-{f['source_kind']}-{f['metric']}-{f['fiscal_period']}")
            self.assertEqual((f["unit"], f["currency"]), ("millions", "DKK"))
            self.assertIsInstance(f["value"], int)
            self.assertRegex(f["period_end"], DATE_RE)

    def test_every_metric_and_year_present(self):
        metrics = ["revenue", "trade_receivables", "inventory", "operating_profit", "net_profit"]
        expected = {(m, p) for m in metrics for p in ("FY2023", "FY2024", "FY2025")}
        self.assertEqual({(f["metric"], f["fiscal_period"]) for f in FACTS}, expected)

    def test_receivables_tie_to_6k_comparatives(self):
        self.assertEqual(_fact("trade_receivables", "FY2024"), 71949)
        self.assertEqual(_fact("trade_receivables", "FY2025"), 70856)
        # Each half-year balance sheet's comparative column is the prior 31 December.
        self.assertEqual(_fact("trade_receivables", "FY2024"),
                         _comparative("NVO-6K-2025Q2", "Trade receivables"))
        self.assertEqual(_fact("trade_receivables", "FY2025"),
                         _comparative("NVO-6K-2026Q2", "Trade receivables"))


if __name__ == "__main__":
    unittest.main()
