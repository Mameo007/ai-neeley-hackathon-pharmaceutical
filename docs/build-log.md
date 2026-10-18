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
- 2026-10-18 [B] decision: baseline demo = run the pipeline live on a laptop (screen share); no hosted deployment needed.
- 2026-10-18 [B] decision: agents are Claude API calls orchestrated by one Python process (asyncio for parallel hunters); Claude API + tool use chosen over Managed Agents and the Claude Agent SDK as least new code for a 2-hour build.
- 2026-10-18 [B] decision: Claude API usage is on someone else's account; each partner keeps the key in a git-ignored .env, never committed or pasted into chat. Preferred: owner adds both partners to a workspace with a spend limit and each creates their own key.
- 2026-10-18 [B] decision: prompt caching on every call; shared prefix (rules + tools) byte-identical across hunters; warm the cache with one call before fanning out to 8 hunters.
- 2026-10-18 [B] decision: every API call logged per request to logs/api_usage.csv (Contract 3) via shared pharmer/llm.py; models: Opus 5.5 for hunters/Synthesizer/CEO Move, Haiku 5.5 for mechanical stages; server-side refusal fallback on Opus calls.
- 2026-10-18 [Claude] test: pharmer/llm.py offline tests pass (4/4, fake client, no API calls); not yet run against the real API.
- 2026-10-18 [B] decision: coding conventions in docs/conventions.md — snake_case code/JSON/agent names, kebab-case branches and log stage, one module per stage with async run(run_dir), prompts as pharmer/prompts/<agent>.md (no run-varying values, for caching), offline-only tests. PHARMER_PARTNER (.env) tags each API usage row with A/B.
- 2026-10-18 [B] decision: Partner B builds against a frozen sample corpus in data/sample/NVO/ (Contract 1 shape) made by scripts/build_sample_corpus.py from real SEC documents only — Novo 6-K Q2 2026, 6-K Q2 2025 and XBRL companyfacts (FY2023–FY2025). Lean scope: 20-F annual-report exhibits deferred (no page breaks, so printed page numbers are costly). `page` = the filing's printed "Page N of M"; split tables repeat their header rows in every chunk. A dev fixture until Partner A's Librarian replaces it; A to confirm the metric keys and the page rule.
- 2026-10-18 [B] test: sample corpus offline tests pass (tests/test_sample_corpus.py + tests/test_build_sample_corpus.py, 20 tests, no network); XBRL trade_receivables FY2024 71,949 / FY2025 70,856 tie to the 6-K balance-sheet comparative columns; two --from-cache rebuilds give byte-identical files.
- 2026-10-18 [B] decision: hunter harness (pharmer/hunters/harness.py) — system prompt is only prompts/hunter_rules.md (explicit cache breakpoint) and the tool list (search_chunks, read_chunks, get_facts, submit_leads) is one constant, so all 8 hunters share a byte-identical cached prefix; each hunter's brief + corpus overview go in the first user message (stricter than conventions.md, which put the brief in the system prompt). Leads are handed in through a submit_leads tool that rejects unknown chunk_ids and non-verbatim quotes, so the model fixes its own citations before anything is recorded. Turn cap 15. Runner writes runs/<run_id>/leads.json (Contract 2 Findings, one reasoning each, F-001… in hunter order) as Dedup's input.
- 2026-10-18 [Claude] test: hunter harness + Channel Checker offline tests pass (tests/test_hunters.py, 15 tests, fake client on a temp copy of data/sample/NVO; full suite 37/37); not yet run against the real API.
- 2026-10-18 [Claude] wrong: first-pass hunter prompts told the model to anchor balance-sheet leads on `H1 2026`, but the corpus labels those 6-Ks `Q2 2026` — Dedup groups on fiscal_period, so the same fact would split across hunters. The brief's example (+25% receivables vs +3% sales) also mirrored the real Novo story, priming the answer. Caught by the three-reviewer code review; prompts now say "use the cited document's fiscal_period" with a neutral example.
- 2026-10-18 [B] decision: review hardening of the hunter harness — submit_leads also requires a quote containing anchor.value (ties the Dedup key to evidence) and a boolean is_label_finding; one failing hunter logs and contributes no leads instead of aborting the swarm; the runner leaves lead_reasoning_id null (Contract 2 gives it to Dedup); the 15-turn budget is stated in hunter_rules.md.
- 2026-10-18 [Claude] test: after review fixes, tests/test_hunters.py has 17 tests (the earlier "15 tests" entry was wrong: it was 13), full suite 41/41, offline.
