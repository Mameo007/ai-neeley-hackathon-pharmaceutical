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
- 2026-10-18 [A] decision: Librarian built as the EDGAR-filings slice first (manifest.json + chunks.jsonl for the subject company's 10-K/20-F/10-Q/6-K); facts.jsonl, DailyMed/Orange/Purple Book, rival filings and transcripts deferred to follow-up specs so Partner B's hunters are unblocked sooner.
- 2026-10-18 [A] decision: Librarian makes no Claude call at all — it is deterministic fetch/parse/index. pharmer.llm is imported lazily so `anthropic` is not an import-time dependency of a stage that never calls the API.
- 2026-10-18 [A] decision: financial 6-K selected by weighted content markers (threshold 8), never by filename, accession prefix, or size. Novo's 4,389KB remuneration report scored 1-2 while its 2,784KB Q2 financial report scored 17; size alone would have picked the wrong document.
- 2026-10-18 [A] decision: page attribution distinguishes the document's own printed page number from an inferred ordinal. Contract 1 is unchanged; the distinction, the 6-K candidate scores, and coverage warnings go in a non-contract sidecar runs/<run_id>/librarian_report.json that Partner B may ignore.
- 2026-10-18 [A] decision: table chunks join cells with tabs and rows with newlines, repeat header rows when a table splits, and keep cell text verbatim — parenthesised negatives stay `(1,234)` so the Fact Checker can string-match hunter quotes against chunk text.
- 2026-10-18 [A] decision: PHARMER_SEC_UA (.env) is required and the Librarian refuses to fetch without it — SEC fair access needs a real contact, and a blocked IP would kill the live demo.
- 2026-10-18 [Claude] wrong: concluded from Lilly's 10-Q that a filing's printed page number is the last line of each page-break section, and generalised it. Novo's 6-K puts it in a running header ("Page 3 of 37"), so the first version reported 0 verified pages for every Novo quarterly. Caught by running the second filer instead of trusting the first; both positions are now checked.
- 2026-10-18 [Claude] wrong: assumed every EDGAR filing carries page-break markup. Novo's FY2025 annual-report exhibit (nvo-20251231_d2.htm, 10.6MB) has no <hr> and no page-break CSS at all, so all 659 of its chunks were cited as "p.1". Caught in the real NVO run; unpaginated sections are now subdivided into synthetic pages and flagged "inferred-only".
- 2026-10-18 [Claude] wrong: first ambiguity check compared the top two 6-K scores, which fires on every foreign run because the prior-year report scores as highly as the current one. Caught while writing the test; ambiguity is now scoped per fiscal period.
- 2026-10-18 [A] test: 42 offline tests pass (.venv/bin/python -m unittest discover tests), no network. Live runs: LLY 4 docs/1,168 chunks/2.5s (299 verified printed pages), NVO 4 docs/1,895 chunks/4.5s incl. the incorporation-by-reference exhibit. Two LLY runs produced an identical chunk_id->text mapping; 14 hand-sampled prose citations all re-derived to the page they claim.
- 2026-10-18 [A] wrong (own verification script, not the pipeline): three sampled citations looked mis-paged. Causes were str.find returning an earlier duplicate of repeated boilerplate and a loose `page \d+` regex matching cross-references like "See Annual review, page 15". The corpus was correct in all 14 cases — a reminder that the checker needs checking too.
