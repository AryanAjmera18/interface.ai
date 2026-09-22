"""Derive replay locator alternatives from recorded AX evidence; forbid live surface I/O."""

from datetime import datetime

from cua.domain.common import EvidenceRef
from cua.domain.locators import (
    AxPathCandidate,
    LabelCandidate,
    LocatorCandidate,
    LocatorLadder,
    RoleNameCandidate,
    ScopedRoleNameCandidate,
    ScopeHints,
    StructuralCandidate,
    stability_key,
)
from cua.domain.observation import AxNode, walk_ax
from cua.domain.predicates import AxTarget


def _common(node: AxNode, evidence_ref: EvidenceRef, observed_at: datetime) -> dict[str, object]:
    return {
        "frame_path": node.frame_path,
        "confidence": 1.0,
        "source": "ax_tree",
        "observed_at": observed_at,
        "evidence_ref": evidence_ref,
    }


def compile_ladder(
    root: AxNode, target: AxTarget, evidence_ref: EvidenceRef, observed_at: datetime
) -> LocatorLadder:
    """Retain semantic and structural alternatives found in one recorded AX snapshot.

    The model-selected locator is only a target hint. Every emitted candidate is independently
    derived from evidence; invented CSS and unavailable coordinates are excluded.
    """
    matches = [
        node
        for node in walk_ax(root)
        if node.role == target.role
        and target.name_matcher.matches(node.name)
        and (target.frame_path is None or node.frame_path == target.frame_path)
    ]
    if not matches:
        description = target.name_matcher.describe()
        raise ValueError(f"Recorded AX snapshot has no {target.role} matching {description}")
    node = matches[target.nth or 0]
    frame_nodes = [item for item in walk_ax(root) if item.frame_path == node.frame_path]
    same_name = sum(
        item.role == node.role and target.name_matcher.matches(item.name) for item in frame_nodes
    )
    common = _common(node, evidence_ref, observed_at)
    candidates: list[LocatorCandidate] = []
    if target.within is not None:
        candidates.append(
            ScopedRoleNameCandidate.model_validate(
                {
                    **common,
                    "value": {
                        "role": node.role,
                        "name_matcher": target.name_matcher,
                        "ancestor": target.within,
                    },
                    "uniqueness_at_record": 1,
                    "rationale": "Recorded AX role and name inside a semantic ancestor",
                }
            )
        )
    candidates.append(
        RoleNameCandidate.model_validate(
            {
                **common,
                "value": {"role": node.role, "name_matcher": target.name_matcher},
                "uniqueness_at_record": same_name,
                "rationale": "Recorded AX role and accessible name",
            }
        )
    )
    adjacent_text = node.name and any(
        item.role == "text" and item.name == node.name and item.frame_path == node.frame_path
        for item in frame_nodes
    )
    if adjacent_text:
        candidates.append(
            LabelCandidate.model_validate(
                {
                    **common,
                    "value": {"matcher": target.name_matcher},
                    "uniqueness_at_record": 1,
                    "rationale": "Matching text appears beside the control in recorded AX",
                }
            )
        )
    if node.node_path:
        for candidate_type, rationale in (
            (AxPathCandidate, "Recorded AX path within its frame"),
            (StructuralCandidate, "Fallback frame-local structural path"),
        ):
            candidates.append(
                candidate_type.model_validate(
                    {
                        **common,
                        "value": {"path": node.node_path},
                        "uniqueness_at_record": 1,
                        "rationale": rationale,
                    }
                )
            )
    return LocatorLadder(
        candidates=tuple(sorted(candidates, key=stability_key, reverse=True)),
        scope_hints=ScopeHints(ancestor=target.within),
    )
