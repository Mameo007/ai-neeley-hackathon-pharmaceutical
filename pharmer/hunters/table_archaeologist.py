"""Table Archaeologist: restated, reclassified or dropped table numbers (architecture §4.1)."""
from pharmer.hunters.harness import PROMPTS_DIR

NAME = "table_archaeologist"
BRIEF = (PROMPTS_DIR / f"{NAME}.md").read_text(encoding="utf-8")
