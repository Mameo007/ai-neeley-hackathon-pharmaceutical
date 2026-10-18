- source_spec: `_bmad-output/implementation-artifacts/spec-sample-novo-corpus.md`
  summary: Add Novo's FY2025 and FY2024 annual-report exhibits (nvo-20251231_d2.htm, nvo-20241231_d2.htm) to the sample corpus so Redliner has year-over-year risk-factor text.
  evidence: Both are PDF-to-HTML with no page-break markers and different structures (FY2025 has 125 inconsistent printed footers, FY2024 none); real page numbers need custom detection, too costly for the first sample.
- source_spec: `_bmad-output/implementation-artifacts/spec-sample-novo-corpus.md`
  summary: Settle whether half-year 6-K docs are labelled `Q2 2026` (Contract 1 manifest example) or `H1 2026` (Contract 2 anchor example), so Quant/Fact Checker period matching on `fiscal_period` cannot miss.
  evidence: docs/contracts.md Contract 1a example uses `fiscal_period "Q2 2026"` for the half-year 6-K while Contract 2's anchor uses `H1 2026`, and Contract 1 says to match periods on `fiscal_period`; changing it needs both partners.
- source_spec: `_bmad-output/implementation-artifacts/spec-sample-novo-corpus.md`
  summary: List the new `scripts/` and `data/sample/<TICKER>/` folders (and how stages point at the sample corpus) in the layout section of docs/conventions.md.
  evidence: The sample-corpus change adds both top-level folders, but docs/conventions.md — which both partners' Claude sessions read before every task — does not mention them, so the other session cannot discover the fixture.
