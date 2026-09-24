"""Verify the real-browser cross-tenant before/after evidence."""

import json
from pathlib import Path

import pytest

from cua.observability.journal import verify_chain_directory

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    ("directory", "status"),
    (("cross-tenant-before", "hard_failure"), ("cross-tenant-after", "success")),
)
def test_cross_tenant_bundle_is_intact(directory: str, status: str) -> None:
    bundle = Path("evidence") / directory
    manifest = json.loads((bundle / "manifest.json").read_bytes())
    assert manifest["context"]["tenant_id"] == "beta"
    assert manifest["result_summary"]["status"] == status
    assert manifest["chain"]["intact"] is True
    assert manifest["evidence_verified"] is True
    assert verify_chain_directory(bundle, manifest["context"]["run_id"]).intact


def test_cross_tenant_before_fails_at_recorded_alpha_checkpoint() -> None:
    manifest = json.loads(Path("evidence/cross-tenant-before/manifest.json").read_bytes())
    assert manifest["result_summary"]["failure_kind"] == "checkpoint"
    assert manifest["result_summary"]["failure_step"] == "01M2YKF4E7WCSXFNYNF98GVJFP"


def test_cross_tenant_metadata_is_redacted() -> None:
    forbidden = (b"10023", b"$13328.23")
    for directory in ("cross-tenant-before", "cross-tenant-after"):
        for name in ("journal.ndjson", "manifest.json", "spans.otlp.jsonl"):
            payload = (Path("evidence") / directory / name).read_bytes()
            assert not any(item in payload for item in forbidden)
