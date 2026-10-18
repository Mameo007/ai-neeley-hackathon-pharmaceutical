---
title: 'Scoring + selection (Partner B build step 2)'
type: 'feature'
created: '2026-10-17'
status: 'done'
baseline_commit: 'e8003fd78dad2b3e66a402b902c05498f1366e10'
route: 'dispatch'
review_loop_iteration: 0
context:
  - '{project-root}/docs/contracts.md'
  - '{project-root}/docs/conventions.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** findings.json leaves Dedup with no scores, statuses or Recommended tag, so nothing downstream (Synthesizer, report, UI) knows which 5 findings the AI picked.

**Approach:** `pharmer/scoring.py`, `async run(run_dir)`, pure Python: gate on confidence, score each reasoning Materiality × Obscurity, rank, apply the §5.1 diversity rule to pick the final 5, tag one Recommended, write findings.json back.

## Boundaries & Constraints

**Always:**
- Gate (§5.1.1/§5.3): a reasoning is eligible only if `confidence` ∈ High/Medium; Low or null → not eligible, `score` null. A finding with no eligible reasoning → `status: dropped`.
- Materiality = `abs(impact.eps_pct)`. Obscurity = `1 / (1 + priced_in.hits)` (0 hits → 1.0, 1 → 0.5, 2 → 0.33, 3 → 0.25). `score` = round(M × O, 4). `stock_move_pct` is not used yet.
- Missing data, never a crash: `impact` null / `eps_pct` not a number → `score` null; the reasoning stays eligible but ranks after every scored one. `priced_in` null / `hits` not an int → obscurity 1.0 ("unchecked = assumed not priced in"), so with the detector cut the ranking falls back to materiality. Peer `status` null or `incomplete` → no demotion.
- Ranking key, best first: industry-wide not demoted before demoted (`peer.status == "industry_wide"` → after all others, order kept); scored before unscored; score desc; then High > Medium, obscurity desc, materiality desc; then input order (stable). A composite row ranks by its strongest reasoning under this same key.
- Greedy fill: walk the ranked list; take each unit unless its hunter already has 2 in the 5; stop at 5. Fewer than 5 eligible (or blocked by the cap) → ship fewer; no backfill past the cap.
- Label guarantee: if no selected unit has `is_label_finding` and an eligible label unit exists, swap out the lowest-ranked non-label selected unit for the highest-ranked eligible label unit (as written: no cap check on the swap). None eligible → log a warning; the Label Lawyer retry is out of scope.
- Recommended: the best-ranked of the final 5; exactly one finding gets `recommended: true` (none if nothing selected).
- Statuses after run: `dropped` (no eligible reasoning), `selected` (in the 5), `verified` (eligible, not selected). Re-running is idempotent: scores, statuses and `recommended` are recomputed from scratch.
- Slot unit (decided Q1 → B): each eligible reasoning is a ranking unit and counts against its own `hunter` for the 2-per-hunter cap; sibling reasonings may both be selected. New field `reasonings[].selected: bool` (true for the ≤5 units, false otherwise; proposed, pending Partner A — flagged in the PR, contracts.md not edited). Finding `status: selected` if any of its reasonings is selected; `recommended: true` on the finding holding the best-ranked selected reasoning. The 5 may span fewer than 5 findings.
- No other field is touched; JSON written per conventions.

**Never:** Claude calls; computing impact/priced-in/peer data; writing run.json or the human Sharpest Needle pick; editing docs/contracts.md without Partner A (proposal goes in the PR only).

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Gate | reasonings Low / null / Medium | Low, null: score null; Medium scored; all-Low finding dropped | N/A |
| Composite | Medium M=1 + High M=3 | row ranks with score 3 | N/A |
| Diversity | 3 top units from one hunter | 3rd skipped, next hunter's unit takes the slot | N/A |
| Tie-break | equal scores | High beats Medium; then higher obscurity beats higher materiality | N/A |
| Label swap | no label in top 5, label unit ranked 7th | lowest non-label swapped out | N/A |
| Few eligible | 2 eligible | 2 selected, 1 recommended | N/A |
| Missing data | impact null; priced_in null; peer incomplete | rules above | no exception |
| Nothing eligible | all null confidence | all dropped, none recommended | warning logged |

</frozen-after-approval>

## Code Map

