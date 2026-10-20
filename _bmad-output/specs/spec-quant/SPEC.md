---
id: SPEC-quant
companions: []
sources:
  - docs/architecture.md
  - docs/contracts.md
  - _bmad-output/implementation-artifacts/deferred-work.md
---

> **Canonical contract.** This SPEC and the files in `companions:` are the complete, preservation-validated contract for what to build, test, and validate. Source documents listed in frontmatter are for traceability — consult them only if you need narrative rationale or prose color this contract intentionally omits.

# Quant: reproducible impact per verified finding

## Why

A pain, and a measured one. `pharmer/scoring.py` ranks on Materiality × Obscurity, and `materiality()` reads exactly one number — `reasonings[].impact.eps_pct`. Nothing writes it, so every reasoning scores `null` and the ranking falls back to tie-breaks; the deliverable's "how big is this" column has no source. The Fact Checker just unblocked the confidence gate in front of scoring, which makes this the next thing standing between a verified finding and a ranked one. The force behind the stage is also evidential: the case requires a dollar figure, and a figure a model simply emits cannot be audited. Architecture §4.4 settles that by splitting the work — the model chooses the formula and names its inputs, Python does the arithmetic — so every number in the report can be recomputed by hand from what the finding itself carries.

## Capabilities

- **CAP-1**
  - **intent:** Each reasoning that passes the confidence gate carries a Contract 2 `impact` (`usd_m`, `eps_pct`, `formula`, `inputs`, `fx`) or an explicit `null`, and nothing else on the finding changes.
  - **success:** Run over a Dedup+Fact-Checker `findings.json`: every High/Medium reasoning has `impact` non-null or a recorded reason for `null`, and a diff shows no field outside `reasonings[].impact` altered.
- **CAP-2**
  - **intent:** A reviewer can recompute any published figure from the finding itself, so the number is auditable rather than asserted.
  - **success:** For every non-null `impact`, evaluating `formula` against `inputs` by hand reproduces `usd_m` exactly; a figure returned by the model is re-derived, never copied through.
- **CAP-3**
  - **intent:** Every input value comes from the run corpus — `facts.jsonl` first, then the chunk the reasoning cites — with its provenance recorded.
  - **success:** Each entry in `inputs` resolves in the sidecar to a `fact_id` or a `chunk_id` plus the quoted figure; an input that resolves to neither leaves `impact` null.
- **CAP-4**
  - **intent:** A figure reported in another currency can be compared in absolute dollars, at the conversion basis the figure's own nature demands.
  - **success:** `impact.fx` names `pair`, `rate`, `date`, `basis` and `source`, with `period_average` for a flow and `period_end` for a balance; the native amount stays visible in `inputs`, and native + converted + rate + date are all recoverable for the report footnote.
- **CAP-5**
  - **intent:** Growth reaches the report as the filer discloses it — reported growth alongside the company's own constant-currency or FX-effect figure.
  - **success:** No output contains a constant-currency growth rate this stage computed; where a CER figure appears it is traceable to a cited chunk.
- **CAP-6**
  - **intent:** A finding that cannot be quantified is left unquantified, with the reason recorded.
  - **success:** A refused or malformed formula, an input absent from the corpus, or an unavailable FX rate each leave `impact` null with a named reason in the sidecar, the stage exits 0, and no exception escapes.
- **CAP-7**
  - **intent:** The stage leaves an audit trail a reviewer can follow without rerunning it.
  - **success:** `runs/<run_id>/quant_report.json` holds, per reasoning, the chosen formula, each resolved input with provenance, the recomputation, the FX basis, and any skip reason — the `librarian_report.json` / `fact_check_report.json` precedent.

## Constraints

