"""Exec Report (architecture §4.10): the run as one sell-side-style research note in PDF.

Two steps. `build_model` reads runs/<run_id>/findings.json (Contract 2, after scoring), run.json,
synthesis.json, ceo_move.json, comparison.json, manifest.json and chunks.jsonl (Contract 1) and
returns the report as plain data: masthead, thesis, warnings, numbered findings, comparison,
CEO-move callout and footnotes. `render_pdf` lays that data out with reportlab. No Claude call:
every number and quote printed comes from those files; the report adds only fixed labels.

A missing or failed upstream file is shown as a warning in the report, never raised; only a
missing findings.json raises.
"""
import json
import logging
from pathlib import Path
from xml.sax.saxutils import escape

from pharmer import ceo_move
from pharmer import synthesizer as syn

log = logging.getLogger(__name__)

MASTHEAD = "PHARMER RESEARCH"
TAGLINE = "We find the needle in the haystack."
FINDINGS_EXPECTED = 5  # §5.1.4: the AI selects 5

PRICED_IN_WORDS = {"priced_in": "priced in", "partly_priced_in": "partly priced in",
                   "not_priced_in": "not priced in"}
CEO_FIELDS = [("action", "Action"), ("timeframe", "Timeframe"), ("trade_off", "Trade-off"),
              ("downside_if_wrong", "Downside if wrong"), ("why_it_works", "Why it works")]
NEEDLE_HUMAN = "Named by the analyst."
NEEDLE_FALLBACK = "AI-recommended; not yet confirmed by the analyst."
NO_COMPARISON = "Comparison table not built for this run."
# What the pipeline does, for a reader meeting the report cold. Describes only stages that
# run in this pipeline; anything a particular run did not complete is named in "Coverage and
# limitations" at the end, never implied to have happened here.
EXEC_OVERVIEW = (
    "Pharmer Research uses specialized AI agents to collect and analyze pharmaceutical "
    "filings, identify five evidence-backed financial findings, and highlight the most "
    "significant discovery. The system selects a key product, compares it with competing "
    "treatments using available evidence, and develops a 24-month strategic recommendation "
    "for the CEO, outlining actions, financial implications, risks, and measurable "
    "milestones.")


class Footnotes:
    """Footnote numbers in the order they are first used; the same text reuses its number."""

    def __init__(self, chunks, docs):
        self.chunks, self.docs = chunks, docs
        self.numbers, self.unresolved = {}, []

    def _number(self, text):
        if text not in self.numbers:
            self.numbers[text] = len(self.numbers) + 1
        return self.numbers[text]

    def source(self, chunk_id):
        """Footnote text for one chunk_id: "<company> <fiscal_period> <doc_type>, p.<page>"."""
        chunk = self.chunks.get(chunk_id)
        doc = self.docs.get(chunk.get("doc_id")) if chunk else None
        if not doc or chunk.get("page") is None:
            if chunk_id not in self.unresolved:
                self.unresolved.append(chunk_id)
            return f"{chunk_id} – citation not found in this run's corpus"
        return (f"{doc.get('company')} {doc.get('fiscal_period')} {doc.get('doc_type')}, "
                f"p.{chunk['page']}")

    def cite(self, chunk_ids):
        return syn.unique(self._number(self.source(c)) for c in chunk_ids)

    def cite_text(self, text):
        """A footnote that is not a chunk, e.g. a cited finding id that is not selected."""
        return [self._number(text)]

    def as_list(self):
        return [{"number": n, "text": t} for t, n in self.numbers.items()]


def _load_corpus(run_dir, warnings):
    """(chunk_id -> chunk, doc_id -> manifest entry); a missing file leaves its side empty."""
    chunks, docs = {}, {}
    path = run_dir / "chunks.jsonl"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                chunk = json.loads(line)
            except ValueError:
                continue
            if isinstance(chunk, dict) and "chunk_id" in chunk:
                chunks[chunk["chunk_id"]] = chunk
    else:
        warnings.append("chunks.jsonl is missing, so citations cannot be resolved to pages.")
    manifest = syn.read_json_or_none(run_dir / "manifest.json")
    if isinstance(manifest, list):
        docs = {d["doc_id"]: d for d in manifest if isinstance(d, dict) and "doc_id" in d}
    else:
        warnings.append("manifest.json is missing or unreadable, so citations cannot be "
                        "resolved to filings.")
    return chunks, docs


