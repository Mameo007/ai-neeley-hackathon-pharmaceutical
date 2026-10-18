# Pharmer — Neeley AI Forward hackathon build

Two people build this repo in parallel, each with their own Claude session.
Claude memory is per-machine, so **the repo is the shared memory**. Read these
before starting any task:

- `docs/architecture.md` — the agreed design (why things are the way they are)
- `docs/contracts.md` — the two data formats; code must match them exactly
- `docs/build-log.md` — shared running log; read the latest entries first

## Ownership

- **Partner A — data + checks:** Librarian, Fact Checker, Quant.
- **Partner B — agents + output:** hunters, Dedup, Priced-In Detector,
  scoring/selection, Peer Agent, Synthesizer, CEO Move, PDF report, UI.
- Stay inside your area. Changing `docs/contracts.md` needs both partners.

## Rules

- `git pull` before starting work; commit and push small changes often.
- Append to `docs/build-log.md` (never edit old entries) whenever you:
  make a decision the code doesn't show, catch the AI being confidently
  wrong, or finish a test. These entries feed the pitch deck and the
  How-we-did-it report.
- No fact from model memory: every number and quote comes from the corpus.
- `uv` is not installed on the main dev machine; use `python3`.
