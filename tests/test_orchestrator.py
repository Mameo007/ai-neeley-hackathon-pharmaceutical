"""Offline tests for orchestrator.py - stage order, skips, failures, the human pause and the
finish phase, with fake stage modules (no network, no API calls).

Run: .venv/bin/python -m unittest tests.test_orchestrator
"""
import asyncio
import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

import orchestrator

FIXTURES = Path(__file__).resolve().parent / "fixtures"
NOT_BUILT = {"fact_checker", "quant", "priced_in", "peer", "comparison"}


class OrchestratorTestCase(unittest.TestCase):
    """Temp runs/ and corpus folders; load_module returns fakes that log their calls."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.runs, self.corpus = root / "runs", root / "corpus"
        self.corpus.mkdir()
        for name in orchestrator.CORPUS_FILES:
            (self.corpus / name).write_text("[]\n", encoding="utf-8")
        (self.corpus / "manifest.json").write_text('[{"ticker": "NVO"}]', encoding="utf-8")
        self.calls, self.fail, self.not_built = [], {}, set(NOT_BUILT)
        self.leads = [{"finding_id": "F-001"}]
        self.synthesis_status = self.ceo_status = "ok"
        self.repo_runs_before = set(orchestrator.RUNS_DIR.glob("*"))
        patcher = mock.patch.object(orchestrator, "load_module", self.fake_load)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def fake_load(self, module):
        if module in self.not_built:
            return None

        async def run(run_dir):
            self.calls.append(module)
            if module in self.fail:
                raise self.fail[module]
            write = {"hunters": ("leads.json", self.leads),
                     "synthesizer": ("synthesis.json", {"status": self.synthesis_status,
                                                        "error": "boom"}),
                     "ceo_move": ("ceo_move.json", {"status": self.ceo_status,
                                                    "error": "bang"}),
                     "report": ("report.pdf", "%PDF")}.get(module)
            if write:
                (Path(run_dir) / write[0]).write_text(json.dumps(write[1]), encoding="utf-8")
            if module == "scoring":
                shutil.copy(FIXTURES / "synthesis_findings.json", Path(run_dir) / "findings.json")
        return NS(run=run)

    def start(self):
        return asyncio.run(orchestrator.start_run("nvo", corpus=self.corpus, runs_dir=self.runs))

    def finish(self, run_dir, **kwargs):
        return asyncio.run(orchestrator.finish_run(run_dir, **kwargs))

    def tearDown(self):
        # No test may leave a folder in the repo's real runs/.
        self.assertEqual(set(orchestrator.RUNS_DIR.glob("*")), self.repo_runs_before)

    def main(self, argv):
        out = io.StringIO()
        with mock.patch.object(orchestrator, "RUNS_DIR", self.runs), \
                contextlib.redirect_stdout(out):
            code = orchestrator.main(argv)
        return code, out.getvalue()

    def status(self, run_dir):
        return json.loads((Path(run_dir) / "pipeline.json").read_text(encoding="utf-8"))


class StartTests(OrchestratorTestCase):
    def test_happy_path_stops_at_the_pause(self):
        run_dir = self.start()
        self.assertTrue(run_dir.name.startswith("NVO-"))
        for name in orchestrator.CORPUS_FILES:
            self.assertTrue((run_dir / name).exists())
        self.assertEqual(self.calls, ["hunters", "dedup", "scoring", "scoring"])
        status = self.status(run_dir)
        self.assertEqual(status["phase"], "awaiting_human")
        self.assertEqual(status["ticker"], "NVO")
        self.assertEqual(list(status["stages"]),
                         ["corpus", "hunters", "dedup", "fact_checker", "quant", "priced_in",
                          "scoring", "peer", "scoring_after_peer"])
        for name in ("fact_checker", "quant", "priced_in", "peer"):
            self.assertEqual((status["stages"][name]["status"], status["stages"][name]["error"]),
                             ("skipped", "not built"))
        self.assertEqual(status["stages"]["dedup"]["status"], "ok")
        self.assertFalse((run_dir / "run.json").exists())

    def test_built_partner_stages_run_in_order(self):
        self.not_built = {"priced_in", "peer", "comparison"}
        self.start()
        self.assertEqual(self.calls, ["hunters", "dedup", "fact_checker", "quant", "scoring",
                                      "scoring"])

    def test_required_stage_failure_stops(self):
        self.fail["dedup"] = ValueError("bad leads")
        status = self.status(self.start())
        self.assertEqual(status["phase"], "failed")
        self.assertEqual(status["stages"]["dedup"]["status"], "failed")
        self.assertIn("ValueError: bad leads", status["stages"]["dedup"]["error"])
        self.assertNotIn("scoring", status["stages"])
        self.assertEqual(self.calls, ["hunters", "dedup"])

    def test_optional_stage_failure_continues(self):
        self.not_built.discard("quant")
        self.fail["quant"] = RuntimeError("no fx")
        status = self.status(self.start())
        self.assertEqual(status["stages"]["quant"]["status"], "failed")
        self.assertEqual(status["phase"], "awaiting_human")
        self.assertIn("scoring", self.calls)

    def test_no_leads_is_degraded(self):
        self.leads = []
        stage = self.status(self.start())["stages"]["hunters"]
        self.assertEqual(stage["status"], "degraded")
        self.assertIn("no leads", stage["error"])

    def test_corpus_failure_stops(self):
        with mock.patch.object(orchestrator, "_corpus_stage",
                               return_value=mock.AsyncMock(side_effect=OSError("sec down"))):
            status = self.status(self.start())
        self.assertEqual(status["stages"]["corpus"]["status"], "failed")
        self.assertEqual(status["phase"], "failed")
        self.assertEqual(self.calls, [])

    def test_bad_ticker_and_corpus_refused_without_a_run_dir(self):
        with self.assertRaises(orchestrator.UsageError):
            asyncio.run(orchestrator.start_run("XYZ", corpus=self.corpus, runs_dir=self.runs))
        with self.assertRaises(orchestrator.UsageError):
            asyncio.run(orchestrator.start_run("NVO", corpus=self.runs, runs_dir=self.runs))
        self.assertFalse(self.runs.exists())

    def test_cli_exit_codes_and_pause_summary(self):
        self.assertEqual(self.main(["start", "XYZ"])[0], 2)
        code, out = self.main(["start", "NVO", "--corpus", str(self.corpus)])
        self.assertEqual(code, 0)
        run_dir = next(self.runs.iterdir())
        self.assertIn("F-001  [Recommended]", out)
        self.assertIn(f"orchestrator.py finish {run_dir} --needle F-001 --product NAME", out)
        with self.assertRaises(SystemExit) as cm:
            orchestrator.main([])
        self.assertEqual(cm.exception.code, 2)

    def test_cli_failed_phase_exits_1(self):
        self.fail["dedup"] = ValueError("bad leads")
        self.assertEqual(self.main(["start", "NVO", "--corpus", str(self.corpus)])[0], 1)

    def test_run_folder_clash_refused(self):
        with mock.patch.object(orchestrator.librarian, "run_id_for", return_value="NVO-X"):
            self.start()
            with self.assertRaises(orchestrator.UsageError):
                self.start()

    def test_corpus_of_another_ticker_refused(self):
        with self.assertRaises(orchestrator.UsageError) as cm:
            asyncio.run(orchestrator.start_run("LLY", corpus=self.corpus, runs_dir=self.runs))
        self.assertIn("NVO", str(cm.exception))

    def test_librarian_audit_copied_when_present(self):
        (self.corpus / "librarian_report.json").write_text("{}", encoding="utf-8")
        self.assertTrue((self.start() / "librarian_report.json").exists())

    def test_live_corpus_is_the_librarian(self):
        self.assertIs(orchestrator._corpus_stage(None), orchestrator.librarian.run)

    def test_interrupt_marks_the_run_failed(self):
        self.fail["dedup"] = KeyboardInterrupt()
        with mock.patch.object(orchestrator.librarian, "run_id_for", return_value="NVO-X"):
            with self.assertRaises(KeyboardInterrupt):
                self.start()
        status = self.status(self.runs / "NVO-X")
        self.assertEqual(status["phase"], "failed")
        self.assertEqual(status["stages"]["dedup"]["error"], "interrupted")

    def test_broken_partner_module_is_failed_not_skipped(self):
        self.not_built.discard("quant")
        real = self.fake_load

        def load(module):
            if module == "quant":
                raise ModuleNotFoundError("No module named 'numpy'", name="numpy")
            return real(module)
        with mock.patch.object(orchestrator, "load_module", load):
            status = self.status(self.start())
        self.assertEqual(status["stages"]["quant"]["status"], "failed")
        self.assertIn("numpy", status["stages"]["quant"]["error"])
        self.assertEqual(status["phase"], "awaiting_human")


class FinishTests(OrchestratorTestCase):
    def setUp(self):
        super().setUp()
        self.run_dir = self.start()
        self.calls.clear()

    def run_json(self):
        return json.loads((self.run_dir / "run.json").read_text(encoding="utf-8"))

    def test_happy_path(self):
        self.finish(self.run_dir, needle="F-003", product="Brand B")
        self.assertEqual(self.run_json(), {"sharpest_needle": "F-003", "product": "Brand B"})
        self.assertEqual(self.calls, ["synthesizer", "ceo_move", "report"])
        status = self.status(self.run_dir)
        self.assertEqual(status["phase"], "done")
        self.assertEqual(status["stages"]["comparison"]["status"], "skipped")
        self.assertIn("scoring_after_peer", status["stages"])  # start's stages are kept
        for entry in status["stages"].values():
            self.assertIn("seconds", entry)

    def test_rerun_keeps_other_keys(self):
        self.finish(self.run_dir, needle="F-003", product="Brand B")
        self.finish(self.run_dir, needle="F-001")
        self.assertEqual(self.run_json(), {"sharpest_needle": "F-001", "product": "Brand B"})

    def test_no_needle_leaves_the_fallback_to_ceo_move(self):
        self.finish(self.run_dir)
        self.assertEqual(self.run_json(), {})

    def test_bad_needle_refused_and_nothing_written(self):
        with self.assertRaises(orchestrator.UsageError) as cm:
            self.finish(self.run_dir, needle="F-006")
        self.assertIn("F-001", str(cm.exception))
        self.assertFalse((self.run_dir / "run.json").exists())
        self.assertEqual(self.calls, [])

    def test_finish_before_scoring_refused(self):
        findings = self.run_dir / "findings.json"
        findings.write_text(json.dumps([{"finding_id": "F-001", "status": "candidate"}]))
        with self.assertRaises(orchestrator.UsageError):
            self.finish(self.run_dir, needle="F-001")
        findings.unlink()
        self.assertEqual(orchestrator.main(["finish", str(self.run_dir)]), 2)
        self.assertFalse((self.run_dir / "run.json").exists())

    def test_failed_synthesis_is_degraded_and_run_continues(self):
        self.synthesis_status = "failed"
        self.finish(self.run_dir, needle="F-003")
        status = self.status(self.run_dir)
        self.assertEqual(status["stages"]["synthesizer"]["status"], "degraded")
        self.assertIn("boom", status["stages"]["synthesizer"]["error"])
        self.assertEqual(self.calls, ["synthesizer", "ceo_move", "report"])
        self.assertEqual(status["phase"], "done")

    def test_failed_ceo_move_is_degraded(self):
        self.ceo_status = "failed"
        self.finish(self.run_dir, needle="F-003")
        stage = self.status(self.run_dir)["stages"]["ceo_move"]
        self.assertEqual(stage["status"], "degraded")
        self.assertIn("bang", stage["error"])

    def test_refused_unless_start_paused(self):
        status = self.status(self.run_dir)
        status["phase"] = "failed"
        (self.run_dir / "pipeline.json").write_text(json.dumps(status), encoding="utf-8")
        with self.assertRaises(orchestrator.UsageError):
            self.finish(self.run_dir, needle="F-003")

    def test_rerun_clears_the_previous_report(self):
        self.finish(self.run_dir, needle="F-003")
        self.assertTrue((self.run_dir / "report.pdf").exists())
        self.fail["report"] = OSError("disk full")
        self.finish(self.run_dir, needle="F-001")
        self.assertFalse((self.run_dir / "report.pdf").exists())

    def test_report_failure_fails_the_phase(self):
        self.fail["report"] = OSError("disk full")
        self.finish(self.run_dir, needle="F-003")
        self.assertEqual(self.status(self.run_dir)["phase"], "failed")


class LoadModuleTests(unittest.TestCase):
    def test_missing_module_is_none_and_real_one_loads(self):
        self.assertIsNone(orchestrator.load_module("no_such_stage"))
        self.assertTrue(hasattr(orchestrator.load_module("scoring"), "run"))

    def test_missing_dependency_of_a_real_module_raises(self):
        error = ModuleNotFoundError("No module named 'numpy'", name="numpy")
        with mock.patch.object(orchestrator.importlib, "import_module", side_effect=error):
            with self.assertRaises(ModuleNotFoundError):
                orchestrator.load_module("quant")


if __name__ == "__main__":
    unittest.main()
