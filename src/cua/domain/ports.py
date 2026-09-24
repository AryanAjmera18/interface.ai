"""Declare effect boundaries only; forbid implementations, I/O, and other cua packages."""

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal, Protocol

from pydantic import AwareDatetime, Field, HttpUrl, JsonValue

from cua.domain.actions import Action
from cua.domain.capability import Capability, ExtractorSpec
from cua.domain.common import ULID, Digest, DomainModel, EvidenceRef, ValueRef
from cua.domain.locators import LocatorCandidate, LocatorLadder
from cua.domain.models import DecisionRequest, DecisionResult, Usage
from cua.domain.observation import AxNode, Observation, SurfaceFingerprint
from cua.domain.predicates import AxTarget, PredicateResult
from cua.domain.provenance import ModelRef
from cua.domain.steps import StepTiming


class ExtractedValue(DomainModel):
    value: JsonValue
    evidence_ref: EvidenceRef


class EvidencePayload(DomainModel):
    """Raw image bytes cross this in-process port; the sink must redact before persistence."""

    media_type: str
    content: bytes
    observation_hash: Digest | None = None
    observation_id: ULID | None = None
    ax_root: AxNode | None = None


class Surface(Protocol):
    async def observe(self) -> Observation: ...
    async def resolve(self, ladder: LocatorLadder) -> "Resolution": ...
    async def act(
        self,
        action: Action,
        resolved_target: "ResolvedTarget | None" = None,
        *,
        timing: StepTiming | None = None,
    ) -> "ActionResult": ...
    async def settle(self, timing: StepTiming) -> Observation: ...
    async def cede_control(self) -> "ControlToken": ...
    async def resume_control(self, token: "ControlToken") -> None: ...


class CandidateAttempt(DomainModel):
    candidate: LocatorCandidate
    outcome: Literal["matched", "not_found", "ambiguous", "wrong_frame", "not_actionable"]
    detail: str


class ResolvedTarget(DomainModel):
    """Opaque adapter reference; never a Playwright handle or an executable locator."""

    handle: str
    observation_hash: Digest


class Resolution(DomainModel):
    winning_candidate: LocatorCandidate | None
    index: int | None
    match_count: int = Field(ge=0)
    attempts: tuple[CandidateAttempt, ...]
    degraded: bool
    degradation_delta: float = Field(ge=0)
    target: ResolvedTarget | None


class TreeChange(DomainModel):
    """Diff metadata excludes raw names/values so timeout errors do not become a data exporter."""

    frame_path: tuple[str, ...]
    node_path: tuple[int, ...]
    fields: tuple[str, ...]


class SettleFailure(DomainModel):
    last_hashes: tuple[Digest, Digest]
    diff: tuple[TreeChange, ...]
    pending_requests: int
    pending_navigation: bool


class SettleTimeout(TimeoutError):
    def __init__(self, detail: SettleFailure) -> None:
        self.detail = detail
        super().__init__("Surface did not settle before its deadline; inspect typed diff metadata")


class ActionResult(DomainModel):
    attempted: Action
    before_hash: Digest
    after_hash: Digest
    changed: bool
    settle_outcome: Literal["settled", "not_requested"]
    value: str | None = None


class ControlToken(DomainModel):
    """Session attachment coordinates, not authorization; an escalation lease must protect them."""

    cdp_endpoint: str
    context_id: str
    page_guid: str
    issued_at: AwareDatetime


class ValueResolver(Protocol):
    async def resolve_value(self, reference: ValueRef) -> str: ...


class LLMClient(Protocol):
    async def decide(self, request: DecisionRequest) -> DecisionResult: ...


class Extractor(Protocol):
    def extract(self, spec: ExtractorSpec, observation: Observation) -> ExtractedValue: ...


NodeAddress = tuple[tuple[str, ...], tuple[int, ...]]


class EvidenceSink(Protocol):
    def sensitive_bounds_targets(self, root: AxNode) -> tuple[NodeAddress, ...]: ...
    async def put(self, payload: EvidencePayload) -> EvidenceRef: ...
    def verify(self, reference: EvidenceRef) -> bool: ...


