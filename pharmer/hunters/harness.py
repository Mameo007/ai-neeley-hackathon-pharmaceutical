"""Shared harness every hunter runs in: closed-book corpus tools, one tool loop, lead validation.

The system prompt and tool list are module constants, byte-identical for every hunter, so all
hunters share one cached prefix. A hunter differs only in its brief, sent in the first user
message. Reads Contract 1 (manifest.json, chunks.jsonl, facts.jsonl); leads come back in the
Contract 2 Finding shape (docs/contracts.md).
"""
import json
import logging
import re
from pathlib import Path

from pharmer import llm

log = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
STAGE = "hunters"
MAX_TURNS = 15  # also stated in prompts/hunter_rules.md; change both together
SEARCH_LIMIT = 10
READ_LIMIT = 10
SNIPPET_CHARS = 240
MIN_TERM_CHARS = 3  # drop "of", "in", ... so stopwords cannot dominate the ranking

# Explicit breakpoint after the rules: the prefix (tools + system) is shared by all hunters,
# while llm.call's automatic breakpoint covers each hunter's own growing conversation.
SYSTEM = [{"type": "text", "text": (PROMPTS_DIR / "hunter_rules.md").read_text(encoding="utf-8"),
           "cache_control": {"type": "ephemeral"}}]

_ANCHOR_FIELDS = ["metric", "entity", "fiscal_period", "value", "unit", "currency"]

TOOLS = [
    {
        "name": "search_chunks",
        "description": (
            "Keyword search over the corpus chunks. Returns up to 10 matches, best first, each "
            "with chunk_id, doc_id, page, section, is_table and a short snippet. Use read_chunks "
            "to see full text before quoting."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keywords, e.g. 'trade receivables'"},
                "doc_id": {"type": "string", "description": "Optional: only this document"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "read_chunks",
        "description": "Full text of up to 10 chunks by chunk_id.",
        "input_schema": {
            "type": "object",
            "properties": {
                "chunk_ids": {"type": "array", "items": {"type": "string"},
                              "minItems": 1, "maxItems": 10},
            },
            "required": ["chunk_ids"],
        },
    },
    {
        "name": "get_facts",
        "description": (
            "Structured numbers (metric, value, unit, currency, fiscal_period, period_end). "
            "Filter by metric and/or fiscal_period; omit both to list every fact. Context only: "
            "facts are not citations."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "metric": {"type": "string", "description": "e.g. revenue, inventory"},
                "fiscal_period": {"type": "string", "description": "e.g. FY2025, H1 2026"},
            },
        },
    },
    {
        "name": "submit_leads",
        "description": (
            "Hand in your leads, once, when done. Every citation's chunk_id must exist and its "
            "quote must be copied exactly from that chunk's text; at least one quote per lead "
            "must contain anchor.value as printed. On error nothing is recorded; fix the listed "
            "problems and submit again. The last valid submission wins. An empty list is allowed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "leads": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "anchor": {
                                "type": "object",
                                "properties": {
                                    "metric": {"type": "string"},
                                    "entity": {"type": "string"},
                                    "fiscal_period": {"type": "string"},
                                    "value": {"type": "number"},
                                    "unit": {"type": "string"},
                                    "currency": {"type": "string"},
                                },
                                "required": _ANCHOR_FIELDS,
                            },
                            "claim": {"type": "string"},
                            "citations": {
                                "type": "array",
                                "minItems": 1,
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "chunk_id": {"type": "string"},
                                        "quote": {"type": "string"},
                                    },
                                    "required": ["chunk_id", "quote"],
                                },
                            },
                            "is_label_finding": {"type": "boolean"},
                        },
                        "required": ["anchor", "claim", "citations", "is_label_finding"],
                    },
                },
            },
            "required": ["leads"],
        },
    },
]


