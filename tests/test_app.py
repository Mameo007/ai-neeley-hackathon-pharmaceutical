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
from pharmer import hunters, librarian

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
CASE = REPO_ROOT / "docs" / "neeley_ai_forward_case_study.md"
CASE_NAMES = {"MRK": "Merck", "BMY": "Bristol Myers Squibb", "PFE": "Pfizer", "LLY": "Eli Lilly",
              "NVO": "Novo Nordisk", "ABBV": "AbbVie", "JNJ": "Johnson & Johnson",
              "AZN": "AstraZeneca", "AMGN": "Amgen", "GILD": "Gilead"}  # as the appendix spells them
RUN_FILES = {"findings.json": "synthesis_findings.json", "run.json": "synthesis_run.json",
             "manifest.json": "report_manifest.json", "chunks.jsonl": "report_chunks.jsonl"}
def html_text(at):
    """Everything the page rendered through st.html."""
    return " ".join(e.proto.body for e in at.get("html"))


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


class AgentCardTests(AppTestCase):
    def test_every_pipeline_stage_and_hunter_has_a_plain_name(self):
        stages = ["corpus"] + [name for name, _, _ in orchestrator.START_STAGES
                               + orchestrator.FINISH_STAGES]
        self.assertEqual(list(app.STAGE_AGENTS), stages)  # same order as the run
        self.assertEqual(set(app.HUNTER_NAMES), {h.NAME for h in hunters.HUNTERS})

    def cards(self, stages, run_dir=None):
        name = "NVO-20261018-0100"
        run_dir = run_dir or (self.runs / name if (self.runs / name).is_dir()
                              else self.make_run(name, "running", "x", stages={}))
        return {c["name"]: c for c in app.agent_cards({"stages": stages}, run_dir)}

    def test_card_states_follow_the_stages(self):
        ok = {"status": "ok"}
        cards = self.cards({"corpus": ok, "hunters": ok, "dedup": {"status": "running"},
                            "priced_in": {"status": "disabled", "reason": "not built"}})
        # The two disabled agents are not this run's agents, so they are not cards at all.
        self.assertEqual(list(cards), ["Librarian", "The hunters", "Dedup", "Fact Checker",
                                       "Quant", "Scoring"])
        self.assertEqual([(c["status"], c["state"]) for c in cards.values()][:3],
                         [("ok", "✓"), ("ok", "✓"), ("running", "working")])
        self.assertEqual(cards["Quant"]["state"], "waiting")
        self.assertIn("Redliner", cards["The hunters"]["who"])
        self.assertAlmostEqual(app.progress_fraction(list(cards.values())), 2 / 6)

    def test_disabled_agents_are_not_cards_and_raise_no_warning(self):
        status = {"stages": {n: {"status": "disabled", "error": None,
                                 "reason": orchestrator.DISABLED_STAGES[n]}
                             for n in orchestrator.DISABLED_STAGES}}
        names = [c["name"] for c in app.agent_cards(status, self.runs, every=True)]
        for stage in orchestrator.DISABLED_STAGES:
            self.assertNotIn(app.STAGE_AGENTS[stage][1], names)
        # and no yellow "this agent did not succeed" banner for them
        self.assertEqual(app.stage_problems(status), [])

    def test_a_genuinely_skipped_stage_still_warns(self):
        status = {"stages": {"comparison": {"status": "skipped", "error": "not built"}}}
        self.assertEqual(app.stage_problems(status),
                         [("comparison", "skipped", "not built")])

    def test_scoring_card_spans_both_scoring_stages(self):
        self.assertEqual(self.cards({"scoring": {"status": "ok"}})["Scoring"]["status"],
                         "ok")  # first pass done; no pulse while Peer runs
        both = {"scoring": {"status": "ok"}, "scoring_after_peer": {"status": "ok"}}
        self.assertEqual(self.cards(both)["Scoring"]["status"], "ok")
        both["scoring_after_peer"] = {"status": "failed", "error": "x"}
        card = self.cards(both)["Scoring"]
        self.assertEqual((card["status"], card["state"]), ("failed", "error"))
        self.assertIn("Stopped with an error", card["problem"])

    def test_cards_after_the_job_ended(self):
        run_dir = self.make_run("NVO-20261018-0100", "failed", "x", stages={})
        status = {"phase": "failed", "stages": {"corpus": {"status": "ok"},
                                                "hunters": {"status": "running"}}}
        cards = {c["name"]: c for c in app.agent_cards(status, run_dir, live=False)}
        self.assertEqual((cards["The hunters"]["state"], cards["Dedup"]["state"]),
                         ("stopped", "not reached"))
        self.assertIn("Stopped mid-step", cards["The hunters"]["problem"])
        self.assertAlmostEqual(app.progress_fraction(list(cards.values())), 1 / 6)
        paused = {"phase": "awaiting_human",
                  "stages": {n: {"status": "ok"} for n in app.STAGE_AGENTS
                             if n not in app.FINISH_STAGE_NAMES}}
        every = app.agent_cards(paused, run_dir, live=False, every=True)
        self.assertEqual({c["state"] for c in every if c["group"] == "After your pick"},
                         {"waiting"})  # still ahead, not "not reached"
        self.assertAlmostEqual(app.progress_fraction(every), 6 / 10)

    def test_hunter_discovery_tolerates_bad_rows(self):
        run_dir = self.make_run("NVO-20261018-0100", "running", "x", stages={})
        (run_dir / "leads.json").write_text('[1, {"reasonings": [{}]}]', encoding="utf-8")
        self.assertEqual(self.cards({"hunters": {"status": "ok"}}, run_dir)["The hunters"]["found"],
                         "2 leads: unknown 2")

    def test_after_pick_cards_only_once_finishing(self):
        self.assertNotIn("CEO Move", self.cards({"scoring_after_peer": {"status": "ok"}}))
        cards = self.cards({"synthesizer": {"status": "degraded", "error": "no verdict"}})
        self.assertEqual(cards["Synthesizer"]["state"], "problem")
        self.assertEqual(cards["CEO Move"]["state"], "waiting")

    def test_discoveries_are_counts_from_the_run_files(self):
        run_dir = self.make_run("NVO-20261018-0100", "running", "x", stages={})
        lead = {"reasonings": [{"hunter": "channel_checker"}]}
        (run_dir / "leads.json").write_text(json.dumps([lead, lead, {"reasonings": [
            {"hunter": "gross_to_net"}]}]), encoding="utf-8")
        names = ["corpus", "hunters", "dedup", "fact_checker", "quant", "scoring",
                 "scoring_after_peer"]
        cards = self.cards({n: {"status": "ok"} for n in names}, run_dir)
        findings = json.loads((run_dir / "findings.json").read_text())
        manifest = json.loads((run_dir / "manifest.json").read_text())
        self.assertEqual(cards["Librarian"]["found"], app._n(len(manifest), "document")
                         + " in the library")
        self.assertEqual(cards["The hunters"]["found"],
                         "3 leads: Channel Checker 2, Gross-to-Net 1")
        self.assertEqual(cards["Dedup"]["found"], f"{len(findings)} distinct findings")
        self.assertEqual((app._n(1, "lead"), app._n(0, "lead")), ("1 lead", "0 leads"))
        selected = sum(f["status"] == "selected" for f in findings)
        self.assertEqual(cards["Scoring"]["found"],
                         app._n(selected, "finding") + " selected for your pick")
        self.assertTrue(cards["Fact Checker"]["found"].startswith("High "))
        self.assertRegex(cards["Quant"]["found"], rf"^\d+ of {len(findings)} findings sized")
        self.assertEqual(self.cards({"dedup": {"status": "running"}}, run_dir)["Dedup"]["found"],
                         "")  # only once the agent is done

    def test_cards_html_escapes(self):
        card = {"group": "Hunt", "name": "<x>", "job": "a & b", "status": "ok", "state": "✓",
                "who": "", "found": "<i>", "problem": ""}
        out = app.cards_html([card])
        self.assertIn("&lt;x&gt;", out)
        self.assertIn("a &amp; b", out)
        self.assertIn("↳ &lt;i&gt;", out)


