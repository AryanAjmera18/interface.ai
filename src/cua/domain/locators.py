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
