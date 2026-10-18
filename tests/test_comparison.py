"""Offline tests for pharmer/comparison.py - one per row of the spec's I/O matrix, plus the cell
checks and an acceptance run on the committed NVO sample corpus, with a fake client (no API
calls).

Run: .venv/bin/python -m unittest tests.test_comparison
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

from pharmer import comparison, llm, report
from tests.test_llm import FakeClient, FakeStream, fake_message

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
SAMPLE = REPO_ROOT / "data" / "sample" / "NVO"
ND = comparison.NOT_DISCLOSED
LABELS = [label for _, label in comparison.DIMENSIONS]

MANIFEST = [
    {"doc_id": "XX-6K-2026Q2", "doc_type": "6-K", "ticker": "XX"},
    {"doc_id": "XX-LABEL-BRANDA", "doc_type": "label", "ticker": "XX"},
    {"doc_id": "YY-LABEL-BRANDB", "doc_type": "label", "ticker": "YY"},
    {"doc_id": "YY-10Q-2026Q2", "doc_type": "10-Q", "ticker": "YY"},
]
CHUNKS = [
    {"chunk_id": "XX-6K-2026Q2-p3-c01", "doc_id": "XX-6K-2026Q2", "page": 3,
     "section": "Sales", "is_table": False,
     "text": "Brand A gross-to-net deductions rose to 61% of gross sales."},
    {"chunk_id": "XX-6K-2026Q2-p4-c01", "doc_id": "XX-6K-2026Q2", "page": 4,
     "section": "Other", "is_table": False, "text": "Unrelated text about the company."},
    {"chunk_id": "XX-6K-2026Q2-p5-c01", "doc_id": "XX-6K-2026Q2", "page": 5,
     "section": "Competition", "is_table": False, "text": "Brand B competes with Brand A."},
    {"chunk_id": "YY-10Q-2026Q2-p7-c01", "doc_id": "YY-10Q-2026Q2", "page": 7,
     "section": "Revenue", "is_table": False, "text": "Brand B revenue grew."},
    {"chunk_id": "XX-LABEL-BRANDA-p1-c01", "doc_id": "XX-LABEL-BRANDA", "page": 1,
     "section": "WARNING: RISK X", "is_table": False, "text": "WARNING: RISK X\nIllustrative."},
    {"chunk_id": "XX-LABEL-BRANDA-p2-c01", "doc_id": "XX-LABEL-BRANDA", "page": 2,
     "section": "1 INDICATIONS AND USAGE", "is_table": False,
     "text": "1 INDICATIONS AND USAGE\nBRAND A® is indicated for illustrative condition Q."},
    {"chunk_id": "XX-LABEL-BRANDA-p3-c01", "doc_id": "XX-LABEL-BRANDA", "page": 3,
     "section": "2 DOSAGE AND ADMINISTRATION", "is_table": False,
     "text": "2 DOSAGE AND ADMINISTRATION\nAdminister once weekly. Start at 0.25 mg."},
    {"chunk_id": "YY-LABEL-BRANDB-p1-c01", "doc_id": "YY-LABEL-BRANDB", "page": 1,
     "section": "1 INDICATIONS AND USAGE", "is_table": False,
     "text": "1 INDICATIONS AND USAGE\nBRAND B is indicated for illustrative condition R."},
]


def cell(value=ND, chunk_id="", quote=""):
    return {"value": value, "chunk_id": chunk_id, "quote": quote}


def column(**cells):
    return {key: cells.get(key, cell()) for key, _ in comparison.DIMENSIONS}


GOOD_A = column(
    approved_uses=cell("Illustrative condition Q", "XX-LABEL-BRANDA-p2-c01",
                       "BRAND A® is indicated for illustrative condition Q"),
    dosing_convenience=cell("Once weekly; start 0.25 mg", "XX-LABEL-BRANDA-p3-c01",
                            "Administer once weekly. Start at 0.25 mg."),
    pricing_gross_to_net=cell("Gross-to-net deductions 61% of gross sales",
                              "XX-6K-2026Q2-p3-c01",
                              "gross-to-net deductions rose to 61% of gross sales"))
GOOD_B = column(approved_uses=cell("Illustrative condition R", "YY-LABEL-BRANDB-p1-c01",
                                   "BRAND B is indicated for illustrative condition R"))


def reply(obj):
    return fake_message("end_turn", [NS(type="text", text=json.dumps(obj))])


class RoutingClient(FakeClient):
    """Answers each request from the queue of the product named in its user message, so the
    parallel columns cannot steal each other's replies. A queued exception is raised."""

    def __init__(self, by_product):
        super().__init__([])
        self.by_product = {k: list(v) for k, v in by_product.items()}

    def _stream(self, **request):
        self.requests.append(request)
        product = request["messages"][0]["content"].split("\n")[2]
        item = self.by_product[product].pop(0)
        if isinstance(item, Exception):
            raise item
        return FakeStream(item)


class StageTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orig_log, self.orig_client = llm.LOG_PATH, llm._client
        llm.LOG_PATH = Path(self.tmp.name) / "logs" / "api_usage.csv"
        self.run_dir = Path(self.tmp.name) / "XX-20261018-0230"
        self.run_dir.mkdir()
        (self.run_dir / "manifest.json").write_text(json.dumps(MANIFEST), encoding="utf-8")
        (self.run_dir / "chunks.jsonl").write_text(
            "".join(json.dumps(c) + "\n" for c in CHUNKS), encoding="utf-8")
        self.run_json({"product": "Brand A", "rivals": ["Brand B"]})

    def tearDown(self):
        llm.LOG_PATH, llm._client = self.orig_log, self.orig_client
        self.tmp.cleanup()

    def run_json(self, obj):
        (self.run_dir / "run.json").write_text(json.dumps(obj), encoding="utf-8")

    def output(self, name):
        return json.loads((self.run_dir / name).read_text(encoding="utf-8"))

    def run_stage(self, by_product, logs=True):
        llm._client = RoutingClient(by_product)
        if logs:
            with self.assertLogs("pharmer", level="INFO") as cm:
                asyncio.run(comparison.run(self.run_dir))
            self.log = "\n".join(cm.output)
        else:
            asyncio.run(comparison.run(self.run_dir))
        return llm._client.requests

    def grid(self):
        out = self.output("comparison.json")
        return {row[0]: row[1:] for row in out["rows"]}, out


