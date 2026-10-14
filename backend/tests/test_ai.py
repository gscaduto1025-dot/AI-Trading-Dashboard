"""Claude explanation layer: schema validation, numeric verification, engine authority,
failure handling and caching. Claude is replaced by a scripted fake client; no network."""

from __future__ import annotations

import copy
import json

import anthropic
import httpx
import pytest
from fastapi.testclient import TestClient

from app.ai.claude import SYSTEM_PROMPT, ClaudeAnalyst
from app.ai.facts import build_facts, cache_key
from app.ai.validate import (OUTPUT_SCHEMA, ExplanationInvalid, check_schema, fact_numbers,
                             unverified_numbers, validate_explanation)
from app.main import create_app

# ---------------------------------------------------------------------- fixtures

ANALYSIS = {
    "symbol": "SPY", "mode": "day", "timeframe": "5Min", "price": 513.77,
    "analyzed_through": 1773172500,
    "indicators": {"atr": 0.8252, "rsi": 55.14, "ema_fast": 513.41, "ema_slow": 513.07,
                   "ema_fast_period": 9, "ema_slow_period": 20, "vwap": 514.36,
                   "sma_long": None, "volume_ratio": 1.33},
    "bias": {"label": "Bullish", "score": 3, "max_score": 6, "factors": [
        {"label": "Price vs EMA20", "signal": "bullish", "detail": "513.77 above 513.07"},
        {"label": "Price vs VWAP", "signal": "bearish", "detail": "513.77 below VWAP 514.36"}]},
    "data_status": {"as_of": "2026-03-10T18:00:00-04:00", "feed_label": "DEMO",
                    "latency": "demo", "coverage": "Simulated prices."},
    "key_levels": [
        {"label": "Daily resistance zone", "type": "RESISTANCE_ZONE", "timeframe": "1Day",
         "low": 517.43, "high": 525.88, "side": "resistance", "strength": 96,
         "evidence": "18 confirmed swing reactions between 517.43 and 525.88."},
        {"label": "Previous day low", "type": "PDL", "timeframe": "1Day", "low": 508.22,
         "high": 508.22, "side": "support", "strength": 80, "evidence": "Low of 2026-03-09."}],
}

PLAN = {
    "strategy_name": "Breakout and retest", "direction": "LONG", "timeframe": "5Min",
    "status": "WATCHING", "reasons": ["Waiting for a close above 514.30."],
    "entry_trigger": {"price": 514.3, "basis": "5m close above the retest high 514.30"},
    "entry": {"price": 514.3, "basis": "planned at the trigger"},
    "entry_zone": {"low": 514.3, "high": 514.51, "basis": "trigger to 0.25 ATR beyond"},
    "stop": {"price": 512.84, "basis": "retest low 512.92 - 0.1 x ATR"},
    "target1": {"price": 517.43, "basis": "Daily resistance zone near edge 517.43"},
    "target2": {"price": 518.25, "basis": "ATR extension"},
    "risk_per_share": 1.46, "reward_per_share_t1": 3.13, "reward_per_share_t2": 3.95,
    "risk_reward_t1": 2.14, "risk_reward_t2": 2.71, "min_rr": 2.0,
    "confirmation": [{"name": "Entry confirmation", "passed": False, "required": True,
                      "detail": "pending"}],
    "invalidation": ["A close below 512.92 cancels the setup."],
    "conflicts": [], "checks": [{"name": "Reward/risk", "passed": True, "detail": "2.14:1"}],
    "quality": {"score": 71.5},
}
SETUP = {"id": "abc", "htf_bias": {"label": "bullish", "detail": "daily bullish"}}


def facts(plan=PLAN, setup=SETUP):
    return build_facts(copy.deepcopy(ANALYSIS), setup, plan, "day", True)


def good(f=None, **over) -> dict:
    """A well-behaved Claude response that only quotes fact-sheet numbers."""
    f = f or facts()
    out = {
        "symbol": "SPY", "trading_mode": "day", "market_bias": "bullish",
        "setup_name": "Breakout and retest (LONG, 5Min)", "setup_status": "WATCHING",
        "technical_summary": "This is simulated demo data. Price 513.77 sits above EMA9 513.41 "
                             "and EMA20 513.07 but below VWAP 514.36, with relative volume 1.33.",
        "key_level_explanations": [
            {"level": "Daily resistance zone 517.43-525.88",
             "explanation": "Overhead supply with 18 confirmed swing reactions; it is the first "
                            "target."},
            {"level": "Previous day low 508.22", "explanation": "Nearest support below."}],
        "setup_explanation": "A breakout and retest is developing; the plan needs a 5-minute "
                             "close above 514.30.",
        "bullish_scenario": "A close above 514.3 with volume opens the path to 517.43.",
        "bearish_scenario": "A close below 512.92 would cancel the retest.",
        "entry_conditions": ["A completed 5-minute close above 514.30."],
        "invalidation_conditions": ["A close below 512.92."],
        "risk_explanation": "Reward/risk to T1 is 2.14:1, close to the 2.0 minimum, and price "
                            "is still below VWAP.",
        "data_timestamp": f["data_timestamp"],
    }
    out.update(over)
    return out


