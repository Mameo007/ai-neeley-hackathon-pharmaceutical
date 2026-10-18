"""Gross-to-Net Tracker: a widening gap between gross and net sales (architecture §4.1)."""
from pharmer.hunters.harness import PROMPTS_DIR

NAME = "gross_to_net"
BRIEF = (PROMPTS_DIR / f"{NAME}.md").read_text(encoding="utf-8")
