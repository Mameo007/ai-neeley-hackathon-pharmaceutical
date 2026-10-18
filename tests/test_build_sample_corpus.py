"""Offline tests for scripts/build_sample_corpus.py, one per row of the spec's I/O matrix.

`_sec_get` is faked, and OUT_DIR / RAW_DIR point at a temp dir. Tests that replay real SEC
documents use the git-ignored data/sample/NVO/_raw/ cache and are skipped when it is absent.

Run: .venv/bin/python -m unittest tests.test_build_sample_corpus
"""
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
HAVE_RAW = all((REAL_RAW / name).is_file() for name in bsc.CACHE_FILES)
needs_raw = unittest.skipUnless(HAVE_RAW, "no data/sample/NVO/_raw cache on this machine")


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
    def test_fetch_caches_raw_and_writes_outputs(self):
        with mock.patch.object(bsc, "_sec_get", side_effect=_replay_sec):
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
    def test_rebuild_is_byte_identical_and_offline(self):
        shutil.copytree(REAL_RAW, self.raw)
        with mock.patch.object(bsc, "_sec_get", side_effect=_no_network):
            bsc.main(["--from-cache"])
            first = self._snapshot()
            bsc.main(["--from-cache"])
            self.assertEqual(self._snapshot(), first)
        for name in bsc.OUTPUT_FILES:
            self.assertEqual((self.out / name).read_bytes(), (COMMITTED / name).read_bytes())

    def test_missing_cache_file_raises_and_leaves_outputs(self):
        self.raw.mkdir(parents=True)
        (self.out / "manifest.json").write_text("old", encoding="utf-8")
        with mock.patch.object(bsc, "_sec_get", side_effect=_no_network):
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

        with mock.patch.object(bsc, "_sec_get", side_effect=flaky):
            with self.assertRaises(RuntimeError):
                bsc.main([])
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(sorted(p.name for p in self.out.iterdir()),
                         sorted(["_raw", *bsc.OUTPUT_FILES]))


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
