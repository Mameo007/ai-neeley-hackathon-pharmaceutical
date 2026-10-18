---
title: 'Orchestrator (Partner B build step 4b)'
type: 'feature'
created: '2026-10-18'
status: 'done'
baseline_commit: '1c329e235423b15a990b5c79f52b907c2660ec23'
route: 'dispatch'
review_loop_iteration: 0
context:
  - '{project-root}/docs/conventions.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Every stage runs on its own; a run means pasting a chain of `python -c` calls, so neither Partner A's real-API pass nor the demo has one command, and nothing enforces the human pause before Synthesizer/CEO Move.

**Approach:** `orchestrator.py` at the repo root with two phases split at the human pause. `start`: corpus → hunters → Dedup → Fact Checker → Quant → Priced-In → scoring → Peer → scoring again, then stop. `finish`: write the human's Sharpest Needle + product to run.json, then Comparison Builder → Synthesizer → CEO Move → Report. Each phase is an async function (the UI calls these in 4c) and a CLI subcommand.

## Boundaries & Constraints

**Always:**
- Stage table in order, each entry = name + module path + required flag; stage call = `await module.run(run_dir)`. A module not on main (`fact_checker`, `quant`, `priced_in`, `peer`, `comparison`) is **skipped visibly**: logged warning + `skipped: not built` in the status file — never silent.
- Required stages (corpus, hunters, dedup, scoring, synthesizer, ceo_move, report): an exception marks it `failed` with the error and stops the phase (later stages would read missing files); the CLI exits 1. Optional stages (the five above): an exception marks `failed` and the run continues.
- Status sidecar `runs/<run_id>/pipeline.json` (not a contract file): ticker, phase (`awaiting_human` / `done` / `failed`), and per stage `{status, seconds, error}`. Timings feed §3.4's budget.
- `start TICKER [--corpus DIR]`: new `runs/<TICKER>-<YYYYMMDD-HHMM>` (librarian.run_id_for); ticker must be in librarian.CIKS. `--corpus` copies manifest.json, chunks.jsonl, facts.jsonl from a prebuilt dir (e.g. data/sample/NVO) instead of the live Librarian — offline dev and the demo fallback. At the pause it prints the selected findings (id, Recommended tag, lead claim) and the exact `finish` command.
- `finish RUN_DIR [--needle F-nnn] [--product NAME]`: refuses (exit 2, nothing written) if scoring has not completed, or if `--needle` is not a selected finding. Writes only the given keys into run.json, keeping existing keys. No `--needle` → CEO Move's existing Recommended fallback (labelled in the report). Re-running `finish` is allowed (human changes pick).
- Sequential stages; `logging`, no print except the CLI's pause/finish summary.

**Never:** UI, Comparison Builder itself, product pre-selection (no stage writes product hints yet), edits to stage modules or `docs/contracts.md`, real-API calls in tests, per-candidate pipelining (§3.4).

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected |
|---|---|---|
| Start happy | `--corpus` fixture, fake stages | corpus copied; stages in order; A's/missing modules skipped; phase awaiting_human; synthesizer not run |
| Required stage fails | dedup raises | dedup failed, later stages not run, phase failed |
| Optional stage fails | quant raises | quant failed, scoring still runs |
| Bad ticker | `XYZ` | exit 2, no run dir |
| Finish happy | after start, `--needle F-003 --product B` | run.json updated (keys kept); comparison skipped; synth → ceo → report run; phase done |
| Finish too early | no completed scoring | exit 2, nothing written |
| Bad needle | `--needle F-006` (not selected) | exit 2, run.json unchanged |

</frozen-after-approval>

## Code Map

- `pharmer/librarian.py` -- `run_id_for(ticker)`, `CIKS`, `async run(run_dir)` (needs PHARMER_SEC_UA, network).
- `pharmer/hunters/__init__.py`, `dedup.py`, `scoring.py`, `synthesizer.py`, `ceo_move.py`, `report.py` -- each `async run(run_dir)`; scoring is idempotent (re-run after Peer is by design).
- `pharmer/synthesizer.py` -- reuse `read_json_or_none`, `write_json`, `selected_findings`.
- `tests/fixtures/synthesis_findings.json` -- scored findings for finish tests (F-001..F-005 selected).

