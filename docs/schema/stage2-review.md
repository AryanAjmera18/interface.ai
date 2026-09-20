# Stage 2 implementation review

Complete requested sources, additive diffs, strict-schema tests, golden artifact, and captured verification output. The diffs are additions relative to the Stage 1 scaffold; they do not invent an intermediate pre-patch implementation.

## src/cua/domain/capability.py

```python
"""The compiler derives artifacts deterministically from the run journal, not model emission.

Artifacts nevertheless remain strict-representable so both providers and LLM-assisted editing
share one wire format. Typed entry lists resolve open-map keys (as in protobuf map encoding);
provider-specific shadow DTOs were rejected because they would diverge in semantics and review.
This module is pure: no I/O, frameworks, or imports from another cua package.
"""

import re
from typing import Annotated, Literal, Self

from pydantic import Field, field_validator, model_validator

from cua.domain.actions import ReadValue, SelectOption, TypeText
from cua.domain.common import (
    ULID,
    DomainModel,
    JsonType,
    LiteralValue,
    NonEmpty,
    ParamRef,
    SecretRef,
    Sensitivity,
    SurfaceKind,
)
from cua.domain.observation import SurfaceFingerprint
from cua.domain.predicates import AxTarget, Predicate
from cua.domain.provenance import CapabilityProvenance, ModelRef, assert_step_attributable
from cua.domain.steps import Step


class RegexValidation(DomainModel):
    kind: Literal["regex"] = "regex"
    pattern: str

    @model_validator(mode="after")
    def valid_regex(self) -> Self:
        try:
            re.compile(self.pattern)
        except re.error as exc:
            raise ValueError("Invalid parameter regex") from exc
        return self


class RangeValidation(DomainModel):
    kind: Literal["range"] = "range"
    minimum: float | None
    maximum: float | None

    @model_validator(mode="after")
    def ordered_range(self) -> Self:
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("minimum must not exceed maximum")
        return self


class EnumValidation(DomainModel):
    kind: Literal["enum"] = "enum"
    values: tuple[LiteralValue, ...] = Field(min_length=1)


Validation = Annotated[
    RegexValidation | RangeValidation | EnumValidation, Field(discriminator="kind")
]


class ParamSpec(DomainModel):
    """UI form parameters are scalars; nested input structure calls for capability decomposition.

    Explicit tagged literals distinguish absent defaults from a null default. Examples must be
    synthetic by authoring policy; a validator cannot determine whether arbitrary text is real PII.
    Secret defaults/examples are prohibited instead of trusting a 'synthetic' checkbox.
    """

    name: NonEmpty
    json_type: Literal["string", "number", "integer", "boolean", "null"]
    required: bool
    default: LiteralValue | None
    example: LiteralValue | None
    sensitivity: Sensitivity
    validation: Validation | None
    description: NonEmpty

    @model_validator(mode="after")
    def literal_types(self) -> Self:
        if self.sensitivity == "secret" and (self.default is not None or self.example is not None):
            raise ValueError(f"Secret parameter {self.name} cannot embed defaults or examples")
        if isinstance(self.validation, RegexValidation) and self.json_type != "string":
            raise ValueError("Regex validation requires a string parameter")
        if isinstance(self.validation, RangeValidation) and self.json_type not in {
            "number",
            "integer",
        }:
            raise ValueError("Range validation requires a numeric parameter")
        enum_values = self.validation.values if isinstance(self.validation, EnumValidation) else ()
        for literal in (self.default, self.example, *enum_values):
            if literal is None:
                continue
            expected = "number" if self.json_type == "integer" else self.json_type
            if literal.kind != expected or (
                self.json_type == "integer"
                and literal.kind == "number"
                and not literal.value.is_integer()
            ):
                raise ValueError(
                    f"Parameter {self.name}: literal kind must agree with {self.json_type}"
                )
            rule = self.validation
            value = literal.value
            if isinstance(rule, RegexValidation) and (
                not isinstance(value, str) or re.search(rule.pattern, value) is None
            ):
                raise ValueError(f"Parameter {self.name}: literal violates regex")
            if isinstance(rule, RangeValidation) and (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or (rule.minimum is not None and float(value) < rule.minimum)
                or (rule.maximum is not None and float(value) > rule.maximum)
            ):
                raise ValueError(f"Parameter {self.name}: literal violates range")
            if isinstance(rule, EnumValidation) and literal not in rule.values:
                raise ValueError(f"Parameter {self.name}: literal violates enum")
        return self


class ExtractorSpec(DomainModel):
    """Extraction is an AX attribute plus optional regex, never executable recorded code."""

    target: AxTarget
    attribute: Literal["value", "name", "description"]
    regex: str | None = None

    @field_validator("regex")
    @classmethod
    def valid_regex(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                re.compile(value)
            except re.error as exc:
                raise ValueError("Invalid extraction regex") from exc
        return value


class OutputSpec(DomainModel):
    name: NonEmpty
    json_type: JsonType
    produced_by_step_id: NonEmpty
    extractor: ExtractorSpec
    nullable: bool
    sensitivity: Sensitivity
    description: NonEmpty


class RecoveryAction(DomainModel):
    """Bounded named recovery only; arbitrary scripts and unbounded retry loops were rejected."""

    kind: Literal["retry", "refresh", "reauthenticate", "dismiss"]
    max_attempts: int = Field(ge=1, le=10)


class OutcomeSpec(DomainModel):
    code: NonEmpty
    detect: Predicate
    classification: Literal["business", "recoverable", "hard"]
    caller_message: NonEmpty
    recovery: RecoveryAction | None

    @model_validator(mode="after")
    def recovery_contract(self) -> Self:
        if self.classification == "recoverable" and self.recovery is None:
            raise ValueError(
                f"Outcome {self.code}: recoverable requires an explicit recovery action"
            )
        if self.classification != "recoverable" and self.recovery is not None:
            raise ValueError(
                f"Outcome {self.code}: {self.classification} forbids recovery; "
                "business is an answer, not a crash"
            )
        return self


class CapabilityTarget(DomainModel):
    surface_kind: SurfaceKind
    app_id: NonEmpty
    entry_point: NonEmpty
    tenant_id: str | None
    allowlist_ref: NonEmpty
    recorded_fingerprint: SurfaceFingerprint


class UndeclaredOutcomeError(ValueError):
    """An edge names no declared outcome; fix the code or declare its detection semantics."""


class Capability(DomainModel):
    schema_version: Literal[1] = 1
    capability_id: ULID
    name: NonEmpty
    description: NonEmpty
    version: str = Field(
        pattern=(
            r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
            r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
        )
    )
    status: Literal["draft", "candidate", "approved"]
    target: CapabilityTarget
    inputs: tuple[ParamSpec, ...]
    outputs: tuple[OutputSpec, ...]
    steps: tuple[Step, ...] = Field(min_length=1)
    outcomes: tuple[OutcomeSpec, ...]
    provenance: CapabilityProvenance

    @field_validator("version")
    @classmethod
    def valid_semver_prerelease(cls, value: str) -> str:
        """SemVer forbids leading zeroes in numeric prerelease IDs, unlike build metadata."""
        core = value.split("+", 1)[0]
        if "-" in core:
            for identifier in core.split("-", 1)[1].split("."):
                if identifier.isdigit() and len(identifier) > 1 and identifier.startswith("0"):
                    raise ValueError(
                        "SemVer numeric prerelease identifiers cannot have leading zeroes"
                    )
        return value

    @model_validator(mode="after")
    def coherent_artifact(self) -> Self:
        self.assert_attributable()
        for label, names in (
            ("steps", [s.step_id for s in self.steps]),
            ("inputs", [p.name for p in self.inputs]),
            ("outputs", [o.name for o in self.outputs]),
            ("outcomes", [o.code for o in self.outcomes]),
        ):
            if len(names) != len(set(names)):
                raise ValueError(f"Duplicate {label}; names must be unique")
        if [step.ordinal for step in self.steps] != list(range(1, len(self.steps) + 1)):
            raise ValueError("Step ordinals must be contiguous, ordered, and start at 1")
        declared = {outcome.code: outcome for outcome in self.outcomes}
        inputs = {param.name: param for param in self.inputs}
        for step in self.steps:
            for entry in step.on_outcome:
                if entry.code not in declared:
                    raise UndeclaredOutcomeError(
                        f"Step {step.step_id}: outcome {entry.code!r} is undeclared; "
                        "add an OutcomeSpec or correct the code"
                    )
                expected = {"business": "return", "recoverable": "recover", "hard": "fail"}[
                    declared[entry.code].classification
                ]
                if entry.handling.kind != expected:
                    raise ValueError(
                        f"Step {step.step_id}: {entry.code} requires handling {expected}"
                    )
            if isinstance(step.action, (TypeText, SelectOption)):
                ref = step.action.value_ref
                if isinstance(ref, (ParamRef, SecretRef)):
                    if ref.name not in inputs:
                        raise ValueError(f"Step {step.step_id}: undeclared input {ref.name!r}")
                    if (inputs[ref.name].sensitivity == "secret") != isinstance(ref, SecretRef):
                        raise ValueError(
                            f"Step {step.step_id}: secret inputs require secret_ref; "
                            "other inputs require param_ref"
                        )
        for output in self.outputs:
            producer = next(
                (s for s in self.steps if s.step_id == output.produced_by_step_id), None
            )
            if (
                producer is None
                or not isinstance(producer.action, ReadValue)
                or producer.action.output_name != output.name
            ):
                raise ValueError(
                    f"Output {output.name}: produced_by_step_id must name its ReadValue step"
                )
        for step in self.steps:
            if isinstance(step.action, ReadValue) and not any(
                o.name == step.action.output_name and o.produced_by_step_id == step.step_id
                for o in self.outputs
            ):
                raise ValueError(f"Step {step.step_id}: ReadValue needs a matching OutputSpec")
        if self.status == "approved" and self.provenance.approval is None:
            raise ValueError("Approved artifacts require a named ApprovalRecord")
        fp = self.target.recorded_fingerprint
        if fp.app_id != self.target.app_id or (
            self.target.tenant_id is not None and fp.tenant_id != self.target.tenant_id
        ):
            raise ValueError("Capability target disagrees with its recorded fingerprint")
        return self

    def assert_attributable(self) -> None:
        for step in self.steps:
            assert_step_attributable(step.step_id, step.provenance)
            if step.provenance.discovery_run_id != self.provenance.derived_from_run_id:
                raise ValueError(
                    f"Step {step.step_id}: discovery run differs from capability provenance"
                )
            if (
                isinstance(step.provenance.decided_by, ModelRef)
                and step.provenance.decided_by not in self.provenance.models_used
            ):
                raise ValueError(f"Step {step.step_id}: deciding model is absent from models_used")
```

