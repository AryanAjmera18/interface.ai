"""Adapt Anthropic structured output to the provider-neutral LLMClient port."""

import hashlib
from time import monotonic
from typing import Any

from langchain_anthropic import ChatAnthropic
from pydantic import Field

from cua.discovery.pricing import PricingEntry, price_usage
from cua.domain.actions import Action
from cua.domain.common import DomainModel
from cua.domain.models import DecisionRequest, DecisionResult, Usage
from cua.domain.predicates import AxTarget
from cua.domain.provenance import ModelRef


def normalize_anthropic_usage(metadata: dict[str, Any]) -> Usage:
    """Translate Anthropic/LangChain counters without pretending the response reports USD."""
    details = metadata.get("input_token_details")
    cached = details.get("cache_read", 0) if isinstance(details, dict) else 0
    return Usage(
        input_tokens=int(metadata.get("input_tokens", 0)),
        cached_input_tokens=int(cached),
        output_tokens=int(metadata.get("output_tokens", 0)),
        cost_usd=None,
    )


class PlannerOutput(DomainModel):
    intent: str = Field(min_length=1)
    action: Action
    target: AxTarget | None
    rationale_digest_source: str = Field(
        min_length=1,
        description="Short rationale summary; it is hashed and never persisted verbatim.",
    )


class AnthropicLLMClient:
    """Keep provider response quirks and usage normalization outside the graph."""

    def __init__(
        self,
        model: ModelRef,
        *,
        max_output_tokens: int = 2048,
        pricing: PricingEntry | None = None,
    ) -> None:
        self.model_ref = model
        self.pricing = pricing
        chat = ChatAnthropic(
            model_name=model.model_id,
            max_tokens_to_sample=max_output_tokens,
            timeout=30,
            stop=None,
        )
        self.structured: Any = chat.with_structured_output(PlannerOutput, include_raw=True)

    async def decide(self, request: DecisionRequest) -> DecisionResult:
        started = monotonic()
        response: dict[str, Any] = await self.structured.ainvoke(request.instruction)
        parsed = PlannerOutput.model_validate(response["parsed"])
        raw: Any = response["raw"]
        metadata = getattr(raw, "usage_metadata", None) or {}
        return DecisionResult(
            decision_id=request.decision_id,
            intent=parsed.intent,
            action=parsed.action,
            target=parsed.target,
            model=self.model_ref,
            prompt_template_id=request.prompt_template_id,
            prompt_hash=hashlib.sha256(request.instruction.encode()).hexdigest(),
            rationale_digest=hashlib.sha256(parsed.rationale_digest_source.encode()).hexdigest(),
            tool_call_id=str(getattr(raw, "id", "anthropic-structured-output")),
            usage=price_usage(normalize_anthropic_usage(metadata), self.pricing),
            latency_ms=int((monotonic() - started) * 1000),
            finish_reason="completed",
        )
