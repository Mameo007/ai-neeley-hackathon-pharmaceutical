# Build Log

Append-only, newest at the bottom. One line per entry:
`- YYYY-MM-DD HH:MM [A|B|Claude] <type>: <what happened>`
Types: `decision`, `wrong` (AI confidently wrong + how it was caught), `test`, `blocker`.

The pitch deck and How-we-did-it report are built from this file.

- 2026-10-17 [Claude] wrong: web search summary said Novo Q2 adjusted sales +7% CER; the 6-K table showed reported +3% CER. Caught by opening the primary filing.
- 2026-10-17 [Claude] wrong: proposed openFDA as a backup label source; it was missing Novo's own Ozempic injection label. Caught by comparing against DailyMed.
- 2026-10-17 [Claude] wrong: source probe matched "Roche" to "Sinopec Shanghai Petrochemical" (substring of PETROCHEMICAL). Caught by reading the probe output.
- 2026-10-17 [Claude] wrong: probe reported Pfizer/J&J prior-year quarters missing; actually 52/53-week fiscal quarters (2026-06-28 vs 2025-06-29). Caught by listing their 10-Q dates.
- 2026-10-18 [Claude] decision: two data contracts (corpus, finding) defined in docs/contracts.md; repo files are the shared memory between both partners' Claude sessions.
- 2026-10-18 [Claude] wrong: contracts.md example labeled DKK 71,949m receivables as FY2025; it is 31 Dec 2024 (FY2024). Caught on self-review before commit.
- 2026-10-18 [B] decision: no direct commits to main; branch per change named <type>/<area>-<short-description>, merged via PR (rules in CLAUDE.md).
- 2026-10-18 [B] decision: coding conventions in docs/conventions.md — snake_case code/JSON/agent names, kebab-case branches and log stage, one module per stage with async run(run_dir), prompts as pharmer/prompts/<agent>.md (no run-varying values, for caching), offline-only tests. PHARMER_PARTNER (.env) tags each API usage row with A/B.