## src/cua/domain/provenance.py

```python
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

    No signature or recursive self-hash is implied. The referenced review evidence names the
    exact pre-approval artifact; mutating status/approval creates a new document digest.
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
```

## src/cua/domain/predicates.py

```python
"""Checkpoints are DATA, not Python: replay must assert state without models or recorded code.

Serializable predicates are reviewable and diffable; eval, callables and embedded scripts
would grant recorded decisions executable authority. Unknown bindings and ambiguous targets
fail closed rather than asking a model to reinterpret the assertion.
"""

import re
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from cua.domain.common import DomainModel, LiteralRef, LiteralValue, NonEmpty, ValueRef
from cua.domain.names import NameMatcher
from cua.domain.observation import AxNode, AxState, Observation, walk_ax


class AxTarget(DomainModel):
    role: NonEmpty
    name_matcher: NameMatcher
    frame_path: tuple[str, ...] | None = None
    node_path: tuple[int, ...] | None = None

    def select(self, observation: Observation) -> tuple[AxNode, ...]:
        return tuple(
            node
            for node in walk_ax(observation.ax_root)
            if node.role == self.role
            and self.name_matcher.matches(node.name)
            and (self.frame_path is None or node.frame_path == self.frame_path)
            and (self.node_path is None or node.node_path == self.node_path)
        )


class AxNodeExists(DomainModel):
    kind: Literal["ax_node_exists"] = "ax_node_exists"
    role: NonEmpty
    name_matcher: NameMatcher
    frame_path: tuple[str, ...] | None = None
    min_count: int = Field(default=1, ge=0)
    max_count: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def ordered_counts(self) -> Self:
        if self.max_count is not None and self.max_count < self.min_count:
            raise ValueError("max_count must be at least min_count")
        return self


class TextScope(DomainModel):
    """Whole AX text or a named subtree, not CSS/XPath with backend-specific semantics."""

    target: AxTarget | None = None
    frame_path: tuple[str, ...] | None = None


class RegexPredicate(DomainModel):
    regex: str

    @model_validator(mode="after")
    def valid_regex(self) -> Self:
        try:
            re.compile(self.regex)
        except re.error as exc:
            raise ValueError("Invalid predicate regex") from exc
        return self


class TextMatches(RegexPredicate):
    kind: Literal["text_matches"] = "text_matches"
    scope: TextScope


class UrlMatches(DomainModel):
    kind: Literal["url_matches"] = "url_matches"
    pattern: str

    @model_validator(mode="after")
    def valid_pattern(self) -> Self:
        try:
            re.compile(self.pattern)
        except re.error as exc:
            raise ValueError("Invalid URL regex") from exc
        return self


class FieldValueEquals(DomainModel):
    kind: Literal["field_value_equals"] = "field_value_equals"
    target: AxTarget
    value_ref: ValueRef


class ElementState(DomainModel):
    kind: Literal["element_state"] = "element_state"
    target: AxTarget
    state: AxState
    expected: bool


class ExtractMatches(RegexPredicate):
    kind: Literal["extract_matches"] = "extract_matches"
    output_name: NonEmpty


class AllOf(DomainModel):
    kind: Literal["all_of"] = "all_of"
    predicates: tuple["Predicate", ...] = Field(min_length=1)


class AnyOf(DomainModel):
    kind: Literal["any_of"] = "any_of"
    predicates: tuple["Predicate", ...] = Field(min_length=1)


class NoneOf(DomainModel):
    kind: Literal["none_of"] = "none_of"
    predicates: tuple["Predicate", ...] = Field(min_length=1)


Predicate = Annotated[
    AxNodeExists
    | TextMatches
    | UrlMatches
    | FieldValueEquals
    | ElementState
    | ExtractMatches
    | AllOf
    | AnyOf
    | NoneOf,
    Field(discriminator="kind"),
]


class PredicateResult(DomainModel):
    satisfied: bool
    explanation: str
    sub_results: tuple["PredicateResult", ...] = ()


class Binding(DomainModel):
    name: NonEmpty
    value: LiteralValue


class EvaluationContext(DomainModel):
    """Explicit runtime bindings avoid hiding parameters or extraction state in observations.

    Secret resolution is intentionally excluded; comparing a raw secret in a checkpoint would
    turn observations into a secret store. Missing bindings return an unsatisfied assertion.
    """

    parameters: tuple[Binding, ...] = ()
    outputs: tuple[Binding, ...] = ()


def evaluate(
    predicate: Predicate, observation: Observation, context: EvaluationContext | None = None
) -> PredicateResult:
    context = context or EvaluationContext()
    if isinstance(predicate, (AllOf, AnyOf, NoneOf)):
        children = tuple(evaluate(child, observation, context) for child in predicate.predicates)
        flags = [child.satisfied for child in children]
        satisfied = (
            all(flags)
            if isinstance(predicate, AllOf)
            else (any(flags) if isinstance(predicate, AnyOf) else not any(flags))
        )
        words = {
            "all_of": "All assertions",
            "any_of": "At least one assertion",
            "none_of": "No assertion",
        }
        return PredicateResult(
            satisfied=satisfied,
            sub_results=children,
            explanation=words[predicate.kind]
            + " must hold: "
            + "; ".join(child.explanation for child in children),
        )
    if isinstance(predicate, AxNodeExists):
        target = AxTarget(
            role=predicate.role,
            name_matcher=predicate.name_matcher,
            frame_path=predicate.frame_path,
        )
        count = len(target.select(observation))
        upper = predicate.max_count if predicate.max_count is not None else "unbounded"
        return PredicateResult(
            satisfied=count >= predicate.min_count
            and (predicate.max_count is None or count <= predicate.max_count),
            explanation=f"Expected {predicate.min_count}..{upper} "
            f"{predicate.role} nodes with {predicate.name_matcher.describe()} "
            f"in frame {predicate.frame_path}; matched {count}.",
        )
    if isinstance(predicate, TextMatches):
        roots = (
            predicate.scope.target.select(observation)
            if predicate.scope.target
            else (observation.ax_root,)
        )
        text_nodes = [
            node
            for root in roots
            for node in walk_ax(root)
            if predicate.scope.frame_path is None or node.frame_path == predicate.scope.frame_path
        ]
        text = "\n".join(
            part for node in text_nodes for part in (node.name, node.value or "", node.description)
        )
        return PredicateResult(
            satisfied=bool(text_nodes) and re.search(predicate.regex, text) is not None,
            explanation=f"Expected AX text in {predicate.scope} to match "
            f"regex {predicate.regex!r}.",
        )
    if isinstance(predicate, UrlMatches):
        return PredicateResult(
            satisfied=observation.url is not None
            and re.search(predicate.pattern, observation.url) is not None,
            explanation=f"Expected surface URL to match regex {predicate.pattern!r}.",
        )
    if isinstance(predicate, ExtractMatches):
        binding = next(
            (item for item in context.outputs if item.name == predicate.output_name), None
        )
        return PredicateResult(
            satisfied=binding is not None
            and re.search(predicate.regex, str(binding.value.value)) is not None,
            explanation=f"Expected extracted output {predicate.output_name!r} to match "
            f"regex {predicate.regex!r}; missing outputs fail.",
        )
    nodes = predicate.target.select(observation)
    expected = f"one {predicate.target.role} with {predicate.target.name_matcher.describe()}"
    if isinstance(predicate, ElementState):
        return PredicateResult(
            satisfied=len(nodes) == 1
            and ((predicate.state in nodes[0].states) == predicate.expected),
            explanation=f"Expected {expected} with {predicate.state}={predicate.expected}.",
        )
    ref = predicate.value_ref
    literal = (
        ref.value
        if isinstance(ref, LiteralRef)
        else next(
            (
                item.value
                for item in context.parameters
                if ref.kind == "param_ref" and item.name == ref.name
            ),
            None,
        )
    )
    return PredicateResult(
        satisfied=len(nodes) == 1 and literal is not None and nodes[0].value == literal.value,
        explanation=f"Expected {expected} whose value equals the {ref.kind} "
        "binding; unavailable bindings fail (values withheld).",
    )
```

