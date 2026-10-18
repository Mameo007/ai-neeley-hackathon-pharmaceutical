"""Build the "How we did it" report (technical portion) as docs/how-we-did-it.pdf.

Every fact in here is sourced from docs/build-log.md, pharmer/prompts/ or the
code itself. Run: .venv/bin/python scripts/build_how_we_did_it.py
"""
from pathlib import Path

from reportlab.graphics.shapes import Drawing, Line, Polygon, Rect, String
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table,
    TableStyle,
)

OUT = Path(__file__).resolve().parent.parent / "docs" / "how-we-did-it.pdf"

INK = colors.HexColor("#1b2430")
MUTED = colors.HexColor("#5b6675")
RULE = colors.HexColor("#d9dde3")
PANEL = colors.HexColor("#f4f6f8")
AMBER = colors.HexColor("#c98a12")
OPUS = colors.HexColor("#f6d58e")
HAIKU = colors.HexColor("#b9d7f5")
CODE = colors.HexColor("#e3e6ea")
HUMAN = colors.HexColor("#a9e3c2")
RED = colors.HexColor("#b4382f")

ss = getSampleStyleSheet()
BODY = ParagraphStyle("body", parent=ss["Normal"], fontName="Helvetica",
                      fontSize=9.6, leading=13.4, textColor=INK, spaceAfter=5)
SMALL = ParagraphStyle("small", parent=BODY, fontSize=8.4, leading=11, spaceAfter=0)
CELL = ParagraphStyle("cell", parent=BODY, fontSize=8.4, leading=11, spaceAfter=0)
CELLB = ParagraphStyle("cellb", parent=CELL, fontName="Helvetica-Bold")
H1 = ParagraphStyle("h1", parent=BODY, fontName="Helvetica-Bold", fontSize=15,
                    leading=19, spaceBefore=10, spaceAfter=6, textColor=INK,
                    keepWithNext=1)
H2 = ParagraphStyle("h2", parent=BODY, fontName="Helvetica-Bold", fontSize=11,
                    leading=14, spaceBefore=8, spaceAfter=3, textColor=INK,
                    keepWithNext=1)
KICK = ParagraphStyle("kick", parent=BODY, fontName="Helvetica-Bold", fontSize=8,
                      leading=10, textColor=AMBER, spaceAfter=2, keepWithNext=1)
QUOTE = ParagraphStyle("quote", parent=BODY, fontName="Courier", fontSize=8.3,
                       leading=11, textColor=INK, alignment=TA_LEFT, spaceAfter=0)
BULLET = ParagraphStyle("bullet", parent=BODY, leftIndent=12, bulletIndent=2,
                        spaceAfter=3)


def p(text, style=BODY):
    return Paragraph(text, style)


def bullets(items):
    return [Paragraph(t, BULLET, bulletText="•") for t in items]


def section(kicker, title):
    return [p(kicker.upper(), KICK), p(title, H1)]


def grid(rows, widths, header=True):
    data = [[p(c, CELLB if header and i == 0 else CELL) for c in r]
            for i, r in enumerate(rows)]
    t = Table(data, colWidths=widths, repeatRows=1 if header else 0)
    style = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, RULE),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ]
    if header:
        style += [("BACKGROUND", (0, 0), (-1, 0), PANEL),
                  ("LINEBELOW", (0, 0), (-1, 0), 0.8, INK)]
    t.setStyle(TableStyle(style))
    return t


def prompt_box(source, text, why):
    inner = Table(
        [[p(source, KICK)], [p(text, QUOTE)], [p("<b>Why it mattered:</b> " + why, SMALL)]],
        colWidths=[6.7 * inch],
    )
    inner.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), PANEL),
        ("LINEBEFORE", (0, 0), (0, -1), 2.5, AMBER),
        ("LEFTPADDING", (0, 0), (-1, -1), 9),
        ("RIGHTPADDING", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return KeepTogether([inner, Spacer(1, 7)])


def callout(title, body_paras, accent=RED):
    rows = [[p(title, H2)]] + [[x] for x in body_paras]
    t = Table(rows, colWidths=[6.7 * inch])
    t.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.6, RULE),
        ("LINEBEFORE", (0, 0), (0, -1), 3, accent),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return KeepTogether([t, Spacer(1, 8)])


