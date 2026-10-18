"""Synthesizer (architecture §4.8): the one-sentence growth-limiter / priced-in verdict.

One Opus call over the selected reasonings of runs/<run_id>/findings.json (Contract 2, after
scoring), plus runs/<run_id>/comparison.json when the Comparison Builder has written it and the
product from run.json when the human has confirmed one. Code validates the reply (citations,
one sentence, no number absent from the input); a failed call or check gets one retry with the
problems listed, then the stage writes a visible failed state instead of raising. Writes
runs/<run_id>/synthesis.json (proposed shape, pending Partner A's sign-off).

The helpers below (selected findings, payload, number check, ask-with-retry) are shared with
pharmer/ceo_move.py.
"""
import json
import logging
import re
from pathlib import Path

import anthropic

from pharmer import llm

log = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
STAGE = "synthesizer"
AGENT = "synthesizer"
MAX_TOKENS = 2000
ATTEMPTS = 2  # one retry, then a failed state

# Byte-identical on every call; the findings go in the user message.
SYSTEM = (PROMPTS_DIR / "synthesizer.md").read_text(encoding="utf-8")

PRICED_IN_CALLS = ["priced_in", "partly_priced_in", "not_priced_in"]

OUTPUT_CONFIG = {
    "format": {
        "type": "json_schema",
        "schema": {
            "type": "object",
            "properties": {
                "verdict": {"type": "string"},
                "priced_in_call": {"type": "string", "enum": PRICED_IN_CALLS},
                "citations": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["verdict", "priced_in_call", "citations"],
            "additionalProperties": False,
        },
    },
}

_ID = re.compile(r"\bF-\d+(?:-[a-z]+)?\b")
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
# A sentence end: . ! ? or an ellipsis (optionally closed by a quote or bracket), whitespace,
# then a capital. The word before it is checked against _NOT_AN_END, so "U.S. Food" or
# "vs. Ozempic" inside one sentence still passes.
_SENTENCE_BREAK = re.compile(r"(\S*?)([.!?\u2026][\"'\u201d\u2019)\]]*)\s+[A-Z]")
_NOT_AN_END = re.compile(r"(?:[A-Za-z]\.)*[A-Za-z]|vs|e\.g|i\.e|approx|inc|no|mr|ms|dr|st",
                         re.IGNORECASE)
_LINE_BREAK = re.compile(r"[\r\n\u2028\u2029]")
MAX_VERDICT_WORDS = 80  # the prompt asks for about 60


def write_json(path, obj):
    with Path(path).open("w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def read_json_or_none(path):
    """Parsed JSON at path, or None when the file is missing or not valid JSON."""
    path = Path(path)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("%s is unreadable or not valid JSON (%s); treated as missing", path.name, exc)
        return None


def selected_findings(findings):
    """[(finding, its selected reasonings)] for every finding holding one, in findings order."""
    out = []
    for finding in findings:
        chosen = [r for r in finding.get("reasonings") or [] if r.get("selected") is True]
        if chosen:
            out.append((finding, chosen))
    return out


def allowed_ids(selected):
    """Ids a citation may use: selected reasoning ids and the findings holding them."""
    ids = set()
    for finding, reasonings in selected:
        ids.add(finding["finding_id"])
        ids.update(r["reasoning_id"] for r in reasonings)
    return ids


def finding_payload(finding, reasonings):
    """What the model sees of one selected finding: the fields the verdict needs and nothing
    else (no chunk ids, Quant inputs or trend), because every number sent here becomes a number
    the reply may quote."""
    return {
        "finding_id": finding["finding_id"],
        "recommended": finding.get("recommended", False),
        "anchor": finding.get("anchor"),
        "is_label_finding": finding.get("is_label_finding", False),
        "contested": finding.get("contested", False),
        "peer_status": (finding.get("peer") or {}).get("status"),
        "reasonings": [{
            "reasoning_id": r["reasoning_id"],
            "hunters": r.get("hunters") or [r.get("hunter")],
            "claim": r.get("claim"),
            "quotes": [c.get("quote") for c in r.get("citations") or []],
            "confidence": r.get("confidence"),
            "flags": r.get("flags") or [],
            "impact": ({k: r["impact"].get(k) for k in ("eps_pct", "usd_m")}
                       if r.get("impact") else None),
            "priced_in_hits": priced_in_hits(r),
        } for r in reasonings],
    }


def priced_in_hits(reasoning):
    """Priced-In Detector hits, or None when the reasoning was not checked."""
    return (reasoning.get("priced_in") or {}).get("hits")


def expected_priced_in_call(citations, selected):
    """The prompt's rule, applied in code to the cited reasonings (a cited finding id stands for
    all its selected reasonings): no hits anywhere -> not priced in, hits everywhere -> priced
    in, otherwise partly. Unchecked (null) counts as no hits."""
    hits = []
    for finding, reasonings in selected:
        for r in reasonings:
            if finding["finding_id"] in citations or r["reasoning_id"] in citations:
                hits.append(bool(priced_in_hits(r)))
    if not any(hits):
        return "not_priced_in"
    return "priced_in" if all(hits) else "partly_priced_in"


def findings_block(selected):
    return json.dumps([finding_payload(f, rs) for f, rs in selected], indent=2,
                      ensure_ascii=False)


def numbers_in(text):
    """Every number in text, normalized: finding/reasoning ids skipped, thousands commas, sign
    and % dropped, trailing decimal zeros removed ("-420.0" and "420" are the same number)."""
    out = set()
    for match in _NUMBER.finditer(_ID.sub(" ", text)):
        number = match.group().rstrip(",").replace(",", "")
        if "." in number:
            number = number.rstrip("0").rstrip(".")
        out.add(number.lstrip("0") or "0")
    return out


def check_text_fields(reply, fields):
    return [f"`{name}` must be a non-empty string" for name in fields
            if not isinstance(reply.get(name), str) or not reply[name].strip()]


def check_citations(citations, allowed):
    if not isinstance(citations, list) or not citations:
        return ["`citations` must list at least one finding or reasoning id"]
    bad = [c for c in citations if not isinstance(c, str) or c not in allowed]
    if bad:
        return [f"citations {bad} are not ids of the selected findings; use only "
                f"{sorted(allowed)}"]
    return []


def check_numbers(texts, allowed_numbers):
    """Numbers in texts that do not appear in the input: the closed-book rule, checked in code."""
    invented = sorted({n for t in texts if isinstance(t, str) for n in numbers_in(t)}
                      - allowed_numbers)
    if invented:
        return [f"numbers {invented} do not appear in the input; quote numbers exactly as "
                "written there or leave them out"]
    return []


def parse_reply(message):
    """(reply dict, []) or (None, problems) for a refused, truncated or non-JSON reply."""
    stop = getattr(message, "stop_reason", None)
    if stop != "end_turn":
        return None, [f"the previous reply ended with stop_reason={stop}; reply with the JSON "
                      "only and keep every field short"]
    text = "".join(getattr(b, "text", "") for b in message.content if b.type == "text")
    try:
        reply = json.loads(text)
    except ValueError:
        return None, ["the previous reply was not valid JSON"]
    if not isinstance(reply, dict):
        return None, ["the previous reply was not a JSON object"]
    return reply, []


async def ask(*, run_id, stage, agent, system, output_config, user_message, validate):
    """Call Opus, validate, retry once with the problems appended to the user message.

    Returns (reply, None) or (None, error). Never raises on an API error, a refusal, a
    truncated or malformed reply, or a failed check.
    """
    problems, error = [], None
    for attempt in range(1, ATTEMPTS + 1):
        content = user_message
        if problems:
            content += ("\n\n# Problems with your previous attempt\n\n"
                        + "\n".join(f"- {p}" for p in problems))
        try:
            message = await llm.call(
                run_id=run_id, stage=stage, agent=agent, turn=attempt, model=llm.OPUS,
                max_tokens=MAX_TOKENS, system=system,
                messages=[{"role": "user", "content": content}], output_config=output_config)
        except anthropic.AnthropicError as exc:  # API errors and client setup (e.g. no key)
            error = f"API error: {type(exc).__name__}: {exc}"[:300]
            log.warning("%s attempt %d: %s", agent, attempt, error)
            continue  # keep the previous problems; the request never reached the model
        reply, found = parse_reply(message)
        if reply is not None:
            found = validate(reply)
        if not found:
            return reply, None
        problems, error = found, "; ".join(found)
        log.warning("%s attempt %d rejected: %s", agent, attempt, error)
    return None, error


def unique(ids):
    return list(dict.fromkeys(ids))


def is_one_sentence(text):
    if _LINE_BREAK.search(text):
        return False
    return not any(not _NOT_AN_END.fullmatch(m.group(1)) for m in _SENTENCE_BREAK.finditer(text))


def validate(reply, selected, allowed_numbers):
    problems = check_text_fields(reply, ["verdict"])
    if not problems:
        verdict = reply["verdict"]
        if not is_one_sentence(verdict) or len(verdict.split()) > MAX_VERDICT_WORDS:
            problems.append("`verdict` must be exactly one sentence of at most about 60 words, "
                            "with no line breaks")
        problems += check_numbers([verdict], allowed_numbers)
    citation_problems = check_citations(reply.get("citations"), allowed_ids(selected))
    call = reply.get("priced_in_call")
    if call not in PRICED_IN_CALLS:
        problems.append(f"`priced_in_call` must be one of {PRICED_IN_CALLS}")
    elif not citation_problems:
        expected = expected_priced_in_call(reply["citations"], selected)
        if call != expected:
            problems.append(f"`priced_in_call` is {call!r}, but the priced_in hits of the "
                            f"cited findings make it {expected!r}")
    return problems + citation_problems


def user_message(selected, product, grid):
    lines = ["# Product", "", product or "not named yet", "", "# Comparison grid", ""]
    lines.append(json.dumps(grid, indent=2, ensure_ascii=False) if grid is not None
                 else "unavailable (not built for this run); judge from the findings alone")
    lines += ["", "# Selected findings", "", findings_block(selected)]
    return "\n".join(lines)


async def run(run_dir: Path) -> None:
    """Read run_dir/findings.json (+ comparison.json, run.json if present), write
    run_dir/synthesis.json with status "ok" or "failed"."""
    run_dir = Path(run_dir)
    findings = json.loads((run_dir / "findings.json").read_text(encoding="utf-8"))
    selected = selected_findings(findings)
    run_info = read_json_or_none(run_dir / "run.json")
    product = run_info.get("product") if isinstance(run_info, dict) else None
    product = product if isinstance(product, str) and product.strip() else None
    grid = read_json_or_none(run_dir / "comparison.json")
    out = {"status": "failed", "verdict": None, "priced_in_call": None, "citations": [],
           "grid_used": grid is not None, "product": product, "error": None}

    if not selected:
        out["error"] = "no selected findings in findings.json"
        log.warning("synthesizer: %s", out["error"])
    else:
        message = user_message(selected, product, grid)
        numbers = numbers_in(message)
        reply, out["error"] = await ask(
            run_id=run_dir.name, stage=STAGE, agent=AGENT, system=SYSTEM,
            output_config=OUTPUT_CONFIG, user_message=message,
            validate=lambda r: validate(r, selected, numbers))
        if reply is not None:
            out.update(status="ok", verdict=reply["verdict"].strip(),
                       priced_in_call=reply["priced_in_call"],
                       citations=unique(reply["citations"]))
    write_json(run_dir / "synthesis.json", out)
    log.info("synthesizer: status=%s grid_used=%s citations=%s", out["status"], out["grid_used"],
             out["citations"])
