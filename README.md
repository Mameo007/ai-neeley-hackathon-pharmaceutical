# Pharmer Research

> 🏆 **Winner — Neeley AI Forward: AI Innovation Competition (Advanced Track), October 2026.**
> Case: *"The Fine Print: Find the Story, Then Make the Call."*

**Pharmer** reads a pharmaceutical company's own SEC filings, finds five material,
non-obvious findings that the headlines miss, lets an analyst pick the one that matters most
(the *Sharpest Needle*), and writes a research note as a PDF: a product comparison against
rivals, a verdict, and one concrete move for the CEO.

It is a reusable pipeline, not a one-off answer: point it at a different company and it runs
again. Every number and quote in the output traces back to a page of a filing.

## How it works

```
SEC filings ─► Librarian ─► 6 hunters ─► Dedup ─► Fact Checker ─► Quant ─► scoring
 (EDGAR,       chunks with   look for      merge    quote must    size each   materiality
  XBRL)        page numbers  findings      repeats  match word    finding     × obscurity
                                                    for word      in Python
                                                                      │
              PDF report ◄─ CEO Move ◄─ Synthesizer ◄─ Comparison ◄─ ⏸ analyst picks
                                                                      the Sharpest Needle
```

- **Hunters** (Claude Opus) each read the filings from one angle: Redliner (what changed
  between filings), Table Archaeologist, Footnote Miner, Gross-to-Net, Label Lawyer, and
  Channel Checker. They search and read filing chunks through tools; nothing is pasted into
  a chat.
- **Checks** (Claude Haiku + code) merge duplicates, confirm each quote supports its claim,
  and size each finding. The model picks a formula; Python does the arithmetic and ties every
  number to a second source (XBRL).
- **A human stays in the loop**: the analyst chooses the company, the Sharpest Needle and the
  product to compare.
- **Output** (Claude Opus): a product comparison using DailyMed labels, a verdict, and a CEO
  move that must be a decision, not more research. Every number in it must appear in that
  call's input.

## Why the output can be trusted

The rules that matter are enforced in code, not only asked for in prompts:

- A finding is rejected unless its quote matches the filing **word for word**.
- Agents may not use facts from model memory: *"Never use facts, numbers, dates, quotes or
  events from your own memory or training data, even if you are sure they are true."*
- The AI judge can only **lower** a finding's confidence, never raise it.
- The model never sets the sign of a number: it labels an amount a cost or a benefit, and
  Python applies the sign. (On the first live run, every write-down came back positive; a
  loss would have printed as a gain.)

We logged 33 cases of the AI being confidently wrong during the build, almost all caught by
going back to the primary source or by a second reviewer. They are in `docs/build-log.md`.

## How we built it

Two people built Pharmer in parallel, each with their own **Claude Code** session; **Codex**
independently reviewed the code. Partner A built the data and checking stages; Partner B
built the agents, the report and the UI. Because AI sessions don't share memory, the repo
was the shared memory: agreed data contracts and an append-only build log kept both sessions
consistent. The code passes 503 offline tests, and a full live run on Eli Lilly completed.

Stack: Claude Opus 5.5 and Haiku 5.5 · SEC EDGAR + XBRL · DailyMed · ECB FX rates ·
Streamlit · reportlab.

More detail:

| Document | What's in it |
|---|---|
| [`docs/how-we-did-it.pdf`](docs/how-we-did-it.pdf) | One-page write-up: tools, workflow, key prompts, where the AI was wrong, what we'd change |
| [`docs/architecture.md`](docs/architecture.md) | The design and why it is that way |
| [`docs/contracts.md`](docs/contracts.md) | Data formats passed between stages |
| [`docs/build-log.md`](docs/build-log.md) | Decisions, AI mistakes caught, and test results, in order |
| [`docs/neeley_ai_forward_case_study.md`](docs/neeley_ai_forward_case_study.md) | The competition case |

## Limitations

- The Priced-In Detector and Peer Agent are not built yet, so the report does not say whether
  a finding is already priced in, and findings are not checked against rivals' filings.
- Quant's assumed rates are chosen by the model; two runs on the same evidence can pick
  different rates and change which finding is recommended.

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