- Writes only `reasonings[].impact`. Every other Contract 2 field is preserved byte-for-byte.
- `formula` and `inputs` are always filled whenever `impact` is non-null (`docs/contracts.md` ownership row), so any published number can be recomputed by hand.
- Arithmetic and FX run in Python. A model-supplied figure is an input to re-derive, never the answer.
- Claude is reached only through `pharmer/llm.py` with `llm.HAIKU`; the system prompt lives in `pharmer/prompts/quant.md` and stays byte-identical across calls (no dates, no run ids) so the cached prefix is reused.
- Standard library only. ECB rates over `urllib`, cached in the run folder so a re-run is deterministic; tests run offline against a rate fixture and never touch the network.
- Our own FX conversion exists for cross-company absolute-dollar comparison only (architecture §4.4). It never replaces a figure the filer itself reports in its own currency, and it never becomes the basis of a growth rate.
- The stage runs and is tested from a `findings.json` plus a corpus fixture, with no Librarian run required — it ships independently of the live pipeline (`deferred-work.md`).
- The stage does not re-verify evidence — the Fact Checker already did. A reasoning whose `confidence` is `Low` or `null` is skipped, because Partner B's scoring gate drops it anyway and the call would be wasted.
- `findings.json` is rewritten atomically (temp file + `os.replace`) and the stage is idempotent on re-run, matching `pharmer/fact_checker.py`.
- No change to `docs/contracts.md`; no edit to Partner B's modules (`hunters`, `dedup`, `priced_in`, `scoring`, `peer`, `comparison`, `synthesizer`, `ceo_move`, `report`, `orchestrator.py`).
- Entry point `async def run(run_dir: Path) -> None`. `orchestrator.py` already lists `quant` in `START_STAGES` as a non-required stage, so no orchestrator change is needed.

## Non-goals

- Computing our own constant-currency growth. Methodologies differ per filer, so ours would manufacture mismatches against the filings (architecture §4.4).
- Scoring, ranking or selection. `impact` is an input to Partner B's scoring; this stage never writes `score`, `status` or `recommended`.
- Re-verifying citations, re-matching quotes or re-tying numbers. That is the Fact Checker's job and is already done when Quant runs.
- Acquiring new corpus material. If a needed figure is not in the run folder, the finding goes unquantified; extending `CONCEPT_MAP` is a Librarian follow-up, not part of this stage.

## Success signal

Run against the live LLY and NVO corpora, a verified finding comes back with an `impact` whose `formula` and `inputs` a reviewer can recompute by hand to the same `usd_m`, whose `fx` block names the ECB rate and date behind a DKK→USD conversion, and which makes `pharmer/scoring.py` produce a non-null score where it previously produced `null` — with the full offline suite passing and no network access.

## Assumptions

- `eps_pct` is the impact expressed as a percentage of EPS ("or equivalent", architecture §5.1.2), which is what `scoring.py materiality()` consumes.
- `usd_m` is millions of USD and signed: negative for damage, positive for upside (`docs/contracts.md` shows `-420.0`).
- The native-currency amount and the rate live in `inputs` / `fx` rather than new fields, so Contract 2's `impact` shape does not change.
- ECB daily reference rates are fetched over stdlib `urllib` and cached per run; USD/DKK is derived via the EUR cross rate, as architecture §4.4 states.

## Open Questions

- `eps_pct` has no structured denominator in the corpus: `CONCEPT_MAP` carries no share count or EPS, and the Librarian deliberately skips share and per-share units. Extend the concept map (a Librarian follow-up, Partner A) or parse the diluted-share row out of the cited filing table at a lower confidence ceiling? `scoring.py materiality()` depends on `eps_pct` alone, so this decides whether Quant moves the ranking at all.
- Contract 2 lists the `unmapped_metric` flag but its ownership table does not say who writes it. If Quant is to set it when a metric falls back to the filing table (architecture §4.4), that row needs both partners' sign-off.
- Does a finding whose impact cannot be computed stay eligible at `score: null` (`scoring.py` line 8 says it does), or should Quant record a zero impact instead? This spec assumes `null`, never zero.
