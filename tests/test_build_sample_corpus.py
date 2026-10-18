"""Offline tests for scripts/build_sample_corpus.py, one per row of the spec's I/O matrix.

`_sec_get` is faked, and OUT_DIR / RAW_DIR point at a temp dir. Tests that replay real SEC
documents use the git-ignored data/sample/NVO/_raw/ cache and are skipped when it is absent.

Run: .venv/bin/python -m unittest tests.test_build_sample_corpus
"""
import contextlib
import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "build_sample_corpus", REPO_ROOT / "scripts" / "build_sample_corpus.py")
bsc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bsc)

COMMITTED = REPO_ROOT / "data" / "sample" / "NVO"
REAL_RAW = COMMITTED / "_raw"
LABEL_FILES = [name for label in bsc.LABELS for name in bsc._label_files(label)]
SEC_FILES = [name for name in bsc.CACHE_FILES if name not in LABEL_FILES]
# Two gates, so a machine holding only the SEC cache still runs the 6-K replay tests.
needs_raw = unittest.skipUnless(all((REAL_RAW / n).is_file() for n in SEC_FILES),
                                "no SEC files in data/sample/NVO/_raw on this machine")
needs_label_raw = unittest.skipUnless(all((REAL_RAW / n).is_file() for n in LABEL_FILES),
                                      "no DailyMed label files in data/sample/NVO/_raw")


def _no_network(url):
    raise AssertionError(f"unexpected network call: {url}")


def _replay_sec(url):
    """Serve the cached raw file that a real fetch of `url` produced."""
    if url == bsc.SUBMISSIONS_URL:
        return (REAL_RAW / "submissions.json").read_bytes()
    if url == bsc.COMPANYFACTS_URL:
        return (REAL_RAW / "companyfacts.json").read_bytes()
    for k in bsc.SIX_KS:
        if url == bsc._doc_url(k["accession"], k["document"]):
            return (REAL_RAW / k["document"]).read_bytes()
    raise AssertionError(f"unknown url {url}")


def _replay_dailymed(url):
    """Serve the cached DailyMed search, NDC list or SPL XML that a real fetch produced."""
    for label in bsc.LABELS:
        spls_name, ndcs_name, xml_name = bsc._label_files(label)
        if url == bsc._spls_url(label["application_no"]):
            return (REAL_RAW / spls_name).read_bytes()
        ndcs = json.loads((REAL_RAW / ndcs_name).read_text(encoding="utf-8"))
        for setid, codes in ndcs.items():
            if url == bsc._ndcs_url(setid):
                return json.dumps({"data": {"ndcs": [{"ndc": c} for c in codes]}}).encode()
            if url == bsc._spl_url(setid):
                return (REAL_RAW / xml_name).read_bytes()
    raise AssertionError(f"unknown url {url}")


def _offline():
    """No SEC and no DailyMed request may leave the process."""
    stack = contextlib.ExitStack()
    for name in ("_sec_get", "_dailymed_get"):
        stack.enter_context(mock.patch.object(bsc, name, side_effect=_no_network))
    return stack