def pipeline():
    """Two rows of stage boxes, coloured by who does the work."""
    w, h = 6.7 * inch, 205
    d = Drawing(w, h)
    bw, bh, gap = 70, 46, (w - 6 * 70) / 5
    rows = [
        [("Librarian", "EDGAR, XBRL,", "DailyMed", CODE),
         ("6 Hunters", "propose leads", "w/ verbatim quotes", OPUS),
         ("Dedup", "merge same", "fact anchor", HAIKU),
         ("Fact Checker", "5 checks; code", "decides, LLM lowers", HAIKU),
         ("Quant", "LLM picks formula", "Python computes", HAIKU),
         ("Scoring", "gate + rank", "top 5", CODE)],
        [("Analyst", "picks Sharpest", "Needle + product", HUMAN),
         ("Comparison", "label grid,", "per-cell quotes", OPUS),
         ("Synthesizer", "one-sentence", "verdict", OPUS),
         ("CEO Move", "one committed", "action", OPUS),
         ("Report", "PDF with", "page footnotes", CODE),
         ("Streamlit UI", "drives both", "phases", CODE)],
    ]
    ys = [h - 60, h - 150]
    for r, row in enumerate(rows):
        y = ys[r]
        for i, (name, l1, l2, fill) in enumerate(row):
            x = i * (bw + gap)
            d.add(Rect(x, y, bw, bh, rx=4, ry=4, fillColor=fill, strokeColor=INK,
                       strokeWidth=0.6))
            d.add(String(x + bw / 2, y + bh - 13, name, fontName="Helvetica-Bold",
                         fontSize=8, textAnchor="middle", fillColor=INK))
            d.add(String(x + bw / 2, y + bh - 25, l1, fontName="Helvetica",
                         fontSize=6.4, textAnchor="middle", fillColor=INK))
            d.add(String(x + bw / 2, y + bh - 34, l2, fontName="Helvetica",
                         fontSize=6.4, textAnchor="middle", fillColor=INK))
            if i < len(row) - 1 and not (r == 1 and i == 4):
                ax, ay = x + bw + 1, y + bh / 2
                d.add(Line(ax, ay, ax + gap - 5, ay, strokeColor=INK, strokeWidth=0.8))
                d.add(Polygon([ax + gap - 2, ay, ax + gap - 7, ay + 3, ax + gap - 7, ay - 3],
                              fillColor=INK, strokeColor=INK))
    # wrap from Scoring (row 1, last) down to Analyst (row 2, first)
    sx = 5 * (bw + gap) + bw / 2
    mid = (ys[0] + ys[1] + bh) / 2
    pts = [(sx, ys[0]), (sx, mid), (bw / 2, mid), (bw / 2, ys[1] + bh + 3)]
    for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
        d.add(Line(x1, y1, x2, y2, strokeColor=INK, strokeWidth=0.8,
                   strokeDashArray=[3, 2]))
    d.add(Polygon([bw / 2, ys[1] + bh + 1, bw / 2 - 3, ys[1] + bh + 6,
                   bw / 2 + 3, ys[1] + bh + 6], fillColor=INK, strokeColor=INK))
    d.add(String(w / 2, mid + 4, "PAUSE: run stops here until a human chooses",
                 fontName="Helvetica-Bold", fontSize=7, textAnchor="middle",
                 fillColor=colors.HexColor("#1f7a4a")))
    # legend
    lx = 0
    for label, fill in [("Python code, no LLM", CODE), ("Claude Opus 5.5", OPUS),
                        ("Claude Haiku 5.5 (plus code checks)", HAIKU), ("Human", HUMAN)]:
        d.add(Rect(lx, 8, 10, 10, fillColor=fill, strokeColor=INK, strokeWidth=0.5))
        d.add(String(lx + 14, 10, label, fontName="Helvetica", fontSize=7, fillColor=MUTED))
        lx += 14 + len(label) * 3.6 + 18
    return d


