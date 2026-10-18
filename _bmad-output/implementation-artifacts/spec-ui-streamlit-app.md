---
title: 'Streamlit UI (Partner B build step 4c)'
type: 'feature'
created: '2026-10-18'
status: 'done'
baseline_commit: 'e4b36ba5b0ff83e926b4197a806bfb9bcf4185b6'
route: 'dispatch'
review_loop_iteration: 0
context:
  - '{project-root}/docs/conventions.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** The pipeline runs only from the CLI; the analyst's three choices (start a company, name the Sharpest Needle, confirm the product) and the PDF download need a screen for the live demo.

**Approach:** `app.py` (Streamlit, repo root) over `orchestrator` and `pipeline.json`: start a run in a background thread, show live stage progress, show the selected findings for review, take the Needle + product, run `finish`, offer the PDF. Pure helpers in `app.py` (importable without rendering) carry the logic so tests rarely touch Streamlit.

## Boundaries & Constraints

**Always (human decisions):** download only, no email; sidebar run picker over `runs/` (resume a paused run, re-pick the Needle, re-download); clean functional look (standard components, PHARMER RESEARCH header + tagline, colored confidence badges); corpus = live Librarian, with a "use prebuilt corpus" toggle only when `data/sample/<TICKER>/` holds one.

**Always:**
- Company list = `librarian.CIKS`. Product dropdown per ticker from `data/products.json`, hand-curated from the case appendix competitive sets; a test proves every product name appears in `docs/neeley_ai_forward_case_study.md`. Plus "Not confirmed" (default; no stage pre-selects yet).
- Review screen reuses `report.build_model` (rows, badges, labels, quotes, "filing, p.N" sources) and adds per reasoning the score breakdown from `scoring.materiality/obscurity/score` and its hunters — the "why selected" of §5.1.6. Needle radio over selected findings, default = run.json pick, else Recommended.
- Long work never blocks the page: `start` and `finish` run in a background thread (registry via `st.cache_resource`); a fragment polls `pipeline.json` every 2 s. A run at phase `running` with no live thread shows as interrupted.
- Stage table from `pipeline.json` with skipped/degraded/failed highlighted; refusals (`UsageError`) shown as an error message, never a traceback.
- Done run: verdict + CEO action shown, PDF download button.
- `orchestrator.py` gains `prepare_run(ticker, corpus, runs_dir) -> run_dir` (validation + folder + pipeline.json), with `start_run` = prepare + stages, so the UI learns the run folder before the stages start. Behavior otherwise unchanged.
- `streamlit` added to requirements.txt.

**Never:** email, stage-module or `docs/contracts.md` edits, real API in tests.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected |
|---|---|---|
| Run list | runs with each phase, a stuck `running` | newest first, labelled; stuck one marked interrupted |
| Review | scored fixture run | rows with badges, score breakdown, sources; default needle = Recommended, or run.json pick |
| Products | NVO | appendix products + "Not confirmed"; unknown ticker → only "Not confirmed" |
| Prebuilt | NVO / LLY | toggle offered / not offered |
| Page smoke | AppTest, fixture paused + done runs | renders, no exception; done run shows download |

</frozen-after-approval>

## Code Map

- `orchestrator.py` -- `start_run`, `finish_run`, `read_status`, `UsageError`, `RUNS_DIR` (resolve at call time).
- `pharmer/report.py` -- `build_model(run_dir)` for rows/sources; `pharmer/scoring.py` -- `materiality`, `obscurity`, `score`.
- `pharmer/synthesizer.py` -- `read_json_or_none`, `selected_findings`.
- `tests/fixtures/synthesis_findings.json`, `report_*` -- fixture run.

## Tasks & Acceptance

**Execution:**
- [x] `orchestrator.py` -- split `prepare_run` out of `start_run`.
- [x] `data/products.json` -- appendix products per ticker.
- [x] `app.py` -- helpers + `main()` page; `if __name__ == "__main__": main()`.
- [x] `requirements.txt` -- `streamlit`.
- [x] `tests/test_app.py` -- matrix rows; AppTest smoke.
- [x] `docs/build-log.md` -- decision + test entries; how to launch.

**Acceptance Criteria:**
- Given `streamlit run app.py`, when a paused run is picked, a needle and product chosen and Finish pressed, then the page shows progress and then the download button, without blocking.

## Implementation Notes

- Implemented directly (user asked). orchestrator also split finish (prepare_finish/run_finish_stages) and its finish gate became "start reached the pause and nothing is running" so a failed finish can be retried.
- Manual check: headless Chrome over DevTools (the Chrome extension was not connected); `?run=<name>` added to open a run directly. Fixed from screenshots: finding ids on cards, stage warning outside the expander, stale phase labels in the keyed selectbox.

## Verification

**Commands:**
- `.venv/bin/python -m unittest discover tests` -- expected: all pass, offline.
- `.venv/bin/streamlit run app.py --server.headless true` on a fixture run -- expected: page loads (screenshot by browser if available).

## Review Triage Log

| # | Source | Finding | Verdict | Route / evidence |
|---|---|---|---|---|
| 1 | blind + edge | "Not confirmed" cannot clear a saved product | medium | patch: product "" pops the key; UI sends "" |
| 2 | verification + blind + edge | finish cut off by a restart can never be retried | medium | patch: analyst can mark the run failed; gate then allows finish |
| 3 | blind + edge | job crash outside stages leaves "running", UI blames a restart | medium | patch: _job records phase failed + error; page shows it |
| 4 | blind + edge | two submits race to two finish threads | medium | patch: server-wide lock + live-job refusal |
| 5 | blind | filing text into st.markdown unescaped ($ → LaTeX) | high | patch: md() escaping for claims, anchors, quotes, sources, verdict, move |
| 6 | blind | report warnings filtered by wording | medium | patch: all listed in an expander |
| 7 | blind | degraded verdict/move shows no reason | low | patch: error shown |
| 8 | blind | products.json display text in product names | low | patch: clean names |
| 9 | blind | streamlit>=1.45 untested | low | patch: >=1.66 |
| 10 | verification + blind | submit / Start toggle / interrupted page untested | medium | patch: AppTest clicks added; surfaced the __main__ vs `app` patch miss (real 401 calls) — fixed |
| 11 | edge | CLI-run job shown as interrupted in the UI | low | patch via #2: wording says "or it is running from the command line"; marking is the analyst's call |
| 12 | blind | IndexError on a row without reasonings | false | build_model rows only hold findings with selected reasonings |
| 13 | blind | gate locks out pipeline.json without scoring_after_peer | false | every start writes it; test_failed_finish_can_be_retried covers failed-finish |
| 14 | edge | findings.json missing/non-list; non-dict synthesis; started_at non-string; stages null | low | rejected: our own stages write these files |
| 15 | edge | OSError from prepare_run shown as a trace | low | rejected: Streamlit shows the exception; not a normal path |
| 16 | verification | fragment polling → rerun not automated | low | defer-equivalent: covered by the manual headless-Chrome finish run |