## src/cua/domain/locators.py

```python
"""Rank evidence-backed locator alternatives; forbid I/O, frameworks, and other cua packages."""

from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, Field, model_validator

from cua.domain.common import DomainModel, EvidenceRef, NonEmpty
from cua.domain.names import NameMatcher
from cua.domain.predicates import AxTarget


class RoleNameValue(DomainModel):
    role: NonEmpty
    name_matcher: NameMatcher


class ScopedRoleNameValue(RoleNameValue):
    ancestor: AxTarget


class PathValue(DomainModel):
    path: tuple[int, ...]


class TextValue(DomainModel):
    matcher: NameMatcher


class CssValue(DomainModel):
    selector: NonEmpty


class CoordinatesValue(DomainModel):
    x: float
    y: float


class CandidateBase(DomainModel):
    frame_path: tuple[str, ...]
    confidence: float = Field(ge=0, le=1)
    source: Literal["ax_tree", "dom", "model", "human"]
    observed_at: AwareDatetime
    evidence_ref: EvidenceRef
    uniqueness_at_record: int = Field(ge=0)
    rationale: NonEmpty


class RoleNameCandidate(CandidateBase):
    strategy: Literal["ax_role_name"] = "ax_role_name"
    value: RoleNameValue


class ScopedRoleNameCandidate(CandidateBase):
    strategy: Literal["ax_role_name_scoped"] = "ax_role_name_scoped"
    value: ScopedRoleNameValue


class AxPathCandidate(CandidateBase):
    strategy: Literal["ax_path"] = "ax_path"
    value: PathValue


class LabelCandidate(CandidateBase):
    strategy: Literal["label_text"] = "label_text"
    value: TextValue


class VisibleTextCandidate(CandidateBase):
    strategy: Literal["visible_text"] = "visible_text"
    value: TextValue


class StructuralCandidate(CandidateBase):
    strategy: Literal["structural_path"] = "structural_path"
    value: PathValue


class CssCandidate(CandidateBase):
    strategy: Literal["frame_scoped_css"] = "frame_scoped_css"
    value: CssValue


class CoordinatesCandidate(CandidateBase):
    strategy: Literal["coordinates"] = "coordinates"
    value: CoordinatesValue


LocatorCandidate = Annotated[
    RoleNameCandidate
    | ScopedRoleNameCandidate
    | AxPathCandidate
    | LabelCandidate
    | VisibleTextCandidate
    | StructuralCandidate
    | CssCandidate
    | CoordinatesCandidate,
    Field(discriminator="strategy"),
]


def stability_key(candidate: LocatorCandidate) -> tuple[int, int]:
    """Prefer semantics, then paths/CSS, then pixels; unique matches win within a strategy.

    Confidence/source/model ordering are excluded: a model's confidence is not stability
    evidence. Ties retain recorded order because neither tied candidate outranks the other.
    """
    rank = {
        "ax_role_name_scoped": 8,
        "ax_role_name": 7,
        "label_text": 6,
        "visible_text": 5,
        "ax_path": 4,
        "structural_path": 3,
        "frame_scoped_css": 2,
        "coordinates": 1,
    }
    uniqueness = (
        2
        if candidate.uniqueness_at_record == 1
        else (1 if candidate.uniqueness_at_record > 1 else 0)
    )
    return rank[candidate.strategy], uniqueness


class ScopeHints(DomainModel):
    ancestor: AxTarget | None = None
    description: str = ""


class LocatorLadder(DomainModel):
    candidates: tuple[LocatorCandidate, ...] = Field(min_length=1)
    match_policy: Literal["require_unique", "first_match"] = "require_unique"
    scope_hints: ScopeHints = Field(default_factory=ScopeHints)

    @model_validator(mode="after")
    def evidence_and_order(self) -> Self:
        if self.candidates[0].strategy == "coordinates":
            raise ValueError(
                "Coordinates cannot be first or the sole locator; add an AX/DOM locator"
            )
        if not any(item.source in {"ax_tree", "dom"} for item in self.candidates):
            raise ValueError("A locator ladder requires at least one AX-tree or DOM observation")
        if list(self.candidates) != sorted(self.candidates, key=stability_key, reverse=True):
            raise ValueError("Sort candidates descending by stability_key(strategy, uniqueness)")
        return self
```

## src/cua/domain/steps.py

