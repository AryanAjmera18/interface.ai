"""Emit provider-neutral schema DATA; forbid filesystem/network I/O and other cua packages."""

import json
from typing import cast

from pydantic import BaseModel, JsonValue, TypeAdapter

from cua.domain.actions import ActionInput
from cua.domain.capability import Capability
from cua.domain.common import DomainModel, canonical_json
from cua.domain.models import DecisionRequest, DecisionResult
from cua.domain.observation import Observation
from cua.domain.results import ReplayResult


class SchemaDocument(DomainModel):
    """Schema documents themselves are metadata, not model tool inputs."""

    name: str
    schema_body: JsonValue

    def text(self) -> str:
        return canonical_json(self.schema_body) + "\n"


def strict_schema(model: type[BaseModel], name: str) -> SchemaDocument:
    """Require canonical full serialization; null represents absence, never omitted keys.

    Discriminated oneOf becomes equivalent anyOf because literal tags are disjoint. Remove
    only annotations (default/discriminator), not validation constraints. No provider-specific
    rewriting exists: OpenAI and Anthropic wrappers carry this exact same schema document.
    """
    value = cast(JsonValue, model.model_json_schema(mode="serialization"))

    def visit(node: JsonValue) -> None:
        if isinstance(node, list):
            for item in node:
                visit(item)
        elif isinstance(node, dict):
            node.pop("default", None)
            node.pop("discriminator", None)
            if "const" in node:
                node["enum"] = [node.pop("const")]
            enum_values = node.get("enum")
            if isinstance(enum_values, list) and name.endswith(".v2"):
                # JSON Schema assigns no meaning to enum order. Pydantic can obtain
                # Literal values through set-backed typing internals, so that order can
                # vary with import history. Canonical sorting keeps emitted schemas and
                # their provenance digests byte-stable across processes.
                enum_values.sort(key=canonical_json)
            if "oneOf" in node:
                node["anyOf"] = node.pop("oneOf")
            if node.get("type") == "object":
                if node.get("additionalProperties") is not False:
                    raise ValueError(f"{name}: open-keyed object is not strict-representable")
                properties = node.get("properties", {})
                if not isinstance(properties, dict):
                    raise ValueError("Object properties must be a schema object")
                node["required"] = list(properties)
            # Property names may themselves be keywords (ParamSpec.default is one).
            # Walk schema positions, never treat a property-name map as a schema node.
            for keyword in ("$defs", "properties"):
                children = node.get(keyword)
                if isinstance(children, dict):
                    for child in children.values():
                        visit(child)
            for keyword in ("items", "anyOf", "allOf", "additionalProperties"):
                child = node.get(keyword)
                if isinstance(child, (dict, list)):
                    visit(child)

    visit(value)
    return SchemaDocument(name=name, schema_body=value)


def public_schemas() -> tuple[SchemaDocument, ...]:
    """ReplayResult is deliberately ordinary JSON Schema, not a strict tool-input schema."""
    return (
        strict_schema(Capability, "capability.v2"),
        strict_schema(ActionInput, "action.v2"),
        strict_schema(Observation, "observation.v1"),
        strict_schema(DecisionRequest, "decision-request.v1"),
        strict_schema(DecisionResult, "decision-result.v2"),
        SchemaDocument(
            name="replay-result.v2",
            schema_body=cast(
                JsonValue, TypeAdapter(ReplayResult).json_schema(mode="serialization")
            ),
        ),
    )


def provider_tool(schema: SchemaDocument, provider: str) -> SchemaDocument:
    if provider == "openai":
        body: JsonValue = {
            "type": "function",
            "function": {
                "name": schema.name.replace(".", "_"),
                "description": "Typed domain data",
                "strict": True,
                "parameters": schema.schema_body,
            },
        }
    elif provider == "anthropic":
        body = {
            "name": schema.name.replace(".", "_"),
            "description": "Typed domain data",
            "input_schema": schema.schema_body,
        }
    else:
        raise ValueError("Schema provider must be openai or anthropic")
    return SchemaDocument(name=f"{provider}-{schema.name}", schema_body=body)


def artifact_json(capability: Capability) -> str:
    return (
        json.dumps(
            json.loads(capability.model_dump_json()),
            sort_keys=True,
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    )
