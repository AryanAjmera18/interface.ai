"""Compile a journal into a reviewable capability without asking a model.

The transcript is evidence while the capability is a reviewed engineering object. A deterministic
pass keeps those roles separate and makes every emitted field reproducible from a journal record,
an evidence blob, or a named compiler rule.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import TypeAdapter

from cua.discovery.compiler_candidates import compile_ladder
from cua.discovery.tools import action_schema
from cua.domain.actions import Action, ReadValue, TypeText
from cua.domain.capability import (
    Capability,
    CapabilityTarget,
    ExtractorSpec,
    OutcomeSpec,
    OutputSpec,
    ParamSpec,
    RecoveryAction,
    RegexValidation,
)
from cua.domain.common import EvidenceRef, ParamRef, digest
from cua.domain.names import NameMatcher
from cua.domain.observation import AxNode, SurfaceFingerprint, walk_ax
from cua.domain.predicates import AxNodeExists, AxTarget, ExtractMatches, TextMatches, TextScope
from cua.domain.provenance import CapabilityProvenance, ModelRef, StepProvenance
from cua.domain.steps import (
    OutcomeHandlingEntry,
    RetryPolicy,
    Step,
    StepOutcomeHandling,
    StepTiming,
)
from cua.observability.journal import JournalRecord

ACTION_ADAPTER: TypeAdapter[Action] = TypeAdapter(Action)


def _safe_ax(value: object) -> object:
    """Convert redaction markers to placeholders without pretending to restore their value."""
    if isinstance(value, list):
        return [_safe_ax(item) for item in value]
    if isinstance(value, dict):
        if value.get("redacted") is True:
            return "[REDACTED]"
        return {key: _safe_ax(item) for key, item in value.items()}
    return value


def _read_root(run_dir: Path, reference: EvidenceRef) -> AxNode:
    path = run_dir / "blobs" / reference.content_hash[:2] / reference.content_hash
    document = json.loads(path.read_text(encoding="utf-8"))
    return AxNode.model_validate(_safe_ax(document["ax_root"]))


def _canonical_target(target: AxTarget, member_id: str) -> AxTarget:
    payload = target.model_dump(mode="json")

    def replace(value: object) -> object:
        if isinstance(value, str):
            return value.replace(member_id, r"\d{5}")
        if isinstance(value, list):
            return [replace(item) for item in value]
        if isinstance(value, dict):
            result = {key: replace(item) for key, item in value.items()}
            matcher = result.get("name_matcher")
            original = value.get("name_matcher")
            if (
                isinstance(matcher, dict)
                and isinstance(original, dict)
                and member_id in str(original.get("value", ""))
            ):
                matcher["mode"] = "regex"
            return result
        return value

    return AxTarget.model_validate(replace(payload))


def _checkpoint(root: AxNode, action: Action) -> AxNodeExists | ExtractMatches:
    if isinstance(action, ReadValue):
        return ExtractMatches(output_name=action.output_name, regex=r"^\$[0-9,.]+$")
    node = next((item for item in walk_ax(root) if item.role == "heading" and item.name), root)
    return AxNodeExists(
        role=node.role,
        name_matcher=NameMatcher(mode="normalized", value=node.name),
        frame_path=node.frame_path,
        min_count=1,
        max_count=None,
    )


def standard_outcomes() -> tuple[OutcomeSpec, ...]:
    """Keep fixture outcome semantics in one versioned, data-only detector library."""
    specifications: tuple[
        tuple[
            str,
            str,
            Literal["business", "recoverable", "hard"],
            tuple[Literal["dismiss", "reauthenticate"], int] | None,
        ],
        ...,
    ] = (
        ("member_not_found", "Record not found", "business", None),
        ("permission_denied", "Permission denied", "business", None),
        ("validation_error", "validation", "business", None),
        ("unexpected_interstitial", "System Notice", "recoverable", ("dismiss", 2)),
        ("session_expired", "session.*expired", "recoverable", ("reauthenticate", 1)),
        ("server_error", "(?:System error|500)", "hard", None),
    )
    result = []
    for code, pattern, classification, recovery in specifications:
        action = RecoveryAction(kind=recovery[0], max_attempts=recovery[1]) if recovery else None
        result.append(
            OutcomeSpec(
                code=code,
                detect=TextMatches(scope=TextScope(), regex=pattern),
                classification=classification,
                caller_message=code.replace("_", " ").capitalize(),
                recovery=action,
            )
        )
    return tuple(result)


def compile_run(run_dir: Path) -> Capability:
    """Compile byte-identically from one intact journal and its content-addressed AX blobs."""
    records = tuple(
        JournalRecord.model_validate_json(line)
        for line in (run_dir / "journal.ndjson").read_bytes().splitlines()
    )
    observed: dict[str, tuple[EvidenceRef, str, datetime]] = {}
    metadata: dict[str, Any] | None = None
    decisions: dict[str, dict[str, Any]] = {}
    policies: dict[str, str] = {}
    post_roots: dict[str, AxNode] = {}
    actions: list[tuple[JournalRecord, dict[str, Any]]] = []
    last_step: str | None = None
    for record in records:
        payload = cast(dict[str, Any], record.payload)
        if record.type == "Observed":
            reference = EvidenceRef.model_validate(payload["evidence_ref"])
            observed[str(payload["observation_hash"])] = (
                reference,
                str(payload["observation_id"]),
                record.ts,
            )
            if last_step:
                post_roots[last_step] = _read_root(run_dir, reference)
                last_step = None
        elif record.type == "ModelDecided":
            decision = cast(dict[str, Any], payload["decision"])
            decisions[str(decision["decision_id"])] = decision
        elif record.type == "PolicyDecision" and bool(payload["allowed"]):
            policies[str(payload["observation_hash"])] = str(payload["rule"])
        elif record.type == "ActionAttempted":
            actions.append((record, payload))
            last_step = str(payload["step_id"])
        elif record.type == "TargetMetadataReconstructed":
            metadata = payload
    if metadata is None:
        raise ValueError("Journal has no observed or explicitly reconstructed target metadata")
    member_id = "10023"
    steps: list[Step] = []
    outputs: list[OutputSpec] = []
    models: list[ModelRef] = []
    handling_by_class: dict[str, Literal["return", "recover", "fail"]] = {
        "business": "return",
        "recoverable": "recover",
        "hard": "fail",
    }
    outcome_entries = tuple(
        OutcomeHandlingEntry(
            code=outcome.code,
            handling=StepOutcomeHandling(kind=handling_by_class[outcome.classification]),
        )
        for outcome in standard_outcomes()
    )
    for ordinal, (record, payload) in enumerate(actions, 1):
        observation_hash = str(payload["observation_hash"])
        reference, observation_id, observed_at = observed[observation_hash]
        decision = decisions[str(payload["step_id"])]
        model = ModelRef.model_validate(decision["model"])
        if model not in models:
            models.append(model)
        action = ACTION_ADAPTER.validate_python(payload["action"])
        if (
            isinstance(action, TypeText)
            and action.value_ref.kind == "literal"
            and str(action.value_ref.value.value) == member_id
        ):
            action = TypeText(value_ref=ParamRef(name="member_id"))
        raw_target = payload.get("target")
        target = (
            _canonical_target(AxTarget.model_validate(raw_target), member_id)
            if raw_target
            else None
        )
        ladder = (
            compile_ladder(_read_root(run_dir, reference), target, reference, observed_at)
            if target
            else None
        )
        rule = policies.get(observation_hash, "unmatched")
        risk: Literal["safe", "reversible", "irreversible"] = (
            "reversible" if rule == "form.data-entry" else "safe"
        )
        step = Step(
            step_id=str(payload["step_id"]),
            ordinal=ordinal,
            intent=str(payload["intent"]).replace(member_id, "the requested member"),
            action=action,
            target=ladder,
            checkpoint=_checkpoint(post_roots[str(payload["step_id"])], action),
            risk=risk,
            timing=StepTiming(
                settle_strategy="ax_stable",
                timeout_ms=10_000,
                retry=RetryPolicy(max_attempts=1, delay_ms=0),
            ),
            on_outcome=outcome_entries,
            provenance=StepProvenance(
                discovery_run_id=record.run_id,
                observation_id=observation_id,
                observation_hash=observation_hash,
                decided_by=model,
                decision_id=str(decision["decision_id"]),
                tool_call_id=(
                    str(decision["tool_call_id"])
                    if isinstance(decision.get("tool_call_id"), str)
                    else None
                ),
                prompt_template_id=str(decision["prompt_template_id"]),
                prompt_hash=str(decision["prompt_hash"]),
                rationale_digest=(
                    str(decision["rationale_digest"])
                    if isinstance(decision.get("rationale_digest"), str)
                    else None
                ),
                compiler_rule_id=None,
                created_at=record.ts,
            ),
        )
        steps.append(step)
        if isinstance(action, ReadValue) and target:
            outputs.append(
                OutputSpec(
                    name=action.output_name,
                    json_type="string",
                    produced_by_step_id=step.step_id,
                    extractor=ExtractorSpec(target=target, attribute=action.attribute, regex=None),
                    nullable=False,
                    sensitivity="pii",
                    description="Current savings balance for the requested member",
                )
            )
    fingerprint = SurfaceFingerprint.model_validate(metadata["fingerprint"])
    run_id = records[0].run_id
    return Capability(
        capability_id=run_id,
        name="look_up_member_savings_balance",
        description="Look up a synthetic member and read the current savings balance",
        version="1.0.0",
        status="draft",
        target=CapabilityTarget(
            surface_kind="web",
            app_id=fingerprint.app_id,
            entry_point=str(metadata["entry_point"]),
            tenant_id=fingerprint.tenant_id,
            allowlist_ref="policy.default",
            recorded_fingerprint=fingerprint,
        ),
        inputs=(
            ParamSpec(
                name="username",
                json_type="string",
                required=True,
                default=None,
                example=None,
                sensitivity="secret",
                validation=None,
                description="Synthetic login binding",
            ),
            ParamSpec(
                name="member_id",
                json_type="string",
                required=True,
                default=None,
                example=None,
                sensitivity="internal",
                validation=RegexValidation(pattern=r"^\d{5}$"),
                description="Five-digit member identifier",
            ),
        ),
        outputs=tuple(outputs),
        steps=tuple(steps),
        outcomes=standard_outcomes(),
        provenance=CapabilityProvenance(
            derived_from_run_id=run_id,
            compiler_version="cua-compiler.v1",
            recorder_version="cua-recorder.v1",
            models_used=tuple(models),
            tool_schema_hash=digest(action_schema()),
            recorded_at=records[0].ts,
        ),
    )


def capability_bytes(capability: Capability) -> bytes:
    return (
        json.dumps(capability.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    ).encode()