class ResultTests(AppTestCase):
    ANCHOR = {"metric": "trade_receivables", "fiscal_period": "H1 2026", "value": 87123,
              "unit": "millions", "currency": "DKK"}

    def test_big_number_prefers_usd_then_eps_then_the_anchor(self):
        impact = {"impact": {"usd_m": -420.0, "eps_pct": -1.8}}
        self.assertEqual(app.big_number(impact, self.ANCHOR),
                         {"text": "−$420m", "basis": "quant",
                          "caption": "estimated impact, US dollars · −1.8% of earnings per share"})
        self.assertEqual(app.big_number({"impact": {"usd_m": 18100.0}}, self.ANCHOR)["text"],
                         "+$18.1bn")
        eps = app.big_number({"impact": {"usd_m": None, "eps_pct": 2.25}}, self.ANCHOR)
        self.assertEqual((eps["text"], eps["caption"]),
                         ("+2.25%", "estimated impact on earnings per share"))
        fallback = app.big_number({"impact": None}, self.ANCHOR)
        self.assertEqual((fallback["text"], fallback["basis"]), ("DKK 87,123m", "anchor"))
        self.assertEqual(fallback["caption"],
                         "trade receivables, H1 2026 · as reported; impact not sized yet")
        self.assertIsNone(app.big_number({}, {"metric": "x", "value": None}))  # never invented
        self.assertEqual(app.big_number({"impact": {"usd_m": 999.96}}, None)["text"], "+$1bn")
        self.assertEqual(app.big_number({"impact": {"usd_m": 999.4}}, None)["text"], "+$999.4m")

    def test_anchor_number_formats(self):
        self.assertEqual(app.anchor_number({"value": 1.5, "unit": "mg", "currency": "none"}),
                         "1.5 mg")
        self.assertEqual(app.anchor_number({"value": 12.0, "unit": "percent"}), "12%")
        self.assertEqual(app.anchor_number({"value": 2.5, "unit": "billions",
                                            "currency": "USD"}), "USD 2.5bn")
        self.assertIsNone(app.anchor_number({"value": True}))

    def test_strongest_reasoning_by_score_else_lead(self):
        f = {"lead_reasoning_id": "F-1-b", "reasonings": [
            {"reasoning_id": "F-1-a", "score": 0.3}, {"reasoning_id": "F-1-b", "score": 4.0}]}
        self.assertEqual(app._strongest(f)["reasoning_id"], "F-1-b")
        for r in f["reasonings"]:
            r["score"] = None
        self.assertEqual(app._strongest(f)["reasoning_id"], "F-1-b")
        self.assertEqual(app._strongest({"reasonings": []}), {})
        f["reasonings"][0].update(selected=True, score=0.3)
        f["reasonings"][1].update(selected=False, score=9.0)  # higher, but not selected
        self.assertEqual(app._strongest(f)["reasoning_id"], "F-1-a")

    def test_result_model_on_a_done_run(self):
        run_dir = self.make_run("NVO-20261018-0100", "done", "x", done=True)
        res = app.result_model(run_dir)
        self.assertEqual((res["needle"], res["headline"]),
                         ("F-003", "Illustrative: inventory 333 built up."))
        self.assertEqual(res["number"]["text"], "−2.5%")
        self.assertEqual(res["quote"], {"text": "Inventories 333",
                                        "source": "Novo Nordisk Q2 2026 6-K, p.14"})
        self.assertEqual(res["ceo"]["timeframe"], "next two quarters")
        self.assertEqual([k for k, _ in res["ceo"]["fields"]],
                         ["The move", "Trade-off", "What would prove us wrong", "Why it works"])
        self.assertEqual(res["priced_in"], "partly priced in")

    def test_result_page_failure_paths(self):
        run_dir = self.make_run("NVO-20261018-0100", "done", "x", done=True)
        (run_dir / "ceo_move.json").unlink()
        (run_dir / "run.json").write_text("{}", encoding="utf-8")  # AI-recommended fallback
        synthesis = json.loads((run_dir / "synthesis.json").read_text())
        synthesis["citations"] = ["F-999"]  # stale: cites a finding no longer selected
        (run_dir / "synthesis.json").write_text(json.dumps(synthesis), encoding="utf-8")
        at = PageTests.page(self, "NVO-20261018-0100")
        self.assertFalse(at.exception)
        self.assertIn("AI pick, not yet confirmed by the analyst", html_text(at))
        warnings = " ".join(w.value for w in at.warning)
        self.assertIn("No verdict: synthesis.json is out of date", warnings)
        self.assertIn("No CEO move: CEO Move has not run", warnings)
        (run_dir / "findings.json").write_text("{not json", encoding="utf-8")
        at = PageTests.page(self, "NVO-20261018-0100")
        self.assertIn("result files couldn't be read", at.error[0].value)
        self.assertEqual(at.expander[-1].label, "Details")  # diagnostics still shown

    def test_result_model_without_selected_findings(self):
        run_dir = self.make_run("NVO-20261018-0100", "done", "x", done=True)
        (run_dir / "findings.json").write_text("[]", encoding="utf-8")
        res = app.result_model(run_dir)
        self.assertIsNone(res["needle"])
        self.assertIsNone(res["number"])
        self.assertIsNone(res["quote"])

    def test_why_line_and_quote_html(self):
        r = {"materiality": 4.5, "priced_in_hits": 0}
        self.assertEqual(app.why_line(r),
                         "Moves earnings per share by about 4.5%; no matching news stories found.")
        self.assertEqual(app.why_line({"materiality": None, "priced_in_hits": None}),
                         "Impact not sized yet; not yet checked against the news.")
        self.assertIn("already in 1 news story", app.why_line({"materiality": 1,
                                                                "priced_in_hits": 1}))
        out = app.quote_html("$1.2 <b>bn</b>\nnext", "src & co")
        self.assertIn("“$1.2 &lt;b&gt;bn&lt;/b&gt;<br>next”", out)
        self.assertIn("src &amp; co", out)
        self.assertNotIn("\\", out)


