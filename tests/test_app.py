"""Offline tests for app.py - the UI helpers on fixture runs, the product list's traceability to
the case appendix, and a headless page smoke test (Streamlit AppTest). No network, no API.

Run: .venv/bin/python -m unittest tests.test_app
"""
import json
import shutil
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from streamlit.testing.v1 import AppTest

import app
import orchestrator
from pharmer import librarian

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
CASE = REPO_ROOT / "docs" / "neeley_ai_forward_case_study.md"
RUN_FILES = {"findings.json": "synthesis_findings.json", "run.json": "synthesis_run.json",
             "manifest.json": "report_manifest.json", "chunks.jsonl": "report_chunks.jsonl"}
START_DONE = {"scoring_after_peer": {"status": "ok", "seconds": 0.0, "error": None}}


class AppTestCase(unittest.TestCase):
    """A temp runs/ folder; make_run builds a fixture run at a given phase."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runs = Path(self.tmp.name) / "runs"
        self.runs.mkdir()
        patcher = mock.patch.object(orchestrator, "RUNS_DIR", self.runs)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def make_run(self, name, phase, started_at, stages=None, done=False):
        run_dir = self.runs / name
        run_dir.mkdir()
        for target, fixture in RUN_FILES.items():
            shutil.copy(FIXTURES / fixture, run_dir / target)
        status = {"ticker": name.split("-")[0], "phase": phase, "started_at": started_at,
                  "stages": stages if stages is not None else dict(START_DONE)}
        (run_dir / "pipeline.json").write_text(json.dumps(status), encoding="utf-8")
        if done:
            shutil.copy(FIXTURES / "report_synthesis.json", run_dir / "synthesis.json")
            shutil.copy(FIXTURES / "report_ceo_move.json", run_dir / "ceo_move.json")
            (run_dir / "report.pdf").write_bytes(b"%PDF-1.4 fixture")
        return run_dir


class HelperTests(AppTestCase):
    def test_list_runs_newest_first_and_stuck_run_interrupted(self):
        self.make_run("NVO-20261018-0100", "done", "2026-10-18T01:00:00+00:00")
        live = self.make_run("LLY-20261018-0300", "running", "2026-10-18T03:00:00+00:00")
        self.make_run("NVO-20261018-0200", "running", "2026-10-18T02:00:00+00:00")
        (self.runs / "not-a-run").mkdir()
        runs = app.list_runs(live=frozenset({str(live)}))
        self.assertEqual([(r["name"], r["phase"]) for r in runs],
                         [("LLY-20261018-0300", "running"), ("NVO-20261018-0200", "interrupted"),
                          ("NVO-20261018-0100", "done")])
        self.assertIn("interrupted", runs[1]["label"])

    def test_review_rows_carry_badges_breakdown_and_sources(self):
        run_dir = self.make_run("NVO-20261018-0100", "awaiting_human", "x")
        model, rows = app.review(run_dir)
        self.assertEqual([r["finding_id"] for r in rows][0], "F-003")  # run.json's needle first
        first = rows[1]["reasonings"][0]  # F-001-a, the Recommended one
        self.assertEqual((first["badge"], first["materiality"], first["obscurity"],
                          first["score"], first["priced_in_hits"], first["hunters"]),
                         ("High", 4.5, 1.0, 4.5, 0, ["channel_checker"]))
        self.assertEqual(first["quotes"][0], {"text": "Trade receivables 111",
                                              "sources": ["Novo Nordisk Q2 2026 6-K, p.14"]})

    def test_default_needle_run_json_pick_else_recommended(self):
        run_dir = self.make_run("NVO-20261018-0100", "awaiting_human", "x")
        _, rows = app.review(run_dir)
        self.assertEqual(app.default_needle(run_dir, rows), "F-003")
        (run_dir / "run.json").write_text("{}", encoding="utf-8")
        self.assertEqual(app.default_needle(run_dir, rows), "F-001")
        self.assertIsNone(app.default_needle(run_dir, []))

    def test_default_product(self):
        run_dir = self.make_run("NVO-20261018-0100", "awaiting_human", "x")
        options = app.product_options("NVO")
        self.assertEqual(app.default_product(run_dir, options), app.NOT_CONFIRMED)
        (run_dir / "run.json").write_text('{"product": "Ozempic/Wegovy"}', encoding="utf-8")
        self.assertEqual(app.default_product(run_dir, options), "Ozempic/Wegovy")

    def test_product_options(self):
        self.assertEqual(app.product_options("NVO"), [app.NOT_CONFIRMED, "Ozempic/Wegovy"])
        self.assertEqual(app.product_options("XYZ"), [app.NOT_CONFIRMED])

    def test_prebuilt_corpus_only_when_complete_and_same_ticker(self):
        self.assertEqual(app.prebuilt_corpus("NVO"), REPO_ROOT / "data" / "sample" / "NVO")
        self.assertIsNone(app.prebuilt_corpus("LLY"))

    def test_md_makes_filing_text_literal(self):
        self.assertEqual(app.md("$1.2 billion *net*"), r"\$1\.2 billion \*net\*")

    def test_job_crash_marks_the_run_failed(self):
        run_dir = self.make_run("NVO-20261018-0100", "running", "x")

        async def boom():
            raise OSError("disk full")
        app._job(run_dir, boom)
        status = orchestrator.read_status(run_dir)
        self.assertEqual(status["phase"], "failed")
        self.assertIn("OSError: disk full", status["error"])

    def test_mark_interrupted_allows_finish_again(self):
        stages = dict(START_DONE, synthesizer={"status": "running", "seconds": 0, "error": None})
        run_dir = self.make_run("NVO-20261018-0100", "running", "x", stages=stages)
        with self.assertRaises(orchestrator.UsageError):
            orchestrator.prepare_finish(run_dir, needle="F-003")
        app.mark_interrupted(run_dir)
        status = orchestrator.read_status(run_dir)
        self.assertEqual((status["phase"], status["stages"]["synthesizer"]["status"]),
                         ("failed", "failed"))
        orchestrator.prepare_finish(run_dir, needle="F-003")  # no longer refused

    def test_stage_problems(self):
        status = {"stages": {"a": {"status": "ok"}, "b": {"status": "skipped", "error": "x"},
                             "c": {"status": "degraded", "error": "y"}}}
        self.assertEqual(app.stage_problems(status),
                         [("b", "skipped", "x"), ("c", "degraded", "y")])


class ProductListTests(unittest.TestCase):
    """data/products.json must come from the case appendix, never from model memory."""

    def test_every_product_and_rival_text_is_in_the_case_document(self):
        text = CASE.read_text(encoding="utf-8")
        products = app.load_products()
        self.assertEqual(set(products), set(librarian.CIKS))
        for ticker, entries in products.items():
            self.assertTrue(entries, ticker)
            for entry in entries:
                self.assertIn(entry["product"], text)
                self.assertIn(entry["rivals"], text)


class ThemeTests(unittest.TestCase):
    """The dark "analyst terminal" theme (docs/ui-plan.md step 1) is set in the repo's config."""

    def test_dark_theme_with_amber_and_green(self):
        config = tomllib.loads((REPO_ROOT / ".streamlit" / "config.toml").read_text())["theme"]
        self.assertEqual((config["base"], config["primaryColor"], config["greenColor"]),
                         ("dark", "#ffb020", "#35d07f"))
        self.assertIn("monospace", config["codeFont"])


