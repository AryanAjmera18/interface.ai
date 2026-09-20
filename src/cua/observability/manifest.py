"""Summarize verified run evidence at completion; forbid higher-layer imports."""

from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import Field, JsonValue

from cua.domain.common import Digest, DomainModel, canonical_json
from cua.domain.models import Usage
from cua.domain.ports import Clock
from cua.domain.provenance import LineageRef
from cua.observability._io import write_bytes
from cua.observability.context import RunContext, current_run
from cua.observability.evidence import EvidenceIndex, EvidenceStore
from cua.observability.journal import (
    ChainReport,
    JournalRecord,
    RunJournal,
    verify_chain_directory,
)
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
    cost_basis: Literal["run_time", "post_run"] = "run_time"


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
    terminal = next(
        (index for index, record in enumerate(records) if record.type == "RunEnded"), None
    )
    if chain.intact and terminal is None:
        raise RuntimeError("Record RunEnded before writing the manifest")
    if terminal is not None and any(
        record.type != "CostAmended" for record in records[terminal + 1 :]
    ):
        raise RuntimeError("Only CostAmended events may follow RunEnded")
    usage, counts, drift, amended_pricing, cost_basis = _summarize(records, pricing)
    allowed, denied = counts.allowed, counts.denied
    pricing = amended_pricing
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
        cost_basis=cost_basis,
    )
    # Field-specific masking happened at ingress. Reapplying a schema rule to its marker
    # would destroy cross-step hash equality; scan the assembled document with patterns only.
    with redaction_policy(RedactionPolicy()):
        safe = redact(canonical_json(manifest.model_dump(mode="json")).encode())
    write_bytes(evidence.root / "manifest.json", safe)
    return manifest


def _summarize(
    records: tuple[JournalRecord, ...],
    pricing: tuple[PricingEvidence, ...],
) -> tuple[
    Usage,
    PolicyCounts,
    tuple[JsonValue, ...],
    tuple[PricingEvidence, ...],
    Literal["run_time", "post_run"],
]:
    """Derive mutable accounting fields from journal facts, including visible amendments."""
    usage = Usage(
        input_tokens=0,
        cached_input_tokens=0,
        output_tokens=0,
        reasoning_tokens=0,
        cost_usd=0,
    )
    allowed, denied = 0, 0
    drift: list[JsonValue] = []
    cost_basis: Literal["run_time", "post_run"] = "run_time"
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
        elif record.type == "CostAmended":
            amended = Usage.model_validate(payload.get("usage"))
            if (
                amended.input_tokens,
                amended.cached_input_tokens,
                amended.output_tokens,
                amended.reasoning_tokens,
            ) != (
                usage.input_tokens,
                usage.cached_input_tokens,
                usage.output_tokens,
                usage.reasoning_tokens,
            ):
                raise ValueError("CostAmended usage does not match journal ModelDecided totals")
            usage = amended
            pricing = (
                PricingEvidence(
                    provider=str(payload["provider"]),
                    model_id=str(payload["model_id"]),
                    source_url=str(payload["source_url"]),
                    retrieved_on=date.fromisoformat(str(payload["retrieved_on"])),
                ),
            )
            cost_basis = "post_run"
    return usage, PolicyCounts(allowed=allowed, denied=denied), tuple(drift), pricing, cost_basis


def regenerate_manifest_from_journal(bundle: Path) -> Manifest:
    """Rebuild journal-derived manifest fields while preserving immutable runtime metadata."""
    current = Manifest.model_validate_json((bundle / "manifest.json").read_bytes())
    chain = verify_chain_directory(bundle, current.context.run_id)
    if not chain.intact:
        raise ValueError(f"Cannot regenerate manifest from broken chain: {chain.reason}")
    records = tuple(
        JournalRecord.model_validate_json(line)
        for line in (bundle / "journal.ndjson").read_bytes().splitlines()
    )
    usage, counts, drift, pricing, cost_basis = _summarize(records, current.pricing)
    amended = current.model_copy(
        update={
            "journal_head_hash": records[-1].hash if records else None,
            "chain": chain,
            "model_usage": usage,
            "pricing": pricing,
            "policy_decisions": counts,
            "drift_observations": drift,
            "cost_basis": cost_basis,
        }
    )
    with redaction_policy(RedactionPolicy()):
        safe = redact(canonical_json(amended.model_dump(mode="json")).encode())
    write_bytes(bundle / "manifest.json", safe)
    return amended
