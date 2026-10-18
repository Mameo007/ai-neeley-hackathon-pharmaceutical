"""Dedup / merge (architecture §4.2): collapse leads that share a fact anchor.

Code groups leads by a normalized anchor key (metric + entity + fiscal_period); within each group
of two or more, a Haiku judge decides which reasonings tell the same story and whether any
contradict. Reads runs/<run_id>/leads.json (Contract 2 Findings, one reasoning each, written by
pharmer/hunters) and writes runs/<run_id>/findings.json (Contract 2).
"""
import asyncio
import copy
import json
import logging
import re
from pathlib import Path

import anthropic

from pharmer import llm

log = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
STAGE = "dedup"
AGENT = "dedup_judge"
MAX_TOKENS = 2000

# Byte-identical on every call; the group under review goes in the user message.
SYSTEM = (PROMPTS_DIR / "dedup_judge.md").read_text(encoding="utf-8")

OUTPUT_CONFIG = {
    "format": {
        "type": "json_schema",
        "schema": {
            "type": "object",
            "properties": {
                "clusters": {"type": "array",
                             "items": {"type": "array", "items": {"type": "integer"}}},
                "contested": {"type": "boolean"},
                "reason": {"type": "string"},
            },
            "required": ["clusters", "contested", "reason"],
            "additionalProperties": False,
        },
    },
}

# Trailing legal-form tokens (after punctuation is removed: "A/S" -> "as", "S.A." -> "sa").
_ENTITY_SUFFIXES = {"as", "inc", "plc", "ltd", "ag", "sa", "corp", "co", "nv", "se", "llc"}


def anchor_key(anchor):
    """Grouping key: case/spacing/legal suffix are ignored; periods are never mapped onto each
    other (H1 2026 != Q2 2026, Contract 1 matches on fiscal_period as labelled)."""
    metric = re.sub(r"[\s\-]+", "_", str(anchor.get("metric") or "").strip().casefold())
    # Hyphens separate words ("Novo-Nordisk"); other punctuation joins them ("A/S" -> "as").
    entity = str(anchor.get("entity") or "").casefold().replace("-", " ")
    tokens = re.sub(r"[^\w\s]", "", entity).split()
    while len(tokens) > 1 and tokens[-1] in _ENTITY_SUFFIXES:
        tokens.pop()
    period = " ".join(str(anchor.get("fiscal_period") or "").upper().replace("-", " ").split())
    return metric, " ".join(tokens), period


def group_leads(leads):
    """Lists of leads sharing an anchor key, in order of each group's first appearance."""
    groups = {}
    for lead in leads:
        groups.setdefault(anchor_key(lead["anchor"]), []).append(lead)
    return list(groups.values())


def _letters(index):
    """0 -> 'a', 25 -> 'z', 26 -> 'aa'."""
    out = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        out = chr(ord("a") + rem) + out
    return out


def judge_message(group):
    """User message for one group: the shared anchor, then each lead's hunter, claim, quotes."""
    lines = ["# Fact anchor", "", json.dumps(group[0]["anchor"], ensure_ascii=False), "", "# Leads"]
    for n, lead in enumerate(group, 1):
        reasoning = lead["reasonings"][0]
        # json.dumps keeps quotes and newlines in corpus text from breaking the lead layout.
        lines += ["", f"## Lead {n} (hunter: {reasoning['hunter']})",
                  f"Claim: {json.dumps(reasoning['claim'], ensure_ascii=False)}", "Quotes:"]
        lines += [f"- {json.dumps(c['quote'], ensure_ascii=False)} ({c['chunk_id']})"
                  for c in reasoning["citations"]]
    return "\n".join(lines)


def parse_verdict(message, size):
    """(clusters as 0-based index lists, contested) from the judge reply, or None if unusable."""
    stop = getattr(message, "stop_reason", None)
    if stop != "end_turn":
        log.warning("dedup judge stop_reason=%s", stop)
        return None
    text = "".join(getattr(b, "text", "") for b in message.content if b.type == "text")
    try:
        verdict = json.loads(text)
        clusters, contested = verdict["clusters"], verdict["contested"]
    except (ValueError, KeyError, TypeError) as exc:
        log.warning("dedup judge reply is not valid JSON: %s", exc)
        return None
    if not (isinstance(clusters, list) and all(isinstance(c, list) for c in clusters)
            and all(type(n) is int for c in clusters for n in c)):
        log.warning("dedup judge clusters are not lists of lead numbers: %s", text)
        return None
    clusters = [[n - 1 for n in cluster] for cluster in clusters]
    members = sorted(i for cluster in clusters for i in cluster)
    if (members != list(range(size)) or not all(clusters) or not isinstance(contested, bool)
            or (contested and len(clusters) < 2)):
        log.warning("dedup judge reply is not a valid partition of %d leads: %s", size, text)
        return None
    log.info("dedup judge: clusters=%s contested=%s reason=%s", verdict["clusters"], contested,
             verdict.get("reason", ""))
    return clusters, contested


