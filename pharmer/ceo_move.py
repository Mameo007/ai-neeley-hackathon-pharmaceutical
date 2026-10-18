"""CEO Move (architecture §4.9): one concrete strategic move answering the Sharpest Needle.

One Opus call over the selected findings of runs/<run_id>/findings.json, the human's Sharpest
Needle from runs/<run_id>/run.json and the verdict in runs/<run_id>/synthesis.json. Code
validates the reply (citations, the needle cited, a real action, no number absent from the
input outside the timeframe); a failed call or check gets one retry, then the stage writes a
visible failed state instead of raising. Writes runs/<run_id>/ceo_move.json (proposed shape,
pending Partner A's sign-off).

Needle rule: no run.json or no pick yet -> the Recommended finding, marked
"recommended_fallback"; a pick that is not one of the selected findings -> failed, no call.
"""
import json
import logging
import re
from pathlib import Path

from pharmer import synthesizer as syn

log = logging.getLogger(__name__)

STAGE = "ceo-move"
AGENT = "ceo_move"

# Byte-identical on every call; the findings, needle and verdict go in the user message.
SYSTEM = (syn.PROMPTS_DIR / "ceo_move.md").read_text(encoding="utf-8")

TEXT_FIELDS = ["action", "timeframe", "trade_off", "downside_if_wrong", "why_it_works"]

OUTPUT_CONFIG = {
    "format": {
        "type": "json_schema",
        "schema": {
            "type": "object",
            "properties": {
                **{name: {"type": "string"} for name in TEXT_FIELDS},
                "citations": {"type": "array", "items": {"type": "string"}},
            },
            "required": [*TEXT_FIELDS, "citations"],
            "additionalProperties": False,
        },
    },
}

# "Investigate further" and friends: more research is not a move (§4.9). Matches the verb (any
# ending: "Investigating", "assessed") after an optional "we will" / "the company should".
_NON_ACTION = re.compile(
    r"^\W*(?:(?:we|the\s+company|management)\s+(?:will|should|must|shall|would)\s+"
    r"(?:\w+\s+)?)?(?:begin\s+to\s+|start\s+to\s+)?"
    r"(investigat|monitor|stud(?:y|ie)|review|explor|assess|evaluat|consider|analy[sz]|"
    r"research|look\s+into|continu|wait|gather)\w*\b", re.IGNORECASE)
# Time expressions the timeframe may state without a source: "12 months", "Q3", "H1", "2027".
_TIME_EXPRESSION = re.compile(
    r"\b\d+(?:\s*(?:-|to|–)\s*\d+)?\s*(?:days?|weeks?|months?|quarters?|years?)\b"
    r"|\b[QH][1-4]\b|\b20\d\d\b", re.IGNORECASE)


def resolve_needle(run_info, selected):
    """(finding_id, source, error). No pick yet -> the Recommended finding; a pick outside the
    selected findings is the human's error to see and fix, so it is not overridden."""
    pick = run_info.get("sharpest_needle") if isinstance(run_info, dict) else None
    if pick is None or (isinstance(pick, str) and not pick.strip()):
        recommended = next((f["finding_id"] for f, _ in selected if f.get("recommended")), None)
        if recommended is None:
            return None, None, "no sharpest_needle in run.json and no Recommended finding"
        log.warning("ceo_move: no sharpest_needle in run.json; using Recommended %s", recommended)
        return recommended, "recommended_fallback", None
    if not isinstance(pick, str) or pick not in {f["finding_id"] for f, _ in selected}:
        return None, None, f"sharpest_needle {pick!r} is not one of the selected findings"
    return pick, "human", None


def usable_verdict(synthesis, selected):
    """The Synthesizer's verdict, or None when it failed, is blank, or cites ids that are no
    longer selected (a synthesis.json left from an earlier scoring pass)."""
    if not isinstance(synthesis, dict) or synthesis.get("status") != "ok":
        return None
    verdict, citations = synthesis.get("verdict"), synthesis.get("citations")
    if not isinstance(verdict, str) or not verdict.strip():
        return None
    if not isinstance(citations, list) or syn.check_citations(citations, syn.allowed_ids(selected)):
        log.warning("ceo_move: synthesis.json cites findings that are no longer selected; "
                    "verdict not used")
        return None
    return verdict.strip()


