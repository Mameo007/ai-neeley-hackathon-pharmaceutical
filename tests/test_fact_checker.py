"""Offline tests for pharmer/fact_checker.py - quote verification, the number-tie precision
rule, column attribution, trend/flags, confidence, and the claim-support judge behind a fake
client (no API calls).

The corpus and findings fixtures hold **real** figures: Lilly's FY2024 revenue as printed to
one decimal in the FY2024 10-K (45,042.7) and rounded in the FY2025 10-K's comparative column
(45,043), and Novo's trade receivables across the Q2 2026 and Q2 2025 6-Ks.

Run: .venv/bin/python -m unittest tests.test_fact_checker
"""
import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

import anthropic
import httpx2

from pharmer import fact_checker as fc
from pharmer import llm
from tests.test_llm import FakeStream, fake_message

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CORPUS = json.loads((FIXTURES / "factcheck_corpus.json").read_text(encoding="utf-8"))
FINDINGS = json.loads((FIXTURES / "factcheck_findings.json").read_text(encoding="utf-8"))

OWNED = ("confidence", "confidence_reason", "trend", "flags")


def support(claim_support="supported", metric_label_matches=True, reason="test",
            stop_reason="end_turn"):
    """A scripted claim-support reply."""
    text = json.dumps({"claim_support": claim_support,
                       "metric_label_matches": metric_label_matches, "reason": reason})
    return fake_message(stop_reason, [NS(type="text", text=text)])


def garbled(text="not json"):
    return fake_message("end_turn", [NS(type="text", text=text)])


class JudgeClient:
    """Answers each claim-support request from the first marker found in its user message,
    so a reply does not depend on the order concurrent reasonings reach the client."""

    def __init__(self, replies=None, default=None):
        self.replies = replies or {}
        self.default = default if default is not None else support()
        self.requests = []
        self.beta = NS(messages=NS(stream=self._stream))

    def _stream(self, **request):
        self.requests.append(request)
        content = request["messages"][0]["content"]
        for marker, message in self.replies.items():
            if marker in content:
                return FakeStream(message)
        return FakeStream(self.default)


class FailingClient(JudgeClient):
    """Every request raises, as a missing key or a connection failure would."""

    def __init__(self, error):
        super().__init__()
        self.error = error

    def _stream(self, **request):
        self.requests.append(request)
        error = self.error

        class Failing(FakeStream):
            async def __aenter__(self):
                raise error

        return Failing(None)


