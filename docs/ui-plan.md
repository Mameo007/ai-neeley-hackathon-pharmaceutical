# UI plan: make Pharmer user-friendly

Partner B's area (`app.py`). Came out of a 2026-10-18 brainstorm (see
`docs/build-log.md`). Goal: score on the hackathon's "user-friendly UI"
criterion.

## Who we design for

A Neeley judge who sees the app only briefly and never uses it. Every
screen must make sense **at a glance, with no narration**: plain English,
one main thing per screen, no internal names (`scoring_after_peer`,
`corpus`, `priced_in_hits`).

## The three hero screens

Everything else waits until these three are done.

1. **Pick a company.** A grid of the 10 shortlist companies as dark tiles
   (ticker, name, the case's starter thread). Click a tile, then
   "Find the needle in <company>". This shows the build works on any
   company.
2. **The agents at work.** Replaces the stage table. One card per agent
   (Librarian, the hunters, Dedup, Fact Checker, Quant, Priced-In, Peer)
   with a one-line job description. The card turns from grey (waiting) to
   amber and pulsing (working) to a green check (done). The card's
   latest discovery is shown when there is one.
3. **The result.** The Sharpest Needle as one plain-English headline,
   its impact as one big number, a one-line verdict, the verbatim quote
   with its source, and the CEO decision card beside it.

## Scope (MoSCoW)

- **Must:** dark "analyst terminal" theme (amber/green accents, monospace
  numbers); runs sidebar removed from the main view; agent cards; result
  screen with needle headline + big impact number.
- **Should:** CEO decision card; count-up animation on the big number;
  quotes shown as clean serif blockquotes (no escape backslashes);
  the score formula caption replaced by a one-line verdict, with the math
  behind a "How we scored it" expander; friendly errors with one recovery
  button.
- **Could:** before/after redline for language-drift findings (added words
  green, removed words red and struck through); product-vs-rivals table on
  screen; always-visible findings scoreboard in the header.
- **Won't (this time):** ranked findings leaderboard; replay mode;
  10-company switcher on the result screen.

## Build order (one branch each)

1. `feat/ui-dark-theme`: `.streamlit/config.toml` dark theme (the repo has
   no `.streamlit/` folder yet) plus a little CSS. Move the run picker out
   of the sidebar into a small "Past runs" expander; keep `?run=<name>`.
2. `feat/ui-company-grid`: replace `new_run_form` with the tile grid.
   Names come from `librarian.CIKS`. Starter threads go in a data file
   copied from the case appendix, with a test that each one appears in
   `docs/neeley_ai_forward_case_study.md` (same rule as `products.json`:
   no model memory). A tile with a prebuilt corpus shows a "filings ready"
   badge. The prebuilt-corpus toggle stays, below the grid.
3. `feat/ui-agent-cards`: replace `stage_table` in `show_progress` with
   the cards. Map stage names to agent names and jobs in one dict.
   `pipeline.json` has one `hunters` stage, so the hunters share one card
   listing their names (Redliner, Channel Checker, Footnote Miner,
   Gross-to-Net, Table Archaeologist, Label Lawyer). `skipped` is grey
   "not built", and `degraded`/`failed` is red with a plain-English
   message. The raw stage table moves into a "Details" expander.
4. `feat/ui-result-screen`: rewrite `results_section` as the hero
   result. Headline = the needle's claim. The big number is
   `impact.usd_m` / `impact.eps_pct` from `findings.json` (Quant). When
   Quant has not filled them, use the finding's anchor value instead and
   never make up a number. The quote and source chip come from
   `report.build_model`. The CEO card shows what `ceo_move.json` holds
   (`action`, `timeframe`, `trade_off`, `downside_if_wrong`,
   `why_it_works`). Budget and rejected alternatives are not in that
   file. Adding them changes CEO Move's output, so it needs both
   partners first.
5. `feat/ui-friendly-errors`: every `st.error` gets a plain sentence and
   one button (Retry / Use prebuilt filings / Mark as failed).

Then the Should/Could items, in the order above. The "Your pick" step
stays as it is, restyled by the theme only.

## Dependency

Partner A records the live run of the app. The UI branches must be merged
to `main` before that recording.
