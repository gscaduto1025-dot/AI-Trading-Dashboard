"""Claude explanations of verified technical analysis.

Claude does not detect setups, choose prices or decide statuses. It receives the
deterministic fact sheet (app/ai/facts.py) for one symbol and setup and returns a
structured explanation (app/ai/validate.py OUTPUT_SCHEMA). The response is validated
against the fact sheet before anyone sees it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

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
- setup_status, symbol, trading_mode, market_bias, setup_name, evidence_label and \
data_timestamp must be copied from the sheet (setup.status, symbol, trading_mode, \
market_bias.label, "<setup.name> (<direction>, <timeframe>)" or "No setup", \
strategy_evidence.label or "Unvalidated", data_timestamp). You cannot change a status, an \
entry, a stop or a target. If the status is NO_TRADE, explain why from setup.status_reasons \
and failed_checks; never suggest taking the trade anyway.
- A TRIGGERED setup whose live_entry.signal_state is HISTORICALLY_TRIGGERED is not a fresh \
entry now: say so and give live_entry.checks_failed.
- strategy_evidence is what a frozen out-of-sample validation found for this STRATEGY after \
costs. Never describe a strategy as profitable, proven or having an edge unless its status is \
VALIDATED_POSITIVE. Do not compute a probability or expected return for the setup.

Content (concise, specific, useful):
- technical_summary: current price action - market bias factors, moving-average alignment, \
VWAP relationship (day trading), volume and its verification.
- market_structure: trend and structure - higher-timeframe context, setup.context, where \
price sits between nearest_support and nearest_resistance.
- key_level_explanations: the 2-4 levels that matter most right now (nearest support and \
resistance, plus setup levels) and why each matters, using its evidence.
- setup_explanation: why the setup exists - the technical evidence for it (or why there is \
none).
- bullish_scenario / bearish_scenario: what price action at which listed levels would support \
each outcome. Describe conditions, not forecasts.
- entry_conditions: the confirmation still required, from setup.confirmation and the entry \
trigger; for a TRIGGERED setup, what confirmed it.
- invalidation_conditions: from setup.invalidation and the stop.
- risk_reward: the planned entry, stop and targets, risk and reward per share and the \
reward/risk ratios against setup.min_risk_reward, all from setup; for NO_TRADE say which check \
failed.
- historical_evidence: one or two sentences restating strategy_evidence (label, trades, net R \
per trade and its confidence interval) and what it means for this setup: a technical setup, \
not a validated trade, unless the label is VALIDATED_POSITIVE.
- risk_explanation: major risks, uncertainty and conflicting evidence (conflicting_signals, \
failed checks, opposing bias factors, unverified volume, stale or demo data).

Never promise or imply guaranteed outcomes, certainty or profitability; this is educational \
analysis, not financial advice, and you do not tell anyone to buy or sell. If the data source \
is DEMO DATA, say in technical_summary that it is simulated data. One to three sentences per \
text field; one sentence per list item."""


@dataclass
class Explanation:
    data: dict
    model: str
    usage: dict = field(default_factory=dict)
    request_id: str | None = None


class AIUnavailable(Exception):
    """Claude could not produce an explanation (no key, API error, refusal, bad output)."""


class ClaudeAnalyst:
    TIMEOUT_SECONDS = 120.0  # one non-streaming structured response
    MAX_RETRIES = 2  # SDK retries 408/409/429/5xx and connection errors with backoff
    MAX_TOKENS = 16000  # includes adaptive thinking; the explanation itself is ~1-2K

    def __init__(self, model: str, effort: str = "medium",
                 client: anthropic.Anthropic | None = None) -> None:
        self.model = model
        self.effort = effort
        self._client = client

    @property
    def client(self) -> anthropic.Anthropic:
        if self._client is None:
            # Credentials come from the environment (ANTHROPIC_API_KEY); never logged.
            self._client = anthropic.Anthropic(timeout=self.TIMEOUT_SECONDS,
                                               max_retries=self.MAX_RETRIES)
        return self._client

    def explain_setup(self, facts: dict) -> Explanation:
        """Ask Claude for the structured explanation. The caller validates it against
        `facts`."""
        user_text = (
            "Explain this setup using only the fact sheet below.\n\n"
            f"<fact_sheet>\n{json.dumps(facts, separators=(',', ':'), sort_keys=True)}\n"
            "</fact_sheet>")
        kwargs: dict = dict(
            model=self.model,
            max_tokens=self.MAX_TOKENS,
            # The instructions are identical for every request: cache them (5 minutes).
            system=[{"type": "text", "text": SYSTEM_PROMPT,
                     "cache_control": {"type": "ephemeral"}}],
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
        except anthropic.APITimeoutError as exc:
            raise AIUnavailable("Claude did not answer in time. Try again.") from exc
        except anthropic.BadRequestError as exc:
            raise AIUnavailable(f"Claude rejected the request: {exc.message}") from exc
        except anthropic.APIStatusError as exc:
            raise AIUnavailable(f"Claude API error ({exc.status_code}). Try again later.") from exc
        except anthropic.APIConnectionError as exc:
            raise AIUnavailable("Could not reach the Claude API (network error).") from exc

        if response.stop_reason == "refusal":
            cat = getattr(getattr(response, "stop_details", None), "category", None)
            raise AIUnavailable("Claude declined to analyze this request"
                                + (f" ({cat})." if cat else "."))
        if response.stop_reason == "max_tokens":
            raise AIUnavailable("Claude's response was cut off; try again.")
        text = "".join(b.text for b in response.content if b.type == "text")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            log.warning("Unparseable Claude output (request %s)", response._request_id)
            raise AIUnavailable("Claude returned output that is not valid JSON.") from exc
        return Explanation(data, response.model, usage_dict(response),
                           getattr(response, "_request_id", None))


def usage_dict(response) -> dict:
    u = getattr(response, "usage", None)
    return {k: int(getattr(u, k, 0) or 0) for k in (
        "input_tokens", "output_tokens", "cache_read_input_tokens",
        "cache_creation_input_tokens")} if u is not None else {}