class FactCheckerTestCase(unittest.TestCase):
    """Builds a run folder from the fixture corpus; no network, no real client."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orig_log, self.orig_client = llm.LOG_PATH, llm._client
        llm.LOG_PATH = Path(self.tmp.name) / "logs" / "api_usage.csv"
        llm._client = JudgeClient()

    def tearDown(self):
        llm.LOG_PATH, llm._client = self.orig_log, self.orig_client
        self.tmp.cleanup()

    def make_run(self, ticker, findings=None, facts=True, extra_chunks=(),
                 extra_facts=(), extra_manifest=(), fact_rows=None):
        """A run folder holding `ticker`'s fixture corpus and findings.

        `extra_*` append rows to the fixture corpus - a rival filer's documents, a
        product-line table - so a test can state what must *not* be used as evidence.
        `fact_rows` replaces facts.jsonl outright when a test needs a different history.
        """
        corpus = CORPUS[ticker]
        run_dir = Path(self.tmp.name) / f"{ticker}-20261018-0230"
        run_dir.mkdir()
        (run_dir / "manifest.json").write_text(
            json.dumps(list(corpus["manifest"]) + list(extra_manifest), indent=2),
            encoding="utf-8")
        (run_dir / "chunks.jsonl").write_text(
            "".join(json.dumps(c, ensure_ascii=False) + "\n"
                    for c in list(corpus["chunks"]) + list(extra_chunks)),
            encoding="utf-8")
        if fact_rows is not None:
            rows = fact_rows
        else:
            rows = (list(corpus["facts"]) + list(extra_facts)) if facts else []
        (run_dir / "facts.jsonl").write_text(
            "".join(json.dumps(f, ensure_ascii=False) + "\n" for f in rows), encoding="utf-8")
        chosen = copy.deepcopy(FINDINGS[ticker] if findings is None else findings)
        (run_dir / "findings.json").write_text(
            json.dumps(chosen, indent=2, ensure_ascii=False), encoding="utf-8")
        return run_dir

    def check(self, ticker, findings=None, facts=True, client=None, **corpus_kwargs):
        """Run the stage and return (findings, report) as written to disk."""
        if client is not None:
            llm._client = client
        run_dir = self.make_run(ticker, findings, facts, **corpus_kwargs)
        asyncio.run(fc.run(run_dir))
        return (json.loads((run_dir / "findings.json").read_text(encoding="utf-8")),
                json.loads((run_dir / "fact_check_report.json").read_text(encoding="utf-8")))

    @staticmethod
    def by_id(findings):
        return {f["finding_id"]: f["reasonings"][0] for f in findings}

    @staticmethod
    def rows_by_id(report):
        return {row["finding_id"]: row for row in report["reasoning_checks"]}


# --------------------------------------------------------------------------
# precision (matrix: rounded restatement, contradicted number)


class PrecisionTests(unittest.TestCase):
    def test_tolerance_is_half_a_unit_of_the_coarser_value(self):
        # Lilly's FY2024 revenue: 45,042.7 printed, 45,043 in XBRL and the restatement.
        self.assertTrue(fc._ties(fc._number_of(45042.7), fc._number_of(45043.0)))
        # a genuinely different number stays different
        self.assertFalse(fc._ties(fc._number_of(45142.0), fc._number_of(45043.0)))
        self.assertFalse(fc._ties(fc._number_of(22974), fc._number_of(22975)))
        # both sides precise: the tolerance tightens with them
        self.assertFalse(fc._ties(fc._number_of(45042.7), fc._number_of(45042.8)))

    def test_trailing_zeros_are_not_precision(self):
        # facts.jsonl carries 45043.0 because XBRL amounts are divided by 1e6; that is an
        # integer figure and must not claim a decimal place of accuracy.
        self.assertEqual(fc._number_of(45043.0), (45043.0, 0))
        self.assertEqual(fc._number_of(45042.7), (45042.7, 1))

    def test_parentheses_are_the_filings_negative(self):
        self.assertEqual(fc._parse_number("(1,234)"), (-1234.0, 0))
        self.assertEqual(fc._parse_number("$ 45,042.7"), (45042.7, 1))
        self.assertIsNone(fc._parse_number("Revenue"))
        self.assertIsNone(fc._parse_number("30 Jun 2026"))


class RoundedTieTests(FactCheckerTestCase):
    def test_rounded_restatement_ties_and_reaches_high(self):
        findings, report = self.check("LLY")
        reasoning = self.by_id(findings)["F-001"]
        self.assertEqual(reasoning["confidence"], "High")
        row = self.rows_by_id(report)["F-001"]
        self.assertEqual(row["tie_fact_id"], "LLY-xbrl-revenue-FY2024")
        self.assertEqual(row["checks"]["tie"], "reproduced")

    def test_number_beyond_rounding_precision_is_low(self):
        findings = copy.deepcopy(FINDINGS["LLY"][:1])
        findings[0]["anchor"]["value"] = 45142.0
        findings[0]["reasonings"][0]["citations"] = [
            {"chunk_id": "LLY-10K-FY2024-p41-c03", "quote": "Revenue\t$\t45,042.7"}]
        findings, report = self.check("LLY", findings)
        reasoning = findings[0]["reasonings"][0]
        checks = self.rows_by_id(report)["F-001"]["checks"]
        self.assertEqual(reasoning["confidence"], "Low")
        # the FY2024 fact exists and disagrees beyond what rounding could explain
        self.assertEqual(checks["tie"], "contradicted")
        self.assertIn("contradict", reasoning["confidence_reason"])
        # and the figure is not printed in the cited evidence either
        self.assertFalse(checks["value_in_evidence"])

    def test_a_contradicting_fact_is_named(self):
        # Only the tie fails here: the quote carries the anchor value, but facts.jsonl
        # reports a different FY2024 revenue.
        findings = copy.deepcopy(FINDINGS["LLY"][:1])
        run_dir = self.make_run("LLY", findings)
        facts = [json.loads(line) for line
                 in (run_dir / "facts.jsonl").read_text(encoding="utf-8").splitlines()]
        for fact in facts:
            if fact["metric"] == "revenue" and fact["fiscal_period"] == "FY2024":
                fact["value"] = 44000.0
        (run_dir / "facts.jsonl").write_text(
            "".join(json.dumps(f) + "\n" for f in facts), encoding="utf-8")
        asyncio.run(fc.run(run_dir))
        report = json.loads(
            (run_dir / "fact_check_report.json").read_text(encoding="utf-8"))
        row = self.rows_by_id(report)["F-001"]
        self.assertEqual(row["checks"]["tie"], "contradicted")
        self.assertEqual(row["confidence"], "Low")
        self.assertEqual(row["tie_fact_id"], "LLY-xbrl-revenue-FY2024")
        self.assertTrue(row["checks"]["value_in_evidence"])


# --------------------------------------------------------------------------
# column attribution (matrix: comparative column, wrong column rejected)


class ColumnTests(unittest.TestCase):
    FY2025 = next(c for c in CORPUS["LLY"]["chunks"]
                  if c["chunk_id"] == "LLY-10K-FY2025-p43-c03")
    NESTED = next(c for c in CORPUS["LLY"]["chunks"]
                  if c["chunk_id"] == "LLY-10K-FY2025-p49-c06")
    TENQ = next(c for c in CORPUS["LLY"]["chunks"]
                if c["chunk_id"] == "LLY-10Q-2026Q2-p5-c03")
    NVO = next(c for c in CORPUS["NVO"]["chunks"]
               if c["chunk_id"] == "NVO-6K-2026Q2-p28-c02")

    def test_comparative_column_keeps_its_own_period(self):
        rows = fc._table_rows(self.FY2025, "FY2025")
        revenue = [(period, value[0]) for label, period, value in rows
                   if label == "Revenue"]
        self.assertIn(("FY2024", 45043.0), revenue)   # the comparative column
        self.assertIn(("FY2025", 65179.0), revenue)   # the reporting column
        # the FY2025 figure is never offered as an FY2024 value
        self.assertNotIn(("FY2024", 65179.0), revenue)

    def test_quarter_and_half_year_columns_are_told_apart(self):
        self.assertEqual(fc._column_periods(self.TENQ["text"]),
                         ["Q2 2026", "Q2 2025", "H1 2026", "H1 2025"])
        rows = fc._table_rows(self.TENQ, "Q2 2026")
        revenue = dict((period, value[0]) for label, period, value in rows
                       if label == "Revenue")
        self.assertEqual(revenue["Q2 2026"], 22974.0)
        self.assertEqual(revenue["H1 2026"], 42773.0)

    def test_nested_table_attributes_nothing(self):
        # U.S. / Outside U.S. / Total / Total prints more value columns than the header
        # declares periods; guessing here is the wrong-column error the rule exists to catch.
        rows = fc._table_rows(self.NESTED, "FY2025")
        self.assertTrue(rows)
        self.assertTrue(all(period is None for _label, period, _value in rows))

    def test_space_joined_table_falls_back_to_the_documents_own_period(self):
        rows = fc._table_rows(self.NVO, "Q2 2026")
        receivables = [(period, value[0]) for label, period, value in rows
                       if label == "Trade receivables"]
        self.assertEqual(receivables, [("Q2 2026", 88949.0), (None, 70856.0)])
        self.assertIsNone(fc._column_periods(self.NVO["text"]))


class CorroborationTests(FactCheckerTestCase):
    def test_comparative_column_hit_is_recorded(self):
        _findings, report = self.check("LLY")
        hits = self.rows_by_id(report)["F-001"]["corroboration"]
        found = {(hit["chunk_id"], hit["fiscal_period"], hit["value"]) for hit in hits}
        # the FY2024 figure is printed in the *FY2025* 10-K's comparative column
        self.assertIn(("LLY-10K-FY2025-p43-c03", "FY2024", 45043.0), found)
        self.assertIn(("LLY-10K-FY2024-p41-c03", "FY2024", 45042.7), found)

    def test_value_from_another_period_column_is_not_a_hit(self):
        # F-003 anchors FY2024 on 65,179, which is the FY2025 column of that same table.
        findings, report = self.check("LLY")
        self.assertEqual(self.by_id(findings)["F-003"]["confidence"], "Low")
        self.assertEqual(self.rows_by_id(report)["F-003"]["corroboration"], [])


# --------------------------------------------------------------------------
# quote verification (matrix: tabs vs spaces, missing evidence)


class QuoteTests(FactCheckerTestCase):
    def test_space_joined_quote_matches_a_tab_joined_chunk(self):
        # Partner B's sample corpus joins table cells with spaces, the Librarian with tabs
        # (docs/build-log.md blocker, 2026-10-18); the same table must still verify.
        findings, report = self.check("LLY")
        reasoning = self.by_id(findings)["F-002"]
        self.assertEqual(self.rows_by_id(report)["F-002"]["checks"]["quote"], "normalized")
        self.assertEqual(reasoning["confidence"], "High")
        self.assertIn("whitespace", reasoning["confidence_reason"])
        self.assertEqual(report["normalized_quote_matches"], 1)

    def test_whitespace_normalization_is_only_offered_to_tables(self):
        prose = next(c for c in CORPUS["LLY"]["chunks"] if not c["is_table"])
        spaced = " ".join(prose["text"].split())[:60]
        self.assertEqual(fc._verify_quote(spaced, prose), "partial")
        table = ColumnTests.FY2025
        self.assertEqual(fc._verify_quote("Revenue $ 65,179", table), "normalized")
        self.assertEqual(fc._verify_quote("Revenue\t$\t65,179", table), "exact")

    def test_missing_chunk_is_low(self):
        findings = copy.deepcopy(FINDINGS["LLY"][:1])
        findings[0]["reasonings"][0]["citations"] = [
            {"chunk_id": "LLY-10K-FY2024-p99-c99", "quote": "Revenue"}]
        findings, report = self.check("LLY", findings)
        self.assertEqual(findings[0]["reasonings"][0]["confidence"], "Low")
        self.assertIn("not in chunks.jsonl", findings[0]["reasonings"][0]["confidence_reason"])
        self.assertEqual(self.rows_by_id(report)["F-001"]["checks"]["quote"], "absent")

    def test_quote_absent_from_the_chunk_is_low(self):
        findings = copy.deepcopy(FINDINGS["LLY"][:1])
        findings[0]["reasonings"][0]["citations"] = [
            {"chunk_id": "LLY-10K-FY2024-p41-c03",
             "quote": "Revenue grew because of channel loading"}]
        findings, _report = self.check("LLY", findings)
        self.assertEqual(findings[0]["reasonings"][0]["confidence"], "Low")

    def test_evidence_without_the_anchor_value_is_low(self):
        findings = copy.deepcopy(FINDINGS["LLY"][:1])
        findings[0]["reasonings"][0]["citations"] = [
            {"chunk_id": "LLY-10K-FY2024-p41-c03", "quote": "Net income"}]
        findings, _report = self.check("LLY", findings)
        reasoning = findings[0]["reasonings"][0]
        self.assertEqual(reasoning["confidence"], "Low")
        self.assertIn("not printed in the cited evidence", reasoning["confidence_reason"])

    def test_partial_quote_match_caps_at_medium(self):
        findings = copy.deepcopy(FINDINGS["LLY"][:1])
        findings[0]["reasonings"][0]["citations"] = [
            {"chunk_id": "LLY-10K-FY2024-p41-c03", "quote": "revenue | $ | 45,042.7"}]
        findings, report = self.check("LLY", findings)
        self.assertEqual(self.rows_by_id(report)["F-001"]["checks"]["quote"], "partial")
        self.assertEqual(findings[0]["reasonings"][0]["confidence"], "Medium")


# --------------------------------------------------------------------------
# number tie (matrix: no second source, mismatched period)


class TieTests(FactCheckerTestCase):
    def test_annual_only_xbrl_leaves_no_second_source(self):
        # Novo's companyfacts is ifrs-full, DKK and annual only, so a Q2 figure has no
        # second artifact to tie against and cannot reach High.
        findings, report = self.check("NVO")
        reasoning = self.by_id(findings)["F-001"]
        self.assertEqual(reasoning["confidence"], "Medium")
        self.assertEqual(self.rows_by_id(report)["F-001"]["checks"]["tie"], "unavailable")
        self.assertIn("no second source", reasoning["confidence_reason"])
        self.assertIn("Q2 2026", reasoning["confidence_reason"])

    def test_annual_anchor_ties_against_annual_xbrl(self):
        findings, report = self.check("NVO")
        reasoning = self.by_id(findings)["F-002"]
        self.assertEqual(self.rows_by_id(report)["F-002"]["tie_fact_id"],
                         "NVO-xbrl-trade_receivables-FY2025")
        self.assertEqual(reasoning["confidence"], "High")

    def test_value_belonging_to_another_period_is_low(self):
        # The hunter read the comparative column and labelled it as its own period.
        findings = copy.deepcopy(FINDINGS["LLY"][:1])
        findings[0]["anchor"]["fiscal_period"] = "FY2025"
        findings, report = self.check("LLY", findings)
        reasoning = findings[0]["reasonings"][0]
        self.assertEqual(reasoning["confidence"], "Low")
        self.assertEqual(self.rows_by_id(report)["F-001"]["checks"]["tie"], "mismatch")
        self.assertIn("FY2024", reasoning["confidence_reason"])

    def test_unit_mismatch_is_low(self):
        findings = copy.deepcopy(FINDINGS["LLY"][:1])
        findings[0]["anchor"]["unit"] = "thousands"
        findings, report = self.check("LLY", findings)
        self.assertEqual(findings[0]["reasonings"][0]["confidence"], "Low")
        self.assertEqual(self.rows_by_id(report)["F-001"]["checks"]["tie"], "mismatch")

    def test_currency_mismatch_is_low(self):
        findings = copy.deepcopy(FINDINGS["LLY"][:1])
        findings[0]["anchor"]["currency"] = "DKK"
        findings, _report = self.check("LLY", findings)
        reasoning = findings[0]["reasonings"][0]
        self.assertEqual(reasoning["confidence"], "Low")
        self.assertIn("DKK", reasoning["confidence_reason"])

    def test_empty_facts_file_caps_every_reasoning_at_medium(self):
        findings, report = self.check("LLY", facts=False)
        levels = {f["reasonings"][0]["confidence"] for f in findings}
        self.assertNotIn("High", levels)
        self.assertEqual(report["confidence_counts"]["High"], 0)
        self.assertEqual(self.by_id(findings)["F-001"]["confidence"], "Medium")

    def test_an_absent_facts_file_does_not_fail_the_run(self):
        run_dir = self.make_run("LLY")
        (run_dir / "facts.jsonl").unlink()
        asyncio.run(fc.run(run_dir))
        findings = json.loads((run_dir / "findings.json").read_text(encoding="utf-8"))
        self.assertNotIn("High", {f["reasonings"][0]["confidence"] for f in findings})
        self.assertEqual(self.by_id(findings)["F-001"]["confidence"], "Medium")


# --------------------------------------------------------------------------
# trend, reversal, seasonality (matrix: like-for-like, reversal)


class TrendTests(FactCheckerTestCase):
    def test_prior_year_quarter_confirms_like_for_like(self):
        # Novo's Q2 2025 6-K is in the corpus, so Q2 2026 is compared year-on-year rather
        # than against the last year end - no "possibly seasonal" warning.
        findings, report = self.check("NVO")
        reasoning = self.by_id(findings)["F-001"]
        self.assertNotIn("possibly_seasonal", reasoning["flags"])
        self.assertTrue(self.rows_by_id(report)["F-001"]["checks"]["like_for_like"])
        self.assertIn("Q2 2025", [p["fiscal_period"] for p in reasoning["trend"]])

    def test_missing_prior_year_comparator_flags_possibly_seasonal(self):
        # The corpus holds Q2 2025, Q2 2026 and the annual facts, but no first quarter, so
        # a Q1 anchor has nothing to compare like-for-like against.
        findings = copy.deepcopy(FINDINGS["NVO"][:1])
        findings[0]["anchor"]["fiscal_period"] = "Q1 2026"
        findings, _report = self.check("NVO", findings)
        reasoning = findings[0]["reasonings"][0]
        self.assertIn("possibly_seasonal", reasoning["flags"])
        self.assertEqual(reasoning["confidence"], "Medium")
        self.assertIn("possibly seasonal", reasoning["confidence_reason"])

    def test_direction_flip_sets_reversal(self):
        # 71,949 (FY2024) -> 70,856 (FY2025) -> 88,949 (Q2 2026): down, then sharply up.
        findings, _report = self.check("NVO")
        reasoning = self.by_id(findings)["F-001"]
        self.assertIn("reversal", reasoning["flags"])
        self.assertEqual([p["fiscal_period"] for p in reasoning["trend"]],
                         ["FY2023", "FY2024", "Q2 2025", "FY2025", "Q2 2026"])

    def test_a_series_that_only_rises_has_no_reversal(self):
        findings, _report = self.check("LLY")
        reasoning = self.by_id(findings)["F-001"]
        self.assertNotIn("reversal", reasoning["flags"])
        self.assertEqual([p["value"] for p in reasoning["trend"]],
                         sorted(p["value"] for p in reasoning["trend"]))

    def test_a_flow_trend_stays_inside_its_period_family(self):
        # A quarter of revenue against a full year of it is a four-fold step that would read
        # as a reversal on every finding.
        findings, _report = self.check("LLY")
        families = {fc._period_family(p["fiscal_period"])
                    for p in self.by_id(findings)["F-001"]["trend"]}
        self.assertEqual(families, {"FY"})
        quarterly = {fc._period_family(p["fiscal_period"])
                     for p in self.by_id(findings)["F-004"]["trend"]}
        self.assertEqual(quarterly, {"Q2"})

    def test_a_balance_trend_mixes_periods(self):
        # Balance-sheet items are point-in-time, so FY and Q figures are comparable
        # balances - the Contract 2 example mixes them for exactly that reason.
        findings, _report = self.check("NVO")
        families = {fc._period_family(p["fiscal_period"])
                    for p in self.by_id(findings)["F-001"]["trend"]}
        self.assertEqual(families, {"FY", "Q2"})

    def test_the_strip_is_capped(self):
        findings, _report = self.check("LLY")
        for finding in findings:
            self.assertLessEqual(len(finding["reasonings"][0]["trend"]),
                                 fc.TREND_MAX_POINTS)


# --------------------------------------------------------------------------
# claim support (matrix: claim unsupported, LLM unavailable)


class ClaimSupportTests(FactCheckerTestCase):
    def test_contradicted_claim_is_low(self):
        client = JudgeClient({"45,042.7 is printed to one decimal": support(
            "contradicted", reason="the quote reports revenue, not a restatement")})
        findings, report = self.check("LLY", client=client)
        reasoning = self.by_id(findings)["F-001"]
        self.assertEqual(reasoning["confidence"], "Low")
        self.assertIn("claim contradicted", reasoning["confidence_reason"])
        self.assertEqual(self.rows_by_id(report)["F-001"]["checks"]["claim_support"],
                         "contradicted")

    def test_unclear_support_caps_at_medium(self):
        client = JudgeClient(default=support("unclear", reason="plausible but unestablished"))
        findings, report = self.check("LLY", client=client)
        self.assertEqual(self.by_id(findings)["F-001"]["confidence"], "Medium")
        self.assertEqual(report["claim_support_unverified"], len(report["reasoning_checks"]))

    def test_a_judged_row_label_mismatch_is_low(self):
        client = JudgeClient(default=support(metric_label_matches=False))
        findings, _report = self.check("LLY", client=client)
        reasoning = self.by_id(findings)["F-001"]
        self.assertEqual(reasoning["confidence"], "Low")
        self.assertIn("different financial concept", reasoning["confidence_reason"])

    def test_refusal_caps_at_medium(self):
        client = JudgeClient(default=support(stop_reason="refusal"))
        findings, _report = self.check("LLY", client=client)
        self.assertEqual(self.by_id(findings)["F-001"]["confidence"], "Medium")

    def test_truncated_reply_caps_at_medium(self):
        client = JudgeClient(default=support(stop_reason="max_tokens"))
        findings, _report = self.check("LLY", client=client)
        self.assertEqual(self.by_id(findings)["F-001"]["confidence"], "Medium")

    def test_malformed_reply_caps_at_medium(self):
        findings, report = self.check("LLY", client=JudgeClient(default=garbled()))
        self.assertEqual(self.by_id(findings)["F-001"]["confidence"], "Medium")
        self.assertIn("malformed", self.by_id(findings)["F-001"]["confidence_reason"])
        self.assertEqual(report["claim_support_unverified"], len(report["reasoning_checks"]))

    def test_out_of_schema_reply_caps_at_medium(self):
        client = JudgeClient(default=garbled(json.dumps(
            {"claim_support": "very supported", "metric_label_matches": "yes",
             "reason": "x"})))
        findings, _report = self.check("LLY", client=client)
        self.assertEqual(self.by_id(findings)["F-001"]["confidence"], "Medium")

    def test_judge_unavailable_caps_at_medium_and_never_raises(self):
        # An API error or a missing key must not take the stage down with it.
        error = anthropic.APIConnectionError(request=httpx2.Request("POST", "https://x"))
        findings, report = self.check("LLY", client=FailingClient(error))
        self.assertEqual(self.by_id(findings)["F-001"]["confidence"], "Medium")
        self.assertIn("judge unavailable", self.by_id(findings)["F-001"]["confidence_reason"])
        self.assertEqual(report["confidence_counts"]["High"], 0)

    def test_the_judge_is_not_called_when_the_evidence_is_unverified(self):
        findings = copy.deepcopy(FINDINGS["LLY"][:1])
        findings[0]["reasonings"][0]["citations"] = [
            {"chunk_id": "LLY-10K-FY2024-p99-c99", "quote": "Revenue"}]
        client = JudgeClient()
        self.check("LLY", findings, client=client)
        self.assertEqual(client.requests, [])

    def test_the_judge_sees_the_quotes_their_pages_and_the_trend(self):
        client = JudgeClient()
        self.check("NVO", client=client)
        content = client.requests[0]["messages"][0]["content"]
        self.assertIn("# Claim", content)
        self.assertIn("# Trend", content)
        self.assertIn("NVO-6K-2026Q2 p.28", content)
        self.assertEqual(client.requests[0]["model"], llm.HAIKU)
        self.assertIs(client.requests[0]["system"], fc.SYSTEM)


# --------------------------------------------------------------------------
# ownership and the sidecar


class OwnershipTests(FactCheckerTestCase):
    def test_only_fact_checker_fields_change(self):
        before = copy.deepcopy(FINDINGS["LLY"])
        after, _report = self.check("LLY")
        self.assertEqual(len(before), len(after))
        for old, new in zip(before, after):
            self.assertEqual({k: v for k, v in old.items() if k != "reasonings"},
                             {k: v for k, v in new.items() if k != "reasonings"})
            for old_r, new_r in zip(old["reasonings"], new["reasonings"]):
                self.assertEqual({k: v for k, v in old_r.items() if k not in OWNED},
                                 {k: v for k, v in new_r.items() if k not in OWNED})

    def test_flags_written_by_dedup_are_preserved(self):
        after, _report = self.check("LLY")
        # F-004 arrives contested from Dedup; the Fact Checker owns only its own two flags.
        self.assertIn("contested", self.by_id(after)["F-004"]["flags"])

    def test_a_rerun_is_idempotent(self):
        run_dir = self.make_run("LLY")
        asyncio.run(fc.run(run_dir))
        once = (run_dir / "findings.json").read_text(encoding="utf-8")
        llm._client = JudgeClient()
        asyncio.run(fc.run(run_dir))
        self.assertEqual(once, (run_dir / "findings.json").read_text(encoding="utf-8"))

    def test_every_reasoning_gets_a_confidence_and_a_reason(self):
        for ticker in ("LLY", "NVO"):
            findings, report = self.check(ticker)
            reasonings = [r for f in findings for r in f["reasonings"]]
            self.assertEqual(len(reasonings), report["reasonings"])
            for reasoning in reasonings:
                self.assertIn(reasoning["confidence"], ("High", "Medium", "Low"))
                self.assertTrue(reasoning["confidence_reason"].endswith("."))

    def test_the_sidecar_counts_match_the_findings(self):
        findings, report = self.check("LLY")
        counts = {level: sum(f["reasonings"][0]["confidence"] == level for f in findings)
                  for level in ("High", "Medium", "Low")}
        self.assertEqual(report["confidence_counts"], counts)
        self.assertEqual(report["stage"], "fact-checker")
        self.assertEqual(report["findings"], len(findings))

    def test_one_usage_row_per_judged_reasoning(self):
        _findings, report = self.check("NVO")
        with llm.LOG_PATH.open(encoding="utf-8") as fh:
            rows = [line for line in fh.read().splitlines()[1:] if line.strip()]
        self.assertEqual(len(rows), report["reasonings"])
        self.assertTrue(all(",fact-checker,fact_checker," in row for row in rows))


class LabelTests(unittest.TestCase):
    def test_printed_labels_map_to_concept_map_metrics(self):
        self.assertTrue(fc._label_matches("trade_receivables", "Trade receivables"))
        self.assertTrue(fc._label_matches("revenue", "Revenue"))
        self.assertTrue(fc._label_matches("revenue", "Net sales"))
        self.assertTrue(fc._label_matches("inventory", "Inventories"))
        # a product line is not the revenue line, and a different balance is not receivables
        self.assertFalse(fc._label_matches("revenue", "Mounjaro"))
        self.assertFalse(fc._label_matches("trade_receivables", "TOTAL CURRENT ASSETS"))
        self.assertIsNone(fc._label_matches("revenue", ""))

    def test_presentation_words_on_the_same_line_still_match(self):
        # The judge prompt promises `Accounts receivable, net` matches trade_receivables.
        self.assertTrue(fc._label_matches("trade_receivables", "Accounts receivable, net"))
        self.assertTrue(fc._label_matches("revenue", "Revenue, net"))
        self.assertTrue(fc._label_matches("inventory", "Inventories, net"))
        self.assertTrue(fc._label_matches("revenue", "Total revenues"))

    def test_a_qualified_row_is_a_different_row(self):
        # Review finding: a prefix rule handed each of these the metric's own hit, which
        # is how a segment, a product or a counterparty row corroborates a statement line
        # it has nothing to do with (spec Boundaries: not a segment/product subtotal).
        self.assertFalse(fc._label_matches("revenue", "Revenue from collaborations"))
        self.assertFalse(fc._label_matches("revenue", "Revenues, net of rebates"))
        self.assertFalse(fc._label_matches("revenue", "Total revenues by segment"))
        self.assertFalse(fc._label_matches("cash", "Cash flow from operations"))
        self.assertFalse(fc._label_matches("cash", "Cash used in investing"))
        self.assertFalse(fc._label_matches("trade_receivables",
                                           "Receivables from related parties"))


# --------------------------------------------------------------------------
# regressions from the independent review (2026-10-18)


# A product-line row that starts with the metric's own word, in a table whose header
# reads cleanly, so the row really is attributed to a period.
PRODUCT_CHUNK = {
    "chunk_id": "LLY-10K-FY2024-p42-c01", "doc_id": "LLY-10K-FY2024", "page": 42,
    "section": "Item 7.Management's Discussion and Analysis", "is_table": True,
    "text": "Year Ended December 31,\n2024\t2023\n"
            "Revenue from collaborations\t$\t45,042.7\t$\t34,124.1",
}

# The same metric, period, unit and currency as Novo's anchor - but Lilly's filing.
RIVAL_MANIFEST = [{
    "doc_id": "LLY-10K-FY2025", "company": "Eli Lilly", "ticker": "LLY", "doc_type": "10-K",
    "fiscal_period": "FY2025", "period_end": "2025-12-31", "filed_date": "2026-02-12",
    "url": "https://www.sec.gov/", "accession": None, "source": "pre-collected",
    "collected_date": "2026-10-18", "application_no": None, "labeler_code": None,
}, {
    "doc_id": "LLY-XBRL", "company": "Eli Lilly", "ticker": "LLY", "doc_type": "xbrl",
    "fiscal_period": "FY2025", "period_end": "2025-12-31", "filed_date": None,
    "url": "https://data.sec.gov/", "accession": None, "source": "pre-collected",
    "collected_date": "2026-10-18", "application_no": None, "labeler_code": None,
}]

RIVAL_FACT = {
    "fact_id": "LLY-xbrl-trade_receivables-2026Q2", "doc_id": "LLY-XBRL",
    "metric": "trade_receivables", "value": 88949, "unit": "millions", "currency": "DKK",
    "fiscal_period": "Q2 2026", "period_end": "2026-06-30", "source_kind": "xbrl",
}

RIVAL_CHUNK = {
    "chunk_id": "LLY-10K-FY2025-p80-c01", "doc_id": "LLY-10K-FY2025", "page": 80,
    "section": "Balance sheet", "is_table": True,
    "text": "DKK million 30 Jun 2026 31 Dec 2025\nTrade receivables 88,949 70,856",
}


class ColumnPositionRegressionTests(unittest.TestCase):
    """A declared non-period column holds the column it was printed in."""

    HEADER = "Year Ended December 31,\n2025\tPercent Change\t2024"
    TEXT = HEADER + "\nRevenue\t65,179\t45\t45,043"

    def test_a_non_period_column_keeps_its_own_position(self):
        # Before: the gap was appended after the years, so the 45% change landed on
        # FY2024 and the real comparative figure was left unattributed.
        self.assertEqual(fc._column_periods(self.HEADER), ["FY2025", None, "FY2024"])

    def test_the_percent_change_cell_is_not_offered_as_a_period_value(self):
        rows = fc._table_rows({"text": self.TEXT, "is_table": True}, "FY2025")
        attributed = {period: value[0] for _label, period, value in rows}
        self.assertEqual(attributed["FY2025"], 65179.0)
        self.assertEqual(attributed["FY2024"], 45043.0)
        self.assertNotIn(45.0, [value[0] for _l, period, value in rows if period])

    def test_a_trailing_non_period_column_is_unchanged(self):
        # The real LLY tables print `Percent Change` last; that layout must not move.
        self.assertEqual(fc._column_periods("Year Ended December 31,\tPercent Change\n2024\t2023"),
                         ["FY2024", "FY2023", None])
        self.assertEqual(fc._column_periods(ColumnTests.TENQ["text"]),
                         ["Q2 2026", "Q2 2025", "H1 2026", "H1 2025"])


class MetricRowRegressionTests(FactCheckerTestCase):
    def test_a_product_row_does_not_corroborate_the_statement_line(self):
        # Before: `Revenue from collaborations` matched `revenue` by prefix, so the
        # product row corroborated the revenue anchor and joined its trend strip.
        _findings, report = self.check("LLY", extra_chunks=[PRODUCT_CHUNK])
        hits = self.rows_by_id(report)["F-001"]["corroboration"]
        self.assertNotIn(PRODUCT_CHUNK["chunk_id"], {hit["chunk_id"] for hit in hits})
        self.assertTrue(hits, "the real revenue rows must still corroborate")


class CompanyScopeRegressionTests(FactCheckerTestCase):
    def test_a_tie_never_crosses_companies(self):
        # Before: `_tie` filtered on metric, period, unit and currency only, so a rival
        # filer's identical figure reproduced Novo's anchor - the corpus gains rival
        # filings in architecture §4.0.
        findings, report = self.check("NVO", extra_facts=[RIVAL_FACT],
                                      extra_manifest=RIVAL_MANIFEST)
        row = self.rows_by_id(report)["F-001"]
        self.assertEqual(row["checks"]["tie"], "unavailable")
        self.assertIsNone(row["tie_fact_id"])
        self.assertEqual(self.by_id(findings)["F-001"]["confidence"], "Medium")

    def test_a_rival_table_neither_corroborates_nor_bends_the_trend(self):
        findings, report = self.check("NVO", extra_chunks=[RIVAL_CHUNK],
                                      extra_manifest=RIVAL_MANIFEST)
        row = self.rows_by_id(report)["F-001"]
        self.assertNotIn(RIVAL_CHUNK["chunk_id"], {h["chunk_id"] for h in row["corroboration"]})
        self.assertEqual([p["fiscal_period"] for p in self.by_id(findings)["F-001"]["trend"]],
                         ["FY2023", "FY2024", "Q2 2025", "FY2025", "Q2 2026"])

    def test_a_manifest_placed_fact_still_ties_to_high(self):
        # The guard must not cost the documented ties: NVO-XBRL is in the manifest under
        # ticker NVO, so FY2025 receivables still reproduce.
        findings, report = self.check("NVO")
        self.assertEqual(self.rows_by_id(report)["F-002"]["checks"]["tie"], "reproduced")
        self.assertEqual(self.by_id(findings)["F-002"]["confidence"], "High")

    def test_an_unplaceable_fact_is_read_but_cannot_grant_high(self):
        # Only a *known* disagreement excludes evidence - an unlisted doc_id is not
        # evidence of another company, so the fact is still read and named. But it cannot
        # certify a cross-source tie: High would then rest on evidence whose owner nobody
        # can show, which is the cross-company error the guard exists to prevent.
        rows = [dict(fact, doc_id="NVO-XBRL-UNLISTED") for fact in CORPUS["NVO"]["facts"]]
        findings, report = self.check("NVO", fact_rows=rows)
        row = self.rows_by_id(report)["F-002"]
        self.assertEqual(row["checks"]["tie"], "unattributed")
        self.assertEqual(row["tie_fact_id"], "NVO-xbrl-trade_receivables-FY2025")
        self.assertIn("does not place", row["checks"]["tie_detail"])
        self.assertEqual(self.by_id(findings)["F-002"]["confidence"], "Medium")

    def test_a_finding_without_a_ticker_cannot_reach_high(self):
        # Contract 2 requires `ticker`; without it nothing can be placed, so no tie can
        # be certified and the honest ceiling is Medium.
        findings = copy.deepcopy(FINDINGS["NVO"][1:2])
        findings[0]["ticker"] = None
        out, report = self.check("NVO", findings)
        self.assertEqual(self.rows_by_id(report)["F-002"]["checks"]["tie"], "unattributed")
        self.assertEqual(out[0]["reasonings"][0]["confidence"], "Medium")

    def test_an_unplaceable_fact_of_another_period_is_still_a_mismatch(self):
        # The ownership guard must not rescue a wrong-period extraction.
        rows = [dict(fact, doc_id="LLY-XBRL-UNLISTED") for fact in CORPUS["LLY"]["facts"]]
        findings = copy.deepcopy(FINDINGS["LLY"][2:3])  # 65,179 labelled FY2024
        out, report = self.check("LLY", findings, fact_rows=rows)
        self.assertEqual(self.rows_by_id(report)["F-003"]["checks"]["tie"], "mismatch")
        self.assertEqual(out[0]["reasonings"][0]["confidence"], "Low")


class LabelFindingRegressionTests(FactCheckerTestCase):
    """A finding with no number is unconfirmable, not contradicted."""

    def label_finding(self):
        finding = copy.deepcopy(FINDINGS["LLY"][0])
        finding["is_label_finding"] = True
        finding["anchor"] = {"metric": "label_breadth", "entity": "Eli Lilly and Company",
                             "fiscal_period": "FY2026", "value": None, "unit": None,
                             "currency": None}
        finding["reasonings"][0]["claim"] = "The 10-Q cover page names the quarter ended."
        finding["reasonings"][0]["citations"] = [
            {"chunk_id": "LLY-10Q-2026Q2-p1-c02",
             "quote": "For the quarterly period ended June 30, 2026"}]
        return [finding]

    def test_a_finding_without_a_number_is_not_rejected(self):
        # Before: a non-numeric anchor was reported as a tie `mismatch`, which is fatal,
        # so every label/product finding came back Low and the scoring gate dropped it -
        # leaving architecture §5's label guarantee nothing eligible to swap in.
        findings, report = self.check("LLY", self.label_finding())
        reasoning = findings[0]["reasonings"][0]
        checks = self.rows_by_id(report)["F-001"]["checks"]
        self.assertEqual(checks["tie"], "not_applicable")
        self.assertFalse(checks["anchor_has_value"])
        self.assertEqual(reasoning["confidence"], "Medium")
        self.assertIn("no number", reasoning["confidence_reason"])

    def test_a_finding_without_a_number_is_still_judged_on_its_quote(self):
        client = JudgeClient(
            default=support("contradicted", reason="the cover page names no label"))
        findings, _report = self.check("LLY", self.label_finding(), client=client)
        self.assertEqual(findings[0]["reasonings"][0]["confidence"], "Low")

    def test_a_label_finding_whose_quote_is_absent_is_still_low(self):
        findings = self.label_finding()
        findings[0]["reasonings"][0]["citations"] = [
            {"chunk_id": "LLY-10Q-2026Q2-p1-c02", "quote": "a sentence that is not there"}]
        out, _report = self.check("LLY", findings)
        self.assertEqual(out[0]["reasonings"][0]["confidence"], "Low")


class SeasonalityRegressionTests(FactCheckerTestCase):
    def fy2023_facts(self):
        """LLY's fixture facts without FY2022, so FY2023 has no prior-year comparator."""
        return [f for f in CORPUS["LLY"]["facts"] if f["fiscal_period"] != "FY2022"]

    def fy2023_finding(self):
        finding = copy.deepcopy(FINDINGS["LLY"][0])
        finding["anchor"]["fiscal_period"] = "FY2023"
        finding["anchor"]["value"] = 34124.1
        finding["reasonings"][0]["claim"] = "FY2023 revenue was 34,124.1."
        finding["reasonings"][0]["citations"] = [
            {"chunk_id": "LLY-10K-FY2024-p41-c03", "quote": "Revenue\t$\t45,042.7\t$\t34,124.1"}]
        return [finding]

    def test_a_full_year_anchor_is_not_called_possibly_seasonal(self):
        # Before: any anchor at the oldest end of the corpus was flagged and capped at
        # Medium, including a full-year figure, where there is no seasonality to confirm
        # (docs/architecture.md §4.3 is about a part-year figure against a year end).
        findings, report = self.check("LLY", self.fy2023_finding(),
                                     fact_rows=self.fy2023_facts())
        reasoning = findings[0]["reasonings"][0]
        checks = self.rows_by_id(report)["F-001"]["checks"]
        self.assertEqual(checks["like_for_like"], "not_applicable")
        self.assertNotIn("possibly_seasonal", reasoning["flags"])
        self.assertEqual(reasoning["confidence"], "High")

    def test_a_part_year_anchor_still_needs_its_prior_year(self):
        findings = copy.deepcopy(FINDINGS["NVO"][:1])
        findings[0]["anchor"]["fiscal_period"] = "Q1 2026"
        out, report = self.check("NVO", findings)
        self.assertEqual(self.rows_by_id(report)["F-001"]["checks"]["like_for_like"],
                         "unconfirmed")
        self.assertIn("possibly_seasonal", out[0]["reasonings"][0]["flags"])

    def test_a_reversed_full_year_anchor_still_needs_its_prior_year(self):
        # A flip is the one case where a full-year figure must be confirmed like-for-like.
        status, _detail = fc._like_for_like({"fiscal_period": "FY2024"}, [], reversal=True)
        self.assertEqual(status, "unconfirmed")


