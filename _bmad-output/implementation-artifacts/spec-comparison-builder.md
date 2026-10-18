---
title: 'Comparison Builder: 5-dimension product grid vs named rivals (+ sample labels)'
type: 'feature'
created: '2026-10-18'
status: 'done'
baseline_commit: 'f7ba7bb314e86f02a3b2a89d94c40b4f38da6bde'
route: 'dispatch'
review_loop_iteration: 0
context: ['{project-root}/docs/contracts.md', '{project-root}/docs/conventions.md']
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** `orchestrator.py finish` skips the `comparison` stage ("not built"), so the Synthesizer runs with `grid_used: false` and the report prints "Comparison table not built for this run." The case needs a 5-dimension product comparison against named rivals. The sample corpus has no FDA labels to fill it from.

**Approach:** (1) Extend `scripts/build_sample_corpus.py` to bundle the latest DailyMed SPL labels for Ozempic (Novo, labeler 0169) and Mounjaro (Lilly, labeler 0002) into `data/sample/NVO`. Each label becomes a `doc_type: "label"` manifest entry, with `application_no` and `labeler_code` set and chunks split by label section. (2) Add `pharmer/comparison.py` with `run(run_dir)`. It reads `product` and `rivals` from `run.json`, asks Claude once per product column to fill the 5 dimensions from the corpus chunks only, checks every cell in code, and writes `runs/<run_id>/comparison.json`. (3) `orchestrator.py finish` accepts `--rivals "A,B"` and stores it in `run.json` as `"rivals": [..]`.

**Decisions (human):** Facts may come from any corpus doc, with `label` chunks preferred. Rivals are named by the analyst (`--rivals`), not inferred. comparison.json keeps the current `{"columns","rows"}` shape with no per-cell citations, so the report and contracts stay as they are. Validated chunk_ids go to a sidecar `comparison_sources.json` (not a contract file). Labels and the builder ship in one spec and one PR.

## Boundaries & Constraints

**Always:** Every Claude call goes through `llm.call` (OPUS), with a byte-identical system prompt and the run data in the user message. A cell is kept only if its cited chunk_id exists in `chunks.jsonl` and its quote is a verbatim substring of that chunk's text. Any other cell becomes `"not disclosed"`. Rows are exactly the five §4.7 dimensions, in order: Approved uses, Boxed warning / safety, Dosing / convenience, Patent / exclusivity, Pricing & gross-to-net. Columns are `["Dimension", product, *rivals]`. On API error, refusal, or non-JSON output, the stage retries once with the problems appended, then fills that column with `"not disclosed"`. It never raises. Label fetches use the DailyMed web services, are cached in `_raw/` so `--from-cache` rebuilds offline, and keep only SPLs from the allowlisted labeler code.

**Never:** No verdict or interpretive sentence (that belongs to the Synthesizer). No facts from model memory. No edits to `librarian.py`, `fact_checker.py`, or quant (Partner A). No edits to `docs/contracts.md`; the `run.json` `rivals` key is proposed in the PR for A's sign-off. No UI change in this spec.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Happy path | product=Ozempic, rivals=[Mounjaro], label chunks present | 5×3 grid; label-backed cells filled | N/A |
| No product | run.json lacks `product` | no comparison.json; returns with a logged reason | report shows NO_COMPARISON |
| No rivals | `rivals` missing/empty | grid with the product column only | logged |
| Unsourced cell | bad chunk_id or non-verbatim quote | `"not disclosed"` | count logged |
| Call fails twice | API error/refusal/non-JSON | column all `"not disclosed"` | never raises |

</frozen-after-approval>

## Code Map

- `scripts/build_sample_corpus.py` -- B's dev fixture standing in for the Librarian. Reuse `fetch_raw`/`_check_cache`/`build_chunks` patterns and write into the same manifest and chunks. `tests/test_build_sample_corpus.py` and `tests/test_sample_corpus.py` check it.
- `docs/contracts.md:20-45` -- Contract 1 manifest/chunk fields. Label docs set `application_no` (`NDA209637` = Ozempic) and `labeler_code`.
- `orchestrator.py:54-59` -- FINISH_STAGES already lists `comparison` as optional. `prepare_finish` (l.226) and `finish_run` (l.277) plus argparse (l.317) take `rivals`, which follows `product`'s semantics (None keeps the stored value, empty clears it).
- `pharmer/synthesizer.py` -- pattern to copy: `read_json_or_none`, `write_json`, `llm.call` with json_schema output, one retry with the problems appended.
- `pharmer/report.py:243-262` -- reads `{"columns","rows"}` as is. Do not change it.
- `tests/test_synthesizer.py`, `tests/test_orchestrator.py` -- fake-llm and CLI test styles to reuse.