```python
"""Define reviewable replay steps; forbid I/O, frameworks, and other cua packages."""

from collections.abc import Mapping
from functools import cached_property
from types import MappingProxyType
from typing import Literal, Never, Self

from pydantic import Field, field_serializer, model_validator

from cua.domain.actions import Action, Click, Dismiss, ReadValue, SelectOption, TypeText
from cua.domain.common import DomainModel, NonEmpty
from cua.domain.locators import LocatorLadder
from cua.domain.predicates import Predicate
from cua.domain.provenance import StepProvenance


class RetryPolicy(DomainModel):
    max_attempts: int = Field(default=1, ge=1, le=10)
    delay_ms: int = Field(default=0, ge=0)


class StepTiming(DomainModel):
    settle_strategy: Literal["none", "ax_stable", "navigation"]
    timeout_ms: int = Field(gt=0)
    retry: RetryPolicy = Field(default_factory=RetryPolicy)


class StepOutcomeHandling(DomainModel):
    kind: Literal["return", "recover", "fail"]


class OutcomeHandlingEntry(DomainModel):
    code: NonEmpty
    handling: StepOutcomeHandling


class _OutcomeEntries(list[OutcomeHandlingEntry]):
    """Keep the requested list wire shape without allowing a stale cached mapping.

    A plain list inside a frozen Pydantic model is still mutable. Reject its mutators rather
    than rely on a convention that edits must reconstruct the Step.
    """

    def _deny(self, *args: object, **kwargs: object) -> Never:
        raise TypeError("Outcome entries are immutable; construct and validate a new Step")

    __setitem__ = _deny
    __delitem__ = _deny
    __iadd__ = _deny
    __imul__ = _deny
    append = _deny
    clear = _deny
    extend = _deny
    insert = _deny
    pop = _deny
    remove = _deny
    reverse = _deny
    sort = _deny


class Step(DomainModel):
    """Outcome codes use entries on the wire, never arbitrary JSON object keys.

    The cached mapping is read-only and never serialized. The list is frozen after validation;
    edits must be revalidated as new Steps so neither the mapping nor invariants become stale.
    """

    step_id: NonEmpty
    ordinal: int = Field(ge=1)
    intent: NonEmpty
    action: Action
    target: LocatorLadder | None
    preconditions: tuple[Predicate, ...] = ()
    checkpoint: Predicate | None
    risk: Literal["safe", "reversible", "irreversible"]
    timing: StepTiming
    on_outcome: list[OutcomeHandlingEntry] = Field(default_factory=list)
    provenance: StepProvenance

    @cached_property
    def outcome_map(self) -> Mapping[str, StepOutcomeHandling]:
        return MappingProxyType({entry.code: entry.handling for entry in self.on_outcome})

    @field_serializer("on_outcome")
    def sorted_outcomes(self, entries: list[OutcomeHandlingEntry]) -> list[OutcomeHandlingEntry]:
        return sorted(entries, key=lambda entry: entry.code)

    @model_validator(mode="after")
    def valid_step(self) -> Self:
        codes = [entry.code for entry in self.on_outcome]
        if len(codes) != len(set(codes)):
            raise ValueError(f"Step {self.step_id}: on_outcome codes must be unique")
        if (
            isinstance(self.action, (Click, TypeText, SelectOption, ReadValue, Dismiss))
            and not self.target
        ):
            raise ValueError(
                f"Step {self.step_id}: {self.action.kind} requires a target locator ladder"
            )
        if self.risk == "irreversible" and (
            self.checkpoint is None or self.timing.retry.max_attempts != 1
        ):
            raise ValueError(
                "Irreversible steps need a checkpoint and exactly one attempt; recovery is explicit"
            )
        object.__setattr__(self, "on_outcome", _OutcomeEntries(self.on_outcome))
        return self
```

## tests/unit/domain/strict_schema.py

```python
"""Independently check the supported schema subset; forbid live provider calls."""

import re
from typing import Any

from cua.domain.schemas import SchemaDocument


def assert_openai_strict(schema: SchemaDocument) -> None:
    """Guard closed objects, full required sets and tagged OBJECT unions, allowing nullability.

    Nullable scalar/object alternatives are not polymorphic variants. Multi-object unions must
    share a required singleton-enum tag with disjoint values. References are resolved for this
    check but definitions are traversed once, so recursive predicates do not recurse forever.
    """
    root = schema.schema_body
    assert isinstance(root, dict)
    assert root.get("type") == "object", "Strict tool inputs require an object root"

    def resolve(node: Any) -> Any:
        if isinstance(node, dict) and "$ref" in node:
            assert node["$ref"].startswith("#/$defs/")
            return root["$defs"][node["$ref"].split("/")[-1]]
        return node

    def visit(node: Any) -> None:
        if isinstance(node, list):
            for value in node:
                visit(value)
        elif isinstance(node, dict):
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False, "Open-keyed object"
                assert set(node.get("required", ())) == set(node.get("properties", ())), (
                    "Missing required property"
                )
            assert "oneOf" not in node and "discriminator" not in node
            if "anyOf" in node:
                variants = [resolve(value) for value in node["anyOf"]]
                objects = [value for value in variants if value.get("type") == "object"]
                if len(objects) > 1:
                    possible = set(objects[0]["properties"])
                    for variant in objects:
                        possible &= {
                            key
                            for key, prop in variant["properties"].items()
                            if len(prop.get("enum", ())) == 1
                        }
                    assert any(
                        len({variant["properties"][key]["enum"][0] for variant in objects})
                        == len(objects)
                        for key in possible
                    ), "Untagged object union"
            for value in node.values():
                visit(value)

    visit(root)


def validate_instance(instance: Any, document: SchemaDocument) -> None:
    """Independent validator for the emitted subset, not a replacement for Pydantic invariants.

    Provider grammar constraints cannot express journal attribution or cross-field references;
    both provider round trips must additionally pass the SAME domain validation. No API is called.
    """
    root = document.schema_body
    assert isinstance(root, dict)

    def validate(value: Any, schema: Any) -> None:
        if "$ref" in schema:
            validate(value, root["$defs"][schema["$ref"].split("/")[-1]])
            return
        if "anyOf" in schema:
            errors = []
            for branch in schema["anyOf"]:
                try:
                    validate(value, branch)
                    return
                except AssertionError as exc:
                    errors.append(exc)
            raise AssertionError(f"No union variant matched ({len(errors)} variants)")
        if "enum" in schema:
            assert value in schema["enum"]
        kind = schema.get("type")
        if kind == "object":
            assert isinstance(value, dict)
            assert set(schema["required"]) <= set(value)
            assert set(value) <= set(schema["properties"])
            for key, field in value.items():
                validate(field, schema["properties"][key])
        elif kind == "array":
            assert isinstance(value, list)
            assert len(value) >= schema.get("minItems", 0)
            assert len(value) <= schema.get("maxItems", len(value))
            for item in value:
                validate(item, schema["items"])
        elif kind == "string":
            assert isinstance(value, str)
            assert len(value) >= schema.get("minLength", 0)
            assert "pattern" not in schema or re.search(schema["pattern"], value)
        elif kind in {"number", "integer"}:
            assert type(value) in {int, float}
            assert kind != "integer" or int(value) == value
            assert "minimum" not in schema or value >= schema["minimum"]
            assert "maximum" not in schema or value <= schema["maximum"]
            assert "exclusiveMinimum" not in schema or value > schema["exclusiveMinimum"]
        elif kind == "boolean":
            assert type(value) is bool
        elif kind == "null":
            assert value is None

    validate(instance, root)
```

## tests/unit/domain/test_schemas.py