class ReversalWindowRegressionTests(FactCheckerTestCase):
    def old_flip_facts(self):
        """Revenue that wobbled years ago and has risen steadily ever since."""
        periods = [("FY2020", 20000.0), ("FY2021", 18000.0), ("FY2022", 28541.4),
                   ("FY2023", 34124.0), ("FY2024", 45043.0)]
        return [{"fact_id": f"LLY-xbrl-revenue-{period}", "doc_id": "LLY-XBRL",
                 "metric": "revenue", "value": value, "unit": "millions", "currency": "USD",
                 "fiscal_period": period, "period_end": None, "source_kind": "xbrl"}
                for period, value in periods]

    def test_a_flip_years_before_the_anchor_is_not_this_findings_reversal(self):
        # Before: a direction change anywhere in the strip set the flag, so a wobble five
        # periods back became the story of an anchor whose own direction never changed.
        findings, _report = self.check("LLY", copy.deepcopy(FINDINGS["LLY"][:1]),
                                       fact_rows=self.old_flip_facts())
        reasoning = findings[0]["reasonings"][0]
        self.assertIn("FY2021", [p["fiscal_period"] for p in reasoning["trend"]])
        self.assertNotIn("reversal", reasoning["flags"])

    def test_the_documented_novo_flip_is_still_a_reversal(self):
        findings, _report = self.check("NVO")
        self.assertIn("reversal", self.by_id(findings)["F-001"]["flags"])

    def test_a_later_periods_move_is_not_the_anchors_reversal(self):
        trend = [{"fiscal_period": "FY2023", "value": 100.0},
                 {"fiscal_period": "FY2024", "value": 120.0},
                 {"fiscal_period": "FY2025", "value": 90.0}]
        self.assertFalse(fc._reversal(trend, "FY2024"))
        self.assertTrue(fc._reversal(trend, "FY2025"))