class PageTests(AppTestCase):
    def page(self, run=None, query_run=None):
        at = AppTest.from_file(str(REPO_ROOT / "app.py"), default_timeout=30)
        if run is not None:
            at.session_state["run"] = run
        if query_run is not None:
            at.query_params["run"] = query_run
        return at.run()

    def test_new_run_form(self):
        at = self.page()
        self.assertFalse(at.exception)
        self.assertEqual(at.title[0].value, "PHARMER RESEARCH")
        self.assertEqual(len(at.sidebar.children), 0)  # the run picker left the sidebar
        self.assertEqual(at.expander[0].label, "Past runs (0)")
        self.assertEqual(at.button[0].label, "Start run")
        self.assertEqual(len(at.toggle), 0)  # first company (ABBV) has no prebuilt corpus

    def test_query_param_opens_a_run_from_past_runs(self):
        self.make_run("NVO-20261018-0100", "awaiting_human", "x")
        at = self.page(query_run="NVO-20261018-0100")
        self.assertFalse(at.exception)
        self.assertEqual(at.expander[0].label, "Past runs (1)")
        self.assertEqual(at.selectbox(key="run").value, "NVO-20261018-0100")
        self.assertEqual(at.radio[0].value, "F-003")

    def test_new_run_button_returns_to_the_form(self):
        self.make_run("NVO-20261018-0100", "awaiting_human", "x")
        at = self.page("NVO-20261018-0100")
        next(b for b in at.button if b.label == "➕ New run").click().run()
        self.assertFalse(at.exception)
        self.assertEqual(at.subheader[0].value, "Start a new run")

    def test_paused_run_shows_review_and_pick(self):
        self.make_run("NVO-20261018-0100", "awaiting_human", "x")
        at = self.page("NVO-20261018-0100")
        self.assertFalse(at.exception)
        self.assertEqual(at.radio[0].value, "F-003")
        product = next(s for s in at.selectbox if s.label == "Product")
        self.assertEqual(product.value, app.NOT_CONFIRMED)
        self.assertEqual(product.options, app.product_options("NVO"))
        text = " ".join(m.value for m in at.markdown)
        self.assertIn(app.md("Novo Nordisk Q2 2026 6-K, p.14"), text)  # rendered literally

    def test_done_run_shows_verdict(self):
        self.make_run("NVO-20261018-0100", "done", "x", done=True)
        at = self.page("NVO-20261018-0100")
        self.assertFalse(at.exception)
        text = " ".join(m.value for m in at.markdown)
        self.assertIn("Verdict:", text)
        self.assertIn("CEO move:", text)

    def submit(self, at):
        with mock.patch.object(app, "launch") as launch:
            next(b for b in at.button if b.label == "Write the report").click().run()
        return launch

    def test_submit_writes_the_pick_and_launches_finish(self):
        run_dir = self.make_run("NVO-20261018-0100", "awaiting_human", "x")
        at = self.page("NVO-20261018-0100")
        at.radio[0].set_value("F-002")
        next(s for s in at.selectbox if s.label == "Product").set_value("Ozempic/Wegovy")
        launch = self.submit(at)
        self.assertFalse(at.exception)
        self.assertEqual(json.loads((run_dir / "run.json").read_text()),
                         {"sharpest_needle": "F-002", "product": "Ozempic/Wegovy"})
        self.assertEqual(launch.call_args.args[0], run_dir)
        self.assertEqual(orchestrator.read_status(run_dir)["phase"], "running")

    def test_not_confirmed_clears_an_earlier_product(self):
        run_dir = self.make_run("NVO-20261018-0100", "awaiting_human", "x")
        (run_dir / "run.json").write_text('{"product": "Ozempic/Wegovy"}', encoding="utf-8")
        at = self.page("NVO-20261018-0100")
        next(s for s in at.selectbox if s.label == "Product").set_value(app.NOT_CONFIRMED)
        self.submit(at)
        self.assertNotIn("product", json.loads((run_dir / "run.json").read_text()))

    def test_submit_refused_while_a_job_is_live(self):
        run_dir = self.make_run("NVO-20261018-0100", "awaiting_human", "x")
        at = self.page("NVO-20261018-0100")
        with mock.patch.object(app, "live_runs", return_value=frozenset({str(run_dir)})):
            launch = self.submit(at)
        launch.assert_not_called()
        self.assertIn("already being processed", at.error[0].value)

    def test_start_uses_prebuilt_only_when_toggled(self):
        for toggled, expected in ((False, None), (True, REPO_ROOT / "data" / "sample" / "NVO")):
            at = self.page()
            next(s for s in at.selectbox if s.label == "Company").set_value("NVO").run()
            at.toggle[0].set_value(toggled)
            with mock.patch.object(orchestrator, "prepare_run",
                                   return_value=self.runs / "NVO-X") as prepare, \
                    mock.patch.object(app, "launch"):
                at.button[0].click().run()
            self.assertEqual(prepare.call_args.kwargs["corpus"], expected)

    def test_running_run_shows_progress(self):
        run_dir = self.make_run("NVO-20261018-0100", "running", "x",
                                stages={"hunters": {"status": "running", "seconds": 1.0,
                                                    "error": None}})
        with mock.patch.object(app, "live_runs", return_value=frozenset({str(run_dir)})):
            at = self.page("NVO-20261018-0100")
        self.assertFalse(at.exception)
        self.assertIn("Working", at.info[0].value)
        self.assertEqual(len(at.radio), 0)

    def test_interrupted_run_offers_the_retry_mark(self):
        self.make_run("NVO-20261018-0100", "running", "x")
        at = self.page("NVO-20261018-0100")
        self.assertFalse(at.exception)
        self.assertEqual(len(at.radio), 0)
        self.assertIn("no job for it is alive", at.error[0].value)
        next(b for b in at.button if b.label.startswith("Mark it as failed")).click().run()
        self.assertEqual(orchestrator.read_status(self.runs / "NVO-20261018-0100")["phase"],
                         "failed")
        self.assertEqual(at.radio[0].value, "F-003")  # the pick form is back

    def test_failed_start_shows_no_pick(self):
        self.make_run("NVO-20261018-0100", "failed", "x",
                      stages={"dedup": {"status": "failed", "seconds": 0.1, "error": "boom"}})
        at = self.page("NVO-20261018-0100")
        self.assertFalse(at.exception)
        self.assertEqual(len(at.radio), 0)
        self.assertIn("dedup", at.warning[0].value)


if __name__ == "__main__":
    unittest.main()