```python
"""Guard provider schema parity, strictness and golden versions; forbid live provider calls."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError
from typer.testing import CliRunner

from cua.cli.__main__ import app, load_model_registry
from cua.domain.actions import Action, ActionInput
from cua.domain.capability import Capability
from cua.domain.models import ModelRole, fake_registry
from cua.domain.schemas import (
    SchemaDocument,
    artifact_json,
    provider_tool,
    public_schemas,
    strict_schema,
)
from tests.unit.domain.samples import FixedClock, SequenceIds, capability
from tests.unit.domain.strict_schema import assert_openai_strict, validate_instance
from tests.unit.domain.test_contracts import model_samples

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    "document",
    [doc for doc in public_schemas() if doc.name != "replay-result.v1"],
    ids=lambda doc: doc.name,
)
def test_every_tool_input_schema_is_strict(document: SchemaDocument) -> None:
    assert_openai_strict(document)


@pytest.mark.parametrize("mutation", ["open", "optional", "tag"])
def test_strictness_guard_rejects_regressions(mutation: str) -> None:
    document = strict_schema(ActionInput, "action.v1")
    body = json.loads(document.text())
    if mutation == "open":
        body["$defs"]["Click"]["additionalProperties"] = {"type": "string"}
    elif mutation == "optional":
        body["$defs"]["Click"]["required"] = []
    else:
        body["$defs"]["Click"]["properties"]["kind"] = {"type": "string"}
    with pytest.raises(AssertionError):
        assert_openai_strict(SchemaDocument(name="broken", schema_body=body))


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_provider_schema_roundtrip_identical_semantics(provider: str) -> None:
    cap = capability()
    examples: list[tuple[type[ActionInput] | type[Capability], Any]] = [(Capability, cap)]
    for sample in model_samples():
        try:
            action = TypeAdapter(Action).validate_json(sample.model_dump_json())
        except ValidationError:
            continue
        examples.append((ActionInput, ActionInput(action=action)))
    assert len(examples) >= 10
    for cls, example in examples:
        document = strict_schema(cls, "tool_input")
        wrapper = provider_tool(document, provider)
        wire = json.loads(wrapper.text())
        body = wire["function"]["parameters"] if provider == "openai" else wire["input_schema"]
        assert body == document.schema_body
        input_schema = SchemaDocument(name="roundtrip", schema_body=body)
        assert_openai_strict(input_schema)
        encoded = example.model_dump_json()
        validate_instance(json.loads(encoded), input_schema)
        parsed = cls.model_validate_json(encoded)
        assert parsed.model_dump_json() == encoded
        invalid = json.loads(encoded)
        invalid["unexpected"] = True
        with pytest.raises(AssertionError):
            validate_instance(invalid, input_schema)
        with pytest.raises(ValidationError):
            cls.model_validate(invalid)
    with pytest.raises(ValueError, match="provider"):
        provider_tool(document, "other")


def test_results_are_explicitly_outside_strict_boundary() -> None:
    document = next(doc for doc in public_schemas() if doc.name == "replay-result.v1")
    with pytest.raises(AssertionError):
        assert_openai_strict(document)
    body = json.loads(document.text())
    assert body["$defs"]["Success"]["properties"]["outputs"]["additionalProperties"] is not False


def test_golden_artifact_byte_stability() -> None:
    path = ROOT / "tests/golden/capability.v1.json"
    recorded = path.read_bytes()
    assert artifact_json(capability(FixedClock(), SequenceIds())).encode() == recorded
    assert artifact_json(Capability.model_validate_json(recorded)).encode() == recorded


def test_schema_emission_and_frozen_version(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["schema", "emit", "--output", str(tmp_path)])
    assert result.exit_code == 0, result.exception
    manifest = json.loads((ROOT / "tests/golden/schema-digests.v1.json").read_text())
    for document in public_schemas():
        name = document.name + ".json"
        emitted = (tmp_path / name).read_bytes()
        assert emitted == document.text().encode()
        assert emitted == (ROOT / "docs/schema" / name).read_bytes()
        assert hashlib.sha256(emitted).hexdigest() == manifest[name], (
            "Published v1 schema changed: introduce a new version instead of regenerating v1."
        )


def test_model_config_offline_defaults_and_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for role in ModelRole:
        monkeypatch.delenv(f"CUA_MODEL_{role.name}", raising=False)
    assert load_model_registry(ROOT / "config/models.yaml") == fake_registry()
    assert load_model_registry(tmp_path / "absent.yaml") == fake_registry()
    model = fake_registry().profiles[0].model.model_dump()
    model.update(provider="openai", model_id="explicit-test-model", api_flavor="responses")
    monkeypatch.setenv("CUA_MODEL_DISCOVERY_PLANNER", json.dumps(model))
    loaded = load_model_registry(ROOT / "config/models.yaml")
    assert loaded.get(ModelRole.DISCOVERY_PLANNER).model.provider == "openai"
    assert loaded.get(ModelRole.EXTRACTOR).model.provider == "fake"
    monkeypatch.setenv("CUA_MODEL_DISCOVERY_PLANNER", '{"provider":"unknown"}')
    with pytest.raises(ValidationError):
        load_model_registry(ROOT / "config/models.yaml")


def test_result_union_is_statically_exhaustive() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--strict",
            "src/cua",
            str(Path(__file__).with_name("result_exhaustiveness.py")),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
```

## tests/golden/capability.v1.json

