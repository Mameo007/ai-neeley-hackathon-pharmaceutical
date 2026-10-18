---
title: 'Librarian: EDGAR filings corpus (manifest + chunks)'
type: 'feature'
created: '2026-10-17'
status: 'in-review'
route: 'dispatch'
review_loop_iteration: 0
baseline_commit: '51dd569755253e034306743ad797f5bc6c85777d'
context:
  - '{project-root}/docs/contracts.md'
  - '{project-root}/docs/conventions.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Nothing in the pipeline exists yet downstream of `pharmer/llm.py`.
Partner B's 8 hunters are closed-book — they may only cite `chunk_id`s from the
Librarian corpus — so until `chunks.jsonl` exists, no hunter can run and the
whole evidence pipeline is blocked.

**Approach:** Build `pharmer/librarian.py` as the stage-0 entry point that, given
a ticker, fetches the company's own SEC periodic filings, page-indexes them, and
writes Contract 1's `manifest.json` and `chunks.jsonl` into the run folder. The
subject company's filings only — structured facts, labels, rivals, and
transcripts are separately specced (see `deferred-work.md`).

## Boundaries & Constraints

**Always:**
- Write exactly the Contract 1 shapes in `docs/contracts.md` §1a/§1b; every
  field in the examples is emitted (`application_no`/`labeler_code` `null`,
  `source: "live"`, `collected_date: null`).
- `chunk_id` = `<doc_id>-p<page>-c<n>`, **byte-identical across re-runs of the
  same document** — a hunter's citation is worthless if a re-run renumbers it.
- `page` is the document's own **printed** page number (it lands in the report
  footnote: "Novo Q2 2026 6-K, p.14").
- Match quarters on fiscal period, never calendar date (Pfizer/J&J run 52/53-week
  quarters: 2026-06-28 vs 2025-06-29).
- Detect the financial 6-K by document **content**, never filename or accession
  prefix. Identify companies by CIK, never name substring.
- SEC fair access: declared `User-Agent` with a real contact, ≤10 req/s.
- Standard library only (`urllib`, `re`, `json`, `pathlib`).
- `facts.jsonl` is created **empty** so the Contract 1 file set is complete and
  Partner B's readers don't crash on a missing path. Log explicitly, at WARNING,
  that structured financial facts are deferred — never emit a placeholder fact.
- Every selection decision is **auditable**. Write a non-contract sidecar
  `librarian_report.json` into the run folder holding: each 6-K candidate with
  its marker score and the reason it was kept or rejected, per-document page
  attribution counts (verified printed vs inferred ordinal), and all coverage
  warnings. It is additive and outside Contract 1, so Partner B may ignore it.
- A page number is **verified** only when the page-break section carries a
  printed number. Otherwise `page` holds the ordinal and that document's
  inferred pages are listed in the sidecar — an inferred position is never
  presented as a verified printed-page citation.
- Table chunks preserve structure: cells delimited, the table's header row(s)
  repeated on every chunk a table is split across, and reporting-period/unit
  header text kept with the cells. Cell text stays **verbatim** — parenthesised
  negatives stay `(1,234)`, never normalised to `-1234`, because the Fact
  Checker string-matches hunter quotes against `text`.

**Never:**
- No `facts.jsonl` rows, XBRL, IR workbooks, DailyMed/Orange/Purple Book, rival
  filings, or transcripts — all deferred.
- Never write a Contract 2 (`findings.json`) field. No web/news search here.
- No per-company hardcoding beyond the CIK table — a judge may name any of the
  10 shortlist companies live.
