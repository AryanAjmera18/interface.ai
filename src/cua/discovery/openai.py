"""Adapt OpenAI Responses strict output to the provider-neutral LLMClient port.

This adapter deliberately uses no hosted computer-use tool. Perception, action dispatch, policy,
and verification remain in this repository because replacing that loop would replace the system
being evaluated rather than supply its model decision seam.
"""

import hashlib
import os
from time import monotonic
from typing import Literal

import httpx
from pydantic import Field, JsonValue

from cua.discovery.anthropic import PlannerOutput
from cua.discovery.pricing import PricingEntry, price_usage
from cua.discovery.usage import normalize_openai_usage
from cua.domain.common import DomainModel
from cua.domain.models import DecisionRequest, DecisionResult
from cua.domain.provenance import ModelRef
from cua.domain.schemas import strict_schema


class _InputDetails(DomainModel):
    cached_tokens: int = 0


class _OutputDetails(DomainModel):
    reasoning_tokens: int = 0


class _ResponseUsage(DomainModel):
    input_tokens: int = 0
    output_tokens: int = 0
    input_tokens_details: _InputDetails = Field(default_factory=_InputDetails)
    output_tokens_details: _OutputDetails = Field(default_factory=_OutputDetails)


class _Content(DomainModel):
    type: Literal["output_text", "refusal"]
    text: str | None = None
    refusal: str | None = None


class _OutputItem(DomainModel):
    type: str
    content: tuple[_Content, ...] = ()


class _Response(DomainModel):
    id: str
    status: str
    output: tuple[_OutputItem, ...]
    usage: _ResponseUsage | None

    def output_text(self) -> str:
        return "".join(
            content.text or ""
            for item in self.output
            for content in item.content
            if content.type == "output_text"
        )


class _ErrorDetail(DomainModel):
    message: str
    type: str
    param: str | None = None
    code: str | None = None


class _ErrorEnvelope(DomainModel):
    error: _ErrorDetail


class OpenAILLMClient:
    """Call `/v1/responses` with the domain Action union embedded in a strict schema."""

    def __init__(
        self,
        model: ModelRef,
        *,
        max_output_tokens: int = 2048,
        pricing: PricingEntry | None = None,
    ) -> None:
        self.model_ref = model
        self.max_output_tokens = max_output_tokens
        self.pricing = pricing

    async def decide(self, request: DecisionRequest) -> DecisionResult:
        started = monotonic()
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("OPENAI_API_KEY is absent")
        body: JsonValue = {
            "model": self.model_ref.model_id,
            "input": request.instruction,
            "max_output_tokens": self.max_output_tokens,
            "reasoning": {"effort": self.model_ref.reasoning_effort or "medium"},
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "discovery_planner_decision",
                    "strict": True,
                    "schema": strict_schema(
                        PlannerOutput, "discovery-planner-decision.v1"
                    ).schema_body,
                }
            },
        }
        async with httpx.AsyncClient(timeout=120) as client:
            raw = await client.post(
                "https://api.openai.com/v1/responses",
                headers={"Authorization": f"Bearer {key}"},
                json=body,
            )
        if raw.is_error:
            envelope = _ErrorEnvelope.model_validate(raw.json())
            detail = envelope.error
            raise RuntimeError(
                f"OpenAI Responses rejected request: status={raw.status_code} "
                f"type={detail.type} code={detail.code} param={detail.param}: {detail.message}"
            )
        # The Responses transport adds fields over time. Select and validate the normalized
        # subset here; strictness still applies to the model-emitted PlannerOutput below.
        response = _Response.model_validate(raw.json(), extra="ignore")
        parsed = PlannerOutput.model_validate_json(response.output_text())
        usage_payload = response.usage.model_dump() if response.usage is not None else {}
        usage = price_usage(normalize_openai_usage(usage_payload), self.pricing)
        return DecisionResult(
            decision_id=request.decision_id,
            intent=parsed.intent,
            action=parsed.action,
            target=parsed.target,
            model=self.model_ref,
            prompt_template_id=request.prompt_template_id,
            prompt_hash=hashlib.sha256(request.instruction.encode()).hexdigest(),
            rationale_digest=hashlib.sha256(parsed.rationale_digest_source.encode()).hexdigest(),
            tool_call_id=response.id,
            usage=usage,
            latency_ms=int((monotonic() - started) * 1000),
            finish_reason="completed",
        )