class _Block:
    type = "text"

    def __init__(self, text):
        self.text = text


class _Resp:
    model = "claude-opus-5-5"
    _request_id = "req_test"

    def __init__(self, text, stop="end_turn"):
        self.content = [_Block(text)]
        self.stop_reason = stop


class FakeMessages:
    def __init__(self, script):
        self.script = list(script)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, Exception):
            raise item
        if callable(item):
            item = item(kwargs)
        if isinstance(item, _Resp):
            return item
        return _Resp(item if isinstance(item, str) else json.dumps(item))


class FakeClient:
    def __init__(self, *script):
        self.beta = type("Beta", (), {})()
        self.beta.messages = FakeMessages(script)


def _status_error(cls, status):
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("boom", response=httpx.Response(status, request=req), body=None)


# ------------------------------------------------------------- number verification

def test_numbers_must_match_the_fact_sheet_at_the_written_precision():
    nums = fact_numbers(facts())
    assert unverified_numbers("Price 513.77 is below VWAP 514.36.", nums) == ([], 2)
    assert unverified_numbers("Price $513.8 holds.", nums)[0] == []  # 513.77 shown to 1 dp
    assert unverified_numbers("Resistance near 520.15.", nums)[0] == ["520.15"]
    assert unverified_numbers("Target 519.9 is likely.", nums)[0] == ["519.9"]
    assert unverified_numbers("R:R is 2.14:1.", nums)[0] == []
    assert unverified_numbers("R:R is 3.5:1.", nums)[0] == ["3.5:1"]
    assert unverified_numbers("Price is -0.1% from VWAP.", nums)[0] == []  # distance_pct
    assert unverified_numbers("A 4.2% drop is possible.", nums)[0] == ["4.2%"]


def test_periods_counts_dates_and_times_are_not_prices():
    nums = fact_numbers(facts())
    text = ("The 5-minute EMA20 and 200 SMA, 3 candles since 09:45 ET on 2026-03-10, "
            "EMA 9 above EMA 20, 2 touches.")
    assert unverified_numbers(text, nums)[0] == []
    # A large integer price is still checked.
    assert unverified_numbers("Watch 530 overhead.", nums)[0] == ["530"]
    assert unverified_numbers("Watch 1,234.50 overhead.", nums)[0] == ["1,234.50"]


# -------------------------------------------------------------------------- schema

def test_schema_rejects_missing_extra_and_wrong_types():
    assert check_schema(good()) == []
    bad = good()
    del bad["risk_explanation"]
    assert "missing field 'risk_explanation'" in check_schema(bad)
    assert any("unexpected field" in e for e in check_schema(good(confidence=0.9)))
    assert any("market_bias must be one of" in e for e in check_schema(good(market_bias="up")))
    assert any("setup_status must be one of" in e
               for e in check_schema(good(setup_status="TAKE_TRADE")))
    assert any("entry_conditions must be an array" in e
               for e in check_schema(good(entry_conditions="close above 514.30")))
    assert any("key_level_explanations[0]" in e
               for e in check_schema(good(key_level_explanations=[{"level": "x"}])))
    assert check_schema([]) == ["response is not a JSON object"]
    with pytest.raises(ExplanationInvalid):
        validate_explanation(good(market_bias="up"), facts())


def test_api_schema_is_strict():
    assert OUTPUT_SCHEMA["additionalProperties"] is False
    assert set(OUTPUT_SCHEMA["required"]) == set(OUTPUT_SCHEMA["properties"])
    for k in ("symbol", "trading_mode", "market_bias", "setup_name", "setup_status",
              "technical_summary", "key_level_explanations", "bullish_scenario",
              "bearish_scenario", "entry_conditions", "invalidation_conditions",
              "risk_explanation", "data_timestamp"):
        assert k in OUTPUT_SCHEMA["required"]


# ---------------------------------------------------------------- validation rules

def test_valid_explanation_passes_and_numbers_are_counted():
    f = facts()
    out = validate_explanation(good(f), f)
    assert out["validation"]["passed"] is True
    assert out["validation"]["numbers_checked"] == out["validation"]["numbers_verified"] > 10
    assert out["explanation"]["setup_status"] == "WATCHING"
    assert len(out["explanation"]["key_level_explanations"]) == 2


