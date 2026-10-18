"""Offline tests for pharmer/hunters - corpus tools on the NVO sample, lead validation, and the
tool loop end to end with a fake client (no API calls).

Run: .venv/bin/python -m unittest tests.test_hunters
"""
import asyncio
import json
import shutil
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from types import SimpleNamespace as NS

from pharmer import hunters, llm
from pharmer.hunters import channel_checker, harness
from tests.test_llm import FakeClient, fake_message

SAMPLE = llm.REPO_ROOT / "data" / "sample" / "NVO"
BALANCE_CHUNK = "NVO-6K-2026Q2-p28-c02"
QUOTE = "Trade receivables 88,949 70,856"


def tool_use(block_id, name, args):
    return NS(type="tool_use", id=block_id, name=name, input=args)


def lead(chunk_id=BALANCE_CHUNK, quote=QUOTE, is_label_finding=False, **anchor):
    return {
        "anchor": {"metric": "trade_receivables", "entity": "Novo Nordisk",
                   "fiscal_period": "Q2 2026", "value": 88949, "unit": "millions",
                   "currency": "DKK", **anchor},
        "claim": "Receivables rose from DKK 70,856m to DKK 88,949m in six months.",
        "citations": [{"chunk_id": chunk_id, "quote": quote}],
        "is_label_finding": is_label_finding,
    }


def end_turn():
    return fake_message("end_turn", [NS(type="text", text="done")])


def tool_results(client, turn):
    """tool_result blocks the harness sent back after `turn` (the request keeps one live list)."""
    return client.requests[-1]["messages"][2 * turn]["content"]


class HunterTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orig_log, self.orig_client = llm.LOG_PATH, llm._client
        llm.LOG_PATH = Path(self.tmp.name) / "logs" / "api_usage.csv"
        self.run_dir = Path(self.tmp.name) / "NVO-20261018-0230"
        self.run_dir.mkdir()
        for name in ("manifest.json", "chunks.jsonl", "facts.jsonl"):
            shutil.copy(SAMPLE / name, self.run_dir / name)
        self.corpus = harness.load_corpus(self.run_dir)

    def tearDown(self):
        llm.LOG_PATH, llm._client = self.orig_log, self.orig_client
        self.tmp.cleanup()


class CorpusToolTests(HunterTestCase):
    def test_search_finds_balance_sheet_with_snippet(self):
        hits = harness.search_chunks(self.corpus, "trade receivables balance sheet")
        self.assertLessEqual(len(hits), harness.SEARCH_LIMIT)
        self.assertIn(BALANCE_CHUNK, [h["chunk_id"] for h in hits])
        self.assertTrue(all(len(h["snippet"]) <= harness.SNIPPET_CHARS for h in hits))

    def test_search_doc_filter_and_no_match(self):
        hits = harness.search_chunks(self.corpus, "receivables", doc_id="NVO-6K-2025Q2")
        self.assertTrue(hits)
        self.assertEqual({h["doc_id"] for h in hits}, {"NVO-6K-2025Q2"})
        self.assertEqual(harness.search_chunks(self.corpus, "zzqxv"), [])

    def test_read_chunks_reports_missing_and_rejects_all_unknown(self):
        result = harness.read_chunks(self.corpus, [BALANCE_CHUNK, "NVO-nope-p1-c01"])
        self.assertEqual([c["chunk_id"] for c in result["chunks"]], [BALANCE_CHUNK])
        self.assertEqual(result["missing"], ["NVO-nope-p1-c01"])
        with self.assertRaises(ValueError):
            harness.read_chunks(self.corpus, ["NVO-nope-p1-c01"])

    def test_get_facts_filters(self):
        facts = harness.get_facts(self.corpus, metric="trade_receivables", fiscal_period="FY2024")
        self.assertEqual([f["value"] for f in facts], [71949])
        self.assertEqual(len(harness.get_facts(self.corpus)), len(self.corpus.facts))


