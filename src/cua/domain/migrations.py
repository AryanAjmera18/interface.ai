"""Version and load in-memory artifact text; forbid filesystem I/O and other cua packages."""

import json
from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import cast

from pydantic import AwareDatetime, JsonValue

from cua.domain.capability import Capability
from cua.domain.common import ULID, Digest, DomainModel, canonical_json, digest
from cua.domain.ports import Clock, IdGenerator


class ArtifactDocument(DomainModel):
    """Typed boundary for raw pre-migration JSON; never pass loose dictionaries between modules."""

    payload: JsonValue


class MigrationRecord(DomainModel):
    migration_id: ULID
    from_version: int
    to_version: int
    rule_id: str
    at: AwareDatetime
    before_hash: Digest
    after_hash: Digest


class LoadedCapability(DomainModel):
    capability: Capability
    migrations: tuple[MigrationRecord, ...]


def v2_identity(document: ArtifactDocument) -> ArtifactDocument:
    """Current reads record validation outside the artifact, preserving approved bytes."""
    return document


def v1_to_v2(document: ArtifactDocument) -> ArtifactDocument:
    """Add explicit scope defaults without guessing semantics or silently deleting sensitive data.

    Stricter parameter rules are checked on load and may require a human edit. Approval is
    invalidated because it signed different bytes. An approved source retains its status but
    loses its approval, so load fails loudly with StaleApprovalError instead of silently granting
    authority or downgrading it. Callers must explicitly request a candidate and obtain new review.
    Historical node_path targets remain positional, never invented semantic scopes.
    """
    payload = cast(JsonValue, json.loads(canonical_json(document.payload)))
    if not isinstance(payload, dict):
        raise ValueError("v1 migration requires an object")

    def visit(value: JsonValue) -> None:
        if isinstance(value, dict):
            if {"role", "name_matcher", "node_path"} <= value.keys():
                value.update(within=None, nth=None)
            if {"settle_strategy", "timeout_ms", "retry"} <= value.keys():
                value.update(poll_interval_ms=50, stability_window_ms=100)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    payload["schema_version"] = 2
    provenance = payload.get("provenance")
    if isinstance(provenance, dict):
        provenance["approval"] = None
    return ArtifactDocument(payload=payload)


CURRENT_VERSION = 2
MIGRATIONS: Mapping[int, Callable[[ArtifactDocument], ArtifactDocument]] = MappingProxyType(
    {1: v1_to_v2, 2: v2_identity}
)


def load_capability(text: str, *, clock: Clock, ids: IdGenerator) -> LoadedCapability:
    document = ArtifactDocument(payload=cast(JsonValue, json.loads(text)))
    if not isinstance(document.payload, dict):
        raise ValueError("Capability must be a JSON object with schema_version")
    version = document.payload.get("schema_version")
    if type(version) is not int or version not in MIGRATIONS or version > CURRENT_VERSION:
        raise ValueError(
            f"Unsupported capability schema_version {version!r}; no registered migration"
        )
    records: list[MigrationRecord] = []
    while True:
        before = digest(document.payload)
        document = MIGRATIONS[version](document)
        next_version = version if version == CURRENT_VERSION else version + 1
        records.append(
            MigrationRecord(
                migration_id=ids.new(),
                from_version=version,
                to_version=next_version,
                rule_id=f"v{version}-identity"
                if version == CURRENT_VERSION
                else f"v{version}-to-v{next_version}",
                at=clock.now(),
                before_hash=before,
                after_hash=digest(document.payload),
            )
        )
        if version == CURRENT_VERSION:
            break
        version = next_version
    capability = Capability.model_validate_json(canonical_json(document.payload))
    return LoadedCapability(capability=capability, migrations=tuple(records))
