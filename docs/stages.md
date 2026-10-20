# Pipeline stages: what each one does, as built

`docs/architecture.md` is the design target. This file describes what each
pipeline stage **actually does in the code on `main`** (checked 2026-10-19):
what it reads, what it writes, which model it uses, how it works and what part
of the design it does not do yet. File names refer to `runs/<run_id>/`; shapes
are in `docs/contracts.md`.

## Terms

- **Agent** means a **hunter**, and only a hunter. A hunter is an Opus tool loop
  (`llm.run_agent()`): it decides which chunks to search and read, over up to 15
  turns, and when to submit leads. The hunters are the only agents in the code.
- **Stage** means any step of the pipeline (`pipeline.json` records one entry
  per stage). Every stage other than the hunters is a Python module that does
  one of two things:
  - runs code only (Librarian, Scoring, Exec Report), or
  - runs code around fixed, single Claude calls (`llm.call()`) with no tools
    and no loop: a judge (Dedup, Fact Checker), a formula choice (Quant) or one
    piece of writing (Comparison Builder, Synthesizer, CEO Move). Code decides
    what goes into each call and checks what comes back.
- `docs/architecture.md` uses the older wording ("Quant agent", "CEO Move
  agent", "Peer Agent"), and the UI labels its stage cards "agent cards". Read
  those as stage names; they are not agents in the sense above.

## The run in one picture

```
orchestrator.py start TICKER [--corpus data/sample/NVO]
  corpus        Librarian (live SEC) or a copy of a prebuilt corpus
  hunters       6 hunters in parallel          -> leads.json
  dedup         merge leads per fact anchor    -> findings.json
  fact_checker  confidence, trend, flags       -> findings.json (+ fact_check_report.json)
  quant         $ / EPS impact                 -> findings.json (+ quant_report.json)
  priced_in     DISABLED (not built)
  scoring       final 5 + Recommended          -> findings.json
  peer          DISABLED (not built)
  scoring_after_peer  (same scoring, idempotent)
        -- human pause: Sharpest Needle, product, rivals -> run.json --
orchestrator.py finish RUN_DIR [--needle F-nnn] [--product X] [--rivals A,B]
  comparison    5-dimension grid               -> comparison.json (+ comparison_sources.json)
  synthesizer   one-sentence verdict           -> synthesis.json
  ceo_move      one strategic move             -> ceo_move.json
  report        PDF research note              -> report.pdf
```

The stages run **one after another**: each stage finishes all findings before the
next one starts (no per-candidate pipelining yet). `pipeline.json` records every
stage's status (`ok`, `degraded`, `skipped`, `disabled`, `failed`) and timing,
and the UI polls it.

| Stage | Module | Kind | Model | Required? |
|---|---|---|---|---|
| Orchestrator | `orchestrator.py` | runner, not a stage | none | n/a |
| Librarian | `pharmer/librarian.py` | code | none | yes |
| 6 hunters | `pharmer/hunters/` | **agents** (tool loop) | Opus | yes |
| Dedup / merge | `pharmer/dedup.py` | code + one judge call per shared anchor | Haiku | yes |
| Fact Checker | `pharmer/fact_checker.py` | code + one judge call per reasoning | Haiku | no |
| Quant | `pharmer/quant.py` | code + one formula call per reasoning | Haiku | no |
| Priced-In Detector | not built | n/a | n/a | disabled |
| Scoring / selection | `pharmer/scoring.py` | code | none | yes |
| Peer Agent | not built | n/a | n/a | disabled |
| Comparison Builder | `pharmer/comparison.py` | code + one call per product column | Opus | no |
| Synthesizer | `pharmer/synthesizer.py` | code + one call | Opus | yes |
| CEO Move | `pharmer/ceo_move.py` | code + one call | Opus | yes |
| Exec Report | `pharmer/report.py` | code | none | yes |
| UI | `app.py` (Streamlit) | front end, not a stage | none | n/a |

"Required" means that if the stage fails, the phase stops. When an optional stage
fails, the run is marked and goes on. Every Claude call goes through
`pharmer/llm.py`, which handles caching, the refusal fallback and the usage log.

---

## Orchestrator (`orchestrator.py`)

**Job:** runs the pipeline in two phases, split at the human pause.

- `start` checks the ticker against `librarian.CIKS`, creates the run folder and
  runs the corpus stage and every stage up to the second scoring pass. Then it
  stops at phase `awaiting_human` and prints the selected findings.
- `finish` writes the analyst's choices (`sharpest_needle`, `product`, `rivals`)
  into `run.json` and clears the old finish outputs, so a re-pick never leaves a
  stale report. Then it runs Comparison → Synthesizer → CEO Move → Report.
  `finish` can be run again with a different pick.
- A stage module that does not exist is recorded as `skipped: not built`. A stage
  named in `DISABLED_STAGES` (Priced-In, Peer) is never imported and is recorded
  as `disabled` with a reason.
- A stage that returns but produced nothing usable (no leads; a verdict that
  failed) is recorded as `degraded`.

## Librarian (`pharmer/librarian.py`): stage 0

**Job:** builds the closed-book corpus that the hunters and every later stage
cite.

- **Reads:** SEC EDGAR, live: the submissions list, filing documents and XBRL
  `companyfacts`. It supports all 10 shortlist tickers (`CIKS`). The SEC user
  agent comes from `PHARMER_SEC_UA`, and requests stay under 10 per second.
- **Writes:** `manifest.json` (one row per document), `chunks.jsonl` (the only
  thing hunters may cite: chunk ID, doc, page, section, text of up to about 2,000
  characters) and `facts.jsonl` (XBRL facts, keyed through `CONCEPT_MAP`, in
  millions). It also writes the audit sidecar `librarian_report.json`.
- **How:** picks the 2 latest annual reports, the latest quarterly report and the
  prior-year report for the same quarter. It branches on filer type: 10-K/10-Q
  for domestic filers, 20-F/6-K for foreign ones. For a 20-F it follows the
  annual-report exhibit incorporated by reference. It finds the financial 6-K by
  scoring the content of each candidate, not by file name. Quarters are matched
  by fiscal period, not by calendar date. Pages come from the document's own
  page numbers where it prints them; otherwise they are inferred, and the
  sidecar records which.
- **Prebuilt corpus:** `scripts/build_sample_corpus.py` builds
  `data/sample/NVO`. This is Novo's filings plus the DailyMed labels of Ozempic
  and Mounjaro, chosen by the §4.0 label rule. The script stops instead of
  auto-picking when zero or several SPLs survive. `start --corpus` (the
  "prebuilt filings" toggle in the UI) copies this folder in place of a live
  fetch.
- **Not done yet:** a live run fetches no DailyMed labels, Orange or Purple Book
  data, IR Excel workbooks, transcripts or rival filings (all are listed under
  `deferred` in `librarian_report.json`). As a result, a live run has no label
  documents, so Label Lawyer and the Comparison Builder only have data on the
  prebuilt NVO corpus. A foreign filer's quarterly figures have no second source
  for the number tie.

## Hunter swarm (`pharmer/hunters/`): stage 1

The hunters are the project's agents: each one chooses its own searches and
reads and decides when it has enough to submit.

**Shared harness (`harness.py`):** every hunter runs the same Opus tool loop,
with a byte-identical system prompt (`prompts/hunter_rules.md`) and the same
tools, so all hunters share one cached prefix. A hunter differs only in its
brief (`prompts/<hunter>.md`), which is sent in the first user message.

- **Tools:** `search_chunks` (keyword search, snippets only), `read_chunks`
  (full chunk text), `get_facts` (XBRL facts: context, not citations) and
  `submit_leads`.
- **Closed book:** facts from model memory are forbidden. `submit_leads` rejects
  a lead whose `chunk_id` does not exist, whose quote is not an exact substring
  of that chunk, or whose `anchor.value` appears in no quote. The hunter gets the
  problems back and resubmits.
- **Limits:** at most 15 turns per hunter (`MAX_TURNS`, also stated in the
  rules), and 0 to 3 leads per hunter. Hunters do not assign confidence.
- **Runner (`__init__.py`):** runs all hunters in parallel. One hunter's failure
  costs only its own leads. It writes `leads.json` (Contract 2 findings with one
  reasoning each, ids `F-001`…) in the order of the `HUNTERS` list.

| Hunter | What it looks for | How its brief makes it work |
|---|---|---|
| **Channel Checker** | Inventory or trade receivables growing faster than revenue (weak demand, pre-launch stocking, channel stuffing) | Gets the revenue, receivables and inventory trend. Compares balance sheets at the same point a year earlier and revenue over the same length of period. Cites the company's own explanation if one exists. |
| **Redliner** | Language drift: new, removed, hedged or re-quantified wording in risk factors, outlook and MD&A | Pairs each latest document with its earlier counterpart and compares the same section sentence by sentence. Must cite **both** the old and the new passage. |
| **Table Archaeologist** | Restated prior-period numbers; products or segments no longer broken out; merged or renamed rows | Compares the prior-period column of the latest table with the same period as first reported. Cites both table rows. |
| **Footnote Miner** | Provisions, litigation reserves, contingent liabilities, royalties, milestones and earn-outs, impairments, tax-rate changes | Compares each note with the earlier filing and sizes the item against a cited revenue or profit figure. |
| **Gross-to-Net Tracker** | Rebates, discounts, chargebacks and government deductions growing faster than sales | Computes deduction rates only from cited numbers, comparing like for like. |
| **Label Lawyer** | Product fine print: indication breadth, boxed warnings, contraindications, REMS, dose limits, adverse-reaction rates | Reads the company's own labels (`doc_type` `label`, matching ticker) and uses rival labels for contrast only. Anchors on a number printed in the label. Sets `is_label_finding: true` on every lead. Only the company-wide pass exists. |
| Call Decoder | not built | Needs transcripts. |
| Peer Contraster | not built | Needs rival filings. |

## Dedup / merge (`pharmer/dedup.py`): stage 2

**Job:** stops the same fact from being checked and scored twice, without
merging different stories about it.

- **Reads** `leads.json`. **Writes** `findings.json`.
- **How:** code groups leads by a normalized fact anchor (metric + entity +
  fiscal period). Only within a group of two or more leads, a Haiku judge splits
  the reasonings into clusters of "same story" and says whether any two
  **contradict** each other. Each cluster becomes one reasoning that credits
  every hunter behind it. A group becomes one finding, which is a composite row
  if it has more than one cluster. A contradiction sets `contested` on the
  finding and on its reasonings. If the judge's reply is unusable, the group's
  reasonings are kept separate.
- **Not done yet:** the "split" option (promoting one reasoning of a composite
  row to its own finding) is not implemented.

## Fact Checker (`pharmer/fact_checker.py`): stage 3

**Job:** re-verifies each reasoning; it does not discover new ones. This is the
only stage that sets `confidence`.

- **Reads** `findings.json` + corpus. **Writes** `confidence`,
  `confidence_reason`, `trend` and the flags `reversal` / `possibly_seasonal`
  into `findings.json`. It also writes the detail sidecar
  `fact_check_report.json`.
- **How (code, authoritative):** re-opens each cited chunk and re-matches the
  quote. Ties the anchor value to `facts.jsonl` at the precision the sources
  support (the number-tie rule). Searches the corpus, including comparative
  columns in filing tables, to build the **trend strip**. Flags a **reversal**
  when the anchor's recent direction flips by a meaningful margin. Runs the
  **like-for-like** check against the prior-year period; full-year figures are
  exempt from it.
- **How (model, can only lower):** one Haiku call per reasoning answers the
  question code cannot: does this evidence support this claim? If the judge is
  unavailable, the reasoning stays Medium, with "claim support unverified".
- **Verdict:** a failed quote or citation → Low. Every check passed → High.
  Otherwise Medium, with the reasons written out, for example "possibly
  seasonal".
- **Not done yet:** IR workbooks are not used as the second source, so a foreign
  filer's quarterly figure cannot tie and reaches Medium at most.

## Quant (`pharmer/quant.py`): stage 4

**Job:** turns each verified reasoning into an impact that can be recomputed.

- **Reads** `findings.json` + corpus. **Writes** only `reasonings[].impact`
  (`usd_m`, `eps_pct`, `formula`, `inputs`, `fx`). The full audit trail goes to
  `quant_report.json`.
- **How:** one Haiku call per Medium or High reasoning returns a formula, named
  inputs and a `direction` (`cost`/`benefit`). Python does the rest:
  - It resolves every input. A `fact` input takes the corpus value, never the
    model's. A `quote` input must be verbatim and contain exactly one whole
    printed figure. An `assumption` input needs a value and a justification.
  - It evaluates the formula through an AST whitelist (`+ - * /` and
    parentheses only) and applies the sign.
  - FX uses ECB reference rates: the period-average rate for flows and the
    period-end rate for balances. If no rate is available, `usd_m` is left null
    rather than guessed.
  - `eps_pct` needs a diluted share count for the matching period. An annual
    count is never used for a quarter.
- **Known limits:**
  - The assumed rates vary between runs. On the same LLY evidence, two runs
    recommended different findings.
  - Foreign quarterly findings get `eps_pct: null`, so they score null.
  - The report prints `usd_m` and `eps_pct`, but not the native-currency amount
    or the FX footnote that §4.4 asks for.

## Priced-In Detector: stage 5 (disabled)

Not built. Listed in `DISABLED_STAGES`. Scoring treats every reasoning as not
yet priced in (obscurity 1.0), so findings are ranked by materiality alone.
When no reasoning has a priced-in check, the report prints "Market pricing not
assessed" and gives no market view.

## Scoring / selection (`pharmer/scoring.py`): stage 6

**Job:** picks the AI's final 5 and tags one as Recommended. Pure code with no
Claude call; re-running it gives the same result.

- **Reads and rewrites** `findings.json`: `reasonings[].score` and `.selected`,
  plus the finding-level `status` (`selected`/`verified`/`dropped`) and
  `recommended`.
- **How:**
  1. **Gate:** only High or Medium reasonings are eligible.
  2. **Score:** each eligible reasoning is a ranking unit, scored |`eps_pct`| ×
     obscurity. A unit with no impact gets score null and ranks after every
     scored unit.
  3. **Sort:** findings not demoted by the Peer Agent come first, then by score.
     Ties are broken by confidence, then obscurity, then materiality.
  4. **Greedy fill:** units are taken in order, at most 2 per hunter, up to 5.
     Two reasonings of one anchor may both be selected.
  5. **Label guarantee:** if none of the 5 is a label finding, the lowest-ranked
     unit is swapped for the best eligible label finding.
  6. **Recommended:** the top unit's finding is tagged Recommended.
- **Not done yet:** there is no top-~10 cut, because the Peer Agent that would
  use it does not exist. There is also no targeted Label Lawyer retry: when no
  label finding exists, the stage logs it and the report prints a warning.

## Peer Agent: stage 7 (disabled)

Not built. Listed in `DISABLED_STAGES`. Scoring treats peer as null, so no
finding is demoted.

## Human pause: stage 8–9

In the UI (or with `finish --needle/--product/--rivals`), the analyst:

- names the Sharpest Needle from the selected findings. The Recommended one is
  pre-filled. If there is no pick, CEO Move uses the Recommended finding and
  marks it `recommended_fallback`.
- confirms a product from the case appendix (`data/products.json`). The dropdown
  starts at "Not confirmed"; there is no AI pre-selection yet.
- names rivals. This is possible from the CLI only; the UI shows the appendix
  rivals as text.

## Comparison Builder (`pharmer/comparison.py`): stage 11

**Job:** builds the 5-dimension grid of the product against its named rivals.
The grid holds only sourced facts and no interpretation.

- **Reads** `run.json` (`product`, `rivals`) + corpus. **Writes**
  `comparison.json` (`{"columns", "rows"}`) and the sidecar
  `comparison_sources.json` (the chunk and quote behind each cell, plus
  rejections and errors).
- **How:** one Opus call per product column, run in parallel. Each call sees
  only that product's label chunks plus up to 30 filing chunks that name it.
  Each call returns a value with a `chunk_id` and a verbatim quote for each of
  the 5 dimensions: approved uses, boxed warning/safety, dosing/convenience,
  patent/exclusivity, and pricing & gross-to-net. Code keeps a cell only when
  the chunk was sent to that column, the quote is verbatim, and every number in
  the value appears in the quote. Anything else becomes **"not disclosed"**. A
  failed call gets one retry; after that, its column is all "not disclosed".
- With no product named, it writes nothing, and the report says the table was
  not built. Patent/exclusivity is usually "not disclosed", because there is no
  Orange or Purple Book data. The product-specific Label Lawyer pass and the
  rival fetch that §6 puts before this stage are not built.

## Synthesizer (`pharmer/synthesizer.py`): stage 12

**Job:** writes the one-sentence verdict on what limits growth and whether the
market has priced it in.

- **Reads** the selected reasonings in `findings.json`, `comparison.json` if it
  exists, and the product in `run.json`. **Writes** `synthesis.json`.
- **How:** one Opus call. Code then checks the reply:
  - every citation refers to a selected reasoning;
  - it is exactly one sentence;
  - it contains no number that is absent from the input.

  A failed call or check gets one retry with the problems listed. After that,
  the stage writes `status: failed` instead of raising, including when no API
  key is set. The ask-and-retry and number-check helpers are shared with CEO
  Move and the Comparison Builder.

## CEO Move (`pharmer/ceo_move.py`): stage 13

**Job:** gives one concrete strategic move that answers the Sharpest Needle.

- **Reads** the selected findings, the needle from `run.json` and
  `synthesis.json`. **Writes** `ceo_move.json` with the action, timeframe,
  trade-off, downside if wrong, why it works and citations.
- **How:** one Opus call. Code checks that:
  - the citations are valid and include the needle;
  - the action is real (not "investigate further");
  - the reply contains no number absent from the input, other than in the
    timeframe.

  It gets one retry, then writes a visible failed state. A needle that is not one
  of the selected findings fails with no call.
- **Not done yet:** there are no budget or rejected-alternatives fields, which
  the case asks for. Adding them changes the output shape, so both partners
  must agree.

## Exec Report (`pharmer/report.py`): stage 14

**Job:** turns the run into one PHARMER RESEARCH note in PDF.

- **Reads** `findings.json`, `run.json`, `synthesis.json`, `ceo_move.json`,
  `comparison.json`, `manifest.json` and `chunks.jsonl`. **Writes**
  `report.pdf`.
- **How:** `build_model` collects the report as plain data, and `render_pdf`
  lays it out with reportlab. There is no Claude call: every number and quote
  comes from the run files. The note has these sections:
  - masthead and tagline
  - Executive Overview
  - thesis with the Sharpest Needle
  - the numbered findings, with confidence badges and labels for contested,
    possibly seasonal and label/product findings
  - the comparison table
  - the "24-Month Strategic Move for the CEO" callout
  - Coverage and limitations
  - plain-text footnotes, giving the filing name and page with no hyperlinks

  A missing upstream file becomes a warning in the report; only a missing
  `findings.json` raises an error.
- **Delivery:** download only, from the UI. Email is not built.

## UI (`app.py`)

A Streamlit app, deployed on Render (`render.yaml`):

- **Company grid:** the 10 shortlist tiles with the case's starter threads.
- **Stage cards** (labelled "agent cards" in the code): one card per stage,
  filled from `pipeline.json`. Disabled stages are hidden.
- **Review screen:** the selected findings and the analyst's pick (needle and
  product).
- **Result screen:** the needle headline, the impact number (never computed by
  the UI), the verdict, the verbatim quote and the CEO card.
- **Errors:** each error is one plain sentence with one recovery button.

The UI runs `orchestrator.prepare_*` / `run_*_stages` in a background job.