class ValidationTests(HunterTestCase):
    def test_valid_lead_has_no_problems(self):
        self.assertEqual(harness.validate_leads(self.corpus, [lead()]), [])

    def test_unknown_chunk_and_paraphrased_quote_are_rejected(self):
        problems = harness.validate_leads(self.corpus, [
            lead(chunk_id="NVO-nope-p1-c01"),
            lead(quote="Trade receivables 88949"),
        ])
        self.assertEqual(len(problems), 2)
        self.assertIn("no such chunk_id", problems[0])
        self.assertIn("exact substring", problems[1])

    def test_missing_anchor_field_and_non_numeric_value(self):
        problems = harness.validate_leads(self.corpus, [lead(currency=""), lead(value="88,949")])
        self.assertIn("anchor missing currency", problems[0])
        self.assertIn("must be a number", problems[1])

    def test_empty_quote_is_rejected(self):
        problems = harness.validate_leads(self.corpus, [lead(quote="")])
        self.assertTrue(any("exact substring" in p for p in problems))

    def test_anchor_value_must_appear_in_a_quote(self):
        self.assertEqual(harness.validate_leads(self.corpus, [lead(value=88949.0)]), [])
        [problem] = harness.validate_leads(self.corpus, [lead(value=99999)])
        self.assertIn("no citation quote contains anchor.value", problem)
        [problem] = harness.validate_leads(self.corpus, [lead(quote="Trade", value=88949)])
        self.assertIn("anchor.value", problem)

    def test_is_label_finding_must_be_boolean(self):
        [problem] = harness.validate_leads(self.corpus, [lead(is_label_finding="false")])
        self.assertIn("is_label_finding", problem)


class RunTests(HunterTestCase):
    def script(self, *messages):
        llm._client = FakeClient(list(messages))
        return llm._client

    def test_run_writes_contract2_leads(self):
        client = self.script(
            fake_message("tool_use", [
                tool_use("t1", "search_chunks", {"query": "trade receivables"}),
                tool_use("t2", "get_facts", {"metric": "revenue"})]),
            fake_message("tool_use", [tool_use("t3", "read_chunks", {"chunk_ids": [BALANCE_CHUNK]})]),
            fake_message("tool_use", [tool_use("t4", "submit_leads", {"leads": [
                lead(), lead(is_label_finding=True)]})]),
            end_turn(),
        )
        # One scripted conversation, so one hunter (the swarm shares the fake client's queue).
        with mock.patch.object(hunters, "HUNTERS", [channel_checker]):
            asyncio.run(hunters.run(self.run_dir))

        first_lead, second_lead = json.loads((self.run_dir / "leads.json").read_text())
        self.assertEqual([first_lead["finding_id"], second_lead["finding_id"]], ["F-001", "F-002"])
        self.assertEqual(second_lead["reasonings"][0]["reasoning_id"], "F-002-a")
        self.assertTrue(second_lead["is_label_finding"])
        finding = first_lead
        self.assertEqual(finding["ticker"], "NVO")
        self.assertEqual(finding["anchor"], lead()["anchor"])
        self.assertFalse(finding["is_label_finding"])
        self.assertIsNone(finding["lead_reasoning_id"])
        self.assertEqual(finding["status"], "candidate")
        self.assertFalse(finding["recommended"])
        [reasoning] = finding["reasonings"]
        self.assertEqual(reasoning["reasoning_id"], "F-001-a")
        self.assertEqual(reasoning["hunter"], "channel_checker")
        self.assertEqual(reasoning["claim"], lead()["claim"])
        self.assertEqual(reasoning["citations"], [{"chunk_id": BALANCE_CHUNK, "quote": QUOTE}])
        self.assertIsNone(reasoning["confidence"])

        first = client.requests[0]
        self.assertEqual(first["model"], llm.OPUS)
        self.assertIn(channel_checker.BRIEF, first["messages"][0]["content"])
        self.assertTrue(all(not r["is_error"] for r in tool_results(client, 1)))

    def test_failing_hunter_does_not_abort_the_run(self):
        async def boom(*args, **kwargs):
            raise RuntimeError("overloaded")
        orig = llm.run_agent
        llm.run_agent = boom
        try:
            with self.assertLogs("pharmer.hunters", "ERROR"):
                asyncio.run(hunters.run(self.run_dir))
        finally:
            llm.run_agent = orig
        self.assertEqual(json.loads((self.run_dir / "leads.json").read_text()), [])

    def test_bad_citation_is_reported_then_fixed(self):
        client = self.script(
            fake_message("tool_use", [tool_use("t1", "submit_leads",
                                               {"leads": [lead(quote="made up")]})]),
            fake_message("tool_use", [tool_use("t2", "submit_leads", {"leads": [lead()]})]),
            end_turn(),
        )
        findings = asyncio.run(harness.run_hunter("NVO-test", self.corpus, "channel_checker", "b"))
        [result] = tool_results(client, 1)
        self.assertTrue(result["is_error"])
        self.assertIn("nothing recorded", result["content"])
        self.assertEqual(len(findings), 1)

    def test_no_submission_and_empty_submission_give_no_leads(self):
        self.script(end_turn())
        with self.assertLogs("pharmer.hunters.harness", "WARNING"):
            self.assertEqual(asyncio.run(harness.run_hunter(
                "NVO-test", self.corpus, "channel_checker", "b")), [])
        self.script(fake_message("tool_use", [tool_use("t1", "submit_leads", {"leads": []})]),
                    end_turn())
        self.assertEqual(asyncio.run(harness.run_hunter(
            "NVO-test", self.corpus, "channel_checker", "b")), [])

    def test_turn_cap_keeps_last_valid_submission(self):
        submit = fake_message("tool_use", [tool_use("t1", "submit_leads", {"leads": [lead()]})])
        self.script(*[submit] * harness.MAX_TURNS)
        with self.assertLogs("pharmer.hunters.harness", "WARNING") as logs:
            findings = asyncio.run(harness.run_hunter(
                "NVO-test", self.corpus, "channel_checker", "b"))
        self.assertIn("turn cap", "\n".join(logs.output))
        self.assertEqual(len(findings), 1)

    def test_unknown_tool_and_bad_args_are_reported_to_model(self):
        client = self.script(
            fake_message("tool_use", [tool_use("t1", "read_chunks", {"chunk_ids": ["nope"]}),
                                      tool_use("t2", "web_search", {"query": "x"})]),
            end_turn())
        with self.assertLogs("pharmer.hunters.harness", "WARNING"):
            asyncio.run(harness.run_hunter("NVO-test", self.corpus, "channel_checker", "b"))
        results = tool_results(client, 1)
        self.assertEqual([r["is_error"] for r in results], [True, True])
        self.assertIn("unknown tool", results[1]["content"])

    def test_system_and_tools_byte_identical_across_hunters(self):
        client = self.script(end_turn(), end_turn())
        with self.assertLogs("pharmer.hunters.harness", "WARNING"):
            asyncio.run(harness.run_hunter("NVO-a", self.corpus, "channel_checker", "brief one"))
            asyncio.run(harness.run_hunter("NVO-b", self.corpus, "redliner", "brief two"))
        a, b = client.requests
        self.assertEqual(json.dumps(a["system"]), json.dumps(b["system"]))
        self.assertEqual(json.dumps(a["tools"]), json.dumps(b["tools"]))
        self.assertNotIn("brief one", json.dumps(a["system"]))
        self.assertEqual(a["system"][0]["cache_control"], {"type": "ephemeral"})