```json
{
  "capability_id": "00000000000000000000000002",
  "description": "Synthetic one-step example: populate the member-search field.",
  "inputs": [
    {
      "default": null,
      "description": "Synthetic member identifier.",
      "example": {
        "kind": "string",
        "value": "10001"
      },
      "json_type": "string",
      "name": "member_id",
      "required": true,
      "sensitivity": "internal",
      "validation": null
    }
  ],
  "name": "enter_member_id",
  "outcomes": [
    {
      "caller_message": "No matching member exists.",
      "classification": "business",
      "code": "member_not_found",
      "detect": {
        "frame_path": null,
        "kind": "ax_node_exists",
        "max_count": null,
        "min_count": 1,
        "name_matcher": {
          "alternatives": [],
          "mode": "normalized",
          "value": "No matching member was found. This is a business outcome."
        },
        "role": "status"
      },
      "recovery": null
    }
  ],
  "outputs": [],
  "provenance": {
    "approval": null,
    "compiler_version": "1.0.0",
    "derived_from_run_id": "00000000000000000000000001",
    "lineage": [],
    "models_used": [
      {
        "api_flavor": "offline",
        "kind": "model",
        "model_id": "deterministic-v1",
        "provider": "fake",
        "structured_output_mode": "json_schema"
      }
    ],
    "parent_capability_id": null,
    "recorded_at": "2026-01-15T00:00:00Z",
    "recorder_version": "1.0.0",
    "tool_schema_hash": "d659f092cad4d14d6d7e05381ab35990ccbebd43aa19afcc64d34fbedb38c4a0"
  },
  "schema_version": 1,
  "status": "candidate",
  "steps": [
    {
      "action": {
        "kind": "type_text",
        "value_ref": {
          "kind": "param_ref",
          "name": "member_id"
        }
      },
      "checkpoint": {
        "kind": "field_value_equals",
        "target": {
          "frame_path": [
            "Content",
            "Member workspace"
          ],
          "name_matcher": {
            "alternatives": [],
            "mode": "normalized",
            "value": "Member ID"
          },
          "node_path": null,
          "role": "textbox"
        },
        "value_ref": {
          "kind": "param_ref",
          "name": "member_id"
        }
      },
      "intent": "Enter the caller's synthetic member ID.",
      "on_outcome": [
        {
          "code": "member_not_found",
          "handling": {
            "kind": "return"
          }
        }
      ],
      "ordinal": 1,
      "preconditions": [
        {
          "frame_path": [
            "Content",
            "Member workspace"
          ],
          "kind": "ax_node_exists",
          "max_count": 1,
          "min_count": 1,
          "name_matcher": {
            "alternatives": [],
            "mode": "normalized",
            "value": "Member ID"
          },
          "role": "textbox"
        }
      ],
      "provenance": {
        "compiler_rule_id": null,
        "created_at": "2026-01-15T00:00:00Z",
        "decided_by": {
          "api_flavor": "offline",
          "kind": "model",
          "model_id": "deterministic-v1",
          "provider": "fake",
          "structured_output_mode": "json_schema"
        },
        "decision_id": "00000000000000000000000003",
        "discovery_run_id": "00000000000000000000000001",
        "edits": [],
        "observation_hash": "6cd00c100e724bd0fca81fdc8b169d38fbe5ed49403d2d0b1be381d876a39aa0",
        "observation_id": "00000000000000000000000001",
        "prompt_hash": "ebb24e6420c6df769702a68173dbe69d464ec8516f343dd4fc3998324496f7c5",
        "prompt_template_id": "member-entry.v1",
        "rationale_digest": "7dbd08c7920f1d8706f24e8b000a217c4e9063799fcd985ec9741ed12e8e8e22",
        "tool_call_id": "fake-call-1"
      },
      "risk": "reversible",
      "step_id": "enter-member",
      "target": {
        "candidates": [
          {
            "confidence": 1.0,
            "evidence_ref": {
              "content_hash": "8b8111443c1cf9ff3b0cfbb42e9c35617c1ecc5795e6c4ace3f56db24f4ab4e9",
              "evidence_id": "00000000000000000000000001",
              "media_type": "application/json"
            },
            "frame_path": [
              "Content",
              "Member workspace"
            ],
            "observed_at": "2026-01-15T00:00:00Z",
            "rationale": "Unique named textbox in the recorded member workspace.",
            "source": "ax_tree",
            "strategy": "ax_role_name",
            "uniqueness_at_record": 1,
            "value": {
              "name_matcher": {
                "alternatives": [],
                "mode": "normalized",
                "value": "Member ID"
              },
              "role": "textbox"
            }
          }
        ],
        "match_policy": "require_unique",
        "scope_hints": {
          "ancestor": null,
          "description": ""
        }
      },
      "timing": {
        "retry": {
          "delay_ms": 0,
          "max_attempts": 1
        },
        "settle_strategy": "ax_stable",
        "timeout_ms": 2000
      }
    }
  ],
  "target": {
    "allowlist_ref": "local-demo.v1",
    "app_id": "cua-synthetic-bank",
    "entry_point": "http://127.0.0.1:8099/t/alpha/",
    "recorded_fingerprint": {
      "app_id": "cua-synthetic-bank",
      "app_version": "0.1.0",
      "config_hash": "adc957f5621d18bb12b0d08c8865d8d75085ee1eb32a74a9c77e65415e508160",
      "observed_at": "2026-01-15T00:00:00Z",
      "tenant_id": "alpha",
      "ui_revision": "legacy-frames-1"
    },
    "surface_kind": "web",
    "tenant_id": "alpha"
  },
  "version": "1.0.0"
}
```

## Additive diffs: capability.py and steps.py