class IoMatrixTests(StageTestCase):
    def test_happy_path(self):
        requests = self.run_stage({"Brand A": [reply(GOOD_A)], "Brand B": [reply(GOOD_B)]})
        grid, out = self.grid()
        self.assertEqual(out["columns"], ["Dimension", "Brand A", "Brand B"])
        self.assertEqual([row[0] for row in out["rows"]], LABELS)
        self.assertEqual(grid["Approved uses"],
                         ["Illustrative condition Q", "Illustrative condition R"])
        self.assertEqual(grid["Dosing / convenience"], ["Once weekly; start 0.25 mg", ND])
        self.assertEqual(grid["Boxed warning / safety"], [ND, ND])
        self.assertEqual(len(requests), 2)
        for r in requests:
            self.assertEqual(r["model"], llm.OPUS)
            self.assertEqual(r["system"], comparison.SYSTEM)
            self.assertEqual(r["output_config"], comparison.OUTPUT_CONFIG)
        sources = self.output("comparison_sources.json")
        self.assertEqual((sources["product"], sources["rivals"]), ("Brand A", ["Brand B"]))
        a = sources["columns"]["Brand A"]
        self.assertEqual(a["status"], "ok")
        self.assertEqual(a["cells"]["Approved uses"],
                         {"chunk_id": "XX-LABEL-BRANDA-p2-c01", "doc_type": "label",
                          "quote": "BRAND A® is indicated for illustrative condition Q"})
        self.assertIsNone(a["cells"]["Boxed warning / safety"])
        self.assertEqual(a["rejected"], [])

    def test_each_column_sees_only_its_chunks(self):
        requests = self.run_stage({"Brand A": [reply(GOOD_A)], "Brand B": [reply(GOOD_B)]})
        by = {r["messages"][0]["content"].split("\n")[2]: r["messages"][0]["content"]
              for r in requests}
        self.assertIn("XX-LABEL-BRANDA-p1-c01", by["Brand A"])  # the whole label, not just hits
        self.assertIn("XX-6K-2026Q2-p3-c01", by["Brand A"])  # filing chunk naming it
        self.assertNotIn("XX-6K-2026Q2-p4-c01", by["Brand A"])
        self.assertNotIn("YY-LABEL-BRANDB", by["Brand A"])
        self.assertNotIn("XX-LABEL-BRANDA", by["Brand B"])
        # Label chunks come before filing chunks.
        self.assertLess(by["Brand A"].index("XX-LABEL-BRANDA-p1-c01"),
                        by["Brand A"].index("XX-6K-2026Q2-p3-c01"))

    def test_rival_filings_only_from_its_label_holder(self):
        requests = self.run_stage({"Brand A": [reply(GOOD_A)], "Brand B": [reply(GOOD_B)]})
        by = {r["messages"][0]["content"].split("\n")[2]: r["messages"][0]["content"]
              for r in requests}
        self.assertIn("YY-10Q-2026Q2-p7-c01", by["Brand B"])
        self.assertNotIn("XX-6K-2026Q2-p5-c01", by["Brand B"])  # XX mentions its rival
        self.assertIn("XX-6K-2026Q2-p5-c01", by["Brand A"])

    def test_no_label_keeps_every_filing_naming_it(self):
        manifest = [m for m in MANIFEST if m["doc_type"] != "label"]
        docs = {m["doc_id"] for m in manifest}
        chunks = [c for c in CHUNKS if c["doc_id"] in docs]
        ids = [c["chunk_id"] for c in comparison.select_chunks("Brand B", manifest, chunks)]
        self.assertEqual(ids, ["XX-6K-2026Q2-p5-c01", "YY-10Q-2026Q2-p7-c01"])

    def test_cell_citing_a_chunk_not_sent_to_its_column_is_rejected(self):
        stolen = column(approved_uses=cell("Illustrative condition R", "YY-LABEL-BRANDB-p1-c01",
                                           "BRAND B is indicated for illustrative condition R"))
        self.run_stage({"Brand A": [reply(stolen)], "Brand B": [reply(GOOD_B)]})
        self.assertEqual(self.grid()[0]["Approved uses"], [ND, "Illustrative condition R"])
        [rejected] = self.output("comparison_sources.json")["columns"]["Brand A"]["rejected"]
        self.assertIn("not among the chunks sent", rejected["reason"])

    def test_quote_across_a_line_break_is_kept(self):
        chunk = {"chunk_id": "c1", "text": "WARNING: RISK X\nIllustrative  warning text."}
        for quote in ("RISK X Illustrative warning", "RISK X\nIllustrative  warning"):
            with self.subTest(quote=quote):
                value, source, reason = comparison.check_cell(
                    cell("Boxed warning: risk X", "c1", quote), {"c1": chunk})
                self.assertEqual((value, reason), ("Boxed warning: risk X", None))
                self.assertEqual(source["chunk_id"], "c1")

    def test_model_abstentions_are_not_rejections(self):
        for value in ("not disclosed", "Not disclosed.", "Not disclosed in the chunks", " "):
            with self.subTest(value=value):
                self.assertEqual(comparison.check_cell(cell(value, "x", "y"), {}),
                                 (ND, None, None))

    def test_filing_chunks_capped(self):
        manifest = [{"doc_id": "L", "doc_type": "label", "ticker": "XX"},
                    {"doc_id": "F", "doc_type": "6-K", "ticker": "XX"}]
        label = {"chunk_id": "L-p1-c01", "doc_id": "L", "section": "1 INDICATIONS AND USAGE",
                 "text": "BRAND A is indicated."}
        filings = [{"chunk_id": f"F-p{i}-c01", "doc_id": "F", "section": "S",
                    "text": f"Brand A note {i}."} for i in range(1, 41)]
        ids = [c["chunk_id"] for c in comparison.select_chunks(
            "Brand A", manifest, filings[:20] + [label] + filings[20:])]
        self.assertEqual(ids, ["L-p1-c01"] + [f"F-p{i}-c01" for i in range(1, 31)])
        # A chunk too big for the remaining budget is skipped, not the end of selection.
        big = {"chunk_id": "F-p0-c01", "doc_id": "F", "section": "S",
               "text": "Brand A " + "x" * comparison.MAX_FILING_CHARS}
        ids = [c["chunk_id"] for c in comparison.select_chunks(
            "Brand A", manifest, [label, big] + filings[:3])]
        self.assertEqual(ids, ["L-p1-c01", "F-p1-c01", "F-p2-c01", "F-p3-c01"])

    def test_no_product_writes_nothing(self):
        self.run_json({"rivals": ["Brand B"]})
        requests = self.run_stage({})
        self.assertEqual(requests, [])
        self.assertIn("no product", self.log)
        self.assertFalse((self.run_dir / "comparison.json").exists())
        self.assertFalse((self.run_dir / "comparison_sources.json").exists())
        shutil.copy(FIXTURES / "synthesis_findings.json", self.run_dir / "findings.json")
        model = report.build_model(self.run_dir)
        self.assertEqual(model["comparison"]["note"], report.NO_COMPARISON)

    def test_no_rivals_gives_product_column_only(self):
        for rivals in ([], None, "", ["Brand A", " "]):
            with self.subTest(rivals=rivals):
                self.run_json({"product": "Brand A", "rivals": rivals})
                self.run_stage({"Brand A": [reply(GOOD_A)]})
                self.assertIn("no rivals", self.log)
                _, out = self.grid()
                self.assertEqual(out["columns"], ["Dimension", "Brand A"])
                self.assertTrue(all(len(row) == 2 for row in out["rows"]))

    def test_unsourced_cells_become_not_disclosed(self):
        bad = column(
            approved_uses=cell("Condition Q", "XX-LABEL-BRANDA-p9-c01", "indicated"),
            boxed_warning_safety=cell("Risk X", "XX-LABEL-BRANDA-p1-c01", "RISK Y"),
            dosing_convenience=cell("Once weekly from 0.5 mg", "XX-LABEL-BRANDA-p3-c01",
                                    "Administer once weekly."),
            patent_exclusivity=cell("Expires 2031", "XX-6K-2026Q2-p3-c01", ""),
            pricing_gross_to_net=GOOD_A["pricing_gross_to_net"])
        self.run_stage({"Brand A": [reply(bad)], "Brand B": [reply(GOOD_B)]})
        grid, _ = self.grid()
        self.assertEqual([grid[label][0] for label in LABELS], [ND, ND, ND, ND,
                         "Gross-to-net deductions 61% of gross sales"])
        rejected = self.output("comparison_sources.json")["columns"]["Brand A"]["rejected"]
        reasons = {r["dimension"]: r["reason"] for r in rejected}
        self.assertEqual(len(reasons), 4)
        self.assertIn("not among the chunks sent", reasons["Approved uses"])
        self.assertIn("verbatim", reasons["Boxed warning / safety"])
        self.assertIn("numbers", reasons["Dosing / convenience"])  # 0.5 is not in the quote
        self.assertIn("verbatim", reasons["Patent / exclusivity"])  # empty quote
        self.assertIn("4 unsourced", self.log)

    def test_call_fails_twice_column_not_disclosed(self):
        error = anthropic.APIConnectionError(request=httpx2.Request("POST", "https://x"))
        requests = self.run_stage({"Brand A": [error, error], "Brand B": [reply(GOOD_B)]})
        grid, _ = self.grid()
        self.assertEqual([grid[label][0] for label in LABELS], [ND] * 5)
        self.assertEqual(grid["Approved uses"][1], "Illustrative condition R")
        record = self.output("comparison_sources.json")["columns"]["Brand A"]
        self.assertEqual(record["status"], "failed")
        self.assertIn("APIConnectionError", record["error"])
        self.assertEqual(len(requests), 3)

    def test_client_setup_error_never_raises(self):
        self.run_stage({"Brand A": [TypeError("Could not resolve authentication method")],
                        "Brand B": [reply(GOOD_B)]})
        grid, _ = self.grid()
        self.assertEqual([grid[label][0] for label in LABELS], [ND] * 5)
        record = self.output("comparison_sources.json")["columns"]["Brand A"]
        self.assertEqual(record["status"], "failed")
        self.assertIn("TypeError", record["error"])

    def test_refusal_then_non_json_fails_column(self):
        self.run_stage({"Brand A": [fake_message("refusal", []),
                                    fake_message("end_turn", [NS(type="text", text="no")])],
                        "Brand B": [reply(GOOD_B)]})
        record = self.output("comparison_sources.json")["columns"]["Brand A"]
        self.assertEqual(record["status"], "failed")
        self.assertIn("not valid JSON", record["error"])

    def test_bad_reply_retried_with_problems(self):
        broken = {k: v for k, v in GOOD_A.items() if k != "patent_exclusivity"}
        requests = self.run_stage({"Brand A": [reply(broken), reply(GOOD_A)],
                                   "Brand B": [reply(GOOD_B)]})
        retry = [r for r in requests if "Problems with your previous attempt"
                 in r["messages"][0]["content"]]
        self.assertEqual(len(retry), 1)
        self.assertIn("patent_exclusivity", retry[0]["messages"][0]["content"])
        self.assertEqual(retry[0]["system"], comparison.SYSTEM)
        self.assertEqual(self.grid()[0]["Approved uses"][0], "Illustrative condition Q")

    def test_product_without_any_chunk_makes_no_call(self):
        self.run_json({"product": "Brand A", "rivals": ["Brand Z"]})
        requests = self.run_stage({"Brand A": [reply(GOOD_A)]})
        self.assertEqual(len(requests), 1)
        self.assertEqual(self.grid()[1]["columns"], ["Dimension", "Brand A", "Brand Z"])
        record = self.output("comparison_sources.json")["columns"]["Brand Z"]
        self.assertEqual(record["status"], "no_sources")

    def test_rivals_deduplicated_and_product_dropped(self):
        self.assertEqual(comparison.clean_rivals(["Brand B", "brand b", "Brand A", "", 3],
                                                 "Brand A"), ["Brand B"])
        self.assertEqual(comparison.clean_rivals("Brand B, Brand C", "Brand A"),
                         ["Brand B", "Brand C"])

    def test_prompt_has_no_run_values(self):
        for word in ("Brand", "Ozempic", "Mounjaro", "Novo", "Lilly", "2026"):
            self.assertNotIn(word, comparison.SYSTEM)


