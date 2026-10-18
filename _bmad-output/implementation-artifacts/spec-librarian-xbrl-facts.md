---
title: 'Librarian: XBRL facts + US-GAAP/IFRS concept map'
type: 'feature'
created: '2026-10-18'
status: 'in-review'
route: 'dispatch'
review_loop_iteration: 0
baseline_commit: '9c5c49c61e514d6eff4b5993d1bffa43c65cd169'
context:
  - '{project-root}/docs/contracts.md'
  - '{project-root}/docs/conventions.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** `facts.jsonl` is empty, so the number-tie rule has no second source and
**every finding in the pipeline is capped at Medium confidence** — including the ones
Partner B's scoring already ranks. High confidence is unreachable today.

**Approach:** Populate Contract 1c from SEC XBRL companyfacts via a hand-built
US-GAAP↔IFRS concept map, emitting one fact per (metric, measurement period) with
values normalised to millions so they reproduce the filing table figure exactly.
IR Excel workbooks stay deferred.

## Boundaries & Constraints

**Always:**
- Write exactly the Contract 1c shape; `source_kind` is `xbrl`; `fact_id` is
  `<TICKER>-xbrl-<metric>-<period>` and `doc_id` is `<TICKER>-XBRL`.
- The concept map must cover the five keys Partner B's merged code already uses:
  `revenue`, `trade_receivables`, `inventory`, `operating_profit`, `net_profit`.
- `metric` is always a concept-map key, never a raw XBRL tag.
- **Values are normalised to millions** (`unit: "millions"`), because XBRL reports
  absolute amounts. Lilly's `Revenues` 22,974,000,000 must emit as `22974` — that
  is what makes it tie to the filing table's `Revenue $ 22,974`.
- Derive the measurement period from each fact's own `start`/`end` dates, **never**
  from the XBRL `fy`/`fp` fields, which describe the filing and mislabel the
  six-month row as `Q2`.
- A duration fact's span decides its label: ~3 months → `Q<n> <year>`, ~6 months →
  `H<n> <year>`, ~12 months → `FY<year>`. Both the 3-month and 6-month rows for the
  same `end` are emitted — they are different figures and a hunter may cite either.
- On restatements (several rows for one metric+period) keep the one with the latest
  `filed` date.
- A concept-map metric with no tag match for this company is recorded as
  `unmapped_metric` in the audit sidecar and simply produces no fact — never a
  guessed or derived value.
- Add one `manifest.json` entry for the XBRL source (`doc_type: "xbrl"`,
  `doc_id: <TICKER>-XBRL`) so every `facts.jsonl` row resolves to a manifest document.
- Standard library only.

**Never:**
- No IR Excel workbooks, no `filing_table` facts, no computed or derived metrics
  (no revenue minus costs), no FX conversion — that is Quant's job.
- Do not modify Partner B's files: `data/sample/**`, `scripts/build_sample_corpus.py`,
  `pharmer/hunters/**`, `pharmer/dedup.py`, `pharmer/scoring.py`, `pharmer/prompts/**`.
- Do not change `docs/contracts.md` (needs both partners).
- Do not change the existing chunking or table-text format — the whitespace question
  is open with Partner B on PR #15 and is out of scope here.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| Domestic quarterly | `LLY` revenue, 90-day row ending 2026-06-30 | one fact, `Q2 2026`, value `22974`, unit millions, currency USD | N/A |
| Same end, two spans | `LLY` revenue 90-day and 180-day both ending 2026-06-30 | two facts: `Q2 2026` = 22974 and `H1 2026` = 42773 | N/A |
| Instant / balance item | `LLY` receivables, no `start`, end 2026-06-30 | one fact `Q2 2026`; a 12-31 instant becomes `FY<year>` | N/A |
| Foreign filer, IFRS | `NVO` — `ifrs-full` tags, DKK, annual only | annual facts only, currency DKK; no quarterly facts invented | N/A |
| Unmapped metric | `LLY` `operating_profit` — no `OperatingIncomeLoss` tag exists | no fact; `unmapped_metric` recorded in the sidecar | Log, never guess |
| Restatement | two rows, same metric+period, different `filed` | the latest-`filed` row wins | N/A |
| Odd duration | a 45-day or 270-day span | skipped, counted in the sidecar | Skip quietly, count it |
| No XBRL at all | companyfacts 404s | run continues, `facts.jsonl` stays empty, coverage warning | Warn, never fail the run |
| Unknown unit | a tag reporting in `shares` or `USD/shares` | skipped; only plain 3-letter currency units are used | N/A |

</frozen-after-approval>

## Code Map

- `pharmer/librarian.py` — extend, do not restructure. `build_corpus()` currently
  writes an empty `facts.jsonl` in `_write_corpus()`; `select_documents()` builds
  manifest entries; `_sec_json()` is the rate-limited fetch chokepoint to reuse;
  `_fiscal_period()`/`_doc_period()` already produce `FY2025` / `Q2 2026` / `2026Q2`
  and must be reused so document and fact labels agree. `CIKS` holds ticker→(cik, name).
- `data/sample/NVO/facts.jsonl` — **read-only reference** for the exact field shape
  and the five metric keys in use. Partner B's file; do not edit.
- `docs/contracts.md` §1c — binding shape. Example values illustrative.
- `tests/test_librarian.py` — extend. `FakeSec` serves a url→content map over
  `_sec_get`; add companyfacts JSON to it rather than inventing a new fake.
