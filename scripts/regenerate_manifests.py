"""Append visible corrections and regenerate historical manifests from their journals."""

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import yaml

from cua.domain.common import canonical_json
from cua.domain.models import ModelRegistry, ModelRole
from cua.domain.ports import Clock, EvidenceRelabeled, ManifestRegenerated
from cua.observability._io import write_bytes
from cua.observability.evidence import EvidenceIndex
from cua.observability.journal import RunJournal, verify_chain_directory
from cua.observability.manifest import regenerate_manifest_from_journal
from cua.observability.redaction import redact

GOAL = "look up member 10023 and read their current savings balance"


class SystemClock(Clock):
    def now(self) -> datetime:
        return datetime.now(UTC)


def main() -> None:
    root = Path("evidence")
    registry = ModelRegistry.model_validate(yaml.safe_load(Path("config/models.yaml").read_text()))
    model = registry.get(ModelRole.DISCOVERY_PLANNER).model
    first = root / "discovery-001"
    first_head = json.loads((first / "journal.head.json").read_bytes())
    first_run_id = str(first_head["run_id"])
    if not verify_chain_directory(first, first_run_id).intact:
        raise ValueError("discovery-001: broken chain")
    first_index = EvidenceIndex.model_validate_json((first / "index.json").read_bytes())
    corrected_first = []
    first_journal = RunJournal(root, first_run_id, clock=SystemClock(), directory=first)
    try:
        for entry in first_index.entries:
            if entry.media_type.startswith("image/png") and entry.kind != "screenshot":
                first_journal.record(
                    EvidenceRelabeled(
                        evidence_id=entry.evidence_id,
                        original_kind=entry.kind,
                        corrected_kind="screenshot",
                        original_media_type=entry.media_type,
                        corrected_media_type="image/png",
                        reason="Legacy full-viewport PNG was mislabeled as AX evidence",
                    )
                )
                corrected_first.append(
                    entry.model_copy(
                        update={
                            "kind": "screenshot",
                            "media_type": "image/png",
                            "redaction": "full_viewport",
                            "redaction_reason": "legacy blank screenshot; no raw pixels retained",
                        }
                    )
                )
            else:
                corrected_first.append(entry)
    finally:
        first_journal.close()
    write_bytes(
        first / "index.json",
        redact(
            canonical_json(
                EvidenceIndex(run_id=first_index.run_id, entries=tuple(corrected_first)).model_dump(
                    mode="json"
                )
            ).encode()
        ),
    )
    if not verify_chain_directory(first, first_run_id).intact:
        raise ValueError("discovery-001: broken chain after correction")
    for number in range(2, 11):
        bundle = root / f"discovery-{number:03d}"
        manifest_path = bundle / "manifest.json"
        if not manifest_path.exists():
            continue
        old = json.loads(manifest_path.read_bytes())
        run_id = str(old["context"]["run_id"])
        before = verify_chain_directory(bundle, run_id)
        if not before.intact:
            raise ValueError(f"{bundle}: broken chain before regeneration: {before.reason}")
        index = EvidenceIndex.model_validate_json((bundle / "index.json").read_bytes())
        journal = RunJournal(root, run_id, clock=SystemClock(), directory=bundle)
        try:
            prior_regeneration = any(
                record.type == "ManifestRegenerated" for record in journal.records()
            )
            completed = any(
                record.type == "ManifestRegenerated"
                and isinstance(record.payload, dict)
                and record.payload.get("reason") == "Published-evidence verification correction"
                for record in journal.records()
            )
            if completed:
                continue
            updated = []
            for entry in index.entries:
                if entry.media_type.startswith("image/png") and (
                    entry.kind != "screenshot" or entry.media_type != "image/png"
                ):
                    journal.record(
                        EvidenceRelabeled(
                            evidence_id=entry.evidence_id,
                            original_kind=entry.kind,
                            corrected_kind="screenshot",
                            original_media_type=entry.media_type,
                            corrected_media_type="image/png",
                            reason="Legacy full-viewport PNG was mislabeled as AX evidence",
                        )
                    )
                    updated.append(
                        entry.model_copy(
                            update={
                                "kind": "screenshot",
                                "media_type": "image/png",
                                "redaction": "full_viewport",
                                "masked_regions": (),
                                "redaction_reason": (
                                    "legacy blank screenshot; no raw pixels retained"
                                ),
                            }
                        )
                    )
                else:
                    updated.append(entry)
            new_index = EvidenceIndex(run_id=index.run_id, entries=tuple(updated))
            write_bytes(
                bundle / "index.json",
                redact(canonical_json(new_index.model_dump(mode="json")).encode()),
            )
            journal.record(
                ManifestRegenerated(
                    attempt_directory=bundle.name,
                    reason=(
                        "Published-evidence verification correction"
                        if prior_regeneration
                        else "Typed summary, null unknown usage, and evidence metadata correction"
                    ),
                    goal=GOAL,
                    goal_source="reviewer_reconstruction",
                    configured_model=model,
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
        regenerated = regenerate_manifest_from_journal(bundle, published_blob_hashes=published)
        after = verify_chain_directory(bundle, run_id)
        if not after.intact:
            raise ValueError(f"{bundle}: broken chain after regeneration: {after.reason}")
        if regenerated.journal_head_hash is None:
            raise ValueError(f"{bundle}: regeneration produced no journal head")
    source = root / "discovery-010" / "journal.ndjson"
    Path("tests/fixtures/real_run_001.ndjson").write_bytes(source.read_bytes())


if __name__ == "__main__":
    main()
