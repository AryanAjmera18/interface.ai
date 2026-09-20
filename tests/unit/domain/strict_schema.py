"""Independently check the supported schema subset; forbid live provider calls."""

import re
from typing import Any

from cua.domain.schemas import SchemaDocument


def assert_openai_strict(schema: SchemaDocument) -> None:
    """Guard closed objects, full required sets and tagged OBJECT unions, allowing nullability.

    Nullable scalar/object alternatives are not polymorphic variants. Multi-object unions must
    share a required singleton-enum tag with disjoint values. References are resolved for this
    check but definitions are traversed once, so recursive predicates do not recurse forever.
    """
    root = schema.schema_body
    assert isinstance(root, dict)
    assert root.get("type") == "object", "Strict tool inputs require an object root"

    def resolve(node: Any) -> Any:
        if isinstance(node, dict) and "$ref" in node:
            assert node["$ref"].startswith("#/$defs/")
            return root["$defs"][node["$ref"].split("/")[-1]]
        return node

    def visit(node: Any) -> None:
        if isinstance(node, list):
            for value in node:
                visit(value)
        elif isinstance(node, dict):
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False, "Open-keyed object"
                assert set(node.get("required", ())) == set(node.get("properties", ())), (
                    "Missing required property"
                )
            assert "oneOf" not in node and "discriminator" not in node
            if "anyOf" in node:
                variants = [resolve(value) for value in node["anyOf"]]
                objects = [value for value in variants if value.get("type") == "object"]
                if len(objects) > 1:
                    possible = set(objects[0]["properties"])
                    for variant in objects:
                        possible &= {
                            key
                            for key, prop in variant["properties"].items()
                            if len(prop.get("enum", ())) == 1
                        }
                    assert any(
                        len({variant["properties"][key]["enum"][0] for variant in objects})
                        == len(objects)
                        for key in possible
                    ), "Untagged object union"
            for value in node.values():
                visit(value)

    visit(root)


def validate_instance(instance: Any, document: SchemaDocument) -> None:
    """Independent validator for the emitted subset, not a replacement for Pydantic invariants.

    Provider grammar constraints cannot express journal attribution or cross-field references;
    both provider round trips must additionally pass the SAME domain validation. No API is called.
    """
    root = document.schema_body
    assert isinstance(root, dict)

    def validate(value: Any, schema: Any) -> None:
        if "$ref" in schema:
            validate(value, root["$defs"][schema["$ref"].split("/")[-1]])
            return
        if "anyOf" in schema:
            errors = []
            for branch in schema["anyOf"]:
                try:
                    validate(value, branch)
                    return
                except AssertionError as exc:
                    errors.append(exc)
            raise AssertionError(f"No union variant matched ({len(errors)} variants)")
        if "enum" in schema:
            assert value in schema["enum"]
        kind = schema.get("type")
        if kind == "object":
            assert isinstance(value, dict)
            assert set(schema["required"]) <= set(value)
            assert set(value) <= set(schema["properties"])
            for key, field in value.items():
                validate(field, schema["properties"][key])
        elif kind == "array":
            assert isinstance(value, list)
            assert len(value) >= schema.get("minItems", 0)
            assert len(value) <= schema.get("maxItems", len(value))
            for item in value:
                validate(item, schema["items"])
        elif kind == "string":
            assert isinstance(value, str)
            assert len(value) >= schema.get("minLength", 0)
            assert "pattern" not in schema or re.search(schema["pattern"], value)
        elif kind in {"number", "integer"}:
            assert type(value) in {int, float}
            assert kind != "integer" or int(value) == value
            assert "minimum" not in schema or value >= schema["minimum"]
            assert "maximum" not in schema or value <= schema["maximum"]
            assert "exclusiveMinimum" not in schema or value > schema["exclusiveMinimum"]
        elif kind == "boolean":
            assert type(value) is bool
        elif kind == "null":
            assert value is None

    validate(instance, root)
