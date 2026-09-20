"""Hand-author the synthetic savings demo; forbid live discovery and external services."""

from cua.domain.actions import Click, Navigate, ReadValue
from cua.domain.capability import (
    Capability,
    ExtractorSpec,
    OutcomeSpec,
    OutputSpec,
    RecoveryAction,
    RegexValidation,
    capability_content_digest,
)
from cua.domain.common import digest
from cua.domain.locators import (
    CoordinatesCandidate,
    CoordinatesValue,
    LocatorLadder,
    RoleNameCandidate,
    RoleNameValue,
    ScopedRoleNameCandidate,
    ScopedRoleNameValue,
    TextValue,
    VisibleTextCandidate,
)
from cua.domain.names import NameMatcher
from cua.domain.predicates import AxNodeExists, AxTarget
from cua.domain.provenance import ApprovalRecord, HumanEdit, HumanRef
from cua.domain.steps import OutcomeHandlingEntry, Step, StepOutcomeHandling
from tests.unit.domain.samples import NOW, capability


def demo_capability(*, approved: bool = False) -> Capability:
    """This fixture claims named synthetic authorship, not a successful discovered run."""
    seed = capability()
    proto = seed.steps[0]
    metadata = proto.target.candidates[0].model_dump(exclude={"strategy", "value"})
    metadata["source"] = "ax_tree"
    metadata["rationale"] = "Synthetic hand-authored fixture; verify against live evidence later."
    row = AxTarget(role="row", name_matcher=NameMatcher(mode="regex", value=r"\bSavings\b"))
    balance = AxTarget(
        role="cell", name_matcher=NameMatcher(mode="regex", value=r"^\$[0-9,.]+$"), within=row
    )
    scoped = LocatorLadder(
        candidates=(
            ScopedRoleNameCandidate(
                value=ScopedRoleNameValue(
                    role="cell", name_matcher=balance.name_matcher, ancestor=row
                ),
                **metadata,
            ),
        )
    )

    def target(role: str, name: str, *, fallbacks: bool = False) -> LocatorLadder:
        primary = RoleNameCandidate(
            value=RoleNameValue(role=role, name_matcher=NameMatcher(value=name)), **metadata
        )
        if not fallbacks:
            return LocatorLadder(candidates=(primary,))
        return LocatorLadder(
            candidates=(
                primary,
                VisibleTextCandidate(value=TextValue(matcher=NameMatcher(value=name)), **metadata),
                CoordinatesCandidate(value=CoordinatesValue(x=160, y=80), **metadata),
            )
        )

    expired = OutcomeSpec(
        code="session_expired",
        detect=AxNodeExists(role="status", name_matcher=NameMatcher(value="Session expired")),
        classification="recoverable",
        caller_message="Session expired; reauthentication is required.",
        recovery=RecoveryAction(kind="reauthenticate", max_attempts=1),
    )
    error = OutcomeSpec(
        code="app_error",
        detect=AxNodeExists(role="heading", name_matcher=NameMatcher(value="System error")),
        classification="hard",
        caller_message="The banking console could not complete the request.",
        recovery=None,
    )
    outcomes = (seed.outcomes[0], expired, error)
    handlers = tuple(
        OutcomeHandlingEntry(code=outcome.code, handling=StepOutcomeHandling(kind=kind))
        for outcome, kind in zip(outcomes, ("return", "recover", "fail"), strict=True)
    )
    specs = (
        ("navigate", Navigate(url="http://127.0.0.1:8099/t/alpha/shell"), None),
        ("enter-member", proto.action, proto.target),
        ("submit-search", Click(), target("button", "Search members", fallbacks=True)),
        ("member-detail", Click(), target("link", "View member 10001")),
        ("read-savings", ReadValue(output_name="savings_balance", attribute="name"), scoped),
    )
    steps = []
    for ordinal, (step_id, action, ladder) in enumerate(specs, 1):
        provenance = proto.provenance.model_copy(
            update={
                "decided_by": HumanRef(actor="synthetic-fixture-author"),
                "decision_id": None,
                "edits": (
                    HumanEdit(
                        actor="synthetic-fixture-author",
                        at=NOW,
                        field_path=f"/steps/{ordinal - 1}",
                        before_hash=digest(None),
                        after_hash=digest(step_id),
                        reason="Hand-authored synthetic test fixture, not verified run evidence.",
                    ),
                ),
            }
        )
        steps.append(
            Step(
                step_id=step_id,
                ordinal=ordinal,
                intent=f"Synthetic demo: {step_id.replace('-', ' ')}.",
                action=action,
                target=ladder,
                checkpoint=AxNodeExists(
                    role="heading", name_matcher=NameMatcher(value="Member detail")
                )
                if step_id == "member-detail"
                else None,
                risk="reversible" if step_id == "enter-member" else "safe",
                timing=proto.timing,
                on_outcome=handlers,
                provenance=provenance,
            )
        )
    result = Capability.model_validate(
        {
            **seed.model_dump(),
            "name": "look_up_member_savings_balance",
            "description": "Hand-authored synthetic demo; assumes an authenticated session.",
            "inputs": [
                seed.inputs[0].model_copy(
                    update={"validation": RegexValidation(pattern=r"^\d{5}$")}
                )
            ],
            "steps": steps,
            "outputs": [
                OutputSpec(
                    name="savings_balance",
                    json_type="string",
                    produced_by_step_id="read-savings",
                    extractor=ExtractorSpec(target=balance, attribute="name"),
                    nullable=False,
                    sensitivity="internal",
                    description="Formatted synthetic Savings balance.",
                )
            ],
            "outcomes": outcomes,
        }
    )
    if approved:
        result = Capability.model_validate(
            {
                **result.model_dump(),
                "status": "approved",
                "provenance": result.provenance.model_copy(
                    update={
                        "approval": ApprovalRecord(
                            actor="synthetic-reviewer",
                            at=NOW,
                            reviewed_digest=capability_content_digest(result),
                            reason="Synthetic approval fixture; not deployment authority.",
                        )
                    }
                ),
            }
        )
    return result
