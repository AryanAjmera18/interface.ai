"""Exercise cross-field artifact invariants; forbid external services."""

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from cua.domain import actions as a
from cua.domain import capability as c
from cua.domain import common as v
from cua.domain import models
from cua.domain import steps as s
from cua.domain.observation import observation_json
from cua.domain.predicates import AxTarget
from cua.domain.schemas import strict_schema
from tests.unit.domain.samples import capability, ladder, observation


@pytest.mark.parametrize(
    "version", ["1.0.0-01", "1.0.0-alpha..1", "01.0.0", "1.0", "1.0.0+", "1.0.0-"]
)
def test_invalid_semver(version: str) -> None:
    data = capability().model_dump()
    data["version"] = version
    with pytest.raises(ValidationError):
        c.Capability.model_validate(data)


@pytest.mark.parametrize("version", ["1.0.0-alpha.1", "0.1.0+001", "2.3.4-rc-1+build.01"])
def test_valid_semver(version: str) -> None:
    assert c.Capability.model_validate({**capability().model_dump(), "version": version})


def test_outcome_entries_cannot_invalidate_cached_map() -> None:
    step = capability().steps[0]
    cached = step.outcome_map
    with pytest.raises((AttributeError, TypeError)):
        step.on_outcome.append(step.on_outcome[0])
    with pytest.raises((AttributeError, TypeError)):
        step.on_outcome.clear()
    with pytest.raises((AttributeError, TypeError)):
        step.on_outcome[0] = step.on_outcome[0]
    assert cached is step.outcome_map and len(cached) == len(step.on_outcome) == 1


@pytest.mark.parametrize(
    "cls,value",
    [(v.StringValue, 4), (v.BooleanValue, "false"), (v.NumberValue, "12"), (v.NumberValue, True)],
)
def test_literal_types_are_not_coerced(cls: type[v.DomainModel], value: Any) -> None:
    with pytest.raises(ValidationError):
        cls.model_validate({"value": value})


@pytest.mark.parametrize(
    "change",
    [
        {"steps": []},
        {"status": "approved"},
        {"version": "invalid"},
    ],
)
def test_capability_rejects_incomplete_shapes(change: dict[str, Any]) -> None:
    data = capability().model_dump()
    data.update(change)
    with pytest.raises(ValidationError):
        c.Capability.model_validate(data)


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate_steps",
        "duplicate_inputs",
        "duplicate_outcomes",
        "ordinal",
        "handling",
        "param",
        "secret",
        "fingerprint",
        "run",
        "model",
    ],
)
def test_cross_reference_failures(mutation: str) -> None:
    data = json.loads(capability().model_dump_json())
    step = data["steps"][0]
    if mutation.startswith("duplicate_"):
        data[mutation.removeprefix("duplicate_")] *= 2
    elif mutation == "ordinal":
        step["ordinal"] = 2
    elif mutation == "handling":
        step["on_outcome"][0]["handling"]["kind"] = "fail"
    elif mutation == "param":
        step["action"]["value_ref"]["name"] = "undeclared"
    elif mutation == "secret":
        data["inputs"][0].update(sensitivity="secret", example=None)
    elif mutation == "fingerprint":
        data["target"]["app_id"] = "wrong"
    elif mutation == "run":
        step["provenance"]["discovery_run_id"] = "0" * 26
    elif mutation == "model":
        data["provenance"]["models_used"] = []
    with pytest.raises(ValidationError):
        c.Capability.model_validate(data)


def test_read_value_requires_matching_output() -> None:
    data = capability().model_dump()
    step = data["steps"][0]
    step["action"] = a.ReadValue(output_name="member_id").model_dump()
    with pytest.raises(ValidationError, match="OutputSpec"):
        c.Capability.model_validate(data)
    candidate = ladder().candidates[0]
    output = c.OutputSpec(
        name="member_id",
        json_type="string",
        produced_by_step_id="enter-member",
        extractor=c.ExtractorSpec(
            target=AxTarget(role="textbox", name_matcher=candidate.value.name_matcher),
            attribute="value",
        ),
        nullable=False,
        sensitivity="internal",
        description="Synthetic field",
    )
    data["outputs"] = [output.model_dump()]
    assert c.Capability.model_validate(data)
    data["outputs"][0]["produced_by_step_id"] = "missing"
    with pytest.raises(ValidationError, match="ReadValue step"):
        c.Capability.model_validate(data)


