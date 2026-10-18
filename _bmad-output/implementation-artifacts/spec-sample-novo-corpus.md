---
title: 'Sample Novo Nordisk corpus for Partner B development'
type: 'feature'
created: '2026-10-17'
status: 'done'
baseline_commit: '51dd569755253e034306743ad797f5bc6c85777d'
route: 'dispatch'
review_loop_iteration: 1
context:
  - '{project-root}/docs/contracts.md'
  - '{project-root}/docs/conventions.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Partner B's stages (hunters, Dedup, scoring, report) read the Librarian's Contract 1
files, but the Librarian (Partner A) does not exist yet, so B has nothing to build or test against.

**Approach:** A one-off script fetches a small set of **real** Novo Nordisk (NVO) documents from
SEC EDGAR, splits them into the filing's own pages and into chunks, and writes a frozen sample
corpus (`manifest.json`, `chunks.jsonl`, `facts.jsonl`) in exactly the Contract 1 shape. The
output is committed so both partners use the same bytes. It stands in for the Librarian; it is
not the Librarian.

**Decisions (human, 2026-10-17):**
- Scope = lean: 6-K Q2 2026, 6-K Q2 2025, XBRL companyfacts. The 20-F annual-report exhibits
  are deferred (they are PDF-to-HTML with no page breaks; real page numbers are costly).
- Location = `data/sample/NVO/` (committed), built by `scripts/build_sample_corpus.py`; owned by
  Partner B as a dev fixture until Partner A's Librarian replaces it. The PR asks A to confirm
  or veto the metric keys and the page rule.

## Boundaries & Constraints

**Always:** Every chunk text and fact value comes from a fetched primary document (no model
memory, no hand-typed numbers). Files match `docs/contracts.md` Contract 1 field-for-field; IDs
follow `docs/conventions.md`. `page` is the filing's printed page number. SEC requests send a
descriptive User-Agent and stay under 10 req/s. The same cached inputs always give
byte-identical outputs.

**Never:** No edits to `docs/contracts.md` or Partner A's modules. No new dependencies. No
transcripts, labels, Orange/Purple Book, IR workbooks or annual reports. No LLM calls. No raw
HTML/JSON committed.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| HAPPY_PATH | Script run with network | Raw files cached, 3 output files written | N/A |
| From cache | `--from-cache`, cache present | Same outputs, no network | Missing cache file → raise |
| XBRL duplicates | Same period reported in several filings | One fact per (metric, period end), latest filing wins | N/A |
| Fetch failure | SEC returns error | Exit non-zero; existing outputs untouched | Write to temp dir, rename only on success |

</frozen-after-approval>

## Code Map

- `docs/contracts.md` Contract 1 -- field names and enums for the three files.
- `docs/conventions.md` -- ID formats, `REPO_ROOT` + `pathlib`, JSON/JSONL writing rule,
  `logging` not `print`, stdlib only, offline tests.
- `_bmad-output/brainstorming/brainstorm-architecture-open-questions-2026-10-17/source-probe/probe.py`
  -- reuse its SEC access pattern (User-Agent, 0.15 s sleep); do not import it.
- Sources (verified 2026-10-17, CIK 353278):
  `0000353278-26-000023/caq22026.htm` (filed 2026-08-04, period end 2026-06-30, 38
  `page-break-after:always`, printed "Page N of 37");
  `0001628280-25-038087/caq22025.htm` (filed 2025-08-06, period end 2025-06-30, 40 breaks,
  "Page N of 39"); `data.sec.gov/api/xbrl/companyfacts/CIK0000353278.json` (ifrs-full, DKK,
  annual only, values in full DKK).
- Verified text: Q2 2026 balance sheet `Trade receivables 88,949 70,856`; Q2 2025
  `Trade receivables 69,575 71,949`.
- `pharmer/llm.py` -- not used.

## Tasks & Acceptance