class Corpus:
    """A run's Contract 1 files, loaded once and shared read-only by all hunters."""

    def __init__(self, manifest, chunks, facts):
        self.manifest = manifest
        self.chunks = chunks
        self.chunks_by_id = {c["chunk_id"]: c for c in chunks}
        self.facts = facts
        self.ticker = manifest[0]["ticker"] if manifest else ""
        self.company = manifest[0]["company"] if manifest else ""


def _read_jsonl(path):
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def load_corpus(run_dir: Path) -> Corpus:
    """Read manifest.json, chunks.jsonl and facts.jsonl from a run folder."""
    run_dir = Path(run_dir)
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    return Corpus(manifest, _read_jsonl(run_dir / "chunks.jsonl"),
                  _read_jsonl(run_dir / "facts.jsonl"))


def _dumps(obj):
    return json.dumps(obj, ensure_ascii=False)


def search_chunks(corpus, query, doc_id=None, limit=SEARCH_LIMIT):
    """Rank chunks by keyword hits in section + text; ties keep corpus order."""
    terms = {t for t in re.findall(r"\w+", query.lower()) if len(t) >= MIN_TERM_CHARS}
    scored = []
    for index, chunk in enumerate(corpus.chunks):
        if doc_id and chunk["doc_id"] != doc_id:
            continue
        haystack = f"{chunk['section']}\n{chunk['text']}".lower()
        score = sum(haystack.count(term) for term in terms)
        if score:
            scored.append((-score, index, chunk))
    scored.sort(key=lambda item: item[:2])
    return [
        {"chunk_id": c["chunk_id"], "doc_id": c["doc_id"], "page": c["page"],
         "section": c["section"], "is_table": c["is_table"],
         "snippet": " ".join(c["text"].split())[:SNIPPET_CHARS]}
        for _, _, c in scored[:limit]
    ]


def read_chunks(corpus, chunk_ids):
    """Full chunks by id; unknown ids are listed under `missing`."""
    if len(chunk_ids) > READ_LIMIT:
        raise ValueError(f"at most {READ_LIMIT} chunk_ids per call")
    found = [corpus.chunks_by_id[cid] for cid in chunk_ids if cid in corpus.chunks_by_id]
    missing = [cid for cid in chunk_ids if cid not in corpus.chunks_by_id]
    if not found:
        raise ValueError(f"no such chunk_id: {', '.join(missing)}")
    return {"chunks": found, "missing": missing}


def get_facts(corpus, metric=None, fiscal_period=None):
    return [f for f in corpus.facts
            if (not metric or f["metric"] == metric)
            and (not fiscal_period or f["fiscal_period"] == fiscal_period)]


def _value_forms(value):
    """Ways a reported number can be printed in a filing: 88949 -> '88949', '88,949'."""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    forms = {str(value)}
    if isinstance(value, int):
        forms.add(f"{value:,}")
    return forms


def validate_leads(corpus, leads):
    """Return a list of problems; empty means every lead meets the hunter rules in Contract 2."""
    problems = []
    for i, lead in enumerate(leads, 1):
        anchor = lead.get("anchor") or {}
        missing = [f for f in _ANCHOR_FIELDS if anchor.get(f) in (None, "")]
        if missing:
            problems.append(f"lead {i}: anchor missing {', '.join(missing)}")
        value = anchor.get("value")
        value_ok = isinstance(value, (int, float)) and not isinstance(value, bool)
        if value is not None and not value_ok:
            problems.append(f"lead {i}: anchor.value must be a number")
        if not isinstance(lead.get("is_label_finding"), bool):
            problems.append(f"lead {i}: is_label_finding must be true or false")
        if not str(lead.get("claim") or "").strip():
            problems.append(f"lead {i}: empty claim")
        citations = lead.get("citations") or []
        if not citations:
            problems.append(f"lead {i}: no citations")
        for j, cite in enumerate(citations, 1):
            chunk = corpus.chunks_by_id.get(cite.get("chunk_id"))
            quote = cite.get("quote") or ""
            if chunk is None:
                problems.append(f"lead {i} citation {j}: no such chunk_id {cite.get('chunk_id')!r}")
            elif not quote or quote not in chunk["text"]:
                problems.append(f"lead {i} citation {j}: quote is not an exact substring of "
                                f"{chunk['chunk_id']}; copy it character-for-character")
        # Tie the anchor to its evidence, so Dedup never groups on an uncited number.
        quotes = [str(c.get("quote") or "") for c in citations]
        if value_ok and not any(form in q for q in quotes for form in _value_forms(value)):
            problems.append(f"lead {i}: no citation quote contains anchor.value {value}")
    return problems


