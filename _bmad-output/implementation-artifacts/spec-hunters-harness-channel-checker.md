---
title: 'Hunter harness and Channel Checker hunter'
type: 'feature'
created: '2026-10-17'
status: 'done'
baseline_commit: 'ce7cce3c953010b1a26b106f3680938aef29cca2'
route: 'dispatch'
review_loop_iteration: 0
context:
  - '{project-root}/docs/contracts.md'
  - '{project-root}/docs/conventions.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** The pipeline has a corpus (Contract 1) but nothing that reads it and proposes leads. Eight
hunters are planned (architecture §4.1); none exist, and they must share one cached prefix.

**Approach:** One shared harness in `pharmer/hunters/` gives any hunter closed-book tools over the
corpus, runs it through `llm.run_agent()` with `llm.OPUS` and a turn cap, validates its submitted
leads, and writes them in the Contract 2 Finding shape. The first hunter, Channel Checker, looks for
inventory or receivables growing faster than revenue.

## Boundaries & Constraints

**Always:** System prompt (`prompts/hunter_rules.md`) and tool list are module constants, byte-identical
for every hunter; the hunter's own brief, run_id and corpus overview go in the first user message.
Every citation's `chunk_id` exists in `chunks.jsonl` and its `quote` is an exact substring of that
chunk's `text`. Hunters leave `confidence` null. All Claude calls via `llm.run_agent()`.

**Never:** Dedup, Fact Checker, confidence, impact, scoring, the other 7 hunters. No real API calls
in tests. No writes into `data/sample/`. No contract changes.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Happy path | run_dir with Contract 1 files; model searches, reads, submits valid leads | `run_dir/leads.json` = list of Findings, ids `F-001…`, one reasoning each, `hunter: channel_checker` | N/A |
| Bad citation | `submit_leads` with unknown chunk_id or non-verbatim quote | Nothing recorded; tool returns `is_error` listing each bad citation | Model may resubmit within the turn cap |
| No leads | Model submits `[]` or never submits | `leads.json` = `[]` for that hunter | Never-submitted logs a warning |
| Turn cap hit | Loop ends with `stop_reason: tool_use` | Last valid submission (if any) is kept | Warning logged |
| Unknown tool / bad args | e.g. `read_chunks` with unknown id | Error text returned to the model | `run_agent` already wraps handler exceptions |

</frozen-after-approval>

## Code Map

- `pharmer/llm.py` -- `run_agent(run_id, stage, agent, tools, tool_handlers, messages, model, max_turns, **params)`; handlers are `async fn(input) -> str`; raise to report `is_error`. Do not change.
- `tests/test_llm.py` -- `fake_message`, `FakeStream`, `FakeClient` pattern to copy; swap `llm._client` and `llm.LOG_PATH` in setUp/tearDown.
- `data/sample/NVO/` -- 277 chunks (~220 KB text, too big to inline), 15 facts (`inventory`, `trade_receivables`, `revenue`, … FY2023–FY2025), 3-doc manifest. Read-only.
- `docs/contracts.md` -- Contract 1 fields; Contract 2 Finding shape + hunter-owned fields.

## Tasks & Acceptance

**Execution:**
- [x] `pharmer/hunters/harness.py` -- `load_corpus(run_dir)`; pure search/read/facts functions; `TOOLS` constant (`search_chunks`, `read_chunks`, `get_facts`, `submit_leads`); `SYSTEM` from `hunter_rules.md` with an explicit cache breakpoint; `validate_leads()`; `async run_hunter(run_dir, corpus, name) -> list[dict]` building Finding-shaped leads; `MAX_TURNS = 15`.
- [x] `pharmer/hunters/__init__.py` -- `HUNTERS = ["channel_checker"]`; `async run(run_dir: Path) -> None`: load corpus once, `asyncio.gather` all hunters, number `F-001…` in HUNTERS order, write `leads.json`.
- [x] `pharmer/hunters/channel_checker.py` -- `NAME`, loads `prompts/channel_checker.md` as `BRIEF`.
- [x] `pharmer/prompts/hunter_rules.md` -- closed-book rules, tool usage, submission format, no confidence, no dates/IDs.
- [x] `pharmer/prompts/channel_checker.md` -- what to compare, like-for-like periods, anchor choice, 0–3 leads.
- [x] `tests/test_hunters.py` -- offline: tools over the NVO sample, validation, end-to-end with FakeClient, byte-identical system/tools.
- [x] `docs/build-log.md` -- append decision + test entries.