def _fmt(value):
    return f"{value:,}" if isinstance(value, int) and not isinstance(value, bool) else str(value)


def _anchor_text(anchor):
    anchor = anchor or {}
    value = " ".join(_fmt(anchor[k]) for k in ("currency", "value", "unit")
                     if anchor.get(k) is not None)
    head = ", ".join(str(anchor[k]).replace("_", " ")
                     for k in ("entity", "metric", "fiscal_period")
                     if anchor.get(k))
    return f"{head}: {value}" if value else head


def _best_score(reasonings):
    scores = [r["score"] for r in reasonings if isinstance(r.get("score"), (int, float))]
    return max(scores) if scores else None


def _reasoning_model(reasoning, finding, notes):
    flags = list(reasoning.get("flags") or [])
    if finding.get("contested") and "contested" not in flags:
        flags.insert(0, "contested")
    impact = reasoning.get("impact") or {}
    impact_parts = []
    if impact.get("eps_pct") is not None:
        impact_parts.append(f"EPS impact {impact['eps_pct']}%")
    if impact.get("usd_m") is not None:
        impact_parts.append(f"USD {impact['usd_m']}m")
    hits = syn.priced_in_hits(reasoning)
    return {
        "reasoning_id": reasoning["reasoning_id"],
        "badge": reasoning.get("confidence") or "Unverified",
        "claim": reasoning.get("claim") or "",
        "labels": [f.replace("_", " ") for f in flags],
        "quotes": [{"text": c.get("quote") or "", "notes": notes.cite([c.get("chunk_id")])}
                   for c in reasoning.get("citations") or []],
        "impact": "; ".join(impact_parts) or None,
        "priced_in": ("priced-in check not run" if hits is None
                      else f"{_fmt(hits)} priced-in hits"),
    }


def _chunk_ids(cited, selected):
    """Chunk ids behind cited finding/reasoning ids (a finding stands for its selected
    reasonings), and the cited ids that match no selected finding."""
    out, unknown = [], []
    for cid in cited:
        hit = False
        for finding, reasonings in selected:
            for r in reasonings:
                if cid in (finding["finding_id"], r["reasoning_id"]):
                    hit = True
                    out += [c.get("chunk_id") for c in r.get("citations") or []]
        if not hit:
            unknown.append(cid)
    return out, unknown


def _cite_ids(cited, selected, notes):
    chunk_ids, unknown = _chunk_ids(cited, selected)
    numbers = notes.cite(chunk_ids)
    for cid in unknown:
        numbers += notes.cite_text(f"{cid} – cited finding is not among the selected "
                                   "findings")
    return syn.unique(numbers)


