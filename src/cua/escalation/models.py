"""Define typed control-transfer state; forbid higher layers and target-app imports."""

from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, JsonValue, model_validator

from cua.domain.common import ULID, DomainModel, EvidenceRef
from cua.domain.ports import TreeChange

HandoffState = Literal["RUNNING", "PAUSED", "OPERATOR_CONTROL", "HANDBACK_PENDING", "ABORTED"]
LeaseHolder = Literal["automation", "operator", "none"]
InterventionReason = Literal[
    "dead_end",
    "request_human_help",
    "irreversible_confirmation",
    "replay_hard_failure",
]


class IllegalLeaseTransitionError(ValueError):
    """The requested control transfer is not an edge in the reviewed state machine."""


class SessionLease(DomainModel):
    """Answer who controls one live session independently from persisted graph state.

    A LangGraph checkpoint restores discovery data after a process interruption. This lease
    protects the live browser that an operator may currently own; restoring one never implies
    restoring the other.
    """

    state: HandoffState = "RUNNING"
    holder: LeaseHolder = "automation"
    lease_id: ULID
    acquired_at: AwareDatetime
    expires_at: AwareDatetime
    reason: str
    resume_token: str


class InterventionInput(DomainModel):
    name: str
    value: JsonValue


class InterventionRequest(DomainModel):
    request_id: ULID
    capability_or_goal: str
    step_id: str | None = None
    step_intent: str | None = None
    reason: InterventionReason
    detail: str
    inputs: tuple[InterventionInput, ...] = ()
    screenshot_ref: EvidenceRef | None = None
    ax_ref: EvidenceRef | None = None
    trace_id: str
    resume_token: str
    lease: SessionLease
    status: Literal["open", "operator_control", "handback_pending", "resolved", "aborted"] = "open"
    operator_actor: str | None = None
    operator_notes: str | None = None
    cdp_endpoint: str | None = None

    @model_validator(mode="after")
    def token_binds_request_to_lease(self) -> "InterventionRequest":
        if self.resume_token != self.lease.resume_token:
            raise ValueError("Intervention resume_token must match its session lease")
        return self


class AxChange(DomainModel):
    path: str
    before_hash: str | None = None
    after_hash: str | None = None
    change: Literal["added", "removed", "changed"]


class HandoffResult(DomainModel):
    request_id: ULID
    resumed: bool
    aborted: bool
    actor: str | None = None
    notes: str | None = None
    changes: tuple[TreeChange, ...] = ()
    at: datetime
