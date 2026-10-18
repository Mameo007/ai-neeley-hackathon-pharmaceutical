"""Runs the Pharmer pipeline for one company, in two phases split at the human pause (§2).

    start  : corpus -> hunters -> Dedup -> Fact Checker -> Quant -> Priced-In -> scoring -> Peer
             -> scoring again, then stop so the analyst can review the selected findings.
    finish : write the analyst's Sharpest Needle, product and rivals into run.json, then
             Comparison Builder -> Synthesizer -> CEO Move -> Report.

Each stage is `await pharmer.<module>.run(run_dir)`. A stage in DISABLED_STAGES is not scheduled
at all (pipeline.json lists it under "disabled"); a stage whose module is not on main yet is
skipped visibly; a required stage that raises stops the phase, an optional one does not.
Progress and per-stage timings go to runs/<run_id>/pipeline.json (a status sidecar, not a
contract file), rewritten after every stage so a UI can poll it.

Usage:
    .venv/bin/python orchestrator.py start NVO [--corpus data/sample/NVO]
    .venv/bin/python orchestrator.py finish runs/NVO-20261018-0230 [--needle F-003] [--product X]
        [--rivals NAME[,NAME]]
"""
import argparse
import asyncio
import importlib
import json
import logging
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from pharmer import comparison, librarian
from pharmer import synthesizer as syn

log = logging.getLogger("orchestrator")

REPO_ROOT = Path(__file__).resolve().parent
RUNS_DIR = REPO_ROOT / "runs"
CORPUS_FILES = ["manifest.json", "chunks.jsonl", "facts.jsonl"]
CORPUS_AUDIT = "librarian_report.json"  # the Librarian's sidecar; copied when present
# Outputs of the finish phase, cleared before a re-run so a failed re-pick never leaves the
# previous pick's report looking current.
FINISH_OUTPUTS = ["comparison.json", "comparison_sources.json", "synthesis.json",
                  "ceo_move.json", "report.pdf"]

# (stage name, module under pharmer/, required). Scoring runs again after the Peer Agent,
# which may demote findings (scoring is idempotent by design).
START_STAGES = [
    ("hunters", "hunters", True),
    ("dedup", "dedup", True),
    ("fact_checker", "fact_checker", False),
    ("quant", "quant", False),
    ("priced_in", "priced_in", False),
    ("scoring", "scoring", True),
    ("peer", "peer", False),
    ("scoring_after_peer", "scoring", True),
]
# Start stages left out of the schedule on purpose, with the reason pipeline.json records.
# Delete an entry to switch the stage back on. Scoring treats a missing priced-in check as
# not priced in and a missing peer status as no demotion, so the ranking is materiality-based.
DISABLED_STAGES = {
    "priced_in": "disabled for the demo: the Priced-In Detector is not built yet",
    "peer": "disabled for the demo: the Peer Agent is not built yet",
}
FINISH_STAGES = [
    ("comparison", "comparison", False),
    ("synthesizer", "synthesizer", True),
    ("ceo_move", "ceo_move", True),
    ("report", "report", True),
]
# Stages that record their own failure in an output file instead of raising.
OUTPUT_STATUS_FILES = {"synthesizer": "synthesis.json", "ceo_move": "ceo_move.json"}


def _output_problem(run_dir, name):
    """Why a stage that returned normally still produced nothing usable, or None. These stages
    contain their own failures (a failed hunter, a refused verdict) instead of raising."""
    if name == "hunters":
        leads = syn.read_json_or_none(Path(run_dir) / "leads.json")
        if not leads:
            return "no leads: every hunter failed or found nothing (see the log)"
    output = OUTPUT_STATUS_FILES.get(name)
    written = syn.read_json_or_none(Path(run_dir) / output) if output else None
    if isinstance(written, dict) and written.get("status") == "failed":
        return f"{output} status failed: {written.get('error')}"
    return None


class UsageError(Exception):
    """A request the orchestrator refuses before touching any file (CLI exit code 2)."""


