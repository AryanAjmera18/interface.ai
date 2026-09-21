"""Persist redacted content-addressed evidence; forbid surface and higher-layer imports."""

import hashlib
import threading
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field

from cua.domain.common import ULID, Digest, DomainModel, EvidenceRef, canonical_json
from cua.domain.observation import AxNode, Bounds
from cua.domain.ports import Clock, EvidencePayload, IdGenerator, NodeAddress
from cua.observability._io import WriterLock, write_bytes
from cua.observability.redaction import (
    RedactedBytes,
    masked_ax_nodes,
    redact,
    redact_screenshot,
    require_tag,
)

EvidenceKind = Literal[
    "screenshot", "ax_snapshot", "dom_digest", "network_summary", "operator_note"
]


class StoredEvidenceRef(DomainModel):
    """Store metadata is separate from the frozen v2 artifact's opaque EvidenceRef.

    to_artifact_ref preserves its existing wire format; neither schema drift nor a path string
    is needed to resolve the blob. The hash always covers the bytes actually persisted.
    """

    sha256: Digest
    media_type: str
    bytes_len: int = Field(ge=0)
    kind: EvidenceKind
    stored_at: AwareDatetime
    evidence_id: ULID
    redaction: Literal["none", "region_masked", "full_viewport"] = "none"
    masked_regions: tuple[Bounds, ...] = ()
    redaction_reason: str | None = None
    observation_hash: Digest | None = None
    observation_id: ULID | None = None

    def to_artifact_ref(self) -> EvidenceRef:
        return EvidenceRef(
            evidence_id=self.evidence_id, content_hash=self.sha256, media_type=self.media_type
        )


class EvidenceIndex(DomainModel):
    run_id: ULID
    entries: tuple[StoredEvidenceRef, ...] = ()


class EvidenceStore:
    def __init__(
        self, root: Path, run_id: str, *, clock: Clock, ids: IdGenerator, strict: bool = True
    ) -> None:
        self.index = EvidenceIndex(run_id=run_id)
        self.root = root / self.index.run_id
        self.clock, self.ids, self.strict = clock, ids, strict
        self._mutex = threading.RLock()
        self._lock = WriterLock(self.root / "evidence.lock")
        try:
            if self.index_path.exists():
                self.index = EvidenceIndex.model_validate_json(self.index_path.read_bytes())
                if self.index.run_id != run_id:
                    raise ValueError("Evidence index belongs to another run")
        except BaseException:
            self.close()
            raise

    @property
    def index_path(self) -> Path:
        return self.root / "index.json"

    def close(self) -> None:
        self._lock.close()

    def put(
        self,
        payload: RedactedBytes,
        media_type: str,
        *,
        kind: EvidenceKind,
        redaction: Literal["none", "region_masked", "full_viewport"] = "none",
        masked_regions: tuple[Bounds, ...] = (),
        redaction_reason: str | None = None,
        observation_hash: Digest | None = None,
        observation_id: ULID | None = None,
    ) -> StoredEvidenceRef:
        if not self.strict and isinstance(payload, bytes):
            payload = redact(payload)
        require_tag(payload)
        if not isinstance(payload, RedactedBytes):
            raise TypeError("Evidence requires redact(bytes)")
        media_type = payload.media_type_hint or media_type
        if (
            ";" in media_type
            or (kind == "screenshot" and not media_type.startswith("image/"))
            or (kind == "ax_snapshot" and media_type != "application/json")
        ):
            raise ValueError(f"Evidence media type {media_type!r} is invalid for {kind}")
        with self._mutex:
            if self._lock.stream.closed:
                raise RuntimeError("Evidence store is closed")
            sha = hashlib.sha256(payload.value).hexdigest()
            # Identical bytes with distinct purpose retain index entries but share one blob.
            for entry in self.index.entries:
                if (
                    entry.sha256,
                    entry.media_type,
                    entry.kind,
                    entry.observation_hash,
                    entry.observation_id,
                ) == (sha, media_type, kind, observation_hash, observation_id):
                    if not self.verify(entry):
                        raise ValueError(
                            "Existing evidence is corrupt; preserve it for investigation"
                        )
                    return entry
            reference = StoredEvidenceRef(
                sha256=sha,
                media_type=media_type,
                bytes_len=len(payload.value),
                kind=kind,
                stored_at=self.clock.now(),
                evidence_id=self.ids.new(),
                redaction=redaction,
                masked_regions=masked_regions,
                redaction_reason=redaction_reason,
                observation_hash=observation_hash,
                observation_id=observation_id,
            )
            blob = self._path(reference)
            if blob.exists() and hashlib.sha256(blob.read_bytes()).hexdigest() != sha:
                raise ValueError("Existing blob digest mismatch")
            if not blob.exists():
                write_bytes(blob, payload)
            updated = EvidenceIndex(
                run_id=self.index.run_id, entries=(*self.index.entries, reference)
            )
            write_bytes(
                self.index_path, redact(canonical_json(updated.model_dump(mode="json")).encode())
            )
            self.index = updated
            return reference

    def _path(self, reference: StoredEvidenceRef | EvidenceRef) -> Path:
        sha = (
            reference.sha256 if isinstance(reference, StoredEvidenceRef) else reference.content_hash
        )
        # Revalidate even model_copy/model_construct bypasses, before deriving filesystem paths.
        validated = EvidenceRef(evidence_id="0" * 26, content_hash=sha, media_type="internal")
        return self.root / "blobs" / validated.content_hash[:2] / validated.content_hash

    def get(self, reference: StoredEvidenceRef | EvidenceRef) -> bytes:
        data = self._path(reference).read_bytes()
        sha = (
            reference.sha256 if isinstance(reference, StoredEvidenceRef) else reference.content_hash
        )
        if hashlib.sha256(data).hexdigest() != sha:
            raise ValueError("Evidence blob digest mismatch")
        if isinstance(reference, StoredEvidenceRef) and len(data) != reference.bytes_len:
            raise ValueError("Evidence byte length mismatch")
        return data

    def exists(self, reference: StoredEvidenceRef | EvidenceRef) -> bool:
        return self._path(reference).is_file()

    def verify(self, reference: StoredEvidenceRef | EvidenceRef) -> bool:
        try:
            self.get(reference)
        except (FileNotFoundError, ValueError):
            return False
        return True


class SurfaceEvidenceSink:
    """Redact in-process screenshot bytes before the content-addressed store sees them."""

    def __init__(self, store: EvidenceStore) -> None:
        self.store = store

    def sensitive_bounds_targets(self, root: AxNode) -> tuple[NodeAddress, ...]:
        return tuple((node.frame_path, node.node_path) for node in masked_ax_nodes(root))

    async def put(self, payload: EvidencePayload) -> EvidenceRef:
        if payload.media_type == "image/png":
            if payload.ax_root is None or payload.observation_hash is None:
                raise ValueError("Screenshot needs the AX tree and observation hash")
            safe, mode, regions, reason = redact_screenshot(payload.content, payload.ax_root)
            return self.store.put(
                safe,
                "image/png",
                kind="screenshot",
                redaction=mode,
                masked_regions=regions,
                redaction_reason=reason,
                observation_hash=payload.observation_hash,
                observation_id=payload.observation_id,
            ).to_artifact_ref()
        return self.store.put(
            redact(payload.content),
            payload.media_type,
            kind="ax_snapshot",
            observation_hash=payload.observation_hash,
            observation_id=payload.observation_id,
        ).to_artifact_ref()

    def verify(self, reference: EvidenceRef) -> bool:
        return self.store.verify(reference)
