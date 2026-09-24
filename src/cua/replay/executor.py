"""Execute reviewed capability data deterministically without a model dependency."""

import re
from typing import Literal, cast

from pydantic import JsonValue

from cua.domain.actions import Click, Dismiss, ReadValue, TypeText
from cua.domain.capability import Capability, EnumValidation, RangeValidation, RegexValidation
from cua.domain.common import (
    BooleanValue,
    DomainModel,
    EvidenceRef,
    LiteralValue,
    NullValue,
    NumberValue,
    SecretRef,
    StringValue,
)
from cua.domain.locators import LocatorLadder, RoleNameCandidate, RoleNameValue
from cua.domain.names import NameMatcher
from cua.domain.observation import Observation, observation_json, walk_ax
from cua.domain.ports import (
    ActionAttempted,
    ActionResultEvent,
    Clock,
    DraftReplayAuthorized,
    DriftObserved,
    EvidencePayload,
    EvidenceSink,
    JournalSink,
    LocatorResolved,
    OutcomeDetected,
    RecoveredEvent,
    Surface,
)
from cua.domain.ports import (
    PolicyDecision as PolicyDecisionEvent,
)
from cua.domain.predicates import Binding, EvaluationContext, evaluate
from cua.domain.results import BusinessOutcome, HardFailure, ReplayResult, Success
from cua.domain.steps import Step
from cua.policy.engine import PolicyEngine
from cua.policy.models import Budget, PolicyContext, TargetSemantics
from cua.replay.overrides import TenantOverride


class ReplayInput(DomainModel):
    name: str
    value: LiteralValue


class ReplayOptions(DomainModel):
    allow_draft: bool = False
    allow_irreversible: bool = False
    trace_id: str
    resume_after_step_id: str | None = None
    tenant_override: TenantOverride | None = None


def validate_inputs(capability: Capability, inputs: tuple[ReplayInput, ...]) -> str | None:
    """Validate before dispatch so invalid caller data cannot partially mutate the surface."""
    supplied = {item.name: item.value for item in inputs}
    for parameter in capability.inputs:
        value = supplied.get(parameter.name, parameter.default)
        if value is None:
            if parameter.sensitivity == "secret":
                continue
            if parameter.required:
                return f"Missing required input {parameter.name}"
            continue
        expected = "number" if parameter.json_type == "integer" else parameter.json_type
        if value.kind != expected:
            return f"Input {parameter.name} must be {parameter.json_type}"
        rule = parameter.validation
        if isinstance(rule, RegexValidation) and re.search(rule.pattern, str(value.value)) is None:
            return f"Input {parameter.name} does not match {rule.pattern}"
        if isinstance(rule, RangeValidation):
            number = float(cast(float, value.value))
            if (rule.minimum is not None and number < rule.minimum) or (
                rule.maximum is not None and number > rule.maximum
            ):
                return f"Input {parameter.name} is outside its allowed range"
        if isinstance(rule, EnumValidation) and value not in rule.values:
            return f"Input {parameter.name} is outside its allowed enum"
    unknown = set(supplied) - {parameter.name for parameter in capability.inputs}
    return f"Unknown input {sorted(unknown)[0]}" if unknown else None


def _literal(value: JsonValue) -> LiteralValue:
    if value is None:
        return NullValue()
    if isinstance(value, bool):
        return BooleanValue(value=value)
    if isinstance(value, (int, float)):
        return NumberValue(value=float(value))
    return StringValue(value=str(value))


