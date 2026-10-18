---
title: 'Synthesizer + CEO Move (Partner B build step 3)'
type: 'feature'
created: '2026-10-17'
status: 'done'
baseline_commit: 'f56166d63632f6cf7ec822d9a9c3b1329f0c196a'
route: 'dispatch'
review_loop_iteration: 0
context:
  - '{project-root}/docs/contracts.md'
  - '{project-root}/docs/conventions.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** After scoring picks the final 5 and the human names the Sharpest Needle, nothing writes the case's growth-limiter / priced-in verdict (§4.8) or the one CEO move (§4.9), so the report has no thesis close or boxed recommendation.

**Approach:** two stage modules, `pharmer/synthesizer.py` and `pharmer/ceo_move.py` (`async run(run_dir)`), one Opus call each through `llm.call` with json_schema structured output; code validates every reply, retries once with the problems listed, and otherwise writes a visible failed state. Outputs `synthesis.json` and `ceo_move.json` (proposed contract, flagged for Partner A).

## Boundaries & Constraints

**Always:**
- Prompts in `pharmer/prompts/synthesizer.md` / `ceo_move.md`, byte-identical; all run data in the user message. They forbid any number, name or claim not in the supplied input, and require quoting numbers as written.
- Input = selected reasonings only (`reasonings[].selected`), grouped under their finding (anchor, flags, contested, is_label_finding, recommended, claim, confidence, impact eps_pct/usd_m, priced_in hits, peer status, quotes). The user message adds no numbers of its own (ids and data only, no "Finding 1" counters).
- Synthesizer also gets `comparison.json` verbatim if it exists (proposed name) and `run.json` `product` if set. No grid → verdict from findings alone, `grid_used: false`, prompt told the grid is unavailable.
- Sharpest Needle (decided Q1 → B): `run.json` missing, unreadable or without `sharpest_needle` → fall back to the Recommended finding, `sharpest_needle_source: "recommended_fallback"` + warning; a `sharpest_needle` that is not a finding holding a selected reasoning → failed state, no API call (a human error to see and fix).
- CEO Move gets the selected findings, the Sharpest Needle id, and the verdict sentence. Synthesis missing or failed → proceeds without it, `verdict_used: false`.
- Validation (code): every citation is a selected reasoning id or a finding id holding one; ≥1 citation; CEO Move cites the needle (its finding id or one of its selected reasoning ids); verdict is one sentence (no `[.!?]` followed by whitespace + capital); no empty strings; action not starting with a non-action verb (investigate, monitor, study, review, explore, assess, evaluate, consider, analyze, research, look into, continue, wait, gather); every number in the output text (ids stripped, commas/sign/% normalized) appears in that call's user message — `timeframe` exempt.
- Failure rule: an API error, refusal, `max_tokens`, non-JSON or failed validation → one retry with the problems appended to the user message; second failure → write the file with `status: "failed"`, `error`, content fields null, a warning log; never raise. Nothing selected → failed state, no API call.
- Output shapes (JSON per conventions):
  - `synthesis.json`: `{status, verdict, priced_in_call ∈ priced_in|partly_priced_in|not_priced_in, citations[], grid_used, product, error}`
  - `ceo_move.json`: `{status, sharpest_needle, sharpest_needle_source ∈ human|recommended_fallback, action, timeframe, trade_off, downside_if_wrong, why_it_works, citations[], verdict_used, error}`
  - `citations` are finding/reasoning ids; the report resolves them to chunk citations → "filing, p.N" footnotes.

**Never:** editing `findings.json`, `run.json` or `docs/contracts.md`; Comparison Builder, report, orchestrator, UI, product pick, Label Lawyer; scoring changes; real API calls in tests; web search.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Happy | 5 selected, valid replies | both files `status: ok`, citations kept | N/A |
| Unselected id | reply cites a non-selected reasoning | retry; 2nd valid reply written | 2nd bad → failed |
| No grid | no comparison.json | `grid_used: false`, one call | N/A |
| Needle not cited | move omits needle | retry | then failed |
| Non-action | action "Investigate further …" | retry | then failed |
| Invented number | output has a number not in input | retry | then failed |
| Refusal / malformed / max_tokens | stop_reason refusal, bad JSON | retry | then failed, no raise |
| Needle missing / invalid | no run.json / id not in the 5 | fallback to Recommended / failed, 0 calls | warning |
| Nothing selected | no selected reasoning | failed, 0 calls | warning |

</frozen-after-approval>

## Code Map

- `pharmer/llm.py` -- `call(run_id, stage, agent, model=llm.OPUS, max_tokens, system, messages, output_config)`; raises `anthropic.APIError`. Do not modify.
- `pharmer/dedup.py` -- pattern to copy: `SYSTEM` loaded at import, `OUTPUT_CONFIG` json_schema, `parse_verdict` (stop_reason must be `end_turn`, text blocks joined, `json.loads`), APIError caught → fallback. Do not modify.
- `pharmer/scoring.py` -- writes `reasonings[].selected`, `status`, `recommended` in findings.json. Do not modify.
- Shared helpers (number extraction, id resolution, call-with-one-retry) live in `synthesizer.py`; `ceo_move.py` imports them (no new module without a stage, conventions.md).
- Log stage/agent: `synthesizer`/`synthesizer`, `ceo-move`/`ceo_move`.
- `tests/test_llm.py` -- `FakeClient`, `FakeStream`, `fake_message`; `tests/test_dedup.py` -- temp run dir + `llm.LOG_PATH`/`llm._client` swap.