class _TempDirs(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.out = self.tmp / "NVO"
        self.raw = self.out / "_raw"
        for name, value in (("OUT_DIR", self.out), ("RAW_DIR", self.raw)):
            patcher = mock.patch.object(bsc, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _snapshot(self):
        return {p.relative_to(self.out): p.read_bytes()
                for p in sorted(self.out.rglob("*")) if p.is_file()}


class HappyPathTest(_TempDirs):
    @needs_raw
    @needs_label_raw
    def test_fetch_caches_raw_and_writes_outputs(self):
        with mock.patch.object(bsc, "_sec_get", side_effect=_replay_sec), \
                mock.patch.object(bsc, "_dailymed_get", side_effect=_replay_dailymed):
            self.assertEqual(bsc.main([]), 0)
        for name in bsc.CACHE_FILES:
            self.assertTrue((self.raw / name).is_file(), name)
        meta = json.loads((self.raw / "cache_meta.json").read_text(encoding="utf-8"))
        self.assertRegex(meta["collected_date"], r"^\d{4}-\d{2}-\d{2}$")
        for name in ("chunks.jsonl", "facts.jsonl"):
            self.assertEqual((self.out / name).read_bytes(), (COMMITTED / name).read_bytes())
        manifest = json.loads((self.out / "manifest.json").read_text(encoding="utf-8"))
        committed = json.loads((COMMITTED / "manifest.json").read_text(encoding="utf-8"))
        for row, ref in zip(manifest, committed, strict=True):
            self.assertEqual(row["collected_date"], meta["collected_date"])
            self.assertEqual({**row, "collected_date": None}, {**ref, "collected_date": None})
        self.assertEqual(sorted(p.name for p in self.out.iterdir()),
                         ["_raw", "chunks.jsonl", "facts.jsonl", "manifest.json"])


class FromCacheTest(_TempDirs):
    @needs_raw
    @needs_label_raw
    def test_rebuild_is_byte_identical_and_offline(self):
        shutil.copytree(REAL_RAW, self.raw)
        with _offline():
            bsc.main(["--from-cache"])
            first = self._snapshot()
            bsc.main(["--from-cache"])
            self.assertEqual(self._snapshot(), first)
        for name in bsc.OUTPUT_FILES:
            self.assertEqual((self.out / name).read_bytes(), (COMMITTED / name).read_bytes())

    def test_missing_cache_file_raises_and_leaves_outputs(self):
        self.raw.mkdir(parents=True)
        (self.out / "manifest.json").write_text("old", encoding="utf-8")
        with _offline():
            with self.assertRaises(FileNotFoundError):
                bsc.main(["--from-cache"])
        self.assertEqual((self.out / "manifest.json").read_text(encoding="utf-8"), "old")
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["_raw", "manifest.json"])


class XbrlDuplicatesTest(unittest.TestCase):
    def test_one_fact_per_period_latest_filing_wins(self):
        def entry(end, val, filed, accn, start=None):
            x = {"end": end, "val": val, "fp": "FY", "form": "20-F", "filed": filed,
                 "accn": accn}
            if start:
                x["start"] = start
            return x

        concepts = {c: {"units": {"DKK": []}} for c in bsc.CONCEPTS.values()}
        concepts["CurrentTradeReceivables"]["units"]["DKK"] = [
            entry("2024-12-31", 71_000_000_000, "2025-02-05", "0001-25-1"),
            entry("2024-12-31", 71_949_000_000, "2026-02-04", "0001-26-1"),
            entry("2025-12-31", 70_856_000_000, "2026-02-04", "0001-26-1"),
            entry("2022-12-31", 1_000_000_000, "2023-02-01", "0001-23-1"),  # out of range
        ]
        concepts["Revenue"]["units"]["DKK"] = [
            entry("2025-12-31", 309_064_000_000, "2026-02-04", "0001-26-1", "2025-01-01"),
            # A half-year duration tagged fp FY must not count as the annual figure.
            entry("2025-12-31", 1_000_000_000, "2026-03-01", "0001-26-2", "2025-07-01"),
        ]
        facts, period_end, filed = bsc.build_facts({"facts": {"ifrs-full": concepts}})
        values = {f["fact_id"]: f["value"] for f in facts}
        self.assertEqual(values, {
            "NVO-xbrl-revenue-FY2025": 309064,
            "NVO-xbrl-trade_receivables-FY2024": 71949,
            "NVO-xbrl-trade_receivables-FY2025": 70856,
        })
        self.assertEqual((period_end, filed), ("2025-12-31", "2026-02-04"))


class FetchFailureTest(_TempDirs):
    def test_failed_fetch_leaves_outputs_and_cache_untouched(self):
        self.raw.mkdir(parents=True)
        (self.raw / "submissions.json").write_text("cached", encoding="utf-8")
        for name in bsc.OUTPUT_FILES:
            (self.out / name).write_text(f"old {name}", encoding="utf-8")
        before = self._snapshot()
        calls = []

        def flaky(url):
            calls.append(url)
            if len(calls) == 3:
                raise RuntimeError("curl exit 22")
            return b"{}"

        with mock.patch.object(bsc, "_sec_get", side_effect=flaky), \
                mock.patch.object(bsc, "_dailymed_get", side_effect=_no_network):
            with self.assertRaises(RuntimeError):
                bsc.main([])
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(sorted(p.name for p in self.out.iterdir()),
                         sorted(["_raw", *bsc.OUTPUT_FILES]))


def _search(*rows):
    return {"data": [{"setid": s, "title": f"T {s}", "published_date": "Jun 10, 2026",
                      "spl_version": 1} for s in rows], "metadata": {"next_page_url": "null"}}


class LabelRuleTest(unittest.TestCase):
    """The §4.0 label rule: keep SPLs whose NDCs all carry the allowlisted labeler code."""

    def test_repackager_with_same_application_is_dropped(self):
        ndcs = {"own": ["0169-4130-01", "0169-4132-11"], "repack": ["50090-1234-0"]}
        chosen = bsc.select_spl(_search("repack", "own"), ndcs, "0169")
        self.assertEqual(chosen["setid"], "own")

    def test_zero_or_several_survivors_stop_the_build(self):
        with self.assertRaises(ValueError):
            bsc.select_spl(_search("repack"), {"repack": ["50090-1234-0"]}, "0169")
        with self.assertRaises(ValueError) as cm:
            bsc.select_spl(_search("a", "b"), {"a": ["0169-1-1"], "b": ["0169-2-2"]}, "0169")
        self.assertIn("T a", str(cm.exception))

    def test_mixed_labelers_and_no_ndcs_are_dropped(self):
        ndcs = {"mixed": ["0169-1-1", "50090-1-1"], "empty": [], "own": ["0169-2-2"]}
        chosen = bsc.select_spl(_search("mixed", "empty", "own"), ndcs, "0169")
        self.assertEqual(chosen["setid"], "own")

    def test_spl_sections_become_pages(self):
        ns = 'xmlns="urn:hl7-org:v3"'
        xml = f"""<document {ns}><effectiveTime value="20260601"/>
          <setId root="own"/><versionNumber value="1"/>
          <component><structuredBody>
            <component><section><code code="48780-1"/><subject><manufacturedProduct>
              <manufacturedProduct><code code="0169-4130" codeSystem="2.16.840.1.113883.6.69"/>
              <approval><id extension="NDA209637"/></approval>
              </manufacturedProduct></manufacturedProduct></subject></section></component>
            <component><section><code code="34066-1"/><title>WARNING: X</title>
              <text><paragraph>Risk <content>of</content> tumors.</paragraph></text>
            </section></component>
            <component><section><code code="34068-7"/><title>2 DOSAGE</title>
              <component><section><title>2.1 Start</title><text>
                <list><item>Once weekly.</item><item>Any time.</item></list>
                <table><tbody><tr><td>Dose</td><td>Volume</td></tr>
                <tr><td>0.25 mg</td><td>0.5 mL</td></tr></tbody></table>
              </text></section></component>
            </section></component>
            <component><section><code code="42231-1"/><title>Medication Guide</title>
              <text><paragraph>Lay text.</paragraph></text></section></component>
          </structuredBody></component></document>""".encode()
        label = {"product": "ozempic", "application_no": "NDA209637", "labeler_code": "0169"}
        pages, effective = bsc.parse_label(xml, label, {"setid": "own", "spl_version": 1})
        self.assertEqual(effective, "2026-06-01")
        self.assertEqual([(p["page"], p["section"]) for p in pages],
                         [(1, "WARNING: X"), (2, "2 DOSAGE")])
        chunks = bsc.build_chunks("NVO-LABEL-OZEMPIC", pages)
        self.assertEqual([c["chunk_id"] for c in chunks],
                         ["NVO-LABEL-OZEMPIC-p1-c01", "NVO-LABEL-OZEMPIC-p2-c01",
                          "NVO-LABEL-OZEMPIC-p2-c02"])
        self.assertEqual(chunks[0]["text"], "WARNING: X\nRisk of tumors.")
        self.assertEqual(chunks[1]["text"], "2 DOSAGE\n2.1 Start\nOnce weekly.\nAny time.")
        self.assertTrue(chunks[2]["is_table"])
        self.assertEqual(chunks[2]["section"], "2 DOSAGE")
        with self.assertRaises(ValueError):  # another labeler's NDC in the XML
            bsc.parse_label(xml, {**label, "labeler_code": "0002"},
                            {"setid": "own", "spl_version": 1})
        with self.assertRaises(ValueError):  # not the version the search chose
            bsc.parse_label(xml, label, {"setid": "own", "spl_version": 2})
        with self.assertRaises(ValueError):  # filed under another application
            bsc.parse_label(xml, {**label, "application_no": "NDA000000"},
                            {"setid": "own", "spl_version": 1})

    def test_paged_search_stops_before_any_ndc_fetch(self):
        search = _search("own")
        search["metadata"]["next_page_url"] = (
            "https://dailymed.nlm.nih.gov/dailymed/services/v2/spls.json?page=2")
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(bsc, "_dailymed_get",
                                  return_value=json.dumps(search).encode()) as get:
            with self.assertRaises(ValueError):
                bsc.fetch_label(Path(tmp), bsc.LABELS[0])
            self.assertEqual(get.call_count, 1)
            self.assertEqual(list(Path(tmp).iterdir()), [])


@needs_raw
class RealDocumentTest(unittest.TestCase):
    """Replays the real 6-K HTML through the parser."""

    def test_page_14_comes_from_printed_page_14(self):
        raw_html = (REAL_RAW / "caq22026.htm").read_text(encoding="utf-8")
        parser = bsc._BlockParser()
        parser.feed(raw_html)
        parser.close()
        [segment] = [s for s in parser.segments
                     if any("Page 14 of 37" in bsc._block_text(b) for b in s)]
        body = [bsc._block_text(b) for b in segment
                if bsc.BANNER_MARKER not in bsc._block_text(b)
                and bsc.NAV_MARKER not in bsc._block_text(b)]
        page14 = [c for c in bsc.build_chunks("NVO-6K-2026Q2", bsc.parse_pages(raw_html))
                  if c["page"] == 14]
        text = "\n".join(c["text"] for c in page14)
        self.assertTrue(body)
        for block in body:
            self.assertIn(block, text)

    def test_every_split_table_repeats_its_header(self):
        for k in bsc.SIX_KS:
            raw_html = (REAL_RAW / k["document"]).read_text(encoding="utf-8")
            for page in bsc.parse_pages(raw_html):
                for block in page["blocks"]:
                    if block[0] != "table":
                        continue
                    rows = block[1]
                    texts = bsc._table_chunks(rows)
                    first_amount = next((i for i, r in enumerate(rows)
                                         if any(bsc._is_amount(c) for c in r)), 0)
                    header = [" ".join(r) for r in rows[:min(first_amount, bsc.HEADER_MAX_ROWS)]]
                    for text in texts:
                        self.assertEqual(text.split("\n")[:len(header)], header)
                    body = [line for t in texts for line in t.split("\n")[len(header):]]
                    self.assertEqual(body, [" ".join(r) for r in rows[len(header):]])


if __name__ == "__main__":
    unittest.main()
