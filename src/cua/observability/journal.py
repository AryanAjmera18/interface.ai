"""Persist typed, redacted, hash-chained history; forbid higher-layer imports."""

import threading
from pathlib import Path
from typing import cast

from pydantic import AwareDatetime, Field, JsonValue, TypeAdapter, ValidationError

from cua.domain.common import ULID, Digest, DomainModel, canonical_json, digest
from cua.domain.ports import Clock, JournalEvent
from cua.observability._io import WriterLock, write_bytes
from cua.observability.redaction import TaggedValue, checked_json, redact

EVENTS: TypeAdapter[JournalEvent] = TypeAdapter(JournalEvent)


class JournalRecord(DomainModel):
    seq: int = Field(ge=1)
    run_id: ULID
    ts: AwareDatetime
    type: str
    payload: JsonValue
    prev_hash: Digest | None
    hash: Digest


class JournalHead(DomainModel):
    run_id: ULID
    seq: int = Field(ge=0)
    hash: Digest | None


class ChainReport(DomainModel):
    intact: bool
    break_at_seq: int | None
    reason: str


class BrokenChainError(ValueError):
    """Do not append over corrupt history; retain the run for investigation."""


def verify_chain(root: Path, run_id: str) -> ChainReport:
    """Verify a run stored under its canonical run-id directory."""
    return verify_chain_directory(root / run_id, run_id)


def verify_chain_directory(directory: Path, run_id: str) -> ChainReport:
    """Compare a bundle chain with its separately persisted head anchor.

    The direct-directory form supports exported bundles whose friendly directory name differs from
    the run ULID. This remains tamper-evidence, not authenticity against an adversarial writer.
    """
    path, anchor = directory / "journal.ndjson", directory / "journal.head.json"
    seq, previous = 0, None
    try:
        if not anchor.exists():
            return ChainReport(intact=False, break_at_seq=1, reason="Missing journal head anchor")
        expected = JournalHead.model_validate_json(anchor.read_bytes())
        if expected.run_id != run_id:
            raise ValueError("Head belongs to another run")
        if not path.exists():
            raise ValueError("Missing journal file")
        with path.open("rb") as stream:
            for line in stream:
                next_seq = seq + 1
                if not line.endswith(b"\n"):
                    raise ValueError("Incomplete journal record")
                record = JournalRecord.model_validate_json(line)
                if record.seq != next_seq or record.run_id != run_id:
                    raise ValueError("Sequence gap, reordered record, or wrong run")
                if record.prev_hash != previous:
                    raise ValueError("Previous hash mismatch")
                if digest(record.model_dump(mode="json", exclude={"hash"})) != record.hash:
                    raise ValueError("Record content hash mismatch")
                seq, previous = record.seq, record.hash
        if (seq, previous) != (expected.seq, expected.hash):
            raise ValueError(
                "Tail differs from the committed head (truncation or interrupted append)"
            )
    except (ValueError, OSError, ValidationError) as error:
        # Never persist exception text: JSON parsing messages can contain the original field.
        reason = str(error) if type(error) is ValueError else "Unreadable or malformed journal"
        return ChainReport(intact=False, break_at_seq=seq + 1, reason=reason)
    return ChainReport(intact=True, break_at_seq=None, reason="Chain and local head agree")


class RunJournal:
    """Exactly one writer per run, enforced by an OS lock and an intra-process RLock.

    record() is the typed redaction gateway; append() is the strict sink accepting only sealed
    redactor output. Deserialized events never regain a trust tag just by claiming to be safe.
    """

    def __init__(self, root: Path, run_id: str, *, clock: Clock, strict: bool = True) -> None:
        self.head = JournalHead(run_id=run_id, seq=0, hash=None)
        self.root, self.clock, self.strict = root, clock, strict
        self.directory = root / self.head.run_id
        self.path = self.directory / "journal.ndjson"
        self.anchor = self.directory / "journal.head.json"
        self._mutex = threading.RLock()
        self._lock = WriterLock(self.directory / "journal.lock")
        try:
            if not self.path.exists() and not self.anchor.exists():
                write_bytes(self.path, redact(b""))
                self._save_head(self.head)
            self._assert_intact()
            self.head = JournalHead.model_validate_json(self.anchor.read_bytes())
        except BaseException:
            self.close()
            raise

    def _save_head(self, head: JournalHead) -> None:
        write_bytes(self.anchor, redact(canonical_json(head.model_dump(mode="json")).encode()))

    def _assert_intact(self) -> None:
        report = self.verify_chain()
        if not report.intact:
            raise BrokenChainError(
                f"Journal broken at sequence {report.break_at_seq}: {report.reason}"
            )

    def close(self) -> None:
        self._lock.close()

    def verify_chain(self) -> ChainReport:
        with self._mutex:
            return verify_chain(self.root, self.head.run_id)

    def record(self, event: JournalEvent) -> JournalRecord:
        validated = EVENTS.validate_json(event.model_dump_json())
        return self.append(redact(validated))

    def append(self, event: TaggedValue) -> JournalRecord:
        value = checked_json(event, strict=self.strict)
        if not isinstance(value, dict) or not isinstance(value.get("type"), str):
            raise ValueError("Journal event needs a literal type discriminator")
        # The public strict sink also rejects unknown event types after redaction.
        allowed = {variant["const"] for variant in _event_tags()}
        if value["type"] not in allowed:
            raise ValueError("Unknown journal event type")
        with self._mutex:
            if self._lock.stream.closed:
                raise RuntimeError("Journal is closed")
            self._assert_intact()
            fields = {
                "seq": self.head.seq + 1,
                "run_id": self.head.run_id,
                "ts": self.clock.now().isoformat().replace("+00:00", "Z"),
                "type": value["type"],
                "payload": {key: child for key, child in value.items() if key != "type"},
                "prev_hash": self.head.hash,
            }
            record = JournalRecord.model_validate(
                {**fields, "hash": digest(cast(JsonValue, fields))}
            )
            encoded = (canonical_json(record.model_dump(mode="json")) + "\n").encode()
            # Already-redacted payloads retain their markers; structural metadata is scanned too.
            write_bytes(self.path, redact(encoded), append=True)
            head = JournalHead(run_id=self.head.run_id, seq=record.seq, hash=record.hash)
            self._save_head(head)
            self.head = head
            return record

    def records(self) -> tuple[JournalRecord, ...]:
        with self._mutex:
            self._assert_intact()
            return tuple(
                JournalRecord.model_validate_json(line)
                for line in self.path.read_bytes().splitlines()
            )


def _event_tags() -> list[dict[str, JsonValue]]:
    schema = EVENTS.json_schema()
    return [
        cast(dict[str, JsonValue], definition["properties"]["type"])
        for definition in schema["$defs"].values()
        if "type" in definition.get("properties", {})
        and "const" in definition["properties"]["type"]
    ]


class JournalAdapter:
    def __init__(self, journal: RunJournal) -> None:
        self.journal = journal

    async def append(self, entry: JournalEvent) -> str:
        return self.journal.record(entry).hash
