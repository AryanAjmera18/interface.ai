"""Define constrained tenant replay overrides; forbid I/O and framework imports."""

from typing import Literal, Self

from pydantic import AwareDatetime, Field, model_validator

from cua.domain.capability import Capability, capability_content_digest
from cua.domain.common import Digest, DomainModel, NonEmpty
from cua.domain.locators import LocatorLadder
from cua.domain.predicates import Predicate
from cua.domain.provenance import HumanEdit


class StaleOverrideError(ValueError):
    """The base artifact changed after the tenant override was reviewed."""


class InvalidOverrideError(ValueError):
    """The patch attempts to change behavior outside locators and checkpoints."""


class StepPatch(DomainModel):
    """A merge-patch entry whose writable surface is intentionally narrow."""

    step_id: NonEmpty
    target: LocatorLadder | None = None
    checkpoint: Predicate | None = None

    @model_validator(mode="after")
    def changes_something(self) -> Self:
        if self.target is None and self.checkpoint is None:
            raise InvalidOverrideError(f"Step {self.step_id!r} has no override fields")
        return self


class CapabilityPatch(DomainModel):
    """Canonical JSON Merge Patch view; arrays avoid untyped, open-keyed maps."""

    steps: tuple[StepPatch, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_steps(self) -> Self:
        identifiers = [item.step_id for item in self.steps]
        if len(identifiers) != len(set(identifiers)):
            raise InvalidOverrideError("A tenant override may patch each step only once")
        return self


class TenantOverride(DomainModel):
    """Bind tenant-only locator/checkpoint edits to exact reviewed capability content.

    RFC 7396 merge patch replaces arrays wholesale, which would let an override silently change
    actions, risk, outcomes, inputs, or outputs. This document instead applies merge-patch
    semantics to a typed override view containing only step locators and checkpoints. The base
    capability remains byte-for-byte unchanged and its approval remains valid.
    """

    schema_version: Literal[1] = 1
    tenant_id: NonEmpty
    parent_capability_id: NonEmpty
    parent_content_digest: Digest
    patch: CapabilityPatch
    created_at: AwareDatetime
    edits: tuple[HumanEdit, ...] = Field(min_length=1)

    def verify_parent(self, capability: Capability) -> None:
        actual = capability_content_digest(capability)
        if (
            capability.capability_id != self.parent_capability_id
            or actual != self.parent_content_digest
        ):
            raise StaleOverrideError(
                "Tenant override is stale: the base artifact changed after review; "
                "regenerate and re-review the override."
            )
        known = {step.step_id for step in capability.steps}
        unknown = [item.step_id for item in self.patch.steps if item.step_id not in known]
        if unknown:
            raise InvalidOverrideError(f"Override references unknown step {unknown[0]!r}")

    def step_patch(self, step_id: str) -> StepPatch | None:
        return next((item for item in self.patch.steps if item.step_id == step_id), None)
