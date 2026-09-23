"""Treat real-browser replay bundles as executable evidence fixtures."""

import json
from pathlib import Path
from typing import Any, cast

import pytest

from cua.observability.journal import JournalRecord, verify_chain_directory

pytestmark = pytest.mark.integration
ROOT = Path("evidence")
EXPECTED = {
    "replay-success": "success",
    "replay-not-found": "business",
    "replay-permission-denied": "business",
    "replay-recovered": "success",
    "replay-session-recovered": "success",
    "replay-hard-failure": "hard_failure",
    "replay-slow": "success",
    "replay-invalid-input": "hard_failure",
}


@pytest.mark.parametrize(("directory", "status"), EXPECTED.items())
def test_committed_replay_bundle_verifies(directory: str, status: str) -> None:
    bundle = ROOT / directory
    manifest = json.loads((bundle / "manifest.json").read_bytes())
    assert manifest["result_summary"]["status"] == status
    assert manifest["context"]["parent_run_id"] == "01M2YKF25VVM1RVNVNV97TG07H"
    assert manifest["context"]["capability_content_digest"] == (
        "234a5cc673d858de22fff897edc269af902b0d2f8b2a98d447f1edcbbdc9e4fa"
    )
    assert manifest["evidence_verified"] is True
    assert verify_chain_directory(bundle, manifest["context"]["run_id"]).intact


def test_recovery_and_failure_history_is_explicit() -> None:
    recovered = _records(ROOT / "replay-recovered")
    session = _records(ROOT / "replay-session-recovered")
    failed = _records(ROOT / "replay-hard-failure")
    assert any(record.type == "Recovered" for record in recovered)
    assert any(record.type == "Recovered" for record in session)
    terminal = next(record for record in failed if record.type == "RunEnded")
    payload = cast(dict[str, Any], terminal.payload)
    assert payload["result"] == "hard_failure"
    assert payload["failure_kind"] == "server"
    assert payload["failure_step"]
    assert payload["failure_reason"]


def test_committed_replay_metadata_contains_no_raw_fixture_values() -> None:
    forbidden = (b"10023", b"99999", b"10050", b"$13328.23")
    for directory in EXPECTED:
        bundle = ROOT / directory
        for name in ("journal.ndjson", "manifest.json", "spans.otlp.jsonl"):
            payload = (bundle / name).read_bytes()
            assert not any(value in payload for value in forbidden), f"{directory}/{name} leaked"


def test_every_acted_step_records_locator_attempts() -> None:
    records = _records(ROOT / "replay-success")
    actions = [record for record in records if record.type == "ActionAttempted"]
    resolutions = [record for record in records if record.type == "LocatorResolved"]
    assert len(actions) == len(resolutions) == 6
    for record in resolutions:
        payload = cast(dict[str, Any], record.payload)
        assert payload["winning_index"] is not None
        assert payload["attempts"]


def _records(bundle: Path) -> tuple[JournalRecord, ...]:
    return tuple(
        JournalRecord.model_validate_json(line)
        for line in (bundle / "journal.ndjson").read_bytes().splitlines()
    )
