"""Label and append the missing target fingerprint without pretending it was observed then."""

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from cua.domain.observation import SurfaceFingerprint
from cua.domain.ports import Clock, ManifestRegenerated, TargetMetadataReconstructed
from cua.observability.journal import RunJournal, verify_chain_directory
from cua.observability.manifest import regenerate_manifest_from_journal
from cua.target_app.config import ALPHA, SurfaceVersion


class SystemClock(Clock):
    def now(self) -> datetime:
        return datetime.now(UTC)


def main() -> None:
    bundle = Path("evidence/discovery-010")
    old = json.loads((bundle / "manifest.json").read_bytes())
    run_id = str(old["context"]["run_id"])
    if not verify_chain_directory(bundle, run_id).intact:
        raise ValueError("Original journal chain is broken")
    journal = RunJournal(bundle.parent, run_id, clock=SystemClock(), directory=bundle)
    try:
        if any(record.type == "TargetMetadataReconstructed" for record in journal.records()):
            return
        version = SurfaceVersion(tenant_id="alpha", config_hash=ALPHA.config_hash)
        fingerprint = SurfaceFingerprint.model_validate(
            {**version.model_dump(), "observed_at": SystemClock().now()}
        )
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
        journal.record(
            TargetMetadataReconstructed(
                fingerprint=fingerprint,
                entry_point="http://127.0.0.1:8099/t/alpha/",
                source_commit=revision,
                reason=(
                    "Original Observed events omitted fingerprint and URL; "
                    "reconstructed from the synthetic target fixture at this commit"
                ),
            )
        )
        journal.record(
            ManifestRegenerated(
                attempt_directory=bundle.name,
                reason="Append explicitly sourced target metadata for deterministic compilation",
                goal=old["inputs"]["goal"],
                goal_source="reviewer_reconstruction",
            )
        )
    finally:
        journal.close()
    tracked = subprocess.run(
        ["git", "ls-files", "--", str(bundle / "blobs")],
        capture_output=True,
        text=True,
        check=True,
    )
    published = frozenset(Path(line).name for line in tracked.stdout.splitlines())
    regenerate_manifest_from_journal(bundle, published_blob_hashes=published)
    if not verify_chain_directory(bundle, run_id).intact:
        raise ValueError("Amended journal chain is broken")
    Path("tests/fixtures/real_run_001.ndjson").write_bytes((bundle / "journal.ndjson").read_bytes())


if __name__ == "__main__":
    main()
