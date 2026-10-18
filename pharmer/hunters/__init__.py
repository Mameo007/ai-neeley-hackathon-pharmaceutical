"""Hunter swarm runner: fans out every hunter in parallel over one corpus and pools their leads.

Reads Contract 1 from the run folder and writes `leads.json` (a list of Contract 2 Findings,
one reasoning each) for Dedup.
"""
import asyncio
import json
import logging
from pathlib import Path

from pharmer.hunters import channel_checker
from pharmer.hunters.harness import Corpus, load_corpus, run_hunter

log = logging.getLogger(__name__)

HUNTERS = [channel_checker]


async def _run_safely(run_id: str, corpus: Corpus, hunter) -> list[dict]:
    """One hunter's failure (API error, timeout) must not cost the other hunters their leads."""
    try:
        return await run_hunter(run_id, corpus, hunter.NAME, hunter.BRIEF)
    except Exception:
        log.exception("%s failed; continuing without its leads", hunter.NAME)
        return []


async def run(run_dir: Path) -> None:
    """Run all hunters on run_dir's corpus; write run_dir/leads.json with ids F-001, F-002, ...

    lead_reasoning_id stays null: Contract 2 gives it to Dedup.
    """
    run_dir = Path(run_dir)
    corpus = load_corpus(run_dir)
    results = await asyncio.gather(
        *(_run_safely(run_dir.name, corpus, h) for h in HUNTERS))
    leads = [finding for findings in results for finding in findings]
    for n, finding in enumerate(leads, 1):
        finding["finding_id"] = f"F-{n:03d}"
        finding["reasonings"][0]["reasoning_id"] = f"F-{n:03d}-a"
    with (run_dir / "leads.json").open("w", encoding="utf-8") as fh:
        json.dump(leads, fh, indent=2, ensure_ascii=False)
    log.info("wrote %d lead(s) to %s", len(leads), run_dir / "leads.json")
