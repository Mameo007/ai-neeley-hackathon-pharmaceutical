"""Redliner: language drift between this filing and the prior one (architecture §4.1)."""
from pharmer.hunters.harness import PROMPTS_DIR

NAME = "redliner"
BRIEF = (PROMPTS_DIR / f"{NAME}.md").read_text(encoding="utf-8")
