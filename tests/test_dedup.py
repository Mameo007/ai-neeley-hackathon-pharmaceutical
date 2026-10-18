"""Offline tests for pharmer/dedup.py - anchor grouping, the judge verdict handling, merging and
renumbering, end to end with a fake client (no API calls).

Run: .venv/bin/python -m unittest tests.test_dedup
"""
import asyncio
import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

import anthropic
import httpx2

from pharmer import dedup, llm
from tests.test_llm import FakeStream, fake_message

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "dedup_leads.json"


def verdict(clusters, contested=False):
    text = json.dumps({"clusters": clusters, "contested": contested, "reason": "test"})
    return fake_message("end_turn", [NS(type="text", text=text)])


GOOD_REPLIES = {
    "trade_receivables": verdict([[1, 2]]),
    "revenue": verdict([[1], [2]]),
    "cost_of_sales": verdict([[2], [1]], contested=True),
}


class RoutingClient:
    """Answers each judge request by the anchor metric in its user message, so the reply does not
    depend on the order concurrent groups reach the client."""

    def __init__(self, replies):
        self.replies = replies
        self.requests = []
        self.beta = NS(messages=NS(stream=self._stream))

    def _stream(self, **request):
        self.requests.append(request)
        anchor = json.loads(request["messages"][0]["content"].split("\n")[2])
        return FakeStream(self.replies[dedup.anchor_key(anchor)[0]])


class DedupTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orig_log, self.orig_client = llm.LOG_PATH, llm._client
        llm.LOG_PATH = Path(self.tmp.name) / "logs" / "api_usage.csv"
        self.run_dir = Path(self.tmp.name) / "NVO-20261018-0230"
        self.run_dir.mkdir()
        shutil.copy(FIXTURE, self.run_dir / "leads.json")

    def tearDown(self):
        llm.LOG_PATH, llm._client = self.orig_log, self.orig_client
        self.tmp.cleanup()

    def run_dedup(self, bad_reply):
        llm._client = RoutingClient({**GOOD_REPLIES, "rebate_provisions": bad_reply})
        with self.assertLogs("pharmer.dedup", level="WARNING"):
            asyncio.run(dedup.run(self.run_dir))
        findings = json.loads((self.run_dir / "findings.json").read_text(encoding="utf-8"))
        return {f["anchor"]["metric"] + "/" + f["anchor"]["fiscal_period"]: f
                for f in findings}, findings


class AnchorKeyTests(unittest.TestCase):
    def test_case_spacing_and_suffix_ignored(self):
        a = {"metric": "Trade receivables", "entity": "novo  Nordisk A/S", "fiscal_period": "q2 2026"}
        b = {"metric": "trade_receivables", "entity": "Novo Nordisk", "fiscal_period": "Q2 2026"}
        self.assertEqual(dedup.anchor_key(a), dedup.anchor_key(b))
        c = {"metric": "trade-receivables", "entity": "Novo-Nordisk", "fiscal_period": "Q2-2026"}
        self.assertEqual(dedup.anchor_key(c), dedup.anchor_key(b))

    def test_periods_not_mapped(self):
        a = {"metric": "revenue", "entity": "Novo Nordisk", "fiscal_period": "H1 2026"}
        b = {**a, "fiscal_period": "Q2 2026"}
        self.assertNotEqual(dedup.anchor_key(a), dedup.anchor_key(b))

    def test_suffix_alone_is_kept(self):
        self.assertEqual(dedup.anchor_key({"entity": "AG"})[1], "ag")

    def test_letters(self):
        self.assertEqual([dedup._letters(i) for i in (0, 1, 25, 26)], ["a", "b", "z", "aa"])


