---
title: 'UI step 3: agent cards replace the stage table'
type: 'feature'
created: '2026-10-18'
status: 'done'
route: 'oneshot'
review_loop_iteration: 0
context: ['{project-root}/docs/ui-plan.md']
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** While a run works, the page shows a raw stage table with internal names (`scoring_after_peer`, `priced_in`), which a judge glancing at the screen cannot read (docs/ui-plan.md, hero screen 2 / build step 3).

**Approach:** Replace `stage_table` in `show_progress` with one card per agent: a single dict maps pipeline.json stage names to agent name + one-line job; one shared Hunters card lists the six hunters (pipeline.json has one `hunters` stage). Card state: waiting grey → working amber pulsing → done green check; `skipped` grey "not built"; `degraded`/`failed` red with a plain-English message. A card shows its latest discovery when the run's files hold one (counts read from the run's own files, never invented). The raw stage table moves into a "Details" expander. Logic in pure helpers above `main()`; mockup 2-agent-cards.png is layout only.

</frozen-after-approval>

## Implementation Notes

- Branch stacked on feat/ui-company-grid (PR #33 unmerged when step 3 started): both steps edit the same CSS block and tests, so branching from main would have guaranteed conflicts. PR targets main after #33 merges.
- `app.py`: `STAGE_AGENTS` (every pipeline.json stage → group, agent, job; order = run order; `scoring` + `scoring_after_peer` share the "Scoring" card), `HUNTER_NAMES` (display names for the six hunters), `CARD_STATES`, `CARD_PROBLEMS`. Pure helpers: `_card_status` (failed > degraded > running > waiting/skipped/ok; a card between its two stages counts as running), `_discovery` + `_n`, `agent_cards`, `progress_fraction`, `cards_html` (all values escaped). Render: `agents_section` (ticker chip + "<Company> · the agents are reading" / "what the agents did", gradient progress bar, card grid, "after your pick" strip until the finish phase starts). `show_progress` shows the cards with the raw table in a "Details" expander; non-running pages get "The agents" + "Details" expanders (open when failed/interrupted). `stage_warning` now lists agent names with plain sentences; raw errors only in Details.
- Discoveries are counts only (documents, leads per hunter, distinct findings, confidence mix, findings sized in dollars, findings selected) — the mockup's quoted discovery lines would need a "latest lead" the pipeline doesn't record while running; counts are always true.
- Agents added beyond the plan's list: Scoring, and the four after-your-pick stages, so a failure in any stage shows on a card.
- Screenshot needed a "running" page without a real job: a scratchpad wrapper patched `app.live_runs` to include a temporary fixture run (removed afterwards).
- Tests: 36 app tests (stage/hunter name coverage vs orchestrator + hunters.HUNTERS, card states, scoring merge, finish cards gating, discoveries vs fixture files, plural, HTML escaping, warning without raw error). Full suite 400 OK; api_usage.csv untouched.
- Review patches: `live=False` cards show "stopped" (red) for a stage left running and "not reached" (grey) for stages never started, except after-your-pick cards on a paused run (still "waiting"); Scoring shows ✓ between its two passes (no second pulsing card while Peer runs); progress bar counts all 12 cards (no backward jump at finish) and only ended steps (an interrupted run no longer shows a full bar — caught on the screenshot after the first patch); bad lead/finding rows skipped, missing hunter shown as "unknown"; 2-column grid under 1000px. 38 app tests; suite 402 OK.

## Review Triage Log

- medium (patched): interrupted run's cards kept "working" under "what the agents did" → "stopped".
- medium (patched): cards after a failed required stage stayed "waiting" on an ended run → "not reached".
- medium (patched): Scoring pulsed between passes, two cards working at once → ✓ after the first pass.
- low (patched): progress bar jumped 100% → 67% when finish started → fixed denominator of every card.
- low (patched): non-dict lead rows crash the page; missing hunter printed "None" → skipped / "unknown".
- low (patched): fixed 4-column grid at narrow widths → 2 columns under 1000px.
- low (patched): new view untested on a page → running-page test asserts agents_section is called and Details exists; pure tests for ended runs.
- low (rejected): hunters with zero leads not listed — the "who" line lists all six; the count line is about what was found.
- low (rejected): Fact Checker counts reasonings, others findings — confidence is per reasoning by contract.
- low (rejected): Priced-In / Peer / after-pick cards show no discovery — Priced-In and Peer are not built; after-pick results are on the result screen (step 4).
- low (rejected): ✓ without screen-reader text, pulse restarting on each poll, header repeating the run name, warning text derived from CARD_PROBLEMS, findings.json parsed per card per poll (tiny file, 2 s poll).
- low (patched in commit): build-log entry.
