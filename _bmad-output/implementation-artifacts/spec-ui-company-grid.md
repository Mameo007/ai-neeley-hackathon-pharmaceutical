---
title: 'UI step 2: company tile grid replaces the new-run form'
type: 'feature'
created: '2026-10-18'
status: 'done'
route: 'oneshot'
review_loop_iteration: 0
context: ['{project-root}/docs/ui-plan.md']
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Starting a run is a bare company dropdown; a judge glancing at the screen cannot see that the build works on any of the 10 shortlist companies or what each company's story is (docs/ui-plan.md, hero screen 1 / build step 2).

**Approach:** Replace `new_run_form` with a 5×2 grid of dark tiles (ticker chip, name from `librarian.CIKS`, the case's starter thread, a "filings ready" badge when `data/sample/<TICKER>` holds a prebuilt corpus). Clicking a tile selects it (amber outline); a primary "Find the needle in <company> →" button starts the run. Starter threads live in a data file copied verbatim from the appendix of docs/neeley_ai_forward_case_study.md, with a test that each string appears in the case doc (same rule as data/products.json). The prebuilt-corpus toggle stays, below the grid. Logic in pure helpers above `main()`; mockup 1-company-grid.png is layout only.

</frozen-after-approval>

## Implementation Notes

- `data/starter_threads.json` (new): the 10 appendix bullets after "**Company:**", verbatim (full sentences, not the mockup's shortened copy), in librarian.CIKS order.
- `app.py`: pure helpers `load_threads`, `company_tiles` (CIKS order, thread, `ready` = `prebuilt_corpus` exists), `default_company` (first ready tile, else first), `tile_html` (all values `html.escape`d). Render: `company_grid` (5 columns, keyed `tile-<T>` containers, selection in `session_state["company"]`, selected outline via a per-render style tag) and `new_run_form` (header, subtitle, grid, prebuilt toggle "Use the prebuilt filings", CTA "Find the needle in <name> →", caption).
- Whole-tile click: a real `st.button` (key `pick-<T>`) inside each tile, stretched over it with opacity 0 via its `st-key-pick-*` wrapper class. First attempt targeted `stButton` and missed: its `stElementContainer` wrapper is `position: relative`, so the overlay sat below the tile at 26px wide — caught by a DevTools hit-test, not by AppTest. Focus ring via `:has(button:focus-visible)`.
- Subtitle says "Annual and quarterly reports from SEC EDGAR" — the mockup's "earnings calls and FDA labels" is not true for a live run (Call Decoder deferred; labels only in the prebuilt NVO corpus).
- Names come from librarian.CIKS (SEC spelling "Bristol-Myers Squibb"), as the plan says.
- Tests: 29 app tests (starter threads ⊆ case doc, tile helpers, escaping, default company, page header + 10 tile buttons + CTA, tile click → Amgen run with no corpus, prebuilt toggle on NVO). Full suite 393 OK; api_usage.csv untouched.
- Review patches: docstring no longer claims the default runs offline; toggle help in plain English (no repo path); thread test pins each thread to its own company's appendix line; chosen tile's button label ends "(selected)"; test that toggling NVO on then picking Amgen starts with corpus None. 30 app tests.

## Review Triage Log

- medium (patched): `default_company` docstring said the default "works offline" but the toggle still starts off (live SEC). Kept the toggle default (unchanged behaviour, plan: "toggle stays"); fixed the docstring.
- medium (patched): toggle help showed `data/sample/NVO` and "demo fallback" — internal names, against ui-plan. Rewritten in plain English.
- low (patched): thread test only checked "appears somewhere" — swapped threads would pass. Now checks `* **<Case name>:** <thread>`.
- low (patched): selection shown by colour only — hidden button label now "<name> (selected)".
- low (patched): no test for stale prebuilt choice when switching tiles — added (code was already right: the toggle isn't rendered for Amgen).
- low (rejected): stale/unknown `session_state["company"]` fallback untested — one-line guard, exercised on every first load.
- low (rejected): tests depend on data/sample holding only NVO — the existing `test_prebuilt_corpus_only_when_complete_and_same_ticker` already does; a second sample corpus is a deliberate change that should update these.
- low (rejected): CSS relies on st-key-* / data-testid and fixed min-height — inherent to styling Streamlit; checked by screenshot at 1600 and 1280 (AZN tile grows ~20px at 1280, still clickable).
- low (patched in commit): no build-log entry.
