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
| `contested`, merging into `reasonings[]`, `lead_reasoning_id` | Dedup (B) | Code groups by `anchor` (metric + entity + fiscal_period); LLM compares claims within a group |
| `confidence`, `confidence_reason`, `trend`, flags `reversal` / `possibly_seasonal` | Fact Checker (A) | Hunters leave `confidence` **null** — never self-assigned |
| `impact` | Quant (A) | Arithmetic and FX done in Python; `formula` + `inputs` always filled |
| `priced_in` | Priced-In Detector (B) | Web search only here; never used as evidence |
| `score` | Scoring code (B) | Materiality × Obscurity; composite row = strongest reasoning |
| `peer`, flag `peer_check_incomplete` | Peer Agent (B) | `status` ∈ `distinctive` / `industry_wide` / `incomplete` |
| `status`, `recommended` | Selection code (B) | `status` ∈ `candidate` / `verified` / `dropped` / `selected`; exactly one selected finding has `recommended: true` |

- `confidence` ∈ `High` | `Medium` | `Low` | `null`
- `flags` ⊆ `reversal`, `possibly_seasonal`, `contested`,
  `peer_check_incomplete`, `unmapped_metric`
- Human output: `run.json` holds `"sharpest_needle": "<finding_id>"` and
  `"product": "<brand>"` once the human decides.

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