**Execution:**
- [x] `scripts/build_sample_corpus.py` -- fetch (or `--from-cache`) into git-ignored
  `data/sample/NVO/_raw/`, then build:
  - **manifest** in fixed order `NVO-6K-2026Q2`, `NVO-6K-2025Q2`, `NVO-XBRL`. The two 6-Ks are
    `doc_type "6-K"` with `fiscal_period` `Q2 2026` / `Q2 2025` (half-year reports; findings
    anchor on `H1 …`). `period_end`, `filed_date`, `accession` and `url` come from the
    submissions JSON. `NVO-XBRL` has `doc_type "xbrl"`, `fiscal_period "FY2025"` (latest year
    covered), `url` = the companyfacts URL, and `accession` null. Every row has
    `source "pre-collected"` and `collected_date` = the cache date, which is stored in the
    cache, not the current time.
  - **pages**: split on `page-break-after:always`; drop segment 0 (SEC cover); `page` = the
    printed `Page N of M` number. A segment with no printed number takes the number of the page
    before it + 1.
  - **cleaning**: unescape entities, collapse whitespace; strip the repeated per-page banner,
    nav menu and `Company announcement No …` line.
  - **chunks**: tables are their own chunks (cells joined by one space, rows by `\n`, split
    only at row boundaries, `is_table: true`); prose in ~1,500-char paragraph groups.
    **Split tables keep their header:** work on the cell lists before joining. A cell is an
    *amount* if it matches `^[(\-–]?\d[\d,.]*\)?\s?%?$` and is not a bare year
    `19xx`/`20xx`. The table's *header* = its leading rows before the first row that has an
    amount cell, at most 3 rows. When a table is split, every chunk after the first starts with
    the header rows, then its own rows; the 1,500-char limit applies to the body rows only.
    (A table that continues onto the next printed page is out of scope.)
    `section` = first heading on the page after the banner. `chunk_id` =
    `<doc_id>-p<page>-c<nn>` (two digits, from `01`, restarting per page).
  - **facts**: concept map `revenue`=`Revenue`, `trade_receivables`=`CurrentTradeReceivables`,
    `inventory`=`Inventories`, `operating_profit`=`ProfitLossFromOperatingActivities`,
    `net_profit`=`ProfitLoss`. Keep annual (`FY`) periods FY2023–FY2025, one row per (metric,
    `end`), with the latest filing winning. `fiscal_period` = `FY<end year>`, value =
    round(DKK / 1e6) with `unit "millions"`, `doc_id "NVO-XBRL"`, `source_kind "xbrl"`.
  - **output**: sorted (manifest fixed, chunks by doc/page/n, facts by `fact_id`), written via a
    temp dir and rename.
- [x] `data/sample/NVO/{manifest.json,chunks.jsonl,facts.jsonl}` -- generated, committed.
- [x] `.gitignore` -- add `data/sample/*/_raw/`.
- [x] `tests/test_sample_corpus.py` -- offline checks on the committed files (ACs below).
- [x] `tests/test_build_sample_corpus.py` -- offline builder tests for every I/O matrix row
  (fake `_sec_get`, `OUT_DIR`/`RAW_DIR` patched to a temp dir; replay the real `_raw/` cache
  when present, skip otherwise).
- [x] `docs/build-log.md` -- append a `decision` entry (sample corpus, real data, lean scope)
  and a `test` entry.
- [x] `_bmad-output/implementation-artifacts/deferred-work.md` -- annual reports entry.

**Acceptance Criteria:**
- Given the committed corpus, when tests run, then every manifest row has every Contract 1 key
  and valid enums, every `chunk_id` is unique and matches `^<doc_id>-p\d+-c\d{2}$`, and every
  chunk and fact `doc_id` is in the manifest.
- Given the Q2 2026 6-K chunks, then exactly one `is_table` chunk contains
  `Trade receivables 88,949 70,856`, and a chunk on page 14 came from the segment printed
  `Page 14 of 37`.
- Given `facts.jsonl`, then `trade_receivables` FY2024 = 71949 and FY2025 = 70856. These are
  the same figures as the 6-K comparative columns (verified in the chunk text by the test, not
  hand-typed).