class MissingCorpusRegressionTests(FactCheckerTestCase):
    def test_a_missing_chunks_file_is_unconfirmable_not_contradicted(self):
        # Before: an absent corpus made every citation "absent", i.e. Low, i.e. dropped by
        # the scoring gate - reporting a pipeline-ordering mistake as bad evidence.
        run_dir = self.make_run("LLY")
        (run_dir / "chunks.jsonl").unlink()
        asyncio.run(fc.run(run_dir))
        findings = json.loads((run_dir / "findings.json").read_text(encoding="utf-8"))
        report = json.loads((run_dir / "fact_check_report.json").read_text(encoding="utf-8"))
        rows, reasonings = self.rows_by_id(report), self.by_id(findings)
        for finding_id in ("F-001", "F-002", "F-004"):
            self.assertEqual(rows[finding_id]["checks"]["quote"], "corpus_missing")
            self.assertEqual(reasonings[finding_id]["confidence"], "Medium")
            self.assertIn("could not be re-opened",
                          reasonings[finding_id]["confidence_reason"])
        # F-003's Low is independent of the corpus: facts.jsonl says 65,179 is FY2025, so
        # the wrong-period extraction is still caught and must not be softened.
        self.assertEqual(reasonings["F-003"]["confidence"], "Low")
        self.assertEqual(rows["F-003"]["checks"]["tie"], "mismatch")
        self.assertNotIn("could not be re-opened", reasonings["F-003"]["confidence_reason"])
        self.assertTrue(any("chunks.jsonl" in w for w in report["corpus_warnings"]))

    def test_a_missing_facts_file_is_named_in_the_report(self):
        run_dir = self.make_run("LLY")
        (run_dir / "facts.jsonl").unlink()
        asyncio.run(fc.run(run_dir))
        report = json.loads((run_dir / "fact_check_report.json").read_text(encoding="utf-8"))
        self.assertTrue(any("facts.jsonl" in w for w in report["corpus_warnings"]))

    def test_a_citation_to_a_chunk_that_is_absent_from_a_loaded_corpus_is_still_low(self):
        findings = copy.deepcopy(FINDINGS["LLY"][:1])
        findings[0]["reasonings"][0]["citations"] = [
            {"chunk_id": "LLY-10K-FY2024-p99-c99", "quote": "Revenue"}]
        out, _report = self.check("LLY", findings)
        self.assertEqual(out[0]["reasonings"][0]["confidence"], "Low")