def build_finding(corpus, hunter, lead):
    """Wrap one submitted lead in the Contract 2 Finding shape; ids are assigned by the runner."""
    return {
        "finding_id": None,
        "ticker": corpus.ticker,
        "anchor": {f: lead["anchor"][f] for f in _ANCHOR_FIELDS},
        "is_label_finding": lead["is_label_finding"],
        "contested": False,
        "lead_reasoning_id": None,
        "reasonings": [{
            "reasoning_id": None,
            "hunter": hunter,
            "claim": lead["claim"],
            "citations": [{"chunk_id": c["chunk_id"], "quote": c["quote"]}
                          for c in lead["citations"]],
            "confidence": None,
            "confidence_reason": None,
            "flags": [],
            "trend": [],
            "impact": None,
            "priced_in": None,
            "score": None,
        }],
        "peer": {"status": None, "note": ""},
        "status": "candidate",
        "recommended": False,
    }


def corpus_overview(corpus):
    """Run-specific context for the first user message (kept out of the cached system prompt)."""
    lines = [f"Company: {corpus.company} ({corpus.ticker})", "", "Documents:"]
    for doc in corpus.manifest:
        lines.append(f"- {doc['doc_id']}: {doc['doc_type']}, {doc['fiscal_period']}, "
                     f"period end {doc['period_end']}, filed {doc['filed_date']}")
    metrics = sorted({f["metric"] for f in corpus.facts})
    lines += ["", f"Fact metrics available: {', '.join(metrics) or 'none'}"]
    return "\n".join(lines)


async def run_hunter(run_id, corpus, hunter, brief):
    """Run one hunter's tool loop; return its validated leads as Findings (ids unassigned)."""
    submitted = None

    async def _search(args):
        return _dumps(search_chunks(corpus, args["query"], args.get("doc_id")))

    async def _read(args):
        return _dumps(read_chunks(corpus, args["chunk_ids"]))

    async def _facts(args):
        return _dumps(get_facts(corpus, args.get("metric"), args.get("fiscal_period")))

    async def _submit(args):
        nonlocal submitted
        leads = args.get("leads") or []
        problems = validate_leads(corpus, leads)
        if problems:
            kept = "your earlier submission still stands" if submitted is not None else ""
            raise ValueError("\n".join([f"nothing recorded{'; ' + kept if kept else ''}:",
                                         *problems]))
        submitted = leads
        return f"Recorded {len(leads)} lead(s). You are done; end your turn."

    handlers = {"search_chunks": _search, "read_chunks": _read, "get_facts": _facts,
                "submit_leads": _submit}
    content = f"{brief}\n\n# Corpus\n\n{corpus_overview(corpus)}"
    message = await llm.run_agent(
        run_id=run_id, stage=STAGE, agent=hunter, tools=TOOLS, tool_handlers=handlers,
        messages=[{"role": "user", "content": content}], model=llm.OPUS, max_turns=MAX_TURNS,
        system=SYSTEM)

    stop = getattr(message, "stop_reason", None)
    if stop == "tool_use":
        log.warning("%s hit the %d-turn cap", hunter, MAX_TURNS)
    elif stop != "end_turn":
        log.warning("%s ended with stop_reason=%s", hunter, stop)
    if submitted is None:
        log.warning("%s never submitted valid leads", hunter)
        return []
    log.info("%s submitted %d lead(s)", hunter, len(submitted))
    return [build_finding(corpus, hunter, lead) for lead in submitted]
