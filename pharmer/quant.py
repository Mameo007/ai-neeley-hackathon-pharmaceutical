"""Quant agent (architecture §4.4): a verified finding becomes a reproducible impact.

The model picks the formula and names its inputs; **Python resolves every input from the
corpus and does all of the arithmetic**, so the number in the report can be recomputed by
hand from what the finding itself carries. Reads runs/<run_id>/findings.json plus Contract 1
and writes Contract 2's `reasonings[].impact` - nothing else on the finding changes.

Currency rules follow architecture §4.4: our own FX conversion exists only to put absolute
amounts in USD for cross-company comparison, flows at the period-average rate and balances
at the period-end rate (ECB reference rates, mirroring IAS 21 / ASC 830). We never compute
a constant-currency growth rate. `eps_pct` needs no FX at all, because an impact and the
EPS it is measured against are in the same currency - so a rate outage costs `usd_m` but
never the materiality score Partner B's scoring reads.

The full audit trail per reasoning goes to the non-contract sidecar
runs/<run_id>/quant_report.json, following librarian_report.json / fact_check_report.json.
"""
import ast
import asyncio
import csv
import json
import logging
import operator
import os
import re
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

import anthropic

from pharmer import llm

log = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
STAGE = "quant"
AGENT = "quant"
# Measured, not guessed: the structured reply repeats eight keys per input and the
# justifications carry real reasoning, so a three-input formula ran to 2,203 output tokens
# on the live NVO finding. At 1,500 every call truncated and every finding went
# unquantified (docs/build-log.md, 2026-10-18).
MAX_TOKENS = 4000

# Byte-identical on every call; the finding under review goes in the user message.
SYSTEM = (PROMPTS_DIR / "quant.md").read_text(encoding="utf-8")

# Only the Fact Checker's High and Medium reach here. Low and null fail Partner B's
# scoring gate anyway (pharmer/scoring.py), so quantifying them would buy nothing and
# architecture §5.1.1 excludes them outright.
QUANTIFIABLE_CONFIDENCE = ("High", "Medium")

# Concurrent judge calls, as pharmer/fact_checker.py: a shared cap belongs in
# pharmer/llm.py, which both partners own, so the limit stays local until that is agreed.
QUANT_CONCURRENCY = 4

ECB_SOURCE = "ECB"
ECB_URL = ("https://data-api.ecb.europa.eu/service/data/EXR/D.{currencies}.EUR.SP00.A"
           "?startPeriod={start}&endPeriod={end}&format=csvdata")
FX_CACHE = "fx_rates.json"
# A period-end rate may fall on a weekend or a TARGET holiday; the ECB publishes no rate
# then, so the last published rate on or before the date is used, looking back at most
# this far. Beyond that we report no rate rather than reach for a stale one.
FX_LOOKBACK_DAYS = 7

OUTPUT_CONFIG = {
    "format": {
        "type": "json_schema",
        "schema": {
            "type": "object",
            "properties": {
                "quantifiable": {"type": "boolean"},
                "reason": {"type": "string"},
                "formula": {"type": "string"},
                "direction": {"type": "string", "enum": ["cost", "benefit"]},
                "basis": {"type": "string", "enum": ["flow", "balance"]},
                "mechanism": {"type": "string"},
                "inputs": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "kind": {"type": "string", "enum": ["fact", "quote", "assumption"]},
                            "value": {"type": ["number", "null"]},
                            "fact_id": {"type": ["string", "null"]},
                            "chunk_id": {"type": ["string", "null"]},
                            "quote": {"type": ["string", "null"]},
                            "justification": {"type": ["string", "null"]},
                            "period_note": {"type": ["string", "null"]},
                        },
                        "required": ["name", "kind", "value", "fact_id", "chunk_id", "quote",
                                     "justification", "period_note"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["quantifiable", "reason", "formula", "direction", "basis",
                         "mechanism", "inputs"],
            "additionalProperties": False,
        },
    },
}

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_NUMBER_IN_TEXT_RE = re.compile(r"\(?\$?\s?-?\d[\d,]*(?:\.\d+)?\)?")
# Scale words and a percent sign change what a figure means, and `_parse_number` reads
# only the digits: "2%" would enter a formula as 2.0 rather than 0.02, and "$1.2 billion"
# as 1.2 against millions-scale facts. A quote carrying either is refused as ambiguous.
_AMBIGUOUS_SCALE_RE = re.compile(
    r"(?i)%|\bper\s?cent|\bbillion\b|\bmillion\b|\bthousand\b|\btrillion\b|\bbn\b|\bm\b|\bk\b")


# --------------------------------------------------------------------------
# formula evaluation


_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.Div: operator.truediv, ast.USub: operator.neg, ast.UAdd: operator.pos}


