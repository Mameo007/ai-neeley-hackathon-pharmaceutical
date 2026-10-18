"""Comparison Builder (architecture §4.7): the 5-dimension product grid vs named rivals.

Reads `product` and `rivals` from runs/<run_id>/run.json (both named by the analyst) and the
corpus (manifest.json + chunks.jsonl, Contract 1). One Opus call per product column, run in
parallel, sees only that product's label chunks plus the filing chunks naming it, and returns per
dimension a value with the chunk_id and verbatim quote it rests on. Code keeps a cell only when
the chunk exists, the quote is a verbatim substring of its text, and every number in the value
appears in the quote; anything else becomes "not disclosed". A failed call (API error, refusal,
malformed reply) gets one retry with the problems listed, then its column is all "not disclosed";
the stage never raises on a model failure.

Writes runs/<run_id>/comparison.json ({"columns", "rows"}, docs/contracts.md) and the sidecar
runs/<run_id>/comparison_sources.json (validated chunk_ids per cell, rejections, call errors; not
a contract file). Writes nothing when no product is named; the report then says the table was not
built.
"""
import asyncio
import json
import logging
import re
from pathlib import Path

from pharmer import synthesizer as syn

log = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
STAGE = "comparison"
AGENT = "comparison"

# Byte-identical on every call; the product and its chunks go in the user message.
SYSTEM = (PROMPTS_DIR / "comparison.md").read_text(encoding="utf-8")

NOT_DISCLOSED = "not disclosed"
# (reply key, row label) in the §4.7 order.
DIMENSIONS = [
    ("approved_uses", "Approved uses"),
    ("boxed_warning_safety", "Boxed warning / safety"),
    ("dosing_convenience", "Dosing / convenience"),
    ("patent_exclusivity", "Patent / exclusivity"),
    ("pricing_gross_to_net", "Pricing & gross-to-net"),
]
CELL_FIELDS = ["value", "chunk_id", "quote"]

# Filing chunks are a supplement to the label; capped so a product named on every page of a
# filing cannot crowd the prompt.
MAX_FILING_CHUNKS = 30
MAX_FILING_CHARS = 45_000

_CELL_SCHEMA = {
    "type": "object",
    "properties": {name: {"type": "string"} for name in CELL_FIELDS},
    "required": CELL_FIELDS,
    "additionalProperties": False,
}
OUTPUT_CONFIG = {
    "format": {
        "type": "json_schema",
        "schema": {
            "type": "object",
            "properties": {key: _CELL_SCHEMA for key, _ in DIMENSIONS},
            "required": [key for key, _ in DIMENSIONS],
            "additionalProperties": False,
        },
    },
}


def _read_jsonl(path):
    """Rows of a JSONL file; [] when it is missing. A bad line is skipped with a warning."""
    path = Path(path)
    if not path.exists():
        return []
    rows = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            log.warning("comparison: %s line %d is not valid JSON; skipped", path.name, n)
            continue
        if isinstance(row, dict) and isinstance(row.get("chunk_id"), str):
            rows.append(row)
    return rows


def _names(product):
    """Regex matching the product name as a whole word, any case."""
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(product.strip())}(?![A-Za-z0-9])",
                      re.IGNORECASE)


def clean_rivals(rivals, product):
    """Rivals ("A,B" or a list) as distinct names (repeats compared ignoring case), in order,
    without the product itself. Shared with orchestrator.py, so run.json stores this list."""
    if isinstance(rivals, str):
        rivals = rivals.split(",")
    if not isinstance(rivals, list):
        return []
    seen = {str(product or "").strip().casefold()}
    out = []
    for name in rivals:
        if not isinstance(name, str) or not name.strip():
            continue
        if name.strip().casefold() not in seen:
            seen.add(name.strip().casefold())
            out.append(name.strip())
    return out


def select_chunks(product, manifest, chunks):
    """The chunks one product column may cite: its label chunks first (a label is the product's
    when the product is named in its indications section), then filing chunks naming it. When
    the product has a label, only filings of the label holder's ticker count, so another
    company's passing mention of a rival cannot fill the rival's column."""
    names = _names(product)
    docs = {d.get("doc_id"): d for d in manifest if isinstance(d, dict)}
    doc_types = {doc_id: d.get("doc_type") for doc_id, d in docs.items()}
    label_docs = {c["doc_id"] for c in chunks
                  if doc_types.get(c.get("doc_id")) == "label"
                  and "INDICATIONS" in str(c.get("section", "")).upper()
                  and names.search(str(c.get("text", "")))}
    label = [c for c in chunks if c.get("doc_id") in label_docs]
    holders = {docs[d].get("ticker") for d in label_docs}
    filings, size = [], 0
    for c in chunks:
        if len(filings) >= MAX_FILING_CHUNKS:
            break
        doc_id = c.get("doc_id")
        if doc_types.get(doc_id) == "label" or not names.search(str(c.get("text", ""))):
            continue
        if label_docs and (docs.get(doc_id) or {}).get("ticker") not in holders:
            continue
        length = len(str(c.get("text", "")))
        if size + length > MAX_FILING_CHARS:
            continue  # too big for what is left of the budget; a smaller one may still fit
        size += length
        filings.append(c)
    return label + filings


def user_message(product, chunks, doc_types):
    payload = [{"chunk_id": c["chunk_id"], "doc_type": doc_types.get(c.get("doc_id")),
                "section": c.get("section"), "text": c.get("text")} for c in chunks]
    return "\n".join(["# Product", "", product, "", "# Chunks", "",
                      json.dumps(payload, indent=2, ensure_ascii=False)])


