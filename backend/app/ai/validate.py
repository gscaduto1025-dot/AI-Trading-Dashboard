"""Validation of Claude's explanation against the verified fact sheet.

1. Schema: the response must match OUTPUT_SCHEMA exactly (types, enums, required keys).
2. Engine authority: symbol, trading_mode, setup_name, setup_status, market_bias and
   data_timestamp are replaced by the deterministic engine values; any disagreement is
   recorded as a correction. Claude can never change the status or a level.
3. Numbers: every number in Claude's text must match a number in the fact sheet (at the
   precision Claude wrote it), except small counts and indicator periods (EMA 20, 5-minute,
   3 candles). A sentence with an unverified number is removed and reported.
4. Claims: sentences promising outcomes (guaranteed, risk-free, can't lose, will definitely,
   certain to, 100% ...) are removed and reported.
If the core narrative does not survive validation, the explanation is rejected.
"""

from __future__ import annotations

import math
import re

from .facts import engine_setup_name, engine_status

BIAS = ["bullish", "bearish", "neutral"]
STATUSES = ["TRIGGERED", "WATCHING", "NO_TRADE"]

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "symbol": {"type": "string"},
        "trading_mode": {"type": "string", "enum": ["day", "swing"]},
        "market_bias": {"type": "string", "enum": BIAS},
        "setup_name": {"type": "string"},
        "setup_status": {"type": "string", "enum": STATUSES},
        "technical_summary": {"type": "string"},
        "key_level_explanations": {
            "type": "array",
            "items": {"type": "object",
                      "properties": {"level": {"type": "string"},
                                     "explanation": {"type": "string"}},
                      "required": ["level", "explanation"],
                      "additionalProperties": False}},
        "setup_explanation": {"type": "string"},
        "bullish_scenario": {"type": "string"},
        "bearish_scenario": {"type": "string"},
        "entry_conditions": {"type": "array", "items": {"type": "string"}},
        "invalidation_conditions": {"type": "array", "items": {"type": "string"}},
        "risk_explanation": {"type": "string"},
        "data_timestamp": {"type": "string"},
    },
    "required": ["symbol", "trading_mode", "market_bias", "setup_name", "setup_status",
                 "technical_summary", "key_level_explanations", "setup_explanation",
                 "bullish_scenario", "bearish_scenario", "entry_conditions",
                 "invalidation_conditions", "risk_explanation", "data_timestamp"],
    "additionalProperties": False,
}

TEXT_FIELDS = ["technical_summary", "setup_explanation", "bullish_scenario",
               "bearish_scenario", "risk_explanation"]
LIST_FIELDS = ["entry_conditions", "invalidation_conditions"]
CORE_FIELDS = ["technical_summary", "setup_explanation", "risk_explanation"]

# Numbers that are periods, counts or timeframe labels rather than market data.
SAFE_INTEGERS = set(range(0, 11)) | {12, 14, 15, 20, 21, 30, 50, 60, 100, 200}
COUNT_WORDS = r"(?:-|\s)?(?:minute|min|hour|day|week|month|candle|bar|session|touch|reaction|" \
              r"swing|period|ema|sma|atr|rsi)s?\b"

PROMISE = re.compile(
    r"\b(guarantee[sd]?|risk[- ]free|can(?:no|')t lose|sure thing|certain(?:ly)? to|"
    r"will definitely|definitely will|100\s?%|easy money|no[- ]brainer|cannot fail|"
    r"can't fail|bound to (?:rise|fall|go)|locked[- ]in profit)\b", re.I)

_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T ][\d:.+-]+)?\b")
_TIME = re.compile(r"\b\d{1,2}:\d{2}(?:\s?(?:am|pm|ET))?\b", re.I)
_RATIO = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*:\s*1\b")
_NUM = re.compile(r"(?<![\w.])[-+−]?\$?(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(%?)")
_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\"'])")


class ExplanationInvalid(Exception):
    """The response violates the schema or nothing verifiable survives."""


# --------------------------------------------------------------------- fact numbers

def fact_numbers(obj) -> list[float]:
    """Every number in the fact sheet: numeric fields and numbers inside its strings."""
    out: list[float] = []

    def walk(x):
        if isinstance(x, bool) or x is None:
            return
        if isinstance(x, (int, float)):
            if math.isfinite(x):
                out.append(abs(float(x)))
        elif isinstance(x, str):
            s = _DATE.sub(" ", x)
            for m in _NUM.finditer(s):
                out.append(abs(float(m.group(1).replace(",", ""))))
        elif isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, (list, tuple)):
            for v in x:
                walk(v)

    walk(obj)
    return sorted(set(out))


def _matches(token: str, facts: list[float]) -> bool:
    x = abs(float(token.replace(",", "")))
    decimals = len(token.split(".")[1]) if "." in token else 0
    tol = 0.5 * 10 ** -decimals + 1e-9
    return any(abs(f - x) <= tol for f in facts)