def build_model(run_dir) -> dict:
    """The report as plain data, from the run folder's contract files (see module docstring)."""
    run_dir = Path(run_dir)
    findings = json.loads((run_dir / "findings.json").read_text(encoding="utf-8"))
    selected = syn.selected_findings(findings)
    run_info = syn.read_json_or_none(run_dir / "run.json")
    synthesis = syn.read_json_or_none(run_dir / "synthesis.json")
    move = syn.read_json_or_none(run_dir / "ceo_move.json")
    grid = syn.read_json_or_none(run_dir / "comparison.json")
    warnings = []
    for name, obj in (("synthesis.json", synthesis), ("ceo_move.json", move)):
        if obj is not None and not isinstance(obj, dict):
            warnings.append(f"{name} is not a JSON object; treated as missing.")
    synthesis = synthesis if isinstance(synthesis, dict) else None
    move = move if isinstance(move, dict) else None
    chunks, docs = _load_corpus(run_dir, warnings)
    notes = Footnotes(chunks, docs)

    product = run_info.get("product") if isinstance(run_info, dict) else None
    product = product.strip() if isinstance(product, str) and product.strip() else None
    tickers = syn.unique(f.get("ticker") for f in findings if f.get("ticker"))

    # Sharpest Needle: the same rule CEO Move uses, so report and move agree.
    needle = source = None
    if selected:
        needle, source, error = ceo_move.resolve_needle(run_info, selected)
        if error:
            warnings.append(f"Sharpest Needle unavailable: {error}.")
        elif source == "recommended_fallback":
            warnings.append(f"Sharpest Needle {needle}: {NEEDLE_FALLBACK}")
    else:
        warnings.append("No selected findings in findings.json.")

    # Whether the Priced-In Detector actually looked at anything in this run, read from the
    # findings themselves rather than from the pipeline file - a reasoning carries hits only
    # if it was checked. Scoring's obscurity is the unchecked default for all of them when
    # this is False, so the ranking is materiality alone and must not be described as more.
    priced_in_assessed = any(syn.priced_in_hits(r) is not None
                             for _f, rs in selected for r in rs)
    if selected and not priced_in_assessed:
        warnings.append("Market pricing not assessed: the Priced-In Detector did not run, so "
                        "findings are ranked by materiality alone and no finding is claimed "
                        "to be priced in or overlooked by the market.")

    n_selected = sum(len(rs) for _, rs in selected)
    if n_selected < FINDINGS_EXPECTED:
        warnings.append(f"Only {n_selected} of {FINDINGS_EXPECTED} findings were selected for "
                        "this run.")
    if selected and not any(f.get("is_label_finding") for f, _ in selected):
        warnings.append("No label/product finding among the selected findings: every finding "
                        "rests on financial filings.")

    # Thesis: headline from the needle's own claims, then the Synthesizer's verdict.
    thesis = {"needle": needle, "needle_source": source, "headline": None,
              "needle_note": None, "verdict": None, "priced_in": None,
              "priced_in_assessed": priced_in_assessed, "notes": []}
    for finding, reasonings in selected:
        if finding["finding_id"] == needle:
            thesis["headline"] = (" ".join(r.get("claim") or "" for r in reasonings).strip()
                                  or _anchor_text(finding.get("anchor")) or needle)
            thesis["needle_note"] = NEEDLE_HUMAN if source == "human" else NEEDLE_FALLBACK
    if synthesis is None:
        warnings.append("Synthesizer has not run for this run (synthesis.json missing); no "
                        "growth-limiter verdict.")
    elif synthesis.get("status") != "ok":
        warnings.append(f"Synthesizer failed: {synthesis.get('error') or 'no error recorded'}.")
    elif ceo_move.usable_verdict(synthesis, selected) is None:
        warnings.append("synthesis.json is out of date or blank (it cites findings that are no "
                        "longer selected); verdict not shown.")
    else:
        thesis["verdict"] = synthesis["verdict"].strip()
        # The Synthesizer is told to count an unchecked reasoning as zero hits, so with the
        # Priced-In Detector not run it always lands on "not priced in" - a market claim
        # nobody checked. When nothing was checked, say that instead of reporting the call.
        thesis["priced_in"] = (PRICED_IN_WORDS.get(synthesis.get("priced_in_call"))
                               if priced_in_assessed else None)
        thesis["notes"] = _cite_ids(synthesis["citations"], selected, notes)

    # Findings: needle first, then by best selected-reasoning score (unscored last).
    order = sorted(range(len(selected)), key=lambda i: (
        selected[i][0]["finding_id"] != needle,
        _best_score(selected[i][1]) is None,
        -(_best_score(selected[i][1]) or 0), i))
    rows = []
    for number, i in enumerate(order, start=1):
        finding, reasonings = selected[i]
        labels = []
        if finding["finding_id"] == needle:
            labels.append("Sharpest Needle")
        if finding.get("recommended"):
            labels.append("Recommended")
        if finding.get("is_label_finding"):
            labels.append("label/product")
        rows.append({"number": number, "finding_id": finding["finding_id"],
                     "anchor": _anchor_text(finding.get("anchor")), "labels": labels,
                     "reasonings": [_reasoning_model(r, finding, notes) for r in reasonings]})
    numbers = {row["finding_id"]: row["number"] for row in rows}
    thesis["needle_number"] = numbers.get(needle)

    # Comparison grid (Comparison Builder, not built yet; shape is a proposal).
    if grid is None:
        comparison = {"status": "missing", "note": NO_COMPARISON, "columns": [], "rows": []}
    elif (isinstance(grid, dict) and isinstance(grid.get("columns"), list) and grid["columns"]
          and isinstance(grid.get("rows"), list)
          and all(isinstance(r, list) for r in grid["rows"])):
        width = len(grid["columns"])
        comparison = {"status": "ok", "note": None, "columns": [str(c) for c in grid["columns"]],
                      "rows": [[_fmt(c) for c in r[:width]]
                               + ["not disclosed"] * (width - len(r)) for r in grid["rows"]]}
    elif isinstance(grid, list) and grid and all(isinstance(r, dict) for r in grid):
        columns = syn.unique(k for r in grid for k in r)
        comparison = {"status": "ok", "note": None, "columns": columns,
                      "rows": [[_fmt(r.get(k, "not disclosed")) for k in columns] for r in grid]}
    else:
        comparison = {"status": "unrecognized", "columns": [], "rows": [],
                      "note": "comparison.json is present but in an unrecognized shape; table "
                              "not shown."}
        warnings.append(comparison["note"])

    # CEO move callout.
    callout = {"status": "missing", "fields": [], "notes": [], "error": None, "stale": None}
    if move is None:
        callout["error"] = "CEO Move has not run for this run (ceo_move.json missing)."
    elif move.get("status") != "ok":
        callout.update(status="failed",
                       error=f"CEO Move failed: {move.get('error') or 'no error recorded'}.")
    else:
        callout.update(status="ok", fields=[(label, str(move.get(key) or "").strip())
                                            for key, label in CEO_FIELDS],
                       notes=_cite_ids(move.get("citations") or [], selected, notes))
        if needle and move.get("sharpest_needle") != needle:
            callout["stale"] = (f"The CEO move was written for {move.get('sharpest_needle')}, "
                                f"but the Sharpest Needle is now {needle}; re-run CEO Move.")
            warnings.append(callout["stale"])
    if callout["error"]:
        warnings.append(callout["error"])

    if notes.unresolved:
        shown = ", ".join(map(str, notes.unresolved[:10]))
        more = len(notes.unresolved) - 10
        warnings.append(f"{len(notes.unresolved)} citations not found in this run's corpus: "
                        f"{shown}{f' and {more} more (see Sources)' if more > 0 else ''}.")
    for w in warnings:
        log.warning("report: %s", w)
    return {"masthead": MASTHEAD, "tagline": TAGLINE, "run_id": run_dir.name,
            "tickers": tickers, "product": product, "warnings": warnings, "thesis": thesis,
            "findings": rows, "comparison": comparison, "ceo_move": callout,
            "footnotes": notes.as_list()}


