"""Guard provider schema parity, strictness and golden versions; forbid live provider calls."""

import hashlib
import json
import subprocess
import sys
from collections.abc import Callable
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
from tests.unit.domain.demo_sample import demo_capability
from tests.unit.domain.samples import capability
from tests.unit.domain.strict_schema import assert_openai_strict, validate_instance
from tests.unit.domain.test_contracts import model_samples

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    "document",
    [doc for doc in public_schemas() if doc.name != "replay-result.v2"],
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
    document = next(doc for doc in public_schemas() if doc.name == "replay-result.v2")
    with pytest.raises(AssertionError):
        assert_openai_strict(document)
    body = json.loads(document.text())
    assert body["$defs"]["Success"]["properties"]["outputs"]["additionalProperties"] is not False


def test_strict_schema_canonicalizes_semantically_unordered_enums() -> None:
    document = strict_schema(Capability, "capability.v2")

    def assert_sorted(node: Any) -> None:
        if isinstance(node, list):
            for child in node:
                assert_sorted(child)
        elif isinstance(node, dict):
            enum_values = node.get("enum")
            if isinstance(enum_values, list):
                assert enum_values == sorted(enum_values, key=lambda value: json.dumps(value))
            for child in node.values():
                assert_sorted(child)

    assert_sorted(document.schema_body)


def test_golden_artifact_byte_stability(golden_file: Callable[[Path, bytes], bytes]) -> None:
    path = ROOT / "tests/golden/capability.v2.json"
    recorded = golden_file(path, artifact_json(demo_capability()).encode())
    assert artifact_json(demo_capability()).encode() == recorded
    assert artifact_json(Capability.model_validate_json(recorded)).encode() == recorded


def test_schema_emission_and_frozen_version(
    tmp_path: Path, golden_file: Callable[[Path, bytes], bytes]
) -> None:
    result = CliRunner().invoke(app, ["schema", "emit", "--output", str(tmp_path)])
    assert result.exit_code == 0, result.exception
    # v2 is the current candidate schema. v1 is historical and its separate guard never updates.
    expected_manifest = {
        doc.name + ".json": hashlib.sha256(doc.text().encode()).hexdigest()
        for doc in public_schemas()
    }
    manifest = json.loads(
        golden_file(
            ROOT / "tests/golden/schema-digests.v2.json",
            (json.dumps(expected_manifest, indent=2) + "\n").encode(),
        )
    )
    for document in public_schemas():
        name = document.name + ".json"
        emitted = (tmp_path / name).read_bytes()
        assert emitted == document.text().encode()
        if name.endswith(".v2.json"):
            golden_file(ROOT / "docs/schema" / name, emitted)
        else:
            assert emitted == (ROOT / "docs/schema" / name).read_bytes()
        assert hashlib.sha256(emitted).hexdigest() == manifest[name], (
            "Published v1 schema changed: introduce a new version instead of regenerating v1."
        )


def test_model_config_and_environment_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for role in ModelRole:
        monkeypatch.delenv(f"CUA_MODEL_{role.name}", raising=False)
    configured = load_model_registry(ROOT / "config/models.yaml")
    assert configured.get(ModelRole.DISCOVERY_PLANNER).model.model_id == "gpt-6-astra"
    assert configured.get(ModelRole.DISCOVERY_PLANNER).model.reasoning_effort == "medium"
    assert configured.get(ModelRole.EXTRACTOR).model.model_id == "gpt-5.6-luna"
    assert configured.get(ModelRole.CATALOG_AGENT).model.provider == "fake"
    assert load_model_registry(tmp_path / "absent.yaml") == fake_registry()
    model = fake_registry().profiles[0].model.model_dump()
    model.update(provider="openai", model_id="explicit-test-model", api_flavor="responses")
    monkeypatch.setenv("CUA_MODEL_DISCOVERY_PLANNER", json.dumps(model))
    loaded = load_model_registry(ROOT / "config/models.yaml")
    assert loaded.get(ModelRole.DISCOVERY_PLANNER).model.provider == "openai"
    assert loaded.get(ModelRole.EXTRACTOR).model.provider == "openai"
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
