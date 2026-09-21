"""Summarize verified run evidence at completion; forbid higher-layer imports."""

import hashlib
from datetime import date
from pathlib import Path
from typing import Literal, cast

from pydantic import Field, JsonValue, model_validator

from cua.domain.common import Digest, DomainModel, canonical_json
from cua.domain.models import Usage
from cua.domain.ports import Clock, ProviderFailure
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
    json_value,
    redact,
    redaction_policy,
)
from cua.observability.tracing import Tracing


class PolicyCounts(DomainModel):
    allowed: int = Field(ge=0, default=0)
    denied: int = Field(ge=0, default=0)
    escalated: int = Field(ge=0, default=0)


class ManifestUsage(DomainModel):
    """Unknown provider accounting remains null; zero requires a reported response."""

    input_tokens: int | None = Field(default=None, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)
    usage_basis: Literal["reported", "unavailable", "post_run"] = "unavailable"


class InputEntry(DomainModel):
    name: str
    value: JsonValue
    sensitivity: Literal["public", "internal", "pii", "secret"]


class ManifestInputs(DomainModel):
    """A historical run can retain null when its old journal omitted the goal."""

    goal: JsonValue = None
    parameters: tuple[InputEntry, ...] = ()


class ModelEntry(DomainModel):
    role: str
    provider: str
    model_id: str
    reasoning_effort: str | None
    source: Literal["observed", "configured"]


class OutputEntry(DomainModel):
    name: str
    value: JsonValue
    sensitivity: Literal["public", "internal", "pii", "secret"]


class ResultSummary(DomainModel):
    """Terminal classification is derived from RunEnded; failure cannot be unexplained."""

    status: Literal["success", "business", "hard_failure", "cancelled"]
    terminal_edge: str
    failure_kind: str | None = None
    failure_step: str | None = None
    failure_reason: str | None = None
    provider_error: ProviderFailure | None = None
    outputs: tuple[OutputEntry, ...] = ()

    @model_validator(mode="after")
    def failure_needs_reason(self) -> "ResultSummary":
        if self.status == "hard_failure" and not self.failure_reason:
            raise ValueError("hard_failure requires a short, redacted failure_reason")
        return self


class PricingEvidence(DomainModel):
    """Identify the dated source used for a model's computed cost."""

    provider: str
    model_id: str
    source_url: str | None
    retrieved_on: date | None


class Manifest(DomainModel):
    context: RunContext
    inputs: ManifestInputs
    result_summary: ResultSummary
    capability_lineage: tuple[LineageRef, ...]
    journal_head_hash: Digest | None
    chain: ChainReport
    evidence_index: EvidenceIndex
    evidence_verified: bool
    span_file: str
    span_export_failures: int
    duration_ms: float = Field(ge=0)
    model_usage: ManifestUsage
    models: tuple[ModelEntry, ...] = ()
    attempt_directory: str = ""
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
    models: tuple[ModelEntry, ...] = (),
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
        record.type not in {"CostAmended", "EvidenceRelabeled", "ManifestRegenerated"}
        for record in records[terminal + 1 :]
    ):
        raise RuntimeError("Only named amendment events may follow RunEnded")
    usage, counts, drift, amended_pricing, cost_basis = _summarize(records, pricing)
    pricing = amended_pricing
    manifest = Manifest(
        context=run,
        inputs=ManifestInputs.model_validate(checked_json(inputs)),
        result_summary=_result_summary(records, checked_json(result_summary)),
        capability_lineage=lineage,
        journal_head_hash=journal.head.hash,
        chain=chain,
        evidence_index=evidence.index,
        evidence_verified=all(evidence.verify(ref) for ref in evidence.index.entries),
        span_file=tracing.path.name,
        span_export_failures=tracing.processor.failures,
        duration_ms=(clock.now() - run.started_at).total_seconds() * 1000,
        model_usage=usage,
        models=_models(records, models),
        attempt_directory=evidence.root.name,
        pricing=pricing,
        policy_decisions=counts,
        drift_observations=tuple(drift),
        cost_basis=cost_basis,
    )
    # Field-specific masking happened at ingress. Reapplying a schema rule to its marker
    # would destroy cross-step hash equality; scan the assembled document with patterns only.
    with redaction_policy(RedactionPolicy()):
        safe = redact(canonical_json(manifest.model_dump(mode="json")).encode())
    write_bytes(evidence.root / "manifest.json", safe)
    return manifest


