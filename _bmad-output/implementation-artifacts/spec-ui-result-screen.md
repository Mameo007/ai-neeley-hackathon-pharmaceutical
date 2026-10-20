---
title: 'UI step 4: hero result screen'
type: 'feature'
created: '2026-10-18'
status: 'done'
route: 'oneshot'
review_loop_iteration: 0
context: ['{project-root}/docs/ui-plan.md']
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** A finished run shows a bold "Verdict:" line, a "CEO move:" line and a download button; a judge glancing at it cannot see the Sharpest Needle, how big it is, or the evidence (docs/ui-plan.md, hero screen 3 / build step 4).

**Approach:** Rewrite `results_section` as the hero result: the needle's claim as a plain-English headline; its impact as one big monospace number (`impact.usd_m`, else `impact.eps_pct`, from findings.json; when Quant left both null, the finding's anchor value as reported — never a made-up number) with a count-up animation; the Synthesizer verdict as the one-line verdict; the verbatim quote as a serif blockquote with its source chip from `report.build_model`; and a CEO decision card beside it showing what ceo_move.json holds (action, timeframe, trade_off, downside_if_wrong, why_it_works) plus the PDF download. Budget and rejected alternatives are not in ceo_move.json — show what exists, note the gap, no contract change. On the review screen, the score-formula caption becomes a one-line verdict with the math in a "How we scored it" expander, and quotes render as serif blockquotes without escape backslashes. Logic in pure helpers above `main()`; mockup 3-result.png is layout only.

</frozen-after-approval>

## Implementation Notes

- `app.py` pure helpers: `_num`, `_signed`, `anchor_number` (value as reported with currency + m/bn/k/% or the unit), `big_number` (usd_m → "+$18.1bn"/"−$420m" with the EPS % in the caption; else eps_pct → "−2.5%"; else anchor → "DKK 87,123m" captioned "as reported; impact not sized yet"; else None), `_strongest` (highest-scored reasoning, else lead — the one scoring ranks the finding by, so headline, number and quote come from the same reasoning), `result_model` (on top of `report.build_model`: needle via resolve_needle, quote + footnote source, CEO fields from `ceo_move` callout, priced-in words), `quote_html`, `why_line`, `ceo_html`.
- Render: `results_section(run_dir, ticker)` = header "<Company> · the result", two keyed panels (`needle-card` 3/5, `ceo-card` 2/5). Big number via `count_up`: a `components.html` iframe with a 1.2 s ease-out JS count; the final frame is the exact text; reduced-motion viewers get the final text straight away. Verified by sampling the iframe text in headless Chrome: −0.2% … −2.4% → −2.5%.
- Verdict = the Synthesizer verdict (already one or two sentences). Chips: "Fact Checker: <badge> confidence", "market: <priced-in call>". CEO card shows The move / Trade-off / What would prove us wrong / Why it works, titled "CEO decision · <timeframe>". Gap: ceo_move.json has no budget or rejected alternatives — not shown, not added (contract change needs both partners).
- Review screen: the "Why selected: score = …" caption is now `why_line` ("Moves earnings per share by about 4.5%; nobody is talking about it yet.") with the formula in a "How we scored it" expander; quotes render through `quote_html` (escaped HTML, serif) so filing text needs no markdown escaping and shows no backslashes.
- Not built (Could): trend/table block from the mockup, header scoreboard.
- Tests: 44 app tests (+ big_number priority/fallback/None, anchor formats, strongest reasoning, result_model on done fixture and with no selected findings, why_line, quote_html escaping; page tests read st.html bodies). Full suite 489 OK; api_usage.csv untouched.
- Review patches: `_strongest` only considers selected reasonings; stale/blank verdict shows the report's own reason; a corrupt findings.json shows an error + PDF button instead of crashing above the diagnostics; "AI pick, not yet confirmed by the analyst" on the fallback needle; missing CEO move shows its reason; usd_m rounds before choosing m/bn; "not yet fact-checked" chip for Unverified, red for Low; why_line says "no matching news stories found"; number font scales down (min(76px, 11vw)), no wrap. 45 app tests; suite 490 OK.

## Review Triage Log

- high (patched): `_strongest` could pick an unselected (higher-scored) reasoning for the headline/number while badge and quote came from a selected one.
- medium (patched): stale verdict reported as "Synthesizer failed" → report warning text.
- medium (patched): corrupt findings.json crashed the result above the diagnostics → guarded with error + PDF button (test).
- medium (patched): AI-recommended needle shown as if analyst-named → label.
- medium (patched): missing ceo_move.json left the CEO column silent → reason shown (test).
- medium (patched): "nobody is talking about it yet" claims beyond the Priced-In search → "no matching news stories found".
- low (patched): 999.96 → "+$1,000m" → rounds first (tests). low (patched): "Unverified confidence" chip wording / Low not highlighted. low (patched): number clipping at narrow widths.
- low (rejected): build_model runs twice per done render (results + review) — small files, one render per interaction.
- low (rejected): two headings (run name + "the result"); CEO_LABELS keyed on report labels — covered by test_result_model_on_a_done_run's field list.
- low (rejected): iframe number not using the page's CSS variable — same font stack written out.
- low (patched in commit): build-log entry.
