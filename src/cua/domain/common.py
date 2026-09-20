"""Define pure shared value types; forbid I/O, frameworks, and other cua packages."""

import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class DomainModel(BaseModel):
    """Reject unknown fields rather than silently losing artifact intent during upgrades."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, validate_default=True, allow_inf_nan=False
    )


ULID = Annotated[str, Field(pattern=r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
NonEmpty = Annotated[str, Field(min_length=1)]
SurfaceKind = Literal["web", "desktop"]
Sensitivity = Literal["public", "internal", "pii", "secret"]
JsonType = Literal["string", "number", "integer", "boolean", "null", "array", "object"]


def canonical_json(value: JsonValue) -> str:
    """Sort object keys, retain array order, emit UTF-8 JSON without NaN or incidental spacing."""
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


def digest(value: JsonValue) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class EvidenceRef(DomainModel):
    """An opaque store key plus content digest, not an arbitrary file path to execute or read."""

    evidence_id: ULID
    content_hash: Digest
    media_type: NonEmpty


class StringValue(DomainModel):
    kind: Literal["string"] = "string"
    value: str = Field(strict=True)


class NumberValue(DomainModel):
    kind: Literal["number"] = "number"
    value: float = Field(strict=True)


class BooleanValue(DomainModel):
    kind: Literal["boolean"] = "boolean"
    value: bool = Field(strict=True)


class NullValue(DomainModel):
    kind: Literal["null"] = "null"
    value: None = None


LiteralValue = Annotated[
    StringValue | NumberValue | BooleanValue | NullValue, Field(discriminator="kind")
]


class LiteralRef(DomainModel):
    """Literal data is public/synthetic; secret material must use SecretRef instead."""

    kind: Literal["literal"] = "literal"
    value: LiteralValue


class ParamRef(DomainModel):
    kind: Literal["param_ref"] = "param_ref"
    name: NonEmpty


class SecretRef(DomainModel):
    """Name a secret resolver binding; never embed a secret value in an artifact."""

    kind: Literal["secret_ref"] = "secret_ref"
    name: NonEmpty


ValueRef = Annotated[LiteralRef | ParamRef | SecretRef, Field(discriminator="kind")]
