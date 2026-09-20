"""Guard scoped targeting and the v2 migration; forbid external services."""

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import ValidationError

from cua.domain.capability import Capability, ParamSpec
from cua.domain.migrations import load_capability
from cua.domain.names import NameMatcher
from cua.domain.observation import AxNode, Observation
from cua.domain.predicates import AmbiguousTargetError, AxTarget
from cua.domain.schemas import artifact_json
from cua.domain.steps import Step
from tests.unit.domain.demo_sample import demo_capability
from tests.unit.domain.samples import FixedClock, SequenceIds, capability, observation

ROOT = Path(__file__).resolve().parents[3]


def rows() -> Observation:
    root = AxNode(
        role="table",
        name="Accounts",
        children=tuple(
            AxNode(
                role="row",
                name=name,
                node_path=(i,),
                children=tuple(
                    AxNode(role="cell", name=value, node_path=(i, j))
                    for j, value in enumerate((name, "$123.45"))
                ),
            )
            for i, name in enumerate(("Checking", "Savings"))
        ),
    )
    return Observation.model_validate(
        {**observation().model_dump(exclude={"hash"}), "ax_root": root}
    )


def test_ambiguity_fails_closed_but_semantic_scope_reads_savings() -> None:
    target = AxTarget(role="cell", name_matcher=NameMatcher(value="$123.45"))
    with pytest.raises(AmbiguousTargetError, match=r"matched 2.*within"):
        target.select(rows())
    scoped = AxTarget(
        **target.model_dump(exclude={"within"}),
        within=AxTarget(role="row", name_matcher=NameMatcher(value="Savings")),
    )
    assert scoped.select(rows())[0].node_path == (1, 1)
    assert (
        scoped.model_copy(
            update={"within": AxTarget(role="row", name_matcher=NameMatcher(value="Missing"))}
        ).select(rows())
        == ()
    )
    ambiguous_parent = AxTarget(role="row", name_matcher=NameMatcher(mode="regex", value=".*"))
    with pytest.raises(AmbiguousTargetError):
        scoped.model_copy(update={"within": ambiguous_parent}).select(rows())


def test_nth_requires_scope_and_depth_is_bounded() -> None:
    data = {"role": "cell", "name_matcher": NameMatcher(mode="regex", value=".*")}
    with pytest.raises(ValidationError, match="nth requires"):
        AxTarget(**data, nth=0)
    row = AxTarget(role="row", name_matcher=NameMatcher(value="Savings"))
    assert AxTarget(**data, within=row, nth=1).select(rows())[0].name == "$123.45"
    assert AxTarget(**data, within=row, nth=8).select(rows()) == ()
    nested = AxTarget(**data, within=AxTarget(**data, within=row))
    with pytest.raises(ValidationError, match="depth"):
        AxTarget(**data, within=nested)


def test_tuple_order_is_identical_on_wire_and_in_memory() -> None:
    step = demo_capability().steps[0]
    parsed = Step.model_validate({**step.model_dump(), "on_outcome": step.on_outcome[::-1]})
    assert isinstance(parsed.on_outcome, tuple)
    codes = [entry.code for entry in parsed.on_outcome]
    assert (
        codes
        == sorted(codes)
        == [entry["code"] for entry in parsed.model_dump(mode="json")["on_outcome"]]
    )
    assert parsed.outcome_map is parsed.outcome_map
    with pytest.raises(TypeError):
        parsed.outcome_map["new"] = parsed.on_outcome[0].handling


@pytest.mark.parametrize("sensitivity", ["secret", "pii"])
@pytest.mark.parametrize("field", ["default", "example"])
def test_sensitive_literals_prohibited(sensitivity: str, field: str) -> None:
    data = capability().inputs[0].model_dump()
    data.update(required=False, sensitivity=sensitivity, example=None)
    data[field] = {"kind": "string", "value": "synthetic"}
    with pytest.raises(ValidationError, match="Sensitive"):
        ParamSpec.model_validate(data)


def test_required_default_and_null_parameter_prohibited() -> None:
    data = capability().inputs[0].model_dump()
    with pytest.raises(ValidationError, match="Required"):
        ParamSpec.model_validate({**data, "default": data["example"]})
    with pytest.raises(ValidationError):
        ParamSpec.model_validate({**data, "json_type": "null", "example": None})


@pytest.mark.parametrize("approved", [False, True])
def test_rich_golden_roundtrip(approved: bool, golden_file: Callable[[Path, bytes], bytes]) -> None:
    name = "capability.approved.v2.json" if approved else "capability.v2.json"
    cap = demo_capability(approved=approved)
    recorded = golden_file(ROOT / "tests/golden" / name, artifact_json(cap).encode())
    assert artifact_json(Capability.model_validate_json(recorded)).encode() == recorded
    assert len(cap.steps) >= 4 and len(cap.outcomes) == 3
    assert cap.outputs[0].extractor.target.within is not None
    if approved:
        with pytest.raises(ValidationError, match="approval"):
            Capability.model_validate({**cap.model_dump(), "provenance": capability().provenance})


def test_v1_migration_and_frozen_schemas() -> None:
    text = (ROOT / "tests/golden/capability.legacy-v1.json").read_text()
    migrated = load_capability(text, clock=FixedClock(), ids=SequenceIds())
    assert migrated.capability.schema_version == 2
    assert [(r.from_version, r.to_version) for r in migrated.migrations] == [(1, 2), (2, 2)]
    assert migrated.migrations[0].before_hash != migrated.migrations[0].after_hash
    old = json.loads(text)
    old["status"] = "approved"
    old["provenance"]["approval"] = demo_capability(approved=True).provenance.approval.model_dump(
        mode="json"
    )
    with pytest.raises(ValidationError, match="changed after approval"):
        load_capability(json.dumps(old), clock=FixedClock(), ids=SequenceIds())
    old["inputs"][0]["sensitivity"] = "pii"
    with pytest.raises(ValidationError, match="Sensitive"):
        load_capability(json.dumps(old), clock=FixedClock(), ids=SequenceIds())
    manifest = json.loads((ROOT / "tests/golden/schema-digests.v1.json").read_text())
    for name, expected in manifest.items():
        assert hashlib.sha256((ROOT / "docs/schema" / name).read_bytes()).hexdigest() == expected
