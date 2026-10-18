---
title: 'Fact Checker: evidence re-verification, number-tie, confidence'
type: 'feature'
created: '2026-10-18'
status: 'done'
route: 'dispatch'
review_loop_iteration: 0
baseline_commit: 'b529b8038d8a7dab7ae6631b418f12e94882736a'
context:
  - '{project-root}/docs/contracts.md'
  - '{project-root}/docs/conventions.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** `confidence` arrives from Dedup as `null` on every reasoning, and Partner B's
scoring gate drops anything not High/Medium — so today **no finding can pass the gate**.
Nothing re-opens a cited chunk, nothing ties a number to a second artifact, and nothing
checks that the evidence actually supports the hunter's claim.

**Approach:** A deterministic verification stage that re-opens each citation, re-matches the
quote, ties the anchor value to `facts.jsonl` at source-supported precision, searches the
corpus (including comparative columns) for corroboration and the trend strip, and assigns
`confidence` from the checks that passed. One Haiku call per reasoning judges only the
genuine interpretation question — does this evidence support this claim — and can never
upgrade a failed deterministic check.

## Boundaries & Constraints

**Always:**
- Write **only** the Fact Checker's Contract 2 fields: `reasonings[].confidence`,
  `confidence_reason`, `trend`, and the flags `reversal` / `possibly_seasonal`. Preserve
  every other field byte-for-byte, including flags written by Dedup (`contested`).
- **Quote verification:** a citation passes `exact` when `quote` is a substring of the
  chunk's `text`. For `is_table` chunks only, fall back to a **whitespace-normalized**
  comparison (runs of spaces/tabs/newlines → one space, on both sides) and record the
  match as `normalized`. This is the explicit fix for the known corpus incompatibility:
  Partner B's sample joins table cells with spaces, the Librarian with tabs, so the same
  table yields different `text` for the same `chunk_id` (build-log blocker, 2026-10-18).
- **Number-tie precision:** compare numerically, never by string equality. Tolerance is
  derived from the sources themselves — `0.5 * 10 ** -min(dp_a, dp_b)`, where `dp` is each
  value's own decimal places. Never a broad percentage tolerance.
- **Comparative-column search:** the corroborating value may sit in a comparative column of
  a *different* document. Search every table chunk in the corpus, and attribute a hit to a
  period by its column position in the table header — not by mere presence in the chunk.
- Before declaring a tie, require agreement on **fiscal period, unit, currency, metric
  concept, and financial context** (same statement line, not a segment/product subtotal).
  Any disagreement is a mismatch, never a tie.
- **Confidence (Contract 2, `High` | `Medium` | `Low`):** `High` only when *all* checks
  pass — quote matched, anchor value present in the cited evidence, number-tie reproduced
  against a second artifact, like-for-like confirmed, claim support confirmed. `Medium`
  when the evidence is located and the value is present but a check is **unconfirmable**
  (no second source, like-for-like unavailable, claim support unverified, normalized-only
  quote match). `Low` when evidence is **unsupported or contradicted** — chunk missing,
  quote absent, anchor value absent, tie contradicted, period/unit/currency/metric
  mismatch, or claim support contradicted. Every verdict carries a `confidence_reason`
  naming the checks behind it.
- **Deterministic validation is authoritative.** The LLM judges claim support and
  ambiguous row-label equivalence only; it may lower a verdict, never raise one. A refused,
  malformed, or failed reply leaves claim support *unverified* → cap at Medium, never High,
  and never raises.
- `trend` = the same metric at every period in the corpus, chronological, Contract 2 shape
  (`{fiscal_period, value}`), facts preferred over table hits when both exist.
  `reversal` = the direction of change flips across the strip; `possibly_seasonal` = the
  anchor's period has no prior-year same-period comparator in the corpus.
- Stage entry point `async def run(run_dir: Path) -> None` reading and rewriting
  `findings.json`; `llm.HAIKU` through `pharmer.llm`; standard library otherwise.
- Per-check detail goes in a non-contract sidecar `runs/<run_id>/fact_check_report.json`,
  following the `librarian_report.json` precedent. Partner B may ignore it.

**Never:**
- No web search, no model memory, no re-discovery — this stage re-verifies what was cited.
- Do not write `impact`, `priced_in`, `score`, `peer`, `status`, `recommended`, `anchor`,
  `claim`, `citations`, `hunter(s)`, `is_label_finding`, `contested`, `lead_reasoning_id`.