async def judge_group(run_id, group):
    """Judge one group; on an API error or a refused, truncated or malformed reply keep every
    lead separate, so one bad request cannot cost the other groups their findings."""
    try:
        message = await llm.call(
            run_id=run_id, stage=STAGE, agent=AGENT, model=llm.HAIKU, max_tokens=MAX_TOKENS,
            system=SYSTEM, messages=[{"role": "user", "content": judge_message(group)}],
            output_config=OUTPUT_CONFIG)
    except anthropic.APIError as exc:
        log.warning("dedup judge request failed: %s", exc)
        message = None
    verdict = parse_verdict(message, len(group)) if message is not None else None
    if verdict is None:
        log.warning("dedup judge failed for %s; keeping %d reasonings separate",
                    group[0]["anchor"], len(group))
        return [[i] for i in range(len(group))], False
    return verdict


def merge_reasonings(leads):
    """One reasoning from leads sharing a story: earliest claim verbatim, every citation and
    every contributing hunter kept."""
    reasonings = [lead["reasonings"][0] for lead in leads]
    merged = copy.deepcopy(reasonings[0])
    merged["citations"], seen = [], set()
    for reasoning in reasonings:
        for cite in reasoning["citations"]:
            if (cite["chunk_id"], cite["quote"]) not in seen:
                seen.add((cite["chunk_id"], cite["quote"]))
                merged["citations"].append(copy.deepcopy(cite))
    hunters = [h for r in reasonings for h in r.get("hunters") or [r["hunter"]]]
    merged["hunters"] = list(dict.fromkeys(hunters))
    merged["flags"] = list(dict.fromkeys(f for r in reasonings for f in r.get("flags") or []))
    return merged


def build_finding(group, clusters, contested):
    """One Contract 2 Finding per anchor group; ids are assigned by renumber()."""
    finding = copy.deepcopy(group[0])
    # Write the normalized metric and period, so the Fact Checker matches facts.jsonl labels.
    finding["anchor"]["metric"], _, finding["anchor"]["fiscal_period"] = anchor_key(
        finding["anchor"])
    numbers = {(lead["anchor"].get("value"), lead["anchor"].get("unit"),
                lead["anchor"].get("currency")) for lead in group}
    if len(numbers) > 1:
        log.warning("anchor %s has differing value/unit/currency %s; keeping the earliest",
                    group[0]["anchor"], sorted(numbers, key=str))
    # Earliest-raised story first; scoring ranks a composite row by its strongest reasoning.
    clusters = sorted(clusters, key=min)
    finding["reasonings"] = [merge_reasonings([group[i] for i in sorted(c)]) for c in clusters]
    finding["is_label_finding"] = any(lead["is_label_finding"] for lead in group)
    finding["contested"] = contested
    if contested:
        for reasoning in finding["reasonings"]:
            if "contested" not in reasoning["flags"]:
                reasoning["flags"].append("contested")
    return finding


def renumber(findings):
    """F-001, F-002, ... and F-001-a, F-001-b, ...; the lead reasoning is the first one."""
    for n, finding in enumerate(findings, 1):
        finding["finding_id"] = f"F-{n:03d}"
        for i, reasoning in enumerate(finding["reasonings"]):
            reasoning["reasoning_id"] = f"F-{n:03d}-{_letters(i)}"
        finding["lead_reasoning_id"] = finding["reasonings"][0]["reasoning_id"]
    return findings


async def run(run_dir: Path) -> None:
    """Read run_dir/leads.json, merge leads per fact anchor, write run_dir/findings.json.

    Groups of one pass straight through with no API call; larger groups are judged concurrently.
    """
    run_dir = Path(run_dir)
    leads = json.loads((run_dir / "leads.json").read_text(encoding="utf-8"))
    groups = group_leads(leads)

    async def _verdict(group):
        if len(group) == 1:
            return [[0]], False
        return await judge_group(run_dir.name, group)

    verdicts = await asyncio.gather(*(_verdict(g) for g in groups))
    findings = renumber([build_finding(g, c, contested)
                         for g, (c, contested) in zip(groups, verdicts)])
    with (run_dir / "findings.json").open("w", encoding="utf-8") as fh:
        json.dump(findings, fh, indent=2, ensure_ascii=False)
    log.info("dedup: %d lead(s) -> %d finding(s), %d judge call(s)", len(leads), len(findings),
             sum(len(g) > 1 for g in groups))