# --- PDF rendering ---------------------------------------------------------------------------

# Helvetica (a PDF base font) covers cp1252: en dash, curly quotes, euro sign. Anything else is
# mapped to a plain equivalent, then "?", so an unusual character never breaks the render.
_SUBSTITUTES = {"−": "-", "‐": "-", "‑": "-", "≤": "<=", "≥": ">=",
                "×": "x", "≈": "~", "→": "->", " ": " ", " ": " ",
                " ": " "}


def pdf_text(text):
    """text made safe for Helvetica and for reportlab's paragraph markup."""
    text = "".join(_SUBSTITUTES.get(ch, ch) for ch in str(text))
    text = text.encode("cp1252", errors="replace").decode("cp1252")
    return escape(text)


def _marks(numbers):
    return "".join(f"[{n}]" for n in numbers)


def render_pdf(model, path, compress=True) -> None:
    """Write the report model to path as a PDF (letter size, no hyperlinks). compress=False
    leaves page text readable in the file bytes, for tests."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import (HRFlowable, KeepTogether, Paragraph, SimpleDocTemplate,
                                    Spacer, Table, TableStyle)

    base = getSampleStyleSheet()["Normal"]
    style = {
        "mast": ParagraphStyle("mast", base, fontName="Helvetica-Bold", fontSize=22, leading=26),
        "tag": ParagraphStyle("tag", base, fontName="Helvetica-Oblique", fontSize=11,
                              leading=14, textColor=colors.HexColor("#444444")),
        "meta": ParagraphStyle("meta", base, fontSize=8.5, leading=11,
                               textColor=colors.HexColor("#555555")),
        "h": ParagraphStyle("h", base, fontName="Helvetica-Bold", fontSize=12.5, leading=16,
                            spaceBefore=10, spaceAfter=4),
        "head": ParagraphStyle("head", base, fontName="Helvetica-Bold", fontSize=11.5,
                               leading=15, spaceAfter=4),
        "body": ParagraphStyle("body", base, fontSize=9.5, leading=13, spaceAfter=3),
        "item": ParagraphStyle("item", base, fontSize=9.5, leading=13, leftIndent=14,
                               spaceAfter=2),
        "small": ParagraphStyle("small", base, fontSize=8.5, leading=11, leftIndent=14,
                                textColor=colors.HexColor("#444444")),
        "cell": ParagraphStyle("cell", base, fontSize=8, leading=10),
        "note": ParagraphStyle("note", base, fontSize=7.5, leading=10),
    }
    badge_colors = {"High": "#1b6e3a", "Medium": "#9a6a00", "Low": "#a12a2a"}
    t = pdf_text

    def boxed(paragraphs, fill, border):
        # One row per paragraph, so a long box can split across pages instead of failing.
        box = Table([[p] for p in paragraphs], colWidths=[6.5 * inch])
        box.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 1, colors.HexColor(border)),
                                 ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(fill)),
                                 ("LEFTPADDING", (0, 0), (-1, -1), 8),
                                 ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                                 ("TOPPADDING", (0, 0), (-1, -1), 6),
                                 ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
        return box

    story = [Paragraph(t(model["masthead"]), style["mast"]),
             Paragraph(t(model["tagline"]), style["tag"]), Spacer(1, 4),
             HRFlowable(width="100%", thickness=1.5, color=colors.black), Spacer(1, 3)]
    meta = [", ".join(model["tickers"]) or "No ticker",
            f"Product: {model['product'] or 'not yet confirmed by the analyst'}",
            f"Run {model['run_id']}"]
    story += [Paragraph(t("  |  ".join(meta)), style["meta"]), Spacer(1, 8)]

    # Executive Overview: what the pipeline does, in plain prose, ahead of the thesis.
    # The run's genuine coverage warnings are not dropped - they move to "Coverage and
    # limitations" at the end of the report, where they stay readable without the
    # yellow box dominating the first page.
    story += [Paragraph("Executive Overview", style["h"]),
              Paragraph(t(EXEC_OVERVIEW), style["body"]), Spacer(1, 8)]

    thesis = model["thesis"]
    story.append(Paragraph("Thesis", style["h"]))
    if thesis["headline"]:
        ref = f" (Finding {thesis['needle_number']})" if thesis.get("needle_number") else ""
        story.append(Paragraph(f"Sharpest Needle: {t(thesis['headline'])}{ref}", style["head"]))
        story.append(Paragraph(f"<i>{t(thesis['needle_note'])}</i>", style["body"]))
    else:
        story.append(Paragraph("No Sharpest Needle for this run.", style["head"]))
    if thesis["verdict"]:
        line = f"{t(thesis['verdict'])} {_marks(thesis['notes'])}".strip()
        if thesis["priced_in"]:
            line += f" <b>Market view: {t(thesis['priced_in'])}.</b>"
        elif not thesis["priced_in_assessed"]:
            line += " <b>Market pricing not assessed.</b>"
        story.append(Paragraph(line, style["body"]))
    else:
        story.append(Paragraph("Growth-limiter verdict unavailable for this run (see "
                               "warnings).", style["body"]))

    story.append(Paragraph("Findings", style["h"]))
    if not model["findings"]:
        story.append(Paragraph("No findings were selected.", style["body"]))
    for row in model["findings"]:
        labels = f" [{t(', '.join(row['labels']))}]" if row["labels"] else ""
        block = [Paragraph(f"{row['number']}. {t(row['anchor'])}{labels}", style["head"])]
        for r in row["reasonings"]:
            color = badge_colors.get(r["badge"], "#555555")
            tags = f" <i>({t(', '.join(r['labels']))})</i>" if r["labels"] else ""
            block.append(Paragraph(f'<font color="{color}"><b>[{t(r["badge"]).upper()}]</b>'
                                   f'</font> {t(r["claim"])}{tags}', style["item"]))
            for q in r["quotes"]:
                block.append(Paragraph(f"“{t(q['text'])}” {_marks(q['notes'])}",
                                       style["small"]))
            extra = [x for x in (r["impact"], r["priced_in"]) if x]
            block.append(Paragraph(t("; ".join(extra)), style["small"]))
        block.append(Spacer(1, 4))
        story.append(KeepTogether(block))

    comparison = model["comparison"]
    story.append(Paragraph("Product comparison", style["h"]))
    if comparison["status"] == "ok" and comparison["columns"]:
        data = [[Paragraph(f"<b>{t(c)}</b>", style["cell"]) for c in comparison["columns"]]]
        data += [[Paragraph(t(c), style["cell"]) for c in r] for r in comparison["rows"]]
        width = 6.5 * inch / len(comparison["columns"])
        table = Table(data, colWidths=[width] * len(comparison["columns"]), repeatRows=1)
        table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                                   ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")),
                                   ("VALIGN", (0, 0), (-1, -1), "TOP")]))
        story.append(table)
    else:
        story.append(Paragraph(t(comparison["note"] or NO_COMPARISON), style["body"]))

    callout = model["ceo_move"]
    heading = Paragraph("24-Month Strategic Move for the CEO", style["h"])
    if callout["status"] == "ok":
        inner = [Paragraph(f"<b>{t(label)}:</b> {t(text)}", style["body"])
                 for label, text in callout["fields"]]
        if callout["stale"]:
            inner.insert(0, Paragraph(f"<b>Out of date.</b> {t(callout['stale'])}",
                                      style["body"]))
        if callout["notes"]:
            inner.append(Paragraph(f"Sources: {_marks(callout['notes'])}", style["body"]))
        box = boxed(inner, "#eef3fb", "#1f3b73")
    else:
        box = boxed([Paragraph(f"<b>CEO move unavailable.</b> {t(callout['error'])}",
                               style["body"])], "#fbeeee", "#a12a2a")
    story.append(KeepTogether([heading, box]))

    if model["warnings"]:
        story.append(Paragraph("Coverage and limitations", style["h"]))
        for w in model["warnings"]:
            # An en dash, not a bullet: reportlab's WinAnsi core-font encoding has no
            # U+2022 and prints it as a DEL box.
            story.append(Paragraph(f"\u2013 {t(w)}", style["small"]))
        story.append(Spacer(1, 8))

    story.append(Paragraph("Sources", style["h"]))
    if not model["footnotes"]:
        story.append(Paragraph("No citations.", style["note"]))
    for note in model["footnotes"]:
        story.append(Paragraph(f"[{note['number']}] {t(note['text'])}", style["note"]))

    doc = SimpleDocTemplate(str(path), pagesize=letter, invariant=1, title=MASTHEAD,
                            pageCompression=1 if compress else 0,
                            author=MASTHEAD, leftMargin=inch, rightMargin=inch,
                            topMargin=0.8 * inch, bottomMargin=0.8 * inch)
    doc.build(story)


async def run(run_dir: Path) -> None:
    """Read the run folder's contract files and write run_dir/report.pdf."""
    run_dir = Path(run_dir)
    model = build_model(run_dir)
    render_pdf(model, run_dir / "report.pdf")
    log.info("report: wrote %s (%d findings, %d footnotes, %d warnings)",
             run_dir / "report.pdf", len(model["findings"]), len(model["footnotes"]),
             len(model["warnings"]))