class ParseVerdictTests(unittest.TestCase):
    def check_rejected(self, message, size=2):
        with self.assertLogs("pharmer.dedup", level="WARNING"):
            self.assertIsNone(dedup.parse_verdict(message, size))

    def test_valid(self):
        self.assertEqual(dedup.parse_verdict(verdict([[2], [1]], True), 2), ([[1], [0]], True))

    def test_rejects_refusal_and_truncation(self):
        self.check_rejected(fake_message("refusal", []))
        self.check_rejected(fake_message("max_tokens", [NS(type="text", text='{"clu')]))

    def test_rejects_malformed(self):
        self.check_rejected(fake_message("end_turn", [NS(type="text", text="not json")]))
        self.check_rejected(fake_message("end_turn", [NS(type="text", text="[1, 2]")]))
        self.check_rejected(verdict([[1]]))                    # lead 2 missing
        self.check_rejected(verdict([[1, 2], [2]]))            # lead 2 twice
        self.check_rejected(verdict([[1, 3]]))                 # out of range
        self.check_rejected(verdict([[1, 2], []]))             # empty cluster
        self.check_rejected(verdict([["1", 2]]))               # not integers
        self.check_rejected(verdict([[1, 2]], contested=True))  # one cluster can't contradict


class RunTests(DedupTestCase):
    def test_end_to_end_with_refused_group(self):
        by_anchor, findings = self.run_dedup(fake_message("refusal", []))
        requests = llm._client.requests

        # Six anchor groups in first-appearance order; four have 2+ leads -> four judge calls.
        self.assertEqual([f["finding_id"] for f in findings], [f"F-{n:03d}" for n in range(1, 7)])
        self.assertEqual([f["anchor"]["metric"] for f in findings],
                         ["inventory", "trade_receivables", "revenue", "cost_of_sales",
                          "trade_receivables", "rebate_provisions"])
        self.assertEqual(len(requests), 4)
        self.assertEqual({r["system"] for r in requests}, {dedup.SYSTEM})
        self.assertEqual({r["model"] for r in requests}, {llm.HAIKU})
        self.assertTrue(all(r["output_config"] == dedup.OUTPUT_CONFIG for r in requests))
        for f in findings:
            self.assertEqual(f["lead_reasoning_id"], f["reasonings"][0]["reasoning_id"])
            self.assertEqual([r["reasoning_id"] for r in f["reasonings"]],
                             [f"{f['finding_id']}-{dedup._letters(i)}"
                              for i in range(len(f["reasonings"]))])

        single = by_anchor["inventory/Q2 2026"]
        self.assertEqual(len(single["reasonings"]), 1)
        self.assertEqual(single["reasonings"][0]["hunters"], ["channel_checker"])
        self.assertEqual(by_anchor["trade_receivables/H1 2026"]["finding_id"], "F-005")

        dup = by_anchor["trade_receivables/Q2 2026"]
        [merged] = dup["reasonings"]
        self.assertEqual(merged["hunter"], "channel_checker")
        self.assertEqual(merged["hunters"], ["channel_checker", "redliner"])
        self.assertTrue(merged["claim"].startswith("Receivables grew far faster"))
        self.assertEqual([c["chunk_id"] for c in merged["citations"]],
                         ["NVO-6K-2026Q2-p28-c02", "NVO-6K-2026Q2-p14-c03"])
        self.assertFalse(dup["contested"])

        comp = by_anchor["revenue/Q2 2026"]
        self.assertEqual([r["hunter"] for r in comp["reasonings"]],
                         ["channel_checker", "footnote_miner"])
        self.assertFalse(comp["contested"])
        self.assertTrue(comp["is_label_finding"])
        self.assertEqual(comp["anchor"]["entity"], "Novo Nordisk")  # earliest lead's anchor

        cont = by_anchor["cost_of_sales/Q2 2026"]
        self.assertTrue(cont["contested"])
        self.assertEqual([r["hunter"] for r in cont["reasonings"]],
                         ["channel_checker", "redliner"])  # earliest-raised story first
        self.assertTrue(all("contested" in r["flags"] for r in cont["reasonings"]))

        refused = by_anchor["rebate_provisions/Q2 2026"]
        self.assertEqual([r["hunters"] for r in refused["reasonings"]],
                         [["channel_checker"], ["table_archaeologist"]])
        self.assertFalse(refused["contested"])
        self.assertEqual(refused["reasonings"][0]["flags"], [])

        # No citation is lost anywhere.
        leads = json.loads(FIXTURE.read_text(encoding="utf-8"))
        def cites(items):
            return {(c["chunk_id"], c["quote"]) for f in items for r in f["reasonings"]
                    for c in r["citations"]}
        self.assertEqual(cites(findings), cites(leads))

    def test_malformed_reply_keeps_reasonings_separate(self):
        bad = fake_message("end_turn", [NS(type="text", text='{"clusters": [[1]]}')])
        by_anchor, _ = self.run_dedup(bad)
        self.assertEqual(len(by_anchor["rebate_provisions/Q2 2026"]["reasonings"]), 2)

    def test_descending_cluster_keeps_earliest_claim_and_hunter(self):
        llm._client = RoutingClient({**GOOD_REPLIES, "trade_receivables": verdict([[2, 1]]),
                                     "rebate_provisions": verdict([[1], [2]])})
        asyncio.run(dedup.run(self.run_dir))
        findings = json.loads((self.run_dir / "findings.json").read_text(encoding="utf-8"))
        [merged] = findings[1]["reasonings"]
        self.assertEqual(merged["hunter"], "channel_checker")
        self.assertEqual(merged["hunters"], ["channel_checker", "redliner"])
        self.assertTrue(merged["claim"].startswith("Receivables grew far faster"))

    def test_api_error_keeps_reasonings_separate(self):
        error = anthropic.APIConnectionError(request=httpx2.Request("POST", "https://x"))

        class FailingStream(FakeStream):
            async def __aenter__(self):
                raise error

        class FailingClient(RoutingClient):
            def _stream(self, **request):
                if "rebate_provisions" in str(request["messages"]):
                    self.requests.append(request)
                    return FailingStream(None)
                return super()._stream(**request)

        llm._client = FailingClient(GOOD_REPLIES)
        with self.assertLogs("pharmer.dedup", level="WARNING"):
            asyncio.run(dedup.run(self.run_dir))
        findings = json.loads((self.run_dir / "findings.json").read_text(encoding="utf-8"))
        self.assertEqual(len(findings), 6)
        self.assertEqual(len(findings[5]["reasonings"]), 2)

    def test_value_mismatch_warns_and_anchor_is_normalized(self):
        leads = json.loads(FIXTURE.read_text(encoding="utf-8"))
        later = copy.deepcopy(leads[6])            # "Revenue" / "q2 2026", raised second
        later["anchor"]["value"] = 149000
        (self.run_dir / "leads.json").write_text(json.dumps([later, leads[2]]), encoding="utf-8")
        llm._client = RoutingClient({"revenue": verdict([[1], [2]])})
        with self.assertLogs("pharmer.dedup", level="WARNING") as logs:
            asyncio.run(dedup.run(self.run_dir))
        self.assertIn("differing value", "".join(logs.output))
        [finding] = json.loads((self.run_dir / "findings.json").read_text(encoding="utf-8"))
        self.assertEqual((finding["anchor"]["metric"], finding["anchor"]["fiscal_period"]),
                         ("revenue", "Q2 2026"))

    def test_empty_leads_makes_no_call(self):
        (self.run_dir / "leads.json").write_text("[]", encoding="utf-8")
        llm._client = RoutingClient({})
        asyncio.run(dedup.run(self.run_dir))
        self.assertEqual(json.loads((self.run_dir / "findings.json").read_text()), [])
        self.assertEqual(llm._client.requests, [])

    def test_singletons_make_no_call(self):
        leads = json.loads(FIXTURE.read_text(encoding="utf-8"))
        (self.run_dir / "leads.json").write_text(json.dumps([leads[0], leads[5]]),
                                                 encoding="utf-8")
        llm._client = RoutingClient({})
        asyncio.run(dedup.run(self.run_dir))
        findings = json.loads((self.run_dir / "findings.json").read_text())
        self.assertEqual([f["finding_id"] for f in findings], ["F-001", "F-002"])
        self.assertEqual(llm._client.requests, [])


if __name__ == "__main__":
    unittest.main()