- Do not change `docs/contracts.md` (requires both partners).

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| Domestic filer | `LLY` | 2 × 10-K, latest 10-Q, prior-year same-quarter 10-Q in manifest; chunks for each | N/A |
| Foreign filer | `NVO` | 2 × 20-F (with the incorporated-by-reference annual-report exhibit), latest financial 6-K, prior-year same-quarter 6-K | N/A |
| 6-K noise | Novo's 895 recent 6-Ks incl. a 4.4MB remuneration report | Only content-scored financial reports selected; remuneration report excluded | N/A |
| 52/53-week quarter | `PFE`, latest Q ends 2026-06-28 | Prior-year quarter 2025-06-29 found via fiscal-period match | N/A |
| Printed page absent | Cover page carries no page number | `page` falls back to the page-break ordinal; sidecar records it as inferred; `chunk_id` still stable | N/A |
| Ambiguous 6-K | Two candidates score within one marker of each other | Highest kept, both recorded in the sidecar with scores, ambiguity warning raised | Flag, never silently pick |
| No financial 6-K | All candidates below the marker threshold | No quarterly entry; coverage warning; run continues | Warn, never fabricate |
| Split table | Table longer than the chunk budget | Header row(s) repeated on each chunk; `is_table: true` on all | N/A |
| Negative cell | Filing shows `(1,234)` | `text` keeps `(1,234)` verbatim | N/A |
| Prior-year quarter missing | No comparable filing on EDGAR | Manifest omits it; run continues and logs a coverage warning | Warn, never fabricate |
| Unknown ticker | `ZZZZ` | Fail fast before any network write | `ValueError` naming the supported tickers |
| SEC UA unset | `PHARMER_SEC_UA` missing | Fail fast before the first request | `RuntimeError` pointing at `.env.example` |
| SEC 429/503 | Rate limited mid-run | Retry with backoff, then propagate | 3 tries, then raise |

</frozen-after-approval>

## Code Map

- `pharmer/llm.py` — **reuse** `REPO_ROOT` and the `_load_dotenv()` pattern. No
  Claude call in this stage; do not import `call()`/`run_agent()`, do not modify.
- `_bmad-output/brainstorming/brainstorm-architecture-open-questions-2026-10-17/source-probe/probe.py`
  — **proven working** fetch code to adapt: `all_filings()` flattens
  `data.sec.gov/submissions/CIK{cik:010d}.json`'s parallel-array
  `filings.recent` into row dicts; foreign-filer test is `"20-F" in forms and
  "10-K" not in forms`; `SHORTLIST` has all 10 verified CIKs. Its `prior_yr_q`
  calendar-date logic is the **known bug** in build-log — do not copy it.
- `docs/contracts.md` §1a/§1b — binding field shapes; example values are
  illustrative, never copy a number out.
- `docs/conventions.md` — `async def run(run_dir: Path) -> None` entry point, ID
  formats, `logging.getLogger(__name__)` not `print`, `indent=2,
  ensure_ascii=False`, JSONL one compact object per line.
- `tests/test_llm.py` — offline-test pattern to copy: fakes injected over module
  globals in `setUp`, `tempfile.TemporaryDirectory`, no network.
- To create: `pharmer/librarian.py`, `tests/test_librarian.py`,
  `tests/fixtures/` filing HTML; edit `.env.example`.

## Tasks & Acceptance

**Execution:**
- [x] `.env.example` — add `PHARMER_SEC_UA="Name email@example.com"` with a
  comment that SEC requires a real contact — fair-access compliance, and the
  demo must not get the IP blocked.
- [x] `pharmer/librarian.py` — module docstring naming the contract files it
  writes; `CIKS` table of the 10 verified tickers→CIK; `_sec_get()` with the UA
  header, a ≥0.11s inter-request sleep, and 3-try backoff on 429/503.
- [x] `pharmer/librarian.py` — `_select_documents(cik)`: flatten
  `filings.recent`; branch domestic vs foreign; pick 2 annuals, the latest
  quarterly, and the prior-year same-quarter by **fiscal period**; for foreign
  filers content-score 6-K candidates and resolve the 20-F's
  incorporation-by-reference exhibit from the accession `index.json`.
- [x] `pharmer/librarian.py` — `_split_pages(html)` and `_chunk_page(...)`:
  split on the page-break `<hr>`, read the trailing printed page number (and
  mark it verified vs inferred), emit chunks with `section` from the nearest
  preceding heading and `is_table` for table-derived chunks; tables keep cell
  delimiters, repeat header rows across splits, and preserve cell text verbatim.
