"""The compiler derives artifacts deterministically from the run journal, not model emission.

Artifacts nevertheless remain strict-representable so both providers and LLM-assisted editing
share one wire format. Typed entry lists resolve open-map keys (as in protobuf map encoding);
provider-specific shadow DTOs were rejected because they would diverge in semantics and review.
This module is pure: no I/O, frameworks, or imports from another cua package.
"""

import json
import re
from typing import Annotated, Literal, Self, cast

from pydantic import Field, JsonValue, field_validator, model_validator

from cua.domain.actions import ReadValue, SelectOption, TypeText
from cua.domain.common import (
    ULID,
    Digest,
    DomainModel,
    JsonType,
    LiteralValue,
    NonEmpty,
    ParamRef,
    SecretRef,
    Sensitivity,
    SurfaceKind,
    digest,
)
from cua.domain.observation import SurfaceFingerprint
from cua.domain.predicates import AxTarget, Predicate
from cua.domain.provenance import CapabilityProvenance, ModelRef, assert_step_attributable
from cua.domain.steps import Step


class RegexValidation(DomainModel):
    kind: Literal["regex"] = "regex"
    pattern: str

    @model_validator(mode="after")
    def valid_regex(self) -> Self:
        try:
            re.compile(self.pattern)
        except re.error as exc:
            raise ValueError("Invalid parameter regex") from exc
        return self


class RangeValidation(DomainModel):
    kind: Literal["range"] = "range"
    minimum: float | None
    maximum: float | None

    @model_validator(mode="after")
    def ordered_range(self) -> Self:
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("minimum must not exceed maximum")
        return self


class EnumValidation(DomainModel):
    kind: Literal["enum"] = "enum"
    values: tuple[LiteralValue, ...] = Field(min_length=1)


Validation = Annotated[
    RegexValidation | RangeValidation | EnumValidation, Field(discriminator="kind")
]


class ParamSpec(DomainModel):
    """UI form parameters are scalars; nested input structure calls for capability decomposition.

    Parameters are non-null scalar inputs; output nullability is a separate concern.
    PII and secret defaults/examples are prohibited instead of trusting a synthetic checkbox.
    Required inputs cannot also supply defaults: callers must explicitly provide them.
    """

    name: NonEmpty
    json_type: Literal["string", "number", "integer", "boolean"]
    required: bool
    default: LiteralValue | None
    example: LiteralValue | None
    sensitivity: Sensitivity
    validation: Validation | None
    description: NonEmpty

    @model_validator(mode="after")
    def literal_types(self) -> Self:
        if self.sensitivity in {"secret", "pii"} and (
            self.default is not None or self.example is not None
        ):
            raise ValueError(f"Sensitive parameter {self.name} cannot embed defaults or examples")
        if self.required and self.default is not None:
            raise ValueError(f"Required parameter {self.name} cannot embed a default")
        if isinstance(self.validation, RegexValidation) and self.json_type != "string":
            raise ValueError("Regex validation requires a string parameter")
        if isinstance(self.validation, RangeValidation) and self.json_type not in {
            "number",
            "integer",
        }:
            raise ValueError("Range validation requires a numeric parameter")
        enum_values = self.validation.values if isinstance(self.validation, EnumValidation) else ()
        for literal in (self.default, self.example, *enum_values):
            if literal is None:
                continue
            expected = "number" if self.json_type == "integer" else self.json_type
            if literal.kind != expected or (
                self.json_type == "integer"
                and literal.kind == "number"
                and not literal.value.is_integer()
            ):
                raise ValueError(
                    f"Parameter {self.name}: literal kind must agree with {self.json_type}"
                )
            rule = self.validation
            value = literal.value
            if isinstance(rule, RegexValidation) and (
                not isinstance(value, str) or re.search(rule.pattern, value) is None
            ):
                raise ValueError(f"Parameter {self.name}: literal violates regex")
            if isinstance(rule, RangeValidation) and (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or (rule.minimum is not None and float(value) < rule.minimum)
                or (rule.maximum is not None and float(value) > rule.maximum)
            ):
                raise ValueError(f"Parameter {self.name}: literal violates range")
            if isinstance(rule, EnumValidation) and literal not in rule.values:
                raise ValueError(f"Parameter {self.name}: literal violates enum")
        return self