def unverified_numbers(text: str, facts: list[float]) -> tuple[list[str], int]:
    """Numbers in `text` that are not backed by the fact sheet, and how many were checked."""
    s = _DATE.sub(" ", text)
    s = _TIME.sub(" ", s)
    bad, checked = [], 0
    for m in _RATIO.finditer(s):
        checked += 1
        if not _matches(m.group(1), facts):
            bad.append(m.group(0))
    s = _RATIO.sub(" ", s)
    for m in _NUM.finditer(s):
        token, pct = m.group(1), m.group(2)
        tail = s[m.end():m.end() + 12]
        value = float(token.replace(",", ""))
        is_int = "." not in token and "," not in token
        if not pct and is_int and (value in SAFE_INTEGERS or
                                   (value <= 60 and re.match(COUNT_WORDS, tail, re.I))):
            continue
        checked += 1
        if not _matches(token, facts):
            bad.append(m.group(0).strip())
    return bad, checked


# -------------------------------------------------------------------------- schema

def check_schema(data) -> list[str]:
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["response is not a JSON object"]
    props = OUTPUT_SCHEMA["properties"]
    for k in OUTPUT_SCHEMA["required"]:
        if k not in data:
            errors.append(f"missing field {k!r}")
    for k, v in data.items():
        if k not in props:
            errors.append(f"unexpected field {k!r}")
            continue
        spec = props[k]
        if spec["type"] == "string":
            if not isinstance(v, str):
                errors.append(f"{k} must be a string")
            elif "enum" in spec and v not in spec["enum"]:
                errors.append(f"{k} must be one of {spec['enum']}")
        elif spec["type"] == "array":
            if not isinstance(v, list):
                errors.append(f"{k} must be an array")
                continue
            for i, item in enumerate(v):
                if spec["items"]["type"] == "string" and not isinstance(item, str):
                    errors.append(f"{k}[{i}] must be a string")
                if spec["items"]["type"] == "object":
                    if not isinstance(item, dict) or set(item) != {"level", "explanation"} \
                            or not all(isinstance(x, str) for x in item.values()):
                        errors.append(f"{k}[{i}] must be {{level, explanation}} strings")
    return errors


# ----------------------------------------------------------------------- validation

def _clean_text(field: str, text: str, nums: list[float], report: dict) -> str:
    kept = []
    for sentence in [s for s in _SENT.split(text.strip()) if s.strip()]:
        if PROMISE.search(sentence):
            report["removed"].append({"field": field, "text": sentence,
                                      "reason": "unsupported prediction or guarantee"})
            continue
        bad, checked = unverified_numbers(sentence, nums)
        report["numbers_checked"] += checked
        if bad:
            report["removed"].append({"field": field, "text": sentence,
                                      "reason": "unverified number(s): " + ", ".join(bad)})
            continue
        report["numbers_verified"] += checked
        kept.append(sentence.strip())
    return " ".join(kept)


def validate_explanation(data, facts: dict) -> dict:
    """Return {"explanation": cleaned, "validation": report}; raise ExplanationInvalid when
    the schema is violated or the core narrative cannot be verified."""
    errors = check_schema(data)
    if errors:
        raise ExplanationInvalid("Schema validation failed: " + "; ".join(errors))
    nums = fact_numbers(facts)
    report = {"passed": True, "corrections": [], "removed": [], "numbers_checked": 0,
              "numbers_verified": 0}
    out = dict(data)
    engine = {
        "symbol": facts["symbol"], "trading_mode": facts["trading_mode"],
        "setup_name": engine_setup_name(facts), "setup_status": engine_status(facts),
        "market_bias": facts["market_bias"]["label"],
        "data_timestamp": facts["data_timestamp"],
    }
    for k, v in engine.items():
        if out.get(k) != v:
            if k in ("setup_status", "market_bias", "symbol", "trading_mode"):
                report["corrections"].append(
                    f"{k}: Claude wrote {out.get(k)!r}; the engine value {v!r} is used")
            out[k] = v
    for f in TEXT_FIELDS:
        out[f] = _clean_text(f, out[f], nums, report)
    for f in LIST_FIELDS:
        out[f] = [c for c in (_clean_text(f, x, nums, report) for x in out[f]) if c]
    levels = []
    for item in out["key_level_explanations"]:
        lbl_bad, n = unverified_numbers(item["level"], nums)
        report["numbers_checked"] += n
        if lbl_bad:
            report["removed"].append({"field": "key_level_explanations", "text": item["level"],
                                      "reason": "level not in market data: " + ", ".join(lbl_bad)})
            continue
        report["numbers_verified"] += n
        text = _clean_text("key_level_explanations", item["explanation"], nums, report)
        if text:
            levels.append({"level": item["level"], "explanation": text})
    out["key_level_explanations"] = levels
    empty = [f for f in CORE_FIELDS if not out[f]]
    if empty:
        raise ExplanationInvalid(f"Nothing verifiable remained in {', '.join(empty)} after "
                                 f"removing {len(report['removed'])} unsupported statement(s).")
    report["passed"] = not report["removed"] and not report["corrections"]
    return {"explanation": out, "validation": report}
