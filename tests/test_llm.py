"""Offline tests for pharmer/llm.py - a fake client stands in for the API, so no tokens are spent.

Run: .venv/bin/python -m unittest tests.test_llm
"""
import asyncio
import csv
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

from pharmer import llm


def fake_message(stop_reason, content, cache_read=0):
    usage = NS(input_tokens=1000, output_tokens=200, cache_creation_input_tokens=0,
               cache_read_input_tokens=cache_read,
               server_tool_use=NS(web_search_requests=1, web_fetch_requests=0))
    msg = NS(stop_reason=stop_reason, content=content, usage=usage)
    msg._request_id = "req_test"
    return msg


class FakeStream:
    def __init__(self, message):
        self.message = message

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_final_message(self):
        return self.message


class FakeClient:
    """Returns queued messages in order and records every request it was sent."""

    def __init__(self, messages):
        self.queue = list(messages)
        self.requests = []
        self.beta = NS(messages=NS(stream=self._stream))

    def _stream(self, **request):
        self.requests.append(request)
        return FakeStream(self.queue.pop(0))


class LlmTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orig_log, self.orig_client = llm.LOG_PATH, llm._client
        llm.LOG_PATH = Path(self.tmp.name) / "logs" / "api_usage.csv"

    def tearDown(self):
        llm.LOG_PATH, llm._client = self.orig_log, self.orig_client
        self.tmp.cleanup()

    def rows(self):
        with llm.LOG_PATH.open() as fh:
            return list(csv.DictReader(fh))

    def test_call_adds_caching_and_fallback_and_logs_one_row(self):
        llm._client = FakeClient([fake_message("end_turn", [NS(type="text", text="ok")], cache_read=800)])
        asyncio.run(llm.call(run_id="NVO-test", stage="hunters", agent="redliner",
                             messages=[{"role": "user", "content": "hi"}]))
        request = llm._client.requests[0]
        self.assertEqual(request["cache_control"], {"type": "ephemeral"})
        self.assertEqual(request["fallbacks"], "default")
        self.assertIn(llm.FALLBACK_BETA, request["betas"])
        [row] = self.rows()
        self.assertEqual(row["run_id"], "NVO-test")
        self.assertEqual(row["cache_read_input_tokens"], "800")
        self.assertEqual(row["web_search_requests"], "1")
        self.assertEqual(row["request_id"], "req_test")
        self.assertEqual(row["stop_reason"], "end_turn")
        self.assertNotEqual(row["est_cost_usd"], "")

    def test_haiku_gets_no_fallback(self):
        llm._client = FakeClient([fake_message("end_turn", [])])
        asyncio.run(llm.call(run_id="r", stage="fact-checker", agent="fc", model=llm.HAIKU,
                             messages=[{"role": "user", "content": "hi"}]))
        self.assertNotIn("fallbacks", llm._client.requests[0])

    def test_run_agent_logs_each_turn_and_returns_tool_results(self):
        tool_call = NS(type="tool_use", id="tu_1", name="read_chunk", input={"chunk_id": "c1"})
        llm._client = FakeClient([
            fake_message("tool_use", [tool_call]),
            fake_message("end_turn", [NS(type="text", text="done")]),
        ])

        async def read_chunk(tool_input):
            return f"text of {tool_input['chunk_id']}"

        final = asyncio.run(llm.run_agent(
            run_id="r", stage="hunters", agent="redliner", tools=[],
            tool_handlers={"read_chunk": read_chunk},
            messages=[{"role": "user", "content": "go"}]))
        self.assertEqual(final.stop_reason, "end_turn")
        self.assertEqual([r["turn"] for r in self.rows()], ["1", "2"])
        sent = llm._client.requests[1]["messages"][-1]["content"][0]
        self.assertEqual(sent["content"], "text of c1")
        self.assertFalse(sent["is_error"])

    def test_unknown_tool_is_reported_as_error(self):
        tool_call = NS(type="tool_use", id="tu_1", name="nope", input={})
        llm._client = FakeClient([fake_message("tool_use", [tool_call]), fake_message("end_turn", [])])
        asyncio.run(llm.run_agent(run_id="r", stage="s", agent="a", tools=[], tool_handlers={},
                                  messages=[{"role": "user", "content": "go"}]))
        sent = llm._client.requests[1]["messages"][-1]["content"][0]
        self.assertTrue(sent["is_error"])


if __name__ == "__main__":
    unittest.main()
