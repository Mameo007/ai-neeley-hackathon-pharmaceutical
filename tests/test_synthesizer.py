"""Offline tests for pharmer/synthesizer.py - the verdict call, its validation, the one-retry
rule and the failed state, with a fake client (no API calls).

Run: .venv/bin/python -m unittest tests.test_synthesizer
"""
import asyncio
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

import anthropic
import httpx2

from pharmer import llm, synthesizer
from tests.test_llm import FakeClient, fake_message

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FINDINGS = FIXTURES / "synthesis_findings.json"
RUN_JSON = FIXTURES / "synthesis_run.json"

GOOD = {"verdict": "Rebate provisions of 222 and an inventory build of 333 limit growth, and "
                   "the market has not priced them in.",
        "priced_in_call": "partly_priced_in", "citations": ["F-002-a", "F-003", "F-002-a"]}


def reply(obj):
    return fake_message("end_turn", [NS(type="text", text=json.dumps(obj))])


def fixture_numbers(*paths):
    return set().union(*(synthesizer.numbers_in(Path(p).read_text(encoding="utf-8"))
                         for p in paths))


class StageTestCase(unittest.TestCase):
    """Temp run dir with the scoring-output fixture and run.json; log and client swapped out."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orig_log, self.orig_client = llm.LOG_PATH, llm._client
        llm.LOG_PATH = Path(self.tmp.name) / "logs" / "api_usage.csv"
        self.run_dir = Path(self.tmp.name) / "NVO-20261018-0230"
        self.run_dir.mkdir()
        shutil.copy(FINDINGS, self.run_dir / "findings.json")
        shutil.copy(RUN_JSON, self.run_dir / "run.json")

    def tearDown(self):
        llm.LOG_PATH, llm._client = self.orig_log, self.orig_client
        self.tmp.cleanup()

    def output(self, name):
        return json.loads((self.run_dir / name).read_text(encoding="utf-8"))


class SynthesizerTests(StageTestCase):
    def run_stage(self, *messages):
        llm._client = FakeClient(list(messages))
        asyncio.run(synthesizer.run(self.run_dir))
        return self.output("synthesis.json"), llm._client.requests

    def test_happy_path(self):
        out, requests = self.run_stage(reply(GOOD))
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["verdict"], GOOD["verdict"])
        self.assertEqual(out["priced_in_call"], "partly_priced_in")
        self.assertEqual(out["citations"], ["F-002-a", "F-003"])  # duplicates dropped
        self.assertEqual(out["product"], "Illustrative Brand")
        self.assertIsNone(out["error"])
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["model"], llm.OPUS)
        self.assertEqual(requests[0]["system"], synthesizer.SYSTEM)
        self.assertEqual(requests[0]["output_config"], synthesizer.OUTPUT_CONFIG)

    def test_only_selected_reasonings_reach_the_model(self):
        _, requests = self.run_stage(reply(GOOD))
        content = requests[0]["messages"][0]["content"]
        for rid in ["F-001-a", "F-002-a", "F-003-a", "F-004-a", "F-005-a"]:
            self.assertIn(rid, content)
        for rid in ["F-001-b", "F-006", "F-007"]:
            self.assertNotIn(rid, content)

    def test_missing_grid(self):
        out, requests = self.run_stage(reply(GOOD))
        self.assertFalse(out["grid_used"])
        self.assertIn("unavailable", requests[0]["messages"][0]["content"])

    def test_grid_used_when_present(self):
        (self.run_dir / "comparison.json").write_text(
            json.dumps({"dosing": "Illustrative weekly cell"}), encoding="utf-8")
        out, requests = self.run_stage(reply(GOOD))
        self.assertTrue(out["grid_used"])
        self.assertIn("Illustrative weekly cell", requests[0]["messages"][0]["content"])

    def test_unselected_citation_retried_then_accepted(self):
        bad = {**GOOD, "citations": ["F-006-a"]}
        with self.assertLogs("pharmer.synthesizer", level="WARNING"):
            out, requests = self.run_stage(reply(bad), reply(GOOD))
        self.assertEqual(out["status"], "ok")
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[1]["system"], requests[0]["system"])
        retry = requests[1]["messages"][0]["content"]
        self.assertIn("Problems with your previous attempt", retry)
        self.assertIn("F-006-a", retry.split("Problems with your previous attempt")[1])

    def test_two_bad_replies_give_failed_state(self):
        bad = {**GOOD, "citations": ["F-001-b"]}
        with self.assertLogs("pharmer.synthesizer", level="WARNING"):
            out, requests = self.run_stage(reply(bad), reply(bad))
        self.assertEqual(out["status"], "failed")
        self.assertIsNone(out["verdict"])
        self.assertEqual(out["citations"], [])
        self.assertIn("F-001-b", out["error"])
        self.assertEqual(len(requests), 2)

    def test_invented_number_rejected(self):
        bad = {**GOOD, "verdict": "Receivables grew 25.5% faster than sales, not priced in."}
        with self.assertLogs("pharmer.synthesizer", level="WARNING"):
            out, requests = self.run_stage(reply(bad), reply(GOOD))
        self.assertEqual(out["status"], "ok")
        self.assertIn("25.5", requests[1]["messages"][0]["content"])

    def test_two_sentences_rejected(self):
        bad = {**GOOD, "verdict": "Rebates limit growth. The market has not priced it in."}
        with self.assertLogs("pharmer.synthesizer", level="WARNING"):
            out, _ = self.run_stage(reply(bad), reply(bad))
        self.assertEqual(out["status"], "failed")
        self.assertIn("one sentence", out["error"])

    def test_each_check_rejects(self):
        cases = {"citations": ({"citations": []}, "at least one"),
                 "blank verdict": ({"verdict": "   "}, "non-empty"),
                 "bad call": ({"priced_in_call": "maybe"}, "must be one of"),
                 "call vs hits": ({"priced_in_call": "not_priced_in"},
                                  "make it 'partly_priced_in'"),
                 "too long": ({"verdict": "Rebates limit growth " + "and more " * 40 + "."},
                              "at most about 60 words")}
        for name, (change, error) in cases.items():
            with self.subTest(name):
                with self.assertLogs("pharmer.synthesizer", level="WARNING"):
                    out, requests = self.run_stage(reply({**GOOD, **change}),
                                                   reply({**GOOD, **change}))
                self.assertEqual(out["status"], "failed")
                self.assertIn(error, out["error"])
                self.assertEqual(len(requests), 2)

    def test_priced_in_call_rule(self):
        selected = synthesizer.selected_findings(json.loads(FINDINGS.read_text(encoding="utf-8")))
        expected = synthesizer.expected_priced_in_call
        self.assertEqual(expected(["F-001-a", "F-004"], selected), "not_priced_in")  # 0 and null
        self.assertEqual(expected(["F-003-a"], selected), "priced_in")
        self.assertEqual(expected(["F-002", "F-003-a"], selected), "partly_priced_in")

    def test_refusal_then_malformed_gives_failed_state(self):
        malformed = fake_message("end_turn", [NS(type="text", text="not json")])
        with self.assertLogs("pharmer.synthesizer", level="WARNING"):
            out, requests = self.run_stage(fake_message("refusal", []), malformed)
        self.assertEqual(out["status"], "failed")
        self.assertIn("not valid JSON", out["error"])
        self.assertIn("stop_reason=refusal", requests[1]["messages"][0]["content"])

    def test_truncated_then_good(self):
        with self.assertLogs("pharmer.synthesizer", level="WARNING"):
            out, _ = self.run_stage(fake_message("max_tokens", []), reply(GOOD))
        self.assertEqual(out["status"], "ok")

    def test_api_error_never_raises(self):
        error = anthropic.APIConnectionError(request=httpx2.Request("POST", "https://x"))

        class FailingClient(FakeClient):
            def _stream(self, **request):
                self.requests.append(request)
                raise error

        llm._client = FailingClient([])
        with self.assertLogs("pharmer.synthesizer", level="WARNING"):
            asyncio.run(synthesizer.run(self.run_dir))
        out = self.output("synthesis.json")
        self.assertEqual(out["status"], "failed")
        self.assertIn("APIConnectionError", out["error"])
        self.assertEqual(len(llm._client.requests), 2)

    def test_missing_credential_never_raises(self):
        class NoKeyClient(FakeClient):
            def _stream(self, **request):
                self.requests.append(request)
                raise TypeError("Could not resolve authentication method.")

        llm._client = NoKeyClient([])
        with self.assertLogs("pharmer.synthesizer", level="WARNING"):
            asyncio.run(synthesizer.run(self.run_dir))
        out = self.output("synthesis.json")
        self.assertEqual(out["status"], "failed")
        self.assertIn("authentication", out["error"])

    def test_nothing_selected_makes_no_call(self):
        findings = json.loads(FINDINGS.read_text(encoding="utf-8"))
        for finding in findings:
            for reasoning in finding["reasonings"]:
                reasoning["selected"] = False
        synthesizer.write_json(self.run_dir / "findings.json", findings)
        with self.assertLogs("pharmer.synthesizer", level="WARNING"):
            out, requests = self.run_stage()
        self.assertEqual(out["status"], "failed")
        self.assertEqual(requests, [])

    def test_missing_or_unreadable_run_json(self):
        for kind in ["missing", "invalid", "directory"]:
            with self.subTest(kind):
                path = self.run_dir / "run.json"
                if path.is_dir():
                    path.rmdir()
                path.unlink(missing_ok=True)
                if kind == "invalid":
                    path.write_text("{not json", encoding="utf-8")
                elif kind == "directory":
                    path.mkdir()
                if kind == "missing":
                    out, requests = self.run_stage(reply(GOOD))
                else:
                    with self.assertLogs("pharmer.synthesizer", level="WARNING"):
                        out, requests = self.run_stage(reply(GOOD))
                self.assertEqual(out["status"], "ok")
                self.assertIsNone(out["product"])
                self.assertIn("not named yet", requests[0]["messages"][0]["content"])

    def test_user_message_numbers_come_from_the_fixture(self):
        _, requests = self.run_stage(reply(GOOD))
        content = requests[0]["messages"][0]["content"]
        self.assertLessEqual(synthesizer.numbers_in(content), fixture_numbers(FINDINGS, RUN_JSON))
        self.assertIn("4.5", synthesizer.numbers_in(content))


class NumbersTests(unittest.TestCase):
    def test_normalization(self):
        self.assertEqual(synthesizer.numbers_in("-420.0 and 87,123m, up 3%."),
                         {"420", "87123", "3"})

    def test_ids_skipped(self):
        self.assertEqual(synthesizer.numbers_in("see F-003-a and F-012"), set())

    def test_one_sentence(self):
        for text in ["U.S. payers limit growth, not priced in.", "Rebates vs. Ozempic limit it.",
                     "Sales of 222, e.g. Payers' rebates, limit growth."]:
            self.assertTrue(synthesizer.is_one_sentence(text), text)
        for text in ["Flagged by the FDA. The market has not priced it in.",
                     "Rebates rose to 222. The market missed it.",
                     "Growth stalls\u2026 Nobody noticed.", "Two\nlines."]:
            self.assertFalse(synthesizer.is_one_sentence(text), text)

if __name__ == "__main__":
    unittest.main()
