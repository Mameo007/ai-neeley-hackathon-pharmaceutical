---
title: 'UI step 5: friendly errors with one recovery button'
type: 'feature'
created: '2026-10-18'
status: 'done'
route: 'oneshot'
review_loop_iteration: 0
context: ['{project-root}/docs/ui-plan.md']
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Errors on screen are raw exception or orchestrator text ("NVO-… already exists; start again in a minute", "The background job crashed: OSError: …", "No report.pdf for this run (see the stage table)") with no way forward, so a judge watching a failure sees jargon and a dead end (docs/ui-plan.md, build step 5).

**Approach:** Every `st.error` gets one plain-English sentence and one button that recovers: Retry (start the company again, or re-run the report step with the same pick), Use prebuilt filings (when the SEC fetch failed and a prebuilt corpus exists), or Mark as failed (an interrupted run). Raw error text moves to the "Details" expander. The sentence/button choice is a pure helper tested without a page; the recovery actions reuse `prepare_run` / `prepare_finish` + `launch` under the existing launch lock. Inside the "Your pick" form, the form's own button is the retry.

</frozen-after-approval>

## Implementation Notes

- Branch stacked on feat/ui-result-screen (PR #35 open): step 5 edits the result screen's errors that step 4 introduced.
- `app.py` pure helpers: `usage_error_text` (orchestrator.UsageError text → one plain sentence, no CLI flags or paths), `recovery(phase, status, run_dir, ticker)` → {"text", "label", "action"}: done without report.pdf or failed after the pause → "Retry the report" (finish with the pick in run.json); corpus failed + prebuilt corpus → "Use prebuilt filings"; corpus failed otherwise or any other start failure → "Retry" (a new run of the same company); else None.
- Actions: `start_run` (prepare_run + launch under `_launch_lock`; a refusal is kept in `session_state["start_error"]` so the Retry button survives the rerun), `finish_again` (prepare_finish with no args keeps run.json's pick), `recovery_box` (top of the run page). Interrupted: plain sentence + "Mark as failed"; once failed, recovery_box offers the retry.
- Raw text moved out of errors: the background job's crash text is a caption in "Details"; stage errors were already there. Inside the "Your pick" form the submit button is the retry (Streamlit forms can't hold another button).
- Not clicked in a real browser: the recovery button would start a real run and call the API (B's key is rejected); AppTest covers each click with prepare_run/launch mocked.
- Tests: 51 app tests (usage_error_text cases, recovery decisions incl. Amgen without prebuilt, failed SEC fetch → prebuilt corpus, failed report → same pick relaunched, crash text only in Details, refused start keeps Retry across reruns). Full suite 496 OK; api_usage.csv untouched.
- Review patches: same-minute clash offers "Open that run" (Retry would collide again); stored refusal dropped on another company or any run page; `used_prebuilt` (run manifest byte-identical to the sample's) keeps a prebuilt run on prebuilt filings when retried; refusals logged; report-step refusals mapped ("never finished reading … Start the company again"); failed-report text points at the form below; safe pop in recovery_box. A test feeding the orchestrator's real messages caught that "needs a run whose `start` reached" has backticks — the hand-copied match missed it. 58 app tests; suite 503 OK.

## Review Triage Log

- medium (patched): Retry after "already exists" is certain to collide within the minute → "Open that run".
- medium (patched): a prebuilt run retried live (offline demo breaks) → `used_prebuilt` keeps prebuilt.
- medium (patched): stale start refusal persisted in the session → cleared on company change / run page (test).
- medium (patched): report-step refusals fell to "could not be started. Try again" → own sentence; real orchestrator messages now tested.
- low (patched): raw refusal text discarded with no log → logged. low (patched): fragile `pop` in recovery_box (test). low (patched): "Your pick is kept" beside a form that changes it → reworded.
- low (patched): test gaps — done without PDF, recover refused, interrupted shows one box.
- low (rejected): two retry paths on a failed report (recovery button + the pick form) — the form is the "change it" path, named in the sentence.
- low (rejected): substring matching on orchestrator messages vs error codes — codes would change orchestrator's API; real-message test guards the coupling.
- low (patched in commit): build-log entry.
