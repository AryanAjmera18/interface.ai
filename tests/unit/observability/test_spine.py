"""Exercise redaction, context, evidence, journal, tracing, and manifest failure paths."""

import asyncio
import io
import json
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest
from hypothesis import given
from hypothesis import strategies as st
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from pydantic import JsonValue

from cua.domain.ports import RunEnded, RunStarted
from cua.observability._io import WriterBusyError
from cua.observability.context import current_run, new_run, run_scope
from cua.observability.evidence import EvidenceStore
from cua.observability.journal import BrokenChainError, RunJournal
from cua.observability.logging import StructuredLogger
from cua.observability.manifest import write_manifest
from cua.observability.redaction import (
    RedactedBytes,
    RedactedValue,
    RedactionPolicy,
    RedactionRequiredError,
    checked_json,
    json_value,
    luhn_valid,
    redact,
    redaction_policy,
)
from cua.observability.tracing import Tracing
from tests.unit.domain.samples import FixedClock, SequenceIds, capability


@pytest.mark.asyncio
async def test_context_propagates_to_gather_and_task_group() -> None:
    context = new_run("discovery", clock=FixedClock(), ids=SequenceIds())

    async def identity() -> str:
        await asyncio.sleep(0)
        return current_run().run_id

    with run_scope(context):
        assert await asyncio.gather(identity(), identity()) == [context.run_id] * 2
        values: list[str] = []

        async def collect() -> None:
            values.append(await identity())

        async with asyncio.TaskGroup() as group:
            group.create_task(collect())
            group.create_task(collect())
        assert values == [context.run_id] * 2
    with pytest.raises(RuntimeError):
        current_run()


@given(st.sampled_from(["123-45-6789", "a.person@example.test", "+1 212-555-0199"]))
def test_pii_never_survives_redactor(value: str) -> None:
    result = json_value(redact({"nested": [value]}))
    assert value not in json.dumps(result)
    marker = cast(dict[str, JsonValue], cast(dict[str, JsonValue], result)["nested"])[0]
    assert cast(dict[str, JsonValue], marker)["redacted"] is True


def test_redaction_hash_luhn_and_strict_boundary() -> None:
    first = redact("123-45-6789")
    second = redact("123-45-6789")
    other = redact("987-65-4321")
    assert isinstance(first, RedactedValue) and isinstance(second, RedactedValue)
    assert isinstance(other, RedactedValue)
    assert first.sha256 == second.sha256 != other.sha256
    assert luhn_valid("4111111111111111")
    # This card-width order number fails Luhn and remains intact absent schema classification.
    assert json_value(redact("1234567890123456")) == "1234567890123456"
    with pytest.raises(RedactionRequiredError):
        checked_json({"safe": True})


def test_schema_sensitivity_is_authoritative_and_logs_are_strict() -> None:
    pii = capability().inputs[0].model_copy(update={"sensitivity": "pii"})
    with redaction_policy(RedactionPolicy.from_specs((pii,), ())):
        value = json_value(redact({"inputs": {"member_id": "safe-looking"}}))
    assert "safe-looking" not in json.dumps(value)
    stream = io.StringIO()
    context = new_run("verification", clock=FixedClock(), ids=SequenceIds())
    with run_scope(context):
        logger = StructuredLogger(stream)
        logger.info("verified", field=redact("123-45-6789"))
        with pytest.raises(RedactionRequiredError):
            logger.info("rejected", field=cast(object, "raw"))  # type: ignore[arg-type]
    assert "123-45-6789" not in stream.getvalue()


def test_evidence_dedupes_verifies_and_handles_spaces(tmp_path: Path) -> None:
    store = EvidenceStore(
        tmp_path / "path with spaces", "0" * 26, clock=FixedClock(), ids=SequenceIds()
    )
    payload = redact(b"safe evidence")
    assert isinstance(payload, RedactedBytes)
    first = store.put(payload, "text/plain", kind="operator_note")
    second = store.put(payload, "text/plain", kind="operator_note")
    assert first == second and store.get(first) == b"safe evidence" and store.verify(first)
    store._path(first).unlink()
    assert not store.exists(first) and not store.verify(first)
    store.close()