- Do not modify Partner B's files (`data/sample/**`, `scripts/build_sample_corpus.py`,
  `pharmer/hunters/**`, `dedup.py`, `scoring.py`, `synthesizer.py`, `ceo_move.py`,
  `pharmer/prompts/` other than the new `fact_checker.md`).
- Do not change `docs/contracts.md`, and do not drop a finding — Low is marked, and
  Partner B's scoring gate decides what to exclude.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| Rounded restatement ties | anchor FY2024 revenue `45042.7` (LLY FY2024 10-K p.41, 1 dp); XBRL fact `45043` (0 dp) | tie reproduces: `min(dp)=0` → tolerance `0.5`, diff `0.3` → High-eligible | N/A |
| Comparative column | same FY2024 anchor; `45,043` printed in the FY2025 10-K p.43 comparative column | corroborating hit recorded against `FY2024` by header column position | N/A |
| Wrong column rejected | `Revenue $ 65,179 $ 45,043`, anchor period `FY2024` | `65,179` is the FY2025 column → not a hit for FY2024 | N/A |
| Tabs vs spaces | quote `"Revenue\t$\t45,042.7"`, chunk text space-joined | `normalized` match, logged; High still reachable | N/A |
| No second source | NVO `Q2 2026` receivables `88949`; XBRL is annual-only | no tie available → **Medium**, reason names the missing second source | N/A |
| Like-for-like confirmed | NVO `Q2 2026` `88949` vs `Q2 2025` `69575` in the prior-year 6-K | no `possibly_seasonal`; High reachable | N/A |
| Reversal | trend FY2024 `71949` → FY2025 `70856` → Q2 2026 `88949` | `reversal` flag set, trend attached | N/A |
| Mismatched period | anchor `FY2025`, only tie candidate is `FY2024` | not a tie; period mismatch → **Low** | N/A |
| Missing evidence | `chunk_id` absent from `chunks.jsonl`, or quote not in text | **Low**, reason names the unlocatable citation | Log, never raise |
| Contradicted number | anchor `45142.0` vs fact `45043` (diff ≫ tolerance) | **Low**, contradiction recorded | N/A |
| Claim unsupported | value verifies, LLM says the quote does not support the causal claim | **Low**, reason names the interpretation failure | N/A |
| LLM unavailable | refusal / malformed / API error | claim support `unverified` → cap **Medium** | Log, never raise |
| No facts.jsonl | file missing or empty | every reasoning caps at Medium; stage completes | Warn, never raise |

</frozen-after-approval>

## Code Map

- `pharmer/fact_checker.py` — **new**. The whole stage. Entry `run(run_dir)`.
- `pharmer/prompts/fact_checker.md` — **new**. System prompt, claim-support judgment only.
  No dates/run ids (cached prefix rule, `docs/conventions.md`).
- `pharmer/llm.py` — reuse `call()` with `model=llm.HAIKU`, `stage="fact-checker"`,
  `agent="fact_checker"`. Structured output via `output_config`, as `pharmer/dedup.py`
  (`parse_verdict`) and `pharmer/synthesizer.py` already do; copy their failure handling.
- `pharmer/dedup.py` — produces the input. `build_finding()` writes the **normalized**
  `anchor.metric` (snake_case) and `fiscal_period` (`Q2 2026`), so they already align with
  `facts.jsonl` labels. `merge_reasonings()` unions citations — a reasoning can carry
  citations from several hunters and several documents.
- `pharmer/librarian.py` — read-only reference, do **not** restructure: `CONCEPT_MAP`
  (metric key → XBRL tags) for metric-concept checks, `_plain_text()` (tab/newline table
  format, `(1,234)` kept verbatim), `_period_from_fact()`/`_fiscal_period()` for the period
  label vocabulary (`FY2025`, `Q2 2026`, `H1 2026`).
