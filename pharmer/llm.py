"""Shared Claude client: every Claude API call in Pharmer goes through here.

Handles three things so no stage has to:
  1. Credentials - ANTHROPIC_API_KEY from the environment or the repo's .env.
  2. Prompt caching + refusal fallback - applied to every request.
  3. Usage logging - one CSV row per API call in logs/api_usage.csv
     (columns: docs/contracts.md, Contract 3).

Use `call()` for a single request and `run_agent()` for a tool loop.
"""
import csv
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import anthropic

REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = Path(os.environ.get("PHARMER_LOGS_DIR", REPO_ROOT / "logs")) / "api_usage.csv"

OPUS = "claude-opus-5-5"    # hunters, Synthesizer, CEO Move
HAIKU = "claude-haiku-5-5"  # mechanical stages: Dedup judge, Fact Checker, Quant

# USD per 1M tokens. Estimates only - verify against current pricing before
# quoting costs. Cache writes assume the 5-minute TTL (1.25x input).
PRICES = {
    OPUS: {"input": 4.00, "output": 20.00, "cache_read": 0.20, "cache_write": 5.00},
    HAIKU: {"input": 0.10, "output": 0.50, "cache_read": 0.01, "cache_write": 0.125},
}

# Server-side refusal fallback; Haiku has none, so it is only sent for Opus.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_MODELS = {OPUS}

LOG_COLUMNS = [
    "timestamp", "run_id", "partner", "stage", "agent", "turn", "model",
    "input_tokens", "output_tokens", "cache_creation_input_tokens",
    "cache_read_input_tokens", "web_search_requests", "stop_reason",
    "latency_ms", "request_id", "est_cost_usd", "error",
]

_log_lock = threading.Lock()
_client = None


def _load_dotenv():
    """Read KEY=VALUE lines from the repo's .env without overriding real env vars."""
    env_file = REPO_ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def get_client():
    global _client
    if _client is None:
        _load_dotenv()
        _client = anthropic.AsyncAnthropic()
    return _client


def estimate_cost(model, usage):
    price = PRICES.get(model)
    if price is None or usage is None:
        return ""
    cost = (
        (usage.input_tokens or 0) * price["input"]
        + (usage.output_tokens or 0) * price["output"]
        + (getattr(usage, "cache_read_input_tokens", 0) or 0) * price["cache_read"]
        + (getattr(usage, "cache_creation_input_tokens", 0) or 0) * price["cache_write"]
    ) / 1_000_000
    return round(cost, 6)


def log_usage(*, run_id, stage, agent, turn, model, message=None, latency_ms=0, error=""):
    """Append one row to logs/api_usage.csv. Safe to call from parallel agents."""
    usage = getattr(message, "usage", None)
    server_tools = getattr(usage, "server_tool_use", None)
    row = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "run_id": run_id,
        "partner": os.environ.get("PHARMER_PARTNER", ""),
        "stage": stage,
        "agent": agent,
        "turn": turn,
        "model": model,
        "input_tokens": getattr(usage, "input_tokens", "") if usage else "",
        "output_tokens": getattr(usage, "output_tokens", "") if usage else "",
        "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", "") if usage else "",
        "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", "") if usage else "",
        "web_search_requests": getattr(server_tools, "web_search_requests", 0) if server_tools else 0,
        "stop_reason": getattr(message, "stop_reason", "") if message else "",
        "latency_ms": latency_ms,
        "request_id": getattr(message, "_request_id", "") if message else "",
        "est_cost_usd": estimate_cost(model, usage),
        "error": error,
    }
    with _log_lock:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        new_file = not LOG_PATH.exists()
        with LOG_PATH.open("a", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=LOG_COLUMNS)
            if new_file:
                writer.writeheader()
            writer.writerow(row)


async def call(*, run_id, stage, agent, turn=1, model=OPUS, max_tokens=16000, **params):
    """One Claude API request, with caching, fallback, and a logged usage row.

    `params` passes straight through (system, messages, tools, output_config, ...).
    Keep the system prompt and tool list byte-identical across calls so the
    cached prefix is reused - no timestamps or run IDs in them.
    """
    request = {"model": model, "max_tokens": max_tokens, **params}
    request.setdefault("cache_control", {"type": "ephemeral"})
    if model in FALLBACK_MODELS:
        request.setdefault("fallbacks", "default")
        request["betas"] = sorted(set(request.get("betas", [])) | {FALLBACK_BETA})

    start = time.monotonic()
    try:
        async with get_client().beta.messages.stream(**request) as stream:
            message = await stream.get_final_message()
    except anthropic.APIError as exc:
        log_usage(run_id=run_id, stage=stage, agent=agent, turn=turn, model=model,
                  latency_ms=int((time.monotonic() - start) * 1000),
                  error=f"{type(exc).__name__}: {exc}"[:300])
        raise
    log_usage(run_id=run_id, stage=stage, agent=agent, turn=turn, model=model,
              message=message, latency_ms=int((time.monotonic() - start) * 1000))
    return message


async def run_agent(*, run_id, stage, agent, tools, tool_handlers, messages,
                    model=OPUS, max_turns=10, **params):
    """Tool loop for one agent; each turn is a separately logged API call.

    A manual loop instead of the SDK tool runner so every request gets its own
    latency measurement and usage row. `tool_handlers` maps a tool name to an
    async function taking the tool input dict and returning a string.
    Returns the final message; `stop_reason` tells the caller how it ended
    ("end_turn", "refusal", "max_tokens", or "tool_use" if max_turns ran out).
    """
    messages = list(messages)
    message = None
    for turn in range(1, max_turns + 1):
        message = await call(run_id=run_id, stage=stage, agent=agent, turn=turn,
                             model=model, tools=tools, messages=messages, **params)
        if message.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": message.content})
            continue
        if message.stop_reason != "tool_use":
            return message

        messages.append({"role": "assistant", "content": message.content})
        results = []
        for block in message.content:
            if block.type != "tool_use":
                continue
            handler = tool_handlers.get(block.name)
            try:
                if handler is None:
                    raise KeyError(f"unknown tool {block.name}")
                content, is_error = await handler(block.input), False
            except Exception as exc:  # report tool failures back to Claude
                content, is_error = f"Error: {exc}", True
            results.append({"type": "tool_result", "tool_use_id": block.id,
                            "content": content, "is_error": is_error})
        # All results go back in one user message so Claude keeps calling tools in parallel.
        messages.append({"role": "user", "content": results})
    return message