**Acceptance Criteria:**
- Given two hunters' requests, when compared, then `system` and `tools` are equal byte-for-byte and the brief appears only in `messages`.
- Given the NVO sample copied to a temp run_dir and a FakeClient scripted search→read→submit→end_turn, when `run()` is awaited, then `leads.json` validates against Contract 2 hunter fields and every citation is verbatim.
- Given the suite, when run, then no network call is made.

## Implementation Notes

- Implemented directly in the main session (no implementation subagent) to save tokens; review used the three reviewer subagents.
- Deviations from the Code Map, kept because they are simpler: `run_hunter(run_id, corpus, hunter, brief)` (runner loads the corpus once) and `HUNTERS = [channel_checker]` holds modules exposing `NAME`/`BRIEF`.
- `tool_results()` in tests indexes the live messages list by turn because `run_agent` appends to the list it sends.
- `docs/conventions.md` Prompts section updated: the hunter system prompt is only `hunter_rules.md`; briefs go in the first user message.

## Spec Change Log

## Review Triage Log

| # | Finding | Verdict | Evidence / route |
|---|---|---|---|
| 1 | gather without return_exceptions aborts the run on one hunter failure | medium | llm.call re-raises → patch `_run_safely` + test |
| 2 | Prompts said `H1 2026`; corpus docs are `Q2 2026`, Dedup keys split | medium | manifest → patch prompts: cited doc's fiscal_period |
| 3 | anchor.entity free text | medium | grouping key → patch: use the Company: line |
| 4 | Trivial quotes pass; anchor.value untied to quotes | medium | `"Trade"` passed → patch: a quote must contain anchor.value |
| 5 | Derived numbers can't be quoted | low | patch: allowed when inputs cited |
| 6 | Turn budget unknown to model | medium | patch: stated in hunter_rules.md |
| 7 | leads.json not in contracts.md | medium | needs both partners → defer |
| 8 | conventions.md contradicts code | medium | patch |
| 9 | Shared prefix may be below min cacheable length | maybe-false | needs real usage log → defer |
| 10 | search counts duplicate/short terms | low | patch: dedupe, drop <3 chars; substring kept for plurals |
| 11 | get_facts unbounded | low | defer |
| 12 | read_chunks([]) unclear error | low | patch: schema minItems 1 |
| 13 | Stale submission silently kept | low | patch: error says earlier submission stands |
| 14 | 0–3 lead cap unenforced | low | rejected: per-hunter prose |
| 15 | bool("false") → label finding | low | patch: must be bool |
| 16 | Non-dict items → AttributeError | low | rejected: already returned as is_error |
| 17 | Duplicate chunk_ids | false | Contract 1: chunk_id unique |
| 18 | Non-atomic leads.json write | low | rejected: rare, re-runnable |
| 19 | read_text without encoding | low | patch |
| 20 | Runner set lead_reasoning_id (Dedup's field) | low | patch: left null |
| 21 | run_hunter / HUNTERS differ from spec | low | rejected: fix is editing the spec |
| 22 | Load errors lack context | low | rejected: traceback names the path |
| 23 | Run test skips anchor/claim/citations/label | medium | patch: exact asserts |
| 24 | F-nnn numbering tested with one lead | medium | patch: two-lead test |
| 25 | Empty quote untested | low | patch |
| 26 | Build log said 15 tests (was 13) | low | patch: new append-only entry |
| 27 | Brief example mirrors the real Novo figure | medium | patch: neutral wording |
| 28 | `[Claude]` log tag; test coupling; empty-manifest fallback | low | rejected: negligible |

## Design Notes

Leads are submitted through a `submit_leads` tool (not parsed from free text) so the harness can
reject bad citations and let the model fix them. The model supplies `anchor`, `claim`,
`citations`, `is_label_finding`; the harness fills `ticker`, ids, `hunter`, nulls for later stages,
`status: candidate`. One `leads.json` for the run (Dedup input); final ids come from Dedup.

## Verification

**Commands:**
- `.venv/bin/python -m unittest discover tests` -- expected: all pass, offline.
