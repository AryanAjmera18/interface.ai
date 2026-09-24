"""Verify committed same-session handoff evidence and actor labeling."""

import json
from pathlib import Path

import pytest

from cua.observability.journal import JournalRecord, verify_chain_directory
from cua.target_app.state import seed_members

pytestmark = pytest.mark.integration


def sensitive_probes() -> tuple[bytes, ...]:
    """Build leak probes without storing credential markers or synthetic identity literals."""
    member = next(item for item in seed_members() if item.member_id == "10023")
    return (
        member.name.encode(),
        member.address.encode(),
        " ".join(member.address.split()).encode(),
        b"Author" + b"ization:",
        b"Bear" + b"er ",
        b"s" + b"k-",
        b"open a new savings sub-account" + b" for member",
    )


@pytest.mark.parametrize(
    ("name", "status"),
    [("escalation-replay-attempt-001", "hard_failure"), ("escalation-replay", "success")],
)
def test_replay_handoff_bundle_is_intact(name: str, status: str) -> None:
    root = Path("evidence") / name
    manifest = json.loads((root / "manifest.json").read_bytes())
    assert manifest["result_summary"]["status"] == status
    assert verify_chain_directory(root, manifest["context"]["run_id"]).intact


def test_successful_handoff_records_named_actor_and_ax_diff() -> None:
    root = Path("evidence/escalation-replay")
    records = tuple(
        JournalRecord.model_validate_json(line)
        for line in (root / "journal.ndjson").read_bytes().splitlines()
    )
    by_type = {record.type: record for record in records}
    assert {
        "EscalationRaised",
        "ControlTransferred",
        "HumanAction",
        "ControlReturned",
        "HumanChangeSummary",
    } <= set(by_type)
    for kind in ("ControlTransferred", "HumanAction", "ControlReturned", "HumanChangeSummary"):
        assert by_type[kind].payload["actor"] == "scripted-operator"
    assert by_type["HumanChangeSummary"].payload["changes"]
    assert records[-1].type == "RunEnded" and records[-1].payload["result"] == "success"


def test_escalation_metadata_excludes_raw_fixture_values() -> None:
    forbidden = sensitive_probes()
    for root in (
        Path("evidence/escalation-replay-attempt-001"),
        Path("evidence/escalation-replay"),
    ):
        for name in ("journal.ndjson", "manifest.json", "spans.otlp.jsonl"):
            payload = (root / name).read_bytes()
            assert not any(value in payload for value in forbidden), f"{root}/{name} leaked"


def test_discovery_handoff_bundle_records_irreversible_transfer() -> None:
    root = Path("evidence/escalation-discovery")
    manifest = json.loads((root / "manifest.json").read_bytes())
    records = tuple(
        JournalRecord.model_validate_json(line)
        for line in (root / "journal.ndjson").read_bytes().splitlines()
    )
    assert manifest["result_summary"]["status"] == "success"
    assert manifest["policy_decisions"]["escalated"] == 1
    assert verify_chain_directory(root, manifest["context"]["run_id"]).intact
    by_type = {record.type: record for record in records}
    assert by_type["PolicyDecision"].payload["rule"] == "navigation.read-and-control"
    escalated = next(
        record
        for record in records
        if record.type == "PolicyDecision" and record.payload["verdict"] == "escalate"
    )
    assert escalated.payload["rule"] == "account.confirm-submit"
    for kind in ("ControlTransferred", "HumanAction", "ControlReturned", "HumanChangeSummary"):
        assert by_type[kind].payload["actor"] == "scripted-operator"
    assert by_type["HumanChangeSummary"].payload["changes"]
    assert records[-1].type == "RunEnded" and records[-1].payload["result"] == "success"


def test_discovery_publication_excludes_checkpoint_and_sensitive_identity_metadata() -> None:
    root = Path("evidence/escalation-discovery")
    assert not tuple(root.glob("checkpoints.sqlite*"))
    forbidden = sensitive_probes()
    for name in ("journal.ndjson", "manifest.json", "spans.otlp.jsonl"):
        payload = (root / name).read_bytes()
        assert not any(value in payload for value in forbidden), f"{root}/{name} leaked"
