"""Offline tests for pharmer/quant.py - the formula whitelist, input resolution and its
provenance, FX conversion, the EPS denominator, Contract 2 ownership, and the audit trail
from evidence through to Partner B's ranking. No API calls and no network: the model is a
fake client and ECB rates come from a fixture series.

Every figure is real. Lilly tags diluted shares and EPS quarterly
(`WeightedAverageNumberOfDilutedSharesOutstanding` 893.7m, `EarningsPerShareDiluted` 7.94
for Q2 2026); Novo tags its diluted count as `AdjustedWeightedAverageShares` and only
annually, so a Novo quarterly impact has no period-matched denominator - a limitation these
tests pin rather than paper over.

Run: .venv/bin/python -m unittest tests.test_quant
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

from pharmer import llm, quant, scoring
from tests.test_llm import FakeStream, fake_message

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CORPUS = json.loads((FIXTURES / "quant_corpus.json").read_text(encoding="utf-8"))

# ECB reference rates for 2026-06-29/30, as published (DKK and USD per EUR).
ECB_SERIES = {"DKK": {"2026-06-29": 7.4600, "2026-06-30": 7.4744},
              "USD": {"2026-06-29": 1.1600, "2026-06-30": 1.1605}}
# 7.4744 / 1.1605
DKK_PER_USD_END = 7.4744 / 1.1605


def fake_ecb(currencies, start, end):
    return {c: dict(days) for c, days in ECB_SERIES.items() if c in currencies}


def no_ecb(currencies, start, end):
    return {}


def fact_input(name, fact_id, period_note=None):
    return {"name": name, "kind": "fact", "value": None, "fact_id": fact_id,
            "chunk_id": None, "quote": None, "justification": None,
            "period_note": period_note}


def quote_input(name, chunk_id, quote, period_note=None):
    return {"name": name, "kind": "quote", "value": None, "fact_id": None,
            "chunk_id": chunk_id, "quote": quote, "justification": None,
            "period_note": period_note}


def assumption(name, value, justification="conventional for this situation"):
    return {"name": name, "kind": "assumption", "value": value, "fact_id": None,
            "chunk_id": None, "quote": None, "justification": justification,
            "period_note": None}


def reply(formula="receivables * write_down_rate", inputs=(), basis="balance",
          quantifiable=True, reason="", mechanism="a stated mechanism",
          direction="cost", stop_reason="end_turn"):
    payload = {"quantifiable": quantifiable, "reason": reason, "formula": formula,
               "direction": direction, "basis": basis, "mechanism": mechanism,
               "inputs": list(inputs)}
    return fake_message(stop_reason, [NS(type="text", text=json.dumps(payload))])


class FormulaClient:
    """Answers each formula request from a marker in its user message."""

    def __init__(self, replies=None, default=None):
        self.replies = replies or {}
        self.default = default
        self.requests = []
        self.beta = NS(messages=NS(stream=self._stream))

    def _stream(self, **request):
        self.requests.append(request)
        content = request["messages"][0]["content"]
        for marker, message in self.replies.items():
            if marker in content:
                return FakeStream(message)
        if self.default is None:
            raise AssertionError(f"no scripted reply for:\n{content[:400]}")
        return FakeStream(self.default)


class RaisingClient(FormulaClient):
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


# --------------------------------------------------------------------------
# findings


def finding(ticker, metric, period, value, currency, citations, confidence="High",
            finding_id="F-001", hunter="channel_checker", claim="a costly reading"):
    return {
        "finding_id": finding_id, "ticker": ticker,
        "anchor": {"metric": metric, "entity": "E", "fiscal_period": period,
                   "value": value, "unit": "millions", "currency": currency},
        "is_label_finding": False, "contested": False,
        "lead_reasoning_id": f"{finding_id}-a",
        "reasonings": [{
            "reasoning_id": f"{finding_id}-a", "hunter": hunter, "hunters": [hunter],
            "claim": claim, "citations": list(citations),
            "confidence": confidence, "confidence_reason": "verified", "flags": [],
            "trend": [], "impact": None, "priced_in": None, "score": None,
        }],
        "peer": {"status": None, "note": ""}, "status": "candidate", "recommended": False,
    }


def lly_finding(**kw):
    """LLY FY2025 receivables - a period with diluted shares and EPS in the corpus."""
    return finding("LLY", "trade_receivables", "FY2025", 17760.0, "USD",
                   [{"chunk_id": "LLY-10K-FY2025-p43-c03", "quote": "Revenue"}], **kw)


def nvo_finding(**kw):
    """NVO Q2 2026 receivables - a quarter with no period-matched denominator."""
    return finding("NVO", "trade_receivables", "Q2 2026", 88949.0, "DKK",
                   [{"chunk_id": "NVO-6K-2026Q2-p28-c02",
                     "quote": "Trade receivables 88,949 70,856"}], **kw)


LLY_WRITEDOWN = [fact_input("receivables", "LLY-xbrl-trade_receivables-FY2025"),
                 assumption("write_down_rate", 0.1,
                            "a tenth of the balance, the usual first-pass haircut")]
NVO_WRITEDOWN = [quote_input("receivables_now", "NVO-6K-2026Q2-p28-c02",
                             "Trade receivables 88,949"),
                 fact_input("baseline", "NVO-xbrl-trade_receivables-FY2025",
                            period_note="FY2025 year-end balance as the baseline"),
                 assumption("write_down_rate", 0.1, "first-pass haircut")]


class QuantTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orig_log, self.orig_client = llm.LOG_PATH, llm._client
        llm.LOG_PATH = Path(self.tmp.name) / "logs" / "api_usage.csv"
        llm._client = FormulaClient()
        self.orig_fetch = quant._ecb_series
        quant._ecb_series = fake_ecb
        self.addCleanup(self._restore)

    def _restore(self):
        llm.LOG_PATH, llm._client = self.orig_log, self.orig_client
        quant._ecb_series = self.orig_fetch
        self.tmp.cleanup()

    def make_run(self, ticker, findings, facts=None):
        corpus = CORPUS[ticker]
        run_dir = Path(self.tmp.name) / f"{ticker}-20261018-0230"
        run_dir.mkdir()
        (run_dir / "manifest.json").write_text(json.dumps(corpus["manifest"]), encoding="utf-8")
        (run_dir / "chunks.jsonl").write_text(
            "".join(json.dumps(c) + "\n" for c in corpus["chunks"]), encoding="utf-8")
        rows = corpus["facts"] if facts is None else facts
        (run_dir / "facts.jsonl").write_text(
            "".join(json.dumps(f) + "\n" for f in rows), encoding="utf-8")
        (run_dir / "findings.json").write_text(
            json.dumps(copy.deepcopy(findings), indent=2), encoding="utf-8")
        return run_dir

    def quantify(self, ticker, findings, client=None, facts=None):
        if client is not None:
            llm._client = client
        run_dir = self.make_run(ticker, findings, facts)
        asyncio.run(quant.run(run_dir))
        return (json.loads((run_dir / "findings.json").read_text(encoding="utf-8")),
                json.loads((run_dir / "quant_report.json").read_text(encoding="utf-8")),
                run_dir)

    @staticmethod
    def rows_by_id(report):
        return {r["reasoning_id"]: r for r in report["reasoning_quant"]}


# --------------------------------------------------------------------------
# the formula whitelist and reproducible arithmetic


class FormulaTests(unittest.TestCase):
    def test_arithmetic_is_reproducible_by_hand(self):
        self.assertAlmostEqual(quant.evaluate("a * b", {"a": 17760.0, "b": 0.1}), 1776.0)
        self.assertAlmostEqual(
            quant.evaluate("-(now - baseline) * rate",
                           {"now": 88949.0, "baseline": 70856.0, "rate": 0.1}), -1809.3)
        self.assertAlmostEqual(
            quant.evaluate("revenue * margin * (1 - tax)",
                           {"revenue": 1000.0, "margin": 0.8, "tax": 0.21}), 632.0)

    def test_anything_that_is_not_arithmetic_is_refused(self):
        for formula in ("__import__('os').system('rm -rf /')", "open('/etc/passwd').read()",
                        "a.__class__", "a if a else a", "[a]", "a ** 500",
                        "(lambda: a)()"):
            with self.assertRaises(ValueError, msg=formula):
                quant.evaluate(formula, {"a": 2.0})

    def test_an_undefined_name_is_refused_rather_than_defaulted(self):
        with self.assertRaises(ValueError) as caught:
            quant.evaluate("receivables * rate", {"receivables": 1.0})
        self.assertIn("undefined input 'rate'", str(caught.exception))

    def test_division_by_zero_and_non_finite_results_are_refused(self):
        with self.assertRaises(ValueError):
            quant.evaluate("a / b", {"a": 1.0, "b": 0.0})
        with self.assertRaises(ValueError):
            quant.evaluate("a * b", {"a": 1e308, "b": 1e308})


# --------------------------------------------------------------------------
# input resolution and provenance


class InputTests(QuantTestCase):
    def setUp(self):
        super().setUp()
        self.facts_by_id, self.facts, self.chunks = quant.load_corpus(
            self.make_run("NVO", [nvo_finding()]))
        self.anchor = nvo_finding()["anchor"]

    def resolve(self, inputs, anchor=None):
        return quant.resolve_inputs(inputs, anchor or self.anchor, self.facts_by_id,
                                    self.chunks)

    def test_a_fact_input_takes_the_corpus_value_not_the_models(self):
        item = fact_input("baseline", "NVO-xbrl-trade_receivables-FY2025",
                          period_note="baseline")
        item["value"] = 999999.0          # the model's number must be ignored
        values, provenance, problems = self.resolve([item])
        self.assertEqual(problems, [])
        self.assertEqual(values["baseline"], 70856)
        self.assertEqual(provenance[0]["source"], "NVO-xbrl-trade_receivables-FY2025")
        self.assertEqual(provenance[0]["fiscal_period"], "FY2025")

    def test_a_fact_from_another_period_needs_a_period_note(self):
        bare = fact_input("baseline", "NVO-xbrl-trade_receivables-FY2025")
        _values, _prov, problems = self.resolve([bare])
        self.assertTrue(any("no period_note explains it" in p for p in problems))
        noted = fact_input("baseline", "NVO-xbrl-trade_receivables-FY2025",
                           period_note="prior year-end baseline")
        self.assertEqual(self.resolve([noted])[2], [])

    def test_an_unknown_fact_id_is_a_problem_never_a_default(self):
        _v, _p, problems = self.resolve([fact_input("x", "NVO-xbrl-nope-FY2025")])
        self.assertTrue(any("is not in facts.jsonl" in p for p in problems))

    def test_a_quote_input_must_be_verbatim_and_carry_one_figure(self):
        good = quote_input("now", "NVO-6K-2026Q2-p28-c02", "Trade receivables 88,949")
        values, provenance, problems = self.resolve([good])
        self.assertEqual(problems, [])
        self.assertEqual(values["now"], 88949.0)
        self.assertEqual(provenance[0]["page"], 28)
        # a two-column row carries two figures, so which one is meant is ambiguous
        two = quote_input("now", "NVO-6K-2026Q2-p28-c02", "Trade receivables 88,949 70,856")
        self.assertTrue(any("exactly one figure" in p for p in self.resolve([two])[2]))
        # and a quote that is not in the chunk is refused
        absent = quote_input("now", "NVO-6K-2026Q2-p28-c02", "Trade receivables 11,111")
        self.assertTrue(any("not in" in p for p in self.resolve([absent])[2]))

    def test_an_assumption_without_a_justification_is_refused(self):
        bare = assumption("rate", 0.1, "")
        self.assertTrue(any("no justification" in p for p in self.resolve([bare])[2]))
        good = assumption("rate", 0.1, "a tenth of the balance")
        values, provenance, problems = self.resolve([good])
        self.assertEqual((problems, values["rate"]), ([], 0.1))
        self.assertEqual(provenance[0]["source"], "assumption")
        self.assertIn("justification", provenance[0])

    def test_malformed_input_names_and_duplicates_are_refused(self):
        for name in ("Receivables", "2receivables", "", "rate; drop"):
            item = assumption(name, 0.1)
            self.assertTrue(any("plain identifier" in p for p in self.resolve([item])[2]),
                            msg=name)
        twice = [assumption("rate", 0.1), assumption("rate", 0.2)]
        self.assertTrue(any("defined twice" in p for p in self.resolve(twice)[2]))

    def test_an_unknown_kind_and_an_empty_list_are_problems(self):
        odd = dict(assumption("rate", 0.1), kind="vibes")
        self.assertTrue(any("unknown input kind" in p for p in self.resolve([odd])[2]))
        self.assertTrue(self.resolve([])[2])


# --------------------------------------------------------------------------
# FX


class FxTests(QuantTestCase):
    def path(self):
        return Path(self.tmp.name)

    def test_a_balance_converts_at_the_period_end_rate(self):
        rate, detail = quant.fx_rate(self.path(), "DKK", "2026-04-01", "2026-06-30",
                                     "balance", fetch=fake_ecb)
        self.assertAlmostEqual(rate, DKK_PER_USD_END, places=6)
        self.assertEqual((detail["basis"], detail["date"]), ("period_end", "2026-06-30"))
        self.assertEqual(detail["source"], "ECB")
        self.assertEqual(detail["pair"], "DKK/USD")
        self.assertIn("USD = DKK / rate", detail["direction"])

    def test_a_flow_converts_at_the_period_average_rate(self):
        rate, detail = quant.fx_rate(self.path(), "DKK", "2026-04-01", "2026-06-30",
                                     "flow", fetch=fake_ecb)
        expected = ((7.4600 / 1.1600) + (7.4744 / 1.1605)) / 2
        self.assertAlmostEqual(rate, expected, places=6)
        self.assertEqual(detail["basis"], "period_average")
        self.assertEqual(detail["observations"], 2)

    def test_a_period_end_on_a_closed_day_looks_back(self):
        series = {"DKK": {"2026-06-26": 7.47}, "USD": {"2026-06-26": 1.16}}
        rate, detail = quant.fx_rate(self.path(), "DKK", "2026-04-01", "2026-06-28",
                                     "balance", fetch=lambda c, s, e: series)
        self.assertAlmostEqual(rate, 7.47 / 1.16, places=6)
        self.assertEqual(detail["date"], "2026-06-26")

    def test_an_unavailable_rate_yields_none_never_an_invented_number(self):
        rate, detail = quant.fx_rate(self.path(), "DKK", "2026-04-01", "2026-06-30",
                                     "balance", fetch=no_ecb)
        self.assertIsNone(rate)
        self.assertIsNone(detail["rate"])
        self.assertIn("no ECB reference rate", detail["note"])

    def test_usd_needs_no_conversion(self):
        rate, detail = quant.fx_rate(self.path(), "USD", "2025-01-01", "2025-12-31",
                                     "balance", fetch=no_ecb)
        self.assertEqual(rate, 1.0)
        self.assertEqual(detail["pair"], "USD/USD")

    def test_rates_are_cached_so_a_rerun_needs_no_network(self):
        run_dir = Path(self.tmp.name) / "cache-run"
        run_dir.mkdir()
        first, _ = quant.fx_rate(run_dir, "DKK", "2026-04-01", "2026-06-30", "balance",
                                 fetch=fake_ecb)
        self.assertTrue((run_dir / quant.FX_CACHE).exists())
        again, detail = quant.fx_rate(run_dir, "DKK", "2026-04-01", "2026-06-30", "balance",
                                      fetch=no_ecb)      # network now refuses
        self.assertEqual(first, again)
        self.assertEqual(detail["source"], "ECB")


# --------------------------------------------------------------------------
# the EPS denominator


class EpsTests(unittest.TestCase):
    LLY = CORPUS["LLY"]["facts"]
    NVO = CORPUS["NVO"]["facts"]

    def test_a_matched_annual_period_resolves(self):
        anchor = {"fiscal_period": "FY2025", "currency": "USD"}
        eps, shares, detail = quant.eps_denominator(anchor, self.LLY)
        self.assertEqual((eps["value"], shares["value"]), (22.95, 899.3))
        self.assertIn("LLY-xbrl-diluted_eps-FY2025", detail)

    def test_a_matched_quarter_resolves_for_a_filer_that_tags_quarterly(self):
        anchor = {"fiscal_period": "Q2 2026", "currency": "USD"}
        eps, shares, _detail = quant.eps_denominator(anchor, self.LLY)
        self.assertEqual((eps["value"], shares["value"]), (7.94, 893.7))

    def test_an_annual_only_filer_has_no_quarterly_denominator(self):
        # The limitation, stated: Novo tags shares and EPS annually only, so no annual
        # count is quietly substituted for a quarter.
        anchor = {"fiscal_period": "Q2 2026", "currency": "DKK"}
        eps, shares, detail = quant.eps_denominator(anchor, self.NVO)
        self.assertIsNone(eps)
        self.assertIsNone(shares)
        self.assertIn("no period-matched", detail)
        self.assertIn("Q2 2026", detail)

    def test_the_annual_novo_period_does_resolve(self):
        anchor = {"fiscal_period": "FY2025", "currency": "DKK"}
        eps, shares, _detail = quant.eps_denominator(anchor, self.NVO)
        self.assertEqual((eps["value"], shares["value"]), (23.03, 4447.7))

    def test_a_currency_mismatch_is_refused(self):
        anchor = {"fiscal_period": "FY2025", "currency": "USD"}   # impact in USD
        eps, _shares, detail = quant.eps_denominator(anchor, self.NVO)   # EPS in DKK
        self.assertIsNone(eps)
        self.assertIn("DKK", detail)

    def test_the_percentage_reconciles_by_hand(self):
        eps = {"value": 22.95, "currency": "USD", "fact_id": "e"}
        shares = {"value": 899.3, "fact_id": "s"}
        # -1,776m USD over 899.3m shares is -1.9749 USD/share against EPS of 22.95
        self.assertAlmostEqual(quant.eps_percent(-1776.0, eps, shares),
                               (-1776.0 / 899.3) / 22.95 * 100, places=9)
        self.assertAlmostEqual(quant.eps_percent(-1776.0, eps, shares), -8.605, places=3)

    def test_a_zero_eps_is_refused_rather_than_dividing_by_zero(self):
        facts = [dict(f) for f in self.LLY]
        for f in facts:
            if f["metric"] == "diluted_eps" and f["fiscal_period"] == "FY2025":
                f["value"] = 0
        eps, _s, detail = quant.eps_denominator({"fiscal_period": "FY2025",
                                                 "currency": "USD"}, facts)
        self.assertIsNone(eps)
        self.assertIn("zero", detail)


# --------------------------------------------------------------------------
# period bounds


class PeriodTests(unittest.TestCase):
    def test_labels_become_date_ranges(self):
        self.assertEqual(quant.period_bounds("FY2025"), ("2025-01-01", "2025-12-31"))
        self.assertEqual(quant.period_bounds("Q2 2026"), ("2026-04-01", "2026-06-30"))
        self.assertEqual(quant.period_bounds("Q4 2025"), ("2025-10-01", "2025-12-31"))
        self.assertEqual(quant.period_bounds("H1 2026"), ("2026-01-01", "2026-06-30"))
        self.assertEqual(quant.period_bounds("H2 2025"), ("2025-07-01", "2025-12-31"))

    def test_a_matched_period_end_overrides_the_calendar_quarter(self):
        # Pfizer and J&J close quarters on a 52/53-week calendar.
        self.assertEqual(quant.period_bounds("Q2 2026", "2026-06-28"),
                         ("2026-04-01", "2026-06-28"))

    def test_an_unlabelable_period_is_none(self):
        for label in ("", "nonsense", "Q5 2026", "FYxx"):
            self.assertIsNone(quant.period_bounds(label), msg=label)


# --------------------------------------------------------------------------
# the stage, end to end


class StageTests(QuantTestCase):
    def test_a_usd_finding_is_quantified_with_usd_and_eps(self):
        client = FormulaClient(default=reply(
            formula="receivables * write_down_rate", inputs=LLY_WRITEDOWN))
        findings, report, _run = self.quantify("LLY", [lly_finding()], client)
        impact = findings[0]["reasonings"][0]["impact"]
        self.assertAlmostEqual(impact["usd_m"], -1776.0, places=1)
        self.assertAlmostEqual(impact["eps_pct"], -8.61, places=2)
        self.assertEqual(impact["formula"], "-(receivables * write_down_rate)")
        self.assertEqual(impact["inputs"], {"receivables": 17760.0, "write_down_rate": 0.1})
        self.assertEqual(impact["fx"]["pair"], "USD/USD")
        self.assertEqual(report["quantified"], 1)
        self.assertEqual((report["with_usd"], report["with_eps_pct"]), (1, 1))

    def test_a_dkk_finding_converts_to_usd_at_the_ecb_rate(self):
        client = FormulaClient(default=reply(
            formula="(receivables_now - baseline) * write_down_rate",
            inputs=NVO_WRITEDOWN))
        findings, report, _run = self.quantify("NVO", [nvo_finding()], client)
        impact = findings[0]["reasonings"][0]["impact"]
        row = self.rows_by_id(report)["F-001-a"]
        # -(88,949 - 70,856) * 0.1 = -1,809.3 DKKm
        self.assertAlmostEqual(row["native_m"], -1809.3, places=1)
        self.assertEqual(row["native_currency"], "DKK")
        self.assertAlmostEqual(impact["usd_m"], round(-1809.3 / DKK_PER_USD_END, 1), places=1)
        self.assertEqual(impact["fx"]["basis"], "period_end")
        self.assertEqual(impact["fx"]["source"], "ECB")

    def test_a_novo_quarter_gets_no_eps_and_says_why(self):
        client = FormulaClient(default=reply(
            formula="(receivables_now - baseline) * write_down_rate",
            inputs=NVO_WRITEDOWN))
        findings, report, _run = self.quantify("NVO", [nvo_finding()], client)
        impact = findings[0]["reasonings"][0]["impact"]
        row = self.rows_by_id(report)["F-001-a"]
        self.assertIsNotNone(impact["usd_m"])
        self.assertIsNone(impact["eps_pct"])
        self.assertIn("no period-matched", row["eps_basis"])
        self.assertTrue(any("eps_pct unavailable" in p for p in row["problems"]))
        self.assertEqual(report["with_eps_pct"], 0)

    def test_eps_survives_an_fx_outage_because_it_needs_no_rate(self):
        quant._ecb_series = no_ecb
        client = FormulaClient(default=reply(
            formula="receivables * write_down_rate", inputs=[
                fact_input("receivables", "NVO-xbrl-trade_receivables-FY2025"),
                assumption("write_down_rate", 0.1, "first-pass haircut")]))
        nvo_annual = finding("NVO", "trade_receivables", "FY2025", 70856.0, "DKK",
                             [{"chunk_id": "NVO-6K-2026Q2-p28-c02",
                               "quote": "Trade receivables 88,949 70,856"}])
        findings, report, _run = self.quantify("NVO", [nvo_annual], client)
        impact = findings[0]["reasonings"][0]["impact"]
        self.assertIsNone(impact["usd_m"])                 # no rate, so no dollars
        self.assertIsNotNone(impact["eps_pct"])            # but the ratio still holds
        self.assertAlmostEqual(impact["eps_pct"],
                               ((-7085.6 / 4447.7) / 23.03) * 100, places=2)
        self.assertTrue(any("no FX rate" in p
                            for p in self.rows_by_id(report)["F-001-a"]["problems"]))

    def test_a_low_confidence_finding_is_skipped_without_a_call(self):
        client = FormulaClient()      # any request would raise AssertionError
        findings, report, _run = self.quantify(
            "LLY", [lly_finding(confidence="Low")], client)
        self.assertIsNone(findings[0]["reasonings"][0]["impact"])
        self.assertEqual(client.requests, [])
        self.assertEqual(report["quantified"], 0)
        self.assertIn("fails the scoring gate", report["skipped"][0]["reason"])

    def test_a_null_confidence_finding_is_skipped_too(self):
        client = FormulaClient()
        findings, _report, _run = self.quantify(
            "LLY", [lly_finding(confidence=None)], client)
        self.assertIsNone(findings[0]["reasonings"][0]["impact"])
        self.assertEqual(client.requests, [])

    def test_a_medium_confidence_finding_is_quantified(self):
        client = FormulaClient(default=reply(
            formula="receivables * write_down_rate", inputs=LLY_WRITEDOWN))
        findings, _report, _run = self.quantify(
            "LLY", [lly_finding(confidence="Medium")], client)
        self.assertIsNotNone(findings[0]["reasonings"][0]["impact"])

    def test_a_declined_quantification_leaves_null_never_zero(self):
        client = FormulaClient(default=reply(
            quantifiable=False, reason="label breadth has no dollar figure"))
        findings, report, _run = self.quantify("LLY", [lly_finding()], client)
        self.assertIsNone(findings[0]["reasonings"][0]["impact"])
        self.assertIn("declined", report["skipped"][0]["reason"])

    def test_an_unresolvable_input_leaves_null_never_zero(self):
        client = FormulaClient(default=reply(
            formula="receivables * write_down_rate",
            inputs=[fact_input("receivables", "LLY-xbrl-nope-FY2025"),
                    assumption("write_down_rate", 0.1)]))
        findings, report, _run = self.quantify("LLY", [lly_finding()], client)
        self.assertIsNone(findings[0]["reasonings"][0]["impact"])
        row = self.rows_by_id(report)["F-001-a"]
        self.assertIn("did not resolve", row["skipped"])
        self.assertTrue(row["problems"])

    def test_a_formula_that_does_not_evaluate_leaves_null(self):
        client = FormulaClient(default=reply(
            formula="receivables * missing_rate", inputs=LLY_WRITEDOWN))
        findings, report, _run = self.quantify("LLY", [lly_finding()], client)
        self.assertIsNone(findings[0]["reasonings"][0]["impact"])
        self.assertIn("could not be evaluated",
                      self.rows_by_id(report)["F-001-a"]["skipped"])

    def test_the_model_being_unavailable_never_raises(self):
        error = anthropic.APIConnectionError(request=httpx2.Request("POST", "https://x"))
        findings, report, _run = self.quantify("LLY", [lly_finding()],
                                               RaisingClient(error))
        self.assertIsNone(findings[0]["reasonings"][0]["impact"])
        self.assertIn("unavailable", report["skipped"][0]["reason"])

    def test_no_credentials_never_raises(self):
        def no_creds(**_request):
            raise TypeError("Could not resolve authentication method")
        llm._client = NS(beta=NS(messages=NS(stream=no_creds)))
        findings, _report, _run = self.quantify("LLY", [lly_finding()])
        self.assertIsNone(findings[0]["reasonings"][0]["impact"])

    def test_a_malformed_reply_leaves_null(self):
        garbled = fake_message("end_turn", [NS(type="text", text="not json")])
        findings, report, _run = self.quantify("LLY", [lly_finding()],
                                               FormulaClient(default=garbled))
        self.assertIsNone(findings[0]["reasonings"][0]["impact"])
        self.assertIn("not valid JSON", report["skipped"][0]["reason"])

    def test_a_truncated_reply_leaves_null(self):
        findings, _report, _run = self.quantify(
            "LLY", [lly_finding()],
            FormulaClient(default=reply(inputs=LLY_WRITEDOWN, stop_reason="max_tokens")))
        self.assertIsNone(findings[0]["reasonings"][0]["impact"])


# --------------------------------------------------------------------------
# Contract 2 ownership and idempotence


class DirectionTests(QuantTestCase):
    """The sign is applied in Python, not trusted to the formula."""

    def run_with(self, direction, formula="receivables * write_down_rate"):
        client = FormulaClient(default=reply(formula=formula, inputs=LLY_WRITEDOWN,
                                             direction=direction))
        return self.quantify("LLY", [lly_finding()], client)

    def test_a_cost_is_negative_even_though_the_formula_is_positive(self):
        # Asked in prose for "negative for damage", the live model returned a positive
        # write-down on every finding, which would have printed a loss as a gain.
        findings, report, _run = self.run_with("cost")
        impact = findings[0]["reasonings"][0]["impact"]
        row = self.rows_by_id(report)["F-001-a"]
        self.assertEqual(row["magnitude"], 1776.0)        # the model's magnitude
        self.assertEqual(row["native_m"], -1776.0)        # the signed figure
        self.assertLess(impact["usd_m"], 0)
        self.assertLess(impact["eps_pct"], 0)

    def test_a_benefit_stays_positive(self):
        findings, _report, _run = self.run_with("benefit")
        impact = findings[0]["reasonings"][0]["impact"]
        self.assertGreater(impact["usd_m"], 0)
        self.assertGreater(impact["eps_pct"], 0)

    def test_a_formula_that_is_already_negative_is_not_double_negated(self):
        findings, report, _run = self.run_with("cost",
                                               formula="-(receivables * write_down_rate)")
        row = self.rows_by_id(report)["F-001-a"]
        self.assertEqual(row["magnitude"], -1776.0)
        self.assertEqual(row["native_m"], -1776.0)
        self.assertLess(findings[0]["reasonings"][0]["impact"]["usd_m"], 0)

    def test_the_stored_formula_reproduces_the_signed_figure_alone(self):
        # Contract 2's impact keys are fixed, so the sign has to live in `formula` for
        # `formula` + `inputs` to explain `usd_m` without the sidecar.
        findings, report, _run = self.run_with("cost")
        impact = findings[0]["reasonings"][0]["impact"]
        self.assertAlmostEqual(quant.evaluate(impact["formula"], impact["inputs"]),
                               self.rows_by_id(report)["F-001-a"]["native_m"], places=6)
        self.assertEqual(set(impact), {"usd_m", "eps_pct", "formula", "inputs", "fx"})

    def test_a_missing_direction_defaults_to_cost(self):
        payload = reply(inputs=LLY_WRITEDOWN)
        broken = json.loads(payload.content[0].text)
        broken.pop("direction")
        message = fake_message("end_turn", [NS(type="text", text=json.dumps(broken))])
        findings, _report, _run = self.quantify("LLY", [lly_finding()],
                                                FormulaClient(default=message))
        self.assertLess(findings[0]["reasonings"][0]["impact"]["usd_m"], 0)


class OwnershipTests(QuantTestCase):
    def client(self):
        return FormulaClient(default=reply(formula="receivables * write_down_rate",
                                           inputs=LLY_WRITEDOWN))

    def test_only_impact_changes(self):
        before = [lly_finding()]
        after, _report, _run = self.quantify("LLY", copy.deepcopy(before), self.client())
        for old, new in zip(before, after):
            self.assertEqual({k: v for k, v in old.items() if k != "reasonings"},
                             {k: v for k, v in new.items() if k != "reasonings"})
            for old_r, new_r in zip(old["reasonings"], new["reasonings"]):
                self.assertEqual({k: v for k, v in old_r.items() if k != "impact"},
                                 {k: v for k, v in new_r.items() if k != "impact"})

    def test_impact_carries_exactly_the_contract_2_keys(self):
        findings, _report, _run = self.quantify("LLY", [lly_finding()], self.client())
        self.assertEqual(set(findings[0]["reasonings"][0]["impact"]),
                         {"usd_m", "eps_pct", "formula", "inputs", "fx"})

    def test_formula_and_inputs_are_always_filled_when_impact_is_set(self):
        findings, _report, _run = self.quantify("LLY", [lly_finding()], self.client())
        impact = findings[0]["reasonings"][0]["impact"]
        self.assertTrue(impact["formula"])
        self.assertTrue(impact["inputs"])

    def test_a_rerun_is_idempotent(self):
        run_dir = self.make_run("LLY", [lly_finding()])
        llm._client = self.client()
        asyncio.run(quant.run(run_dir))
        once = (run_dir / "findings.json").read_text(encoding="utf-8")
        llm._client = self.client()
        asyncio.run(quant.run(run_dir))
        self.assertEqual(once, (run_dir / "findings.json").read_text(encoding="utf-8"))

    def test_no_temporary_file_survives_a_run(self):
        _f, _r, run_dir = self.quantify("LLY", [lly_finding()], self.client())
        self.assertEqual([p.name for p in run_dir.glob("*.tmp")], [])

    def test_a_composite_finding_quantifies_each_reasoning(self):
        composite = lly_finding()
        second = copy.deepcopy(composite["reasonings"][0])
        second["reasoning_id"] = "F-001-b"
        composite["reasonings"].append(second)
        findings, report, _run = self.quantify("LLY", [composite], self.client())
        self.assertEqual(report["reasonings"], 2)
        self.assertEqual(report["quantified"], 2)
        for reasoning in findings[0]["reasonings"]:
            self.assertIsNotNone(reasoning["impact"])

    def test_a_missing_facts_file_leaves_everything_unquantified(self):
        findings, report, _run = self.quantify("LLY", [lly_finding()], self.client(),
                                               facts=[])
        self.assertIsNone(findings[0]["reasonings"][0]["impact"])
        self.assertEqual(report["quantified"], 0)


# --------------------------------------------------------------------------
# the audit trail, and what Partner B's scoring does with it


class AuditTrailTests(QuantTestCase):
    def quantified(self):
        client = FormulaClient(default=reply(
            formula="receivables * write_down_rate", inputs=LLY_WRITEDOWN))
        return self.quantify("LLY", [lly_finding()], client)

    def test_every_input_names_its_source_and_period(self):
        _findings, report, _run = self.quantified()
        rows = self.rows_by_id(report)["F-001-a"]["inputs"]
        by_name = {r["name"]: r for r in rows}
        self.assertEqual(by_name["receivables"]["source"],
                         "LLY-xbrl-trade_receivables-FY2025")
        self.assertEqual(by_name["receivables"]["fiscal_period"], "FY2025")
        self.assertEqual(by_name["write_down_rate"]["source"], "assumption")
        self.assertTrue(by_name["write_down_rate"]["justification"])

    def test_the_recorded_formula_and_inputs_reproduce_usd_m_by_hand(self):
        findings, report, _run = self.quantified()
        impact = findings[0]["reasonings"][0]["impact"]
        row = self.rows_by_id(report)["F-001-a"]
        # recompute from what the finding itself carries, with no help from the stage
        recomputed = quant.evaluate(impact["formula"], impact["inputs"])
        self.assertAlmostEqual(recomputed, row["native_m"], places=6)
        self.assertAlmostEqual(round(recomputed / 1.0, 1), impact["usd_m"], places=6)

    def test_eps_pct_is_explainable_from_the_recorded_denominator(self):
        findings, report, _run = self.quantified()
        impact = findings[0]["reasonings"][0]["impact"]
        row = self.rows_by_id(report)["F-001-a"]
        self.assertIn("LLY-xbrl-diluted_eps-FY2025", row["eps_basis"])
        self.assertIn("LLY-xbrl-diluted_shares-FY2025", row["eps_basis"])
        native = quant.evaluate(impact["formula"], impact["inputs"])
        self.assertAlmostEqual(round((native / 899.3) / 22.95 * 100, 2), impact["eps_pct"],
                               places=6)

    def test_the_sidecar_records_the_mechanism_and_the_basis(self):
        _findings, report, _run = self.quantified()
        row = self.rows_by_id(report)["F-001-a"]
        self.assertTrue(row["mechanism"])
        self.assertEqual(row["basis"], "balance")
        self.assertEqual(row["confidence"], "High")
        self.assertEqual(report["stage"], "quant")

    def test_scoring_turns_the_impact_into_a_real_materiality_score(self):
        # The point of the stage: before it, materiality() returned None for everything.
        findings, _report, _run = self.quantified()
        reasoning = findings[0]["reasonings"][0]
        reasoning["priced_in"] = {"hits": 0, "evidence": [], "stock_move_pct": None}
        self.assertAlmostEqual(scoring.materiality(reasoning), 8.61, places=2)
        self.assertIsNotNone(scoring.score(reasoning))

    def test_an_unquantified_finding_scores_null_not_zero(self):
        client = FormulaClient(default=reply(quantifiable=False, reason="no dollar figure"))
        findings, _report, _run = self.quantify("LLY", [lly_finding()], client)
        reasoning = findings[0]["reasonings"][0]
        reasoning["priced_in"] = {"hits": 0, "evidence": [], "stock_move_pct": None}
        self.assertIsNone(scoring.materiality(reasoning))
        self.assertIsNone(scoring.score(reasoning))

    def test_a_bigger_impact_outranks_a_smaller_one(self):
        """Financial impact must actually move the ranking, all else held constant."""
        findings = []
        for n, eps_pct in enumerate((-2.0, -9.0), 1):
            f = lly_finding(finding_id=f"F-{n:03d}", hunter=f"hunter_{n}")
            r = f["reasonings"][0]
            r["impact"] = {"usd_m": -100.0, "eps_pct": eps_pct, "formula": "a * b",
                           "inputs": {"a": 1, "b": 1}, "fx": None}
            r["priced_in"] = {"hits": 0, "evidence": [], "stock_move_pct": None}
            findings.append(f)
        scoring.apply(findings)
        scores = {f["finding_id"]: f["reasonings"][0]["score"] for f in findings}
        self.assertGreater(scores["F-002"], scores["F-001"])
        recommended = [f["finding_id"] for f in findings if f["recommended"]]
        self.assertEqual(recommended, ["F-002"])

        # Flip the impacts and the recommendation follows the money, nothing else changed.
        for f, eps_pct in zip(findings, (-9.0, -2.0)):
            f["reasonings"][0]["impact"]["eps_pct"] = eps_pct
        scoring.apply(findings)
        self.assertEqual([f["finding_id"] for f in findings if f["recommended"]], ["F-001"])


if __name__ == "__main__":
    unittest.main()
