"""Offline tests for pharmer/librarian.py - no network, no API calls.

The HTML fixtures are real, unedited page sections from real filings (Lilly's
Q2 2026 10-Q, Novo's Q2 2026 financial 6-K, and Novo's remuneration 6-K), kept
real because synthetic markup would not have exposed that the two filers print
their page numbers in different places.

Run: .venv/bin/python -m unittest tests.test_librarian
"""
import json
import logging
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from pharmer import librarian as L

FIXTURES = Path(__file__).parent / "fixtures"

# The stage logs its coverage warnings at WARNING by design; keep them out of
# the test output, where the assertions are what matter.
logging.getLogger("pharmer.librarian").setLevel(logging.ERROR)


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def submissions(rows):
    """The submissions API's parallel-array `filings.recent` shape."""
    keys = ["form", "reportDate", "filingDate", "accessionNumber",
            "primaryDocument", "size"]
    return {"filings": {"recent": {k: [r[k] for r in rows] for k in keys}}}


def filing(form, report_date, filing_date, accession, document, size=1_000_000):
    return {"form": form, "reportDate": report_date, "filingDate": filing_date,
            "accessionNumber": accession, "primaryDocument": document, "size": size}


class FakeSec:
    """Stands in for `_sec_get`: serves bytes from a url -> content map."""

    def __init__(self, pages):
        self.pages = pages
        self.requested = []

    def __call__(self, url, tries=3):
        self.requested.append(url)
        try:
            content = self.pages[url]
        except KeyError:
            raise urllib.error.HTTPError(url, 404, "not in fake", {}, None) from None
        return content if isinstance(content, bytes) else content.encode("utf-8")


class SecFetchTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {L.SEC_UA_VAR: "Tester t@example.com"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_missing_user_agent_fails_before_any_request(self):
        with mock.patch.dict(os.environ, {L.SEC_UA_VAR: ""}), \
             mock.patch("urllib.request.urlopen") as opener:
            with self.assertRaises(RuntimeError) as caught:
                L._sec_get("https://data.sec.gov/anything.json")
        self.assertIn(L.SEC_UA_VAR, str(caught.exception))
        opener.assert_not_called()