def test_claude_cannot_override_status_bias_or_identity():
    f = facts()
    out = validate_explanation(good(f, setup_status="TRIGGERED", market_bias="bearish",
                                    symbol="QQQ", setup_name="Something else",
                                    data_timestamp="yesterday"), f)
    e = out["explanation"]
    assert e["setup_status"] == "WATCHING" and e["market_bias"] == "bullish"
    assert e["symbol"] == "SPY" and e["setup_name"] == "Breakout and retest (LONG, 5Min)"
    assert e["data_timestamp"] == f["data_timestamp"]
    joined = " ".join(out["validation"]["corrections"])
    assert "setup_status" in joined and "market_bias" in joined and "symbol" in joined
    assert out["validation"]["passed"] is False


def test_no_trade_without_setup_is_enforced():
    f = facts(plan=None, setup=None)
    resp = good(f, setup_status="TRIGGERED", setup_explanation="No setup was detected.",
                risk_explanation="Price 513.77 is below VWAP 514.36.")
    out = validate_explanation(resp, f)
    assert out["explanation"]["setup_status"] == "NO_TRADE"
    assert out["explanation"]["setup_name"] == "No setup"
    # The plan's numbers are not in a no-setup sheet, so quoting them is removed.
    assert any("514.30" in r["text"] for r in out["validation"]["removed"])


def test_hallucinated_numbers_are_removed_sentence_by_sentence():
    f = facts()
    resp = good(f, technical_summary="Price 513.77 sits above EMA20 513.07. A squeeze to "
                                     "531.20 is next.",
                bullish_scenario="Above 514.3 the next stop is 522.75.",
                key_level_explanations=[
                    {"level": "Weekly resistance 540.00", "explanation": "Big level."},
                    {"level": "Daily resistance zone 517.43-525.88",
                     "explanation": "First target at 517.43. A spike to 519.99 follows."}])
    out = validate_explanation(resp, f)
    e, v = out["explanation"], out["validation"]
    assert e["technical_summary"] == "Price 513.77 sits above EMA20 513.07."
    assert e["bullish_scenario"] == ""
    assert e["key_level_explanations"] == [{"level": "Daily resistance zone 517.43-525.88",
                                            "explanation": "First target at 517.43."}]
    removed = " | ".join(r["text"] + " -> " + r["reason"] for r in v["removed"])
    for bad in ("531.20", "522.75", "540.00", "519.99"):
        assert bad in removed
    assert v["passed"] is False


def test_guarantees_and_certain_predictions_are_removed():
    f = facts()
    out = validate_explanation(good(f, setup_explanation="This breakout is guaranteed to "
                                                         "work. The plan needs a close above "
                                                         "514.30.",
                                    risk_explanation="Risk-free trade. Price is below VWAP."),
                               f)
    e = out["explanation"]
    assert e["setup_explanation"] == "The plan needs a close above 514.30."
    assert e["risk_explanation"] == "Price is below VWAP."
    assert sum(r["reason"].startswith("unsupported") for r in out["validation"]["removed"]) == 2


def test_explanation_rejected_when_core_narrative_is_unverifiable():
    f = facts()
    with pytest.raises(ExplanationInvalid, match="technical_summary"):
        validate_explanation(good(f, technical_summary="Price will hit 600.25 by Friday."), f)


# ---------------------------------------------------------------- Claude request

def test_request_uses_schema_fact_sheet_and_fallbacks():
    f = facts()
    fake = FakeClient(good(f))
    data, model = ClaudeAnalyst("claude-opus-5-5", "medium", client=fake).explain_setup(f)
    call = fake.beta.messages.calls[0]
    assert call["model"] == "claude-opus-5-5" and call["system"] == SYSTEM_PROMPT
    assert call["output_config"]["format"] == {"type": "json_schema", "schema": OUTPUT_SCHEMA}
    assert call["output_config"]["effort"] == "medium"
    assert call["fallbacks"] == "default"
    assert call["betas"] == ["server-side-fallback-2026-07-01"]
    content = call["messages"][0]["content"]
    assert "<fact_sheet>" in content and '"last_price":513.77' in content
    assert data["symbol"] == "SPY" and model == "claude-opus-5-5"


@pytest.mark.parametrize("failure,needle", [
    (lambda: _status_error(anthropic.RateLimitError, 429), "rate limit"),
    (lambda: _status_error(anthropic.AuthenticationError, 401), "API key was rejected"),
    (lambda: _status_error(anthropic.InternalServerError, 500), "API error (500)"),
    (lambda: anthropic.APIConnectionError(request=httpx.Request("POST", "https://x")),
     "network"),
    (lambda: _Resp("", stop="refusal"), "declined"),
    (lambda: _Resp('{"symbol": "SP', stop="max_tokens"), "cut off"),
    (lambda: _Resp("not json at all"), "not valid JSON"),
])
def test_api_failures_become_unavailable_and_keep_the_analysis(service, failure, needle):
    fake = FakeClient(failure())
    service.analyst = ClaudeAnalyst("claude-opus-5-5", client=fake)
    service.ai_enabled = True
    r = service.ai_explanation("SPY", "day")
    assert r["available"] is False and needle in r["reason"]
    assert r["reason"].startswith("AI explanation unavailable")
    assert r["facts"]["symbol"] == "SPY" and r["facts"]["last_price"] > 0  # analysis retained
    assert r["status"] in ("TRIGGERED", "WATCHING", "NO_TRADE")
    # Failures are not cached: the next request tries again.
    fake.beta.messages.script = [_echo]
    assert service.ai_explanation("SPY", "day")["available"] is True


