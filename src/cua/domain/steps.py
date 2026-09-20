"""Define reviewable replay steps; forbid I/O, frameworks, and other cua packages."""

from collections.abc import Mapping
from functools import cached_property
from types import MappingProxyType
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from cua.domain.actions import Action, Click, Dismiss, ReadValue, SelectOption, TypeText
from cua.domain.common import DomainModel, NonEmpty
from cua.domain.locators import LocatorLadder
from cua.domain.predicates import Predicate
from cua.domain.provenance import StepProvenance


class RetryPolicy(DomainModel):
    max_attempts: int = Field(default=1, ge=1, le=10)
    delay_ms: int = Field(default=0, ge=0)


class StepTiming(DomainModel):
    settle_strategy: Literal["none", "ax_stable", "navigation"]
    timeout_ms: int = Field(gt=0)
    poll_interval_ms: int = Field(default=50, gt=0)
    stability_window_ms: int = Field(default=100, ge=0)
    retry: RetryPolicy = Field(default_factory=RetryPolicy)


class StepOutcomeHandling(DomainModel):
    kind: Literal["return", "recover", "fail"]


class OutcomeHandlingEntry(DomainModel):
    code: NonEmpty
    handling: StepOutcomeHandling


class Step(DomainModel):
    """Outcome codes use entries on the wire, never arbitrary JSON object keys.

    The cached mapping is read-only and never serialized. Validation sorts the immutable tuple;
    in-memory order and wire order agree without a serializer or a mutable-list wrapper.
    """

    step_id: NonEmpty
    ordinal: int = Field(ge=1)
    intent: NonEmpty
    action: Action
    target: LocatorLadder | None
    preconditions: tuple[Predicate, ...] = ()
    checkpoint: Predicate | None
    risk: Literal["safe", "reversible", "irreversible"]
    timing: StepTiming
    on_outcome: tuple[OutcomeHandlingEntry, ...] = ()
    provenance: StepProvenance

    @cached_property
    def outcome_map(self) -> Mapping[str, StepOutcomeHandling]:
        return MappingProxyType({entry.code: entry.handling for entry in self.on_outcome})

    @field_validator("on_outcome")
    @classmethod
    def sorted_outcomes(
        cls, entries: tuple[OutcomeHandlingEntry, ...]
    ) -> tuple[OutcomeHandlingEntry, ...]:
        return tuple(sorted(entries, key=lambda entry: entry.code))

    @model_validator(mode="after")
    def valid_step(self) -> Self:
        codes = [entry.code for entry in self.on_outcome]
        if len(codes) != len(set(codes)):
            raise ValueError(f"Step {self.step_id}: on_outcome codes must be unique")
        if (
            isinstance(self.action, (Click, TypeText, SelectOption, ReadValue, Dismiss))
            and not self.target
        ):
            raise ValueError(
                f"Step {self.step_id}: {self.action.kind} requires a target locator ladder"
            )
        if self.risk == "irreversible" and (
            self.checkpoint is None or self.timing.retry.max_attempts != 1
        ):
            raise ValueError(
                "Irreversible steps need a checkpoint and exactly one attempt; recovery is explicit"
            )
        return self
