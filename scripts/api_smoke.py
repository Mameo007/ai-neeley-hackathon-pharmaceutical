"""Real-API smoke check for pharmer/llm.py: does every request shape the stages use work?

Five small requests, all through llm.call() so each one lands in logs/api_usage.csv:
  1. haiku       - a plain Haiku call (Dedup judge, Fact Checker, Quant)
  2. opus        - a plain Opus call with the refusal fallback (hunters, Synthesizer, CEO Move)
  3. json_schema - Opus with output_config json_schema (Dedup, Synthesizer, CEO Move)
  4. cache_write - Opus with a long fixed system prompt (writes the prompt cache)
  5. cache_read  - the same request again; must report cache_read_input_tokens > 0

The cached prompt is filler well above the minimum cacheable length, so a 0 on step 5 means
caching is broken, not that the prompt was too short. Estimated cost: under $0.10.
Prints PASS/FAIL per check (a failure does not stop the rest) and exits 1 if any failed.

Usage:
    .venv/bin/python scripts/api_smoke.py
"""
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import anthropic  # noqa: E402

from pharmer import llm  # noqa: E402

STAGE = "repo"
AGENT = "api_smoke"
# Fixed text, byte-identical on both cache calls; about 6,000 tokens.
LONG_SYSTEM = ("You are a smoke test. Reply with the single word OK. "
               + "This sentence is padding so the prompt is long enough to be cached. " * 450)
SCHEMA = {"format": {"type": "json_schema", "schema": {
    "type": "object", "properties": {"answer": {"type": "string"}},
    "required": ["answer"], "additionalProperties": False}}}
SAY_OK = [{"role": "user", "content": "Reply with the single word OK."}]


def text_of(message):
    return "".join(getattr(b, "text", "") for b in message.content if b.type == "text")


def describe(message):
    usage = message.usage
    return (f"model={getattr(message, 'model', '?')} stop={message.stop_reason} "
            f"in={usage.input_tokens} out={usage.output_tokens} "
            f"cache_write={getattr(usage, 'cache_creation_input_tokens', 0)} "
            f"cache_read={getattr(usage, 'cache_read_input_tokens', 0)} "
            f"request_id={getattr(message, '_request_id', '')}")


async def check(name, run_id, turn, model, verify, **params):
    """Run one request; (passed, message or None). Never raises."""
    try:
        message = await llm.call(run_id=run_id, stage=STAGE, agent=AGENT, turn=turn,
                                 model=model, max_tokens=200, **params)
    except anthropic.AnthropicError as exc:
        print(f"FAIL {name}: {type(exc).__name__}: {exc}"[:500])
        return False, None
    problem = None if message.stop_reason == "end_turn" else "stop_reason is not end_turn"
    problem = problem or verify(message)
    print(f"{'FAIL' if problem else 'PASS'} {name}: {describe(message)}"
          + (f"\n     problem: {problem}" if problem else ""))
    return problem is None, message


def has_text(message):
    return None if text_of(message).strip() else "empty reply"


def is_json(message):
    try:
        reply = json.loads(text_of(message))
    except ValueError:
        return f"reply is not JSON: {text_of(message)[:100]!r}"
    return None if isinstance(reply, dict) and "answer" in reply else "JSON lacks 'answer'"


def cache_written(message):
    usage = message.usage
    used = ((getattr(usage, "cache_creation_input_tokens", 0) or 0)
            + (getattr(usage, "cache_read_input_tokens", 0) or 0))
    return None if used else "no cache write or read on a long prompt"


def cache_read(message):
    if getattr(message.usage, "cache_read_input_tokens", 0):
        return None
    return "cache_read_input_tokens is 0 on an identical repeat: caching is not working"


async def main():
    run_id = "SMOKE-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    print(f"run_id {run_id}; rows go to {llm.LOG_PATH}")
    results = [
        (await check("haiku", run_id, 1, llm.HAIKU, has_text, messages=SAY_OK))[0],
        (await check("opus", run_id, 2, llm.OPUS, has_text, messages=SAY_OK))[0],
        (await check("json_schema", run_id, 3, llm.OPUS, is_json, output_config=SCHEMA,
                     messages=[{"role": "user", "content": "Answer OK in the answer field."}]))[0],
    ]
    long_call = {"system": LONG_SYSTEM, "messages": SAY_OK}
    results.append((await check("cache_write", run_id, 4, llm.OPUS, cache_written,
                                **long_call))[0])
    results.append((await check("cache_read", run_id, 5, llm.OPUS, cache_read, **long_call))[0])
    passed = sum(results)
    print(f"{passed}/{len(results)} checks passed. Compare the est_cost_usd of the {run_id} rows "
          "with the Console's usage to check PRICES.")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