def footer(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(MUTED)
    canvas.drawString(0.75 * inch, 0.5 * inch,
                      "Pharmer  |  How we did it: the build  |  Neeley AI Forward, Advanced Track")
    canvas.drawRightString(letter[0] - 0.75 * inch, 0.5 * inch, f"{doc.page}")
    canvas.restoreState()


def story():
    s = []
    # ---------------------------------------------------------------- cover
    s += [p("NEELEY AI FORWARD  |  ADVANCED TRACK  |  HOW WE DID IT", KICK),
          p("The Build: how Pharmer reads the fine print", ParagraphStyle(
              "title", parent=H1, fontSize=22, leading=27, spaceBefore=4)),
          p("The technical part of our team's How-we-did-it report: the tools, the "
            "pipeline, the prompts that carry the rules, where the AI was confidently "
            "wrong, and what we would change.", ParagraphStyle(
                "sub", parent=BODY, fontSize=11, leading=15, textColor=MUTED)),
          Spacer(1, 6)]
    s.append(callout("In one paragraph", [p(
        "We did not run the analysis as a chat. We built <b>Pharmer</b>, a Python "
        "pipeline that takes a ticker, pulls the company's own SEC filings and FDA "
        "labels, has six specialist AI 'hunters' propose findings with page-cited "
        "verbatim quotes, and then checks, sizes and ranks those findings <b>in "
        "code</b> before a human picks the Sharpest Needle. Only after that pick do "
        "the AI stages write a verdict and a CEO move, and the tool renders a "
        "footnoted PDF. Each stage's code knows nothing about a particular company, so "
        "the same build runs on any of the ten shortlist companies. A full live run "
        "on <b>Eli Lilly</b> completed on Partner A's machine; its findings are in the "
        "deck. Two people built it in parallel over one weekend: 36 merged pull "
        "requests and 503 offline tests, all passing.")], accent=AMBER))
    s.append(pipeline())
    s.append(p("Figure 1. The Pharmer pipeline. Colour shows who does the work at each "
               "stage. The dashed line is the human checkpoint: the run stops after "
               "Scoring and waits for the analyst.", SMALL))

    # ---------------------------------------------------------------- tools
    s.append(PageBreak())
    s += section("1", "The tools we used, and what each was for")
    s.append(grid([
        ["Tool", "What it was for"],
        ["<b>Claude Code</b> (Opus 5.5)",
         "Our build environment. Each partner ran their own session. It wrote the "
         "code, tests and agent prompts, ran the tests, and drove headless Chrome to "
         "screenshot the UI. Humans made every design and scope decision."],
        ["<b>Claude API: Opus 5.5</b>",
         "The judgement-heavy agents inside the pipeline: the six hunters, the "
         "Comparison Builder, the Synthesizer and the CEO Move."],
        ["<b>Claude API: Haiku 5.5</b>",
         "Mechanical stages: the Dedup judge (are two leads the same story?), the "
         "Fact Checker's claim-support judge, and Quant's formula choice."],
        ["<b>Codex</b> (OpenAI)",
         "An independent review of the codebase by a second model family, reading "
         "code that Claude wrote. It is less likely to share Claude's blind spots."],
        ["<b>BMAD skills</b> in Claude Code",
         "Structured brainstorming for the pipeline design and the UI plan, and "
         "multi-reviewer code reviews (security, edge-case and adversarial lenses) "
         "on every major stage."],
        ["<b>SEC EDGAR + XBRL companyfacts</b>",
         "Primary source: 10-K, 10-Q, 20-F and 6-K filings, plus structured figures "
         "used as a second, independent source for every number."],
        ["<b>DailyMed</b> (FDA labels)",
         "Current prescribing information for the product and its rivals."],
        ["<b>ECB reference rates</b>",
         "FX conversion for non-USD filers (Novo reports in DKK). We fetch the rates "
         "from the ECB and never take them from the model."],
        ["<b>Python stack</b>",
         "asyncio orchestrator, reportlab (PDF), Streamlit (UI), unittest with a fake "
         "Claude client. Every API call is logged per request to "
         "<font face='Courier'>logs/api_usage.csv</font>."],
        ["<b>GitHub</b>",
         "One branch and one PR per change. The repo docs (architecture, data "
         "contracts, build log) were the shared memory between two AI sessions."],
    ], [1.75 * inch, 4.95 * inch]))

    # ---------------------------------------------------------------- workflow
    s += section("2", "Workflow")
    s.append(p("How documents got into the AI", H2))
    s += bullets([
        "<b>No PDFs pasted into a chat.</b> The Librarian (pure Python, no LLM call) "
        "fetches filings from EDGAR and splits them into chunks. Each chunk carries a "
        "<font face='Courier'>chunk_id</font>, the document, the section and the "
        "filing's own <i>printed</i> page number. When a filing has no printed page "
        "number, the chunk is flagged as an inferred page.",
        "It selects the right document by content, not by filename. Novo's 4.4 MB "
        "remuneration report scored 1-2 on our financial markers. Its 2.8 MB Q2 "
        "financial report scored 17. Picking by size would have chosen the wrong file.",
        "Tables keep their cell text verbatim, for example "
        "<font face='Courier'>(1,234)</font> stays as printed, so a quote can later "
        "be string-matched against the source.",
        "Hunters never see the whole corpus in their prompt. They get four tools "
        "(<font face='Courier'>search_chunks</font>, "
        "<font face='Courier'>read_chunks</font>, "
        "<font face='Courier'>get_facts</font>, "
        "<font face='Courier'>submit_leads</font>), and "
        "<font face='Courier'>submit_leads</font> <b>rejects</b> any unknown chunk "
        "or any quote that is not a character-for-character substring of its chunk. "
        "The model has to fix its own citation before anything is recorded.",
        "FDA labels come from DailyMed and are accepted only when every package code "
        "belongs to the drug's maker. A repackager's copy of the Ozempic label was "
        "filed under the same application number, and that rule kept it out.",
    ])
    s.append(p("How work was split between people and tools", H2))
    s.append(grid([
        ["Who", "What they owned"],
        ["<b>Partner A</b> (data + checks)",
         "Librarian, Fact Checker, Quant: getting the evidence in and deciding "
         "whether it holds."],
        ["<b>Partner B</b> (agents + output)",
         "Hunters, Dedup, scoring, Comparison Builder, Synthesizer, CEO Move, PDF "
         "report, Streamlit UI."],
        ["<b>Humans only</b>",
         "Which company. The Sharpest Needle. The product and its rivals. Scope cuts "
         "(e.g. deferring the Priced-In Detector and Peer Agent). Every UI option. "
         "Any change to the shared data contracts needed both partners."],
        ["<b>AI (Claude Code)</b>",
         "Drafting code, tests and prompts against written specs, and first-pass "
         "review. Every decision it made that the code doesn't show was logged in "
         "<font face='Courier'>docs/build-log.md</font> for the humans to see."],
        ["<b>AI (in the pipeline)</b>",
         "Reading and proposing (hunters), judging similarity and claim support "
         "(Haiku), and writing the verdict and the move (Opus). It never does "
         "arithmetic and never chooses the final finding."],
    ], [1.75 * inch, 4.95 * inch]))
    s.append(Spacer(1, 6))
    s.append(p("How outputs were checked", H2))
    s.append(p("Our rule was <b>\"no fact from model memory.\"</b> So the checks live "
               "in code that the model cannot talk its way past:"))
    s.append(grid([
        ["Stage", "Check enforced in code"],
        ["Hunters", "Quote must be verbatim in the cited chunk and must contain the "
                    "anchor value. Otherwise the lead is bounced back to the model."],
        ["Fact Checker", "Five checks: quote match, value carried by the quote, "
                         "number-tie against XBRL, like-for-like period, claim "
                         "support. The deterministic checks are authoritative. The "
                         "Haiku judge can only <i>lower</i> confidence, never raise it."],
        ["Number-tie", "Tolerance is 0.5 × 10<super>-dp</super> of the printed "
                       "decimals, not a percentage. 45,042.7 ties to 45,043. A 1% "
                       "tolerance would have accepted 45,492, a different number."],
        ["Quant", "The model names a formula and its inputs. Python evaluates it "
                  "through an AST whitelist (+ - * / only). Each input must be a "
                  "corpus fact, a verbatim quote, or a stated and justified "
                  "assumption."],
        ["Synthesizer / CEO Move", "Every number in the output must appear in that "
                                   "call's input. The CEO move must cite the "
                                   "Sharpest Needle and may not start with "
                                   "'investigate', 'monitor', etc. On failure the "
                                   "model gets one retry, then the stage writes "
                                   "status <i>failed</i> and never invents."],
        ["Comparison grid", "A cell is kept only if its quote is verbatim in a chunk "
                            "sent to that column and every number is in the quote. "
                            "Otherwise the cell shows \"not disclosed\"."],
    ], [1.5 * inch, 5.2 * inch]))
    s.append(Spacer(1, 5))
    s.append(p("During development we also checked the checkers. Every test runs "
               "offline against a fake Claude client. A network-blocked run of the "
               "suite proves zero calls. We hand-tied 24 XBRL facts to printed filing "
               "tables (Novo 8/8, Lilly 12/16 same-period, with the other 4 found in "
               "comparative columns), and we hand-sampled 14 citations back to the "
               "page they claim. Every major stage then went through a three-reviewer "
               "AI code review plus Codex, and every UI change was checked on a "
               "screenshot."))

    # ---------------------------------------------------------------- prompts
    s.append(PageBreak())
    s += section("3", "Our most important prompts")
    s.append(p("The prompts below are quoted verbatim from "
               "<font face='Courier'>pharmer/prompts/</font>. Each one is "
               "byte-identical on every call, so prompt caching reuses it. Each rule "
               "in a prompt is also enforced by code, because a prompt alone is a "
               "request, not a guarantee."))
    s.append(prompt_box(
        "hunter_rules.md: shared by all six hunters",
        "Use only the corpus you can reach with your tools. Never use facts, numbers, "
        "dates, quotes or events from your own memory or training data, even if you "
        "are sure they are true.<br/>...<br/>a <font face='Courier-Bold'>quote</font> "
        "copied character-for-character from that chunk's text (same spacing, commas "
        "and line breaks; keep quotes short, one line of a table is ideal).",
        "This is the case's warning (\"a model working from memory will get numbers "
        "and quotes wrong\") turned into an operating rule. The submit_leads tool "
        "rejects any quote that breaks it."))
    s.append(prompt_box(
        "quant.md: sizing a finding",
        "You size a verified finding. You do <b>not</b> compute the answer: Python "
        "evaluates your formula against the inputs you name, so the number in the "
        "report is calculated and auditable rather than generated.<br/>...<br/>"
        "Needing a conventional rate is <b>not</b> a reason to refuse.",
        "It splits judgement (which mechanism, which rate) from arithmetic. The second "
        "line was added after a live run where the model refused to size anything "
        "(see section 4)."))
    s.append(prompt_box(
        "fact_checker.md: the claim-support judge",
        "A number can be perfectly correct and still not support the story told about "
        "it. That gap is what you are here to find.<br/>...<br/>Your verdict can lower "
        "a finding's confidence; it can never raise it above what the deterministic "
        "checks already allow.",
        "Code can confirm a number. Only a reader can tell whether \"channel stuffing\" "
        "follows from it. We gave the LLM that one narrow job and capped its power."))
    s.append(prompt_box(
        "ceo_move.md: Part 2",
        "It must be a decision, not more research: never \"investigate\", \"monitor\", "
        "\"study\", \"review\", \"explore\", \"assess\", ... and never \"we will "
        "investigate\" or similar.<br/>...<br/>You must cite the Sharpest Needle.",
        "The case says AI will argue for almost any strategy. Without this rule the "
        "model's safe answer was more research. Code rejects these verbs as well."))
    s.append(prompt_box(
        "Our standing instruction to Claude Code (CLAUDE.md, every session)",
        "No fact from model memory: every number and quote comes from the corpus."
        "<br/>...<br/>Append to docs/build-log.md (never edit old entries) whenever you: "
        "make a decision the code doesn't show, catch the AI being confidently wrong, "
        "or finish a test.",
        "The build log is how two separate AI sessions stayed consistent. It is also "
        "the raw material for this report: every 'wrong' case below was logged when "
        "it happened."))

    # ---------------------------------------------------------------- wrong
    s.append(PageBreak())
    s += section("4", "Where the AI was confidently wrong, and how we caught it")
    s.append(callout("Case A: a write-down that would have printed as a gain", [
        p("<b>What happened.</b> On the first live Quant run the prompt asked in "
          "prose for \"negative for damage\". The model returned a <b>positive</b> "
          "magnitude for a write-down on every finding, with no hedging. In the "
          "report, a loss would have appeared as a gain."),
        p("<b>How we caught it.</b> By reading the first live run's output rather than "
          "trusting that the stage finished without errors."),
        p("<b>Fix.</b> The sign is no longer the model's choice. "
          "<font face='Courier'>direction</font> is a required "
          "<font face='Courier'>cost</font>/<font face='Courier'>benefit</font> "
          "field, and Python applies the sign. The same week, two live runs on "
          "<i>identical</i> Lilly evidence picked different write-down rates (one "
          "sized the two findings at -$36.7m / -$276.2m, the other at -$888.0m / "
          "-$462.4m), so the recommended finding <b>flipped</b>. Every figure was "
          "auditable, but the assumptions were not stable between runs. That is why a "
          "human, not the ranking, names the Sharpest Needle."),
    ]))
    s.append(callout("Case B: the same mistake, twice, through two different doors", [
        p("<b>First door.</b> A web-search summary said Novo's Q2 sales grew "
          "<b>+7% at constant exchange rates</b>. The 6-K table showed reported "
          "<b>+3%</b>. Caught by opening the primary filing."),
        p("<b>Second door.</b> Days later, inside our own Fact Checker, the code "
          "matched table rows by prefix, so Novo's row <font face='Courier'>Net sales "
          "at CER</font> counted as <font face='Courier'>revenue</font>. That would "
          "have put a constant-currency figure into a reported-basis trend: the same "
          "confusion, coming back in. Caught by an independent reviewer. Labels now "
          "match by equality, with a regression test to keep it that way."),
    ]))
    s.append(p("More from the build log (33 'wrong' entries in total)", H2))
    s.append(grid([
        ["What the AI asserted", "Reality", "How we caught it"],
        ["Proposed openFDA as a backup label source.",
         "It was missing Novo's own Ozempic label.", "Compared against DailyMed."],
        ["Matched \"Roche\" to a company in the SEC list.",
         "It was Sinopec Shanghai PETROCHEMICAL (a substring match).",
         "Read the probe output."],
        ["Pfizer / J&J prior-year quarters \"missing\".",
         "They use 52/53-week fiscal quarters (Jun 28 vs Jun 29).",
         "Listed the actual 10-Q dates."],
        ["A filing's page number is the last line of each page.",
         "True for Lilly. Novo prints it in a running header, so every Novo page "
         "came out unverified.",
         "Ran the second company instead of trusting the first."],
        ["The PDF said \"Market view: not priced in\".",
         "No priced-in check had run. It was a default, not a finding.",
         "Reviewing the rendered report."],
        ["A revenue trend from FY2007 to Q2 2026 in one series.",
         "Mixed quarters with full years, so every Lilly finding was flagged "
         "\"reversal\".",
         "Ran on the real Lilly corpus, not fixtures."],
        ["\"No API key\" handled gracefully, and the test passed.",
         "The real path raised TypeError and killed the stage. The test injected a "
         "different error.",
         "An edge-case reviewer ran it for real."],
        ["The offline test suite makes no network calls.",
         "It was silently hitting the ECB live: 6.5599, not the fixture's 6.4407.",
         "Two tests got the wrong FX rate."],
        ["The hunter prompt is neutral.",
         "Its example (+25% receivables vs +3% sales) mirrored the real Novo "
         "story, priming the answer.",
         "Three-reviewer code review."],
    ], [2.2 * inch, 2.6 * inch, 1.9 * inch]))
    s.append(Spacer(1, 6))
    s.append(p("<b>The pattern.</b> Almost nothing was caught by asking the AI "
               "whether it was sure. It was caught by going back to the primary "
               "document, by running a <i>second</i> real company, by a reviewer with "
               "a different lens or a different model (Codex), or by a test written to "
               "fail first. The checker needs checking too: one of our own "
               "verification scripts mis-paged three citations that were in fact "
               "correct."))

    # ---------------------------------------------------------------- differently
    s += section("5", "What we would do differently")
    s += bullets([
        "<b>Prove the API works in the first hour.</b> Most stages were built and "
        "tested against a fake client before any real call succeeded. We lost hours "
        "to a truncated key (401) and then an organisation-scoped key that needed a "
        "workspace header (400). Today we would run our 5-check API smoke test before "
        "writing a single agent.",
        "<b>One corpus format from the start.</b> Partner B's sample corpus joined "
        "table cells with spaces and the Librarian used tabs. 132 chunk IDs collided "
        "and only 2 texts matched. We patched it in the Fact Checker. A shared fixture "
        "on day one would have prevented it.",
        "<b>Pin Quant's assumptions.</b> A reviewed table of write-down rates per "
        "metric would make the ranking stable across runs (Case A).",
        "<b>Get transcripts and peer filings in earlier.</b> They were the inputs for "
        "the Call Decoder, Peer Agent and Priced-In Detector, so those stages were "
        "cut. Without them, \"not obvious\" is not measured by the tool. The human "
        "Sharpest Needle pick is the safeguard.",
        "<b>Design the outputs from the case rubric backwards.</b> The CEO move file "
        "has no budget or rejected-alternatives fields, and Quant's native-currency "
        "figure has no contract field. The case asks for both. We found these gaps "
        "late, while building the UI.",
        "<b>Never change demo configuration late without a live check.</b> A "
        "last-minute PR that switched off unbuilt stages caused a regression after "
        "the push and was reverted.",
    ])
    return s


def main():
    doc = SimpleDocTemplate(
        str(OUT), pagesize=letter, leftMargin=0.9 * inch, rightMargin=0.9 * inch,
        topMargin=0.75 * inch, bottomMargin=0.8 * inch,
        title="Pharmer - How we did it (the build)", author="Pharmer team",
    )
    doc.build(story(), onFirstPage=footer, onLaterPages=footer)
    print(OUT)


if __name__ == "__main__":
    main()
