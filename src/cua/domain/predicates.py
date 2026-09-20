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


class AmbiguousTargetError(ValueError):
    """More than one semantic match; refine the container or explicitly choose a scoped nth."""


class AxTarget(DomainModel):
    """Position is acceptable inside a semantic container, never as a substitute for one.

    Depth counts targets including this leaf (at most three). Ancestors must themselves
    resolve uniquely; an ambiguous container cannot silently widen the search.
    """

    role: NonEmpty
    name_matcher: NameMatcher
    frame_path: tuple[str, ...] | None = None
    node_path: tuple[int, ...] | None = None
    within: "AxTarget | None" = None
    nth: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def bounded_scope(self) -> Self:
        if self.nth is not None and self.within is None:
            raise ValueError("nth requires a semantically identified within container")
        depth, parent = 1, self.within
        while parent is not None:
            depth, parent = depth + 1, parent.within
        if depth > 3:
            raise ValueError("AxTarget recursion depth must not exceed 3 targets")
        return self

    def select(self, observation: Observation) -> tuple[AxNode, ...]:
        roots = self.within.select(observation) if self.within else (observation.ax_root,)
        matches = tuple(
            node
            for root in roots
            for node in (walk_ax(root)[1:] if self.within else walk_ax(root))
            if node.role == self.role
            and self.name_matcher.matches(node.name)
            and (self.frame_path is None or node.frame_path == self.frame_path)
            and (self.node_path is None or node.node_path == self.node_path)
        )
        if self.nth is not None:
            return matches[self.nth : self.nth + 1]
        if len(matches) > 1:
            raise AmbiguousTargetError(
                f"Expected one {self.role} with {self.name_matcher.describe()}; "
                f"matched {len(matches)}. Add a semantic within scope or scoped nth."
            )
        return matches


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
        # A cardinality assertion intentionally counts all matches; it is not target resolution.
        count = sum(
            node.role == predicate.role
            and predicate.name_matcher.matches(node.name)
            and (predicate.frame_path is None or node.frame_path == predicate.frame_path)
            for node in walk_ax(observation.ax_root)
        )
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