- Given the manifest, then each 6-K `url` equals
  `https://www.sec.gov/Archives/edgar/data/353278/<accession without dashes>/<document>`,
  `period_end` is 2026-06-30 / 2025-06-30, `filed_date` is 2026-08-04 / 2025-08-06, and the
  `NVO-XBRL` `filed_date` is a date not before its `period_end`.
- Given the chunks, then no `section` is empty, the page-14 chunks of `NVO-6K-2026Q2` have
  section `CASH FLOW AND CAPITAL ALLOCATION`, and the `Trade receivables 88,949 70,856` chunk
  has section `APPENDIX 3: CONDENSED BALANCE SHEET`.
- Given a split table (e.g. `NVO-6K-2026Q2` page 30 sales split), then every continuation
  table chunk starts with the same header lines as the table's first chunk (the test derives
  the header from the first chunk; it does not hand-type it).
- Given any chunk, then it does not contain the repeated nav string
  `Financials Cash flow and capital allocation Outlook`.
- Given the cache, when the script runs twice with `--from-cache`, then the outputs are
  byte-identical (manual check; not part of the offline suite).

## Implementation Notes

- SEC fetch uses `curl` via `subprocess` (as the source probe does): the python.org macOS
  Python on the dev machine has no CA bundle, so `urllib` fails TLS verification.
- Raw cache also holds `cache_meta.json` (`collected_date`, UTC date of the fetch).
- Section heading rule: first non-table, non-dateline paragraph on the page if it is short
  (<= 120 chars, no trailing `.,;:`, not a `1)` footnote); a page opening with running
  prose or only tables continues the previous page's section; page 1 uses its headline.
- The front page has no printed number and becomes page 1; the trailing SIGNATURES page has
  none either and becomes 38 (Q2 2026) / 40 (Q2 2025) under the prev + 1 rule.
- `NVO-XBRL` `period_end` = latest kept period end; `filed_date` = filing date of the newest
  20-F that supplied a kept fact (the spec did not fix these two).
- Single paragraphs or table rows longer than 1,500 chars are not split (max chunk 1,740).
- Matrix audit (step 3) added `tests/test_build_sample_corpus.py`: offline fakes for
  `_sec_get` cover HAPPY_PATH, from-cache (identical + missing file raises), XBRL duplicates
  (latest filing wins) and fetch failure (outputs and `_raw/` untouched).

## Spec Change Log

- **Loop 1 (review, 2026-10-17).** Trigger: split tables lost their column header in
  continuation chunks (e.g. `NVO-6K-2026Q2-p30-c03` has numbers but not
  `DKK million Total US Operations …`), so hunters cannot tell which column is which period.
  Amended: chunks rule (header repetition, defined header detection), new test task, three new
  ACs (manifest provenance values, section values, split-table header). Known-bad state
  avoided: table continuation chunks without headers; manifest URL/dates and `section` with no
  test pinning them. **KEEP:** everything in Implementation Notes above (curl fetch,
  `cache_meta.json` UTC collected date, section heuristic, front/SIGNATURES page numbering,
  XBRL row `period_end`/`filed_date` rule, unsplit oversize rows); the `_BlockParser` design
  (inline `<font>` runs joined without spaces, `<br>`/`<div>` in cells become spaces);
  boilerplate detection (banner + `Page N of M` in one block gives the printed page, whole
  nav-menu block dropped); XBRL selection (`fp FY`, >=360-day duration, latest
  `(filed, accn)` wins); temp-dir writes; the offline matrix tests in
  `tests/test_build_sample_corpus.py`. Also: write the zero-width space in `_clean` as
  `"\u200b"`, not a literal invisible character; build-log entries use the date only
  (`- 2026-10-18 [B] decision: …`), like earlier entries. Reuse the existing git-ignored
  `data/sample/NVO/_raw/` cache (`--from-cache`); do not refetch from SEC.

## Review Triage Log