- `pharmer/dedup.py` -- style reference: module docstring naming the contract files, `log = logging.getLogger(__name__)`, `async def run(run_dir: Path)`, `json.dump(..., indent=2, ensure_ascii=False)` + trailing newline. Do not modify.
- `docs/contracts.md` Contract 2 -- fields read: `confidence`, `impact.eps_pct`, `priced_in.hits`, `peer.status`, `hunter`, `lead_reasoning_id`, `is_label_finding`; written: `score`, `selected` (new, per reasoning), `status`, `recommended`.
- `tests/test_dedup.py` -- test style (unittest, temp run dir, `asyncio.run`).
- `docs/build-log.md` -- append-only entries.

## Tasks & Acceptance

**Execution:**
- [x] `pharmer/scoring.py` -- new stage module: pure helpers (`materiality`, `obscurity`, `score_reasoning`, rank key, `select`) + `async run(run_dir)` -- testable without I/O.
- [x] `tests/test_scoring.py` -- hand-made findings (illustrative numbers) covering every matrix row + exactly-one-recommended + idempotent re-run.
- [x] `docs/build-log.md` -- decision entry (mapping, missing-data rules, slot answer) + test entry.

**Acceptance Criteria:**
- Given any findings.json matching Contract 2, when `run` executes, then it never raises on null/absent impact, priced_in or peer fields.
- Given ≥1 eligible reasoning, when `run` executes, then exactly one finding has `recommended: true` and at most 5 units are selected with no hunter above 2 (label swap excepted).

## Implementation Notes

- Implemented directly (user asked: no implementation subagent). Label guarantee fills a free slot instead of swapping when fewer than 5 were taken (no one to evict). `ranked_units` is public so the Peer Agent can take the top ~10.

## Spec Change Log

## Review Triage Log

Pass 1 (blind, edge-case, verification-gap):
- EC integral-float hits (1.0) -> obscurity 1.0 — low, patch: integral floats accepted; other unusable values now logged.
- EC huge-int eps_pct OverflowError in math.isfinite — low, reject: Quant writes floats; unreachable in everyday use.
- EC truthy non-dict impact/priced_in/peer, non-list findings.json — false: contract fixes the shapes; a loud failure on an unshown state is correct.
- EC hunter-less reasonings share a None cap bucket — low, reject: harness always sets hunter.
- EC/BH label swap can exceed the 2-per-hunter cap — false as a defect: frozen intent ("as written: no cap check on the swap"), approved at checkpoint.
- EC ranking on rounded score — low, reject: 4-decimal ties negligible; ranking matches displayed score.
- EC/BH Recommended can be unscored with no signal — low, patch: warning logged; selection rule itself is frozen intent.
- EC missing/malformed findings.json raises raw — false: conventions say let exceptions propagate.
- EC/BH non-atomic in-place write — low, reject: same pattern as every stage; data is loaded JSON so dump cannot fail on types.
- EC no trailing newline "unlike dedup" — false: dedup.py writes none either.
- EC helper named `score`, spec says `score_reasoning` — low, reject: no callers; cosmetic.
- BH cap per own hunter vs §5.1.4 lead-reasoning rule — false as a defect: Q1 decision B (frozen); recorded in build-log.
- BH final 5 can span <5 findings — false: frozen intent.
- BH `reasonings[].selected` not in contract — false as a defect: frozen intent says proposal flagged in PR, contracts.md untouched.
- BH no-label warning only logged — low, reject: report/UI can derive it (no selected finding has is_label_finding); noted in build-log.
- BH no first-pass ranking mode for Peer Agent — medium, defer: Peer Agent is not built; ranked_units is reusable; orchestrator re-runs scoring after Peer (idempotent).
- BH materiality ignores usd_m "(or equivalent)" — low, reject: Quant always fills eps_pct per contract; mixing $ and % breaks the scale.
- BH Recommended can be industry-wide — false: only when every selected unit is demoted; frozen "best-ranked" rule.
- BH finding-level recommended doesn't name the reasoning — low, reject: derivable via rank_key on selected siblings; noted in PR as contract question.
- BH extra test gaps (cap-full label swap, sibling eviction, demotion x cap) — low, reject except float hits (added).
- VG best-ranked label not pinned by a test — gap, patch: added test_label_swap_takes_best_ranked_label.

## Verification

**Commands:**
- `.venv/bin/python -m unittest discover tests` -- expected: all pass, offline.
