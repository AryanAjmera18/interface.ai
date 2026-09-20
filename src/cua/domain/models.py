"""Define provider-neutral model decisions and profiles; forbid I/O and other cua packages."""

from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, model_validator

from cua.domain.actions import Action
from cua.domain.common import ULID, Digest, DomainModel, NonEmpty
from cua.domain.observation import Observation
from cua.domain.predicates import AxTarget
from cua.domain.provenance import ModelRef


class Usage(DomainModel):
    """Adapters normalize provider counters; unknown monetary cost remains explicitly null."""

    input_tokens: int = Field(ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)


class ModelRole(StrEnum):
    DISCOVERY_PLANNER = "discovery_planner"
    EXTRACTOR = "extractor"
    CATALOG_AGENT = "catalog_agent"


class ModelProfile(DomainModel):
    role: ModelRole
    model: ModelRef
    max_output_tokens: int = Field(default=2048, gt=0)


class ModelRegistry(DomainModel):
    profiles: tuple[ModelProfile, ...]

    @model_validator(mode="after")
    def all_roles_once(self) -> Self:
        if sorted(item.role for item in self.profiles) != sorted(ModelRole):
            raise ValueError("Profiles must bind every ModelRole exactly once")
        return self

    def get(self, role: ModelRole) -> ModelProfile:
        return next(profile for profile in self.profiles if profile.role == role)


def fake_registry() -> ModelRegistry:
    return ModelRegistry(
        profiles=tuple(
            ModelProfile(
                role=role,
                model=ModelRef(
                    provider="fake",
                    model_id="deterministic-v1",
                    api_flavor="offline",
                    structured_output_mode="json_schema",
                ),
            )
            for role in ModelRole
        )
    )


class DecisionRequest(DomainModel):
    decision_id: ULID
    role: ModelRole
    instruction: NonEmpty
    observation: Observation
    prompt_template_id: NonEmpty
    tool_schema_hash: Digest


class DecisionResult(DomainModel):
    """A refusal/error has no action; a fabricated no-op would hide failed control transfer."""

    decision_id: ULID
    intent: NonEmpty
    action: Action | None
    target: AxTarget | None
    model: ModelRef
    prompt_template_id: NonEmpty
    prompt_hash: Digest
    rationale_digest: Digest | None
    tool_call_id: NonEmpty
    usage: Usage
    latency_ms: int = Field(ge=0)
    finish_reason: Literal["completed", "length", "refusal", "error"]

    @model_validator(mode="after")
    def action_requires_completion(self) -> Self:
        if (self.finish_reason == "completed") != (self.action is not None):
            raise ValueError(
                "Only completed decisions carry an action; other finish reasons require null"
            )
        return self
