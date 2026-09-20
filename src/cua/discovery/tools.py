"""Generate planner tool schemas from domain actions; forbid provider-specific branches."""

from typing import Literal

from pydantic import JsonValue, TypeAdapter

from cua.domain.actions import Action
from cua.domain.common import DomainModel


class ToolSchema(DomainModel):
    name: str
    input_schema: JsonValue


class NoArguments(DomainModel):
    kind: Literal["see_screenshot", "request_human_help", "declare_done"]
    reason: str | None


def action_schema() -> JsonValue:
    return TypeAdapter(Action).json_schema()


def planner_tools() -> tuple[ToolSchema, ...]:
    schema = action_schema()
    raw_variants = schema.get("oneOf", []) if isinstance(schema, dict) else []
    variants = raw_variants if isinstance(raw_variants, list) else []
    tools = []
    for variant in variants:
        if not isinstance(variant, dict) or "$ref" not in variant:
            continue
        name = str(variant["$ref"]).rsplit("/", 1)[-1]
        tools.append(ToolSchema(name=name, input_schema=variant))
    extras = TypeAdapter(NoArguments).json_schema()
    tools.extend(
        ToolSchema(name=name, input_schema=extras)
        for name in ("see_screenshot", "request_human_help", "declare_done")
    )
    return tuple(tools)
