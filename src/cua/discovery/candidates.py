"""Derive locator evidence from observations; forbid accepting model-authored locator strings."""

from cua.domain.common import EvidenceRef
from cua.domain.locators import (
    LocatorLadder,
    RoleNameCandidate,
    RoleNameValue,
    ScopedRoleNameCandidate,
    ScopedRoleNameValue,
    ScopeHints,
)
from cua.domain.observation import Observation, walk_ax
from cua.domain.predicates import AxTarget
from cua.policy.models import TargetSemantics


def target_semantics(target: AxTarget | None) -> TargetSemantics | None:
    if target is None:
        return None
    return TargetSemantics(
        role=target.role, name=target.name_matcher.value, frame_path=target.frame_path or ()
    )


def locator_ladder(
    target: AxTarget | None, observation: Observation, evidence_ref: EvidenceRef
) -> LocatorLadder | None:
    if target is None:
        return None
    matches = [
        node
        for node in walk_ax(observation.ax_root)
        if node.role == target.role
        and target.name_matcher.matches(node.name)
        and (target.frame_path is None or node.frame_path == target.frame_path)
    ]
    if not matches:
        return None
    candidate = RoleNameCandidate(
        value=RoleNameValue(role=target.role, name_matcher=target.name_matcher),
        frame_path=matches[0].frame_path,
        confidence=1.0,
        source="ax_tree",
        observed_at=observation.captured_at,
        evidence_ref=evidence_ref,
        uniqueness_at_record=len(matches),
        rationale="Derived from the normalized AX tree",
    )
    if target.within is None:
        return LocatorLadder(candidates=(candidate,))
    scoped_matches = target.select(observation)
    scoped = ScopedRoleNameCandidate(
        value=ScopedRoleNameValue(
            role=target.role,
            name_matcher=target.name_matcher,
            ancestor=target.within,
        ),
        frame_path=matches[0].frame_path,
        confidence=1.0,
        source="ax_tree",
        observed_at=observation.captured_at,
        evidence_ref=evidence_ref,
        uniqueness_at_record=len(scoped_matches),
        rationale="Derived from the normalized AX tree with ancestor scope",
    )
    return LocatorLadder(
        candidates=(scoped, candidate), scope_hints=ScopeHints(ancestor=target.within)
    )
