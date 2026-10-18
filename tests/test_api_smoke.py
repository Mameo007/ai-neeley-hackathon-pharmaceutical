"""Offline tests for scripts/api_smoke.py on the fake client: the checks pass and fail when
they should, and the requests carry the shapes the stages use. No API calls.

Run: .venv/bin/python -m unittest tests.test_api_smoke
"""
import asyncio
import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

from pharmer import llm
from tests.test_llm import FakeClient, fake_message

_spec = importlib.util.spec_from_file_location(
    "api_smoke", Path(__file__).resolve().parent.parent / "scripts" / "api_smoke.py")
smoke = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(smoke)


def text(value):
    return [NS(type="text", text=value)]


def replies(cache_read=7000):
    return [fake_message("end_turn", text("OK")), fake_message("end_turn", text("OK")),
            fake_message("end_turn", text(json.dumps({"answer": "OK"}))),
            fake_message("end_turn", text("OK"), cache_read=0),
            fake_message("end_turn", text("OK"), cache_read=cache_read)]


class ApiSmokeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orig_log, self.orig_client = llm.LOG_PATH, llm._client
        llm.LOG_PATH = Path(self.tmp.name) / "logs" / "api_usage.csv"

    def tearDown(self):
        llm.LOG_PATH, llm._client = self.orig_log, self.orig_client
        self.tmp.cleanup()

    def run_smoke(self, messages):
        llm._client = FakeClient(messages)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = asyncio.run(smoke.main())
        return code, out.getvalue(), llm._client.requests

    def test_all_pass_and_request_shapes(self):
        msgs = replies()
        msgs[3].usage.cache_creation_input_tokens = 6000
        code, out, requests = self.run_smoke(msgs)
        self.assertEqual(code, 0, out)
        self.assertIn("5/5 checks passed", out)
        self.assertEqual([r["model"] for r in requests],
                         [llm.HAIKU, llm.OPUS, llm.OPUS, llm.OPUS, llm.OPUS])
        self.assertNotIn("fallbacks", requests[0])
        self.assertEqual(requests[1]["fallbacks"], "default")
        self.assertEqual(requests[2]["output_config"], smoke.SCHEMA)
        self.assertEqual(requests[3]["system"], requests[4]["system"])
        self.assertEqual(len(llm.LOG_PATH.read_text().splitlines()), 6)  # header + 5 rows

    def test_zero_cache_read_fails(self):
        code, out, _ = self.run_smoke(replies(cache_read=0))
        self.assertEqual(code, 1)
        self.assertIn("FAIL cache_read", out)

    def test_bad_json_and_refusal_fail_without_stopping(self):
        msgs = replies()
        msgs[1] = fake_message("refusal", text(""))
        msgs[2] = fake_message("end_turn", text("not json"))
        code, out, requests = self.run_smoke(msgs)
        self.assertEqual(code, 1)
        self.assertIn("FAIL opus", out)
        self.assertIn("FAIL json_schema", out)
        self.assertEqual(len(requests), 5)


if __name__ == "__main__":
    unittest.main()