def _models(
    records: tuple[JournalRecord, ...], configured: tuple[ModelEntry, ...]
) -> tuple[ModelEntry, ...]:
    """Journal decisions prove actual use; config identifies a rejected pre-decision request."""
    observed: dict[tuple[str, str], ModelEntry] = {}
    for record in records:
        if record.type != "ModelDecided" or not isinstance(record.payload, dict):
            continue
        decision = record.payload.get("decision")
        if not isinstance(decision, dict):
            continue
        model = decision.get("model")
        if not isinstance(model, dict):
            continue
        provider, model_id = model.get("provider"), model.get("model_id")
        if not isinstance(provider, str) or not isinstance(model_id, str):
            continue
        effort = model.get("reasoning_effort")
        entry = ModelEntry(
            role="discovery_planner",
            provider=provider,
            model_id=model_id,
            reasoning_effort=effort if isinstance(effort, str) else None,
            source="observed",
        )
        observed[(provider, model_id)] = entry
    if observed:
        return tuple(observed.values())
    for record in reversed(records):
        if record.type != "ManifestRegenerated" or not isinstance(record.payload, dict):
            continue
        model = record.payload.get("configured_model")
        if not isinstance(model, dict):
            continue
        provider, model_id = model.get("provider"), model.get("model_id")
        if isinstance(provider, str) and isinstance(model_id, str):
            effort = model.get("reasoning_effort")
            return (
                ModelEntry(
                    role="discovery_planner",
                    provider=provider,
                    model_id=model_id,
                    reasoning_effort=effort if isinstance(effort, str) else None,
                    source="configured",
                ),
            )
    return configured


def _result_summary(records: tuple[JournalRecord, ...], supplied: JsonValue) -> ResultSummary:
    """Derive the terminal claim and sensitive outputs from journal facts, not caller prose."""
    terminal = next((record for record in reversed(records) if record.type == "RunEnded"), None)
    if terminal is None or not isinstance(terminal.payload, dict):
        raise ValueError("RunEnded is required for result_summary")
    payload = terminal.payload
    status = str(payload["result"])
    edge = str(payload["summary"])
    details = supplied if isinstance(supplied, dict) else {}
    outputs: list[OutputEntry] = []
    for record in records:
        if record.type != "ActionResult" or not isinstance(record.payload, dict):
            continue
        result = record.payload.get("result")
        if not isinstance(result, dict):
            continue
        action = result.get("attempted")
        if not isinstance(action, dict) or action.get("kind") != "read_value":
            continue
        name, value = action.get("output_name"), result.get("value")
        if not isinstance(name, str) or value is None:
            continue
        safe = (
            value
            if isinstance(value, dict) and value.get("redacted") is True
            else json_value(redact(value, sensitivity="pii"))
        )
        outputs.append(OutputEntry(name=name, value=safe, sensitivity="pii"))
    reason = payload.get("failure_reason") or details.get("failure_reason")
    if status == "hard_failure" and not reason:
        reason = edge
    failure_step = payload.get("failure_step") or details.get("failure_step")
    provider_raw = payload.get("provider_error") or details.get("provider_error")
    provider_error = ProviderFailure.model_validate(provider_raw) if provider_raw else None
    failure_kind = payload.get("failure_kind")
    return ResultSummary(
        status=cast(Literal["success", "business", "hard_failure", "cancelled"], status),
        terminal_edge=edge,
        failure_kind=(
            str(failure_kind) if failure_kind else edge if status == "hard_failure" else None
        ),
        failure_step=failure_step if isinstance(failure_step, str) else None,
        failure_reason=str(reason) if reason is not None else None,
        provider_error=provider_error,
        outputs=tuple(outputs),
    )