- `docs/contracts.md` §1b/§1c/Contract 2 — binding shapes; example values illustrative.
- `pharmer/scoring.py` — read-only consumer: Low/null ⇒ score null ⇒ finding `dropped`.
- Real evidence, already verified live — do not re-investigate:
  - `data/sample/NVO/` — `NVO-6K-2026Q2-p28-c02` `Trade receivables 88,949 70,856`
    (30 Jun 2026 | 31 Dec 2025); `NVO-6K-2025Q2-p30-c02` `Trade receivables 69,575 71,949`;
    facts `trade_receivables` FY2023 `64770` / FY2024 `71949` / FY2025 `70856`, DKK, annual
    only. Table cells are **space**-joined here.
  - `runs/LLY-*/` (git-ignored, regenerate with `python -m pharmer.librarian LLY`) —
    `LLY-10K-FY2024-p41-c03` `Revenue\t$\t45,042.7`; `LLY-10K-FY2025-p43-c03`
    `Revenue\t$\t65,179\t$\t45,043`; `LLY-10Q-2026Q2-p5-c03` `Revenue\t$\t22,974 … 42,773`;
    facts revenue FY2024 `45043`, Q2 2026 `22974`, H1 2026 `42773`. **Tab**-joined.

## Tasks & Acceptance

**Execution:**
- [x] `pharmer/fact_checker.py` — corpus loading + quote verification: index
  `chunks.jsonl`/`facts.jsonl`, `_verify_quote()` returning `exact` / `normalized` /
  `absent`, and `_value_in_text()` confirming `anchor.value` appears in cited evidence
  (number parsing tolerant of `,` `$` `%` and `(1,234)` negatives).
- [x] `pharmer/fact_checker.py` — `_table_columns()`: parse a table chunk into header/body
  and map column index → period label, so a value is attributed to the period of **its own
  column**; unparseable header ⇒ `period_unconfirmed`, never an asserted period.
- [x] `pharmer/fact_checker.py` — `_tie()`: numeric equality at
  `0.5 * 10 ** -min(dp_a, dp_b)`, gated on period + unit + currency + metric concept +
  context agreement; plus `_corroborate()` searching every table chunk corpus-wide
  (comparative columns included).
- [x] `pharmer/fact_checker.py` — `_trend()` + flags: chronological strip from facts and
  table hits, `reversal` on a direction flip, `possibly_seasonal` when no prior-year
  same-period comparator exists.
- [x] `pharmer/fact_checker.py` + `pharmer/prompts/fact_checker.md` — one Haiku call per
  reasoning for claim support and ambiguous row-label equivalence; structured output,
  deterministic checks authoritative, failure ⇒ `unverified` ⇒ Medium cap.
- [x] `pharmer/fact_checker.py` — `_confidence()` applying the Always rules, then
  `run(run_dir)`: rewrite `findings.json` in place touching only the owned fields, and
  write `fact_check_report.json`.
- [x] `tests/fixtures/factcheck_findings.json` + `tests/test_fact_checker.py` — one offline
  test per I/O matrix row, built on the real LLY and NVO values above with a fake client as
  in `tests/test_llm.py`; assert non-owned fields are unchanged.
- [x] `docs/build-log.md` — append decisions, the real-API/live run result, and any
  confidently-wrong case.

**Acceptance Criteria:**
- Given a `findings.json` from Dedup, when the stage runs, then every reasoning has a
  non-null `confidence` and a `confidence_reason`, and a diff shows no change to any field
  outside the Fact Checker's Contract 2 ownership.
- Given the live LLY corpus and a hand-written finding anchoring FY2024 revenue
  `45042.7`, when the stage runs, then confidence is High, the tie cites
  `LLY-xbrl-revenue-FY2024`, and the report records the FY2025 10-K comparative-column hit.
- Given the NVO sample corpus and the `Q2 2026` receivables anchor, when the stage runs,
  then confidence is Medium with the missing second source named, `possibly_seasonal` is
  **absent** (Q2 2025 is in the corpus), and `reversal` is set.
- Given a run with no `ANTHROPIC_API_KEY`, when the stage runs, then it completes with
  every reasoning capped at Medium and no exception.
- `.venv/bin/python -m unittest discover tests` passes with no network access.

## Implementation Notes

**Files:** `pharmer/fact_checker.py` (new, the whole stage), `pharmer/prompts/fact_checker.md`
(new), `tests/test_fact_checker.py` (new, 85 tests), `tests/fixtures/factcheck_corpus.json` +
`factcheck_findings.json` (new, real LLY/NVO figures), `docs/build-log.md`. Full suite 258
passing / 4 skipped, offline.