class ReplayExecutor:
    """Run policy, locator, detector, and checkpoint data with no model fallback.

    LLMClient is deliberately absent from this constructor. A missing control is a typed failure;
    allowing replay to ask a model would make identical inputs produce unreviewed new behavior.
    """

    def __init__(
        self,
        *,
        surface: Surface,
        policy: PolicyEngine,
        evidence: EvidenceSink,
        journal: JournalSink,
        clock: Clock,
    ) -> None:
        self.surface = surface
        self.policy = policy
        self.evidence = evidence
        self.journal = journal
        self.clock = clock

    async def _evidence(self, observation: Observation) -> EvidenceRef:
        if observation.screenshot_ref is not None:
            return observation.screenshot_ref
        return await self.evidence.put(
            EvidencePayload(
                media_type="application/json",
                content=observation_json(observation).encode(),
                observation_hash=observation.hash,
                observation_id=observation.observation_id,
                ax_root=observation.ax_root,
            )
        )

    async def _hard(
        self,
        step: Step,
        observation: Observation,
        expected: str,
        observed: str,
        kind: Literal[
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
        ],
        options: ReplayOptions,
        head: str,
    ) -> HardFailure:
        return HardFailure(
            step_id=step.step_id,
            step_intent=step.intent,
            expected=expected,
            observed=observed,
            failure_kind=kind,
            evidence_ref=await self._evidence(observation),
            trace_id=options.trace_id,
            journal_head_hash=head,
        )

    async def execute(
        self, capability: Capability, inputs: tuple[ReplayInput, ...], options: ReplayOptions
    ) -> ReplayResult:
        started = self.clock.now()
        observation = await self.surface.observe()
        evidence = await self._evidence(observation)
        invalid = validate_inputs(capability, inputs)
        if capability.status != "approved" and not options.allow_draft:
            invalid = "Draft capability requires allow_draft=True"
        if invalid is not None:
            first = capability.steps[0]
            return HardFailure(
                step_id=first.step_id,
                step_intent=first.intent,
                expected="valid inputs and execution authorization",
                observed=invalid,
                failure_kind="invalid_input",
                evidence_ref=evidence,
                trace_id=options.trace_id,
                journal_head_hash="0" * 64,
            )
        if options.tenant_override is not None:
            options.tenant_override.verify_parent(capability)
        outputs: dict[str, JsonValue] = {}
        head = "0" * 64
        if capability.status != "approved":
            head = await self.journal.append(
                DraftReplayAuthorized(capability_id=capability.capability_id)
            )
        fingerprint = observation.fingerprint
        recorded = capability.target.recorded_fingerprint
        if (
            fingerprint.app_version != recorded.app_version
            or fingerprint.config_hash != recorded.config_hash
        ):
            head = await self.journal.append(
                DriftObserved(
                    observation_hash=observation.hash,
                    expected_hash=recorded.config_hash,
                    detail=(
                        f"surface fingerprint mismatch: app {recorded.app_version} -> "
                        f"{fingerprint.app_version}, config {recorded.config_hash} -> "
                        f"{fingerprint.config_hash}"
                    ),
                    degradation_delta=1,
                )
            )
        parameters = tuple(Binding(name=item.name, value=item.value) for item in inputs)
        start = 0
        if options.resume_after_step_id is not None:
            start = next(
                (
                    index + 1
                    for index, item in enumerate(capability.steps)
                    if item.step_id == options.resume_after_step_id
                ),
                len(capability.steps),
            )
        for index, step in enumerate(capability.steps[start:], start=start):
            patch = (
                options.tenant_override.step_patch(step.step_id)
                if options.tenant_override is not None
                else None
            )
            if patch is not None:
                step = step.model_copy(
                    update={
                        "target": patch.target if patch.target is not None else step.target,
                        "checkpoint": (
                            patch.checkpoint if patch.checkpoint is not None else step.checkpoint
                        ),
                    }
                )
            before_context = EvaluationContext(parameters=parameters)
            failed_precondition = next(
                (
                    evaluate(item, observation, before_context)
                    for item in step.preconditions
                    if not evaluate(item, observation, before_context).satisfied
                ),
                None,
            )
            if failed_precondition is not None:
                return await self._hard(
                    step,
                    observation,
                    failed_precondition.explanation,
                    "precondition was not satisfied",
                    "precondition",
                    options,
                    head,
                )
            semantics = self._target_semantics(step)
            decision = self.policy.evaluate(
                step.action,
                semantics,
                PolicyContext(
                    phase="replay",
                    url=observation.url or capability.target.entry_point,
                    budget=Budget(steps=index),
                    capability_status=capability.status,
                    step_recorded_risk=step.risk,
                    allow_irreversible=options.allow_irreversible,
                ),
            )
            head = await self.journal.append(
                PolicyDecisionEvent(
                    observation_hash=observation.hash,
                    allowed=decision.verdict == "allow",
                    verdict=decision.verdict,
                    rule=decision.rule_id,
                    reason=decision.reason,
                )
            )
            if decision.verdict != "allow":
                return await self._hard(
                    step,
                    observation,
                    "policy allow",
                    f"{decision.rule_id}: {decision.reason}",
                    "policy",
                    options,
                    head,
                )
            resolution = await self.surface.resolve(step.target) if step.target else None
            if resolution is not None:
                head = await self.journal.append(
                    LocatorResolved(
                        observation_hash=observation.hash,
                        step_id=step.step_id,
                        winning_index=resolution.index,
                        attempts=resolution.attempts,
                    )
                )
            if resolution is not None and resolution.target is None:
                misses = "; ".join(attempt.outcome for attempt in resolution.attempts)
                return await self._hard(
                    step, observation, "one locator candidate", misses, "locator", options, head
                )
            if resolution is not None and resolution.degraded:
                head = await self.journal.append(
                    DriftObserved(
                        observation_hash=observation.hash,
                        expected_hash=step.provenance.observation_hash or observation.hash,
                        detail=f"Resolved candidate {resolution.index} after higher-ranked misses",
                        degradation_delta=resolution.degradation_delta,
                    )
                )
            head = await self.journal.append(
                ActionAttempted(
                    observation_hash=observation.hash,
                    step_id=step.step_id,
                    intent=step.intent,
                    action=step.action,
                    target=None,
                    locator_ladder=step.target,
                )
            )
            try:
                action_result = await self.surface.act(
                    step.action, resolution.target if resolution else None, timing=step.timing
                )
            except TimeoutError:
                return await self._hard(
                    step,
                    observation,
                    "surface settles within the recorded timeout",
                    "surface timeout",
                    "timeout",
                    options,
                    head,
                )
            except Exception:
                return await self._hard(
                    step,
                    observation,
                    "surface action completes",
                    "surface adapter error; inspect evidence",
                    "unknown",
                    options,
                    head,
                )
            head = await self.journal.append(
                ActionResultEvent(
                    observation_hash=action_result.after_hash,
                    step_id=step.step_id,
                    result=action_result,
                )
            )
            observation = await self.surface.observe()
            if isinstance(step.action, ReadValue) and action_result.value is not None:
                outputs[step.action.output_name] = action_result.value
            context = EvaluationContext(
                parameters=parameters,
                outputs=tuple(
                    Binding(name=name, value=_literal(value)) for name, value in outputs.items()
                ),
            )
            outcome = next(
                (
                    item
                    for item in capability.outcomes
                    if evaluate(item.detect, observation, context).satisfied
                ),
                None,
            )
            if outcome is not None:
                head = await self.journal.append(
                    OutcomeDetected(
                        observation_hash=observation.hash,
                        code=outcome.code,
                        classification=outcome.classification,
                    )
                )
                if outcome.classification == "business":
                    return BusinessOutcome(
                        code=outcome.code,
                        caller_message=outcome.caller_message,
                        detail=evaluate(outcome.detect, observation, context).explanation,
                        partial_outputs=outputs,
                        evidence_ref=await self._evidence(observation),
                    )
                if outcome.classification == "hard":
                    return await self._hard(
                        step,
                        observation,
                        outcome.caller_message,
                        outcome.code,
                        "server",
                        options,
                        head,
                    )
                recovery = outcome.recovery
                if recovery is not None and recovery.kind == "dismiss":
                    node = next(
                        (
                            item
                            for item in walk_ax(observation.ax_root)
                            if item.role == "button" and item.name == "Dismiss notice"
                        ),
                        None,
                    )
                    if node is not None:
                        reference = await self._evidence(observation)
                        ladder = LocatorLadder(
                            candidates=(
                                RoleNameCandidate(
                                    value=RoleNameValue(
                                        role="button", name_matcher=NameMatcher(value=node.name)
                                    ),
                                    frame_path=node.frame_path,
                                    confidence=1,
                                    source="ax_tree",
                                    observed_at=observation.captured_at,
                                    evidence_ref=reference,
                                    uniqueness_at_record=1,
                                    rationale=(
                                        "Named recovery control observed with the matched dialog"
                                    ),
                                ),
                            )
                        )
                        recovery_target = await self.surface.resolve(ladder)
                        if recovery_target.target is not None:
                            await self.surface.act(
                                Dismiss(), recovery_target.target, timing=step.timing
                            )
                            observation = await self.surface.observe()
                            head = await self.journal.append(
                                RecoveredEvent(
                                    observation_hash=observation.hash,
                                    step_id=step.step_id,
                                    outcome_code=outcome.code,
                                    recovery_kind="dismiss",
                                    attempt=1,
                                )
                            )
                        else:
                            return await self._hard(
                                step,
                                observation,
                                "dismiss recovery control",
                                "recovery locator exhausted",
                                "locator",
                                options,
                                head,
                            )
                    else:
                        return await self._hard(
                            step,
                            observation,
                            "Dismiss notice button",
                            "recovery control absent",
                            "locator",
                            options,
                            head,
                        )
                elif recovery is not None and recovery.kind == "reauthenticate":
                    controls = {
                        item.name: item
                        for item in walk_ax(observation.ax_root)
                        if (item.role, item.name)
                        in {("textbox", "Username"), ("button", "Sign in")}
                    }
                    if set(controls) != {"Username", "Sign in"}:
                        return await self._hard(
                            step,
                            observation,
                            "login controls for bounded reauthentication",
                            "required controls absent",
                            "session",
                            options,
                            head,
                        )
                    for action, name in (
                        (TypeText(value_ref=SecretRef(name="username")), "Username"),
                        (Click(), "Sign in"),
                    ):
                        node = controls[name]
                        reference = await self._evidence(observation)
                        ladder = LocatorLadder(
                            candidates=(
                                RoleNameCandidate(
                                    value=RoleNameValue(
                                        role=node.role, name_matcher=NameMatcher(value=node.name)
                                    ),
                                    frame_path=node.frame_path,
                                    confidence=1,
                                    source="ax_tree",
                                    observed_at=observation.captured_at,
                                    evidence_ref=reference,
                                    uniqueness_at_record=1,
                                    rationale="Observed login control for named reauthentication",
                                ),
                            )
                        )
                        resolved = await self.surface.resolve(ladder)
                        if resolved.target is None:
                            return await self._hard(
                                step,
                                observation,
                                "unique login recovery control",
                                f"could not resolve {name}",
                                "session",
                                options,
                                head,
                            )
                        await self.surface.act(action, resolved.target, timing=step.timing)
                        observation = await self.surface.observe()
                    head = await self.journal.append(
                        RecoveredEvent(
                            observation_hash=observation.hash,
                            step_id=step.step_id,
                            outcome_code=outcome.code,
                            recovery_kind="reauthenticate",
                            attempt=1,
                        )
                    )
                else:
                    return await self._hard(
                        step,
                        observation,
                        "bounded recovery execution",
                        f"unsupported recovery {recovery.kind if recovery else 'missing'}",
                        "session",
                        options,
                        head,
                    )
            if step.checkpoint is not None:
                checkpoint = evaluate(step.checkpoint, observation, context)
                if not checkpoint.satisfied:
                    return await self._hard(
                        step,
                        observation,
                        checkpoint.explanation,
                        "checkpoint was not satisfied",
                        "checkpoint",
                        options,
                        head,
                    )
        elapsed = self.clock.now() - started
        return Success(
            outputs=outputs,
            evidence_ref=await self._evidence(observation),
            steps_executed=len(capability.steps) - start,
            duration_ms=max(0, int(elapsed.total_seconds() * 1000)),
        )

    @staticmethod
    def _target_semantics(step: Step) -> TargetSemantics | None:
        if step.target is None:
            return None
        candidate = step.target.candidates[0]
        matcher = getattr(candidate.value, "name_matcher", None)
        return TargetSemantics(
            role=getattr(candidate.value, "role", None),
            name=matcher.value if matcher is not None else None,
            frame_path=candidate.frame_path,
        )


def handoff_checkpoint_satisfied(
    capability: Capability,
    failed_step_id: str,
    observation: Observation,
    inputs: tuple[ReplayInput, ...],
) -> bool:
    """Re-check recorded state after handback; never resume from the operator's assertion alone."""
    step = next((item for item in capability.steps if item.step_id == failed_step_id), None)
    if step is None or step.checkpoint is None:
        return False
    context = EvaluationContext(
        parameters=tuple(Binding(name=item.name, value=item.value) for item in inputs)
    )
    return evaluate(step.checkpoint, observation, context).satisfied
