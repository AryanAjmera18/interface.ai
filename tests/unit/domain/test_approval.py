"""Bind approval to content and reject silent migration authority; forbid external services."""

import json
from typing import Any

import pytest
from pydantic import ValidationError

from cua.domain.capability import Capability, StaleApprovalError, capability_content_digest
from cua.domain.migrations import ArtifactDocument, v1_to_v2
from tests.unit.domain.demo_sample import demo_capability
from tests.unit.domain.samples import capability


def test_approval_binds_this_content() -> None:
    cap = demo_capability(approved=True)
    assert cap.provenance.approval is not None
    assert cap.provenance.approval.reviewed_digest == capability_content_digest(cap)
    assert Capability.model_validate_json(cap.model_dump_json()) == cap
    stale = cap.model_copy(update={"description": "Changed content"})
    with pytest.raises(StaleApprovalError, match=r"changed after approval.*re-reviewed"):
        stale.assert_current_approval()


def content_mutations(
    value: Any, path: tuple[str | int, ...] = ()
) -> list[tuple[tuple[str | int, ...], Any]]:
    """Change every scalar leaf independently; invalid shape also correctly rejects approval."""
    if isinstance(value, dict):
        return [
            item for key, child in value.items() for item in content_mutations(child, (*path, key))
        ]
    if isinstance(value, list):
        return [
            item for i, child in enumerate(value) for item in content_mutations(child, (*path, i))
        ]
    if path == ("status",) or path[:2] == ("provenance", "approval"):
        return []
    replacement = (
        not value
        if isinstance(value, bool)
        else (
            value + 1
            if isinstance(value, (int, float))
            else (value + " changed" if isinstance(value, str) else "changed")
        )
    )
    return [(path, replacement)]


def test_every_content_leaf_changes_digest_and_rejects_approved_artifact() -> None:
    cap = demo_capability(approved=True)
    source = cap.model_dump(mode="json")
    for path, replacement in content_mutations(source):
        changed = json.loads(json.dumps(source))
        parent = changed
        for part in path[:-1]:
            parent = parent[part]
        parent[path[-1]] = replacement
        with pytest.raises(ValidationError):
            Capability.model_validate(changed)


def test_exclusions_do_not_change_content_digest() -> None:
    cap = demo_capability(approved=True)
    original = capability_content_digest(cap)
    for status in ("draft", "candidate", "approved"):
        changed = Capability.model_validate({**cap.model_dump(), "status": status})
        assert capability_content_digest(changed) == original
    data = cap.model_dump(mode="json")
    data["provenance"]["approval"].update(
        actor="different-reviewer", reason="Updated review note", at="2026-02-01T00:00:00Z"
    )
    assert capability_content_digest(Capability.model_validate(data)) == original
    data["provenance"]["approval"]["reviewed_digest"] = "f" * 64
    with pytest.raises(ValidationError, match="changed after approval"):
        Capability.model_validate(data)
    draft = Capability.model_validate({**capability().model_dump(), "status": "draft"})
    assert draft.provenance.approval is None


def test_approved_migration_cannot_pass_vacuously() -> None:
    source = demo_capability(approved=True).model_dump(mode="json")
    source["schema_version"] = 1
    migrated = v1_to_v2(ArtifactDocument(payload=source))
    assert isinstance(migrated.payload, dict)
    assert migrated.payload["status"] == "approved"
    with pytest.raises(ValidationError, match=r"changed after approval.*re-reviewed"):
        Capability.model_validate(migrated.payload)
