"""Channel Checker: inventory or receivables growing faster than revenue (architecture §4.1)."""
from pharmer.hunters.harness import PROMPTS_DIR

NAME = "channel_checker"
BRIEF = (PROMPTS_DIR / f"{NAME}.md").read_text(encoding="utf-8")