def load_module(module):
    """pharmer.<module>, or None when that module does not exist yet."""
    name = f"pharmer.{module}"
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as exc:
        if exc.name == name:
            return None
        raise  # the module exists but one of its own imports is missing


def read_status(run_dir):
    status = syn.read_json_or_none(Path(run_dir) / "pipeline.json")
    return status if isinstance(status, dict) else {"stages": {}}


def _save(run_dir, status):
    """Write pipeline.json atomically, so a polling UI never reads a half-written file."""
    path = Path(run_dir) / "pipeline.json"
    tmp = path.with_suffix(".json.tmp")
    syn.write_json(tmp, status)
    os.replace(tmp, path)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def _run_stages(run_dir, status, stages):
    """Run stages in order, recording each; False when a required stage failed.

    A stage's runner is a module name under pharmer/ (its `run(run_dir)` is awaited) or an
    async callable taking run_dir.
    """
    for name, runner, required in stages:
        entry = {"status": "running", "started_at": _now(), "seconds": 0.0, "error": None}
        status["stages"][name] = entry
        start = time.monotonic()
        try:
            if isinstance(runner, str):
                module = load_module(runner)
                if module is None:
                    entry.update(status="skipped", error="not built")
                    log.warning("%s: skipped (pharmer/%s is not built yet)", name, runner)
                    _save(run_dir, status)
                    continue
                runner = module.run
            log.info("%s: running", name)
            _save(run_dir, status)
            await runner(run_dir)
        except Exception as exc:  # any stage failure (or broken import) is recorded, not lost
            entry.update(status="failed", error=f"{type(exc).__name__}: {exc}"[:500])
            log.exception("%s failed", name)
        except BaseException:  # Ctrl-C / cancellation: never leave the run at "running"
            entry.update(status="failed", error="interrupted",
                         seconds=round(time.monotonic() - start, 2))
            status["phase"] = "failed"
            _save(run_dir, status)
            raise
        else:
            entry["status"] = "ok"
            problem = _output_problem(run_dir, name)
            if problem:
                entry.update(status="degraded", error=problem)
                log.warning("%s: %s", name, problem)
        entry["seconds"] = round(time.monotonic() - start, 2)
        _save(run_dir, status)
        if entry["status"] == "failed" and required:
            return False
    return True


def _corpus_stage(corpus):
    """The corpus stage: the live Librarian, or a copy of a prebuilt corpus folder."""
    if corpus is None:
        return librarian.run

    async def copy(run_dir):
        for name in CORPUS_FILES + [CORPUS_AUDIT]:
            if name in CORPUS_FILES or (Path(corpus) / name).is_file():
                shutil.copy(Path(corpus) / name, Path(run_dir) / name)
    return copy


def _corpus_tickers(corpus):
    manifest = syn.read_json_or_none(Path(corpus) / "manifest.json")
    if not isinstance(manifest, list):
        return set()
    return {d.get("ticker") for d in manifest if isinstance(d, dict) and d.get("ticker")}


def prepare_run(ticker, corpus=None, runs_dir=None) -> Path:
    """Validate a start request and create its run folder + pipeline.json (phase "running").

    Raises UsageError before creating anything. Split from start_run so a UI learns the run
    folder before the (long) stages begin.
    """
    ticker = ticker.upper()
    if ticker not in librarian.CIKS:
        raise UsageError(f"unknown ticker {ticker!r}; known: {', '.join(sorted(librarian.CIKS))}")
    if corpus is not None:
        if not all((Path(corpus) / n).is_file() for n in CORPUS_FILES):
            raise UsageError(f"--corpus {corpus} must hold {', '.join(CORPUS_FILES)}")
        if ticker not in _corpus_tickers(corpus):
            raise UsageError(f"--corpus {corpus} is not a {ticker} corpus (its manifest has "
                             f"{', '.join(sorted(_corpus_tickers(corpus))) or 'no ticker'})")
    run_dir = Path(runs_dir or RUNS_DIR) / librarian.run_id_for(ticker)
    run_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        run_dir.mkdir()
    except FileExistsError:
        raise UsageError(f"{run_dir} already exists; start again in a minute") from None
    _save(run_dir, {"ticker": ticker, "phase": "running", "started_at": _now(), "stages": {},
                    "disabled": dict(DISABLED_STAGES)})
    return run_dir


