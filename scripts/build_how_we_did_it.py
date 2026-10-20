"""Build the "How we did it" report (technical portion) as docs/how-we-did-it.pdf.

Every fact in here is sourced from docs/build-log.md, pharmer/prompts/ or the
code itself. Run: .venv/bin/python scripts/build_how_we_did_it.py
"""
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate

OUT = Path(__file__).resolve().parent.parent / "docs" / "how-we-did-it.pdf"

INK = colors.HexColor("#1b2430")
MUTED = colors.HexColor("#5b6675")
AMBER = colors.HexColor("#c98a12")

BODY = ParagraphStyle("body", fontName="Helvetica", fontSize=9.8, leading=13.4,
                      textColor=INK, spaceAfter=4)
H2 = ParagraphStyle("h2", parent=BODY, fontName="Helvetica-Bold", fontSize=11,
                    spaceBefore=8, spaceAfter=2, keepWithNext=1)
KICK = ParagraphStyle("kick", parent=BODY, fontName="Helvetica-Bold", fontSize=8,
                      textColor=AMBER, spaceAfter=0)
TITLE = ParagraphStyle("title", parent=BODY, fontName="Helvetica-Bold", fontSize=18,
                       leading=23, spaceAfter=2)
SUB = ParagraphStyle("sub", parent=BODY, textColor=MUTED, spaceAfter=6)

SECTIONS = [
    ("Tools and what each was for",
     "We built a reusable Python pipeline, <b>Pharmer</b>, using <b>Claude Code</b> "
     "(Opus 5.5) as our coding environment and <b>Codex</b> to independently review "
     "the codebase. Inside the pipeline, <b>Claude Opus 5.5</b> does the judgement "
     "work: six \"hunter\" agents that look for findings, the product comparison, "
     "the verdict and the CEO move. <b>Claude Haiku 5.5</b> does the mechanical "
     "checks: merging duplicate findings, judging whether a quote supports its "
     "claim, and choosing a formula to size a finding. Filings and figures come "
     "from <b>SEC EDGAR</b> and its XBRL data, labels from <b>DailyMed</b> and FX "
     "rates from the <b>ECB</b>. <b>Streamlit</b> runs the UI, and <b>reportlab</b> "
     "renders the PDF."),
    ("Workflow",
     "No documents were pasted into a chat. Code fetches each filing, splits it into "
     "chunks tagged with the filing's printed page number, and the agents search and "
     "read those chunks through tools. A finding is rejected unless its quote "
     "matches the filing word for word. Partner A built the data and checking "
     "stages, and Partner B built the agents and the output. Shared data contracts "
     "and an append-only build log kept two separate AI sessions consistent. Humans "
     "chose the company, the Sharpest Needle, the product and every scope cut. "
     "Outputs are checked in code, not by asking the model. Every number is tied to "
     "a second source (XBRL), Python does all the arithmetic, the AI judge can only "
     "lower a finding's confidence, and every number in the verdict or CEO move must "
     "appear in that call's input. The code itself passed 503 offline tests and "
     "several multi-reviewer code reviews. A full live run on Eli Lilly completed."),
    ("Most important prompts",
     "Three rules carried most of the weight, and each is also enforced in code. "
     "For every hunter: <i>\"Never use facts, numbers, dates, quotes or events from "
     "your own memory or training data, even if you are sure they are true.\"</i> "
     "For sizing a finding: <i>\"You do not compute the answer: Python evaluates "
     "your formula against the inputs you name.\"</i> For the CEO move: <i>\"It must "
     "be a decision, not more research: never 'investigate', 'monitor', "
     "...\"</i>"),
    ("Where the AI was confidently wrong",
     "On the first live run, the sizing stage returned a <b>positive</b> number for "
     "every write-down, so a loss would have printed as a gain. We caught it by "
     "reading the output rather than trusting that the stage finished cleanly. The "
     "model no longer sets the sign: it must label each amount a cost or a benefit, "
     "and Python applies the sign. Earlier, a web-search summary stated that Novo's "
     "Q2 sales grew <b>+7%</b> at constant exchange rates, and the 6-K showed "
     "<b>+3%</b> reported. We caught it by opening the filing. The same confusion "
     "later came back through our own code, which matched the table row \"Net "
     "sales at CER\" as revenue. An independent reviewer caught that one. In total "
     "we logged 33 such cases, almost all caught by going back to the primary "
     "source or by a second reviewer."),
    ("What we would do differently",
     "First, verify the API key on day one. We lost hours to key errors and built "
     "most stages against a fake client before any real call worked. Second, fix "
     "the sizing stage's assumed rates in a reviewed table: two runs on identical "
     "Lilly evidence chose different write-down rates, and the recommended finding "
     "flipped. Third, load earnings-call transcripts and peer filings earlier, so "
     "the tool can measure whether a finding is already priced in, instead of "
     "leaving that judgement entirely to the analyst."),
]


def main():
    doc = SimpleDocTemplate(
        str(OUT), pagesize=letter, leftMargin=0.9 * inch, rightMargin=0.9 * inch,
        topMargin=0.7 * inch, bottomMargin=0.7 * inch,
        title="Pharmer - How we did it (the build)", author="Pharmer team",
    )
    story = [
        Paragraph("NEELEY AI FORWARD  |  HOW WE DID IT", KICK),
        Paragraph("The Build: how Pharmer reads the fine print", TITLE),
        Paragraph("The technical part of our team's report.", SUB),
    ]
    for title, text in SECTIONS:
        story += [Paragraph(title, H2), Paragraph(text, BODY)]
    doc.build(story)
    print(OUT)


if __name__ == "__main__":
    main()