def validate_shape(reply):
    """Problems that warrant a retry: a missing dimension or a cell that is not three strings."""
    problems = []
    for key, _ in DIMENSIONS:
        cell = reply.get(key)
        if not isinstance(cell, dict) or not all(isinstance(cell.get(f), str)
                                                 for f in CELL_FIELDS):
            problems.append(f"`{key}` must be an object with string fields {CELL_FIELDS}")
    return problems


def _squash(text):
    return " ".join(str(text).split())


def check_cell(cell, sent):
    """(kept value, source) or (NOT_DISCLOSED, None, reason); reason is None when the model
    itself said not disclosed. `sent`: {chunk_id: chunk} of the chunks this column was sent."""
    value = _squash(cell["value"])
    if not value or value.casefold().rstrip(".").startswith(NOT_DISCLOSED):
        return NOT_DISCLOSED, None, None
    chunk_id = cell["chunk_id"].strip()
    chunk = sent.get(chunk_id)
    if chunk is None:
        return NOT_DISCLOSED, None, (f"chunk_id {chunk_id!r} is not among the chunks sent for "
                                     "this column")
    quote = cell["quote"].strip()
    # Whitespace-insensitive: chunk text keeps line breaks the model may write as spaces.
    if not quote or _squash(quote) not in _squash(chunk.get("text", "")):
        return NOT_DISCLOSED, None, f"quote is not a verbatim substring of {chunk_id}"
    invented = sorted(syn.numbers_in(value) - syn.numbers_in(quote))
    if invented:
        return NOT_DISCLOSED, None, f"numbers {invented} in the value are not in the quote"
    return value, {"chunk_id": chunk_id, "quote": quote}, None


async def build_column(run_id, product, manifest, chunks):
    """One product column: ([cell per dimension], its sources record)."""
    doc_types = {d.get("doc_id"): d.get("doc_type") for d in manifest if isinstance(d, dict)}
    selected = select_chunks(product, manifest, chunks)
    sent = {c["chunk_id"]: c for c in selected}
    record = {"status": "ok", "error": None, "chunks_sent": len(selected), "rejected": [],
              "cells": {label: None for _, label in DIMENSIONS}}
    cells = [NOT_DISCLOSED] * len(DIMENSIONS)
    if not selected:
        record.update(status="no_sources", error=f"no label or filing chunk names {product}")
        log.warning("comparison: %s", record["error"])
        return cells, record
    try:
        reply, error = await syn.ask(
            run_id=run_id, stage=STAGE, agent=AGENT, system=SYSTEM, output_config=OUTPUT_CONFIG,
            user_message=user_message(product, selected, doc_types), validate=validate_shape)
    except Exception as exc:  # e.g. the SDK's TypeError when no credential is set: the stage
        # must never raise, and one bad column must not cost the others theirs
        reply, error = None, f"{type(exc).__name__}: {exc}"[:300]
        log.exception("comparison: %s column call raised", product)
    if reply is None:
        record.update(status="failed", error=error)
        log.warning("comparison: %s column failed, all not disclosed: %s", product, error)
        return cells, record
    for i, (key, label) in enumerate(DIMENSIONS):
        value, source, reason = check_cell(reply[key], sent)
        cells[i] = value
        if source is not None:
            source["doc_type"] = doc_types.get(sent[source["chunk_id"]].get("doc_id"))
        record["cells"][label] = source
        if reason:
            record["rejected"].append({"dimension": label, "reason": reason})
    if record["rejected"]:
        log.warning("comparison: %s: %d unsourced cell(s) set to not disclosed", product,
                    len(record["rejected"]))
    return cells, record


async def run(run_dir: Path) -> None:
    """Read run_dir/run.json (product, rivals), manifest.json and chunks.jsonl; write
    run_dir/comparison.json and run_dir/comparison_sources.json. No product: writes nothing."""
    run_dir = Path(run_dir)
    run_info = syn.read_json_or_none(run_dir / "run.json")
    run_info = run_info if isinstance(run_info, dict) else {}
    product = run_info.get("product")
    if not isinstance(product, str) or not product.strip():
        log.warning("comparison: no product named in run.json; comparison table not built")
        return
    product = product.strip()
    rivals = clean_rivals(run_info.get("rivals"), product)
    if not rivals:
        log.warning("comparison: no rivals named in run.json; grid has the product column only")
    manifest = syn.read_json_or_none(run_dir / "manifest.json")
    manifest = manifest if isinstance(manifest, list) else []
    chunks = _read_jsonl(run_dir / "chunks.jsonl")

    names = [product] + rivals
    results = await asyncio.gather(*(build_column(run_dir.name, name, manifest, chunks)
                                     for name in names))
    rows = [[label] + [cells[i] for cells, _ in results]
            for i, (_, label) in enumerate(DIMENSIONS)]
    syn.write_json(run_dir / "comparison.json", {"columns": ["Dimension"] + names, "rows": rows})
    syn.write_json(run_dir / "comparison_sources.json", {
        "product": product, "rivals": rivals,
        "columns": {name: record for name, (_, record) in zip(names, results)}})
    filled = sum(cell != NOT_DISCLOSED for row in rows for cell in row[1:])
    rejected = sum(len(record["rejected"]) for _, record in results)
    log.info("comparison: %d columns, %d/%d cells filled, %d rejected as unsourced",
             len(names), filled, len(names) * len(DIMENSIONS), rejected)
