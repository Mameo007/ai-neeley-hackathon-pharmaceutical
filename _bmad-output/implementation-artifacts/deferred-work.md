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