    def test_declared_user_agent_is_sent(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b"{}"
        with mock.patch("urllib.request.urlopen", return_value=response) as opener:
            L._sec_get("https://data.sec.gov/x.json")
        self.assertEqual(opener.call_args[0][0].headers["User-agent"],
                         "Tester t@example.com")

    def test_rate_limit_is_retried_then_raised(self):
        error = urllib.error.HTTPError("u", 429, "slow down", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=error) as opener, \
             mock.patch("time.sleep"):
            with self.assertRaises(urllib.error.HTTPError):
                L._sec_get("https://data.sec.gov/x.json", tries=3)
        self.assertEqual(opener.call_count, 3)

    def test_transient_error_then_success(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b"ok"
        error = urllib.error.HTTPError("u", 503, "unavailable", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=[error, response]), \
             mock.patch("time.sleep"):
            self.assertEqual(L._sec_get("https://data.sec.gov/x.json"), b"ok")


class PeriodTests(unittest.TestCase):
    def test_quarter_is_keyed_on_month_not_day(self):
        # Pfizer and J&J run 52/53-week quarters; a calendar-date match reported
        # their prior-year quarter as missing (docs/build-log.md).
        self.assertEqual(L._quarter("2026-06-28"), L._quarter("2025-06-29"))

    def test_fifty_two_week_prior_year_quarter_is_found(self):
        latest = filing("10-Q", "2026-06-28", "2026-08-05", "a-1", "pfe.htm")
        options = [filing("10-Q", "2026-03-29", "2026-05-01", "a-2", "x.htm"),
                   filing("10-Q", "2025-06-29", "2025-08-05", "a-3", "y.htm")]
        self.assertEqual(L._prior_year_match(latest, options)["accessionNumber"], "a-3")

    def test_prior_year_quarter_absent_returns_none(self):
        latest = filing("10-Q", "2026-06-30", "2026-08-05", "a-1", "x.htm")
        options = [filing("10-Q", "2026-03-31", "2026-04-30", "a-2", "y.htm")]
        self.assertIsNone(L._prior_year_match(latest, options))

    def test_period_labels(self):
        self.assertEqual(L._fiscal_period("10-K", "2025-12-31"), "FY2025")
        self.assertEqual(L._fiscal_period("10-Q", "2026-06-30"), "Q2 2026")
        self.assertEqual(L._fiscal_period("6-K", "2026-06-30", half_year=True), "H1 2026")
        self.assertEqual(L._doc_id("NVO", "6-K", "2026-06-30"), "NVO-6K-2026Q2")
        self.assertEqual(L._doc_id("NVO", "20-F", "2025-12-31"), "NVO-20F-FY2025")


class ChunkingTests(unittest.TestCase):
    def test_printed_page_read_from_page_footer(self):
        # Lilly's 10-Q prints a bare page number at the foot of each page.
        _, provenance = L.chunk_document("LLY-10Q-2026Q2", fixture("lly_10q_pages.htm"))
        self.assertEqual(provenance["verified_pages"], [5, 7])

    def test_printed_page_read_from_running_header(self):
        # Novo's 6-K prints "Page 3 of 37" in a running header instead.
        _, provenance = L.chunk_document("NVO-6K-2026Q2", fixture("nvo_6k_financial.htm"))
        self.assertEqual(provenance["verified_pages"], [3, 12])

    def test_unnumbered_page_falls_back_to_ordinal_and_is_flagged(self):
        chunks, provenance = L.chunk_document("LLY-10Q-2026Q2",
                                              fixture("lly_10q_pages.htm"))
        self.assertEqual(provenance["inferred_pages"], [1])
        self.assertNotIn(1, provenance["verified_pages"])
        self.assertTrue(any(c["page"] == 1 for c in chunks))

    def test_chunk_id_encodes_doc_and_page(self):
        chunks, _ = L.chunk_document("LLY-10Q-2026Q2", fixture("lly_10q_pages.htm"))
        for chunk in chunks:
            self.assertTrue(chunk["chunk_id"].startswith(f"{chunk['doc_id']}-p{chunk['page']}-c"))

    def test_chunk_ids_are_unique_and_stable_across_runs(self):
        first, _ = L.chunk_document("LLY-10Q-2026Q2", fixture("lly_10q_pages.htm"))
        second, _ = L.chunk_document("LLY-10Q-2026Q2", fixture("lly_10q_pages.htm"))
        ids = [c["chunk_id"] for c in first]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual({c["chunk_id"]: c["text"] for c in first},
                         {c["chunk_id"]: c["text"] for c in second})

    def test_table_chunk_keeps_rows_columns_periods_and_units(self):
        chunks, _ = L.chunk_document("NVO-6K-2026Q2", fixture("nvo_6k_financial.htm"))
        tables = [c for c in chunks if c["is_table"]]
        self.assertTrue(tables)
        profit_and_loss = next(c for c in tables if "PROFIT AND LOSS" in c["text"])
        header, *rows = profit_and_loss["text"].splitlines()
        self.assertIn("DKK million", header)            # units survive
        self.assertIn("Q2 2026", header)                # reporting periods survive
        self.assertIn("H1 2026", header)
        net_sales = next(r for r in rows if r.startswith("Net sales"))
        self.assertIn("\t", net_sales)                  # row/column association survives
        self.assertEqual(len(net_sales.split("\t")[0]), len("Net sales"))

    def test_parenthesised_negative_is_preserved_verbatim(self):
        # The Fact Checker string-matches hunter quotes against `text`, so a
        # negative must never be normalised from (1,234) to -1234.
        chunks, _ = L.chunk_document("LLY-10Q-2026Q2", fixture("lly_10q_pages.htm"))
        corpus = "\n".join(c["text"] for c in chunks)
        self.assertRegex(corpus, r"\(\d[\d,]*")
        self.assertNotRegex(corpus, r"-\d{1,3},\d{3}\)")

    def test_long_table_splits_with_headers_repeated(self):
        rows = "".join(f"<tr><td>Row {i}</td><td>{i:,}00</td><td>({i}00)</td></tr>"
                       for i in range(400))
        html = ("<html><body><table>"
                "<tr><th>Metric</th><th>Q2 2026 DKK million</th><th>Q2 2025</th></tr>"
                f"{rows}</table></body></html>")
        chunks, _ = L.chunk_document("TST-10Q-2026Q2", html)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(c["is_table"] for c in chunks))
        for chunk in chunks:
            self.assertTrue(chunk["text"].startswith("Metric\tQ2 2026 DKK million\tQ2 2025"))

    def test_unpaginated_document_is_divided_into_synthetic_pages(self):
        # Novo's FY2025 annual-report exhibit has no page-break markup at all;
        # without subdivision its whole 10.6MB would cite as "p.1".
        body = "".join(f"<p>{'paragraph %d. ' % i * 30}</p>" for i in range(200))
        chunks, provenance = L.chunk_document("NVO-20F-FY2025", f"<html><body>{body}</body></html>")
        self.assertEqual(provenance["verified_pages"], [])
        self.assertGreater(len(provenance["inferred_pages"]), 10)
        self.assertEqual(len({c["page"] for c in chunks}),
                         len(provenance["inferred_pages"]))

    def test_short_unnumbered_page_is_not_subdivided(self):
        chunks, provenance = L.chunk_document("TST-10Q-2026Q2",
                                              "<html><body><p>short cover page</p></body></html>")
        self.assertEqual(provenance["inferred_pages"], [1])
        self.assertEqual({c["page"] for c in chunks}, {1})

    def test_section_tracks_item_headings(self):
        html = ("<html><body><p>Item 1A. Risk Factors</p><p>" + "risk text. " * 40
                + "</p></body></html>")
        chunks, _ = L.chunk_document("TST-10K-FY2025", html)
        self.assertEqual(chunks[0]["section"], "Item 1A. Risk Factors")


class SixKSelectionTests(unittest.TestCase):
    def test_financial_report_scores_above_threshold(self):
        score, _ = L._score_financial_6k(L._plain_text(fixture("nvo_6k_financial.htm")))
        self.assertGreaterEqual(score, L.FINANCIAL_6K_THRESHOLD)

    def test_remuneration_report_is_rejected(self):
        # 4,389KB of noise, larger than the 2,784KB financial report it would
        # otherwise displace - size alone cannot make this call.
        score, _ = L._score_financial_6k(L._plain_text(fixture("nvo_6k_noise.htm")))
        self.assertLess(score, L.FINANCIAL_6K_THRESHOLD)

    def test_half_year_filer_is_labelled_h1(self):
        self.assertTrue(L._is_half_year("Results for the six months ended 30 June 2026"))
        self.assertFalse(L._is_half_year("Results for the three months ended 30 June"))


def companyfacts(concepts):
    """A companyfacts payload: {taxonomy: {tag: {units: {unit: [rows]}}}}."""
    return {"facts": dict(concepts, dei={"EntityCommonStockSharesOutstanding": {}})}


def duration(start, end, val, filed="2026-08-05", form="10-Q"):
    return {"start": start, "end": end, "val": val, "filed": filed, "form": form}


def instant(end, val, filed="2026-08-05", form="10-Q"):
    return {"end": end, "val": val, "filed": filed, "form": form}


class XbrlFactTests(unittest.TestCase):
    """Contract 1c facts from XBRL companyfacts."""

    def facts_for(self, ticker, concepts):
        cik = L.CIKS[ticker][0]
        url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
        fake = FakeSec({url: json.dumps(companyfacts(concepts))})
        with mock.patch.object(L, "_sec_get", fake):
            return L._xbrl_facts(ticker)

    def test_value_is_normalised_to_millions(self):
        # 22,974,000,000 must emit as 22974 - the figure the filing table prints,
        # which is the whole point of the number-tie.
        facts, _, _ = self.facts_for("LLY", {"us-gaap": {"Revenues": {"units": {"USD": [
            duration("2026-04-01", "2026-06-30", 22_974_000_000)]}}}})
        [fact] = facts
        self.assertEqual(fact["value"], 22974)
        self.assertEqual(fact["unit"], "millions")
        self.assertEqual(fact["currency"], "USD")
        self.assertEqual(fact["source_kind"], "xbrl")
        self.assertEqual(fact["doc_id"], "LLY-XBRL")
        self.assertEqual(fact["fact_id"], "LLY-xbrl-revenue-2026Q2")

    def test_three_and_six_month_rows_become_separate_periods(self):
        facts, _, _ = self.facts_for("LLY", {"us-gaap": {"Revenues": {"units": {"USD": [
            duration("2026-04-01", "2026-06-30", 22_974_000_000),
            duration("2026-01-01", "2026-06-30", 42_773_000_000)]}}}})
        got = {f["fiscal_period"]: f["value"] for f in facts}
        self.assertEqual(got, {"Q2 2026": 22974, "H1 2026": 42773})

    def test_filing_fy_fp_fields_are_ignored(self):
        # The six-month row carries fp "Q2"; trusting it would file the H1 figure
        # under Q2 and tie two different numbers together.
        row = duration("2026-01-01", "2026-06-30", 42_773_000_000)
        row.update(fy=2026, fp="Q2")
        facts, _, _ = self.facts_for("LLY", {"us-gaap": {"Revenues": {"units": {"USD": [row]}}}})
        self.assertEqual(facts[0]["fiscal_period"], "H1 2026")

    def test_instant_becomes_quarter_or_fiscal_year(self):
        facts, _, _ = self.facts_for("LLY", {"us-gaap": {"AccountsReceivableNetCurrent":
            {"units": {"USD": [instant("2026-06-30", 20_083_000_000),
                               instant("2025-12-31", 14_000_000_000, form="10-K")]}}}})
        self.assertEqual({f["fiscal_period"] for f in facts}, {"Q2 2026", "FY2025"})

    def test_fifty_two_week_year_end_in_december_is_a_fiscal_year(self):
        # J&J's fiscal year ends 2025-12-28.
        self.assertEqual(L._period_from_fact(instant("2025-12-28", 1)), "FY2025")

    def test_fifty_two_week_year_end_spilling_into_january(self):
        self.assertEqual(L._period_from_fact(instant("2027-01-02", 1)), "FY2026")

    def test_unlabelable_span_is_skipped_and_counted(self):
        facts, _, skipped = self.facts_for("LLY", {"us-gaap": {"Revenues": {"units": {"USD": [
            duration("2026-01-01", "2026-02-15", 1_000_000),       # 45 days
            duration("2025-10-01", "2026-06-30", 9_000_000)]}}}})  # 272 days
        self.assertEqual(facts, [])
        self.assertEqual(skipped, 2)

    def test_non_currency_units_are_skipped(self):
        facts, _, skipped = self.facts_for("LLY", {"us-gaap": {"Revenues": {"units": {
            "USD/shares": [duration("2026-04-01", "2026-06-30", 5)],
            "shares": [instant("2026-06-30", 900_000_000)]}}}})
        self.assertEqual(facts, [])
        self.assertEqual(skipped, 2)

    def test_restatement_keeps_the_latest_filed_row(self):
        facts, _, _ = self.facts_for("LLY", {"us-gaap": {"AccountsReceivableNetCurrent":
            {"units": {"USD": [instant("2026-06-30", 20_000_000_000, filed="2026-08-05"),
                               instant("2026-06-30", 20_500_000_000, filed="2026-11-04")]}}}})
        [fact] = facts
        self.assertEqual(fact["value"], 20500)

    def test_unmapped_metric_is_reported_not_guessed(self):
        # Lilly tags no operating income at all.
        facts, misses, _ = self.facts_for("LLY", {"us-gaap": {"Revenues": {"units": {"USD": [
            duration("2026-04-01", "2026-06-30", 22_974_000_000)]}}}})
        missed = {m["metric"] for m in misses}
        self.assertIn("operating_profit", missed)
        self.assertNotIn("operating_profit", {f["metric"] for f in facts})
        self.assertTrue(all(m["candidates_tried"] for m in misses))

    def test_ifrs_tags_and_foreign_currency_resolve(self):
        facts, _, _ = self.facts_for("NVO", {"ifrs-full": {
            "CurrentTradeReceivables": {"units": {"DKK": [
                instant("2024-12-31", 71_949_000_000, form="20-F")]}},
            "ProfitLossFromOperatingActivities": {"units": {"DKK": [
                duration("2024-01-01", "2024-12-31", 128_000_000_000, form="20-F")]}}}})
        got = {f["metric"]: (f["value"], f["currency"], f["fiscal_period"]) for f in facts}
        self.assertEqual(got["trade_receivables"], (71949, "DKK", "FY2024"))
        self.assertEqual(got["operating_profit"][2], "FY2024")

    def test_preferred_tag_wins_when_a_filer_reports_several(self):
        facts, _, _ = self.facts_for("LLY", {"us-gaap": {
            "Revenues": {"units": {"USD": [duration("2026-04-01", "2026-06-30", 1_000_000)]}},
            "RevenueFromContractWithCustomerExcludingAssessedTax":
                {"units": {"USD": [duration("2026-04-01", "2026-06-30", 2_000_000)]}}}})
        revenue = [f for f in facts if f["metric"] == "revenue"]
        self.assertEqual([f["value"] for f in revenue], [1])  # Revenues is first in the map

    def test_one_currency_per_metric_keeps_fact_ids_unique(self):
        facts, _, skipped = self.facts_for("AZN", {"ifrs-full": {"Revenue": {"units": {
            "USD": [duration("2026-01-01", "2026-06-30", 1_000_000, form="6-K"),
                    duration("2025-01-01", "2025-06-30", 2_000_000, form="6-K")],
            "GBP": [duration("2026-01-01", "2026-06-30", 3_000_000, form="6-K")]}}}})
        ids = [f["fact_id"] for f in facts]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual({f["currency"] for f in facts}, {"USD"})
        self.assertEqual(skipped, 1)

    def test_concept_map_covers_the_keys_partner_b_already_uses(self):
        for metric in ("revenue", "trade_receivables", "inventory",
                       "operating_profit", "net_profit"):
            self.assertIn(metric, L.CONCEPT_MAP)


class ShareConceptTests(unittest.TestCase):
    """Diluted shares and EPS - the denominator Quant needs for `eps_pct`.

    Real tag names and real figures: Lilly tags both quarterly, Novo tags neither
    sub-annually and calls its diluted count `AdjustedWeightedAverageShares`.
    """

    facts_for = XbrlFactTests.facts_for

    def test_unit_fields_decides_scale_and_currency_per_kind(self):
        # A money amount scales to millions and keeps its currency.
        self.assertEqual(L._unit_fields("currency", "USD"), (L.FACT_SCALE, "millions", "USD"))
        # A share count scales to millions of shares and has no currency.
        self.assertEqual(L._unit_fields("shares", "shares"), (L.FACT_SCALE, "millions", None))
        # A per-share amount is carried through: dividing EPS by a million destroys it.
        self.assertEqual(L._unit_fields("per_share", "USD/shares"), (1, "per_share", "USD"))
        self.assertEqual(L._unit_fields("per_share", "DKK/shares"), (1, "per_share", "DKK"))
        # A unit that cannot express the metric is refused, so its rows are skipped.
        self.assertIsNone(L._unit_fields("currency", "shares"))
        self.assertIsNone(L._unit_fields("shares", "USD"))
        self.assertIsNone(L._unit_fields("per_share", "USD"))
        self.assertIsNone(L._unit_fields("currency", "pure"))

    def test_us_gaap_diluted_shares_and_eps(self):
        facts, _misses, _skipped = self.facts_for("LLY", {"us-gaap": {
            "WeightedAverageNumberOfDilutedSharesOutstanding": {"units": {"shares": [
                duration("2026-04-01", "2026-06-30", 893_700_000)]}},
            "EarningsPerShareDiluted": {"units": {"USD/shares": [
                duration("2026-04-01", "2026-06-30", 7.94)]}}}})
        by_metric = {f["metric"]: f for f in facts}
        shares, eps = by_metric["diluted_shares"], by_metric["diluted_eps"]
        self.assertEqual((shares["value"], shares["unit"], shares["currency"]),
                         (893.7, "millions", None))
        self.assertEqual((eps["value"], eps["unit"], eps["currency"]),
                         (7.94, "per_share", "USD"))
        self.assertEqual(shares["fiscal_period"], "Q2 2026")
        self.assertEqual(eps["fact_id"], "LLY-xbrl-diluted_eps-2026Q2")

    def test_ifrs_diluted_shares_and_eps(self):
        # Novo's diluted count is AdjustedWeightedAverageShares, in DKK/shares for EPS.
        facts, _misses, _skipped = self.facts_for("NVO", {"ifrs-full": {
            "AdjustedWeightedAverageShares": {"units": {"shares": [
                duration("2025-01-01", "2025-12-31", 4_447_700_000)]}},
            "DilutedEarningsLossPerShare": {"units": {"DKK/shares": [
                duration("2025-01-01", "2025-12-31", 23.03)]}}}})
        by_metric = {f["metric"]: f for f in facts}
        self.assertEqual(by_metric["diluted_shares"]["value"], 4447.7)
        self.assertEqual(by_metric["diluted_eps"]["value"], 23.03)
        self.assertEqual(by_metric["diluted_eps"]["currency"], "DKK")
        for fact in by_metric.values():
            self.assertEqual(fact["fiscal_period"], "FY2025")

    def test_the_basic_share_count_is_not_used_as_the_diluted_one(self):
        # WeightedAverageShares is Novo's *basic* count; standing it in for the diluted
        # count would overstate EPS impact on every finding.
        facts, misses, _skipped = self.facts_for("NVO", {"ifrs-full": {
            "WeightedAverageShares": {"units": {"shares": [
                duration("2025-01-01", "2025-12-31", 4_463_000_000)]}}}})
        self.assertEqual([f for f in facts if f["metric"] == "diluted_shares"], [])
        self.assertIn("diluted_shares", [m["metric"] for m in misses])

    def test_an_annual_only_filer_emits_no_quarterly_denominator(self):
        # The coverage gap Quant must report instead of substituting an annual count.
        facts, _misses, _skipped = self.facts_for("NVO", {"ifrs-full": {
            "AdjustedWeightedAverageShares": {"units": {"shares": [
                duration("2024-01-01", "2024-12-31", 4_463_000_000),
                duration("2025-01-01", "2025-12-31", 4_447_700_000)]}}}})
        periods = {f["fiscal_period"] for f in facts if f["metric"] == "diluted_shares"}
        self.assertEqual(periods, {"FY2024", "FY2025"})
        self.assertFalse([p for p in periods if p.startswith(("Q", "H"))])

    def test_a_money_metric_still_ignores_share_units(self):
        # Adding the share kinds must not let `shares` rows into a currency metric.
        facts, _misses, skipped = self.facts_for("LLY", {"us-gaap": {"Revenues": {"units": {
            "USD": [duration("2026-04-01", "2026-06-30", 22_974_000_000)],
            "shares": [duration("2026-04-01", "2026-06-30", 893_700_000)]}}}})
        revenue = [f for f in facts if f["metric"] == "revenue"]
        self.assertEqual([f["value"] for f in revenue], [22974.0])
        self.assertEqual(skipped, 1)

    def test_every_share_fact_keeps_its_provenance(self):
        facts, _misses, _skipped = self.facts_for("LLY", {"us-gaap": {
            "EarningsPerShareDiluted": {"units": {"USD/shares": [
                duration("2026-04-01", "2026-06-30", 7.94)]}}}})
        eps = next(f for f in facts if f["metric"] == "diluted_eps")
        self.assertEqual(eps["doc_id"], "LLY-XBRL")
        self.assertEqual(eps["source_kind"], "xbrl")
        self.assertEqual(eps["period_end"], "2026-06-30")


class CorpusTests(unittest.TestCase):
    """End-to-end `build_corpus` runs against a fake EDGAR."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.run_dir = Path(self.tmp.name) / "LLY-20261017-1200"

    def read(self, name):
        path = self.run_dir / name
        if name.endswith(".jsonl"):
            return [json.loads(line) for line in path.read_text().splitlines() if line]
        return json.loads(path.read_text())

    def domestic_sec(self):
        cik = L.CIKS["LLY"][0]
        rows = [
            filing("10-Q", "2026-06-30", "2026-08-05", "0000059478-26-000081", "q2.htm"),
            filing("10-Q", "2026-03-31", "2026-04-30", "0000059478-26-000045", "q1.htm"),
            filing("10-Q", "2025-06-30", "2025-08-07", "0000059478-25-000150", "pq2.htm"),
            filing("10-K", "2025-12-31", "2026-02-12", "0000059478-26-000010", "fy25.htm"),
            filing("10-K", "2024-12-31", "2025-02-19", "0000059478-25-000020", "fy24.htm"),
        ]
        pages = {f"https://data.sec.gov/submissions/CIK{cik:010d}.json":
                 json.dumps(submissions(rows))}
        for row in rows:
            pages[L._archive_url(cik, row["accessionNumber"], row["primaryDocument"])] = \
                fixture("lly_10q_pages.htm")
        pages[f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"] = \
            json.dumps(companyfacts({"us-gaap": {
                "Revenues": {"units": {"USD": [
                    duration("2026-04-01", "2026-06-30", 22_974_000_000),
                    duration("2026-01-01", "2026-06-30", 42_773_000_000)]}},
                "AccountsReceivableNetCurrent": {"units": {"USD": [
                    instant("2026-06-30", 20_083_000_000)]}},
                "InventoryNet": {"units": {"USD": [instant("2026-06-30", 9_000_000_000)]}},
                "NetIncomeLoss": {"units": {"USD": [
                    duration("2026-04-01", "2026-06-30", 6_000_000_000)]}}}}))
        return FakeSec(pages), rows

    def test_domestic_corpus_has_two_annuals_and_both_quarters(self):
        fake, _ = self.domestic_sec()
        with mock.patch.object(L, "_sec_get", fake):
            L.build_corpus("LLY", self.run_dir)
        manifest = self.read("manifest.json")
        self.assertEqual([e["doc_id"] for e in manifest],
                         ["LLY-10K-FY2025", "LLY-10K-FY2024",
                          "LLY-10Q-2026Q2", "LLY-10Q-2025Q2", "LLY-XBRL"])
        for entry in [e for e in manifest if e["doc_type"] != "xbrl"]:
            self.assertEqual(entry["ticker"], "LLY")
            self.assertEqual(entry["company"], "Eli Lilly")
            self.assertEqual(entry["source"], "live")
            self.assertIsNone(entry["collected_date"])
            self.assertIsNone(entry["application_no"])
            self.assertIsNone(entry["labeler_code"])
            self.assertTrue(entry["url"].startswith("https://www.sec.gov/Archives/"))
            self.assertTrue(entry["accession"])

    def test_facts_are_written_and_tie_to_the_filing_table_figure(self):
        fake, _ = self.domestic_sec()
        with mock.patch.object(L, "_sec_get", fake):
            report = L.build_corpus("LLY", self.run_dir)
        facts = self.read("facts.jsonl")
        self.assertTrue(facts)
        revenue = {f["fiscal_period"]: f["value"] for f in facts if f["metric"] == "revenue"}
        self.assertEqual(revenue, {"Q2 2026": 22974, "H1 2026": 42773})
        self.assertTrue(all(f["source_kind"] == "xbrl" and f["unit"] == "millions"
                            for f in facts))
        self.assertEqual(len({f["fact_id"] for f in facts}), len(facts))
        self.assertEqual(report["facts"]["total"], len(facts))

    def test_unmapped_metrics_are_warned_and_counted(self):
        fake, _ = self.domestic_sec()
        with mock.patch.object(L, "_sec_get", fake):
            report = L.build_corpus("LLY", self.run_dir)
        missed = {m["metric"] for m in report["facts"]["unmapped_metrics"]}
        self.assertIn("operating_profit", missed)
        self.assertTrue(any("unmapped_metric operating_profit" in w
                            for w in report["coverage_warnings"]))

    def test_missing_xbrl_leaves_facts_empty_without_failing_the_run(self):
        cik = L.CIKS["LLY"][0]
        fake, _ = self.domestic_sec()
        del fake.pages[f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"]
        with mock.patch.object(L, "_sec_get", fake):
            report = L.build_corpus("LLY", self.run_dir)
        self.assertEqual((self.run_dir / "facts.jsonl").read_text(), "")
        self.assertTrue(self.read("chunks.jsonl"))          # filings corpus survives
        self.assertNotIn("LLY-XBRL", [e["doc_id"] for e in self.read("manifest.json")])
        self.assertTrue(any("XBRL companyfacts unavailable" in w
                            for w in report["coverage_warnings"]))
        self.assertTrue(any("facts.jsonl is empty" in w
                            for w in report["coverage_warnings"]))

    def test_sidecar_records_page_provenance(self):
        fake, _ = self.domestic_sec()
        with mock.patch.object(L, "_sec_get", fake):
            L.build_corpus("LLY", self.run_dir)
        pages = self.read("librarian_report.json")["page_attribution"]
        entry = pages["LLY-10Q-2026Q2"]
        self.assertEqual(entry["verified_printed_pages"], 2)
        self.assertEqual(entry["inferred_page_positions"], 1)
        self.assertEqual(entry["inferred_pages"], [1])

    def test_document_without_printed_pages_warns_against_page_footnotes(self):
        cik = L.CIKS["LLY"][0]
        rows = [filing("10-K", "2025-12-31", "2026-02-12", "a-1", "fy25.htm")]
        body = "".join(f"<p>{'unpaginated text %d. ' % i * 30}</p>" for i in range(200))
        pages = {f"https://data.sec.gov/submissions/CIK{cik:010d}.json":
                 json.dumps(submissions(rows)),
                 L._archive_url(cik, "a-1", "fy25.htm"): f"<html><body>{body}</body></html>"}
        with mock.patch.object(L, "_sec_get", FakeSec(pages)):
            report = L.build_corpus("LLY", self.run_dir)
        self.assertEqual(
            self.read("librarian_report.json")["page_attribution"]["LLY-10K-FY2025"]
            ["attribution"], "inferred-only")
        self.assertTrue(any("must cite the section, not a printed page" in w
                            for w in report["coverage_warnings"]))

    def test_paginated_document_is_reported_as_mixed_or_printed(self):
        fake, _ = self.domestic_sec()
        with mock.patch.object(L, "_sec_get", fake):
            L.build_corpus("LLY", self.run_dir)
        attribution = self.read("librarian_report.json")["page_attribution"]
        self.assertEqual(attribution["LLY-10Q-2026Q2"]["attribution"], "mixed")

    def test_missing_prior_year_quarter_warns_and_continues(self):
        cik = L.CIKS["LLY"][0]
        rows = [
            filing("10-Q", "2026-06-30", "2026-08-05", "a-1", "q2.htm"),
            filing("10-K", "2025-12-31", "2026-02-12", "a-2", "fy25.htm"),
            filing("10-K", "2024-12-31", "2025-02-19", "a-3", "fy24.htm"),
        ]
        pages = {f"https://data.sec.gov/submissions/CIK{cik:010d}.json":
                 json.dumps(submissions(rows))}
        for row in rows:
            pages[L._archive_url(cik, row["accessionNumber"], row["primaryDocument"])] = \
                fixture("lly_10q_pages.htm")
        with mock.patch.object(L, "_sec_get", FakeSec(pages)):
            report = L.build_corpus("LLY", self.run_dir)
        self.assertEqual(len(self.read("manifest.json")), 3)
        self.assertTrue(any("no prior-year same-quarter 10-Q" in w
                            for w in report["coverage_warnings"]))

    # ---- foreign filer ----

    def foreign_sec(self, sixk_rows=None):
        cik = L.CIKS["NVO"][0]
        sixk_rows = sixk_rows if sixk_rows is not None else [
            filing("6-K", "2026-06-30", "2026-08-04", "0000353278-26-000023",
                   "caq22026.htm", size=2_784_000),
            filing("6-K", "2025-12-31", "2026-02-04", "0000353278-26-000009",
                   "novonordisk_remuneration.htm", size=4_389_000),
            filing("6-K", "2025-06-30", "2025-08-06", "0001628280-25-000100",
                   "caq22025.htm", size=2_910_000),
        ]
        annuals = [
            filing("20-F", "2025-12-31", "2026-02-05", "0000353278-26-000012",
                   "nvo-20251231.htm"),
            filing("20-F", "2024-12-31", "2025-02-05", "0001628280-25-003920",
                   "nvo-20241231.htm"),
        ]
        small_noise = [filing("6-K", "2026-10-13", "2026-10-13", "n-1",
                              "f6k_101226.htm", size=163_000)]
        rows = sixk_rows + annuals + small_noise
        pages = {f"https://data.sec.gov/submissions/CIK{cik:010d}.json":
                 json.dumps(submissions(rows))}
        for row in annuals:
            accession = row["accessionNumber"].replace("-", "")
            pages[f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/index.json"] = \
                json.dumps({"directory": {"item": [
                    {"name": row["primaryDocument"], "size": "900000"},
                    {"name": row["primaryDocument"].replace(".htm", "_d2.htm"),
                     "size": "10592000"},
                    {"name": "logo.jpg", "size": "40845000"},
                ]}})
            pages[L._archive_url(cik, row["accessionNumber"], row["primaryDocument"])] = \
                fixture("nvo_6k_noise.htm")       # the 20-F wrapper, no financials
            pages[L._archive_url(cik, row["accessionNumber"],
                                 row["primaryDocument"].replace(".htm", "_d2.htm"))] = \
                fixture("nvo_6k_financial.htm")   # the incorporated annual report
        for row in sixk_rows + small_noise:
            body = ("nvo_6k_noise.htm" if "remuneration" in row["primaryDocument"]
                    or row["primaryDocument"].startswith("f6k")
                    else "nvo_6k_financial.htm")
            pages[L._archive_url(cik, row["accessionNumber"], row["primaryDocument"])] = \
                fixture(body)
        # Novo's companyfacts are ifrs-full, DKK, and annual only - there is no
        # quarterly receivables fact to tie a 6-K figure against.
        pages[f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"] = \
            json.dumps(companyfacts({"ifrs-full": {
                "CurrentTradeReceivables": {"units": {"DKK": [
                    instant("2024-12-31", 71_949_000_000, form="20-F"),
                    instant("2025-12-31", 70_856_000_000, form="20-F")]}},
                "Revenue": {"units": {"DKK": [
                    duration("2025-01-01", "2025-12-31", 290_000_000_000, form="20-F")]}}}}))
        return FakeSec(pages)

    def test_foreign_corpus_follows_exhibit_and_picks_financial_6ks(self):
        fake = self.foreign_sec()
        run_dir = Path(self.tmp.name) / "NVO-20261017-1200"
        with mock.patch.object(L, "_sec_get", fake):
            L.build_corpus("NVO", run_dir)
        manifest = json.loads((run_dir / "manifest.json").read_text())
        self.assertEqual([e["doc_id"] for e in manifest if e["doc_type"] != "xbrl"],
                         ["NVO-20F-FY2025", "NVO-20F-FY2024",
                          "NVO-6K-2026Q2", "NVO-6K-2025Q2"])
        # the annual's url is the incorporated-by-reference exhibit, not the wrapper
        self.assertTrue(manifest[0]["url"].endswith("nvo-20251231_d2.htm"))
        self.assertEqual(manifest[2]["fiscal_period"], "Q2 2026")
        self.assertEqual(manifest[2]["doc_type"], "6-K")

    def test_remuneration_6k_is_recorded_as_rejected_not_selected(self):
        fake = self.foreign_sec()
        run_dir = Path(self.tmp.name) / "NVO-20261017-1200"
        with mock.patch.object(L, "_sec_get", fake):
            L.build_corpus("NVO", run_dir)
        report = json.loads((run_dir / "librarian_report.json").read_text())
        candidates = {c["document"]: c for c in report["selection"]["sixk_candidates"]}
        self.assertFalse(candidates["novonordisk_remuneration.htm"]["selected"])
        self.assertIn("below threshold",
                      candidates["novonordisk_remuneration.htm"]["reason"])
        self.assertTrue(candidates["caq22026.htm"]["selected"])
        self.assertNotIn("novonordisk_remuneration.htm", report["selection"]["sixk_selected"])

    def test_small_6k_is_never_fetched(self):
        fake = self.foreign_sec()
        run_dir = Path(self.tmp.name) / "NVO-20261017-1200"
        with mock.patch.object(L, "_sec_get", fake):
            L.build_corpus("NVO", run_dir)
        self.assertFalse(any("f6k_101226.htm" in url for url in fake.requested))

    def test_no_financial_6k_warns_and_leaves_no_quarterly(self):
        rows = [filing("6-K", "2026-02-04", "2026-02-04", "0000353278-26-000009",
                       "novonordisk_remuneration.htm", size=4_389_000)]
        fake = self.foreign_sec(sixk_rows=rows)
        run_dir = Path(self.tmp.name) / "NVO-20261017-1200"
        with mock.patch.object(L, "_sec_get", fake):
            report = L.build_corpus("NVO", run_dir)
        manifest = json.loads((run_dir / "manifest.json").read_text())
        self.assertEqual([e["doc_type"] for e in manifest if e["doc_type"] != "xbrl"],
                         ["20-F", "20-F"])
        self.assertTrue(any("no financial 6-K passed content scoring" in w
                            for w in report["coverage_warnings"]))

    def test_two_reports_for_one_period_raise_an_ambiguity_warning(self):
        rows = [
            filing("6-K", "2026-06-30", "2026-08-04", "a-1", "caq22026.htm",
                   size=2_784_000),
            filing("6-K", "2026-06-30", "2026-08-03", "a-2", "caq22026_v2.htm",
                   size=2_700_000),
        ]
        fake = self.foreign_sec(sixk_rows=rows)
        run_dir = Path(self.tmp.name) / "NVO-20261017-1200"
        with mock.patch.object(L, "_sec_get", fake):
            report = L.build_corpus("NVO", run_dir)
        self.assertTrue(any("both read as the Q2 2026 financial report" in w
                            for w in report["coverage_warnings"]))

    def test_foreign_filer_gets_annual_facts_only_and_is_warned(self):
        fake = self.foreign_sec()
        run_dir = Path(self.tmp.name) / "NVO-20261018-1200"
        with mock.patch.object(L, "_sec_get", fake):
            report = L.build_corpus("NVO", run_dir)
        facts = [json.loads(l) for l in
                 (run_dir / "facts.jsonl").read_text().splitlines() if l]
        self.assertTrue(facts)
        self.assertTrue(all(f["fiscal_period"].startswith("FY") for f in facts))
        self.assertEqual({f["currency"] for f in facts}, {"DKK"})
        receivables = {f["fiscal_period"]: f["value"] for f in facts
                       if f["metric"] == "trade_receivables"}
        self.assertEqual(receivables["FY2024"], 71949)
        self.assertTrue(any("annual facts only" in w for w in report["coverage_warnings"]))

    def test_unknown_ticker_fails_before_fetching(self):
        with mock.patch.object(L, "_sec_get") as fetch:
            with self.assertRaises(ValueError) as caught:
                L.build_corpus("ZZZZ", self.run_dir)
        fetch.assert_not_called()
        self.assertIn("ZZZZ", str(caught.exception))


class PartnerBCompatibilityTests(unittest.TestCase):
    """A minimal Contract 1 reader, standing in for Partner B's hunters.

    This is the operation the whole closed-book rule rests on: resolve a cited
    chunk_id back to a document and page, and confirm the quote is a verbatim
    substring of that chunk's text.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.run_dir = Path(self.tmp.name) / "LLY-20261017-1200"
        cik = L.CIKS["LLY"][0]
        rows = [
            filing("10-Q", "2026-06-30", "2026-08-05", "a-1", "q2.htm"),
            filing("10-Q", "2025-06-30", "2025-08-07", "a-2", "pq2.htm"),
            filing("10-K", "2025-12-31", "2026-02-12", "a-3", "fy25.htm"),
            filing("10-K", "2024-12-31", "2025-02-19", "a-4", "fy24.htm"),
        ]
        pages = {f"https://data.sec.gov/submissions/CIK{cik:010d}.json":
                 json.dumps(submissions(rows))}
        for row in rows:
            pages[L._archive_url(cik, row["accessionNumber"], row["primaryDocument"])] = \
                fixture("lly_10q_pages.htm")
        pages[f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"] = \
            json.dumps(companyfacts({"us-gaap": {
                "Revenues": {"units": {"USD": [
                    duration("2026-04-01", "2026-06-30", 22_974_000_000)]}},
                "AccountsReceivableNetCurrent": {"units": {"USD": [
                    instant("2026-06-30", 20_083_000_000)]}},
                "InventoryNet": {"units": {"USD": [instant("2026-06-30", 9_000_000_000)]}},
                "NetIncomeLoss": {"units": {"USD": [
                    duration("2026-04-01", "2026-06-30", 6_000_000_000)]}}}}))
        with mock.patch.object(L, "_sec_get", FakeSec(pages)):
            L.build_corpus("LLY", self.run_dir)
        self.manifest = json.loads((self.run_dir / "manifest.json").read_text())
        self.chunks = [json.loads(line) for line
                       in (self.run_dir / "chunks.jsonl").read_text().splitlines() if line]
        self.facts = [json.loads(line) for line
                      in (self.run_dir / "facts.jsonl").read_text().splitlines() if line]

    def test_contract_file_set_is_complete(self):
        for name in ("manifest.json", "chunks.jsonl", "facts.jsonl"):
            self.assertTrue((self.run_dir / name).exists(), name)

    def test_every_fact_resolves_to_a_manifest_document(self):
        documents = {e["doc_id"] for e in self.manifest}
        self.assertTrue(self.facts)
        for fact in self.facts:
            self.assertIn(fact["doc_id"], documents)
            self.assertEqual(fact["source_kind"], "xbrl")
            self.assertEqual(fact["unit"], "millions")
            self.assertRegex(fact["currency"], r"^[A-Z]{3}$")

    def test_facts_cover_the_metric_keys_partner_b_reads(self):
        present = {f["metric"] for f in self.facts}
        for metric in ("revenue", "trade_receivables", "inventory", "net_profit"):
            self.assertIn(metric, present)

    def test_a_fact_and_a_chunk_can_be_tied_on_metric_and_period(self):
        # The number-tie: the Fact Checker pairs a figure quoted from a filing
        # table with the XBRL fact for the same metric and fiscal period.
        fact = next(f for f in self.facts
                    if f["metric"] == "revenue" and f["fiscal_period"] == "Q2 2026")
        self.assertEqual(fact["value"], 22974)
        printed = f"{int(fact['value']):,}"
        self.assertTrue(any(printed in c["text"] for c in self.chunks),
                        f"{printed} should appear in a filing-table chunk")

    def test_every_chunk_resolves_to_a_manifest_document(self):
        documents = {e["doc_id"]: e for e in self.manifest}
        self.assertTrue(self.chunks)
        for chunk in self.chunks:
            self.assertIn(chunk["doc_id"], documents)
            self.assertIsInstance(chunk["page"], int)
            self.assertIsInstance(chunk["is_table"], bool)
            self.assertIn("section", chunk)

    def test_a_citation_resolves_to_a_footnote_and_a_verbatim_quote(self):
        by_id = {c["chunk_id"]: c for c in self.chunks}
        documents = {e["doc_id"]: e for e in self.manifest}
        cited = next(c for c in self.chunks if c["is_table"] and len(c["text"]) > 60)

        chunk = by_id[cited["chunk_id"]]                    # hunter cites a chunk_id
        document = documents[chunk["doc_id"]]               # Fact Checker re-opens it
        quote = chunk["text"][10:60]                        # hunter quotes verbatim
        self.assertIn(quote, chunk["text"])

        footnote = (f"{document['company']} {document['fiscal_period']} "
                    f"{document['doc_type']}, p.{chunk['page']}")
        self.assertRegex(footnote, r"^Eli Lilly (FY|[QH]\d )\d{4} 10-[KQ], p\.\d+$")
        self.assertEqual(chunk["chunk_id"].rsplit("-p", 1)[0], document["doc_id"])

    def test_a_fabricated_chunk_id_does_not_resolve(self):
        by_id = {c["chunk_id"]: c for c in self.chunks}
        self.assertNotIn("LLY-10Q-2026Q2-p999-c01", by_id)


if __name__ == "__main__":
    unittest.main()