```diff
diff --git a/src/cua/domain/capability.py b/src/cua/domain/capability.py
new file mode 100644
--- /dev/null
+++ b/src/cua/domain/capability.py
@@ -0,0 +1,304 @@
+"""The compiler derives artifacts deterministically from the run journal, not model emission.
+
+Artifacts nevertheless remain strict-representable so both providers and LLM-assisted editing
+share one wire format. Typed entry lists resolve open-map keys (as in protobuf map encoding);
+provider-specific shadow DTOs were rejected because they would diverge in semantics and review.
+This module is pure: no I/O, frameworks, or imports from another cua package.
+"""
+
+import re
+from typing import Annotated, Literal, Self
+
+from pydantic import Field, field_validator, model_validator
+
+from cua.domain.actions import ReadValue, SelectOption, TypeText
+from cua.domain.common import (
+    ULID,
+    DomainModel,
+    JsonType,
+    LiteralValue,
+    NonEmpty,
+    ParamRef,
+    SecretRef,
+    Sensitivity,
+    SurfaceKind,
+)
+from cua.domain.observation import SurfaceFingerprint
+from cua.domain.predicates import AxTarget, Predicate
+from cua.domain.provenance import CapabilityProvenance, ModelRef, assert_step_attributable
+from cua.domain.steps import Step
+
+
+class RegexValidation(DomainModel):
+    kind: Literal["regex"] = "regex"
+    pattern: str
+
+    @model_validator(mode="after")
+    def valid_regex(self) -> Self:
+        try:
+            re.compile(self.pattern)
+        except re.error as exc:
+            raise ValueError("Invalid parameter regex") from exc
+        return self
+
+
+class RangeValidation(DomainModel):
+    kind: Literal["range"] = "range"
+    minimum: float | None
+    maximum: float | None
+
+    @model_validator(mode="after")
+    def ordered_range(self) -> Self:
+        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
+            raise ValueError("minimum must not exceed maximum")
+        return self
+
+
+class EnumValidation(DomainModel):
+    kind: Literal["enum"] = "enum"
+    values: tuple[LiteralValue, ...] = Field(min_length=1)
+
+
+Validation = Annotated[
+    RegexValidation | RangeValidation | EnumValidation, Field(discriminator="kind")
+]
+
+
+class ParamSpec(DomainModel):
+    """UI form parameters are scalars; nested input structure calls for capability decomposition.
+
+    Explicit tagged literals distinguish absent defaults from a null default. Examples must be
+    synthetic by authoring policy; a validator cannot determine whether arbitrary text is real PII.
+    Secret defaults/examples are prohibited instead of trusting a 'synthetic' checkbox.
+    """
+
+    name: NonEmpty
+    json_type: Literal["string", "number", "integer", "boolean", "null"]
+    required: bool
+    default: LiteralValue | None
+    example: LiteralValue | None
+    sensitivity: Sensitivity
+    validation: Validation | None
+    description: NonEmpty
+
+    @model_validator(mode="after")
+    def literal_types(self) -> Self:
+        if self.sensitivity == "secret" and (self.default is not None or self.example is not None):
+            raise ValueError(f"Secret parameter {self.name} cannot embed defaults or examples")
+        if isinstance(self.validation, RegexValidation) and self.json_type != "string":
+            raise ValueError("Regex validation requires a string parameter")
+        if isinstance(self.validation, RangeValidation) and self.json_type not in {
+            "number",
+            "integer",
+        }:
+            raise ValueError("Range validation requires a numeric parameter")
+        enum_values = self.validation.values if isinstance(self.validation, EnumValidation) else ()
+        for literal in (self.default, self.example, *enum_values):
+            if literal is None:
+                continue
+            expected = "number" if self.json_type == "integer" else self.json_type
+            if literal.kind != expected or (
+                self.json_type == "integer"
+                and literal.kind == "number"
+                and not literal.value.is_integer()
+            ):
+                raise ValueError(
+                    f"Parameter {self.name}: literal kind must agree with {self.json_type}"
+                )
+            rule = self.validation
+            value = literal.value
+            if isinstance(rule, RegexValidation) and (
+                not isinstance(value, str) or re.search(rule.pattern, value) is None
+            ):
+                raise ValueError(f"Parameter {self.name}: literal violates regex")
+            if isinstance(rule, RangeValidation) and (
+                not isinstance(value, (int, float))
+                or isinstance(value, bool)
+                or (rule.minimum is not None and float(value) < rule.minimum)
+                or (rule.maximum is not None and float(value) > rule.maximum)
+            ):
+                raise ValueError(f"Parameter {self.name}: literal violates range")
+            if isinstance(rule, EnumValidation) and literal not in rule.values:
+                raise ValueError(f"Parameter {self.name}: literal violates enum")
+        return self
+
+
+class ExtractorSpec(DomainModel):
+    """Extraction is an AX attribute plus optional regex, never executable recorded code."""
+
+    target: AxTarget
+    attribute: Literal["value", "name", "description"]
+    regex: str | None = None
+
+    @field_validator("regex")
+    @classmethod
+    def valid_regex(cls, value: str | None) -> str | None:
+        if value is not None:
+            try:
+                re.compile(value)
+            except re.error as exc:
+                raise ValueError("Invalid extraction regex") from exc
+        return value
+
+
+class OutputSpec(DomainModel):
+    name: NonEmpty
+    json_type: JsonType
+    produced_by_step_id: NonEmpty
+    extractor: ExtractorSpec
+    nullable: bool
+    sensitivity: Sensitivity
+    description: NonEmpty
+
+
+class RecoveryAction(DomainModel):
+    """Bounded named recovery only; arbitrary scripts and unbounded retry loops were rejected."""
+
+    kind: Literal["retry", "refresh", "reauthenticate", "dismiss"]
+    max_attempts: int = Field(ge=1, le=10)
+
+
+class OutcomeSpec(DomainModel):
+    code: NonEmpty
+    detect: Predicate
+    classification: Literal["business", "recoverable", "hard"]
+    caller_message: NonEmpty
+    recovery: RecoveryAction | None
+
+    @model_validator(mode="after")
+    def recovery_contract(self) -> Self:
+        if self.classification == "recoverable" and self.recovery is None:
+            raise ValueError(
+                f"Outcome {self.code}: recoverable requires an explicit recovery action"
+            )
+        if self.classification != "recoverable" and self.recovery is not None:
+            raise ValueError(
+                f"Outcome {self.code}: {self.classification} forbids recovery; "
+                "business is an answer, not a crash"
+            )
+        return self
+
+
+class CapabilityTarget(DomainModel):
+    surface_kind: SurfaceKind
+    app_id: NonEmpty
+    entry_point: NonEmpty
+    tenant_id: str | None
+    allowlist_ref: NonEmpty
+    recorded_fingerprint: SurfaceFingerprint
+
+
+class UndeclaredOutcomeError(ValueError):
+    """An edge names no declared outcome; fix the code or declare its detection semantics."""
+
+
+class Capability(DomainModel):
+    schema_version: Literal[1] = 1
+    capability_id: ULID
+    name: NonEmpty
+    description: NonEmpty
+    version: str = Field(
+        pattern=(
+            r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
+            r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
+        )
+    )
+    status: Literal["draft", "candidate", "approved"]
+    target: CapabilityTarget
+    inputs: tuple[ParamSpec, ...]
+    outputs: tuple[OutputSpec, ...]
+    steps: tuple[Step, ...] = Field(min_length=1)
+    outcomes: tuple[OutcomeSpec, ...]
+    provenance: CapabilityProvenance
+
+    @field_validator("version")
+    @classmethod
+    def valid_semver_prerelease(cls, value: str) -> str:
+        """SemVer forbids leading zeroes in numeric prerelease IDs, unlike build metadata."""
+        core = value.split("+", 1)[0]
+        if "-" in core:
+            for identifier in core.split("-", 1)[1].split("."):
+                if identifier.isdigit() and len(identifier) > 1 and identifier.startswith("0"):
+                    raise ValueError(
+                        "SemVer numeric prerelease identifiers cannot have leading zeroes"
+                    )
+        return value
+
+    @model_validator(mode="after")
+    def coherent_artifact(self) -> Self:
+        self.assert_attributable()
+        for label, names in (
+            ("steps", [s.step_id for s in self.steps]),
+            ("inputs", [p.name for p in self.inputs]),
+            ("outputs", [o.name for o in self.outputs]),
+            ("outcomes", [o.code for o in self.outcomes]),
+        ):
+            if len(names) != len(set(names)):
+                raise ValueError(f"Duplicate {label}; names must be unique")
+        if [step.ordinal for step in self.steps] != list(range(1, len(self.steps) + 1)):
+            raise ValueError("Step ordinals must be contiguous, ordered, and start at 1")
+        declared = {outcome.code: outcome for outcome in self.outcomes}
+        inputs = {param.name: param for param in self.inputs}
+        for step in self.steps:
+            for entry in step.on_outcome:
+                if entry.code not in declared:
+                    raise UndeclaredOutcomeError(
+                        f"Step {step.step_id}: outcome {entry.code!r} is undeclared; "
+                        "add an OutcomeSpec or correct the code"
+                    )
+                expected = {"business": "return", "recoverable": "recover", "hard": "fail"}[
+                    declared[entry.code].classification
+                ]
+                if entry.handling.kind != expected:
+                    raise ValueError(
+                        f"Step {step.step_id}: {entry.code} requires handling {expected}"
+                    )
+            if isinstance(step.action, (TypeText, SelectOption)):
+                ref = step.action.value_ref
+                if isinstance(ref, (ParamRef, SecretRef)):
+                    if ref.name not in inputs:
+                        raise ValueError(f"Step {step.step_id}: undeclared input {ref.name!r}")
+                    if (inputs[ref.name].sensitivity == "secret") != isinstance(ref, SecretRef):
+                        raise ValueError(
+                            f"Step {step.step_id}: secret inputs require secret_ref; "
+                            "other inputs require param_ref"
+                        )
+        for output in self.outputs:
+            producer = next(
+                (s for s in self.steps if s.step_id == output.produced_by_step_id), None
+            )
+            if (
+                producer is None
+                or not isinstance(producer.action, ReadValue)
+                or producer.action.output_name != output.name
+            ):
+                raise ValueError(
+                    f"Output {output.name}: produced_by_step_id must name its ReadValue step"
+                )
+        for step in self.steps:
+            if isinstance(step.action, ReadValue) and not any(
+                o.name == step.action.output_name and o.produced_by_step_id == step.step_id
+                for o in self.outputs
+            ):
+                raise ValueError(f"Step {step.step_id}: ReadValue needs a matching OutputSpec")
+        if self.status == "approved" and self.provenance.approval is None:
+            raise ValueError("Approved artifacts require a named ApprovalRecord")
+        fp = self.target.recorded_fingerprint
+        if fp.app_id != self.target.app_id or (
+            self.target.tenant_id is not None and fp.tenant_id != self.target.tenant_id
+        ):
+            raise ValueError("Capability target disagrees with its recorded fingerprint")
+        return self
+
+    def assert_attributable(self) -> None:
+        for step in self.steps:
+            assert_step_attributable(step.step_id, step.provenance)
+            if step.provenance.discovery_run_id != self.provenance.derived_from_run_id:
+                raise ValueError(
+                    f"Step {step.step_id}: discovery run differs from capability provenance"
+                )
+            if (
+                isinstance(step.provenance.decided_by, ModelRef)
+                and step.provenance.decided_by not in self.provenance.models_used
+            ):
+                raise ValueError(f"Step {step.step_id}: deciding model is absent from models_used")
diff --git a/src/cua/domain/steps.py b/src/cua/domain/steps.py
new file mode 100644
--- /dev/null
+++ b/src/cua/domain/steps.py
@@ -0,0 +1,107 @@
+"""Define reviewable replay steps; forbid I/O, frameworks, and other cua packages."""
+
+from collections.abc import Mapping
+from functools import cached_property
+from types import MappingProxyType
+from typing import Literal, Never, Self
+
+from pydantic import Field, field_serializer, model_validator
+
+from cua.domain.actions import Action, Click, Dismiss, ReadValue, SelectOption, TypeText
+from cua.domain.common import DomainModel, NonEmpty
+from cua.domain.locators import LocatorLadder
+from cua.domain.predicates import Predicate
+from cua.domain.provenance import StepProvenance
+
+
+class RetryPolicy(DomainModel):
+    max_attempts: int = Field(default=1, ge=1, le=10)
+    delay_ms: int = Field(default=0, ge=0)
+
+
+class StepTiming(DomainModel):
+    settle_strategy: Literal["none", "ax_stable", "navigation"]
+    timeout_ms: int = Field(gt=0)
+    retry: RetryPolicy = Field(default_factory=RetryPolicy)
+
+
+class StepOutcomeHandling(DomainModel):
+    kind: Literal["return", "recover", "fail"]
+
+
+class OutcomeHandlingEntry(DomainModel):
+    code: NonEmpty
+    handling: StepOutcomeHandling
+
+
+class _OutcomeEntries(list[OutcomeHandlingEntry]):
+    """Keep the requested list wire shape without allowing a stale cached mapping.
+
+    A plain list inside a frozen Pydantic model is still mutable. Reject its mutators rather
+    than rely on a convention that edits must reconstruct the Step.
+    """
+
+    def _deny(self, *args: object, **kwargs: object) -> Never:
+        raise TypeError("Outcome entries are immutable; construct and validate a new Step")
+
+    __setitem__ = _deny
+    __delitem__ = _deny
+    __iadd__ = _deny
+    __imul__ = _deny
+    append = _deny
+    clear = _deny
+    extend = _deny
+    insert = _deny
+    pop = _deny
+    remove = _deny
+    reverse = _deny
+    sort = _deny
+
+
+class Step(DomainModel):
+    """Outcome codes use entries on the wire, never arbitrary JSON object keys.
+
+    The cached mapping is read-only and never serialized. The list is frozen after validation;
+    edits must be revalidated as new Steps so neither the mapping nor invariants become stale.
+    """
+
+    step_id: NonEmpty
+    ordinal: int = Field(ge=1)
+    intent: NonEmpty
+    action: Action
+    target: LocatorLadder | None
+    preconditions: tuple[Predicate, ...] = ()
+    checkpoint: Predicate | None
+    risk: Literal["safe", "reversible", "irreversible"]
+    timing: StepTiming
+    on_outcome: list[OutcomeHandlingEntry] = Field(default_factory=list)
+    provenance: StepProvenance
+
+    @cached_property
+    def outcome_map(self) -> Mapping[str, StepOutcomeHandling]:
+        return MappingProxyType({entry.code: entry.handling for entry in self.on_outcome})
+
+    @field_serializer("on_outcome")
+    def sorted_outcomes(self, entries: list[OutcomeHandlingEntry]) -> list[OutcomeHandlingEntry]:
+        return sorted(entries, key=lambda entry: entry.code)
+
+    @model_validator(mode="after")
+    def valid_step(self) -> Self:
+        codes = [entry.code for entry in self.on_outcome]
+        if len(codes) != len(set(codes)):
+            raise ValueError(f"Step {self.step_id}: on_outcome codes must be unique")
+        if (
+            isinstance(self.action, (Click, TypeText, SelectOption, ReadValue, Dismiss))
+            and not self.target
+        ):
+            raise ValueError(
+                f"Step {self.step_id}: {self.action.kind} requires a target locator ladder"
+            )
+        if self.risk == "irreversible" and (
+            self.checkpoint is None or self.timing.retry.max_attempts != 1
+        ):
+            raise ValueError(
+                "Irreversible steps need a checkpoint and exactly one attempt; recovery is explicit"
+            )
+        object.__setattr__(self, "on_outcome", _OutcomeEntries(self.on_outcome))
+        return self
```

