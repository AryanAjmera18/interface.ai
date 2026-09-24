"""Invoke an OpenAI catalog agent while keeping UI perception and action inside this system."""

import json
import os
from time import monotonic

import httpx
from pydantic import Field, JsonValue

from cua.catalog.registry import CatalogTool
from cua.discovery.openai import openai_authorization_headers
from cua.discovery.pricing import PricingEntry, price_usage
from cua.discovery.usage import normalize_openai_usage
from cua.domain.common import DomainModel, NonEmpty
from cua.domain.models import Usage
from cua.domain.provenance import ModelRef


class CatalogToolCall(DomainModel):
    response_id: NonEmpty
    call_id: NonEmpty
    name: NonEmpty
    arguments_json: NonEmpty
    usage: Usage
    latency_ms: int = Field(ge=0)


class CatalogAnswer(DomainModel):
    response_id: NonEmpty
    text: NonEmpty
    usage: Usage
    latency_ms: int = Field(ge=0)


class _InputDetails(DomainModel):
    cached_tokens: int = 0


class _OutputDetails(DomainModel):
    reasoning_tokens: int = 0


class _Usage(DomainModel):
    input_tokens: int = 0
    output_tokens: int = 0
    input_tokens_details: _InputDetails = Field(default_factory=_InputDetails)
    output_tokens_details: _OutputDetails = Field(default_factory=_OutputDetails)


class _Content(DomainModel):
    type: str
    text: str | None = None


class _Output(DomainModel):
    type: str
    call_id: str | None = None
    name: str | None = None
    arguments: str | None = None
    content: tuple[_Content, ...] = ()


class _Response(DomainModel):
    id: NonEmpty
    output: tuple[_Output, ...]
    usage: _Usage | None = None


class OpenAICatalogAgent:
    """Use strict function tools; hosted computer-use would replace the graded loop."""

    def __init__(
        self,
        model: ModelRef,
        *,
        pricing: PricingEntry | None,
        max_output_tokens: int = 256,
        timeout_s: float = 60,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.model = model
        self.pricing = pricing
        self.max_output_tokens = max_output_tokens
        self.timeout_s = timeout_s
        self.transport = transport

    def _usage(self, response: _Response) -> Usage:
        payload = response.usage.model_dump() if response.usage is not None else {}
        return price_usage(normalize_openai_usage(payload), self.pricing)

    async def _post(self, body: object) -> tuple[_Response, int]:
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("OPENAI_API_KEY is absent")
        started = monotonic()
        async with httpx.AsyncClient(timeout=self.timeout_s, transport=self.transport) as client:
            raw = await client.post(
                "https://api.openai.com/v1/responses",
                headers=openai_authorization_headers(key),
                json=body,
            )
        raw.raise_for_status()
        return _Response.model_validate(raw.json(), extra="ignore"), int(
            (monotonic() - started) * 1000
        )

    async def select(self, question: str, tools: tuple[CatalogTool, ...]) -> CatalogToolCall:
        body = {
            "model": self.model.model_id,
            "input": question,
            "reasoning": {"effort": self.model.reasoning_effort or "low"},
            "max_output_tokens": self.max_output_tokens,
            "tools": [
                {
                    "type": "function",
                    "name": item.name,
                    "description": item.description,
                    "parameters": item.parameters,
                    "strict": True,
                }
                for item in tools
            ],
            "tool_choice": "required",
        }
        response, latency = await self._post(body)
        call = next((item for item in response.output if item.type == "function_call"), None)
        if call is None or call.call_id is None or call.name is None or call.arguments is None:
            raise ValueError("Catalog agent returned no typed function call")
        json.loads(call.arguments)
        return CatalogToolCall(
            response_id=response.id,
            call_id=call.call_id,
            name=call.name,
            arguments_json=call.arguments,
            usage=self._usage(response),
            latency_ms=latency,
        )

    async def answer(self, call: CatalogToolCall, tool_output_json: str) -> CatalogAnswer:
        response, latency = await self._post(
            {
                "model": self.model.model_id,
                "previous_response_id": call.response_id,
                "input": [
                    {
                        "type": "function_call_output",
                        "call_id": call.call_id,
                        "output": tool_output_json,
                    }
                ],
                "reasoning": {"effort": self.model.reasoning_effort or "low"},
                "max_output_tokens": self.max_output_tokens,
            }
        )
        text = "".join(
            content.text or ""
            for item in response.output
            for content in item.content
            if content.type == "output_text"
        )
        return CatalogAnswer(
            response_id=response.id,
            text=text,
            usage=self._usage(response),
            latency_ms=latency,
        )


class CatalogRunManifest(DomainModel):
    """Publish redacted catalog evidence while linking the model-free replay bundle."""

    model: ModelRef
    question: JsonValue
    tool_name: NonEmpty
    arguments: JsonValue
    tool_result: JsonValue
    answer: JsonValue | None = None
    linked_replay: NonEmpty
    input_tokens: int = Field(ge=0)
    cached_input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    reasoning_tokens: int = Field(ge=0)
    cost_usd: float | None = Field(default=None, ge=0)
    pricing_source_url: str | None = None
    pricing_retrieved_on: str | None = None