class MalformedCorpusRegressionTests(FactCheckerTestCase):
    def test_a_malformed_chunk_row_does_not_take_the_run_down(self):
        # Contract 1b requires `text`, but this stage must report, not raise.
        broken = {"chunk_id": "LLY-10K-FY2024-p41-c03", "doc_id": "LLY-10K-FY2024",
                  "page": 41, "is_table": True}
        run_dir = self.make_run("LLY")
        (run_dir / "chunks.jsonl").write_text(
            json.dumps(broken) + "\nthis line is not json\n", encoding="utf-8")
        asyncio.run(fc.run(run_dir))
        findings = json.loads((run_dir / "findings.json").read_text(encoding="utf-8"))
        report = json.loads((run_dir / "fact_check_report.json").read_text(encoding="utf-8"))
        self.assertEqual(self.by_id(findings)["F-001"]["confidence"], "Low")
        self.assertTrue(any("not valid JSON" in w for w in report["corpus_warnings"]))

    def test_a_chunk_without_a_page_still_reaches_the_judge(self):
        chunk = dict(next(c for c in CORPUS["LLY"]["chunks"]
                          if c["chunk_id"] == "LLY-10K-FY2024-p41-c03"))
        chunk.pop("page")
        chunk.pop("doc_id")
        client = JudgeClient()
        findings = copy.deepcopy(FINDINGS["LLY"][:1])
        run_dir = self.make_run("LLY", findings)
        (run_dir / "chunks.jsonl").write_text(json.dumps(chunk) + "\n", encoding="utf-8")
        llm._client = client
        asyncio.run(fc.run(run_dir))
        self.assertEqual(len(client.requests), 1)
        out = json.loads((run_dir / "findings.json").read_text(encoding="utf-8"))
        self.assertIn(out[0]["reasonings"][0]["confidence"], ("High", "Medium"))