def test_evidence_strict_sink_rejects_raw_bytes(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path, "0" * 26, clock=FixedClock(), ids=SequenceIds())
    with pytest.raises(RedactionRequiredError):
        store.put(cast(RedactedBytes, b"raw"), "text/plain", kind="operator_note")
    store.close()


def _journal(tmp_path: Path) -> RunJournal:
    return RunJournal(tmp_path, "0" * 26, clock=FixedClock())


def test_journal_detects_tamper_truncate_reorder_and_lock(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    journal.record(RunStarted(kind="replay"))
    journal.record(RunEnded(result="success", summary="ok"))
    assert journal.verify_chain().intact
    with pytest.raises(WriterBusyError):
        _journal(tmp_path)
    lines = journal.path.read_bytes().splitlines(keepends=True)
    journal.path.write_bytes(lines[1] + lines[0])
    assert not journal.verify_chain().intact
    journal.path.write_bytes(b"".join(lines[:-1]))
    assert "Tail differs" in journal.verify_chain().reason
    journal.path.write_bytes(b"".join(lines))
    damaged = json.loads(lines[0])
    damaged["payload"]["kind"] = "discovery"
    journal.path.write_bytes((json.dumps(damaged) + "\n").encode() + lines[1])
    assert "hash mismatch" in journal.verify_chain().reason
    with pytest.raises(BrokenChainError):
        journal.record(RunEnded(result="success", summary="again"))
    journal.close()


def test_journal_append_after_verification(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    journal.record(RunStarted(kind="replay"))
    assert journal.verify_chain().intact
    journal.record(RunEnded(result="success", summary="ok"))
    assert journal.verify_chain().intact and journal.head.seq == 2
    journal.close()


class Capture(SpanExporter):
    def __init__(self) -> None:
        self.spans: list[ReadableSpan] = []

    def export(self, spans: list[ReadableSpan] | tuple[ReadableSpan, ...]) -> SpanExportResult:
        self.spans.extend(spans)
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        pass


def test_trace_export_redacts_and_local_otlp_parses(tmp_path: Path) -> None:
    context = new_run("replay", clock=FixedClock(), ids=SequenceIds())
    capture = Capture()
    with run_scope(context):
        tracing = Tracing(
            tmp_path,
            clock=FixedClock(),
            ids=SequenceIds(),
            extra_exporters=(capture,),
            remote=False,
        )
        with tracing.span("action") as span:
            span.set_attribute("member", "123-45-6789")
            span.set_attribute("run_id", "overwritten")
        tracing.close()
    assert capture.spans[0].attributes is not None
    attributes = capture.spans[0].attributes
    assert attributes["run_id"] == context.run_id
    assert "123-45-6789" not in str(attributes["member"])
    line = tracing.path.read_text().splitlines()[0]
    assert json.loads(line)["resourceSpans"]


def test_manifest_is_complete_and_golden(
    tmp_path: Path, golden_file: Callable[[Path, bytes], bytes]
) -> None:
    context = new_run("replay", clock=FixedClock(), ids=SequenceIds())
    with run_scope(context):
        ids = SequenceIds()
        journal = RunJournal(tmp_path, context.run_id, clock=FixedClock())
        evidence = EvidenceStore(tmp_path, context.run_id, clock=FixedClock(), ids=ids)
        tracing = Tracing(tmp_path, clock=FixedClock(), ids=ids, remote=False)
        journal.record(RunStarted(kind="replay"))
        with tracing.span("run"):
            pass
        tracing.close()
        journal.record(RunEnded(result="success", summary="completed"))
        manifest = write_manifest(
            journal=journal,
            evidence=evidence,
            tracing=tracing,
            clock=FixedClock(),
            inputs=redact({"member_id": "123-45-6789"}),
            result_summary=redact({"status": "success"}),
        )
        assert manifest.chain.intact and manifest.evidence_verified
        expected = (
            json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
        ).encode()
        golden_file(Path("tests/golden/manifest.json"), expected)
        assert json.loads((tmp_path / context.run_id / "manifest.json").read_text())
        evidence.close()
        journal.close()
