"""Verify the retained catalog invocation and linked model-free replay."""

import json
from pathlib import Path

import pytest

from cua.catalog.openai import CatalogRunManifest
from cua.observability.journal import verify_chain_directory

pytestmark = pytest.mark.integration


def test_catalog_invocation_is_redacted_and_linked() -> None:
    root = Path("evidence/catalog-invoke")
    manifest = CatalogRunManifest.model_validate_json((root / "manifest.json").read_bytes())
    replay = json.loads((root / manifest.linked_replay).read_bytes())
    assert manifest.model.model_id == "gpt-5.6-luna"
    assert manifest.tool_name == "look_up_member_savings_balance"
    assert manifest.cost_usd == pytest.approx(0.0000444)
    assert isinstance(manifest.question, dict) and manifest.question["redacted"] is True
    assert isinstance(manifest.arguments, dict) and manifest.arguments["redacted"] is True
    assert isinstance(manifest.tool_result, dict) and manifest.tool_result["redacted"] is True
    assert replay["result_summary"]["status"] == "success"
    replay_dir = root / "replay"
    assert verify_chain_directory(replay_dir, replay["context"]["run_id"]).intact


def test_catalog_publication_contains_no_raw_request_or_result() -> None:
    forbidden = (b"10023", b"$13328.23", b"What is member")
    root = Path("evidence/catalog-invoke")
    for path in (
        root / "manifest.json",
        root / "replay/journal.ndjson",
        root / "replay/manifest.json",
    ):
        payload = path.read_bytes()
        assert not any(value in payload for value in forbidden), f"{path} leaked catalog data"
