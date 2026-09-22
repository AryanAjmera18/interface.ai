"""Compiler and provenance verifier tests use the committed real run offline."""

import shutil
from pathlib import Path

from cua.discovery.compiler import capability_bytes, compile_run
from cua.discovery.verifier import RunProvenanceVerifier

RUN = Path("evidence/discovery-010")


def test_compiler_is_deterministic_and_canonicalizes_sensitive_values() -> None:
    first = compile_run(RUN)
    second = compile_run(RUN)
    assert capability_bytes(first) == capability_bytes(second)
    document = capability_bytes(first)
    assert b"10023" not in document
    assert b'"value": "synthetic"' not in document
    assert first.steps[2].action.value_ref.kind == "param_ref"  # type: ignore[union-attr]
    assert first.steps[2].target is not None
    assert len(first.steps[2].target.candidates) >= 4
    assert first.outputs[0].extractor.target.within is not None


def test_compiled_artifact_verifies_and_manual_edit_is_named() -> None:
    capability = compile_run(RUN)
    report = RunProvenanceVerifier(RUN).verify(capability)
    assert report.verified
    target = capability.steps[2].target
    assert target is not None
    first = target.candidates[0].model_copy(update={"rationale": "manual edit"})
    ladder = target.model_copy(update={"candidates": (first, *target.candidates[1:])})
    step = capability.steps[2].model_copy(update={"target": ladder})
    edited = capability.model_copy(
        update={"steps": (*capability.steps[:2], step, *capability.steps[3:])}
    )
    report = RunProvenanceVerifier(RUN).verify(edited)
    assert not report.verified
    assert any(
        not check.passed and "deterministic journal compilation" in check.asserted
        for check in report.checks
    )


def test_missing_blob_fails_verification(tmp_path: Path) -> None:
    copied = tmp_path / "run"
    shutil.copytree(RUN, copied)
    capability = compile_run(copied)
    target = capability.steps[0].target
    assert target is not None
    reference = target.candidates[0].evidence_ref
    (copied / "blobs" / reference.content_hash[:2] / reference.content_hash).unlink()
    report = RunProvenanceVerifier(copied).verify(capability)
    assert not report.verified
    assert any(not check.passed and check.found == "missing" for check in report.checks)
