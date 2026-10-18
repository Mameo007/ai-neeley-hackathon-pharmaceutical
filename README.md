# ai-neeley-hackathon-pharmaceutical

**Pharmer Research** reads a pharma company's filings, finds five non-obvious findings, lets
an analyst name the Sharpest Needle, and writes a research note as a PDF. Design:
`docs/architecture.md`; data formats: `docs/contracts.md`.

## Setup (once)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env
```

Fill in `.env` (git-ignored; never commit it or paste the key anywhere):

| Variable | What it is |
|---|---|
| `ANTHROPIC_API_KEY` | Claude API key — needed for hunters, Dedup, Synthesizer, CEO Move |
| `PHARMER_PARTNER` | `A` or `B`, recorded on every row of `logs/api_usage.csv` |
| `PHARMER_SEC_UA` | Your real name and email, e.g. `"Jane Doe jane@example.com"` — SEC requires it; the live Librarian refuses to fetch without it |

## Start the app

```bash
.venv/bin/streamlit run app.py
```

Then open http://localhost:8501. To open an existing run directly:
`http://localhost:8501/?run=NVO-20261018-0230`.

### A run, step by step

1. **Start a new run** — pick a company and press **Start run**. For Novo Nordisk you can
   switch on **Use prebuilt corpus** to use `data/sample/NVO` instead of fetching filings from
   SEC (offline / demo fallback). The page updates by itself while the pipeline runs.
2. **Review** — when the run says *needs your pick*, read the selected findings: confidence
   badge, why each was selected (score = materiality × obscurity), and each quote with its
   filing and page.
3. **Pick** — choose the Sharpest Needle (the AI's Recommended one is pre-selected) and the
   product, then press **Write the report**.
4. **Download** — when the run says *done*, press **Download report (PDF)**.

Earlier runs are listed in the sidebar: open one to change the pick or download its PDF again.
Stages that are not built yet show as *skipped*; a stage that ran but produced nothing usable
shows as *degraded*, with the reason.

## Command line (same pipeline, no UI)

```bash
.venv/bin/python orchestrator.py start NVO [--corpus data/sample/NVO]
.venv/bin/python orchestrator.py finish runs/<run_id> --needle F-003 --product "Ozempic/Wegovy"
```

`start` stops at the human pause and prints the exact `finish` command. Each run's files,
including `report.pdf` and `pipeline.json` (stage status and timings), are in `runs/<run_id>/`.

## Deploy on Render

Create a Render Blueprint from this repository and select the branch containing
`render.yaml`. It configures a Free web service with no paid disk or database.
Enter `ANTHROPIC_API_KEY` and `PHARMER_SEC_UA` when prompted; keep secrets out of
Git. Anthropic API usage is billed separately from hosting.

Free services sleep after 15 minutes without inbound traffic and can take about
a minute to wake up. Runs, PDFs, and API logs are temporary: they are lost on
sleep, restart, or redeploy. Download reports before leaving the demo. Existing
local runs are not uploaded, and in-flight jobs stop when the service restarts.
Render includes 750 free instance hours per workspace per month. To avoid paid
overages, use a workspace without a payment method; usage limits then suspend
services or builds instead of billing. See https://render.com/docs/free.

The health endpoint is `/_stcore/health`. Local startup remains unchanged.

## Tests

```bash
.venv/bin/python -m unittest discover tests
```

Offline only — tests never call the Claude API. `scripts/api_smoke.py` is the one real-API
check (a few cents); see the handoff entry in `docs/build-log.md`.
