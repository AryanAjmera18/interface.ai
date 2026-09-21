"""Exercise bounded OpenAI transport retries with offline recorded-shape responses."""

import json

import httpx
import pytest

from cua.discovery.openai import OpenAILLMClient
from cua.domain.common import digest
from cua.domain.models import DecisionRequest, ModelRole
from cua.domain.provenance import ModelRef
from tests.unit.domain.samples import IDENTIFIER, observation


def _request() -> DecisionRequest:
    return DecisionRequest(
        decision_id=IDENTIFIER,
        role=ModelRole.DISCOVERY_PLANNER,
        instruction="Choose an action from the observed page",
        observation=observation(),
        prompt_template_id="test.v1",
        tool_schema_hash=digest({"tool": "test"}),
    )


def _model() -> ModelRef:
    return ModelRef(
        provider="openai",
        model_id="gpt-6-astra",
        api_flavor="responses",
        structured_output_mode="json_schema",
        reasoning_effort="medium",
    )


def _response() -> dict[str, object]:
    output = {
        "intent": "Click the visible control",
        "action": {"kind": "click"},
        "target": None,
        "rationale_digest_source": "The control is present",
    }
    return {
        "id": "resp_fixture",
        "status": "completed",
        "output": [
            {"type": "message", "content": [{"type": "output_text", "text": json.dumps(output)}]}
        ],
        "usage": {
            "input_tokens": 150,
            "output_tokens": 40,
            "input_tokens_details": {"cached_tokens": 50},
            "output_tokens_details": {"reasoning_tokens": 12},
        },
    }


@pytest.mark.asyncio
async def test_retries_429_and_5xx_then_normalizes_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-only")
    seen: list[int] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(len(seen))
        if len(seen) == 1:
            return httpx.Response(429, json={"error": {"message": "retry", "type": "rate_limit"}})
        if len(seen) == 2:
            return httpx.Response(503, json={"error": {"message": "retry", "type": "server"}})
        return httpx.Response(200, json=_response())

    client = OpenAILLMClient(_model(), transport=httpx.MockTransport(respond), retry_backoff_s=0)
    result = await client.decide(_request())
    assert len(seen) == 3
    assert (result.usage.input_tokens, result.usage.cached_input_tokens) == (150, 50)
    assert (result.usage.output_tokens, result.usage.reasoning_tokens) == (40, 12)


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "expected_calls"), [(400, 1), (503, 3)])
async def test_nonretryable_and_exhausted_errors_hide_provider_message(
    monkeypatch: pytest.MonkeyPatch, status: int, expected_calls: int
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-only")
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            status,
            json={
                "error": {
                    "message": "provider echoed a synthetic secret",
                    "type": "fixture_error",
                    "code": "fixture_code",
                }
            },
        )

    client = OpenAILLMClient(_model(), transport=httpx.MockTransport(respond), retry_backoff_s=0)
    with pytest.raises(RuntimeError, match="fixture_error") as caught:
        await client.decide(_request())
    assert calls == expected_calls
    assert "synthetic secret" not in str(caught.value)