class ExtractorSpec(DomainModel):
    """Extraction is an AX attribute plus optional regex, never executable recorded code."""

    target: AxTarget
    attribute: Literal["value", "name", "description"]
    regex: str | None = None

    @field_validator("regex")
    @classmethod
    def valid_regex(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                re.compile(value)
            except re.error as exc:
                raise ValueError("Invalid extraction regex") from exc
        return value


class OutputSpec(DomainModel):
    name: NonEmpty
    json_type: JsonType
    produced_by_step_id: NonEmpty
    extractor: ExtractorSpec
    nullable: bool
    sensitivity: Sensitivity
    description: NonEmpty


class RecoveryAction(DomainModel):
    """Bounded named recovery only; arbitrary scripts and unbounded retry loops were rejected."""

    kind: Literal["retry", "refresh", "reauthenticate", "dismiss"]
    max_attempts: int = Field(ge=1, le=10)


class OutcomeSpec(DomainModel):
    code: NonEmpty
    detect: Predicate
    classification: Literal["business", "recoverable", "hard"]
    caller_message: NonEmpty
    recovery: RecoveryAction | None

    @model_validator(mode="after")
    def recovery_contract(self) -> Self:
        if self.classification == "recoverable" and self.recovery is None:
            raise ValueError(
                f"Outcome {self.code}: recoverable requires an explicit recovery action"
            )
        if self.classification != "recoverable" and self.recovery is not None:
            raise ValueError(
                f"Outcome {self.code}: {self.classification} forbids recovery; "
                "business is an answer, not a crash"
            )
        return self


class CapabilityTarget(DomainModel):
    surface_kind: SurfaceKind
    app_id: NonEmpty
    entry_point: NonEmpty
    tenant_id: str | None
    allowlist_ref: NonEmpty
    recorded_fingerprint: SurfaceFingerprint


class UndeclaredOutcomeError(ValueError):
    """An edge names no declared outcome; fix the code or declare its detection semantics."""


class StaleApprovalError(ValueError):
    """The reviewed content no longer matches; the artifact must be re-reviewed."""


class Capability(DomainModel):
    schema_version: Literal[2] = 2
    capability_id: ULID
    name: NonEmpty
    description: NonEmpty
    version: str = Field(
        pattern=(
            r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
            r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
        )
    )
    status: Literal["draft", "candidate", "approved"]
    target: CapabilityTarget
    inputs: tuple[ParamSpec, ...]
    outputs: tuple[OutputSpec, ...]
    steps: tuple[Step, ...] = Field(min_length=1)
    outcomes: tuple[OutcomeSpec, ...]
    provenance: CapabilityProvenance

    @field_validator("version")
    @classmethod
    def valid_semver_prerelease(cls, value: str) -> str:
        """SemVer forbids leading zeroes in numeric prerelease IDs, unlike build metadata."""
        core = value.split("+", 1)[0]
        if "-" in core:
            for identifier in core.split("-", 1)[1].split("."):
                if identifier.isdigit() and len(identifier) > 1 and identifier.startswith("0"):
                    raise ValueError(
                        "SemVer numeric prerelease identifiers cannot have leading zeroes"
                    )
        return value

    @model_validator(mode="after")
    def coherent_artifact(self) -> Self:
        self.assert_attributable()
        for label, names in (
            ("steps", [s.step_id for s in self.steps]),
            ("inputs", [p.name for p in self.inputs]),
            ("outputs", [o.name for o in self.outputs]),
            ("outcomes", [o.code for o in self.outcomes]),
        ):
            if len(names) != len(set(names)):
                raise ValueError(f"Duplicate {label}; names must be unique")
        if [step.ordinal for step in self.steps] != list(range(1, len(self.steps) + 1)):
            raise ValueError("Step ordinals must be contiguous, ordered, and start at 1")
        declared = {outcome.code: outcome for outcome in self.outcomes}
        inputs = {param.name: param for param in self.inputs}
        for step in self.steps:
            for entry in step.on_outcome:
                if entry.code not in declared:
                    raise UndeclaredOutcomeError(
                        f"Step {step.step_id}: outcome {entry.code!r} is undeclared; "
                        "add an OutcomeSpec or correct the code"
                    )
                expected = {"business": "return", "recoverable": "recover", "hard": "fail"}[
                    declared[entry.code].classification
                ]
                if entry.handling.kind != expected:
                    raise ValueError(
                        f"Step {step.step_id}: {entry.code} requires handling {expected}"
                    )
            if isinstance(step.action, (TypeText, SelectOption)):
                ref = step.action.value_ref
                if isinstance(ref, (ParamRef, SecretRef)):
                    if ref.name not in inputs:
                        raise ValueError(f"Step {step.step_id}: undeclared input {ref.name!r}")
                    if (inputs[ref.name].sensitivity == "secret") != isinstance(ref, SecretRef):
                        raise ValueError(
                            f"Step {step.step_id}: secret inputs require secret_ref; "
                            "other inputs require param_ref"
                        )
        for output in self.outputs:
            producer = next(
                (s for s in self.steps if s.step_id == output.produced_by_step_id), None
            )
            if (
                producer is None
                or not isinstance(producer.action, ReadValue)
                or producer.action.output_name != output.name
            ):
                raise ValueError(
                    f"Output {output.name}: produced_by_step_id must name its ReadValue step"
                )
        for step in self.steps:
            if isinstance(step.action, ReadValue) and not any(
                o.name == step.action.output_name and o.produced_by_step_id == step.step_id
                for o in self.outputs
            ):
                raise ValueError(f"Step {step.step_id}: ReadValue needs a matching OutputSpec")
        self.assert_current_approval()
        fp = self.target.recorded_fingerprint
        if fp.app_id != self.target.app_id or (
            self.target.tenant_id is not None and fp.tenant_id != self.target.tenant_id
        ):
            raise ValueError("Capability target disagrees with its recorded fingerprint")
        return self

    def assert_current_approval(self) -> None:
        if self.status == "approved" and (
            self.provenance.approval is None
            or self.provenance.approval.reviewed_digest != capability_content_digest(self)
        ):
            raise StaleApprovalError(
                "Artifact changed after approval and must be re-reviewed; "
                "approved artifacts require a named ApprovalRecord bound to this content"
            )

    def assert_attributable(self) -> None:
        for step in self.steps:
            assert_step_attributable(step.step_id, step.provenance)
            if step.provenance.discovery_run_id != self.provenance.derived_from_run_id:
                raise ValueError(
                    f"Step {step.step_id}: discovery run differs from capability provenance"
                )
            if (
                isinstance(step.provenance.decided_by, ModelRef)
                and step.provenance.decided_by not in self.provenance.models_used
            ):
                raise ValueError(f"Step {step.step_id}: deciding model is absent from models_used")


def capability_content_digest(capability: Capability) -> Digest:
    """SHA-256 over canonical JSON excluding EXACTLY /status and /provenance/approval.

    Approval describes content, so including either field would make the review self-referential.
    All other fields, including provenance, human edits and version, participate. Approval actor,
    time and reason still require journal verification: this digest is not an identity signature.
    """
    content = capability.model_dump_json(exclude={"status": True, "provenance": {"approval"}})
    return digest(cast(JsonValue, json.loads(content)))