- [x] `pharmer/librarian.py` — `async def run(run_dir: Path) -> None` plus a
  `__main__` block so one ticker can be fetched from the CLI; writes
  `manifest.json`, `chunks.jsonl`, an empty `facts.jsonl`, and the
  `librarian_report.json` audit sidecar.
- [x] `tests/fixtures/` — trimmed real filing HTML (a few pages of a 10-Q and of
  a financial 6-K, plus a noise 6-K) so tests stay offline.
- [x] `tests/test_librarian.py` — cover every I/O Matrix row with a fake fetcher
  injected over the module's HTTP helper; assert `chunk_id` stability by
  chunking the same fixture twice; assert table row/column association, repeated
  headers on split tables, retained period/unit headers, and verbatim
  parenthesised negatives.
- [x] `tests/test_librarian.py` — a **Partner B compatibility test**: a minimal
  Contract 1 reader that loads `manifest.json` + `chunks.jsonl`, resolves a
  `chunk_id` to its document and page, and confirms a quote taken from a chunk
  is a verbatim substring of that chunk's `text` — the exact operation the
  hunters and Fact Checker perform.
- [x] `docs/build-log.md` — append entries for the decisions and the
  confidently-wrong case recorded in Design Notes.

**Acceptance Criteria:**
- Given a clean checkout, when `python3 -m unittest discover tests` runs, then
  all tests pass without touching the network.
- Given `PHARMER_SEC_UA` is set, when the stage runs for `LLY` and for `NVO`,
  then `runs/<run_id>/` holds a `manifest.json` whose every entry has a
  reachable `url` and an `accession`, and a `chunks.jsonl` whose every `doc_id`
  appears in the manifest.
- Given a generated `chunks.jsonl`, when any `chunk_id` is parsed, then its
  `<doc_id>` and `p<page>` segments match that record's `doc_id` and `page`.
- Given two runs of the same ticker on the same filings, when the two
  `chunks.jsonl` files are compared, then the `chunk_id`→`text` mapping is
  identical.
- Given a completed run, when `librarian_report.json` is read, then every
  selected and rejected 6-K candidate appears with its score and reason, every
  document reports its verified/inferred page counts, and the deferred
  `facts.jsonl` coverage gap is stated.

## Implementation Notes

**Files:** `pharmer/librarian.py` (new, 747 lines), `tests/test_librarian.py`
(new, 511 lines / 42 tests), `tests/fixtures/{lly_10q_pages,nvo_6k_financial,
nvo_6k_noise}.htm` (real unedited filing page sections, 472KB — left unedited
because their fidelity is what exposed defect 1), `.env.example`
(+`PHARMER_SEC_UA`), `docs/build-log.md`.

**Two defects the live runs exposed, both in page attribution:**

1. *Printed-page position is not a property of the form.* The footer rule
   derived from Lilly's 10-Q found **0** verified pages in Novo's 6-K, which
   prints `Page 3 of 37` in a running header instead. `_split_pages` now checks
   the last line **and** the first two lines of every page section.
2. *Novo's FY2025 annual-report exhibit has no page-break markup at all* — no
   `<hr>`, no `page-break-*` CSS, one continuous 10.6MB document. All 659 of its
   chunks were landing on `p.1`. Added `_subdivide`: a section longer than a real
   printed page (`UNPAGINATED_MIN_CHARS`) is cut into `SYNTHETIC_PAGE_CHARS`
   fragments at table and paragraph boundaries, all recorded as inferred. The
   first version only split on table boundaries, so an all-prose document stayed
   one block — caught by `test_unpaginated_document_is_divided_into_synthetic_pages`.
   That document now reports `attribution: "inferred-only"` and raises a warning
   that its footnotes must cite the section, not a printed page. Its printed page
   numbers are genuinely unrecoverable: the standalone integers in its text are
   table-of-contents references, only 104 of 1,426 consecutive.

