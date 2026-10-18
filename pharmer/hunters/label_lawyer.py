"""Label Lawyer: product fine print - label breadth, boxed warnings, REMS (architecture §4.1).

Company-wide pass only; the product-specific pass after the human pause is deferred (§9.5).
"""
from pharmer.hunters.harness import PROMPTS_DIR

NAME = "label_lawyer"
BRIEF = (PROMPTS_DIR / f"{NAME}.md").read_text(encoding="utf-8")