**Two spec assumptions were wrong, both caught by running real data rather than fixtures:**

1. *The trend strip is "every period in the corpus".* Taken literally this pulled Lilly's
   revenue back to **FY2007** and mixed FY, H1 and Q figures into one series, so `reversal`
   fired on every LLY finding — a quarter of revenue against a full year is a four-fold
   step, not a change of direction. A flow metric's strip now stays inside the anchor's
   period family (FY with FY, Q2 with Q2); a balance metric keeps every period, since all
   of them are point-in-time balances, which is exactly why the Contract 2 example mixes
   FY2024 and H1 2026 receivables. Capped at `TREND_MAX_POINTS = 8` nearest the anchor,
   always keeping the anchor and its prior-year comparator.
2. *"the anchor value appears in cited evidence"* was implemented against the whole chunk,
   which let a quote of `Net income` verify a revenue anchor because the figure sat
   elsewhere on the page. The value must be carried by a quote that itself verified.

**Resolved spec tension (quote matching).** Boundaries said a `normalized`-only match caps
at Medium; the matrix said High stays reachable for tabs-vs-spaces. Both are now true
because normalization is split: `normalized` collapses whitespace runs only — lossless, so
High is reachable, which is the whole point of handling the corpus incompatibility — while
`partial`, which also discards case and punctuation, caps at Medium (architecture §4.3's
"partial match → Medium").

**Decisions not visible in the spec.** The authoritative tie is `anchor.value` against a
`facts.jsonl` row, the two artifacts Contract 2 names; the corpus-wide table search serves
corroboration and the trend strip. A period mismatch is distinguished from a missing second
source by whether the value ties to a fact of *another* period — if it does, the hunter read
a comparative column and mislabelled it (Low); if it ties to nothing and no fact exists for
that period, there is simply no second source (Medium). The judge is skipped when the quote
could not be located or does not carry the figure: the verdict is already Low and there is
nothing to judge, so the usage log holds one row per *judged* reasoning, not per reasoning.

**Live results.** LLY (`runs/LLY-20261018-0617`, fresh Librarian run): FY2024 revenue
`45,042.7` → **High**, tied to `LLY-xbrl-revenue-FY2024`, comparative-column hits recorded
on `LLY-10K-FY2025-p43-c03` and `-p49-c02`; Q2 2026 revenue `22,974` → High. NVO sample
corpus: `Q2 2026` receivables `88,949` → **Medium** (annual-only XBRL names the missing
second source), `reversal` set, `possibly_seasonal` absent because Q2 2025 is in the corpus;
`FY2025` `70,856` → High against the XBRL fact.

**Live re-run after the review fixes** (same LLY corpus, judge stubbed `supported`): FY2024
revenue `45,042.7` → High with comparative-column hits on `LLY-10K-FY2025-p43-c03`,
`-p49-c02` and `-p57-c02`; a second reasoning on the same anchor quoting the restated
`45,043` → High with Dedup's `contested` flag preserved (the first composite finding run
end to end); `65,179` labelled `FY2024` → Low naming `LLY-xbrl-revenue-FY2025`; `Q2 2026`
`22,974` → High. Without the stub the same run produced four `401`s, capped the three
otherwise-High reasonings at Medium, left the Low unchanged and exited 0.

**Left incomplete — the claim-support judge is unverified against the real API.** Partner
A's key is rejected with `401 invalid x-api-key` (build-log blocker, request ids recorded
there), so every live run capped at Medium through the degradation path. That did verify the
failure path on real traffic, but not a real verdict. The one behaviour still unproven is
requirement 9's point: during the live run a deliberately unsupported claim ("Q2 2026
revenue was driven by price increases on Mounjaro in Europe", cited only to the revenue
line) reached High with the judge stubbed as `supported` — which is precisely what the real
judge is there to stop. The build log records the exact command to settle it on a working
key.

## Spec Change Log

