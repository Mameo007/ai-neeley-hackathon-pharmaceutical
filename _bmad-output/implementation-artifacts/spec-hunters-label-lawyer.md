---
title: 'Label Lawyer hunter (company-wide pass)'
type: 'feature'
created: '2026-10-18'
status: 'done'
route: 'oneshot'
review_loop_iteration: 0
context: []
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** The hunter swarm has no hunter for product fine print (architecture §4.1: label breadth, boxed warnings, REMS vs. rivals), so scoring's label guarantee has nothing labelled `is_label_finding: true` to work with.

**Approach:** Add a sixth hunter, `label_lawyer`, following the existing pattern (module + brief, registered in `HUNTERS`, run by `harness.run_hunter`). Company-wide pass only, in `start` with the other hunters. Its leads set `is_label_finding: true`, concern the company's own product, and use rival labels only as contrast. The product-specific pass and the scoring retry stay deferred (§9.5). Done = offline tests pass with a fake LLM, plus a build-log entry; Partner A does the real API run.

</frozen-after-approval>

## Implementation Notes

- New `pharmer/hunters/label_lawyer.py` (copy of `footnote_miner.py`) and `pharmer/prompts/label_lawyer.md` (starts with `# `, mentions `fiscal_period`, no "Novo").
- Append to `HUNTERS` in `pharmer/hunters/__init__.py`, after `gross_to_net`, so the existing F-00n order stays the same. Update the name list in `tests/test_hunters.py::HunterRegistryTests`.
- Harness constraint: the anchor needs a numeric `value` that appears in a quote, and a non-empty `fiscal_period`. Label documents have `fiscal_period: ''`. Decision: anchor on a number printed in the own label (a dose, an age limit, an adverse-reaction rate, a count). Use the `fiscal_period` of the latest filing listed under `Documents:` and the product name as `entity`. Use `currency: "none"` for non-money units.
- `corpus_overview` lists docs without company/ticker, so the hunter cannot tell its own label from a rival's. Add the doc's `ticker` to each `Documents:` line. This only changes the first user message, so caching is not affected.
- Tests: the brief requires `is_label_finding: true` and covers rival contrast. Add one fake-LLM run of `label_lawyer` on the sample corpus that cites an NVO-LABEL chunk and comes out as a Finding with `is_label_finding: true`.
- Done as planned. Files: label_lawyer.py, prompts/label_lawyer.md, hunters/__init__.py, harness.py (overview line), tests/test_hunters.py, docs/build-log.md. Suite: 383 OK.

## Review Triage Log

- The Blind Hunter subagent was skipped because the user asked to save tokens. In a self-review, `doc['ticker']` in corpus_overview is safe because Contract 1 requires ticker on every manifest entry (false). A label lead's metric has no fact, so the Fact Checker returns not_applicable and caps confidence at Medium (build log, A). The lead stays eligible for the label guarantee (false).