class CompositeFindingTests(FactCheckerTestCase):
    """Dedup's own output shape: one anchor, several merged reasonings."""

    def composite(self):
        finding = copy.deepcopy(FINDINGS["LLY"][0])
        finding["contested"] = True
        lead = finding["reasonings"][0]
        lead["flags"] = ["contested"]
        second = copy.deepcopy(lead)
        second.update({
            "reasoning_id": "F-001-b", "hunter": "footnote_miner",
            "hunters": ["footnote_miner"],
            "claim": "FY2024 revenue of 45,042.7 is restated as 45,043 a year later.",
            "citations": [{"chunk_id": "LLY-10K-FY2024-p99-c99", "quote": "Revenue"}],
        })
        finding["reasonings"] = [lead, second]
        return [finding]

    def test_every_reasoning_of_a_composite_finding_is_verified(self):
        client = JudgeClient()
        findings, report = self.check("LLY", self.composite(), client=client)
        reasonings = findings[0]["reasonings"]
        self.assertEqual(report["reasonings"], 2)
        self.assertEqual(report["findings"], 1)
        self.assertEqual(reasonings[0]["confidence"], "High")
        self.assertEqual(reasonings[1]["confidence"], "Low")
        self.assertEqual([row["reasoning_id"] for row in report["reasoning_checks"]],
                         ["F-001-a", "F-001-b"])

    def test_a_composite_finding_keeps_dedups_flags_on_every_reasoning(self):
        findings, _report = self.check("LLY", self.composite())
        for reasoning in findings[0]["reasonings"]:
            self.assertIn("contested", reasoning["flags"])

    def test_only_the_judged_reasoning_costs_an_api_call(self):
        client = JudgeClient()
        _findings, report = self.check("LLY", self.composite(), client=client)
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(report["reasonings_judged"], 1)
        with llm.LOG_PATH.open(encoding="utf-8") as fh:
            rows = [line for line in fh.read().splitlines()[1:] if line.strip()]
        self.assertEqual(len(rows), report["reasonings_judged"])


