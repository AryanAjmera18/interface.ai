"""Build synthetic examples with injected time and IDs; forbid external-service imports."""

import hashlib
from datetime import UTC, datetime

from cua.domain.actions import ActionInput, TypeText
from cua.domain.capability import Capability, CapabilityTarget, OutcomeSpec, ParamSpec
from cua.domain.common import EvidenceRef, ParamRef, StringValue, digest
from cua.domain.locators import LocatorLadder, RoleNameCandidate, RoleNameValue
from cua.domain.names import NameMatcher
from cua.domain.observation import (
    AxNode,
    Bounds,
    FrameInfo,
    Observation,
    SurfaceFingerprint,
    observation_json,
)
from cua.domain.ports import Clock, IdGenerator
from cua.domain.predicates import AxNodeExists, AxTarget, FieldValueEquals
from cua.domain.provenance import CapabilityProvenance, ModelRef, StepProvenance
from cua.domain.schemas import strict_schema
from cua.domain.steps import OutcomeHandlingEntry, Step, StepOutcomeHandling, StepTiming

NOW = datetime(2026, 1, 15, tzinfo=UTC)
IDENTIFIER = "00000000000000000000000001"
HASH = "a" * 64


class FixedClock:
    def now(self) -> datetime:
        return NOW


class SequenceIds:
    def __init__(self) -> None:
        self.count = 0

    def new(self) -> str:
        self.count += 1
        return str(self.count).rjust(26, "0")


def observation(tenant: str = "alpha") -> Observation:
    """Normalized fixture transcription of Stage 1's search screen, not fabricated live evidence."""
    frame = ("Content", "Member workspace")
    label = "Member ID" if tenant == "alpha" else "Account Holder Number"
    root = AxNode(
        role="document",
        name="Member search",
        frame_path=frame,
        children=(
            AxNode(role="heading", name="Member search", frame_path=frame, node_path=(0,)),
            AxNode(role="text", name=label, frame_path=frame, node_path=(1,)),
            AxNode(
                role="textbox",
                name="Member ID",
                value="10001",
                frame_path=frame,
                node_path=(2,),
                bounds=Bounds(x=100, y=40, w=160, h=24),
            ),
            AxNode(role="button", name="Search members", frame_path=frame, node_path=(3,)),
        ),
    )
    return Observation(
        observation_id=IDENTIFIER,
        captured_at=NOW,
        surface_kind="web",
        url=f"http://127.0.0.1:8099/t/{tenant}/ui/search",
        title="Member search",
        ax_root=root,
        frames=(FrameInfo(frame_path=frame, title="Member workspace"),),
        fingerprint=SurfaceFingerprint(
            app_id="cua-synthetic-bank",
            app_version="0.1.0",
            tenant_id=tenant,
            config_hash=digest(label),
            ui_revision="legacy-frames-1",
            observed_at=NOW,
        ),
    )


def ladder() -> LocatorLadder:
    return LocatorLadder(
        candidates=(
            RoleNameCandidate(
                value=RoleNameValue(role="textbox", name_matcher=NameMatcher(value="Member ID")),
                frame_path=("Content", "Member workspace"),
                confidence=1,
                source="ax_tree",
                observed_at=NOW,
                evidence_ref=EvidenceRef(
                    evidence_id=IDENTIFIER,
                    content_hash=hashlib.sha256(
                        (observation_json(observation()) + "\n").encode()
                    ).hexdigest(),
                    media_type="application/json",
                ),
                uniqueness_at_record=1,
                rationale="Unique named textbox in the recorded member workspace.",
            ),
        )
    )


def capability(clock: Clock | None = None, ids: IdGenerator | None = None) -> Capability:
    clock = clock or FixedClock()
    ids = ids or SequenceIds()
    run_id, capability_id, decision_id = ids.new(), ids.new(), ids.new()
    obs = observation()
    model = ModelRef(
        provider="fake",
        model_id="deterministic-v1",
        api_flavor="offline",
        structured_output_mode="json_schema",
    )
    target = AxTarget(
        role="textbox",
        name_matcher=NameMatcher(value="Member ID"),
        frame_path=("Content", "Member workspace"),
    )
    outcome = OutcomeSpec(
        code="member_not_found",
        detect=AxNodeExists(
            role="status",
            name_matcher=NameMatcher(
                value="No matching member was found. This is a business outcome."
            ),
        ),
        classification="business",
        caller_message="No matching member exists.",
        recovery=None,
    )
    step = Step(
        step_id="enter-member",
        ordinal=1,
        intent="Enter the caller's synthetic member ID.",
        action=TypeText(value_ref=ParamRef(name="member_id")),
        target=ladder(),
        preconditions=(
            AxNodeExists(
                role="textbox",
                name_matcher=NameMatcher(value="Member ID"),
                frame_path=target.frame_path,
                max_count=1,
            ),
        ),
        checkpoint=FieldValueEquals(target=target, value_ref=ParamRef(name="member_id")),
        risk="reversible",
        timing=StepTiming(settle_strategy="ax_stable", timeout_ms=2000),
        on_outcome=[
            OutcomeHandlingEntry(
                code="member_not_found", handling=StepOutcomeHandling(kind="return")
            )
        ],
        provenance=StepProvenance(
            discovery_run_id=run_id,
            observation_id=obs.observation_id,
            observation_hash=obs.hash,
            decided_by=model,
            decision_id=decision_id,
            tool_call_id="fake-call-1",
            prompt_template_id="member-entry.v1",
            prompt_hash=digest("synthetic prompt"),
            rationale_digest=digest("Unique accessible textbox"),
            created_at=clock.now(),
        ),
    )
    return Capability(
        capability_id=capability_id,
        name="enter_member_id",
        description="Synthetic one-step example: populate the member-search field.",
        version="1.0.0",
        status="candidate",
        target=CapabilityTarget(
            surface_kind="web",
            app_id="cua-synthetic-bank",
            entry_point="http://127.0.0.1:8099/t/alpha/",
            tenant_id="alpha",
            allowlist_ref="local-demo.v1",
            recorded_fingerprint=obs.fingerprint,
        ),
        inputs=(
            ParamSpec(
                name="member_id",
                json_type="string",
                required=True,
                default=None,
                example=StringValue(value="10001"),
                sensitivity="internal",
                validation=None,
                description="Synthetic member identifier.",
            ),
        ),
        outputs=(),
        steps=(step,),
        outcomes=(outcome,),
        provenance=CapabilityProvenance(
            derived_from_run_id=run_id,
            compiler_version="1.0.0",
            recorder_version="1.0.0",
            models_used=(model,),
            tool_schema_hash=digest(strict_schema(ActionInput, "action.v1").schema_body),
            recorded_at=clock.now(),
        ),
    )
