"""Define checkpointable discovery state; forbid I/O and provider frameworks."""

from typing import Literal

from pydantic import JsonValue

from cua.domain.actions import Action
from cua.domain.common import ULID, DomainModel, Sensitivity
from cua.domain.locators import LocatorLadder
from cua.domain.models import DecisionResult
from cua.domain.observation import Observation
from cua.policy.models import Budget, PolicyDecision, TargetSemantics


class InputBinding(DomainModel):
    name: str
    value: JsonValue
    sensitivity: Sensitivity


class OutputBinding(DomainModel):
    name: str
    value: JsonValue


class TurnRecord(DomainModel):
    observation_hash: str
    intent: str
    action: Action
    target: TargetSemantics | None
    locator_ladder: LocatorLadder | None
    policy: PolicyDecision
    after_hash: str | None = None


class EscalationRequest(DomainModel):
    reason: str
    terminal_edge: Literal[
        "budget_exhausted", "dead_end", "policy_denied", "human_help_requested", "surface_error"
    ]


class DiscoveryState(DomainModel):
    goal: str
    inputs: tuple[InputBinding, ...]
    run_id: ULID
    budget: Budget = Budget()
    last_observation: Observation | None = None
    history: tuple[TurnRecord, ...] = ()
    pending_decision: DecisionResult | None = None
    pending_action: Action | None = None
    pending_target: TargetSemantics | None = None
    pending_ladder: LocatorLadder | None = None
    pending_policy: PolicyDecision | None = None
    status: Literal[
        "running",
        "goal_reached",
        "budget_exhausted",
        "dead_end",
        "policy_denied",
        "human_help_requested",
        "surface_error",
    ] = "running"
    escalation: EscalationRequest | None = None
    outputs: tuple[OutputBinding, ...] = ()
    consecutive_no_change: int = 0
    repeated_action_count: int = 0
