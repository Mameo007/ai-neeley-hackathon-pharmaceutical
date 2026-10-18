---
title: 'Exec Report PDF (Partner B build step 4a)'
type: 'feature'
created: '2026-10-18'
status: 'done'
baseline_commit: '94bad63347ef1ce3f4137f5f0989b38197fdaba6'
route: 'dispatch'
review_loop_iteration: 0
context:
  - '{project-root}/docs/contracts.md'
  - '{project-root}/docs/conventions.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Synthesizer and CEO Move write JSON, but nothing turns the run into the sell-side-style PDF note the case asks for (§4.10), so the pipeline stops short of a deliverable.

**Approach:** `pharmer/report.py`, `async run(run_dir)`: a pure `build_model(run_dir) -> dict` (masthead, thesis, warnings, numbered findings, comparison, CEO callout, footnotes — plain data) and a thin `render_pdf(model, path)`. No Claude call. Writes `runs/<run_id>/report.pdf`.

## Boundaries & Constraints

**Always:**
- Masthead "PHARMER RESEARCH" / "We find the needle in the haystack." in our own plain styling; never the word "pharming".
- Every number and quote printed comes from the input files (findings, run, synthesis, ceo_move, comparison, manifest, chunks); the model adds only fixed labels.
- Thesis = Sharpest Needle (headline: its finding's selected reasoning claims) + Synthesizer verdict + `priced_in_call` in plain words; needle resolved with `ceo_move.resolve_needle` (same rule as CEO Move).
- Findings: one numbered row per finding holding a selected reasoning (slot = reasoning, so a composite row lists each selected reasoning with its own confidence badge); needle first, then by best reasoning score (null last), then findings order. Each reasoning shows claim, badge, quotes, impact (eps_pct / usd_m) and priced-in hits when present; "Recommended", "contested" (finding or reasoning flag) and "possibly seasonal" labels carried through.
- Footnotes: `[n]` markers, numbered in reading order (thesis → findings → CEO callout), deduped by rendered text "<company> <fiscal_period> <doc_type>, p.<page>"; finding/reasoning ids cited by verdict or move expand to their selected reasonings' chunk_ids. Plain text, no hyperlinks.
- Unresolvable chunk_id (not in chunks.jsonl, or doc not in manifest, or either file missing) → its own footnote "<chunk_id> — citation not found in this run's corpus" plus one warning in the model; never dropped.
- Visible warning boxes, never a raise: synthesis or ceo_move missing / status "failed" (with its error); `recommended_fallback` → "AI-recommended; not yet confirmed by the analyst"; needle pick invalid; no label/product finding among the selected; fewer than 5 selected reasonings. Missing comparison.json → "Comparison table not built for this run." Only a missing findings.json raises.
- Comparison: renders `{"columns": [...], "rows": [[...]]}` or a list of row dicts; any other shape → visible "unrecognized shape" note (shape is a proposal for the Comparison Builder).
- Text the PDF font can't encode is replaced by a fixed substitution map, then "?", never a crash.

- PDF library (human decision): **reportlab**, built-in Helvetica (cp1252); not strictly pure Python (pulls Pillow + charset-normalizer wheels, no compiler) — said so in the PR. `invariant=1` so output is reproducible.

**Never:** orchestrator, UI, email, Comparison Builder, edits to upstream stages or `docs/contracts.md`, real-API calls.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected |
|---|---|---|
| Happy | fixture run, all files ok | 5 findings, thesis with needle F-003, CEO box, footnotes 1..n |
| Dedupe | two citations same doc+page | one footnote number reused |
| Unresolvable | chunk_id absent | numbered "not found" footnote + warning |
| Composite | F-001-a and -b selected | one row, two badges |
| Failures | each failure state above | matching warning text, run() still writes the PDF |
| Smoke | run() | report.pdf exists, starts `%PDF` |

</frozen-after-approval>

## Code Map

- `pharmer/synthesizer.py` -- reuse `read_json_or_none`, `selected_findings`, `unique`; `PRICED_IN_CALLS` values.
- `pharmer/ceo_move.py` -- reuse `resolve_needle(run_info, selected)`; `TEXT_FIELDS` for the callout.
- `tests/fixtures/synthesis_findings.json` / `synthesis_run.json` -- base fixture (F-001..F-005 selected, F-004 label, F-001 Recommended, needle F-003; all cite `NVO-6K-2026Q2-p14-c03`). Tests mutate a temp copy for composite/flags/failures.
- `tests/test_synthesizer.py` -- temp-dir fixture pattern to copy.
- `requirements.txt` -- add the PDF library.

## Tasks & Acceptance

**Execution:**
- [x] `requirements.txt` -- add the chosen PDF library.
- [x] `pharmer/report.py` -- `build_model`, `render_pdf`, `run` per Boundaries.
- [x] `tests/fixtures/report_{synthesis,ceo_move,manifest}.json`, `report_chunks.jsonl` -- small, illustrative.
- [x] `tests/test_report.py` -- matrix rows + each failure state + "pharming" absent + smoke.
- [x] `docs/build-log.md` -- decision + test entries; flag comparison.json shape as a proposal.

**Acceptance Criteria:**
- Given any combination of missing/failed optional inputs, when `run()` runs, then report.pdf is written and each problem appears as a warning in the model.
- Given the PDF text, then no hyperlink annotations and no "pharming".

## Implementation Notes

- Implemented directly (user asked for no implementation subagent). Files: `pharmer/report.py`, `tests/test_report.py`, `tests/fixtures/report_*`, `requirements.txt` (`reportlab>=4.0`).
- `render_pdf(model, path, compress=True)`; `compress=False` lets tests byte-search page text (no PDF parser dependency).
- Boxes are one table row per paragraph so a long warnings/CEO box splits across pages; the unresolved-citation warning lists 10 ids then "and N more (see Sources)".
- A CEO move written for another Needle stays shown but is marked "Out of date" inside the box.

## Verification

**Commands:**
- `.venv/bin/python -m unittest discover tests` -- expected: all pass, offline.

## Review Triage Log

| # | Source | Finding | Verdict | Route / evidence |
|---|---|---|---|---|
| 1 | verification-gap | PDF content never checked, only `%PDF` | medium | patch: `compress=False` + byte-search tests for masthead, badge, footnote, CEO fields, warnings |
| 2 | verification-gap | corrupt chunks.jsonl line untested | low | patch: test added (cheap) |
| 3 | edge-case | non-dict synthesis/ceo_move JSON → AttributeError | medium | patch: treated as missing + warning |
| 4 | edge-case | single-cell box taller than a page → LayoutError | medium | patch: one row per paragraph; long-box test |
| 5 | edge-case | ragged comparison rows / empty columns | low | patch: pad/trim; empty columns → unrecognized |
| 6 | edge-case + blind | needle with empty claims prints "No Sharpest Needle" | low | patch: fall back to anchor text, then id |
| 7 | blind | stale CEO move shown as current in the box | medium | patch: "Out of date" line inside the box |
| 8 | edge-case | build-log entries missing although task marked done | medium | patch: entries appended |
| 9 | edge-case + blind | "Only N of 5" counts reasonings | false | frozen decision: slot = reasoning (scoring); 4 rows from 5 reasonings is a full selection |
| 10 | edge-case | flags/impact/chunk_id/citations of wrong type, NaN score | low | rejected: Contract 2 types, written by our own stages; guards add complexity |
| 11 | blind | verdict_used true while synthesis now hidden | low | rejected: rare re-run ordering; stale-move warning covers the common case |
| 12 | blind | Quant formula/inputs/FX, confidence_reason, trend, peer status, priced-in evidence not shown | medium | defer: content additions beyond §4.10's list |
| 13 | blind | comparison cells uncited, width uncapped | low | defer: with the Comparison Builder, which fixes the shape |
| 14 | blind | no date, page numbers, disclaimer | low | defer |
| 15 | blind | footnotes print `None` for missing manifest fields; same-label docs merge | low | rejected: Contract 1 requires those fields; same label = same text the reader sees |
| 16 | blind | inferred vs printed page not distinguished | medium | defer: lives in the Librarian sidecar, not Contract 1 |
| 17 | blind + verification-gap | product not read from synthesis.json | false | synthesis copies run.json's product; the analyst confirms it |
| 18 | edge-case | missing comparison not in warnings box | false | spec: visible "not built" note in the comparison section |
| 19 | blind | delivery / email / file naming | false | out of scope by intent |