async def run_start_stages(run_dir, corpus=None) -> Path:
    """Phase 1's stages on a prepared run folder; ends at "awaiting_human" or "failed"."""
    run_dir = Path(run_dir)
    status = read_status(run_dir)
    stages = [s for s in START_STAGES if s[0] not in DISABLED_STAGES]
    ok = await _run_stages(run_dir, status, [("corpus", _corpus_stage(corpus), True)] + stages)
    status["phase"] = "awaiting_human" if ok else "failed"
    _save(run_dir, status)
    return run_dir


async def start_run(ticker, corpus=None, runs_dir=None) -> Path:
    """Phase 1: new run folder, corpus, hunting, verification, scoring; stops at the human pause.

    corpus: a folder holding a prebuilt manifest.json/chunks.jsonl/facts.jsonl to copy instead of
    running the live Librarian. Returns the run folder; its pipeline.json says how it went.
    """
    return await run_start_stages(prepare_run(ticker, corpus, runs_dir), corpus)


def _scored_findings(run_dir):
    """findings.json after scoring (no finding left at status "candidate"), else None."""
    findings = syn.read_json_or_none(Path(run_dir) / "findings.json")
    if not isinstance(findings, list) or any(f.get("status") == "candidate" for f in findings):
        return None
    return findings


def prepare_finish(run_dir, needle=None, product=None, rivals=None) -> Path:
    """Validate a finish request, record the analyst's choices in run.json, clear the previous
    finish outputs and mark the run "running". Raises UsageError before writing anything.

    rivals: "A,B" or a list of names; None keeps run.json's, empty clears them."""
    run_dir = Path(run_dir)
    status = syn.read_json_or_none(run_dir / "pipeline.json")
    # A hand-assembled run folder has no pipeline.json. One made by `start` must have reached
    # the human pause, and not be running now; a failed earlier `finish` may be retried.
    if isinstance(status, dict):
        start_done = ((status.get("stages") or {}).get("scoring_after_peer") or {}).get(
            "status") == "ok"
        if status.get("phase") == "running" or not start_done:
            raise UsageError(f"{run_dir} is at phase {status.get('phase')!r}; `finish` needs a "
                             "run whose `start` reached the human pause (see pipeline.json)")
    findings = _scored_findings(run_dir)
    if findings is None:
        raise UsageError(f"{run_dir} has no scored findings.json; run `start` first")
    selected = {f["finding_id"] for f, _ in syn.selected_findings(findings)}
    if needle is not None and needle not in selected:
        raise UsageError(f"--needle {needle} is not a selected finding; choose one of "
                         f"{', '.join(sorted(selected)) or '(none selected)'}")
    run_info = syn.read_json_or_none(run_dir / "run.json")
    run_info = run_info if isinstance(run_info, dict) else {}
    if needle is not None:
        run_info["sharpest_needle"] = needle
    if product == "":
        run_info.pop("product", None)  # the analyst withdrew an earlier product choice
    elif product is not None:
        run_info["product"] = product
    if rivals is not None:
        # The Comparison Builder's own normalizer, so run.json holds the list the stage uses.
        names = comparison.clean_rivals(rivals, run_info.get("product"))
        if names:
            run_info["rivals"] = names
        else:
            run_info.pop("rivals", None)  # the analyst withdrew the rival list
    syn.write_json(run_dir / "run.json", run_info)

    for name in FINISH_OUTPUTS:
        (run_dir / name).unlink(missing_ok=True)
    status = read_status(run_dir)
    status["phase"] = "running"
    status.pop("error", None)  # a crash recorded by a UI job belongs to the previous attempt
    for name, _, _ in FINISH_STAGES:
        status["stages"].pop(name, None)
    _save(run_dir, status)
    return run_dir