## Tasks & Acceptance

**Execution:**
- [x] `pharmer/prompts/synthesizer.md`, `pharmer/prompts/ceo_move.md` -- system prompts (closed-book, cite ids, action rules).
- [x] `pharmer/synthesizer.py` -- message builder, validation, retry, `run`.
- [x] `pharmer/ceo_move.py` -- needle resolution, message builder, validation, `run`.
- [x] `tests/fixtures/synthesis_findings.json`, `tests/fixtures/synthesis_run.json` -- scoring-output shape, 5 selected + unselected/dropped rows, illustrative numbers.
- [x] `tests/test_synthesizer.py`, `tests/test_ceo_move.py` -- every matrix row + needle cases + "user message numbers ⊆ fixture numbers".
- [x] `docs/build-log.md` -- decision + test entries.

**Acceptance Criteria:**
- Given any reply sequence, when either `run` executes, then it writes its output file and never raises (except a missing findings.json).
- Given a run, when calls are made, then every request uses `llm.OPUS`, the same `system` string, and at most 2 calls per stage.

## Verification

**Commands:**
- `.venv/bin/python -m unittest discover tests` -- expected: all pass, no network.

## Implementation Notes

- Implemented directly (user asked: no implementation subagent). Shared helpers live in `synthesizer.py`; `ceo_move.py` imports them.
- Review pass 1 changed the payload to match the spec's field list exactly (impact eps_pct/usd_m, priced_in_hits, peer_status; no trend, Quant inputs or chunk ids), and `priced_in_call` is now checked in code against the cited reasonings' hits.

## Review Triage Log

Pass 1 (blind, edge-case, verification-gap):
- VG/BH/EC unhashable `sharpest_needle` (list/dict) raises TypeError in resolve_needle — high, patch: `isinstance(pick, str)` guard; blank string = no pick (fallback).
- VG empty citations / blank text / bad priced_in_call untested — medium, patch: table-driven tests added (synth + CEO empty action).
- BH/EC verdict_used true on a no-call failure — low, patch: set only once a needle resolves.
- EC blank or non-string verdict in synthesis.json used — low, patch: `usable_verdict`.
- BH stale synthesis.json citing unselected ids used — medium, patch: verdict dropped unless its citations are all still selected.
- BH/EC read_json_or_none misses OSError ("unreadable" in spec) — medium, patch: catch OSError; tests for invalid JSON + directory.
- EC/BH one-sentence check passes "by the FDA. The …" and fails "vs. X" / "e.g. Y" — medium, patch: break = [.!?…] + space + capital unless the word before is an initialism or a listed abbreviation; \r/\u2028 count as line breaks; 80-word cap.
- BH/EC non-action check only first word ("We will investigate", "Investigating") — medium, patch: optional subject+modal prefix, verb stems; prompt now lists "gather".
- BH timeframe exemption hides any figure — medium, patch: only time expressions (N days/weeks/months/quarters/years, Q1–Q4/H1–H2, 20xx) exempt; prompt says timeframe holds only dates/durations.
- BH/EC payload wider than the spec's field list — medium, patch: trimmed to the spec list.
- BH priced_in_call not checked against hits — medium, patch: `expected_priced_in_call` in validate; prompt states the rule on cited reasonings.
- EC non-APIError client errors (no API key) escape — medium, patch: catch `anthropic.AnthropicError`.
- EC CEO text fields with line breaks unchecked — low, patch.
- BH Synthesizer prompt drops "the product's" growth (§4.8) — low, patch: prompt names the product.
- VG CEO 2-call cap / same system on retry not asserted; API error then success untested — low, patch: assertions + test.
- BH number check ignores sign/unit ("rises 4.5%" vs -4.5; "222 billion") — low, reject: presence check by design; sign/unit semantics need NLP; prompt forbids it. Recorded in build log as a known limit.
- EC small integers allowed via labels (Q2 2026 → "2") — low, reject: inherent to a presence check; recorded with the above.
- EC invented ids (F-999) in prose bypass the number check — low, reject: unlikely; citations are checked.
- EC European decimal comma ("1,5") — low, reject: inputs are JSON numbers and English SEC text.
- VG ".7" read as "7" — low, reject: both sides normalized the same way.
- EC invalid findings.json / malformed finding entries raise — false: spec excludes a broken findings.json; a contract-violating upstream file should fail loudly.
- BH failed/empty comparison.json treated as a grid — maybe-false, defer: the grid file has no contract yet (Comparison Builder unbuilt).
- BH spec out of date (status, checkboxes) — false: spec is in-review with tasks checked.
