"""Normalize surface perception and canonical hashes; forbid I/O and other cua packages."""

import json
from typing import Literal, Self, cast

from pydantic import AwareDatetime, Field, JsonValue, field_serializer, model_validator

from cua.domain.common import ULID, Digest, DomainModel, EvidenceRef, NonEmpty, SurfaceKind, digest

AxState = Literal["disabled", "focused", "checked", "expanded", "required", "invalid"]


class Bounds(DomainModel):
    x: float
    y: float
    w: float = Field(ge=0)
    h: float = Field(ge=0)


class AxNode(DomainModel):
    role: NonEmpty
    name: str
    value: str | None = None
    description: str = ""
    states: frozenset[AxState] = frozenset()
    frame_path: tuple[str, ...] = ()
    node_path: tuple[int, ...] = ()
    bounds: Bounds | None = None
    children: tuple["AxNode", ...] = ()

    @field_serializer("states")
    def ordered_states(self, states: frozenset[AxState]) -> list[AxState]:
        return sorted(states)


class FrameInfo(DomainModel):
    """Backend-neutral scope path; desktop windows/panes can supply the same hierarchy."""

    frame_path: tuple[str, ...]
    title: str


class SurfaceFingerprint(DomainModel):
    app_id: NonEmpty
    app_version: NonEmpty
    tenant_id: NonEmpty
    config_hash: Digest
    ui_revision: NonEmpty
    observed_at: AwareDatetime


def ax_digest(root: AxNode) -> str:
    """Hash only the AX tree, retaining child order, paths, labels, values and semantic states.

    Exclude all bounds and focused state recursively. No AX IDs or timestamps exist in this
    shape. Observation IDs/times, URL/title, frame metadata, screenshot/DOM digests and the
    fingerprint are outside the hash. Thus this is state identity, NOT a complete evidence
    integrity hash; EvidenceRef.content_hash protects the full stored observation separately.
    """

    def normalized(node: AxNode) -> JsonValue:
        return {
            "role": node.role,
            "name": node.name,
            "value": node.value,
            "description": node.description,
            "states": [str(state) for state in sorted(node.states - {"focused"})],
            "frame_path": list(node.frame_path),
            "node_path": list(node.node_path),
            "children": [normalized(child) for child in node.children],
        }

    return digest(normalized(root))


class Observation(DomainModel):
    observation_id: ULID
    captured_at: AwareDatetime
    surface_kind: SurfaceKind
    url: str | None
    title: str
    ax_root: AxNode
    frames: tuple[FrameInfo, ...] = ()
    screenshot_ref: EvidenceRef | None = None
    dom_digest: Digest | None = None
    fingerprint: SurfaceFingerprint
    hash: Digest = Field(default="", validate_default=False)

    @model_validator(mode="after")
    def verify_hash(self) -> Self:
        expected = ax_digest(self.ax_root)
        if self.hash and self.hash != expected:
            raise ValueError("Observation hash does not match its normalized AX tree")
        object.__setattr__(self, "hash", expected)
        return self


def walk_ax(root: AxNode) -> tuple[AxNode, ...]:
    return (root, *(node for child in root.children for node in walk_ax(child)))


def observation_json(observation: Observation) -> str:
    from cua.domain.common import canonical_json

    return canonical_json(cast(JsonValue, json.loads(observation.model_dump_json())))