class JournalSink(Protocol):
    async def append(self, entry: "JournalEvent") -> str: ...


class Clock(Protocol):
    def now(self) -> datetime: ...


class IdGenerator(Protocol):
    def new(self) -> str: ...


class VerificationCheck(DomainModel):
    asserted: str
    found: str
    passed: bool


class VerificationReport(DomainModel):
    verified: bool
    checks: tuple[VerificationCheck, ...]


class ProvenanceVerifier(Protocol):
    """Schema validation proves an artifact CLAIMS attribution; verification proves the claim.

    Stage 7 must resolve observation hashes in the named run, verify the chain through each
    referenced record, resolve and hash every evidence_ref, and check that HumanEdit before/after
    hashes bracket real changes. Without this boundary provenance is decoration.
    Verify approval.reviewed_digest against capability_content_digest and the journal's review
    record, including the authenticated reviewer and exactly the content that was reviewed.
    """

    def verify(self, capability: Capability) -> VerificationReport: ...


class RunStarted(DomainModel):
    type: Literal["RunStarted"] = "RunStarted"
    kind: Literal["discovery", "replay", "escalation", "verification"]
    parent_run_id: ULID | None = None


class PageEvent(DomainModel):
    """State-derived events require the normalized observation digest, never a best-effort join.

    Journal payloads are tool results, not model inputs: they are intentionally exempt from
    provider strict-schema restrictions. Redaction may replace values in the persisted payload;
    validate this typed union before redaction, not by coercing masked values back into originals.
    """

    observation_hash: Digest


class Observed(PageEvent):
    type: Literal["Observed"] = "Observed"
    observation_id: ULID
    evidence_ref: EvidenceRef
    fingerprint: SurfaceFingerprint | None = None
    url: str | None = None
    title: str | None = None


class ModelDecided(PageEvent):
    type: Literal["ModelDecided"] = "ModelDecided"
    decision: DecisionResult


class PolicyDecision(PageEvent):
    type: Literal["PolicyDecision"] = "PolicyDecision"
    allowed: bool
    rule: str
    reason: str
    verdict: Literal["allow", "deny", "escalate"] | None = None


class ActionAttempted(PageEvent):
    type: Literal["ActionAttempted"] = "ActionAttempted"
    step_id: str
    intent: str
    action: Action
    target: AxTarget | None
    locator_ladder: LocatorLadder | None


class LocatorResolved(PageEvent):
    """Record the winner and every miss so fallback is observable, including successful runs."""

    type: Literal["LocatorResolved"] = "LocatorResolved"
    step_id: str
    winning_index: int | None = Field(default=None, ge=0)
    attempts: tuple[CandidateAttempt, ...]


class ActionResultEvent(PageEvent):
    type: Literal["ActionResult"] = "ActionResult"
    step_id: str
    result: ActionResult


class CheckpointEvaluated(PageEvent):
    type: Literal["CheckpointEvaluated"] = "CheckpointEvaluated"
    step_id: str
    result: PredicateResult


class OutcomeDetected(PageEvent):
    type: Literal["OutcomeDetected"] = "OutcomeDetected"
    code: str
    classification: Literal["business", "recoverable", "hard"]


class RecoveredEvent(PageEvent):
    """Recovery is observable history; it never becomes a terminal caller result."""

    type: Literal["Recovered"] = "Recovered"
    step_id: str
    outcome_code: str
    recovery_kind: str
    attempt: int = Field(ge=1)


class DriftObserved(PageEvent):
    type: Literal["DriftObserved"] = "DriftObserved"
    expected_hash: Digest
    detail: str
    degradation_delta: float = Field(ge=0)


class EscalationRaised(PageEvent):
    type: Literal["EscalationRaised"] = "EscalationRaised"
    escalation_id: ULID
    reason: str


class ControlTransferred(PageEvent):
    type: Literal["ControlTransferred"] = "ControlTransferred"
    actor: str = Field(min_length=1)


