---
title: 'Dedup: group leads by fact anchor, LLM judge within groups'
type: 'feature'
created: '2026-10-17'
status: 'done'
baseline_commit: '44f1c86b467b3642a4fae247a0009919c9b18194'
route: 'dispatch'
review_loop_iteration: 0
context:
  - '{project-root}/docs/contracts.md'
  - '{project-root}/docs/conventions.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Hunters write `leads.json` (Contract 2 Findings, one reasoning each), but nothing collapses
leads that share a fact anchor, so the Fact Checker/Quant would verify the same fact several times
(architecture §4.2). This is step 1 of Partner B's build order and unblocks Partner A.

**Approach:** `pharmer/dedup.py` `async run(run_dir)` groups leads in code by a normalized anchor key;
groups of 2+ go to a Haiku judge (`llm.call`, structured output, prompt in
`pharmer/prompts/dedup_judge.md`) that partitions the reasonings into same-reasoning clusters and says
whether clusters contradict. Writes `findings.json` with renumbered ids.

## Boundaries & Constraints

**Always:** Every Claude call via `llm.call(model=llm.HAIKU, stage="dedup", agent="dedup_judge")`;
system prompt byte-identical across calls, group content only in the user message. No reasoning,
citation, or contributing hunter is ever dropped. Groups of one make no API call. Output order is
deterministic (first appearance in `leads.json`).
Decision (hunter credit, option A): `hunter` stays the earliest contributor; a new
`reasonings[].hunters` list names every contributor in order. The contracts.md edit is flagged for
Partner A's sign-off in the PR.

**Never:** Real API calls in tests. Touch Fact Checker/Quant/scoring/other hunters. Let the judge
write new claim text (merged claim = earliest member's claim, verbatim). Merge across anchor keys.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Singleton | lead with unique key | one Finding, 1 reasoning, `F-00n-a`, lead set | no API call |
| Duplicate | 2 leads, same key, judge `[[1,2]]` | 1 reasoning; citations unioned (dedup by chunk_id+quote); `hunters` lists both | N/A |
| Composite | 2 leads, judge `[[1],[2]]`, not contested | 2 reasonings `-a`,`-b`; `contested: false` | N/A |
| Contested | judge `[[1],[2]]`, `contested: true` | `contested: true`; `"contested"` in every reasoning's flags | N/A |
| Bad judge reply | refusal / max_tokens / non-JSON / not an exact partition of 1..N / contested with one cluster | keep all reasonings separate (composite), not contested | log warning, continue |
| Empty | `leads.json` is `[]` | `findings.json` is `[]` | N/A |

</frozen-after-approval>

## Code Map

- `pharmer/llm.py` -- `call()`, `HAIKU`; params pass through (`system`, `messages`, `output_config`).
  Haiku gets no fallback beta. Do not change.
- `pharmer/hunters/__init__.py` -- writes `leads.json`: ids `F-nnn` / `F-nnn-a`, `lead_reasoning_id`
  null, `contested` false. Do not change.
- `pharmer/hunters/harness.py` `build_finding` -- exact Finding/reasoning shape to preserve.
- `tests/test_llm.py` -- `FakeClient(messages)`, `fake_message(stop_reason, content)`; swap via
  `llm._client`, redirect `llm.LOG_PATH` (pattern in `tests/test_hunters.py` `HunterTestCase`).
- `docs/contracts.md` Contract 2 -- add `hunters` field + new leads.json section (both "proposed").

## Tasks & Acceptance

**Execution:**
- [x] `pharmer/prompts/dedup_judge.md` -- judge rules: same reasoning = same causal story about the
  anchor (wording may differ); different = distinct cause/implication; contested = clusters cannot
  both be true; output a partition with 1-based indices.
- [x] `pharmer/dedup.py` -- `anchor_key`, grouping, `_judge` (structured output schema
  `{clusters: int[][], contested: bool, reason: str}`, validation, fallback), merge, renumber, `run`.
  Groups judged concurrently with `asyncio.gather`. Anchor of a group = earliest lead's anchor; warn
  if `value` differs inside a group. `is_label_finding` = any member's.
- [x] `tests/fixtures/dedup_leads.json` -- 10 leads, interleaved: 2 singletons, a duplicate pair
  (overlapping citation), a composite pair whose keys differ only in case/whitespace/suffix, a
  contested pair, and a pair that gets the bad reply (refused in one test, malformed in another).
- [x] `tests/test_dedup.py` -- every matrix row, key normalization, byte-identical system prompt,
  call count = number of 2+ groups, ids renumbered.
- [x] `docs/contracts.md` -- per Open Question decision, plus drafted leads.json entry, marked
  pending Partner A.
- [x] `docs/build-log.md` -- append decision (normalization, fallback, lead choice) + test entries.

**Acceptance Criteria:**
- Given the fixture, when `run` is called with a fake client, then `findings.json` matches the matrix
  and ids are `F-001…` in first-appearance order with reasoning letters `a, b, …`.
- Given any finding, then `lead_reasoning_id` equals its first reasoning's id (the earliest-raised
  reasoning; scoring later ranks by strongest).

## Design Notes

Anchor key normalization (moderate): `metric` casefold, spaces/hyphens → `_`; `entity` casefold,
punctuation dropped, whitespace collapsed, trailing corporate suffix (`a/s`, `inc`, `plc`, `ltd`,
`ag`, `sa`, `corp`, `co`) removed; `fiscal_period` uppercased, whitespace collapsed. No period
mapping (`H1 2026` ≠ `Q2 2026` — Contract 1 says match on `fiscal_period` as labelled). Value is not
in the key: a value mismatch is the Fact Checker's job.

Judge failure falls back to "all separate", the only choice that loses nothing; the cost is a possible
duplicate row, logged. An `APIError` on one judge call gets the same fallback (amended in review: one
failed request must not cost every group its findings; hunters isolate failures the same way).

## Verification

**Commands:**
- `.venv/bin/python -m unittest discover tests` -- expected: all pass, no network.