def test_action_target_and_irreversible_retry_invariants() -> None:
    data = capability().steps[0].model_dump()
    data["target"] = None
    with pytest.raises(ValidationError, match="requires a target"):
        s.Step.model_validate(data)
    data = capability().steps[0].model_dump()
    data["risk"] = "irreversible"
    data["timing"]["retry"]["max_attempts"] = 2
    with pytest.raises(ValidationError, match="exactly one"):
        s.Step.model_validate(data)


def test_registry_and_validation_errors() -> None:
    with pytest.raises(ValidationError, match="exactly once"):
        models.ModelRegistry(profiles=())
    with pytest.raises(ValidationError, match="Invalid parameter regex"):
        c.RegexValidation(pattern="[")
    with pytest.raises(ValidationError, match="minimum"):
        c.RangeValidation(minimum=5, maximum=1)
    target = capability().steps[0].checkpoint.target
    with pytest.raises(ValidationError, match="extraction regex"):
        c.ExtractorSpec(target=target, attribute="name", regex="[")


@pytest.mark.parametrize(
    "rule,json_type,example",
    [
        (c.RangeValidation(minimum=0, maximum=2), "number", v.NumberValue(value=3)),
        (c.RangeValidation(minimum=0, maximum=2), "string", v.StringValue(value="1")),
        (c.RegexValidation(pattern=".*"), "number", v.NumberValue(value=1)),
        (
            c.EnumValidation(values=(v.StringValue(value="allowed"),)),
            "string",
            v.StringValue(value="other"),
        ),
        (c.EnumValidation(values=(v.NumberValue(value=1),)), "string", None),
        (None, "integer", v.NumberValue(value=1.5)),
    ],
)
def test_parameter_validation_matrix(rule: Any, json_type: str, example: Any) -> None:
    data = capability().inputs[0].model_dump()
    data.update(validation=rule, json_type=json_type, example=example)
    with pytest.raises(ValidationError):
        c.ParamSpec.model_validate(data)


def test_evidence_reference_matches_committed_fixture(golden_file: Any) -> None:
    root = Path(__file__).resolve().parents[3]
    path = root / "tests/golden/ax-alpha.json"
    content = golden_file(path, (observation_json(observation()) + "\n").encode())
    assert (
        capability().steps[0].target.candidates[0].evidence_ref.content_hash
        == hashlib.sha256(content).hexdigest()
    )


def test_emitter_rejects_open_objects() -> None:
    class BadInput(v.DomainModel):
        open_values: dict[str, str]

    with pytest.raises(ValueError, match="open-keyed"):
        strict_schema(BadInput, "bad-input")


@pytest.mark.parametrize("reason", ["completed", "length", "refusal", "error"])
@pytest.mark.parametrize("has_action", [False, True])
def test_noncompleted_decisions_cannot_execute(reason: str, has_action: bool) -> None:
    data = {
        "decision_id": "0" * 26,
        "intent": "Synthetic decision",
        "action": {"kind": "click"} if has_action else None,
        "target": None,
        "model": capability().provenance.models_used[0].model_dump(),
        "prompt_template_id": "test.v1",
        "prompt_hash": "a" * 64,
        "rationale_digest": None,
        "tool_call_id": "fake",
        "usage": {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0},
        "latency_ms": 0,
        "finish_reason": reason,
    }
    if has_action == (reason == "completed"):
        assert models.DecisionResult.model_validate(data)
    else:
        with pytest.raises(ValidationError, match="completed decisions"):
            models.DecisionResult.model_validate(data)