def user_message(selected, needle, source, verdict, product):
    who = ("named by the human analyst" if source == "human"
           else "the AI's Recommended finding; the human has not named one yet")
    lines = ["# Product", "", product or "not named yet", "",
             "# Sharpest Needle", "", f"{needle} ({who})", "", "# Verdict", "",
             json.dumps(verdict, ensure_ascii=False) if verdict
             else "unavailable for this run; decide from the findings alone",
             "", "# Selected findings", "", syn.findings_block(selected)]
    return "\n".join(lines)


def validate(reply, allowed, needle_ids, allowed_numbers):
    problems = syn.check_text_fields(reply, TEXT_FIELDS)
    if not problems:
        if _NON_ACTION.match(reply["action"]):
            problems.append("`action` must be a decision, not more research (no investigate, "
                            "monitor, review, assess, ...); start with a decisive verb")
        problems += [f"`{f}` must have no line breaks" for f in TEXT_FIELDS
                     if syn._LINE_BREAK.search(reply[f])]
        # Time expressions in the timeframe ("within 12 months") are a commitment, not a fact
        # from the filings; every other number there is still checked.
        texts = [reply[f] for f in TEXT_FIELDS if f != "timeframe"]
        texts.append(_TIME_EXPRESSION.sub(" ", reply["timeframe"]))
        problems += syn.check_numbers(texts, allowed_numbers)
    problems += syn.check_citations(reply.get("citations"), allowed)
    citations = reply.get("citations")
    if isinstance(citations, list) and not any(isinstance(c, str) and c in needle_ids
                                               for c in citations):
        problems.append(f"`citations` must include the Sharpest Needle ({sorted(needle_ids)[0]} "
                        "or one of its reasoning ids)")
    return problems


async def run(run_dir: Path) -> None:
    """Read run_dir/findings.json, run.json and synthesis.json, write run_dir/ceo_move.json with
    status "ok" or "failed"."""
    run_dir = Path(run_dir)
    findings = json.loads((run_dir / "findings.json").read_text(encoding="utf-8"))
    selected = syn.selected_findings(findings)
    run_info = syn.read_json_or_none(run_dir / "run.json")
    product = run_info.get("product") if isinstance(run_info, dict) else None
    product = product if isinstance(product, str) and product.strip() else None
    synthesis = syn.read_json_or_none(run_dir / "synthesis.json")
    verdict = usable_verdict(synthesis, selected)
    out = {"status": "failed", "sharpest_needle": None, "sharpest_needle_source": None,
           **{name: None for name in TEXT_FIELDS}, "citations": [],
           "verdict_used": False, "error": None}

    needle = source = None
    if not selected:
        out["error"] = "no selected findings in findings.json"
    else:
        needle, source, out["error"] = resolve_needle(run_info, selected)
    if needle is None:
        log.warning("ceo_move: %s", out["error"])
    else:
        out.update(sharpest_needle=needle, sharpest_needle_source=source,
                   verdict_used=verdict is not None)
        if not verdict:
            log.warning("ceo_move: no usable Synthesizer verdict; deciding from the findings")
        message = user_message(selected, needle, source, verdict, product)
        allowed, numbers = syn.allowed_ids(selected), syn.numbers_in(message)
        needle_ids = {needle} | {r["reasoning_id"] for f, rs in selected
                                 if f["finding_id"] == needle for r in rs}
        reply, out["error"] = await syn.ask(
            run_id=run_dir.name, stage=STAGE, agent=AGENT, system=SYSTEM,
            output_config=OUTPUT_CONFIG, user_message=message,
            validate=lambda r: validate(r, allowed, needle_ids, numbers))
        if reply is not None:
            out.update(status="ok", citations=syn.unique(reply["citations"]),
                       **{name: reply[name].strip() for name in TEXT_FIELDS})
    syn.write_json(run_dir / "ceo_move.json", out)
    log.info("ceo_move: status=%s needle=%s (%s) citations=%s", out["status"],
             out["sharpest_needle"], out["sharpest_needle_source"], out["citations"])