- The frozen Intent/Boundaries/matrix are unchanged. Three behaviours now say more than the
  matrix row they implement, all inside the existing Contract 2 vocabulary:
  `confidence` ∈ High/Medium/Low, no new fields on a finding.
  1. **Matrix row "Missing evidence"** splits in two. A citation that cannot be found in a
     corpus that *was* loaded is still **Low**; a corpus that could not be loaded at all
     (no `chunks.jsonl` rows) is **Medium** with the file named, because nothing was
     examined. The matrix's "Low, reason names the unlocatable citation" still holds for
     the case it describes.
  2. **"Like-for-like confirmed"** is now asked only of a part-year anchor or a reversed
     one. The Boundaries sentence ("`possibly_seasonal` = the anchor's period has no
     prior-year same-period comparator") read literally flags a full-year anchor at the
     oldest end of the corpus, where there is no seasonality to confirm — architecture
     §4.3 scopes the check to a part-year figure against a year end.
  3. **A non-numeric anchor** is `not_applicable`, i.e. unconfirmable (Medium), not a
     mismatch (Low). The Boundaries list only numeric anchors; Contract 2's
     `is_label_finding` and architecture §5's label guarantee need such a finding to
     survive the scoring gate.

## Review Triage Log

Pass 1 (2026-10-18). Three layers launched; `blind-hunter` and `verification-gap` returned,
`edge-case-hunter` died on a session limit and was relaunched and returned. Verdicts below
are mine after verifying each claim at its cited location.

| # | Finding (layer) | Verdict | Evidence | Route |
|---|---|---|---|---|
| 1 | Non-period column appended after the years, not kept in its printed position (blind, edge) | high | Reproduced: header `2025⇥Percent Change⇥2024` over `Revenue⇥65,179⇥45⇥45,043` attributed `45` to FY2024 and dropped `45,043`. Both fixtures print `Percent Change` last, so nothing caught it. | patch |
| 2 | `_label_matches` prefix rule grants hits to segment/product/other rows (blind, edge) | high | Reproduced: `Cash flow from operations`→`cash`, `Receivables from related parties`→`trade_receivables`, and against the real NVO corpus `Net sales at CER`→`revenue` — the reported-vs-CER confusion the build log already records as an AI-was-wrong case. Hits feed corroboration and the trend strip. | patch |
| 3 | No-credentials run raises `TypeError`, not an `anthropic` error (edge, filed as a claim) | high | Verified live with `env -u ANTHROPIC_API_KEY`: `TypeError("Could not resolve authentication method")` escaped `judge_claim`, the stage died and every confidence stayed null — a direct failure of the spec's own acceptance criterion. `FailingClient` injected `APIConnectionError`, which never exercises client construction, so it masked this. | patch |
| 4 | `possibly_seasonal` and the like-for-like cap applied to every anchor, including full-year flows (blind) | medium | Reproduced: an FY2023 revenue anchor at the oldest end of the corpus was flagged `possibly_seasonal` and capped at Medium for a reason no corpus could ever cure. Architecture §4.3 scopes the check to a part-year figure against a year end. | patch |
| 5 | `_reversal` fires on a flip anywhere in the strip, not the anchor's own move (blind) | medium | Reproduced: a wobble five periods before the anchor set `reversal` on a finding whose own direction never changed; the flag is what the Synthesizer recasts as the story. | patch |
| 6 | No company guard in `_tie` or the corpus scan (blind, edge) | medium | Confirmed by reading: facts were filtered on metric/period/unit/currency only and `ticker`/`entity` were never read, while architecture §4.0 adds rival filings and `rival_annual_report` to the corpus. Not reachable on today's single-company corpus, but one rival document makes a cross-company tie available. | patch |
| 7 | Non-numeric anchor reported as tie `mismatch` → fatal → Low (blind, edge) | medium | Reproduced: `_tie` returned `("mismatch", None, "the anchor carries no numeric value")`, which `_confidence` treats as fatal, so every label/product finding came back Low and the scoring gate dropped it. | patch |
| 8 | Trend cap branch never executed by any test (verification-gap, pre-verified) | medium | Filed with evidence: instrumenting the branch and running the suite showed it unreached, and replacing it with naive truncation left the suite passing while the anchor and its comparator fell out of the strip on a real history. | patch |
| 9 | `REVERSAL_MIN_PCT` noise floor unverified (verification-gap, pre-verified) | medium | Filed with evidence: setting it to 0.0 left the suite passing. | patch |
| 10 | `OWNED_FLAGS` re-run filter unverified — idempotency test uses the one fixture that never sets an owned flag (verification-gap, pre-verified) | medium | Filed with evidence: removing the filter left the suite passing while a second NVO pass produced `["reversal","reversal"]`. | patch |
| 11 | Unit/currency both blank satisfies the gate, so a tie reports `reproduced` with neither agreed (edge) | medium | Reproduced: `_tie` with `unit: None`/`currency: None` on both sides returned `reproduced`. The spec requires agreement on both before declaring a tie; a figure of unknown scale could be thousands or millions. | patch |
| 12 | Missing/empty `chunks.jsonl` silently marks every reasoning Low (blind) | medium | Confirmed: an absent corpus made every citation `absent`, i.e. Low, i.e. dropped by the scoring gate — reporting a pipeline-ordering mistake as bad evidence, and with no record in the output. | patch |
| 13 | Empty quote `""` substring-matches as `exact` (edge) | medium | Confirmed by reading `_verify_quote`: `"" in text` is True, so citing nothing scored a verbatim match. | patch |
| 14 | `chunk["text"]`, `chunk["doc_id"]`, `chunk["page"]` indexed without guards in a stage whose contract is never to raise (blind, edge) | low | Contract 1b mandates the fields, so a malformed row is not demonstrated reachable in a Librarian-built corpus — but the matrix does promise "log, never raise" for bad evidence, and the fix is a direct `.get`. | patch |
| 15 | Malformed JSON line in `chunks.jsonl`/`facts.jsonl` aborts the stage (edge) | low | Confirmed by reading `_load_corpus`: `json.loads` per line with no guard. Same "never raise" argument as #14. | patch |
| 16 | No fixture or test covers a composite finding with two or more reasonings (blind) | medium | Confirmed: every fixture finding had one reasoning and the helpers read `reasonings[0]`, so Dedup's own output shape — one anchor, several merged reasonings — was unverified, including per-reasoning flag preservation. | patch |
| 17 | `findings.json` rewritten non-atomically (blind, edge) | medium | Confirmed by reading: `open("w")` then `json.dump` over the pipeline's only copy of everything the hunters and Dedup produced. | patch |
| 18 | Unbounded `asyncio.gather` fans every judge call out at once; no concurrency cap (blind, edge) | medium | Confirmed by reading: the spec's own `cache_read_input_tokens > 0` check cannot hold when no request has returned before the rest are sent. | patch |
| 19 | `_scan_corpus` and `_trend` recomputed per reasoning though they depend only on the anchor metric (blind) | low | Confirmed by reading: O(reasonings × chunks); every reasoning of a composite finding re-parses the whole table set. | patch |
| 20 | Unused `import copy` (blind, verification-gap) | low | Confirmed: `copy` appeared nowhere else in the module. | patch |
| 21 | Doc drift — spec and build log claimed 47 tests / 220 passing (blind) | low | Confirmed: actual counts were 49/222 at the time of filing. | patch |
| 22 | Build-log blocker entry pasted the key prefix `sk-ant-api03-…` into a committed file (blind) | medium | Confirmed against CLAUDE.md ("Never commit the key or paste it into a chat"); the request ids carry the diagnostic value. | patch |
| 23 | `trend` can publish an anchor figure that exists nowhere in the corpus (verification-gap, other) | medium | Filed with evidence: `values.setdefault(anchor_period, …)` injects the hunter's own figure when no fact or table hit supports that period, in tension with CLAUDE.md's "every number comes from the corpus". | patch |
| 24 | One raising reasoning inside `gather` discards every verdict already computed (edge) | medium | Confirmed by reading: no `return_exceptions`. Once #3 is fixed the demonstrated crash path is closed; a blanket handler would hide genuine defects and `dedup.py:205` has the same shape, so this is a cross-stage decision. | defer |
| 25 | `metric_label_matches: None` never blocks High, so a cited segment subtotal could pass (edge) | maybe-false | Could not settle: the judge returns `None` only when no row label appears in the quotes, and a segment row cited for a statement-line anchor would have to pass `_value_in_evidence` on the anchor's own figure first. Would need a case where a subtotal equals the statement line exactly. | defer |
| 26 | Half-unit boundary ties two different numbers (`45,042.5` matches both `45,042` and `45,043`) (edge) | low | Real but intended: rounding half-up maps `45,042.5` to `45,043`, so an inclusive bound is the correct reading of "what rounding could have changed". First-match-wins on two equidistant candidates is a genuine wrinkle, not demonstrated on any real filing. | defer |
| 27 | A series crossing zero drops the move instead of measuring it (edge) | low | Confirmed by reading `_moves` (`if not before: continue`), but no mapped metric in `CONCEPT_MAP` crosses zero in the corpora, and the percentage change from zero is undefined. | defer |
| 28 | Space-joined fallback asserts the document's period for the first figure, which the spec calls `period_unconfirmed` (edge, filed as a claim) | low | Real tension in wording, but the behaviour is deliberate and documented in `_table_rows`: financial statements print the reporting period first, and it is what recovers Novo's Q2 2025 comparator for the like-for-like check. The fallback is also never used to grant a tie. | defer |
| 29 | `prompts/fact_checker.md` read at import time, so a missing file breaks any importing stage (edge) | low | Matches `dedup.py:27` and `synthesizer.py` exactly; the prompt ships with the package and a loud import failure is correct for a missing one. | rejected — consistent with the codebase, no named harm |
| 30 | `_trend`'s except fallback sorts lexicographically and uncapped (edge) | low | Reachable only through an unparseable period label, which `_period_from_fact` and Dedup's normalization do not produce; the fallback logs and degrades rather than raising. | rejected — not demonstrated reachable |
| 31 | `findings.json` absent/unreadable/not-a-list raises (edge) | low | The stage's input contract is Dedup's output; a missing input is a pipeline-ordering error that should fail loudly, not be inferred as empty. Code that loudly fails on an unreachable state is correct behaviour. | rejected |
| 32 | `message.content` or a block without `.type` raises `AttributeError` in `parse_support` (edge) | low | Shapes come from the SDK, which guarantees both; `dedup.py:100` reads them identically. | rejected — consistent with the codebase |
| 33 | "One Haiku call per reasoning" is violated because the judge is skipped for unverifiable evidence (edge, claim) | false | The skip is deliberate and recorded in Implementation Notes: with no locatable quote the verdict is already Low and there is nothing to judge. The sidecar now reports `reasonings_judged` separately, and a test pins one usage row per *judged* reasoning. | rejected on refutation |
| 34 | Label/product findings "can structurally never pass", leaving §5's label guarantee nothing to swap in (blind) | false | Refuted at `pharmer/scoring.py:20`: `CONFIDENCE_RANK = {"High": 2, "Medium": 1}` — Medium passes the gate, so a label finding with no second source stays eligible. Finding #7 (its Low verdict) was the real defect and is patched. | rejected on refutation |
| 35 | `_verify_quote` returns a fourth status `partial` not named in the task list (edge) | false | `partial` is specified in the frozen Boundaries and its Medium cap is tested; the task-list wording is a summary, not the contract. | rejected on refutation |

Independent review of the diff, 2026-10-18. Ten findings were reproducible defects; each
was fixed with a regression test that fails against the pre-review implementation (verified
by running the new tests against the staged original) and passes after.

| Finding | Root cause | Fix |
|---|---|---|
| A `Percent Change` column between two year columns stole the comparative period's value | `_column_periods` counted non-period columns and appended them all at the end, discarding their position | header read as an ordered token stream; a column named among the years keeps its slot, one named before any year follows its span's year columns |
| Segment/product/counterparty rows corroborated a statement line | `_label_matches` accepted any label *starting* with an alias | equality after normalization, with only presentation words (`net`/`gross`/`total`) stripped |
| A tie or trend point could come from another company | `_tie`/the table search filtered on metric, period, unit, currency only; `ticker` and `anchor.entity` were never read | per-document owner from the manifest; evidence skipped only when both owners are known and disagree |
| Label/non-numeric findings always came back Low | a missing number was reported as a tie `mismatch`, which is fatal | `not_applicable` tie status; the value-in-evidence check is skipped when there is no value; the quote must still verify |
| `possibly_seasonal` on full-year anchors, capping them at Medium forever | the like-for-like gate applied to every anchor | tri-state like-for-like, `not_applicable` for an unreversed full-year anchor |
| `reversal` fired on a wobble years away from the anchor | the flip was tested across the whole strip | moves after the anchor ignored, window of the last `REVERSAL_WINDOW_MOVES` |
| A missing corpus was reported as bad citations | `_load_corpus` only logged, and an empty index made every quote `absent` | `corpus_missing` quote status (Medium), `corpus_warnings` in the sidecar, bad JSONL lines skipped with a warning |
| `findings.json` rewritten in place | `open("w")` on the live file | temp file + `os.replace`, temp removed on failure |
| Every judge call fired at once, so the cached prefix was never warm | unbounded `asyncio.gather` | first call alone, then `asyncio.Semaphore(JUDGE_CONCURRENCY)` — kept local to this stage, since the shared cap belongs in `pharmer/llm.py` and needs Partner B |
| A chunk missing `text`/`page`/`doc_id` raised | direct `chunk["text"]` / `chunk['page']` indexing | `_chunk_text()` and `.get()` defaults; a malformed row is unverifiable evidence, not a crash |
| A blank unit or currency on both sides counted as agreement | `"" == ""` satisfied the like-for-like gate | equal-but-absent is `unavailable`, not `reproduced`; one side blank stays a mismatch |
| A key-less run raised instead of capping at Medium | client construction raises `TypeError`, not an `anthropic` error, so only `AnthropicError` was caught | `TypeError`/`OSError` caught too, with the user message built outside the `try` so our own bugs still surface |

Also fixed without behaviour change: unused `import copy`; the corpus table scan and trend
strip are memoized per (metric, ticker) instead of recomputed for every reasoning of a
composite finding; the sidecar gained `run_id`, `generated`, `corpus_warnings` and
`reasonings_judged`; the stale key fragment was removed from this change's build-log entry.

Not changed, by design: a number-tie contradiction stays **Low** (it is evidence about the
figure, not a gap in the corpus); an unlisted `doc_id` is still used (not knowing the owner
is not evidence of a different owner); `pharmer/llm.py`, `docs/contracts.md` and Partner B's
modules are untouched.

## Design Notes

**Why the tie is structured-vs-structured, and the text search is corroboration.** Contract
2's number-tie pairs two independently produced artifacts: the filing table the hunter cited
and XBRL. The authoritative comparison is therefore `anchor.value` vs a `facts.jsonl` row
with matching metric/period/unit/currency — fully deterministic, no table parsing required.
The corpus-wide table search exists for the other two jobs the live reconciliation exposed:
confirming the figure is really printed where claimed (possibly only in a *comparative*
column of a newer filing) and building the trend strip.

**Precision, from the real case.** Lilly's FY2024 10-K prints `45,042.7`; its FY2025 10-K
prints the same year as `45,043`; XBRL's latest-filed row is `45,043`. All three are
correct — the restatement rounded. `min(dp) = 0` ⇒ tolerance `0.5` ⇒ `|45042.7 − 45043| =
0.3` ties, while `45,142` (diff `99.3`) does not. A fixed 1 % tolerance would have accepted
`45,492` too, which is a different number.

**Column attribution is not optional.** `Revenue\t$\t65,179\t$\t45,043` contains both FY2025
and FY2024 revenue. "Value appears in chunk" would let `65,179` corroborate an FY2024 claim
— a wrong-column error, the exact class of mistake the number-tie rule exists to catch
(`docs/architecture.md` §2).

## Verification

**Commands:**
- `.venv/bin/python -m unittest discover tests` — expected: all pass, offline
- `.venv/bin/python -m pharmer.librarian LLY` then the stage on a hand-written FY2024
  revenue finding — expected: High, tie `LLY-xbrl-revenue-FY2024`, comparative hit on
  `LLY-10K-FY2025-p43-c03`
- the stage on the NVO sample corpus — expected: Medium on the `Q2 2026` anchor (annual-only
  XBRL), `reversal` set, `possibly_seasonal` absent
- `logs/api_usage.csv` — expected: `stage=fact-checker`, `model=claude-haiku-5-5`, one row
  per **judged** reasoning (a reasoning whose quote could not be located is never sent, so
  the count matches `reasonings_judged` in the sidecar, not `reasonings`),
  `cache_read_input_tokens > 0` after the first call — the first judge call is issued alone
  for exactly that reason, the rest fan out under `JUDGE_CONCURRENCY`

**Manual check:**
- `git diff` the rewritten `findings.json`: only `confidence`, `confidence_reason`, `trend`
  and the two owned flags may change.
