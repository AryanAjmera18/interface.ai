"""Test approved-only catalog loading and recorded OpenAI tool-call responses."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from cua.catalog.openai import OpenAICatalogAgent
from cua.catalog.registry import CapabilityCatalog, tool_for
from cua.discovery.pricing import load_pricing
from cua.domain.capability import Capability
from cua.domain.provenance import ModelRef


def approved() -> Capability:
    return Capability.model_validate_json(
        Path("capabilities/look_up_member_savings_balance@1.0.0.json").read_bytes()
    )


def test_tool_schema_is_derived_from_param_specs() -> None:
    capability = approved()
    tool = tool_for(capability)
    parameters = tool.parameters
    assert isinstance(parameters, dict)
    assert parameters["additionalProperties"] is False
    assert parameters["required"] == ["member_id"]
    assert parameters["properties"]["member_id"]["pattern"] == r"^\d{5}$"


def test_catalog_exposes_only_approved_capabilities(tmp_path: Path) -> None:
    capability = approved()
    (tmp_path / "approved.json").write_text(capability.model_dump_json(), encoding="utf-8")
    draft = capability.model_copy(
        update={
            "status": "draft",
            "provenance": capability.provenance.model_copy(update={"approval": None}),
        }
    )
    (tmp_path / "draft.json").write_text(draft.model_dump_json(), encoding="utf-8")

    catalog = CapabilityCatalog.load(tmp_path)

    assert len(catalog.entries) == 1
    assert catalog.entries[0].capability.status == "approved"


@pytest.mark.asyncio
async def test_recorded_catalog_responses_normalize_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict[str, Any]] = []
    responses = [
        {
            "id": "resp_select",
            "output": [
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "look_up_member_savings_balance",
                    "arguments": '{"member_id":"10023"}',
                }
            ],
            "usage": {
                "input_tokens": 200,
                "output_tokens": 40,
                "input_tokens_details": {"cached_tokens": 50},
                "output_tokens_details": {"reasoning_tokens": 12},
            },
        },
        {
            "id": "resp_answer",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "The balance is available."}],
                }
            ],
            "usage": {
                "input_tokens": 100,
                "output_tokens": 20,
                "input_tokens_details": {"cached_tokens": 25},
                "output_tokens_details": {"reasoning_tokens": 5},
            },
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=responses[len(requests) - 1])

    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-test-key")
    pricing = load_pricing(Path("config/pricing.yaml")).get("openai", "gpt-5.6-luna")
    agent = OpenAICatalogAgent(
        ModelRef(
            provider="openai",
            model_id="gpt-5.6-luna",
            api_flavor="responses",
            structured_output_mode="function_tools",
            reasoning_effort="low",
        ),
        pricing=pricing,
        transport=httpx.MockTransport(handler),
    )
    tool = tool_for(approved())

    call = await agent.select("question", (tool,))
    answer = await agent.answer(call, '{"kind":"success"}')

    assert call.name == tool.name
    assert call.usage.cached_input_tokens == 50
    assert call.usage.reasoning_tokens == 12
    assert answer.usage.cached_input_tokens == 25
    assert answer.usage.reasoning_tokens == 5
    assert requests[0]["tools"][0]["strict"] is True
    assert requests[1]["previous_response_id"] == "resp_select"
    assert call.usage.cost_usd is not None
    assert answer.usage.cost_usd is not None
