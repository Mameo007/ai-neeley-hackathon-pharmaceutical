---
title: 'UI step 1: dark analyst-terminal theme, runs picker out of the sidebar'
type: 'feature'
created: '2026-10-18'
status: 'done'
route: 'oneshot'
review_loop_iteration: 0
context: ['{project-root}/docs/ui-plan.md']
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** The Streamlit app uses the default light theme and a sidebar run picker, so it does not read as the "analyst terminal" the UI plan (docs/ui-plan.md, build order step 1) asks for, and the sidebar competes with the main screen.

**Approach:** Add `.streamlit/config.toml` with a dark theme (bg #0a0d11, panels #11161d, amber #ffb020 primary, green #35d07f accent, monospace numbers) plus a small injected CSS block (amber monospace masthead, header bar look, mono numbers). Replace the sidebar with a small "Past runs" expander on the main page; `?run=<name>`, the background-thread launch and the "Your pick" form keep working unchanged (restyle only). Reference look: /Users/kanta/Desktop/pharmer-ui-mockups (layout only; not copied into the repo).

</frozen-after-approval>

## Implementation Notes

- `.streamlit/config.toml` (new): dark base, mockup palette (bg #0a0d11, widgets #161c25, borders #232b37, amber primary/orange, green #35d07f, red #ff5d5d), mono codeFont, toolbar minimal.
- `app.py`: `CSS` constant injected with `st.html` (header bar, amber mono h1, 4.5rem top padding, panel bg for expanders and finding cards, dark text on amber primary buttons, tabular/mono metric numbers for step 4). `sidebar()` → `past_runs()`: same selectbox/`?run=`/pending_run logic inside a "Past runs (n)" expander; `initial_sidebar_state="collapsed"`.
- Masthead kept as "PHARMER RESEARCH" (not the mockup's "PHARMER"): it is the PDF masthead in architecture.md/report.py, so screen and report match.
- Finding cards get `key=f"card-{finding_id}"` because Streamlit 1.66 bordered containers have only hashed emotion classes; `st-key-card-*` is the stable CSS hook.
- Surprise: the running server kept the old `app` module (app.py re-imports itself as `app`), so CSS edits only showed after a server restart.
- Tests: ThemeTests (config values), sidebar empty + "Past runs (0)" expander, `?run=` opens a run via query params, running page with a live job shows progress. 23 app tests; full suite 387 OK; api_usage.csv untouched.
- Screenshots (headless Chrome over DevTools, scratchpad): new-run page with Past runs open, paused review + Your pick form, done run.
- Review patches: visible "➕ New run" button on an open run (+ AppTest), `blueColor` #4ea1ff from the mockup palette, padding comment, dropped the no-op `initial_sidebar_state`.

## Review Triage Log

- medium (patched): "New run" only reachable inside the collapsed Past runs expander — a judge can't find the way back. Added a visible button.
- low (patched): blueColor/violetColor unset so running badges / info boxes use Streamlit defaults — set blueColor from mockup.css; violet left (labels only).
- low (patched): 4.5rem padding uncommented — commented. low (patched): `initial_sidebar_state` no-op — removed.
- low (rejected): CSS hex values duplicated from config.toml — 4 values, one file each; drift is visible on sight.
- low (rejected): orangeColor = amber makes "Medium"/"degraded" badges match the brand — mockup uses amber pills for in-between states (`.pill.a`); stage badges are replaced by agent cards in step 3 with red for degraded.
- low (rejected): running stage badge still blue — step 3 (agent cards) replaces the stage table; out of this step.
- low (rejected): CSS ignores a viewer switching to Light — toolbar minimal, demo is screen-shared by us.
- low (rejected): CSS depends on Streamlit data-testids — inherent to styling Streamlit; screenshots per step catch breakage; pinning is a dependency change.
- false: global h1 rule could restyle model text with `#` headings — `md()` escapes `#`, so model text can't render as h1.
- low (rejected): card key collision — finding_id is unique per findings.json (contracts.md); the radio would already break on duplicates.
- low (rejected): "Past runs (N)" counts beyond the 15 captions — the selectbox still lists all N.
- low (rejected): extra theme/CSS test gaps — CSS is verified by screenshot, not unit test.
- low (patched in commit): no build-log entry. rejected: marking docs/ui-plan.md done — the PR/build log record progress.