class SampleCorpusAcceptanceTest(StageTestCase):
    """`finish --product Ozempic --rivals Mounjaro` on data/sample/NVO, with replies that quote
    real label chunks: 5-row grid, filled cells traceable to label chunks, report renders it."""

    def test_sample_corpus_grid_and_report(self):
        for name in ("manifest.json", "chunks.jsonl"):
            shutil.copy(SAMPLE / name, self.run_dir / name)
        shutil.copy(FIXTURES / "synthesis_findings.json", self.run_dir / "findings.json")
        self.run_json({"product": "Ozempic", "rivals": ["Mounjaro"]})
        chunks = {c["chunk_id"]: c for c in comparison._read_jsonl(SAMPLE / "chunks.jsonl")}
        oz_ind, mj_ind = "NVO-LABEL-OZEMPIC-p2-c01", "LLY-LABEL-MOUNJARO-p2-c01"
        oz_box = "NVO-LABEL-OZEMPIC-p1-c01"
        oz = column(
            approved_uses=cell("Glycemic control in adults with type 2 diabetes mellitus",
                               oz_ind, "to improve glycemic control in adults with type 2 "
                                       "diabetes mellitus"),
            boxed_warning_safety=cell("Boxed warning: risk of thyroid C-cell tumors", oz_box,
                                      "WARNING: RISK OF THYROID C-CELL TUMORS"))
        mj = column(approved_uses=cell("Glycemic control, adults and pediatric patients 10+",
                                       mj_ind, "adults and pediatric patients 10 years of age"))
        for chunk_id, c in ((oz_ind, oz["approved_uses"]), (oz_box, oz["boxed_warning_safety"]),
                            (mj_ind, mj["approved_uses"])):
            self.assertIn(c["quote"], chunks[chunk_id]["text"])
        requests = self.run_stage({"Ozempic": [reply(oz)], "Mounjaro": [reply(mj)]})
        by = {r["messages"][0]["content"].split("\n")[2]: r["messages"][0]["content"]
              for r in requests}
        self.assertIn(oz_ind, by["Ozempic"])
        self.assertNotIn("LLY-LABEL-MOUNJARO", by["Ozempic"])
        self.assertIn(mj_ind, by["Mounjaro"])
        self.assertNotIn("NVO-LABEL-OZEMPIC", by["Mounjaro"])

        grid, out = self.grid()
        self.assertEqual(out["columns"], ["Dimension", "Ozempic", "Mounjaro"])
        self.assertEqual(len(out["rows"]), 5)
        sources = self.output("comparison_sources.json")["columns"]
        for name, col in (("Ozempic", 0), ("Mounjaro", 1)):
            for label in LABELS:
                src = sources[name]["cells"][label]
                if grid[label][col] == ND:
                    self.assertIsNone(src)
                else:
                    self.assertEqual(src["doc_type"], "label")
                    self.assertIn(src["quote"], chunks[src["chunk_id"]]["text"])
        model = report.build_model(self.run_dir)
        self.assertEqual(model["comparison"]["status"], "ok")
        self.assertEqual(model["comparison"]["columns"], ["Dimension", "Ozempic", "Mounjaro"])
        self.assertEqual(len(model["comparison"]["rows"]), 5)


if __name__ == "__main__":
    unittest.main()
