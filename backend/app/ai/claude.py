"""Claude explanations of verified technical analysis.

Claude does not detect setups, choose prices or decide statuses. It receives the
deterministic fact sheet (app/ai/facts.py) for one symbol and setup and returns a
structured explanation (app/ai/validate.py OUTPUT_SCHEMA). The response is validated
against the fact sheet before anyone sees it.
"""

from __future__ import annotations

import json
import logging

import anthropic

from .validate import OUTPUT_SCHEMA

log = logging.getLogger(__name__)

# Models that accept the server-side `fallbacks: "default"` option on the Claude API.
_FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}

SYSTEM_PROMPT = """You are the market-analysis explainer inside TradeScope AI, an analysis \
dashboard for day and swing traders. Deterministic engines have already measured the market: \
bias, moving averages, VWAP, volume, support/resistance zones and a rules-based trade plan. \
You receive their output as a fact sheet and explain it to a trader who makes their own \
decisions.

Numbers:
- Every price, level, percentage, ratio or other number you write must be copied exactly as \
it appears in the fact sheet. Do not compute, round differently, estimate or extrapolate new \
numbers, and never mention a price or level that is not in the sheet. Unverifiable sentences \
are removed automatically, so a number you invent is wasted space.
- Refer to levels by their label and price as given (for example "Daily resistance zone \
517.43-525.88").

Authority:
- setup_status, symbol, trading_mode, market_bias, setup_name and data_timestamp must be \
copied from the sheet (setup.status, symbol, trading_mode, market_bias.label, \
"<setup.name> (<direction>, <timeframe>)" or "No setup", data_timestamp). You cannot change a \
status, an entry, a stop or a target. If the status is NO_TRADE, explain why from \
setup.status_reasons and failed_checks; never suggest taking the trade anyway.

Content (concise, specific, useful):
- technical_summary: current price structure - market bias factors, higher-timeframe trend, \
moving-average alignment, VWAP relationship (day trading), volume.
- key_level_explanations: the 2-4 levels that matter most right now (nearest support and \
resistance, plus setup levels) and why each matters, using its evidence.
- setup_explanation: the technical evidence for the setup (or why there is none).
- bullish_scenario / bearish_scenario: what price action at which listed levels would support \
each outcome. Describe conditions, not forecasts.
- entry_conditions: the confirmation still required, from setup.confirmation and the entry \
trigger; for a TRIGGERED setup, what confirmed it.
- invalidation_conditions: from setup.invalidation and the stop.
- risk_explanation: major risks, uncertainty and conflicting evidence (conflicting_signals, \
failed checks, opposing bias factors, thin volume, demo data).

Never promise or imply guaranteed outcomes, certainty or profitability; this is educational \
analysis, not financial advice, and you do not tell anyone to buy or sell. If the data source \
is DEMO DATA, say in technical_summary that it is simulated data. One to three sentences per \
text field; one sentence per list item."""


class AIUnavailable(Exception):
    """Claude could not produce an explanation (no key, API error, refusal, bad output)."""


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

    def explain_setup(self, facts: dict) -> tuple[dict, str]:
        """Ask Claude for the structured explanation. Returns (parsed JSON, model).
        The caller validates it against `facts`."""
        user_text = (
            "Explain this setup using only the fact sheet below.\n\n"
            f"<fact_sheet>\n{json.dumps(facts, separators=(',', ':'), sort_keys=True)}\n"
            "</fact_sheet>")
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
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            log.warning("Unparseable Claude output (request %s)", response._request_id)
            raise AIUnavailable("Claude returned output that is not valid JSON.") from exc
        return data, response.model