## Tasks & Acceptance

**Execution:**
- [x] `scripts/build_sample_corpus.py` -- fetch and cache 2 SPLs, chunk them by section (`section` = SPL section title, `page` = section ordinal), and append them to the manifest and chunks.
- [x] `data/sample/NVO/*` -- regenerate with the new labels.
- [x] `pharmer/prompts/comparison.md` -- system prompt: for one product, return per dimension `{value, chunk_id, quote}` or `"not disclosed"`.
- [x] `pharmer/comparison.py` -- `run(run_dir)`: select chunks (that product's label chunks plus filing chunks naming it), one call per column (asyncio.gather), validate, write comparison.json and comparison_sources.json.
- [x] `orchestrator.py` -- `--rivals` flag, wired through to run.json.
- [x] `tests/test_comparison.py` -- fake llm; every I/O row. Extend the orchestrator and sample-corpus tests for rivals and label docs.
- [x] `docs/build-log.md` -- append the decision entry.

**Acceptance Criteria:**
- Given the sample NVO corpus, when `finish RUN --product Ozempic --rivals Mounjaro` runs, then comparison is "ok" and the report renders a 5-row table whose filled cells trace to label chunks in comparison_sources.json.

## Verification

**Commands:**
- `.venv/bin/python -m unittest discover tests` -- expected: all pass
- `.venv/bin/python scripts/build_sample_corpus.py --from-cache` -- expected: rebuilds offline with 2 label docs

## Review Triage Log

| # | Finding | Verdict | Evidence | Route |
|---|---------|---------|----------|-------|
| 1 | Cell may cite a chunk not sent to its column (blind+edge) | medium | `check_cell` validates against corpus-wide `chunks_by_id` | patch |
| 2 | Rival column filled from other company's filings naming it | medium | `select_chunks` filing filter is name-only, no ticker tie | patch |
| 3 | Quote across a line break rejected | medium | chunk text holds 385 `\\n`; raw `in` check | patch |
| 4 | "Not disclosed." variants counted as rejections (blind+edge) | low | exact casefold equality | patch |
| 5 | Oversized filing chunk ends selection (blind+edge) | low | `size` added then `break` | patch |
| 6 | Filing cap untested (VG) | medium | no test hits MAX_FILING_* | patch |
| 7 | Label cache in CACHE_FILES skips 6-K replay tests on A's machine (VG) | medium | `HAVE_RAW` at tests/test_build_sample_corpus.py:25 needs all files | patch |
| 8 | Label-rule guards (app no., next_page_url) untested (VG) | low | no test hits them | patch |
| 9 | Two rival normalizers disagree (blind+VG) | low | `parse_rivals` case-sensitive, keeps product | patch |
| 10 | Pause hint prints literal `--rivals "A,B"` | low | orchestrator.py:16/_pause_summary | patch |
| 11 | Prompt says "numbered", never explains doc_type | low | prompts/comparison.md:5 | patch |
| 12 | Broad except hides programming errors | low | no traceback logged; direct fix log.exception | patch |
| 13 | Name match has no aliases / generic name hits several labels (blind+edge) | low | analyst types brand; sample labels are distinct molecules; fix adds logic | reject |
| 14 | Label chunks uncapped | low | 2 labels, ~110 chunks; budgeting adds logic | reject |
| 15 | Rejected cells not retried | low | spec retries only API/refusal/non-JSON; enhancement | reject |
| 16 | Sources sidecar unread / no contracts note | false | human decision: keep shape; contracts.md off-limits | reject |
| 17 | Patent row will be mostly empty (no Orange Book) | medium | data gap outside this intent | defer |
| 18 | Malformed SPL XML raises AttributeError (blind+edge x3) | low | dev script over 2 fixed cached SPLs | reject |
| 19 | One-word quote passes check | low | heuristic guard adds complexity | reject |
| 20 | select_chunks outside try can KeyError | false | `_read_jsonl` + manifest label rows always carry doc_id | reject |
| 21 | Non-API exception gets no retry | low | column still fails safely, never raises | reject |
| 22 | DailyMed NDC list pagination | maybe-false | would need an SPL with >1 NDC page; ours are small; if true only low | reject |
| 23 | Nested SPL tables duplicated | low | not in the 2 labels' scope | reject |
| 24 | parse_rivals non-str types | low | CLI passes strings only | reject |
| 25 | --rivals without product silently no grid | low | stage logs warning; report says not built | reject |