**Decisions taken during implementation:**
- `pharmer.llm` is imported lazily inside `_sec_user_agent()` and `REPO_ROOT` is
  computed locally, so fetching filings does not make `anthropic` a hard
  import-time dependency of a stage that makes no Claude call.
- Table cells are joined with `\t` and rows with `\n` — whitespace-only, so
  row/column structure survives without making hunter quotes read oddly.
- `Accept-Encoding: gzip` was removed after being added: `urllib` does not
  transparently decompress, so it would have returned compressed bytes.
- 6-K ambiguity is scoped **per fiscal period**. Comparing the top two scores
  outright fired on every foreign run, because the prior-year report scores just
  as highly as the current one.
- `_select_financial_6ks` stops fetching once the latest report and its
  prior-year counterpart are both in hand (Novo has 73 oversized 6-Ks).

**Verified against live EDGAR:** `LLY` 4 docs / 1,168 chunks / 2.5s, 299 verified
printed pages. `NVO` 4 docs / 1,895 chunks / 4.5s, including the FY2025 exhibit
via incorporation-by-reference and the correct Q2 6-Ks. Novo's prior-year 6-K
came back under accession prefix `0001628280-25-` while the current one is
`0000353278-26-` — live confirmation that keying on accession prefix would fail.
Two separate LLY runs produced an identical `chunk_id`→`text` mapping.

Hand-sampled 14 prose citations across both companies and re-derived each page
independently: all 14 correct. Three apparent mismatches were flaws in the
checking script, not the corpus — `str.find` returning an earlier duplicate of
repeated boilerplate, and a loose `page \d+` regex matching cross-references
("See Annual review, page 15").

## Design Notes

**Page numbers are real, not synthetic** — verified against Lilly's Q2 2026 10-Q
(`lly-20260630.htm`): it contains 33 `page-break-after` markers / 34 `<hr>`
sections, and the last text line of each section is the document's own printed
page number (`2`, `3`, `4`, … `9`, `10`). So footnote-grade page attribution
comes straight out of the HTML; the cover page is simply unnumbered and falls
back to its ordinal.

**Content-scoring separates the financial 6-K cleanly** — verified on Novo:

```
caq22026.htm  (Q2 2026 financial)  9/9 markers: financial report×50,
              income statement×5, balance sheet×2, trade receivables×1
novonordisk_remuneration.htm (noise) 2/9 markers, and zero of
              {income statement, balance sheet, net sales, cash flow}
```

Size alone would have picked the wrong one — the 4,389KB remuneration report is
*larger* than the 2,784KB financial report. This is the live confirmation of the
architecture's "never key on filename or accession prefix" rule.

**The 20-F exhibit is discoverable** — the accession's `index.json` lists
`nvo-20251231_d2.htm` (10.6MB), the largest non-primary `.htm`, which is the
incorporated-by-reference Annual Report. Select it by size+content rather than
the `_d2` suffix, since §8.2 established that filing-agent naming is not a rule.

**Fetch is keyed on CIK, not ticker,** so the deferred rival-filings follow-up is
a config list rather than new fetch logic.

## Verification

**Commands:**
- `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt` --
  expected: clean install (`uv` is not on this machine; `python3` is 3.14.3)
- `.venv/bin/python -m unittest discover tests` -- expected: all tests pass,
  no network access
- `.venv/bin/python -m pharmer.librarian LLY` -- expected: a `runs/LLY-*/`
  folder with a 4-entry manifest and a non-trivial `chunks.jsonl`
- `.venv/bin/python -m pharmer.librarian NVO` -- expected: manifest includes the
  20-F annual-report exhibit and the Q2 2026 6-K, not the remuneration 6-K

**Manual checks:**
- Open 3 random chunks' `url` at the stated `page` and confirm the chunk `text`
  actually appears on that printed page — this is the check the Fact Checker
  will later automate, and the whole closed-book guarantee rests on it.