def _summarize(
    records: tuple[JournalRecord, ...],
    pricing: tuple[PricingEvidence, ...],
) -> tuple[
    ManifestUsage,
    PolicyCounts,
    tuple[JsonValue, ...],
    tuple[PricingEvidence, ...],
    Literal["run_time", "post_run"],
]:
    """Derive mutable accounting fields from journal facts, including visible amendments."""
    usage = ManifestUsage()
    allowed, denied, escalated = 0, 0, 0
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
                usage = ManifestUsage(
                    input_tokens=(usage.input_tokens or 0) + item.input_tokens,
                    cached_input_tokens=(usage.cached_input_tokens or 0) + item.cached_input_tokens,
                    output_tokens=(usage.output_tokens or 0) + item.output_tokens,
                    reasoning_tokens=(usage.reasoning_tokens or 0) + item.reasoning_tokens,
                    cost_usd=(
                        None
                        if usage.usage_basis != "reported"
                        or usage.cost_usd is None
                        or item.cost_usd is None
                        else usage.cost_usd + item.cost_usd
                    )
                    if usage.usage_basis != "unavailable"
                    else item.cost_usd,
                    usage_basis="reported",
                )
        elif record.type == "PolicyDecision":
            if payload.get("verdict") == "escalate":
                escalated += 1
            else:
                allowed += payload.get("allowed") is True
                denied += payload.get("allowed") is False
        elif record.type == "DriftObserved":
            drift.append(payload)
        elif record.type == "CostAmended":
            amended = Usage.model_validate(payload.get("usage"))
            # A rejected request has no ModelDecided response. Historical zero-valued
            # amendments cannot turn missing provider usage into a reported zero.
            if usage.usage_basis == "unavailable":
                pricing = (
                    PricingEvidence(
                        provider=str(payload["provider"]),
                        model_id=str(payload["model_id"]),
                        source_url=str(payload["source_url"]),
                        retrieved_on=date.fromisoformat(str(payload["retrieved_on"])),
                    ),
                )
                cost_basis = "post_run"
                continue
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
            usage = ManifestUsage(
                input_tokens=amended.input_tokens,
                cached_input_tokens=amended.cached_input_tokens,
                output_tokens=amended.output_tokens,
                reasoning_tokens=amended.reasoning_tokens,
                cost_usd=amended.cost_usd,
                usage_basis="post_run",
            )
            pricing = (
                PricingEvidence(
                    provider=str(payload["provider"]),
                    model_id=str(payload["model_id"]),
                    source_url=str(payload["source_url"]),
                    retrieved_on=date.fromisoformat(str(payload["retrieved_on"])),
                ),
            )
            cost_basis = "post_run"
    return (
        usage,
        PolicyCounts(allowed=allowed, denied=denied, escalated=escalated),
        tuple(drift),
        pricing,
        cost_basis,
    )


def regenerate_manifest_from_journal(
    bundle: Path, *, published_blob_hashes: frozenset[str] | None = None
) -> Manifest:
    """Rebuild journal-derived manifest fields while preserving immutable runtime metadata."""
    import json

    old = json.loads((bundle / "manifest.json").read_bytes())
    run_id = str(old["context"]["run_id"])
    chain = verify_chain_directory(bundle, run_id)
    if not chain.intact:
        raise ValueError(f"Cannot regenerate manifest from broken chain: {chain.reason}")
    records = tuple(
        JournalRecord.model_validate_json(line)
        for line in (bundle / "journal.ndjson").read_bytes().splitlines()
    )
    old["attempt_directory"] = bundle.name
    if not old.get("inputs"):
        old["inputs"] = {"goal": None, "parameters": []}
    for record in reversed(records):
        if record.type == "ManifestRegenerated" and isinstance(record.payload, dict):
            goal = record.payload.get("goal")
            if isinstance(goal, str):
                old["inputs"]["goal"] = json_value(redact(goal, sensitivity="internal"))
            break
    index_path = bundle / "index.json"
    if index_path.exists():
        old["evidence_index"] = EvidenceIndex.model_validate_json(
            index_path.read_bytes()
        ).model_dump(mode="json")
    entries = EvidenceIndex.model_validate(old["evidence_index"]).entries
    old["evidence_verified"] = all(
        (published_blob_hashes is None or entry.sha256 in published_blob_hashes)
        and (bundle / "blobs" / entry.sha256[:2] / entry.sha256).is_file()
        and hashlib.sha256(
            (bundle / "blobs" / entry.sha256[:2] / entry.sha256).read_bytes()
        ).hexdigest()
        == entry.sha256
        for entry in entries
    )
    old["result_summary"] = _result_summary(records, old.get("result_summary", {})).model_dump(
        mode="json"
    )
    current = Manifest.model_validate(old)
    usage, counts, drift, pricing, cost_basis = _summarize(records, current.pricing)
    amended = current.model_copy(
        update={
            "journal_head_hash": records[-1].hash if records else None,
            "chain": chain,
            "model_usage": usage,
            "models": _models(records, current.models),
            "attempt_directory": bundle.name,
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
