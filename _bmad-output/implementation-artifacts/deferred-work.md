# Deferred Work

Goals and integrations split out of a Build run. Append-only — do not edit
existing entries.

- source_spec: `_bmad-output/implementation-artifacts/spec-librarian-edgar-filings.md`
  summary: Librarian structured-facts path — populate `facts.jsonl` from XBRL
    companyfacts and IR Excel workbooks, with the hand-built US-GAAP↔IFRS
    concept map and the `unmapped_metric` miss log.
  evidence: Split from the Librarian stage on 2026-10-17 so the filings corpus
    (`manifest.json` + `chunks.jsonl`) can merge first and unblock Partner B's
    hunters. The EDGAR slice writes `facts.jsonl` as an empty file so the
    Contract 1 file set exists; the Fact Checker's number-tie rule stays blocked
    until this lands.

- source_spec: `_bmad-output/implementation-artifacts/spec-librarian-edgar-filings.md`
  summary: Librarian label/product path — DailyMed SPL selection via the
    NDA/BLA → labeler-code allowlist → latest-version rule, plus Orange Book and
    Purple Book acquisition.
  evidence: Split from the Librarian stage on 2026-10-17. Independent of the
    filings corpus (different sources, different `doc_type` values) and only
    needed once a product is selected, which is stage 9. The source probe already
    proved every appendix product resolves, so this is low-risk to defer.

- source_spec: `_bmad-output/implementation-artifacts/spec-librarian-edgar-filings.md`
  summary: Librarian rival + transcript corpus — rival companies' EDGAR filings
    for Peer Contraster/Peer Agent, the pre-bundled non-EDGAR rival annual
    reports, and the ~40 pre-bundled earnings-call transcripts.
  evidence: Split from the Librarian stage on 2026-10-17. The EDGAR slice
    deliberately keeps its fetch keyed on CIK so a rival is just another CIK —
    this follow-up is mostly a config list plus the `pre-collected` manifest
    path, not new fetch logic.

- source_spec: none
  summary: Fact Checker stage (`pharmer/fact_checker.py`) — writes `confidence`,
    `confidence_reason`, `trend`, and the `reversal` / `possibly_seasonal` flags
    into `findings.json`.
  evidence: Split from the Partner A build intent on 2026-10-17. Independently
    shippable — needs only a `findings.json` fixture, not a real Librarian run.
    Deferred because Partner B is blocked until `chunks.jsonl` exists, so the
    Librarian stage goes first.

- source_spec: none
  summary: Quant stage (`pharmer/quant.py`) — writes `impact` (`usd_m`,
    `eps_pct`, `formula`, `inputs`, `fx`), with ECB FX conversion done in Python.
  evidence: Split from the Partner A build intent on 2026-10-17. Independently
    shippable and fixture-driven.

- source_spec: `_bmad-output/implementation-artifacts/spec-sample-novo-corpus.md`
  summary: Add Novo's FY2025 and FY2024 annual-report exhibits (nvo-20251231_d2.htm, nvo-20241231_d2.htm) to the sample corpus so Redliner has year-over-year risk-factor text.
  evidence: Both are PDF-to-HTML with no page-break markers and different structures (FY2025 has 125 inconsistent printed footers, FY2024 none); real page numbers need custom detection, too costly for the first sample.
- source_spec: `_bmad-output/implementation-artifacts/spec-sample-novo-corpus.md`
  summary: Settle whether half-year 6-K docs are labelled `Q2 2026` (Contract 1 manifest example) or `H1 2026` (Contract 2 anchor example), so Quant/Fact Checker period matching on `fiscal_period` cannot miss.
  evidence: docs/contracts.md Contract 1a example uses `fiscal_period "Q2 2026"` for the half-year 6-K while Contract 2's anchor uses `H1 2026`, and Contract 1 says to match periods on `fiscal_period`; changing it needs both partners.
- source_spec: `_bmad-output/implementation-artifacts/spec-sample-novo-corpus.md`
  summary: List the new `scripts/` and `data/sample/<TICKER>/` folders (and how stages point at the sample corpus) in the layout section of docs/conventions.md.
  evidence: The sample-corpus change adds both top-level folders, but docs/conventions.md — which both partners' Claude sessions read before every task — does not mention them, so the other session cannot discover the fixture.
- source_spec: `_bmad-output/implementation-artifacts/spec-hunters-harness-channel-checker.md`
  summary: Add `runs/<run_id>/leads.json` (hunter output, Dedup input, provisional F-nnn ids that Dedup may renumber) to docs/contracts.md.
  evidence: The harness writes leads.json but Contract 2 only names findings.json; contract changes need both partners (CLAUDE.md).
- source_spec: `_bmad-output/implementation-artifacts/spec-hunters-harness-channel-checker.md`
  summary: Confirm the shared hunter prefix (tools + hunter_rules.md, ~1.5k tokens) actually caches on Opus, and warm it with one call before fanning out.
  evidence: maybe-false, medium if true — the prefix may be below Opus's minimum cacheable length, making the explicit system breakpoint a no-op; settle by reading cache_creation/cache_read in logs/api_usage.csv on the first real run.
- source_spec: `_bmad-output/implementation-artifacts/spec-hunters-harness-channel-checker.md`
  summary: Bound get_facts output (cap or required filter) once Partner A's Librarian adds IR-workbook facts.
  evidence: get_facts() with no filter returns every fact; harmless at 15 sample facts, context-flooding on a full corpus.
- source_spec: `_bmad-output/implementation-artifacts/spec-dedup-judge.md`
  summary: When leads in one anchor group cite different anchor values/units, Dedup only logs a warning; nothing in findings.json tells the Fact Checker, and a later reasoning can sit under a value none of its quotes contain.
  evidence: build_finding keeps the earliest anchor; needs either a value in the grouping key or a contract flag (e.g. value_mismatch), which needs Partner A.

- source_spec: `_bmad-output/implementation-artifacts/spec-scoring-selection.md`
  summary: Give the Peer Agent a first-pass top-~10 from scoring without selection side effects (or have the orchestrator run scoring -> peer -> scoring).
  evidence: §4.6 runs Peer on the first-pass top ~10; scoring.apply marks selected/recommended; ranked_units already exposes the order.