async def run_finish_stages(run_dir) -> Path:
    """Phase 2's stages on a run prepared by prepare_finish; ends at "done" or "failed"."""
    run_dir = Path(run_dir)
    status = read_status(run_dir)
    ok = await _run_stages(run_dir, status, FINISH_STAGES)
    status["phase"] = "done" if ok else "failed"
    _save(run_dir, status)
    return run_dir


async def finish_run(run_dir, needle=None, product=None, rivals=None) -> Path:
    """Phase 2: record the analyst's choices in run.json, then grid, verdict, move and report.

    needle: a selected finding id, or None to let CEO Move fall back to the Recommended one
    (the report labels it as not yet confirmed). product: a name, None to keep run.json's, or ""
    to clear it. rivals: "A,B" or a list, None to keep run.json's, or empty to clear them.
    Re-running with a new pick is allowed.
    """
    return await run_finish_stages(prepare_finish(run_dir, needle, product, rivals))


def _stage_lines(status):
    return [f"  {name:<20} {e['status']:<9} {e['seconds']:>7.1f}s"
            + (f"  {e['error']}" if e.get("error") else "")
            for name, e in status["stages"].items()]


def _pause_summary(run_dir):
    findings = _scored_findings(run_dir) or []
    selected = syn.selected_findings(findings)
    lines = ["", "Selected findings:"]
    for finding, reasonings in selected:
        tag = "  [Recommended]" if finding.get("recommended") else ""
        lines.append(f"  {finding['finding_id']}{tag}: {reasonings[0].get('claim')}")
    command = f"  .venv/bin/python orchestrator.py finish {run_dir}"
    if not selected:
        return lines + ["  none (see the stage list above).", "",
                        "Nothing to name. `finish` without --needle still writes a report that "
                        "says why:", command]
    recommended = next((f["finding_id"] for f, _ in selected if f.get("recommended")), "F-nnn")
    return lines + ["", "Name the Sharpest Needle (Recommended pre-filled; change it to "
                        "pick another), confirm the product and name the rivals, then run:",
                    f"{command} --needle {recommended} --product NAME --rivals NAME[,NAME]"]


def main(argv):
    parser = argparse.ArgumentParser(prog="orchestrator.py", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p_start = sub.add_parser("start", help="corpus to scoring; stops at the human pause")
    p_start.add_argument("ticker")
    p_start.add_argument("--corpus", help="prebuilt corpus folder instead of the live Librarian")
    p_finish = sub.add_parser("finish", help="Sharpest Needle + product, then verdict to PDF")
    p_finish.add_argument("run_dir")
    p_finish.add_argument("--needle", help="selected finding id named by the analyst")
    p_finish.add_argument("--product", help="product confirmed by the analyst")
    p_finish.add_argument("--rivals", help='comma-separated rival products, NAME[,NAME] '
                                           '("" clears them)')
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    try:
        if args.command == "start":
            run_dir = asyncio.run(start_run(args.ticker, corpus=args.corpus))
        else:
            run_dir = asyncio.run(finish_run(args.run_dir, needle=args.needle,
                                             product=args.product, rivals=args.rivals))
    except UsageError as exc:
        print(f"orchestrator: {exc}", file=sys.stderr)
        return 2
    status = read_status(run_dir)
    try:
        run_dir = run_dir.resolve().relative_to(Path.cwd())
    except ValueError:
        pass
    lines = [f"\n{run_dir}: {status['phase']}"] + _stage_lines(status)
    if status["phase"] == "awaiting_human":
        lines += _pause_summary(run_dir)
    elif status["phase"] == "done":
        lines.append(f"\nReport: {run_dir / 'report.pdf'}")
    print("\n".join(lines))
    return 0 if status["phase"] in ("awaiting_human", "done") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
