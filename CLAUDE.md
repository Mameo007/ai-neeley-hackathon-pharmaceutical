# Pharmer — Neeley AI Forward hackathon build

Two people build this repo in parallel, each with their own Claude session.
Claude memory is per-machine, so **the repo is the shared memory**. Read these
before starting any task:

- `docs/architecture.md` — the agreed design (why things are the way they are)
- `docs/contracts.md` — the data formats; code must match them exactly
- `docs/conventions.md` — naming, layout, code style, prompts, tests
- `docs/build-log.md` — shared running log; read the latest entries first

## Ownership

- **Partner A — data + checks:** Librarian, Fact Checker, Quant.
- **Partner B — agents + output:** hunters, Dedup, Priced-In Detector,
  scoring/selection, Peer Agent, Synthesizer, CEO Move, PDF report, UI.
- Stay inside your area. Changing `docs/contracts.md` needs both partners.

## Git & GitHub

- **Never commit to `main` directly.** Every piece of work starts on a new
  branch from an up-to-date `main`:
  `git checkout main && git pull && git checkout -b <branch>`
- **Branch names:** `<type>/<area>-<short-description>`, lowercase, words
  joined by hyphens.
  - `type` ∈ `feat` (new functionality), `fix` (bug fix), `docs`
    (documentation only), `test` (tests only), `chore` (config, tooling,
    renames)
  - `area` = the pipeline part from `docs/architecture.md`: `librarian`,
    `fact-checker`, `quant`, `hunters`, `dedup`, `priced-in`, `scoring`,
    `peer`, `comparison`, `synthesizer`, `ceo-move`, `report`, `ui`,
    `contracts`, `repo`
  - Examples: `feat/hunters-redliner`, `fix/librarian-6k-detection`,
    `docs/contracts-finding-flags`, `chore/repo-github-rules`
- One branch = one small, focused change. Push often
  (`git push -u origin <branch>`).
- **Merge through a pull request** to `main`, filled in from
  `.github/pull_request_template.md`. Ask the partner to review when the PR
  touches `docs/contracts.md` or the other partner's area; otherwise the
  author may merge their own PR. Delete the branch after merging.
- Before opening a PR: `git pull origin main` into the branch and resolve
  conflicts locally.
- Commit messages: one imperative summary line ("Add Redliner hunter
  prompt"), optional body explaining why.

## Claude API

- The API account belongs to someone else. Put the key in `.env` (copy
  `.env.example`; `.env` is git-ignored) with `PHARMER_PARTNER=A` or `B`.
  **Never commit the key or paste it into a chat.**
- **Every Claude call goes through `pharmer/llm.py`** (`call()` or
  `run_agent()`), never the SDK directly. It applies prompt caching and the
  refusal fallback, and logs usage per call to `logs/api_usage.csv`
  (`docs/contracts.md`, Contract 3).
- Models: `llm.OPUS` for hunters, Synthesizer, CEO Move; `llm.HAIKU` for
  mechanical stages (Dedup judge, Fact Checker, Quant).
- Keep system prompts and tool lists byte-identical across calls (no
  timestamps or run IDs in them) so the cached prefix is reused.
- Setup: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`.
  Tests: `.venv/bin/python -m unittest discover tests`.

## Rules

- `git pull` on `main` before starting new work; commit small changes often.
- Append to `docs/build-log.md` (never edit old entries) whenever you:
  make a decision the code doesn't show, catch the AI being confidently
  wrong, or finish a test. These entries feed the pitch deck and the
  How-we-did-it report.
- No fact from model memory: every number and quote comes from the corpus.
- `uv` is not installed on the main dev machine; use `python3`.
