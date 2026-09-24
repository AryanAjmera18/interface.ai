"""Adapt OpenAI Responses strict output to the provider-neutral LLMClient port.

This adapter deliberately uses no hosted computer-use tool. Perception, action dispatch, policy,
and verification remain in this repository because replacing that loop would replace the system
being evaluated rather than supply its model decision seam.
"""

import asyncio
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


class ProviderResponseError(RuntimeError):
    """Expose only typed safe provider metadata; response messages can echo secrets."""

    def __init__(self, *, status: int, error_type: str, code: str | None) -> None:
        self.status = status
        self.error_type = error_type
        self.code = code or "unknown"
        super().__init__(
            f"OpenAI Responses rejected request: status={status} type={error_type} code={self.code}"
        )


def openai_authorization_headers(key: str) -> httpx.Headers:
    """Centralize the sole reviewable provider authorization construction."""
    return httpx.Headers(
        headers={"Authorization": f"Bearer {key}"},
    )


class OpenAILLMClient:
    """Call `/v1/responses` with the domain Action union embedded in a strict schema."""

    def __init__(
        self,
        model: ModelRef,
        *,
        max_output_tokens: int = 2048,
        pricing: PricingEntry | None = None,
        timeout_s: float = 120,
        max_attempts: int = 3,
        retry_backoff_s: float = 0.25,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if max_attempts < 1 or timeout_s <= 0 or retry_backoff_s < 0:
            raise ValueError("OpenAI timeout/retry settings are invalid")
        self.model_ref = model
        self.max_output_tokens = max_output_tokens
        self.pricing = pricing
        self.timeout_s = timeout_s
        self.max_attempts = max_attempts
        self.retry_backoff_s = retry_backoff_s
        self.transport = transport

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
        async with httpx.AsyncClient(timeout=self.timeout_s, transport=self.transport) as client:
            for attempt in range(self.max_attempts):
                raw = await client.post(
                    "https://api.openai.com/v1/responses",
                    headers=openai_authorization_headers(key),
                    json=body,
                )
                if raw.status_code not in {429, 500, 502, 503, 504}:
                    break
                if attempt + 1 < self.max_attempts:
                    await asyncio.sleep(self.retry_backoff_s * (2**attempt))
        if raw.is_error:
            envelope = _ErrorEnvelope.model_validate(raw.json())
            detail = envelope.error
            raise ProviderResponseError(
                status=raw.status_code, error_type=detail.type, code=detail.code
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
            goal_reached=parsed.goal_reached,
        )
