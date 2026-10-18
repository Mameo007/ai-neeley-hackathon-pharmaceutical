---
title: 'Demo config: disable the unbuilt Priced-In Detector and Peer Agent'
type: 'chore'
created: '2026-10-18'
status: 'done'
baseline_commit: '4ab5d42dbffa879057c73a8d2c0d3a77fa8f309d'
route: 'dispatch'
review_loop_iteration: 0
context:
  - '{project-root}/docs/conventions.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Priced-In Detector (`pharmer/priced_in.py`) and Peer Agent (`pharmer/peer.py`) are not built. Every run lists them as "skipped / not built", the UI shows a yellow warning banner and two greyed agent cards, and the PDF says "Market view: not priced in" even though nobody checked market pricing. That last line comes from the Synthesizer's rule "unchecked = zero hits". For the presentation this is noisy and partly misleading.

**Approach:** Add one explicit config, `orchestrator.DISABLED_STAGES = {"priced_in": reason, "peer": reason}`. New runs leave those stages out of the schedule and record them under a top-level `"disabled"` key in `pipeline.json`. The UI hides disabled stages from the agent cards, the progress bar and the warning banner. The UI and PDF say the ranking is materiality-based and print "Market pricing not assessed." wherever no priced-in check ran.

## Boundaries & Constraints

**Always:**
- Keep every agent declaration: the `START_STAGES` entries, the `STAGE_AGENTS` texts, specs and deferred-work. Disabling is a filter at schedule time, so re-enabling means deleting one dict entry.
- Disabled stages are never imported or run, and get no `stages` entry in new runs. `pipeline.json` records them only as `"disabled": {name: reason}`, never as "ok".
- The Details stage table still shows every recorded stage with its real status. That covers old runs too, which keep their "skipped / not built" rows.
- `scoring_after_peer` keeps running: scoring is idempotent, and `prepare_finish` and the UI gate on it.
- "Market pricing not assessed" is decided from the data: none of the relevant selected reasonings has a non-null `priced_in.hits`. It is not decided from the config, so old runs and future runs where priced-in is enabled render correctly.
- Real problems keep their warnings: failed or degraded stages, Quant or Fact Checker not run, citations not found, a missing comparison.

**Never:** delete code, specs or deferred entries; change `docs/contracts.md`, the score formula or the selection logic; touch Partner A's modules (Librarian, Fact Checker, Quant); invent peer or priced-in data; make live API calls.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected |
|---|---|---|
| New start run | default config | stages: corpus…quant, scoring, scoring_after_peer; `disabled` lists priced_in, peer; neither module imported |
| Cards / bar | any run | no Priced-In or Peer card; progress counts only active cards |
| Banner | old run with priced_in/peer "skipped" | no yellow line for them; another skipped/degraded stage still warns |
| PDF thesis | verdict ok, cited reasonings all `priced_in` null | "Market pricing not assessed." and no "Market view: not priced in" |
| PDF thesis | some hits checked | "Market view: <words>." as today |
| PDF findings | no selected reasoning checked | note "Ranked by materiality (EPS impact); market pricing not assessed."; per-reasoning "market pricing not assessed" |
| UI scoring text | hits null | "How we scored it" says materiality-based, no obscurity claim; result chip "market pricing not assessed" |

</frozen-after-approval>

## Code Map

- `orchestrator.py` -- `START_STAGES` (keep it); add `DISABLED_STAGES` and filter it in `run_start_stages`; `prepare_run` writes `"disabled"`; update the docstring.
- `app.py` -- `STAGE_AGENTS` (keep it), `agent_cards` (skip disabled stages), `stage_problems` (exclude disabled stages), Scoring card job text, `why_line`, the `reasoning_block` caption, the results chip (around line 949).
- `pharmer/report.py` -- `build_model` thesis `priced_in` (line ~219), `_reasoning_model` "priced-in check not run", `render_pdf` "Market view" (line ~380) and the Findings heading.
- `pharmer/prompts/synthesizer.md` -- add one rule: when every cited reasoning's hits are null, the verdict sentence must not claim whether the finding is priced in. `priced_in_call` stays `not_priced_in`, so the code check is unchanged.
- Tests: `tests/test_orchestrator.py:85-110`, `tests/test_app.py:140-165, 291, 320-326`, `tests/test_report.py:69, 194-200, 355`.

## Tasks & Acceptance

**Execution:**
- [x] `orchestrator.py` -- DISABLED_STAGES + filter + pipeline.json key -- one explicit switch.
- [x] `app.py` -- hide disabled stages; materiality-based wording when hits are null.
- [x] `pharmer/report.py` -- `market_assessed` and `ranking_note` in the model; PDF wording.
- [x] `pharmer/prompts/synthesizer.md` -- the not-assessed rule.
- [x] tests -- update the expectations above and add matrix cases.
- [x] `docs/build-log.md` -- decision entry and test entry.

**Acceptance Criteria:**
- Given the full offline suite, when it is run, then every test passes.
- Given the fixture run regenerated offline, when the PDF text is searched, then it has no "Priced-In", "Peer Agent", "not built" or "Market view: not priced in", and it still has the findings, footnotes and CEO move.

## Verification

**Commands:**
- `.venv/bin/python -m unittest discover tests` -- expected: all pass (the API smoke test skips without a key).

## Implementation Notes

- Implemented directly (no implementation subagent, per the checkpoint). Review run inline across the adversarial, edge-case and verification-gap lenses.
- `scoring_after_peer` kept; `SCORING_JOB` in app.py picks the card text from `DISABLED_STAGES`.
- Old runs' `stages` entries for priced_in/peer stay as recorded; only the cards and the banner hide them.

## Review Triage Log

| # | Finding | Verdict | Route / evidence |
|---|---|---|---|
| 1 | With no selected reasoning sized by Quant (NVO has no Q2 EPS denominator, per the build log), the PDF note "Ranked by materiality" is false: `scoring.rank_key` then orders by confidence | medium | patch: the note is now data-driven (`CONFIDENCE_ONLY`) and tested |
| 2 | A synthesis.json written before the prompt rule (or a model ignoring it) can still claim "priced in" inside the verdict sentence, next to "Market pricing not assessed." | maybe-false (medium if true) | defer: depends on live model output; settle by reading synthesis.json after the demo run |
| 3 | Disabled stages missing from the CLI summary (`_stage_lines`) | false | the "disabled" key is in pipeline.json, and the CLI never listed skipped-by-design stages before either; nothing misleading is printed |
| 4 | `SCORING_JOB` is computed at import, so tests patching `DISABLED_STAGES` don't change it | low | rejected: only tests patch the dict; the demo edits the source |
