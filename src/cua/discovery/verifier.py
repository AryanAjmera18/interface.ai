"""Verify artifact provenance against its journal and content-addressed evidence."""

import hashlib
from pathlib import Path
from typing import Any, cast

from cua.discovery.compiler import compile_run
from cua.domain.capability import Capability, capability_content_digest
from cua.domain.common import EvidenceRef
from cua.domain.ports import VerificationCheck, VerificationReport
from cua.observability.journal import JournalRecord, verify_chain_directory


class RunProvenanceVerifier:
    """Resolve every claim against one named run; schema-valid claims alone are insufficient."""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir

    def _records(self) -> tuple[JournalRecord, ...]:
        return tuple(
            JournalRecord.model_validate_json(line)
            for line in (self.run_dir / "journal.ndjson").read_bytes().splitlines()
        )

    def _evidence_check(self, reference: EvidenceRef) -> VerificationCheck:
        path = self.run_dir / "blobs" / reference.content_hash[:2] / reference.content_hash
        found = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "missing"
        return VerificationCheck(
            asserted=reference.content_hash,
            found=found,
            passed=found == reference.content_hash,
        )

    def verify(self, capability: Capability) -> VerificationReport:
        records = self._records()
        chain = verify_chain_directory(self.run_dir, capability.provenance.derived_from_run_id)
        checks = [
            VerificationCheck(
                asserted="intact journal chain", found=chain.reason, passed=chain.intact
            )
        ]
        observations = {
            str(cast(dict[str, Any], record.payload).get("observation_hash"))
            for record in records
            if record.type == "Observed"
        }
        for step in capability.steps:
            claimed = step.provenance.observation_hash or "missing"
            checks.append(
                VerificationCheck(
                    asserted=f"step {step.step_id} observation {claimed}",
                    found="journal observation" if claimed in observations else "missing",
                    passed=claimed in observations,
                )
            )
            if step.target:
                for candidate in step.target.candidates:
                    checks.append(self._evidence_check(candidate.evidence_ref))
            for edit in step.provenance.edits:
                checks.append(
                    VerificationCheck(
                        asserted=f"HumanEdit {edit.field_path} brackets a journaled change",
                        found="no HumanEdit journal event model exists",
                        passed=False,
                    )
                )
        try:
            rebuilt = compile_run(self.run_dir)
            rebuilt_digest = capability_content_digest(rebuilt)
            checks.append(
                VerificationCheck(
                    asserted="artifact content equals deterministic journal compilation",
                    found=rebuilt_digest,
                    passed=rebuilt_digest == capability_content_digest(capability),
                )
            )
        except (OSError, ValueError):
            checks.append(
                VerificationCheck(
                    asserted="artifact content equals deterministic journal compilation",
                    found="recompilation failed because source evidence is missing or invalid",
                    passed=False,
                )
            )
        if capability.status == "approved" and capability.provenance.approval:
            approval = capability.provenance.approval
            matched = any(
                record.type == "CapabilityApproved"
                and cast(dict[str, Any], record.payload).get("reviewed_digest")
                == approval.reviewed_digest
                and cast(dict[str, Any], record.payload).get("actor") == approval.actor
                for record in records
            )
            checks.append(
                VerificationCheck(
                    asserted="content-bound approval is journaled",
                    found="matching approval event" if matched else "missing approval event",
                    passed=matched,
                )
            )
        return VerificationReport(
            verified=all(check.passed for check in checks), checks=tuple(checks)
        )
