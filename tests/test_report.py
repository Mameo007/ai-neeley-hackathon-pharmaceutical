"""Offline tests for pharmer/report.py - the report model (sections, findings, footnotes,
failure states) on a fixture run dir, plus one PDF smoke test. No API calls.

Run: .venv/bin/python -m unittest tests.test_report
"""
import asyncio
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from pharmer import report

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FILES = {"findings.json": "synthesis_findings.json", "run.json": "synthesis_run.json",
         "synthesis.json": "report_synthesis.json", "ceo_move.json": "report_ceo_move.json",
         "manifest.json": "report_manifest.json", "chunks.jsonl": "report_chunks.jsonl"}
SOURCE_P14 = "Novo Nordisk Q2 2026 6-K, p.14"


class ReportTestCase(unittest.TestCase):
    """Temp run dir holding every report input; tests edit or delete files before building."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.tmp.name) / "NVO-20261018-0230"
        self.run_dir.mkdir()
        for name, fixture in FILES.items():
            shutil.copy(FIXTURES / fixture, self.run_dir / name)

    def tearDown(self):
        self.tmp.cleanup()

    def read(self, name):
        return json.loads((self.run_dir / name).read_text(encoding="utf-8"))

    def write(self, name, obj):
        (self.run_dir / name).write_text(json.dumps(obj), encoding="utf-8")

    def edit(self, name, change):
        obj = self.read(name)
        change(obj)
        self.write(name, obj)

    def edit_finding(self, finding_id, change):
        def apply(findings):
            change(next(f for f in findings if f["finding_id"] == finding_id))
        self.edit("findings.json", apply)

    def model(self):
        return report.build_model(self.run_dir)

    def warned(self, model, text):
        return any(text in w for w in model["warnings"])


class HappyPathTests(ReportTestCase):
    def test_sections(self):
        m = self.model()
        self.assertEqual(m["masthead"], "PHARMER RESEARCH")
        self.assertEqual(m["tagline"], "We find the needle in the haystack.")
        self.assertEqual(m["product"], "Illustrative Brand")
        self.assertEqual(m["warnings"], [])
        self.assertEqual(m["thesis"]["needle"], "F-003")
        self.assertEqual(m["thesis"]["needle_note"], report.NEEDLE_HUMAN)
        self.assertEqual(m["thesis"]["headline"], "Illustrative: inventory 333 built up.")
        self.assertIn("222", m["thesis"]["verdict"])
        self.assertEqual(m["thesis"]["priced_in"], "partly priced in")
        self.assertEqual(m["comparison"]["status"], "missing")
        self.assertEqual(m["comparison"]["note"], report.NO_COMPARISON)
        self.assertEqual(m["ceo_move"]["status"], "ok")
        self.assertEqual([label for label, _ in m["ceo_move"]["fields"]],
                         ["Action", "Timeframe", "Trade-off", "Downside if wrong",
                          "Why it works"])

    def test_findings_order_and_badges(self):
        m = self.model()
        # Needle first, then best score: F-001 4.5, F-002 3.25, F-004 1.5, F-005 unscored.
        self.assertEqual([r["finding_id"] for r in m["findings"]],
                         ["F-003", "F-001", "F-002", "F-004", "F-005"])
        self.assertEqual([r["number"] for r in m["findings"]], [1, 2, 3, 4, 5])
        self.assertEqual(m["thesis"]["needle_number"], 1)
        first, second = m["findings"][0], m["findings"][1]
        self.assertEqual(first["labels"], ["Sharpest Needle"])
        self.assertEqual(second["labels"], ["Recommended"])
        self.assertIn("label/product", m["findings"][3]["labels"])
        self.assertEqual(first["reasonings"][0]["badge"], "High")
        self.assertEqual(second["reasonings"][0]["impact"], "EPS impact -4.5%")
        self.assertEqual(second["reasonings"][0]["priced_in"], "0 priced-in hits")
        self.assertEqual(first["anchor"], "Novo Nordisk, inventory, Q2 2026: DKK 333 millions")
        # The unselected sibling F-001-b is not shown.
        self.assertEqual([r["reasoning_id"] for r in second["reasonings"]], ["F-001-a"])

    def test_no_pharming(self):
        text = json.dumps(self.model()).lower()
        self.assertNotIn("pharming", text)
        for name in dir(report):
            value = getattr(report, name)
            if isinstance(value, str):
                self.assertNotIn("pharming", value.lower())

    def test_every_number_comes_from_the_inputs(self):
        inputs = set()
        for name in FILES:
            inputs |= report.syn.numbers_in((self.run_dir / name).read_text(encoding="utf-8"))
        m = self.model()
        texts = [w for w in m["warnings"]] + [m["thesis"]["headline"], m["thesis"]["verdict"]]
        texts += [text for _, text in m["ceo_move"]["fields"]]
        for row in m["findings"]:
            texts.append(row["anchor"])
            for r in row["reasonings"]:
                texts += [r["claim"], r["impact"] or "", r["priced_in"]]
                texts += [q["text"] for q in r["quotes"]]
        texts += [n["text"] for n in m["footnotes"]]
        found = set().union(*(report.syn.numbers_in(t) for t in texts))
        self.assertLessEqual(found, inputs)


class FootnoteTests(ReportTestCase):
    def test_numbered_in_reading_order_and_deduped(self):
        m = self.model()
        # Every fixture citation is p.14: thesis, findings and callout share footnote 1.
        self.assertEqual(m["footnotes"], [{"number": 1, "text": SOURCE_P14}])
        self.assertEqual(m["thesis"]["notes"], [1])
        self.assertEqual(m["ceo_move"]["notes"], [1])
        for row in m["findings"]:
            for r in row["reasonings"]:
                self.assertEqual([q["notes"] for q in r["quotes"]], [[1]])

    def test_same_page_other_chunk_reuses_number_new_page_gets_next(self):
        def cite(chunk_id):
            def change(f):
                f["reasonings"][0]["citations"] = [{"chunk_id": chunk_id, "quote": "Sales 555"}]
            return change
        self.edit_finding("F-001", cite("NVO-6K-2026Q2-p14-c04"))
        self.edit_finding("F-005", cite("NVO-6K-2026Q2-p9-c01"))
        m = self.model()
        self.assertEqual(m["footnotes"], [{"number": 1, "text": SOURCE_P14},
                                          {"number": 2, "text": "Novo Nordisk Q2 2026 6-K, p.9"}])
        by_id = {r["finding_id"]: r for r in m["findings"]}
        self.assertEqual(by_id["F-001"]["reasonings"][0]["quotes"][0]["notes"], [1])
        self.assertEqual(by_id["F-005"]["reasonings"][0]["quotes"][0]["notes"], [2])

    def test_unresolvable_chunk_is_numbered_and_warned(self):
        def change(f):
            f["reasonings"][0]["citations"].append({"chunk_id": "NVO-6K-2026Q2-p99-c01",
                                                    "quote": "missing"})
        self.edit_finding("F-003", change)
        m = self.model()
        self.assertEqual(m["footnotes"][1]["text"],
                         "NVO-6K-2026Q2-p99-c01 – citation not found in this run's corpus")
        self.assertEqual(m["findings"][0]["reasonings"][0]["quotes"][1]["notes"], [2])
        self.assertTrue(self.warned(m, "NVO-6K-2026Q2-p99-c01"))

    def test_missing_corpus_files_leave_every_citation_unresolved(self):
        (self.run_dir / "manifest.json").unlink()
        (self.run_dir / "chunks.jsonl").unlink()
        m = self.model()
        self.assertTrue(self.warned(m, "manifest.json is missing"))
        self.assertTrue(self.warned(m, "chunks.jsonl is missing"))
        self.assertEqual(len(m["footnotes"]), 1)
        self.assertIn("not found in this run's corpus", m["footnotes"][0]["text"])

    def test_corrupt_chunks_line_skipped(self):
        path = self.run_dir / "chunks.jsonl"
        path.write_text("{not json\n" + path.read_text(encoding="utf-8"), encoding="utf-8")
        m = self.model()
        self.assertEqual(m["footnotes"], [{"number": 1, "text": SOURCE_P14}])

    def test_cited_id_not_selected_gets_a_footnote(self):
        self.edit("ceo_move.json", lambda o: o["citations"].append("F-006"))
        m = self.model()
        self.assertIn("F-006 – cited finding is not among the selected findings",
                      [n["text"] for n in m["footnotes"]])
        self.assertEqual(len(m["ceo_move"]["notes"]), 2)


class LabelTests(ReportTestCase):
    def test_composite_row_shows_each_selected_reasoning_with_its_badge(self):
        self.edit_finding("F-001", lambda f: f["reasonings"][1].update(selected=True))
        row = next(r for r in self.model()["findings"] if r["finding_id"] == "F-001")
        self.assertEqual([(r["reasoning_id"], r["badge"]) for r in row["reasonings"]],
                         [("F-001-a", "High"), ("F-001-b", "Medium")])

    def test_contested_and_possibly_seasonal_carried_through(self):
        def change(f):
            f["contested"] = True
            f["reasonings"][0]["flags"] = ["possibly_seasonal"]
        self.edit_finding("F-002", change)
        row = next(r for r in self.model()["findings"] if r["finding_id"] == "F-002")
        self.assertEqual(row["reasonings"][0]["labels"], ["contested", "possibly seasonal"])

    def test_null_confidence_and_unchecked_priced_in(self):
        def change(f):
            f["reasonings"][0]["confidence"] = None
        self.edit_finding("F-004", change)
        row = next(r for r in self.model()["findings"] if r["finding_id"] == "F-004")
        self.assertEqual(row["reasonings"][0]["badge"], "Unverified")
        self.assertEqual(row["reasonings"][0]["priced_in"], "priced-in check not run")


class FailureStateTests(ReportTestCase):
    def test_synthesis_failed(self):
        self.write("synthesis.json", {"status": "failed", "verdict": None, "citations": [],
                                      "error": "numbers ['9'] do not appear in the input"})
        m = self.model()
        self.assertIsNone(m["thesis"]["verdict"])
        self.assertTrue(self.warned(m, "Synthesizer failed: numbers ['9']"))

    def test_synthesis_missing(self):
        (self.run_dir / "synthesis.json").unlink()
        m = self.model()
        self.assertIsNone(m["thesis"]["verdict"])
        self.assertTrue(self.warned(m, "synthesis.json missing"))

    def test_synthesis_stale(self):
        self.edit("synthesis.json", lambda o: o.update(citations=["F-006"]))
        m = self.model()
        self.assertIsNone(m["thesis"]["verdict"])
        self.assertTrue(self.warned(m, "out of date"))

    def test_ceo_move_failed(self):
        self.write("ceo_move.json", {"status": "failed", "error": "API error: boom"})
        m = self.model()
        self.assertEqual(m["ceo_move"]["status"], "failed")
        self.assertTrue(self.warned(m, "CEO Move failed: API error: boom"))

    def test_non_object_json_treated_as_missing(self):
        self.write("synthesis.json", ["not", "an", "object"])
        self.write("ceo_move.json", "nope")
        m = self.model()
        self.assertIsNone(m["thesis"]["verdict"])
        self.assertEqual(m["ceo_move"]["status"], "missing")
        self.assertTrue(self.warned(m, "synthesis.json is not a JSON object"))
        self.assertTrue(self.warned(m, "ceo_move.json is not a JSON object"))

    def test_stale_move_marked_in_callout(self):
        self.write("run.json", {"sharpest_needle": "F-002"})
        m = self.model()
        self.assertIn("written for F-003", m["ceo_move"]["stale"])
        self.assertTrue(self.warned(m, "re-run CEO Move"))

    def test_needle_with_empty_claim_keeps_a_headline(self):
        self.edit_finding("F-003", lambda f: f["reasonings"][0].update(claim=""))
        self.assertEqual(self.model()["thesis"]["headline"],
                         "Novo Nordisk, inventory, Q2 2026: DKK 333 millions")

    def test_ceo_move_missing(self):
        (self.run_dir / "ceo_move.json").unlink()
        m = self.model()
        self.assertEqual(m["ceo_move"]["status"], "missing")
        self.assertTrue(self.warned(m, "ceo_move.json missing"))

    def test_recommended_fallback(self):
        self.write("run.json", {"product": "Illustrative Brand"})
        m = self.model()
        self.assertEqual(m["thesis"]["needle"], "F-001")
        self.assertEqual(m["thesis"]["needle_note"],
                         "AI-recommended; not yet confirmed by the analyst.")
        self.assertTrue(self.warned(m, "AI-recommended; not yet confirmed by the analyst"))
        # ceo_move.json was written for F-003; the stale move is called out.
        self.assertTrue(self.warned(m, "re-run CEO Move"))

    def test_invalid_needle_pick(self):
        self.write("run.json", {"sharpest_needle": "F-006"})
        m = self.model()
        self.assertIsNone(m["thesis"]["needle"])
        self.assertIsNone(m["thesis"]["headline"])
        self.assertTrue(self.warned(m, "Sharpest Needle unavailable"))
        self.assertIsNone(m["product"])

    def test_no_label_finding(self):
        self.edit_finding("F-004", lambda f: f.update(is_label_finding=False))
        self.assertTrue(self.warned(self.model(), "No label/product finding"))

    def test_fewer_than_five_selected(self):
        self.edit_finding("F-005", lambda f: f["reasonings"][0].update(selected=False))
        m = self.model()
        self.assertEqual(len(m["findings"]), 4)
        self.assertTrue(self.warned(m, "Only 4 of 5 findings were selected"))

    def test_nothing_selected(self):
        def clear(findings):
            for f in findings:
                for r in f["reasonings"]:
                    r["selected"] = False
        self.edit("findings.json", clear)
        m = self.model()
        self.assertEqual(m["findings"], [])
        self.assertTrue(self.warned(m, "No selected findings"))

    def test_missing_findings_raises(self):
        (self.run_dir / "findings.json").unlink()
        with self.assertRaises(FileNotFoundError):
            self.model()


class PricedInAssessmentTests(ReportTestCase):
    """With the Priced-In Detector disabled, the report must not report a market view."""

    def strip_priced_in(self):
        def apply(findings):
            for f in findings:
                for r in f.get("reasonings") or []:
                    r["priced_in"] = None
        self.edit("findings.json", apply)

    def test_an_assessed_run_still_states_the_market_view(self):
        m = self.model()
        self.assertTrue(m["thesis"]["priced_in_assessed"])
        self.assertIsNotNone(m["thesis"]["priced_in"])
        self.assertFalse(self.warned(m, "Market pricing not assessed"))

    def test_an_unassessed_run_states_that_instead_of_a_call(self):
        # The Synthesizer is told to count unchecked as zero hits, so it lands on
        # "not priced in" - a market claim nobody checked.
        self.strip_priced_in()
        m = self.model()
        self.assertFalse(m["thesis"]["priced_in_assessed"])
        self.assertIsNone(m["thesis"]["priced_in"])
        self.assertTrue(self.warned(m, "Market pricing not assessed"))
        self.assertTrue(self.warned(m, "ranked by materiality alone"))

    def test_the_per_finding_line_says_the_check_did_not_run(self):
        self.strip_priced_in()
        for row in self.model()["findings"]:
            for r in row["reasonings"]:
                self.assertEqual(r["priced_in"], "priced-in check not run")

    def test_the_pdf_says_market_pricing_not_assessed(self):
        self.strip_priced_in()
        report.render_pdf(self.model(), self.run_dir / "report.pdf", compress=False)
        data = (self.run_dir / "report.pdf").read_bytes()
        self.assertIn(b"Market pricing not assessed", data)
        self.assertNotIn(b"Market view:", data)

    def test_an_assessed_pdf_keeps_the_market_view(self):
        report.render_pdf(self.model(), self.run_dir / "report.pdf", compress=False)
        data = (self.run_dir / "report.pdf").read_bytes()
        self.assertIn(b"Market view:", data)


class ComparisonTests(ReportTestCase):
    def test_columns_and_rows(self):
        self.write("comparison.json", {"columns": ["Dimension", "Brand"],
                                       "rows": [["Dosing", "weekly"]]})
        c = self.model()["comparison"]
        self.assertEqual((c["status"], c["columns"], c["rows"]),
                         ("ok", ["Dimension", "Brand"], [["Dosing", "weekly"]]))

    def test_list_of_row_dicts(self):
        self.write("comparison.json", [{"dimension": "Dosing", "brand": "weekly"},
                                       {"dimension": "Pricing"}])
        c = self.model()["comparison"]
        self.assertEqual(c["rows"], [["Dosing", "weekly"], ["Pricing", "not disclosed"]])

    def test_ragged_rows_padded_and_trimmed(self):
        self.write("comparison.json", {"columns": ["A", "B"], "rows": [["1"], ["1", "2", "3"]]})
        self.assertEqual(self.model()["comparison"]["rows"],
                         [["1", "not disclosed"], ["1", "2"]])

    def test_empty_columns_unrecognized(self):
        self.write("comparison.json", {"columns": [], "rows": []})
        self.assertEqual(self.model()["comparison"]["status"], "unrecognized")

    def test_unrecognized_shape(self):
        self.write("comparison.json", {"grid": "?"})
        m = self.model()
        self.assertEqual(m["comparison"]["status"], "unrecognized")
        self.assertTrue(self.warned(m, "unrecognized shape"))


class PdfTests(ReportTestCase):
    def test_run_writes_pdf(self):
        asyncio.run(report.run(self.run_dir))
        data = (self.run_dir / "report.pdf").read_bytes()
        self.assertTrue(data.startswith(b"%PDF"))
        self.assertNotIn(b"/URI", data)
        self.assertNotIn(b"/Annot", data)

    def test_run_writes_pdf_in_every_failure_state(self):
        for name in ("synthesis.json", "ceo_move.json", "manifest.json", "chunks.jsonl",
                     "run.json"):
            (self.run_dir / name).unlink()
        self.edit_finding("F-002", lambda f: f["reasonings"][0].update(
            claim="Odd characters − ≥ × 中 & <b> “quoted” – DKK"))
        asyncio.run(report.run(self.run_dir))
        self.assertTrue((self.run_dir / "report.pdf").read_bytes().startswith(b"%PDF"))

    def render(self):
        path = self.run_dir / "plain.pdf"
        report.render_pdf(report.build_model(self.run_dir), path, compress=False)
        return path.read_bytes()

    def test_model_content_reaches_the_pdf(self):
        data = self.render()
        for text in (b"PHARMER RESEARCH", b"We find the needle in the haystack.",
                     b"Sharpest Needle: Illustrative: inventory 333 built up.", b"[HIGH]",
                     b"Market view: partly priced in.", b"Recommended CEO move",
                     b"Downside if wrong:", SOURCE_P14.encode(), b"[1]",
                     b"Comparison table not built for this run."):
            self.assertIn(text, data)
        self.assertNotIn(b"Warnings for this run", data)

    def test_failure_states_reach_the_pdf(self):
        (self.run_dir / "ceo_move.json").unlink()
        self.write("run.json", {})
        data = self.render()
        for text in (b"Warnings for this run", b"CEO move unavailable.",
                     b"not yet confirmed by the analyst"):
            self.assertIn(text, data)

    def test_long_warnings_box_splits_across_pages(self):
        def cite_many(f):
            f["reasonings"][0]["citations"] = [{"chunk_id": f"X-p{i}-c01", "quote": "q"}
                                               for i in range(400)]
        self.edit_finding("F-002", cite_many)
        self.edit("ceo_move.json", lambda o: o.update(action="Illustrative: act. " * 300))
        asyncio.run(report.run(self.run_dir))
        self.assertTrue((self.run_dir / "report.pdf").read_bytes().startswith(b"%PDF"))

    def test_pdf_text(self):
        self.assertEqual(report.pdf_text("a ≥ b & <c> 中 – “x”"),
                         "a &gt;= b &amp; &lt;c&gt; ? – “x”")


if __name__ == "__main__":
    unittest.main()