if __name__ == "__main__":
    unittest.main()


    def test_label_lawyer_lead_cites_own_label(self):
        from pharmer.hunters import label_lawyer
        label = lead(chunk_id="NVO-LABEL-OZEMPIC-p4-c03", quote="1 mg 1 mg / 0.5 mL",
                     is_label_finding=True, metric="max_dose_per_injection", entity="OZEMPIC",
                     value=1, unit="mg", currency="none")
        client = self.script(
            fake_message("tool_use", [tool_use("t1", "submit_leads", {"leads": [label]})]),
            end_turn())
        [finding] = asyncio.run(harness.run_hunter(
            "NVO-test", self.corpus, label_lawyer.NAME, label_lawyer.BRIEF))
        self.assertTrue(finding["is_label_finding"])
        self.assertEqual(finding["reasonings"][0]["hunter"], "label_lawyer")
        # The overview tells own labels from rival labels by ticker.
        overview = client.requests[0]["messages"][0]["content"]
        self.assertIn("NVO-LABEL-OZEMPIC: NVO label", overview)
        self.assertIn("LLY-LABEL-MOUNJARO: LLY label", overview)


class HunterRegistryTests(unittest.TestCase):
    """Every hunter module follows the Channel Checker pattern (offline; briefs not yet run
    against the real API)."""

    def test_each_hunter_has_matching_name_brief_and_registration(self):
        from pharmer import hunters
        names = [h.NAME for h in hunters.HUNTERS]
        self.assertEqual(names, ["channel_checker", "redliner", "table_archaeologist",
                                 "footnote_miner", "gross_to_net", "label_lawyer"])
        for hunter in hunters.HUNTERS:
            self.assertEqual(hunter.__name__, f"pharmer.hunters.{hunter.NAME}")
            self.assertTrue(hunter.BRIEF.startswith("# "), hunter.NAME)
            self.assertIn("fiscal_period", hunter.BRIEF)  # the Dedup anchor rule
            self.assertNotIn("Novo", hunter.BRIEF)  # no company-specific priming
        self.assertIn("`is_label_finding` to true", hunters.label_lawyer.BRIEF)
