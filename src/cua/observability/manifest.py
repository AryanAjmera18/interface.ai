"""Summarize verified run evidence at completion; forbid higher-layer imports."""

from datetime import date

from pydantic import Field, JsonValue

from cua.domain.common import Digest, DomainModel, canonical_json
from cua.domain.models import Usage
from cua.domain.ports import Clock
from cua.domain.provenance import LineageRef
from cua.observability._io import write_bytes
from cua.observability.context import RunContext, current_run
from cua.observability.evidence import EvidenceIndex, EvidenceStore
from cua.observability.journal import ChainReport, RunJournal
from cua.observability.redaction import (
    RedactionPolicy,
    TaggedValue,
    checked_json,
    redact,
    redaction_policy,
)
from cua.observability.tracing import Tracing


class PolicyCounts(DomainModel):
    allowed: int = Field(ge=0, default=0)
    denied: int = Field(ge=0, default=0)


class PricingEvidence(DomainModel):
    """Identify the dated source used for a model's computed cost."""

    provider: str
    model_id: str
    source_url: str | None
    retrieved_on: date | None


class Manifest(DomainModel):
    context: RunContext
    inputs: JsonValue
    result_summary: JsonValue
    capability_lineage: tuple[LineageRef, ...]
    journal_head_hash: Digest | None
    chain: ChainReport
    evidence_index: EvidenceIndex
    evidence_verified: bool
    span_file: str
    span_export_failures: int
    duration_ms: float = Field(ge=0)
    model_usage: Usage
    pricing: tuple[PricingEvidence, ...]
    policy_decisions: PolicyCounts
    drift_observations: tuple[JsonValue, ...]


def write_manifest(
    *,
    journal: RunJournal,
    evidence: EvidenceStore,
    tracing: Tracing,
    clock: Clock,
    inputs: TaggedValue,
    result_summary: TaggedValue,
    lineage: tuple[LineageRef, ...] = (),
    pricing: tuple[PricingEvidence, ...] = (),
) -> Manifest:
    """Write only after RunEnded and trace closure. Inputs must be redacted at /inputs.

    Explicit closure makes incomplete span bundles observable instead of silently claiming a
    finished run while its root span is still open. A corrupt chain is reported, not repaired.
    """
    run = current_run()
    if journal.head.run_id != run.run_id or evidence.index.run_id != run.run_id:
        raise ValueError("Manifest components belong to different runs")
    if tracing.processor.active_count:
        raise RuntimeError("End all spans before writing the run manifest")
    chain = journal.verify_chain()
    records = journal.records() if chain.intact else ()
    if chain.intact and (not records or records[-1].type != "RunEnded"):
        raise RuntimeError("Record RunEnded before writing the manifest")
    usage = Usage(
        input_tokens=0,
        cached_input_tokens=0,
        output_tokens=0,
        reasoning_tokens=0,
        cost_usd=0,
    )
    allowed, denied = 0, 0
    drift: list[JsonValue] = []
    for record in records:
        payload = record.payload
        if not isinstance(payload, dict):
            continue
        if record.type == "ModelDecided":
            decision = payload.get("decision")
            if isinstance(decision, dict):
                item = Usage.model_validate(decision.get("usage"))
                usage = Usage(
                    input_tokens=usage.input_tokens + item.input_tokens,
                    cached_input_tokens=usage.cached_input_tokens + item.cached_input_tokens,
                    output_tokens=usage.output_tokens + item.output_tokens,
                    reasoning_tokens=usage.reasoning_tokens + item.reasoning_tokens,
                    cost_usd=(
                        None
                        if usage.cost_usd is None or item.cost_usd is None
                        else usage.cost_usd + item.cost_usd
                    ),
                )
        elif record.type == "PolicyDecision":
            allowed += payload.get("allowed") is True
            denied += payload.get("allowed") is False
        elif record.type == "DriftObserved":
            drift.append(payload)
    manifest = Manifest(
        context=run,
        inputs=checked_json(inputs),
        result_summary=checked_json(result_summary),
        capability_lineage=lineage,
        journal_head_hash=journal.head.hash,
        chain=chain,
        evidence_index=evidence.index,
        evidence_verified=all(evidence.verify(ref) for ref in evidence.index.entries),
        span_file=tracing.path.name,
        span_export_failures=tracing.processor.failures,
        duration_ms=(clock.now() - run.started_at).total_seconds() * 1000,
        model_usage=usage,
        pricing=pricing,
        policy_decisions=PolicyCounts(allowed=allowed, denied=denied),
        drift_observations=tuple(drift),
    )
    # Field-specific masking happened at ingress. Reapplying a schema rule to its marker
    # would destroy cross-step hash equality; scan the assembled document with patterns only.
    with redaction_policy(RedactionPolicy()):
        safe = redact(canonical_json(manifest.model_dump(mode="json")).encode())
    write_bytes(evidence.root / "manifest.json", safe)
    return manifest