class JudgeUnavailableRegressionTests(FactCheckerTestCase):
    def test_no_credentials_caps_at_medium_instead_of_raising(self):
        # The acceptance criterion is that a key-less run completes with every reasoning
        # capped at Medium. Constructing the client with no credentials raises TypeError,
        # not an anthropic error, so catching only AnthropicError let it escape and the
        # stage died with every confidence still null. FailingClient hid this by injecting
        # an APIConnectionError, which never exercises client construction.
        def no_credentials(**_request):
            raise TypeError("Could not resolve authentication method")

        llm._client = NS(beta=NS(messages=NS(stream=no_credentials)))
        findings, report = self.check("NVO")
        self.assertEqual({f["reasonings"][0]["confidence"] for f in findings}, {"Medium"})
        self.assertIn("judge unavailable", self.by_id(findings)["F-001"]["confidence_reason"])
        self.assertEqual(report["confidence_counts"]["High"], 0)

    def test_an_os_error_reaching_the_api_caps_at_medium(self):
        def unreachable(**_request):
            raise OSError("network is unreachable")

        llm._client = NS(beta=NS(messages=NS(stream=unreachable)))
        findings, _report = self.check("LLY")
        self.assertNotIn("High", {f["reasonings"][0]["confidence"] for f in findings})


