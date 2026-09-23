"""Define caller results and journal events; forbid I/O, frameworks, and other cua packages.

Results and journal payloads are ordinary provider tool-result JSON, explicitly exempt from
strict tool-input schemas. Open output maps remain appropriate here; forcing entry lists on
callers would solve a constraint neither provider imposes on tool results.
"""

from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, JsonValue

from cua.domain.common import ULID, Digest, DomainModel, EvidenceRef, NonEmpty


class Success(DomainModel):
    kind: Literal["success"] = "success"
    outputs: dict[str, JsonValue]
    evidence_ref: EvidenceRef
    steps_executed: int = Field(ge=0)
    duration_ms: int = Field(ge=0)


class BusinessOutcome(DomainModel):
    kind: Literal["business_outcome"] = "business_outcome"
    code: NonEmpty
    caller_message: NonEmpty
    detail: str
    partial_outputs: dict[str, JsonValue]
    evidence_ref: EvidenceRef


class HardFailure(DomainModel):
    kind: Literal["hard_failure"] = "hard_failure"
    step_id: NonEmpty
    step_intent: NonEmpty
    expected: NonEmpty
    observed: NonEmpty
    failure_kind: Literal[
        "drift",
        "invalid_input",
        "precondition",
        "checkpoint",
        "locator",
        "timeout",
        "permission",
        "session",
        "server",
        "policy",
        "extraction",
        "unknown",
    ]
    evidence_ref: EvidenceRef
    trace_id: NonEmpty
    journal_head_hash: Digest


ReplayResult = Annotated[Success | BusinessOutcome | HardFailure, Field(discriminator="kind")]


class Recovered(DomainModel):
    """Recovery is observable history, not a terminal outcome the caller branches on.

    A recovered run still ends in success, a business answer, or hard failure; adding Recovered
    to ReplayResult would leave callers unable to know whether execution actually completed.
    """

    kind: Literal["recovered"] = "recovered"
    event_id: ULID
    run_id: ULID
    at: AwareDatetime
    step_id: NonEmpty
    outcome_code: NonEmpty
    recovery_kind: Literal["retry", "refresh", "reauthenticate", "dismiss"]
    attempt: int = Field(ge=1)
    evidence_ref: EvidenceRef


class JournalEntry(DomainModel):
    event_id: ULID
    run_id: ULID
    at: AwareDatetime
    kind: NonEmpty
    payload: JsonValue
    previous_hash: Digest | None
    hash: Digest