- Verified live, so do not re-investigate: Lilly exposes `Revenues`,
  `AccountsReceivableNetCurrent`, `InventoryNet`, `NetIncomeLoss` with quarterly rows
  but **no operating-income tag at all**; Novo exposes all five as `ifrs-full` in DKK,
  annual only (0 quarterly rows).

## Tasks & Acceptance

**Execution:**
- [x] `pharmer/librarian.py` — add `CONCEPT_MAP`: metric key → ordered candidate tags
  across `us-gaap` and `ifrs-full`, covering at least B's five keys. Ordered so the
  preferred tag wins when a filer reports several.
- [x] `pharmer/librarian.py` — add `_period_from_fact()`: span/instant → `FY2025`,
  `Q2 2026`, `H1 2026`, or `None` for an odd span, deriving from `start`/`end` only.
- [x] `pharmer/librarian.py` — add `_xbrl_facts(ticker)`: fetch companyfacts, resolve
  each metric through `CONCEPT_MAP`, normalise to millions, dedup on latest `filed`,
  return `(facts, misses, skipped)`.
- [x] `pharmer/librarian.py` — wire into `build_corpus()`: write real rows to
  `facts.jsonl`, add the `<TICKER>-XBRL` manifest entry, and record `unmapped_metric`
  misses plus a fact count per metric in `librarian_report.json`. Replace the
  "facts.jsonl is empty" warning with one that fires only when there are no facts.
- [x] `tests/test_librarian.py` — cover every matrix row offline with a companyfacts
  fixture served through `FakeSec`; assert the millions normalisation reproduces
  `22974` and that the 90-day and 180-day rows become `Q2 2026` and `H1 2026`.
- [x] `tests/test_librarian.py` — extend the Partner B compatibility test: every
  `facts.jsonl` row's `doc_id` resolves in `manifest.json`, and the five metric keys
  B uses are present for a domestic filer.
- [x] `docs/build-log.md` — append the decisions and any confidently-wrong case.

**Acceptance Criteria:**
- Given `LLY`, when the stage runs, then `facts.jsonl` is non-empty, every row has
  `source_kind: "xbrl"` and `unit: "millions"`, and revenue for `Q2 2026` is `22974`
  — the figure printed in the filing table.
- Given `NVO`, when the stage runs, then facts are annual and in DKK, and no quarterly
  fact is emitted for a metric XBRL reports annually only.
- Given any run, when `librarian_report.json` is read, then every concept-map metric
  with no tag match for that company is listed as an `unmapped_metric`.
- Given a completed run, when `facts.jsonl` is read, then every `doc_id` resolves to a
  `manifest.json` entry and every `fact_id` is unique.
- `.venv/bin/python -m unittest discover tests` passes with no network access.

## Implementation Notes

**Files:** `pharmer/librarian.py` (+`CONCEPT_MAP`, `_period_from_fact`,
`_fact_period_key`, `_xbrl_facts`, `_xbrl_manifest_entry`; `_write_corpus` and
`build_corpus` now carry facts), `tests/test_librarian.py` (+17 tests, 55 total),
`docs/build-log.md`, `deferred-work.md`.

**Live results:** LLY 737 facts (`operating_profit` and `gross_profit` unmapped —
Lilly tags no operating-income concept at all), NVO 121 facts, DKK, annual only with
the quarterly-gap warning raised. 138 offline tests pass, 4 skipped.

**The number-tie, hand-checked on 24 facts:** NVO 8/8 tie to the printed filing table;
LLY 12/16 tie within the same-period document and **the other 4 tie in a comparative
column of a newer filing**. Two assumptions in this spec turned out to be wrong, both
caught here:

1. *"A fact ties against the filing that reports its period."* False — financial
   statements print the prior period alongside, so LLY FY2024 revenue is found on
   `LLY-10K-FY2025` p.43, not in the FY2024 10-K.
2. *"They must match digit for digit."* False, and the Verification section below is
   corrected. Lilly's FY2024 10-K prints `45,042.7`; its FY2025 10-K prints the same
   year as `45,043`; XBRL's latest-filed row is `45,043`. All three are correct — they
   differ in precision because the restatement rounded. **The Fact Checker must compare
   numerically with a rounding tolerance and must search comparative columns.** String
   equality would reject a true tie.

**Decisions:** one reporting currency per metric keeps `fact_id` unique (Contract 1c has
no currency segment); "latest filed wins" on restatements, which means a fact carries the
newest presentation — keeping both the original and restated value needs a contract change
and is in `deferred-work.md`; missing XBRL is caught per-company so the filings corpus
survives a companyfacts outage.

## Verification

**Commands:**
- `.venv/bin/python -m unittest discover tests` -- expected: all pass, offline
- `.venv/bin/python -m pharmer.librarian LLY` -- expected: facts.jsonl with quarterly
  and annual rows; `operating_profit` listed as an unmapped metric
- `.venv/bin/python -m pharmer.librarian NVO` -- expected: annual DKK facts, five
  metrics mapped, no invented quarterly facts

**Manual check:**
- Compare emitted facts against the figures printed in the filing table chunks. Match
  **numerically, allowing for rounding**, and search the whole corpus rather than only
  the same-period document — a restated figure is printed to coarser precision and a
  prior period appears in the newer filing's comparative column. (This replaces an
  earlier "digit for digit, same document" instruction that the first run disproved.)