## Verification: make lint typecheck test

```text
uv run --locked ruff check .
All checks passed!
uv run --locked ruff format --check .
58 files already formatted
uv run --locked lint-imports
=============
Import Linter
=============


---------
Contracts
---------

Analyzed 61 files, 133 dependencies.
------------------------------------

Domain is framework-free KEPT
Policy is framework-free KEPT
Replay is framework-free KEPT
Application dependency layers KEPT
Automation cannot import the target application KEPT

Contracts: 5 kept, 0 broken.
uv run --locked mypy --strict src/cua
Success: no issues found in 33 source files
uv run --locked pytest
============================= test session starts =============================
platform win32 -- Python 3.12.14, pytest-9.1.1, pluggy-1.6.0
rootdir: C:\interface.ai
configfile: pyproject.toml
testpaths: tests
plugins: anyio-4.15.1, Faker-40.39.0, hypothesis-6.168.0, langsmith-0.12.6, asyncio-1.4.0, cov-7.1.0
asyncio: mode=Mode.AUTO, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collected 152 items

tests\browser\test_target_app_smoke.py ss                                [  1%]
tests\integration\target_app\test_app.py ............................... [ 21%]
.............                                                            [ 30%]
tests\unit\domain\test_contracts.py .................................... [ 53%]
                                                                         [ 53%]
tests\unit\domain\test_invariants.py ................................... [ 76%]
...........                                                              [ 84%]
tests\unit\domain\test_schemas.py ...............                        [ 94%]
tests\unit\test_scaffold.py .                                            [ 94%]
tests\unit\test_target_app_boundary.py ..                                [ 96%]
tests\unit\test_target_app_cli.py ......                                 [100%]

=============================== tests coverage ================================
______________ coverage: platform win32, python 3.12.14-final-0 _______________

Name                                Stmts   Miss Branch BrPart  Cover   Missing
-------------------------------------------------------------------------------
src\cua\__init__.py                     1      0      0      0   100%
src\cua\catalog\__init__.py             0      0      0      0   100%
src\cua\discovery\__init__.py           0      0      0      0   100%
src\cua\domain\__init__.py              0      0      0      0   100%
src\cua\domain\actions.py              34      0      0      0   100%
src\cua\domain\capability.py          176      0     72      1    99%   258->242
src\cua\domain\common.py               43      0      0      0   100%
src\cua\domain\locators.py             72      0      6      0   100%
src\cua\domain\migrations.py           43      1      6      1    96%   80
src\cua\domain\models.py               51      0      4      0   100%
src\cua\domain\names.py                38      0     16      1    98%   51->exit
src\cua\domain\observation.py          61      0      2      0   100%
src\cua\domain\ports.py                23      0      0      0   100%
src\cua\domain\predicates.py          112      0     14      0   100%
src\cua\domain\provenance.py           66      0     12      0   100%
src\cua\domain\results.py              45      0      0      0   100%
src\cua\domain\schemas.py              56      1     30      2    97%   39->exit, 51
src\cua\domain\steps.py                66      0      6      0   100%
src\cua\escalation\__init__.py          0      0      0      0   100%
src\cua\observability\__init__.py       0      0      0      0   100%
src\cua\policy\__init__.py              0      0      0      0   100%
src\cua\replay\__init__.py              0      0      0      0   100%
src\cua\surface\__init__.py             0      0      0      0   100%
src\cua\target_app\__init__.py          0      0      0      0   100%
src\cua\target_app\__main__.py          8      1      2      1    80%   23
src\cua\target_app\app.py             125      3     40      3    96%   65, 152, 183
src\cua\target_app\config.py           30      0      0      0   100%
src\cua\target_app\faults.py           29      1      4      1    94%   67
src\cua\target_app\models.py           92      0     10      0   100%
src\cua\target_app\state.py            28      0      6      1    97%   58->60
src\cua\target_app\workflow.py         76      3     42      2    96%   93-94, 114
-------------------------------------------------------------------------------
TOTAL                                1275     10    272     13    99%
Required test coverage of 80% reached. Total coverage: 98.51%
======================= 150 passed, 2 skipped in 11.08s =======================
```

The provider-schema tests run offline. They verify a shared schema representation and domain validation, not acceptance by live provider APIs. The two skipped tests are opt-in Chromium smoke tests from Stage 1.
