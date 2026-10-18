"""Scoring + selection (architecture §5): pick the AI's final 5 and tag one Recommended.

Pure Python, no Claude calls. Each reasoning that passes the confidence gate is a ranking unit
scored Materiality x Obscurity; a greedy fill with the 2-per-hunter cap and the label guarantee
picks up to 5 units. Reads and rewrites runs/<run_id>/findings.json (Contract 2), filling
reasonings[].score and reasonings[].selected and the finding-level status and recommended.

Missing upstream data never raises: no Quant impact -> score null (still eligible, ranked after
every scored unit); no Priced-In result -> obscurity 1.0; peer null/incomplete -> no demotion.
"""
import json
import logging
import math
from pathlib import Path

log = logging.getLogger(__name__)

FINAL_SIZE = 5
MAX_PER_HUNTER = 2
CONFIDENCE_RANK = {"High": 2, "Medium": 1}  # Low and null fail the gate


def _number(value):
    """value as a float if it is a real, finite number (bools are not numbers here), else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def materiality(reasoning):
    """abs(impact.eps_pct): damage and upside both matter; None when Quant left no figure."""
    eps_pct = _number((reasoning.get("impact") or {}).get("eps_pct"))
    return None if eps_pct is None else abs(eps_pct)


def obscurity(reasoning):
    """1 / (1 + priced_in.hits): 0 hits -> 1.0, 1 -> 0.5, 2 -> 0.33. Unchecked counts as not
    priced in (1.0), so with the Priced-In Detector cut the ranking falls back to materiality."""
    hits = (reasoning.get("priced_in") or {}).get("hits")
    if isinstance(hits, float) and hits.is_integer():
        hits = int(hits)  # 2.0 from a hand- or model-written JSON is still 2 hits
    if isinstance(hits, bool) or not isinstance(hits, int) or hits < 0:
        if hits is not None:
            log.warning("scoring: unusable priced_in.hits %r on %s; counted as not priced in",
                        hits, reasoning.get("reasoning_id"))
        return 1.0
    return 1.0 / (1 + hits)


def eligible(reasoning):
    """Confidence gate (§5.1.1): only Fact Checker High/Medium reasonings are scored."""
    return reasoning.get("confidence") in CONFIDENCE_RANK


def score(reasoning):
    """Materiality x Obscurity, or None when the reasoning fails the gate or has no impact."""
    m = materiality(reasoning)
    if not eligible(reasoning) or m is None:
        return None
    return round(m * obscurity(reasoning), 4)


def rank_key(finding, reasoning, position):
    """Sort key, best first: not industry-wide, scored, score, confidence, obscurity (before
    materiality: "not obvious" is the core of the case, §5.1.4), materiality, input order."""
    s = score(reasoning)
    m = materiality(reasoning)
    demoted = (finding.get("peer") or {}).get("status") == "industry_wide"
    return (demoted, s is None, -(s or 0.0), -CONFIDENCE_RANK.get(reasoning.get("confidence"), 0),
            -obscurity(reasoning), m is None, -(m or 0.0), position)


def ranked_units(findings):
    """Every eligible reasoning as (finding, reasoning), best first. A composite row's strongest
    reasoning is simply its best-ranked unit."""
    units = [(f, r) for f in findings for r in f.get("reasonings") or [] if eligible(r)]
    keys = {id(r): rank_key(f, r, i) for i, (f, r) in enumerate(units)}
    return sorted(units, key=lambda u: keys[id(u[1])]), keys


def select(units, keys):
    """Greedy fill of the final 5 (§5.1.4) over ranked units, then the label guarantee.

    A unit is skipped if its hunter already has 2 in the 5; sibling reasonings of one anchor may
    both be taken (§4.2). If no selected unit is a label finding, the best-ranked eligible label
    unit replaces the lowest-ranked selected one (or fills a free slot when fewer than 5 were
    taken). Returns the selected units, best first.
    """
    chosen, per_hunter = [], {}
    for finding, reasoning in units:
        if len(chosen) == FINAL_SIZE:
            break
        hunter = reasoning.get("hunter")
        if per_hunter.get(hunter, 0) >= MAX_PER_HUNTER:
            continue
        chosen.append((finding, reasoning))
        per_hunter[hunter] = per_hunter.get(hunter, 0) + 1

    def is_label(unit):
        return bool(unit[0].get("is_label_finding"))

    if chosen and not any(is_label(u) for u in chosen):
        label = next((u for u in units if is_label(u)), None)
        if label is None:
            log.warning("scoring: no eligible label/product finding in the final %d", len(chosen))
        else:
            if len(chosen) == FINAL_SIZE:
                chosen.pop()  # every chosen unit is non-label; the last is the lowest-ranked
            chosen.append(label)
    return sorted(chosen, key=lambda u: keys[id(u[1])])


def apply(findings):
    """Fill score/selected per reasoning and status/recommended per finding, in place.

    Recomputed from scratch, so re-running is idempotent. status: dropped (no reasoning passed
    the gate), selected (holds a selected reasoning), verified (eligible, not selected).
    """
    for finding in findings:
        for reasoning in finding.get("reasonings") or []:
            reasoning["score"] = score(reasoning)
            reasoning["selected"] = False
        finding["recommended"] = False
        finding["status"] = ("verified" if any(eligible(r) for r in finding.get("reasonings") or [])
                             else "dropped")

    units, keys = ranked_units(findings)
    chosen = select(units, keys)
    for finding, reasoning in chosen:
        reasoning["selected"] = True
        finding["status"] = "selected"
    if chosen:
        chosen[0][0]["recommended"] = True
        if chosen[0][1]["score"] is None:
            log.warning("scoring: recommended %s has no score (no Quant impact in the final %d)",
                        chosen[0][1].get("reasoning_id"), len(chosen))
    else:
        log.warning("scoring: no finding passed the confidence gate; nothing selected")
    return chosen


async def run(run_dir: Path) -> None:
    """Read run_dir/findings.json, score and select the final 5, write it back."""
    path = Path(run_dir) / "findings.json"
    findings = json.loads(path.read_text(encoding="utf-8"))
    chosen = apply(findings)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(findings, fh, indent=2, ensure_ascii=False)
    log.info("scoring: %d finding(s), %d eligible reasoning(s), %d selected, recommended %s",
             len(findings), sum(eligible(r) for f in findings for r in f.get("reasonings") or []),
             len(chosen), chosen[0][0].get("finding_id") if chosen else None)