class FriendlyErrorTests(AppTestCase):
    def test_usage_errors_in_plain_words(self):
        cases = {"runs/NVO-1 already exists; start again in a minute": "less than a minute ago",
                 "this run is already being processed": "already working",
                 "--needle F-9 is not a selected finding; choose one of F-1": "Pick another one",
                 "--corpus x must hold manifest.json": "Retry without them",
                 "something new": "could not be started"}
        for message, expected in cases.items():
            self.assertIn(expected, app.usage_error_text(message))
            self.assertNotIn("--", app.usage_error_text(message))

    def test_recovery_picks_one_button(self):
        run_dir = self.make_run("NVO-20261018-0100", "failed", "x", stages={})
        failed = {"status": "failed", "error": "boom"}
        cases = [("done", START_DONE, None),
                 ("failed", dict(START_DONE, synthesizer=failed), "finish"),
                 ("failed", {"corpus": failed}, "start_prebuilt"),
                 ("failed", {"corpus": {"status": "ok"}, "dedup": failed}, "start"),
                 ("awaiting_human", START_DONE, None)]
        (run_dir / "report.pdf").write_bytes(b"%PDF")
        for phase, stages, action in cases:
            fix = app.recovery(phase, {"stages": stages}, run_dir, "NVO")
            self.assertEqual(fix and fix["action"], action, (phase, stages))
        (run_dir / "report.pdf").unlink()
        self.assertEqual(app.recovery("done", {"stages": START_DONE}, run_dir, "NVO")["label"],
                         "Retry the report")
        amgn = app.recovery("failed", {"stages": {"corpus": failed}}, run_dir, "AMGN")
        self.assertEqual((amgn["label"], amgn["action"]), ("Retry", "start"))
        self.assertIn("Amgen's filings", amgn["text"])

    def page(self, run=None):
        return PageTests.page(self, run)

    def test_failed_sec_fetch_offers_prebuilt_filings(self):
        self.make_run("NVO-20261018-0100", "failed", "x",
                      stages={"corpus": {"status": "failed", "seconds": 1, "error": "HTTP 503"}})
        at = self.page("NVO-20261018-0100")
        self.assertEqual(at.error[0].value, "Couldn't collect Novo Nordisk's filings from SEC.")
        with mock.patch.object(orchestrator, "prepare_run",
                               return_value=self.runs / "NVO-X") as prepare, \
                mock.patch.object(app, "launch"):
            at.button(key="recover").click().run()
        self.assertEqual(prepare.call_args.kwargs["corpus"], REPO_ROOT / "data" / "sample" / "NVO")

    def test_failed_report_step_retries_with_the_same_pick(self):
        run_dir = self.make_run("NVO-20261018-0100", "failed", "x", stages=dict(
            START_DONE, synthesizer={"status": "failed", "seconds": 1, "error": "TypeError: x"}))
        at = self.page("NVO-20261018-0100")
        self.assertIn("Retry with the same pick, or change it below", at.error[0].value)
        self.assertNotIn("TypeError", at.error[0].value)
        with mock.patch.object(app, "launch") as launch:
            at.button(key="recover").click().run()
        self.assertFalse(at.exception)
        self.assertEqual(launch.call_args.args[0], run_dir)
        self.assertEqual(orchestrator.read_status(run_dir)["phase"], "running")
        self.assertEqual(json.loads((run_dir / "run.json").read_text())["sharpest_needle"],
                         "F-003")

    def test_crash_detail_moves_to_details(self):
        run_dir = self.make_run("NVO-20261018-0100", "failed", "x",
                                stages={"dedup": {"status": "failed", "seconds": 0, "error": "x"}})
        status = orchestrator.read_status(run_dir)
        status["error"] = "OSError: disk full"
        orchestrator._save(run_dir, status)
        at = self.page("NVO-20261018-0100")
        self.assertNotIn("OSError", " ".join(e.value for e in at.error))
        self.assertIn("Background job error: OSError: disk full",
                      " ".join(c.value for c in at.caption))

    def test_refused_start_shows_plain_text_and_retry(self):
        at = self.page()
        refusal = orchestrator.UsageError("unknown ticker 'NVO'")
        with mock.patch.object(orchestrator, "prepare_run", side_effect=refusal), \
                mock.patch.object(app, "launch") as launch:
            next(b for b in at.button if b.label.startswith("Find the needle")).click().run()
            self.assertEqual(at.error[0].value, "That request could not be started. Try again.")
            at.button(key="retry-start").click().run()
            self.assertEqual(len(at.error), 1)  # still refused, Retry still offered
        launch.assert_not_called()
        with mock.patch.object(orchestrator, "prepare_run",
                               return_value=self.runs / "NVO-X") as prepare, \
                mock.patch.object(app, "launch"):
            at.button(key="retry-start").click().run()
        prepare.assert_called_once()

    def test_same_minute_clash_opens_the_existing_run(self):
        self.make_run("NVO-20261018-0100", "running", "x")
        at = self.page()
        clash = orchestrator.UsageError(f"{self.runs / 'NVO-20261018-0100'} already exists; "
                                        "start again in a minute")
        with mock.patch.object(orchestrator, "prepare_run", side_effect=clash):
            next(b for b in at.button if b.label.startswith("Find the needle")).click().run()
        self.assertIn("less than a minute ago", at.error[0].value)
        self.assertEqual(len([b for b in at.button if b.key == "retry-start"]), 0)
        at.button(key="open-existing").click().run()
        self.assertEqual(at.selectbox(key="run").value, "NVO-20261018-0100")

    def test_stale_refusal_is_dropped_for_another_company(self):
        at = self.page()
        with mock.patch.object(orchestrator, "prepare_run",
                               side_effect=orchestrator.UsageError("boom")):
            next(b for b in at.button if b.label.startswith("Find the needle")).click().run()
        self.assertEqual(len(at.error), 1)
        at.button(key="pick-AMGN").click().run()
        self.assertEqual(len(at.error), 0)
        self.assertNotIn("start_error", at.session_state)

    def test_real_orchestrator_refusals_map_to_plain_text(self):
        run_dir = orchestrator.prepare_run("NVO")
        with self.assertRaises(orchestrator.UsageError) as clash:
            orchestrator.prepare_run("NVO")
        self.assertIn("less than a minute ago", app.usage_error_text(str(clash.exception)))
        with self.assertRaises(orchestrator.UsageError) as early:
            orchestrator.prepare_finish(run_dir)
        self.assertIn("already working", app.usage_error_text(str(early.exception)))
        status = orchestrator.read_status(run_dir)
        status["phase"] = "failed"
        orchestrator._save(run_dir, status)
        with self.assertRaises(orchestrator.UsageError) as unscored:
            orchestrator.prepare_finish(run_dir)
        self.assertIn("never finished reading", app.usage_error_text(str(unscored.exception)))

    def test_prebuilt_run_retries_with_prebuilt_filings(self):
        run_dir = self.make_run("NVO-20261018-0100", "failed", "x",
                                stages={"dedup": {"status": "failed", "seconds": 0, "error": "x"}})
        shutil.copy(REPO_ROOT / "data" / "sample" / "NVO" / "manifest.json",
                    run_dir / "manifest.json")
        fix = app.recovery("failed", orchestrator.read_status(run_dir), run_dir, "NVO")
        self.assertEqual((fix["label"], fix["action"]), ("Retry", "start_prebuilt"))
        self.assertFalse(app.used_prebuilt(run_dir, "AMGN"))

    def test_done_run_without_pdf_offers_the_report_retry(self):
        run_dir = self.make_run("NVO-20261018-0100", "done", "x", done=True)
        (run_dir / "report.pdf").unlink()
        at = self.page("NVO-20261018-0100")
        self.assertFalse(at.exception)
        self.assertEqual(at.button(key="recover").label, "Retry the report")
        self.assertIn("Sharpest Needle · F-003", html_text(at))  # result still shown

    def test_recover_refused_shows_plain_text(self):
        self.make_run("NVO-20261018-0100", "failed", "x",
                      stages={"dedup": {"status": "failed", "seconds": 0, "error": "x"}})
        at = self.page("NVO-20261018-0100")
        with mock.patch.object(orchestrator, "prepare_run",
                               side_effect=orchestrator.UsageError("boom")):
            at.button(key="recover").click().run()
        self.assertFalse(at.exception)
        self.assertIn("That request could not be started", at.error[-1].value)

    def test_interrupted_run_shows_one_box(self):
        self.make_run("NVO-20261018-0100", "running", "x")
        at = self.page("NVO-20261018-0100")
        self.assertEqual(len(at.error), 1)
        self.assertEqual(len([b for b in at.button if b.key == "recover"]), 0)


