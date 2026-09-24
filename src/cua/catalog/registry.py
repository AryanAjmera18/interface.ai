"""Load approved capabilities and expose provider-neutral catalog tools."""

import re
from pathlib import Path
from typing import Literal

from pydantic import JsonValue

from cua.domain.capability import (
    Capability,
    EnumValidation,
    ParamSpec,
    RangeValidation,
    RegexValidation,
)
from cua.domain.common import DomainModel, NonEmpty
from cua.replay.overrides import TenantOverride


class CatalogTool(DomainModel):
    type: Literal["function"] = "function"
    name: NonEmpty
    description: NonEmpty
    parameters: JsonValue
    strict: Literal[True] = True
    capability_id: NonEmpty


class CatalogEntry(DomainModel):
    artifact_path: Path
    capability: Capability
    override_path: Path | None = None
    tenant_override: TenantOverride | None = None
    tool: CatalogTool


def _parameter_schema(parameter: ParamSpec) -> JsonValue:
    body: dict[str, JsonValue] = {
        "type": parameter.json_type,
        "description": parameter.description,
    }
    if isinstance(parameter.validation, RegexValidation):
        body["pattern"] = parameter.validation.pattern
    elif isinstance(parameter.validation, RangeValidation):
        if parameter.validation.minimum is not None:
            body["minimum"] = parameter.validation.minimum
        if parameter.validation.maximum is not None:
            body["maximum"] = parameter.validation.maximum
    elif isinstance(parameter.validation, EnumValidation):
        body["enum"] = [item.value for item in parameter.validation.values]
    return body


def tool_for(capability: Capability) -> CatalogTool:
    """Derive tool input schema from ParamSpec so catalog calls cannot drift from replay."""
    exposed = tuple(item for item in capability.inputs if item.sensitivity != "secret")
    properties = {item.name: _parameter_schema(item) for item in exposed}
    parameters: JsonValue = {
        "type": "object",
        "properties": properties,
        "required": [item.name for item in exposed],
        "additionalProperties": False,
    }
    return CatalogTool(
        name=re.sub(r"[^a-z0-9_]", "_", capability.name.casefold()).strip("_"),
        description=capability.description,
        parameters=parameters,
        capability_id=capability.capability_id,
    )


class CapabilityCatalog:
    """Expose approved artifacts only; tenant overrides never change the tool contract."""

    def __init__(self, entries: tuple[CatalogEntry, ...]) -> None:
        self.entries = entries

    @classmethod
    def load(cls, root: Path, *, tenant_id: str | None = None) -> "CapabilityCatalog":
        entries: list[CatalogEntry] = []
        for path in sorted(root.glob("*.json")):
            capability = Capability.model_validate_json(path.read_bytes())
            if capability.status != "approved":
                continue
            override_path = (
                root / "overrides" / tenant_id / f"{capability.name}.json"
                if tenant_id is not None
                else None
            )
            override = None
            if override_path is not None and override_path.exists():
                override = TenantOverride.model_validate_json(override_path.read_bytes())
                override.verify_parent(capability)
            entries.append(
                CatalogEntry(
                    artifact_path=path,
                    capability=capability,
                    override_path=override_path if override is not None else None,
                    tenant_override=override,
                    tool=tool_for(capability),
                )
            )
        return cls(tuple(entries))

    def by_tool_name(self, name: str) -> CatalogEntry:
        return next(item for item in self.entries if item.tool.name == name)
