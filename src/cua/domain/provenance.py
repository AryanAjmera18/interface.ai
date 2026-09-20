"""Attribute recorded decisions and named edits; forbid I/O and other cua packages."""

from typing import Literal

from pydantic import AwareDatetime, Field

from cua.domain.common import ULID, Digest, DomainModel, NonEmpty


class ModelRef(DomainModel):
    """Provider identity is data, not an SDK class; discovery and invocation may differ."""

    kind: Literal["model"] = "model"
    provider: Literal["anthropic", "openai", "fake"]
    model_id: NonEmpty
    api_flavor: NonEmpty
    structured_output_mode: NonEmpty
    reasoning_effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None


class HumanRef(DomainModel):
    kind: Literal["human"] = "human"
    actor: str = Field(pattern=r"\S", min_length=1)


class HumanEdit(DomainModel):
    """Hashes retain before/after identity without duplicating sensitive field values."""

    actor: str = Field(pattern=r"\S", min_length=1)
    at: AwareDatetime
    field_path: str = Field(pattern=r"^(?:/(?:[^~/]|~[01])*)*$")
    before_hash: Digest
    after_hash: Digest
    reason: NonEmpty


class StepProvenance(DomainModel):
    discovery_run_id: ULID
    observation_id: ULID
    observation_hash: Digest | None
    decided_by: ModelRef | Literal["compiler"] | HumanRef
    decision_id: ULID | None = None
    tool_call_id: str | None = None
    prompt_template_id: str | None = None
    prompt_hash: Digest | None = None
    rationale_digest: Digest | None = None
    compiler_rule_id: str | None = None
    created_at: AwareDatetime
    edits: tuple[HumanEdit, ...] = ()


class ApprovalRecord(DomainModel):
    """Record the reviewed document digest; journal verification must bind the approver identity.

    reviewed_digest binds canonical content excluding status and provenance.approval. The
    Capability validator checks the binding; the journal must authenticate actor/time/reason.
    """

    actor: str = Field(pattern=r"\S", min_length=1)
    at: AwareDatetime
    reviewed_digest: Digest
    reason: NonEmpty


class LineageRef(DomainModel):
    capability_id: ULID
    version: NonEmpty
    content_hash: Digest
    relationship: Literal["derived", "forked", "migrated"]


class CapabilityProvenance(DomainModel):
    """Compiler/recorder versions attribute deterministic assembly, not just model-authored steps.

    This is an evidence index, not a signature: referenced decisions, hashes and human identities
    still require verification against the journal. Schema validation cannot prove historical truth.
    """

    derived_from_run_id: ULID
    compiler_version: NonEmpty
    recorder_version: NonEmpty
    models_used: tuple[ModelRef, ...]
    tool_schema_hash: Digest
    recorded_at: AwareDatetime
    parent_capability_id: ULID | None = None
    lineage: tuple[LineageRef, ...] = ()
    approval: ApprovalRecord | None = None


class UnattributableStepError(ValueError):
    """Raised by assert_attributable; wrapped with its message during model validation."""


def assert_step_attributable(step_id: str, provenance: StepProvenance) -> None:
    """Require a resolvable decision ID, named compiler rule, or matching named human edit.

    A provider name alone was rejected: it identifies who could have decided, not which decision
    did. Compiler attribution similarly needs a versioned rule; 'compiler' alone proves nothing.
    """
    actor = provenance.decided_by
    problem: str | None = None
    if isinstance(actor, ModelRef):
        if (
            not provenance.observation_hash
            or not provenance.decision_id
            or not provenance.prompt_hash
        ):
            problem = "model decisions need observation_hash, decision_id and prompt_hash"
    elif actor == "compiler":
        if not provenance.compiler_rule_id or not provenance.compiler_rule_id.strip():
            problem = "compiler attribution needs a named, versioned compiler_rule_id"
    elif not any(edit.actor == actor.actor for edit in provenance.edits):
        problem = f"human {actor.actor!r} needs a matching named HumanEdit"
    if problem:
        raise UnattributableStepError(
            f"Step {step_id!r} is unattributable: {problem}; "
            "attach journal evidence before shipping."
        )