def _facts_from(kwargs) -> dict:
    content = kwargs["messages"][0]["content"]
    return json.loads(content.split("<fact_sheet>\n")[1].split("\n</fact_sheet>")[0])


def _echo(kwargs):
    """A fake Claude that answers from whatever fact sheet it receives."""
    f = _facts_from(kwargs)
    return good(f, technical_summary=f"Price {f['last_price']} is the last close.",
                key_level_explanations=[], setup_explanation="Explained from the sheet.",
                bullish_scenario="", bearish_scenario="", entry_conditions=[],
                invalidation_conditions=[], risk_explanation="Conditions can change.")


def test_schema_violation_from_claude_is_unavailable_not_shown(service):
    service.analyst = ClaudeAnalyst("claude-opus-5-5",
                                    client=FakeClient(lambda kw: {"verdict": "BUY"}))
    service.ai_enabled = True
    r = service.ai_explanation("SPY", "day")
    assert r["available"] is False and "failed validation" in r["reason"]
    assert "explanation" not in r and r["facts"]


def test_explanations_are_cached_by_fact_sheet(service):
    fake = FakeClient(_echo)
    service.analyst = ClaudeAnalyst("claude-opus-5-5", client=fake)
    service.ai_enabled = True
    miss = service.ai_explanation("SPY", "day", generate=False)
    assert miss["available"] is False and miss["not_generated"] and not fake.beta.messages.calls
    first = service.ai_explanation("SPY", "day")
    assert first["available"] is True and first["cached"] is False
    assert first["explanation"]["setup_status"] == first["status"]
    second = service.ai_explanation("SPY", "day")
    assert second["cached"] is True and len(fake.beta.messages.calls) == 1
    assert service.ai_explanation("SPY", "day", generate=False)["cached"] is True
    third = service.ai_explanation("SPY", "day", refresh=True)
    assert third["cached"] is False and len(fake.beta.messages.calls) == 2
    # A different model, effort or fact sheet is a different cache entry.
    f = first["facts"]
    assert cache_key(f, "claude-opus-5-5", "medium") != cache_key(f, "claude-opus-5-5", "high")
    changed = {**f, "last_price": f["last_price"] + 0.01}
    assert cache_key(changed, "claude-opus-5-5", "medium") != cache_key(f, "claude-opus-5-5",
                                                                        "medium")


def test_selected_setup_drives_the_explanation(service):
    service.analyst = ClaudeAnalyst("claude-opus-5-5", client=FakeClient(_echo))
    service.ai_enabled = True
    rows = service.strategy_setups(["SPY"], record=False)["setups"]
    pick = next(r for r in rows if r["plan"] and r["status"] == "NO_TRADE")
    r = service.ai_explanation("SPY", "day", pick["id"])
    assert r["setup_id"] == pick["id"] and r["status"] == "NO_TRADE"
    assert r["facts"]["setup"]["name"] == pick["strategy_name"]
    assert r["explanation"]["setup_status"] == "NO_TRADE"
    swing = service.ai_explanation("AAPL", "swing")
    assert swing["facts"]["trading_mode"] == "swing" and swing["available"] is True


def test_ai_endpoints(service):
    service.ai_enabled = False
    with TestClient(create_app(service, run_scanner=False)) as c:
        r = c.post("/api/ai/SPY", params={"mode": "day"}).json()
        assert r["available"] is False and "ANTHROPIC_API_KEY" in r["reason"]
        assert r["facts"]["symbol"] == "SPY"  # technical analysis still returned
        assert c.get("/api/ai/SPY", params={"mode": "swing"}).json()["facts"]
        assert c.post("/api/ai/SPY", params={"mode": "day", "setup_id": "nope"}).status_code == 400
        assert c.get("/api/ai/SPY", params={"mode": "weekly"}).status_code == 400
        meta = c.post("/api/ai/META", params={"mode": "day"}).json()
        assert meta["facts"]["symbol"] == "META" and meta["status"] == meta["facts"]["setup"]["status"]
        assert c.post("/api/ai/ZZZZ", params={"mode": "day"}).status_code == 400