class CompanyTileTests(unittest.TestCase):
    def test_tiles_follow_the_shortlist_with_threads_and_ready_badge(self):
        tiles = app.company_tiles()
        self.assertEqual([t["ticker"] for t in tiles], list(librarian.CIKS))
        self.assertTrue(all(t["thread"] for t in tiles))
        self.assertEqual([t["ticker"] for t in tiles if t["ready"]], ["NVO"])
        self.assertEqual(app.default_company(tiles), "NVO")
        for tile in tiles:
            tile["ready"] = False
        self.assertEqual(app.default_company(tiles), "MRK")

    def test_missing_thread_file_gives_empty_threads(self):
        self.assertEqual(app.load_threads(REPO_ROOT / "no-such.json"), {})
        self.assertEqual(app.company_tiles(threads={})[0]["thread"], "")

    def test_tile_html_escapes_and_shows_the_badge_only_when_ready(self):
        tile = {"ticker": "JNJ", "name": "Johnson & Johnson", "thread": "<b>x</b>", "ready": False}
        out = app.tile_html(tile)
        self.assertIn("Johnson &amp; Johnson", out)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", out)
        self.assertNotIn("filings ready", out)
        self.assertIn("filings ready", app.tile_html(dict(tile, ready=True)))


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

    def test_every_starter_thread_is_in_the_case_document(self):
        text = CASE.read_text(encoding="utf-8")
        threads = app.load_threads()
        self.assertEqual(set(threads), set(librarian.CIKS))
        for ticker, thread in threads.items():
            self.assertTrue(thread, ticker)
            self.assertIn(f"* **{CASE_NAMES[ticker]}:** {thread}\n", text)  # its own company's line


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
        self.assertEqual(at.header[0].value, "Pick a company. Our agents read its filings.")
        self.assertEqual(len([b for b in at.button if b.key and b.key.startswith("pick-")]), 10)
        self.assertEqual(self.cta(at).label, "Find the needle in Novo Nordisk →")
        self.assertEqual(len(at.toggle), 1)  # NVO, selected first, has prebuilt filings

    def cta(self, at):
        return next(b for b in at.button if b.label.startswith("Find the needle in"))

    def test_clicking_a_tile_selects_the_company(self):
        at = self.page()
        at.button(key="pick-AMGN").click().run()
        self.assertFalse(at.exception)
        self.assertEqual(self.cta(at).label, "Find the needle in Amgen →")
        self.assertEqual(len(at.toggle), 0)  # no prebuilt filings for Amgen
        with mock.patch.object(orchestrator, "prepare_run",
                               return_value=self.runs / "AMGN-X") as prepare, \
                mock.patch.object(app, "launch"):
            self.cta(at).click().run()
        self.assertEqual(prepare.call_args.args[0], "AMGN")
        self.assertIsNone(prepare.call_args.kwargs["corpus"])

    def test_prebuilt_toggle_does_not_follow_to_a_company_without_filings(self):
        at = self.page()
        at.toggle[0].set_value(True).run()
        self.assertEqual(at.button(key="pick-NVO").label, "Novo Nordisk (selected)")
        at.button(key="pick-AMGN").click().run()
        with mock.patch.object(orchestrator, "prepare_run",
                               return_value=self.runs / "AMGN-X") as prepare, \
                mock.patch.object(app, "launch"):
            self.cta(at).click().run()
        self.assertEqual(prepare.call_args.args[0], "AMGN")
        self.assertIsNone(prepare.call_args.kwargs["corpus"])

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
        self.assertEqual(at.header[0].value, "Pick a company. Our agents read its filings.")

    def test_paused_run_shows_review_and_pick(self):
        self.make_run("NVO-20261018-0100", "awaiting_human", "x")
        at = self.page("NVO-20261018-0100")
        self.assertFalse(at.exception)
        self.assertEqual(at.radio[0].value, "F-003")
        product = next(s for s in at.selectbox if s.label == "Product")
        self.assertEqual(product.value, app.NOT_CONFIRMED)
        self.assertEqual(product.options, app.product_options("NVO"))
        self.assertIn("Novo Nordisk Q2 2026 6-K, p.14", html_text(at))  # source chip, verbatim
        self.assertIn("“Trade receivables 111”", html_text(at))  # serif quote, no backslashes
        self.assertIn("How we scored it", [e.label for e in at.expander])

    def test_done_run_shows_verdict(self):
        self.make_run("NVO-20261018-0100", "done", "x", done=True)
        at = self.page("NVO-20261018-0100")
        self.assertFalse(at.exception)
        text = html_text(at)
        self.assertIn("Sharpest Needle · F-003", text)
        self.assertIn("Illustrative: inventory 333 built up.", text)  # the needle's claim
        self.assertIn("estimated impact on earnings per share", text)  # F-003-a eps_pct -2.5
        self.assertIn("Illustrative: rebate provisions of 222", text)  # Synthesizer verdict
        self.assertIn("CEO decision · next two quarters", text)
        self.assertIn("Illustrative: cut channel inventory of 333", text)
        self.assertEqual(at.get("download_button")[0].proto.label,
                         "⬇ Download the CEO report (PDF)")

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
        self.assertEqual(at.error[0].value,
                         "This run is already working. Wait for it to finish, then try again.")

    def test_start_uses_prebuilt_only_when_toggled(self):
        for toggled, expected in ((False, None), (True, REPO_ROOT / "data" / "sample" / "NVO")):
            at = self.page()
            at.toggle[0].set_value(toggled)
            with mock.patch.object(orchestrator, "prepare_run",
                                   return_value=self.runs / "NVO-X") as prepare, \
                    mock.patch.object(app, "launch"):
                self.cta(at).click().run()
            self.assertEqual(prepare.call_args.kwargs["corpus"], expected)

    def test_running_run_shows_progress(self):
        run_dir = self.make_run("NVO-20261018-0100", "running", "x",
                                stages={"hunters": {"status": "running", "seconds": 1.0,
                                                    "error": None}})
        with mock.patch.object(app, "live_runs", return_value=frozenset({str(run_dir)})), \
                mock.patch.object(app, "agents_section") as cards:
            at = self.page("NVO-20261018-0100")
        self.assertFalse(at.exception)
        self.assertIn("Working", at.info[0].value)
        self.assertEqual(len(at.radio), 0)
        self.assertEqual(cards.call_args.args[0], run_dir)  # the cards replace the stage table
        self.assertEqual(at.expander[-1].label, "Details")

    def test_interrupted_run_offers_the_retry_mark(self):
        self.make_run("NVO-20261018-0100", "running", "x")
        at = self.page("NVO-20261018-0100")
        self.assertFalse(at.exception)
        self.assertEqual(len(at.radio), 0)
        self.assertIn("stopped when the app restarted", at.error[0].value)
        next(b for b in at.button if b.label == "Mark as failed").click().run()
        self.assertEqual(orchestrator.read_status(self.runs / "NVO-20261018-0100")["phase"],
                         "failed")
        self.assertEqual(at.radio[0].value, "F-003")  # the pick form is back

    def test_failed_start_shows_no_pick(self):
        self.make_run("NVO-20261018-0100", "failed", "x",
                      stages={"dedup": {"status": "failed", "seconds": 0.1, "error": "boom"}})
        at = self.page("NVO-20261018-0100")
        self.assertFalse(at.exception)
        self.assertEqual(len(at.radio), 0)
        self.assertIn("**Dedup**: stopped with an error", at.warning[0].value)
        self.assertNotIn("boom", at.warning[0].value)  # the raw error stays in Details


if __name__ == "__main__":
    unittest.main()
