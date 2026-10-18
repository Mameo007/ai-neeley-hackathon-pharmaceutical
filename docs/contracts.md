# Data Contracts

The two formats that let Partner A (data + checks) and Partner B (agents +
output) build in parallel. Change a field here first, tell your partner, then
change code. Design rationale lives in `docs/architecture.md`.

**All example values are illustrative — only the shape is binding.** Never copy an
example number into findings or the deck.

All files are JSON / JSONL, UTF-8. One run writes to `runs/<TICKER>-<YYYYMMDD-HHMM>/`.

---

## Contract 1 — Corpus (Librarian output, owned by Partner A)

Three files per run.

### 1a. `manifest.json` — one entry per document read

```json
{
  "doc_id": "NVO-6K-2026Q2",
  "company": "Novo Nordisk",
  "ticker": "NVO",
  "doc_type": "6-K",
  "fiscal_period": "Q2 2026",
  "period_end": "2026-06-30",
  "filed_date": "2026-08-04",
  "url": "https://www.sec.gov/Archives/edgar/data/353278/000035327826000023/caq22026.htm",
  "accession": "0000353278-26-000023",
  "source": "live",
  "collected_date": null,
  "application_no": null,
  "labeler_code": null
}
```

- `doc_type` ∈ `10-K` `10-Q` `20-F` `6-K` `transcript` `label` `orange_book`
  `purple_book` `xbrl` `ir_workbook` `rival_annual_report`
- `fiscal_period`: `FY2025`, `Q2 2026`, `H1 2026` — match quarters on this,
  never on `period_end` (52/53-week filers).
- `source` ∈ `live` | `pre-collected` (then `collected_date` is required).
- `application_no` (`NDA209637`, `BLA125514`) and `labeler_code` (`0169`)
  are set for `label` docs only.

### 1b. `chunks.jsonl` — the only thing hunters may cite

```json
{"chunk_id": "NVO-6K-2026Q2-p14-c03", "doc_id": "NVO-6K-2026Q2", "page": 14,
 "section": "Balance sheet", "is_table": true,
 "text": "Trade receivables 87,123 69,575 ..."}
```

- `chunk_id` = `<doc_id>-p<page>-c<n>`; stable across re-runs of the same doc.
- `page` is what goes in the report footnote ("Novo Q2 2026 6-K, p.14").

### 1c. `facts.jsonl` — structured numbers for the number-tie rule

```json
{"fact_id": "NVO-xbrl-trade_receivables-FY2024", "doc_id": "NVO-XBRL",
 "metric": "trade_receivables", "value": 71949, "unit": "millions",
 "currency": "DKK", "fiscal_period": "FY2024", "period_end": "2024-12-31",
 "source_kind": "xbrl"}
```

- `metric` uses our concept-map keys (snake_case: `revenue`,
  `trade_receivables`, `inventory`, …), never raw XBRL tags.
- `source_kind` ∈ `xbrl` | `ir_workbook` | `filing_table`.

---

## Contract 2 — Finding (owned by Partner B; Fact Checker/Quant fields by Partner A)

`findings.json` — a list. One object per **fact anchor**; multiple
reasonings make it a composite row (§4.2).

```json
{
  "finding_id": "F-007",
  "ticker": "NVO",
  "anchor": {
    "metric": "trade_receivables",
    "entity": "Novo Nordisk",
    "fiscal_period": "H1 2026",
    "value": 87123,
    "unit": "millions",
    "currency": "DKK"
  },
  "is_label_finding": false,
  "contested": false,
  "lead_reasoning_id": "F-007-a",
  "reasonings": [
    {
      "reasoning_id": "F-007-a",
      "hunter": "channel_checker",
      "hunters": ["channel_checker"],
      "claim": "Receivables grew 25.5% in six months while sales grew 3% (reported, CER), suggesting channel loading.",
      "citations": [
        {"chunk_id": "NVO-6K-2026Q2-p14-c03", "quote": "Trade receivables 87,123"}
      ],
      "confidence": "Medium",
      "confidence_reason": "Verbatim match; tied to IR workbook; like-for-like H1 2025 comparison confirmed.",
      "flags": ["reversal"],
      "trend": [
        {"fiscal_period": "FY2024", "value": 71949},
        {"fiscal_period": "H1 2025", "value": 69575},
        {"fiscal_period": "FY2025", "value": 70870},
        {"fiscal_period": "H1 2026", "value": 87123}
      ],
      "impact": {
        "usd_m": -420.0,
        "eps_pct": -1.8,
        "formula": "excess_receivables * write_down_rate",
        "inputs": {"excess_receivables": 16253, "write_down_rate": 0.1},
        "fx": {"pair": "DKK/USD", "rate": 6.44, "date": "2026-06-30",
               "basis": "period_end", "source": "ECB"}
      },
      "priced_in": {"hits": 0, "evidence": [], "stock_move_pct": null},
      "score": null
    }
  ],
  "peer": {"status": null, "note": ""},
  "status": "candidate",
  "recommended": false
}
```

*(Numbers above are illustrative — only the shape is binding.)*

### Field ownership — who writes what

