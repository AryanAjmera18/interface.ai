"""Test that tenant overrides are narrow, attributable, and content-bound."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from cua.domain.capability import Capability, capability_content_digest
from cua.domain.common import digest
from cua.domain.provenance import HumanEdit
from cua.replay.overrides import CapabilityPatch, StaleOverrideError, StepPatch, TenantOverride


def approved_capability() -> Capability:
    return Capability.model_validate_json(
        Path("capabilities/look_up_member_savings_balance@1.0.0.json").read_bytes()
    )


def tenant_override(capability: Capability) -> TenantOverride:
    step = capability.steps[0]
    assert step.checkpoint is not None
    changed = step.checkpoint.model_copy(
        update={
            "name_matcher": step.checkpoint.name_matcher.model_copy(
                update={"value": "Beta Community — Sign in"}
            )
        }
    )
    return TenantOverride(
        tenant_id="beta",
        parent_capability_id=capability.capability_id,
        parent_content_digest=capability_content_digest(capability),
        patch=CapabilityPatch(steps=(StepPatch(step_id=step.step_id, checkpoint=changed),)),
        created_at=datetime(2026, 9, 24, tzinfo=UTC),
        edits=(
            HumanEdit(
                actor="reviewer:test",
                at=datetime(2026, 9, 24, tzinfo=UTC),
                field_path="/steps/0/checkpoint",
                before_hash=digest(step.checkpoint.model_dump(mode="json")),
                after_hash=digest(changed.model_dump(mode="json")),
                reason="Test the beta heading without changing executable action data.",
            ),
        ),
    )


def test_committed_override_is_valid_and_content_bound() -> None:
    capability = approved_capability()
    override = TenantOverride.model_validate_json(
        Path("capabilities/overrides/beta/look_up_member_savings_balance.json").read_bytes()
    )

    override.verify_parent(capability)
    assert override.tenant_id == "beta"
    assert all(
        item.target is not None or item.checkpoint is not None for item in override.patch.steps
    )


def test_base_content_change_makes_override_stale() -> None:
    capability = approved_capability()
    override = tenant_override(capability)
    changed = capability.model_copy(update={"description": "changed after review"})

    with pytest.raises(StaleOverrideError, match="base artifact changed after review"):
        override.verify_parent(changed)


def test_patch_rejects_behavior_fields_and_unknown_steps() -> None:
    capability = approved_capability()
    payload = tenant_override(capability).model_dump(mode="json")
    payload["patch"]["steps"][0]["risk"] = "safe"

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        TenantOverride.model_validate(payload)

    unknown = tenant_override(capability).model_copy(
        update={
            "patch": CapabilityPatch(
                steps=(
                    tenant_override(capability)
                    .patch.steps[0]
                    .model_copy(update={"step_id": "missing"}),
                )
            )
        }
    )
    with pytest.raises(ValueError, match="unknown step"):
        unknown.verify_parent(capability)


def test_override_serialization_is_stable() -> None:
    path = Path("capabilities/overrides/beta/look_up_member_savings_balance.json")
    parsed = TenantOverride.model_validate_json(path.read_bytes())
    rendered = (
        json.dumps(parsed.model_dump(mode="json"), indent=2, sort_keys=True, ensure_ascii=False)
        + "\n"
    )
    assert rendered.encode() == path.read_bytes()
