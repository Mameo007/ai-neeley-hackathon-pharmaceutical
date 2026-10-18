"""Footnote Miner: material items buried in the notes (architecture §4.1)."""
from pharmer.hunters.harness import PROMPTS_DIR

NAME = "footnote_miner"
BRIEF = (PROMPTS_DIR / f"{NAME}.md").read_text(encoding="utf-8")
