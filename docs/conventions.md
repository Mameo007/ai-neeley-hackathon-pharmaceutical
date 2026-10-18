# Coding Conventions

Rules both partners (and both Claude sessions) follow so the code reads as
one codebase. Data formats are in `docs/contracts.md`; Git and API rules are
in `CLAUDE.md`. When this file and existing code disagree, follow this file
and fix the code.

## Project layout

```
pharmer/                  Python package — all pipeline code
  llm.py                  shared Claude client (every API call goes here)
  librarian.py            Partner A
  fact_checker.py         Partner A
  quant.py                Partner A
  hunters/                Partner B — one module per hunter
    __init__.py           runner: fans out the hunters in parallel
    channel_checker.py
    redliner.py
  dedup.py  priced_in.py  scoring.py  peer.py          Partner B
  comparison.py  synthesizer.py  ceo_move.py  report.py  Partner B
  prompts/                one Markdown file per agent prompt
    channel_checker.md
orchestrator.py           runs the whole pipeline for one company
app.py                    Streamlit UI (demo entry point)
tests/                    test_<module>.py, offline only
runs/<run_id>/            run outputs (git-ignored)
logs/api_usage.csv        per-call API usage (git-ignored)
```

One module per pipeline stage; a stage module exposes one entry function
(below). Don't create a module for something that doesn't have a stage yet.

## Naming

| Thing | Style | Example |
|---|---|---|
| Modules, files, functions, variables | `snake_case` | `fact_checker.py`, `load_chunks()` |
| Constants | `UPPER_SNAKE_CASE` | `LOG_PATH`, `PRICES` |
| Classes | `PascalCase` | `FakeClient` |
| JSON keys | `snake_case` | `fiscal_period`, `lead_reasoning_id` |
| Agent / hunter names (code, JSON `hunter`, log `agent`, prompt file) | `snake_case`, one name everywhere | `channel_checker` → `hunters/channel_checker.py`, `prompts/channel_checker.md` |
| Branch names, log `stage` values | `kebab-case` area names | `fact-checker`, `priced-in` |
| Private helpers | leading underscore | `_load_dotenv()` |

The same agent must have the same name in its module, its prompt file, the
`hunter` field of findings, and the `agent` column of the usage log.

### IDs

| ID | Format | Example |
|---|---|---|
| `run_id` | `<TICKER>-<YYYYMMDD-HHMM>` (UTC) | `NVO-20261018-0230` |
| `doc_id` | `<TICKER>-<DOC_TYPE>-<PERIOD>` | `NVO-6K-2026Q2`, `NVO-20F-FY2025` |
| `chunk_id` | `<doc_id>-p<page>-c<n>` | `NVO-6K-2026Q2-p14-c03` |
| `fact_id` | `<TICKER>-<source_kind>-<metric>-<period>` | `NVO-xbrl-trade_receivables-FY2024` |
| `finding_id` / `reasoning_id` | `F-<nnn>` / `F-<nnn>-<letter>` | `F-007`, `F-007-a` |

Tickers are uppercase (`NVO`, `LLY`, `AZN`).

## Code style

- **Python 3.12**, PEP 8, 4-space indent, lines ≤ 100 characters.
- **Standard library first.** A new dependency goes in `requirements.txt` in
  the same PR, with a reason in the PR description.
- **Paths** with `pathlib.Path`, built from `REPO_ROOT` — never hardcoded
  absolute paths.
- **Stage entry points** take the run folder and read/write the contract
  files, so each stage can be run and tested on its own:

  ```python
  async def run(run_dir: Path) -> None:
      """Read findings.json, add confidence fields, write it back."""
  ```

- **Async** for anything that calls Claude (`await llm.call(...)`); plain
  functions for pure data work.
- **Type hints** on stage entry points; optional elsewhere.
- **Docstrings** on every public function: what it does and which contract
  files it reads/writes. Comments explain *why*, not *what*.
- **Errors:** let exceptions propagate; catch only where you can recover or
  must report (e.g. tool results back to Claude). No bare `except:`.
- **Progress output:** `logging.getLogger(__name__)`, not `print`, so the UI
  can show it.

## Prompts

- Every agent prompt lives in `pharmer/prompts/<agent>.md`, never inline in
  Python strings. Load it once at import time.
- Prompt files are part of the cached prefix: **no dates, run IDs, or other
  values that change between runs** inside them. Put run-specific content in
  the user message instead.
- Shared rules all hunters follow (closed-book citation, verbatim quotes,
  leave `confidence` null) go in `prompts/hunter_rules.md`, placed first in
  every hunter's system prompt so the hunters share one cached prefix.

## Data and numbers

- No fact from model memory: every number and quote comes from the corpus
  (`chunks.jsonl`, `facts.jsonl`).
- Monetary values carry their `unit` and `currency` fields; never store a
  bare number. Conversion happens only in `quant.py`.
- Dates are ISO 8601 (`2026-06-30`); timestamps are UTC.
- Write JSON with `json.dump(obj, fh, indent=2, ensure_ascii=False)`; JSONL
  files get one compact object per line.

## Tests

- `tests/test_<module>.py` with `unittest`; run with
  `.venv/bin/python -m unittest discover tests`.
- **Tests never call the real API** — use a fake client as in
  `tests/test_llm.py`. Real-API checks are manual and get a `test` entry in
  `docs/build-log.md`.
- Small fixtures (a few chunks, one finding) live in `tests/fixtures/`.