## Tasks & Acceptance

**Execution:**
- [x] `orchestrator.py` -- stage table, `start_run`, `finish_run`, `main(argv)` per Boundaries.
- [x] `tests/test_orchestrator.py` -- matrix rows with fake stage modules (stage table injectable), no network/API.
- [x] `docs/build-log.md` -- decision + test entries; the Step 1/2 handoff commands become `orchestrator.py start/finish`.

**Acceptance Criteria:**
- Given a finished run, then pipeline.json lists every stage of both phases with a status and seconds.
- Given `python orchestrator.py --help`/no args, then usage is printed and exit code is 2.

## Implementation Notes

- Implemented directly (user asked). The corpus is a stage like the others: `_run_stages` takes a module name or an async callable.
- Added after the first live check: hunters with zero leads and synthesis/ceo_move files with status failed mark the stage `degraded` (the hunter runner swallows a 401).

## Verification

**Commands:**
- `.venv/bin/python -m unittest discover tests` -- expected: all pass, offline.
- `.venv/bin/python orchestrator.py start NVO --corpus data/sample/NVO` without a key -- expected: corpus copied, hunters fail visibly (API error logged, no leads), run stops cleanly with a status, no traceback.

## Review Triage Log

| # | Source | Finding | Verdict | Route / evidence |
|---|---|---|---|---|
| 1 | edge + verification | `runs_dir=RUNS_DIR` bound at def time; CLI test writes the real runs/, fails twice a minute | high | patch: `runs_dir=None` resolved at call; tearDown asserts runs/ untouched; reproduced (runs/NVO-…-0641) |
| 2 | edge + blind | finish runs on a failed/running start | medium | patch: phase must be awaiting_human/done when pipeline.json exists |
| 3 | edge + blind | Ctrl-C/cancel leaves phase "running" | medium | patch: BaseException marks failed, saves, re-raises |
| 4 | edge | broken partner import escapes, phase stuck | medium | patch: load inside try → stage failed |
| 5 | blind | re-run finish leaves the old report.pdf | medium | patch: finish outputs cleared first |
| 6 | edge + blind | --corpus of another ticker accepted | medium | patch: manifest ticker check |
| 7 | edge | corpus entry is a directory | low | patch: is_file |
| 8 | edge + blind | exists-then-mkdir race → raw FileExistsError | low | patch: mkdir → UsageError |
| 9 | blind | UI cannot see the running stage; no timestamps | medium | patch: status running + started_at saved first |
| 10 | blind | non-atomic pipeline.json | low | patch: temp + os.replace |
| 11 | blind | librarian_report.json not copied | low | patch: copied when present |
| 12 | blind | pause message placeholders / wrong advice when none selected | low | patch: Recommended pre-filled; none → finish without needle |
| 13 | verification | pause summary, exit 1, ceo_move degraded, load_module re-raise, live corpus untested | medium | patch: tests added |
| 14 | blind | build-log says full run works; finish refuses a needle while nothing is selected | medium | patch: entry corrected (uncommitted) |
| 15 | blind | synth/ceo raising skips the report | low | rejected: they only raise on a missing findings.json, which finish already refuses |
| 16 | blind | stage skips/degradations not in the PDF | medium | defer: report would read pipeline.json |
| 17 | blind | zero leads still exits 0 | low | rejected: shown as degraded; selecting nothing is a legitimate outcome |
| 18 | edge | non-dict findings / missing finding_id | low | rejected: Contract 2 shape from our own stages |
| 19 | edge | re-run finish cannot clear a needle | low | rejected: analyst names the Recommended explicitly |
| 20 | edge | non-UsageError → traceback, not exit 1 | low | rejected: conventions let exceptions propagate; stage errors are already caught |
| 21 | edge | report returns without PDF yet ok | low | rejected: report raises on failure; not reachable |
| 22 | edge | spec says stage table injectable | false | tests inject via load_module; tables are plain constants |
| 23 | blind | []-findings counts as scored | low | rejected: finish then writes an explanatory report, which is the useful outcome |