def evaluate(formula, values):
    """Evaluate an arithmetic `formula` over `values`; raise ValueError on anything else.

    A whitelist over the parsed tree rather than `eval`: the formula is model-written, so
    only + - * / and parentheses over the named inputs are reachable. No calls, no
    attributes, no subscripts, no names outside `values`.
    """
    try:
        tree = ast.parse(formula, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"formula does not parse: {exc}") from exc

    def walk(node):
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](walk(node.left), walk(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](walk(node.operand))
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
                and not isinstance(node.value, bool):
            return float(node.value)
        if isinstance(node, ast.Name):
            if node.id not in values:
                raise ValueError(f"formula uses undefined input {node.id!r}")
            return float(values[node.id])
        raise ValueError(f"formula contains an unsupported expression: {type(node).__name__}")

    try:
        result = walk(tree)
    except ZeroDivisionError as exc:
        raise ValueError("formula divides by zero") from exc
    except (OverflowError, ValueError) as exc:
        # A literal too large for a double raises OverflowError inside float(); left
        # uncaught it escaped `quantify` and killed the whole stage, so one malformed
        # formula discarded every other finding's work.
        raise ValueError(f"formula could not be evaluated: {exc}") from exc
    if result != result or result in (float("inf"), float("-inf")):  # NaN / overflow
        raise ValueError("formula did not produce a finite number")
    return result


# --------------------------------------------------------------------------
# corpus


def _norm_period(label):
    return " ".join(str(label or "").upper().split())


def _parse_number(token):
    """A printed figure as a float, or None. `(1,234)` is the filings' negative."""
    text = str(token).strip()
    negative = text.startswith("(") and text.endswith(")")
    body = text.strip("()").replace(",", "").replace("$", "").replace("%", "").strip()
    if body.startswith("-"):
        negative, body = True, body[1:].strip()
    if not re.fullmatch(r"\d+(?:\.\d+)?", body):
        return None
    return -float(body) if negative else float(body)


def _whole_figure_in(token, text):
    """Whether `token` appears in `text` as a complete printed figure.

    Guards the digit-fragment hole in substring matching: a quote of `"70,85"` really is
    contained in `"Trade receivables 88,949 70,856"`, so containment alone would accept a
    number the filing never printed. The occurrence must not have a digit, a comma or a
    decimal point immediately on either side.
    """
    needle = token.strip()
    if not needle:
        return False
    digits = "0123456789.,"
    for match in re.finditer(re.escape(needle), text):
        before = text[match.start() - 1] if match.start() else ""
        after = text[match.end()] if match.end() < len(text) else ""
        # `"" in digits` is True in Python, so the emptiness has to be tested first or a
        # figure at either end of the chunk would be rejected as a fragment.
        if (before and before in digits) or (after and after in digits):
            continue
        return True
    return False


def load_corpus(run_dir):
    """(facts by id, facts list, chunks by id) from one run folder; missing files are empty."""
    def jsonl(name):
        path = run_dir / name
        if not path.exists():
            log.warning("quant: %s is missing", name)
            return []
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    log.warning("quant: a %s line is not valid JSON and was skipped", name)
        return rows

    facts = [f for f in jsonl("facts.jsonl") if isinstance(f, dict)]
    chunks = {c["chunk_id"]: c for c in jsonl("chunks.jsonl")
              if isinstance(c, dict) and c.get("chunk_id")}
    by_id = {f["fact_id"]: f for f in facts if f.get("fact_id")}
    return by_id, facts, chunks


def period_bounds(fiscal_period, period_end=None):
    """(start, end) ISO dates for a Contract 1a period label, or None if unlabelable.

    `period_end` from a matched fact wins when given, because a 52/53-week filer's quarter
    does not end on the calendar quarter day (Pfizer and J&J, architecture §4.0).
    """
    label = _norm_period(fiscal_period)
    try:
        if label.startswith("FY"):
            year = int(label[2:])
            start, end = date(year, 1, 1), date(year, 12, 31)
        else:
            kind, year = label.split()
            year = int(year)
            index = int(kind[1:])
            if kind[0] == "Q":
                start = date(year, 3 * (index - 1) + 1, 1)
                end = date(year + (index == 4), 3 * index % 12 + 1, 1) - timedelta(days=1)
            elif kind[0] == "H":
                start = date(year, 6 * (index - 1) + 1, 1)
                end = date(year + (index == 2), 6 * index % 12 + 1, 1) - timedelta(days=1)
            else:
                return None
    except (ValueError, IndexError):
        return None
    if period_end:
        try:
            end = date.fromisoformat(period_end)
        except ValueError:
            pass
    return start.isoformat(), end.isoformat()


# --------------------------------------------------------------------------
# FX


def _ecb_series(currencies, start, end):
    """{currency: {date: rate per EUR}} from the ECB reference-rate API."""
    url = ECB_URL.format(currencies="+".join(sorted(currencies)), start=start, end=end)
    request = urllib.request.Request(url, headers={"Accept": "text/csv"})
    with urllib.request.urlopen(request, timeout=30) as response:
        text = response.read().decode("utf-8")
    series = {}
    for row in csv.DictReader(text.splitlines()):
        currency, day, value = row.get("CURRENCY"), row.get("TIME_PERIOD"), row.get("OBS_VALUE")
        try:
            rate = float(value)
        except (TypeError, ValueError):
            continue
        series.setdefault(currency, {})[day] = rate
    return series


def fx_rate(run_dir, currency, start, end, basis, fetch=None):
    """(rate, detail) converting `currency` into USD, or (None, why not).

    `rate` is units of `currency` per USD, so `usd = native / rate`. Flows take the
    period-average rate and balances the period-end rate (architecture §4.4). Rates are
    cached in the run folder, so a re-run is deterministic and needs no network; a rate
    that cannot be obtained yields None and the caller leaves `usd_m` null rather than
    reaching for a stale or invented number.

    `fetch` is resolved at call time, not bound as a default, so a test that replaces
    `_ecb_series` on the module is actually honoured - a default argument would capture
    the real fetcher at import and quietly put the network back into the test suite.
    """
    fetch = fetch or _ecb_series
    convention = "period_average" if basis == "flow" else "period_end"
    if currency == "USD":
        # `basis` is reported as the FX convention on every path; passing the raw
        # flow/balance word through here gave consumers two vocabularies for one key.
        return 1.0, {"pair": "USD/USD", "rate": 1.0, "date": end, "basis": convention,
                     "source": "none (already USD)", "direction": "already USD"}
    cache_path = Path(run_dir) / FX_CACHE
    cache = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except ValueError:
            log.warning("quant: %s is not valid JSON; refetching", FX_CACHE)
    key = f"{currency}|{start}|{end}|{basis}"
    if key in cache:
        entry = cache[key]
        return entry.get("rate"), entry.get("detail")

    series = {}
    try:
        series = fetch({currency, "USD"}, start, end)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log.warning("quant: ECB rates unavailable (%s)", exc)

    usd = series.get("USD") or {}
    # The EUR is the ECB's base, so the dataset has no EUR/EUR row: a EUR filer needs the
    # reciprocal of USD-per-EUR, not a cross. Without this every EUR-reporting filer
    # (Sanofi, Bayer) lost its dollar figure and was reported as a rate outage.
    native = {day: 1.0 for day in usd} if currency == "EUR" else (series.get(currency) or {})
    rate, detail = None, None
    if native and usd:
        if basis == "flow":
            days = sorted(set(native) & set(usd))
            if days:
                # Cross through EUR: (currency per EUR) / (USD per EUR) = currency per USD.
                crosses = [native[d] / usd[d] for d in days if usd[d]]
                if crosses:
                    rate = sum(crosses) / len(crosses)
                    detail = {"pair": f"{currency}/USD", "rate": round(rate, 6),
                              "date": f"{days[0]}..{days[-1]}", "basis": "period_average",
                              "source": ECB_SOURCE, "observations": len(crosses),
                              "direction": f"USD = {currency} / rate"}
        else:
            limit = date.fromisoformat(end)
            for back in range(FX_LOOKBACK_DAYS + 1):
                day = (limit - timedelta(days=back)).isoformat()
                if day in native and usd.get(day):
                    rate = native[day] / usd[day]
                    detail = {"pair": f"{currency}/USD", "rate": round(rate, 6), "date": day,
                              "basis": "period_end", "source": ECB_SOURCE,
                              "direction": f"USD = {currency} / rate"}
                    break
    if rate is None:
        return None, {"pair": f"{currency}/USD", "rate": None, "date": end, "basis": basis,
                      "source": ECB_SOURCE,
                      "note": "no ECB reference rate available for this period"}
    cache[key] = {"rate": rate, "detail": detail}
    try:
        cache_path.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")
    except OSError as exc:  # a read-only run folder must not lose the computed impact
        log.warning("quant: could not cache FX rates (%s)", exc)
    return rate, detail


# --------------------------------------------------------------------------
# input resolution


def resolve_inputs(reply_inputs, anchor, facts_by_id, chunks):
    """({name: value}, [provenance rows], [problems]).

    A `fact` input takes the corpus value, never the model's; a `quote` input must appear
    verbatim in the cited chunk and its number is parsed out of that quote; an
    `assumption` must carry a justification. Anything unresolvable becomes a problem, and
    the caller leaves the finding unquantified - an input is never defaulted or invented.
    """
    values, provenance, problems = {}, [], []
    anchor_period = _norm_period(anchor.get("fiscal_period"))
    for item in reply_inputs or []:
        name, kind = item.get("name"), item.get("kind")
        if not isinstance(name, str) or not _NAME_RE.match(name):
            problems.append(f"input name {name!r} is not a plain identifier")
            continue
        if name in values:
            problems.append(f"input {name!r} is defined twice")
            continue
        row = {"name": name, "kind": kind, "period_note": item.get("period_note")}

        if kind == "fact":
            fact_id = item.get("fact_id")
            if not fact_id or not isinstance(fact_id, str):
                # `None` is a live dict key, so a null fact_id used to resolve against any
                # corpus row that was itself missing one - an invented input.
                problems.append(f"{name}: a fact input needs a fact_id")
                continue
            fact = facts_by_id.get(fact_id)
            if fact is None:
                problems.append(f"{name}: fact_id {item.get('fact_id')!r} is not in facts.jsonl")
                continue
            value = fact.get("value")
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                problems.append(f"{name}: {fact.get('fact_id')} carries no numeric value")
                continue
            period = _norm_period(fact.get("fiscal_period"))
            if period != anchor_period and not item.get("period_note"):
                problems.append(f"{name}: {fact.get('fact_id')} is {period}, the anchor is "
                                f"{anchor_period}, and no period_note explains it")
                continue
            values[name] = float(value)
            row.update({"value": float(value), "source": fact.get("fact_id"),
                        "fiscal_period": period, "unit": fact.get("unit"),
                        "currency": fact.get("currency")})

        elif kind == "quote":
            chunk = chunks.get(item.get("chunk_id"))
            quote = item.get("quote") or ""
            if chunk is None:
                problems.append(f"{name}: chunk_id {item.get('chunk_id')!r} is not in the corpus")
                continue
            text = chunk.get("text") if isinstance(chunk.get("text"), str) else ""
            if quote not in text and " ".join(quote.split()) not in " ".join(text.split()):
                problems.append(f"{name}: the quote is not in {item.get('chunk_id')}")
                continue
            tokens = [t for t in _NUMBER_IN_TEXT_RE.findall(quote)
                      if _parse_number(t) is not None]
            if len(tokens) != 1:
                problems.append(f"{name}: the quote must contain exactly one figure, found "
                                f"{len(tokens)}")
                continue
            if _AMBIGUOUS_SCALE_RE.search(quote):
                problems.append(f"{name}: the quote carries a percent sign or a scale word, "
                                f"so its magnitude is ambiguous")
                continue
            if not _whole_figure_in(tokens[0], text):
                # "70,85" is a verbatim substring of "70,856" and parses to 7085.0 - a
                # figure the filing never printed. Substring containment alone is not
                # provenance; the figure has to be a whole printed number.
                problems.append(f"{name}: {tokens[0].strip()!r} is part of a longer figure "
                                f"in {item.get('chunk_id')}, not a figure in its own right")
                continue
            values[name] = _parse_number(tokens[0])
            row.update({"value": values[name], "source": item.get("chunk_id"),
                        "quote": quote, "page": chunk.get("page"),
                        "fiscal_period": item.get("period_note") or anchor_period})

        elif kind == "assumption":
            value = item.get("value")
            justification = (item.get("justification") or "").strip()
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                problems.append(f"{name}: an assumption needs a numeric value")
                continue
            if not justification:
                problems.append(f"{name}: assumption {value} has no justification")
                continue
            values[name] = float(value)
            row.update({"value": float(value), "source": "assumption",
                        "justification": justification})

        else:
            problems.append(f"{name}: unknown input kind {kind!r}")
            continue
        provenance.append(row)
    if not values and not problems:
        problems.append("the reply named no inputs")
    return values, provenance, problems


# --------------------------------------------------------------------------
# EPS


def eps_denominator(anchor, facts):
    """(eps, shares, detail) for the anchor's own period, or (None, None, why not).

    Both must be period-matched: an annual share count is not a denominator for a
    quarterly impact, so a filer that tags shares annually only (Novo: IFRS, FY rows only)
    yields no quarterly `eps_pct` and the limitation is reported rather than papered over.
    The EPS currency must also match the impact's currency, or the ratio is meaningless.
    """
    period = _norm_period(anchor.get("fiscal_period"))
    currency = str(anchor.get("currency") or "").upper()
    matched = {}
    for fact in facts:
        if fact.get("metric") not in ("diluted_eps", "diluted_shares"):
            continue
        if _norm_period(fact.get("fiscal_period")) != period:
            continue
        value = fact.get("value")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            matched[fact["metric"]] = fact
    eps, shares = matched.get("diluted_eps"), matched.get("diluted_shares")
    if eps is None or shares is None:
        have = sorted(matched)
        return None, None, (f"no period-matched diluted {'shares' if eps else 'EPS'} for "
                            f"{period} (found: {', '.join(have) or 'neither'})")
    eps_currency = str(eps.get("currency") or "").upper()
    if eps_currency != currency:
        return None, None, (f"diluted EPS is in {eps_currency or 'an unstated currency'}, the "
                            f"impact in {currency or 'an unstated currency'}")
    if not eps.get("value"):
        return None, None, f"diluted EPS for {period} is zero, so a percentage is undefined"
    if not shares.get("value") or shares["value"] <= 0:
        # A zero count divided by zero in eps_percent and took the whole stage with it;
        # a negative one would have silently inverted the sign of every eps_pct.
        return None, None, (f"the diluted share count for {period} is "
                            f"{shares.get('value')!r}, which cannot be a denominator")
    return eps, shares, (f"{eps['fact_id']} = {eps['value']} {eps_currency}/share over "
                         f"{shares['fact_id']} = {shares['value']}m shares")


def eps_percent(native_m, eps, shares):
    """The impact as a percentage of diluted EPS.

    `native_m` is millions of the anchor's currency and `shares` millions of shares, so
    their ratio is a per-share amount in that same currency - which is exactly what EPS
    is. No FX enters, which is why a rate outage cannot cost the materiality score.
    """
    per_share = native_m / shares["value"]
    return (per_share / eps["value"]) * 100.0


# --------------------------------------------------------------------------
# the model call


def quant_message(finding, reasoning, facts, chunks):
    """User message for one reasoning: the anchor, claim, quotes, usable facts, trend."""
    anchor = finding.get("anchor") or {}
    lines = ["# Anchor", "", json.dumps(anchor, ensure_ascii=False),
             "", "# Claim", "", json.dumps(reasoning.get("claim"), ensure_ascii=False),
             "", "# Quotes"]
    for citation in reasoning.get("citations") or []:
        chunk = chunks.get(citation.get("chunk_id"))
        where = (f"{chunk.get('doc_id', '?')} p.{chunk.get('page', '?')}" if chunk
                 else "not in the corpus")
        lines.append(f"- ({citation.get('chunk_id')}, {where}) "
                     f"{json.dumps(citation.get('quote'), ensure_ascii=False)}")
    lines += ["", "# Facts"]
    for fact in facts:
        lines.append(f"- {fact.get('fact_id')}: {fact.get('metric')} {fact.get('fiscal_period')}"
                     f" = {fact.get('value')} {fact.get('unit')}"
                     f"{' ' + fact['currency'] if fact.get('currency') else ''}")
    trend = reasoning.get("trend") or []
    if trend:
        lines += ["", "# Trend", "", json.dumps(trend, ensure_ascii=False)]
    return "\n".join(lines)


def parse_reply(message):
    """(reply dict, problem) - the structured formula choice, or a named failure."""
    stop = getattr(message, "stop_reason", None)
    if stop != "end_turn":
        return None, f"the model stopped with stop_reason={stop}"
    text = "".join(getattr(b, "text", "") for b in getattr(message, "content", None) or []
                   if getattr(b, "type", "") == "text")
    try:
        reply = json.loads(text)
    except ValueError as exc:
        return None, f"the reply is not valid JSON ({exc})"
    if not isinstance(reply, dict) or not isinstance(reply.get("quantifiable"), bool):
        return None, "the reply is not in the expected shape"
    return reply, None


async def choose_formula(run_id, finding, reasoning, facts, chunks, gate=None):
    """One Haiku call picking a formula for this reasoning; never raises."""
    async def call():
        try:
            return await llm.call(
                run_id=run_id, stage=STAGE, agent=AGENT, model=llm.HAIKU,
                max_tokens=MAX_TOKENS, system=SYSTEM,
                messages=[{"role": "user",
                           "content": quant_message(finding, reasoning, facts, chunks)}],
                output_config=OUTPUT_CONFIG)
        except (anthropic.AnthropicError, TypeError, OSError) as exc:
            # TypeError covers client construction with no credentials.
            log.warning("quant: formula request failed: %s", exc)
            return None

    message = await (call() if gate is None else _gated(gate, call))
    if message is None:
        return None, "the model was unavailable"
    return parse_reply(message)


async def _gated(gate, call):
    async with gate:
        return await call()


# --------------------------------------------------------------------------
# stage


async def quantify(run_id, finding, reasoning, facts_by_id, facts, chunks, run_dir, gate=None):
    """Compute one reasoning's `impact` and return its audit row.

    Every path that cannot produce a defensible number leaves `impact` null with a reason -
    never zero, which would read as "we measured no effect" (docs/contracts.md: an absent
    impact scores null and is ranked last, it is not scored as harmless).
    """
    anchor = finding.get("anchor") or {}
    row = {"finding_id": finding.get("finding_id"),
           "reasoning_id": reasoning.get("reasoning_id"),
           "confidence": reasoning.get("confidence"),
           "anchor": {"metric": anchor.get("metric"),
                      "fiscal_period": _norm_period(anchor.get("fiscal_period")),
                      "currency": anchor.get("currency")},
           "quantified": False, "impact": None, "skipped": None, "problems": []}

    # Cleared up front, so a re-run that fails cannot leave the previous run's figure in
    # findings.json while the sidecar reports the reasoning as skipped - scoring reads
    # findings.json, and the two files disagreeing is worse than no impact at all.
    reasoning["impact"] = None

    if reasoning.get("confidence") not in QUANTIFIABLE_CONFIDENCE:
        row["skipped"] = (f"confidence {reasoning.get('confidence')!r} fails the scoring gate; "
                          f"not quantified and no call made")
        return row

    reply, problem = await choose_formula(run_id, finding, reasoning, facts, chunks, gate)
    if reply is None:
        row["skipped"] = problem
        return row
    row["mechanism"] = reply.get("mechanism")
    if not reply.get("quantifiable"):
        row["skipped"] = f"the model declined to quantify: {reply.get('reason') or 'no reason'}"
        return row

    values, provenance, problems = resolve_inputs(reply.get("inputs"), anchor, facts_by_id,
                                                  chunks)
    row["inputs"] = provenance
    formula = reply.get("formula") or ""
    row["formula"] = formula
    basis = reply.get("basis") if reply.get("basis") in ("flow", "balance") else "balance"
    row["basis"] = basis
    if problems:
        row["problems"] = problems
        row["skipped"] = f"{len(problems)} input(s) did not resolve"
        return row

    try:
        magnitude = evaluate(formula, values)
    except ValueError as exc:
        row["problems"] = [str(exc)]
        row["skipped"] = "the formula could not be evaluated"
        return row
    # The sign is applied here, not left to the formula: asked for "negative for damage"
    # in prose the model returned a positive write-down on every live finding, which would
    # have printed a loss as a gain. `direction` is a required enum, so the sign is
    # deterministic and a sign-flipped formula cannot change it.
    direction = reply.get("direction") if reply.get("direction") in ("cost", "benefit") \
        else "cost"
    row["direction"] = direction
    native_m = -abs(magnitude) if direction == "cost" else abs(magnitude)
    # The stored formula carries the sign, so `impact.formula` evaluated over
    # `impact.inputs` reproduces `usd_m` with no other field needed - Contract 2's
    # `impact` keys stay exactly as they are.
    signed_formula = f"-({formula})" if native_m != magnitude else formula
    row["model_formula"] = formula
    row["direction"] = direction
    row["magnitude"] = magnitude
    if native_m == 0:
        # A zero assumption ("no loss expected") produced impact 0.0, which scoring reads
        # as materiality 0.0 - a ranked zero that outranks every unquantified finding.
        # Contract 2's absent impact is the honest representation of "no size".
        row["problems"].append("the formula evaluates to zero, which is not a measurement")
        row["skipped"] = "the formula evaluates to zero"
        return row
    row["native_m"] = native_m
    row["native_currency"] = anchor.get("currency")
    formula = signed_formula
    row["formula"] = formula

    currency = str(anchor.get("currency") or "").upper()
    bounds = period_bounds(anchor.get("fiscal_period"),
                           _matched_period_end(anchor, facts))
    usd_m, fx = None, None
    if not currency:
        row["problems"].append("the anchor states no currency, so no USD amount is derived")
    elif bounds is None:
        row["problems"].append(f"cannot date {anchor.get('fiscal_period')!r} for an FX rate")
    else:
        rate, fx = fx_rate(run_dir, currency, bounds[0], bounds[1], basis)
        if rate:
            usd_m = native_m / rate
        else:
            row["problems"].append("no FX rate, so usd_m is left null (eps_pct is unaffected)")

    eps, shares, eps_detail = eps_denominator(anchor, facts)
    row["eps_basis"] = eps_detail
    eps_pct = None
    if eps is not None:
        eps_pct = eps_percent(native_m, eps, shares)
    else:
        row["problems"].append(f"eps_pct unavailable: {eps_detail}")

    row["impact"] = {
        "usd_m": None if usd_m is None else round(usd_m, 1),
        "eps_pct": None if eps_pct is None else round(eps_pct, 2),
        "formula": formula,
        "inputs": {name: value for name, value in values.items()},
        "fx": fx,
    }
    row["quantified"] = True
    reasoning["impact"] = row["impact"]
    return row


def _matched_period_end(anchor, facts):
    """The anchor period's own period_end from a corpus fact, for 52/53-week filers."""
    period = _norm_period(anchor.get("fiscal_period"))
    for fact in facts:
        if _norm_period(fact.get("fiscal_period")) == period and fact.get("period_end"):
            return fact["period_end"]
    return None


def _write_json(path, payload):
    """Write JSON atomically; findings.json is the pipeline's only copy of the run."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


async def run(run_dir: Path) -> None:
    """Read run_dir/findings.json, add `reasonings[].impact`, write it back.

    Touches only `reasonings[].impact`; every other Contract 2 field belongs to another
    stage and is preserved. Also writes the non-contract sidecar run_dir/quant_report.json,
    which carries the audit trail: each input with its source and period, the formula, the
    recomputation, the FX basis and any reason a figure was left null.
    """
    run_dir = Path(run_dir)
    findings = json.loads((run_dir / "findings.json").read_text(encoding="utf-8"))
    facts_by_id, facts, chunks = load_corpus(run_dir)

    pairs = [(f, r) for f in findings for r in f.get("reasonings") or []]
    gate = asyncio.Semaphore(QUANT_CONCURRENCY)
    rows = []
    # The first call goes alone so the cached system prefix is written before the rest
    # fan out (docs/contracts.md Contract 3).
    # Indexed, not value-compared: two reasonings can be identical dicts.
    warm = next((i for i, (_f, r) in enumerate(pairs)
                 if r.get("confidence") in QUANTIFIABLE_CONFIDENCE), None)
    if warm is not None:
        finding, reasoning = pairs[warm]
        rows.append(await quantify(run_dir.name, finding, reasoning, facts_by_id, facts,
                                   chunks, run_dir))
    rest = [p for i, p in enumerate(pairs) if i != warm]
    rows += await asyncio.gather(*(
        quantify(run_dir.name, finding, reasoning, facts_by_id, facts, chunks, run_dir, gate)
        for finding, reasoning in rest))

    _write_json(run_dir / "findings.json", findings)
    quantified = [r for r in rows if r["quantified"]]
    report = {
        "stage": STAGE,
        "run_id": run_dir.name,
        "reasonings": len(rows),
        "quantified": len(quantified),
        "with_usd": sum(r["impact"]["usd_m"] is not None for r in quantified),
        "with_eps_pct": sum(r["impact"]["eps_pct"] is not None for r in quantified),
        "skipped": [{"reasoning_id": r["reasoning_id"], "reason": r["skipped"]}
                    for r in rows if r["skipped"]],
        "reasoning_quant": rows,
    }
    _write_json(run_dir / "quant_report.json", report)
    log.info("quant: %d reasoning(s) -> %d quantified (%d with usd_m, %d with eps_pct)",
             len(rows), len(quantified), report["with_usd"], report["with_eps_pct"])


def main(argv=None):
    """`python -m pharmer.quant runs/<run_id>`."""
    import sys
    logging.basicConfig(level=logging.INFO)
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: python -m pharmer.quant <run_dir>")
        return 2
    asyncio.run(run(Path(args[0])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