| Field | Written by | Rule |
|---|---|---|
| `anchor`, `reasonings[].hunter/claim/citations`, `is_label_finding` | Hunters (B) | Every `chunk_id` must exist in `chunks.jsonl`; `quote` must be a verbatim substring of that chunk's `text` |
| `contested`, merging into `reasonings[]`, `lead_reasoning_id`, `reasonings[].hunters` | Dedup (B) | Code groups by `anchor` (metric + entity + fiscal_period); LLM compares claims within a group |
| `confidence`, `confidence_reason`, `trend`, flags `reversal` / `possibly_seasonal` | Fact Checker (A) | Hunters leave `confidence` **null** — never self-assigned |
| `impact` | Quant (A) | Arithmetic and FX done in Python; `formula` + `inputs` always filled |
| `priced_in` | Priced-In Detector (B) | Web search only here; never used as evidence |
| `score` | Scoring code (B) | Materiality × Obscurity; composite row = strongest reasoning |
| `peer`, flag `peer_check_incomplete` | Peer Agent (B) | `status` ∈ `distinctive` / `industry_wide` / `incomplete` |
| `status`, `recommended` | Selection code (B) | `status` ∈ `candidate` / `verified` / `dropped` / `selected`; exactly one selected finding has `recommended: true` |

- `confidence` ∈ `High` | `Medium` | `Low` | `null`
- `flags` ⊆ `reversal`, `possibly_seasonal`, `contested`,
  `peer_check_incomplete`, `unmapped_metric`
- `hunter` is the agent that first raised the reasoning; `hunters` lists every hunter whose
  lead was merged into it, in order, starting with `hunter`. *(Proposed by B, pending A's
  sign-off.)*
- Human output: `run.json` holds `"sharpest_needle": "<finding_id>"` and
  `"product": "<brand>"` once the human decides.

### `leads.json` — hunter output, Dedup input *(proposed by B, pending A's sign-off)*

Written by `pharmer/hunters` to `runs/<run_id>/leads.json`; read only by Dedup, which writes
`findings.json`. A list of Contract 2 Findings with these restrictions:

- Exactly **one** reasoning per Finding; one Finding per lead (no grouping yet).
- `finding_id` `F-001`, `F-002`, … in hunter order; `reasoning_id` = `<finding_id>-a`. Dedup
  renumbers both, so these ids never reach later stages.
- Hunter fields filled (`anchor`, `is_label_finding`, `reasonings[0].hunter/claim/citations`);
  every citation already checked against `chunks.jsonl`, and one quote contains `anchor.value`.
- Everything else at its empty value: `contested: false`, `lead_reasoning_id: null`, no
  `hunters`, `confidence`/`confidence_reason`/`impact`/`priced_in`/`score` `null`, `flags`/`trend`
  `[]`, `peer` `{"status": null, "note": ""}`, `status: "candidate"`, `recommended: false`.
- Dedup groups on `anchor.metric` + `anchor.entity` + `anchor.fiscal_period`, ignoring case,
  spacing and a trailing legal form (`A/S`, `Inc.`, `plc`, …); periods are compared as labelled
  (`H1 2026` ≠ `Q2 2026`). Hyphens count as spaces. `findings.json` carries the normalized
  `metric` (snake_case) and `fiscal_period` (uppercase, single spaces).
- `lead_reasoning_id` in `findings.json` is the earliest-raised reasoning (`-a`, hunter run
  order), not the strongest; scoring ranks a composite row by its strongest reasoning.

### `comparison.json` — Comparison Builder output *(proposed by B, pending A's sign-off)*

Written by `pharmer/comparison.py` to `runs/<run_id>/comparison.json`; read by the Synthesizer
(as context) and the Exec Report (as the comparison table). Absent when the stage did not run;
the report then prints "Comparison table not built for this run."

```json
{
  "columns": ["Dimension", "Brand A", "Brand B"],
  "rows": [
    ["Approved uses", "Indication X", "Indication X, Indication Y"],
    ["Dosing", "Once weekly", "not disclosed"]
  ]
}
```

*(Values above are illustrative — only the shape is binding.)*

- `columns`: non-empty list of header strings; `rows`: list of lists, one cell per column.
  A short row is padded with `"not disclosed"`, a long one trimmed.
- Rows are the five §4.7 dimensions: uses, boxed warnings/safety, dosing/convenience,
  patent/exclusivity timeline, pricing & gross-to-net exposure.
- A cell that cannot be sourced is the string `"not disclosed"`, never a guess.
- The report also reads a list of row objects (`[{"dimension": …, "Brand A": …}]`, keys as
  columns); any other shape is shown as a visible "unrecognized shape" warning.
- Open: per-cell citations (label section / Orange or Purple Book chunk ids) are not in this
  shape yet, so cells print without footnotes; to be added with the Comparison Builder.

---

## Contract 3 — API usage log (written by `pharmer/llm.py`, both partners)

Every Claude API call goes through `pharmer.llm.call()` or
`pharmer.llm.run_agent()`, which append one row per request to
`logs/api_usage.csv` (git-ignored). A tool-loop agent writes one row per turn.

| Column | Example | Notes |
|---|---|---|
| `timestamp` | `2026-10-18T02:31:07+00:00` | UTC |
| `run_id` | `NVO-20261018-0230` | Same as the `runs/` folder name |
| `partner` | `B` | From `PHARMER_PARTNER` in `.env` |
| `stage` | `hunters` | An `area` name from the branch rules in `CLAUDE.md` |
| `agent` | `redliner` | |
| `turn` | `3` | Turn within the agent's tool loop; `1` for single calls |
| `model` | `claude-opus-5-5` | |
| `input_tokens`, `output_tokens` | `41200`, `1850` | |
| `cache_creation_input_tokens`, `cache_read_input_tokens` | `0`, `38900` | `cache_read` staying 0 on repeated calls = caching broken |
| `web_search_requests` | `2` | Priced-In Detector |
| `stop_reason` | `end_turn` | `tool_use`, `end_turn`, `max_tokens`, `refusal`, `pause_turn` |
| `latency_ms` | `8400` | Wall time of the request |
| `request_id` | `req_…` | For reporting failures |
| `est_cost_usd` | `0.18` | From `PRICES` in `pharmer/llm.py` — an estimate, verify before quoting |
| `error` | `RateLimitError: …` | Set only when the request failed |