class HumanAction(PageEvent):
    type: Literal["HumanAction"] = "HumanAction"
    actor: str = Field(min_length=1)
    action: Action


class ControlReturned(PageEvent):
    type: Literal["ControlReturned"] = "ControlReturned"
    actor: str = Field(min_length=1)


class HumanChangeSummary(PageEvent):
    """Record a metadata-only AX diff after automation regains the live session."""

    type: Literal["HumanChangeSummary"] = "HumanChangeSummary"
    actor: str = Field(min_length=1)
    before_hash: Digest
    after_hash: Digest
    changes: tuple[TreeChange, ...]
    notes: str = ""


class DraftReplayAuthorized(DomainModel):
    """Make the caller's explicit bypass of approval visible in replay history."""

    type: Literal["DraftReplayAuthorized"] = "DraftReplayAuthorized"
    capability_id: ULID


class CapabilityCompiled(DomainModel):
    type: Literal["CapabilityCompiled"] = "CapabilityCompiled"
    capability_id: ULID
    content_digest: Digest


class CapabilityApproved(DomainModel):
    """Bind a named review act to the exact canonical content reviewed."""

    type: Literal["CapabilityApproved"] = "CapabilityApproved"
    capability_id: ULID
    actor: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    reviewed_digest: Digest


class CostAmended(DomainModel):
    """Record a post-run price without rewriting the usage events that existed at execution."""

    type: Literal["CostAmended"] = "CostAmended"
    provider: str
    model_id: str
    input_per_mtok: Decimal = Field(ge=0)
    cached_input_per_mtok: Decimal | None = Field(default=None, ge=0)
    output_per_mtok: Decimal = Field(ge=0)
    currency: str
    source_url: HttpUrl
    retrieved_on: date
    usage: Usage
    reason: Literal["pricing entry filled after the run"]


class EvidenceRelabeled(DomainModel):
    """Metadata correction is visible history; immutable blob bytes and hashes stay unchanged."""

    type: Literal["EvidenceRelabeled"] = "EvidenceRelabeled"
    evidence_id: ULID
    original_kind: str
    corrected_kind: str
    original_media_type: str
    corrected_media_type: str
    reason: str


class TargetMetadataReconstructed(DomainModel):
    """Post-run fixture metadata is labeled reconstruction, never a recorded observation."""

    type: Literal["TargetMetadataReconstructed"] = "TargetMetadataReconstructed"
    fingerprint: SurfaceFingerprint
    entry_point: str
    source_commit: str
    source: Literal["target_app_config"] = "target_app_config"
    reason: str


class ManifestRegenerated(DomainModel):
    """Reconstruction is appended after the original result, never a silent rewrite."""

    type: Literal["ManifestRegenerated"] = "ManifestRegenerated"
    attempt_directory: str
    reason: str
    goal: str | None = None
    goal_source: Literal["journal", "reviewer_reconstruction", "unavailable"] = "unavailable"
    configured_model: ModelRef | None = None


class ProviderFailure(DomainModel):
    code: str
    safe_message: str


class RunEnded(DomainModel):
    type: Literal["RunEnded"] = "RunEnded"
    result: Literal["success", "business", "hard_failure", "cancelled"]
    summary: str
    failure_kind: str | None = None
    failure_reason: str | None = None
    failure_step: str | None = None
    provider_error: ProviderFailure | None = None


JournalEvent = Annotated[
    RunStarted
    | Observed
    | ModelDecided
    | PolicyDecision
    | ActionAttempted
    | LocatorResolved
    | ActionResultEvent
    | CheckpointEvaluated
    | OutcomeDetected
    | RecoveredEvent
    | DriftObserved
    | EscalationRaised
    | ControlTransferred
    | HumanAction
    | ControlReturned
    | HumanChangeSummary
    | CapabilityCompiled
    | CapabilityApproved
    | DraftReplayAuthorized
    | CostAmended
    | EvidenceRelabeled
    | TargetMetadataReconstructed
    | ManifestRegenerated
    | RunEnded,
    Field(discriminator="type"),
]
