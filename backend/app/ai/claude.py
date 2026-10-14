"""Claude-generated explanations of the engine's analysis.

Claude does not detect setups or choose prices. It receives the deterministic engine output
(levels, bias, setup, checklist, recent bars) and explains it in plain language. Its verdict
may be more cautious than the engine's, never more aggressive.
"""

from __future__ import annotations

import json
import logging

import anthropic

log = logging.getLogger(__name__)

# Models that accept the server-side `fallbacks: "default"` option on the Claude API.
_FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}

SYSTEM_PROMPT = """You are the analysis explainer inside TradeScope AI, a stock and options \
trading analysis dashboard for day and swing traders. A deterministic rules engine has already \
computed indicators, support/resistance, market bias and a candidate trade setup. Your job is \
to explain that analysis clearly to a trader who will make their own decision.

Rules:
- Use only the numbers provided. Never invent prices, levels, news, earnings dates or other \
facts that are not in the input.
- Your verdict may be more cautious than the engine's status, never more aggressive. If the \
engine says NO_TRADE, your verdict is NO_TRADE. If it says FORMING, your verdict is WAIT or \
NO_TRADE. Only an ACTIONABLE engine status can receive TAKE_SETUP, and only if you agree.
- Saying there is no trade is a good outcome when conditions are unclear; say so plainly.
- Be specific: reference the actual entry, stop, targets, levels and checklist items.
- Point out what would invalidate the idea and what would need to change for a better setup.
- analysis.key_levels lists automatically detected levels with evidence. Their strength
scores are heuristic rankings of supporting evidence, not probabilities of a reversal; never
describe them as odds or likelihoods.
- If the input says the data is DEMO DATA, state in the summary that the analysis is based on \
simulated demo data and is not a real market read.
- This is educational analysis, not financial advice. Do not tell the user to buy or sell; \
describe the conditions instead. Keep each bullet to one or two sentences."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string", "description": "One-line takeaway, max ~15 words"},
        "verdict": {"type": "string", "enum": ["TAKE_SETUP", "WAIT", "NO_TRADE"]},
        "summary": {"type": "string", "description": "2-4 sentence plain-language summary"},
        "bullish_case": {"type": "array", "items": {"type": "string"}},
        "bearish_case": {"type": "array", "items": {"type": "string"}},
        "key_levels_commentary": {"type": "string"},
        "what_to_watch": {"type": "array", "items": {"type": "string"}},
        "risk_notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["headline", "verdict", "summary", "bullish_case", "bearish_case",
                 "key_levels_commentary", "what_to_watch", "risk_notes"],
    "additionalProperties": False,
}

_VERDICT_CAP = {"NO_TRADE": {"NO_TRADE"}, "FORMING": {"WAIT", "NO_TRADE"},
                "ACTIONABLE": {"TAKE_SETUP", "WAIT", "NO_TRADE"}}


class AIUnavailable(Exception):
    """Claude could not produce an analysis (no key, API error, refusal...)."""


def build_prompt_payload(analysis: dict, recent_bars: list[dict], is_demo: bool) -> dict:
    trimmed = {k: v for k, v in analysis.items() if k not in ("overlays",)}
    return {
        "data_source": "DEMO DATA (simulated, not real market prices)" if is_demo
        else "Alpaca market data; feed, coverage and latency are in analysis.data_status",
        "analysis": trimmed,
        "recent_bars": recent_bars,
    }


class ClaudeAnalyst:
    def __init__(self, model: str, effort: str = "medium",
                 client: anthropic.Anthropic | None = None) -> None:
        self.model = model
        self.effort = effort
        self._client = client

    @property
    def client(self) -> anthropic.Anthropic:
        if self._client is None:
            self._client = anthropic.Anthropic()
        return self._client

    def explain(self, analysis: dict, recent_bars: list[dict], is_demo: bool) -> dict:
        payload = build_prompt_payload(analysis, recent_bars, is_demo)
        user_text = (
            f"Explain this {analysis['mode']}-trading analysis of {analysis['symbol']} on the "
            f"{analysis['timeframe']} timeframe. Engine status: {analysis['setup']['status']}.\n\n"
            f"<analysis_json>\n{json.dumps(payload, separators=(',', ':'))}\n</analysis_json>"
        )
        kwargs: dict = dict(
            model=self.model,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_text}],
            output_config={"effort": self.effort,
                           "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
        )
        if self.model in _FALLBACK_MODELS:
            # On a safety-classifier decline, let the API re-run on its recommended fallback.
            kwargs["betas"] = ["server-side-fallback-2026-07-01"]
            kwargs["fallbacks"] = "default"
        try:
            response = self.client.beta.messages.create(**kwargs)
        except anthropic.AuthenticationError as exc:
            raise AIUnavailable("Anthropic API key was rejected. Check ANTHROPIC_API_KEY.") from exc
        except anthropic.PermissionDeniedError as exc:
            raise AIUnavailable("This API key does not have access to the configured model.") from exc
        except anthropic.NotFoundError as exc:
            raise AIUnavailable(f"Model {self.model!r} was not found. Check CLAUDE_MODEL.") from exc
        except anthropic.RateLimitError as exc:
            raise AIUnavailable("Claude rate limit reached. Try again shortly.") from exc
        except anthropic.BadRequestError as exc:
            raise AIUnavailable(f"Claude rejected the request: {exc.message}") from exc
        except anthropic.APIStatusError as exc:
            raise AIUnavailable(f"Claude API error ({exc.status_code}). Try again later.") from exc
        except anthropic.APIConnectionError as exc:
            raise AIUnavailable("Could not reach the Claude API (network error).") from exc

        if response.stop_reason == "refusal":
            raise AIUnavailable("Claude declined to analyze this request.")
        if response.stop_reason == "max_tokens":
            raise AIUnavailable("Claude's response was cut off; try again.")
        text = "".join(b.text for b in response.content if b.type == "text")
        try:
            result = json.loads(text)
        except json.JSONDecodeError as exc:
            log.warning("Unparseable Claude output (request %s)", response._request_id)
            raise AIUnavailable("Claude returned an unexpected format.") from exc

        # Enforce the "never more aggressive than the engine" rule.
        allowed = _VERDICT_CAP.get(analysis["setup"]["status"], {"NO_TRADE"})
        if result.get("verdict") not in allowed:
            result["verdict"] = "WAIT" if "WAIT" in allowed else "NO_TRADE"
        result["model"] = response.model
        return result
