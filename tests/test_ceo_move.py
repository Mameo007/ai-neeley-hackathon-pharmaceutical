"""Offline tests for pharmer/ceo_move.py - Sharpest Needle resolution, the move's validation, the
one-retry rule and the failed state, with a fake client (no API calls).

Run: .venv/bin/python -m unittest tests.test_ceo_move
"""
import asyncio
import json
import unittest
from types import SimpleNamespace as NS

import anthropic
import httpx2

from pharmer import ceo_move, llm, synthesizer
from tests.test_llm import FakeClient, fake_message
from tests.test_synthesizer import FINDINGS, RUN_JSON, StageTestCase, fixture_numbers, reply

VERDICT = "Rebate provisions of 222 limit growth, and the market has not priced them in."

GOOD = {"action": "Cut the inventory of 333 by slowing shipments to wholesalers.",
        "timeframe": "Start next quarter; finish within 9 months.",
        "trade_off": "Lower reported sales near term in exchange for a cleaner channel.",
        "downside_if_wrong": "If the build was demand-led, supply runs short; weekly orders "
                             "would show it.",
        "why_it_works": "Payers and investors see sales that match demand.",
        "citations": ["F-003-a", "F-002"]}


class CeoMoveTests(StageTestCase):
    def setUp(self):
        super().setUp()
        synthesizer.write_json(self.run_dir / "synthesis.json", {
            "status": "ok", "verdict": VERDICT, "priced_in_call": "not_priced_in",
            "citations": ["F-002-a"], "grid_used": False, "product": None, "error": None})

    def run_stage(self, *messages):
        llm._client = FakeClient(list(messages))
        asyncio.run(ceo_move.run(self.run_dir))
        return self.output("ceo_move.json"), llm._client.requests

    def write_run_json(self, obj):
        synthesizer.write_json(self.run_dir / "run.json", obj)

    def test_happy_path(self):
        out, requests = self.run_stage(reply(GOOD))
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["sharpest_needle"], "F-003")
        self.assertEqual(out["sharpest_needle_source"], "human")
        self.assertEqual(out["action"], GOOD["action"])
        self.assertEqual(out["citations"], ["F-003-a", "F-002"])
        self.assertTrue(out["verdict_used"])
        self.assertIsNone(out["error"])
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["model"], llm.OPUS)
        self.assertEqual(requests[0]["system"], ceo_move.SYSTEM)
        content = requests[0]["messages"][0]["content"]
        self.assertIn(VERDICT, content)
        self.assertIn("F-003 (named by the human analyst)", content)

    def test_needle_not_cited_retried(self):
        bad = {**GOOD, "citations": ["F-002-a"]}
        with self.assertLogs("pharmer.synthesizer", level="WARNING"):
            out, requests = self.run_stage(reply(bad), reply(GOOD))
        self.assertEqual(out["status"], "ok")
        self.assertIn("Sharpest Needle (F-003", requests[1]["messages"][0]["content"])
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[1]["system"], ceo_move.SYSTEM)

    def test_unselected_citation_rejected(self):
        bad = {**GOOD, "citations": ["F-003-a", "F-007-a"]}
        with self.assertLogs("pharmer.synthesizer", level="WARNING"):
            out, _ = self.run_stage(reply(bad), reply(bad))
        self.assertEqual(out["status"], "failed")
        self.assertIn("F-007-a", out["error"])
        self.assertIsNone(out["action"])
        self.assertEqual(out["sharpest_needle"], "F-003")  # the pick is still shown

    def test_non_action_rejected(self):
        for action in ["Investigate further the inventory build.", "  monitor rebates",
                       "Look into the channel.", "We will investigate the build.",
                       "Investigating the channel.", "The company should closely monitor it."]:
            with self.subTest(action=action):
                bad = {**GOOD, "action": action}
                with self.assertLogs("pharmer.synthesizer", level="WARNING"):
                    out, _ = self.run_stage(reply(bad), reply(bad))
                self.assertEqual(out["status"], "failed")
                self.assertIn("decision, not more research", out["error"])

    def test_invented_number_rejected_outside_timeframe(self):
        bad = {**GOOD, "trade_off": "Gives up 12% of near-term sales."}
        with self.assertLogs("pharmer.synthesizer", level="WARNING"):
            out, requests = self.run_stage(reply(bad), reply(GOOD))
        self.assertEqual(out["status"], "ok")
        self.assertIn("['12']", requests[1]["messages"][0]["content"])
        # "9 months" in GOOD's timeframe is not in the input, and passes.
        self.assertNotIn("9", synthesizer.numbers_in(requests[0]["messages"][0]["content"]))

    def test_empty_action_rejected(self):
        with self.assertLogs("pharmer.synthesizer", level="WARNING"):
            out, _ = self.run_stage(reply({**GOOD, "action": ""}), reply({**GOOD, "action": ""}))
        self.assertEqual(out["status"], "failed")
        self.assertIn("`action` must be a non-empty string", out["error"])

    def test_timeframe_only_exempts_time_expressions(self):
        good = {**GOOD, "timeframe": "Announce in Q3 2027; finish within 6-9 months."}
        bad = {**GOOD, "timeframe": "Within 9 months, freeing 400 of cash."}
        with self.assertLogs("pharmer.synthesizer", level="WARNING"):
            out, requests = self.run_stage(reply(bad), reply(good))
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["timeframe"], good["timeframe"])
        self.assertIn("['400']", requests[1]["messages"][0]["content"])

    def test_line_break_rejected(self):
        bad = {**GOOD, "trade_off": "Lower sales.\nCleaner channel."}
        with self.assertLogs("pharmer.synthesizer", level="WARNING"):
            out, _ = self.run_stage(reply(bad), reply(bad))
        self.assertIn("`trade_off` must have no line breaks", out["error"])

    def test_api_error_then_success(self):
        error = anthropic.APIConnectionError(request=httpx2.Request("POST", "https://x"))

        class FlakyClient(FakeClient):
            def _stream(self, **request):
                if not self.requests:
                    self.requests.append(request)
                    raise error
                return super()._stream(**request)

        llm._client = FlakyClient([reply(GOOD)])
        with self.assertLogs("pharmer.synthesizer", level="WARNING"):
            asyncio.run(ceo_move.run(self.run_dir))
        self.assertEqual(self.output("ceo_move.json")["status"], "ok")
        self.assertEqual(len(llm._client.requests), 2)

    def test_stale_synthesis_not_used(self):
        synthesizer.write_json(self.run_dir / "synthesis.json", {
            "status": "ok", "verdict": VERDICT, "citations": ["F-006-a"]})
        with self.assertLogs("pharmer.ceo_move", level="WARNING"):
            out, requests = self.run_stage(reply(GOOD))
        self.assertFalse(out["verdict_used"])
        self.assertNotIn(VERDICT, requests[0]["messages"][0]["content"])

    def test_missing_run_json_falls_back_to_recommended(self):
        (self.run_dir / "run.json").unlink()
        good = {**GOOD, "citations": ["F-001-a"]}
        with self.assertLogs("pharmer.ceo_move", level="WARNING"):
            out, requests = self.run_stage(reply(good))
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["sharpest_needle"], "F-001")
        self.assertEqual(out["sharpest_needle_source"], "recommended_fallback")
        self.assertIn("Recommended finding", requests[0]["messages"][0]["content"])

    def test_no_pick_yet_falls_back(self):
        for run_info in [{"product": "Illustrative Brand"}, {"sharpest_needle": None},
                         {"sharpest_needle": "  "}]:
            with self.subTest(run_info=run_info):
                self.write_run_json(run_info)
                good = {**GOOD, "citations": ["F-001"]}
                with self.assertLogs("pharmer.ceo_move", level="WARNING"):
                    out, _ = self.run_stage(reply(good))
                self.assertEqual(out["sharpest_needle_source"], "recommended_fallback")
                self.assertEqual(out["status"], "ok")

    def test_invalid_needle_fails_without_a_call(self):
        for pick in ["F-006", "F-001-a", "F-999", 3, ["F-003"], {"id": "F-003"}]:
            with self.subTest(pick=pick):
                self.write_run_json({"sharpest_needle": pick})
                with self.assertLogs("pharmer.ceo_move", level="WARNING"):
                    out, requests = self.run_stage()
                self.assertEqual(out["status"], "failed")
                self.assertIsNone(out["sharpest_needle"])
                self.assertIn("not one of the selected findings", out["error"])
                self.assertFalse(out["verdict_used"])
                self.assertEqual(requests, [])

    def test_missing_or_failed_synthesis(self):
        for synthesis in [None, {"status": "failed", "verdict": None}]:
            with self.subTest(synthesis=synthesis):
                if synthesis is None:
                    (self.run_dir / "synthesis.json").unlink(missing_ok=True)
                else:
                    synthesizer.write_json(self.run_dir / "synthesis.json", synthesis)
                with self.assertLogs("pharmer.ceo_move", level="WARNING"):
                    out, requests = self.run_stage(reply(GOOD))
                self.assertEqual(out["status"], "ok")
                self.assertFalse(out["verdict_used"])
                self.assertIn("unavailable", requests[0]["messages"][0]["content"])

    def test_refusal_then_malformed_gives_failed_state(self):
        malformed = fake_message("end_turn", [NS(type="text", text="[1, 2]")])
        with self.assertLogs("pharmer.synthesizer", level="WARNING"):
            out, requests = self.run_stage(fake_message("refusal", []), malformed)
        self.assertEqual(out["status"], "failed")
        self.assertIn("not a JSON object", out["error"])
        self.assertEqual(len(requests), 2)

    def test_user_message_numbers_come_from_the_fixture(self):
        _, requests = self.run_stage(reply(GOOD))
        content = requests[0]["messages"][0]["content"]
        allowed = fixture_numbers(FINDINGS, RUN_JSON) | synthesizer.numbers_in(VERDICT)
        self.assertLessEqual(synthesizer.numbers_in(content), allowed)


if __name__ == "__main__":
    unittest.main()