| # | Finding | Verdict | Evidence / route |
|---|---------|---------|------------------|
| 1 | Split tables lose column header in continuation chunks | medium | p30-c03, p26-c03, p27-c04 etc. start with data rows, header only in first chunk. bad_spec → loop 1 |
| 2 | Manifest url/period_end/filed_date untested (VG) | medium | Pre-verified by mutation (dashless URL, swapped dates, null filed_date all pass). Folded into loop 1 ACs |
| 3 | Chunk `section` value never tested (VG) | medium | Pre-verified by mutation (`return ""` passes). Folded into loop 1 ACs |
| 4 | Zero-width space written as invisible literal in `_clean` | low | Direct correction. Folded into loop 1 KEEP |
| 5 | Build-log entries add a time, unlike earlier entries | low | Earlier entries are date-only. Folded into loop 1 KEEP |
| 6 | `chunks.jsonl` missing from diff | false | Left out of the review diff on purpose (265 KB generated); not git-ignored, will be committed |
| 7 | No H1 / filing_table facts | false | Contract 1c does not require them; the H1 Medium cap is the designed outcome (Design Notes) |
| 8 | 6-Ks labelled `Q2 2026`, Contract 2 anchor says `H1 2026` | medium | Manifest matches the Contract 1 example exactly; the tension is between Contracts 1 and 2 (pre-existing). defer |
| 9 | Cells joined by one space | rejected | Spec mandates single-space join; fix edits this spec |
| 10 | `<th>`, `<p>`, nested tables not handled | false | Both raw 6-Ks have 0 `<th>` and 0 `<p>`; script handles exactly these inputs |
| 11 | Page numbers can collide / fall back silently, no builder guard | low | No collision in current data; committed-output test catches duplicate chunk_ids; guard adds branches. rejected |
| 12 | Builder/footer tests skip on a fresh clone (no `_raw/`) | low | Real, but a fix means committing raw/synthetic HTML (spec: no raw HTML committed). rejected |
| 13 | No unit tests for parser/heading/group helpers | low | Covered end-to-end by replay tests; adds test surface only. rejected |
| 14 | Missing concept / DKK unit raises bare KeyError | false | Loud failure on an unreachable input is correct behavior |
| 15 | `fp == "FY"` admits non-year-end instants (duplicate fact_id) | false | Cached companyfacts: 0 FY rows in 2023-2025 with end not 12-31 for all 5 concepts |
| 16 | Float division / round-half-even alters values | false | All 5 concepts' DKK values are exact multiples of 1e6 |
| 17 | SEC User-Agent placeholder email, no retry | low | Fetch succeeded; real contact is a personal detail to choose. rejected |
| 18 | Raw-cache / output swap not atomic as a set; temp dirs not ignored | low | Needs a crash mid-rename; one-off dev script. rejected |
| 19 | Section heuristic picks bad headings | false | All 53 section values in the corpus are real headings; 0 empty |
| 20 | XBRL manifest row semantics loose | low | Documented in Implementation Notes. rejected |
| 21 | No page breaks → zero chunks silently | low | Unreachable on cached inputs; adds a guard. rejected |
| 22 | >99 chunks per page breaks c\d{2} | false | Max 7 chunks per page |
| 23 | Nav-marker table block may carry real rows | false | Every dropped block is the 244/254-char menu only |
| 24 | No facts → bare `max()` ValueError | false | Loud failure on unreachable input |
| 25 | Accession aged out of `filings.recent` → KeyError | false | Loud failure; both accessions present |
| 26 | Empty reportDate/filingDate | false | Both present in cached submissions; manifest dates set |
| 27 | curl 200 with empty body replaces good cache | low | Unlikely (`-f` fails on HTTP errors); adds guard. rejected |
| 28 | Fetch OK but build fails → old cache already gone | low | Refetch restores it; one-off script. rejected |
| 29 | Non-UTF-8 HTML → UnicodeDecodeError | false | Loud failure; both files decode |
| 30 | Comparative-cell parse crashes on parentheses/dash | false | Loud test failure is acceptable; the cell is a plain number |
| 31 | Footer test false-fails after mid-text stripping | false | Test passes on the real data |
| 32 | accession/url taken from constants, not submissions | false | `build_manifest` looks the accession up in submissions and checks primaryDocument + form; values identical |
| 33 | (loop 1) `chunks.jsonl` missing from diff (BH, VG) | false | carried #6 — left out of the review diff on purpose; not git-ignored, will be committed |
| 34 | (loop 1) Parser/chunker tests skip without `_raw/` (VG, BH) | low | carried #12 — committing raw HTML is forbidden by the spec; committed outputs are pinned. rejected |
| 35 | (loop 1) No H1 / filing_table facts (BH) | false | carried #7 |
| 36 | (loop 1) `Q2` vs `H1` fiscal_period label (BH) | medium | carried #8 — defer (already logged) |
| 37 | (loop 1) Bare KeyError on missing concept / accession (BH) | false | carried #14 |
| 38 | (loop 1) `<th>` / nested tables ignored (BH) | false | carried #10 |
| 39 | (loop 1) Placeholder SEC User-Agent (BH) | low | carried #17. rejected |
| 40 | (loop 1) Temp dirs not ignored; output swap not atomic as a set (BH) | low | carried #18. rejected |
| 41 | `main()` docstring promises all-or-nothing (BH) | low | Renames happen one at a time after a successful build; direct docstring correction. patch |
| 42 | Section set once per page, not per heading (BH) | rejected | Spec mandates "first heading on the page"; fix edits this spec |
| 43 | SIGNATURES page gets unprinted number (BH) | rejected | Spec mandates prev + 1; fix edits this spec |
| 44 | Page rule is a contract change, only in build log (BH) | false | The intent already says the PR asks Partner A to confirm or veto the page rule |
| 45 | No test of fact completeness (5 metrics × 3 years) (BH) | low | A missing year would be dropped silently; one assertion. patch |
| 46 | `ProfitLoss` labelled `net_profit`; XBRL `accession` null (BH) | rejected | Both mandated by the spec; fix edits this spec |
| 47 | Chunks can exceed CHUNK_LIMIT with header (BH) | false | Spec: the limit applies to body rows only; oversize single rows are a KEEP note |
| 48 | curl `-s` hides HTTP error detail (BH) | low | A 403 is indistinguishable from a timeout; direct correction (`-sS` + stderr). patch |
| 49 | Test copies `AMOUNT_RE`/`YEAR_RE` from the script (BH) | false | An independent copy is what keeps the test from being tautological; no named harm |
| 50 | New `scripts/` and `data/sample/` folders not in docs/conventions.md (BH) | low | Real; the fix edits an agent-read rules doc. defer |
| 51 | `collected_date` is the UTC date (BH) | low | Documented in Implementation Notes. rejected |
| 52 | Footnote-marker cell makes a header row look like an amount row (ECH) | false | Every split table in both 6-Ks: the detected header holds the column labels (checked) |
| 53 | Banner block with real content dropped (ECH) | false | No banner block over 200 chars in either 6-K |
| 54 | `page-break-before` / other casing not split (ECH) | false | Both cached 6-Ks use `page-break-after:always` only (38 / 40 breaks) |
| 55 | First page opening with a table gets empty section (ECH) | false | 0 empty sections; page 1 of both 6-Ks opens with prose |
| 56 | Multi-year FY durations win as annual (ECH) | false | 0 FY rows over 371 days in the 5 cached concepts |
| 57 | Split-table header test hard-codes 3 (ECH) | low | Would diverge if HEADER_MAX_ROWS changes; direct correction. patch |
| 58 | Chunks not explicitly sorted by doc/page/n (ECH claim) | low | Spec task says sorted; output is in parse order (happens to be sorted). Direct fix. patch |

## Design Notes

XBRL is annual-only for Novo, so half-year 6-K figures have no second source in this sample.
Under the number-tie rule, findings anchored on H1 figures therefore cap at Medium. This is
expected, not a bug.

## Verification

**Commands:**
- `.venv/bin/python scripts/build_sample_corpus.py` -- expected: exits 0, writes 3 files.
- `.venv/bin/python scripts/build_sample_corpus.py --from-cache` twice, then
  `shasum data/sample/NVO/*.json*` -- expected: identical hashes.
- `.venv/bin/python -m unittest discover tests` -- expected: all pass, no network.