class UnitAgreementRegressionTests(FactCheckerTestCase):
    def test_a_blank_unit_is_not_agreement(self):
        # Before: "" == "" satisfied the unit and currency gate, so a tie was reported
        # "reproduced" with neither actually agreed - the spec requires agreement on both.
        anchor = {"metric": "revenue", "fiscal_period": "FY2024", "value": 45043.0,
                  "unit": None, "currency": None}
        facts = [{"fact_id": "X-1", "doc_id": "X", "metric": "revenue",
                  "fiscal_period": "FY2024", "value": 45043.0, "unit": None,
                  "currency": None}]
        status, fact_id, detail = fc._tie(anchor, facts)
        self.assertEqual(status, "unavailable")
        self.assertEqual(fact_id, "X-1")
        self.assertIn("unit or currency is missing", detail)

    def test_a_missing_unit_on_one_side_alone_is_a_mismatch(self):
        anchor = {"metric": "revenue", "fiscal_period": "FY2024", "value": 45043.0,
                  "unit": "millions", "currency": "USD"}
        facts = [{"fact_id": "X-1", "doc_id": "X", "metric": "revenue",
                  "fiscal_period": "FY2024", "value": 45043.0, "unit": None,
                  "currency": "USD"}]
        self.assertEqual(fc._tie(anchor, facts)[0], "mismatch")

    def test_a_blank_unit_cannot_reach_high(self):
        findings = copy.deepcopy(FINDINGS["LLY"][:1])
        findings[0]["anchor"]["unit"] = None
        rows = [dict(f, unit=None) for f in CORPUS["LLY"]["facts"]]
        out, report = self.check("LLY", findings, fact_rows=rows)
        self.assertEqual(out[0]["reasonings"][0]["confidence"], "Medium")
        self.assertEqual(self.rows_by_id(report)["F-001"]["checks"]["tie"], "unavailable")

    def test_the_real_corpora_still_tie(self):
        # The guard must not cost the documented ties, where both sides state millions.
        for ticker, finding_id in (("LLY", "F-001"), ("NVO", "F-002")):
            findings, _report = self.check(ticker)
            self.assertEqual(self.by_id(findings)[finding_id]["confidence"], "High")


class WriteAndConcurrencyTests(FactCheckerTestCase):
    def test_a_failed_write_leaves_the_previous_findings_intact(self):
        # findings.json is the only copy of what the hunters and Dedup produced, so it is
        # replaced, never truncated in place.
        run_dir = self.make_run("LLY")
        path = run_dir / "findings.json"
        original = path.read_text(encoding="utf-8")
        with self.assertRaises(TypeError):
            fc._write_json(path, {"not": {1, 2, 3}})
        self.assertEqual(path.read_text(encoding="utf-8"), original)
        self.assertFalse(list(run_dir.glob("findings.json.tmp")))

    def test_no_temporary_file_survives_a_run(self):
        run_dir = self.make_run("LLY")
        asyncio.run(fc.run(run_dir))
        self.assertEqual([p.name for p in run_dir.glob("*.tmp")], [])

    def test_judge_calls_respect_the_concurrency_cap(self):
        class Counting(JudgeClient):
            def __init__(self):
                super().__init__()
                self.live, self.peak = 0, 0

            def _stream(self, **request):
                self.requests.append(request)
                client = self

                class Tracked(FakeStream):
                    async def __aenter__(self):
                        client.live += 1
                        client.peak = max(client.peak, client.live)
                        await asyncio.sleep(0)
                        return self

                    async def __aexit__(self, *exc):
                        client.live -= 1
                        return False

                return Tracked(self.default)

        finding = copy.deepcopy(FINDINGS["LLY"][0])
        reasonings = []
        for n in range(10):
            reasoning = copy.deepcopy(finding["reasonings"][0])
            reasoning["reasoning_id"] = f"F-001-{chr(ord('a') + n)}"
            reasonings.append(reasoning)
        finding["reasonings"] = reasonings
        client = Counting()
        _findings, report = self.check("LLY", [finding], client=client)
        self.assertEqual(report["reasonings"], 10)
        self.assertEqual(len(client.requests), 10)
        self.assertLessEqual(client.peak, fc.JUDGE_CONCURRENCY)

    def test_the_first_judge_call_is_made_alone_so_the_prefix_is_cached(self):
        order = []

        class Ordered(JudgeClient):
            def _stream(self, **request):
                self.requests.append(request)
                order.append(len(self.requests))
                return FakeStream(self.default)

        finding = copy.deepcopy(FINDINGS["LLY"][0])
        finding["reasonings"] = [copy.deepcopy(finding["reasonings"][0]) for _ in range(3)]
        for n, reasoning in enumerate(finding["reasonings"]):
            reasoning["reasoning_id"] = f"F-001-{chr(ord('a') + n)}"
        self.check("LLY", [finding], client=Ordered())
        self.assertEqual(order, [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
