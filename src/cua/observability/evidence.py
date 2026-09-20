"""Persist redacted content-addressed evidence; forbid surface and higher-layer imports."""

import base64
import hashlib
import threading
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field

from cua.domain.common import ULID, Digest, DomainModel, EvidenceRef, canonical_json
from cua.domain.ports import Clock, EvidencePayload, IdGenerator
from cua.observability._io import WriterLock, write_bytes
from cua.observability.redaction import RedactedBytes, redact, require_tag

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
        self, payload: RedactedBytes, media_type: str, *, kind: EvidenceKind
    ) -> StoredEvidenceRef:
        if not self.strict and isinstance(payload, bytes):
            payload = redact(payload)
        require_tag(payload)
        if not isinstance(payload, RedactedBytes):
            raise TypeError("Evidence requires redact(bytes)")
        with self._mutex:
            if self._lock.stream.closed:
                raise RuntimeError("Evidence store is closed")
            sha = hashlib.sha256(payload.value).hexdigest()
            media_type = payload.media_type_hint or media_type
            # Identical bytes with distinct purpose retain index entries but share one blob.
            for entry in self.index.entries:
                if (entry.sha256, entry.media_type, entry.kind) == (sha, media_type, kind):
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
    """Adapt the existing injected surface port; raw content is sanitized at this gateway.

    PNGs are masked again centrally, even if an adapter claims it already masked them. This
    intentionally sacrifices screenshot detail until a reviewed region-aware redactor exists.
    """

    def __init__(self, store: EvidenceStore) -> None:
        self.store = store

    async def put(self, payload: EvidencePayload) -> EvidenceRef:
        image = payload.media_type == "image/png"
        data = (
            base64.b64decode(payload.content, validate=True) if image else payload.content.encode()
        )
        return self.store.put(
            redact(data), payload.media_type, kind="screenshot" if image else "ax_snapshot"
        ).to_artifact_ref()

    def verify(self, reference: EvidenceRef) -> bool:
        return self.store.verify(reference)
